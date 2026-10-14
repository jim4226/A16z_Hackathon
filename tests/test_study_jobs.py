"""Heavy jobs (morph/studio/jobs.py) and `morph study`: a study reports its progress to the project's room, one runs
per project and the others wait their turn, cancelling takes under 2 s and keeps the finished runs, a study whose
process stopped is found again and resumes to the same result, and the room's build queue never waits for a study.
Offline, 2 ms simulations of the egg-picker."""

import json
import os
import threading
import time

import pytest

pytest.importorskip("mujoco")

from morph import cli  # noqa: E402
from morph.services import robust  # noqa: E402
from morph.studio import jobs as heavy  # noqa: E402
from morph.studio.rooms import Room  # noqa: E402
from study_fixture import egg_project  # noqa: E402

FAST = {"timestep": 0.002, "workers": 2}
CACHE = {}                         # packed robots shared by the managers below (one per project)


@pytest.fixture(scope="module")
def home(tmp_path_factory, monkeypatch_module):
    home = tmp_path_factory.mktemp("home")
    for name in ("egg-picker", "egg-picker-2"):
        egg_project(tmp_path_factory, home, name, CACHE)
    return str(home)


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    mp.setenv("MORPH_STUDY_NICE", "0")
    yield mp
    mp.undo()


class Events:
    def __init__(self):
        self.items, self.lock = [], threading.Lock()

    def __call__(self, project, event):
        with self.lock:
            self.items.append((project, event))

    def of(self, project):
        with self.lock:
            return [e for p, e in self.items if p == project]


