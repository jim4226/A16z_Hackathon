# Morph: recording the demo (about 3 minutes, single take)

The demo is built into the app. Click **Present** (top right, or press **P**). Then each press of
**→** (or Space, or a presentation clicker) runs the next step for real: it switches the view,
types the chat request, flips the build config or asks Darwin. A caption at the bottom says what
Morph is doing, so the video explains itself. Steps that change the robot hold the Next button
until the pipeline has finished, so you can't skip ahead mid-build.

| Key | Does |
|---|---|
| → · Space · PageDown · clicker | next step |
| ← · PageUp | back (only re-shows the view; a chat edit is never sent twice) |
| C | hide or show the caption text (keeps the controls) |
| Esc / P | leave Present mode |

## Before each take

1. Double-click `run_studio.bat`. Wait for the Morph Studio window. The first start after an update
   rebuilds both robots in its terminal window for about two minutes. Maximize the window.
2. Click **Present**, then **Reset demo** on the start card. This deletes the chat edits from the
   last take; both demo robots (egg-picker and so101) go back to their original design in a few seconds
   (up to a minute right after an update, when they are rebuilt). Projects you made yourself are never
   touched, and other browser tabs keep their own projects. If the start card says the window is too
   narrow for the right panel (common at 150 % Windows display scaling), press **Ctrl** and **-** once
   and reopen Present: steps 9 and 14 highlight that panel.
3. Screen recorder: full screen, 1080p, mic on. Press **Start**.

## The 14 steps (what you'll see, and what to say)

The caption is the script. Read it, or say it in your own words.

| # | Step | What happens on screen | Wait |
|---|---|---|---|
| 1 | One sentence in, a whole robot out | Robot tab, the egg-picker arm | |
| 2 | Real kinematics, real parts inside | Motion demo, then X-ray and labels: controller, servo bus, cables, sensors | |
| 3 | Every part is a real part | Explodes to 65% | |
| 4 | Tested in physics simulation | Simulation tab, pick & place plays with live torque | |
| 5 | Change it in one sentence | Types **Swap to Dynamixel servos**; new actuators touch everything, so all 9 stages regenerate; the auto-tour walks the changed tabs | ~30 s |
| 6 | From modules to a custom PCB | Types **Use a custom PCB**; only 6 of 9 stages rerun (sizing, grasp and simulation are reused); the board places, routes and pours on screen | ~30 s |
| 7 | Same robot, another way to build it | Switches the build to **Pilot** (CNC, sheet metal, MJF); heavier metal links re-size the joints (8 of 9 stages) | ~25 s |
| 8 | Darwin: who sells and who builds every part | BOM tab, **Darwin agents**: one search per purchased part and bulk material (filament, plate, sheet, wire), plus PCB fab, CNC, sheet metal and prints, stream in | ~20 s, keep talking |
| 9 | Keewano: the build memory | Simulation tab; the grip-force window in the right panel lights up, with the Keewano calibration line | |
| 10 | The assembly manual writes itself | Assembly tab, autoplay | |
| 11 | Import a real open-source robot | Switches to **so101** (Hugging Face SO-101 from its own URDF) | |
| 12 | Edit someone else's robot | Types **Swap to Dynamixel servos**; holders that no longer fit turn orange | ~30 s |
| 13 | Add a wrist camera | Types **Add a wrist camera** | ~30 s |
| 14 | Time given back | Back to egg-picker; the **Time saved** card lights up: the last change (hours by hand vs minutes) and this session's total. The caption gives the whole-design number (241 h vs about 15 min) | |

Where a step waits, the caption stays up and the button shows "Regenerating… 4/9 stages". Talk over
it: the pipeline strip and the auto-tour are the visual.

## If something goes wrong on camera

- A stage shows an error: keep talking. Every other stage keeps working, and the strip shows exactly
  what failed.
- Darwin is slow or down: its rows are labelled **cached** and come from the last good search
  (shipped with the repo). Move on whenever you like; the search finishes in the background.
- Keewano isn't connected: the caption and the right panel both say so honestly. Add the keys with
  **Keys** (top right; see `docs/SPONSORS.md`), then **Reset demo and rebuild** before the next take.
- The 3D view is slow: press → past the X-ray step, or turn **X-ray** and **Labels** off.
- Anything else: **Esc**, fix it by hand, **P**, then click the step's dot in the progress bar to
  jump back in.

**Numbers to have on screen or a slide:** see `docs/TIME_SAVED.md` (cited table and one-liners).
