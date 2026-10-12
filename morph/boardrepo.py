"""BoardRepo (boardrepo.com): open reference boards next to Morph's, and a place to publish Morph's.

BoardRepo hosts about 8,000 public KiCad and Altium boards with schematic, PCB and BOM views.
Morph uses it two ways, neither of which needs an account:

  - reference designs: a short, checked list of open boards that already do what a Morph design
    needs (a servo gripper carrier, a Feetech/Dynamixel bus interface, a PCA9685 PWM board, an HX711
    load cell). Each board's public markdown page (<board>.md) is read live for its licence,
    description and which views exist; good answers are cached on disk.
  - publishing: every carrier board is also a KiCad project (morph/kicad.py). `morph kicad` zips it
    the way boardrepo.com/upload takes it. Uploading needs the user's own sign-in, so Morph stops at
    the zip and the steps.

BoardRepo's MCP server (https://boardrepo.com/mcp) needs a browser OAuth token for every call, public
boards included, so Morph reads the public pages instead. Nothing here raises: a design run never
waits on BoardRepo.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen

from . import settings

BASE = "https://boardrepo.com"
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "morph", "boardrepo")

# Checked on boardrepo.com on 2026-10-11. `license` is what BoardRepo showed then; None means no licence
# it recognised, so link to the board, don't copy it. GPL-3.0 boards are to learn from: copying their
# layout makes a derivative work. `for` holds design/catalog keys (see keys_for).
REFERENCES = [
    {"board": "evroc/robotic-gripper", "for": ["gripper"], "license": "MIT",
     "why": "3D-printed servo gripper for robot arms with its own carrier board (Seeeduino, USB-C, DC jack): "
            "the closest open analog to Morph's gripper carrier, without a force sensor"},
    {"board": "pollen-robotics/elec-rpi-robot-hat",
     "for": ["smart_bus", "dynamixel", "sts3215", "xl430_w250", "xm430_w350", "bus_servo_adapter", "u2d2"], "license": None,
     "why": "Dynamixel/Feetech servos over TTL and RS-485 plus an IMU, 5-28 V in: a working half-duplex servo-bus interface"},
    {"board": "c-ake/pca9685-rpi-hat", "for": ["hobby_pwm", "pca9685_board"], "license": "GPL-3.0",
     "why": "16-channel PCA9685 PWM board, through-hole for hand soldering"},
    {"board": "szbeni/forcegauge", "for": ["fsr402", "load_cell", "hx711"], "license": "GPL-3.0",
     "why": "load cell + HX711 + ESP32-C3 streaming force over WiFi: the absolute-force upgrade from an FSR"},
    {"board": "solderedelectronics/load-cell-ampfilier-hx711-board-hardware-design",
     "for": ["fsr402", "load_cell", "hx711"], "license": None, "why": "22 x 22 mm HX711 load-cell amplifier breakout"},
]


def page(board: str) -> str:
    return f"{BASE}/{board}"


def board_id(s: str) -> str | None:
    """'owner/slug', a pasted board URL, or its .md twin -> 'owner/slug'; None if it isn't one."""
    s = s.strip()
    for pre in (BASE + "/", "boardrepo.com/", "/"):
        s = s[len(pre):] if s.startswith(pre) else s
    s = s.rstrip("/")
    s = s[:-3] if s.endswith(".md") else s
    ok = re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", s) and not {".", ".."} & set(s.split("/"))
    return s if ok else None


def parse_md(text: str) -> dict:
    """A board's markdown twin -> {title, description, license, contents}. Pure."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n")
    fm = {}
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if v.strip():
                fm[k.strip()] = v.strip().strip('"')
    contents = []
    sec = re.search(r"^## Contents\n(.*?)(?:\n## |\Z)", text, re.S | re.M)
    if sec:
        contents = [ln[2:].strip() for ln in sec.group(1).splitlines() if ln.startswith("- ")]
    return {"title": fm.get("title"), "description": fm.get("description"), "license": fm.get("license"),
            "contents": contents}


def _cache_path(board: str) -> str:
    return os.path.join(CACHE, hashlib.sha1(board.encode()).hexdigest()[:16] + ".json")


def fetch(board: str, timeout: float = 15) -> dict:
    """Live details of one public board; falls back to the disk cache. Never raises."""
    bid = board_id(board)
    if not bid:
        return {"board": board, "url": None, "ok": False, "error": "not a BoardRepo board (expected owner/slug)"}
    board, out = bid, {"board": bid, "url": page(bid)}
    try:
        if not settings.network_allowed():
            raise RuntimeError("network lookups are off in the settings: showing saved board details only")
        req = Request(f"{page(board)}.md", headers={"Accept": "text/markdown", "User-Agent": "morph"})
        with urlopen(req, timeout=timeout) as resp:
            info = {**out, **parse_md(resp.read(1 << 20).decode("utf-8", "replace")), "ok": True}
        if not (info["title"] or info["license"] or info["contents"]):
            raise ValueError("no board front matter in the page")
    except Exception as exc:
        err = f"{exc.__class__.__name__}: {exc}"
    else:
        try:   # best effort: an unwritable cache must not drop a good answer
            os.makedirs(CACHE, exist_ok=True)
            with open(_cache_path(board), "w", encoding="utf-8") as fh:
                json.dump({**info, "fetched": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh)
        except OSError:
            pass
        return info
    try:
        with open(_cache_path(board), encoding="utf-8") as fh:
            return {**json.load(fh), "cached": True, "error": err}
    except (OSError, ValueError):
        return {**out, "ok": False, "error": err}


def keys_for(design: dict, summary: dict | None = None, robot: bool = False) -> set:
    """Design dict (+ results summary) -> the keys REFERENCES are tagged with.

    Every Design carries arm defaults, so the arm's actuator family only counts for a robot
    revision (robot=True: the whole-robot pipeline built it)."""
    keys = set()
    if design.get("finger"):
        keys.add("gripper")
    e, f = design.get("electronics") or {}, design.get("finger") or {}
    fam = (design.get("arm") or {}).get("actuator_family") if robot else None
    keys |= {v for v in (e.get("mcu"), e.get("servo"), f.get("sensor"), fam, (summary or {}).get("servo"))
             if v and v not in ("auto", "none")}
    return keys


def related(keys, live: bool = False) -> list:
    """References relevant to `keys`, in REFERENCES order; with live=True, each with fetch() details."""
    refs = [dict(r) for r in REFERENCES if set(r["for"]) & set(keys)]
    if live and refs:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for r, info in zip(refs, pool.map(lambda r: fetch(r["board"]), refs)):
                r["live"] = info
    return refs


def notes_md(refs: list) -> str:
    """Markdown section for a published board's README."""
    if not refs:
        return ""
    lines = ["## Related open designs on BoardRepo", ""]
    for r in refs:
        lic = r.get("license") or "no licence recognised: link only"
        lines.append(f"- [{r['board']}]({page(r['board'])}) ({lic}): {r['why']}")
    return "\n".join(lines) + "\n"


def publish_steps(zip_path: str) -> list:
    return [f"1. Open {BASE}/upload and sign in with GitHub or Google (signing in creates the account).",
            f"2. Upload {zip_path} (a zipped KiCad project folder; KiCad 6+ gets schematic, PCB, 3D and BOM views).",
            "3. Pick the visibility (Public, Unlisted, Password or Private) and publish.",
            f"4. When processing finishes, {BASE}/<you>/<board>.md lists 'PCB layout' under Contents."]
