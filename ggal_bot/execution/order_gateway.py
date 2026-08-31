"""
order_gateway.py
=================
Gateway de conexion y ejecucion contra el ALYC via PyRofex:

    - initialize_environment() : inicializa pyRofex.initialize() (REMARKET/LIVE),
                                   con reintentos ante fallas transitorias.
    - WebSocketConnectionManager: abre el (unico) websocket de PyRofex que
                                   multiplexa market data + order reports, y lo
                                   reconecta automaticamente con backoff si se cae.
    - send_order() / cancel_order() / get_account_positions(): wrappers
      delgados de bajo nivel sobre las funciones equivalentes de pyRofex.
    - OrderGateway: capa de mas alto nivel que usa las funciones anteriores y
      mantiene el estado local de cada orden (OrderState) para que el resto
      del bot (execution/mid_price_exec.py, strategy/delta_hedger.py) pueda
      consultarlo sin acoplarse a la API del broker.

IMPORTANTE: este modulo es la integracion real con el ALYC. Antes de operar
en LIVE, correr contra REMARKET durante al menos un ciclo mensual completo
(ver checklist en docs/Diseno_Bot_Opciones_GGAL.md) y validar que los nombres
de parametros usados aca coinciden con la version de pyRofex instalada -
la libreria ha tenido cambios de firma entre versiones.
"""

from __future__ import annotations

import csv
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Optional

from ggal_bot.config import SETTINGS
from ggal_bot import paths

logger = logging.getLogger("ggal_bot.order_gateway")

try:
    import pyRofex
    _PYROFEX_AVAILABLE = True
