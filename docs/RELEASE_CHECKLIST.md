# Release checklist

For every tagged release (`vX.Y.Z`, or `vX.Y.ZbN` for a beta). Copy it into the release pull request and tick each
line. A release ships only when every line is ticked or has a written reason in the PR.

## Decided for 0.2.0b1

The owner's decisions from the release review (W3), so the lines below aren't argued again in the release PR:

| Item | Decision | What it means for the release |
|---|---|---|
| REL-6: README's Keewano paragraph | **Keep** the accurate rewrite ("with event sending on, every design, bench test and human minute is logged, and with calibration on, logged results tune the next design's grip force. Both are off by default, and the R Cubed demo history in Keewano is seeded data, not real builds."). `origin/main`'s line ("recursive self-improvement", everything logged and tuning the next design) is not restored: it described both switches as always on. | Nothing to do. A README diff against `main` at that paragraph is intended. |
| REL-5: GitHub private vulnerability reporting | **The owner's switch.** Only the repository owner can turn it on; on 2026-10-15 `gh api repos/jim4226/A16z_Hackathon/private-vulnerability-reporting` still printed `{"enabled":false}`. Until then SECURITY.md's fallback applies: a public "Security contact" issue with no details. | The line under *Before tagging* stays unticked until the owner turns it on, or the release PR says in writing that 0.2.0b1 ships with the fallback only. |
| REL-10: Farnell (element14) tool | **Off by default until a legal review** of Farnell's API terms says Morph may keep its answers in run files and send them to Claude. The owner's switch in Keys turns it on for their own Studio. | `element14.json` keeps `off_by_default`; the release notes say the Farnell tool ships off (the line under *Before tagging*). |

## Before tagging

- [ ] `morph/__init__.py` has the new version, and CHANGELOG.md has its section with every feature that ships (the
      placeholder lines replaced or removed).
- [ ] CHANGELOG.md's heading for the release has the release date instead of "not yet tagged", for example
      `## 0.2.0b1 (beta, YYYY-MM-DD)` with the day of the tag.
- [ ] GitHub private vulnerability reporting is on, so SECURITY.md's *Report a vulnerability* link works:
      `gh api repos/jim4226/A16z_Hackathon/private-vulnerability-reporting` prints `{"enabled":true}` (the owner turns it
      on under Settings, Code security, or with `gh api -X PUT repos/jim4226/A16z_Hackathon/private-vulnerability-reporting`).
- [ ] CI is green on the release commit: all 15 test cells (Windows, macOS, Linux; Python 3.10 to 3.14), lint, the
      package job on all three systems, and the launchers job (both launchers run for real, the Windows one from
      Command Prompt and both PowerShells; on Windows also the upgrade from a launcher that pulled on every start, with LF
      and with CRLF line endings and `--update` before any release, and `--update` switching to a tag from a folder
      with `^` in its name, staying on it, and leaving a clone with newer commits alone).
- [ ] `python tools/check_constraints.py --extras` passes. If `constraints.txt` changed, the CI matrix ran on the new
      pins.
