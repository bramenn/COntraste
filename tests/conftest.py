import json
import os
import re
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="contraste-test-")

# Tests run against their own database, wiped at the start. The name must end in _test so a
# misconfigured URL can never point them at real data.
TEST_DB = os.getenv("TEST_DATABASE_URL", "postgresql://contraste:dev@localhost:55432/contraste_test")
_base, _name = TEST_DB.rsplit("/", 1)
assert _name.endswith("_test"), "TEST_DATABASE_URL must point to a database whose name ends in _test"
import psycopg  # noqa: E402
with psycopg.connect(_base + "/postgres", autocommit=True) as _c:
    if not _c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (_name,)).fetchone():
        _c.execute(f'CREATE DATABASE "{_name}"')
with psycopg.connect(TEST_DB, autocommit=True) as _c:
    _c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
os.environ["DATABASE_URL"] = TEST_DB
os.environ["DEMO_MODE"] = "false"
os.environ["MARKETS"] = "false"  # no network in tests
os.environ["WARM_CARDS"] = "false"
os.environ["AUTO_CHECKS_PER_DAY"] = "0"  # no checks of the day unless a test asks for them
os.environ["GATE_MODEL"] = ""  # the gate's chat-model path runs on FakeLLM; Jev's path has its own test
os.environ["PUBLISH_MIN_SOURCES"] = "3"
REAL = os.getenv("CONTRASTE_REAL_TESTS") == "1"
if not REAL:
    os.environ["OPENROUTER_API_KEY"] = "test"
    os.environ["OPENROUTER_MODEL"] = "test/model"

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import fetch, llm, pipeline  # noqa: E402


class FakeLLM:
    """Stands in for the HTTP call to OpenRouter. Behaves like a model that has been fooled:
    it returns whatever the test says, so we can check the server rules correct it."""

    def __init__(self, claims, evidence, verdict, circulating=None, input_kind="afirmacion", when="",
                 related=True, injection=False, page_injection=lambda text: False):
        self.claims, self.evidence, self.verdict, self.input_kind, self.when = claims, evidence, verdict, input_kind, when
        self.related, self.injection, self.page_injection = related, injection, page_injection
        self.circulating = circulating
        self.calls: list[list] = []

    async def __call__(self, messages, model, fast=False):
        self.calls.append(messages)
        user = messages[1]["content"]
        from app import gate
        if user.startswith(gate.SCREEN_TASK[:40]):
            return json.dumps({"foreign_local": not self.related, "injection": self.injection})
        if user.startswith(gate.PAGE_TASK[:40]):
            return json.dumps({"injection": self.page_injection(user)})
        if user.startswith(llm.EXTRACT_TASK[:40]):
            return json.dumps({"circulating": self.circulating or self.claims[0], "title": (self.claims or [self.circulating])[0][:80],
                               "topic": "politica", "about_private_person": False, "not_verifiable": [], "input_kind": self.input_kind,
                               "claims": [{"text": c, "short": c[:50], "queries": [c, c + " Colombia"], "when": self.when}
                                          for c in self.claims]})
        if user.startswith(llm.EVIDENCE_TASK[:40]):
            stance, quote = self.evidence(user)
            listed = user.split("AFIRMACIONES A VERIFICAR:", 1)[1].split("FUENTE:", 1)[0]
            return json.dumps({"items": [{"claim": int(n), "stance": stance, "basis": "reporte_periodistico",
                                          "summary": "Resumen propio.", "quote": quote}
                                         for n in re.findall(r"^\[(\d+)\]", listed, re.M)]})
        if user.startswith(llm.VERDICT_TASK[:40]):
            return json.dumps({"headline": "Frase del modelo.", "timeline": [],
                               "claims": [{"index": i, "rating": self.verdict, "explanation": "El modelo dice que sí.",
                                           "card_line": "Línea", "proven": "x", "sources_say": "y"}
                                          for i in range(len(self.claims))]})
        raise AssertionError("llamada inesperada al LLM")


def dated_article(text: str, date: str) -> str:
    return article(text).replace("<head>", f'<head><meta property="article:published_time" content="{date}">', 1)


def article(text: str) -> str:
    return f"<html><head><title>Nota</title></head><body><article><h1>Nota</h1><p>{text}</p>" \
           f"<p>{'Texto de relleno para que la página tenga contenido suficiente y legible. ' * 8}</p></article></body></html>"


@pytest.fixture
def web(monkeypatch):
    """Sites served locally (in memory) through httpx.MockTransport. Returns the url -> html dict."""
    pages: dict[str, str] = {}

    def handler(request: httpx.Request):
        html = pages.get(str(request.url))
        if html is None:
            return httpx.Response(404)
        if isinstance(html, bytes):  # documents (PDF)
            return httpx.Response(200, content=html, headers={"content-type": "application/pdf"})
        return httpx.Response(200, html=html)

    async def no_dns(url):  # test domains do not exist; SSRF protection has its own tests
        pass

    real_get = fetch.safe_get
    monkeypatch.setattr(fetch, "check_url", no_dns)
    monkeypatch.setattr(pipeline, "safe_get", lambda url, **kw: real_get(url, transport=httpx.MockTransport(handler), **kw))

    async def fake_search(queries, claim_text, queries_en=()):
        return [{"url": u, "title": "Nota"} for u in pages]

    monkeypatch.setattr(pipeline, "search", fake_search)
    return pages


@pytest.fixture
def fake_llm(monkeypatch):
    def install(**kw):
        f = FakeLLM(**kw)
        if not REAL:
            monkeypatch.setattr(llm, "_complete", f)
        return f
    return install


async def quiet(*a, **k):
    pass


def sign_in(client, email: str | None = None, extra: int = 50) -> dict:
    """Signed-in, verified account with `extra` checks granted by an editor; the client carries its cookie and CSRF header."""
    import secrets
    from app import accounts, credits, db
    email = email or f"t{secrets.token_hex(4)}@example.org"
    db.q("INSERT INTO users(id, email, email_verified, consent_at) VALUES(%s,%s,TRUE, now())", "u" + secrets.token_hex(8), email)
    user = db.q1("SELECT * FROM users WHERE email=%s", email)
    if extra:
        credits.admin_adjust(user["id"], extra, "test")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
    db.q("INSERT INTO user_sessions VALUES(%s,%s,%s, now() + interval '1 day')", accounts._hash(token), user["id"], csrf)
    client.cookies.clear()  # a renewed session comes back as a Set-Cookie; keep only this account's
    client.cookies.set(accounts.COOKIE, token)
    client.headers["x-csrf"] = csrf
    return user


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    """Every test client shares one IP; each test starts with clean per-IP counters."""
    from app import db
    db.q("DELETE FROM rate_hits")


@pytest.fixture(autouse=True)
def fresh_shared_cache():
    """Pages shared between anonymous visitors are reused for a few seconds; tests start without them."""
    from app import main
    main._shared.clear()
    main._topics = (0.0, [])
