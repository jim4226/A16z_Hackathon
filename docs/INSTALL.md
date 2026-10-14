# Installing Morph Studio

Morph Studio runs on your own computer and opens in your browser. It works on:

- **Windows 10 or 11**, 64-bit (x64).
- **macOS 12 or newer on Apple silicon** (M1 or later). Intel Macs aren't supported: MuJoCo, the physics engine,
  no longer publishes builds for them.
- **Linux x64** with glibc 2.28 or newer: Ubuntu 22.04, Debian 12, Fedora 36 and later come with a new enough
  Python; older ones work once a newer Python is installed.

You need **Python 3.10, 3.11, 3.12, 3.13 or 3.14** (3.13 recommended; 3.15 is too new for now), about 600 MB of
disk to start (the packages and the two demo robots; each new revision adds 20 to 60 MB), and a browser. The app
window uses Chrome or Edge when it finds them; any browser works at http://127.0.0.1:8765. No GPU is needed. API
keys are optional.

## 1. Install Python

- **Windows:** the Python 3.13 installer from [python.org](https://www.python.org/downloads/windows/). Keep the
  default options (they include the `py` launcher, which `run_studio.bat` uses) and tick *Add python.exe to PATH*.
- **macOS:** the Python 3.13 installer from [python.org](https://www.python.org/downloads/macos/), or
  `brew install python@3.13`. The `python3` that comes with Apple's command line tools is 3.9, too old for Morph.
- **Linux:** your distribution's Python if it is 3.10 to 3.14, with its venv module. On Debian and Ubuntu:
  `sudo apt install python3 python3-venv`.

## 2. Get Morph

**With Git** (updates are one command), in a terminal (Command Prompt or PowerShell on Windows):

```shell
git clone https://github.com/jim4226/A16z_Hackathon
cd A16z_Hackathon
```

**Without Git:** download *Source code (zip)* of the latest release from the
[releases page](https://github.com/jim4226/A16z_Hackathon/releases) and unzip it into a normal folder, such as
Documents (not inside the zip viewer, and not a read-only or cloud-synced folder).

## 3. Start it

- **Windows:** double-click `run_studio.bat`. In a git clone you can run it from Command Prompt or PowerShell
  instead, to switch to the latest release first:

  ```bat
  .\run_studio.bat --update
  ```

- **macOS and Linux:**

  ```sh
  ./run_studio.sh --update
  ```

  or `./run_studio.sh` without `--update`. If you get *permission denied*, run `bash run_studio.sh`.

Paste commands from these docs as they are. If one comes with a comment (`#` and words after it, as older versions of
this guide had), the launchers stop reading options at the `#` and say so: Command Prompt, and zsh on a Mac, would
otherwise hand the comment's words to Morph as options.

The first launch:

1. finds a supported Python (Windows tries `py -3.13`, `-3.12`, `-3.11`, `-3.10`, `-3.14`, then `python`; macOS and
   Linux try `python3.13` down to `python3.10`, then `python3.14` and `python3`) and says what to install if there
   is none;
2. creates `.venv` in the Morph folder and installs the packages, showing pip's progress (a few minutes, depending on
   the connection);
3. builds the two demo robots (the generated egg-picker arm and the imported SO-101), printing each stage, which takes
   about a minute;
4. opens the Studio.

Later launches start in seconds and use neither git nor the network. Keep the terminal window open while you work:
closing it stops Morph. Other options are passed on to the Studio, for example `--remote` (see
[Bring your own compute](../README.md#bring-your-own-compute)) or `--port 8766`.

## 4. Add keys (optional)

Everything runs without keys. Click **Keys** in the Studio to add your own; they are saved in `.env` in the Morph
folder and take effect at once. What each key adds, and what it sends where: [README](../README.md#run-it) and
[PRIVACY.md](PRIVACY.md).

## Updating

- **Git clone:** `.\run_studio.bat --update` (Windows) or `./run_studio.sh --update` fetches the release tags from
  GitHub, switches to the newest release (a `v` tag such as `v0.2.0b1`) and starts it. It won't switch if you changed
  files that belong to Morph (`git status` lists them). Your `projects` folder, `.env` and `.venv` stay as they are. The
  launcher reinstalls packages only if the release changed them, and both demo robots are rebuilt once if the
  pipeline changed.
- **Zip download:** download the new release into a new folder, copy your `projects` folder and `.env` across, and
  start it there.
- **On a branch, or before the first release:** until there is a `v` tag, `--update` says there is no release yet and
  starts the version you have. To get the newest code of the branch your clone is on (a teammate's branch, or `main`
  after `git checkout main`), run `git pull`, then start the launcher as usual; it reinstalls packages only if
  `requirements.txt` or `constraints.txt` changed.
- Morph never updates itself when it starts.

### A clone made before v0.2

Launchers before v0.2 ran `git pull` every time they started. The first start after this version reaches the branch
such a clone follows (`main`, once this release is merged) pulls it one last time:

- **Windows:** the pull replaces `run_studio.bat` while it runs, and the new launcher takes over where the old one
  stopped, with the options you gave it. It installs the tested packages (a few minutes, once) and starts the Studio.
- **macOS and Linux:** the old launcher finishes that start itself, as it was (bash keeps running the file it opened);
  the next start uses the new one, which installs the tested package versions once.

From then on the launcher never pulls: use `--update` to switch to each new release, or `git pull` on a branch, as
above.

## Uninstalling

1. Delete the Morph folder. Your projects, `.env` and `.venv` are inside it, so copy out any project you want to keep.
2. Delete the cache of saved answers: `~/.cache/morph` (on Windows, `%USERPROFILE%\.cache\morph`).
3. If you no longer use your API keys, revoke them with each provider (Anthropic, Keewano, SubQ).

If you installed Morph as a package with pip instead, run `pip uninstall morph` and delete its settings folder
(`%LOCALAPPDATA%\Morph`, `~/Library/Application Support/Morph` or `~/.config/morph`).

## When something goes wrong

- **"Morph needs Python 3.10, 3.11, 3.12, 3.13 or 3.14":** install Python 3.13 (step 1) and run the launcher again.
- **"Python couldn't create .venv"** (Debian, Ubuntu): install the venv package the message names, such as
  `sudo apt install python3.12-venv`, then run the launcher again.
- **Installing the packages failed:** check the internet connection (behind a proxy, set `HTTPS_PROXY`), then run
  the launcher again. To start from scratch, delete the `.venv` folder.
- **"Morph Studio is already running":** close the other terminal window, or start this one with `--port 8766`.
- **Anything else:** run `.venv\Scripts\python -m morph.cli doctor` (Windows) or `.venv/bin/python -m morph.cli
  doctor`. It checks Python, the packages, keys, the network and whether every stage built, and says what to fix.

## For developers

```shell
python -m pip install -e ".[dev]" -c constraints.txt
python -m pytest -q
```

Tests run offline and without keys. `constraints.txt` pins the tested version of every package for each supported
Python; `python tools/check_constraints.py --extras` checks that all of them install from wheels on Windows, macOS
and Linux. `python -m build` makes a wheel that carries the robots, the Darwin cache and the licences;
`python -I tools/package_smoke.py`, run with the Python the wheel is installed into, checks it. CI runs all of this:
[.github/workflows/ci.yml](../.github/workflows/ci.yml).

The end-to-end browser tests in `tests/e2e/` are skipped unless you ask for them. They start a real Studio (offline,
no keys, full simulations) and drive Chromium through New robot, Import, Show Morph the job, a three-person Teams change
request and World mode, so they take about 20 minutes on 4 cores and need Playwright and, for the video test, ffmpeg:

```shell
python -m pip install playwright
python -m playwright install chromium
```

Then run them with `MORPH_E2E=1` set. On macOS and Linux:

```sh
MORPH_E2E=1 python -m pytest tests/e2e -v
```

In PowerShell:

```powershell
$env:MORPH_E2E = "1"
python -m pytest tests/e2e -v
```

In Command Prompt:

```cmd
set MORPH_E2E=1
python -m pytest tests/e2e -v
```

`MORPH_E2E_SHOTS=<folder>` keeps screenshots of every step, and `MORPH_E2E_DEMO=<folder>` (New robot, Import and video)
and `MORPH_E2E_WORLD_HOME=<folder>` (World mode) keep the demo robots they build between runs.
`MORPH_E2E_PORTS=8790-8799` keeps every Studio they start on those ports; with `-n 4` each test process takes every
fourth one, so give each at least two.
