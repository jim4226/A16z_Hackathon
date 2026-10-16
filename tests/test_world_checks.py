"""World mode exact checks and the AI review (morph/studio/worlds.py): pass or fail comes from geometry and the
simulation's measurements only; Claude and SubQ add suggestions. Offline: the model calls use fakes."""

import base64
import copy
import io
import json
from types import SimpleNamespace

import pytest

from morph.cli import EGG_ROBOT
from morph.spec import Design
from morph.studio import worlds as W

ROBOT = {"root": "base", "parts": [{"name": "base_plate", "link": "base", "bbox": [[-70, -70, 0], [70, 70, 12]], "mass_g": 400},
                                   {"name": "upper_arm", "link": "upper", "bbox": [[-900, -900, 0], [900, 900, 5]], "mass_g": 800}]}


def design(**changes):
    return Design().with_changes({**EGG_ROBOT, **changes})


def results(world, pp=None, ws=None, peak=0.3):
    sw = W.sim_world(world)
    m = {"success": True, "reachable": True, "ik_error_mm": 0.2, "place_error_mm": 0.3, "world": world["id"],
         "world_sha": sw["sha"], "world_contacts": [], "min_clearance_mm": 60.0,
         "min_clearance_at": {"t": 4.2, "link": "jaw_left", "obstacle": "Mug"}, "clearance_ok": True}
    m.update(pp or {})
    w = {"reach_mm": 474.0, "reach_3d_mm": 420.0, "max_height_mm": 400.0, "pick_reachable": True, "place_reachable": True,
         "usable_workspace_fraction": 0.9, "arm_top_mm": 400.0, "arm_reach_mm": 420.0}
    w.update(ws or {})
    return {"sims": {"scenarios": {"pick_place": {"metrics": m}, "workspace": {"metrics": w},
                                   "payload_sweep": {"metrics": {"curve": [{"shoulder_torque_nm": peak, "ok": True}]}}}},
            "cad": {"mass_kg": 1.2}, "summary": {"dof": 5}}


def run(world=None, d=None, sims=None, **kw):
    world = world or W.starter("desk")
    out = W.checks(world, results(world, **kw), sims=sims, robot=ROBOT, design=d or design())
    return out, {c["id"]: c for c in out["checks"]}


def moved(dx=0.0, dy=0.0, name="desk"):
    w = copy.deepcopy(W.starter(name))
    w["mount"]["position"][0] += dx
    w["mount"]["position"][1] += dy
    return W.validate(w)


def test_all_clear_is_ready():
    out, c = run()
    assert out["verdict"] == "ready"
    assert {k: v["status"] for k, v in c.items()} == {
        "sim_world": "pass", "reach": "pass", "pick_place": "pass", "clearance": "pass", "targets": "pass",
        "footprint": "pass", "tipover": "pass", "people": "pass", "overhead": "pass", "workspace": "pass"}
    assert c["footprint"]["value"] == pytest.approx(160.0, abs=0.5)     # 230 mm to the back edge, base 70 mm
    assert out["facts"]["verdict"] == "ready" and out["facts"]["robot"]["base_footprint_mm"] == [[-70, -70], [70, 70]]


TRUTH = [
    # (inputs, {check: status}, verdict)
    ({"pp": {"world": None}}, {"sim_world": "fail", "clearance": "skip"}, "needs_changes"),
    ({"pp": {"world_sha": "0000000000000000"}}, {"sim_world": "fail"}, "needs_changes"),
    ({"pp": {"reachable": False, "note": "place target out of top-down reach"}}, {"reach": "fail"}, "blocked"),
    ({"pp": {"success": False, "failure": "object dropped"}}, {"pick_place": "fail"}, "blocked"),
    ({"pp": {"success": False, "world_contacts": [{"t": 3.2, "link": "jaw_left", "obstacle": "Mug"}],
             "min_clearance_mm": -2.0}}, {"clearance": "fail", "pick_place": "fail"}, "blocked"),
    ({"pp": {"min_clearance_mm": 5.0}}, {"clearance": "warn"}, "ready"),
    ({"pp": {"min_clearance_mm": -1.0}}, {"clearance": "fail"}, "blocked"),
    ({"d": design(**{"task.place_x_mm": 240.0, "task.place_y_mm": 330.0})}, {"targets": "fail"}, "blocked"),
    ({"d": design(**{"task.place_x_mm": 490.0, "task.place_y_mm": 0.0})}, {"targets": "fail"}, "blocked"),
    ({"world": moved(dy=180.0)}, {"footprint": "fail"}, "needs_changes"),
    ({"world": moved(dy=150.0)}, {"footprint": "warn"}, "ready"),
    ({"peak": 5.0}, {"tipover": "warn"}, "ready"),
    ({"ws": {"arm_reach_mm": 600.0}}, {"people": "warn"}, "ready"),
    ({"world": W.starter("counter"), "ws": {"arm_top_mm": 567.0}}, {"overhead": "warn"}, "ready"),
    ({"ws": {"usable_workspace_fraction": 0.3}}, {"workspace": "warn"}, "ready"),
]


