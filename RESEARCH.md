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

### 2bis. Medicion de cobertura del filtro + respaldo de ultimo resorte (2026-10-14)

**Medicion pedida: DATA INSUFFICIENT, intento documentado.** Se corrio el
filtro real (`is_valid_quote`, dos puntas + spread<=5% + antiguedad<=90s)
contra el UNICO export de `logs/market_snapshots.csv` disponible en este
seguimiento (87.018 filas, 52 simbolos, vencimiento 2026-10-16 unicamente).
Resultado de esa medicion: solo 3.2% de las filas tienen dos puntas, 0%
pasa el filtro completo - pero esa cifra es **inutilizable** para calibrar
nada: el export cubre EXCLUSIVAMENTE 2026-10-09T00:00:04 a 01:56:04 UTC,
una ventana de ~2hs enteramente FUERA de la rueda de BYMA (10:30-17:00 ART
= 13:30-20:00 UTC). Mide el libro nocturno (practicamente muerto por
diseño), no la liquidez real durante la rueda - exactamente la pregunta
que el usuario hizo no se puede responder con este dato. Ahora que la
rotacion diaria esta activa (`data/market_snapshot_log.py`, MEJORA
2026-10-08), cada dia de produccion queda en su propio archivo
descargable de Northflank (`market_snapshots-YYYY-MM-DD.csv.gz`) - en
cuanto exista uno que cubra una rueda real completa, la medicion pedida
es directa de correr.

**Respaldo implementado y testeado** (no se esperó a tener la medicion
real - el mecanismo en si no depende de ella, solo su calibracion fina):
`RiskConfig.enable_quote_gap_backstop` (default `True`, risk-reducing).
Dos gatillos independientes (`risk/risk_manager.py::
RiskManager.evaluate_quote_gap_backstop`):

1. **`catastrophic_loss_backstop`**: el PnL% sobre la prima, medido con el
   ULTIMO precio conocido (bid/ask>0, aunque ese book no pase
   `is_valid_quote()` - es precisamente el book que el filtro normal
   ignora), ya es <= -75%. No espera los N minutos.
2. **`quote_gap_backstop`**: el book lleva sin pasar `is_valid_quote()` por
   >= 10 minutos, sin importar el PnL conocido.

Cuando dispara: `mid_price_exec.py::MidPriceExecutionEngine.
submit_protected_limit` arma una orden **LIMITE** (nunca de mercado) a
`mid * (1 - 5%)` al vender / `mid * (1 + 5%)` al comprar, usando el
ULTIMO book conocido (que puede ser ancho/viejo - ese es el punto).
Integrado en `run_bot.py::GgalOptionsBot._apply_quote_gap_backstop`,
corre en cada ciclo para CUALQUIER posicion abierta (larga o corta, de
cualquier strategy_tag) con tracking propio (`_position_invalid_quote_since`
/`_position_invalid_quote_last_mid`), separado del tracker de la alerta
puramente informativa ya existente (`_position_missing_quote_since`) para
no alterar su comportamiento ya testeado. Si no hay NINGUN precio
conocido (bid/ask=0 siempre), no se fabrica una orden - se loguea error y
se sigue confiando en la alerta informativa.

**Los 3 valores son DEFAULTS RAZONADOS, explicitamente NO medidos** (ver
comentario extenso en `config.py`, por honestidad no se repite completo
aca) - construidos sobre el UNICO precedente real que ya existia para
cada uno en este mismo archivo, nunca un numero arbitrario nuevo:

| Parametro | Default propuesto | Precedente real usado |
|---|---|---|
| N minutos (`quote_gap_backstop_minutes`) | 10 min | 2x `stale_quote_warning_seconds` (300s=5min, el unico umbral de "cuanto esperar" ya existente) |
| Umbral catastrofico (`quote_gap_backstop_catastrophic_loss_pct`) | 75% | Mas severo que `stop_loss_pct` (50%) y que su etapa mas angosta (20%) - ultimo resorte, no reemplazo |
| Margen del limite (`quote_gap_backstop_limit_margin_pct`) | 5% | Reutiliza `max_spread_relative`, ya establecido para decisiones de calidad de ejecucion |

