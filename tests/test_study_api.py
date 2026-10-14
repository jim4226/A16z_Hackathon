"""Robustness studies over HTTP (morph/studio/api_study.py) in a real Studio on a free port: a study started, followed
through its `study` events (progress with partial results, then done), read back with the card's sentences, a run
replayed with its frames; sizes and bad requests refused in plain English; one study per project; cancel keeps the
finished runs and the study carries on; tolerance edits make a study outdated; deleting a project, Reset demo and
stopping the Studio stop its study; and the Teams role matrix (viewers read and replay, editors start and stop).
Offline, 2 ms simulations of the egg-picker."""

import http.client
import json
import os
import re
import shutil
import threading
import time
import types

import pytest

pytest.importorskip("mujoco")

from morph import keys  # noqa: E402
from morph.robot.pipeline2 import RobotProject  # noqa: E402
from morph.services import robust  # noqa: E402
from morph.studio import api_study, api_team, jobs, projects, server  # noqa: E402
from morph.studio.teams import auth, roles  # noqa: E402
from study_fixture import egg_project  # noqa: E402

CACHE = {}                         # packed robots (2 ms) shared by every Studio in this module
FAST = {"timestep_ms": 2, "workers": 2}
ROUTES = [("GET", "/api/study", "viewer"), ("POST", "/api/study", "editor"), ("POST", "/api/study/cancel", "editor"),
          ("POST", "/api/study/resume", "editor"), ("GET", "/api/study/tolerances", "viewer"),
          ("POST", "/api/study/tolerances", "editor"), ("GET", "/api/study/replay", "viewer")]


@pytest.fixture(scope="module")
def env():
    mp = pytest.MonkeyPatch()
    mp.setenv("MORPH_STUDY_NICE", "0")
    yield mp
    mp.undo()


def _start(st, allowed=None):
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(st, allowed))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return httpd


