"""
conftest.py
============
Red de seguridad adicional, solo para corridas con pytest: importa
_shadow_audit_isolation antes de coleccionar cualquier test de este
paquete, para que ningun test nuevo (presente o futuro) que se olvide de
importarlo explicitamente pueda volver a escribir sobre el CSV real de
produccion (paths.SHADOW_TRADES_LOG). Ver _shadow_audit_isolation.py y
docs/AUDITORIA_MAESTRA_2026-08-27.md seccion 3.3.

Los archivos de test de este proyecto tambien corren de forma standalone
via `python -m ggal_bot.validation.test_X` (sin pytest) - para ESE modo,
cada archivo de test importa _shadow_audit_isolation explicitamente al
principio, porque conftest.py no aplica fuera de pytest.
"""

import pytest

from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_shared_kill_switch_state():
    """
    Red de seguridad (Fase 5.3, agregada tras un caso real durante el
    desarrollo de ggal_bot/risk/kill_switch.py): a diferencia de
    ShadowAuditLogger (solo append, sin un estado "pegajoso" que cambie
    comportamiento entre tests), KillSwitch persiste tripped=True/False en
    disco - y _shadow_audit_isolation.py apunta paths.KILL_SWITCH_STATE_FILE
    a UN SOLO archivo compartido para TODA la corrida de pytest. Un test
    que dispare el kill switch (KillSwitch().trip(...)) sin resetearlo
    "contamina" cualquier otro test posterior en la MISMA corrida que
    construya un GgalOptionsBot() nuevo (su kill_switch por defecto lee del
    mismo archivo compartido) - se confirmo este exacto efecto en
    test_fase53_kill_switch.py contra test_scalping_mode.py/
    test_strategy_selector.py antes de este fixture. Se resetea ANTES y
    DESPUES de cada test para blindar contra el orden de ejecucion en
    cualquier direccion.
    """
    from ggal_bot import paths as _paths
    from ggal_bot.risk.kill_switch import KillSwitch

    KillSwitch(path=_paths.KILL_SWITCH_STATE_FILE).reset("autouse fixture: estado limpio antes del test")
    yield
    KillSwitch(path=_paths.KILL_SWITCH_STATE_FILE).reset("autouse fixture: estado limpio despues del test")
