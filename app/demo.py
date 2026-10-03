"""Sample case (DEMO_MODE and visual checks) plus 12 [EJEMPLO] articles for reviewing the portal."""
import random
from datetime import timedelta

from . import db
from .rules import source_info

ZULUAGA_TEXT = ("Despidieron a la periodista Camila Zuluaga de Blu Radio por criticar a Abelardo, pero durante "
                "los 4 años de Petro nunca peligró su programa.")


def _src(url, title, summary, stance, basis="reporte_periodistico"):
    tier, name, domain = source_info(url)
    return {"url": url, "title": title, "summary": summary, "stance": stance, "tier": tier, "name": name,
            "domain": domain, "basis": basis}


def zuluaga() -> dict:
    s = [
        _src("https://www.lasillavacia.com/en-vivo/caracol-television-termina-programa-de-camila-zuluaga-en-blu-radio/",
             "Caracol Televisión termina programa de Camila Zuluaga en Blu Radio",
             "Informa que Caracol Televisión terminó el programa y que la empresa lo explicó por audiencia y anunciantes. "
             "Recoge fuentes que hablan de inconformidad del Gobierno con el espacio; no hay confirmación oficial.",
             "contexto", "fuentes_anonimas"),
        _src("https://www.semana.com/nacion/articulo/se-acabo-el-programa-de-camila-zuluaga-y-ana-cristina-restrepo-en-blu-radio/202616/",
             "Se acabó el programa de Camila Zuluaga y Ana Cristina Restrepo en Blu Radio",
             "Reporta el final del programa que conducían Camila Zuluaga y Ana Cristina Restrepo.", "contexto"),
        _src("https://www.elespectador.com/el-magazin-cultural/blu-radio-confirma-que-mananas-blu-10-am-de-camila-zuluaga-sale-del-aire/",
             "Blu Radio confirma que Mañanas Blu de Camila Zuluaga sale del aire",
             "Cuenta que la emisora confirmó la salida del aire del programa. Habla del fin del espacio, no de un despido.",
             "contradice", "declaracion_oficial"),
        _src("https://www.colombia.com/entretenimiento/noticias/por-que-salio-camila-zuluaga-de-blu-radio-esto-se-sabe-sobre-su-inesperado-cambio-598551",
             "¿Por qué salió Camila Zuluaga de Blu Radio? Esto se sabe",
             "Resume lo que se sabe del cambio y recuerda que el programa estaba al aire desde 2018.", "contexto"),
        _src("https://www.publimetro.co/entretenimiento/2026/09/01/asi-fue-como-camila-zuluaga-se-entero-que-su-programa-no-continuaba-en-blu-radio/",
             "Así fue como Camila Zuluaga se enteró de que su programa no continuaba en Blu Radio",
             "Relata cómo la periodista supo que el programa no seguiría.", "contexto"),
        _src("https://www.infobae.com/colombia/2026/09/01/polemica-por-abrupta-salida-del-aire-de-programa-que-presentaban-camila-zuluaga-y-ana-cristina-restrepo-en-blu-radio-ataques-a-la-libertad-de-prensa/",
             "Polémica por abrupta salida del aire de programa de Camila Zuluaga y Ana Cristina Restrepo",
             "Recoge la polémica y las críticas que ven un ataque a la libertad de prensa; no aporta pruebas de una orden del presidente.",
             "contexto"),
    ]
    return {
        "title": "¿Despidieron a Camila Zuluaga de Blu Radio por criticar a Abelardo?",
        "topic": "medios", "rating": "enganoso",
        "headline": "Lo que terminó fue su programa, no hay confirmación de un despido y no está probado que la causa fueran sus críticas al presidente.",
        "circulating": "Despidieron a Camila Zuluaga de Blu Radio por criticar a Abelardo; con Petro su programa nunca peligró.",
        "input": {"kind": "image", "url": None},
        "claims": [
            {"text": "Camila Zuluaga fue despedida de Blu Radio.", "short": "La despidieron", "rating": "enganoso",
             "explanation": "Lo que se canceló fue su programa, Mañanas Blu 10:30, cuya última emisión fue el 31 de agosto de 2026. "
                            "Ningún medio confirma que la empresa la haya despedido.",
             "card_line": "No hay despido confirmado: se canceló su programa",
             "proven": "El programa Mañanas Blu 10:30 salió del aire el 31 de agosto de 2026.",
             "sources_say": "Los medios hablan del fin del programa, no de un despido.",
             "evidence": [{"source": 2, "stance": "contradice"}, {"source": 1, "stance": "contexto"}, {"source": 4, "stance": "contexto"}]},
            {"text": "La salida se debió a sus críticas al presidente Abelardo.", "short": "Por criticar a Abelardo",
             "rating": "sin_pruebas",
             "explanation": "Caracol Televisión lo atribuyó a audiencia y anunciantes. La Silla Vacía reportó fuentes que hablan de "
                            "inconformidad del Gobierno, pero ningún medio confirma una intervención del presidente.",
             "card_line": "No está probado que fuera por criticar al presidente",
             "proven": "La empresa dio como razones la audiencia y los anunciantes.",
             "sources_say": "Hay versiones de fuentes anónimas sobre molestia del Gobierno, sin confirmar.",
             "evidence": [{"source": 0, "stance": "contexto"}, {"source": 5, "stance": "contexto"}]},
            {"text": "Durante los cuatro años del gobierno Petro su programa nunca peligró.", "short": "Con Petro nunca peligró",
             "rating": "matices",
             "explanation": "El programa existía desde 2018 y siguió al aire durante ese gobierno. Decir que ahora hay censura es una "
                            "conclusión de opinión, no un hecho comprobado.",
             "card_line": "Con Petro siguió al aire, pero eso no prueba censura",
             "proven": "El programa estuvo al aire desde 2018 hasta agosto de 2026.",
             "sources_say": "Los medios confirman la continuidad del programa en ese periodo.",
             "evidence": [{"source": 3, "stance": "confirma"}, {"source": 1, "stance": "contexto"}]},
        ],
        "not_verifiable": [{"text": "La imagen del meme que acompaña el mensaje.", "kind": "satira",
                            "note": "Es un montaje satírico; no muestra un hecho."}],
        "timeline": [{"date": "2018", "event": "Empieza a emitirse el programa en Blu Radio.", "sources": [3]},
                     {"date": "31 ago 2026", "event": "Última emisión de Mañanas Blu 10:30.", "sources": [2, 1]},
                     {"date": "1 sep 2026", "event": "Medios reportan la salida del aire y la polémica por libertad de prensa.",
                      "sources": [5, 0]}],
        "sources": s, "omitted": [], "notes": [], "security": {"input_injection": False, "page_injections": 0},
        "media": {"thumb": None, "original_url": None, "transcript": None},
        "reviewed_at": "2026-09-01T15:00:00+00:00",
    }