@pytest.mark.parametrize("inputs,expect,verdict", TRUTH)
def test_truth_table(inputs, expect, verdict):
    out, c = run(**inputs)
    assert {k: c[k]["status"] for k in expect} == expect, {k: c[k]["text"] for k in expect}
    assert out["verdict"] == verdict
    for k in expect:
        assert c[k]["text"] and c[k]["view"] in W.VIEWS


def test_clearance_texts_name_what_and_where():
    _, c = run(pp={"success": False, "world_contacts": [{"t": 3.2, "link": "jaw_left", "obstacle": "Mug"}]})
    assert "jaw left hits Mug at 3.2 s" in c["clearance"]["text"]
    _, c = run(pp={"min_clearance_mm": 5.0})
    assert "Only 5.0 mm" in c["clearance"]["text"] and "Mug" in c["clearance"]["text"]
    _, c = run(d=design(**{"task.place_x_mm": 240.0, "task.place_y_mm": 330.0}))
    assert "inside Mug" in c["targets"]["text"]


@pytest.mark.parametrize("mc, status, text", [
    (9.97, "warn", "Only 9.9 mm between the arm and the room"),       # before: "Only 10 mm ...; 10 mm or more is OK"
    (9.5, "warn", "Only 9.5 mm between the arm and the room"), (5.04, "warn", "Only 5.0 mm"),
    (30.68, "pass", "At least 30 mm between the arm and the room"),   # before: "At least 31 mm"
    (10.0, "pass", "At least 10 mm"), (47.64, "pass", "At least 47 mm")])
def test_the_simulations_clearance_is_never_rounded_up(mc, status, text):
    """The clearance along the simulated path, shown rounded down (to 0.1 mm under 10 mm, else to the millimetre):
    never more room than was measured, and a near miss is never shown as the 10 mm that would be OK."""
    _, c = run(pp={"min_clearance_mm": mc})
    assert c["clearance"]["status"] == status and text in c["clearance"]["text"], c["clearance"]
    assert "Only 10 mm" not in c["clearance"]["text"], c["clearance"]


def test_the_base_margin_and_the_room_overhead_are_never_rounded_up():
    """The base 19.96 mm inside the desk's edge is "19 mm" with the advice to keep 20 (before: "20 mm", a pass), and
    a cabinet 49.97 mm over the arm's highest point is "49 mm above" (before: "50"); 0.04 mm into it is "0.1 mm"."""
    robot = {**ROBOT, "parts": [{**ROBOT["parts"][0], "bbox": [[-70.04, -70, 0], [70.04, 70, 12]]}, ROBOT["parts"][1]]}
    w = moved(dy=140.0)
    c = {x["id"]: x for x in W.checks(w, results(w), robot=robot, design=design())["checks"]}
    assert c["footprint"]["value"] <= 19.96 and c["footprint"]["status"] == "warn", c["footprint"]
    assert c["footprint"]["text"].startswith("The base sits 19 mm inside the edge") and "Keep 20 mm or more." in \
        c["footprint"]["text"], c["footprint"]
    counter = W.starter("counter")
    room = run(world=counter, ws={"arm_top_mm": 0.0})[1]["overhead"]["value"]     # its bottom over the counter top
    for hmax, status, text, most in ((room - 49.97, "warn", "is 49 mm above the arm's highest point", 49.97),
                                     (room - 50.0, "pass", "is 50 mm above", 50.0),
                                     (room + 0.04, "warn", "reaches 0.1 mm into", -0.04)):
        o = run(world=counter, ws={"arm_top_mm": hmax})[1]["overhead"]
        assert o["status"] == status and text in o["text"] and o["value"] <= most, (hmax, o)


