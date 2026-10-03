# Seguridad

## Cómo reportar una vulnerabilidad

**No abras un *issue* público.** Repórtala en privado con el botón «Report a vulnerability» de la pestaña
*Security* de este repositorio, o escribe a bramendev@gmail.com con el asunto «Seguridad Contraste».

Incluye qué encontraste, cómo reproducirlo y qué impacto crees que tiene. Respondemos en un máximo de 7 días y te
mantenemos al tanto hasta corregirlo. Si quieres, te damos crédito en el cambio que lo corrige.

Prueba solo contra tu propia instalación (`docker compose up`), nunca contra https://contraste.bramen.org ni contra
cuentas o datos de otras personas.

## Qué nos interesa especialmente

- **Manipular una verificación:** hacer que algo salga calificado distinto de lo que dice la evidencia, por
  ejemplo con instrucciones ocultas en el contenido o en una página fuente (*prompt injection*), citas inventadas que
  pasen la validación o fuentes que se hagan pasar por otras.
- **Límites de uso:** pedir verificaciones por encima del límite mensual o diario, o recuperar verificaciones gastadas.
- **SSRF:** que el servidor descargue direcciones internas o de metadatos de la nube.
- **Cuentas y sesiones:** suplantación, fijación de sesión, CSRF, enlaces de entrada reutilizables.
- **Datos personales:** cualquier forma de saber quién pidió una verificación, o de recuperar IP o *user agent*.
- **Gasto en modelos:** formas de hacer que el servicio gaste en modelos sin límite.

## Fuera de alcance

Ataques de denegación de servicio por volumen, ingeniería social, y reportes automáticos sin una prueba de concepto.
