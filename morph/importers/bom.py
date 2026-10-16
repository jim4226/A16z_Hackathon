"""Parts lists someone already has -> BOM rows Morph and Darwin understand.

  res = parse("README.md")       # Markdown tables, CSV, TSV or XLSX (read with the standard library)
  res["rows"][0] -> {"item": "STS3215 Servo 7.4V, 1/345 gear (C001)", "qty": 7, "unit_price": 13.89,
                     "ext_price": 97.23, "currency": "USD", "url": "https://...", "make_or_buy": "buy",
                     "category": "actuator", "catalog": "sts3215_7v4", ...}
  darwin_parts(res["rows"])      # [(item, Darwin query)] for sourcing

Headers are matched by synonym (Part/Item/Description, Qty/Amount, MPN/Part number, Unit Cost/Price, Buy/Link,
Ref/Designator, ...). With several price columns the US dollar one wins (the SO-ARM100 README lists US, EU, RMB
and JPY prices side by side). Total rows are skipped and recomputed.
"""

from __future__ import annotations

import csv
import io
import os
import re
import xml.etree.ElementTree as ET
import zipfile

from .common import MB, ImportFailed, check_xml_text

FIELDS = [  # matched in this order, so "Part number" becomes the MPN before "Part" can become the item
    ("mpn", r"\bmpn\b|mfr\.?\s*(part|p/?n|#)|manufacturer\s*part|part\s*(number|no\.?|#)|\bp/?n\b|lcsc|digi-?key\s*(part|#)"),
    ("ref", r"^(ref|refs|reference|references|designator|designators)\b"),
    ("ext_price", r"ext(ended)?\.?\s*(price|cost)|line\s*total|sub-?total|total\s*(price|cost)"),
    ("unit_price", r"unit\s*(cost|price)|^price|^cost|price\s*\(|cost\s*\(|\bprice\b|\bcost\b|^(einzel)?preis\b"),
    ("qty", r"^(qty|qnty|q\.ty|quantity|amount|count|pcs|q'?ty|number|no\.?|#|units|menge|anzahl|stk|st\u00fcck)\b"),
    ("url", r"\b(buy|link|url|where\s*to\s*buy|purchase|shop|store\s*link|source)\b"),
    ("manufacturer", r"^(manufacturer|mfr|mfg|brand|maker)\b"),
    ("supplier", r"^(supplier|vendor|distributor|seller|store)\b"),
    ("item", r"^(part|parts|item|items|component|components|name|product|designation|material|value|bezeichnung|"
             r"artikel|benennung|teil)\b"),
    ("description", r"^(description|desc\.?|details|notes?|comments?|specs?|specification|beschreibung|bemerkung)\b"),  # the part name if there is no other
]
CURRENCY = {"$": "USD", "us$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP",
            "¥": "CNY", "￥": "CNY", "rmb": "CNY", "cny": "CNY", "jpy": "JPY", "yen": "JPY"}
HEAD_CUR = [(r"\(us\)|\busd\b|\bus\b|\$", "USD"), (r"\(eu\)|\beur\b|€", "EUR"), (r"\brmb\b|\bcny\b|\bcn\b", "CNY"),
            (r"\bjpy\b|\bjp\b|\byen\b", "JPY"), (r"\bgbp\b|£", "GBP")]
SKIP_ITEM = re.compile(r"^(total|totals|sum|subtotal|grand total)\b", re.I)


def _cell(s) -> tuple[str, str | None]:
    """Markdown/HTML cell -> (text, first link URL)."""
    s = "" if s is None else str(s)
    url = None
    m = re.search(r"\[([^\]]*)\]\((https?://[^)\s]+)\)", s)
    if m:
        url = m.group(2)
    else:
        m2 = re.search(r"https?://[^\s|)\"'<>]+", s)
        url = m2.group(0) if m2 else None
    s = re.sub(r"<sup>.*?</sup>", "", s, flags=re.S | re.I)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"<[^>]+>|\*\*|__|`", "", s)
    return re.sub(r"\s+", " ", s).strip(), url


def _money(s: str, default_cur: str | None = None) -> tuple[float | None, str | None]:
    t = (s or "").strip()
    if not t or re.fullmatch(r"[-–—/]+|n/?a|tbd|\?", t, re.I):
        return None, None
    cur = None
    for sym, code in CURRENCY.items():
        if sym in t.lower():
            cur = code
            break
    m = re.search(r"(\d[\d,.\s]*)", t)
    if not m:
        return None, cur or default_cur
    num = m.group(1).strip().replace(" ", "")
    if re.fullmatch(r"\d+,\d{1,2}", num):          # decimal comma: 12,20
        num = num.replace(",", ".")
    num = num.replace(",", "")
    try:
        return round(float(num), 4), cur or default_cur
    except ValueError:
        return None, cur or default_cur


