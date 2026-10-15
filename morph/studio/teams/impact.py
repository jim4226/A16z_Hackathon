"""What a change request does, in numbers, before anything is built: the preview its author and reviewers read.

The pipeline's fast stages run on the changed design (sizing, the gripper checks and the electronics plan: well under a
second for a generated arm, about a second the first time for an imported one) and are compared with what the base
revision built: torque margins joint by joint, the servos, the bus protocol, controller and supply, the connectors and
firmware library the wiring stage generates, the geometry the CAD stage rebuilds around, the grip force, the purchased
parts' cost (priced the way the electronics stage prices them) and the issues the change adds or clears. Nothing is built
or written. Every number that doesn't come from a finished build is labelled an estimate: the build decides.
"""

from __future__ import annotations

import collections
import os
import threading
import time

from ...spec import Design, diff
from .disciplines import affected_stages
from .store import read_json

FIRMWARE_LIB = {"pca9685_pwm": "Adafruit PWM Servo Driver (PCA9685)", "feetech_scs": "SCServo (Feetech)",
                "dynamixel_2": "Dynamixel2Arduino (ROBOTIS)"}
BUS = {"pca9685_pwm": "hobby PWM through a PCA9685 driver", "feetech_scs": "Feetech SCS/STS serial bus",
       "dynamixel_2": "DYNAMIXEL Protocol 2.0 bus"}
LEVEL_PARTS = {"prototype": "mostly 3D-printed parts", "pilot": "sheet metal, CNC and MJF parts",
               "production": "injection-molded and sheet-metal parts"}
CONFIG = {"modules": "off-the-shelf modules", "custom_pcb": "a custom controller PCB"}
LINKS = {"base_h": "shoulder height", "L1": "upper arm", "L2": "forearm", "L3": "wrist link", "wrist": "wrist", "hand": "hand"}
EXTRA = {"wrist_camera": "wrist camera", "estop": "e-stop", "imu": "IMU", "wifi": "Wi-Fi"}
CRASH = "preflight could not run"
_RESULTS: "collections.OrderedDict" = collections.OrderedDict()
_SIZES: "collections.OrderedDict" = collections.OrderedDict()
_LOCK = threading.Lock()


def _base(root: str, rev: str) -> dict | None:
    """The parts of a revision's results.json the comparison needs (cached by the file's mtime and size)."""
    p = os.path.join(root, rev, "results.json")
    try:
        st = os.stat(p)
    except OSError:
        return None
    key = (os.path.normcase(os.path.abspath(p)), st.st_mtime_ns, st.st_size)
    with _LOCK:
        hit = _RESULTS.get(key)
    if hit is not None:
        return hit
    res, design = read_json(p, None), read_json(os.path.join(root, rev, "design.json"), None)
    if not isinstance(res, dict) or not isinstance(design, dict):
        return None
    el, bom, mfg = res.get("electronics") or {}, res.get("bom") or {}, res.get("manufacturing") or {}
    configs = mfg.get("configs") if isinstance(mfg, dict) and isinstance(mfg.get("configs"), dict) else {}
    out = {"design": design, "sizing": res.get("sizing") or {}, "eplan": res.get("eplan") or {},
           # each build level's mold tooling (paid once): the per-level unit costs spread it over the build quantity
           "tooling": {k: (v or {}).get("tooling_cost") for k, v in configs.items() if isinstance(v, dict)},
           "grip": (res.get("gripper") or {}).get("force"),
           "grip_n": ((res.get("gripper") or {}).get("force") or {}).get("target_force_n"),
           "summary": res.get("summary") or {}, "bom_totals": bom.get("totals") or {} if isinstance(bom, dict) else {},
           "configs": {k: (v or {}).get("cost") for k, v in (el.get("configs") or {}).items()}}
    with _LOCK:
        _RESULTS[key] = out
        while len(_RESULTS) > 32:
            _RESULTS.popitem(last=False)
    return out


def _size(design: Design) -> dict:
    """The sizing stage on a design (imported arms reload their URDF, about a second: memoized on what it reads)."""
    if not design.arm.source:
        from ...robot import arm
        return arm.size(design)
    from ...robot import imported
    a = design.arm
    key = (a.source, a.actuator_family, a.actuator_overrides, float(a.payload_margin), float(design.object.mass_g),
           design.electronics.extras, design.build.config, float(design.electronics.servo_v))
    with _LOCK:
        hit = _SIZES.get(key)
    if hit is None:
        hit = imported.size(design)
        with _LOCK:
            _SIZES[key] = hit
            while len(_SIZES) > 16:
                _SIZES.popitem(last=False)
    return hit


