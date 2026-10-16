"""Chat message -> design changes.

Claude (tool use) when credentials exist; a deterministic rule parser otherwise,
so the live demo never depends on the network. Either way the result is a dict
of dotted-key changes that the pipeline validates.
"""

from __future__ import annotations

import contextvars
import json
import re

from ..spec import Design

MODEL = "claude-opus-5-5"
# the project name, robot source and world mode's room come from the user, never from chat
CHAT_FORBIDDEN = {"name", "arm.source", "world.id", "world.sha"}

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


_UNIT_AHEAD = (r"(?=\s*-?\s*(?:kgs?|kilos?|kilograms?|lbs?|pounds?|oz|ounces?|grams?|gr|gms?|g|cm|centimet(?:er|re)s?|mm|"
               r"millimet(?:er|re)s?|inch(?:es)?|in)\b)")
_ONES = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                    "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i > 1}
_NUM_WORD = r"(?:" + "|".join(list(_ONES) + list(_TENS)) + r"|hundred|thousand)"


def _words_value(words: str) -> float | None:
    """'three hundred' -> 300, 'twenty-five' -> 25, 'two thousand five hundred' -> 2500."""
    total, part = 0, 0
    for w in re.split(r"[\s-]+(?:and[\s-]+)?", words.strip()):
        if w in _ONES:
            part += _ONES[w]
        elif w in _TENS:
            part += _TENS[w]
        elif w == "hundred":
            part = (part or 1) * 100
        elif w == "thousand":
            total, part = total + (part or 1) * 1000, 0
        else:
            return None
    return float(total + part)


def _plain_numbers(text: str) -> str:
    """Numbers before a unit written so the unit rules read them: thousands separators ("1,050 g", "1 000 g", and
    "1.000 g" for grams, where three decimals of a gram would be odd), number words ("three hundred grams", "two-inch"),
    "half a kilo", "a pound" and a hyphen or none between the number and its unit ("300-gram", "50mm-wide")."""
    sep = r"(?:,|[\u00a0\u202f\u2009])"
    text = re.sub(r"(?<![\d.,])(\d{1,3})((?:" + sep + r"\d{3})+)(?![\d,]|\.\d)" + _UNIT_AHEAD,
                  lambda m: m.group(1) + re.sub(r"\D", "", m.group(2)), text)
    text = re.sub(r"(?<![\d.,])(\d{1,3})((?: 000)+)(?![\d,]|\.\d)" + _UNIT_AHEAD, lambda m: m.group(1) + m.group(2).replace(" ", ""),
                  text)                                                            # "1 000 g"; never "3 100 g bags"
    text = re.sub(r"(?<![\d.,])([1-9]\d{0,2})((?:\.000)+)(?![\d,]|\.\d)(?=\s*(?:grams?|gr|gms?|g)\b)",
                  lambda m: m.group(1) + m.group(2).replace(".", ""), text)
    text = re.sub(r"(?<![\w.])\.(\d+)" + _UNIT_AHEAD, r"0.\1", text)                     # ".3 kg"
    text = re.sub(r"\bhalf\s+an?\s+(?=(?:kilo|kilogram|pound|lb)s?\b)", "0.5 ", text)
    text = re.sub(r"\b(?:a|an|one)\s+(?=(?:kilo|kilogram|pound|ounce|inch)\b)", "1 ", text)
    text = re.sub(r"\b(" + _NUM_WORD + r"(?:[\s-]+(?:and[\s-]+)?" + _NUM_WORD + r"){0,7})" + _UNIT_AHEAD,
                  lambda m: f"{_words_value(m.group(1)):g}" if _words_value(m.group(1)) is not None else m.group(1), text)
    text = re.sub(r"(\d)\s*-\s*(?=(?:kgs?|kilos?|kilograms?|lbs?|pounds?|oz|ounces?|grams?|gms?|g|cm|centimet(?:er|re)s?|mm|"
                  r"millimet(?:er|re)s?|inch(?:es)?)\b)", r"\1 ", text)
    text = re.sub(r"\b(\d+(?:\.\d+)?)\s*gms?\b", r"\1 g", text)
    return re.sub(r"(\d\s*(?:mm|cm|in|inch(?:es)?))-(?=(?:wide|across|thick|diameter|long|high|tall|deep)\b)", r"\1 ", text)


