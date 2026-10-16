"""Whole-robot electronics: plan (bus, supply, budget, IDs) and finalize (schematic,
harness, custom PCB, firmware, sourcing, configuration comparison).

Two configurations the user can switch between (design.electronics.config):

* ``modules``    off-the-shelf boards wired together: controller module + servo
                 driver / bus adapter + regulators + sensor breakouts + supply.
* ``custom_pcb`` one generated controller PCBA (ESP32-S3-WROOM-1, half-duplex
                 TTL buffer or PCA9685, buck + LDO, FSR dividers, USB-C, reverse
                 polarity + fuse + TVS input, firmware-switched servo rail).

design.arm.actuator_family decides the bus: hobby_pwm -> PWM via PCA9685,
smart_bus -> Feetech SCServo TTL bus, dynamixel -> DYNAMIXEL protocol 2.0 TTL.
Extras (design.electronics.extras): wrist_camera (USB to host), imu (I2C),
estop (cuts servo power), wifi.

Numbers are typical datasheet values. Anything marked ``verify`` should be
checked against the current datasheet before ordering.
"""

from __future__ import annotations

import csv
import json
import math
import os

import numpy as np

from . import catalog
from . import catalog_extra  # noqa: F401  (registers the imported robots' actuators: a reused sizing names them)
from .catalog import ACTUATORS, MODULES, V
from .schematic import Circuit

G = 9.81
FSR_K_OHM_N = 10_000.0          # FSR 402: R ~ K / F  (10 kOhm at ~1 N); calibrate on the bench
FSR_MAX_N = 20.0
E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]

FAMILY_BUS = {  # family -> (contract bus, protocol, baud)
    "hobby_pwm": ("pwm", "pca9685_pwm", 0),
    "smart_bus": ("ttl_half_duplex", "feetech_scs", 1_000_000),
    "smart_bus_7v4": ("ttl_half_duplex", "feetech_scs", 1_000_000),
    "dynamixel": ("ttl_half_duplex", "dynamixel_2", 1_000_000),
}
SPEAKS = {"pca9685_pwm": "pwm", "feetech_scs": "feetech_scs", "dynamixel_2": "dynamixel_2"}  # bus -> actuator protocol
BUS_CONNECTOR = {"feetech_scs": "Molex 5264 3p (2.5 mm)", "dynamixel_2": "JST EH 3p (B3B-EH)", "pca9685_pwm": "Dupont/JR 3p (2.54 mm)"}

# chassis-wiring ampacity (A) for short bundled runs in silicone insulation, and resistance (ohm/m)
AWG = [(28, 1.4, 0.213), (26, 2.2, 0.134), (24, 3.5, 0.0842), (22, 5.0, 0.0530), (20, 7.5, 0.0333),
       (18, 10.0, 0.0210), (16, 13.0, 0.0132), (14, 18.0, 0.0083)]
CONNECTOR_A = {"Molex 5264 3p (2.5 mm)": 3.0, "JST EH 3p (B3B-EH)": 3.0, "Dupont/JR 3p (2.54 mm)": 3.0, "JST-PH 2p": 2.0,
               "XT30": 15.0, "JST-XH 2p": 3.0, "JST-SH 4p (Qwiic)": 1.0}
STD_FUSES = [1, 2, 3, 5, 7.5, 10, 15, 20, 25, 30]

# pin maps: logical signal -> controller pin name (as printed on the board / module pinout)
PINMAP = {
    "esp32_s3_wroom": {"BUS_TX": "IO11", "BUS_RX": "IO12", "BUS_DIR": "IO13", "SDA": "IO42", "SCL": "IO41", "PWM_OE": "IO9",
                       "FSR_L": "IO1", "FSR_R": "IO2", "VSERVO_SENSE": "IO10", "SERVO_EN": "IO14", "LED": "IO21",
                       "USB_DM": "IO19", "USB_DP": "IO20", "ESTOP": "IO10"},
    "esp32_s3_devkit": {"BUS_TX": "GPIO17", "BUS_RX": "GPIO18", "BUS_DIR": "GPIO16", "SDA": "GPIO8", "SCL": "GPIO9",
                        "PWM_OE": "GPIO7", "FSR_L": "GPIO4", "FSR_R": "GPIO5", "ESTOP": "GPIO6", "5V": "5V", "3V3": "3V3"},
    "esp32_devkit": {"BUS_TX": "IO17", "BUS_RX": "IO16", "BUS_DIR": "IO4", "SDA": "IO21", "SCL": "IO22", "PWM_OE": "IO25",
                     "FSR_L": "IO34", "FSR_R": "IO35", "ESTOP": "IO27", "5V": "5V", "3V3": "3V3"},
    "rp2040_pico": {"BUS_TX": "GP4", "BUS_RX": "GP5", "BUS_DIR": "GP6", "SDA": "GP20", "SCL": "GP21", "PWM_OE": "GP22",
                    "FSR_L": "GP26", "FSR_R": "GP27", "ESTOP": "GP15", "5V": "VSYS", "3V3": "3V3"},
    "arduino_nano": {"BUS_TX": "TX1", "BUS_RX": "RX0", "BUS_DIR": "D2", "SDA": "A4", "SCL": "A5", "PWM_OE": "D4",
                     "FSR_L": "A0", "FSR_R": "A1", "ESTOP": "D3", "5V": "5V", "3V3": "3V3"},
    "openrb_150": {"SDA": "SDA", "SCL": "SCL", "FSR_L": "A0", "FSR_R": "A1", "ESTOP": "D2", "5V": "5V", "3V3": "3V3"},
}
# Arduino pin expressions for firmware (same signals)
ARDUINO_PIN = {
    "esp32_s3_wroom": {"BUS_TX": "11", "BUS_RX": "12", "BUS_DIR": "13", "SDA": "42", "SCL": "41", "PWM_OE": "9", "FSR_L": "1",
                       "FSR_R": "2", "VSERVO_SENSE": "10", "SERVO_EN": "14", "LED": "21", "ESTOP": "-1"},
    "esp32_s3_devkit": {"BUS_TX": "17", "BUS_RX": "18", "BUS_DIR": "16", "SDA": "8", "SCL": "9", "PWM_OE": "7", "FSR_L": "4",
                        "FSR_R": "5", "ESTOP": "6", "LED": "-1", "VSERVO_SENSE": "-1", "SERVO_EN": "-1"},
    "esp32_devkit": {"BUS_TX": "17", "BUS_RX": "16", "BUS_DIR": "4", "SDA": "21", "SCL": "22", "PWM_OE": "25", "FSR_L": "34",
                     "FSR_R": "35", "ESTOP": "27", "LED": "2", "VSERVO_SENSE": "-1", "SERVO_EN": "-1"},
    "rp2040_pico": {"BUS_TX": "4", "BUS_RX": "5", "BUS_DIR": "6", "SDA": "20", "SCL": "21", "PWM_OE": "22", "FSR_L": "26",
                    "FSR_R": "27", "ESTOP": "15", "LED": "25", "VSERVO_SENSE": "-1", "SERVO_EN": "-1"},
    "arduino_nano": {"BUS_TX": "1", "BUS_RX": "0", "BUS_DIR": "2", "SDA": "A4", "SCL": "A5", "PWM_OE": "4", "FSR_L": "A0",
                     "FSR_R": "A1", "ESTOP": "3", "LED": "13", "VSERVO_SENSE": "-1", "SERVO_EN": "-1"},
    "openrb_150": {"BUS_TX": "-1", "BUS_RX": "-1", "BUS_DIR": "-1", "SDA": "SDA", "SCL": "SCL", "PWM_OE": "-1", "FSR_L": "A0",
                   "FSR_R": "A1", "ESTOP": "2", "LED": "LED_BUILTIN", "VSERVO_SENSE": "-1", "SERVO_EN": "-1"},
}
WIFI_MCUS = ("esp32_s3_devkit", "esp32_devkit", "esp32_s3_wroom")
ADC_BITS = {"esp32_s3_wroom": 12, "esp32_s3_devkit": 12, "esp32_devkit": 12, "rp2040_pico": 12, "arduino_nano": 10, "openrb_150": 12}
LOGIC_V = {"esp32_s3_wroom": 3.3, "esp32_s3_devkit": 3.3, "esp32_devkit": 3.3, "rp2040_pico": 3.3, "arduino_nano": 5.0, "openrb_150": 3.3}


# ------------------------------------------------------------------ small helpers

def extras_of(design) -> list:
    return sorted({e.strip() for e in (design.electronics.extras or "").split(",") if e.strip()})


def nearest_e12(ohms: float) -> float:
    decade = 10 ** math.floor(math.log10(ohms))
    return min((m * decade for m in E12 + [10.0]), key=lambda r: abs(math.log(r / ohms)))


def ohm_str(r: float) -> str:
    if r >= 1e6:
        return f"{r / 1e6:g}M"
    if r >= 1e3:
        return f"{r / 1e3:g}k"
    return f"{r:g}"


def pick_awg(amps: float) -> int:
    for g, a, _ in AWG:
        if a >= amps:
            return g
    return AWG[-1][0]


def awg_ohm_m(g: int) -> float:
    return next(r for gg, _, r in AWG if gg == g)


def grip_window(design, sizing: dict | None = None, grip: dict | None = None) -> dict:
    """The grip per finger the electronics, firmware and manual use: the slip and damage bounds (window_n), the target and
    the firmware's hard limit, all from the gripper stage's force plan (`grip`, results.json gripper.force), which the
    simulation uses too. A plan's own grip (this function's output) passes through. Without either, for callers that
    plan electronics without a gripper stage, the same force plan without its grasp simulation
    (pipeline2.gripper_stage(simulate=False)), keeping the target a partial plan or sizing["grip_target_n"] gives."""
    if grip and "target_n" in grip:
        return dict(grip)
    if not grip or not grip.get("window_n") or grip["window_n"][0] is None or grip.get("limit_n") is None:
        from .pipeline2 import gripper_stage   # a result saved before the stock jaws had a window comes here too

        t = (grip or {}).get("target_force_n") or (sizing or {}).get("grip_target_n")
        d = design.with_changes({"electronics.target_force_n": float(t)}) if t else design
        grip = gripper_stage(d, sizing, simulate=False)["force"]
    lo, hi = grip["window_n"]
    target = float(grip["target_force_n"])
    top = hi if hi is not None else float("inf")
    jaw = grip.get("jaw") or ("revolute" if design.arm.source else "rack")
    return {"hold_n": grip.get("hold_force_n"), "window_n": [lo, hi], "target_n": round(target, 2),
            "limit_n": round(float(grip["limit_n"]), 2), "sensor": grip.get("sensor") or "none", "ok": lo <= top,
            "object_limit_n": design.object.max_force_n or None, "jaw": jaw,
            "lever_mm": float(grip.get("lever_mm") or (70.0 if jaw == "revolute" else design.electronics.pinion_radius_mm))}


def has_torque_limit(gkey: str, family: str) -> bool:
    """Whether the firmware can set this gripper servo's torque limit on the arm's bus (Feetech STS: register 48;
    DYNAMIXEL: Goal PWM). A hobby PWM servo has none, nor has a servo whose protocol the bus doesn't speak."""
    proto = FAMILY_BUS.get(family, FAMILY_BUS["smart_bus"])[1]
    return proto != "pca9685_pwm" and gkey in ACTUATORS and catalog.protocol(gkey) == SPEAKS[proto]


def grip_torque_limit(grip: dict, gkey: str, servo_v: float, family: str) -> dict | None:
    """The gripper servo's torque limit, where its protocol has one (Feetech STS: the torque limit register; DYNAMIXEL:
    Goal PWM, which bounds the stall torque in position control). Without force sensors it is the grip: set for the
    target at the jaw's lever (morph.electronics.jaw_torque: a rack-and-pinion jaw's one pinion drives both racks, per
    finger F = torque x 0.6 / 2r; a stock revolute jaw F = torque / lever). With them it is a hard cap at the
    firmware's force limit, in case the force loop fails, counted without gear losses so it never allows more. Hobby
    PWM servos have none: None. {force_n, nm, fraction (of the stall torque at the rail, at most 1), per_n (fraction
    per newton, for the F command), register, short (the servo can't reach it), stall_force_n (per finger at stall on
    the rail, jaw_force)}."""
    if not has_torque_limit(gkey, family):
        return None
    from ..electronics import jaw_force, jaw_torque

    proto = FAMILY_BUS.get(family, FAMILY_BUS["smart_bus"])[1]

    sensor = grip["sensor"] == "fsr402"
    force = float(grip["limit_n"] if sensor else grip["target_n"])
    nm_per_n = jaw_torque(1.0, grip["lever_mm"], grip["jaw"], losses=not sensor)
    stall = catalog.torque_at(gkey, servo_v)
    return {"force_n": round(force, 2), "nm": round(min(force * nm_per_n, stall), 4),
            "fraction": round(min(force * nm_per_n / stall, 1.0), 4), "per_n": round(nm_per_n / stall, 6),
            "register": "feetech_torque_limit" if proto == "feetech_scs" else "dynamixel_goal_pwm",
            "short": bool(force * nm_per_n > stall), "stall_force_n": round(jaw_force(stall, grip["lever_mm"], grip["jaw"]), 2)}


PWM_STEP_US = 1e6 / 50.0 / 4096.0   # one PCA9685 count: 12 bits over the 20 ms frame of a 50 Hz servo pulse


def _command_step_mm(gkey: str, grip: dict, family: str) -> float:
    """The gripper's smallest position command at the jaw (mm): one PCA9685 count for a hobby PWM servo, one of a bus
    servo's 4096 ticks a turn. A rack's pinion and a stock jaw's lever both turn a servo radian into lever_mm at the jaw."""
    if FAMILY_BUS.get(family, FAMILY_BUS["smart_bus"])[0] == "pwm" and gkey in ACTUATORS:
        return PWM_STEP_US / (2000.0 / math.radians(ACTUATORS[gkey]["range_deg"])) * grip["lever_mm"]
    return 2.0 * math.pi / 4096.0 * grip["lever_mm"]


def open_loop_squeeze(grip: dict, gkey: str, family: str, width_mm: float | None = None) -> dict:
    """How far past the object the firmware closes a gripper servo with no torque limit (a hobby PWM servo). Its own
    position loop squeezes in proportion to how far it is held past contact and reaches its stall force
    morph.electronics.SERVO_BAND_MM past it (the simulation's jaw servo model). Held at GRIP_MIN it would sit at stall,
    drawing its stall current for the whole carry, and hobby servos have no overload protection. So:
      - without force sensors G closes `squeeze_mm` past the gauge contact: band x target / stall, at least two position
        commands (so it moves at all), and never past what the servo holds continuously (`rated_n`, its rated torque's
        share of stall: the band x rated / stall). That squeezes a gauge-sized object with `squeeze_n` (the target; more
        when the target is under two commands; `rated_n` when the target is above it: `capped`) and the servo draws
        that share of its stall current. The jaws are calibrated on a gauge of the object's nominal size (`width_mm`)
        and the object strays from it by OBJECT_SIZE_SPREAD each way, so one at the large end of its size is held
        `spread_mm` per jaw further past its own contact. G closes no further than keeps it under PROBE_STALL_SHARE of
        the stall and under the object's damage force (`room_mm` past its contact; `band_capped` when that lowers the
        squeeze, `band_n` the most F may then ask for). `covers` is whether two position commands fit in that room;
        when they don't (a plan issue) G keeps the squeeze above. `large_n`: what the large end gets;
      - with them the close stops where it has read no force by `probe_mm` past the gauge (the firmware's GRIP_PROBE),
        reads there too and opens. The jaws are calibrated on a gauge of the object's nominal size (`width_mm`), and
        the object strays from it by OBJECT_SIZE_SPREAD each way: `spread_mm` per jaw at the small end. `reach_mm`, the
        spread plus two position commands (to get a reading), is the probe (`covers`; at least where a gauge-sized
        object gets the hard limit, at most PROBE_STALL_SHARE x band) when, with a dead sensor, a gauge-sized object
        then gets at most PROBE_STALL_SHARE of the stall and no more than its damage force, and one at the large end of
        its size (`spread_mm` further past its contact) no more than its damage force either. Otherwise (`covers`
        False: a plan issue) the probe stops where a gauge-sized object gets the hard limit (at least two position
        commands, at most that share), and an object narrower than it per jaw isn't gripped. `probe_n` and `large_n`
        are what a gauge-sized object and one at the large end of its size get at the probe with a dead sensor, until
        the firmware opens (the large end of an object with no damage force can reach the stall for that moment);
        `reach_large_n` is what the large end would get at `reach_mm`.
    {servo_band_mm, step_mm, rated_n, squeeze_mm, squeeze_n, capped, spread_mm, room_mm, covers, band_capped, band_n,
    large_n} or {servo_band_mm, step_mm, rated_n, spread_mm, reach_mm, covers, probe_mm, probe_n, large_n,
    reach_large_n}."""
    from ..electronics import OBJECT_SIZE_SPREAD, PROBE_STALL_SHARE, SERVO_BAND_MM

    stall = float(grip["stall_force_n"])
    step = _command_step_mm(gkey, grip, family)
    rated = stall * ACTUATORS[gkey]["rated_nm"] / ACTUATORS[gkey]["torque_nm"]
    out = {"servo_band_mm": SERVO_BAND_MM, "step_mm": round(step, 4), "rated_n": round(rated, 2)}
    if grip["sensor"] == "fsr402":
        most = PROBE_STALL_SHARE * SERVO_BAND_MM
        spread = OBJECT_SIZE_SPREAD * float(width_mm or 0.0) / 2.0
        reach = spread + 2.0 * step
        at_limit = max(SERVO_BAND_MM * float(grip["limit_n"]) / stall, 2.0 * step)
        dmg = float(grip.get("object_limit_n") or 0.0)
        # a dead sensor must leave a gauge-sized object under PROBE_STALL_SHARE of the stall and under its damage force
        # (a 20 mm grape's 0.96 mm reach would put 19.77 N on a 2 N grape), and the large end of its size, held spread_mm
        # further past its own contact at the probe, under its damage force (a 22 mm part with a 25 N damage force got
        # the MG996R's 30.86 N stall from 4 % over its size); without one, the large end can reach the stall for the
        # moment before the jaw opens
        want = min(max(reach, at_limit), most)
        covers = reach <= most + 1e-9 and not (dmg > 0 and stall * min(want + spread, SERVO_BAND_MM) / SERVO_BAND_MM > dmg)
        probe = round(want if covers else min(at_limit, most), 3)
        return {**out, "spread_mm": round(spread, 3), "reach_mm": round(reach, 3), "covers": covers, "probe_mm": probe,
                "probe_n": round(stall * probe / SERVO_BAND_MM, 2),
                "large_n": round(stall * min(probe + spread, SERVO_BAND_MM) / SERVO_BAND_MM, 2),
                "reach_large_n": round(stall * min(reach + spread, SERVO_BAND_MM) / SERVO_BAND_MM, 2)}
    want = SERVO_BAND_MM * min(float(grip["target_n"]), rated) / stall
    sq = max(want, 2.0 * step)
    spread = OBJECT_SIZE_SPREAD * float(width_mm or 0.0) / 2.0
    dmg = float(grip.get("object_limit_n") or 0.0)
    room = min(PROBE_STALL_SHARE * SERVO_BAND_MM, SERVO_BAND_MM * dmg / stall if dmg > 0 else float("inf"))
    covers = room - spread >= 2.0 * step - 1e-9
    band_capped = covers and sq > room - spread + 1e-9
    if band_capped:
        sq = room - spread
    return {**out, "squeeze_mm": round(sq, 4), "capped": float(grip["target_n"]) > rated + 0.005,
            "squeeze_n": round(min(float(grip["target_n"]), rated) if sq == want else stall * sq / SERVO_BAND_MM, 2),
            "spread_mm": round(spread, 3), "room_mm": round(room, 3), "covers": covers, "band_capped": band_capped,
            "band_n": round(stall * (room - spread) / SERVO_BAND_MM, 2) if covers else None,
            "large_n": round(stall * min(sq + spread, SERVO_BAND_MM) / SERVO_BAND_MM, 2)}


