#!/usr/bin/env bash
set -euo pipefail
if [[ -z "${PYTHON_BIN:-}" && -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
elif [[ -z "${PYTHON_BIN:-}" && -x "../.venv/bin/python" ]]; then
  PYTHON_BIN="../.venv/bin/python"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$PWD/.pyinstaller}"
"$PYTHON_BIN" -m PyInstaller --clean --noconfirm gbflash_unlock.spec
echo "Linux runnable directory: dist/gbflash-unlock"
