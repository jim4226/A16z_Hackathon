"""Robustness studies (morph/services/robust.py): tolerances and their sources, seeded sampling (identical to the
published research study), Wilson and exact intervals, failure rules, model-sensitivity labels, worker processes
(spawn) that give the same runs on any number of workers and batch size, saved studies that resume and deepen, and
replays. Offline; the egg-picker revision here is built in a few seconds with 2 ms simulations. MORPH_SLOW=1 adds
the acceptance study (200 runs at 1 ms: 140 succeed, the yaw band, model-sensitive) and the full determinism matrix."""

import json
import math
import os
import threading
import time

import numpy as np
import pytest

mujoco = pytest.importorskip("mujoco")

from morph.robot import arm, electronics2, simulate  # noqa: E402
from morph.services import robust  # noqa: E402
from morph.spec import Design  # noqa: E402
from morph.studio import projects  # noqa: E402
from study_fixture import egg_project, make_revision  # noqa: E402

SLOW = os.environ.get("MORPH_SLOW") == "1"
TIMING = ("wall_s", "cpu_s")
# run 0 of the published study (research: seed 1, default tolerances; its parameters rounded to 5 places)
PUBLISHED_RUN0 = {"object_across_mm": 0.18915, "object_along_mm": 7.20742, "object_yaw_deg": -10.67521,
                  "object_size": 1.07178, "object_mass_g": 56.86029, "pad_friction": 0.954, "grip_force": 1.16385,
                  "servo_torque": 0.82276, "servo_deadband_deg": 0.2748, "encoder_noise": 0.04134,
                  "setpoint_hz": 158.09723}


CACHE = {}                         # the packed robot (robust.prepare at 2 ms) for the module's studies


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    return egg_project(tmp_path_factory, tmp_path_factory.mktemp("home"), cache=CACHE)


@pytest.fixture(scope="module")
def prepared(project):
    return CACHE[robust.cache_key(project, "r001", 0.002)]


def _plain(row):
    return {k: v for k, v in row.items() if k not in TIMING}


def _jobs(doc_like, n, boxes=0):
    return [robust._job(doc_like, i, boxes) for i in range(n)]


def _doc(project, timestep=0.002, seed=1):
    return {"spec": robust.tolerances(project, "r001"), "seed": seed, "timestep": timestep}


# ------------------------------------------------------------------ tolerances and sampling

def test_default_tolerances_and_their_sources():
    spec = robust.default_spec(projects.template("egg-picker"))
    p = {e["name"]: e for e in spec["params"]}
    assert [e["name"] for e in spec["params"]] == [n for n, _, _ in robust.BASE]
    assert (p["object_mass_g"]["lo"], p["object_mass_g"]["hi"], p["object_mass_g"]["nominal"]) == (50.0, 72.0, 60.0)
    assert "USDA" in p["object_mass_g"]["source"] and "FSR 400" in p["grip_force"]["source"]
    assert "ST3215" in p["servo_torque"]["source"] and (p["servo_torque"]["lo"], p["servo_torque"]["hi"]) == (0.7, 1.0)
    assert (p["object_yaw_deg"]["lo"], p["object_yaw_deg"]["hi"]) == (-15.0, 15.0) and spec["encoder_cpr"] == 4096
    assert spec["world_preset"] is None and all(e["on"] for e in spec["params"])
    can = Design().with_changes({"object.name": "soda can", "object.mass_g": 370.0, "arm.actuator_family": "dynamixel"})
    c = {e["name"]: e for e in robust.default_spec(can)["params"]}
    assert (c["object_mass_g"]["lo"], c["object_mass_g"]["hi"]) == (314.5, 425.5) and "USDA" not in c["object_mass_g"]["source"]
    assert c["servo_torque"]["source"].startswith("assumed")
    assert robust.default_spec(Design().with_changes({"arm.actuator_family": "hobby_pwm"}))["encoder_cpr"] == 1024
    w = robust.default_spec(projects.template("egg-picker"), world={"obstacles": []})
    assert [e["name"] for e in w["params"]][-4:] == ["mount_dx_mm", "mount_dy_mm", "mount_yaw_deg", "box_error_mm"]
    assert w["world_preset"] == "standard"
    seen = []
    robust.register_world_preset("test-scan", lambda world, design: seen.append(world) or robust._standard_world(world, design))
    try:
        robust.default_spec(projects.template("egg-picker"), world={"id": "x"}, preset="test-scan")
        assert seen == [{"id": "x"}]
    finally:
        robust.WORLD_PRESETS.pop("test-scan")


