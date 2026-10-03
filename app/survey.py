"""Product survey, shown once the reader has seen the value (after their second check, or when the monthly
checks run out). It opens with Sean Ellis' question ("how would you feel if you could no longer use it?"): 40 %
or more "very disappointed" is the usual sign that the product is a must-have.
One answer per account, a few checks as thanks, all multiple choice except two optional text fields."""
import csv
import io

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import accounts, credits, db

router = APIRouter()

PMF = {"muy": "Muy decepcionado", "algo": "Algo decepcionado", "nada": "No me importaría", "no_uso": "Ya casi no lo uso"}
USES = {"antes_compartir": "Revisar algo antes de compartirlo", "responder": "Responder a alguien que compartió algo falso",
        "trabajo": "Mi trabajo (periodismo, docencia, investigación)", "curiosidad": "Curiosidad por un tema",
        "otro": "Otro"}
IMPROVE = {"rapidez": "Que sea más rápido", "fuentes": "Más fuentes o fuentes más confiables",
           "claridad": "Explicaciones más claras o más cortas", "redes": "Que lea mejor videos y redes sociales",
           "compartir": "Mejores tarjetas para compartir", "whatsapp": "Poder usarlo desde WhatsApp",
           "otro": "Otra cosa"}
MIN_CHECKS = 2  # ask after the second finished check: enough use to have an opinion


def reward_amount() -> int:
    return int(db.setting("survey_reward", 2))


def answered(user: dict) -> bool:
    return bool(db.q1("SELECT 1 FROM surveys WHERE user_id=%s", user["id"]))


def should_prompt(user: dict, balance: dict) -> bool:
    if answered(user):
        return False
    done = db.q1("SELECT COUNT(*) AS n FROM jobs WHERE user_id=%s AND kind='check' AND status='done'", user["id"])["n"]
    return done >= MIN_CHECKS or balance["total"] <= 0


def no_credits_response(user: dict) -> JSONResponse:
    """402 with where to send the reader: the survey (which gives a few checks) or their account, which says
    when the monthly checks come back."""
    nxt = "/cuenta?sin_creditos=1" if answered(user) else "/encuesta?sin_creditos=1"
    return JSONResponse({"error": "no_credits", "next": nxt}, status_code=402)


def validate(form) -> tuple[dict | None, str | None]:
    a = {"pmf": str(form.get("pmf", "")), "uses": [u for u in form.getlist("uses") if u in USES],
         "improve": [i for i in form.getlist("improve") if i in IMPROVE][:2],
         "benefit": str(form.get("benefit", "")).strip()[:500], "improve_text": str(form.get("improve_text", "")).strip()[:500]}
    if a["pmf"] not in PMF:
        return None, "Responde cómo te sentirías si ya no pudieras usar COntraste."
    if not a["uses"] or not a["improve"]:
        return None, "Elige para qué lo usas y qué deberíamos mejorar."
    return a, None


@router.get("/encuesta", response_class=HTMLResponse)
def survey_page(sin_creditos: str = "", gracias: str = ""):
    from .main import page
    user = accounts.CURRENT_USER.get()
    if not user:
        return RedirectResponse("/entrar?next=/encuesta", status_code=303)
    return page("encuesta.html", noindex=True, done=answered(user), thanks=bool(gracias), no_credits=bool(sin_creditos),
                reward=reward_amount(), PMF=PMF, USES=USES, IMPROVE=IMPROVE)


@router.post("/encuesta")
async def survey_submit(request: Request):
    from .main import page
    user = accounts.CURRENT_USER.get()
    form = await request.form()
    if not user or not accounts.csrf_ok(request, str(form.get("csrf", ""))):
        return RedirectResponse("/entrar?next=/encuesta", status_code=303)
    answers, err = validate(form)
    if err:
        return page("encuesta.html", status=400, noindex=True, error=err, form=form, reward=reward_amount(), PMF=PMF,
                    USES=USES, IMPROVE=IMPROVE)
    inserted = db.q1("INSERT INTO surveys(user_id, answers) VALUES(%s,%s) ON CONFLICT (user_id) DO NOTHING RETURNING id",
                     user["id"], db.Jsonb(answers))
    if inserted and (n := reward_amount()) > 0:
        credits.reward(user["id"], "survey:" + user["id"], "Gracias por responder la encuesta", amount=n, bucket="free")
    return RedirectResponse("/encuesta?gracias=1", status_code=303)


# --- Results for editors ----------------------------------------------------------------------------

def summary() -> dict:
    rows = [r["answers"] for r in db.q("SELECT answers FROM surveys ORDER BY created_at DESC")]
    n = len(rows)

    def count(key, labels, multi=False):
        c = {k: 0 for k in labels}
        for a in rows:
            for v in (a.get(key) or []) if multi else [a.get(key)]:
                if v in c:
                    c[v] += 1
        return [(labels[k], v, round(100 * v / n) if n else 0) for k, v in sorted(c.items(), key=lambda x: -x[1])]
    pmf = count("pmf", PMF)
    very = next((pct for label, _, pct in pmf if label == PMF["muy"]), 0)
    return {"n": n, "pmf": pmf, "very_pct": very, "uses": count("uses", USES, True),
            "improve": count("improve", IMPROVE, True),
            "texts": [a for a in rows if a.get("benefit") or a.get("improve_text")][:100]}


def to_csv() -> str:
    out = io.StringIO()
    w = csv.writer(out)
    cols = ["pmf", "uses", "improve", "benefit", "improve_text"]
    w.writerow(["fecha", *cols])
    for r in db.q("SELECT answers, created_at FROM surveys ORDER BY created_at"):
        a = r["answers"]
        w.writerow([r["created_at"].date().isoformat(),
                    *["|".join(a[c]) if isinstance(a.get(c), list) else a.get(c, "") for c in cols]])
    return out.getvalue()
