# RESEARCH.md — Mandato de research cuantitativo GGAL_BOT

Iniciado 2026-10-13, a pedido explicito del usuario (ver hilo de auditoria).
Documento vivo: se actualiza con cada hallazgo, pre-registro y resultado.
Disciplina de todo el proyecto aplica aca tambien: nunca fabricar un
numero, dato o resultado; todo lo que sigue esta trazado a una fuente
(codigo, log real, fuente externa citada, o tool call de esta sesion).

## Fase 0 — estado final (cerrada 2026-10-13)

### 1. Horario de BYMA (opciones)

**VERIFIED.** Rueda continua de opciones: **10:30-17:00 ART = 13:30-20:00
UTC**, vigente desde el 28-jul-2025 (alineacion con el horario de Wall
Street). Confirmado contra 3 fuentes independientes: comentario de codigo
en `ggal_bot/market_hours.py` (cita Comunicado BYMA N19024), nota de
Bloomberg Linea, nota de iProfesional. No se encontro ninguna fuente que
respalde "11:00 ART" como apertura de opciones; el usuario confirmo el
error y lo corrigio. El cluster de perdidas en los primeros segundos de
rueda (hallazgo de la sesion anterior) queda en pie bajo el horario
correcto.

### 2. Filtro de "quote operable" para Stop Loss/Take Profit

**IMPLEMENTADO Y TESTEADO.** `RiskConfig.enforce_quote_validity_filter_for_exits`
(default `True`, commit `42d6f8a`). `OrderBookSnapshot.is_valid_quote()`
exige dos puntas + spread_relative <= `max_spread_relative` (5% default) +
antiguedad <= `max_option_quote_staleness_seconds` (90s default) - mismos
umbrales que ya usaba el resto del proyecto, ningun umbral nuevo. Cuando un
book no pasa el filtro, `current_price` llega `None` a
`evaluate_position_exit`: Stop Loss/Take Profit simplemente esperan al
primer quote valido; las salidas de calendario (horizonte semanal,
weekend_theta_guard) siguen evaluandose igual; ninguna salida ya disparada
queda bloqueada. Aplicado identicamente a weekly_asymmetric y scalping.
5 tests nuevos + 2 tests pre-existentes ajustados (su fixture de spread
10% ya no pasaba el nuevo limite de 5% - se angosto a 2.5% preservando la
perdida de -60% que esos tests necesitan). Suite completa: **779/779**
(corrida 3 veces). Bundle entregado al usuario para aplicar/pushear
(`quote-validity-filter-oct13.bundle`).

Esto reemplaza la hipotesis "ventana de apertura" de la lista anterior:
es un fix de ejecucion/medicion, no una fuente de edge a testear.

### 3. Disponibilidad de datos: ADR GGAL (Nasdaq)

**VERIFIED - disponible.** `get_price_history` (IOL) trae la serie diaria
completa: 2.693 barras OHLC+volumen, 2016-01-04 a 2026-10-13 (subset "10
anos" usado en el analisis de abajo: 2016-10-13 a 2026-10-12, 2.499
barras). Cross-validado: un segundo fetch independiente en esta sesion
coincide con el dataset ya en uso en 2.499/2.499 fechas superpuestas (0
discrepancias).

