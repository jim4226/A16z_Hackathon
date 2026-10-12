"""What import understands: detection, URDF bundles, auto-profiles, MuJoCo XML, meshes as objects, parts lists,
KiCad boards, Morph exports and the user robot store. Small generated fixtures; offline."""

import json
import os
import zipfile

import numpy as np
import pytest

from morph.importers import ImportFailed, autoprofile, bom, detect, kicad, mesh_object, mjcf, project_zip, robots, urdf
from test_import_fixtures import BOM_MD, KICAD_PCB, SO101, arm_urdf, egg_stl, morph_export, stl, xlsx

# ------------------------------------------------------------------------------------------- detection


def test_sniff_by_content_not_extension(tmp_path):
    files = {
        "robot.txt": '<?xml version="1.0"?>\n<!-- c --><robot name="r"><link name="a"/></robot>',
        "macro.xml": '<robot xmlns:xacro="http://www.ros.org/wiki/xacro" name="r"/>',
        "model.urdf": '<mujoco model="m"><worldbody/></mujoco>',
        "package.xml": "<package><name>x</name></package>",
        "part.step": "ISO-10303-21;\nHEADER;",
        "board.txt": "(kicad_pcb (version 20240108))",
        "old.sch": "EESchema Schematic File Version 4",
        "parts.csv": "Part,Qty,Price\nServo,6,13.89\nBoard,1,10.6\n",
        "notes.md": "| Part | Qty |\n|---|---|\n| a | 1 |\n",
        "LICENSE": "Apache License\nVersion 2.0",
        "mesh.dat": "solid x\nfacet normal 0 0 1\nouter loop\nendloop\nendfacet\nendsolid x\n",
        "obj.bin": "# exported\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    (tmp_path / "cad.sldprt").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100)
    (tmp_path / "pic.stl").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    stl(str(tmp_path / "box.anything"))
    want = {"robot.txt": "urdf", "macro.xml": "xacro", "model.urdf": "mjcf", "package.xml": "package_xml",
            "part.step": "step", "board.txt": "kicad_pcb", "old.sch": "kicad_legacy", "parts.csv": "csv",
            "notes.md": "markdown", "LICENSE": "license", "mesh.dat": "stl", "obj.bin": "obj", "cad.sldprt": "native_cad",
            "pic.stl": "image", "box.anything": "stl"}
    got = {f["path"]: f["format"] for f in detect.scan(str(tmp_path))}
    assert got == want
    assert "onshape-to-robot" in detect.refusal("native_cad", "cad.sldprt")
    z = tmp_path / "x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("export-manifest.json", "{}")
        zf.writestr("design.json", "{}")
    assert detect.sniff(str(z)) == "morph_project"
    assert detect.sniff(xlsx(str(tmp_path / "b.xlsx"), [["Part", "Qty"], ["a", 1]])) == "xlsx"


# ------------------------------------------------------------------------------------------- URDF bundles


def test_package_paths_world_link_and_missing_meshes(tmp_path):
    bundle = tmp_path / "koch_like"
    u = arm_urdf(str(bundle / "follower"), package="follower", world=True, missing=("wrist",))
    rep = urdf.normalize(u, str(bundle), str(tmp_path / "out"))
    assert rep["meshes"]["total"] == 7 and rep["meshes"]["found"] == 6
    assert rep["meshes"]["missing"][0]["ref"] == "package://follower/meshes/wrist.stl"
    assert rep["warnings"][0].startswith("1 of 7 meshes missing (wrist.stl); searched packages: follower.")
    assert rep["renamed"] == {"world": "world_link"} and rep["root"] == "world_link"
    out = open(rep["urdf"], encoding="utf-8").read()
    assert "package://" not in out and 'filename="meshes/shoulder.stl"' in out
    from morph.robot.urdf_import import load_urdf
    asm = load_urdf(rep["urdf"])
    assert len(asm.parts) == 6 and "world" not in asm.links


def test_names_are_made_safe_and_unique(tmp_path):
    p = tmp_path / "r.urdf"
    p.write_text('<robot name="r"><link name="Base Link"/><link name="base-link"/><link name="tcp"/>'
                 '<joint name="J 1" type="revolute"><parent link="Base Link"/><child link="base-link"/><limit lower="-1" upper="1"/></joint>'
                 '<joint name="j2" type="floating"><parent link="base-link"/><child link="tcp"/></joint></robot>', encoding="utf-8")
    rep = urdf.normalize(str(p), str(tmp_path), str(tmp_path / "o"))
    j = urdf.joints_of(rep["urdf"])
    assert set(j) == {"j_1", "j2"} and j["j2"]["type"] == "fixed" and j["j_1"]["child"] == "base_link_2"
    assert rep["renamed"]["tcp"] == "tcp_orig" and any("floating" in w for w in rep["warnings"])


@pytest.mark.parametrize("body,msg", [
    ('<link name="a"/><link name="b"/>', "root links"),
    ('<link name="a"/><link name="b"/><joint name="j" type="fixed"><parent link="a"/><child link="c"/></joint>', "doesn't exist"),
    ('<link name="a"/><link name="b"/><joint name="j" type="screw"><parent link="a"/><child link="b"/></joint>', "unknown type"),
])
def test_broken_trees_are_refused(tmp_path, body, msg):
    p = tmp_path / "r.urdf"
    p.write_text(f'<robot name="r">{body}</robot>', encoding="utf-8")
    with pytest.raises(ImportFailed, match=msg):
        urdf.normalize(str(p), str(tmp_path), str(tmp_path / "o"))


# ------------------------------------------------------------------------------------------- auto-profile


def _derive(tmp_path, **kw):
    answers = kw.pop("answers", {})
    u = arm_urdf(str(tmp_path / "src"), **kw)
    out = tmp_path / "cand"
    rep0 = urdf.normalize(u, str(tmp_path / "src"), str(out))
    return autoprofile.derive(rep0["urdf"], str(out), bundle=str(tmp_path / "src"), answers=answers, title="Test arm")


def test_autoprofile_finds_chain_gripper_tcp_base_and_ready_pose(tmp_path):
    prof, rep = _derive(tmp_path)
    assert rep["chain"] == ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]
    assert rep["arm_joints"] == ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]
    assert rep["gripper"]["joint"] == "gripper" and "named like a gripper" in rep["gripper"]["why"]
    assert rep["tcp"]["how"].startswith("wrist-roll axis")
    assert rep["ready"]["err_mm"] < 2 and rep["ready"]["tool_down"] > 0.97
    assert abs(rep["base"]["lift_mm"]) < 1 and abs(rep["base"]["yaw_deg"]) < 1
    # the URDF says nothing about motors: Morph asks instead of guessing from effort values
    assert rep["scope"] == "needs_answers" and rep["unknown_actuators"] == rep["arm_joints"] + ["gripper"]
    assert "simulation value" in rep["actuators"]["shoulder_lift"]["basis"]
    assert os.path.isfile(tmp_path / "cand" / "robot.urdf") and prof["urdf"] == "robot.urdf"
    assert prof["imported"]["auto"] is True and prof["steps"][0]["title"] == "Install base link"


