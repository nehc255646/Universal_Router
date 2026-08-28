@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Universal Router

echo ========================================
echo  Universal Router - Local Gateway
echo ========================================
echo.

set "PY="
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :found_py
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto :found_py
python3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=python3"
if defined PY goto :found_py
echo [ERROR] Python 3.11+ not found
pause
exit /b 1

:found_py
%PY% --version

%PY% -c "import fastapi,uvicorn,httpx,pydantic" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing dependencies...
    %PY% -m pip install -q -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] pip install failed
        pause
        exit /b 1
    )
)

if not exist "config.json" (
    copy /y "config.example.json" "config.json" >nul
    echo [INFO] Created config.json from example (edit API keys)
)

set "PORT=8787"
set "PORTFILE=%TEMP%\ur-port-%RANDOM%.txt"
%PY% -c "import json;print(json.load(open('config.json',encoding='utf-8')).get('server',{}).get('port',8787))" > "%PORTFILE%" 2>nul
if exist "%PORTFILE%" set /p PORT=<"%PORTFILE%"
if exist "%PORTFILE%" del /q "%PORTFILE%" >nul 2>&1
if "%PORT%"=="" set "PORT=8787"

echo.
echo [INFO] Gateway:  http://127.0.0.1:%PORT%/v1
echo [INFO] Frontend: http://127.0.0.1:%PORT%/
echo [INFO] Health:   http://127.0.0.1:%PORT%/health
echo [INFO] Open frontend via 入口.url after the server starts
echo ========================================

%PY% -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
if errorlevel 1 pause
endlocal
