"""The robustness studies under attack (B2-S4): what an adversarial review found, each pinned by a test that failed
before its fix. A Studio killed mid-study (its lock, its saved runs, carrying on), studies whose revision was replaced or
whose Morph code changed (never carried on or replayed as theirs), revisions that can't be studied (refused before
anything starts), gripper-only projects, absurd worker counts and tolerance edits, and replays waiting for their turn.
Offline; study records and their runs are written directly, so nothing is simulated."""

import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip("mujoco")

from morph import cli  # noqa: E402
from morph.robot.pipeline2 import RobotProject  # noqa: E402
from morph.services import robust  # noqa: E402
from morph.studio import api_study, jobs, server  # noqa: E402
from study_fixture import egg_project  # noqa: E402

PARAMS = [robust._p(n, -1, 1, 0, "assumed") for n, _, _ in robust.BASE]


@pytest.fixture(scope="module")
def home(tmp_path_factory):
    home = tmp_path_factory.mktemp("projects")
    for name in ("egg-picker", "lock-test", "crashed", "replaced", "no-sim", "tolerant", "replays"):
        egg_project(tmp_path_factory, home, name)       # one each: tests may run in any order on a worker
    return str(home)


@pytest.fixture(scope="module")
def site(home):
    from test_study_api import _call, _start

    st = server.Studio(RobotProject(os.path.join(home, "egg-picker")), home)
    httpd = _start(st)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(st, {f"127.0.0.1:{port}"})
    yield {"st": st, "call": lambda *a, **k: _call(port, *a, **k)}
    jobs.shutdown(st)
    httpd.shutdown()
    httpd.server_close()


def rows_for(params, n, outcome, seed=1):
    """n runs as a study saves them: run i's world from the seed, its outcome from outcome(i, values) (None: placed)."""
    out = []
    for i in range(n):
        vals, _, _ = robust.sample(params, seed, i)
        g = outcome(i, vals)
        out.append({"i": i, "params": vals, "outcome": g or "placed", "group": g or "placed", "success": g is None})
    return out


