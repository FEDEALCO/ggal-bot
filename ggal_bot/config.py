"""
config.py
=========
Configuracion centralizada del bot. Los valores por defecto son los que
figuran en el documento de diseño (docs/Diseno_Bot_Opciones_GGAL.md) y deben
recalibrarse con el tamaño real de cuenta y la volatilidad reciente de GGAL
antes de operar en vivo. Las credenciales NUNCA se hardcodean aca: se leen
de variables de entorno (ver .env.example) via python-dotenv.
"""

import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional, Tuple

# Resolucion explicita del .env (en vez de dejar que load_dotenv() busque
# desde el directorio de trabajo actual): esto importa especialmente para
# el .exe empaquetado con PyInstaller (ver build_exe.bat), donde el cwd al
# lanzar desde el Explorador de Windows puede no ser la carpeta del
# ejecutable. sys.frozen es el flag estandar que setea PyInstaller en
# tiempo de ejecucion; sys.executable ahi apunta al .exe real (NO al
# directorio temporal de extraccion sys._MEIPASS, que se borra al cerrar el
# proceso en modo --onefile y por lo tanto no sirve para ubicar un .env
# persistente que el usuario edito a mano).
if getattr(sys, "frozen", False):
    _PROJECT_ROOT_FOR_ENV = Path(sys.executable).resolve().parent
else:
    _PROJECT_ROOT_FOR_ENV = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    _env_path = _PROJECT_ROOT_FOR_ENV / ".env"
    load_dotenv(dotenv_path=_env_path if _env_path.exists() else None)
except ImportError:
    # python-dotenv es opcional para correr los modulos de calculo sin broker
    pass


def _env_float(name: str, default: float) -> float:
    """Lee un float desde el entorno, tolerando que la variable no exista o venga vacia."""
    raw = os.getenv(name, "")
    if raw in ("", None):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    if raw in ("", None):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "")
    if raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "si", "sí")


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name, "")
    return raw if raw != "" else default


def _env_date_list(name: str, default: Tuple[date, ...] = ()) -> Tuple[date, ...]:
    """
    Lista de fechas ISO (YYYY-MM-DD) separadas por coma, ej.
    "2026-11-06,2027-02-19" (MEJORA 2026-09-28, blackout de earnings - ver
    LongFirstConfig.earnings_dates). Deliberadamente SIN ninguna fecha
    hardcodeada por defecto: no hay forma de conocer con certeza, desde este
    codigo, el calendario real de resultados de Grupo Financiero Galicia -
    inventar fechas seria fabricar un dato, exactamente lo que este proyecto
    evita en cada auditoria. Una fecha individual que no parsea como ISO se
    ignora (se loguea un warning), en vez de tirar abajo todo el arranque
    del bot por un typo en la variable de entorno.
    """
    raw = os.getenv(name, "")
    if raw.strip() == "":
        return tuple(default)
    parsed = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            parsed.append(date.fromisoformat(token))
        except ValueError:
            logging.getLogger("ggal_bot.config").warning(
                "%s: no se pudo interpretar %r como fecha ISO (YYYY-MM-DD) - se ignora.", name, token,
            )
    return tuple(parsed)


# ---------------------------------------------------------------------------
# Credenciales, ambiente y endpoints del broker (PyRofex / ALYC)
# ---------------------------------------------------------------------------

@dataclass
class BrokerConfig:
    """
    Credenciales y endpoints del ALYC. environment selecciona REMARKET (paper
    trading, usar siempre primero aca) o LIVE (dinero real). rest_url/ws_url
    quedan disponibles para ALYCs que exponen su propio endpoint de pyRofex
    (comun en brokers argentinos que corren su propia instancia de la API
    Matriz/Primary): si se completan en .env, order_gateway.initialize_environment()
    los pasa a pyRofex.initialize(); si quedan vacios, se usa el default de
    la libreria para el ambiente elegido.
    """
    user: str = os.getenv("PYROFEX_USER", "")
    password: str = os.getenv("PYROFEX_PASSWORD", "")
    account: str = os.getenv("PYROFEX_ACCOUNT", "")
    environment: str = os.getenv("PYROFEX_ENV", "REMARKET")  # REMARKET (paper) o LIVE
    rest_url: str = os.getenv("PYROFEX_REST_URL", "")   # endpoint REST propio del ALYC (opcional)
    ws_url: str = os.getenv("PYROFEX_WS_URL", "")        # endpoint WS propio del ALYC (opcional)

    # Reconexion automatica del websocket (ver order_gateway.WebSocketConnectionManager)
    ws_reconnect_initial_seconds: float = _env_float("PYROFEX_WS_RECONNECT_INITIAL_SECONDS", 2.0)
    ws_reconnect_max_seconds: float = _env_float("PYROFEX_WS_RECONNECT_MAX_SECONDS", 60.0)
    ws_reconnect_backoff_factor: float = _env_float("PYROFEX_WS_RECONNECT_BACKOFF_FACTOR", 2.0)
    ws_max_reconnect_attempts: int = _env_int("PYROFEX_WS_MAX_RECONNECT_ATTEMPTS", 0)  # 0 = infinito

    # --- Credenciales de solo Market Data (MD-only), para la fuente Shadow
    # "primary_ws" (ver ShadowConfig.source_priority / data/live_shadow_feed.py:
    # PrimaryMarketDataSource): permite conectar a Primary/Matba Rofex con un
    # usuario de SOLO LECTURA de market data (recomendado por el ALYC para
    # este uso), sin exponer las credenciales de trading real de arriba a un
    # proceso que ademas esta corriendo en modo Shadow/paper. Si alguna de
    # estas tres queda vacia, se cae a las credenciales de trading de arriba
    # (self.user/password/account) - util si el usuario solo dispone de un
    # unico usuario, pero implica que la fuente "primary_ws" en particular
    # necesita esas credenciales reales (las demas fuentes Shadow - data912,
    # mock - no requieren ninguna credencial). Ver md_credentials()/validate_md().
    md_user: str = os.getenv("PYROFEX_MD_USER", "")
    md_password: str = os.getenv("PYROFEX_MD_PASSWORD", "")
    md_account: str = os.getenv("PYROFEX_MD_ACCOUNT", "")
    md_environment: str = os.getenv("PYROFEX_MD_ENV", "")  # vacio = usar self.environment

    def validate(self) -> Tuple[bool, str]:
        """Chequeo minimo antes de intentar conectar: evita fallar recien dentro de pyRofex."""
        missing = [name for name, val in (
            ("PYROFEX_USER", self.user),
            ("PYROFEX_PASSWORD", self.password),
            ("PYROFEX_ACCOUNT", self.account),
        ) if not val]
        if missing:
            return False, f"Faltan variables de entorno: {', '.join(missing)} (ver .env.example)"
        if self.environment.upper() not in ("REMARKET", "LIVE"):
            return False, f"PYROFEX_ENV invalido: '{self.environment}' (debe ser REMARKET o LIVE)"
        return True, ""

    def md_credentials(self) -> Tuple[str, str, str, str]:
        """
        Resuelve (user, password, account, environment) a usar para la
        conexion de SOLO market data de la fuente Shadow "primary_ws" (ver
        docstring de los campos md_* arriba): PYROFEX_MD_* si estan
        completos, si no las credenciales de trading real como fallback.
        """
        user = self.md_user or self.user
        password = self.md_password or self.password
        account = self.md_account or self.account
        environment = (self.md_environment or self.environment).upper()
        return user, password, account, environment

    def validate_md(self) -> Tuple[bool, str]:
        """Equivalente de validate() pero para las credenciales MD-only resueltas por md_credentials()."""
        user, password, account, environment = self.md_credentials()
        missing = [name for name, val in (
            ("PYROFEX_MD_USER/PYROFEX_USER", user),
            ("PYROFEX_MD_PASSWORD/PYROFEX_PASSWORD", password),
            ("PYROFEX_MD_ACCOUNT/PYROFEX_ACCOUNT", account),
        ) if not val]
        if missing:
            return False, f"Faltan credenciales para la fuente 'primary_ws': {', '.join(missing)} (ver .env.example)"
        if environment not in ("REMARKET", "LIVE"):
            return False, f"PYROFEX_MD_ENV/PYROFEX_ENV invalido: '{environment}' (debe ser REMARKET o LIVE)"
        return True, ""


# ---------------------------------------------------------------------------
# Fuente REST de IOL/InvertirOnline (ver
# data/live_shadow_feed.py:BrokerRestSource) - login y esquema de
# cotizacion/opciones CONFIRMADOS corriendo diagnose_iol_api.py contra una
# cuenta real (ver README, seccion "IOL / InvertirOnline").
# ---------------------------------------------------------------------------

@dataclass
class BrokerRestConfig:
    username: str = os.getenv("BROKER_REST_USERNAME", "")
    password: str = os.getenv("BROKER_REST_PASSWORD", "")
    base_url: str = _env_str("BROKER_REST_BASE_URL", "https://api.invertironline.com")
    # Compartido por la consulta liviana de Cotizacion y la consulta masiva
    # de Opciones (174 registros con cotizacion embebida en cada uno). 5s
    # alcanza fuera de horario pero se queda corto en horario de mercado
    # activo (confirmado con timeouts reales); 15s da margen razonable.
    request_timeout_seconds: float = _env_float("BROKER_REST_REQUEST_TIMEOUT", 15.0)

    # Segmento de mercado que espera la URL de la API (ej. "/api/v2/{market}/
    # Titulos/{simbolo}/Cotizacion"). "bCBA" (Bolsa de Comercio de Buenos
    # Aires) confirmado contra una cuenta real para instrumentos de GGAL/BYMA.
    market: str = _env_str("BROKER_REST_MARKET", "bCBA")
    # Segmento de version de la URL. "v2" confirmado contra una cuenta real;
    # se deja configurable (poner "" para omitirlo) por si IOL lo cambia.
    api_version_segment: str = _env_str("BROKER_REST_API_VERSION_SEGMENT", "v2")

    # --- Refresco de puntas INDIVIDUALES por opcion (ver BrokerRestSource.
    # _refresh_near_the_money_quotes(), hallazgo del 2026-09-01 corriendo
    # diagnose_iol_puntas.py contra una cuenta real durante horario de
    # rueda): el endpoint de CADENA (`/Titulos/GGAL/Opciones`, usado por
    # bootstrap()/fetch_snapshot() de arriba) devuelve 'puntas': null para
    # el 100% de los registros SIEMPRE - incluso para una opcion con una
    # operacion reciente (ultimoPrecio>0) - no es que el mercado este
    # ilíquido, ese endpoint especifico simplemente no trae profundidad.
    # El endpoint INDIVIDUAL por simbolo (el mismo que ya se usa para el
    # SUBYACENTE) SI trae 'puntas' pobladas para el mismo simbolo en el
    # mismo instante - confirmado en produccion. Pedir las ~104 opciones
    # individualmente en cada poll no es viable (arriesga empeorar los
    # timeouts/503 ya observados contra la API de IOL) - se restringe a
    # una banda de moneyness alrededor del spot (las UNICAS opciones que
    # la estrategia puede llegar a usar: ver LongFirstConfig.
    # moneyness_band_pct=0.15 y spread_wing_moneyness_pct), con un tope
    # duro de simbolos por refresh, y en un intervalo propio MAS LENTO que
    # el poll principal (2s) - las puntas de opciones no necesitan ser mas
    # frescas que esto para una estrategia semanal.
    individual_quote_moneyness_band_pct: float = _env_float(
        "BROKER_REST_INDIVIDUAL_QUOTE_MONEYNESS_BAND_PCT", 0.20
    )
    individual_quote_max_symbols: int = _env_int("BROKER_REST_INDIVIDUAL_QUOTE_MAX_SYMBOLS", 30)
    individual_quote_timeout_seconds: float = _env_float("BROKER_REST_INDIVIDUAL_QUOTE_TIMEOUT", 8.0)
    individual_quote_min_refresh_interval_seconds: float = _env_float(
        "BROKER_REST_INDIVIDUAL_QUOTE_REFRESH_SECONDS", 20.0
    )


# ---------------------------------------------------------------------------
# Universo de instrumentos: GGAL contado, futuro, y cadena de opciones
# ---------------------------------------------------------------------------

@dataclass
class InstrumentsConfig:
    underlying_symbol: str = "GGAL"
    contado_ticker: str = "MERV - XMEV - GGAL - 24hs"
    futuro_ticker: str = ""  # completar si hay futuro de GGAL con liquidez vigente
    # Prefijos habituales de opciones de GGAL en BYMA (calls: GFGC..., puts: GFGV...)
    call_prefix: str = "GFGC"
    put_prefix: str = "GFGV"
    option_multiplier: int = 100
    expiries_ahead: int = 2  # cantidad de vencimientos vigentes a suscribir hacia adelante
    market_segment: str = "MERV - XMEV"  # segmento/mercado usado al listar instrumentos

    # --- Calibracion del parser de simbolos (fallback cuando el instrumento
    # no trae strike/vencimiento en su propia metadata, ver
    # data/market_data_feed.py:bootstrap_universe) ---
    option_symbol_regex: str = r"^(\d+)([A-L])$"  # digitos de strike + letra de mes (A=Ene...L=Dic)
    strike_scale: float = 1.0  # multiplicador para convertir los digitos parseados en precio real

    # --- Vencimiento forzado (a pedido explicito del usuario, 2026-09-01) ---
    # Fuerza al bot a operar UN SOLO vencimiento especifico, ignorando el
    # resto por completo (ni para entradas nuevas en run_bot.py ni para
    # completar spreads/wings en WeeklyAsymmetricStrategy.
    # scan_spread_completion_signals) - en vez de dejar que el horizonte
    # semanal (LongFirstConfig.max_holding_business_days) determine solo
    # que vencimiento termina siendo operable. Formato ISO "YYYY-MM-DD".
    # Vacio (default) = sin forzar, comportamiento normal (todos los
    # `expiries_ahead` vencimientos son elegibles segun el horizonte
    # semanal, como antes). IMPORTANTE: si se fuerza un vencimiento mas
    # lejano que max_holding_business_days (ej. un vencimiento mensual con
    # el horizonte semanal default de 5 dias habiles), el bot lo va a
    # trackear pero NUNCA va a poder abrir una entrada ahi - subir
    # max_holding_business_days junto con esto si el vencimiento forzado
    # excede el horizonte actual.
    forced_expiry: str = _env_str("GGAL_BOT_FORCE_EXPIRY", "")

    def forced_expiry_date(self) -> Optional[date]:
        """Parsea `forced_expiry` a `date`, o None si esta vacio o es invalido (el llamador loguea el caso invalido)."""
        if not self.forced_expiry.strip():
            return None
        try:
            return date.fromisoformat(self.forced_expiry.strip())
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Tasa de referencia y convenciones de dias
# ---------------------------------------------------------------------------

