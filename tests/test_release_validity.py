"""Public-beta regression checks: failed revisions, native firmware units, and opt-in analytics."""

import json
import math
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from morph import keewano, settings
from morph.cli import EGG_ROBOT
from morph.robot import arm, arm_stub, electronics2, imported, pipeline2, urdf_import
from morph.spec import Design


@pytest.fixture(autouse=True)
def private_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MORPH_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("MORPH_TELEMETRY_ENABLED", raising=False)
    monkeypatch.delenv("MORPH_OFFLINE", raising=False)


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
            return  # accidental success with no mandatory output
        _fake_output(node, results)

    monkeypatch.setattr(pipeline2.RobotProject, "_compute", compute)
    project = pipeline2.RobotProject(str(tmp_path / "robot"))
    design = Design().with_changes(EGG_ROBOT)
    revision = project.run(design, verbose=False)
    for i in range(3):
        results = project.load_results(revision)
        assert not results["summary"]["feasible"]
        assert pipeline2.stage_problem(results, "electronics")
        assert pipeline2.stage_problem(results, "manufacturing")
        assert results.get("manufacturing") is None
        design = design.with_changes({"task.cycle_time_s": 8.0 + i})
        revision = project.run(design, parent=revision, verbose=False)


def test_legacy_reused_revision_without_required_output_is_invalid():
    results = {"provenance": {n: "reused from r001" for n in pipeline2.ORDER}}
    for node in pipeline2.ORDER:
        _fake_output(node, results)
    results["electronics"] = None
    assert not pipeline2.summarize(results)["feasible"]
    assert "wiring" in " ".join(pipeline2.summarize(results)["issues"])


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
    revision = project.run(design, parent="r001", verbose=False)
    child = project.load_results(revision)
    assert child["provenance"]["cad"] == "error"
    assert "missing required artifact" in child["errors"]["cad"]
    assert not child["summary"]["feasible"]


