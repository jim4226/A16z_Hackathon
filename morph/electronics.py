"""Electronics for a force-sensing gripper: part choice, electrical checks, wiring, firmware.

The key number is the grip-force window:
  lower bound = force the simulation needs to hold the object (x hold margin)
  upper bound = force that damages the object (/ margin), and the sensor's range
A hobby servo driven open-loop pushes to stall, often far above the upper bound.
The generated firmware closes on the force sensor instead and stops at the target.
Part values are typical datasheet numbers; calibrate the sensor on the bench.
"""

from __future__ import annotations

import math
import os

from . import keewano
from .design import FSR
from .spec import Design

G = 9.81
GEAR_EFF = 0.6
# How far past contact a position-controlled jaw servo closes before it squeezes with its whole stall force (mm at the
# jaw): its position loop's proportional band plus the gear's and the jaw's give. A typical figure, the simulation's
# jaw servo model (simulate.GRIP_SAT_MM); the bench calibrates the real servo (the firmware's F command).
SERVO_BAND_MM = 1.5
HOLD_MARGIN = 1.5
CRUSH_MARGIN = 1.5
FSR_K_OHM_N = 10_000.0   # FSR 402 roughly follows R ~ K / F (10 kOhm at ~1 N); calibrate on the bench
E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]

MCUS = {
    "arduino_nano": dict(name="Arduino Nano (ATmega328P)", vcc=5.0, adc_bits=10, servo_pin="D9", fsr_pin="A0",
                         servo_lib="Servo.h", price=8.0),
    "esp32_devkit": dict(name="ESP32 DevKitC", vcc=3.3, adc_bits=12, servo_pin="GPIO18", fsr_pin="GPIO34",
                         servo_lib="ESP32Servo.h", price=8.0),
    "rp2040_pico": dict(name="Raspberry Pi Pico (RP2040)", vcc=3.3, adc_bits=12, servo_pin="GP15", fsr_pin="GP26",
                        servo_lib="Servo.h", price=4.0),
    # a robot's Controller option: its gripper stage plans the grip force with it (FSR on ADC1, as the robot's pin map)
    "esp32_s3_devkit": dict(name="ESP32-S3-DevKitC-1", vcc=3.3, adc_bits=12, servo_pin="GPIO15", fsr_pin="GPIO4",
                            servo_lib="ESP32Servo.h", price=15.0),
}
SERVOS = {  # stall torque kgf*cm at the listed voltage, stall current A, voltage range
    "sg90": dict(name="TowerPro SG90 (micro, plastic gear)", torque=1.8, stall_a=0.65, v=(4.8, 6.0), price=3.0),
    "mg90s": dict(name="MG90S (micro, metal gear)", torque=2.2, stall_a=0.7, v=(4.8, 6.0), price=5.0),
    "mg996r": dict(name="TowerPro MG996R (standard, metal gear)", torque=11.0, stall_a=2.5, v=(4.8, 7.2), price=9.0),
    "ds3218": dict(name="DS3218 (20 kg digital)", torque=20.0, stall_a=2.5, v=(4.8, 6.8), price=16.0),
    "sts3215": dict(name="Feetech STS3215 serial bus servo (12 V)", torque=29.6, stall_a=2.7, v=(6.0, 12.6), price=17.0),
    "xl430_w250": dict(name="ROBOTIS DYNAMIXEL XL430-W250-T", torque=15.3, stall_a=1.4, v=(6.5, 12.0), price=49.9),
    "xm430_w350": dict(name="ROBOTIS DYNAMIXEL XM430-W350-T", torque=41.8, stall_a=2.3, v=(10.0, 14.8), price=289.9),
}


def servo_spec(servo: str) -> dict:
    """SERVOS[servo], or a robot actuator in the same shape: a robot's gripper may use any actuator in its catalog
    (an XL330 or a 7.4 V STS3215 through arm.actuator_overrides), at its rated voltage."""
    if servo in SERVOS:
        return SERVOS[servo]
    from .robot import catalog, catalog_extra  # noqa: F401  (catalog_extra registers the imported robots' servos)

    a = catalog.ACTUATORS[servo]
    return dict(name=a["name"], torque=a["torque_nm"] / (G / 100.0), stall_a=a["stall_a"], v=tuple(a["voltage"]),
                price=catalog.price(servo))


