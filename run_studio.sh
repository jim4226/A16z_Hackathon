#!/usr/bin/env bash
# Morph Studio launcher for macOS and Linux. The first run makes .venv with Python 3.10 to 3.14 and installs the
# tested packages (requirements.txt with constraints.txt: a few minutes, with progress). Later runs start in seconds
# and use neither git nor the network.
#   ./run_studio.sh              start the Studio (other options go to `morph studio`, e.g. --remote)
#   ./run_studio.sh --update     first switch this git clone to the latest release (newest v* tag), then start
# Everything runs from functions that bash has read in full, so --update can safely replace this file.

supported() {  # is "$1" a Python 3.10 to 3.14?
  "$1" -c 'import sys; sys.exit(not ((3, 10) <= sys.version_info[:2] <= (3, 14)))' >/dev/null 2>&1
}

find_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3.14 python3; do
    if command -v "$c" >/dev/null 2>&1 && supported "$c"; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

update() {  # 0: switched to the latest release; 1: keep the version that is here
  local latest
  if [ ! -e .git ]; then
    echo "--update: this folder isn't a git clone, so it can't switch versions itself. Download the latest release"
    echo "from https://github.com/jim4226/A16z_Hackathon/releases into a new folder and copy your projects folder"
    echo "and .env across. Starting the version you have."
    return 1
  fi
  if ! command -v git >/dev/null 2>&1; then
    echo "--update needs Git (https://git-scm.com/downloads). Starting the version you have."
    return 1
  fi
  echo "Looking for the latest Morph release..."
  if ! git fetch --tags --force --quiet; then
    echo "Couldn't reach GitHub (details above). Starting the version you have."
    return 1
  fi
  latest=$(git -c versionsort.suffix=a -c versionsort.suffix=b -c versionsort.suffix=rc \
    tag --list 'v*' --sort=-v:refname | head -n 1)
  if [ -z "$latest" ]; then
    echo "There is no release (v* tag) yet. Starting the version you have."
    return 1
  fi
  if [ "$(git rev-parse HEAD)" = "$(git rev-parse "$latest~0")" ]; then
    echo "Already on $latest, the latest release."
    return 1
  fi
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "Files in this folder were changed (git status lists them), so --update won't switch to $latest."
    echo "Commit or undo the changes, then run it again. Starting the version you have."
    return 1
  fi
  if ! git -c advice.detachedHead=false checkout --quiet "$latest"; then
    echo "Couldn't switch to $latest (details above). Starting the version you have."
    return 1
  fi
  echo "Switched to Morph $latest."
}

make_venv() {
  local py found
  if [ -d .venv ] && { [ ! -f .venv/bin/activate ] || ! supported .venv/bin/python; }; then
    echo "The .venv folder is incomplete or uses an unsupported Python: making a new one."
    rm -rf .venv
  fi
  [ -d .venv ] && return 0
  if ! py=$(find_python); then
    found=$(python3 --version 2>/dev/null) && found=" (python3 here is $found)"
    echo "Morph needs Python 3.10, 3.11, 3.12, 3.13 or 3.14 and found none of them$found."
    echo "Install Python 3.13 from https://www.python.org/downloads/ (or your package manager), then run this again."
    echo "Python 3.15 is too new for now: one of Morph's libraries has no build for it yet."
    return 1
  fi
  echo "Creating .venv with $("$py" --version 2>&1)..."
  if ! "$py" -m venv .venv || [ ! -f .venv/bin/activate ]; then
    rm -rf .venv
    echo "Python couldn't create .venv. On Debian or Ubuntu, install its venv module first:"
    echo "  sudo apt install $("$py" -c 'import sys; print("python%d.%d-venv" % sys.version_info[:2])')"
    return 1
  fi
}

install_packages() {  # once, and again only when requirements.txt or constraints.txt change (after --update)
  local want
  want=$(cat requirements.txt constraints.txt | cksum)
  if [ -f .venv/.morph-installed ] && [ "$(cat .venv/.morph-installed)" = "$want" ]; then
    return 0
  fi
  echo "Installing Morph's packages into .venv (numpy, SciPy, MuJoCo and others; the first time takes a few minutes)."
  .venv/bin/python -m pip install --quiet --upgrade pip || true
  if ! .venv/bin/python -m pip install -r requirements.txt -c constraints.txt; then
    echo
    echo "Installing the packages failed (details above). Check the internet connection, then run this again."
    return 1
  fi
  echo "$want" > .venv/.morph-installed
}

main() {
  local arg want_update=0
  local args=()
  cd "$(dirname "$0")" || return 1
  export PYTHONUTF8=1 PIP_DISABLE_PIP_VERSION_CHECK=1
  for arg in "$@"; do
    if [ "$arg" = "--update" ]; then want_update=1; else args+=("$arg"); fi
  done
  if [ "$want_update" = 1 ] && update; then
    exec bash ./run_studio.sh ${args[@]+"${args[@]}"}  # the launcher of the release just checked out
  fi
  make_venv || return 1
  install_packages || return 1
  # shellcheck disable=SC1091
  . .venv/bin/activate
  python -m morph.cli studio egg-picker --open ${args[@]+"${args[@]}"}
}

main "$@"; exit $?
