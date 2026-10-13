"""The project service: new projects (default, SO-101 or a design), forks, deletes to the trash, unique names inside
MORPH_HOME, meta.json with origin/demo/created_utc, and a Reset demo that touches demo projects only."""

import argparse
import http.client
import json
import os
import threading

import pytest

from morph import cli, keys
from morph.robot import pipeline2
from morph.robot.pipeline2 import RobotProject, pipeline_version
from morph.spec import Design
from morph.studio import projects, server
from test_studio_rooms import FakeBuilds, finish, make_project


@pytest.fixture
def builds(monkeypatch):
    fake = FakeBuilds()
    monkeypatch.setattr(RobotProject, "run", lambda self, *a, **k: fake(self, *a, **k))
    return fake


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "projects"
    make_project(h, "egg-picker")
    return h


# ------------------------------------------------------------------ names and meta

def test_names_are_slugs_unique_inside_home(home):
    assert projects.slug("Strawberry Picker!") == "strawberry-picker"
    assert projects.slug("Erdbeer-Pflücker") == "erdbeer-pflucker"
    assert projects.slug("CON") == "con-robot" and projects.slug("r012") == "r012-robot" and projects.slug("") == "robot"
    assert len(projects.slug("x" * 99)) == 40
    (home / "strawberry-picker").mkdir()
    (home / "strawberry-picker-2.txt").write_text("not a folder, still taken", encoding="utf-8")
    assert projects.unique_name(str(home), "strawberry-picker") == "strawberry-picker-2"
    (home / "strawberry-picker-2").mkdir()
    assert projects.unique_name(str(home), "strawberry-picker") == "strawberry-picker-3"
    assert projects.unique_name(str(home), "y" * 40) == "y" * 40
    (home / ("y" * 40)).mkdir()
    assert projects.unique_name(str(home), "y" * 40) == "y" * 38 + "-2"


def test_create_writes_meta_and_builds_nothing(home):
    info = projects.create(str(home), "Egg Picker", need="eggs, gently")
    assert info["name"] == "egg-picker-2" and info["design"].name == "egg-picker-2"
    meta = json.loads((home / "egg-picker-2" / "meta.json").read_text(encoding="utf-8"))
    assert meta["kind"] == "robot" and meta["source"] == "" and meta["need"] == "eggs, gently"
    assert meta["origin"] == "default" and meta["demo"] is False and meta["created_utc"].endswith("Z")
    assert RobotProject(info["root"]).revs() == []
    arm = projects.create(str(home), source="so101", by="m-ana")
    assert arm["name"] == "so101" and arm["design"].arm.source == "so101" and arm["meta"]["created_by"] == "m-ana"
    assert projects.create(str(home), source="so101")["name"] == "so101-2"
    d = projects.create(str(home), "my arm", design={"name": "x", "arm": {"dof": 6}}, origin="brief",
                        meta={"brief_id": "b1", "kind": "evil"})
    assert d["design"].arm.dof == 6 and d["design"].name == "my-arm" and d["meta"]["brief_id"] == "b1"
    assert d["meta"]["kind"] == "robot" and d["meta"]["origin"] == "brief"
    m = projects.create(str(home), "sneaky", meta={"demo": True, "origin": "import", "source": "so101", "created_by": "x",
                                                   "created_utc": "1970", "brief_id": "b2"})["meta"]
    assert (m["demo"], m["origin"], m["source"], m["brief_id"]) == (False, "default", "", "b2") and "created_by" not in m
    listed = {p["name"]: p for p in projects.list_projects(str(home))}
    assert set(listed) == {"egg-picker", "egg-picker-2", "so101", "so101-2", "my-arm", "sneaky"}
    assert listed["egg-picker"]["revs"] == 1 and listed["my-arm"]["revs"] == 0


def test_create_refuses_bad_names_sources_and_designs_before_making_anything(home, tmp_path):
    before = sorted(os.listdir(home))
    for kwargs in ({"source": "../so101"}, {"source": "nope"}, {"source": "So101"}, {"design": {"arm": {"nope": 1}}},
                   {"design": "arm.dof=6"}, {"design": {"arm": {"source": "../../etc"}}}, {"origin": "cloud"},
                   {"design": {"bogus": 1}}, {"design": {"arms": {"dof": 6}}}, {"design": {"arm": 5}}):
        with pytest.raises(ValueError):
            projects.create(str(home), "x", **kwargs)
    for name in ("../pwned", "/tmp/abs", "Egg", "a/b", "con", ".hidden", 5):
        with pytest.raises(ValueError):
            projects.create(str(home), name, unique=False)
    with pytest.raises(FileExistsError):
        projects.create(str(home), "egg-picker", unique=False)   # has revisions
    assert sorted(os.listdir(home)) == before and not (tmp_path / "pwned").exists()
    (home / "half-made").mkdir()                                  # a first build that failed: may be tried again
    assert projects.create(str(home), "half-made", unique=False)["name"] == "half-made"


