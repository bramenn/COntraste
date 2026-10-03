"""Share cards and stamped thumbnails, rendered with headless Chromium (Playwright)."""
import asyncio
import base64
import io
import logging
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from PIL import Image, ImageFilter

from .rules import MARK, RATINGS
from . import db
from .settings import APP_DIR, PUBLIC_BASE_URL

log = logging.getLogger("contraste.cards")
# Rendered cards are a per-replica cache (they can always be rendered again); stamped thumbnails live in
# Postgres because they cannot be rebuilt without the original image.
CARD_DIR = Path(tempfile.gettempdir()) / "contraste-cards"
CARD_DIR.mkdir(exist_ok=True)

FORMATS = {"post": (1080, 1350), "story": (1080, 1920), "og": (1200, 630), "cover": (1200, 750)}  # cover: image used in article listings
COLORS = {"verdadero": "#1d6b3e", "matices": "#4b6614", "enganoso": "#a94700", "falso": "#b3261e",
          "sin_pruebas": "#50565e", "no_verificable": "#2f587d"}
_P = 'fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"'
ICONS = {
    "verdadero": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "matices": '<path d="M3.5 11.5l4 4 8.5-8.5"/><path d="M15 17.5h6"/>',
    "enganoso": '<path d="M12 3.8L21.2 19.6H2.8z"/><path d="M12 10v4.2"/><path d="M12 17.1v.2"/>',
    "falso": '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    "sin_pruebas": '<path d="M9.2 9.2a2.9 2.9 0 1 1 4.1 2.6c-.8.4-1.3 1-1.3 1.9v.8"/><path d="M12 17.6v.2"/>',
    "no_verificable": '<circle cx="12" cy="12" r="8.3"/><path d="M6.2 17.8L17.8 6.2"/>',
}
MARK_ICON = {"cierto": "verdadero", "falso": "falso", "no_probado": "sin_pruebas"}
MARK_LABEL = {"cierto": "Cierto", "falso": "Falso o impreciso", "no_probado": "No probado"}
MONTHS = "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split()


def icon(key: str, size: int = 24, label: str | None = None) -> Markup:
    aria = f'role="img" aria-label="{label}"' if label else 'aria-hidden="true"'
    return Markup(f'<svg class="icon" width="{size}" height="{size}" viewBox="0 0 24 24" {_P} {aria}>{ICONS[key]}</svg>')


COLOMBIA = timezone(timedelta(hours=-5))  # Colombia has no daylight saving time


DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def edition_date() -> str:
    """Dateline for the masthead: "Miércoles, 30 de septiembre de 2026" (Colombian time)."""
    d = datetime.now(COLOMBIA)
    return f"{DAYS[d.weekday()].capitalize()}, {d.day} de {MONTHS[d.month - 1]} de {d.year}"


def format_date(ts: str) -> str:
    d = datetime.fromisoformat(ts).astimezone(COLOMBIA)
    return f"{d.day} de {MONTHS[d.month - 1]} de {d.year}"


def usd(x: float) -> str:
    """US$0,0312 (Spanish decimal comma; small amounts keep four decimals)."""
    return "US$" + (f"{x:.4f}" if x < 1 else f"{x:.2f}").replace(".", ",")


def clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


env = Environment(loader=FileSystemLoader(APP_DIR / "templates"), autoescape=select_autoescape(["html", "xml"]))
env.filters["fromjson"] = __import__("json").loads
env.filters["cop"] = lambda n: f"{int(n):,}".replace(",", ".")
env.globals.update(edition_date=edition_date, icon=icon, usd=usd, RATINGS=RATINGS, COLORS=COLORS, format_date=format_date, clip=clip, MARK=MARK,
                   MARK_ICON=MARK_ICON, MARK_LABEL=MARK_LABEL)
_FONTS = {n: base64.b64encode((APP_DIR / "static/fonts" / f).read_bytes()).decode()
          for n, f in (("display", "space-grotesk-latin-wght-normal.woff2"), ("body", "public-sans-latin-wght-normal.woff2"),
                       ("mono", "jetbrains-mono-latin-wght-normal.woff2"))}


def card_context(result: dict, aid: str, created_at: str) -> dict:
    lines = [{"mark": MARK[c["rating"]], "text": clip(c.get("card_line") or c["short"], 80)} for c in result["claims"]][:3]
    for nv in result.get("not_verifiable", []):
        if len(lines) >= 2:
            break
        lines.append({"mark": "no_probado", "text": clip(nv["text"], 80)})
    names = list(dict.fromkeys(s["name"] for s in result.get("sources", [])))
    short = PUBLIC_BASE_URL.split("://", 1)[-1] + f"/v/{aid}"
    return {"rating": result["rating"], "circulating": clip(result["circulating"], 110), "lines": lines,
            "n_sources": len(result.get("sources", [])), "source_names": names[:4], "date": format_date(created_at),
            "short_url": short, "fonts": _FONTS}


# --- Navegador ------------------------------------------------------------------------------

_browser = None
_pw = None
_block = asyncio.Lock()


async def _get_browser():
    global _browser, _pw
    async with _block:
        if _browser is None or not _browser.is_connected():
            from playwright.async_api import async_playwright
            _pw = await async_playwright().start()
            _browser = await _pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
    return _browser


async def close():
    global _browser, _pw
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()
    _browser = _pw = None


