"""«Aportar evidencia»: a reader sends 1 to 3 sources that could change a check. The article is investigated
again with fresh searches plus those sources, under the same rules. The reader's note is kept for the
editors only: it never reaches the model and never appears on the article."""
import json
import logging
import re
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import accounts, cards, credits, db, llm, settings
from .fetch import FetchError, check_url
from .pipeline import investigate, publish_decision
from .rules import find_injection, host_of, is_excluded
from .search import canonical

log = logging.getLogger("contraste.contributions")
router = APIRouter()

SOCIAL = ("facebook.com", "fb.watch", "instagram.com", "tiktok.com", "x.com", "twitter.com", "t.co", "youtube.com",
          "youtu.be", "threads.net", "linkedin.com", "reddit.com", "whatsapp.com", "wa.me", "t.me", "telegram.org",
          "snapchat.com", "pinterest.com", "bsky.app", "kwai.com")
PER_ARTICLE, PER_DAY = 3, 10
FREEZE_AT = 5  # contributions to one article within 24 h before auto-updates stop and editors decide
ACCEPTED_NOTE = "nueva evidencia aportada por un lector."
CROSS_NOTE = "nueva evidencia encontrada al verificar un tema relacionado: «{}»."
CROSS_MIN = float(__import__("os").getenv("CROSS_MIN_SIMILARITY", "0.6"))  # closer than this = related topic
CROSS_TARGETS = 2   # related checks one new check may feed
CROSS_SOURCES = 3   # new sources handed to each of them


def is_social(url: str) -> bool:
    h = host_of(url)
    return any(h == d or h.endswith("." + d) for d in SOCIAL)


@router.post("/api/articles/{aid}/contributions")
async def contribute(aid: str, request: Request):
    user = accounts.CURRENT_USER.get()
    if not user:
        return JSONResponse({"error": "login_required"}, status_code=401)
    form = await request.form()
    if not accounts.csrf_ok(request, request.headers.get("x-csrf") or str(form.get("csrf", ""))):
        return JSONResponse({"error": "Tu sesión venció. Recarga la página."}, status_code=403)
    row = db.get(aid)
    if not row or row["status"] == "removed":
        return JSONResponse({"error": "No encontramos esta verificación."}, status_code=404)
    claims = json.loads(row["result"])["claims"]
    try:
        claim = int(form.get("claim", -1))
    except ValueError:
        claim = -1
    if not 0 <= claim < len(claims):
        return JSONResponse({"error": "Elige la afirmación a la que se refiere tu fuente."}, status_code=400)
    note = str(form.get("note", "")).strip()
    if len(note) > 500:
        return JSONResponse({"error": "La nota puede tener hasta 500 caracteres."}, status_code=400)

    urls = []
    for raw in form.getlist("url"):
        u = str(raw).strip()
        if not u:
            continue
        if not re.match(r"^https?://", u, re.I):
            u = "https://" + u
        if len(u) > 2000 or is_social(u):
            return JSONResponse({"error": "Envía enlaces a la fuente (medio, documento, dato oficial), no a redes sociales."},
                                status_code=400)
        if is_excluded(u, set()):
            return JSONResponse({"error": f"{host_of(u)} no se acepta como fuente."}, status_code=400)
        try:
            await check_url(u)
        except FetchError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if canonical(u) not in {canonical(x) for x in urls}:
            urls.append(u)
    if not 1 <= len(urls) <= 3:
        return JSONResponse({"error": "Envía entre 1 y 3 enlaces."}, status_code=400)

    counts = db.q1("""SELECT COUNT(*) FILTER (WHERE article_id=%s) AS here,
                             COUNT(*) FILTER (WHERE created_at > now() - interval '1 day') AS today
                      FROM contributions WHERE user_id=%s""", aid, user["id"])
    if counts["here"] >= PER_ARTICLE:
        return JSONResponse({"error": "Ya enviaste 3 aportes para esta verificación."}, status_code=429)
    if counts["today"] >= PER_DAY:
        return JSONResponse({"error": "Llegaste al máximo de 10 aportes por día."}, status_code=429)
    recent = db.q1("SELECT COUNT(*) AS n FROM contributions WHERE article_id=%s AND created_at > now() - interval '1 day'",
                   aid)["n"]
    cid = "c" + secrets.token_hex(6)
    try:
        credits.spend(user, cid)
    except credits.Blocked as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except (credits.NoCredits, credits.Paused):
        from .survey import no_credits_response
        return no_credits_response(user)
    # Many contributions in a short time look like a campaign: an editor decides instead of the machine.
    status = "frozen" if recent + 1 >= FREEZE_AT else "queued"
    db.q("INSERT INTO contributions(id, user_id, article_id, claim_index, urls, note, status) VALUES(%s,%s,%s,%s,%s,%s,%s)",
         cid, user["id"], aid, claim, json.dumps(urls), note, status)
    if find_injection(note):
        log.warning("contribution %s: note looks like an injection attempt (kept for editors only)", cid)
    if status == "queued":
        enqueue(cid)
    return {"id": cid, "status": status, "remaining": credits.balances(user)["total"]}


