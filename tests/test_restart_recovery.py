"""A Studio that stops with work open (Ctrl+C, a closed window, a crash) picks it up when it starts again (ROB-1).

Each test runs a Studio until a build is under way (FakeBuilds holds it at its gate), copies the projects folder at that
moment (what a killed Studio leaves on disk: the queue journal, a revision with only design.json, the Teams and study
records), then starts a new Studio on the copy. tests/e2e/test_e2e_restart.py does the same with a real Studio and
SIGINT."""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from morph.robot.pipeline2 import RobotProject
from morph.services import robust
from morph.spec import Design
from morph.studio import api_brief, api_team, jobs, rooms, server
from morph.studio import brief as B
from morph.studio.teams import activity, collab
from test_studio_rooms import FakeBuilds, finish, make_project

REBUILT = " (rebuilt after a stop)"


@pytest.fixture
def builds(monkeypatch):
    fake = FakeBuilds()
    monkeypatch.setattr(RobotProject, "run", lambda self, *a, **k: fake(self, *a, **k))
    return fake


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "projects"
    make_project(h, "egg")
    make_project(h, "so101")
    return h


def stopped(home, tmp_path, builds):
    """The projects folder as a Studio killed right now leaves it; then the stopped Studio's builds are let go."""
    copy = tmp_path / "after-stop"
    shutil.copytree(home, copy)
    return copy


def settled():
    """Wait for the follow-ups features run after a build (when_done threads: the brief's after-build agents)."""
    for t in threading.enumerate():
        if t.name.startswith("morph-after-"):
            t.join(10)


def texts(room):
    return [h["text"] for h in room.hub.history if h["type"] == "assistant"]


def test_a_new_projects_cut_off_first_build_is_built_again(home, tmp_path, builds):
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    builds.gate = threading.Event()
    room, job = st.create_project("kb1", need="a test")
    assert builds.started.wait(5)
    after = stopped(home, tmp_path, builds)
    builds.gate.set()
    finish(job)
    assert os.listdir(after / "kb1" / "r001") == ["design.json"]          # cut off mid-build
    builds.gate = threading.Event()
    st2 = server.Studio(RobotProject(str(after / "egg")), str(after))
    kb1 = st2.room("kb1")
    queued = [kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()]
    assert kb1.jobs.busy() and kb1.jobs.snapshot()[0]["label"] == "first design" + REBUILT
    builds.gate.set()
    finish(*queued)
    assert kb1.completed() == ["r001"] and RobotProject(str(after / "kb1")).load_results("r001")["label"] == "first design" + REBUILT
    assert "The Studio stopped before kb1's first build finished: building it again now." in texts(kb1)
    assert [n for n in os.listdir(after / "kb1") if n.startswith("r001.incomplete-")]   # the cut-off folder, set aside
    assert not rooms.journals(str(after / "kb1"))                                     # nothing open: no journal


def test_a_first_build_still_waiting_for_its_turn_is_built_too(home, tmp_path, builds):
    """No revision folder yet (the build slot was busy): only the journal knows the design."""
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    builds.gate = threading.Event()
    hold = st.queue_params(st.room(), {"build.quantity": 40})             # takes the only build slot
    assert builds.started.wait(5)
    _, job = st.create_project("kb2", design=Design().with_changes({"build.quantity": 7}))
    after = stopped(home, tmp_path, builds)
    builds.gate.set()
    finish(hold, job)
    assert not RobotProject(str(after / "kb2")).revs()
    st2 = server.Studio(RobotProject(str(after / "egg")), str(after))
    kb2 = st2.room("kb2")
    finish(*(kb2.jobs.get(j["id"]) for j in kb2.jobs.snapshot()))
    assert kb2.completed() == ["r001"] and kb2.p.load_design("r001").build.quantity == 7


def test_queued_edits_are_named_in_the_chat_never_rerun(home, tmp_path, builds):
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    builds.gate = threading.Event()
    egg = st.room()
    running = st.queue_params(egg, {"object.mass_g": 70})
    assert builds.started.wait(5)
    waiting = st.queue_chat(egg, "use a quantity of 50")
    after = stopped(home, tmp_path, builds)
    builds.gate.set()
    finish(running, waiting)
    st2 = server.Studio(RobotProject(str(after / "egg")), str(after))
    egg2 = st2.room()
    said = [t for t in texts(egg2) if t.startswith("The Studio stopped before this was built")]
    assert said == ["The Studio stopped before this was built: “Object mass → 70 g”. Send it again if you still want it.",
                    "The Studio stopped before this was built: “use a quantity of 50”. Send it again if you still want it."]
    assert not egg2.jobs.busy() and egg2.completed() == ["r001"]          # nothing re-run on its own
    assert [n for n in os.listdir(after / "egg") if n.startswith("r002.incomplete-")]
    st3 = server.Studio(RobotProject(str(after / "egg")), str(after))      # told once, not at every start
    assert sum(t.startswith("The Studio stopped") for t in texts(st3.room())) == 2


def test_an_edit_whose_revision_was_written_just_before_the_stop_is_not_called_lost(home, tmp_path, builds):
    """The revision is on disk, then the journal's "ended" line: a stop between the two must not say it wasn't built."""
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    egg = st.room()
    job = st.queue_params(egg, {"object.mass_g": 70})
    finish(job)
    journal = home / "egg" / ".morph" / "queue.jsonl"
    entry = {"op": "queued", "id": job.id, "kind": "params", "label": job.label, "by": None, "base_rev": "r001",
             "ts": job.created, "pid": os.getpid(), "host": rooms.HOST}
    journal.write_text(json.dumps(entry) + "\n", encoding="utf-8")         # as if "ended" never reached the disk
    st2 = server.Studio(RobotProject(str(home / "egg")), str(home))
    assert not any(t.startswith("The Studio stopped") for t in texts(st2.room())) and st2.room().completed() == ["r001", "r002"]
    again = {**entry, "id": "j-again"}                                    # the same edit sent again, still waiting
    journal.write_text(json.dumps(entry) + "\n" + json.dumps(again) + "\n", encoding="utf-8")
    st3 = server.Studio(RobotProject(str(home / "egg")), str(home))
    stopped = [t for t in texts(st3.room()) if t.startswith("The Studio stopped")]
    assert len(stopped) == 1 and "Object mass" in stopped[0]              # one revision accounts for one job


