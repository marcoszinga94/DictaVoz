@echo off
REM Compila DictaVoz.exe y el instalador en un PC con Windows.
REM Requisitos: Python 3.12 instalado con "Add to PATH", y NSIS instalado.
REM Uso:        build_win.bat   (opcional: set WHISPER_DIR=C:\ruta\con\whisper-cli.exe)
cd /d "%~dp0\..\.."

pip install -r packaging\windows\requirements-win.txt pyinstaller
if errorlevel 1 goto :error

if defined WHISPER_DIR (
    echo Empaquetando whisper-cli.exe desde %WHISPER_DIR%
) else (
    echo Aviso: WHISPER_DIR no definido. El .exe NO llevará whisper-cli; descarga el
    echo modelo y pon whisper-cli.exe en el PATH para poder transcribir.
)

pyinstaller --noconfirm packaging\windows\dictavoz_win.spec
if errorlevel 1 goto :error

makensis packaging\windows\installer.nsi
if errorlevel 1 goto :error

echo.
echo Listo: dist\DictaVoz.exe y dist\DictaVoz-Setup.exe
exit /b 0

:error
echo Falló la compilación. Revisa los errores de arriba.
exit /b 1