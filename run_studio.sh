#!/usr/bin/env bash
# Morph Studio launcher for macOS and Linux. The first run makes .venv with Python 3.10 to 3.14 and installs the
# tested packages (requirements.txt with constraints.txt: a few minutes, with progress) and Morph itself, so .venv
# has the morph command. Later runs start in seconds and use neither git nor the network.
#   ./run_studio.sh              start the Studio (other options go to `morph studio`, e.g. --remote)
#   ./run_studio.sh --update     first switch this git clone to the latest release (newest v* tag), then start
# An option starting with # ends the options: the rest of the line was a comment pasted along with the command (zsh
# passes it on unless interactivecomments is set), so it is dropped instead of reaching `morph studio`.
# Everything runs from functions that bash has read in full, so --update can safely replace this file. (bash keeps
# reading the file it opened when git replaces it: launchers before v0.2, which ran git pull, finished as themselves.)

supported() {  # is "$1" a Python 3.10 to 3.14?
  "$1" -c 'import sys; sys.exit(not ((3, 10) <= sys.version_info[:2] <= (3, 14)))' >/dev/null 2>&1
}

runs_natively() {  # pip fetches packages for the Python's own platform: on a Mac, MuJoCo has builds for Apple silicon
  # (arm64) only, so a Python that runs as an Intel program (an Intel build, or any build in Rosetta) can't install it
  [ "$(uname -s 2>/dev/null)" = Darwin ] || return 0
  [ "$("$1" -c 'import platform; print(platform.machine())' 2>/dev/null)" = arm64 ]
}

in_rosetta() {  # this shell runs in Intel mode on an Apple silicon Mac
  [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = 1 ]
}

find_python() {  # a Python 3.10 to 3.14 that can install Morph's packages; with "any", one of any build
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3.14 python3; do
    if command -v "$c" >/dev/null 2>&1 && supported "$c" && { [ "$1" = any ] || runs_natively "$c"; }; then
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
    echo "from https://github.com/jim4226/morph-studio/releases into a new folder and copy your projects folder"
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
    echo "There is no release (v* tag) yet: \"git pull\" gets the newest code of this branch. Starting the version you have."
    return 1
  fi
  if [ "$(git rev-parse HEAD)" = "$(git rev-parse "$latest~0")" ]; then
    echo "Already on $latest, the latest release."
    return 1
  fi
  if git merge-base --is-ancestor "$latest" HEAD; then
    echo "This clone has $latest, the latest release, and newer commits, so --update leaves it as it is. Starting the"
    echo "version you have (\"git pull\" gets the newest code of its branch)."
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

platform_ok() {  # MuJoCo, Morph's physics engine, has builds for Macs with Apple silicon, not for Intel Macs
  [ "$(uname -s 2>/dev/null)" = Darwin ] && [ "$(uname -m 2>/dev/null)" = x86_64 ] || return 0
  in_rosetta && return 0                              # Apple silicon: the Python found decides (runs_natively)
  echo "Morph needs a Mac with Apple silicon (M1 or later): MuJoCo, its physics engine, has no build for Intel Macs."
  echo "Nothing was installed. A Windows or Linux computer with an Intel or AMD processor works."
  return 1
}

intel_python() {  # "$1" runs as an Intel program on an Apple silicon Mac: say why that can't work, and what does
  if in_rosetta; then
    echo "This Terminal runs in Intel mode (Rosetta), so $1 runs as an Intel program, where Morph's packages can't be"
    echo "installed. Nothing was installed. Start it as an Apple silicon program instead: arch -arm64 ./run_studio.sh"
    echo "(or turn off \"Open using Rosetta\" in Terminal's Get Info window)."
  else
    echo "$1 ($(command -v "$1")) is an Intel (x86_64) Python, which runs in Rosetta, where Morph's packages can't be"
    echo "installed. Nothing was installed. Install Python 3.13 from https://www.python.org/downloads/ (it runs natively"
    echo "on Apple silicon), then run this again."
  fi
}

make_venv() {
  local py found
  # an Intel .venv on Apple silicon never works; in a Terminal in Intel mode, a native one only looks that way
  if [ -d .venv ] && { [ ! -f .venv/bin/activate ] || ! supported .venv/bin/python ||
    { ! in_rosetta && ! runs_natively .venv/bin/python; }; }; then
    echo "The .venv folder is incomplete or uses an unsupported Python: making a new one."
    rm -rf .venv
  fi
  if [ -d .venv ]; then                              # kept: in a Terminal in Intel mode, only the mode is wrong
    runs_natively .venv/bin/python && return 0
    echo "This Terminal runs in Intel mode (Rosetta), where .venv's packages, made for Apple silicon, don't load."
    echo "Start it as an Apple silicon program instead: arch -arm64 ./run_studio.sh"
    echo "(or turn off \"Open using Rosetta\" in Terminal's Get Info window)."
    return 1
  fi
  platform_ok || return 1
  if ! py=$(find_python); then
    if py=$(find_python any); then
      intel_python "$py"
      return 1
    fi
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

install_packages() {  # once, and again only when requirements.txt, constraints.txt or pyproject.toml change
  local want
  want=$(cat requirements.txt constraints.txt pyproject.toml | cksum)
  if [ -f .venv/.morph-installed ] && [ "$(cat .venv/.morph-installed)" = "$want" ]; then
    return 0
  fi
  echo "Installing Morph's packages into .venv (numpy, SciPy, MuJoCo and others; the first time takes a few minutes)."
  .venv/bin/python -m pip install --quiet --upgrade pip || true
  if ! .venv/bin/python -m pip install -r requirements.txt -c constraints.txt -e .; then
    echo
    echo "Installing the packages failed (details above). Check the internet connection, then run this again."
    echo "Morph's packages have builds for an x64 Python on Windows, Macs with Apple silicon and 64-bit Linux on Intel or"
    echo "AMD; with other Pythons this step can fail."
    return 1
  fi
  echo "$want" > .venv/.morph-installed
}

parse_args() {  # sets want_update (0 or 1) and args (the options for `morph studio`)
  local arg
  want_update=0
  args=()
  for arg in "$@"; do
    case $arg in
      '#'*)
        echo "Ignoring the rest of the line from # on: it is a comment, not options for Morph."
        break ;;
      --update) want_update=1 ;;
      *) args+=("$arg") ;;
    esac
  done
}

main() {
  cd "$(dirname "$0")" || return 1
  export PYTHONUTF8=1 PIP_DISABLE_PIP_VERSION_CHECK=1
  parse_args "$@"
  if [ "$want_update" = 1 ] && update; then
    exec bash ./run_studio.sh ${args[@]+"${args[@]}"}  # the launcher of the release just checked out
  fi
  make_venv || return 1
  install_packages || return 1
  # shellcheck disable=SC1091
  . .venv/bin/activate
  # glibc keeps a build's freed memory in one malloc arena per thread: two arenas keep the Studio near one build's needs
  export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
  python -m morph.cli studio egg-picker --open ${args[@]+"${args[@]}"}
}

# run main unless this file is sourced (tests/test_launchers.py sources it to call parse_args)
if ! (return 0 2>/dev/null); then
  main "$@"; exit $?
fi
