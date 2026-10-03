import asyncio
import base64
import difflib
import hashlib
import ipaddress
import json
import logging
import re
import secrets
import socket
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from functools import lru_cache
from datetime import datetime, timedelta
from email.utils import format_datetime
from xml.sax.saxutils import escape as xml_escape

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from markupsafe import Markup, escape
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from . import accounts, auto, cards, credits, db, llm, markets, memoria, news, settings
from .fetch import FetchError, check_url, extract, safe_get
from .ingest import Charged, UserError, fingerprint, load_image, png_bytes
from .pipeline import Duplicate, investigate, publish_decision
from .rules import RATINGS, concentration, source_group
from .search import canonical
from .similar import dhash, embed, text_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("contraste")

TOPICS = llm.TOPICS
def media_url(row, name: str) -> str:
    """Versioned like the cards: the stamped thumbnail changes when the rating does, and a fixed URL let browsers
    and Cloudflare keep showing the old band for hours after a re-investigation changed it."""
    v = hashlib.sha1(f"{row['rating']}|{row['updated_at']}".encode()).hexdigest()[:10]
    return f"/media/{name}?v={v}"


def image_of(row) -> dict:
    """Listing image for an article: the stamped thumbnail of the original if there is one, otherwise
    its cover card. Never the unstamped original."""
    thumb = (json.loads(row["result"]).get("media") or {}).get("thumb")
    if thumb:
        return {"src": media_url(row, thumb), "original": True,
                "alt": f"Miniatura del contenido original con el sello {RATINGS[row['rating']]} y rostros difuminados"}
    return {"src": cards.card_url(row, "cover"), "alt": "", "original": False}


_topics: tuple[float, list] = (0.0, [])


def topics_in_use() -> list[tuple[str, str]]:
    """Sections that have published checks, most populated first. Empty sections are not shown: they only
    led to empty pages. Cached for a minute per process."""
    global _topics
    if time.monotonic() - _topics[0] > 60:
        rows = db.q("""SELECT topic, count(*) AS n FROM articles WHERE status='listed' AND NOT demo
                       GROUP BY topic ORDER BY n DESC, topic""")
        _topics = (time.monotonic(), [(r["topic"], TOPICS[r["topic"]]) for r in rows if r["topic"] in TOPICS])
    return _topics[1]


_ticker: tuple[float, list] = (0.0, [])


def ticker() -> list[dict]:
    """Headlines for the crawl at the top: what people are checking the most right now, then the newest.
    Cached for a minute per replica."""
    global _ticker
    if time.monotonic() - _ticker[0] > 60:
        rows = db.q("""SELECT id, slug, created_at, title, rating FROM articles WHERE status='listed' AND NOT demo
                       ORDER BY score DESC, created_at DESC LIMIT 12""")
        _ticker = (time.monotonic(), rows)
    return _ticker[1]


@lru_cache(maxsize=None)
def asset(name: str) -> str:
    """/static URL with a hash of the file, so it can be cached for a year and still update on deploy."""
    return f"/static/{name}?v={hashlib.sha1((settings.APP_DIR / 'static' / name).read_bytes()).hexdigest()[:10]}"


cards.env.globals.update(media_url=media_url, topics_in_use=topics_in_use, memoria_highlights=memoria.highlights, news_example=news.example, asset=asset, ticker=ticker, markets=markets.current, market_value=markets.show, TURNSTILE=settings.TURNSTILE_SITE_KEY, free_credits=credits.free_monthly,
                         daily_limit=credits.daily_limit, renews_on=credits.renews_on, SOURCE_URL=settings.SOURCE_URL, GOOGLE=bool(settings.GOOGLE_OAUTH_CLIENT_ID),
                         TOPICS=TOPICS, path_of=db.path_of, DEMO=settings.DEMO_MODE, BASE=settings.PUBLIC_BASE_URL,
                         image_of=image_of)


# --- Rate limits (shared by every replica through Postgres) ----------------------------------

def limited(key: str, n: int, window_s: int) -> bool:
    # Keys carry IPs, so only a hash salted with the daily salt (deleted the next day) is stored.
    return db.rate_limited(hashlib.sha256(f"{key}|{db.daily_salt()}".encode()).hexdigest(), n, window_s)


def via_cloudflare(req: Request) -> bool:
    """The request came through Cloudflare: it carries the origin secret Cloudflare adds."""
    sent = req.headers.get("x-contraste-origin", "")
    return bool(settings.ORIGIN_SECRET) and secrets.compare_digest(sent.encode(), settings.ORIGIN_SECRET.encode())


def client_ip(req: Request) -> str:
    """Real visitor IP. In the cluster every request reaches us through nginx with an internal address, so
    the only real IP is CF-Connecting-IP; it is trusted only on requests that prove they came through
    Cloudflare. X-Forwarded-For is never used: here it only ever holds internal addresses, and elsewhere it
    can be forged."""
    if via_cloudflare(req):
        cf = req.headers.get("cf-connecting-ip", "").strip()
        try:
            ipaddress.ip_address(cf)
            return cf
        except ValueError:
            pass
    return req.client.host if req.client else "0.0.0.0"


# --- Background tasks -----------------------------------------------------------------------

_tasks: set[asyncio.Task] = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def finalize(result: dict, keys: dict, thumb, aid: str) -> str:
    status, reason = publish_decision(result)
    if thumb is not None:
        try:
            result["media"]["thumb"] = await cards.stamped_thumb(thumb, result["rating"], aid)
        except Exception:
            log.exception("could not create the thumbnail")
    db.save_article(result, status=status, reason=reason, keys=keys, article_id=aid)
    db.record_consultation(aid)
    row = db.get(aid)
    for fmt in ("og", "post") if cards.shareable(row) else ():
        try:
            await cards.card_png(row, fmt)
        except Exception:
            log.exception("could not render the %s card", fmt)
    return db.path_of(row)


def done_event(aid: str, **extra) -> dict:
    """Final SSE event: where the article is plus what the notification needs to say."""
    row = db.get(aid)
    return {"type": "done", "url": db.path_of(row), "rating": RATINGS[row["rating"]], "title": row["title"], **extra}