def grip_problems(design, grip: dict) -> list:
    """Plan issues for a grip the firmware would ship outside its own window or above its hard stop."""
    lo, hi = grip["window_n"]
    t, name = grip["target_n"], design.object.name or "object"
    if not grip["ok"]:  # the same sentence as the gripper stage's own issue for it
        from ..electronics import no_safe_grip
        return [no_safe_grip(lo, hi, design.object, grip["sensor"] if grip["sensor"] != "none" else None, grip["jaw"])]
    if t < lo:
        return [f"grip target {t:g} N is below the {lo:g} N the {name} needs not to slip (window {lo:g}-"
                f"{'' if hi is None else f'{hi:g} '}N): set the Grip force target inside the window, or 0 for Morph's"]
    if hi is not None and t > hi:
        return [f"grip target {t:g} N is above the {hi:g} N the {name} takes (window {lo:g}-{hi:g} N): set the Grip "
                "force target inside the window, or 0 for Morph's"]
    ol = grip.get("open_loop") or {}
    if ol.get("squeeze_mm"):    # a hobby servo held open loop: how far past the object G closes sets the squeeze
        from ..electronics import OBJECT_SIZE_SPREAD, PROBE_STALL_SHARE

        out, dmg = [], grip.get("object_limit_n")
        large = f"a {name} at the large end of its size, {ol['spread_mm']:.2f} mm per jaw wider than the calibration gauge"
        keep = (f"under {PROBE_STALL_SHARE * 100:.0f} % of the servo's stall force"
                + (f" and the {dmg:g} N that damages it" if dmg else ""))
        if not ol["covers"]:
            share = ol["large_n"] / grip["stall_force_n"]
            force = ("its whole stall force" if share >= 0.995 else
                     f"{ol['large_n']:g} N per finger, {share * 100:.0f} % of its stall force")
            out.append(f"gripper: the hobby gripper servo has no torque limit and closes by position only, so {large} "
                       f"({OBJECT_SIZE_SPREAD * 100:g} % over its nominal size) is held "
                       f"{ol['squeeze_mm'] + ol['spread_mm']:.2f} mm past its contact, with {force}: no close past a "
                       f"gauge-sized {name} keeps that {name} {keep}; use a smart-bus or Dynamixel gripper servo, whose "
                       "torque limit sets the squeeze whatever the size")
        if grip["squeeze_n"] < lo - 0.005:
            if ol.get("band_capped"):
                out.append(f"gripper: the hobby gripper servo has no torque limit and closes by position only, so G "
                           f"closes {ol['squeeze_mm']:.2f} mm past a gauge-sized {name} to keep {large}, {keep}: that "
                           f"squeezes a gauge-sized {name} with only {grip['squeeze_n']:g} N per finger, below the "
                           f"{lo:g} N it needs not to slip: use a smart-bus or Dynamixel gripper servo, whose torque "
                           "limit sets the squeeze whatever the size")
            elif ol.get("capped"):    # what the servo holds continuously (its rated torque)
                out.append(f"gripper: the hobby gripper servo has no torque limit and holds only {grip['squeeze_n']:g} N "
                           f"per finger continuously (its rated torque), below the {lo:g} N the {name} needs not to slip: "
                           "use a stronger gripper servo, or a bus servo whose torque limit sets the squeeze")
        return out
    if t > grip["limit_n"]:
        return [f"grip target {t:g} N is above the firmware's {grip['limit_n']:g} N hard stop"]
    return []


def _family_of(key: str) -> str:
    return ACTUATORS[key].get("family", "smart_bus")


def _gripper_key(design, sizing: dict, family: str) -> tuple[str, list]:
    """The gripper actuator sizing chose (the CAD hand is built around it). One on another bus is a plan issue
    (bus_problems), never swapped here, so the electronics always match the robot that gets built."""
    warns = []
    g = sizing.get("gripper") if isinstance(sizing.get("gripper"), dict) else None
    key = (g or {}).get("actuator") or sizing.get("gripper_actuator") or ""
    if key not in ACTUATORS:
        fam_keys = catalog.FAMILY_ACTUATORS.get(family, [])
        fallback = {"hobby_pwm": "mg996r", "smart_bus": "sts3215", "dynamixel": "xl430_w250"}.get(family, fam_keys[0])
        if key:
            warns.append(f"gripper actuator {key} is not in the catalog; using {fallback} on the {family} bus")
        key = fallback
    return key, warns


def bus_problems(acts: list, family: str) -> list:
    """One line per actuator that can't share this family's bus: other wiring (PWM vs TTL) or another command
    protocol (Feetech and DYNAMIXEL servos share the TTL wiring, not the commands). Firmware drives one protocol."""
    bus, proto, _ = FAMILY_BUS.get(family, FAMILY_BUS["smart_bus"])
    want, out = SPEAKS[proto], []
    for a in acts:
        spec, have = ACTUATORS.get(a["key"]), catalog.protocol(a["key"])
        if spec is None:
            out.append(f"{a['joint']}: {a['key']} is not in the actuator catalog")
        elif spec.get("interface") != ("pwm" if bus == "pwm" else "ttl_half_duplex"):
            out.append(f"{a['joint']}: {a['key']} is not a {bus} actuator; all joints must share the {family} bus")
        elif have != want:
            out.append(f"{a['joint']}: {a['key']} speaks {catalog.PROTOCOLS.get(have, 'an unknown protocol')} but the "
                       f"{family} bus speaks {catalog.PROTOCOLS[want]}; one bus can't mix protocols, so use a {family} "
                       "actuator here or change arm.actuator_family for the whole arm")
    return out


def actuator_list(design, sizing: dict) -> tuple[list, list]:
    """[{joint, key, id, channel, torque_req_nm, torque_with_margin_nm}] arm joints + gripper, bus ids from 1."""
    family = design.arm.actuator_family
    bus, proto, _ = FAMILY_BUS.get(family, FAMILY_BUS["smart_bus"])
    out = []
    for i, j in enumerate(sizing.get("joints", [])):
        n = max(int(j.get("count", 1) or 1), 1)        # dual (back-to-back) actuators share one joint
        for k in range(n):
            out.append({"joint": j["name"], "key": j["actuator"], "torque_req_nm": (j.get("torque_req_nm") or 0.0) / n,
                        "torque_with_margin_nm": (j.get("torque_with_margin_nm") or 0.0) / n,
                        "sid": j.get("servo_id") if n == 1 else None, "mirror": k > 0, "count": n})
    gkey, warns = _gripper_key(design, sizing, family)
    g = sizing.get("gripper") if isinstance(sizing.get("gripper"), dict) else {}
    out.append({"joint": "gripper", "key": gkey, "torque_req_nm": g.get("torque_req_nm"),
                "torque_with_margin_nm": g.get("torque_with_margin_nm"), "sid": g.get("servo_id"), "mirror": False, "count": 1})
    for i, a in enumerate(out):
        if bus == "pwm":
            a["id"], a["channel"] = i + 1, i
        else:
            a["id"], a["channel"] = int(a.get("sid") or (i + 1)), None
        a.pop("sid", None)
    return out, warns


# ------------------------------------------------------------------ power

def _actuator_currents(a: dict, design, grip: dict) -> tuple[float, float, float]:
    """(peak, avg, stall) current for one actuator. The gripper's comes from the squeeze its firmware holds the object
    with (grip["squeeze_n"]: the target, or what a hobby servo's open-loop close gives), its servo's share of stall."""
    s = ACTUATORS[a["key"]]
    tau = s["torque_nm"]
    if a["joint"] == "gripper":
        from ..electronics import jaw_torque
        t_need = jaw_torque(grip.get("squeeze_n") or grip["target_n"], grip["lever_mm"], grip["jaw"])
        r_peak = min(1.0, max(t_need * 1.6 / tau, 0.35))
        r_avg = min(1.0, t_need / tau)
    else:
        r_peak = min(1.0, max((a.get("torque_with_margin_nm") or 0.3 * tau) / tau, 0.35))
        r_avg = min(1.0, (a.get("torque_req_nm") or 0.15 * tau) / tau)
    peak = s["idle_a"] + s["stall_a"] * r_peak
    avg = s["idle_a"] + s["stall_a"] * r_avg * 0.5
    return peak, avg, s["stall_a"]


def _servo_voltage(acts: list) -> tuple[tuple, float, list]:
    lo = max(ACTUATORS[a["key"]]["voltage"][0] for a in acts)
    hi = min(ACTUATORS[a["key"]]["voltage"][1] for a in acts)
    nom = min(ACTUATORS[a["key"]]["nominal_v"] for a in acts)
    issues = []
    if lo > hi:
        issues.append(f"actuators need incompatible supply voltages ({lo:g} V min > {hi:g} V max): split the servo rail")
    return (lo, hi), nom, issues


def _derated(acts: list, servo_v: float, requested: bool = False) -> list:
    """Warnings for servos the rail runs below their rated voltage: each gives torque_at(rail) of its rated stall torque,
    which sizing already counts in every margin (a joint that falls short is a sizing issue). Below 70 % on a rail
    nobody asked for, the servo is in the wrong voltage class (a 12 V STS3215 beside 7.4 V ones): say so."""
    low = {}
    for a in acts:
        k = catalog.torque_at(a["key"], servo_v) / ACTUATORS[a["key"]]["torque_nm"]
        if k < 0.995:
            low.setdefault(a["key"], (k, []))[1].append(a["joint"])
    out = []
    for key, (k, joints) in low.items():
        rated = ACTUATORS[key]["nominal_v"]
        out.append(f"{', '.join(dict.fromkeys(joints))}: {key} is rated at {rated:g} V but the shared servo rail runs at "
                   f"{servo_v:g} V, so it gives about {k:.0%} of its rated torque (the torque margins are worked out at "
                   f"{servo_v:g} V)" + ("; put every servo on one voltage class to use all of it" if k < 0.7 and not requested
                                        else ""))
    return out


RAIL_MIN_TORQUE = 0.8   # a supply below the servos' rated voltage is used only while every servo keeps 80 % of its torque


def _supplies() -> list:
    return [(k, m) for k, m in MODULES.items() if m.get("role") == "supply" and m.get("external") and "battery" not in k]


def _peak_estimate(acts: list) -> float:
    """Servo-rail peak current (A) from the sizing alone (the gripper at its 35 % floor): the rail choice needs it before
    the grip force is known."""
    out = 0.0
    for a in acts:
        s = ACTUATORS[a["key"]]
        need = a.get("torque_with_margin_nm") or 0.3 * s["torque_nm"]
        out += s["idle_a"] + s["stall_a"] * min(1.0, max(need / s["torque_nm"], 0.35))
    return out


def servo_rail(acts: list, requested: float = 0.0) -> dict:
    """The one servo rail's voltage for these actuators ([{key, joint, torque_with_margin_nm}], one per servo), decided
    with the sizing (its margins and the simulation's efforts are worked out at it) and the same for every controller
    choice. `requested` (electronics.servo_v) wins when every servo takes it. Otherwise the servos' rated voltage when a
    catalogued supply gives it with the current; else the catalogued supply voltage nearest to it inside every servo's
    range that keeps each at RAIL_MIN_TORQUE of its torque or more; else the rated voltage from a 12 V supply through
    the adjustable servo buck. {v, rated_v, range, requested, note}: note says why a request was not taken."""
    keys = list(dict.fromkeys(a["key"] for a in acts if a["key"] in ACTUATORS))
    lo = max(ACTUATORS[k]["voltage"][0] for k in keys)
    hi = min(ACTUATORS[k]["voltage"][1] for k in keys)
    rated = max(ACTUATORS[k]["nominal_v"] for k in keys)
    out = {"v": None, "rated_v": rated, "range": [lo, hi], "requested": requested or None, "note": None}
    if lo > hi:   # no voltage suits them all: the plan says so and makes no wiring
        out["v"] = min(max(min(ACTUATORS[k]["nominal_v"] for k in keys), lo), hi)
        return out
    if requested:
        bad = [k for k in keys if not ACTUATORS[k]["voltage"][0] <= requested <= ACTUATORS[k]["voltage"][1]]
        if not bad:
            out["v"] = float(requested)
            return out
        out["note"] = (f"a {requested:g} V servo supply was asked for, but " + ", ".join(
            f"the {k} takes {ACTUATORS[k]['voltage'][0]:g}-{ACTUATORS[k]['voltage'][1]:g} V" for k in bad))
    target = min(rated, hi)
    need = _peak_estimate([a for a in acts if a["key"] in ACTUATORS]) * 1.25
    k_min = lambda v: min(catalog.torque_at(k, v) / ACTUATORS[k]["torque_nm"] for k in keys)  # noqa: E731
    volts = sorted({m["out_v"] for _, m in _supplies() if m["out_a"] >= need})   # voltages a supply gives with the current
    if any(abs(v - target) <= 0.03 * target for v in volts):
        v = target
    else:
        near = [v for v in volts if lo - 1e-6 <= v <= hi + 1e-6 and k_min(v) >= RAIL_MIN_TORQUE]
        v = min(near, key=lambda x: (abs(x - target), -x)) if near else target
    out["v"] = float(v)
    if out["note"]:
        out["note"] += f": the servo rail stays at {v:g} V"
    return out


def _pick_supply(servo_v: float, need_a: float) -> dict:
    """The supply for a servo rail of `servo_v`: the cheapest that gives that voltage itself (within 3 %) with enough
    current; else, below 12 V, the cheapest 12 V supply with enough current and the adjustable servo buck; else the
    strongest at that voltage (the power budget then says it is short)."""
    at = sorted(((m["out_a"] < need_a * 1.25, catalog.price(k), k) for k, m in _supplies()
                 if abs(m["out_v"] - servo_v) <= 0.03 * servo_v))
    if at and not at[0][0]:
        k = at[0][-1]
        return {"key": k, "v": MODULES[k]["out_v"], "a": MODULES[k]["out_a"], "servo_buck": False}
    need_in = need_a * servo_v / (12.0 * 0.9)
    twelve = sorted(((m["out_a"] < need_in * 1.25, catalog.price(k), k) for k, m in _supplies() if m["out_v"] == 12.0))
    if twelve and servo_v < 11.5 and (not twelve[0][0] or not at):
        k = twelve[0][-1]
        return {"key": k, "v": 12.0, "a": MODULES[k]["out_a"], "servo_buck": True}
    k = max(at, key=lambda c: MODULES[c[-1]]["out_a"])[-1] if at else "psu_12v"
    return {"key": k, "v": MODULES[k]["out_v"], "a": MODULES[k]["out_a"], "servo_buck": False}


def rail_acts(sizing: dict) -> list:
    """One entry per servo for servo_rail: sizing's joints (a dual joint counts twice) and its gripper."""
    out = []
    for j in sizing.get("joints", []):
        n = max(int(j.get("count", 1) or 1), 1)
        out += [{"key": j["actuator"], "joint": j["name"], "torque_with_margin_nm": (j.get("torque_with_margin_nm") or 0) / n}] * n
    g = sizing.get("gripper") if isinstance(sizing.get("gripper"), dict) else {}
    key = g.get("actuator") or sizing.get("gripper_actuator")
    if key:
        out.append({"key": key, "joint": "gripper", "torque_with_margin_nm": g.get("torque_with_margin_nm")})
    return out


def power_budget(design, acts: list, config: str, supply: dict, extras: list, ctrl: str, grip: dict | None = None) -> dict:
    grip = grip_window(design, None, grip)
    peaks = [_actuator_currents(a, design, grip) for a in acts]
    servo_v = supply.get("servo_v", supply["v"])
    s_peak = sum(p[0] for p in peaks)
    s_avg = sum(p[1] for p in peaks)
    s_stall = sum(p[2] for p in peaks)
    logic_peak = 0.38 if ctrl.startswith("esp32") else 0.12      # ESP32 radio TX bursts
    logic_avg = 0.12 if ctrl.startswith("esp32") else 0.05
    if "imu" in extras:
        logic_peak += 0.02
        logic_avg += 0.015
    if design.arm.actuator_family == "hobby_pwm":
        logic_peak += 0.01
        logic_avg += 0.01
    logic_peak += 0.03   # buffer, LED, dividers
    rails = []
    if supply.get("servo_buck"):
        rails.append({"name": f"V_SERVO {servo_v:g} V (servos)", "v": servo_v, "peak_a": round(s_peak, 2),
                      "avg_a": round(s_avg, 2), "stall_a": round(s_stall, 2), "source": "buck_servo · 20 A"})
    else:
        rails.append({"name": f"V_SERVO {servo_v:g} V (servos)", "v": servo_v, "peak_a": round(s_peak, 2),
                      "avg_a": round(s_avg, 2), "stall_a": round(s_stall, 2), "source": f"{supply['key']} · {supply['a']:g} A"})
    if config == "custom_pcb":
        if supply["v"] > 6.5:
            rails.append({"name": "5V (logic, buck)", "v": 5.0, "peak_a": round(logic_peak * 1.05, 2), "avg_a": round(logic_avg, 2),
                          "source": "TPS563201 buck · 3 A"})
        rails.append({"name": "3V3 (MCU, sensors)", "v": 3.3, "peak_a": round(logic_peak, 2), "avg_a": round(logic_avg, 2),
                      "source": "AMS1117-3.3 · 1 A"})
    else:
        rails.append({"name": "5V (controller)", "v": 5.0, "peak_a": round(logic_peak * 1.1, 2), "avg_a": round(logic_avg, 2),
                      "source": "buck_5v · 3 A" if supply["v"] >= 6.0 else f"{supply['key']} · {supply['a']:g} A"})
        rails.append({"name": "3V3 (MCU, sensors)", "v": 3.3, "peak_a": round(logic_peak, 2), "avg_a": round(logic_avg, 2),
                      "source": "controller LDO · 0.5 A"})
    eff = 0.9
    servo_in = s_peak * servo_v / (supply["v"] * eff) if supply.get("servo_buck") else s_peak
    servo_in_avg = s_avg * servo_v / (supply["v"] * eff) if supply.get("servo_buck") else s_avg
    logic_in = logic_peak * 5.0 / (supply["v"] * 0.85)
    in_peak = servo_in + logic_in
    in_avg = servo_in_avg + logic_avg * 5.0 / (supply["v"] * 0.85)
    in_stall = (s_stall * servo_v / (supply["v"] * eff) if supply.get("servo_buck") else s_stall) + logic_in
    margin = supply["a"] / max(in_peak, 1e-6)
    fuse = next((f for f in STD_FUSES if f >= max(in_peak * 1.1, in_avg * 1.5)), STD_FUSES[-1])
    if fuse > supply["a"] >= in_peak:          # the supply's own current limit is the tighter bound
        fuse = max(f for f in STD_FUSES if f <= supply["a"])
    feed_awg = pick_awg(min(fuse, max(in_peak, 1.0)))
    return {"rails": rails, "supply_ok": bool(margin >= 1.1), "supply_a": supply["a"], "supply_v": supply["v"],
            "supply_w": round(supply["a"] * supply["v"], 1), "peak_w": round(in_peak * supply["v"], 1),
            "input_peak_a": round(in_peak, 2), "input_avg_a": round(in_avg, 2), "input_stall_a": round(in_stall, 2),
            "margin": round(margin, 2), "fuse_a": fuse, "feed_awg": feed_awg,
            "per_actuator": [{"joint": a["joint"], "key": a["key"], "peak_a": round(p[0], 2), "avg_a": round(p[1], 2),
                              "stall_a": p[2]} for a, p in zip(acts, peaks)]}


# ------------------------------------------------------------------ plan

def _controller(design, config: str, family: str, extras: list) -> tuple[str, list]:
    warns = []
    if config == "custom_pcb":
        if design.electronics.mcu not in ("auto", "esp32_s3_devkit", "esp32_devkit"):
            warns.append(f"the custom PCB is built around an ESP32-S3-WROOM-1; controller '{design.electronics.mcu}' ignored")
        return "esp32_s3_wroom", warns
    if family == "dynamixel":
        if design.electronics.mcu not in ("auto",):
            warns.append("DYNAMIXEL modules build uses the ROBOTIS OpenRB-150 (built-in TTL ports + Dynamixel2Arduino); "
                         f"controller '{design.electronics.mcu}' ignored")
        if "wifi" in extras:
            warns.append("OpenRB-150 has no Wi-Fi: commands come over USB serial (switch to the custom PCB for Wi-Fi)")
        return "openrb_150", warns
    mcu = design.electronics.mcu if design.electronics.mcu != "auto" else "esp32_s3_devkit"
    bus = FAMILY_BUS.get(family, FAMILY_BUS["smart_bus"])[0]
    if mcu == "arduino_nano" and bus != "pwm":
        warns.append("Arduino Nano has one UART, shared with USB, so it cannot run the servo bus and take commands: "
                     "using an ESP32-S3-DevKitC-1 instead")
        mcu = "esp32_s3_devkit"
    if "wifi" in extras and mcu not in WIFI_MCUS:
        if design.electronics.mcu == "auto":
            mcu = "esp32_s3_devkit"
        else:
            warns.append(f"{mcu} has no Wi-Fi; the wifi extra needs an ESP32 controller")
    if mcu not in PINMAP:
        warns.append(f"unknown controller {mcu}; using ESP32-S3-DevKitC-1")
        mcu = "esp32_s3_devkit"
    return mcu, warns


def board_size(design, family: str, n_act: int, extras: list) -> list:
    """Controller PCB outline, sized to the base bay (stub arm: 140 mm base disc)."""
    w, h = 92.0, 66.0
    if family == "hobby_pwm" and n_act > 6:
        w += 6.0
    return [w, h]


