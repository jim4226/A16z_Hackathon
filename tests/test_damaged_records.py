"""Records cut short by a power cut, a crash or a full disk (ROB-2, ROB-5): a results.json that doesn't parse never stops
the launcher or a route and is never built on; every record writer syncs before it renames; a build that fails before
its results exist leaves no folder behind; a full disk reads "The disk is full"; lock files never outlive a failed write;
a revision another process is still building is never set aside."""

import argparse
import errno
import json
import os
import subprocess
import sys
import time

import pytest

from morph import cli, settings
from morph.robot import pipeline2
from morph.robot.pipeline2 import RobotProject, pipeline_version
from morph.services import durable
from morph.spec import Design
from morph.studio import projects, server
from morph.studio.routes import HTTPError
from test_studio_rooms import FakeBuilds, finish

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def robot(home, name, revs=("r001",), source=""):
    """A robot project with finished revisions whose results carry this code's pipeline_version (nothing rebuilds)."""
    root = home / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot", "source": source, "need": name, "demo": True}),
                                    encoding="utf-8")
    design = Design().with_changes({"name": name, "arm.source": source})
    for i, rev in enumerate(revs):
        (root / rev).mkdir()
        (root / rev / "design.json").write_text(design.dumps(), encoding="utf-8")
        (root / rev / "results.json").write_text(json.dumps({
            "rev": rev, "parent": revs[i - 1] if i else None, "label": "first design" if not i else "edit",
            "summary": {"dof": 5}, "pipeline_version": pipeline_version(source)}), encoding="utf-8")
    return RobotProject(str(root))


def cut(path, keep=0.5):
    raw = open(path, "rb").read()
    open(path, "wb").write(raw[:int(len(raw) * keep)])


@pytest.fixture
def builds(monkeypatch):
    fake = FakeBuilds()
    monkeypatch.setattr(RobotProject, "run", lambda self, *a, **k: fake(self, *a, **k))
    return fake


@pytest.fixture
def instant(monkeypatch):
    """The real RobotProject.run with every stage reported as not computed: a build in milliseconds."""
    monkeypatch.setattr(RobotProject, "_compute", lambda self, *a, **k: False)


# ------------------------------------------------------------------ the launcher

@pytest.mark.parametrize("damage", ["so101 cut in half", "egg-picker empty"])
def test_the_launcher_starts_with_a_damaged_results_json_in_either_demo(tmp_path, monkeypatch, capsys, damage):
    home = tmp_path / "projects"
    robot(home, "egg-picker")
    robot(home, "so101", source="so101")
    if damage == "so101 cut in half":
        cut(home / "so101" / "r001" / "results.json")
    else:
        open(home / "egg-picker" / "r001" / "results.json", "w").close()
    monkeypatch.setattr(cli, "HOME", str(home))
    from morph import keys
    monkeypatch.setattr(keys, "ensure_env_file", lambda *a, **k: False)
    rebuilt, served = [], []
    monkeypatch.setattr(cli, "cmd_robot", lambda a: rebuilt.append(a.name))
    monkeypatch.setattr(server, "serve", lambda p, *a, **k: served.append(p.root))
    monkeypatch.setattr(cli, "open_app_window", lambda url: None)
    cli.cmd_studio(argparse.Namespace(name="egg-picker", host="127.0.0.1", port=1, open=False, remote=False, team=False,
                                      allow_url_import=False))
    which = "so101" if damage.startswith("so101") else "egg-picker"
    out = capsys.readouterr().out
    assert f"{which}: r001 has saved files that can't be read" in out and "setting it aside" in out, out
    assert rebuilt == [which] and served == [str(home / "egg-picker")]   # rebuilt (here: asked to), then served
    assert [n for n in os.listdir(home / which) if n.startswith("r001.incomplete-")]


# ------------------------------------------------------------------ the Studio and its routes

