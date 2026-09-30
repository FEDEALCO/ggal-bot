"""
test_state_writer.py
=======================
Tests para ggal_bot/state_writer.py (persistencia de state/bot_state.json).
Cubre especificamente los campos nuevos env_flags/deployed_git_sha
(MEJORA 2026-09-30, correccion de arquitectura del panel de config del
dashboard - ver REPORT.md).

Correr con:
    python -m ggal_bot.validation.test_state_writer
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot.state_writer import StateWriter


def _temp_path() -> Path:
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    path = Path(name)
    path.unlink()
    return path


def test_write_defaults_env_flags_empty_and_git_sha_unknown_when_not_passed():
    path = _temp_path()
    try:
        writer = StateWriter(path=path)
        writer.write(
            portfolio_greeks_total={}, portfolio_greeks_by_expiry={},
            active_signals=[], risk_breaches="",
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["env_flags"] == {}
        assert data["deployed_git_sha"] == "unknown"
    finally:
        path.unlink(missing_ok=True)
        path.with_suffix(".tmp").unlink(missing_ok=True)


def test_write_persists_env_flags_and_git_sha_when_passed():
    path = _temp_path()
    try:
        writer = StateWriter(path=path)
        writer.write(
            portfolio_greeks_total={}, portfolio_greeks_by_expiry={},
            active_signals=[], risk_breaches="",
            env_flags={"GGAL_BOT_ENABLE_SCALPING": "true", "GGAL_BOT_SOME_API_KEY": "***"},
            deployed_git_sha="abc1234",
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["env_flags"]["GGAL_BOT_ENABLE_SCALPING"] == "true"
        assert data["env_flags"]["GGAL_BOT_SOME_API_KEY"] == "***"
        assert data["deployed_git_sha"] == "abc1234"
    finally:
        path.unlink(missing_ok=True)
        path.with_suffix(".tmp").unlink(missing_ok=True)


ALL_TESTS = [
    test_write_defaults_env_flags_empty_and_git_sha_unknown_when_not_passed,
    test_write_persists_env_flags_and_git_sha_when_passed,
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
