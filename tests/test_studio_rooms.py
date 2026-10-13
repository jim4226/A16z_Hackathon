"""Rooms: one per project, so tabs on different projects never see each other's events or move each other; a job
queue per room, so a second edit waits instead of being dropped; conflicts are asked about, never overwritten."""

import http.client
import json
import os
import socket
import threading
import time

import pytest

from morph import keys
from morph.robot.pipeline2 import RobotProject
from morph.spec import Design
from morph.studio import server
from morph.studio.rooms import Hub, JobQueue, QueueFull


def make_project(home, name, design=None):
    root = home / name
    (root / "r001").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot", "source": "", "need": name}), encoding="utf-8")
    (root / "r001" / "design.json").write_text((design or Design()).dumps(), encoding="utf-8")
    (root / "r001" / "results.json").write_text(json.dumps({"rev": "r001", "parent": None, "summary": {}}), encoding="utf-8")
    (root / "r001" / "note.txt").write_text(f"file of {name}", encoding="utf-8")
    return RobotProject(str(root))


class FakeBuilds:
    """RobotProject.run without the pipeline: writes design.json, emits start/node, waits on `gate` when set, then
    writes results.json. Records which projects built and how many builds overlapped."""

    def __init__(self):
        self.gate, self.started, self.built, self.active, self.most = None, threading.Event(), [], 0, 0
        self.lock = threading.Lock()

    def __call__(self, p, design, parent=None, label="", verbose=True, on_event=None, by=None, **kw):
        emit = on_event or (lambda e: None)
        rev = p.claim_rev()
        with open(os.path.join(p.rev_dir(rev), "design.json"), "w", encoding="utf-8") as fh:
            fh.write(design.dumps())
        with self.lock:
            self.active += 1
            self.most = max(self.most, self.active)
        emit({"type": "start", "rev": rev, "parent": parent, "changes": {}, "dirty": ["sizing"]})
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        emit({"type": "node", "node": "sizing", "status": "computed", "seconds": 0.01})
        with open(os.path.join(p.rev_dir(rev), "results.json"), "w", encoding="utf-8") as fh:
            json.dump({"rev": rev, "parent": parent, "label": label, "summary": {}, "by": by,
                       "cad": {"parts": 1, "urdf": "cad/urdf/robot.urdf"}}, fh)
        with self.lock:
            self.active -= 1
            self.built.append((os.path.basename(p.root), rev, parent, label))
        return rev


@pytest.fixture
def builds(monkeypatch):
    fake = FakeBuilds()
    monkeypatch.setattr(RobotProject, "run", lambda self, *a, **k: fake(self, *a, **k))
    return fake


@pytest.fixture
def studio(tmp_path):
    home = tmp_path / "projects"
    make_project(home, "egg")
    make_project(home, "so101")
    return server.Studio(RobotProject(str(home / "egg")), str(home))


def collect(hub):
    got = []
    hub.send = got.append
    return got


def finish(*jobs):
    for job in jobs:
        assert job.wait(10), job.info()


# ------------------------------------------------------------------ one room per project

def test_each_project_gets_its_own_room_and_events(studio, builds):
    egg, arm = studio.room("egg"), studio.room("so101")
    assert studio.room() is egg and studio.room("so101") is arm and egg.hub is not arm.hub
    q_egg, q_arm = egg.hub.subscribe(), arm.hub.subscribe()
    finish(studio.queue_params(arm, {"arm.dof": 6}))
    got = [q_arm.get_nowait() for _ in range(q_arm.qsize())]
    assert {e["type"] for e in got} >= {"queue", "assistant", "start", "node", "done"}
    assert all(e["project"] == "so101" for e in got)
    assert [e["type"] for e in got][-2:] == ["done", "queue"] and got[-1]["jobs"][-1]["status"] == "done"
    assert got[-2]["current"]["robot"]["glb"] == "/files/so101/r002/cad/robot.glb"
    assert q_egg.empty()                                    # nothing of so101's reached the egg tab
    assert RobotProject(arm.p.root).revs() == ["r001", "r002"] and egg.p.revs() == ["r001"]
    assert [b[:2] for b in builds.built] == [("so101", "r002")]


