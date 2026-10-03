"""One-time move from the old SQLite file to Postgres. Runs at startup and does nothing unless the SQLite
file exists and Postgres has no articles yet. The old file is kept, renamed, as a backup."""
import json
import logging
import sqlite3

from . import db
from .settings import DATA_DIR

log = logging.getLogger("contraste.migrate")
SQLITE = DATA_DIR / "contraste.db"
MEDIA = DATA_DIR / "media"


def migrate() -> int:
    if not SQLITE.exists():
        return 0
    with db.singleton(3) as mine:
        if not mine or db.q1("SELECT 1 FROM articles LIMIT 1"):
            return 0
        src = sqlite3.connect(SQLITE)
        src.row_factory = sqlite3.Row
        articles = src.execute("SELECT * FROM articles").fetchall()
        with db.pool.connection() as c, c.transaction():
            for r in articles:
                c.execute("""INSERT INTO articles(id, slug, created_at, updated_at, status, unlisted_reason, rating, title,
                                 topic, input_kind, url_key, text_key, content_hash, phash, emb, emb2, result, score, demo,
                                 search)
                             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, to_tsvector('spanish', %s))""",
                          (r["id"], r["slug"], r["created_at"], r["updated_at"], r["status"], r["unlisted_reason"],
                           r["rating"], r["title"], r["topic"], r["input_kind"], r["url_key"], r["text_key"],
                           r["content_hash"], r["phash"], r["emb"], r["emb2"], r["result"], r["score"], bool(r["demo"]),
                           db.search_text(json.loads(r["result"]))))
            for table, cols in (("consultations", 2), ("changes", 6), ("article_views_hourly", 3), ("view_rejected", 4)):
                rows = [tuple(x) for x in src.execute(f"SELECT * FROM {table}")]
                if rows:
                    c.cursor().executemany(f"INSERT INTO {table} VALUES({','.join(['%s'] * cols)}) ON CONFLICT DO NOTHING", rows)
        src.close()
        for f in MEDIA.glob("*.jpg") if MEDIA.exists() else []:
            db.media_put(f.name, f.read_bytes())
        SQLITE.rename(SQLITE.with_suffix(".db.migrated"))
        log.info("moved %d articles from SQLite to Postgres; old file kept as %s", len(articles),
                 SQLITE.with_suffix(".db.migrated").name)
        return len(articles)
