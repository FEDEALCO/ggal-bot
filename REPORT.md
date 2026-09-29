# REPORT.md — GGAL_BOT: análisis de rentabilidad neta de costos

_Última actualización: 2026-09-29 (Fase 0 — baseline)._

Este documento se va completando estrategia de fase en fase, según la metodología acordada
(Fase 0 → baseline; Fase 1 → mitigar riesgos identificados; Fase 2 → validación walk-forward /
out-of-sample; Fase 3 → control de overfitting; Fase 4 → nuevas mejoras). Cada sección nueva se
agrega, nunca se reescribe una anterior salvo para corregir un error.

---

## ⚠️ HALLAZGO PRINCIPAL (Fase 0 — leer primero)

**Las TRES estrategias (`weekly_asymmetric`, `scalping`, `vol_arbitrage`) dan PnL neto de costos
NEGATIVO durante toda la ventana observada, en los 4 escenarios de costo evaluados — incluso en
el escenario más optimista posible (`mid_sin_spread`: solo comisión + derecho de mercado + IVA,
CERO costo de cruce de spread).**

| Estrategia | Ventana | N trades | PnL bruto (ARS) | PnL neto @ mid_sin_spread (ARS) | Costo como % del \|bruto\| @ mid_sin_spread |
|---|---|---:|---:|---:|---:|
| `weekly_asymmetric` | 2026-09-09 a 2026-09-25 (17 días) | 199 | **-86.637** | **-720.770** | 731,9% |
| `scalping` | 2026-09-11 a 2026-09-25 (15 días) | 214 | **+188.246** | **-53.796** | 128,6% |
| `vol_arbitrage` | 2026-09-04 a 2026-09-25 (22 días) | 577 | **-698.093** | **-1.879.222** | 169,2% |

`scalping` es la única de las tres con PnL **bruto** positivo, pero el costo regulatorio mínimo
(comisión + derecho de mercado + IVA, ~1,7% round-trip sobre el nocional, sin siquiera contar
spread) ya alcanza para volverla neta negativa. Con cualquier supuesto de spread por encima de
cero (3%, 6%, 10% round-trip) las tres estrategias empeoran sustancialmente y el % de trades
ganadores netos colapsa a valores de un solo dígito.

**Esto NO significa que las estrategias sean necesariamente inviables en vivo** — ver limitaciones
metodológicas abajo (ventana de 15-22 días, un solo régimen de mercado, intervalos de confianza
muy anchos). Significa que, con la evidencia disponible hoy, **ninguna de las tres estrategias
tiene un caso demostrado de rentabilidad neta de costos**, y que activar cualquier mejora en vivo
sin resolver esto primero sería prematuro.

---

## 1. Alcance de la Fase 0

Medir el desempeño de las 3 estrategias **tal como operaron durante la ventana de los exports
disponibles** (con las 8 mejoras del 2026-09-28 apagadas — es decir, exactamente el
comportamiento real registrado), bajo costos BYMA/IOL reales superpuestos, para saber cuál es
rentable neta de costos ANTES de evaluar ninguna mejora.

### Datos usados

| Fuente | Cobertura | Filas | Estrategias |
|---|---|---:|---|
| `export-lifecycle.csv` (Position Lifecycle Event Journal) | 2026-09-09/11 a 2026-09-25 | 1.333 eventos | `weekly_asymmetric`, `scalping` |
| `export.csv` (reconstrucción de cierres, `dashboard/pnl_engine.py::match_trades_fifo`) | 2026-09-04 a 2026-09-25 | 577 trades ya apareados | `vol_arbitrage` |

No existe ningún otro historial de mercado accesible (ver Sección 5, Limitaciones) — `market_snapshots.csv`
y `position_events.csv` en el repo tienen encabezado pero **0 filas de datos reales** al momento de
este análisis.

---

## 2. Metodología

### 2.1 Reconstrucción de trades

