"""Investigate every published check again with the current method (python -m app.reinvestigate_all).

Uses the same re-investigation as the scheduled one: an article changes only when the rating changes or new
sources appear, and every change goes to its public history. Stops before today's model spend reaches
--max-usd (default: half the daily limit), so readers keep budget for their own checks; run it again later
to finish. --dry-run only lists what it would do."""
import argparse
import asyncio
import json
import time

from . import db, settings
from .main import reinvestigate


async def run(ids: list[str], max_usd: float, concurrency: int) -> list[dict]:
    sem, out = asyncio.Semaphore(concurrency), []

    async def one(i: int, aid: str):
        async with sem:
            if db.spend_today() >= max_usd:
                out.append({"id": aid, "skipped": "budget"})
                return
            before = db.get(aid)
            t0 = time.monotonic()
            await reinvestigate(aid)
            after = db.get(aid)
            n_old, n_new = (len(json.loads(r["result"])["sources"]) for r in (before, after))
            row = {"id": aid, "title": after["title"][:70], "before": before["rating"], "after": after["rating"],
                   "sources": f"{n_old}→{n_new}", "changed": after["updated_at"] != before["updated_at"],
                   "secs": round(time.monotonic() - t0)}
            out.append(row)
            print(f"[{len(out)}/{len(ids)}] {aid} {row['before']}→{row['after']} fuentes {row['sources']} "
                  f"{'ACTUALIZADO' if row['changed'] else 'sin cambios'} ({row['secs']} s) · gasto hoy "
                  f"US${db.spend_today():.2f}", flush=True)

    await asyncio.gather(*(one(i, aid) for i, aid in enumerate(ids)))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-usd", type=float, default=settings.DAILY_SPEND_LIMIT_USD / 2)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--skip-since", help="ISO time: skip checks made or re-investigated after it (already on current rules)")
    a = p.parse_args()
    t = a.skip_since
    ids = [r["id"] for r in db.q("""SELECT id FROM articles a WHERE status != 'removed' AND NOT demo
                                    AND (%s::timestamptz IS NULL OR NOT (a.created_at::timestamptz >= %s::timestamptz
                                         OR EXISTS (SELECT 1 FROM followups f WHERE f.article_id = a.id AND f.at >= %s::timestamptz)))
                                    ORDER BY created_at""", t, t, t)]
    print(f"{len(ids)} verificaciones · gasto hoy US${db.spend_today():.2f} · se detiene en US${a.max_usd:.2f}", flush=True)
    if a.dry_run:
        return
    res = asyncio.run(run(ids, a.max_usd, a.concurrency))
    changed = [r for r in res if r.get("before") and r["before"] != r["after"]]
    print(f"\nListo: {sum(1 for r in res if r.get('changed'))} actualizadas, {len(changed)} con calificación distinta, "
          f"{sum(1 for r in res if r.get('skipped'))} pendientes por presupuesto · gasto hoy US${db.spend_today():.2f}")
    for r in changed:
        print(f"  {r['id']} {r['before']} → {r['after']}: {r['title']}")


if __name__ == "__main__":
    main()
