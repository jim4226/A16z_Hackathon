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
    assert jam["rule"]["text"].startswith("egg turned ") and " fail this way vs " in jam["rule"]["text"]
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
    assert "Succeeds in all 200 runs: at least 98.1% with 95% confidence." in text and text.isascii()   # as the card
    assert robust.INTERVAL_NOTE not in text and "CI" not in text and "solver" not in text


def test_the_report_says_it_as_the_card_does():
    """`morph study` prints the card's words (W3 UX-10): "likely between" for the 95% interval, with the note saying
    so, and "Same with other simulator settings" or "Model-sensitive" for a failure group's checks, never "95% CI" or
    "robust to solver settings"."""
    from morph.studio import api_study

    params = robust.default_spec(projects.template("egg-picker"))["params"]
    outs = ["placed"] * 6 + ["jam"] * 4
    rows = [{"i": i, "params": robust.sample(params, 1, i)[0], "outcome": o, "group": o, "success": o == "placed"}
            for i, o in enumerate(outs)]
    s = robust.analyze(rows, params)
    s["groups"][0].update(model_sensitive=False, checks={"half_timestep": {"runs": 4, "still_fail": 4},
                                                        "impratio_1": {"runs": 4, "still_fail": 3}})
    doc = {"project": "egg-picker", "rev": "r001", "id": "0" * 12, "n": 10, "done": 10, "seed": 1, "timestep": 0.001,
           "status": "done", "stress": False, "summary": s, "spec": {"params": params}}
    text = robust.report(doc)
    lo, hi = s["wilson95"]
    assert f"Succeeds in 6 of 10 runs: 60.0% ({api_study.likely(lo, hi)})." in text, text
    assert api_study.card(doc)["headline"].startswith(f"Succeeds in 6 of 10 randomized runs ({api_study.likely(lo, hi)})")
    assert robust.INTERVAL_NOTE in text and api_study.card(doc)["interval_note"] == robust.INTERVAL_NOTE
    assert "       Same with other simulator settings: at half the timestep 4 of 4 still fail this way" in text, text
    s["groups"][0]["model_sensitive"] = True
    assert "       Model-sensitive: at half the timestep" in robust.report(doc)
    zero = robust.analyze([{**r, "outcome": "jam", "group": "jam", "success": False} for r in rows], params)
    text = robust.report({**doc, "summary": zero})
    assert "Succeeds in 0 of 10 runs: likely at most 30.9%." in text, text
    for old in ("95% CI", "robust to solver", "solver settings", "(95%, exact)"):
        assert old not in text and old not in robust.report(doc), old


def test_the_report_counts_single_runs_in_plain_english():
    params = robust.default_spec(projects.template("egg-picker"))["params"]
    outs = ["placed", "placed", "jam", "unstable", "error"]
    rows = [{"i": i, "params": robust.sample(params, 1, i)[0], "outcome": o, "group": o, "success": o == "placed"}
            for i, o in enumerate(outs)]
    doc = {"project": "egg-picker", "rev": "r001", "id": "0" * 12, "n": 5, "done": 5, "seed": 1, "timestep": 0.001,
           "status": "done", "stress": False, "summary": robust.analyze(rows, params), "spec": {"params": params},
           "timing": {"workers": 1, "runs_per_s": 1.2, "first_result_s": 3.0}}
    text = robust.report(doc)
    assert "Succeeds in 2 of 3 runs: 66.7%" in text
    assert "1 run was numerically unstable and is not counted (run 3)." in text
    assert "1 run stopped with an error and is not counted (run 4)." in text
    assert "Succeeds in 2 of 3 runs: 66.7% (likely between 20% and 94%)." in text, text
    assert "\n     1 run  (33.3%, likely between 6% and 80%)" in text and "Timing: 5 runs, 1.2 runs/s on 1 worker," in text
    assert "95% CI" not in text
    one = {**doc, "n": 1, "done": 1, "summary": robust.analyze(rows[:1], params)}
    text = robust.report(one)
    assert "1 of 1 randomized run of pick and place" in text and "Succeeds in its one run: at least 2.5%" in text
    assert "Timing: 1 run," in text and " 1 runs" not in text


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
    with pytest.raises(robust.StudyError, match="use half_timestep or impratio_1"):
        robust.replay(project, doc["id"], 0, "softer", cache=CACHE)
    for rev, msg in (("r999", "This project has no finished revision r999."), ("../r001", "There is no such revision.")):
        with pytest.raises(robust.StudyError) as err:
            robust.new_study(project, rev, runs=2)
        assert str(err.value) == msg