`ggal_bot/backtest/reconstruct.py` reconstruye cada posición cerrada **preservando cada pata
(fill) por separado** — no solo una entrada y una salida agregadas — para poder cobrarle a cada
fill su propia comisión + derecho de mercado + IVA, tal como ocurriría en la cuenta real. Replica
la misma matemática que `ggal_bot/portfolio/lifecycle.py::build_episode_lifecycles` (promedio
ponderado de entrada, PnL = Σ sobre patas de salida de `(precio_salida - entrada_promedio) * qty
* multiplicador`), pero sin llamarla directamente, precisamente para no perder el detalle por pata.

Para `vol_arbitrage` el PnL bruto de cada trade se **recalculó de forma independiente** a partir
de precio/cantidad/multiplicador/dirección y se cruzó contra el PnL ($) que trae el propio CSV;
toda fila donde ambos no coinciden dentro de una tolerancia de redondeo se excluye y se cuenta
(nunca se usa un número sin verificar contra sus propios insumos).

**Corrección de un gap de datos detectado durante esta Fase 0:** al agrupar las 218 posiciones
únicas de `weekly_asymmetric` por `Position ID`, se encontró que 9 de ellas tienen un evento
`CLOSE` dentro de la ventana del export pero **ningún evento `ENTRY`/`ADD`** (posiciones abiertas
antes de que empezara la ventana del export — "legacy"). La primera versión de este código las
descartaba en silencio sin contarlas en ningún lado. Se corrigió agregando un contador explícito
(`n_excluded_incomplete_data`), distinto de "todavía abierta" — ahora `trades + still_open +
incomplete_data` suman exactamente el total de `Position ID` únicos vistos, sin overlap y sin
pérdida silenciosa de datos. Este gap **no afecta los números de PnL reportados** (las 9
posiciones simplemente no tienen un precio de entrada conocido con el cual calcular su PnL — no
se les fabricó ninguno), pero sí afecta la interpretación de la muestra: el N real de posiciones
que pasaron por el bot en la ventana es 218, no 199+10=209. `scalping` y `vol_arbitrage` no
mostraron este gap (0 posiciones incompletas en ambas).

### 2.2 Modelo de costos (`ggal_bot/backtest/costs.py`)

Fuentes citadas y ambigüedad explícita documentada en el docstring del módulo (relevadas por
búsqueda web el 2026-09-29):

- **Comisión de bróker (IOL):** tarifario público, 3 escalas por volumen mensual — Gold 0,50%
  (usada por defecto, la más conservadora), Platinum 0,30%, Black 0,10% — aplica sobre el monto de
  cada operación (compra y venta por separado), para opciones igual que para acciones/CEDEARs.
- **Derecho de mercado BYMA (opciones):** PDF público vigente desde 2026-06-09. Se usa el bucket
  "Privados" (0,20%) por ser el más cercano estructuralmente a una opción sobre una acción privada
  como GGAL y el más conservador de los aplicables — el PDF no tiene una fila separada
  "Privados-Acciones" distinta de "Privados-CEDEAR" (ambigüedad no resuelta, documentada).
- **IVA:** 21% (alícuota general de Argentina) sobre comisión + derecho de mercado.
- **Costo de cruce de spread:** NO existe ningún historial de bid/ask punto a punto al momento de
  cada fill pasado (los exports registran el precio de referencia/mid). Se modela como una
  **banda de sensibilidad explícita** (0% / 3% / 6% / 10% round-trip) en vez de presentar un
  número inventado como medición exacta — calibrada contra el propio umbral de diseño del bot
  (`execution_cost_max_pct=8%`), no contra una observación de mercado en vivo.

Costo regulatorio por pata: `(comisión + derecho_mercado) × (1 + IVA)` = `(0,50% + 0,20%) × 1,21`
= **0,847% por pata**, aplicado sobre el nocional de cada pata (precio × cantidad × multiplicador
de 100 para opciones). Round-trip mínimo (sin spread): **~1,69%** del nocional.

### 2.3 Métricas y bootstrap (`ggal_bot/backtest/metrics.py`)

