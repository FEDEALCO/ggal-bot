# Auditoría Maestra y Reingeniería — GGAL BOT
**Fecha:** 2026-08-27 · **Alcance:** todo el código fuente actual del repositorio, el diseño documentado (`docs/Diseno_Bot_Opciones_GGAL.md`, `README.md`), la suite de tests (168 tests), y los registros reales de shadow trading tanto en la copia de este sandbox como en la máquina del usuario.

## 0. Nota metodológica (léase antes que el resto)

Esta auditoría se hizo leyendo el código fuente real, línea por línea, en los módulos críticos (motor cuantitativo, riesgo, ejecución, estrategia, datos, dashboard), más tres revisiones independientes en paralelo sobre submódulos específicos, más verificación manual directa de:

- El archivo `logs/shadow_trades.csv` **de este sandbox** (94 filas).
- El archivo `logs/shadow_trades.csv` **real, de tu máquina** (133 filas) — este es el que importa.
- Grep exhaustivo de todo el repositorio buscando ML, backtesting, EV, kill-switch, hipótesis estadísticas.

Todo hallazgo de esta auditoría cita archivo y línea. Donde algo no se pudo verificar completamente (por ejemplo, el esquema exacto en vivo de un endpoint externo), se marca explícitamente como **no verificado**, nunca se asume. No se inventó ningún resultado, número o experimento que no exista en el repositorio.

**Primer hallazgo, y el más importante para calibrar todo lo que sigue**: el proyecto que existe hoy **no es** un sistema de Machine Learning con features, targets, entrenamiento, walk-forward validation e hipótesis estadísticas sobre miles de trades. Es un **bot de reglas** (rule-based) de arbitraje de volatilidad / smile skew sobre opciones de GGAL, con un motor cuantitativo (Black-Scholes, IV, griegas, superficie de volatilidad) matemáticamente explícito, sin ningún componente de ML, sin backtest histórico, y con exactamente **cero trades reales o de paper trading limpios** registrados hasta la fecha (ver §2). Muchas secciones del pedido original de auditoría (leakage de features, drift de un modelo, tablas de hipótesis con p-values y AUC, `breakout_intensity`, deflated Sharpe ratio) **no aplican** porque ese sistema no existe en este repositorio. Esto se declara explícitamente en cada sección relevante en lugar de rellenar con contenido inventado.

---

## 1. RESUMEN EJECUTIVO

