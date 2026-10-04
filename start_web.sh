#!/usr/bin/env bash
# ============================================================================
#  Paper Trader - start the web app (Linux / macOS)
#
#      ./start_web.sh                     http://127.0.0.1:8080
#      ./start_web.sh --host 0.0.0.0      reachable from other devices
#      ./start_web.sh --reset-password    print a new owner password and exit
#      ./start_web.sh --reset-password you@example.com
#
#  For a server that keeps running after you log out, use the systemd unit in
#  deploy/paper-trader.service (or deploy/oracle-cloud-setup.sh for a free
#  Oracle Cloud VM) instead of this script.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

PYEXE="${PYEXE:-python3}"
if ! command -v "$PYEXE" >/dev/null 2>&1; then
  echo "Python 3 was not found. Install it first (python3 --version)." >&2
  exit 1
fi

if ! "$PYEXE" -c "import pandas, websocket" >/dev/null 2>&1; then
  echo "Installing runtime dependencies (pandas, websocket-client)..."
  "$PYEXE" -m pip install -r requirements.txt
fi

echo
echo "  Starting the paper trader. The trading engine runs inside this server,"
echo "  so it keeps scanning whether or not a browser is open."
echo
exec "$PYEXE" app_web.py "$@"
