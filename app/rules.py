"""Deterministic server-side rules: source trust, prompt-injection detection, quote validation
and verdict adjustment. None of this goes through the LLM."""
import html
import re
import unicodedata
from urllib.parse import urlsplit

import yaml

from .settings import ROOT

RATINGS = {
    "verdadero": "Verdadero",
    "matices": "Cierto, con matices",
    "enganoso": "Engañoso",
    "falso": "Falso",
    "sin_pruebas": "Sin pruebas",
    "no_verificable": "No verificable",
}
TRUTHY = {"verdadero", "matices"}

_src = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
TIERS: dict[str, tuple[int, str]] = {}
for _tier in (1, 2, 3):
    for _dom, _name in (_src.get(f"tier_{_tier}") or {}).items():
        TIERS[_dom.lower()] = (_tier, _name)
EXCLUDED = {d.lower() for d in _src.get("excluded") or []}
LEADS_ONLY = {d.lower() for d in _src.get("leads_only") or []}
GROUPS: dict[str, str] = {d.lower(): g for g, doms in (_src.get("owner_groups") or {}).items() for d in doms or []}


def host_of(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def _matches(host: str, dom: str) -> bool:
    return host == dom or host.endswith("." + dom)


def source_info(url: str) -> tuple[int, str, str]:
    """(tier, outlet name, domain). The most specific match wins."""
    host = host_of(url)
    best = max((d for d in TIERS if _matches(host, d)), key=len, default=None)
    if best:
        return TIERS[best][0], TIERS[best][1], host
    return 4, host, host


# Suffixes under which anyone can register a name (so "x.com.co" is one owner, not "com.co").
_SUFFIXES_2 = {"com.co", "gov.co", "org.co", "edu.co", "net.co", "mil.co", "nom.co", "co.uk", "org.uk", "gov.uk",
               "ac.uk", "com.ar", "gob.ar", "org.ar", "com.mx", "gob.mx", "org.mx", "com.br", "gov.br", "org.br",
               "com.pe", "gob.pe", "com.ve", "gob.ve", "com.ec", "gob.ec", "com.es", "gob.es", "com.au", "co.jp",
               "com.uy", "gub.uy", "cl", "com.pa", "gob.pa"}
# Free hosting: every site on them is written by whoever signs up, so together they count as ONE owner.
# Otherwise a.blogspot.com and b.blogspot.com would look like two independent outlets.
FREE_HOSTS = ("blogspot.com", "blogger.com", "wordpress.com", "medium.com", "substack.com", "wixsite.com", "wix.com",
              "weebly.com", "tumblr.com", "github.io", "gitlab.io", "netlify.app", "vercel.app", "pages.dev",
              "web.app", "firebaseapp.com", "herokuapp.com", "onrender.com", "glitch.me", "godaddysites.com",
              "squarespace.com", "jimdofree.com", "jimdosite.com", "webnode.com", "webnode.es", "over-blog.com",
              "notion.site", "canva.site", "sites.google.com", "site123.me", "strikingly.com", "carrd.co",
              "hashnode.dev", "ghost.io", "beehiiv.com", "mystrikingly.com", "blog.fc2.com", "ucoz.com", "000webhostapp.com")


def registrable(host: str) -> str:
    """The name someone registered: "noticias.eltiempo.com" -> "eltiempo.com", "a.b.com.co" -> "b.com.co"."""
    parts = host.split(".")
    n = 3 if len(parts) >= 3 and ".".join(parts[-2:]) in _SUFFIXES_2 else 2
    return ".".join(parts[-n:])


def source_group(domain: str) -> str:
    """Owner group of an outlet (sources.yaml > owner_groups). Unknown outlets are grouped by the name that
    was registered, and every site on a free hosting platform belongs to that platform."""
    best = max((d for d in GROUPS if _matches(domain, d)), key=len, default=None)
    if best:
        return GROUPS[best]
    free = next((d for d in FREE_HOSTS if _matches(domain, d)), None)
    return f"{free} (alojamiento gratuito)" if free else registrable(domain)


def is_excluded(url: str, user_hosts: set[str]) -> bool:
    """Social networks, forums, the submitted content itself and outlets owned by the same group."""
    host = host_of(url)
    user_groups = {source_group(h) for h in user_hosts} - user_hosts
    if source_group(host) in user_groups:
        return True
    return any(_matches(host, d) for d in EXCLUDED | user_hosts)


# --- Prompt injection ---------------------------------------------------------------------

# Phrases aimed at a model (imperative or second person). Constructions that show up in regular news
# copy are avoided ("ignoraron las órdenes", "Colombiacheck califica como falso").
_INJECTION = [
    r"\bignora\s+(?:todas?\s+)?(?:tus|las|estas)\s+(?:instrucciones|indicaciones)",
    r"\bignora\s+tus\s+(?:reglas|[oó]rdenes|directrices)",
    r"\bolvida\s+(?:todas?\s+)?(?:tus|las)\s+(?:instrucciones|reglas|indicaciones)",
    r"\bignore\s+(?:all\s+|any\s+)?(?:previous\s+|prior\s+|above\s+|your\s+|the\s+)+(?:instructions|rules|prompts?)",
    r"\bdisregard\s+(?:all\s+|any\s+)?(?:previous\s+|prior\s+|your\s+|the\s+)+(?:instructions|rules)",
    r"\b(?:califica|clasifica|marca)\s+(?:esto|esta\s+(?:afirmaci[oó]n|noticia|fuente|p[aá]gina)|este\s+(?:contenido|texto|art[ií]culo))\s+como",
    r"\b(?:rate|mark|classify|label)\s+(?:this|it)\s+as\s+(?:true|false|verified)",
    r"\bsystem\s+prompt\b|\bprompt\s+del\s+sistema\b|\binstrucciones\s+del\s+sistema\b",
    r"\byou\s+are\s+now\s+(?:a|an|the)\b|\bahora\s+eres\s+(?:un|una)\s+(?:asistente|modelo|ia\b|inteligencia)",
    r"\bnew\s+instructions\s*:",
    r"<<\s*(?:end_)?data_|\[/?(?:inst|system)\]|<\|im_start\|>",
    r"\b(?:responde|devuelve)\s+(?:solo|únicamente|unicamente)\s+(?:con\s+)?(?:el\s+)?json",
    r"\b(?:respond|reply|answer)\s+only\s+with\b",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION), re.IGNORECASE | re.DOTALL)


