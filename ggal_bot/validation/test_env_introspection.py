"""
test_env_introspection.py
============================
Tests para ggal_bot/env_introspection.py (introspeccion de env vars
GGAL_BOT_* que el BOT publica en bot_state.json via StateWriter - ver
REPORT.md, correccion de arquitectura 2026-09-30: el dashboard ya NO lee
esto directamente, ver dashboard/data/bot_config.py).

Correr con:
    python -m ggal_bot.validation.test_env_introspection
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot.env_introspection import list_ggal_bot_env_vars


def test_list_ggal_bot_env_vars_only_includes_prefixed_vars():
    os.environ["GGAL_BOT_TEST_FLAG_X"] = "true"
    os.environ["SOME_OTHER_VAR"] = "should_not_appear"
    try:
        out = list_ggal_bot_env_vars()
        assert out["GGAL_BOT_TEST_FLAG_X"] == "true"
        assert "SOME_OTHER_VAR" not in out
    finally:
        del os.environ["GGAL_BOT_TEST_FLAG_X"]
        del os.environ["SOME_OTHER_VAR"]


def test_list_ggal_bot_env_vars_masks_sensitive_names():
    os.environ["GGAL_BOT_SOME_API_KEY"] = "s3cr3t"
    try:
        out = list_ggal_bot_env_vars()
        assert out["GGAL_BOT_SOME_API_KEY"] == "***"
    finally:
        del os.environ["GGAL_BOT_SOME_API_KEY"]


def test_list_ggal_bot_env_vars_sorted_alphabetically():
    os.environ["GGAL_BOT_ZZZ_FLAG"] = "1"
    os.environ["GGAL_BOT_AAA_FLAG"] = "2"
    try:
        out = list_ggal_bot_env_vars()
        keys = [k for k in out if k in ("GGAL_BOT_ZZZ_FLAG", "GGAL_BOT_AAA_FLAG")]
        assert keys.index("GGAL_BOT_AAA_FLAG") < keys.index("GGAL_BOT_ZZZ_FLAG")
    finally:
        del os.environ["GGAL_BOT_ZZZ_FLAG"]
        del os.environ["GGAL_BOT_AAA_FLAG"]


ALL_TESTS = [
    test_list_ggal_bot_env_vars_only_includes_prefixed_vars,
    test_list_ggal_bot_env_vars_masks_sensitive_names,
    test_list_ggal_bot_env_vars_sorted_alphabetically,
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
