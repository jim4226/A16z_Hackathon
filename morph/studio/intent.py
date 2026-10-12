"""Chat message -> design changes.

Claude (tool use) when credentials exist; a deterministic rule parser otherwise,
so the live demo never depends on the network. Either way the result is a dict
of dotted-key changes that the pipeline validates.
"""

from __future__ import annotations

import json
import re

from ..spec import Design

MODEL = "claude-opus-5-5"

OBJECTS = {  # rough real-world values; max_force_n = per-finger squeeze that damages it (0 = robust)
    "raw egg": dict(shape="cylinder", diameter_mm=44, length_mm=57, mass_g=60, friction=0.35, max_force_n=25),
    "strawberry": dict(shape="sphere", diameter_mm=30, length_mm=35, mass_g=20, friction=0.6, max_force_n=3),
    "tomato": dict(shape="sphere", diameter_mm=65, length_mm=65, mass_g=120, friction=0.5, max_force_n=8),
    "grape": dict(shape="sphere", diameter_mm=20, length_mm=22, mass_g=6, friction=0.5, max_force_n=2),
    "ping pong ball": dict(shape="sphere", diameter_mm=40, length_mm=40, mass_g=2.7, friction=0.4, max_force_n=2),
    "light bulb": dict(shape="sphere", diameter_mm=60, length_mm=60, mass_g=30, friction=0.3, max_force_n=15),
    "soda can": dict(shape="cylinder", diameter_mm=66, length_mm=120, mass_g=370, friction=0.35, max_force_n=0),
    "marker": dict(shape="cylinder", diameter_mm=18, length_mm=130, mass_g=15, friction=0.5, max_force_n=0),
    "water bottle": dict(shape="cylinder", diameter_mm=65, length_mm=200, mass_g=520, friction=0.4, max_force_n=0),
}
ALIASES = {"egg": "raw egg", "strawberr": "strawberry", "tomato": "tomato", "grape": "grape",
           "ping pong": "ping pong ball", "pingpong": "ping pong ball", "bulb": "light bulb", "can": "soda can",
           "soda": "soda can", "marker": "marker", "pen": "marker", "bottle": "water bottle"}
# Whole words only (plurals allowed), so "eggplant", "grapefruit", "penguin" and "candy" match nothing. "can" counts only
# as a noun ("a can", "the can", "cans", "soda can"), never the verb in "a robot that can pour" or "can it lift 2 kg?".
_VERB_AFTER_CAN = r"(?:i|you|it|we|they|he|she|be|do|lift|pick|hold|carry|move|grab|reach|handle|go|make|pour|sort|help)"
ALIAS_PATTERNS = [
    (r"\beggs?\b", "raw egg"), (r"\bstrawberr(?:y|ies)\b", "strawberry"), (r"\btomato(?:e?s)?\b", "tomato"),
    (r"\bgrapes?\b", "grape"), (r"\bping[ -]?pong\b", "ping pong ball"), (r"\b(?:light ?)?bulbs?\b", "light bulb"),
    (r"\bsodas?\b|\bcans\b|\b(?:soda|beer|tin|drink|food|coke|pop|aluminium|aluminum|empty)\s+can\b"
     rf"|\b(?:a|an|the|one|each|every|my|your|this)\s+can\b(?!['’]|\s+{_VERB_AFTER_CAN}\b)", "soda can"),
    (r"\bmarkers?\b", "marker"), (r"\bpens?\b", "marker"), (r"\bbottles?\b", "water bottle"),
]