def test_people_zones_workspace_share_and_targets():
    pts = [[800.0, 200.0, 0.0]] * 30 + [[100.0, 0.0, 100.0]] * 70        # 30 % of the cloud sits in the chair zone
    out, c = run(ws={"arm_reach_mm": 600.0}, sims={"workspace": {"points": pts}})
    assert c["people"]["status"] == "fail" and c["people"]["value"] == pytest.approx(0.3) and out["verdict"] == "needs_changes"
    w = copy.deepcopy(W.starter("desk"))
    w["zones"].append({"id": "aisle", "label": "Aisle", "kind": "walkway",
                       "polygon": [[-400, -300], [0, -300], [0, 0], [-400, 0]]})       # around the pick spot
    out, c = run(world=W.validate(w))
    assert c["people"]["status"] == "fail" and "pick spot is in Aisle" in c["people"]["text"]


def test_wrist_camera_line_of_sight():
    w = copy.deepcopy(W.starter("desk"))
    over = W.to_world(w, [220.0, -80.0, 100.0])
    w["obstacles"].append({"label": "Lamp arm", "center": over, "half": [30, 30, 15]})
    out, c = run(world=W.validate(w), d=design(**{"electronics.extras": "wrist_camera"}))
    assert c["wrist_camera"]["status"] == "fail" and "Lamp arm" in c["wrist_camera"]["text"]
    assert c["targets"]["status"] == "pass" and out["verdict"] == "needs_changes"
    _, c = run(d=design(**{"electronics.extras": "wrist_camera"}))
    assert c["wrist_camera"]["status"] == "pass"
    assert "wrist_camera" not in run()[1]


def test_footprint_shift_moves_the_base_back_on():
    w = moved(dy=180.0)
    shift = W.footprint_shift(w, [[-70, -70], [70, 70]])
    assert shift is not None
    w2 = copy.deepcopy(w)
    w2["mount"]["position"] = W.to_world(w, [shift[0], shift[1], 0.0])
    _, c = run(world=W.validate(w2))
    assert c["footprint"]["status"] == "pass" and c["footprint"]["value"] == pytest.approx(25.0, abs=0.5)
    assert W.footprint_shift(W.starter("desk"), [[-70, -70], [70, 70]]) is None


# ---------------------------------------------------------------------- the AI review


def _jpeg(color=(40, 90, 160)):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def views(names=W.VIEWS):
    return W.clean_views([{"name": n, "jpeg_b64": _jpeg()} for n in names])


class FakeClaude:
    def __init__(self, payload=None, stop="end_turn", exc=None):
        self.payload, self.stop, self.exc, self.calls = payload, stop, exc, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SimpleNamespace(stop_reason=self.stop, content=[SimpleNamespace(type="thinking", thinking=""),
                                                                SimpleNamespace(type="text", text=text)])


AI = {"verdict": "ready", "summary": "Looks fine to me.",
      "findings": [{"category": "clearance", "severity": "warning", "viewpoint": "along_path",
                    "evidence": "A cable runs past the place spot without a grey box.", "recommendation": "Add a box."},
                   {"category": "astrology", "severity": "critical", "viewpoint": "overview", "evidence": "x",
                    "recommendation": "y"},
                   {"category": "safety", "severity": "nuclear", "viewpoint": "ceiling", "evidence": "A chair is close.",
                    "recommendation": "Move it."}],
      "suggested_changes": [{"kind": "move_target", "target": "place", "x_mm": 900, "y_mm": -50, "z_mm": 0, "yaw_deg": 0,
                             "size_x_mm": 0, "size_y_mm": 0, "size_z_mm": 0, "label": "", "reason": "Away from the cable."},
                            {"kind": "move_mount", "target": "none", "x_mm": 50, "y_mm": 0, "z_mm": 0, "yaw_deg": 400,
                             "size_x_mm": 0, "size_y_mm": 0, "size_z_mm": 0, "label": "", "reason": "More room."},
                            {"kind": "add_obstacle", "target": "none", "x_mm": 200, "y_mm": 100, "z_mm": 10, "yaw_deg": 0,
                             "size_x_mm": 300, "size_y_mm": 8, "size_z_mm": 8, "label": "Cable", "reason": "Unmodelled."},
                            {"kind": "launch", "target": "none"}],
      "tour": [{"viewpoint": "overview", "caption": "The arm on the desk."}, {"viewpoint": "nowhere", "caption": "x"},
               {"viewpoint": "along_path", "caption": "Along the path " * 40}]}