def test_payload_urls_name_the_project(studio, builds):
    finish(studio.queue_params(studio.room("so101"), {"arm.dof": 6}))
    st = studio.state(studio.room("so101"))
    assert st["project"] == "so101" and st["start"] == "egg" and st["revs"] == ["r001", "r002"]
    assert st["current"]["rev"] == "r002" and st["previous"]["rev"] == "r001"
    assert st["queue"] == [] and st["building"] is None
    assert server.rev_payload(studio.room("so101").p, "r002", "so101")["manual"] is None
    assert studio.artifact("so101/r001/note.txt").endswith(os.path.join("so101", "r001", "note.txt"))
    assert studio.artifact("r001/note.txt").endswith(os.path.join("egg", "r001", "note.txt"))  # older URLs: start project
    for bad in ("so101/r009/note.txt", "so101/../egg/r001/note.txt", "so101", "nope/r001/note.txt", "/r001/note.txt",
                "so101/r001/../../egg/meta.json", "So101/r001/note.txt", ".trash/r001/x", "so101%2fr001/note.txt%00"):
        assert studio.artifact(bad) is None, bad


def test_a_revision_being_built_is_not_shown_until_it_finishes(studio, builds):
    builds.gate = threading.Event()
    room = studio.room("so101")
    job = studio.queue_params(room, {"arm.dof": 6})
    assert builds.started.wait(10)
    st = studio.state(room)
    assert st["revs"] == ["r001"] and st["current"]["rev"] == "r001"
    assert st["building"] == {"rev": "r002", "parent": "r001", "dirty": ["sizing"], "nodes": {}}
    assert st["queue"][0]["status"] == "running" and st["queue"][0]["position"] == 0
    builds.gate.set()
    finish(job)
    assert studio.state(room)["revs"] == ["r001", "r002"] and studio.state(room)["building"] is None


# ------------------------------------------------------------------ the queue

def test_a_second_edit_waits_and_then_runs_on_the_newest_revision(studio, builds):
    builds.gate = threading.Event()
    room = studio.room()
    events = collect(room.hub)
    first = studio.queue_params(room, {"arm.dof": 6})
    assert builds.started.wait(10)
    second = studio.queue_params(room, {"arm.reach_mm": 380})
    assert [(j["status"], j["position"]) for j in room.jobs.snapshot()] == [("running", 0), ("queued", 1)]
    assert second.status == "queued" and not second.wait(0.2)
    builds.gate.set()
    finish(first, second)
    assert (first.status, first.result, second.status, second.result) == ("done", "r002", "done", "r003")
    assert [b[1:3] for b in builds.built] == [("r002", "r001"), ("r003", "r002")]
    d = room.p.load_design("r003")
    assert d.arm.dof == 6 and d.arm.reach_mm == 380             # applied on top of the first edit, not over it
    queued = [e for e in events if e["type"] == "queue"]
    assert queued[-1]["jobs"][-1] == {**second.info(), "position": None}
    assert all(set(j) >= {"id", "by", "status", "position"} for e in queued for j in e["jobs"])


def test_a_change_to_the_same_setting_since_it_was_asked_is_a_conflict(studio, builds):
    room = studio.room()
    events = collect(room.hub)
    finish(studio.queue_params(room, {"arm.dof": 6}))            # r002, asked from r001
    late = studio.queue_params(room, {"arm.dof": 4}, base_rev="r001")
    other = studio.queue_params(room, {"arm.reach_mm": 380}, base_rev="r001")
    finish(late, other)
    assert late.status == "conflict" and late.conflict == {"keys": ["arm.dof"], "base_rev": "r001", "rev": "r002"}
    ask = [e for e in events if e.get("conflict")][-1]
    assert ask["type"] == "assistant" and ask["conflict"]["job"] == late.id and "Apply it to r002 anyway" in ask["text"]
    assert other.status == "done" and other.result == "r003"   # a different setting goes on top
    assert room.p.load_design("r003").arm.dof == 6
    again = studio.resolve(room, late.id, "apply")
    finish(again)
    assert again.status == "done" and room.p.load_design(again.result).arm.dof == 4 and late.status == "resolved"
    dropped = studio.queue_params(room, {"arm.reach_mm": 300}, base_rev="r001")
    finish(dropped)
    assert dropped.status == "conflict" and studio.resolve(room, dropped.id, "drop") is None
    assert dropped.status == "dropped"
    with pytest.raises(LookupError):
        studio.resolve(room, dropped.id, "apply")


def test_noop_and_errors_keep_their_messages(studio, builds, monkeypatch):
    room = studio.room()
    events = collect(room.hub)
    job = studio.queue_params(room, {"arm.dof": Design().arm.dof})
    finish(job)
    assert job.status == "noop" and events[-2]["noop"] is True and "Nothing to change" in events[-2]["text"]
    bad = studio.queue_changes(room, {"arm.nope": 1}, label="bad")
    finish(bad)
    assert bad.status == "error" and [e for e in events if e["type"] == "assistant"][-1]["text"].startswith("Couldn't apply")

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(RobotProject, "run", boom)
    broken = studio.queue_params(room, {"arm.dof": 6})
    finish(broken)
    assert broken.status == "error" and [e for e in events if e["type"] == "assistant"][-1]["text"] == "Error: disk full"
    assert room.live is None and not room.jobs.busy()