Limitacion NO resuelta: los bares solo traen `close` (sin campo de "close
ajustado"), asi que el ajuste por dividendos no esta confirmado - se
trata esto como dato RAW, no confirmado como total-return.

Ratio del ADR: **VERIFIED - 1 ADR = 10 acciones ordinarias Clase B**,
confirmado via 2 fuentes independientes (Hargreaves Lansdown, Boerse
Stuttgart: "GGAL ADR Each Rep 10 Ord B Shares SPON"). No se encontro
ningun cambio historico de ratio documentado en ninguna fuente consultada.

Barrido de saltos diarios >15% (ln-return) sobre toda la serie: ningun
salto limpio compatible con un split/reverse-split no ajustado - los
saltos grandes coinciden con eventos macro/politicos conocidos (PASO
2019-08-12: ln-return -83.1%/simple -56.4%; COVID marzo 2020; elecciones
octubre 2025: +32.6% el 2025-10-27, -26.8% el 2025-09-08, etc.).

Calibracion de riesgo de salto (uso: Fase 2, hipotesis de venta de vol):
- VaR historico 1 dia al 99% = **9.14%**.
- Peor retorno acumulado de 10 ruedas = **-61.61%** (ventana que termina
  2019-08-23, PASO 2019). Cualquier estrategia vendedora de vol que no
  sobreviva este escenario no se implementa (regla del usuario).
- Drawdown maximo (cualquier duracion) = **-91.88%** (pico 2018-01-18,
  valle 2022-07-21).

Regimen de volatilidad realizada (ventana movil de 20 ruedas, anualizada,
toda la serie de 10 anios):
- media=56.02%, mediana=49.57%, min=16.19%, max=313.16%.
- **Los ultimos 25 dias de operacion real del bot (2026-09-08 a
  2026-10-12) muestran vol realizada anualizada=32.69%, percentil 11.7
  dentro de la distribucion completa de 10 anios** - la muestra shadow del
  bot vivio en un regimen de vol atipicamente BAJO frente a la historia
  completa de GGAL. Cualquier conclusion de esta muestra (incluida
  cualquier venta de vol) debe leerse con esa salvedad.

No sirve para lead-lag intradia puro (es serie diaria) - ver item 5.

### 4. Disponibilidad de datos: CCL

**DATA INSUFFICIENT (historico).** El bot tiene un logger de cotizaciones
de bonos ya construido (`ggal_bot/data/ccl_bond_quote_log.py`,
GD30/GD30C/AL30/AL30C via data912.com) pero es opt-in y **nunca estuvo
activo en produccion**: `logs/ccl_bond_quotes.csv` tiene solo el header (53
bytes, creado 2026-09-30, cero filas de datos desde entonces). El endpoint
`data912.com/live/arg_bonds` (verificado ahora via WebFetch) devuelve
unicamente un snapshot actual sin timestamps ni serie historica - no hay
forma de reconstruir CCL historico desde ahi tampoco.

**Pregunta para el usuario**: si tenes una fuente propia de CCL historico
(igual que vas a pasarme el CSV del ADR), la hipotesis 4 se vuelve
testeable. Sin eso, queda en backlog como DATA INSUFFICIENT.

### 5. Disponibilidad de datos: caucion

**DATA INSUFFICIENT (historico).** El bot solo usa una tasa anual FIJA de
config (`RiskConfig.default_annual_rate`, default 0.40) para Black-Scholes
- nunca logueo una tasa de caucion real. La tool `get_caucion_rates` (IOL)
devuelve unicamente la tasa VIGENTE para los proximos 3 dias habiles (ej.
hoy: 1 dia=20.8%, 2 dias=20.5%, 3 dias=20.9% anualizado ARS) - no expone
historia. No se encontro ninguna fuente historica de caucion accesible
desde esta sesion.

**Misma pregunta que CCL**: si el usuario tiene una serie historica de
caucion, la hipotesis 3 se vuelve testeable: si no, queda en backlog.

### 6. Reconciliacion de posiciones abiertas ("11 vs 23")

**Hallazgo nuevo, no trivial.** Sobre el dataset completo (1.444 fills /
2.020 eventos, 2026-09-01 a 2026-10-09T19:59:47 UTC), `match_trades_fifo` +
`aggregate_open_positions` (pnl_engine.py, sin modificar) dan **23 filas**
de "lote abierto" (simbolo x estrategia x position_id). Pero al sumar la
quantity de TODOS los lotes de un mismo simbolo (ignorando a que
estrategia/position_id quedo asignado cada lote), **9 de los 12 simbolos
con algun lote abierto netean exactamente a CERO**:

| simbolo | lotes que netean a 0 |
|---|---|
| GFGC6400OC, GFGC6800OC, GFGC7000OC, GFGC7200OC, GFGC7400OC, GFGC7600OC, GFGC7800OC, GFGV5400OC, GFGV5600OC, GFGC8000OC | weekly_asymmetric corto vs. unknown_legacy largo (o weekly_asymmetric largo vs. corto), magnitud idéntica |

Patron: en 6 de esos simbolos, el lote largo "unknown_legacy" (entrada sin
`position_id` resoluble porque el fill es anterior a la cobertura del
journal) tiene la MISMA cantidad, en signo opuesto, que un lote corto
clasificado "weekly_asymmetric". Esto es demasiado exacto para ser
coincidencia en 6 simbolos distintos: la lectura mas probable es que son
LA MISMA posicion economica, pero el fill de cierre quedo en un
`position_id`/bucket de estrategia distinto al de apertura (porque
`resolve_position_ids_from_journal` no pudo resolver el extremo de
apertura, anterior al journal), y el matching FIFO -que deliberadamente
usa `(symbol, position_id)` para evitar contaminar PnL entre estrategias-
los trata como dos posiciones independientes en vez de una cerrada.

**Posiciones netas reales (las unicas que no netean a cero) al cierre del
dataset:**
- GFGC6600OC: **-8** (corto, weekly_asymmetric)
- GGAL (subyacente, delta_hedge): **-335.85** acciones

Esto tampoco coincide con "11". No tengo forma de saber desde aca de donde
sale ese numero (¿view en vivo del dashboard? ¿estado del portfolio en
memoria antes del ultimo reset? ¿Northflank logs?) - si el usuario puede
decir la fuente exacta del "11", lo reconcilio contra eso especificamente.
Mientras tanto, la vista neta por simbolo de arriba (2 posiciones reales:
GFGC6600OC y el subyacente) es la unica reconciliacion que puedo sostener
con los datos que tengo, trazada paso a paso a `dashboard/pnl_engine.py`
sin ninguna reconstruccion paralela.

## Correcciones aceptadas del usuario (incorporadas)

- **Hipotesis 2 redefinida**: weekly_asymmetric compra calls (larga en
  vol) - no es vehiculo para testear prima de riesgo de volatilidad. La
  hipotesis 2 pasa a ser una estrategia NUEVA (vender vol con riesgo
  definido: bull put / bear call spreads semanales), comparando IV vs. vol
  realizada posterior. weekly_asymmetric se evalua aparte, sin privilegio,
  en el item 5.
- **Hipotesis principal agregada**: provision de liquidez (cotizar dentro
  del spread en strikes de spread ancho, limites de inventario/griegas,
  modelo de fill conservador - fill solo si un precio posterior atraviesa
  el limite). Incluye la ventana de apertura como caso particular.
- Filtro de quote operable (item 2 arriba) reemplaza la vieja hipotesis
  "ventana de apertura" como fuente de edge.

## Orden de testeo aprobado (Fase 2), con veredicto de datos

| # | Hipotesis | Datos | Estado |
|---|---|---|---|
| 1 | Provision de liquidez | book local (ya logueado) | **Testeable - siguiente paso** |
| 2 | Venta de vol riesgo definido (VRP) | opciones locales + ADR (vol historica) | **Testeable** |
| 3 | Paridad put-call vs. caucion | caucion historica | **DATA INSUFFICIENT** - backlog |
| 4 | Lead-lag ADR x CCL -> opciones locales | ADR (ok) + CCL historico | **DATA INSUFFICIENT** - backlog (falta CCL) |
| 5 | scalping / weekly_asymmetric (sin privilegio) | ya logueado | **Testeable**, con filtro de quotes aplicado |

Backlog explicito (3 y 4): quedan pre-registradas en texto para no
perderlas, pero no se testean hasta tener caucion/CCL historico (fuente
propia del usuario, o logging forward a partir de hoy).

## Pre-registro — Hipotesis 1: Provision de liquidez

**Pre-registrado 2026-10-13, antes de ver ningun resultado.**

- **Mecanismo economico**: en strikes donde el spread observado supera
  `max_spread_relative` (5%), cotizar DENTRO del spread (comprar al bid+k,
  vender al ask-k) captura parte de ese spread como edge, siempre que el
  inventario resultante se gestione (no es direccional por diseño).
- **Regla exacta**: para cada book con spread_relative > 5% y tamanio de
  punta >= `min_book_size`, insertar una cotizacion propia a
  mid +/- (spread_observado * 0.5 * factor_captura), factor_captura a
  calibrar en {0.25, 0.5, 0.75} (3 variantes).
- **Limites de inventario/griegas**: posicion neta por simbolo <= N
  contratos (a definir con el presupuesto de griegas ya existente en
  `RiskConfig`), vega/gamma agregados dentro de los limites YA vigentes
  (nunca se tocan - instruccion permanente del usuario).
- **Modelo de fill conservador**: una cotizacion propia solo se considera
  "llena" si un trade posterior en el book REAL atraviesa el precio
  limite (nunca con solo tocarlo) - exactamente la regla pedida por el
  usuario.
- **Metrica de exito**: PnL neto de costos (comisiones IOL, derecho de
  mercado, IVA, spread realmente cruzado) por dia, con bootstrap CI 95%,
  N de fills simulados, capacidad (contratos/dia antes de mover el
  spread), drawdown maximo, stress +/-15% en el gap de apertura, y
  sensibilidad +/-20% en factor_captura y limites de inventario.
- **Holdout**: el ultimo 25% del tiempo del dataset (fills desde
  ~2026-09-25 en adelante, a confirmar el corte exacto por numero de dias
  habiles, no de filas) queda intocado hasta el final.
- **Variantes totales de esta hipotesis**: 3 (factor_captura) x a definir
  (limite de inventario) - a reportar el numero final exacto antes de ver
  resultados.

(Hipotesis 2 y 5 se pre-registran de la misma forma antes de correrlas -
secciones a agregar aca mismo cuando empiecen.)