def test_a_damaged_results_json_in_another_project_is_set_aside_and_never_a_500(tmp_path, builds, capsys):
    home = tmp_path / "projects"
    robot(home, "egg")
    robot(home, "proj2", revs=("r001", "r002", "r003"))
    cut(home / "proj2" / "r003" / "results.json", 1 / 3)        # the newest: set aside when the Studio starts
    open(home / "proj2" / "r001" / "results.json", "w").close()  # an older one: never listed, never shown
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    assert "proj2: r003 has saved files that can't be read" in capsys.readouterr().out
    room = st.room("proj2")
    state = st.state(room)
    assert state["revs"] == ["r002"] and state["current"]["rev"] == "r002" and state["previous"] is None
    assert [p["revs"] for p in st.projects() if p["name"] == "proj2"] == [1]
    job = st.queue_params(room, {"build.quantity": 50})
    finish(job)
    assert job.status == "done" and builds.built[-1][1:3] == ("r003", "r002")   # built on r002, numbered after it


def test_a_revision_damaged_while_the_studio_runs_is_skipped_not_a_500(tmp_path, builds):
    home = tmp_path / "projects"
    robot(home, "egg", revs=("r001", "r002"))
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    cut(home / "egg" / "r002" / "design.json")
    state = st.state()
    assert state["revs"] == ["r001"] and state["current"]["rev"] == "r001"


def test_state_falls_back_when_a_revision_cant_be_shown(tmp_path, builds, monkeypatch):
    home = tmp_path / "projects"
    robot(home, "egg", revs=("r001", "r002"))
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    real = server.rev_payload
    monkeypatch.setattr(server, "rev_payload", lambda p, rev, project=None: (_ for _ in ()).throw(KeyError("odd"))
                        if rev == "r002" else real(p, rev, project))
    state = st.state()
    assert state["current"]["rev"] == "r001"


def test_a_build_never_starts_on_a_parent_it_cant_read(tmp_path, instant):
    p = robot(tmp_path / "projects", "egg", revs=("r001",))
    cut(os.path.join(p.rev_dir("r001"), "results.json"))
    with pytest.raises(ValueError, match="r001's saved files can't be read"):
        p.run(Design().with_changes({"build.quantity": 50}), parent="r001", verbose=False)
    assert p.revs() == ["r001"] and not [n for n in os.listdir(p.root) if n.startswith("r002")]


def test_a_build_that_fails_before_its_results_leaves_no_folder(tmp_path, instant, monkeypatch):
    p = RobotProject(str(tmp_path / "projects" / "egg"))
    rev = p.run(Design(), label="first", verbose=False)
    assert rev == "r001" and p.finished() == ["r001"] and not os.path.exists(p.build_lock("r001"))
    real = pipeline2.write_text

    def full(path, text, **kw):
        if path.endswith("results.json"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, text, **kw)
    monkeypatch.setattr(pipeline2, "write_text", full)
    with pytest.raises(OSError):
        p.run(Design().with_changes({"build.quantity": 50}), parent="r001", verbose=False)
    assert not any(n.startswith("r002") for n in os.listdir(p.root)), os.listdir(p.root)
    assert p.revs() == ["r001"] and not os.path.exists(p.build_lock("r002"))


def test_a_full_disk_reads_the_disk_is_full_in_the_chat(tmp_path, monkeypatch):
    home = tmp_path / "projects"
    robot(home, "egg")
    monkeypatch.setattr(RobotProject, "run", lambda self, *a, **k: (_ for _ in ()).throw(
        OSError(errno.ENOSPC, "No space left on device")))
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    room = st.room()
    finish(st.queue_params(room, {"build.quantity": 50}))
    assert room.hub.history[-1]["text"] == "Error: The disk is full: free some space on this computer, then try again."


def test_routes_answer_507_the_disk_is_full(tmp_path, monkeypatch):
    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(settings, "save", full)
    with pytest.raises(HTTPError) as exc:
        server._api_settings_save(type("R", (), {"body": {"values": {"network_enabled": False}}})())
    assert exc.value.status == 507 and exc.value.message == durable.DISK_FULL
    from morph.studio import api_team
    status, out = api_team._safe(full)(None)
    assert status == 507 and out["error"] == durable.DISK_FULL


# ------------------------------------------------------------------ writers sync before they rename

PROC = os.path.isdir("/proc/self/fd")


