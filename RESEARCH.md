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
| 1 | Provision de liquidez | book local (ya logueado) | **Datos acumulándose** (ver 2026-10-14 abajo; actualizado 2026-10-27: 64.948 transiciones reales de `last_price` en 9 dias habiles desde 15/10, suficiente para exploratorio en los contratos mas liquidos, corto todavia para el test completo con holdout) |
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

**Extension multi-dia (2026-10-27)**: ya hay 13 dias reales de
`market_snapshots.csv` (via `.csv.gz` rotados, bajados del volumen de
Northflank, 2026-10-09 a 10-26) con `bid`/`ask` reales. NO hay una serie
semanal real cotizando en este chain durante esta ventana (lo mas corto
disponible con liquidez es la serie de Octubre, agonizando hacia su
vencimiento, con spreads que se disparan por tick minimo los ultimos 1-3
dias - el mismo patron ya documentado arriba para ilustrar "por que un
book a T-1 no sirve", no el caso de una entrada real). La serie
MENSUAL 2026-12-18 (la misma usada en la medicion puntual del 15/10
arriba) es la que se mantuvo liquida y consistente los 13 dias:

| Moneyness objetivo | Mediana spread% | Media | Min | Max | N dias |
|---|---|---|---|---|---|
| ~5% OTM (strike ~5600, spot ~5600-6250) | 5.09% | 12.55% | 0.76% | 106.98% | 13 |
| ~10% OTM (strike ~5200-5600) | 5.40% | 8.51% | 0.26% | 45.02% | 13 |

Las medianas (5.09%/5.40%) son del mismo orden que la medicion puntual
del 15/10 (2.60%/2.89% para strikes equivalentes esa fecha especifica) -
mismo rango de magnitud, no una contradiccion. Dos dias (13/10 y 14/10)
muestran spreads disparados (45-107%) que tienen toda la pinta de un
book temporalmente roto/cruzado (mismo criterio de `is_valid_quote()`
que ya se usa en el resto del proyecto para descartar estos casos) antes
que un spread real de mercado - no se excluyen de la tabla (serian
descartados a mano sin un criterio automatico, y el pedido fue extender
la medicion con los dias reales que hay, no limpiarla primero) pero se
marcan explicitamente como sospechosos. **Esta extension REFUERZA el
veredicto de la seccion anterior (H2 DESCARTADA en el regimen actual) -
el spread real medido en mas dias sigue en el mismo rango usado para esa
conclusion, no lo cambia.**

**Credito REAL de mercado vs. costo esperado empirico (2026-10-27)**:
la seccion anterior comparaba un credito ESTIMADO con Black-Scholes (a
la IV real de un solo dia, 10/15) contra el costo esperado empirico. El
usuario pidio reemplazar eso por el credito REAL observado (book en
vivo, 13 dias, mismos `bid`/`ask`/`mid` ya usados arriba para el spread),
contra la MISMA familia de costo esperado empirico (10 años de retornos
log diarios del ADR, `ggal_adr_daily_10y.csv`, ventanas rolling de 42
ruedas ~60 dias calendario, igual al horizonte real de estos quotes -
mediana de dias_calendar observada: 60). Credito real (mediana de 13
dias, mid/strike):

| Moneyness | Mediana credito real (mid/strike) | Mediana dias_calendar |
|---|---|---|
| ~5% OTM | 3,924% | 60 |
| ~10% OTM | 2,266% | 60 |

Costo esperado empirico (mismos 13 dias/strikes reales, 3 regimenes de
vol, payout naked `max(0, strike - spot*exp(r_42dias))` promediado sobre
2.457 ventanas rolling historicas) y el edge resultante (credito real -
costo empirico):

| Regimen de vol | ~5% OTM: costo emp. (mediana) | ~5% OTM: edge | Dias con edge>0 | ~10% OTM: costo emp. (mediana) | ~10% OTM: edge | Dias con edge>0 |
|---|---|---|---|---|---|---|
| Sin escalar (cruda 10y, 64,93% anualizada) | 391,30 ARS | **-198,96 ARS** | 0/13 | 312,86 ARS | **-177,94 ARS** | 0/13 |
| IV mensual real hoy (44%) | 248,21 ARS | **-57,96 ARS** | 3/13 | 182,66 ARS | **-52,64 ARS** | 2/13 |
| Vol realizada reciente 60r (34,85%) | 181,64 ARS | +6,86 ARS | 9/13 | 124,20 ARS | +1,17 ARS | 8/13 |

**Lectura**: en 2 de los 3 regimenes de vol ya usados en la seccion
anterior (el crudo de 10 años y el de IV mensual real, los dos mas
conservadores/realistas para un horizonte de 60 dias) el edge con
credito REAL de mercado sigue siendo negativo, en el mismo orden de
magnitud que ya se habia encontrado con el credito BS-estimado de un
solo dia. Solo bajo el regimen MENOS conservador (vol realizada reciente
de 60 ruedas, que ignora el riesgo de cola/regimen y typicamente
subestima el riesgo a 2 meses) el edge se vuelve positivo, pero por un
margen minusculo (+6,86 y +1,17 ARS - estadisticamente ruido, no una
señal) y usando la estimacion de vol que menos captura el riesgo real
de esta ventana. **No cambia el veredicto: H2 sigue DESCARTADA, ahora con
credito real de mercado en vez de un estimado BS de un solo dia.**
Hereda las mismas limitaciones ya documentadas (ADR como proxy de la
local, ver seccion anterior) - no son nuevas de este agregado.

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

**Acumulacion de datos (2026-10-27)**: el campo `last_price` existe en
`market_snapshots.csv` desde el 2026-10-15 (confirmado por header: los
archivos rotados del 9 al 14/10 tienen 17 columnas, SIN `last_price`/
`last_volume`; desde el 15/10 tienen 19). Hay 13 dias calendario (9
dias habiles) de ese campo poblado, 118 simbolos distintos, ~19M filas
con el campo presente.

**Correccion de metodo importante**: contar filas con `last_price>0`
NO mide trades reales - el campo queda "pegado" al ultimo valor
conocido entre ticks (verificado: una opcion con 17.958 filas en un
solo dia tiene solo 214 valores DISTINTOS de `last_price` ese dia - el
78.74% de filas con `last_price>0` en toda la ventana es, en su enorme
mayoria, el mismo numero repetido mientras no hay trade nuevo, no 78.74%
de probabilidad de trade por poll). La metrica correcta para "cuantos
trades reales hay para validar un modelo de fill conservador" es el
CONTEO DE CAMBIOS de `last_price` entre filas consecutivas de un mismo
simbolo (cota inferior de trades reales - dos trades consecutivos al
mismo precio cuentan como 1 cambio, no 2).

Con esa metrica correcta: **64.948 transiciones de precio reales en
total**, 118 simbolos, sobre los 9 dias habiles disponibles. Los 15
contratos mas liquidos (todos de la serie `DI`/Diciembre) acumulan entre
1.716 y 3.725 transiciones cada uno (datos en al menos 8 de los 9 dias
habiles) - una densidad razonable para un PRIMER analisis exploratorio
de esos contratos puntuales. 23 simbolos (de 118) tienen CERO
transiciones en toda la ventana - estrikes sin ningun trade real, no
testeables todavia.

**Respuesta a "¿alcanza para un exploratorio sin tocar el holdout?"**:
para los contratos mas liquidos, si, alcanza para mirar el patron sin
tocar el holdout (no hace falta particionar estos 9 dias habiles - son
muchos menos que el dataset completo de quotes, que arranca en
septiembre; el holdout pre-registrado de H1 se define sobre ESE dataset
mas largo, no sobre esta sub-ventana de last_price). Para el test
PRE-REGISTRADO completo (con holdout propio, grilla de 3 variantes de
factor_captura x limite de inventario, bootstrap CI) la ventana de solo
9 dias habiles con datos de trades reales sigue siendo corta - se
mantiene la clasificacion "Datos acumulandose" de la tabla de arriba,
ahora con un numero concreto en vez de cero.

(Hipotesis 2 y 5 se pre-registran de la misma forma antes de correrlas -
secciones a agregar aca mismo cuando empiecen.)

## Pre-registro — Hipotesis 1b: señal de scalping + ejecucion pasiva

**Pre-registrado 2026-10-27, ANTES de correr el modelo de fill contra
`market_snapshots.csv`.** Pedido explicito del usuario: la señal de
scalping ya mostro edge real al mid (ver "Descomposicion al mid" en la
seccion de Hipotesis 5 arriba, +607 ARS/trade, IC95% positivo), pero el
costo de EJECUTARLA (cruzar el spread + comision/derechos/IVA) se lo
come. H1b pregunta: ¿sobrevive la señal si se ejecuta pasivamente (orden
limite, no cruce) en vez de apagarla del todo?

**Mecanismo (pre-registrado)**:
- **Entrada**: cuando la señal dispara, se coloca una orden limite AL
  MID vigente en ese momento (no se cruza el spread). Si no se llena
  dentro de la ventana de timeout, la señal se descarta y **no se
  entra** - sin costo de oportunidad mas alla de no tomar ese trade
  (instruccion literal del usuario: "si no llena, no se entra").
- **Salida**: se extiende la MISMA regla a la salida (limite al mid,
  mismo timeout) - si no llena, se cruza el spread real para garantizar
  el cierre, en vez de dejar una posicion de scalping abierta
  indefinidamente. **Esto es una extension del pedido del usuario** (que
  hablo solo de la entrada) a la salida, necesaria para poder medir el
  resultado economico completo de un round-trip - se deja explicito como
  decision de modelado, no como parte literal del pedido original.
- **Timeout**: N=30 segundos como caso primario (decidido ahora, antes
  de ver ningun resultado). Se reporta tambien N=10 y N=60 como analisis
  de sensibilidad. Se eligio 30s por ser del orden de 5-8 polls del
  propio sistema de snapshots (ver intervalo real medido abajo), ni tan
  corto que casi nunca llene ni tan largo que sea irrealista para una
  señal de scalping (tenencia mediana real de estos trades: 1,2 min).
- **Modelo de fill CONSERVADOR** (exactamente como pidio el usuario): una
  orden se considera llena UNICAMENTE si un `last_price` POSTERIOR (un
  print real de trade, no solo el bid/ask tocando el limite) atraviesa el
  precio limite dentro de la ventana de N segundos. Precio de fill = el
  limite mismo (mid), nunca mejor que el limite (no se asume mejora de
  precio por gap).
- **Universo de prueba**: los 213 trades de scalping con bid/ask/mid real
  registrado (post-fix `55d2f67`, 2026-10-05 a 2026-10-26) - mismo
  dataset ya usado en la descomposicion al mid.

**Metricas a reportar (pre-registradas)**: tasa de llenado de la entrada,
de la salida, y de ambas patas juntas; PnL-al-mid de los trades llenados
vs. no llenados (test de seleccion adversa: ¿los que llenan pasivamente
tienen sistematicamente peor edge subsecuente que los que no llenan?);
neto por escala de comision IOL (gold/platinum/black) sobre la submuestra
de trades donde la entrada llena pasivamente (la salida sigue la regla de
fallback de arriba).

**Criterio de exito (definido AHORA, antes de ver resultados)**: H1b se
considera soporte para una futura implementacion (sujeta a aprobacion
del usuario - el freeze de 3 semanas sigue vigente, nada de esto se
implementa en el bot todavia) si, en la escala **Black**: neto promedio
por trade > 0 con IC95% bootstrap (mismo `bootstrap_ci` de
`backtest/metrics.py`, 10.000 resamples, semilla 1234) enteramente > 0, Y
al menos **30 trades** con la entrada llenada pasivamente en la muestra.
Si no se cumple en Black (la escala con menor costo regulatorio), se
considera NO soportada automaticamente en Gold/Platinum.

**Limitacion de datos conocida de antemano (declarada antes de correr
nada)**: `market_snapshots.csv` es polling periodico (intervalo real
medido ~3-6 segundos para un simbolo liquido el 2026-10-22), no un feed
de trades tick-by-tick - el modelo conservador de fill solo puede
detectar un cruce si el polling capturo una fila con `last_price`
distinto dentro de la ventana de N segundos, lo cual pone un piso de
sensibilidad a esta medicion que no se puede mejorar con los datos
disponibles. Ademas: no hay archivo de snapshots para 2026-10-05,
10-06 ni 10-07 (3 de las 13 fechas con trades reales) - los trades de
esos dias quedan como DATA INSUFFICIENT para este test especifico (se
reportan aparte, no se descartan silenciosamente). El archivo del
2026-10-08 es un outlier de tamaño (21,5M filas vs. ~1-2M filas/dia
tipico, ya flagueado en la seccion de Hipotesis 1 sin investigar a
fondo) - se incluye en este analisis pero sus resultados se reportan
por separado para poder detectar si el outlier distorsiona algo.

**Limitacion ADICIONAL descubierta DESPUES de pre-registrar lo de arriba
(hecho de disponibilidad de datos, no un resultado del test - se declara
antes de calcular ninguna tasa de llenado ni PnL)**: el campo
`last_price` (necesario para el modelo conservador de fill) solo existe
en `market_snapshots.csv` desde el 2026-10-15 (schema de 17 a 19
columnas, ya documentado en la seccion de Hipotesis 1 arriba) - los
archivos del 10-08 al 10-14 NO tienen esa columna. De los 213 trades del
universo de prueba, solo los de fechas 10-15 en adelante son testeables:
**67 de 213** (10-15: 12, 10-16: 3, 10-21: 3, 10-22: 10, 10-23: 23,
10-26: 16). Los 146 trades restantes (10-05 a 10-14) quedan como DATA
INSUFFICIENT para este test especifico - se excluyen, no se fabrica un
sustituto. 67 trades sigue siendo mas que el minimo de 30 pre-registrado,
asi que el test es viable con la muestra reducida.

**Resultados (2026-10-27, DESPUES del pre-registro de arriba)**:

| Timeout N | Tasa llenado entrada | Tasa llenado AMBAS patas | PnL-al-mid: LLENADOS | PnL-al-mid: NO LLENADOS |
|---|---|---|---|---|
| 10s | 28/67 (41,8%) | 13/67 | -179,41 ARS/tr  IC95%[-974,2, 484,0] | +1.056,10 ARS/tr  IC95%[-238,2, 2.643,3] |
| 30s | 33/67 (49,3%) | 21/67 | -139,03 ARS/tr  IC95%[-821,1, 432,8] | +1.198,61 ARS/tr  IC95%[-262,2, 2.972,9] |
| 60s | 42/67 (62,7%) | 32/67 | -100,28 ARS/tr  IC95%[-988,5, 864,0] | **+1.615,05 ARS/tr  IC95%[264,7, 3.673,1]** |

**Seleccion adversa: CONFIRMADA, y en la direccion mas desfavorable
posible.** En los 3 timeouts, los trades que SI llenan pasivamente
tienen PnL-al-mid promedio NEGATIVO, y los que NO llenan tienen PnL-al-mid
promedio fuertemente POSITIVO (para N=60, con IC95% enteramente positivo
- estadisticamente significativo). Interpretacion: el precio solo vuelve
a tocar el limite propio (y por lo tanto solo se llena la orden pasiva)
cuando el movimiento es DESFAVORABLE - las mejores oportunidades (el
movimiento favorable grande que la señal detecta) nunca retroceden lo
suficiente para llenar al mid, se escapan. Es el patron clasico de
seleccion adversa contra un market maker/liquidity taker pasivo sin
logica de cancelacion/reposicionamiento.

**Neto por escala IOL, sobre los trades con AMBAS patas llenadas
pasivamente (spread_cost=0 en ambas patas, solo costo regulatorio)**:

| Timeout N | n | Gold | Platinum | Black |
|---|---|---|---|---|
| 10s | 13 | -1.775,74  IC95%[-3.328,9,-601,3] | -1.468,66  IC95%[-3.010,5,-291,1] | -1.161,58  IC95%[-2.692,1, 15,0] |
| 30s | 21 | -1.535,61  IC95%[-2.502,6,-804,5] | -1.226,52  IC95%[-2.188,0,-499,2] | -917,43  IC95%[-1.875,4,-193,1] |
| 60s | 32 | -1.384,06  IC95%[-2.493,2,-194,7] | -1.068,42  IC95%[-2.177,1, 121,3] | -752,78  IC95%[-1.857,6, 436,5] |

**Respuesta al criterio de exito pre-registrado: H1b NO SOPORTADA en
ninguna combinacion de timeout x escala.** Ninguna celda tiene IC95%
bootstrap enteramente positivo en Black (el requisito minimo
pre-registrado) - la mas cercana (N=60, Black, n=32>=30) sigue negativa
en el punto central (-752,78) y su IC95% todavia cruza cero. La seleccion
adversa (ver arriba) explica por que: la submuestra de trades que llenan
pasivamente es, por construccion, la peor parte de la distribucion de
PnL-al-mid, no una muestra representativa del edge de +607 ARS/trade
medido sobre el universo completo.

**VEREDICTO H1b: NO IMPLEMENTAR tal como esta pre-registrada.** Una orden
limite simple al mid con timeout fijo no captura el edge de la señal -
lo pierde por seleccion adversa antes de llegar a ahorrar el costo de
spread. No descarta la idea de ejecucion pasiva en general (podria
funcionar con logica de cancelacion dinamica, reposicionamiento, o
condicionando el fill a una señal de momentum a favor en vez de solo
tiempo) pero esa es una hipotesis DISTINTA, no probada aca, y el freeze
de 3 semanas sigue vigente para cualquier implementacion real.

**Correccion/ampliacion 2026-10-27 (pedido del usuario): el modelo de
fill conservador sesga hacia seleccion adversa POR CONSTRUCCION, no solo
por el resultado empirico de arriba.** Mecanismo (no es una medicion, es
la logica misma de la señal + el mecanismo de orden):

- La señal de scalping es de **reversion a la media de IV** - dispara
  una compra cuando el precio esta "barato" relativo a su media reciente,
  apostando a que el precio SUBA (revierta hacia arriba) despues de
  entrar.
- Una orden pasiva de COMPRA al mid solo se llena si el precio BAJA hasta
  tocar (o cruzar) ese mid - es decir, exactamente lo contrario del
  movimiento que la señal esta apostando que va a pasar.
- Si la señal tiene razon (el precio sube despues de la entrada), la
  orden pasiva de compra NUNCA se llena (el precio se aleja hacia arriba,
  nunca vuelve a tocar el mid de entrada) - la oportunidad se escapa
  intacta, sin registrar ni ganancia ni perdida.
- Si la señal esta equivocada (el precio sigue bajando en vez de
  revertir), la orden pasiva SI se llena - y precisamente ese es el
  subconjunto de casos donde el trade iba a salir mal.
- Conclusion: para una señal DIRECCIONAL (no neutral de inventario, como
  si seria un market maker puro), pedir ejecucion pasiva en la MISMA
  direccion de la apuesta es estructuralmente auto-derrotista - el
  mecanismo de fill filtra sistematicamente a favor de los casos donde la
  señal fallo. Esto no es un artefacto de la muestra chica (67 trades) ni
  de la granularidad del polling (ver abajo) - es una propiedad logica
  del diseño "señal direccional + orden pasiva en la direccion de la
  apuesta + fill solo si el precio viene en contra". Confirma por que el
  resultado empirico de arriba (filled=negativo, no-filled=positivo) no
  es casualidad de esta muestra: es lo que el mecanismo predice que tiene
  que pasar.

**Granularidad real del polling de `last_price` (medido, no supuesto)**:
sobre GFGC6400DI el 2026-10-22 (contrato liquido, primeros 20 timestamps
del dia), intervalos entre filas consecutivas: mediana **~3,9 segundos**,
media **~4,5 segundos**, minimo 3,28s, maximo 8,52s (n=19 intervalos, una
muestra puntual de un solo contrato en un solo dia - no se valido que
sea representativa de contratos menos liquidos ni de otros dias). Con
N=30s de timeout eso da ~6-9 polls dentro de la ventana - suficiente
resolucion para que el polling en si mismo no sea el cuello de botella
principal (el sesgo de seleccion adversa de arriba domina, no la
granularidad). Para N=10s (~2-3 polls) la resolucion es mas pobre y el
resultado de esa fila especifica (n=13, el mas chico de los tres) es el
menos confiable de los tres timeouts por este motivo, ademas del tamaño
de muestra.

**La medicion definitiva solo puede salir de ordenes reales (no shadow)
- queda como decision del usuario**, consistente con que cualquier
cambio de mecanismo de ejecucion sigue bloqueado por el freeze y
requiere su aprobacion explicita antes de implementarse.

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

**Cuenta completa del usuario (2026-10-27): ejecucion pasiva sola no
alcanza.** El usuario hizo la cuenta: +607 (edge al mid) - 1.221
(regulatorio Gold) = -614 ARS/trade, aun con fills PERFECTOS al mid (sin
pagar nada de spread). Se recalculo exacto (`bootstrap_ci` oficial de
`backtest/metrics.py`, 10.000 resamples, semilla 1234, sobre los mismos
213 trades) por escala de comision IOL (Gold 0.50% / Platinum 0.30% /
Black 0.10%, `BROKER_COMMISSION_TIERS_PCT`) x 3 escenarios de ejecucion,
con el derecho de mercado RE-VERIFICADO (ver `costs.py`, sigue en 0.20%,
sin cambio):

| Escala IOL | (a) Cruzando spread (actual) | (b) Al mid (spread=0) | (c) Capturando 25% del spread a favor* |
|---|---|---|---|
| Gold (0.50%) | -2.961,52 ARS/tr  IC95%[-3.520,6, -2.376,8] | -618,54 ARS/tr  IC95%[-1.148,1, -34,5] | -32,80 ARS/tr  IC95%[-570,4, 563,3] |
| Platinum (0.30%) | -2.611,39 ARS/tr  IC95%[-3.169,5, -2.025,6] | -268,41 ARS/tr  IC95%[-799,8, 318,4] | +317,34 ARS/tr  IC95%[-221,8, 914,3] |
| Black (0.10%) | -2.261,25 ARS/tr  IC95%[-2.818,5, -1.676,2] | +81,73 ARS/tr  IC95%[-450,9, 670,3] | **+667,48 ARS/tr  IC95%[126,7, 1.266,9]** |

\* "Capturando 25% del spread a favor" = precio de ejecucion mejora un
25% del costo de spread que cruzar hubiera significado (no es al mid
puro ni cruce completo - un punto intermedio, modelado como
`pnl_mid + 0.25*spread_cost_medido`, nunca validado contra un mecanismo
real de ejecucion, ver Hipotesis 1b abajo para eso).

**Confirma exactamente lo que dijo el usuario**: ni "al mid" solo (columna
b) alcanza en Gold o Platinum (sigue negativo, aunque Platinum ya no es
significativo - el IC95% cruza cero). La UNICA celda con IC95%
enteramente positivo es Black + capturando 25% del spread - necesita
AMBAS cosas (la escala de comision mas barata Y una mejora real de
ejecucion, no solo pasividad), no una sola. Esto motiva directamente el
pre-registro de Hipotesis 1b (ejecucion pasiva real, no un supuesto de
"25% a favor") que sigue abajo.

**Descartado por el propio usuario, verificado**: escalar el tamaño de
posicion para diluir el costo regulatorio (ya cubierto arriba, IOL sin
comision minima - todo proporcional, esta tabla ya lo refleja: ningun
numero cambia en % si se escala la cantidad de contratos).

## RESUELTO (2026-10-27): "22 posiciones / delta $18M / kill switch
disparado" del 21/10 — fuente identificada, bug real encontrado en
weekly_review.py, corregido con test, verificado contra los datos reales

**Fuente de los dos numeros, segun el propio usuario**: el delta de
"$18M" salio de sumar las filas `REJECT` del journal (kill switch,
hasta las 14:01 UTC) - no es un delta de cartera en absoluto, es una
suma de notional de ORDENES RECHAZADAS, nunca ejecutadas. Las "22
abiertas" salieron de correr `weekly_review.py` ese dia. El usuario
concluyo correctamente, antes de que se investigara nada: *"si la
reconstruccion de hoy da 1 posicion, weekly_review.py cuenta mal las
abiertas"* - y pidio encontrar el bug, corregirlo con test, y verificar
que las dos vias (el reporte de `weekly_review.py` y la reconciliacion
real) dieran el mismo numero para el 21/10 a las 19:34 UTC.

**Bug real encontrado (dos sub-bugs distintos, ambos en el camino que
usa `weekly_review.py`, NUNCA en el camino real de produccion que usa
`run_bot.py` al arrancar)**:

1. **Filas `REJECT` con `position_id` corrompido a la cadena literal
   `"nan"`** (contaminacion de un round-trip previo por pandas en el
   export, no del journal original): `reconstruct_lifecycle_trades`
   (`ggal_bot/backtest/reconstruct.py`) agrupa por `position_id` CRUDO
   via `pid = str(r.get("position_id") or "")`; como `"nan"` es una
   cadena no vacia, el filtro `if not pid: continue` (pensado para
   descartar exactamente las filas REJECT/CANCEL, que nunca tienen
   `position_id`) no las excluye. El grupo resultante no tiene ningun
   evento `CLOSE`/`EXPIRY` (son todas REJECT) -> se cuenta como
   "todavia abierta" sin haber tenido NUNCA un `ENTRY`. Corregido en la
   raiz, en el unico loader pandas compartido
   (`dashboard/pnl_engine.py::load_position_events`): un campo vacio
   leido por pandas como `float('nan')` ahora se normaliza a `""` con
   `.fillna("")` antes de que cualquier consumidor lo vea.
2. **`position_id` "reinventado" en cada restart del bot ("BUG B",
   ya documentado en `reconciliation.py` para OTRO consumidor pero
   nunca corregido en `reconstruct_lifecycle_trades`)**: una posicion
   abierta con un `ENTRY` bajo un `position_id` legacy, cerrada MAS
   TARDE con un `CLOSE` bajo un `position_id` distinto (el que el bot
   le asigno tras un restart posterior), queda invisible para el
   agrupamiento por `position_id` crudo: el grupo del id viejo nunca ve
   su propio `CLOSE` (cuenta como "todavia abierta") y el grupo del id
   nuevo nunca ve su propio `ENTRY` (cuenta como "datos incompletos",
   pero en ningun caso como "cerrada"). La funcion ya existente
   `reconstruct_positions_from_event_journal` (usada por
   `run_bot.py` en produccion) no tiene este problema porque agrupa por
   `(symbol, strategy_tag)` -no por `position_id`- y hace FIFO
   cronologico sobre TODOS los eventos del grupo sin importar que el
   `position_id` haya cambiado a mitad de camino.

**Fix aplicado (3 archivos, sin tocar ninguna logica de trading, solo
herramienta de reporte/medicion - permitido durante el freeze, ver
FREEZE.md: bug de integridad de datos en una herramienta de
reporte/reconciliacion)**:

- `dashboard/pnl_engine.py::load_position_events`: normaliza
  `position_id` vacio/`NaN` a `""` en el loader compartido.
- `ggal_bot/portfolio/reconciliation.py::reconstruct_positions_from_event_journal`:
  nuevo parametro opcional `rows=` para reconciliar directo sobre filas
  ya cargadas en memoria (sin un segundo read de disco potencialmente
  inconsistente).
- `ggal_bot/ops/weekly_review.py::section_trades_pnl_by_strategy`: la
  columna `"abiertos"` ahora muestra el conteo REAL (via
  `reconstruct_positions_from_event_journal(rows=journal_rows)`,
  agrupado por `strategy_tag`) en vez del conteo crudo de
  `reconstruct_lifecycle_trades`. El conteo crudo (`n_still_open_excluded`)
  sigue pasandose sin cambios a `build_strategy_report` (preserva el
  invariante interno trades+still_open+incomplete==total position_id
  que otros calculos dependen de el) - solo cambio lo que se IMPRIME.

**Test de regresion**: `ggal_bot/validation/test_weekly_review.py` -
`test_real_open_position_counts_ignores_reject_phantom_and_merges_reinvented_id`
y `test_section_trades_pnl_by_strategy_shows_real_open_count_not_raw_inflated_count`.
Reproducen los dos sub-bugs con un journal sintetico (REJECT con
`position_id="nan"`, ENTRY bajo un id "legacy" cerrado bajo un id
distinto, mas un control positivo de una posicion REALMENTE abierta
que no debe desaparecer) y verifican que el conteo crudo sigue dando 3
"todavia abiertas" (el bug, intacto, sigue existiendo internamente)
mientras que lo que el reporte IMPRIME ahora es 1 (el numero correcto -
solo la posicion de control). Suite completa corrida:
`pytest ggal_bot/` -> 864 passed, 1 failed (una falla PREEXISTENTE,
confirmada reproducible identica en el commit anterior a este fix, en
`test_execution_pipeline.py::test_bootstrap_universe_classifies_by_underlying_field_without_symbol_prefix`
- no relacionada con este cambio, no tocada).

**Verificacion contra los datos REALES del 21/10 a las 19:34 UTC (el
instante que el usuario pidio explicitamente)**, corriendo las dos
vias sobre el MISMO journal real (`position_events.csv` del export
`ggal_bot_export_2026-10-21_1633`, 2212 filas):

| Via | Resultado |
|---|---|
| `reconstruct_lifecycle_trades` (conteo CRUDO, el que mostraba el bug) | **23** "todavia abiertas" (+ 15 "datos incompletos") |
| `reconstruct_positions_from_event_journal` (reconciliacion REAL, la que usa `run_bot.py`) | **1** posicion abierta: GFGC6400DI, scalping, qty=2.0 |
| `weekly_review.py::section_trades_pnl_by_strategy` **DESPUES del fix** (columna "abiertos" impresa) | scalping=**1**, weekly_asymmetric=**0** |

Las dos vias ahora dan EXACTAMENTE el mismo numero (1), consistente con
`bot_state.json` de ese mismo snapshot (`portfolio_greeks_total.delta`
= 101.54 en unidades de delta de opcion, NO en ARS; `risk_breaches` =
"Griegas dentro de limite") - una sola posicion chica, nada parecido a
22 posiciones ni a una magnitud de "$18M". El "23 todavia abiertas"
crudo esta ademas muy cerca del "22" que reporto originalmente
`weekly_review.py` ese dia (la diferencia de 1 es compatible con el
crecimiento normal del journal entre el momento exacto en que se corrio
el reporte original y el snapshot de 19:34 usado aca) - es decir, el
"22" del usuario SI es trazable: es el numero crudo y buggy, no un
numero inventado ni de otra fuente.

**El "$18M" sigue sin ser un delta de cartera real** (ni del shadow ni
de ninguna cuenta real de IOL - el bot nunca coloca ordenes reales).
**CORRECCION 2026-10-27 (version mas precisa que la de mas abajo)**:
re-leyendo el campo `reason` real de las filas REJECT (antes solo se
habia inferido, sin citarlo textualmente) - NO es una suma de notional
de varias ordenes rechazadas. Es el texto LITERAL que el propio kill
switch escribio la ULTIMA vez que se disparo (2026-10-16T17:55:06 UTC,
ver subseccion siguiente), y que `run_bot.py::_act_on_entry_signal`
despues copia tal cual (`state_ks.reason`, nunca recalculado) en cada
REJECT subsiguiente mientras el switch sigue disparado:

> `kill_switch_tripped: Exposicion direccional agregada del portfolio
> (delta=3,115.95 acciones equiv. x spot=$5,797.50 = $18,064,705.27)
> supera el limite configurado ($12,000,000.00).`

Es decir: SI fue un delta de cartera real en algun momento (el momento
en que `evaluate()` lo calculo y disparo el switch, 2026-10-16 17:55
UTC) - pero es una foto CONGELADA de ESE instante, no el estado actual
del portfolio en cada rechazo posterior. Los otros 173 REJECT con esta
misma reason, repartidos entre el 2026-10-16 17:55 UTC y el 2026-10-21
18:46 UTC, todos citan el MISMO numero textual - no es que la
exposicion real se haya quedado quieta 5 dias, es que nadie volvio a
calcularla hasta el proximo reset. Con esto, la contradiccion original
(22 posiciones/$18M el 21/10 vs. 0-1 posiciones hoy) queda completamente
explicada: el "22" media con una herramienta que tenia un bug (ya
corregido, ver arriba) y el "$18M" es el texto congelado de un trip de
hace 5 dias, repetido en cada rechazo, nunca una medicion en vivo de la
cartera a las 21/10.

### Investigacion adicional (2026-10-27): ¿quien/que reseteo el kill
switch a las 19:32 UTC del 21/10? - mandato explicito del usuario

**Verificado en el codigo (busqueda exhaustiva, sin ningun llamado
automatico encontrado)**: `KillSwitch.reset()` (`ggal_bot/risk/
kill_switch.py`) es, por diseño explicito (ver docstring del modulo,
linea 31: *"RESET: deliberadamente manual (no hay auto-recuperacion por
tiempo ni por ningun criterio automatico)"*), la UNICA forma de volver
a `tripped=False`. Se grepeo TODO el codigo de produccion (`grep -rn
"\.reset(" --include="*.py"` sobre todo el repo) y los UNICOS llamados
a `KillSwitch().reset(...)` que existen son: (a) el propio CLI de este
modulo (`python -m ggal_bot.risk.kill_switch --reset "motivo"`,
linea ~231), y (b) los fixtures `autouse` de la suite de tests
(`ggal_bot/validation/conftest.py`), que operan sobre un
`KILL_SWITCH_STATE_FILE` de test, nunca el de produccion. `dashboard/
app.py` SOLO lee el estado (`KillSwitch().status()`) para mostrarlo -
no tiene ningun boton ni endpoint que llame a `.reset()`; el texto que
el propio dashboard le muestra al operador le dice que lo resetee a
mano con ese mismo comando de CLI. `run_bot.py` llama a `evaluate()`
(que SOLO puede *disparar*, nunca resetear - linea 1220: `if not
self.kill_switch.is_tripped(): self.kill_switch.evaluate(...)`) pero
JAMAS llama a `.reset()` en ningun punto.

**Verificado en los datos reales**: entre el primer trip (2026-09-07
17:44 UTC, por integridad de portfolio) y el ultimo REJECT del 21/10
(18:46 UTC, por exposicion), el journal muestra TRES reasons distintas
de `kill_switch_tripped` (ver tabla), cada una con su propio rango de
fechas - y como `evaluate()` solo corre cuando el switch NO esta
disparado (ver arriba), la UNICA forma de que aparezca una reason
nueva es que algo lo haya reseteado entre una y la siguiente. Esto
quiere decir que el reset de las 19:32 UTC del 21/10 (el que pregunta
el usuario) **no fue el primero** - hubo, como minimo, otros DOS
resets manuales antes, uno entre el 07/09 y el 02/10, y otro entre el
05/10 y el 16/10:

| Reason (resumida) | Primera vez vista | Ultima vez vista |
|---|---|---|
| Integridad de portfolio (5 bases con Position duplicada) | 2026-09-07 17:44 UTC | 2026-09-07 18:43 UTC |
| Exposicion delta=-5.300 x spot=$5.915 = $31.349.500 (limite $12M) | 2026-10-02 13:33 UTC | 2026-10-05 18:57 UTC |
| Exposicion delta=3.115,95 x spot=$5.797,50 = **$18.064.705** (limite $12M) | 2026-10-16 17:55 UTC | 2026-10-21 18:46 UTC |

`kill_switch.json` en el volumen real de Northflank tiene mtime
`2026-10-21 19:32 UTC` (confirmado via `ls -la /app/logs` sobre el
contenedor real), y la siguiente fila del journal despues de ese
instante es un `ENTRY` normal (GFGC6400DI, scalping, razon de señal
IV, no relacionada al kill switch) a las **19:32:48 UTC** - 48 segundos
despues, consistente con: reset -> el siguiente ciclo de
`_act_on_entry_signal` encuentra `is_tripped()==False` -> la señal ya
pendiente entra sin flag.

**VEREDICTO (SUPPORTED por el codigo, no se puede cerrar 100% sin el
dato que falta)**: no existe NINGUN camino automatico en el codigo que
pueda haber reseteado el kill switch - tiene que haber sido una accion
humana deliberada (CLI o una llamada directa a `.reset()` desde una
shell contra el contenedor real de Northflank). Esta sesion no tiene
acceso al log de auditoria/historial de shell de Northflank, asi que
no se puede confirmar DESDE ACA quien especificamente lo corrio - eso
queda pendiente de que el usuario lo confirme (si fue el mismo) o lo
investigue del lado de Northflank (si no lo fue). Dato adicional para
esa confirmacion: dado que hubo al menos 2 resets previos
(07/09->02/10 y 05/10->16/10) de los que no habia registro en este
documento hasta ahora, vale la pena confirmar los TRES, no solo el del
21/10.

---

### Investigacion original (2026-10-27, antes de que el usuario revelara
la fuente de los numeros) - se conserva como registro de lo que se pudo
trazar solo con los datos, sin la aclaracion del usuario