def normalize_units(text: str) -> str:
    """'38 cm' -> '380 mm', '2 kg' -> '2000 g', '1.5 lb' -> '680 g', '2 in wide' -> '51 mm wide' (text is lower case),
    after _plain_numbers ("1,050 g" -> "1050 g", "half a kilo" -> "500 g", "two-inch" -> "50.8 mm")."""
    text = _plain_numbers(text)
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


NOTE = "~"  # a `said` entry that changes nothing ("it already has an emergency stop"): the reply says so, apart
CHEAPER = re.compile(r"\b(cheaper|cheapest|less expensive|cost less|lower (?:the )?(?:cost|price)|(?:reduce|cut|lower) (?:the )?"
                     r"(?:parts )?(?:cost|price)s?|save money|costs? down)\b")


def _hobby_grips(d: Design) -> bool:
    """Whether hobby PWM servos would still grip the object safely: their gripper servo has no torque limit, so a
    force-sensing close may not reach the object's small end, and a sensorless one may not hold it or keep every size of
    it below stall (the plan's hobby_gripper issues, brief.classify)."""
    from .brief import classify, preflight

    return "hobby_gripper" not in classify(preflight({**d.flat(), "arm.actuator_family": "hobby_pwm"})["issues"])


def cost_down(d: Design, ch: dict, said: list):
    """"Make it cheaper": one step down the cost ladder that still lifts the object with a 2x torque margin
    (capability.PAYLOAD_G): Dynamixel to smart-bus servos, then one joint fewer, then hobby PWM servos when they still
    grip it safely (_hobby_grips)."""
    from .capability import payload_g

    if d.arm.source:
        said.append(f"{NOTE}an imported arm keeps its own servos; a Morph-generated arm can be made cheaper")
        return
    fam, dof, g = d.arm.actuator_family, int(d.arm.dof), float(d.object.mass_g)
    if fam == "dynamixel" and payload_g("smart_bus", dof) >= g:
        ch["arm.actuator_family"] = "smart_bus"
        said.append(f"actuators → smart bus (Feetech STS3215, far cheaper than Dynamixel; they still lift the {g:g} g object "
                    "with a 2x margin)")
    elif dof > 4 and payload_g(fam, dof - 1) >= g:
        ch["arm.dof"] = dof - 1
        said.append(f"{dof - 1}-DOF arm (one servo fewer; top-down picks need only 4 joints)")
    elif fam == "smart_bus" and payload_g("hobby_pwm", dof) >= g and _hobby_grips(d):
        ch["arm.actuator_family"] = "hobby_pwm"
        said.append("actuators → hobby PWM servos (the cheapest; they have no position feedback)")
    elif fam == "smart_bus" and payload_g("hobby_pwm", dof) >= g:
        said.append(f"{NOTE}this is already the cheapest arm Morph makes that grips the {d.object.name or 'object'} safely "
                    f"({dof} joints, smart-bus servos): a hobby PWM gripper servo has no torque limit to back its grip up")
    else:
        label = {"hobby_pwm": "hobby PWM", "smart_bus": "smart-bus", "dynamixel": "Dynamixel"}.get(fam, fam)
        said.append(f"{NOTE}this is already the cheapest arm Morph makes for a {g:g} g object ({dof} joints, {label} servos)")


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
        from .labels import show_value

        ch["arm.actuator_family"] = fam
        said.append(f"actuators → {show_value('arm.actuator_family', fam)}")
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
    if CHEAPER.search(t) and not fam and "arm.dof" not in ch:
        cost_down(d, ch, said)
    pace = (re.search(r"\b(faster|quicker|speed (?:it )?up|more per (?:minute|hour))\b(?! (?:servos?|motors?|actuators?))", t)
            and -1) or (re.search(r"\b(slower|more slowly|slow (?:it )?down)\b", t) and 1)
    if pace:
        from .capability import CYCLE_S

        cur = float(d.task.cycle_time_s)
        new = round(min(max(cur * (0.75 if pace < 0 else 1.33), CYCLE_S[0]), CYCLE_S[1]), 1)
        if new != cur:
            ch["task.cycle_time_s"] = new
            said.append(f"cycle time → {new:g} s per pick and place (was {cur:g} s)")
        else:
            said.append(f"{NOTE}one pick and place every {cur:g} s is already the "
                        f"{'fastest' if pace < 0 else 'slowest'} these arms plan for")
    if re.search(r"custom pcb|custom board|pcba|single board|one board", t):
        ch["electronics.config"] = "custom_pcb"
        said.append("electronics → one custom controller PCBA")
    elif re.search(r"off[- ]the[- ]shelf|modules|dev ?boards|breakout", t):
        ch["electronics.config"] = "modules"
        said.append("electronics → off-the-shelf modules")
    extras = [e for e in d.electronics.extras.split(",") if e]
    names = {"wrist_camera": ("a", "wrist camera"), "estop": ("an", "emergency stop"), "imu": ("an", "IMU")}
    for pat, key in ((r"camera", "wrist_camera"), (r"e-?stop|emergency stop|kill switch", "estop"), (r"\bimu\b", "imu")):
        if re.search(pat, t):
            if re.search(r"(remove|no|without|drop) (the |a )?(wrist )?" + pat, t):
                if key in extras:
                    extras = [e for e in extras if e != key]
                    said.append(f"removed the {names[key][1]}")
                else:
                    said.append(f"{NOTE}it has no {names[key][1]}")
            elif key not in extras:
                extras.append(key)
                said.append(f"added {' '.join(names[key])}")
            else:
                said.append(f"{NOTE}it already has {' '.join(names[key])}")
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
        said.append("removed the force sensor")
    elif re.search(r"(add|with|use)( a)? force sensor|force feedback|fsr", t) and d.arm.source:
        said.append(f"{NOTE}this robot keeps its stock jaws, which have no place for force sensors; its gripper servo's torque "
                    "limit sets the grip instead")
    elif re.search(r"(add|with|use)( a)? force sensor|force feedback|fsr", t):
        ch.update({"finger.sensor": "fsr402", "finger.width_mm": 20.0, "fab.process": "fdm_print",
                   "finger.material": ch.get("finger.material", d.finger.material) if d.finger.material in ("PLA", "PETG", "Nylon-CF") else "PETG"})
        said.append("added an FSR 402 force sensor (20 mm printed finger)")
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
    cells = CELLS.search(t)     # "a 2S LiPo": 3.7 V a cell
    v = float(m.group(1)) if m else round(3.7 * int(cells.group(1)), 1) if cells else None
    if v is not None:
        why = rail_refusal(d.with_changes(ch), v)
        if why:
            said.append(NOTE + why)
        else:
            ch["electronics.servo_v"] = v
            said.append(f"servo supply → {v:g} V")
    elif re.search(r"\b(?:rated|default|auto(?:matic)?)\s+(?:servo\s+)?(?:supply|voltage|rail)\b", t):
        ch["electronics.servo_v"] = 0.0
        said.append("servo supply → the servos' rated voltage")
    if BATTERY.search(t):       # never a mains supply built in silence for a battery request
        said.append(NOTE + battery_note(d.with_changes(ch), v))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(g|grams|kg)\b", t)
    if m and "object.mass_g" not in ch:
        ch["object.mass_g"] = float(m.group(1)) * (1000 if m.group(2) == "kg" else 1)
        said.append(f"object mass → {ch['object.mass_g']:g} g")
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*(hole|mount|spacing)", t)
    if m:
        ch["finger.mount_hole_spacing_mm"] = float(m.group(1))
        said.append(f"mount hole spacing → {m.group(1)} mm")
    return ch, said


