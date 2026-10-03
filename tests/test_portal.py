"""Required tests 6-9: deduplication, view counting and publishing thresholds."""
import re
import json

import pytest
from fastapi.testclient import TestClient

from app import db
from app.demo import example
from app.main import app
from conftest import REAL, article, sign_in

BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
pytestmark = pytest.mark.skipif(REAL, reason="solo con LLM simulado")


@pytest.fixture
def client():
    with TestClient(app) as c:
        sign_in(c)
        yield c


def wait(c, job_id):
    """Follow the progress stream like a browser would, reconnecting when a connection ends."""
    for _ in range(10):
        with c.stream("GET", f"/api/checks/{job_id}/events") as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    if ev["type"] in ("done", "error"):
                        return ev


def three_sources(web, fact):
    for u in ("https://www.registraduria.gov.co/a", "https://www.eltiempo.com/b", "https://www.semana.com/c"):
        web[u] = article(fact)


def test_6_same_claim_worded_differently_is_one_article(client, web, fake_llm):
    fact = "Los organizadores confirmaron que el paro nacional fue cancelado hasta nuevo aviso."
    three_sources(web, fact)
    fake_llm(claims=["El paro nacional fue cancelado."], evidence=lambda u: ("confirma", "el paro nacional fue cancelado hasta nuevo aviso"),
             verdict="verdadero", circulating="El paro nacional fue cancelado.")

    r1 = client.post("/api/checks", data={"text": "El paro nacional fue cancelado"}).json()
    done = wait(client, r1["id"])
    assert done["type"] == "done"
    aid = done["url"].rsplit("-", 1)[-1]

    r2 = client.post("/api/checks", data={"text": "¿Es cierto que cancelaron el paro nacional?"}).json()
    assert r2.get("duplicate") and r2["id"] == aid
    assert db.q1("SELECT COUNT(*) AS n FROM articles WHERE title LIKE '%paro%'")["n"] == 1
    assert db.consult_count(aid) == 2
    # The same person asking again the same day does not push it up the front page.
    client.post("/api/checks", data={"text": "¿Es cierto que cancelaron el paro nacional?"})
    client.post("/api/checks", data={"text": "¿Es cierto que cancelaron el paro nacional?"}, headers={"user-agent": "otro"})
    assert db.consult_count(aid) == 2
    html = client.get(done["url"]).text
    assert "2 consultas" in html
    # SEO of a listed article
    assert '"@type": "NewsArticle"' in html and '"@type": "ClaimReview"' in html and '"@type": "BreadcrumbList"' in html
    assert 'max-image-preview:large' in html and '<link rel="canonical"' in html
    assert f"{aid}/og.png" in client.get("/sitemap.xml").text


def _new_article():
    return db.save_article(example(0), status="listed", reason=None, keys={})


def test_7_hundred_views_from_one_visitor_count_once(client):
    aid = _new_article()
    for _ in range(100):
        client.post(f"/api/views/{aid}", content=json.dumps({"visible_ms": 6000}), headers={"user-agent": BROWSER})
    assert db.view_count(aid) == 1
    assert db.q1("SELECT COUNT(*) AS n FROM view_rejected WHERE article_id=%s", aid)["n"] == 99
    # neither the IP nor the user agent is stored in clear
    dump = json.dumps([db.q(f"SELECT * FROM {t}") for t in ("view_seen", "view_rejected", "article_views_hourly", "rate_hits")],
                      default=str)
    assert "testclient" not in dump and "Chrome/140" not in dump


def test_8_no_beacon_or_bot_does_not_count(client):
    aid = _new_article()
    browser = BROWSER + " Edg/140.0"  # a different visitor: the one from test 7 already hit its hourly limit
    path = db.path_of(db.get(aid))
    assert client.get(path, headers={"user-agent": BROWSER}).status_code == 200      # just loading the page
    for ua in ("Googlebot/2.1 (+http://www.google.com/bot.html)", BROWSER.replace("Chrome", "HeadlessChrome"),
               "curl/8.5", "python-requests/2.32", ""):
        client.post(f"/api/views/{aid}", content='{"visible_ms": 9000}', headers={"user-agent": ua})
    client.post(f"/api/views/{aid}", content='{"visible_ms": 1200}', headers={"user-agent": browser})  # < 5 s
    assert db.view_count(aid) == 0
    reasons = {r["reason"] for r in db.q("SELECT reason FROM view_rejected WHERE article_id=%s", aid)}
    assert reasons == {"bot", "poco_tiempo"}
    client.post(f"/api/views/{aid}", content='{"visible_ms": 5200}', headers={"user-agent": browser})
    assert db.view_count(aid) == 1                                                     # control positivo


