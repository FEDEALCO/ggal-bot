"""
state_writer.py
================
Persiste periodicamente el estado del bot (griegas totales, griegas por
vencimiento, señales activas, estado de ordenes) a un JSON en disco, para
poder monitorear el bot desde afuera (un dashboard, un script de chequeo,
o simplemente inspeccion manual) sin acoplarse al proceso principal.
Mismo patron que state_writer.py en Quantbot.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from ggal_bot.paths import STATE_FILE


def _json_default(obj: Any) -> Any:
    if isinstance(obj, date):
        return obj.isoformat()
    return str(obj)


class StateWriter:
    def __init__(self, path=STATE_FILE):
        self.path = path

    def write(
        self,
        portfolio_greeks_total: Dict[str, float],
        portfolio_greeks_by_expiry: Dict[Optional[date], Dict[str, float]],
        active_signals: List[Dict[str, Any]],
        risk_breaches: str,
        extra: Optional[Dict[str, Any]] = None,
        option_chain_snapshot: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "portfolio_greeks_total": portfolio_greeks_total,
            "portfolio_greeks_by_expiry": {
                (k.isoformat() if isinstance(k, date) else "sin_vencimiento"): v
                for k, v in portfolio_greeks_by_expiry.items()
            },
            "active_signals": active_signals,
            "risk_breaches": risk_breaches,
            "extra": extra or {},
            # Snapshot de mercado (bid/ask/mid/IV/griegas por base vigente):
            # lo consume dashboard/pnl_engine.py para marcar a mercado las
            # posiciones abiertas y para el grafico de smile de IV. No lo
            # usa ninguna parte del motor de trading (run_bot.py solo
            # ESCRIBE aca; nadie adentro del bot lo relee).
            "option_chain_snapshot": option_chain_snapshot or [],
        }
        tmp_path = self.path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=_json_default, ensure_ascii=False)
        tmp_path.replace(self.path)  # escritura atomica: evita leer un JSON a medio escribir