@dataclass
class RateConfig:
    """
    default_annual_rate es la 'r' (tasa libre de riesgo local, ARS) usada por
    defecto en Black-Scholes cuando no hay una lectura en vivo del mercado de
    caucion/badlar. En produccion, reemplazar por una fuente de datos real.
    """
    default_annual_rate: float = _env_float("GGAL_BOT_RISK_FREE_RATE", 0.40)  # 'r'
    dividend_yield: float = _env_float("GGAL_BOT_DIVIDEND_YIELD", 0.0)
    day_count_calendar: int = 365   # para descuento/forward (tasa)
    day_count_business: int = 252   # para riesgo/vol (griegas)


# ---------------------------------------------------------------------------
# Umbrales de señal (arbitraje de smile / nivel IV vs HV)
# ---------------------------------------------------------------------------

@dataclass
class SignalConfig:
    smile_threshold_vol_points: float = 3.0     # dislocacion minima IV cruda vs curva
    level_threshold_vol_points: float = 5.0     # dislocacion minima IV promedio vs HV
    hv_windows: tuple = (5, 10, 20, 60)          # ruedas para HV multi-ventana
    iv_sigma_guess: float = 0.35                 # semilla inicial para Newton-Raphson


# ---------------------------------------------------------------------------
# Limites de riesgo y filtros de liquidez (calibrar por tamaño de cuenta)
# ---------------------------------------------------------------------------

@dataclass
class RiskConfig:
    # delta_band = umbral de delta-neutralidad: acciones equivalentes de GGAL
    # que el portafolio puede acumular antes de disparar un rehedge automatico.
    delta_band: float = _env_float("GGAL_BOT_DELTA_NEUTRAL_THRESHOLD", 150.0)
    # A pedido explicito del usuario (2026-09-01, ver run_bot.py._maybe_hedge):
    # apaga por completo el rehedge automatico contra el subyacente/futuro -
    # el bot pasa a operar UNICAMENTE opciones, sin ninguna orden sobre GGAL
    # contado/futuro. Deliberadamente SIN ningun tope/limite de reemplazo que
    # bloquee nuevas entradas por delta agregado (decision explicita del
    # usuario, no un default nuevo): el delta de la cartera de opciones queda
    # sin ningun control automatico mientras este flag este en false.
    enable_delta_hedge: bool = _env_bool("GGAL_BOT_ENABLE_DELTA_HEDGE", True)
    max_vega_total: float = 5000.0       # $ por punto de vol (1 vol point = 0.01 de IV)
    max_gamma_total: float = 2000.0      # $ por (punto de movimiento de GGAL)^2
    max_spread_relative: float = 0.05    # spread relativo maximo para considerar operable
    min_book_size: float = 20.0          # tamaño minimo en punta (contratos)
    min_daily_volume: float = 50.0       # volumen minimo operado reciente
    hedge_max_spread_relative: float = 0.01
    hedge_min_size: float = 50.0

    # Guardia de staleness de datos de mercado (ver
    # run_bot.py:_is_market_data_stale / GgalOptionsBot._on_book_update): si
    # pasan mas de este umbral sin una actualizacion exitosa del spot de GGAL
    # (caida de conectividad con data912/websocket, timeouts repetidos, etc.),
    # el bot deja de generar ENTRADAS nuevas y de completar spreads hasta que
    # vuelva a haber datos frescos - un motivo real detectado en produccion
    # (ver README, seccion de la guardia): sin este control, el bot seguia
    # calculando IV/griegas/señales contra precios de varios minutos de
    # antiguedad sin ninguna alerta mas alla del warning de poll individual.
    # Las SALIDAS (Stop Loss/Take Profit/etc.) y el delta-hedger siguen
    # evaluandose con la ultima punta conocida - deliberado: es preferible
    # seguir gestionando riesgo ya tomado con un dato ligeramente viejo que
    # dejarlo completamente sin vigilancia mientras dura la caida.
    max_market_data_staleness_seconds: float = _env_float("GGAL_BOT_MAX_DATA_STALENESS_SECONDS", 60.0)

    # Guardia de staleness POR OPCION (BUG REAL CORREGIDO, ver auditoria
    # docs/AUDITORIA_MAESTRA_2026-08-27.md y su seguimiento del 2026-08-31):
    # la guardia de arriba (max_market_data_staleness_seconds) solo cubre el
    # SPOT, bajo el supuesto (documentado en el docstring de
    # GgalOptionsBot._on_book_update) de que spot y cadena de opciones
    # siempre fallan de forma atomica. Ese supuesto es CIERTO para
    # Data912RestSource (un fallo de red devuelve (None, {}) para ambos a la
    # vez) pero es FALSO para BrokerRestSource/IOL: se confirmo en una
    # corrida real (31/08, ~12:00-14:11 ART) que la cadena de opciones puede
    # fallar sola, repetidamente, durante horas, mientras el spot se sigue
    # actualizando con normalidad - BrokerRestSource._quote_cache reproduce
    # la ULTIMA cotizacion buena de cada opcion como si fuera fresca en cada
    # poll, sin que nada aguas abajo supiera que ese dato ya tiene mucho
    # tiempo. El riesgo concreto: recalcular IV/griegas de una opcion con un
    # spot FRESCO contra un precio de opcion VIEJO puede fabricar una
    # "dislocacion de smile" espuria que no es una señal real, solo un
    # artefacto de datos desincronizados entre fuentes. Con esta guardia, una
    # opcion cuyo book.as_of supera este umbral se excluye de la deteccion de
    # señales de ENTRADA nuevas (mismo criterio que el spot: las salidas y el
    # delta-hedger la siguen usando con su ultimo valor conocido).
    max_option_quote_staleness_seconds: float = _env_float("GGAL_BOT_MAX_OPTION_STALENESS_SECONDS", 90.0)

    # Alerta ACTIVA por posicion sin cotizacion vigente (MEJORA 2026-09-17,
    # a pedido explicito del usuario, misma tanda que VolArbitrageConfig).
    # Hasta esta mejora, que una base con una posicion abierta desapareciera
    # de la cadena vigente (vencio del universo de vencimientos, la cadena
    # cayo sola, etc.) solo se hacia visible de forma PASIVA en el
    # dashboard (ver dashboard/app.py, caption "Sin cotizacion actual" en la
    # pestaña "Abiertas") - y ese dashboard depende de reconstruir
    # shadow_trades.csv, no del estado vivo del bot, asi que requiere que
    # alguien lo abra para notarlo. El riesgo real (VERIFICADO por lectura
    # de risk/risk_manager.py:evaluate_position_exit): con
    # current_price=None, Stop Loss/Take Profit/toma de ganancia parcial/
    # compresion de vega simplemente se OMITEN para esa posicion (no
    # "fallan seguro") - solo el horizonte de dias habiles y la guardia de
    # fin de semana (que no dependen del precio) le siguen aplicando. Ver
    # run_bot.py:GgalOptionsBot._warn_positions_without_valid_quote.
    #
    # Deliberadamente un umbral, no instantaneo: una base puede quedar sin
    # punta operable por un instante (poll individual fallido) sin que eso
    # sea todavia motivo de alerta - mismo criterio de "caida sostenida, no
    # un fallo puntual" que max_market_data_staleness_seconds arriba.
    # None desactiva esta alerta por completo (ningun logger.warning nuevo)
    # - se deja en un valor por defecto (no None) porque esta mejora es
    # PURO LOGGING/OBSERVABILIDAD (no cierra posiciones, no bloquea
    # entradas, no cambia ninguna decision de trading): a diferencia de los
    # flags de comportamiento nuevos de este archivo (que preservan
    # comportamiento apagados por default), no hay ningun comportamiento
    # de trading previo que este cambio pudiera alterar.
    stale_quote_warning_seconds: Optional[float] = (
        _env_float("GGAL_BOT_STALE_QUOTE_WARNING_SECONDS", 300.0) or None
    )

    # --- Presupuesto PREVENTIVO de Griegas por entrada (MEJORA 2026-09-28) ---
    # A pedido explicito del usuario ("mejor trader quant... presupuesto de
    # riesgo agregado en vez de esperar al muro duro"): hasta esta mejora, el
    # unico chequeo de Griegas en la entrada era should_halt_new_positions
    # (RiskManager.check_greeks_limits) contra los totales YA vigentes ANTES
    # de sumar la posicion nueva - una entrada podia empujar el total de
    # vega/gamma MUY por encima del limite duro en un solo salto (y de hecho
    # es exactamente el patron real observado: cientos de REJECT por
    # greeks_limit_exceeded concentrados en los dias de mayor actividad, ver
    # analisis del export 2026-09-28). Esta guarda es ADICIONAL (se SUMA al
    # chequeo existente, no lo reemplaza): proyecta los totales de la
    # estrategia CON la posicion nueva ya sumada (contratos*griega_por_unidad,
    # ver risk/risk_manager.py::RiskManager.projected_greeks_breach) y
    # rechaza si eso superaria una FRACCION del limite duro (nunca el limite
    # en si, que sigue siendo el ultimo resorte) - la idea es frenar ANTES de
    # llegar al muro, no reemplazar el muro. DATA INSUFFICIENT para calibrar
    # la fraccion optima sin datos propios de esta mejora; 0.85 es un piso de
    # arranque razonable (deja un 15% de margen), a criterio explicito del
    # usuario ajustarlo con lo que se observe.
    enable_preemptive_greeks_budget: bool = _env_bool("GGAL_BOT_ENABLE_PREEMPTIVE_GREEKS_BUDGET", False)
    preemptive_greeks_budget_fraction: float = _env_float("GGAL_BOT_PREEMPTIVE_GREEKS_BUDGET_FRACTION", 0.85)


# ---------------------------------------------------------------------------
# Kill switch y limites de riesgo CENTRALIZADOS a nivel de portfolio
# (Fase 5.3, ver ggal_bot/risk/kill_switch.py). Distinto de RiskConfig
# arriba (Griegas evaluadas POR ESTRATEGIA via Portfolio.
# greeks_for_strategy_tag) y de LongFirstConfig/ScalpingConfig (sizing/
# capital por estrategia): esto evalua limites AGREGADOS de TODA la cuenta
# y, si se exceden, bloquea entradas nuevas en TODO el bot (nunca las
# salidas - ver docstring de KillSwitch) hasta un reset manual persistido
# en disco (paths.KILL_SWITCH_STATE_FILE), sobreviviendo restarts.
# ---------------------------------------------------------------------------

@dataclass
class RiskLimitsConfig:
    enabled: bool = _env_bool("GGAL_BOT_KILL_SWITCH_ENABLED", True)
    # Perdida realizada MAXIMA del dia (ARS, valor positivo) antes de
    # disparar el kill switch. None (default) = sin limite de PnL diario -
    # DELIBERADO: calcular PnL realizado del dia requiere leer y parsear
    # logs/shadow_trades.csv (dashboard.pnl_engine, que depende de pandas -
    # ver comentario en requirements-dashboard.txt), una dependencia
    # opcional que run_bot.py no fuerza por defecto (ver
    # ggal_bot/portfolio/reconciliation.py, mismo criterio). Fijar un valor
    # aca NO activa el calculo automaticamente: quien llame a
    # KillSwitch.evaluate() debe pasarle `realized_pnl_today_ars` con un
    # numero real, nunca None mientras el limite este seteado, en cuyo caso
    # KillSwitch simplemente no evalua ese chequeo especifico (ver su
    # docstring) - nunca se fabrica un valor de PnL.
    max_daily_loss_ars: Optional[float] = _env_float("GGAL_BOT_MAX_DAILY_LOSS_ARS", 0.0) or None
    # Techo agregado de contratos abiertos (suma de |quantity| de TODAS las
    # posiciones, todas las estrategias). None (default) = sin techo
    # agregado (cada estrategia sigue limitada por su propio capital/sizing).
    max_open_contracts_total: Optional[float] = _env_float("GGAL_BOT_MAX_OPEN_CONTRACTS_TOTAL", 0.0) or None
    # Cuantas Position simultaneas (quantity>0) se toleran para el MISMO
    # symbol+strategy_tag antes de marcarlo como violacion de integridad -
    # en el flujo normal, Guarda 2 (_act_on_entry_signal) ya garantiza como
    # maximo 1 (ver AUDITORIA_FASE5.2_LIFECYCLE_ROOT_CAUSE.md); este limite
    # es la red de contencion que DETECTA si esa garantia se violo (ej. el
    # mismo escenario de restart-sin-persistencia que motivo
    # reconciliation.py, antes de que ese modulo existiera).
    max_positions_per_symbol_strategy: int = _env_int("GGAL_BOT_MAX_POSITIONS_PER_SYMBOL_STRATEGY", 1)
    # BRECHA VERIFICADA (mega-prompt "OPTIMIZACION EJECUTABLE", seccion 12):
    # ni RiskLimitsConfig (este archivo) ni RiskLimits (ggal_bot/risk/
    # risk_manager.py, que solo topea max_vega_total/max_gamma_total) tenian
    # NINGUN limite de exposicion DIRECCIONAL agregada (delta). El riesgo
    # concreto: "Trade A ok + Trade B ok + Trade C ok" en simbolos/strikes
    # DISTINTOS pero misma direccion (ej. varios calls de GGAL) puede acumular
    # un delta de portfolio grande sin que ningun chequeo individual lo vea -
    # exactamente el patron de riesgo correlacionado que el mega-prompt pide
    # cubrir. Se agrava porque delta-hedging esta deshabilitado por pedido
    # explicito previo del usuario (ver comentario en risk_manager.py: "sin
    # ningun tope de reemplazo"). None (default) = sin limite (comportamiento
    # actual sin cambios hasta que se configure explicitamente). Unidad: ARS
    # nocionales = |delta total del portfolio (en acciones subyacentes
    # equivalentes)| * spot - por eso KillSwitch.evaluate() ahora acepta un
    # `spot` opcional (ver su docstring/firma). Sin `spot` este chequeo no se
    # evalua (no se fabrica un spot ficticio).
    max_portfolio_delta_ars: Optional[float] = _env_float("GGAL_BOT_MAX_PORTFOLIO_DELTA_ARS", 0.0) or None


# ---------------------------------------------------------------------------
# Ejecucion / market making / control de slippage
# ---------------------------------------------------------------------------

@dataclass
class ExecutionConfig:
    tick_size: float = 0.01
    liquid_spread_relative_threshold: float = 0.02
    order_timeout_seconds: int = 15       # ventana antes de mejorar precio o cancelar
    max_price_improvements: int = 3
    # slippage maximo tolerado entre el precio de referencia al armar la orden
    # y el precio de mercado vigente al momento de repricear/monitorear (ver
    # execution/mid_price_exec.py). Expresado como fraccion (0.01 = 1%).
    max_slippage_pct: float = _env_float("GGAL_BOT_MAX_SLIPPAGE_PCT", 0.01)
    # si el subyacente se mueve mas que esto (fraccion) desde que se armo la
    # orden de la opcion, se cancela/repricea aunque no haya vencido el timeout.
    underlying_move_cancel_pct: float = _env_float("GGAL_BOT_UNDERLYING_MOVE_CANCEL_PCT", 0.005)