**Pendiente explicito**: recalibrar los 3 en cuanto exista un export de
`market_snapshots.csv` que cubra una rueda real completa - hasta entonces
estos quedan como "mejor estimacion razonada", nunca como "medido".
17 tests nuevos (`ggal_bot/validation/test_quote_gap_backstop.py`), suite
completa **808/808**.

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

**Historico: RESUELTO via calculo propio (2026-10-14).** El bot tiene un
logger de cotizaciones de bonos ya construido
(`ggal_bot/data/ccl_bond_quote_log.py`, GD30/GD30C/AL30/AL30C via
data912.com) pero es opt-in y **nunca estuvo activo en produccion**:
`logs/ccl_bond_quotes.csv` tiene solo el header (53 bytes, creado
2026-09-30, cero filas de datos desde entonces). El endpoint
`data912.com/live/arg_bonds` (verificado via WebFetch) devuelve
unicamente un snapshot actual sin timestamps ni serie historica - no hay
forma de reconstruir CCL historico desde los bonos tampoco.

En vez de eso, se calculo el **CCL implicito diario** = GGAL local (BCBA,
ARS) x 10 / GGAL ADR (Nasdaq, USD), con las dos series de IOL ya
verificadas en la seccion 3 (ratio 10:1 confirmado, cross-validado 0
discrepancias). Serie resultante: **2.546 filas** (fechas con cierre en
AMBAS plazas simultaneamente, 2016-2026), entregada en
`ccl_implicito_diario.csv`. **Validacion contra un valor real
independiente**: para 2026-08-13, el CCL calculado da **$1.592,69**
vs. **$1.580,29** reportado por dolarhistorico.com (fuente independiente)
- diferencia de **0,78%**, validacion fuerte de la formula.

**Intradia (pedido del usuario - el logueo lo activa el usuario mismo):**
el logger de bonos ya construido se activa con la env var
**`GGAL_BOT_ENABLE_CCL_BOND_QUOTE_LOG`** (default `false`,
`ShadowConfig.enable_ccl_bond_quote_log`, `config.py` linea ~919) - sin
cambios de codigo necesarios, ya corre en cada ciclo si esta prendida.

### 5. Disponibilidad de datos: caucion

**Historico: DATA INSUFFICIENT**, sin cambios (ver investigacion de la
Fase 0: la tool `get_caucion_rates` de IOL solo expone la tasa VIGENTE
para los proximos 1-3 dias habiles, nunca historia; no se encontro ninguna
fuente historica accesible desde esta sesion).

