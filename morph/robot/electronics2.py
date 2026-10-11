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
from .catalog import ACTUATORS, MODULES, V
from .schematic import Circuit

G = 9.81
FSR_K_OHM_N = 10_000.0          # FSR 402: R ~ K / F  (10 kOhm at ~1 N); calibrate on the bench
FSR_MAX_N = 20.0
GEAR_EFF = 0.6
E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]

FAMILY_BUS = {  # family -> (contract bus, protocol, baud)
    "hobby_pwm": ("pwm", "pca9685_pwm", 0),
    "smart_bus": ("ttl_half_duplex", "feetech_scs", 1_000_000),
    "smart_bus_7v4": ("ttl_half_duplex", "feetech_scs", 1_000_000),
    "dynamixel": ("ttl_half_duplex", "dynamixel_2", 1_000_000),
}
BUS_CONNECTOR = {"feetech_scs": "Molex 5264 3p (2.5 mm)", "dynamixel_2": "JST EH 3p (B3B-EH)", "pca9685_pwm": "Dupont/JR 3p (2.54 mm)"}

# chassis-wiring ampacity (A) for short bundled runs in silicone insulation, and resistance (ohm/m)
AWG = [(28, 1.4, 0.213), (26, 2.2, 0.134), (24, 3.5, 0.0842), (22, 5.0, 0.0530), (20, 7.5, 0.0333),
       (18, 10.0, 0.0210), (16, 13.0, 0.0132), (14, 18.0, 0.0083)]
CONNECTOR_A = {"Molex 5264 3p (2.5 mm)": 3.0, "JST EH 3p (B3B-EH)": 3.0, "Dupont/JR 3p (2.54 mm)": 3.0, "JST-PH 2p": 2.0,
               "XT30": 15.0, "JST-XH 2p": 3.0, "JST-SH 4p (Qwiic)": 1.0}
STD_FUSES = [1, 2, 3, 5, 7.5, 10, 15, 20, 25, 30]