# ---------------------------------------------------------------------------
# Shadow Trading / Live Replay: probar la logica cuantitativa sin arriesgar
# capital y sin depender de que el ALYC tenga la cadena de opciones de GGAL
# aprovisionada en REMARKET (ver docs/Diseno_Bot_Opciones_GGAL.md y la
# discusion previa sobre el ambiente de paper trading). Cuando
# `enabled=True`:
#   - data/live_shadow_feed.py reemplaza a market_data_feed.py como fuente
#     de datos (REST publico de data912.com, o un generador Mock/Replay si
#     no hay red o se pide explicitamente).
#   - execution/order_gateway.py entra en "Paper Execution": las ordenes se
#     dan por FILLED de inmediato al mid-price de referencia, sin tocar
#     nunca la API de ejecucion real de pyRofex, y quedan auditadas en
#     logs/shadow_trades.csv (ver paths.SHADOW_TRADES_LOG).
# ---------------------------------------------------------------------------

@dataclass
class ShadowConfig:
    enabled: bool = _env_bool("GGAL_BOT_SHADOW_MODE", False)
    # LEGADO: "auto" = intenta data912 y cae a mock si no hay red/responde
    # vacio; "data912" fuerza el REST publico; "mock" fuerza el generador
    # sintetico. Se mantiene por compatibilidad hacia atras (ver
    # source_priority() abajo, que lo usa como fallback si
    # GGAL_BOT_SHADOW_SOURCE_PRIORITY no esta seteada) - para elegir entre
    # MAS de dos fuentes con prioridad explicita (ej. Primary/pyRofex antes
    # que data912), usar GGAL_BOT_SHADOW_SOURCE_PRIORITY en su lugar.
    data_source: str = _env_str("GGAL_BOT_SHADOW_SOURCE", "auto")

    # --- Multi-fuente con prioridad y failover (ver
    # data/live_shadow_feed.py:LiveShadowFeed) ---
    # Lista de nombres de fuente separados por coma, en orden de preferencia
    # (ej. "primary_ws,data912,mock"). Nombres validos: "primary_ws"
    # (Primary/Matba Rofex via pyRofex, MD-only, ver PrimaryMarketDataSource
    # - requiere pyRofex instalado y credenciales, ver BrokerConfig.md_*),
    # "data912" (REST publico data912.com, ver Data912RestSource),
    # "broker_rest" (scaffold de un ALYC local, ver BrokerRestSource - NO
    # verificado contra una cuenta real, no se agrega solo/automaticamente:
    # el usuario debe incluirlo explicitamente aca tras validarlo), "mock"
    # (generador sintetico 100 por ciento local, ver MockReplaySource).
    # Vacia (default) = usar el selector legado de arriba (data_source).
    source_priority_raw: str = _env_str("GGAL_BOT_SHADOW_SOURCE_PRIORITY", "")
    # Cuantos fallos CONSECUTIVOS de poll (fetch_snapshot devolviendo spot
    # None y ninguna opcion) tolera la fuente activa antes de conmutar a la
    # siguiente en la prioridad - deliberadamente > 1 (mismo criterio que la
    # guardia de staleness de datos, ver RiskConfig.max_market_data_staleness_seconds):
    # un timeout aislado no debe disparar un failover completo (que implica
    # re-descubrir el universo de instrumentos contra la fuente nueva), solo
    # una caida sostenida.
    source_failure_threshold: int = _env_int("GGAL_BOT_SHADOW_SOURCE_FAILURE_THRESHOLD", 3)
    # Cada cuanto se reintenta volver a una fuente de MAYOR prioridad que la
    # activa, una vez que ya hubo un failover (ej. volver a "primary_ws"
    # despues de haber caido a "data912"). No es instantaneo a proposito:
    # evita "flapping" (conmutar de ida y vuelta en cada ciclo) si la fuente
    # preferida esta intermitente.
    source_reprobe_interval_seconds: float = _env_float("GGAL_BOT_SHADOW_SOURCE_REPROBE_SECONDS", 300.0)

    poll_interval_seconds: float = _env_float("GGAL_BOT_SHADOW_POLL_SECONDS", 5.0)

    # Fase 5.3 (ver ggal_bot/portfolio/reconciliation.py y
    # AUDITORIA_FASE5.3_*.md): al arrancar, reconstruye self.portfolio
    # desde logs/shadow_trades.csv en vez de arrancar siempre vacio -
    # cierra el gap identificado como la explicacion mas probable de la
    # contradiccion de Guarda 2 observada en produccion (un restart de
    # proceso sin persistencia de estado). Default True porque es
    # estrictamente mas seguro que el comportamiento anterior (Guarda 2
    # ve la posicion real en vez de creer que esta en cero) y falla de
    # forma segura (portfolio vacio + warning) si no puede ejecutarse.
    reconcile_portfolio_on_startup: bool = _env_bool("GGAL_BOT_SHADOW_RECONCILE_ON_STARTUP", True)

    def source_priority(self) -> Tuple[str, ...]:
        """
        Devuelve la lista de fuentes candidatas, en orden de preferencia,
        para el modo Shadow (ver data/live_shadow_feed.py:LiveShadowFeed).
        Resuelve, en orden:
          1. source_priority_raw (GGAL_BOT_SHADOW_SOURCE_PRIORITY) si no
             esta vacia - formato nuevo, prioridad explicita entre >=2
             fuentes (ej. "primary_ws,data912,mock").
          2. Si no, mapea el selector legado data_source (GGAL_BOT_SHADOW_SOURCE):
             "data912" -> ("data912",) (fuerza esa unica fuente, sin
             fallback - comportamiento identico al de antes de esta
             funcion); "mock" -> ("mock",); cualquier otro valor (incluido
             el default "auto") -> ("data912", "mock") (comportamiento
             previo de "auto": probar data912 y caer a mock).
        """
        if self.source_priority_raw.strip():
            names = tuple(n.strip().lower() for n in self.source_priority_raw.split(",") if n.strip())
            if names:
                return names
        legacy = self.data_source.strip().lower()
        if legacy == "data912":
            return ("data912",)
        if legacy == "mock":
            return ("mock",)
        return ("data912", "mock")

    # --- Fuente REST publica (https://data912.com, sin autenticacion, "free
    # market data", ~120 req/min de limite documentado) ---
    data912_base_url: str = _env_str("GGAL_BOT_DATA912_BASE_URL", "https://data912.com")
    data912_stocks_endpoint: str = "/live/arg_stocks"
    data912_options_endpoint: str = "/live/arg_options"
    # Historico de velas diarias (OHLCV) por ticker - lo usa
    # data/technical_analysis.py para el filtro de tendencia 1D, NO el feed
    # de shadow trading en tiempo real (ver live_shadow_feed.py) - se
    # documenta aca junto a data912_base_url/request_timeout_seconds porque
    # es el mismo proveedor y se reusan esos dos parametros.
    data912_historical_stocks_endpoint_template: str = "/historical/stocks/{ticker}"
    request_timeout_seconds: float = _env_float("GGAL_BOT_SHADOW_REQUEST_TIMEOUT", 5.0)
    # Bonos soberanos para CCL implicito (MEJORA 2026-09-30, ver
    # ggal_bot/data/ccl_bond_quote_log.py y REPORT.md) - mismo REST/mismo
    # base_url/timeout de arriba, endpoint distinto ("/live/arg_bonds",
    # confirmado real via WebFetch: GD30/GD30C/AL30/AL30C existen ahi con
    # el mismo schema symbol/px_bid/px_ask/c que arg_stocks/arg_options).
    # Opt-in, apagado por defecto - mismo criterio que
    # market_snapshot_log/signal_funnel_log: una fuente nueva nunca se
    # activa sola.
    data912_bonds_endpoint: str = "/live/arg_bonds"
    enable_ccl_bond_quote_log: bool = _env_bool("GGAL_BOT_ENABLE_CCL_BOND_QUOTE_LOG", False)

    # --- Generador Mock/Replay (sin ninguna dependencia de red) ---
    mock_initial_spot: float = _env_float("GGAL_BOT_MOCK_INITIAL_SPOT", 6600.0)
    mock_atm_iv: float = _env_float("GGAL_BOT_MOCK_ATM_IV", 0.55)
    mock_smile_curvature: float = 0.15         # coeficiente cuadratico en log-moneyness
    mock_iv_noise_std: float = 0.01            # ruido idiosincratico por tick (OU discreto)
    mock_iv_noise_decay: float = 0.90          # decaimiento del ruido (mean-reversion)
    mock_mispricing_probability: float = 0.002  # prob. por strike y por tick de un shock transitorio
    mock_mispricing_vol_points: float = 6.0     # magnitud del shock, en vol points
    mock_mispricing_duration_ticks: int = 5     # cuantos ticks dura el shock antes de decaer
    mock_strike_step: float = 200.0
    mock_num_strikes_each_side: int = 6         # strikes por encima y por debajo del spot inicial
    # dias corridos a los 2 vencimientos simulados. Se cambio de (25, 55)
    # (2 mensuales) a (5, 25) para que el mas cercano caiga DENTRO del
    # horizonte semanal del modo Long-First (ver LongFirstConfig abajo) -
    # con (25, 55) el generador Mock nunca producia una base elegible para
    # esa estrategia (todo quedaba fuera del corte de 5 dias habiles).
    mock_expiries_days_ahead: tuple = (5, 25)
    mock_atm_spread_pct: float = 0.03           # spread relativo minimo (bases ATM/liquidas)
    mock_spread_widening_per_logmoneyness: float = 0.15  # como se ensancha el spread lejos del dinero
    mock_min_absolute_spread: float = 0.01
    mock_min_size: float = 10.0
    mock_max_size: float = 200.0
    mock_tick_size_underlying: float = 1.0
    mock_random_seed: Optional[int] = _env_int("GGAL_BOT_MOCK_SEED", 0) or None
    trading_seconds_per_year: float = 252 * 6.5 * 3600  # ~5.9M seg (ruedas de 6.5hs)


# ---------------------------------------------------------------------------
# Modo operativo "Long-First / Weekly Asymmetric" (sin posiciones
# descubiertas, horizonte semanal, sizing por capital asignado). Ver
# strategy/weekly_asymmetric.py y risk/position_sizer.py.
#
# Este es un modo operativo DISTINTO del arbitraje de volatilidad
# delta-neutral original (RiskConfig/SignalConfig arriba, ver
# strategy/vol_arbitrage.py): ese sigue disponible tal cual, sin cambios;
# este bloque no lo modifica ni lo reemplaza, agrega parametros nuevos para
# la estrategia nueva.
#
# NOTA DE RIESGO (leer antes de operar con capital real): weekly_target_ars
# es un PARAMETRO DE DIMENSIONAMIENTO (para calibrar cuanta convexidad se
# busca), NO una proyeccion ni una garantia de retorno. Un objetivo de 100%
# semanal implica, por construccion matematica, arriesgar una fraccion
# grande del capital en estructuras que pueden perder la totalidad de la
# prima pagada. Nada en este modulo ni en strategy/weekly_asymmetric.py
# estima la probabilidad de alcanzar ese objetivo - eso depende del mercado,
# no de la configuracion.
# ---------------------------------------------------------------------------