def jaw_force(torque_nm: float, lever_mm: float, jaw: str = "rack") -> float:
    """Per-finger squeeze (N) from the gripper servo's torque (N·m): the one model of each gripper type. A rack-and-pinion
    jaw: one pinion drives both racks, so each finger gets torque x GEAR_EFF / (2 x the pinion's pitch radius). A revolute
    stock jaw on the servo's own horn: torque / lever, the object's distance from the jaw's pivot."""
    lever = max(float(lever_mm), 1e-6) / 1000.0
    return torque_nm * GEAR_EFF / (2.0 * lever) if jaw == "rack" else torque_nm / lever


def jaw_torque(force_n: float, lever_mm: float, jaw: str = "rack", losses: bool = True) -> float:
    """The servo torque (N·m) for a per-finger squeeze of `force_n` (jaw_force inverted). losses=False leaves the rack's
    gear losses out, which gives the torque that can never squeeze harder than `force_n` (a hard cap)."""
    lever = max(float(lever_mm), 1e-6) / 1000.0
    return force_n * 2.0 * lever / (GEAR_EFF if losses else 1.0) if jaw == "rack" else force_n * lever


def servo_max_force(servo: str, pinion_radius_mm: float) -> float:
    """Per-finger force at stall through the rack-and-pinion jaw drive (jaw_force)."""
    return jaw_force(servo_spec(servo)["torque"] * G / 100.0, pinion_radius_mm, "rack")


def choose_servo(design: Design, target: float) -> str:
    """The standalone gripper's servo: the first hobby servo its printed mechanism mounts whose stall force per finger is
    at least twice the target."""
    from .mechanism import SERVOS as MOUNTS

    e = design.electronics
    if e.servo != "auto":
        return e.servo
    fits = [k for k in SERVOS if k in MOUNTS and servo_max_force(k, e.pinion_radius_mm) >= 2.0 * target]
    return fits[0] if fits else "ds3218"


def target_force(design: Design, analysis: dict, sim: dict) -> float:
    """Same target the electronics plan picks; used to size the servo before electronics runs."""
    return plan(design, analysis, sim, mech=None)["target_force_n"]


def nearest_e12(ohms: float) -> float:
    decade = 10 ** math.floor(math.log10(ohms))
    return min((m * decade for m in E12 + [10.0]), key=lambda r: abs(math.log(r / ohms)))


def force_window(o, hold: float, sensor: str | None = None, target: float = 0.0, floor: float = 0.0) -> tuple:
    """(lo, hi, target) per finger, N: the force that holds the object (hold x HOLD_MARGIN), the force that damages it
    (/ CRUSH_MARGIN; inf for a robust object) capped at 80 % of the force sensor's range, and the target: `target` when
    one is asked for, else
      - an object without a damage force: twice the lower bound, three times the static hold: a 1.5x margin over the
        robot's own shake test, which loads the grip with about 1.9 g (simulate._shake);
      - with a force sensor (`sensor`), which regulates the squeeze: the window's geometric mean, but at most twice the
        lower bound;
      - without one (a torque limit sets the squeeze, open loop: it varies with where the object sits on the jaw, the
        gearbox's friction and the supply): the window's geometric mean, as far from both ends as the window allows;
    raised to `floor` while that stays inside the window. A narrow window gets less margin, and the simulation says so
    when that slips. Every grip target Morph ships comes from here."""
    lo = hold * HOLD_MARGIN
    hi = (o.max_force_n / CRUSH_MARGIN) if o.max_force_n > 0 else float("inf")
    if sensor:
        hi = min(hi, FSR[sensor]["max_n"] * 0.8)
    if target:
        return lo, hi, float(target)
    if hi == float("inf"):
        t = lo * 2.0
    else:
        t = min(math.sqrt(lo * hi), lo * 2.0) if sensor else math.sqrt(max(lo, 0.0) * hi)
    return lo, hi, max(t, min(floor, hi))


def static_hold(o, gain: float = 1.0) -> float:
    """The per-finger squeeze that just holds the object against gravity by friction (N): m g / (2 mu gain), `gain` the
    pad's wedge gain (analysis.wedge_gain; 1 for flat jaws)."""
    return o.mass_g / 1000.0 * G / (2.0 * max(o.friction, 0.05) * max(gain, 1e-6))


