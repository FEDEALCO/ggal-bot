# REPORT.md — GGAL_BOT: análisis de rentabilidad neta de costos

_Última actualización: 2026-09-29 (Fase 0 — baseline + correcciones + diagnóstico de PnL bruto)._

Este documento se va completando estrategia de fase en fase, según la metodología acordada
(Fase 0 → baseline; Fase 1 → mitigar riesgos identificados; Fase 2 → validación walk-forward /
out-of-sample; Fase 3 → control de overfitting; Fase 4 → nuevas mejoras). Cada sección nueva se
agrega, nunca se reescribe una anterior salvo para corregir un error.

**Correcciones aplicadas el 2026-09-29 tras la primera entrega (ver conversación):**

1. Se corrigió un error de unidades: la versión anterior reportaba Sharpe/Sortino **anualizados**
   como punto central pero el intervalo de confianza (bootstrap) quedaba **sin anualizar** — el
   punto caía fuera de su propio IC. Se eliminó la anualización por completo (no tiene sustento
   estadístico con 15-22 días de datos) — ahora Sharpe/Sortino se reportan **por trade**, en las
   mismas unidades que su IC (§2.3 y §3, abajo).
2. Se agregaron escenarios de comisión Platinum (0,30%) y Black (0,10%) además de Gold (§3.1).
   **Pendiente: el usuario todavía no confirmó cuál es la escala real de su cuenta** (depende del
   volumen mensual operado) — se muestran las 3 como banda de sensibilidad, no como elección.
3. Se agregó un diagnóstico completo del **PnL bruto** por estrategia (§4) — motivo de salida,
   moneyness, días al vencimiento, tiempo de tenencia y hora del día — porque dos de las tres
   estrategias pierden ANTES de costos, y ningún filtro de costos/ejecución puede arreglar eso.

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
expectancy, PnL bruto/neto, costo como % del PnL bruto, Sharpe y Sortino (por trade, NUNCA
anualizados — ver corrección abajo), max drawdown + tiempo de recuperación, peor día y peor semana.

- **Win rate, expectancy, Sharpe y Sortino** llevan intervalo de confianza del 95% por
  **bootstrap i.i.d. de trades** (remuestreo con reemplazo, 10.000 resamples, semilla fija 1234
  para reproducibilidad).
- **Max drawdown y tiempo de recuperación NO se bootstrapean** — el bootstrap i.i.d. destruye el
  orden temporal, que es precisamente lo que define un drawdown. Se reportan como punto único,
  con la advertencia de que su incertidumbre real es al menos tan grande como la de las demás
  métricas.
- **Sharpe/Sortino se reportan UNICAMENTE POR TRADE (corrección aplicada el 2026-09-29).** La
  entrega anterior de este reporte anualizaba el punto central escalando por
  `sqrt(trades_por_año_observado)` pero dejaba el intervalo de confianza bootstrap sin anualizar
  — un error de unidades real: el punto (ej. weekly_asymmetric: -35,7) caía fuera de su propio IC
  (-2,06 a -0,40). Se corrigió eliminando la anualización por completo: no tiene sustento
  estadístico extrapolar una frecuencia de trading observada durante 15-22 días a una base anual.
  Ahora Sharpe y Sortino se calculan sobre la serie de retornos por trade
  (`pnl_net_ars / notional_de_entrada_de_ESE_trade`) y se reportan tal cual, en las mismas unidades
  que su propio IC. `trades_per_year_observed` se sigue reportando en el CSV como dato informativo
  (frecuencia de trading observada), pero ya no se usa para anualizar nada.
- **Max drawdown** se reporta en ARS (curva de equity acumulada); en % solo cuando el pico previo
  a la caída fue positivo (si nunca fue positivo, no hay una base sensata para expresar %).

---

## 3. Resultados — tabla completa (18 filas = 3 estrategias × [4 escenarios de spread + 2 de comisión])

_CSV completo con todas las columnas (incluyendo intervalos de confianza): `fase0_results.csv`,
adjunto en esta conversación. Sharpe/Sortino en esta tabla son **por trade** (ver corrección §2.3),
no anualizados._

