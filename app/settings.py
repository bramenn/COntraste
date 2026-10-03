import os
import re
from pathlib import Path
from urllib.parse import urlsplit

APP_DIR = Path(__file__).parent
ROOT = APP_DIR.parent


def env(name: str, default: str = "") -> str:
    """Read an env var, tolerating inline .env comments ("VALUE   # note" or "   # note").
    Depending on the compose version, the comment arrives with or without the leading spaces."""
    return re.split(r"(?:^|\s)#", os.getenv(name) or "")[0].strip() or default


DATA_DIR = Path(env("DATA_DIR", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

OPENROUTER_API_KEY = env("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = env("OPENROUTER_MODEL", "")
OPENROUTER_VISION_MODEL = env("OPENROUTER_VISION_MODEL") or OPENROUTER_MODEL
OPENROUTER_FAST_MODEL = env("OPENROUTER_FAST_MODEL") or OPENROUTER_MODEL  # used to read sources
BRAVE_API_KEY = env("BRAVE_API_KEY", "")
GOOGLE_FACTCHECK_API_KEY = env("GOOGLE_FACTCHECK_API_KEY", "")
WHISPER_MODEL = env("WHISPER_MODEL", "small")
MAX_VIDEO_MINUTES = float(env("MAX_VIDEO_MINUTES", "10"))
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")
# AGPL-3.0: whoever runs Contraste as a service offers its source to the people using it. Point this at yours.
SOURCE_URL = env("SOURCE_URL", "https://github.com/bramenn/COntraste")
DEMO_MODE = env("DEMO_MODE", "false").lower() in ("1", "true", "yes", "si", "sí")

MAX_TEXT_CHARS = 3000
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_CLAIMS = 5
MAX_SOURCES_PER_CLAIM = 8
PUBLISH_MIN_SOURCES = int(env("PUBLISH_MIN_SOURCES", "3"))
ADMIN_PASSWORD = env("ADMIN_PASSWORD", "")
DATABASE_URL = env("DATABASE_URL", "postgresql://contraste:contraste@localhost:5432/contraste")
MAX_CONCURRENT_CHECKS = int(env("MAX_CONCURRENT_CHECKS", "10"))  # per replica

# Accounts and credits (Contraste is free: credits only limit use)
SMTP_HOST = env("SMTP_HOST")
SMTP_PORT = int(env("SMTP_PORT", "587"))
SMTP_USER = env("SMTP_USER")
SMTP_PASSWORD = env("SMTP_PASSWORD")
MAIL_FROM = env("MAIL_FROM", "COntraste <no-reply@localhost>")
GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET")
TURNSTILE_SITE_KEY = env("TURNSTILE_SITE_KEY")
TURNSTILE_SECRET_KEY = env("TURNSTILE_SECRET_KEY")
# Hostnames siteverify must report back. Prod deployments must list only real hosts (never localhost);
# if unset, it is derived from PUBLIC_BASE_URL, which keeps local development working out of the box.
TURNSTILE_HOSTNAMES = [h for h in (x.strip() for x in env("TURNSTILE_HOSTNAMES").split(",")) if h] or [
    (urlsplit(PUBLIC_BASE_URL).hostname or "")
]
DAILY_CHECKS = int(env("DAILY_CHECKS", "5"))  # per account per day; editors change it in /admin
AUTO_CHECKS_PER_DAY = int(env("AUTO_CHECKS_PER_DAY", "5"))  # checks COntraste picks from the day's news; 0 = off
FREE_MONTHLY_CREDITS = int(env("FREE_MONTHLY_CREDITS", "5"))  # starting value; editors change it in /admin
# Entry gate (app/gate.py): fast classifier for relevance to Colombia and prompt injection, before research.
GATE = env("GATE", "true").lower() in ("1", "true", "yes")
# Model for the gate on OpenRouter's Decisions API (Jev). Empty: the fast chat model does it.
GATE_MODEL = env("GATE_MODEL", "typesafe/jev-1.13")
# Shared secret Cloudflare adds to every request (Transform Rule, header X-Contraste-Origin). When set, only
# requests carrying it reach the site (the nodes also answer directly, bypassing Cloudflare), and only then is
# CF-Connecting-IP trusted as the visitor's IP. Unset (local development): the peer address is used.
ORIGIN_SECRET = env("ORIGIN_SECRET")
# How long a signed-in session lasts. It also slides: using the site pushes the expiry back.
SESSION_DAYS = int(env("SESSION_DAYS", "180"))
DAILY_SPEND_LIMIT_USD = float(env("DAILY_SPEND_LIMIT_USD", "5"))
# Optional list of IPs allowed into /admin (comma separated). Empty = any IP (password still required).
ADMIN_ALLOWED_IPS = {ip.strip() for ip in env("ADMIN_ALLOWED_IPS", "").split(",") if ip.strip()}
# Deep research: follow documents and sources cited by the useful pages.
DEEP_DEPTH = int(env("DEEP_DEPTH", "2"))            # hops away from the search results
DEEP_MAX_PAGES = int(env("DEEP_MAX_PAGES", "8"))    # extra pages per check, all hops together
# Market indicators in the masthead (dollar, coffee, gold, oil), refreshed from official sources.
MARKETS = env("MARKETS", "true").lower() in ("1", "true", "yes")
# Render the front page's card images in the background after start-up (the card cache starts empty).
WARM_CARDS = env("WARM_CARDS", "true").lower() in ("1", "true", "yes")
