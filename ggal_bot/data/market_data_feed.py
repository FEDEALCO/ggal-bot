"""
market_data_feed.py
====================
Parsing de mercado (matriz de puntas) y bootstrap del universo de
instrumentos de GGAL. Este modulo YA NO abre la conexion de PyRofex por si
mismo (eso lo hace order_gateway.initialize_environment() +
order_gateway.WebSocketConnectionManager, que multiplexan market data y
order reports sobre el mismo websocket): MarketDataFeed se limita a (a)
descubrir el universo de instrumentos vigentes (bootstrap_universe) y
(b) traducir cada mensaje de market data entrante a un OrderBookSnapshot
para el resto del bot. Ver run_bot.py para el cableado completo.

Referencia de la API de pyRofex (puede variar segun version del paquete;
validar contra la documentacion oficial del proveedor antes de operar):
    pyRofex.get_all_instruments() / get_detailed_instruments()
    pyRofex.market_data_subscription(tickers=[...], entries=[...])
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OptionChain, OptionQuote, OrderBookSnapshot
from ggal_bot.models.black_scholes import OptionType

logger = logging.getLogger("ggal_bot.market_data_feed")

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

# A=Enero ... L=Diciembre. Convencion tipo OCC adaptada: como el tipo
# (call/put) ya se identifica por el prefijo del simbolo (GFGC/GFGV), no hace
# falta el segundo rango de letras (M-X) que usa OCC para diferenciar tipo.
# AJUSTAR si el ALYC usa una convencion de letras distinta.
_MONTH_LETTER_MAP = {chr(ord("A") + i): i + 1 for i in range(12)}


class MarketDataFeed:
    """
    Traduce los mensajes de market data de pyRofex a OrderBookSnapshot y los
    despacha a un callback provisto por el orquestador (run_bot.py). Tambien
    resuelve el universo de instrumentos de GGAL a suscribir.
    """

    def __init__(self, on_book_update: Callable[[str, OrderBookSnapshot], None]):
        self.on_book_update = on_book_update

    # -- Suscripcion de mercado --------------------------------------------

    def subscribe(self, tickers: List[str]) -> None:
        """
        Suscribe a puntas (bid/ask) y ultimo operado. Requiere que el
        websocket ya este conectado (ver order_gateway.WebSocketConnectionManager.connect()).
        """
        if not _PYROFEX_AVAILABLE:
            raise RuntimeError("pyRofex no disponible.")
        if not tickers:
            logger.warning("subscribe() llamado con una lista de tickers vacia.")
            return
        entries = [
            pyRofex.MarketDataEntry.BIDS,
            pyRofex.MarketDataEntry.OFFERS,
            pyRofex.MarketDataEntry.LAST,
        ]
        pyRofex.market_data_subscription(tickers=tickers, entries=entries)
        logger.info("Suscripto a %d instrumentos.", len(tickers))

    # -- Handler de mercado (registrado en WebSocketConnectionManager) ----

    def handle_market_data(self, message: Dict) -> None:
        """
        Traduce un mensaje de market data de pyRofex a un OrderBookSnapshot
        y lo despacha via el callback registrado. La forma exacta del
        payload depende de la version de pyRofex; validar contra un log
        real antes de confiar en este parsing en produccion.
        """
        try:
            symbol = message["instrumentId"]["symbol"]
            md = message.get("marketData", {})
            bids = md.get("BI", [])
            offers = md.get("OF", [])
            last = md.get("LA", {})

            bid = bids[0]["price"] if bids else 0.0
            bid_size = bids[0]["size"] if bids else 0.0
            ask = offers[0]["price"] if offers else 0.0
            ask_size = offers[0]["size"] if offers else 0.0
            last_volume = last.get("size", 0.0) if isinstance(last, dict) else 0.0

            book = OrderBookSnapshot(
                symbol=symbol, bid=bid, ask=ask,
                bid_size=bid_size, ask_size=ask_size, last_volume=last_volume,
            )
            self.on_book_update(symbol, book)
        except (KeyError, IndexError) as exc:
            logger.debug("Mensaje de market data incompleto, se ignora: %s (%s)", message, exc)

    # -- Bootstrap del universo de instrumentos ----------------------------

    def bootstrap_universe(self, option_chain: OptionChain) -> List[str]:
        """
        Descubre los instrumentos vigentes de GGAL (contado, futuro si hay,
        y toda la cadena de opciones de los proximos
        SETTINGS.instruments.expiries_ahead vencimientos), puebla
        `option_chain` con una OptionQuote por cada base (sin datos de
        mercado todavia: el book llega despues via handle_market_data), y
        devuelve la lista de tickers a suscribir.

        Identifica las opciones de GGAL en dos pasadas, de mas a menos
        confiable:

            1. Semantica (PRIMARIA): el propio instrumento trae un campo
               `underlying` (subyacente) y `cficode` (codigo ISO 10962: la
               categoria 'O' = Option) y/o un `strike` numerico > 0. Esto no
               depende de como este armado el texto del simbolo, y es lo que
               confirmo funcionar contra datos reales de un ALYC (ver
               diagnose_instruments.py). Ademas prioriza leer strike y
               vencimiento directamente de la metadata del instrumento.
            2. Por prefijo de simbolo (FALLBACK): si el ALYC no completa
               `underlying`/`cficode` de forma confiable, se cae al patron
               configurado en InstrumentsConfig.call_prefix/put_prefix (ver
               _parse_option_symbol para el detalle de esa heuristica).
        """
        instruments = self._fetch_instruments()
        if not instruments:
            logger.error(
                "bootstrap_universe: no se pudo obtener el listado de instrumentos. "
                "Verificar conexion/credenciales antes de continuar."
            )
            return self._fallback_tickers()

        cfg = SETTINGS.instruments
        candidates: List[Tuple[str, OptionType, float, date]] = []  # symbol, type, strike, expiry

        for inst in instruments:
            symbol = self._extract_symbol(inst)
            if not symbol:
                continue

            classification = self._classify_option(inst, symbol)
            if classification is None:
                continue
            option_type, bare_for_fallback, prefix_for_fallback = classification

            parsed = self._resolve_strike_and_expiry(inst, bare_for_fallback, prefix_for_fallback)
            if parsed is None:
                logger.debug("No se pudo resolver strike/vencimiento para %s, se descarta.", symbol)
                continue
            strike, expiry = parsed
            candidates.append((symbol, option_type, strike, expiry))

        if not candidates:
            logger.warning(
                "bootstrap_universe: no se encontraron opciones de GGAL (ni por "
                "underlying/cficode ni por los prefijos %s/%s configurados). "
                "Correr diagnose_instruments.py para confirmar si el ALYC/ambiente "
                "tiene opciones de GGAL aprovisionadas.",
                cfg.call_prefix, cfg.put_prefix,
            )
            return self._fallback_tickers()

        # Nos quedamos solo con los N vencimientos mas cercanos configurados.
        distinct_expiries = sorted({c[3] for c in candidates if c[3] >= date.today()})
        kept_expiries = set(distinct_expiries[: cfg.expiries_ahead])

        today = date.today()
        tickers: List[str] = [cfg.contado_ticker]
        if cfg.futuro_ticker:
            tickers.append(cfg.futuro_ticker)

        count = 0
        for symbol, option_type, strike, expiry in candidates:
            if expiry not in kept_expiries:
                continue
            days_cal = (expiry - today).days
            days_biz = _business_days_between(today, expiry)
            placeholder_book = OrderBookSnapshot(symbol=symbol, bid=0.0, ask=0.0, bid_size=0.0, ask_size=0.0)
            quote = OptionQuote(
                symbol=symbol, strike=strike, expiry=expiry, option_type=option_type,
                book=placeholder_book, days_calendar=days_cal, days_business=days_biz,
            )
            option_chain.upsert_quote(quote)
            tickers.append(symbol)
            count += 1

        logger.info(
            "bootstrap_universe: %d opciones cargadas en %d vencimientos (%s).",
            count, len(kept_expiries), ", ".join(e.isoformat() for e in sorted(kept_expiries)),
        )
        return tickers

    def _fallback_tickers(self) -> List[str]:
        cfg = SETTINGS.instruments
        tickers = [cfg.contado_ticker]
        if cfg.futuro_ticker:
            tickers.append(cfg.futuro_ticker)
        return tickers

    def _fetch_instruments(self) -> List[Dict]:
        """
        Intenta varios nombres de funcion de pyRofex para listar instrumentos,
        ya que ha variado entre versiones de la libreria.
        """
        if not _PYROFEX_AVAILABLE:
            return []
        for fn_name in ("get_detailed_instruments", "get_all_instruments", "get_instruments"):
            fn = getattr(pyRofex, fn_name, None)
            if not callable(fn):
                continue
            try:
                response = fn()
                instruments = response.get("instruments", response) if isinstance(response, dict) else response
                if instruments:
                    logger.info("Instrumentos obtenidos via pyRofex.%s() (%d items).", fn_name, len(instruments))
                    return list(instruments)
            except Exception as exc:
                logger.warning("pyRofex.%s() fallo: %s", fn_name, exc)
        return []

    @staticmethod
    def _extract_symbol(inst: Dict) -> Optional[str]:
        instrument_id = inst.get("instrumentId") or {}
        symbol = instrument_id.get("symbol") or inst.get("symbol")
        return symbol

    def _classify_option(self, inst: Dict, symbol: str) -> Optional[Tuple[OptionType, str, str]]:
        """
        Determina si `inst` es una opcion de GGAL y de que tipo, probando
        primero el enfoque semantico (underlying/cficode/strike) y cayendo
        al prefijo de simbolo configurado si ese enfoque no da resultado.
        Devuelve (option_type, simbolo_bare, prefijo_para_fallback_de_parseo)
        o None si no es una opcion de GGAL.
        """
        cfg = SETTINGS.instruments
        bare = _bare_symbol(symbol)

        # --- Enfoque semantico (primario) ---
        underlying = str(inst.get("underlying") or "").upper()
        if underlying == cfg.underlying_symbol.upper():
            cficode = str(inst.get("cficode") or "").upper()
            strike_value = _to_float(inst.get("strike"))
            is_option = cficode.startswith("O") or strike_value > 0
            if is_option:
                option_type = _option_type_from_cficode(cficode)
                if option_type is None:
                    # cficode ausente/no concluyente: usar el prefijo del
                    # simbolo solo para desambiguar call vs. put, ya
                    # confirmamos por 'underlying' que es una opcion de GGAL.
                    if bare.startswith(cfg.call_prefix):
                        option_type = OptionType.CALL
                    elif bare.startswith(cfg.put_prefix):
                        option_type = OptionType.PUT
                if option_type is not None:
                    return option_type, bare, ""  # ya tenemos underlying: no hace falta el prefijo para el parseo
                logger.debug(
                    "Instrumento %s tiene underlying=GGAL y parece opcion, pero no se pudo "
                    "determinar call/put (cficode=%r); se descarta.", symbol, inst.get("cficode"),
                )
            return None  # es un derivado de GGAL pero no una opcion (futuro, CI, etc.)

        # --- Fallback: prefijo de simbolo (para ALYCs sin underlying/cficode confiables) ---
        if bare.startswith(cfg.call_prefix) or cfg.call_prefix in symbol:
            return OptionType.CALL, bare, cfg.call_prefix
        if bare.startswith(cfg.put_prefix) or cfg.put_prefix in symbol:
            return OptionType.PUT, bare, cfg.put_prefix
        return None

    def _resolve_strike_and_expiry(
        self, inst: Dict, symbol: str, prefix: str,
    ) -> Optional[Tuple[float, date]]:
        """
        Primero intenta leer strike/vencimiento de la metadata real del
        instrumento (nombres de campo variables segun ALYC/version de
        pyRofex); si no estan, cae al parseo heuristico del simbolo.
        """
        strike = self._first_present(inst, ("strikePrice", "strike_price", "strike"))
        maturity_raw = self._first_present(
            inst, ("maturityDate", "maturity_date", "dueDate", "expirationDate", "expiration_date"),
        )
        expiry = self._parse_date(maturity_raw) if maturity_raw is not None else None

        if strike is not None and expiry is not None:
            try:
                return float(strike), expiry
            except (TypeError, ValueError):
                pass

        # Fallback: parseo heuristico del simbolo (ver docstring del modulo
        # sobre la convencion de letra de mes; AJUSTAR el regex en
        # config.InstrumentsConfig.option_symbol_regex al formato real de tu ALYC).
        return self._parse_option_symbol(symbol, prefix)

    @staticmethod
    def _first_present(d: Dict, keys: Tuple[str, ...]):
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
        return None

    @staticmethod
    def _parse_date(raw) -> Optional[date]:
        if isinstance(raw, date):
            return raw
        if isinstance(raw, str):
            for fmt in ("%Y%m%d", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    return datetime.strptime(raw, fmt).date()
                except ValueError:
                    continue
        return None

    @staticmethod
    def _parse_option_symbol(symbol: str, prefix: str) -> Optional[Tuple[float, date]]:
        """
        Heuristica de fallback: separa el prefijo (GFGC/GFGV) y matchea el
        resto contra config.InstrumentsConfig.option_symbol_regex (default:
        digitos de strike + 1 letra de mes, A=Enero..L=Diciembre). El año se
        resuelve como la proxima ocurrencia futura de ese mes, y el dia como
        el tercer viernes del mes (convencion habitual de vencimiento de
        opciones; AJUSTAR si tu ALYC usa otra fecha fija).

        Este fallback es deliberadamente conservador: si el simbolo no
        matchea, devuelve None y bootstrap_universe() descarta esa base en
        vez de arriesgar un strike/vencimiento incorrecto.
        """
        remainder = symbol[len(prefix):] if symbol.startswith(prefix) else symbol
        pattern = SETTINGS.instruments.option_symbol_regex
        match = re.match(pattern, remainder)
        if not match:
            return None
        strike_digits, month_letter = match.group(1), match.group(2)
        month = _MONTH_LETTER_MAP.get(month_letter.upper())
        if month is None:
            return None
        try:
            strike = float(strike_digits) * SETTINGS.instruments.strike_scale
        except ValueError:
            return None
        expiry = _third_friday_on_or_after(date.today(), month)
        return strike, expiry


def _to_float(value) -> float:
    """Conversion tolerante: devuelve 0.0 si el valor es None/vacio/no numerico."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _option_type_from_cficode(cficode: str) -> Optional[OptionType]:
    """
    ISO 10962 CFI code: 6 caracteres, categoria 'O' = Option, y el segundo
    caracter (Group) indica 'C' (Call) o 'P' (Put) en la mayoria de las
    implementaciones que siguen el estandar. AJUSTAR si tu ALYC usa una
    variante distinta (algunos feeds locales no completan cficode con
    precision; por eso este helper puede devolver None y dejar que
    _classify_option() caiga al prefijo del simbolo como desambiguador).
    """
    if len(cficode) < 2 or not cficode.startswith("O"):
        return None
    if cficode[1] == "C":
        return OptionType.CALL
    if cficode[1] == "P":
        return OptionType.PUT
    return None


