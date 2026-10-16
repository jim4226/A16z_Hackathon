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