def plan(design, sizing: dict, grip: dict | None = None) -> dict:
    """The electronics plan for a sized robot. `grip` is the gripper stage's force plan (results.json gripper.force); see
    grip_window for callers without one."""
    config = design.electronics.config if design.electronics.config in ("modules", "custom_pcb") else "modules"
    family = design.arm.actuator_family if design.arm.actuator_family in FAMILY_BUS else "smart_bus"
    bus, proto, baud = FAMILY_BUS[family]
    extras = extras_of(design)
    issues, warnings = [], []
    if design.electronics.config not in ("modules", "custom_pcb"):
        warnings.append(f"unknown electronics.config '{design.electronics.config}', using modules")
    acts, w = actuator_list(design, sizing)
    warnings += w
    issues += bus_problems(acts, family)
    if bus == "pwm" and len(acts) > 16:
        issues.append(f"{len(acts)} PWM servos exceed the PCA9685's 16 channels")
    if bus != "pwm":
        ids = [a["id"] for a in acts]
        if len(set(ids)) != len(ids):
            issues.append(f"duplicate bus servo ids {ids}")
    ctrl, w = _controller(design, config, family, extras)
    warnings += w

    vrange, nom, w = _servo_voltage(acts)
    issues += w
    grip = grip_window(design, sizing, grip)
    rail = sizing.get("servo_rail") if (sizing.get("servo_rail") or {}).get("v") else servo_rail(acts, design.electronics.servo_v)
    servo_v = float(rail["v"])
    gkey = next((a["key"] for a in acts if a["joint"] == "gripper"), None)
    from ..electronics import jaw_force
    # what the gripper servo squeezes at stall on this rail, per finger (the one jaw model); its torque limit, where its
    # bus has one; without one (a hobby servo) how far past the object the firmware closes it (open_loop_squeeze)
    grip["stall_force_n"] = (round(jaw_force(catalog.torque_at(gkey, servo_v), grip["lever_mm"], grip["jaw"]), 2)
                             if gkey in ACTUATORS else None)
    grip["torque_limit"] = grip_torque_limit(grip, gkey, servo_v, family)
    tl = grip["torque_limit"]
    grip["open_loop"] = (open_loop_squeeze(grip, gkey, family, design.object.diameter_mm)
                         if not tl and grip["stall_force_n"] else None)
    grip["squeeze_n"] = (grip["open_loop"] or {}).get("squeeze_n") or grip["target_n"]
    if (grip["open_loop"] or {}).get("squeeze_mm"):
        if grip["squeeze_n"] > grip["limit_n"] + 0.005:   # a long lever: one position command already squeezes hard
            name = design.object.name or "object"
            issues.append(f"gripper: the {gkey} has no torque limit, and the least it can close past the {name} (two "
                          f"position commands, {grip['open_loop']['squeeze_mm']:.2f} mm at the jaw) squeezes about "
                          f"{grip['squeeze_n']:g} N per finger, above the {grip['limit_n']:g} N limit for the {name}: add "
                          "force sensors, or use a bus servo whose torque limit sets the squeeze")
        # F moves a hobby servo's open-loop squeeze, up to the object's limit, what the servo holds continuously (its
        # rated torque) or what keeps the object at the large end of its size under PROBE_STALL_SHARE of the stall and
        # its damage force, whichever is lowest: no F command holds a gauge-sized object past its rated torque, nor one
        # of the object's sizes near stall
        ol = grip["open_loop"]
        grip["limit_n"] = round(min(grip["limit_n"], ol["rated_n"], ol["band_n"] if ol["covers"] else float("inf")), 2)
    rough_peak = sum(_actuator_currents(a, design, grip)[0] for a in acts)
    supply = _pick_supply(servo_v, rough_peak)
    supply["servo_v"] = servo_v
    if rail.get("note"):
        warnings.append(rail["note"])
    warnings += _derated(acts, servo_v, servo_v == rail.get("requested"))
    if config == "custom_pcb" and supply["servo_buck"]:
        issues.append(f"no catalogued supply gives the servos' {servo_v:g} V; the custom PCB passes its input straight to "
                      "the servo rail, so use the modules config (12 V + servo buck)")
    issues += rail_problems(acts, {**supply, "servo_v": servo_v}, config)
    pb = power_budget(design, acts, config, supply, extras, ctrl, grip)
    if not pb["supply_ok"]:
        issues.append(f"supply {supply['key']} ({supply['a']:g} A) has {pb['margin']:.2f}x margin over the "
                      f"{pb['input_peak_a']:.1f} A peak: pick a bigger supply or slower motion profiles")
    if pb["input_stall_a"] > supply["a"]:
        warnings.append(f"all servos stalled together would draw {pb['input_stall_a']:.1f} A > {supply['a']:g} A supply: "
                        "the firmware ramps targets and the supply's current limit / the fuse protect the rest")
    if config == "custom_pcb" and pb["input_peak_a"] > 8.0:
        warnings.append(f"{pb['input_peak_a']:.1f} A peak through the PCB power path: order 2 oz copper (JLCPCB option)")
    if config == "modules" and supply["v"] < 7.0 and ctrl != "openrb_150":
        warnings.append("buck_5v from a 6 V input runs near its dropout; verify the regulator's minimum input at 1 A")
    issues += grip_problems(design, grip)
    if tl and tl["short"] and grip["sensor"] != "fsr402":
        issues.append(f"gripper: the {gkey} squeezes only {tl['stall_force_n']:g} N per finger on the {servo_v:g} V rail, "
                      f"below the {grip['target_n']:g} N grip target")

    # modules: what physically goes in the base bay (arm.build places the non-external ones)
    mods = []
    if config == "custom_pcb":
        mods.append({"ref": "U1", "key": "morph_controller_pcba", "role": "controller"})
    else:
        mods.append({"ref": "U1", "key": ctrl, "role": "controller"})
        n = 2
        if family == "hobby_pwm":
            mods.append({"ref": f"U{n}", "key": "pca9685_board", "role": "servo_driver"})
            n += 1
        elif family in ("smart_bus", "smart_bus_7v4"):
            mods.append({"ref": f"U{n}", "key": "bus_servo_adapter", "role": "servo_driver"})
            n += 1
        if ctrl != "openrb_150" and supply["v"] >= 6.0:
            mods.append({"ref": f"U{n}", "key": "buck_5v", "role": "power"})
            n += 1
        if supply["servo_buck"]:
            mods.append({"ref": f"U{n}", "key": "buck_servo", "role": "power"})
            n += 1
        mods.append({"ref": "F1", "key": "fuse_inline", "role": "protection"})
    if "estop" in extras:
        mods.append({"ref": "SW1", "key": "estop", "role": "safety"})
    mods.append({"ref": "PS1", "key": supply["key"], "role": "supply"})

    sensors = []
    if grip["sensor"] == "fsr402":
        sensors += [{"ref": "S1", "key": "fsr402", "where": "jaw_left", "signal": "FSR_L"},
                    {"ref": "S2", "key": "fsr402", "where": "jaw_right", "signal": "FSR_R"}]
        ol = grip.get("open_loop") or {}
        if ol.get("probe_mm"):
            name, dmg = design.object.name or "object", design.object.max_force_n
            small = f"a {name} at the small end of its size, {ol['spread_mm']:.2f} mm per jaw narrower than the gauge"
            large = f"a {name} at the large end of its size, {ol['spread_mm']:.2f} mm per jaw wider,"
            whole = lambda n: n >= grip["stall_force_n"] - 0.005   # noqa: E731
            if not ol["covers"] and ACTUATORS[gkey].get("interface") == "pwm":   # (a servo off its bus is an issue already)
                from ..electronics import PROBE_STALL_SHARE
                share = ol["reach_mm"] / ol["servo_band_mm"]
                reach_n = grip["stall_force_n"] * min(share, 1.0)
                big = ol["reach_large_n"]
                issues.append(f"gripper: the {gkey} has no torque limit to back up the force sensors, and reaching {small}, "
                              f"means closing {ol['reach_mm']:.2f} mm past the gauge with no force reading, where a dead "
                              f"sensor would put "
                              + ("its whole stall force" if share >= 1 else
                                 f"{reach_n:.2f} N per finger, {share * 100:.0f} % of its stall force,")
                              + f" on a gauge-sized {name}"
                              + (f" (Morph's probe stops at {PROBE_STALL_SHARE * 100:.0f} %)"
                                 if share > PROBE_STALL_SHARE + 1e-9 else "")
                              + (f", above the {dmg:g} N that damages it" if dmg and reach_n > dmg else "")
                              + (f", and {'its whole stall force' if whole(big) else f'{big:g} N per finger'} on {large[:-1]}, "
                                 f"above the {dmg:g} N that damages it" if dmg and reach_n <= dmg and big > dmg else "")
                              + f": the firmware gives up at {ol['probe_mm']:.2f} mm, so a {name} more than that narrower "
                              "per jaw isn't gripped; use a smart-bus or Dynamixel gripper servo, whose torque limit backs "
                              "up the sensors")
            warnings.append(f"the hobby gripper servo has no torque limit to back up the force sensors, so a close that has read "
                            f"no force by {ol['probe_mm']:.2f} mm past the calibration gauge stops there and opens (!GRIP)"
                            + (f", which reaches {small}" if ol["covers"] else "")
                            + f". With a dead sensor a gauge-sized {name} gets about {ol['probe_n']:g} N per finger there, "
                            f"{ol['probe_n'] / grip['stall_force_n'] * 100:.0f} % of the servo's stall force and current, "
                            f"until the jaw opens"
                            + (f" (above the {dmg:g} N that damages it)" if dmg and ol["probe_n"] > dmg else "")
                            + f", and {large} gets "
                            + ("its whole stall force" if whole(ol["large_n"]) else f"about {ol['large_n']:g} N per finger")
                            + " for that moment"
                            + (f" (above the {dmg:g} N that damages it)" if dmg and ol["large_n"] > dmg else ""))
        if not grip.get("torque_limit") and grip.get("stall_force_n") and design.object.max_force_n and \
                grip["stall_force_n"] > design.object.max_force_n:
            warnings.append(f"the hobby gripper servo has no torque limit to back up the force sensors: a sensor that reads "
                            f"low lets it squeeze with up to {grip['stall_force_n']:g} N per finger, above the "
                            f"{design.object.max_force_n:g} N that damages the {design.object.name or 'object'}")
    elif grip.get("torque_limit"):
        warnings.append(f"no force sensors on the fingers: G closes the jaws past the object and the gripper servo's torque "
                        f"limit sets the squeeze ({grip['torque_limit']['fraction'] * 100:.0f} % of its stall torque for "
                        f"{grip['target_n']:g} N per finger), open loop: check it once with a kitchen scale")
    elif grip.get("open_loop"):
        from ..electronics import PROBE_STALL_SHARE

        ol = grip["open_loop"]
        share = grip["squeeze_n"] / grip["stall_force_n"]
        name = design.object.name or "object"
        dmg = design.object.max_force_n
        under = (f"under {PROBE_STALL_SHARE * 100:.0f} % of the stall"
                 + (f" and its {dmg:g} N damage force" if dmg else ""))
        large = f"a {name} at the large end of its size, {ol['spread_mm']:.2f} mm per jaw wider than the gauge,"
        why = (f" (more than the {grip['target_n']:g} N target: two position commands are the least it can close past it)"
               if grip["squeeze_n"] > grip["target_n"] + 0.005 else
               f" (less than the {grip['target_n']:g} N target: the most that keeps one at the large end of its size "
               f"{under})" if ol.get("band_capped") else
               f" (less than the {grip['target_n']:g} N target: the most it holds continuously, its rated torque)"
               if ol.get("capped") else "")
        cap = ("the servo's rated torque" if grip["limit_n"] >= ol["rated_n"] - 0.005 else
               f"what keeps a {name} at the large end of its size {under}"
               if ol["covers"] and grip["limit_n"] >= ol["band_n"] - 0.005 else f"the {name}'s limit")
        big = ol["large_n"] / grip["stall_force_n"]
        warnings.append(f"no force sensors on the fingers and no torque limit on the hobby gripper servo: G closes the jaws "
                        f"only {ol['squeeze_mm']:.2f} mm past the calibration gauge, where the servo's own position loop "
                        f"squeezes a gauge-sized {name} about {grip['squeeze_n']:g} N per finger{why}, {share * 100:.0f} % "
                        f"of its stall force and current. "
                        + (f"{large[0].upper()}{large[1:]} gets about {ol['large_n']:g} N per finger, {big * 100:.0f} % of "
                           f"it" + (f" (above the {ol['rated_n']:g} N it holds continuously: it runs warm on long carries)"
                                    if ol["large_n"] > ol["rated_n"] + 0.005 else "")
                           + f": G never closes as far as its {grip['stall_force_n']:g} N stall, about "
                           f"{ol['servo_band_mm']:g} mm past contact, where it would sit for the whole carry with no "
                           f"overload protection" if ol["covers"] else
                           f"{large[0].upper()}{large[1:]} gets "
                           + ("its whole stall force" if big >= 0.995 else f"about {ol['large_n']:g} N per finger, "
                              f"{big * 100:.0f} % of it") + " (a plan issue)")
                        + f". Open loop: check the squeeze once with a kitchen scale; F <newtons> sets how far G closes, "
                        f"up to {grip['limit_n']:g} N ({cap})")
    else:
        warnings.append("no force sensors on the fingers and no torque limit on the gripper servo (open loop)")
    if gkey in ACTUATORS and grip.get("stall_force_n") and ACTUATORS[gkey].get("rated_nm"):
        share, rated = grip["squeeze_n"] / grip["stall_force_n"], ACTUATORS[gkey]["rated_nm"] / ACTUATORS[gkey]["torque_nm"]
        if share > rated + 1e-6:     # the rated torque is what a servo sustains; a hold above it heats the motor
            warnings.append(f"the gripper's {gkey} holds {grip['squeeze_n']:g} N per finger at {share * 100:.0f} % of its "
                            f"stall torque, above the {rated * 100:.0f} % it is rated to hold continuously: it runs warm "
                            f"on long carries")
    if "wrist_camera" in extras:
        sensors.append({"ref": "CAM1", "key": "wrist_camera", "where": "gripper_base", "signal": "USB to host"})
    if "imu" in extras:
        sensors.append({"ref": "IMU1", "key": "imu", "where": "gripper_base", "signal": "I2C 0x4A"})
        warnings.append("IMU at the wrist runs I2C over ~0.6 m: keep it at 100 kHz on a twisted 4-wire cable")

    pcb = None
    if config == "custom_pcb":
        size = board_size(design, family, len(acts), extras)
        pcb = {"name": "Morph controller PCBA", "size_mm": size, "board_json": "board/board.json",
               "mcu": "ESP32-S3-WROOM-1-N8R8", "layers": 2, "thickness_mm": 1.6,
               "mounting_holes": [[3.5, 3.5], [size[0] - 3.5, 3.5], [3.5, size[1] - 3.5], [size[0] - 3.5, size[1] - 3.5]],
               "hole_d_mm": 3.2}
    fsr_div = nearest_e12(FSR_K_OHM_N / max(grip["target_n"], 0.3)) if grip["sensor"] == "fsr402" else None
    return {
        "config": config, "bus": bus, "protocol": proto, "baud": baud, "family": family, "controller": ctrl,
        "supply": {"key": supply["key"], "v": supply["v"], "a": supply["a"], "servo_v": servo_v,
                   "servo_buck": supply["servo_buck"]},
        "modules": mods, "custom_pcb": pcb, "actuators": acts, "sensors": sensors, "extras": extras,
        "grip": {**grip, "fsr_divider_ohm": fsr_div},
        "base_bay_mm": [110.0, 80.0],
        "power_budget": pb, "issues": issues, "warnings": warnings,
    }


# ------------------------------------------------------------------ circuits
# Parts library for the custom PCBA: (value, mpn, lcsc | "verify", unit price USD @ qty 10, footprint)
P = {
    "R10k": ("10k", "0603WAF1002T5E", "C25804", 0.002, "R0603"),
    "R100k": ("100k", "0603WAF1003T5E", "C25803", 0.002, "R0603"),
    "R22k": ("22k", "0603WAF2202T5E", "C31850", 0.002, "R0603"),
    "R5k1": ("5.1k", "0603WAF5101T5E", "C23186", 0.002, "R0603"),
    "R4k7": ("4.7k", "0603WAF4701T5E", "C23162", 0.002, "R0603"),
    "R1k": ("1k", "0603WAF1001T5E", "C21190", 0.002, "R0603"),
    "R56k": ("56k", "0603WAF5602T5E", "verify", 0.002, "R0603"),
    "R220": ("220", "0603WAF2200T5E", "verify", 0.002, "R0603"),
    "C100n": ("100n", "CC0603KRX7R9BB104", "C14663", 0.003, "C0603"),
    "C1u": ("1u", "CL10A105KB8NNNC", "C15849", 0.004, "C0603"),
    "C10u": ("10u 25V", "CL21A106KAYNNNE", "C15850", 0.012, "C0805"),
    "C22u": ("22u 25V", "CL21A226MAQNNNE", "C45783", 0.02, "C0805"),
    "SS34": ("SS34", "SS34", "C8678", 0.03, "SMA"),
    "2N7002": ("2N7002", "2N7002", "C8545", 0.02, "SOT23"),
    "AOD4185": ("AOD4185", "AOD4185 (P-ch 40 V 40 A)", "verify", 0.45, "TO252"),
    "AMS1117": ("AMS1117-3.3", "AMS1117-3.3", "C6186", 0.12, "SOT223"),
    "TPS563201": ("TPS563201", "TPS563201DDCR", "verify", 0.35, "SOT23_6"),
    "L4u7": ("4.7uH", "SWPA4030S4R7MT (2.2 A)", "verify", 0.08, "L_4x4"),
    "ESP32S3": ("ESP32-S3-WROOM-1", "ESP32-S3-WROOM-1-N8R8", "verify", 4.3, "ESP32S3_WROOM1"),
    "USBC": ("USB-C", "TYPE-C-31-M-12", "C165948", 0.25, "USBC_16P"),
    "USBLC6": ("USBLC6-2SC6", "USBLC6-2SC6", "C7519", 0.1, "SOT23_6"),
    "LVC2G241": ("74LVC2G241", "SN74LVC2G241DCTR", "verify", 0.18, "SSOP8"),
    "PCA9685": ("PCA9685PW", "PCA9685PW,118", "verify", 1.6, "TSSOP28"),
    "BTN": ("SW_Push", "TS-1187A-B-A-B", "C318884", 0.03, "SW_SMD_5x5"),
    "LED_R": ("RED", "KT-0603R", "C2286", 0.01, "LED0603"),
    "LED_G": ("GREEN", "KT-0603G", "verify", 0.01, "LED0603"),
    "XT30": ("XT30PW-M", "Amass XT30PW-M", "verify", 0.55, "XT30PW"),
    "PH2": ("JST-PH 2p", "B2B-PH-K-S(LF)(SN)", "verify", 0.03, "JST_PH_2"),
    "XH2": ("JST-XH 2p", "B2B-XH-A(LF)(SN)", "verify", 0.03, "JST_XH_2"),
    "SH4": ("Qwiic 4p", "SM04B-SRSS-TB(LF)(SN)", "verify", 0.08, "JST_SH_4"),
    "BUS_FT": ("5264 3p", "A2501WV-3P (Molex 5264 compatible)", "verify", 0.04, "CONN_2P5_3"),
    "BUS_DXL": ("JST EH 3p", "B3B-EH-A(LF)(SN)", "verify", 0.05, "CONN_2P5_3"),
    "CBULK": ("470u", "470 uF 25 V radial 10x16 low-ESR", "verify", 0.3, "CP_D10"),
    "MH": ("M3", "M3 mounting hole (NPTH 3.2 mm)", "", 0.0, "MH_M3"),
}


def _part(c: Circuit, ref, key, kind, pins, block, value=None, place=None, note="", **kw) -> object:
    val, mpn, lcsc, price, fp = P[key]
    return c.add(ref, value or val, kind, pins, footprint=fp, mpn=mpn, lcsc=lcsc, price=price, block=block,
                 place=place or {}, note=note, **kw)


def _tvs_for(v: float) -> str:
    standoff = next(x for x in (5.0, 6.0, 6.5, 7.0, 8.5, 10.0, 12.0, 13.0, 15.0, 18.0, 20.0, 24.0) if x >= v * 1.1)
    return f"SMBJ{standoff:g}A"


def bus_ports(acts: list, bus: str) -> list:
    """Split the actuators into daisy chains, one per board port: [base yaw], [shoulder], [elbow ... gripper]."""
    if bus == "pwm":
        return [[a] for a in acts]
    if len(acts) <= 3:
        return [acts]
    return [acts[:1], acts[1:2], acts[2:]]


