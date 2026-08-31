@echo off
REM run_dashboard.bat
REM ===================
REM Lanza el Dashboard de monitoreo (dashboard/app.py) en el navegador.
REM Igual que run_bot.bat, evita el problema de ejecutar un ".py" directo
REM en PowerShell/Explorador (ver run_bot.bat para el detalle).
REM
REM Uso: doble click desde el Explorador, o desde PowerShell/CMD:
REM     .\run_dashboard.bat
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo No se encontro el entorno virtual .venv en esta carpeta.
    echo Corre primero:
    echo     python -m venv .venv
    echo     .venv\Scripts\activate
    echo     pip install -r requirements.txt
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"

python -m pip show streamlit >nul 2>&1
if errorlevel 1 (
    echo Instalando dependencias del dashboard ^(solo hace falta una vez^)...
    python -m pip install -r requirements-dashboard.txt
    if errorlevel 1 (
        echo No se pudieron instalar las dependencias del dashboard. Revisa tu conexion/pip.
        pause
        exit /b 1
    )
)

echo.
echo Abriendo el dashboard en el navegador (Ctrl+C en esta ventana para detenerlo)...
echo.
streamlit run dashboard\app.py

pause