El usuario pidio reconciliar un reporte previo (21/10: 22 posiciones
abiertas, delta de cartera ~$18M, kill switch disparado) contra el
estado de hoy (0 posiciones, delta $0, sin eventos `EXPIRY`). Se
investigo con TODO el dato real disponible de ese dia:

- `bot_state.json` de dos exports reales del 21/10 (14:24 UTC y 19:34
  UTC): delta de cartera = **0.0** (14:24) y **101.54** (19:34) - en
  unidades de delta de opcion (griega), NO en ARS, y en ningun caso
  cercano a 22 ni a una magnitud de "$18M". `risk_breaches` dice
  "Griegas dentro de limite" en AMBOS snapshots (no un breach activo en
  ese instante). `open_orders`=0 y `active_signals`=[] en los dos.
- `reconstruct_positions_from_event_journal()` (la funcion real del
  proyecto) corrida contra el journal real tal como estaba a las 19:34
  UTC del 21/10 devuelve **1 sola posicion abierta** (GFGC6400DI,
  scalping, qty=2) - no 22.
- `kill_switch.json` en el volumen real de Northflank tiene mtime
  `2026-10-21 19:32 UTC` - ~2 minutos antes del snapshot de 19:34 y
  coincide con la entrada real de GFGC6400DI (19:32:48 UTC, ya
  investigado en una sesion anterior) - consistente con que el kill
  switch SI se disparo en algun momento ANTES de las 19:32 ese dia (no
  capturado en ninguno de los dos snapshots que se tienen, que son de
  14:24 y 19:34) y se reseteo manualmente justo antes de esa entrada -
  pero no hay un numero de "22 posiciones"/"$18M" en ningun dato real
  revisado para ese dia.