def test_autoprofile_motors_from_mesh_names_and_answers(tmp_path):
    prof, rep = _derive(tmp_path / "a", motor_names=True)
    assert rep["ok"] and set(prof["actuators"].values()) == {"sts3215_7v4"}
    assert prof["part_overrides"] and "mesh name mentions sts3215" in rep["actuators"]["elbow_flex"]["basis"]
    prof, rep = _derive(tmp_path / "b", answers={"family": "dynamixel"})
    assert rep["ok"] and {k for k in prof["actuators"].values()} <= {"xl330_m288", "xl430_w250", "xm430_w350"}
    assert "sized by Morph" in rep["actuators"]["shoulder_lift"]["basis"]
    prof, rep = _derive(tmp_path / "c", answers={"actuators": {n: "mg996r" for n in
                                                               ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]}})
    assert rep["ok"] and rep["family"] == "hobby_pwm"
    with pytest.raises(ImportFailed, match="not an actuator Morph knows"):
        _derive(tmp_path / "d", answers={"actuators": {"gripper": "warp_drive"}})


def test_generic_joint_names_still_find_the_gripper(tmp_path):
    names = {"shoulder_pan": "joint_1", "shoulder_lift": "joint_2", "elbow_flex": "joint_3", "wrist_flex": "joint_4",
             "wrist_roll": "joint_5", "gripper": "joint_6"}
    prof, rep = _derive(tmp_path, joint_names=names)
    assert rep["gripper"]["joint"] == "joint_6" and rep["gripper"]["score"] >= 3
    assert rep["arm_joints"] == ["joint_1", "joint_2", "joint_3", "joint_4", "joint_5"]