1. GGAL BOT es un bot de opciones sobre GGAL (BYMA) basado en reglas explícitas de arbitraje de volatilidad (smile skew), no en Machine Learning. No hay features, target, modelo, ni entrenamiento en ningún lugar del repositorio — confirmado por ausencia, no asumido.
2. El motor cuantitativo (Black-Scholes, IV solver, HV, superficie de volatilidad, griegas de portafolio) está, en general, bien construido y con una separación de responsabilidades limpia — pero tiene un bug de cálculo real en `theta()` (~12% de sobreestimación, ver §3.1) y un bug de diseño en el fit del smile que anula la señal principal justo en el caso más común (exactamente 3 strikes válidos, ver §3.2).
3. La estrategia activa por defecto es `weekly_asymmetric` (long-only, sin venta en descubierto), seleccionada por un simple flag de configuración (`GGAL_BOT_ACTIVE_STRATEGY`), no por detección de régimen. La estrategia alternativa (`vol_arbitrage`, delta-neutral) sigue existiendo pero no está en uso.
4. **No existe ningún backtest histórico en este proyecto** (confirmado por grep exhaustivo). Toda "validación" hasta hoy es: (a) tests unitarios que verifican que las fórmulas matemáticas son correctas contra casos calculados a mano, y (b) un puñado de sesiones cortas de Shadow/paper trading en vivo.
5. **El historial de shadow trading existente no sirve como evidencia de nada.** El archivo `logs/shadow_trades.csv` de tu máquina real tiene 133 filas: 4 son contaminación de un test unitario mal aislado, y las 129 restantes (25-26 de agosto) fueron generadas por versiones del código que tenían **dos bugs de re-entrada infinita ya corregidos** (ver §3.4/§3.5) — es decir, ni siquiera reflejan el comportamiento del código actual. Con el código de hoy, el track record real es: **cero filas útiles.**
6. No existe cálculo explícito de Expected Value (EV = P×ganancia − (1−P)×pérdida − costos) en ningún punto de la lógica de señales o de sizing. Las entradas se filtran por umbral de dislocación de smile (en "vol points"), no por una EV calculada.
7. No existe kill-switch ni límite de pérdida diaria/semanal a nivel de cuenta. Sólo hay stops por posición individual (Stop Loss -50%, Take Profit +100%, horizonte semanal, guardia de fin de semana, salida por compresión de vega).
8. El sizing es fracción fija de capital (20% del capital disponible por trade, redondeado hacia abajo a contratos enteros) — no es Kelly, no está basado en probabilidad ni en EV.
9. Los fills de shadow/paper trading llenan siempre al 100% y al precio de referencia (mid), sin comisión ni slippage modelados — el paper trading actual es sistemáticamente optimista respecto de lo que pasaría en vivo, especialmente en los hedges agresivos (que en la vida real cruzan spread y acá llenan al mid).
10. El proyecto **sí** tiene un historial real y verificable de bugs de producción encontrados y corregidos con disciplina: re-entrada infinita de la misma señal (corregido), cobertura de delta que nunca se registraba en el portafolio → rehedge infinito (corregido, con nota explícita "en modo real esto habría sido una posición direccional descontrolada con dinero real"), multiplicador x100 incorrecto en el PnL del dashboard (corregido), timeout de DNS bajo Windows/VPN (corregido), y el timeout de IOL `/Opciones` bajo carga de mercado (corregido hoy mismo). Esto es una señal positiva de proceso — el equipo encuentra y corrige bugs reales vía uso — pero también significa que el sistema todavía está en una fase de estabilización de ejecución, no de validación de edge.
11. **Bug confirmado y aún no corregido**: `OrderGateway()` no acepta un path de auditoría por constructor; casi todos los tests que corren en modo shadow escriben sobre el CSV de producción real (`logs/shadow_trades.csv`) en lugar de un archivo temporal. Esto ya contaminó el log real de tu máquina (4 filas) y contaminó por completo la copia de este sandbox (94/94 filas). Mientras no se arregle, cualquier corrida futura de la suite de tests volverá a ensuciar el registro real.
12. El dashboard de PnL (`dashboard/pnl_engine.py`) está, en general, bien construido (FIFO correcto, drawdown correcto, Sharpe correctamente etiquetado como "aproximado, sin anualizar"), pero tiene un bug de clasificación: si el símbolo del subyacente llega como `"GGAL"` en lugar del ticker completo configurado (`"MERV - XMEV - GGAL - 24hs"`), se le aplica por error el multiplicador de opciones (100) — exactamente el mismo bug de clase que ya se había corregido para el ticker canónico.
13. El filtro de tendencia técnica (EMA20/EMA50/ADX/MACD/RSI) está implementado sin look-ahead bias en la indexación (verificado línea por línea) — el riesgo real está en el límite con la fuente de datos externa: no hay ningún filtro que excluya la vela del día en curso (potencialmente todavía "viva"/no cerrada) de la serie usada para calcular EMA/RSI, lo cual podría hacer que la lectura de tendencia "parpadee" intradía. No se pudo verificar en vivo el esquema exacto de la respuesta de data912 para confirmar si esto ocurre realmente.
14. No existe ningún límite de posiciones concurrentes, concentración o correlación — mitigado en parte porque GGAL es el único subyacente operado, pero nada impide acumular exposición en muchas bases/strikes simultáneamente hasta agotar el capital.
15. La "tesis de edge" (arbitraje de smile: comprar la base cuya IV cruda esté anormalmente barata respecto de sus vecinas del mismo vencimiento) está razonada de forma coherente en el propio diseño del proyecto y en el propio README ("Estrategia elegida: Hybrid Trend-Aligned Skew Reversion"), incluyendo por qué se descartaron alternativas (gamma scalping puro, mean-reversion de nivel, microestructura pura, SABR/SVI completo). Es una tesis **razonable**, pero — y esto lo dice el propio README con honestidad poco común — **no está validada empíricamente contra ningún dato histórico real.** Esta auditoría confirma que esa autocrítica del propio equipo es correcta y no encuentra ninguna evidencia adicional que la contradiga en ningún sentido (ni a favor ni en contra del edge).
16. El motor de datos multi-fuente (Primary/pyRofex, data912 REST, IOL/InvertirOnline REST, Mock/Replay) con failover automático, construido e integrado esta misma semana, es la pieza de infraestructura más reciente y mejor probada del proyecto (30 tests dedicados) — pero, precisamente por ser tan nueva, es también la que tiene menos horas de uso real en condiciones de mercado.
17. No hay ningún archivo `conftest.py` ni fixture compartido en `ggal_bot/validation/` — cada test gestiona su propio setup/teardown manualmente, lo cual explica directamente el bug de contaminación del §11: no hay una capa central que fuerce el aislamiento de I/O en los tests.
18. El proyecto no tiene control de versiones (`git`) inicializado en este sandbox — no se pudo reconstruir un historial de commits; la reconstrucción cronológica de este informe se basa en el historial de la conversación, los comentarios "bug real" dentro del propio código, y las marcas de tiempo de archivos.
19. No hay ningún criterio cuantitativo de "go/no-go" documentado hoy en el proyecto (el README dice "correr en paralelo... durante al menos un ciclo mensual" pero no define umbrales numéricos de aceptación). Esta auditoría propone criterios concretos en §21.
20. Conclusión operativa inmediata: **todavía no existe evidencia suficiente para operar con dinero real** — no porque el diseño sea malo, sino porque (a) el track record existente es inválido/inexistente, (b) hay bugs de cálculo confirmados sin corregir (theta, smile de 3 puntos), y (c) no hay ningún control de riesgo a nivel de cuenta (kill-switch). Ver §21-22 para el detalle y el plan concreto.

---

## 2. ESTADO REAL DEL PROYECTO

### 2.1 Línea de tiempo reconstruida

No hay historial de `git` en este sandbox, así que esta reconstrucción se arma a partir de: comentarios "bug real" en el propio código (con la explicación del bug incluida ahí mismo por quien lo corrigió), las fechas de los archivos de log, y el historial de esta conversación.