| Estrategia | Escenario costo | N | Abierto | Incompl. | Win% neto | PnL neto (ARS) | PnL bruto (ARS) | Costo % \|bruto\| | Sharpe/trade | Sortino/trade | MaxDD (ARS) | Recup. |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| weekly_asymmetric | mid_sin_spread (Gold) | 199 | 10 | 9 | 4,52% | -720.770 | -86.637 | 731,9% | -0,546 | -0,494 | -741.256 | no recuperó |
| weekly_asymmetric | spread_3pct | 199 | 10 | 9 | 0,50% | -1.843.792 | -86.637 | 2028,2% | -1,431 | -0,826 | -1.859.187 | no recuperó |
| weekly_asymmetric | spread_6pct | 199 | 10 | 9 | 0,50% | -2.966.813 | -86.637 | 3324,4% | -2,344 | -0,922 | -2.977.118 | no recuperó |
| weekly_asymmetric | spread_10pct | 199 | 10 | 9 | 0,50% | -4.464.175 | -86.637 | 5052,7% | -3,606 | -0,964 | -4.467.693 | no recuperó |
| scalping | mid_sin_spread (Gold) | 214 | 0 | 0 | 21,50% | -53.796 | +188.246 | 128,6% | -0,087 | -0,181 | -94.095 | no recuperó |
| scalping | spread_3pct | 214 | 0 | 0 | 9,35% | -482.441 | +188.246 | 356,3% | -0,674 | -0,709 | -460.630 | no recuperó |
| scalping | spread_6pct | 214 | 0 | 0 | 7,48% | -911.087 | +188.246 | 584,0% | -1,280 | -0,852 | -874.919 | no recuperó |
| scalping | spread_10pct | 214 | 0 | 0 | 3,74% | -1.482.614 | +188.246 | 887,6% | -2,119 | -0,924 | -1.427.305 | no recuperó |
| vol_arbitrage | mid_sin_spread (Gold) | 577 | 0 | 0 | 15,94% | -1.879.222 | -698.093 | 169,2% | -0,229 | -0,246 | -1.491.475 | no recuperó |
| vol_arbitrage | spread_3pct | 577 | 0 | 0 | 7,80% | -3.970.948 | -698.093 | 468,8% | -0,575 | -0,523 | -3.097.235 | no recuperó |
| vol_arbitrage | spread_6pct | 577 | 0 | 0 | 5,55% | -6.062.674 | -698.093 | 768,5% | -0,931 | -0,697 | -4.702.996 | no recuperó |
| vol_arbitrage | spread_10pct | 577 | 0 | 0 | 3,47% | -8.851.642 | -698.093 | 1168,0% | -1,424 | -0,824 | -6.844.010 | no recuperó |

_"Abierto" = posiciones sin evento CLOSE en la ventana (nunca se les fabricó un cierre). "Incompl."
= posiciones con CLOSE pero sin ENTRY dentro de la ventana (legacy, ver §2.1) — excluidas, nunca
fabricadas._

### 3.1 Sensibilidad a la escala de comisión (Gold / Platinum / Black), a spread=0

**Pendiente de confirmar: cuál es la escala real de la cuenta** (depende del volumen mensual
operado — el usuario todavía no la proveyó). Mientras tanto, banda completa a spread=0 (para aislar
el efecto de la comisión del efecto del spread, que se sensibiliza aparte en la tabla de arriba):

| Estrategia | Escala | Win% neto | PnL neto (ARS) | Costo % \|bruto\| | Sharpe/trade |
|---|---|---:|---:|---:|---:|
| weekly_asymmetric | Gold (0,50%) | 4,52% | -720.770 | 731,9% | -0,546 |
| weekly_asymmetric | Platinum (0,30%) | 8,04% | -539.589 | 522,8% | -0,405 |
| weekly_asymmetric | Black (0,10%) | 12,06% | -358.409 | 313,7% | -0,266 |
| scalping | Gold (0,50%) | 21,50% | -53.796 | 128,6% | -0,087 |
| scalping | Platinum (0,30%) | 25,23% | **+15.359** | 91,8% | +0,007 |
| scalping | Black (0,10%) | 29,91% | **+84.514** | 55,1% | +0,099 |
| vol_arbitrage | Gold (0,50%) | 15,94% | -1.879.222 | 169,2% | -0,229 |
| vol_arbitrage | Platinum (0,30%) | 19,41% | -1.541.756 | 120,9% | -0,175 |
| vol_arbitrage | Black (0,10%) | 23,05% | -1.204.291 | 72,5% | -0,120 |

