# GGAL BOT — Bot de Trading Cuantitativo de Opciones sobre GGAL (BYMA)

Bot de opciones delta-neutral / trading de volatilidad sobre GGAL (Grupo Financiero Galicia),
basado en la filosofia de volatilidad, delta-neutralidad y gestion de griegas de
Ricardo Saenz de Heredia. Mismo esquema modular de proyecto usado en Quantbot, adaptado
a opciones sobre BYMA en lugar de futuros de cripto.

Ver **`docs/Diseno_Bot_Opciones_GGAL.md`** para el marco teorico completo, la arquitectura,
el diagrama de flujo del ciclo de vida de una orden, y el checklist de validacion.

## Estructura del proyecto

```
GGAL BOT/
├── README.md
├── requirements.txt
├── requirements-build.txt      # solo para compilar el .exe (ver build_exe.bat)
├── requirements-dashboard.txt   # solo para el dashboard (ver run_dashboard.bat)
├── .env.example              # copiar a .env y completar credenciales del ALYC
├── run_bot.py                 # punto de entrada (orquestador principal)
├── run_bot.bat                 # lanzador para Windows (usar en vez de ".\run_bot.py")
├── run_dashboard.bat             # lanza el dashboard de monitoreo en el navegador
├── build_exe.bat                # compila dist\GGAL_BOT.exe con PyInstaller
├── docs/
│   └── Diseno_Bot_Opciones_GGAL.md
├── logs/                       # logs de corrida (se crea solo)
├── dashboard/                   # Dashboard web local de monitoreo (Streamlit)
│   ├── app.py                    # UI: KPIs, tabla de trades, equity curve, smile de IV
│   └── pnl_engine.py              # apareo FIFO de compras/ventas -> PnL realizado/no realizado
└── ggal_bot/                   # paquete Python
    ├── config.py                # toda la configuracion/limites del bot
    ├── paths.py                 # resolucion de rutas (logs, estado)
    ├── state_writer.py          # persistencia del estado (JSON) para monitoreo
    ├── data/
    │   ├── market_data_feed.py   # conexion PyRofex (WS de mercado)
    │   ├── live_shadow_feed.py   # Shadow Trading / Live Replay (multi-fuente con failover: primary_ws/data912/broker_rest/mock)
    │   ├── http_utils.py         # GET con timeout de pared real (ver seccion "bot trabado" abajo)
    │   ├── option_chain.py       # matriz de puntas + cadena de opciones
    │   └── technical_analysis.py # filtro de tendencia 1D (EMA/RSI/ADX/MACD) - ver seccion propia abajo
    ├── models/
    │   ├── black_scholes.py      # pricer + griegas
    │   ├── implied_vol.py        # solver de IV (Newton-Raphson + biseccion)
    │   ├── historical_volatility.py
    │   ├── volatility_surface.py # smile suavizado, señal IV vs HV
    │   └── microstructure.py     # Order Book Imbalance (confirmacion de calidad de ejecucion)
    ├── portfolio/
    │   └── portfolio.py          # posiciones y griegas totales de cuenta
    ├── execution/
    │   ├── market_making.py      # mid-price / agresivo vs pasivo
    │   ├── mid_price_exec.py     # ejecucion a mid-price con control de slippage
    │   └── order_gateway.py      # envio/tracking de ordenes (PyRofex) + Paper Execution shadow
    ├── strategy/
    │   ├── delta_hedger.py       # rebalanceo de delta (contado/futuro)
    │   ├── vol_arbitrage.py      # deteccion de descalibres de smile (modo delta-neutral)
    │   ├── weekly_asymmetric.py  # modo "Long-First / Weekly Asymmetric" (ver seccion propia abajo)
    │   └── scalping.py           # modo "Scalping Intradia" ADITIVO (ver seccion propia abajo)
    ├── risk/
    │   ├── risk_manager.py       # limites de vega/gamma, filtros de liquidez, exits de Long-First y Scalping
    │   └── position_sizer.py     # sizing dinamico por capital (reusado por Long-First y Scalping)
    └── validation/
        ├── test_quant_engine.py       # tests de sanity sin broker real
        ├── test_execution_pipeline.py # gateway, mid-price exec, delta-hedger, bootstrap
        ├── test_shadow_trading.py     # Shadow Trading: fuentes de datos y paper execution
        ├── test_dashboard_pnl.py      # apareo FIFO, marca a mercado, metricas de PnL
        ├── test_long_first_mode.py    # sizing, exits y señales del modo Long-First / Weekly Asymmetric
        ├── test_strategy_selector.py  # seleccion de estrategia en run_bot.py y orden salidas-antes-que-entradas
        ├── test_technical_analysis.py # indicadores, clasificacion de tendencia y cache del filtro 1D
        ├── test_microstructure.py     # Order Book Imbalance y profundidad minima de ASK
        ├── test_http_utils.py         # timeout de pared real para las llamadas REST a data912.com
        └── test_scalping_mode.py      # modo Scalping ADITIVO: velas intradia, reversion de IV, exits, aislamiento
```

## Shadow Trading / Live Replay (probar la logica sin arriesgar capital)

Si tu ambiente `REMARKET` no tiene aprovisionada la cadena de opciones de GGAL (ver
`diagnose_instruments.py`), activa el modo Shadow en `.env`:

```bash
GGAL_BOT_SHADOW_MODE=true
GGAL_BOT_SHADOW_SOURCE=auto   # legado: auto | data912 | mock (ver multi-fuente abajo)
```

En este modo `order_gateway.py` **nunca envia ordenes reales**: toda orden se simula
con un fill inmediato al mid vigente (nunca toca la API real de pyRofex) y queda
auditada en `logs/shadow_trades.csv`. Los DATOS de mercado, en cambio, pueden venir de
varias fuentes intercambiables (ver siguiente seccion).

### Multi-fuente con prioridad y failover automatico (`GGAL_BOT_SHADOW_SOURCE_PRIORITY`)

Ademas del selector legado de dos fuentes (`GGAL_BOT_SHADOW_SOURCE=auto|data912|mock`),
el modo Shadow soporta una **lista de fuentes con prioridad explicita**, con conmutacion
automatica (failover) si la fuente activa deja de responder de forma sostenida:

```bash
GGAL_BOT_SHADOW_SOURCE_PRIORITY=primary_ws,data912,mock
GGAL_BOT_SHADOW_SOURCE_FAILURE_THRESHOLD=3      # fallos consecutivos antes de conmutar
GGAL_BOT_SHADOW_SOURCE_REPROBE_SECONDS=300      # cada cuanto se reintenta volver a una fuente preferida
```

Fuentes disponibles (ver `ggal_bot/data/live_shadow_feed.py`):

| Nombre | Clase | Que es | Estado |
|---|---|---|---|
| `primary_ws` | `PrimaryMarketDataSource` | Primary/Matba Rofex via pyRofex, credenciales de **solo Market Data** (L1: BID/OFFER/LAST) | Oficial, reusa `market_data_feed.py`/`order_gateway.py` ya probados |
| `data912` | `Data912RestSource` | REST publico de data912.com (sin autenticacion) | Verificado contra respuestas reales |
| `broker_rest` | `BrokerRestSource` | REST de IOL/InvertirOnline | **Confirmado contra una cuenta real** (login + esquema de cotizacion/opciones) |
| `mock` | `MockReplaySource` | Generador sintetico 100% local (GBM + smile + shocks) | Siempre disponible, nunca falla |

