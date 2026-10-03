"""Public open data as sources: World Bank (any country) and datos.gov.co (Colombia).
Each query becomes a text "page" that goes through the same assessment and quote checks as any
other source. Only these fixed domains are queried, read only."""
import asyncio
import json
import logging
import re

import httpx

from . import llm

log = logging.getLogger("contraste.opendata")
TIMEOUT = httpx.Timeout(30.0, connect=15.0)

# World Bank indicators the model may request (code -> what it measures).
WB_INDICATORS = {
    "SL.UEM.TOTL.ZS": "desempleo",
    "FP.CPI.TOTL.ZG": "inflación anual",
    "NY.GDP.MKTP.KD.ZG": "crecimiento del PIB",
    "NY.GDP.PCAP.CD": "PIB por habitante",
    "SI.POV.NAHC": "pobreza (línea nacional)",
    "SI.POV.GINI": "desigualdad (Gini)",
    "VC.IHR.PSRC.P5": "homicidios por 100.000 habitantes",
    "SP.POP.TOTL": "población",
    "SP.DYN.LE00.IN": "esperanza de vida",
    "SH.DYN.MORT": "mortalidad de menores de 5 años",
    "SE.XPD.TOTL.GD.ZS": "gasto en educación (% del PIB)",
    "SH.XPD.CHEX.GD.ZS": "gasto en salud (% del PIB)",
    "GC.DOD.TOTL.GD.ZS": "deuda del gobierno central (% del PIB)",
    "BX.KLT.DINV.CD.WD": "inversión extranjera directa",
    "MS.MIL.XPND.GD.ZS": "gasto militar (% del PIB)",
    "SM.POP.REFG": "refugiados por país de asilo",
    "IT.NET.USER.ZS": "uso de internet",
    "EG.ELC.ACCS.ZS": "acceso a electricidad",
    "AG.LND.FRST.ZS": "área de bosque",
    "SP.URB.TOTL.IN.ZS": "población urbana",
}


def _page(url, name, domain, title, text):
    return {"url": url, "name": name, "domain": domain, "tier": 1, "title": title, "text": text, "html": "",
            "kind": "datos", "paywalled": False}


async def world_bank(codes: list[str], countries: list[str]) -> list[dict]:
    countries = [c.upper() for c in countries if re.fullmatch(r"[A-Za-z]{3}", c)][:4] or ["COL"]
    out = []
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        for code in [x for x in codes if x in WB_INDICATORS][:2]:
            try:
                r = await c.get(f"https://api.worldbank.org/v2/es/country/{';'.join(countries)}/indicator/{code}",
                                params={"format": "json", "mrv": 8, "per_page": 200})
                meta, rows = r.json()
            except (httpx.HTTPError, ValueError, TypeError) as e:
                log.info("World Bank %s failed: %s", code, e)
                continue
            rows = [x for x in rows or [] if x.get("value") is not None]
            if not rows:
                continue
            name = rows[0]["indicator"]["value"]
            lines = [f"Fuente: Banco Mundial, indicador {code}: {name}."]
            lines += [f"{x['country']['value']}, {x['date']}: {x['value']:g}." for x in rows]
            out.append(_page(f"https://data.worldbank.org/indicator/{code}?locations={'-'.join(countries)}",
                             "Banco Mundial (datos)", "data.worldbank.org", name, "\n".join(lines)))
    return out


class Soql(llm.BaseModel):
    usable: bool = llm.Field(description="false si ninguno de los conjuntos sirve para esta afirmación")
    dataset: llm.Text(20, "id del conjunto elegido (formato xxxx-xxxx)")
    select: llm.Text(300, "cláusula $select de SoQL; puede agregar con count(*), sum(), date_extract_y()")
    where: llm.Text(300, "cláusula $where de SoQL o vacío")
    group: llm.Text(200, "cláusula $group o vacío")
    order: llm.Text(200, "cláusula $order o vacío")


SOQL_TASK = """Tarea: elige, entre los conjuntos de datos abiertos del Estado colombiano listados, el que sirva para
verificar la afirmación y escribe UNA consulta SoQL (Socrata) de solo lectura que devuelva a lo sumo 50 filas con
las cifras necesarias (usa agregaciones como count(*) o sum(cantidad) agrupadas por año o lugar). Usa solo
columnas listadas. Si ninguno sirve, usable=false."""


async def datos_gov_co(query: str, claim: str) -> list[dict]:
    if not query.strip():
        return []
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            r = await c.get("https://api.us.socrata.com/api/catalog/v1",
                            params={"domains": "www.datos.gov.co", "search_context": "www.datos.gov.co",
                                    "q": query[:120], "only": "dataset", "limit": 4})
            found = [x["resource"] for x in r.json().get("results", [])]
        except (httpx.HTTPError, ValueError) as e:
            log.info("datos.gov.co catalog failed: %s", e)
            return []
        if not found:
            return []
        catalog = [{"id": d["id"], "nombre": d["name"], "descripcion": (d.get("description") or "")[:300],
                    "columnas": d.get("columns_field_name", [])[:40], "actualizado": d.get("data_updated_at")}
                   for d in found]
        try:
            q = await llm.ask(SOQL_TASK, f"AFIRMACIÓN:\n{claim}\n\nCONJUNTOS:\n{json.dumps(catalog, ensure_ascii=False)}", Soql)
        except llm.LLMError:
            return []
        ds = next((d for d in found if d["id"] == q.dataset), None)
        if not q.usable or not ds:
            return []
        params = {"$select": q.select, "$limit": 50}
        params |= {k: v for k, v in (("$where", q.where), ("$group", q.group), ("$order", q.order)) if v.strip()}
        try:
            r = await c.get(f"https://www.datos.gov.co/resource/{ds['id']}.json", params=params)
            rows = r.json() if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError) as e:
            log.info("datos.gov.co query failed: %s", e)
            return []
    if not isinstance(rows, list) or not rows:
        return []
    lines = [f"Fuente: Datos Abiertos Colombia, conjunto «{ds['name']}» ({ds['id']}), actualizado {ds.get('data_updated_at', '')[:10]}.",
             f"Consulta: {' '.join(f'{k}={v}' for k, v in params.items())}"]
    lines += ["; ".join(f"{k}: {v}" for k, v in row.items()) + "." for row in rows[:50]]
    return [_page(f"https://www.datos.gov.co/d/{ds['id']}", "Datos Abiertos Colombia", "datos.gov.co",
                  ds["name"], "\n".join(lines))]


async def fetch(claim) -> list[dict]:
    """Open data for a claim, based on what the model asked for when extracting it."""
    jobs = []
    if claim.wb_indicators:
        jobs.append(world_bank(claim.wb_indicators, claim.countries))
    if claim.datos_query:
        jobs.append(datos_gov_co(claim.datos_query, claim.text))
    pages = []
    for res in await asyncio.gather(*jobs, return_exceptions=True):
        if isinstance(res, list):
            pages += res
        else:
            log.info("open data: %s", res)
    return pages