def _qty(s: str) -> int | None:
    m = re.search(r"\d+(\.\d+)?", s or "")
    if not m:
        return None
    try:
        return max(int(round(float(m.group(0)))), 0)
    except ValueError:
        return None


# what a line is comes from its part name's head: the first noun phrase, before what the part comes with, is
# described by or fits: "(", ",", ";", "+", a dash between words or an unspaced em or en dash, a "/" right after the
# motor word ("STS3215 servo/metal case"), "with", "w/", "incl.", "included", "built-in", and the fits-what clauses
# ("for", "compatible (with)", "fits", "suits", "suitable for"): a model named there is what the part fits, never it
_CLAUSE = re.compile(r"[(\[,;+]|\s[-–—]+\s|[–—]|\bw/|\b(?:with|incl|including|includes|included|"
                     r"built[- ]?in|for|compatible|fits?|suits?|suitable|to\s+fit|to\s+suit)\b", re.I)
_FITS = re.compile(r"\b(?:for|compatible|fits?|suits?|suitable|to\s+fit|to\s+suit)\b", re.I)
_MOTOR_WORD = re.compile(r"(?:servo|motor|actuator|servomotor|stepper|gearmotor|bldc)s?", re.I)
# words that are never a line's head noun: units, colours, materials, revisions, "replacement", how many
_UNIT = re.compile(r"(?:mm|cm|m|in|inch|inches|ft|g|kg|deg|degree|degrees|awg|pin|pins|way)", re.I)
_COLOUR = re.compile(r"(?:black|white|silver|grey|gray|red|blue|green|yellow|orange|purple|pink|gold|golden|clear|"
                     r"transparent|translucent|natural)", re.I)
_MATERIAL = re.compile(r"(?:metal|aluminium|aluminum|alu|alloy|steel|stainless|brass|copper|titanium|plastic|nylon|"
                       r"carbon|abs|pom|cnc|anodized|anodised)", re.I)
_FILLER = re.compile(r"(?:replacement|spare|original|genuine|new|rev|revision|ver|version|type|model|single|double|"
                     r"style|edition|an?|the|of|and|or|in)", re.I)
_GROUP = re.compile(r"(?:kits?|sets?|packs?|pairs?|bundles?|pcs?|pieces?|lots?|units?)", re.I)
# besides its model, a motor's name holds its maker and what kind of motor it is ("Feetech STS3215 Serial Bus Servo
# Magnetic Encoder", "Dynamixel X-series XL430-W250-T", "FEETECH STS3215 12V 30KG")
_BRAND = re.compile(r"(?:feetech|feetechrc|robotis|dynamixel|waveshare|towerpro|tower|pro|dsservo|ds|hitec|savox|"
                    r"futaba|emax|corona|miuzei|diymore|hiwonder|lobot|lewansoul)", re.I)
_MOTOR_BRAND = re.compile(r"(?:myactuator|cubemars|t-motor|steadywin|eaglepower)", re.I)   # they make only motors
_KIND = re.compile(r"(?:serial|bus|ttl|rs|smart|digital|analog|analogue|high|torque|high-torque|speed|high-speed|"
                   r"magnetic|encoder|encoding|coreless|brushless|micro|mini|standard|waterproof|continuous|rotation|"
                   r"programmable|intelligent|series|x-series|half-duplex|duplex|full|large|big|dual)", re.I)
# what a motor's listing says it is for, after the motor word: "MG996R Servo Motor RC Robot Arm" is a servo for a robot
# arm (the "arm" of a robot, not a servo arm), "MG996R Servo Motor Metal Gear Dual Ball Bearing" one with ball bearings
_USE = re.compile(r"(?:rc|robot|robots|robotic|robotics|car|cars|boat|boats|helicopter|helicopters|plane|planes|"
                  r"airplane|aircraft|drone|drones|truck|trucks|vehicle|crawler|arduino|raspberry|pi|diy|hobby|toy|"
                  r"toys)", re.I)
_USE_NOUN = {"robot": re.compile(r"arms?|hands?|grippers?|kits?", re.I), "ball": re.compile(r"bearings?", re.I)}
# a material's case or gears right before or after the motor word describe the motor: "Metal case STS3215 servo",
# "STS3215 Servo Motor Metal Case 12V", "MG996R Metal Gear Servo"
_BUILD = re.compile(r"(?:case|cases|housing|shell|gear|gears|geared)", re.I)
# a part for a motor named like one when its word comes before the motor word: "Power supply servo", "Cable servo"
ACCESSORY = re.compile(r"(?:horns?|brackets?|mounts?|mounting|holders?|plates?|covers?|cases?|adapters?|clips?|caps?|"
                       r"boards?|drivers?|controllers?|supply|supplies|psus?|screws?|bolts?|nuts?|washers?|cables?|"
                       r"wires?|leads?|harness(?:es)?|hubs?|extensions?|testers?|chargers?|batter(?:y|ies))", re.I)