def test_review_schema_is_strict():
    def walk(s):
        if s.get("type") == "object":
            assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
            for v in s["properties"].values():
                walk(v)
        elif s.get("type") == "array":
            walk(s["items"])
    walk(W.SCHEMA)
    assert W.SCHEMA["properties"]["verdict"]["enum"] == ["ready", "needs_changes", "blocked"]
    assert W.SCHEMA["properties"]["findings"]["items"]["properties"]["viewpoint"]["enum"] == list(W.VIEWS)


def test_review_request_shape_and_the_ai_never_changes_pass_or_fail():
    world = W.starter("desk")
    facts = run(pp={"success": False, "failure": "the jaw left hits Mug at 3.2 s"})[0]["facts"]
    assert facts["verdict"] == "blocked"
    fake = FakeClaude(AI)
    v = views()
    out = W.review_request(v, facts, client=fake, world=world)
    kw = fake.calls[0]
    from morph.studio.intent import MODEL
    assert kw["model"] == MODEL and kw["output_config"]["format"] == {"type": "json_schema", "schema": W.SCHEMA}
    assert "tool_choice" not in kw and "tools" not in kw and kw["system"] == W.SYSTEM
    content = kw["messages"][0]["content"]
    assert sum(1 for b in content if b["type"] == "image") == 6 and "FACTS:" in content[-1]["text"]
    assert all(b["source"]["media_type"] == "image/jpeg" for b in content if b["type"] == "image")
    # the measured verdict stands; the model's lean is shown separately
    assert out["verdict"] == "blocked" and out["ai_verdict"] == "ready" and "measured verdict stays blocked" in out["note"]
    assert out["source"] == "claude"
    # strictly the schema's enums: an unknown category, severity or view drops the finding (nothing is guessed)
    assert [f["category"] for f in out["findings"]] == ["clearance"]
    ch = {c["kind"]: c for c in out["suggested_changes"]}
    assert set(ch) == {"move_target", "move_mount", "add_obstacle"}
    assert ch["move_target"]["changes"] == {"task.place_x_mm": 400.0, "task.place_y_mm": -50.0}     # clamped to PARAMS
    assert ch["move_mount"]["params"]["yaw_deg"] == 180.0
    assert ch["move_mount"]["mount"]["position"] == pytest.approx(W.to_world(world, [50.0, 0.0, 0.0]), abs=0.1)
    assert ch["add_obstacle"]["obstacle"]["half"] == [150.0, 5.0, 5.0] and ch["add_obstacle"]["obstacle"]["source"] == "user"
    W.validate({**world, "obstacles": world["obstacles"] + [ch["add_obstacle"]["obstacle"]]})
    assert [s["viewpoint"] for s in out["tour"]] == ["overview", "along_path"] and len(out["tour"][1]["caption"]) <= 280
    # and the other way round: a measured "ready" stays ready when the model says blocked
    ready = run()[0]["facts"]
    out = W.review_request(v, ready, client=FakeClaude({**AI, "verdict": "blocked"}), world=world)
    assert out["verdict"] == "ready" and out["ai_verdict"] == "blocked"
    # a mount move that would take the base off the desk top can't be applied: it is dropped
    far = {**AI, "suggested_changes": [{**AI["suggested_changes"][1], "x_mm": 1000, "y_mm": -1000, "yaw_deg": 0}]}
    out = W.review_request(v, ready, client=FakeClaude(far), world=world)
    assert out["suggested_changes"] == []


def test_review_falls_back_to_the_measured_review(monkeypatch):
    facts = run(pp={"min_clearance_mm": 5.0})[0]["facts"]
    v = views()
    for fake in (FakeClaude(exc=TimeoutError("slow")), FakeClaude(AI, stop="refusal"), FakeClaude("{not json"),
                 FakeClaude(AI, stop="max_tokens")):
        out = W.review_request(v, facts, client=fake)
        assert out["source"] == "rules" and out["verdict"] == "ready" and "unavailable" in out["error"]
        assert any(f["category"] == "clearance" and f["severity"] == "warning" for f in out["findings"])
        assert [s["viewpoint"] for s in out["tour"]] == list(W.VIEWS)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    out = W.review_request(v, facts)
    assert out["source"] == "rules" and "error" not in out


def test_rules_review_suggests_moving_the_base_on():
    w = moved(dy=180.0)
    out, _ = run(world=w)
    rev = W.rules_review(out["facts"], W.VIEWS, w)
    mv = [c for c in rev["suggested_changes"] if c["kind"] == "move_mount"]
    assert mv and mv[0]["source"] == "rules" and mv[0]["params"]["x_mm"] > 0     # forward, off the back edge
    assert any(f["category"] == "mounting" and f["severity"] == "critical" for f in rev["findings"])