**Como funciona el failover**: al arrancar, `LiveShadowFeed` prueba cada fuente de la
lista en orden (`is_available()`) y usa la primera que responda. Durante la corrida, si
la fuente activa acumula `GGAL_BOT_SHADOW_SOURCE_FAILURE_THRESHOLD` polls consecutivos
sin datos utiles (no un fallo aislado - mismo criterio que la guardia de staleness de
abajo), conmuta automaticamente a la siguiente fuente disponible **y re-descubre el
universo de instrumentos contra ella** (los simbolos de una fuente no sirven para otra:
`"GFGC4200AG"` en data912 no significa nada para pyRofex). Si se agotan todas las
fuentes configuradas, cae incondicionalmente a `mock` (100% local, nunca falla) - el bot
jamas se queda sin ninguna fuente de datos, y ningun fallo de fuente interrumpe la
estrategia ni tira el proceso. Cada tanto (`GGAL_BOT_SHADOW_SOURCE_REPROBE_SECONDS`) se
reintenta volver a una fuente de mayor prioridad si esta volvio a estar disponible.

Sin `GGAL_BOT_SHADOW_SOURCE_PRIORITY` seteada, el comportamiento es identico al de
antes de esta funcion (mapea el selector legado: `"auto"` → `("data912", "mock")`,
`"data912"` → solo esa, `"mock"` → solo esa).

**`primary_ws` (Primary/Matba Rofex, MD-only)**: para usarla, completar en `.env`
`PYROFEX_MD_USER`/`PYROFEX_MD_PASSWORD`/`PYROFEX_MD_ACCOUNT` (idealmente un usuario de
**solo lectura** de market data que tu ALYC te habilite, separado de tu usuario de
trading real) - si se dejan vacias, cae a `PYROFEX_USER`/`PASSWORD`/`ACCOUNT` de
trading. Aunque abre una conexion real de websocket a Primary, **nunca puede operar**:
en modo Shadow, `OrderGateway.send()` intercepta cualquier orden antes de que llegue a
pyRofex (ver `test_order_gateway_shadow_mode_never_touches_real_send_order`).

**`broker_rest` (IOL/InvertirOnline)**: login y esquema de cotizacion/opciones
CONFIRMADOS corriendo `diagnose_iol_api.py` contra una cuenta real de IOL:

- **Login** (`POST /token`, form-urlencoded, `grant_type=password`, respuesta con
  `access_token`/`token_type`/`expires_in` (~20 min en la corrida de referencia) /
  `refresh_token`, uso como `Authorization: Bearer <token>`).
- **Cotizacion de un titulo** (`GET /api/v2/{market}/Titulos/{simbolo}/Cotizacion`):
  `ultimoPrecio` a nivel raiz; `puntas` (lista, se toma el mejor nivel `puntas[0]`) puede
  venir **vacia fuera de rueda** (visto en la corrida de referencia, con el mercado
  cerrado) - no es un bug, simplemente no hay punta vigente en ese momento.
- **Cadena de opciones** (`GET /api/v2/{market}/Titulos/{simbolo}/Opciones`): cada
  registro trae `simbolo`, `tipoOpcion` (`"Call"`/`"Put"`, directo) y `fechaVencimiento`
  (ISO, directo) - **y, ademas, la cotizacion de esa opcion YA EMBEBIDA** en el campo
  `cotizacion` de cada registro. Esto es mejor de lo esperado: un unico request devuelve
  la cadena completa (174 opciones en la corrida de referencia) con su cotizacion
  incluida, asi que `fetch_snapshot()` refresca TODA la cadena con **un solo request**
  (mas uno para el subyacente) en cada poll - sin necesidad de pedir cada simbolo por
  separado ni de ningun mecanismo de rate-limit/round-robin.
- El **strike** no viene como campo numerico separado - se extrae del simbolo
  (convencion de exchange/BYMA: prefijo + digitos + mes, ej. `"GFGV4200SE"` → strike
  4200, la misma convencion que ya usa `data912`), reusando `tipoOpcion`/`fechaVencimiento`
  como fuente primaria y el simbolo solo como fallback para esos dos campos.

Antes de operar con esta fuente, una recomendacion: confirmar que el uso programatico
de la API esta permitido por los terminos de tu cuenta de IOL.

**`BROKER_REST_REQUEST_TIMEOUT` (confirmado en horario de mercado real)**: la primera
corrida de validacion se hizo fuera de horario de rueda, con las puntas vacias/en cero;
al correr el bot durante la rueda (BYMA ~11-17hs ART) con el default original de 5
segundos aparecieron `Read timed out (read timeout=5.0)` repetidos en
`BrokerRestSource.fetch_snapshot()` - el request a `/Opciones` devuelve 174 registros
con la cotizacion de cada uno embebida, y ese volumen de datos tarda mas de 5s en
resolverse con el mercado activo (fuera de horario, con menos actividad, entraba comodo
en el margen). El default paso a **15 segundos** (`BROKER_REST_REQUEST_TIMEOUT=15` en
`.env.example` y en `BrokerRestConfig`) para darle margen a ese request masivo; si
persisten los timeouts en tu conexion particular, subir este valor aun mas es seguro
(el failover a `data912`/`mock` sigue activo como red de contencion si la fuente queda
no disponible de verdad).

Ver la seccion siguiente para la evaluacion completa de las fuentes consideradas
(incluidas las que se descartaron y por que).

Toda orden que el bot "envia" en modo Shadow se simula con un fill inmediato al mid
vigente en `order_gateway.py` (nunca toca la API real) y queda auditada en
`logs/shadow_trades.csv`. Ver `ggal_bot/data/live_shadow_feed.py` y `ggal_bot/config.py`
(`ShadowConfig`/`BrokerConfig.md_*`/`BrokerRestConfig`) para todos los parametros
ajustables.

### Evaluacion tecnica de fuentes de datos alternativas

Analisis realizado para decidir que fuentes agregar como reemplazo/complemento de
`data912.com` (ver tabla arriba). Resumen por candidata:

- **Primary / PyRofex (implementada, `primary_ws`)**: acceso oficial y documentado a
  Matba Rofex, con soporte para credenciales de solo Market Data. Es la unica fuente de
  las evaluadas que puede dar profundidad L2 real (puntas con cantidades) y baja
  latencia (push por websocket, no polling). Ademas ya estaba parcialmente implementada
  en este proyecto (`market_data_feed.py`, usado por el camino de trading real), asi que
  no se fabrico ningun protocolo nuevo. **Recomendada como fuente primaria** una vez que
  se disponga de credenciales MD-only.
- **IOL / InvertirOnline (implementada y confirmada, `broker_rest`)**: API oficial y
  documentada. El login se confirmo contra la documentacion oficial de IOL, y el
  esquema de cotizacion/cadena de opciones se confirmo corriendo `diagnose_iol_api.py`
  contra una cuenta real de un usuario del proyecto - incluyendo un hallazgo favorable
  no anticipado (la cadena de opciones trae la cotizacion de cada opcion ya embebida,
  asi que un solo request por poll alcanza para toda la cadena, sin necesidad de
  ningun mecanismo de rate-limit). No tiene profundidad L2 (solo la mejor punta), asi
  que queda por detras de `primary_ws` en la prioridad recomendada si se dispone de
  credenciales de Primary/Matba Rofex, pero es una alternativa solida y ya verificada
  si no.
- **Bull Market Brokers / Cocos Capital**: sin API oficial publica confirmada - solo se
  encontraron clientes no oficiales/de ingenieria inversa de la comunidad, sin garantia
  de estabilidad ni de que su uso este permitido por los terminos de servicio. **No
  implementadas** (riesgo de ToS + inestabilidad de un protocolo no documentado).
- **Rava Bursatil**: no se encontro una API publica de desarrollador documentada.
  **No implementada**.
- **Bolsar / BYMA (APIs oficiales, `byma.com.ar/en/byma-apis`, `open.bymadata.com.ar`)**:
  existen, pero son de acceso **institucional/contractual** (requieren aprobacion
  comercial de BYMA), no publico ni gratuito. **No implementadas** por no ser accesibles
  para un usuario individual sin ese contrato.
- **"TradingView local feed"**: no existe un feed de redistribucion publica y legitima
  de TradingView para este uso; cualquier alternativa implicaria un protocolo no
  oficial/violatorio de sus terminos de servicio. **No implementada**, deliberadamente.