def test_readme_motor_mentions_become_suggestions(tmp_path):
    src = tmp_path / "src"
    arm_urdf(str(src))
    (src / "README.md").write_text("Uses six STS3215 Servo 7.4V motors.", encoding="utf-8")
    out = tmp_path / "cand"
    rep0 = urdf.normalize(str(src / "testarm.urdf"), str(src), str(out))
    prof, rep = autoprofile.derive(rep0["urdf"], str(out), bundle=str(src))
    a = rep["actuators"]["shoulder_lift"]
    assert a["key"] is None and a["suggest"] == "sts3215_7v4" and "README.md" in a["suggest_basis"]
    prof, rep = autoprofile.derive(rep0["urdf"], str(out), bundle=str(src), answers={"accept_suggestions": True})
    assert rep["ok"] and set(prof["actuators"].values()) == {"sts3215_7v4"}


def test_legged_robots_and_tiny_chains_are_out_of_scope(tmp_path):
    L = ['<robot name="hexapod"><link name="body"/>']
    for leg in range(6):
        prev = "body"
        for k in range(3):
            n = f"l{leg}_{k}"
            L.append(f'<link name="{n}"/><joint name="j{leg}_{k}" type="revolute"><parent link="{prev}"/><child link="{n}"/>'
                     f'<axis xyz="0 1 0"/><limit lower="-1" upper="1"/></joint>')
            prev = n
    L.append("</robot>")
    p = tmp_path / "hex.urdf"
    p.write_text("".join(L), encoding="utf-8")
    rep0 = urdf.normalize(str(p), str(tmp_path), str(tmp_path / "o"))
    with pytest.raises(ImportFailed, match="18 joints in 6 limbs"):
        autoprofile.derive(rep0["urdf"], str(tmp_path / "o"))


# ------------------------------------------------------------------------------------------- MuJoCo XML


def test_mjcf_so101_converts_with_motor_names_and_gripper(tmp_path):
    rep = mjcf.to_urdf(os.path.join(SO101, "so101_new_calib.xml"), SO101, str(tmp_path))
    assert rep["movable"] == 6 and rep["meshes"]["found"] == 17
    assert {a["joint"] for a in rep["actuated"]} >= {"shoulder_pan", "gripper"}
    prof, r = autoprofile.derive(rep["urdf"], str(tmp_path), bundle=SO101)
    assert r["gripper"]["joint"] == "gripper" and r["ok"] and set(prof["actuators"].values()) == {"sts3215_7v4"}


def test_mjcf_scene_props_are_dropped(tmp_path):
    stl(str(tmp_path / "m.stl"), (0.02, 0.02, 0.02))
    (tmp_path / "r.xml").write_text("""<mujoco model="r"><compiler angle="radian"/><asset><mesh name="m" file="m.stl"/></asset><worldbody>
      <geom type="plane" size="1 1 0.1"/>
      <body name="cube" pos="0.3 0 0.02"><freejoint/><geom type="box" size="0.02 0.02 0.02"/></body>
      <body name="base"><geom type="mesh" mesh="m"/>
        <body name="link1" pos="0 0 0.05"><joint name="j1" axis="0 0 1" range="-1 1"/><geom type="box" size="0.01 0.01 0.03"/>
          <body name="link2" pos="0 0 0.06"><joint name="j2" axis="0 1 0" range="-1 1"/><geom type="capsule" size="0.01 0.03"/></body>
        </body></body></worldbody></mujoco>""", encoding="utf-8")
    rep = mjcf.to_urdf(str(tmp_path / "r.xml"), str(tmp_path), str(tmp_path / "o"))
    assert rep["movable"] == 2 and any("ignored 1 other bodies (cube)" in w for w in rep["warnings"])
    j = urdf.joints_of(rep["urdf"])
    assert j["j1"]["type"] == "revolute" and j["j1"]["upper"] == pytest.approx(1.0)


# ------------------------------------------------------------------------------------------- meshes as objects


