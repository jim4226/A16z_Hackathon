# Changelog

Morph Studio by R Cubed. Releases are git tags (`v0.2.0b1`); the launchers' `--update` (`.\run_studio.bat --update` on
Windows, `./run_studio.sh --update` on macOS and Linux) switches a clone to the newest one. Versions follow
[PEP 440](https://peps.python.org/pep-0440/): `b1` is a beta.

## 0.2.0b1 (beta, not yet tagged)

The first tagged release. Generated designs, wiring and firmware are not bench-validated.

### Known issues

New robot's built-in rules (they read every message when no Claude key is set) read these cautiously, so the robot can
come out bigger or with a generated arm where your SO-101 would do. Check the object Morph assumed in its summary before
you build:
- A smaller size in a later message narrows the objects only as a reply that is just the size ("20 mm", "sorry,
  20 mm") or with "wide", "across" or "in diameter" ("the cylinders are 20 mm wide"). Said any other way ("they're
  20 mm", "the cylinders are 20 mm", "actually 20 mm") it may be a lid's, a question or a lower end, so the larger size
  stays and the stock SO-101 isn't kept in case they are 20 mm. The same goes for a size for some of the objects or a
  part of them, even one the rules read as theirs before ("the small ones are 20 mm across", "the caps are 20 mm
  across"): it never narrows them, but keeps the SO-101 off when its jaws wouldn't hold it, even if it is only a cap's.
  After such a size, a reply that is just a size ("some of them are 20 mm", then "sorry, 40 mm") may correct that one,
  so it only widens them too. Say "the bottles are 40 mm wide" to keep the SO-101.
- A larger size for a part, some of the objects or something else widens them ("50 mm lids" on 40 mm jars plan 50 mm
  jars, "the bins are 100 mm wide" plans 100 mm objects), as before. A size at a neck, an opening or a lid as wide as
  the objects ("the jars are 40 mm at the neck") keeps the stock SO-101 off: their widest part isn't given.
- Naming again an object Morph knows ("the blocks are fragile") never makes it smaller or lighter than Morph's own size
  for it: 28 mm blocks named again are planned as 40 mm ones, on a generated arm. Say "the blocks are 28 mm wide".
- A mass per hour or a batch's is left out only beside the object's own mass given with its name ("50 g cubes",
  "cubes weighing 50 g"), only when every other mass in the message is theirs too, and only when the whole conversation
  is plain: what the robot should do, where things go, the objects' sizes, the rate and who is around, with every
  number a size, a mass or a time. Any other word keeps the rate as the object's mass, so the arm is sized for Morph's
  largest, 500 g: "cubes, 50 g each, at 2 kg per hour", "60 g empty bottles at 2 kg per hour", "some are heavier",
  "jars of honey", "jars in sleeves", "2 kg of them at a time", "up to 120", even a word about something else ("out of
  the bin", "the robot is loud"), and a later message with one brings the rate back. A total is read as one object's
  weight ("130 g in total").
- Number words joined by "and" are added up ("between twenty and forty mm" reads as 60 mm): give sizes in digits.
- "Them" given anything but food or drink ("delivers them parts") is not read as people next to the robot, and a new
  arm is read only when asked for ("build a new arm", "I'd rather have a new arm"), not in a question ("can you design a
  new arm?"). Name who it serves, and say "build a new arm".

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
  description or notes pick the variant (a "12V" STS3215, or a C018 or C047, is the 12 V servo; a note about the other
  arm, "the follower uses the 12V version", doesn't); notes that mention the servo on a screw or a driver board don't
  make it one, and neither does a name for a part of one, whatever follows or what it fits ("STS3215 servo cable 200
  mm", "SG90 servo arm set", "U2D2 for Dynamixel XL430"), nor a kit named for its joint without a motor word ("Pan tilt
  SG90 kit"; a "servo kit" is servos), nor a printed part ("printed", "3D printed", "PLA", "PETG", "ABS", "resin" or
  "STL" anywhere in its name or notes: "Base (PLA, holds STS3215)", "Base MG996R (printed)", except in what a servo
  fits: "MG996R servo for the 3D printed arm"); what a servo comes with or is built with doesn't change what it is
  ("STS3215 servo, horn included", "Feetech STS3215 Servo Motor 12V Metal Case", "DS3218MG"), and neither does the
  joint, arm or motor ID it drives or is, named before it, in brackets or after it ("Elbow flex: STS3215 C001", "Waist
  XM430-W350-T", "Left master arm - XL430-W250-T", "ID 1: STS3215", "[J1] STS3215", "J1 (STS3215)", "STS3215 C018
  shoulder pan", "MG996R: upper arm", "Servo for gripper (SG90)", "Schulter MG996R"); a label is only joint, motion,
  arm, side and ID words (beside makers', kind, colour and filler words), so without a motor word ("Wheel servo STS3215
  12V" is the servo) any other noun keeps a line a part ("Shoulder bracket STS3215", "Lift arm MG996R", "Upper arm link
  (MG996R)", "Gripper rack (SG90)", "Moving jaw SG90", "SG90 gripper"), and a shop's "Upgrade" servo is a servo ("DS3218
  Upgrade 20KG Full Metal Gear Digital Servo"). A line named "Feetech servo", "Gripper servo" or "Servo (x6)" is the
  motor its description names, read the same way (just "Dynamixel" described as "U2D2 interface for XL430" is no motor);
  a part named only by its joint is the motor its MPN names, or its description when that says it is a servo ("Base"
  described as "STS3215 12V servo"; "Gripper" described as "SG90" may be a whole gripper); a "Model" or "Motor" column
  counts as the MPN; and a pack bought once counts its motors ("STS3215 servo ×6 (follower arm)", "Set of 6 STS3215
  servos"). KiCad's grouped CSV and KiBoM parts lists are read too, and a list without prices shows no total. The
  preview shows the MPN and the description. An object whose grip would bend the fingers gets the thicker beam New robot
  would give it. An import whose build isn't feasible says why in its project's chat, with the fixes Morph can build as
  buttons (12 V STS3215 servos, Dynamixel servos, thicker fingers).
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
  egg-picker succeeds in 150 of 200 runs (68-81 %); all 50 failures are the jaws staying shut at release (44 % of the
  runs with the egg -4.7 to 1.5 mm off its mark along its length end that way), and that failure changes with the
  solver's settings. Runs are reproducible from the seed on any number of worker processes, saved as they finish and
  resumable (Ctrl+C keeps them): `morph study egg-picker` (with `--runs 1000` or `--stress`). In the Studio, the
  **Robustness** card under the Simulation tab's pick-and-place result runs a study of the revision on screen (50, 200
  or 1,000 runs, or a stress test with every range doubled), shows its progress live in view in place of the earlier
  result, stops it on request within seconds (the finished runs stay, and it carries on later), says what the result
  means and what it doesn't in plain words ("likely between 68% and 81%", the 95% interval in its tooltip; no "Most
  likely when" for a failure group of fewer than 3 runs; `morph study` prints the same words), replays a failing run in
  the 3D view, and lists every tolerance with where its range comes from. Editors start studies and change tolerances
  (a change marks older studies outdated); every member of a team reads them. Studies run on the Studio's computer, so
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
  grey box, trimmed so it is no closer to the path as it is than the scan, offered only when a simulation with it, run
  during the check as Save and simulate would run it, goes round it (below). A place next
  to a grey box, or one a box was added for (even when the trim moved the box away from it), isn't offered again while
  the path comes no closer there than the box was fitted for; when it does (the robot moved, say), the place is offered
  again with a box fitted to the new path. A box sized or moved by hand is a plain grey box. The same box is never added
  twice, and a box the room holds already is never offered again. The check, the boxes' fit included, has 20 seconds
  (trying the boxes 30 seconds more):
  the review's desk scan takes 9.9 to 11.5 seconds to measure on a 4-core computer at load 1-4, 13.3 to 14.6 at load
  9-10 and 15 to 16 at load 11-12, and trying its box 4.8 to 6.5 seconds more at load 6-10, and a generated room of a
  million triangles 9.8 seconds at load 2 and 11.5 to 12.3 at load 5-10 (below: it now measures the arm's own shapes and
  the motion between its poses).
  The boxes' share of the time is set aside only once the check has listed a near miss,
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
  with a 10 mm cube and a place 50 mm past it on every side could hide a real 8.8 mm near miss; now that place is
  ignored and the near miss is offered.
- A room-scan near miss's row says "at most X mm" unless it is the check's closest place measured along the whole path,
  and its distance is measured exactly at its own closest points: before, a row read "2.2 mm from the wrist" in a check
  cut short where the wrist comes within 2.1 mm, and a box added from it kept 2.2 mm as its fit.
- The room-scan check measures the arm's own collision shapes, and the motion between the poses it takes. It measured
  the arm by points of its surface 1 to 2 mm apart (the carried egg, a capsule, as a 24-sided prism), only at poses 10
  ms apart, so a plate 9.95 mm under the egg read "At least 10 mm", a speck 0.25 mm under a finger "comes within 0.6
  mm", and a speck the jaw passes straight through between two poses "comes within 1.0 mm" (a 0.4 mm cable there, 0.8
  mm). Now each part's convex hull (a box, a sphere and a capsule as they are, a cylinder as the 32-sided prism around
  it) is measured exactly against the scan's triangles, within 0.005 mm of MuJoCo's own distances on scans made of
  boxes, and wherever the arm could come closer between two poses than the closest it found, the move is measured in
  between, so the distance kept is never more than the closest approach anywhere along the path and at most 0.1 mm
  under it before rounding down (the moves the check left alone count with their bounds): those now read 9.8 mm and
  touching. Where the arm touches or passes through the scan, Add box here offers a grey box over that part of the scan,
  untrimmed (at least 20 mm each way, so a speck gets a 20 mm box), offered when a simulation with it goes round it; its
  row says it touches the path unless the whole path keeps at least 0.5 mm from it (a row read "0.1 mm from the path"
  for a box 8.75 mm into it). Where the arm must go (its starting pose, the descent, the grasp and the lift, the
  set-down and the release, the object where it starts and ends) no box is offered, and the row says the arm must reach
  there and when the scan holds the object itself: a box there made the simulation fail ("the object touches Scan near
  miss 1 in the arm's starting pose", with the egg itself scanned at the pick spot). Under 0.5 mm the check's distance
  and its rows' are kept as 0.0 (a speck at 0.197 mm read 0.2), and the scan line in the Studio's progress says the arm
  touches the scan ("Closest to the scan: 0.2 mm" before), and with nothing within 50 mm gives the check's own bound
  ("Nothing within 49 mm"). A box from Add box here is measured against the path
  with MuJoCo's own distances, between the poses too, and its row says how close the path comes to it. More than eight
  places closer than 10 mm are all counted ("12 places closer than 10 mm (the 8 closest are listed)"; it said 8), and a
  check cut short after 606 of 607 poses says "99% of it", not 100%. A grey box's place counts only with a distance of
  at least 0.5 mm and only while the box itself stands about as near the path (a 100 mm cabinet 52 mm from the path,
  saved through the API with a place 50 mm below it and a distance of 0.1 mm, hid a 6 mm near miss), and a near miss is
  the box's only when measured no closer than the box was fitted for (the distance to the nearest of the scan's points
  decided, up to 1.4 mm more, so a plate 0.3 mm nearer than its box was fitted for stayed hidden); when the box the room
  holds is the one that fits there anyway, the place is listed without one, saying the path comes closer than the box
  was fitted to. Measuring between the poses makes the check slower: the desk scan above took 14.7 seconds at load 16
  measured only at the poses; at load 13 to 17 it can run out of time between them, and then says how close the arm
  comes at most and at least, and that the motion between the poses wasn't all measured.
- Add box here is tried before it is offered. A box was offered when it kept clear of the path as it was (a touch box
  when no move a plan can't change met it), but the next simulation plans again with the box in the room, and its other
  ways for the approach, the carry and the way back can meet the box too, or its run strays 1 to 3 mm from its plan into
  a box trimmed to 0.6 mm: on the hunters' bench 8 of 26 touch boxes and 2 of 20 trimmed boxes made the next simulation
  fail ("the wrist hits Scan near miss 1 during the approach at 1.7 s"), after the check had said "a grey box the
  simulation keeps clear of". Now Check placement plans and simulates the pick and place again with each box, as Save
  and simulate would: a box that simulation fails with is not offered and its row says how it fails; one it goes round
  is offered and its row says how close that simulation comes to it, as the simulation after Add box here says (of the
  hunters' cases kept as tests, all 8 failures are caught and the 7 others offered with the clearance their next
  simulation measured). Boxes offered together are tried together too; when there isn't time for that simulation, the
  check says so (not that they fail) and checking again tries it. A check numbers its near misses on past the "Scan near
  miss" boxes the world holds, so a box added never takes another's label (a second "Scan near miss 1" made its row show
  the first box's distance), and a box is tried as itself, not by its label. A row whose trial comes within 0.5 mm of
  another box, or into it, gives no number ("keeps clear of it", and "still fails elsewhere" when the simulation fails
  there; it read "keeps at least -2.4 mm from it"). Trying a box is a simulation run, so a check that offers one takes
  longer: on the review's desk scan 4.8 to 6.5 seconds more at load 6-10 (17.6 to 18.7 seconds in all), with the mount
  moved 15 mm 9.6 to 13.3 seconds more at load 6-12 (21 to 25 seconds in all; 47 seconds in the Studio at load 16). When
  the simulation fails on a box Add box here made, the check names it and says to remove it. Also: a trimmed box is
  fitted to the near miss's own distance (a 2.0 mm near miss's box read 1.9 mm from the path), the scan line in the
  Studio's progress counts every place, not only the eight listed ("8 near misses" where the check said 12), and where
  the scan holds the object itself the check adds "or something within 2 mm of it" (a block 0.3 mm beside the egg read
  as the egg).
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

- The repository is now [github.com/jim4226/morph-studio](https://github.com/jim4226/morph-studio) (it was
  `A16z_Hackathon`, from the hackathon Morph started at; old links and clones still reach it). A new clone's folder
  is `morph-studio`. Keewano and Darwin are written up as integrations: `docs/INTEGRATIONS.md` (it was
  `docs/SPONSORS.md`).
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
- One key filter for everything people type: chat, New robot (the request and every answer), Show Morph the job's
  cards, a project's description, a world's labels and every Teams text field (comments, change requests, reviews,
  releases, branch and display names, script token labels) refuse a Claude, SubQ, Keewano or tool key, a bearer token,
  or a Teams script token, invite or sign-in code before anything is saved, shown or sent. It also refuses a remote
  Studio's own access code (raw or in its access link), a `MORPH_TOKEN` or `ANTHROPIC_AUTH_TOKEN` that is set, other
  services' keys with a fixed prefix (GitHub, GitLab, Hugging Face, AWS key ids, Google, Stripe, Slack, Replicate, Groq,
  xAI, Perplexity, npm, PyPI and more), private key blocks, links with a password (also with a raw `?`, `#` or `@` in
  the password, no user as in `redis://:password@host`, or an e-mail address as the user; a port is no password, so
  `http://10.0.0.5:8765?user=bob@example.com`, a port and a query with an e-mail address, passes, where 9,201 of
  20,000 such generated links were refused; so does minified JSON in which a link starts a string and an e-mail
  value follows, `{"homepage":"https://example.com","email":"robert.smith@example.com"}`, but not yet one with text
  before the link in its string, `{"bio":"Robotics at https://rsmith.dev","email":"robert.smith@gmail.com"}`, nor
  JSON written inside another JSON string with escaped quotes (as log bodies hold it), nor a link in which a `;` or
  other symbol is followed by a time such as `12:30` and later an `@`
  (`https://example.com;time=12:30;by=bob@example.com`, read as a port and a password). Still missed: a password
  with a raw `/`,
  which `openssl rand -base64` passwords hold 22% (12 bytes) to 40% (24 bytes) of the time; one that starts with 1 to
  5 digits and a `?`, `#`, `@`, `"`, `<` or `>`, such as `postgres://admin:2024?Secret@db` (0.5% to 1.6% of 20,000
  generated passwords with such symbols); one with any character from U+2000 on, such as € or an emoji; one under 8
  characters; in a link inside quotes, one holding that quote unescaped (a CSV `""` counts as unescaped) and then
  `,`, `:`, `}` or `]`; and a link written with escaped slashes, `postgres:\/\/admin:...@db`, as PHP's
  `json_encode` writes it) and `.env`
  lines such as `HF_TOKEN=...`; a key glued to Chinese, Japanese or Korean text with no
  space (these keys and JWTs used to slip past), and a tool key after `key=` or inside a link. A key with no prefix of its own pasted alone (an AWS secret key, a bare
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

- New robot's built-in rules (no Claude key) follow a later message. A larger size for the objects is read ("sorry,
  they're 60 mm", "the cylinders are 60 mm", "actually 60 mm"), so the stock SO-101 is no longer kept for 60 mm objects
  its jaws don't hold. A size that may be something else's never narrows or lightens the objects: a part, some of them
  or what they hold ("the caps, 35 mm", "the lids of the jars are 35 mm across", "the pills are 10 mm" in pill bottles,
  "the small ones are 20 mm", "the lids? they're 20 mm", "the lids are 20 g each", "the lids: 20 g"), a question
  ("what if they're 40 mm?") or a lower end ("40 mm and up", "at least 40 mm"). The stock SO-101 is kept only when its
  jaws hold a
  smaller size that may be theirs too (round from 26 mm, flat-sided from 22.5 mm), even when the SO-101 is named only
  later, and a range is checked at both ends ("cylinders between 20 and 40 mm", "40 mm and 20 mm cylinders"). Naming
  the object again ("the eggs are fragile") keeps the sizes you gave instead of Morph's preset (150 g or 60 mm eggs
  were planned as 60 g, 44 mm ones on the SO-101), and naming the SO-101 again after a generated arm was accepted for
  objects below its jaws' minimum ("15 mm beads", then "keep my SO-101") no longer puts it on the 30 mm stand-in. A
  new shape is read ("sorry, they come as 40 mm cubes"); a size said before any object is described ("they're 60 mm")
  only widens the object Morph assumes, never below its generic 45 mm, 100 g one.
- New robot: "actually, build a new arm" after "use my SO-101" builds a Morph-generated arm. An SO-101 robot takes who
  is next to it and where it works from its job, as a generated arm does ("use my SO-101 to tidy up the playroom" has
  children nearby and an emergency stop), and leaving the SO-101 keeps them ("cups to the sink": people nearby), also
  after the nearest robot was accepted ("recycles 80 mm espresso cups") and when the object is named afterwards ("I
  don't have an SO-101", then espresso cups): a kitchen's or a lab's emergency stop too.
- New robot: "them" served food or drink ("a robot that delivers them coffee", "makes them tea") are people next to the
  robot, so it gets an emergency stop.
- New robot: a mass per hour or a batch's beside the object's own ("50 g cubes at 2 kg per hour", "sort 50 g blocks in
  2 kg batches", "sort 2 kg of 50 g cubes") is not the object's mass when the conversation says nothing else about it:
  the arm is sized for 50 g, not Morph's largest. A total stays the object's ("60 g empty, 350 g in total when full" is
  350 g), and so does the rate whenever the conversation says anything more (a part's or what the objects hold, "the
  pills weigh 1 g each", "jars of honey"; "some are heavier", "on average"; "2 kg of them at a time"), also in a later
  message ("that's the empty weight"), and a batch or a total that isn't per hour, day or shift when the arm is said
  to pick up, move or carry them ("pick up 2 kg of 50 g cubes": it may lift them at once); "lots of 300 g jars" and
  "pouches of 300 g every minute" are the objects' own mass.
- Agent tools: a run's card keeps the lines of the calls it has shown when a list of runs the Studio read before those
  calls reaches the page late (on a busy computer, after Allow once): the card showed Working with none of them, or
  Done without them, until the next update.
- Studio on Windows: a revision whose results were read at the moment Windows was finishing saving them (Windows
  refuses to open a file for that moment, and a virus scanner can do the same) is no longer hidden until the Studio
  restarts: a new or imported project's first build could look as if it never finished (an edit was refused with
  "delete the project and make it again"), and after a later build the next edit started from the revision before.
- Import: a parts list saved with Windows line ends (as Excel, Notepad or a Windows checkout saves it) is read as a
  table on every Python: with Python 3.10 and 3.12 one whose quoted cells hold commas ("waist, shoulder (x2), elbow")
  was read as plain text, and the import found nothing it could use.
- New robot on Windows: a built brief's draft is deleted even when the page is reading it at that moment (Windows
  refuses to delete or replace a file that is open; it is tried again for up to a second), so the brief no longer
  stays "building" after its robot is built.
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
  try, as is the first build of a project made while it stops (a New robot request still being answered at Ctrl+C).
  Ctrl+C pressed once more while the stopped Studio's process ends no longer prints a traceback or leaves its lock
  files behind. A start that can't listen (a busy port, a mistyped `--host`) no longer starts that rebuild first, so
  failed starts are no tries either.
- Ctrl+C (or, on macOS and Linux, closing the terminal window or `kill`) while `morph studio` builds the first robot,
  before the Studio starts, stops in one line, "Stopped before the Studio started: ...", with exit status 130, where it printed a
  traceback from inside MuJoCo. A project that start made and didn't finish is not kept, so the next start builds it
  again: one named other than the demos (`morph studio my-arm`) used to be left without a design, and the Studio
  refused to start on it. That includes the first second or so of the build, while Morph first loads trimesh and
  MuJoCo: a Ctrl+C then was swallowed (the Studio started anyway) or ended in a segmentation fault or a TypeError
  that left the project unstartable. The launcher now loads them before it prints "generating the first robot" (or
  "rebuilding with the latest Morph code"), holding a stop until they are loaded (10 s at most), and then stops in the
  same line; `morph robot new`, `morph robot change`, a one-liner and `morph world --starter`/`--off` load them the same
  way before they build. Every start was exposed too, with nothing to build: the check for newer Morph code loaded
  trimesh for so101, and a Ctrl+C then could leave the Studio running with trimesh half-made; that check no longer
  loads it. A stop during the rebuild for newer Morph code says that rebuild was cut off and the finished revision
  is kept (it said the project wasn't finished).
- With two Studios on one projects folder, Reset demo in one no longer deletes the revision the other is building (it
  is refused, and says why), Delete project is refused while another Studio or a `morph study` command runs a
  robustness study of the project, and says which process runs it and on which computer (the study went on in a
  folder moved to the trash; a `morph study` started while Delete project or Reset demo is under way is refused for
  that moment, except on a full disk, where neither can write that hold and both go on without it), a Studio no
  longer deletes the project another running Studio started on, a project
  another Studio deleted and made again under its name opens with its own chat and kept design (the deleted one's were
  shown, written into, and its kept design built as the new project's first revision), and a deleted project's chat no
  longer makes its folder again. `morph studio box-picker`, for a project of your own whose first build was cut off or
  failed, says how to go on instead of building the default robot into it ("no project ... yet").
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
  nobody nearby and no emergency stop (6 more had an emergency stop but nobody nearby), among them "pass the clamp
  to the surgeon", "a robot that hands parts to the operator", "bring them their drinks", "give the pills to Mary",
  "bring dad his slippers", "give the baby her bottle", "get me a drink from the fridge", "a robot that works next to
  the operator" and "bring the meds to her twice a day, she lives alone"; 77 more kept the e-stop but not the
  hand-over ("bring the meds to my grandma"), and 12 that hand nothing to anyone were hand-overs ("bring her cup to
  the sink", "pass the parts to his work cell"). None is now.
  "Alone", "on its own" and "enclosed" say nobody is near only when they describe the robot, and the card then says
  Morph assumed it; a hand-over always keeps its emergency stop, also when the answer or the designer model says it
  works on its own, and New robot doesn't ask about people on one. "Get me a robot that..." is no hand-over, a job
  title before a place ("the operator station", "the nurses' station") is a place, "bring her cup to the sink" moves
  her cup (with her nearby and an emergency stop; "bring her tea in the garden", "bring her medicine on a tray" and
  "bring her tea to the garden" hand it to her), and the tool robot's "bring the tools to his workbench" is a pick
  and place. An animal it gives something
  to is a pet nearby, with an emergency stop, not a person it hands things to: "hand the dog a bone", "give my parrot
  seeds" and "hand the horse a carrot" had no one nearby and no emergency stop, and "toss balls to my dog" and "pass
  the treats to my dog" were robots that "hand over" the things, as if to a person.
- New robot reads the person it hands things to from how the request is put together, not only from word lists. Of
  the release review's second hunt, 683 more one-line requests, 165 built a robot with nobody nearby and no
  emergency stop although a person takes the thing or works beside it (18 more said only pets, or nobody, were
  nearby, with an emergency stop), among them job titles not on the lists ("hand
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
  person it hands things to beat "nobody"). New robot now reads the signs of a person it knows first, apart from the
  task, and says no one is near only when it finds none. That person keeps the emergency stop also when an answer or a
  later message says it works on its own.
- A person named in a typed reply is kept like one named in the first message: "a robot that sorts screws", then "the
  operator takes the bins from it", then "no one works near it" (or the chip "It works on its own") built a robot with
  nobody nearby and no emergency stop, and so did "a robot that sorts pills", "for my mum", "it works alone". 14 of
  the release review's 32 such conversations lost the emergency stop that the same words in one message keep; none
  does now (`tests/fixtures/brief/people_turns.json`).
- New robot reads a person next to the robot from how the request is put together, not only from lists of verbs and
  words: someone at the start of a clause who does something to the robot ("the operator empties it", "the nurse
  swaps its tray", "I hand it the parts and it sorts them", "staff come by to refill it", "pickers drop the totes on
  its conveyor"), someone working while it does its part ("holds the board while I solder", "sorts mail while the
  receptionist answers the phone"), the robot taking something from someone ("takes the parcel from the postman",
  "takes the phone from my hand"), a robot someone uses ("a robot the nurses can use", "it'll be used by
  apprentices"), someone by, among, facing or within reach of it, and places and jobs that leave people around it ("it
  works in a care home", "brings the food to table 5", "a cobot for our packing line", "a robot that waits tables").
  It knows more words for people too: groups ("helps the elderly", "for the homeless", "hands the public leaflets",
  "my family"), family words from other languages ("helps my oma", "bring the tea to nonno"), first names in lower
  case after help, for, with or to ("helps sarah", "bring the pills to siobhan") and in Title Case or capitals, a role
  before a name ("give the chart to nurse kelly"), abbreviations ("to the RN", "the GP", "the IT guy"), job titles
  that end like a machine ("the hairdresser", "the pot washer"), "im", "ive" and "theyre" typed without an apostrophe,
  and a child's age in words ("my five-year-old"). A person's verb before "its" no longer reads as a place they own:
  "my dad fills its tray every morning" and "grandma loads its tray with pills" lost the person in this beta and
  have it again. Of the release review's round-6 hunt (3,033 requests whose readings were written down before it
  ran), 666 of the 2,739 that put a person next to the robot built it with no one nearby or without an emergency stop;
  2 do now, the known misses: "a robot that brings the tea to her room" ("her room" names no one) and the person's own
  things at a machine ("a robot that grabs my printouts from the printer"). A place that belongs to someone named ("put
  the parts into the operator's tray", "place the pills on dad's bedside table", "take the post to dad's study") is
  still no hand-over, but now leaves them nearby, assumed, with an emergency stop; 6 of the hunt's requests with no one
  near the robot get that extra emergency stop ("restock the nurses' station", "move the workers' lockers", "move the
  doctor's bag to the car"). The other way round, a department ("bring the files to Legal"), a machine
  named like a job ("the spot welder", "the bender", "the case packer"), baby products ("move the baby monitor") and a
  workbench (a robot "on my workbench" was a tool hand-over) are no longer read as people. Of 609 requests that name
  no one near the robot, written to check this, two more read a person than before ("I want to mount it on the wall":
  an extra emergency stop) (`tests/fixtures/brief/people_hunt.json`).
- The local harm filter reads what a request does with something that can hurt, not only whether it names a gun.
  Of the release review's round-6 hunt (`tests/fixtures/brief/harm_hunt.json`), 583 of 1,075 harmful one-liners
  were built: tool guns turned on someone in ways the filter didn't read ("aims the glue gun at people passing by",
  "points the nail gun at the raccoons", "a robot with a nail gun on the neighbours", "uses the glue gun to keep the
  cat off the counter", "never aims the tape gun at the wall, only at people"), blades, chemicals and weapons without
  the word gun ("slashes at the cat with a razor", "holds a sword to his throat", "puts ammonia in my husband's
  coffee", "aims a shotgun at the door") and harm or restraint in words it didn't list ("locks the kids in their
  room"). 1,878 of its 3,149 harmless ones were refused, nearly all a tool gun used on a thing named with a body word
  ("aims the paint gun at the screw heads", "holds the soldering gun on the pump body"), and "fires the nail gun into
  the pallet", "a robot that holds a confetti gun" and "restrains parts in a jig while the glue dries". None of the
  harmful ones is built now, and one harmless one is refused: "a robot that hands me the flamethrower" (a weapon's
  name is refused whatever the request does with it). The rules were written against this table, so these figures
  say what it holds, not how often a new phrasing is caught; of the 5,848 harmless requests in the people tables,
  none is refused. A tool held in one message and turned on someone in the next ("a robot that holds a rivet gun",
  then "point it at my brother"; "a robot that holds the heat gun", then "on the cat") was built: each later message
  is now read with the ones before it.
- More people keep their place next to the robot, and its emergency stop. A robot word in the thing handed over
  ("hand the nurse the device", "pass the surgeon the system", "hand the operator the gripper", "a robot that hands
  the operator the machine parts"; "give me a robot that sorts screws" is still a request for a robot), handing
  things out ("hand out the gloves", "pass around the snacks", "hand round the biscuits", "a robot that gives out
  flyers", "hand the leaflets to passers-by"), "a robot that puts the part in the operator's hand", "take the nurse
  the tray", "nobody near the robot, a technician calibrates it daily", "no one near it; customers collect their
  orders from the tray", "give the operator a system that sorts parts" and "a robot that brushes my hair" were built
  with nobody nearby and no emergency stop. "Take her a cup of tea" is a hand-over too (it was a pick and place).
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
- "My SO-101" is kept only for the object the person describes, read in more of the ways people write it. It was kept
  for flat-sided objects read as round ("use my SO-101 to move 40 mm wide phones weighing 90 g", "... soap bars",
  "... tiles", "... square tubes"; bars, phones, tiles, cards, wallets, cases, trays, crates, bins, dice and square,
  rectangular or hex things are now flat-sided, and an object whose shape the request doesn't give is held to the
  flat-sided limit, with a note that saying it is round keeps the SO-101), for masses written with a thousands
  separator ("1,050 g" was built as 50 g, "1,000 g", "1 000 g" and "1.000 g" as 1 g), and for sizes and weights it didn't read
  ("50 mm, 30 g tubes", "tubes (50 mm)", "tubes of 50 mm", "50 mm oranges", "2-inch tubes", "half a kilo", "a pound",
  "300 gm", "three hundred grams"; with two sizes, "60 mm and 30 mm cylinders", the larger now counts). Its stock jaws
  drop a 21.5 mm box in Morph's simulation and let a 22 mm one slip 11 mm, so a stated flat-sided object under 22.5 mm,
  or a round one under 26 mm, gets a Morph-generated arm too, not the SO-101 kept on a bigger stand-in; below the
  gripper's own range (21.5 mm flat, 26 mm round) the generated arm is sized around a 25 mm or 30 mm stand-in and the
  note names the SO-101's limit. For a request that is out of scope in another way, the nearest robot carries the person's
  own object instead of its stand-in: "use my SO-101 to pour out 30 mm cylinders weighing 50 g" got a generated arm
  blamed on a 330 ml bottle nobody mentioned, and "use my SO-101 to cook" kept the SO-101 with eggs whatever the
  object weighed. "I have an SO-101" or "use my SO-101" typed in a later message was ignored, and "I don't have an
  SO-101" read as having one. Of the release review's sweep of 18,753 SO-101 conversations, graded by its own rules,
  3,079 were wrong; none is now (`tests/fixtures/brief/so101_hunt.json`).
- Requests that hand someone a tool called a gun are built, not refused as meant to hurt people: "hand the packer the
  tape gun", "pass the rivet gun to the riveter", "hand me the scan gun", "pass me the temp gun" and "hand the
  mechanic the impact gun" (and the tape, grease, soldering, price, screw, barcode and similar guns) were refused.
  A stun gun, an air gun, a blow gun or a gun on its own still is, and so is a gun the request turns on someone:
  "a robot that aims a rivet gun at people", "holds a grease gun to his head", "shoots darts with a blow gun" or
  "tapes people's mouths shut" with a tape gun. That also refuses hostile glue, nail, staple, heat and label gun
  requests the filter had always built ("a robot that points the nail gun at my brother", "a staple gun turret",
  "holds the heat gun on the cat", "a nail gun on a robot to defend my house"), and "a robot that tags people with a
  tag gun", which was refused before tool guns were let through.
- The local harm filter no longer refuses a safety-minded request (a robot that stops, slows, switches off, or keeps
  people or hands away) or an everyday one whose danger word is the tool's ordinary use. A danger word now has to be
  the object of a hostile verb in its own clause, so "a robot that switches off when someone comes near", "keeps the
  kids away from the nail gun", "hands me the knife when my mum comes home", "cuts the cake", "trims the cat's claws",
  "wraps the baby in a blanket", "straps the baby into the car seat", "pins my hair up", "a heat gun on the arm" (the
  robot's own) and "beat my dad at chess" build, as does a reply read with the request ("plays chess", then "it should
  beat my dad"). It also catches more hostile phrasings: a blade used on a person or pet ("cuts my brother with a box
  cutter", "slices the cat"), something held over a face or forced under water, a ligature tightened around a neck,
  and a hand pushed onto a hot surface. Cutting, slicing or carving something for someone ("cuts me some cheese",
  "carves us the turkey"), a torch shone on someone's hands or at a mouth for the dentist, "shoot me a message",
  whack-a-mole at the fair, a baby held in the bath, a warm towel on a face before a shave, a chain (a necklace) put
  around her neck, a robot that sits on a shoulder, water splashed or confetti thrown in play, and pills, gloves or
  glasses put in or on someone's hand or face build too; the blade, smothering, drowning and ligature rules still
  refuse the hostile forms ("cuts my brother", "holds his head under the water", "presses a pillow over her face",
  "tightens a chain around his neck"). It catches common hostile phrasings, not every one, and can only very rarely
  refuse a harmless one: measured on sets kept aside while the rules were written, it refuses 152 of 205 held-out
  hostile phrasings (74%) and 0 of 292 held-out harmless ones; on the harm and people tables it builds no harmful
  request and refuses no harmless one (one weapon name, "hand me the flamethrower", stays refused by design).
- "My SO-101" (or SO-ARM101) is read only when the request has it to use: "I have an SO-101", "use my SO-101", "I'd
  like my SO-101 to ...", "my dad bought me an SO-101". Each reason it isn't the user's to use is read from the words
  next to the SO-101 itself, so "use my SO-101 to sort faulty parts" or "to pick dead batteries" still keeps it. A
  past or sold one ("I used to have an SO-101", "I sold my SO-101"), someone else's ("my friend has an SO-101", "the
  school's SO-101"), a question whether to use it ("should I use my SO-101?"), a comparison ("can it be like an
  SO-101?", "an SO-101 style arm", "something cheaper than an SO-101", "an alternative to the SO-101"), a replacement
  ("replace my SO-101") or a request for a new arm ("my SO-101 is broken, build a new arm"; not "don't build a new
  arm") no longer builds the stock SO-101.
- "A pound coin", "one pound coins" and "a pound shop" are no longer read as 453.6 g objects (nor "2 pound coins" as
  907 g); "a pound of flour" and "half a pound" still convert. An object is no longer named "weighing" ("cylinders
  30 to 50 mm wide weighing 50 g" is a cylinder). Masses written "150 grammes" or "150 grm", "1/4 lb" (113 g; it was
  read as 4 lb) and "a quarter pound", and sizes written "1 1/2 inch", "3/4 inch", "1.2 in cubes", "0.03 m cubes" or
  2'' are read: before, "use my SO-101 to move 30 mm cubes weighing 150 grammes" kept the stock SO-101 on an assumed
  100 g, for an object it can't lift.
- New robot no longer invents a person next to it from a lower-case job step or process word after "to" ("moves the
  parts to rework", also quarantine, autoclave, decon, deburr, reflow and others), a numbered bench, bed or room in a
  greenhouse or workshop ("moves the seedlings to table 4"), a Title Case tagline word ("A Robot That Sorts Lego For
  Fun", "... Works With Steel"), or a bare "cobot", "collaborative robot" or "HRC" when the request also says nobody
  is around. A person the request names or describes still keeps the emergency stop, and so do a numbered table, bed
  or room whatever the verb ("carries the meds to bed 4", "hands the menus to table 2"), a given name that is also a
  word ("brings tea to Joy") or a capitalised one ("hands the bolts to Buffer"), and a robot whose user is at it by
  construction (a power chair, hospital bed, feeding, assistive, daily-living or mobility robot, a cobot barista),
  even with "nobody near it".
- New robot reads someone served or cared for at arm's length as a person next to the robot, with an emergency stop:
  "pours us coffee", "delivers the nurse coffee", "makes Tom a sandwich", "tops up the guests' wine glasses",
  "refills her glass", "paints my nails", "does her makeup", "puts in my eye drops", "puts the straw in her mouth",
  "ties my shoelaces", "greets customers at the door", "a robot doorman", "a robot whose bin I empty every evening".
- New robot asks what the robot will pick up when the request doesn't say ("I want a robot arm", "something to help
  me in the kitchen"), with keys too: before, SubQ or Claude's guess (a raw egg) was taken as the answer and the
  follow-up asked "Where does it pick up the eggs?" without asking. A guessed object counts only when the request's
  words name it ("pills" for a pill bottle) or SubQ reads an object in them.
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
  isn't available; INTEGRATIONS (was SPONSORS) no longer tells readers to add a payment method for Darwin: Morph never pays.
