#!/bin/bash
# Start the VidSqueeze server and open the UI in the browser.
# Windows: use run.ps1 instead.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || ./setup.sh

URL="http://127.0.0.1:8765"
opener=$(command -v open || command -v xdg-open || true)
[ -n "$opener" ] && ( sleep 1.5; "$opener" "$URL" ) &
exec .venv/bin/uvicorn app.server:app --host 127.0.0.1 --port 8765 --no-access-log