def test_a_chat_or_model_edit_can_not_ask_for_an_impossible_robot(studio, builds, monkeypatch):
    """Claude's tool output (and the rules) may name any design key: a size no robot can have would tie up the shared
    build slot for hours (a 1 km object means millions of gear teeth), so it is refused before anything is built."""
    room = studio.room()
    events = collect(room.hub)
    for changes in ({"object.diameter_mm": 1e9}, {"finger.length_mm": 5000}, {"arm.dof": 12}, {"object.friction": 0},
                    {"fab.quantity": 10 ** 12}, {"task.cycle_time_s": float("nan")}, {"object.shape": ["torus"]}):
        monkeypatch.setattr(server, "interpret", lambda *a, ch=changes: {"changes": ch, "reply": "sure", "source": "claude"})
        job = studio.queue_chat(room, "make it enormous")
        finish(job)
        said = [e for e in events if e["type"] == "assistant"][-1]["text"]
        assert job.status == "error" and said.startswith("Couldn't apply that:"), (changes, said)
    assert builds.built == []
    ok = studio.queue_changes(room, {"object.diameter_mm": 120, "finger.length_mm": 95}, label="big but possible")
    finish(ok)
    assert ok.status == "done" and len(builds.built) == 1


def test_chat_is_interpreted_in_the_room_it_was_sent_from(studio, builds, monkeypatch):
    seen = []

    def interpret(msg, design, grip):
        seen.append((msg, design.arm.dof))
        return {"changes": {"arm.dof": 6}, "reply": "6-DOF.", "source": "rules"}

    monkeypatch.setattr(server, "interpret", interpret)
    builds.gate = threading.Event()
    arm, egg = studio.room("so101"), studio.room("egg")
    blocker = studio.queue_params(arm, {"arm.reach_mm": 380})
    assert builds.started.wait(10)
    events = collect(arm.hub)
    job = studio.queue_chat(arm, "Make it 6-DOF")                # the tab then switches to egg: nothing follows it
    assert events[0] == {"type": "user", "text": "Make it 6-DOF", "job": job.id}   # shown at once, before its turn
    assert seen == []                                               # interpreted when its turn comes, not before
    builds.gate.set()
    finish(blocker, job)
    assert seen == [("Make it 6-DOF", 5)] and job.result == "r003"
    assert arm.p.load_design("r003").arm.reach_mm == 380 and arm.p.load_design("r003").arm.dof == 6
    assert egg.p.revs() == ["r001"]


def test_queue_is_bounded_and_a_waiting_job_can_be_cancelled(studio, builds, monkeypatch):
    monkeypatch.setattr(JobQueue, "MAX_QUEUED", 3)
    builds.gate = threading.Event()
    room = studio.room()
    running = studio.queue_params(room, {"arm.dof": 6})
    assert builds.started.wait(10)
    waiting = [studio.queue_params(room, {k: v}) for k, v in (("arm.reach_mm", 300), ("arm.base_height_mm", 90),
                                                               ("arm.payload_margin", 2.5))]
    with pytest.raises(QueueFull):
        studio.queue_params(room, {"arm.reach_mm": 400})
    assert room.jobs.cancel(waiting[1].id) is waiting[1] and waiting[1].status == "cancelled"
    assert room.jobs.cancel(running.id) is None                # already running
    builds.gate.set()
    finish(running, waiting[0], waiting[2])
    assert [b[1] for b in builds.built] == ["r002", "r003", "r004"] and waiting[2].status == "done"
    later = studio.queue_params(room, {"arm.reach_mm": 320}, base_rev="r002")   # asked before r003 changed the reach
    finish(later)
    assert later.status == "conflict"


def test_one_build_at_a_time_across_projects(studio, builds):
    builds.gate = threading.Event()
    a = studio.queue_params(studio.room("egg"), {"arm.dof": 6})
    b = studio.queue_params(studio.room("so101"), {"arm.dof": 6})
    assert builds.started.wait(10)
    time.sleep(0.3)
    assert builds.active == 1 and b.status == "running"         # so101's job started, but waits for the build slot
    builds.gate.set()
    finish(a, b)
    assert builds.most == 1


