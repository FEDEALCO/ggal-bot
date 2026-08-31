"""
delta_hedger.py (execution) - DEPRECADO
=========================================
Este modulo se movio a `ggal_bot.strategy.delta_hedger` (DeltaHedgingEngine):
"cuando y cuanto rehedgear" es una decision de estrategia (usa las griegas
del portafolio), no de ejecucion. Se deja este archivo como alias de
compatibilidad para no romper imports existentes; usar el modulo nuevo en
codigo nuevo.
"""

from __future__ import annotations

import warnings

from ggal_bot.strategy.delta_hedger import DeltaHedgingEngine, HedgeInstruction

warnings.warn(
    "ggal_bot.execution.delta_hedger esta deprecado; usar "
    "ggal_bot.strategy.delta_hedger.DeltaHedgingEngine en su lugar.",
    DeprecationWarning,
    stacklevel=2,
)

# Alias de compatibilidad con el nombre de clase anterior.
DeltaHedger = DeltaHedgingEngine

__all__ = ["DeltaHedger", "DeltaHedgingEngine", "HedgeInstruction"]
