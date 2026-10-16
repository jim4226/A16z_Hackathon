# Changelog

Morph Studio by R Cubed. Releases are git tags (`v0.2.0b1`); the launchers' `--update` (`.\run_studio.bat --update` on
Windows, `./run_studio.sh --update` on macOS and Linux) switches a clone to the newest one. Versions follow
[PEP 440](https://peps.python.org/pep-0440/): `b1` is a beta.

## 0.2.0b1 (beta, not yet tagged)

The first tagged release. Generated designs, wiring and firmware are not bench-validated.

### Features (beta)

- **New robot** from a one-line request: describe the job in one sentence; Morph asks up to three rounds of chip
  questions or uses sensible defaults, shows every assumption, checks the design in about 30 ms and builds the whole
  robot as a new project. Requests Morph can't build get a plain reason and the nearest robot it can build; harmful
  requests are refused, and nothing is sent anywhere when the local filter catches them. The agents timeline labels
  each step (SubQ Snap, Claude, Morph's own checks, Darwin, Keewano); without keys everything runs on built-in rules.
  Typing a new job into an open project's chat ("a robot that waters my plants") offers **Start a new robot from
  this** instead of rebuilding the open robot, and a request to bring in an existing robot offers Import; **Change
  <project> instead** keeps it an edit. Clear wording decides on this computer; SubQ (with a key) settles only
  wording the rules can't place, within 4 seconds. `morph robot new "help me sort socks"` runs the same brief in a
  terminal: it asks the questions there (or takes the defaults with `--defaults`, and without a terminal), then
  builds the project (`--name`, and `--nearest` for a request Morph can only build the nearest robot for).
- Chat edits understand units (mm, cm, in, g, kg, lb, oz) and whole-word object names, and questions such as "can it
  lift 2 kg?" no longer change the design.
- **Import** (beta): drop a robot folder or zip (URDF, xacro, MuJoCo XML) or paste a GitHub or Hugging Face link; drop
  an STL, OBJ or GLB of the object to grasp, a parts list (CSV, XLSX, Markdown), a KiCad board or a Morph export. Morph
  finds the gripper, tool point, base and ready pose, asks only what the files can't tell (usually the motors) and
  builds the robot as a new project. Imported robots are kept in `projects/.robots/` with their licence and where they
  came from; `morph import` does the same from a terminal, given a path or a link. Auto-imported arms are sized for
  their worst pose over the joint range, so they come out more conservative than the shipped SO-101 (sized in its
  stretched-out pose). On a Studio other computers use, links are off unless it was started with `--allow-url-import`. A
  parts list line is a catalog motor when its part name says it is a motor and its name or MPN names one, and its
  description or notes pick the variant (a "12V" STS3215, or a C018 or C047, is the 12 V servo); notes that mention
  the servo on a screw or a driver board don't make it one, and neither does a name for a part of one, whatever follows
  or what it fits ("STS3215 servo cable 200 mm", "SG90 servo arm set", "U2D2 for Dynamixel XL430"); what a servo comes
  with or is built with doesn't change what it is ("STS3215 servo, horn included", "Feetech STS3215 Servo Motor 12V
  Metal Case"). A line named "Feetech servo", "Gripper servo" or "Servo (x6)" is the motor its description names, and a
  pack bought once counts its motors. KiCad's grouped CSV and KiBoM parts lists are read too, and a list without prices
  shows no total. The preview shows the MPN and the description. An object whose grip would bend the
  fingers gets the thicker beam New robot would give it. An import whose build isn't feasible says why in its project's
  chat, with the fixes Morph can build as buttons (12 V STS3215 servos, Dynamixel servos, thicker fingers).
- **Show Morph the job** (beta): add a phone video or photos of the task done by hand. Morph picks still frames in
  your browser, estimates the object, the pick and place spots, the cycle time and safety needs, and lists every
  change before Apply. Only the chosen frames are sent, after you agree: to Claude, plus up to 4 to SubQ as an
  optional second opinion. With no key, or network lookups off, you fill in the cards by hand with a measuring tool.
  Clips your browser can't play (often iPhone HEVC) can go to the Studio computer's ffmpeg (install ffmpeg, or the
  `imageio-ffmpeg` package: `morph doctor` gives the command), and the video is deleted once its frames are out.
- SubQ Snap: an optional SubQ key (Keys, `SUBQ_API_KEY`) for quick yes/no and multiple-choice decisions, shown as
  "SubQ Snap (AI)". It is only called with your key and network lookups on, retries once, switches itself off for a
  refused or out-of-credit key, caches answers (never requests or photos) for 7 days, and every use has an offline
  fallback. `morph doctor` checks it; `morph subq "help me sort socks"` tries a key by hand.
- **Morph Teams** (preview, "GitHub for physical things"): `morph team init --owner "Jim"`, then `morph studio
  --team`. Invite teammates with a single-use link and a role (owner, editor, viewer) plus disciplines; comment on
  revisions, stages, BOM lines and parameters; change requests are routed to the owners of the stages they touch, and
  a generalist covers any discipline with no specialist; a change request's preview says what it does in numbers
  before anything builds (torque margins joint by joint, the servos, bus protocol, controller and supply, connectors
  and firmware library, geometry, grip force, the purchased parts' cost and the issues it adds or clears, estimated
  from the pipeline's fast stages); above prototype nobody approves their own work, and the page says who can instead;
  reviews are tied to the exact revision, and an approval goes stale only when a later edit recomputes that
  discipline's stages; releases are gated by build level; branches, an activity feed, an inbox, and a hash-chained
  audit log (`morph team audit --verify`). Team data stays on the Studio computer. Links travel as plain HTTP unless
  the Studio is behind HTTPS (use a tunnel or VPN), and an approval doesn't certify that a robot is safe.
- **World mode** (beta): put the robot in a room (a built-in desk, kitchen counter or workbench, or your own .glb room
  scan with a two-point scale), place it by clicking a surface, walk through the room, and simulate pick and place
  against grey obstacle boxes with a carry path that keeps clear of them. Editing the room reruns only the
  simulation, and each revision is simulated in exactly the room it names: every saved room is kept under its hash
  (`world/saved/`), so editing the room while a build waits never changes what that build simulates. Check placement:
  measured checks set the verdict; an optional AI review (beta) of six rendered views adds findings, suggested changes
  and a guided tour, and never changes pass or fail (without a key it says so and sends nothing). When the straight
  move from the rest pose would sweep through a box, the approach and the return to rest follow the carry path too.
  It is not a motion planner. Needs MuJoCo 3.1.6 or newer.
  `morph world egg-picker --starter desk` (or `--off`) does the same from a terminal; [docs/WORLD.md](docs/WORLD.md).
- `morph export egg-picker -o egg-picker.zip` (`--rev r003` for an older revision, `--full` to add every revision's
  design and results): the Studio's Export from a terminal, with `team.json` on a team. `morph doctor` lists each
  feature's status: New robot, Import (xacro and STEP support), Show Morph the job (whether ffmpeg was found), Teams
  and World mode.
- **Robustness studies** (beta): one nominal simulation says a design works once, exactly as drawn. A study runs a
  revision's own pick-and-place plan hundreds of times in worlds drawn from the project's tolerances (the object off its
  mark, turned, its size and mass, pad friction, grip-force calibration, servo torque, deadband, encoder noise, the
  host's set-point rate and, in World mode, the mount and every box; each range says where it comes from), labels every
  run (placed, jaws stayed shut, dragged, missed grasp, dropped, over-force, hit the room in a named phase) and reports
  the success rate with a Wilson 95 % interval, the rule that best isolates each failure group, and whether that failure
  holds at half the timestep and with softer friction ("model-sensitive: test it on the real robot"). The default
  egg-picker succeeds in 140 of 200 runs (63-76 %); 58 of the 60 failures are the jaws staying shut at release (half the
  runs with the egg turned 3-9 degrees end that way), and that failure changes with the solver's settings. Runs are
  reproducible from the seed on any number of worker processes, saved as they finish and resumable (Ctrl+C keeps them):
  `morph study egg-picker` (with `--runs 1000` or `--stress`). In the Studio, the **Robustness** card under the
  Simulation tab's pick-and-place result runs a study of the revision on screen (50, 200 or 1,000 runs, or a stress
  test with every range doubled), shows its progress live in view in place of the earlier result, stops it on request
  within seconds (the finished runs stay, and it carries on later), says what the result means and what it doesn't in
  plain words ("likely between 63% and 76%", the 95% interval in its tooltip; no "Most likely when" for a failure group
  of fewer than 3 runs; `morph study` prints the same words), replays a failing run in the 3D view,
  and lists every tolerance with where its range comes from. Editors start studies and change tolerances (a change
  marks older studies outdated); every member of a team reads them. Studies run on the Studio's computer, so
  `morph studio --remote` on a bigger machine is how a team brings its own compute; one study at a time per project, at
  most 10,000 runs from the Studio. Where no study exists the card says so: one nominal run tests nothing around it.
  Simulation, for the tolerances shown; not a field failure rate.
- **World mode checks the whole path** (beta): every move of the pick and place (the approach, the descent to the
  object, the grasp, the lift, the carry, the set-down, the release, the rise and the way back to rest) is checked
  against the grey boxes every 10 ms, with the object while it is carried. A move that comes within 10 mm tries its
  alternatives (along the carry path, from higher up, the release with the hand opened only 4 mm past the object, the
  way back straight up first) and keeps the first that stays clear; when none does, the simulation fails and says
  which move and which box ("the jaw left hits Carton wall during the set-down at 5.0 s"). The World panel's status
  line says which way the arm went. Simulations without a room are unchanged. The motion between those poses counts
  too: a jaw moves up to 6.5 mm between two 10 ms samples and 19.5 mm between two of the run's frames, so beside the
  bench bin 18.5 mm out the check said "At least 10 mm" where the run comes within 9.96 mm. How far any point of the
  arm can travel between two poses is bounded, and the moves that could come closer are checked in between, so the
  clearance kept and shown (the smaller of the run's and the plan's) is never more than the closest approach; the
  check isn't OK while any planned move comes within 10 mm. A shelf over the robot and a walkway beside it are measured
  from the arm's own highest and furthest point, not its tool point's (the egg-picker's fingertips reach 21 mm higher),
  and "Room to move" rounds its share down.
- **Clearance to the room scan** (beta): with an uploaded scan, Check placement also measures the simulated path against
  the scan itself, as a warning that never sets the verdict ("Measured against the scanned surface, which can be noisy
  or have holes."), and lists each place the arm comes within 10 mm of it with **Add box here**: that spot becomes a
  grey box, trimmed so it is no closer to the path than the scan, and the next simulation plans around it. A place next
  to a grey box, or one a box was added for (even when the trim moved the box away from it), isn't offered again while
  the path comes no closer there than the box was fitted for; when it does (the robot moved, say), the place is offered
  again with a box fitted to the new path. A box sized or moved by hand is a plain grey box. The same box is never added
  twice, and a box the room holds already is never offered again. The check, the boxes' fit included, stays within its
  20 seconds: the review's desk scan takes 7.5 to 8.5 seconds on a 4-core computer at load 4-7 (12 to 13 before) and 10
  to 11.5 at load 11-13, where it used to take 16 to 18 and was often cut short; a generated room of a million triangles
  takes 10.5 seconds at load 7. The boxes' share of the time is set aside only once the check has listed a near miss,
  sized from what fitting the boxes takes. Cut short, the check never claims more room than it measured: it says the arm
  comes within at most the closest distance it measured, or that there wasn't time to measure it, says only part of the
  path was measured, and says whether checking again when the computer is less busy may finish it. Answers cut short,
  whose box ran out of time to be fitted, or that ran out of time before every near miss was listed (they say so, and
  that checking again may list them all), aren't kept, so Check placement measures them again.
- World mode's distances are rounded down, never up, wherever they are kept or shown (the room scan, the simulation's
  clearance, a near miss's row, the base's margin, the room above the arm, the World panel and `morph world`): to 0.1
  mm under 10 mm, to the millimetre from there. Before, 30.68 mm read "At least 31 mm", and a near miss of 9.96 mm was
  kept as 10.0, so the scan check passed it as "At least 10 mm" and the simulation called it clear.
- The place a box from Add box here keeps (`covers`) counts only as what the fit makes: past the box only on trimmed
  sides, no further than the box's own size, and with the distance it was fitted to. A world saved through the API
  with a 10 mm cube and a place 50 mm past it on every side could hide a real 9.0 mm near miss; now that place is
  ignored and the near miss is offered.
- A room-scan near miss's row says "at most X mm" unless it is the check's closest place measured along the whole path,
  and its distance is measured exactly at its own closest points: before, a row read "2.2 mm from the wrist" in a check
  cut short where the wrist comes within 2.1 mm, and a box added from it kept 2.2 mm as its fit.
- Robustness studies in World mode can let only the room vary (**Only the room varies** under the card's tolerances:
  the mount and every box, the object and the hardware nominal), so every failure is the room's. A carton wall 12 mm
  from the open jaws: 185 of 200 runs succeed (88-95 %); the 15 others hit it at the release, most often with the
  mount 4.5 to 10 mm off.
- Robustness studies keep their runs when the Studio stops: after a crash or a closed window the study reads "Stopped
  when the Studio stopped after N of M runs" at once, with the runs it saved, and *Carry on* finishes it with the same
  runs as if it had never stopped. A revision that can't be studied (its simulation failed, a preview-only build), and
  a study whose revision was replaced or whose Morph code changed since, are refused before anything starts, with the
  reason. A failure group that holds every run gets no rule, and successful runs that fail with other simulator
  settings are flagged as well: [docs/ROBUSTNESS.md](docs/ROBUSTNESS.md).
- **Live prices** (beta): in the BOM tab, Claude checks current distributor prices and stock for the purchased parts
  with Morph's read-only distributor tools (Mouser, and Farnell, element14 and Newark; written from their API
  documentation and marked "Not yet tried with a real key"). The Farnell tool ships turned off until a legal review of
  Farnell's API terms, which limit storing its data and passing it on: Keys says so next to it, and only the owner
  turns it on. Every price says which distributor it is from ("from Farnell/element14", "from Mouser Electronics"),
  on the card, in Show prices and in Claude's summary. Each run gets a card in the chat that says what it used
  and what it waits for: **Key needed** names the service, what the key is for, its cost and what is sent, with Add
  key (opens Keys on that key), Get a key and Skip; once the key is saved the run continues by itself ("Key added,
  continuing…"), and a key the service refuses can be replaced. A tool that asks first shows a **Tool request** card
  (Allow once, Always for read-only tools, Skip). Morph keeps a price only from a tool that answered in the run, marks
  one it can't find in that answer "check it", and never orders anything. Runs use Claude on your Anthropic key and
  start only from the BOM tab, unless you turn on "Check live prices after a build changes the purchased parts" (off by
  default); "Let agents use tools (beta)" under Keys turns them off. On a team only the owner adds keys and allows
  tools; editors start runs and may skip; viewers see the cards and are told to ask the owner.
- **Morph writes a tool** (preview): when a run needs a tool Morph doesn't have, its card offers **Draft a tool** to
  the owner (also **New tool (preview)** in Keys). You name the need and the API's official documentation; Claude reads
  that documentation site only and fills in a form, never code, and Morph builds a declarative read-only tool from it
  (GET lookups to hosts the documentation names; nothing written, uploaded or paid) and checks it. The review shows the
  hosts and where the documentation names them, each lookup's address and what it sends, where the key goes, cost,
  privacy, the pages read and the spec's sha256. **Approve and install** installs exactly that spec for the next run,
  which asks for the new key in the chat; a later change to the installed file turns its tools off until a new draft
  is approved. A draft that breaks a rule says why and is never installed. The need card also lists services
  Darwin's public search knows for the need, with their websites, as suggestions only: Morph never connects to them,
  signs in or pays. Drafting uses Claude on your Anthropic key; on a team only the owner drafts and approves.

### Studio

- Each browser tab works on its own project: the project menu (it always opens now, even with one project) switches
  that tab only and puts `?project=` in its address, so other tabs and other people stay where they are.
- Chat and parameter edits wait their turn in a queue per project instead of being dropped while a build runs; the
  message says where it waits. An edit to a setting that changed after you asked is not applied over it: the chat
  asks whether to apply it to the newest revision or drop it. One build runs at a time across projects.
- Each project keeps its chat, so it is still there after switching projects or restarting.
- New projects, forks and deletes through `/api/projects` (deleted projects move to `projects/.trash/`).
  **Reset demo** only touches the demo robots (egg-picker and so101), never projects you made.
- Revisions sort by number (r1000 after r999) and a number is never reused; ledger times are UTC.
- Only changes to pipeline code rebuild the demo robots at the next start.
- The page fits smaller windows: at 1280 px wide or narrower the right panel folds away and a panel button opens it
  over the robot; the header shortens its buttons to icons down to 910 px instead of running off the edge; the chat's
  tools (Show Morph the job) sit under the message box. A new chat says where New robot, Import, Show Morph the job and
  World are.
- Dropping a video or photos on the page opens Show Morph the job; other files go to Import.
- On a team Studio, a viewer's page has no editing tools: the chat box, Apply and the New robot, Import, video and
  World buttons are off or gone, and the chat box says how viewers propose changes instead.
- Agent run cards (beta): Stop shows "Stopping" until the call under way returns; a run stopped because agent tools or
  network lookups were turned off, or Claude refused the Anthropic key, offers the owner **Open Keys** on that switch
  or key (others are told to ask the owner); a refused key's card says it may be mistyped, expired or not enabled for
  that service; tools that aren't Morph's own are tagged "drafted" or "your own manifest"; a finished card no longer
  shows Working again when an answer from before the end arrives late. A tool key whose tools are off, or whose
  manifest is gone, stays in Keys as "not used now" until you remove it, and `morph doctor` says so.
  [docs/TOOLS.md](docs/TOOLS.md) describes agent tools, the cards, the manifest format and what has been verified.

### Install and updates

- Launchers find Python 3.10 to 3.14 and say what to install when there is none (Python 3.15 is too new for now).
  They rebuild a broken `.venv`, show pip's progress on the first install, and skip pip on later launches.
- Launchers no longer pull `main` on every start. `--update` switches a git clone to the newest release tag, and
  leaves a clone that already has commits made after it (a branch, or `main` once it moves on) as it is; without it,
  the launchers never run git, and after the first install they start offline.
- The launchers install Morph itself into `.venv` too, so `.venv` has the `morph` command. README, *Commands in a
  terminal*, says how a terminal uses it, and the docs' `morph` and `python` commands run there. Messages that name a
  command give one that works in a terminal in the Morph folder, and name optional packages as they are
  (`imageio-ffmpeg`, `cascadio`, `xacro`): a package called morph on PyPI is an unrelated project.
- A clone whose launcher pulled on every start upgrades cleanly on Windows, where that last pull replaces the launcher
  while it runs: the new one takes over with the options given. On macOS and Linux the old launcher finishes that start
  itself, and `morph studio` ignores the `--update` it passes on. Both launchers ignore a `#` comment pasted along with
  a command instead of passing it to the Studio (so does `morph studio`), Windows options containing `)` work, and
  every command in the docs pastes as is into Command Prompt, PowerShell, bash and zsh. Nothing more is read from
  `run_studio.bat` once the Studio starts, so a `git pull` in another window can't make it misread the new file. CI
  runs both launchers, from Command Prompt and PowerShell on Windows, the upgrade and `--update` included.
- `--remote` says how to let the link through the firewall on Windows and macOS when another computer can't open it.
- Where MuJoCo has no build, the launchers say so before they make `.venv`, instead of failing in pip and blaming the
  internet connection. What counts is the Python's own platform, since that is what pip installs for: an Intel Mac or
  32-bit Windows is told at once; an ARM64 or 32-bit Python on Windows, an Intel Python on an Apple silicon Mac, or a
  Mac Terminal in Intel mode (Rosetta) is named with the fix; an x64 Python on Windows 11 on ARM goes on, in x64
  emulation. Windows on ARM is untested: MuJoCo's x64 build uses AVX instructions, which that emulation runs only on
  Windows 11 24H2 or later, so there every start first checks that MuJoCo loads and steps a one-sphere model (which
  runs its compute code, not only the library loading) and, when that fails, says to update Windows or use another
  computer. When pip fails, both launchers name the same Pythons the packages have builds for
  (an x64 Python on Windows).
- `constraints.txt` pins tested versions with wheels for Python 3.10 to 3.14 on Windows x64, macOS 12+ on Apple
  silicon and Linux x64.
- One version number, in `morph/__init__.py`. The package metadata now has the licence, project links, supported
  Pythons and the extras `video`, `cad`, `xacro` and `dev`.
- A wheel now carries the SO-101 robot, Darwin's cached answers and the licences, and keeps `.env` out of
  site-packages. Running from the git folder is unchanged.
- Morph installed with pip keeps its projects in your user's data folder (`%LOCALAPPDATA%\Morph\projects`,
  `~/Library/Application Support/Morph/projects`, `~/.local/share/morph/projects`) instead of a `projects` folder in
  whatever folder it was started from, where it moved into a folder of yours with that name. A `projects` folder that
  already holds Morph projects is kept, and Morph says so; `MORPH_HOME` still chooses. `morph doctor` and `morph
  studio` print the projects folder's full path. A Morph folder from git or a zip uses its own `projects` folder even
  when its `.venv` `morph` runs from another folder (it used that folder's `./projects`).
- CI on Windows, macOS and Linux with Python 3.10 to 3.14, a lint job, and a check that the installed wheel builds
  both demo robots.
- End-to-end browser tests (`tests/e2e`, run with `MORPH_E2E=1` set) start a real Studio, offline and without keys,
  and drive Chromium through New robot, Import, Show Morph the job, a three-person Teams change request, World mode,
  a robustness study, Live prices and drafting a tool (with a local stand-in for Claude), all with full simulations;
  `MORPH_E2E_PORTS` keeps every Studio they start inside one port range, shared out between pytest-xdist workers.
  [INSTALL](docs/INSTALL.md#for-developers) says how to run them.
- New docs: [INSTALL](docs/INSTALL.md), [PRIVACY](docs/PRIVACY.md), [SECURITY](SECURITY.md),
  [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md), a [release checklist](docs/RELEASE_CHECKLIST.md), and one page each
  for [Teams](docs/TEAMS.md), [Import](docs/IMPORT.md) and [World mode](docs/WORLD.md). Internal planning docs moved
  to `docs/internal/`.
- The source archive and the wheel carry what they say: NOTICE and THIRD_PARTY_NOTICES name the fonts, Darwin's saved
  answers and the test fixtures recorded from real services (the others are made up), and no file names a folder of
  the computer Morph was developed on (a browser test saved its screenshots there; it takes them only when
  `MORPH_SHOTS` is set now). Building the wheel no longer prints a setuptools warning for each Studio folder, and CI
  fails if one comes back.

### Privacy

- The Studio page loads nothing from other sites: Inter and JetBrains Mono ship with Morph instead of coming from
  Google Fonts.
- Keewano build events and grip-force calibration are opt-in. Calibration never uses synthetic or thin data, and the
  UI says that the R Cubed demo history is seeded, not measured.
- New switches under Keys, Privacy and network; `MORPH_OFFLINE=1` turns every network call off.
- With Keewano events on, imports, world mode and SubQ calls add categories, counts and timings to them (the kind of
  import and its format, the kind of room and its verdict, which SubQ decision and how long it took), never file
  names, links, robot names, frames or what you typed. Team activity is never sent.
- Board exports no longer carry Morph's licence; `morph kicad --license` adds one when you choose.
- Live prices sends the distributor only the keywords or part numbers it searches for (from your parts list), with
  your key for that distributor, and sends the parts list and what each tool answered to Claude; the run's file keeps
  those answers (cut down, without keys) for up to 7 days, and cards, events and logs never carry a key, a tool's
  answer or Claude's messages.
- Drafting a tool (preview) sends Claude the need you typed, the documentation's address and the tool ids and key
  names Morph already uses; Claude's web fetch reads pages of that documentation site only. Morph keeps the pages'
  addresses and the lines that name each host, not the pages. Darwin's suggestions send the need to Darwin's public
  search (no account); its answers and each suggested service's public details are cached on this computer.
- With network lookups off, an agent run sends nothing more to Claude: it stops before its next step and waits, instead
  of asking Claude to carry on without its tools.

### Security

- The server keeps every file request inside the open project, checks project and robot names before creating
  anything, caps and checks request bodies, bounds its event queues and sends security headers with a content
  security policy.
- `arm.source` names a robot in a known folder; URDF meshes stay inside the robot's folder.
- `.env` is written readable by your user only (on Windows, as private as the Morph folder where Windows won't let
  Morph limit it to your user; see below). Chat can't change the project name or the robot source, and a chat
  message that looks like an API key (Claude, SubQ or Keewano) is refused.
- Windows ignores the 0600 mode Morph gives its private files, so they were as private as the folder they were in:
  every user of the computer could read them in a folder such as C:\Morph. Morph now limits `.env` (each time it
  saves it) and the `.runs`, `.tools` and `.team` folders (when it makes them) to your user, with Windows' own
  permissions (icacls). Where that isn't possible, they stay as private as their folder: keep Morph in your own user
  folder, such as Documents.
- API keys are never sent on to another server when a service answers with a redirect (SubQ, Keewano). A `.env` from
  an older Morph that others could read is made private when the Studio starts.
- Remote mode: after 10 different wrong access codes from one address, that address waits 15 minutes.
- The server never opens a pipe or device file, and a request whose body stops arriving gets an answer.
- Every value a chat edit, Claude or a request can set must fit limits far wider than any robot Morph builds: a
  kilometre-wide object used to hold the build slot for hours. Designs with unknown keys are refused. `morph robot
  change` and `morph robot new --need` apply the same limits before they build anything.
- Each connection gets 20 seconds for its request line and headers, and the Studio serves at most 64 connections at
  once (48 of them live event streams); more are answered "busy" (503) at once. A slow or idle client could hold a
  thread for good, which matters most in remote mode.
- Teams: members sign in with a single-use invite or a 15-minute sign-in link (signed session cookies; codes and
  script tokens are stored hashed, and wrong codes are throttled); `MORPH_TOKEN` plays no part in team mode. Every
  route is checked against the team's role table, each member's tabs get only their own notifications, and gated
  order packages of an unreleased revision follow the team's policy, on Export too (a viewer's export of an unreleased
  revision leaves its order files out and lists them). Imports go into the audit log, with the link they came from.
  Without `--team` the Studio has no Teams routes.
- Imports treat every file and link as hostile: archive limits, no XML entities, meshes and paths kept inside the
  import, xacro expanded in a separate process, and links only from github.com and Hugging Face over HTTPS.
- World mode names its room by id and hash, never a path: chat can't change it, Teams change requests can't name
  anything else, and an imported export (which doesn't carry the room) is simulated on the plain table.
- Agent tools (beta): a tool is a vetted manifest run by one hardened runner (HTTPS to the manifest's own hosts only,
  every address checked to be public and connected to as checked, no redirects, size and time limits, keys removed
  from every answer). A tool's key (`MORPH_TOOL_*`) shows in the Keys dialog under its service, with where to get it,
  its cost and what is sent; it is checked against the tool's pattern when saved, sent only to that tool's hosts and
  never to Claude, and chat refuses it like any other key.
  Agent runs that wait for a key, an approval or a restart are kept in `projects/.runs/` (readable by your user only
  on macOS and Linux; on Windows, see below; deleted after 7 days); every tool decision and call goes into a
  hash-chained log, `projects/.tools/audit.jsonl`.
- A drafted tool (preview) is checked before the owner sees it: GET only, at most 4 lookups; every address HTTPS to a
  public DNS name that the documentation pages Claude read name outside any text aimed at AI agents, resolving to
  public addresses; its key a `MORPH_TOOL_*` name sent only to those hosts; no wording that addresses an AI; no clash
  with Morph's own tools. Documentation text reaches Claude only as fetched pages, never as instructions, and pages
  that talk to AI agents are quoted in the review. Approval is bound to the spec's sha256 (`projects/.tools/pins.json`),
  and every step goes into the audit log.
- Agent tools after an adversarial review: a key that a service echoes back is removed before any text is shortened, so
  no part of it reaches Claude, a run file or a summary; a call's one deadline includes looking up its address;
  turning off agent tools stops a working run before its next request or call; Stop pressed during a call or Claude's
  answer ends the run when it returns; a run or draft past its 7 days can't be continued or approved; a refused
  draft's review never links an address Morph refused; a drafted service named like a Windows device (aux, com1) is
  saved under another name. Tests now search every request, event, file, log and export for marker keys, and cover
  the Teams roles, remote mode and two tabs answering the same card.

### Fixes

- World mode: the AI review no longer brings back the move arrows of a box you had deselected (they floated where the
  box had been); a box still selected keeps them.
- A failed stage stays failed instead of being reused as feasible by a later edit; required output files are
  checked; fallback CAD is labelled preview-only.
- SO-101 gripper firmware uses radians.
- Changing the SO-101's electronics in a fresh process no longer fails (`KeyError: 'sts3215_7v4'`).
- The CLI exits non-zero when a build fails, prints safely on Windows consoles, and `morph doctor` shows the version
  and git commit.
- Export a revision as a ZIP with a manifest of what completed. Keys, settings, chat and the ledger are never included.
- One servo bus speaks one protocol: a DYNAMIXEL servo on a Feetech bus (or the reverse), for example through
  `arm.actuator_overrides`, is an issue and gets no wiring or firmware, instead of firmware that sends it the wrong
  commands. An override naming an actuator the catalog doesn't have is reported instead of ignored.
- A wrist camera or IMU rides on the gripper, so adding one re-sizes the joints and reruns the simulations (Teams
  asks the simulation reviewers too). The SO-101 is sized with its wrist-camera mount when it has one.
- A page opened or refreshed during a build follows the build and shows the new revision when it is done. When the
  server can't answer, the page keeps listening and retries instead of staying offline; after a server restart it
  reloads.
- A Studio left running no longer grows with every build (about 1.2 GB after six builds, 1.5 GB at its peak): each
  project's builds run on one thread, and after each build, and after the demo builds of a first start, Morph
  collects what the build left in reference cycles and hands the freed memory back to the system. It waits at about
  300 to 400 MB (it idled at 720-780 MB after a first start) and peaks near 1.1 GB while building.
  [docs/INSTALL.md](docs/INSTALL.md) says how much memory Morph needs.
- Robots with a custom controller PCB print their base on a 250 mm bed again (it was 253 mm): the USB-C and power
  jacks sit higher, over the board's edge, so the bay behind the board is shorter.
- Chat: a Claude call gives up after about a minute instead of holding the project's queue, and "gentler grip" scales
  that project's own grip target.
- New robot: building the same brief twice (a double click, a retry) gives the project it is already building instead
  of a second copy, and a brief whose first build failed can be built again. The dialog's stage list includes the
  stages that finished before it joined the new project (Sizing used to stay "queued").
- Teams: a teammate's comment or change request no longer clears the comment or change request you are writing; the
  panel updates when you leave the field. In a team Studio an export carries the team's record (`team.json`): the
  approvals, release gate and releases of that revision, or the project's whole team history, with names instead of
  member ids.
- Teams: the author of a change could approve it above prototype; the approval was recorded and shown ("You approved")
  but the release gate ignored it. Above prototype nobody approves or asks for changes on their own work now, and the
  page says who can approve instead. Removing a member (or their "sign out everywhere") ends their open pages within
  seconds: an open tab used to keep receiving the project's live updates; it now stops and shows why it was signed
  out. The record of an applied change request names who applied it (`applied_by` in `team.json`), inbox items name
  change requests by number and title, a viewer is told how to propose a change instead of "ask an owner", and the
  Share dialog lists a new invite at once (with "expires in 7 days"; it said "expires just now") and keeps what the
  owner is typing when a teammate joins. The right panel scrolls to its end at 1280x720 (a change request's Apply
  button was out of reach), Keys is hidden from members who aren't owners, and switching to a new revision no longer
  shows the previous one's comments under its name for a moment.
- Robots with Controller = ESP32-S3 DevKit build their gripper stage (it stopped with KeyError 'esp32_s3_devkit'), and
  the quick checks of New robot and of a change request's preview run for them.
- Show Morph the job: cards you fill in yourself (no key, or kept on this computer) are described as yours, not as
  "seen in the video".
- New robot: a typed reply such as "6 cm, 50 g, 10 per minute, people nearby" keeps the object it describes
  (strawberries stay strawberries, with their damage force), "nobody nearby" means no, and "from the left side to the
  right side" is kept as said. The plant waterer and the cat feeder set things down by the plant or the feeding spot,
  not in a bin, and say that pouring isn't simulated. The summary says which sizes come from Morph's presets, and a
  reload no longer starts a second brief from `?new=`.
- Chat without Claude: "make it cheaper" steps down to the cheapest arm that still lifts the object, "faster" and
  "slower" move the cycle time within what the arm can do, a change that is already there says so, and questions and
  thanks get a fitting reply instead of "couldn't map that".
- Import: picking one motor family keeps the arm on one supply voltage and sizes the motors as the build does, so the
  SO-101 with Smart bus builds as feasible; the dialog says when an imported robot built but isn't feasible, and why.
  An object mesh brings its own damage force and friction (never the previous object's) and, when fragile or narrow,
  the force-sensing finger. An archive with nothing usable left says what was skipped, and a link pasted without
  `https://` works. The motor families are offered for robots whose files name their motors too (the SO-101), and a
  link that fails at once (offline) says why instead of showing "Fetching" forever; a refused link stays in the box.
- A servo that would run far below its rated voltage on the arm's one servo rail is an issue: a 12 V STS3215 picked
  for one joint beside 7.4 V ones runs at 6 V there and gives about half the torque it was sized with, and the build
  called that robot feasible.
- Show Morph the job: a fragile object, or one narrower than the plain finger closes on, gets the force-sensing
  finger in the list of changes, so Apply builds a robot that passes its own checks. Review lists the change typed a
  moment before (it could miss it), each step opens at its title, and cards filled in by hand say whether network
  lookups are off or there is no Claude key.
- New robot: with Darwin lookups off, the sourcing step says so after a build.
- The project menu scrolls when there are many projects and refreshes its "building" tags each time it opens.
- World mode: a full simulation's return to rest no longer sweeps through the box the carry path just avoided (the run
  failed at its end). The line under the room's name names the closest box, points out passes under 10 mm and says the
  carry path changed only when it did (every room said it was "pulled in 12%"); walking starts on the room's own floor
  with the robot in sight (it started behind the kitchen scan's back wall), keys held when the window loses focus are
  let go, World off gives the camera back its usual range, and a new scan is never the cached old one.
- Parameter edits and the chat's built-in rules read the way the Parameters panel names things: "Parameter edit: Build →
  Pilot." instead of "config → pilot" (in the chat and the revision list), "Added an emergency stop." instead of
  "Changing added estop." and "Changing actuators → Dynamixel."
- `morph studio --team --remote` prints the network address teammates use, and the owner's sign-in link starts with
  it; Share warns when the page that makes an invite was opened at an address only this computer can reach.
- World mode's panel no longer covers the robot's stat chips.
- A tab that switches projects tells world mode and Teams, so the other project's room and team panel no longer stay
  on screen. It listens to the new project before loading it, so a build that ends during the switch is never missed,
  and a project whose first build is still running (or failed) no longer shows the previous project's revision as its
  own: the New robot dialog could report the new robot as done with the old project's numbers, and the robot, its
  tabs, its parameters and its Export button stayed on screen. They wait for the project's first revision now.
- Deleting a project, or Reset demo removing revisions, also ends the project's agent runs (Live prices) that were
  waiting for a key or still working, as it already stopped the project's robustness study: such a run could carry
  on later in a new project that took the same name, or for a revision Reset demo had removed, and price the parts
  of a robot it was never started for. Its card says why it stopped.
- `morph new` and `morph demo` without an Anthropic key say they use the offline parser instead of "Claude unavailable:
  TypeError". An `ANTHROPIC_PROFILE` whose config file is missing is named as that (the SDK's CredentialsError and the
  file it looked for), not as a missing SDK.
- Keys: the Anthropic key's section lists everything it powers (chat, New robot, Show Morph the job, World mode's AI
  review, Live prices, New tool), once, and the dialog names the folder `.env` is in on every system.
- A project opened while its first build runs shows the welcome and tips once that build is done, instead of keeping
  "no finished revision yet".
- Simulation tab: the player stays on screen in a narrow window (910 px), and Space plays and pauses even right after
  clicking the tab.
- Live prices: a wrong Mouser key, which Mouser answers with HTTP 200 and an error, reads "Mouser Electronics refused
  the key" with Replace key instead of "no results"; without a Claude key, Live prices opens Keys without sending a
  request the Studio refuses.
- World mode: the World panel steps aside on every tab but Robot (still open), and a walk or a guided tour stops
  there.
- Docs: README lists all five privacy switches, says which keys Test keys tries and has a row for tool keys
  (`MORPH_TOOL_*`); INSTALL names every key to revoke (tool keys included) and every end-to-end browser flow, and says
  to close the Studio before `git pull` on Windows; SECURITY.md says what to do when GitHub's private reporting form
  isn't available; SPONSORS no longer tells readers to add a payment method for Darwin: Morph never pays.
