"""Checks chosen by COntraste: every day a few of the stories moving Colombia at that moment are checked on
their own, so the site always has something new even when nobody asks.

They are spread over the day (7 a.m. to 7 p.m., Colombian time), and each one is picked from the headlines
of that moment (news.py refreshes them every 30 minutes). The fast model picks the story with the most
consequential checkable claim, and states what an actor (a government, a politician, an institution, a
company) asserts, not the outlet's report. Anything already checked, or picked earlier today, is skipped.
The check then runs like any other, with the same rules and the same bar to reach the front page, and the
article says it was chosen by COntraste, not asked for by a reader."""
import base64
import logging

from pydantic import BaseModel

from . import db, llm, settings
from .similar import SIMILAR_TEXT, cosine, embed, text_key

log = logging.getLogger("contraste.auto")
FIRST_HOUR, LAST_HOUR = 7, 19

PICK_TASK = """Tarea: de la lista numerada de titulares de hoy en Colombia, elige el que contenga la afirmación más
importante para verificar: algo que un actor (gobierno, político, entidad, empresa, figura pública) afirma o
denuncia, con impacto público y que se pueda contrastar con fuentes (cifras, hechos, decisiones, acusaciones).
Descarta deportes, farándula, loterías, clima, avisos de servicios, consejos y sucesos sin nada en disputa.
No elijas temas parecidos a los ya elegidos hoy. 'index' es el número elegido, o -1 si ninguno sirve.
'claim' es la afirmación central tal como circula, en una frase, centrada en lo que afirma el actor
(por ejemplo "El Gobierno denuncia que faltan $148.000 millones en RTVC"), no en lo que reporta el medio."""


class Pick(BaseModel):
    index: int
    claim: str


def per_day() -> int:
    """Checks chosen by COntraste per day. Editors set it in /admin (0 turns them off)."""
    return int(db.setting("auto_checks", settings.AUTO_CHECKS_PER_DAY))


def due_now(n: int, hour: float) -> int:
    """How many of today's n checks should have started by `hour` (Colombian time): evenly spread from
    FIRST_HOUR to LAST_HOUR, the first at FIRST_HOUR."""
    if n <= 0 or hour < FIRST_HOUR:
        return 0
    if n == 1:
        return 1
    step = (LAST_HOUR - FIRST_HOUR) / (n - 1)
    return min(n, int((hour - FIRST_HOUR) / step) + 1)


def _today() -> list[dict]:
    start = db.today_co().replace(hour=0, minute=0, second=0, microsecond=0)
    return db.q("SELECT input FROM jobs WHERE kind='check' AND input->>'auto' = 'true' AND created_at >= %s", start)


async def tick():
    """Called by one replica every ~10 minutes: starts the next check if one is due."""
    n, now = per_day(), db.today_co()
    if db.spend_today() >= settings.DAILY_SPEND_LIMIT_USD > 0:
        return
    done = _today()
    if len(done) >= due_now(n, now.hour + now.minute / 60):
        return
    picked = [d["input"]["text"] for d in done]
    picked_embs = [embed(t) for t in picked]
    candidates = []
    for item in (db.setting("trending", {}) or {}).get("items", []):
        e = embed(item["title"])
        # Already checked, or the same story as one picked today.
        if db.find_duplicate(text_key=text_key(item["title"]), emb=e) or any(cosine(p.tobytes(), e) >= SIMILAR_TEXT for p in picked_embs):
            continue
        candidates.append(item)
    if not candidates:
        log.info("no new headline to check")
        return
    listed = "\n".join(f"[{i}] {c['title']} ({c['source']})" for i, c in enumerate(candidates))
    if picked:
        listed += "\n\nYA ELEGIDOS HOY:\n" + "\n".join(f"- {t}" for t in picked)
    choice = await llm.ask(PICK_TASK, listed, Pick, fast=True)
    if not 0 <= choice.index < len(candidates) or len(choice.claim.strip()) < 12:
        log.info("no headline worth checking right now")
        return
    item, claim = candidates[choice.index], choice.claim.strip()[:300]
    e = embed(claim)
    if db.find_duplicate(text_key=text_key(claim), emb=e):
        return
    job_id = db.new_id()
    db.job_create(job_id, {"kind": "text", "text": claim, "auto": True, "headline": item["title"], "source": item["source"],
                           "keys": {"text_key": text_key(claim), "emb": base64.b64encode(e.tobytes()).decode()}})
    log.info("chose a check of the day: %s", claim)


if __name__ == "__main__":
    # Five a day: 7, 10, 13, 16 and 19 h.
    assert [due_now(5, h) for h in (6.9, 7, 9.9, 10, 13, 18.9, 19, 23)] == [0, 1, 1, 2, 3, 4, 5, 5]
    assert due_now(1, 8) == 1 and due_now(0, 12) == 0
    print("ok")