_SPEC = re.compile(r"\d+(?:[.,]\d+)?(?:-\d+(?:[.,]\d+)?)?(?:v|vdc|volts?|kg(?:[.]?cm|f)?|n[·.]?m|ncm|m?a|mah|rpm|"
                   r"w|g|kv)", re.I)                                             # 12V, 4.8-7.2V, 30kg.cm, 55g, 340KV
# the joint or the arm a motor drives, named before its model: "Shoulder STS3215 12V", "Wrist pitch MG996R", "Koch
# follower XL430-W250-T", "Axis 1 MG996R" (a number or J1 is no plain word); "arm" only after whose arm it is ("Leader
# arm: 6x STS3215 7.4V"; a "servo arm" is a horn)
_JOINT = re.compile(r"(?:base|shoulder|elbow|wrist|forearm|pitch|roll|yaw|lift|pan|tilt|swivel|rotation|gripper|grip|"
                    r"jaws?|claw|leader|follower|master|joints?|axis|axes|koch|moss|aloha|lerobot|lekiwi)", re.I)
# a stand-in for a motor is no motor: "STS3215 dummy servo (3D printed spacer)"
_STAND_IN = re.compile(r"(?:dummy|dummies|fake|mock|mockup|mock-up|replica|placeholder|imitation)", re.I)
_ARM_OWNER = re.compile(r"(?:leader|follower|master|koch|moss|aloha|lerobot|lekiwi)", re.I)
# a label that names the joint before a ":" or a dash, then the part: "Base (J1): STS3215 12V", "Gripper - XL330-M288-T",
# "Joint 1 - STS3215 servo", "J1 - Feetech STS3215 12V", "Wrist roll — STS3215", "Base / shoulder: XL430-W250-T"
_LABEL = re.compile(r"^\s*(?P<label>[^:–—]{1,40}?)\s*(?::|\s[-–—]+\s|[–—])\s*(?P<rest>\S.*)$", re.S)
_PLAIN = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")
_MODEL_MARK = "§"
_COUNT = re.compile(r"\s*(?:#\s*\d+|[x×]\s*\d+|\d+\s*[x×])\s*$|^\s*\d+\s*[x×]\s*", re.I)   # "Servo #1", "6x Servo"
# reference designators of connectors, chips, passive parts and switches ("J2 J3", "U1"); M is a motor
_NOT_MOTOR_REF = re.compile(r"(?:j|p|cn|x|xj|u|ic|c|r|rn|l|d|led|q|f|fb|y|sw|k|bt|tp|h|mh|jp|t|ps|vr|z)", re.I)
# a count of motors in the name of a line bought once: "STS3215 12V servo (6 pcs)", "6 x STS3215 12V servo", "SG90
# micro servo 10 pack", "MG996R servo x4"
_PACK = re.compile(r"\((?:x\s*)?(\d+)\s*(?:pcs?|pieces?|x)?\)|\b(\d+)\s*(?:pcs?|pieces?)\b|^\s*(\d+)\s*[x×]\s|"
                   r"\s[x×]\s*(\d+)\s*$|\b(\d+)[\s-]*pack\b|\bpack\s+of\s+(\d+)\b", re.I)


def _joint_words(plain: list) -> bool:
    """Whether every plain word names a joint or whose arm it is (_JOINT; "arm" after _ARM_OWNER), or is ignorable."""
    for i, w in enumerate(plain):
        if _JOINT.fullmatch(w) or _ignorable(w) and not _GROUP.fullmatch(w):
            continue
        if w in ("arm", "arms") and i and _ARM_OWNER.fullmatch(plain[i - 1]):
            continue
        return False
    return True


def _is_label(text: str) -> bool:
    """Whether text only names a joint: joint words (_joint_words), numbers and joint references (J1, #2, 1-5), and no
    motor model ("SG90" is the servo, not a joint reference)."""
    from .autoprofile import MODEL_RX
    if MODEL_RX.search(text):
        return False
    words = [w.strip(".,;!?*\"'#") for w in re.split(r"[\s/()\[\],&+]+", text)]
    words = [w for w in words if w]
    if not words or not all(_PLAIN.fullmatch(w) or re.fullmatch(r"(?:[a-z]{1,2})?\d+(?:-(?:[a-z]{1,2})?\d+)?", w, re.I)
                            for w in words):
        return False
    return _joint_words([w.lower() for w in words if _PLAIN.fullmatch(w)])


def _unlabel(item: str) -> str:
    """A part name without the joint label before or after its ":" or dash (_LABEL, _is_label): "Base (J1): STS3215
    12V" and "STS3215 12V: base" are the STS3215 12 V, "Gripper - cable for SG90" a cable. "Servo - shoulder", "Horn -
    STS3215" and "SG90: 2 pcs" keep their heads."""
    m = _LABEL.match(item or "")
    if not m:
        return item
    if _is_label(m.group("label")):
        return m.group("rest")
    if _is_label(m.group("rest")):
        return m.group("label")
    return item


