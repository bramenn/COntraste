"""Monthly and daily limits set by editors, and the product survey."""
import pytest
from fastapi.testclient import TestClient

from app import credits, db, main
from app.main import app
from conftest import REAL, sign_in

pytestmark = pytest.mark.skipif(REAL, reason="solo con LLM simulado")

ANSWERS = {"pmf": "muy", "uses": ["antes_compartir", "trabajo"], "improve": ["rapidez"],
           "benefit": "Me ahorra discusiones en el grupo de la familia"}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(main, "limited", lambda *a: False)
    db.q("DELETE FROM app_settings")
    yield
    db.q("DELETE FROM app_settings")


def fresh(user):
    return db.q1("SELECT * FROM users WHERE id=%s", user["id"])


def test_editor_sets_the_free_checks_and_raising_tops_everyone_up():
    with TestClient(app) as c:
        user = sign_in(c, extra=0)
    assert credits.balances(fresh(user))["free"] == 30             # default for new accounts
    db.set_setting("free_monthly_credits", 32)
    assert credits.balances(fresh(user))["free"] == 32             # raised mid-month: gets the difference
    db.set_setting("free_monthly_credits", 2)
    assert credits.balances(fresh(user))["free"] == 32             # lowered: nothing taken back this month
    assert credits.balances(fresh(user))["free"] == 32             # and no double grant


def test_daily_limit_counts_only_checks_really_spent(monkeypatch):
    """Editors set how many checks an account can ask for per Colombian day. A rejection we refunded does
    not count against it."""
    db.set_setting("daily_checks", 2)
    with TestClient(app) as c:
        user = sign_in(c, extra=0)
        credits.spend(fresh(user), "day-1-" + user["id"])
        credits.spend(fresh(user), "day-2-" + user["id"])
        credits.refund("day-2-" + user["id"])
        assert credits.used_today(fresh(user)) == 1
        credits.spend(fresh(user), "day-3-" + user["id"])
        r = c.post("/api/checks", data={"text": "Una afirmación nueva cuando ya se usó el cupo del día"})
        assert r.status_code == 429 and "Ya hiciste las 2 verificaciones de hoy" in r.json()["error"]
        assert "hasta 2 por día" in c.get("/").text and "disponibles hoy" in c.get("/cuenta").text


def test_out_of_free_checks_leads_to_the_survey_which_rewards_once(monkeypatch):
    async def quick(inp, emit, dedup=True):
        raise main.UserError("fin de la prueba")
    monkeypatch.setattr(main, "investigate", quick)
    db.set_setting("free_monthly_credits", 1)
    db.set_setting("daily_checks", 10)  # this test is about the monthly checks running out, not the daily ones
    with TestClient(app) as c:
        user = sign_in(c, extra=0)
        credits.spend(fresh(user), "use-" + user["id"])
        r = c.post("/api/checks", data={"text": "Una afirmación nueva cuando ya no quedan verificaciones"})
        assert r.status_code == 402 and r.json()["next"] == "/encuesta?sin_creditos=1"
        assert 'id="survey-toast"' in c.get("/archivo").text                 # invited while out of checks

        bad = c.post("/encuesta", data={**ANSWERS, "csrf": c.headers["x-csrf"], "pmf": ""})
        assert bad.status_code == 400 and "Responde cómo te sentirías" in bad.text
        ok = c.post("/encuesta", data={**ANSWERS, "csrf": c.headers["x-csrf"]}, follow_redirects=False)
        assert ok.status_code == 303
        c.post("/encuesta", data={**ANSWERS, "csrf": c.headers["x-csrf"]})   # a second answer changes nothing
        assert credits.balances(fresh(user))["total"] == 2                    # survey reward, once
        assert 'id="survey-toast"' not in c.get("/archivo").text
        saved = db.q1("SELECT answers FROM surveys WHERE user_id=%s", user["id"])["answers"]
        assert saved["uses"] == ["antes_compartir", "trabajo"] and set(saved) <= {"pmf", "uses", "improve", "benefit", "improve_text"}

        for i in range(2):
            credits.spend(fresh(user), f"use-{i}-" + user["id"])
        r = c.post("/api/checks", data={"text": "Otra afirmación nueva cuando ya respondió la encuesta"})
        assert r.json()["next"] == "/cuenta?sin_creditos=1"

        c.post("/cuenta/eliminar", data={"csrf": c.headers["x-csrf"], "confirm": "eliminar"})
    assert db.q1("SELECT 1 FROM surveys WHERE user_id=%s", user["id"]) is None
    assert db.q1("SELECT 1 FROM surveys WHERE answers->>'benefit' LIKE 'Me ahorra%%' AND user_id IS NULL")


def test_one_account_cannot_hog_the_queue(monkeypatch):
    async def slow(inp, emit, dedup=True):
        await __import__("asyncio").sleep(5)
    monkeypatch.setattr(main, "investigate", slow)
    with TestClient(app) as c:
        sign_in(c)
        codes = [c.post("/api/checks", data={"text": f"Afirmación número {i} para llenar la cola de una cuenta"}).status_code
                 for i in range(3)]
    assert codes == [200, 200, 429]


def test_people_see_how_many_checks_they_have_left(monkeypatch):
    async def quick(inp, emit, dedup=True):
        raise main.UserError("fin de la prueba")
    monkeypatch.setattr(main, "investigate", quick)
    with TestClient(app) as c:
        assert "Con una cuenta gratis tienes 30 verificaciones al mes, hasta 3 por día" in c.get("/").text
        sign_in(c, extra=0)
        home = c.get("/").text
        assert "Te quedan <b data-balance>30</b>" in home and "data-balance-pill" in home
        r = c.post("/api/checks", data={"text": "Afirmación nueva para ver cuántas verificaciones quedan"})
        assert r.json()["remaining"] == 29
