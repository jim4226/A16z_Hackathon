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


def test_clean_stops_during_a_first_build_are_no_tries(home, tmp_path, builds):
    """Ctrl+C (serve() returning) during a long first build, twice: a person closing the Studio is no sign the build
    stops it, so it is built again at each start and never waits to be asked for. A crash after that still counts."""
    st, where = server.Studio(RobotProject(str(home / "egg")), str(home)), home
    builds.gate = threading.Event()
    room, job = st.create_project("kb1", need="a test")
    jobs = [job]
    for n in range(1, 4):
        assert builds.started.wait(5)
        st.stopping()                                                       # Ctrl+C: serve()'s finally
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