def limit_source(o, sensor: str | None = None) -> str:
    """Where the top of the grip window comes from: the object's damage force, or the force sensor's range below it."""
    if sensor and not (0 < o.max_force_n / CRUSH_MARGIN <= FSR[sensor]["max_n"] * 0.8):
        return "80 % of the FSR 402 force sensor's range" if sensor == "fsr402" else f"80 % of the {sensor} force sensor's range"
    return f"the {o.name or 'object'}'s {o.max_force_n:g} N damage force / {CRUSH_MARGIN:g}"


def no_safe_grip(lo: float, hi: float, o, sensor: str | None = None, jaw: str = "rack") -> str:
    """The issue for a grip window with no force in it (the gripper stage's and the electronics plan's are one sentence)."""
    fix = ("try a contour pad, higher-friction pad, or a different object orientation" if jaw == "rack" else
           "the stock jaws need a lighter object, or one with a grippier surface")
    return f"no safe grip force: holding needs {lo:.2f} N but the limit is {hi:.2f} N ({limit_source(o, sensor)}); {fix}"


def plan(design: Design, analysis: dict, sim: dict, mech: dict | None = None, stall_nm: float | None = None,
         torque_limited: bool = False) -> dict:
    """The grip's force plan: window, target, hard limit, the servo and its stall force per finger. `stall_nm` is the
    servo's stall torque on the supply it runs from (a robot's gripper stage passes it on the arm's servo rail); without
    it, the servo's rated torque. `torque_limited`: the firmware sets the servo's torque limit for the target (a robot's
    smart-bus gripper), so without a force sensor its stall force can't crush the object and that is no issue."""
    e, o, f = design.electronics, design.object, design.finger
    issues, warnings = [], []
    sensor = f.sensor if f.sensor in FSR else None

    # the hold: never below the static friction hold m g / (2 mu gain); the grasp simulation only ever raises it (its
    # pads can cradle an object the robot's own fingers then drop)
    static = static_hold(o, analysis["wedge_gain"])
    sim_hold = sim.get("min_hold_force_n") if sim else None
    if sim_hold is None or sim_hold == float("inf"):
        hold = static
        warnings.append("no simulated holding force; using closed-form estimate")
    else:
        hold = max(static, float(sim_hold))
    calibration = None
    if not e.target_force_n:  # opt-in (Keys: Use Keewano calibration); None unless enough logged, non-synthetic builds
        calibration = keewano.hold_ratio(f.pad, f.material)
        if calibration:
            hold *= calibration["ratio"]
            warnings.append(f"holding force scaled x{calibration['ratio']:.2f} by Keewano calibration from {calibration['n']} "
                            f"logged {f.pad}/{f.material} builds (seeded demo data unless your project has real bench results)")
    lo, hi, target = force_window(o, hold, sensor, e.target_force_n)
    if lo > hi:
        issues.append(no_safe_grip(lo, hi, o, sensor))
    elif not lo <= target <= hi:
        warnings.append(f"target {target:.2f} N is outside the window {lo:.2f}-{hi:.2f} N")

    servo = choose_servo(design, target)
    s = servo_spec(servo)
    f_stall = jaw_force(stall_nm, e.pinion_radius_mm, "rack") if stall_nm else servo_max_force(servo, e.pinion_radius_mm)
    crush_open_loop = o.max_force_n > 0 and f_stall > o.max_force_n
    if f_stall < 1.5 * target:
        issues.append(f"{servo} reaches only {f_stall:.1f} N per finger (< 1.5 x target)")
    if crush_open_loop and not sensor and not torque_limited:
        issues.append(f"{servo} can reach {f_stall:.1f} N > object limit {o.max_force_n} N and there is no force "
                      f"sensor: add finger.sensor=fsr402")
    if not s["v"][0] <= e.supply_v <= s["v"][1]:
        issues.append(f"supply {e.supply_v} V outside {servo} range {s['v'][0]}-{s['v'][1]} V")

    mcu_key = e.mcu if e.mcu != "auto" else "arduino_nano"
    mcu = MCUS[mcu_key]
    supply_a = math.ceil((s["stall_a"] * 1.2 + 0.3) * 2) / 2
    warnings.append(f"power the servo from a separate {e.supply_v:g} V / {supply_a:g} A supply, not the board's 5V pin; "
                    f"tie grounds together; 470 uF across the servo supply")

    sensing = {}
    if sensor:
        r_div = nearest_e12(FSR_K_OHM_N / target)
        adc_max = 2 ** mcu["adc_bits"] - 1

        def volts(force):
            r = FSR_K_OHM_N / force
            return mcu["vcc"] * r_div / (r_div + r)

        dv = (volts(target * 1.05) - volts(target * 0.95)) / (target * 0.1)  # V per N at target
        lsb = mcu["vcc"] / adc_max
        sensing = {"sensor": sensor, "divider_ohm": r_div, "v_at_target": round(volts(target), 3),
                   "resolution_n_per_lsb": round(lsb / dv, 4), "range_n": [FSR[sensor]["min_n"], FSR[sensor]["max_n"]]}
        if target < FSR[sensor]["min_n"] * 2:
            warnings.append(f"target {target:.2f} N is near the FSR's {FSR[sensor]['min_n']} N threshold; "
                            f"readings will be noisy")

    wiring = [
        ("servo signal (orange/yellow)", mcu["servo_pin"]),
        ("servo V+ (red)", f"external {e.supply_v:g} V +"),
        ("servo GND (brown/black)", "external supply GND and board GND"),
    ]
    if sensor:
        wiring += [
            (f"{sensor} lead 1", f"board {mcu['vcc']:g} V"),
            (f"{sensor} lead 2", f"{mcu['fsr_pin']} and {sensing['divider_ohm']:g} ohm to GND"),
        ]
    parts = [
        {"item": mcu["name"], "qty": 1, "cost": mcu["price"]},
        {"item": s["name"], "qty": 1, "cost": s["price"], "note": "skip if your gripper already has one"},
        {"item": f"{e.supply_v:g} V {supply_a:g} A supply (or 2S LiPo + UBEC)", "qty": 1, "cost": 10.0},
        {"item": "470 uF 10 V electrolytic capacitor", "qty": 1, "cost": 0.3},
    ]
    if sensor:
        parts += [{"item": "Interlink FSR 402", "qty": 1, "cost": 7.0, "note": "one finger is enough to close the loop"},
                  {"item": f"{sensing['divider_ohm']:g} ohm resistor", "qty": 1, "cost": 0.1}]

    if mech and mech.get("servo") and mech["servo"] != servo:
        warnings.append(f"mechanism was sized for {mech['servo']}, electronics chose {servo}")
    return {
        "open_deg": mech["servo_open_deg"] if mech else 20, "closed_deg": mech["servo_closed_deg"] if mech else 160,
        "mcu": mcu_key, "servo": servo, "sensor": sensor or "none", "jaw": "rack", "lever_mm": e.pinion_radius_mm,
        "hold_force_n": round(hold, 3), "static_hold_n": round(static, 3),
        "sim_hold_n": None if sim_hold is None or sim_hold == float("inf") else sim_hold, "window_n": [round(lo, 2), round(hi, 2) if hi != float("inf") else None],
        "target_force_n": round(target, 2), "hold_calibration": calibration, "servo_stall_force_n": round(f_stall, 1),
        "limit_n": round(min(o.max_force_n / 1.2 if o.max_force_n > 0 else 1e9,
                             FSR[sensor]["max_n"] * 0.95 if sensor else 1e9, f_stall), 2),
        "open_loop_crushes": bool(crush_open_loop), "object_limit_n": o.max_force_n or None,
        "sensing": sensing, "wiring": wiring, "parts": parts,
        "pass": not issues, "issues": issues, "warnings": warnings,
    }


