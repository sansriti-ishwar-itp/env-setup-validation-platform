# Start API (port 8000) and Vite UI (port 5173). Requires: pip install -e . ; cd frontend && npm install

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Start-Process powershell -ArgumentList "-NoExit", "-Command", `
  "cd `"$root`"; uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000"

Start-Sleep -Seconds 2
Set-Location "$root\frontend"
npm run dev
