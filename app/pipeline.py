"""Fact-checking pipeline. The LLM proposes; the rules in `rules.py` decide."""
import asyncio
import hashlib
import json
import logging
import re

from . import db, gate, llm, memory, opendata, settings
from .fetch import DOC_EXT, DOC_MAX_BYTES, FetchError, cited_links, extract, extract_pdf, safe_get
from .ingest import Charged, UserError, fetch_thumb, platform, read_article, read_image, read_video
from .rules import (RATINGS, REASONS, adjust_claim, concentration, find_injection, host_of, is_excluded, focus_rating,
                    is_lead_only, predates, quote_in_page, source_group, source_info)
from .search import canonical, search
from .cards import clip

WORD = re.compile(r"\w{4,}")


def relevant_text(text: str, claims: list[str], limit: int = 5000) -> str:
    """Keep only paragraphs that share words with the claims (in original order), up to `limit` chars.
    Every paragraph is a verbatim slice of the page, so quotes are still checked against the full text."""
    if len(text) <= limit:
        return text
    keys = {w.casefold() for c in claims for w in WORD.findall(c)}
    paras = [p for p in text.split("\n") if p.strip()]
    scored = sorted(range(len(paras)), key=lambda i: (-len(keys & {w.casefold() for w in WORD.findall(paras[i])}), i))
    keep, size = set(), 0
    for i in [0] + scored:  # the first paragraph (lede) is always kept
        if i not in keep and size + len(paras[i]) <= limit:
            keep.add(i)
            size += len(paras[i]) + 1
    return "\n".join(paras[i] for i in sorted(keep))

log = logging.getLogger("contraste.pipeline")

INPUT_NOTE = ("El contenido enviado incluía frases dirigidas a sistemas automáticos. Se trataron como parte del "
              "texto analizado y no influyeron en la calificación.")
FALLBACK_HEADLINE = {
    "verdadero": "Lo que circula está respaldado por varias fuentes independientes.",
    "matices": "Lo que circula es en general cierto, pero le falta contexto.",
    "enganoso": "Lo que circula mezcla hechos reales con conclusiones que no se sostienen.",
    "falso": "Lo que circula es falso según las fuentes consultadas.",
    "sin_pruebas": "No encontramos pruebas suficientes para confirmarlo ni para descartarlo.",
    "no_verificable": "Es opinión, sátira o predicción: no se puede verificar como un hecho.",
}
ADJUSTED_LINE = {"sin_pruebas": "Sin pruebas suficientes: {}", "enganoso": "Impreciso o sin contexto: {}"}


CHECKABLE = {"afirmacion", "pregunta_sobre_hecho", "opinion_o_satira_publica"}
# Every rejection says what we found and whether the verification was charged. Rule: if we stop before any costly
# analysis it is not charged (run_job adds "No se descontó de tu saldo"); once the image was read or the main model
# ran, it is charged (Charged) and the message says why.
NOT_A_CLAIM = ("No lo verificamos: no encontramos nada que se presente como un hecho. Contraste verifica noticias, "
               "rumores, titulares, cifras y mensajes sobre asuntos públicos; no responde preguntas generales ni hace tareas.")
NOT_RELATED = ("No lo verificamos: parece un asunto local de otro país, sin relación con Colombia ni con temas que "
               "afecten a cualquiera (salud, ciencia, tecnología, estafas, hechos internacionales de gran alcance).")
CHARGED_IMAGE = "Leer la imagen ya tuvo un costo, así que esta verificación se descontó de tu saldo."
CHARGED_ANALYSIS = "Analizarlo ya usó nuestro modelo de análisis, así que esta verificación se descontó de tu saldo."
KIND_REASON = {
    "pregunta_general": "parece una pregunta general o una tarea (una traducción, un cálculo, cómo hacer algo)",
    "conversacion": "parece un saludo o una conversación",
    "personal": "trata de un asunto privado de una persona, no de un asunto público",
    "publicidad_u_otro": "parece publicidad o un contenido sin afirmaciones sobre hechos",
}
NOT_CHECKABLE = ("No lo verificamos: {reason}. Contraste verifica afirmaciones sobre asuntos públicos que circulan "
                 "como hechos.")


