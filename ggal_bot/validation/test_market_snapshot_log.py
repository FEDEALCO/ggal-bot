"""
test_market_snapshot_log.py
==============================
Tests para data/market_snapshot_log.py::MarketSnapshotLogger (MEJORA
2026-09-28: persistencia de la cadena de opciones completa por ciclo, base
para backtesting offline futuro - ver docstring de ese modulo).

Correr con:
    python -m ggal_bot.validation.test_market_snapshot_log
"""
from __future__ import annotations

import csv
import gzip
import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import date, datetime, timedelta, timezone

from ggal_bot.data.market_snapshot_log import MarketSnapshotLogger
from ggal_bot.data.option_chain import OptionQuote, OrderBookSnapshot
from ggal_bot.models.black_scholes import OptionType


def _quote(symbol, strike=5150.0, option_type=OptionType.CALL, iv=0.45, spot_ref=5200.0,
           greeks=None, expiry=date(2026, 10, 2), days_calendar=10, days_business=7):
    book = OrderBookSnapshot(symbol, bid=95.0, ask=105.0, bid_size=100.0, ask_size=100.0, last_volume=1000.0)
    q = OptionQuote(symbol, strike=strike, expiry=expiry, option_type=option_type,
                     book=book, days_calendar=days_calendar, days_business=days_business)
    q.iv = iv
    q.spot_ref = spot_ref
    q.greeks = greeks
    return q


def _temp_logger_path() -> Path:
    fd, name = tempfile.mkstemp(prefix="market_snapshot_test_", suffix=".csv")
    os.close(fd)
    path = Path(name)
    path.unlink()  # el logger debe poder crearlo desde cero (no existe todavia)
    return path


def test_logger_writes_header_on_first_use():
    path = _temp_logger_path()
    try:
        MarketSnapshotLogger(path=path)
        assert path.exists()
        with open(path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))
        assert header == MarketSnapshotLogger._HEADER
    finally:
        path.unlink(missing_ok=True)


def test_logger_does_not_duplicate_header_on_reuse():
    path = _temp_logger_path()
    try:
        MarketSnapshotLogger(path=path)
        MarketSnapshotLogger(path=path)  # segunda instancia sobre el mismo archivo (ej. reinicio del bot)
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows.count(MarketSnapshotLogger._HEADER) == 1
    finally:
        path.unlink(missing_ok=True)


def test_logger_appends_one_row_per_quote():
    path = _temp_logger_path()
    try:
        logger = MarketSnapshotLogger(path=path)
        now = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
        quotes = [
            _quote("GFGC5150O", greeks={"delta": 0.55, "gamma": 0.002, "vega": 3.1, "theta": -0.8}),
            _quote("GFGV5150O", option_type=OptionType.PUT, greeks=None),
        ]
        logger.log_quotes(quotes, now=now)
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 3  # header + 2 filas
        assert rows[1][1] == "GFGC5150O"
        assert rows[2][1] == "GFGV5150O"
    finally:
        path.unlink(missing_ok=True)


def test_logger_leaves_iv_and_greeks_blank_when_missing_never_fabricated():
    path = _temp_logger_path()
    try:
        logger = MarketSnapshotLogger(path=path)
        q = _quote("GFGC5150O", iv=None, greeks=None)
        logger.log_quotes([q])
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        header = rows[0]
        row = dict(zip(header, rows[1]))
        assert row["iv"] == ""
        assert row["delta"] == "" and row["gamma"] == "" and row["vega"] == "" and row["theta"] == ""
    finally:
        path.unlink(missing_ok=True)


def test_logger_appends_across_multiple_calls():
    path = _temp_logger_path()
    try:
        logger = MarketSnapshotLogger(path=path)
        logger.log_quotes([_quote("GFGC5150O")])
        logger.log_quotes([_quote("GFGC5150O")])
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 3  # header + 2 filas (append-only, nunca sobreescribe)
    finally:
        path.unlink(missing_ok=True)


def test_logger_does_nothing_with_empty_quotes_iterable():
    path = _temp_logger_path()
    try:
        logger = MarketSnapshotLogger(path=path)
        logger.log_quotes([])
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 1  # solo el header, ninguna fila fabricada
    finally:
        path.unlink(missing_ok=True)