| Fase | Objetivo | Implementación | Evidencia | Estado |
|---|---|---|---|---|
| Diseño inicial | Especificar un bot de arbitraje de volatilidad delta-neutral para GGAL/BYMA | `docs/Diseno_Bot_Opciones_GGAL.md` (griegas, IV, smile, checklist de validación) | Documento completo, checklist incluye "excluir ruedas con feriados", "validar contra un pricer de referencia", "backtest con puntas históricas reales" — ninguno de estos ítems del propio checklist está resuelto todavía | **CONFIRMADO** (el documento existe y es coherente), pero es un plan, no un resultado |
| Motor cuantitativo | Black-Scholes, IV solver, HV, superficie de vol, griegas de portafolio | `ggal_bot/models/*.py`, `ggal_bot/portfolio/portfolio.py` | 8 tests en `test_quant_engine.py`, bug de theta encontrado en esta auditoría (§3.1) | **PARCIALMENTE CONFIRMADO** — la mayoría es correcta, un bug real de cálculo sigue sin corregir |
| Estrategia original (vol_arbitrage) | Delta-neutral, comprar/vender IV según dislocación de smile | `ggal_bot/strategy/vol_arbitrage.py` | Existe, sigue disponible, pero ya no es la estrategia activa por defecto | **VIGENTE PERO NO USADA** |
| Ejecución en shadow mode (25-26 ago) | Probar la lógica sin arriesgar capital | `execution/order_gateway.py`, `logs/shadow_trades.csv` real (133 filas) | 2 bugs reales de re-entrada/rehedge infinito descubiertos y corregidos durante esta fase (ver §3.4/§3.5) | **REFUTADO como evidencia de comportamiento válido** — el código que generó esos logs tenía bugs que ya no existen hoy |
| Dashboard de PnL | Visualizar fills, griegas, PnL, smile | `dashboard/pnl_engine.py`, `dashboard/app.py` | Bug de multiplicador x100 encontrado y corregido (PnL mostraba ~$1.67M sobre un CSV que sólo sostenía unos pocos millones); bug nuevo encontrado hoy (símbolo `"GGAL"` sin calificar, ver §3.6) | **PARCIALMENTE CONFIRMADO** |
| Estrategia nueva (weekly_asymmetric) | Long-only, horizonte semanal, sizing por capital, filtro técnico obligatorio, OBI, salida por compresión de vega | `ggal_bot/strategy/weekly_asymmetric.py`, `risk/position_sizer.py`, `data/technical_analysis.py`, `models/microstructure.py` | 946 líneas de tests (`test_long_first_mode.py`), documentación honesta de sus propios límites en README | **IMPLEMENTADO Y TESTEADO A NIVEL UNITARIO** — cero validación empírica de resultado |
| Arquitectura multi-fuente de datos (esta semana) | Failover automático Primary/pyRofex → data912 → IOL REST → Mock | `data/live_shadow_feed.py` (1167 líneas) | 30 tests dedicados, confirmado funcionando contra datos reales de IOL (`diagnose_iol_api.py`) | **CONFIRMADO funcionando técnicamente** — sin horas de uso real todavía |
| Integración IOL/InvertirOnline | Reemplazar el 100% mock por datos reales de puntas | `BrokerRestSource` en `live_shadow_feed.py` | Confirmado contra cuenta real (login, `/Cotizacion`, `/Opciones` con cotización embebida) | **CONFIRMADO** |
| Corrección de timeout IOL (hoy) | El timeout de 5s no alcanzaba para `/Opciones` en horario de mercado | `BROKER_REST_REQUEST_TIMEOUT` 5→15s | Corregido y verificado en tu máquina real hoy mismo | **CONFIRMADO Y CORREGIDO** |
| Track record limpio bajo el código actual | — | — | **No existe ninguna fila en ningún log que corresponda a una corrida del código de hoy sin bugs conocidos** | **PENDIENTE — no iniciado** |

### 2.2 Qué funciona (verificado)

- El motor de Black-Scholes/griegas/IV es correcto salvo el bug de theta (§3.1).
- El solver de IV (Newton-Raphson + bisección) es robusto: nunca devuelve un valor silenciosamente incorrecto; falla explícitamente (`None`) cuando no converge.
- La agregación de griegas de portafolio (`portfolio.py`) es correcta, incluyendo el signo de posiciones cortas.
- El filtro de Order Book Imbalance (`microstructure.py`) es matemáticamente correcto y bien testeado.
- La lógica de "comprar primero" (long-only, spreads sólo si ya hay una posición larga confirmada) está implementada como una invariante de código, no sólo de intención (verificado en `weekly_asymmetric.py`).
- El filtro técnico (EMA/ADX/MACD/RSI) no tiene look-ahead bias en su indexación (verificado línea por línea).
- El failover multi-fuente de datos (Primary → data912 → IOL → Mock) funciona y está bien testeado.
- El motor de PnL del dashboard (FIFO, drawdown, win rate, profit factor) es matemáticamente correcto para el caso canónico.
- El proceso de ingeniería encuentra y corrige bugs reales de producción con regresiones documentadas — esto es un punto a favor real, no un halago vacío.

### 2.3 Qué no funciona o no existe

- No hay backtest histórico.
- No hay EV explícito en ninguna decisión de entrada/tamaño.
- No hay kill-switch de cuenta ni límite de pérdida diaria/semanal.
- No hay límite de posiciones concurrentes/concentración.
- El log de shadow trading está contaminado y, aun sin contaminación, reflejaría código ya obsoleto.
- El bug de theta hace que cualquier métrica de P&L atribuido a griegas (`Δ·ΔS + ½Γ·ΔS² + Θ·Δt + Vega·ΔIV`, tal como pide el propio checklist de diseño en `docs/Diseno_Bot_Opciones_GGAL.md`) sea inexacta hoy.
- El fit de smile con exactamente 3 strikes (el caso más probable dada la liquidez delgada de GGAL) es matemáticamente incapaz de detectar una dislocación real.

---

## 3. ERRORES CRÍTICOS (ordenados por impacto)

### 3.1 — P0 — `theta()` divide todo por el day-count equivocado
**Archivo**: `ggal_bot/models/black_scholes.py:120`.
El pricer usa un "reloj doble" deliberado (`t_rate = días_calendario/365` para la tasa, `t_vol = días_hábiles/252` para la volatilidad — decisión de diseño documentada y razonable). Pero `theta()` suma el término de decaimiento de volatilidad (que vive en el reloj de 252) con el término de carry de tasa/dividendo (que vive en el reloj de 365) y divide la **suma completa** por 252. Verificado numéricamente (spot=strike=5200, r=0.40, σ=0.55, 30 días calendario/21 hábiles): el código da theta=-11.93; la diferencia finita real da ≈-10.66; la reconstrucción correcta (`term_vol/252 + term_carry/365`) da ≈-10.56. **El theta está sobreestimado en magnitud ~12%**, y el error crece con options de mayor plazo o con la tasa de referencia ARS (que ronda 40% anual). Esto corrompe cualquier atribución de P&L por griegas y la guardia de "compresión de vega"/"weekend theta guard" que consumen este número. Ningún test ejercita `theta()` directamente — el bug nunca podía haber sido detectado por la suite actual.
**Fix**: `return term_vol / 252.0 + (term_rate_dividend) / 365.0` (separar los dos términos y escalar cada uno por su propio day-count).