# A battery request. Morph plans mains power only (its supply catalog has no battery), so the reply says what the robot is
# built with instead, and whether a charged lithium pack of that voltage would be too much for its servos.
BATTERY = re.compile(r"\bbatter(?:y|ies)\b|\bli-?pos?\b|\bli-?ion\b|\b18650s?\b|\bcordless\b|\b[1-6]\s*s\s+(?:pack|li-?po|li-?ion)")
CELLS = re.compile(r"\b([1-6])\s*s\s+(?:pack|li-?po|li-?ion|batter)")


def battery_note(d: Design, v: float | None = None) -> str:
    """What a request to run the robot from a battery builds: the mains supply (and servo buck) its servo rail gets, and,
    for a lithium pack's voltage (3.7 V a cell), the servos whose maximum its full charge (4.2 V a cell) exceeds."""
    from ..robot import arm, electronics2, imported
    from ..robot.catalog import ACTUATORS

    plan = "Morph builds robots for mains power, not batteries"
    try:
        sz = imported.size(d) if d.arm.source else arm.size(d)
        sup = electronics2.plan(d, sz)["supply"]
    except Exception:  # noqa: BLE001  (the build will say what is wrong with the design)
        return f"{plan}: it plans no battery, charger or low-voltage cutoff"
    built = f"a {sup['v']:g} V {sup['a']:g} A mains supply" + (f" and the servo buck set to {sup['servo_v']:g} V"
                                                               if sup["servo_buck"] else " straight to its servos")
    out = f"{plan}, so this one gets {built}, with no battery, charger or low-voltage cutoff"
    n = round(v / 3.7) if v else 0
    if n and abs(v - 3.7 * n) <= 0.15:
        keys = list(dict.fromkeys(a["key"] for a in electronics2.rail_acts(sz) if a["key"] in ACTUATORS))
        over = [k for k in keys if ACTUATORS[k]["voltage"][1] < 4.2 * n - 1e-9]
        if over:
            out += (f"; a charged {n}S lithium pack reads {4.2 * n:.1f} V, above the "
                    + ", ".join(f"{k.upper().replace('_', '-')}'s {ACTUATORS[k]['voltage'][1]:g} V" for k in over)
                    + " maximum, so don't wire one straight to the servos")
    return out


