#!/bin/bash
# entrypoint.sh
# =============
# Arranca el bot (run_bot.py) y, por defecto, TAMBIEN el dashboard de
# monitoreo (dashboard/app.py, via Streamlit) dentro del MISMO contenedor.
#
# Por que en el mismo contenedor y no como un segundo servicio de
# Northflank: el volumen persistente que guarda logs/shadow_trades.csv y
# state/bot_state.json (ver Dockerfile y ggal_bot/paths.py) quedo creado en
# Northflank con access mode "Single Read/Write Once" (un solo attach a la
# vez) - un segundo servicio Northflank no puede montar ese mismo volumen
# en simultaneo. Corriendo el dashboard como proceso HERMANO dentro del
# mismo contenedor, ambos procesos comparten el mismo filesystem montado
# sin ningun conflicto de acceso.
#
# Supervision minima: si CUALQUIERA de los dos procesos termina (crash o
# señal de apagado), este script mata al otro y sale con ese codigo de
# salida - para que Northflank vea el contenedor como caido y lo reinicie,
# en vez de quedar "medio vivo" con el bot muerto y el dashboard sirviendo
# datos viejos (o viceversa). Tambien reenvia SIGTERM/SIGINT a ambos hijos
# para que run_bot.py pueda completar su graceful shutdown (ver
# GgalOptionsBot._install_signal_handlers en run_bot.py) antes de que
# Northflank mande SIGKILL.
#
# Interruptor de emergencia: si el dashboard causara cualquier problema de
# estabilidad/recursos, GGAL_BOT_ENABLE_DASHBOARD=false (env var en
# Northflank + redeploy) vuelve a correr SOLO el bot, sin tocar esta imagen
# ni el codigo del bot en si.

set -u

BOT_PID=""
DASHBOARD_PID=""

_term() {
    echo "entrypoint.sh: señal de apagado recibida, propagando a los procesos hijos..."
    [ -n "$BOT_PID" ] && kill -TERM "$BOT_PID" 2>/dev/null
    [ -n "$DASHBOARD_PID" ] && kill -TERM "$DASHBOARD_PID" 2>/dev/null
}
trap _term TERM INT

if [ "${GGAL_BOT_ENABLE_DASHBOARD:-true}" = "false" ]; then
    echo "entrypoint.sh: GGAL_BOT_ENABLE_DASHBOARD=false - se corre SOLO el bot (sin dashboard)."
    exec python run_bot.py
fi

echo "entrypoint.sh: arrancando bot + dashboard en el mismo contenedor (puerto dashboard=${GGAL_BOT_DASHBOARD_PORT:-8501})."

python run_bot.py &
BOT_PID=$!

streamlit run dashboard/app.py \
    --server.port "${GGAL_BOT_DASHBOARD_PORT:-8501}" \
    --server.address 0.0.0.0 \
    --server.headless true \
    --server.enableCORS false \
    --server.enableXsrfProtection false &
DASHBOARD_PID=$!

# Espera a que CUALQUIERA de los dos termine primero (requiere bash >=4.3,
# presente en la imagen base python:3.11-slim/Debian).
wait -n "$BOT_PID" "$DASHBOARD_PID"
EXIT_CODE=$?

if ! kill -0 "$BOT_PID" 2>/dev/null; then
    echo "entrypoint.sh: el BOT termino primero (codigo=$EXIT_CODE) - apagando el dashboard tambien."
else
    echo "entrypoint.sh: el DASHBOARD termino primero (codigo=$EXIT_CODE) - apagando el bot tambien."
fi

kill "$BOT_PID" "$DASHBOARD_PID" 2>/dev/null
wait "$BOT_PID" 2>/dev/null
wait "$DASHBOARD_PID" 2>/dev/null

exit "$EXIT_CODE"
