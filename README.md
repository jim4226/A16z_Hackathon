# R Cubed

**R Cubed turns weeks of work by a dedicated robotics engineering team into one afternoon for a non-technical person.**

Every business and builder has tasks a robot could do, but a custom robot for one specific job means stitching together CAD, simulation, electronics, firmware and a dozen part suppliers, and any change to the design means modifying and redoing work across all of these subsystems. That loop takes skilled engineers days for a single gripper and weeks for a whole robot, so most of those tasks never get a robot at all.

R Cubed puts that whole loop in one place: tell it "pick up a raw egg without cracking it," and in about 25 seconds you have a printable gripper with force-sensing fingers, firmware that grips at 2.3 N when the egg cracks at about 25 N, a routed PCB ready to order, and about 90 minutes of print files. Swap the servo or change the object and only the affected parts rebuild while you watch.

Darwin handles purchasing and engineering help, finding agents that can quote and sell every part, fabricate the PCB and prints, and review the design.

Keewano gives R Cubed observability: with event sending on, every design, bench test and human minute is logged, and with calibration on, logged results tune the next design's grip force. Both are off by default, and the R Cubed demo history in Keewano is seeded data, not real builds.

## Run it

You need **Python 3.10 to 3.14** (3.13 recommended: [python.org](https://www.python.org/downloads/); on Windows
tick "Add python.exe to PATH"), **Git**, and for the app window **Chrome or Edge** (any browser works at
http://127.0.0.1:8765). Morph runs on Windows 10 and 11, macOS 12 or newer on Apple silicon, and 64-bit Linux.
Step by step, updating, uninstalling and fixes for common problems: [docs/INSTALL.md](docs/INSTALL.md).

```bash
git clone https://github.com/jim4226/A16z_Hackathon
cd A16z_Hackathon
run_studio.bat --update     # Windows (later, double-click run_studio.bat in Explorer)
./run_studio.sh --update    # macOS / Linux (later, ./run_studio.sh)
```

`--update` switches the clone to the latest release (the newest `v` tag) and starts it; use it whenever you want
to update. Without it, the launcher never runs git, and after the first install it starts offline. No Git?
Download *Source code (zip)* from the [latest release](https://github.com/jim4226/A16z_Hackathon/releases) and run
the launcher in the unzipped folder.

The first launch takes a few minutes. The launcher looks for Python 3.13, 3.12, 3.11, 3.10, then 3.14 (and says what
to install if none is there), creates `.venv`, and installs the tested package versions (`requirements.txt` with
`constraints.txt`), showing pip's progress. Then it **builds two robots** (the generated egg-picker arm and the
imported SO-101), printing each pipeline stage in the terminal, and the Morph Studio window opens when they are
ready. Later launches open in seconds (after an update that changes the pipeline, both robots are rebuilt once,
about two minutes).

**API keys are optional.** Everything runs without them. Click **Keys** (top right of the Studio) to
paste your own; they are saved only on your computer, in the git-ignored `.env` (created from
`env.example` on first launch), and take effect immediately. **Test keys** in the same dialog tries
each one, so a mistyped key shows up at once.

| Key | What it adds | Where to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | Free-form chat requests, Claude reading a **New robot** request, the frame analysis in **Show Morph the job** and World mode's AI review (without it, the suggestion chips, common phrasings and built-in rules work, and you fill in the video cards yourself) | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| `SUBQ_API_KEY` | SubQ Snap: quick yes/no and multiple-choice decisions, shown as "SubQ Snap (AI)" ([SubQ Snap](#subq-snap-optional)) | [console.subq.ai](https://console.subq.ai) |
| `KEEWANO_API_KEY`, `KEEWANO_PAT`, `KEEWANO_PROJECT_ID` | Build memory: revisions and measurements logged once event sending is on, and an optional grip-force calibration from that history (seeded demo data unless your project holds real bench results) | Keewano settings ([docs/SPONSORS.md](docs/SPONSORS.md)) |
| Darwin | Nothing to add: Darwin Search is public | |

**Privacy and network** (at the bottom of the Keys dialog) has three switches, saved in
`projects/.settings.json` (never any keys): *Allow network lookups (Darwin, SubQ, Claude)* is on; off keeps
everything on this computer. *Send build events to Keewano* and *Use Keewano calibration* are off until you
turn them on. `MORPH_OFFLINE=1` turns every network call off whatever the switches say. What each service
receives, and when anything leaves your computer: [docs/PRIVACY.md](docs/PRIVACY.md). To report a security problem:
[SECURITY.md](SECURITY.md).

**Present the demo:** click **Present** (or press `P`), then **Reset demo** and **Start**; press `→`
for each of the 14 steps. Script and tips: [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md).

**If something looks wrong or empty:**
- Run `python -m morph.cli doctor` (inside `.venv`: `.venv\Scripts\python -m morph.cli doctor` on Windows,
  `.venv/bin/python -m morph.cli doctor` elsewhere). It checks Python, packages, keys, network and
  whether every pipeline stage built, lists which features are ready (New robot, Import, Show Morph the job, Teams,
  World mode) with the optional tools they use, and says what to fix. Send its output to the team.
- A stage marked *error* in the pipeline strip leaves its tab empty; the other stages still work.
  Delete the `projects` folder and start again to rebuild everything from scratch.
- "Morph Studio is already running": close the other terminal window first.
- Packages broken or half-installed: delete the `.venv` folder and run the launcher again.
- The right panel (time card, grip force) folds away when the window is 1280 px wide or narrower, as at
  150 % Windows display scaling: the panel button at the top right opens it over the robot, or zoom out once
  (**Ctrl** and **-**, or **Cmd** and **-** on a Mac) to keep it beside the robot.

## Bring your own compute

Morph runs entirely on the computer that starts it. CAD, simulations, PCB routing, the BOM and the
manual are all computed there; only the optional services (Claude, Keewano, Darwin, SubQ) are called over
the network ([docs/PRIVACY.md](docs/PRIVACY.md)), and no GPU is needed. To do the work on a stronger computer
(a desktop, a workstation or a cloud VM) and use it from a laptop, start Morph on that computer with
`--remote` and open it in the laptop's browser:

```bash
run_studio.bat --remote         # Windows, on the stronger computer
./run_studio.sh --remote        # macOS / Linux, including a server without a screen
```

The terminal prints a private link with an access code, such as `http://192.168.1.20:8765/?token=...`.
Open it on the laptop. That browser stays signed in (for 30 days, or until Morph restarts with a new
code); anyone without the code gets a page asking for it. To keep the same link across restarts, put
`MORPH_TOKEN=` and a code of your choice (8 or more letters and digits) in `.env`. Keys added with the
**Keys** button are saved on the computer running Morph, and the header reads **Live on** that
computer's name, so it is clear which machine does the work. `python -m morph.cli doctor` shows each
machine's CPU cores.

The link is plain HTTP ([SECURITY.md](SECURITY.md)), so use it on a network you trust or over a VPN such as
Tailscale. From anywhere else, use an SSH tunnel, which needs no `--remote`: start Morph normally on the server, run
`ssh -L 8765:127.0.0.1:8765 you@server` on the laptop, and open http://127.0.0.1:8765. On a cloud VM,
keep port 8765 closed to the internet and use the tunnel.

## Morph Studio: the whole-robot platform

Morph Studio is R Cubed's platform for whole robots: a 5-axis arm with a force-sensing gripper, or an
existing open-source robot (Hugging Face's SO-101, imported from its own URDF). One request generates
the CAD, MuJoCo simulations, electronics (schematic, routed PCB, harness, firmware), manufacturing plan
(prototype, pilot and production), BOM with sourcing, and an assembly manual. Every chat edit
regenerates only what depends on it.

- **Tabs:** Robot (3D, motion, X-ray, explode), Simulation (pick and place, payload, workspace,
  shake), Electronics, Manufacturing, Assembly, and BOM & Sourcing (parts, bulk materials with buy
  links, and the **Darwin agents** view, which finds an agent for every part and fabrication job).
- **Inspector:** the time-saved card (cited by-hand hours vs measured time), metric deltas, the
  grip-force window (with the optional Keewano calibration), and issues.
- **Export** (next to the revision number above the tabs): the revision as a ZIP of its CAD,
  simulations, electronics and firmware, manufacturing files and manual, with a manifest of which stages
  completed and the firmware marked unverified. Keys, settings, chat and the ledger are never included.
  From a terminal: `morph export egg-picker -o egg-picker.zip` (`--rev r003` for an older revision, `--full` to add
  every revision's design and results).
- **Projects** (the menu next to the logo): each browser tab opens its own project (`?project=` in its
  address), so switching one tab moves no other tab and nobody else. Chat and parameter edits wait their turn
  in each project's queue, and each project keeps its chat.
- **Present** (button or `P`): the demo as 14 press-through steps with captions.
- More: [docs/SPONSORS.md](docs/SPONSORS.md) (Keewano and Darwin), [docs/TIME_SAVED.md](docs/TIME_SAVED.md)
  (time-saved sources), [docs/internal/PLATFORM_CONTRACTS.md](docs/internal/PLATFORM_CONTRACTS.md) (how the stages fit
  together), [CHANGELOG.md](CHANGELOG.md) (what changed in each release), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

### New robot

Describe a job in one sentence: **New robot** (top right, or in the project menu), then something like "help me sort
socks". Morph asks up to three rounds of quick questions with chip answers (or takes its defaults when you skip
them), shows every assumption and anything it adjusted to what it can build, checks the design in about 30 ms and
builds the whole robot as a new project, with a timeline of each step. Requests it can't build get a plain reason
and the nearest robot it can build; harmful requests are refused.

Typing a new job into an open project's chat ("a robot that waters my plants") doesn't rebuild that robot: Morph
offers **Start a new robot from this**, or **Change egg-picker instead** if you did mean the open robot.

```bash
morph robot new "help me sort socks"                           # the same questions, in the terminal
morph robot new "help me sort socks" --defaults --name socks   # no questions: Morph's defaults
```

Morph's own code picks the questions and checks the design. With keys, SubQ Snap takes the first look at the request
and Claude reads it and your answers; without them, built-in rules do both, and the build is the same pipeline.

### Import

**Import** (top right, or the project menu) brings in what you already have: a robot as a folder or zip of URDF,
xacro or MuJoCo XML files, or a GitHub or Hugging Face link, built as a new project; the object the gripper should
hold, as an STL, OBJ or GLB mesh; a parts list; a KiCad board; or a Morph export. You can also drop the files
anywhere on the page. Morph asks only what the files can't tell, usually which motors drive the joints (pick one
motor family and Morph sizes every joint for its worst pose, on one supply voltage), and says why when an imported
robot builds but doesn't pass its own checks.

```bash
morph import robots/my-arm --family smart_bus                      # a folder, a zip, a URDF or a link
morph import cup.stl --as object --project egg-picker --units mm   # the object of an existing project
```

Formats, limits, safety and how imported arms are sized: [docs/IMPORT.md](docs/IMPORT.md).

### Show Morph the job

**Show Morph the job** (under the chat box) takes a phone video or photos of the task, done by hand. Morph picks
still frames in your browser, works out the object, the pick and place spots, the cycle time and safety needs, and
lists every design change for you to check before **Apply**. The frames go to Claude only with an Anthropic key and
after you agree; without a key (or with network lookups off), you fill in the same cards yourself, with a measuring
tool on the frames. Dropping a video or photos on the page opens it too. Clips your browser can't play (often iPhone
HEVC) need ffmpeg on the computer running Morph (install it, or `pip install morph[video]`; `morph doctor` says
whether it found one).

### Teams (preview)

One Studio, shared by a team ("GitHub for physical things"): teammates sign in from their own browsers with an
invite link, comment on revisions, propose changes that go to the people who own the stages they touch, approve them
per discipline and release revisions for building. A change request shows what it would change in numbers before
anything builds (torque margins, servos, wiring and firmware, cost), and above prototype nobody approves their own
work. It runs on the owner's computer, with no Morph server or account.

```bash
morph team init --owner "Jim" --name "R Cubed"   # once
morph studio --team --remote                     # or: ./run_studio.sh --team --remote
```

Roles, disciplines, reviews, releases and the security notes: [docs/TEAMS.md](docs/TEAMS.md).

### World mode (beta)

**World** (in the Robot tab's toolbar, over the robot) puts the robot in a room: a built-in desk, kitchen counter or
workbench, or your own room scan as a `.glb`. Place the robot on a surface, mark walls and furniture as grey boxes
and where people walk or sit, and save: the pick and place is simulated again in that room (no other stage reruns),
and **Check placement** gives a measured verdict. The simulation sees only the grey boxes, and it is not a motion
planner.

```bash
morph world egg-picker --starter desk     # or --off for the plain table again
```

Limits, the optional AI review and what it sends: [docs/WORLD.md](docs/WORLD.md).

### SubQ Snap (optional)

With a SubQ key (Keys, `SUBQ_API_KEY`), SubQ Snap makes quick yes/no and multiple-choice decisions, labelled
"SubQ Snap (AI)": the first look at a New robot request, whether a chat message asks for a new robot when its wording
alone can't tell, an optional second opinion in Show Morph the job, and an optional check in World mode. It is only
called with your key and network lookups on, answers are cached on your computer for 7 days, and every decision has
an offline fallback. `morph subq "help me sort socks"` tries a key by hand.

### Live prices and agent tools (beta)

**Live prices** in the BOM tab has Claude check current distributor prices and stock for the purchased parts with
Morph's read-only distributor tools (Mouser; Farnell, element14 and Newark). When a tool needs your key or your OK, the
run asks in the chat and carries on once you answer; each key goes only to its own service, never to Claude. A price is
kept only with the answer it came from, and nothing is ever ordered. The distributor tools were written from their
providers' documentation and are not yet tried with real keys. The owner can add tools as JSON manifests, or have
Claude draft one from an API's documentation (preview): [docs/TOOLS.md](docs/TOOLS.md).

## The gripper-only prototype (command line)

Before the whole-robot platform, R Cubed started as a command-line pipeline for one force-sensing
gripper. It still works and produces an HTML report rather than opening in the Studio:

```bash
python -m morph.cli demo           # ~25 s -> projects/raw-egg/report.html (open it in a browser)
```

To see a finished run without installing, open the committed example at `examples/raw-egg/report.html`.

## Live hardware loop

```bash
morph new egg --need "pick up a raw egg (44 mm wide, 57 mm long, 60 g) on its side without cracking it, MG996R gripper"
morph explore egg                                   # pad styles × sections, simulated, ranked
# print  projects/egg/r00X/fab/plate.stl            (finger + pad, ×2)
# flash  projects/egg/r00X/electronics/morph_grip/morph_grip.ino   (Arduino IDE; set OPEN_DEG/CLOSED_DEG)
morph bench egg r00X --port COM5                    # live force bar in the terminal; trace + result logged
morph bench egg r00X --simulate                     # rehearsal without hardware (labelled SIMULATED everywhere)
morph clock egg test 12                             # log human minutes so the time claim counts them
morph time egg r00X                                 # before/after time study
```

Other commands:

```bash
morph change egg electronics.servo=sg90             # rebuilds the affected artifacts
morph change egg object.max_force_n=3               # e.g. a strawberry; window and firmware update
morph compare egg r001 r004 r005
morph send egg r00X --grbl COM3                     # CNC/laser parts (non-sensor fingers)
morph research "FSR 402 vs load cell for a hobby gripper"
```

Run `pip install -e .` to get the `morph` command, or use `python -m morph.cli …`.
`new` and `research` use Claude when `ANTHROPIC_API_KEY` is set; everything else runs offline.

## Modules

| module | job |
|---|---|
| `mechanism.py` | printable rack-and-pinion parallel gripper sized to the servo and object: involute pinion, slotted racks, back plate with servo/guide/arm holes, jaw travel, servo angles |
| `board.py` | carrier board: real module pinouts, placement, two-layer maze router, GND pour, DRC, Gerbers + drill, perfboard-ready |
| `kicad.py` | the same board as a KiCad 8/9 project (PCB, schematic, project file) + a BoardRepo-ready zip; pure Python, `kicad-cli` DRC/ERC when installed |
| `boardrepo.py` | checked open reference boards on BoardRepo (gripper carrier, servo bus, PWM, load cell), read live from their public pages |
| `studio/` | Morph Studio web app (stdlib server + SSE, three.js viewer, PCB build animation, chat → changes) |
| `spec.py` | one `Design` (object, finger, gripper, electronics, fab); changes are dotted keys |
| `design.py` | 2.5D profiles → watertight solids. Pads: flat, V, or contour. With `finger.sensor=fsr402`, a two-part finger: a dovetail socket plus a sliding pad whose tail presses on the FSR |
| `analysis.py` | grip-force budget, cantilever deflection and stress, print derate |
| `sim.py` | MuJoCo close → lift → shake over randomized trials, plus a bisection for the minimum holding force |
| `electronics.py` | grip-force window, servo choice and stall-force check, divider sizing and resolution, supply and wiring, BOM, generated firmware |
| `bench.py` | serial driver for the firmware, live terminal bar, CSV trace, measured-vs-predicted |
| `explore.py` | closed-form screening, then simulate the best of each pad style |
| `fab.py` | DFM, print plate, laser SVG, GRBL G-code, time and cost |
| `pipeline.py` | dependency graph, partial recompute, revisions, ledger |
| `timestudy.py` | before/after time study (auto-logged tool time, logged human minutes, overridable baseline) |
| `report.py` | the comparison page |
| `agent.py` | Claude need → spec (fragility, sensor, servo inferred), web research |

## Sponsor integrations

**Keewano: the build memory.** With **Send build events to Keewano** on (Keys, Privacy and network;
off by default), every ledger entry (revision, bench measurement, human minutes, Darwin lookup) is
sent to Keewano as typed events from `Project.log()` (`morph/keewano.py`). With **Use Keewano
calibration** on (also off by default), `electronics.plan()` asks Keewano how much more holding force
logged builds with the same pad and material needed than the simulation predicted, and scales the
target by that ratio: only from 3 or more builds, and never from `synthetic-*` projects. The ratio is
only as real as the project's history; the R Cubed demo project is seeded. With calibration off, the
firmware ships the simulation-based number.

```bash
export KEEWANO_API_KEY=...                       # send (Settings -> Project -> API Keys)
export KEEWANO_PAT=kwpat-... KEEWANO_PROJECT_ID=<uuid>   # query (Settings -> Personal -> Security)
python tools/keewano_backfill.py --send          # 2 weeks of synthetic-* builds for dashboards (calibration ignores them)
morph ask 'SELECT "PadStyle", avg("HoldForceRatioPct") FROM events GROUP BY "PadStyle"'
```

**Darwin: who sells the parts and builds the rest.** R Cubed designs and programs; `morph source
<project> [rev]` asks Darwin Search for an agent per purchased BOM line plus the engineering
handoffs (PCB fab from the Gerbers, printing the STLs, a design review), writes
`<rev>/sourcing_darwin.json`, and logs every lookup, including gaps, to Keewano. It stops at a
shortlist: ordering is Darwin Act, which needs the user's own account and approval.

With no keys set, both integrations are no-ops and everything runs offline.

**BoardRepo: the board in KiCad, and open boards to learn from** (not a sponsor). Every carrier
board is also written as a KiCad project next to its Gerbers (`electronics/board/kicad/`), with
the same copper, the GND pour included. KiCad 9 reports 0 DRC violations, 0 unconnected items, 0 PCB-vs-schematic
mismatches and 0 ERC errors for every MCU. Darwin's PCB-fab handoff includes it. `morph kicad`
packages a revision for [BoardRepo](https://boardrepo.com), which takes a zipped KiCad project and
renders schematic, PCB, 3D and BOM. The README inside the zip lists related open designs.

```bash
morph kicad raw-egg r004      # projects/raw-egg/r004/boardrepo/raw-egg-r004/*-kicad.zip + upload steps
morph boardrepo               # reference boards with live licence and views
```

Uploading needs your own BoardRepo sign-in, so R Cubed stops at the zip. More in `docs/BOARDREPO.md`.

## Honest limits

- The FSR is calibrated with one known mass (`c<grams>` over serial). Its absolute accuracy is about ±10–25%, so the window margins absorb that error.
- The egg damage force and servo stall torques are typical values. Check them with a kitchen scale.
- The sim treats fingers as rigid and models the egg as a capsule.
- The firmware assumes a position servo and a 0.5° step. Set `OPEN_DEG`/`CLOSED_DEG` for your gripper.

Demo script (Present mode): `docs/DEMO_SCRIPT.md`. The original hackathon plan, from the gripper prototype: `docs/internal/HACKATHON_PLAN.md`. Data sources: `docs/DATA_SOURCES.md`.
