"""Run the model standard for one stage and one model (see evals/README.md).

    OPENROUTER_API_KEY=... python -m evals.run --stage fuentes --model qwen/qwen3.8-27b:free

It needs only an OpenRouter key: no database, no search engines. It prints the report, says whether the model
passes, and saves the report in evals/results/ so a proposal can attach it."""
import argparse
import asyncio
import base64
import difflib
import io
import json
import re
import statistics
import sys
import time
import types
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

# llm.py only needs two things from the database module: today's date and recording spend. A stand-in keeps the
# standard runnable with nothing but an API key.
SPENT: list[float] = []
_db = types.ModuleType("app.db")
_db.today_co = lambda: datetime.now(timezone(timedelta(hours=-5)))
_db.add_spend = lambda usd, job_id=None: SPENT.append(usd)
sys.modules["app.db"] = _db

from app import llm, settings  # noqa: E402
from app.rules import quote_in_page  # noqa: E402
from evals import cases  # noqa: E402

RESULTS = Path(__file__).parent / "results"
RUNS = 2  # every case runs twice: a model must also agree with itself
# The standard. A model is eligible for a stage when it meets every line, and never makes a critical mistake.
BAR = {
    "fuentes":    {"valid": 0.98, "accuracy": 0.90, "quotes": 0.90, "consistency": 0.85},
    "imagenes":   {"valid": 0.98, "accuracy": 0.95, "consistency": 0.85},
    "extraccion": {"valid": 0.98, "accuracy": 0.85, "consistency": 0.80},
    "veredicto":  {"valid": 0.98, "accuracy": 0.85, "consistency": 0.80},
}
TASK_DATE = "2026-10-03"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    return " ".join("".join(ch for ch in s if ch.isalnum() or ch.isspace()).split())


THROTTLED = [0]


async def _call(task, data, cls, **kw):
    """One model, no fallback. Free models share a pool and answer 429 when it is saturated: that is
    availability, not quality, so the call waits and retries (and the report counts it)."""
    t0 = time.perf_counter()
    for attempt in range(6):
        try:
            return await llm.ask(task, data, cls, **kw), time.perf_counter() - t0
        except llm.LLMError as e:
            if "429" not in str(e) or attempt == 5:
                return None, time.perf_counter() - t0
            THROTTLED[0] += 1
            await asyncio.sleep(20)
            t0 = time.perf_counter()


async def fuentes(case):
    listed = "\n".join(f"[{i}] {c}" for i, c in enumerate(case["claims"]))
    data = (f"AFIRMACIONES A VERIFICAR:\n{listed}\n\nFUENTE: {case.get('source', 'Medio de prueba')} (prueba.co)\n"
            f"FECHA DE LA FUENTE: {case['date']}\nTÍTULO: Nota de prueba\n\nTEXTO DE LA FUENTE:\n{case['text']}")
    r, dt = await _call(llm.EVIDENCE_TASK, data, llm.SourceEvidence, fast=True)
    if r is None:
        return None, dt
    got = {it.claim: it for it in r.items}
    marks = []
    for i, want in enumerate(case["expect"]):
        it = got.get(i)
        stance = it.stance if it else "no_relacionada"
        quote_ok = stance == "no_relacionada" or bool(it and quote_in_page(it.quote, case["text"]))
        party_ok = "party" not in case or bool(it and it.is_party) == case["party"]
        marks.append({"ok": stance == want and party_ok, "quote": quote_ok, "answer": stance})
    return marks, dt


def _image(lines):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.load_default(size=30)
    img = Image.new("RGB", (900, 90 + 48 * len(lines)), "white")
    d = ImageDraw.Draw(img)
    for n, line in enumerate(lines):
        d.text((36, 40 + 48 * n), line, font=font, fill=(20, 20, 20))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


async def imagenes(case):
    r, dt = await _call(llm.IMAGE_TASK, "(imagen adjunta)", llm.ImageReading, image_b64=_image(case["lines"]), vision=True)
    if r is None:
        return None, dt
    ratio = difflib.SequenceMatcher(None, _norm(" ".join(case["lines"])), _norm(r.text)).ratio()
    ok = ratio >= 0.95 and r.kind in case["kinds"]
    return [{"ok": ok, "answer": f"{'leído' if ok else f'{ratio:.0%}'} {r.kind}"}], dt


async def extraccion(case):
    task = llm.EXTRACT_TASK.replace("{wb}", "[]")
    r, dt = await _call(task, case["text"], llm.Extraction)
    if r is None:
        return None, dt
    ok = r.input_kind in case["kind"]
    if case.get("no_claims"):
        ok = ok and not r.claims
    central = next((c for c in r.claims if c.central), r.claims[0] if r.claims else None)
    text = (central.text if central else "").lower()
    ok = ok and all(re.search(p, text) for p in case.get("central_has", []))
    ok = ok and not any(re.search(p, text) for p in case.get("central_lacks", []))
    return [{"ok": ok, "answer": f"{r.input_kind} · {text[:70]}"}], dt