_NUM = r"(\d+(?:\.\d+)?)"
_UNITS = [  # every length becomes "<n> mm" and every mass "<n> g", so the rules below only parse mm and g
    (_NUM + r"\s*(?:kgs?|kilos?|kilograms?)\b", 1000.0, "g"),
    (_NUM + r"\s*(?:lbs?|pounds?)\b", 453.592, "g"),
    (_NUM + r"\s*(?:oz|ounces?)\b", 28.3495, "g"),
    (_NUM + r"\s*(?:grams?|gr)\b", 1.0, "g"),
    (_NUM + r"\s*(?:cm|centimet(?:er|re)s?)\b", 10.0, "mm"),
    (_NUM + r"\s*(?:millimet(?:er|re)s?)\b", 1.0, "mm"),
    (_NUM + r"\s*(?:met(?:er|re)s?)\b", 1000.0, "mm"),
    # "in" only when it cannot be the preposition: "2 in wide", "a 3 in cube", "2 inches"
    (_NUM + r"\s*(?:inch(?:es)?\b|in\.?(?=\s*(?:wide|long|tall|high|thick|across|diameter|dia\b|cube|box|ball|sphere|"
             r"cylinder|tube|object)\b|\s*(?:$|[,;)?!])))", 25.4, "mm"),
    (_NUM + r'\s*(?:"|”)', 25.4, "mm"),
]


def normalize_units(text: str) -> str:
    """'38 cm' -> '380 mm', '2 kg' -> '2000 g', '1.5 lb' -> '680 g', '2 in wide' -> '51 mm wide' (text is lower case)."""
    for pat, k, unit in _UNITS:
        text = re.sub(pat, lambda m: f"{round(float(m.group(1)) * k, 1):g} {unit}", text)
    return text


def object_alias(text: str) -> str | None:
    """The intent.OBJECTS preset a (lower-case) message names, if any."""
    for pat, key in ALIAS_PATTERNS:
        if re.search(pat, text):
            return key
    return None


def fit_finger(obj: dict) -> dict:
    r = obj["diameter_mm"] / 2
    return {"finger.length_mm": round(max(50.0, r + 30.0), 1),
            "finger.pad_height_mm": round(min(max(r * 0.9, 15.0), 40.0), 1),
            "finger.pad_depth_mm": round(min(max(r * 0.25, 3.0), 12.0), 1)}


def object_changes(key: str) -> dict:
    o = OBJECTS[key]
    ch = {f"object.{k}": v for k, v in o.items()}
    ch["object.name"] = key
    ch.update(fit_finger(o))
    return ch


def robot_rules(t: str, d: Design, ch: dict, said: list):
    """Whole-robot requests: actuator family, DOF, reach, electronics config, extras, build config."""
    fam = None
    if re.search(r"dynamixel|robotis|xl430|xm430", t):
        fam = "dynamixel"
    elif re.search(r"feetech|sts3215|smart (bus )?servo|serial bus|bus servo", t):
        fam = "smart_bus"
    elif re.search(r"hobby|pwm servo|rc servo|cheap servo|mg996r servos|ds3218", t) and "gripper" not in t:
        fam = "hobby_pwm"
    if fam:
        ch["arm.actuator_family"] = fam
        said.append(f"actuators → {fam.replace('_', ' ')}")
    m = re.search(r"\b([456])\s*-?\s*(dof|axis|axes|degrees)", t) or re.search(r"\b(four|five|six)\s*-?\s*(dof|axis|axes|degrees)", t)
    if m:
        n = {"four": 4, "five": 5, "six": 6}.get(m.group(1), None) or int(m.group(1))
        ch["arm.dof"] = n
        said.append(f"{n}-DOF arm")
    m = re.search(r"reach (?:to |of )?(\d{3})\s*mm", t) or re.search(r"(\d{3})\s*mm reach", t)
    if m:
        ch["arm.reach_mm"] = float(m.group(1))
        said.append(f"reach → {m.group(1)} mm")
    elif re.search(r"(longer|more|bigger|increase) reach", t):
        ch["arm.reach_mm"] = d.arm.reach_mm + 60
        said.append(f"reach → {d.arm.reach_mm + 60:g} mm")
    elif re.search(r"(shorter|less|smaller|reduce) reach|more compact", t):
        ch["arm.reach_mm"] = max(d.arm.reach_mm - 60, 200)
        said.append(f"reach → {max(d.arm.reach_mm - 60, 200):g} mm")
    if re.search(r"custom pcb|custom board|pcba|single board|one board", t):
        ch["electronics.config"] = "custom_pcb"
        said.append("electronics → one custom controller PCBA")
    elif re.search(r"off[- ]the[- ]shelf|modules|dev ?boards|breakout", t):
        ch["electronics.config"] = "modules"
        said.append("electronics → off-the-shelf modules")
    extras = [e for e in d.electronics.extras.split(",") if e]
    for pat, key in ((r"camera", "wrist_camera"), (r"e-?stop|emergency stop|kill switch", "estop"), (r"\bimu\b", "imu")):
        if re.search(pat, t):
            if re.search(r"(remove|no|without|drop) (the |a )?(wrist )?" + pat, t):
                extras = [e for e in extras if e != key]
                said.append(f"removed {key.replace('_', ' ')}")
            elif key not in extras:
                extras.append(key)
                said.append(f"added {key.replace('_', ' ')}")
    if ",".join(extras) != d.electronics.extras:
        ch["electronics.extras"] = ",".join(extras)
    if re.search(r"\bpilot\b|cnc|sheet[- ]metal|aluminum|aluminium|machined", t):
        ch["build.config"] = "pilot"
        said.append("build → pilot (CNC aluminium + sheet metal + PCBA)")
    elif re.search(r"production|injection|molded|moulded|mass produc", t):
        ch["build.config"] = "production"
        said.append("build → production (molded covers + sheet metal + PCBA)")
    elif re.search(r"prototype|3d[- ]?print", t):
        ch["build.config"] = "prototype"
        said.append("build → prototype (3D printed)")
    m = re.search(r"(\d+)\s*(units|robots|pcs|pieces)", t)
    if m:
        ch["build.quantity"] = int(m.group(1))
        said.append(f"quantity → {m.group(1)}")


