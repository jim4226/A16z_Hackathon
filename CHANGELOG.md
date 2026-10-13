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
  and a guided tour, and never changes pass or fail. It is not a motion planner. Needs MuJoCo 3.1.6 or newer.
  `morph world <project> --starter desk` (or `--off`) does the same from a terminal; [docs/WORLD.md](docs/WORLD.md).
- `morph export <project> [--rev r003] [--full] -o file.zip`: the Studio's Export from a terminal, with `team.json` on
  a team. `morph doctor` lists each feature's status: New robot, Import (xacro and STEP support), Show Morph the job
  (whether ffmpeg was found), Teams and World mode.

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
- World mode names its room by id, never a path, and chat can't change it.

### Fixes

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
- `morph studio --team --remote` prints the network address teammates use, and the owner's sign-in link starts with
  it; Share warns when the page that makes an invite was opened at an address only this computer can reach.
- World mode's panel no longer covers the robot's stat chips.
- A tab that switches projects tells world mode and Teams, so the other project's room and team panel no longer stay
  on screen. It listens to the new project before loading it, so a build that ends during the switch is never missed,
  and a project whose first build is still running (or failed) no longer shows the previous project's revision as its
  own: the New robot dialog could report the new robot as done with the old project's numbers, and the robot, its
  tabs, its parameters and its Export button stayed on screen. They wait for the project's first revision now.