def test_9_single_source_is_unlisted_and_noindex(client, web, fake_llm):
    web["https://www.dane.gov.co/ipc"] = article("El DANE reportó que la inflación anual de agosto fue de 4,1 por ciento.")
    fake_llm(claims=["La inflación de agosto fue de 4,1 %."], evidence=lambda u: ("confirma", "la inflación anual de agosto fue de 4,1 por ciento"),
             verdict="verdadero", circulating="La inflación anual de agosto fue de 4,1 %.")
    r = client.post("/api/checks", data={"text": "La inflación anual de agosto fue de 4,1 %"}).json()
    done = wait(client, r["id"])
    aid = done["url"].rsplit("-", 1)[-1]
    row = db.get(aid)
    assert row["status"] == "unlisted" and "menos de 3 fuentes" in row["unlisted_reason"]
    assert json.loads(row["result"])["rating"] == "sin_pruebas"
    html = client.get(done["url"]).text
    assert '<meta name="robots" content="noindex' in html
    assert "ClaimReview" not in html
    assert aid not in client.get("/sitemap.xml").text
    assert aid not in client.get("/").text
    assert aid not in client.get("/archivo?q=inflaci%C3%B3n").text
    assert aid not in client.get("/feed.xml").text


def test_thin_checks_are_marked_and_private_people_get_no_card(client, web, fake_llm):
    """A check that is not on the front page carries a visible band on its card; one about a private
    person has no card at all, so our logo never travels next to an accusation against them."""
    from app import cards
    rendered = []

    async def fake_render(result, aid, created_at, fmt, caution=None):
        rendered.append(caution)
        return b"png"
    import app.cards as cards_mod
    orig = cards_mod.render_card
    cards_mod.render_card = fake_render
    try:
        fact = "El colegio del barrio La Esperanza cerró sus puertas por falta de estudiantes este año."
        web["https://www.eltiempo.com/z1"] = article(fact)
        fake_llm(claims=["El colegio del barrio La Esperanza cerró."], evidence=lambda u: ("confirma", "cerró sus puertas por falta de estudiantes"),
                 verdict="verdadero", circulating="El colegio del barrio La Esperanza cerró.")
        done = wait(client, client.post("/api/checks", data={"text": "El colegio del barrio La Esperanza cerró"}).json()["id"])
        aid = done["url"].rsplit("-", 1)[-1]
        assert db.get(aid)["status"] == "unlisted"
        assert rendered and all(c == cards.CAUTION for c in rendered)
        assert "Verificación preliminar" in client.get(done["url"]).text

        r = json.loads(db.get(aid)["result"])
        r["private_person"] = True
        db.update_article(aid, r)
        assert client.get(f"/api/cards/{aid}/post.png").status_code == 404
        page = client.get(done["url"]).text
        assert "og:image" not in page and "No hacemos tarjeta" in page
    finally:
        cards_mod.render_card = orig


def test_an_edited_page_is_not_covered_by_its_old_verdict(client, web, fake_llm, monkeypatch):
    """Someone gets a correct page checked, then edits it into something false: the old verdict must not
    be handed out for the new content."""
    from app import ingest, main, pipeline
    monkeypatch.setattr(ingest, "safe_get", pipeline.safe_get)
    monkeypatch.setattr(main, "safe_get", pipeline.safe_get)

    async def no_dns(url):
        pass
    monkeypatch.setattr(main, "check_url", no_dns)
    url = "https://www.blog-de-prueba.com/nota-puente"
    fact = "El nuevo puente peatonal de la avenida Boyacá fue inaugurado el martes por la Alcaldía."
    three_sources(web, fact)
    web[url] = article(fact)
    fake_llm(claims=["El puente peatonal de la avenida Boyacá fue inaugurado."],
             evidence=lambda u: ("confirma", "fue inaugurado el martes por la alcaldía"), verdict="verdadero",
             circulating="El puente peatonal de la avenida Boyacá fue inaugurado.")
    wait(client, client.post("/api/checks", data={"url": url}).json()["id"])

    client.cookies.clear()  # from here on, someone without an account
    r = client.post("/api/checks", data={"url": url})
    assert r.json().get("duplicate"), r.text                                        # unchanged: old verdict
    web[url] = article(fact + " Además, el alcalde anunció que regalará bicicletas a todos los habitantes.")
    r = client.post("/api/checks", data={"url": url})
    assert r.status_code == 200 and not r.json().get("duplicate"), r.text           # edited: a new check (the day's one without an account)


