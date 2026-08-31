@echo off
REM run_bot.bat
REM ============
REM Lanzador para Windows: evita el problema de ejecutar ".\run_bot.py"
REM directo en PowerShell/Explorador, donde la extension .py puede estar
REM asociada a otro programa (por ejemplo un editor) en vez del interprete
REM de Python. Este .bat siempre invoca "python run_bot.py" explicitamente.
REM
REM Uso: doble click desde el Explorador, o desde PowerShell/CMD:
REM     .\run_bot.bat
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else (
    echo ADVERTENCIA: no se encontro el entorno virtual .venv en esta carpeta.
    echo Se intentara usar el "python" del PATH del sistema.
    echo Si esto falla, corre primero:
    echo     python -m venv .venv
    echo     .venv\Scripts\activate
    echo     pip install -r requirements.txt
    echo.
)

python run_bot.py
set EXITCODE=%ERRORLEVEL%

echo.
if %EXITCODE% neq 0 (
    echo [run_bot.bat] El bot termino con un error ^(codigo %EXITCODE%^). Revisa el detalle de arriba.
) else (
    echo [run_bot.bat] El bot se detuvo normalmente.
)

pause
exit /b %EXITCODE%
