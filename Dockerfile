# Dockerfile para correr GGAL_BOT en Northflank (o cualquier host con Docker).
#
# Notas de diseño:
# - Solo instala requirements.txt (pyRofex/python-dotenv/requests): las
#   dependencias de build-a-exe (requirements-build.txt, PyInstaller) y del
#   dashboard (requirements-dashboard.txt, Streamlit) NO hacen falta para
#   correr el bot en un contenedor Linux y se dejan afuera a proposito.
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

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ggal_bot/ ggal_bot/
COPY run_bot.py .

RUN mkdir -p logs state data_cache

CMD ["python", "run_bot.py"]