async def veredicto(case):
    payload = [{"index": 0, "afirmacion": case["claim"], "central": True, "se_refiere_a": None, "evidencia": case["evidence"]}]
    r, dt = await _call(llm.VERDICT_TASK, json.dumps(payload, ensure_ascii=False), llm.Verdict)
    if r is None:
        return None, dt
    rating = next((c.rating for c in r.claims if c.index == 0), "sin_pruebas")
    return [{"ok": rating in case["ok"], "answer": rating}], dt


STAGES = {"fuentes": (fuentes, cases.SOURCES, "OPENROUTER_FAST_MODEL"),
          "imagenes": (imagenes, cases.IMAGES, "OPENROUTER_VISION_MODEL"),
          "extraccion": (extraccion, cases.EXTRACTION, "OPENROUTER_MODEL"),
          "veredicto": (veredicto, cases.VERDICT, "OPENROUTER_MODEL")}


async def evaluate(stage: str, model: str) -> dict:
    fn, items, setting = STAGES[stage]
    setattr(settings, setting, model)
    rows, calls, valid, lat = [], 0, 0, []
    for case in items:
        runs = []
        for _ in range(RUNS):
            marks, dt = await fn(case)
            calls += 1
            lat.append(dt)
            if marks is not None:
                valid += 1
            runs.append(marks)
        rows.append({"case": case, "runs": runs})
    marks = [m for row in rows for run in row["runs"] if run for m in run]
    critical = [row["case"]["id"] for row in rows if row["case"].get("critical")
                and any(run is None or not all(m["ok"] for m in run) for run in row["runs"])]
    def answers(run):
        return [m["answer"] for m in run] if run else None
    agree = [answers(row["runs"][0]) is not None and answers(row["runs"][0]) == answers(row["runs"][1]) for row in rows]
    score = {"valid": valid / calls, "accuracy": sum(m["ok"] for m in marks) / len(marks) if marks else 0.0,
             "consistency": sum(agree) / len(agree)}
    if stage == "fuentes":
        score["quotes"] = sum(m["quote"] for m in marks) / len(marks) if marks else 0.0
    bar = BAR[stage]
    passed = not critical and all(score[k] >= v for k, v in bar.items())
    return {"stage": stage, "model": model, "date": datetime.now().date().isoformat(), "score": score, "bar": bar,
            "critical": critical, "passed": passed, "usd": round(sum(SPENT), 4), "latency_p50": round(statistics.median(lat), 1),
            "throttled": THROTTLED[0],
            "cases": [{"id": row["case"]["id"], "answers": [[m["answer"] for m in run] if run else "formato inválido"
                                                             for run in row["runs"]]} for row in rows]}


def report(res: dict) -> str:
    s, b = res["score"], res["bar"]
    lines = [f"## {res['stage']} · `{res['model']}` · {res['date']}", "",
             f"**{'ELEGIBLE' if res['passed'] else 'NO ELEGIBLE'}** · costo de la prueba US${res['usd']} · latencia mediana {res['latency_p50']} s"
             f" · saturado (429) {res['throttled']} veces", "",
             "| Métrica | Resultado | Mínimo |", "|---|---|---|"]
    lines += [f"| {k} | {s[k]:.0%} | {b[k]:.0%} |" for k in b]
    lines.append(f"| errores críticos | {', '.join(res['critical']) or 'ninguno'} | ninguno |")
    lines += ["", "| Caso | Respuestas (2 corridas) |", "|---|---|"]
    lines += [f"| {c['id']} | {' / '.join(map(str, c['answers']))} |" for c in res["cases"]]
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--stage", required=True, choices=list(STAGES))
    p.add_argument("--model", required=True)
    a = p.parse_args()
    if "," in a.model:
        sys.exit("El estándar evalúa un solo modelo por corrida, sin respaldo.")
    if not settings.OPENROUTER_API_KEY:
        sys.exit("Falta OPENROUTER_API_KEY.")
    res = asyncio.run(evaluate(a.stage, a.model))
    text = report(res)
    print(text)
    RESULTS.mkdir(exist_ok=True)
    name = f"{res['date']}-{a.stage}-{re.sub(r'[^a-z0-9.-]+', '_', a.model.lower())}"
    (RESULTS / f"{name}.md").write_text(text)
    (RESULTS / f"{name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    sys.exit(0 if res["passed"] else 1)


if __name__ == "__main__":
    main()