def saved_study(root, n=40, rows=0, outcome=lambda i, v: None, **record):
    """A study record of r001 written as a study leaves it (record overrides on top), with its first `rows` runs."""
    doc = robust.new_study(root, "r001", runs=n, seed=1, timestep=0.002)
    doc.update(record)
    d = robust.study_dir(root, doc["id"])
    os.makedirs(d, exist_ok=True)
    robust._write_json(os.path.join(d, "study.json"), doc)
    for r in rows_for(doc["spec"]["params"], rows, outcome):
        robust._append(os.path.join(d, "runs.jsonl"), r)
    return doc


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def lock(root, sid, pid, host=None, age=0.0):
    path = os.path.join(root, robust.STUDIES, "active.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"id": sid, "pid": pid, "host": host or socket.gethostname(), "started_utc": robust._utc()}, fh)
    if age:
        t = time.time() - age
        os.utime(path, (t, t))


# ------------------------------------------------------------------ a Studio killed mid-study

def test_a_lock_left_by_a_killed_process_is_stale_at_once(home):
    """The Studio (or `morph study`) was killed: its lock names a process that is gone from this computer, so the study
    reads as interrupted at once (not "running" for 30 s) and a new study may start. A live owner, or one on another
    computer (a shared folder), still holds it until its heartbeat stops."""
    root = os.path.join(home, "lock-test")
    doc = saved_study(root, status="running")
    lock(root, doc["id"], dead_pid())
    assert robust.active(root) is None and robust.load(root, doc["id"])["status"] == "interrupted"
    hj = jobs.HeavyJobs(home)
    assert doc["id"] in [s["id"] for s in hj.interrupted() if s["project"] == "lock-test"]
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock(root, doc["id"], sleeper.pid)
        assert robust.active(root)["pid"] == sleeper.pid
        with pytest.raises(robust.StudyBusy):
            hj.start_study("lock-test", runs=2, timestep=0.002)
    finally:
        sleeper.kill()
        sleeper.wait()
    lock(root, doc["id"], dead_pid(), host="another-computer")
    assert robust.active(root)["host"] == "another-computer"
    lock(root, doc["id"], dead_pid(), host="another-computer", age=robust.STALE_S + 1)
    assert robust.active(root) is None
    assert robust._alive(os.getpid()) and not robust._alive(dead_pid()) and robust._alive(None) and robust._alive(True)
    os.unlink(os.path.join(root, robust.STUDIES, "active.json"))


def test_a_killed_studys_saved_runs_show_and_are_offered_to_carry_on(home, site):
    """A process killed mid-study leaves study.json as it was when the runs began (done 0, no summary): the runs it
    saved are read back (done, and a summary of them) and the card offers to carry on."""
    root = os.path.join(home, "crashed")
    doc = saved_study(root, n=40, rows=8, outcome=lambda i, v: "jam" if i in (2, 5) else None, status="running", done=0)
    lock(root, doc["id"], dead_pid())
    got = robust.load(root, doc["id"])
    assert got["status"] == "interrupted" and got["done"] == 8 and got["summary"]["n"] == 8
    assert got["summary"]["successes"] == 6
    code, info = site["call"]("GET", "/api/study?project=crashed")
    assert code == 200 and info["latest"]["id"] == doc["id"]
    assert info["latest"]["card"]["headline"].startswith("Succeeds in 6 of 8 randomized runs")
    assert any(f.startswith("Stopped when the Studio stopped after 8 of 40 runs") for f in info["latest"]["card"]["flags"])
    assert [(s["id"], s["done"]) for s in info["stopped"]] == [(doc["id"], 8)]
    assert next(s for s in info["studies"] if s["id"] == doc["id"])["p"] == 0.75


# ------------------------------------------------------------------ studies that are no longer theirs

def test_a_replaced_revision_or_new_code_is_never_carried_on_or_replayed_as_the_study(home, site):
    """Reset demo reused the revision's name for another design: carrying the study on would append that design's runs
    to it, and a replay would simulate that design under the study's name. Morph's code changed: carrying on would mix
    two versions. Both are refused before anything runs (409), and neither is offered as a study to carry on."""
    root = os.path.join(home, "replaced")
    doc = saved_study(root, n=40, rows=4, status="cancelled", done=4)
    code_doc = saved_study(root, n=40, rows=4, status="cancelled", done=4, id="c0dec0dec0de", pipeline_version="0" * 12)
    hj = jobs.HeavyJobs(home)
    with pytest.raises(robust.StudyConflict, match="code changed"):
        hj.resume("replaced", code_doc["id"])
    assert robust.load(root, code_doc["id"])["code_changed"] is True
    design = os.path.join(root, "r001", "design.json")
    original = open(design, encoding="utf-8").read()
    d = json.loads(original)
    d["object"]["mass_g"] = d["object"]["mass_g"] + 5
    with open(design, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    try:
        assert robust.load(root, doc["id"])["rev_changed"] is True
        with pytest.raises(robust.StudyConflict, match="replaced"):
            hj.resume("replaced", doc["id"])
        with pytest.raises(robust.StudyConflict, match="replaced"):
            robust.replay(root, doc["id"], 1)
        t = time.time()
        code, out = site["call"]("GET", f"/api/study/replay?project=replaced&id={doc['id']}&run=1")
        assert code == 409 and "replaced" in out["error"] and time.time() - t < 5
        code, out = site["call"]("POST", "/api/study/resume?project=replaced", {"id": doc["id"]})
        assert code == 409 and "replaced" in out["error"]
        code, info = site["call"]("GET", "/api/study?project=replaced")
        assert not {doc["id"], code_doc["id"]} & {s["id"] for s in info["stopped"]}
    finally:
        with open(design, "w", encoding="utf-8") as fh:
            fh.write(original)
    code, info = site["call"]("GET", "/api/study?project=replaced")
    assert code_doc["id"] not in [s["id"] for s in info["stopped"]] and doc["id"] in [s["id"] for s in info["stopped"]]
    code, out = site["call"]("POST", "/api/study/resume?project=replaced", {"id": code_doc["id"]})
    assert code == 409 and "code changed" in out["error"]


# ------------------------------------------------------------------ requests that can't or mustn't run

def test_a_revision_that_cannot_be_studied_is_refused_before_anything_starts(home, site):
    """The simulation stage failed, a preview-only build, no pick and place: 409 with the reason at once, no job and no
    process (it used to answer 202 and fail in the preparing process a second or two later)."""
    root = os.path.join(home, "no-sim")
    res_path = os.path.join(root, "r001", "results.json")
    original = open(res_path, encoding="utf-8").read()
    broken = {"sim failed": lambda r: r.update(errors={"sim": "RuntimeError: the simulation stopped"}),
              "preview": lambda r: r.update(preview_only=True),
              "no pick and place": lambda r: r["sims"].update(scenarios={})}
    try:
        for why, fn in broken.items():
            res = json.loads(original)
            fn(res)
            with open(res_path, "w", encoding="utf-8") as fh:
                json.dump(res, fh)
            code, out = site["call"]("POST", "/api/study?project=no-sim", {"runs": 2, "timestep_ms": 2})
            assert code == 409 and out["error"].startswith("r001 "), (why, code, out)
            assert ("simulation stopped" in out["error"]) == (why == "sim failed")
            assert jobs.manager(site["st"]).status("no-sim") is None
            with pytest.raises(robust.StudyConflict):
                robust.new_study(root, "r001", runs=2)
    finally:
        with open(res_path, "w", encoding="utf-8") as fh:
            fh.write(original)


def test_a_gripper_only_project_is_named_as_one(tmp_path, monkeypatch, capsys):
    """`morph study` on a project of the earlier gripper prototype says so, without starting a study process."""
    root = tmp_path / "raw-egg"
    (root / "r001").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"need": "pick up a raw egg"}), encoding="utf-8")
    (root / "r001" / "results.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "HOME", str(tmp_path))
    assert cli.main(["study", "raw-egg", "--runs", "2", "--workers", "1"]) == 1
    out = capsys.readouterr()
    assert "gripper-only project" in out.err and "rebuilding the robot" not in out.out


def test_worker_counts_stay_within_the_computer(home, monkeypatch, capsys):
    """No more worker processes than the cores this process may use (a container's or taskset's share): the CLI refuses
    more, the engine refuses absurd counts before it locks anything, MORPH_STUDY_WORKERS is capped."""
    monkeypatch.setattr(os, "process_cpu_count", lambda: 2, raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    assert robust.cores() == 2 and robust.default_workers() == 1
    monkeypatch.setenv("MORPH_STUDY_WORKERS", "100000")
    assert robust.default_workers() == 2
    monkeypatch.setattr(cli, "HOME", home)
    t = time.time()
    assert cli.main(["study", "egg-picker", "--runs", "2", "--workers", "100000"]) == 1
    assert "use 1 to 2 worker processes" in capsys.readouterr().err and time.time() - t < 5
    for bad in (0, -3, 100000, True):
        with pytest.raises(robust.StudyError, match="worker processes"):
            robust.run_study(os.path.join(home, "egg-picker"), runs=2, workers=bad)
    assert robust.active(os.path.join(home, "egg-picker")) is None
    assert api_study._cores() == 2


def test_tolerance_edits_take_only_true_or_false(home):
    """"on": "false" used to switch a range on and "reset": "no" to throw every edit away."""
    root = os.path.join(home, "tolerant")
    robust.save_tolerances(root, {"pad_friction": {"hi": 1.2}})
    for bad, says in (({"pad_friction": {"on": "false"}}, "true or false"), ({"pad_friction": {"on": 0}}, "true or false"),
                      ({"reset": "no"}, "reset: true"), ({"pad_friction": "off"}, "{lo, hi, on}")):
        with pytest.raises(robust.StudyError, match=says.replace("{", r"\{").replace("}", r"\}")):
            robust.save_tolerances(root, bad)
    spec = robust.tolerances(root)
    assert next(e for e in spec["params"] if e["name"] == "pad_friction")["hi"] == 1.2       # nothing was lost
    robust.save_tolerances(root, {"reset": True})


def test_replays_are_checked_before_they_wait_and_only_a_few_wait(home, site, monkeypatch):
    """A replay waits its turn (one at a time) holding one of the Studio's connections: a bad request is refused before
    it waits, and past REPLAY_QUEUE waiting replays the next is told to try again at once."""
    root = os.path.join(home, "replays")
    doc = saved_study(root, n=10, rows=10, status="done", done=10, id="ab12ab12ab12")
    monkeypatch.setattr(api_study, "REPLAY_WAIT_S", 3.0)
    assert api_study._REPLAY.acquire(timeout=30)
    try:
        t = time.time()
        code, out = site["call"]("GET", "/api/study/replay?project=replays&id=0123456789ab&run=1")
        assert code == 404 and time.time() - t < 2.5
        code, out = site["call"]("GET", f"/api/study/replay?project=replays&id={doc['id']}&run=99")
        assert code == 404 and "no such run" in out["error"]
        got = {}

        def ask(run):
            got[run] = (site["call"]("GET", f"/api/study/replay?project=replays&id={doc['id']}&run={run}"), time.time())

        t = time.time()
        waiting = [threading.Thread(target=ask, args=(r,)) for r in range(api_study.REPLAY_QUEUE)]
        for th in waiting:
            th.start()
        end = time.time() + 2
        while api_study._WAITING[0] < api_study.REPLAY_QUEUE and time.time() < end:
            time.sleep(0.02)
        ask(9)
        (code, out), when = got[9]
        assert code == 503 and "waiting" in out["error"] and when - t < 2.5
        for th in waiting:
            th.join(30)
        assert all(got[r][0][0] == 503 for r in range(api_study.REPLAY_QUEUE))
        assert api_study._WAITING[0] == 0
    finally:
        api_study._REPLAY.release()


def test_a_preparing_process_whose_study_went_away_ends_quietly(capfd):
    """A Studio killed while its preparing process rebuilds the robot: that process used to print two BrokenPipeError
    tracebacks into the terminal after the Studio had gone."""
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    a, b = ctx.Pipe()
    a.close()                                   # nobody will read the answer
    p = ctx.Process(target=robust._preparer, args=(b, 0, "/nonexistent/morph-project", "r001", 0.002), daemon=True)
    p.start()
    b.close()
    p.join(60)
    assert p.exitcode == 0
    assert "Traceback" not in capfd.readouterr().err


def test_a_job_follows_the_study_it_runs():
    """Tolerances edited while a study waited for its turn make it a new study: the job's id follows the events."""
    hj = jobs.HeavyJobs(os.getcwd())
    job = jobs.Job("p", ".", {}, {"id": "aaaaaaaaaaaa", "rev": "r001", "seed": 1, "stress": False, "timestep": 0.001,
                                 "n": 4}, None)
    hj._progress(job, {"type": "study", "id": "bbbbbbbbbbbb", "status": "preparing", "phase": "prepare"})
    assert job.id == "bbbbbbbbbbbb" and job.view()["id"] == "bbbbbbbbbbbb"
