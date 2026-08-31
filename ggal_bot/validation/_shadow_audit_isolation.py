"""
_shadow_audit_isolation.py
===========================
Red de seguridad para que ningun test de la suite escriba, por accidente,
sobre el CSV REAL de produccion (paths.SHADOW_TRADES_LOG, tipicamente
logs/shadow_trades.csv).

BUG REAL CORREGIDO (auditoria del 2026-08-27, ver
docs/AUDITORIA_MAESTRA_2026-08-27.md seccion 3.3): antes de este modulo,
`OrderGateway()`/`GgalOptionsBot()` construidos sin argumentos dentro de
tests en modo shadow escribian su ShadowAuditLogger directamente sobre el
CSV real (unico default que existia). Confirmado en el archivo real del
usuario (contaminacion parcial) y en la copia de este sandbox (contaminacion
total: 94/94 filas rastreadas a fixtures de tests).

Uso: cada archivo de test que ejercite OrderGateway/GgalOptionsBot en modo
shadow debe importar este modulo ANTES de instanciar nada (basta el import,
no hace falta llamar ninguna funcion) - `ggal_bot.execution.order_gateway`
hace `from ggal_bot import paths` y lee `paths.SHADOW_TRADES_LOG` de forma
perezosa (recien al construir cada ShadowAuditLogger), asi que redirigir el
atributo en el modulo `ggal_bot.paths` ANTES de que corra cualquier test
alcanza para aislar TODAS las instancias de esa corrida, sin necesidad de
tocar cada `OrderGateway()`/`GgalOptionsBot()` individualmente.

Esto funciona tanto invocado via `python -m ggal_bot.validation.test_X`
(el import de este modulo corre como parte del import normal del archivo,
antes de que se ejecute ningun test) como via pytest (mismo mecanismo de
import). Un test que necesite inspeccionar el contenido exacto del CSV
(unico caso real: test_order_gateway_shadow_mode_logs_fill_to_audit_csv)
sigue usando su propio tempfile.TemporaryDirectory() dedicado via
`gateway._shadow_logger = ShadowAuditLogger(path=...)`, que tiene prioridad
sobre este default de modulo.
"""

from __future__ import annotations

import atexit
import tempfile
from pathlib import Path

from ggal_bot import paths as _paths

_ISOLATED_LOG_DIR = tempfile.TemporaryDirectory(prefix="ggal_bot_test_shadow_logs_")
_ISOLATED_LOG_PATH = Path(_ISOLATED_LOG_DIR.name) / "shadow_trades_test.csv"

# Redirige el default global ANTES de que cualquier test importe/instancie
# OrderGateway/GgalOptionsBot. Nunca apunta al CSV real de produccion.
_paths.SHADOW_TRADES_LOG = _ISOLATED_LOG_PATH

atexit.register(_ISOLATED_LOG_DIR.cleanup)
