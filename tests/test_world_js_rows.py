"""World mode's scan rows and Add box here, the functions world.js ships, run in Node (no browser): what a row says a
tried box leaves, and the id and label a box gets when the world holds that label already. Skipped without Node."""

import json
import os
import re
import shutil
import subprocess

import pytest

WORLD_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "morph", "studio", "static",
                        "world.js")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="no Node.js")


def _shipped(*names) -> str:
    """The top-level `const <name> = ...` definitions of world.js, as shipped: from that line to the next top-level
    statement or comment."""
    with open(WORLD_JS, encoding="utf-8") as fh:
        src = fh.read()
    out = []
    for n in names:
        m = re.search(rf"^const {n} = .*?(?=^(?:const |let |function |class |export |import |//))", src, re.M | re.S)
        assert m, n
        out.append(m.group(0).strip())
    return "\n".join(out)


def _run(defs: str, expr: str):
    js = f"{defs}\nconsole.log(JSON.stringify({expr}));"
    p = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_add_box_here_never_gives_a_box_a_label_the_world_holds():
    """1f6b690's addScanBox kept the region's label and changed only a clashing id: after an earlier "Scan near miss 1"
    was added and simulated, the next check's box 2 (renumbered 1) was added as a second "Scan near miss 1" (ids scan-1
    and scan-1-2). The box now takes the next free "Scan near miss k" past every one the world holds."""
    defs = _shipped("scanBoxName")
    case = lambda obs, g: _run(defs, f"scanBoxName({json.dumps(obs)}, {json.dumps(g)})")  # noqa: E731
    g = {"id": "scan-1", "label": "Scan near miss 1"}
    assert case([], g) == g
    assert case([{"id": "shelf", "label": "Shelf"}], g) == g
    assert case([{"id": "scan-1", "label": "Scan near miss 1"}], g) == {"id": "scan-2", "label": "Scan near miss 2"}
    assert case([{"id": "scan-1", "label": "Scan near miss 1"}, {"id": "scan-3", "label": "Scan near miss 3"}], g) == \
        {"id": "scan-4", "label": "Scan near miss 4"}
    assert case([{"id": "scan-1-2", "label": "Scan near miss 1"}], g) == {"id": "scan-2", "label": "Scan near miss 2"}
    assert case([{"id": "drawn", "label": "Scan near miss 1"}], g) == {"id": "scan-2", "label": "Scan near miss 2"}
    assert case([{"id": "scan-2", "label": "Shelf"}], {"id": "scan-2", "label": "Scan near miss 2"}) == \
        {"id": "scan-3", "label": "Scan near miss 3"}


def _tried(trial):
    defs = _shipped("esc", "mmDown", "scanTrialClear", "scanTried", "scanBoxTitle")
    g = {"trial": trial, "touches": False}
    return _run(defs, f"[scanTried({json.dumps(g)}), scanBoxTitle({json.dumps(g)})]")


def test_a_tried_rows_number_is_never_another_boxs_overlap_or_a_failure_that_did_not_happen():
    """A box kept although the revision's own simulation fails (`fails`): its trial's clearance is the simulation's
    own, measured to whatever box it comes closest to, and can be negative there (it fails on that other box).
    1f6b690's row said "tried: the simulation keeps at least -2.4 mm from it (and still fails: ...)". Measured to
    another box and under 0.5 mm, the row now gives no number: the simulation keeps clear of this box and still fails
    elsewhere, or, when it doesn't fail, just keeps clear of it."""
    fails = "the wrist hits Shelf during the carry at 4.1 s"
    row, title = _tried({"ok": True, "min_clearance_mm": -2.31, "at_box": False, "fails": fails})
    assert row == f"; tried: the simulation keeps clear of it and still fails elsewhere ({fails})", row
    assert "-2" not in title and "keeps clear of it" in title, title
    row, title = _tried({"ok": True, "min_clearance_mm": 0.3, "at_box": False, "fails": fails})
    assert row == f"; tried: the simulation keeps clear of it and still fails elsewhere ({fails})", row
    row, title = _tried({"ok": True, "min_clearance_mm": 0.3, "at_box": False, "fails": None})
    assert row == "; tried: the simulation keeps clear of it" and "fail" not in title, (row, title)
    # measured to another box, 0.5 mm or more: a lower bound for this one
    row, title = _tried({"ok": True, "min_clearance_mm": 3.17, "at_box": False, "fails": None})
    assert row == "; tried: the simulation keeps at least 3.1 mm from it" and "no closer to it than 3.1 mm" in title
    row, _ = _tried({"ok": True, "min_clearance_mm": 3.17, "at_box": False, "fails": fails})
    assert row == f"; tried: the simulation keeps at least 3.1 mm from it (and still fails: {fails})", row
    # measured to this box: its own distance, rounded down
    row, title = _tried({"ok": True, "min_clearance_mm": 2.06, "at_box": True, "fails": None})
    assert row == "; tried: the simulation keeps 2.0 mm from it" and "no closer to it than 2.0 mm" in title
    row, _ = _tried({"ok": True, "min_clearance_mm": 0.02, "at_box": True, "fails": None})
    assert row == "; tried: the simulation keeps 0.0 mm from it", row
    row, title = _tried({"ok": True, "min_clearance_mm": None, "at_box": False, "fails": None})
    assert row == "; tried: the simulation keeps clear of it" and "keeps clear of it" in title
    assert _tried({"ok": False, "failure": fails})[0] == ""
