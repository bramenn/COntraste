"""Re-apply the current verdict rules to stored articles and move them into the current sections.

Run inside the container:  python -m app.recompute            (apply)
                           python -m app.recompute --dry-run  (only report)
"""
import asyncio
import json
import sys

from . import cards, db, llm
from .pipeline import ADJUSTED_LINE
from .rules import RATINGS, adjust_claim, focus_rating, source_group

NOTE = "Se recalculó con la regla de desacuerdo proporcional: una fuente aislada en contra ya no baja la calificación."
TOPIC_TASK = "Tarea: di la sección periodística principal de este caso verificado."


class TopicOnly(llm.BaseModel):
    topic: llm.Topic


def rescore(r: dict) -> list[str]:
    """Update the claim and overall ratings in place. Returns a readable list of rating changes."""
    sources = r.get("sources", [])
    changed = []
    for c in r.get("claims", []):
        if not c.get("proposed"):
            continue
        ev = []
        for e in c.get("evidence", []):
            s = sources[e["source"]]
            ev.append({"source": e["source"], "stance": e["stance"], "domain": s["domain"], "tier": s["tier"], "date": s.get("date"),
                       "group": source_group(s["domain"]), "party": e.get("party", False),
                       "primary": s.get("kind") == "datos"
                       or (s["tier"] == 1 and s.get("basis") in ("dato_oficial", "documento"))})
        rating, reason = adjust_claim(c["proposed"], ev)
        # Older checks replaced the model's finding with the rule's reason; recover the finding if we still have it.
        if c.get("adjusted") and c.get("explanation") == c["adjusted"] and c.get("sources_say"):
            c["explanation"] = c["sources_say"]
        if rating != c["rating"]:
            changed.append(f"«{c['short']}»: {RATINGS[c['rating']]} → {RATINGS[rating]}")
        if rating != c["rating"] or reason != c.get("adjusted"):
            c["rating"], c["adjusted"] = rating, reason
            c["card_line"] = ADJUSTED_LINE.get(rating, "{}").format(c["short"]) if reason else c["short"]
    if r.get("claims"):
        overall = focus_rating(r["claims"])
        if changed or overall != r["rating"]:
            r["rating"] = overall
            r["headline"] = " ".join(f"{c['short'].rstrip('.')}: {RATINGS[c['rating']].lower()}." for c in r["claims"])
    return changed


async def retopic(r: dict) -> str:
    data = "\n".join([r["title"], r["circulating"]] + [c["text"] for c in r.get("claims", [])])
    return (await llm.ask(TOPIC_TASK, data, TopicOnly, fast=True)).topic


async def main(dry_run: bool):
    rows = db.q("SELECT * FROM articles WHERE NOT demo AND status != 'removed' ORDER BY created_at")
    for row in rows:
        r = json.loads(row["result"])
        before = json.dumps(r, sort_keys=True)
        old_rating, old_topic = r["rating"], r["topic"]
        changed = rescore(r)
        try:
            r["topic"] = await retopic(r)
        except llm.LLMError as e:
            print(f"{row['id']}: section not updated ({e})")
        print(f"{row['id']} | {old_rating} -> {r['rating']} | {old_topic} -> {r['topic']} | {'; '.join(changed) or '-'}")
        if dry_run or json.dumps(r, sort_keys=True) == before:
            continue
        if r["rating"] != old_rating or changed:
            if r["rating"] != old_rating:  # stamp before saving: saving changes the thumbnail's URL version
                await cards.restamp(row["id"], r["rating"])
            db.update_article(row["id"], r, old_rating=old_rating, change=("actualizacion", f"{NOTE} {'; '.join(changed)}".strip()))
        else:
            db.update_article(row["id"], r)
    await cards.close()


if __name__ == "__main__":
    asyncio.run(main("--dry-run" in sys.argv))