- [ ] `python -m pytest -q` passes locally with no keys set and the network off.
- [ ] With `MORPH_E2E=1` set, `python -m pytest tests/e2e` passes: the end-to-end browser tests
      ([INSTALL](INSTALL.md#for-developers) says what they need).
- [ ] [PRIVACY.md](PRIVACY.md) lists every network call the release makes. Check against the code: `urlopen`,
      `anthropic`, `subq` and `requests` calls, and any new service in Keys.
- [ ] [SECURITY.md](../SECURITY.md) still describes what the release protects, and
      [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) covers every vendored file (`static/vendor/`, `robots/`,
      `data/`).
- [ ] No secrets in the history: `git log --all -p -G "sk-[A-Za-z0-9_-]{20,}|kwpat-|AIza|ghp_|github_pat_|AKIA|xox[bp]-|(MORPH_TOOL_[A-Z0-9_]+|MORPH_TOKEN|[A-Z_]*API_KEY|[A-Z_]*_PAT)=[^ ]{8,}"`
      shows only tests, docs and the code that recognizes key formats, and none of the matching lines is a real key.
      Running [gitleaks](https://github.com/gitleaks/gitleaks) 8.19 or newer (`gitleaks git .`) as well is better still.
- [ ] UI copy is honest: "estimated", "seeded demo data", "beta" and "not bench-validated" wherever they apply; no
      claim that an order was placed.
- [ ] With `MORPH_SLOW=1` set, `python -m pytest -q tests/test_robust.py tests/test_robust_room.py
      tests/test_world_scan.py tests/test_sim_perturb.py -k "acceptance or sweeps"` passes on the release commit: the
      published egg-picker study (140 of 200), the same runs on 1, 2 and 4 worker processes, the carton wall's collision
      rate, the million-triangle scan in under 20 s and the yaw sweeps. Put the numbers in the release notes.
- [ ] `tests/test_sim_golden.py` ran on the tagging machine instead of skipping. Its hashes are kept per environment: on
      a new one, record them with `MORPH_GOLDEN=record` on the previous release's commit, then run the test on the
      release commit (recording on the candidate itself proves nothing).
- [ ] Agent tools: with real keys and `MORPH_LIVE=1` set, `python -m pytest tests/test_toolbox_live.py
      tests/test_toolbox_vendors_live.py tests/test_toolbox_draft_live.py` passes (a few cents of Claude usage), and
      each distributor manifest whose test passed has `verified.live` set to that date. A manifest nobody tried keeps
      `live: null`, so the Studio says "Not yet tried with a real key"; the release notes say which.
- [ ] Farnell (element14): until a legal review of its API terms says Morph may keep its answers in run files and send
      them to Claude, `morph/studio/toolbox/catalog/element14.json` keeps `off_by_default` (Keys shows it off, with why)
      and the release notes say the Farnell tool ships off.

## Manual QA

Run each on a clean machine or a fresh user account, from a fresh clone of the release commit.

| # | Setup | Steps | Expected |
|---|---|---|---|
| 1 | Windows 11, Python 3.13 from python.org | Double-click `run_studio.bat`. Close it, start it again. | First run: Python found, pip progress, both robots build, app window opens. Second run: starts in seconds with no pip output. |
| 2 | Windows 11 with only Python 3.15 | Double-click `run_studio.bat`. | Clear message naming Python 3.10 to 3.14 and python.org; no `.venv` left behind. |
| 3 | Any OS | Delete `.venv/Scripts/activate.bat` (Windows) or `.venv/bin/activate`, then launch. | The launcher says it is making a new `.venv` and starts normally. |
| 4 | macOS on Apple silicon | `./run_studio.sh`; open the Studio in Chrome and in Safari. | 3D viewer, every tab, Present, Export download and Keys work in both browsers. |
| 5 | Ubuntu 24.04 desktop; then a server without `python3-venv` | `./run_studio.sh` on each. | Desktop: starts. Server: the launcher names the apt package to install; after installing it, it starts. |
| 6 | Two computers on one network | `.\run_studio.bat --remote` (or `./run_studio.sh --remote`) on one; open the printed link on the other; then open the address without the code. | The link opens the Studio and the header names the host. Without the code: the sign-in page; a wrong code is refused. Keys saved from the laptop land in the host's `.env`. |
| 7 | Network unplugged after the first install | Launch, make edits, click Find agents, chat. | Everything builds; Darwin shows saved answers; chat uses the built-in rules. With `MORPH_OFFLINE=1` nothing leaves the machine (check with a firewall log). |
| 8 | Fresh demo | Present: Reset demo, Start, press → through all 14 steps. | Every step runs and its caption matches what is on screen. |
| 9 | Your own keys | Keys: save each key, Test keys; then save a wrong one. | Working keys say so; the wrong one is reported; `.env` is readable by you only (macOS, Linux); no key value is ever shown. |
| 10 | Teams preview | Create a team, invite a second browser, propose a change, review it. | Invite works once and expires; the review is routed to the right disciplines; revoking removes access. |
| 11 | Import | Import a URDF robot (zip or folder) and a mesh as an object. | The preview appears, the user confirms the gripper and actuators, and the pipeline builds. |
| 12 | Video | Upload a short phone clip; analyse with and without a Claude key. | Frames appear; with a key, editable suggestions; without one, the fields to fill in by hand. |
| 13 | World mode (beta) | Open a starter world, place the robot, walk through, simulate. | Runs on the CPU and WebGL; labelled beta. |
| 14 | `--update` (Windows, and macOS or Linux) | In a clone of `main`, `git checkout v0.2.0b1~1` (the commit before the new tag), then run `--update`; run it again; `git checkout -b after`, commit any change and run it a third time; repeat the first with an edited tracked file; run it in a zip download. | First: switches to the new tag and the Studio opens once, with no "is not recognized". Second: "Already on" the tag. Third: says the clone has newer commits and stays on `after`. Refuses with local edits; explains the download route for a zip. |
| 15 | Two tabs, two projects | Open egg-picker in one tab and so101 in another; send two chat edits quickly in one; switch the other tab's project. | Each tab shows only its own project's events; the second edit waits ("queued") and then builds on top of the first; switching one tab never moves the other. |
| 16 | Robustness study (Windows, macOS and Linux) | Simulation tab, pick and place: run a 200-run study, cancel it part way, *Carry on*, replay a failing run, open Details. Start another and close the Studio's window (or end its process) mid-study; start the Studio again. | Live progress, then the result with its interval and "This is simulation, ..."; cancel stops within seconds and keeps the finished runs; the replay plays in the 3D view. After the restart the card says "Stopped when the Studio stopped after N of M runs" with those runs, and *Carry on* finishes it. No study process is left running (Task Manager, Activity Monitor or `ps`). |
| 17 | World mode with a real room scan | Upload a phone scan (.glb) of a bench, place the robot, *Save and simulate*, *Check placement*; *Add box here* on a near miss; *Save and simulate* again. | The scan check warns and never sets the verdict, naming each near miss; the added box appears; the next simulation plans around it. |
| 18 | Live prices with real keys: Anthropic, then Mouser | BOM tab, **Live prices (beta)**; add the Mouser key from the Key needed card. | The card says what the key is for, what is sent and to which host; after Save the run continues by itself; prices show "from Mouser Electronics" and two of them match mouser.com. The key's value appears nowhere: search `projects/.runs/`, `projects/.tools/` and the terminal output for it. |
| 19 | An element14 key, only where your agreement with Farnell allows it | Keys, Tools agents can use: Farnell is off and says why; turn Farnell on and Mouser off; Live prices again. | Farnell's note names the legal review and what turning it on means. Prices read "from Farnell/element14" on the card, in Show prices and in Claude's summary. Its answers stay in the run's file, which the Studio deletes 7 days after the run last changed. |
| 20 | A wrong key | Save a wrong Mouser key; Live prices. | "Mouser Electronics refused the key" with Replace key, and replacing it continues the run. If the card shows a failed call instead, write down the status Mouser answered. |
| 21 | Runs that stop | Start Live prices, then turn off *Let agents use tools*; start another and quit the Studio while it works, then start it again. | "Agent tools were turned off" with Open Keys and Continue, which finishes once they are on; after the restart, "The Studio restarted while this ran" and Continue. |
| 22 | Draft a tool (preview) | On a need card, Draft a tool from a small read-only API's official documentation; approve; run again with its real key. | The review names each host with the documentation's line; the next run asks for the new key; the tool answers with the real key. |
| 23 | Teams and tools | Owner, editor and viewer browsers; the editor starts Live prices. | Only the owner adds keys and allows tools; the editor can skip; the viewer is told to ask the owner. |
| 24 | Upgrading a clone whose launcher pulled (Windows 11, Git for Windows with its default settings) | In a clone of `main` after the release is merged, `git reset --hard 0f75a21` (a launcher that pulled on every start), then double-click `run_studio.bat`. Close it; in Command Prompt run `.\run_studio.bat` followed by a space and a comment such as `# Windows (later, double-click run_studio.bat in Explorer)`. | First start: git pulls, then the new launcher carries on with no "is not recognized", "was unexpected at this time" or "The syntax of the command is incorrect"; the packages install with pip's progress and the Studio opens. Second start: no git output, "Ignoring the rest of the line from # on", and the Studio opens. |
| 25 | Upgrading a clone whose launcher pulled (macOS) | In a clone of `main` after the release is merged, `git reset --hard 0f75a21`, then `./run_studio.sh --update` in Terminal (zsh). Close it and run `./run_studio.sh` again. | First start: git pulls, pip runs quietly, then "--update is an option of the launcher" and the Studio opens. Second start: pip's progress once (the tested versions and the `morph` command), then the Studio. `.venv/bin/morph doctor` works afterwards. |
| 26 | An Intel Mac, or Windows 11 on ARM | `./run_studio.sh` (Mac); on Windows on ARM, double-click `run_studio.bat` with only the ARM64 Python installed, then again with the x64 Python from python.org. | Intel Mac and ARM64 Python: a message saying why and what to install, before any download, and no `.venv` folder left behind. x64 Python on ARM: the note about x64 emulation, then the Studio opens and builds; on Windows 11 before 24H2, the message that MuJoCo doesn't load or run and to update Windows (record the Windows version and which you saw, and that a pick-and-place build simulates after the check passes). |

Row 26 still needs real hardware: it can't be run in the release review, which has no Windows on ARM computer.
The launcher's check there (`run_studio.bat` stepping a MuJoCo model before it starts the Studio) is tested only
through the script's lines and its Python one-liners run on Linux (`tests/test_launchers.py`), and CI's launchers
job runs on x64 Windows, where that check is skipped. Run the row on a Windows 11 on ARM machine and record the
Windows version, which message appeared, and whether a pick-and-place build simulates after the check passes.

## Tag and publish

- [ ] Merge the release pull request into `main`.
- [ ] Tag the merge commit and push the tag: `git tag -a v0.2.0b1 -m "Morph 0.2.0b1"`, then
      `git push origin v0.2.0b1`. Never move or delete a published tag: fix forward with a new version.
- [ ] Create the GitHub release from the tag with the CHANGELOG section as its notes. Tick *pre-release* for a beta.
      Optionally attach the wheel from `python -m build` with its SHA-256.
- [ ] In a fresh clone of `main`, `.\run_studio.bat --update` (Windows) and `./run_studio.sh --update` say they are on
      the new tag (or, if `main` has moved past it, that the clone has newer commits).
- [ ] Tell teammates who follow a pre-release branch on Windows to close Morph and run `git pull` once before their
      first `--update`: the launchers from before this release's launcher fix stop after switching to the tag
      ([INSTALL](INSTALL.md#a-clone-made-before-v02)).