async def screenshot(html: str, w: int, h: int, *, full: bool = False, jpeg: bool = False) -> bytes:
    browser = await _get_browser()
    page = await browser.new_page(viewport={"width": w, "height": h})
    try:
        # The template is self-contained, so every network request is blocked.
        await page.route("**/*", lambda r: r.abort() if not r.request.url.startswith(("data:", "about:")) else r.continue_())
        await page.set_content(html, wait_until="load")
        await page.wait_for_selector("body[data-ready]", timeout=10000)
        return await page.screenshot(type="jpeg" if jpeg else "png", quality=82 if jpeg else None, full_page=full)
    finally:
        await page.close()


CAUTION = "Verificación preliminar · no publicada en portada"
DESIGN = "co1"  # change it when card.html changes, so every replica renders the cards again


async def render_card(result: dict, aid: str, created_at: str, fmt: str, caution: str | None = None) -> bytes:
    w, h = FORMATS[fmt]
    html = env.get_template("card.html").render(fmt=fmt, w=w, h=h, caution=caution, **card_context(result, aid, created_at))
    return await screenshot(html, w, h)


def shareable(row) -> bool:
    """No card at all for checks about private people: a card with our logo next to an accusation
    against someone who is not a public figure would spread the accusation, whatever the rating."""
    import json
    return not json.loads(row["result"]).get("private_person")


def card_stamp(row) -> str:
    """Changes whenever what the card shows changes: the design, the article, or whether it is listed."""
    return f"{DESIGN}-" + row["updated_at"].replace(":", "") + ("" if row["status"] == "listed" else "-c")


def card_url(row, fmt: str, ext: str = "webp") -> str:
    """Versioned URL, so browsers can keep the image for a year and still never show a stale one."""
    import hashlib
    return f"/api/cards/{row['id']}/{fmt}.{ext}?v={hashlib.sha1(card_stamp(row).encode()).hexdigest()[:10]}"


env.globals["card_url"] = card_url


async def card_png(row, fmt: str) -> bytes:
    """PNG cached on disk; re-rendered whenever the article changes. Cards of checks that are not on the
    front page carry a visible band, so a thin check cannot pass for a finished one when shared."""
    import json
    stamp = card_stamp(row)
    path = CARD_DIR / f"{row['id']}-{fmt}-{stamp}.png"
    if not path.exists():
        for old in CARD_DIR.glob(f"{row['id']}-{fmt}-*.png"):
            old.unlink(missing_ok=True)
        path.write_bytes(await render_card(json.loads(row["result"]), row["id"], row["created_at"], fmt,
                                           None if row["status"] == "listed" else CAUTION))
    return path.read_bytes()


async def card_webp(row, fmt: str) -> bytes:
    """Light copy for showing the card on the site (a fraction of the PNG). The PNG stays for link previews,
    sharing and downloads, where it is the format every app accepts."""
    path = CARD_DIR / f"{row['id']}-{fmt}-{card_stamp(row)}.webp"
    if not path.exists():
        png = await card_png(row, fmt)

        def convert() -> bytes:
            buf = io.BytesIO()
            Image.open(io.BytesIO(png)).save(buf, "WEBP", quality=82, method=4)
            return buf.getvalue()
        for old in CARD_DIR.glob(f"{row['id']}-{fmt}-*.webp"):
            old.unlink(missing_ok=True)
        path.write_bytes(await asyncio.to_thread(convert))
    return path.read_bytes()


# --- Miniaturas selladas --------------------------------------------------------------------

def blur_faces(img: Image.Image) -> Image.Image:
    """Blur detected faces (OpenCV Haar cascades).
    Known limitation: Haar misses side profiles and very small faces, which is why the thumbnail is also kept small."""
    import cv2
    import numpy as np
    img = img.copy()
    gray = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2GRAY)
    faces = []
    for name in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml"):
        det = cv2.CascadeClassifier(cv2.data.haarcascades + name)
        faces += list(det.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(16, 16)))
    for x, y, w, h in faces:
        pad = int(max(w, h) * 0.3)
        box = (max(0, x - pad), max(0, y - pad), min(img.width, x + w + pad), min(img.height, y + h + pad))
        region = img.crop(box).resize((6, 6)).resize((box[2] - box[0], box[3] - box[1]), Image.Resampling.NEAREST)
        img.paste(region.filter(ImageFilter.GaussianBlur(4)), box)
    return img


async def stamped_thumb(img: Image.Image, rating: str, aid: str) -> str:
    """Save a small thumbnail with blurred faces and the rating stamp. Returns the file name."""
    img = img.copy()
    img.thumbnail((480, 480))
    img = blur_faces(img)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    db.media_put(f"{aid}-base.jpg", buf.getvalue())  # unstamped copy, so it can be re-stamped after a correction
    return await restamp(aid, rating)


async def restamp(aid: str, rating: str) -> str | None:
    base = db.media_get(f"{aid}-base.jpg")
    if not base:
        return None
    data = bytes(base["data"])
    w, h = Image.open(io.BytesIO(data)).size
    html = env.get_template("thumb.html").render(img=base64.b64encode(data).decode(), rating=rating,
                                                 w=w, h=h, fonts=_FONTS)
    db.media_put(f"{aid}.jpg", await screenshot(html, w, h, jpeg=True))
    return f"{aid}.jpg"