Hallazgo relevante: `scalping` **pasa a ser neto positivo** en Platinum y Black (sin contar
spread) — es la única de las tres donde la escala de comisión decide el signo del resultado. En
`weekly_asymmetric` y `vol_arbitrage` el resultado sigue siendo negativo en las 3 escalas: el
problema ahí no es principalmente la comisión (ver §4, el PnL bruto ya es negativo en ambas).

### Intervalos de confianza (bootstrap 95%, escenario `mid_sin_spread`, Gold)

| Estrategia | Win% neto (IC 95%) | Expectancy ARS/trade (IC 95%) | Sharpe/trade (IC 95%) |
|---|---|---|---|
| weekly_asymmetric | 4,52% (2,01% – 7,54%) | -3.622 (-4.723 – -2.875) | -0,546 (-2,06 – -0,40) |
| scalping | 21,50% (15,89% – 27,10%) | -251 (-731 – +301) | -0,087 (-0,29 – +0,05) |
| vol_arbitrage | 15,94% (13,00% – 19,06%) | -3.257 (-4.424 – -2.239) | -0,229 (-0,29 – -0,17) |

El punto central ahora cae dentro de su propio intervalo en las tres filas (corrección aplicada,
ver §2.3). Notar que en `scalping` el intervalo de expectancy y de Sharpe **cruza el cero** en el
escenario más optimista — es la única de las tres donde, dado el ruido de la muestra, no se puede
descartar con confianza que el resultado neto sea aproximadamente breakeven en ese escenario
particular. En los otros dos escenarios y en las otras dos estrategias, los intervalos son
negativos de punta a punta.

---

## 4. Diagnóstico de PnL bruto (por qué pierden ANTES de costos)

Dos de las tres estrategias (`weekly_asymmetric`, `vol_arbitrage`) tienen PnL bruto negativo — ver
el HALLAZGO PRINCIPAL arriba. Ningún filtro de costos/ejecución puede arreglar eso: hay que entender de dónde viene el PnL
bruto antes de proponer ninguna mejora nueva. Este corte usa `ggal_bot/backtest/attribution.py`
(30 tests nuevos) y el precio real diario de GGAL (`ggal_bot/backtest/data/ggal_underlying_daily_2026-08-25_2026-09-29.csv`,
consultado el 2026-09-29 vía la API del broker). CSV completo con las 5 cortes × 3 estrategias:
`fase0_attribution.csv`, adjunto en esta conversación.

### 4.0 Hallazgo más importante: entradas de viernes auto-liquidadas por `weekend_theta_guard`

**194 de los 199 trades de `weekly_asymmetric` (97,5%) se abren un VIERNES y se cierran en menos de
60 segundos** (mediana: 23 segundos), forzados por el propio `weekend_theta_guard` — un guard
**real e intencional** (`ggal_bot/risk/risk_manager.py::evaluate_position_exit`, línea 246-255;
documentado en `config.py` desde un incidente real de -$133.568 de decay de fin de semana no
capturado). El guard dispara si `now.weekday() == 4` (viernes) y el vencimiento es posterior a
ese día, **sin ningún piso de tiempo mínimo de tenencia** (`weekend_theta_guard_max_holding_business_days=None`
por defecto) — es decir, se aplica igual a una posición abierta hace 3 días que a una abierta hace
8 segundos.

**El problema verificado por lectura de código:** la lógica de ENTRADA de `weekly_asymmetric.py`
(`scan_entry_signals`) **no chequea el día de la semana en ningún lado** — no existe ningún gate
que evite abrir una posición nueva basada en una tesis de "horizonte semanal" (15-25 días hábiles,
según el propio motivo de entrada registrado) un viernes, sabiendo que el propio guard la va a
revertir casi con certeza en el siguiente ciclo de evaluación. El resultado observado en esta
ventana:

| | N | PnL bruto sum (ARS) | Costo regulatorio total (ARS, mid_sin_spread) | PnL NETO (ARS) |
|---|---:|---:|---:|---:|
| Entradas de viernes cerradas en <60s por weekend_theta_guard | 194 | +9.904 | 618.981 | **-609.077** |
| Resto de `weekly_asymmetric` (stop_loss, otro, timeout >60s) | 5 | -96.541 | ~14.615 | ~-111.693 |
| **Total `weekly_asymmetric`** | 199 | -86.637 | ~633.596 | -720.770 |

