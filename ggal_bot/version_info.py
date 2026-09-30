"""
version_info.py
==================
SHA del commit de git que corre en ESTE deploy (MEJORA 2026-09-30, a
pedido explicito del usuario - ver REPORT.md, "Salud del bot: version/
commit desplegado").

GAP QUE CIERRA: verificado por grep (no asumido) que, hasta esta mejora,
ningun mecanismo bakeaba el SHA de git en la imagen desplegada - el
Dockerfile no tenia ningun ARG/LABEL con esa informacion, y .dockerignore
excluye .git/ tanto de la imagen COMO del build context (por eso
`git rev-parse HEAD` no se puede correr DENTRO del Dockerfile: el
directorio .git ni siquiera llega al daemon de Docker). La unica forma de
que el SHA llegue a la imagen es que quien dispara el build lo calcule
AFUERA y lo pase como --build-arg GIT_SHA=<sha> (ver Dockerfile: ARG
GIT_SHA + ENV GGAL_BOT_GIT_SHA=${GIT_SHA}).

LIMITACION EXPLICITA, NO VERIFICADA (no fabricada): no encontre
documentacion publica de Northflank que indique una variable
auto-inyectada con el SHA del commit que se esta buildeando (busque
"build arguments"/"inject build arguments" - Northflank soporta pasar
build-args custom, pero no un valor automatico de git SHA). Esto implica
que, salvo que Northflank exponga esto de otra forma no documentada
publicamente, el usuario tiene que configurar el build-arg GIT_SHA el
mismo (manualmente, o con un paso de CI que lo calcule) para que este
mecanismo funcione en Northflank - queda como tarea pendiente del lado de
infraestructura, fuera del alcance de este repo.

Default "unknown" (build local sin build-arg, o build en Northflank sin
configurar la variable) - nunca se fabrica un SHA.
"""
from __future__ import annotations

import os

_UNKNOWN = "unknown"


def get_deployed_git_sha() -> str:
    return os.environ.get("GGAL_BOT_GIT_SHA", "").strip() or _UNKNOWN