### 3.2 — P0 — El ajuste de smile con exactamente 3 strikes anula la señal principal
**Archivo**: `ggal_bot/models/volatility_surface.py:25-26` (umbral mínimo) y `:47-62` (fit cuadrático).
El fit es una parábola de 3 parámetros. El código exige un mínimo de 3 puntos válidos — pero 3 puntos es exactamente el caso degenerado: la parábola interpola perfectamente los 3 puntos, dejando residuo cero. Verificado numéricamente: 3 strikes con IVs `[0.55, 0.70, 0.55]` (un salto deliberado de 15 vol points en el strike del medio) producen una `smile_dislocation()` de ≈0 para los tres. **La señal de arbitraje de smile — el corazón de la estrategia — es matemáticamente incapaz de dispararse exactamente quando la cadena de opciones tiene la liquidez más delgada posible (3 bases con book operable), que es el escenario más probable en GGAL/BYMA según el propio `diagnose_instruments.py`.** El test que debería haber atrapado esto (`test_smile_dislocation_detects_synthetic_bump`) tiene un nombre engañoso: no inyecta ningún salto real, sólo verifica el caso nulo (sin falso positivo), nunca el verdadero positivo.
**Fix**: exigir más puntos que grados de libertad antes de confiar en el residuo (p. ej. mínimo 5), o usar un ajuste lineal (2 parámetros) cuando sólo hay 3-4 strikes, dejando margen real de residuo.

### 3.3 — P0 — Contaminación del log de auditoría por tests mal aislados
**Archivo**: `ggal_bot/execution/order_gateway.py:463-471` (`OrderGateway.__init__` no acepta ningún parámetro de path) + prácticamente todos los tests en `test_execution_pipeline.py`, `test_shadow_trading.py` y `test_strategy_selector.py` que instancian `OrderGateway()`/`GgalOptionsBot()` sin redirigir `_shadow_logger`.
Confirmado de dos formas independientes: (1) en la copia de este sandbox, **94 de 94 filas** de `logs/shadow_trades.csv` se rastrearon una por una hasta fixtures específicos de tests (símbolo/precio idénticos, repetidos en 3 corridas distintas de la suite); (2) en tu máquina real, **4 de 133 filas** (a las 17:20:51 del 25 de agosto) corresponden exactamente al mismo patrón de contaminación — o sea, alguna vez se corrió la suite de tests contra tu instalación real y ensució tu log de producción. Sólo un test en toda la suite (`test_order_gateway_shadow_mode_logs_fill_to_audit_csv`) aísla correctamente su output con un directorio temporal; todos los demás no.
**Impacto**: cualquier análisis de P&L futuro sobre este archivo puede volver a contaminarse la próxima vez que corras los tests. No hay ningún `conftest.py` que lo prevenga centralmente.
**Fix recomendado**: agregar un parámetro `path`/`shadow_logger` inyectable en `OrderGateway.__init__` (o un `conftest.py` que fuerce `SETTINGS.shadow.enabled=False` y/o redirija `paths.SHADOW_TRADES_LOG` a un tmp dir por defecto para toda la suite), y — importante — **limpiar/archivar el `logs/shadow_trades.csv` real hoy**, ya que ninguna fila actual es utilizable (ver §3.4).

### 3.4/3.5 — P0 (ya corregidos, documentados aquí porque invalidan todo el track record histórico) — Re-entrada infinita de señal y rehedge infinito
Dos bugs reales, ya corregidos, autodocumentados en el propio código:
- `run_bot.py:549-558`: sin memoria de la posición ya abierta, `_act_on_signal()` reentraba la misma base cada ciclo (~cada 4s), sin límite, porque la señal de dislocación de smile persiste mientras la sonrisa no se corrige. Esto generó, en tu log real, 56 compras idénticas de `GFGC8000OC` en 16 minutos (25/8, 17:08-17:24) y 13 de `GFGC6800OC`.
- `run_bot.py:734-750` (comentario "BUG REAL CORREGIDO" en el propio código): el fill de la orden de cobertura de delta nunca se registraba en `self.portfolio`, así que cada ciclo siguiente veía el mismo delta "fuera de banda" y disparaba otra cobertura del mismo tamaño — rehedge sin fin. Esto generó, en tu log real, 49 compras idénticas (`quantity=784.6040184273553` exacto, repetido) del ticker de contado en 97 segundos (26/8, 13:54-13:56), con el precio simulado subiendo en cada compra. El propio comentario del código dice: *"En modo real (no shadow) esto habría sido una posición direccional descontrolada con dinero real."*
**Por qué esto importa hoy**: ambos bugs ya están corregidos (con tests de regresión: `test_act_on_signal_does_not_reenter_same_symbol_across_cycles_in_shadow_mode`, `test_maybe_hedge_records_fill_so_delta_reflects_the_hedge`). Pero significa que **129 de las 133 filas de tu log real de producción son de un código que ya no existe** — no son evidencia de nada sobre el comportamiento actual del bot, ni buena ni mala.

### 3.6 — P1 — El dashboard mal clasifica el subyacente si llega como símbolo corto
**Archivo**: `dashboard/pnl_engine.py:136-165` (`classify_strategy`, `multiplier_for_symbol`).
Ambas funciones comparan por igualdad exacta de string contra `SETTINGS.instruments.contado_ticker` (`"MERV - XMEV - GGAL - 24hs"`). Si el símbolo llega como `"GGAL"` a secas (como efectivamente ocurre en las filas contaminadas de test), no matchea, y cae al multiplicador de opciones (100) en lugar de 1.0 — reintroduciendo, para esa variante de símbolo, exactamente el mismo bug x100 que ya se había corregido para el ticker canónico.
**Fix**: normalizar/detectar el subyacente semánticamente (por ejemplo, comparar contra un conjunto de alias conocidos, o usar el multiplicador que ya viene adjunto en la fila si existe) en lugar de un único match de string.

