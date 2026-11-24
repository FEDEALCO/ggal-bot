"""
log_rotation.py
=================
Rotacion diaria + compresion gzip, y retencion configurable (borrado de
archivos .gz viejos), reutilizable por cualquier logger CSV append-only
del proyecto.

MEJORA 2026-11-24 (a pedido explicito del usuario, ultimos ajustes de
seguridad antes de la pausa de desarrollo: "Rotacion diaria con gzip para
ccl_bond_quotes.csv, portfolio_greeks_log.csv y ggal_bot.log, con
retencion configurable (default 180 dias)"). Generaliza el patron que ya
existia (sin retencion) en MarketSnapshotLogger._rotate_if_new_day_locked
(ver ese modulo, MEJORA 2026-10-08) para que no se duplique entre los 3
loggers CSV del proyecto (MarketSnapshotLogger ya tenia rotacion diaria;
CclBondQuoteLogger/PortfolioGreeksLogger no tenian NINGUNA - verificado por
grep, cero coincidencias de "rotat|gzip" en ninguno de los dos) y ademas
agrega algo que NINGUN logger de este proyecto tenia todavia: borrado de
los .gz rotados mas viejos que la retencion configurada (sin esto, el
rotado de MarketSnapshotLogger desde el 2026-10-08 acumula archivos para
siempre - confirmado leyendo ese modulo, no existe ninguna logica de
borrado ahi).

Convencion de nombre de archivo rotado: `{stem}-{YYYY-MM-DD}{suffix}.gz`
(identica a la que ya usaba MarketSnapshotLogger) - la fecha es la del
archivo VIEJO que se esta archivando, nunca la de hoy.

UBICACION DELIBERADA (ggal_bot/log_rotation.py, NO ggal_bot/data/
log_rotation.py): este modulo lo usa tanto un logger de ggal_bot/data/
(MarketSnapshotLogger/CclBondQuoteLogger) como uno de ggal_bot/portfolio/
(PortfolioGreeksLogger). Ponerlo dentro de ggal_bot/data/ obligaria a
PortfolioGreeksLogger a hacer `from ggal_bot.data import log_rotation`,
lo que ejecuta ggal_bot/data/__init__.py entero (importa MarketDataFeed/
order_gateway/etc. de forma EAGER) con solo importar el logger de
Griegas - un modulo que, antes de esta mejora, no dependia de
ggal_bot.config en absoluto. Eso rompe el orden de aislamiento de tests
documentado en ggal_bot/validation/_env_isolation.py (confirmado
empiricamente: test_portfolio_greeks_log.py empezo a fallar
intermitentemente con este modulo puesto en ggal_bot/data/, porque un test
simple que solo construye PortfolioGreeksLogger - sin pasar por
_shadow_audit_isolation primero - terminaba "congelando" SETTINGS con el
entorno REAL del proceso antes de tiempo). Vivir en ggal_bot/ (sin
`__init__.py` con imports eager) evita el problema por completo.
"""
from __future__ import annotations

import gzip
import logging
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ggal_bot.log_rotation")


def archive_name(path: Path, for_date: date) -> Path:
    """Nombre del archivo .gz rotado de `path` para la fecha `for_date` (ver docstring del modulo)."""
    return path.with_name(f"{path.stem}-{for_date.isoformat()}{path.suffix}.gz")


def rotate_if_new_day(path: Path, now: Optional[datetime] = None) -> None:
    """
    Si `path` existe, no esta vacio, y su mtime cae en un dia UTC anterior
    al de `now`, lo archiva comprimido (gzip) a `archive_name(path,
    mtime.date())` y lo borra - dejando el path "vivo" libre para que el
    proximo write empiece un archivo nuevo y vacio.

    No hace nada si `path` no existe/esta vacio, o si su mtime ya es del
    dia de `now` (nada que rotar todavia). Un archivo de destino ya
    existente (ej. ya se roto antes en este mismo arranque) NUNCA se
    sobreescribe - se deja el archivo actual sin rotar antes que perder
    datos. Un fallo al archivar/comprimir se loguea y nunca propaga (mismo
    criterio que el resto de los loggers de este proyecto: un fallo de
    rotacion NUNCA debe tumbar el ciclo de trading real).
    """
    if not path.exists() or path.stat().st_size == 0:
        return
    current = now if now is not None else datetime.now(timezone.utc)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    if mtime.date() >= current.date():
        return
    dest = archive_name(path, mtime.date())
    if dest.exists():
        logger.warning(
            "log_rotation: %s ya existe - no se vuelve a archivar %s (queda sin rotar esta vez).",
            dest, path,
        )
        return
    try:
        with open(path, "rb") as src, gzip.open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)
        path.unlink()
        logger.info("log_rotation: archivado y comprimido %s -> %s.", path, dest)
    except Exception:
        logger.exception(
            "log_rotation: fallo al archivar/comprimir %s - se sigue escribiendo en el mismo archivo.",
            path,
        )


def purge_old_archives(path: Path, retention_days: Optional[int], now: Optional[datetime] = None) -> None:
    """
    Borra los archivos .gz ya rotados de `path` (ver rotate_if_new_day/
    archive_name) cuya fecha - la del NOMBRE del archivo, no el mtime del
    .gz en si (ese es el momento de la COMPRESION, no el del dato que
    contiene) - sea mas vieja que `retention_days` dias antes de `now`.

    `retention_days` None o <=0 desactiva el borrado (retencion infinita,
    comportamiento identico al de antes de esta mejora) - nunca se borra
    nada salvo que el caller pase un entero positivo explicito. Un nombre
    de archivo que no matchea el patron esperado (`{stem}-{YYYY-MM-DD}
    {suffix}.gz`) se deja intacto, nunca se borra a ciegas. Un fallo al
    borrar un archivo puntual se loguea y nunca propaga (mismo criterio
    que rotate_if_new_day).
    """
    if not retention_days or retention_days <= 0:
        return
    current = now if now is not None else datetime.now(timezone.utc)
    cutoff = current.date() - timedelta(days=retention_days)
    prefix = f"{path.stem}-"
    pattern = f"{prefix}*{path.suffix}.gz"
    for archive in path.parent.glob(pattern):
        date_str = archive.name[len(prefix):len(prefix) + 10]
        try:
            archive_date = date.fromisoformat(date_str)
        except ValueError:
            continue  # nombre no matchea el patron esperado - no se toca
        if archive_date < cutoff:
            try:
                archive.unlink()
                logger.info(
                    "log_rotation: borrado %s (retencion=%d dias, fecha_archivo=%s < corte=%s).",
                    archive, retention_days, archive_date.isoformat(), cutoff.isoformat(),
                )
            except Exception:
                logger.exception("log_rotation: fallo al borrar archivo vencido %s.", archive)
