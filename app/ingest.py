"""Input ingestion: text, web articles, video (yt-dlp + ffmpeg + faster-whisper) and images (vision model)."""
import asyncio
import base64
import hashlib
import io
import logging
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from PIL import Image

from . import llm, settings
from .fetch import FetchError, extract, safe_get
from .rules import host_of

log = logging.getLogger("contraste.ingest")


class UserError(Exception):
    """Error whose message is shown to the user as is, in plain language. The verification is refunded."""


class Charged(UserError):
    """A rejection after costly analysis already ran (reading an image, the main model): the verification stays
    spent, and the message must say why."""


VIDEO_HOSTS = {"youtube.com": "YouTube", "youtu.be": "YouTube", "tiktok.com": "TikTok", "instagram.com": "Instagram",
               "facebook.com": "Facebook", "fb.watch": "Facebook", "x.com": "X", "twitter.com": "X"}
SUGGEST = "Toma una captura de pantalla y súbela, o copia y pega el texto de la publicación."


def platform(url: str) -> str | None:
    h = host_of(url)
    return next((name for d, name in VIDEO_HOSTS.items() if h == d or h.endswith("." + d)), None)


# --- Images -----------------------------------------------------------------------------------

_MAGIC = {b"\xff\xd8\xff": "JPEG", b"\x89PNG\r\n\x1a\n": "PNG", b"GIF87a": "GIF", b"GIF89a": "GIF"}
Image.MAX_IMAGE_PIXELS = 40_000_000


def load_image(raw: bytes) -> Image.Image:
    """Check the real type by magic bytes and the size, then re-encode (drops metadata and hidden payloads)."""
    if len(raw) > settings.MAX_IMAGE_BYTES:
        raise UserError("La imagen pesa más de 10 MB. Prueba con una captura más liviana.")
    kind = next((k for m, k in _MAGIC.items() if raw.startswith(m)), None)
    if not kind and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        kind = "WEBP"
    if not kind:
        raise UserError("El archivo no parece una imagen válida. Usa JPG, PNG, WEBP o GIF.")
    try:
        img = Image.open(io.BytesIO(raw))
        if img.format != kind:
            raise ValueError("formato no coincide")
        img.load()
    except Exception:
        raise UserError("No pudimos abrir la imagen. Prueba con otra captura.")
    img = img.convert("RGB")
    img.thumbnail((2048, 2048))
    clean = Image.new("RGB", img.size)
    clean.paste(img)
    return clean


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


async def read_image(img: Image.Image, emit) -> dict:
    await emit("Leyendo la imagen", 12)
    b64 = base64.b64encode(png_bytes(img)).decode()
    r = await llm.ask(llm.IMAGE_TASK, "(imagen adjunta)", llm.ImageReading, image_b64=b64, vision=True)
    text = f"Texto en la imagen: {r.text}\nDescripción: {r.description}\nTipo: {r.kind}"
    if r.satire_signals:
        text += f"\nSeñales de montaje o sátira: {r.satire_signals}"
    return {"kind": "image", "text": text, "title": "Imagen enviada", "thumb": img,
            "seen": r.description, "seen_text": r.text}


# --- Articles ---------------------------------------------------------------------------------

def fingerprint(text: str) -> list[str]:
    """Short hashes of the page's sentences. If a checked page later gains sentences, it was edited and
    the old verdict must not be handed out for it (someone could get a correct page checked, then edit
    it into something false and keep showing our card)."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return sorted({hashlib.sha1(re.sub(r"\W+", "", s.casefold()).encode()).hexdigest()[:10]
                   for s in sentences if len(s) >= 40})[:600]


async def read_article(url: str, emit) -> dict:
    await emit("Leyendo el enlace", 8)
    try:
        final, body, ctype = await safe_get(url)
    except FetchError as e:
        msg = str(e)
        if msg.startswith("paywall"):
            raise UserError("Esa página no nos deja leer su contenido (puede tener muro de pago). Pega el texto que quieres verificar.")
        raise UserError(msg)
    if "html" not in ctype and "text" not in ctype:
        raise UserError("Ese enlace no lleva a una página de texto. " + SUGGEST)
    page = extract(body, final)
    if len(page["text"]) < 200:
        raise UserError("No pudimos leer el texto de esa página. " + SUGGEST)
    return {"kind": "article", "text": f"Título: {page['title']}\n\n{page['text'][:20000]}",
            "title": page["title"] or host_of(final), "raw_html": page["html"], "fingerprint": fingerprint(page["text"])}


# --- Video ----------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _whisper():
    from faster_whisper import WhisperModel
    return WhisperModel(settings.WHISPER_MODEL, device="cpu", compute_type="int8")


def _friendly(plat: str, err: str) -> str:
    e = err.lower()
    if plat in ("Instagram", "Facebook") or "login" in e or "cookies" in e:
        return f"{plat} no permite descargar esta publicación sin iniciar sesión. {SUGGEST}"
    if "private" in e or "privad" in e:
        return f"Esta publicación de {plat} es privada. {SUGGEST}"
    if "unavailable" in e or "not available" in e or "removed" in e:
        return f"Esta publicación de {plat} ya no está disponible. {SUGGEST}"
    return f"No pudimos descargar el contenido de {plat}. {SUGGEST}"


def _download_audio(url: str, workdir: Path) -> tuple[dict, Path]:
    import yt_dlp
    base = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20}
    with yt_dlp.YoutubeDL(base) as y:
        info = y.extract_info(url, download=False)
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        # A live stream has no duration and its download never ends.
        raise UserError("Es una transmisión en vivo y no se puede verificar mientras está al aire. "
                        "Cuando termine, envía el enlace de la grabación, o pega como texto lo que se dijo.")
    dur = info.get("duration") or 0
    if dur > settings.MAX_VIDEO_MINUTES * 60:
        raise UserError(f"El video dura {round(dur / 60)} minutos y el máximo es {settings.MAX_VIDEO_MINUTES:g}. "
                        "Pega como texto la parte que quieres verificar.")
    if not info.get("formats") and not info.get("url"):
        return info, None
    opts = base | {"format": "bestaudio/best", "outtmpl": str(workdir / "media.%(ext)s"), "max_filesize": 300 * 1024 * 1024}
    with yt_dlp.YoutubeDL(opts) as y:
        y.download([info.get("webpage_url") or url])
    media = next(workdir.glob("media.*"))
    wav = workdir / "audio.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
                    "-t", str(int(settings.MAX_VIDEO_MINUTES * 60)), str(wav)], check=True, timeout=600)
    return info, wav


async def _x_oembed(url: str) -> str | None:
    """Text of an X post without video, through the public oEmbed endpoint."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://publish.twitter.com/oembed", params={"url": url, "omit_script": "1"},
                            follow_redirects=True)  # hoy responde 301 hacia publish.x.com
        if r.status_code == 200:
            html = r.json().get("html", "")
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.split("&mdash;")[0])).strip() or None
    except Exception:
        return None
    return None