@dataclass
class LongFirstConfig:
    enabled: bool = _env_bool("GGAL_BOT_LONG_FIRST_MODE", True)

    # --- Restriccion estructural: nunca posiciones descubiertas ---
    # (documental/flag de intencion - la garantia REAL es de codigo: ver
    # strategy/weekly_asymmetric.py.scan_spread_completion_signals(), que
    # nunca genera una pata corta sin una Position larga ya confirmada).
    forbid_naked_short: bool = True

    # --- Capital y sizing dinamico (ver risk/position_sizer.py) ---
    # AJUSTE DE RIESGO 2026-09-07 (post Fase 5.3, a pedido explicito del
    # usuario, ver conversacion de esa fecha): bajado de 0.20 a 0.10.
    # Motivo: mientras el Position Lifecycle Engine (Fase 5.3) recien se
    # esta bedding-in en produccion, el usuario decidio arrancar en el
    # extremo INFERIOR del rango de riesgo por trade en vez del superior -
    # textual: "estamos corrigiendo un sistema que todavia tiene problemas
    # de lifecycle/ejecucion, por lo que no tiene sentido mantener el
    # extremo superior del rango mientras todavia estamos descubriendo como
    # se comporta realmente". Subida propuesta EXPLICITAMENTE progresiva
    # (10% -> 12% -> 15%) solo si los datos forward (via el event journal
    # nuevo, ver ggal_bot/portfolio/event_journal.py) lo justifican - NO se
    # sube de nuevo por decision unilateral de este commit.
    max_capital_ars: float = _env_float("GGAL_BOT_MAX_CAPITAL_ARS", 1_000_000.0)
    max_risk_pct_per_trade: float = _env_float("GGAL_BOT_MAX_RISK_PCT_PER_TRADE", 0.10)
    min_contracts_per_trade: int = _env_int("GGAL_BOT_MIN_CONTRACTS_PER_TRADE", 1)

    # --- Objetivo de retorno (dimensionamiento, NO garantia - ver nota arriba) ---
    # AJUSTE DE RIESGO 2026-09-07 (primera vuelta): bajado de $1.000.000
    # (100% semanal) a $400.000 (40% semanal), a pedido explicito del
    # usuario, como "politica de riesgo" declarada - NO porque hubiera
    # evidencia de que $1.000.000 causara el problema observado (el usuario
    # fue explicito en ese momento: "razonable como control de agresividad,
    # pero no es una causa demostrada del problema").
    #
    # AJUSTE 2026-09-07 (segunda vuelta, mismo dia): vuelto a subir a
    # $1.000.000 a pedido explicito del usuario ("buscar que la estrategia
    # genere $1.000.000 por semana de profit"). Se le recordo la misma
    # limitacion de abajo (sigue sin estar conectado a ningun calculo) antes
    # de aplicar el cambio.
    #
    # LIMITACION IMPORTANTE, verificada por grep (no asumida, sigue vigente
    # tras este segundo cambio): este campo NO esta conectado a NINGUN
    # calculo del bot hoy. `risk/position_sizer.py::PositionSizer.
    # compute_contracts()` (la unica fuente real de sizing) NO lo lee, y no
    # hay ningun otro punto del codigo que lo use para limitar contratos,
    # capital, frecuencia de entradas, ni para medir/reportar si la
    # estrategia efectivamente lo esta cumpliendo - es puramente documental/
    # de intencion (ver el docstring de weekly_asymmetric.py que lo cita
    # como "PARAMETRO DE DIMENSIONAMIENTO para calibrar cuanta convexidad se
    # busca", nunca como un input a una formula). Subirlo a $1.000.000 no
    # cambia, por si solo, NINGUN comportamiento real del bot - no aumenta
    # el sizing, no relaja ningun filtro de entrada, no agrega presion para
    # operar mas seguido. El usuario fue informado explicitamente de esto
    # antes de aplicar el cambio. Si en el futuro se decide conectarlo a
    # algo real (ej. medir PnL semanal acumulado contra este objetivo en el
    # dashboard, o un techo de riesgo agregado semanal), eso es un cambio de
    # codigo nuevo, no cubierto por este commit.
    #
    # AJUSTE 2026-09-08 (TANDA 2 "OPTIMIZACION EJECUTABLE", seccion 7, tercera
    # vuelta sobre el mismo parametro): bajado de nuevo a $500.000 (50%
    # semanal) a pedido explicito del usuario, junto con max_risk_pct_per_trade
    # (ver ese campo mas arriba - su default de codigo YA estaba en 0.10,
    # exactamente el baseline pedido en esta misma tanda, verificado por
    # lectura, sin necesidad de cambiarlo). La limitacion de arriba (campo
    # puramente documental, sin ninguna conexion de codigo) sigue vigente sin
    # cambios.
    weekly_target_ars: float = _env_float("GGAL_BOT_WEEKLY_TARGET_ARS", 500_000.0)

    # --- Horizonte de entrada/salida y guardia de decay de fin de semana ---
    # AJUSTE 2026-09-07, a pedido explicito del usuario: "quita el limite de
    # vencimiento de aca a maximo 5 dias habiles... el objetivo es que opere
    # en el vto mas proximo de opciones que tenga mayor profundidad y
    # liquidez, como en este caso el vto de octubre". Cambiado de un entero
    # fijo (5) a Optional[int] con None = "sin limite" (0 o sin setear la
    # variable de entorno tambien resuelve a None - mismo patron que
    # RiskLimitsConfig.max_daily_loss_ars/max_open_contracts_total mas
    # arriba). Este campo cumple DOS roles distintos, acoplados por ser el
    # mismo valor (ver strategy/weekly_asymmetric.py::scan_entry_signals y
    # risk/risk_manager.py::evaluate_position_exit):
    #   1) ENTRADA: descarta cualquier cotizacion cuyo vencimiento este a
    #      mas dias habiles que este valor - con None, ninguna cotizacion se
    #      descarta por este motivo (segun evidencia real en produccion, ver
    #      ggal_bot/data/live_shadow_feed.py::_refresh_individual_quotes,
    #      esto es exactamente lo que bloqueaba toda entrada nueva bajo
    #      GGAL_BOT_FORCE_EXPIRY=octubre: "10 validas en 2026-10-16
    #      (bloqueadas igual por horizonte semanal) vs. apenas 2 en
    #      2026-09-18" - Septiembre ya no tiene profundidad suficiente ni
    #      para llegar al piso de 3 cotizaciones para escanear).
    #   2) SALIDA: fuerza el cierre de una posicion abierta tras mantenerla
    #      este numero de dias habiles ("weekly_horizon_expired") - con
    #      None, esta salida especifica queda desactivada (nunca se
    #      dispara), dejando el resto de las salidas (Stop Loss/Take
    #      Profit/tiered SL/toma parcial/compresion de vega) intactas y
    #      operando exactamente igual que antes.
    #
    # ADVERTENCIA (NO resuelta por este cambio, ver weekend_theta_guard_
    # enabled inmediatamente abajo): quitar este limite NO implica que una
    # posicion pueda quedar abierta indefinidamente sin ningun corte
    # temporal - weekend_theta_guard_enabled es un mecanismo COMPLETAMENTE
    # INDEPENDIENTE (no lee este campo) que sigue forzando el cierre de
    # CUALQUIER posicion todos los viernes mientras su vencimiento no haya
    # llegado, sin importar cuantos dias lleve abierta. Ver la nota de
    # riesgo junto a ese flag.
    max_holding_business_days: Optional[int] = _env_int("GGAL_BOT_MAX_HOLDING_BUSINESS_DAYS", 0) or None

    # --- Piso de liquidez/vencimiento minimo para ENTRAR (MEJORA 2026-09-17) ---
    # BUG REAL VERIFICADO (ver analisis de logs del 2026-09-09/10 y del
    # export 2026-09-17T17-13_export.csv): con max_holding_business_days en
    # None (sin limite, AJUSTE 2026-09-07 de arriba), el bot puede entrar en
    # CUALQUIER vencimiento con profundidad, incluido uno demasiado cercano
    # para tener mercado real (evidencia real: vencimiento de esta semana
    # sin oferta, Octubre con spreads 2-3%, Diciembre con spreads del 50%).
    # Quitar el horizonte de arriba resolvio el problema de "no hay
    # suficientes quotes para escanear" pero reabrio este otro: nada impide
    # elegir una base sin mercado real simplemente porque paso el filtro de
    # dislocacion.
    #
    # Este campo es la contrapartida deliberada: un piso de dias HABILES a
    # vencimiento por DEBAJO del cual una cotizacion NUNCA se considera para
    # una entrada nueva, sin importar que tan atractiva luzca su
    # dislocacion de smile. Default None (sin piso, backward-compatible):
    # cualquiera que no configure esto ve el comportamiento de siempre. Se
    # deja a criterio explicito del usuario fijar el valor (ej. ~20-25 dias
    # habiles, discutido en la conversacion sobre el indicador SuperTrend AI
    # del 2026-09-10) - DATA INSUFFICIENT para fijar un default propio sin
    # que el usuario lo confirme.
    #
    # NO reemplaza risk_manager.check_liquidity() (spread/volumen del libro
    # vigente, ya aplicado en scan_entry_signals) - lo complementa: liquidez
    # de HOY puede estar bien y aun asi ser un vencimiento que va a perder
    # profundidad antes de poder salir con orden.
    min_business_days_to_expiry_for_entry: Optional[int] = (
        _env_int("GGAL_BOT_MIN_BUSINESS_DAYS_TO_EXPIRY_FOR_ENTRY", 0) or None
    )
    # NOTA DE RIESGO (leer junto con el cambio de arriba, 2026-09-07): este
    # flag sigue en True por defecto - fuerza el cierre de CUALQUIER
    # posicion todos los viernes cuyo vencimiento sea posterior a ese
    # viernes (ver risk/risk_manager.py::evaluate_position_exit, chequeo
    # "weekend_theta_guard"), sin importar el valor de
    # max_holding_business_days de arriba. Se agrego originalmente (ver
    # docs/auditoria del 2026-09-01) tras un caso real de -$133.568 de
    # decay overnight/de fin de semana no capturado a tiempo por el stop
    # fijo. Si el objetivo es sostener una posicion en un vencimiento mas
    # lejano (ej. octubre) A TRAVES de uno o mas fines de semana, este flag
    # la va a seguir cerrando cada viernes de todos modos - quitar el
    # horizonte de arriba NO alcanza por si solo para lograr ese objetivo.
    # Desactivarlo (GGAL_BOT_WEEKEND_THETA_GUARD=false) es una decision de
    # riesgo aparte, deliberadamente NO tomada en este mismo cambio sin
    # confirmacion explicita del usuario.
    weekend_theta_guard_enabled: bool = _env_bool("GGAL_BOT_WEEKEND_THETA_GUARD", True)
    # RESOLUCION de la contradiccion senalada arriba (TANDA 2 -
    # "OPTIMIZACION EJECUTABLE", seccion 3, 2026-09-08): VERIFICADO por
    # lectura de codigo (risk/risk_manager.py::evaluate_position_exit) que
    # el guard de arriba SI es, literalmente, un cierre general
    # incondicional - dispara todos los viernes para CUALQUIER posicion
    # cuyo vencimiento no haya llegado todavia, sin importar cuantos dias
    # lleva abierta ni que tan lejos este ese vencimiento (podria ser
    # Octubre). Esto vuelve inutil, en la practica, haber quitado el
    # limite de max_holding_business_days de arriba: ninguna posicion
    # sobrevive nunca a un fin de semana, sin importar el horizonte
    # "sin limite" configurado.
    #
    # Riesgo REAL que este guard protege (no se elimina, se ACOTA): el
    # caso documentado del 2026-09-01 (-$133.568 de decay overnight/fin de
    # semana no capturado a tiempo) ocurrio sobre una posicion RECIEN
    # ABIERTA, sin todavia la proteccion del Stop Loss escalonado
    # (tiered_stop_loss_*, MEJORA 2026-09-04, que en su defecto actual
    # angosta el stop ya desde el primer dia habil de holding, ver
    # tiered_stop_loss_stage2_business_day=1 mas abajo) - es decir, el
    # riesgo de fin de semana es mayor CUANTO MAS TEMPRANO en su vida esta
    # la posicion (todavia con el stop mas ancho), no despues de que ya
    # paso por las etapas mas angostas del stop escalonado.
    #
    # Politica nueva (configurable, backward-compatible): con este campo
    # en None (default), el comportamiento es IDENTICO al de siempre (el
    # guard sigue disparando sin excepcion, cero cambio de comportamiento
    # para quien no lo configure). Si se fija un numero de dias habiles,
    # el guard deja de aplicarse una vez que la posicion ya lleva ESE
    # numero de dias habiles abierta - reutiliza deliberadamente el mismo
    # concepto ya validado por el usuario en tiered_stop_loss_stage3_
    # business_day (dia a partir del cual el stop mas angosto, 20% por
    # default, ya esta protegiendo la posicion) en vez de inventar un
    # numero nuevo sin evidencia: una posicion que ya esta bajo el stop
    # mas angosto tiene, por construccion, una perdida maxima ya acotada
    # aunque el fin de semana la sorprenda. NO se fija un default distinto
    # de None aca (DATA INSUFFICIENT para elegir un numero de dias optimo
    # sin datos post Position Lifecycle Engine) - queda a criterio
    # explicito del usuario configurarlo via
    # GGAL_BOT_WEEKEND_THETA_GUARD_MAX_HOLDING_BUSINESS_DAYS.
    weekend_theta_guard_max_holding_business_days: Optional[int] = (
        _env_int("GGAL_BOT_WEEKEND_THETA_GUARD_MAX_HOLDING_BUSINESS_DAYS", 0) or None
    )

    # FIX 2026-09-29 (ver REPORT.md secciones 4.0 y 9.0, hallazgo verificado
    # por lectura de codigo): weekend_theta_guard_enabled (arriba) cierra
    # CUALQUIER posicion cuyo vencimiento no haya llegado todavia, todos los
    # viernes - pero scan_entry_signals() nunca supo que dia es, asi que
    # nada le impedia generar una entrada nueva un viernes sobre un
    # vencimiento posterior. Esa posicion recien abierta tiene
    # holding_business_days=0, lo que dispara el guard casi de inmediato en
    # el primer ciclo de riesgo siguiente (weekend_theta_guard_max_holding_
    # business_days=0 no lo evita: 0 < N es cierto para cualquier N>=1, y
    # con el default None el guard nunca tiene excepcion). En la muestra
    # real de Fase 0, esto produjo 194/199 (97.5%) de las entradas de
    # weekly_asymmetric: viernes abiertas y cerradas por este guard en una
    # mediana de 23 SEGUNDOS, cobrando el costo regulatorio de round-trip
    # completo (-609.077 ARS neto) sobre un PnL bruto de apertura/cierre
    # casi nulo (+9.904 ARS) - aproximadamente 85% de la perdida neta total
    # de la estrategia en la muestra.
    #
    # Esto NO afloja el guard (que sigue cerrando cualquier posicion ya
    # abierta un viernes, exactamente igual que siempre): esto evita ABRIR
    # una posicion nueva que el guard va a cerrar el mismo dia por
    # construccion. Opt-in, default False (cambio de comportamiento real
    # solo si se activa explicitamente vía
    # GGAL_BOT_WEEKEND_THETA_GUARD_BLOCK_NEW_ENTRIES) - se recomienda
    # activarlo dada la evidencia de arriba, pero queda a criterio explicito
    # del usuario (ver weekly_asymmetric.py::scan_entry_signals, parametro
    # `now`).
    weekend_theta_guard_block_new_entries: bool = _env_bool(
        "GGAL_BOT_WEEKEND_THETA_GUARD_BLOCK_NEW_ENTRIES", False
    )

    # --- Logger de embudo de señales (MEJORA 2026-09-29, ver REPORT.md §12.3 y
    # §12.5 punto 5) ---
    # A pedido explicito del usuario ("logger de embudo estructurado: universo
    # completo de candidatas por ciclo con spread, profundidad, griegas, delta
    # y que filtros paso cada una"), prioridad de despliegue junto con
    # market_snapshots.csv. Hasta esta mejora, `_log_entry_scan_diagnostics_if_due`
    # (run_bot.py) solo logueaba un RESUMEN AGREGADO por logger.info() (conteos
    # por filtro, nunca persistido) - suficiente para responder "¿cuantas se
    # descartaron por moneyness?" pero no "¿CUALES, con que spread/griegas
    # exactas, y en que orden de filtros?" - lo segundo es lo que hace falta
    # para poder recalibrar un umbral offline sin esperar dias de shadow.
    # Opt-in, default False: cuando esta activo, scan_entry_signals() llena
    # EntryScanDiagnostics.candidate_funnel (ver weekly_asymmetric.py,
    # CandidateFunnelRecord) con UN registro POR CANDIDATA evaluada este ciclo
    # (calificada o no, con el nombre del primer filtro que la descarto);
    # run_bot.py persiste esa lista a logs/signal_funnel.csv via
    # ggal_bot.data.signal_funnel_log.SignalFunnelLogger, analogo a
    # MarketSnapshotLogger. Con el flag apagado, el costo adicional es CERO
    # (candidate_funnel queda vacio, ni siquiera se instancian los registros).
    enable_signal_funnel_log: bool = _env_bool("GGAL_BOT_ENABLE_SIGNAL_FUNNEL_LOG", False)

    # --- Salida forzada, medida sobre la PRIMA pagada (no sobre el subyacente) ---
    stop_loss_pct: float = _env_float("GGAL_BOT_STOP_LOSS_PCT", 0.50)     # -50% de la prima -> cerrar
    take_profit_pct: float = _env_float("GGAL_BOT_TAKE_PROFIT_PCT", 1.00)  # +100% de la prima -> cerrar

    # --- Stop Loss escalonado por dia habil (MEJORA 2026-09-04) ---
    # Angosta progresivamente stop_loss_pct de arriba a medida que pasan
    # los dias habiles desde la entrada, en vez de sostener el mismo -50%
    # fijo durante las 5 ruedas del horizonte semanal completo (ver
    # risk.risk_manager.RiskManager.evaluate_position_exit para la
    # justificacion completa, motivada por el analisis del export de
    # trades del 01-04/09/2026: 16 posiciones multi-dia perdieron hasta
    # -33.5% sin que el stop fijo de -50% las frenara a tiempo). Default
    # enabled=True (mejora activa por default; poner en false via env
    # restaura el stop_loss_pct fijo de siempre).
    # AJUSTE DE RIESGO 2026-09-07 (post Fase 5.3, a pedido explicito del
    # usuario): tiered_stop_loss_stage2_business_day adelantado de 2 a 1.
    # Motivo, tal como lo planteo el usuario: el hallazgo de Fase 5.1
    # (GFGC7600OC: overnight catastrofico -$133.568 corregido vs. intradia
    # casi neutro -$1.334, ver AUDITORIA_FASE5.1_RECOVERY_FORENSICS.md
    # seccion F) demuestra que el riesgo se concentra en ATRAVESAR UNA
    # NOCHE, no especificamente en "haber pasado 2 dias habiles". Con
    # `evaluate_position_exit()` (risk_manager.py) usando
    # `holding_business_days >= tiered_stop_loss_stage2_business_day`,
    # bajar el umbral de 2 a 1 hace que la etapa 2 (35%) se active ya
    # despues del PRIMER overnight (holding_business_days>=1), en vez de
    # recien despues del segundo - preserva intacto el stop_loss_pct del
    # dia de entrada (0.50, sin overnight todavia) en vez de tocarlo
    # directamente, que era la alternativa considerada y descartada
    # (destruye la protección más holgada del día 0 sin necesidad, ya que
    # el dia de entrada en si no tiene overnight).
    #
    # EXPLICITAMENTE NO VALIDADO COMO OPTIMO (ver discusion del usuario,
    # 2026-09-07): "no tenemos evidencia suficiente para afirmar que 35%
    # sea el SL optimo" - esto se trata como una politica de riesgo
    # parametrizada (feature-flag friendly via env var), no como una
    # conclusion estadistica. Medir su efecto real REQUIERE datos post
    # Position Lifecycle Engine (Fase 5.3, ya desplegado) para no repetir
    # el error de medir fragmentos FIFO como si fueran trades
    # independientes (ver AUDITORIA_FASE5.1_RECOVERY_FORENSICS.md seccion E).
    enable_tiered_stop_loss: bool = _env_bool("GGAL_BOT_ENABLE_TIERED_STOP_LOSS", True)
    tiered_stop_loss_stage2_business_day: int = _env_int("GGAL_BOT_TIERED_SL_STAGE2_DAY", 1)
    tiered_stop_loss_stage2_pct: float = _env_float("GGAL_BOT_TIERED_SL_STAGE2_PCT", 0.35)
    tiered_stop_loss_stage3_business_day: int = _env_int("GGAL_BOT_TIERED_SL_STAGE3_DAY", 4)
    tiered_stop_loss_stage3_pct: float = _env_float("GGAL_BOT_TIERED_SL_STAGE3_PCT", 0.20)

    # --- Toma de ganancia parcial (MEJORA 2026-09-04) ---
    # Asegura una fraccion de la posicion apenas el PnL% no realizado supera
    # partial_profit_trigger_pct, en vez de esperar el +100% de Take Profit
    # (nunca tocado en el export de trades del 01-04/09/2026 - el mejor
    # resultado individual fue +8.82%). Se toma UNA sola vez por posicion
    # (ver Position.partial_profit_taken); el resto queda como "runner"
    # sujeto a las mismas reglas de siempre (ver risk_manager.
    # evaluate_partial_profit_take y WeeklyAsymmetricStrategy.
    # build_exit_signals). Con quantity < 2 no se aplica (no hay fraccion
    # posible que deje un runner).
    enable_partial_profit_take: bool = _env_bool("GGAL_BOT_ENABLE_PARTIAL_PROFIT_TAKE", True)
    partial_profit_trigger_pct: float = _env_float("GGAL_BOT_PARTIAL_PROFIT_TRIGGER_PCT", 0.15)
    partial_profit_take_fraction: float = _env_float("GGAL_BOT_PARTIAL_PROFIT_TAKE_FRACTION", 0.50)

    # --- Filtro de entrada: convexidad / moneyness / confirmacion de nivel ---
    smile_threshold_vol_points: float = _env_float("GGAL_BOT_LONGFIRST_SMILE_THRESHOLD", 3.0)
    moneyness_band_pct: float = _env_float("GGAL_BOT_MONEYNESS_BAND_PCT", 0.15)  # |log(K/S)| maximo considerado
    require_level_confirmation: bool = _env_bool("GGAL_BOT_REQUIRE_LEVEL_CONFIRMATION", False)
    level_threshold_vol_points: float = _env_float("GGAL_BOT_LONGFIRST_LEVEL_THRESHOLD", 5.0)

    # --- Filtro de entrada ADICIONAL por banda de delta (MEJORA 2026-09-17) ---
    # A pedido explicito del usuario (conversacion sobre el indicador
    # SuperTrend AI, 2026-09-10): "en vez de fijar un strike nominal (que se
    # desactualiza si el spot se mueve), yo apuntaria a una banda de delta
    # (ej. 0.40-0.55 para la opcion comprada) - mantiene la exposicion
    # consistente mes a mes". El delta ya se calculaba (ver
    # models/black_scholes.py::Greeks.delta) pero solo se usaba para
    # rankear candidatas por convexidad (convexity_score) - nunca como
    # filtro de admision.
    #
    # Deliberadamente ADITIVO (se suma al filtro de moneyness existente, no
    # lo reemplaza) y apagado por defecto (`enabled=False`): a diferencia de
    # la Mejora 1 (vol_arbitrage) esto NO corrige un bug ya demostrado, es
    # un cambio de criterio de seleccion de strike que el usuario todavia no
    # confirmo que quiere en produccion. `delta_band_min`/`delta_band_max`
    # se comparan contra abs(delta) (un put ATM tiene delta negativo, la
    # banda se piensa en magnitud, igual que se la describe habitualmente en
    # la jerga de opciones).
    enable_delta_band_filter: bool = _env_bool("GGAL_BOT_ENABLE_DELTA_BAND_FILTER", False)
    delta_band_min: float = _env_float("GGAL_BOT_DELTA_BAND_MIN", 0.40)
    delta_band_max: float = _env_float("GGAL_BOT_DELTA_BAND_MAX", 0.55)

    # --- Spreads (Bull Call / Bear Put): pata corta solo tras la larga confirmada ---
    enable_spread_completion: bool = _env_bool("GGAL_BOT_ENABLE_SPREAD_COMPLETION", True)
    spread_wing_moneyness_pct: float = _env_float("GGAL_BOT_SPREAD_WING_MONEYNESS_PCT", 0.05)

    # --- Spread de DEBITO como entrada nueva cuando la IV esta CARA (MEJORA 2026-09-17) ---
    # A pedido explicito del usuario (conversacion sobre el indicador
    # SuperTrend AI, 2026-09-10): hasta esta mejora, una base con
    # dislocacion de smile POSITIVA (IV cara respecto de la curva) nunca
    # generaba ninguna señal - scan_entry_signals() solo actua sobre
    # dislocaciones negativas (IV barata). scan_spread_completion_signals()
    # existe hace tiempo pero SOLO financia una pata larga YA CONFIRMADA en
    # el portafolio (uso defensivo/de cap) - nunca se disparaba *porque* la
    # IV estuviera cara. Esta mejora agrega esa rama faltante:
    # WeeklyAsymmetricStrategy.scan_expensive_iv_spread_signals() arma AMBAS
    # patas de un spread de debito (comprar el strike cercano, vender uno
    # mas OTM via _find_wing_quote, reutilizado tal cual) como ENTRADA
    # NUEVA, cuando la dislocacion supera este umbral en sentido "cara".
    #
    # Deliberadamente apagado por defecto (`enabled=False`): es la mejora
    # mas especulativa de esta tanda (dos ordenes coordinadas en vez de una,
    # superficie de ejecucion nueva) - el usuario debe confirmarla
    # explicitamente despues de revisar el codigo, no arrancar activa sola.
    # `expensive_iv_spread_threshold_vol_points` usa el mismo default
    # (3.0) que VolatilityArbitrageStrategy.smile_threshold_vol_points para
    # "cara" (ver strategy/vol_arbitrage.py) - mismo umbral ya usado en el
    # bot para esa lectura, no un numero inventado nuevo.
    enable_expensive_iv_spread_entry: bool = _env_bool("GGAL_BOT_ENABLE_EXPENSIVE_IV_SPREAD_ENTRY", False)
    expensive_iv_spread_threshold_vol_points: float = _env_float(
        "GGAL_BOT_EXPENSIVE_IV_SPREAD_THRESHOLD", 3.0
    )

    # --- Salida por reversion de tendencia (MEJORA 2026-09-17, misma tanda) ---
    # A pedido explicito del usuario (conversacion sobre el indicador
    # SuperTrend AI, 2026-09-10: "si la tendencia se da vuelta en contra de
    # la posicion, hay que salir aunque el stop de prima todavia no se haya
    # tocado"). Hasta esta mejora, build_exit_signals() solo cerraba una
    # posicion por Stop Loss/Take Profit/horizonte/guardia de fin de
    # semana/compresion de vega - todas medidas sobre la PRIMA o el
    # calendario, nunca sobre si la tesis direccional que motivo la entrada
    # (ver Position.trend_at_entry, poblado en run_bot.py:
    # _act_on_entry_signal desde EntrySignal.trend_context) segui vigente.
    #
    # Definicion deliberadamente ESTRICTA (ver
    # WeeklyAsymmetricStrategy._trend_has_reversed): solo dispara cuando la
    # tendencia vigente paso al EXTREMO CONTRARIO del que motivo la entrada
    # (BULLISH->BEARISH para una CALL, BEARISH->BULLISH para una PUT) - una
    # lectura NEUTRAL de por medio (fading, no reversion confirmada) NO
    # dispara esta salida, para no cerrar posiciones sanas ante ruido de
    # corto plazo del filtro tecnico. Una posicion abierta bajo NEUTRAL
    # (dislocacion extrema, sin tesis direccional) tampoco puede disparar
    # esta salida (no hay tendencia de entrada de la cual "reversar").
    #
    # Apagado por defecto (`enabled=False`): cambia CUANDO se cierra una
    # posicion ya ganadora en Griegas/prima segun el filtro tecnico vigente,
    # el usuario debe confirmarlo explicitamente antes de que rija en
    # produccion (mismo criterio que enable_delta_band_filter/
    # enable_expensive_iv_spread_entry de arriba).
    enable_trend_reversal_exit: bool = _env_bool("GGAL_BOT_ENABLE_TREND_REVERSAL_EXIT", False)

    # --- Confirmacion de microestructura (ver models/microstructure.py) ---
    # Order Book Imbalance = (bid_size - ask_size) / (bid_size + ask_size).
    # Filtro de CALIDAD DE EJECUCION (no de alpha direccional): descarta una
    # base si el libro muestra un desbalance extremo hacia el lado vendedor
    # (ask_size >> bid_size), tipico de una punta aislada/iliquida en un
    # libro delgado como el de opciones de GGAL, no necesariamente
    # informacion genuina de precio. min_obi_for_entry=-0.30 solo bloquea
    # el 30% mas desbalanceado hacia el lado vendedor; no exige apoyo
    # comprador, solo evita el peor caso.
    enable_obi_filter: bool = _env_bool("GGAL_BOT_ENABLE_OBI_FILTER", True)
    min_obi_for_entry: float = _env_float("GGAL_BOT_MIN_OBI_FOR_ENTRY", -0.30)

    # --- Salida por compresion de Vega (convexidad agotada) ---
    # Complementa (no reemplaza) Stop Loss/Take Profit/horizonte semanal: si
    # el |vega| actual de la posicion cayo por debajo de este porcentaje del
    # |vega| que tenia al momento de la entrada, la tesis de convexidad que
    # motivo la compra ya se agoto (la opcion dejo de ser sensible a la vol)
    # aunque el PnL% de la prima todavia no dispare Stop Loss/Take Profit -
    # se cierra para no seguir pagando theta por una posicion que ya no
    # aporta la convexidad que se buscaba.
    enable_vega_decay_exit: bool = _env_bool("GGAL_BOT_ENABLE_VEGA_DECAY_EXIT", True)
    # FLEXIBILIZADO 2026-09-04 a pedido explicito del usuario: esta salida
    # estaba generando muchos cierres con PnL bajo (el vega de una opcion
    # cercana al dinero se comprime rapido con apenas un movimiento del
    # subyacente en las primeras horas de vida de la posicion, sin que la
    # tesis de convexidad haya fallado de verdad todavia). Dos cambios:
    # (1) el ratio bajo de 0.35 a 0.20 (exige una compresion de vega mucho
    # mas profunda antes de forzar el cierre); (2) un tiempo minimo de
    # tenencia (vega_decay_min_holding_hours) antes de que esta salida
    # pueda dispararse en absoluto - ver risk_manager.evaluate_vega_decay_exit.
    vega_decay_exit_ratio: float = _env_float("GGAL_BOT_VEGA_DECAY_EXIT_RATIO", 0.20)
    vega_decay_min_holding_hours: float = _env_float("GGAL_BOT_VEGA_DECAY_MIN_HOLDING_HOURS", 3.0)

    # --- Filtro de dislocacion RELATIVA por z-score (MEJORA 2026-09-28) ---
    # A pedido explicito del usuario ("mejor trader quant... exprime tu
    # capacidad al maximo"). smile_threshold_vol_points (arriba) es un
    # umbral FIJO en puntos de vol absolutos: no distingue una base que
    # SIEMPRE tiene 2-3 vol pts de ruido de smile de una que de golpe se
    # desvio muy por fuera de su propio comportamiento reciente - la
    # segunda es la candidata mas fuerte a una dislocacion genuina, la
    # primera es solo ruido estructural de ese book en particular. Mismo
    # mecanismo que YA existe (ver data/iv_mean_reversion.py:
    # IVMeanReversionTracker) para la salida de reversion de Scalping, pero
    # ahi deliberadamente confinado a ese modo para no romper el diseño
    # stateless de WeeklyAsymmetricStrategy (ver su docstring). Esta mejora
    # lo trae TAMBIEN como filtro de ENTRADA para weekly_asymmetric, con el
    # mismo patron de inyeccion que `trend`/`momentum_shift` (el estado del
    # tracker vive en run_bot.py, WeeklyAsymmetricStrategy solo recibe el
    # z-score ya calculado - ver GgalOptionsBot._dislocation_tracker):
    # SE SUMA al umbral fijo existente, nunca lo reemplaza - una base debe
    # seguir pasando smile_threshold_vol_points Y, si esta habilitado,
    # tener ademas un z-score por debajo de -zscore_threshold contra su
    # propia ventana reciente. Apagado por defecto: DATA INSUFFICIENT para
    # calibrar zscore_threshold sin datos propios de este filtro todavia.
    enable_zscore_filter: bool = _env_bool("GGAL_BOT_ENABLE_ZSCORE_FILTER", False)
    zscore_window_seconds: float = _env_float("GGAL_BOT_ZSCORE_WINDOW_SECONDS", 1800.0)
    zscore_min_samples: int = _env_int("GGAL_BOT_ZSCORE_MIN_SAMPLES", 10)
    zscore_threshold: float = _env_float("GGAL_BOT_ZSCORE_THRESHOLD", 1.5)

    # --- Sizing por CONVICCION de la señal (MEJORA 2026-09-28) ---
    # A pedido explicito del usuario. Hoy risk/position_sizer.py::
    # PositionSizer asigna la MISMA fraccion de capital
    # (max_risk_pct_per_trade) a toda señal que califica, sin importar si la
    # dislocacion de IV es apenas la minima exigida o mucho mas extrema.
    # Con esto habilitado, el capital asignado se escala por
    # |dislocacion| / conviction_sizing_reference_vol_points, acotado entre
    # conviction_sizing_min_multiplier y conviction_sizing_max_multiplier
    # (ver PositionSizer.conviction_multiplier_for). None en
    # conviction_sizing_reference_vol_points (default) cae a
    # smile_threshold_vol_points de arriba: una señal que recien alcanza el
    # umbral minimo queda en 1.0x (sizing identico al actual), y solo una
    # dislocacion MAS extrema que el umbral escala hacia arriba - nunca
    # hacia abajo del baseline actual salvo que se configure explicitamente
    # un min_multiplier < 1.0. Apagado por defecto: DATA INSUFFICIENT para
    # calibrar el rango optimo sin el historial de win-rate por magnitud de
    # dislocacion que el logger de mercado (MEJORA 2026-09-28, ver
    # data/market_snapshot_log.py) recien empieza a construir.
    enable_conviction_sizing: bool = _env_bool("GGAL_BOT_ENABLE_CONVICTION_SIZING", False)
    conviction_sizing_reference_vol_points: Optional[float] = (
        _env_float("GGAL_BOT_CONVICTION_SIZING_REFERENCE_VOL_POINTS", 0.0) or None
    )
    conviction_sizing_min_multiplier: float = _env_float("GGAL_BOT_CONVICTION_SIZING_MIN_MULTIPLIER", 0.5)
    conviction_sizing_max_multiplier: float = _env_float("GGAL_BOT_CONVICTION_SIZING_MAX_MULTIPLIER", 1.5)

    # --- Costo de ejecucion estimado (MEJORA 2026-09-28) ---
    # A pedido explicito del usuario. El libro de opciones de GGAL en BYMA
    # es delgado: check_liquidity() (risk/risk_manager.py) hoy es un
    # pasa/no-pasa binario (spread relativo y tamaño minimo de punta), sin
    # estimar CUANTO costaria realmente cruzar ese spread. Este filtro
    # estima un costo en % de la prima (mitad del spread relativo, que se
    # paga siempre al cruzar al ask; mas un termino de impacto que crece
    # cuando el tamaño de punta mostrado es chico, proxy de cuanto se
    # moveria el precio si el tamaño real operado excede lo mostrado - ver
    # WeeklyAsymmetricStrategy._estimate_execution_cost_pct) y descarta la
    # señal si ese costo estimado supera execution_cost_max_pct de la prima.
    # LIMITACION EXPLICITA: al momento del scan todavia no se conoce la
    # cantidad de contratos final (eso lo decide PositionSizer despues, con
    # el capital disponible real) - el termino de impacto asume el peor
    # caso razonable (1 contrato contra el tamaño de punta mostrado), no un
    # calculo exacto contra la cantidad que se vaya a pedir. Apagado por
    # defecto.
    enable_execution_cost_filter: bool = _env_bool("GGAL_BOT_ENABLE_EXECUTION_COST_FILTER", False)
    execution_cost_impact_coefficient: float = _env_float("GGAL_BOT_EXECUTION_COST_IMPACT_COEFFICIENT", 0.02)
    execution_cost_max_pct: float = _env_float("GGAL_BOT_EXECUTION_COST_MAX_PCT", 0.08)

    # --- Filtro cruzado ADR (NYSE: GGAL) / dolar CCL implicito (MEJORA 2026-09-28) ---
    # A pedido explicito del usuario, marcado HYPOTHESIS (no VERIFIED): el
    # ADR cotiza en USD y cierra en un horario distinto al de BYMA, asi que
    # un movimiento fuerte del ADR (ajustado por el CCL implicito) durante
    # la noche PODRIA anticipar el gap de apertura local - pero este codigo
    # NO integra ninguna fuente de datos en vivo del ADR/CCL (no hay forma
    # de verificar un endpoint real sin credenciales, y fabricar una
    # integracion sin poder probarla contra el proveedor real seria
    # exactamente el tipo de dato inventado que este proyecto evita). Lo que
    # se agrega aca es el PUNTO DE EXTENSION: si algun llamador futuro
    # inyecta una lectura ("BULLISH"/"BEARISH"/"NEUTRAL", igual que `trend`)
    # y esta habilitado, un option_type que CONTRADIGA esa lectura exige el
    # umbral EXTREMO en vez del normal (mismo patron ya usado para NEUTRAL/
    # Momentum Shift) en lugar de bloquearse de plano - nunca reemplaza al
    # filtro de `trend` existente, solo lo endurece cuando ambas lecturas
    # discrepan. Apagado por defecto (y sin ningun efecto mientras nadie
    # inyecte `adr_ccl_trend` real).
    enable_adr_ccl_filter: bool = _env_bool("GGAL_BOT_ENABLE_ADR_CCL_FILTER", False)

    # --- Blackout de earnings / eventos conocidos (MEJORA 2026-09-28) ---
    # A pedido explicito del usuario. La IV de GGAL casi seguro tiene una
    # prima de evento antes de resultados trimestrales de Grupo Financiero
    # Galicia que se desinfla despues del anuncio - una base "barata" un dia
    # antes de earnings puede estar barata PORQUE el mercado todavia no
    # precio el evento, no por una ineficiencia real. earnings_dates queda
    # deliberadamente VACIO por defecto (ver _env_date_list): no hay forma
    # de conocer con certeza, desde este codigo, el calendario real de
    # resultados - cargarlo es responsabilidad explicita del usuario via
    # GGAL_BOT_EARNINGS_DATES ("2026-11-06,2027-02-19", ISO separado por
    # coma). Con la lista vacia (default), este filtro es un no-op completo
    # aunque enable_earnings_blackout este en True. Bloquea TODA entrada
    # nueva (no solo el lado que perdiera con el evento) durante la ventana,
    # porque la prima de evento puede inflar la IV de ambos lados del
    # smile por igual.
    enable_earnings_blackout: bool = _env_bool("GGAL_BOT_ENABLE_EARNINGS_BLACKOUT", False)
    earnings_dates: Tuple[date, ...] = field(default_factory=lambda: _env_date_list("GGAL_BOT_EARNINGS_DATES"))
    earnings_blackout_days_before: int = _env_int("GGAL_BOT_EARNINGS_BLACKOUT_DAYS_BEFORE", 2)