def test_logger_write_failure_does_not_raise():
    """Igual que ShadowAuditLogger/PositionEventJournal: un fallo de disco al auditar
    NUNCA debe tumbar una decision de trading real ya tomada."""
    path = _temp_logger_path()
    try:
        logger = MarketSnapshotLogger(path=path)
        # Se fuerza un path invalido tras la creacion, para simular una falla de escritura
        # (ej. disco lleno / permiso revocado a mitad de sesion) sin tocar el sistema real.
        logger._path = Path("/root/no_existe/no_se_puede_crear/market_snapshots.csv")
        logger.log_quotes([_quote("GFGC5150O")])  # no debe lanzar excepcion
    finally:
        path.unlink(missing_ok=True)


# --- Rotacion diaria + compresion (MEJORA 2026-10-08, hallazgo de auditoria) ---

def _set_mtime_to_yesterday(path: Path, today: date) -> None:
    yesterday_noon = datetime(today.year, today.month, today.day, 12, 0, tzinfo=timezone.utc) - timedelta(days=1)
    ts = yesterday_noon.timestamp()  # datetime aware (UTC) -> epoch, correcto sin importar la TZ del sistema
    os.utime(path, (ts, ts))


def test_logger_archives_and_compresses_file_left_from_a_previous_day_on_startup():
    """
    Simula un restart del bot cruzando la medianoche UTC: el archivo
    "vivo" quedo con una fila de AYER (mtime de ayer). Al construir una
    nueva instancia (equivalente a un restart), debe archivarse comprimido
    con gzip (contenido intacto) y dejar el path vivo libre de datos viejos
    para un archivo nuevo.

    BUG DE TEST CORREGIDO (2026-10-09, hallazgo de auditoria): la version
    original hardcodeaba `today = date(2026, 10, 9)` - un dia en el FUTURO
    relativo a cuando se escribio el test (2026-10-08). Eso rompia en dos
    frentes apenas el reloj real alcanzaba esa fecha: (1) el constructor de
    `new_logger` dispara su propio chequeo de rotacion automatico
    (`_ensure_header` -> `_rotate_if_new_day_locked(now=None)`, que usa el
    reloj real, no el `today` hardcodeado) - mientras el reloj real no
    llegaba a esa fecha, ese chequeo automatico era un no-op y SOLO la
    llamada explicita de mas abajo rotaba (sin volver a crear el header);
    en cuanto el reloj real alcanzo esa fecha, el chequeo automatico paso a
    disparar el solo, y como SI pasa por `_ensure_header`, deja el archivo
    vivo con el header recien creado (no vacio/inexistente como asumia el
    assert original). (2) El nombre del archivo comprimido tambien estaba
    hardcodeado a `-2026-10-08`. Fix: usar el `today` REAL (reloj de la
    maquina en el momento de correr el test) en vez de una fecha fija, asi
    el test es determinista para siempre (sin fecha de vencimiento), y
    verificar el invariante real - que no sobreviva ninguna fila de datos
    de ayer en el path vivo - en vez de asumir "vacio o inexistente"
    (puede quedar con el header nuevo, que es igualmente valido).
    """
    path = _temp_logger_path()
    archive_path = None
    try:
        old_logger = MarketSnapshotLogger(path=path)
        old_logger.log_quotes([_quote("GFGC5150O")])
        with open(path, "rb") as f:
            original_content = f.read()
        today = datetime.now(timezone.utc).date()
        _set_mtime_to_yesterday(path, today)

        new_logger = MarketSnapshotLogger(path=path, rotate_daily=True)
        new_logger._rotate_if_new_day_locked(now=datetime(today.year, today.month, today.day, 1, 0, tzinfo=timezone.utc))

        yesterday = today - timedelta(days=1)
        archive_path = path.with_name(f"{path.stem}-{yesterday.isoformat()}{path.suffix}.gz")
        assert archive_path.exists(), "El archivo de ayer deberia haberse archivado comprimido."
        if path.exists():
            with open(path, "r", newline="", encoding="utf-8") as f:
                remaining_rows = list(csv.reader(f))
            data_rows = (
                remaining_rows[1:] if remaining_rows and remaining_rows[0] == MarketSnapshotLogger._HEADER
                else remaining_rows
            )
            assert not data_rows, (
                f"El path vivo no deberia conservar filas de ayer tras rotar (quedaron: {data_rows})."
            )
        with gzip.open(archive_path, "rb") as f:
            archived_content = f.read()
        assert archived_content == original_content, "El contenido archivado debe ser identico al original."
    finally:
        path.unlink(missing_ok=True)
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)