### 3.7 — P1 — Ausencia de kill-switch / límite de pérdida a nivel de cuenta
No existe en ningún archivo (`risk_manager.py`, `position_sizer.py`, `config.py`) ningún límite de pérdida diaria/semanal ni un halt global de trading basado en P&L realizado/no realizado. `RiskManager.should_halt_new_positions()` sólo mira breach de vega/gamma, no P&L de cuenta. `weekly_target_ars` es explícitamente un parámetro de sizing, no un stop (documentado así en el propio `config.py`).

### 3.8 — P1 — Ausencia de cálculo explícito de Expected Value
Grep exhaustivo (`expected_value`, `ev_`, `break_even`, `breakeven`) sobre todo `ggal_bot/` no encuentra nada. Las entradas se aceptan por umbral de dislocación de smile en vol points, el tamaño se decide por % fijo de capital — en ningún punto se calcula `P(ganar) × payoff − P(perder) × pérdida − costos` antes de aceptar o dimensionar un trade.

### 3.9 — P2 — Fills de shadow trading sin costos
`order_gateway.py:473-495`: todo fill en modo shadow llena instantáneo, completo, al precio de referencia (mid), sin comisión ni slippage — incluso las órdenes de hedge que en la vida real cruzan el spread (compran al ask/venden al bid). Esto es una simplificación reconocida en el propio comentario del código, pero significa que cualquier P&L de paper trading actual será sistemáticamente optimista respecto de la ejecución real.

### 3.10 — P2 — Guardia de datos obsoletos no cubre el modo `vol_arbitrage`
`run_bot.py:308-324` (`_run_vol_arbitrage_cycle`) no tiene ningún chequeo de staleness de datos de mercado, a diferencia de `_run_weekly_asymmetric_cycle` (líneas 439-483) que sí lo tiene. Como `vol_arbitrage` ya no es la estrategia activa por defecto, el impacto práctico hoy es bajo, pero si alguna vez se vuelve a activar, quedaría sin esta protección.

---

## 4. CONCLUSIONES INVÁLIDAS (qué NO debemos seguir usando)

- **"El bot fue validado en producción durante dos sesiones de shadow trading"** — inválido: esas sesiones corrieron sobre versiones del código con bugs de re-entrada/rehedge infinito ya corregidos; no reflejan el comportamiento actual.
- **"`logs/shadow_trades.csv` es un registro de P&L de la estrategia"** — inválido, tanto en la copia de este sandbox (100% contaminación de tests) como, parcialmente, en tu máquina real (contaminación + código obsoleto).
- **"El filtro OBI / el filtro técnico / la compresión de vega son fuentes de alpha"** — inválido; los tres están documentados y verificados como filtros de calidad de ejecución o de gestión de riesgo, no como señales de dirección o de edge.
- **"El Sharpe/drawdown que muestra el dashboard es comparable a un Sharpe anualizado estándar"** — inválido; está correctamente calculado pero es una razón media/σ por trade **sin anualizar** (así lo etiqueta el propio código/UI), y hoy además estaría calculado sobre datos contaminados.
- **"La estrategia `weekly_asymmetric` tiene un edge medido"** — inválido; es una tesis de diseño razonada (documentada honestamente como tal en el propio README), nunca contrastada contra datos reales.
- **"El motor de griegas es 100% correcto porque tiene tests"** — parcialmente inválido; los tests existentes no ejercitan `theta()` en absoluto, ni casos ITM profundo, ni 0 días a vencimiento — el bug de theta pasó desapercibido precisamente por ese vacío de cobertura.

---

## 5. EDGE REAL ENCONTRADO

**No existe evidencia empírica de edge — ni a favor ni en contra.** Existe una **tesis de diseño razonada**: comprar convexidad donde la sonrisa de volatilidad de GGAL está anormalmente barata respecto de sus vecinas del mismo vencimiento, sólo en la dirección que confirma la tendencia diaria del subyacente, con horizonte semanal. El propio equipo ya documentó honestamente por qué se descartaron alternativas (gamma scalping puro — el costo de rehedgear en un ciclo de segundos sin colocation se come el edge; mean-reversion de nivel puro — no distingue "esta base barata" de "el vencimiento entero está barato con razón"; microestructura pura tipo HFT — el libro de GGAL es demasiado delgado y el ciclo demasiado lento; SABR/SVI completo — no se paga el costo de calibrarlo con tan pocas bases líquidas por vencimiento). Este razonamiento es coherente con la estructura conocida del mercado, pero **es cualitativo, no medido**. Esta auditoría no encontró ningún dataset, backtest, ni track record limpio que lo confirme o lo refute empíricamente. La respuesta correcta y honesta hoy es: **edge teórico plausible, edge empírico: no demostrado.**

---

## 6. HIPÓTESIS DE DISEÑO VIGENTES (no refutadas por falta de evidencia — tampoco confirmadas)

| Hipótesis | Origen | Test empírico realizado | Estado |
|---|---|---|---|
| Comprar la base cuya IV cruda está por debajo de la curva suavizada del smile genera edge | `docs/Diseno_Bot_Opciones_GGAL.md` §1.1, `weekly_asymmetric.py` | Ninguno (sin backtest) | HIPÓTESIS |
| El filtro de tendencia técnica diaria (EMA/ADX/MACD) mejora el resultado al alinear la dirección de la compra de convexidad | `weekly_asymmetric.py` docstring | Ninguno | HIPÓTESIS |
| El "Momentum Shift" (RSI) permite capturar reversiones tempranas sin perder disciplina de tendencia | `technical_analysis.py`, `weekly_asymmetric.py` | Ninguno | HIPÓTESIS |
| El filtro OBI mejora la calidad de ejecución evitando puntas aisladas | `microstructure.py` | 8 tests unitarios de la fórmula (correcta); cero evidencia de mejora de ejecución real | HIPÓTESIS (mecanismo verificado, beneficio económico no) |
| La salida por compresión de vega evita seguir pagando theta por una tesis agotada | `risk_manager.py` | Ninguno | HIPÓTESIS |
| Gamma scalping puro no se paga en BYMA por costos de rehedge frecuente | README §"Estrategia elegida" | Ninguno (razonamiento cualitativo) | HIPÓTESIS |