def test_articles_show_how_they_were_researched_and_accept_replies(client, web, fake_llm):
    fact = "La Gobernación confirmó que la vía al Llano estará cerrada durante tres días por obras de mantenimiento."
    three_sources(web, fact)
    fake_llm(claims=["La vía al Llano estará cerrada tres días."], evidence=lambda u: ("confirma", "estará cerrada durante tres días"),
             verdict="verdadero", circulating="La vía al Llano estará cerrada tres días.")
    done = wait(client, client.post("/api/checks", data={"text": "Cierran la vía al Llano tres días"}).json()["id"])
    aid = done["url"].rsplit("-", 1)[-1]
    page = client.get(done["url"]).text
    assert "Cómo lo buscamos" in page and "La vía al Llano estará cerrada tres días. Colombia" in page
    assert "Verificación automatizada con reglas fijas" in page
    r = client.post(f"/v/{aid}/replica", data={"name": "Gobernación", "email": "prensa@gob.example.org",
                                               "message": "La vía reabre un día antes.", "consent": "1"})
    assert r.status_code == 200 and "Recibimos tu respuesta" in r.text
    assert db.q1("SELECT name FROM replies WHERE article_id=%s", aid)["name"] == "Gobernación"
    assert "prensa@gob.example.org" not in client.get(done["url"]).text        # never public


def test_market_indicators_show_value_date_and_source(client):
    from app import markets
    db.set_setting("markets", {"items": [
        {"key": "trm", "label": "Dólar (TRM)", "value": 3341.23, "unit": "cop", "date": "2026-09-30",
         "source": "Superintendencia Financiera", "url": "https://www.datos.gov.co/d/32sa-8pi3"},
        {"key": "brent", "label": "Petróleo Brent", "value": 113.96, "unit": "usd_bbl", "date": "2026-09-29",
         "source": "EIA de EE. UU. (vía FRED)", "url": "https://fred.stlouisfed.org/series/DCOILBRENTEU"}], "at": db.iso()})
    markets._cache = (0.0, [])
    page = client.get("/archivo").text
    assert "$3.341" in page and "US$113,96/barril" in page and "29/09" in page and "Superintendencia Financiera" in page
    db.q("DELETE FROM app_settings WHERE key='markets'")
    markets._cache = (0.0, [])


def test_active_filter_can_be_clicked_again_to_clear_it(client):
    """Clicking the chip of the filter that is already on goes back to everything, instead of sticking."""
    page = client.get("/archivo?tema=seguridad").text
    active = re.search(r'href="([^"]+)" aria-current="true" title="Quitar este filtro"', page)
    assert active and "tema=seguridad" not in active.group(1).replace("&amp;", "&") and ">Seguridad</a>" in page
    page = client.get("/archivo?calificacion=falso").text
    active = re.search(r'href="([^"]+)" aria-current="true" title="Quitar este filtro"', page)
    assert active and "calificacion=falso" not in active.group(1)


def test_pages_are_light_and_cacheable(client):
    """Fast by construction: compressed HTML/CSS/JS, year-long caching only for versioned files, and
    listing images as light WebP with a versioned URL."""
    page = client.get("/", headers={"accept-encoding": "gzip"})
    assert page.headers.get("content-encoding") == "gzip"
    css = re.search(r'href="(/static/app\.css\?v=[0-9a-f]{10})"', page.text).group(1)
    r = client.get(css, headers={"accept-encoding": "gzip"})
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable" and r.headers.get("content-encoding") == "gzip"
    assert "immutable" not in client.get("/static/favicon.svg").headers["cache-control"]   # unversioned: one day
    assert re.search(r'src="/api/cards/[a-z0-9]+/cover\.webp\?v=[0-9a-f]{10}"', client.get("/archivo").text) or \
        "cover.webp" not in client.get("/archivo").text


def test_example_is_a_headline_of_the_moment(client):
    """«Probar» offers one of today's headlines (cleaned, nothing about minors), and the old sample when
    there are none."""
    from app import news
    feed = """<rss><channel>
      <item><title>Atención | Bruce Mac Master sale de la presidencia de la ANDI tras más de 13 años - Valora Analitik</title></item>
      <item><title>Menor de edad que le disparó al exalcalde aceptó su responsabilidad en el asesinato - El Tiempo</title></item>
      <item><title>Corto - X</title></item></channel></rss>"""
    items = news.parse(feed)
    assert [i["title"] for i in items] == ["Bruce Mac Master sale de la presidencia de la ANDI tras más de 13 años"]
    db.set_setting("trending", {"items": items, "at": db.iso()})
    news._cache = (0.0, [])
    assert "Bruce Mac Master sale de la presidencia" in client.get("/").text
    db.q("DELETE FROM app_settings WHERE key='trending'")
    news._cache = (0.0, [])
    assert news.example() == news.FALLBACK