def enqueue(cid: str):
    db.job_create(cid, {"contribution": cid}, kind="contribution")
    from .main import _wake
    _wake.set()


def _decide(cid: str, status: str, outcome: str):
    db.q("UPDATE contributions SET status=%s, outcome=%s, decided_at=now() WHERE id=%s", status, outcome, cid)


async def run_contribution(job: dict):
    """Worker side. Accepted: the article is updated, its card redone, the credit refunded plus one extra.
    For review: an editor decides (see approve/reject). Rejected: a private reason, the credit stays spent."""
    cid = job["input"]["contribution"]
    con = db.q1("SELECT * FROM contributions WHERE id=%s", cid)
    try:
        status, outcome = await review(con)
    except Exception:
        log.exception("contribution %s failed", cid)
        credits.refund(cid)
        status, outcome = "rejected", "Algo falló de nuestro lado al revisar tu aporte. Te devolvimos el crédito."
    if status == "review":
        db.q("UPDATE contributions SET status='review', outcome=%s WHERE id=%s", outcome, cid)
    else:
        _decide(cid, status, outcome)
    if status == "accepted":
        _thank(con)
    db.job_finish(job["id"], {"type": "done", "contribution": status, "message": outcome})


def _thank(con: dict):
    if con["user_id"]:  # cross-validation has no reader to thank
        credits.refund(con["id"], "Aporte aceptado")
        credits.reward(con["user_id"], con["id"])


def _note(con: dict) -> str:
    if con.get("origin") and (src := db.get(con["origin"])):
        return CROSS_NOTE.format(src["title"])
    return ACCEPTED_NOTE


def cross_from(aid: str):
    """After a new check: hand the outlets it found (tiers 1-3, confirming or contradicting something) to
    published checks on a related subject that did not have them. Each of those is investigated again with
    them under the same rules, like a reader's contribution: added sources publish on their own, a rating
    change waits for an editor. Nothing cascades: only new checks feed others."""
    cap = settings.DAILY_SPEND_LIMIT_USD
    if settings.DEMO_MODE or (cap and db.spend_today() >= 0.8 * cap):
        return
    found = [s["url"] for s in json.loads(db.get(aid)["result"])["sources"]
             if s["tier"] <= 3 and s["stance"] in ("confirma", "contradice")]
    if not found:
        return
    from .similar import SIMILAR_TEXT
    for other in db.near_articles(aid, CROSS_MIN, SIMILAR_TEXT, CROSS_TARGETS):
        have = {canonical(s["url"]) for s in json.loads(other["result"])["sources"]}
        urls = [u for u in found if canonical(u) not in have][:CROSS_SOURCES]
        recent = db.q1("""SELECT 1 FROM contributions WHERE article_id=%s AND origin IS NOT NULL
                          AND created_at > now() - interval '1 day'""", other["id"])
        if not urls or recent:
            continue
        cid = "x" + secrets.token_hex(6)
        db.q("""INSERT INTO contributions(id, user_id, article_id, claim_index, urls, note, status, origin)
                VALUES(%s, NULL, %s, -1, %s, '', 'queued', %s)""", cid, other["id"], json.dumps(urls), aid)
        enqueue(cid)


