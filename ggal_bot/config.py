"""
config.py
=========
Configuracion centralizada del bot. Los valores por defecto son los que
figuran en el documento de diseño (docs/Diseno_Bot_Opciones_GGAL.md) y deben
recalibrarse con el tamaño real de cuenta y la volatilidad reciente de GGAL
antes de operar en vivo. Las credenciales NUNCA se hardcodean aca: se leen
de variables de entorno (ver .env.example) via python-dotenv.
"""

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
    max_capital_ars: float = _env_float("GGAL_BOT_MAX_CAPITAL_ARS", 1_000_000.0)
    max_risk_pct_per_trade: float = _env_float("GGAL_BOT_MAX_RISK_PCT_PER_TRADE", 0.20)
    min_contracts_per_trade: int = _env_int("GGAL_BOT_MIN_CONTRACTS_PER_TRADE", 1)

    # --- Objetivo de retorno (dimensionamiento, NO garantia - ver nota arriba) ---
    weekly_target_ars: float = _env_float("GGAL_BOT_WEEKLY_TARGET_ARS", 1_000_000.0)

    # --- Horizonte semanal y guardia de decay de fin de semana ---
    max_holding_business_days: int = _env_int("GGAL_BOT_MAX_HOLDING_BUSINESS_DAYS", 5)
    weekend_theta_guard_enabled: bool = _env_bool("GGAL_BOT_WEEKEND_THETA_GUARD", True)

    # --- Salida forzada, medida sobre la PRIMA pagada (no sobre el subyacente) ---
    stop_loss_pct: float = _env_float("GGAL_BOT_STOP_LOSS_PCT", 0.50)     # -50% de la prima -> cerrar
    take_profit_pct: float = _env_float("GGAL_BOT_TAKE_PROFIT_PCT", 1.00)  # +100% de la prima -> cerrar

    # --- Filtro de entrada: convexidad / moneyness / confirmacion de nivel ---
    smile_threshold_vol_points: float = _env_float("GGAL_BOT_LONGFIRST_SMILE_THRESHOLD", 3.0)
    moneyness_band_pct: float = _env_float("GGAL_BOT_MONEYNESS_BAND_PCT", 0.15)  # |log(K/S)| maximo considerado
    require_level_confirmation: bool = _env_bool("GGAL_BOT_REQUIRE_LEVEL_CONFIRMATION", False)
    level_threshold_vol_points: float = _env_float("GGAL_BOT_LONGFIRST_LEVEL_THRESHOLD", 5.0)

    # --- Spreads (Bull Call / Bear Put): pata corta solo tras la larga confirmada ---
    enable_spread_completion: bool = _env_bool("GGAL_BOT_ENABLE_SPREAD_COMPLETION", True)
    spread_wing_moneyness_pct: float = _env_float("GGAL_BOT_SPREAD_WING_MONEYNESS_PCT", 0.05)

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
    vega_decay_exit_ratio: float = _env_float("GGAL_BOT_VEGA_DECAY_EXIT_RATIO", 0.35)


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
    # enteramente independiente y se mide en minutos.
    max_holding_business_days: int = _env_int("GGAL_BOT_SCALPING_MAX_HOLDING_BUSINESS_DAYS", 3)
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


# ---------------------------------------------------------------------------
# Selector de estrategia activa (ver run_bot.py: GgalOptionsBot.__init__ y
# recompute_cycle() ramifican todo el ciclo segun este valor)
# ---------------------------------------------------------------------------

# "weekly_asymmetric" -> Long-First / Weekly Asymmetric, ver
#   strategy/weekly_asymmetric.py + risk/position_sizer.py (DEFAULT).
# "vol_arbitrage"     -> arbitraje de volatilidad delta-neutral original,
#   ver strategy/vol_arbitrage.py (el modo con el que arranco el proyecto).
VALID_STRATEGIES: Tuple[str, ...] = ("weekly_asymmetric", "vol_arbitrage")


@dataclass
class StrategyConfig:
    """
    Que estrategia corre el orquestador principal (run_bot.py). Un valor
    invalido en GGAL_BOT_ACTIVE_STRATEGY (fuera de VALID_STRATEGIES) NO
    frena el arranque del bot: run_bot.py cae a "weekly_asymmetric" y
    loguea una advertencia explicita (ver GgalOptionsBot.__init__) en vez
    de fallar en silencio o crashear.
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
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    shadow: ShadowConfig = field(default_factory=ShadowConfig)
    broker_rest: BrokerRestConfig = field(default_factory=BrokerRestConfig)
    long_first: LongFirstConfig = field(default_factory=LongFirstConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    technical_analysis: TechnicalAnalysisConfig = field(default_factory=TechnicalAnalysisConfig)
    scalping: ScalpingConfig = field(default_factory=ScalpingConfig)


SETTINGS = Settings()

# Alias literal pedido para activar/consultar el modo shadow directamente
# (`from ggal_bot.config import SHADOW_MODE`). La fuente de verdad sigue
# siendo SETTINGS.shadow.enabled (controlable por .env); este modulo-level
# constant solo se fija una vez, al importar el modulo, con el mismo valor.
SHADOW_MODE = SETTINGS.shadow.enabled
