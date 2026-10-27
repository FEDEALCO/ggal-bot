# RESEARCH.md — Mandato de research cuantitativo GGAL_BOT

Iniciado 2026-10-13, a pedido explicito del usuario (ver hilo de auditoria).
Documento vivo: se actualiza con cada hallazgo, pre-registro y resultado.
Disciplina de todo el proyecto aplica aca tambien: nunca fabricar un
numero, dato o resultado; todo lo que sigue esta trazado a una fuente
(codigo, log real, fuente externa citada, o tool call de esta sesion).

**CONGELAMIENTO DE LOGICA DE TRADING, 2026-10-15 a 2026-11-05**: ver
`FREEZE.md` (raiz del repo). Desde el deploy del 2026-10-15, solo bugs de
integridad de datos o de riesgo - nada de features ni cambios de
parametros - para preservar una muestra de una sola version. Este
documento sigue actualizandose con analisis OFFLINE durante la ventana
(eso esta explicitamente permitido - ver FREEZE.md).

**REVISION SEMANAL EN UN COMANDO (a pedido explicito del usuario,
2026-10-15, punto 4)**: `python -m ggal_bot.ops.weekly_review` (correr
desde la raiz del repo, todos los viernes despues de las 17:00). Junta,
desde los logs reales (nunca fabrica nada - cualquier seccion sin datos
dice DATA INSUFFICIENT en vez de inventar un numero): trades y PnL neto
por estrategia con IC bootstrap, violaciones de invariantes, fills fuera
de horario de rueda, % de quotes validas por contrato operado, disparos
del backstop de ultimo resorte, y acumulacion de datos para Hipotesis 1
(filas con `last_price>0`). 22 tests en
`ggal_bot/validation/test_weekly_review.py`. Ver docstring completo del
modulo para el detalle de cada fuente reutilizada (audit_journal_
invariants.py, backtest/metrics.py, dashboard/data/market_hours_quality.py).

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

**CORRECCION 2026-10-14 (a pedido explicito del usuario, caso real
GFGC6800OC: book 64 -> 15 en la apertura) - el disparo catastrofico
descripto arriba estaba MAL CALCULADO.** `last_known_pnl_pct` se medía
contra el ULTIMO `book.mid` con dos puntas (bid>0 y ask>0) - pero ese
criterio es mas laxo que `is_valid_quote()`: un book que llega a tener dos
puntas pero con spread anormal (o stale) YA fallo el filtro, y su mid
puede ser un precio roto, no uno real. Verificado con el caso del usuario:
un book que pasa de un ultimo precio operable ~64 a un book roto con
mid~15 (sin que el subyacente se haya movido) habria calculado
(15-64)/64=-76,6% - por encima del umbral catastrofico default (75%) -
**disparando un cierre forzado sobre una perdida que nunca existio.**

**Fix**: `last_mid`/`last_valid_iv` (nuevo tracking) ahora se actualizan
EXCLUSIVAMENTE en un ciclo donde `is_valid_quote()` fue True - nunca con
un book que ya fallo el filtro. Cuando el book vigente es invalido, tanto
el PnL% del disparo catastrofico como el precio base de la orden limite
protegida (`submit_protected_limit`, nuevo parametro `reference_value`) se
calculan contra un **VALOR TEORICO** (Black-Scholes, `models/
black_scholes.py`, con el spot vigente VALIDADO + la ultima IV valida
conocida) - nunca contra el mid de un book roto. Si todavia no hay
ninguna IV valida conocida (ej. el book ya nacio invalido), el valor
teorico queda en `None` - nunca se fabrica un dato: el disparo
catastrofico simplemente no se evalua ese ciclo (el disparo por TIEMPO
sigue funcionando igual, independiente de esto).

**LIMITACION EXPLICITA, NO RESUELTA EN ESTA CORRECCION**: en modo shadow,
`OrderGateway.send()` sigue simulando el FILL cruzando el book REAL
vigente (bid/ask), no el precio teorico de la orden - es decir, la orden
se ARMA correctamente contra el valor teorico, pero el simulador de
shadow no modela la semantica de un LIMITE real (que nunca ejecutaria
peor que el limite puesto) cuando el book sigue roto. Queda documentado en
el codigo (`mid_price_exec.py::submit_protected_limit`) para que el
usuario decida si lo prioriza - no es lo que se pidio corregir aca (que
era la base de PRECIO/DISPARO, no la fidelidad del simulador de fills).

