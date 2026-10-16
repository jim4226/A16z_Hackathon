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
  egg-picker succeeds in 140 of 200 runs (63-76 %); 58 of the 60 failures are the jaws staying shut at release (half the
  runs with the egg turned 3-9 degrees end that way), and that failure changes with the solver's settings. Runs are
  reproducible from the seed on any number of worker processes, saved as they finish and resumable (Ctrl+C keeps them):
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
- Remote mode: after 10 different wrong access codes from one address, a `MORPH_TOKEN` you chose isn't checked from
  that address for 15 minutes.
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
- One key filter for everything people type: chat, New robot (the request and every answer), Show Morph the job's
  cards, a project's description, a world's labels and every Teams text field (comments, change requests, reviews,
  releases, branch and display names, script token labels) refuse a Claude, SubQ, Keewano or tool key, a bearer token,
  or a Teams script token, invite or sign-in code before anything is saved, shown or sent. It also refuses a remote
  Studio's own access code (raw or in its access link), a `MORPH_TOKEN` or `ANTHROPIC_AUTH_TOKEN` that is set, other
  services' keys with a fixed prefix (GitHub, GitLab, Hugging Face, AWS key ids, Google, Stripe, Slack, Replicate, Groq,
  xAI, Perplexity, npm, PyPI and more), private key blocks, links with a password (a link's password ends at `/`, `?`
  or `#`, so `http://10.0.0.5:8765?user=bob@example.com`, a port and a query with an e-mail address, passes; 9,201 of
  20,000 such generated links were refused) and `.env` lines such as `HF_TOKEN=...`; a key glued to Chinese, Japanese or Korean text with no space (these keys and JWTs used to slip past),
  and a tool key after `key=` or inside a link. A key with no prefix of its own pasted alone (an AWS secret key, a bare
  hex token) is still not caught unless it is saved in the Keys dialog. New robot used to keep such
  a key in its draft, show it to every tab on the project and send it to SubQ and Claude, and Teams comments let SubQ
  and tool keys through; drafts saved before this stay until their 7-day cleanup. The filter knows keys and codes by
  their random part, so snake_case names and part names that only share a prefix (`mt_shoulder_joint_torque_limit`,
  `ml_inference_pipeline_enabled`, `sk-6x40-stainless-steel-screws`) are no longer refused, and a key saved in the
  Keys dialog is refused whatever its shape. Chip, board and model names (`mt_stm32f405_firmware_build`,
  `ml_resnet50_backbone_weights`, `ml_YOLOv8n_grasp_detector`, `mt_STM32F405RGT6_firmware_build_config`), numbered
  names (`mt_servo1_servo2_servo3_torque_limit`, `mt_left_knee_arm2_torque_limit`), camelCase names
  (`mt_shoulderJointTorqueLimit`, `mt_maxPWMFreq_default_hz`, `mt_camera_offsetX_mm_value`), versioned names
  (`ml_yoloV8_grasp_detector`, `ml_gripperV2_checkpoint_final`, `mt_servoID3_offset_degrees`,
  `mt_so101Arm_follower_calib`, `ml_GraspNet1B_weights_file`, `mt_Servo1_torque_limit_value`), numbers with a unit or
  a size (`mt_motor_max_speed_3000rpm`, `mi_camera_intrinsics_1280x720`), a part code beside words of its length
  (`mt_SG90_horn_trim_offset`) and lower-case names with a commit hash or a run id (`ml_policy_weights_3f9a2c1e`) pass
  too (these were refused whole), and so does a name as long as a Teams invite code (`ml_policyV1_final_weights`):
  Morph draws a code again when it, a piece of it cut short, or the code with text joined to it (`mi_<code>-thanks`)
  could read as words, so every code it makes is still refused. A key written in groups (`sk-live-4f8a-91bc-77de-12ab`)
  is refused, now also with no word in front (`sk-4f8a-91bc-77de-12ab`, under the 20 characters the `sk-` rule
  needs). The filter goes by how random text looks, so it misses some keys and refuses some names
  (`tools/key_filter_rates.py`, 100,000 random keys per shape): keys written in groups of lower-case letters and digits
  are missed when every group with a digit reads as a numbered word, 2.02% of `sk-live-xxxxx-xxxxx-xxxxx` keys that
  have a digit (2.75%, about 3 in 100, of all of them, counting those with no digit, which an `sk-` key needs), 1.69%
  (2.28%) of `sk-live-xxxx-xxxx-xxxx-xxxx` and 0.86% (1.14%) of `sk-live-xxxxxx-xxxxxx-xxxxxx`; the first two shapes
  in capitals and digits 2.00% (2.73%) and 1.54% (2.09%), in hex 0.27% and 0.18%; random base64url keys of 20 to 32
  characters not in groups 0.009% or less of those with a digit. Names still refused (a name under the mi_, ml_ or
  mt_ prefix only once 20 characters or more follow it): a few model spellings with two-letter pieces or no common
  word (`ViT`, `UNet`, `MiDaS`, `LoFTR`, `ConvNeXt`, `SmolVLA`, `EfficientNetB0`), part numbers in mixed case or
  starting with a digit (`ATmega328P`, `nRF52840`, `17HS4401`), whole part numbers after `sk-`
  (`sk-STM32F405RGT6-breakout-board`), camelCase with `I2C`, a part code beside unusual words of its length, and
  capitalized names made of a numbered series and one word of three letters or fewer, such as
  `mi_Max1_Max2_Max3_Max4_Hip` or `mi_Joint1_Joint2_Joint3_Joint4_Max` (all 571 such names among 4,000 generated
  series of four and a word; none of the other 3,429, whose word has four letters or more and which pass, as
  `mi_Cam1_Cam2_Cam3_Cam4_Gain` does).
