"""Source memory. Pages from outlets we can weigh (tiers 1-3) read in any check are kept as paragraphs
with their embeddings. Later checks recall the paragraphs closest to their claims, which finds evidence
a search engine no longer returns (older reports, PDFs).

What is recalled is only a lead: the page is downloaded again and goes through every rule like a search
result, so an outdated or edited page can never slip in, and our own articles are never used as evidence.

Retrieval is hybrid so it scales without extra services: Postgres full-text search picks candidates, the
local embedding model reranks them.
ponytail: rerank of 200 candidates in Python; move to pgvector when the store passes a few million paragraphs."""
import asyncio
import logging
import re

from . import db
from .fetch import _words
from .search import canonical
from .similar import cosine, embed

log = logging.getLogger("contraste.memory")

CHUNK = 400          # characters per piece: small enough that one fact is not diluted by its neighbours
MAX_CHUNKS = 30      # per page
MAX_PAGES = 12       # per check
MIN_SCORE = 0.5      # cosine between a claim and a paragraph to count as a lead
PER_CLAIM, TOTAL = 2, 5
BACKGROUND = True    # tests store synchronously


def chunks(text: str) -> list[str]:
    """One piece per paragraph, so a fact keeps its own vector. Very short lines (headings) join the next
    paragraph; long paragraphs are split into groups of sentences (the model reads ~256 tokens)."""
    out, carry = [], ""
    for para in re.split(r"\n+", text):
        para = f"{carry} {para.strip()}".strip()
        if len(para) < 60:
            carry = para
            continue
        carry, cur = "", ""
        for sentence in (re.split(r"(?<=[.!?])\s+", para) if len(para) > CHUNK else [para]):
            if cur and len(cur) + len(sentence) > CHUNK:
                out.append(cur)
                cur = ""
            cur = f"{cur} {sentence}".strip()
        out.append(cur)
    return [c for c in out if len(c) >= 60][:MAX_CHUNKS]


def remember(pages: list[dict]):
    """Store (or refresh) the paragraphs of the weighable pages read in a check."""
    kept = [p for p in pages if p and p.get("tier", 4) <= 3 and p.get("kind") in ("web", "documento")
            and len(p.get("text", "")) >= 200][:MAX_PAGES]
    for p in kept:
        parts = chunks(p["text"])
        if not parts:
            continue
        rows = [(canonical(p["url"]), p["url"], p["tier"], (p.get("title") or "")[:300], c, embed(c).tobytes(),
                 " ".join(_words(p.get("title", "") + " " + c))) for c in parts]
        with db.pool.connection() as c, c.transaction():
            c.execute("DELETE FROM passages WHERE url_key=%s", (rows[0][0],))
            c.cursor().executemany("""INSERT INTO passages(url_key, url, tier, title, text, emb, search)
                                      VALUES(%s,%s,%s,%s,%s,%s, to_tsvector('simple', %s))""", rows)


def remember_later(pages: list[dict]):
    if not BACKGROUND:
        return remember(pages)
    task = asyncio.get_running_loop().create_task(asyncio.to_thread(_safe_remember, pages))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


_tasks: set = set()


def _safe_remember(pages):
    try:
        remember(pages)
    except Exception:
        log.exception("could not store source memory")


def recall(claims: list[str], skip: set[str]) -> list[list[dict]]:
    """For each claim, the best pages from memory (not in `skip`, canonical URLs) as search-like results."""
    out, used = [], set(skip)
    for text in claims:
        words = sorted(_words(text), key=len, reverse=True)[:12]
        hits = []
        if words:
            rows = db.q("""SELECT url_key, url, title, emb FROM passages, to_tsquery('simple', %s) query
                           WHERE search @@ query ORDER BY ts_rank(search, query) DESC LIMIT 200""", " | ".join(words))
            if rows:
                v = embed(text)
                best: dict[str, tuple[float, dict]] = {}
                for r in rows:
                    s = cosine(r["emb"], v)
                    if s >= MIN_SCORE and r["url_key"] not in used and s > best.get(r["url_key"], (0,))[0]:
                        best[r["url_key"]] = (s, {"url": r["url"], "title": r["title"], "memory": True})
                for key, (_, hit) in sorted(best.items(), key=lambda kv: -kv[1][0])[:PER_CLAIM]:
                    if len(used) - len(skip) < TOTAL:
                        hits.append(hit)
                        used.add(key)
        out.append(hits)
    return out


if __name__ == "__main__":
    parts = chunks("Primer párrafo corto.\n\n" + ("Una oración con suficiente contenido para contar. " * 30))
    assert parts and all(60 <= len(p) <= CHUNK + 200 for p in parts), [len(p) for p in parts]
    print(len(parts), "chunks ok")