def _head(item: str) -> str:
    """The part name's first noun phrase (_CLAUSE): what the line is."""
    text = (item or "").strip()
    m = _CLAUSE.search(text)
    head = text[:m.start()] if m else text
    cut = head.find("/")
    if cut > 0 and head[:cut].split() and _MOTOR_WORD.fullmatch(head[:cut].split()[-1]):
        head = head[:cut]                              # "STS3215 servo/metal case"; "Wrist pitch/roll servo" stays
    return head.strip()


def _words(head: str) -> list:
    """The head's words, its catalog models (autoprofile.MODEL_RX) marked: [(word, kind)], kind "plain" (letters),
    "spec" (12V, 30kg.cm) or "other" (part numbers, the model mark)."""
    from .autoprofile import MODEL_RX
    head = MODEL_RX.sub(f" {_MODEL_MARK} ", head)
    head = re.sub(r"(\d)\s+(v|vdc|volts?|a|ma|mah|kg|w|g|rpm|kv)\b", r"\1\2", head, flags=re.I)
    out = []
    for w in re.split(r"[\s/]+", head):
        w = w.strip(".:,;!?*\"'()[]{}_")
        if not w:
            continue
        out.append((w.lower(), "plain" if _PLAIN.fullmatch(w) else "spec" if _SPEC.fullmatch(w) else "other"))
    return out


def _ignorable(w: str) -> bool:
    return bool(len(w) == 1 or _UNIT.fullmatch(w) or _COLOUR.fullmatch(w) or _MATERIAL.fullmatch(w)
                or _FILLER.fullmatch(w) or _GROUP.fullmatch(w))


def _is_motor_name(item: str) -> bool:
    """Whether a part name names a motor, read the way a reader does. Its head (_head) is a motor word with nothing
    after it but what kind of motor it is, a material's case or gears, specs, colours, units and a kit or set word
    ("STS3215 servo", "MG996R 55g Metal Gear Servo", "Feetech STS3215 Servo Motor 12V Metal Case", "STS3215 servo kit"),
    and no part for a motor before it ("Power supply servo" is a power supply); or, with no motor word, a motor's model
    with only its maker, specs and what kind of motor it is ("Feetech STS3215-C018", "Dynamixel X-series
    XL430-W250-T"). Whatever else the head names is the part: "STS3215 servo cable 200 mm" is a cable, "STS3215 servo
    horn aluminum" a horn, "XL430 frame FR12-S102K" a frame."""
    words = _words(_head(item))
    if not words or any(k == "plain" and _STAND_IN.fullmatch(w) for w, k in words):
        return False                                   # "STS3215 dummy servo (3D printed spacer)"
    motors = [i for i, (w, k) in enumerate(words) if k == "plain" and _MOTOR_WORD.fullmatch(w)]
    if not motors:
        model = next((i for i, (w, _) in enumerate(words) if w.startswith(_MODEL_MARK)), len(words))
        plain = [w for w, k in words if k == "plain"]
        # the joint the model drives may come before it ("Shoulder STS3215 12V", "Koch follower XL430-W250-T"), never
        # after ("STS3215 gripper") and never with a kit ("Pan tilt kit SG90" is a bracket kit)
        named = [w for w, k in words[:model] if k == "plain"
                 and not (_BRAND.fullmatch(w) or _MOTOR_BRAND.fullmatch(w) or _KIND.fullmatch(w) or _ignorable(w))]
        if named and (not _joint_words(named)
                      or any(_GROUP.fullmatch(w) for w, k in words[:model] if k == "plain")):
            return False
        if not all(_BRAND.fullmatch(w) or _MOTOR_BRAND.fullmatch(w) or _KIND.fullmatch(w) or _ignorable(w)
                   for w, k in words[model:] if k == "plain"):
            return False
        return model < len(words) or any(_MOTOR_BRAND.fullmatch(w) for w in plain)
    before = [w for w, k in words[:motors[0]] if k == "plain"]
    for i, w in enumerate(before):
        if ACCESSORY.fullmatch(w) and not (_BUILD.fullmatch(w) and i and _MATERIAL.fullmatch(before[i - 1])):
            return False
    after = []
    for w, k in words[motors[-1] + 1:]:
        if k == "spec":
            break                                      # "Servo Motor 12V Metal Case": what follows describes it
        if k == "plain":
            after.append(w)
    for i, w in enumerate(after):
        if _BUILD.fullmatch(w) and i and _MATERIAL.fullmatch(after[i - 1]):
            continue                                   # "Servo Metal Case", "Servo metal gears"
        prev = after[i - 1].rstrip("s").replace("robotic", "robot") if i else ""
        if prev in _USE_NOUN and _USE_NOUN[prev].fullmatch(w):
            continue                                   # "... RC Robot Arm", "... Dual Ball Bearing"
        if not (_KIND.fullmatch(w) or _BRAND.fullmatch(w) or _USE.fullmatch(w) or w == "ball" or _ignorable(w)):
            return False                               # the head noun is something else: "servo horn", "servo arm"
    return True


