"""People's names for design keys and values, the way the Parameters panel shows them ("Build", "Pilot", "380 mm",
"wrist camera + e-stop"). The chat's parameter edits and Teams' change requests read the same."""

from __future__ import annotations

from ..robot.pipeline2 import PARAMS

_PARAMS = {k: (label, group, extra) for k, label, group, _, extra in PARAMS}
VALUES = {"hobby_pwm": "Hobby PWM", "smart_bus": "Smart bus", "dynamixel": "Dynamixel", "modules": "Off-the-shelf modules",
          "custom_pcb": "Custom PCB", "prototype": "Prototype", "pilot": "Pilot", "production": "Production", "fsr402": "FSR 402",
          "esp32_devkit": "ESP32 DevKit", "esp32_s3_devkit": "ESP32-S3 DevKit", "arduino_nano": "Arduino Nano",
          "rp2040_pico": "RP2040 Pico", "v": "V-groove", "wrist_camera": "wrist camera", "estop": "e-stop", "imu": "IMU"}


def show_value(key: str, value) -> str:
    """A design value as people read it: option names and numbers with their unit ("Dynamixel", "70 g", "wrist camera +
    e-stop"), the way the Parameters panel shows them."""
    if value is None or value == "":
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        unit = ((_PARAMS.get(key) or (None, None, {}))[2] or {}).get("unit") or ""
        return f"{value:g}{unit}" if unit == "×" else f"{value:g} {unit}".strip()
    s = str(value)
    return " + ".join(VALUES.get(x, x.replace("_", " ")) for x in s.split(",")) if "," in s else VALUES.get(s, s)


def show_key(key: str) -> str:
    """A design key as the Parameters panel names it ("Arm · Actuators"), else the key itself."""
    p = _PARAMS.get(key)
    return f"{p[1]} · {p[0]}" if p else key


def describe(changes: dict) -> str:
    """Parameter edits in a line: "Build → Pilot, Reach → 380 mm" (a key the panel doesn't list keeps its own name)."""
    return ", ".join(f"{(_PARAMS.get(k) or (k.split('.')[-1].replace('_', ' '),))[0]} → {show_value(k, v)}"
                     for k, v in changes.items())