def test_sampling_matches_the_published_study():
    params = robust.default_spec(projects.template("egg-picker"))["params"]
    vals, dirs, noise = robust.sample(params, 1, 0)
    assert {k: round(v, 5) for k, v in vals.items()} == PUBLISHED_RUN0
    again = robust.sample(params, 1, 0)
    assert again[0] == vals and list(again[1]) == list(dirs) and again[2] == noise
    off = [dict(e, on=False) if e["name"] == "object_yaw_deg" else e for e in params]
    v2, _, n2 = robust.sample(off, 1, 0)
    assert v2["object_yaw_deg"] == 0.0 and n2 == noise and all(v2[k] == vals[k] for k in vals if k != "object_yaw_deg")
    assert robust.sample(params, 1, 1)[0] != vals and robust.sample(params, 2, 0)[0] != vals


def test_stress_doubles_every_range_about_its_nominal():
    s = {e["name"]: (e["lo"], e["hi"]) for e in robust.stressed(robust.default_spec(projects.template("egg-picker")))["params"]}
    assert s["object_yaw_deg"] == (-30.0, 30.0) and s["object_mass_g"] == (40.0, 84.0)
    assert s["servo_torque"] == pytest.approx((0.4, 1.0)) and s["setpoint_hz"] == (15.0, 200.0)
    assert s["servo_deadband_deg"] == (0.0, 1.0) and s["object_size"] == pytest.approx((0.84, 1.16))
    assert s["pad_friction"] == pytest.approx((0.4, 1.6)) and s["grip_force"] == pytest.approx((0.5, 1.5))


# ------------------------------------------------------------------ statistics and rules

def test_wilson_and_exact_intervals():
    lo, hi = robust.wilson(140, 200)
    assert (round(lo, 3), round(hi, 3)) == (0.633, 0.759)
    assert round(robust.clopper_pearson(200, 200)[0], 3) == 0.982
    assert round(robust.clopper_pearson(1000, 1000)[0], 3) == 0.996
    assert round(robust.clopper_pearson(10000, 10000)[0], 4) == 0.9996
    assert robust.clopper_pearson(0, 200) == (0.0, pytest.approx(1 - 0.982, abs=5e-4))
    lo, hi = robust.clopper_pearson(140, 200)
    assert lo < robust.wilson(140, 200)[0] and hi > robust.wilson(140, 200)[1]   # exact is the wider of the two here


def _brute(x, y):
    """The research prototype's search, one interval at a time."""
    cuts = np.unique(np.quantile(x, np.linspace(0.0, 1.0, 26)))
    P, N, best = y.sum(), len(y) - y.sum(), None
    for ia in range(len(cuts)):
        for ib in range(ia, len(cuts)):
            if ia == 0 and ib == len(cuts) - 1:
                continue
            sel = (x >= cuts[ia]) & (x <= cuts[ib])
            j = (sel & (y == 1)).sum() / P - (sel & (y == 0)).sum() / N
            if best is None or j > best[0] + 1e-12:
                best = (j, cuts[ia], cuts[ib])
    return best


def test_interval_rules_match_a_brute_force_search():
    rng = np.random.default_rng(5)
    for trial in range(6):
        x = np.round(rng.uniform(-15, 15, 150), 1 if trial % 2 else 5)       # with and without ties
        y = ((np.abs(x) > 3) & (np.abs(x) < 9) & (rng.random(150) < 0.7)).astype(float)
        f = robust._Feature("object_yaw_deg", "value", x)
        J, tp, fp = f.scores(y)
        k = int(np.argmax(J))
        a, b = divmod(k, len(f.cuts))
        j, lo, hi = _brute(x, y)
        assert (round(float(J[a, b]), 9), f.cuts[a], f.cuts[b]) == (round(j, 9), lo, hi)
        sel = (x >= lo) & (x <= hi)
        assert (tp[a, b], fp[a, b]) == ((sel & (y == 1)).sum(), (sel & (y == 0)).sum())