def _generic_motor(item: str) -> bool:
    """A motor named only by its kind or its maker, with no model ("Servo", "Bus servo", "Feetech TTL serial bus
    servo", "Wrist pitch/roll servo", "Servo motor, shoulder", "Servo - shoulder", "Servo #1", "Servo x6", "Servo (qty
    6)", "Dynamixel", "Feetech"): its description says which."""
    head = _COUNT.sub("", _head(item))
    words = _words(head)
    if not words or any(k != "plain" and not w.isdigit() for w, k in words):
        return False          # "Joint 1 servo"
    plain = [w for w, k in words if k == "plain"]
    if len(plain) <= 2 and all(_BRAND.fullmatch(w) for w in plain):
        return True
    return any(_MOTOR_WORD.fullmatch(w) for w in plain) and _is_motor_name(head)


def _joint_head(item: str) -> bool:
    """Whether a part name's head names only a joint (_joint_words; numbers and J1 too): "Base (MG996R)"."""
    words = _words(_COUNT.sub("", _head(item)))
    plain = [w for w, k in words if k == "plain"]
    return bool(plain) and not any(w.startswith(_MODEL_MARK) for w, _ in words) and _joint_words(plain)


def _motor_text(text: str) -> str | None:
    """The words of a motor named only by its kind or maker (_generic_motor), or of its description, that name its
    model, read by the same rules as a part name: the head says what it is and a model named after "for" is what it
    fits. "Servo, STS3215 12V": "STS3215 12V"; a description "Feetech STS3215, 7.4V" or "Shoulder - STS3215 12V": the
    STS3215; "Bus servo adapter, for STS3215", "U2D2 interface for XL430", "Bus adapter (STS3215)" or "STS3215 servo
    horn": None (an adapter, an interface, a horn)."""
    from .autoprofile import MODEL_RX
    text = _unlabel(text or "")
    head = _head(text)
    if MODEL_RX.search(head):
        return text if _is_motor_name(text) else None
    own = _FITS.split(text, 1)[0]
    m = MODEL_RX.search(own)
    if not m:
        return None
    rest = [w for w, k in _words(head) if k == "plain" and not (
        _BRAND.fullmatch(w) or _MOTOR_BRAND.fullmatch(w) or _KIND.fullmatch(w) or _MOTOR_WORD.fullmatch(w)
        or _ignorable(w))]
    if rest and not _joint_words(rest):
        return None                                    # the head names another part: "Bus adapter (STS3215)"
    seg = own[m.start():]
    return seg if _is_motor_name(seg) else None


def _not_a_motor_ref(ref: str) -> bool:
    """Reference designators that name only connectors, chips, passive parts or switches ("J2 J3 J4")."""
    refs = [m.group(0) for m in re.finditer(r"[A-Za-z]+(?=\d)", ref or "")]
    return bool(refs) and all(_NOT_MOTOR_REF.fullmatch(r) for r in refs)


def pack_count(item: str) -> int | None:
    """How many motors a line names in a pack ("STS3215 12V servo (6 pcs)": 6), or None."""
    m = _PACK.search(item or "")
    n = int(next(g for g in m.groups() if g)) if m else 0
    return n if 2 <= n <= 100 else None


def _classify(item: str, mpn: str = "", detail: str = "", ref: str = "") -> tuple[str | None, bool]:
    """(the catalog motor a BOM line is or None, whether it is a motor at all). The part name says what the line is
    (_is_motor_name): a horn, bracket, cable, frame, power supply or board named after the servo it is for is not a
    motor, and a model named only where the name says what the part comes with or fits ("for", "compatible with",
    "fits") is what it fits. A motor's model is in its name's head or its MPN; the name, then the description, notes
    or specs (`detail`) pick its variant (autoprofile.VARIANT_RX: an STS3215 described as 12 V is the 12 V servo). A
    motor named only by its kind or maker (_generic_motor) is the motor its description names. A line whose reference
    designators (`ref`: only a board's parts list passes them) are all connectors, chips, passive parts or switches ("J2
    J3") is none, whatever its value says."""
    from .autoprofile import MODEL_RX, TEXT_RX, VARIANT_RX, VARIANTS
    if ref and _not_a_motor_ref(ref):
        return None, False
    item = _unlabel(item)                              # "Base (J1): STS3215 12V", "Gripper - XL330-M288-T"
    if MODEL_RX.search(_head(item)) or (mpn and MODEL_RX.search(mpn)):
        if not _is_motor_name(item):
            return None, False
        name = " ".join(x for x in (item, mpn) if x)
    elif _generic_motor(item):                         # "Servo, STS3215 12V", "Servo" with a description
        name = _motor_text(item) or _motor_text(detail)
        if not name:
            return None, _is_motor_name(item)
    elif _joint_head(item) and _motor_text(item):     # "Base (MG996R)", "Shoulder (STS3215 12V)": its own model only
        name = _motor_text(item)                       # ("Gripper" described "SG90" may be the whole gripper)
    else:
        return None, _is_motor_name(item)
    key = next((k for rx, k in TEXT_RX if rx.search(name)), None)
    motor = VARIANTS.get(key)
    if motor:
        for text in (name, detail):
            pick = next((k for rx, k in VARIANT_RX if VARIANTS.get(k) == motor and rx.search(text)), None)
            if pick:
                return pick, True
    return key, True