---

## 7. HIPÓTESIS REFUTADAS (con evidencia concreta, esta auditoría)

| Hipótesis previamente asumida | Evidencia que la refuta | Estado |
|---|---|---|
| "Con 3 strikes válidos, `smile_dislocation()` puede detectar una dislocación real" | Verificación numérica: salto de 15 vol points en 3 puntos produce dislocación ≈0 (fit interpola perfectamente) | REFUTADO |
| "El theta calculado por `black_scholes.py` es preciso" | Verificación numérica: ~12% de sobreestimación de magnitud vs. diferencia finita | REFUTADO |
| "El log de shadow trading refleja el comportamiento actual del bot" | 129/133 filas reales generadas por código con bugs de re-entrada/rehedge ya corregidos; 4/133 son contaminación de tests | REFUTADO |
| "Los tests de la suite están aislados de los datos de producción" | `OrderGateway()` sin override escribe sobre el CSV real; confirmado en ambas copias del repositorio | REFUTADO |
| "Un timeout de 5s alcanza para cualquier endpoint de IOL" | Timeouts reales confirmados en horario de mercado sobre `/Opciones`; corregido hoy a 15s | REFUTADO (y ya corregido) |

---

## 8. PROBLEMAS DE DATOS

- Fuente de datos técnica (velas 1D) es data912.com REST, con mínimo 60 velas requeridas y objetivo de 200 (`TechnicalAnalysisConfig.lookback_bars=200`, `min_bars_required=60`), con fallback a un generador sintético local si no hay suficientes — siempre logueado, nunca silencioso.
- No hay filtro que excluya la vela del día en curso (potencialmente no cerrada) de la serie técnica — riesgo de "parpadeo" intradía en la lectura de tendencia, no verificado en vivo por falta de acceso de red al esquema real de data912 desde este entorno.
- El histórico de shadow trading disponible es inutilizable (ver §3.4-3.5).
- No existe ningún dataset histórico de puntas (bid/ask) de la cadena de opciones de GGAL en este proyecto — el propio checklist de diseño (`docs/Diseno_Bot_Opciones_GGAL.md`) ya señala esto como pendiente ("verificar que los datos históricos de puntas... estén disponibles para reconstruir mid-price real").
- Calendario de vencimientos: se toma dinámicamente de la cadena real vía IOL/data912, no hardcodeado — punto a favor.

## 9. PROBLEMAS DE TARGET

**No aplica** — no hay ningún target de Machine Learning en este proyecto (no hay clasificación, no hay regresión, no hay labeling de ningún tipo). La "señal" es una regla determinística (dislocación de smile por encima de un umbral configurado), no una predicción de un modelo entrenado.

## 10. PROBLEMAS DE FEATURES

**No aplica en el sentido de ML** — no hay un vector de features alimentando un modelo. Si se interpreta "features" como las variables que alimentan las reglas de decisión, estas son: dislocación de smile (vol points), nivel de IV vs HV (opcional, apagado por defecto), moneyness (`|log(K/S)|`), convexidad por peso de prima (`(|gamma|+|vega|/100)/prima`), tendencia técnica diaria (EMA/ADX/MACD), momentum shift (RSI), y Order Book Imbalance. Todas estas están calculadas correctamente en cuanto a indexación temporal (sin look-ahead confirmado por lectura línea por línea), pero ninguna tiene un estudio de importancia, estabilidad o correlación — porque no hay un modelo que las combine estadísticamente, se combinan por reglas fijas (umbrales y filtros secuenciales).

## 11. PROBLEMAS DE MACHINE LEARNING

**No aplica — no existe ningún componente de ML en este repositorio.** Confirmado por grep exhaustivo (`sklearn`, `xgboost`, `lightgbm`, `catboost`, ningún hit) y por lectura completa de los módulos de estrategia y datos. No hay leakage de ML porque no hay ML. No hay que decidir walk-forward vs. purged CV porque no hay nada que validar de esa forma. Si en el futuro se quisiera incorporar un componente de ML (por ejemplo, para calibrar dinámicamente el umbral de dislocación de smile, o para estimar una probabilidad de reversión), correspondería auditar esa pieza específicamente cuando exista.

## 12. PROBLEMAS DE OPCIONES

- **Griegas**: correctas salvo theta (§3.1).
- **Volatilidad**: IV/HV correctos; smile fit con bug de umbral mínimo (§3.2); no hay IV Rank/Percentile ni term structure entre vencimientos (el diseño original lo prevé — §1.1 del documento de diseño — pero no está implementado en `weekly_asymmetric.py`, que opera un solo vencimiento a la vez dentro del horizonte semanal).
- **Contrato**: selección por banda de moneyness (`|log(K/S)| <= 0.15` por defecto) y ranking por score de convexidad — razonable y testeado.
- **Estrategias**: sólo Long Call/Put y Bull Call Spread/Bear Put Spread (spreads sólo como "ala" sobre una posición larga ya confirmada) bajo `weekly_asymmetric`; el modo `vol_arbitrage` (delta-neutral, con cobertura activa) sigue existiendo pero no está en uso. No hay iron condors, calendars, straddles/strangles ni ratios implementados — coherente con la decisión de diseño documentada de mantener el sistema long-only por restricción del bróker (sin venta en descubierto).
- **Riesgo de ejercicio anticipado (americano)**: el pricer usa Black-Scholes europeo como aproximación explícitamente reconocida en su propio docstring, delegando el tratamiento del riesgo de ejercicio anticipado a `risk_manager.py` — no se verificó en esta auditoría si `risk_manager.py` efectivamente compensa ese gap (fuera del alcance de los módulos leídos en detalle); queda como punto a verificar.

