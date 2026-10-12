# R Cubed

**R Cubed turns weeks of work by a dedicated robotics engineering team into one afternoon for a non-technical person.**

Every business and builder has tasks a robot could do, but a custom robot for one specific job means stitching together CAD, simulation, electronics, firmware and a dozen part suppliers, and any change to the design means modifying and redoing work across all of these subsystems. That loop takes skilled engineers days for a single gripper and weeks for a whole robot, so most of those tasks never get a robot at all.

R Cubed puts that whole loop in one place: tell it "pick up a raw egg without cracking it," and in about 25 seconds you have a printable gripper with force-sensing fingers, firmware that grips at 2.3 N when the egg cracks at about 25 N, a routed PCB ready to order, and about 90 minutes of print files. Swap the servo or change the object and only the affected parts rebuild while you watch.

Darwin handles purchasing and engineering help, finding agents that can quote and sell every part, fabricate the PCB and prints, and review the design.

Keewano gives R Cubed observability and recursive self-improvement: every design, bench test and human minute is logged, and the measured results tune the next design's grip force.

## Local beta: 0.2.0b1

The opening example describes the original hackathon demonstration and product direction. This beta
is a **local engineering preview** for generated arms and the SO-101 open-source robot. Automatic
calibration from remote build history is disabled: demo measurements are not physical validation.
Generated firmware has not been verified against real board cores or on a physical robot.

R Cubed Studio connects CAD, simulation, electronics, manufacturing, sourcing and assembly in one
browser interface. Its Python backend runs on your computer. Your projects and credentials are
separate from the installation, and no R Cubed account is required.

## Install and open

This beta requires **Python 3.10–3.12**; Python 3.11 is the recommended Windows setup. The initial
installation needs internet access and can take several minutes. Git is not needed for the setup ZIP.

1. Extract a versioned `r-cubed-0.2.0b1-local-setup.zip` built by the release workflow.
2. On Windows, double-click `run_studio.bat`. On Linux, run `bash run_studio.sh`.
3. Choose **New project**, name it, and start with the generated arm or SO-101. Existing projects can
   be copied from the same dialog. Creating an example does not require an API key.
4. Edit parameters or chat, inspect changed stages, then choose **Export revision** in the inspector.
5. Keep the terminal open while using Studio. Close it to stop; reopen the launcher to resume.

Launchers install this version once. They do not pull Git changes or update dependencies on each
launch. See [installation, troubleshooting and updates](docs/INSTALL.md). This branch provides the
build tooling; a signed installer, bundled Python runtime and verified macOS release are not included.

## Connect your own Claude key

Open **Settings** to enter your key, select a Claude model ID, and test the connection. Store the key
in your system credential store, or choose **Until this app closes**. Replace or remove it there.
Never paste a key into design chat. The app does not store keys in browser storage, projects or exports.

The local backend calls Anthropic directly; R Cubed's servers never receive your key. Anthropic
receives the request and design context and bills your account. Without a connection, simple requests
and parameter editing use offline rules. Provider errors clearly identify that fallback.

**Network and data sharing** are separate Settings controls:

- **Network access:** turn off for local-only operations. `MORPH_OFFLINE=1` also enforces this.
- **Darwin sourcing:** off by default; enable it, then choose Find agents to send component and
  fabrication queries. Files are not uploaded and nothing is purchased automatically.
- **Keewano events:** off by default; an optional developer integration using your own Keewano
  credentials. Enabling events does not enable force calibration.

Read [privacy and data flow](docs/PRIVACY.md) and [integration setup](docs/SPONSORS.md).

## What the beta includes

- **Robot:** generated arm or SO-101 model, motion, X-ray and exploded views.
- **Simulation:** pick-and-place, payload, workspace and shake scenarios.
- **Electronics:** schematic, harness, controller plan, PCB artifacts and generated firmware.
- **Manufacturing:** prototype/pilot/production estimates and supported fabrication artifacts.
- **Assembly and sourcing:** an assembly guide, BOM and optional Darwin lookups.
- **Revision exports:** a ZIP of generated artifacts plus a manifest with stage status, issues and
  firmware-validation status. Failed or missing required artifacts remain visible across revisions.

The Studio accepts loopback connections only. It is not a multi-user hosted service. Team accounts,
private shared workspaces and hardware pull-request approvals are future work.

Presentation controls are opt-in: `python -m morph.cli studio --demo --open`. A fixture preview
(`?fixture=1`) replays precomputed examples; its edits do not run CAD, simulation or fabrication.

## Develop and verify

```bash
python -m venv .venv
# Activate .venv for your shell.
python -m pip install --require-hashes -r requirements.lock
python -m morph.cli studio --open
python tools/run_offline_tests.py -q
python tools/build_release.py
```

Project files default to the current user's R Cubed application-data directory. Set `MORPH_HOME`
to select another project directory; existing source-checkout users can point it at their old
`projects` folder. `MORPH_CONFIG_DIR` overrides non-secret preferences. Existing `morph` commands
remain available; `rcubed` is an additional command alias after installation.

For developer-only environment configuration, copy `env.example` to `.env` and explicitly set
`MORPH_LOAD_DOTENV=1`. Keys already set in the environment take precedence. End users should use
Settings rather than editing `.env`.

The legacy gripper slice remains available with `python -m morph.cli demo`, which writes a report;
Studio's supported workflow is the whole-robot pipeline. KiCad export and BoardRepo references are
documented in [docs/BOARDREPO.md](docs/BOARDREPO.md).

## Validation and release notes

See [CHANGELOG.md](CHANGELOG.md), [release gates](docs/RELEASE_CHECKLIST.md), and
[security reporting](SECURITY.md). The build workflow tests Python 3.10–3.12 on Windows/Linux and
checks an installed wheel outside the checkout. Remote CI and platform results must be checked for
the exact commit before publishing.

Simulation results and software checks do not establish physical safety. Force estimates, FSR
calibration, fabrication tolerances and real firmware need review on the intended hardware.
Time-saving estimates and historical demo material remain in [docs/TIME_SAVED.md](docs/TIME_SAVED.md),
[docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md), and [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).
