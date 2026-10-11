# Morph: from "pick this up without breaking it" to printed, wired, force-controlled fingers

**SpeedHacks, SF Tech Week, Oct 11 2026.** Challenge: *give people their time back.*

Adding a custom gripper for one tricky object today means bouncing between CAD, a
simulator, hand calcs, a slicer, a parts list, a wiring diagram and firmware. Every
change sends you around the loop again. Morph connects the whole loop:

```
describe a need → spec → parametric CAD → closed-form checks → MuJoCo grasp sim
   → electronics (servo, force sensor, MCU, wiring, firmware) → machine files
   → fabricate → live bench test → measured vs predicted
```

Changing a parameter reruns **only the artifacts that depend on it**. Every revision
(design → prediction → fab → measured result) is logged, building a dataset no public
source has.

**Demo use case: picking up a raw egg without cracking it.** A typical MG996R hobby gripper
squeezes up to about 54 N per finger at stall, while an egg's side can crack around 25 N (estimate). Morph:

- designs fingers with a curved pad and a **force-sensor pocket**, where the pad's dovetail tail bears on an FSR 402;
- simulates the grasp to find the force that actually holds the egg (≈0.8 N);
- sets a grip-force window: slips below 1.2 N, damage-limited at 16 N;
- generates the **Arduino firmware** that closes until 2.3 N and never exceeds 19 N;
- sizes the sensor divider resistor and produces the wiring table and electronics BOM;
- generates the **gripper itself**: a printable rack-and-pinion mechanism for the chosen servo, so the device doesn't depend on a gripper kit you may not have;
- generates a **carrier PCB** (Nano + FSR divider + servo + power) with Gerbers, solderable on perfboard today;
- emits print files: fingers ≈31 min plus the gripper body ≈61 min.

## Morph Studio (the live demo)

```bash
python -m morph.cli demo            # builds projects/raw-egg (≈25 s)
python -m morph.cli studio raw-egg  # → http://localhost:8765
```

Chat on the left: "make it work for a strawberry", "use an SG90 servo instead", "switch to an ESP32",
"make the fingers 10 mm longer", "gentler grip". Each request becomes parameter changes. The pipeline strip
shows which of the 7 stages regenerate and which are reused. Then the views tour what changed:

- **3D design:** the whole printable gripper (back plate, servo, pinion, two racks, sensor fingers) around the object. The previous revision is shown dashed.
- **Electronics + PCB:** the carrier board builds itself: parts drop in, then ratsnest, routed traces, GND pour, and a DRC pass. Download Gerbers or solder it on perfboard.
- **Fab:** print beds for the fingers and the gripper body, with time and cost.
- **Firmware:** generated `morph_grip.ino` with changed lines highlighted (target force, divider, servo angles).
- **Simulation:** MuJoCo grasp GIF.
- **Inspector:** design diffs, metric deltas, and the grip-force window.

The Studio uses Claude to interpret chat when `ANTHROPIC_API_KEY` is set; otherwise a built-in rule parser
handles the common requests, so the demo works offline.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m morph.cli demo           # ~25 s → projects/raw-egg/report.html
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
morph change egg electronics.servo=sg90             # only electronics + BOM rerun
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

**Keewano: the build memory.** Every ledger entry (revision, bench measurement, human minutes,
Darwin lookup) is sent to Keewano as typed events from `Project.log()` (`morph/keewano.py`).
Before choosing the grip-force target, `electronics.plan()` asks Keewano how much more holding
force real builds with the same pad and material needed than the simulation predicted, and
scales the target by that ratio. Remove Keewano and the firmware ships the uncalibrated number.

```bash
export KEEWANO_API_KEY=...                       # send (Settings -> Project -> API Keys)
export KEEWANO_PAT=kwpat-... KEEWANO_PROJECT_ID=<uuid>   # query (Settings -> Personal -> Security)
python tools/keewano_backfill.py --send          # 2 weeks of synthetic-* builds, labelled
morph ask 'SELECT "PadStyle", avg("HoldForceRatioPct") FROM events GROUP BY "PadStyle"'
```

**Darwin: who sells the parts and builds the rest.** Morph designs and programs; `morph source
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

Uploading needs your own BoardRepo sign-in, so Morph stops at the zip. More in `docs/BOARDREPO.md`.

## Honest limits

- The FSR is calibrated with one known mass (`c<grams>` over serial). Its absolute accuracy is about ±10–25%, so the window margins absorb that error.
- The egg damage force and servo stall torques are typical values. Check them with a kitchen scale.
- The sim treats fingers as rigid and models the egg as a capsule.
- The firmware assumes a position servo and a 0.5° step. Set `OPEN_DEG`/`CLOSED_DEG` for your gripper.

Plan, schedule and demo script: `docs/HACKATHON_PLAN.md`. Data sources: `docs/DATA_SOURCES.md`.