def _rows(n=200, seed=3):
    """Synthetic runs: a jam whenever the egg is turned 3.5-9 degrees (every other such run), a missed grasp when it is
    more than 6 mm across the jaws from its mark."""
    rng = np.random.default_rng(seed)
    params = robust.default_spec(projects.template("egg-picker"))["params"]
    rows = []
    for i in range(n):
        v, _, _ = robust.sample(params, seed, i)
        jam = 3.5 < abs(v["object_yaw_deg"]) < 9 and i % 2 == 0
        miss = v["object_across_mm"] > 6
        out = "missed_grasp" if miss else "jam" if jam else "placed"
        rows.append({"i": i, "params": v, "outcome": out, "group": out, "success": out == "placed"})
    rows.append({"i": n, "params": robust.sample(params, seed, n)[0], "outcome": "unstable", "group": "unstable",
                 "success": False})
    return params, rows, rng


def test_failure_groups_get_rules_rates_and_model_sensitivity():
    params, rows, _ = _rows()
    pick, oks = robust.check_selection(rows, 3)
    assert set(pick) == {"jam", "missed_grasp"} and all(len(v) <= robust.CHECK_RUNS for v in pick.values())
    assert len(oks) == robust.CHECK_SUCCESSES and robust.check_selection(rows, 3) == (pick, oks)
    checks = []
    for v in robust.VARIANTS:
        for i in sorted({i for ids in pick.values() for i in ids} | set(oks)):
            r = rows[i]
            g = r["group"]
            if g == "jam" and (v == "impratio_1" or i % 3 == 0):       # the jam goes away with softer friction
                g = "placed"
            checks.append({"i": i, "variant": v, "group": g, "outcome": g, "success": g == "placed"})
    s = robust.analyze(rows, params, checks, seed=3, obj="egg")
    counted = [r for r in rows if r["outcome"] != "unstable"]
    assert s["n"] == len(counted) == 200 and s["unstable"] == [200]
    assert s["successes"] == sum(r["success"] for r in counted)
    assert s["wilson95"] == [round(v, 4) for v in robust.wilson(s["successes"], 200)]
    g = {x["id"]: x for x in s["groups"]}
    jam, miss = g["jam"], g["missed_grasp"]
    assert jam["rule"]["param"] == "object_yaw_deg" and jam["rule"]["on"] == "deviation" and jam["rule"]["significant"]
    assert 2.5 <= jam["rule"]["lo"] <= 4.5 and 8 <= jam["rule"]["hi"] <= 10
    assert jam["rule"]["text"].startswith("egg turned ") and " fail vs " in jam["rule"]["text"]
    assert jam["model_sensitive"] is True and jam["checks"]["impratio_1"]["still_fail"] == 0
    assert miss["rule"]["param"] == "object_across_mm" and miss["model_sensitive"] is False
    assert jam["label"] == "Jaws stayed shut at release: the egg was carried away"
    assert s["sensitivity"][0]["param"] in ("object_yaw_deg", "object_across_mm") and s["chance_J"] < jam["rule"]["J"]
    assert robust.analyze(rows, params, None, seed=3)["groups"][0].get("model_sensitive", "absent") == "absent"


def test_all_successes_get_the_exact_bound():
    params = robust.default_spec(projects.template("egg-picker"))["params"]
    rows = [{"i": i, "params": robust.sample(params, 1, i)[0], "outcome": "placed", "group": "placed", "success": True}
            for i in range(200)]
    s = robust.analyze(rows, params)
    assert s["p"] == 1.0 and round(s["clopper_pearson95"][0], 3) == 0.982 and s["groups"] == []
    doc = {"project": "egg-picker", "rev": "r001", "id": "0" * 12, "n": 200, "done": 200, "seed": 1, "timestep": 0.001,
           "status": "done", "stress": False, "summary": s, "spec": {"params": params}}
    text = robust.report(doc)
    assert "Succeeds in all 200 runs: at least 98.2% (95%, exact)." in text and text.isascii()


