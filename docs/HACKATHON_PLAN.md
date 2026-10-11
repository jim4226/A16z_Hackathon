# SpeedHacks plan: Morph, one use case, end to end

**Event:** Sun Oct 11, 9:00am to 9:00pm, 1025 Sansome St. Finalists demo live.
**Theme:** *Give people their time back.*
**Our claim:** a robotics engineer goes from "my gripper can't hold this" to a
validated, fabricated part in under an hour instead of days. Today we prove it once, on
real hardware, with measured numbers.

## Judging rubric → what we show

| criterion | weight | our evidence |
|---|---|---|
| Problem & time saved | **40** | one person, one task, measured before/after: `morph time` + the report's time card. Human setup/review/corrections are logged with `morph clock`, so they count, per the rules |
| Technical execution | 25 | it runs live: CAD → MuJoCo → G-code/STL in ~30 s, 8 passing tests, partial recompute |
| Market & path forward | 15 | every robotics team, lab, and hardware startup makes custom end effectors and brackets; the ledger dataset compounds; next part types + real-robot sim |
| Originality | 10 | the change-propagation graph + measured-vs-predicted ledger (no public dataset links design → fab → test) |
| Product quality & demo | 10 | printed fingers in hand, flat vs contour lift on video/live, report on screen |

**Pick a person:** a robotics engineer (or grad student in a manipulation lab) whose arm keeps dropping one object.
**Frustrating task:** design, check, simulate and fabricate custom gripper fingers for it, which today takes a day across 4 tools.
**Show the difference:** the time card. Before = baseline estimates per step (`morph/timestudy.py`), **replace at least one with a real timed manual run today**: have a teammate model the contour finger in Fusion/Onshape with a stopwatch, put the minutes in `projects/<name>/baseline.json` as `{"cad_first": <min>}`.
After = Morph wall-clock (auto-logged) + machine time + every human minute logged:

```bash
morph clock can spec 2          # typing the need
morph clock can review 6        # reading the report, picking a design
morph clock can test 10         # bolting on + 10 lifts
morph clock can corrections 18  # e.g. one reprint after measuring hole spacing
morph time can r004             # prints the before/after table
```

## The one use case

Stock parallel-gripper fingers vs. a 66 mm soda can lying on its side. Morph:

1. reads one sentence and writes the spec,
2. rejects its own first design (too flexible, SF 0.9),
3. screens 324 variants and simulates three pad styles,
4. picks a contour pad that needs **~2.7× less grip force** than flat fingers in simulation (2.3 N vs 6.3 N),
5. emits the print file, which we **print at the venue**, bolt on, and
6. test flat vs. contour on the real gripper. The measured result goes back into the ledger.

The software pipeline already works (`python -m morph.cli demo`, about 30 s). The rest of the day goes to the
physical loop and the pitch.

## Hardware path (decide by 11:00)

| option | needs | fab time | notes |
|---|---|---|---|
| **A. FDM print (default)** | any desktop printer (team-owned Bambu/Prusa) + PETG/PLA | ~15–20 min for 2 fingers | `r004/fab/plate.stl`. Print flat, no supports |
| B. Laser cut Delrin/acrylic | access to a CO2 laser | ~3–4 min | `cut.svg`, kerf already compensated. Useful as the "what-if" slide even without a laser |
| C. Desktop CNC (GRBL) | router + 6 mm Delrin/Al plate | 7 min (Delrin) / 23 min (Al) | `job.nc` + `morph send --grbl PORT`. Always `--dry-run` first |

Gripper to mount on, from best to fallback: an SO-100/SO-101 or similar hobby arm's
gripper; a servo parallel-gripper kit; a 3D-printed rack-and-pinion jaw on a stand,
driven by hand or a servo. Measure the M3 hole spacing on the real jaw first, then
`morph change can finger.mount_hole_spacing_mm=<value>` and reprint.

## Measurement protocol (what makes the demo credible)

- **Dimensional:** calipers on finger length and pad depth → `--length`.
- **Fabrication time:** printer's reported time → `--fab-minutes`.
- **Grasp:** 10 lift attempts each for flat and contour fingers, same gripper, same can → `--grasp 9/10`.
- **Holding force (optional):** with the can gripped, pull down with a luggage scale until it slips. Or lower servo torque step by step → `--min-force`.
- Run `morph measure` live. The report shows predicted vs. measured.

## Schedule (Pacific)

| time | who | what |
|---|---|---|
| 10:30–11:00 | all | confirm printer and gripper. Measure jaw mount. Set `ANTHROPIC_API_KEY` |
| 11:00–11:30 | HW | `morph change` mount spacing → print **flat** baseline (r002) and **contour** (r004) |
| 11:00–13:00 | SW | stretch goal 1 (below). Keep `demo` green, `pytest -q` |
| 12:00–13:30 | HW | assemble, run grasp trials, log with `morph measure` |
| 13:30–15:00 | all | second iteration: apply what measurement taught us (e.g. add friction tape, adjust pad depth). Reprint once |
| 15:00–17:00 | SW | stretch goal 2. Polish report. Record a backup video of the physical test |
| 17:00–18:30 | all | pitch deck (5 slides) + 3-min demo rehearsals ×3 |
| 18:30– | | submit, demo |

## 3-minute demo script

1. **Pain (20 s).** "Custom fingers for one object: CAD, sim, CAM, slicer, test. That's days, and every change starts the loop over."
2. **Live (70 s).** Type the need → `morph demo` → open the report: baseline fails, explorer finds contour, sim GIF, the change-propagation chips ("only analysis + sim reran").
3. **Physical (45 s).** Hold up the printed fingers. Show the video or do it live: flat fingers drop the can, contour fingers hold it. Show the measured-vs-predicted row.
4. **Time card (20 s).** Before vs after, with our own review/test/correction minutes counted. Say which "before" numbers were timed today and which are estimates.
5. **Why it compounds (15 s).** Every revision records design → prediction → measured outcome. That dataset doesn't exist publicly, and it's what lets Morph's predictions improve over time.

## Stretch goals (in order)

1. **Agent loop:** Claude tool-use driving `new / explore / change / compare`, so "make it lighter but still hold 500 g" iterates by itself.
2. **Real robot in sim:** swap our box hand for a MuJoCo Menagerie gripper/arm model (license varies per model).
3. **Object library:** pick objects by name from YCB-style dimension and mass tables instead of typing numbers.
4. **Second part type** (e.g. a sensor bracket) to show the graph generalizes.

## Rust

Not today. MuJoCo and the Python CAD stack cover everything the demo needs, and a
Rust rewrite would cost the physical demo. If performance matters later, the
right first port is the closed-form screener + dependency graph (pure math, easy to
move to Rust and expose via PyO3); Burn is an option if we train a surrogate model on the ledger.

## Sponsor prizes

Five "best use" awards, $1,000 each: Alike, Darwin, Dial, Keewano, Sparta. Partner tools
are optional for the overall prize, and meaningful use can win more than one award. We
couldn't verify what each product does online. Ask each partner at their table, then
plug the ones that fit into a real step (need intake, research, review/approval, or the
measurement ledger) rather than bolting them on.

## Risks and fallbacks

| risk | fallback |
|---|---|
| no printer at venue | laser/CNC files as artifacts + pre-recorded print. State plainly that preparation ≠ fabrication |
| gripper force unknown | measure with luggage scale. `morph change can gripper.force_n=<N>` |
| no Claude credentials | offline parser runs automatically; `research` is the only thing lost |
| sim/real mismatch | that's the pitch: the ledger records the error, and the next design uses it |
