"""
test_dashboard_data_bot_config.py
====================================
Tests para dashboard/data/bot_config.py (introspeccion de env vars
GGAL_BOT_* y loader de state/bot_state.json).

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


def test_list_ggal_bot_env_vars_only_includes_prefixed_vars():
    os.environ["GGAL_BOT_TEST_FLAG_X"] = "true"
    os.environ["SOME_OTHER_VAR"] = "should_not_appear"
    try:
        out = bc.list_ggal_bot_env_vars()
        assert out["GGAL_BOT_TEST_FLAG_X"] == "true"
        assert "SOME_OTHER_VAR" not in out
    finally:
        del os.environ["GGAL_BOT_TEST_FLAG_X"]
        del os.environ["SOME_OTHER_VAR"]


def test_list_ggal_bot_env_vars_masks_sensitive_names():
    os.environ["GGAL_BOT_SOME_API_KEY"] = "s3cr3t"
    try:
        out = bc.list_ggal_bot_env_vars()
        assert out["GGAL_BOT_SOME_API_KEY"] == "***"
    finally:
        del os.environ["GGAL_BOT_SOME_API_KEY"]


def test_load_bot_state_none_when_file_missing():
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    path = Path(name)
    path.unlink()
    assert bc.load_bot_state(path) is None


def test_load_bot_state_none_when_invalid_json():
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    path = Path(name)
    path.write_text("{esto no es json valido", encoding="utf-8")
    try:
        assert bc.load_bot_state(path) is None
    finally:
        path.unlink(missing_ok=True)


def test_load_bot_state_returns_dict_for_valid_json():
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    path = Path(name)
    path.write_text(json.dumps({"timestamp": "2026-09-30T12:00:00+00:00", "active_signals": []}), encoding="utf-8")
    try:
        data = bc.load_bot_state(path)
        assert data is not None
        assert data["timestamp"] == "2026-09-30T12:00:00+00:00"
    finally:
        path.unlink(missing_ok=True)


ALL_TESTS = [
    test_list_ggal_bot_env_vars_only_includes_prefixed_vars,
    test_list_ggal_bot_env_vars_masks_sensitive_names,
    test_load_bot_state_none_when_file_missing,
    test_load_bot_state_none_when_invalid_json,
    test_load_bot_state_returns_dict_for_valid_json,
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
