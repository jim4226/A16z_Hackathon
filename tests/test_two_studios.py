"""Two Studios on one projects folder (a second port, a second terminal): neither removes what the other works on, nor
serves a project the other replaced under the same name. The other Studio is a real second process holding its OS
locks (test_restart_recovery.hold)."""

import json
import os
import re
import threading
import time

import pytest

from morph.robot.pipeline2 import RobotProject, pipeline_version
from morph.spec import Design
from morph.studio import projects, rooms, server
from test_restart_recovery import hold, texts
from test_studio_rooms import FakeBuilds, finish, make_project

OTHER = "feedfacecafebeef"


@pytest.fixture
def builds(monkeypatch):
    fake = FakeBuilds()
    monkeypatch.setattr(RobotProject, "run", lambda self, *a, **k: fake(self, *a, **k))
    return fake


def _rev(root, rev, parent, design=None):
    (root / rev).mkdir(parents=True)
    (root / rev / "results.json").write_text(json.dumps({"parent": parent, "pipeline_version": pipeline_version()}))
    (root / rev / "design.json").write_text(json.dumps(Design().with_changes(design or {}).to_dict()))


def _demo(home):
    for r, parent, d in (("r001", None, None), ("r002", "r001", {"arm.dof": 6}), ("r003", "r002", {"arm.dof": 5})):
        _rev(home / "egg-picker", r, parent, d)
    (home / "egg-picker" / "meta.json").write_text(json.dumps({"kind": "robot", "demo": True}))
    make_project(home, "my-arm")


@pytest.mark.parametrize("what", ["queue journal", "build lock", "study"])
def test_reset_demo_leaves_a_demo_project_another_process_works_on(tmp_path, builds, what):
    """Studio B's Reset demo removed the revision Studio A was building (A's chat: "No such file or directory ...
    design.json", then a false "saved files that can't be read" at the next start). It is refused now while another
    Studio has egg-picker queued or building, a `morph robot` build holds a revision's lock, or another process runs a
    robustness study on it; nothing is removed."""
    home = tmp_path / "projects"
    _demo(home)
    egg = RobotProject(str(home / "egg-picker"))
    if what == "queue journal":
        journal = home / "egg-picker" / ".morph" / f"queue-{OTHER}.jsonl"
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps({"op": "queued", "id": "a1b2c3d4e5f6", "kind": "params", "label": "edit",
                                       "studio": OTHER}) + "\n", encoding="utf-8")
        locks = [home / ".studios" / f"{OTHER}.lock"]
    elif what == "build lock":
        locks = [egg.build_lock("r003")]
    else:
        study = home / "egg-picker" / "r003" / "studies"
        locks = []
    other = hold(*locks) if locks else None
    try:
        if what == "study":  # a study another process runs: its record names that process
            from morph.services import robust
            path = robust._active_path(str(home / "egg-picker"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"id": "s1", "pid": os.getppid(), "host": "another-computer", "started_utc": "now"}, fh)
            assert study and robust.active(str(home / "egg-picker"))
        st = server.Studio(RobotProject(str(home / "my-arm")), str(home))
        said = ("A robustness study of egg-picker is running in another Morph Studio or a `morph study` command "
                f"(process {os.getppid()} on another-computer)" if what == "study" else
                "is building egg-picker, has changes of it waiting or runs a study")
        with pytest.raises(server.Refused, match=re.escape(said)):
            st.reset()
        assert egg.revs() == ["r001", "r002", "r003"]
    finally:
        if other is not None:
            other.stdin.close()
            other.wait(10)
    if what == "study":
        os.remove(robust._active_path(str(home / "egg-picker")))
    assert st.reset() is True and egg.revs() == ["r001"]                  # once it is done


def test_the_reset_route_says_why_it_was_refused(tmp_path, builds):
    from test_studio_features import caller, start
    home = tmp_path / "projects"
    _demo(home)
    other = hold(RobotProject(str(home / "egg-picker")).build_lock("r002"))
    try:
        st = server.Studio(RobotProject(str(home / "my-arm")), str(home))
        httpd = start(st, allowed=None)
        status, out, _ = caller(httpd)("POST", "/api/reset", {})
        httpd.shutdown()
        httpd.server_close()
    finally:
        other.stdin.close()
        other.wait(10)
    assert status == 409 and out["ok"] is False and "is building egg-picker" in out["error"]


def test_a_studio_never_deletes_another_running_studios_start_project(tmp_path, builds):
    """Studio B deleted egg-picker, the project Studio A started on, while A was idle: A then answered every message
    with "Morph kept no design ... delete the project" and refused that delete itself, and its chat writes made a ghost
    egg-picker/.morph folder that kept the name taken."""
    home = tmp_path / "projects"
    make_project(home, "egg-picker")
    make_project(home, "so101")
    record = home / ".studios" / f"{OTHER}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"start": "egg-picker"}), encoding="utf-8")
    other = hold(home / ".studios" / f"{OTHER}.lock")
    try:
        b = server.Studio(RobotProject(str(home / "so101")), str(home))
        assert json.loads((home / ".studios" / f"{rooms.OWNER}.json").read_text(encoding="utf-8"))["start"] == "so101"
        with pytest.raises(server.Refused, match="egg-picker is the project another Morph Studio on this projects "
                                                 "folder started on"):
            b.delete_project("egg-picker")
        assert (home / "egg-picker" / "r001").exists()
    finally:
        other.stdin.close()
        other.wait(10)
    b.delete_project("egg-picker")                                          # that Studio stopped: allowed again
    assert not (home / "egg-picker").exists()