async def run_job(job: dict):
    """Run one claimed job. Progress and the final event are written to the jobs table, so any
    replica can stream them and a restart does not lose them."""
    job_id = job["id"]
    llm.JOB.set(job_id)
    if job.get("kind") == "contribution":
        from .contributions import run_contribution
        return await run_contribution(job)
    inp, keys = dict(job["input"]), dict(job["input"].get("keys") or {})
    if keys.get("emb"):
        keys["emb"] = base64.b64decode(keys["emb"])
    progress = 0

    async def emit(label: str, p: int | None, replace: bool = False, **extra):
        """extra: claims=[...], queries=[...] or source={id, name, state, ...} for the progress view."""
        nonlocal progress
        if p is not None:
            progress = max(progress, p)
        db.job_event(job_id, {"type": "step", "label": label, "progress": progress, **extra})

    try:
        if inp["kind"] == "image":
            inp["img"] = load_image(bytes(job["image"]))
        if settings.DEMO_MODE:
            url = await run_demo(emit)
        else:
            await emit("Recibiendo el contenido", 3)
            result, k2, thumb = await investigate(inp, emit)
            if inp.get("auto"):  # chosen by COntraste among the day's news, not asked for by a reader
                result["auto"] = {"headline": inp.get("headline", ""), "source": inp.get("source", "")}
            await emit("Preparando la tarjeta", 94)
            url = await finalize(result, keys | {k: v for k, v in k2.items() if v}, thumb, job_id)
        aid = url.rsplit("-", 1)[-1]
        db.job_finish(job_id, done_event(aid, anon=bool(inp.get("anon"))), article_id=aid)
        if not settings.DEMO_MODE:
            try:
                from .contributions import cross_from
                cross_from(aid)
            except Exception:
                log.exception("cross-validation from %s failed", aid)
    except Duplicate as d:
        # Found to be an existing check halfway through: nothing new was produced, so the credit goes back.
        credits.refund(job_id, "Ya estaba verificado")
        db.record_consultation(d.article_id)
        db.job_finish(job_id, done_event(d.article_id, duplicate=True), article_id=d.article_id)
    except (UserError, FetchError, llm.LLMError) as e:
        # Every rejection says why and whether it was charged. Charged: costly analysis already ran and its
        # message explains it. Anything else (unreadable source, our failure, stopped at the gate) is refunded.
        msg = str(e)
        if not isinstance(e, Charged) and credits.refund(job_id):
            msg += " " + REFUNDED
        db.job_finish(job_id, {"type": "error", "message": msg}, error=msg)
    except Exception:
        log.exception("check %s failed", job_id)
        msg = "Algo falló de nuestro lado. Intenta de nuevo en unos minutos."
        if credits.refund(job_id):
            msg += " " + REFUNDED
        db.job_finish(job_id, {"type": "error", "message": msg}, error=msg)


WORKER_ID = f"{socket.gethostname()}-{secrets.token_hex(3)}"
_running: set[str] = set()
_stopping = asyncio.Event()  # replaced on every startup (tests start the app more than once)
_wake = asyncio.Event()      # set when this replica queues a job, so its worker does not wait for the next poll