## Dashboard de monitoreo (PnL, griegas, smile de IV)

`run_dashboard.bat` (o `streamlit run dashboard/app.py` con el `.venv` activado) abre un
panel web local que lee `logs/shadow_trades.csv` y `state/bot_state.json` — no hace falta
que el bot este corriendo en ese momento, alcanza con que haya generado datos alguna vez.
Se auto-refresca cada 2-30s (configurable en la barra lateral) releyendo esos dos archivos,
sin reiniciar el bot.

Incluye: PnL total (realizado + no realizado, ARS y %), Win Rate, Profit Factor, Sharpe
aproximado y Max Drawdown; griegas agregadas en tiempo real; tabla de trades cerrados y
posiciones abiertas (filtrable por estrategia — vol_arbitrage vs delta_hedge —, tipo de
opcion y simbolo); curva de equity; histograma de retornos; y el smile de IV vigente con
una curva teorica ajustada y las bases donde el bot efectivamente opero destacadas.

El apareo de compras/ventas (FIFO, por simbolo) y el calculo de PnL viven en
`dashboard/pnl_engine.py` — **leer el docstring de ese archivo antes de confiar en los
numeros**: la clasificacion de estrategia se deduce del simbolo (no es un campo
persistido), el Sharpe no esta anualizado, y ninguno de estos numeros reemplaza una
conciliacion contra el estado de cuenta real de tu ALYC.

Instalacion (una vez): `pip install -r requirements-dashboard.txt` (streamlit, plotly,
pandas, numpy — `run_dashboard.bat` lo hace por vos si falta).

### Dos bugs reales encontrados y corregidos (PnL inflado x100 y rehedge sin fin)

Reportado por el usuario: el dashboard mostraba un PnL Total de **~$1.670 millones ARS**
con un bot dimensionado para un capital de $1.000.000 — un numero fisicamente imposible.
La investigacion encontro DOS bugs reales distintos, uno de calculo (visual) y uno de
logica de trading (el serio de verdad):

1. **`dashboard/pnl_engine.py` aplicaba el multiplicador de OPCIONES (100) a TODOS los
   simbolos**, incluidas las patas de delta-hedge sobre el subyacente (acciones de GGAL,
   sin multiplicador de contrato) — inflando x100 el PnL de cada pata de hedge. Corregido
   con `multiplier_for_symbol()`: 1.0 para el subyacente/futuro, 100 para opciones. Ver
   `test_dashboard_pnl.py`, `test_match_trades_fifo_uses_multiplier_1_for_delta_hedge_legs`
   y los otros tests de regresion de este bug.