def until(fn, timeout=120.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("timed out")


def test_a_study_reports_progress_to_its_room_and_finishes(home):
    ev = Events()
    jobs = heavy.HeavyJobs(home, emit=ev, cache=CACHE)
    job = jobs.start_study("egg-picker", runs=4, seed=3, **FAST)
    assert job["status"] == "queued" and job["n"] == 4 and job["kind"] == "study"
    assert jobs.wait("egg-picker", 300)
    st = jobs.status("egg-picker")
    assert st["status"] == "done" and st["done"] == 4 and st["error"] is None
    events = ev.of("egg-picker")
    assert events[0]["status"] == "queued" and all(e["type"] == "study" and e["id"] == job["id"] for e in events)
    assert [e["phase"] for e in events if "phase" in e][0] == "prepare"
    assert {"runs", "checks", "done"} <= {e.get("phase") for e in events}
    last = events[-1]
    assert last["status"] == "done" and last["summary"]["n"] + len(robust.load(os.path.join(home, "egg-picker"),
                                                                                 job["id"])["summary"]["unstable"]) == 4
    assert last["ci"] and 0 <= last["p"] <= 1 and last["first_result_s"] > 0
    runs = [e for e in events if e.get("phase") == "runs" and "groups" in e]   # partial results: failures so far, by group
    assert runs and all(sum(e["groups"].values()) == e["failures"] for e in runs)
    doc = jobs.result("egg-picker", job["id"], rows=True)
    assert len(doc["runs"]) == 4 and doc["checks"] and doc["summary"]["checks"]
    assert [s["id"] for s in jobs.studies("egg-picker")] == [job["id"]]
    with pytest.raises(heavy.StudyError):
        jobs.start_study("no-such-project", runs=2)
    with pytest.raises(heavy.StudyError):
        jobs.start_study("egg-picker", runs=2, workers=0)


def test_one_study_per_project_and_the_others_wait_their_turn(home):
    ev = Events()
    jobs = heavy.HeavyJobs(home, emit=ev, max_running=1, cache=CACHE)
    a = jobs.start_study("egg-picker", runs=6, seed=4, checks=False, **FAST)
    assert a["status"] == "queued"
    with pytest.raises(heavy.StudyBusy):
        jobs.start_study("egg-picker", runs=2, seed=5, **FAST)
    b = jobs.start_study("egg-picker-2", runs=2, seed=4, checks=False, **FAST)
    assert b["status"] == "queued"
    until(lambda: jobs.status("egg-picker")["status"] in ("preparing", "running"))
    assert jobs.status("egg-picker-2")["status"] == "queued"            # waits for the other project's study
    assert jobs.wait("egg-picker", 300) and jobs.wait("egg-picker-2", 300)
    assert jobs.status("egg-picker")["status"] == jobs.status("egg-picker-2")["status"] == "done"
    c = jobs.start_study("egg-picker", runs=6, seed=6, checks=False, **FAST)
    d = jobs.start_study("egg-picker-2", runs=2, seed=6, checks=False, **FAST)
    cancelled = jobs.cancel("egg-picker-2")                            # still waiting: cancelled at once
    assert cancelled["status"] == "cancelled" and jobs.status("egg-picker-2")["id"] == d["id"]
    assert ev.of("egg-picker-2")[-1]["status"] == "cancelled"
    assert jobs.wait("egg-picker", 300) and jobs.status("egg-picker")["id"] == c["id"]
    assert jobs.cancel("egg-picker") is None                            # nothing left to cancel


def test_cancel_takes_under_two_seconds_keeps_finished_runs_and_resume_finishes(home):
    root = os.path.join(home, "egg-picker")
    jobs = heavy.HeavyJobs(home, emit=Events(), cache=CACHE)
    job = jobs.start_study("egg-picker", runs=12, seed=8, checks=False, **FAST)
    until(lambda: (jobs.status("egg-picker")["done"] or 0) >= 2)
    t = time.time()
    jobs.cancel("egg-picker")
    assert jobs.wait("egg-picker", 10)
    took = time.time() - t
    st = jobs.status("egg-picker")
    assert st["status"] == "cancelled" and took < 2.0 and st["cancel_s"] < 2.0
    folder = os.path.join(root, ".studies", job["id"])
    with open(os.path.join(folder, "runs.jsonl"), encoding="utf-8") as fh:
        kept = fh.read()
    saved = robust.load(root, job["id"])
    assert saved["status"] == "cancelled" and 2 <= saved["done"] < 12 and len(kept.splitlines()) == saved["done"]
    assert robust.active(root) is None
    # a Studio stopped mid-study: the record still says "running", nobody holds the lock
    raw = json.load(open(os.path.join(folder, "study.json"), encoding="utf-8"))
    raw["status"] = "running"
    with open(os.path.join(folder, "study.json"), "w", encoding="utf-8") as fh:
        json.dump(raw, fh)
    restarted = heavy.HeavyJobs(home, emit=Events(), cache=CACHE)
    assert {"project": "egg-picker", "id": job["id"], "rev": "r001", "done": saved["done"], "n": 12} in restarted.interrupted()
    assert restarted.resume("egg-picker", job["id"])["status"] == "queued"
    assert restarted.wait("egg-picker", 300) and restarted.status("egg-picker")["status"] == "done"
    with open(os.path.join(folder, "runs.jsonl"), encoding="utf-8") as fh:
        rows = fh.read()
    assert rows.startswith(kept) and len(rows.splitlines()) == 12
    done = robust.load(root, job["id"], rows=True)
    assert [r["i"] for r in done["runs"]] == list(range(12)) and done["status"] == "done"
    assert restarted.interrupted() == []


def test_the_build_queue_never_waits_for_a_study(home):
    root = os.path.join(home, "egg-picker-2")
    room = Room("egg-picker-2", root)
    jobs = heavy.HeavyJobs(home, emit=lambda p, e: room.hub.send(e), cache=CACHE)
    q = room.hub.subscribe()
    jobs.start_study("egg-picker-2", runs=16, seed=9, checks=False, **FAST)
    until(lambda: (jobs.status("egg-picker-2")["done"] or 0) >= 1)
    ran = []
    t = time.time()
    build = room.jobs.submit("change", lambda job: ran.append(time.time() - t), label="a build while the study runs")
    assert build.wait(5) and build.status == "done" and ran[0] < 1.0
    with room.lock:                                     # a build holding the room: the study goes on
        before = jobs.status("egg-picker-2")["done"]
        until(lambda: jobs.status("egg-picker-2")["done"] > before or jobs.status("egg-picker-2")["status"] == "done")
    jobs.cancel("egg-picker-2")
    jobs.wait("egg-picker-2", 10)
    seen = []
    while not q.empty():
        seen.append(q.get_nowait())
    assert any(e["type"] == "study" and e["project"] == "egg-picker-2" for e in seen)
    assert any(e["type"] == "queue" for e in seen)


def test_morph_study_prints_a_report_and_carries_on(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "HOME", home)
    args = ["study", "egg-picker-2", "--runs", "3", "--workers", "1", "--timestep", "2", "--seed", "21", "--no-checks"]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "robustness study (beta)" in out and "3 of 3 randomized runs" in out and "Saved in" in out
    assert robust.NOTE in out and out.isascii()
    assert cli.main(args[:3] + ["5"] + args[4:]) == 0                    # deeper: two more runs, the first three kept
    assert "5 of 5 randomized runs" in capsys.readouterr().out
    assert cli.main(["study", "egg-picker-2", "--rev", "r009", "--runs", "1", "--workers", "1"]) == 1
    out = capsys.readouterr()
    assert "no finished revision" in out.err and "1 randomized run on 1 worker process." in out.out


def test_the_studios_hooks_stop_a_projects_study(home):
    """A Studio stops a project's study before deleting or resetting it (jobs.stop) and every study when it stops
    (jobs.shutdown); a Studio that never ran a heavy job has nothing to stop."""
    from types import SimpleNamespace

    assert heavy.stop(SimpleNamespace(), "egg-picker") is True
    heavy.shutdown(SimpleNamespace())
    studio = SimpleNamespace(heavy_jobs=heavy.HeavyJobs(home, emit=Events(), cache=CACHE))
    assert heavy.stop(studio, "egg-picker") is True                     # nothing running
    studio.heavy_jobs.start_study("egg-picker", runs=12, seed=23, checks=False, **FAST)
    until(lambda: (studio.heavy_jobs.status("egg-picker")["done"] or 0) >= 1)
    t = time.time()
    assert heavy.stop(studio, "egg-picker") is True and time.time() - t < 5.0
    assert studio.heavy_jobs.status("egg-picker")["status"] == "cancelled"
    studio.heavy_jobs.start_study("egg-picker-2", runs=12, seed=23, checks=False, **FAST)
    until(lambda: (studio.heavy_jobs.status("egg-picker-2")["done"] or 0) >= 1)
    heavy.shutdown(studio)
    assert studio.heavy_jobs.status("egg-picker-2")["status"] == "cancelled"
    assert robust.active(os.path.join(home, "egg-picker-2")) is None