def rules(msg: str, d: Design) -> tuple[dict, list]:
    t = normalize_units(msg.lower())
    ch, said = {}, []
    if re.match(r"\s*(?:can|could|will|would|does|do|is|are)\s+(?:it|this|that|the (?:robot|arm|gripper|design))\b", t) \
            and t.rstrip().endswith("?"):
        return ch, said  # "can it lift 2 kg?" asks about the design; it is not an edit
    robot_rules(t, d, ch, said)
    key = object_alias(t)
    if key:
        ch.update(object_changes(key))
        o = OBJECTS[key]
        said.append(f"object → {key} (Ø{o['diameter_mm']} mm, {o['mass_g']} g"
                    + (f", damages above ~{o['max_force_n']} N)" if o["max_force_n"] else ")"))
    else:
        m = re.search(r"(?:ø|⌀|diameter(?: of)?|dia\.?)\s*(\d+(?:\.\d+)?)\s*mm|(\d+(?:\.\d+)?)\s*mm\s*(?:wide|across|"
                      r"diameter|dia\b|thick|ball|sphere|cube|box|block|brick|cylinder|tube|object)", t)
        if m and not re.search(r"reach|finger|hole|mount|spacing|longer|shorter", t[max(0, m.start() - 24):m.end()]):
            dia = float(m.group(1) or m.group(2))
            ch.update({"object.diameter_mm": dia, **fit_finger({"diameter_mm": dia})})
            said.append(f"object width → {dia:g} mm")
            shape = next((s for pat, s in ((r"\b(?:box|cube|block|brick)", "box"), (r"\b(?:ball|sphere)", "sphere"),
                                           (r"\b(?:tube|cylinder|rod|bottle|can)s?\b", "cylinder")) if re.search(pat, t)), None)
            if shape:
                ch["object.shape"] = shape
                if shape == "box":
                    ch["finger.pad"] = "flat"
                said.append(f"object shape → {shape}")
    for s in ("sg90", "mg90s", "mg996r", "ds3218"):
        if s in t.replace(" ", "").replace("-", "") and ("gripper" in t or "arm.actuator_family" not in ch):
            ch["electronics.servo"] = s
            said.append(f"servo → {s.upper()}")
    for words, mcu in ((("esp32-s3", "esp32 s3", "esp32s3"), "esp32_s3_devkit"), (("esp32",), "esp32_devkit"), (("pico", "rp2040"), "rp2040_pico"),
                       (("nano", "arduino"), "arduino_nano")):
        if any(w in t for w in words):
            ch["electronics.mcu"] = mcu
            said.append(f"MCU → {mcu}")
            break
    if "build.config" in ch:
        pass
    elif "laser" in t:
        ch.update({"fab.process": "laser_cut", "finger.material": "Delrin", "finger.width_mm": 6.0, "finger.sensor": "none"})
        said.append("fab → laser-cut 6 mm Delrin (the FSR needs a 20 mm printed finger, so the sensor is dropped)")
    elif "cnc" in t or "alumin" in t:
        ch.update({"fab.process": "cnc_router", "finger.material": "Al6061", "finger.width_mm": 6.0, "finger.sensor": "none"})
        said.append("fab → CNC 6 mm aluminium (sensor dropped: needs a 20 mm printed finger)")
    elif re.search(r"\b(print|petg|pla|nylon)\b", t):
        mat = "PLA" if "pla" in t.split() else "Nylon-CF" if "nylon" in t else "PETG"
        ch.update({"fab.process": "fdm_print", "finger.material": mat})
        said.append(f"fab → 3D print {mat}")
    if re.search(r"(no|remove|without|drop)( the)? (force )?sensor", t):
        ch["finger.sensor"] = "none"
        said.append("force sensor removed")
    elif re.search(r"(add|with|use)( a)? force sensor|force feedback|fsr", t):
        ch.update({"finger.sensor": "fsr402", "finger.width_mm": 20.0, "fab.process": "fdm_print",
                   "finger.material": ch.get("finger.material", d.finger.material) if d.finger.material in ("PLA", "PETG", "Nylon-CF") else "PETG"})
        said.append("FSR 402 force sensor added (20 mm printed finger)")
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*(longer|shorter)", t)
    if m:
        dl = float(m.group(1)) * (1 if m.group(2) == "longer" else -1)
        ch["finger.length_mm"] = round(ch.get("finger.length_mm", d.finger.length_mm) + dl, 1)
        said.append(f"fingers {m.group(2)} by {m.group(1)} mm")
    elif re.search(r"longer fingers?", t):
        ch["finger.length_mm"] = round(ch.get("finger.length_mm", d.finger.length_mm) + 10, 1)
        said.append("fingers 10 mm longer")
    if re.search(r"thicker finger|stiffer finger|stronger finger", t):
        ch["finger.thickness_mm"] = round(d.finger.thickness_mm + 1.5, 1)
        said.append("finger beam 1.5 mm thicker")
    elif re.search(r"thinner finger|lighter finger", t):
        ch["finger.thickness_mm"] = round(max(d.finger.thickness_mm - 1.0, 3.0), 1)
        said.append("finger beam 1 mm thinner")
    for words, pad in ((("v-groove", "v groove", "vee"), "v"), (("flat pad", "flat finger", "flat"), "flat"),
                       (("contour", "curved", "conform"), "contour")):
        if any(w in t for w in words):
            ch["finger.pad"] = pad
            said.append(f"pad → {pad}")
            break
    m = re.search(r"(\d+(?:\.\d+)?)\s*n\b", t)
    if m and re.search(r"grip|force|target|squeeze", t):
        ch["electronics.target_force_n"] = float(m.group(1))
        said.append(f"grip target → {m.group(1)} N")
    elif re.search(r"gentler|softer|lighter grip|less force", t):
        ch["electronics.target_force_n"] = 0.0
        ch["electronics.target_force_n"] = round(_target(d) * 0.7, 2)
        said.append("grip 30% gentler")
    elif re.search(r"firmer|tighter|stronger grip|more force", t):
        ch["electronics.target_force_n"] = round(_target(d) * 1.4, 2)
        said.append("grip 40% firmer")
    m = re.search(r"(\d+(?:\.\d+)?)\s*v\b", t)
    if m:
        ch["electronics.supply_v"] = float(m.group(1))
        said.append(f"servo supply → {m.group(1)} V")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(g|grams|kg)\b", t)
    if m and "object.mass_g" not in ch:
        ch["object.mass_g"] = float(m.group(1)) * (1000 if m.group(2) == "kg" else 1)
        said.append(f"object mass → {ch['object.mass_g']:g} g")
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*(hole|mount|spacing)", t)
    if m:
        ch["finger.mount_hole_spacing_mm"] = float(m.group(1))
        said.append(f"mount hole spacing → {m.group(1)} mm")
    return ch, said