2. **El bug de verdad grave — `run_bot._maybe_hedge()` nunca registraba el fill de la
   orden de cobertura en `self.portfolio`**: como `needs_hedge()`/`execute_hedge()`
   deciden cuanto cubrir en base al delta total de la cuenta (`portfolio.total_greeks()`),
   y esa cobertura recien ejecutada nunca quedaba reflejada ahi, CADA ciclo siguiente
   volvia a ver el mismo delta "fuera de banda" y disparaba OTRA orden de cobertura
   identica — un rehedge sin fin, cada ~2-4s, sin limite. En la sesion real del usuario
   esto acumulo una posicion de ~38.000 acciones de GGAL en unos pocos minutos. **En modo
   real (no shadow) esto habria sido una posicion direccional descontrolada con dinero
   real** — el bug mas serio encontrado en todo este proyecto hasta ahora. Corregido:
   `_maybe_hedge()` ahora registra el fill como una `Position` del subyacente
   (`multiplier=1.0`, `greeks_per_unit=None` — la misma marca de "esto es el subyacente,
   no una opcion" que ya usaba `portfolio.py`), asi el delta total converge dentro de la
   banda en vez de reabrirse indefinidamente. `_capital_available_ars()` se ajusto en
   paralelo para excluir esa posicion del "capital comprometido" de opciones (son
   presupuestos de riesgo separados). Ver `test_execution_pipeline.py`,
   `test_maybe_hedge_records_fill_so_delta_reflects_the_hedge`.

**Si corriste el bot antes de esta correccion**: revisa `state/bot_state.json` y
`logs/shadow_trades.csv` — cualquier posicion de delta-hedge acumulada de esa manera es
ficticia (una consecuencia del bug, no una posicion real que haya que deshacer en tu
ALYC), pero si el bot llego a correr alguna vez en ambiente LIVE con este bug presente,
hay que revisar la cuenta real del broker antes de asumir nada.

### Guardia de staleness de datos de mercado (`GGAL_BOT_MAX_DATA_STALENESS_SECONDS`)

Reportado por el usuario: un log real mostro varios minutos seguidos (10+) de timeouts
de conexion contra data912.com (`Max retries exceeded`, `Read timed out`). Cada poll
fallido individual ya se manejaba bien (se descarta ese ciclo puntual sin romper nada -
ver `Data912RestSource.fetch_snapshot()`), pero **no habia ningun control sobre cuanto
tiempo acumulado llevaba el bot sin un dato fresco**: si la caida se extiende, el bot
seguia calculando IV, griegas, dislocacion de smile y señales de entrada/salida contra
precios cada vez mas viejos, sin ninguna alerta mas alla del warning de cada poll
individual (facil de pasar por alto en un log largo).

Se agrego una guardia explicita en `run_bot.py` (`GgalOptionsBot._is_market_data_stale`):
se registra la marca de tiempo de la ultima actualizacion exitosa del SPOT de GGAL
(`_on_book_update`, tanto en modo Shadow via `poll()` como en modo real via websocket) y,
si pasan mas de `GGAL_BOT_MAX_DATA_STALENESS_SECONDS` (default **60s**, conservador)
desde esa marca, `_run_weekly_asymmetric_cycle()`:

- **deja de generar ENTRADAS nuevas y de completar spreads** ese ciclo (no se toma
  exposicion nueva contra un precio potencialmente desactualizado), y
- loguea una **alerta explicita** (`ALERTA: datos de mercado con Ns de antiguedad...`),
  una unica vez al entrar en el estado stale (no en cada ciclo de ~2-4s mientras dura la
  caida) y otra vez al recuperarse.

**Decision deliberada**: las SALIDAS (Stop Loss/Take Profit/horizonte semanal/guardia de
fin de semana) y el delta-hedger automatico **siguen activos** durante la caida, usando
la ultima punta conocida. La alternativa (congelar todo, incluidas las salidas) es mas
conservadora en apariencia pero mas riesgosa en la practica: deja una posicion que
deberia cerrar por Stop Loss completamente sin vigilancia mientras dura la caida de
datos, que es justamente el escenario que mas conviene poder seguir gestionando. Esto es
una guardia de **calidad de dato**, no de riesgo de mercado: no reemplaza al `RiskManager`,
lo complementa evitando que se tome exposicion nueva sobre informacion vieja.

Ver `ggal_bot/validation/test_execution_pipeline.py` (5 tests: marcado de la marca de
tiempo, calculo de antiguedad, umbral inclusivo/exclusivo) y
`test_strategy_selector.py::test_weekly_asymmetric_cycle_skips_entries_and_spreads_when_market_data_stale`
(integracion: la salida se reconcilia igual, la entrada candidata NO se abre).

### El bot queda "trabado" varios minutos (root cause real: timeout de red que no se respeta)

Reportado por el usuario: en modo Shadow, el bot dejaba de mostrar actividad en el log
durante 7-15 minutos seguidos, con un unico warning de `Data912RestSource.fetch_snapshot()`
al final de cada ventana muerta - pese a que el timeout configurado
(`GGAL_BOT_SHADOW_REQUEST_TIMEOUT`) es de apenas 5 segundos.

**Root cause**: el `ConnectTimeoutError`/`ReadTimeoutError` que reporta `requests` dice
`timeout=5.0`, pero en Windows la resolucion de nombre (`getaddrinfo()`, DNS) puede quedar
bloqueada a nivel de sistema operativo mucho mas alla de ese valor (VPN, DNS corporativo,
drivers de red particulares) - el timeout de `requests`/`urllib3` se aplica sobre el
socket ya creado, no necesariamente cubre la fase de resolucion de nombre en si. El
resultado: la excepcion que finalmente aparece en el log es correcta, pero tarda
**minutos** en levantarse en vez de los 5 segundos nominales - y como
`Data912RestSource.fetch_snapshot()` se llama de forma sincronica dentro del mismo hilo
del ciclo principal, **todo** `recompute_cycle()` (IV/griegas, señales, riesgo, hedge,
vigilancia de ordenes, persistencia de estado) queda bloqueado esperando esa unica
llamada, no solo el poll de datos.

**La correccion**: nuevo modulo `ggal_bot/data/http_utils.py` (`http_get_json()`), usado
tanto por `Data912RestSource` (datos en vivo) como por `Data912DailyBarsSource` (historico
1D del filtro de tendencia) en vez de llamar a `requests.get()` directamente. Corre la
llamada de red en un hilo de un pool compartido y la espera con su PROPIO timeout
(`concurrent.futures.Future.result(timeout=...)`, timeout configurado + 2s de margen) -
ese `.result(timeout=)` si corta de verdad al nivel de Python, sin importar cuanto tarde
la llamada bloqueada en resolverse por su cuenta a nivel de sistema operativo. El hilo
bloqueado sigue vivo en el pool hasta que el SO finalmente le devuelva el control (no hay
forma portable de matarlo a la fuerza en Python puro), pero el bot ya no espera por el:
el ciclo principal sigue con el proximo poll casi de inmediato, y la guardia de staleness
de arriba se encarga de pausar entradas nuevas si la falta de datos frescos se sostiene.

Ver `ggal_bot/validation/test_http_utils.py` (4 tests: caso exitoso, excepcion rapida sin
convertirse en timeout propio, timeout duro cuando la llamada subyacente se cuelga mas
alla de su propio timeout nominal, `requests` no instalado).

## Modo "Long-First / Weekly Asymmetric" (sin posiciones descubiertas)

Modo operativo **activo por defecto** en `run_bot.py`, alternativo al arbitraje de
volatilidad delta-neutral original (`strategy/vol_arbitrage.py`, que sigue existiendo
sin cambios y sigue disponible via configuracion). Este modo esta pensado para un ALYC
que **no permite venta en descubierto** de calls/puts, con horizonte de tenencia maximo
semanal y sizing dinamico por capital. Vive en tres archivos:

- **`ggal_bot/strategy/weekly_asymmetric.py`** (`WeeklyAsymmetricStrategy`): genera
  *unicamente* señales de compra (`buy_to_open`) sobre bases baratas (IV por debajo de
  la curva de smile) dentro del horizonte semanal y de una banda de moneyness ATM/OTM
  cercana, rankeadas por un score de convexidad (`|gamma| + |vega|/100`, por peso de
  prima pagada). La pata corta de un spread (Bull Call Spread / Bear Put Spread) **solo**
  se arma si el portafolio ya tiene una posicion larga confirmada (`quantity > 0`) en esa
  base especifica — esto es una invariante de codigo, no solo de intencion: sin esa
  posicion previa, no existe ninguna ruta en el modulo que genere una venta sobre esa
  base. Las salidas (Stop Loss / Take Profit / vencimiento del horizonte semanal /
  guardia de fin de semana) se calculan aparte, en `risk_manager.py`.
- **`ggal_bot/risk/position_sizer.py`** (`PositionSizer`): calcula la cantidad de
  contratos como `floor(capital_asignado_al_trade / (prima * multiplicador))`, con
  `capital_asignado_al_trade = capital_disponible * max_risk_pct_per_trade`, nunca por
  encima del techo `max_capital_ars` configurado.
- **`ggal_bot/risk/risk_manager.py`** (`evaluate_position_exit`): unica fuente de verdad
  de "cuando forzar el cierre" de una posicion larga — Stop Loss y Take Profit medidos
  sobre el PnL% de la prima, vencimiento del horizonte de `max_holding_business_days`
  ruedas habiles, y una guardia de fin de semana que fuerza el cierre los viernes si la
  posicion vence despues de ese viernes (para no quedar expuesto al theta de 2-3 dias
  corridos sin ninguna rueda para reaccionar).

Toda la configuracion vive en `LongFirstConfig` (`ggal_bot/config.py`) y se controla por
variables de entorno (ver la seccion nueva de `.env.example`): capital maximo
(`GGAL_BOT_MAX_CAPITAL_ARS`, default $1.000.000), riesgo maximo por trade
(`GGAL_BOT_MAX_RISK_PCT_PER_TRADE`, default 20%), objetivo de retorno semanal
(`GGAL_BOT_WEEKLY_TARGET_ARS`), horizonte maximo en ruedas habiles
(`GGAL_BOT_MAX_HOLDING_BUSINESS_DAYS`, default 5), Stop Loss / Take Profit
(`GGAL_BOT_STOP_LOSS_PCT` / `GGAL_BOT_TAKE_PROFIT_PCT`), y los umbrales de
smile/moneyness que definen "barata" y la banda ATM/OTM buscada.

**Nota de riesgo, importante antes de tocar esto con capital real**: el objetivo de
retorno semanal (`GGAL_BOT_WEEKLY_TARGET_ARS`, por defecto igual al capital total, es
decir 100% semanal) es un **parametro de dimensionamiento** para calibrar cuanta
convexidad busca el bot por trade — **no es una proyeccion ni una garantia de
resultado**. Perseguir un 100% de retorno semanal implica, por construccion
matematica, arriesgar una fraccion grande del capital (hasta el `max_risk_pct_per_trade`
configurado) en estructuras que pueden perder la totalidad de la prima pagada si la
volatilidad esperada no se materializa. Nada en este modulo estima la probabilidad de
alcanzar ese objetivo — eso depende del mercado, no de la configuracion. Se recomienda
validar este modo en Shadow Trading (ver seccion de arriba) durante varios ciclos
semanales completos antes de considerar capital real.

### Seleccion de estrategia activa (`GGAL_BOT_ACTIVE_STRATEGY`)

`run_bot.py` arranca la estrategia que indique `GGAL_BOT_ACTIVE_STRATEGY` en `.env`
(ver `ggal_bot/config.py StrategyConfig`):

```bash
GGAL_BOT_ACTIVE_STRATEGY=weekly_asymmetric   # DEFAULT: Long-First / Weekly Asymmetric
GGAL_BOT_ACTIVE_STRATEGY=vol_arbitrage       # arbitraje de volatilidad delta-neutral original
```

Un valor invalido (typo, vacio con otra cosa) no frena el arranque: cae a
`weekly_asymmetric` y `run_bot.py` deja una advertencia explicita en el log.

Bajo `weekly_asymmetric`, cada ciclo de `recompute_cycle()` sigue un orden deliberado
(ver `GgalOptionsBot._run_weekly_asymmetric_cycle()`):

1. **Salidas primero**: `RiskManager.evaluate_position_exit()` se evalua sobre todo el
   portafolio (Stop Loss, Take Profit, horizonte semanal, guardia de fin de semana)
   *antes* de mirar señales de entrada nuevas. Esto libera capital comprometido en el
   mismo ciclo en que se cierra una posicion, para que el sizing de una entrada nueva
   ese mismo ciclo ya vea ese capital disponible (ver `_capital_available_ars()`).
2. **Entradas nuevas**: `WeeklyAsymmetricStrategy.scan_entry_signals()` + sizing
   dinamico via `PositionSizer.compute_contracts()` (nunca 1 contrato fijo).
3. **Completar spreads**: `scan_spread_completion_signals()` arma la pata corta de un
   Bull Call/Bear Put Spread unicamente sobre bases con una larga ya confirmada.

**Estado de la integracion**: los tres modulos de calculo y el wiring en `run_bot.py`
estan completos y cubiertos por `ggal_bot/validation/test_long_first_mode.py` (48
tests, logica de estrategia/sizing/exits/filtro de tendencia/momentum shift/
microestructura/compresion de vega en aislamiento) y
`ggal_bot/validation/test_strategy_selector.py` (6 tests, seleccion de estrategia y el
orden salidas-antes-que-entradas dentro de `run_bot.py`). El delta-hedger
(`strategy/delta_hedger.py`) sigue activo sin cambios en ambos modos — bajo
`weekly_asymmetric` esto puede no ser lo que se busca, ya que la filosofia de este modo
es justamente ACUMULAR delta direccional (convexidad), no neutralizarlo; evaluar si
conviene desactivar el rehedge automatico mientras se opera este modo (no se desactivo
por decision propia, ya que no fue parte de lo pedido).

### Filtro de Tendencia 1D (Analisis Tecnico obligado, `ggal_bot/data/technical_analysis.py`)

Antes de evaluar cualquier compra de opciones o armado de spread, el modo
`weekly_asymmetric` exige una lectura de la tendencia principal de GGAL en el grafico
**diario (1D)**, calculada sobre 100-200 velas OHLCV reales (data912.com, con fallback a
un generador sintetico local si no hay red — mismo patron que Shadow Trading) o
sinteticas (para tests). El motor calcula EMA(20), EMA(50), RSI(14), ADX(14) y
MACD(12,26,9) con indicadores puros en Python (sin pandas/numpy, para no engordar el
`.exe` — ver `build_exe.bat`) y clasifica:

| Tendencia | Condicion | Efecto en `weekly_asymmetric.py` |
|---|---|---|
| **BULLISH** | Cierre > EMA20 > EMA50, ADX > 20, histograma MACD > 0 | Solo se consideran Calls (Long Call / Bull Call Spread) |
| **BEARISH** | Cierre < EMA20 < EMA50, ADX > 20, histograma MACD < 0 | Solo se consideran Puts (Long Put / Bear Put Spread) |
| **NEUTRAL** | Ninguna de las dos anteriores (lateral o sin fuerza de tendencia) | Solo entra con dislocacion de smile **extrema** (umbral normal × `GGAL_BOT_TA_NEUTRAL_EXTREME_MULT`, default 2x) y **nunca** completa spreads |

La tendencia se computa en `TechnicalAnalysisEngine` (con cache propio de
`GGAL_BOT_TA_REFRESH_SECONDS`, default 1 hora — las velas 1D no cambian intra-dia, no
tiene sentido pegarle a la red en cada ciclo de ~2s del bot) y se inyecta como
parametro explicito (`trend=`) en `scan_entry_signals()` /
`scan_spread_completion_signals()`: `weekly_asymmetric.py` nunca hace I/O ni calcula la
tendencia por su cuenta, siguiendo el mismo patron de inyeccion que el `now` de
`evaluate_position_exit()` — esto es lo que lo mantiene trivialmente testeable con
datos sinteticos. `GGAL_BOT_TECHNICAL_FILTER_ENABLED=false` desactiva el filtro por
completo (comportamiento identico al de antes de este modulo). Ver
`ggal_bot/validation/test_technical_analysis.py` (26 tests: indicadores, clasificacion,
fuente de datos con fallback, cache del motor, Momentum Shift) y las 11 pruebas de
integracion en `test_long_first_mode.py` (filtro aplicado sobre `scan_entry_signals`/
`scan_spread_completion_signals`, 4 de ellas especificas del Momentum Shift Override
de abajo).

**Disclosure de quant, importante**: el MACD de este modulo se calcula sobre **precio
crudo** (no logaritmico), tal como lo especifica la formula estandar. Sobre una caida
**sostenida a tasa constante** (sin aceleracion), esto produce — por una propiedad
matematica real del indicador, no un error de implementacion — un histograma MACD que
tiende a **positivo** en regimen estacionario (la EMA de una exponencial decreciente es
proporcional al precio, asi que la linea MACD converge monotonicamente a cero desde
abajo de su propia señal). En la practica esto significa que la condicion BEARISH exige,
de hecho, una caida con momentum **creciente** (aceleracion), mientras que BULLISH se
satisface con una suba sostenida a tasa constante — es decir, el filtro es
estructuralmente mas facil de activar en BULLISH que en BEARISH bajo una tendencia
"pareja". Esto esta verificado explicitamente en
`test_compute_technical_snapshot_constant_rate_decline_is_not_bearish` (ver
`test_technical_analysis.py`) y documentado aca en vez de "corregido" silenciosamente,
porque la especificacion pedida es implementar la formula estandar tal cual, no una
variante sobre log-precio. Como con cualquier lectura tecnica: **es una descripcion de
la estructura reciente de precios, no una prediccion** — sirve para evitar comprar Puts
en medio de una tendencia alcista fuerte (o viceversa), no para garantizar que la
direccion elegida sea la correcta.

### Momentum Shift / Early Reversal Override (relaja el filtro sin eliminarlo)

**Motivo (feedback directo del usuario, 2026-08)**: el filtro BULLISH/BEARISH de
arriba es, por construccion, un filtro de **estructura ya confirmada** — EMA20/EMA50
recien cruzan varias ruedas *despues* de que el nuevo regimen arranco. Eso significa
que, tal cual estaba, el bot siempre iba a llegar tarde a un cambio de tendencia: se
perdian movimientos aprovechables y las entradas quedaban condicionadas a que la
tendencia diaria terminara de girar del todo. La correccion pedida NO fue "sacar el
filtro" (seguia siendo un requisito mandatorio de este mismo proyecto), sino encontrar
una forma de relajarlo puntualmente sin resignar la disciplina de tendencia.

**El mecanismo**: `TechnicalAnalysisEngine` calcula, ademas de `trend`, una señal
adicional `momentum_shift` (ver `data/technical_analysis.py:MomentumShift`) basada en
RSI(14) — elegido en vez de, por ejemplo, la pendiente del histograma MACD, por ser
**adimensional y acotado (0-100)**: GGAL viene con un fuerte re-precio nominal por
depreciacion del ARS a lo largo de los años, asi que comparar "puntos de MACD" de una
etapa historica contra otra no es comparable, mientras que RSI si lo es. Si el RSI se
movio `GGAL_BOT_TA_MOMENTUM_RSI_DELTA` puntos o mas (default 8.0) **en contra** de la
tendencia vigente en las ultimas `GGAL_BOT_TA_MOMENTUM_LOOKBACK_BARS` velas (default 3),
se marca una reversion temprana:

| Tendencia vigente | Momentum Shift detectado | Efecto en `scan_entry_signals()` |
|---|---|---|
| BEARISH | `EARLY_BULLISH_REVERSAL` (RSI subio con fuerza) | Las Calls dejan de descartarse de plano — se vuelven a evaluar, exigiendo el umbral **extremo** de dislocacion de smile (el mismo de NEUTRAL), nunca el normal |
| BULLISH | `EARLY_BEARISH_REVERSAL` (RSI cayo con fuerza) | Simetrico: los Puts se vuelven a evaluar bajo el umbral extremo |

Puntos importantes de diseño:

- **No es "sacar el filtro"**: el tipo de opcion contrario a la tendencia sigue
  bloqueado salvo que (a) el momentum ya haya girado con fuerza en esa direccion Y
  (b) la dislocacion de smile sea extrema. Se agrega un gatillo adicional (momentum),
  no se resigna disciplina de tendencia.
- **Alcance deliberadamente limitado a entradas nuevas**: `scan_spread_completion_signals()`
  (armado de la pata corta de un spread) se dejo **sin cambios**, estrictamente alineado
  a `trend` — el reclamo que motivo este mecanismo fue especificamente sobre entradas
  tardias, no sobre el armado de spreads sobre una posicion ya confirmada.
- **`get_daily_trend_signal()` no cambia**: sigue devolviendo el literal
  BULLISH/BEARISH/NEUTRAL pedido originalmente. `momentum_shift` es un campo **aditivo**
  en `TechnicalSnapshot`, nunca un reemplazo de esa clasificacion.
- **Apagable en dos niveles**: `GGAL_BOT_TA_ENABLE_MOMENTUM_OVERRIDE=false` desactiva
  el mecanismo completo (se comporta identico a como estaba antes de este agregado);
  independientemente, `GGAL_BOT_TECHNICAL_FILTER_ENABLED=false` sigue desactivando todo
  el modulo de Analisis Tecnico (trend y momentum shift juntos).
- **Disclosure honesto**: esto reduce el problema de "llegar tarde", no lo elimina. Un
  RSI que gira 8 puntos no garantiza que la reversion sea real ni sostenida — sigue
  siendo, como el resto del filtro tecnico, una lectura de estructura/momentum reciente,
  no una prediccion. Y por la propia sensibilidad del histograma MACD (ver disclosure de
  arriba), en la practica un movimiento de precio lo bastante fuerte como para mover el
  RSI 8+ puntos suele tambien empezar a mover el histograma MACD — en muchos casos la
  tendencia ya habra pasado a NEUTRAL (que de por si ya exige solo el umbral extremo,
  sin distincion de tipo de opcion) antes de que el Momentum Shift llegue a activarse
  sobre un BULLISH/BEARISH todavia vigente; el mecanismo sigue siendo util como
  gatillo explicito y auditable, pero no hay que esperar que dispare en cada reversion.

## Modo "Scalping Intradia y Trading Semanal de Corto Plazo" (ADITIVO)

A pedido explicito del usuario (2026-09-03), se agrego un modo de scalping intradia
para el mismo universo de opciones de GGAL. **Decision de arquitectura central, leer
antes de tocar `GGAL_BOT_ACTIVE_STRATEGY` o este modulo**: este modo **no** es un valor
mas de `GGAL_BOT_ACTIVE_STRATEGY` (que sigue aceptando unicamente `weekly_asymmetric` o
`vol_arbitrage`, mutuamente excluyentes entre si). En cambio, es un modulo **bolt-on**
gateado por su propio flag independiente, `GGAL_BOT_ENABLE_SCALPING` (default `false`):
cuando esta prendido, `GgalOptionsBot._run_scalping_cycle()` corre **siempre despues**
del ciclo de la estrategia principal, en la **misma** `recompute_cycle()`, con su propio
capital, sus propias posiciones y sus propias reglas — nunca en lugar de la estrategia
principal.

El motivo de esta decision: si "scalping" fuera un valor mas del selector de estrategia,
activarlo **reemplazaria** a `weekly_asymmetric` por completo, apagando la gestion (Stop
Loss/Take Profit/horizonte semanal/guardia de fin de semana) de cualquier posicion ya
abierta bajo ese modo — en particular, la posicion viva con vencimiento forzado
(`GGAL_BOT_FORCE_EXPIRY`) que ya pueda existir en produccion. Con el diseño bolt-on, esa
posicion sigue gestionada exactamente igual, linea por linea, este o no activado el
scalping.

### Aislamiento entre estrategias (`Position.strategy_tag`)

Ambos modos comparten el mismo `Portfolio` (y por lo tanto el mismo calculo de griegas
totales/delta-hedge, que es correctamente **global** a toda la cuenta). Para que nunca se
pisen entre si, cada posicion queda marcada con `Position.strategy_tag` (`None`/
`"weekly_asymmetric"` para el modo original, `"scalping"` para este modo nuevo):

- `WeeklyAsymmetricStrategy.build_exit_signals()`/`scan_spread_completion_signals()`
  **solo** evaluan/cierran posiciones marcadas `"weekly_asymmetric"` (o sin marca, el
  caso de toda posicion abierta antes de que este campo existiera).
- `ScalpingStrategy.build_exit_signals()` **solo** evalua/cierra posiciones marcadas
  `"scalping"`.
- `GgalOptionsBot._capital_available_ars(strategy_tag)` calcula el capital comprometido
  **por separado** para cada estrategia — el pool de capital de una nunca reduce el de
  la otra.
- La unica guarda **compartida** (deliberadamente) es "no abrir dos posiciones sobre la
  misma base a la vez" (`_position_quantity`), independientemente de que estrategia la
  pida.

### Diferencias respecto de Long-First / Weekly Asymmetric

| | Weekly Asymmetric | Scalping (nuevo) |
|---|---|---|
| Horizonte de **elegibilidad** de entrada | `GGAL_BOT_MAX_HOLDING_BUSINESS_DAYS` (dias habiles) | `GGAL_BOT_SCALPING_MAX_HOLDING_BUSINESS_DAYS` (dias habiles, mas corto) |
| Horizonte de **salida** de una posicion abierta | mismo horizonte semanal + guardia de fin de semana | `GGAL_BOT_SCALPING_MAX_HOLDING_MINUTES` — **minutos**, no dias |
| Tendencia direccional | Analisis Tecnico 1D (EMA/RSI/MACD/ADX diario) | Multi-timeframe intradia (5m/15m por defecto, ver abajo) — exige acuerdo entre ambos timeframes |
| Cierre de Fin de Dia | no aplica (sostiene posiciones varios dias por diseño) | obligatorio, `GGAL_BOT_SCALPING_EOD_CLOSE_TIME` (hora ART) |
| Salida por falta de progreso | no existe | si: `GGAL_BOT_SCALPING_PROGRESS_CHECK_MINUTES`/`GGAL_BOT_SCALPING_MIN_PROGRESS_PNL_PCT` |
| Salida por reversion de IV | no existe (solo compresion de vega) | si: z-score de la dislocacion propia de cada base, ver abajo |
| Filtro de microestructura | Order Book Imbalance | OBI + profundidad minima de ASK (`GGAL_BOT_SCALPING_MIN_ASK_SIZE`) |
| Spreads (Bull Call/Bear Put) | si (`scan_spread_completion_signals`) | **no** — solo Long Call/Long Put desnudas, deliberadamente mas simple |
| Capital | `GGAL_BOT_MAX_CAPITAL_ARS` (pool propio) | `GGAL_BOT_SCALPING_MAX_CAPITAL_ARS` (pool propio, separado) |
| Concurrencia | sin limite explicito de posiciones simultaneas | `GGAL_BOT_SCALPING_MAX_CONCURRENT_POSITIONS` — reparte el capital en mas trades de menor tamaño |

### Velas intradia sintetizadas en memoria (`ggal_bot/data/intraday_bars.py`)

Ninguna fuente disponible (data912.com, IOL/BrokerRestSource) expone velas intradia de
GGAL — solo velas diarias y puntas en vivo. `IntradayBarAggregator` arma velas OHLC de
`N` minutos **localmente**, alimentado con el mismo spot que el bot ya consume cada
ciclo (no hace falta tick-by-tick real: la cadencia de ~2-5s del bot entra comoda en un
bucket de 5+ minutos). Reutiliza el dataclass `DailyBar` y `compute_technical_snapshot()`
de `data/technical_analysis.py` **sin modificarlos** — ver el docstring de ese modulo
para el porque es seguro (ninguna de las dos piezas depende de que `bar_date` sea unico).
`MultiTimeframeIntradayEngine` corre dos aggregators (rapido/lento) y solo confirma una
direccion si ambos timeframes coinciden (`GGAL_BOT_SCALPING_REQUIRE_MTF_AGREEMENT`) —
si no, NEUTRAL.

### Reversion de IV en alta frecuencia (`ggal_bot/data/iv_mean_reversion.py`)

Ademas del umbral fijo de dislocacion de smile (heredado de
`WeeklyAsymmetricStrategy.scan_entry_signals`, reusado por composicion — ver
`ScalpingStrategy`), `IVMeanReversionTracker` mantiene una ventana rodante de la
dislocacion de **cada base individual** y calcula su z-score. La salida
`scalping_iv_mean_reversion` dispara cuando ese z-score vuelve a acercarse a 0: la
mispricing que motivo la entrada ya se corrigio hacia el comportamiento reciente de esa
base en particular, sin esperar a que Stop Loss/Take Profit/horizonte lo fuercen por
otro motivo.

### Interaccion con `GGAL_BOT_FORCE_EXPIRY` (importante si activas Scalping en un
servicio que ya tiene un vencimiento forzado)

