"""Integration tests for the whole-robot pipeline, Studio payload and chat rules."""

import json

from morph.cli import EGG_ROBOT
from morph.robot.pipeline2 import ORDER, RobotProject, dirty_nodes, params_for
from morph.spec import Design
from morph.studio.intent import rules


def test_dependency_graph_is_selective():
    assert dirty_nodes(["task.cycle_time_s"]) == ["sim"]
    assert "sim" not in dirty_nodes(["electronics.config"])
    assert {"sizing", "gripper", "sim"}.isdisjoint(dirty_nodes(["electronics.config"]))
    assert dirty_nodes(["arm.actuator_family"])[0] == "sizing"


def test_sizing_reruns_for_everything_it_reads():
    # arm.size() reads the build config (metal links are heavier) and the pick/place positions (reach)
    assert "sizing" in dirty_nodes(["build.config"]) and "gripper" not in dirty_nodes(["build.config"])
    assert "sizing" in dirty_nodes(["task.pick_x_mm"]) and "sizing" not in dirty_nodes(["task.cycle_time_s"])


def test_moving_accessories_resize_and_resimulate():
    # a wrist camera or an IMU rides on the gripper: every joint carries it and the simulation moves it
    dirty = dirty_nodes(["electronics.extras"])
    assert {"sizing", "sim"} <= set(dirty) and "gripper" not in dirty


def test_an_extras_edit_reruns_sizing_and_sim(tmp_path):
    p = RobotProject(str(tmp_path / "egg"))
    d = Design().with_changes(EGG_ROBOT)
    r1 = p.run(d, n_trials=1, verbose=False, fast_sim=True)
    r2 = p.run(d.with_changes({"electronics.extras": "wrist_camera,imu"}), parent=r1, n_trials=1, verbose=False, fast_sim=True)
    a, b = p.load_results(r1), p.load_results(r2)
    assert b["stage_status"]["sizing"] == b["stage_status"]["sim"] == "computed"
    assert b["stage_status"]["gripper"] == "reused" and not b.get("errors"), b.get("errors")
    assert b["sizing"]["hand_mass_g"] >= a["sizing"]["hand_mass_g"] + 15


def test_imported_robots_size_the_camera_they_carry():
    from morph.robot import imported

    d = Design().with_changes({**EGG_ROBOT, "name": "so101", "arm.source": "so101"})
    req = lambda s: {j["name"]: j["torque_req_nm"] for j in s["joints"]}  # noqa: E731
    plain, cam = req(imported.size(d)), req(imported.size(d.with_changes({"electronics.extras": "wrist_camera"})))
    assert all(cam[n] > plain[n] for n in ("shoulder_lift", "elbow_flex", "wrist_flex"))


def test_the_present_custom_pcb_step_builds_a_feasible_robot(tmp_path):
    # Present step 6: the egg-picker on Dynamixel servos with one custom controller PCB; its printed base used to
    # exceed the 250 mm bed, so the step showed an issue
    p = RobotProject(str(tmp_path / "egg"))
    d = Design().with_changes({**EGG_ROBOT, "arm.actuator_family": "dynamixel", "electronics.config": "custom_pcb"})
    res = p.load_results(p.run(d, n_trials=1, verbose=False, fast_sim=True))
    assert res["summary"]["feasible"] and res["summary"]["issues"] == [], res["summary"]["issues"]
    assert res["electronics"]["board"]["drc"]["pass"]


def test_pipeline_runs_and_reuses(tmp_path):
    p = RobotProject(str(tmp_path / "egg"))
    d = Design().with_changes(EGG_ROBOT)
    r1 = p.run(d, n_trials=1, verbose=False, fast_sim=True)
    res = p.load_results(r1)
    assert set(res["provenance"]) == set(ORDER)
    assert not res.get("errors"), res.get("errors")
    assert set(res["stage_status"].values()) == {"computed"}
    assert res["cad"]["validate"] == [] and res["cad"]["parts"] > 10
    assert (tmp_path / "egg" / r1 / "cad" / "robot.glb").exists()
    s, sc = res["summary"], res["sims"]["scenarios"]
    pay, ws = sc["payload_sweep"]["metrics"], sc["workspace"]["metrics"]
    assert s["payload_test_reach_mm"] == pay["test_reach_mm"] and s["stretch_payload_g"] == pay["stretch_payload_g"]
    assert s["stretch_reach_mm"] == pay["stretch_reach_mm"] and s["top_down_reach_mm"] == ws["top_down_reach_mm"][1]
    r2 = p.run(d.with_changes({"task.cycle_time_s": 8.0}), parent=r1, n_trials=1, verbose=False, fast_sim=True)
    res2 = p.load_results(r2)
    prov = res2["provenance"]
    assert prov["sim"] == "computed" and prov["cad"].startswith("reused")
    assert not res2.get("errors") and res2["stage_status"]["cad"] == "reused"


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


def test_payload_urls_point_at_revision_files(tmp_path):
    from morph.studio.server import rev_payload

    p = RobotProject(str(tmp_path / "egg"))
    r1 = p.run(Design().with_changes(EGG_ROBOT), n_trials=1, verbose=False, fast_sim=True)
    rev = rev_payload(p, r1)
    assert rev["robot"]["json"] == f"/files/{r1}/cad/robot.json"
    assert "time_study" in rev and "error" not in rev["time_study"]
