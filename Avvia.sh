#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Errore: Python 3 non è installato o non è disponibile nel PATH." >&2
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Creo l'ambiente Python..."
  python3 -m venv .venv
fi

if [[ "${1:-}" == "--cli" ]]; then
  module_check='import playwright, dotenv'
else
  module_check='import playwright, dotenv, customtkinter, PIL'
fi

if ! .venv/bin/python -c "$module_check" >/dev/null 2>&1; then
  echo "Installo le dipendenze..."
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python -m playwright install chromium
fi

if [[ "${1:-}" == "--cli" ]]; then
  shift
  exec .venv/bin/python -u extract_stream_transcript.py "$@"
fi

exec .venv/bin/python gui.py