def custom_circuit(design, ep: dict) -> Circuit:
    vin = ep["supply"]["v"]
    pwm = ep["bus"] == "pwm"
    proto = ep["protocol"]
    extras = ep["extras"]
    acts = ep["actuators"]
    fsr = ep["grip"]["sensor"] == "fsr402"
    pm = PINMAP["esp32_s3_wroom"]
    blocks = [("power", "POWER INPUT · PROTECTION · SERVO RAIL SWITCH"), ("regs", "LOGIC SUPPLY"),
              ("mcu", "ESP32-S3 CONTROLLER"), ("usb", "USB-C (PROGRAMMING · HOST LINK)"),
              ("pwm", f"PWM SERVO DRIVER · {len(acts)} CHANNELS") if pwm else
              ("bus", f"SERVO BUS · {'FEETECH SCS' if proto == 'feetech_scs' else 'DYNAMIXEL 2.0'} TTL HALF-DUPLEX"),
              ("sensors", "FORCE SENSORS · EXPANSION"), ("mech", "MECHANICAL")]
    c = Circuit("Morph controller PCBA", subtitle=f"{design.name} · {ep['family']} · {len(acts)} actuators · {vin:g} V in",
                blocks=blocks)
    W, H = ep["custom_pcb"]["size_mm"] if ep.get("custom_pcb") else board_size(design, ep["family"], len(acts), extras)
    fuse_a = ep["power_budget"]["fuse_a"]

    # ---- power input, protection, servo rail switch
    _part(c, "J1", "XT30", "conn", [("1", "+", "VIN_RAW"), ("2", "-", "GND")], "power", value=f"{vin:g}V IN",
          place={"edge": "left", "at": 0.47}, note=f"{vin:g} V in from {ep['supply']['key']}")
    c.add("F1", f"{fuse_a:g}A", "F", [("1", "1", "VIN_RAW"), ("2", "2", "V_FUSED")], footprint="FUSE_2410",
          mpn=f"Littelfuse 0456{int(round(fuse_a)):03d}.ER (NANO2, fast)", lcsc="verify", price=0.6, block="power",
          place={"near": "J1", "side": "right"})
    tvs = _tvs_for(vin)
    c.add("D1", tvs, "TVS", [("1", "K", "V_FUSED"), ("2", "A", "GND")], footprint="SMB", mpn=tvs, lcsc="verify",
          price=0.08, block="power", place={"near": "F1", "side": "below"})
    _part(c, "Q1", "AOD4185", "QP", [("1", "G", "Q1_G"), ("2", "D", "V_FUSED"), ("3", "S", "V_IN")], "power",
          place={"near": "F1", "side": "right"}, note="reverse-polarity")
    _part(c, "R1", "R10k", "R", [("1", "1", "Q1_G"), ("2", "2", "GND")], "power", place={"near": "Q1", "side": "below"})
    if vin > 9:
        c.add("D2", "BZT52C12", "DZ", [("1", "K", "V_IN"), ("2", "A", "Q1_G")], footprint="SOD123", mpn="BZT52C12",
              lcsc="verify", price=0.01, block="power", place={"near": "Q1", "side": "below"})
    _part(c, "Q2", "AOD4185", "QP", [("1", "G", "Q2_G"), ("2", "D", "V_SERVO"), ("3", "S", "V_IN")], "power",
          place={"near": "Q1", "side": "right"}, note="servo rail switch")
    _part(c, "R2", "R10k", "R", [("1", "1", "V_IN"), ("2", "2", "Q2_G")], "power", place={"near": "Q2", "side": "below"})
    loop_net = "ESTOP_LOOP" if "estop" in extras else "Q2_G"
    _part(c, "Q3", "2N7002", "QN", [("1", "G", "SERVO_EN"), ("2", "S", "GND"), ("3", "D", loop_net)], "power",
          place={"near": "Q2", "side": "below"})
    _part(c, "R3", "R1k", "R", [("1", "1", "SERVO_EN_MCU"), ("2", "2", "SERVO_EN")], "power", place={"near": "Q3"})
    _part(c, "R4", "R100k", "R", [("1", "1", "SERVO_EN"), ("2", "2", "GND")], "power", place={"near": "Q3"})
    if "estop" in extras:
        _part(c, "J9", "XH2", "conn", [("1", "1", "Q2_G"), ("2", "2", "ESTOP_LOOP")], "power", value="E-STOP",
              place={"edge": "left", "at": 0.17}, note="E-stop NC loop (opens = servo power off)")
    cb = _part(c, "C1", "CBULK", "CP", [("1", "+", "V_SERVO"), ("2", "-", "GND")], "power",
               value=f"470u {'25V' if vin > 9 else '16V'}", place={"near": "Q2", "side": "below"})
    cb.mpn = f"470 uF {'25' if vin > 9 else '16'} V radial D10 low-ESR"
    _part(c, "R5", "R100k", "R", [("1", "1", "V_SERVO"), ("2", "2", "VSERVO_SENSE")], "power", place={"near": "C1", "side": "right"})
    _part(c, "R6", "R22k", "R", [("1", "1", "VSERVO_SENSE"), ("2", "2", "GND")], "power", place={"near": "R5"})

    # ---- logic supply: buck (VIN > 6.5 V) + diode-OR with USB VBUS + 3V3 LDO
    if vin > 6.5:
        _part(c, "U3", "TPS563201", "box", [("3", "VIN", "V_IN", "L", "power_in"), ("5", "EN", "BUCK_EN", "L", "input"),
                                            ("1", "GND", "GND", "B", "power_in"), ("6", "VBST", "BST", "R", "passive"),
                                            ("2", "SW", "SW", "R", "output"), ("4", "VFB", "FB", "R", "input")], "regs",
              place={"at_xy": (0.15, 0.78)})
        _part(c, "R7", "R100k", "R", [("1", "1", "V_IN"), ("2", "2", "BUCK_EN")], "regs", place={"near": "U3"})
        _part(c, "C2", "C10u", "C", [("1", "1", "V_IN"), ("2", "2", "GND")], "regs", place={"near": "U3"})
        _part(c, "C3", "C100n", "C", [("1", "1", "BST"), ("2", "2", "SW")], "regs", place={"near": "U3"})
        _part(c, "L1", "L4u7", "L", [("1", "1", "SW"), ("2", "2", "5V")], "regs", place={"near": "U3", "side": "right"})
        _part(c, "C4", "C22u", "C", [("1", "1", "5V"), ("2", "2", "GND")], "regs", place={"near": "L1"})
        _part(c, "C5", "C22u", "C", [("1", "1", "5V"), ("2", "2", "GND")], "regs", place={"near": "L1"})
        _part(c, "R8", "R56k", "R", [("1", "1", "5V"), ("2", "2", "FB")], "regs", place={"near": "U3"})
        _part(c, "R9", "R10k", "R", [("1", "1", "FB"), ("2", "2", "GND")], "regs", place={"near": "U3"})
        src = "5V"
    else:
        src = "V_IN"
    _part(c, "D3", "SS34", "DS", [("1", "K", "VSYS"), ("2", "A", src)], "regs", place={"near": "U4", "side": "left"})
    _part(c, "D4", "SS34", "DS", [("1", "K", "VSYS"), ("2", "A", "VBUS")], "regs", place={"near": "U4", "side": "left"})
    _part(c, "U4", "AMS1117", "box", [("3", "VI", "VSYS", "L", "power_in"), ("1", "GND", "GND", "B", "power_in"),
                                      ("2", "VO", "3V3", "R", "power_out")], "regs", place={"at_xy": (0.34, 0.52)})
    _part(c, "C6", "C10u", "C", [("1", "1", "VSYS"), ("2", "2", "GND")], "regs", place={"near": "U4"})
    _part(c, "C7", "C22u", "C", [("1", "1", "3V3"), ("2", "2", "GND")], "regs", place={"near": "U4"})
    _part(c, "R10", "R1k", "R", [("1", "1", "3V3"), ("2", "2", "LED_PWR")], "regs", place={"near": "U4"})
    _part(c, "D5", "LED_G", "LED", [("1", "K", "GND"), ("2", "A", "LED_PWR")], "regs", value="PWR", place={"near": "R10"})

    # ---- MCU
    used = {"BUS_TX": not pwm, "BUS_RX": not pwm, "BUS_DIR": not pwm, "SDA": pwm or "imu" in extras,
            "SCL": pwm or "imu" in extras, "PWM_OE": pwm, "FSR_L": fsr, "FSR_R": fsr, "VSERVO_SENSE": True,
            "SERVO_EN": True, "LED": True, "USB_DM": True, "USB_DP": True}
    sig_net = {"BUS_TX": "UART_TX", "BUS_RX": "UART_RX", "BUS_DIR": "BUS_DIR", "SDA": "SDA", "SCL": "SCL", "PWM_OE": "PWM_OE",
               "FSR_L": "FSR_L", "FSR_R": "FSR_R", "VSERVO_SENSE": "VSERVO_SENSE", "SERVO_EN": "SERVO_EN_MCU",
               "LED": "LED_STAT", "USB_DM": "USB_DM", "USB_DP": "USB_DP"}
    gpio_net = {pm[s]: sig_net[s] for s, u in used.items() if u}
    wroom = [("1", "GND"), ("2", "3V3"), ("3", "EN"), ("4", "IO4"), ("5", "IO5"), ("6", "IO6"), ("7", "IO7"), ("8", "IO15"),
             ("9", "IO16"), ("10", "IO17"), ("11", "IO18"), ("12", "IO8"), ("13", "IO19"), ("14", "IO20"), ("15", "IO3"),
             ("16", "IO46"), ("17", "IO9"), ("18", "IO10"), ("19", "IO11"), ("20", "IO12"), ("21", "IO13"), ("22", "IO14"),
             ("23", "IO21"), ("24", "IO47"), ("25", "IO48"), ("26", "IO45"), ("27", "IO0"), ("28", "IO35"), ("29", "IO36"),
             ("30", "IO37"), ("31", "IO38"), ("32", "IO39"), ("33", "IO40"), ("34", "IO41"), ("35", "IO42"), ("36", "RXD0"),
             ("37", "TXD0"), ("38", "IO2"), ("39", "IO1"), ("40", "GND"), ("41", "EPAD")]
    pins = []
    for num, name in wroom:
        net = {"GND": "GND", "EPAD": "GND", "3V3": "3V3", "EN": "EN", "IO0": "BOOT"}.get(name) or gpio_net.get(name)
        n = int(num)
        side = "L" if n <= 14 or name in ("GND", "3V3", "EN") else "R"
        if name in ("GND", "EPAD"):
            side = "B"
        et = "power_in" if name in ("GND", "3V3", "EPAD") else "bidir"
        pins.append((num, name, net, side, et))
    _part(c, "U1", "ESP32S3", "box", pins, "mcu", place={"edge": "top", "at": 0.74, "rot": 0, "keepout": "antenna"})
    _part(c, "C8", "C10u", "C", [("1", "1", "3V3"), ("2", "2", "GND")], "mcu", place={"near": "U1", "pin": "2", "prio": 1})
    _part(c, "C9", "C100n", "C", [("1", "1", "3V3"), ("2", "2", "GND")], "mcu", place={"near": "U1", "pin": "2", "prio": 1})
    _part(c, "R11", "R10k", "R", [("1", "1", "3V3"), ("2", "2", "EN")], "mcu", place={"near": "U1", "pin": "3"})
    _part(c, "C10", "C1u", "C", [("1", "1", "EN"), ("2", "2", "GND")], "mcu", place={"near": "U1", "pin": "3"})
    _part(c, "SW1", "BTN", "SW", [("1", "1", "EN"), ("2", "2", "GND")], "mcu", value="RESET", place={"near": "U1", "pin": "27", "prio": 2})
    _part(c, "SW2", "BTN", "SW", [("1", "1", "BOOT"), ("2", "2", "GND")], "mcu", value="BOOT", place={"near": "U1", "pin": "27"})
    _part(c, "R12", "R10k", "R", [("1", "1", "3V3"), ("2", "2", "BOOT")], "mcu", place={"near": "U1", "pin": "27"})
    _part(c, "R13", "R1k", "R", [("1", "1", "LED_STAT"), ("2", "2", "LED_K")], "mcu", place={"near": "U1", "pin": "23", "prio": 3})
    _part(c, "D6", "LED_R", "LED", [("1", "K", "GND"), ("2", "A", "LED_K")], "mcu", value="STAT", place={"near": "R13"})

    # ---- USB-C
    _part(c, "J2", "USBC", "conn", [("A1B12", "GND", "GND", "L", "power_in"), ("A4B9", "VBUS", "VBUS", "L", "power_out"),
                                    ("A5", "CC1", "CC1", "R"), ("B5", "CC2", "CC2", "R"), ("A6", "D+", "USB_DP", "R"),
                                    ("B6", "D+", "USB_DP", "R"), ("A7", "D-", "USB_DM", "R"), ("B7", "D-", "USB_DM", "R"),
                                    ("A8", "SBU1", None, "R"), ("B8", "SBU2", None, "R"), ("B4A9", "VBUS", "VBUS", "L", "power_out"),
                                    ("B1A12", "GND", "GND", "L", "power_in"), ("SH", "SHIELD", "GND", "L", "passive")], "usb",
          place={"edge": "top", "at": 0.44}, note="USB 2.0 FS (native ESP32-S3 USB)")
    _part(c, "R14", "R5k1", "R", [("1", "1", "CC1"), ("2", "2", "GND")], "usb", place={"near": "J2", "side": "below", "prio": 0})
    _part(c, "R15", "R5k1", "R", [("1", "1", "CC2"), ("2", "2", "GND")], "usb", place={"near": "J2", "side": "below", "prio": 0})
    _part(c, "U5", "USBLC6", "box", [("1", "IO1", "USB_DP", "L"), ("6", "IO1", "USB_DP", "R"), ("3", "IO2", "USB_DM", "L"),
                                     ("4", "IO2", "USB_DM", "R"), ("5", "VBUS", "VBUS", "T", "power_in"),
                                     ("2", "GND", "GND", "B", "power_in")], "usb", place={"near": "J2", "side": "below", "prio": 0})

    # ---- servo interface
    if pwm:
        n = len(acts)
        led_num = lambda k: str(6 + k if k < 8 else 7 + k)  # noqa: E731  (TSSOP-28: LED0-7 = 6-13, LED8-15 = 15-22)
        pins = [(led_num(k), f"LED{k}", f"PWM{k}" if k < n else None, "R", "output") for k in range(16)]
        pins += [(num, f"A{i}", "GND", "L", "input") for i, num in enumerate(("1", "2", "3", "4", "5", "24"))]
        pins += [("23", "~OE", "PWM_OE", "L", "input"), ("25", "EXTCLK", "GND", "L", "input"), ("26", "SCL", "SCL", "L", "input"),
                 ("27", "SDA", "SDA", "L", "bidir"), ("28", "VDD", "3V3", "T", "power_in"), ("14", "VSS", "GND", "B", "power_in")]
        _part(c, "U2", "PCA9685", "box", pins, "pwm", place={"at_xy": (0.56, 0.42), "rot": 90})
        _part(c, "C11", "C100n", "C", [("1", "1", "3V3"), ("2", "2", "GND")], "pwm", place={"near": "U2", "pin": "28"})
        _part(c, "R16", "R10k", "R", [("1", "1", "3V3"), ("2", "2", "PWM_OE")], "pwm", place={"near": "U2", "pin": "23"})
        _part(c, "R17", "R4k7", "R", [("1", "1", "3V3"), ("2", "2", "SDA")], "pwm", place={"near": "U2", "pin": "27"})
        _part(c, "R18", "R4k7", "R", [("1", "1", "3V3"), ("2", "2", "SCL")], "pwm", place={"near": "U2", "pin": "26"})
        hp = []
        for k, a in enumerate(acts):
            _part(c, f"R{20 + k}", "R220", "R", [("1", "1", f"PWM{k}"), ("2", "2", f"SERVO{k + 1}")], "pwm",
                  place={"near": "U2", "pin": led_num(k), "prio": 2})
            hp += [(str(3 * k + 1), f"S{k + 1}", f"SERVO{k + 1}", "R", "output"), (str(3 * k + 2), f"V{k + 1}", "V_SERVO", "R", "power_out"),
                   (str(3 * k + 3), f"G{k + 1}", "GND", "R", "power_in")]
        c.add("J3", f"SERVO 3x{n}", "conn", hp, footprint=f"HDR_3x{n}", mpn=f"2.54 mm pin header 3x{n}, vertical",
              lcsc="verify", price=0.15, block="pwm", place={"edge": "bottom", "at": 0.64},
              note="ch " + ", ".join(f"{k}:{a['joint'].replace('_', ' ')}" for k, a in enumerate(acts)))
    else:
        key = "BUS_FT" if proto == "feetech_scs" else "BUS_DXL"
        _part(c, "U2", "LVC2G241", "box", [("8", "VCC", "3V3", "T", "power_in"), ("1", "~1OE", "BUS_DIR", "L", "input"),
                                           ("7", "2OE", "BUS_DIR", "L", "input"), ("5", "2A", "UART_TX", "L", "input"),
                                           ("6", "1Y", "UART_RX", "L", "output"), ("2", "1A", "BUS_DATA", "R", "input"),
                                           ("3", "2Y", "BUS_DATA", "R", "output"), ("4", "GND", "GND", "B", "power_in")], "bus",
              place={"at_xy": (0.50, 0.30)})
        _part(c, "C11", "C100n", "C", [("1", "1", "3V3"), ("2", "2", "GND")], "bus", place={"near": "U2", "pin": "8", "prio": 1})
        _part(c, "R16", "R10k", "R", [("1", "1", "3V3"), ("2", "2", "BUS_DATA")], "bus", place={"near": "U2", "pin": "2"})
        _part(c, "R17", "R10k", "R", [("1", "1", "3V3"), ("2", "2", "UART_RX")], "bus", place={"near": "U2", "pin": "6"})
        ports = bus_ports(acts, "ttl")
        for i, chain in enumerate(ports):
            ids = [a["id"] for a in chain]
            _part(c, f"J{3 + i}", key, "conn", [("1", "GND", "GND"), ("2", "V+", "V_SERVO"), ("3", "DATA", "BUS_DATA")], "bus",
                  value=f"BUS {chr(65 + i)}",
                  place={"edge": "bottom", "at": 0.33 + 0.17 * i}, note=f"bus port {chr(65 + i)} → ID {ids[0]}" +
                  (f"-{ids[-1]}" if len(ids) > 1 else ""))
        if "imu" in extras:
            _part(c, "R18", "R4k7", "R", [("1", "1", "3V3"), ("2", "2", "SDA")], "sensors", place={"near": "J8"})
            _part(c, "R19", "R4k7", "R", [("1", "1", "3V3"), ("2", "2", "SCL")], "sensors", place={"near": "J8"})

    # ---- force sensors + expansion
    if fsr:
        rdiv = ep["grip"]["fsr_divider_ohm"]
        for j, (net, at, where) in enumerate((("FSR_L", 0.30, "left"), ("FSR_R", 0.12, "right"))):
            _part(c, f"J{6 + j}", "PH2", "conn", [("1", "1", "3V3"), ("2", "2", net)], "sensors", value=f"FSR {where[0].upper()}",
                  place={"edge": "right", "at": at}, note=f"FSR 402 on the {where} jaw")
            c.add(f"R{40 + j}", ohm_str(rdiv), "R", [("1", "1", net), ("2", "2", "GND")], footprint="R0603",
                      mpn=f"0603 1% {ohm_str(rdiv)}", lcsc="verify" if rdiv != 10000 else "C25804", price=0.002,
                      block="sensors", place={"near": f"J{6 + j}", "side": "left"})
            _part(c, f"C{40 + j}", "C100n", "C", [("1", "1", net), ("2", "2", "GND")], "sensors",
                  place={"near": f"J{6 + j}", "side": "left"})
    if "imu" in extras:
        _part(c, "J8", "SH4", "conn", [("1", "GND", "GND"), ("2", "3V3", "3V3"), ("3", "SDA", "SDA"), ("4", "SCL", "SCL")],
              "sensors", value="IMU", place={"edge": "right", "at": 0.48}, note="Qwiic / STEMMA QT → BNO085 IMU")
    for i, (fx, fy) in enumerate(((0, 0), (1, 0), (0, 1), (1, 1))):
        _part(c, f"H{i + 1}", "MH", "MH", [("1", "1", None)], "mech", place={"hole": i})
    c.notes = [f"Servo rail V_SERVO = input ({vin:g} V) switched by Q2; firmware enables it with SERVO_EN"
               + (", the e-stop loop (J9) cuts it in hardware" if "estop" in extras else ""),
               f"Input protection: {fuse_a:g} A fuse F1, {tvs} TVS D1, P-FET Q1 against reverse polarity",
               "Logic: " + ("TPS563201 buck 5 V, " if vin > 6.5 else "") + "diode-OR with USB VBUS, AMS1117 3.3 V LDO",
               "ESP32-S3 native USB on IO19/IO20; hold BOOT, tap RESET to enter the bootloader"]
    return c


# module pinouts for the wiring diagram (header pin name lists, top view)
MODULE_PINS = {
    "esp32_s3_devkit": (["3V3", "3V3", "RST", "GPIO4", "GPIO5", "GPIO6", "GPIO7", "GPIO15", "GPIO16", "GPIO17", "GPIO18",
                         "GPIO8", "GPIO3", "GPIO46", "GPIO9", "GPIO10", "GPIO11", "GPIO12", "GPIO13", "GPIO14", "5V", "GND"],
                        ["GND", "TX", "RX", "GPIO1", "GPIO2", "GPIO42", "GPIO41", "GPIO40", "GPIO39", "GPIO38", "GPIO37",
                         "GPIO36", "GPIO35", "GPIO0", "GPIO45", "GPIO48", "GPIO47", "GPIO21", "GPIO20", "GPIO19", "GND", "GND"]),
    "esp32_devkit": (["3V3", "EN", "VP", "VN", "IO34", "IO35", "IO32", "IO33", "IO25", "IO26", "IO27", "IO14", "IO12", "GND",
                      "IO13", "SD2", "SD3", "CMD", "5V"],
                     ["GND", "IO23", "IO22", "TX", "RX", "IO21", "GND", "IO19", "IO18", "IO5", "IO17", "IO16", "IO4", "IO0",
                      "IO2", "IO15", "SD1", "SD0", "CLK"]),
    "rp2040_pico": (["GP0", "GP1", "GND", "GP2", "GP3", "GP4", "GP5", "GND", "GP6", "GP7", "GP8", "GP9", "GND", "GP10", "GP11",
                     "GP12", "GP13", "GND", "GP14", "GP15"],
                    ["VBUS", "VSYS", "GND", "3V3_EN", "3V3", "ADC_VREF", "GP28", "AGND", "GP27", "GP26", "RUN", "GP22", "GND",
                     "GP21", "GP20", "GP19", "GP18", "GND", "GP17", "GP16"]),
    "arduino_nano": (["D13", "3V3", "AREF", "A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7", "5V", "RST", "GND", "VIN"],
                     ["D12", "D11", "D10", "D9", "D8", "D7", "D6", "D5", "D4", "D3", "D2", "GND", "RST", "RX0", "TX1"]),
    "openrb_150": (["AREF", "A0", "A1", "A2", "A3", "A4", "A5", "A6", "D0", "D1", "D2", "D3", "D4", "D5"],
                   ["5V", "VIN", "VCC", "GND", "RESET", "D14/TX", "D13/RX", "D12/SCL", "D11/SDA", "D10", "D9", "D8", "D7", "D6"]),
}