`BrokerRestSource._refresh_near_the_money_quotes_unsafe()` (fuente `broker_rest`/IOL, ver
`ggal_bot/data/live_shadow_feed.py`) reparte un cupo LIMITADO de refrescos individuales de
bid/ask entre las expiraciones relevantes. Con `GGAL_BOT_FORCE_EXPIRY` seteado y Scalping
**apagado**, ese cupo se dedica ENTERO al vencimiento forzado (comportamiento sin cambios
respecto de antes de este modulo). Con Scalping **activo**, esa regla se amplia
automaticamente a la UNION de `[vencimiento forzado]` + las `expiries_ahead` expiraciones
mas proximas — si no fuera asi, el Scalping (que deliberadamente no respeta
`GGAL_BOT_FORCE_EXPIRY`, busca sus propias bases de corto plazo) nunca tendria bid/ask
fresco en ninguna expiracion y no podria generar ninguna señal. `weekly_asymmetric` sigue
mirando UNICAMENTE el vencimiento forzado para sus propias señales — este cambio solo
afecta que expiraciones tienen cotizacion individual fresca disponible, nunca que
expiracion puede operar cada estrategia. Ver
`test_broker_rest_source_near_the_money_refresh_includes_forced_expiry_plus_nearest_when_scalping_enabled`
en `test_shadow_trading.py`.

