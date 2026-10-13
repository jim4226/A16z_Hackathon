"""Import -> build, end to end: robots auto-profiled from their own files run all nine pipeline stages and pass the
pick-and-place simulation (fast sims, about 15 s each). Offline. Third-party robots (SO-ARM100 repo, MuJoCo
Menagerie, Koch v1.1) are only used when MORPH_IMPORT_TESTDATA points at a folder holding them:
soarm100/, menagerie/trs_so_arm100/, koch_sim/, soarm_readme/README.md."""

import json
import os

import pytest

from morph.importers import ImportFailed, session as sess
from morph.robot import imported
from test_import_fixtures import SO101, arm_urdf, so101_copy

RESEARCH = os.environ.get("MORPH_IMPORT_TESTDATA", "")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "projects"
    h.mkdir()
    for k in ("ANTHROPIC_API_KEY", "KEEWANO_API_KEY", "KEEWANO_PAT"):
        monkeypatch.delenv(k, raising=False)
    from morph.importers import common
    monkeypatch.setattr(common, "_HOME", None)
    monkeypatch.setenv("MORPH_HOME", str(h))
    return str(h)


def _built(home, out):
    res = json.load(open(os.path.join(home, out["project"], out["rev"], "results.json"), encoding="utf-8"))
    prov = res["provenance"]
    pp = res["sims"]["scenarios"]["pick_place"]["metrics"]
    meta = json.load(open(os.path.join(home, out["project"], "meta.json"), encoding="utf-8"))
    return prov, pp, res, meta


def test_so101_from_its_urdf_alone_builds_and_picks(home, tmp_path):
    src = so101_copy(str(tmp_path / "so101-urdf"))
    events = []
    out = sess.cli_import(src, name="SO-101 auto", home_dir=home, on_event=events.append, fast_sim=True)
    prov, pp, res, meta = _built(home, out)
    assert sorted(prov.values()) == ["computed"] * 9
    assert pp["success"] is True and pp["place_error_mm"] < 5 and not pp["dropped"]
    c = out["candidate"]
    assert c["tcp"]["how"] == "URDF tool frame gripper_frame_link" and c["license"]["spdx"] == "Apache-2.0"
    assert meta["origin"] == "import" and meta["source"] == out["result"]["source"] and meta["demo"] is False
    assert res["sizing"]["title"] and all(j["actuator"] == "sts3215_7v4" for j in res["sizing"]["joints"])
    assert any(e.get("type") == "node" and e.get("project") == out["project"] for e in events)
    assert any(e.get("type") == "project_created" for e in events)


def test_so101_mujoco_xml_builds_and_picks(home, tmp_path):
    import shutil

    src = tmp_path / "so101-mjcf"
    src.mkdir()
    shutil.copyfile(os.path.join(SO101, "so101_new_calib.xml"), src / "so101_new_calib.xml")
    shutil.copyfile(os.path.join(SO101, "joints_properties.xml"), src / "joints_properties.xml")
    shutil.copytree(os.path.join(SO101, "assets"), src / "assets")
    out = sess.cli_import(str(src), name="so101 mjcf", home_dir=home, fast_sim=True)
    prov, pp, res, meta = _built(home, out)
    assert out["candidate"]["format"] == "mjcf"
    assert sorted(prov.values()) == ["computed"] * 9 and pp["success"] is True


def test_koch_like_package_paths_ask_for_actuators(home, tmp_path):
    src = tmp_path / "koch-v1-1"
    arm_urdf(str(src / "follower"), package="follower",
             joint_names={"shoulder_pan": "joint_1", "shoulder_lift": "joint_2", "elbow_flex": "joint_3",
                          "wrist_flex": "joint_4", "wrist_roll": "joint_5", "gripper": "joint_6"})
    out = sess.cli_import(str(src), home_dir=home, dry_run=True)
    c = out["candidate"]
    assert c["meshes"]["found"] == c["meshes"]["total"] == 7 and c["gripper"]["joint"] == "joint_6"
    assert c["needs"] == ["actuators"] and all(a["key"] is None for a in c["actuators"].values())
    with pytest.raises(ImportFailed, match="Pick the actuators for joint_1"):
        sess.cli_import(str(src), home_dir=home)