def _module_box(c: Circuit, ref: str, key: str, nets: dict, block: str, value=None, extra=(), note=""):
    """Box symbol for a dev-board module: every header pin, nets by pin name ("D12/SCL" matches "D12" or "SCL")."""
    left, right = MODULE_PINS[key]
    pins = []
    for side, names, hdr in (("L", left, "J1"), ("R", right, "J3" if key == "esp32_s3_devkit" else "J2")):
        for i, nm in enumerate(names):
            net = "GND" if nm in ("GND", "AGND") else next((nets[k] for k in (nm, *nm.split("/")) if k in nets), None)
            et = "power_in" if nm in ("GND", "AGND", "5V", "VIN", "VSYS", "VBUS") else "bidir"
            pins.append((f"{hdr}-{i + 1}", nm, net, side, et))
    pins += list(extra)
    return c.add(ref, value or MODULES[key]["name"].split(" (")[0], "box", pins, board=False, block=block,
                 mpn=MODULES[key]["name"], note=note, meta={"key": key})


def modules_circuit(design, ep: dict) -> Circuit:
    pwm = ep["bus"] == "pwm"
    extras = ep["extras"]
    acts = ep["actuators"]
    ctrl = ep["controller"]
    sup = ep["supply"]
    fsr = ep["grip"]["sensor"] == "fsr402"
    pm = PINMAP[ctrl]
    drv_title = {"hobby_pwm": "PWM SERVO DRIVER", "dynamixel": "DYNAMIXEL TTL BUS (OpenRB-150 ports)"}.get(
        ep["family"], "SERIAL BUS SERVO ADAPTER")
    c = Circuit("Morph robot wiring (off-the-shelf modules)",
                subtitle=f"{design.name} · {ep['family']} · {len(acts)} actuators · {sup['v']:g} V {sup['a']:g} A supply",
                blocks=[("power", "SUPPLY · FUSE · E-STOP · REGULATORS"), ("ctrl", "CONTROLLER"), ("drive", drv_title),
                        ("act", "ACTUATORS"), ("sense", "SENSORS"), ("host", "HOST PC")])
    fuse = ep["power_budget"]["fuse_a"]
    c.add("PS1", f"{sup['v']:g} V {sup['a']:g} A", "box", [("1", "+V", "VIN_RAW", "R", "power_out"),
                                                        ("2", "-V", "GND", "R", "power_out")],
          board=False, block="power", mpn=MODULES[sup["key"]]["name"], note="external supply, 5.5 mm barrel")
    c.add("F1", f"{fuse:g}A", "F", [("1", "1", "VIN_RAW"), ("2", "2", "V_FUSED")], board=False, block="power",
          mpn=f"inline ATO fuse holder + {fuse:g} A fuse")
    servo_in = "V_FUSED"
    if "estop" in extras:
        c.add("SW1", "E-STOP", "box", [("11", "NC.1", "V_FUSED", "L", "passive"), ("12", "NC.2", "V_ESTOP", "R", "passive"),
                                       ("23", "NO.1", "ESTOP", "R", "passive"), ("24", "NO.2", "GND", "R", "passive")],
              board=False, block="power", mpn="22 mm latching mushroom, 1NC + 1NO", note="NC cuts servo power, NO tells the MCU")
        servo_in = "V_ESTOP"
    if sup["servo_buck"]:
        c.add("U4", f"BUCK {sup['servo_v']:g} V", "box", [("1", "IN+", servo_in, "L", "power_in"), ("2", "IN-", "GND", "L", "power_in"),
                                                        ("3", "OUT+", "V_SERVO", "R", "power_out"), ("4", "OUT-", "GND", "R", "power_out")],
              board=False, block="power", mpn=MODULES["buck_servo"]["name"], note=f"set to {sup['servo_v']:g} V before connecting")
        servo_net = "V_SERVO"
    else:
        servo_net = servo_in
    logic5 = "5V"
    if ctrl != "openrb_150" and sup["v"] >= 6.0:
        c.add("U3", "BUCK 5V", "box", [("1", "VIN", "V_FUSED", "L", "power_in"), ("2", "GND", "GND", "L", "power_in"),
                                       ("3", "VOUT", "5V", "R", "power_out"), ("4", "EN", None, "L", "input"),
                                       ("5", "PG", None, "R", "output")],
              board=False, block="power", mpn=MODULES["buck_5v"]["name"], note="logic stays powered when the e-stop is hit")
    elif ctrl != "openrb_150":
        logic5 = "V_FUSED"

    # controller
    nets = {"GND": "GND", "3V3": "3V3"}
    if ctrl == "openrb_150":
        nets.update({"VIN": "V_FUSED", "VCC": "3V3"})
        if fsr:
            nets.update({pm["FSR_L"]: "FSR_L", pm["FSR_R"]: "FSR_R"})
        if "imu" in extras:
            nets.update({"SDA": "SDA", "SCL": "SCL"})
        if "estop" in extras:
            nets.update({pm["ESTOP"]: "ESTOP"})
        extra = [("DXL-G", "DXL GND", "GND", "T", "power_out"), ("DXL-V", "DXL VDD", servo_net, "T", "power_out"),
                 ("DXL-D", "DXL DATA", "BUS_DATA", "T", "bidir"), ("USB", "USB", "USB_CTRL", "T", "bidir")]
        _module_box(c, "U1", ctrl, nets, "ctrl", value="OpenRB-150", extra=extra,
                    note="DYNAMIXEL power = VIN terminal (switched by the e-stop)" if "estop" in extras else "")
        # the e-stop must cut the DXL power: VIN terminal carries servo power on the OpenRB-150
        if "estop" in extras:
            c.comp("U1").pin("VIN").net = servo_in
            c.comp("U1").pin("DXL-V").net = servo_in
    else:
        nets[pm["5V"]] = logic5
        if pwm:
            nets.update({pm["SDA"]: "SDA", pm["SCL"]: "SCL", pm["PWM_OE"]: "PWM_OE"})
        else:
            nets.update({pm["BUS_TX"]: "UART_TX", pm["BUS_RX"]: "UART_RX"})
            if "imu" in extras:
                nets.update({pm["SDA"]: "SDA", pm["SCL"]: "SCL"})
        if fsr:
            nets.update({pm["FSR_L"]: "FSR_L", pm["FSR_R"]: "FSR_R"})
        if "estop" in extras:
            nets[pm["ESTOP"]] = "ESTOP"
        _module_box(c, "U1", ctrl, nets, "ctrl", extra=[("USB", "USB", "USB_CTRL", "T", "bidir")])
    # driver
    if pwm:
        n = len(acts)
        pins = [("1", "GND", "GND", "L", "power_in"), ("2", "OE", "PWM_OE", "L", "input"), ("3", "SCL", "SCL", "L", "input"),
                ("4", "SDA", "SDA", "L", "bidir"), ("5", "VCC", "3V3", "L", "power_in"), ("6", "V+", servo_net, "L", "power_in"),
                ("T1", "V+ (term)", servo_net, "L", "power_in"), ("T2", "GND (term)", "GND", "L", "power_in")]
        pins += [(f"P{k}", f"PWM{k}", f"PWM{k}" if k < n else None, "R", "output") for k in range(16)]
        c.add("U2", "PCA9685 16-ch", "box", pins, board=False, block="drive", mpn=MODULES["pca9685_board"]["name"],
              note="I2C 0x40 · 50 Hz · V+ terminal feeds the servo headers")
        c.add("C1", "470u", "CP", [("1", "+", servo_net), ("2", "-", "GND")], board=False, block="drive",
              mpn="470 uF 16 V electrolytic (into the PCA9685 board's C2 footprint)")
    elif ctrl != "openrb_150":
        c.add("U2", "BUS ADAPTER", "box", [("1", "VIN+", servo_net, "L", "power_in"), ("2", "VIN-", "GND", "L", "power_in"),
                                           ("3", "RXD", "UART_TX", "L", "input"), ("4", "TXD", "UART_RX", "L", "output"),
                                           ("5", "GND", "GND", "L", "power_in"), ("6", "BUS-G", "GND", "R", "power_out"),
                                           ("7", "BUS-V", servo_net, "R", "power_out"), ("8", "BUS-D", "BUS_DATA", "R", "bidir")],
              board=False, block="drive", mpn=MODULES["bus_servo_adapter"]["name"],
              note="jumper in UART (ESP32) mode; verify the header labels on your revision")
    # actuators
    for k, a in enumerate(acts):
        s = ACTUATORS[a["key"]]
        jn = a["joint"].replace("_", " ")
        if pwm:
            c.add(f"M{k + 1}", a["key"].upper(), "box", [("1", "SIG", f"PWM{k}", "L", "input"), ("2", "V+", servo_net, "L", "power_in"),
                                                        ("3", "GND", "GND", "L", "power_in")],
                  board=False, block="act", mpn=s["name"], note=f"{jn} · ch {a['channel']}")
        else:
            c.add(f"M{k + 1}", a["key"].upper(), "box", [("1", "GND", "GND", "L", "power_in"), ("2", "V+", servo_net, "L", "power_in"),
                                                        ("3", "DATA", "BUS_DATA", "L", "bidir")],
                  board=False, block="act", mpn=s["name"], note=f"{jn} · ID {a['id']}")
    # sensors
    if fsr:
        rdiv = ohm_str(ep["grip"]["fsr_divider_ohm"])
        vref = "3V3" if LOGIC_V.get(ctrl, 3.3) < 4 else "5V"
        for j, (net, where) in enumerate((("FSR_L", "left jaw"), ("FSR_R", "right jaw"))):
            c.add(f"S{j + 1}", "FSR 402", "box", [("1", "1", vref, "R", "passive"), ("2", "2", net, "R", "passive")], board=False,
                  block="sense", mpn=MODULES["fsr402"]["name"], note=where)
            c.add(f"R{j + 1}", rdiv, "R", [("1", "1", net), ("2", "2", "GND")], board=False, block="sense",
                  mpn=f"{rdiv} 1% divider (perfboard / inline)")
    if "imu" in extras:
        c.add("IMU1", "BNO085", "box", [("1", "VIN", "3V3", "L", "power_in"), ("2", "GND", "GND", "L", "power_in"),
                                        ("3", "SDA", "SDA", "L", "bidir"), ("4", "SCL", "SCL", "L", "input")],
              board=False, block="sense", mpn=MODULES["imu"]["name"], note="STEMMA QT · 0x4A · wrist")
    host = [("1", "USB1", "USB_CTRL", "L", "bidir")]
    if "wrist_camera" in extras:
        c.add("CAM1", "USB CAMERA", "box", [("1", "USB", "USB_CAM", "R", "bidir")], board=False, block="host",
              mpn=MODULES["wrist_camera"]["name"], note="UVC 1080p at the wrist")
        host.append(("2", "USB2", "USB_CAM", "L", "bidir"))
    c.add("H1", "HOST PC", "box", host, board=False, block="host", mpn="Host computer (LeRobot / ROS / Studio)",
          note="serial commands + camera stream")
    c.notes = [f"All grounds common at the supply; fuse {fuse:g} A on the supply lead",
               "Servo power and logic power are separate branches: the e-stop (if fitted) only opens the servo branch"]
    return c


# ------------------------------------------------------------------ harness

STOCK_MM = [100, 150, 200, 250, 300, 400, 500, 750, 1000]
BUS_CABLE_VENDOR = {
    "feetech_scs": lambda L: [V("aliexpress", f"Feetech servo cable 5264 3P {L}mm", 0.6, lead_days=14),
                              V("seeed", "STS3215 servo cable", 1.2)],
    "dynamixel_2": lambda L: [V("robotis", f"Robot Cable-X3P {L}mm", 1.8 if L <= 180 else 2.4,
                                 "https://www.robotis.us/robot-cable-x3p-180mm-10pcs/", note="10-pack price / 10")],
}


def _stock(L):
    return next((s for s in STOCK_MM if s >= L), None)


def _joint_alias(name, joints):
    if name in joints:
        return name
    return next((j for j in joints if j.endswith(name) or name.endswith(j.split("_")[-1])), name)


def _route_len(asm, la, pa, lb, pb):
    """Cable length between two points on (possibly) different links, routed through the joint origins between them."""
    from .assembly import Cable
    def up(link):
        out = [link]
        while link != asm.root:
            j = asm.parent_joint(link)
            if j is None:
                break
            link = j.parent
            out.append(link)
        return out
    A, B = up(la), up(lb)
    common = next((l for l in A if l in B), asm.root)
    pts = [(la, tuple(pa))]
    for l in A[:A.index(common)]:
        j = asm.parent_joint(l)
        pts.append((j.parent, tuple(j.origin[:3, 3])))
    tail = []
    for l in B[:B.index(common)]:
        j = asm.parent_joint(l)
        tail.append((j.parent, tuple(j.origin[:3, 3])))
    pts += tail[::-1] + [(lb, tuple(pb))]
    return asm.cable_length(Cable("tmp", pts))


def _anchor(asm, joint):
    """(link, point) of the actuator driving `joint` (part meta 'drives', stub naming), else the joint origin."""
    parts = [p for p in asm.parts if p.kind == "actuator" and (p.meta.get("drives") == joint or
             p.name.startswith(f"act_{joint.split('_')[0]}_"))]
    if parts:
        p = max(parts, key=lambda q: len(q.mesh.faces))
        return p.link, p.mesh.bounds.mean(axis=0)
    try:
        j = asm.joint(joint)
        return j.parent, j.origin[:3, 3]
    except StopIteration:
        return asm.root, np.zeros(3)


def build_harness(design, ep: dict, asm, pb: dict) -> tuple[list, list]:
    proto, bus, cfg = ep["protocol"], ep["bus"], ep["config"]
    acts = ep["actuators"]
    warns = []
    rows = []
    joints = [j.name for j in asm.joints] if asm is not None else []
    grip = ep["grip"]
    cur = {a["id"] if bus != "pwm" else a["channel"]: _actuator_currents(a, design, grip) for a in acts}
    servo_v = ep["supply"]["servo_v"]
    conn = BUS_CONNECTOR[proto]
    cables = list(asm.cables) if asm is not None else []
    by_kind: dict = {}
    for c in cables:
        by_kind.setdefault((c.meta or {}).get("kind", ""), []).append(c)
    ports = bus_ports(acts, "ttl") if bus != "pwm" else []
    {a["id"]: chr(65 + i) for i, ch in enumerate(ports) for a in ch}

    def L(c):
        return round(asm.cable_length(c), 0)

    def wire_for(peak, avg, length_mm, min_awg=24, cond=3):
        g = min(pick_awg(avg * 1.25), min_awg)
        while True:
            drop = peak * 2 * length_mm / 1000.0 * awg_ohm_m(g)
            if drop <= 0.05 * servo_v or g <= 20:
                return g, drop
            g -= 2

    n = 0

    def add(name, frm, to, length, cond, wire, ca, cb, kind, current=None, mob="make", stock=None, note=""):
        nonlocal n
        n += 1
        rows.append({"name": f"W{n} {name}", "from": frm, "to": to, "length_mm": int(round(length)), "conductors": cond,
                     "wire": wire, "connector_a": ca, "connector_b": cb, "kind": kind,
                     "current_a": round(current, 2) if current is not None else None, "make_or_buy": mob,
                     "stock_mm": stock, "note": note})

    ctrl_ref = {"custom_pcb": "PCBA"}.get(cfg, "U2" if ep["controller"] != "openrb_150" else "U1")
    # ---- servo bus / PWM channels
    if bus != "pwm":
        chain = sorted(by_kind.get("servo_bus", []), key=lambda c: (c.meta or {}).get("servo_id", 0))
        if not chain:       # stub arm: synthesise one cable per hop from actuator anchors
            prev = (asm.root, np.array([-40.0, -45.0, 20.0])) if asm is not None else None
            for a in acts:
                la, pa = _anchor(asm, a["joint"]) if asm is not None else (None, None)
                length = _route_len(asm, prev[0], prev[1], la, pa) if asm is not None else 200.0
                chain.append(type("C", (), {"meta": {"servo_id": a["id"], "to_joint": a["joint"]}, "_len": length,
                                            "src": "", "dst": ""})())
                prev = (la, pa)
        ids = [a["id"] for a in acts]
        for c in chain:
            sid = (c.meta or {}).get("servo_id")
            length = c._len if hasattr(c, "_len") else L(c)
            down = [cur[i] for i in ids if i >= (sid or 0)]
            peak, avg = sum(d[0] for d in down), sum(d[1] for d in down)
            g, drop = wire_for(peak, avg, length)
            first = sid == min(ids)
            if cfg == "custom_pcb":
                frm = f"J{3 + 0} BUS A" if first else f"ID{sid - 1}"
            else:
                frm = f"{ctrl_ref} bus port" if first else f"ID{sid - 1}"
            jn = _joint_alias((c.meta or {}).get("to_joint", ""), joints)
            st = _stock(length)
            buy = st is not None and st <= 500
            add(f"bus → ID{sid} {jn.replace('_', ' ')}", frm, f"ID{sid} {jn}", length, 3, f"{g} AWG silicone, GND/V+/DATA",
                conn, conn, "servo_bus", peak, "buy" if buy else "make", st,
                f"carries {len(down)} servo(s): {avg:.1f} A avg / {peak:.1f} A peak, {drop:.2f} V drop at peak")
            if avg > CONNECTOR_A.get(conn, 3.0):
                warns.append(f"bus hop to ID{sid} carries {avg:.1f} A average through a {conn} (rated "
                             f"{CONNECTOR_A.get(conn, 3.0):g} A): feed power at the shoulder or split the chain")
    else:
        pw = sorted(by_kind.get("pwm", []), key=lambda c: (c.meta or {}).get("channel", 0))
        if not pw and asm is not None:
            for a in acts:
                la, pa = _anchor(asm, a["joint"])
                pw.append(type("C", (), {"meta": {"channel": a["channel"], "to_joint": a["joint"]},
                                          "_len": _route_len(asm, asm.root, np.array([-40.0, -45.0, 20.0]), la, pa)})())
        for c in pw:
            ch = (c.meta or {}).get("channel", 0)
            length = c._len if hasattr(c, "_len") else L(c)
            peak, avg, _ = cur.get(ch, (1.0, 0.3, 1.0))
            g, drop = wire_for(peak, avg, length, 22)
            jn = _joint_alias((c.meta or {}).get("to_joint", ""), joints)
            lead = 300 if ACTUATORS[acts[min(ch, len(acts) - 1)]["key"]]["mass_g"] > 30 else 250
            ext = max(0, length - lead)
            add(f"PWM ch{ch} → {jn.replace('_', ' ')}", "J3 SERVO" if cfg == "custom_pcb" else f"U2 PWM{ch}",
                f"{jn} servo", length, 3, f"{g} AWG servo extension (signal/V+/GND)", "JR/Dupont 3p (F)", "servo lead (JR 3p M)",
                "pwm", peak, "buy", _stock(ext) if ext else None,
                f"servo's own ~{lead} mm lead + {ext:.0f} mm extension" if ext else "servo lead reaches directly")
    # ---- force sensors
    fsr_from = {"S1": "J6 FSR L", "S2": "J7 FSR R"} if cfg == "custom_pcb" else {"S1": "U1 A/IO + R1", "S2": "U1 A/IO + R2"}
    sens = {s["ref"]: s for s in ep["sensors"]}
    for side, ref in (("left", "S1"), ("right", "S2")):
        if ref not in sens:
            continue
        c = next((c for c in by_kind.get("sensor", []) if side in c.name), None)
        if c is not None:
            length = L(c)
        elif asm is not None and f"jaw_{side}" in asm.links:
            length = _route_len(asm, f"jaw_{side}", np.zeros(3), asm.root, np.array([-40.0, -45.0, 20.0]))
        else:
            length = 800.0
        add(f"FSR {side} jaw", fsr_from[ref], f"{ref} FSR 402 ({side} jaw)", length, 2, "28 AWG twisted pair, shielded",
            "JST-PH 2p" if cfg == "custom_pcb" else "Dupont 2p (F)", "Amphenol Clincher 2p (FSR tail)", "sensor", 0.001,
            "make", None, "route along the arm's cable channel with the bus; leave a service loop at every joint")
    if "imu" in ep["extras"]:
        c = (by_kind.get("i2c") or [None])[0]
        length = L(c) if c is not None else 750.0
        st = _stock(length)
        add("IMU (I2C, Qwiic)", "J8 IMU" if cfg == "custom_pcb" else "U1 SDA/SCL/3V3/GND", "IMU1 BNO085 (wrist)", length, 4,
            "28 AWG 4-core (Qwiic colours)", "JST-SH 4p (Qwiic)" if cfg == "custom_pcb" else "Dupont 4p (F)", "JST-SH 4p (Qwiic)",
            "i2c", 0.02, "buy" if st and st <= 500 and cfg == "custom_pcb" else "make", st, "keep I2C at 100 kHz over this length")
    if "wrist_camera" in ep["extras"]:
        c = next((c for c in by_kind.get("usb", []) if "camera" in c.name), None)
        length = (L(c) if c is not None else 750.0) + 1500.0
        add("wrist camera USB → host", "CAM1 (wrist)", "host PC USB-A", length, 4, "USB 2.0 high-flex (2x28 + 2x24 AWG)",
            "camera USB header / micro-B", "USB-A", "usb", 0.25, "buy", None, "camera streams straight to the host, not through the controller")
    # ---- power, host link, e-stop
    feed = pb["feed_awg"]
    c = (by_kind.get("power") or [None])[0]
    length = L(c) if c is not None else 140.0
    if cfg == "custom_pcb":
        add("power in", "J_PWR panel jack 5.5x2.5", "J1 XT30 (PCBA)", length, 2, f"{feed} AWG silicone red/black",
            "5.5x2.5 mm panel jack", "XT30 (F)", "power", pb["input_peak_a"], "make")
    else:
        add("power in", "J_PWR panel jack 5.5x2.5", "F1 inline fuse", length, 2, f"{feed} AWG silicone red/black",
            "5.5x2.5 mm panel jack", "ATO fuse holder leads", "power", pb["input_peak_a"], "make")
        dest = "U4 buck (servo)" if ep["supply"]["servo_buck"] else {"hobby_pwm": "U2 PCA9685 V+ terminal", "dynamixel": "U1 OpenRB VIN"}.get(
            ep["family"], "U2 adapter DC in")
        add("servo power", "SW1 e-stop NC" if "estop" in ep["extras"] else "F1 fuse", dest, 120, 2, f"{feed} AWG silicone",
            "spade / screw", "screw terminal", "power", pb["rails"][0]["peak_a"], "make")
        if ep["controller"] != "openrb_150" and ep["supply"]["v"] >= 6.0:
            add("logic 5 V", "U3 buck 5V VOUT", "U1 5V pin", 100, 2, "22 AWG", "Dupont 2p (F)", "Dupont 2p (F)", "power",
                pb["rails"][1]["peak_a"], "make")
        if ep["bus"] == "pwm":
            add("I2C + OE", "U1 GPIO8/9/7, 3V3, GND", "U2 PCA9685 header", 100, 5, "26 AWG jumper", "Dupont 5p (F)", "Dupont 6p (F)",
                "signal", 0.01, "buy")
        elif ep["controller"] != "openrb_150":
            add("UART", "U1 GPIO17/18, GND", "U2 adapter UART header", 100, 3, "26 AWG jumper", "Dupont 3p (F)", "Dupont 3p (F)",
                "signal", 0.01, "buy")
    c = next((c for c in by_kind.get("usb", []) if "host" in c.name), None)
    add("USB host link", "J_USB panel USB-C", "USB-C (controller)", L(c) if c is not None else 140.0, 4, "USB-C extension",
        "USB-C panel mount (F)", "USB-C (M)", "usb", 0.5, "buy", None, "then a USB-C cable to the host PC")
    if "estop" in ep["extras"]:
        c = (by_kind.get("safety") or [None])[0]
        length = L(c) if c is not None else 150.0
        if cfg == "custom_pcb":
            add("e-stop loop", "J9 E-STOP", "SW1 NC contact (11-12)", length, 2, "24 AWG", "JST-XH 2p", "6.3 mm spade",
                "safety", 0.002, "make", None, "low-current gate loop: opening it turns the servo rail FET off in hardware")
        else:
            add("e-stop", "SW1 NC (11-12) / NO (23-24)", "F1 / U1 ESTOP pin", length, 4, f"2x {feed} AWG + 2x 24 AWG",
                "6.3 mm spades", "screw terminal / Dupont", "safety", pb["rails"][0]["peak_a"], "make", None,
                "NC contact carries the servo current; NO contact tells the firmware")
    return rows, warns


