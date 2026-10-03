"""Today's headlines about Colombia (Google News), so «Probar con un ejemplo» offers something people are
talking about right now instead of always the same sample.

Only news related to Colombia in some way. The fast model reads the candidates in one batch, because a
keyword list cannot tell that "EE. UU. retira la certificación antidrogas" concerns Colombia; if it fails,
only headlines from the Colombia search are kept. Refreshed every 30 minutes by one replica into the
database, like the market indicators."""
import logging
import random
import re
import time
import xml.etree.ElementTree as ET

import httpx
from pydantic import BaseModel

from . import db, llm

log = logging.getLogger("contraste.news")
FEED = "https://news.google.com/rss?hl=es-419&gl=CO&ceid=CO:es-419"                      # top stories in Colombia
SEARCH = "https://news.google.com/rss/search?q=Colombia&hl=es-419&gl=CO&ceid=CO:es-419"   # stories about Colombia
FALLBACK = ("Despidieron a la periodista Camila Zuluaga de Blu Radio por criticar a Abelardo, pero durante los "
            "4 años de Petro nunca peligró su programa.")
# Never offered as a sample: stories about children or that could hurt a private person if checked for fun.
SENSITIVE = re.compile(r"menor(es)? de edad|\bniñ[oa]s?\b|adolescente|abuso sexual|violaci[oó]n|suicid|feminicid", re.I)


def clean(title: str) -> str:
    """"Video | Colapsó vivienda … - El Tiempo" -> "Colapsó vivienda …"."""
    title = re.sub(r"\s+-\s+[^-]{2,60}$", "", title.strip())          # trailing " - Outlet"
    title = re.sub(r"^(?:[\wÁÉÍÓÚáéíóúñÑ ]{2,20})\s*\|\s*", "", title)  # leading "Atención | ", "Video | "
    return title.strip(" .")


def parse(xml: str) -> list[dict]:
    out = []
    for it in ET.fromstring(xml).findall("./channel/item"):
        title = clean(it.findtext("title") or "")
        if 40 <= len(title) <= 200 and not SENSITIVE.search(title):
            out.append({"title": title, "source": (it.findtext("source") or "").strip()})
    return out


class Related(BaseModel):
    related: list[int] = []


RELATED_TASK = """Tarea: de la lista numerada de titulares, devuelve en 'related' los números de los que tienen relación
con Colombia de cualquier forma: ocurren en Colombia, involucran a colombianos o a instituciones, empresas, regiones o
figuras públicas colombianas, o afectan directamente al país (por ejemplo, una decisión de otro país sobre Colombia o lo
que pasa en la frontera). Deja fuera los que no tienen ninguna relación con Colombia."""


async def colombian(items: list[dict], about: set[str]) -> list[dict]:
    """Keep the headlines related to Colombia. `about` holds the titles from the Colombia search, the fallback."""
    listed = "\n".join(f"[{i}] {it['title']}" for i, it in enumerate(items))
    try:
        keep = set((await llm.ask(RELATED_TASK, listed, Related, fast=True)).related)
        return [it for i, it in enumerate(items) if i in keep]
    except Exception as e:
        log.warning("could not classify headlines (%s); keeping only the Colombia search", e)
        return [it for it in items if it["title"] in about]


async def refresh():
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0 (compatible; Contraste/1.0)"}) as c:
            top, search = await c.get(FEED), await c.get(SEARCH)
        top.raise_for_status()
        search.raise_for_status()
        about = parse(search.text)[:25]
        seen, candidates = set(), []
        for it in parse(top.text)[:25] + about:
            if it["title"] not in seen:
                seen.add(it["title"])
                candidates.append(it)
        items = (await colombian(candidates, {it["title"] for it in about}))[:15]
    except Exception as e:
        log.warning("could not read today's headlines: %s", e)
        return
    if items:
        db.set_setting("trending", {"items": items, "at": db.iso()})


_cache: tuple[float, list] = (0.0, [])


def example() -> str:
    """A headline of the moment, a different one each time; the old sample if none is available."""
    global _cache
    if time.monotonic() - _cache[0] > 60 or not _cache[1]:  # an empty list is re-read: headlines may have just arrived
        _cache = (time.monotonic(), (db.setting("trending", {}) or {}).get("items", []))
    return random.choice(_cache[1])["title"] if _cache[1] else FALLBACK


if __name__ == "__main__":
    assert clean("Atención | Bruce Mac Master sale de la presidencia de la ANDI tras más de 13 años - Valora Analitik") == \
        "Bruce Mac Master sale de la presidencia de la ANDI tras más de 13 años"
    assert clean("Juan Manuel Santos no está de acuerdo con prorrogar la JEP - Revista Semana") == \
        "Juan Manuel Santos no está de acuerdo con prorrogar la JEP"
    assert SENSITIVE.search("Menor de edad que le disparó al exalcalde")
    print("ok")
