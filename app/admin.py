"""Editor panel: a single password from .env (bcrypt), no user accounts."""
import copy
import json
import secrets

import bcrypt
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import cards, db, settings
from .rules import RATINGS

router = APIRouter()
_HASH = bcrypt.hashpw(settings.ADMIN_PASSWORD.encode()[:72], bcrypt.gensalt()) if settings.ADMIN_PASSWORD else None
COOKIE = "contraste_admin"
TTL = 8 * 3600


def _session(req: Request) -> str | None:
    """CSRF token of a valid session. Sessions live in Postgres so any replica can check them."""
    token = req.cookies.get(COOKIE, "")
    return db.session_csrf(token) if token else None


async def _form(req: Request, csrf: str):
    form = await req.form()
    return form if secrets.compare_digest(str(form.get("csrf", "")), csrf) else None


def _page(name: str, **ctx) -> HTMLResponse:
    return HTMLResponse(cards.env.get_template(name).render(noindex=True, **ctx),
                        headers={"Cache-Control": "no-store"})


@router.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, status: str = Query("", alias="estado")):
    csrf = _session(request)
    if not csrf:
        return _page("admin.html", view="login", configured=bool(_HASH))
    where = "WHERE status=%s" if status in ("listed", "unlisted", "removed") else ""
    rows = db.q(f"SELECT * FROM articles {where} ORDER BY created_at DESC LIMIT 300", *([status] if where else []))
    replies = db.q("""SELECT r.article_id, a.title, COUNT(*) AS n FROM replies r JOIN articles a ON a.id=r.article_id
                      WHERE r.status='new' GROUP BY 1, 2 ORDER BY MIN(r.created_at)""")
    return _page("admin.html", view="list", rows=rows, csrf=csrf, status_filter=status, spend=spend_status(), replies=replies)


def spend_status() -> dict:
    today, cap = db.spend_today(), settings.DAILY_SPEND_LIMIT_USD
    pct = round(100 * today / cap) if cap else 0
    return {"today": today, "cap": cap, "pct": pct, "warn": cap and pct >= 80, "paused": cap and pct >= 100}


@router.get("/admin/negocio", response_class=HTMLResponse)
def business(request: Request, msg: str = ""):
    """Model spend, limits, accounts and contributions waiting for an editor."""
    csrf = _session(request)
    if not csrf:
        return RedirectResponse("/admin", status_code=303)
    avg = db.q1("""SELECT COUNT(*) AS n, COALESCE(AVG(cost_usd), 0) AS usd FROM jobs
                   WHERE kind='check' AND status='done' AND cost_usd > 0 AND created_at > now() - interval '30 days'""")
    frozen = db.q("""SELECT c.id, c.status, c.urls, c.note, c.claim_index, c.outcome, a.title, a.rating AS old_rating,
                            c.pending->>'rating' AS new_rating, o.title AS origin_title
                     FROM contributions c JOIN articles a ON a.id=c.article_id LEFT JOIN articles o ON o.id=c.origin
                     WHERE c.status IN ('frozen', 'review') ORDER BY c.created_at""")
    from . import auto, credits, survey
    return _page("admin.html", view="business", csrf=csrf, spend=spend_status(), avg=avg, frozen=frozen, msg=msg,
                 free_n=credits.free_monthly(), daily_n=credits.daily_limit(), survey_reward=survey.reward_amount(),
                 auto_n=auto.per_day())


@router.get("/admin/encuesta", response_class=HTMLResponse)
def survey_results(request: Request):
    from . import survey
    if not _session(request):
        return RedirectResponse("/admin", status_code=303)
    return _page("admin.html", view="survey", s=survey.summary())


