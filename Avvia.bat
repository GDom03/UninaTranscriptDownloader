@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creo l'ambiente Python...
    py -3 -m venv .venv
    if errorlevel 1 goto errore
)

if /I "%~1"=="--cli" (
    ".venv\Scripts\python.exe" -c "import playwright, dotenv" >nul 2>&1
) else (
    ".venv\Scripts\python.exe" -c "import playwright, dotenv, customtkinter, PIL" >nul 2>&1
)
if errorlevel 1 (
    echo Installo le dipendenze...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto errore
    ".venv\Scripts\python.exe" -m playwright install chromium
    if errorlevel 1 goto errore
)

if /I "%~1"=="--cli" goto cli
".venv\Scripts\python.exe" gui.py
set "result=%errorlevel%"
if not "%result%"=="0" (
    echo.
    echo La GUI non si e' avviata correttamente. Leggi il messaggio di errore qui sopra.
    pause
)
exit /b %result%

:cli
shift
".venv\Scripts\python.exe" -u extract_stream_transcript.py %*
set "result=%errorlevel%"
echo.
if not "%result%"=="0" pause
exit /b %result%

:errore
echo.
echo Installazione non riuscita. Verifica che Python 3 sia installato e disponibile con il comando py.
pause
exit /b 1