### Como activarlo

Ver la seccion completa de variables en `.env.example` (bloque
`GGAL_BOT_ENABLE_SCALPING`). Con el flag apagado (default), no hace falta tocar ninguna
otra variable — el bot se comporta exactamente igual que antes de este modulo. Tests:
`ggal_bot/validation/test_scalping_mode.py` (40 tests: aislamiento entre estrategias,
agregador de velas intradia, tracker de reversion de IV, exits de `RiskManager`, filtro
de profundidad de ASK, wiring en `run_bot.py`).

## Estrategia elegida: Hybrid Trend-Aligned Skew Reversion (razonamiento de diseño)

Esta seccion documenta la decision de arquitectura detras de la estrategia activa por
defecto (`weekly_asymmetric`) y por que, con criterio de Quant Lead, se descartaron
otras familias de estrategias para el mercado especifico de opciones de GGAL en BYMA.

**La estrategia en una frase**: comprar convexidad (Calls o Puts, nunca vender para
abrir) donde la sonrisa de volatilidad esta anormalmente barata respecto de sus
vecinas, solo en la direccion que confirma la tendencia diaria del subyacente, con
horizonte semanal, ejecucion pasiva a mid-price, sizing dinamico por capital, y dos
capas adicionales de confirmacion/salida (microestructura del libro y compresion de
vega) que se suman sobre ese chasis. Es, literalmente, la combinacion de **Volatility
Skew Arbitrage + Trend-Following con Spreads Asimetricos** de las familias que se
evaluaron — no una tercera alternativa exotica — porque es la que mejor calza con las
restricciones estructurales del mercado, detalladas abajo.

