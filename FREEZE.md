# FREEZE.md — Congelamiento de lógica de trading (2026-10-15 a 2026-11-05)

**Pedido explícito del usuario, 2026-10-15** (verbatim): "Desde el deploy
de hoy, la lógica de trading queda CONGELADA por 3 semanas: solo bugs de
integridad de datos o de riesgo, nada de features ni cambios de
parámetros. Necesitamos una muestra con una sola versión. Todo lo demás
es análisis offline."

## Ventana

- **Inicio**: 2026-10-15 (fecha del deploy del bundle
  `ggal-bot-correcciones-oct15.bundle` que incluye este archivo, aplicado
  por el usuario "hoy después de las 17:00" según su mensaje). Si ese
  deploy termina ocurriendo otro día, esta ventana se corre esa misma
  cantidad de días — lo que importa es "3 semanas desde la versión que
  efectivamente queda corriendo en producción", no la fecha calendario en
  sí.
- **Fin**: 2026-11-05 (21 días corridos desde el inicio).
- **Objetivo**: una muestra de trades/resultados generada por UNA SOLA
  versión del código de decisión, sin contaminarla con cambios de
  parámetros o features a mitad de camino — cualquier análisis de
  performance durante esta ventana (ver Tarea 4, script de revisión
  semanal) asume implícitamente esto; si se rompe el congelamiento sin
  dejar constancia acá, ese análisis queda inválido sin que nadie se dé
  cuenta.

## Qué SÍ está permitido durante el congelamiento

1. **Bugs de integridad de datos**: cualquier caso donde el Event
   Journal, la reconciliación de posiciones, o cualquier log, represente
   mal la realidad (la clase de bug de GFGC6600OC, ver RESEARCH.md
   sección 6bis) — corregir esto es necesario para que la muestra sea
   confiable, no es una "feature".
2. **Bugs/guardias de riesgo que SOLO reducen exposición** — nunca abren
   operatoria nueva, nunca cambian cuándo/cuánto se entra. Ejemplos ya
   aplicados antes de esta ventana (mismo criterio si aparece algo
   similar durante ella): el respaldo de valor teórico del quote-gap
   backstop, la guardia de asignación anticipada sobre patas cortas ITM.
   Van **default ON** (nunca opt-in) por ser reducción de riesgo, igual
   que el resto del proyecto.
3. **Instrumentación/logging que no cambia ninguna decisión de trading**
   (nuevas columnas de log, diagnósticos de solo lectura, el propio
   script de revisión semanal de la Tarea 4) — esto es "análisis
   offline", explícitamente fuera del alcance del congelamiento según el
   pedido del usuario.

## Qué NO está permitido (queda para después del 2026-11-05)

- Nuevas estrategias o nuevas condiciones de entrada/salida.
- Cambios de parámetros en `config.py` que alteren CUÁNDO, CUÁNTO o QUÉ
  opera el bot (umbrales de dislocación de smile, bandas de moneyness,
  Stop Loss/Take Profit %, sizing, etc.) — aunque estén respaldados por
  nuevo análisis (ej. cualquier ajuste que surja de H1/H2 en
  RESEARCH.md).
- Activar un feature flag que hoy está en `False` y que cambia
  comportamiento de trading (lo que sí sigue permitido: activar un flag
  que sea puramente una guardia de riesgo default-ON, ver arriba — esos
  no son "features" en el sentido de esta regla).
- Cualquier cambio en `ggal_bot/strategy/*.py` que no sea una corrección
  de un bug de integridad/riesgo ya identificado y documentado.

## Registro de excepciones aplicadas durante la ventana

(Ninguna todavía — se agrega una fila por cada cambio de
código/parámetro que toque producción entre 2026-10-15 y 2026-11-05,
con fecha, archivo, commit y por qué categoriza como integridad de datos
o riesgo, no como feature/parámetro.)

| Fecha | Commit | Archivo(s) | Categoría | Justificación |
|---|---|---|---|---|
| — | — | — | — | — |
