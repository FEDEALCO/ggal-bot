# Dockerfile para correr GGAL_BOT en Northflank (o cualquier host con Docker).
#
# Notas de diseño:
# - Instala requirements.txt (pyRofex/python-dotenv/requests) Y TAMBIEN
#   requirements-dashboard.txt (Streamlit/Plotly/pandas/numpy) para poder
#   correr el Dashboard de monitoreo (dashboard/app.py) DENTRO del mismo
#   contenedor que el bot, como proceso hermano (ver entrypoint.sh).
#   requirements-build.txt (PyInstaller, solo para el .exe de escritorio)
#   sigue sin hacer falta aca y se deja afuera.
# - Por que el dashboard va en el MISMO contenedor y no como un segundo
#   servicio Northflank: el volumen persistente que guarda logs/ y state/
#   (ver mas abajo) se creo con access mode "Single Read/Write Once" - un
#   solo servicio puede montarlo a la vez, asi que un segundo servicio no
#   podria leer los datos reales del bot. Corriendo ambos procesos en el
#   mismo contenedor comparten el mismo filesystem sin ese problema.
#   GGAL_BOT_ENABLE_DASHBOARD=false (env var) desactiva el dashboard y
#   corre solo el bot, sin tocar esta imagen (ver entrypoint.sh).
# - NO se copia ningun .env real ni credenciales a la imagen (ver
#   .dockerignore): en Northflank las variables de entorno / secrets se
#   inyectan en runtime como variables de entorno del proceso, y config.py
#   ya las lee via os.getenv() con .env como fallback opcional (ver
#   ggal_bot/config.py) - no hace falta ningun cambio de codigo.
# - logs/, state/ y data_cache/ se crean solos en el arranque (ver
#   ggal_bot/paths.py) pero viven DENTRO del contenedor por defecto: si el
#   contenedor se recrea (redeploy, restart), bot_state.json y los shadow
#   trades se pierden salvo que se monte un volumen persistente en
#   /app/state, /app/logs y /app/data_cache (Northflank > Volumes).
# - GIT_SHA (build arg, MEJORA 2026-09-30, ver ggal_bot/version_info.py):
#   .dockerignore excluye .git/ tanto de la imagen COMO del build context,
#   asi que `git rev-parse HEAD` no se puede correr DENTRO de este
#   Dockerfile (el directorio .git ni siquiera llega al daemon de Docker).
#   Quien dispare el build tiene que calcular el SHA AFUERA y pasarlo con
#   --build-arg GIT_SHA=$(git rev-parse HEAD). En Northflank esto se
#   configura en "Build arguments" del servicio (ver docs.northflank.com,
#   "inject build arguments") - no encontramos documentacion publica de
#   una variable auto-inyectada con el SHA del commit buildeado, asi que
#   el usuario tiene que setear ese build-arg el mismo (o automatizarlo
#   con un paso de CI propio). Sin build-arg, GGAL_BOT_GIT_SHA queda en
#   "unknown" - nunca se fabrica un SHA.

FROM python:3.11-slim

ARG GIT_SHA=unknown
ENV GGAL_BOT_GIT_SHA=${GIT_SHA}

# Evita que Python bufferee stdout/stderr (para que los logs de Northflank
# se vean en tiempo real, no recien al final del proceso).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt requirements-dashboard.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dashboard.txt

COPY ggal_bot/ ggal_bot/
COPY dashboard/ dashboard/
COPY run_bot.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Directorios que paths.py crea si no existen; se declaran aca para que
# quede claro cuales son los candidatos a volumen persistente en Northflank.
RUN mkdir -p logs state data_cache

# Puerto del dashboard (Streamlit) - ver GGAL_BOT_DASHBOARD_PORT en
# entrypoint.sh. El bot en si no expone ningun puerto (no es un servidor
# HTTP).
EXPOSE 8501

CMD ["./entrypoint.sh"]
