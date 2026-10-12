#!/usr/bin/env bash
# Install once, then run the installed release without updating code or packages.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUTF8=1
if [ -x .rcubed-venv/bin/python ]; then
  exec .rcubed-venv/bin/python tools/install_local.py --launch
else
  exec "${RCUBED_PYTHON:-python3}" tools/install_local.py --launch
fi
