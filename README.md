# Morph: from "I need to grab this" to fabricated robot fingers

**SpeedHacks, SF Tech Week, Oct 11 2026.** Challenge: *give people their time back.*

Designing one custom part for a robot today means hopping between CAD, a simulator, a
spreadsheet, CAM, and a slicer. Every change means redoing each step by hand. Morph
connects those steps into one workflow:

```
describe a need → spec → parametric CAD → closed-form checks → MuJoCo grasp sim
      → compare variants → DFM + machine files (print / laser / CNC) → fabricate → measure
```

When you change a parameter, Morph reruns **only the artifacts that depend on it** and
carries over the rest. It also logs each revision (design, predictions, measured results)
to a ledger. That ledger becomes the dataset none of the public sources provide.

**Demo slice:** custom parallel-gripper fingers for an object the robot can't grasp
well with stock fingers (default: a 66 mm, 370 g soda can lying on its side).

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m morph.cli demo                                # ~30 s, writes projects/soda-can/report.html
```

To see a finished run without installing anything, open
`examples/soda-can/report.html`. It is committed.

## What the demo shows (real output from `examples/soda-can`)

| step | what happens | result |
|---|---|---|
| 1. need → spec | Claude (or the offline parser) turns one sentence into a design spec | 66 mm cylinder, 370 g, μ≈0.35, PETG print |
| 2. baseline | first proposal is evaluated | **fails**: bending SF 0.9, 4.1 mm tip deflection |
| 3. explore | 324 variants screened closed-form in about 1 s, best per pad style simulated (8 randomized trials each) | contour pad holds the can with **2.3 N** of grip force; V-groove needs 5.5 N, flat needs 6.3 N |
| 4. "can is heavier" | `object.mass_g=520` | only analysis and sim rerun; CAD, fab and BOM are reused |
| 5. "laser cut Delrin" | material and process change | CAD and sim reused; new kerf-compensated `cut.svg`, 3.3 min instead of 15 min |
| 6. fabricate | `finger.stl`/`plate.stl` (print), `cut.svg` (laser), `job.nc` (GRBL CNC with tabs) | `morph send` streams to GRBL or uploads to OctoPrint |
| 7. measure | `morph measure … --grasp 9/10 --fab-minutes 18` | ledger stores the prediction error |
| 8. time saved | `morph time can r004` | by hand ≈ 520 min (baseline estimates, 6 variants) vs. 0.5 min of Morph compute + 15 min printing, **before** human review/test minutes are logged with `morph clock` |

## Commands

```bash
morph new can --need "pick up a 66 mm soda can lying on its side, 370 g, print in PETG"
morph explore can                       # search pad styles × sections, simulate, rank
morph change can object.mass_g=520      # recompute only what's affected
morph change can finger.material=Delrin fab.process=laser_cut
morph compare can r001 r004 r006        # HTML side by side
morph gif can r004                      # grasp animation (needs MUJOCO_GL=egl or osmesa on Linux)
morph send can r006 --grbl COM3         # stream G-code to a GRBL router/laser (--dry-run first)
morph measure can r004 --fab-minutes 17 --grasp 10/10 --length 63.1 --min-force 3.0
morph clock can review 6               # log human minutes (spec | review | test | corrections)
morph time can r004                     # before/after time study (also in the report)
morph research "parallel gripper under $300 with 25 N grip and M3 finger mounts"
```

Run `pip install -e .` to get the `morph` command. Without it, use `python -m morph.cli …`.
For Claude-backed `new`/`research`, set `ANTHROPIC_API_KEY`. Everything else runs offline.

## How it works

| module | job |
|---|---|
| `spec.py` | one `Design` dataclass. Every artifact derives from it; changes are dotted keys |
| `design.py` | 2.5D finger profile (shapely) → watertight solid (manifold3d) → STL. Pads: flat, V-groove, or contour matched to the object |
| `analysis.py` | grip-force budget with wedge gain, cantilever deflection and stress, layer-adhesion derate for prints |
| `sim.py` | MuJoCo: close, lift 80 mm, shake at about 0.5 g. Randomized offset, friction and mass. Bisection finds the minimum holding force. The pad is sliced into convex strips so concave contours collide correctly |
| `explore.py` | screens hundreds of variants closed-form, then simulates the best of each pad style |
| `fab.py` | process choice, DFM checks (tool-radius corners, sheet thickness, laser-safe materials), print time and cost, laser SVG, GRBL G-code (pecked holes, multi-pass climb contour, holding tabs), BOM |
| `pipeline.py` | dependency graph, partial recompute, revision directories, `ledger.jsonl` |
| `report.py` | the comparison page, led by the before/after time card |
| `timestudy.py` | before/after time study: auto-logged command time, logged human minutes, overridable manual baseline |
| `agent.py` | Claude structured output for need → spec, and Claude web search for component research |
| `machine.py` | GRBL serial streaming and OctoPrint upload |

## Honest limits

- The simulation treats the finger as rigid. Compliance is checked closed-form (deflection), not in MuJoCo.
- The can is modeled as a capsule (MuJoCo's cylinder collisions were unreliable with mesh pads).
- Print time is an estimate until a slicer runs. If `prusa-slicer` is on PATH, Morph slices automatically.
- Material values are typical datasheet numbers. Measured results in the ledger are what calibrate them.

See `docs/HACKATHON_PLAN.md` for today's plan and pitch, and `docs/DATA_SOURCES.md` for data sources.
