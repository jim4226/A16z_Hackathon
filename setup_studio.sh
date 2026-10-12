#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUTF8=1
exec "${RCUBED_PYTHON:-python3}" tools/install_local.py --setup