def test_memoria_every_fact_has_a_source_and_renders(client):
    """Each fact in memoria.yaml names its source and the words that source must contain (checked against
    the live pages with `python -m app.memoria`), uses an icon that exists, and shows up on /memoria."""
    import yaml
    from app import settings
    d = yaml.safe_load((settings.ROOT / "memoria.yaml").read_text(encoding="utf-8"))
    sprite = (settings.APP_DIR / "templates" / "_art.html").read_text(encoding="utf-8")
    events = [e for era in d["eras"] for e in era["events"]]
    assert len(events) >= 20
    for e in events:
        # An unquoted comma in YAML flow style silently cuts a title or text in two: no stray keys allowed.
        assert set(e) <= {"year", "date", "title", "icon", "text", "source", "verify"}, (e["title"], set(e))
        assert e["source"]["url"].startswith("http") and e["verify"], e["title"]
        assert f'id="art-{e["icon"]}"' in sprite, e["icon"]
        assert "wikipedia" not in e["source"]["url"] and "grokipedia" not in e["source"]["url"]
    page = client.get("/memoria").text
    for e in events:
        assert e["title"].replace("&", "&amp;") in page and e["source"]["url"].replace("&", "&amp;") in page
    assert "Patrimonio Mundial" in page and page.count('class="mem-stat"') == 4
    footer = client.get("/").text
    assert 'href="/memoria"' in footer and "Ver la memoria completa" in footer
    # The footer promises "cada una con su fuente": every card must actually link to its source.
    for e in [era["events"][0] for era in d["eras"]][:4]:
        assert e["source"]["url"].replace("&", "&amp;") in footer, e["title"]


def test_shared_cache_is_only_for_anonymous_pages():
    """Anonymous pages are reused for a few seconds; a signed-in page (with a balance, a CSRF token) never is."""
    from fastapi.testclient import TestClient
    from app import main
    from conftest import sign_in
    with TestClient(app) as anon:
        anon.get("/memoria")
        assert any(k[0] == "/memoria" for k in main._shared)
        main._shared.clear()
        sign_in(anon)
        page = anon.get("/memoria").text
        assert 'name="csrf"' in page and not any(k[0] == "/memoria" for k in main._shared)
        anon.get("/cuenta")
        assert not any(k[0] == "/cuenta" for k in main._shared)


def test_headlines_keep_only_news_related_to_colombia(monkeypatch):
    """The model picks what concerns Colombia; if it fails, only the Colombia search survives."""
    import asyncio
    from app import llm, news
    items = [{"title": "Putin advierte que usará todo su arsenal si Rusia es atacada"},
             {"title": "CAF aprobó cinco proyectos estratégicos en Colombia que suman US$1.250 millones"}]

    async def picks_second(task, data, model_cls, **kw):
        return model_cls(related=[1])
    monkeypatch.setattr(llm, "ask", picks_second)
    assert [i["title"][:3] for i in asyncio.run(news.colombian(items, set()))] == ["CAF"]

    async def fails(*a, **k):
        raise llm.LLMError("caído")
    monkeypatch.setattr(llm, "ask", fails)
    assert asyncio.run(news.colombian(items, {items[1]["title"]})) == [items[1]]


def test_every_template_compiles_and_every_admin_view_renders(monkeypatch):
    """A template that does not compile takes the whole page down (the admin panel once did, unnoticed):
    compile them all, and sign into /admin and open each of its views."""
    import bcrypt
    from fastapi.testclient import TestClient
    from app import admin, cards, settings
    for name in sorted(p.name for p in (settings.APP_DIR / "templates").glob("*.html")):
        cards.env.get_template(name)
    monkeypatch.setattr(admin, "_HASH", bcrypt.hashpw(b"clave-de-prueba", bcrypt.gensalt()))
    aid = db.q1("SELECT id FROM articles ORDER BY created_at DESC LIMIT 1")
    with TestClient(app) as c:
        assert "Contraseña" in c.get("/admin").text
        assert c.post("/admin/login", data={"password": "clave-de-prueba"}, follow_redirects=False).status_code == 303
        for path in ["/admin", "/admin/negocio", "/admin/encuesta"] + ([f"/admin/a/{aid['id']}"] if aid else []):
            r = c.get(path)
            assert r.status_code == 200, (path, r.status_code)