_LAST_TARGET = {}


def _target(d: Design) -> float:
    return d.electronics.target_force_n or _LAST_TARGET.get("value", 2.0)


TOOL = {
    "name": "apply_design_changes",
    "description": "Apply parameter changes to the gripper design. Every key must be one of the dotted keys in "
                   "the current design JSON. Keep changes minimal but complete: when the object changes, also set "
                   "its shape, diameter_mm, length_mm, mass_g, friction and max_force_n (per-finger squeeze that "
                   "damages it, 0 if robust), and resize finger.length_mm (~radius+30), finger.pad_height_mm and "
                   "finger.pad_depth_mm (~radius/4). A force sensor (finger.sensor=fsr402) needs finger.width_mm=20 "
                   "and fab.process=fdm_print. Allowed enums: finger.pad flat|v|contour; finger.sensor none|fsr402; "
                   "finger.material PLA|PETG|Nylon-CF|Acrylic|Delrin|HDPE|Al6061; fab.process "
                   "auto|fdm_print|laser_cut|cnc_router; electronics.mcu auto|arduino_nano|esp32_devkit|rp2040_pico; "
                   "electronics.servo auto|sg90|mg90s|mg996r|ds3218 (the GRIPPER servo); electronics.config modules|custom_pcb; "
                   "electronics.extras comma list of wrist_camera,estop,imu; arm.dof 4|5|6; arm.reach_mm 200-500; "
                   "arm.actuator_family hobby_pwm|smart_bus|dynamixel; arm.actuator_overrides 'j2=xm430_w350'; "
                   "build.config prototype|pilot|production; build.quantity; task.pick_x_mm/pick_y_mm/place_x_mm/place_y_mm.",
    "input_schema": {
        "type": "object",
        "properties": {
            "changes": {"type": "object", "description": "dotted key -> new value",
                        "additionalProperties": {"type": ["number", "string"]}},
            "reply": {"type": "string", "description": "one or two sentences to the engineer: what you changed and "
                                                        "what they should expect to ripple through"},
        },
        "required": ["changes", "reply"],
    },
}