Todas las métricas pedidas se reportan siempre juntas: win rate, avg win/loss, payoff ratio,
expectancy, PnL bruto/neto, costo como % del PnL bruto, Sharpe y Sortino (anualizados), max
drawdown + tiempo de recuperación, peor día y peor semana.

- **Win rate, expectancy, Sharpe y Sortino** llevan intervalo de confianza del 95% por
  **bootstrap i.i.d. de trades** (remuestreo con reemplazo, 10.000 resamples, semilla fija 1234
  para reproducibilidad).
- **Max drawdown y tiempo de recuperación NO se bootstrapean** — el bootstrap i.i.d. destruye el
  orden temporal, que es precisamente lo que define un drawdown. Se reportan como punto único,
  con la advertencia de que su incertidumbre real es al menos tan grande como la de las demás
  métricas.
- **Sharpe/Sortino "anualizados"**: no existe una serie de retornos periódicos de cuenta (no se
  conoce el capital real). Se calculan sobre retornos por trade y se anualizan escalando por
  `sqrt(trades_por_año_observado)`, una **extrapolación de la frecuencia ya vista en la muestra**,
  no una proyección de que esa frecuencia se sostendrá. Con 15-22 días de datos esto es, por
  construcción, una anualización de altísima incertidumbre.
- **Max drawdown** se reporta en ARS (curva de equity acumulada); en % solo cuando el pico previo
  a la caída fue positivo (si nunca fue positivo, no hay una base sensata para expresar %).

---

## 3. Resultados — tabla completa (12 filas = 3 estrategias × 4 escenarios de costo)

_CSV completo con todas las columnas (incluyendo intervalos de confianza): `fase0_results.csv`,
adjunto en esta conversación._

| Estrategia | Escenario costo | N | Abierto | Incompl. | Win% neto | PnL neto (ARS) | PnL bruto (ARS) | Costo % \|bruto\| | Sharpe an. | Sortino an. | MaxDD (ARS) | Recup. |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| weekly_asymmetric | mid_sin_spread | 199 | 10 | 9 | 4,52% | -720.770 | -86.637 | 731,9% | -35,7 | -32,3 | -741.256 | no recuperó |
| weekly_asymmetric | spread_3pct | 199 | 10 | 9 | 0,50% | -1.843.792 | -86.637 | 2028,2% | -93,6 | -54,0 | -1.859.187 | no recuperó |
| weekly_asymmetric | spread_6pct | 199 | 10 | 9 | 0,50% | -2.966.813 | -86.637 | 3324,4% | -153,3 | -60,3 | -2.977.118 | no recuperó |
| weekly_asymmetric | spread_10pct | 199 | 10 | 9 | 0,50% | -4.464.175 | -86.637 | 5052,7% | -235,8 | -63,0 | -4.467.693 | no recuperó |
| scalping | mid_sin_spread | 214 | 0 | 0 | 21,50% | -53.796 | +188.246 | 128,6% | -6,2 | -13,1 | -94.095 | no recuperó |
| scalping | spread_3pct | 214 | 0 | 0 | 9,35% | -482.441 | +188.246 | 356,3% | -48,7 | -51,1 | -460.630 | no recuperó |
| scalping | spread_6pct | 214 | 0 | 0 | 7,48% | -911.087 | +188.246 | 584,0% | -92,4 | -61,5 | -874.919 | no recuperó |
| scalping | spread_10pct | 214 | 0 | 0 | 3,74% | -1.482.614 | +188.246 | 887,6% | -152,9 | -66,7 | -1.427.305 | no recuperó |
| vol_arbitrage | mid_sin_spread | 577 | 0 | 0 | 15,94% | -1.879.222 | -698.093 | 169,2% | -22,4 | -24,1 | -1.491.475 | no recuperó |
| vol_arbitrage | spread_3pct | 577 | 0 | 0 | 7,80% | -3.970.948 | -698.093 | 468,8% | -56,3 | -51,2 | -3.097.235 | no recuperó |
| vol_arbitrage | spread_6pct | 577 | 0 | 0 | 5,55% | -6.062.674 | -698.093 | 768,5% | -91,2 | -68,2 | -4.702.996 | no recuperó |
| vol_arbitrage | spread_10pct | 577 | 0 | 0 | 3,47% | -8.851.642 | -698.093 | 1168,0% | -139,4 | -80,7 | -6.844.010 | no recuperó |

