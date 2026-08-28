@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Universal Router

echo ========================================
echo  Universal Router - Local Gateway
echo ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found, please install Python 3.11+
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do echo [INFO] Python %%v

echo [INFO] Checking dependencies...
python -c "import fastapi,uvicorn,httpx,pydantic" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing dependencies...
    python -m pip install -q fastapi "uvicorn[standard]" httpx pydantic anyio
    if errorlevel 1 (
        echo [ERROR] pip install failed
        pause
        exit /b 1
    )
    echo [INFO] Dependencies installed
) else (
    echo [INFO] Dependencies OK
)

if not exist "config.json" (
    if exist "config.example.json" (
        copy /y "config.example.json" "config.json" >nul
        echo [INFO] Created config.json from example — edit API keys
    ) else (
        echo {"server": {"host": "127.0.0.1", "port": 8787, "local_api_key": ""}, "providers": []} > config.json
        echo [INFO] Created default config.json
    )
)

set PORT=8787
for /f "tokens=*" %%a in ('python -c "import json;print(json.load(open('config.json',encoding='utf-8')).get('server',{}).get('port',8787))" 2^>nul') do set PORT=%%a

echo.
echo [INFO] Gateway: http://127.0.0.1:%PORT%/v1
echo [INFO] Frontend: http://127.0.0.1:%PORT%/
echo [INFO] Health: http://127.0.0.1:%PORT%/health
echo [INFO] Press Ctrl+C to stop
echo ========================================
echo.

start "" "http://127.0.0.1:%PORT%/"
python -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%

if errorlevel 1 pause
endlocal
