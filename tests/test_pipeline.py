import os

from morph import analysis, design as cad, fab, sim
from morph.agent import heuristic
from morph.pipeline import Project, dirty_nodes
from morph.spec import Design


def test_profile_and_mesh_are_valid(tmp_path):
    for pad in ("flat", "v", "contour"):
        d = Design().with_changes({"finger.pad": pad})
        g = cad.build_profile(d)
        assert g.profile.is_valid and len(g.profile.interiors) == 2
        info = cad.export_stl(g, str(tmp_path / f"{pad}.stl"))
        assert info["watertight"]


def test_dirty_propagation():
    assert dirty_nodes(["object.max_force_n"]) == ["electronics", "bom"]
    assert dirty_nodes(["electronics.mcu"]) == ["electronics", "bom"]
    assert dirty_nodes(["electronics.servo"]) == ["mech", "electronics", "fab", "bom"]
    assert dirty_nodes(["finger.length_mm"]) == ["cad", "analysis", "sim", "mech", "electronics", "fab", "bom"]


def test_analysis_flags_weak_finger():
    d = Design().with_changes({"finger.thickness_mm": 4.0})
    a = analysis.analyze(d, cad.build_profile(d))
    assert not a["pass"] and a["bending_sf"] < 2


def test_contour_needs_less_force_than_flat():
    forces = {}
    for pad in ("flat", "contour"):
        d = Design().with_changes({"finger.pad": pad, "finger.thickness_mm": 10.0})
        forces[pad] = sim.min_hold_force(d, cad.build_profile(d), iters=4)
    assert forces["contour"] < forces["flat"]


def test_fab_outputs(tmp_path):
    for mat, proc, f in (("PETG", "auto", "finger.stl"), ("Delrin", "laser_cut", "cut.svg"),
                         ("Al6061", "cnc_router", "job.nc")):
        d = Design().with_changes({"finger.material": mat, "fab.process": proc})
        out = fab.plan(d, cad.build_profile(d), str(tmp_path / mat))
        assert os.path.exists(tmp_path / mat / f) and out["minutes"] > 0
    text = (tmp_path / "Al6061" / "job.nc").read_text()
    assert "G21" in text and "M30" in text


def test_change_reuses_unaffected_nodes(tmp_path):
    p = Project(str(tmp_path / "proj"))
    r1 = p.run(Design(), n_trials=1, verbose=False)
    r2 = p.run(Design().with_changes({"finger.material": "Delrin", "fab.process": "laser_cut"}),
               parent=r1, n_trials=1, verbose=False)
    prov = p.load_results(r2)["provenance"]
    assert prov["cad"].startswith("reused") and prov["sim"].startswith("reused")
    assert prov["fab"] == "computed"
    m = p.measure(r2, {"fab_minutes": 5.0, "grasp_success_rate": 0.9})
    assert "fab_minutes" in m["error_vs_prediction"]


def test_heuristic_parser():
    n = heuristic("pick up a 66 mm soda can lying on its side, 370 g, print it in PETG")
    assert n.diameter_mm == 66 and n.mass_g == 370 and n.object_name == "soda can"


def test_time_study_counts_human_time(tmp_path):
    from morph.timestudy import study

    p = Project(str(tmp_path / "proj"))
    r1 = p.run(Design(), n_trials=1, verbose=False)
    p.log({"type": "tool_time", "cmd": "new", "seconds": 30.0})
    p.log({"type": "human_time", "step": "review", "minutes": 6.0})
    p.log({"type": "human_time", "step": "corrections", "minutes": 20.0})
    t = study(p, r1)
    rows = {r["key"]: r for r in t["rows"]}
    assert rows["review"]["after_min"] == 6.0 and rows["design"]["after_min"] == 0.5
    assert rows["corrections"]["before_min"] > rows["corrections"]["after_min"]
    assert t["after_min"] < t["before_min"]