def test_logger_rotates_mid_session_when_a_write_crosses_midnight():
    """
    A diferencia del test de arriba (chequeo en __init__, equivalente a un
    restart), esto cubre un proceso de LARGA VIDA que nunca se reinicia:
    la rotacion tambien debe dispararse dentro de log_quotes()/_write_rows,
    no solo al construir la instancia.
    """
    path = _temp_logger_path()
    archive_path = None
    try:
        logger = MarketSnapshotLogger(path=path)
        logger.log_quotes([_quote("GFGC5150O")], now=datetime(2026, 10, 8, 23, 59, tzinfo=timezone.utc))
        _set_mtime_to_yesterday(path, date(2026, 10, 9))  # fuerza el mtime a "ayer" respecto del proximo write

        logger.log_quotes([_quote("GFGV5150O")], now=datetime(2026, 10, 9, 0, 5, tzinfo=timezone.utc))

        archive_path = path.with_name(f"{path.stem}-2026-10-08{path.suffix}.gz")
        assert archive_path.exists(), "Deberia haber rotado el archivo de ayer antes de escribir la fila de hoy."
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 2  # header nuevo + la fila de GFGV5150O (la de ayer quedo en el archivo .gz)
        assert rows[1][1] == "GFGV5150O"
    finally:
        path.unlink(missing_ok=True)
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)


def test_logger_rotation_disabled_via_flag_keeps_accumulating_in_the_same_file():
    """Con rotate_daily=False (equivalente a GGAL_BOT_MARKET_SNAPSHOT_ROTATE_DAILY=false),
    un archivo de ayer debe seguir acumulando filas nuevas sin rotar, igual que antes de esta mejora."""
    path = _temp_logger_path()
    try:
        logger = MarketSnapshotLogger(path=path, rotate_daily=False)
        logger.log_quotes([_quote("GFGC5150O")])
        _set_mtime_to_yesterday(path, date(2026, 10, 9))

        logger.log_quotes([_quote("GFGV5150O")], now=datetime(2026, 10, 9, 0, 5, tzinfo=timezone.utc))

        archive_path = path.with_name(f"{path.stem}-2026-10-08{path.suffix}.gz")
        assert not archive_path.exists(), "Con rotate_daily=False no deberia archivarse nunca."
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 3  # header + las dos filas, todo en el mismo archivo
    finally:
        path.unlink(missing_ok=True)


def test_logger_never_overwrites_an_archive_that_already_exists():
    """Si el archivo de destino ya existe (ej. dos rotaciones en el mismo arranque,
    o uno manual previo), nunca se sobreescribe - se deja el archivo vivo sin rotar esta vez,
    para no perder ni el archivo viejo ni el que se iba a archivar."""
    path = _temp_logger_path()
    archive_path = None
    try:
        logger = MarketSnapshotLogger(path=path)
        logger.log_quotes([_quote("GFGC5150O")])
        with open(path, "rb") as f:
            live_content_before = f.read()
        _set_mtime_to_yesterday(path, date(2026, 10, 9))

        archive_path = path.with_name(f"{path.stem}-2026-10-08{path.suffix}.gz")
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(archive_path, "wb") as f:
            f.write(b"contenido preexistente, no debe perderse")

        logger._rotate_if_new_day_locked(now=datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc))

        with gzip.open(archive_path, "rb") as f:
            assert f.read() == b"contenido preexistente, no debe perderse", (
                "Nunca debe sobreescribirse un archivo de destino ya existente."
            )
        with open(path, "rb") as f:
            assert f.read() == live_content_before, "El archivo vivo debe quedar intacto si no se pudo rotar."
    finally:
        path.unlink(missing_ok=True)
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)


ALL_TESTS = [
    test_logger_writes_header_on_first_use,
    test_logger_does_not_duplicate_header_on_reuse,
    test_logger_appends_one_row_per_quote,
    test_logger_leaves_iv_and_greeks_blank_when_missing_never_fabricated,
    test_logger_appends_across_multiple_calls,
    test_logger_does_nothing_with_empty_quotes_iterable,
    test_logger_write_failure_does_not_raise,
    test_logger_archives_and_compresses_file_left_from_a_previous_day_on_startup,
    test_logger_rotates_mid_session_when_a_write_crosses_midnight,
    test_logger_rotation_disabled_via_flag_keeps_accumulating_in_the_same_file,
    test_logger_never_overwrites_an_archive_that_already_exists,
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