def _seen(content: dict) -> str:
    """For images: what we saw, so the person knows why."""
    if content.get("kind") != "image":
        return ""
    seen = f"En tu imagen vimos: «{clip(content.get('seen') or 'sin descripción', 160)}»"
    if (content.get("seen_text") or "").strip():
        seen += f", con el texto «{clip(content['seen_text'].strip(), 120)}»"
    return seen + ". "


def _reject(content: dict, message: str):
    """Images were already read (a cost): charged. Text stopped at the gate: not charged."""
    if content.get("kind") == "image":
        return Charged(f"{_seen(content)}{message} {CHARGED_IMAGE}")
    return UserError(message)


class Duplicate(Exception):
    def __init__(self, article_id: str):
        self.article_id = article_id


def sourced_timeline(items: list[dict], n_sources: int) -> list[dict]:
    """Keep only timeline events backed by at least one source of this check; drop unknown source ids."""
    out = []
    for t in items:
        ids = [i for i in dict.fromkeys(t.get("sources") or []) if isinstance(i, int) and 0 <= i < n_sources]
        if ids:
            out.append({"date": t["date"], "event": t["event"], "sources": ids})
    return out


def pick(candidates: list[dict], n: int) -> list[dict]:
    """Favour trust tier and variety: one result per owner group first, then fill up."""
    ranked = sorted(candidates, key=lambda r: source_info(r["url"])[0])
    first, rest, seen = [], [], set()
    for r in ranked:
        g = source_group(host_of(r["url"]))
        (rest if g in seen else first).append(r)
        seen.add(g)
    return (first + rest)[:n]


async def ingest(inp: dict, emit, *, dedup: bool) -> dict:
    kind = inp["kind"]
    if kind == "text":
        return {"kind": "text", "text": inp["text"], "title": "Texto enviado"}
    if kind == "image":
        c = await read_image(inp["img"], emit)
        return c
    url = inp["url"]
    loop = asyncio.get_running_loop()
    c = await (read_video(url, emit, loop) if platform(url) else read_article(url, emit))
    c["url"] = url
    if dedup and c.get("content_hash") and (dup := db.find_duplicate(content_hash=c["content_hash"])):
        raise Duplicate(dup)
    if c.get("thumb_url"):
        c["thumb"] = await fetch_thumb(c["thumb_url"])
    return c


async def investigate(inp: dict, emit, *, dedup: bool = True, extra_urls: tuple = (), screen: bool = True) -> tuple[dict, dict, object]:
    """Returns (result, dedup keys, PIL thumbnail or None). The result carries the models used and their cost."""
    records: list[dict] = []
    token = llm.USAGE.set(records)
    try:
        result, keys, thumb = await _investigate(inp, emit, dedup=dedup, extra_urls=extra_urls, screen=screen)
    finally:
        llm.USAGE.reset(token)
    result["usage"] = llm.usage_summary(records)
    return result, keys, thumb


