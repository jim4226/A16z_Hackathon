"""World mode routes (morph/studio/api_world.py) against fake requests, rooms and a fake queue: no server, no
network. The router (routes.py) and rooms come with Wave 0; these tests use the same Request surface."""

import base64
import io
import json
import os
import struct
import sys
from types import SimpleNamespace

import pytest

from morph.cli import EGG_ROBOT
from morph.robot.pipeline2 import RobotProject
from morph.spec import Design
from morph.studio import api_world, worlds

sys.path.insert(0, os.path.dirname(__file__))
from test_world_checks import ROBOT, results  # noqa: E402


class Room:
    def __init__(self, root):
        self.name = "egg-picker"
        self.p = RobotProject(str(root))
        self.events = []
        self.hub = SimpleNamespace(send=self.events.append)

    def completed(self):
        return [r for r in self.p.revs() if os.path.isfile(os.path.join(self.p.rev_dir(r), "results.json"))]


class Studio:
    def __init__(self):
        self.calls = []

    def queue_changes(self, room, changes, **kw):
        self.calls.append((changes, kw))
        return SimpleNamespace(info=lambda: {"id": "job1", "status": "queued", "kind": kw.get("kind")})


class Req:
    def __init__(self, room, studio=None, body=None, query=None, raw=b"", member=None):
        self.room, self.studio, self.body, self.member = room, studio or Studio(), body, member
        self.query = {k: [v] for k, v in (query or {}).items()}
        self.raw, self.length = raw, len(raw)
        self.served = []
        self.handler = SimpleNamespace(_file=lambda path, csp=None: self.served.append(path))

    def arg(self, name, default=None):
        v = self.query.get(name)
        return v[0] if v else default

    def save(self, path):
        with open(path, "xb") as fh:
            fh.write(self.raw)
        return len(self.raw)


def make_rev(root, world=None, **kw):
    """r001 of an egg-picker project with the given world active (fake results, no pipeline run)."""
    d = Design().with_changes(EGG_ROBOT)
    if world is not None:
        d = d.with_changes({"world.id": world["id"], "world.sha": worlds.sha(world)})
    rd = root / "r001"
    (rd / "cad").mkdir(parents=True)
    (rd / "sim").mkdir()
    (root / "meta.json").write_text(json.dumps({"kind": "robot"}), encoding="utf-8")
    (rd / "design.json").write_text(d.dumps(), encoding="utf-8")
    (rd / "results.json").write_text(json.dumps(results(world or worlds.starter("desk"), **kw)), encoding="utf-8")
    (rd / "cad" / "robot.json").write_text(json.dumps(ROBOT), encoding="utf-8")
    return d


@pytest.fixture
def room(tmp_path):
    root = tmp_path / "egg-picker"
    make_rev(root)
    return Room(root)


def jpeg():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (90, 120, 150)).save(buf, "JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def test_get_without_a_world(room):
    st, body = api_world.get_world(Req(room))
    assert st == 200 and body["world"] is None and body["active"] == {"id": "", "sha": ""}
    assert [s["id"] for s in body["starters"]] == ["desk", "counter", "workbench"]
    assert body["notes"]["world"].startswith("World mode (beta): the robot is simulated against the grey boxes")
    assert body["notes"]["review"].startswith("AI review (beta) sends 6 pictures")
    assert [v["name"] for v in body["views"]] == list(worlds.VIEWS)
    assert body["sim"]["rev"] == "r001"


def test_save_a_starter_queues_a_sim_only_revision(room):
    req = Req(room, body={"starter": "desk"}, member={"id": "m1", "name": "Ana"})
    st, body = api_world.save_world(req)
    w = worlds.load(room.p.root)
    assert st == 202 and body["world"] == w and body["sha"] == worlds.sha(w) and body["job"]["id"] == "job1"
    changes, kw = req.studio.calls[0]
    assert changes == {"world.id": "desk", "world.sha": worlds.sha(w)}
    assert kw["label"] == "world: Desk" and kw["base_rev"] == "r001" and kw["by"] == "m1" and kw["kind"] == "world"
    assert any(e["type"] == "world" and e["stage"] == "save" for e in room.events)


def test_save_unchanged_off_and_errors(tmp_path):
    w = worlds.starter("counter")
    root = tmp_path / "p"
    make_rev(root, world=w)
    room = Room(root)
    worlds.save(room.p.root, w)
    st, body = api_world.save_world(Req(room, body={"world": w}))
    assert st == 200 and body["unchanged"]
    req = Req(room, body={"off": True})
    st, body = api_world.save_world(req)
    assert st == 202 and req.studio.calls[0][0] == {"world.id": "", "world.sha": ""}
    for bad, msg in (({}, "Send {starter"), ({"starter": "garage"}, "starter world"),
                     ({"world": {**w, "asset": "../x.glb"}}, "room.glb"), ({"world": {**w, "mount": None}}, "Place the robot")):
        st, body = api_world.save_world(Req(room, body=bad))
        assert st == 400 and msg in body["error"]

    class Full(Studio):
        def queue_changes(self, *a, **k):
            raise RuntimeError("10 changes are already waiting")
    st, body = api_world.save_world(Req(room, Full(), body={"starter": "desk"}))
    assert st == 429


def _glb_bytes():
    from test_world_obstacles import counter_scene_glb

    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "x.glb")
    from pathlib import Path
    counter_scene_glb(Path(p))
    with open(p, "rb") as fh:
        return fh.read()


