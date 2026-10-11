#!/usr/bin/env bash
# Morph Studio launcher for macOS/Linux: pulls the latest code, installs deps on first run, opens the browser.
set -e
cd "$(dirname "$0")"
git pull --ff-only || true
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q --upgrade pip && pip install -q -r requirements.txt
python -m morph.cli studio egg-picker --open
