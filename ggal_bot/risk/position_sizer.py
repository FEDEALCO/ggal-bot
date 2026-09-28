"""
position_sizer.py
===================
Calculo de tamaño de posicion (cantidad de contratos) para el modo
"Long-First / Weekly Asymmetric" (ver config.LongFirstConfig y
strategy/weekly_asymmetric.py): dado un capital asignado a un trade y la
prima de la opcion, devuelve la cantidad ENTERA de contratos a comprar, sin
exceder el capital total configurado ni el limite de riesgo por trade.

Formula (piso, nunca una fraccion de contrato):

    contratos = floor( capital_asignado_al_trade / (prima * multiplicador) )

    capital_asignado_al_trade = capital_disponible * max_risk_pct_per_trade

IMPORTANTE: bajo el modo Long-First, comprar una opcion es la operacion de
riesgo MAXIMO DEFINIDO por construccion (la perdida maxima es la prima
pagada, nunca mas, porque este modo prohibe posiciones descubiertas - ver
config.LongFirstConfig.forbid_naked_short). Por eso "capital arriesgado por
trade" es simplemente "capital destinado a esa compra": no hace falta un
modelo de margen como el que si requeriria una posicion descubierta.

Este modulo NO decide QUE comprar (eso es
strategy/weekly_asymmetric.py.scan_entry_signals) ni CUANDO cerrar (eso es
risk/risk_manager.py.evaluate_position_exit) - solo CUANTOS contratos, dado
un precio de prima y el capital disponible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from ggal_bot.config import SETTINGS


@dataclass
class SizingResult:
    contracts: int
    capital_allocated_ars: float       # capital destinado a este trade (capital_disponible * max_risk_pct_per_trade)
    capital_used_ars: float             # contracts * prima * multiplicador (lo que realmente se gasta)
    capital_remaining_ars: float        # capital_allocated_ars - capital_used_ars (se pierde por el piso de redondeo)
    rejected_reason: Optional[str] = None  # None si contracts > 0

    @property
    def is_tradeable(self) -> bool:
        return self.contracts > 0 and self.rejected_reason is None


class PositionSizer:
    """
    Instanciar una vez (ver run_bot.py) y reusar; los parametros por
    defecto se leen de SETTINGS.long_first, pero se pueden overridear por
    instancia (util para tests o para correr varias configuraciones de
    capital en paralelo).
    """

    def __init__(
        self,
        max_capital_ars: Optional[float] = None,
        max_risk_pct_per_trade: Optional[float] = None,
        option_multiplier: Optional[float] = None,
        min_contracts: Optional[int] = None,
    ):
        cfg = SETTINGS.long_first
        self.max_capital_ars = max_capital_ars if max_capital_ars is not None else cfg.max_capital_ars
        self.max_risk_pct_per_trade = (
            max_risk_pct_per_trade if max_risk_pct_per_trade is not None else cfg.max_risk_pct_per_trade
        )
        self.option_multiplier = (
            option_multiplier if option_multiplier is not None else SETTINGS.instruments.option_multiplier
        )
        self.min_contracts = min_contracts if min_contracts is not None else cfg.min_contracts_per_trade

        # --- Sizing por CONVICCION de la señal (MEJORA 2026-09-28, ver
        # config.LongFirstConfig.enable_conviction_sizing) --- getattr()
        # defensivo: esta clase la reusan otras estrategias (ver
        # ScalpingConfig, que hoy no tiene estos campos) pasando un `cfg`
        # distinto de SETTINGS.long_first en teoria - aunque hoy siempre se
        # construye contra SETTINGS.long_first (self.cfg no se guarda), este
        # patron evita un AttributeError si eso cambiara.
        self.conviction_sizing_enabled = getattr(cfg, "enable_conviction_sizing", False)
        self.conviction_sizing_reference_vol_points = getattr(
            cfg, "conviction_sizing_reference_vol_points", None,
        ) or getattr(cfg, "smile_threshold_vol_points", None)
        self.conviction_sizing_min_multiplier = getattr(cfg, "conviction_sizing_min_multiplier", 1.0)
        self.conviction_sizing_max_multiplier = getattr(cfg, "conviction_sizing_max_multiplier", 1.0)

    def conviction_multiplier_for(self, iv_dislocation_vol_points: Optional[float]) -> float:
        """
        Multiplicador de capital asignado en funcion de la magnitud de la
        dislocacion de IV que motivo la señal (ver docstring de
        config.LongFirstConfig.enable_conviction_sizing). 1.0 (no-op,
        idem sizing actual) si el flag esta apagado, si no hay dislocacion
        provista, o si no hay una referencia valida contra la cual escalar.
        """
        if not self.conviction_sizing_enabled or iv_dislocation_vol_points is None:
            return 1.0
        reference = self.conviction_sizing_reference_vol_points
        if not reference or reference <= 0:
            return 1.0
        raw_multiplier = abs(iv_dislocation_vol_points) / reference
        return max(self.conviction_sizing_min_multiplier, min(self.conviction_sizing_max_multiplier, raw_multiplier))

    def compute_contracts(
        self,
        premium_price: float,
        capital_available_ars: Optional[float] = None,
        max_risk_pct_override: Optional[float] = None,
        conviction_multiplier: float = 1.0,
    ) -> SizingResult:
        """
        `capital_available_ars`: capital LIBRE actual (capital total menos
        lo ya comprometido en posiciones abiertas), si el llamador lo
        trackea (ej. run_bot.py deduciendo lo ya usado esta semana). Si se
        omite, se asume el capital maximo configurado completo.

        `conviction_multiplier` (MEJORA 2026-09-28, ver
        conviction_multiplier_for() arriba): escala el capital asignado a
        ESTE trade especifico. Default 1.0 (comportamiento identico al de
        antes de esta mejora para cualquier llamador que no lo pase) -
        nunca se acota aca de nuevo (ya viene acotado de
        conviction_multiplier_for si el llamador lo uso), pero un valor
        negativo o cero se trata como "sin asignacion" (mismo criterio que
        una prima invalida), nunca como asignar capital negativo.
        """
        if premium_price is None or premium_price <= 0:
            return SizingResult(0, 0.0, 0.0, 0.0, rejected_reason="prima_invalida")
        if conviction_multiplier <= 0:
            return SizingResult(0, 0.0, 0.0, 0.0, rejected_reason="conviction_multiplier_invalido")

        capital_base = capital_available_ars if capital_available_ars is not None else self.max_capital_ars
        # Nunca por encima del techo configurado, aunque el llamador pase un
        # capital_available_ars mas alto por error.
        capital_base = max(0.0, min(capital_base, self.max_capital_ars))

        risk_pct = max_risk_pct_override if max_risk_pct_override is not None else self.max_risk_pct_per_trade
        capital_allocated = capital_base * risk_pct * conviction_multiplier

        cost_per_contract = premium_price * self.option_multiplier
        if cost_per_contract <= 0:
            return SizingResult(
                0, capital_allocated, 0.0, capital_allocated,
                rejected_reason="costo_por_contrato_invalido",
            )

        contracts = math.floor(capital_allocated / cost_per_contract)
        if contracts < self.min_contracts:
            return SizingResult(
                0, capital_allocated, 0.0, capital_allocated,
                rejected_reason=(
                    f"capital_insuficiente: se necesitan $ {cost_per_contract:,.2f} por contrato "
                    f"y hay $ {capital_allocated:,.2f} asignados a este trade "
                    f"(capital_disponible=$ {capital_base:,.2f} x {risk_pct:.0%})"
                ),
            )

        capital_used = contracts * cost_per_contract
        return SizingResult(
            contracts=contracts,
            capital_allocated_ars=capital_allocated,
            capital_used_ars=capital_used,
            capital_remaining_ars=capital_allocated - capital_used,
        )