def test_mesh_object_egg_in_metres_and_units_answer(tmp_path):
    res = mesh_object.object_changes(egg_stl(str(tmp_path / "egg.stl")))
    ch = res["changes"]
    assert res["units"]["unit"] == "m" and ch["object.shape"] == "cylinder"
    assert ch["object.diameter_mm"] == pytest.approx(44, abs=1.5) and ch["object.length_mm"] == pytest.approx(57, abs=1.5)
    assert 40 < ch["object.mass_g"] < 65 and "estimated" in res["mass"]["basis"]
    assert ch["object.name"] == "egg" and "finger.length_mm" in ch
    res2 = mesh_object.object_changes(egg_stl(str(tmp_path / "egg2.stl"), metres=False), answers={"units": "cm"})
    assert res2["changes"]["object.diameter_mm"] == pytest.approx(440, abs=10) and res2["warnings"]


def test_mesh_object_box_and_sphere(tmp_path):
    import trimesh

    box = tmp_path / "Box_40x60x100_v2.obj"
    trimesh.creation.box((40, 60, 100)).export(str(box))
    ch = mesh_object.object_changes(str(box))["changes"]
    assert (ch["object.shape"], ch["object.diameter_mm"], ch["object.length_mm"]) == ("box", 60.0, 100.0)
    assert ch["object.name"] == "Box 40x60x100" and ch["object.mass_g"] == pytest.approx(240, rel=0.01)
    ball = tmp_path / "ball.ply"
    trimesh.creation.icosphere(4, 20.0).export(str(ball))
    ch = mesh_object.object_changes(str(ball), answers={"density_g_cc": 0.1})["changes"]
    assert ch["object.shape"] == "sphere" and ch["object.diameter_mm"] == pytest.approx(40, abs=0.5)
    assert ch["object.mass_g"] == pytest.approx(3.3, abs=0.2)


def test_flat_surface_is_not_an_object(tmp_path):
    import trimesh

    p = tmp_path / "plane.stl"
    trimesh.Trimesh(vertices=[[0, 0, 0], [10, 0, 0], [0, 10, 0]], faces=[[0, 1, 2]]).export(str(p))
    with pytest.raises(ImportFailed):
        mesh_object.object_changes(str(p))


# ------------------------------------------------------------------------------------------- parts lists


def test_markdown_bom_like_the_so_arm100_readme(tmp_path):
    p = tmp_path / "README.md"
    p.write_text(BOM_MD, encoding="utf-8")
    res = bom.parse(str(p))
    rows = res["rows"]
    assert [r["item"] for r in rows] == ["STS3215 Servo 7.4V, 1/345 gear (C001)", "Motor Control Board", "USB-C Cable 2 pcs",
                                         "Power Supply", "Screwdriver Set"]
    assert rows[0]["qty"] == 6 and rows[0]["unit_price"] == 13.89 and rows[0]["ext_price"] == 83.34
    assert rows[0]["currency"] == "USD" and rows[0]["url"] == "https://www.alibaba.com/product-detail/STS3215_1"
    assert rows[0]["catalog"] == "sts3215_7v4" and rows[0]["category"] == "actuator" and rows[0]["make_or_buy"] == "buy"
    assert res["total"] == 116.94 and res["columns"]["unit_price"] == "Unit Cost (US)" and res["title"].startswith("Parts For One")
    assert bom.actuator_keys(rows) == {"sts3215_7v4": 6}
    parts = bom.darwin_parts(rows)
    assert ("Motor Control Board", "buy Motor Control Board, price and shipping") in parts


def test_csv_tsv_and_xlsx_with_header_synonyms(tmp_path):
    (tmp_path / "bom.csv").write_text("Robot parts list\n\nDescription,Manufacturer Part Number,Quantity,Price (each),Link\n"
                                      '"Bearing 608ZZ",608ZZ,4,$1.20,https://example.com/608\n'
                                      "M3x8 screw,,20,0.05,\nTotal,,,5.80,\n", encoding="utf-8")
    res = bom.parse(str(tmp_path / "bom.csv"))
    assert [(r["item"], r["qty"], r["mpn"]) for r in res["rows"]] == [("Bearing 608ZZ", 4, "608ZZ"), ("M3x8 screw", 20, None)]
    assert res["rows"][1]["category"] == "fastener" and res["total"] == pytest.approx(5.8)
    (tmp_path / "bom.tsv").write_text("Part\tQty\tUnit price\nXL430-W250-T\t2\t49.90\n", encoding="utf-8")
    r = bom.parse(str(tmp_path / "bom.tsv"))["rows"][0]
    assert r["catalog"] == "xl430_w250" and r["ext_price"] == pytest.approx(99.8)
    path = xlsx(str(tmp_path / "bom.xlsx"), [["Item", "Qty", "Unit Cost"], ["MG996R servo", 6, 9.0], ["Arduino Nano", 1, 24.9]])
    res = bom.parse(path)
    assert [(r["item"], r["qty"], r["unit_price"]) for r in res["rows"]] == [("MG996R servo", 6, 9.0), ("Arduino Nano", 1, 24.9)]
    assert res["rows"][0]["catalog"] == "mg996r" and res["total"] == pytest.approx(78.9)


