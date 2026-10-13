# World mode (beta)

World mode puts the robot in a room and simulates its pick and place there, so you can see whether it fits the place
it will work in before you build it. Open it with **World** in the Robot tab's toolbar (it needs the 3D viewer, so it
is not there when the browser has no WebGL).

## What it does

1. **Choose a room:** a built-in desk, kitchen counter or workbench, or upload your own room scan as one `.glb` file
   (y up, in metres; at most 300 MB and 1.5 million triangles). For a scan, click two points a known distance apart
   and type that distance to set its scale.
2. **Place the robot:** *Place*, then click a level surface; turn it in 15 degree steps or drag the white knob.
3. **Grey boxes** are what the simulation sees: walls, a monitor, a lamp, a mug. Add, move, resize or delete them. For
   a scan, *Find boxes in the scan* proposes boxes around the robot (amber) for you to keep or drop.
4. **People areas:** mark walkways and seats, so the checks can say when the arm reaches into them.
5. **Save and simulate:** saving the world reruns only the simulation stage; every other stage is reused. The pick and
   place then runs against the boxes, with a carry path picked to stay at least 10 mm clear of them with the object in
   hand. *Play in the room* replays it there, and *Walk* lets you walk through the room (WASD and the mouse).
6. **Check placement:** measured checks decide the verdict (*ready*, *needs changes* or *blocked*): the simulation ran
   in this room, both targets are in reach and on the surface, the arm stays clear of the boxes, the base sits on the
   surface, tip-over (a heuristic), people areas inside the reach, anything overhead, and how much of the workspace
   stays clear.

Each revision is simulated in exactly the room its design names: every saved room is kept under its hash in
`<project>/world/saved/`, so editing the room while a build waits never changes what that build simulates. *World off*
simulates on the plain table again. From a terminal:

```bash
morph world egg-picker --starter desk     # save the desk and rerun the simulation in it
morph world egg-picker                    # the room and how the newest simulation went in it
morph world egg-picker --off              # back to the plain table
```

## Optional reviews (only when you ask)

- **AI review (beta)** renders six views of the scene (overview, top-down, along the path, the wrist camera's, from the
  nearest walkway, the base) and sends them, with the measured checks, to Claude: only with an Anthropic key, network
  lookups on, and when you click *AI review*. It adds findings, suggested changes you can apply one by one, and a
  guided tour. **It never changes pass or fail**, and its comments can be wrong.
- **also SubQ** (with a SubQ key) asks SubQ Snap yes/no questions about up to four of the views, such as whether
  something near the path has no grey box. Its answers are shown as "SubQ Snap (AI)" findings and never change the
  verdict either.

Without keys, *Check placement* runs entirely on this computer. What Anthropic and SubQ keep: [PRIVACY.md](PRIVACY.md).

## Honest limits

- **The simulation sees only the grey boxes and the surface.** A scan is visual only: anything without a box is
  invisible to the simulation, and the scale is only as good as the length you typed. Check clearances in the real
  room before running the arm.
- **Not a motion planner.** It picks one carry arc from a fixed family; the approach, descent and retreat are not
  re-planned around obstacles, and contacts there are reported as failures, not avoided.
- The payload sweep ignores the room, and the shake test only reports contacts.
- Walking collides with the boxes and with scans up to 400,000 triangles; larger scans walk against the boxes and the
  floor only, and the panel says so.
- Smooth frame rates are not verified: it was measured only with software rendering.
- One room per project: uploading a new scan replaces the old one before the world is saved.
- Candidate mount positions (A, B, C) exist in the room format but have no buttons yet.
- Needs MuJoCo 3.1.6 or newer (`morph doctor` says which you have).
- **Not in this release:** turning a video into a 3D room, photo-real splat rooms and generative world-model previews.
  Import a room scan as `.glb`, or start from a built-in room.

## Where it is kept

`<project>/world/world.json` (the room), `<project>/world/room.glb` (an uploaded scan) and
`<project>/world/saved/<hash>.json` (each saved version). A fork copies the room along; `/files` doesn't serve it (the
Studio streams the scan from `/api/world`). With Keewano events on, the project's ledger entries for world mode send
only the kind of room, how many boxes and people areas it has, the measured verdict and how long the check took.
