"""A failed, incomplete or preview-only stage is never laundered into a feasible robot by a later edit, and the
SO-101's revolute jaw gets firmware in radians, not rack-and-pinion millimetres. Offline."""

import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from morph.cli import EGG_ROBOT
from morph.robot import arm, arm_stub, electronics2, imported, pipeline2, urdf_import
from morph.spec import Design


def _fake_output(node, results):
    results[pipeline2.OUTPUT_KEYS[node]] = {"ok": True}


@pytest.mark.parametrize("failure", ["exception", "pending", "missing"])
def test_failed_stage_stays_invalid_across_reused_revisions(tmp_path, monkeypatch, failure):
    def compute(self, node, design, results, *args):
        if node == "electronics":
            if failure == "exception":
                raise RuntimeError("no controller fits")
            if failure == "pending":
                return False
            return None  # "succeeded" without its mandatory output
        _fake_output(node, results)

    monkeypatch.setattr(pipeline2.RobotProject, "_compute", compute)
    project = pipeline2.RobotProject(str(tmp_path / "robot"))
    design = Design().with_changes(EGG_ROBOT)
    revision = project.run(design, verbose=False)
    for i in range(3):
        results = project.load_results(revision)
        assert results["summary"]["feasible"] is False
        assert pipeline2.stage_problem(results, "electronics")
        assert results["stage_status"]["manufacturing"] in ("blocked", "error")
        assert results.get("manufacturing") is None
        assert "not computed" in " ".join(results["summary"]["issues"])
        design = design.with_changes({"task.cycle_time_s": 8.0 + i})  # an unrelated edit: only sim reruns
        revision = project.run(design, parent=revision, verbose=False)
        assert project.load_results(revision)["provenance"]["electronics"] == "error"


def test_a_failed_stage_recovers_when_an_edit_reruns_it(tmp_path, monkeypatch):
    calls = []
    original = pipeline2.RobotProject._compute

    def compute(self, node, design, results, *args):
        calls.append(node)
        if node == "electronics" and calls.count("electronics") == 1:
            raise RuntimeError("transient")
        return original(self, node, design, results, *args)

    monkeypatch.setattr(pipeline2.RobotProject, "_compute", compute)
    project = pipeline2.RobotProject(str(tmp_path / "robot"))
    design = Design().with_changes(EGG_ROBOT)
    r1 = project.run(design, n_trials=1, verbose=False, fast_sim=True)
    res = project.load_results(r1)
    assert res["stage_status"]["electronics"] == "error" and res["stage_status"]["cad"] == "blocked"
    assert "not built: needs electronics" in res["errors"]["cad"]
    r2 = project.run(design.with_changes({"electronics.config": "custom_pcb"}), parent=r1, n_trials=1, verbose=False,
                     fast_sim=True)
    res = project.load_results(r2)
    assert res["stage_status"]["electronics"] == res["stage_status"]["cad"] == "computed"
    assert res["provenance"]["sizing"] == f"reused from {r1}" and res["stage_status"]["sizing"] == "reused"
    assert not res.get("errors"), res.get("errors")


def test_legacy_reused_revision_without_required_output_is_invalid():
    results = {"provenance": {n: "reused from r001" for n in pipeline2.ORDER}}
    for node in pipeline2.ORDER:
        _fake_output(node, results)
    results["electronics"] = None
    summary = pipeline2.summarize(results)
    assert not summary["feasible"] and "wiring" in " ".join(summary["issues"])


def test_missing_cached_artifact_cannot_be_reused_as_success(tmp_path):
    project = pipeline2.RobotProject(str(tmp_path / "robot"))
    design = Design().with_changes(EGG_ROBOT)
    saved = Path(project.rev_dir("r001"))
    saved.mkdir()
    (saved / "design.json").write_text(design.dumps(), encoding="utf-8")
    results = {"provenance": {n: "computed" for n in pipeline2.ORDER}}
    for node in pipeline2.ORDER:
        _fake_output(node, results)
    results["cad"] = {"parts": 12, "urdf": "cad/urdf/robot.urdf"}
    (saved / "results.json").write_text(json.dumps(results), encoding="utf-8")
    child = project.load_results(project.run(design, parent="r001", verbose=False))
    assert child["provenance"]["cad"] == "error"
    assert "missing required artifact for cad" in child["errors"]["cad"] and "carried over from r001" in child["errors"]["cad"]
    assert not child["summary"]["feasible"]