## 13. PROBLEMAS DE EV

Ya cubierto en detalle en §3.8 y §5. Resumen: **no existe** cálculo de EV, break-even, ni sensibilidad a RR/costos/slippage/probabilidad en ningún punto del sistema. El criterio de entrada actual (dislocación de smile > umbral) es un **proxy** de edge, no una EV calculada. Antes de operar con capital real correspondería, como mínimo, estimar el break-even de premium pagado vs. probabilidad histórica (una vez que exista track record real) de alcanzar Take Profit antes que Stop Loss dentro del horizonte semanal, neto de costos.

## 14. PROBLEMAS DE RIESGO

- Sin kill-switch de cuenta (§3.7).
- Sin límite de posiciones concurrentes/concentración.
- Sizing fijo al 20% de capital por trade con Stop Loss de -50%: cinco stops consecutivos a sizing completo reducen el capital de forma significativa (no es un bug, es una calibración agresiva marcada explícitamente como "ilustrativa" en el propio `config.py` — debe recalibrarse antes de capital real, tal como ya dice el propio README en "Calibración").
- `RiskManager.should_halt_new_positions()` sólo detiene por breach de vega/gamma, nunca por P&L.
- Guardia de staleness de datos sólo cubre el modo activo (`weekly_asymmetric`), no `vol_arbitrage` (§3.10).

## 15. PROBLEMAS DE BACKTEST

**No existe backtest.** Confirmado por grep exhaustivo (`backtest` sólo aparece en prosa de README/docs, nunca en código). No hay, por lo tanto, nada que auditar en materia de bid/ask realista, slippage, comisión, latencia, gaps, ejecución parcial dentro de un backtest — porque el backtest en sí no existe. Esto es, en sí mismo, el hallazgo: antes de cualquier cosa, el proyecto necesita decidir si va a construir un backtest histórico real (requiere el dataset de puntas históricas que el propio checklist de diseño ya señala como pendiente) o si va a apoyarse exclusivamente en un track record de paper trading en vivo suficientemente largo y limpio.

## 16. PROBLEMAS DE CÓDIGO

- `OrderGateway()` sin path inyectable → contaminación de logs de producción por tests (§3.3) — el hallazgo de código más urgente de corregir.
- `black_scholes.py:56,60` hardcodea 365/252 en vez de leer `RateConfig.day_count_calendar/business`, que existen en `config.py` pero nunca se usan — configuración fantasma.
- `risk/risk_manager.py` (`RiskLimits`, líneas 42-48) duplica los mismos 5 límites numéricos que `config.py` (`RiskConfig`) sin ningún mecanismo que los mantenga sincronizados — hoy coinciden por casualidad, no por diseño.
- `execution/delta_hedger.py` es un shim de deprecación de 27 líneas que re-exporta `strategy/delta_hedger.py` — no es una duplicación real de lógica, pero parece ser código muerto (no se encontró ningún import activo del shim).
- `dashboard/pnl_engine.py:classify_strategy/multiplier_for_symbol` — comparación por string exacto, frágil ante variantes de símbolo (§3.6).
- No hay `conftest.py` ni fixtures compartidos en `ggal_bot/validation/` — cada test gestiona su propio aislamiento manualmente, lo cual es la causa raíz de §3.3.
- Nombres, estructura de módulos y separación de responsabilidades son, en general, buenos y consistentes (data/models/strategy/risk/execution/portfolio/validation) — no se encontraron funciones gigantes ni acoplamiento fuerte entre capas en los módulos leídos.

---

## 17. NUEVA ARQUITECTURA PROPUESTA

El sistema **no necesita ser reemplazado** — la separación en capas ya existente (datos → motor cuantitativo → estrategia/señal → riesgo → ejecución) es sólida y debe conservarse. Lo que falta no es una arquitectura de ML (no aplica a este proyecto), sino una **capa de validación empírica y de control de cuenta** que hoy no existe:

```
CAPA 1 — MARKET DATA           (ya existe: Primary/data912/IOL/Mock con failover)
CAPA 2 — MOTOR CUANTITATIVO    (ya existe: BS/IV/HV/smile/griegas — corregir theta y umbral de smile)
CAPA 3 — SEÑAL/ESTRATEGIA      (ya existe: weekly_asymmetric — sin cambios estructurales)
CAPA 4 — EV / VALIDACIÓN       (NUEVA — no existe hoy: break-even, EV neto de costos, tracking de resultado real por señal)
CAPA 5 — RIESGO DE CUENTA      (EXTENDER: agregar kill-switch de pérdida diaria/semanal y límite de posiciones concurrentes)
CAPA 6 — EJECUCIÓN             (ya existe: mid-price/market making/hedger — agregar costo/slippage al fill de shadow mode)
CAPA 7 — AUDITORÍA/LOGS        (ARREGLAR — aislar tests de producción, archivar el log contaminado, empezar un track record limpio)
CAPA 8 — MONITOREO/DASHBOARD   (ya existe — corregir clasificación de símbolo del subyacente)
```

## 18. NUEVO PIPELINE (operativo, no de ML)

1. Bootstrap del universo de opciones (multi-fuente, ya existe).
2. Cálculo de IV/griegas por base (ya existe, corregir theta).
3. Ajuste de smile por vencimiento (ya existe, corregir umbral mínimo de puntos).
4. Filtro direccional técnico + momentum shift (ya existe).
5. Filtro de liquidez + OBI (ya existe).
6. Señal de entrada (dislocación > umbral) — **agregar aquí** un cálculo de break-even/EV aproximado usando el histórico de resultados una vez que exista (capa nueva).
7. Sizing (fracción fija de capital, ya existe — considerar escalar por convicción/EV una vez que exista la capa 4).
8. Ejecución (mid-price/agresivo según liquidez, ya existe — agregar modelo de costo en shadow mode).
9. Gestión de salida (Stop Loss/Take Profit/horizonte/vega decay, ya existe).
10. Registro en auditoría (arreglar aislamiento, capa 7).
11. Dashboard/monitoreo (ya existe, corregir bug de símbolo).