def find_injection(text: str) -> str | None:
    """Return the suspicious snippet or None. Runs on raw HTML (hidden text, comments and attributes
    included) and on the user's input."""
    m = _INJECTION_RE.search(html.unescape(text or ""))
    return m.group(0)[:120] if m else None


# --- Citas ----------------------------------------------------------------------------------

_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "«": '"', "»": '"', "‘": "'", "’": "'",
                         "‚": "'", "´": "'", "`": "'", "–": "-", "—": "-", " ": " "})
MIN_QUOTE_CHARS = 20


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").translate(_QUOTES)
    return re.sub(r"\s+", " ", s).strip().casefold()


def quote_in_page(quote: str, page_text: str) -> bool:
    q = normalize(quote).strip(" .…\"'")
    return len(q) >= MIN_QUOTE_CHARS and q in normalize(page_text)


# --- Veredictos -----------------------------------------------------------------------------

REASONS = {
    "no_evidence": "No encontramos fuentes verificables que respalden esta calificación.",
    "contributed_alone": "Una fuente aportada por un lector no basta por sí sola para una calificación firme.",
    "need_two": "Para calificarlo como {label} hacen falta al menos dos fuentes independientes, de dueños "
                "distintos y sin interés en el caso, y al menos una confiable. No las encontramos.",
    "conflict": "Las fuentes no coinciden: {pro} y {con}, así que no es posible dar una calificación más firme.",
    "concentration": "Más de la mitad de las fuentes pertenece al mismo dueño ({group}) y no hay un documento "
                     "o dato primario que lo respalde, así que no es posible dar una calificación más firme.",
}


def is_lead_only(url: str) -> bool:
    """Tertiary sources (open encyclopedias, machine-written ones): a place to find sources, never one."""
    return any(_matches(host_of(url), d) for d in LEADS_ONLY)


def predates(source_date: str | None, when: str | None) -> bool:
    """True when a source was published before the moment a claim refers to ("2026-09" starts on
    2026-09-01). Such a source cannot contradict the claim: at most it describes an earlier situation.
    Unknown dates never trigger it."""
    m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", when or "")
    if not m or not source_date or not re.match(r"^\d{4}-\d{2}-\d{2}", source_date):
        return False
    start = f"{m[1]}-{m[2] or '01'}-{m[3] or '01'}"
    return source_date[:10] < start


def concentration(evidence: list[dict]) -> tuple[str, int, int] | None:
    """(group, sources from that group, total) when one owner holds more than half of the sources."""
    srcs = {e["source"]: e["group"] for e in evidence if "source" in e} or {i: e["group"] for i, e in enumerate(evidence)}
    if len(srcs) < 2:
        return None
    counts: dict[str, int] = {}
    for g in srcs.values():
        counts[g] = counts.get(g, 0) + 1
    group, n = max(counts.items(), key=lambda kv: kv[1])
    return (group, n, len(srcs)) if n * 2 > len(srcs) else None


def superseded(evidence: list[dict]) -> list[dict]:
    """News moves on: sources reporting the state before an event (Mac Master still heading the Andi on
    September 25) do not contradict reports of the event (his resignation on October 1). When every weighable
    independent source on one side is dated before what at least two independent owners on the other side
    published, the older side becomes context. A false rumour does not pass: serious outlets publish a fresh
    denial, so not all of its contradictions are older. Any undated source on the older side stops the rule."""
    def side(stance):
        return [e for e in evidence if e["stance"] == stance and not e.get("party") and e["tier"] <= 3]
    for newer, older in (("confirma", "contradice"), ("contradice", "confirma")):
        old = side(older)
        if not old or any(not re.match(r"^\d{4}-\d{2}-\d{2}", e.get("date") or "") for e in old):
            continue
        last = max(e["date"][:10] for e in old)
        if len({e["group"] for e in side(newer) if (e.get("date") or "")[:10] > last}) >= 2:
            return [e | {"stance": "contexto"} if e["stance"] == older else e for e in evidence]
    return evidence