def test_fallback_cad_is_a_preview_that_is_never_manufactured(tmp_path, monkeypatch):
    original = pipeline2.RobotProject._compute
    visited = []

    def compute(self, node, design, results, *args):
        visited.append(node)
        if node == "cad":
            return original(self, node, design, results, *args)
        _fake_output(node, results)

    def fail(*args):
        raise ValueError("detailed CAD failed")

    def write(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("preview", encoding="utf-8")
        return str(path)

    preview = SimpleNamespace(parts=[], links={}, joints=[], meta={}, validate=lambda: [], export_glb=write,
                              to_robot_json=write, export_urdf=lambda folder: write(Path(folder) / "robot.urdf"))
    monkeypatch.setattr(pipeline2.RobotProject, "_compute", compute)
    monkeypatch.setattr(arm, "build", fail)
    monkeypatch.setattr(arm_stub, "build", lambda *args: preview)
    project = pipeline2.RobotProject(str(tmp_path / "robot"))
    revision = project.run(Design().with_changes(EGG_ROBOT), verbose=False)
    results = project.load_results(revision)
    assert results["cad"]["preview_only"] and results["stage_status"]["cad"] == "preview"
    assert (tmp_path / "robot" / revision / "cad" / "robot.glb").exists()  # still there to look at
    assert "manufacturing" not in visited and "bom" not in visited and "manual" not in visited
    assert "needs the detailed robot CAD" in results["errors"]["manufacturing"]
    assert results["summary"]["feasible"] is False
    assert any(i.startswith("preview only") for i in results["summary"]["issues"])
    child = project.load_results(project.run(Design().with_changes({**EGG_ROBOT, "task.cycle_time_s": 9.0}),
                                             parent=revision, verbose=False))
    assert child["stage_status"]["cad"] == "preview" and child["summary"]["feasible"] is False


def _firmware(tmp_path, revolute):
    design = Design().with_changes({**EGG_ROBOT, "arm.actuator_family": "smart_bus"})
    if revolute:
        design = design.with_changes({"arm.source": "so101"})
        profile, folder = imported.load_profile("so101")
        assembly = urdf_import.load_urdf(str(Path(folder) / profile["urdf"]), profile)
        sizing = {"joints": [{"name": name, "actuator": "sts3215_7v4"} for name in profile["arm_joints"]],
                  "gripper_actuator": "sts3215_7v4"}
    else:
        sizing = arm.size(design)
        assembly = arm.build(design, sizing)
    plan = electronics2.plan(design, sizing)
    rel = electronics2.firmware(design, plan, assembly, str(tmp_path), "r001")
    return design, plan, assembly, (tmp_path / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("revolute", [True, False])
def test_firmware_uses_the_gripper_joints_own_units(tmp_path, revolute):
    design, plan, assembly, code = _firmware(tmp_path, revolute)
    motion = electronics2.grip_motion(design, plan, assembly)
    unit = "rad" if revolute else "mm"
    assert motion["unit"] == unit
    assert f"grip_{unit},force_n" in code and f"Gripper unit: {unit}" in code and f"W {unit}" in code
    assert "GRIP_PITCH_R_MM" not in code and "GRIP_PER_UNIT" in code and "busRead(N_SERVOS, &p)" in code
    assert "UNVERIFIED" in code
    if revolute:
        assert "revolute-jaw gripper" in code and "pinion" not in code.split("// gripper:")[1].split("\n")[0]
        assert motion["per_unit"] == pytest.approx(4096 / (2 * math.pi))
        assert 0 < motion["close_speed"] < motion["open_speed"] <= 0.5
        # the SO-101 URDF's -10..100 degree jaw maps onto the STS3215 encoder around its centre
        assert motion["zero"] + motion["lo"] * motion["per_unit"] == pytest.approx(1934.22, abs=0.01)
        assert motion["zero"] + motion["hi"] * motion["per_unit"] == pytest.approx(3185.78, abs=0.01)
    else:
        assert motion["per_unit"] == pytest.approx(4096 / (2 * math.pi) / design.electronics.pinion_radius_mm)
        assert (motion["close_speed"], motion["open_speed"], motion["backoff"]) == (8.0, 25.0, 1.0)  # as before
        assert "force-sensing parallel gripper" in code and "pinion" in code.split("// gripper:")[1].split("\n")[0]


@pytest.mark.skipif(shutil.which("g++") is None, reason="g++ not installed")
@pytest.mark.parametrize("revolute", [True, False])
def test_generated_servo_mapping_clamps_to_the_joint_limits(tmp_path, revolute):
    _, _, _, code = _firmware(tmp_path, revolute)
    # run the generated C++ itself, not a Python copy of its formula
    constants = "\n".join(re.findall(r"^(?:const )?float GRIP_\w+\s*=.*?;", code, re.M))
    function = re.search(r"float gripServoPosition\(float g\) \{.*?\n\}", code, re.S).group()
    cpp = tmp_path / "encoder.cpp"
    cpp.write_text('#include <cstdio>\n#include "Arduino.h"\n' + constants + "\n" + function +
                   '\nint main() { printf("%.6f %.6f %.6f %.6f", gripServoPosition(GRIP_MIN), '
                   'gripServoPosition(GRIP_MAX), gripServoPosition(-10000), gripServoPosition(10000)); }', encoding="utf-8")
    exe = tmp_path / "encoder.exe"
    result = subprocess.run(["g++", "-std=gnu++17", "-I" + str(Path(__file__).parent / "fw_stubs"), str(cpp), "-o", str(exe)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lo, hi, below, above = map(float, subprocess.check_output([str(exe)], text=True).split())
    assert below == lo and above == hi and 0 <= lo < hi <= 4095
    if revolute:
        assert lo == pytest.approx(1934.22, abs=0.01) and hi == pytest.approx(3185.78, abs=0.01)


def test_wiring_results_say_the_firmware_is_unverified(tmp_path):
    p = pipeline2.RobotProject(str(tmp_path / "so101"))
    rev = p.run(Design().with_changes({**EGG_ROBOT, "name": "so101", "arm.source": "so101"}), n_trials=1, verbose=False,
                fast_sim=True)
    res = p.load_results(rev)
    assert res["electronics"]["firmware_validation"] == {"status": "unverified", "board_core": "not_verified",
                                                         "bench": "not_verified"}
    assert res["electronics"]["grip"]["motion"]["unit"] == "rad"
    assert set(res["stage_status"].values()) == {"computed"} and not res.get("errors"), res.get("errors")
    assert res["summary"]["feasible"] is True


def test_a_fresh_process_can_edit_the_so101_with_its_sizing_reused(tmp_path):
    """Its servos come from catalog_extra, which only imported.py used to register: a new process that reused the
    sizing (no import of imported.py) failed the electronics stage with KeyError 'sts3215_7v4'."""
    import os
    import sys
    root = tmp_path / "so101"
    p = pipeline2.RobotProject(str(root))
    design = Design().with_changes({**EGG_ROBOT, "name": "so101", "arm.source": "so101"})
    r1 = p.run(design, n_trials=1, verbose=False, fast_sim=True)
    code = ("import sys; from morph.robot.pipeline2 import RobotProject; from morph.spec import Design; "
            f"p = RobotProject({str(root)!r}); d = p.load_design({r1!r}).with_changes({{'electronics.config': 'custom_pcb'}}); "
            f"r = p.load_results(p.run(d, parent={r1!r}, n_trials=1, verbose=False, fast_sim=True)); "
            "print(sorted(set(r['stage_status'].values())), r.get('errors'))")
    env = {**os.environ, "PYTHONPATH": str(Path(pipeline2.__file__).resolve().parents[2])}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=600)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "['computed', 'reused'] None", out.stdout[-2000:]
