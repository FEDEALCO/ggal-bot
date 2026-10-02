"""
conftest.py
============
Red de seguridad adicional, solo para corridas con pytest: importa
_env_isolation/_shadow_audit_isolation antes de coleccionar cualquier test
de este paquete, para que ningun test nuevo (presente o futuro) que se
olvide de importarlos explicitamente pueda (a) volver a escribir sobre el
CSV real de produccion (paths.SHADOW_TRADES_LOG, ver
_shadow_audit_isolation.py y docs/AUDITORIA_MAESTRA_2026-08-27.md seccion
3.3), o (b) heredar config/credenciales reales del .env/entorno del
usuario (ver _env_isolation.py - MEJORA 2026-10-02 a pedido explicito del
usuario, tras los bugs reales documentados en el commit "Fix: 3 bugs de
aislamiento en la suite de tests, hallados al verificar en Windows").

_env_isolation se importa EXPLICITAMENTE PRIMERO aca (aunque
_shadow_audit_isolation ya lo importa a su vez, ver su propio docstring):
es la unica forma de garantizar el orden correcto para pytest incluso si
algun archivo de test llegara a importar ggal_bot.config ANTES que
_shadow_audit_isolation (hay 2 casos asi hoy: test_dashboard_pnl.py y
test_technical_analysis.py - no rompen nada bajo pytest porque conftest.py
siempre se importa antes que cualquier modulo de test del directorio, pero
corregir el orden aca, en la unica fuente de verdad para pytest, es mas
robusto que confiar en que cada archivo nuevo lo haga bien).

Los archivos de test de este proyecto tambien corren de forma standalone
via `python -m ggal_bot.validation.test_X` (sin pytest) - para ESE modo,
conftest.py no aplica; cada archivo de test que ya importa
_shadow_audit_isolation queda protegido igual (ver su docstring), y los 2
casos sin esa importacion siguen siendo, en modo standalone, el mismo gap
preexistente que ya tenian con el aislamiento de shadow_trades.csv - no
agravado por este cambio.
"""

import pytest

from ggal_bot.validation import _env_isolation  # noqa: F401  (ver docstring de arriba: debe ir primero)
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


@pytest.fixture(autouse=True)
def _strip_real_env_vars_around_each_test():
    """
    Defensa EN PROFUNDIDAD, complementaria a _env_isolation.py (MEJORA
    2026-10-02, a pedido explicito del usuario). _env_isolation.py resuelve
    el problema de fondo (SETTINGS no debe congelar valores reales del
    entorno/.env del usuario al importarse) - este fixture cubre un caso
    DISTINTO y mas chico: un test que haga `os.environ["GGAL_BOT_X"] = ...`
    o similar a mano (en vez de mutar SETTINGS directamente, que es el
    patron que usa el resto de la suite) y se olvide de limpiarlo en su
    finally, lo que podria filtrarse a CUALQUIER test posterior en la misma
    corrida. Se corre ANTES y DESPUES de cada test, mismo criterio que el
    fixture de arriba para el kill switch.

    Deliberadamente NO restaura los valores originales despues del test:
    el objetivo explicito es que ninguna variable reconocida (GGAL_BOT_*,
    PYROFEX_*, BROKER_REST_*, o cualquier nombre que contenga PASSWORD/
    SECRET/TOKEN/APIKEY) este NUNCA presente durante una corrida de tests,
    ni antes ni despues de ningun test individual - restaurarla
    reintroduciria exactamente el valor real que todo este mecanismo busca
    eliminar.
    """
    from ggal_bot.validation._env_isolation import _is_dangerous
    import os as _os

    def _strip():
        for name in list(_os.environ.keys()):
            if _is_dangerous(name):
                _os.environ.pop(name, None)

    _strip()
    yield
    _strip()