@dataclass
class VolArbitrageConfig:
    """
    Gestion de riesgo para el modo "vol_arbitrage" (arbitraje de volatilidad
    delta-neutral original, ver strategy/vol_arbitrage.py) - NO-GO de
    produccion desde 2026-09-08 (ver run_bot.py.__init__), solo corre en
    shadow.

    BUG REAL VERIFICADO (2026-09-16/17, ver analisis del export
    2026-09-17T17-13_export.csv y confirmacion por log de produccion):
    _run_vol_arbitrage_cycle() (run_bot.py) UNICAMENTE escaneaba señales de
    entrada - nunca evaluaba ninguna condicion de salida sobre posiciones ya
    abiertas. VolatilityArbitrageStrategy.scan_for_signals() SI emite una
    señal "sell" cuando la IV se encarece, pero _act_on_signal() la
    descartaba sin mas en cuanto ya existia una posicion en esa base
    (Guarda 2) - el propio docstring de _act_on_signal ya admitia esto como
    TODO ("cerrar la posicion cuando la sonrisa se normalice sigue siendo un
    TODO aparte"). Ademas, esas posiciones se guardaban con
    Position.strategy_tag=None, que "por convencion" se trata como
    "weekly_asymmetric" en el resto del bot (ver _act_on_signal) - es decir,
    quedaban invisibles para SU PROPIA estrategia pero potencialmente
    adoptables por otra, un estado ambiguo real (ver fix de tag explicito
    en _act_on_signal).

    Consecuencia real (shadow, no produccion real): GFGC8000OC (6 lotes
    comprados 2026-09-02/03, 58 contratos en total) quedo sin ningun stop
    ni horizonte durante ~13 dias mientras la prima colapsaba de ~163-186 a
    55.24 (-66% a -70%), hasta que un stop_loss disparo el 2026-09-16
    10:47:18 UTC (log verificado: "Salida GFGC8000OC [reason=stop_loss]...
    requested_qty=58.00") recien cuando la posicion volvio a tener
    cotizacion vigente evaluable. Perdida (shadow): -$681.053.

    Esta config agrega el mismo mecanismo de proteccion ya validado en
    LongFirstConfig (Stop Loss/Take Profit sobre la prima, horizonte de
    dias habiles, guardia de fin de semana), reutilizando
    RiskManager.evaluate_position_exit() tal cual - deliberadamente MINIMO
    (sin tiered stop ni toma de ganancia parcial: vol_arbitrage no tiene la
    misma disciplina direccional que weekly_asymmetric, el objetivo aca es
    que ninguna posicion quede sin NINGUN corte, no replicar el motor de
    salida completo del otro modo).

    `enabled=True` por defecto: a diferencia de otros flags nuevos de este
    archivo (que preservan comportamiento existente apagados), esto CORRIGE
    un bug de riesgo real ya demostrado con evidencia - dejarlo apagado por
    defecto reproduciria el mismo problema para cualquiera que reactive
    vol_arbitrage sin conocer este historial.
    """
    enabled: bool = _env_bool("GGAL_BOT_VOL_ARBITRAGE_ENABLE_EXIT_MANAGEMENT", True)
    stop_loss_pct: float = _env_float("GGAL_BOT_VOL_ARBITRAGE_STOP_LOSS_PCT", 0.50)
    take_profit_pct: float = _env_float("GGAL_BOT_VOL_ARBITRAGE_TAKE_PROFIT_PCT", 1.00)
    # None = sin limite (mismo default historico que LongFirstConfig.
    # max_holding_business_days) - DATA INSUFFICIENT para fijar un numero de
    # dias optimo sin evidencia propia de este modo; queda a criterio
    # explicito del usuario configurarlo.
    max_holding_business_days: Optional[int] = (
        _env_int("GGAL_BOT_VOL_ARBITRAGE_MAX_HOLDING_BUSINESS_DAYS", 0) or None
    )
    weekend_theta_guard_enabled: bool = _env_bool("GGAL_BOT_VOL_ARBITRAGE_WEEKEND_THETA_GUARD", True)

    # --- Cooldown de reentrada tras stop_loss (MEJORA 2026-09-17, misma tanda) ---
    # A pedido explicito del usuario ("revisar el codigo... y analizar a
    # fondo como mejorar la estrategia del bot", sobre el patron de
    # reentradas de GFGC7600OC en el export de trades 2026-09-17T17-13:
    # 54 operaciones, -$121.233,10, muchas con duraciones de apenas 20-30
    # segundos). VERIFICADO por lectura de codigo que esas duraciones tan
    # cortas NO pueden explicarse por vega_decay_exit (exige
    # vega_decay_min_holding_hours=3.0, muy por encima de 20-30s) ni por
    # build_exit_signals() de weekly_asymmetric "adoptando" la posicion
    # (ese ciclo NUNCA corre mientras GGAL_BOT_ACTIVE_STRATEGY=vol_arbitrage
    # - ver run_bot.py.recompute_cycle) - el cierre real de esas 54
    # operaciones historicas es, con la evidencia disponible, DATA
    # INSUFFICIENT para atribuirlo con certeza (lo mas probable, dado que
    # antes de esta misma tanda de mejoras vol_arbitrage no tenia NINGUNA
    # salida automatica, es intervencion manual del usuario).
    #
    # Lo que SI cambia con esta tanda de mejoras: _check_vol_arbitrage_exits
    # (arriba) ahora SI cierra automaticamente por stop_loss. Como
    # VolatilityArbitrageStrategy.scan_for_signals() re-emite la MISMA
    # dislocacion persistente todos los ciclos mientras la sonrisa no se
    # corrija (ver docstring de run_bot.py._act_on_signal), sin este
    # cooldown el bot podria reabrir la base recien stopeada en el
    # ciclo INMEDIATAMENTE siguiente - encadenando stops contra una IV que
    # probablemente todavia no se normalizo, un patron de reentrada nuevo
    # que esta mejora previene. None (default) = sin cooldown, idéntico al
    # comportamiento de _check_vol_arbitrage_exits recien agregado (esta
    # mejora es un AJUSTE FINO de esa otra, no un cambio de comportamiento
    # independiente) - DATA INSUFFICIENT para fijar un numero de segundos
    # optimo sin datos propios de este modo tras la Mejora 1; queda a
    # criterio explicito del usuario configurarlo.
    reentry_cooldown_seconds: Optional[float] = (
        _env_float("GGAL_BOT_VOL_ARBITRAGE_REENTRY_COOLDOWN_SECONDS", 0.0) or None
    )