def test_a_conflict_waiting_for_an_answer_waits_again_after_a_restart(home, tmp_path, builds):
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    egg = st.room()
    finish(st.queue_params(egg, {"arm.dof": 6}, by="m_ana"))
    late = st.queue_params(egg, {"arm.dof": 4}, base_rev="r001", by="m_raj")
    finish(late)
    assert late.status == "conflict"
    after = stopped(home, tmp_path, builds)
    st2 = server.Studio(RobotProject(str(after / "egg")), str(after))
    egg2 = st2.room()
    waiting = egg2.jobs.conflicts()
    assert [(c["id"], c["status"], c["conflict"]["keys"]) for c in waiting] == [(late.id, "conflict", ["arm.dof"])]
    assert st2.state(egg2)["conflicts"] == waiting                        # the page draws Apply / Drop for it again
    new = st2.resolve(egg2, late.id, "apply", by="m_raj")
    finish(new)
    assert new.status == "done" and egg2.p.load_design(egg2.latest()).arm.dof == 4 and not egg2.jobs.conflicts()


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def hold(*paths):
    """Another process (another Studio) that holds the OS locks on `paths` until its stdin closes."""
    code = ("import sys; from morph.services import durable; locks = [durable.BuildLock(a) for a in sys.argv[1:]]; "
            "print('held' if all(k.acquire() and k.real for k in locks) else 'not held', flush=True); sys.stdin.read()")
    proc = subprocess.Popen([sys.executable, "-c", code, *map(str, paths)], cwd=ROOT, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    assert proc.stdout.readline().strip() == b"held"
    return proc


def cut_off_first_build(home, name="kb1", owner="0123456789abcdef", **line):
    """What a Studio killed during `name`'s first build leaves: r001 with only its design, and its queue journal."""
    root = home / name
    (root / "r001").mkdir(parents=True)
    (root / ".morph").mkdir()
    (root / "meta.json").write_text(json.dumps({"kind": "robot", "source": "", "need": name}), encoding="utf-8")
    design = Design().with_changes({"build.quantity": 5})
    (root / "r001" / "design.json").write_text(design.dumps(), encoding="utf-8")
    entries = [{"op": "queued", "id": "a1b2c3d4e5f6", "kind": "build", "label": "first design", "studio": owner,
                "pid": os.getpid(), "host": rooms.HOST, "ts": time.time(), "design": design.to_dict(), **line},
               {"op": "started", "id": "a1b2c3d4e5f6", "studio": owner}]
    journal = root / ".morph" / f"queue-{owner}.jsonl"
    journal.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return journal


def test_a_journal_another_running_studio_writes_is_left_alone(home, tmp_path, builds, capsys):
    """Two Studios on one projects folder: the other one holds its lock (rooms.claim), so its queue is its own; once
    it has stopped, however it stopped, its lock is free and the next Studio picks its work up."""
    journal = cut_off_first_build(home, owner="feedfacecafebeef")
    other = hold(home / ".studios" / "feedfacecafebeef.lock", RobotProject(str(home / "kb1")).build_lock("r001"))
    try:
        st = server.Studio(RobotProject(str(home / "egg")), str(home))
        assert "kb1: another Morph Studio on this projects folder is still working on it" in capsys.readouterr().out
        assert not st.room("kb1").jobs.busy() and journal.exists()
        assert os.path.isdir(home / "kb1" / "r001")                         # its build is its own (tidy: BuildLock)
    finally:
        other.stdin.close()
        other.wait(10)
    shutil.rmtree(home / "kb1" / "r001")                                    # what tidy() sets aside once it has stopped
    st2 = server.Studio(RobotProject(str(home / "egg")), str(home))
    kb1 = st2.room("kb1")
    finish(*(kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()))
    assert kb1.completed() == ["r001"] and not journal.exists()
    assert not os.path.exists(home / ".studios" / "feedfacecafebeef.lock") or \
        time.time() - os.path.getmtime(home / ".studios" / "feedfacecafebeef.lock") < rooms.STALE_LOCK_S


@pytest.mark.parametrize("line", [{"host": "Johns-MacBook-Pro.local"},   # a Mac's name changes with its network
                                  {"pid": os.getppid()},                   # its pid now belongs to a live process
                                  {"pid": 1, "host": "another-computer"}])
def test_a_stopped_studios_journal_is_recovered_whatever_its_pid_and_host_say(home, builds, capsys, line):
    """Only the OS lock of the Studio that wrote a journal tells whether it still runs: after a reboot its pid may
    belong to another process, and a computer's name can change; neither may leave a project empty for good."""
    cut_off_first_build(home, **line)
    builds.gate = threading.Event()
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    assert "still working on it" not in capsys.readouterr().out
    kb1 = st.room("kb1")
    queued = [kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()]
    assert [j.label for j in queued] == ["first design" + REBUILT]
    builds.gate.set()
    finish(*queued)
    assert kb1.completed() == ["r001"] and kb1.p.load_design("r001").build.quantity == 5


def test_a_first_build_the_studio_stopped_during_twice_waits_to_be_asked_for_again(home, tmp_path, builds, capsys):
    """A first build that itself stops the Studio (memory, a crash in a native library) must not stop it at every
    start: it is built again once, and cut off a second time it waits until someone asks for it."""
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    builds.gate = threading.Event()
    room, job = st.create_project("kb1", need="a test")
    assert builds.started.wait(5)
    after = stopped(home, tmp_path, builds)                               # cut off once
    builds.gate.set()
    finish(job)
    builds.gate, builds.started = threading.Event(), threading.Event()
    st2 = server.Studio(RobotProject(str(after / "egg")), str(after))
    rebuilt = [st2.room("kb1").jobs.get(j["id"]) for j in st2.room("kb1").jobs.snapshot()]
    assert [j.label for j in rebuilt] == ["first design" + REBUILT] and builds.started.wait(5)
    again = tmp_path / "after-second-stop"
    shutil.copytree(after, again)                                          # cut off again, mid-build
    builds.gate.set()
    finish(*rebuilt)
    capsys.readouterr()
    st3 = server.Studio(RobotProject(str(again / "egg")), str(again))
    kb1 = st3.room("kb1")
    assert not kb1.jobs.busy() and kb1.completed() == []                    # not started on its own
    out = capsys.readouterr().out
    assert "kb1: the Studio stopped twice while building its first design, so Morph won't start it again" in out
    assert 'send "build it again" in kb1\'s chat' in out
    said = texts(kb1)[-1]
    assert said.startswith("The Studio stopped twice while building kb1's first design") and "delete kb1" in said
    assert (again / "kb1" / ".morph" / "first-build.json").exists() and not rooms.journals(str(again / "kb1"))
    finish(st3.queue_params(kb1, {"build.quantity": 4}))
    assert 'send "build it again"' in texts(kb1)[-1]                        # an edit says how to go on
    capsys.readouterr()
    st4 = server.Studio(RobotProject(str(again / "egg")), str(again))      # every start says so in the terminal
    assert "kb1: the Studio stopped twice" in capsys.readouterr().out and not st4.room("kb1").jobs.busy()
    kb1 = st4.room("kb1")
    finish(st4.queue_chat(kb1, "build it again"))
    finish(*(kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()))
    assert kb1.completed() == ["r001"] and not (again / "kb1" / ".morph" / "first-build.json").exists()
    assert "Building kb1's first design again." in texts(kb1)


@pytest.mark.parametrize("ends", [KeyboardInterrupt(), None, MemoryError(), RuntimeError("accept loop")],
                         ids=["ctrl-c", "returns", "memory-error", "error"])
def test_only_a_clean_stop_of_serve_journals_its_first_builds_as_no_try(home, monkeypatch, ends):
    """serve() calls Studio.stopping (a first build it cuts off is no try) on Ctrl+C and when serve_forever returns;
    an error escaping serve_forever is no clean stop, so a first build it cut off still counts. Studies stop either
    way (jobs.shutdown)."""
    from morph.studio import capability
    calls = []

    class Server:
        def __init__(self, address, handler):
            pass

        def serve_forever(self):
            if ends is not None:
                raise ends

    monkeypatch.setattr(server, "_Server", Server)
    monkeypatch.setattr(capability, "warm", lambda block=True: None)
    monkeypatch.setattr(server.Studio, "stopping", lambda self: calls.append("stopping"))
    monkeypatch.setattr(jobs, "shutdown", lambda studio, timeout=5.0, hurry=None: calls.append("studies stopped"))
    run = lambda: server.serve(RobotProject(str(home / "egg")), port=0, home=str(home))  # noqa: E731
    if isinstance(ends, Exception):
        with pytest.raises(type(ends)):
            run()
        assert calls == ["studies stopped"]
    else:
        run()
        assert calls == ["stopping", "studies stopped"]


def test_clean_stops_during_a_first_build_are_no_tries(home, tmp_path, builds):
    """Ctrl+C (serve() returning) during a long first build, twice: a person closing the Studio is no sign the build
    stops it, so it is built again at each start and never waits to be asked for. A crash after that still counts."""
    st, where = server.Studio(RobotProject(str(home / "egg")), str(home)), home
    builds.gate = threading.Event()
    room, job = st.create_project("kb1", need="a test")
    jobs = [job]
    for n in range(1, 4):
        assert builds.started.wait(5)
        st.stopping()                                                       # Ctrl+C: what serve() calls
        after = tmp_path / f"after-clean-stop-{n}"
        shutil.copytree(where, after)
        builds.gate.set()
        finish(*jobs)
        builds.gate, builds.started = threading.Event(), threading.Event()
        st, where = server.Studio(RobotProject(str(after / "egg")), str(after)), after
        kb1 = st.room("kb1")
        jobs = [kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()]
        assert [j.label for j in jobs] == ["first design" + REBUILT], n     # built again, never parked
        assert not (after / "kb1" / ".morph" / "first-build.json").exists()
        assert "stopped twice" not in " ".join(texts(kb1))
    assert builds.started.wait(5)
    crashed = stopped(where, tmp_path, builds)                              # no stopping(): a crash, a kill
    builds.gate.set()
    finish(*jobs)
    builds.gate = threading.Event()
    kb1 = server.Studio(RobotProject(str(crashed / "egg")), str(crashed)).room("kb1")
    assert len(kb1.jobs.snapshot()) == 1                                    # one cut: built again once more
    builds.gate.set()
    finish(*(kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()))
    assert kb1.completed() == ["r001"]


def test_build_it_again_while_it_builds_is_answered_at_once(home, builds):
    """A second "build it again" sent while the first one's build runs is answered now ("already being built"); queued,
    it ran after the build and was read as an edit ("I couldn't map that to a design parameter")."""
    cut_off_first_build(home, cut=1)
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    kb1 = st.room("kb1")
    assert not kb1.jobs.busy()                                              # parked
    builds.gate = threading.Event()
    finish(st.queue_chat(kb1, "build it again"))
    assert builds.started.wait(5)
    building = [kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()]
    again = st.queue_chat(kb1, "Build it again!")
    assert again.wait(0) and again.status == "noop"
    assert texts(kb1)[-1] == "kb1's first design is already being built."
    assert [j["id"] for j in kb1.jobs.snapshot()] == [b.id for b in building]   # nothing more queued
    builds.gate.set()
    finish(*building)
    assert kb1.completed() == ["r001"]
    assert not any("couldn't map" in t for t in texts(kb1))


def test_build_it_again_retries_a_first_build_that_ended_with_an_error(home, tmp_path, monkeypatch, capsys):
    """A first build that ended with an error (not a stop) leaves no revision. "build it again" tries it once more
    (it was answered "Wait for its first build", a build that would never come), an edit says how to go on, and a
    Studio started later still can."""
    fake, tries = FakeBuilds(), []

    def flaky(self, design, *a, **k):
        tries.append(design.name)
        if len(tries) == 1:
            raise PermissionError(13, "Permission denied", str(home / "kb1" / "r001" / "cad"))
        return fake(self, design, *a, **k)

    monkeypatch.setattr(RobotProject, "run", flaky)
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    room, job = st.create_project("kb1", need="a test")
    finish(job)
    assert job.status == "error" and room.completed() == [] and "projects/kb1/r001/cad" in texts(room)[-1]
    finish(st.queue_params(room, {"build.quantity": 4}))                    # an edit: nothing to change yet
    said = texts(room)[-1]
    assert 'send "build it again"' in said and "isn't running" in said and "Wait for" not in said, said
    later = tmp_path / "later"
    shutil.copytree(home, later)
    capsys.readouterr()
    st2 = server.Studio(RobotProject(str(later / "egg")), str(later))     # a later start: the terminal says so too
    out = capsys.readouterr().out
    assert "kb1: its first build ended with an error" in out and 'send "build it again" in kb1\'s chat' in out
    assert "stopped" not in out.split("kb1: ", 1)[1].splitlines()[0] and not st2.room("kb1").jobs.busy()
    finish(st.queue_chat(room, "build it again"))                           # this Studio: tried once more
    finish(*(room.jobs.get(j["id"]) for j in room.jobs.snapshot()))
    assert room.completed() == ["r001"] and "Building kb1's first design again." in texts(room)
    assert len(tries) == 2 and not (home / "kb1" / ".morph" / "first-build.json").exists()
    kb1 = st2.room("kb1")                                                   # the later Studio, from the kept design
    finish(st2.queue_chat(kb1, "Build it again!"))
    finish(*(kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()))
    assert kb1.completed() == ["r001"] and len(tries) == 3


def test_an_empty_project_with_nothing_kept_to_build_says_so(home, builds):
    """No first build running, waiting or kept (one that failed under an older Morph): the chat never says to wait for
    a first build that will never come."""
    from morph.studio import projects
    projects.create(str(home), "kb1")                                      # a project folder, never built
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    kb1 = st.room("kb1")
    for job in (st.queue_chat(kb1, "build it again"), st.queue_params(kb1, {"build.quantity": 4})):
        finish(job)
        said = texts(kb1)[-1]
        assert job.status == "error" and "kept no design" in said and "delete the project" in said, said
    assert builds.built == [] and kb1.completed() == []


ELSEWHERE = "Another Morph Studio on this projects folder is building kb1's first design; wait for it there."


def test_a_first_build_another_running_studio_builds_is_left_to_it(home, builds):
    """Two Studios on one projects folder: while the other one builds kb1's first design, this one says so to an edit
    and to "build it again", queues no second first build, never advises deleting the project, and refuses Delete
    project (it said "Morph kept no design ... delete the project", and deleting moved kb1 to .trash mid-build)."""
    journal = cut_off_first_build(home, owner="feedfacecafebeef")          # its journal: queued, started
    other = hold(home / ".studios" / "feedfacecafebeef.lock", RobotProject(str(home / "kb1")).build_lock("r001"))
    try:
        st = server.Studio(RobotProject(str(home / "egg")), str(home))
        kb1 = st.room("kb1")
        for ask in (lambda: st.queue_chat(kb1, "make it lighter"), lambda: st.queue_chat(kb1, "build it again"),
                    lambda: st.queue_params(kb1, {"build.quantity": 4})):
            finish(ask())
            assert texts(kb1)[-1] == ELSEWHERE and not kb1.jobs.busy()
        assert not any("delete" in t.lower() or "kept no design" in t for t in texts(kb1))
        with pytest.raises(server.Refused, match="Another Morph Studio on this projects folder"):
            st.delete_project("kb1")
        assert journal.exists() and (home / "kb1" / "r001" / "design.json").exists() and not (home / ".trash").exists()
        assert builds.built == []
    finally:
        other.stdin.close()
        other.wait(10)


def test_build_it_again_in_a_second_studio_while_the_first_rebuilds_queues_nothing(home, builds):
    """kb1's first build ended with an error; Studio A got "build it again" and rebuilds it. Studio B getting "build
    it again" too queued a second build: two parentless first revisions, r001 and r002."""
    from morph.studio import projects
    cut_off_first_build(home, owner="feedfacecafebeef")                    # A's rebuild, under way
    projects.park_first_build(str(home / "kb1"), {"design": Design().to_dict(), "label": "first design", "by": None,
                                                  "cut": 0, "error": "[Errno 13] Permission denied"})
    other = hold(home / ".studios" / "feedfacecafebeef.lock", RobotProject(str(home / "kb1")).build_lock("r001"))
    try:
        st = server.Studio(RobotProject(str(home / "egg")), str(home))
        kb1 = st.room("kb1")
        for msg in ("build it again", "make it lighter"):
            job = st.queue_chat(kb1, msg)
            finish(job)
            assert texts(kb1)[-1] == ELSEWHERE and not kb1.jobs.busy(), texts(kb1)
        assert builds.built == [] and job.status == "error"
    finally:
        other.stdin.close()
        other.wait(10)


@pytest.mark.parametrize("meanwhile", ["queued", "built"])
def test_two_studios_asked_to_build_it_again_at_once_build_it_once(home, builds, meanwhile):
    """Whether to queue a first build is decided by one Studio at a time (an OS lock in the project): a Studio that
    waited for it reads what the other one did meanwhile (queued the build, or even finished it) and queues nothing."""
    from morph.studio import projects
    root = home / "kb1"
    (root / ".morph").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot", "source": "", "need": "kb1"}), encoding="utf-8")
    projects.park_first_build(str(root), {"design": Design().to_dict(), "label": "first design", "by": None, "cut": 0,
                                          "error": "[Errno 13] Permission denied"})
    other = hold(home / ".studios" / "feedfacecafebeef.lock")              # the other Studio runs ...
    deciding = hold(root / ".morph" / "first-build.lock")                   # ... and is deciding right now
    try:
        st = server.Studio(RobotProject(str(home / "egg")), str(home))
        kb1 = st.room("kb1")
        job = st.queue_chat(kb1, "build it again")
        assert not job.wait(0.5)                                            # waits for its turn to decide
        if meanwhile == "queued":                                           # ... the other one queued its build
            entry = {"op": "queued", "id": "a1b2c3d4e5f6", "kind": "build", "label": "first design",
                     "studio": "feedfacecafebeef", "ts": time.time(), "design": Design().to_dict()}
            (root / ".morph" / "queue-feedfacecafebeef.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
        else:                                                               # ... and even built it
            make_project(home, "kb1")
        deciding.stdin.close()
        deciding.wait(10)
        finish(job)
        said = texts(kb1)[-1]
        assert said == (ELSEWHERE if meanwhile == "queued" else "kb1's first design has just been built (r001).")
        assert not kb1.jobs.busy() and builds.built == []
    finally:
        for proc in (deciding, other):
            if proc is not None and proc.poll() is None:
                proc.stdin.close()
                proc.wait(10)


def test_a_first_build_a_studio_that_stopped_meanwhile_left_is_built_here_when_asked(home, builds):
    """The other Studio stopped (crashed) during kb1's first build while this one ran: its journal keeps the design, so
    this Studio says "build it again" (not "Morph kept no design: delete the project") and builds it from there; the
    stopped Studio's cut-off r001 is set aside and its journal goes (a later start doesn't build it a second time)."""
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    journal = cut_off_first_build(home, owner="feedfacecafebeef")          # after this Studio started: no recovery
    kb1 = st.room("kb1")
    finish(st.queue_chat(kb1, "make it lighter"))
    said = texts(kb1)[-1]
    assert 'send "build it again"' in said and "kept no design" not in said, said
    finish(st.queue_chat(kb1, "build it again"))
    finish(*(kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()))
    assert "Building kb1's first design again." in texts(kb1)
    assert kb1.completed() == ["r001"] and kb1.p.load_design("r001").build.quantity == 5
    assert not journal.exists() and [n for n in os.listdir(home / "kb1") if n.startswith("r001.incomplete-")]
    assert builds.built == [("kb1", "r001", None, "first design")]


def test_the_parked_first_build_message_never_blames_the_design():
    for terminal in (False, True):
        text = server.Studio._parked_text("kb1", 2, terminal=terminal)
        assert "stopped twice" in text and "build it again" in text
        assert not any(w in text for w in ("in this design", "crash", "stops the Studio", "stop it at every start"))


def test_a_rebuild_that_never_started_is_no_try(home, builds):
    """A Studio stopped while the rebuild still waited for the build slot didn't cut it off: it is built again."""
    journal = cut_off_first_build(home, cut=1)
    lines = journal.read_text(encoding="utf-8").splitlines()
    journal.write_text(lines[0] + "\n", encoding="utf-8")                  # no "started" line
    shutil.rmtree(home / "kb1" / "r001")                                    # nor a revision folder
    builds.gate = threading.Event()
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    kb1 = st.room("kb1")
    queued = [kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()]
    assert len(queued) == 1
    builds.gate.set()
    finish(*queued)
    assert kb1.completed() == ["r001"]


def test_two_stops_of_a_journaled_first_build_park_it(home, builds):
    cut_off_first_build(home, cut=1)                                       # its rebuild, started and cut off
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    assert not st.room("kb1").jobs.busy() and (home / "kb1" / ".morph" / "first-build.json").exists()


def test_a_cut_off_first_build_without_a_journal_is_built_from_its_design(home, tmp_path, builds):
    """An older Morph, or `morph robot new` stopped in a terminal: no journal, only the cut-off r001."""
    root = home / "cli-bot"
    (root / "r001").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot", "source": "", "need": "cli"}), encoding="utf-8")
    (root / "r001" / "design.json").write_text(Design().with_changes({"build.quantity": 3}).dumps(), encoding="utf-8")
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    room = st.room("cli-bot")
    finish(*(room.jobs.get(j["id"]) for j in room.jobs.snapshot()))
    assert room.completed() == ["r001"] and room.p.load_design("r001").build.quantity == 3


def test_the_front_end_says_when_a_first_build_waits_its_turn():
    app = open(os.path.join(os.path.dirname(server.__file__), "static", "app.js"), encoding="utf-8").read()
    assert "its first build is waiting for its turn" in app


# ------------------------------------------------------------------ New robot


def _ready_brief(home):
    req = SimpleNamespace(body={"text": "something to pick strawberries"}, query={}, studio=SimpleNamespace(home=str(home)),
                          room=SimpleNamespace(name="egg", hub=SimpleNamespace(send=lambda ev: None)), member=None)
    st, out = api_brief.post(req, home=str(home), sync=True)
    bid = out["brief_id"]
    req.body = {"brief_id": bid, "action": "defaults"}
    assert api_brief.post(req, home=str(home), sync=True)[1]["brief"]["status"] == "ready"
    return bid


def _build_brief(studio, bid):
    req = SimpleNamespace(body={"brief_id": bid, "action": "build", "name": "berry-bot"}, query={}, studio=studio,
                          room=studio.room(), member=None)
    return api_brief.post(req, create_project=api_brief._studio_create(req), revive=api_brief._studio_revive(req))


def test_a_briefs_first_build_cut_off_is_built_again_and_the_brief_ends_built(home, tmp_path, builds, monkeypatch):
    monkeypatch.setenv("MORPH_OFFLINE", "1")                                # no Darwin lookups after the build
    monkeypatch.setattr(api_brief, "STORE", api_brief.Store())
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    bid = _ready_brief(st.home)
    builds.gate = threading.Event()
    status, out = _build_brief(st, bid)
    assert status == 200 and out["project"] == "berry-bot" and builds.started.wait(5)
    first = [st.room("berry-bot").jobs.get(j["id"]) for j in st.room("berry-bot").jobs.snapshot()]
    after = stopped(home, tmp_path, builds)
    builds.gate.set()                                                      # the stopped Studio's own build ends here ...
    finish(*first)
    settled()                                                              # ... and its brief with it
    monkeypatch.setattr(api_brief, "STORE", api_brief.Store())          # a new process: briefs come from their drafts
    assert B.load_draft(str(after), bid).status == "building"
    builds.gate = threading.Event()
    st2 = server.Studio(RobotProject(str(after / "egg")), str(after))
    room = st2.room("berry-bot")
    queued = [room.jobs.get(j["id"]) for j in room.jobs.snapshot()]
    status, out = _build_brief(st2, bid)                                   # Build again: the one being rebuilt
    assert status == 200 and out["already"] and out["project"] == "berry-bot"
    builds.gate.set()
    finish(*queued)
    for _ in range(200):  # _first_build runs on its own thread once the build ends
        if os.path.exists(after / "berry-bot" / "brief.json") and B.load_draft(str(after), bid) is None:
            break
        time.sleep(0.05)
    settled()
    assert room.completed() == ["r001"] and os.path.exists(after / "berry-bot" / "brief.json")
    assert B.load_draft(str(after), bid) is None                            # built: the draft is done with


def test_build_again_on_a_brief_whose_project_never_got_a_build_makes_it_anew(home, tmp_path, builds, monkeypatch):
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    monkeypatch.setattr(api_brief, "STORE", api_brief.Store())
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    bid = _ready_brief(st.home)
    b = B.load_draft(st.home, bid)
    b.status, b.project = "building", "berry-bot"                           # a brief left building ...
    B.save_draft(b, st.home)
    (home / "berry-bot").mkdir()                                            # ... for a project with nothing to build from
    (home / "berry-bot" / "meta.json").write_text(json.dumps({"kind": "robot", "origin": "brief", "brief_id": bid}),
                                                  encoding="utf-8")
    monkeypatch.setattr(api_brief, "STORE", api_brief.Store())
    status, out = _build_brief(st, bid)
    assert status == 200 and not out.get("already") and out["project"] == "berry-bot"
    assert [n for n in os.listdir(home / ".trash") if n.startswith("berry-bot-")]   # the empty one, kept in the trash
    room = st.room("berry-bot")
    finish(*(room.jobs.get(j["id"]) for j in room.jobs.snapshot()))
    for _ in range(200):  # the brief is marked built on its own thread (offline, while this test still runs)
        if B.load_draft(st.home, bid) is None:
            break
        time.sleep(0.05)
    settled()
    assert room.completed() == ["r001"] and os.path.exists(home / "berry-bot" / "brief.json")


def test_build_on_a_brief_whose_project_another_studio_builds_never_trashes_it(home, builds, monkeypatch):
    """Build asked again for a brief whose project another Studio on this projects folder is building: that project is
    on its way. This Studio, seeing it empty and idle in its own queue, moved it to .trash and made a second one."""
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    monkeypatch.setattr(api_brief, "STORE", api_brief.Store())
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    bid = _ready_brief(st.home)
    b = B.load_draft(st.home, bid)
    b.status, b.project = "building", "berry-bot"
    B.save_draft(b, st.home)
    cut_off_first_build(home, name="berry-bot", owner="feedfacecafebeef")
    (home / "berry-bot" / "meta.json").write_text(json.dumps({"kind": "robot", "origin": "brief", "brief_id": bid}),
                                                  encoding="utf-8")
    other = hold(home / ".studios" / "feedfacecafebeef.lock")
    try:
        monkeypatch.setattr(api_brief, "STORE", api_brief.Store())
        status, out = _build_brief(st, bid)
        assert status == 200 and out.get("already") and out["project"] == "berry-bot", out
        assert (home / "berry-bot" / "r001" / "design.json").exists() and not (home / ".trash").exists()
        assert builds.built == [] and not os.path.exists(home / "berry-bot-2")
    finally:
        other.stdin.close()
        other.wait(10)


# ------------------------------------------------------------------ Teams


def test_a_change_request_applied_but_never_built_is_open_again(home, tmp_path, builds):
    from test_team_util import make_team, members
    ids, _ = make_team(str(home))
    root = str(home / "egg")
    p = collab.create_proposal(root, ids["Vic"], "Quantity to 77", {"build.quantity": 77}, members=members(home))
    collab.apply_proposal(root, p["id"], ids["Ana"], members=members(home))   # applied; the Studio stopped before r002
    server.Studio(RobotProject(str(home / "egg")), str(home))               # a plain Studio leaves team work alone
    assert collab.get_proposal(root, p["id"])["status"] == "applied"
    server.Studio(RobotProject(str(home / "egg")), str(home), team=True)
    q = collab.get_proposal(root, p["id"])
    assert q["status"] == "open" and q["last_error"] == api_team.STOPPED
    for who in ("Ana", "Vic"):
        items = activity.inbox(str(home), ids[who])["items"]
        assert any("wasn't built: the Studio stopped" in i["text"] for i in items), (who, items)
    collab.apply_proposal(root, p["id"], ids["Ana"], members=members(home))   # and it can be applied again


def test_a_change_request_another_running_studio_builds_is_left_alone(home, tmp_path, builds):
    """Two team Studios on one projects folder: a change request applied in the other one, which still runs (its lock
    is held), is that Studio's to build; a second Studio starting must neither reopen it nor tell its people it wasn't
    built. Once that Studio has stopped, the next start reopens what it left unbuilt. A build that ends after a reopen
    it shouldn't have had (a file system without locks) still records its revision."""
    from test_team_util import make_team, members
    ids, _ = make_team(str(home))
    root = str(home / "egg")
    p = collab.create_proposal(root, ids["Vic"], "Quantity to 77", {"build.quantity": 77}, members=members(home))
    collab.apply_proposal(root, p["id"], ids["Ana"], members=members(home), studio="feedfacecafebeef")
    other = hold(home / ".studios" / "feedfacecafebeef.lock")
    try:
        assert api_team.reopen_stopped(server.Studio(RobotProject(root), str(home), team=True)) == []
        assert collab.get_proposal(root, p["id"])["status"] == "applied"
        assert not any("wasn't built" in i["text"] for who in ("Ana", "Vic")
                       for i in activity.inbox(str(home), ids[who])["items"])
    finally:
        other.stdin.close()
        other.wait(10)
    server.Studio(RobotProject(root), str(home), team=True)                 # that Studio stopped before building it
    q = collab.get_proposal(root, p["id"])
    assert q["status"] == "open" and q["last_error"] == api_team.STOPPED
    collab.proposal_done(root, p["id"], rev="r002")                          # ... yet it was building it after all
    q = collab.get_proposal(root, p["id"])
    assert q["status"] == "applied" and q["applied_rev"] == "r002" and q["last_error"] is None


# ------------------------------------------------------------------ robustness studies


def _fake_studies(monkeypatch, gate):
    def new_study(root, rev=None, runs=200, **kw):
        sid = "a1b2c3d4e5f6" if os.path.basename(root) == "so101" else "0123456789ab"
        return {"version": robust.VERSION, "id": sid, "project": os.path.basename(root), "rev": "r001", "n": runs,
                "seed": 1, "stress": False, "timestep": 0.001, "status": "queued", "done": 0}

    def run_study(root, rev=None, **kw):
        gate.wait(10)
        return {"id": new_study(root)["id"], "status": "done"}
    monkeypatch.setattr(robust, "new_study", new_study)
    monkeypatch.setattr(robust, "run_study", run_study)
    monkeypatch.setattr(robust, "active", lambda root: None)


def test_a_study_waiting_for_its_turn_is_listed_after_a_stop(home, tmp_path, monkeypatch):
    gate = threading.Event()
    _fake_studies(monkeypatch, gate)
    hj = jobs.HeavyJobs(str(home), max_running=1)
    hj.start_study("egg", runs=100)
    hj.start_study("so101", runs=100)                                      # waits: one heavy job at a time
    assert hj.status("so101")["status"] == "queued"
    sid = "a1b2c3d4e5f6"
    assert robust.load(str(home / "so101"), sid)["status"] == "queued"     # this process will run it
    after = stopped(home, tmp_path, None)
    monkeypatch.setattr(robust, "_PROCESS", "a later process")
    doc = robust.load(str(after / "so101"), sid)
    assert doc["status"] == "interrupted" and doc["n"] == 100 and doc["done"] == 0   # to carry on, never lost
    monkeypatch.setattr(robust, "_PROCESS", robust._PROCESS)
    hj.cancel("so101")                                                     # cancelled while it waits: as if never asked
    assert not os.path.exists(robust.study_dir(str(home / "so101"), sid))
    hj.start_study("so101", runs=100)
    hj.shutdown(timeout=0.1)                                               # the Studio stops: it stays, to carry on
    gate.set()
    assert os.path.exists(os.path.join(robust.study_dir(str(home / "so101"), sid), "study.json"))


def test_a_change_request_whose_build_clashed_and_was_applied_anyway_records_its_revision(home, builds):
    """A change request's build that clashes with a newer change waits for Apply or Drop like any other; its record
    follows the answer (it used to be reopened with "the build ended as conflict" and never got the revision Apply
    built). A dropped one is reopened, saying why."""
    from test_team_util import make_team, members
    ids, _ = make_team(str(home))
    root = str(home / "egg")
    st = server.Studio(RobotProject(root), str(home), team=True)
    egg = st.room("egg")
    req = SimpleNamespace(room=egg, studio=st)
    for answer in ("apply", "drop"):
        builds.gate, builds.started = threading.Event(), threading.Event()
        first = st.queue_params(egg, {"build.quantity": 40 if answer == "apply" else 41}, by=ids["Ana"])
        assert builds.started.wait(5)                                       # Ana's edit builds ...
        p = collab.create_proposal(root, ids["Raj"], f"Quantity ({answer})", {"build.quantity": 77 if answer == "apply" else 78},
                                   members=members(home))
        job = collab.apply_proposal(root, p["id"], ids["Jim"], members=members(home))
        assert api_team._enqueue(req, job)                                 # ... while Jim applies Raj's request
        q = egg.jobs.get(job["job"])
        builds.gate.set()
        finish(first, q)
        assert q.status == "conflict"
        time.sleep(0.3)
        assert collab.get_proposal(root, p["id"])["status"] == "applied"     # waiting for an answer, not reopened
        new = st.resolve(egg, q.id, answer, by=ids["Jim"])
        if new is not None:
            finish(new)
        for _ in range(100):
            got = collab.get_proposal(root, p["id"])
            if got["status"] != "applied" or got.get("applied_rev"):
                break
            time.sleep(0.05)
        if answer == "apply":
            assert got["status"] == "applied" and got["applied_rev"] == new.result == egg.latest(), got
        else:
            assert got["status"] == "open" and "dropped" in got["last_error"], got


def test_a_briefs_first_build_asked_for_again_marks_the_brief_built(home, tmp_path, builds, monkeypatch):
    """A New robot project whose first build stopped the Studio twice waits; "build it again" builds it, and its brief
    ends built as after any first build."""
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    monkeypatch.setattr(api_brief, "STORE", api_brief.Store())
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    bid = _ready_brief(st.home)
    builds.gate = threading.Event()
    status, out = _build_brief(st, bid)
    assert status == 200 and builds.started.wait(5)
    first = [st.room("berry-bot").jobs.get(j["id"]) for j in st.room("berry-bot").jobs.snapshot()]
    after = stopped(home, tmp_path, builds)
    builds.gate.set()
    finish(*first)
    settled()
    for path in rooms.journals(str(after / "berry-bot")):                   # as if this was its rebuild, cut off too
        lines = [json.loads(line) for line in open(path, encoding="utf-8")]
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps({**e, "cut": 1} if e["op"] == "queued" else e) + "\n" for e in lines)
    monkeypatch.setattr(api_brief, "STORE", api_brief.Store())
    st2 = server.Studio(RobotProject(str(after / "egg")), str(after))
    room = st2.room("berry-bot")
    assert not room.jobs.busy() and (after / "berry-bot" / ".morph" / "first-build.json").exists()
    finish(st2.queue_chat(room, "build it again"))
    finish(*(room.jobs.get(j["id"]) for j in room.jobs.snapshot()))
    for _ in range(200):
        if B.load_draft(str(after), bid) is None:
            break
        time.sleep(0.05)
    settled()
    assert room.completed() == ["r001"] and os.path.exists(after / "berry-bot" / "brief.json")
    assert B.load_draft(str(after), bid) is None


# ------------------------------------------------------------------ a full disk, and a stop at the wrong moment


def test_a_full_disk_at_a_restart_never_loses_a_cut_off_first_build(home, tmp_path, monkeypatch):
    """The disk is full when the Studio starts: the rebuild's journal line, the rebuild itself and the error record all
    fail. The stopped Studio's journal used to go first, leaving the design only in memory ("kept no design ... delete
    the project"); it stays until what replaces it is written, so the next start, with space again, builds kb1."""
    from morph.services import durable
    cut_off_first_build(home)
    full = [True]
    real_append, real_write = durable.append_line, durable.write_json

    def append_line(path, line):
        if full[0] and "kb1" in str(path):
            raise OSError(28, "No space left on device", str(path))
        return real_append(path, line)

    def write_json(path, value, *a, **k):
        if full[0] and "kb1" in str(path):
            raise OSError(28, "No space left on device", str(path))
        return real_write(path, value, *a, **k)

    def run(self, design, *a, **k):
        raise OSError(28, "No space left on device", os.path.join(self.root, "r001", "design.json"))

    monkeypatch.setattr(durable, "append_line", append_line)
    monkeypatch.setattr(durable, "write_json", write_json)
    monkeypatch.setattr(RobotProject, "run", run)
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    kb1 = st.room("kb1")
    finish(*(kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()))
    assert kb1.completed() == [] and rooms.journals(str(home / "kb1"))   # the stopped Studio's journal stays
    assert not (home / "kb1" / ".morph" / "first-build.json").exists()
    after = tmp_path / "space-again"
    shutil.copytree(home, after)
    full[0] = False
    fake = FakeBuilds()
    monkeypatch.setattr(RobotProject, "run", lambda self, *a, **k: fake(self, *a, **k))
    st2 = server.Studio(RobotProject(str(after / "egg")), str(after))
    kb1 = st2.room("kb1")
    finish(*(kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()))
    assert kb1.completed() == ["r001"] and kb1.p.load_design("r001").build.quantity == 5
    assert not rooms.journals(str(after / "kb1"))


def test_a_stop_between_taking_over_a_journal_and_removing_it_loses_nothing(home, tmp_path, builds, monkeypatch):
    """The new journal line is written before the stopped Studio's journal goes: a kill in between leaves both (it used
    to leave neither), and the next start builds the first design once, counting the stop once."""
    cut_off_first_build(home)
    snap, may_start = tmp_path / "killed-mid-recovery", threading.Event()
    real_remove, real_started = server._remove, rooms.JobQueue.started

    def remove(path):
        if "queue-" in str(path) and not snap.exists():
            shutil.copytree(home, snap)                                     # killed right here
            may_start.set()
        real_remove(path)

    def started(self, job):
        assert may_start.wait(5)
        real_started(self, job)

    monkeypatch.setattr(server, "_remove", remove)
    monkeypatch.setattr(rooms.JobQueue, "started", started)
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    finish(*(st.room("kb1").jobs.get(j["id"]) for j in st.room("kb1").jobs.snapshot()))
    assert len(rooms.journals(str(snap / "kb1"))) == 2                     # the old journal and the new one
    monkeypatch.setattr(server, "_remove", real_remove)
    builds.gate = threading.Event()                                         # the rebuild waits until looked at
    st2 = server.Studio(RobotProject(str(snap / "egg")), str(snap))
    kb1 = st2.room("kb1")
    queued = [kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()]
    assert [j.label for j in queued] == ["first design" + REBUILT]          # once, and not parked
    builds.gate.set()
    finish(*queued)
    assert kb1.completed() == ["r001"] and not rooms.journals(str(snap / "kb1"))


def test_build_it_again_finds_a_set_aside_first_design_nothing_else_kept(home, builds):
    """No journal, no record and no finished revision, but the cut-off revision set aside at an earlier start still
    holds the design: "build it again" builds it instead of saying Morph kept nothing."""
    from morph.spec import Design as D
    root = home / "kb1"
    (root / f"r001.incomplete-{time.time_ns()}").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot", "source": "", "need": "kb1"}), encoding="utf-8")
    aside = next(root.glob("r001.incomplete-*"))
    (aside / "design.json").write_text(D().with_changes({"build.quantity": 9}).dumps(), encoding="utf-8")
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    kb1 = st.room("kb1")
    assert not kb1.jobs.busy()
    finish(st.queue_chat(kb1, "build it again"))
    finish(*(kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()))
    assert kb1.completed() == ["r001"] and kb1.p.load_design("r001").build.quantity == 9


def test_a_start_that_set_a_first_build_aside_recovers_it_under_the_lock(home, builds, monkeypatch):
    """A Studio whose start-up set the cut-off revision aside, and that sees no journal (another starting Studio had
    just removed the stopped one's and not yet written its own), recovered without the project's first-build lock and
    built the first design a second time. It now takes the lock and reads the journals again under it."""
    root = home / "kb1"
    (root / "r001").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot", "source": "", "need": "kb1"}), encoding="utf-8")
    (root / "r001" / "design.json").write_text(Design().with_changes({"build.quantity": 5}).dumps(), encoding="utf-8")
    locked, real = [], rooms.first_build_turn

    def turn(path, wait=5.0):
        locked.append(os.path.basename(str(path)))
        return real(path, wait)

    monkeypatch.setattr(server, "first_build_turn", turn)
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    kb1 = st.room("kb1")
    assert locked == ["kb1"]
    finish(*(kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()))
    assert kb1.completed() == ["r001"]


# ------------------------------------------------------------------ stops during the start-up, and other ways to stop


class _Listener:
    """serve()'s server: binds nothing; serve_forever returns at once (or `fail` raises at bind time)."""
    fail, served = None, []

    def __init__(self, address, handler):
        if self.fail:
            raise self.fail

    def serve_forever(self):
        _Listener.served.append(True)

    def shutdown(self):
        pass


@pytest.mark.parametrize("sig", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_a_stop_asked_for_during_the_start_up_is_a_clean_one(home, tmp_path, builds, monkeypatch, sig):
    """Ctrl+C (or a closed window: SIGHUP, or SIGTERM) while the start-up rebuilds a cut-off first build, before the
    Studio serves: it used to escape serve()'s handler (a KeyboardInterrupt traceback; SIGHUP and SIGTERM had no
    handler at all), leave the rebuild journaled "started" and count as a try, so two such stops parked the design. The
    stop is now a clean one, journaled "interrupted", and the next start builds it again."""
    import signal
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, sig):
        pytest.skip("signals reach the main thread only")
    from morph.studio import capability
    cut_off_first_build(home)
    builds.gate = threading.Event()
    real = server.Studio._recover

    def recover(self, *a, **k):
        real(self, *a, **k)
        assert builds.started.wait(5)                                       # the rebuild has started
        os.kill(os.getpid(), getattr(signal, sig))

    _Listener.fail, _Listener.served = None, []
    monkeypatch.setattr(server, "_Server", _Listener)
    monkeypatch.setattr(server.Studio, "_recover", recover)
    monkeypatch.setattr(capability, "warm", lambda block=True: None)
    monkeypatch.setattr(jobs, "shutdown", lambda studio, timeout=5.0, hurry=None: None)
    def unhandled(signum, frame):  # what an unhandled stop would do, without ending the test run
        raise RuntimeError(f"{sig} reached no handler of serve()")

    before = signal.signal(getattr(signal, sig), unhandled)
    try:
        server.serve(RobotProject(str(home / "egg")), port=0, home=str(home))  # returns: no KeyboardInterrupt
        assert _Listener.served == [] and signal.getsignal(getattr(signal, sig)) is unhandled
    finally:
        signal.signal(getattr(signal, sig), before)
    ops = [json.loads(line)["op"] for path in rooms.journals(str(home / "kb1")) for line in open(path, encoding="utf-8")]
    assert ops[-2:] == ["started", "interrupted"], ops
    after = stopped(home, tmp_path, builds)                                 # the process ends here
    monkeypatch.setattr(server.Studio, "_recover", real)
    builds.gate.set()                                                       # the stopped Studio's build ends
    builds.gate = threading.Event()                                         # this one's waits until looked at
    st = server.Studio(RobotProject(str(after / "egg")), str(after))
    kb1 = st.room("kb1")
    queued = [kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()]
    assert [j.label for j in queued] == ["first design" + REBUILT] and "stopped twice" not in " ".join(texts(kb1))
    builds.gate.set()
    finish(*queued)


@pytest.mark.parametrize("presses", [2, 3, 6])
def test_ctrl_c_pressed_again_and_again_during_the_start_up_is_still_a_clean_stop(home, tmp_path, builds, monkeypatch,
                                                                                presses):
    """Ctrl+C pressed twice while the start-up rebuilt a cut-off first build (after one real crash): the second press
    raised KeyboardInterrupt inside Studio(), before serve() had the Studio, so nothing journaled the stop
    "interrupted" and the next start parked the design ("The Studio stopped twice ...") after one crash. Presses that
    keep coming (one landing while the stop is written down, more while studies stop) never cut the stop short or
    escape serve() either: a person stopping the Studio is never a try, however often they press."""
    import signal
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signals reach the main thread only")
    from morph.studio import capability
    cut_off_first_build(home)                                               # one real crash: one try
    builds.gate = threading.Event()
    real_recover, real_stopping = server.Studio._recover, server.Studio.stopping
    sent = []

    def press():
        sent.append(True)
        os.kill(os.getpid(), signal.SIGINT)

    def recover(self, *a, **k):
        real_recover(self, *a, **k)
        assert builds.started.wait(5)                                       # the rebuild has started
        press()                                                             # the first: a clean stop is asked for
        press()                                                             # the second: stop now
        raise AssertionError("the second Ctrl+C didn't stop the start-up")

    def stopping(self):
        if len(sent) < presses:
            press()                                                         # one more, while the stop is written down
        real_stopping(self)

    def shutdown(studio, timeout=5.0, hurry=None):
        while len(sent) < presses:
            press()                                                         # and the rest while studies stop
        assert hurry is None or hurry.is_set()                              # asked again: no waiting for them

    _Listener.fail, _Listener.served = None, []
    monkeypatch.setattr(server, "_Server", _Listener)
    monkeypatch.setattr(server.Studio, "_recover", recover)
    monkeypatch.setattr(server.Studio, "stopping", stopping)
    monkeypatch.setattr(capability, "warm", lambda block=True: None)
    monkeypatch.setattr(jobs, "shutdown", shutdown)

    def unhandled(signum, frame):
        raise RuntimeError("a Ctrl+C reached no handler of serve()")

    before = signal.signal(signal.SIGINT, unhandled)
    try:
        server.serve(RobotProject(str(home / "egg")), port=0, home=str(home))  # returns: nothing escapes it
        assert _Listener.served == [] and len(sent) == presses
    finally:
        signal.signal(signal.SIGINT, before)
    ops = [json.loads(line)["op"] for path in rooms.journals(str(home / "kb1")) for line in open(path, encoding="utf-8")]
    assert ops[-2:] == ["started", "interrupted"], ops
    after = stopped(home, tmp_path, builds)                                 # the process ends here
    monkeypatch.setattr(server.Studio, "_recover", real_recover)
    builds.gate.set()
    builds.gate = threading.Event()
    st = server.Studio(RobotProject(str(after / "egg")), str(after))
    kb1 = st.room("kb1")
    queued = [kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()]
    assert [j.label for j in queued] == ["first design" + REBUILT] and "stopped twice" not in " ".join(texts(kb1))
    assert not (after / "kb1" / ".morph" / "first-build.json").exists()
    builds.gate.set()
    finish(*queued)


def test_a_first_build_that_starts_after_a_clean_stop_was_written_down_is_no_try(home, builds):
    """A first build waiting for the build slot when the Studio stops cleanly: Studio.stopping journaled nothing for
    it (it hadn't started), then the build ahead of it ended while studies stopped, it took the slot and journaled
    "started", and the process ended: the next start counted the clean stop as a try. A first build that starts once
    the clean stop is under way is cut off by that stop, and its journal says so."""
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    builds.gate = threading.Event()
    ahead = st.queue_params(st.room(), {"build.quantity": 40})             # takes the only build slot
    assert builds.started.wait(5)
    room, job = st.create_project("kb2", design=Design().with_changes({"build.quantity": 7}))
    st.stopping()                                                           # Ctrl+C: what serve() calls
    first, builds.gate = builds.gate, threading.Event()                     # kb2's build will wait at this one
    builds.started.clear()
    first.set()                                                             # the build ahead ends; kb2's starts
    assert builds.started.wait(5)
    entries = rooms.open_jobs(rooms.journals(str(home / "kb2"))[0])
    assert [(e["id"], e["started"]) for e in entries] == [(job.id, False)]  # cut off by a clean stop: no try
    builds.gate.set()
    finish(ahead, job)


def test_a_start_that_cant_listen_changes_no_journal(home, monkeypatch):
    """A mistyped --host: serve() listened only after the start-up had rebuilt (and journaled "started") a cut-off first
    build, so each failed start counted as a try. It now listens first."""
    from morph.studio import capability
    journal = cut_off_first_build(home)
    before = journal.read_text(encoding="utf-8")
    _Listener.fail = OSError(99, "Cannot assign requested address")
    monkeypatch.setattr(server, "_Server", _Listener)
    monkeypatch.setattr(capability, "warm", lambda block=True: None)
    try:
        with pytest.raises(SystemExit, match="couldn't listen"):
            server.serve(RobotProject(str(home / "egg")), host="10.255.255.1", port=0, home=str(home))
    finally:
        _Listener.fail = None
    assert journal.read_text(encoding="utf-8") == before and rooms.journals(str(home / "kb1")) == [str(journal)]
    assert os.listdir(home / "kb1" / "r001") == ["design.json"]             # not even set aside


_STUDIO = """
import sys, threading
from types import SimpleNamespace
sys.path.insert(0, sys.argv[2])
from morph.robot.pipeline2 import RobotProject
from morph.studio import capability, server
from test_studio_rooms import FakeBuilds
fake = FakeBuilds()
fake.gate = SimpleNamespace(wait=lambda timeout=None: threading.Event().wait())  # builds until the process ends
RobotProject.run = lambda self, *a, **k: fake(self, *a, **k)
capability.warm = lambda block=True: None
threading.Thread(target=lambda: fake.started.wait() and print("<build started>", flush=True), daemon=True).start()
server.serve(RobotProject(sys.argv[1] + "/egg"), port=0, home=sys.argv[1])
print("serve() returned", flush=True)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals sent from outside the process")
@pytest.mark.parametrize("sig", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_a_studio_process_stopped_twice_by_a_signal_mid_first_build_builds_it_again(home, builds, sig):
    """Closing the Studio's terminal window (SIGHUP), a logout or `kill` (SIGTERM) while it served and rebuilt a cut-off
    first build ended the process with nothing journaled, so each stop counted as a crash and the second parked the
    design ("The Studio stopped twice ..."). A real Studio process, signalled from outside twice, now journals each stop
    as "interrupted", exits 0 without a traceback, and the next start builds the design again (SIGINT as control)."""
    import signal
    cut_off_first_build(home)
    tests = os.path.dirname(os.path.abspath(__file__))
    dfl = lambda: [signal.signal(getattr(signal, s), signal.SIG_DFL) for s in ("SIGINT", "SIGTERM", "SIGHUP")]  # noqa: E731
    for _ in range(2):
        proc = subprocess.Popen([sys.executable, "-c", _STUDIO, str(home), tests], cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", preexec_fn=dfl)
        lines = []
        reader = threading.Thread(target=lambda: lines.extend(proc.stdout), daemon=True)
        reader.start()
        try:
            t0 = time.time()
            while not ("Morph Studio:" in "".join(lines) and "<build started>" in "".join(lines)):  # any order
                assert proc.poll() is None and time.time() - t0 < 60, "".join(lines)
                time.sleep(0.05)
            proc.send_signal(getattr(signal, sig))
            rc = proc.wait(30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(10)
        reader.join(10)
        out = "".join(lines)
        assert rc == 0 and "serve() returned" in out and "Traceback" not in out, (rc, out)
        ops = [json.loads(line)["op"] for path in rooms.journals(str(home / "kb1"))
               for line in open(path, encoding="utf-8")]
        assert ops[-2:] == ["started", "interrupted"], ops
    builds.gate = threading.Event()                                         # the rebuild waits until looked at
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    kb1 = st.room("kb1")
    queued = [kb1.jobs.get(j["id"]) for j in kb1.jobs.snapshot()]
    assert [j.label for j in queued] == ["first design" + REBUILT] and "stopped twice" not in " ".join(texts(kb1))
    assert not (home / "kb1" / ".morph" / "first-build.json").exists()
    builds.gate.set()
    finish(*queued)
    assert kb1.completed() == ["r001"]


# ------------------------------------------------------------------ change requests a stopped Studio left


def _team_server(home):
    from morph.studio.teams import auth
    from test_studio_features import caller, start
    from test_team_util import make_team
    ids, cookies = make_team(str(home))
    st = server.Studio(RobotProject(str(home / "egg")), str(home), team=True)
    httpd = start(st)
    port = httpd.server_address[1]
    jar = {n: {"Cookie": f"{auth.cookie_name(port)}={c}"} for n, c in cookies.items()}
    return st, httpd, caller(httpd), ids, jar


def test_a_change_request_a_stopped_studio_applied_opens_again_in_one_that_runs(home, builds):
    """Two team Studios: Olivia applies a request in A, and A is killed mid-build. B, still running, showed it
    "applied" with no revision; Apply and Close both answered "already applied", and nothing built it until some Studio
    restarted. B now opens it again (or records it built) as soon as it is listed, applied or closed."""
    from test_team_util import members
    st, httpd, call, ids, jar = _team_server(home)
    try:
        root = str(home / "egg")
        p = collab.create_proposal(root, ids["Vic"], "Quantity to 77", {"build.quantity": 77}, members=members(home))
        other = hold(home / ".studios" / "feedfacecafebeef.lock")               # Studio A, running
        collab.apply_proposal(root, p["id"], ids["Ana"], members=members(home), studio="feedfacecafebeef")
        s, out, _ = call("GET", "/api/proposals", headers=jar["Ana"])
        assert s == 200 and out["proposals"][0]["status"] == "applied"           # A's to build while it runs
        other.stdin.close()
        other.wait(10)                                                           # A is gone, r002 never built
        s, out, _ = call("GET", "/api/proposals", headers=jar["Ana"])
        assert out["proposals"][0]["status"] == "open" and out["proposals"][0]["last_error"] == api_team.STOPPED
        assert any("wasn't built: the Studio stopped" in i["text"] for i in activity.inbox(str(home), ids["Vic"])["items"])
        s, out, _ = call("POST", "/api/proposals/apply", {"id": p["id"]}, headers=jar["Ana"])
        assert s == 202, out
        finish(*(st.room().jobs.get(j["id"]) for j in st.room().jobs.snapshot()))
        settled()
        q = collab.get_proposal(root, p["id"])
        assert q["status"] == "applied" and q["applied_rev"] == "r002"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_request_another_studio_applied_again_meanwhile_is_not_opened_again(home, builds, monkeypatch):
    """Studios B and C both list a request a Studio that has stopped applied. C opens it again and applies it again (C
    runs and builds it) after B read it as the stopped Studio's and before B wrote it open again: B's late write made the
    request C was building "open", so it could be applied a third time, and told both people again."""
    from test_team_util import make_team, members
    ids, _ = make_team(str(home))
    root = str(home / "egg")
    p = collab.create_proposal(root, ids["Vic"], "Quantity to 77", {"build.quantity": 77}, members=members(home))
    collab.apply_proposal(root, p["id"], ids["Ana"], members=members(home), studio="deadbeefdeadbeef")  # A: stopped
    c = hold(home / ".studios" / "c0ffeec0ffeec0ff.lock")                       # Studio C, running
    real = api_team.built_after_all

    def meanwhile(root_, q):  # B has read the request as A's; C opens it again and applies it now
        monkeypatch.setattr(api_team, "built_after_all", real)
        collab.proposal_done(root, p["id"], error=api_team.STOPPED)
        collab.apply_proposal(root, p["id"], ids["Ana"], members=members(home), studio="c0ffeec0ffeec0ff")
        return real(root_, q)
    monkeypatch.setattr(api_team, "built_after_all", meanwhile)
    try:
        assert api_team.settle_stopped(root, str(home)) == []
        q = collab.get_proposal(root, p["id"])
        assert q["status"] == "applied" and q.get("applied_in") == "c0ffeec0ffeec0ff", q
        assert not any("wasn't built" in i["text"] for i in activity.inbox(str(home), ids["Vic"])["items"])
    finally:
        c.stdin.close()
        c.wait(10)


def test_a_change_request_built_just_before_a_stop_is_recorded_built(home, builds):
    """A stop in the few ms between a request's revision (results.json) and its "done" record: the next start reopened
    it ("wasn't built: the Studio stopped") though r002 held the change, and applying it again said "Nothing to change"
    and left it open for good. The start now finds r002 and records the request built there."""
    from test_team_util import make_team, members
    ids, _ = make_team(str(home))
    root = str(home / "egg")
    p = collab.create_proposal(root, ids["Vic"], "Quantity to 77", {"build.quantity": 77}, members=members(home))
    collab.apply_proposal(root, p["id"], ids["Ana"], members=members(home))
    time.sleep(1.1)                                                              # applied a second before it was built
    egg = RobotProject(root)
    rev = egg.run(egg.load_design("r001").with_changes({"build.quantity": 77}), parent="r001",
                  label=f"#{p['n']} {p['title']}"[:48], by=ids["Ana"], proposed_by=ids["Vic"])
    assert rev == "r002"                                                         # ... and the Studio stopped here
    server.Studio(RobotProject(root), str(home), team=True)
    q = collab.get_proposal(root, p["id"])
    assert q["status"] == "applied" and q["applied_rev"] == "r002" and not q.get("last_error")
    texts_ = [i["text"] for i in activity.inbox(str(home), ids["Vic"])["items"]]
    assert any("built r002" in t for t in texts_) and not any("wasn't built" in t for t in texts_)


def test_applying_a_change_the_design_already_has_marks_it_applied(home, builds):
    """A request whose change the head already has (built by hand, or by a request recorded before) used to end "Nothing
    to change" and stay open, its last error "nothing to change", for good: it is applied, at the head revision."""
    from test_team_util import members
    st, httpd, call, ids, jar = _team_server(home)
    try:
        root = str(home / "egg")
        p = collab.create_proposal(root, ids["Vic"], "Quantity to 77", {"build.quantity": 77}, members=members(home))
        finish(st.queue_params(st.room(), {"build.quantity": 77}))               # the head has it now (r002)
        s, out, _ = call("POST", "/api/proposals/apply", {"id": p["id"]}, headers=jar["Ana"])
        assert s == 202, out
        finish(*(st.room().jobs.get(j["id"]) for j in st.room().jobs.snapshot()))
        settled()
        q = collab.get_proposal(root, p["id"])
        assert q["status"] == "applied" and q["applied_rev"] == "r002" and not q.get("last_error")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_change_request_that_needed_no_build_never_says_it_built_one(home, builds):
    """h12d_noop: two requests asking for the same change; #1 built r002, then #2 was applied. The chat said "Nothing to
    change: r002 already has that", but the inbox said "Change request #2 built r002", and its record, the activity
    feed and the card's "Built r002" said the same. Each now says #2 needed no build, as the chat does."""
    from test_team_util import members
    st, httpd, call, ids, jar = _team_server(home)
    try:
        root, ms = str(home / "egg"), members(home)
        p1 = collab.create_proposal(root, ids["Vic"], "Quantity to 77", {"build.quantity": 77}, members=ms)
        p2 = collab.create_proposal(root, ids["Vic"], "77 of them", {"build.quantity": 77}, members=ms)
        for p in (p1, p2):
            s, out, _ = call("POST", "/api/proposals/apply", {"id": p["id"]}, headers=jar["Ana"])
            assert s == 202, out
            finish(*(st.room().jobs.get(j["id"]) for j in st.room().jobs.snapshot()))
            settled()
        assert st.room().completed() == ["r001", "r002"]                         # #1 built r002; #2 built nothing
        assert texts(st.room())[-1].startswith("Nothing to change")
        q1, q2 = collab.get_proposal(root, p1["id"]), collab.get_proposal(root, p2["id"])
        assert (q1["status"], q1["applied_rev"], q1.get("noop")) == ("applied", "r002", None)
        assert (q2["status"], q2["applied_rev"], q2.get("noop")) == ("applied", "r002", True)
        for who in ("Vic", "Ana"):
            said = [i["text"] for i in activity.inbox(str(home), ids[who])["items"]]
            assert f"Change request #{p1['n']} built r002" in said, said
            assert f"Change request #{p2['n']} needed no build: r002 already has it" in said, said
            assert not any(t.startswith(f"Change request #{p2['n']} built") for t in said), said
        feed = [i["text"] for i in activity.feed(root, ms)]
        assert f"Change request #{p2['n']} needed no build: r002 already has it" in feed, feed
        s, out, _ = call("GET", "/api/proposals", headers=jar["Ana"])
        built = {x["id"]: x["built"] for x in out["proposals"]}
        assert built[p2["id"]]["noop"] is True and built[p1["id"]]["noop"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()
