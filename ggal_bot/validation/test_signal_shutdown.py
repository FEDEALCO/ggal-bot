"""
test_signal_shutdown.py
========================
Test de regresion para un bug real encontrado en produccion (log de
Northflank, container shutdown ~2026-09-08 20:09:49 UTC):

    RuntimeError: reentrant call inside <_io.BufferedWriter
    name='/app/logs/ggal_bot.log'>

ROOT CAUSE: GgalOptionsBot._install_signal_handlers() registraba un handler
de SIGINT/SIGTERM que llamaba a `logger.info(...)` DIRECTAMENTE desde el
handler. Un signal handler en Python se ejecuta en el hilo principal,
insertado en el punto exacto de bytecode en que la señal llego - incluso si
ese punto esta en medio de un logger.info(...) ya en curso escribiendo al
mismo stream (`ggal_bot.log`). Cuando el handler tambien llama a
logger.info(...), termina reentrando el mismo `_io.BufferedWriter` todavia
bloqueado por la escritura interrumpida, lo cual CPython detecta y convierte
en este RuntimeError (en vez de dejar que deadlockee). Python's logging
module lo atrapa internamente (imprime "--- Logging error ---" a stderr) asi
que no era fatal - el shutdown terminaba igual ("Shutdown completo." se
logueaba) - pero es un bug real: cualquier I/O (logging incluido) dentro de
un signal handler es inseguro por esta misma razon.

FIX (no rompe nada, es estrictamente aditivo en comportamiento observable):
el handler ya NO loguea nada - solo guarda `self._shutdown_signal = signum`
y levanta `self._shutting_down = True`. El mensaje "Señal de apagado
recibida..." se sigue emitiendo, pero desde `run_forever()` (vía el nuevo
metodo `_log_shutdown_signal_if_any()`), en el flujo secuencial normal del
hilo principal, fuera de cualquier signal handler - momento en el que
loguear es seguro porque no se esta interrumpiendo ninguna escritura en
curso.

Estos tests NO pueden invocar `run_forever()` completo (loop infinito con
conexion real de mercado), asi que verifican por separado las dos mitades
del fix:
  1. El handler registrado por _install_signal_handlers(), invocado
     directamente (como el SO lo invocaria), no llama a logger.info() y deja
     el estado (_shutdown_signal/_shutting_down) correcto.
  2. _log_shutdown_signal_if_any() (el metodo que run_forever() llama desde
     su `finally`, fuera del signal handler) loguea el mensaje correcto
     cuando corresponde, y no loguea nada si nunca hubo señal.

IMPORTANTE sobre aislamiento: estos tests instalan signal handlers de
SIGINT/SIGTERM a nivel de PROCESO (igual que el codigo real). Restauran los
handlers previos en un `finally` para no interferir con pytest ni con el
resto de la suite (ej. Ctrl+C durante una corrida interactiva).
"""
from __future__ import annotations

import signal

import pytest

from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS
import run_bot
from run_bot import GgalOptionsBot


def _make_bot() -> GgalOptionsBot:
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        return GgalOptionsBot()
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_signal_handler_does_not_log_directly_and_records_signal(monkeypatch):
    bot = _make_bot()

    original_sigint = signal.getsignal(signal.SIGINT)
    original_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        assert bot._shutdown_signal is None
        assert bot._shutting_down is False

        bot._install_signal_handlers()
        handler = signal.getsignal(signal.SIGTERM)
        assert handler is not None

        logged_calls = []
        monkeypatch.setattr(run_bot.logger, "info", lambda *a, **k: logged_calls.append((a, k)))
        monkeypatch.setattr(run_bot.logger, "exception", lambda *a, **k: logged_calls.append((a, k)))

        # Invocar el handler tal cual lo invocaria el SO al recibir la señal.
        handler(signal.SIGTERM, None)

        assert logged_calls == [], (
            "el signal handler NO debe hacer ningun logging directo - "
            "es exactamente la causa del RuntimeError de reentrant call "
            "observado en produccion"
        )
        assert bot._shutdown_signal == signal.SIGTERM
        assert bot._shutting_down is True
    finally:
        signal.signal(signal.SIGINT, original_sigint)
        signal.signal(signal.SIGTERM, original_sigterm)


def test_signal_handler_records_sigint_distinctly(monkeypatch):
    bot = _make_bot()

    original_sigint = signal.getsignal(signal.SIGINT)
    original_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        bot._install_signal_handlers()
        handler = signal.getsignal(signal.SIGINT)

        monkeypatch.setattr(run_bot.logger, "info", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("no deberia loguearse nada desde el handler")
        ))

        handler(signal.SIGINT, None)

        assert bot._shutdown_signal == signal.SIGINT
        assert bot._shutting_down is True
    finally:
        signal.signal(signal.SIGINT, original_sigint)
        signal.signal(signal.SIGTERM, original_sigterm)


def test_log_shutdown_signal_if_any_logs_once_signal_was_recorded(monkeypatch):
    bot = _make_bot()

    logged_calls = []
    monkeypatch.setattr(run_bot.logger, "info", lambda *a, **k: logged_calls.append((a, k)))

    assert bot._shutdown_signal is None
    bot._log_shutdown_signal_if_any()
    assert logged_calls == [], "sin señal recibida, no debe loguear nada"

    bot._shutdown_signal = signal.SIGTERM
    bot._log_shutdown_signal_if_any()
    assert len(logged_calls) == 1
    args, _ = logged_calls[0]
    assert "Señal de apagado recibida" in args[0]
    assert args[1] == signal.SIGTERM


# Nota: a diferencia de otros archivos de test de esta suite, este no expone
# un runner `if __name__ == "__main__"` propio porque estos tests dependen
# del fixture `monkeypatch` de pytest (no trivial de replicar a mano) - el
# mismo patron ya existente en test_fase53_reconciliation_and_journal.py.
# Correr con: pytest ggal_bot/validation/test_signal_shutdown.py
