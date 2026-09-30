"""
test_version_info.py
=======================
Tests para ggal_bot/version_info.py (SHA de git desplegado - MEJORA
2026-09-30, ver Dockerfile: ARG GIT_SHA + ENV GGAL_BOT_GIT_SHA).

Correr con:
    python -m ggal_bot.validation.test_version_info
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot.version_info import get_deployed_git_sha


def test_get_deployed_git_sha_returns_unknown_when_env_var_absent():
    os.environ.pop("GGAL_BOT_GIT_SHA", None)
    assert get_deployed_git_sha() == "unknown"


def test_get_deployed_git_sha_returns_env_var_value_when_present():
    os.environ["GGAL_BOT_GIT_SHA"] = "abc1234"
    try:
        assert get_deployed_git_sha() == "abc1234"
    finally:
        del os.environ["GGAL_BOT_GIT_SHA"]


def test_get_deployed_git_sha_treats_blank_value_as_unknown():
    os.environ["GGAL_BOT_GIT_SHA"] = "   "
    try:
        assert get_deployed_git_sha() == "unknown"
    finally:
        del os.environ["GGAL_BOT_GIT_SHA"]


ALL_TESTS = [
    test_get_deployed_git_sha_returns_unknown_when_env_var_absent,
    test_get_deployed_git_sha_returns_env_var_value_when_present,
    test_get_deployed_git_sha_treats_blank_value_as_unknown,
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