# ---------------------------------------------------------------------------
# Modo "Scalping Intradia y Trading Semanal de Corto Plazo" (a pedido
# explicito del usuario, 2026-09-03 - ver strategy/scalping.py,
# data/intraday_bars.py, data/iv_mean_reversion.py).
#
# DECISION DE ARQUITECTURA DELIBERADA (leer antes de tocar este bloque o
# GGAL_BOT_ACTIVE_STRATEGY): este modo NO es un valor mas de
# StrategyConfig.active/VALID_STRATEGIES de mas abajo. El usuario pidio
# explicitamente que fuera "un modo nuevo aparte" que deje la posicion viva
# de Octubre bajo weekly_asymmetric (GGAL_BOT_FORCE_EXPIRY=2026-10-16)
# "como esta". Como run_bot.py.GgalOptionsBot solo instancia UNA estrategia
# principal segun StrategyConfig.active (ver mas abajo), agregar "scalping"
# ahi REEMPLAZARIA a weekly_asymmetric por completo - apagando la gestion
# de esa posicion de Octubre (Stop Loss/Take Profit/horizonte semanal/
# guardia de fin de semana dejarian de evaluarse). En cambio, este modo es
# un modulo ADITIVO gateado por su PROPIO flag independiente (`enabled`
# abajo / GGAL_BOT_ENABLE_SCALPING, default False): cuando esta prendido,
# GgalOptionsBot corre self._run_scalping_cycle() SIEMPRE DESPUES del ciclo
# de la estrategia principal (sea weekly_asymmetric o vol_arbitrage), en la
# MISMA recompute_cycle(), con su propio capital (max_capital_ars abajo,
# pool SEPARADO del de LongFirstConfig), su propio position sizer, sus
# propias posiciones (marcadas Position.strategy_tag="scalping" - ver
# portfolio/portfolio.py) y sus propias reglas de entrada/salida. Con
# GGAL_BOT_ENABLE_SCALPING sin setear (default False), absolutamente nada
# de este bloque tiene ningun efecto - el bot se comporta exactamente igual
# que antes de este modulo.
#
# Reutiliza WeeklyAsymmetricStrategy.scan_entry_signals() por COMPOSICION
# (no herencia) para el escaneo de entradas - ver strategy/scalping.py -
# por eso varios campos de aca abajo tienen deliberadamente el MISMO nombre
# que sus equivalentes en LongFirstConfig (smile_threshold_vol_points,
# moneyness_band_pct, max_holding_business_days, enable_obi_filter,
# min_obi_for_entry, require_level_confirmation, level_threshold_vol_points):
# ese metodo ya es generico/inyectable (no depende de LongFirstConfig en
# particular, solo de esos nombres de atributo en self.cfg), asi que no
# hace falta duplicar esa logica de filtrado.
# ---------------------------------------------------------------------------