# (title, what circulates, topic, rating, headline, [(short, rating, finding)], domains)
EXAMPLES = [
    ("El Gobierno eliminará el pago en efectivo desde enero", "Desde enero ya no se podrá pagar en efectivo en Colombia por orden del Gobierno.",
     "economia", "falso", "No existe ninguna norma que elimine el efectivo; el Banco de la República sigue emitiendo billetes.",
     [("Se eliminará el efectivo", "falso", "Ninguna norma elimina el pago en efectivo")], ["banrep.gov.co", "colombiacheck.com", "eltiempo.com"]),
    ("La vacuna contra el dengue causa infertilidad", "La nueva vacuna contra el dengue deja estériles a las mujeres.",
     "salud", "falso", "Los estudios de la vacuna no muestran efectos sobre la fertilidad.",
     [("Causa infertilidad", "falso", "No hay evidencia de efectos en la fertilidad")], ["ins.gov.co", "minsalud.gov.co", "factual.afp.com"]),
    ("El desempleo bajó a un dígito en el último trimestre", "El desempleo en Colombia bajó a un dígito según el DANE.",
     "economia", "verdadero", "El DANE reportó una tasa de desempleo de un dígito para el trimestre.",
     [("Desempleo de un dígito", "verdadero", "El DANE reportó una tasa menor al 10 %")], ["dane.gov.co", "elespectador.com", "semana.com"]),
    ("La Registraduría cambió puestos de votación sin avisar", "La Registraduría movió el puesto de votación de millones de personas sin avisarles.",
     "elecciones", "enganoso", "Hubo traslados de puestos, pero fueron anunciados y afectan a muchas menos personas.",
     [("Hubo cambios de puestos", "verdadero", "Sí hubo traslados de algunos puestos"),
      ("Fueron sin avisar", "falso", "Los cambios se publicaron con anticipación")], ["registraduria.gov.co", "lasillavacia.com", "elcolombiano.com"]),
    ("Bogotá tendrá racionamiento de agua todo el próximo año", "Bogotá tendrá racionamiento de agua durante todo el próximo año.",
     "otro", "sin_pruebas", "No hay ningún anuncio oficial que lo confirme ni que lo descarte por completo.",
     [("Racionamiento todo el año", "sin_pruebas", "No hay anuncio oficial en ningún sentido")], ["colombiacheck.com", "eltiempo.com", "caracol.com.co"]),
    ("Video de saqueos en Cali es de esta semana", "Video muestra saqueos en Cali esta semana.",
     "seguridad", "enganoso", "El video es real, pero fue grabado años atrás y circula como si fuera reciente.",
     [("Los saqueos ocurrieron", "verdadero", "El video es real"), ("Son de esta semana", "falso", "Fue grabado en 2021")],
     ["colombiacheck.com", "elpais.com.co", "efe.com"]),
    ("El salario mínimo subirá 20 % por decreto", "El Gobierno ya decidió que el salario mínimo subirá 20 % por decreto.",
     "economia", "sin_pruebas", "La negociación no ha terminado y no hay decreto publicado.",
     [("Subirá 20 %", "sin_pruebas", "No hay decreto ni acuerdo publicado")], ["presidencia.gov.co", "lafm.com.co", "infobae.com"]),
    ("Un canal de televisión anunció su cierre definitivo", "Un canal nacional anunció que cierra definitivamente este mes.",
     "medios", "falso", "El canal desmintió el cierre y sigue con su programación.",
     [("Anunció su cierre", "falso", "El canal desmintió el cierre")], ["flip.org.co", "elheraldo.co", "wradio.com.co"]),
    ("El Congreso aprobó eliminar las primas", "El Congreso aprobó la reforma que elimina las primas de los trabajadores.",
     "politica", "falso", "Ningún proyecto aprobado elimina las primas; el texto se refiere a otro tema.",
     [("Se aprobó eliminar las primas", "falso", "Ninguna ley aprobada elimina las primas")], ["secretariasenado.gov.co", "colombiacheck.com", "cambiocolombia.com"]),
    ("Meme: el café será gratis en todo el país", "Imagen dice que el café será gratis en todo el país desde mañana.",
     "politica", "no_verificable", "Es una imagen satírica; no presenta un hecho que se pueda verificar.",
     [], ["colombiacheck.com", "lasillavacia.com", "semana.com"]),
    ("Los homicidios aumentaron en el primer semestre", "Los homicidios aumentaron en el primer semestre frente al año anterior.",
     "seguridad", "matices", "Las cifras oficiales muestran un aumento, aunque menor al que se menciona y no en todas las regiones.",
     [("Aumentaron los homicidios", "matices", "Aumento leve, no en todas las regiones")], ["medicinalegal.gov.co", "fiscalia.gov.co", "elespectador.com"]),
    ("La ley seca empieza el sábado antes de elecciones", "La ley seca por las elecciones empieza desde el sábado a las 6 p. m.",
     "elecciones", "verdadero", "El decreto de orden público fija la ley seca desde el sábado a las 6 p. m.",
     [("Ley seca desde el sábado", "verdadero", "El decreto fija la hora de inicio")], ["registraduria.gov.co", "noticiascaracol.com", "rcnradio.com"]),
]

