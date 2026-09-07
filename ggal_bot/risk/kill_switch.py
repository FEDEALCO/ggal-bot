"""
kill_switch.py
===============
Kill switch centralizado y limites de riesgo de PORTFOLIO (Fase 5.3, ver
AUDITORIA_FASE5.3_*.md). Distinto de RiskManager (risk_manager.py), que
evalua Griegas/liquidez POR SEÑAL/POR ESTRATEGIA (ver
Portfolio.greeks_for_strategy_tag) - este modulo evalua limites AGREGADOS
de TODA la cuenta (todas las estrategias, todos los simbolos) y, si se
disparan, bloquea ENTRADAS NUEVAS en todo el bot hasta que un operador lo
resetee explicitamente.

DISEÑO DELIBERADO - por que persistido en disco:
Sin esto, un halt de riesgo activado en una corrida se "olvida" en el
siguiente restart del proceso (exactamente la misma clase de problema que
motiva ggal_bot/portfolio/reconciliation.py: self.portfolio tampoco
persistia entre restarts). Un kill switch que no sobrevive a un restart no
sirve como control de riesgo real - un restart accidental (o un deploy)
podria levantar al bot de nuevo sin que nadie se entere de que el dia
anterior se habia disparado por perdida maxima.

DISEÑO DELIBERADO - por que NUNCA bloquea salidas:
Un kill switch que bloquea TODO (entradas y salidas) puede terminar
atrapando al bot con posiciones abiertas que no puede cerrar - el peor
escenario posible para un control de riesgo. Este kill switch SOLO se
consulta en run_bot.py::_act_on_entry_signal() (bloquea entradas nuevas);
_act_on_exit_signal() no lo consulta en ningun punto, a proposito, para que
Stop Loss/Take Profit/horizonte/guardia de fin de semana/kill switch de
Griegas sigan pudiendo cerrar posiciones ya abiertas sin importar el estado
del kill switch.

RESET: deliberadamente manual (no hay auto-recuperacion por tiempo ni por
ningun criterio automatico) - ver reset(). Se puede resetear via
`python -m ggal_bot.risk.kill_switch --reset "motivo"` o llamando a
KillSwitch().reset("motivo") directamente.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from ggal_bot import paths
from ggal_bot.config import RiskLimitsConfig

logger = logging.getLogger("ggal_bot.risk.kill_switch")


@dataclass
class KillSwitchState:
    tripped: bool = False
    reason: str = ""
    tripped_at: Optional[str] = None
    tripped_by: str = ""


class KillSwitch:
    """
    Estado persistido en `paths.KILL_SWITCH_STATE_FILE` (JSON, escritura
    atomica: tmp + replace, mismo patron que ggal_bot/state_writer.py).
    Se relee de disco en CADA `is_tripped()`/`evaluate()` (no cachea en
    memoria) para que un reset externo (ej. desde el dashboard, o desde
    otro proceso) se vea de inmediato sin necesitar reiniciar el bot.
    """

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path is not None else paths.KILL_SWITCH_STATE_FILE
        self._lock = threading.Lock()

    # -- Persistencia --------------------------------------------------------

    def _read(self) -> KillSwitchState:
        with self._lock:
            if not self._path.exists():
                return KillSwitchState()
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                return KillSwitchState(
                    tripped=bool(data.get("tripped", False)),
                    reason=str(data.get("reason", "")),
                    tripped_at=data.get("tripped_at"),
                    tripped_by=str(data.get("tripped_by", "")),
                )
            except Exception:
                logger.exception(
                    "No se pudo leer el estado del kill switch (%s) - se asume NO disparado "
                    "para no bloquear el bot por un archivo corrupto, pero esto se debe "
                    "investigar manualmente.", self._path,
                )
                return KillSwitchState()

    def _write(self, state: KillSwitchState) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "tripped": state.tripped, "reason": state.reason,
                "tripped_at": state.tripped_at, "tripped_by": state.tripped_by,
            }, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    # -- API publica -----------------------------------------------------------

    def is_tripped(self) -> bool:
        return self._read().tripped

    def status(self) -> KillSwitchState:
        return self._read()

    def trip(self, reason: str, tripped_by: str = "risk_engine") -> None:
        state = KillSwitchState(
            tripped=True, reason=reason,
            tripped_at=datetime.now(timezone.utc).isoformat(), tripped_by=tripped_by,
        )
        self._write(state)
        logger.critical("KILL SWITCH DISPARADO (%s): %s", tripped_by, reason)

    def reset(self, reason: str = "reset manual") -> None:
        """
        Deliberadamente la UNICA forma de volver a habilitar entradas
        nuevas tras un trip() - nunca automatico (ver docstring del
        modulo). Preserva un registro de que hubo un reset via el log, aun
        cuando el JSON persistido no distinga historial.
        """
        self._write(KillSwitchState(tripped=False, reason="", tripped_at=None, tripped_by=""))
        logger.warning("Kill switch reseteado manualmente: %s", reason)

    def evaluate(
        self,
        portfolio,
        limits: RiskLimitsConfig,
        realized_pnl_today_ars: Optional[float] = None,
    ) -> Optional[str]:
        """
        Evalua los limites agregados de portfolio contra `limits` y, si
        alguno se excede, llama a trip() y devuelve el motivo (string). Si
        no se excede ninguno, devuelve None SIN tocar el estado persistido
        (para no pisar un trip previo con reason distinta - `evaluate` solo
        dispara, nunca resetea; el reset es siempre manual, ver reset()).

        No hace nada (devuelve None sin evaluar) si `limits.enabled` es
        False - permite desactivar el kill switch entero via config sin
        tener que comentar el call site en run_bot.py.
        """
        if not limits.enabled:
            return None

        if limits.max_daily_loss_ars is not None and realized_pnl_today_ars is not None:
            if realized_pnl_today_ars <= -abs(limits.max_daily_loss_ars):
                reason = (
                    f"Perdida realizada del dia (${realized_pnl_today_ars:,.2f}) alcanzo/supero "
                    f"el limite configurado (${limits.max_daily_loss_ars:,.2f})."
                )
                self.trip(reason, tripped_by="max_daily_loss_ars")
                return reason

        if limits.max_open_contracts_total is not None:
            total_open = sum(abs(p.quantity) for p in portfolio.positions)
            if total_open > limits.max_open_contracts_total:
                reason = (
                    f"Contratos abiertos totales ({total_open:g}) supera el limite agregado "
                    f"configurado ({limits.max_open_contracts_total:g})."
                )
                self.trip(reason, tripped_by="max_open_contracts_total")
                return reason

        # Deteccion de integridad (ver docstring de
        # RiskLimitsConfig.max_positions_per_symbol_strategy): no dispara
        # el kill switch salvo que el limite este configurado en 1 (su
        # default) y se detecte una violacion real - de encontrarse, es
        # evidencia de fragmentacion/bypass de Guarda 2 en produccion.
        counts: Dict[tuple, int] = {}
        for p in portfolio.positions:
            if p.quantity <= 0:
                continue
            key = (p.symbol, p.strategy_tag or "weekly_asymmetric")
            counts[key] = counts.get(key, 0) + 1
        violations = {k: v for k, v in counts.items() if v > limits.max_positions_per_symbol_strategy}
        if violations:
            reason = (
                f"Integridad de portfolio violada: {len(violations)} base(s) con mas de "
                f"{limits.max_positions_per_symbol_strategy} Position activa simultanea "
                f"(fragmentacion no esperada, ver AUDITORIA_FASE5.2_LIFECYCLE_ROOT_CAUSE.md): "
                f"{violations}"
            )
            self.trip(reason, tripped_by="max_positions_per_symbol_strategy")
            return reason

        return None


def _cli() -> int:
    """`python -m ggal_bot.risk.kill_switch --reset "motivo"` /
    `python -m ggal_bot.risk.kill_switch --status`."""
    args = sys.argv[1:]
    ks = KillSwitch()
    if args and args[0] == "--reset":
        reason = args[1] if len(args) > 1 else "reset manual via CLI"
        ks.reset(reason)
        print(f"Kill switch reseteado: {reason}")
        return 0
    state = ks.status()
    print(json.dumps({
        "tripped": state.tripped, "reason": state.reason,
        "tripped_at": state.tripped_at, "tripped_by": state.tripped_by,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
