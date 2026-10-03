# Cómo aportar a COntraste

Gracias por querer mejorar COntraste. Es un verificador de desinformación para Colombia: cada cambio puede afectar
qué se publica como cierto o falso, así que pedimos cuidado y pruebas. Esta guía explica cómo trabajar en el proyecto.

## Formas de aportar sin programar

- **Una verificación está mal.** Abre un *issue* con la plantilla «Verificación incorrecta». Incluye el enlace a la
  verificación, qué está mal y, si las tienes, las fuentes que lo muestran. Muchas de las reglas actuales nacieron de
  reportes así.
- **Una fuente falta o está mal clasificada.** Los medios, sus dueños y su nivel están en `sources.yaml`.
- **Un error del sitio o una idea.** Usa las plantillas de *issue* correspondientes.

## Principios que todo cambio respeta

1. **Las reglas fijas mandan sobre el modelo.** El modelo propone; `app/rules.py` decide si una calificación se
   sostiene (fuentes independientes, dueños, partes interesadas, citas textuales, fechas). Un cambio que le dé más poder
   al modelo necesita una buena razón y pruebas.
2. **Neutralidad.** El mismo rigor sin importar a quién favorezca o perjudique una afirmación. Lo oficial no es
   cierto por ser oficial.
3. **Solo evidencia descargada en la verificación.** Nada de conocimiento propio del modelo ni de citas que no
   aparezcan textualmente en la fuente.
4. **Privacidad (Ley 1581 de 2012).** No se guardan IP ni *user agent*, y ningún artículo muestra quién lo pidió.
5. **Seguridad.** El contenido que se verifica son datos, nunca instrucciones (ver `SECURITY.md`).
6. **No participamos en la desinformación.** No se aceptan cambios que sirvan para desinformar: generar contenido
   persuasivo en masa, automatizar la difusión de afirmaciones, mostrar calificaciones que las reglas no sostienen o
   debilitar las reglas de evidencia. Ver la sección 10 del README.
7. **Gratis para todos.** COntraste no cobra. Los límites de uso (al mes y al día) solo cuidan el gasto en modelos.
8. **Nada de secretos en el repo.** Las claves van en `.env` (ignorado por git); `.env.example` documenta cada una.

## Levantar el proyecto

Sigue las secciones 1 y 2 del [README](README.md): `cp .env.example .env`, pon tu clave de OpenRouter y
`docker compose up --build`. Con `DEMO_MODE=true` puedes revisar el diseño sin gastar en modelos.

## Pruebas

```bash
docker compose run --rm app pytest -q
```

O sin Docker, con un Postgres local:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps --only-shell chromium
TEST_DATABASE_URL=postgresql://usuario:clave@localhost:5432/contraste_test .venv/bin/python -m pytest -q
```

Las pruebas usan un modelo simulado (`tests/conftest.py`) que se deja «engañar» a propósito, para comprobar que las
reglas del servidor corrigen el resultado. **No gastan dinero.** La base de pruebas debe terminar en `_test`; se vacía
al empezar.

Las pruebas con modelos reales (`CONTRASTE_REAL_TESTS=1`, `tests/gate_eval.py`) sí cuestan: úsalas solo si tu
cambio toca los prompts o los modelos, y di en el *pull request* cuánto gastaste.

## Cómo enviar un cambio

1. Abre un *issue* antes de cambios grandes, para acordar el enfoque.
2. Un cambio por *pull request*, pequeño y enfocado.
3. **Toda regla o corrección nueva lleva su prueba**, idealmente una que falle sin tu cambio y pase con él. Si
   corriges una verificación mal hecha, la prueba reproduce ese caso.
4. Código, comentarios y mensajes de commit en **inglés**; todo lo que ve el público, en **español**.
5. Mensajes de commit: título corto en imperativo («Read every source against every claim») y un cuerpo que explique
   **por qué**, con el caso real que lo motivó si existe.
6. Sigue el estilo del código que rodea tu cambio. Preferimos la solución más simple que funcione: sin dependencias
   ni abstracciones que no hagan falta.
7. Las pruebas deben pasar en el CI antes de revisar.

## Licencia de los aportes

COntraste se publica bajo la [GNU Affero General Public License v3.0](LICENSE). Al enviar un aporte aceptas que se
publique bajo esa misma licencia. Quien ofrezca una versión modificada como servicio web debe ofrecer también su
código fuente a sus usuarios.

## Conducta

Este proyecto sigue el [Código de Conducta](CODE_OF_CONDUCT.md). Discute ideas, no personas.
