"""Accounts, only to pay for and use checks. No passwords: a one-time email link or Google sign-in.
Nothing about a user is ever shown on an article."""
import asyncio
import hashlib
import logging
import re
import secrets
import smtplib
from contextvars import ContextVar
from email.message import EmailMessage
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import credits, db, settings

log = logging.getLogger("contraste.accounts")
router = APIRouter()

COOKIE = "contraste_session"
SESSION_TTL = settings.SESSION_DAYS * 24 * 3600
LINK_TTL = 15 * 60
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,}$")
DISPOSABLE = {l.strip().lower() for l in (settings.ROOT / "disposable_domains.txt").read_text().splitlines()
              if l.strip() and not l.startswith("#")}

CURRENT_USER: ContextVar[dict | None] = ContextVar("current_user", default=None)
CURRENT_CSRF: ContextVar[str | None] = ContextVar("current_csrf", default=None)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# Providers that deliver "name+anything@" to "name@". Gmail also ignores dots in the name.
PLUS_ALIASES = {"gmail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com",
                "protonmail.com", "proton.me", "pm.me", "fastmail.com", "zoho.com", "yandex.com", "gmx.com"}


def normalize_email(email: str) -> str:
    """One mailbox, one account: "Juan.Perez+x@googlemail.com" and "juanperez@gmail.com" are the same person,
    so they cannot collect free checks twice."""
    local, _, domain = email.strip().lower().rpartition("@")
    domain = "gmail.com" if domain == "googlemail.com" else domain
    if domain in PLUS_ALIASES:
        local = local.split("+", 1)[0]
    if domain == "gmail.com":
        local = local.replace(".", "")
    return f"{local}@{domain}"


def is_disposable(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower()
    return any(domain == d or domain.endswith("." + d) for d in DISPOSABLE)


def ip_limited(req: Request, key: str, n: int, window_s: int) -> bool:
    """Per-IP limit; only a hash of the IP salted with the daily salt is stored."""
    ip = req.client.host if req.client else "0.0.0.0"
    return db.rate_limited(hashlib.sha256(f"{key}:{ip}|{db.daily_salt()}".encode()).hexdigest(), n, window_s)


# --- Sessions ---------------------------------------------------------------------------------

def load_session(req: Request) -> tuple[dict | None, str | None, bool]:
    """(user, csrf, renewed). renewed: the expiry was pushed back, so the cookie must be sent again."""
    token = req.cookies.get(COOKIE)
    if not token:
        return None, None, False
    row = db.q1("""SELECT u.*, s.csrf FROM user_sessions s JOIN users u ON u.id = s.user_id
                   WHERE s.token_hash=%s AND s.expires_at > now()""", _hash(token))
    if not row:
        return None, None, False
    # Sliding expiry: only past the halfway point, so an active session is never logged out and the
    # write is rare (one no-op UPDATE attempt per request at most).
    renewed = db.q1("""UPDATE user_sessions SET expires_at = now() + make_interval(secs => %s)
                       WHERE token_hash = %s AND expires_at < now() + make_interval(secs => %s) RETURNING 1""",
                    SESSION_TTL, _hash(token), SESSION_TTL // 2) is not None
    csrf = row.pop("csrf")
    return row, csrf, renewed


def set_session_cookie(resp, token: str):
    resp.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax",
                    secure=settings.PUBLIC_BASE_URL.startswith("https"), path="/")


def start_session(resp, user_id: str):
    token = secrets.token_urlsafe(32)
    db.q("INSERT INTO user_sessions VALUES(%s,%s,%s, now() + make_interval(secs => %s))",
         _hash(token), user_id, secrets.token_urlsafe(16), SESSION_TTL)
    set_session_cookie(resp, token)


def csrf_ok(req: Request, token: str | None) -> bool:
    expected = CURRENT_CSRF.get()
    return bool(expected and token and secrets.compare_digest(token, expected))


def get_or_create_user(req: Request, email: str, consent: bool) -> tuple[dict | None, str | None]:
    """Existing users sign in; new ones need consent and pass the sign-up limit (3 per IP per day)."""
    user = db.q1("SELECT * FROM users WHERE email=%s", email)
    if user:
        if not user["email_verified"]:
            db.q("UPDATE users SET email_verified=TRUE WHERE id=%s", user["id"])
            user["email_verified"] = True
        return user, None
    if not consent:
        return None, "Para crear la cuenta necesitas aceptar la política de tratamiento de datos."
    if ip_limited(req, "signup", 3, 86400):
        return None, "Se crearon demasiadas cuentas desde esta conexión hoy. Intenta mañana."
    uid = "u" + secrets.token_hex(8)
    db.q("""INSERT INTO users(id, email, email_verified, consent_at, disposable) VALUES(%s,%s,TRUE, now(), %s)
            ON CONFLICT (email) DO NOTHING""", uid, email, is_disposable(email))
    return db.q1("SELECT * FROM users WHERE email=%s", email), None


def safe_next(nxt: str | None) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/"


# --- Captcha ----------------------------------------------------------------------------------

async def turnstile_ok(token: str | None, req: Request, action: str) -> bool:
    """Cloudflare Turnstile, canonical siteverify: the token must verify, carry the expected action and
    come from a hostname this deployment serves. Without a secret key (local development) it is skipped."""
    if not settings.TURNSTILE_SECRET_KEY:
        return True
    if not token or len(token) > 2048 or not settings.TURNSTILE_HOSTNAMES:
        return False
    from .main import client_ip
    remoteip = client_ip(req)
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://challenges.cloudflare.com/turnstile/v0/siteverify",
                             data={"secret": settings.TURNSTILE_SECRET_KEY, "response": token, "remoteip": remoteip})
        d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("turnstile siteverify error: %r remoteip=%r", e, remoteip)
        return False
    ok = bool(d.get("success")) and d.get("action") == action \
        and d.get("hostname") in settings.TURNSTILE_HOSTNAMES
    if not ok:
        log.warning("turnstile reject: action=%r hostname=%r errors=%s sent_action=%r remoteip=%r",
                    d.get("action"), d.get("hostname"), d.get("error-codes"), action, remoteip)
    return ok