def test_design_values_from_requests_are_checked(home):
    d = projects.template("x")
    projects.check_design(d)
    for bad in ({"arm.reach_mm": 900}, {"arm.dof": 7}, {"build.config": "mass"}, {"arm.actuator_family": "banana"},
                {"electronics.target_force_n": float("nan")}, {"object.diameter_mm": 1e9}, {"finger.pad_height_mm": 1e6},
                {"gripper.stroke_mm": -5}, {"fab.quantity": 0}, {"object.shape": 5}, {"finger.material": "x" * 501},
                {"task.cycle_time_s": float("inf")}):
        with pytest.raises(ValueError):
            projects.check_design(d.with_changes(bad))
    with pytest.raises(ValueError):  # create() checks any design it is given, not only ones from the API
        projects.create(str(home), "huge", design=d.with_changes({"object.diameter_mm": 5000}))
    projects.check_design(d.with_changes({"object.name": "sock", "electronics.extras": "imu"}))   # custom objects are fine


def test_uploads_have_their_own_folder_cleared_when_the_studio_starts(home):
    old, fresh = projects.upload_path(str(home), ".mp4"), projects.upload_path(str(home), "../x;.glb")
    assert os.path.dirname(old) == os.path.dirname(fresh) == str(home / ".uploads") and old.endswith(".mp4") and old != fresh
    for path in (old, fresh):
        with open(path, "wb") as fh:
            fh.write(b"data")
    os.makedirs(home / ".uploads" / "session")
    hour_ago = os.path.getmtime(old) - 3700
    for path in (old, str(home / ".uploads" / "session")):
        os.utime(path, (hour_ago, hour_ago))
    server.Studio(RobotProject(str(home / "egg-picker")), str(home))
    assert sorted(os.listdir(home / ".uploads")) == [os.path.basename(fresh)]  # one still being received survives
    assert [p["name"] for p in projects.list_projects(str(home))] == ["egg-picker"]


def test_backfill_marks_only_the_launchers_robots(home):
    make_project(home, "so101")
    make_project(home, "mine")
    (home / "so101" / "meta.json").write_text(json.dumps({"kind": "robot", "source": "so101", "demo": False}), encoding="utf-8")
    assert projects.backfill_demo(str(home)) == ["egg-picker"]
    meta = projects.read_meta(str(home / "egg-picker"))
    assert meta["demo"] is True and meta["origin"] == "default" and meta["created_utc"].endswith("Z")
    assert projects.read_meta(str(home / "so101"))["demo"] is False       # said so explicitly: left alone
    assert "demo" not in projects.read_meta(str(home / "mine"))
    assert projects.backfill_demo(str(home)) == []


# ------------------------------------------------------------------ fork and delete

