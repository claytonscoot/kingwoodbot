@echo off
setlocal enabledelayedexpansion

REM ====== SETTINGS ======
set PROJECT_DIR=C:\Users\clayt\kingwoodbot
set PORT=8000

REM ====== GO TO PROJECT ======
cd /d "%PROJECT_DIR%"

REM ====== CHECK PYTHON DEPENDENCIES ======
echo Checking Python dependencies...
pip show fastapi >nul 2>&1
if errorlevel 1 (
    echo Installing required Python packages...
    pip install fastapi uvicorn requests aiofiles pydantic[email]
)

REM ====== START OLLAMA (only if not already running) ======
echo Checking Ollama service...
tasklist /FI "IMAGENAME eq ollama.exe" | find /I "ollama.exe" >nul
if errorlevel 1 (
    echo Starting Ollama...
    start "Ollama" cmd /k "ollama serve"
    timeout /t 5 >nul
) else (
    echo Ollama already running.
)

REM ====== CHECK MISTRAL MODEL ======
echo Checking if Mistral model is available...
ollama list | find "mistral" >nul
if errorlevel 1 (
    echo Mistral model not found. Pulling model...
    echo This may take a few minutes...
    ollama pull mistral
)

REM ====== START FASTAPI ======
echo Starting FastAPI server...
REM Note: Changed from app:app to main:app to match our main.py file
start "FastAPI - Astro Outdoor Designs" cmd /k "cd /d %PROJECT_DIR% && uvicorn main:app --reload --host 0.0.0.0 --port %PORT%"

REM ====== WAIT FOR PORT TO LISTEN ======
echo Waiting for FastAPI to listen on %PORT%...
for /L %%i in (1,1,30) do (
    powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient('127.0.0.1', %PORT%); $c.Close(); exit 0 } catch { exit 1 }"
    if not errorlevel 1 (
        echo FastAPI is up and running!
        goto :PORT_READY
    )
    echo Attempt %%i/30 - Still waiting...
    timeout /t 2 >nul
)
echo ERROR: FastAPI did not start on port %PORT%.
echo Check the FastAPI window for errors.
goto :END

:PORT_READY

REM ====== START NGROK (if available) ======
where ngrok >nul 2>&1
if not errorlevel 1 (
    echo Starting ngrok tunnel...
    start "ngrok" cmd /k "ngrok http %PORT% --host-header=localhost:%PORT%"
    timeout /t 3 >nul
) else (
    echo ngrok not found - skipping tunnel creation
    echo Install ngrok if you need public access: https://ngrok.com/download
)

REM ====== OPEN PAGES ======
echo Opening application...
start "" "http://127.0.0.1:%PORT%"

REM ====== OPEN NGROK DASHBOARD (if ngrok is running) ======
where ngrok >nul 2>&1
if not errorlevel 1 (
    timeout /t 2 >nul
    start "" "http://127.0.0.1:4040"
    echo.
    echo ====== SUCCESS! ======
    echo Local URL: http://127.0.0.1:%PORT%
    echo Ngrok Dashboard: http://127.0.0.1:4040
    echo Admin Dashboard: http://127.0.0.1:%PORT%/admin
) else (
    echo.
    echo ====== SUCCESS! ======
    echo Local URL: http://127.0.0.1:%PORT%
    echo Admin Dashboard: http://127.0.0.1:%PORT%/admin
)

echo.
echo Press any key to close this window...
pause >nul

:END
endlocal
