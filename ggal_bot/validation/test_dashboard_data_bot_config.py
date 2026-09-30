"""
test_dashboard_data_bot_config.py
====================================
Tests para dashboard/data/bot_config.py (loader de state/bot_state.json y
extraccion de env_flags/deployed_git_sha PUBLICADOS POR EL BOT en ese
mismo snapshot).

CORREGIDO 2026-09-30 (ver REPORT.md): este archivo ya NO testea lectura
directa de os.environ del proceso dashboard (esa logica se movio a
ggal_bot/env_introspection.py, que es quien la llama es el BOT via
StateWriter - ver test_env_introspection.py) - el dashboard solo lee lo
que el bot publico en el JSON.

Correr con:
    python -m ggal_bot.validation.test_dashboard_data_bot_config
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dashboard.data import bot_config as bc


def _temp_json_path() -> Path:
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    path = Path(name)
    path.unlink()
    return path


def test_load_bot_state_none_when_file_missing():
    assert bc.load_bot_state(_temp_json_path()) is None


def test_load_bot_state_none_when_invalid_json():
    path = _temp_json_path()
    path.write_text("{esto no es json valido", encoding="utf-8")
    try:
        assert bc.load_bot_state(path) is None
    finally:
        path.unlink(missing_ok=True)


def test_load_bot_state_returns_dict_for_valid_json():
    path = _temp_json_path()
    path.write_text(json.dumps({"timestamp": "2026-09-30T12:00:00+00:00", "active_signals": []}), encoding="utf-8")
    try:
        data = bc.load_bot_state(path)
        assert data is not None
        assert data["timestamp"] == "2026-09-30T12:00:00+00:00"
    finally:
        path.unlink(missing_ok=True)


def test_get_env_flags_from_state_empty_when_state_is_none():
    assert bc.get_env_flags_from_state(None) == {}


def test_get_env_flags_from_state_empty_when_key_missing():
    assert bc.get_env_flags_from_state({"timestamp": "x"}) == {}


def test_get_env_flags_from_state_returns_flags_published_by_bot():
    state = {"env_flags": {"GGAL_BOT_ENABLE_SCALPING": "true", "GGAL_BOT_SOME_API_KEY": "***"}}
    flags = bc.get_env_flags_from_state(state)
    assert flags["GGAL_BOT_ENABLE_SCALPING"] == "true"
    assert flags["GGAL_BOT_SOME_API_KEY"] == "***"  # el bot ya lo enmascaro, el dashboard no re-enmascara ni desenmascara


def test_get_deployed_git_sha_from_state_none_when_state_is_none():
    assert bc.get_deployed_git_sha_from_state(None) is None


def test_get_deployed_git_sha_from_state_none_when_bot_published_unknown():
    assert bc.get_deployed_git_sha_from_state({"deployed_git_sha": "unknown"}) is None


def test_get_deployed_git_sha_from_state_returns_real_sha():
    assert bc.get_deployed_git_sha_from_state({"deployed_git_sha": "abc1234"}) == "abc1234"


ALL_TESTS = [
    test_load_bot_state_none_when_file_missing,
    test_load_bot_state_none_when_invalid_json,
    test_load_bot_state_returns_dict_for_valid_json,
    test_get_env_flags_from_state_empty_when_state_is_none,
    test_get_env_flags_from_state_empty_when_key_missing,
    test_get_env_flags_from_state_returns_flags_published_by_bot,
    test_get_deployed_git_sha_from_state_none_when_state_is_none,
    test_get_deployed_git_sha_from_state_none_when_bot_published_unknown,
    test_get_deployed_git_sha_from_state_returns_real_sha,
]


if __name__ == "__main__":
    failures = 0
    for test_fn in ALL_TESTS:
        try:
            test_fn()
            print(f"OK   - {test_fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL - {test_fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR - {test_fn.__name__}: {exc!r}")

    print(f"\n{len(ALL_TESTS) - failures}/{len(ALL_TESTS)} tests OK")
    if failures:
        raise SystemExit(1)