def test_upload_check_and_serve(room):
    data = _glb_bytes()
    st, body = api_world.upload_world(Req(room, raw=data, query={"name": "../../My Kitchen.glb"}))
    assert st == 200 and body["faces"] > 100 and body["draft"]["asset"] == "room.glb"
    assert body["draft"]["name"] == "My Kitchen" and body["draft"]["units"] == "m" and len(body["draft"]["asset_sha"]) == 16
    folder = worlds.world_dir(room.p.root)
    assert sorted(os.listdir(folder)) == ["room.glb"]
    req = Req(room, query={"file": "room.glb"})
    assert api_world.get_world(req) is None and req.served == [worlds.asset_path(room.p.root)]
    assert api_world.get_world(Req(room, query={"file": "../../meta.json"}))[0] == 404
    # a file that isn't a .glb is refused and leaves nothing behind
    st, body = api_world.upload_world(Req(room, raw=b"PK\x03\x04" + b"\0" * 64))
    assert st == 400 and "isn't a .glb" in body["error"] and sorted(os.listdir(folder)) == ["room.glb"]
    assert api_world.upload_world(Req(room, raw=b""))[0] == 400
    big = Req(room, raw=b"x")
    big.length = worlds.MAX_GLB_BYTES + 1
    assert api_world.upload_world(big)[0] == 413


def test_auto_obstacles_route(room):
    st, body = api_world.auto_obstacles(Req(room, body={}))
    assert st == 400 and "Upload a room" in body["error"]
    st, up = api_world.upload_world(Req(room, raw=_glb_bytes()))
    draft = {**up["draft"], "mount": {"position": [0.0, -100.0, 900.0], "yaw_deg": -90.0}}
    st, body = api_world.auto_obstacles(Req(room, body={"world": draft}))
    assert st == 200 and len(body["obstacles"]) >= 3
    s = body["surface"]                                   # in the robot's axes (yaw -90): the counter's length is y
    assert s["yaw_deg"] == -90.0 and sorted(s["half"][:2]) == [pytest.approx(320, abs=25), pytest.approx(620, abs=25)]
    kinds = [(e["type"], e.get("role"), e.get("status")) for e in room.events]
    assert ("agent", "feasibility", "done") in kinds and ("world", None, "done") in kinds
    assert api_world.auto_obstacles(Req(room, body={"world": {"mount": None}}))[0] == 400


def test_inspect_route(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    w = worlds.starter("desk")
    root = tmp_path / "p"
    make_rev(root, world=w, pp={"success": False, "world_contacts": [{"t": 3.0, "link": "jaw_left", "obstacle": "Mug"}]})
    room = Room(root)
    views = [{"name": n, "jpeg_b64": jpeg()} for n in worlds.VIEWS]
    assert api_world.inspect_world(Req(room, body={"views": views}))[0] == 400            # no saved world yet
    worlds.save(room.p.root, w)
    st, body = api_world.inspect_world(Req(room, body={"views": views, "ai": False}))
    assert st == 200 and body["verdict"] == "blocked" and body["review"]["source"] == "rules"
    assert {c["id"]: c["status"] for c in body["checks"]}["clearance"] == "fail"
    assert "skipped" not in body["review"]
    # the AI review asked for without a key: the measured review, and it says why (nothing was sent)
    st, body = api_world.inspect_world(Req(room, body={"views": views}))
    assert st == 200 and body["review"]["source"] == "rules" and "Anthropic key" in body["review"]["skipped"]
    seen = []

    def fake_review(views, facts, **kw):
        seen.append(len(views))
        return {**worlds.rules_review(facts), "source": "claude", "verdict": "ready", "ai_verdict": "ready", "ms": 5}
    monkeypatch.setattr(worlds, "review_request", fake_review)
    monkeypatch.setattr(worlds, "_claude_ready", lambda: True)
    st, body = api_world.inspect_world(Req(room, body={"views": views}))
    assert seen == [6] and body["verdict"] == "blocked" and body["review"]["verdict"] == "blocked"   # the AI can't pass it
    agents = [(e["role"], e["provider"], e["status"]) for e in room.events if e["type"] == "agent"]
    assert ("reviewer", "claude", "done") in agents and ("feasibility", "code", "done") in agents
    assert api_world.inspect_world(Req(room, body={"views": [{"name": "base", "jpeg_b64": "@@"}]}))[0] == 400
    assert api_world.inspect_world(Req(room, body={"views": views, "rev": "r999"}))[0] == 404


def test_register_routes():
    got = []

    def route(method, path, **kw):
        return lambda fn: got.append((method, path, fn.__name__, kw))
    api_world.register(route)
    assert [(m, p) for m, p, _, _ in got] == [("GET", "/api/world"), ("POST", "/api/world"), ("PUT", "/api/world/upload"),
                                              ("POST", "/api/world/obstacles/auto"), ("POST", "/api/world/inspect")]
    kw = {p + m: k for m, p, _, k in got}
    assert kw["/api/worldGET"] == {"role": "viewer"}
    assert kw["/api/world/uploadPUT"] == {"raw_body": True, "max_body": 300 << 20}
    assert kw["/api/world/inspectPOST"] == {"max_body": 16 << 20}


def test_glb_magic_is_checked_not_the_extension(room):
    fake = struct.pack("<III", 0x46546C67, 2, 20) + struct.pack("<II", 0, 0x4E4F534A)
    st, body = api_world.upload_world(Req(room, raw=fake, query={"name": "room.glb"}))
    assert st == 400