def test_a_replay_in_a_study_process_matches_one_in_this_process(project, prepared, tmp_path, monkeypatch):
    """The Studio replays a run in a spawned study process (its CPU and MuJoCo's warning log stay out of the server):
    the same frames as a replay here, and the packed robot it rebuilt is kept for the next one."""
    doc = robust.run_study(project, runs=2, workers=1, timestep=0.002, checks=False, seed=13, nice=0, cache=CACHE)
    here = robust.replay(project, doc["id"], 1, cache=CACHE)
    monkeypatch.chdir(tmp_path)
    cache = {}
    t = time.time()
    there = robust.replay(project, doc["id"], 1, cache=cache, process=True, nice=0)
    assert there["frames"] == here["frames"] and there["metrics"] == here["metrics"]
    assert {k: v for k, v in there["robustness"].items() if k not in TIMING} == \
           {k: v for k, v in here["robustness"].items() if k not in TIMING}
    assert list(cache) == [robust.cache_key(project, "r001", 0.002)] and time.time() - t < 120
    again = robust.replay(project, doc["id"], 0, "impratio_1", cache=cache, process=True, nice=0)
    assert again["robustness"]["variant"] == "impratio_1" and again["frames"]
    assert not os.listdir(tmp_path)                                   # nothing written where the server runs
    with pytest.raises(robust.StudyError, match="took too long"):
        robust.replay(project, doc["id"], 1, cache={}, process=True, nice=0, timeout=0.05)


@pytest.mark.parametrize("change", [{"electronics.mcu": "rp2040_pico"}, {"electronics.config": "custom_pcb"}])
def test_an_electronics_only_revision_matches_the_simulation_it_reuses(tmp_path_factory, tmp_path, monkeypatch, change):
    # a controller or custom-PCB edit reuses the simulation (the boards sit in the base, welded to the table): the
    # rebuilt model differs only in the base's mass and shapes, so a study of it isn't "simulated with other Morph code"
    import shutil

    from morph.robot import catalog

    root = egg_project(tmp_path_factory, tmp_path / "home", name="pcb-check")
    r2 = os.path.join(root, "r002")
    shutil.copytree(os.path.join(root, "r001"), r2)
    with open(os.path.join(r2, "design.json"), "w", encoding="utf-8") as fh:
        fh.write(projects.template("egg-picker").with_changes(change).dumps())
    with open(os.path.join(r2, "results.json"), encoding="utf-8") as fh:
        res = json.load(fh)
    with open(os.path.join(r2, "results.json"), "w", encoding="utf-8") as fh:
        json.dump({**res, "rev": "r002", "parent": "r001", "provenance": {"sim": "reused from r001"},
                   "stage_status": {"sim": "reused"}}, fh)
    assert robust.prepare(root, "r002", 0.002)["matches_revision"] is True
    # a change to what moves (here every servo 10 % weaker) is still told apart
    torque = catalog.torque_at
    monkeypatch.setattr(catalog, "torque_at", lambda key, volts: 0.9 * torque(key, volts))
    assert robust.prepare(root, "r002", 0.002)["matches_revision"] is False


def test_a_study_of_a_revision_that_now_holds_another_design_says_so(project, prepared):
    doc = robust.run_study(project, runs=1, workers=1, timestep=0.002, checks=False, seed=17, nice=0, cache=CACHE)
    assert robust.load(project, doc["id"])["rev_changed"] is False
    path = os.path.join(project, "r001", "design.json")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace('"quantity": 1', '"quantity": 2') if '"quantity": 1' in text else text + "\n")
        loaded = robust.load(project, doc["id"])
        assert loaded["rev_changed"] is True and loaded["rev_missing"] is False
        assert next(s for s in robust.studies(project) if s["id"] == doc["id"])["rev_changed"] is True
    finally:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    assert robust.load(project, doc["id"])["rev_changed"] is False


