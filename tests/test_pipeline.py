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
    assert dirty_nodes(["object.mass_g"]) == ["analysis", "sim"]
    assert dirty_nodes(["finger.material"]) == ["analysis", "fab", "bom"]
    assert dirty_nodes(["finger.length_mm"]) == ["cad", "analysis", "sim", "fab", "bom"]


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
