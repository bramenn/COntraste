# Estándar para elegir modelos

COntraste no elige modelos por su fama ni por su origen, sino por cómo les va en este estándar. **Un modelo es elegible para una etapa cuando pasa todos los mínimos de esa etapa y no comete ningún error crítico.** Entre los elegibles se prefiere el más barato; a igual precio, el más rápido.

Cualquiera puede repetir la prueba con un solo comando y proponer otro modelo con el informe que genera.

## Etapas

| Etapa | Qué hace en COntraste | Variable |
|---|---|---|
| `fuentes` | Lee cada fuente y dice si confirma, contradice, da contexto o no tiene relación, con una cita textual. Es la mayoría de las llamadas. | `OPENROUTER_FAST_MODEL` |
| `imagenes` | Transcribe y describe capturas, titulares, cadenas y memes. | `OPENROUTER_VISION_MODEL` |
| `extraccion` | Separa lo que circula en afirmaciones verificables y marca la central. | `OPENROUTER_MODEL` |
| `veredicto` | Califica cada afirmación con la evidencia que le llega. | `OPENROUTER_MODEL` |

El filtro de entrada (Jev) tiene su propia prueba con casos reales: `tests/gate_eval.py`.

## Los casos

Están en [`cases.py`](cases.py). Los escribimos nosotros para este fin: son sintéticos y tienen la respuesta correcta conocida. Así el resultado no depende de buscadores, de las noticias del día ni de textos con derechos de autor, y es el mismo en cualquier equipo. Cada caso apunta a algo que un modelo debe hacer bien para que COntraste siga siendo honesto:

- **Fuentes:** confirmar y contradecir lo evidente; no tomar como desmentido lo que una fuente simplemente no menciona; marcar como no relacionada otra entidad con un nombre parecido (Bomberos de Bogotá frente a la Dirección Nacional de Bomberos) u otra persona con el mismo apellido; respetar "posibles irregularidades"; reconocer a una parte interesada; separar dos afirmaciones en una misma fuente; ignorar instrucciones escondidas en la fuente.
- **Imágenes:** leer completo el texto de un tuit, una cadena de WhatsApp, un titular y un meme.
- **Extracción:** distinguir una afirmación, una pregunta sobre un hecho, una tarea, un saludo y una opinión; conservar el atenuante ("posibles"); verificar lo que alguien dijo, no el hecho de que lo dijo.
- **Veredicto:** verdadero con dos fuentes independientes; falso cuando contradicen; sin pruebas sin evidencia o solo con contexto; el atenuante no baja la calificación; una frase tajante sin hechos no es verdadera; una mezcla es engañosa o con matices; ignorar instrucciones escondidas en la evidencia.

Los marcados como **críticos** no admiten ni un fallo, en ninguna de las dos corridas: inventar relación con otra entidad, obedecer una inyección, verificar una tarea, perder el atenuante, dar por cierto algo sin evidencia o desmentido.

## Los mínimos

Cada caso corre **dos veces**: un modelo también tiene que coincidir consigo mismo.

| Etapa | Respuestas válidas | Acierto | Citas textuales | Coincide consigo mismo | Errores críticos |
|---|---|---|---|---|---|
| `fuentes` | 98 % | 90 % | 90 % | 85 % | ninguno |
| `imagenes` | 98 % | 95 % | — | 85 % | ninguno |
| `extraccion` | 98 % | 85 % | — | 80 % | ninguno |
| `veredicto` | 98 % | 85 % | — | 80 % | ninguno |

- **Respuestas válidas:** respeta el formato JSON pedido, incluso después del reintento que COntraste hace.
- **Acierto:** la respuesta coincide con la correcta. En imágenes, al menos el 95 % del texto leído.
- **Citas textuales:** la cita que da el modelo aparece tal cual en la fuente; si no, COntraste la descarta.

Pasar el estándar es necesario, no suficiente: antes de cambiar el modelo en producción también se comparan veredictos sobre casos reales, como explica la sección «Por qué estos modelos» del [README](../README.md#modelos).

## Repetir la prueba

Solo hace falta una clave de OpenRouter. No hace falta base de datos ni buscadores.

```bash
pip install -r requirements.txt
OPENROUTER_API_KEY=sk-or-... python -m evals.run --stage fuentes --model qwen/qwen3.8-27b:free
```

Los modelos gratuitos comparten un cupo y a ratos responden «saturado» (error 429). Eso es disponibilidad, no calidad: la prueba espera y reintenta, y el informe dice cuántas veces pasó. En producción, un modelo gratuito siempre va con un respaldo de pago (ver abajo).

Imprime el informe, dice si el modelo es **ELEGIBLE**, y lo guarda en `evals/results/` en Markdown y JSON. Cada corrida cuesta centavos de dólar (los modelos gratuitos, nada).

## Modelos gratuitos y respaldo

Un modelo gratuito puede usarse si pasa el estándar, pero **siempre con un respaldo** que también lo haya pasado: cada variable acepta una lista separada por comas, y OpenRouter prueba el siguiente cuando uno está saturado o caído. Por ejemplo:

```
OPENROUTER_FAST_MODEL=nvidia/nemotron-3-super-120b-a12b:free,deepseek/deepseek-v4-flash
```

## Proponer un modelo

1. Corre el estándar en la etapa que quieres mejorar.
2. Abre un *issue* «Propuesta de mejora» o un *pull request* con el informe de `evals/results/`, el precio del modelo y por qué lo propones.
3. Si pasa y es más barato o mejor que el actual, lo comparamos con casos reales y lo adoptamos.

## Cambiar el estándar

Un caso nuevo vale más que mil opiniones: si encontraste algo que los modelos hacen mal y que importa para COntraste, agrégalo a `cases.py` con su respuesta correcta y explica en el *pull request* qué error real lo motivó. Los mínimos solo se cambian con una razón explícita, y el cambio se aplica a todos los modelos por igual, también a los que ya están en uso.

## Resultados vigentes

Están en [`results/`](results/). La tabla de modelos en uso está en el [README](../README.md#modelos).