def test_clean_views():
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 20).decode()
    out = W.clean_views([{"name": "overview", "jpeg_b64": "data:image/jpeg;base64," + _jpeg()},
                         {"name": "overview", "jpeg_b64": _jpeg()}, {"name": "selfie", "jpeg_b64": _jpeg()},
                         {"name": "top_down", "jpeg_b64": png}])
    assert [(v["name"], v["media_type"]) for v in out] == [("overview", "image/jpeg"), ("top_down", "image/png")]
    for bad in ([{"name": "base", "jpeg_b64": base64.b64encode(b"GIF89a....").decode()}],
                [{"name": "base", "jpeg_b64": "@@@"}], [{"name": "base", "jpeg_b64": "A" * (5 << 20)}], "views"):
        with pytest.raises(W.WorldError):
            W.clean_views(bad)


class FakeSubQ:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def enabled(self):
        return True

    def decide(self, state, questions, images=None, **kw):
        self.calls.append((state, questions, images, kw))
        return {"answers": self.answers, "usage": {"input_tokens": 900}, "ms": 420, "source": "subq"}


def test_subq_findings_never_change_the_verdict(monkeypatch):
    sq = FakeSubQ({"unmodelled_obstacle": {"noul": 0.82}, "person_in_reach": {"noul": 0.4},
                   "best_mount": {"choice": "B", "confidence": 0.71}})
    monkeypatch.setattr(W, "_subq", lambda: sq)
    world = copy.deepcopy(W.starter("desk"))
    world["candidates"] = [{"position": world["mount"]["position"]}, {"position": [100.0, 100.0, 740.0]}]
    world = W.validate(world)
    res = W.inspect(world, results(world), views(), robot=ROBOT, design=design(), ai=False, use_subq=True)
    state, questions, images, kw = sq.calls[0]
    assert len(images) == 4 and all(set(i) == {"mediaType", "base64"} for i in images)
    assert set(questions) == {"unmodelled_obstacle", "person_in_reach", "best_mount"}
    assert questions["best_mount"]["criteria"] == {"A": None, "B": None}
    assert kw["timeout"] <= 8.0
    assert res["verdict"] == res["review"]["verdict"] == "ready"
    sub = [f for f in res["review"]["findings"] if f["source"] == "subq"]
    assert [f["severity"] for f in sub] == ["warning", "info"] and sub[0]["evidence"].startswith("SubQ Snap (AI)")
    assert res["subq"]["best_mount"] == "B"
    monkeypatch.setattr(W, "_subq", lambda: None)
    assert W.subq_review(views(), res["facts"]) is None


def test_world_without_boxes_warns_instead_of_failing():
    w = copy.deepcopy(W.starter("desk"))
    w["obstacles"] = []
    w = W.validate(w)
    res = results(w)
    for k in ("min_clearance_mm", "min_clearance_at"):                       # as simulate reports a box-free world
        res["sims"]["scenarios"]["pick_place"]["metrics"].pop(k)
    out = W.checks(w, res, robot=ROBOT, design=design())
    c = {x["id"]: x for x in out["checks"]}
    assert c["sim_world"]["status"] == "pass" and c["clearance"]["status"] == "warn" and "no grey boxes" in c["clearance"]["text"]
    assert out["verdict"] == "ready"


class LegendSubQ(FakeSubQ):
    def level(self, res, name):                       # as morph.studio.subq.level: the nearest level's label
        a = res["answers"][name]
        return float(a["score"]), str((a.get("legend") or {}).get(str(round(a["score"])), ""))


def test_subq_wrist_light_reads_the_nearest_level(monkeypatch):
    world = W.starter("desk")
    facts = W.checks(world, results(world), robot=ROBOT, design=design())["facts"]
    quiet = {"unmodelled_obstacle": {"noul": 0.1}, "person_in_reach": {"noul": 0.1}}
    legend = {"1": "poor: dark or glare", "2": "ok: usable", "3": "good: even, no glare"}
    for sq, poor in ((LegendSubQ({**quiet, "wrist_light": {"score": 1.2, "confidence": 0.8, "legend": legend}}), True),
                     (LegendSubQ({**quiet, "wrist_light": {"score": 2.1, "confidence": 0.8, "legend": legend}}), False),
                     (LegendSubQ({**quiet, "wrist_light": {"score": 1.0, "confidence": 0.4, "legend": legend}}), False),
                     (FakeSubQ({**quiet, "wrist_light": {"score": 0.2, "confidence": 0.8}}), True),
                     (FakeSubQ({**quiet, "wrist_light": {"score": 1.4, "confidence": 0.8}}), False)):
        monkeypatch.setattr(W, "_subq", lambda sq=sq: sq)
        out = W.subq_review(views(), facts, wrist_camera=True)
        assert "wrist_light" in sq.calls[0][1]
        assert any(f["category"] == "perception" for f in out["findings"]) is poor