- Remote and team Studios: one address may hold at most 16 connections that haven't signed in. Someone without the
  access link could lock every signed-in person out by holding 64 idle connections; signed-in pages don't count, so
  teammates behind one router don't run into it in normal use. Many addresses together can still fill the slots
  (SECURITY.md).
- Wrong codes from one address no longer lock out the person there who has the link: Morph's own access code and
  every Teams invite, sign-in link and script token are random, so a right one is always checked and works (only a
  `MORPH_TOKEN` you chose still makes that address wait). A script's live updates no longer end when someone at the
  same address mistypes links. Each address keeps only its last 10 wrong codes, so a flood of them costs the Studio
  no more memory or time per request than ten.
- Error messages a browser gets (a job that failed, a chat message that couldn't be read, Find agents, a robustness
  study, a build stage, an import, a video ffmpeg couldn't read, the Teams preview and Apply, New robot, a tool that
  didn't load, an unexpected server error) name files from the projects folder (`projects/egg/r002/...`), never where
  the server keeps them; the terminal still prints the whole error.

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
- A quick second edit of the same setting, made while the first still builds, builds on it: the newest choice used to
  come back as a conflict with your own first edit and be held back. A change another tab or teammate made meanwhile
  is still asked about, by name ("Build changed after r001") instead of its key.
- A change waiting for Apply or Drop keeps its buttons after a reload and in tabs opened later; once anyone answers it,
  every other tab's buttons go (a late click used to fail), and a viewer is told an editor can answer instead of being
  shown buttons.
- With its output going to a log file or a pipe (nohup, a service), the Studio's access link and the owner's sign-in
  link reach it at once: they used to wait in a buffer, and a Studio stopped with SIGTERM never wrote them.
- `morph subq --lint` sends nothing while network lookups are off or `MORPH_OFFLINE=1` (it sent the request text to
  SubQ anyway). `morph ask` tells "not configured" apart from "network lookups are off" and a failed query, and exits
  1 without rows; `morph study` says why it can't study a project or revision before announcing a study. Relaunching
  on a Linux server without a screen while a Studio runs no longer opens a text browser in the terminal.
- A crash or a full disk that cut off the last line of a log no longer loses the next one too: the next chat
  message, ledger entry (the Time saved card disappeared), robustness run or Teams record joined the cut-off line and
  was lost with it. The team and tool audit logs report such a line as cut off by a crash or a full disk instead of
  "edited by hand", and a stop between writing an entry and its `audit.head` is no longer flagged as tampering.
- A results.json left empty or cut off (a power cut, a crash while it was saved) no longer stops the launcher with a
  traceback, or breaks its project with a 500 on every request and a new cut-off revision on every edit: when the
  Studio starts, such a revision and any build a stop cut off are set aside (named in the terminal; after a day they
  move to `projects/.trash/`), a revision Morph can't read is never shown or built on, and every record is synced to
  the disk before it replaces the old one. A build that fails before it finishes (a full disk) removes its revision
  folder, so the space comes back; a full disk reads "The disk is full" in the chat and in dialogs, and in one plain
  line in the terminal (a Studio starting on a full disk printed a traceback for the agents' tools, each chat and
  queue line and the failed rebuild, and the first run the disk refused kept the agents' other runs and drafts from
  being marked stopped; `morph studio --team` didn't start at all, with a traceback for the owner's sign-in link: it
  starts without the link now and names `morph team link` for one once there is space), and a full disk no longer
  leaves Teams and tool files "busy" for 30 seconds afterwards. A build another Studio or the CLI is still
  running is never set aside.
- A Studio stopped during a build (Ctrl+C, a closed window, a crash) no longer strands what it was doing. When it
  starts again, a new, imported or forked project whose first build was cut off is built again (labelled "rebuilt
  after a stop"; it stayed empty for good, and New robot's Build kept answering with the dead project); edits that were
  waiting or building are named in the chat so they can be sent again (they disappeared without a word); a change
  waiting for Apply or Drop waits again; a Teams change request that was applied but not built is open again, with
  why, for whoever applied it and its author (it read "applied" with no revision and could not be applied again or
  closed); and a robustness study waiting for its turn is listed as stopped, to carry on (it vanished). Whether
  another Studio on the same projects folder still runs is told by a lock the system frees when it stops, so a
  stopped Studio's work is never left alone because its process number was reused after a reboot or the computer's
  name changed. A first build the Studio stopped during twice is not started again on its own (one that crashes the
  Studio would crash it at every start): the chat and the terminal say so, and "build it again" in the project's chat
  tries once more. A team Studio starting while another one runs on the same projects folder leaves the change
  requests that one is building alone (it reopened them, told their people they weren't built, and the finished build
  left its revision unrecorded). Closing the Studio with Ctrl+C during a first build is no try: only a crash, a
  SIGKILL (`kill -9`, or the system ending it when memory runs out), a power cut or an error that stops the Studio
  counts, and the chat no longer suggests the design stopped the
  Studio. "Build it again" sent again while that build runs is answered at once (it is already being built), not with
  "I couldn't map that" once it ends. A first build that ends with an error (not a stop) can be tried again too:
  "build it again" in its chat builds it once more (it was answered "Wait for its first build", for a build that would
  never come). With two Studios on one projects folder, a project whose first build the other Studio is running is
  answered "Another Morph Studio on this projects folder is building <project>'s first design; wait for it there." (it
  was told Morph kept no design and to delete the project), "build it again" there queues nothing (two Studios could
  each build a first revision, r001 and r002), Delete project is refused while the other Studio builds it or has
  changes of it waiting (it moved the project to `.trash` mid-build), and New robot's Build on that project's brief no
  longer moves it to `.trash` to make a second one. A first build left open by a Studio that stopped while this one
  ran is built by "build it again" here (it read "Morph kept no design"). A full disk when the Studio starts no longer loses a
  cut-off first build: the stopped Studio's journal, its only record, used to go before anything replaced it, so a
  rebuild and an error record the disk refused left only "delete the project" (the design survives now until it is
  written down again, and a first build that fails on a full disk stays journaled). "Build it again" also builds from
  a revision set aside unfinished when nothing else kept the design, and two Studios starting at once build a cut-off
  first design once (they could build it twice, r001 and r002). Closing the Studio's terminal window (SIGHUP), a
  logout or a `kill` (SIGTERM), and Ctrl+C during the start-up while it rebuilds a cut-off first build, are clean
  stops too (on Windows, closing the console window, logging off or shutting down is handled the same way, but that
  has not been run on Windows yet): they
  used to count as crashes (twice parked the design, "The Studio stopped twice ..."), and Ctrl+C there ended in a
  traceback. Ctrl+C pressed twice or more while the start-up rebuilds it, or again while the Studio writes its stop
  down, is a clean stop as well (the second press counted as a crash, so one real crash and a double Ctrl+C parked the
  design), and a first build that gets its turn while the Studio is stopping is cut off by that stop, not counted as a
  try. A start that can't listen (a busy port, a mistyped `--host`) no longer starts that rebuild first, so
  failed starts are no tries either.
- With two Studios on one projects folder, Reset demo in one no longer deletes the revision the other is building (it
  is refused, and says why), Delete project is refused while another Studio or a `morph study` command runs a
  robustness study of the project, and says which process runs it and on which computer (the study went on in a
  folder moved to the trash), a Studio no longer deletes the project another running Studio started on, a project
  another Studio deleted and made again under its name opens with its own chat and kept design (the deleted one's were
  shown, written into, and its kept design built as the new project's first revision), and a deleted project's chat no
  longer makes its folder again. `morph studio <project>` on a project of your own whose first build was cut off or
  failed says how to go on instead of building the default robot into it ("no project ... yet").
- Teams: a change request applied in a Studio that stopped before building it no longer stays "applied" with no
  revision in another Studio that keeps running on the same projects folder (Apply and Close answered "already
  applied" until some Studio restarted): that Studio opens it again, with why, as soon as the requests are listed,
  applied or closed. A request whose revision was written just before the stop is recorded built there (it was opened
  again, and applying it again ended "nothing to change"), and a request whose change the design already has is
  applied at the head revision (it stayed open with "nothing to change" for good); its inbox notice, record, activity
  feed and card say it needed no build because that revision already has it (they said it built the revision).
- A project whose `meta.json` was cut off no longer disappears (listed nowhere, every request 404, its name still
  taken, impossible to delete from the Studio): it stays in the project menu, and when the Studio starts it gets a new
  `meta.json` made from its revisions, with the damaged one kept beside it.
- Teams: a damaged `team.json` (cut off, or a bad hand edit) is no longer read as "no team": `morph team members`
  printed nothing, the team Studio said "The team has no owner", and the suggested `morph team init` replaced every
  member, invite, token and the policy. Now every team command and the team Studio name the line and column that
  can't be read, nobody signs in, nothing writes over it, and Morph keeps the copy from before its last change as
  `team.json.bak` to put back.
- Damaged settings fail closed: a `.settings.json` that can't be read used to turn network lookups, Darwin sourcing
  and agent tools back on without a word (PRIVACY.md promises Off keeps everything on this computer). Now every
  switch is off, and the Keys dialog and `morph doctor` say why, until you set them again. Agent tools you turned off
  stay off when the tools' state file is damaged, and a damaged `tolerances.json` is reported instead of studies
  running on the defaults and the next edit writing over it.
- Import: a Studio stopped during an upload no longer leaves the import session claiming a file it doesn't have (it
  counted the whole file before it arrived, so Find said there was nothing to import and the size limit was used up);
  a file counts once it is all there, and the half-written file is removed when the Studio starts again.
- Ctrl+C stops the Studio at once while Darwin lookups hang (a server that accepts and never answers): the Studio
  waited for every queued lookup to time out in turn, about a minute for one Find agents, and ignored Ctrl+C meanwhile.
  Network lookups (Darwin, Test keys, BoardRepo) now run on threads the process never waits for when it stops.
- Stopping the Studio with Ctrl+C no longer ends now and then with "Segmentation fault" (exit code -11) after the
  launcher built a robot before serving: the stopped Studio's process ends without the interpreter's shutdown, whose
  clean-up of the 3D libraries crashed. Everything was already saved. A build still running at Ctrl+C now stops at
  once, where the interpreter could wait for it to finish; the next start picks it up like any build a stop cut off.