**No se fabrica una explicacion**: no se encontro soporte para "22
posiciones"/"$18M" en bot_state.json, en el journal reconstruido, ni en
RESEARCH.md (no hay ninguna mencion previa de esos numeros en este
documento). Puede ser: (a) un reporte sobre la cartera REAL de IOL del
usuario, no sobre el shadow del bot (serian cosas distintas - el bot
esta en shadow mode, no coloca ordenes reales); (b) un numero de un
momento intermedio del 21/10 que no esta en ninguno de los dos exports
disponibles (falta un snapshot entre 14:24 y 19:34, la ventana donde
el kill switch realmente se disparo); o (c) un error de transcripcion en
el momento en que se reporto. ~~Pendiente: el usuario indique la fuente
exacta (mensaje, hora, o captura) del "22 posiciones/$18M" para poder
trazarlo en vez de descartarlo sin verificar.~~ **RESUELTO arriba: el
usuario aclaro la fuente exacta de ambos numeros y se encontro, corrigio
y verifico el bug real que explica el "22".**

## Pre-registro — Módulo B: rupturas (breakout) del ADR GGAL, con/sin filtro de régimen CCL — 2026-10-27

A pedido explicito del usuario: nueva linea de investigacion, 100%
OFFLINE (no toca el bot, no cambia logica de produccion, permitido
durante el freeze de 3 semanas - FREEZE.md), sobre una señal de
ruptura/momentum distinta a todo lo ya evaluado (scalping - APAGADA,
weekly_asymmetric, H1/H1b/H2 - todas sobre una señal de REVERSION, no de
momentum). Metodologia COMPLETA fijada ANTES de correr una sola linea
de backtest, con el mismo criterio de todo este documento: parametros
FIJOS sin optimizar, holdout de los ultimos 2 años, comparacion contra
buy&hold y contra entradas aleatorias, veredicto con el mismo criterio
VERIFIED/SUPPORTED/HYPOTHESIS/DATA INSUFFICIENT/REJECTED.