@dataclass
class ScalpingConfig:
    # Interruptor maestro (ver nota de arquitectura arriba): modulo ADITIVO,
    # apagado por defecto.
    enabled: bool = _env_bool("GGAL_BOT_ENABLE_SCALPING", False)

    # --- Capital y sizing dinamico (pool SEPARADO del de weekly_asymmetric -
    # ver run_bot.py:_capital_available_ars()/PositionSizer): mismo capital
    # total que weekly_asymmetric ($1.000.000 ARS por defecto) pero
    # repartido en MAS trades de MENOR tamaño (max_risk_pct_per_trade mas
    # chico que el 20% default de LongFirstConfig) para poder sostener
    # varias posiciones de scalping concurrentes sin concentrar todo el
    # capital en una sola - ver max_concurrent_positions abajo.
    max_capital_ars: float = _env_float("GGAL_BOT_SCALPING_MAX_CAPITAL_ARS", 1_000_000.0)
    max_risk_pct_per_trade: float = _env_float("GGAL_BOT_SCALPING_MAX_RISK_PCT_PER_TRADE", 0.08)
    min_contracts_per_trade: int = _env_int("GGAL_BOT_SCALPING_MIN_CONTRACTS_PER_TRADE", 1)
    max_concurrent_positions: int = _env_int("GGAL_BOT_SCALPING_MAX_CONCURRENT_POSITIONS", 6)

    # --- Filtro de entrada (mismos nombres de atributo que LongFirstConfig -
    # ver nota de arriba) ---
    smile_threshold_vol_points: float = _env_float("GGAL_BOT_SCALPING_SMILE_THRESHOLD", 2.0)
    moneyness_band_pct: float = _env_float("GGAL_BOT_SCALPING_MONEYNESS_BAND_PCT", 0.10)
    # Horizonte de ELEGIBILIDAD de entrada (dias habiles al vencimiento) -
    # NO confundir con el horizonte de SALIDA (max_holding_minutes abajo,
    # en minutos): esto solo filtra que bases se consideran para abrir
    # (bases de vencimiento muy lejano no sirven para scalping de alta
    # convexidad), la decision de CUANDO cerrar una posicion ya abierta es
    # enteramente independiente y se mide en minutos (o el cierre EOD, ver
    # eod_close_enabled/eod_close_time mas abajo - NINGUNO de los dos lee
    # este campo).
    #
    # AJUSTE 2026-09-07, a pedido explicito del usuario (mismo pedido que
    # LongFirstConfig.max_holding_business_days arriba en el archivo, ver
    # esa nota para el detalle completo/evidencia de produccion): cambiado
    # de un entero fijo (3) a Optional[int] con None = "sin limite" (0 o
    # sin setear la variable de entorno tambien resuelve a None). A
    # diferencia de weekly_asymmetric, para Scalping esto es un cambio
    # LIMPIO sin ninguna contrapartida de riesgo escondida: este campo NUNCA
    # se usó para decidir cuando cerrar una posicion (eso ya es
    # completamente independiente, en minutos + cierre EOD diario) - quitar
    # el limite solo amplia que bases son candidatas para abrir (ej.
    # octubre en vez de solo los proximos 3 dias habiles), sin tocar en
    # absoluto la disciplina intradia de salida.
    max_holding_business_days: Optional[int] = _env_int("GGAL_BOT_SCALPING_MAX_HOLDING_BUSINESS_DAYS", 0) or None
    # Mismo mecanismo que LongFirstConfig.min_business_days_to_expiry_for_entry
    # (mismo nombre de atributo, ver nota de "Filtro de entrada" arriba de
    # esta clase - scan_entry_signals es generico sobre self.cfg). Default
    # None (sin piso): Scalping ya filtra por minutos/cierre EOD del lado de
    # salida, asi que este piso es opcional aca, no una correccion de un bug
    # ya demostrado como en weekly_asymmetric.
    min_business_days_to_expiry_for_entry: Optional[int] = (
        _env_int("GGAL_BOT_SCALPING_MIN_BUSINESS_DAYS_TO_EXPIRY_FOR_ENTRY", 0) or None
    )
    # Mismo mecanismo que LongFirstConfig.enable_delta_band_filter (mismo
    # nombre de atributo, ver nota de "Filtro de entrada" arriba de esta
    # clase). Default apagado: un scalp de minutos ya elige por moneyness
    # estrecho (moneyness_band_pct=0.10 arriba), no hay evidencia propia de
    # Scalping que justifique cambiarlo.
    enable_delta_band_filter: bool = _env_bool("GGAL_BOT_SCALPING_ENABLE_DELTA_BAND_FILTER", False)
    delta_band_min: float = _env_float("GGAL_BOT_SCALPING_DELTA_BAND_MIN", 0.40)
    delta_band_max: float = _env_float("GGAL_BOT_SCALPING_DELTA_BAND_MAX", 0.55)
    require_level_confirmation: bool = _env_bool("GGAL_BOT_SCALPING_REQUIRE_LEVEL_CONFIRMATION", False)
    level_threshold_vol_points: float = _env_float("GGAL_BOT_SCALPING_LEVEL_THRESHOLD", 5.0)
    enable_obi_filter: bool = _env_bool("GGAL_BOT_SCALPING_ENABLE_OBI_FILTER", True)
    # Mas exigente que el -0.30 default de weekly_asymmetric
    # (LongFirstConfig.min_obi_for_entry): un scalp de minutos tolera MENOS
    # desbalance vendedor que uno semanal, porque no hay tiempo de "esperar
    # a que el libro se acomode" dentro del horizonte de la posicion.
    min_obi_for_entry: float = _env_float("GGAL_BOT_SCALPING_MIN_OBI_FOR_ENTRY", -0.15)

    # --- Profundidad minima de punta vendedora (ver models/microstructure.py.
    # passes_min_ask_depth) - requerimiento NUEVO especifico de scalping:
    # "garantizar fill inmediato" contra un tamaño de ASK razonable, mas
    # estricto que el OBI de arriba (que solo mira el desbalance RELATIVO,
    # no el tamaño ABSOLUTO de la punta que la orden va a levantar).
    enable_min_ask_depth_filter: bool = _env_bool("GGAL_BOT_SCALPING_ENABLE_MIN_ASK_DEPTH_FILTER", True)
    min_ask_size_for_entry: float = _env_float("GGAL_BOT_SCALPING_MIN_ASK_SIZE", 30.0)

    # Mismo mecanismo que LongFirstConfig.enable_signal_funnel_log (mismo
    # nombre de atributo, scan_entry_signals es generico sobre self.cfg) -
    # env var propia para poder activar el embudo detallado de scalping
    # independientemente del de weekly_asymmetric. Default False.
    enable_signal_funnel_log: bool = _env_bool("GGAL_BOT_SCALPING_ENABLE_SIGNAL_FUNNEL_LOG", False)

    # --- Salida forzada sobre la PRIMA (ver risk.risk_manager.RiskManager.
    # evaluate_scalping_exit) - umbrales mas ajustados que weekly_asymmetric
    # (LongFirstConfig.stop_loss_pct=50%/take_profit_pct=100%): un scalp que
    # se mueve en contra o a favor lo hace rapido, no hace falta tolerar
    # tanto rango.
    stop_loss_pct: float = _env_float("GGAL_BOT_SCALPING_STOP_LOSS_PCT", 0.25)
    take_profit_pct: float = _env_float("GGAL_BOT_SCALPING_TAKE_PROFIT_PCT", 0.35)

    # --- Horizonte ACELERADO de salida, en MINUTOS (la diferencia central
    # respecto de weekly_asymmetric, que usa dias habiles) ---
    max_holding_minutes: float = _env_float("GGAL_BOT_SCALPING_MAX_HOLDING_MINUTES", 120.0)
    # Cierre preventivo por FALTA DE PROGRESO: si a los `progress_check_minutes`
    # de abierta la posicion todavia no alcanzo `min_progress_pnl_pct` de
    # ganancia sobre la prima, se cierra - la tesis de scalping es "moverse
    # rapido o salir", no sostener una posicion sin señal de que la
    # dislocacion se esta corrigiendo en la direccion esperada.
    progress_check_minutes: float = _env_float("GGAL_BOT_SCALPING_PROGRESS_CHECK_MINUTES", 30.0)
    min_progress_pnl_pct: float = _env_float("GGAL_BOT_SCALPING_MIN_PROGRESS_PNL_PCT", 0.05)

    # --- Cierre obligatorio de Fin de Dia (EOD), en horario de Argentina
    # (ART = UTC-3 todo el año, sin horario de verano desde 2009 - no se
    # usa zoneinfo/pytz a proposito solo para esto, ver RiskManager.
    # _is_past_eod) - NUNCA se sostiene una posicion de scalping durante la
    # noche/fin de semana, a diferencia de weekly_asymmetric (que sostiene
    # posiciones varios dias por diseño, con su propia guardia de fin de
    # semana separada, ver LongFirstConfig.weekend_theta_guard_enabled).
    eod_close_enabled: bool = _env_bool("GGAL_BOT_SCALPING_EOD_CLOSE_ENABLED", True)
    eod_close_time: str = _env_str("GGAL_BOT_SCALPING_EOD_CLOSE_TIME", "16:50")
    eod_timezone_offset_hours: float = _env_float("GGAL_BOT_SCALPING_EOD_TZ_OFFSET_HOURS", -3.0)

    # --- Reversion de IV en alta frecuencia (ver data/iv_mean_reversion.py):
    # salida ADICIONAL (no reemplaza las de arriba) para cuando la
    # dislocacion de smile que motivo la entrada ya se corrigio hacia el
    # comportamiento reciente de esa base en particular (z-score de la
    # propia serie de dislocaciones, no un umbral fijo en vol points).
    enable_iv_mean_reversion_exit: bool = _env_bool("GGAL_BOT_SCALPING_ENABLE_IV_REVERSION_EXIT", True)
    iv_reversion_window_seconds: float = _env_float("GGAL_BOT_SCALPING_IV_REVERSION_WINDOW_SECONDS", 1800.0)
    iv_reversion_min_samples: int = _env_int("GGAL_BOT_SCALPING_IV_REVERSION_MIN_SAMPLES", 10)
    iv_reversion_exit_zscore: float = _env_float("GGAL_BOT_SCALPING_IV_REVERSION_EXIT_ZSCORE", 0.5)

    # --- Analisis Tecnico intradia MULTI-TIMEFRAME (ver data/intraday_bars.py,
    # que REUSA compute_technical_snapshot() de data/technical_analysis.py
    # sin forkearlo - ver ese modulo). Dos timeframes (rapido/lento, 5m/15m
    # por defecto) en vez del unico grafico 1D de TechnicalAnalysisConfig;
    # periodos de indicador mas cortos/rapidos, calibrados para velas de
    # minutos en vez de diarias. require_multi_timeframe_agreement exige
    # que AMBOS timeframes coincidan antes de habilitar una direccion (si
    # no coinciden, NEUTRAL - el estado mas conservador).
    fast_bar_interval_minutes: int = _env_int("GGAL_BOT_SCALPING_FAST_BAR_MINUTES", 5)
    slow_bar_interval_minutes: int = _env_int("GGAL_BOT_SCALPING_SLOW_BAR_MINUTES", 15)
    require_multi_timeframe_agreement: bool = _env_bool("GGAL_BOT_SCALPING_REQUIRE_MTF_AGREEMENT", True)
    max_bars_retained: int = _env_int("GGAL_BOT_SCALPING_MAX_BARS_RETAINED", 300)
    refresh_interval_seconds: float = _env_float("GGAL_BOT_SCALPING_TA_REFRESH_SECONDS", 30.0)
    min_bars_required: int = _env_int("GGAL_BOT_SCALPING_TA_MIN_BARS", 25)
    ema_fast_period: int = _env_int("GGAL_BOT_SCALPING_TA_EMA_FAST", 9)
    ema_slow_period: int = _env_int("GGAL_BOT_SCALPING_TA_EMA_SLOW", 21)
    rsi_period: int = _env_int("GGAL_BOT_SCALPING_TA_RSI_PERIOD", 9)
    adx_period: int = _env_int("GGAL_BOT_SCALPING_TA_ADX_PERIOD", 9)
    macd_fast_period: int = _env_int("GGAL_BOT_SCALPING_TA_MACD_FAST", 6)
    macd_slow_period: int = _env_int("GGAL_BOT_SCALPING_TA_MACD_SLOW", 13)
    macd_signal_period: int = _env_int("GGAL_BOT_SCALPING_TA_MACD_SIGNAL", 5)
    adx_trend_threshold: float = _env_float("GGAL_BOT_SCALPING_TA_ADX_THRESHOLD", 15.0)
    # Momentum Shift interno del snapshot intradia (informativo/logging por
    # ahora - ver data/intraday_bars.py; distinto del Momentum Shift Override
    # de TechnicalAnalysisConfig que SI consume WeeklyAsymmetricStrategy.
    # scan_entry_signals() via SETTINGS.technical_analysis, compartido con
    # weekly_asymmetric a proposito por ser solo un multiplicador de umbral
    # generico, no algo especifico de velas diarias).
    enable_momentum_shift_override: bool = _env_bool("GGAL_BOT_SCALPING_TA_ENABLE_MOMENTUM_OVERRIDE", False)
    momentum_shift_lookback_bars: int = _env_int("GGAL_BOT_SCALPING_TA_MOMENTUM_LOOKBACK_BARS", 3)
    momentum_shift_rsi_delta: float = _env_float("GGAL_BOT_SCALPING_TA_MOMENTUM_RSI_DELTA", 10.0)

    # --- Techo de riesgo de Griegas PROPIO de scalping (ver risk/risk_manager.
    # RiskManager/RiskLimits y run_bot.py:GgalOptionsBot.__init__ ->
    # self.scalping_risk_manager) ---
    #
    # CORRECCION (2026-09-03, ver README "Interaccion con el techo de
    # Griegas"): originalmente scalping usaba el MISMO RiskManager/
    # RiskLimits que weekly_asymmetric (self.risk_manager, compartido), y
    # su gate should_halt_new_positions() se evaluaba contra
    # self.portfolio.total_greeks() -- es decir, la EXPOSICION TOTAL de la
    # cuenta, sumando ambas estrategias. Esto rompia el aislamiento por
    # strategy_tag que ya regia capital (_capital_available_ars) y salidas
    # (build_exit_signals): en produccion se observo que un libro de
    # weekly_asymmetric con vega=9550 (por encima del techo default de
    # RiskConfig.max_vega_total=5000.0) bloqueaba TODAS las entradas
    # nuevas, incluidas las de scalping, aunque scalping no tuviera
    # posiciones propias abiertas todavia ("Señal ... descartada: la
    # cuenta ya excede limites de riesgo." disparado por el book de la
    # OTRA estrategia).
    #
    # La correccion le da a scalping su PROPIO RiskManager/RiskLimits,
    # evaluado solo contra las Griegas de las posiciones con
    # strategy_tag="scalping" (ver run_bot.py:_greeks_for_strategy()), de
    # forma simetrica a como ya funcionaba el capital. Los defaults son
    # mas chicos que los de weekly_asymmetric (RiskConfig.max_vega_total=
    # 5000.0/max_gamma_total=2000.0) porque el sizing de scalping es
    # deliberadamente MENOR por posicion (ver max_risk_pct_per_trade
    # arriba, 8% vs el 20% default de LongFirstConfig) - un techo propio
    # mas chico refleja ese capital mas chico por trade, no una tolerancia
    # de riesgo distinta. El piso de liquidez (spread/book/volumen) sigue
    # siendo el UNICO compartido (SETTINGS.risk.*), porque mide calidad de
    # mercado de la punta, no presupuesto de cartera.
    max_vega_total: float = _env_float("GGAL_BOT_SCALPING_MAX_VEGA_TOTAL", 3000.0)
    max_gamma_total: float = _env_float("GGAL_BOT_SCALPING_MAX_GAMMA_TOTAL", 1500.0)