# ------------------------------------------------------------------ firmware

FW_PROTOCOL = """//   J q1 .. qN       joint targets in radians (order below), clamped to the joint limits
//   M i q            move joint i (0..N-1) to q radians
//   V s              speed scale 0.05 .. 1.0
//   W {grip_unit:<13}gripper target in {grip_unit} ({grip_origin})
//   G                {grip_g}
//   O                open the gripper
//   F n              {grip_f}
//   H | R            move to the home | ready pose
//   E | C            software e-stop (torque off) | clear the e-stop
//   S1 | S0          start | stop 20 Hz telemetry: T,ms,state,q1..qN,grip_{grip_unit},force_n
//   Q                print one status line
//   Z grams          {grip_z}"""


def _grip_commands(fsr: bool, torque: bool, probe_end: str = "GRIP_MIN", give_up: str = "GRIP_CLOSED") -> dict:
    """What G, F and Z do in this firmware, for its header."""
    if fsr:
        return {"grip_g": "grip: close until the fingertip force reaches the target, then hold it (an object narrower "
                          f"than the gauge too, as far as {probe_end}; with no force reading by then, "
                          + ("open to GRIP_MAX)" if give_up == "GRIP_MAX" else "back to GRIP_CLOSED)"),
                "grip_f": "grip force target in newtons (capped at GRIP_LIMIT_N)",
                "grip_z": "calibrate the force sensors with a known mass resting on the left pad"}
    if torque:
        return {"grip_g": "grip: close past the object towards GRIP_MIN; the gripper servo's torque limit sets the squeeze "
                          "(no force sensors)",
                "grip_f": "grip force in newtons: sets the gripper servo's torque limit (capped at GRIP_LIMIT_N)",
                "grip_z": "calibrate force sensors (none are fitted: replies with the unchanged factor)"}
    return {"grip_g": "grip: close GRIP_SQUEEZE past the object, where the hobby servo's own position loop squeezes about the "
                      "target (no force sensors and no torque limit: never on to GRIP_MIN, where it would sit at stall)",
            "grip_f": "grip force in newtons: sets how far past the object G closes (GRIP_SQUEEZE_PER_N per N, capped at "
                      "GRIP_LIMIT_N)",
            "grip_z": "calibrate force sensors (none are fitted: replies with the unchanged factor)"}


def _c(v, nd=4):
    return f"{v:.{nd}f}f"


def _window_text(grip: dict) -> str:
    lo, hi = grip["window_n"]
    return f"window {lo:g} - {hi:g} N" if hi is not None else f"at least {lo:g} N (no damage force)"


def grip_motion(design, ep: dict, asm) -> dict:
    """The gripper joint in its own units: mm for a rack-and-pinion jaw (the pinion radius converts to servo
    turns), radians for a revolute jaw such as the SO-101's (no pinion: the servo turns the jaw directly).
    Limits are the joint's, clipped to what the servo can reach; speeds never exceed the joint's own."""
    name = (asm.meta.get("gripper_joint") or "gripper") if asm is not None else "gripper"
    joint = next((j for j in asm.joints if j.name == name), None) if asm is not None else None
    kind = joint.type if joint is not None else "prismatic"
    if kind not in ("prismatic", "revolute"):
        raise ValueError(f"unsupported gripper joint type: {kind}")
    actuator = next(a for a in ep["actuators"] if a["joint"] == "gripper")
    pwm = ep["bus"] == "pwm"
    per_rad = 2000.0 / math.radians(ACTUATORS[actuator["key"]]["range_deg"]) if pwm else 4096 / (2 * math.pi)
    radius = design.electronics.pinion_radius_mm
    if kind == "prismatic" and radius <= 0:
        raise ValueError("a rack-and-pinion gripper needs a positive pinion radius")
    per_unit = per_rad if kind == "revolute" else per_rad / radius
    zero = 1500.0 if pwm else 2048.0
    servo_lo, servo_hi = (500.0, 2500.0) if pwm else (0.0, 4095.0)
    lo, hi = (joint.lower, joint.upper) if joint is not None else (-3.0, 14.0)
    lo, hi = max(lo, (servo_lo - zero) / per_unit), min(hi, (servo_hi - zero) / per_unit)
    if not all(math.isfinite(v) for v in (lo, hi, per_unit)) or lo >= hi:
        raise ValueError("the gripper joint limits are outside the servo's position range")
    scale = 0.02 if kind == "revolute" else 1.0  # rad vs mm: conservative angular rates until calibrated on a bench
    vmax = joint.velocity if joint is not None else 25.0
    if not math.isfinite(vmax) or vmax <= 0:
        raise ValueError("the gripper joint needs a positive velocity limit")
    closed = max(lo, min(0.0, hi))
    over = max(closed - lo, 0.0)                 # the whole overtravel: GRIP_CLOSED to the max-squeeze stop
    ol = (ep.get("grip") or {}).get("open_loop") or {}
    per_mm = 1.0 / max(float((ep.get("grip") or {}).get("lever_mm") or 1.0), 1e-6) if kind == "revolute" else 1.0
    squeeze = min(ol["squeeze_mm"] * per_mm, over) if ol.get("squeeze_mm") else over
    return {"kind": kind, "unit": "rad" if kind == "revolute" else "mm", "lo": lo, "hi": hi, "zero": zero,
            "per_unit": per_unit, "closed": closed, "close_speed": min(8.0 * scale, vmax),
            "open_speed": min(25.0 * scale, vmax), "backoff": scale, "force_gain": 10.0 * scale,
            # a force-sensing close looks for a reading all the way to the max-squeeze stop: an object narrower than the
            # calibration gauge (an egg at 92 % of its nominal size) meets the jaws past GRIP_CLOSED. A hobby servo, with
            # no torque limit to cap it meanwhile, only as far as open_loop_squeeze's probe
            "probe": min(ol["probe_mm"] * per_mm, over) if ol.get("probe_mm") else over,
            # how far past GRIP_CLOSED a close without force sensors goes: to the stop where a torque limit sets the
            # squeeze; a hobby servo only as far as its own position loop squeezes the target (open_loop_squeeze)
            "squeeze": squeeze,
            "squeeze_per_n": (ol["servo_band_mm"] * per_mm / ep["grip"]["stall_force_n"] if ol.get("squeeze_mm") else 0.0),
            "squeeze_least": (min(2.0 * ol["step_mm"] * per_mm, over) if ol.get("squeeze_mm") else 0.0)}


def rail_problems(acts: list, supply: dict, config: str) -> list:
    """One line per servo whose voltage range leaves out what its rail really gets: the servo rail, or on the custom PCB
    (which passes its input straight to the servos) the supply's own voltage."""
    volts = supply["v"] if config == "custom_pcb" else supply.get("servo_v", supply["v"])
    out = {}
    for a in acts:
        lo, hi = ACTUATORS[a["key"]]["voltage"]
        if not lo - 1e-6 <= volts <= hi + 1e-6:
            out.setdefault(a["key"], (lo, hi, []))[2].append(a["joint"])
    return [f"{', '.join(dict.fromkeys(j))}: {k} takes {lo:g}-{hi:g} V but its servo rail is {volts:g} V; no wiring or "
            "firmware is made for it" for k, (lo, hi, j) in out.items()]


def _one_rail(ep: dict):
    """ValueError when a servo's rail is outside its voltage range (XL430s and XL330s on one rail, a custom PCB passing
    12 V to 7.4 V servos): its harness and firmware would run servos off their rating, or not at all."""
    problems = rail_problems(ep["actuators"], ep["supply"], ep["config"])
    if problems:
        raise ValueError(f"no wiring or firmware for a servo rail outside a servo's voltage range ({'; '.join(p.split(';')[0] for p in problems)})")


def _one_protocol(ep: dict):
    """ValueError when the plan puts actuators of another bus or protocol on this bus: its firmware would send
    them commands they don't understand (Feetech packets to a DYNAMIXEL, or the reverse)."""
    problems = [p.split(";")[0] for p in bus_problems(ep["actuators"], ep["family"])]
    if problems:
        raise ValueError(f"no wiring or firmware for a servo bus that mixes protocols ({'; '.join(problems)})")