except ImportError:
    pyRofex = None
    _PYROFEX_AVAILABLE = False
    logger.warning(
        "pyRofex no esta instalado. Instalar con 'pip install pyRofex' para "
        "conectar a un ALYC real. El resto del bot puede probarse igual con "
        "datos simulados (ver validation/)."
    )


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderTypeEnum(Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(Enum):
    PENDING_NEW = "pending_new"
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class OrderRequest:
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    order_type: OrderTypeEnum = OrderTypeEnum.LIMIT
    client_order_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class OrderState:
    request: OrderRequest
    status: OrderStatus = OrderStatus.PENDING_NEW
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    sent_at: float = field(default_factory=time.time)
    last_update_at: float = field(default_factory=time.time)
    price_improvements: int = 0
    # Precio de referencia (mid o spot) al momento de armar la orden, usado
    # por execution/mid_price_exec.py para medir slippage acumulado.
    reference_price: float = 0.0


# ---------------------------------------------------------------------------
# FASE 1 - Inicializacion del ambiente (REMARKET / LIVE)
# ---------------------------------------------------------------------------

_environment_ready = False  # flag de modulo: evita reabrir sesion sin necesidad


def is_environment_ready() -> bool:
    return _environment_ready


def initialize_environment(max_retries: int = 3, retry_backoff_seconds: float = 3.0) -> bool:
    """
    Inicializa pyRofex.initialize() contra el ambiente configurado, con
    reintentos ante errores transitorios de red/autenticacion. Debe llamarse
    una unica vez al arrancar el bot (y de nuevo tras cada reconexion de
    websocket, ver WebSocketConnectionManager), antes de usar send_order(),
    cancel_order() o get_account_positions().

    Devuelve True si la inicializacion fue exitosa; False si se agotaron los
    reintentos, en cuyo caso el bot NO debe continuar con el arranque.
    """
    global _environment_ready

    if not _PYROFEX_AVAILABLE:
        logger.error("pyRofex no esta instalado; no se puede inicializar el ambiente.")
        return False

    ok, msg = SETTINGS.broker.validate()
    if not ok:
        logger.error("Configuracion de broker invalida: %s", msg)
        return False

    broker = SETTINGS.broker
    environment = (
        pyRofex.Environment.LIVE if broker.environment.upper() == "LIVE"
        else pyRofex.Environment.REMARKET
    )

    # Muchos ALYCs argentinos corren su propia instancia de la API y requieren
    # apuntar pyRofex a un endpoint propio en vez del default de la libreria.
    # pyRofex expone esto via un diccionario de configuracion de ambiente que
    # ha cambiado de nombre entre versiones (environment_config en algunas,
    # components.globals.environment_config en otras) - se intenta de forma
    # defensiva y se continua sin URL custom si no se encuentra el atributo,
    # dejando un warning explicito para que se verifique contra la version
    # instalada antes de asumir que la URL propia esta siendo usada.
    if broker.rest_url or broker.ws_url:
        _apply_custom_endpoints(environment, broker.rest_url, broker.ws_url)

    attempt = 0
    while attempt < max_retries:
        attempt += 1
        try:
            pyRofex.initialize(
                user=broker.user,
                password=broker.password,
                account=broker.account,
                environment=environment,
            )
            _environment_ready = True
            logger.info(
                "Ambiente PyRofex inicializado (%s, intento %d/%d)",
                broker.environment, attempt, max_retries,
            )
            return True
        except Exception as exc:
            logger.error("Fallo al inicializar PyRofex (intento %d/%d): %s", attempt, max_retries, exc)
            if attempt < max_retries:
                time.sleep(retry_backoff_seconds)

    logger.critical("No se pudo inicializar el ambiente PyRofex tras %d intentos.", max_retries)
    _environment_ready = False
    return False


def _apply_custom_endpoints(environment, rest_url: str, ws_url: str) -> None:
    """Best-effort: aplica endpoints propios del ALYC si la version de pyRofex lo permite."""
    try:
        config_dict = getattr(pyRofex, "environment_config", None)
        if config_dict is None:
            config_dict = pyRofex.components.globals.environment_config  # type: ignore[attr-defined]
        env_entry = config_dict[environment]
        if rest_url:
            env_entry["url"] = rest_url
        if ws_url:
            env_entry["ws"] = ws_url
        logger.info("Endpoints propios del ALYC aplicados a pyRofex (%s).", environment)
    except Exception as exc:
        logger.warning(
            "No se pudo aplicar la URL/WS propia del ALYC (verificar la API de "
            "configuracion de ambiente de tu version de pyRofex): %s", exc,
        )


# ---------------------------------------------------------------------------
# FASE 1 - Conexion de websocket con reconexion automatica
# ---------------------------------------------------------------------------

class WebSocketConnectionManager:
    """
    PyRofex multiplexa market data y order reports sobre una unica conexion
    de websocket; por eso la reconexion se maneja en un unico lugar (aca) en
    vez de duplicarse entre market_data_feed.py y order_gateway.py.

    Uso tipico (ver run_bot.py):
        manager = WebSocketConnectionManager(
            market_data_handler=market_feed.handle_market_data,
            order_report_handler=order_gateway.on_order_report,
            on_reconnect=lambda: market_feed.subscribe(current_tickers),
        )
        manager.connect()
    """

    def __init__(
        self,
        market_data_handler: Callable[[Dict], None],
        order_report_handler: Callable[[Dict], None],
        on_reconnect: Optional[Callable[[], None]] = None,
    ):
        self._market_data_handler = market_data_handler
        self._order_report_handler = order_report_handler
        self._on_reconnect = on_reconnect
        self._connected = False
        self._closing = False
        self._reconnect_attempts = 0
        self._lock = threading.Lock()

    def connect(self) -> bool:
        if not _PYROFEX_AVAILABLE:
            raise RuntimeError("pyRofex no disponible.")
        if not is_environment_ready():
            raise RuntimeError("Llamar initialize_environment() antes de abrir el websocket.")
        try:
            pyRofex.init_websocket_connection(
                market_data_handler=self._market_data_handler,
                order_report_handler=self._order_report_handler,
                error_handler=self._handle_error,
            )
            with self._lock:
                self._connected = True
                self._reconnect_attempts = 0
            logger.info("Websocket de PyRofex conectado.")
            return True
        except Exception as exc:
            logger.error("Error al abrir el websocket de PyRofex: %s", exc)
            with self._lock:
                self._connected = False
            self._schedule_reconnect()
            return False

    def _handle_error(self, message: Dict) -> None:
        logger.error("Error de PyRofex (websocket): %s", message)
        # Un error del canal puede implicar (o anticipar) una desconexion;
        # se agenda un intento de reconexion de forma preventiva. connect()
        # es idempotente: si el socket sigue vivo, simplemente reabre.
        with self._lock:
            self._connected = False
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._closing:
            return
        broker = SETTINGS.broker
        max_attempts = broker.ws_max_reconnect_attempts
        if max_attempts and self._reconnect_attempts >= max_attempts:
            logger.critical(
                "Se agotaron los %d intentos de reconexion de websocket. "
                "Requiere intervencion manual.", max_attempts,
            )
            return

        self._reconnect_attempts += 1
        delay = min(
            broker.ws_reconnect_initial_seconds * (
                broker.ws_reconnect_backoff_factor ** (self._reconnect_attempts - 1)
            ),
            broker.ws_reconnect_max_seconds,
        )
        logger.warning("Reintentando conexion de websocket en %.1fs (intento %d)", delay, self._reconnect_attempts)
        timer = threading.Timer(delay, self._attempt_reconnect)
        timer.daemon = True
        timer.start()

    def _attempt_reconnect(self) -> None:
        if self._closing:
            return
        # Por experiencia con ALYCs locales, la sesion/token suele invalidarse
        # cuando cae la conexion: se re-inicializa el ambiente antes de reabrir el WS.
        if not initialize_environment(max_retries=1):
            self._schedule_reconnect()
            return
        if self.connect() and self._on_reconnect:
            try:
                self._on_reconnect()
            except Exception as exc:
                logger.error("Error en el callback on_reconnect: %s", exc)

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def close(self) -> None:
        """Cierre prolijo para graceful shutdown (ver run_bot.py)."""
        self._closing = True
        with self._lock:
            self._connected = False
        logger.info("Cerrando conexion de websocket (shutdown).")
        close_fn = getattr(pyRofex, "close_websocket_connection", None) if _PYROFEX_AVAILABLE else None
        if callable(close_fn):
            try:
                close_fn()
            except Exception as exc:
                logger.warning("Error al cerrar el websocket: %s", exc)


# ---------------------------------------------------------------------------
# FASE 1 - Funciones de bajo nivel: envio de ordenes, cancelacion, posiciones
# ---------------------------------------------------------------------------

def send_order(
    ticker: str,
    side: OrderSide,
    size: float,
    price: float,
    order_type: OrderTypeEnum = OrderTypeEnum.LIMIT,
    client_order_id: Optional[str] = None,
) -> Dict:
    """Wrapper delgado sobre pyRofex.send_order(). Nunca lanza: devuelve un dict con status."""
    client_order_id = client_order_id or uuid.uuid4().hex[:12]

    if not _PYROFEX_AVAILABLE:
        logger.warning("pyRofex no disponible: send_order() simulado (no se envia a mercado real).")
        return {"status": "simulated", "clOrdId": client_order_id}
    if not is_environment_ready():
        logger.error("send_order llamado sin initialize_environment() previo.")
        return {"status": "error", "clOrdId": client_order_id, "error": "environment_not_ready"}

    pyrofex_side = pyRofex.Side.BUY if side is OrderSide.BUY else pyRofex.Side.SELL
    pyrofex_type = (
        pyRofex.OrderType.LIMIT if order_type is OrderTypeEnum.LIMIT else pyRofex.OrderType.MARKET
    )
    try:
        response = pyRofex.send_order(
            ticker=ticker,
            side=pyrofex_side,
            size=size,
            order_type=pyrofex_type,
            price=price,
            client_order_id=client_order_id,
        )
        logger.info(
            "send_order OK: %s %s x%.0f @ %.2f (id=%s)",
            side.value, ticker, size, price, client_order_id,
        )
        return response or {"status": "sent", "clOrdId": client_order_id}
    except Exception as exc:  # pyRofex puede lanzar excepciones propias segun el error del ALYC
        logger.error("send_order fallo para %s: %s", client_order_id, exc)
        return {"status": "error", "clOrdId": client_order_id, "error": str(exc)}


def cancel_order(client_order_id: str) -> Dict:
    """Wrapper delgado sobre pyRofex.cancel_order(). Nunca lanza: devuelve un dict con status."""
    if not _PYROFEX_AVAILABLE:
        logger.warning("pyRofex no disponible: cancel_order() simulado.")
        return {"status": "simulated", "clOrdId": client_order_id}
    if not is_environment_ready():
        logger.error("cancel_order llamado sin initialize_environment() previo.")
        return {"status": "error", "clOrdId": client_order_id, "error": "environment_not_ready"}
    try:
        response = pyRofex.cancel_order(client_order_id=client_order_id)
        logger.info("cancel_order OK: id=%s", client_order_id)
        return response or {"status": "cancelled", "clOrdId": client_order_id}
    except Exception as exc:
        logger.error("cancel_order fallo para %s: %s", client_order_id, exc)
        return {"status": "error", "clOrdId": client_order_id, "error": str(exc)}


def get_account_positions(account: Optional[str] = None) -> Dict:
    """
    Wrapper delgado sobre pyRofex.get_account_position(). Se usa tanto para
    reconciliar el portafolio interno (portfolio/portfolio.py) contra lo que
    realmente informa el ALYC, como en el checklist de paper trading.
    """
    if not _PYROFEX_AVAILABLE:
        logger.warning("pyRofex no disponible: get_account_positions() devuelve vacio.")
        return {"positions": []}
    if not is_environment_ready():
        logger.error("get_account_positions llamado sin initialize_environment() previo.")
        return {"positions": [], "error": "environment_not_ready"}
    account = account or SETTINGS.broker.account
    try:
        response = pyRofex.get_account_position(account=account)
        return response or {"positions": []}
    except Exception as exc:
        logger.error("get_account_positions fallo: %s", exc)
        return {"positions": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# Shadow Trading - "Paper Execution": auditoria de fills simulados
# ---------------------------------------------------------------------------

class ShadowAuditLogger:
    """
    Registra en un CSV local (paths.SHADOW_TRADES_LOG) cada operacion
    simulada por OrderGateway cuando SETTINGS.shadow.enabled es True, para
    poder auditar la logica de señales/ejecucion del bot sin haber tocado
    la API real del broker en ningun momento. Un archivo por proyecto (se
    va agregando una fila por evento; nunca se sobreescribe ni se rota).
    """

    _HEADER = [
        "timestamp_utc", "client_order_id", "symbol", "side", "order_type",
        "quantity", "requested_price", "fill_price", "reference_price", "event",
    ]

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path is not None else paths.SHADOW_TRADES_LOG
        self._lock = threading.Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        with self._lock:
            needs_header = (not self._path.exists()) or self._path.stat().st_size == 0
            if needs_header:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(self._HEADER)

    def log_fill(self, request: "OrderRequest", fill_price: float, reference_price: float) -> None:
        self._write_row([
            datetime.now(timezone.utc).isoformat(), request.client_order_id, request.symbol,
            request.side.value, request.order_type.value, request.quantity,
            request.price, fill_price, reference_price, "shadow_fill",
        ])

    def log_cancel(self, client_order_id: str, symbol: str = "") -> None:
        self._write_row([
            datetime.now(timezone.utc).isoformat(), client_order_id, symbol,
            "", "", "", "", "", "", "shadow_cancel",
        ])

    def _write_row(self, row) -> None:
        with self._lock:
            try:
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(row)
            except OSError as exc:
                logger.error("ShadowAuditLogger: no se pudo escribir en %s: %s", self._path, exc)


# ---------------------------------------------------------------------------
# FASE 1 - OrderGateway: tracking de estado de ordenes de alto nivel
# ---------------------------------------------------------------------------

class OrderGateway:
    """
    Envuelve send_order/cancel_order y mantiene un diccionario de OrderState
    por client_order_id para que el resto del bot (RiskManager, delta_hedger,
    mid_price_exec, VolatilityArbitrageStrategy) pueda consultar el estado de
    sus ordenes sin acoplarse a la API del broker.
    """

    def __init__(self, shadow_audit_path: Optional[Path] = None):
        """
        `shadow_audit_path`: override explicito de donde escribe el
        ShadowAuditLogger (por defecto None -> paths.SHADOW_TRADES_LOG, el
        CSV real de produccion).

        BUG REAL CORREGIDO (auditoria del 2026-08-27, ver
        docs/AUDITORIA_MAESTRA_2026-08-27.md seccion 3.3): antes de este
        parametro, OrderGateway() no tenia ninguna forma de redirigir el
        audit log, asi que practicamente todos los tests que corren en modo
        shadow (test_execution_pipeline.py, test_shadow_trading.py,
        test_strategy_selector.py via GgalOptionsBot()) escribian, sin
        querer, sobre el CSV REAL de produccion (logs/shadow_trades.csv) -
        confirmado en el propio archivo real del usuario (contaminacion
        parcial) y en la copia de este sandbox (contaminacion total: 94/94
        filas rastreadas a fixtures de tests). Ver
        ggal_bot/validation/conftest.py para la red de seguridad adicional
        a nivel de suite.
        """
        self._orders: Dict[str, OrderState] = {}
        self._timeout_seconds = SETTINGS.execution.order_timeout_seconds
        self._max_improvements = SETTINGS.execution.max_price_improvements
        # Shadow Trading / Paper Execution (ver config.ShadowConfig.enabled):
        # el logger solo se instancia (y solo entonces crea el CSV) cuando el
        # modo esta activo, para no dejar artefactos de auditoria en corridas
        # normales contra el ALYC real.
        self._shadow_logger = ShadowAuditLogger(path=shadow_audit_path) if SETTINGS.shadow.enabled else None

    def send(self, request: OrderRequest, reference_price: float = 0.0) -> OrderState:
        state = OrderState(request=request, reference_price=reference_price or request.price)
        self._orders[request.client_order_id] = state

        if SETTINGS.shadow.enabled:
            # Paper Execution: nunca se llama a send_order() (ni por lo tanto
            # a pyRofex/la API real). Se simula un fill inmediato y completo
            # al precio de referencia (el mid vigente al armar la orden), que
            # es la aproximacion estandar para no sesgar optimistamente al
            # motor de señales con fills al propio limite. Cada fill queda
            # auditado en logs/shadow_trades.csv.
            fill_price = state.reference_price or request.price
            state.status = OrderStatus.FILLED
            state.filled_quantity = request.quantity
            state.avg_fill_price = fill_price
            state.last_update_at = time.time()
            if self._shadow_logger is not None:
                self._shadow_logger.log_fill(request, fill_price, state.reference_price)
            logger.info(
                "[SHADOW] Fill simulado: %s %s x%.2f @ %.4f (id=%s, mercado real NO tocado)",
                request.side.value, request.symbol, request.quantity, fill_price, request.client_order_id,
            )
            return state

        response = send_order(
            ticker=request.symbol,
            side=request.side,
            size=request.quantity,
            price=request.price,
            order_type=request.order_type,
            client_order_id=request.client_order_id,
        )
        state.status = OrderStatus.REJECTED if response.get("status") == "error" else OrderStatus.NEW
        return state

    def cancel(self, client_order_id: str) -> None:
        state = self._orders.get(client_order_id)
        if state is None:
            return

        if SETTINGS.shadow.enabled:
            # En la practica send() ya deja la orden en FILLED de inmediato
            # en modo shadow, por lo que esta rama rara vez se ejerce; se
            # mantiene por completitud/seguridad (nunca debe llegar a tocar
            # cancel_order() real mientras el modo este activo).
            if self._shadow_logger is not None:
                self._shadow_logger.log_cancel(client_order_id, state.request.symbol)
            state.status = OrderStatus.CANCELLED
            state.last_update_at = time.time()
            return

        cancel_order(client_order_id)
        state.status = OrderStatus.CANCELLED
        state.last_update_at = time.time()

    def get_account_positions(self, account: Optional[str] = None) -> Dict:
        if SETTINGS.shadow.enabled:
            # No hay una cuenta real que consultar en modo shadow: se
            # sintetiza un resumen de posiciones a partir de los fills
            # simulados trackeados localmente (mismo criterio de signo que
            # usa portfolio/portfolio.py: compras suman, ventas restan).
            net_by_symbol: Dict[str, float] = {}
            for state in self._orders.values():
                if state.status is not OrderStatus.FILLED:
                    continue
                signed_qty = state.filled_quantity if state.request.side is OrderSide.BUY else -state.filled_quantity
                net_by_symbol[state.request.symbol] = net_by_symbol.get(state.request.symbol, 0.0) + signed_qty
            return {
                "positions": [{"symbol": s, "quantity": q} for s, q in net_by_symbol.items() if q != 0],
                "mode": "shadow",
            }
        return get_account_positions(account)

    def on_order_report(self, report: Dict) -> None:
        """
        Callback a enganchar en WebSocketConnectionManager(order_report_handler=...)
        para actualizar el estado local de la orden con lo que informa el broker.
        """
        client_order_id = report.get("clOrdId") or report.get("client_order_id")
        state = self._orders.get(client_order_id) if client_order_id else None
        if state is None:
            logger.debug("Order report para una orden no trackeada: %s", report)
            return

        status_raw = str(report.get("status", "")).upper()
        if status_raw == "FILLED":
            state.status = OrderStatus.FILLED
        elif status_raw in ("PARTIALLY_FILLED", "PARTIALLY FILLED"):
            state.status = OrderStatus.PARTIALLY_FILLED
        elif status_raw in ("CANCELLED", "CANCELED"):
            state.status = OrderStatus.CANCELLED
        elif status_raw == "REJECTED":
            state.status = OrderStatus.REJECTED
        elif status_raw == "NEW":
            state.status = OrderStatus.NEW

        state.filled_quantity = report.get("orderQty_filled", state.filled_quantity)
        state.avg_fill_price = report.get("avg_px", state.avg_fill_price)
        state.last_update_at = time.time()

    def should_reprice(self, client_order_id: str) -> bool:
        """
        Regla de 'tiempo maximo de exposicion': si una orden pasiva a
        mid-price no tuvo fill dentro de la ventana configurada, corresponde
        mejorar precio o cancelar/reintentar (ver execution/mid_price_exec.py).
        """
        state = self._orders.get(client_order_id)
        if state is None or state.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            return False
        elapsed = time.time() - state.sent_at
        return elapsed > self._timeout_seconds and state.price_improvements < self._max_improvements

    def get_state(self, client_order_id: str) -> Optional[OrderState]:
        return self._orders.get(client_order_id)

    def open_orders(self) -> Dict[str, OrderState]:
        return {
            cid: s for cid, s in self._orders.items()
            if s.status in (OrderStatus.PENDING_NEW, OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)
        }

    def cancel_all_open(self) -> None:
        """Usado en el graceful shutdown de run_bot.py."""
        for client_order_id in list(self.open_orders().keys()):
            logger.info("Cancelando orden abierta %s por shutdown.", client_order_id)
            self.cancel(client_order_id)