**Estas 194 entradas relámpago explican ~85% del PnL neto negativo total de `weekly_asymmetric` en
esta ventana** (-609.077 de -720.770 ARS) — no por tener un edge bruto negativo (de hecho suman
+9.904 ARS, prácticamente ruido/breakeven), sino porque cada una paga el costo regulatorio COMPLETO
de un round-trip (~1,7% del nocional, ~188.000 ARS promedio por trade) por una posición que nunca
tuvo tiempo de desarrollar su tesis. Esto es un **problema estructural y recurrente** (se va a
repetir todos los viernes mientras el bot corra, no es un evento aislado de esta muestra), y es el
hallazgo más accionable de todo este diagnóstico: **candidato a prioridad #0 de Fase 1** (por
delante de los 5 puntos ya acordados) — un gate opt-in en la entrada que evite abrir posiciones
nuevas de horizonte semanal un viernes (o que exija que el horizonte restante hasta el vencimiento
sobreviva el propio guard), sin tocar el guard en sí (que existe por una razón real y documentada).
Ver §8 para la propuesta concreta (sin implementar todavía, a la espera de confirmación del
usuario).

### 4.1 Motivo de salida (bucket: stop / take_profit / timeout / otro)

`vol_arbitrage`: **DATA INSUFFICIENT** — el export de cierres de esta estrategia no trae un campo
de motivo de salida (columnas: Ticker/Estrategia/Dirección/Cantidad/Entrada/Salida/Precio
Entrada/Precio Salida/PnL($)/PnL(%)/Duración(s)). No se fabricó ninguna clasificación.

| Estrategia | Bucket | N | PnL bruto sum (ARS) | PnL bruto medio (ARS) | Win% bruto |
|---|---|---:|---:|---:|---:|
| weekly_asymmetric | timeout (weekend_theta_guard) | 197 | -25.069 | -127 | 37,6% |
| weekly_asymmetric | stop (stop_loss) | 1 | -84.929 | -84.929 | 0% |
| weekly_asymmetric | otro (vega_theta_decay) | 1 | +23.361 | +23.361 | 100% |
| scalping | timeout (scalping_eod_close) | 82 | +110.105 | +1.343 | 46,3% |
| scalping | take_profit (scalping_iv_mean_reversion + take_profit) | 131 | +78.141 | +597 | 48,9% |
| scalping | otro (scalping_no_progress) | 1 | 0 | 0 | 0% |

`scalping` tiene un edge bruto positivo y razonablemente estable en sus dos categorías principales
de salida — su problema (§3) es puramente de costos, no de motivo de salida.

### 4.2 Moneyness a la entrada (spot real vs. strike; positivo = ITM)

Contexto de mercado real durante las 3 ventanas (fuente: precio diario real de GGAL, no
fabricado): GGAL cayó **-11,3%** durante la ventana de `weekly_asymmetric` (7090→6290),
**-10,3%** durante la de `scalping` y **-10,5%** durante la de `vol_arbitrage` — una baja
sostenida y marcada en las tres, no un ruido de corto plazo.

| Estrategia | Bucket | N | PnL bruto sum (ARS) | PnL bruto medio (ARS) | Win% bruto |
|---|---|---:|---:|---:|---:|
| weekly_asymmetric | OTM >5% | 156 | **-121.976** | -782 | 36,5% |
| weekly_asymmetric | ITM >5% | 16 | +29.396 | +1.837 | 43,8% |
| weekly_asymmetric | OTM 2-5% | 16 | +2.463 | +154 | 25,0% |
| weekly_asymmetric | ATM (±2%) | 10 | +3.464 | +346 | 60,0% |
| vol_arbitrage | OTM >5% | 302 | **-768.214** | -2.544 | 39,4% |
| vol_arbitrage | ITM >5% | 34 | +51.634 | +1.519 | 52,9% |
| vol_arbitrage | OTM 2-5% | 87 | +49.843 | +573 | 46,0% |
| vol_arbitrage | ATM (±2%) | 105 | -35.601 | -339 | 42,9% |
| scalping | OTM >5% | 87 | +107.068 | +1.231 | 40,2% |
| scalping | OTM 2-5% | 55 | +57.608 | +1.047 | 49,1% |