# --- Email link -------------------------------------------------------------------------------

def _magic_html(link: str) -> str:
    """Sign-in email. Table layout and inline styles so it looks the same in every client."""
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Entra a COntraste</title></head>
<body style="margin:0;padding:0;background:#e9e2d0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#e9e2d0;padding:24px 12px;">
<tr><td align="center">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#faf6ec;border:1px solid #cdbfa0;">
    <tr><td style="padding:26px 32px 8px;text-align:center;font-family:Georgia,'Times New Roman',serif;">
      <div style="font-size:34px;line-height:1;color:#1c1a17;letter-spacing:.5px;">COntraste</div>
      <div style="height:6px;margin:12px auto 6px;width:150px;background:#f2c200;border-top:2px solid #10306e;border-bottom:2px solid #c8102e;"></div>
      <div style="font-style:italic;color:#6b6250;font-size:14px;">Verifica antes de compartir</div>
    </td></tr>
    <tr><td style="padding:18px 32px 0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#2a271f;">
      <p style="margin:0 0 14px;font-size:17px;line-height:1.55;">Hola. Pulsa el botón para entrar a tu cuenta de COntraste.</p>
    </td></tr>
    <tr><td align="center" style="padding:6px 32px 4px;">
      <a href="{link}" style="display:inline-block;background:#1c1a17;color:#faf6ec;text-decoration:none;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-weight:700;font-size:16px;padding:14px 30px;border-radius:2px;">Entrar en Contraste</a>
    </td></tr>
    <tr><td style="padding:16px 32px 0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#6b6250;font-size:14px;line-height:1.55;">
      <p style="margin:0 0 10px;">El enlace vence en 15 minutos y sirve una sola vez. Sin contraseñas.</p>
      <p style="margin:0 0 18px;word-break:break-all;color:#8a8069;font-size:12px;">Si el botón no funciona, copia y pega esta dirección:<br><a href="{link}" style="color:#8a8069;">{link}</a></p>
    </td></tr>
    <tr><td style="padding:14px 32px 22px;border-top:1px solid #e2d8bf;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#8a8069;font-size:12px;line-height:1.5;">
      Si no pediste este enlace, ignora este correo y no pasará nada.
    </td></tr>
  </table>
  <div style="max-width:520px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#8a8069;font-size:11px;padding:12px;text-align:center;">
    COntraste · Verificación de datos para Colombia
  </div>
