"""World mode schema (morph/studio/worlds.py): validation, starter worlds, frames, storage and .glb checks. Offline."""

import copy
import json
import math
import struct

import numpy as np
import pytest

from morph.studio import worlds as W


def _glb(doc: dict, bin_bytes: bytes = b"") -> bytes:
    js = json.dumps(doc).encode()
    js += b" " * (-len(js) % 4)
    out = struct.pack("<II", len(js), 0x4E4F534A) + js
    if bin_bytes:
        bin_bytes += b"\0" * (-len(bin_bytes) % 4)
        out += struct.pack("<II", len(bin_bytes), 0x004E4942) + bin_bytes
    return struct.pack("<III", 0x46546C67, 2, 12 + len(out)) + out


def _tri_doc(n_tris=1, **extra):
    doc = {"asset": {"version": "2.0"}, "buffers": [{"byteLength": 36}], "bufferViews": [{"buffer": 0, "byteLength": 36}],
           "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3 * n_tris, "type": "VEC3",
                          "min": [0, 0, 0], "max": [1, 2, 3]}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "mode": 4}]}], "nodes": [{"mesh": 0}],
           "scenes": [{"nodes": [0]}], "scene": 0}
    doc.update(extra)
    return doc


@pytest.mark.parametrize("name", list(W.STARTERS))
def test_starter_worlds_are_valid(name):
    w = W.starter(name)
    assert W.validate(copy.deepcopy(w)) == w                     # idempotent
    top = w["surface"]["center"][2] + w["surface"]["half"][2]
    assert w["mount"]["position"][2] == pytest.approx(top)
    assert w["obstacles"] and w["zones"] and w["scene"] and w["asset"] == ""
    sw = W.sim_world(w)
    s = sw["surface"]
    assert s["center"][2] + s["half"][2] == pytest.approx(0.0, abs=1e-6)   # surface top = robot base z = 0
    assert all(-5000 < v < 5000 for o in sw["obstacles"] for v in o["center"])
    ids = [o["id"] for o in w["obstacles"]] + [z["id"] for z in w["zones"]]
    assert len(ids) == len(set(ids))
    assert len(W.sha(w)) == 16 and W.sha(w) == W.sha(W.starter(name))


def test_starters_listing():
    s = W.starters()
    assert [x["id"] for x in s] == ["desk", "counter", "workbench"] and all(x["description"] for x in s)
    with pytest.raises(W.WorldError):
        W.starter("garage")


def test_transforms_round_trip():
    w = W.starter("counter")
    rng = np.random.default_rng(3)
    for p in rng.uniform(-2000, 2000, size=(20, 3)):
        assert np.allclose(W.to_world(w, W.to_base(w, p)), p, atol=1e-9)
        assert np.allclose(W.to_base(w, W.to_world(w, p)), p, atol=1e-9)
    assert np.allclose(W.mount_matrix(w) @ W.base_matrix(w), np.eye(4), atol=1e-12)
    # the base x axis points along the mount yaw (-90 deg: the robot faces -y in the room)
    assert np.allclose(W.to_world(w, [100, 0, 0]), np.add(w["mount"]["position"], [0, -100, 0]), atol=1e-9)
    b = W.box_to_base(w, w["obstacles"][0])
    assert b["yaw_deg"] == pytest.approx(90.0)
    assert np.allclose(W.to_world(w, b["center"]), w["obstacles"][0]["center"], atol=0.01)


def test_asset_matrix_maps_gltf_y_up_metres():
    w = {**W.starter("desk"), "asset": "room.glb", "units": "m", "up": "y", "scale": 2.0}
    A = W.asset_matrix(w)
    assert np.allclose(A[:3, :3] @ [1, 0, 0], [2000, 0, 0])
    assert np.allclose(A[:3, :3] @ [0, 1, 0], [0, 0, 2000])          # glTF up -> world z
    assert np.allclose(A[:3, :3] @ [0, 0, 1], [0, -2000, 0])         # glTF +z (towards the viewer) -> world -y
    w2 = {**w, "units": "cm", "up": "z", "scale": 1.0}
    assert np.allclose(W.asset_matrix(w2)[:3, :3], np.eye(3) * 10)


