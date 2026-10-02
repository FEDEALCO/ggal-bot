"""
_env_isolation.py
====================
Aisla la suite de tests de CUALQUIER .env / variable de entorno real del
usuario - a pedido explicito del usuario (sesion 2026-10-02), despues de
que 3 tests fallaran en su maquina real porque heredaban config/flags
reales (GGAL_BOT_ENABLE_SCALPING=true, pyRofex instalado - ver el commit
"Fix: 3 bugs de aislamiento en la suite de tests, hallados al verificar en
Windows"). El usuario pidio explicitamente que, ademas de corregir esos
tests puntuales, la suite entera quede blindada para que ESTO NO VUELVA A
PASAR con ninguna variable (incluidas credenciales) sin tener que acordarse
de aislar cada test nuevo a mano.

POR QUE NO ALCANZA CON UN FIXTURE DE PYTEST (lo que se pidio literalmente,
"fixture que limpie las GGAL_BOT_* y cualquier credencial del entorno"):
un fixture de pytest corre POR TEST, mucho DESPUES de que pytest ya
importo/coleccion todos los modulos de test. Pero `ggal_bot/config.py` lee
TODAS sus variables de entorno (os.getenv/_env_bool/_env_float, y antes
que nada, python-dotenv.load_dotenv() contra el .env real del proyecto)
como VALORES DEFAULT DE CAMPOS DE DATACLASS - expresiones evaluadas UNA
SOLA VEZ, en el momento en que el modulo `ggal_bot.config` se importa por
PRIMERA VEZ en todo el proceso (tipicamente, el primer test que haga
`from ggal_bot.config import SETTINGS` o transitivamente `from run_bot
import GgalOptionsBot`). Para cuando un fixture de pytest llega a correr,
SETTINGS ya quedo "congelado" con los valores reales del .env/entorno del
usuario - limpiar os.environ en ese punto no tiene ningun efecto retroactivo
sobre los campos ya evaluados. Por eso este modulo hace el trabajo real a
NIVEL DE IMPORT (no de fixture), y DEBE importarse antes que cualquier cosa
que transitivamente importe ggal_bot.config - exactamente el mismo
requisito/mecanismo que ya usa _shadow_audit_isolation.py (que este modulo
mismo importa primero, para heredar su misma garantia de orden: todo test
que ya importaba _shadow_audit_isolation - la convencion obligatoria de
este proyecto para tocar OrderGateway/GgalOptionsBot en modo shadow - ahora
tambien queda aislado del entorno real sin tener que tocar cada archivo).
conftest.py (pytest) tambien lo importa explicitamente, como primera linea,
por claridad y como defensa adicional para los pocos archivos de test que
leen SETTINGS sin pasar por _shadow_audit_isolation.

QUE HACE (a nivel de import, una sola vez por proceso):
    1. Neutraliza dotenv.load_dotenv() (no-op) ANTES de que config.py
       pueda invocarlo - el .env real del proyecto (si existe en el
       filesystem) nunca se lee durante una corrida de tests, sin
       necesidad de borrarlo ni tocarlo.
    2. Borra de os.environ, ANTES de que se evalue cualquier default de
       ggal_bot.config, toda variable cuyo nombre empiece con alguno de
       los prefijos reconocidos de este proyecto (GGAL_BOT_, PYROFEX_,
       BROKER_REST_) o que "suene" a credencial por su nombre (contiene
       PASSWORD, SECRET, TOKEN, APIKEY) - red de seguridad generica para
       una variable de credencial que se agregue en el futuro sin
       actualizar este modulo.

Deliberadamente NO se restauran al final del proceso: el objetivo es que
NINGUNA corrida de tests, en ningun orden ni combinacion de archivos, vea
jamas un valor real - no solo "durante" un test puntual.

Ver tambien el fixture `_assert_no_real_credentials_leaked` en conftest.py:
defensa adicional, por-test, contra el caso (distinto del que resuelve este
modulo) de que un test individual setee una variable real a mano con
os.environ[...] = ... y se olvide de restaurarla.

EXCEPCION DELIBERADA - GGAL_BOT_ALLOW_MOCK_SOURCE (2026-10-02, a pedido
explicito del usuario, item 3 de su instruccion): ShadowConfig.allow_mock_source
(ver ggal_bot/config.py) ahora bloquea la construccion de MockReplaySource
fuera de "tests/dev" - exactamente las palabras del usuario. Este modulo ES
el punto de verdad de "estamos en la suite de tests" (se importa a nivel de
import, antes de que config.py congele sus defaults), asi que es el lugar
correcto para otorgar esa unica excepcion explicita: fuerza
GGAL_BOT_ALLOW_MOCK_SOURCE=true para toda corrida de tests, DESPUES de la
purga de variables peligrosas de arriba (si el usuario tuviera esa variable
en su .env/entorno real, igual queda purgada primero y resetada a "true" aca
de forma deliberada y explicita - no es una fuga, es la excepcion pedida).
Sin esto, toda la suite existente que construye MockReplaySource/LiveShadowFeed
con mock en su source_priority empezaria a fallar con el RuntimeError nuevo.
"""
from __future__ import annotations

import os

# Prefijos de variables reconocidas de este proyecto (ver config.py: todo
# _env_bool/_env_float/_env_int con ese prefijo, mas las credenciales de
# BrokerConfig/BrokerRestConfig, que usan os.getenv directo sin prefijo
# GGAL_BOT_ - PYROFEX_* y BROKER_REST_*).
_RECOGNIZED_PREFIXES = ("GGAL_BOT_", "PYROFEX_", "BROKER_REST_")

# Red de seguridad generica, por nombre, para cualquier otra variable de
# credencial presente o futura que no caiga bajo los prefijos de arriba.
_CREDENTIAL_SUBSTRINGS = ("PASSWORD", "SECRET", "TOKEN", "APIKEY", "API_KEY")


def _is_dangerous(name: str) -> bool:
    upper = name.upper()
    if upper.startswith(_RECOGNIZED_PREFIXES):
        return True
    return any(substr in upper for substr in _CREDENTIAL_SUBSTRINGS)


# 1) Neutraliza dotenv ANTES de que ggal_bot.config pueda importarlo y
# llamarlo - si este modulo ya corrio, load_dotenv() es un no-op para el
# resto del proceso, sin importar cuantas veces se re-importe config.py.
try:
    import dotenv as _dotenv_module
    _dotenv_module.load_dotenv = lambda *args, **kwargs: False
except ImportError:
    # python-dotenv no instalado: config.py ya tolera este caso (ImportError
    # silencioso), no hay nada que neutralizar.
    pass

# 2) Purga variables peligrosas YA presentes en el entorno del proceso
# (heredadas del .env real si algo las cargo antes que este modulo, de la
# sesion de shell del usuario, o de variables de sistema) ANTES de que
# ggal_bot.config pueda leerlas como default de un campo.
for _name in list(os.environ.keys()):
    if _is_dangerous(_name):
        os.environ.pop(_name, None)

# 3) Excepcion deliberada "tests/dev" para MockReplaySource (ver docstring
# de arriba): se fuerza DESPUES de la purga de (2), nunca antes.
os.environ["GGAL_BOT_ALLOW_MOCK_SOURCE"] = "true"
