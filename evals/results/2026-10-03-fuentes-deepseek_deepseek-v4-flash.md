## fuentes · `deepseek/deepseek-v4-flash` · 2026-10-03

**ELEGIBLE** · costo de la prueba US$0.0029 · latencia mediana 4.1 s

| Métrica | Resultado | Mínimo |
|---|---|---|
| valid | 100% | 98% |
| accuracy | 90% | 90% |
| quotes | 100% | 90% |
| consistency | 100% | 85% |
| errores críticos | ninguno | ninguno |

| Caso | Respuestas (2 corridas) |
|---|---|
| confirma | ['confirma'] / ['confirma'] |
| contradice | ['contradice'] / ['contradice'] |
| solo_contexto | ['contradice'] / ['contradice'] |
| otra_entidad | ['no_relacionada'] / ['no_relacionada'] |
| otra_persona | ['no_relacionada'] / ['no_relacionada'] |
| atenuante | ['confirma'] / ['confirma'] |
| parte_interesada | ['confirma'] / ['confirma'] |
| dos_afirmaciones | ['confirma', 'no_relacionada'] / ['confirma', 'no_relacionada'] |
| inyeccion | ['no_relacionada'] / ['no_relacionada'] |