# ------------------------------------------------------------------ the robot, packed for workers

def test_prepare_rebuilds_the_revisions_model_and_workers_run_it_exactly(project, prepared):
    assert prepared["matches_revision"] is True and prepared["revision_timestep"] == 0.002
    assert prepared["nominal"]["success"] is True and prepared["boxes"] == 0 and len(prepared["blob"]) < 8 << 20
    runner = robust.Runner(prepared["blob"])
    assert isinstance(runner.rob.asm, robust.Skeleton) and not hasattr(runner.rob.asm, "parts")
    job = _jobs(_doc(project), 1)[0]
    d = projects.template("egg-picker")
    sz = arm.size(d)
    rob = simulate._make_robot(arm.build(d, sz, electronics2.plan(d, {**sz, "grip_target_n": 2.34})), d, False,
                               {"timestep": 0.002})
    plan, at = simulate._plan_pick_place(rob, d, False)
    with simulate._perturbed(rob, job["perturb"]):
        _, met = simulate._run_pick_place(rob, d, plan, at, job["perturb"])
    row = runner.run(job)
    assert row["peak_grip_n"] == met["grip_n_max"] and row["slip_mm"] == met["max_slip_mm"]
    assert _plain(runner.run(job)) == _plain(row)                          # the same run twice in one worker
    full = runner.run(job, doc=True)
    assert full["frames"] and full["robustness"]["outcome"] == row["outcome"]
    with pytest.raises(robust.StudyError):
        robust.prepare(project, "r002")


def test_runs_are_the_same_on_any_number_of_workers_and_batches(project, prepared):
    jobs = _jobs(_doc(project), 6)
    seen = {}
    for workers, batch in ((1, 2), (2, 4), (4, 1)):
        pool = robust.Pool(workers, nice=0, blob=prepared["blob"])
        assert pool.ctx.get_start_method() == "spawn"
        rows = []
        try:
            assert pool.drive(jobs, batch, rows.append)
            info = pool.info()
        finally:
            pool.close()
        assert len(info) == workers and all(w["start_s"] is not None for w in info)
        seen[(workers, batch)] = sorted((_plain(r) for r in rows), key=lambda r: r["i"])
    first = next(iter(seen.values()))
    assert [r["i"] for r in first] == list(range(6))
    assert all(rows == first for rows in seen.values())


# ------------------------------------------------------------------ saved studies

def test_a_study_saves_resumes_and_deepens(project, prepared):
    events = []
    doc = robust.run_study(project, runs=4, workers=2, timestep=0.002, checks=False, progress=events.append, nice=0,
                           cache=CACHE)
    sid = doc["id"]
    assert doc["status"] == "done" and doc["done"] == 4 and doc["summary"]["n"] + len(doc["summary"]["unstable"]) == 4
    assert doc["model"]["matches_revision"] is True and doc["timing"]["first_result_s"] is not None
    assert events[0]["phase"] == "prepare" and events[-1]["status"] == "done" and "summary" in events[-1]
    assert [e["done"] for e in events if e["phase"] == "runs"] == [1, 2, 3, 4]
    folder = os.path.join(project, ".studies", sid)
    with open(os.path.join(folder, "runs.jsonl"), encoding="utf-8") as fh:
        first = fh.read()
    assert robust.active(project) is None and not os.path.exists(os.path.join(project, ".studies", "active.json"))
    again = robust.run_study(project, runs=6, workers=2, timestep=0.002, checks=False, nice=0, cache=CACHE)
    assert again["id"] == sid and again["done"] == 6 and again["n"] == 6
    with open(os.path.join(folder, "runs.jsonl"), encoding="utf-8") as fh:
        assert fh.read().startswith(first)                              # the first four were not run again
    same = robust.run_study(project, runs=6, timestep=0.002, checks=False, cache=CACHE)        # nothing left to run
    assert same["done"] == 6 and same["timing"] == again["timing"]
    loaded = robust.load(project, sid, rows=True)
    assert loaded["status"] == "done" and loaded["outdated"] is False and [r["i"] for r in loaded["runs"]] == list(range(6))
    assert [s["id"] for s in robust.studies(project)] == [sid]
    text = robust.report(loaded)
    assert text.isascii() and "6 of 6 randomized runs" in text and robust.NOTE in text
    with pytest.raises(robust.StudyError):
        robust.load(project, "../etc")
    for bad in ({"runs": 0}, {"runs": True}, {"seed": -1}, {"timestep": 0.5}, {"rev": "r999"}, {"rev": "../r001"}):
        with pytest.raises(robust.StudyError):
            robust.run_study(project, **{"runs": 2, **bad})


