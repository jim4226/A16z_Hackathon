# R Cubed local beta

R Cubed opens in your browser and runs CAD, simulation and project storage on your own computer.
This is a **local beta**, not a hosted multi-user service. The downloadable setup bundle contains
the application and assets, but **does not bundle Python**. Internet access is needed during setup
to download the pinned dependencies. Later launches do not pull Git or reinstall dependencies.

## Install on Windows

1. Install 64-bit Python 3.11 from [python.org](https://www.python.org/downloads/windows/), including
   the Python launcher. Python 3.10 and 3.12 are also supported by the package; the double-click
   Windows setup uses 3.11.
2. Download the versioned `r-cubed-0.2.0b1-local-setup.zip` release asset and extract the entire folder
   somewhere you can write, such as Documents. Do not run from inside the ZIP viewer.
3. Double-click `run_studio.bat`. On first run it creates a private environment, installs the locked
   dependencies and this application version, and opens the local Studio. Keep its terminal open.
4. Connect your own Claude API key in Settings, or use the available offline operations.
5. Close the terminal to stop the local server. Double-click the same launcher to return.

If setup is interrupted, run `setup_studio.bat` to retry. With another supported Python version use
`py -3.12 tools/install_local.py --setup`, then launch normally. The installer never changes your
system Python packages. Windows may ask before opening downloaded scripts.

## Linux and macOS source/bundle setup

With Python 3.10-3.12 installed, run `bash setup_studio.sh`, then `bash run_studio.sh`.
Set `RCUBED_PYTHON=python3.11` to select an interpreter. Linux needs an active desktop session and
Secret Service for persistent key storage; session-only credentials are available when the system
store is unavailable. Headless rendering needs an OpenGL implementation such as OSMesa.

The CI matrix covers Windows and Linux on Python 3.10, 3.11 and 3.12. macOS has a launcher and
compatible dependency markers, but must be tested on a Mac before advertising it as a verified
release platform. Installing a Python version outside this beta range is rejected clearly.

## Projects, privacy and updates

Projects live in your per-user R Cubed application data directory, separate from the installation.
Settings holds connection status and non-secret preferences; persistent API keys use the operating
system credential store. Projects, exports and setup bundles do not contain keys. Optional supplier
lookups and telemetry have separate controls. Offline mode disables provider access.

To update, download and extract the next version in a new folder, then run its launcher. Your
per-user projects remain available. To remove the application, delete its extracted folder. Your
projects and saved credentials remain until you explicitly remove them; remove the API key in
Settings before uninstalling if you do not want it kept.

There is no automatic updater, signed native installer or bundled Python runtime yet. This release
does not claim physical validation of generated designs. Firmware stub syntax tests are not a
compile against the real Arduino board cores and libraries; review generated firmware before use.

## Developer/release checks

Run these commands from a source checkout or extracted source archive, not the setup ZIP.

```sh
python -m venv .venv
# Activate .venv for your shell.
python -m pip install --require-hashes -r requirements.lock
python tools/run_offline_tests.py -q
python tools/build_release.py
```

`build_release.py` builds an sdist, a wheel, a setup ZIP and SHA256SUMS; it does not upload or publish.
The wheel includes Studio, its vendored UI, SO-101 URDF/STL sources and notices, baseline data and
cached sourcing data. The source archive can rebuild the wheel without a Git checkout.

Before publishing, install the wheel into a fresh environment with the same lock and run
`python -I tools/package_smoke.py`. This changes to a temporary directory, blocks network access,
imports the installed modules and checks UI assets, robot meshes, CAD and offline cache use.
CI performs this isolated package check as well as the offline test suite.

To intentionally refresh the dependency lock, use the command in its header (`uv pip compile
pyproject.toml --all-extras --universal --python-version 3.10 --generate-hashes --output-file
requirements.lock`) and rerun the full matrix. The lock includes transitive dependencies and hashes
with platform/Python markers. Runtime setup uses pip; users do not need uv or Git.
