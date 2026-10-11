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
- emits print files (4 parts, about 31 min).

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

## Honest limits

- The FSR is calibrated with one known mass (`c<grams>` over serial). Its absolute accuracy is about ±10–25%, so the window margins absorb that error.
- The egg damage force and servo stall torques are typical values. Check them with a kitchen scale.
- The sim treats fingers as rigid and models the egg as a capsule.
- The firmware assumes a position servo and a 0.5° step. Set `OPEN_DEG`/`CLOSED_DEG` for your gripper.

Plan, schedule and demo script: `docs/HACKATHON_PLAN.md`. Data sources: `docs/DATA_SOURCES.md`.
