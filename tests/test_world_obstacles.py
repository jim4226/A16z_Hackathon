"""Automatic obstacle boxes from a room scan (morph/studio/worlds.auto_obstacles) on a synthetic counter scene.
Offline, a few seconds."""

import time

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")

from morph.studio import worlds as W  # noqa: E402


def counter_scene_glb(path, noise=0.0):
    """A kitchen counter as a glTF scan would arrive (y up, metres): counter top (top at 0.90 m), a kettle standing on
    it, the back wall, a wall shelf above and the floor. Returns the kettle's world position (mm, z up)."""
    tf = trimesh.transformations
    up = tf.rotation_matrix(-np.pi / 2, (1, 0, 0))               # build z-up, export y-up (glTF)
    parts = [
        trimesh.creation.box((1.2, 0.6, 0.04), tf.translation_matrix((0, 0, 0.88))),
        trimesh.creation.cylinder(0.08, 0.25, sections=48, transform=tf.translation_matrix((0.25, 0.05, 1.025))),
        trimesh.creation.box((2.4, 0.02, 1.6), tf.translation_matrix((0, 0.31, 1.0))),
        trimesh.creation.box((0.9, 0.25, 0.02), tf.translation_matrix((-0.1, 0.18, 1.45))),
        trimesh.creation.box((4.0, 4.0, 0.02), tf.translation_matrix((0, 0, -0.01))),
    ]
    mesh = trimesh.util.concatenate(parts).subdivide()
    if noise:
        mesh.vertices += np.random.default_rng(0).normal(0, noise, mesh.vertices.shape)
    mesh.apply_transform(up)
    scene = trimesh.Scene()
    scene.add_geometry(mesh, node_name="scan", geom_name="scan")
    path.write_bytes(scene.export(file_type="glb"))
    return np.array([250.0, 50.0, 1025.0])


def placed_world(mount=(0.0, -100.0, 900.0)):
    return W.validate({"id": "room", "name": "Kitchen scan", "asset": "room.glb", "units": "m", "up": "y",
                       "mount": {"position": list(mount), "yaw_deg": -90.0},
                       "surface": {"label": "Counter", "center": [mount[0], mount[1], mount[2] - 15], "half": [400, 250, 15],
                                   "yaw_deg": 0.0}})


def _inside(box, p, tol=0.0):
    loc = W._rz(box["yaw_deg"]).T @ (np.asarray(p) - box["center"])
    return bool(np.all(np.abs(loc) <= np.asarray(box["half"]) + tol))


def test_auto_obstacles_from_a_counter_scan(tmp_path):
    glb = tmp_path / "room.glb"
    kettle = counter_scene_glb(glb)
    w = placed_world()
    t0 = time.time()
    out = W.auto_obstacles(str(glb), w)
    assert time.time() - t0 < 15.0
    obs, st = out["obstacles"], out["stats"]
    assert 3 <= len(obs) <= 60 and not st["truncated"] and st["voxels"] > 50
    assert any(_inside(o, kettle, 5.0) for o in obs), "the kettle needs a box"
    assert any(_inside(o, [0.0, 300.0, 1100.0], 5.0) for o in obs), "the back wall needs a box"
    assert any(_inside(o, [-100.0, 180.0, 1450.0], 15.0) for o in obs), "the shelf needs a box"
    assert all(o["center"][2] - o["half"][2] >= 900.0 - 1e-6 for o in obs)       # nothing below the counter top
    assert not any(_inside(o, [0.0, -100.0, 905.0]) for o in obs)                  # the robot's spot stays free
    assert all(o["source"] == "auto" for o in obs)
    s = out["surface"]
    assert s is not None and s["center"][2] + s["half"][2] == pytest.approx(900.0, abs=1.0)
    assert s["half"][0] == pytest.approx(600.0, abs=25.0) and s["half"][1] == pytest.approx(300.0, abs=25.0)
    assert s["center"][0] == pytest.approx(0.0, abs=25.0) and s["center"][1] == pytest.approx(0.0, abs=25.0)
    # the proposals are valid world content
    w2 = W.validate({**w, "surface": s, "obstacles": obs})
    assert len(w2["obstacles"]) == len(obs)


def test_auto_obstacles_with_scan_noise_and_a_box_cap(tmp_path):
    glb = tmp_path / "room.glb"
    kettle = counter_scene_glb(glb, noise=0.002)
    full = W.auto_obstacles(str(glb), placed_world())["obstacles"]
    assert 3 <= len(full) <= 6, "2 mm of scan noise must not shatter the shelf and the wall into slabs"
    out = W.auto_obstacles(str(glb), placed_world(), max_boxes=2)
    obs = out["obstacles"]
    assert len(obs) == 2 and out["stats"]["truncated"] and out["stats"]["dropped_boxes"] > 0
    assert any(_inside(o, kettle, 25.0) for o in obs)                              # the nearest boxes are kept
    assert not any(_inside(o, [0.0, 300.0, 1100.0], 5.0) for o in obs)              # the far wall is the one dropped


def test_auto_obstacles_refuses_a_bad_file(tmp_path):
    bad = tmp_path / "room.glb"
    bad.write_bytes(b"not a glb at all, just text" * 4)
    with pytest.raises(W.WorldError):
        W.auto_obstacles(str(bad), placed_world())