def test_a_deleted_projects_room_writes_nothing_and_a_new_project_of_its_name_gets_a_fresh_room(tmp_path, builds):
    """Studio A kept a cached room for kb after Studio B deleted kb and made a new kb: A showed the deleted project's
    chat for the new one, wrote into it, and built the deleted project's kept first design as the new kb's r001."""
    home = tmp_path / "projects"
    make_project(home, "egg-picker")
    a = server.Studio(RobotProject(str(home / "egg-picker")), str(home))
    room, job = a.create_project("kb", design=Design().with_changes({"arm.dof": 6}))
    finish(job)
    finish(a.queue_chat(room, "make the gripper 10 percent stronger please"))
    room.first_build = {"design": Design().with_changes({"arm.dof": 6}).to_dict(), "label": "first design", "cut": 0}
    old_history = list(room.hub.history)
    projects.delete(str(home), "kb")                                        # Studio B deletes it ...
    room.hub.send({"type": "assistant", "text": "late reply"})              # ... A's late write makes no ghost folder
    assert not (home / "kb").exists()
    time.sleep(0.01)
    projects.create(str(home), "kb", design=Design().with_changes({"arm.dof": 5}))   # ... and makes a new kb, its
    fresh = a.room("kb")                                                    # first build not yet started
    assert fresh is not room and fresh.hub.history == [] and fresh.first_build is None and old_history
    finish(a.queue_chat(fresh, "build it again"))
    finish(*(fresh.jobs.get(j["id"]) for j in fresh.jobs.snapshot()))
    assert fresh.completed() == [] and [b[0] for b in builds.built] == ["kb"]  # never the deleted kb's design
    assert "make the gripper" not in json.dumps(texts(fresh))


def test_a_room_with_work_is_kept_whatever_its_folder_id_reads(tmp_path, builds, monkeypatch):
    """A network folder may number a folder anew between two looks: a room whose queue has work is never swapped for
    a fresh one (its build, its queue and the tabs watching it stay together); an idle one is, as for a replaced
    project."""
    home = tmp_path / "projects"
    make_project(home, "egg-picker")
    make_project(home, "kb")
    a = server.Studio(RobotProject(str(home / "egg-picker")), str(home))
    room = a.room("kb")
    builds.gate = threading.Event()
    job = a.queue_params(room, {"build.quantity": 9})
    assert builds.started.wait(5)
    monkeypatch.setattr(server, "folder_id", lambda root: ("elsewhere", 1))
    assert a.room("kb") is room                                             # building: kept
    builds.gate.set()
    finish(job)
    assert a.room("kb") is not room                                         # idle, another folder: a fresh room


def test_a_full_disk_at_the_start_costs_the_start_record_one_line(tmp_path, builds, monkeypatch, capsys):
    """The start record is written when the Studio starts; on a full disk the Studio still starts, and the terminal
    says what that costs in one line (it printed a traceback)."""
    from morph.services import durable
    home = tmp_path / "projects"
    make_project(home, "egg-picker")
    real = durable.write_json

    def write_json(path, value, *a, **k):
        if str(path).endswith(f"{rooms.OWNER}.json"):
            raise OSError(28, "No space left on device", str(path))
        return real(path, value, *a, **k)

    monkeypatch.setattr(durable, "write_json", write_json)
    server.Studio(RobotProject(str(home / "egg-picker")), str(home))
    out = capsys.readouterr()
    assert "could delete egg-picker while this one runs. The disk is full" in out.out
    assert "Traceback" not in out.out + out.err


@pytest.mark.parametrize("where", ["another Studio", "morph study", "another computer"])
def test_delete_project_is_refused_while_another_process_runs_a_study_of_it(tmp_path, builds, where):
    """Delete project stopped only this Studio's own robustness study: one that another Studio on the projects folder,
    or `morph study` in a terminal, ran on the project went on in a folder moved to the trash (Reset demo refused that
    already). It is refused now, saying where the study runs; nothing is moved."""
    import subprocess
    import sys
    from morph.services import robust
    home = tmp_path / "projects"
    make_project(home, "egg-picker")
    make_project(home, "kb")
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    other = None
    try:
        host = "lab-pc" if where == "another computer" else rooms.HOST
        if where == "another Studio":  # that Studio's start record names its process, and it holds its lock
            record = home / ".studios" / f"{OTHER}.json"
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps({"start": "egg-picker", "pid": sleeper.pid, "host": host}), encoding="utf-8")
            other = hold(home / ".studios" / f"{OTHER}.lock")
        path = robust._active_path(str(home / "kb"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"id": "s1", "pid": sleeper.pid, "host": host, "started_utc": "now"}, fh)
        assert robust.active(str(home / "kb"))
        st = server.Studio(RobotProject(str(home / "egg-picker")), str(home))
        said = {"another Studio": f"in another Morph Studio on this projects folder (process {sleeper.pid} on this "
                                  "computer)",
                "morph study": f"in a `morph study` command (process {sleeper.pid} on this computer)",
                "another computer": f"(process {sleeper.pid} on lab-pc)"}[where]
        with pytest.raises(server.Refused) as refused:
            st.delete_project("kb")
        assert str(refused.value).startswith("A robustness study of kb is running ") and said in str(refused.value), \
            str(refused.value)
        assert (home / "kb" / "r001").exists()
    finally:
        sleeper.kill()
        sleeper.wait()
        if other is not None:
            other.stdin.close()
            other.wait(10)
    os.remove(path)
    st.delete_project("kb")                                                 # once it is done
    assert not (home / "kb").exists()
