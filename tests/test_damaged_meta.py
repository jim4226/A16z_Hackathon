"""A robot project whose meta.json was cut off (a power cut while it was saved, a hand edit) stays a project (ROB-10): it
is listed, opens, can be deleted, and the Studio writes a new meta.json from its revisions when it starts, keeping the
damaged one beside it. A folder that isn't a Studio robot project (the earlier CLI prototype's) is never taken for one."""

import json
import os

from morph.robot.pipeline2 import RobotProject
from morph.spec import Design
from morph.studio import projects, server


def robot(home, name, source="", demo=False):
    root = home / name
    (root / "r001").mkdir(parents=True)
    meta = {"kind": "robot", "source": source, "need": f"{name} need", "origin": "brief", "demo": demo, "brief_id": "b1"}
    (root / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    (root / "r001" / "design.json").write_text(Design().with_changes({"name": name, "arm.source": source}).dumps(),
                                               encoding="utf-8")
    (root / "r001" / "results.json").write_text(json.dumps({"rev": "r001", "parent": None, "kind": "robot", "summary": {}}),
                                                encoding="utf-8")
    return root


def cut(path):
    raw = open(path, "rb").read()
    open(path, "wb").write(raw[:40])


def test_a_project_with_a_cut_off_meta_json_stays_listed_opens_and_is_repaired(tmp_path, capsys):
    home = tmp_path / "projects"
    robot(home, "egg-picker", demo=True)
    root = robot(home, "proj2", source="so101")
    cut(root / "meta.json")
    assert projects.project_root(str(home), "proj2") == str(root)             # still a project before any repair
    assert "proj2" in [p["name"] for p in projects.list_projects(str(home))]
    st = server.Studio(RobotProject(str(home / "egg-picker")), str(home))
    assert "proj2: meta.json couldn't be read" in capsys.readouterr().out
    meta = projects.read_meta(str(root))
    assert meta["kind"] == "robot" and meta["source"] == "so101" and meta["demo"] is False
    kept = [n for n in os.listdir(root) if n.startswith("meta.json.damaged-")]
    assert len(kept) == 1 and meta["repaired_from"] == kept[0] and len(open(root / kept[0], "rb").read()) == 40
    assert st.state(st.room("proj2"))["revs"] == ["r001"]
    assert projects.unique_name(st.home, "proj2") == "proj2-2"               # its name stays taken
    trash = st.delete_project("proj2")                                       # and it can be deleted
    assert os.path.isdir(trash) and not os.path.exists(root)


def test_a_demo_projects_damaged_meta_keeps_it_a_demo(tmp_path):
    home = tmp_path / "projects"
    robot(home, "egg-picker", demo=True)
    robot(home, "so101", source="so101", demo=True)
    cut(home / "so101" / "meta.json")
    assert projects.backfill_demo(str(home)) == []                           # repaired first, never written over unread
    meta = projects.read_meta(str(home / "so101"))
    assert meta["demo"] is True and meta["kind"] == "robot" and meta["source"] == "so101"
    assert [n for n in os.listdir(home / "so101") if n.startswith("meta.json.damaged-")]
    assert "so101" in [p["name"] for p in projects.list_projects(str(home)) if p["demo"]]


def test_a_folder_that_isnt_a_studio_robot_project_is_never_taken_for_one(tmp_path):
    home = tmp_path / "projects"
    old = home / "gripper-proto"
    (old / "r001").mkdir(parents=True)
    (old / "meta.json").write_text('{"kind": "gri', encoding="utf-8")       # damaged, and no robot results or .morph
    (old / "r001" / "results.json").write_text(json.dumps({"rev": "r001"}), encoding="utf-8")
    assert projects.project_root(str(home), "gripper-proto") is None
    assert not projects.repair_meta(str(old)) and open(old / "meta.json").read() == '{"kind": "gri'


def test_meta_json_is_synced_before_it_replaces_the_old_one(tmp_path, monkeypatch):
    from morph.services import durable
    seen = []
    monkeypatch.setattr(durable, "write_text", lambda path, text, **kw: seen.append(os.path.basename(path)))
    projects.write_meta(str(tmp_path), {"kind": "robot"})
    assert seen == ["meta.json"]