# pin maps: logical signal -> controller pin name (as printed on the board / module pinout)
PINMAP = {
    "esp32_s3_wroom": {"BUS_TX": "IO17", "BUS_RX": "IO18", "BUS_DIR": "IO16", "SDA": "IO42", "SCL": "IO41", "PWM_OE": "IO7",
                       "FSR_L": "IO1", "FSR_R": "IO2", "VSERVO_SENSE": "IO6", "SERVO_EN": "IO15", "LED": "IO21",
                       "USB_DM": "IO19", "USB_DP": "IO20", "ESTOP": "IO6"},
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
    "esp32_s3_wroom": {"BUS_TX": "17", "BUS_RX": "18", "BUS_DIR": "16", "SDA": "42", "SCL": "41", "PWM_OE": "7", "FSR_L": "1",
                       "FSR_R": "2", "VSERVO_SENSE": "6", "SERVO_EN": "15", "LED": "21", "ESTOP": "-1"},
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


def grip_window(design, sizing: dict | None = None) -> dict:
    """Grip-force window per finger: hold (slip) bound, damage bound, target and hard limit."""
    o, e = design.object, design.electronics
    sensor = design.finger.sensor if design.finger.sensor == "fsr402" else None
    hold = o.mass_g / 1000.0 * G * design.safety_factor / (2.0 * max(o.friction, 0.05))
    lo = hold * 1.5
    hi = o.max_force_n / 1.5 if o.max_force_n > 0 else float("inf")
    if sensor:
        hi = min(hi, FSR_MAX_N * 0.8)
    target = (sizing or {}).get("grip_target_n") or e.target_force_n or (
        min(math.sqrt(lo * hi), 2.0 * lo) if hi != float("inf") else 1.3 * lo)
    limit = min(o.max_force_n / 1.2 if o.max_force_n > 0 else 1e9, FSR_MAX_N * 0.95 if sensor else 1e9,
                max(design.gripper.force_n, target * 1.2))
    return {"hold_n": round(hold, 2), "window_n": [round(lo, 2), round(hi, 2) if hi != float("inf") else None],
            "target_n": round(float(target), 2), "limit_n": round(float(limit), 2), "sensor": sensor or "none",
            "ok": lo <= hi}


def _family_of(key: str) -> str:
    return ACTUATORS[key].get("family", "smart_bus")


def _gripper_key(design, sizing: dict, family: str) -> tuple[str, list]:
    warns = []
    g = sizing.get("gripper") if isinstance(sizing.get("gripper"), dict) else None
    key = (g or {}).get("actuator") or sizing.get("gripper_actuator") or ""
    fam_keys = catalog.FAMILY_ACTUATORS.get(family, [])
    if key not in ACTUATORS or ACTUATORS[key]["interface"] != ACTUATORS[fam_keys[0]]["interface"]:
        fallback = {"hobby_pwm": "mg996r", "smart_bus": "sts3215", "dynamixel": "xl430_w250"}.get(family, fam_keys[0])
        if key:
            warns.append(f"gripper actuator {key} does not match the {family} bus; using {fallback} on the same bus")
        key = fallback
    return key, warns


def actuator_list(design, sizing: dict) -> tuple[list, list]:
    """[{joint, key, id, channel, torque_req_nm, torque_with_margin_nm}] arm joints + gripper, bus ids from 1."""
    family = design.arm.actuator_family
    bus, proto, _ = FAMILY_BUS.get(family, FAMILY_BUS["smart_bus"])
    out = []
    for i, j in enumerate(sizing.get("joints", [])):
        out.append({"joint": j["name"], "key": j["actuator"], "torque_req_nm": j.get("torque_req_nm", 0.0),
                    "torque_with_margin_nm": j.get("torque_with_margin_nm", 0.0), "sid": j.get("servo_id")})
    gkey, warns = _gripper_key(design, sizing, family)
    g = sizing.get("gripper") if isinstance(sizing.get("gripper"), dict) else {}
    out.append({"joint": "gripper", "key": gkey, "torque_req_nm": g.get("torque_req_nm"),
                "torque_with_margin_nm": g.get("torque_with_margin_nm"), "sid": g.get("servo_id")})
    for i, a in enumerate(out):
        if bus == "pwm":
            a["id"], a["channel"] = i + 1, i
        else:
            a["id"], a["channel"] = int(a.pop("sid") or (i + 1)), None
        a.pop("sid", None)
    return out, warns


# ------------------------------------------------------------------ power

def _actuator_currents(a: dict, design, grip: dict) -> tuple[float, float, float]:
    """(peak, avg, stall) current for one actuator."""
    s = ACTUATORS[a["key"]]
    tau = s["torque_nm"]
    if a["joint"] == "gripper":
        t_need = grip["target_n"] * design.electronics.pinion_radius_mm / 1000.0 / GEAR_EFF
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


def _pick_supply(vrange, nom, need_a) -> dict:
    """Supply whose voltage sits inside the actuators' range (closest to nominal, enough current), else 12 V + buck."""
    cands = []
    for k, m in MODULES.items():
        if m.get("role") != "supply" or not m.get("external") or "battery" in k:
            continue
        v, a = m["out_v"], m["out_a"]
        if vrange[0] - 1e-6 <= v <= vrange[1] + 1e-6:
            cands.append((a < need_a * 1.1, abs(v - nom), catalog.price(k), k))
    if cands:
        cands.sort()
        k = cands[0][3]
        return {"key": k, "v": MODULES[k]["out_v"], "a": MODULES[k]["out_a"], "servo_buck": False}
    return {"key": "psu_12v", "v": 12.0, "a": 10.0, "servo_buck": True}


def power_budget(design, acts: list, config: str, supply: dict, extras: list, ctrl: str) -> dict:
    grip = grip_window(design)
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


def plan(design, sizing: dict) -> dict:
    config = design.electronics.config if design.electronics.config in ("modules", "custom_pcb") else "modules"
    family = design.arm.actuator_family if design.arm.actuator_family in FAMILY_BUS else "smart_bus"
    bus, proto, baud = FAMILY_BUS[family]
    extras = extras_of(design)
    issues, warnings = [], []
    if design.electronics.config not in ("modules", "custom_pcb"):
        warnings.append(f"unknown electronics.config '{design.electronics.config}', using modules")
    acts, w = actuator_list(design, sizing)
    warnings += w
    for a in acts:
        if ACTUATORS[a["key"]]["interface"] != ("pwm" if bus == "pwm" else "ttl_half_duplex"):
            issues.append(f"{a['joint']}: {a['key']} is not a {bus} actuator; all joints must share the {family} bus")
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
    grip = grip_window(design, sizing)
    rough_peak = sum(_actuator_currents(a, design, grip)[0] for a in acts)
    supply = _pick_supply(vrange, nom, rough_peak)
    servo_v = supply["v"] if not supply["servo_buck"] else min(max(nom, vrange[0]), vrange[1])
    supply["servo_v"] = servo_v
    if config == "custom_pcb" and supply["servo_buck"]:
        issues.append(f"no catalogued supply sits inside the servos' {vrange[0]:g}-{vrange[1]:g} V range; the custom PCB "
                      "passes its input straight to the servo rail, so use the modules config (12 V + servo buck)")
    pb = power_budget(design, acts, config, supply, extras, ctrl)
    if not pb["supply_ok"]:
        issues.append(f"supply {supply['key']} ({supply['a']:g} A) has {pb['margin']:.2f}x margin over the "
                      f"{pb['input_peak_a']:.1f} A peak: pick a bigger supply or slower motion profiles")
    if pb["input_stall_a"] > supply["a"]:
        warnings.append(f"all servos stalled together would draw {pb['input_stall_a']:.1f} A > {supply['a']:g} A supply: "
                        "the firmware ramps targets and the supply's current limit / the fuse protect the rest")
    if not grip["ok"]:
        issues.append(f"no safe grip force: holding needs {grip['window_n'][0]} N but the object limit is {grip['window_n'][1]} N")

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
    else:
        warnings.append("no force sensor on the fingers: grip force is estimated from servo load (open loop for PWM servos)")
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
    _part(c, "R5", "R100k", "R", [("1", "1", "V_SERVO"), ("2", "2", "VSERVO_SENSE")], "power", place={"near": "U1", "pin": "6"})
    _part(c, "R6", "R22k", "R", [("1", "1", "VSERVO_SENSE"), ("2", "2", "GND")], "power", place={"near": "U1", "pin": "6"})

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
    _part(c, "SW1", "BTN", "SW", [("1", "1", "EN"), ("2", "2", "GND")], "mcu", value="RESET", place={"near": "U1", "side": "left"})
    _part(c, "SW2", "BTN", "SW", [("1", "1", "BOOT"), ("2", "2", "GND")], "mcu", value="BOOT", place={"near": "U1", "pin": "27"})
    _part(c, "R12", "R10k", "R", [("1", "1", "3V3"), ("2", "2", "BOOT")], "mcu", place={"near": "U1", "pin": "27"})
    _part(c, "R13", "R1k", "R", [("1", "1", "LED_STAT"), ("2", "2", "LED_K")], "mcu", place={"near": "U1", "pin": "23"})
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
            r = c.add(f"R{40 + j}", ohm_str(rdiv), "R", [("1", "1", net), ("2", "2", "GND")], footprint="R0603",
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
    """Box symbol for a dev-board module: every header pin, nets by pin name (first match for duplicates)."""
    left, right = MODULE_PINS[key]
    pins, seen = [], set()
    for side, names, hdr in (("L", left, "J1"), ("R", right, "J3" if key == "esp32_s3_devkit" else "J2")):
        for i, nm in enumerate(names):
            label = nm.replace("D12/SCL", "D12/SCL")
            net = None
            for k in (nm, nm.split("/")[-1]):
                if k in nets:
                    net = nets[k]
            if nm in ("GND", "AGND"):
                net = "GND"
            if nm in ("3V3", "VCC") and "3V3" in nets.values() and (nm, side) not in seen:
                net = "3V3" if any(v == "3V3" for v in nets.values()) else None
            seen.add((nm, side))
            et = "power_in" if nm in ("GND", "AGND", "5V", "VIN", "VSYS", "VBUS") else "bidir"
            pins.append((f"{hdr}-{i + 1}", label, net, side, et))
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
        nets.update({"VIN": "V_FUSED"})
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