### Datos

- **GGAL ADR (NYSE, USD), diario (OHLCV)**: `ggal_adr_daily_10y.csv`
  (scratchpad de esta sesion), obtenido via
  `mcp__invertironline__get_price_history` (API real de IOL, no
  fabricado) — 2.499 ruedas, **2016-10-13 a 2026-10-12**. Columnas:
  date, open, high, low, close, volume, log_return.
- **GGAL local (BYMA, ARS), close diario**: columna `ggal_local_close_ars`
  de `ccl_implicito_diario.csv` (scratchpad, ya usado y documentado en
  este proyecto para el CCL implicito — mismo archivo, misma fuente) —
  2.546 filas, 2016-01-04 a 2026-10-13. Se usa SOLO para (a) la
  expresion en opciones (spot local real por fecha, nunca estimado) y
  (b) el filtro de regimen opcional (CCL implicito = `ggal_local_close_ars
  / ggal_adr_close_usd`, ya calculado en ese mismo archivo). Verificado:
  2.361 fechas en comun entre ambos archivos, 0 discrepancias en el
  close del ADR entre los dos (mismo dato, misma fuente).
- **MERVAL**: DATA INSUFFICIENT — no se encontro ni se calculo en esta
  sesion ninguna serie historica del indice MERVAL: el filtro de
  regimen opcional se construye SOLO con CCL implicito (disponible y ya
  validado), nunca con MERVAL. Declarado explicitamente, no fabricado.