STANCE_FOR = {"verdadero": "confirma", "matices": "confirma", "falso": "contradice", "enganoso": "contexto",
              "sin_pruebas": "contexto", "no_verificable": "contexto"}


def example(i: int) -> dict:
    title, circ, topic, rating, headline, claims, domains = EXAMPLES[i]
    sources = [_src(f"https://www.{d}/", f"[EJEMPLO] Documento de referencia {n + 1}",
                    "Resumen de ejemplo para revisar el diseño; no corresponde a un artículo real.",
                    STANCE_FOR[rating], "dato_oficial" if source_info(f"https://{d}/")[0] == 1 else "reporte_periodistico")
               for n, d in enumerate(domains)]
    return {
        "title": f"[EJEMPLO] {title}", "topic": topic, "rating": rating, "headline": headline, "circulating": circ,
        "input": {"kind": "text", "url": None},
        "claims": [{"text": s, "short": s, "rating": r, "explanation": f"{found}. (Texto de ejemplo.)", "card_line": found,
                    "proven": found, "sources_say": "Las fuentes de ejemplo coinciden en esto.",
                    "evidence": [{"source": k, "stance": STANCE_FOR[r]} for k in range(len(sources))]}
                   for s, r, found in claims],
        "not_verifiable": [{"text": circ, "kind": "satira", "note": "Es sátira (ejemplo)."}] if rating == "no_verificable" else [],
        "timeline": [], "sources": sources, "omitted": [], "notes": [],
        "security": {"input_injection": False, "page_injections": 0},
        "media": {"thumb": None, "original_url": None, "transcript": None},
    }


def seed():
    """Load the real case and the 12 examples once (DEMO_MODE only)."""
    if db.q1("SELECT 1 FROM articles WHERE demo"):
        return
    from .similar import text_key
    rnd = random.Random(7)
    z = zuluaga()
    zid = db.save_article(z, status="listed", reason=None, keys={"text_key": text_key(ZULUAGA_TEXT)}, demo=True,
                          created_at="2026-09-01T15:00:00+00:00")
    for i in range(len(EXAMPLES)):
        created = db.now() - timedelta(hours=rnd.randint(1, 120))
        r = example(i)
        r["reviewed_at"] = db.iso(created)
        aid = db.save_article(r, status="listed", reason=None, keys={}, demo=True, created_at=db.iso(created))
        for _ in range(rnd.randint(0, 40)):
            db.q("INSERT INTO consultations VALUES(%s,%s)", aid, db.iso(db.now() - timedelta(minutes=rnd.randint(1, 1400))))
        for h in range(24):
            if rnd.random() < 0.5:
                hour = (db.now() - timedelta(hours=h)).strftime("%Y-%m-%dT%H")
                db.q("""INSERT INTO article_views_hourly VALUES(%s,%s,%s)
                        ON CONFLICT (article_id, hour) DO UPDATE SET views=EXCLUDED.views""", aid, hour, rnd.randint(1, 60))
    db.recompute_scores()
    return zid