def _call(port, method, path, body=None, headers=None):
    hdrs = {"X-Morph-Client": "1"} if method != "GET" else {}
    hdrs.update(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        raw = r.read()
    finally:
        conn.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw


class Events:
    """A tab's event stream (/api/events) read on a thread."""

    def __init__(self, port, project, headers=None):
        self.items, self.conn = [], http.client.HTTPConnection("127.0.0.1", port, timeout=300)
        self.conn.request("GET", f"/api/events?project={project}", headers=headers or {})
        self.r = self.conn.getresponse()
        assert self.r.status == 200
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.r.fp:
                if line.startswith(b"data: "):
                    self.items.append(json.loads(line[6:]))
        except (OSError, ValueError):
            pass

    def studies(self):
        return [e for e in self.items if e.get("type") == "study"]

    def close(self):
        self.conn.close()


def until(fn, timeout=240.0, step=0.3):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("timed out")


@pytest.fixture(scope="module")
def site(tmp_path_factory, env):
    """A local Studio started on egg-picker, with a robot of its own for each test (pytest-xdist may run them on
    different workers, each with its own Studio)."""
    home = tmp_path_factory.mktemp("projects")
    for name in ("egg-picker", "cancel-me", "tolerant", "delete-me", "demo-arm"):
        egg_project(tmp_path_factory, home, name, CACHE)
    keys_env = env
    keys_env.setattr(keys, "ENV_PATH", str(home / ".env-test"))
    st = server.Studio(RobotProject(str(home / "egg-picker")), str(home))
    jobs.manager(st).cache.update(CACHE)
    httpd = _start(st)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(st, {f"127.0.0.1:{port}"})
    yield {"st": st, "port": port, "home": str(home), "call": lambda *a, **k: _call(port, *a, **k)}
    jobs.shutdown(st)
    httpd.shutdown()
    httpd.server_close()


def _finish(call, project="egg-picker", timeout=300):
    until(lambda: (call("GET", f"/api/study?project={project}")[1]["job"] or {}).get("status") in
          ("done", "cancelled", "error"), timeout)
    return call("GET", f"/api/study?project={project}")[1]


# ------------------------------------------------------------------ the card's sentences

def _doc(k, n, nominal=True, stress=False, **extra):
    s = {"n": n, "successes": k, "p": k / n, "wilson95": list(robust.wilson(k, n)), "unstable": [], "errors": [], "groups": []}
    if k in (0, n):
        s["clopper_pearson95"] = list(robust.clopper_pearson(k, n))
    return {"summary": s, "stress": stress, "model": {"nominal": {"success": nominal}, "matches_revision": True},
            "status": "done", "done": n, "n": n, "rev": "r002", **extra}


def test_the_card_says_what_the_study_shows_and_no_more():
    assert api_study.headline(_doc(140, 200)) == ("Succeeds in 140 of 200 randomized runs (95% CI 63-76%); the nominal "
                                                  "run succeeds.")
    assert api_study.headline(_doc(200, 200)) == ("Succeeds in all 200 randomized runs (at least 98.1% with 95% "
                                                  "confidence); the nominal run succeeds.")   # exact bound 98.17%, rounded down
    assert api_study.headline(_doc(0, 200, nominal=False)) == ("Succeeds in 0 of 200 randomized runs (95% CI 0-1.9%); "
                                                               "the nominal run fails.")
    assert api_study.headline(_doc(1, 1)) == "Succeeds in its one randomized run (at least 2.5% with 95% confidence); " \
                                             "the nominal run succeeds."
    assert api_study.headline(_doc(82, 195, stress=True)).startswith("With every tolerance doubled (stress test), "
                                                                     "succeeds in 82 of 195 randomized runs (95% CI 35-50%)")  # Wilson 35.3-49.1, outward
    assert api_study.headline({"summary": {"n": 0}}) is None
    assert api_study.interval(0.6331, 0.7589) == "63-76%" and api_study.interval(0.9817, 0.9999) == "98.1-100.0%"
    group = {"id": "jam", "label": "Jaws stayed shut at release: the egg was carried away", "count": 58, "rate": 0.29,
             "wilson95": [0.2311, 0.3568], "example_run": 107, "model_sensitive": True,
             "rule": {"text": "egg turned 3.4-9.4 deg: 49% fail this way vs 13% otherwise", "significant": True},
             "checks": {"half_timestep": {"runs": 20, "still_fail": 16}, "impratio_1": {"runs": 20, "still_fail": 1}}}
    doc = _doc(140, 200, outdated=True)
    doc["summary"]["groups"] = [group, {**group, "id": "drag", "label": "Dragged off the target", "count": 2,
                                        "model_sensitive": False, "rule": {"text": "x", "significant": False}, "checks": {}}]
    doc["summary"]["unstable"] = [4]
    doc["model"]["matches_revision"] = False
    c = api_study.card(doc)
    assert c["note"] == "This is simulation, for the tolerances shown; not a field failure rate."
    assert c["flags"] == [api_study.NOTES["outdated"], "1 run was numerically unstable and is not counted.",
                          "r002 was simulated with other Morph code; this study uses today's model of it."]
    jam, drag = c["groups"]
    assert jam["share"] == "58 of 200 runs (23-36%, 95% CI)" and jam["rule"].endswith("13% otherwise")
    assert jam["sensitivity"] == "This result changes with the simulator's friction or timestep settings. Test it on the " \
                                 "real robot." and jam["example_run"] == 107
    assert jam["checks"] == "Re-run at half the timestep, 16 of 20 still fail this way; with impratio=1, 1 of 20."
    assert drag["rule"] == "x (could be chance)" and drag["sensitivity"] == api_study.NOTES["robust"] and drag["checks"] is None
    cut = api_study.card({**_doc(30, 30), "status": "interrupted", "done": 30, "n": 200, "rev_changed": True})
    assert cut["flags"][0] == api_study.NOTES["rev_changed"]
    assert cut["flags"][1].startswith("Stopped when the Studio stopped after 30 of 200 runs; the numbers are for those runs.")
    assert api_study.NOTES["none"] == "One nominal run. Small differences in placement or hardware aren't tested."
    for text in (*api_study.NOTES.values(), jam["share"], jam["checks"], cut["flags"][1]):
        assert text.isascii()


def test_every_study_route_has_its_role_in_the_team_table():
    declared = {(r.method, r.path): r.role for r in server.routes.routes() if r.path.startswith("/api/study")}
    assert declared == {(m, p): ("editor" if role == "editor" else "viewer") for m, p, role in ROUTES}
    owner, editor, viewer = ({"id": "m", "role": r} for r in ("owner", "editor", "viewer"))
    for method, path, role in ROUTES:
        assert roles.required(method, path) == (role, None)
        assert roles.authorize(owner, method, path) and roles.authorize(editor, method, path)
        assert roles.authorize(viewer, method, path) is (role == "viewer")



def test_the_time_estimate_uses_this_computers_newest_timed_study():
    class Hj:
        def __init__(self):
            self.read = []

        def result(self, project, sid):
            self.read.append(sid)
            if sid == "gone":
                raise robust.StudyError("There is no such study.")
            return {"b": {"id": "b", "timestep": 0.002, "timing": {"runs_per_s": 1.2, "workers": 3, "runs": 40}}}.get(sid)

    hj, cut = Hj(), {"id": "a", "timing": {"workers": 3, "first_result_s": 7.3}}     # stopped before its runs were timed
    listing = [{"id": "a"}, {"id": "gone"}, {"id": "b"}, {"id": "c"}]
    assert api_study._rate(hj, "p", listing, [cut]) == {"per_worker": 0.2, "measured": True}   # 2 ms runs: half the cost
    assert hj.read == ["gone", "b"]                     # the shown study is not read again; the first timed one is enough
    assert api_study._rate(Hj(), "p", [], []) == {"per_worker": api_study.RATE_PER_WORKER, "measured": False}
    few = {"id": "s", "timing": {"runs_per_s": 2.0, "workers": 2, "runs": 6}}           # too few runs to time
    assert api_study._rate(Hj(), "p", [], [few])["measured"] is False

# ------------------------------------------------------------------ a study over HTTP

def test_a_study_runs_with_live_progress_and_reads_back_with_its_card(site):
    call, port = site["call"], site["port"]
    s, info = call("GET", "/api/study")
    assert s == 200 and info["project"] == "egg-picker" and info["rev"] == "r001" and info["latest"] is None
    assert info["job"] is None and info["studies"] == [] and info["may"] == {"start": True}
    assert info["limits"]["max_runs"] == 10_000 and info["limits"]["max_workers"] == robust.cores()
    assert info["host"]["cores"] == robust.cores() and info["host"]["remote"] is False
    assert info["notes"]["none"] == api_study.NOTES["none"] and info["rate"]["measured"] is False
    tab = Events(port, "egg-picker")
    try:
        s, out = call("POST", "/api/study", {"runs": 6, "seed": 3, **FAST})
        assert s == 202 and out["job"]["status"] == "queued" and out["job"]["n"] == 6
        sid = out["job"]["id"]
        s, busy = call("POST", "/api/study", {"runs": 2, **FAST})
        assert s == 409 and "already" in busy["error"]
        done = _finish(call)
        until(lambda: any(e.get("status") == "done" for e in tab.studies()), 30)
    finally:
        tab.close()
    events = tab.studies()
    assert all(e["id"] == sid and e["project"] == "egg-picker" for e in events)
    runs = [e for e in events if e.get("phase") == "runs" and e["status"] == "running"]
    assert runs and all(e["done"] <= 6 and "groups" in e and e["successes"] + e["failures"] <= e["done"] for e in runs)
    last = events[-1]
    assert last["status"] == "done" and last["summary"]["n"] + len(robust.load(os.path.join(site["home"], "egg-picker"),
                                                                                 sid)["summary"]["unstable"]) == 6
    latest = done["latest"]
    assert latest["id"] == sid and latest["status"] == "done" and latest["outdated"] is False
    k, n = latest["summary"]["successes"], latest["summary"]["n"]
    assert re.fullmatch(rf"Succeeds in (all {n}|{k} of {n}) randomized runs? \(.*\); the nominal run succeeds\.",
                        latest["card"]["headline"]), latest["card"]["headline"]
    assert "runs" not in latest and latest["spec"]["params"][0]["source"].startswith("assumed")
    assert [s["id"] for s in done["studies"]] == [sid] and done["rate"]["measured"] is False      # 6 runs: too few to time
    s, one = call("GET", f"/api/study?id={sid}")
    assert s == 200 and one["study"]["card"] == latest["card"]
    assert call("GET", "/api/study?id=../../etc")[0] == 404 and call("GET", "/api/study?rev=r009")[0] == 404
    # replay a run: its frames, in a study process, the same outcome as recorded
    rows = robust.load(os.path.join(site["home"], "egg-picker"), sid, rows=True)["runs"]
    run = next((r for r in rows if not r["success"]), rows[0])
    t = time.time()
    s, doc = call("GET", f"/api/study/replay?id={sid}&run={run['i']}")
    took = time.time() - t
    assert s == 200 and doc["scenario"] == "pick_place" and len(doc["frames"]) > 100 and doc["joints"]
    rb = doc["robustness"]
    assert (rb["study"], rb["run"], rb["outcome"], rb["rev"]) == (sid, run["i"], run["outcome"], "r001")
    assert rb["matches_record"] is True and rb["recorded"]["outcome"] == run["outcome"] and rb["label"]
    t = time.time()
    assert call("GET", f"/api/study/replay?id={sid}&run={run['i']}")[1] == doc and time.time() - t < min(1.0, took)   # kept
    for q, status in (("run=abc", 400), ("run=", 400), ("run=99", 404), ("run=1&variant=softer", 404)):
        assert call("GET", f"/api/study/replay?id={sid}&{q}")[0] == status, q
    assert call("GET", "/api/study/replay?id=nope&run=1")[0] == 404



def test_a_replay_that_cannot_run_now_says_to_try_again(monkeypatch):
    class Busy:
        def __init__(self, exc):
            self.exc = exc

        def replay(self, *a):
            raise self.exc

        def result(self, *a):                 # the study and its run exist: the replay itself fails
            return {"n": 10, "done": 10, "rev_changed": False}

    room = types.SimpleNamespace(name="egg-picker", p=types.SimpleNamespace(root="/nowhere"))
    req = types.SimpleNamespace(room=room, studio=None, arg={"id": "s1", "run": "3"}.get)
    for exc, status, words in (
            (robust.StudyError("The replay took too long; try again when the computer is less busy."), 503, "took too long"),
            (robust.StudyError("The replay's simulation process stopped."), 503, "process stopped"),
            (OSError(24, "Too many open files"), 503, "couldn't start a simulation process for the replay (Too many open "
                                                      "files); try again in a moment."),
            (robust.StudyError("Run 3 stopped with an error in the simulation: unstable"), 400, "Run 3 stopped")):
        monkeypatch.setattr(jobs, "manager", lambda studio, hj=Busy(exc): hj)
        got, body = api_study.replay(req)
        assert got == status and words in body["error"] and body["ok"] is False, (exc, body)
    assert api_study._REPLAY.acquire(timeout=0) and api_study._REPLAY.release() is None   # every failure let go of it

def test_sizes_and_bad_requests_are_refused_in_plain_english(site):
    call = site["call"]
    cores = robust.cores()                  # the cores this process may use
    for body, status, words in (
            ({"runs": 0}, 400, "Ask for 1 to 10,000 runs at 1 ms steps"),
            ({"runs": 10_001}, 400, "For more, run `morph study` in a terminal"),
            ({"runs": 5_001, "timestep_ms": 0.5}, 400, "Ask for 1 to 5,000 runs at 0.5 ms steps"),
            ({"runs": 2, "timestep_ms": 3}, 400, "The timestep must be 0.5, 1 or 2 ms."),
            ({"runs": 2, "timestep_ms": True}, 400, "timestep"),
            ({"runs": 2, "workers": cores + 1}, 400, f"use 1 to {cores} worker processes"),
            ({"runs": 2, "workers": 0}, 400, "worker processes"),
            ({"runs": 2.5}, 400, "Ask for"), ({"runs": True}, 400, "Ask for"),
            ({"runs": 2, "seed": -1}, 400, "The seed must be a whole number"),
            ({"runs": 2, "stress": "yes"}, 400, "true or false"),
            ({"runs": 2, "rev": 2}, 400, "revision name"),
            ({"runs": 2, "rev": "r009"}, 404, "This project has no finished revision r009."),
            ({"runs": 2, "rev": "../r001"}, 404, "There is no such revision.")):
        s, out = call("POST", "/api/study", body)
        assert s == status and words in out["error"] and out["error"].isascii(), (body, s, out)
    assert call("POST", "/api/study?project=nope", {"runs": 2})[0] == 404
    assert call("POST", "/api/study", b"[1]")[0] == 400
    s, out = call("POST", "/api/study/cancel", {})
    assert s == 409 and out["error"] == "No robustness study is running for egg-picker."
    assert call("POST", "/api/study/resume", {})[0] == 400 and call("POST", "/api/study/resume", {"id": "abc"})[0] == 404
    assert call("POST", "/api/study", {"runs": 2}, headers={"X-Morph-Client": ""})[0] == 403


def test_cancel_keeps_the_finished_runs_and_the_study_carries_on(site):
    call, root, q = site["call"], os.path.join(site["home"], "cancel-me"), "?project=cancel-me"
    s, out = call("POST", f"/api/study{q}", {"runs": 8, "seed": 8, "checks": False, "workers": 1, "timestep_ms": 2})
    assert s == 202
    sid = out["job"]["id"]
    until(lambda: ((call("GET", f"/api/study{q}")[1]["job"] or {}).get("done") or 0) >= 2)
    t = time.time()
    s, out = call("POST", f"/api/study/cancel{q}", {})
    assert s == 200 and out["job"]["id"] == sid
    info = _finish(call, "cancel-me")
    assert time.time() - t < 3.0 and info["job"]["status"] == "cancelled"
    kept = robust.load(root, sid)["done"]
    assert 2 <= kept < 8
    assert info["stopped"] == [{"id": sid, "rev": "r001", "done": kept, "n": 8, "status": "cancelled", "stress": False}]
    assert info["latest"]["id"] == sid and "Cancelled after" in info["latest"]["card"]["flags"][0]
    s, out = call("POST", f"/api/study/resume{q}", {"id": sid})
    assert s == 202 and out["job"]["id"] == sid
    info = _finish(call, "cancel-me")
    assert info["job"]["status"] == "done" and robust.load(root, sid)["done"] == 8 and not info["stopped"]
    assert call("POST", f"/api/study/cancel{q}", {})[0] == 409


def test_tolerance_edits_make_the_study_outdated_until_they_are_reset(site):
    q = "?project=tolerant"
    call = lambda method, path, *a, **k: site["call"](method, path + q, *a, **k)  # noqa: E731
    assert call("POST", "/api/study", {"runs": 2, "seed": 11, "checks": False, **FAST})[0] == 202
    _finish(site["call"], "tolerant")
    s, out = call("GET", "/api/study/tolerances")
    assert s == 200
    spec = {e["name"]: e for e in out["spec"]["params"]}
    assert spec["object_mass_g"]["source"].startswith("USDA") and spec["object_mass_g"]["limit"] == [6.0, 600.0]
    assert spec["object_yaw_deg"]["limit"] == [-180, 180] and out["spec"]["presets"] == ["room", "standard"]
    assert out["spec"]["preset_labels"]["room"].startswith("Only the room varies")
    before = call("GET", "/api/study")[1]["latest"]
    assert before and before["outdated"] is False
    s, out = call("POST", "/api/study/tolerances", {"changes": {"object_yaw_deg": {"lo": -5, "hi": 5}}})
    yaw = next(e for e in out["spec"]["params"] if e["name"] == "object_yaw_deg")
    assert s == 200 and (yaw["lo"], yaw["hi"], yaw["edited"]) == (-5.0, 5.0, True)
    after = call("GET", "/api/study")[1]
    assert after["latest"]["outdated"] is True and api_study.NOTES["outdated"] in after["latest"]["card"]["flags"]
    for body, words in (({"changes": {"object_yaw_deg": {"lo": 5, "hi": -5}}}, "low end is above the high end"),
                        ({"changes": {"object_size": {"hi": 50}}}, "Object size: hi must be a number from 0.3 to 3"),
                        ({"changes": {"nope": {"lo": 1}}}, "There is no tolerance called nope."),
                        ({"changes": {"mount_dx_mm": {"lo": -1}}}, "only applies in World mode"),
                        ({"changes": {}}, "Send {changes"), ({"changes": [1]}, "Send {changes"),
                        ({"changes": {"reset": True}, "rev": 3}, "revision name")):
        s, out = call("POST", "/api/study/tolerances", body)
        assert s in (400, 404) and words in out["error"], (body, out)
    s, _ = call("POST", "/api/study/tolerances", {"changes": {"reset": True}})
    assert s == 200 and call("GET", "/api/study")[1]["latest"]["outdated"] is False


def test_deleting_a_project_resetting_the_demo_and_stopping_the_studio_stop_its_study(site):
    call, st, home = site["call"], site["st"], site["home"]

    def running(project, seed):
        s, out = call("POST", f"/api/study?project={project}", {"runs": 40, "seed": seed, "checks": False, **FAST})
        assert s == 202
        until(lambda: ((call("GET", f"/api/study?project={project}")[1]["job"] or {}).get("done") or 0) >= 1)
        return out["job"]["id"]

    sid = running("delete-me", 5)
    s, out = call("DELETE", "/api/projects?project=delete-me")
    assert s == 200 and jobs.manager(st).status("delete-me")["status"] == "cancelled"
    saved = robust.load(os.path.join(home, out["trash"]), sid)          # its finished runs went to the trash with it
    assert saved["status"] == "cancelled" and 1 <= saved["done"] < 40
    # Reset demo: a demo project's study stops first (the revision kept is current, so nothing is rebuilt)
    root = os.path.join(home, "demo-arm")
    projects.write_meta(root, {**projects.read_meta(root), "demo": True})
    res = os.path.join(root, "r001", "results.json")
    with open(res, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["pipeline_version"] = server.pipeline_version("")
    with open(res, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    running("demo-arm", 6)
    assert st.reset() is True and jobs.manager(st).status("demo-arm")["status"] == "cancelled"
    # the Studio stops: its studies stop and keep their runs (the same settings carry on next time)
    sid = running("demo-arm", 7)
    jobs.shutdown(st)
    assert jobs.manager(st).status("demo-arm")["status"] == "cancelled" and robust.active(root) is None
    assert robust.load(root, sid)["done"] >= 1


# ------------------------------------------------------------------ Teams: viewers read, editors start

def test_team_viewers_read_and_replay_editors_start_and_stop(tmp_path_factory, env, tmp_path):
    from test_team_util import make_team
    home = tmp_path / "projects"
    egg_project(tmp_path_factory, home, "egg-picker", CACHE)
    env.setattr(keys, "ENV_PATH", str(tmp_path / ".env-test"))
    team = (("Jim", "owner", ["generalist"]), ("Ana", "editor", ["simulation"]), ("Vic", "viewer", []))
    ids, cookies = make_team(str(home), team)
    st = server.Studio(RobotProject(str(home / "egg-picker")), str(home), team=True)
    api_team.configure(home=st.home, secure_cookies=False, enabled=True)
    jobs.manager(st).cache.update(CACHE)
    httpd = _start(st)
    port = httpd.server_address[1]
    jar = {n: {"Cookie": f"{auth.cookie_name(port)}={c}"} for n, c in cookies.items()}
    try:
        call = lambda *a, who, **k: _call(port, *a, headers=jar[who], **k)  # noqa: E731
        s, info = call("GET", "/api/study", who="Vic")
        assert s == 200 and info["may"] == {"start": False}
        assert call("GET", "/api/study", who="Ana")[1]["may"] == {"start": True}
        assert call("GET", "/api/study/tolerances", who="Vic")[0] == 200
        for method, path, body in (("POST", "/api/study", {"runs": 2, **FAST}), ("POST", "/api/study/cancel", {}),
                                   ("POST", "/api/study/resume", {"id": "a" * 12}),
                                   ("POST", "/api/study/tolerances", {"changes": {"reset": True}})):
            assert call(method, path, body, who="Vic")[0] == 403, path
        assert _call(port, "GET", "/api/study")[0] == 401                     # signed out
        s, out = call("POST", "/api/study", {"runs": 3, "seed": 2, "checks": False, **FAST}, who="Ana")
        assert s == 202 and out["job"]["by"] == ids["Ana"]
        sid = out["job"]["id"]
        until(lambda: (call("GET", "/api/study", who="Vic")[1]["job"] or {}).get("status") == "done")
        doc = robust.load(str(home / "egg-picker"), sid)
        assert doc["by"] == ids["Ana"] and call("GET", "/api/study", who="Vic")[1]["latest"]["id"] == sid
        s, rep = call("GET", f"/api/study/replay?id={sid}&run=0", who="Vic")
        assert s == 200 and rep["robustness"]["run"] == 0 and rep["frames"]
    finally:
        jobs.shutdown(st)
        api_team.configure(home=None, enabled=False)
        httpd.shutdown()
        httpd.server_close()
    shutil.rmtree(home, ignore_errors=True)