**Por que esta combinacion y no otra, para GGAL especificamente:**

- **Gamma Scalping puro (rehedgear delta continuamente contra una posicion larga de
  gamma) se descarta como estrategia primaria**: requiere re-hedgear el subyacente con
  alta frecuencia para monetizar la convexidad, lo cual en BYMA implica pagar comisiones
  y spread del contado en cada ajuste — en un ciclo de calculo de ~2-4s (no
  tick-by-tick) y sin colocation, el costo de transaccion de rehedgear seguido
  probablemente se come el edge de gamma antes de capturarlo. El delta-hedger
  (`strategy/delta_hedger.py`) sigue disponible y activo como control de riesgo (evita
  que el delta direccional acumulado se vaya de banda), pero no como fuente primaria de
  retorno.
- **Mean-Reversion de IV a nivel (vs. HV historica) como señal primaria se descarta**:
  ya existe como opcion secundaria (`require_level_confirmation`), pero por si sola no
  distingue "esta base especifica esta barata" de "todo el vencimiento esta barato
  porque el mercado tiene razon en no pagar mas prima" — el smile arbitrage (dislocacion
  relativa entre bases del MISMO vencimiento) es una señal mas robusta porque compara
  contra vecinos con el mismo riesgo de evento, no contra un promedio historico que
  puede estar desactualizado.
- **Momentum de microestructura pura (HFT-like, prediciendo el proximo tick a partir del
  order flow) se descarta como señal de alpha primaria**: el libro de opciones de GGAL
  es delgado (pocos participantes, sin un market maker continuo dedicado en cada base) y
  el ciclo de recalculo del bot es de segundos, no microsegundos — cualquier señal de
  microestructura de tan corto plazo se degrada a ruido en ese horizonte. Por eso el
  Order Book Imbalance se usa como **filtro de calidad de ejecucion** (ver mas abajo),
  no como predictor direccional.
- **Calibracion estocastica completa (SABR/SVI) del smile se descarta, deliberadamente,
  por ahora**: el ajuste cuadratico local que ya usa `volatility_surface.py` captura la
  curvatura (convexidad) del smile adecuadamente para bases ATM/OTM cercanas a
  vencimiento semanal, que es exactamente la zona donde opera este modo. El beneficio
  principal de SABR/SVI — extrapolacion arbitrage-free en los extremos del smile y
  across-vencimientos — importa mas en libros profundos con muchas bases liquidas por
  vencimiento; con la cadena real de GGAL (pocas bases con book operable por
  vencimiento, ver `diagnose_instruments.py`), el costo de implementar y validar una
  calibracion estocastica completa no se paga con una mejora de señal proporcional. Es
  una decision de alcance, no una limitacion tecnica — si la liquidez de la cadena
  mejora sustancialmente, vale la pena revisar esto.

**Lo que se agrego sobre el chasis existente (las dos mejoras concretas de esta
iteracion):**

1. **Confirmacion de microestructura — Order Book Imbalance** (`models/microstructure.py`,
   `GGAL_BOT_ENABLE_OBI_FILTER` / `GGAL_BOT_MIN_OBI_FOR_ENTRY`): antes de comprar una
   base barata, se descarta si el libro muestra un desbalance extremo hacia el lado
   vendedor (`OBI = (bid_size - ask_size) / (bid_size + ask_size)` por debajo del piso
   configurado). En un libro delgado como el de GGAL, un desbalance asi suele reflejar
   una punta aislada/iliquida mas que informacion de precio genuina — es un guardrail de
   calidad de ejecucion, no una señal de alpha (ver nota de alcance en el modulo).
2. **Salida por compresion de Vega** (`risk_manager.evaluate_vega_decay_exit`,
   `GGAL_BOT_ENABLE_VEGA_DECAY_EXIT` / `GGAL_BOT_VEGA_DECAY_EXIT_RATIO`): la tesis de
   este modo es exposicion a convexidad (gamma/vega), no direccion pura — si el `|vega|`
   de una posicion ya cayo por debajo de un porcentaje configurable (default 35%) del
   `|vega|` que tenia al momento de la entrada (comparando siempre contra
   `Position.greeks_per_unit`, congelado al fill), la tesis que motivo la compra ya se
   agoto aunque el PnL% de la prima todavia no dispare Stop Loss ni Take Profit. Esto
   complementa esas reglas — se evalua DESPUES y solo si nada mas disparo ya (ver
   `test_build_exit_signals_stop_loss_takes_priority_over_vega_decay`) — para no seguir
   pagando theta por una posicion que dejo de ser la apuesta de convexidad original.