FIRMWARE = r"""// Morph force-limited gripper firmware, generated for {design_name} ({rev_hint})
// {mcu_name} + {servo_name} + {sensor}
// Serial (115200): g = grip, o = open, t<N> = set target newtons, c<grams> = calibrate with a known mass
// Streams CSV at 50 Hz: t_ms,force_n,angle_deg,state
#include <{servo_lib}>

const int SERVO_PIN = {servo_pin_num};
const int FSR_PIN = {fsr_pin_num};
const float VCC = {vcc};
const float ADC_MAX = {adc_max}.0;
const float R_DIV = {r_div};          // ohms, divider resistor to GND
float K_FSR = {k_fsr};                // ohm*N: F = K_FSR / R_fsr (calibrate with c<grams>)
float TARGET_N = {target};            // from Morph: inside the window {lo} - {hi} N
const float LIMIT_N = {limit};        // never exceed (object damage / sensor range)
const int OPEN_DEG = {open_deg};      // from the generated rack-and-pinion: jaws fully open
const int CLOSED_DEG = {closed_deg};  // contact at 90 deg + overtravel; slots stop here (swap if your servo turns the other way)
const float STEP_DEG = 0.5;           // closing speed per 20 ms tick

Servo servo;
float angle = OPEN_DEG;
enum State {{ OPEN, CLOSING, HOLD }} state = OPEN;
const char* NAMES[] = {{"OPEN", "CLOSING", "HOLD"}};

float readForce() {{
  float v = analogRead(FSR_PIN) * VCC / ADC_MAX;
  if (v < 0.01) return 0.0;
  float r_fsr = R_DIV * (VCC - v) / v;
  return K_FSR / r_fsr;
}}

void setup() {{
  Serial.begin(115200);
  servo.attach(SERVO_PIN);
  servo.write(OPEN_DEG);
}}

void loop() {{
  while (Serial.available()) {{
    char c = Serial.read();
    if (c == 'g') state = CLOSING;
    if (c == 'o') {{ state = OPEN; angle = OPEN_DEG; }}
    if (c == 't') TARGET_N = min(Serial.parseFloat(), LIMIT_N);
    if (c == 'c') {{                       // known mass resting on the sensor
      float grams = Serial.parseFloat();
      float v = analogRead(FSR_PIN) * VCC / ADC_MAX;
      if (v > 0.01) K_FSR = (grams / 1000.0 * 9.81) * (R_DIV * (VCC - v) / v);
      Serial.print("# K_FSR="); Serial.println(K_FSR);
    }}
  }}
  float f = readForce();
  int dir = CLOSED_DEG > OPEN_DEG ? 1 : -1;
  if (f > LIMIT_N) {{ angle -= dir * 3 * STEP_DEG; state = HOLD; }}     // back off hard
  else if (state == CLOSING) {{
    if (f >= TARGET_N) state = HOLD; else angle += dir * STEP_DEG;
  }} else if (state == HOLD) {{                                         // small bang-bang trim
    if (f < TARGET_N * 0.85) angle += dir * STEP_DEG * 0.5;
    if (f > TARGET_N * 1.15) angle -= dir * STEP_DEG * 0.5;
  }}
  angle = constrain(angle, min(OPEN_DEG, CLOSED_DEG), max(OPEN_DEG, CLOSED_DEG));
  servo.write((int)angle);
  Serial.print(millis()); Serial.print(',');
  Serial.print(f, 3); Serial.print(',');
  Serial.print(angle, 1); Serial.print(',');
  Serial.println(NAMES[state]);
  delay(20);
}}
"""