## 19. EXPERIMENTOS PRIORITARIOS (qué hacer primero, no una lista infinita)

1. **Arreglar el bug de theta** (§3.1) y agregar un test que lo ejerza directamente contra diferencia finita.
2. **Arreglar el umbral mínimo del smile fit** (§3.2) y agregar un test que sí inyecte una dislocación real de 3-4 puntos y verifique que el residuo la detecta.
3. **Aislar `OrderGateway` de logs de producción en tests** (§3.3) — agregar parámetro de path inyectable + un `conftest.py` que fuerce aislamiento por defecto.
4. **Archivar `logs/shadow_trades.csv` actual** (ambas copias) y empezar un track record limpio corriendo el código de hoy, sin bugs conocidos de re-entrada.
5. **Correr Shadow Trading limpio durante varias semanas completas** (incluyendo al menos un vencimiento), sin tocar el código, para empezar a acumular evidencia real.
6. **Agregar un cálculo explícito de break-even/EV** una vez que haya, aunque sea, unas pocas docenas de trades cerrados reales — comparar win rate observado y payoff promedio contra el break-even que impone el costo (spread + comisión, cuando se modele).
7. **Agregar kill-switch de pérdida diaria/semanal** antes de considerar capital real.
8. **Corregir el bug de clasificación de símbolo del dashboard** (§3.6).

## 20. ROADMAP

- **FASE 0 — Auditoría y congelamiento**: este documento. Criterio de aprobación: bugs P0 identificados y priorizados (cumplido con esta entrega). Criterio de rechazo: N/A, ya completada.
- **FASE 1 — Corrección de bugs de cálculo y de aislamiento de tests**: §3.1, §3.2, §3.3. Archivos: `black_scholes.py`, `volatility_surface.py`, `order_gateway.py`, nuevo `conftest.py`. Tests: nuevos, dirigidos a cada bug. Aprobación: los 3 bugs corregidos con test de regresión; suite completa sigue en verde.
- **FASE 2 — Track record limpio**: archivar logs contaminados, correr Shadow Trading sin tocar código por varias semanas. Aprobación: al menos un ciclo semanal completo con datos reales de IOL/data912 sin caídas de fuente no manejadas.
- **FASE 3 — EV/break-even**: agregar cálculo de EV aproximado usando resultado real acumulado. Aprobación: al menos ~30-50 trades cerrados para empezar a hablar de win rate/payoff con algún significado (con toda la cautela estadística que amerita ese tamaño de muestra — no es una muestra grande).
- **FASE 4 — Riesgo de cuenta**: kill-switch de pérdida diaria/semanal, límite de posiciones concurrentes. Aprobación: simulado en Shadow, verificado que efectivamente detiene nuevas entradas al tocar el límite (no sólo que loguea una alerta).
- **FASE 5 — Recalibración**: ajustar los límites "ilustrativos" (`max_risk_pct_per_trade=20%`, `stop_loss_pct=50%`, etc.) al tamaño real de cuenta, siguiendo la propia nota de "Calibración" del README.
- **FASE 6 — Paper trading extendido**: al menos un ciclo mensual completo en `PYROFEX_ENV=REMARKET`, tal como ya indica el propio README, ahora con datos limpios y con la capa de EV activa.
- **FASE 7 — Decisión Go/No-Go con capital real**: sólo si se cumplen los criterios de §21.

## 21. CRITERIOS DE GO / NO-GO

**GO**, únicamente si TODO lo siguiente se cumple:
- Los bugs P0 de §3.1-3.3 están corregidos y con test de regresión.
- Existe al menos un track record de Shadow Trading limpio (sin contaminación, sobre el código corregido) que cubra al menos un ciclo mensual completo con vencimiento incluido.
- Existe un kill-switch de cuenta funcionando y verificado (detiene entradas, no sólo alerta).
- Existe algún cálculo de EV/break-even, aunque sea aproximado, contrastado contra el resultado real acumulado.
- Los límites de riesgo fueron recalibrados al tamaño real de cuenta (dejaron de ser los valores "ilustrativos" por defecto).

**NO-GO** si cualquiera de estos aplica hoy (y hoy aplican varios):
- El track record disponible es inválido o inexistente — **aplica hoy**.
- Hay bugs de cálculo confirmados sin corregir (theta, smile de 3 puntos) — **aplica hoy**.
- No existe kill-switch de cuenta — **aplica hoy**.
- No existe ningún cálculo de EV — **aplica hoy**.

## 22. CONCLUSIÓN FINAL

> **¿Tenemos actualmente evidencia suficiente para operar dinero real?**
>
> **TODAVÍA NO.**

No porque el diseño sea malo — el motor cuantitativo es en general sólido, la arquitectura de datos multi-fuente es robusta, y el equipo tiene un historial demostrado de encontrar y corregir bugs reales de producción con disciplina (re-entrada infinita, rehedge infinito, multiplicador x100, timeouts de red — todos corregidos con tests de regresión). El problema es que, a día de hoy: (1) el track record de shadow trading existente no sirve como evidencia porque está contaminado y/o corresponde a versiones del código ya corregidas; (2) hay al menos dos bugs de cálculo confirmados (theta, umbral mínimo del smile) que todavía no se corrigieron y que afectan directamente el corazón de la señal de la estrategia; (3) no existe ningún control de riesgo a nivel de cuenta (kill-switch) ni cálculo de EV explícito en ningún punto de la decisión de entrada o de sizing. El camino hacia GO es concreto y corto (§19-21), no requiere reconstruir el proyecto desde cero — pero ninguno de esos pasos está completo todavía.
