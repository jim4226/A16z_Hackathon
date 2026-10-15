"""Integration tests for the whole-robot pipeline, Studio payload and chat rules."""

import json

from morph.cli import EGG_ROBOT
from morph.robot.pipeline2 import ORDER, RobotProject, dirty_nodes, params_for
from morph.spec import Design
from morph.studio.intent import rules


def test_dependency_graph_is_selective():
    assert dirty_nodes(["task.cycle_time_s"]) == ["sim", "manual"]      # the manual prints the target cycle time
    assert "sim" not in dirty_nodes(["electronics.config"])
    assert {"sizing", "gripper", "sim"}.isdisjoint(dirty_nodes(["electronics.config"]))
    assert dirty_nodes(["arm.actuator_family"])[0] == "sizing"
    # the controller is the electronics' business (and the CAD's, whose base bay holds it), never the gripper stage's;
    # a fabrication process is manufacturing's alone
    assert {"sizing", "gripper", "sim"}.isdisjoint(dirty_nodes(["electronics.mcu"]))
    assert dirty_nodes(["fab.process"]) == ["manufacturing", "bom", "manual"]
    assert {"sizing", "gripper", "sim"}.isdisjoint(dirty_nodes(["electronics.supply_v"]))


def test_sizing_reruns_for_everything_it_reads():
    # arm.size() reads the build config (metal links are heavier), the pick/place positions (reach) and the pinion
    # radius (the hand's rack-and-pinion drive); tests/test_robot_stage_reads.py checks every read against GRAPH
    assert "sizing" in dirty_nodes(["build.config"]) and "sizing" not in dirty_nodes(["task.cycle_time_s"])
    assert "sizing" in dirty_nodes(["task.pick_x_mm"]) and "sizing" in dirty_nodes(["electronics.pinion_radius_mm"])
    assert "gripper" in dirty_nodes(["build.config"])      # the build config decides what the fingers are made of


def test_the_gripper_stage_analyses_the_fingers_the_build_makes():
    from morph.robot.pipeline2 import gripper_design, gripper_stage

    d = Design().with_changes({**EGG_ROBOT, "fab.process": "laser_cut", "finger.material": "Acrylic",
                               "electronics.mcu": "rp2040_pico"})
    for cfg, mat, printed in (("prototype", "PETG", True), ("pilot", "Al6061", False), ("production", "PC", False)):
        gd = gripper_design(d.with_changes({"build.config": cfg}))[0]
        g = gripper_stage(d.with_changes({"build.config": cfg}), simulate=False)
        assert (gd.finger.material, g["analysis"]["material"], g["analysis"]["printed"]) == (mat, mat, printed), cfg
    # the standalone gripper's finger.material, fab.process and controller don't reach it: the robot builds neither
    base = gripper_stage(Design().with_changes(EGG_ROBOT), simulate=False)
    g = gripper_stage(d, simulate=False)
    assert g["analysis"] == base["analysis"] and g["force"] == base["force"]
    assert not {"mcu", "wiring", "parts", "sensing"} & set(g["force"])   # the electronics plan's, for a robot


def test_moving_accessories_resize_and_resimulate():
    # a wrist camera or an IMU rides on the gripper: every joint carries it and the simulation moves it. The gripper
    # stage reads sizing's servo rail, so it is downstream of sizing; the build reuses it while the rail stays put
    dirty = dirty_nodes(["electronics.extras"])
    assert {"sizing", "gripper", "sim"} <= set(dirty)


def test_an_extras_edit_reruns_sizing_and_sim(tmp_path):
    from morph.robot import arm
    from morph.robot.pipeline2 import gripper_stage

    p = RobotProject(str(tmp_path / "egg"))
    d = Design().with_changes(EGG_ROBOT)
    r1 = p.run(d, n_trials=1, verbose=False, fast_sim=True)
    r2 = p.run(d.with_changes({"electronics.extras": "wrist_camera,imu"}), parent=r1, n_trials=1, verbose=False, fast_sim=True)
    a, b = p.load_results(r1), p.load_results(r2)
    assert b["stage_status"]["sizing"] == b["stage_status"]["sim"] == "computed"
    # sizing reran but left the servo rail the gripper stage reads (pipeline2.PARTIAL): its grasp simulation is reused
    assert b["stage_status"]["gripper"] == "reused" and not b.get("errors"), b.get("errors")
    assert b["provenance"]["gripper"] == f"reused from {r1}"
    assert b["sizing"]["hand_mass_g"] >= a["sizing"]["hand_mass_g"] + 15
    # a lower servo rail does change it: the gripper reruns and plans its stall force on the new rail, as a fresh build
    d74 = d.with_changes({"electronics.servo_v": 7.4})
    c = p.load_results(p.run(d74, parent=r1, n_trials=1, verbose=False, fast_sim=True))
    assert c["stage_status"]["gripper"] == "computed" and c["sizing"]["servo_rail"]["v"] == 7.4
    fresh = gripper_stage(d74, arm.size(d74), n_trials=1)
    assert c["gripper"]["force"] == json.loads(json.dumps(fresh["force"]))
    assert c["gripper"]["force"]["servo_stall_force_n"] < 0.7 * a["gripper"]["force"]["servo_stall_force_n"]
    # one stall force: the force plan's (to 0.1 N) is the electronics plan's (to 0.01 N), both on the 7.4 V rail
    assert abs(c["eplan"]["grip"]["stall_force_n"] - c["gripper"]["force"]["servo_stall_force_n"]) <= 0.05