def firmware(design, ep: dict, asm, outdir: str, rev: str = "") -> str:
    _one_protocol(ep)
    _one_rail(ep)
    fam, proto, cfg, ctrl = ep["family"], ep["protocol"], ep["config"], ep["controller"]
    pins = ARDUINO_PIN[ctrl]
    acts = ep["actuators"]
    grip = ep["grip"]
    arm_joints = [a["joint"] for a in acts if a["joint"] != "gripper"]
    names = list(dict.fromkeys(arm_joints))
    jmap = {j.name: j for j in asm.joints} if asm is not None else {}
    home = (asm.meta.get("home") if asm is not None else None) or {}
    ready = (asm.meta.get("ready") if asm is not None else None) or {}
    bus = ep["bus"]
    jt = []
    for n_ in names:
        j = jmap.get(n_)
        key = next(a["key"] for a in acts if a["joint"] == n_)
        rng = math.radians(ACTUATORS[key]["range_deg"]) / 2 * 0.98
        lo, hi = (max(j.lower, -rng), min(j.upper, rng)) if j else (-rng, rng)
        vmax = ACTUATORS[key]["speed_rad_s"] * 0.6
        jt.append((n_, lo, hi, float(home.get(n_, 0.0)), float(ready.get(n_, home.get(n_, 0.0))), vmax))
    servo_rows = []
    for a in acts:
        if a["joint"] == "gripper":
            continue
        k = names.index(a["joint"])
        if bus == "pwm":
            scale = 2000.0 / math.radians(ACTUATORS[a["key"]]["range_deg"])
            servo_rows.append((a["channel"], k, -1 if a.get("mirror") else 1, scale, 1500.0))
        else:
            servo_rows.append((a["id"], k, -1 if a.get("mirror") else 1, 4096 / (2 * math.pi), 2048.0))
    ga = next(a for a in acts if a["joint"] == "gripper")
    motion = grip_motion(design, ep, asm)
    gu = motion["unit"]
    fsr = grip["sensor"] == "fsr402"
    tl = grip.get("torque_limit")
    ol = grip.get("open_loop") or {}
    from ..electronics import PROBE_STALL_SHARE

    under = (f"under {PROBE_STALL_SHARE * 100:.0f} % of the stall"
             + (" and its damage force" if design.object.max_force_n else ""))   # a hobby open loop's size band
    # a hobby servo's force-sensing close stops short of GRIP_MIN (grip_motion's probe): say where
    probe_end = "GRIP_MIN" if motion["probe"] >= motion["closed"] - motion["lo"] - 1e-9 else "GRIP_PROBE past GRIP_CLOSED"
    # where a close with no force reading by then goes: back to GRIP_CLOSED, where a torque limit caps any squeeze on an
    # object larger than the gauge; a hobby servo, with none, opens, so it never holds one there with no reading
    give_up = "GRIP_MAX" if ol.get("probe_mm") and probe_end != "GRIP_MIN" else "GRIP_CLOSED"
    adc_bits = ADC_BITS[ctrl]
    vref = LOGIC_V[ctrl]
    wifi = "wifi" in ep["extras"] and ctrl in WIFI_MCUS
    estop_mode = "none"
    if cfg == "custom_pcb":
        estop_mode = "vsense"
    elif "estop" in ep["extras"]:
        estop_mode = "pin"
    ctrl_name = {"esp32_s3_wroom": "Morph controller PCBA (ESP32-S3-WROOM-1)"}.get(ctrl, MODULES.get(ctrl, {}).get("name", ctrl))
    libs = {"pca9685_pwm": "Adafruit PWM Servo Driver Library (Adafruit_PWMServoDriver)",
            "feetech_scs": "SCServo (Feetech, SMS_STS class)", "dynamixel_2": "Dynamixel2Arduino (ROBOTIS)"}[proto]
    rd = grip.get("fsr_divider_ohm") or 10000
    jaw = "revolute-jaw" if motion["kind"] == "revolute" else "parallel"
    hdr = [f"// morph_arm.ino - generated by Morph for {design.name}" + (f" ({rev})" if rev else ""),
           f"// {len(names)}-DOF arm + {'force-sensing ' if fsr else ''}{jaw} gripper | {fam} | {len(acts)} actuators | {ep['supply']['v']:g} V supply",
           f"// Controller: {ctrl_name} | bus: {proto} | library: {libs}",
           "// UNVERIFIED: compile it for your board and calibrate on a bench before it drives hardware.",
           "// Do not edit by hand: change the design in Morph Studio and regenerate.",
           "//", "// Serial protocol (USB, 115200 baud, one command per line):",
           FW_PROTOCOL.format(grip_unit=gu, grip_origin="0 = the jaw's calibrated zero" if motion["kind"] == "revolute"
                              else "0 = jaws touching the object", **_grip_commands(fsr, bool(tl), probe_end, give_up)), ""]
    L = hdr
    L.append("#include <Arduino.h>")
    if proto == "pca9685_pwm":
        L += ["#include <Wire.h>", "#include <Adafruit_PWMServoDriver.h>"]
    elif proto == "feetech_scs":
        L += ["#include <SCServo.h>"]
        if cfg == "custom_pcb":
            L += ["#if defined(ARDUINO_ARCH_ESP32)", "#include \"driver/uart.h\"", "#endif"]
    else:
        L += ["#include <Dynamixel2Arduino.h>"]
    if wifi:
        L += ["#include <WiFi.h>"]
    if "imu" in ep["extras"] and proto != "pca9685_pwm":
        L += ["#include <Wire.h>"]
    L += ["", "// ---------------------------------------------------------------- hardware (matches schematic.svg)"]
    pdef = lambda name, sig: L.append(f"#define {name:<16} {pins.get(sig, '-1'):<6}")  # noqa: E731
    if bus != "pwm":
        L.append(f"#define BUS_SERIAL       {'Serial1' if ctrl in ('esp32_s3_wroom', 'esp32_s3_devkit', 'openrb_150') else 'Serial2' if ctrl in ('esp32_devkit', 'rp2040_pico') else 'Serial'}")
        L.append(f"#define BUS_BAUD         {ep['baud']}")
        pdef("PIN_BUS_TX", "BUS_TX")
        pdef("PIN_BUS_RX", "BUS_RX")
        L.append(f"#define PIN_BUS_DIR      {pins['BUS_DIR'] if cfg == 'custom_pcb' else '-1'}       // 74LVC2G241 direction, HIGH = transmit"
                 if cfg == "custom_pcb" else "#define PIN_BUS_DIR      -1       // the adapter / OpenRB switches direction itself")
    else:
        pdef("PIN_SDA", "SDA")
        pdef("PIN_SCL", "SCL")
        pdef("PIN_PWM_OE", "PWM_OE")
        L.append("#define PCA9685_ADDR     0x40")
    if "imu" in ep["extras"] and bus != "pwm":
        pdef("PIN_SDA", "SDA")
        pdef("PIN_SCL", "SCL")
    L.append(f"#define PIN_FSR_L        {pins['FSR_L'] if fsr else '-1'}")
    L.append(f"#define PIN_FSR_R        {pins['FSR_R'] if fsr else '-1'}")
    L.append(f"#define PIN_ESTOP        {pins['ESTOP'] if estop_mode == 'pin' else '-1'}       // e-stop NO contact to GND (LOW = pressed)")
    L.append(f"#define PIN_VSERVO_SENSE {pins['VSERVO_SENSE'] if estop_mode == 'vsense' else '-1'}       // servo rail through 100k/22k")
    L.append(f"#define PIN_SERVO_EN     {pins['SERVO_EN'] if cfg == 'custom_pcb' else '-1'}       // HIGH turns the servo rail FET on")
    L.append(f"#define PIN_LED          {pins.get('LED', '-1')}")
    L.append(f"#define ADC_MAX          {2 ** adc_bits - 1}.0f")
    L.append(f"#define ADC_VREF         {vref:.2f}f")
    L.append(f"#define VSERVO_NOMINAL   {ep['supply']['servo_v']:.2f}f")
    L.append(f"#define VSERVO_DIVIDER   {(100 + 22) / 22:.4f}f   // (100k + 22k) / 22k")
    L.append(f"#define MORPH_WIFI       {1 if wifi else 0}")
    L += ["", "// ---------------------------------------------------------------- robot model (from the generated assembly)",
          f"#define N_JOINTS         {len(names)}",
          f"#define N_SERVOS         {len(servo_rows)}",
          "struct JointCfg { const char *name; float lo, hi, home, ready, vmax; };   // rad, rad/s",
          "const JointCfg JOINT[N_JOINTS] = {"]
    for (n_, lo, hi, h, r, vm) in jt:
        L.append(f"  {{\"{n_}\", {_c(lo)}, {_c(hi)}, {_c(h)}, {_c(r)}, {_c(vm, 2)}}},")
    L.append("};")
    unit = "us" if bus == "pwm" else "ticks"
    L += [f"struct ServoCfg {{ uint8_t {'ch' if bus == 'pwm' else 'id'}; uint8_t joint; int8_t dir; float per_rad; float zero; }};   // {unit}",
          "// dir -1 = the second servo of a dual (back-to-back) joint; zero = position at q = 0 (calibrate on the bench)",
          "const ServoCfg SERVO[N_SERVOS] = {"]
    for (i, k, d, sc, z) in servo_rows:
        L.append(f"  {{{i}, {k}, {d}, {_c(sc, 2)}, {_c(z, 1)}}},   // {names[k]}")
    L.append("};")
    L += ["", ("// gripper: revolute jaw driven directly by its servo, angle in rad from the jaw's zero" if motion["kind"] == "revolute"
               else f"// gripper: rack-and-pinion ({design.electronics.pinion_radius_mm:g} mm pinion), opening in mm beyond contact with the object"),
          f"#define GRIP_{'CH' if bus == 'pwm' else 'ID'}          {ga['channel'] if bus == 'pwm' else ga['id']}",
          f"const float GRIP_PER_UNIT   = {_c(motion['per_unit'], 6)};   // {unit} per {gu}",
          f"float GRIP_ZERO             = {_c(motion['zero'], 1)};   // {unit} at {gu} = 0 (calibrate)",
          f"const float GRIP_MIN        = {_c(motion['lo'], 6)};   // {gu}: max squeeze (joint and servo limit); G "
          + ("closes towards it until the force reaches the target" if fsr else
             "closes towards it, past the object, so the torque limit sets the squeeze" if tl else
             "closes only GRIP_SQUEEZE past GRIP_CLOSED, never to here: a hobby servo held here would sit at stall"),
          f"const float GRIP_MAX        = {_c(motion['hi'], 6)};   // {gu}: fully open",
          f"const float GRIP_CLOSED     = {_c(motion['closed'], 6)};   // {gu}: the calibrated zero ("
          + ("jaw faces touching" if motion["kind"] == "revolute" else "jaws on the gauge") + ")"
          + ("; a close with no force reading goes back here" if fsr and give_up == "GRIP_CLOSED" else ""),
          f"const float GRIP_CLOSE_SPEED = {_c(motion['close_speed'], 6)};   // {gu}/s",
          f"const float GRIP_OPEN_SPEED = {_c(motion['open_speed'], 6)};   // {gu}/s",
          f"const float GRIP_BACKOFF    = {_c(motion['backoff'], 6)};   // {gu}: step back when the force limit trips",
          f"const float GRIP_FORCE_GAIN = {_c(motion['force_gain'], 6)};   // force loop gain, {gu} per N*s",
          (f"float GRIP_TARGET_N         = {_c(grip['squeeze_n'], 2)};   // what G squeezes on a gauge-sized "
           f"{design.object.name}, the most that keeps one at the large end of its size ({ol['spread_mm']:g} mm per jaw "
           f"wider) {under}: below Morph's {grip['target_n']:g} N "
           f"target ({_window_text(grip)})" if ol.get("band_capped") else
           f"float GRIP_TARGET_N         = {_c(grip['squeeze_n'], 2)};   // what G squeezes, the most the hobby servo holds "
           f"continuously (its rated torque): below Morph's {grip['target_n']:g} N target ({_window_text(grip)} for the "
           f"{design.object.name})" if ol.get("capped") else
           f"float GRIP_TARGET_N         = {_c(grip['target_n'], 2)};   // {_window_text(grip)} for the {design.object.name}"),
          f"const float GRIP_LIMIT_N    = {_c(grip['limit_n'], 2)};   // hard stop and F's cap: the lowest of the object's "
          "damage force / 1.2, the force sensor's range and the servo's stall force per finger"
          + (", what the hobby servo holds continuously (its rated torque)"
             + (f" and what keeps a {design.object.name} at the large end of its size {under}" if ol.get("covers") else "")
             if ol.get("squeeze_mm") else ""),
          "const float GRIP_SENSE_N    = 0.05f;   // N: any force reading at all (an FSR 402 reads nothing below about 0.2 N)",
          f"const float GRIP_PROBE      = {_c(motion['probe'], 6)};   // {gu}: how far past GRIP_CLOSED the jaw may close before "
          + ("a force sensor reads anything (unused: no force sensors)" if not fsr else
             "a force sensor reads anything: to GRIP_MIN, so an object narrower than the gauge still gets gripped (the "
             "torque limit caps the squeeze meanwhile)" if tl else
             "a force sensor reads anything: to GRIP_MIN, so an object narrower than the gauge still gets gripped (the "
             "hobby servo's stall is below GRIP_LIMIT_N)" if not ol.get("probe_mm") else
             "a force sensor reads anything: it stops there, reads once more and opens to GRIP_MAX"
             + (f" (it reaches a {design.object.name} at the small end of its size)" if ol.get("covers") else "")
             + f"; with a dead sensor the hobby servo's own position loop squeezes a gauge-sized object about "
             f"{ol['probe_n']:g} N there and one at the large end of its size, {ol['spread_mm']:g} mm per jaw wider, "
             + ("its whole stall force" if ol["large_n"] >= grip["stall_force_n"] - 0.005 else f"about {ol['large_n']:g} N")
             + f", until it opens (it has no torque limit; it reaches stall about {ol['servo_band_mm']:g} mm past "
             f"contact)"),
          f"float GRIP_SQUEEZE          = {_c(motion['squeeze'], 6)};   // {gu}: how far past GRIP_CLOSED G closes without force "
          + ("sensors (unused: the force loop closes)" if fsr else
             "sensors: to GRIP_MIN, the torque limit sets the squeeze" if tl else
             f"sensors: where the hobby servo's own position loop squeezes a gauge-sized object about "
             f"{grip['squeeze_n']:g} N per finger and one at the large end of its size, {ol['spread_mm']:g} mm per jaw "
             f"wider, about {ol['large_n']:g} N (it reaches its {grip['stall_force_n']:g} N stall about "
             f"{ol['servo_band_mm']:g} mm past contact)"),
          f"const float GRIP_SQUEEZE_PER_N = {_c(motion['squeeze_per_n'], 6)};   // {gu} per N: F sets GRIP_SQUEEZE "
          + ("(0: not used here)" if fsr or tl else "(no torque limit: the travel past contact sets the squeeze)"),
          f"const float GRIP_SQUEEZE_LEAST = {_c(motion['squeeze_least'], 6)};   // {gu}: two position commands, the least close "
          "past contact the servo can be sent",
          f"const float GRIP_TORQUE_PER_N = {_c(tl['per_n'] if tl else 0.0, 6)};   // gripper servo torque limit per N at the jaw, "
          + (f"as a fraction of its stall torque on the {ep['supply']['servo_v']:g} V rail ({grip['lever_mm']:g} mm "
             f"{'pinion' if grip['jaw'] == 'rack' else 'lever'})" if tl else "0: hobby PWM servos have no torque limit"),
          f"float GRIP_TORQUE_N         = {_c(tl['force_n'] if tl else 0.0, 2)};   // N: the squeeze the torque limit allows "
          + ("(none: no torque limit)" if not tl else "(the grip itself: no force sensors)" if not fsr
             else "(a hard cap if the force loop fails)"),
          f"const float FSR_R_DIV       = {rd:.1f}f;   // divider resistor to GND, ohms",
          f"float FSR_K                 = {FSR_K_OHM_N:.1f}f;   // ohm*N: F = K / R_fsr (Z command calibrates)",
          ""]
    # bus layer
    L.append("// ---------------------------------------------------------------- servo bus")
    if proto == "pca9685_pwm":
        L += ["Adafruit_PWMServoDriver pca = Adafruit_PWMServoDriver(PCA9685_ADDR);", "",
              "void busBegin() {",
              "#if defined(ARDUINO_ARCH_ESP32)", "  Wire.begin(PIN_SDA, PIN_SCL);",
              "#elif defined(ARDUINO_ARCH_RP2040)", "  Wire.setSDA(PIN_SDA);", "  Wire.setSCL(PIN_SCL);", "  Wire.begin();",
              "#else", "  Wire.begin();", "#endif",
              "  pca.begin();", "  pca.setOscillatorFrequency(27000000);", "  pca.setPWMFreq(50);",
              "  if (PIN_PWM_OE >= 0) { pinMode(PIN_PWM_OE, OUTPUT); digitalWrite(PIN_PWM_OE, HIGH); }   // outputs off until enabled",
              "}", "",
              "void busTorque(bool on) {",
              "  if (PIN_PWM_OE >= 0) digitalWrite(PIN_PWM_OE, on ? LOW : HIGH);",
              "  if (!on) for (uint8_t ch = 0; ch < 16; ch++) pca.setPWM(ch, 0, 0);   // no pulses = servos go limp",
              "}", "",
              "void busWrite(const float *pos, uint8_t n, float grip) {",
              "  for (uint8_t k = 0; k < n; k++) pca.writeMicroseconds(SERVO[k].ch, (uint16_t)constrain(pos[k], 500.0f, 2500.0f));",
              "  pca.writeMicroseconds(GRIP_CH, (uint16_t)constrain(grip, 500.0f, 2500.0f));",
              "}", "",
              "bool busRead(uint8_t k, float *pos) { return false; }   // hobby servos have no position feedback", "",
              "void gripTorque() {}   // hobby PWM servos have no torque limit", ""]
    elif proto == "feetech_scs":
        L += ["SMS_STS bus;", "", "void busBegin() {",
              "#if defined(ARDUINO_ARCH_ESP32)",
              "  BUS_SERIAL.begin(BUS_BAUD, SERIAL_8N1, PIN_BUS_RX, PIN_BUS_TX);"]
        if cfg == "custom_pcb":
            L += ["  // the 74LVC2G241 direction pin is driven by UART1's RTS in RS-485 half-duplex mode (asserted while sending)",
                  "  uart_set_pin(UART_NUM_1, PIN_BUS_TX, PIN_BUS_RX, PIN_BUS_DIR, UART_PIN_NO_CHANGE);",
                  "  uart_set_mode(UART_NUM_1, UART_MODE_RS485_HALF_DUPLEX);",
                  "  // bench check: DIR must read HIGH while a frame is sent; if it is inverted on your silicon add",
                  "  // uart_set_line_inverse(UART_NUM_1, UART_SIGNAL_RTS_INV);"]
        L += ["#else", "  BUS_SERIAL.begin(BUS_BAUD);", "#endif", "  bus.pSerial = &BUS_SERIAL;", "  delay(50);", "}", "",
              "void busTorque(bool on) {",
              "  for (uint8_t k = 0; k < N_SERVOS; k++) bus.EnableTorque(SERVO[k].id, on ? 1 : 0);",
              "  bus.EnableTorque(GRIP_ID, on ? 1 : 0);", "}", "",
              "uint8_t ids[N_SERVOS + 1];", "s16 tgt[N_SERVOS + 1];", "u16 spd[N_SERVOS + 1];", "u8 acc[N_SERVOS + 1];", "",
              "void busWrite(const float *pos, uint8_t n, float grip) {",
              "  for (uint8_t k = 0; k < n; k++) { ids[k] = SERVO[k].id; tgt[k] = (s16)constrain(pos[k], 0.0f, 4095.0f); spd[k] = 0; acc[k] = 0; }",
              "  ids[n] = GRIP_ID; tgt[n] = (s16)constrain(grip, 0.0f, 4095.0f); spd[n] = 0; acc[n] = 0;",
              "  bus.SyncWritePosEx(ids, n + 1, tgt, spd, acc);   // targets are already rate-limited",
              "}", "",
              "bool busRead(uint8_t k, float *pos) {",
              "  int p = bus.ReadPos(k < N_SERVOS ? SERVO[k].id : GRIP_ID);",
              "  if (p < 0) return false;", "  *pos = (float)p;", "  return true;", "}", "",
              "#define STS_TORQUE_LIMIT 48   // RAM torque limit, 0-1000 = 0-100 % of stall (back to the EEPROM maximum at power-up)",
              "void gripTorque() {   // the gripper squeezes no harder than GRIP_TORQUE_N at the jaw",
              "  if (GRIP_TORQUE_PER_N <= 0.0f) return;",
              "  bus.writeWord(GRIP_ID, STS_TORQUE_LIMIT, (u16)(constrain(GRIP_TORQUE_N * GRIP_TORQUE_PER_N * 1000.0f, 10.0f, 1000.0f) + 0.5f));",
              "}", ""]
    else:
        L += ["Dynamixel2Arduino dxl(BUS_SERIAL, PIN_BUS_DIR);", "using namespace ControlTableItem;", "",
              "void busBegin() {", "  dxl.begin(BUS_BAUD);",
              "#if defined(ARDUINO_ARCH_ESP32)", "  BUS_SERIAL.setPins(PIN_BUS_RX, PIN_BUS_TX);", "#endif",
              "  dxl.setPortProtocolVersion(2.0);",
              "  for (uint8_t k = 0; k <= N_SERVOS; k++) {",
              "    uint8_t id = k < N_SERVOS ? SERVO[k].id : GRIP_ID;",
              "    if (!dxl.ping(id)) { Serial.print(\"# no DYNAMIXEL at id \"); Serial.println(id); continue; }",
              "    dxl.torqueOff(id);", "    dxl.setOperatingMode(id, OP_POSITION);",
              "    dxl.writeControlTableItem(PROFILE_VELOCITY, id, 0);   // targets are rate-limited here",
              "  }", "}", "",
              "void busTorque(bool on) {",
              "  for (uint8_t k = 0; k <= N_SERVOS; k++) { uint8_t id = k < N_SERVOS ? SERVO[k].id : GRIP_ID; if (on) dxl.torqueOn(id); else dxl.torqueOff(id); }",
              "}", "",
              "void busWrite(const float *pos, uint8_t n, float grip) {",
              "  for (uint8_t k = 0; k < n; k++) dxl.setGoalPosition(SERVO[k].id, constrain(pos[k], 0.0f, 4095.0f));",
              "  dxl.setGoalPosition(GRIP_ID, constrain(grip, 0.0f, 4095.0f));", "}", "",
              "bool busRead(uint8_t k, float *pos) {",
              "  float p = dxl.getPresentPosition(k < N_SERVOS ? SERVO[k].id : GRIP_ID);",
              "  if (p <= 0.0f) return false;", "  *pos = p;", "  return true;", "}", "",
              "void gripTorque() {   // Goal PWM caps the gripper's stall torque in position control (reset at power-up)",
              "  if (GRIP_TORQUE_PER_N <= 0.0f) return;",
              "  dxl.setGoalPWM(GRIP_ID, constrain(GRIP_TORQUE_N * GRIP_TORQUE_PER_N * 100.0f, 1.0f, 100.0f), UNIT_PERCENT);",
              "}", ""]
    tmpl = r'''// ---------------------------------------------------------------- state
enum State : uint8_t { ST_RUN, ST_ESTOP };
enum Grip : uint8_t { G_POS, G_CLOSING, G_HOLD };
const char *STATE_NAME[] = {"RUN", "ESTOP"};
const char *GRIP_NAME[] = {"POS", "CLOSING", "HOLD"};
State state = ST_RUN;
Grip gstate = G_POS;
float qT[N_JOINTS], qC[N_JOINTS];      // target / commanded joint angle, rad
float gT = GRIP_MAX, gC = GRIP_MAX;    // gripper target / commanded, $GRIP_UNIT
float speedScale = 0.5f, force = 0.0f, gripI = 0.0f, gripSeen = 0.0f;   // gripSeen: the highest force of this close
bool telemetry = false;
uint32_t lastTick = 0, lastTele = 0, lowSince = 0;
const uint16_t TICK_MS = 10;
char line[96];
uint8_t lineLen = 0;
Print *out = &Serial;
#if MORPH_WIFI
WiFiServer server(3333);
WiFiClient client;
#endif

// ---------------------------------------------------------------- sensors
float fsrNewtons(int pin) {
  if (pin < 0) return 0.0f;
  float v = analogRead(pin) * ADC_VREF / ADC_MAX;
  if (v < 0.01f) return 0.0f;
  float rFsr = FSR_R_DIV * (ADC_VREF - v) / v;
  return FSR_K / rFsr;
}

float readForce() {    // per-finger normal force: mean of the fitted pads
  float l = fsrNewtons(PIN_FSR_L), r = fsrNewtons(PIN_FSR_R);
  if (PIN_FSR_L >= 0 && PIN_FSR_R >= 0) return 0.5f * (l + r);
  return l + r;
}

float servoRailVolts() {
  if (PIN_VSERVO_SENSE < 0) return VSERVO_NOMINAL;
  return analogRead(PIN_VSERVO_SENSE) * ADC_VREF / ADC_MAX * VSERVO_DIVIDER;
}

bool estopActive() {
  if (PIN_ESTOP >= 0 && digitalRead(PIN_ESTOP) == LOW) return true;
  if (PIN_VSERVO_SENSE >= 0) {               // servo rail collapsed = e-stop loop open or supply lost
    if (servoRailVolts() < 0.6f * VSERVO_NOMINAL) { if (!lowSince) lowSince = millis(); }
    else lowSince = 0;
    return lowSince && millis() - lowSince > 50;
  }
  return false;
}

// ---------------------------------------------------------------- outputs
float servoPos[N_SERVOS];

float gripServoPosition(float g) {
  return GRIP_ZERO + constrain(g, GRIP_MIN, GRIP_MAX) * GRIP_PER_UNIT;
}

void writeAll() {
  for (uint8_t k = 0; k < N_SERVOS; k++)
    servoPos[k] = SERVO[k].zero + SERVO[k].dir * qC[SERVO[k].joint] * SERVO[k].per_rad;
  float g = gripServoPosition(gC);
  busWrite(servoPos, N_SERVOS, g);
}

void servoPower(bool on) {
  if (PIN_SERVO_EN >= 0) digitalWrite(PIN_SERVO_EN, on ? HIGH : LOW);
  busTorque(on);
}

void enterEstop(const char *why) {
  if (state == ST_ESTOP) return;
  state = ST_ESTOP;
  servoPower(false);
  gstate = G_POS;
  if (PIN_LED >= 0) digitalWrite(PIN_LED, HIGH);
  out->print("!ESTOP ");
  out->println(why);
}

void resyncFromServos() {   // after an e-stop the arm may have moved: start from where it is
  for (uint8_t k = 0; k < N_SERVOS; k++) {
    float p;
    if (busRead(k, &p)) qC[SERVO[k].joint] = qT[SERVO[k].joint] = (p - SERVO[k].zero) / (SERVO[k].dir * SERVO[k].per_rad);
  }
  float p;
  if (busRead(N_SERVOS, &p)) gC = gT = constrain((p - GRIP_ZERO) / GRIP_PER_UNIT, GRIP_MIN, GRIP_MAX);
}

void clearEstop() {
  if (PIN_ESTOP >= 0 && digitalRead(PIN_ESTOP) == LOW) { out->println("!ESTOP still pressed"); return; }
  if (PIN_SERVO_EN >= 0) { digitalWrite(PIN_SERVO_EN, HIGH); delay(150); }
  if (PIN_VSERVO_SENSE >= 0 && servoRailVolts() < 0.8f * VSERVO_NOMINAL) {
    if (PIN_SERVO_EN >= 0) digitalWrite(PIN_SERVO_EN, LOW);
    out->println("!ESTOP servo rail still off (release the e-stop / check the supply)");
    return;
  }
  lowSince = 0;
  resyncFromServos();
  busTorque(true);
  gripTorque();                              // a servo that lost power restarts at its maximum torque limit
  state = ST_RUN;
  if (PIN_LED >= 0) digitalWrite(PIN_LED, LOW);
  out->println("OK estop cleared");
}

// ---------------------------------------------------------------- commands
void status() {
  out->print("S,"); out->print(STATE_NAME[state]); out->print(','); out->print(GRIP_NAME[gstate]);
  for (uint8_t i = 0; i < N_JOINTS; i++) { out->print(','); out->print(qC[i], 4); }
  out->print(','); out->print(gC, 2); out->print(','); out->print(force, 3);
  out->print(','); out->println(servoRailVolts(), 2);
}

void setPose(bool ready) {
  for (uint8_t i = 0; i < N_JOINTS; i++) qT[i] = ready ? JOINT[i].ready : JOINT[i].home;
}

void command(char *s) {
  char *tok = strtok(s, " ,\t");
  if (!tok) return;
  char c = toupper(tok[0]);
  float v[N_JOINTS];
  uint8_t n = 0;
  char *t;
  while (n < N_JOINTS && (t = strtok(NULL, " ,\t")) != NULL) v[n++] = atof(t);
  switch (c) {
    case 'J':
      if (n != N_JOINTS) { out->println("ERR J needs N_JOINTS values"); return; }
      for (uint8_t i = 0; i < N_JOINTS; i++) qT[i] = constrain(v[i], JOINT[i].lo, JOINT[i].hi);
      break;
    case 'M':
      if (n == 2 && v[0] >= 0 && v[0] < N_JOINTS) { uint8_t i = (uint8_t)v[0]; qT[i] = constrain(v[1], JOINT[i].lo, JOINT[i].hi); }
      break;
    case 'V': if (n) speedScale = constrain(v[0], 0.05f, 1.0f); break;
    case 'W': if (n) { gstate = G_POS; gT = constrain(v[0], GRIP_MIN, GRIP_MAX); } break;
    case 'G':   // force sensors: close on the force loop; none: close GRIP_SQUEEZE past the object
      gstate = (PIN_FSR_L >= 0 || PIN_FSR_R >= 0) ? G_CLOSING : G_POS;
      if (gstate == G_POS) gT = constrain(GRIP_CLOSED - GRIP_SQUEEZE, GRIP_MIN, GRIP_MAX);
      gripI = 0; gripSeen = 0.0f; break;
    case 'O': gstate = G_POS; gT = GRIP_MAX; break;
    case 'F':
      if (n) GRIP_TARGET_N = constrain(v[0], 0.1f, GRIP_LIMIT_N);
      if (n && PIN_FSR_L < 0 && PIN_FSR_R < 0) {   // no sensors: the torque limit, or a hobby servo's travel past contact, is the grip
        GRIP_TORQUE_N = GRIP_TARGET_N; gripTorque();
        if (GRIP_SQUEEZE_PER_N > 0.0f) GRIP_SQUEEZE = constrain(GRIP_TARGET_N * GRIP_SQUEEZE_PER_N, GRIP_SQUEEZE_LEAST, GRIP_CLOSED - GRIP_MIN);
      }
      break;
    case 'H': setPose(false); break;
    case 'R': setPose(true); break;
    case 'E': enterEstop("software"); return;
    case 'C': clearEstop(); return;
    case 'S': telemetry = n && v[0] > 0.5f; break;
    case 'Q': status(); return;
    case 'Z':
      if (n) {
        int pin = PIN_FSR_L >= 0 ? PIN_FSR_L : PIN_FSR_R;
        float volts = pin >= 0 ? analogRead(pin) * ADC_VREF / ADC_MAX : 0.0f;
        if (volts > 0.01f) FSR_K = (v[0] / 1000.0f * 9.81f) * (FSR_R_DIV * (ADC_VREF - volts) / volts);
        out->print("OK K_FSR="); out->println(FSR_K, 1);
      }
      return;
    default: out->println("ERR unknown command"); return;
  }
  out->println("OK");
}

void pollStream(Stream &in, Print &reply) {
  while (in.available()) {
    char ch = in.read();
    if (ch == '\n' || ch == '\r') {
      if (lineLen) { line[lineLen] = 0; out = &reply; command(line); out = &Serial; lineLen = 0; }
    } else if (lineLen < sizeof(line) - 1) {
      line[lineLen++] = ch;
    }
  }
}

// ---------------------------------------------------------------- control loop (100 Hz)
void gripControl(float dt) {
  force = readForce();
  if (force > GRIP_LIMIT_N) { gT = constrain(gC + GRIP_BACKOFF, GRIP_MIN, GRIP_MAX); gstate = G_HOLD; gripI = 0; }   // over the limit: back off
  else if (gstate == G_CLOSING) {
    if (force > gripSeen) gripSeen = force;
    if (force >= GRIP_TARGET_N) { gstate = G_HOLD; gT = gC; }
    else gT = gC - GRIP_CLOSE_SPEED * dt;
$PROBE_BLOCK
    if (gT <= GRIP_MIN) { gT = GRIP_MIN; gstate = G_HOLD; }
  } else if (gstate == G_HOLD) {                                         // PI on the force error
    if (force < GRIP_SENSE_N) { gripI = 0.0f; }                         // reading lost (sensor or object): hold still
    else {
      float e = GRIP_TARGET_N - force;
      gripI = constrain(gripI + e * dt, -2.0f, 2.0f);
      gT = constrain(gT - (0.08f * e + 0.25f * gripI) * dt * GRIP_FORCE_GAIN, GRIP_MIN, GRIP_MAX);
    }
  }
  float step = (gT >= gC ? GRIP_OPEN_SPEED : GRIP_CLOSE_SPEED) * dt;    // slew limit in the gripper's own units
  gC = constrain(gC + constrain(gT - gC, -step, step), GRIP_MIN, GRIP_MAX);
}

void tick(float dt) {
  if (estopActive()) enterEstop("hardware");
  if (state == ST_ESTOP) return;
  for (uint8_t i = 0; i < N_JOINTS; i++) {
    float step = JOINT[i].vmax * speedScale * dt;
    qC[i] += constrain(qT[i] - qC[i], -step, step);
  }
  gripControl(dt);
  writeAll();
}

void setup() {
  Serial.begin(115200);
#if defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_RP2040)
  analogReadResolution(12);
#endif
  if (PIN_LED >= 0) pinMode(PIN_LED, OUTPUT);
  if (PIN_ESTOP >= 0) pinMode(PIN_ESTOP, INPUT_PULLUP);
  if (PIN_SERVO_EN >= 0) { pinMode(PIN_SERVO_EN, OUTPUT); digitalWrite(PIN_SERVO_EN, LOW); }
  busBegin();
  setPose(false);
  for (uint8_t i = 0; i < N_JOINTS; i++) qC[i] = qT[i];
  resyncFromServos();
  servoPower(true);
  delay(100);
  gripTorque();
#if MORPH_WIFI
  WiFi.softAP("morph-arm", "morph-arm-$PASS");   // or WiFi.begin(ssid, pass) for your network
  server.begin();
#endif
  Serial.println("# Morph arm ready: $NJ joints + gripper ($PROTO). Send Q for status. Gripper unit: $GRIP_UNIT.");
}

void loop() {
  pollStream(Serial, Serial);
#if MORPH_WIFI
  if (!client || !client.connected()) client = server.available();
  if (client && client.connected()) pollStream(client, client);
#endif
  uint32_t now = millis();
  if (now - lastTick >= TICK_MS) {
    float dt = (now - lastTick) / 1000.0f;
    lastTick = now;
    tick(dt > 0.05f ? 0.05f : dt);
  }
  if (telemetry && now - lastTele >= 50) {
    lastTele = now;
    Serial.print("T,"); Serial.print(now); Serial.print(','); Serial.print(STATE_NAME[state]);
    for (uint8_t i = 0; i < N_JOINTS; i++) { Serial.print(','); Serial.print(qC[i], 4); }
    Serial.print(','); Serial.print(gC, 2); Serial.print(','); Serial.println(force, 3);
  }
}
'''
    import string
    if give_up == "GRIP_MAX":   # a hobby probe: close no further than GRIP_PROBE with no reading, read there too, open
        probe_block = (
            "    if (gripSeen < GRIP_SENSE_N && gT < GRIP_CLOSED - GRIP_PROBE) {   // nothing read yet: no further than $PROBE_END\n"
            "      if (gC <= GRIP_CLOSED - GRIP_PROBE + 0.0001f) {                  // read there too: no object, or no sensor\n"
            "        gT = GRIP_MAX; gstate = G_POS;\n"
            "        out->println(\"!GRIP no force reading down to $PROBE_END: opened to GRIP_MAX instead of squeezing on (check the force sensors and their leads)\");\n"
            "      } else gT = GRIP_CLOSED - GRIP_PROBE;\n"
            "    }")
    else:
        probe_block = (
            "    if (gripSeen < GRIP_SENSE_N && gT <= GRIP_CLOSED - GRIP_PROBE) {   // nothing read all the way to $PROBE_END: no object, or no sensor\n"
            "      gT = GRIP_CLOSED; gstate = G_POS;\n"
            "      out->println(\"!GRIP no force reading down to $PROBE_END: back at GRIP_CLOSED instead of squeezing on (check the force sensors and their leads)\");\n"
            "    }")
    body = string.Template(tmpl.replace("$PROBE_BLOCK", probe_block)).safe_substitute(
        NJ=len(names), PROTO=proto, PASS=design.name[:8], GRIP_UNIT=gu, PROBE_END=probe_end)
    L.append(body)
    code = "\n".join(L)
    d = os.path.join(outdir, "firmware", "morph_arm")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "morph_arm.ino"), "w", encoding="utf-8") as fh:
        fh.write(code)
    return "firmware/morph_arm/morph_arm.ino"