def _catalog_key(item: str, mpn: str = "", detail: str = "", ref: str = "") -> str | None:
    """The catalog motor a BOM line is (_classify), or None."""
    return _classify(item, mpn, detail, ref)[0]


def _catalog_name(key: str | None) -> str | None:
    if not key:
        return None
    from ..robot import catalog, catalog_extra  # noqa: F401  (catalog_extra adds the 7.4 V STS3215)
    return (catalog.ACTUATORS.get(key) or {}).get("name")


def _category(item: str, key: str | None, motor: bool = False) -> str:
    """A line's kind, from its name's head (_head; the whole name when the head says nothing): "actuator" for any
    motor (a catalog one, or a stepper, BLDC motor, gear motor or servo the catalog lacks: `catalog` None), then
    fastener, cable, electronics, material or "purchased"."""
    if key or motor:
        return "actuator"
    for t in dict.fromkeys((_head(item).lower(), item.lower())):
        if re.search(r"\b(screw|bolt|nut|washer|insert|standoff|spacer|rivet|fastener)s?\b|\bm\d(?:\.\d)?\s?x\s?\d|"
                     r"\b(?:button|socket|cap|pan|flat|countersunk) head\b", t):
            return "fastener"
        if re.search(r"\b(?:cables?|wires?|harness(?:es)?|leads?|jumpers?|dupont)\b|\bcable-|\bextension\b(?!\s+board)|\bsplitters?\b", t):
            return "cable"
        if re.search(r"\b(board|controller|adapter|driver|tester|power supply|psu|smps|battery|arduino|esp32|"
                     r"raspberry pi|sensor|camera|webcam|u2d2|openrb|urt-?1|esc|odrive|encoder|step-?down|buck|"
                     r"converter|regulator|"
                     r"capacitor|resistor|shifter|buffer|ch340|usb to ttl|shield|interface|module|pcb)s?\b", t):
            return "electronics"
        if re.search(r"filament|pla\b|petg|resin|sheet|plate stock", t):
            return "material"
    return "purchased"