async def _investigate(inp: dict, emit, *, dedup: bool, extra_urls: tuple = (), screen: bool = True) -> tuple[dict, dict, object]:
    """extra_urls: sources a reader contributed. They are read and assessed like any search result, against
    every claim, and they alone can never move a claim to verdadero or falso.
    screen: run the entry gate (relevance to Colombia). Off for re-investigations of published checks."""
    content = await ingest(inp, emit, dedup=dedup)
    keys = {"content_hash": content.get("content_hash")}
    notes, audit = [], {"quotes_rejected": [], "page_injections": [], "input_injection": None, "outdated": []}

    user_hosts = {host_of(inp["url"])} if inp.get("url") else set()
    user_urls = {canonical(u) for u in re.findall(r"https?://[^\s\"'<>]+", content["text"])}
    if inp.get("url"):
        user_urls.add(canonical(inp["url"]))

    hit = find_injection(content["text"]) or find_injection(content.get("raw_html", ""))
    if screen:
        await emit("Revisando el contenido", 30)
        verdict = await gate.screen_input(content["text"])
        if not verdict.claim:
            log.info("input turned away by the entry gate: nothing presented as a fact")
            raise _reject(content, NOT_A_CLAIM)
        if not verdict.related:
            log.info("input turned away by the entry gate: not related to Colombia")
            raise _reject(content, NOT_RELATED)
        if verdict.injection and not hit:
            hit = "clasificador: instrucciones dirigidas a una IA"
    if hit:
        log.warning("possible prompt injection in the input: %r", hit)
        audit["input_injection"] = hit
        notes.append(INPUT_NOTE)

    await emit("Separando las afirmaciones", 32)
    wb = json.dumps(opendata.WB_INDICATORS, ensure_ascii=False)
    ex = await llm.ask(llm.EXTRACT_TASK.replace("{wb}", wb),
                       content["text"][:12000], llm.Extraction)
    # Admission gate: general questions, chit-chat, private matters and ads never become articles.
    if ex.input_kind not in CHECKABLE or not (ex.claims or ex.not_verifiable):
        log.info("input rejected as not checkable (%s)", ex.input_kind)
        reason = KIND_REASON.get(ex.input_kind, "no encontramos ninguna afirmación concreta que se pueda comprobar")
        raise Charged(f"{_seen(content)}{NOT_CHECKABLE.format(reason=reason)} {CHARGED_ANALYSIS}")
    if dedup:
        from .similar import embed
        emb2 = embed(ex.circulating)
        keys["emb2"] = emb2.tobytes()
        if dup := db.find_duplicate(emb=emb2):
            raise Duplicate(dup)

    claims = ex.claims[: settings.MAX_CLAIMS]
    await emit("Separando las afirmaciones", 34, claims=[c.short for c in claims])
    sources: list[dict] = []
    src_idx: dict[str, int] = {}
    pages: dict[str, dict | None] = {}
    omitted: dict[str, dict] = {}
    evidence: list[list[dict]] = [[] for _ in claims]
    fetch_sem, llm_sem = asyncio.Semaphore(10), asyncio.Semaphore(8)

    def sid(key: str) -> str:
        return hashlib.sha1(key.encode()).hexdigest()[:10]

    async def load(r) -> dict | None:
        key = canonical(r["url"])
        tier, name, domain = source_info(r["url"])
        await emit(f"Leyendo {name}", None, source={"id": sid(key), "name": name, "state": "reading"})
        page = await _load(r, key, tier, name, domain)
        state = {"state": "ok"} if page else {"state": "omitted", "reason": omitted.get(key, {}).get("reason", "")}
        await emit(f"Leyendo {name}", None, source={"id": sid(key), "name": name, **state})
        return page

    async def _load(r, key, tier, name, domain) -> dict | None:
        try:
            async with fetch_sem:
                final, body, ctype = await safe_get(r["url"], **({"max_bytes": DOC_MAX_BYTES} if DOC_EXT.search(r["url"]) else {}))
            if "pdf" in ctype or body[:5] == b"%PDF-":
                page = await asyncio.to_thread(extract_pdf, body, final)
            elif "html" in ctype or "text" in ctype:
                page = await asyncio.to_thread(extract, body, final)
            else:
                raise FetchError("no es una página de texto")
        except FetchError as e:
            reason = "Muro de pago o acceso restringido" if str(e).startswith("paywall") else "No se pudo leer"
            omitted[key] = {"url": r["url"], "name": name, "domain": domain, "reason": reason}
            return None
        except Exception:  # one broken source must never take the whole check down
            log.exception("unreadable source: %s", r["url"])
            omitted[key] = {"url": r["url"], "name": name, "domain": domain, "reason": "No se pudo leer"}
            return None
        if hit := find_injection(page["html"]):
            log.warning("source dropped for prompt injection: %s (%r)", r["url"], hit)
            audit["page_injections"].append({"url": r["url"], "pattern": hit})
            omitted[key] = {"url": r["url"], "name": name, "domain": domain,
                            "reason": "Descartada: la página contenía instrucciones ocultas"}
            return None
        if page["paywalled"]:
            omitted[key] = {"url": r["url"], "name": name, "domain": domain, "reason": "Muro de pago"}
            return None
        if len(page["text"]) < 200:
            omitted[key] = {"url": r["url"], "name": name, "domain": domain, "reason": "Sin texto legible"}
            return None
        if await gate.page_injected(page["text"]):
            log.warning("source dropped by the classifier for prompt injection: %s", r["url"])
            audit["page_injections"].append({"url": r["url"], "pattern": "clasificador"})
            omitted[key] = {"url": r["url"], "name": name, "domain": domain,
                            "reason": "Descartada: la página contenía instrucciones ocultas"}
            return None
        pages[key] = page | {"url": r["url"], "tier": tier, "name": name, "domain": domain, "kind": page.get("kind", "web"),
                             "title": page["title"] or r.get("title", ""), "via": r.get("via"),
                             "memory": r.get("memory", False)}
        return pages[key]

    def register(ci: int, page: dict, ev):
        """Record validated evidence from one source for claim ci."""
        stance = ev.stance
        if stance == "no_relacionada":
            return
        if not quote_in_page(ev.quote, page["text"]):
            log.warning("quote dropped, not found in %s: %r", page["url"], ev.quote[:80])
            audit["quotes_rejected"].append({"url": page["url"], "quote": ev.quote[:200]})
            return
        if stance == "contradice" and predates(page.get("date"), claims[ci].when):
            # Published before the moment the claim is about: it describes an earlier situation.
            stance = "contexto"
            audit["outdated"].append({"url": page["url"], "claim": ci, "date": page.get("date")})
        key = canonical(page["url"])
        group = source_group(page["domain"])
        # A record that is itself the fact (the certified result, the published law, the data table), from an
        # official body that is not a party to the claim. What an official body says is just another version.
        primary = not ev.is_party and (page.get("kind") == "datos" or (page["tier"] == 1 and ev.basis in ("dato_oficial", "documento")))
        if key not in src_idx:
            src_idx[key] = len(sources)
            sources.append({"url": page["url"], "title": clip(page["title"], 160), "name": page["name"],
                            "domain": page["domain"], "tier": page["tier"], "summary": ev.summary,
                            "basis": ev.basis, "group": group, "kind": page.get("kind", "web"), "date": page.get("date"),
                            "via": page.get("via"), "memory": page.get("memory", False),
                            "party": False, "primary": False, "stances": []})
        i = src_idx[key]
        sources[i]["stances"].append(stance)
        sources[i]["party"] |= ev.is_party
        sources[i]["primary"] |= primary
        evidence[ci].append({"source": i, "stance": stance, "summary": ev.summary, "basis": ev.basis, "date": page.get("date"),
                             "contributed": key in contributed,
                             "domain": page["domain"], "tier": page["tier"], "group": group,
                             "party": ev.is_party, "primary": primary})

    done = 0

    async def assess(page: dict, total: int):
        """ONE call per source covering every claim, not only the ones whose search found it: a search can come
        back empty, and the article that confirms the main claim was then never read against it (a resignation
        rated "sin pruebas" while its own sources reported it). The source is still alone inside its delimiter."""
        nonlocal done
        cis = list(range(len(claims)))
        before = {i: len(evidence[i]) for i in cis}
        listed = "\n".join(f"[{ci}] {claims[ci].text}" + (f" (se refiere a: {claims[ci].when})" if claims[ci].when else "")
                           for ci in cis)
        data = (f"AFIRMACIONES A VERIFICAR:\n{listed}\n\nFUENTE: {page['name']} ({page['domain']})\n"
                f"FECHA DE LA FUENTE: {page.get('date') or 'desconocida'}\n"
                f"TÍTULO: {page['title']}\n\nTEXTO DE LA FUENTE:\n"
                f"{relevant_text(page['text'], [claims[ci].text for ci in cis])}")
        try:
            async with llm_sem:
                ev = await llm.ask(llm.EVIDENCE_TASK, data, llm.SourceEvidence, fast=True)
            for item in ev.items:
                if item.claim in cis:
                    register(item.claim, page, item)
        except llm.LLMError as e:
            log.warning("evidence not assessed (%s): %s", page["url"], e)
        done += 1
        stances = [e["stance"] for i in cis for e in evidence[i][before[i]:]]
        stance = next((s for s in ("contradice", "confirma") if s in stances), "contexto" if stances else "no_relacionada")
        await emit(f"Contrastando · {done} de {total} fuentes", 60 + int(22 * done / total),
                   source={"id": sid(canonical(page["url"])), "name": page["name"], "state": "assessed", "stance": stance})

    # 1. Search for every claim at once.
    await emit("Buscando en Colombia y el mundo", 35,
               queries=[q for c in claims for q in list(c.queries) + list(c.queries_en)][:12])
    results = await asyncio.gather(*(search(c.queries, c.text, c.queries_en) for c in claims))
    wanted: dict[str, dict] = {}          # canonical URL -> search result
    page_claims: dict[str, list[int]] = {}  # canonical URL -> claims it showed up for
    for ci, res in enumerate(results):
        cands = [r for r in res if not is_excluded(r["url"], user_hosts) and canonical(r["url"]) not in user_urls]
        for r in pick(cands, settings.MAX_SOURCES_PER_CLAIM):
            key = canonical(r["url"])
            wanted.setdefault(key, r)
            page_claims.setdefault(key, []).append(ci)
    # Pages we read in earlier checks whose paragraphs match these claims: leads the search engine may not
    # return any more. They are downloaded again below and pass every rule like any other result.
    recalled = await asyncio.to_thread(memory.recall, [c.text for c in claims], set(wanted) | user_urls)
    for ci, hits in enumerate(recalled):
        for r in hits:
            if not is_excluded(r["url"], user_hosts):
                wanted.setdefault(canonical(r["url"]), r)
                page_claims.setdefault(canonical(r["url"]), []).append(ci)
    contributed = {canonical(u) for u in extra_urls}
    for u in extra_urls:
        wanted.setdefault(canonical(u), {"url": u, "title": ""})
        page_claims[canonical(u)] = list(range(len(claims)))
    await emit(f"Buscando en {len(wanted)} fuentes", 45)

    # 2. Read every page and query open data, in parallel.
    data_claims = [ci for ci, c in enumerate(claims) if c.wb_indicators or c.datos_query]
    loaded, data = await asyncio.gather(
        asyncio.gather(*(load(r) for r in wanted.values())),
        asyncio.gather(*(opendata.fetch(claims[ci]) for ci in data_claims)))
    for ci, dps in zip(data_claims, data):
        for dp in dps:
            key = canonical(dp["url"])
            if key not in pages:
                pages[key] = dp
                await emit(f"Leyendo {dp['name']}", None, source={"id": sid(key), "name": dp["name"], "state": "ok", "kind": "datos"})
            page_claims.setdefault(key, [])
            if ci not in page_claims[key]:
                page_claims[key].append(ci)

    # 3. Assess each source once, in parallel.
    # Encyclopedias anyone can edit (or that a machine writes) are not evidence; their references are
    # followed below instead, to reach the sources they are based on.
    ready = [(p, page_claims[k]) for k, p in pages.items() if p and page_claims.get(k) and not is_lead_only(p["url"])]
    leads = {k: page_claims[k] for k, p in pages.items() if p and page_claims.get(k) and is_lead_only(p["url"])}
    await emit("Contrastando lo que dice cada fuente", 60)
    await asyncio.gather(*(assess(p, len(ready)) for p, _ in ready))

    # 4. Go deeper: the documents and sources that the useful pages cite (the official report behind a
    #    story, a PDF, the outlet it is based on), up to DEEP_DEPTH hops and DEEP_MAX_PAGES pages in total.
    #    They pass the same checks: safe download, hidden instructions, verbatim quotes, tiers and owners.
    def claims_of() -> dict[str, list[int]]:
        by_key: dict[str, list[int]] = {}
        for ci, ev in enumerate(evidence):
            for e in ev:
                cis = by_key.setdefault(canonical(sources[e["source"]]["url"]), [])
                if ci not in cis:
                    cis.append(ci)
        return by_key

    budget, frontier = settings.DEEP_MAX_PAGES, claims_of() | leads
    texts = [c.text for c in claims]
    for _ in range(settings.DEEP_DEPTH):
        cands: dict[str, tuple[float, dict, list[int]]] = {}
        for key, cis in frontier.items():
            parent = pages.get(key)
            if not parent:
                continue
            for score, url, anchor in cited_links(parent, [texts[ci] for ci in cis], lambda u: source_info(u)[0])[:3]:
                k = canonical(url)
                if k in pages or k in wanted or k in user_urls or k in omitted or is_excluded(url, user_hosts):
                    continue
                if score > cands.get(k, (0,))[0]:
                    cands[k] = (score, {"url": url, "title": anchor, "via": parent["name"]}, cis)
        chosen = sorted(cands.items(), key=lambda kv: -kv[1][0])[:budget]
        if not chosen:
            break
        budget -= len(chosen)
        await emit(f"Siguiendo {len(chosen)} documentos y fuentes citadas", None,
                   queries=[r["title"] or host_of(r["url"]) for _, (_, r, _) in chosen][:6])
        known = set(claims_of())
        await asyncio.gather(*(load(r) for _, (_, r, _) in chosen))
        deeper = [(pages[k], cis) for k, (_, _, cis) in chosen if pages.get(k)]
        done = 0
        await asyncio.gather(*(assess(p, len(deeper)) for p, _ in deeper))
        frontier = {k: v for k, v in claims_of().items() if k not in known}
        if budget <= 0 or not frontier:
            break

    await emit("Contrastando y calificando", 82)
    final_claims = []
    if claims:
        payload = [{"index": i, "afirmacion": c.text, "central": c.central, "se_refiere_a": c.when or None,
                    "evidencia": [{"id": e["source"], "medio": sources[e["source"]]["name"], "nivel": e["tier"],
                                   "dueño": e["group"], "parte_interesada": e["party"], "dato_primario": e["primary"],
                                   "postura": e["stance"], "base": e["basis"], "resumen": e["summary"],
                                   "fecha": sources[e["source"]].get("date")}
                                  for e in evidence[i]]} for i, c in enumerate(claims)]
        v = await llm.ask(llm.VERDICT_TASK, json.dumps(payload, ensure_ascii=False), llm.Verdict)
        by_index = {cv.index: cv for cv in v.claims}
        for i, c in enumerate(claims):
            cv = by_index.get(i)
            proposed = cv.rating if cv else "sin_pruebas"
            rating, reason = adjust_claim(proposed, evidence[i])
            if rating in ("verdadero", "falso") and any(e["contributed"] for e in evidence[i]):
                # A contributed source may add weight, but the rating must stand without it.
                without = adjust_claim(proposed, [e for e in evidence[i] if not e["contributed"]])
                if without[0] != rating:
                    rating, reason = without[0], without[1] or REASONS["contributed_alone"]
            # The model's finding is kept even when a rule adjusts the rating; the reason is shown next to it.
            item = {"text": c.text, "short": c.short, "central": c.central, "rating": rating, "proposed": proposed, "adjusted": reason,
                    "explanation": cv.explanation if cv else REASONS["no_evidence"],
                    "card_line": cv.card_line if cv else c.short,
                    "evidence": [{"source": e["source"], "stance": e["stance"], "party": e["party"]}
                                 for e in evidence[i]]}
            if reason:
                item["card_line"] = ADJUSTED_LINE.get(rating, "{}").format(c.short)
            final_claims.append(item)
        headline, timeline = v.headline, sourced_timeline([t.model_dump() for t in v.timeline], len(sources))
    else:
        headline, timeline = FALLBACK_HEADLINE["no_verificable"], []

    final_claims.sort(key=lambda c: not c["central"])  # the central claim first, everywhere it is shown
    rating = focus_rating(final_claims)
    if not claims:
        headline = FALLBACK_HEADLINE[rating]
    elif any(c["adjusted"] for c in final_claims):
        # The model wrote its headline for ratings the rules changed: say plainly what each claim ended up as.
        headline = " ".join(f"{c['short'].rstrip('.')}: {RATINGS[c['rating']].lower()}." for c in final_claims)
    for s in sources:
        st = set(s.pop("stances"))
        s["stance"] = st.pop() if len(st) == 1 else "contexto"
    groups = {s["group"] for s in sources}
    conc = concentration([{"source": i, "group": s["group"]} for i, s in enumerate(sources)])
    if conc:
        notes.append(f"{conc[1]} de las {conc[2]} fuentes pertenecen al mismo dueño ({conc[0]}).")
    diversity = {"groups": len(groups), "data": sum(s["kind"] == "datos" for s in sources),
                 "primary": sum(s["primary"] for s in sources), "party": sum(s["party"] for s in sources),
                 "concentrated": list(conc) if conc else None}
    if audit["outdated"]:
        notes.append(f"{len(audit['outdated'])} fuentes publicadas antes del momento al que se refiere la afirmación se "
                     "tomaron como contexto: describen una situación anterior y no la pueden desmentir.")
    if audit["quotes_rejected"]:
        notes.append(f"Se descartaron {len(audit['quotes_rejected'])} citas que no aparecían textualmente en su fuente.")

    result = {
        "title": ex.title, "topic": ex.topic, "rating": rating, "headline": headline, "circulating": ex.circulating,
        "input": {"kind": content["kind"], "url": inp.get("url"), "fingerprint": content.get("fingerprint")},
        "claims": final_claims, "not_verifiable": [n.model_dump() for n in ex.not_verifiable],
        "timeline": timeline, "sources": sources, "diversity": diversity, "omitted": list(omitted.values()), "notes": notes,
        "private_person": ex.about_private_person,
        "security": {"input_injection": bool(audit["input_injection"]), "page_injections": len(audit["page_injections"])},
        "audit": audit,
        "media": {"thumb": None, "original_url": inp.get("url"),
                  "transcript": content.get("transcript")},
        "reviewed_at": db.iso(),
        # So anyone can repeat the research: what was searched, and how many cited documents were followed.
        "method": {"queries": [q for c in claims for q in list(c.queries) + list(c.queries_en)][:20],
                   "followed": sum(1 for s in sources if s.get("via")), "recalled": sum(1 for s in sources if s.get("memory"))},
    }
    memory.remember_later(list(pages.values()))
    return result, keys, content.get("thumb")


def publish_decision(result: dict) -> tuple[str, str | None]:
    """Quality bar for showing up on the front page, in search and in the sitemap."""
    srcs = result.get("sources", [])
    # Only the submitted content counts here. A source page with hidden instructions is already dropped;
    # if it also unlisted the article, anyone could hide a correct check by planting such a page.
    if result.get("security", {}).get("input_injection"):
        return "unlisted", "Se activó una regla de seguridad durante la verificación."
    if result.get("private_person"):
        return "unlisted", "Trata sobre una persona que no es figura pública."
    if len(srcs) < settings.PUBLISH_MIN_SOURCES:
        return "unlisted", f"Tiene menos de {settings.PUBLISH_MIN_SOURCES} fuentes válidas."
    if not any(s["tier"] <= 2 for s in srcs):
        return "unlisted", "No tiene fuentes oficiales ni de verificadores."
    return "listed", None
