# Release checklist

For every tagged release (`vX.Y.Z`, or `vX.Y.ZbN` for a beta). Copy it into the release pull request and tick each
line. A release ships only when every line is ticked or has a written reason in the PR.

## Before tagging

- [ ] `morph/__init__.py` has the new version, and CHANGELOG.md has its section with every feature that ships (the
      placeholder lines replaced or removed).
- [ ] CI is green on the release commit: all 15 test cells (Windows, macOS, Linux; Python 3.10 to 3.14), lint, and
      the package job on all three systems.
- [ ] `python tools/check_constraints.py --extras` passes. If `constraints.txt` changed, the CI matrix ran on the new
      pins.
- [ ] `python -m pytest -q` passes locally with no keys set and the network off.
- [ ] `MORPH_E2E=1 python -m pytest tests/e2e` passes: the end-to-end browser tests
      ([INSTALL](INSTALL.md#for-developers) says what they need).
- [ ] [PRIVACY.md](PRIVACY.md) lists every network call the release makes. Check against the code: `urlopen`,
      `anthropic`, `subq` and `requests` calls, and any new service in Keys.
- [ ] [SECURITY.md](../SECURITY.md) still describes what the release protects, and
      [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) covers every vendored file (`static/vendor/`, `robots/`,
      `data/`).
- [ ] No secrets in the history: `git log -p --all | grep -nE "sk-ant-api|kwpat-|AIza"` finds only test fixtures.
- [ ] UI copy is honest: "estimated", "seeded demo data", "beta" and "not bench-validated" wherever they apply; no
      claim that an order was placed.
- [ ] `MORPH_SLOW=1 python -m pytest -q tests/test_robust.py tests/test_robust_room.py tests/test_world_scan.py
      tests/test_sim_perturb.py -k "acceptance or sweeps"` passes on the release commit: the published egg-picker study
      (150 of 200), the same runs on 1, 2 and 4 worker processes, the carton wall's collision rate, the million-triangle
      scan in under 20 s and the yaw sweeps. Put the numbers in the release notes.
- [ ] `tests/test_sim_golden.py` ran on the tagging machine instead of skipping. Its hashes are kept per environment: on
      a new one, record them with `MORPH_GOLDEN=record` on the previous release's commit, then run the test on the
      release commit (recording on the candidate itself proves nothing).
- [ ] Agent tools: with real keys, `MORPH_LIVE=1 python -m pytest tests/test_toolbox_live.py
      tests/test_toolbox_vendors_live.py tests/test_toolbox_draft_live.py` passes (a few cents of Claude usage), and
      each distributor manifest whose test passed has `verified.live` set to that date. A manifest nobody tried keeps
      `live: null`, so the Studio says "Not yet tried with a real key"; the release notes say which.

## Manual QA

Run each on a clean machine or a fresh user account, from a fresh clone of the release commit.

| # | Setup | Steps | Expected |
|---|---|---|---|
| 1 | Windows 11, Python 3.13 from python.org | Double-click `run_studio.bat`. Close it, start it again. | First run: Python found, pip progress, both robots build, app window opens. Second run: starts in seconds with no pip output. |
| 2 | Windows 11 with only Python 3.15 | Double-click `run_studio.bat`. | Clear message naming Python 3.10 to 3.14 and python.org; no `.venv` left behind. |
| 3 | Any OS | Delete `.venv/Scripts/activate.bat` (Windows) or `.venv/bin/activate`, then launch. | The launcher says it is making a new `.venv` and starts normally. |
| 4 | macOS on Apple silicon | `./run_studio.sh`; open the Studio in Chrome and in Safari. | 3D viewer, every tab, Present, Export download and Keys work in both browsers. |
| 5 | Ubuntu 24.04 desktop; then a server without `python3-venv` | `./run_studio.sh` on each. | Desktop: starts. Server: the launcher names the apt package to install; after installing it, it starts. |
| 6 | Two computers on one network | `run_studio --remote` on one; open the printed link on the other; then open the address without the code. | The link opens the Studio and the header names the host. Without the code: the sign-in page; a wrong code is refused. Keys saved from the laptop land in the host's `.env`. |
| 7 | Network unplugged after the first install | Launch, make edits, click Find agents, chat. | Everything builds; Darwin shows saved answers; chat uses the built-in rules. With `MORPH_OFFLINE=1` nothing leaves the machine (check with a firewall log). |
| 8 | Fresh demo | Present: Reset demo, Start, press → through all 14 steps. | Every step runs and its caption matches what is on screen. |
| 9 | Your own keys | Keys: save each key, Test keys; then save a wrong one. | Working keys say so; the wrong one is reported; `.env` is readable by you only (macOS, Linux); no key value is ever shown. |
| 10 | Teams preview | Create a team, invite a second browser, propose a change, review it. | Invite works once and expires; the review is routed to the right disciplines; revoking removes access. |
| 11 | Import | Import a URDF robot (zip or folder) and a mesh as an object. | The preview appears, the user confirms the gripper and actuators, and the pipeline builds. |
| 12 | Video | Upload a short phone clip; analyse with and without a Claude key. | Frames appear; with a key, editable suggestions; without one, the fields to fill in by hand. |
| 13 | World mode (beta) | Open a starter world, place the robot, walk through, simulate. | Runs on the CPU and WebGL; labelled beta. |
| 14 | `--update` | In a clone of the previous release run `--update`; repeat with an edited tracked file; run it in a zip download. | Switches to the new tag and starts it; refuses with local edits; explains the download route for a zip. |
| 15 | Two tabs, two projects | Open egg-picker in one tab and so101 in another; send two chat edits quickly in one; switch the other tab's project. | Each tab shows only its own project's events; the second edit waits ("queued") and then builds on top of the first; switching one tab never moves the other. |
| 16 | Robustness study (Windows, macOS and Linux) | Simulation tab, pick and place: run a 200-run study, cancel it part way, *Carry on*, replay a failing run, open Details. Start another and close the Studio's window (or end its process) mid-study; start the Studio again. | Live progress, then the result with its interval and "This is simulation, ..."; cancel stops within seconds and keeps the finished runs; the replay plays in the 3D view. After the restart the card says "Stopped when the Studio stopped after N of M runs" with those runs, and *Carry on* finishes it. No study process is left running (Task Manager, Activity Monitor or `ps`). |
| 17 | World mode with a real room scan | Upload a phone scan (.glb) of a bench, place the robot, *Save and simulate*, *Check placement*; *Add box here* on a near miss; *Save and simulate* again. | The scan check warns and never sets the verdict, naming each near miss; the added box appears; the next simulation plans around it. |
| 18 | Live prices with real keys: Anthropic, then Mouser | BOM tab, **Live prices (beta)**; add the Mouser key from the Key needed card. | The card says what the key is for, what is sent and to which host; after Save the run continues by itself; prices show "from Mouser Electronics" and two of them match mouser.com. The key's value appears nowhere: search `projects/.runs/`, `projects/.tools/` and the terminal output for it. |
| 19 | An element14 key | Keys, Tools agents can use: Farnell on, Mouser off; Live prices again. | Prices from Farnell. Its answers are kept only in the run's file, which the Studio deletes 7 days after the run last changed. |
| 20 | A wrong key | Save a wrong Mouser key; Live prices. | "Mouser Electronics refused the key" with Replace key, and replacing it continues the run. If the card shows a failed call instead, write down the status Mouser answered. |
| 21 | Runs that stop | Start Live prices, then turn off *Let agents use tools*; start another and quit the Studio while it works, then start it again. | "Agent tools were turned off" with Open Keys and Continue, which finishes once they are on; after the restart, "The Studio restarted while this ran" and Continue. |
| 22 | Draft a tool (preview) | On a need card, Draft a tool from a small read-only API's official documentation; approve; run again with its real key. | The review names each host with the documentation's line; the next run asks for the new key; the tool answers with the real key. |
| 23 | Teams and tools | Owner, editor and viewer browsers; the editor starts Live prices. | Only the owner adds keys and allows tools; the editor can skip; the viewer is told to ask the owner. |

## Tag and publish

- [ ] Merge the release pull request into `main`.
- [ ] Tag the merge commit and push the tag: `git tag -a v0.2.0b1 -m "Morph 0.2.0b1"`, then
      `git push origin v0.2.0b1`. Never move or delete a published tag: fix forward with a new version.
- [ ] Create the GitHub release from the tag with the CHANGELOG section as its notes. Tick *pre-release* for a beta.
      Optionally attach the wheel from `python -m build` with its SHA-256.
- [ ] In a fresh clone of `main`, `run_studio --update` lands on the new tag.