def test_more_build_slots_run_projects_side_by_side(tmp_path, builds):
    home = tmp_path / "projects"
    make_project(home, "egg")
    make_project(home, "so101")
    st = server.Studio(RobotProject(str(home / "egg")), str(home), max_builds=2)
    builds.gate = threading.Event()
    jobs = [st.queue_params(st.room(n), {"arm.dof": 6}) for n in ("egg", "so101")]
    for _ in range(100):
        if builds.active == 2:
            break
        time.sleep(0.05)
    assert builds.active == 2
    builds.gate.set()
    finish(*jobs)


def test_a_new_project_build_has_no_previous_revision(tmp_path, builds):
    home = tmp_path / "projects"
    make_project(home, "egg")
    (home / "fresh").mkdir()
    (home / "fresh" / "meta.json").write_text(json.dumps({"kind": "robot"}), encoding="utf-8")
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    room = st.room("fresh")
    assert st.state(room)["revs"] == [] and "current" not in st.state(room)
    events = collect(room.hub)
    finish(st.queue_build(room, Design(), label="first design"))
    done = [e for e in events if e["type"] == "done"][0]
    assert done["previous"] is None and done["current"]["rev"] == "r001" and done["current"]["parent"] is None


# ------------------------------------------------------------------ chat history

def test_chat_is_saved_per_project_and_comes_back_after_a_restart(studio, builds, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "interpret", lambda m, d, g: {"changes": {}, "reply": "Nothing maps to that.",
                                                              "source": "rules"})
    finish(studio.queue_chat(studio.room("so101"), "make it sparkle"))
    saved = tmp_path / "projects" / "so101" / ".morph" / "chat.jsonl"
    lines = [json.loads(x) for x in saved.read_text(encoding="utf-8").splitlines()]
    assert [(e["type"], e["text"]) for e in lines] == [("user", "make it sparkle"), ("assistant", "Nothing maps to that.")]
    assert all(e["project"] == "so101" for e in lines)
    again = server.Studio(RobotProject(str(tmp_path / "projects" / "egg")), str(tmp_path / "projects"))
    assert [e["text"] for e in again.state(again.room("so101"))["history"]] == ["make it sparkle", "Nothing maps to that."]
    assert again.state()["history"] == []                       # egg's chat is egg's


