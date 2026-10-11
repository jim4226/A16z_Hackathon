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

**Pick a person:** a robotics engineer (or grad student in a manipulation lab) who needs their gripper to handle one fragile object.
**Frustrating task:** design, simulate and fabricate custom fingers for it, and add force sensing and firmware. Today that takes a day or more across CAD, a simulator, a slicer and the Arduino IDE.
**Show the difference:** the time card. Before = baseline estimates per step (`morph/timestudy.py`), **replace at least one with a real timed manual run today**: have a teammate model the contour finger in Fusion/Onshape with a stopwatch, put the minutes in `projects/<name>/baseline.json` as `{"cad_first": <min>}`.
After = Morph wall-clock (auto-logged) + machine time + every human minute logged:

```bash
morph clock egg spec 2          # typing the need
morph clock egg review 6        # reading the report, picking a design
morph clock egg test 10         # bolting on + 10 lifts
morph clock egg corrections 18  # e.g. one reprint after measuring hole spacing
morph time egg r004             # prints the before/after table
```

## The one use case: pick up a raw egg without cracking it

A hobby MG996R gripper squeezes ~54 N per finger at stall; an egg's side cracks around 25 N.
Morph turns one sentence into fingers with a force-sensor pocket, the electronics, and
firmware that grips at 2.3 N. Then we prove it on camera.

Before / after on camera:
1. **Open-loop (stock behaviour):** the servo closes to its end stop. Either use stock fingers, or send `t19` to the firmware to show the max-force grip. Use a hard-boiled egg if you don't want the mess. The bench trace shows the force spike.
2. **Morph:** `morph bench egg r00X --port COM5`. The live bar climbs to the green target line and holds. Lift the egg, then put it down whole.

## Hardware (mix and match; Morph re-plans if you change it)

| need | default | swap-ins (`morph change …`) |
|---|---|---|
| MCU | Arduino Nano/Uno | `electronics.mcu=esp32_devkit` / `rp2040_pico` |
| servo / gripper | MG996R gripper kit | `electronics.servo=sg90 / mg90s / ds3218` |
| force sensor | Interlink FSR 402 | (any round FSR ≥ 18 mm; a load cell + HX711 is a later extension) |
| divider resistor | value from the report (≈3.9 kΩ) | any 3.3–4.7 kΩ works; recalibrate with `c<grams>` |
| power | 6 V ≥3 A supply or 2S LiPo + UBEC, 470 µF cap | never the board's 5 V pin |
| printer | any FDM, PETG or PLA | `fab.process=…` for non-sensor parts |
| objects | raw + hard-boiled eggs, kitchen scale for calibration | `object.max_force_n=3` (strawberry), etc. |

Measure your jaw's mount hole spacing first: `morph change egg finger.mount_hole_spacing_mm=<value>`.

## Live-recorded demo (3 min, single take)

1. **0:00 Pain (15 s).** "One fragile object costs a day of CAD, sim, wiring and firmware."
2. **0:15 Type the need (30 s).** `morph new` then `morph explore`. Open the report: sensor-pocket finger, sim GIF, force window (54 N stall vs 25 N crack vs 2.3 N target).
3. **0:45 Change something live (20 s).** `morph change egg electronics.servo=sg90`. The chips show only electronics + BOM reran.
4. **1:05 Hardware (60 s).** Printed fingers on the gripper. Run the open-loop squeeze (force spike), then `morph bench --port …`: the bar holds at target, lift the egg.
5. **2:05 Measured vs predicted + time card (35 s).** The report shows the measured hold force next to the prediction, and before/after minutes including our logged review and test time.
6. **2:40 Why it compounds (20 s).** Every run feeds the ledger.

Recording tips:
- Split screen: terminal plus a phone camera on the gripper.
- Increase the terminal font size; the live bar is the visual.
- Do one full rehearsal with `--simulate`, then record on hardware.
- Keep a fallback take recorded early in the afternoon.

## Measurement protocol

- **Calibrate:** rest a known mass (e.g. a 100 g weight) on the pad and send `c100` over serial.
- **Grip:** 10 grip-lift-hold cycles on a hard-boiled egg, each with `morph bench`. Count the holds.
- **Force:** the bench logs hold force vs the predicted target, peak force, and time to target.
- **Damage:** one open-loop squeeze on camera (hard-boiled egg or a cheap one).
- **Time:** `morph clock` for spec, review, test and corrections. Fab time comes from the printer.

## Schedule (Pacific)

| time | who | what |
|---|---|---|
| 10:30–11:00 | all | confirm printer and gripper. Measure jaw mount. Set `ANTHROPIC_API_KEY` |
| 11:30–12:15 | HW | `morph change` mount spacing → start printing r004 plate (~31 min) |
| 11:30–12:30 | HW | wire Nano + FSR + divider + servo supply; flash `morph_grip.ino`; set OPEN/CLOSED_DEG |
| 12:30–13:30 | HW | assemble, calibrate (`c100`), `morph bench` runs, log with `morph clock` |
| 11:30–13:30 | SW | stretch goal 1 (below). Keep `demo` green, `pytest -q` |
| 13:30–15:00 | all | second iteration from measurements (pad depth, friction tape, target). Reprint once if needed |
| 15:00–17:00 | SW | stretch goal 2. Polish report. Record a backup video of the physical test |
| 17:00–18:30 | all | pitch deck (5 slides) + 3-min demo rehearsals ×3 |
| 18:30– | | submit, demo |

## Stretch goals (in order)

1. **Agent loop:** Claude tool-use driving `new / explore / change / compare`, so "make it lighter but still hold 500 g" iterates by itself.
2. **Real robot in sim:** swap our box hand for a MuJoCo Menagerie gripper/arm model (license varies per model).
3. **Object library:** pick objects by name from YCB-style dimension and mass tables instead of typing numbers.
4. **Load cell + HX711 option** for absolute force accuracy, and slip detection from the force trace.

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
| no printer at venue | print at a nearby makerspace, or tape the FSR to stock fingers to still demo the force loop; state plainly what was and wasn't fabricated |
| no FSR | `morph change egg finger.sensor=none` → passive contour fingers; demo slip/force with a luggage scale instead |
| no hardware at all | `morph bench --simulate` for the flow, but say it's simulated: judges want the live recording |
| gripper force unknown | measure with luggage scale. `morph change can gripper.force_n=<N>` |
| no Claude credentials | offline parser runs automatically; `research` is the only thing lost |
| sim/real mismatch | that's the pitch: the ledger records the error, and the next design uses it |
