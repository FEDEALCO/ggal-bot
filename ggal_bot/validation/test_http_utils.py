"""
test_http_utils.py
=====================
Tests de sanity para ggal_bot/data/http_utils.py: el wrapper de timeout de
PARED REAL usado por Data912RestSource (live_shadow_feed.py) y
Data912DailyBarsSource (technical_analysis.py) para pegarle a data912.com.

Motivo (ver docstring de http_utils.py y README): un caso real reportado
por el usuario mostro que un timeout de `requests` configurado en 5.0s
podia, en la practica, tardar MINUTOS en levantarse en Windows (resolucion
DNS colgada a nivel de sistema operativo, no cubierta por el timeout de
`requests`) - bloqueando el ciclo entero de run_bot.py mientras tanto. Este
archivo verifica que `http_get_json()` efectivamente corta dentro de
timeout + margen, sin esperar a que la llamada subyacente se resuelva por
su cuenta, y que el caso normal (rapido, con o sin error) no se ve afectado.

Correr con:
    python -m ggal_bot.validation.test_http_utils
"""

from __future__ import annotations

import os
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot.data import http_utils


def test_http_get_json_returns_parsed_json_on_success():
    original_get = http_utils.requests.get

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "n": 42}

    def _fake_get(url, timeout):  # noqa: ARG001
        return _FakeResponse()

    http_utils.requests.get = _fake_get
    try:
        result = http_utils.http_get_json("https://example.invalid/endpoint", timeout=1.0)
        assert result == {"ok": True, "n": 42}
    finally:
        http_utils.requests.get = original_get


def test_http_get_json_propagates_requests_exception_quickly():
    """
    Caso normal (rapido): `requests.get` levanta su propia excepcion (ej.
    problema de red inmediato) dentro del timeout nominal - eso NO debe
    convertirse en el TimeoutError propio de este modulo, que es
    especificamente para el caso en que la llamada subyacente NO responde
    a tiempo (ni con exito ni con error).
    """
    original_get = http_utils.requests.get

    def _fake_get(url, timeout):  # noqa: ARG001
        raise ConnectionError("simulado: fallo de red inmediato")

    http_utils.requests.get = _fake_get
    try:
        raised_type = None
        try:
            http_utils.http_get_json("https://example.invalid/endpoint", timeout=1.0)
        except Exception as exc:  # noqa: BLE001
            raised_type = type(exc)
        assert raised_type is ConnectionError
    finally:
        http_utils.requests.get = original_get


def test_http_get_json_raises_timeout_error_when_underlying_call_hangs_past_hard_timeout():
    """
    Reproduce el caso real reportado: la llamada subyacente NO respeta su
    propio timeout (queda colgada mas alla de el, ej. por una resolucion
    DNS bloqueada a nivel de SO) - http_get_json debe cortar de todos modos
    dentro de timeout + margen, sin esperar a que la llamada bloqueada
    termine por su cuenta. El margen se reduce temporalmente para que el
    test corra rapido sin dejar de ejercitar el mecanismo real.
    """
    original_get = http_utils.requests.get
    original_margin = http_utils._HARD_TIMEOUT_MARGIN_SECONDS

    def _hanging_get(url, timeout):  # noqa: ARG001
        time.sleep(2.0)  # simula un cuelgue mucho mas largo que el timeout nominal
        raise AssertionError("no deberia llegar a ejecutarse dentro de la ventana del test")

    http_utils.requests.get = _hanging_get
    http_utils._HARD_TIMEOUT_MARGIN_SECONDS = 0.1  # margen chico: test rapido, mecanismo real igual
    try:
        started = time.monotonic()
        raised_type = None
        try:
            http_utils.http_get_json("https://example.invalid/endpoint", timeout=0.05)
        except Exception as exc:  # noqa: BLE001
            raised_type = type(exc)
        elapsed = time.monotonic() - started
        assert raised_type is TimeoutError
        assert elapsed < 1.0  # corta MUCHO antes de que el sleep(2.0) termine por su cuenta
    finally:
        http_utils.requests.get = original_get
        http_utils._HARD_TIMEOUT_MARGIN_SECONDS = original_margin


def test_http_get_json_raises_runtime_error_when_requests_not_available():
    original_flag = http_utils._REQUESTS_AVAILABLE
    http_utils._REQUESTS_AVAILABLE = False
    try:
        raised_type = None
        try:
            http_utils.http_get_json("https://example.invalid/endpoint", timeout=1.0)
        except Exception as exc:  # noqa: BLE001
            raised_type = type(exc)
        assert raised_type is RuntimeError
    finally:
        http_utils._REQUESTS_AVAILABLE = original_flag


ALL_TESTS = [
    test_http_get_json_returns_parsed_json_on_success,
    test_http_get_json_propagates_requests_exception_quickly,
    test_http_get_json_raises_timeout_error_when_underlying_call_hangs_past_hard_timeout,
    test_http_get_json_raises_runtime_error_when_requests_not_available,
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