def _post_text(info: dict, plat: str, fallback: str | None = None) -> str:
    """Author and full text of the post. The title is only kept for YouTube: on X, TikTok and Instagram
    it is just a truncated copy of the same text."""
    desc = (info.get("description") or "").strip() or (fallback or "")
    who, handle = info.get("uploader") or info.get("channel") or "", info.get("uploader_id") or ""
    lines = [f"Autor: {who}" + (f" (@{handle})" if handle and plat != "YouTube" else "")] if who else []
    if plat == "YouTube" and info.get("title"):
        lines.append(f"Título: {info['title']}")
    if desc or info.get("title"):
        lines.append(f"Texto de la publicación:\n{(desc or info['title'])[:8000]}")
    return "\n".join(lines)


async def read_video(url: str, emit, loop) -> dict:
    import yt_dlp
    plat = platform(url)
    await emit(f"Obteniendo el contenido de {plat}", 5)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            info, wav = await asyncio.to_thread(_download_audio, url, Path(tmp))
        except UserError:
            raise
        except (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError, StopIteration, subprocess.SubprocessError) as e:
            if plat == "X" and (text := await _x_oembed(url)):
                return {"kind": "post", "text": text, "title": "Publicación en X"}
            log.info("yt-dlp failed for %s: %s", url, e)
            raise UserError(_friendly(plat, str(e)))
        fallback = await _x_oembed(url) if plat == "X" and len((info.get("description") or "").strip()) < 20 else None
        header = _post_text(info, plat, fallback)
        if not wav:
            return {"kind": "post", "text": header, "title": info.get("title") or f"Publicación en {plat}",
                    "thumb_url": info.get("thumbnail")}
        dur = info.get("duration") or 0
        await emit("Preparando el transcriptor", 8)
        model = await asyncio.to_thread(_whisper)
        await emit("Transcribiendo el video", 10)

        def run():
            segs, meta = model.transcribe(str(wav), language="es", vad_filter=True)
            total = dur or meta.duration or 1
            out = []
            for s in segs:
                out.append(s.text.strip())
                pct = min(1, s.end / total)
                asyncio.run_coroutine_threadsafe(
                    emit(f"Transcribiendo el video · {int(s.end // 60)}:{int(s.end % 60):02d} de "
                         f"{int(total // 60)}:{int(total % 60):02d}", 10 + int(pct * 20), replace=True), loop)
            return " ".join(out)

        transcript = await asyncio.to_thread(run)
    if not transcript.strip():
        # Video without speech (music, short clip): the checkable part is usually the post text.
        if "Texto de la publicación" in header:
            return {"kind": "post", "text": header + "\n\n(El video no tiene voz.)", "thumb_url": info.get("thumbnail"),
                    "title": info.get("title") or f"Publicación en {plat}"}
        raise UserError("No encontramos voz en el video. Si el mensaje está escrito en pantalla, sube una captura.")
    return {"kind": "video", "text": f"{header}\n\nTranscripción del video:\n{transcript}", "transcript": transcript,
            "title": info.get("title") or "Video",
            "thumb_url": info.get("thumbnail"),
            "content_hash": "tx:" + hashlib.sha256(re.sub(r"\W+", "", transcript.casefold()).encode()).hexdigest()}


async def fetch_thumb(url: str | None) -> Image.Image | None:
    if not url:
        return None
    try:
        _, body, _ = await safe_get(url, max_bytes=5 * 1024 * 1024)
        return load_image(body)
    except Exception:
        return None
