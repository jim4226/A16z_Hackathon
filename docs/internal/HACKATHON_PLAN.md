# SpeedHacks plan: Morph, one use case, end to end

**Event:** Sun Oct 11, 9:00am to 9:00pm, 1025 Sansome St. Finalists demo live.
**Theme:** *Give people their time back.*
**Our claim:** a robotics engineer goes from "my gripper crushes or drops this" to a
validated, fabricated, force-controlled gripper in under an hour of their time instead of days. Today we prove it once, on
real hardware, with measured numbers.

## Judging rubric → what we show

| criterion | weight | our evidence |
|---|---|---|
| Problem & time saved | **40** | one person, one task, measured before/after: `morph time` + the report's time card. Human setup/review/corrections are logged with `morph clock`, so they count, per the rules |
| Technical execution | 25 | it runs live: CAD → MuJoCo → firmware + wiring + print files in ~25 s, live hardware bench, 11 passing tests, partial recompute |
| Market & path forward | 15 | every robotics team, lab, and hardware startup makes custom end effectors and brackets; the ledger dataset compounds; next part types + real-robot sim |
| Originality | 10 | the change-propagation graph + measured-vs-predicted ledger (no public dataset links design → fab → test) |
| Product quality & demo | 10 | printed fingers in hand, flat vs contour lift on video/live, report on screen |

**Pick a person:** a robotics engineer (or grad student in a manipulation lab) who needs their gripper to handle one fragile object.
**Frustrating task:** design, simulate and fabricate custom fingers for it, and add force sensing and firmware. Today that takes a day or more across CAD, a simulator, a slicer and the Arduino IDE.
**Show the difference:** the time card. Before = baseline estimates per step (`morph/timestudy.py`), **replace at least one with a real timed manual run today**: have a teammate model the contour finger in Fusion/Onshape with a stopwatch, put the minutes in `projects/<name>/baseline.json` as `{"cad_first": <min>}`.
After = Morph wall-clock (auto-logged) + machine time + every human minute logged: typing the need, reading the report
and picking a design, bolting on + 10 lifts, corrections (e.g. one reprint after measuring hole spacing), then the
before/after table:

```shell
morph clock egg spec 2
morph clock egg review 6
morph clock egg test 10
morph clock egg corrections 18
morph time egg r004
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
| servo | MG996R (Morph generates the gripper around it) | `electronics.servo=sg90 / mg90s / ds3218` |
| hardware | M3×30 ×4, M3×16 ×4 with washers + nylock nuts, servo screws | listed in the BOM |
| force sensor | Interlink FSR 402 | (any round FSR ≥ 18 mm; a load cell + HX711 is a later extension) |
| divider resistor | value from the report (≈3.9 kΩ) | any 3.3–4.7 kΩ works; recalibrate with `c<grams>` |
| power | 6 V ≥3 A supply or 2S LiPo + UBEC, 470 µF cap | never the board's 5 V pin |
| printer | any FDM, PETG or PLA | `fab.process=…` for non-sensor parts |
| objects | raw + hard-boiled eggs, kitchen scale for calibration | `object.max_force_n=3` (strawberry), etc. |

Measure your servo's ear-hole spacing and compare it with `mechanism.py` SERVOS (typical values) before printing the plate.

## Live-recorded demo (3 min, single take)

Setup: Morph Studio full screen (`morph studio raw-egg`, auto-tour on), a phone on a tripod aimed at the gripper, terminal in a corner for `morph bench`.

1. **0:00 Pain (15 s).** "One fragile object costs a robotics engineer a day across CAD, simulation, wiring, PCB and firmware."
2. **0:15 Studio at the egg design (20 s).** Orbit the 3D gripper and point at the force window: the servo can push 62 N, the egg cracks at about 25 N, the target is 2.3 N.
3. **0:35 Chat: "Use an SG90 servo instead" (25 s).** Only 4 of 7 stages light up. The mechanism plate's screw holes change, the board re-routes on screen, and the BOM drops by $6.
4. **1:00 Chat: "Make it work for a strawberry" (30 s).** All 7 stages run. The fingers re-contour, jaw travel and servo angles change, the firmware diff highlights the new target and angles, and the fab beds regenerate.
5. **1:30 Hardware (60 s).** The printed gripper with the carrier board.
   - Run `morph bench raw-egg r004 --port COMx`: the live bar climbs to the target and holds.
   - Lift the raw egg and set it down whole.
   - Contrast: send `t19` (or use stock fingers) on a hard-boiled egg. The force spikes and it cracks.
6. **2:30 Measured vs predicted + time card (20 s).** Open the report: measured hold force next to the prediction, and the before/after minutes.
7. **2:50 Close (10 s).** "Every change and every test lands in the ledger. That's the dataset nobody has."

Print order: the **gripper body (≈61 min)** goes first and doesn't change when you switch eggs. Then the fingers (≈31 min).
Assembly: center the servo at 90° (send `o` then set the angle manually, or flash and press reset). Mesh both racks with the pads touching the egg, then bolt.

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
| 11:30–12:30 | HW | print `fab/gripper_plate.stl` (≈61 min), then `fab/plate.stl` fingers + pads (≈31 min) |
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