def interpret(msg: str, d: Design, last_target: float | None = None) -> dict:
    if last_target:
        _LAST_TARGET["value"] = last_target
    try:
        import anthropic

        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=4000, output_config={"effort": "low"},
            system="You are Morph's design assistant for a whole robot: a robot arm with a force-sensing parallel "
                   "gripper, its electronics and its manufacturing. Turn each request into a call to "
                   "apply_design_changes. Always call the tool exactly once.",
            tools=[TOOL], tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": f"Current design:\n{json.dumps(d.to_dict())}\n\nRequest: {msg}"}],
        )
        if resp.stop_reason != "refusal":
            for block in resp.content:
                if block.type == "tool_use" and block.name == TOOL["name"]:
                    return {"changes": block.input.get("changes", {}), "reply": block.input.get("reply", ""),
                            "source": "claude"}
    except Exception:
        pass
    ch, said = rules(msg, d)
    if not ch:
        return {"changes": {}, "source": "rules",
                "reply": "I couldn't map that to a design parameter. Try: “swap to Dynamixel servos”, “use a custom PCB”, "
                         "“make it 6-DOF”, “add a wrist camera”, “pilot build”, “reach 380 mm”, “make it work for a strawberry”."}
    return {"changes": ch, "reply": "Changing " + "; ".join(said) + ".", "source": "rules"}
