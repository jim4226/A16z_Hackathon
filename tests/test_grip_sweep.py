"""The robots people build (the two demo robots and New robot's one-liners) either pass every scenario they are
simulated in, or their build says which one failed: a grip that holds the object in the pick and place but drops it in
the robot's own shake test must not read as "software checks pass". And no scenario that passed before the release
review's grip fixes (6c49545) fails now.

Each case is built as the Studio builds it (every stage, full-precision simulations, offline, defaults for every
question), so the sweep takes 15 to 25 minutes: MORPH_SLOW=1. BASE records 6c49545's outcomes, built the same way."""

import json
import os

import pytest

SLOW = os.environ.get("MORPH_SLOW") == "1"

# (case, one-liner): None builds the demo robot of that name
CASES = [("egg-picker", None), ("so101", None),
         # tests/test_brief_e2e.py
         ("t-cans300", "a robot that picks up 300 g cans"), ("t-egg-readme", "pick up a raw egg without cracking it"),
         ("t-strawberries", "something to pick strawberries"), ("t-socks-help", "help me sort socks"),
         ("t-egg-desc", "Pick a raw egg and place it without cracking it."),
         ("t-eggs-nobreak", "pick up eggs without breaking them"),
         # the release review's one-liners
         ("ux-robot", "I need a robot"), ("ux-berries", "pick strawberries"), ("ux-socks", "sort socks"),
         ("ux-plants", "a robot that waters my plants"), ("ux-jars-grandma", "help my grandma open jars"),
         ("ux-cat", "feed my cat when I'm away"), ("ux-lego", "assemble lego"), ("ux-jars", "open jars"),
         ("ux-caps", "unscrew bottle caps"), ("ux-tools", "hand me screwdrivers at my workbench"),
         ("ux-pills", "pass my dad his pills"), ("ux-mugs", "help my grandma move her mugs"),
         ("k-cans-crate", "move full soda cans from the counter into a crate"), ("k-plants", "water my plants"),
         ("k-cat", "feed my cat"), ("k-so101-berries", "use my SO-101 to pick strawberries into a punnet")]

ALL = ("pick_place", "payload_sweep", "workspace", "shake")
# 6c49545: every scenario passed except the egg one-liners', whose egg became a 3 N strawberry (fixed since)
BASE = {tag: {s: True for s in ALL} for tag, _ in CASES}
for _tag in ("t-egg-readme", "t-egg-desc", "t-eggs-nobreak"):
    BASE[_tag].update(pick_place=False, shake=False)


def _design(tag, text, home):
    from morph.spec import Design
    from morph.studio import agents
    from morph.studio import brief as B
    from morph.studio.projects import template

    if text is None:
        return template(tag, "so101" if tag == "so101" else "")
    b = agents.run_turn(agents.run_turn(text=text, home=home), action="defaults", home=home)
    assert b.status == "ready", (text, b.status)
    return Design.from_dict(B.build_payload(b, home=home)["design"])


@pytest.mark.skipif(not SLOW, reason="MORPH_SLOW=1: 24 robots built with full-precision simulations (15-25 minutes)")
@pytest.mark.parametrize("tag,text", CASES, ids=[c[0] for c in CASES])
def test_every_simulated_scenario_passes_or_the_build_says_which_failed(tmp_path, monkeypatch, tag, text):
    from morph.robot.pipeline2 import RobotProject, sim_issue

    for k in list(os.environ):
        if k.startswith(("ANTHROPIC", "KEEWANO", "SUBQ")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    d = _design(tag, text, str(tmp_path / "briefs"))
    p = RobotProject(str(tmp_path / tag))
    res = p.load_results(p.run(d, label="first design", n_trials=4, verbose=False))
    s, sims = res["summary"], res["sims"]["scenarios"]
    assert set(sims) == set(ALL)
    outcome = {name: sc["metrics"]["success"] for name, sc in sims.items()}
    print(tag, json.dumps({"grip_target_n": s["grip_target_n"], "outcome": outcome, "feasible": s["feasible"],
                           "shake_slip_mm": sims["shake"]["metrics"].get("slip_mm")}))
    for name, sc in sims.items():
        if not outcome[name]:        # a failure is an issue that names it, and the robot isn't called feasible
            assert sim_issue(name, sc) in s["issues"] and s["feasible"] is False, (name, s["issues"])
    regressed = [name for name in ALL if BASE[tag][name] and not outcome[name]]
    assert not regressed, f"{tag}: {regressed} passed on 6c49545 and fail now: " + json.dumps(
        {n: sims[n]["metrics"] for n in regressed}, default=str)[:1500]