**Todas las opciones que este análisis puede clasificar en `weekly_asymmetric` y `vol_arbitrage`
son calls o puts sobre GGAL** (ver `parse_option_symbol`), y en ambas estrategias el bucket "OTM
>5%" (la mayoría de los trades, 156/199 y 302/577 respectivamente) concentra la práctica totalidad
de la pérdida bruta (-121.976 y -768.214 ARS). Dado que GGAL cayó ~10-11% en las tres ventanas, esto
es consistente con una hipótesis concreta y verificable: **una parte sustancial de las posiciones
de estas dos estrategias son estructuras con exposición direccional neta positiva a GGAL (largas de
calls o equivalente) que perdieron por el movimiento direccional bajista del subyacente, no
necesariamente porque la dislocación de IV detectada al entrar fuera "ruido"** — el patrón por sí
solo no permite distinguir "la IV estaba realmente mal calculada (mispricing real)" de "la IV
dislocation era real pero el riesgo direccional no cubierto dominó el resultado". Separar ambas
hipótesis requeriría conocer el delta neto de cada posición al momento de la entrada (dato que el
logger de embudo de señales, Fase 1 punto 5, va a empezar a capturar) — con los datos actuales,
esto queda como **HYPOTHESIS**, no como hallazgo verificado.

**Nota metodológica:** el spot usado es el cierre diario real del día de la entrada (o el hábil
anterior más cercano) — no el precio intradía exacto al momento de la entrada. Es una aproximación
de fin de día para un análisis diagnóstico retrospectivo (no una señal usada por el bot), pero
puede diferir del spot exacto si la entrada ocurrió temprano en la rueda y el precio se movió
intradía.

### 4.3 Días al vencimiento (DTE) a la entrada

`vol_arbitrage`: **DATA INSUFFICIENT** — el export de cierres no trae Contract Key ni fecha de
vencimiento; no se fabricó ninguna fecha.

`weekly_asymmetric`: **no aporta granularidad en esta muestra** — las 199 entradas caen todas en
el bucket "15+d" (consistente con el horizonte de 15-25 días hábiles mencionado en el propio motivo
de entrada). Los límites de bucket actuales (0-3d/4-7d/8-14d/15+d) están pensados para
`scalping`/`vol_arbitrage` (horizontes más cortos) y deberían refinarse (ej. 15-20d/21-25d/26+d)
para que este corte sea útil en `weekly_asymmetric` — queda como mejora pendiente, no urgente.

`scalping`: 156 trades a 15+ días, 58 a 4-7 días — sin una diferencia de edge bruto grande entre
ambos (+246 ARS/trade medio vs. +2.584 ARS/trade medio respectivamente; la submuestra de 58 es
pequeña, no se puede concluir con confianza que el horizonte corto sea sistemáticamente mejor).

### 4.4 Tiempo de tenencia

Ya cubierto en gran parte por el hallazgo de §4.0 (weekly_asymmetric: 196/199 trades duran menos
de 1 hora, casi todos por el patrón de entrada-de-viernes). Para `vol_arbitrage`: 506/577 trades
(87,7%) duran menos de 1 hora (+233.177 ARS sum, la única categoría de tenencia positiva), mientras
que las posiciones sostenidas más de 3 días (23 trades) concentran la mayor pérdida individual
(-640.490 ARS, -27.847 ARS/trade medio) — consistente con la hipótesis de exposición direccional no
cubierta de §4.2: cuanto más tiempo sostenida la posición, más tiempo expuesta a la baja sostenida
de GGAL.

### 4.5 Hora de entrada (ART, UTC-3)

Sin un patrón claro y consistente entre estrategias (ver `fase0_attribution.csv` para el detalle
completo por hora) — las franjas de mayor N de trades (12h-16h ART, horario central de la rueda de
BYMA) no muestran un edge bruto sistemáticamente mejor o peor que el resto. No se encontró
evidencia de un efecto de hora del día en esta muestra.

### 4.6 Hipótesis de "spread ancho = ruido de mid, no mispricing real" — DATA INSUFFICIENT

El usuario pidió contar cuántas señales de `vol_arbitrage`/`weekly_asymmetric` se originaron en
opciones con spread ancho, para testear si el bot está operando dislocaciones de IV que en
realidad son ruido del precio de referencia (mid) y no un mispricing real. **Esto no se puede medir
con los datos actuales**: ninguno de los dos exports disponibles registra el spread bid/ask vigente
al momento de cada señal (son exports de trade/evento, no snapshots de mercado). Es exactamente el
dato que el logger de embudo de señales (Fase 1, punto 5, ya priorizado) va a empezar a capturar
por ciclo — una vez desplegado y con datos reales acumulados, este corte se puede hacer de forma
retroactiva sobre trades nuevos. Queda como pendiente explícito, no descartado ni aproximado.