def test_stub_preview_cannot_be_manufactured(tmp_path, monkeypatch):
    original_compute = pipeline2.RobotProject._compute
    visited = []

    def compute(self, node, design, results, *args):
        visited.append(node)
        if node == "cad":
            return original_compute(self, node, design, results, *args)
        _fake_output(node, results)

    def fail(*args):
        raise ValueError("detailed CAD failed")

    def write(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("preview", encoding="utf-8")
        return str(path)

    preview = SimpleNamespace(parts=[], links={}, joints=[], meta={}, validate=lambda: [],
                              export_glb=write, to_robot_json=write,
                              export_urdf=lambda folder: write(Path(folder) / "robot.urdf"))
    monkeypatch.setattr(pipeline2.RobotProject, "_compute", compute)
    monkeypatch.setattr(arm, "build", fail)
    monkeypatch.setattr(arm_stub, "build", lambda *args: preview)
    project = pipeline2.RobotProject(str(tmp_path / "robot"))
    revision = project.run(Design().with_changes(EGG_ROBOT), verbose=False)
    results = project.load_results(revision)
    assert results["cad"]["preview_only"]
    assert results["stage_status"]["cad"] == "preview"
    assert (tmp_path / "robot" / revision / "cad" / "robot.glb").exists()
    assert "manufacturing" not in visited
    assert not results["summary"]["feasible"]


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
        assembly = arm_stub.build(design, sizing)
    plan = electronics2.plan(design, sizing)
    rel = electronics2.firmware(design, plan, assembly, str(tmp_path), "r001")
    return design, plan, assembly, (tmp_path / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("revolute", [True, False])
def test_firmware_uses_native_gripper_units(tmp_path, revolute):
    design, plan, assembly, code = _firmware(tmp_path, revolute)
    motion = electronics2.grip_motion(design, plan, assembly)
    unit = "rad" if revolute else "mm"
    assert motion["unit"] == unit
    assert f"grip_{unit},force_n" in code
    assert f"Gripper unit: {unit}" in code
    assert "GRIP_PITCH_R_MM" not in code
    assert "GRIP_PER_UNIT" in code
    assert "busRead(N_SERVOS, &p)" in code
    if revolute:
        assert motion["per_unit"] == pytest.approx(4096 / (2 * math.pi))
        assert 0 < motion["close_speed"] < motion["open_speed"] <= 0.5
        # SO-101 URDF -10 to 100 degrees maps to the calibrated STS encoder.
        assert motion["zero"] + motion["lo"] * motion["per_unit"] == pytest.approx(1934.22, abs=0.01)
        assert motion["zero"] + motion["hi"] * motion["per_unit"] == pytest.approx(3185.78, abs=0.01)
    else:
        assert motion["per_unit"] == pytest.approx(4096 / (2 * math.pi) / design.electronics.pinion_radius_mm)


@pytest.mark.skipif(shutil.which("g++") is None, reason="g++ not installed")
@pytest.mark.parametrize("revolute", [True, False])
def test_generated_encoder_function_clamps_endpoints(tmp_path, revolute):
    _, _, _, code = _firmware(tmp_path, revolute)
    # Execute the generated function and constants, not a Python replica of its formula.
    constants = "\n".join(re.findall(r"^(?:const )?float GRIP_\w+\s*=.*?;", code, re.M))
    function = re.search(r"float gripServoPosition\(float opening\) \{.*?\n\}", code, re.S).group()
    cpp = tmp_path / "encoder.cpp"
    cpp.write_text('#include <cstdio>\n#include "Arduino.h"\n' + constants + "\n" + function +
                   '\nint main() { printf("%.6f %.6f %.6f %.6f", gripServoPosition(GRIP_MIN), '
                   'gripServoPosition(GRIP_MAX), gripServoPosition(-10000), gripServoPosition(10000)); }', encoding="utf-8")
    exe = tmp_path / "encoder.exe"
    result = subprocess.run(["g++", "-std=gnu++17", "-I" + str(Path(__file__).parent / "fw_stubs"), str(cpp), "-o", str(exe)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lo, hi, below, above = map(float, subprocess.check_output([str(exe)], text=True).split())
    assert below == lo and above == hi
    assert 0 <= lo < hi <= 4095
    if revolute:
        assert lo == pytest.approx(1934.22, abs=0.01)
        assert hi == pytest.approx(3185.78, abs=0.01)


def test_credentials_alone_never_send_telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEWANO_API_KEY", "test-key")
    monkeypatch.setattr(keewano, "_post", lambda *args: pytest.fail("unexpected telemetry"))
    assert not keewano.send("project", [("Revision", "r001")], key="explicit-key")
    assert not keewano.emit("project", {"type": "human_time", "minutes": 5})


def test_telemetry_is_opt_in_and_synthetic_ratios_are_excluded(monkeypatch):
    settings.save({"telemetry_enabled": True})
    monkeypatch.setenv("KEEWANO_API_KEY", "test-key")
    monkeypatch.setattr(keewano, "_registered", False)
    calls = []
    monkeypatch.setattr(keewano, "_post", lambda path, body, key: calls.append((path, body)))
    assert keewano.send("synthetic-test", [("HoldForceRatioPct", 150)])
    batch = calls[-1][1]["events"]
    assert not any(e["eventTypeId"] == keewano._ID["HoldForceRatioPct"][0] for e in batch)
    assert any(e["data"] == "synthetic" for e in batch)
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    calls.clear()
    assert not keewano.send("real-test", [("Revision", "r001")])
    assert calls == []


def test_simulated_measurement_has_no_calibration_ratio():
    events = dict(keewano.events_for({"type": "measurement", "actual": {"simulated": True, "min_hold_force_n": 1.2},
                                      "error_vs_prediction": {"min_hold_force_n": 0.4}}))
    assert events["MeasurementSource"] == "synthetic"
    assert "HoldForceRatioPct" not in events


def test_history_calibration_and_offline_query_cannot_reach_network(monkeypatch):
    monkeypatch.setenv("KEEWANO_PAT", "test-token")
    monkeypatch.setenv("KEEWANO_PROJECT_ID", "test-project")
    monkeypatch.setattr(keewano, "urlopen", lambda *args, **kwargs: pytest.fail("unexpected request"))
    assert keewano.hold_ratio("flat", "PLA") is None
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    assert keewano.query("SELECT 1") is None