def adjust_claim(proposed: str, evidence: list[dict]) -> tuple[str, str | None]:
    """Apply the rules to one claim. `evidence` only holds validated items (URL downloaded in this check,
    quote present on the page, no injection), each with `stance`, `domain`, `tier`, `group` (owner),
    `party` (interested party) and `primary` (primary document or data).
    Returns (final rating, adjustment reason or None)."""
    if proposed not in RATINGS:
        proposed = "sin_pruebas"
    if proposed in ("sin_pruebas", "no_verificable"):
        return proposed, None
    relevant = superseded([e | {"group": e.get("group") or e["domain"]} for e in evidence if e["stance"] != "no_relacionada"])
    if not relevant:
        return "sin_pruebas", REASONS["no_evidence"]
    # An interested party's account is shown, but it neither confirms nor refutes.
    independent = [e for e in relevant if not e.get("party")]

    def strong(stance):
        es = [e for e in independent if e["stance"] == stance]
        return len({e["group"] for e in es}) >= 2 and any(e["tier"] <= 3 for e in es)

    def groups(stance):
        return {e["group"] for e in independent if e["stance"] == stance}

    def serious(stance):
        """Disagreement weighs only when it comes from outlets we can weigh (tier 1-3) and is not a lone
        voice: two owners, an official source or fact-checker, or at least half as many owners as the other
        side. Unknown sites (tier 4) cost nothing to create, so on their own they are only a nuance."""
        es = [e for e in independent if e["stance"] == stance and e["tier"] <= 3]
        mine = {e["group"] for e in es}
        other = groups("contradice" if stance == "confirma" else "confirma")
        return bool(es) and (len(mine) >= 2 or any(e["tier"] <= 2 for e in es) or len(mine) * 2 >= len(other))

    weighty = [e for e in independent if e["tier"] <= 3]
    confirm = any(e["stance"] == "confirma" for e in weighty)
    contra = any(e["stance"] == "contradice" for e in weighty)
    conflict = REASONS["conflict"].format(pro=_n(len(groups("confirma")), "lo respalda", "lo respaldan"),
                                          con=_n(len(groups("contradice")), "lo contradice", "lo contradicen"))

    # A lone dissenting source is shown as a nuance; a serious one means we cannot tell.
    if proposed in ("verdadero", "matices") and serious("contradice"):
        return "sin_pruebas", conflict
    if proposed == "falso" and serious("confirma"):
        return "sin_pruebas", conflict
    if proposed == "verdadero" and not strong("confirma"):
        return ("enganoso" if contra else "sin_pruebas"), REASONS["need_two"].format(label="verdadero")
    if proposed == "falso" and not strong("contradice"):
        return ("enganoso" if contra else "sin_pruebas"), REASONS["need_two"].format(label="falso")
    # Concentration looks at weighable outlets only, so a pile of free blogs can neither create nor dilute it.
    if proposed in ("verdadero", "falso") and (c := concentration(weighty)) and not any(e.get("primary") for e in relevant):
        return ("enganoso" if confirm and contra else "sin_pruebas"), REASONS["concentration"].format(group=c[0])
    return proposed, None


def _n(k: int, one: str, many: str) -> str:
    return f"{k} fuente {one}" if k == 1 else f"{k} fuentes {many}"


def focus_rating(claims: list[dict]) -> str:
    """The overall rating follows the central claim, the controversial one the content is about. True side
    facts (a meeting did take place) must not make "we nearly lost democracy" come out "verdadero". Side claims
    can only make it worse, never better: when extraction marked the true detail as central ("the law bans child
    marriage") and the false part as a side claim ("De la Espriella's government signed it"), the content is
    still misleading. Without a gradable central claim, every claim counts."""
    central = [c["rating"] for c in claims if c.get("central") and c["rating"] != "no_verificable"]
    if not central:
        return overall_rating([c["rating"] for c in claims])
    worse = [c["rating"] for c in claims if not c.get("central") and c["rating"] in ("falso", "enganoso")]
    return overall_rating(central + worse)


def overall_rating(ratings: list[str]) -> str:
    """Overall rating derived from the claim ratings, without the LLM."""
    v = [r for r in ratings if r != "no_verificable"]
    if not v:
        return "no_verificable"
    if len(set(v)) == 1:
        return v[0]
    if all(r in TRUTHY for r in v):
        return "matices"
    if any(r in TRUTHY for r in v) or "enganoso" in v:
        return "enganoso"
    return "falso" if "falso" in v else "sin_pruebas"


MARK = {"verdadero": "cierto", "matices": "cierto", "enganoso": "falso", "falso": "falso",
        "sin_pruebas": "no_probado", "no_verificable": "no_probado"}