def test_auto_profiles_size_for_the_worst_pose_and_shipped_so101_is_unchanged(home, tmp_path):
    from morph.spec import Design

    egg = {"object.mass_g": 60.0}
    s = imported.size(Design().with_changes({**egg, "arm.source": "so101"}))
    lift = next(j for j in s["joints"] if j["name"] == "shoulder_lift")
    assert lift["torque_req_nm"] == pytest.approx(0.661, abs=0.01) and s["reach_mm"] == pytest.approx(391.4, abs=0.5)
    src = so101_copy(str(tmp_path / "s"))
    sess.cli_import(src, name="so101 grid", home_dir=home, build_project=False)
    g = imported.size(Design().with_changes({**egg, "arm.source": "so101-grid"}))
    glift = next(j for j in g["joints"] if j["name"] == "shoulder_lift")
    assert glift["torque_req_nm"] > lift["torque_req_nm"] * 1.3        # the stretched-out pose q = 0 missed
    assert any("shoulder_lift" in i for i in g["issues"])               # 7.4 V STS3215 is marginal at 2x


def test_actuator_family_matching_the_robot_is_not_swapped(home, tmp_path):
    from morph.spec import Design

    prof = {"actuators": {"j1": "xm430_w350", "j2": "xm430_w350", "g": "xm430_w350"}}
    plan = imported.actuator_plan(Design().with_changes({"arm.actuator_family": "dynamixel"}), prof)
    assert set(plan.values()) == {"xm430_w350"}                         # OMX keeps its XM430s
    so101 = imported.load_profile("so101")[0]
    swapped = imported.actuator_plan(Design().with_changes({"arm.actuator_family": "dynamixel"}), so101)
    assert swapped["shoulder_pan"] == "xl430_w250" and swapped["gripper"] == "xl330_m288"
    same = imported.actuator_plan(Design().with_changes({"arm.actuator_family": "smart_bus"}), so101)
    assert set(same.values()) == {"sts3215_7v4"}


def test_morph_export_reimports_as_a_rebuilt_project(home, tmp_path):
    from morph.spec import Design
    from test_import_fixtures import morph_export

    d = Design().with_changes({"name": "egg-picker", "arm.source": "so101", "object.mass_g": 60.0}).to_dict()
    z = morph_export(str(tmp_path / "egg-picker-r002.zip"), d, rev="r002")
    out = sess.cli_import(z, home_dir=home, fast_sim=True)
    prov, pp, res, meta = _built(home, out)
    assert out["kind"] == "project" and out["project"] == "egg-picker" and meta["origin"] == "import"
    assert sorted(prov.values()) == ["computed"] * 9 and res["label"].startswith("imported r002")


# ------------------------------------------------------------------- third-party robots (optional, MORPH_IMPORT_TESTDATA)

def _need(path):
    if not RESEARCH or not os.path.exists(path):
        pytest.skip("set MORPH_IMPORT_TESTDATA to a folder with the downloaded robots")
    return path


def test_research_so100_builds_and_picks(home):
    out = sess.cli_import(_need(f"{RESEARCH}/soarm100"), name="so100", answers={"accept_suggestions": True}, home_dir=home,
                          fast_sim=True)
    prov, pp, _, _ = _built(home, out)
    assert sorted(prov.values()) == ["computed"] * 9 and pp["success"] is True


def test_research_menagerie_so_arm100_builds_and_picks(home):
    out = sess.cli_import(_need(f"{RESEARCH}/menagerie/trs_so_arm100"), name="trs so arm100",
                          answers={"family": "smart_bus"}, home_dir=home, fast_sim=True)
    prov, pp, _, _ = _built(home, out)
    assert out["candidate"]["format"] == "mjcf" and sorted(prov.values()) == ["computed"] * 9 and pp["success"] is True


def test_research_koch_flags_actuators(home):
    out = sess.cli_import(_need(f"{RESEARCH}/koch_sim"), home_dir=home, dry_run=True)
    c = out["candidate"]
    assert c["meshes"]["found"] == c["meshes"]["total"] and "actuators" in c["needs"]
    assert c["actuators"]["joint_1"]["options"] == ["xl330_m288", "xl430_w250"]


def test_research_so_arm100_readme_bom(tmp_path):
    from morph.importers import bom

    res = bom.parse(_need(f"{RESEARCH}/soarm_readme/README.md"))
    assert len(res["rows"]) == 8 and res["total"] == 229.88 and res["currency"] == "USD"
    assert res["tables"][1]["title"].startswith("Parts for One Follower Arm")