_"Abierto" = posiciones sin evento CLOSE en la ventana (nunca se les fabricó un cierre). "Incompl."
= posiciones con CLOSE pero sin ENTRY dentro de la ventana (legacy, ver §2.1) — excluidas, nunca
fabricadas._

### Intervalos de confianza (bootstrap 95%, escenario `mid_sin_spread`)

| Estrategia | Win% neto (IC 95%) | Expectancy ARS/trade (IC 95%) | Sharpe an. (IC 95%) |
|---|---|---|---|
| weekly_asymmetric | 4,52% (2,01% – 7,54%) | -3.622 (-4.723 – -2.875) | -35,7 (-2,06 – -0,40) |
| scalping | 21,50% (15,89% – 27,10%) | -251 (-731 – +301) | -6,2 (-0,29 – +0,05) |
| vol_arbitrage | 15,94% (13,00% – 19,06%) | -3.257 (-4.424 – -2.239) | -22,4 (-0,29 – -0,17) |

Notar que en `scalping` el intervalo de expectancy y de Sharpe **cruza el cero** en el escenario
más optimista — es la única de las tres donde, dado el ruido de la muestra, no se puede descartar
con confianza que el resultado neto sea aproximadamente breakeven en ese escenario particular. En
los otros dos escenarios y en las otras dos estrategias, los intervalos son negativos de punta a
punta.

---

## 4. Interpretación por estrategia

**`weekly_asymmetric`**: PnL bruto ya es ligeramente negativo (-86.637 ARS sobre 199 trades,
-435 ARS promedio/trade) antes de aplicar ningún costo — el borde (edge) bruto de la estrategia en
esta ventana es, en el mejor de los casos, marginal. Encima de un costo regulatorio mínimo de
~1,7% round-trip sobre un nocional promedio de ~188.000 ARS por trade (~3.200 ARS de costo por
trade), el resultado neto se vuelve fuertemente negativo incluso sin spread.

**`scalping`**: única con PnL bruto positivo (+188.246 ARS / +880 ARS promedio/trade), pero el
nocional promedio por trade es menor (~66.000 ARS) y el edge bruto por trade es pequeño en
relación al costo regulatorio fijo (~1,7% de 66.000 ≈ 1.100 ARS), que en muchos trades individuales
supera el PnL bruto de ese trade — de ahí el colapso del win rate neto (47,7% bruto → 21,5% neto en
el escenario más optimista).

**`vol_arbitrage`**: PnL bruto claramente negativo (-698.093 ARS / -1.210 ARS promedio/trade sobre
577 trades), la muestra más grande de las tres. Es también la única con un N de trades grande, por
lo que sus intervalos de confianza son los más angostos relativos a su magnitud — el resultado
negativo en el escenario `mid_sin_spread` es el que tiene más sustento estadístico de los tres
(IC de Sharpe: -0,29 a -0,17, sin cruzar cero).

---

## 5. Limitaciones metodológicas explícitas (leer antes de sacar conclusiones)

1. **Ventana de 15-22 días, un solo régimen de mercado.** Esto NO alcanza para activar ninguna
   flag en vivo ni para descartar definitivamente ninguna estrategia. Se reporta con intervalos de
   confianza amplios como punto de partida, no como evidencia suficiente.
2. **No hay costo de spread histórico real** — se modela como banda de sensibilidad (0/3/6/10%),
   no como medición. El verdadero costo de ejecución podría estar en cualquier punto de esa banda,
   o fuera de ella.
3. **No se conoce el capital real de la cuenta.** El "PnL neto anualizado" no se pudo expresar como
   % de capital de cuenta — se reporta solo en ARS absolutos y como % del nocional de entrada
   promedio (proxy de "capital empleado"), nunca como retorno sobre una base de capital fabricada.