def write_firmware(design: Design, ep: dict, outdir: str) -> str:
    mcu = MCUS[ep["mcu"]]
    sensing = ep["sensing"] or {"divider_ohm": 10000}
    pin = lambda p: p.replace("GPIO", "").replace("GP", "").replace("D", "")  # noqa: E731
    lo, hi = ep["window_n"]
    code = FIRMWARE.format(
        design_name=design.name, rev_hint="see results.json", mcu_name=mcu["name"],
        servo_name=servo_spec(ep["servo"])["name"], sensor=ep["sensor"], servo_lib=mcu["servo_lib"],
        servo_pin_num=pin(mcu["servo_pin"]), fsr_pin_num=mcu["fsr_pin"] if mcu["fsr_pin"].startswith("A") else pin(mcu["fsr_pin"]),
        vcc=mcu["vcc"], adc_max=2 ** mcu["adc_bits"] - 1, r_div=sensing["divider_ohm"], k_fsr=FSR_K_OHM_N,
        target=ep["target_force_n"], lo=lo, hi=hi if hi else "inf", limit=ep["limit_n"],
        open_deg=int(round(ep.get("open_deg", 20))), closed_deg=int(round(ep.get("closed_deg", 160))))
    os.makedirs(os.path.join(outdir, "morph_grip"), exist_ok=True)
    path = os.path.join(outdir, "morph_grip", "morph_grip.ino")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(code)
    return path
