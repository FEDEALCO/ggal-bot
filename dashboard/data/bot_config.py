"""
dashboard/data/bot_config.py
==============================
Introspeccion pura y testeada de: (1) el snapshot en vivo
state/bot_state.json (ver ggal_bot/state_writer.py), y (2) la config
efectiva del BOT (env vars GGAL_BOT_* enmascaradas y SHA de git
desplegado) que el bot mismo publica DENTRO de ese snapshot.

CORREGIDO 2026-09-30 (a pedido explicito del usuario - ver REPORT.md):
la version anterior de este modulo asumia que el dashboard corre como
proceso HERMANO del bot en el mismo contenedor (ver entrypoint.sh) y leia
os.environ de ESTE proceso directamente. Esa topologia es un DISEÑO
documentado en Dockerfile/entrypoint.sh, pero nunca fue verificada contra
la config REAL de Northflank (no hay ningun archivo de config de
Northflank versionado en este repo) - el usuario pidio explicitamente no
asumirlo. Ahora el dashboard NUNCA lee su propio os.environ para esto:
lee unicamente lo que el bot publico en bot_state.json (via
StateWriter.write(env_flags=..., deployed_git_sha=...), alimentado por
ggal_bot.env_introspection/ggal_bot.version_info) - funciona identico
corran ambos procesos en el mismo contenedor o en servicios separados,
siempre que compartan el volumen de state/ (si ni siquiera eso se
comparte, load_bot_state() ya devuelve None y el panel muestra SIN DATOS,
nunca datos fabricados).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from ggal_bot import paths


def load_bot_state(json_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """
    Lee state/bot_state.json (ver ggal_bot/state_writer.py::StateWriter.write) -
    snapshot de UN SOLO punto en el tiempo (no historico): griegas de
    cartera, señales activas, risk_breaches, option_chain_snapshot,
    env_flags, deployed_git_sha.

    Devuelve None si el archivo no existe, esta vacio o no es JSON valido -
    NUNCA fabrica un snapshot ni devuelve un dict con valores por defecto.
    El llamador debe mostrar "SIN DATOS" en ese caso.
    """
    path = Path(json_path) if json_path is not None else paths.STATE_FILE
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def get_env_flags_from_state(state: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """
    Extrae las env vars GGAL_BOT_* efectivas QUE EL BOT PUBLICO en
    bot_state.json (ya enmascaradas si el nombre sugiere una credencial -
    ver ggal_bot.env_introspection.list_ggal_bot_env_vars, que es quien
    las calculo del lado del bot). Devuelve {} (nunca fabrica flags) si el
    state es None o no trae esa clave - el llamador debe distinguir "{}"
    de "SIN DATOS" mostrando SIN DATOS cuando `state` mismo es None.
    """
    if not state:
        return {}
    flags = state.get("env_flags")
    if not isinstance(flags, dict):
        return {}
    return {str(k): str(v) for k, v in flags.items()}


def get_shadow_mode_from_state(state: Optional[Dict[str, Any]]) -> Optional[bool]:
    """
    Valor RESUELTO de SETTINGS.shadow.enabled publicado por el bot en este
    snapshot (ver run_bot.py, ambos llamados a state_writer.write() -
    Fase 1 dashboard, Prioridad 3: distincion visual shadow/vivo). None
    (nunca se fabrica True/False) si el state es None, no trae la clave
    (bot_state.json de un deploy anterior a esta mejora), o el valor no es
    un bool - el llamador debe mostrar "SIN DATOS: modo desconocido" en
    esos casos, jamas asumir un modo por default.
    """
    if not state:
        return None
    value = state.get("shadow_mode_enabled")
    if not isinstance(value, bool):
        return None
    return value


def get_deployed_git_sha_from_state(state: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    SHA de git publicado por el bot en este snapshot (ver
    ggal_bot.version_info.get_deployed_git_sha, horneado en la imagen via
    el build-arg GIT_SHA del Dockerfile). None si el state es None, no
    trae la clave, o el bot publico su propio "unknown" (build sin
    build-arg) - en los tres casos el llamador debe mostrar SIN DATOS/
    "desconocido", nunca fabricar un SHA.
    """
    if not state:
        return None
    sha = state.get("deployed_git_sha")
    if not sha or not isinstance(sha, str) or sha == "unknown":
        return None
    return sha
