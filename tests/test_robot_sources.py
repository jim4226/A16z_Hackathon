"""arm.source names a robot bundle (robots/<name>/ or MORPH_HOME/.robots/<name>/), never a path, and a bundle's
URDF loads meshes only from inside its own folder."""

import json
import os

import pytest
import trimesh

from morph.cli import EGG_ROBOT
from morph.robot import imported
from morph.robot.pipeline2 import RobotProject
from morph.robot.urdf_import import load_urdf
from morph.spec import Design


def test_the_shipped_robot_resolves():
    d = imported.robot_dir("so101")
    assert os.path.samefile(d, os.path.join(imported.ROOT, "so101"))
    prof, folder = imported.load_profile("so101")
    assert prof["urdf"] and folder == d
    assert imported.bundle_file(d, prof["urdf"]).endswith(prof["urdf"])


@pytest.mark.parametrize("source", ["../robots/so101", "so101/..", "./so101", "/etc", "SO101", "so101/", "", ".robots",
                                    "so101\\..\\so101", "c:so101", None, 101, "nope",
                                    os.path.join(imported.ROOT, "so101")])
def test_sources_are_names_of_known_robots(source):
    with pytest.raises(ValueError):
        imported.load_profile(source)


def test_imported_robots_resolve_after_the_shipped_ones():
    user = os.path.join(os.environ["MORPH_HOME"], ".robots")
    for name in ("mybot", "so101"):
        os.makedirs(os.path.join(user, name))
        with open(os.path.join(user, name, "morph_profile.json"), "w", encoding="utf-8") as fh:
            json.dump({"urdf": "robot.urdf"}, fh)
    assert os.path.samefile(imported.robot_dir("mybot"), os.path.join(user, "mybot"))
    assert os.path.samefile(imported.robot_dir("so101"), os.path.join(imported.ROOT, "so101"))  # shipped wins


def test_a_profile_can_not_point_out_of_its_folder(tmp_path):
    (tmp_path / "bot").mkdir()
    (tmp_path / "secret.urdf").write_text("<robot/>", encoding="utf-8")
    for rel in ("../secret.urdf", str(tmp_path / "secret.urdf"), "missing.urdf", ".hidden.urdf", None):
        with pytest.raises(ValueError):
            imported.bundle_file(str(tmp_path / "bot"), rel)


def _bundle(tmp_path, filenames):
    bot, outside = tmp_path / "bot", tmp_path / "outside"
    (bot / "assets").mkdir(parents=True)
    outside.mkdir()
    trimesh.creation.box((0.01, 0.01, 0.01)).export(bot / "assets" / "box.stl")
    trimesh.creation.box((0.02, 0.02, 0.02)).export(outside / "outside.stl")
    links = "".join(f'<link name="l{i}"><visual><geometry><mesh filename="{f}"/></geometry></visual></link>'
                    for i, f in enumerate(filenames))
    joints = "".join(f'<joint name="j{i}" type="fixed"><parent link="l0"/><child link="l{i}"/></joint>'
                     for i in range(1, len(filenames)))
    (bot / "bot.urdf").write_text(f'<robot name="bot">{links}{joints}</robot>', encoding="utf-8")
    return bot, outside


def test_mesh_paths_stay_inside_the_robot_folder(tmp_path):
    outside_stl = str(tmp_path / "outside" / "outside.stl").replace("\\", "/")
    ok = ["assets/box.stl", "package://bot/assets/box.stl", "assets/../assets/box.stl"]
    bad = [outside_stl, "file://" + outside_stl, "../outside/outside.stl", "assets/../../outside/outside.stl",
           "C:/outside/outside.stl", "https://example.com/box.stl", "package://bot/../outside/outside.stl", ""]
    bot, outside = _bundle(tmp_path, ok + bad)
    asm = load_urdf(str(bot / "bot.urdf"), bundle=str(bot))
    assert len(asm.parts) == len(ok)
    asm = load_urdf(str(bot / "bot.urdf"), mesh_dirs=[str(outside)], bundle=str(bot))  # nor via a mesh folder outside
    assert len(asm.parts) == len(ok)


def test_symlinked_mesh_out_of_the_robot_folder_is_skipped(tmp_path):
    bot, outside = _bundle(tmp_path, ["assets/box.stl", "assets/link.stl"])
    try:
        os.symlink(outside / "outside.stl", bot / "assets" / "link.stl")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need extra rights on this system")
    assert [p.meta["mesh_file"] for p in load_urdf(str(bot / "bot.urdf")).parts] == ["box.stl"]


def test_a_design_naming_a_path_fails_its_stages_without_reading_it(tmp_path):
    p = RobotProject(str(tmp_path / "egg"))
    rev = p.run(Design().with_changes({**EGG_ROBOT, "arm.source": "../../etc"}), n_trials=1, verbose=False, fast_sim=True)
    res = p.load_results(rev)
    assert "robot source" in res["errors"]["sizing"] or "arm.source" in res["errors"]["sizing"]
    assert res["summary"]["feasible"] is False
