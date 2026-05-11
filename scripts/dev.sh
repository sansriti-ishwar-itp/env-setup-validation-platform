#!/usr/bin/env bash
# Start API and UI. Usage: chmod +x scripts/dev.sh && ./scripts/dev.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000 &
sleep 2
cd "$ROOT/frontend"
npm run dev
