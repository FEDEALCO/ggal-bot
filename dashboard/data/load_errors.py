"""
load_errors.py
================
Registro centralizado de errores de carga para los loaders del dashboard
(dashboard/pnl_engine.py y dashboard/data/*.py).

BUG REAL CORREGIDO (2026-10-05, a pedido explicito del usuario, auditoria
completa de loaders tras encontrar el caso real en load_fills()): hasta
este cambio, CADA loader atrapaba su excepcion de parseo (CSV corrupto,
JSON invalido, etc.) y devolvia un valor vacio/default EN SILENCIO - sin
loguear nada ni avisar en la UI. El caso real que lo disparo:
shadow_trades.csv con un header desactualizado (ver
ggal_bot/execution/order_gateway.py), que rompia pandas.read_csv() con
ParserError sobre el archivo COMPLETO; dashboard/pnl_engine.py::load_fills()
lo atrapaba y devolvia 0 filas, y dashboard/app.py mostraba "Todavia no hay
operaciones registradas" - un mensaje de "no hay datos" para un archivo
que en realidad tenia 1200+ fills reales que no se pudieron leer.

Esto NO reemplaza el comportamiento de "archivo no existe todavia" (eso
sigue siendo un caso legitimo y silencioso - el bot nunca corrio, o es la
primera vez - ver cada loader para el chequeo de existencia ANTES de
intentar parsear). Esto es solo para el caso de "el archivo existe, tiene
contenido, y fallo al parsearlo" - eso SIEMPRE es una condicion anormal
que merece aviso.

register()/clear()/get_all() dan un lugar unico donde dashboard/app.py
puede preguntar, despues de llamar a todos los loaders de una corrida de
Streamlit, "¿algo fallo?" y mostrar un st.error() por cada archivo con
problemas - en vez de que cada loader decida por su cuenta como mostrar
(o no mostrar) su propio error. Los loaders fuera de Streamlit (tests,
ggal_bot/ops/*.py, reconciliation.py al arranque del bot) siguen
recibiendo su DataFrame/dict vacio de siempre (fail-safe por defecto, no
rompen su caller), pero ahora SIEMPRE queda un logger.error() real.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import List

logger = logging.getLogger("ggal_bot.dashboard.load_errors")

_lock = threading.Lock()
_errors: List["LoadError"] = []


@dataclass(frozen=True)
class LoadError:
    source: str   # ruta (str) del archivo que fallo al cargar
    reason: str   # "TipoDeExcepcion: mensaje"

    def __str__(self) -> str:  # pragma: no cover - solo para logging/debug
        return f"{self.source}: {self.reason}"


def clear() -> None:
    """dashboard/app.py la llama al principio de cada corrida/rerun de Streamlit."""
    with _lock:
        _errors.clear()


def register(source: str, reason: str) -> None:
    """
    Loguea SIEMPRE (corra o no dentro de Streamlit) y ademas lo deja
    disponible para que dashboard/app.py lo muestre como banner visible al
    final de la corrida via get_all().
    """
    logger.error("Error de carga en %s: %s", source, reason)
    with _lock:
        _errors.append(LoadError(source=str(source), reason=reason))


def get_all() -> List[LoadError]:
    with _lock:
        return list(_errors)