def rail_refusal(d: Design, v: float) -> str | None:
    """Why the robot's servos can't run from a `v` volt supply (one a servo's voltage range leaves out), or None: then
    the build puts the servo rail at `v` (electronics.servo_v)."""
    from ..robot import arm, electronics2, imported
    from ..robot.catalog import ACTUATORS

    try:
        sz = imported.size(d) if d.arm.source else arm.size(d)
    except Exception:  # noqa: BLE001  (the build will say what is wrong with the design)
        return None
    keys = list(dict.fromkeys(a["key"] for a in electronics2.rail_acts(sz) if a["key"] in ACTUATORS))
    bad = [k for k in keys if not ACTUATORS[k]["voltage"][0] <= v <= ACTUATORS[k]["voltage"][1]]
    if not bad:
        return None
    ranges = ", ".join(f"the {k.upper().replace('_', '-')} takes {ACTUATORS[k]['voltage'][0]:g}-{ACTUATORS[k]['voltage'][1]:g} V"
                       for k in bad)
    now = (sz.get("servo_rail") or {}).get("v")
    return f"{ranges}, so a {v:g} V supply can't run it" + (f"; the servo supply stays at {now:g} V" if now else "")


# The grip target (N) that "gentler" and "firmer" scale when the design sets none. It is the room's own: interpret()
# gets it with each message and sets it only while that call runs, so rooms (and their threads) never share it.
_LAST_TARGET = contextvars.ContextVar("last_grip_target", default=None)


def _target(d: Design) -> float:
    return d.electronics.target_force_n or _LAST_TARGET.get() or 2.0


