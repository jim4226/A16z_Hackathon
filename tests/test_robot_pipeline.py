"""Integration tests for the whole-robot pipeline, Studio payload and chat rules."""

import json

from morph.cli import EGG_ROBOT
from morph.robot.pipeline2 import ORDER, RobotProject, dirty_nodes, params_for
from morph.spec import Design
from morph.studio.intent import rules


def test_dependency_graph_is_selective():
    assert dirty_nodes(["task.pick_x_mm"]) == ["sim"]
    assert "sim" not in dirty_nodes(["electronics.config"])
    assert "sizing" not in dirty_nodes(["build.config"])
    assert dirty_nodes(["arm.actuator_family"])[0] == "sizing"


def test_pipeline_runs_and_reuses(tmp_path):
    p = RobotProject(str(tmp_path / "egg"))
    d = Design().with_changes(EGG_ROBOT)
    r1 = p.run(d, n_trials=1, verbose=False, fast_sim=True)
    res = p.load_results(r1)
    assert set(res["provenance"]) == set(ORDER)
    assert res["cad"]["validate"] == [] and res["cad"]["parts"] > 10
    assert (tmp_path / "egg" / r1 / "cad" / "robot.glb").exists()
    r2 = p.run(d.with_changes({"task.pick_x_mm": 200.0}), parent=r1, n_trials=1, verbose=False, fast_sim=True)
    prov = p.load_results(r2)["provenance"]
    assert prov["sim"] == "computed" and prov["cad"].startswith("reused")


def test_server_payload_shape(tmp_path):
    from morph.studio.server import rev_payload

    p = RobotProject(str(tmp_path / "egg"))
    r1 = p.run(Design().with_changes(EGG_ROBOT), n_trials=1, verbose=False, fast_sim=True)
    rev = rev_payload(p, r1)
    for key in ("rev", "nodes", "design", "params", "summary", "robot", "sims", "electronics", "manufacturing", "bom", "manual"):
        assert key in rev
    json.dumps(rev)
    assert rev["robot"]["glb"].endswith("robot.glb")
    assert {x["key"] for x in params_for(Design())} >= {"arm.dof", "electronics.config", "build.config"}


def test_robot_chat_rules():
    d = Design()
    assert rules("Swap to Dynamixel servos", d)[0] == {"arm.actuator_family": "dynamixel"}
    assert rules("Use a custom PCB", d)[0] == {"electronics.config": "custom_pcb"}
    assert rules("Make it 6-DOF", d)[0] == {"arm.dof": 6}
    assert rules("Add a wrist camera", d)[0] == {"electronics.extras": "wrist_camera"}
    assert rules("Pilot build: CNC + sheet metal", d)[0] == {"build.config": "pilot"}
    assert rules("Increase reach to 380 mm", d)[0] == {"arm.reach_mm": 380.0}