def test_one_study_per_project_even_across_processes(project):
    robust._lock(project, "a" * 12)
    try:
        with pytest.raises(robust.StudyBusy):
            robust.run_study(project, runs=2, timestep=0.002)
        assert robust.active(project)["id"] == "a" * 12
    finally:
        robust._unlock(project, "a" * 12)
    stale = os.path.join(project, ".studies", "active.json")
    with open(stale, "w", encoding="utf-8") as fh:
        json.dump({"id": "b" * 12, "pid": -1}, fh)
    os.utime(stale, (time.time() - 120, time.time() - 120))           # its process stopped two minutes ago
    assert robust.active(project) is None
    robust._lock(project, "c" * 12)
    robust._unlock(project, "c" * 12)


def test_tolerance_edits_are_checked_saved_and_make_old_studies_outdated(project, prepared):
    doc = robust.run_study(project, runs=2, workers=1, timestep=0.002, checks=False, seed=7, nice=0, cache=CACHE)
    base = robust.tolerances(project)
    spec = robust.save_tolerances(project, {"object_yaw_deg": {"lo": -5, "hi": 5}, "encoder_noise": {"on": False}}, by="ana")
    p = {e["name"]: e for e in spec["params"]}
    assert (p["object_yaw_deg"]["lo"], p["object_yaw_deg"]["hi"], p["object_yaw_deg"]["edited"]) == (-5.0, 5.0, True)
    assert p["encoder_noise"]["on"] is False and spec["sha"] != base["sha"]
    assert robust.load(project, doc["id"])["outdated"] is True
    for bad in ({"object_yaw_deg": {"lo": 5, "hi": -5}}, {"object_yaw_deg": {"lo": float("nan")}},
                {"object_size": {"hi": 50}}, {"pad_friction": {"lo": "low"}}, {"nope": {"lo": 1}},
                {"mount_dx_mm": {"lo": -1}}, {"world_preset": "nope"}, {"object_mass_g": {"hi": 6000}}, []):
        with pytest.raises(robust.StudyError):
            robust.save_tolerances(project, bad)
    assert robust.tolerances(project)["sha"] == spec["sha"]
    edited = robust.run_study(project, runs=2, workers=1, timestep=0.002, checks=False, seed=7, nice=0, cache=CACHE)
    assert edited["id"] != doc["id"] and edited["base_spec_sha"] == spec["sha"]
    vals = [r["params"]["encoder_noise"] for r in robust.load(project, edited["id"], rows=True)["runs"]]
    assert vals == [0.0, 0.0]
    assert robust.save_tolerances(project, {"reset": True})["sha"] == base["sha"]
    assert robust.load(project, doc["id"])["outdated"] is False and robust.load(project, edited["id"])["outdated"] is True


def test_replay_gives_a_runs_frames(project, prepared):
    doc = robust.run_study(project, runs=2, workers=1, timestep=0.002, checks=False, seed=11, nice=0, cache=CACHE)
    row = robust.load(project, doc["id"], rows=True)["runs"][1]
    out = robust.replay(project, doc["id"], 1, cache=CACHE)
    assert out["scenario"] == "pick_place" and len(out["frames"]) > 100 and out["metrics"]
    rb = out["robustness"]
    assert (rb["study"], rb["run"], rb["outcome"], rb["place_error_mm"]) == (doc["id"], 1, row["outcome"], row["place_error_mm"])
    for bad in (2, -1, True, "1"):
        with pytest.raises(robust.StudyError):
            robust.replay(project, doc["id"], bad, cache=CACHE)


# ------------------------------------------------------------------ acceptance (MORPH_SLOW=1)

