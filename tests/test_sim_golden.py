"""Golden hashes of the nominal whole-robot simulation (morph/robot/simulate.py): everything run_all writes (robot.xml
and every sim_<scenario>.json, frame traces included, with only the wall-clock time left out), its return value, and
the full-precision MuJoCo state at the end of each scenario, for the egg-picker (full and fast), the SO-101 and the
egg-picker in a room with a bin on its carry path. Robustness studies hook into the simulation (perturb=...); with
perturb=None the output must stay bit-identical to these hashes, recorded before that hook existed.

Floating point differs between CPUs and library builds, so hashes are kept per environment (system, CPU, MuJoCo and
NumPy versions) and a test is skipped where none were recorded. Record them only on a commit whose simulation is
known to be right: MORPH_GOLDEN=record python -m pytest tests/test_sim_golden.py (writes tests/sim_golden_data.py).
A change to the robot generator (arm.py, imported.py, electronics2.py) changes the model and needs a new recording
too; a change to simulate.py that is meant to leave nominal results alone must not."""

import ast
import hashlib
import json
import os
import platform
import sys

import numpy as np
import pytest

mujoco = pytest.importorskip("mujoco")

from morph.robot import arm, electronics2, imported, simulate  # noqa: E402
from morph.studio.projects import template  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_golden_data.py")
EGG_GRIP_TARGET_N = 2.34   # what the pipeline's gripper stage computes for the egg-picker (5 s of grasp simulations)


def environment() -> str:
    cpu = platform.processor() or ""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/cpuinfo", encoding="utf-8") as fh:
                cpu = next((ln.split(":", 1)[1].strip() for ln in fh if ln.startswith("model name")), cpu)
        except OSError:
            pass
    return f"{sys.platform}-{platform.machine()} | {cpu} | mujoco {mujoco.__version__} | numpy {np.__version__}"


def golden() -> dict:
    try:
        with open(DATA, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {}
    return ast.literal_eval(text.split("GOLDEN = ", 1)[1])


def egg():
    d = template("egg-picker")
    sz = arm.size(d)
    return d, arm.build(d, sz, electronics2.plan(d, {**sz, "grip_target_n": EGG_GRIP_TARGET_N}))


def so101():
    d = template("so101", "so101")
    sz = imported.size(d)
    target = imported.gripper_summary(d, sz)["force"]["target_force_n"]
    return d, imported.build(d, sz, electronics2.plan(d, {**sz, "grip_target_n": target}))


def bin_world(design, h=90.0) -> dict:
    """The plain table as a world surface and a 60 x 60 x 90 mm bin just outside the middle of the carry arc: the arc,
    the approach and the return to rest all go around it (tests/test_world_sim.py's research case)."""
    pick = np.array([design.task.pick_x_mm, design.task.pick_y_mm])
    place = np.array([design.task.place_x_mm, design.task.place_y_mm])
    a0, a1 = simulate._arc_angles(np.r_[pick, 0], np.r_[place, 0])
    am = (a0 + a1) / 2
    rm = 0.5 * (np.linalg.norm(pick) + np.linalg.norm(place)) + 20
    box = {"id": "bin", "label": "Bin", "center": [rm * np.cos(am), rm * np.sin(am), h / 2], "half": [30.0, 30.0, h / 2],
           "yaw_deg": 0.0}
    return {"id": "golden-bin", "sha": "golden", "surface": {"label": "Table", "center": [100.0, 0.0, -12.0],
                                                             "half": [550.0, 600.0, 12.0], "yaw_deg": 0.0},
            "floor_z": -750.0, "obstacles": [box], "zones": []}


FIXTURES = {
    "egg-picker": lambda: (*egg(), False, {}),
    "egg-picker-fast": lambda: (*egg(), True, {}),
    "so101": lambda: (*so101(), False, {}),
    "egg-picker-bin-world": lambda: (lambda d, asm: (d, asm, False, {"world": bin_world(d)}))(*egg()),
}


def _sha(data) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def outputs(name, tmp_path, monkeypatch) -> dict:
    """{what: sha256} for one fixture: robot.xml, each sim_<scenario>.json as written (info.wall_s removed, the only
    value that changes from run to run), run_all's return value, and the MuJoCo state after each scenario."""
    design, asm, fast, opts = FIXTURES[name]()
    states = {}
    run_one = simulate._run_one

    def recorded(rob, design_, scenario, fast_, *a, **k):
        doc = run_one(rob, design_, scenario, fast_, *a, **k)
        d = rob.data
        states[scenario] = _sha(b"".join(np.ascontiguousarray(v, float).tobytes()
                                         for v in (d.qpos, d.qvel, d.act, d.ctrl, d.qacc, [d.time])))
        return doc
    monkeypatch.setattr(simulate, "_run_one", recorded)
    out = tmp_path / name
    ret = simulate.run_all(asm, design, str(out), fast=fast, **opts)
    hashes = {"robot.xml": _sha((out / "robot.xml").read_bytes()),
              "run_all": _sha(json.dumps(ret, separators=(",", ":")))}
    for scenario in simulate.SCENARIOS:
        with open(out / f"sim_{scenario}.json", encoding="utf-8") as fh:
            raw = fh.read()
        doc = json.loads(raw)
        assert isinstance(doc["info"].pop("wall_s"), float)
        hashes[f"sim_{scenario}.json"] = _sha(json.dumps(doc, separators=(",", ":")))
        hashes[f"state after {scenario}"] = states[scenario]
    return hashes


@pytest.mark.parametrize("name", list(FIXTURES))
def test_nominal_simulation_is_bit_identical(name, tmp_path, monkeypatch):
    env = environment()
    if os.environ.get("MORPH_GOLDEN") == "record":
        got = outputs(name, tmp_path, monkeypatch)
        data = golden()
        data.setdefault(env, {})[name] = got
        with open(DATA, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# Written by MORPH_GOLDEN=record python -m pytest tests/test_sim_golden.py (see that file).\n")
            fh.write("GOLDEN = " + json.dumps(data, indent=1, sort_keys=True) + "\n")
        pytest.skip(f"recorded {name} for {env}")
    want = golden().get(env, {}).get(name)
    if not want:
        pytest.skip(f"no golden hashes recorded for {env}")
    got = outputs(name, tmp_path, monkeypatch)
    changed = sorted(k for k in want if got.get(k) != want[k])
    assert not changed, f"{name}: the nominal simulation changed: {changed}"
    assert set(got) == set(want)