EGG = {"object.name": "raw egg", "object.diameter_mm": 44.0, "object.length_mm": 57.0, "object.mass_g": 60.0,
       "object.friction": 0.35, "object.max_force_n": 25.0, "finger.sensor": "fsr402", "finger.width_mm": 20.0,
       "finger.length_mm": 52.0, "finger.thickness_mm": 6.0, "finger.pad_depth_mm": 5.5, "electronics.servo": "mg996r"}


def test_sensor_finger_is_two_printable_parts(tmp_path):
    d = Design().with_changes(EGG)
    g = cad.build_profile(d)
    assert set(g.parts) == {"finger", "pad"} and g.profile.geom_type == "Polygon"
    info = cad.export_stl(g, str(tmp_path / "f.stl"))
    assert all(p["watertight"] for p in info["parts"].values())


def test_electronics_window_and_firmware(tmp_path):
    from morph import electronics

    p = Project(str(tmp_path / "egg"))
    rev = p.run(Design().with_changes(EGG), n_trials=1, verbose=False)
    ep = p.load_results(rev)["electronics"]
    lo, hi = ep["window_n"]
    assert lo < ep["target_force_n"] < hi < 25.0
    assert ep["open_loop_crushes"] and ep["servo_stall_force_n"] > 25.0
    assert ep["sensing"]["divider_ohm"] in [r * 10 ** k for r in electronics.E12 for k in range(2, 6)]
    fw = (tmp_path / "egg" / rev / ep["firmware"]).read_text()
    assert f"TARGET_N = {ep['target_force_n']}" in fw and "LIMIT_N" in fw

    # an object softer than the holding force needs to be flagged, not silently accepted
    rev2 = p.run(Design().with_changes({**EGG, "object.max_force_n": 0.5}), parent=rev, n_trials=1, verbose=False)
    assert not p.load_results(rev2)["electronics"]["pass"]


def test_simulated_bench_logs_measurement(tmp_path):
    from morph import bench

    p = Project(str(tmp_path / "egg"))
    rev = p.run(Design().with_changes(EGG), n_trials=1, verbose=False)
    entry = bench.run(p, rev, None, seconds=3.0, simulate=True)
    assert entry["actual"]["simulated"] and entry["actual"]["held_at_end"]
    assert "hold_force_n" in entry["error_vs_prediction"]


def test_gripper_mechanism_meshes_and_drives_firmware(tmp_path):
    from morph import mechanism

    d = Design().with_changes(EGG)
    g = cad.build_profile(d)
    m = mechanism.build(d, g, "mg996r", str(tmp_path))
    assert not m["issues"] and m["travel_mm"] >= 8
    assert all(p["watertight"] for p in m["printed"].values())
    # racks sit on the pinion's pitch circle top and bottom; fingers bolt to the rack ears
    assert m["servo_open_deg"] < 90 < m["servo_closed_deg"] <= 180
    p = Project(str(tmp_path / "egg"))
    rev = p.run(d, n_trials=1, verbose=False)
    res = p.load_results(rev)
    fw = (tmp_path / "egg" / rev / res["electronics"]["firmware"]).read_text()
    assert f"OPEN_DEG = {int(round(res['mech']['servo_open_deg']))};" in fw
    assert "gripper_plate.stl" in res["fab"]["files"]


def test_board_routes_for_every_mcu(tmp_path):
    from morph import board

    for mcu in ("arduino_nano", "rp2040_pico", "esp32_devkit"):
        out = board.build({"mcu": mcu, "sensing": {"divider_ohm": 4700}}, str(tmp_path / mcu))
        assert out["drc"]["pass"], out["drc"]
        assert (tmp_path / mcu / "gerbers" / "gerbers.zip").exists()


def test_chat_rules_map_requests_to_changes():
    from morph.studio.intent import rules

    d = Design().with_changes(EGG)
    ch, _ = rules("make it work for a strawberry", d)
    assert ch["object.name"] == "strawberry" and ch["object.max_force_n"] == 3
    assert rules("use an SG90 servo instead", d)[0] == {"electronics.servo": "sg90"}
    assert rules("switch to an ESP32", d)[0] == {"electronics.mcu": "esp32_devkit"}
    assert rules("make the fingers 10 mm longer", d)[0]["finger.length_mm"] == d.finger.length_mm + 10