# ---------------------------------------------------------------------------
# Selector de estrategia activa (ver run_bot.py: GgalOptionsBot.__init__ y
# recompute_cycle() ramifican todo el ciclo segun este valor)
# ---------------------------------------------------------------------------

# "weekly_asymmetric" -> Long-First / Weekly Asymmetric, ver
#   strategy/weekly_asymmetric.py + risk/position_sizer.py (DEFAULT).
# "vol_arbitrage"     -> arbitraje de volatilidad delta-neutral original,
#   ver strategy/vol_arbitrage.py (el modo con el que arranco el proyecto).
# "scalping"          -> Scalping Intradia (ver ScalpingConfig arriba),
#   AGREGADO 2026-09-07 a pedido EXPLICITO del usuario, para poder elegir
#   la estrategia activa al arrancar el bot entre las tres.
#
#   ADVERTENCIA DE DISEÑO (leer antes de setear esto en produccion): este
#   valor es la seleccion EXCLUYENTE de "que estrategia principal corre" -
#   run_bot.py.GgalOptionsBot.recompute_cycle() llama a UNA SOLA de las
#   tres ramas (_run_weekly_asymmetric_cycle / _run_vol_arbitrage_cycle /
#   _run_scalping_cycle como PRINCIPAL, no como aditivo) segun este valor.
#   Esto es DISTINTO y en principio CONTRADICTORIO con la decision de
#   arquitectura documentada arriba junto a ScalpingConfig (2026-09-03,
#   "modo nuevo aparte" ADITIVO): ese modo aditivo (GGAL_BOT_ENABLE_SCALPING,
#   independiente de esta variable) fue elegido especificamente para NO
#   apagar la gestion (entradas Y SALIDAS - Stop Loss/Take Profit/horizonte/
#   guardia de fin de semana) de las posiciones de weekly_asymmetric al
#   activar scalping. GGAL_BOT_ACTIVE_STRATEGY=scalping SI apaga esa
#   gestion por completo para cualquier posicion que no sea de scalping -
#   fue una decision EXPLICITA del usuario (2026-09-07, eligio la opcion
#   "excluyente" tras ser advertido de este mismo contraste) preferir un
#   selector literal de 3 vias antes que preservar esa proteccion. Ver
#   GgalOptionsBot._warn_orphaned_positions_for_active_strategy(), que
#   loguea (sin bloquear nada - la eleccion ya fue tomada) cada posicion
#   que quede sin ninguna gestion bajo la seleccion vigente, para que
#   nunca sea una sorpresa silenciosa en produccion.
VALID_STRATEGIES: Tuple[str, ...] = ("weekly_asymmetric", "vol_arbitrage", "scalping")


@dataclass
class StrategyConfig:
    """
    Que estrategia corre el orquestador principal (run_bot.py) como
    PRINCIPAL (entradas nuevas Y gestion de salidas) de forma EXCLUYENTE.
    Un valor invalido en GGAL_BOT_ACTIVE_STRATEGY (fuera de
    VALID_STRATEGIES) NO frena el arranque del bot: run_bot.py cae a
    "weekly_asymmetric" y loguea una advertencia explicita (ver
    GgalOptionsBot.__init__) en vez de fallar en silencio o crashear.

    Ver la nota de advertencia junto a VALID_STRATEGIES arriba antes de
    setear "scalping" aca en produccion.
    """
    active: str = _env_str("GGAL_BOT_ACTIVE_STRATEGY", "weekly_asymmetric")


# ---------------------------------------------------------------------------
# Modulo de Analisis Tecnico (ver data/technical_analysis.py): filtro de
# tendencia 1D obligatorio para el modo Long-First / Weekly Asymmetric -
# BULLISH habilita solo Calls, BEARISH solo Puts, NEUTRAL exige una
# dislocacion de smile extrema para siquiera considerar una entrada (y
# nunca completa spreads). Ver la nota de riesgo en LongFirstConfig arriba:
# esto es un FILTRO DIRECCIONAL, no una prediccion - un ADX/MACD/EMA
# "BULLISH" no garantiza que el precio suba, solo indica que la estructura
# tecnica reciente de GGAL es consistente con esa lectura.
# ---------------------------------------------------------------------------

@dataclass
class TechnicalAnalysisConfig:
    # Interruptor general: si esta en False, WeeklyAsymmetricStrategy no
    # aplica ningun filtro direccional (comportamiento identico al de antes
    # de este modulo) - util para aislar el efecto del filtro en pruebas.
    enabled: bool = _env_bool("GGAL_BOT_TECHNICAL_FILTER_ENABLED", True)

    # --- Fuente de datos (ver data/technical_analysis.py) ---
    # "auto"      -> intenta data912 (/historical/stocks/{ticker}) y cae a
    #                un generador sintetico local si no hay red/pocas barras.
    # "data912"   -> fuerza el REST publico (sin fallback sintetico).
    # "synthetic" -> fuerza el generador local (100% offline, para tests).
    data_source: str = _env_str("GGAL_BOT_TA_SOURCE", "auto")
    lookback_bars: int = _env_int("GGAL_BOT_TA_LOOKBACK_BARS", 200)  # velas 1D a pedir (100-200 tipico)
    # Minimo de velas utilizables para animarse a clasificar tendencia (EMA
    # 50 + MACD(12,26,9) necesitan bastante historia para estabilizarse);
    # por debajo de esto, get_daily_trend_signal() devuelve NEUTRAL con el
    # motivo "datos insuficientes", nunca BULLISH/BEARISH por defecto.
    min_bars_required: int = _env_int("GGAL_BOT_TA_MIN_BARS", 60)
    # Cada cuanto se refresca el historico diario y se recalculan los
    # indicadores (ver TechnicalAnalysisEngine.refresh): las velas 1D no
    # cambian intra-dia, asi que refrescar en cada ciclo de ~2s del bot
    # seria puro desperdicio de red - por defecto, una vez por hora.
    refresh_interval_seconds: float = _env_float("GGAL_BOT_TA_REFRESH_SECONDS", 3600.0)

    # --- Periodos de los indicadores (ver requerimiento funcional) ---
    ema_fast_period: int = _env_int("GGAL_BOT_TA_EMA_FAST", 20)
    ema_slow_period: int = _env_int("GGAL_BOT_TA_EMA_SLOW", 50)
    rsi_period: int = _env_int("GGAL_BOT_TA_RSI_PERIOD", 14)
    adx_period: int = _env_int("GGAL_BOT_TA_ADX_PERIOD", 14)
    macd_fast_period: int = _env_int("GGAL_BOT_TA_MACD_FAST", 12)
    macd_slow_period: int = _env_int("GGAL_BOT_TA_MACD_SLOW", 26)
    macd_signal_period: int = _env_int("GGAL_BOT_TA_MACD_SIGNAL", 9)

    # --- Umbral de fuerza de tendencia (ADX) ---
    # NOTA: el enunciado funcional menciona "ADX > 25" como fuerza fuerte en
    # la introduccion, pero especifica "ADX > 20" en la regla BULLISH/BEARISH
    # concreta - se sigue esta ultima (el umbral operativo real), configurable.
    adx_trend_threshold: float = _env_float("GGAL_BOT_TA_ADX_THRESHOLD", 20.0)

    # --- Comportamiento bajo NEUTRAL ---
    # Bajo NEUTRAL el bot NO completa spreads (cash/espera estricto) y solo
    # considera una entrada nueva si la dislocacion de smile es "extrema":
    # smile_threshold_vol_points (LongFirstConfig) multiplicado por este
    # factor (ej. 3.0 vol pts * 2.0 = 6.0 vol pts exigidos en vez de 3.0).
    neutral_extreme_smile_multiplier: float = _env_float("GGAL_BOT_TA_NEUTRAL_EXTREME_MULT", 2.0)

    # --- Momentum Shift / Early Reversal Override ---
    # El filtro de tendencia 1D (EMA20/EMA50/ADX/MACD) es, por diseño, un
    # filtro de ESTRUCTURA ya confirmada - siempre llega despues de que el
    # nuevo regimen ya arranco (cruce de medias moviles requiere varias
    # ruedas en la nueva direccion). Para no perder movimientos por operar
    # "demasiado tarde" (feedback explicito del usuario, 2026-08) sin
    # eliminar el filtro de tendencia en si (sigue siendo obligatorio), se
    # agrega esta señal complementaria basada en RSI(14) (lider, acotado
    # 0-100, no depende de la escala nominal de precio de GGAL como si
    # dependeria la pendiente del histograma MACD): si el RSI se movio
    # `momentum_shift_rsi_delta` puntos o mas EN CONTRA de la tendencia
    # vigente en las ultimas `momentum_shift_lookback_bars` velas, se marca
    # una reversion temprana (ver data/technical_analysis.py:MomentumShift).
    # Esa señal, cuando esta activa, relaja el bloqueo del tipo de opcion
    # contrario en WeeklyAsymmetricStrategy.scan_entry_signals() - pero SOLO
    # bajo el umbral EXTREMO de dislocacion de smile (el mismo que ya exige
    # NEUTRAL), nunca el umbral normal: se sigue exigiendo una dislocacion
    # de smile fuerte para operar en contra de la tendencia diaria, ahora
    # con un gatillo adicional (momentum) en vez de solo el gatillo temporal
    # (esperar a que la tendencia diaria termine de girar).
    enable_momentum_shift_override: bool = _env_bool("GGAL_BOT_TA_ENABLE_MOMENTUM_OVERRIDE", True)
    momentum_shift_lookback_bars: int = _env_int("GGAL_BOT_TA_MOMENTUM_LOOKBACK_BARS", 3)
    momentum_shift_rsi_delta: float = _env_float("GGAL_BOT_TA_MOMENTUM_RSI_DELTA", 8.0)

    # --- Vol realizada robusta a saltos (MEJORA 2026-09-28) ---
    # A pedido explicito del usuario. LongFirstConfig.require_level_confirmation
    # espera un `hv_estimate` (vol realizada de referencia) para comparar
    # contra el nivel promedio de IV del vencimiento (ver
    # models/volatility_surface.py::VolatilitySurface.level_dislocation) -
    # VERIFICADO por grep que, hasta esta mejora, run_bot.py NUNCA pasaba
    # ese parametro (`hv_estimate` quedaba siempre en None), asi que
    # require_level_confirmation era un no-op completo aunque estuviera en
    # True. Esta mejora conecta un estimador real, reusando las MISMAS
    # velas 1D ya cacheadas por TechnicalAnalysisEngine para la tendencia
    # (sin pegarle a una fuente de datos nueva) - ver
    # TechnicalAnalysisEngine.hv_estimate() y models/realized_vol.py. El
    # GGAL en pesos tiene saltos discretos por eventos de
    # devaluacion/CCL que NO son volatilidad en el sentido de difusion
    # continua - un estimador close-to-close ingenuo (varianza de retornos)
    # los trata igual que ruido normal y queda "inflado" durante semanas
    # despues de un solo salto. bipower_realized_vol (ver ese modulo) es
    # jump-robust POR CONSTRUCCION: multiplica retornos ADYACENTES en vez de
    # elevarlos al cuadrado, asi que un retorno aislado enorme se pondera
    # contra sus vecinos (tipicamente chicos), no contra si mismo. Apagado
    # por defecto: cambia el resultado de un chequeo que hoy es puro no-op,
    # asi que activarlo es una decision de comportamiento nuevo, no un
    # ajuste neutro.
    enable_jump_robust_hv: bool = _env_bool("GGAL_BOT_ENABLE_JUMP_ROBUST_HV", False)


# ---------------------------------------------------------------------------
# Configuracion agregada
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    instruments: InstrumentsConfig = field(default_factory=InstrumentsConfig)
    rate: RateConfig = field(default_factory=RateConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    risk_limits: RiskLimitsConfig = field(default_factory=RiskLimitsConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    shadow: ShadowConfig = field(default_factory=ShadowConfig)
    broker_rest: BrokerRestConfig = field(default_factory=BrokerRestConfig)
    long_first: LongFirstConfig = field(default_factory=LongFirstConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    technical_analysis: TechnicalAnalysisConfig = field(default_factory=TechnicalAnalysisConfig)
    scalping: ScalpingConfig = field(default_factory=ScalpingConfig)
    vol_arbitrage: VolArbitrageConfig = field(default_factory=VolArbitrageConfig)


SETTINGS = Settings()

# Alias literal pedido para activar/consultar el modo shadow directamente
# (`from ggal_bot.config import SHADOW_MODE`). La fuente de verdad sigue
# siendo SETTINGS.shadow.enabled (controlable por .env); este modulo-level
# constant solo se fija una vez, al importar el modulo, con el mismo valor.
SHADOW_MODE = SETTINGS.shadow.enabled
