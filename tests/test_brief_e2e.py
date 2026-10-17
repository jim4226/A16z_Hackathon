"""One-liner -> brief -> defaults -> a real RobotProject build (fast sims): every stage computes and the robot is feasible.
The default run builds a repaired design and the README's egg request (about 15 s each); MORPH_E2E=1 builds four more."""

import json
import os

import pytest

from morph.robot.pipeline2 import ORDER, RobotProject
from morph.spec import Design
from morph.studio import agents
from morph.studio import brief as B

CASES = [("a robot that picks up 300 g cans", "can-mover", True),        # repaired: 4 joints, 7.5 mm finger beams
         ("pick up a raw egg without cracking it", "egg-picker", True),  # the README's example: the egg keeps its 25 N
         ("something to pick strawberries", "strawberry-picker", False),
         ("help me sort socks", "sock-sorter", False),
         ("Pick a raw egg and place it without cracking it.", "egg-picker", False),
         ("pick up eggs without breaking them", "egg-picker", False)]


@pytest.mark.parametrize("text,name,default", CASES, ids=[f"{c[1]}-{i}" for i, c in enumerate(CASES)])
def test_one_liner_builds(tmp_path, monkeypatch, text, name, default):
    if not default and not os.environ.get("MORPH_E2E"):
        pytest.skip("set MORPH_E2E=1 for the full end-to-end set")
    for k in list(os.environ):
        if k.startswith(("ANTHROPIC", "KEEWANO", "SUBQ")):
            monkeypatch.delenv(k, raising=False)
    home = str(tmp_path)
    b = agents.run_turn(agents.run_turn(text=text, home=home), action="defaults", home=home)
    assert b.status == "ready" and b.preflight["issues"] == []
    pay = B.build_payload(b, home=home)
    assert pay["name"] == name
    p = RobotProject(os.path.join(home, pay["name"]))
    with open(os.path.join(p.root, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(pay["meta"], fh)
    rev = p.run(Design.from_dict(pay["design"]), label="first design", n_trials=4, verbose=False, fast_sim=True)
    res = p.load_results(rev)
    assert [res["provenance"][n] for n in ORDER] == ["computed"] * len(ORDER), res["provenance"]
    s = res["summary"]
    assert s["feasible"], s["issues"]
    assert s["pick_place_success"] is True
    if b.estimate:
        assert abs(b.estimate["unit_cost"] - s["unit_cost"]) <= 0.12 * s["unit_cost"]  # the confirm card's estimate holds
    B.mark_built(b, pay["name"], rev)
    B.write_final(b, p.root)
    assert json.load(open(os.path.join(p.root, "brief.json"), encoding="utf-8"))["rev"] == rev