async def review(con: dict) -> tuple[str, str]:
    """Investigate again with the contributed sources. Only a source that confirms or contradicts a claim
    counts. It is applied automatically only when it comes from an outlet we can weigh (tier 1-3) and the
    rating stays the same; a rating change, or an unknown site, waits for an editor."""
    aid = con["article_id"]
    row = db.get(aid)
    old = json.loads(row["result"])
    keys = {canonical(u) for u in con["urls"]} - {canonical(s["url"]) for s in old["sources"]}
    if not keys:
        return "rejected", "Esa fuente ya estaba considerada en la verificación."
    text = old["circulating"] + "\n" + "\n".join(c["text"] for c in old["claims"])

    async def quiet(*a, **k):
        pass

    new, _, _ = await investigate({"kind": "text", "text": text}, quiet, dedup=False, extra_urls=tuple(con["urls"]), screen=False)
    found = [s for s in new["sources"] if canonical(s["url"]) in keys]
    used = [s for s in found if s["stance"] in ("confirma", "contradice")]
    if not used:
        if found:
            return "rejected", "La fuente solo aporta contexto: no confirma ni contradice lo que se verificó."
        omitted = [o for o in new["omitted"] if canonical(o["url"]) in keys]
        if omitted:
            return "rejected", f"No pudimos usar la fuente: {omitted[0]['reason'].lower()}."
        if any(canonical(q["url"]) in keys for q in new["audit"]["quotes_rejected"]):
            return "rejected", "La fuente no dice textualmente lo que se le atribuye."
        return "rejected", "La fuente no trata directamente sobre las afirmaciones verificadas."

    # Same bookkeeping as a scheduled re-investigation; the history entry never names the reader.
    new["input"], new["media"], new["title"] = old["input"], old["media"], old["title"]
    new["usage"] = llm.merge_usage(old.get("usage"), new.get("usage"))
    new["security"]["input_injection"] = old.get("security", {}).get("input_injection", False)
    if new["rating"] != old["rating"] or not any(s["tier"] <= 3 for s in used):
        db.q("UPDATE contributions SET pending=%s WHERE id=%s", db.Jsonb(new), con["id"])
        return "review", ("Tu fuente cambiaría la calificación; " if new["rating"] != old["rating"] else
                          "Tu fuente viene de un sitio que no tenemos catalogado; ") + \
            "un editor la revisa antes de publicarla."
    await _apply(aid, row, old, new, _note(con))
    return "accepted", "Tu fuente se agregó a la verificación. Te devolvimos el crédito y te dimos uno extra."


async def _apply(aid: str, row: dict, old: dict, new: dict, note: str = ACCEPTED_NOTE):
    status, reason = publish_decision(new)
    if row["status"] == "removed":
        status, reason = "removed", row["unlisted_reason"]
    db.update_article(aid, new, status=status, reason=reason, old_rating=old["rating"], change=("aporte", note))
    if new["rating"] != old["rating"]:
        await cards.restamp(aid, new["rating"])


async def approve(cid: str) -> bool:
    """Editor accepts a contribution waiting for review: publish the result it produced."""
    con = db.q1("SELECT * FROM contributions WHERE id=%s AND status='review'", cid)
    if not con or not con["pending"]:
        return False
    row = db.get(con["article_id"])
    await _apply(con["article_id"], row, json.loads(row["result"]), con["pending"], _note(con))
    changed = con["pending"]["rating"] != json.loads(row["result"])["rating"]
    _decide(cid, "accepted", ("Tu fuente cambió la calificación. " if changed else "Tu fuente se agregó a la verificación. ")
            + "Te devolvimos el crédito y te dimos uno extra.")
    db.q("UPDATE contributions SET pending=NULL WHERE id=%s", cid)
    _thank(con)
    return True


def reject(cid: str) -> bool:
    ok = db.q1("SELECT 1 FROM contributions WHERE id=%s AND status IN ('frozen','review')", cid)
    if ok:
        _decide(cid, "rejected", "El equipo editorial revisó tu aporte y no lo aplicó.")
        db.q("UPDATE contributions SET pending=NULL WHERE id=%s", cid)
    return bool(ok)
