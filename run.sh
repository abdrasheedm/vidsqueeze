#!/bin/bash
# Start the VidSqueeze server and open the UI in the browser.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || ./setup.sh

URL="http://127.0.0.1:8765"
( sleep 1.5; open "$URL" ) &
exec .venv/bin/uvicorn app.server:app --host 127.0.0.1 --port 8765 --no-access-log