**Test con el caso real**: `test_real_gfgc6800oc_case_broken_book_does_not_false_trigger_catastrophic`
(numeros representativos de la misma magnitud que el caso real - 64≈60.03,
15 exacto - verificados con `BlackScholesGreeks.price()` directo, ver el
docstring del test; no hay en este sandbox la fila cruda de
`market_snapshots.csv` de ese instante especifico). Reproduce la logica
VIEJA inline (sin pasar por el bot, ya que ese codigo ya no existe en el
repo) y confirma que habria disparado (-75,0%, cruza el umbral), mientras
que la logica NUEVA, corriendo contra el bot real, NO cierra la posicion
(valor teorico implica -6,4%, muy por debajo del umbral). 2 tests
preexistentes (`test_catastrophic_loss_forces_close_immediately_even_before_n_minutes`,
`test_short_position_loses_when_price_rises_not_falls`) se ajustaron para
sembrar una IV valida antes de romper el book (antes, ambos jamas tenian
una IV valida - este fix ahora la exige para poder calcular un valor
teorico). Suite completa **809/809**.

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
subyacente) — CORREGIDO/MATIZADO 2026-10-14 (correccion #4 del usuario)**:
la afirmacion de arriba ("el reset nunca se ejecuto en esta corrida") fue
IMPRECISA tal como esta escrita - cero ocurrencias de `SHADOW_RESET` en el
journal es compatible con DOS explicaciones distintas, y la version
anterior de este documento solo contemplaba la mas alarmante:

  (a) el flag `GGAL_BOT_SHADOW_RESET_ON_START` nunca se activo en
      Northflank (default `False` en `ggal_bot/config.py` linea 900,
      verificado por grep) - en ese caso, cero eventos es el
      comportamiento ESPERADO, no un bug;
  (b) el flag SI estuvo activo pero un build pre-fix seguia corriendo -
      en ese caso, cero eventos SI serian un problema real.

El propio codigo de `_perform_shadow_reset` (comentario "FIX REAL...
Verificado contra produccion (2026-10-05)") afirma textualmente que (b)
fue lo que se confirmo en esa sesion: el flag SI estaba activo. Esta
sesion no tiene forma de re-verificar eso de forma independiente (no hay
acceso a las variables de entorno reales de Northflank desde el sandbox) -
se traslada la afirmacion tal cual, sin confirmarla de nuevo, y se le pide
al usuario el chequeo cruzado de abajo.

**Verificacion pedida (commit + SHA)**: los DOS commits del fix de
"siempre escribir el checkpoint SHADOW_RESET" (pedido 2026-10-05) SI estan
en `main`, confirmados via `git log`/`git merge-base --is-ancestor` contra
HEAD local Y `origin/main` (ambos ancestros limpios, sin divergencia):

| Commit | SHA completo | Fecha |
|---|---|---|
| `_perform_shadow_reset`: siempre escribe el checkpoint SHADOW_RESET | `7bb91fb010a54725488959b3e10a4b3fbe01b016` | 2026-10-05 17:48:29 UTC |
| `_perform_shadow_reset`: barre tambien `shadow_trades.csv` | `ab327423aba5d77a7a1025d5724571fcd3bf5e39` | 2026-10-05 17:54:25 UTC |

**Como confirmar que Northflank corre esto**: `run_bot.py` ya expone
`ggal_bot.version_info.get_deployed_git_sha()` (dos call sites, el
principal en `StateWriter.write(deployed_git_sha=...)` dentro del ciclo
normal) - lee la env var `GGAL_BOT_GIT_SHA`, que **nunca se auto-completa**
(el propio modulo documenta por que: `.git/` esta excluido del build
context de Docker, asi que el SHA solo llega si quien dispara el build lo
pasa explicitamente como `--build-arg GIT_SHA=$(git rev-parse HEAD)`).
Si Northflank no tiene ese build-arg configurado, el campo queda en
`"unknown"` SIEMPRE, sin importar que SHA este realmente desplegado - en
ese caso esta verificacion no es posible hasta configurar ese paso de
build/CI (fuera del alcance de este repo). Si SI esta configurado, el
usuario puede leer `deployed_git_sha` en el estado que publica el bot
(dashboard/el JSON que lee `StateWriter`) y compararlo directo contra la
columna "SHA completo" de la tabla de arriba.

**PRIORIDAD 1 (correccion #3 del usuario) — rastreo completo de
`portfolio.add`/mutacion directa de `Position.quantity`, cruzado contra si
cada uno escribe al journal.** `Portfolio` (ggal_bot/portfolio/
portfolio.py) solo tiene DOS metodos mutadores: `add()` (append) y
`clear()` (vacia todo) - no existe `remove()`/`update()`. Las
"reducciones"/"cierres" de una posicion EXISTENTE se hacen por mutacion
directa de `pos.quantity` en el código de cada estrategia, no por un
metodo de Portfolio. Lista COMPLETA de cada sitio en `run_bot.py` (nunca
en tests) que llama a `self.portfolio.add(...)` o muta `.quantity`
directamente, cruzada contra si ese mismo sitio tambien llama a
`self.position_event_journal.log_event(...)`:

| Sitio (run_bot.py) | Mutacion | ¿Loguea al journal? |
|---|---|---|
| `_reconcile_portfolio_on_startup` (linea ~778) | `self.portfolio.add(pos)` en loop, reconstruyendo desde el journal | **NO** (deliberado - ver mas abajo, es un REPLAY de eventos YA logueados, no un evento nuevo) |
| `_perform_shadow_reset`/`_close_at_mid` (linea ~946) | `pos.quantity = 0.0` | SI, `log_event("SHADOW_RESET", ...)` **ANTES** de la mutacion (linea ~927) |
| `_apply_quote_gap_backstop` (linea ~1547) | `p.quantity += ...` | SI, `log_event("CLOSE"/"REDUCE", ...)` inmediatamente despues |
| `_act_on_signal` (vol_arbitrage ENTRY, linea ~2432) | `self.portfolio.add(new_pos)` | SI, `log_event("ENTRY", ...)` inmediatamente despues |
| `_act_on_exit_signal` (linea ~2637) | `pos.quantity -= reduce_qty` | SI, `log_event("CLOSE"/"PARTIAL_EXIT"/"REDUCE", ...)` por lote, dentro del mismo loop |
| `_act_on_naked_short_wing_exit_signal` (linea ~2713) | `pos.quantity += reduce_qty` | SI, `log_event("CLOSE"/"REDUCE", ...)` inmediatamente despues |
| `_act_on_entry_signal` (weekly_asymmetric/scalping ENTRY, linea ~2933) | `self.portfolio.add(new_pos)` | SI, `log_event("ENTRY", ...)` inmediatamente despues |
| `_act_on_spread_completion_signal` (ala corta de spread, linea ~2999) | `self.portfolio.add(new_short_leg)` | SI, `log_event("ENTRY", ..., reason=...)` inmediatamente despues |
| delta-hedge, hedge nuevo (linea ~3148) | `self.portfolio.add(new_hedge)` | SI, `log_event("ENTRY", strategy_tag="delta_hedge", ...)` inmediatamente despues |
| delta-hedge, hedge existente (linea ~3173) | `existing_hedge.quantity = old_qty + signed_qty` | SI, `log_event("ENTRY"/"ADD"/"CLOSE"/"REDUCE", ...)` inmediatamente despues |

`Portfolio.clear()` (el otro mutador posible): **0 call sites** en
`run_bot.py` - no se usa en produccion hoy.

**Conclusion: HOY (HEAD actual) no hay ningun camino de produccion que
mueva `quantity` sin loguear un evento nuevo**, con la UNICA excepcion
deliberada y correcta de la reconciliacion de arranque (que no es un
evento nuevo, es releer eventos que YA estan en el journal). Pero esa
misma funcion **NO SIEMPRE** fue un replay puro del journal - y ahi esta
la causa raiz real, encontrada con evidencia directa del journal:

**CAUSA RAIZ ENCONTRADA (con evidencia del journal real, no solo del
codigo)**: `_reconcile_portfolio_on_startup` leyo `logs/shadow_trades.csv`
(un log de FILLS, no el Event Journal) desde su introduccion
(`b3397fd`, 2026-09-07) hasta el fix `019a4dc` ("Fix URGENTE:
reconciliacion de arranque lee el Event Journal, no shadow_trades.csv",
**2026-10-01 18:48:21 UTC**, confirmado ancestro de HEAD y de
`origin/main`). En ese periodo, el loop `for pos in positions:
self.portfolio.add(pos)` (identico al de hoy en estructura) NO TENIA
ningun `log_event` - cada restart del proceso (ej. un redeploy de
Northflank) que encontrara una cantidad neta abierta en
`shadow_trades.csv` reconstruia esa posicion como un objeto `Position`
**nuevo** (position_id fresco, `uuid4()`, nunca reusado) y la agregaba al
portfolio en memoria sin dejar ninguna fila ENTRY en
`logs/position_events.csv`. El propio commit del fix documenta el bug
gemelo (BUG A: sin `strategy_tag`, el fill de dos estrategias distintas
sobre el mismo simbolo se mezclaba en una sola Position; BUG B:
`position_id` se reinventaba en cada restart).

**Verificado contra el journal real** (transcript `cat
logs/position_events.csv` pegado por el usuario, 2026-09-08 a 2026-10-05):
GFGC6600OC tiene, el 2026-09-30/10-01, exactamente 4 filas
(`PARTIAL_EXIT` x3 + `CLOSE` x1, cantidades originales implícitas 8, 4, 2,
1 contratos) cada una con un `position_id` que **no aparece en ninguna
otra fila de todo el journal** - ni una sola fila `ENTRY` los explica. Eso
es exactamente la firma que predice el bug: un restart (con el codigo
viejo, pre-`019a4dc`, todavia desplegado el 2026-09-30) encontro estas 4
cantidades en `shadow_trades.csv`, las reinyecto como Positions nuevas sin
loguear nada, y cuando la logica normal de salida las tocó mas tarde
(`_act_on_exit_signal`, que SI loguea) escribio PARTIAL_EXIT/CLOSE contra
un `position_id` que nunca tuvo un ENTRY. Esto, sumado a filas reales de
ENTRY/CLOSE limpias que SI netean a cero, es lo que produce el acumulado
neto -8 que `check_cumulative_long_only_ledger` detecta.

**Conclusion sobre el fix**: `019a4dc` (2026-10-01 18:48) cierra la causa
raiz HACIA ADELANTE - desde ese commit, la reconciliacion de arranque lee
el Event Journal mismo (nunca puede "inventar" una posicion que no esté ya
logueada) y reusa el `position_id` del evento mas reciente en vez de
fabricar uno nuevo. **No repara retroactivamente** las filas ya corruptas
del 2026-09-30/10-01 - esas 4 filas quedan permanentemente en el journal,
y son la razon por la que `check_cumulative_long_only_ledger` sigue
marcando CRITICAL en GFGC6600OC hoy, aun con el bug ya corregido desde
hace 13 dias.

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
| 2 | Venta de vol riesgo definido (VRP) | opciones locales + ADR (vol historica) | **Datos acumulándose** (gate rehecho 2026-10-14; test definitivo 2026-10-15 con puts OTM reales y distribucion empirica 10y: edge negativo en los 6 escenarios de vol probados, se descarta en el regimen actual - ver corrección 2026-10-15 abajo) |
| 3 | Paridad put-call vs. caucion | caucion historica | **DATA INSUFFICIENT** - backlog (logging intradia bloqueado, ver seccion 5) |
| 4 | Lead-lag ADR x CCL -> opciones locales | ADR (ok) + CCL **ahora calculado** | **Testeable** (CCL historico resuelto, ver seccion 4) |
| 5 | scalping / weekly_asymmetric (sin privilegio) | ya logueado | scalping: **APAGAR** (veredicto 2026-10-27, ver seccion dedicada abajo - PnL neto negativo, IC95% nunca cruza cero, historico y ventana reciente consistentes); weekly_asymmetric: **Testeable**, con filtro de quotes + respaldo aplicados |

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

**CORRECCION 2026-10-14 (correccion #5 del usuario) - el holdout NO puede
ser "desde ~2026-09-25".** Esa fecha venia de la seccion 2bis (el ultimo
25% del UNICO export de quotes disponible en ese momento, que cubria
fills hasta el 2026-10-12) - un corte que no tiene nada que ver con
`last_price`/`last_volume`. El commit que agrega esas dos columnas
(`55d9bc2`, "data: log last_price/last_volume en market_snapshots
(Hipotesis 1)") se hizo **hoy, 2026-10-14**, dentro de este mismo
seguimiento, y **todavia no esta desplegado** (el usuario planea aplicar
el bundle correspondiente recien despues de este intercambio) - es decir,
`logs/market_snapshots.csv` en produccion no tiene NINGUNA fila con
`last_price`/`last_volume` poblados todavia; la columna existe en el
header desde el fix pero esta vacia (0.0 por defecto) hasta que el build
con este commit corra en Northflank.

**Redefinicion correcta (operacional, no una fecha fija)**: el holdout de
H1 se define como *"el ultimo 25% (por dias habiles, no por filas) de las
ruedas cubiertas desde la PRIMERA fila con `last_price>0` o
`last_volume>0` registrada DESPUES del deploy de este fix"* - nunca una
fecha de calendario fijada de antemano, porque el momento exacto del
deploy (y por lo tanto el primer dia real de este dato) todavia no se
conoce desde este sandbox. En la practica: cuando llegue el momento de
testear H1, el primer paso es `grep` el primer timestamp con
`last_price>0` en el `market_snapshots.csv` (o su serie de archivos
rotados `.gz`) real de produccion, y recien ahi calcular el corte del 25%
sobre los dias habiles posteriores a ESE timestamp, no sobre el
2026-09-25.

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

**VEREDICTO ORIGINAL (2026-10-14, temprano) - RECHAZADO por el usuario,
corregido mas abajo: "SOBREVIVE, CONDICIONADO".** El razonamiento sobre
el riesgo de LEGGING (que una pata quede sola si falla el fill de la
corta) sigue siendo valido y se preserva: con secuencia long-first, un
fallo de legging deja al bot con la pata larga SOLA, que GANA en un
crash (nunca pierde mas que la prima pagada, ~54 ARS/contrato) - al
reves de lo que la primera version de este analisis encontro con
secuencia corta-primero (ver nota mas abajo). **Pero "sobrevive" referido
al SIZING AGREGADO (34 spreads, perdida maxima conjunta=97% del capital
si TODOS tocan su tope estructural el mismo dia) fue un error de
criterio, sealado correctamente por el usuario: perder el 97% del
capital en una semana no es sobrevivir, aunque cada spread individual
tenga su perdida "acotada". Que el riesgo de legging este resuelto no
dice nada sobre si el TAMAÑO AGREGADO de la apuesta es razonable - son
dos preguntas distintas.** Ver la correccion completa mas abajo
("CORRECCION 2026-10-14 (correccion #1 del usuario)"), que reemplaza
este veredicto.

**Riesgo residual NO cubierto por este gate (flagged, no resuelto)**:
ejercicio anticipado (BYMA es americano - ver docstring de
`black_scholes.py`) sobre la pata corta mientras esta abierta. Verificado
por grep: este proyecto no tiene NINGUN mecanismo de deteccion/manejo de
asignacion anticipada en ningun lado (ni para H2 ni para los wings cortos
ya existentes de weekly_asymmetric) - riesgo real, pre-existente, no
nuevo de H2, pero que H2 hace mas relevante por ser su mecanismo central.

## CORRECCION 2026-10-14 (correccion #1 del usuario) — rehecho completo

El usuario rechazo el veredicto "sobrevive" y pidio 4 cosas puntuales
antes de seguir: (a) credito con bid/ask REALES, no BS a vol realizada;
(b) win rate de breakeven contra probabilidad EMPIRICA de ITM (serie
ADR+local, no BS); (c) garantia BYMA + retorno sobre capital inmovilizado;
(d) manejo de ejercicio anticipado. Y una regla de sizing nueva: perdida
maxima semanal <=3% del capital. Resultado de cada punto, con numeros
reales:

**(a) Credito con bid/ask reales.** No existe en este sandbox un export
de `market_snapshots.csv` (bid/ask por opcion) que cubra una rueda real
completa (ver 2bis: el unico export disponible es una ventana nocturna de
~2hs, DATA INSUFFICIENT) - asi que reconstruir el credito EXACTO que
habria tenido esta estructura (strikes 5700/5400) en una fecha real no es
posible con lo disponible hoy. Lo que SI es reconstruible: el spread
bid/ask REAL que GGAL viene mostrando en ejecucion, medido sobre
`logs/shadow_trades.csv` **desde el fix real de simulacion de fills
(2026-10-01)** que empezo a registrar `bid_at_fill`/`ask_at_fill`/
`mid_at_fill` por cada fill (antes de ese fix, fill_price==reference_price
siempre - CERO spread simulado, inutilizable para esto). Sobre **228
fills reales de opciones GGAL** (`GFGC*`/`GFGV*`, 2026-10-05 a 10-09,
excluyendo 8 fills con spread>10% como libro roto/no operable - mismo
criterio que `is_valid_quote()`):

- Spread relativo real `(ask-bid)/mid`: mediana **2.74%**, media **2.97%**,
  p10=1.18%, p90=4.65%, max(dentro del corte)=9.43%.

Aplicando ESE spread real (no uno inventado) como haircut sobre el
credito BS-a-vol-realizada de la estructura pre-registrada (vender el
corto 5700 al BID, comprar el largo 5400 al ASK, spot=6010, sigma=32.69%,
tasa=40%, 5 dias calendario/3 habiles - mismos insumos que el intento
anterior):

| | Mid (BS, vol realizada) | Real (bid/ask, haircut medido) |
|---|---|---|
| Precio corto 5700 (put) | 4.417 | 4.356 (vendido al bid) |
| Precio largo 5400 (put) | 0.044 | 0.045 (comprado al ask) |
| Credito neto/contrato | 4.372 | 4.311 (-1,4%) |
| Perdida maxima/contrato | 295.63 | 295.69 |
| Perdida maxima/spread (x100) | 29.563 ARS | **29.569 ARS** |

**Hallazgo clave: el haircut de ejecucion real (bid/ask) es CHICO
(-1,4% sobre el credito) - el problema de fondo NO es el costo de
cruzar el spread, es el PRECIO EN SI** (ver punto (b) mas abajo). Nota:
estos numeros de BS difieren levemente de un intento anterior en este
mismo documento (credito BS=1.125, perdida max=28.875) porque ese intento
no dejo explicito su supuesto exacto de dias a vencimiento - esta version
lo deja explicito (5 dias calendario/3 habiles) para que sea reproducible;
la conclusion cualitativa (credito chico, perdida maxima ~ el ancho
completo) es la misma en ambos casos.

**(b) Win rate de breakeven vs. probabilidad EMPIRICA de ITM (serie real
ADR + local, 2016-2026, SIN Black-Scholes).** Win rate de breakeven
implicito en la estructura de arriba = perdida_maxima/ancho =
295,69/300 = **98,56%** (equivalente al ~96,3% citado por el usuario de
un intento previo - mismo orden de magnitud, la diferencia es el detalle
de dias a vencimiento). Se calculo la contraparte EMPIRICA: sobre
`ccl_implicito_diario.csv` (2.546 filas, 2016-2026, local ARS Y ADR USD
ya verificados en la seccion 4), la frecuencia historica real de que el
GGAL local (o el ADR) caiga >=5% en una ventana de 5 ruedas (una semana
habil, sin superposicion de feriados) - NUNCA con un modelo, contando
directo sobre los 2.541 retornos de 5 dias de toda la serie:

| Serie | Horizonte | Prob. empirica de caer >=5% en 5 ruedas |
|---|---|---|
| GGAL local (ARS) | 2016-2026 completo (n=2.541) | **18,18%** |
| GGAL ADR (USD) | 2016-2026 completo (n=2.541) | **22,16%** |
| GGAL local (ARS) | Solo ultimos ~2 anios (n=495) | **24,04%** |

**El win rate de breakeven implicito (98,56%, equivalente a asumir solo
1,44% de probabilidad de ITM) subestima la probabilidad EMPIRICA real de
ITM por un factor de ~12x a ~17x** (1,44% supuesto vs. 18-24% real,
segun la ventana). Esto es exactamente lo que el usuario anticipo: usar
vol realizada como si fuera IV, para un strike vendido ~5% OTM a una
semana, asume implicitamente una probabilidad de breach muchisimo mas
baja que la que la propia historia del activo muestra - "asume la
hipotesis falsa (prima de riesgo cero) por construccion", tal cual.
**VERIFIED** (no modelo: conteo directo sobre la serie real).

**(c) Garantia BYMA + retorno sobre capital inmovilizado.** **DATA
INSUFFICIENT, sin fuente verificada.** Esta sesion no tiene acceso a la
formula real de garantias de BYMA/CNV para una pata vendida de opciones
(ni documentacion propia del proyecto, ni un tool de esta sesion que la
devuelva - los tools de `mcp__invertironline__` disponibles cubren
caucion/portfolio/ordenes, no un calculo de margen de opciones). Nunca se
fabrica este numero. Camino recomendado para el usuario: `validate_order`
(tool de IOL, cuenta real) sobre un combo bull-put-spread hipotetico con
estos strikes devolveria el margen/garantia REAL que exige el broker -
no se ejecuto en esta sesion para no operar sobre la cuenta real del
usuario sin que lo pida explicitamente. Sin este dato, el "retorno sobre
capital inmovilizado" tampoco se puede calcular honestamente - queda
pendiente.

**(d) Manejo de ejercicio anticipado (americanas).** **CONFIRMADO, sin
ambiguedad: no existe NINGUN mecanismo de deteccion o manejo de
asignacion anticipada en todo el proyecto.** `black_scholes.py` tiene un
comentario que dice "el ejercicio anticipado se trata por fuera, en
risk/risk_manager.py, no aca" - pero `grep -n "ejercicio\|assignment\|
exercise" ggal_bot/risk/risk_manager.py` NO encuentra ninguna
implementacion real, solo un comentario generico sin codigo asociado. Si
IOL asignara la pata corta de H2 (o cualquier wing corto YA existente de
weekly_asymmetric) antes del vencimiento, el bot **no se entera** - el
`Position` en memoria seguiria mostrando la cantidad original hasta que
una reconciliacion de arranque (o una revision manual) lo detecte por la
diferencia contra el broker real. Riesgo real, PRE-EXISTENTE (no nuevo de
H2), que H2 vuelve mas relevante por depender de vender puts/calls cortos
como mecanismo central en vez de como wing secundario de un spread
largo-primero.

**ACTUALIZACION 2026-10-15 (pedido explicito del usuario, punto 2):**
mientras no exista deteccion real de asignacion (ver abajo), se agrego
una guardia PREVENTIVA que reduce la ventana de exposicion: cerrar
(`buy_to_close`) toda pata corta de `weekly_asymmetric` que este
ACTUALMENTE ITM, en los ultimos N dias habiles al vencimiento o antes de
una fecha ex-dividendo real configurada - ver
`strategy/weekly_asymmetric.py::build_itm_short_leg_risk_exit_signals` y
`config.LongFirstConfig.enable_itm_short_leg_assignment_guard` (**default
ON**, excepcion de riesgo igual que el resto de guardias de este
proyecto - nunca opt-in). 12 tests nuevos en
`test_long_first_mode.py`. Esto NO es deteccion de asignacion (eso sigue
sin existir, ver abajo): es una guardia que evita LLEGAR a una situacion
de asignacion en primer lugar, cerrando la exposicion antes de que el
riesgo se materialice. Si IOL asignara una pata corta ANTES de que esta
guardia alcance a cerrarla (ej. una asignacion fuera de horario, o una
fecha ex-dividendo no cargada en `ex_dividend_dates` porque este codigo
no tiene forma de conocerla con certeza - ver abajo), el bot sigue sin
enterarse hasta una reconciliacion de arranque.

**Dato real verificado hoy**: `get_next_corporate_events("GGAL")`
(conector IOL) devuelve `next_ex_date: null` - **VERIFIED, no hay ninguna
fecha ex-dividendo cargada en la fuente del broker para GGAL en este
momento.** Consistente con que `ex_dividend_dates` (config) este vacio
por defecto: hoy mismo, poblarlo con un valor real traido de ESTA fuente
no agregaria ninguna fecha (no hay ninguna pendiente) - queda como
tarea de monitoreo continuo, no como un dato que faltara cargar hoy.

**Que haria falta para DETECCION real de asignacion en modo vivo (no
solo esta guardia preventiva)** - documentado a pedido explicito del
usuario, sin implementarlo (el bot sigue en shadow, sin ordenes reales -
implementar esto recien tendria sentido al pasar a modo vivo):

1. **Fuente de datos**: un endpoint del broker que distinga un
   movimiento de "ejercicio"/"asignacion" de un trade comun. El conector
   IOL de esta sesion expone `get_activities` (movimientos de cuenta,
   default ultimos 30 dias) y `get_portfolio` (tenencias actuales) - en
   principio los candidatos naturales, pero esta sesion NO tiene
   confirmado el campo/codigo exacto que IOL usa para marcar un
   ejercicio/asignacion dentro de `get_activities` (**DATA
   INSUFFICIENT**: no se inspecciono su schema real ni se hizo la
   llamada, para no leer actividad real de la cuenta del usuario sin un
   motivo operativo concreto hoy - la respuesta de ese endpoint
   requeriria revisarse el dia que una asignacion real (o un ejercicio de
   prueba en un instrumento sin impacto) ocurra, para confirmar como se
   ve en los datos reales antes de programar contra un campo asumido).
2. **Poll o webhook**: `get_activities`/`get_portfolio` son pull (hay que
   consultarlos); el bot necesitaria un ciclo de reconciliacion
   adicional (ademas del de arranque que ya existe, ver seccion 6bis) que
   compare periodicamente la cantidad esperada de cada `Position` del
   portfolio en memoria contra la tenencia REAL que devuelve el broker, y
   trate cualquier diferencia hacia abajo en una pata corta como
   candidata a asignacion (nunca al reves - una diferencia hacia arriba
   es un bug de otro tipo, no una asignacion).
3. **Registro distinto en el Event Journal**: un asignacion NO es un
   cierre voluntario - mismo razonamiento que motivo toda la
   investigacion de la seccion 6bis (nunca mutar `Position.quantity` sin
   loguear el evento, y el TIPO de evento debe permitir distinguir
   despues "se cerro porque el bot decidio cerrarla" de "se cerro porque
   el mercado la ejercio en contra nuestra" - esto ultimo es evidencia
   sobre H2/el riesgo real de vender patas cortas, lo primero no). Se
   recomienda un nuevo `reason`/tipo de evento explicito (ej.
   `"ASSIGNED"`) en vez de reusar `"CLOSE"`.
4. **Alertable, no silencioso**: dado que una asignacion real cambia el
   perfil de riesgo del portfolio de forma inmediata (una pata corta
   asignada puede convertirse en una posicion del subyacente, no en
   cierre a cero), detectarla deberia disparar una alerta operativa (el
   mismo canal/mecanismo que ya exista para otras condiciones criticas
   del bot), no solo quedar como una fila mas del log.

Nada de esto esta implementado hoy - es la especificacion pedida por el
usuario para cuando el proyecto pase de shadow a vivo, no un cambio de
codigo de esta entrega.

**Resizing obligatorio a <=3% de perdida maxima semanal (regla nueva del
usuario), con los numeros REALES de (a):**

| | Sizing viejo (rechazado) | Sizing nuevo (<=3%) |
|---|---|---|
| Capital | 1.000.000 ARS | 1.000.000 ARS |
| Perdida maxima/spread (real, con haircut bid/ask) | 29.569 ARS | 29.569 ARS |
| n_spreads | 34 | **1** |
| Perdida maxima agregada | 981.750 ARS (**98,2%** del capital) | **29.569 ARS (2,96%** del capital) |

**Con esta estructura exacta (short 5700/long 5400, ancho=5% del spot),
el limite de 3% fuerza n=1 spread - 34 veces mas chico que el sizing
rechazado.** A esta escala, la posicion es apenas operable (un solo
spread, sin forma de diversificar entre vencimientos/strikes dentro del
mismo presupuesto de riesgo) - un sintoma adicional, no buscado, de que
el ancho/strike elegidos son demasiado agresivos para el nivel de vol
real de GGAL frente a un limite de riesgo serio. Una estructura mas
angosta (short mas cerca del dinero, ancho menor) licuaria algo de esto
pero tambien licuaria el credito - no se explora mas en esta correccion
porque el punto pedido (resizing a 3%) ya queda respondido con la
estructura pre-registrada.

**VEREDICTO FINAL (reemplaza "SOBREVIVE, CONDICIONADO"): "datos
acumulándose", NO "sobrevive".** La logica de legging (long-first,
seccion de arriba) sigue siendo correcta y se mantiene. Pero el gate de
sizing agregado fallaba (97% del capital, ahora corregido a 3%), el
credito se media con un modelo que asume cero prima de riesgo (ahora
reemplazado por bid/ask real + probabilidad empirica, que muestra una
brecha de 12-17x), la garantia BYMA es DATA INSUFFICIENT, y el riesgo de
asignacion anticipada no tiene ningun mecanismo de manejo. Por pedido
explicito del usuario: mientras no haya al menos varias semanas de IV
real acumulada (`market_snapshots.csv`, columna `iv`, poblada desde que
el bot corre - ver seccion 2bis sobre su cobertura real), H2 NO se
reclasifica como "sobrevive" bajo ninguna condicion, sin importar que
sizing se use.

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

## CORRECCION 2026-10-15 (correccion #1 del usuario, ronda 2) — test definitivo con instrumento correcto

El usuario senalo un error concreto en la seccion anterior: **el 2.74%
de spread real (a) se midio sobre `shadow_trades.csv`, que son fills de
CALLS ATM que el bot efectivamente opero (scalping) - no sobre los PUTS
OTM (~5%/~10% fuera del dinero) que H2 necesitaria para el bull put
spread.** Medir el spread del instrumento equivocado invalida la
conclusion de esa seccion sobre liquidez/credito. Esta seccion repite el
test con el instrumento correcto, usando datos reales nuevos disponibles
hoy (conector de mercado IOL, agregado a este sandbox despues de la
seccion anterior).

**Disponibilidad de datos real, verificada hoy (2026-10-15):**

- `logs/market_snapshots.csv` y sus rotaciones `.gz` en este sandbox:
  **DATA INSUFFICIENT** (confirmado de nuevo) - todos los archivos tienen
  unicamente la fila de encabezado, cero filas de datos. Este checkout no
  corrio nunca contra el book real de BYMA.
- Chain de opciones GGAL en vivo (conector IOL, `get_options_chain`):
  **VERIFIED - datos reales, hoy 2026-10-15 ~13:48 ART, mercado abierto.**
  Spot real BCBA: **5790 ARS**. Hay exactamente 3 vencimientos listados:
  (1) semanal 2026-10-16 (a **T-1 rueda**, vence maniana - NO representa
  el libro de una entrada semanal tipica a ~5 dias calendario/3 ruedas),
  (2) mensual 2026-11-20 (sin liquidez, casi todo bid/ask en null), (3)
  mensual 2026-12-18 (liquido, dos puntas reales en la mayoria de los
  strikes). **No existe todavia una serie semanal "siguiente" (ej.
  2026-10-23)** - normalmente abre recien despues de que vence la actual.
  Un intento de leer la chain completa sin fecha fue bloqueado una vez por
  el clasificador de permisos de la sesion ("Credential Exploration");
  una llamada mas acotada (con `expiration` especifico) si fue autorizada
  - no se insistio mas alla de eso.
- **Conclusion de disponibilidad**: no existe hoy, ni en logs historicos
  ni en vivo, un book semanal real a ~5 dias de vencimiento (el momento en
  que `weekly_asymmetric` entraria la posicion). El book semanal vivo de
  hoy esta a T-1 (demasiado cerca del vencimiento, premios casi nulos,
  spreads porcentuales inflados por el piso de tick minimo - no sirve
  como proxy de credito de entrada). **DATA INSUFFICIENT para el book
  semanal real multi-fecha que pidio el usuario.** Lo que si se puede
  medir con datos 100% reales, y se usa abajo: (i) la estructura real de
  spread % en PUTS OTM de GGAL en BYMA (del book mensual liquido,
  2026-12-18, mismo activo subyacente y mismo mercado, instrumento
  correcto - PUT, no CALL), y (ii) la distribucion empirica real de
  retornos a 5 ruedas del ADR a 10 anios (ya disponible, sin cambios).

**Spread real medido en PUTS OTM (no calls) - book BYMA vivo, vencimiento
2026-12-18, 2026-10-15:**

| Strike | Moneyness (spot 5790) | Bid real | Ask real | Mid | Spread % del mid |
|---|---|---|---|---|---|
| 5600 | ~3.3% OTM | 219.207 | 224.984 | 222.10 | **2.60%** |
| 5400 | ~6.7% OTM | 156.000 | 160.579 | 158.29 | **2.89%** |
| 5200 | ~10.2% OTM | 103.384 | 115.014 | 109.20 | **10.65%** |

Para comparacion honesta, el mismo strike en el book semanal real pero a
T-1 (hoy, vence maniana - NO usable como credito de entrada, solo ilustra
por que el book a T-1 no sirve): 5600 bid 12.405/ask 14.70 (16.9% del
mid), 5400 bid 1.90/ask 2.20 (14.6%), 5200 bid 0.40/ask 0.67 (50.5%) - los
premios son casi cero y el spread % se dispara por el tick minimo, exactamente
el patron esperado a horas del vencimiento, no un spread de entrada real.

**Limitacion explicita**: 2026-12-18 es un vencimiento mensual (2 meses),
no semanal. Es razonable esperar que una serie semanal real tenga MENOS
volumen/interes abierto que la mensual mas liquida del momento, por lo
que el spread % real de la serie semanal en una entrada tipica
probablemente sea IGUAL O PEOR (nunca mejor) que el medido aqui - este
numero es, si acaso, optimista para H2, no pesimista.

**Volatilidad real usada para escalar la distribucion empirica al
regimen actual:**

- Vol realizada 10 anios completos (serie diaria ADR, 2498 retornos
  log validos): **64.91% anualizada**. VERIFIED (calculo directo, sin
  modelo).
- Vol realizada ultimas 60 ruedas ADR: **34.85% anualizada**. VERIFIED.
- IV real de mercado HOY (promedio call/put ATM-ish, strikes 5600/5800/
  6000, book mensual liquido 2026-12-18): **~44%**. VERIFIED (dato de
  mercado, no supuesto).
- IV real de mercado HOY, book semanal a T-1 (strike 5800 ATM):
  **~68%** - se incluye como sensibilidad alta porque un book a T-1
  puede sobre-estimar IV por ruido de microestructura cerca del
  vencimiento; no se descarta, se marca como limite superior razonable.

**Test definitivo: costo esperado empirico (10y ADR, 5 ruedas, rolling)
vs. credito de mercado realista (BS al IV real de hoy, con el haircut de
spread real de puts OTM medido arriba), para dos pares de strikes
BYMA reales (incrementos de 200 ARS) que sandwichean el ~5%/~10% OTM
pedido:**

| Regimen de vol usado para escalar | Par (short/long) | Costo esperado empirico (ARS/contrato) | Credito realista (ARS/contrato) | Edge | Veredicto |
|---|---|---|---|---|---|
| Sin escalar (distribucion cruda 10y) | 5600/5200 (~3.3%/~10.2%) | 68.34 | n/a* | - | - |
| Sin escalar (distribucion cruda 10y) | 5400/5200 (~6.7%/~10.2%) | 21.72 | n/a* | - | - |
| Escalado a IV mensual real hoy (44%) | 5600/5200 | 38.38 | 35.22 | **-3.16** | **DESCARTA** |
| Escalado a IV mensual real hoy (44%) | 5400/5200 | 9.86 | 6.80 | **-3.06** | **DESCARTA** |
| Escalado a IV semanal T-1 (68%, sensib. alta) | 5600/5200 | 72.37 | 72.27 | **-0.09** | **DESCARTA** |
| Escalado a IV semanal T-1 (68%, sensib. alta) | 5400/5200 | 23.60 | 23.00 | **-0.60** | **DESCARTA** |
| Escalado a vol realizada reciente 60r (34.85%) | 5600/5200 | 24.74 | 20.24 | **-4.49** | **DESCARTA** |
| Escalado a vol realizada reciente 60r (34.85%) | 5400/5200 | 5.44 | 2.28 | **-3.16** | **DESCARTA** |

\* La fila "sin escalar" usa la cola de riesgo cruda de 10 anios (incluye
PASO 2019, devaluaciones, crashes) sin fijar un sigma de credito
consistente - se omite el credito ahi porque mezclar una distribucion de
cola de 10 anios con un credito de regimen "actual" no es comparable
apples-to-apples; igual se deja el costo esperado (68.34 y 21.72) para
contexto: es muy superior a cualquier credito realista en cualquier
regimen, asi que no cambia el veredicto, lo refuerza.

**Probabilidad empirica de que la pata corta termine ITM al vencimiento
(conteo directo sobre la serie real, no BS)**, para contexto frente al
"win rate de breakeven" de la seccion anterior:

| Regimen escalado | Short 5600 (~3.3% OTM) | Short 5400 (~6.7% OTM) |
|---|---|---|
| Sin escalar | 32.05% | 15.64% |
| IV mensual 44% | 24.07% | 7.78% |
| IV semanal T-1 68% | 33.05% | 16.97% |
| Vol realizada 60r 34.85% | 18.25% | 4.69% |

Confirma, con el instrumento correcto esta vez (puts OTM, no calls ATM),
el mismo patron que la seccion anterior ya habia encontrado con el
instrumento equivocado: la probabilidad empirica de ITM de la pata corta
(4.7%-33%, segun regimen) es sistematicamente mayor que lo que un win
rate de breakeven del 98.56% (BS, implicando ~1.44% de probabilidad de
perdida) asumiria. La magnitud exacta de la brecha cambia con el
regimen, pero el signo y la conclusion cualitativa no.

**VEREDICTO (reemplaza cualquier numero de spread de la seccion
anterior basado en calls ATM)**: en los 6 escenarios con credito
calculable (2 pares de strikes x 3 regimenes de vol, cubriendo desde vol
realizada reciente hasta el extremo superior de IV semanal observado
hoy), el edge (credito realista - costo esperado empirico) es **negativo
en los 6**. No hay un regimen de vol dentro del rango real observado
hoy en el que el credito de mercado realista supere la perdida esperada
empirica. Regla pre-acordada con el usuario ("si el credito de mercado
es menor que la perdida esperada empirica, H2 se descarta en este
regimen"): **H2 se descarta bajo el regimen de vol actual, con el
instrumento correcto (puts OTM) y spread real medido del book liquido
disponible hoy.** Se mantiene la clasificacion de la seccion anterior
("datos acumulandose", no "sobrevive") - este test la refuerza con el
instrumento corregido en vez de cambiarla.

**Lo que haria falta para cerrar esto sin ninguna limitacion**: capturar
en vivo, durante varias semanas consecutivas, el book real de
`weekly_asymmetric` en el momento exacto de entrada (lunes, ~5 dias
calendario al vencimiento) via `market_snapshots.csv` (que ya loguea
`option_type`, `bid`, `ask` por contrato - simplemente no tiene filas
todavia en este sandbox) o repitiendo esta misma consulta al chain en
vivo cada lunes. Esto es exactamente el tipo de dato que la Tarea 4 (ver
mas abajo) y el congelamiento de 3 semanas (Tarea 3) estan pensados para
acumular.

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

## Hipotesis 5 (scalping) — VEREDICTO 2026-10-27: APAGAR

**Decision del usuario, 2026-10-27, en base a evidencia real (no una
hipotesis mas a pre-registrar: scalping ya opero en shadow, hay historia
real de journal para evaluarlo).**

Corrida con `ggal_bot/ops/weekly_review.py` (escenario `CostAssumptions()`
default - gold, `spread_round_trip_pct=0.0` - el correcto para fills
post-1/10 que ya cruzan el spread real, ver advertencia agregada hoy en
`backtest/costs.py` junto a `spread_round_trip_pct`):

| Ventana | n trades | win% | PnL neto (comision+derechos+IVA) | IC95% expectancy/trade |
|---|---|---|---|---|
| Historico completo (desde 2026-09-01) | 502 | 14.5% | -815.906 ARS | [-1.973, -1.261] ARS/tr |
| Ultimos 5 dias habiles (20-26/10) | 52 | 3.8% | -86.497 ARS | [-1.940, -1.381] ARS/tr |

Los dos IC95% son enteramente negativos y consistentes entre si (la
ventana reciente no es una racha distinta de la historia completa).
Descomposicion adicional sobre los 52 trades recientes (PnL BRUTO, precio
puro, antes de cualquier costo): -32.960 ARS, 9 ganadoras/41
perdedoras/2 flat, ganancia promedio $727 vs. perdida promedio -$963 - el
edge de precio YA es plano-a-negativo antes de aplicar ningun costo.
Tenencia mediana 1,2 min (scalping real, no la etiqueta). Separado por
motivo de cierre, tanto el exit de señal (`scalping_iv_mean_reversion`,
48 trades, promedio bruto -$610) como el forzado de EOD (`scalping_eod_close`,
4 trades, promedio bruto -$922) son negativos - no es un exit forzado
arrastrando a uno bueno.

**Correccion metodologica (2026-10-27)**: un analisis previo, en el mismo
dia, aplico ADEMAS escenarios de `spread_round_trip_pct` (6%, 10%) sobre
este mismo PnL bruto para "estresar" el costo de cruzar el spread - eso
es un doble conteo (ver advertencia nueva en `costs.py`): los fills reales
desde el commit `55d2f67` (2026-10-01) YA cruzan el spread real (compran
al ask, venden al bid), asi que `pnl_gross_ars` ya lo paga. El numero
valido es el de la tabla arriba (comision+derechos+IVA sobre el fill
real, spread_round_trip_pct=0.0): **-86.497 ARS / 52 trades**. Los
numeros de 6%/10% reportados en el chat esa misma tarde quedan
RETRACTADOS.

**Descomposicion al mid (2026-10-27, resuelve el pendiente de arriba)**:
se bajo `shadow_trades.schema2.csv` (el sibling rotado del volumen de
Northflank, cubre 2026-10-05 a 2026-10-26, con `bid_at_fill`/
`ask_at_fill`/`mid_at_fill` reales por fill) y se joineo contra
`position_events.csv` por `order_client_id`/`client_order_id` - 213 de
213 trades de scalping con ambas patas en ese rango matchearon 1:1 (join
completo, sin huecos). Para cada trade: `pnl_mid = (mid_salida -
mid_entrada) * qty * mult` (el PnL que hubiera tenido llenando siempre al
mid, sin fricción de spread), `costo_spread = pnl_mid - pnl_fill` (lo que
cruzar el spread real le costo), `costo_regulatorio` = comision+derechos+
IVA de `costs.py` sobre el notional real. Identidad verificada:
`pnl_mid - costo_spread == pnl_fill` en los 213 casos (el join es
consistente).

| | n | PnL total | PnL promedio/trade | win% |
|---|---|---|---|---|
| Al FILL (real, ya reportado arriba) | 213 | -369.777 ARS | -1.736 | 17.8% |
| **Al MID (sin fricción de spread)** | 213 | **+129.278 ARS** | **+607** | 49.8% |
| Costo de spread pagado (mid→fill) | 213 | 499.055 ARS | 2.343 | - |
| Costo regulatorio (comision+derechos+IVA) | 213 | 260.163 ARS | 1.221 | - |
| Neto (fill - regulatorio, = fila de arriba de esta tabla) | 213 | -629.940 ARS | -2.957 | 8.0% |

**Respuesta a "¿la señal tiene edge al mid?": SI** - IC95% bootstrap
(5000 resamples) del PnL-al-mid promedio por trade: **[+59.8, +1.195.6]
ARS/trade**, enteramente positivo, no cruza cero. La señal de IV mean
reversion de scalping identifica movimientos reales en la dirección
correcta con mas frecuencia de la que explicaria el azar. El problema NO
es la señal: es que el costo de EJECUTARLA (cruzar el spread real +
comision/derechos/IVA, ~3.564 ARS/trade combinados en promedio) es 5-6x
mas grande que el edge que la señal genera (~607 ARS/trade al mid). Por
dia, el costo de spread fue mas alto durante la serie de Octubre cerca de
su vencimiento (hasta ~5.255 ARS/trade promedio el 16/10, el ultimo dia
de esa serie) y bajo notablemente con la serie de Diciembre mas liquida
desde el 21/10 (560-968 ARS/trade) - sigue siendo friccion grande, pero
mejora con liquidez.

**Conexion directa con Hipotesis 1 (provision de liquidez)**: esto
cuantifica EXACTAMENTE lo que H1 propone capturar en vez de pagar -
cotizar DENTRO del spread en vez de cruzarlo. Si una version de scalping
pudiera capturar (no solo evitar pagar) una fraccion de esos ~2.343
ARS/trade de costo de spread, combinado con el edge al mid ya positivo
(~607 ARS/trade), el caso economico cambia por completo. Esto no revierte
el veredicto APAGAR de hoy (la implementacion actual cruza el spread, no
lo captura), pero es el primer dato real que conecta scalping con el
pre-registro de H1 - a tener en cuenta cuando se testee esa hipotesis.

**Descartado explicitamente (2026-10-27)**: escalar el tamaño de
posicion para diluir un costo fijo por operacion. Verificado con el
usuario: IOL no tiene comision minima (todos los costos de
`backtest/costs.py` son proporcionales al notional) - escalar el tamaño
no cambia el resultado por trade en términos porcentuales, asi que no es
una via de mejora para esta estrategia.

**VEREDICTO: APAGAR.** `GGAL_BOT_ENABLE_SCALPING=false` (default del
propio flag, hoy en `true` en produccion - ver `ScalpingConfig.enabled`
en `config.py`). Al momento de esta decision el portfolio esta flat en
scalping (0 posiciones abiertas, ver verificacion de vencimientos de
octubre mas abajo), asi que apagar el flag no deja ninguna posicion sin
gestion de salida.