@pytest.mark.skipif(not PROC, reason="names each synced file through /proc (Linux)")
def test_every_record_writer_syncs_the_file_before_the_rename(tmp_path, monkeypatch, instant):
    synced, real = [], os.fsync

    def fsync(fd):
        try:
            synced.append(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            synced.append(fd)
        return real(fd)
    monkeypatch.setattr(os, "fsync", fsync)
    home = tmp_path / "projects"
    settings.save({"network_enabled": False})
    projects.write_meta(str(tmp_path), {"kind": "robot"})
    p = RobotProject(str(home / "egg"))
    p.run(Design(), label="first", verbose=False)
    from morph.services import robust
    robust._write_json(str(tmp_path / "study.json"), {"id": "x"})
    from morph.studio.teams import store as team_store
    from morph.studio.toolbox import store as tool_store
    team_store.write_json(str(tmp_path / "team.json"), {"members": {}})
    tool_store.write_json(str(tmp_path / "state.json"), {"enabled": {}})
    names = {os.path.basename(str(s)) for s in synced}
    tmp = [n for n in names if n.startswith(".tmp.")]
    assert len(tmp) >= 7, names          # every one through a synced temp file (design, results, meta, settings, ...)
    assert {"egg", "projects", os.path.basename(str(tmp_path))} & names   # and the folder synced after the rename


# ------------------------------------------------------------------ lock files

@pytest.mark.parametrize("mod,cls", [("morph.studio.teams.store", "FileLock"), ("morph.studio.toolbox.store", "_FileLock")])
def test_a_lock_whose_first_write_fails_leaves_nothing_behind(tmp_path, monkeypatch, mod, cls):
    import importlib
    Lock = getattr(importlib.import_module(mod), cls)
    path = str(tmp_path / ".lock")
    real, fails = os.write, [1]

    def write(fd, data):
        if fails:
            fails.pop()
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(fd, data)
    monkeypatch.setattr(os, "write", write)
    before = len(os.listdir("/proc/self/fd")) if PROC else None
    with pytest.raises(OSError):
        with Lock(path, timeout=1.0):
            pass
    assert not os.path.exists(path), "the .lock file was left behind"
    if PROC:
        assert len(os.listdir("/proc/self/fd")) == before, "a descriptor leaked"
    t0 = time.monotonic()
    with Lock(path, timeout=1.0):
        pass
    assert time.monotonic() - t0 < 0.5, "the next writer waited for a lock nobody holds"


# ------------------------------------------------------------------ the start-up sweep

def test_the_sweep_leaves_a_build_another_process_is_running(tmp_path):
    p = robot(tmp_path / "projects", "egg", revs=("r001",))
    os.mkdir(p.rev_dir("r002"))
    (open(os.path.join(p.rev_dir("r002"), "design.json"), "w")).write(Design().dumps())
    holder = subprocess.Popen([sys.executable, "-c", (
        "import sys, time; from morph.services.durable import BuildLock\n"
        "lock = BuildLock(sys.argv[1]); assert lock.acquire(); print('held', flush=True); time.sleep(60)"),
        p.build_lock("r002")], cwd=CODE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert projects.set_aside_unfinished(p) == [] and p.revs() == ["r001", "r002"]
    finally:
        holder.kill()
        holder.wait(10)
    said = []
    assert projects.set_aside_unfinished(p, say=said.append) == ["r002"] and p.revs() == ["r001"]
    assert said == ["egg: r002 was cut off mid-build; setting it aside"]


def test_two_builds_never_claim_the_same_number_and_skip_a_held_one(tmp_path):
    p = RobotProject(str(tmp_path / "projects" / "egg"))
    held = durable.BuildLock(p.build_lock("r001"))
    assert held.acquire()                         # another build is about to make r001
    rev, lock = p.claim_build()
    assert rev == "r002" and os.path.isdir(p.rev_dir("r002"))
    lock.release()
    held.release()
    assert not durable.BuildLock.held(p.build_lock("r002"))


def test_old_set_aside_builds_go_to_the_trash_and_deleted_leftovers_go(tmp_path):
    home = tmp_path / "projects"
    p = robot(home, "egg", revs=("r001",))
    day = 24 * 3600
    old, new = int(time.time()) - 2 * day, int(time.time()) - 60
    for name in (f"r002.incomplete-{old}", f"r003.incomplete-{new}", f"r004.deleted-{time.time_ns()}"):
        os.makedirs(os.path.join(p.root, name, "cad"))
        open(os.path.join(p.root, name, "cad", "robot.glb"), "wb").write(b"x" * 1000)
    said = []
    out = projects.tidy(str(home), say=said.append)
    left = sorted(os.listdir(p.root))
    assert f"r003.incomplete-{new}" in left and not any(n.startswith(("r002.", "r004.")) for n in left)
    trash = os.listdir(home / ".trash")
    assert len(trash) == 1 and trash[0].startswith("egg-r002-")
    assert out["trashed"] == 1 and "to " in said[-1] and "empty it to free the space" in said[-1]
    assert ", <1 MB, to " in said[-1] and "0 MB" not in said[-1]          # 1 kB: never "0 MB"


def test_a_build_error_in_the_chat_names_files_from_the_projects_folder(tmp_path):
    """A browser is told which file failed, never where the server keeps its files."""
    home = tmp_path / "projects"
    exc = PermissionError(13, "Permission denied", str(home / "kb1" / "r002" / "cad" / "base.stl"))
    told = durable.explain(exc, str(home))
    assert str(tmp_path) not in told and told.startswith("[Errno 13] Permission denied: 'projects/kb1")
    assert durable.explain(OSError(28, "No space left on device"), str(home)) == durable.DISK_FULL


def test_errors_a_browser_sees_name_files_from_the_projects_folder(tmp_path, monkeypatch, capsys):
    """A job that raised, a chat message that couldn't be read, Find agents and a robustness study that failed with an
    OSError, and a route that hit one, tell the browser which file (from the projects folder) and never where the
    server keeps it; the terminal keeps the whole error."""
    from morph import darwin
    from morph.services import robust
    from morph.studio import jobs, rooms
    home = tmp_path / "projects"
    robot(home, "egg")
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    egg, where = st.room("egg"), str(home / "egg" / "r001" / "bom.json")
    oops = lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "Permission denied", where))  # noqa: E731
    seen = []
    monkeypatch.setattr(egg.hub, "send", lambda ev: seen.append(ev))

    job = egg.jobs.submit("import", oops, label="an import")                   # a job that raised
    finish(job)
    monkeypatch.setattr(server, "interpret", oops)                               # a chat message
    finish(st.queue_chat(egg, "make it lighter"))
    monkeypatch.setattr(darwin, "source", oops)                                  # Find agents
    st.source(room=egg)
    told = [str(ev.get("text") or ev.get("error") or "") for ev in seen if ev.get("type") in ("assistant", "sourcing")]
    told = [t for t in told if "Permission denied" in t]
    assert len(told) == 3 and job.error.startswith("PermissionError: [Errno 13]"), told
    for text in told + [job.error]:
        assert "projects/egg/r001/bom.json" in text and str(tmp_path) not in text, text

    sent = []                                                                    # a robustness study
    hj = jobs.HeavyJobs(str(home), emit=lambda project, ev: sent.append(ev), max_running=1)
    monkeypatch.setattr(robust, "new_study", lambda root, rev=None, runs=200, **kw: {
        "version": robust.VERSION, "id": "a1b2c3d4e5f6", "project": "egg", "rev": "r001", "n": runs, "seed": 1,
        "stress": False, "timestep": 0.001, "status": "queued", "done": 0})
    monkeypatch.setattr(robust, "run_study", oops)
    monkeypatch.setattr(robust, "active", lambda root: None)
    hj.start_study("egg", runs=10)
    for _ in range(200):
        if any(e.get("status") == "error" for e in sent):
            break
        time.sleep(0.02)
    err = next(e["error"] for e in sent if e.get("status") == "error")
    assert "projects/egg/r001/bom.json" in err and str(tmp_path) not in err, err
    hj.shutdown(timeout=1)

    monkeypatch.setattr(rooms.JobQueue, "snapshot", oops)                       # a route that hit one
    from test_studio_features import caller, start
    httpd = start(st)
    try:
        status, out, _ = caller(httpd)("GET", "/api/jobs?project=egg")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 500 and "projects/egg/r001/bom.json" in out["error"] and str(tmp_path) not in out["error"]
    assert where in capsys.readouterr().err                                     # the terminal: the whole path