---

## 5. Interpretación por estrategia

**`weekly_asymmetric`**: PnL bruto ligeramente negativo (-86.637 ARS / -435 ARS promedio/trade), pero
esto **no** es un edge bruto uniformemente marginal — es la suma de un problema estructural
identificado y cuantificado en §4.0 (194/199 trades son entradas de viernes auto-liquidadas por
`weekend_theta_guard`, -609.077 ARS netos de costo por ~cero edge bruto) más un solo trade de stop
loss grande (-84.929 ARS) y exposición direccional no cubierta en el resto (§4.2). El costo
regulatorio (~1,7% round-trip) agrava todo lo anterior pero no es la causa raíz.

**`scalping`**: única con PnL bruto positivo (+188.246 ARS / +880 ARS promedio/trade) y sin ningún
patrón problemático en §4 (motivo de salida, moneyness y DTE muestran edge bruto positivo en casi
todos los cortes) — su problema (§3) es puramente de costos: nocional promedio menor (~66.000 ARS)
implica que el costo regulatorio fijo (~1,7% ≈ 1.100 ARS) supera el PnL bruto de muchos trades
individuales, de ahí el colapso del win rate neto (47,7% bruto → 21,5% neto en el escenario más
optimista). Es también la única de las tres que se vuelve neta positiva bajo comisión Platinum/Black
(§3.1).

**`vol_arbitrage`**: PnL bruto claramente negativo (-698.093 ARS / -1.210 ARS promedio/trade sobre
577 trades), la muestra más grande de las tres, con el resultado negativo más sostenido
estadísticamente (IC de Sharpe: -0,29 a -0,17, sin cruzar cero). El corte de moneyness (§4.2) ubica
la pérdida casi enteramente en el bucket "OTM >5%" (302/577 trades, -768.214 ARS) — la misma
hipótesis de exposición direccional no cubierta que en `weekly_asymmetric`, agravada por la baja
sostenida de GGAL (~-10,5%) durante la ventana. Las posiciones sostenidas más de 3 días concentran
la mayor pérdida por trade (§4.4).

---

## 6. Limitaciones metodológicas explícitas (leer antes de sacar conclusiones)

1. **Ventana de 15-22 días, un solo régimen de mercado.** Esto NO alcanza para activar ninguna
   flag en vivo ni para descartar definitivamente ninguna estrategia. Se reporta con intervalos de
   confianza amplios como punto de partida, no como evidencia suficiente.
2. **No hay costo de spread histórico real** — se modela como banda de sensibilidad (0/3/6/10%),
   no como medición. El verdadero costo de ejecución podría estar en cualquier punto de esa banda,
   o fuera de ella.
3. **No se conoce el capital real de la cuenta.** El "PnL neto anualizado" no se pudo expresar como
   % de capital de cuenta — se reporta solo en ARS absolutos y como % del nocional de entrada
   promedio (proxy de "capital empleado"), nunca como retorno sobre una base de capital fabricada.
4. **Sharpe/Sortino se reportan por trade, nunca anualizados** (corrección aplicada el 2026-09-29,
   ver §2.3) — con 15-22 días de datos, escalar por la frecuencia de trades ya vista en la muestra
   no tiene sustento estadístico. `trades_per_year_observed` queda solo como dato informativo.
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

## 7. Qué NO funcionó / qué no se pudo hacer con los datos actuales

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
- La primera entrega de este reporte tenía un error de unidades real en Sharpe/Sortino (punto
  anualizado, IC sin anualizar) — corregido, ver §2.3 y §6.4.