def test_tables_without_parts_are_not_boms(tmp_path):
    (tmp_path / "x.md").write_text("| Joint | Range |\n|---|---|\n| j1 | 90 |\n", encoding="utf-8")
    with pytest.raises(ImportFailed, match="No parts list"):
        bom.parse(str(tmp_path / "x.md"))


# ------------------------------------------------------------------------------------------- KiCad


def test_kicad_pcb_footprints_and_outline(tmp_path):
    (tmp_path / "b.kicad_pcb").write_text(KICAD_PCB, encoding="utf-8")
    res = kicad.read(str(tmp_path / "b.kicad_pcb"))
    assert res["board_mm"] == [50.0, 30.0] and res["layers"] == 2 and res["footprints"] == 4
    rows = {r["value"]: r for r in res["rows"]}
    assert set(rows) == {"10k", "100n"}                      # the mounting hole is excluded from the BOM
    assert rows["10k"]["qty"] == 2 and rows["10k"]["refs"] == "R1, R2" and rows["10k"]["mpn"] == "RC0603FR-0710KL"


def test_kicad_schematic_skips_power_symbols(tmp_path):
    (tmp_path / "s.kicad_sch").write_text("""(kicad_sch (version 20231120)
      (lib_symbols (symbol "Device:R" (property "Reference" "R")))
      (symbol (lib_id "Device:R") (in_bom yes) (dnp no) (property "Reference" "R1") (property "Value" "1k") (property "Footprint" "R_0603"))
      (symbol (lib_id "Device:R") (in_bom yes) (dnp yes) (property "Reference" "R2") (property "Value" "1k"))
      (symbol (lib_id "power:GND") (property "Reference" "#PWR01") (property "Value" "GND")))""", encoding="utf-8")
    res = kicad.read(str(tmp_path / "s.kicad_sch"))
    assert [(r["item"], r["refs"]) for r in res["rows"]] == [("1k R_0603", "R1")]


@pytest.mark.parametrize("text,msg", [("(kicad_pcb (version 1)", "unbalanced"), ("EESchema Schematic File", "KiCad 5"),
                                      ("(kicad_pcb " + "(a " * 600 + ")" * 601, "nested too deeply")])
def test_kicad_bad_files(tmp_path, text, msg):
    (tmp_path / "x.kicad_pcb").write_text(text, encoding="utf-8")
    with pytest.raises(ImportFailed, match=msg):
        kicad.read(str(tmp_path / "x.kicad_pcb"))


# ------------------------------------------------------------------------------------------- Morph exports


def test_morph_export_is_rebuilt_not_trusted(tmp_path):
    from morph.spec import Design

    d = Design().with_changes({"name": "egg-picker", "object.mass_g": 80.0}).to_dict()
    d["arm"]["future_setting"] = 3                        # a newer Morph wrote it: dropped with a warning
    z = morph_export(str(tmp_path / "egg-picker-r004.zip"), d, extra={"cad/robot.glb": b"glb"})
    info = project_zip.inspect(z)
    assert info["project"] == "egg-picker" and info["designs"][0]["rev"] == "r004"
    assert any("future_setting" in w for w in info["warnings"])
    plan = project_zip.plan(z, home_dir=str(tmp_path / "home"))
    assert plan["kind"] == "project" and plan["design"]["object"]["mass_g"] == 80.0 and plan["robot"] is None
    assert plan["meta"]["origin"] == "import" and "results" not in json.dumps(plan)