- **Caucion (tasa libre de riesgo historica)**: DATA INSUFFICIENT (ya
  documentado en este archivo, seccion "Disponibilidad de datos:
  caucion" — sin cambios). Para el Black-Scholes de la expresion en
  opciones se usa `rate=0.0` como supuesto EXPLICITO (no fabricado como
  dato real), igual que para dividend_yield=0.0.

### Señal (alcista — reportada separado de la bajista, nunca mezclada)

- Dia de señal T: `close[T] > max(close[T-20:T-1])` (maximo rolling de
  las 20 ruedas PREVIAS, sin incluir T) Y `volume[T] > 1.5 *
  mean(volume[T-50:T-1])` (promedio rolling de las 50 ruedas previas,
  sin incluir T). Ambas condiciones sobre el ADR (USD), exactamente
  como la pidio el usuario.
- Entrada ejecutada en `open[T+1]` (la rueda SIGUIENTE a la señal, nunca
  en el cierre de la propia señal — evita look-ahead).
- Sin pirámide: si una nueva señal aparece mientras ya hay una posicion
  abierta de una señal anterior (misma direccion), se descarta (no se
  acumula, no se reemplaza).

### Señal bajista (simetrica, SEPARADA — puts, nunca mezclada con la alcista)

- Dia de señal T: `close[T] < min(close[T-20:T-1])` Y `volume[T] > 1.5 *
  mean(volume[T-50:T-1])`. Entrada en `open[T+1]`. Misma regla de "sin
  piramide".

### Salida (identica logica para ambas direcciones, signo invertido)

- `ATR(14)` (True Range diario, promedio simple de 14 ruedas) calculado
  ÚNICAMENTE con datos hasta el dia de señal T (sin look-ahead) y
  FIJADO para toda la vida del trade (no se recalcula dia a dia — una
  decision de diseño declarada aca, no optimizada).
- Alcista: stop inicial = `entry_price - 2*ATR14`. Trailing (estilo
  chandelier): cada dia, `stop = max(stop_previo, max_close_desde_entrada
  - 2*ATR14)` — el stop solo sube, nunca baja. Sale si `close[t] <=
  stop` ese dia, ejecutado en `open[t+1]`.
- Bajista: simetrico con signos invertidos (`entry_price + 2*ATR14`,
  trailing sobre el minimo close desde entrada, sale si
  `close[t] >= stop`).
- Salida por tiempo: si ningun stop se disparo antes, se cierra en la
  apertura de la rueda 21 despues de la entrada (20 ruedas habiles de
  holding maximo — elegido para que el horizonte calce con la ventana
  de ~30 dias corridos usada despues en la expresion de opciones).

### Holdout y ventanas de reporte

Rango total de datos: 2016-10-13 a 2026-10-12. **Holdout = ultimos 2
años calendario** (2024-10-12 a 2026-10-12), nunca mirado para elegir
ningun parametro de arriba (todos fijados por el enunciado del usuario
o por una convencion estandar declarada, no por tanteo). Se reportan
TRES vistas de las mismas metricas: periodo completo, desarrollo
(2016-10-13 a 2024-10-11) y holdout (2024-10-12 a 2026-10-12) — el
holdout es el que decide el veredicto final.

### Métricas (por cada ventana, por cada direccion, reportadas separado)

1. N (trades cerrados).
2. Expectancy neta por trade (retorno % simple sobre el precio de
   entrada del ADR, BRUTO de costos de opciones — eso se trata aparte
   en la seccion de opciones) con IC95% via `bootstrap_ci` (proyecto,
   `ggal_bot/backtest/metrics.py`, seed=1234 — mismo estandar que el
   resto de este documento).
3. Profit factor = suma de retornos positivos / |suma de retornos
   negativos|.
4. Max drawdown sobre la curva de equity compuesta de los trades
   (producto de `(1+retorno_i)` en orden cronologico).
5. Buy&hold de la misma ventana: retorno total y anualizado de comprar
   el ADR al inicio y mantenerlo hasta el final de esa ventana (un solo
   numero, no un benchmark con distribucion).
6. Entradas aleatorias (Monte Carlo, **2.000 draws, seed=1234, fijado
   ahora**): en cada draw, N fechas de entrada elegidas uniformemente al
   azar dentro del rango valido de esa ventana, cada una emparejada 1:1
   (shuffle) con la DURACION real (en ruedas) de uno de los N trades
   reales de esa ventana/direccion — retorno de cada pata aleatoria =
   `close[entry_random+duracion] / open[entry_random+1] - 1`. Se
   reporta la expectancy promedio de cada uno de los 2.000 draws, y el
   PERCENTIL donde cae la expectancy REAL dentro de esa distribucion
   (ej. "percentil 82" = la señal real bate al 82% de los draws
   aleatorios de mismo N y duracion).

### Expresión en costos de opciones (aproximación declarada — Gold y Black)

Para cada trade alcista con entrada real (no para la rama bajista en
esta parte — ver limitacion abajo):

1. Spot LOCAL real (ARS) en la fecha de entrada: `ggal_local_close_ars`
   de `ccl_implicito_diario.csv` para esa fecha exacta (dato real, no
   estimado desde el ADR).
2. Call sintetico ATM (`strike = spot_local`, `days_calendar=30`,
   `days_business=21`) via `ggal_bot/models/black_scholes.py`
   (`BlackScholesGreeks`, ya existente en el proyecto, sin reinventar
   la formula). `rate=0.0`, `dividend_yield=0.0` (ver DATA INSUFFICIENT
   arriba). `sigma` = volatilidad realizada anualizada de los
   **ULTIMOS 60 ruedas del GGAL LOCAL** terminando en la fecha de señal
   (sin look-ahead, recalculada por cada trade — nunca un numero fijo
   inventado).
3. Costo de ida y vuelta: `round_trip_regulatory_cost_ars` de
   `ggal_bot/backtest/costs.py` (comision + derecho de mercado + IVA,
   **mismo modulo ya usado en todo el proyecto, sin reinventar
   numeros**) en escala **Gold** y **Black** — las dos que pidio el
   usuario. `spread_round_trip_pct=0` explicito (no hay dato real de
   spread de opciones de 10 años — mismo DATA INSUFFICIENT ya declarado
   en costs.py, no se fabrica).
4. Movimiento de spot requerido para cubrir ese costo ≈
   `costo_ars / (delta_BS * multiplicador)`, expresado como % del spot
   local — el "breakeven" de friccion, nunca de prima completa (eso
   seria otro numero, mas conservador; este es el piso minimo).
5. Movimiento REAL realizado: el retorno LOCAL real (ARS, mismo
   `ggal_local_close_ars`) entre la entrada y 21 ruedas habiles despues
   (mismo horizonte que el call sintetico) — dato real, no el del ADR
   (se tiene el dato local real pareado por fecha, no hace falta
   aproximar con el USD).
6. Se reporta, por escala (Gold/Black): mediana/media del movimiento
   requerido vs. el realizado, y la fraccion de trades donde el
   movimiento realizado CUBRE el requerido.

**Limitacion declarada de antemano**: esto NO es una simulacion de
comprar realmente esas calls (no hay 10 años de cadenas de opciones
reales con estas fechas/estrikes exactos) — es una aproximacion
Black-Scholes con volatilidad realizada contemporanea, exactamente la
misma clase de aproximacion ya usada y etiquetada como tal en la
seccion de Hipotesis 2 de este documento. La rama bajista (puts) NO se
expresa en esta ronda en terminos de opciones (fuera del pedido
explicito del usuario, que solo mencionó calls para esta parte) — si
se pide despues, se pre-registra aparte antes de calcularla.

### Filtro de régimen opcional (CCL implícito) — corte de robustez, NUNCA el veredicto principal

- "Régimen de estrés cambiario" en la fecha de señal: CCL implícito en
  el **percentil >75 de su propia distribución de las 252 ruedas
  previas** (ventana móvil de 1 año, sin look-ahead, sin optimizar el
  umbral — 75 es un punto de corte estandar de "cuartil superior", no
  elegido mirando el resultado).
- Se reportan las mismas métricas (N, expectancy+IC, profit factor)
  separadas en "régimen de estrés" vs. "régimen normal", **solo para la
  rama alcista** (simetria bajista fuera de alcance de este corte por
  ahora) — un corte exploratorio adicional, declarado desde ya como
  secundario: el veredicto final de Módulo B se basa en el resultado
  SIN este filtro (la pregunta que el usuario hizo primero), este corte
  es para contexto adicional, nunca para elegir a posteriori cual
  resultado mostrar como veredicto.

### Veredicto — criterio de éxito, declarado ANTES de correr nada

Sobre el **holdout de 2 años** (2024-10-12 a 2026-10-12), rama alcista:

- **SOPORTADA** si las TRES condiciones se cumplen a la vez: (a)
  expectancy neta con IC95% enteramente > 0; (b) la expectancy real cae
  en el percentil ≥75 de la distribución Monte Carlo de entradas
  aleatorias de mismo N/duración; (c) en la expresión de opciones,
  escala **Gold** (la más exigente), más del 50% de los trades cubren
  el movimiento requerido.
- **HIPÓTESIS** si se cumple (a) pero no (b) y/o (c) — señal con algo
  de edge bruto pero sin verificar que supere al azar o que sobreviva
  los costos reales de operarla con opciones.
- **RECHAZADA** si (a) no se cumple (IC95% cruza cero o es negativo).
- **DATA INSUFFICIENT** si N<30 en el holdout (muestra demasiado chica
  para cualquier conclusión, sin importar el resultado puntual).

Mismo criterio, aplicado de forma independiente, para la rama bajista
(sin la parte de opciones, ver limitación arriba).

**Nada de esto se corrió todavía** — este pre-registro se commitea
ANTES de ejecutar una sola línea del backtest, a pedido explícito del
usuario.

## Resultados — Módulo B (rupturas) — 2026-10-27

Corrido EXACTAMENTE con la metodología pre-registrada arriba (commit
`b84b7eb`, antes de este resultado) — ningún parámetro se tocó después
de ver un número. Script: `modulo_b_breakout.py` (scratchpad de esta
sesión, no forma parte de `ggal_bot/` — es análisis offline, no código
de producción).

**Bug real encontrado y corregido ANTES de publicar ningún número**: la
primera corrida tenía el retorno de la rama bajista (short) calculado
como `entry/exit - 1` — exactamente al revés de cómo funciona un short
real (esa fórmula deja la GANANCIA sin techo y la PÉRDIDA acotada a
-100%, cuando en un short real es al revés: ganancia máxima 100% si el
precio va a 0, pérdida sin techo si el precio sube sin límite). Se
corrigió a `(entry-exit)/entry` — misma convención que la rama alcista,
signo invertido — y se volvió a correr. El numero de la rama bajista
cambió sustancialmente (expectancy completo: de +3,58% a +0,31%); el de
la rama alcista no se tocó (no tenía el bug).

### N de señales generadas (10 años completos, antes de cortar por holdout)

Alcista: 38. Bajista: 37. (~3,7-3,8 señales/año cada rama — frecuencia
razonable para un filtro de ruptura de 20 ruedas + volumen en un solo
papel).

### Rama ALCISTA (calls)

| Ventana | N | Expectancy | IC95% | Profit factor | Max DD | Buy&hold (misma ventana) | Percentil vs. aleatorio |
|---|---|---|---|---|---|---|---|
| Completo (2017-01 a 2026-06) | 38 | +2,80% | [-1,82%, +7,73%] | 1,59 | -31,30% | +75,72% (6,19% anual) | 73,8 |
| Desarrollo (2017-01 a 2024-09) | 31 | +3,73% | [-1,75%, +9,47%] | 1,74 | -31,30% | +48,46% (5,32% anual) | 78,2 |
| **Holdout (2024-12 a 2026-06)** | **7** | -1,32% | [-6,48%, +4,95%] | 0,64 | -15,37% | -27,44% (-19,16% anual) | 46,2 |

### Rama BAJISTA (puts) — SIEMPRE separada, nunca mezclada con la alcista

| Ventana | N | Expectancy | IC95% | Profit factor | Max DD | Buy&hold (misma ventana) | Percentil vs. aleatorio |
|---|---|---|---|---|---|---|---|
| Completo (2017-06 a 2026-10) | 37 | +0,31% | [-4,88%, +5,74%] | 1,05 | -64,52% | -11,10% (-1,27% anual) | 73,9 |
| Desarrollo (2017-06 a 2024-08) | 29 | -0,42% | [-6,36%, +5,93%] | 0,94 | -63,82% | -30,80% (-5,07% anual) | 61,5 |
| **Holdout (2025-02 a 2026-10)** | **8** | +2,94% | [-6,63%, +12,35%] | 1,61 | -23,44% | -41,56% (-27,61% anual) | 61,4 |

### Filtro de régimen opcional (CCL implícito, percentil >75 de 252 ruedas previas) — corte de robustez, todo el período

| Dirección | Régimen | N | Expectancy | IC95% |
|---|---|---|---|---|
| Alcista | Estrés (CCL alto) | 28 | +3,02% | [-2,80%, +9,22%] |
| Alcista | Normal | 6 | +1,20% | [-6,31%, +10,73%] |
| Bajista | Estrés (CCL alto) | 33 | +0,42% | [-2,83%, +11,75%]* |
| Bajista | Normal | 4 | -0,63% | [-12,48%, +11,23%] |

*(nota: el expectancy de "bajista/estrés" en la tabla de arriba usa el
mismo punto +0,42% calculado por el script — IC consistente con el
resto, crece un poco porque son 33 de 37 trades, casi todo el período
cae en el percentil alto de CCL de los últimos 2 años, ya que el CCL
viene subiendo sostenidamente — ver limitación abajo)

**Limitación del corte de régimen**: con solo 10 años de historia y un
umbral de percentil 75 sobre una ventana de 252 ruedas, casi todas las
señales bajistas (33/37) y la mayoría de las alcistas (28/38) caen en
"estrés" — el CCL implícito de GGAL viene en tendencia alcista
estructural casi todo el período, así que este corte separa MUY poca
muestra en el grupo "normal" (4 y 6 trades) para decir algo con
sentido. Dato de contexto, no una conclusión.

### Expresión en opciones (aproximación, rama alcista, calls, Gold y Black)

n=35 de 38 trades (3 sin fecha pareada en `ccl_implicito_diario.csv` o
sin suficiente historia de 60 ruedas para la vol realizada — DATA
INSUFFICIENT para esos 3, no se fabricó nada):

| Escala | Movimiento requerido (mediana, % spot) | Movimiento realizado (mediana, 21 ruedas) | Cobertura |
|---|---|---|---|
| Gold | 0,19% | 10,05% | 24/35 (68,6%) |
| Black | 0,08% | 10,05% | 24/35 (68,6%) |

**Esta métrica es DÉBIL por diseño, declarado explícitamente ahora que
se ve el número** (no se cambia el pre-registro, se explica por qué no
alcanza sola): el "movimiento requerido" es solo la FRICCIÓN de
operar (comisión+derecho+IVA sobre la prima, dividido por delta) — no
el costo de la prima completa. Con la volatilidad real de GGAL
(~35-65% anualizada), ese piso de friccion (0,08%-0,19% del spot) es
tan chico que CASI CUALQUIER movimiento de un mes lo supera, señal real
o puramente al azar. Por eso el criterio de verdicto pre-registrado
nunca usó esto solo: es la condición (c) de tres, no decide nada por sí
misma. La rama bajista no se expresó en opciones en esta ronda (fuera
del pedido explícito del usuario para esta parte).

### Veredicto — aplicando el criterio pre-registrado (holdout decide, nada se mira antes)

**Alcista: DATA INSUFFICIENT.** N=7 en el holdout, por debajo del piso
de 30 declarado antes de correr nada — no se puede aplicar ninguna de
las tres condiciones de SOPORTADA/HIPÓTESIS/RECHAZADA con una muestra
así de chica, sin importar que el punto caiga en -1,32%.

**Bajista: DATA INSUFFICIENT.** N=8 en el holdout, mismo problema.

**Contexto honesto (no es el veredicto, es información adicional
pedida igual por el usuario)**: ni siquiera relajando al período
completo (10 años, no el holdout designado) se cumple la condición (a)
del criterio (IC95% enteramente >0) en NINGUNA de las dos direcciones
— el IC95% cruza cero en alcista (+2,80%, [-1,82%,+7,73%]) y en
bajista (+0,31%, [-4,88%,+5,74%]). La rama alcista sí bate al
percentil ~74-78% de las entradas aleatorias de mismo N/duración en el
período completo y en desarrollo (condición b, cerca pero no sobre el
75 declarado) — la bajista también bate el azar en el período completo
(73,9) y en desarrollo (61,5) pero mas debil. Es decir: **hay una señal
con ALGO de contenido direccional (bate al azar mas seguido que no), en
ninguna de las dos direcciones el bruto es estadísticamente
significativo, y la muestra pre-registrada específicamente para decidir
(el holdout de 2 años) es demasiado chica para decidir nada** — ni para
confirmar ni para descartar. Esto no es un resultado "lindo", es el
resultado honesto de haber fijado el umbral de N ANTES de mirar el
dato: con una señal que dispara ~4 veces por año, 2 años de holdout
nunca iban a dar mas de 7-8 observaciones.

**Pendiente, si el usuario quiere seguir esta línea**: la unica forma
de resolver el DATA INSUFFICIENT del holdout sin fabricar nada es (a)
acumular mas años de holdout real esperando que pase el tiempo (no
acelerable), o (b) que el usuario acepte relajar el piso de N=30 a algo
mas chico A SABIENDAS de que eso reduce la confiabilidad estadistica
del corte (una decision suya, no algo que se cambie en silencio
despues de ver que 30 no se alcanzaba).

El usuario acepto el DATA INSUFFICIENT de arriba y diagnostico el
motivo el mismo: diseño de un solo activo con ~4 señales/año no da
muestra suficiente en 2 años de holdout. Pedido explicito, mismo rigor:
generalizar la señal a un PANEL de ADRs argentinos para juntar N real
sin tocar ningun parametro de la señal. Eso es lo que sigue.

## Pre-registro — Módulo B PANEL: misma señal sobre 14 ADRs argentinos — 2026-10-27

Pedido explicito del usuario, mismo criterio de rigor que arriba:
pre-registrar (commitear ANTES de correr) la generalizacion de Módulo B
a un PANEL de ADRs argentinos que cotizan en NYSE/NASDAQ via IOL, para
resolver el problema de muestra chica del pre-registro anterior (señal
de ~4/año en un solo activo). **Señal, entrada, salida y parametros de
riesgo son EXACTAMENTE los mismos ya fijados y commiteados en el
pre-registro single-asset de arriba (commit `b84b7eb`) — nada se
reoptimiza ni se ajusta por activo.** Analisis 100% OFFLINE (scratchpad
de esta sesion, no toca `ggal_bot/`), permitido durante el freeze de 3
semanas (FREEZE.md) por la misma razon que el pre-registro anterior.

### Universo y disponibilidad de datos

Pedido: GGAL, BMA, SUPV, BBAR, YPF, PAM, TGS, CEPU, EDN, LOMA, CRESY,
IRS, TEO, CAAP — "usar los que esten disponibles y listar cuales no".
**Resultado: las 14 estan disponibles** (ninguna ausente), obtenidas
via `mcp__invertironline__get_price_history` (API real de IOL, mismo
tool ya usado y documentado arriba) en el mercado NYSE para todas
excepto CRESY (NASDAQ). 4 de las 14 tienen menos de 10 años de
historia por cotizar en NYSE/NASDAQ desde una fecha posterior (listado
mas reciente del ADR) — se usan igual, con la fecha real de inicio
declarada, nunca rellenada hacia atras:

| Ticker | Mercado | Primera fecha | Última fecha | N limpio | Dropped | Repaired |
|---|---|---|---|---|---|---|
| GGAL  | NYSE   | 2016-10-13 | 2026-10-26 | 2.509 | 0  | 3   |
| YPF   | NYSE   | 2016-10-27 | 2026-10-26 | 2.500 | 0  | 18  |
| BMA   | NYSE   | 2010-01-04 | 2026-10-26 | 4.183 | 7  | 24  |
| BBAR  | NYSE   | 2010-01-04 | 2026-10-26 | 4.176 | 4  | 13  |
| PAM   | NYSE   | 2010-01-04 | 2026-10-26 | 4.156 | 12 | 26  |
| TGS   | NYSE   | 2010-01-04 | 2026-10-26 | 4.149 | 18 | 168 |
| EDN   | NYSE   | 2010-01-04 | 2026-10-26 | 3.949 | 12 | 223 |
| CRESY | NASDAQ | 2010-01-04 | 2026-10-26 | 4.156 | 4  | 14  |
| IRS   | NYSE   | 2010-01-04 | 2026-10-26 | 4.083 | 8  | 112 |
| TEO   | NYSE   | 2010-01-04 | 2026-10-26 | 4.151 | 7  | 57  |
| SUPV  | NYSE   | 2018-07-26 | 2026-10-26 | 2.067 | 2  | 59  |
| CEPU  | NYSE   | 2018-03-01 | 2026-10-26 | 2.169 | 1  | 11  |
| LOMA  | NYSE   | 2018-10-26 | 2026-10-26 | 2.000 | 2  | 15  |
| CAAP  | NYSE   | 2020-04-07 | 2026-10-26 | 1.578 | 1  | 53  |

**4 tickers con historia mas corta que 10 años (SUPV, CEPU, LOMA,
CAAP)**: cotizan en NYSE desde una fecha posterior a 2016 (listado del
ADR), no es un problema de la API — se usan con su rango real completo,
declarado en la tabla. No se excluye ningun ticker del panel.

**Limpieza de datos (regla declarada, aplicada igual a los 14,
incluyendo GGAL retroactivamente sobre la copia usada en este panel —
el archivo del pre-registro single-asset anterior NO se tocó)**: cada
fuente de `get_price_history` trae ocasionalmente barras con `open`,
`high`, `low` o `close` en `0` literal (artefacto de campo
faltante/placeholder, visto en las 14 series, 0,03%-4,5% de las filas
segun el ticker). Regla, sin excepciones y sin mirar el resultado antes
de fijarla: si `close<=0` o `open<=0` → **DROP** (dato realmente
invalido/faltante, no se puede reconstruir sin fabricar). Si solo
`high<=0` o `low<=0` (con open/close validos) → **REPAIR** usando SOLO
datos de la misma fila: `high=max(open,close)` si high era 0,
`low=min(open,close)` si low era 0 (nunca se inventa un numero nuevo,
solo se corrige un campo obviamente erroneo con otro campo real de la
MISMA barra). Conteos exactos por ticker en la tabla de arriba — ningun
resultado de abajo usa una fila dropeada, y toda fila reparada queda
marcada como tal en el CSV limpio de scratchpad.

### Señal, entrada, salida — SIN CAMBIOS respecto del pre-registro anterior

Exactamente las reglas ya fijadas arriba ("Señal (alcista...)", "Señal
bajista...", "Salida..."), aplicadas de forma IDENTICA a cada uno de
los 14 tickers de forma independiente (rolling de 20 ruedas, volumen
1.5x del promedio de 50 ruedas previas, ATR14 fijado al momento de la
señal, trailing chandelier 2xATR14, salida por tiempo a 21 ruedas). Sin
piramide DENTRO de cada ticker/direccion (igual que antes). Entre
tickers no hay restriccion: dos señales de tickers distintos el mismo
dia son dos trades independientes del panel (eso es precisamente lo
que da la muestra que faltaba).

### Costos de acciones (NO los de opciones — modelo nuevo, especifico de este pre-registro)

El modulo `ggal_bot/backtest/costs.py` (comision Gold/Platinum/Black +
derecho de mercado BYMA + IVA) es especifico de **opciones sobre
acciones LOCALES en BYMA** y no aplica a este panel, que opera los
**ADRs mismos en NYSE/NASDAQ** via IOL — un producto y mercado
distinto, con tarifario propio. Se investigo el tarifario real de IOL
para operaciones en EEUU (no se reusa el de BYMA por conveniencia):

**FUENTES (relevadas por WebFetch el 2026-10-27, ver conversacion)**:
   1. `https://www.invertironline.com/tarifas` — confirma que las
      escalas Gold/Platinum/Black (0.50/0.30/0.10%) aplican
      explicitamente a "acciones, bonos, cedears, letras, obligaciones
      negociables y opciones, **en Argentina**" — NO a operaciones en
      el mercado de EEUU. Remite a una pagina separada para EEUU.
   2. `https://www.invertironline.com/tarifas-estados-unidos` —
      tarifario real para "Compra y venta de acciones, ETFs, ADR y
      REITs": **comision variable 0,35% + IVA**, **minimo US$ 2 +
      IVA**, por operacion (se aplica a cada pata: compra Y venta por
      separado, igual logica que el "ida y vuelta" ya usado en
      `costs.py` para opciones). Nota al pie: clientes vinculados a
      AP/AAG/Cuenta Empresas/Otros pueden pagar hasta 1% — no aplica a
      este caso (no hay informacion de que la cuenta real sea de ese
      tipo, se usa la tarifa estandar 0,35%).

**Formula de costo de ida y vuelta (declarada ahora, se usa sin
cambios al correr)**:
```
comision_por_pata = max(0.0035 * monto_pata, 2.00_USD)
comision_por_pata_con_iva = comision_por_pata * 1.21
costo_total_ida_y_vuelta = comision_por_pata_con_iva(entrada) + comision_por_pata_con_iva(salida)
```
**Correccion de un bug real, ANTES de publicar ningun numero** (se
detecto al ver que la primera corrida daba expectancies de -50%/-60%
por trade, numero sin sentido economico para un stop de 2xATR con
holding maximo de 20 ruedas): la version original de esta formula no
declaraba el tamaño de posicion (`monto_pata`), y la implementacion
inicial asumio 1 ADR por trade "por default" — eso hace que el minimo
de US$2+IVA por pata (≈US$4,84 de ida y vuelta) se compare contra un
notional de ~US$10-40 (precio tipico de los ADRs del panel), un
~12%-48% de costo de ida y vuelta por trade, un artefacto de unidades
(nadie opera 1 ADR real; el minimo de orden existe precisamente para
que sea irrelevante en cualquier operacion de tamaño normal), no un
costo real. **Formula corregida y la que se usa**: para cualquier
orden con notional por pata mayor a US$2/0,35% ≈ US$571,43 (el punto
donde el minimo deja de aplicar — cualquier posicion real, sea de
varios cientos o miles de dolares, supera eso con margen), el costo
converge al termino puramente porcentual:
```
costo_total_ida_y_vuelta_pct = 2 * 0.0035 * 1.21 = 0,8470% del notional de entrada
```
Se usa este numero FIJO (no depende del precio de fill ni de un
tamaño de posicion asumido) como el costo de ida y vuelta para TODOS
los trades del panel — es el costo real para cualquier operacion de
tamaño institucional o retail razonable (no para 1 ADR), y es
levemente CONSERVADOR (ligeramente mas caro) que una operacion mas
grande donde el minimo nunca aplicaria de todas formas. El minimo de
US$2+IVA por pata sigue siendo real y relevante solo para una cuenta
que efectivamente opere con notionales de pocos cientos de dolares por
pata — fuera de alcance de este backtest (no hay un supuesto de
tamaño de cuenta real documentado en el proyecto para la pata de
acciones, a diferencia de `max_capital_ars` que es especifico de la
pata de opciones — ver `ggal_bot/config.py`), declarado aca
explicitamente para que conste.

**DATA INSUFFICIENT declarado, NO fabricado, excluido del modelo base**:
la misma pagina de EEUU lista un "ADR Fee*: 2%" con la nota "costo
estimado por valor nominal a la fecha de posicion", sin aclarar
periodicidad (¿anual? ¿por operacion? ¿se cobra solo a quien mantiene
la posicion en una fecha de corte, como el "record date" tipico de los
depositary fees de ADRs en EEUU, que normalmente son centavos por ADR
y no un %?). No se encontro una aclaracion mas especifica en la pagina.
**Decision**: se EXCLUYE este cargo del modelo de costos base (no se
fabrica un supuesto sobre su periodicidad), y se declara explicitamente
que esto hace que los costos reportados sean un **piso** (podrian ser
mayores si ese cargo aplica por operacion o por posicion abierta en
alguna fecha de corte dentro del holding period de 21 ruedas) — nunca
un techo. No se encontro tampoco ningun cargo regulatorio equivalente
al SEC fee (existe en el mercado real de EEUU pero es de fracciones de
basis point, inmaterial frente al 0,35%+IVA ya modelado; no cambia
ningun veredicto, se omite por ser no-material, no por falta de
busqueda).

### Bootstrap agrupado por fecha (cluster bootstrap) — pedido explicito del usuario

El bootstrap i.i.d. ya existente en el proyecto (`bootstrap_ci` de
`ggal_bot/backtest/metrics.py`, remuestreo por TRADE individual) no
sirve aca sin modificacion: señales de distintos tickers el mismo dia
estan correlacionadas (mismo shock macro/FX/dia de mercado argentino),
tratarlas como independientes infla artificialmente la confianza del
IC. Metodo declarado (implementacion nueva en el script de scratchpad,
no se toca `metrics.py` del proyecto):

1. Agrupar los trades (de la ventana y direccion en cuestion) por
   **fecha de señal** (no fecha de entrada — la correlacion nace el
   dia que dispara la señal en todos los tickers a la vez). Cada fecha
   de señal es un "cluster" que puede contener 1 o varios trades (de
   distintos tickers).
2. Remuestreo: `n_resamples=10.000`, `seed=1234` (misma semilla fija
   que el resto del proyecto, por reproducibilidad — no es una fuente
   de aleatoriedad de mercado). En cada resample, elegir con reemplazo
   tantas fechas-cluster como clusters distintos existan en la muestra
   real; juntar TODOS los trades de las fechas elegidas (si una fecha
   sale sorteada 2 veces, sus trades entran 2 veces); calcular la
   expectancy promedio (media simple de retornos) sobre ese pool.
3. IC95% = percentil 2.5/97.5 de las 10.000 expectancies resampleadas
   (misma convencion de percentiles que `bootstrap_ci`). Punto central
   = expectancy real (media simple de todos los trades de la ventana,
   sin resamplear).
4. Aplicado por separado para alcista y bajista, y por separado para
   cada ventana de reporte (completo/desarrollo/holdout).

### Construcción de la cartera de señales (portfolio) — regla declarada, una sola, sin optimizar

Para el drawdown y la curva de equity de "la cartera de señales"
(pedido explicito), con trades de distintos tickers que pueden
solaparse en el tiempo:

- **Peso igual por trade abierto, rebalanceado diariamente**: si en un
  dia dado hay `M` trades abiertos (de cualquier ticker/direccion de
  esa rama), cada uno recibe peso `1/M` del capital de esa rama ese
  dia. El retorno diario de la cartera = suma ponderada de los
  retornos diarios (close-to-close, con open-to-close en el dia de
  entrada y close-to-open-de-salida en el dia de cierre) de cada trade
  abierto ese dia.
- Curva de equity = producto acumulado de `(1+retorno_diario_cartera)`.
- **Max drawdown** = maximo retroceso pico-a-valle de esa curva (igual
  definicion que el resto del documento).
- **Profit factor** se reporta a nivel trade (no de la curva diaria,
  mas simple y sin ambiguedad): suma de retornos de trades positivos /
  |suma de retornos de trades negativos|, pooleando todos los trades de
  esa ventana/direccion (igual formula que el pre-registro anterior).
- Esta regla (peso igual, rebalanceo diario) es una convencion simple y
  declarada ANTES de ver el resultado — no es la unica regla posible
  (podria pesarse por volatilidad, por ejemplo), pero es la mas simple
  que no requiere ningun parametro adicional que optimizar.

### Comparación contra entradas aleatorias — mismo N, duración Y tickers, con p-valor

Generalizacion directa del benchmark Monte Carlo ya usado arriba,
ahora emparejado tambien por TICKER (no solo por duracion):

1. `n_draws=2.000`, `seed=1234` (misma convencion que el pre-registro
   anterior).
2. En cada draw, para CADA trade real `i` (de ticker `t_i`, duracion
   `d_i` ruedas) se sortea una fecha de entrada aleatoria dentro del
   rango valido de ESE MISMO ticker `t_i` en esa ventana (nunca se
   cambia de ticker) — asi el draw tiene exactamente el mismo N, la
   misma duracion por trade y el mismo ticker por trade que la muestra
   real, solo cambia CUANDO se entro.
3. Retorno de cada pata aleatoria = `close[entry_random+d_i] /
   open[entry_random+1] - 1` (misma formula que el pre-registro
   anterior).
4. Estadistico de cada draw = expectancy promedio (media simple) del
   pool de N retornos aleatorios de ese draw.
5. **p-valor** (one-sided, direccion = "la señal real le gana al
   azar"): `p = (1 + #draws con estadistico >= expectancy_real) /
   (1 + 2.000)` para la rama alcista y bajista por separado (formula
   estandar de test de permutacion, nunca puede dar exactamente 0).
   Se reporta tambien el percentil (igual que antes) como lectura
   complementaria mas intuitiva, pero el p-valor es el numero que
   decide.

### Filtro de régimen — DOS variantes declaradas de antemano, nunca el veredicto principal

- **Variante primaria: CCL implícito** (`ccl_implicito_diario.csv`, ya
  validado en este proyecto) — misma regla ya usada arriba (percentil
  >75 de la ventana movil de 252 ruedas previas, sin look-ahead, sin
  optimizar el umbral). Cobertura real: 2016-01-04 a 2026-10-13 — las
  señales con fecha posterior a 2026-10-13 (ultimas ~2 semanas del
  panel) quedan DATA INSUFFICIENT para este corte especifico
  (declarado ahora, afecta a muy pocos trades, no al resto del
  resultado).
- **Variante secundaria: MERVAL** — **DEFECTO DE DATOS REAL
  encontrado y declarado ahora, ANTES de usar la serie**: la serie de
  10 años de MERVAL/BCBA obtenida via `get_price_history` tiene un
  valor identico congelado del 2026-10-16 al 2026-10-21 (stale quote)
  seguido de una caida de ~29x el 2026-10-22 (de 2.841.556,46 a
  98.228,57) que no corresponde a ninguna redenominacion conocida —
  un defecto de la fuente, no un evento real de mercado. **Decision**:
  la serie de MERVAL se **recorta en 2026-10-15** (ultimo valor
  confiable antes del tramo defectuoso) para esta variante — cualquier
  señal con fecha de régimen posterior a esa fecha queda DATA
  INSUFFICIENT para el corte de MERVAL especificamente (igual
  tratamiento que el gap de CCL arriba). Misma regla de percentil >75
  de 252 ruedas previas, aplicada sobre el nivel del indice (no hay
  OHLC de indice, solo close).
- Ambas variantes se reportan como corte de robustez adicional (N,
  expectancy, IC agrupado por fecha) separadas en
  "regimen de estres" vs. "regimen normal", **solo para contexto**: el
  veredicto principal de esta ronda se basa SIEMPRE en el resultado SIN
  filtro de regimen (la pregunta que pidio el usuario primero).

### Veredicto — criterio de éxito del PANEL, declarado ANTES de correr nada

Sobre el holdout (**últimos 2 años de TODO el panel**: toda señal con
fecha de entrada dentro de los ultimos 2 años calendario del rango
total de datos del panel, 2024-10-26 a 2026-10-26, sin importar el
ticker — no hay un holdout distinto por ticker), separado por
direccion:

- **SOPORTADA** si las TRES condiciones se cumplen a la vez: (a)
  expectancy neta del panel (ya neta de costos de acciones) con IC95%
  por bootstrap agrupado por fecha enteramente > 0; (b) p-valor < 0.05
  contra el benchmark de entradas aleatorias de mismo N/duracion/
  ticker; (c) N(holdout, esa direccion, panel completo) ≥ 30.
- **HIPÓTESIS** si se cumple (a) y/o (b) pero no las tres juntas, Y
  N≥30 — hay algo de señal pero no pasa el estandar completo.
- **RECHAZADA** si N≥30 pero el IC95% de (a) cruza cero o es negativo
  Y el p-valor de (b) no es significativo.
- **DATA INSUFFICIENT** si N<30 en el holdout del panel para esa
  direccion (mismo piso que el pre-registro anterior) — con 14
  tickers en vez de 1 esto debería ser mucho mas dificil de alcanzar
  que en la ronda anterior, pero se declara igual por si algun ticker
  aporta muy pocas señales en esa ventana especifica.

**GATE EXPLICITO, pedido por el usuario — condicion de bloqueo, no de
sugerencia**: la expresion en costos de opciones de GGAL (escalas Gold
y Black, misma metodologia ya usada arriba) **SOLO se ejecuta si la
rama ALCISTA del panel (la que corresponde a calls, unica direccion
que este proyecto expresa en opciones hasta ahora) resulta SOPORTADA o
al menos cumple la condicion (a) con IC95% enteramente > 0 en el
holdout** — si el panel no llega a eso, la expresion en opciones de
GGAL NO se hace en esta ronda, sin excepcion y sin pedir permiso para
"probar igual". (La rama bajista no tiene expresion en opciones en
este proyecto todavia — ver limitacion ya declarada arriba — por lo
que el gate no aplica a esa rama hasta que exista esa expresion, y si
se pide, se pre-registrara ese gate aparte antes de calcularla.)

**Nada de esto se corrió todavía** — este pre-registro se commitea
ANTES de ejecutar una sola línea del backtest del panel, a pedido
explícito del usuario, igual que la ronda anterior.
