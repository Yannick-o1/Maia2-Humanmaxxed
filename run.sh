#!/usr/bin/env bash
# Set up whatever is missing, then start the server and open the board.
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
PORT="${PORT:-8000}"
URL="http://localhost:${PORT}"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m==>\033[0m %s\n' "$*" >&2; exit 1; }

# --- Homebrew ---------------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$candidate" ] && eval "$("$candidate" shellenv)" && break
  done
fi
command -v brew >/dev/null 2>&1 || die "Homebrew not found. Install it from https://brew.sh and run this again."

# --- Python 3.12 ------------------------------------------------------------
# maia2 requires >=3.10,<3.13, so we pin 3.12 rather than whatever is default.
find_python() {
  for candidate in \
    "$(brew --prefix 2>/dev/null)/opt/python@3.12/bin/python3.12" \
    /opt/homebrew/opt/python@3.12/bin/python3.12 \
    /usr/local/opt/python@3.12/bin/python3.12 \
    "$(command -v python3.12 2>/dev/null || true)"; do
    [ -n "$candidate" ] && [ -x "$candidate" ] && echo "$candidate" && return 0
  done
  return 1
}

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
  say "Installing Python 3.12 (maia2 does not support 3.13 yet)"
  brew install python@3.12
  PYTHON="$(find_python || true)"
  [ -n "$PYTHON" ] || die "Could not find python3.12 after installing it."
fi

# --- Stockfish --------------------------------------------------------------
if ! command -v stockfish >/dev/null 2>&1 && [ ! -x "$(brew --prefix)/bin/stockfish" ]; then
  say "Installing Stockfish (used for the post-game review)"
  brew install stockfish
fi
export STOCKFISH_PATH="${STOCKFISH_PATH:-$(command -v stockfish || echo "$(brew --prefix)/bin/stockfish")}"

# --- virtualenv -------------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  say "Creating virtualenv with $("$PYTHON" --version)"
  "$PYTHON" -m venv "$VENV"
fi

if ! "$VENV/bin/python" -c "import maia2, fastapi, chess, uvicorn" >/dev/null 2>&1; then
  say "Installing Python dependencies (this pulls in PyTorch, so give it a minute)"
  "$VENV/bin/python" -m pip install --upgrade pip >/dev/null
  "$VENV/bin/python" -m pip install -r requirements.txt
fi

# --- model ------------------------------------------------------------------
# ~280MB, downloaded once into ./maia2_models on first run.
if [ ! -f "maia2_models/rapid_model.pt" ]; then
  say "The Maia-2 rapid model (280MB) will download on first start."
fi

mkdir -p data

# --- go ---------------------------------------------------------------------
say "Starting on $URL"
(
  # Wait for the port to answer before opening the browser.
  for _ in $(seq 1 60); do
    if curl -sf "$URL/api/health" >/dev/null 2>&1; then
      open "$URL" 2>/dev/null || true
      exit 0
    fi
    sleep 0.5
  done
) &

exec "$VENV/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --log-level info
