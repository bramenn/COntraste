"""Job queue: a fixed number of checks run at once per replica, the rest wait in line, and jobs of a
replica that died are picked up again."""
import asyncio
import json
import time

from fastapi.testclient import TestClient

from app import db, main, settings
from conftest import sign_in


def test_checks_beyond_capacity_wait_in_line(monkeypatch):
    monkeypatch.setattr(settings, "MAX_CONCURRENT_CHECKS", 2)
    db.q("DELETE FROM jobs")  # jobs left by earlier tests would sit ahead in the line
    running, peak = 0, 0

    async def slow_investigate(inp, emit, dedup=True):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await emit("Trabajando", 50)
        await asyncio.sleep(1.2)
        running -= 1
        raise main.UserError("fin de la prueba")  # ends the job without needing a real pipeline
    monkeypatch.setattr(main, "investigate", slow_investigate)

    with TestClient(main.app) as c:
        ids = []
        for i in range(3):  # one account each: an account can only have 2 checks open at once
            sign_in(c)
            ids.append(c.post("/api/checks", data={"text": f"Afirmación de prueba número {i} sobre la cola"}).json()["id"])
        for _ in range(20):  # until the first two are running
            if sum(db.job_get(i)["status"] == "running" for i in ids) == 2:
                break
            time.sleep(0.1)
        with c.stream("GET", f"/api/checks/{ids[2]}/events") as r:
            first = [json.loads(l[6:]) for l in r.iter_lines() if l.startswith("data: ")][:1]
        # Wait until all three are finished.
        for _ in range(40):
            if all(db.job_get(i)["status"] == "error" for i in ids):
                break
            time.sleep(0.25)
    assert first and first[0] == {"type": "queued", "position": 1}
    assert peak == 2                                       # never more than the capacity at once
    assert all(db.job_get(i)["status"] == "error" for i in ids)


def test_jobs_of_a_dead_replica_are_picked_up_again():
    db.job_create("dead01", {"kind": "text", "text": "x"})
    assert db.job_claim("replica-that-dies")["id"] == "dead01"
    db.q("UPDATE jobs SET heartbeat_at = now() - interval '5 minutes' WHERE id='dead01'")
    assert db.job_requeue_stale(stale_s=90) == 1
    assert db.job_get("dead01")["status"] == "queued"
    # After too many attempts it fails instead of looping forever.
    for _ in range(2):
        db.job_claim("replica-that-dies")
        db.q("UPDATE jobs SET heartbeat_at = now() - interval '5 minutes' WHERE id='dead01'")
        db.job_requeue_stale(stale_s=90)
    db.job_claim("replica-that-dies")
    db.q("UPDATE jobs SET heartbeat_at = now() - interval '5 minutes' WHERE id='dead01'")
    db.job_requeue_stale(stale_s=90, max_attempts=3)
    assert db.job_get("dead01")["status"] == "error"


def test_two_replicas_never_claim_the_same_job():
    for i in range(5):
        db.job_create(f"pair{i:02d}", {"kind": "text", "text": "x"})
    a = [db.job_claim("replica-a") for _ in range(3)]
    b = [db.job_claim("replica-b") for _ in range(3)]
    claimed = [j["id"] for j in a + b if j and j["id"].startswith("pair")]
    assert len(claimed) == len(set(claimed)) == 5