4. **Sharpe/Sortino "anualizados" son una extrapolación de la frecuencia de trades YA VISTA**, no
   una proyección de que esa frecuencia (ni ese resultado) se sostendrá fuera de la muestra.
5. **Max drawdown y recuperación no llevan intervalo de confianza** (bootstrap i.i.d. rompe el
   orden temporal) — su incertidumbre real es probablemente mayor que la de las demás métricas.
6. **Comisión asumida en la escala Gold (0,50%)**, la más conservadora de las 3 escalas de IOL. Si
   el volumen mensual real de la cuenta calificara para Platinum (0,30%) o Black (0,10%), los
   resultados netos mejorarían proporcionalmente (pero seguirían siendo negativos en la mayoría de
   los escenarios — el costo de comisión es solo una parte del costo regulatorio total, y no afecta
   el spread).
7. **Prueba de estrés (spot ±15% + shock de IV +20 puntos de vol) — pendiente, no incluida en esta
   entrega.** Requiere Griegas (delta/vega) por posición al momento de cada trade histórico, dato
   que no existe en los dos exports disponibles (son exports de trade/evento, no snapshots de
   mercado con Griegas). Esta prueba solo podrá hacerse retroactivamente sobre trades **nuevos**,
   una vez que el logger de embudo de señales (Fase 1, punto 5, prioridad alta) esté desplegado y
   acumulando filas reales con Griegas por ciclo — no se fabrica un valor de Griegas para trades ya
   cerrados. Queda como ítem explícito de Fase 1/2, no descartado.
8. **`scalping` conviction/otras mejoras del 2026-09-28 están apagadas en este baseline** — este es
   el comportamiento real tal como operó el bot, sin ninguna de las 8 mejoras activas (todas
   opt-in, apagadas por defecto). La ablación de cada mejora es la Fase 1/2, no esta.

---

## 6. Qué NO funcionó / qué no se pudo hacer con los datos actuales

- No se pudo reconstruir un costo de spread histórico real (no existe bid/ask punto a punto para
  fills pasados) — se dejó como banda de sensibilidad en vez de fabricar un número.
- No se pudo hacer la prueba de estrés de spot/IV pedida originalmente (falta de Griegas
  históricas por posición) — ver limitación 7 arriba.
- No se pudo expresar el retorno como % de capital real de cuenta (capital no provisto, no
  fabricado).
- Los primeros resultados de `weekly_asymmetric` parecían anómalamente malos (win rate neto
  colapsando a ~0,5%, costo de hasta 5000% del PnL bruto) — se investigó a fondo antes de aceptarlos:
  se verificó a mano el PnL bruto/neto de un trade individual, se reconcilió el total de costo vs.
  el total de PnL bruto de forma independiente, y se descubrió y corrigió un gap de conteo (9
  posiciones legacy no contadas). Los números finales son consistentes y no un artefacto de un bug
  de escala/unidades.

---

## 7. Próximos pasos (según el orden acordado)

Ninguna flag debe activarse en vivo con la evidencia actual. Antes de evaluar mejoras (Fase 2+),
sigue Fase 1 en el orden acordado:

1. Desplegar el logger de embudo de señales completo (persistiendo TODO el universo de candidatas
   por ciclo, no solo señales) en Northflank junto con `market_snapshots.csv`, lo antes posible —
   es lo que habilitará tanto la ablación contrafáctica de filtros como la prueba de estrés
   pendiente (limitación 7).
2. Verificar/corregir el orden del presupuesto preventivo de Griegas respecto del tamaño ya
   escalado por conviction sizing.
3. Condicionar el conviction sizing a liquidez/costo de ejecución.
4. Agregar el parámetro de buffer de prima de riesgo de salto (jump risk premium) a la comparación
   IV vs. HV bipower.
5. Agregar prueba de estrés basada en escenarios (no solo de primer orden) para Griegas.

Ver conversación para el detalle completo de cada ítem.
