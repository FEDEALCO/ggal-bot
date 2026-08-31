@echo off
REM build_exe.bat
REM ==============
REM Compila GGAL BOT como un ejecutable de Windows standalone (dist\GGAL_BOT.exe)
REM usando PyInstaller. Correr UNA VEZ (o cada vez que cambies el codigo) desde
REM esta misma carpeta, en Windows, con el .venv del proyecto ya creado.
REM
REM IMPORTANTE: PyInstaller empaqueta para el sistema operativo en el que se
REM ejecuta - no hay forma de "cross-compilar" un .exe de Windows desde Linux/Mac,
REM por eso este build tiene que correr en TU maquina Windows, no se puede generar
REM el binario final desde otro lado. Este script automatiza los pasos, pero el
REM primer build tiene que hacerse aca.
REM
REM Uso:
REM     .\build_exe.bat
REM
REM Resultado: dist\GGAL_BOT.exe (+ tu .env copiado al lado, para que el
REM ejecutable arranque con tus credenciales sin pasos adicionales).

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

python -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Instalando PyInstaller ^(solo hace falta una vez^)...
    python -m pip install -r requirements-build.txt
    if errorlevel 1 (
        echo No se pudo instalar PyInstaller. Revisa tu conexion/pip.
        pause
        exit /b 1
    )
)

REM 'enum34' es un backport obsoleto de la libreria estandar 'enum' que
REM algunos paquetes viejos instalan como dependencia transitiva; en Python
REM 3 ya viene incluida en el interprete, y PyInstaller directamente se
REM niega a compilar si la detecta instalada (choca con la version nativa).
REM Se desinstala automaticamente si aparece, en vez de hacer fallar el
REM build cada vez.
python -m pip show enum34 >nul 2>&1
if not errorlevel 1 (
    echo Se detecto el paquete 'enum34' instalado ^(incompatible con PyInstaller
    echo en Python 3^); se desinstala automaticamente...
    python -m pip uninstall -y enum34
)

echo.
echo Compilando GGAL_BOT.exe (puede tardar 1-2 minutos)...
echo.

REM --onefile: un unico .exe portable (mas lento para arrancar, mas comodo
REM   para copiar/compartir).
REM --console: deja la ventana de consola abierta con los logs en vivo
REM   (el bot es un proceso de larga duracion pensado para monitorear).
REM --name: nombre del ejecutable resultante (dist\GGAL_BOT.exe).
pyinstaller --noconfirm --onefile --console --name GGAL_BOT run_bot.py

if errorlevel 1 (
    echo.
    echo La compilacion fallo. Revisa el detalle de arriba.
    echo Si el error menciona un modulo faltante ^(ModuleNotFoundError al
    echo correr el .exe^), agregalo con --hidden-import=NOMBRE_DEL_MODULO
    echo en la linea de pyinstaller de este script y volve a correrlo.
    pause
    exit /b 1
)

REM Copiar el .env real (con tus credenciales) y .env.example al lado del
REM .exe generado, para que dist\GGAL_BOT.exe arranque sin pasos extra.
if exist ".env" copy /Y ".env" "dist\.env" >nul
if exist ".env.example" copy /Y ".env.example" "dist\.env.example" >nul

echo.
echo ==============================================================
echo Listo. El ejecutable quedo en:  dist\GGAL_BOT.exe
echo.
echo Podes moverlo (junto con el .env que se copio al lado) a
echo cualquier carpeta o pinchearlo en la barra de tareas. Al
echo correrlo, los logs, el estado (state\) y el CSV de auditoria de
echo Shadow Trading (logs\shadow_trades.csv) se crean automaticamente
echo AL LADO del .exe, no en esta carpeta de codigo fuente.
echo ==============================================================
pause