Ambas mejoras estan cubiertas por tests dedicados (`test_microstructure.py`, 8 tests;
mas 8 tests de integracion nuevos en `test_long_first_mode.py`) y son individualmente
desactivables por config (`enable_obi_filter=false`, `enable_vega_decay_exit=false`)
para poder aislar su efecto en Shadow Trading antes de confiar en ellas con capital
real.

**Disclosure honesto — lo que este diseño NO puede prometer**: "mayor Sharpe Ratio
posible" es un objetivo de diseño, no un resultado medido. Ninguno de los cambios de
esta iteracion fue validado contra un backtest de fills historicos reales de la cadena
de opciones de GGAL (no existe ese dataset en este proyecto todavia) — el razonamiento
de arriba es cualitativo y basado en la estructura conocida del mercado (liquidez
delgada, ciclo de calculo de segundos, restriccion long-only), no en una medicion
empirica de Sharpe/drawdown/win-rate. Antes de operar esto con capital real, corresponde
validar en Shadow Trading durante varios ciclos semanales completos (ver seccion de
Shadow Trading arriba) y, si es posible, armar un backtest con datos historicos reales
de la cadena antes de confiar en cualquier metrica de retorno ajustado por riesgo.

## Iniciar el bot en Windows

**No corras `.\run_bot.py` directo en PowerShell/Explorador**: en Windows, ejecutar un
`.py` por su path usa la asociacion de archivos del sistema para esa extension (en
algunas maquinas eso abre un editor en vez de invocar Python), no el interprete. Hay dos
formas de arrancarlo que si funcionan:

- **`run_bot.bat`** (rapido, no requiere compilar nada): doble click desde el Explorador,
  o `.\run_bot.bat` desde PowerShell/CMD. Activa `.venv` si existe y llama a
  `python run_bot.py` explicitamente; deja la ventana abierta al terminar (incluso si
  hubo un error) para poder leer el log.
- **`build_exe.bat`** (genera un `.exe` standalone): corre `.\build_exe.bat` una vez para
  compilar `dist\GGAL_BOT.exe` con PyInstaller (instala PyInstaller solo si hace falta).
  El `.exe` resultante se puede mover, pinchear en la barra de tareas, o compartir sin
  necesitar Python instalado en la maquina destino. **Este build tiene que correr en
  Windows** (PyInstaller no compila para un sistema operativo distinto al que lo ejecuta),
  asi que no hay forma de generarte el `.exe` ya compilado desde otro lado — el script
  automatiza el proceso, pero el primer build lo tenes que hacer vos, una vez, en tu
  maquina. Los logs, `state\` y `logs\shadow_trades.csv` se crean al lado del `.exe`, no
  en la carpeta de codigo fuente.

## Instalacion

```bash
cd "GGAL BOT"
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
copy .env.example .env            # y completar PYROFEX_USER / PASSWORD / ACCOUNT
```

## Validar el motor cuantitativo (sin broker, recomendado antes que nada)

```bash
python -m ggal_bot.validation.test_quant_engine
```

Esto corre paridad put-call, convergencia del solver de IV, deteccion de dislocaciones
de smile, limites de riesgo y delta-hedger — todo con datos sinteticos, sin necesitar
conexion a mercado. Si algo falla aca, **no correr `run_bot.py` contra mercado real**.

Suite completa (230 tests, 10 archivos — incluye ejecucion/shadow (multi-fuente,
failover e IOL/BrokerRestSource con esquema confirmado incluidos)/dashboard/Long-First/
seleccion de estrategia/Analisis Tecnico (incluido Momentum Shift)/microestructura/
guardia de staleness de datos/timeout de pared real/modo Scalping ADITIVO):

```bash
for f in test_quant_engine test_execution_pipeline test_shadow_trading test_dashboard_pnl test_long_first_mode test_strategy_selector test_technical_analysis test_microstructure test_http_utils test_scalping_mode; do
  python -m ggal_bot.validation.$f
done
```

## Completar antes de operar en vivo

`run_bot.py` trae marcados con `TODO` los puntos que requieren tu integracion especifica
con el ALYC (listado real de instrumentos vigentes en `bootstrap_universe()`, armado y
envio de las ordenes delta-neutrales en `recompute_cycle()`). El motor de calculo
(IV, griegas, smile, riesgo, hedge) esta completo y probado; lo que falta es la
plomeria de ejecucion real contra tu cuenta.

**Siempre correr primero contra `PYROFEX_ENV=REMARKET`** (paper trading) durante al
menos un ciclo mensual completo, siguiendo el checklist de `docs/Diseno_Bot_Opciones_GGAL.md`,
antes de pasar a `LIVE`.

## Calibracion

Los limites por defecto en `ggal_bot/config.py` (banda de delta ±150, vega/gamma
maximos, umbrales de señal de smile) son ilustrativos. Deben ajustarse al tamaño real
de la cuenta y a la volatilidad reciente de GGAL antes de operar con capital real.

## Auditoria maestra y Fase 1 del roadmap hacia Go/No-Go

Ver `docs/AUDITORIA_MAESTRA_2026-08-27.md` para la auditoria completa del proyecto
(arquitectura, matematica, riesgo, EV, backtesting, criterios de Go/No-Go). La
**Fase 1** de su roadmap (correccion de bugs P0 + aislamiento de tests) esta
**completa** desde 2026-08-31:

- **Bug de theta corregido** (`models/black_scholes.py:theta()`): mezclaba el
  reloj de dias habiles (252) con el de dias corridos (365), sobreestimando la
  magnitud ~12%. Ver test de regresion contra diferencia finita en
  `test_quant_engine.py`.
- **Bug de umbral minimo del smile corregido** (`models/volatility_surface.py`):
  con exactamente 3 strikes validos, el ajuste cuadratico interpolaba
  perfectamente y `smile_dislocation()` daba ~0 sin importar el mispricing real.
  Ahora se exige un minimo de 5 puntos para la cuadratica; con 3-4 puntos se cae
  a un ajuste lineal (menos sensible, pero con residuo real). Ver los tres tests
  nuevos de `smile_dislocation_*` en `test_quant_engine.py`.
- **Contaminacion de `logs/shadow_trades.csv` corregida**: `OrderGateway` ahora
  acepta un `shadow_audit_path` inyectable, `GgalOptionsBot` acepta un
  `order_gateway` inyectable, y `ggal_bot/validation/_shadow_audit_isolation.py`
  (importado por cada archivo de test relevante, mas un `conftest.py` para
  pytest) redirige el CSV de auditoria a un directorio temporal durante toda
  corrida de tests — ninguna corrida de la suite vuelve a escribir sobre el CSV
  real de produccion.
- **Logs historicos archivados**: el `logs/shadow_trades.csv` previo (generado
  por versiones del codigo con los bugs de re-entrada/rehedge infinito ya
  corregidos, mas contaminacion de tests) se movio a
  `logs/archive/shadow_trades_CONTAMINADO_pre_fix_2026-08-27.csv` y se empezo un
  archivo limpio. **Cualquier fila desde este punto en adelante es la primera
  evidencia real y confiable de shadow trading con el codigo actual** — antes de
  esto, no existia ningun track record utilizable (ver seccion 3.4/3.5 de la
  auditoria).
- **Bug de clasificacion de simbolo del dashboard corregido**
  (`dashboard/pnl_engine.py`): un fill con el simbolo corto del subyacente
  ("GGAL" a secas, sin el ticker completo calificado) caia por error al
  multiplicador de opciones (100). Ahora se compara contra un conjunto de alias
  conocidos del subyacente, no un unico string exacto.

Suite completa: 172/172 tests en verde despues de estos cambios (168 + 1 test de
theta + 2 tests nuevos de smile − 1 test viejo con nombre enganoso reemplazado, +
1 test de clasificacion de simbolo del dashboard).

Quedan pendientes de la Fase 1, todavia no abordados: kill-switch de perdida
diaria/semanal a nivel de cuenta, y calculo explicito de Expected Value/break-even
(ver Fases 3-4 del roadmap en la auditoria).