</td></tr></table></body></html>"""


def _send_mail(to: str, subject: str, body: str, html: str | None = None):
    if not settings.SMTP_HOST:
        log.warning("SMTP not configured; sign-in link for %s: %s", to, body.split("\n")[3])
        return
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.MAIL_FROM, to, subject
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as s:
        s.starttls()
        if settings.SMTP_USER:
            s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        s.send_message(msg)


@router.post("/auth/magic")
async def magic_request(request: Request, email: str = Form(...), consent: str = Form(""), next: str = Form("/"),
                        captcha: str = Form("", alias="cf-turnstile-response")):
    email = email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        return JSONResponse({"error": "Escribe un correo válido."}, status_code=400)
    email = normalize_email(email)
    if not await turnstile_ok(captcha, request, "login"):
        return JSONResponse({"error": "No pudimos confirmar que no eres un robot. Intenta de nuevo."}, status_code=400)
    if ip_limited(request, "magic", 10, 3600) or \
            db.rate_limited(hashlib.sha256(f"magicto:{email}|{db.daily_salt()}".encode()).hexdigest(), 3, 3600):
        # Per address too: nobody can use us to flood someone else's inbox.
        return JSONResponse({"error": "Pediste muchos enlaces. Espera un rato."}, status_code=429)
    token = secrets.token_urlsafe(32)
    db.q("INSERT INTO login_tokens VALUES(%s,%s,%s,%s, now() + make_interval(secs => %s), NULL)",
         _hash(token), email, safe_next(next), consent in ("1", "on", "true"), LINK_TTL)
    link = f"{settings.PUBLIC_BASE_URL}/auth/magic/{token}"
    body = f"Hola.\n\nPara entrar a COntraste abre este enlace (vence en 15 minutos y sirve una sola vez):\n{link}\n\n" \
           "Si no lo pediste, ignora este correo."
    await asyncio.to_thread(_send_mail, email, "Tu enlace para entrar a COntraste", body, _magic_html(link))
    return {"ok": True}


@router.get("/auth/magic/{token}", response_class=HTMLResponse)
def magic_confirm(token: str):
    """Opening the link only shows who you are about to sign in as. Mail scanners that open links on their
    own do not use it up, and a link to someone else's account is visible before it is used."""
    row = db.q1("SELECT email FROM login_tokens WHERE token_hash=%s AND used_at IS NULL AND expires_at > now()", _hash(token))
    if not row:
        return _message("Este enlace ya se usó o venció. Pide uno nuevo desde «Entrar».", 400)
    return _page("entrar_confirmar.html", email=row["email"], token=token, noindex=True)


@router.post("/auth/magic/{token}")
def magic_consume(request: Request, token: str):
    row = db.q1("""UPDATE login_tokens SET used_at=now()
                   WHERE token_hash=%s AND used_at IS NULL AND expires_at > now() RETURNING *""", _hash(token))
    if not row:
        return _message("Este enlace ya se usó o venció. Pide uno nuevo desde «Entrar».", 400)
    user, err = get_or_create_user(request, row["email"], row["consent"])
    if err:
        return _message(err, 400)
    resp = RedirectResponse(row["next"] or "/", status_code=303)
    start_session(resp, user["id"])
    return resp


# --- Google -----------------------------------------------------------------------------------

@router.get("/auth/google")
def google_start(next: str = "/", consent: str = ""):
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        return _message("El inicio con Google no está configurado.", 404)
    state = secrets.token_urlsafe(24)
    params = {"client_id": settings.GOOGLE_OAUTH_CLIENT_ID, "response_type": "code", "scope": "openid email",
              "redirect_uri": f"{settings.PUBLIC_BASE_URL}/auth/google/callback", "state": state,
              "prompt": "select_account"}
    resp = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params), status_code=303)
    resp.set_cookie("contraste_oauth", f"{state}|{int(consent == '1')}|{safe_next(next)}", max_age=600, httponly=True,
                    samesite="lax", secure=settings.PUBLIC_BASE_URL.startswith("https"), path="/auth/google")
    return resp