TOOL = {
    "name": "apply_design_changes",
    "description": "Apply parameter changes to the gripper design. Every key must be one of the dotted keys in "
                   "the current design JSON, except name, arm.source and world.*, which never change here. "
                   "Keep changes minimal but complete: when the object changes, also set "
                   "its shape, diameter_mm, length_mm, mass_g, friction and max_force_n (per-finger squeeze that "
                   "damages it, 0 if robust), and resize finger.length_mm (~radius+30), finger.pad_height_mm and "
                   "finger.pad_depth_mm (~radius/4). A force sensor (finger.sensor=fsr402) needs finger.width_mm=20 "
                   "and fab.process=fdm_print. Allowed enums: finger.pad flat|v|contour; finger.sensor none|fsr402; "
                   "finger.material PLA|PETG|Nylon-CF|Acrylic|Delrin|HDPE|Al6061; fab.process "
                   "auto|fdm_print|laser_cut|cnc_router; electronics.mcu auto|arduino_nano|esp32_devkit|rp2040_pico; "
                   "electronics.servo auto|sg90|mg90s|mg996r|ds3218 (the GRIPPER servo); electronics.config modules|custom_pcb; "
                   "electronics.extras comma list of wrist_camera,estop,imu; arm.dof 4|5|6; arm.reach_mm 200-500; "
                   "arm.actuator_family hobby_pwm|smart_bus|dynamixel; arm.actuator_overrides 'j2=xm430_w350'; "
                   "build.config prototype|pilot|production; build.quantity; task.pick_x_mm/pick_y_mm/place_x_mm/place_y_mm; "
                   "electronics.servo_v the servo supply in volts (0 = the servos' rated voltage; electronics.supply_v "
                   "does nothing on a robot arm). Morph plans mains power only: for a battery, set electronics.servo_v to "
                   "its voltage; Morph adds to your reply what the robot is built with.",
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


def _from_tool(args) -> dict:
    """Claude's tool call as {changes, reply}: only plain values, and never the keys chat may not touch."""
    args = args if isinstance(args, dict) else {}
    raw = args.get("changes") if isinstance(args.get("changes"), dict) else {}
    changes = {k: v for k, v in raw.items() if isinstance(k, str) and k not in CHAT_FORBIDDEN
               and isinstance(v, (str, int, float)) and not isinstance(v, bool)}
    reply = str(args.get("reply") or "")
    if any(k in CHAT_FORBIDDEN for k in raw):
        reply = (reply + " (The project name, robot source and world stay as they are: chat can't change them.)").strip()
    return {"changes": changes, "reply": reply, "source": "claude"}


def interpret(msg: str, d: Design, last_target: float | None = None) -> dict:
    """{changes, reply, source, last_target} for a chat message. `last_target` is the room's grip target (N); the
    result's `last_target` is the target after this message, for the room to pass in with its next one."""
    token = _LAST_TARGET.set(last_target or None)
    try:
        out = _interpret(msg, d)
    finally:
        _LAST_TARGET.reset(token)
    new = out["changes"].get("electronics.target_force_n")
    out["last_target"] = new if isinstance(new, (int, float)) and not isinstance(new, bool) and new > 0 else last_target
    return out


def _interpret(msg: str, d: Design) -> dict:
    from .. import settings

    try:
        if not settings.network_allowed():
            raise RuntimeError("network lookups are off")
        import anthropic

        client = anthropic.Anthropic(timeout=30, max_retries=1)  # a slow answer holds the room's queue a minute at most
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
                    out = _from_tool(block.input)
                    if BATTERY.search(normalize_units(msg.lower())):   # what is built, whatever the reply says
                        try:
                            nd = d.with_changes(out["changes"])
                        except (KeyError, TypeError, ValueError):
                            nd = d
                        note = battery_note(nd, nd.electronics.servo_v or None)
                        out["reply"] = f"{out['reply']} ({note}.)".strip()
                    return out
    except Exception:
        pass
    ch, said = rules(msg, d)
    notes = [s[len(NOTE):] for s in said if s.startswith(NOTE)]
    said = [s for s in said if not s.startswith(NOTE)]
    if not ch:
        t = " ".join(msg.lower().split())
        if notes:
            reply = "Nothing to change: " + "; ".join(notes) + "."
        elif t.endswith("?") or re.match(r"(?:what|how|why|which|can|could|does|do|is|are|will|would)\b", t):
            reply = ("That reads like a question, so nothing was changed: without Claude, the chat only makes changes. The right "
                     "panel shows this design's payload, reach, cost and torque margins.")
        elif len(t.split()) <= 3 and re.match(r"(?:hi|hello|hey|thanks|thank you|thx|ok|okay|cool|great|nice|yes|no)\b", t):
            reply = "Ask for a change whenever you're ready, for example “make it cheaper” or “add a wrist camera”."
        else:
            reply = ("I couldn't map that to a design parameter. Try: “swap to Dynamixel servos”, “make it cheaper”, "
                     "“use a custom PCB”, “make it 6-DOF”, “add a wrist camera”, “pilot build”, “reach 380 mm”, "
                     "“make it work for a strawberry”.")
        return {"changes": {}, "source": "rules", "reply": reply}
    note, text = "; ".join(notes), "; ".join(said)
    text = text[:1].upper() + text[1:] if re.match(r"(?:added|removed) ", text) else "Changing " + text  # "Added a wrist camera"
    return {"changes": ch, "reply": text + "." + (f" ({note[:1].upper() + note[1:]}.)" if note else ""), "source": "rules"}