def test_inspect_without_ai_sends_nothing(monkeypatch):
    world = W.starter("desk")
    boom = FakeClaude(exc=AssertionError("must not be called"))
    res = W.inspect(world, results(world), views(), robot=ROBOT, design=design(), ai=False, client=boom)
    assert not boom.calls and res["review"]["source"] == "rules" and res["subq"] is None


def test_a_shelf_and_a_walkway_are_measured_from_the_arm_not_its_tool_point():
    """R6: the overhead and people checks took the highest and furthest tool point of 4000 random poses (the
    egg-picker's 567.1 / 568.0 mm), while its fingers reach 587.8 mm up and 587.9 mm out. A shelf 50 mm above the tool
    point's top passed as "50 mm above the arm's highest reach" with 29 mm left, and a walkway 105 mm past the tool
    point's reach plus 100 mm passed with 85 mm left. They now take the arm's own top and furthest point."""
    tcp = {"max_height_mm": 567.1, "reach_3d_mm": 568.0}
    arm = {**tcp, "arm_top_mm": 588.0, "arm_reach_mm": 588.0}             # the egg-picker's workspace scenario
    shelf = {"id": "shelf", "label": "Shelf", "center": [0.0, 0.0, 567.1 + 50.0 + 20.0], "half": [150.0, 150.0, 20.0]}
    aisle = {"id": "aisle", "label": "Aisle", "kind": "walkway", "z": [-200.0, 1800.0],
             "polygon": [[673.0, -300.0], [1073.0, -300.0], [1073.0, 300.0], [673.0, 300.0]]}      # 568 + 100 + 5 out
    w = W.validate({"id": "bench", "mount": {"position": [0, 0, 0], "yaw_deg": 0}, "obstacles": [shelf], "zones": [aisle],
                    "surface": {"label": "Bench top", "center": [100.0, 0.0, -12.5], "half": [550.0, 600.0, 12.5]}})
    c = {x["id"]: x for x in W.checks(w, results(w, ws=arm), robot=ROBOT, design=design())["checks"]}
    assert c["overhead"]["status"] == "warn" and c["overhead"]["text"] == "Shelf is 29 mm above the arm's highest point."
    assert c["people"]["status"] == "warn" and "overlaps Aisle" in c["people"]["text"], c["people"]
    far = W.validate({**w, "zones": [{**aisle, "polygon": [[x + 200.0, y] for x, y in aisle["polygon"]]}]})
    c = {x["id"]: x for x in W.checks(far, results(far, ws=arm), robot=ROBOT, design=design())["checks"]}
    assert c["people"]["status"] == "pass" and c["people"]["text"] == \
        "No walkway or seat is within 100 mm of anywhere the arm can reach."
    # a revision simulated before the arm's own extent was measured: not judged on its tool point
    old = {k: v for k, v in results(w)["sims"]["scenarios"]["workspace"]["metrics"].items() if not k.startswith("arm_")}
    res = results(w)
    res["sims"]["scenarios"]["workspace"]["metrics"] = old
    c = {x["id"]: x for x in W.checks(w, res, robot=ROBOT, design=design())["checks"]}
    assert c["overhead"]["status"] == "skip" and "simulate it again" in c["overhead"]["text"]
    assert c["people"]["status"] == "skip" and "simulate it again" in c["people"]["text"]


@pytest.mark.parametrize("kept, status, text", [(0.4996, "warn", "49% of the arm's poses"), (0.499, "warn", "49%"),
                                                (0.5, "pass", "50% of the arm's poses stay clear of the room.")])
def test_the_room_to_move_is_never_rounded_up(kept, status, text):
    """R6: 1249 of 2500 poses clear (49.96 %) was kept as 0.5 and passed as "50% of the arm's poses stay clear"."""
    _, c = run(ws={"usable_workspace_fraction": kept})
    assert c["workspace"]["status"] == status and text in c["workspace"]["text"], c["workspace"]