@router.get("/auth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = ""):
    saved = (request.cookies.get("contraste_oauth") or "").split("|", 2)
    if len(saved) != 3 or not code or not secrets.compare_digest(saved[0], state):
        return _message("No pudimos completar el inicio con Google. Intenta de nuevo.", 400)
    async with httpx.AsyncClient(timeout=15) as c:
        tok = await c.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": f"{settings.PUBLIC_BASE_URL}/auth/google/callback", "grant_type": "authorization_code"})
        if tok.status_code != 200:
            return _message("Google no confirmó el inicio de sesión.", 400)
        info = (await c.get("https://openidconnect.googleapis.com/v1/userinfo",
                            headers={"Authorization": f"Bearer {tok.json()['access_token']}"})).json()
    if not info.get("email") or not info.get("email_verified"):
        return _message("Tu cuenta de Google no tiene un correo verificado.", 400)
    user, err = get_or_create_user(request, normalize_email(info["email"]), saved[1] == "1")
    if err:
        return _message(err, 400)
    resp = RedirectResponse(saved[2], status_code=303)
    resp.delete_cookie("contraste_oauth", path="/auth/google")
    start_session(resp, user["id"])
    return resp


# --- Pages ------------------------------------------------------------------------------------

def _page(name: str, status: int = 200, **ctx) -> HTMLResponse:
    from .main import page
    return page(name, status=status, **ctx)


def _message(text: str, status: int = 200) -> HTMLResponse:
    return _page("error.html", status=status, message=text)


@router.get("/entrar", response_class=HTMLResponse)
def login_page(next: str = "/"):
    if CURRENT_USER.get():
        return RedirectResponse(safe_next(next), status_code=303)
    return _page("entrar.html", next=safe_next(next), noindex=True)


@router.post("/salir")
async def logout(request: Request, csrf: str = Form("")):
    if csrf_ok(request, csrf):
        db.q("DELETE FROM user_sessions WHERE token_hash=%s", _hash(request.cookies.get(COOKIE, "")))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/cuenta", response_class=HTMLResponse)
def account(request: Request, sin_creditos: str = ""):
    user = CURRENT_USER.get()
    if not user:
        return RedirectResponse("/entrar?next=/cuenta", status_code=303)
    checks = db.q("""SELECT j.id, j.status, j.created_at, j.error, a.id AS aid, a.title, a.rating, a.slug,
                            a.created_at AS a_created
                     FROM jobs j LEFT JOIN articles a ON a.id = j.article_id
                     WHERE j.user_id=%s AND j.kind='check' ORDER BY j.created_at DESC LIMIT 50""", user["id"])
    contribs = db.q("""SELECT c.*, a.title, a.slug, a.created_at AS a_created FROM contributions c
                       JOIN articles a ON a.id = c.article_id WHERE c.user_id=%s ORDER BY c.created_at DESC LIMIT 50""",
                    user["id"])
    return _page("cuenta.html", noindex=True, balance=credits.balances(user), ledger=credits.ledger(user["id"]),
                 no_credits=bool(sin_creditos), renews=credits.renews_on(),
                 checks=checks, contribs=contribs, used_today=credits.used_today(user))


@router.post("/cuenta/eliminar")
async def delete_account(request: Request, csrf: str = Form(""), confirm: str = Form("")):
    """Deletes the personal data (the email). Articles stay published: they never carried user data.
    Ledger rows keep only the random account id, which no longer points to anyone."""
    user = CURRENT_USER.get()
    if not user or not csrf_ok(request, csrf):
        return RedirectResponse("/entrar", status_code=303)
    if confirm.strip().lower() != "eliminar":
        return _message("Para eliminar la cuenta escribe «eliminar» en la casilla de confirmación.", 400)
    with db.pool.connection() as c, c.transaction():
        c.execute("DELETE FROM login_tokens WHERE email=%s", (user["email"],))
        c.execute("DELETE FROM user_sessions WHERE user_id=%s", (user["id"],))
        c.execute("UPDATE contributions SET note='' WHERE user_id=%s", (user["id"],))  # free text may identify them
        c.execute("UPDATE surveys SET user_id=NULL WHERE user_id=%s", (user["id"],))  # the answer stays, anonymous
        c.execute("DELETE FROM users WHERE id=%s", (user["id"],))
    resp = _message("Eliminamos tu cuenta y tu correo. Las verificaciones públicas siguen disponibles.")
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/privacidad", response_class=HTMLResponse)
def privacy():
    return _page("privacidad.html")
