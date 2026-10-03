"""Credits: how many checks each account has. Contraste is free; credits only limit use. The ledger is
append-only (a database trigger refuses UPDATE and DELETE) and every balance is computed from it. Two buckets:
"free" belongs to a month (the monthly allowance) and does not carry over; "extra" (rewards for contributed
evidence, editor grants) never expires. Free credits are spent first. On top of the monthly allowance there is a
daily limit, also set by editors."""
from . import db, settings


class NoCredits(Exception):
    pass


class Blocked(Exception):
    """An editor blocked the account."""


def month() -> str:
    return db.today_co().strftime("%Y-%m")


def _add(c, user_id: str, kind: str, bucket: str, amount: int, ref: str | None = None, note: str | None = None,
         month_: str | None = None) -> bool:
    """Insert one ledger row. Returns False when a unique rule says it already happened."""
    cur = c.execute("""INSERT INTO credit_ledger(user_id, kind, bucket, month, amount, ref, note)
                       VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (user_id, kind, bucket, month_, amount, ref, note))
    return cur.rowcount == 1


def free_monthly() -> int:
    """Free checks per account per month. Editors set it in /admin; .env only gives the starting value."""
    return int(db.setting("free_monthly_credits", settings.FREE_MONTHLY_CREDITS))


def renews_on() -> str:
    """Date the free checks come back, for people ("1 de noviembre de 2026")."""
    from datetime import timedelta
    from .cards import format_date
    nxt = db.today_co().replace(day=28) + timedelta(days=4)
    return format_date(nxt.replace(day=1, hour=12).isoformat())


def daily_limit() -> int:
    """Checks per account per Colombian day. Editors set it in /admin; .env only gives the starting value."""
    return int(db.setting("daily_checks", settings.DAILY_CHECKS))


def used_today(user: dict) -> int:
    """Checks spent today (Colombian day) that were not given back: a rejection we refunded does not count."""
    start = db.today_co().replace(hour=0, minute=0, second=0, microsecond=0)
    return db.q1("""SELECT COUNT(*) AS n FROM credit_ledger s WHERE s.user_id=%s AND s.kind='spend' AND s.created_at >= %s
                    AND NOT EXISTS (SELECT 1 FROM credit_ledger r WHERE r.kind='refund' AND r.ref=s.ref)""",
                 user["id"], start)["n"]


def _lock(c, user_id: str):
    c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("credits:" + user_id,))


def _grant_monthly(c, user: dict):
    """Bring this month's free credits up to the current allowance (only for verified, non-disposable
    addresses). Raising the allowance mid-month tops everyone up; lowering it takes nothing back.
    Callers hold the per-user lock."""
    n = free_monthly()
    if not (user["email_verified"] and not user["disposable"]) or n <= 0:
        return
    granted = c.execute("""SELECT COALESCE(SUM(amount), 0) AS g FROM credit_ledger
                           WHERE user_id=%s AND kind='monthly_grant' AND month=%s""", (user["id"], month())).fetchone()["g"]
    if granted < n:
        _add(c, user["id"], "monthly_grant", "free", n - int(granted), month_=month(),
             note=None if granted == 0 else "Más verificaciones gratis este mes")


def _balances(c, user_id: str) -> dict:
    row = c.execute("""SELECT COALESCE(SUM(amount) FILTER (WHERE bucket='free' AND month=%s), 0) AS free,
                              COALESCE(SUM(amount) FILTER (WHERE bucket='extra'), 0) AS extra
                       FROM credit_ledger WHERE user_id=%s""", (month(), user_id)).fetchone()
    extra = max(0, int(row["extra"]))
    return {"free": int(row["free"]), "extra": extra, "total": int(row["free"]) + extra}


def balances(user: dict) -> dict:
    with db.pool.connection() as c, c.transaction():
        _lock(c, user["id"])
        _grant_monthly(c, user)
        return _balances(c, user["id"])


def spend(user: dict, ref: str) -> str:
    """Take one credit for `ref` (a job id). Serialised per user so two requests at the same time can
    never spend the same credit. Returns the bucket used."""
    with db.pool.connection() as c, c.transaction():
        _lock(c, user["id"])
        if user.get("blocked"):
            raise Blocked("La cuenta está bloqueada.")
        _grant_monthly(c, user)
        b = _balances(c, user["id"])
        if b["free"] > 0:
            _add(c, user["id"], "spend", "free", -1, ref, month_=month())
            return "free"
        if b["extra"] > 0:
            _add(c, user["id"], "spend", "extra", -1, ref)
            return "extra"
        raise NoCredits()


ANON_SHARE = 0.25  # share of the day's model budget that checks without an account can use, all together


def anon_claim(visitor: str, job_id: str) -> bool:
    """The one check a day without an account. False if this visitor already used today's."""
    return bool(db.q1("""INSERT INTO anon_checks VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING job_id""",
                      db.today_co().date().isoformat(), visitor, job_id))


def anon_budget_left() -> bool:
    """Checks without an account share ANON_SHARE of the day's model budget: abuse can use that up, never
    the checks of people with an account."""
    cap = settings.DAILY_SPEND_LIMIT_USD * ANON_SHARE
    start = db.today_co().replace(hour=0, minute=0, second=0, microsecond=0)
    spent = db.q1("SELECT COALESCE(SUM(cost_usd), 0) AS usd FROM jobs WHERE input->>'anon' = 'true' AND created_at >= %s",
                  start)["usd"]
    return not cap or spent < cap


def refund(ref: str, note: str = "Devolución por error del sistema") -> bool:
    """Give back the credit spent on `ref`, into the same bucket and month, or the day's check without an
    account. At most once."""
    with db.pool.connection() as c, c.transaction():
        row = c.execute("SELECT * FROM credit_ledger WHERE kind='spend' AND ref=%s", (ref,)).fetchone()
        if not row:
            return bool(c.execute("DELETE FROM anon_checks WHERE job_id=%s", (ref,)).rowcount)
        return _add(c, row["user_id"], "refund", row["bucket"], 1, ref, note, row["month"])


def reward(user_id: str, ref: str, note: str = "Gracias por aportar evidencia", amount: int = 1,
           bucket: str = "extra") -> bool:
    """Once per ref. bucket "free" lasts until the end of the month; "extra" never expires."""
    with db.pool.connection() as c, c.transaction():
        return _add(c, user_id, "reward", bucket, amount, ref, note, month() if bucket == "free" else None)


def admin_adjust(user_id: str, amount: int, note: str):
    with db.pool.connection() as c, c.transaction():
        _add(c, user_id, "admin_adjust", "extra", amount, None, note)


def ledger(user_id: str, limit: int = 100) -> list[dict]:
    return db.q("SELECT * FROM credit_ledger WHERE user_id=%s ORDER BY id DESC LIMIT %s", user_id, limit)