def _checks(design: Design) -> dict:
    """The New robot brief's cheap checks (sizing, gripper analysis, grip-force window, electronics plan): its issues and
    grip force target, never touching the network."""
    from .. import brief
    return brief.preflight(design.flat())


def _short(key: str) -> str:
    """A servo's catalog key as people say it: sts3215 -> STS3215, xm430_w350 -> XM430-W350, sts3215_7v4 -> STS3215 (7.4 V)."""
    k = str(key or "?")
    if k.endswith("_7v4"):
        return k[:-4].upper().replace("_", "-") + " (7.4 V)"
    return k.upper().replace("_", "-")


def _name(key: str) -> str:
    """A catalog part's name without its parenthesised details (ROBOTIS OpenRB-150, 12 V 10 A desktop power supply)."""
    from ...robot.catalog import ACTUATORS, MODULES
    if key == "esp32_s3_wroom":
        return "Morph controller PCB (ESP32-S3)"
    name = (MODULES.get(key) or ACTUATORS.get(key) or {}).get("name") or str(key)
    return name.split(" (", 1)[0].strip() if " (" in name and not name.startswith("(") else name


def _money(v: float, sign: bool = False) -> str:
    s = f"${abs(v):,.0f}" if abs(v) >= 100 else f"${abs(v):,.2f}".replace(".00", "")
    return (("+" if v >= 0 else "-") + s) if sign else s


def _servos(ep: dict) -> collections.Counter:
    return collections.Counter(a.get("key") for a in ep.get("actuators") or [] if a.get("key"))


def _servo_text(c: collections.Counter) -> str:
    return " + ".join(f"{n}× {_short(k)}" for k, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))) or "none"


def _parts(ep: dict) -> dict:
    """{key: (category, qty, price)} for the purchased parts a change can swap (servos, electronics modules, sensors),
    priced the way the electronics stage prices them (electronics2._row: the cheapest vendor). The custom PCBA is left out:
    it is quoted from its generated circuit when it builds."""
    from ...robot import electronics2
    from ...robot.catalog import ACTUATORS, MODULES
    out = {}

    def add(key, table, cat, n):
        item = table.get(key)
        if not item or key == "morph_controller_pcba":
            return
        row = electronics2._row(item.get("name") or key, cat, n, item.get("vendors") or [])
        q, p = out.get(key, (cat, 0, 0.0))[1:]
        out[key] = (cat, q + n, round(p + row["ext_price"], 2))

    for k, n in _servos(ep).items():
        add(k, ACTUATORS, "servos", n)
    for m in ep.get("modules") or []:
        add(m.get("key"), MODULES, "electronics", 1)
    for s in ep.get("sensors") or []:
        add(s.get("key"), MODULES, "electronics", 1)
    return out


def _bodies(keys) -> str:
    """Servo body sizes, servos of one size together: '46.5 × 28.5 × 34 mm (XL430-W250, XM430-W350)'."""
    from ...robot.catalog import ACTUATORS
    sizes = collections.OrderedDict()
    for k in keys:
        d = (ACTUATORS.get(k) or {}).get("dims") or {}
        if d:
            sizes.setdefault(f"{d.get('l', 0):g} × {d.get('w', 0):g} × {d.get('h', 0):g} mm", []).append(_short(k))
    return ", ".join(f"{s} ({', '.join(n)})" for s, n in sizes.items())


def _joints(sz: dict) -> dict:
    return {j.get("name"): j for j in sz.get("joints") or [] if j.get("name")}


def _jt(j: dict | None) -> str:
    if not j:
        return "none"
    n = int(j.get("count") or 1)
    return f"{n}× {_short(j.get('actuator'))} at {j.get('margin'):g}×" if isinstance(j.get("margin"), (int, float)) else \
        f"{n}× {_short(j.get('actuator'))}"


def _row(rows: list, key: str, label: str, text: str, tone: str = "", detail=None):
    rows.append({"key": key, "label": label, "text": text, "tone": tone, "detail": list(detail or [])})


def impact(root: str, base_rev: str | None, changes: dict, stages=()) -> dict:
    """{rows: [{key, label, text, tone, detail}], torque, servos, cost, issues, estimated: True, ms} for `changes` on
    `base_rev`. Never raises: a failure is reported in `error` (the change request still opens)."""
    t0 = time.perf_counter()
    try:
        out = _impact(root, base_rev, changes or {}, stages or ())
    except Exception as exc:  # noqa: BLE001  (an estimate must never block a change request)
        out = {"rows": [], "error": f"the numbers couldn't be estimated ({exc.__class__.__name__}: {exc})"}
    out.update(estimated=True, ms=round((time.perf_counter() - t0) * 1000))
    return out