def test_validation_rejects_and_cleans():
    base = W.starter("desk")
    bad = [({"mount": None}, "Place the robot"), ({"surface": None}, "surface"),
           ({"asset": "../../etc/passwd"}, "room.glb"), ({"units": "furlong"}, "units"), ({"up": "x"}, "up"),
           ({"scale": 0.0}, "scale"), ({"scale": float("nan")}, "scale"), ({"version": 9}, "newer"),
           ({"mount": {"position": [0, 0, 2000.0], "yaw_deg": 0}}, "stand on its surface"),
           ({"obstacles": [{"center": [0, 0, 0], "half": [0, 1, 1]}]}, "half size"),
           ({"obstacles": [{"center": [0, 0, float("inf")], "half": [1, 1, 1]}]}, "number"),
           ({"obstacles": [{"center": [0, 0, 1e9], "half": [1, 1, 1]}]}, "between"),
           ({"obstacles": [{}] * 201}, "at most"), ({"zones": [{"kind": "lava", "polygon": [[0, 0]] * 3}]}, "walkway"),
           ({"zones": [{"kind": "seat", "polygon": [[0, 0], [1, 1]]}]}, "polygon"), ({"candidates": [{}] * 4}, "at most")]
    for patch, msg in bad:
        with pytest.raises(W.WorldError, match=msg):
            W.validate({**base, **patch})
    with pytest.raises(W.WorldError):
        W.validate([1, 2])
    raw = {**base, "id": "My Room!!", "name": "  Kitchen\u0000 \n  counter  ",
           "mount": {"position": [-180.0, 120.0, 755.0], "yaw_deg": 270.0},
           "obstacles": [{"label": "Kettle", "center": [1, 2, 3], "half": [5, 5, 5], "source": "evil"},
                         {"label": "Kettle", "center": [4, 5, 6], "half": [5, 5, 5]}],
           "scene": [{"label": "x", "center": [0, 0, 0], "half": [1, 1, 1], "color": "red;}"}], "extra": {"x": 1}}
    w = W.validate(raw)
    assert w["id"] == "my-room" and w["name"] == "Kitchen counter"
    assert w["mount"]["position"][2] == 740.0 and w["mount"]["yaw_deg"] == -90.0     # snapped, yaw wrapped
    assert [o["id"] for o in w["obstacles"]] == ["kettle", "kettle-2"] and w["obstacles"][0]["source"] == "user"
    assert w["scene"][0]["color"] == "#9aa3ad" and "extra" not in w
    z = W.validate({**base, "zones": [{"kind": "walkway", "polygon": [[0, 0], [10, 0], [10, 10]]}]})["zones"][0]
    assert z["z"] == [0.0, 2000.0] and z["label"] == "Walkway"


def test_zone_box_and_polygon_in_base_frame():
    w = W.starter("desk")
    sw = W.sim_world(w)
    chair = next(z for z in sw["zones"] if z["id"] == "chair")
    assert len(chair["polygon"]) == 4
    mz = w["mount"]["position"][2]
    assert chair["z"] == [pytest.approx(0 - mz), pytest.approx(1200 - mz)]
    xs = [p[0] for p in chair["polygon"]]
    assert min(xs) > 400                                 # in front of the robot (it faces the chair)


def test_save_and_load(tmp_path):
    root = str(tmp_path / "proj")
    assert W.load(root) is None
    w, h = W.save(root, W.starter("workbench"))
    assert h == W.sha(w) and W.load(root) == w
    with open(W.world_path(root), encoding="utf-8") as fh:
        assert json.load(fh)["id"] == "workbench"
    with pytest.raises(W.WorldError, match="Upload the room"):
        W.save(root, {**w, "asset": "room.glb"})
    with open(W.world_path(root), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    with pytest.raises(W.WorldError, match="can't be read"):
        W.load(root)
    assert not [f for f in (tmp_path / "proj" / "world").iterdir() if f.name.startswith(".world.")]


def test_check_glb(tmp_path):
    p = tmp_path / "room.glb"
    p.write_bytes(_glb(_tri_doc(2), b"\0" * 36))
    info = W.check_glb(str(p))
    assert info["faces"] == 2 and info["bounds_m"] == [[0, 0, 0], [1, 2, 3]] and not info["warnings"]
    cases = [(b"PK\x03\x04" + b"\0" * 40, "isn't a .glb"), (b"glTF", "too short"),
             (_glb(_tri_doc(), b"\0" * 36)[:-4], "cut off"),
             (_glb(_tri_doc(buffers=[{"byteLength": 36, "uri": "https://example.com/x.bin"}])), "outside"),
             (_glb(_tri_doc(images=[{"uri": "../secret.png"}])), "outside"),
             (_glb(_tri_doc(extensionsRequired=["KHR_draco_mesh_compression"])), "Draco"),
             (_glb(_tri_doc(n_tris=600_000, nodes=[{"mesh": 0}] * 3)), "limit"),
             (_glb({**_tri_doc(), "meshes": []}), "no triangles")]
    for data, msg in cases:
        p.write_bytes(data)
        with pytest.raises(W.WorldError, match=msg):
            W.check_glb(str(p))
    hdr = struct.pack("<III", 0x46546C67, 1, 40) + b"\0" * 28
    p.write_bytes(hdr)
    with pytest.raises(W.WorldError, match="glTF 1"):
        W.check_glb(str(p))
    p.write_bytes(_glb(_tri_doc(extensionsUsed=["KHR_draco_mesh_compression"])))
    assert W.check_glb(str(p))["warnings"]


def test_draft_from_glb_guesses_units():
    d = W.draft_from_glb({"bounds_m": [[-200, 0, -150], [200, 250, 150]]}, "kitchen scan.glb")
    assert d["units"] == "cm" and d["name"] == "kitchen scan" and d["asset"] == "room.glb" and d["notes"]
    assert W.draft_from_glb({"bounds_m": [[-2, -0.05, -2], [2, 2.5, 2]]})["units"] == "m"
    assert W.draft_from_glb({"bounds_m": [[-2000, 0, -2000], [2000, 2500, 2000]]})["units"] == "mm"
    assert math.isclose(W.draft_from_glb({"bounds_m": [[-2, -0.05, -2], [2, 2.5, 2]]})["floor_z"], -50.0)
