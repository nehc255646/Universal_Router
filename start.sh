#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "========================================"
echo " Universal Router - Local Gateway"
echo "========================================"

if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
  echo "[ERROR] Python 3.11+ not found"
  exit 1
fi
PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
$PY --version

if ! $PY -c "import fastapi,uvicorn,httpx,pydantic" >/dev/null 2>&1; then
  echo "[INFO] Installing dependencies..."
  $PY -m pip install -q -r requirements.txt
fi

if [ ! -f config.json ]; then
  cp config.example.json config.json
  echo "[INFO] Created config.json from example (edit API keys)"
fi

PORT=$($PY -c "import json;print(json.load(open('config.json',encoding='utf-8')).get('server',{}).get('port',8787))" 2>/dev/null || echo 8787)

echo
echo "[INFO] Gateway:  http://127.0.0.1:${PORT}/v1"
echo "[INFO] Frontend: http://127.0.0.1:${PORT}/"
echo "[INFO] Health:   http://127.0.0.1:${PORT}/health"
echo "========================================"

exec $PY -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}"
