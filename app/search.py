"""Web search: DuckDuckGo (ddgs) first, Brave as a backup key, Google Fact Check as an optional extra."""
import asyncio
import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from . import settings

log = logging.getLogger("contraste.search")


def canonical(url: str) -> str:
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "gclid"))]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", urlencode(q), ""))


async def _brave(q: str, world: bool) -> list[dict]:
    # Brave's API rejects country=CO (Colombia is not in its country enum), so we only hint the language.
    params = {"q": q, "count": 10} | ({} if world else {"search_lang": "es"})
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://api.search.brave.com/res/v1/web/search", params=params,
                        headers={"X-Subscription-Token": settings.BRAVE_API_KEY, "Accept": "application/json"})
        r.raise_for_status()
    return [{"url": x["url"], "title": x.get("title", "")} for x in r.json().get("web", {}).get("results", [])]


def _ddg_sync(q: str, world: bool) -> list[dict]:
    from ddgs import DDGS
    region = "wt-wt" if world else "co-es"
    # 15 s: on slow egress the default (~5 s) times out before the engines answer.
    return [{"url": x["href"], "title": x.get("title", "")}
            for x in DDGS(timeout=15).text(q, region=region, max_results=10)]


async def _web(q: str, world: bool = False) -> list[dict]:
    """DuckDuckGo first; if it fails or comes back empty, Brave as a backup (when there is a key)."""
    try:
        out = await asyncio.to_thread(_ddg_sync, q, world)
        if out:
            return out
        log.warning("DuckDuckGo returned nothing for %r; trying Brave", q)
    except Exception as e:  # ddgs raises DDGSException and friends
        log.warning("DuckDuckGo failed (%s); trying Brave", e)
    if settings.BRAVE_API_KEY:
        return await _brave(q, world)
    return []


async def _factcheck(q: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://factchecktools.googleapis.com/v1alpha1/claims:search",
                        params={"query": q, "key": settings.GOOGLE_FACTCHECK_API_KEY})  # every language and country
        r.raise_for_status()
    return [{"url": rv["url"], "title": rv.get("title", "")}
            for cl in r.json().get("claims", []) for rv in cl.get("claimReview", []) if rv.get("url")]


async def search(queries: list[str], claim_text: str, queries_en: list[str] = ()) -> list[dict]:
    """Unique results (by canonical URL): Spanish queries focused on Colombia, English queries worldwide."""
    jobs = [_web(q) for q in queries] + [_web(q, world=True) for q in queries_en]
    if settings.GOOGLE_FACTCHECK_API_KEY:
        jobs.append(_factcheck(claim_text))
    seen, out = set(), []
    for res in await asyncio.gather(*jobs, return_exceptions=True):
        if isinstance(res, Exception):
            log.warning("search failed: %s", res)
            continue
        for item in res:
            key = canonical(item["url"])
            if key not in seen and item["url"].startswith(("http://", "https://")):
                seen.add(key)
                out.append(item)
    return out