def map_table(rows: list) -> tuple[list, dict, list]:
    """Rows of cells (first row = header, possibly after title rows) -> (BOM rows, {field: header}, warnings)."""
    warns = []
    hi, head = None, None
    for i, r in enumerate(rows[:10]):
        cells = [_cell(c)[0].lower() for c in r]
        hits = sum(any(re.search(rx, c) for _, rx in FIELDS) for c in cells if c)
        if hits >= 2:
            hi, head = i, cells
            break
    if head is None:
        return [], {}, ["no header row with a part and a quantity or price column"]
    cols = {}
    used = set()
    data = rows[hi + 1:hi + 201]
    # a board's parts list (KiCad, KiBoM, JLC: it has a footprint column) names its parts by reference designators, J
    # a connector and U a chip; a hand-made list's "Ref" can be anything ("J1-J6": the arm's joints)
    board = any(re.search(r"footprint|package", h) for h in head if h)
    for field, rx in FIELDS:
        idx = [i for i, h in enumerate(head) if h and i not in used and re.search(rx, h)]
        if not idx:
            continue
        if field in ("unit_price", "ext_price", "url") and len(idx) > 1:
            usd = [i for i in idx if any(re.search(p, head[i]) for p, c in HEAD_CUR if c == "USD")]
            idx = usd or idx
        if field == "item" and len(idx) > 1:          # KiBoM: "Component" is the group number 1, 2, 3; with
            named = [i for i in idx if not all(re.fullmatch(r"\s*\d*\s*", _cell(r[i])[0] if i < len(r) else "")
                                               for r in data)]
            idx = named or idx                         # reference designators, the value names the part (SG90)
            if "ref" in cols:
                idx = [i for i in idx if head[i].startswith("value")] + [i for i in idx if not head[i].startswith("value")]
        cols[field] = idx[0]
        used.add(idx[0])
    if "item" not in cols and "description" in cols:
        cols["item"] = cols.pop("description")
    if "item" not in cols:
        cand = [i for i, h in enumerate(head) if i not in used and h]
        if "mpn" in cols and not cand:
            cols["item"] = cols["mpn"]
        elif cand:
            cols["item"] = cand[0]
            warns.append(f"used the column {head[cand[0]]!r} as the part name")
        else:
            return [], {}, ["no part name column"]
    head_cur = None
    if "unit_price" in cols:
        head_cur = next((c for p, c in HEAD_CUR if re.search(p, head[cols["unit_price"]])), None)
    out = []
    for r in rows[hi + 1:]:
        get = lambda f: r[cols[f]] if f in cols and cols[f] < len(r) else ""  # noqa: E731
        item, link = _cell(get("item"))
        if not item or SKIP_ITEM.match(item) or set(item) <= set("-:| "):
            continue
        qty = _qty(_cell(get("qty"))[0]) if "qty" in cols else None
        if "qty" in cols and qty == 0 and not get("qty").strip():
            qty = None
        unit, cur = _money(_cell(get("unit_price"))[0], head_cur) if "unit_price" in cols else (None, None)
        ext, cur2 = _money(_cell(get("ext_price"))[0], head_cur) if "ext_price" in cols else (None, None)
        url = _cell(get("url"))[1] if "url" in cols else None
        url = url or link
        q = qty if qty is not None else 1
        if ext is None and unit is not None:
            ext = round(unit * q, 2)
        if unit is None and ext is not None and q:
            unit = round(ext / q, 4)
        mpn = _cell(get("mpn"))[0] if "mpn" in cols else ""
        desc = _cell(get("description"))[0] if "description" in cols else ""
        ref = _cell(get("ref"))[0] if "ref" in cols else ""
        key, motor = _classify(item, mpn, desc, ref if board else "")
        out.append({"item": item[:200], "qty": q, "unit_price": unit, "ext_price": ext, "currency": cur or cur2,
                    "url": url if url and re.match(r"^https?://", url) else None, "mpn": mpn[:80] or None,
                    "manufacturer": _cell(get("manufacturer"))[0][:80] or None if "manufacturer" in cols else None,
                    "supplier": _cell(get("supplier"))[0][:80] or None if "supplier" in cols else None,
                    "ref": ref[:200] or None, "description": desc[:200] or None, "make_or_buy": "buy",
                    "category": "electronics" if board and ref and _not_a_motor_ref(ref) else _category(_unlabel(item),
                                                                                                     key, motor),
                    "catalog": key, "catalog_name": _catalog_name(key),
                    "pack": (pack_count(_unlabel(item)) or pack_count(item)) if motor else None, "source": "import"})
    return out, {f: rows[hi][i] if i < len(rows[hi]) else "" for f, i in cols.items()}, warns


# ------------------------------------------------------------------- readers
def markdown_tables(text: str) -> list:
    """[(title, rows)]: every pipe table with a separator row; title = the nearest heading above it."""
    out, rows, title, cur_title = [], [], None, None
    lines = text.splitlines() + [""]
    for ln in lines:
        s = ln.strip()
        if s.startswith("#"):
            cur_title = s.lstrip("#").strip()
        if s.startswith("|"):
            s = s.replace("\\|", "\x00")
            cells = [c.replace("\x00", "|").strip() for c in s.strip("|").split("|")]
            if rows and len(rows) == 1 and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                rows.append(None)                     # separator marker
                continue
            if not rows:
                title = cur_title
            rows.append(cells)
        elif rows:
            if len(rows) > 2 and rows[1] is None:
                out.append((title, [rows[0]] + rows[2:]))
            rows = []
    return out


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("cp1252", errors="replace")


def csv_rows(text: str, fmt: str) -> list:
    sample = text[:16384]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t" if fmt == "tsv" else ",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if fmt == "tsv" else csv.excel
    rows = []
    for i, r in enumerate(csv.reader(io.StringIO(text), dialect)):
        if i > 20000:
            raise ImportFailed("The table has more than 20,000 rows; that doesn't look like a parts list.")
        rows.append([c[:2000] for c in r[:60]])
    return rows


