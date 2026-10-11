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
    "esp32_s3_wroom": {"BUS_TX": "IO17", "BUS_RX": "IO18", "BUS_DIR": "IO16", "SDA": "IO8", "SCL": "IO9", "PWM_OE": "IO7",
                       "FSR_L": "IO4", "FSR_R": "IO5", "VSERVO_SENSE": "IO6", "SERVO_EN": "IO15", "LED": "IO21",
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
    "esp32_s3_wroom": {"BUS_TX": "17", "BUS_RX": "18", "BUS_DIR": "16", "SDA": "8", "SCL": "9", "PWM_OE": "7", "FSR_L": "4",
                       "FSR_R": "5", "VSERVO_SENSE": "6", "SERVO_EN": "15", "LED": "21", "ESTOP": "-1"},
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