**Intradia (pedido del usuario - "agregá el registro de la tasa de
caución de IOL en cada ciclo"): BLOQUEADO, no se fabrico un endpoint.**
A diferencia del logger de bonos (endpoint `data912.com/live/arg_bonds`
YA confirmado y en uso), este proyecto no tiene ningun cliente REST de
caucion, confirmado o inferido (`grep -rn caucion ggal_bot/` solo
encuentra comentarios sobre la tasa FIJA de Black-Scholes). Investigacion
hecha antes de escribir codigo: el tool `get_caucion_rates` del MCP de
IOL (uso exclusivo de esta sesion, NO disponible para el bot en
produccion) confirma que el DATO existe (`days`, `due_date`, `rate`,
`min_amount`) pero no revela que URL REST usa internamente. Por busqueda
web se encontro un UNICO ejemplo real, un gist de un tercero
(`github.com/alejoasotelo`) que pega contra
`POST https://iol.invertironline.com/Mercado/GetCaucionPuntas` - pero eso
es el backend AJAX INTERNO del sitio web (cookie de sesion del login web),
un mecanismo de autenticacion DISTINTO al OAuth Bearer que usa
`BrokerRestSource` para todo lo demas en este proyecto. Construir sobre
un endpoint sin documentar, con un esquema de auth nuevo, violaria la
misma disciplina que ya se siguio para cotizacion/opciones (confirmar
contra una cuenta real con un script `diagnose_*` ANTES de escribir el
cliente de produccion - ver `diagnose_iol_api.py`).

**Entregado en su lugar**: `diagnose_iol_caucion.py` (mismo patron que
`diagnose_iol_api.py`) - prueba candidatos SIN CONFIRMAR sobre la API
publica (Bearer token) y deja constancia completa del hallazgo del
endpoint AJAX interno, para que el usuario lo corra contra su cuenta real
(.env, nunca visto por esta sesion) y confirme (o descarte) una ruta real
antes de que se escriba el `CaucionRateLogger` de produccion. Hasta
entonces, la hipotesis 3 (paridad put-call vs. caucion) sigue en backlog
- "datos acumulándose" NO aplica aca todavia porque no hay NINGUN cliente
confirmado para empezar a acumular.

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

Sobre el "11": revise el transcript de esta sesion buscando "11
posiciones"/"unknown_legacy" y solo encontre ese numero en mensajes MIOS
anteriores, nunca en uno del usuario - es decir, no puedo descartar que
lo haya introducido yo mismo en algun momento sin una fuente real detras.
No tengo como reconstruir de forma concluyente de donde salio mas alla de
eso. Dado que la reconciliacion de arriba (2 posiciones netas reales,
trazada paso a paso) es sólida independientemente de ese numero, no le
dedico mas investigacion - si el usuario recuerda una fuente puntual
(dashboard en un momento especifico, un log de Northflank), lo reconcilio
contra eso.

### 6bis. Por que el chequeo de invariantes no marcaba GFGC6600OC -8 (RESUELTO, 2026-10-14)

**Causa raiz encontrada y corregida.** El chequeo existente
(`check_long_only` en `audit_journal_invariants.py`) solo mira
`quantity_after` FILA POR FILA - nunca ve negativo para GFGC6600OC porque
sus 4 filas "misteriosas" (PARTIAL_EXIT 3x + CLOSE, 2026-09-30 a
2026-10-01) van 4.0 -> 2.0 -> 1.0 -> 0.0, todas >= 0 individualmente, aun
cuando la posicion es neta -8 cuando se reconstruyen los fills de forma
independiente. Razon de fondo: cada una de esas 4 filas usa un
`position_id` DISTINTO y nunca reusado, sin ninguna fila ENTRY en todo el
journal que explique de donde salio la cantidad inicial (presumiblemente
8) de la que se va descontando - un hueco real de logging, no una
violacion que el chequeo por-fila pueda ver en principio.

**Fix**: nuevo chequeo `check_cumulative_long_only_ledger` (commit de
hoy) que suma `quantity_delta` CRONOLOGICAMENTE por (symbol,
strategy_tag), deliberadamente SIN pasar por `position_id` (que no
persiste de forma confiable entre restarts del bot - el propio
GFGC6600OC usa 3 `position_id` distintos solo en su ciclo limpio original
del 2026-09-08/11), excluyendo las filas `reason="spread_completion"`
(ver mas abajo). Corrido contra el journal real completo: marca
**CRITICAL** en GFGC6600OC (2026-09-30T17:52:07 UTC, el acumulado ya
habia estado en 0 - cerro limpio el 2026-09-11 - y cae a -4 sin ninguna
ENTRY nueva que lo explique; sigue cayendo con las filas siguientes hasta
el -8 final ya reportado arriba) y **INFO** en los 6 simbolos
"unknown_legacy" ya documentados (arrancan negativos desde su primera
fila en el journal, consistente con fills anteriores al inicio real del
journal, no un gap nuevo).

**Hallazgo colateral (falso positivo corregido de paso)**: el chequeo
viejo SI marcaba `GFGV5000OC` (2026-10-01, ENTRY quantity_after=-75.0)
como CRITICAL - pero ese es el wing corto de un spread completion de
weekly_asymmetric (`reason="spread_completion"`, vende una pata corta YA
financiada por una larga confirmada, por diseño), cerrado legitimamente
al dia siguiente via `manual_close`. La vieja docstring del chequeo
afirmaba "ninguna estrategia vende en descubierto", lo cual es FALSO para
este mecanismo especifico de weekly_asymmetric. Corregido excluyendo esas
filas de ambos chequeos.

17 tests nuevos en total entre ambos chequeos y el respaldo de quotes
(ver 2bis), suite completa **808/808**.

**Por que el reset no cerro ninguna de las dos (GFGC6600OC ni el
subyacente)**: no es que el reset haya corrido y fallado en barrer
`shadow_trades.csv` (esa correccion - que el usuario ya habia pedido -
esta implementada desde el 2026-10-05 y funciona). Es mas simple: el
evento `SHADOW_RESET` tiene **CERO ocurrencias** en todo el journal real
disponible - el reset nunca se ejecuto en esta corrida. Sobre el
subyacente especificamente: el propio docstring de `_perform_shadow_reset`
documenta que el usuario ya habia decidido explicitamente (2026-10-05) NO
reconciliar esa pata a proposito, tratandola como "residuo de la era
mock" - coincide exactamente con como la caracterizaste ahora.

**Pendiente, no resuelto todavia**: el mecanismo EXACTO de como
GFGC6600OC llego a tener ~8 contratos en el portfolio en memoria del bot
sin ninguna ENTRY logueada - `_reconcile_portfolio_on_startup` reconstruye
el portfolio ESTRICTAMENTE desde el journal, que muestra este simbolo en
0 al 2026-09-11. Hay una contradiccion real ahi (¿un bug en
`reconstruct_positions_from_event_journal`? ¿un path de codigo distinto
que agrega una posicion sin pasar por el journal?) que no llegue a
rastrear a su origen exacto en esta sesion - puedo seguir investigando si
el usuario lo pide.

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
| 1 | Provision de liquidez | book local (ya logueado) | **Datos acumulándose** (ver 2026-10-14 abajo) |
| 2 | Venta de vol riesgo definido (VRP) | opciones locales + ADR (vol historica) | **Stress gate: SOBREVIVE condicionado** (ver pre-registro abajo) |
| 3 | Paridad put-call vs. caucion | caucion historica | **DATA INSUFFICIENT** - backlog (logging intradia bloqueado, ver seccion 5) |
| 4 | Lead-lag ADR x CCL -> opciones locales | ADR (ok) + CCL **ahora calculado** | **Testeable** (CCL historico resuelto, ver seccion 4) |
| 5 | scalping / weekly_asymmetric (sin privilegio) | ya logueado | **Testeable**, con filtro de quotes + respaldo aplicados |

Backlog explicito (3): queda pre-registrada en texto para no perderse,
pero no se testea hasta tener caucion historica o forward logging
(bloqueado, ver seccion 5). El item 4 se desbloqueo esta sesion (CCL
historico resuelto por calculo propio).

### Hipotesis 1 — actualizacion 2026-10-14: "datos acumulándose"

**Confirmado: `market_snapshots.csv` NO registraba el ultimo precio
operado ni el volumen por contrato** (pedido explicito del usuario antes
de testear H1 - "sin eso no se puede simular un fill pasivo
conservador"). El dato SI se parseaba en memoria desde el origen real
(`RawQuote.last_price`/`last_volume`, poblado desde el campo "c"/"v" de
data912) pero se perdia en la frontera `OrderBookSnapshot` (solo tenia
`last_volume`, nunca `last_price`) y nunca se persistia al CSV (ni
siquiera `last_volume`, que si existia). **Corregido**: 6 archivos
(`option_chain.py`, `live_shadow_feed.py`, `market_data_feed.py`,
`market_snapshot_log.py`, `dashboard/data/market_data.py`,
`test_market_snapshot_log.py`), nuevas columnas `last_price`/`last_volume`
agregadas AL FINAL del header fijo (seguro porque el archivo rota a
diario desde el 2026-10-08 - cada dia es su propio archivo autonomo). 1
test nuevo, suite **780/780** en el momento de este cambio.

Por pedido explicito del usuario, H1 se marca **"datos acumulándose"** en
vez de inventar un modelo de fill: recien desde hoy el campo se
persiste, asi que no hay historia real todavia para calibrar el modelo
de fill pasivo conservador (fill solo si un trade posterior atraviesa el
limite). Se retestea en cuanto haya suficientes dias de este dato nuevo
acumulado en produccion.

## Pre-registro — Hipotesis 2: Venta de volatilidad con riesgo definido

**Pre-registrado 2026-10-14, ANTES de correr ningun backtest histórico**
(que, ademas, no es posible en sentido estricto: H2 nunca operó, no hay
fills reales que reconstruir - ver limitacion ya documentada de que la
historia de IV solo existe desde que `market_snapshots.csv` arranco).
El gate obligatorio que pidio el usuario ("si no sobrevive con el sizing
propuesto, se descarta aunque el backtest dé positivo") se corre ANTES
que cualquier backtest, con Black-Scholes propio (`models/black_scholes.py`,
sin libreria externa) sobre los datos REALES de ADR/vol ya verificados en
la seccion 3 - nunca se fabrica un precio.

**Estructura (pre-registrada)**: bull put spread semanal sobre GGAL
local (BYMA) - vender un put ~5% OTM (strike corto), comprar un put ~10%
OTM (strike largo), ancho del spread = 5% del spot. Secuencia de entrada
**long-first** (comprar la pata larga/protectora PRIMERO, confirmar el
fill, vender la pata corta recien despues) - EXACTAMENTE el mismo
mecanismo ya probado en produccion para weekly_asymmetric
(`_act_on_spread_completion_signal`/`LongFirstConfig`, ver seccion 6bis:
"garantiza que nunca se llega aca sin esa larga ya en el portafolio") en
vez de inventar una secuencia nueva.

**Sizing (pre-registrado)**: capital propuesto ARS 1.000.000 (misma
magnitud que `LongFirstConfig.max_capital_ars`/`ScalpingConfig.
max_capital_ars`, ningun numero nuevo inventado), N spreads = capital /
perdida_maxima_por_spread (sizing por PEOR CASO, nunca por valor
esperado).

**Numeros reales (BS, spot=6010 ARS, ultimo cierre local real 2026-10-13;
sigma=32.69%, vol realizada real de los ultimos 25 dias del bot; tasa
anual=40%, `RiskConfig.default_annual_rate`, unico dato real disponible)**:
strike corto=5700, strike largo=5400, ancho=300. Credito neto BS=1.125
ARS/contrato. Perdida maxima definida=28.875 ARS/contrato. n_spreads=34.
Perdida maxima agregada si TODOS llegan a perdida maxima=981.733 ARS (97%
del capital propuesto).

**Gate obligatorio — crash de 10 ruedas (-61,6%, PASO 2019) y los 10
peores dias individuales del ADR** (aplicado como shock real al
subyacente local - ver nota metodologica abajo):

| Escenario | Spread COMPLETO (ambas patas) | Solo pata larga (legging, long-first) |
|---|---|---|
| Crash 10 ruedas -61,6% | -28.875 ARS/spread (= tope estructural, SIN sorpresa) | +309.162 ARS/spread (GANANCIA - la pata larga sola se revaloriza) |
| Peor dia individual (2019-08-12, -56,44%) | -28.875 ARS/spread | +278.151 ARS/spread (GANANCIA) |
| Resto de los 9 peores dias (-11,7% a -23,5%) | -28.875 ARS/spread (igual, tope estructural) | Ganancia menor pero siempre positiva |

**VEREDICTO: SOBREVIVE, CONDICIONADO.** Con secuencia long-first, el
riesgo de "legging" (que una pata quede sola) NUNCA es el escenario
catastrofico que se evaluo primero por error (ver nota abajo) - si falla
la venta de la pata corta, el bot queda con la pata larga SOLA, que
GANA en un crash (nunca pierde mas que la prima pagada por esa pata, ~54
ARS/contrato). La perdida esta ESTRUCTURALMENTE acotada en 28.875
ARS/spread sin importar la magnitud del crash, mientras la secuencia sea
long-first. La condicion no es opcional: si H2 se implementara con la
pata corta PRIMERO (orden inverso), el analisis cambia por completo (una
pata corta sola, sin proteccion, en un crash de puts es perdida casi
total de la garantia) - **H2 solo se aprueba para la secuencia
long-first, nunca para la secuencia corta-primero**.

**Riesgo residual NO cubierto por este gate (flagged, no resuelto)**:
ejercicio anticipado (BYMA es americano - ver docstring de
`black_scholes.py`) sobre la pata corta mientras esta abierta. Verificado
por grep: este proyecto no tiene NINGUN mecanismo de deteccion/manejo de
asignacion anticipada en ningun lado (ni para H2 ni para los wings cortos
ya existentes de weekly_asymmetric) - riesgo real, pre-existente, no
nuevo de H2, pero que H2 hace mas relevante por ser su mecanismo central.

**Segmentado por regimen de vol (mismo strike fijo 5700/5400, sigma
variable)**:

| Regimen | sigma | Credito/spread | Perdida max/spread | n_spreads posibles (cap 1M) |
|---|---|---|---|---|
| Minimo historico 10y | 16.19% | 16 ARS | 29.984 ARS | 33 |
| **Regimen actual del bot (percentil 11.7)** | **32.69%** | **1.125 ARS** | **28.875 ARS** | **34** |
| Mediana historica 10y | 49.57% | 3.498 ARS | 26.502 ARS | 37 |
| Media historica 10y | 56.02% | 4.386 ARS | 25.614 ARS | 39 |
| Crisis (referencia ilustrativa) | 100% | 8.700 ARS | 21.300 ARS | 46 |

**Hallazgo economico adicional (no pedido, pero relevante)**: en el
regimen ACTUAL (percentil 11.7, atipicamente calmo), el credito cobrado
es solo **3.9%** de la perdida maxima arriesgada (1.125/28.875) - una
relacion riesgo/beneficio muy pobre comparada con regimenes de vol mas
altos (13.2% en la mediana historica, 40.8% en un escenario de crisis).
Esto sugiere que H2 NO deberia venderse todas las semanas sin condicion:
la tesis de vender prima de riesgo de volatilidad es mas defendible
cuando la vol vigente esta por encima de su propia mediana historica, no
cuando esta en su percentil 11.7 como ahora. Pre-registro de la
condicion de entrada antes de testear con datos reales: entrar solo
cuando la vol realizada de 20 ruedas este por encima de su mediana
historica (49.57%) - a confirmar/ajustar cuando haya suficiente historia
de IV real (`market_snapshots.csv`) para comparar contra IV, no solo vol
realizada.

**Nota metodologica (shock aplicado al ADR, no al precio local
nominal)**: el shock de crash se aplica directamente como % sobre el
spot LOCAL, usando el movimiento del ADR en USD - no el movimiento
nominal de GGAL en ARS. Esto es deliberado, no un error: el usuario ya
pidio evitar sesgo de inflacion/devaluacion de pesos en el modulo de
regimen (seccion 3), y durante una devaluacion real (PASO 2019) el precio
NOMINAL en ARS de GGAL no cae tanto como el ADR en USD porque el peso
tambien se devalua al mismo tiempo - el movimiento del ADR es la medida
de riesgo REAL (ajustada por inflacion/devaluacion), consistente con el
CCL implicito de la seccion 4 (Local_ARS = ADR_USD x CCL / 10 por
identidad casi-arbitraje).

**Primer intento con un sizing ingenuo (descartado antes de publicarse
aca)**: la primera version de este analisis asumio secuencia
corta-primero (vender la pata corta antes de la larga) y encontro que un
fallo de "legging" en ese orden exponia hasta 338.037 ARS/contrato (11.7x
la perdida maxima definida) bajo el mismo crash - habria descartado H2 de
plano. Se corrigio releyendo el codigo real de weekly_asymmetric (ver
seccion 6bis) y encontrando que esa estrategia YA resuelve este problema
exacto con secuencia long-first - H2 reutiliza esa solucion en vez de
re-inventar una nueva. Se deja constancia del error y la correccion por
transparencia (nunca se publica un resultado sin volver a chequearlo
contra el codigo real).

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
