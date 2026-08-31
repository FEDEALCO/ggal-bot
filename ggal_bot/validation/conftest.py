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

from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401