def _bare_symbol(full_symbol: str) -> str:
    """
    Los tickers de pyRofex suelen venir con el formato completo
    'MERV - XMEV - <SIMBOLO> - <LIQUIDACION>' (asi esta definido, por ejemplo,
    InstrumentsConfig.contado_ticker = "MERV - XMEV - GGAL - 24hs"). Esta
    funcion devuelve solo <SIMBOLO> cuando el ticker matchea ese formato de 4
    segmentos separados por ' - ', o el string original sin cambios si no
    (algunos ALYCs devuelven el simbolo "pelado" directamente). Se usa para
    que el matching de prefijos (GFGC/GFGV) y el parseo de strike/vencimiento
    no dependan de si vino envuelto en el ticker completo o no.
    """
    parts = [p.strip() for p in full_symbol.split(" - ")]
    if len(parts) >= 3:
        return parts[2]
    return full_symbol


def _third_friday_on_or_after(reference: date, month: int) -> date:
    """Tercer viernes del proximo mes calendario 'month' a partir de 'reference'."""
    year = reference.year if month >= reference.month else reference.year + 1
    first_of_month = date(year, month, 1)
    first_friday_offset = (4 - first_of_month.weekday()) % 7  # weekday(): lunes=0 ... viernes=4
    third_friday = first_of_month + timedelta(days=first_friday_offset + 14)
    return third_friday


def _business_days_between(start: date, end: date) -> int:
    """Cuenta dias habiles (lunes a viernes) entre dos fechas, sin feriados locales."""
    if end <= start:
        return 0
    days = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:  # 0-4 = lunes a viernes
            days += 1
    return days
