# Changelog

Morph Studio by R Cubed. Releases are git tags (`v0.2.0b1`); `run_studio --update` switches a clone to the newest
one. Versions follow [PEP 440](https://peps.python.org/pep-0440/): `b1` is a beta.

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
  an STL, OBJ or GLB of the object to grasp, a parts list (CSV, XLSX, Markdown), a KiCad board or a Morph export.
  Morph finds the gripper, tool point, base and ready pose, asks only what the files can't tell (usually the motors)
  and builds the robot as a new project. Imported robots are kept in `projects/.robots/` with their licence and where
  they came from; `morph import <path|url>` does the same from a terminal. Auto-imported arms are sized for their
  worst pose over the joint range, so they come out more conservative than the shipped SO-101 (sized in its
  stretched-out pose). On a Studio other computers use, links are off unless it was started with
  `--allow-url-import`.
- **Show Morph the job** (beta): add a phone video or photos of the task done by hand. Morph picks still frames in
  your browser, estimates the object, the pick and place spots, the cycle time and safety needs, and lists every
  change before Apply. Only the chosen frames are sent, after you agree: to Claude, plus up to 4 to SubQ as an
  optional second opinion. With no key, or network lookups off, you fill in the cards by hand with a measuring tool.
  Clips your browser can't play (often iPhone HEVC) can go to the Studio computer's ffmpeg (install ffmpeg, or
  `pip install morph[video]`), and the video is deleted once its frames are out.
- SubQ Snap: an optional SubQ key (Keys, `SUBQ_API_KEY`) for quick yes/no and multiple-choice decisions, shown as
  "SubQ Snap (AI)". It is only called with your key and network lookups on, retries once, switches itself off for a
  refused or out-of-credit key, caches answers (never requests or photos) for 7 days, and every use has an offline
  fallback. `morph doctor` checks it; `morph subq "<text>"` tries a key by hand.
- **Morph Teams** (preview, "GitHub for physical things"): `morph team init --owner <name>`, then `morph studio
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
  `morph world <project> --starter desk` (or `--off`) does the same from a terminal; [docs/WORLD.md](docs/WORLD.md).
- `morph export <project> [--rev r003] [--full] -o file.zip`: the Studio's Export from a terminal, with `team.json` on
  a team. `morph doctor` lists each feature's status: New robot, Import (xacro and STEP support), Show Morph the job
  (whether ffmpeg was found), Teams and World mode.
- **Robustness studies** (beta): one nominal simulation says a design works once, exactly as drawn. A study runs a
  revision's own pick-and-place plan hundreds of times in worlds drawn from the project's tolerances (the object off its
  mark, turned, its size and mass, pad friction, grip-force calibration, servo torque, deadband, encoder noise, the
  host's set-point rate and, in World mode, the mount and every box; each range says where it comes from), labels every
  run (placed, jaws stayed shut, dragged, missed grasp, dropped, over-force, hit the room in a named phase) and reports
  the success rate with a Wilson 95 % interval, the rule that best isolates each failure group, and whether that failure
  holds at half the timestep and with softer friction ("model-sensitive: test it on the real robot"). The default
  egg-picker succeeds in 150 of 200 runs (68-81 %); all 50 failures are the jaws staying shut at release (44 % of the
  runs with the egg -4.7 to 1.5 mm off its mark along its length end that way), and that failure changes with the
  solver's settings. Runs are reproducible from the seed on any number of worker processes, saved as they finish and
  resumable (Ctrl+C keeps them):
  `morph study egg-picker [--runs N] [--stress]`. In the Studio, the **Robustness** card under the Simulation tab's
  pick-and-place result runs a study of the revision on screen (50, 200 or 1,000 runs, or a stress test with every range
  doubled), shows its progress live, stops it on request within seconds (the finished runs stay, and it carries on
  later), says what the result means and what it doesn't, replays a failing run in the 3D view, and lists every
  tolerance with where its range comes from. Editors start studies and change tolerances (a change marks older studies
  outdated); every member of a team reads them. Studies run on the Studio's computer, so `morph studio --remote` on a
  bigger machine is how a team brings its own compute; one study at a time per project, at most 10,000 runs from the
  Studio. Where no study exists the card says so: one nominal run tests nothing around it. Simulation, for the
  tolerances shown; not a field failure rate.
- **World mode checks the whole path** (beta): every move of the pick and place (the approach, the descent to the
  object, the grasp, the lift, the carry, the set-down, the release, the rise and the way back to rest) is checked
  against the grey boxes every 10 ms, with the object while it is carried. A move that comes within 10 mm tries its
  alternatives (along the carry path, from higher up, the release with the hand opened only 4 mm past the object, the
  way back straight up first) and keeps the first that stays clear; when none does, the simulation fails and says
  which move and which box ("the jaw left hits Carton wall during the set-down at 5.0 s"). The World panel's status
  line says which way the arm went. Simulations without a room are unchanged.
- **Clearance to the room scan** (beta): with an uploaded scan, Check placement also measures the simulated path
  against the scan itself, as a warning that never sets the verdict ("Measured against the scanned surface, which can
  be noisy or have holes."), and lists each place the arm comes within 10 mm of it with **Add box here**: that spot
  becomes a grey box, and the next simulation plans around it. A generated room of a million triangles is checked in 7
  to 12 seconds.
- Robustness studies in World mode can let only the room vary (**Only the room varies** under the card's tolerances:
  the mount and every box, the object and the hardware nominal), so every failure is the room's. A carton wall 12 mm
  from the open jaws: 185 of 200 runs succeed (88-95 %); the 15 others hit it at the release, most often with the
  mount 4.5 to 10 mm off.
- Robustness studies keep their runs when the Studio stops: after a crash or a closed window the study reads "Stopped
  when the Studio stopped after N of M runs" at once, with the runs it saved, and *Carry on* finishes it with the same
  runs as if it had never stopped. A revision that can't be studied (its simulation failed, a preview-only build), and
  a study whose revision was replaced or whose Morph code changed since, are refused before anything starts, with the
  reason. A failure group that holds every run gets no rule, and successful runs that fail under other solver settings
  are flagged as well: [docs/ROBUSTNESS.md](docs/ROBUSTNESS.md).
- **Live prices** (beta): in the BOM tab, Claude checks current distributor prices and stock for the purchased parts
  with Morph's read-only distributor tools (Mouser, and Farnell, element14 and Newark; written from their API
  documentation and marked "Not yet tried with a real key"). Each run gets a card in the chat that says what it used
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
- Launchers no longer pull `main` on every start. `run_studio --update` switches a git clone to the newest release
  tag; without it, the launchers never run git, and after the first install they start offline.
- `constraints.txt` pins tested versions with wheels for Python 3.10 to 3.14 on Windows x64, macOS 12+ on Apple
  silicon and Linux x64.
- One version number, in `morph/__init__.py`. The package metadata now has the licence, project links, supported
  Pythons and the extras `video`, `cad`, `xacro` and `dev`.
- A wheel now carries the SO-101 robot, Darwin's cached answers and the licences, and keeps `.env` out of
  site-packages. Running from the git folder is unchanged.
- CI on Windows, macOS and Linux with Python 3.10 to 3.14, a lint job, and a check that the installed wheel builds
  both demo robots.
- End-to-end browser tests (`MORPH_E2E=1 python -m pytest tests/e2e`) start a real Studio, offline and without keys,
  and drive Chromium through New robot, Import, Show Morph the job, a three-person Teams change request, World mode,
  a robustness study, Live prices and drafting a tool (with a local stand-in for Claude), all with full simulations;
  `MORPH_E2E_PORTS` keeps every Studio they start inside one port range, shared out between pytest-xdist workers.
  [INSTALL](docs/INSTALL.md#for-developers) says how to run them.
- New docs: [INSTALL](docs/INSTALL.md), [PRIVACY](docs/PRIVACY.md), [SECURITY](SECURITY.md),
  [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md), a [release checklist](docs/RELEASE_CHECKLIST.md), and one page each
  for [Teams](docs/TEAMS.md), [Import](docs/IMPORT.md) and [World mode](docs/WORLD.md). Internal planning docs moved
  to `docs/internal/`.

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
  your key for that distributor, and sends the parts list to Claude; cards, events and logs never carry a key, a
  tool's answer or Claude's messages. Farnell's terms don't allow storing its data, so its answers stay only in the
  run, which is deleted after 7 days.
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
- `.env` is written readable by your user only. Chat can't change the project name or the robot source, and a chat
  message that looks like an API key (Claude, SubQ or Keewano) is refused.
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
  Agent runs that wait for a key, an approval or a restart are kept in `projects/.runs/` (private, deleted after 7
  days); every tool decision and call goes into a hash-chained log, `projects/.tools/audit.jsonl`.
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
- A `gripper=` entry in `arm.actuator_overrides` reaches the gripper stage too: its force plan, grasp simulation and
  the servo stall force it shows used the family's default servo while the CAD hand and the electronics used the
  override. A hobby arm asked for an MG90S gripper servo (not in the robot catalog) gets the MG996R its hand is
  built around in that stage too, instead of a gripper stage that stopped with KeyError 'mg90s'.
- One grip force per robot, from the gripper stage to the firmware, the simulation and the manual. An imported SO-101
  shipped firmware that squeezed at a quarter of its servo's force whatever it held (6.82 N for a strawberry that
  bruises at 3 N, while its own comment gave a 0.49-2.0 N window), ignored the Grip force parameter, and was simulated
  at another force; its grip now comes from the object (0.5 N for a strawberry, inside 0.25-2.0 N) and the parameter.
  On generated arms the firmware's window disagreed with the gripper stage's (2.52-16 N against 1.17-16 N for the egg)
  and the simulation used a third number (2.17 N against the 2.34 N shipped). The manual named a 12.5 N "grip force
  limit" the firmware has no command for, and asked for a force-sensor curve and tare the firmware doesn't do; it now
  gives the firmware's target, hard stop and window and its F and Z commands, and leaves the force-sensor steps out of
  robots without force sensors. A grip target outside its window, or above the hard stop, is an issue.
- Torque margins, actuator picks and the simulation's servos are worked out at the servo supply the robot gets, not at
  the servos' rated voltage. The SO-101's 7.4 V servos run from 6 V (no catalogued supply gives 7.4 V, and 6 V keeps
  85 % of their torque), so its shoulder margin is 2.45x, not 2.89x; a hobby arm's DS3218s (rated 6.8 V) on the
  SG90's 6 V rail give 88 %. The SO-101's margins say they are at q = 0, the pose its profile is sized in, and a
  warning gives what its shoulder needs over the whole joint range. Servos without a catalogued supply near their
  rating get a 12 V supply and the servo buck set to it (a 12 V STS3215 beside 7.4 V ones now runs at 7.4 V, not 6 V).
  So New robot now says the stock SO-101 lifts about 100 g with a 2x margin (it said 150 g), and a 4-joint hobby arm
  100 g (it said 200 g). "Use a 7.4 V supply" runs the SO-101's servos at their rating.
- Chat "use a 5 V supply" changes the robot's servo supply when every servo takes 5 V, and otherwise changes nothing
  and says which servo's range rules it out (it used to answer "Changing servo supply → 5 V" and keep 12 V). "Use the
  rated supply voltage" goes back to Morph's choice.
- "Swap to Dynamixel servos" on the SO-101 (Present step 12) put XL430s (6.5-12 V) and XL330s (3.7-6 V) on one 6 V
  rail: a build that wasn't feasible, a margin worked out at 12 V, and firmware and a parts list for a rail below the
  XL430's minimum, while the caption said the electronics followed. A family swap now keeps one voltage class per arm
  (six XL430s on 12 V for the SO-101), a servo whose range leaves out its rail is an issue and gets no wiring or
  firmware, and the caption says what happens: the six printed holders are flagged until they are redesigned.
- Robots that keep their stock jaws (the SO-101 and other imports) no longer buy, wire, program and calibrate two FSR
  402 force sensors their jaws have no place for (finger.sensor is ignored for them, with a warning). Their grip is set
  by the gripper servo's torque limit, which the firmware now writes (Feetech STS: the torque limit register; DYNAMIXEL:
  Goal PWM) for the grip target at the jaw: 2 % of the SO-101 gripper's stall torque for a strawberry. A generated
  gripper without force sensors grips the same way, and one with them gets the limit as a hard cap at its force stop.
  A hobby PWM gripper servo has no torque limit; that is an issue when its stall force could crush the object.
- Firmware with force sensors that get no reading through a whole close (a sensor missing, unplugged or dead) used to
  squeeze on to the gripper's maximum-squeeze stop with nothing limiting it. A smart-bus or Dynamixel gripper servo's
  torque limit now caps that squeeze at the firmware's force limit; a hobby PWM servo has no torque limit, so its close
  stops where it would squeeze a gauge-sized object at that limit. With no reading by then the jaw goes back to the
  closed position and the firmware prints "!GRIP no force reading" on the serial port; a reading lost while holding
  keeps the jaw where it is.
- The manual's first pick-and-place test names the robot's own object (it asked for a hard-boiled egg and a raw one
  whatever the robot picks), and a robot without force sensors is told what to watch instead of a grip-force readout.
- New robot: saying an object is fragile ("pick up a raw egg without cracking it", the README's example, or the
  egg-picker's own description) no longer lowers the damage force Morph has measured for it. The egg became a 3 N
  strawberry while the card showed 25 N, and no grip both holds a 60 g egg and stays under 2 N, so the robot failed its
  own pick and place; it now builds with the egg's 25 N and passes. A stated fragility still gives an object without a
  damage force one (3 N for "fragile", 10 N for "gently"), and the card says so with the other readings to pick from. A
  "no safe grip force" issue now says where both numbers come from, and Build now stops at the confirm card when the
  preflight has issues instead of building straight away.
- New robot: requests to open jars, unscrew caps, twist lids or assemble parts read as "Adjusted" with a plain note
  that the gripper only picks and places, and the robot built moves the jars, bottles or parts (it said "Inside what
  Morph builds" and quietly built a bottle mover). A person the robot works for ("help my grandma", "pass my dad his
  pills", "for me") puts people next to it, so it gets an emergency stop. Pills get a pill-bottle robot with a note
  about pills, not one about screws; every width note gives the gripper's real minimum for the shape, and a water
  bottle is "too heavy", not "outside 26 to 75 mm".
- The inspector's "Payload at full reach" was measured top-down at less than half the reach (216 of 475 mm on the
  egg-picker) and overstated what the arm holds stretched out by 2.4 to 3x; "Top-down reach" showed the any-pose
  maximum. They now read "Max payload, top-down at 216 mm" (1,729 g), "Payload stretched out at 476 mm" (720 g, a new
  static check at full stretch), "Top-down reach" (280 mm) and "Reach, any pose" (475 mm), with a note that payloads are
  what holds before a servo stalls. Quick facts' "Payload" was the object's mass and now says so, and New robot's done
  card says where its payload was measured.
- Revisions no longer reuse a stage that a change affects. A new cycle time left the manual saying the old one (and
  Teams kept its Documentation approval); a new pinion radius left the sizing (gear teeth, grip force, hand mass) at the
  old one under freshly built CAD and simulation; a new controller left the gripper stage's force plan with the old
  board's pins. The gripper stage now analyses the fingers the build really makes (printed PETG for a prototype,
  machined aluminium for a pilot, molded polycarbonate for production; it analysed printed PETG for every build) and
  leaves the controller to the electronics, so a controller or fabrication-process change no longer reruns it. A test
  reads every key each stage uses and checks that a change to it reruns the stage.
- Print files stand inside the printer. The base shell's print file and 3MF stood it 244 mm tall on a 220 mm-tall bed,
  and its manufacturing check, which compared sorted dimensions, said it fit. The print orientation is now the best one
  that fits the process's build volume (turned 90 degrees on the bed when that is what fits), and a part that fits no way
  up is told to split, with its size as it would stand.
- A robustness study of a revision that only changed the controller or the electronics build no longer says it was
  "simulated with other Morph code". Such a revision reuses its parent's simulation, and the study compared the whole
  rebuilt model, including the boards' mass in the base welded to the table; it now compares what moves.
- When sizing builds something other than what was asked, Morph says so. The egg-picker asks for a 320 mm reach and is
  built at 475 mm (a top-down pick 234 mm out needs it); the Parameters panel showed 320 mm and nothing said why the
  robot was bigger. The inspector now lists "Morph adjusted reach raised from 320 to 475 mm: ..." with the reason, the
  Reach and Shoulder height parameters say "built at N mm", and New robot's done card and the terminal summary list the
  same notes.
- New robot no longer calls a design "Feasible" or "Built and checked": it says its software checks pass (simulation,
  torque margins, DRC, DFM) and that it is not bench-validated. The Firmware pane and the Export button say the files
  are generated and not bench-tested.
- The assembly manual no longer asks you to fit an "Upper arm logo" that has no print file and isn't in the BOM: the
  logo inlay is decoration in the 3D view only, and the manual's parts tables list exactly the parts with make files.
- Production cost no longer reads as a per-unit price when it is mostly molds. At quantity 1 the BOM tab said
  "Production $77,742"; it now says $4,771 per unit + $72,971 tooling, once, and the Manufacturing cards and the
  inspector say how much of the unit cost is tooling spread over how many units.
- Robots no longer drop what they carry in their own shake test while the build says every check passed. Round one's
  single grip source took the holding force from the gripper stage's grasp simulation, whose contour pads cradle a can
  at half the force the robot's own fingers need: a 370 g can was gripped at 4.56 N per finger and dropped (213 mm),
  and so were bottles, a watering bottle and a cup of pet food, robots 6c49545 had built without trouble. The holding
  force is now never below the static friction hold, m g / (2 mu gain); an object with no damage force is gripped at
  three times it (13.9 N for the can), a 1.5x margin over the shake test's 1.9 g; and every simulated scenario that
  fails is an issue that says why ("shake test failed in simulation (object dropped)"), so the robot no longer reads as
  passing its software checks. Teams' tag and "Built r002: ..." line and Import's verdict say "software checks pass"
  or how many issues the checks found, as New robot does, instead of "feasible".
- One stall-force model per gripper: one pinion drives both racks of a generated gripper, so each finger gets the
  servo's torque x 0.6 / (2 x pitch radius). Sizing, the gripper stage and the torque limit used three models: the
  can-mover's firmware capped F at a 165.9 N "hard stop" its servo gives 82.9 N of. The firmware's limit, the
  simulation's jaws and the standalone gripper's servo choice now use the honest figure.
- Force-sensing grippers grip an object narrower than their calibration gauge: the close looked for a reading only
  1 mm past the gauge and released an egg 2 mm narrower ("no force reading"); it now closes as far as the max-squeeze
  stop under the torque limit's cap (a hobby PWM gripper, which has none, only as far as its probe, GRIP_PROBE; see
  below). Grippers without force sensors closed to the gauge position, where a torque limit has nothing to push against;
  G now closes past the object, so the torque limit really sets the squeeze, as the manual and the plan say.
- Stock jaws (the SO-101): the torque limit was set for the grip 70 mm from the jaw's pivot, but the egg sits 62 mm
  out, so the simulation squeezed 2.83 N (3.41 N peak) for a 2.52 N target. It is now set at the lever where the
  simulation grasps the object. An open-loop grip (a torque limit, no force sensor) aims at the middle of the object's
  window, since nothing measures it: the SO-101 grips an egg at 4.58 N and a strawberry at 0.7 N.
- Changing the servo supply of an imported robot (chat "use a 5 V supply" on the SO-101) re-plans its grip: the
  gripper stage kept the old servo rail's numbers and the firmware shipped a 20.83 N grip limit where a fresh build
  says 19.29 N. A generated arm's grip limit and stall force are now worked out on the servo rail too (they were at the
  servo's rated voltage). Adding a wrist camera or changing the reach still reuses the gripper's grasp simulation when
  the servo rail stays the same. A test now checks that every stage reruns when a result it reads changes, not only a
  setting.
- New robot no longer reads a thing's name as a job. "Pick up cans and pass them to the packing station" built a
  hand-over with an emergency stop ("hands over drink cans"); "open boxes arrive on the left" was read as opening boxes,
  "sort twist ties" as twisting, and screw caps, brass inserts, plugs and an assembly line the same way. A hand-over
  now needs a person to receive it ("give it back to her" and "hand the cup to my mum" count, which they didn't).
- An arm that can't hold its own weight stretched out no longer shows a negative payload there ("Payload stretched out
  at 465 mm -13 g" for the SO-101 on Dynamixel servos at 7.4 V). The inspector and `morph robot` say "none" and that
  the shoulder servo can't hold the arm stretched out, so it carries payloads only closer in.
- "Run it from a 7.4 V battery" no longer builds a mains supply in silence. Morph plans mains power only; the chat now
  says what the robot gets (a 12 V mains supply and the servo buck set to 7.4 V, no battery, charger or low-voltage
  cutoff), warns when a charged lithium pack would exceed the servos' maximum (2S reads 8.4 V; the SO-101's servos take
  7.4 V), and understands "run it from a battery" and "a 2S LiPo".
- Teams' change-request preview no longer prices the molds as a cost per robot: switching the default egg-picker to
  production at quantity 1 read "about +$77,438 per robot"; it now reads "about +$4,466 per robot ... plus $72,971 of
  mold tooling, paid once". The "Built" line and the release checklist say how much of a unit cost is tooling, as the inspector does.
- A robot set to Force sensor: none is no longer refused with "add finger.sensor=fsr402" when its gripper servo's torque
  limit sets the squeeze (smart-bus and Dynamixel grippers): the egg-picker without force sensors now passes its
  software checks, the egg intact. A hobby servo gripper, which has no torque limit, is still told.
- A hobby PWM gripper servo (an MG996R) is no longer driven to stall: without force sensors G closed it to its
  max-squeeze stop, 3 mm past a can, where it sat at its stall current (2.5 A) for the whole carry, and hobby servos
  have no overload protection. G now closes it only as far past the calibration gauge as its own position loop needs
  for the grip, never past what the servo holds continuously (its rated torque): 0.49 mm for the can, 10 N against
  its 13.94 N target and the 6.97 N it needs not to slip; a 900 g can's target, above the stall, is an issue, and so
  is a grip where two position commands, the least it can close, already squeeze more: the SO-101's stock jaw on
  hobby servos (one count is 0.48 mm at its 62 mm lever) put 11.04 N on an egg, twice the 5.62 N the servo holds
  there, with no issue, and its firmware and manual called a can's 11.05 N "below" its 10.82 N target. The manual
  had a stock jaw on hobby servos calibrated with its faces touching, so G's close past that zero would hold the
  servo at stall on any object; it is now calibrated on a gauge of the object held against the fixed jaw, and its one
  moving jaw takes the object's whole size spread, not half. An
  object larger than the gauge is held further past its own contact, so G also closes no further than keeps one at
  the large end of its size (8 % over nominal) under 80 % of the stall and under its damage force; where no close
  can (a can at the large end is 2.64 mm per jaw over and would be held at stall, as would an egg or a water bottle)
  the plan says so as an issue, New robot uses smart-bus servos and chat's "make it cheaper" stops before hobby
  servos. The simulation and robustness studies fail a trial that holds the servo at stall ("Held the gripper servo
  at stall"); they counted a can 4 % over its size placed. They also crush a trial whose squeeze in the servo model
  the firmware closes by is above the object's damage force ("Squeezed harder than the object can take"): the
  simulated contact measured a light bulb 2 % over its size at 10.35 N, under its 15 N, where that model gives
  15.66 N, and the study counted it placed; both labels come from that model, not from the measured grip. F sets that distance, up to the servo's rated torque, the
  object's limit or what keeps the large end of its size under 80 % of the stall, whichever is lowest. With force
  sensors a close that has read nothing by GRIP_PROBE stops there and opens (it went on to the stop): far enough to
  reach the object at the small end of its size (8 % under nominal) where a dead sensor then leaves a gauge-sized
  object under 80 % of the stall and under its damage force, and one at the large end of its size (8 % over) under
  its damage force (1.04 mm for a 22 mm flat part with no damage force, whose large end can reach the stall for the
  moment before the jaw opens); otherwise (an egg would need 1.92 mm; a dead sensor at a grape's 0.96 mm would put
  19.77 N on a 2 N grape, and at a 22 mm part's 1.04 mm the MG996R's whole 30.86 N stall on one 8 % over its size,
  above its 25 N damage force) it stops where a gauge-sized object gets the force limit, or two position commands
  (0.92 mm for the egg, 0.16 mm for the grape), a narrower object isn't gripped, and the plan says so as an issue.
  On the generated MG996R hand that leaves force-sensing grips on flat-sided parts from 21.5 mm (the least those
  fingers close on; the plan itself raises no issue from 21.01 mm) up to 25.97 mm wide, with no damage force or one
  at or above the servo's 30.86 N stall, as the only hobby grips without an issue (measured on parts of 5 to 132 g;
  heavier ones overload the hobby shoulder); without force sensors none fits the fingers and covers its size.
  The simulation and robustness studies let such an object go too, as "Let go with !GRIP"; New robot then uses
  smart-bus servos and says why. The
  power budget, the plan's warning, the manual and the simulation use that squeeze, and a hold above the servo's
  rated torque is a warning, as is a force-sensing grip whose F can ask for more than that (up to 19 N for an egg,
  against the MG996R's 10 N).
- A robustness study run whose object the arm hits before the grasp is now "Knocked away by the arm before the
  grasp", not "Missed the grasp" (still counted: a real arm would hit it too). The default egg-picker's resting hand
  is 1.07 mm from a 200 mm water bottle's end, so a bottle 1.5 % over its size is hit at the start and rolls off
  the table; studies of it read those bottles as missed grasps.
- Teams' change-request preview no longer shows a grip change the build won't make: a new servo supply, servo family
  or build level on the egg-picker read "Grip: grip force target 2.34 → 2.17 N" (New robot's estimate without the
  grasp simulation) and planned the electronics with it. The build keeps the 2.34 N its grasp simulation found, and
  so does the preview now; an edit to a generated arm's object or fingers shows the grip its build will get (its
  grasp simulation's hold, about 3 s more): a 6 mm pad depth keeps 2.34 N (it read 2.17 N).
- "Bring the mail to her desk", "bring them to her room" and "give the parts to my dad's workshop" are no longer read as
  handing things to a person (with an emergency stop): a place that belongs to someone is a pick and place. Only a
  place, furniture or a container that ends the phrase counts as theirs; "bring the meds to her twice a day", "give
  the medicine to her nurse", "bring the remote to my dad's friend" and a place word that describes the person ("bring
  the medicine to her home nurse", "to her office manager", "to my dad's room mate") stay hand-overs with an emergency
  stop.
- New robot reads far more people as the person it hands things to, or works beside, and keeps the emergency stop
  next to them. Of the release review's 573 one-line requests, 151 that put a person next to the robot built it with
  nobody nearby and no emergency stop (6 more had an emergency stop but nobody nearby), among them "pass the clamp to the surgeon", "a robot that hands parts to the
  operator", "bring them their drinks", "give the pills to Mary", "bring dad his slippers", "give the baby her bottle",
  "get me a drink from the fridge", "a robot that works next to the operator" and "bring the meds to her twice a day,
  she lives alone"; 77 more kept the e-stop but not the hand-over ("bring the meds to my grandma"), and 12 that hand
  nothing to anyone were hand-overs ("bring her cup to the sink", "pass the parts to his work cell"). None is now.
  "Alone", "on its own" and "enclosed" say nobody is near only when they describe the robot, and the card then says
  Morph assumed it; a hand-over always keeps its emergency stop, also when the answer or the designer model says it
  works on its own, and New robot doesn't ask about people on one. "Get me a robot that..." is no hand-over, a job
  title before a place ("the operator station", "the nurses' station") is a place, "bring her cup to the sink" moves
  her cup (while "bring her tea in the garden", "bring her medicine on a tray" and "bring her tea to the garden" hand
  it to her), and the tool robot's "bring the tools to his workbench" is a pick and place. An animal it gives something
  to is a pet nearby, with an emergency stop, not a person it hands things to: "hand the dog a bone", "give my parrot
  seeds" and "hand the horse a carrot" had no one nearby and no emergency stop, and "toss balls to my dog" and "pass
  the treats to my dog" were robots that "hand over" the things, as if to a person.
- New robot reads the person it hands things to from how the request is put together, not only from word lists. Of
  the release review's second hunt, 683 more one-line requests, 165 built a robot with nobody nearby and no
  emergency stop although a person takes the thing or works beside it (18 more said only pets, or nobody, were nearby,
  with an emergency stop), among them job titles not on the lists ("hand
  parts to the fitter", "pass the hose to the fireman", "hand the optometrist the lenses", "pass the reins to the
  jockey"), family and casual words ("bring granma her tea", "bring pa his boots", "pass the salt to sis", "hand my
  bestie the controller"), "hand Nurse Jones the gauze" and names in lower case ("give the pills to mary"), "I'm right
  next to it", "no one near it but me", "the operator takes the part from its gripper", "bring them water", "chuck me
  a beer", "supply the welder with rods" and "hand teh nurse the gauze"; a pet feeder's hand-over to a person said
  pets were nearby ("hand the dog walker the dog food"). 44 more kept the emergency stop but built a pick and place,
  a sort or a transfer ("hand the x-ray films to the doctor", "return the cup to my grandma"), and 9 hand-overs of
  tool guns were refused. None does now. A noun phrase between a
  giving verb and a second one is who gets the second ("hand the bride her bouquet", "give the morning shift their
  parts") unless it is a thing, a place, the robot or an animal; a word that names someone by what they do (the
  riveter, the anaesthetist, the librarian, the postman, the salesperson) is a person unless it is a machine, a
  container or a tool (the capper, the dryer, the coffee maker); and a request that leaves it unsure whether someone
  is there ("a robot that pours me a coffee", "pass the salt over here", "pass the butter", "a robot barista in a busy
  cafe") keeps a person nearby, with an emergency stop, as Morph's assumption. The other way round, "bring them all to
  the bin", "give me a cheap way to move parts", "get me a quote for an arm that moves boxes", "bring her keys to the
  hook", "bring the shopping to her car boot" and "bring the mail to Reception" are no longer hand-overs; a request
  word is a request only when a robot, its design or its job follows, so "bring me my list", "give me the shopping
  list" and "bring me something for my headache" still hand the thing over. Over the
  2,697 phrases of all the release review's hunts, 272 readings change: 268 of the second hunt's requests, two
  hand-overs of pet food to a person (people nearby, not pets) and two Studio labels typed in as requests; none of the
  first hunt's 573.
- New robot no longer reads a vacuum, floor or steam cleaner next to the robot as a person who cleans there: "move
  the boxes next to the floor cleaner" and "a robot that sorts mail next to the steam cleaner" were built with
  people nearby and an emergency stop.
- "Nobody nearby" no longer takes away a person the same request puts next to the robot: "no one is around, it helps
  my grandma eat her soup", "no people around it, it assists the surgeon", "nobody nearby, the nurse takes the tray
  from its gripper", "nobody near it, I stand next to it to load it", "no one near it; an operator loads the parts by
  hand" and "no people around, students will use it" were built with nobody nearby and no emergency stop (only a
  person it hands things to beat "nobody"). New robot now reads every sign of a person first, apart from the task,
  and says no one is near only when there is none. That person keeps the emergency stop also when an answer or a
  later message says it works on its own.
- More hand-overs keep their person and emergency stop: a robot word in the thing handed over ("hand the nurse the
  device", "pass the surgeon the system", "hand the operator the gripper", "a robot that hands the operator the
  machine parts"; "give me a robot that sorts screws" is still a request for a robot), handing things out ("hand out
  the gloves", "pass around the snacks", "hand round the biscuits", "a robot that gives out flyers", "hand the
  leaflets to passers-by"), "a robot that puts the part in the operator's hand", "take her a cup of tea" and "take the
  nurse the tray" were built with nobody nearby and no emergency stop. So were "nobody near the robot, a technician
  calibrates it daily" and "no one near it; customers collect their orders from the tray", and "give the operator a
  system that sorts parts" had no one next to the robot the operator gets.
- New robot no longer reads a department or a packing machine as a person it hands things to, nor baby products as
  children nearby: "bring the bottles to Recycling", "bring the parcels to Goods In", "bring the samples to
  Pathology", "move the trays to the denester" and "pass the bottles to the corker" were hand-overs, and "a robot
  that sorts baby food jars", "move the baby wipes to the shelf", "a robot that dispenses infant formula" and "give
  the baby carrots to the rabbit" had children nearby. A grader, a candler, a stacker or a deburrer may be a person,
  so New robot still hands things to one, with an emergency stop.
- New robot no longer keeps an object on "my SO-101" that the stock jaws can't hold: "use my SO-101 to move light
  bulbs" built an SO-101 with no issue whose own simulation then dropped the 60 mm bulb. In Morph's simulation the
  stock jaws dropped every round object tried from 47 to 60 mm across and flat-sided ones of 36, 40, 44 and 48 mm (a
  38 mm one was placed with 9.6 mm of slip), and placed the ones tried up to 46 mm round and 34 mm flat-sided; for a
  wider object New robot builds a Morph-generated arm and says why, as it does for one above the SO-101's 100 g, also
  when the request is out of scope in another way and New robot offers the nearest robot ("use my SO-101 to stack soda
  cans" kept the SO-101 with a 66 mm, 370 g can, and "... to stack 50 mm wooden blocks" with a 50 mm block).
- Requests that hand someone a tool called a gun are built, not refused as meant to hurt people: "hand the packer the
  tape gun", "pass the rivet gun to the riveter", "hand me the scan gun", "pass me the temp gun" and "hand the
  mechanic the impact gun" (and the tape, grease, soldering, price, screw, barcode and similar guns) were refused.
  A stun gun, an air gun, a blow gun or a gun on its own still is, and so is any gun the request turns on someone:
  "a robot that aims a rivet gun at people", "holds a grease gun to his head", "shoots darts with a blow gun" or
  "tapes people's mouths shut" with a tape gun. That also refuses hostile glue, nail, staple, heat and label gun
  requests the filter had always built ("a robot that points the nail gun at my brother", "a staple gun turret",
  "holds the heat gun on the cat", "a robot that tags people with a tag gun", "a nail gun on a robot to defend my
  house").