def test_saved_chat_stays_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(Hub, "MAX_HISTORY", 5)
    path = tmp_path / ".morph" / "chat.jsonl"
    hub = Hub("p", str(path))
    for i in range(23):
        hub.send({"type": "assistant", "text": str(i)})
    hub.send({"type": "queue", "jobs": []})                     # live-only events are never saved
    hub.send({"type": "assistant", "text": "private", "to": "m1"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 10 and json.loads(lines[-1])["text"] == "22"
    assert [e["text"] for e in Hub("p", str(path)).history] == ["18", "19", "20", "21", "22"]
    path.write_text("not json\n" + json.dumps({"type": "user", "text": 5}) + "\n" + json.dumps(["x"]) + "\n"
                    + json.dumps({"type": "user", "text": "ok"}) + "\n", encoding="utf-8")
    assert [e["text"] for e in Hub("p", str(path)).history] == ["ok"]
    hub.clear_history()
    assert not path.exists() and hub.history == []


def test_events_for_one_member_reach_only_that_member():
    hub = Hub("p")
    ana, ben, anon = hub.subscribe("m-ana"), hub.subscribe("m-ben"), hub.subscribe()
    hub.send({"type": "inbox", "to": "m-ana", "items": []})
    hub.send({"type": "user", "text": "hi"})
    assert [e["type"] for e in (ana.get_nowait(), ana.get_nowait())] == ["inbox", "user"]
    assert ben.get_nowait()["type"] == "user" and ben.empty() and anon.get_nowait()["type"] == "user"
    assert sorted(hub.online(), key=str) == sorted(["m-ana", "m-ben", None], key=str)
    assert hub.history == [{"type": "user", "text": "hi", "project": "p"}]


# ------------------------------------------------------------------ over HTTP: two tabs, two projects

@pytest.fixture
def http_studio(studio, tmp_path, monkeypatch):
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(studio, None))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(studio, {f"127.0.0.1:{port}", f"localhost:{port}"})
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def request(path, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Content-Type": "application/json", "X-Morph-Client": "1"} if method != "GET" else {}
        try:
            conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
            res = conn.getresponse()
            return res.status, json.loads(res.read() or b"null")
        finally:
            conn.close()

    yield studio, request, port
    httpd.shutdown()
    httpd.server_close()


class Stream:
    """A tab's event stream, read straight from the socket."""

    def __init__(self, port, project):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.sock.sendall(f"GET /api/events?project={project} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.sock.recv(4096)
        self.buf = self.buf.split(b"\r\n\r\n", 1)[1]

    def events(self, until: str, timeout=10):
        out, deadline = [], time.time() + timeout
        self.sock.settimeout(0.2)
        while time.time() < deadline:
            while b"\n\n" in self.buf:
                chunk, self.buf = self.buf.split(b"\n\n", 1)
                if chunk.startswith(b"data: "):
                    out.append(json.loads(chunk[6:]))
                    if out[-1]["type"] == until:
                        return out
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                break
            self.buf += data
        return out

    def close(self):
        self.sock.close()


def test_two_tabs_on_two_projects_stay_independent(http_studio, builds):
    studio, request, port = http_studio
    egg_tab, arm_tab = Stream(port, "egg"), Stream(port, "so101")
    try:
        for _ in range(50):  # both subscribed
            if studio.room("egg").hub.online() and studio.room("so101").hub.online():
                break
            time.sleep(0.02)
        code, out = request("/api/params?project=so101", "POST", {"changes": {"arm.dof": 6}})
        assert code == 202 and out["job"]["status"] in ("queued", "running")
        got = arm_tab.events(until="done")
        assert got[-1]["type"] == "done" and got[-1]["current"]["rev"] == "r002" and {e["project"] for e in got} == {"so101"}
        assert got[-1]["current"]["robot"]["json"] == "/files/so101/r002/cad/robot.json"
        assert egg_tab.events(until="done", timeout=0.5) == []   # the egg tab saw nothing
        code, out = request("/api/project", "POST", {"name": "so101"})          # an older page "switching"
        assert code == 200 and request("/api/state")[1]["project"] == "egg"     # moved nobody
        assert request("/api/state?project=so101")[1]["revs"] == ["r001", "r002"]
        assert request("/api/state?project=egg")[1]["revs"] == ["r001"]
        assert request("/api/rev?project=so101&rev=r002")[0] == 200 and request("/api/rev?rev=r002")[0] == 404
        code, out = request("/api/chat?project=egg", "POST", {"message": "Make it 6-DOF", "base_rev": "r001"})
        assert code == 202
        got = egg_tab.events(until="done")
        assert got[0]["type"] in ("user", "queue") and got[-1]["current"]["rev"] == "r002"
        assert {e["project"] for e in got} == {"egg"}
    finally:
        egg_tab.close()
        arm_tab.close()


def test_files_are_served_per_project(http_studio):
    _, request, port = http_studio

    def get(path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path)
            res = conn.getresponse()
            return res.status, res.read()
        finally:
            conn.close()

    assert get("/files/so101/r001/note.txt") == (200, b"file of so101")
    assert get("/files/egg/r001/note.txt") == (200, b"file of egg")
    assert get("/files/r001/note.txt") == (200, b"file of egg")                    # older pages: the start project
    for bad in ("/files/so101/r001/../../egg/meta.json", "/files/so101/..%2f..%2fegg/meta.json", "/files/nope/r001/x",
                "/files/so101/meta.json", "/files/so101/r002/note.txt", "/files/so101", "/files/so101/"):
        assert get(bad)[0] == 404, bad


def test_queue_full_is_a_clear_refusal(http_studio, builds, monkeypatch):
    studio, request, _ = http_studio
    monkeypatch.setattr(JobQueue, "MAX_QUEUED", 1)
    builds.gate = threading.Event()
    try:
        assert request("/api/params", "POST", {"changes": {"arm.dof": 6}})[0] == 202
        assert builds.started.wait(10)
        assert request("/api/params", "POST", {"changes": {"arm.dof": 4}})[0] == 202
        code, out = request("/api/params", "POST", {"changes": {"arm.reach_mm": 300}})
        assert code == 429 and "already waiting" in out["error"]
        code, out = request("/api/jobs")
        assert [j["status"] for j in out["jobs"]] == ["running", "queued"]
        job = out["jobs"][1]["id"]
        assert request("/api/jobs/cancel", "POST", {"job": job})[0] == 200
        assert request("/api/jobs/cancel", "POST", {"job": job})[0] == 409
        assert request("/api/jobs/resolve", "POST", {"job": job, "action": "apply"})[0] == 409
        assert request("/api/jobs/resolve", "POST", {"job": job, "action": "maybe"})[0] == 400
    finally:
        builds.gate.set()