def _impact(root: str, base_rev: str | None, changes: dict, stages) -> dict:
    b = _base(root, base_rev) if base_rev else None
    if b is None:
        return {"rows": [], "error": "no finished base revision to compare with"}
    base = Design.from_dict(b["design"])
    new = base.with_changes(changes)
    keys = list(diff(base, new))
    st = set(affected_stages(keys, stages))
    rows, out = [], {"stages": [s for s in ("sizing", "gripper", "electronics", "cad", "sim", "wiring", "manufacturing", "bom",
                                             "manual") if s in st]}
    if not keys:
        return {**out, "rows": rows}
    sz0, ep0 = b["sizing"], b["eplan"]
    sz1 = _size(new) if "sizing" in st else sz0
    chk0, chk1 = (_checks(base), _checks(new)) if st & {"sizing", "gripper", "electronics"} else ({}, {})
    grip1 = chk1.get("grip_target_n") if "gripper" in st else b["grip_n"]
    if "electronics" in st:
        from ...robot import electronics2
        ep1 = electronics2.plan(new, sz1, grip=chk1.get("grip") if "gripper" in st else b.get("grip"))
    else:
        ep1 = ep0

    # torque, joint by joint
    if "sizing" in st and sz0.get("joints") and sz1.get("joints"):  # (a base without sizing has nothing to compare with)
        j0, j1 = _joints(sz0), _joints(sz1)
        m0 = min((j.get("margin") for j in j0.values() if isinstance(j.get("margin"), (int, float))), default=None)
        m1 = min((j.get("margin") for j in j1.values() if isinstance(j.get("margin"), (int, float))), default=None)
        need = float(new.arm.payload_margin or 2.0)
        detail = []
        for name in list(j0) + [n for n in j1 if n not in j0]:
            a, c = j0.get(name), j1.get(name)
            same = a and c and a.get("actuator") == c.get("actuator") and a.get("count") == c.get("count")
            if same and abs((a.get("margin") or 0) - (c.get("margin") or 0)) < 0.03 * max(1e-9, a.get("margin") or 1):
                continue
            detail.append(f"{name.replace('_', ' ')}: {_jt(a)} → {_jt(c)}")
        same = m0 is not None and m1 is not None and abs(m1 - m0) < 0.005
        tone = "bad" if m1 is not None and m1 < need else "" if same else "good" if (m0 or 0) < (m1 or 0) else "warn"
        text = ("joint torques are sized again" if m0 is None or m1 is None else
                f"the weakest joint keeps {m1:g}× the torque it needs (the design asks for {need:g}×)" if same else
                f"the weakest joint has {m0:g}× → {m1:g}× the torque it needs (the design asks for {need:g}×)")
        detail = detail[:6]
        _row(rows, "torque", "Torque", text, tone, detail)
        out["torque"] = {"before_min": m0, "after_min": m1, "need": need}

    # servos (the gripper's too)
    s0, s1 = _servos(ep0), _servos(ep1)
    if s0 != s1 and s0 and s1:
        _row(rows, "servos", "Servos", f"{_servo_text(s0)} → {_servo_text(s1)}")
        out["servos"] = {"before": dict(s0), "after": dict(s1)}

    # electronics plan, wiring and firmware (compared with the base's plan, when it has one)
    if "electronics" in st and ep0:
        notes = []
        if ep0.get("protocol") != ep1.get("protocol"):
            notes.append(f"{BUS.get(ep0.get('protocol'), ep0.get('protocol'))} → {BUS.get(ep1.get('protocol'), ep1.get('protocol'))}")
        if ep0.get("config") != ep1.get("config"):
            notes.append(f"{CONFIG.get(ep0.get('config'), ep0.get('config'))} → {CONFIG.get(ep1.get('config'), ep1.get('config'))}")
        if ep0.get("controller") != ep1.get("controller"):
            notes.append(f"controller {_name(ep0.get('controller'))} → {_name(ep1.get('controller'))}")
        sp0, sp1 = (ep0.get("supply") or {}).get("key"), (ep1.get("supply") or {}).get("key")
        if sp0 != sp1:
            notes.append(f"supply {_name(sp0)} → {_name(sp1)}")
        m0 = {m.get("key") for m in ep0.get("modules") or []}
        m1 = {m.get("key") for m in ep1.get("modules") or []}
        gone = [_name(k) for k in sorted(m0 - m1) if k not in (ep0.get("controller"), sp0)]
        new_ = [_name(k) for k in sorted(m1 - m0) if k not in (ep1.get("controller"), sp1)]
        if gone:
            notes.append("no more " + ", ".join(gone))
        if new_:
            notes.append("adds " + ", ".join(new_))
        x0, x1 = set(ep0.get("extras") or []), set(ep1.get("extras") or [])
        if x1 - x0:
            notes.append("adds the " + " and the ".join(EXTRA.get(x, x.replace("_", " ")) for x in sorted(x1 - x0)))
        if x0 - x1:
            notes.append("drops the " + " and the ".join(EXTRA.get(x, x.replace("_", " ")) for x in sorted(x0 - x1)))
        warn = [w for w in ep1.get("warnings") or [] if w not in (ep0.get("warnings") or [])]
        _row(rows, "electronics", "Electronics", "; ".join(notes) or "the plan is recomputed: same controller, bus and supply",
             "warn" if warn else "", warn[:3])
    if "wiring" in st and ep0:
        from ...robot.electronics2 import BUS_CONNECTOR
        p0, p1 = ep0.get("protocol"), ep1.get("protocol")
        wiring = (f"servo cables {BUS_CONNECTOR.get(p0, p0)} → {BUS_CONNECTOR.get(p1, p1)}" if p0 != p1
                  else "schematic and harness regenerate")
        fw = (f"firmware library {FIRMWARE_LIB.get(p0, p0)} → {FIRMWARE_LIB.get(p1, p1)}" if p0 != p1
              else f"firmware regenerates ({FIRMWARE_LIB.get(p1, p1)})")
        if ep0.get("controller") != ep1.get("controller"):
            fw += f", built for the {_name(ep1.get('controller'))}"
        _row(rows, "wiring", "Wiring and firmware", f"{wiring}; {fw}")

    # geometry the CAD stage rebuilds around
    if st & {"sizing", "cad", "gripper"}:
        geo = []
        added, removed = [k for k in s1 if k not in s0], [k for k in s0 if k not in s1]
        after, before = _bodies(added), _bodies(removed)
        if after:
            geo.append((f"servo bodies {before} → {after}" if before else f"adds {after}") + ": joint brackets are redrawn")
        l0, l1 = sz0.get("links_mm") or {}, sz1.get("links_mm") or {}
        for k in [k for k in l1 if isinstance(l1.get(k), (int, float)) and isinstance(l0.get(k), (int, float))
                  and abs(l1[k] - l0[k]) >= 0.5]:
            geo.append(f"{LINKS.get(k, k)} {l0[k]:g} → {l1[k]:g} mm")
        for k, label, unit in (("reach_mm", "reach", " mm"), ("hand_mass_g", "hand", " g"), ("payload_g", "payload", " g")):
            a, c = sz0.get(k), sz1.get(k)
            if isinstance(a, (int, float)) and isinstance(c, (int, float)) and abs(a - c) >= 0.5:
                geo.append(f"{label} {a:g} → {c:g}{unit}")
        if not geo:
            geo.append("the robot CAD and its printed parts are rebuilt" if "cad" in st else "the same geometry")
        _row(rows, "geometry", "Geometry", "; ".join(geo))

    if "gripper" in st:
        g0 = b["grip_n"]
        if isinstance(g0, (int, float)) and isinstance(grip1, (int, float)) and abs(g0 - grip1) >= 0.05:
            _row(rows, "grip", "Grip", f"grip force target {g0:g} → {grip1:g} N")

    if "sim" in st:
        why = " in the new room" if any(k.startswith("world.") for k in keys) else ""
        _row(rows, "sim", "Simulation", f"pick and place, payload and reach are simulated again{why}: results when it builds")

    if base.build.config != new.build.config:
        _row(rows, "manufacturing", "Manufacturing", f"{base.build.config} ({LEVEL_PARTS.get(base.build.config, '')}) → "
                                                     f"{new.build.config} ({LEVEL_PARTS.get(new.build.config, '')})")

    # cost: the purchased parts that change, and the build level's parts
    if "bom" in st and ep0:
        p0, p1 = _parts(ep0), _parts(ep1)
        by = collections.defaultdict(float)
        for k in set(p0) | set(p1):
            cat = (p1.get(k) or p0.get(k))[0]
            by[cat] += (p1.get(k, (cat, 0, 0.0))[2]) - (p0.get(k, (cat, 0, 0.0))[2])
        notes, cfg0, cfg1 = [], ep0.get("config"), ep1.get("config")
        if cfg0 != cfg1 and b["configs"].get(cfg0) is not None and b["configs"].get(cfg1) is not None:
            by["electronics"] = b["configs"][cfg1] - b["configs"][cfg0]  # the base build priced both electronics options
        elif cfg1 == "custom_pcb" and "electronics" in st:
            notes.append("the custom PCBA is quoted again when it builds")
        # per unit without the mold tooling, which is paid once and shown apart (as the BOM tab does): the unit cost and
        # the per-level costs spread it over the build quantity, so at quantity 1 a production switch read +$73k a robot
        q0 = max(1, int(base.build.quantity or 1))
        tool = b.get("tooling") or {}
        t0 = float(tool.get(base.build.config) or 0.0)
        t1 = t0
        unit0 = (b["summary"] or {}).get("unit_cost")
        unit0 = round(unit0 - t0 / q0, 2) if isinstance(unit0, (int, float)) else unit0
        per = (b["bom_totals"] or {}).get("per_config") or {}
        if base.build.config != new.build.config and isinstance(per.get(base.build.config), (int, float)) \
                and isinstance(per.get(new.build.config), (int, float)):
            t1 = float(tool.get(new.build.config) or 0.0)
            by["parts made"] = (per[new.build.config] - t1 / q0) - (per[base.build.config] - t0 / q0)
        elif "manufacturing" in st:
            notes.append("the parts made are priced again when it builds")
        tdelta = round(t1 - t0, 2)
        if tdelta >= 0.5:
            notes.append(f"plus {_money(tdelta)} of mold tooling, paid once")
        elif tdelta <= -0.5:
            notes.append(f"and {_money(-tdelta)} less mold tooling, paid once" if t1 >= 0.5 else
                         f"and no mold tooling (it had {_money(t0)}, paid once)")
        elif t1 >= 0.5:
            notes.append(f"plus the {_money(t1)} of mold tooling, paid once")
        delta = round(sum(by.values()), 2)
        parts = ", ".join(f"{k} {_money(v, True)}" for k, v in sorted(by.items(), key=lambda kv: -abs(kv[1])) if abs(v) >= 0.5)
        qty = base.build.quantity != new.build.quantity
        if qty:
            notes.append(f"volume prices at {new.build.quantity:g} units come from the build")
        after = None if qty or not isinstance(unit0, (int, float)) else round(unit0 + delta, 2)
        if abs(delta) < 0.5:
            text = "the purchased parts don't change" + (f": {_money(unit0)} per robot" if after is not None and not notes else "")
        else:
            text = f"about {_money(delta, True)} per robot" + (f" ({parts})" if parts else "")
            if after is not None:
                text += f": {_money(unit0)} → about {_money(after)}"
        big = 0.15 * max(1.0, unit0 or 0)
        _row(rows, "cost", "Cost", text + ("; " + "; ".join(notes) if notes else ""),
             "bad" if delta >= big or tdelta >= big * q0 else "", [])
        # before, delta and after are per unit without the tooling; tooling is [before, after], paid once
        out["cost"] = {"before": unit0, "delta": delta, "after": after, "split": {k: round(v, 2) for k, v in by.items()},
                       "tooling": [round(t0, 2), round(t1, 2)]}

    # what the checks find before it builds (a check that crashed is not an issue of the design: the build decides)
    crash = [i for i in chk1.get("issues") or [] if i.startswith(CRASH)]
    i0 = [i for i in chk0.get("issues") or [] if not i.startswith(CRASH)]
    i1 = [i for i in chk1.get("issues") or [] if not i.startswith(CRASH)]
    added_issues = [i for i in i1 if i not in set(i0)] if not crash else []
    cleared = [i for i in i0 if i not in set(i1)] if not crash else []
    if crash:
        _row(rows, "checks", "Checks", "the quick checks couldn't run on this design" + crash[0][len(CRASH):]
             + "; the build will show whether a stage fails", "warn")
    if added_issues:
        _row(rows, "issues", "New issues", f"{len(added_issues)} issue{'s' if len(added_issues) != 1 else ''} the build would "
             "report", "bad", added_issues[:4])
    if cleared:
        _row(rows, "cleared", "Fixes", f"clears {len(cleared)} issue{'s' if len(cleared) != 1 else ''}", "good", cleared[:4])
    out["issues"] = {"added": added_issues, "cleared": cleared}
    return {**out, "rows": rows}