async def worker_loop():
    """Claim queued jobs while this replica has free slots. Each replica runs its own loop, so
    capacity grows with the number of replicas."""
    slots = asyncio.Semaphore(settings.MAX_CONCURRENT_CHECKS)
    while not _stopping.is_set():
        await slots.acquire()
        job = None if _stopping.is_set() else await asyncio.to_thread(db.job_claim, WORKER_ID)
        if not job:
            slots.release()
            _wake.clear()
            try:  # other replicas' jobs are found by polling every second
                await asyncio.wait_for(_wake.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
            continue

        async def run(job=job):
            _running.add(job["id"])
            try:
                await run_job(job)
            finally:
                _running.discard(job["id"])
                slots.release()
        spawn(run())


async def maintenance_loop():
    """Heartbeats for our jobs, re-queue jobs of replicas that died, and (in one replica only)
    front-page scores and cleanup."""
    tick = 0
    while not _stopping.is_set():
        try:
            await asyncio.to_thread(db.job_heartbeat, list(_running))
            await asyncio.to_thread(db.job_requeue_stale)
            if tick % 40 == 0:  # every ~10 minutes
                with db.singleton(1) as mine:
                    if mine:
                        await asyncio.to_thread(db.recompute_scores)
                        await asyncio.to_thread(db.jobs_cleanup)
                        if not settings.DEMO_MODE:
                            try:
                                await auto.tick()
                            except Exception:
                                log.exception("check of the day not started")
                            # Second look at a recent check, in the background: it takes minutes, and this loop
                            # also keeps the heartbeats of running jobs.
                            if auto.followup_allowed() and (aid := await asyncio.to_thread(auto.due_followup)):
                                log.info("second look at %s", aid)
                                spawn(reinvestigate(aid))
            if settings.MARKETS and tick % 120 == 0:  # every ~30 minutes, one replica
                with db.singleton(3) as mine:
                    if mine:
                        await markets.refresh()
                        await news.refresh()
        except Exception:
            log.exception("maintenance tick failed")
        tick += 1
        try:
            await asyncio.wait_for(_stopping.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass


async def run_demo(emit) -> str:
    from .demo import zuluaga
    await emit("Leyendo la imagen", 10)
    await asyncio.sleep(0.8)
    await emit("Separando las afirmaciones", 34, claims=["La despidieron", "Por criticar a Abelardo", "Con Petro nunca peligró"])
    await asyncio.sleep(0.8)
    await emit("Buscando en Colombia y el mundo", 35, queries=["Camila Zuluaga Blu Radio programa", "Mañanas Blu salida del aire",
                                                                 "Caracol Televisión audiencia anunciantes"])
    names = ["La Silla Vacía", "Semana", "El Espectador", "Infobae", "colombia.com", "publimetro.co", "El Tiempo"]
    for i, n in enumerate(names):
        await emit(f"Leyendo {n}", 45, source={"id": str(i), "name": n, "state": "reading"})
        await asyncio.sleep(0.25)
    for i, n in enumerate(names):
        state = {"state": "omitted", "reason": "Muro de pago"} if n == "El Tiempo" else {"state": "ok"}
        await emit(f"Leyendo {n}", 55, source={"id": str(i), "name": n, **state})
        await asyncio.sleep(0.2)
    for i, n in enumerate(names[:-1]):
        await emit(f"Contrastando · {i + 1} de 6 fuentes", 60 + 4 * i,
                   source={"id": str(i), "name": n, "state": "assessed", "stance": "contradice" if n == "El Espectador" else "contexto"})
        await asyncio.sleep(0.5)
    await emit("Contrastando y calificando", 85)
    await asyncio.sleep(1)
    await emit("Preparando la tarjeta", 94)
    row = db.q1("SELECT * FROM articles WHERE demo AND title=%s", zuluaga()["title"])
    db.record_consultation(row["id"])
    return db.path_of(row)


# --- Re-investigation -----------------------------------------------------------------------

async def reinvestigate(aid: str) -> None:
    row = db.get(aid)
    if not row:
        return
    # Atomic claim: with several replicas, two could pass the read-check and re-investigate twice.
    if not db.q1("UPDATE articles SET reinvestigating=TRUE WHERE id=%s AND NOT reinvestigating RETURNING id", aid):
        return
    try:
        old = json.loads(row["result"])
        text = old["circulating"] + "\n" + "\n".join(c["text"] for c in old["claims"])

        async def quiet(*a, **k):
            pass

        new, _, _ = await investigate({"kind": "text", "text": text}, quiet, dedup=False, screen=False)
        if len(new["sources"]) * 2 < len(old["sources"]):
            # Search engines failing (all of them timed out from one node for an hour) leave a run with a
            # fraction of the evidence; it must not replace a better published check. Checked again next time.
            log.warning("re-investigation of %s found %d sources against %d published: kept the published one",
                        aid, len(new["sources"]), len(old["sources"]))
            return
        new["input"], new["media"], new["title"] = old["input"], old["media"], old["title"]
        new["usage"] = llm.merge_usage(old.get("usage"), new.get("usage"))
        new["security"]["input_injection"] = old.get("security", {}).get("input_injection", False)
        # Also counts as a change: sources that no longer qualify (e.g. an open encyclopedia) and a claim
        # whose own rating moved, even if the overall rating stayed.
        changed = new["rating"] != old["rating"] or {s["url"] for s in new["sources"]} != {s["url"] for s in old["sources"]} \
            or [c["rating"] for c in new["claims"]] != [c["rating"] for c in old["claims"]]
        if changed:
            status, reason = publish_decision(new)
            if row["status"] == "removed":
                status, reason = "removed", row["unlisted_reason"]
            # Stamp first, then save: the saved article changes the thumbnail's URL version, which must never
            # point at the old band (browsers keep a versioned image for a year).
            if new["rating"] != old["rating"]:
                await cards.restamp(aid, new["rating"])
            db.update_article(aid, new, status=status, reason=reason, old_rating=old["rating"],
                              change=("actualizacion", "Se encontró nueva evidencia." if new["rating"] != old["rating"]
                                      else "Se actualizaron las fuentes con el método actual; la calificación general no cambió."))
        else:
            old["last_checked"] = db.iso()
            old["usage"] = llm.merge_usage(old.get("usage"), new.get("usage"))
            db.q("UPDATE articles SET result=%s WHERE id=%s", json.dumps(old, ensure_ascii=False), aid)
    except Exception:
        log.exception("re-investigation failed for %s", aid)
    finally:
        db.q("UPDATE articles SET reinvestigating=FALSE WHERE id=%s", aid)


def maybe_reinvestigate(aid: str):
    row = db.get(aid)
    if settings.DEMO_MODE or not row or row["rating"] != "sin_pruebas":
        return
    last = json.loads(row["result"]).get("last_checked") or row["updated_at"]
    if db.now() - datetime.fromisoformat(last) > timedelta(days=7):
        spawn(reinvestigate(aid))


# --- App ------------------------------------------------------------------------------------

async def warm_cards():
    """After a deploy the card cache is empty: draw the images the front page and articles show before the
    first visitor has to wait for them. One worker does it; the cache is shared on disk."""
    await asyncio.sleep(5)
    with db.singleton(4) as mine:
        if mine:
            await _warm_cards()


async def _warm_cards():
    rows = await asyncio.to_thread(db.q, """SELECT * FROM articles WHERE status='listed' AND NOT demo
                                            ORDER BY score DESC, created_at DESC LIMIT 30""")
    for row in rows:
        for fmt in ("cover", "post"):
            if _stopping.is_set():
                return
            try:
                await cards.card_webp(row, fmt)
            except Exception as e:
                log.warning("could not pre-render the %s card of %s: %s", fmt, row["id"], e)


GRACE_S = 140  # stay under the container's stop grace period (docker-compose: 3 minutes)


@asynccontextmanager
async def lifespan(app):
    global _stopping, _wake
    _stopping, _wake = asyncio.Event(), asyncio.Event()
    from .migrate_sqlite import migrate
    await asyncio.to_thread(migrate)
    # Load the embedding model before taking traffic: loading it on the first check made that person
    # wait 7-9 s with nothing on screen.
    await asyncio.to_thread(embed, "precarga")
    if settings.DEMO_MODE:
        from .demo import seed
        with db.singleton(2) as mine:
            if mine:
                seed()
    spawn(worker_loop())
    spawn(maintenance_loop())
    if settings.WARM_CARDS and not settings.DEMO_MODE:
        spawn(warm_cards())
    yield
    # Graceful stop: take no new jobs, give running ones time to finish, hand the rest back.
    _stopping.set()
    deadline = time.monotonic() + GRACE_S
    while _running and time.monotonic() < deadline:
        await asyncio.sleep(1)
    if _running:
        db.q("UPDATE jobs SET status='queued', worker=NULL, events='[]' WHERE worker=%s AND status='running'", WORKER_ID)
    await cards.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# HTML, CSS and JS go compressed (images, fonts and the progress stream are left alone by default).
app.add_middleware(GZipMiddleware, minimum_size=800, compresslevel=6)
app.mount("/static", StaticFiles(directory=settings.APP_DIR / "static"), name="static")

_TS = " https://challenges.cloudflare.com" if settings.TURNSTILE_SITE_KEY else ""  # captcha script and frame
# 'inline-speculation-rules' allows only the prerender rules in base.html, not inline scripts.
CSP = (f"default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self' 'inline-speculation-rules'{_TS}; font-src 'self'; "
       f"connect-src 'self'; frame-src{_TS or " 'none'"}; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")


# Shared responses: a page that is the same for every anonymous visitor is built once and reused for a few
# seconds, and versioned files (CSS, JS, fonts, card images) are kept in memory. Nothing personal is ever
# stored: requests with a session cookie are built per request, and so is anything that sets a cookie.
_shared: "OrderedDict[tuple, tuple[float, int, dict, bytes]]" = OrderedDict()
PAGE_TTL, FILE_TTL, SHARED_MAX = 60, 3600, 400  # a new check reaches anonymous visitors within a minute
_PER_REQUEST = ("/api/", "/media/", "/admin", "/auth", "/dev", "/cuenta", "/encuesta", "/entrar", "/salir", "/healthz")


def _shared_ttl(request: Request) -> int:
    if request.method != "GET":
        return 0
    path = request.url.path
    if path.startswith("/static/") or (path.startswith("/api/cards/") and "v" in request.query_params):
        return FILE_TTL
    if accounts.COOKIE in request.cookies or "contraste_admin" in request.cookies or path.startswith(_PER_REQUEST):
        return 0
    return PAGE_TTL


@app.middleware("http")
async def security(request: Request, call_next):
    if settings.ORIGIN_SECRET and request.url.path != "/healthz" and not via_cloudflare(request):
        # The nodes answer on their own public IPs too: going around Cloudflare skips its protections.
        return PlainTextResponse(f"Entra por {settings.PUBLIC_BASE_URL}", status_code=403)
    ttl = _shared_ttl(request)
    key = (request.url.path, request.url.query, "gzip" in request.headers.get("accept-encoding", "")) if ttl else None
    if key and (hit := _shared.get(key)) and time.monotonic() - hit[0] < ttl:
        return Response(hit[3], status_code=hit[1], headers=hit[2])
    if int(request.headers.get("content-length") or 0) > settings.MAX_IMAGE_BYTES + 100_000:
        return JSONResponse({"error": "El archivo pesa más de 10 MB."}, status_code=413)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
            return JSONResponse({"error": "Origen no permitido."}, status_code=403)
    if request.url.path.startswith("/admin") and settings.ADMIN_ALLOWED_IPS and \
            client_ip(request) not in settings.ADMIN_ALLOWED_IPS:
        return not_found()
    renewed = False
    if accounts.COOKIE in request.cookies and not request.url.path.startswith(("/static/", "/media/")):
        user, csrf, renewed = await asyncio.to_thread(accounts.load_session, request)
        accounts.CURRENT_USER.set(user)
        accounts.CURRENT_CSRF.set(csrf)
    resp = await call_next(request)
    if renewed and "set-cookie" not in resp.headers:  # the session was extended: the browser keeps it too
        accounts.set_session_cookie(resp, request.cookies[accounts.COOKIE])
    path = request.url.path
    if path.startswith("/static/"):
        # CSS and JS URLs carry a hash of their content and fonts never change in place: keep them a year.
        versioned = "v" in request.query_params or path.startswith("/static/fonts/")
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if versioned else "public, max-age=86400"
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if key and resp.status_code == 200 and "set-cookie" not in resp.headers:
        body = b"".join([chunk async for chunk in resp.body_iterator])
        headers = dict(resp.headers)
        _shared[key] = (time.monotonic(), 200, headers, body)
        _shared.move_to_end(key)
        while len(_shared) > SHARED_MAX:
            _shared.popitem(last=False)
        return Response(body, status_code=200, headers=headers)
    return resp


def page(name: str, status: int = 200, **ctx) -> HTMLResponse:
    user = accounts.CURRENT_USER.get()
    ctx.setdefault("user", user)
    ctx.setdefault("csrf", accounts.CURRENT_CSRF.get())
    if user:
        ctx.setdefault("balance", credits.balances(user))
        if name != "encuesta.html":
            from .survey import reward_amount, should_prompt
            if should_prompt(user, ctx["balance"]):
                ctx.setdefault("survey_prompt", reward_amount())
    return HTMLResponse(cards.env.get_template(name).render(**ctx), status_code=status)


def not_found(msg="No encontramos esta página.", status=404):
    return page("error.html", status=status, message=msg)


@app.get("/healthz")
def healthz(live: bool = False):
    # live=1 is the liveness probe: only "the process answers". A slow or briefly unreachable database must
    # take a pod out of rotation (readiness), never get healthy pods killed and replaced.
    if not live:
        db.q1("SELECT 1")
    return {"ok": True, "demo": settings.DEMO_MODE, "llm_configured": bool(settings.OPENROUTER_API_KEY and settings.OPENROUTER_MODEL)}


# --- API ------------------------------------------------------------------------------------

async def page_changed(url: str, aid: str) -> bool:
    """True when a page checked before now has sentences it did not have then. Only for web pages (posts
    on video platforms are checked by their media); if the page cannot be read, the old check stands."""
    old = (json.loads(db.get(aid)["result"]).get("input") or {}).get("fingerprint")
    if not old:
        return False
    try:
        final, body, ctype = await safe_get(url)
        page = await asyncio.to_thread(extract, body, final)
    except Exception:
        return False
    return bool(set(fingerprint(page["text"])) - set(old))


REFUNDED = "No se descontó de tus verificaciones."
PAUSED = "Pausamos las verificaciones nuevas por hoy. Puedes consultar todo el archivo."
PER_USER_OPEN = 2   # checks one account can have queued or running at once
ANON_USED = ("Ya hiciste tu verificación sin cuenta de hoy. Crea tu cuenta gratis: tienes {n} verificaciones al mes "
             "y guardas tu historial.")
ANON_SPENT = ("Las verificaciones sin cuenta de hoy se agotaron. Crea tu cuenta gratis para seguir: tienes {n} "
              "verificaciones al mes.")


async def anonymous_check(request: Request, inp: dict, keys: dict, image_png, captcha: str):
    """One check a day without an account, to try COntraste before signing up. Captcha required; counted per
    visitor with the daily salted hash (no IP is stored); all of them together use at most ANON_SHARE of the
    day's model budget. Every time someone is asked to sign up it is counted, to measure the friction."""
    if not await accounts.turnstile_ok(captcha, request, "check"):
        return JSONResponse({"error": "No pudimos confirmar que no eres un robot. Intenta de nuevo."}, status_code=400)
    cap = settings.DAILY_SPEND_LIMIT_USD
    if cap and db.spend_today() >= cap:
        return JSONResponse({"error": PAUSED}, status_code=503)
    job_id = db.new_id()
    n = credits.free_monthly()
    if not credits.anon_budget_left():
        db.bump("login_prompt")
        return JSONResponse({"error": "login_required", "message": ANON_SPENT.format(n=n)}, status_code=401)
    if not credits.anon_claim(db.visitor_hash(client_ip(request), "", "anon-check"), job_id):
        db.bump("login_prompt")
        return JSONResponse({"error": "login_required", "message": ANON_USED.format(n=n)}, status_code=401)
    db.job_create(job_id, inp | {"keys": keys, "anon": True}, image_png)
    db.bump("anon_check")
    _wake.set()
    return {"id": job_id, "position": db.job_position(job_id)}


@app.post("/api/checks")
async def create_check(request: Request, url: str | None = Form(None), text: str | None = Form(None),
                       image: UploadFile | None = File(None), captcha: str = Form("", alias="cf-turnstile-response")):
    """The API only takes `url`, `text` or `image`. No prompts, models or tuning parameters."""
    given = [x for x in (url and url.strip(), text and text.strip(), image) if x]
    if len(given) != 1:
        return JSONResponse({"error": "Envía un enlace, un texto o una imagen (solo uno)."}, status_code=400)
    if limited("check:" + client_ip(request), 10, 600):
        return JSONResponse({"error": "Hiciste muchas verificaciones seguidas. Espera unos minutos."}, status_code=429)

    keys: dict = {}
    image_png = None
    try:
        if image:
            raw = await image.read(settings.MAX_IMAGE_BYTES + 1)
            img = load_image(raw)
            keys["phash"] = f"{dhash(img):016x}"
            inp = {"kind": "image"}
            image_png = png_bytes(img)
            dup = db.find_duplicate(phash=keys["phash"])
        elif url and url.strip():
            u = url.strip()
            if len(u) > 2000:
                raise UserError("El enlace es demasiado largo.")
            if not re.match(r"^https?://", u, re.I):
                u = "https://" + u
            await check_url(u)
            keys["url_key"] = canonical(u)
            inp = {"kind": "url", "url": u}
            dup = db.find_duplicate(url_key=keys["url_key"])
            if dup and await page_changed(u, dup):
                dup = None
        else:
            t = text.strip()
            if len(t) > settings.MAX_TEXT_CHARS:
                raise UserError(f"El texto es muy largo (máximo {settings.MAX_TEXT_CHARS:,} caracteres).".replace(",", "."))
            if len(t) < 12:
                raise UserError("Escribe la afirmación completa que quieres verificar.")
            if re.fullmatch(r"https?://\S+", t):
                return await create_check(request, url=t, text=None, image=None, captcha=captcha)
            keys["text_key"] = text_key(t)
            emb = await asyncio.to_thread(embed, t)
            keys["emb"] = emb.tobytes()
            inp = {"kind": "text", "text": t}
            dup = db.find_duplicate(text_key=keys["text_key"], emb=emb)
    except (UserError, FetchError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    if dup and not settings.DEMO_MODE:
        db.record_consultation(dup, db.visitor_hash(client_ip(request), "", dup))  # IP only: renaming the browser gains nothing
        maybe_reinvestigate(dup)
        return {"id": dup, "url": db.path_of(db.get(dup)), "duplicate": True}

    # Something new: from here on it costs a check.
    if keys.get("emb"):
        keys["emb"] = base64.b64encode(keys["emb"]).decode()
    user = accounts.CURRENT_USER.get()
    if not user:
        return await anonymous_check(request, inp, keys, image_png, captcha)
    if not accounts.csrf_ok(request, request.headers.get("x-csrf")):
        return JSONResponse({"error": "Tu sesión venció. Recarga la página."}, status_code=403)
    if not await accounts.turnstile_ok(captcha, request, "check"):
        return JSONResponse({"error": "No pudimos confirmar que no eres un robot. Intenta de nuevo."}, status_code=400)
    mine = db.q1("SELECT COUNT(*) AS n FROM jobs WHERE user_id=%s AND kind='check' AND status IN ('queued', 'running')",
                 user["id"])["n"]
    if mine >= PER_USER_OPEN:
        return JSONResponse({"error": "Ya tienes 2 verificaciones en curso. Espera a que termine una."}, status_code=429)
    if credits.used_today(user) >= (limit := credits.daily_limit()):
        return JSONResponse({"error": f"Ya hiciste las {limit} verificaciones de hoy. Mañana tienes más; mientras tanto "
                                      "puedes consultar todo el archivo."}, status_code=429)
    # Half of the day's model budget is kept for accounts older than 3 days, so a batch of fresh accounts
    # cannot use it all up and pause checks for everyone else.
    spent, cap = db.spend_today(), settings.DAILY_SPEND_LIMIT_USD
    new_account = db.now() - user["created_at"] < timedelta(days=3)
    if cap and (spent >= cap or (new_account and spent >= cap / 2)):
        return JSONResponse({"error": PAUSED}, status_code=503)
    job_id = db.new_id()
    try:
        credits.spend(user, job_id)
    except credits.Blocked as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except credits.NoCredits:
        from .survey import no_credits_response
        return no_credits_response(user)
    try:
        db.job_create(job_id, inp | {"keys": keys}, image_png, user_id=user["id"])
    except Exception:
        credits.refund(job_id)
        raise
    remaining = credits.balances(user)["total"]
    db.bump("check")
    _wake.set()
    return {"id": job_id, "position": db.job_position(job_id), "remaining": remaining}


@app.get("/api/checks/{job_id}/events")
async def events(job_id: str, request: Request):
    """Progress read from the jobs table, so any replica can serve it. Each connection lasts up to
    25 s; the browser reconnects on its own and Last-Event-ID resumes where it left off."""
    try:
        start = int(request.headers.get("last-event-id", "-1")) + 1
    except ValueError:
        start = 0

    def sse(ev: dict, i: int | None = None) -> str:
        return (f"id: {i}\n" if i is not None else "") + f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    async def stream():
        sent, position, t0 = start, None, time.monotonic()
        yield "retry: 1000\n\n"
        while time.monotonic() - t0 < 25:
            job = await asyncio.to_thread(db.job_get, job_id)
            if not job:
                row = db.get(job_id)
                yield sse(done_event(job_id) if row else
                          {"type": "error", "message": "Esta verificación ya no está en curso. Envíala de nuevo."})
                return
            evs = job["events"]
            if len(evs) < sent:  # the job was re-queued and started over
                sent = 0
            for i, ev in enumerate(evs[sent:], start=sent):
                yield sse(ev, i)
            sent = len(evs)
            if job["status"] in ("done", "error"):
                return
            if job["status"] == "queued":
                p = await asyncio.to_thread(db.job_position, job_id)
                if p != position:
                    position = p
                    yield sse({"type": "queued", "position": p})
            await asyncio.sleep(0.7)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/cards/{aid}/{fmt}.webp")
async def card_webp(aid: str, fmt: str, request: Request):
    row = db.get(aid)
    if not row or row["status"] == "removed" or fmt not in cards.FORMATS or not cards.shareable(row):
        return Response(status_code=404)
    long = request.query_params.get("v") == cards.card_url(row, fmt).rsplit("v=", 1)[1]
    return Response(await cards.card_webp(row, fmt), media_type="image/webp",
                    headers={"Cache-Control": "public, max-age=31536000, immutable" if long else "public, max-age=300"})


@app.get("/api/cards/{aid}/{fmt}.png")
async def card(aid: str, fmt: str):
    row = db.get(aid)
    if not row or row["status"] == "removed" or fmt not in cards.FORMATS or not cards.shareable(row):
        return Response(status_code=404)
    return Response(await cards.card_png(row, fmt), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=300"})


@app.get("/media/{name}")
def media(name: str, request: Request):
    m = re.fullmatch(r"([a-z0-9]{6})\.jpg", name)
    row = db.get(m.group(1)) if m else None
    item = db.media_get(name) if row and row["status"] != "removed" else None
    if not item:
        return Response(status_code=404)
    versioned = "v" in request.query_params  # the version changes with the image: it can be kept a year
    return Response(bytes(item["data"]), media_type=item["mime"],
                    headers={"Cache-Control": "public, max-age=31536000, immutable" if versioned else "public, max-age=300"})


BOT_UA = re.compile(r"bot|crawl|spider|slurp|headless|phantom|puppeteer|playwright|selenium|lighthouse|curl|wget|"
                    r"python|httpx|requests|aiohttp|go-http|java/|okhttp|facebookexternalhit|whatsapp|preview|"
                    r"scrapy|node-fetch|axios|libwww|monitor|uptime", re.I)


@app.post("/api/views/{aid}")
async def view(aid: str, request: Request):
    """Count a view when the browser sends the beacon after 5+ visible seconds. No cookies, no raw IP/UA stored."""
    row = db.get(aid)
    if not row or row["status"] == "removed":
        return Response(status_code=204)
    ip, ua = client_ip(request), request.headers.get("user-agent", "")
    visitor = db.visitor_hash(ip, ua, aid)
    try:
        body = json.loads((await request.body())[:200] or b"{}")
        visible_ms = int(body.get("visible_ms", 0))
    except (ValueError, TypeError, AttributeError):
        visible_ms = 0
    reason = None
    if not ua or BOT_UA.search(ua):
        reason = "bot"
    elif visible_ms < 5000:
        reason = "poco_tiempo"
    elif limited("views:" + ip, 120, 3600):
        # Per IP only: changing the browser name on every request must not buy more views. Nothing is
        # written for these, so a flood cannot fill the database either.
        return Response(status_code=204)
    if reason:
        db.reject_view(aid, reason, visitor)
    elif not db.count_view(aid, visitor):
        db.reject_view(aid, "repetida", visitor)
    return Response(status_code=204)


# --- Pages ---------------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home():
    return page("home.html",
                active=[r for r in db.listed("score", 20) if r["rating"] in ("falso", "enganoso")][:3],
                consulted=db.listed("consulted", 5), viewed=db.listed("viewed", 5), recent=db.listed("recent", 8))


@app.get("/archivo", response_class=HTMLResponse)
def archive(q: str = "", rating: str = Query("", alias="calificacion"), topic: str = Query("", alias="tema"),
            page_num: int = Query(1, alias="pagina")):
    # Query names stay in Spanish: they are part of the public URLs.
    rating = rating if rating in RATINGS else None
    topic = topic if topic in TOPICS else None
    page_num = max(1, min(page_num, 500))
    rows = db.listed("recent", 21, rating=rating, topic=topic, search=q[:120] or None, offset=(page_num - 1) * 20)
    return page("archive.html", rows=rows[:20], more=len(rows) > 20, q=q[:120], rating=rating, topic=topic, page_num=page_num)


@app.get("/v/{aid}")
def short(aid: str):
    row = db.get(aid)
    return RedirectResponse(db.path_of(row), status_code=301) if row else not_found()


AI_ASK = ("Te comparto una verificación de COntraste, un verificador de desinformación de Colombia. Analízala con "
          "pensamiento crítico: ¿la evidencia sostiene cada calificación?, ¿qué falta?, ¿qué contraargumentos o fuentes "
          "podrían cambiarla? Si buscas en la web, cuenta solo lo que aporte algo nuevo (un hecho, un documento, una "
          "versión, una fecha); si lo que encuentras repite lo mismo, dilo en una línea en vez de enumerarlo. Si aparece "
          "una afirmación concreta que valga la pena verificar, propónla como enlace listo para verificar en COntraste: "
          "{base}/?verificar= seguido de la afirmación codificada para URL. Responde en español.\n\n")
AI_MAX = 6000  # Markdown chars in a prefilled prompt: Claude cuts at ~14,000 and long links fail on some servers


def article_markdown(row, r: dict, full: bool = True) -> str:
    """The check as Markdown, to continue with an AI assistant or read without HTML. `full` adds the evidence of
    each claim and every source; the short form is what fits in a prefilled prompt."""
    url = settings.PUBLIC_BASE_URL + db.path_of(row)
    src = r.get("sources", [])
    out = [f"# {r['title']}", "", f"> Uso responsable: cita a COntraste y enlaza esta verificación; no la uses para producir "
           f"desinformación ni alteres su calificación. Detalles: {settings.PUBLIC_BASE_URL}/llms.txt", "",
           f"**Calificación: {RATINGS[r['rating']]}.** {r['headline']}", "",
           f"Verificación de COntraste del {row['created_at'][:10]}: {url}", "", "## Lo que circula", "", f"> {r['circulating']}",
           "", "## Lo que se verificó", ""]
    for c in r.get("claims", []):
        out += [f"- **{RATINGS[c['rating']]}{' (lo central)' if c.get('central') else ''}:** {c['text']}", f"  {c['explanation']}"]
        if full:
            out += [f"  - {src[e['source']]['name']} ({e['stance']}): {src[e['source']]['url']}"
                    for e in c.get("evidence", []) if e["source"] < len(src)]
    out += [f"- **No verificable:** {n.get('text', '')}" for n in r.get("not_verifiable", []) if n.get("text")]
    out += ["", "## Fuentes", ""]
    for s in src if full else src[:8]:
        out.append(f"- {s['name']}{', ' + s['date'][:10] if s.get('date') else ''}: [{(s.get('title') or s['url'])[:120]}]({s['url']})")
    return "\n".join(out) + "\n"


def ai_prompt(row, r: dict) -> str:
    md = article_markdown(row, r, full=False)
    if len(md) > AI_MAX:
        md = md[:AI_MAX].rsplit("\n", 1)[0] + (f"\n\n(Resumen recortado. La verificación completa, en Markdown: "
                                              f"{settings.PUBLIC_BASE_URL}{db.path_of(row)}.md)\n")
    return AI_ASK.format(base=settings.PUBLIC_BASE_URL) + md


def version_lines(r: dict) -> list[str]:
    """A version as readable lines, for comparing it with the previous one. Sources are sorted by name, so a
    different order is not shown as a change."""
    lines = [f"Título: {r['title']}", f"Calificación: {RATINGS[r['rating']]}", f"Veredicto: {r['headline']}",
             f"Lo que circula: {r['circulating']}"]
    for c in r.get("claims", []):
        lines += [f"Afirmación{' central' if c.get('central') else ''}: {c['text']}",
                  f"   Calificación: {RATINGS[c['rating']]}", f"   Explicación: {c['explanation']}"]
    lines += [f"Fuente: {s['name']}, {s.get('title') or s['url']}" for s in sorted(r.get("sources", []), key=lambda s: (s["name"], s["url"]))]
    return lines


def version_diff(old: list[str], new: list[str], context: int = 1) -> list[dict]:
    """Git-style: "-" removed or changed, "+" added, " " context, "…" unchanged lines folded. In a changed line
    the words that changed are marked, so a new date or a different rating stands out."""
    def words(a: str, b: str):
        wa, wb = a.split(" "), b.split(" ")
        segs_a, segs_b = [], []
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, wa, wb).get_opcodes():
            if i2 > i1:
                segs_a.append((op != "equal", " ".join(wa[i1:i2])))
            if j2 > j1:
                segs_b.append((op != "equal", " ".join(wb[j1:j2])))
        return segs_a, segs_b
    rows = []
    ops = difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
    for k, (op, i1, i2, j1, j2) in enumerate(ops):
        if op == "equal":
            block = old[i1:i2]
            head = block[:context] if k > 0 else []
            tail = block[-context:] if k < len(ops) - 1 and len(block) > context else []
            rows += [{"op": " ", "segs": [(False, t)]} for t in head]
            if len(block) > len(head) + len(tail):
                rows.append({"op": "…", "n": len(block) - len(head) - len(tail)})
            rows += [{"op": " ", "segs": [(False, t)]} for t in tail]
            continue
        a, b = old[i1:i2], new[j1:j2]
        paired = [words(x, y) for x, y in zip(a, b)] if op == "replace" else []
        rows += [{"op": "-", "segs": paired[i][0] if i < len(paired) else [(False, t)]} for i, t in enumerate(a)]
        rows += [{"op": "+", "segs": paired[i][1] if i < len(paired) else [(False, t)]} for i, t in enumerate(b)]
    return rows


@app.get("/v/{year}/{month}/{slug_id}/cambios", response_class=HTMLResponse)
def article_history(year: int, month: int, slug_id: str):
    """Every version of a check and what changed between each one and the previous, with date and time."""
    row = db.get(slug_id.rsplit("-", 1)[-1])
    if not row or row["status"] == "removed":
        return not_found()
    if f"/v/{year}/{month:02d}/{slug_id}" != db.path_of(row):
        return RedirectResponse(db.path_of(row) + "/cambios", status_code=301)
    items, prev = [], None
    for v in db.versions(row["id"]):
        lines = version_lines(json.loads(v["result"]))
        items.append({"n": v["n"], "at": v["at"], "rating": v["rating"], "note": v["note"],
                      "diff": version_diff(prev, lines) if prev is not None else None})
        prev = lines
    return page("historial.html", row=row, path=db.path_of(row), versions=list(reversed(items)),
                changes=db.changes(row["id"]), noindex=True)


@app.get("/v/{year}/{month}/{slug_id}.md")
def article_md(year: int, month: int, slug_id: str):
    row = db.get(slug_id.rsplit("-", 1)[-1])
    if not row or row["status"] == "removed":
        return PlainTextResponse("No encontrado.", status_code=404)
    headers = {} if row["status"] == "listed" else {"X-Robots-Tag": "noindex"}
    return PlainTextResponse(article_markdown(row, json.loads(row["result"])), media_type="text/markdown; charset=utf-8",
                             headers=headers)


@app.get("/v/{year}/{month}/{slug_id}", response_class=HTMLResponse)
def article(year: int, month: int, slug_id: str):
    aid = slug_id.rsplit("-", 1)[-1]
    row = db.get(aid)
    if not row:
        return not_found()
    if row["status"] == "removed":
        return not_found("Este artículo fue retirado por el equipo editorial.", 410)
    canonical_path = db.path_of(row)
    if canonical_path != f"/v/{year}/{month:02d}/{slug_id}":
        return RedirectResponse(canonical_path, status_code=301)
    r = json.loads(row["result"])
    for s in r.get("sources", []):  # articles saved before the ownership rule existed
        s.setdefault("group", source_group(s["domain"]))
    if "diversity" not in r and r.get("sources"):
        conc = concentration([{"source": i, "group": s["group"]} for i, s in enumerate(r["sources"])])
        r["diversity"] = {"groups": len({s["group"] for s in r["sources"]}), "data": 0, "party": 0,
                          "primary": 0, "concentrated": list(conc) if conc else None}
    url = settings.PUBLIC_BASE_URL + canonical_path
    image = f"{settings.PUBLIC_BASE_URL}/api/cards/{aid}/og.png"
    org = {"@type": "Organization", "name": "COntraste", "url": settings.PUBLIC_BASE_URL,
           "logo": {"@type": "ImageObject", "url": f"{settings.PUBLIC_BASE_URL}/static/logo.png", "width": 512, "height": 512}}
    claim_review = None
    if row["status"] == "listed":
        item = {"@type": "Claim", "datePublished": row["created_at"][:10]}
        if r["input"].get("url"):
            item["appearance"] = {"@type": "CreativeWork", "url": r["input"]["url"]}
        claim_review = json.dumps({"@context": "https://schema.org", "@graph": [
            {"@type": "NewsArticle", "@id": url + "#articulo", "headline": r["title"][:110], "description": r["headline"],
             "url": url, "mainEntityOfPage": url, "image": [image], "inLanguage": "es-CO",
             "datePublished": row["created_at"], "dateModified": row["updated_at"],
             "articleSection": TOPICS.get(row["topic"], ""), "author": org, "publisher": org},
            {"@type": "ClaimReview", "url": url, "datePublished": row["created_at"][:10], "claimReviewed": r["circulating"],
             "itemReviewed": item, "author": org, "publisher": org, "inLanguage": "es",
             "reviewRating": {"@type": "Rating", "ratingValue": list(RATINGS).index(r["rating"]) + 1, "bestRating": 1,
                              "worstRating": 6, "alternateName": RATINGS[r["rating"]]}},
            {"@type": "BreadcrumbList", "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Inicio", "item": settings.PUBLIC_BASE_URL + "/"},
                {"@type": "ListItem", "position": 2, "name": TOPICS.get(row["topic"], "Archivo"),
                 "item": f"{settings.PUBLIC_BASE_URL}/archivo?tema={row['topic']}"},
                {"@type": "ListItem", "position": 3, "name": r["title"][:110]}]},
        ]}, ensure_ascii=False).replace("</", "<\\/")
    return page("article.html", row=row, r=r, path=canonical_path, claim_review=claim_review, ai_prompt=ai_prompt(row, r),
                consults=db.consult_count(aid), views=db.view_count(aid), changes=db.changes(aid),
                related=db.related(row), noindex=row["status"] != "listed",
                reviewed_n=db.q1("""SELECT COUNT(*) AS n FROM contributions WHERE article_id=%s AND user_id IS NOT NULL
                                    AND status IN ('accepted','rejected')""",
                                 aid)["n"])


@app.post("/v/{aid}/replica")
async def right_of_reply(aid: str, request: Request):
    """Right of reply (IFCN, EFCSN): whoever a check is about can answer; an editor reads it."""
    form = await request.form()
    row = db.get(aid)
    name, email, message = (str(form.get(k, "")).strip() for k in ("name", "email", "message"))
    url = str(form.get("url", "")).strip()
    if not row or row["status"] == "removed":
        return not_found()
    if limited("reply:" + client_ip(request), 3, 86400):
        return page("error.html", status=429, message="Ya enviaste varias respuestas hoy. Escríbenos mañana.")
    if not (name and message and form.get("consent") and accounts.EMAIL_RE.match(email)) or \
            (url and not re.match(r"^https?://", url)):
        return page("error.html", status=400, message="Completa nombre, correo válido, tu respuesta y la autorización de datos.")
    db.q("INSERT INTO replies(article_id, name, email, message, url) VALUES(%s,%s,%s,%s,%s)",
         aid, name[:160], email[:254], message[:2000], url[:2000] or None)
    return page("error.html", message="Recibimos tu respuesta. Una persona del equipo la leerá y te escribirá.")


@app.get("/como-funciona", response_class=HTMLResponse)
def how():
    return page("como_funciona.html", min_sources=settings.PUBLISH_MIN_SOURCES)


@app.get("/correcciones", response_class=HTMLResponse)
def corrections():
    return page("correcciones.html", rows=[c for c in db.changes(kind="correccion") if c["status"] != "removed"])


def _dev_allowed(request: Request) -> bool:
    """Card previews start Chromium on every request: only in demo mode or for a signed-in editor."""
    from .admin import _session
    return settings.DEMO_MODE or bool(_session(request))


@app.get("/dev/cards", response_class=HTMLResponse)
def dev_cards(request: Request):
    if not _dev_allowed(request):
        return not_found()
    return page("dev_cards.html", formats=cards.FORMATS, noindex=True)


@app.get("/dev/cards/{rating}/{fmt}.png")
async def dev_card(request: Request, rating: str, fmt: str):
    from .demo import zuluaga
    if rating not in RATINGS or fmt not in cards.FORMATS or not _dev_allowed(request):
        return Response(status_code=404)
    r = zuluaga() | {"rating": rating}
    return Response(await cards.render_card(r, "k3x9ab", "2026-09-01T15:00:00+00:00", fmt), media_type="image/png")


# What we ask of every crawler, scraper and AI model that reads COntraste. A request, not a lock: it is said in
# public, in Spanish and English, wherever machines read (robots.txt, llms.txt, each check's Markdown).
USE_ES = ("COntraste publica verificaciones para que la gente esté mejor informada. Puedes leerlas, citarlas y "
          "enlazarlas. No las uses para producir ni difundir desinformación: no inviertas ni alteres sus calificaciones, "
          "no saques afirmaciones de contexto, no las presentes como respaldo de lo que desmienten y no generes contenido "
          "falso a partir de ellas. Cita a COntraste y enlaza la verificación. COntraste no participa en la desinformación.")
USE_EN = ("COntraste publishes fact-checks so people are better informed. You may read, quote and link them. Do not use "
          "them to produce or spread disinformation: do not invert or alter their ratings, take claims out of context, "
          "present them as support for what they debunk, or generate false content from them. Credit COntraste and link "
          "the fact-check. COntraste takes no part in disinformation.")


def simple_markdown(text: str) -> Markup:
    """The few Markdown features AVISO_LEGAL.md uses (headings, paragraphs, lists, bold, links), so the site shows the
    same text as the repository. The text is escaped first: only these tags come out."""
    def inline(s: str) -> str:
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", str(escape(s)))
        return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    out, items = [], []
    for block in text.strip().split("\n\n"):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if all(l.startswith("- ") for l in lines):
            out.append("<ul>" + "".join(f"<li>{inline(l[2:])}</li>" for l in lines) + "</ul>")
        elif lines[0].startswith("## "):
            out.append(f"<h2>{inline(lines[0][3:])}</h2>")
        elif lines[0].startswith("# "):
            out.append(f"<h1>{inline(lines[0][2:])}</h1>")
        else:
            out.append(f"<p>{inline(' '.join(lines))}</p>")
    return Markup("\n".join(out))


@app.get("/aviso-legal", response_class=HTMLResponse)
def legal_notice():
    """The same text as AVISO_LEGAL.md in the repository: one source, never two versions."""
    return page("aviso_legal.html", body=simple_markdown((settings.ROOT / "AVISO_LEGAL.md").read_text(encoding="utf-8")))


@app.get("/robots.txt")
def robots():
    note = "".join(f"# {line}\n" for line in (USE_ES, "", USE_EN, "", f"Más / More: {settings.PUBLIC_BASE_URL}/llms.txt"))
    return Response(note + f"\nUser-agent: *\nDisallow: /admin\nDisallow: /dev/\nDisallow: /api/\n"
                    f"Sitemap: {settings.PUBLIC_BASE_URL}/sitemap.xml\n", media_type="text/plain")


@app.get("/llms.txt")
def llms_txt():
    """For language models and the people who build them (llms.txt convention): what COntraste is, how to read it,
    and what we ask in return."""
    base = settings.PUBLIC_BASE_URL
    return Response(f"""# COntraste

> Verificador de desinformación para Colombia, gratuito y de código abierto. / A free, open-source fact-checker for Colombia.

## Uso responsable / Responsible use

{USE_ES}

{USE_EN}

## Cómo leerlo / How to read it

- Cada verificación está también en Markdown: agrega `.md` a su dirección. / Every fact-check is also in Markdown: add `.md` to its URL.
- Lista completa: {base}/sitemap.xml · Novedades: {base}/feed.xml
- Método y reglas: {base}/como-funciona · Código: {settings.SOURCE_URL}
- La calificación la deciden reglas fijas sobre la evidencia, no la opinión de un modelo. / Ratings come from fixed rules over the evidence, not from a model's opinion.
""", media_type="text/markdown; charset=utf-8")


@app.get("/sitemap.xml")
def sitemap():
    rows = db.q("SELECT * FROM articles WHERE status='listed' ORDER BY created_at DESC LIMIT 50000")
    base = settings.PUBLIC_BASE_URL
    urls = "".join(f"<url><loc>{xml_escape(base + db.path_of(r))}</loc><lastmod>{r['updated_at'][:10]}</lastmod>"
                   f"<image:image><image:loc>{base}/api/cards/{r['id']}/og.png</image:loc></image:image></url>" for r in rows)
    topics = "".join(f"<url><loc>{base}/archivo?tema={t}</loc></url>" for t, _ in topics_in_use())
    return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
                    'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">'
                    f"<url><loc>{base}/</loc></url><url><loc>{base}/como-funciona</loc></url><url><loc>{base}/memoria</loc></url>{topics}{urls}</urlset>",
                    media_type="application/xml")


@app.get("/feed.xml")
def feed():
    rows = db.q("SELECT * FROM articles WHERE status='listed' ORDER BY created_at DESC LIMIT 50")
    items = "".join(
        f"<item><title>{xml_escape(RATINGS[r['rating']] + ': ' + r['title'])}</title>"
        f"<link>{xml_escape(settings.PUBLIC_BASE_URL + db.path_of(r))}</link>"
        f"<guid>{xml_escape(settings.PUBLIC_BASE_URL + '/v/' + r['id'])}</guid>"
        f"<pubDate>{format_datetime(datetime.fromisoformat(r['created_at']))}</pubDate>"
        f"<description>{xml_escape(json.loads(r['result'])['headline'])}</description></item>" for r in rows)
    return Response('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>COntraste</title>'
                    f"<link>{settings.PUBLIC_BASE_URL}/</link><description>Verificaciones publicadas</description>"
                    f"<language>es-co</language>{items}</channel></rss>", media_type="application/rss+xml")


from . import admin  # noqa: E402  (registers /admin)

app.include_router(admin.router)
app.include_router(accounts.router)
from . import contributions, survey  # noqa: E402

app.include_router(survey.router)
app.include_router(memoria.router)
app.include_router(contributions.router)
