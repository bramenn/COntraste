"""«Memoria»: Colombian history in verified facts (memoria.yaml), shown at /memoria and in the footer.

Every fact carries its source and the words that source must contain. `python -m app.memoria` downloads each
source and checks them, so nothing is published that its source does not say."""
import re
import sys
import unicodedata
from functools import lru_cache

import yaml
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from . import db, settings

router = APIRouter()


@lru_cache(maxsize=1)
def data() -> dict:
    d = yaml.safe_load((settings.ROOT / "memoria.yaml").read_text(encoding="utf-8"))
    years = str(db.today_co().year - 1810)
    for s in d["stats"]:
        s["value"] = s["value"].replace("{years_since_1810}", years)
    return d


def highlights(n: int = 4) -> list[dict]:
    """A few facts for the footer, one from each era."""
    return [era["events"][0] for era in data()["eras"]][:n]


@router.get("/memoria", response_class=HTMLResponse)
def memoria_page():
    from .main import page
    d = data()
    return page("memoria.html", nav="memoria", stats=d["stats"], eras=d["eras"], heritage=d["heritage"], checked=d["checked"],
                total=sum(len(e["events"]) for e in d["eras"]))


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return re.sub(r"\s+", " ", "".join(c for c in s if not unicodedata.combining(c)))


def check() -> int:
    """Download every source and confirm it says what we publish. Returns the number of problems."""
    import httpx
    from .fetch import extract
    d, problems, pages = data(), 0, {}
    with httpx.Client(timeout=25, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/140.0"}) as c:
        def text(url):
            if url not in pages:
                try:
                    r = c.get(url)
                    pages[url] = _norm(extract(r.content, str(r.url))["text"] + " " + re.sub(r"<[^>]+>", " ", r.text))
                except Exception as e:
                    pages[url] = f"!!{e}"
            return pages[url]
        items = [(f"{e['year']} {e['title']}", e["source"]["url"], e["verify"]) for era in d["eras"] for e in era["events"]]
        items += [(f"UNESCO: {s['name']}", d["heritage"]["source"]["url"], [s["verify"]]) for s in d["heritage"]["sites"]]
        for name, url, words in items:
            t = text(url)
            missing = [w for w in words if _norm(w) not in t]
            if t.startswith("!!") or missing:
                problems += 1
                print(f"✗ {name}: {'no se pudo leer ' + t[2:80] if t.startswith('!!') else 'falta ' + str(missing)}")
            else:
                print(f"✓ {name}")
    print(f"\n{problems} problemas")
    return problems


if __name__ == "__main__":
    sys.exit(1 if check() else 0)