def test_multi_revision_export_with_its_robot(tmp_path):
    from morph.spec import Design

    cand = tmp_path / "cand"
    rep0 = urdf.normalize(arm_urdf(str(tmp_path / "src"), motor_names=True), str(tmp_path / "src"), str(cand))
    autoprofile.derive(rep0["urdf"], str(cand))
    z = tmp_path / "proj.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("myarm/manifest.json", json.dumps({"format": "morph-project", "project": "My Arm"}))
        zf.writestr("myarm/meta.json", json.dumps({"kind": "robot", "need": "pick eggs"}))
        for i, m in enumerate((60.0, 70.0), 1):
            zf.writestr(f"myarm/r00{i}/design.json", json.dumps(Design().with_changes({"arm.source": "myarm", "object.mass_g": m}).to_dict()))
        for dp, _, fns in os.walk(cand):
            for f in fns:
                if f in ("robot.urdf", "morph_profile.json") or dp.endswith("meshes"):
                    rel = os.path.relpath(os.path.join(dp, f), cand).replace(os.sep, "/")
                    zf.write(os.path.join(dp, f), f"myarm/robots/myarm/{rel}")
    home = tmp_path / "home"
    plan = project_zip.plan(str(z), home_dir=str(home))
    assert plan["revisions"] == 2 and plan["design"]["object"]["mass_g"] == 70.0 and len(plan["history"]) == 1
    assert plan["robot"] == "myarm" and plan["changes"] == {"arm.source": "myarm"} and plan["meta"]["need"] == "pick eggs"
    assert os.path.isfile(robots.resolve("myarm", home_dir=str(home)) + "/robot.urdf")
    plan2 = project_zip.plan(str(z), home_dir=str(home))      # importing it again never overwrites the first robot
    assert plan2["robot"] == "myarm-2"


# ------------------------------------------------------------------------------------------- robot store, licences


def test_robot_store_save_resolve_and_list(tmp_path):
    home = str(tmp_path / "home")
    cand = tmp_path / "cand"
    src = tmp_path / "src"
    rep0 = urdf.normalize(arm_urdf(str(src), motor_names=True), str(src), str(cand))
    autoprofile.derive(rep0["urdf"], str(cand))
    (src / "LICENSE").write_text("Apache License\n Version 2.0, January 2004", encoding="utf-8")
    lic = robots.detect_license(str(src))
    assert lic["spdx"] == "Apache-2.0" and lic["warning"] is None
    source, folder = robots.save(str(cand), "My Test Arm", title="My test arm", license_info=lic, bundle=str(src), home_dir=home)
    assert source == "my-test-arm" and robots.resolve(source, home_dir=home) == os.path.realpath(folder)
    assert os.path.isfile(os.path.join(folder, "licenses", "LICENSE"))
    meta = json.load(open(os.path.join(folder, "import.json"), encoding="utf-8"))
    assert meta["license"]["spdx"] == "Apache-2.0" and "robot.urdf" in meta["files"]
    assert robots.resolve("so101", home_dir=home).endswith(os.path.join("robots", "so101"))     # shipped robots win
    names = {r["name"]: r for r in robots.list_robots(home)}
    assert names["so101"]["shipped"] and not names["my-test-arm"]["shipped"]
    assert robots.save(str(cand), "so101", home_dir=home)[0] == "so101-2"                         # never shadows a shipped robot
    assert robots.bundle_hash("my-test-arm", home_dir=home)


@pytest.mark.parametrize("text,spdx,warn", [
    ("MIT License\n\nPermission is hereby granted, free of charge", "MIT", False),
    ("GNU GENERAL PUBLIC LICENSE\n Version 3, 29 June 2007", "GPL-3.0", True),
    ("Creative Commons Attribution-NonCommercial 4.0", "CC-BY-NC", True),
])
def test_license_detection_and_warnings(tmp_path, text, spdx, warn):
    (tmp_path / "LICENSE.md").write_text(text, encoding="utf-8")
    lic = robots.detect_license(str(tmp_path))
    assert lic["spdx"] == spdx and bool(lic["warning"]) == warn


def test_no_license_and_hf_card(tmp_path):
    assert "No licence found" in robots.detect_license(str(tmp_path))["warning"]
    (tmp_path / "README.md").write_text("---\nlicense: apache-2.0\ntags: [robotics]\n---\n# Arm", encoding="utf-8")
    assert robots.detect_license(str(tmp_path))["spdx"] == "Apache-2.0"
    (tmp_path / "package.xml").write_text("<package><license>BSD</license></package>", encoding="utf-8")