@router.get("/admin/encuesta.csv")
def survey_csv(request: Request):
    from fastapi.responses import Response
    from . import survey
    if not _session(request):
        return RedirectResponse("/admin", status_code=303)
    return Response(survey.to_csv(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="encuesta-contraste.csv"', "Cache-Control": "no-store"})


@router.post("/admin/negocio/{action}")
async def business_act(request: Request, action: str):
    from . import contributions, credits
    csrf = _session(request)
    form = await _form(request, csrf) if csrf else None
    if not form:
        return RedirectResponse("/admin", status_code=303)
    msg = ""
    if action == "settings":
        try:
            free_n, daily_n, reward_n, auto_n = (int(form.get(k, -1)) for k in
                                                 ("free_monthly_credits", "daily_checks", "survey_reward", "auto_checks"))
        except ValueError:
            free_n = daily_n = reward_n = auto_n = -1
        if not (0 <= free_n <= 100 and 1 <= daily_n <= 100 and 0 <= reward_n <= 20 and 0 <= auto_n <= 13):
            msg = ("Usa entre 0 y 100 verificaciones al mes, entre 1 y 100 por día, entre 0 y 20 de premio por la encuesta "
                   "y entre 0 y 13 verificaciones del día.")
        else:
            db.set_setting("free_monthly_credits", free_n)
            db.set_setting("daily_checks", daily_n)
            db.set_setting("survey_reward", reward_n)
            db.set_setting("auto_checks", auto_n)
            msg = (f"Guardado: {free_n} verificaciones al mes, {daily_n} por día, {reward_n} de premio por la encuesta, "
                   f"{auto_n} verificaciones del día elegidas por COntraste.")
    elif action in ("adjust", "block", "unblock"):
        user = db.q1("SELECT * FROM users WHERE email=%s", str(form.get("email", "")).strip().lower())
        if not user:
            msg = "No existe una cuenta con ese correo."
        elif action == "adjust":
            try:
                amount = int(form.get("amount", 0))
            except ValueError:
                amount = 0
            note = str(form.get("note", "")).strip()[:200]
            if amount and note:
                credits.admin_adjust(user["id"], amount, note)
                msg = f"Ajuste de {amount:+d} créditos aplicado."
            else:
                msg = "El ajuste necesita una cantidad distinta de cero y un motivo."
        else:
            db.q("UPDATE users SET blocked=%s WHERE id=%s", action == "block", user["id"])
            if action == "block":
                db.q("DELETE FROM user_sessions WHERE user_id=%s", user["id"])
            msg = "Cuenta bloqueada." if action == "block" else "Cuenta desbloqueada."
    elif action in ("process", "approve", "reject"):
        cid = str(form.get("id", ""))
        if action == "process" and db.q1("SELECT 1 FROM contributions WHERE id=%s AND status='frozen'", cid):
            db.q("UPDATE contributions SET status='queued' WHERE id=%s", cid)
            contributions.enqueue(cid)
            msg = "Aporte enviado a revisión automática."
        elif action == "approve":
            msg = "Aporte aplicado al artículo." if await contributions.approve(cid) else "Ese aporte ya no está pendiente."
        elif action == "reject":
            msg = "Aporte rechazado." if contributions.reject(cid) else "Ese aporte ya no está pendiente."
    from urllib.parse import quote
    return RedirectResponse(f"/admin/negocio?msg={quote(msg)}", status_code=303)


@router.post("/admin/login")
async def login(request: Request):
    from .main import client_ip, limited
    if limited("login:" + client_ip(request), 5, 900):
        return _page("admin.html", view="login", configured=bool(_HASH), error="Demasiados intentos. Espera 15 minutos.")
    pw = str((await request.form()).get("password", ""))
    if not _HASH or not bcrypt.checkpw(pw.encode()[:72], _HASH):
        return _page("admin.html", view="login", configured=bool(_HASH), error="Contraseña incorrecta.")
    token, _ = db.session_create(TTL)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(COOKIE, token, max_age=TTL, httponly=True, samesite="strict", path="/",
                    secure=settings.PUBLIC_BASE_URL.startswith("https"))
    return resp


@router.post("/admin/logout")
async def logout(request: Request):
    db.session_delete(request.cookies.get(COOKIE, ""))
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/admin/a/{aid}", response_class=HTMLResponse)
def edit(request: Request, aid: str):
    csrf = _session(request)
    row = db.get(aid)
    if not csrf or not row:
        return RedirectResponse("/admin", status_code=303)
    return _page("admin.html", view="edit", row=row, r=json.loads(row["result"]), csrf=csrf, changes=db.changes(aid),
                 replies=db.q("SELECT * FROM replies WHERE article_id=%s ORDER BY created_at DESC", aid))


@router.post("/admin/a/{aid}/{action}")
async def act(request: Request, aid: str, action: str):
    from .main import reinvestigate, spawn
    csrf = _session(request)
    row = db.get(aid)
    form = await _form(request, csrf) if csrf else None
    if not form or not row:
        return RedirectResponse("/admin", status_code=303)
    r = json.loads(row["result"])
    if action == "status" and form.get("status") in ("listed", "unlisted", "removed"):
        reason = {"listed": None, "unlisted": "Decisión editorial.", "removed": "Retirado por el equipo editorial."}
        db.q("UPDATE articles SET status=%s, unlisted_reason=%s WHERE id=%s", form["status"], reason[form["status"]], aid)
    elif action == "correct" and str(form.get("note", "")).strip():
        new = copy.deepcopy(r)
        if form.get("rating") in RATINGS:
            new["rating"] = form["rating"]
        new["headline"] = str(form.get("headline", new["headline"]))[:300]
        for i, c in enumerate(new["claims"]):
            if form.get(f"claim_rating_{i}") in RATINGS:
                c["rating"] = form[f"claim_rating_{i}"]
            c["explanation"] = str(form.get(f"claim_explanation_{i}", c["explanation"]))[:1000]
        db.update_article(aid, new, change=("correccion", str(form["note"]).strip()[:500]), old_rating=r["rating"])
        if new["rating"] != r["rating"]:
            await cards.restamp(aid, new["rating"])
    elif action == "reinvestigate":
        spawn(reinvestigate(aid))
    elif action == "reviewed":
        db.q("UPDATE articles SET human_reviewed_at=%s WHERE id=%s", db.iso(), aid)
        db.q("INSERT INTO changes VALUES(%s,%s,'revision',%s,%s,%s)", aid, db.iso(),
             "Revisada por el equipo editorial.", r["rating"], r["rating"])
    elif action == "reply_done":
        db.q("UPDATE replies SET status='done', email='' WHERE id=%s AND article_id=%s", int(form.get("id", 0) or 0), aid)
    return RedirectResponse(f"/admin/a/{aid}", status_code=303)