def test_fork_copies_a_current_revision(home, monkeypatch):
    src = RobotProject(str(home / "egg-picker"))
    folder = src.rev_dir("r001")
    res = json.loads(open(os.path.join(folder, "results.json"), encoding="utf-8").read())
    res.update(pipeline_version=pipeline_version(), provenance={n: "computed" for n in pipeline2.ORDER},
               stage_status={n: "computed" for n in pipeline2.ORDER}, by="m-ben", label="first design")
    with open(os.path.join(folder, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    os.makedirs(os.path.join(folder, "cad"))
    with open(os.path.join(folder, "cad", "robot.glb"), "wb") as fh:
        fh.write(b"glTF")
    with open(os.path.join(folder, ".hidden"), "w", encoding="utf-8") as fh:
        fh.write("not copied")
    try:
        os.symlink(str(home.parent / "outside.txt"), os.path.join(folder, "cad", "link.txt"))
    except (OSError, NotImplementedError):
        pass
    monkeypatch.setattr(projects, "stage_problem", lambda r, n: None)
    monkeypatch.setattr(projects, "artifact_problem", lambda r, n, f: None)
    info = projects.fork(str(home), "egg-picker", by="m-ana")
    assert info["copied"] and info["rev"] == "r001" and info["name"] == "egg-picker-2"
    new = RobotProject(info["root"])
    out = new.load_results("r001")
    assert out["parent"] is None and out["label"] == "fork of egg-picker r001" and out["by"] == "m-ana"
    assert set(out["provenance"].values()) == {"copied from egg-picker r001"} and set(out["stage_status"].values()) == {"reused"}
    assert new.load_design("r001").to_dict() == src.load_design("r001").to_dict()   # the very design those files came from
    assert os.path.isfile(os.path.join(new.rev_dir("r001"), "cad", "robot.glb"))
    assert not os.path.exists(os.path.join(new.rev_dir("r001"), ".hidden"))
    assert not os.path.lexists(os.path.join(new.rev_dir("r001"), "cad", "link.txt"))
    assert info["meta"]["origin"] == "fork" and info["meta"]["forked_from"] == {"project": "egg-picker", "rev": "r001"}
    entry = new.entries()[-1]
    assert entry["type"] == "revision" and entry["forked_from"]["project"] == "egg-picker" and entry["by"] == "m-ana"


def test_fork_of_older_files_is_built_again(home):
    info = projects.fork(str(home), "egg-picker", "r001", "Copy")
    assert info["name"] == "copy" and not info["copied"] and info["rev"] is None
    assert RobotProject(info["root"]).revs() == []
    with pytest.raises(LookupError):
        projects.fork(str(home), "egg-picker", "r009")
    with pytest.raises(LookupError):
        projects.fork(str(home), "nope")


def test_delete_moves_to_the_trash(home):
    make_project(home, "mine")
    trash = projects.delete(str(home), "mine")
    assert not (home / "mine").exists() and os.path.isdir(os.path.join(trash, "r001"))
    assert os.path.dirname(trash) == str(home / ".trash") and os.path.basename(trash).startswith("mine-2")
    make_project(home, "mine")
    assert projects.delete(str(home), "mine") != trash                    # same second: a second folder
    assert [p["name"] for p in projects.list_projects(str(home))] == ["egg-picker"]   # .trash is not a project
    for bad in ("mine", "../egg-picker", ".trash"):
        with pytest.raises(LookupError):
            projects.delete(str(home), bad)


# ------------------------------------------------------------------ the Studio and its routes

@pytest.fixture
def api(home, tmp_path, monkeypatch):
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    st = server.Studio(RobotProject(str(home / "egg-picker")), str(home))
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(st, None))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(st, {f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def call(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Content-Type": "application/json", "X-Morph-Client": "1"} if method != "GET" else {}
        try:
            conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
            res = conn.getresponse()
            return res.status, json.loads(res.read())
        finally:
            conn.close()

    yield st, call
    httpd.shutdown()
    httpd.server_close()


def test_new_projects_from_the_api_build_in_their_own_room(api, builds, home):
    st, call = api
    elsewhere = st.room().hub.subscribe()                          # a tab on egg-picker
    code, out = call("POST", "/api/projects", {"from": "default", "name": "Strawberry Picker"})
    assert code == 202 and out["project"] == "strawberry-picker" and out["job"]["kind"] == "build"
    room = st.room("strawberry-picker")
    finish(room.jobs.get(out["job"]["id"]))
    assert room.p.revs() == ["r001"] and builds.built[-1][:3] == ("strawberry-picker", "r001", None)
    created = [elsewhere.get_nowait() for _ in range(elsewhere.qsize())]
    assert {"type": "project_created", "project": "strawberry-picker", "origin": "default"} in created
    assert not [e for e in created if e["type"] in ("start", "done")]  # the build streams only to its own room
    code, out = call("GET", "/api/projects")
    names = {p["name"]: p for p in out["projects"]}
    assert code == 200 and out["start"] == "egg-picker" and names["strawberry-picker"]["revs"] == 1
    code, out = call("POST", "/api/projects", {"from": "so101"})
    assert code == 202 and out["project"] == "so101"
    finish(st.room("so101").jobs.get(out["job"]["id"]))
    design = Design().with_changes({"arm.dof": 6, "object.name": "sock"}).to_dict()
    code, out = call("POST", "/api/projects", {"from": "design", "design": design, "brief_id": "b-12", "need": "socks"})
    assert code == 202 and out["project"] == "can-gripper"
    meta = projects.read_meta(str(home / "can-gripper"))
    assert meta["origin"] == "brief" and meta["brief_id"] == "b-12" and meta["need"] == "socks" and meta["demo"] is False
    for body in ({"from": "cloud"}, {"from": "design"}, {"from": "design", "design": {"arm": {"reach_mm": 900}}},
                 {"from": "design", "design": {"arm": {"source": "../x"}}}, {"name": 5}, {"brief_id": "../b"},
                 {"name": "x" * 81}):
        assert call("POST", "/api/projects", body)[0] == 400, body


def test_fork_and_delete_over_the_api(api, builds, home):
    st, call = api
    code, out = call("POST", "/api/projects/fork", {"project": "egg-picker"})
    assert code == 202 and out["project"] == "egg-picker-2" and out["copied"] is False   # fake revision: rebuilt
    finish(st.room("egg-picker-2").jobs.get(out["job"]["id"]))
    assert st.room("egg-picker-2").p.revs() == ["r001"]
    assert call("POST", "/api/projects/fork", {"project": "nope"})[0] == 404
    assert call("POST", "/api/projects/fork", {"project": "../x"})[0] == 400
    assert call("POST", "/api/projects/fork", {"project": "egg-picker", "rev": "r042"})[0] == 404
    watcher = st.room("egg-picker-2").hub.subscribe()
    code, out = call("DELETE", "/api/projects?project=egg-picker-2")
    assert code == 200 and out["trash"].startswith(".trash/egg-picker-2-")
    assert "egg-picker-2" not in st.rooms and not (home / "egg-picker-2").exists()
    assert {"type": "project_deleted", "project": "egg-picker-2"} in [watcher.get_nowait() for _ in range(watcher.qsize())]
    assert call("GET", "/api/state?project=egg-picker-2")[0] == 404
    assert call("DELETE", "/api/projects?project=egg-picker")[0] == 409          # the start project
    assert call("DELETE", "/api/projects?project=nope")[0] == 404
    assert call("DELETE", "/api/projects?project=..%2Fx")[0] == 400
    make_project(home, "busy-one")
    with st.room("busy-one").lock:
        assert call("DELETE", "/api/projects?project=busy-one")[0] == 409
    assert (home / "busy-one").exists()


# ------------------------------------------------------------------ Reset demo

def _rev(root, rev, parent, design=None):
    (root / rev).mkdir(parents=True)
    (root / rev / "results.json").write_text(json.dumps({"parent": parent, "pipeline_version": pipeline_version()}))
    (root / rev / "design.json").write_text(json.dumps(Design().with_changes(design or {}).to_dict()))


def test_reset_touches_demo_projects_only(tmp_path):
    home = tmp_path / "projects"
    for name, demo in (("egg-picker", True), ("so101", True), ("my-arm", False), ("old-style", None)):
        for r, parent, d in (("r001", None, None), ("r002", "r001", {"arm.dof": 6})):
            _rev(home / name, r, parent, d)
        meta = {"kind": "robot", **({"demo": demo} if demo is not None else {})}
        (home / name / "meta.json").write_text(json.dumps(meta))
    st = server.Studio(RobotProject(str(home / "my-arm")), str(home))
    listed = {p["name"]: p for p in st.projects()}
    assert listed["egg-picker"]["at_start"] is False and "at_start" not in listed["my-arm"]
    assert st.reset()
    assert RobotProject(str(home / "egg-picker")).revs() == ["r001"] and RobotProject(str(home / "so101")).revs() == ["r001"]
    assert RobotProject(str(home / "my-arm")).revs() == ["r001", "r002"]
    assert RobotProject(str(home / "old-style")).revs() == ["r001", "r002"]
    assert {p["name"]: p.get("at_start") for p in st.projects()}["egg-picker"] is True


def test_reset_waits_for_a_demo_build(tmp_path, builds):
    home = tmp_path / "projects"
    _rev(home / "egg-picker", "r001", None)
    (home / "egg-picker" / "meta.json").write_text(json.dumps({"kind": "robot", "demo": True}))
    st = server.Studio(RobotProject(str(home / "egg-picker")), str(home))
    builds.gate = threading.Event()
    job = st.queue_params(st.room(), {"arm.dof": 6})
    assert builds.started.wait(10)
    assert st.reset() is False
    builds.gate.set()
    finish(job)
    assert st.reset() and RobotProject(str(home / "egg-picker")).revs() == ["r001"]


# ------------------------------------------------------------------ the CLI makes projects the same way

def test_cli_robot_new_uses_the_project_service(tmp_path, monkeypatch, builds):
    home = tmp_path / "projects"
    monkeypatch.setattr(cli, "HOME", str(home))
    monkeypatch.setattr(cli, "_robot_summary", lambda p, rev: [])   # the fake build has no stage outputs to report
    ns = lambda **k: argparse.Namespace(**{"action": "new", "name": "arm-one", "need": None, "trials": 1, "fast": True,  # noqa: E731
                                           "base": None, "changes": [], "source": None, **k})
    assert cli.cmd_robot(ns()) == 0
    meta = projects.read_meta(str(home / "arm-one"))
    assert meta["origin"] == "default" and meta["demo"] is False and meta["need"].startswith("Pick a raw egg")
    assert cli.cmd_robot(ns(name="so101", demo=True)) == 0
    assert projects.read_meta(str(home / "so101"))["demo"] is True and projects.read_meta(str(home / "so101"))["source"] == "so101"
    for bad in (ns(), ns(name="../x"), ns(name="arm-two", source="../so101")):
        with pytest.raises(SystemExit):
            cli.cmd_robot(bad)
    assert not (home / "arm-two").exists() and sorted(os.listdir(home)) == ["arm-one", "so101"]