def test_a_servo_supply_edit_regrips_an_imported_robot_like_a_fresh_build(tmp_path):
    # chat "use a 5 V supply" on the SO-101 re-sized it but reused its gripper stage, which reads sizing's servo rail:
    # the firmware shipped GRIP_LIMIT_N 20.83 N where a fresh build of the same design says 19.29 N
    import re

    from morph.studio.projects import template

    d = template("so101", "so101")
    d5 = d.with_changes({"electronics.servo_v": 5.0})
    p, q = RobotProject(str(tmp_path / "edited")), RobotProject(str(tmp_path / "fresh"))
    r2 = p.run(d5, parent=p.run(d, n_trials=1, verbose=False, fast_sim=True), n_trials=1, verbose=False, fast_sim=True)
    rf = q.run(d5, n_trials=1, verbose=False, fast_sim=True)
    inc, fresh = p.load_results(r2), q.load_results(rf)
    assert inc["stage_status"]["gripper"] == "computed"
    assert inc["gripper"] == fresh["gripper"] and inc["eplan"]["grip"] == fresh["eplan"]["grip"]

    def limit(proj, rev):
        with open(f"{proj.root}/{rev}/electronics/firmware/morph_arm/morph_arm.ino", encoding="utf-8") as fh:
            return re.search(r"GRIP_LIMIT_N\s*=\s*([\d.]+)", fh.read()).group(1)

    assert limit(p, r2) == limit(q, rf)


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
    # what sizing changed from the request travels with the summary (the inspector and New robot show it)
    assert s["notes"] == res["sizing"]["notes"]
    if res["sizing"]["reach_mm"] > d.arm.reach_mm + 1:
        assert any(n.startswith(f"reach raised from {d.arm.reach_mm:.0f} to ") for n in s["notes"]), s["notes"]
    r2 = p.run(d.with_changes({"task.cycle_time_s": 8.0}), parent=r1, n_trials=1, verbose=False, fast_sim=True)
    res2 = p.load_results(r2)
    prov = res2["provenance"]
    assert prov["sim"] == "computed" and prov["cad"].startswith("reused")
    assert not res2.get("errors") and res2["stage_status"]["cad"] == "reused"
    # the manual is rebuilt and gives the new target cycle (it was reused and said 6 s)
    assert prov["manual"] == "computed"
    with open(tmp_path / "egg" / r2 / "manual" / "manual.json", encoding="utf-8") as fh:
        text = " ".join(st.get("instructions", "") for st in json.load(fh)["steps"])
    assert "target cycle 8 s" in text and "target cycle 6 s" not in text


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


def test_every_failed_simulated_scenario_is_an_issue():
    """The summary counted only the pick and place among the simulations: a robot whose shake test dropped the can was
    'feasible' and New robot said every software check passed. Each failed scenario is now an issue, saying why."""
    from morph.robot.pipeline2 import summarize

    ok = {"success": True}
    sims = {"pick_place": {"title": "Pick and place", "metrics": ok},
            "payload_sweep": {"title": "Payload sweep", "metrics": {"success": False, "max_payload_g": 300.0,
                                                                     "rated_payload_g": 370.0}},
            "workspace": {"title": "Reachable workspace", "metrics": {"success": False, "pick_reachable": True,
                                                                      "place_reachable": False}},
            "shake": {"title": "Shake test (in-hand slip)", "metrics": {"success": False, "dropped": True, "slip_mm": 212.9}}}
    s = summarize({"sims": {"scenarios": sims}})
    assert s["feasible"] is False and s["sim_failed"] == ["payload_sweep", "workspace", "shake"]
    assert "shake test failed in simulation (object dropped (212.9 mm slip))" in s["issues"]
    assert "payload test failed in simulation (holds 300 g, less than the object's 370 g)" in s["issues"]
    assert "workspace check failed in simulation (the place point is out of reach)" in s["issues"]
    slipped = summarize({"sims": {"scenarios": {"shake": {"metrics": {"success": False, "dropped": False, "slip_mm": 11.72}}}}})
    assert "shake test failed in simulation (object slipped 11.72 mm in the jaws (5 mm allowed))" in slipped["issues"]
    crushed = summarize({"sims": {"scenarios": {"pick_place": {"metrics": {"success": False, "intact": False,
                                                                           "grip_n_max": 4.1, "damage_force_n": 3.0}}}}})
    assert "pick-and-place failed in simulation (squeezed 4.1 N per finger, above the 3 N that damages it)" in crushed["issues"]
    passed = summarize({"sims": {"scenarios": {k: {"metrics": ok} for k in sims}}})
    assert passed["sim_failed"] == [] and not [i for i in passed["issues"] if "simulation" in i]


def test_a_stretched_out_payload_is_never_negative():
    # the SO-101 on Dynamixel servos at 7.4 V can't hold even its own arm stretched out: the static capacity there is
    # -13 g, which the inspector showed as "Payload stretched out at 465 mm -13 g" and the CLI as stretch_payload_g=-13.0
    from morph.robot.pipeline2 import summarize

    sweep = {"metrics": {"stretch_payload_g": -13.0, "stretch_reach_mm": 465.4, "stretch_limited_by": "shoulder_lift",
                         "max_payload_g": 265.0, "test_reach_mm": 251.8}}
    s = summarize({"sims": {"scenarios": {"payload_sweep": sweep}}})
    assert (s["stretch_payload_g"], s["stretch_holds_itself"], s["stretch_limited_by"]) == (0.0, False, "shoulder_lift")
    sweep["metrics"]["stretch_payload_g"] = 216.0
    s = summarize({"sims": {"scenarios": {"payload_sweep": sweep}}})
    assert (s["stretch_payload_g"], s["stretch_holds_itself"]) == (216.0, True)
    assert summarize({})["stretch_holds_itself"] is None