def test_a_sensorless_hobby_grip_that_holds_its_servo_at_stall_is_a_failure_with_its_name(tmp_path):
    """A hobby PWM gripper without force sensors on a 370 g can: G closes 0.49 mm past the calibration gauge, so a can
    8 % over its nominal size is held 3.13 mm past its own contact, at the servo's stall, for the whole carry. The
    study counted it placed; it now fails it as "stalled", as the simulation does, and the plan says so as an issue."""
    from morph.cli import EGG_ROBOT
    from morph.robot import pipeline2
    from morph.studio.intent import object_changes

    d = Design().with_changes({**EGG_ROBOT, **object_changes("soda can"), "finger.sensor": "none",
                               "arm.actuator_family": "hobby_pwm"})
    info = projects.create(str(tmp_path), "hobby-can", origin="default", demo=False, unique=False)
    root, sz = info["root"], arm.size(d)
    force = pipeline2.gripper_stage(d, sz, simulate=False)["force"]
    ep = electronics2.plan(d, sz, grip=force)
    assert any("closes by position only" in i for i in ep["issues"])
    rev = os.path.join(root, "r001")
    os.makedirs(rev)
    with open(os.path.join(rev, "design.json"), "w", encoding="utf-8") as fh:
        fh.write(d.dumps())
    doc = simulate.run(arm.build(d, sz, ep), d, "pick_place", os.path.join(rev, "sim"), fast=True)
    res = {"rev": "r001", "kind": "robot", "stage_status": {"sim": "computed"}, "provenance": {"sim": "computed"},
           "gripper": {"force": force}, "sims": {"mjcf": "robot.xml", "scenarios": {"pick_place": {
               "file": "sim_pick_place.json", "title": "Pick and place", "metrics": doc["metrics"]}}}}
    with open(os.path.join(rev, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    runner = robust.Runner(robust.prepare(root, "r001", 0.002)["blob"])
    rows = {s: runner.run({"i": 0, "params": {"object_size": s}, "perturb": {"size_scale": s}}) for s in (1.0, 1.08)}
    assert rows[1.0]["outcome"] != "stalled"
    assert rows[1.08]["outcome"] == "stalled" and not rows[1.08]["success"], rows[1.08]
    assert robust.group_label("stalled", "can") == ("Held the gripper servo at stall: the can was wider than its "
                                                    "position-only close allows")


def test_a_sensorless_hobby_grip_that_would_crush_its_object_is_over_force_not_placed(tmp_path):
    """A hobby PWM gripper without force sensors on a light bulb: one 2 % over its size is held 0.76 mm past its contact,
    15.66 N in the servo model the firmware closes by, above the bulb's 15 N damage force, while MuJoCo's soft contact
    measured 10.35 N, so the study counted it placed. It is now over_force, as the simulation's own crush check says."""
    from morph.cli import EGG_ROBOT
    from morph.robot import pipeline2
    from morph.studio.intent import object_changes

    d = Design().with_changes({**EGG_ROBOT, **object_changes("light bulb"), "finger.sensor": "none",
                               "arm.actuator_family": "hobby_pwm"})
    info = projects.create(str(tmp_path), "hobby-bulb", origin="default", demo=False, unique=False)
    root, sz = info["root"], arm.size(d)
    force = pipeline2.gripper_stage(d, sz, simulate=False)["force"]
    ep = electronics2.plan(d, sz, grip=force)
    rev = os.path.join(root, "r001")
    os.makedirs(rev)
    with open(os.path.join(rev, "design.json"), "w", encoding="utf-8") as fh:
        fh.write(d.dumps())
    doc = simulate.run(arm.build(d, sz, ep), d, "pick_place", os.path.join(rev, "sim"), fast=True)
    res = {"rev": "r001", "kind": "robot", "stage_status": {"sim": "computed"}, "provenance": {"sim": "computed"},
           "gripper": {"force": force}, "sims": {"mjcf": "robot.xml", "scenarios": {"pick_place": {
               "file": "sim_pick_place.json", "title": "Pick and place", "metrics": doc["metrics"]}}}}
    with open(os.path.join(rev, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    runner = robust.Runner(robust.prepare(root, "r001", 0.002)["blob"])
    row = runner.run({"i": 0, "params": {"object_size": 1.02}, "perturb": {"size_scale": 1.02}})
    assert row["outcome"] == "over_force" and not row["success"], row


def test_an_object_the_resting_arm_knocks_away_is_knocked_not_a_missed_grasp(tmp_path):
    """The default smart-bus egg-picker on a 520 g, 200 mm water bottle lying on its side: the resting hand's finger is
    1.07 mm from the bottle's end, so one 1.5 % over its size touches it at the start, and the arm's first move
    knocks it off the table (68.95 m of 'slip', with no floor). The study called that 'Missed the grasp'. It is a
    real failure of this layout (the finger would touch a real bottle that size too), so it still counts, as
    'Knocked away by the arm before the grasp'."""
    from morph.robot import pipeline2
    from morph.studio.intent import object_changes

    d = projects.template("egg-picker").with_changes({**object_changes("water bottle"), "finger.sensor": "none"})
    info = projects.create(str(tmp_path), "bottle", origin="default", demo=False, unique=False)
    root, sz = info["root"], arm.size(d)
    force = pipeline2.gripper_stage(d, sz, simulate=False)["force"]
    ep = electronics2.plan(d, sz, grip=force)
    rev = os.path.join(root, "r001")
    os.makedirs(rev)
    with open(os.path.join(rev, "design.json"), "w", encoding="utf-8") as fh:
        fh.write(d.dumps())
    doc = simulate.run(arm.build(d, sz, ep), d, "pick_place", os.path.join(rev, "sim"), fast=True)
    res = {"rev": "r001", "kind": "robot", "stage_status": {"sim": "computed"}, "provenance": {"sim": "computed"},
           "gripper": {"force": force}, "sims": {"mjcf": "robot.xml", "scenarios": {"pick_place": {
               "file": "sim_pick_place.json", "title": "Pick and place", "metrics": doc["metrics"]}}}}
    with open(os.path.join(rev, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    runner = robust.Runner(robust.prepare(root, "r001", 0.002)["blob"])
    rows = {s: runner.run({"i": 0, "params": {"object_size": s}, "perturb": {"size_scale": s}}) for s in (1.0, 1.02)}
    assert rows[1.0]["outcome"] == "placed", rows[1.0]
    assert rows[1.02]["outcome"] == "knocked" and not rows[1.02]["success"], rows[1.02]
    assert rows[1.02]["knocked_mm"] > robust.KNOCK_MM
    assert robust.group_label("knocked", "bottle") == "Knocked away by the arm before the grasp"


# ------------------------------------------------------------------ acceptance (MORPH_SLOW=1)

@pytest.mark.skipif(not SLOW, reason="MORPH_SLOW=1: 200 runs at 1 ms plus the solver checks (minutes)")
def test_acceptance_the_published_study(tmp_path):
    """The egg-picker at default tolerances and 1 ms steps: 150 of 200 succeed (Wilson 68.6-80.5 %); the jam at
    release is concentrated where the egg sits a few mm off its mark along its length (as good a separator as the egg
    turned a few degrees) and the solver checks label it model-sensitive. At the 2.1715 N grip the simulation used before
    the grip came from the gripper stage (2.34 N), the same seed gave 140 of 200, 58 jams with the egg turned."""
    project = make_revision(tmp_path / "home", fast=False)
    events, t0 = [], time.time()
    doc = robust.run_study(project, runs=200, progress=events.append, nice=0)
    s = doc["summary"]
    print("\n" + robust.report(doc))
    print(json.dumps(doc["timing"], indent=1))
    assert doc["model"]["matches_revision"] is True and doc["model"]["nominal"]["success"] is True
    assert (s["n"], s["successes"]) == (200, 150) and [round(v, 3) for v in s["wilson95"]] == [0.686, 0.805]
    jam = s["groups"][0]
    assert jam["id"] == "jam" and jam["count"] == 50 and jam["model_sensitive"] is True and len(s["groups"]) == 1
    r = jam["rule"]
    assert r["param"] == "object_along_mm" and r["on"] == "value" and -6.0 <= r["lo"] <= -3.5 and 0.5 <= r["hi"] <= 2.5
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
