"""Market indicators for the masthead: dollar (TRM), coffee, gold and oil, each from an official or primary
source, with its own date and a link to it. Refreshed in the background every 30 minutes by one replica and
kept in the database; a source that fails keeps its last good value, which shows its real date."""
import asyncio
import csv
import html
import io
import logging
import re
import time

import httpx

from . import db, settings

log = logging.getLogger("contraste.markets")
UA = {"User-Agent": "Mozilla/5.0 (compatible; Contraste/1.0; +https://contraste.co)"}
EVERY_S = 30 * 60

TRM_URL = "https://www.datos.gov.co/resource/32sa-8pi3.json?$order=vigenciadesde%20DESC&$limit=1"
FNC_URL = "https://federaciondecafeteros.org/wp/"
# Banco de la República's page blocks repeated automated reads, so gold is the international spot price.
GOLD_URL = "https://api.gold-api.com/price/XAU"
BRENT_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILBRENTEU"


def _num(text: str) -> float:
    """Colombian number format: "3.307,50" -> 3307.5, "2.170.000" -> 2170000."""
    return float(text.replace(".", "").replace(",", "."))


def parse_trm(data: list) -> dict:
    return {"key": "trm", "label": "Dólar (TRM)", "value": float(data[0]["valor"]), "unit": "cop",
            "date": data[0]["vigenciadesde"][:10], "source": "Superintendencia Financiera", "url": "https://www.datos.gov.co/d/32sa-8pi3"}


def parse_fnc(page: str) -> list[dict]:
    text = html.unescape(page)
    internal = re.search(r"Precio interno de referencia:\s*\$\s*([\d.]+)", text)
    ny = re.search(r"Bolsa de NY:\s*\$?\s*([\d.]+,\d+)", text)
    dates = sorted(d for d in re.findall(r"Fecha:\s*(\d{4}-\d{2}-\d{2})", text) if d <= db.today_co().date().isoformat())
    date = dates[-1] if dates else db.today_co().date().isoformat()
    out = []
    if internal:
        out.append({"key": "cafe", "label": "Café carga 125 kg", "value": _num(internal[1]), "unit": "cop", "date": date,
                    "source": "Federación Nacional de Cafeteros", "url": "https://federaciondecafeteros.org/"})
    if ny:
        out.append({"key": "cafe_ny", "label": "Café NY", "value": _num(ny[1]), "unit": "usc_lb", "date": date,
                    "source": "Federación Nacional de Cafeteros (contrato C, ICE)", "url": "https://federaciondecafeteros.org/"})
    return out


def parse_gold(data: dict) -> dict | None:
    if data.get("currency") != "USD" or not data.get("price"):
        return None
    return {"key": "oro", "label": "Oro (onza)", "value": float(data["price"]), "unit": "usd_oz",
            "date": str(data.get("updatedAt", ""))[:10], "source": "Precio internacional al contado (gold-api.com)",
            "url": "https://gold-api.com/"}


def parse_brent(text: str) -> dict | None:
    rows = [r for r in csv.reader(io.StringIO(text)) if len(r) == 2 and re.match(r"^\d{4}-\d{2}-\d{2}$", r[0])
            and re.match(r"^[\d.]+$", r[1])]
    if not rows:
        return None
    date, value = rows[-1]
    return {"key": "brent", "label": "Petróleo Brent", "value": float(value), "unit": "usd_bbl", "date": date,
            "source": "EIA de EE. UU. (vía FRED)", "url": "https://fred.stlouisfed.org/series/DCOILBRENTEU"}


async def fetch() -> list[dict]:
    async with httpx.AsyncClient(timeout=20, headers=UA, follow_redirects=True) as c:
        async def get(url):
            r = await c.get(url)
            r.raise_for_status()
            return r

        async def one(url, parse, kind):
            try:
                r = await get(url)
                got = parse(r.json() if kind == "json" else r.text)
                return got if isinstance(got, list) else [got] if got else []
            except Exception as e:  # one source down must not hide the others
                log.warning("market source failed (%s): %s", url, e)
                return []
        parts = await asyncio.gather(one(TRM_URL, parse_trm, "json"), one(FNC_URL, parse_fnc, "text"),
                                     one(GOLD_URL, parse_gold, "json"), one(BRENT_URL, parse_brent, "text"))
    return [x for p in parts for x in p]


ORDER = ["trm", "cafe", "cafe_ny", "oro", "brent"]


async def refresh():
    """Fetch every source and merge with what we had: a source that failed keeps its last value."""
    old = {i["key"]: i for i in (db.setting("markets", {}) or {}).get("items", [])}
    new = {i["key"]: i for i in await fetch()}
    items = [(new.get(k) or old.get(k)) for k in ORDER if new.get(k) or old.get(k)]
    db.set_setting("markets", {"items": items, "at": db.iso()})


_cache: tuple[float, list] = (0.0, [])


def current() -> list[dict]:
    """What the masthead shows; read from the database at most once a minute per replica."""
    global _cache
    if time.monotonic() - _cache[0] > 60:
        _cache = (time.monotonic(), (db.setting("markets", {}) or {}).get("items", []))
    return _cache[1]


def show(item: dict) -> str:
    """"$3.341", "US$113,96", "290,70 ¢/lb" in Colombian number format."""
    v, unit = item["value"], item["unit"]
    if unit in ("cop", "usd_oz"):
        return ("US$" if unit == "usd_oz" else "$") + f"{v:,.0f}".replace(",", ".")
    txt = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return {"usd_bbl": f"US${txt}/barril", "usd_oz": f"US${txt}", "usc_lb": f"{txt} ¢/lb"}.get(unit, txt)


if __name__ == "__main__":
    assert _num("3.307,50") == 3307.5 and _num("2.170.000") == 2170000
    assert show({"value": 3341.23, "unit": "cop"}) == "$3.341"
    assert show({"value": 113.96, "unit": "usd_bbl"}) == "US$113,96/barril"
    g = parse_gold({"currency": "USD", "price": 4153.0, "updatedAt": "2026-09-30T19:18:48Z"})
    assert g["value"] == 4153.0 and g["date"] == "2026-09-30" and show(g) == "US$4.153", g
    b = parse_brent("observation_date,DCOILBRENTEU\n2026-09-28,119.97\n2026-09-29,113.96\n2026-09-30,\n")
    assert b["value"] == 113.96 and b["date"] == "2026-09-29"
    print("ok")