def xlsx_rows(path: str, sheet: int = 0) -> list:
    """First worksheet of an .xlsx as rows of strings, with the standard library only."""
    from .zipsafe import read_member

    def col(ref):
        n = 0
        for ch in re.match(r"[A-Z]+", ref).group(0):
            n = n * 26 + ord(ch) - 64
        return n - 1

    def xml(zf, name, cap=20 * MB):
        data = read_member(zf, name, cap)
        check_xml_text(data, name)
        return ET.fromstring(data)

    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise ImportFailed(f"{os.path.basename(path)} is not a readable .xlsx file.") from None
    with zf:
        if len(zf.infolist()) > 2000:
            raise ImportFailed("The workbook has too many parts.")
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
              "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
        names = set(zf.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in xml(zf, "xl/sharedStrings.xml").findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
        target = "xl/worksheets/sheet1.xml"
        if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
            wb = xml(zf, "xl/workbook.xml")
            sheets = wb.findall("m:sheets/m:sheet", ns)
            rels = {r.get("Id"): r.get("Target") for r in xml(zf, "xl/_rels/workbook.xml.rels")}
            if sheets:
                rid = sheets[min(sheet, len(sheets) - 1)].get(f"{{{ns['r']}}}id")
                t = (rels.get(rid) or "").lstrip("/")
                t = t if t.startswith("xl/") else "xl/" + t
                if t in names:
                    target = t
        if target not in names:
            raise ImportFailed("The workbook has no worksheet.")
        rows = []
        for row in xml(zf, target, 50 * MB).iter(f"{{{ns['m']}}}row"):
            cells = {}
            for c in row.findall("m:c", ns):
                ref, typ = c.get("r") or "", c.get("t")
                v = c.find("m:v", ns)
                if typ == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                    val = shared[int(v.text)]
                elif typ == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter(f"{{{ns['m']}}}t"))
                else:
                    val = v.text if v is not None and v.text is not None else ""
                if ref:
                    cells[col(ref)] = val
            if cells:
                width = min(max(cells) + 1, 60)
                rows.append([cells.get(i, "") for i in range(width)])
            if len(rows) > 20000:
                raise ImportFailed("The sheet has more than 20,000 rows; that doesn't look like a parts list.")
        return rows


def parse(path: str, fmt: str | None = None, table: int | None = None) -> dict:
    """{"rows", "columns", "tables": [{"index", "title", "rows"}], "table", "total", "currency", "warnings"}"""
    from .detect import sniff

    fmt = fmt or sniff(path)
    if os.path.getsize(path) > 20 * MB:
        raise ImportFailed(f"{os.path.basename(path)} is over 20 MB; that's not a parts list.")
    if fmt == "xlsx":
        tables = [(None, xlsx_rows(path))]
    else:
        with open(path, "rb") as fh:
            text = _decode(fh.read())
        if fmt in ("markdown", "readme", "text") and "|" in text:
            tables = markdown_tables(text)
        elif fmt in ("csv", "tsv", "text"):
            tables = [(None, csv_rows(text, fmt))]
        else:
            raise ImportFailed(f"{os.path.basename(path)} is not a table Morph can read.")
    mapped = []
    for i, (title, rows) in enumerate(tables):
        out, cols, warns = map_table(rows)
        if out and ("qty" in cols or "unit_price" in cols or "ext_price" in cols or "mpn" in cols):
            mapped.append({"index": len(mapped), "title": title, "rows": out, "columns": cols, "warnings": warns})
    if not mapped:
        raise ImportFailed(f"No parts list found in {os.path.basename(path)}: Morph looks for a table with a part "
                           "column and a quantity or price column.")
    pick = mapped[table] if table is not None and 0 <= table < len(mapped) else mapped[0]
    rows = pick["rows"]
    curs = {r["currency"] for r in rows if r.get("currency")}
    priced = [r["ext_price"] for r in rows if r.get("ext_price") is not None]
    total = round(sum(priced), 2) if priced and len(curs) <= 1 else None      # no prices: no total, not $0.00
    warns = list(pick["warnings"])
    if len(curs) > 1:
        warns.append(f"prices in several currencies ({', '.join(sorted(curs))}); no total")
    if any(r["unit_price"] is None for r in rows):
        warns.append(f"{sum(r['unit_price'] is None for r in rows)} rows have no price")
    return {"rows": rows, "columns": pick["columns"], "table": pick["index"], "title": pick["title"],
            "tables": [{"index": t["index"], "title": t["title"], "rows": len(t["rows"])} for t in mapped],
            "total": total, "currency": next(iter(curs)) if len(curs) == 1 else None, "warnings": warns}


def darwin_parts(rows: list) -> list:
    """[(item, query)] Darwin can search: one per purchased line (fasteners and cables as one kit each)."""
    from .. import darwin

    out, seen, kits = [], set(), {}
    for r in rows:
        if r.get("make_or_buy") != "buy":
            continue
        if r.get("category") in ("fastener", "cable"):
            kits.setdefault(r["category"], r["item"])
            continue
        q = darwin.short_item(r.get("mpn") or r["item"]) if r.get("mpn") else darwin.short_item(r["item"])
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append((r["item"], darwin.part_query(q)))
    for cat, item in kits.items():
        out.append((item, darwin.part_query(darwin.short_item(item))))
    return out


def actuator_keys(rows: list) -> dict:
    """{catalog key: count} for the actuators a parts list names (seeds the import's actuator picker): a line's Qty,
    or the motors its name packs when it was bought once ("STS3215 12V servo (6 pcs)", Qty 1: 6)."""
    out = {}
    for r in rows:
        if r.get("catalog"):
            n = int(r.get("qty") or 1)
            if n <= 1 and r.get("pack"):
                n = int(r["pack"])
            out[r["catalog"]] = out.get(r["catalog"], 0) + n
    return out