- No se pudo contar cuántas señales se originaron en opciones con spread ancho (hipótesis de "IV
  dislocation = ruido de mid, no mispricing real") — dato no disponible en los exports actuales,
  ver §4.6.

**Lo que SÍ se encontró y es directamente accionable:** el patrón de entradas de viernes
auto-liquidadas por `weekend_theta_guard` en `weekly_asymmetric` (§4.0) — a diferencia de los
demás hallazgos de este reporte (exposición direccional, régimen de mercado corto), este es un
problema de coordinación entre la lógica de entrada y un guard de salida, verificado por lectura de
código, con una causa raíz clara y una propuesta de fix concreta (§9, punto 0).

---

## 8. Próximos pasos

Ninguna flag debe activarse en vivo con la evidencia actual. Orden actualizado tras el diagnóstico
de PnL bruto (§4) y la confirmación del usuario del 2026-09-29:

0. **(Nuevo, prioridad más alta que los 5 puntos ya acordados)** Corregir la interacción
   entrada-viernes / `weekend_theta_guard` de `weekly_asymmetric` (§4.0) — responsable de ~85% del
   PnL neto negativo de esa estrategia en esta muestra. Propuesta concreta en §9, punto 0 (todavía
   sin implementar, a la espera de confirmación del usuario).
1. Desplegar el logger de embudo de señales completo (persistiendo TODO el universo de candidatas
   por ciclo — strike, vencimiento, IV, dislocación, spread, profundidad, Griegas, y qué filtros
   pasó o no con qué valor, no solo señales) en Northflank junto con `market_snapshots.csv`, lo
   antes posible. Habilita: la ablación contrafáctica de filtros, la prueba de estrés pendiente
   (limitación §6.7), el corte de "spread ancho" pendiente (§4.6), y separar la hipótesis de
   mispricing real vs. riesgo direccional no cubierto (§4.2).
2. Verificar/corregir el orden del presupuesto preventivo de Griegas respecto del tamaño ya
   escalado por conviction sizing.
3. Condicionar el conviction sizing a liquidez/costo de ejecución.
4. Agregar el parámetro de buffer de prima de riesgo de salto (jump risk premium) a la comparación
   IV vs. HV bipower.
5. Agregar prueba de estrés basada en escenarios (no solo de primer orden) para Griegas.

Ver conversación para el detalle completo de cada ítem.

---

## 9. Pendiente de esta ronda (pedido explícitamente por el usuario el 2026-09-29, no implementado
   todavía — instrucción fue "arrancá por 1, 2 y 3" de esa lista, quedando estos para la próxima)

0. **Propuesta (sin implementar, a confirmar con el usuario): gate opt-in de "no entrar en
   horizonte semanal un viernes"** — fix directo para el hallazgo de §4.0. Idea de diseño: en
   `weekly_asymmetric.py::scan_entry_signals`, si `weekend_theta_guard_enabled` está activo, no
   generar una señal de entrada nueva cuando `now.weekday() == 4` (viernes) y el vencimiento
   candidato es posterior a ese viernes — exactamente la misma condición que ya usa el guard de
   salida (`risk_manager.py::evaluate_position_exit`, línea 246-255), para que ambas lógicas queden
   coordinadas en vez de contradictorias. Alternativa más permisiva: permitir la entrada pero exigir
   que el edge esperado sea grande respecto al costo regulatorio completo de un round-trip
   (emparenta con el filtro k×costo del punto 2 de abajo). Cualquiera de las dos requiere
   confirmación del usuario antes de tocar código de producción — el guard de salida existe por una
   razón real y documentada (incidente de -$133.568), y no se toca.
1. **Edge bruto mínimo para breakeven, por estrategia y escenario de costo, vs. edge bruto
   promedio observado** — la brecha entre ambos como número central del reporte. Es calculable
   directamente de los datos ya generados (`total_cost_ars / n_trades` vs. `gross_pnl_ars /
   n_trades` de cada fila de `fase0_results.csv`) — queda para la próxima entrega junto con los
   puntos 2 y 3 de abajo.
2. **Filtro de entrada opt-in propuesto (sin implementar):** edge esperado en ARS > k × costo
   round-trip estimado (spread real del book + comisión + derecho + IVA), con k configurable,
   default 2. Diseño a confirmar con el usuario antes de escribir código — en particular cómo
   estimar "edge esperado en ARS" de forma consistente con la dislocación de IV ya calculada por
   cada estrategia, y qué costo de spread usar mientras no haya datos reales del book (¿la banda de
   sensibilidad de §2.2, o un valor fijo conservador?).
3. Se mantiene como prioridad desplegar `market_snapshots.csv` + el logger de embudo de señales
   (ítem 1 de §8): el spread real que empiece a registrar reemplaza la banda de sensibilidad actual
   por una medición real, y es un insumo directo para el filtro del punto 2 de esta sección.
