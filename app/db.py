"""PostgreSQL storage: articles, consultations, hourly views, change history, full-text search, the job
queue and everything replicas must share (rate limits, admin sessions, images). Nothing lives in a
container's memory or local disk, so the app can run as several replicas."""
import hashlib
import json
import math
import os
import re
import secrets
import socket
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .settings import DATABASE_URL

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=20, open=True,
                      # prepare_threshold=None: no server-side prepared statements, so pgbouncer in
                      # transaction mode can route every query to any pooled connection safely.
                      kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None})

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles(
  id TEXT PRIMARY KEY, slug TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  status TEXT NOT NULL,              -- listed | unlisted | removed
  unlisted_reason TEXT, rating TEXT NOT NULL, title TEXT NOT NULL, topic TEXT NOT NULL,
  input_kind TEXT NOT NULL, url_key TEXT, text_key TEXT, content_hash TEXT, phash TEXT,
  emb BYTEA, emb2 BYTEA, result TEXT NOT NULL, score DOUBLE PRECISION NOT NULL DEFAULT 0,
  demo BOOLEAN NOT NULL DEFAULT FALSE, reinvestigating BOOLEAN NOT NULL DEFAULT FALSE,
  search TSVECTOR);
CREATE INDEX IF NOT EXISTS articles_url ON articles(url_key);
CREATE INDEX IF NOT EXISTS articles_text ON articles(text_key);
CREATE INDEX IF NOT EXISTS articles_hash ON articles(content_hash);
CREATE INDEX IF NOT EXISTS articles_listed ON articles(status, created_at);
CREATE INDEX IF NOT EXISTS articles_search ON articles USING GIN(search);
CREATE TABLE IF NOT EXISTS consultations(article_id TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS consultations_article ON consultations(article_id, at);
CREATE TABLE IF NOT EXISTS consult_seen(day TEXT NOT NULL, visitor TEXT NOT NULL, article_id TEXT NOT NULL,
  PRIMARY KEY (day, visitor, article_id));
CREATE TABLE IF NOT EXISTS changes(article_id TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL,
  note TEXT NOT NULL, old_rating TEXT, new_rating TEXT);
CREATE TABLE IF NOT EXISTS article_views_hourly(article_id TEXT NOT NULL, hour TEXT NOT NULL,
  views INTEGER NOT NULL, PRIMARY KEY(article_id, hour));
CREATE TABLE IF NOT EXISTS view_seen(day TEXT NOT NULL, visitor TEXT NOT NULL, article_id TEXT NOT NULL,
  PRIMARY KEY(day, visitor, article_id));
CREATE TABLE IF NOT EXISTS view_rejected(at TEXT NOT NULL, article_id TEXT NOT NULL, reason TEXT NOT NULL, visitor TEXT);
CREATE TABLE IF NOT EXISTS salts(day TEXT PRIMARY KEY, salt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, status TEXT NOT NULL,          -- queued | running | done | error
  input JSONB NOT NULL, image BYTEA, events JSONB NOT NULL DEFAULT '[]', article_id TEXT, error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, worker TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), heartbeat_at TIMESTAMPTZ, finished_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS rate_hits(key TEXT NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS rate_hits_key ON rate_hits(key, at);
CREATE TABLE IF NOT EXISTS admin_sessions(token TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS media(name TEXT PRIMARY KEY, data BYTEA NOT NULL, mime TEXT NOT NULL);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'check';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS user_id TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, created_at);

-- Accounts: only what Ley 1581 allows us to keep (email, sign-up date, balance and movements).
CREATE TABLE IF NOT EXISTS users(
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  email_verified BOOLEAN NOT NULL DEFAULT FALSE, consent_at TIMESTAMPTZ,
  disposable BOOLEAN NOT NULL DEFAULT FALSE, blocked BOOLEAN NOT NULL DEFAULT FALSE);
CREATE TABLE IF NOT EXISTS user_sessions(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, csrf TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS login_tokens(token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, next TEXT,
  consent BOOLEAN NOT NULL DEFAULT FALSE, expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ);

-- Credits: append-only ledger; balances are always computed from it.
CREATE TABLE IF NOT EXISTS credit_ledger(
  id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('monthly_grant','spend','refund','reward','admin_adjust')),
  bucket TEXT NOT NULL CHECK (bucket IN ('free','extra')), month TEXT, amount INTEGER NOT NULL,
  ref TEXT, note TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS ledger_user ON credit_ledger(user_id, bucket, month);
DROP INDEX IF EXISTS ledger_monthly;  -- the monthly grant can be topped up when an editor raises it
CREATE UNIQUE INDEX IF NOT EXISTS ledger_once ON credit_ledger(kind, ref)
  WHERE kind IN ('refund','reward') AND ref IS NOT NULL;
CREATE OR REPLACE FUNCTION ledger_append_only() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'credit_ledger is append-only'; END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS ledger_no_change ON credit_ledger;
CREATE TRIGGER ledger_no_change BEFORE UPDATE OR DELETE ON credit_ledger
  FOR EACH ROW EXECUTE FUNCTION ledger_append_only();

CREATE TABLE IF NOT EXISTS contributions(
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, article_id TEXT NOT NULL, claim_index INTEGER NOT NULL,
  urls JSONB NOT NULL, note TEXT NOT NULL,
  status TEXT NOT NULL,              -- queued | frozen | review | accepted | rejected
  outcome TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), decided_at TIMESTAMPTZ);
ALTER TABLE contributions ADD COLUMN IF NOT EXISTS pending JSONB;  -- result waiting for an editor
-- Cross-validation: evidence found while checking a related topic. No user; origin = the article that found it.
ALTER TABLE contributions ADD COLUMN IF NOT EXISTS origin TEXT;
ALTER TABLE contributions ALTER COLUMN user_id DROP NOT NULL;
CREATE INDEX IF NOT EXISTS contributions_article ON contributions(article_id, created_at);
CREATE INDEX IF NOT EXISTS contributions_user ON contributions(user_id, created_at);

CREATE TABLE IF NOT EXISTS llm_spend(day TEXT PRIMARY KEY, usd DOUBLE PRECISION NOT NULL DEFAULT 0);

-- Source memory: paragraphs of weighable pages read before (see memory.py).
CREATE TABLE IF NOT EXISTS passages(id BIGSERIAL PRIMARY KEY, url_key TEXT NOT NULL, url TEXT NOT NULL,
  tier INTEGER NOT NULL, title TEXT, text TEXT NOT NULL, emb BYTEA NOT NULL, search TSVECTOR NOT NULL,
  read_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS passages_search ON passages USING GIN(search);
CREATE INDEX IF NOT EXISTS passages_url ON passages(url_key);

ALTER TABLE articles ADD COLUMN IF NOT EXISTS human_reviewed_at TEXT;  -- an editor read and approved it

-- Right of reply: people or organisations a check is about can answer. Contact data is erased once handled.
CREATE TABLE IF NOT EXISTS replies(id BIGSERIAL PRIMARY KEY, article_id TEXT NOT NULL, name TEXT NOT NULL,
  email TEXT NOT NULL, message TEXT NOT NULL, url TEXT, status TEXT NOT NULL DEFAULT 'new',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());

-- Leases: which process runs a once-per-cluster task (front-page scores, markets, card warm-up).
CREATE TABLE IF NOT EXISTS leases(name TEXT PRIMARY KEY, holder TEXT NOT NULL, until TIMESTAMPTZ NOT NULL);

-- Values an editor changes from /admin without a restart.
CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY, value JSONB NOT NULL);

-- One answer per account. On account deletion user_id becomes NULL and the answer stays anonymous.
CREATE TABLE IF NOT EXISTS surveys(id BIGSERIAL PRIMARY KEY, user_id TEXT UNIQUE, answers JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
"""


def _init():
    with pool.connection() as c, c.transaction():
        c.execute("SELECT pg_advisory_xact_lock(4242)")  # several replicas may start at once
        c.execute(SCHEMA)


_init()


def q(sql: str, *args) -> list[dict]:
    with pool.connection() as c:
        cur = c.execute(sql, args or None)
        return cur.fetchall() if cur.description else []


def q1(sql: str, *args):
    rows = q(sql, *args)
    return rows[0] if rows else None


# Who holds a lease: this process. Leases are rows, not session advisory locks, so they work through
# pgbouncer in transaction mode (a session lock taken on one server connection would be released on
# another and leak).
HOLDER = f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"


@contextmanager
def singleton(lock_id: int, ttl_s: int = 900):
    """Yields True in exactly one process at a time, False elsewhere. If the holder dies, the lease
    expires after ttl_s and another process can take it."""
    name = f"singleton:{lock_id}"
    got = q1("""INSERT INTO leases VALUES(%s, %s, now() + make_interval(secs => %s))
                ON CONFLICT (name) DO UPDATE SET holder=EXCLUDED.holder, until=EXCLUDED.until
                WHERE leases.until < now() OR leases.holder = EXCLUDED.holder
                RETURNING holder""", name, HOLDER, ttl_s) is not None
    try:
        yield got
    finally:
        if got:
            q("DELETE FROM leases WHERE name=%s AND holder=%s", name, HOLDER)


def now() -> datetime:
    return datetime.now(timezone.utc)


def today_co() -> datetime:
    """Colombian time (UTC-5, no daylight saving): free credits renew and the spend limit resets at local midnight."""
    return datetime.now(timezone(timedelta(hours=-5)))


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def slugify(text: str, max_words: int = 7) -> str:
    t = unicodedata.normalize("NFKD", text.casefold().replace("[ejemplo]", "ejemplo"))
    t = re.sub(r"[^a-z0-9]+", " ", "".join(c for c in t if not unicodedata.combining(c)))
    stop = {"de", "la", "el", "los", "las", "y", "a", "en", "que", "por", "un", "una", "su", "del", "al", "con", "es", "se"}
    words = [w for w in t.split() if w not in stop][:max_words]
    return "-".join(words) or "verificacion"


def new_id() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    while True:
        i = "".join(secrets.choice(alphabet) for _ in range(6))
        if not q1("SELECT 1 FROM articles WHERE id=%s UNION SELECT 1 FROM jobs WHERE id=%s", i, i):
            return i


def path_of(row) -> str:
    d = datetime.fromisoformat(row["created_at"])
    return f"/v/{d.year}/{d.month:02d}/{row['slug']}-{row['id']}"


def _plain(text: str) -> str:
    """Accent-free text for the Spanish full-text index (so "inflacion" finds "inflación")."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def search_text(result: dict) -> str:
    parts = [result.get("title", ""), result.get("circulating", ""), result.get("headline", "")]
    parts += [c.get("text", "") + " " + c.get("explanation", "") for c in result.get("claims", [])]
    return _plain(" ".join(parts))


def save_article(result: dict, *, status: str, reason: str | None, keys: dict, demo: bool = False,
                 article_id: str | None = None, created_at: str | None = None) -> str:
    aid = article_id or new_id()
    ts = created_at or iso()
    q("""INSERT INTO articles(id, slug, created_at, updated_at, status, unlisted_reason, rating, title, topic,
                              input_kind, url_key, text_key, content_hash, phash, emb, emb2, result, demo, search)
         VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, to_tsvector('spanish', %s))""",
      aid, slugify(result["title"]), ts, ts, status, reason, result["rating"], result["title"], result["topic"],
      result["input"]["kind"], keys.get("url_key"), keys.get("text_key"), keys.get("content_hash"), keys.get("phash"),
      keys.get("emb"), keys.get("emb2"), json.dumps(result, ensure_ascii=False), demo, search_text(result))
    return aid


def update_article(aid: str, result: dict, *, status: str | None = None, reason: str | None = None,
                   change: tuple[str, str] | None = None, old_rating: str | None = None):
    """Replace the stored result and record the public change entry (kind, note)."""
    with pool.connection() as c, c.transaction():
        c.execute("""UPDATE articles SET result=%s, rating=%s, title=%s, topic=%s, updated_at=%s,
                        status=COALESCE(%s, status),
                        unlisted_reason=CASE WHEN %s::text IS NULL THEN unlisted_reason ELSE %s END,
                        search=to_tsvector('spanish', %s)
                     WHERE id=%s""",
                  (json.dumps(result, ensure_ascii=False), result["rating"], result["title"], result["topic"], iso(),
                   status, status, reason, search_text(result), aid))
        if change:
            c.execute("INSERT INTO changes VALUES(%s,%s,%s,%s,%s,%s)",
                      (aid, iso(), change[0], change[1], old_rating, result["rating"]))


def get(aid: str):
    return q1("SELECT * FROM articles WHERE id=%s", aid)


def record_consultation(aid: str, visitor: str | None = None):
    """visitor: daily-salted hash of who asked. The same person asking again the same day counts once,
    so "más consultado" cannot be pushed by resubmitting."""
    if visitor and q1("INSERT INTO consult_seen VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1",
                      now().date().isoformat(), visitor, aid) is None:
        return
    q("INSERT INTO consultations VALUES(%s,%s)", aid, iso())


def consult_count(aid: str) -> int:
    return q1("SELECT COUNT(*) AS n FROM consultations WHERE article_id=%s", aid)["n"]


def view_count(aid: str) -> int:
    return q1("SELECT COALESCE(SUM(views),0) AS n FROM article_views_hourly WHERE article_id=%s", aid)["n"]


# --- Deduplication ---------------------------------------------------------------------------

def find_duplicate(*, url_key=None, text_key=None, content_hash=None, phash=None, emb=None) -> str | None:
    from .similar import SIMILAR_IMAGE_BITS, SIMILAR_TEXT, cosine, hamming
    live = "status != 'removed'"
    for col, val in (("url_key", url_key), ("text_key", text_key), ("content_hash", content_hash)):
        if val and (r := q1(f"SELECT id FROM articles WHERE {col}=%s AND {live} ORDER BY created_at DESC LIMIT 1", val)):
            return r["id"]
    if phash:
        for r in q(f"SELECT id, phash FROM articles WHERE phash IS NOT NULL AND {live}"):
            if hamming(int(r["phash"], 16), int(phash, 16)) <= SIMILAR_IMAGE_BITS:
                return r["id"]
    if emb is not None:
        # Linear scan in Python. Fine for thousands of articles; move to pgvector beyond that.
        best, best_id = 0.0, None
        for r in q(f"SELECT id, emb, emb2 FROM articles WHERE (emb IS NOT NULL OR emb2 IS NOT NULL) AND {live}"):
            s = max(cosine(b, emb) for b in (r["emb"], r["emb2"]) if b)
            if s > best:
                best, best_id = s, r["id"]
        if best >= SIMILAR_TEXT:
            return best_id
    return None


# --- Views (no raw IPs or user agents are stored) -----------------------------------------

def daily_salt() -> str:
    day = now().date().isoformat()
    row = q1("SELECT salt FROM salts WHERE day=%s", day)
    if row:
        return row["salt"]
    with pool.connection() as c, c.transaction():
        c.execute("INSERT INTO salts VALUES(%s,%s) ON CONFLICT (day) DO NOTHING", (day, secrets.token_hex(16)))
        c.execute("DELETE FROM salts WHERE day < %s", (day,))
        c.execute("DELETE FROM view_seen WHERE day < %s", (day,))
    return q1("SELECT salt FROM salts WHERE day=%s", day)["salt"]


def visitor_hash(ip: str, ua: str, aid: str) -> str:
    return hashlib.sha256(f"{ip}|{ua}|{aid}|{daily_salt()}".encode()).hexdigest()


def count_view(aid: str, visitor: str) -> bool:
    day, hour = now().date().isoformat(), now().strftime("%Y-%m-%dT%H")
    with pool.connection() as c, c.transaction():
        if c.execute("INSERT INTO view_seen VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (day, visitor, aid)).rowcount == 0:
            return False
        c.execute("""INSERT INTO article_views_hourly VALUES(%s,%s,1)
                     ON CONFLICT (article_id, hour) DO UPDATE SET views = article_views_hourly.views + 1""", (aid, hour))
    return True


def reject_view(aid: str, reason: str, visitor: str | None):
    q("INSERT INTO view_rejected VALUES(%s,%s,%s,%s)", iso(), aid, reason, visitor)


# --- Rate limits, admin sessions and images shared by every replica ---------------------------

def rate_limited(key: str, n: int, window_s: int) -> bool:
    """Sliding window over the last `window_s` seconds; records the hit when it is allowed."""
    with pool.connection() as c, c.transaction():
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
        c.execute("DELETE FROM rate_hits WHERE key=%s AND at < now() - make_interval(secs => %s)", (key, window_s))
        if c.execute("SELECT COUNT(*) AS n FROM rate_hits WHERE key=%s", (key,)).fetchone()["n"] >= n:
            return True
        c.execute("INSERT INTO rate_hits(key) VALUES(%s)", (key,))
    return False


def session_create(ttl_s: int) -> tuple[str, str]:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
    q("INSERT INTO admin_sessions VALUES(%s,%s, now() + make_interval(secs => %s))", token, csrf, ttl_s)
    q("DELETE FROM admin_sessions WHERE expires_at < now()")
    return token, csrf


def session_csrf(token: str) -> str | None:
    row = q1("SELECT csrf FROM admin_sessions WHERE token=%s AND expires_at > now()", token)
    return row["csrf"] if row else None


def session_delete(token: str):
    q("DELETE FROM admin_sessions WHERE token=%s", token)


def media_put(name: str, data: bytes, mime: str = "image/jpeg"):
    q("INSERT INTO media VALUES(%s,%s,%s) ON CONFLICT (name) DO UPDATE SET data=EXCLUDED.data, mime=EXCLUDED.mime",
      name, data, mime)


def media_get(name: str) -> dict | None:
    return q1("SELECT data, mime FROM media WHERE name=%s", name)


# --- Job queue ------------------------------------------------------------------------------

def job_create(job_id: str, inp: dict, image: bytes | None = None, *, user_id: str | None = None, kind: str = "check"):
    q("INSERT INTO jobs(id, status, input, image, user_id, kind) VALUES(%s, 'queued', %s, %s, %s, %s)",
      job_id, Jsonb(inp), image, user_id, kind)


def job_claim(worker: str) -> dict | None:
    """Take the oldest queued job. SKIP LOCKED lets every replica claim at the same time safely."""
    return q1("""UPDATE jobs SET status='running', worker=%s, attempts=attempts+1, heartbeat_at=now()
                 WHERE id = (SELECT id FROM jobs WHERE status='queued' ORDER BY created_at
                             FOR UPDATE SKIP LOCKED LIMIT 1)
                 RETURNING *""", worker)


def job_event(job_id: str, ev: dict):
    q("UPDATE jobs SET events = events || %s, heartbeat_at=now() WHERE id=%s", Jsonb([ev]), job_id)


def job_finish(job_id: str, ev: dict, article_id: str | None = None, error: str | None = None):
    q("""UPDATE jobs SET status=%s, events = events || %s, article_id=%s, error=%s, finished_at=now(), image=NULL
         WHERE id=%s""", "error" if error else "done", Jsonb([ev]), article_id, error, job_id)


def job_heartbeat(job_ids: list[str]):
    if job_ids:
        q("UPDATE jobs SET heartbeat_at=now() WHERE id = ANY(%s)", job_ids)


def job_requeue_stale(stale_s: int = 90, max_attempts: int = 3) -> int:
    """Jobs whose replica died (no heartbeat) go back to the queue; after max_attempts they fail."""
    q("""UPDATE jobs SET status='error', finished_at=now(), error='Se interrumpió varias veces.',
             events = events || %s
         WHERE status='running' AND heartbeat_at < now() - make_interval(secs => %s) AND attempts >= %s""",
      Jsonb([{"type": "error", "message": "La verificación se interrumpió varias veces. Envíala de nuevo."}]),
      stale_s, max_attempts)
    return len(q("""UPDATE jobs SET status='queued', worker=NULL, events='[]'
                    WHERE status='running' AND heartbeat_at < now() - make_interval(secs => %s) RETURNING id""",
                 stale_s))


def job_get(job_id: str) -> dict | None:
    return q1("SELECT id, status, events, article_id, error, created_at, user_id, kind FROM jobs WHERE id=%s", job_id)


# --- Editor settings ----------------------------------------------------------------------------

def setting(key: str, default):
    row = q1("SELECT value FROM app_settings WHERE key=%s", key)
    return row["value"] if row else default


def set_setting(key: str, value):
    q("INSERT INTO app_settings VALUES(%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", key, Jsonb(value))


# --- Model spend (cost protection) ------------------------------------------------------------

def add_spend(usd: float, job_id: str | None = None):
    q("""INSERT INTO llm_spend VALUES(%s,%s) ON CONFLICT (day) DO UPDATE SET usd = llm_spend.usd + EXCLUDED.usd""",
      today_co().date().isoformat(), usd)
    if job_id:
        q("UPDATE jobs SET cost_usd = cost_usd + %s WHERE id=%s", usd, job_id)


def spend_today() -> float:
    row = q1("SELECT usd FROM llm_spend WHERE day=%s", today_co().date().isoformat())
    return float(row["usd"]) if row else 0.0


def job_position(job_id: str) -> int:
    """1-based place in the queue."""
    return q1("""SELECT COUNT(*) + 1 AS n FROM jobs WHERE status='queued'
                 AND created_at < (SELECT created_at FROM jobs WHERE id=%s)""", job_id)["n"]


def jobs_cleanup(days: int = 7):
    # Anonymous and system jobs go away; an account's jobs are its history in /cuenta, so only their
    # progress events and uploaded image are dropped. Deleting the account leaves them without an owner.
    q("""DELETE FROM jobs WHERE status IN ('done', 'error') AND user_id IS NULL
         AND finished_at < now() - make_interval(days => %s)""", days)
    q("""UPDATE jobs SET events='[]', image=NULL WHERE status IN ('done', 'error') AND events <> '[]'
         AND finished_at < now() - make_interval(days => %s)""", days)
    q("DELETE FROM rate_hits WHERE at < now() - interval '1 day'")
    q("DELETE FROM view_rejected WHERE at < %s", iso(now() - timedelta(days=days)))
    q("DELETE FROM consult_seen WHERE day < %s", now().date().isoformat())


# --- Front page and ranking ----------------------------------------------------------------

HALF_LIFE_H = 18


def recompute_scores():
    """score = (consultations_24h × 3 + views_24h) × 0.5^(age_h / 18) × quality"""
    since = iso(now() - timedelta(hours=24))
    since_h = (now() - timedelta(hours=24)).strftime("%Y-%m-%dT%H")
    rows = q("""SELECT a.id, a.created_at, a.result,
        (SELECT COUNT(*) FROM consultations c WHERE c.article_id=a.id AND c.at>=%s) AS c24,
        (SELECT COALESCE(SUM(views),0) FROM article_views_hourly v WHERE v.article_id=a.id AND v.hour>=%s) AS v24
        FROM articles a WHERE a.status='listed'""", since, since_h)
    for r in rows:
        age_h = (now() - datetime.fromisoformat(r["created_at"])).total_seconds() / 3600
        tiers = {s.get("tier", 4) for s in json.loads(r["result"]).get("sources", [])}
        quality = 1.0 if tiers & {1, 2} else 0.7
        score = (r["c24"] * 3 + r["v24"]) * math.pow(0.5, age_h / HALF_LIFE_H) * quality
        q("UPDATE articles SET score=%s WHERE id=%s", score, r["id"])


def listed(order: str = "recent", limit: int = 10, rating: str | None = None, topic: str | None = None,
           search: str | None = None, offset: int = 0):
    since = iso(now() - timedelta(hours=24))
    since_h = (now() - timedelta(hours=24)).strftime("%Y-%m-%dT%H")
    where, args = ["a.status='listed'"], []
    if rating:
        where.append("a.rating=%s"); args.append(rating)
    if topic:
        where.append("a.topic=%s"); args.append(topic)
    if search:
        where.append("a.search @@ websearch_to_tsquery('spanish', %s)"); args.append(_plain(search))
    orders = {"recent": "a.created_at DESC",
              "score": "a.score DESC, a.created_at DESC",
              "consulted": "c24 DESC, a.created_at DESC",
              "viewed": "v24 DESC, a.created_at DESC"}
    having = {"consulted": "WHERE c24 > 0", "viewed": "WHERE v24 > 0"}.get(order, "")
    return q(f"""SELECT * FROM (SELECT a.*,
        (SELECT COUNT(*) FROM consultations c WHERE c.article_id=a.id AND c.at>=%s) AS c24,
        (SELECT COALESCE(SUM(views),0) FROM article_views_hourly v WHERE v.article_id=a.id AND v.hour>=%s) AS v24
        FROM articles a WHERE {' AND '.join(where)}) a {having} ORDER BY {orders[order]} LIMIT %s OFFSET %s""",
             since, since_h, *args, limit, offset)


def related(row, limit: int = 4):
    words = re.findall(r"\w{5,}", _plain(row["title"]))[:6]
    rows = []
    if words:
        rows = q("""SELECT a.* FROM articles a, websearch_to_tsquery('spanish', %s) query
                    WHERE a.search @@ query AND a.status='listed' AND a.id != %s
                    ORDER BY ts_rank(a.search, query) DESC LIMIT %s""", " or ".join(words), row["id"], limit)
    if len(rows) < limit:
        seen = {r["id"] for r in rows} | {row["id"]}
        rows += [r for r in q("SELECT * FROM articles WHERE status='listed' AND topic=%s ORDER BY created_at DESC LIMIT %s",
                              row["topic"], limit + len(seen)) if r["id"] not in seen][: limit - len(rows)]
    return rows


def near_articles(aid: str, low: float, high: float, limit: int) -> list[dict]:
    """Other checks close in meaning to this one (cosine in [low, high)): the same subject seen from
    another angle, not the same claim (that is deduplication, from `high` up)."""
    import numpy as np
    from .similar import cosine
    me = q1("SELECT emb, emb2 FROM articles WHERE id=%s", aid)
    mine = [np.frombuffer(b, dtype=np.float32) for b in (me or {}).values() if b]
    if not mine:
        return []
    scored = []
    for r in q("""SELECT id, emb, emb2 FROM articles WHERE id != %s AND status != 'removed' AND NOT demo
                  AND (emb IS NOT NULL OR emb2 IS NOT NULL)""", aid):
        s = max(cosine(b, m) for b in (r["emb"], r["emb2"]) if b for m in mine)
        if low <= s < high:
            scored.append((s, r["id"]))
    return [get(i) for _, i in sorted(scored, reverse=True)[:limit]]


def changes(aid: str | None = None, kind: str | None = None):
    sql = """SELECT c.*, a.title, a.slug, a.created_at AS a_created, a.id AS aid, a.status
             FROM changes c JOIN articles a ON a.id=c.article_id WHERE TRUE"""
    args = []
    if aid:
        sql += " AND c.article_id=%s"; args.append(aid)
    if kind:
        sql += " AND c.kind=%s"; args.append(kind)
    return q(sql + " ORDER BY c.at DESC", *args)
