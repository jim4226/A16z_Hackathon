# World mode (beta)

World mode puts the robot in a room and simulates its pick and place there, so you can see whether it fits the place
it will work in before you build it. Open it with **World** in the Robot tab's toolbar (it needs the 3D viewer, so it
is not there when the browser has no WebGL).

## What it does

1. **Choose a room:** a built-in desk, kitchen counter or workbench, or upload your own room scan as one `.glb` file
   (y up, in metres; at most 300 MB and 1.5 million triangles). For a scan, click two points a known distance apart
   and type that distance to set its scale. The robot stays hidden until you place it (it would stand at the scan's
   origin), and the room is not simulated until you save it.
2. **Place the robot:** *Place*, then click a level surface; turn it in 15 degree steps or drag the white knob.
3. **Grey boxes** are what the simulation sees: walls, a monitor, a lamp, a mug. Add, move, resize or delete them;
   select one to type its position and size in millimetres from the robot's base. For a scan, *Find boxes in the
   scan* proposes boxes around the robot (amber): keep or drop each one, or all of them, and use the scanned surface
   the robot stands on.
4. **People areas:** mark walkways and seats, so the checks can say when the arm reaches into them.
5. **Save and simulate:** saving the world reruns only the simulation stage; every other stage is reused. The pick and
   place then runs against the boxes. Every move of it (the approach, the descent to the object, the grasp, the lift,
   the carry, the set-down, the release, the rise and the way back to rest) is checked against the boxes every 10 ms,
   with the object while it is carried. The carry path is picked to stay at least 10 mm clear; a move that would come
   closer tries its alternatives: the approach and the way back along the carry path (the hand closed to the object's
   width on the way back), the approach from higher up, the release with the hand opened only 4 mm past the object,
   and the way back straight up first. The first that keeps 10 mm wins, else the one with the most room. A move that
   would still pass into a box fails the simulation, naming the move and the box. The line under the room's name says
   how the newest simulation went, how close the arm came to a box, and which other way it took.
   *Play in the room* replays it there, and *Walk* lets you walk through the room (WASD and the mouse, or drag to
   look where the browser won't lock the mouse): you start on a walkway when there is one, else on the floor a step or
   two from the robot, with it in sight.
6. **Check placement:** measured checks decide the verdict (*ready*, *needs changes* or *blocked*): the simulation ran
   in this room, both targets are in reach and on the surface, the arm stays clear of the boxes, the base sits on the
   surface, tip-over (a heuristic), people areas inside the reach, anything overhead, and how much of the workspace
   stays clear. With a room scan, it also measures the simulated path against the scan itself (below). Every distance
   these checks keep or show is rounded down, never up: to 0.1 mm under 10 mm and to the millimetre from there, so
   9.97 mm reads 9.9 (never the 10 mm that is OK) and 30.68 mm reads "at least 30 mm"; room that is missing (the base
   over the edge, the arm into a box) is rounded up.

Each revision is simulated in exactly the room its design names: every saved room is kept under its hash in
`<project>/world/saved/`, so editing the room while a build waits never changes what that build simulates, and a
design naming a room this project doesn't have fails its simulation stage instead of running in another one. *World
off* simulates on the plain table again. From a terminal (set up as in
[Commands in a terminal](../README.md#commands-in-a-terminal)): save the desk and simulate again in it; show the room
and how the newest simulation went in it; go back to the plain table:

```shell
morph world egg-picker --starter desk
morph world egg-picker
morph world egg-picker --off
```

## Clearance to the room scan (a warning)

The simulation sees only the grey boxes, so *Check placement* also measures how close the simulated arm (and the object
while it is carried), posed every 10 ms along the newest simulation, comes to the uploaded scan. It looks at the part
of the scan near the arm: the scan's triangles are covered by points of their own that a k-d tree searches, and the
closest ones get exact point-to-triangle distances; on scans made of a world's own boxes it agrees with MuJoCo's own
distances to within a few tenths of a millimetre. The result is a warning, never the verdict: "Measured against the
scanned surface, which can be noisy or have holes. A warning, not a verdict." It names the part of the arm and the
time, and says *touches or passes through* below 0.5 mm.

Each place where the arm comes within 10 mm of the scan is listed with **Add box here**: a grey box over that part of
the scan (standing on the surface when the scan under it does), added to the room. A box around a round thing sticks
out at its corners, so the box is measured against the same path first and trimmed on the sides that face it until it
is no closer to the path than the scan there (the row says how close it is, "at most" unless it is the check's closest
place measured along the whole path, and "trimmed" when it was): adding it never
makes the clearance worse. When a box that keeps clear would be thinner than 5 mm, none is offered: move the robot or
the thing. *Save and simulate* then plans around it, and the next check measures the new path. A near miss within 10 mm
of a grey box is that box's, and so is the place a box was added for, even when the trim moved the box away from it (a
vase's rim just under the arm): the same place is not offered again, and the check says it has a grey box already. A box
added this way keeps how close the path came there and which revision's path it was fitted to: when a new path comes
closer at that place (the robot moved, say), the place is offered again with a box fitted to the new path. Sizing,
moving or turning such a box by hand, or setting the room's scale again, makes it a plain grey box. The place a box
keeps reaches past it only on the sides that were trimmed, and no further than the box is big, so a small box saved
from elsewhere can't claim a big place. The same box is
never added twice, and a box the room holds already is never offered again.

The check, the boxes' fit included, has 20 seconds. On a 4-core computer the review's desk scan, a vase 9 mm from the
arm's path, takes 7.5 to 8.5 seconds at load 4-7 and 10 to 11.5 at load 11-13; the same desk with the wrist touching the
vase 9 to 10 seconds at load 2-3 and 17 at load 8; a generated room of a million triangles 10.5 seconds at load 7. When
a check can't measure the whole path in time, it never claims more room than it measured: it says how close the arm
comes at most (the closest distance it did measure: "The wrist comes within at most 4.6 mm of the scanned surface"), or
that there wasn't time to measure it, and that only part of the path was measured. It says whether checking again when
the computer is less busy may finish it, or whether, at that pace, it would take more than twice as long. Its answer is
kept until the room, the scan or the revision's simulation changes, except an answer cut short, one whose box ran out
of time to be fitted, or one that ran out of time before every near miss was listed: *Check placement* measures those
again.

- Scan the room without the robot and without the object it picks up: anything scanned where the arm or the object
  goes counts as a near miss.
- Scanned triangles that rise no more than 10 mm above the surface the robot stands on are left out (that is the
  surface itself).
- A scan only shows the sides it saw: a near miss behind a hole is not found, and a box from *Add box here* is at least
  20 mm thick, grown away from the arm.

## Robustness studies in a room

A [robustness study](ROBUSTNESS.md) of a revision simulated in a room also varies where the robot is mounted (+/-10 mm
and +/-2 degrees) and where each grey box is (0 to 15 mm, in a random direction), and a run that touches a box fails as
*Hit a box in the room during* the move it was in. Under the study's tolerances, **Only the room varies** keeps the
object and the hardware nominal, so every failure is the room's. A carton wall 12 mm from the open jaws at the place
spot, which passes the 10 mm check in the nominal run: 15 of 200 runs hit it (95 % CI 4.6 to 12 %), all during the
release, most often with the mount 4.5 to 10 mm off in y.

## Optional reviews (only when you ask)

- **AI review (beta)** renders six views of the scene (overview, top-down, along the path, the wrist camera's, from the
  nearest walkway, the base) and sends them, with the measured checks, to Claude: only with an Anthropic key, network
  lookups on, and when you click *AI review*. It adds findings, suggested changes you can apply one by one, and a
  guided tour. **It never changes pass or fail**, and its comments can be wrong.
- **also SubQ** (with a SubQ key) asks SubQ Snap yes/no questions about up to four of the views, such as whether
  something near the path has no grey box. Its answers are shown as "SubQ Snap (AI)" findings and never change the
  verdict either.

Without keys, *Check placement* runs entirely on this computer, and *AI review* says it is off and shows the measured
review: no pictures are rendered or sent. What Anthropic and SubQ keep: [PRIVACY.md](PRIVACY.md).

## Honest limits

- **The simulation sees only the grey boxes and the surface.** A scan is visual only: anything without a box is
  invisible to the simulation, and the scale is only as good as the length you typed. Check clearances in the real
  room before running the arm.
- **Not a motion planner.** Every move is checked every 10 ms, but the only ways around a box are the fixed ones above:
  a carry arc from a fixed family, the approach and the way back along it, from higher up or straight up first, and the
  release opened less. When none keeps clear the simulation fails and says which move and which box; it doesn't
  search for other paths. The pick and place spots never move: a box right over the object fails the run.
- The payload sweep ignores the room, and the shake test only reports contacts.
- Walking collides with the boxes and with scans up to 400,000 triangles; larger scans walk against the boxes and the
  floor only, and the panel says so.
- Smooth frame rates are not verified: it was measured only with software rendering. The view draws only while
  something moves, and walking stops when you leave the Robot tab. The World panel steps aside on the other tabs (the
  Simulation tab replays in the room without it) and is back, as you left it, on the Robot tab.
- One room per project: uploading a new scan replaces the old one before the world is saved.
- Candidate mount positions (A, B, C) exist in the room format but have no buttons yet.
- Needs MuJoCo 3.1.6 or newer (`morph doctor` says which you have).
- **Not in this release:** turning a video into a 3D room, photo-real splat rooms and generative world-model previews.
  Import a room scan as `.glb`, or start from a built-in room.

## Where it is kept

`<project>/world/world.json` (the room), `<project>/world/room.glb` (an uploaded scan) and
`<project>/world/saved/<hash>.json` (each saved version). A design names its room only by id and hash, never by a
path or file name. A fork copies the room along; `/files` doesn't serve it (the Studio streams the scan from
`/api/world`). An export carries a revision's files but not the room, so importing it simulates on the plain table
and says so: set the room up again in World mode. With Keewano events on, the project's ledger entries for world
mode send only the kind of room, how many boxes and people areas it has, the measured verdict and how long the check
took.
