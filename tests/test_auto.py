"""Checks chosen by COntraste among the day's news."""
import asyncio

import pytest

from app import auto, db
from conftest import REAL

pytestmark = pytest.mark.skipif(REAL, reason="solo con LLM simulado")


def test_checks_of_the_day_are_spread_from_7_to_19():
    assert [auto.due_now(5, h) for h in (6.9, 7, 9.9, 10, 13, 18.9, 19, 23)] == [0, 1, 1, 2, 3, 4, 5, 5]
    assert auto.due_now(1, 8) == 1 and auto.due_now(0, 12) == 0


def test_picks_a_new_story_skips_what_was_checked_and_waits_for_the_next_slot(monkeypatch):
    from app.main import page  # noqa: F401  (loads the app and its schema)
    from app.demo import example
    db.q("DELETE FROM jobs")
    db.set_setting("auto_checks", 5)
    checked = "El Gobierno denuncia que faltan 148.000 millones de pesos en el sistema de medios públicos RTVC"
    db.save_article(example(0) | {"circulating": checked}, status="listed", reason=None,
                    keys={"text_key": __import__("app.similar", fromlist=["text_key"]).text_key(checked)})
    db.set_setting("trending", {"items": [
        {"title": "Lotería de Bogotá: resultados del sorteo del jueves 1 de octubre", "source": "Blu Radio"},
        {"title": checked, "source": "El Colombiano"},
        {"title": "Francisco Barbosa anuncia acción penal por presunto espionaje ilegal en su contra", "source": "Semana"}]})
    eight = db.today_co().replace(hour=8, minute=0)
    monkeypatch.setattr(db, "today_co", lambda: eight)
    shown = []

    async def pick(task, data, model_cls, **kw):
        shown.append(data)
        n = next(i for i, line in enumerate(data.splitlines()) if "Barbosa" in line)
        return auto.Pick(index=n, claim="Francisco Barbosa denuncia que fue espiado ilegalmente")
    monkeypatch.setattr(auto.llm, "ask", pick)
    try:
        asyncio.run(auto.tick())
        jobs = db.q("SELECT input FROM jobs WHERE input->>'auto' = 'true'")
        assert len(jobs) == 1 and jobs[0]["input"]["text"].startswith("Francisco Barbosa denuncia")
        assert jobs[0]["input"]["source"] == "Semana" and jobs[0]["input"]["keys"]["text_key"]
        assert "RTVC" not in shown[0]                      # already checked: never offered
        asyncio.run(auto.tick())                           # 8 a.m.: only the 7 a.m. slot is due
        assert len(db.q("SELECT 1 FROM jobs WHERE input->>'auto' = 'true'")) == 1
    finally:
        db.q("DELETE FROM jobs")
        db.q("DELETE FROM app_settings")


def test_the_article_says_it_was_chosen_by_contraste():
    from fastapi.testclient import TestClient
    from app.demo import example
    from app.main import app
    aid = db.save_article(example(0) | {"auto": {"headline": "Titular", "source": "Semana"}}, status="listed",
                          reason=None, keys={})
    with TestClient(app) as client:
        page = client.get(db.path_of(db.get(aid))).text
    assert "Elegida por COntraste entre las noticias del día, a partir de un titular de Semana" in page


def test_a_recent_check_gets_a_second_look_once_and_old_ones_are_left_alone():
    """Stories in development change within hours: checks get a second look 6 and 24 hours after they were made.
    Each pass runs once, and only within its window, so a deploy never re-investigates the archive."""
    from datetime import timedelta
    from app.demo import example
    from app.main import page  # noqa: F401  (loads the app and its schema)
    db.q("DELETE FROM followups")
    fresh = db.save_article(example(0), status="listed", reason=None, keys={}, created_at=db.iso(db.now() - timedelta(hours=7)))
    old = db.save_article(example(1), status="listed", reason=None, keys={}, created_at=db.iso(db.now() - timedelta(hours=40)))
    due = [auto.due_followup() for _ in range(3)]
    assert fresh in due and old not in due and due.count(fresh) == 1
    db.q("DELETE FROM followups")