# ------------------------------------------------------------------ sourcing

def _row(item, category, qty, vendors, make_or_buy="buy", how="buy from vendor", sku=None, note="", ref=""):
    vs = [dict(v) for v in vendors if v] or [V("digikey", item, 0.0)]
    sel = min(vs, key=lambda v: (v["unit_price"], v.get("lead_days", 99)))
    return {"item": item, "category": category, "qty": qty, "make_or_buy": make_or_buy, "how": how, "vendors": vs,
            "selected": {"vendor": sel["vendor"], "unit_price": round(sel["unit_price"], 2)},
            "ext_price": round(sel["unit_price"] * qty, 2), "sku": sku, "note": note, "ref": ref, "source": "electronics"}


def pcba_cost(circuit, qty: int) -> dict:
    """JLCPCB-style per-board estimate: bare PCB + assembly fees amortised over the order + parts."""
    comps = [c for c in circuit.comps if c.board and c.kind != "MH"]
    parts = sum(c.price for c in comps)
    ext_lines = len({(c.value, c.footprint) for c in comps if c.lcsc in ("verify", "")})
    tht_joints = sum(len(c.pins) for c in comps if (c.footprint or "").startswith(("CONN", "JST_PH", "JST_XH", "XT30", "CP_", "HDR")))
    smd_joints = sum(len(c.pins) for c in comps) - tht_joints
    q = max(qty, 2)
    fixed = 2.0 + 8.0 + 1.5 + 3.0 * ext_lines + (3.5 if tht_joints else 0.0) + 12.0     # pcb, setup, stencil, ext-part fees, THT setup, shipping
    unit = parts + smd_joints * 0.0017 + tht_joints * 0.0173 + fixed / q
    return {"unit": round(unit, 2), "parts": round(parts, 2), "fixed": round(fixed, 2), "order_qty": q,
            "extended_lines": ext_lines, "smd_joints": smd_joints, "tht_joints": tht_joints}


def sourcing_rows(design, ep: dict, circuit, harness: list, include_actuators=True) -> list:
    rows = []
    cfg = ep["config"]
    qty_build = max(design.build.quantity, 1)
    if include_actuators:
        counts: dict = {}
        for a in ep["actuators"]:
            counts[a["key"]] = counts.get(a["key"], 0) + 1
        for k, n_ in counts.items():
            joints = sorted({a["joint"] for a in ep["actuators"] if a["key"] == k})
            rows.append(_row(ACTUATORS[k]["name"], "actuator", n_, ACTUATORS[k]["vendors"], sku=k,
                             note="joints: " + ", ".join(joints) + ("; set bus IDs before assembly" if ep["bus"] != "pwm" else "")))
    if cfg == "custom_pcb":
        est = pcba_cost(circuit, qty_build)
        W, H = (ep.get("custom_pcb") or {}).get("size_mm", [92, 66])
        rows.append(_row(f"Morph controller PCBA ({W:g} x {H:g} mm, 2-layer, ESP32-S3)", "pcb", 1,
                         [V("jlcpcb", "PCBA", est["unit"], "https://jlcpcb.com/quote", lead_days=12,
                            note=f"order {est['order_qty']}: gerbers.zip + jlc_bom.csv + jlc_cpl.csv; {est['extended_lines']} extended-part lines"),
                          V("pcbway", "PCBA", round(est["unit"] * 1.35, 2), "https://www.pcbway.com/orderonline.aspx", lead_days=15,
                            note="same files; turnkey assembly")],
                         make_or_buy="make", how="order PCBA from JLCPCB with gerbers.zip + jlc_bom.csv + jlc_cpl.csv",
                         sku="morph_controller_pcba", note=f"parts ${est['parts']:.2f}/board + fixed ${est['fixed']:.0f}/order; "
                         "bench alternative: hand-build on perfboard with an ESP32-S3-DevKitC and the same pinout", ref="U1"))
    for m in ep["modules"]:
        k = m["key"]
        if k == "morph_controller_pcba":
            continue
        cat = {"supply": "electronics", "safety": "electronics", "protection": "electronics"}.get(MODULES[k].get("role"), "electronics")
        rows.append(_row(MODULES[k]["name"], cat, 1, MODULES[k]["vendors"], sku=k, ref=m["ref"],
                         note={"buck_servo": f"set the output to {ep['supply']['servo_v']:g} V before connecting servos",
                               "estop": "22 mm panel cut-out in the base cover; 1NC + 1NO contact blocks"}.get(k, "")))
    sens: dict = {}
    for s in ep["sensors"]:
        sens.setdefault(s["key"], []).append(s)
    for k, ss in sens.items():
        rows.append(_row(MODULES[k]["name"], "electronics", len(ss), MODULES[k]["vendors"], sku=k,
                         ref=",".join(s["ref"] for s in ss), note="mounted at " + ", ".join(s["where"] for s in ss)))
    if cfg == "modules" and ep["grip"]["sensor"] == "fsr402":
        r = ohm_str(ep["grip"]["fsr_divider_ohm"])
        rows.append(_row(f"FSR divider: 2 x {r} 1% resistors on a 20 x 30 mm perfboard + 2 x 2-pin headers", "electronics", 1,
                         [V("amazon", "perfboard resistor kit", 1.2)], make_or_buy="make", how="hand-solder on perfboard (10 min)"))
    # cables + connectors from the harness
    proto = ep["protocol"]
    bus_rows = [h for h in harness if h["kind"] == "servo_bus"]
    by_len: dict = {}
    for h in bus_rows:
        by_len.setdefault(h["stock_mm"] or h["length_mm"], []).append(h)
    for L_, hs in sorted(by_len.items(), key=lambda kv: kv[0] or 0):
        if hs[0]["make_or_buy"] == "buy":
            rows.append(_row(f"{BUS_CONNECTOR[proto]} servo cable {L_} mm", "cable", len(hs), BUS_CABLE_VENDOR[proto](L_),
                             note=", ".join(h["name"].split(" ", 1)[0] for h in hs)))
        else:
            rows.append(_row(f"{BUS_CONNECTOR[proto]} servo cable, custom {L_} mm", "cable", len(hs),
                             [V("amazon", "silicone wire 24 AWG 3 colours", 0.4)], make_or_buy="make",
                             how="cut and crimp to harness.csv", note=", ".join(h["name"].split(" ", 1)[0] for h in hs)))
    pwm = [h for h in harness if h["kind"] == "pwm" and h.get("stock_mm")]
    if pwm:
        rows.append(_row("Servo extension cable 3-pin JR, 300/500 mm assortment", "cable", len(pwm),
                         [V("amazon", "servo extension cable 300mm 500mm", 0.5)], note="one per channel that needs it"))
    if any(h["kind"] == "sensor" for h in harness):
        rows.append(_row("FSR lead: shielded 28 AWG pair + JST-PH 2p housing + Amphenol Clincher", "cable",
                         sum(1 for h in harness if h["kind"] == "sensor"),
                         [V("digikey", "Amphenol Clincher 2 pin 65801-002LF", 1.9), V("adafruit", "FSR clincher", 1.5)],
                         make_or_buy="make", how="crimp per harness.csv; heat-shrink the Clincher"))
    if any(h["kind"] == "i2c" for h in harness):
        h = next(h for h in harness if h["kind"] == "i2c")
        rows.append(_row(f"Qwiic / STEMMA QT cable {h['stock_mm'] or h['length_mm']} mm", "cable", 1,
                         [V("sparkfun", "Qwiic cable 500mm", 1.95), V("adafruit", "STEMMA QT cable 400mm", 1.25)],
                         make_or_buy=h["make_or_buy"]))
    if "wrist_camera" in ep["extras"]:
        rows.append(_row("High-flex USB 2.0 cable, 2 m (wrist camera to host)", "cable", 1, [V("amazon", "high flex USB 2.0 cable 2m", 7.0)]))
    rows.append(_row("USB-C panel-mount extension (host link)", "connector", 1, MODULES["usb_c_breakout"]["vendors"], sku="usb_c_breakout"))
    rows.append(_row("5.5 x 2.5 mm panel DC jack + " + (f"XT30 (F) pigtail, {ep['power_budget']['feed_awg']} AWG" if cfg == "custom_pcb"
                     else f"{ep['power_budget']['feed_awg']} AWG leads"), "connector", 1,
                     [V("amazon", "5.5x2.5 panel jack XT30 pigtail", 2.5)], make_or_buy="make", how="solder + heat-shrink"))
    rows.append(_row("Crimp kit: JST-PH/XH/SH housings + contacts, heat-shrink, cable ties", "connector", 1,
                     [V("amazon", "JST PH XH crimp connector kit", 9.0)], note="shared across builds"))
    return rows


def configs_compare(design, sizing, asm, grip: dict | None = None) -> dict:
    out = {}
    for cfg in ("modules", "custom_pcb"):
        d2 = design.with_changes({"electronics.config": cfg})
        ep = plan(d2, sizing, grip)
        circ = custom_circuit(d2, ep) if cfg == "custom_pcb" else modules_circuit(d2, ep)
        har, _ = build_harness(d2, ep, asm, ep["power_budget"])
        rows = sourcing_rows(d2, ep, circ, har, include_actuators=False)
        cost = sum(r["ext_price"] for r in rows)
        wires = len(har)
        bay = [h for h in har if h["kind"] in ("power", "signal")]
        if cfg == "custom_pcb":
            minutes = 15 + 3 * wires
            lead = 12
            est = pcba_cost(circ, max(design.build.quantity, 1))
            summary = (f"One {ep['custom_pcb']['size_mm'][0]:g} x {ep['custom_pcb']['size_mm'][1]:g} mm 2-layer PCBA from JLCPCB "
                       f"(ESP32-S3, {'PCA9685' if ep['bus'] == 'pwm' else 'half-duplex TTL buffer'}, buck + LDO, FSR inputs, USB-C, "
                       f"protected input, FET-switched servo rail); {wires} harness cables")
            effort = "order PCBA, crimp the harness, flash firmware"
            extra = {"pcba_unit": est["unit"], "order_qty": est["order_qty"]}
        else:
            minutes = 30 + 5 * len(bay) + 3 * (wires - len(bay)) + (10 if ep["grip"]["sensor"] == "fsr402" else 0)
            lead = max([v.get("lead_days", 3) for r in rows for v in r["vendors"][:1]] + [3])
            names = [MODULES[m["key"]]["name"].split(" (")[0] for m in ep["modules"] if not MODULES[m["key"]].get("external")
                     and m["key"] not in ("estop",)]
            summary = f"{' + '.join(names)} on a printed tray; {wires} wires incl. {len(bay)} in the base bay" + \
                      (", perfboard FSR dividers" if ep["grip"]["sensor"] == "fsr402" else "")
            effort = "mount modules, wire the bay, crimp the harness, flash firmware"
            extra = {}
        if cfg == "custom_pcb":
            pc = pcba_cost(circ, 100)
            cost100 = cost - est["unit"] + pc["unit"]
        else:
            cost100 = cost * 0.82          # distributor price breaks on modules
        out[cfg] = {"cost": round(cost, 2), "cost_qty100": round(cost100, 2), "summary": summary, "effort": effort,
                    "assembly_min": int(minutes),
                    "lead_days": lead, "wires": wires, "parts": len(rows), "issues": ep["issues"], **extra}
    return out


# ------------------------------------------------------------------ finalize

def _rev_of(outdir):
    import re
    for part in os.path.normpath(outdir).split(os.sep)[::-1]:
        if re.fullmatch(r"r\d{3,}", part):
            return part
    return ""


def finalize(design, sizing: dict, eplan: dict, asm, outdir: str) -> dict:
    ep = eplan
    _one_protocol(ep)       # before anything is written: no harness, board or firmware for a mixed bus
    _one_rail(ep)           # nor for servos on a rail outside their voltage range
    os.makedirs(outdir, exist_ok=True)
    issues, warnings = list(ep.get("issues", [])), list(ep.get("warnings", []))
    rev = _rev_of(outdir)
    import datetime
    date = datetime.date.today().isoformat()
    # bus ids: the assembly is the physical truth when it carries its own id table
    if asm is not None and asm.meta.get("servo_ids") and ep["bus"] != "pwm":
        mine = [(a["joint"], a["id"]) for a in ep["actuators"]]
        joints = [j.name for j in asm.joints]
        theirs = [(_joint_alias(s["joint"], joints), s["id"]) for s in asm.meta["servo_ids"]]
        if [i for _, i in mine] != [i for _, i in theirs] or [j for j, _ in mine] != [j for j, _ in theirs]:
            warnings.append(f"assembly servo ids {theirs} differ from the electronics plan {mine}; firmware follows the plan")
    circuit = custom_circuit(design, ep) if ep["config"] == "custom_pcb" else modules_circuit(design, ep)
    from . import schematic
    sch = schematic.write(circuit, outdir, rev=rev, date=date, name="schematic")
    # system wiring sheet for the custom board too (PCBA + supply + actuators + sensors)
    board = None
    if ep["config"] == "custom_pcb":
        from . import pcb
        W, H = ep["custom_pcb"]["size_mm"]
        res = pcb.build(circuit, W, H, os.path.join(outdir, "board"), hc_nets=("VIN_RAW", "V_FUSED", "V_IN", "V_SERVO"),
                        pwr_nets=("5V", "VSYS", "3V3", "VBUS", "SW"), name="morph_ctrl",
                        title=f"MORPH CTRL {rev.upper() or 'REV A'}")
        b = res["board"]
        board = {"json": "board/board.json", "gerbers": "board/gerbers/gerbers.zip", "size_mm": b["size_mm"], "drc": b["drc"],
                 "svg": "board/board.svg", "render": "board/board_render.svg", "bom": "board/jlc_bom.csv", "cpl": "board/jlc_cpl.csv",
                 "layers": 2, "components": b["stats"]["components"], "traces": b["stats"]["traces"], "vias": b["stats"]["vias"],
                 "stats": b["stats"], "jlc": res["jlc"], "files": ["board/" + f for f in res["gerbers"]]}
        if not b["drc"]["pass"]:
            issues.append(f"custom PCB DRC fails: {len(b['drc']['unrouted'])} unrouted, {b['drc']['n_violations']} violations")
        if b["size_mm"] != list(ep["custom_pcb"]["size_mm"]):
            warnings.append(f"PCB grew to {b['size_mm']} mm (planned {ep['custom_pcb']['size_mm']})")
    harness, w = build_harness(design, ep, asm, ep["power_budget"])
    warnings += w
    with open(os.path.join(outdir, "harness.csv"), "w", newline="", encoding="utf-8") as fh:
        cols = ["name", "from", "to", "length_mm", "conductors", "wire", "connector_a", "connector_b", "current_a", "make_or_buy",
                "stock_mm", "note"]
        wr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(harness)
    fw = firmware(design, ep, asm, outdir, rev)
    sourcing = sourcing_rows(design, ep, circuit, harness)
    configs = configs_compare(design, sizing, asm, ep["grip"])
    result = {
        "config": ep["config"], "bus": ep["bus"], "protocol": ep["protocol"], "controller": ep["controller"],
        "schematic_svg": "schematic.svg", "schematic_json": "schematic.json", "sheet": sch["sheet"], "nets": sch["nets"],
        "board": board, "harness": harness, "harness_csv": "harness.csv", "firmware": fw,
        "firmware_validation": {"status": "unverified", "board_core": "not_verified", "bench": "not_verified"},
        "power_budget": ep["power_budget"], "grip": {**ep["grip"], "motion": grip_motion(design, ep, asm)},
        "sourcing": sourcing, "configs": configs,
        "totals": {"electronics": round(sum(r["ext_price"] for r in sourcing if r["category"] != "actuator"), 2),
                   "actuators": round(sum(r["ext_price"] for r in sourcing if r["category"] == "actuator"), 2)},
        "issues": issues, "warnings": list(dict.fromkeys(warnings)),
    }
    with open(os.path.join(outdir, "electronics.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, default=str)
    return result
