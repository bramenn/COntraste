"""Safe page downloads (SSRF protection) and text extraction."""
import asyncio
import ipaddress
import re
import socket
import subprocess
import tempfile
import unicodedata
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura

from .settings import MAX_PAGE_BYTES


class FetchError(Exception):
    pass


UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36 Contraste/1.0"
MAX_REDIRECTS = 5
# The cluster's egress is slow: connecting to big news sites takes 6-7 s. Give it room so pages
# are actually read instead of dropped as "no se pudo leer".
TIMEOUT = httpx.Timeout(30.0, connect=15.0)
_metadata_hosts = {"metadata.google.internal", "metadata", "instance-data"}


def _ip_ok(ip: str) -> bool:
    a = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


async def check_url(url: str) -> None:
    """Reject non-http(s) schemes and hosts that resolve to private, loopback,
    link-local (169.254.x.x, cloud metadata) or reserved addresses."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("Solo se aceptan enlaces http o https.")
    if parts.username or parts.password:
        raise FetchError("El enlace no puede incluir usuario o contraseña.")
    host = parts.hostname.lower().rstrip(".")
    if host in _metadata_hosts or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise FetchError("Ese enlace apunta a una dirección interna y no se puede abrir.")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise FetchError("No encontramos ese sitio. Revisa que el enlace esté bien escrito.")
    if not infos or not all(_ip_ok(i[4][0]) for i in infos):
        raise FetchError("Ese enlace apunta a una dirección interna y no se puede abrir.")


# DNS is checked before every hop, but httpx resolves again when connecting, so a DNS rebinding
# attack with TTL 0 could slip through. If this is exposed to the internet, add an egress proxy.
async def safe_get(url: str, max_bytes: int = MAX_PAGE_BYTES, transport=None) -> tuple[str, bytes, str]:
    """GET that re-validates every redirect and enforces size and time limits.
    Returns (final url, body, content-type). Any network error is raised as FetchError."""
    try:
        return await _safe_get(url, max_bytes, transport)
    except httpx.HTTPError as e:
        raise FetchError(f"No se pudo conectar con el sitio ({type(e).__name__}).")


async def _safe_get(url: str, max_bytes: int, transport) -> tuple[str, bytes, str]:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, transport=transport,
                                 headers={"User-Agent": UA, "Accept-Language": "es-CO,es;q=0.9"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await check_url(url)
            async with client.stream("GET", url) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers.get("location", ""))
                    continue
                if r.status_code in (401, 402, 403):
                    raise FetchError(f"paywall:{r.status_code}")
                if r.status_code >= 400:
                    raise FetchError(f"La página respondió con error {r.status_code}.")
                if int(r.headers.get("content-length") or 0) > max_bytes:
                    raise FetchError("La página es demasiado grande.")
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise FetchError("La página es demasiado grande.")
                return str(r.url), bytes(body), r.headers.get("content-type", "")
        raise FetchError("El enlace redirige demasiadas veces.")


_PAYWALL = re.compile(r'"isAccessibleForFree"\s*:\s*"?false|contenido exclusivo para suscriptores|'
                      r"suscr[ií]bete para (?:seguir|continuar) leyendo", re.I)


DOC_MAX_BYTES = 15 * 1024 * 1024
DOC_EXT = re.compile(r"\.(pdf|docx?|xlsx?|csv|odt|ods)(?:$|[?#])", re.I)


def extract_pdf(raw: bytes, url: str) -> dict:
    """Text of a PDF through pdftotext in a separate process, first 40 pages, killed after 30 s: a hostile
    file can neither hang nor crash the app."""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(raw)
        f.flush()
        try:
            out = subprocess.run(["pdftotext", "-q", "-l", "40", "-enc", "UTF-8", f.name, "-"],
                                 capture_output=True, timeout=30, check=True).stdout
        except (subprocess.SubprocessError, OSError):
            raise FetchError("No se pudo leer el documento PDF.")
    text = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", out.decode("utf-8", "replace"))).strip()
    title = next((l.strip() for l in text.splitlines() if len(l.strip()) > 15), "")[:160]
    # The text doubles as "html" so the hidden-instruction check reads it too.
    return {"html": text, "text": text, "title": title, "date": None, "paywalled": False, "kind": "documento"}


_CITE = re.compile(r"informe|comunicado|resoluci|decreto|bolet|estudio|documento|reporte|datos|encuesta|sentencia|"
                   r"\bley\b|gaceta|\bacta\b|fuente|cifras|report|study|survey|ruling|statement|press release", re.I)


def _words(text: str) -> set[str]:
    t = unicodedata.normalize("NFKD", text.casefold())
    return set(re.findall(r"[a-z0-9]{5,}", "".join(c for c in t if not unicodedata.combining(c))))


def cited_links(page: dict, claims: list[str], tier_of) -> list[tuple[float, str, str]]:
    """Links in a source that are worth following, best first: documents (PDF, spreadsheets), official
    and fact-checking sites, and links whose words match the claims. Menus, headers and footers are
    skipped, and so are plain links to the same site (navigation). tier_of(url) -> 1..4."""
    import lxml.html
    base, links = page["url"], []
    host = urlsplit(base).hostname or ""
    if page.get("kind") != "documento" and page.get("html"):
        try:
            doc = lxml.html.fromstring(page["html"])
            for a in doc.xpath("//a[@href][not(ancestor::nav or ancestor::header or ancestor::footer or ancestor::aside)]"):
                links.append((a.get("href"), " ".join(a.text_content().split())[:160]))
        except (ValueError, lxml.etree.ParserError):
            pass
    links += [(u.rstrip(".,;)"), "") for u in re.findall(r"https?://[^\s<>\"')]+", page.get("text", ""))]
    want = _words(" ".join(claims))
    out: dict[str, tuple[float, str, str]] = {}
    for href, anchor in links:
        url = urljoin(base, (href or "").strip()).split("#", 1)[0]
        if not url.startswith(("http://", "https://")) or url == base:
            continue
        doc_link, same = bool(DOC_EXT.search(url)), (urlsplit(url).hostname or "") == host
        if same and not doc_link:
            continue
        tier = tier_of(url)
        score = (3 if doc_link else 0) + {1: 3, 2: 3, 3: 1.5}.get(tier, 0) \
            + (1 if _CITE.search(anchor + " " + url) else 0) + (1.5 if len(_words(anchor + " " + url) & want) >= 2 else 0)
        if score >= 2.5 and score > out.get(url, (0,))[0]:
            out[url] = (score, url, anchor)
    return sorted(out.values(), reverse=True)


def extract(raw: bytes, url: str) -> dict:
    """Clean article text via trafilatura; flags paywalled pages."""
    html = raw.decode("utf-8", errors="replace")
    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True, favor_precision=True) or ""
    meta = trafilatura.extract_metadata(html, default_url=url)
    title = (meta.title if meta else None) or ""
    paywalled = bool(_PAYWALL.search(html)) and len(text) < 1500
    return {"html": html, "text": text, "title": title.strip(), "date": (meta.date if meta else None), "paywalled": paywalled}