def test_section_bar_lists_only_sections_with_checks(client):
    """Empty sections only led to empty pages: the bar, the archive filters and the sitemap list the
    sections that have published checks, most populated first."""
    from app import main
    db.q("UPDATE articles SET topic='economia' WHERE status='listed' AND NOT demo")
    main._topics = (0.0, [])
    page = client.get("/archivo").text
    bar = page.split('aria-label="Secciones"', 2)[2].split("</nav>", 1)[0]
    assert ">Economía<" in bar and ">Deportes<" not in bar and ">Entretenimiento<" not in bar
    assert "tema=deportes" not in client.get("/sitemap.xml").text
    assert ">Deportes<" in client.get("/archivo?tema=deportes").text      # a section opened by URL still shows its chip


def test_pages_prerender_links_without_opening_inline_scripts(client):
    """Chrome prerenders the next page from the speculation rules; the CSP lets those rules in and still
    blocks inline scripts, and links with side effects (sign-in, admin, API) are never prerendered."""
    import json, re
    r = client.get("/")
    rules = json.loads(re.search(r'<script type="speculationrules">(.*?)</script>', r.text, re.S).group(1))
    skipped = rules["prerender"][0]["where"]["and"][1]["not"]["href_matches"]
    assert {"/auth/*", "/admin*", "/api/*"} <= set(skipped)
    csp = r.headers["content-security-policy"]
    assert "'inline-speculation-rules'" in csp and "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";")[0]


def test_liveness_does_not_depend_on_the_database(client, monkeypatch):
    """A database hiccup takes pods out of rotation (readiness) but must not get them killed (liveness)."""
    from app import db

    def down(*a):
        raise RuntimeError("database unreachable")
    client.cookies.clear()  # probes carry no session
    monkeypatch.setattr(db, "q1", down)
    assert client.get("/healthz?live=1").status_code == 200
    with pytest.raises(RuntimeError):
        client.get("/healthz")


def test_a_degraded_reinvestigation_does_not_replace_the_published_check(client, monkeypatch):
    """Search engines timed out from one node for an hour and re-investigations came back with a fraction of
    the sources, replacing better checks. A run with less than half the published sources is discarded."""
    import asyncio, json
    from app import main
    aid = _new_article()
    old = json.loads(db.get(aid)["result"])
    assert len(old["sources"]) >= 2

    async def blind(inp, emit, **kw):
        return old | {"rating": "sin_pruebas", "sources": []}, {}, None
    monkeypatch.setattr(main, "investigate", blind)
    asyncio.run(main.reinvestigate(aid))
    row = db.get(aid)
    assert json.loads(row["result"])["sources"] == old["sources"] and row["rating"] == old["rating"]
    assert not row["reinvestigating"]


def test_the_site_points_to_its_open_source_code(client):
    """AGPL: the people using the site can reach its source. Each check links to a public report form that
    already carries its address."""
    from app import settings
    aid = _new_article()
    row = db.get(aid)
    assert settings.SOURCE_URL in client.get("/").text and 'id="codigo-abierto"' in client.get("/como-funciona").text
    page = client.get(db.path_of(row)).text
    assert "issues/new?template=verificacion-incorrecta.yml" in page and "url=http" in page


def test_a_check_continues_in_an_ai_assistant(client):
    """Each check opens in ChatGPT or Claude with a prefilled, neutral prompt and its evidence, and exists as
    Markdown. The prompt stays under what Claude accepts (~14,000 chars) even for a huge check."""
    import urllib.parse
    from app import main
    aid = _new_article()
    row = db.get(aid)
    md = client.get(db.path_of(row) + ".md")
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    r = __import__("json").loads(row["result"])
    assert md.text.startswith(f"# {r['title']}") and "## Lo que se verificó" in md.text and "## Fuentes" in md.text
    page = client.get(db.path_of(row)).text
    q = urllib.parse.unquote(page.split("https://claude.ai/new?q=", 1)[1].split('"', 1)[0])
    assert q.startswith("Te comparto una verificación de COntraste") and r["title"] in q
    assert "/?verificar=" in q and "cuenta solo lo que aporte algo nuevo" in q   # suggests checks, skips filler news
    assert "https://chatgpt.com/?q=" in page and 'id="ai-prompt"' in page
    huge = r | {"sources": [{"name": f"Medio {i}", "url": f"https://m{i}.co/n", "title": "x" * 200} for i in range(500)]}
    assert len(main.ai_prompt(row, huge)) < len(main.AI_ASK) + main.AI_MAX + 300