@pytest.mark.skipif(not SLOW, reason="MORPH_SLOW=1: 200 runs at 1 ms plus the solver checks (minutes)")
def test_acceptance_the_published_study(tmp_path):
    """The egg-picker at default tolerances and 1 ms steps: 140 of 200 succeed (Wilson 63.3-75.9 %); the jam at
    release is concentrated where the egg is turned a few degrees and the solver checks label it model-sensitive."""
    project = make_revision(tmp_path / "home", fast=False)
    events, t0 = [], time.time()
    doc = robust.run_study(project, runs=200, progress=events.append, nice=0)
    s = doc["summary"]
    print("\n" + robust.report(doc))
    print(json.dumps(doc["timing"], indent=1))
    assert doc["model"]["matches_revision"] is True and doc["model"]["nominal"]["success"] is True
    assert (s["n"], s["successes"]) == (200, 140) and [round(v, 3) for v in s["wilson95"]] == [0.633, 0.759]
    jam = s["groups"][0]
    assert jam["id"] == "jam" and jam["count"] >= 50 and jam["model_sensitive"] is True
    r = jam["rule"]
    assert r["param"] == "object_yaw_deg" and r["on"] == "deviation" and 2.0 <= r["lo"] <= 4.5 and 6.5 <= r["hi"] <= 10.0
    assert r["significant"] and jam["checks"]["impratio_1"]["still_fail"] <= 0.2 * jam["checks"]["impratio_1"]["runs"]
    assert doc["timing"]["first_result_s"] < 15 and time.time() - t0 < 900


@pytest.mark.skipif(not SLOW, reason="MORPH_SLOW=1: 40 runs at 1 ms on 1, 2 and 4 workers in batches of 2, 4 and 8")
def test_acceptance_determinism_matrix(tmp_path):
    project = make_revision(tmp_path / "home", fast=False)
    prepared = robust.prepare(project, "r001", 0.001)
    jobs = _jobs({"spec": robust.tolerances(project, "r001"), "seed": 1, "timestep": 0.001}, 40)
    seen = {}
    for workers in (1, 2, 4):
        for batch in (2, 4, 8):
            pool = robust.Pool(workers, nice=0, blob=prepared["blob"])
            rows = []
            t = time.time()
            try:
                assert pool.drive(jobs, batch, rows.append)
            finally:
                pool.close()
            print(f"\n{workers} workers, batch {batch}: {len(rows) / (time.time() - t):.2f} runs/s")
            seen[(workers, batch)] = sorted((_plain(r) for r in rows), key=lambda r: r["i"])
    first = seen[(1, 2)]
    assert len(first) == 40 and all(rows == first for rows in seen.values())
    assert sum(r["success"] for r in first) == sum(1 for r in first if r["outcome"] == "placed")


def test_spawn_workers_ignore_ctrl_c_and_start_quickly(project, prepared):
    """Workers leave Ctrl+C to their parent (which stops them) and load the robot without the CAD libraries."""
    pool = robust.Pool(1, nice=0, blob=prepared["blob"])
    try:
        assert pool.drive(_jobs(_doc(project), 1), 1, lambda r: None)
        w = pool.info()[0]
        assert w["load_s"] < 3.0 and (w["rss_mb"] is None or w["rss_mb"] < 250)
    finally:
        t = time.time()
        pool.close(kill=True)
        assert time.time() - t < 2.0
    assert all(not w["p"].is_alive() for w in pool.workers)


def test_a_worker_that_dies_has_its_runs_tried_again(project, prepared, monkeypatch):
    pool = robust.Pool(2, nice=0, blob=prepared["blob"])
    rows, killed = [], threading.Event()

    def on_row(r):
        rows.append(r)
        if not killed.is_set():
            killed.set()
            victim = next(w for w in pool.workers if w["jobs"] and not w["dead"])
            victim["p"].kill()
    try:
        assert pool.drive(_jobs(_doc(project), 6), 3, on_row)
    finally:
        pool.close()
    assert sorted(r["i"] for r in rows) == list(range(6)) and all(r["outcome"] != "error" for r in rows)
    assert math.isfinite(sum(r["place_error_mm"] for r in rows))
