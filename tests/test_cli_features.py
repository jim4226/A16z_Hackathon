"""The 0.2 features from a terminal: `morph robot new "<one-liner>"` (the New robot brief, then a build), `morph export`,
`morph world` and the feature lines of `morph doctor`. Offline; the pipeline is a stand-in except where a test says so."""

import argparse
import json
import zipfile

import pytest

from morph import cli, keys
from morph.robot.pipeline2 import ORDER, OUTPUT_KEYS, RobotProject
from morph.studio import agents, projects


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "HOME", str(tmp_path / "projects"))
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))  # main() must not load the developer's keys
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    monkeypatch.setattr(agents, "_subq", lambda: None)
    return tmp_path / "projects"


@pytest.fixture
def builds(monkeypatch):
    """RobotProject.run without the pipeline: each design it was asked to build, and a pick and place result."""
    built = []

    def run(self, design, parent=None, label="", **kw):
        rev = self.claim_rev()
        with open(f"{self.rev_dir(rev)}/design.json", "w", encoding="utf-8") as fh:
            fh.write(design.dumps())
        pp = {"success": True, "world": design.world.id, "world_sha": design.world.sha, "min_clearance_mm": 41.0,
              "world_contacts": []}
        res = {"rev": rev, "parent": parent, "label": label, "summary": {"feasible": True}, "provenance": {n: "computed" for n in ORDER},
               "stage_status": {n: "computed" for n in ORDER}, **{OUTPUT_KEYS[n]: {"ok": True} for n in ORDER},
               "sims": {"scenarios": {"pick_place": {"metrics": pp}}}}
        with open(f"{self.rev_dir(rev)}/results.json", "w", encoding="utf-8") as fh:
            json.dump(res, fh)
        (self.p if hasattr(self, "p") else self).log({"type": "revision", "rev": rev})
        built.append((design, kw))
        return rev

    monkeypatch.setattr(RobotProject, "run", run)
    return built


def test_robot_new_from_a_one_liner_runs_the_brief_then_builds(home, builds, capsys):
    assert cli.main(["robot", "new", "help me sort socks", "--defaults", "--trials", "2"]) == 0
    out = capsys.readouterr().out
    assert "triage" in out and "built-in rules" in out and "take Morph's default answers" in out
    assert "Building sock-sorter" in out and "morph studio sock-sorter" in out
    root = home / "sock-sorter"
    meta = projects.read_meta(str(root))
    assert meta["origin"] == "brief" and meta["demo"] is False and len(meta["brief_id"]) == 16 and "socks" in meta["need"]
    final = json.loads((root / "brief.json").read_text(encoding="utf-8"))
    assert final["status"] == "built" and final["project"] == "sock-sorter" and final["rev"] == "r001"
    design, kw = builds[-1]
    assert design.name == "sock-sorter" and kw.get("n_trials") == 2 and kw.get("fast_sim") is False
    assert not (home / ".briefs").exists() or not list((home / ".briefs").iterdir())   # no draft left behind
    # the same request again gets its own project; --name names it, and a taken name is refused before anything is built
    assert cli.main(["robot", "new", "help me sort socks", "--defaults"]) == 0 and (home / "sock-sorter-2").is_dir()
    assert cli.main(["robot", "new", "sort LEGO bricks by colour", "--defaults", "--name", "lego-sorter"]) == 0
    assert projects.read_meta(str(home / "lego-sorter"))["origin"] == "brief"
    with pytest.raises(SystemExit, match="already exists"):
        cli.main(["robot", "new", "sort LEGO bricks by colour", "--defaults", "--name", "lego-sorter"])
    with pytest.raises(SystemExit, match="lower-case"):
        cli.main(["robot", "new", "sort LEGO bricks by colour", "--name", "Bad_Name"])
    assert len(builds) == 3


def test_robot_new_without_a_terminal_takes_the_defaults(home, builds, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["robot", "new", "a robot that picks strawberries"]) == 0
    assert "(no terminal to ask in)" in capsys.readouterr().out and len(builds) == 1


def test_robot_new_asks_the_questions_in_a_terminal(home, builds, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    answers = iter(["2", "", "y"])   # the second chip, then the default, then build it
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers, ""))
    qs = agents.run_turn(text="help me sort socks").questions   # what the CLI is about to ask
    assert len(qs) == 2 and all(len(q["chips"]) >= 2 for q in qs)
    default = next(c["value"] for c in qs[1]["chips"] if c.get("default"))
    assert cli.main(["robot", "new", "help me sort socks"]) == 0
    out = capsys.readouterr().out
    assert "1. " in out and "2. " in out and "[default]" in out and len(builds) == 1
    final = json.loads((next(home.iterdir()) / "brief.json").read_text(encoding="utf-8"))
    said = {k: v for t in final["turns"] for k, v in (t.get("answers") or {}).items()}
    assert said == {qs[0]["slot"]: qs[0]["chips"][1]["value"], qs[1]["slot"]: default}
    answers = iter(["q"])
    assert cli.main(["robot", "new", "help me sort socks"]) == 1 and len(builds) == 1   # stopped: nothing built


def test_robot_new_refuses_harm_and_builds_out_of_scope_only_when_asked(home, builds, capsys):
    assert cli.main(["robot", "new", "a robot that shoots people", "--defaults"]) == 1
    assert "Refused" in capsys.readouterr().out and builds == []
    assert cli.main(["robot", "new", "a robot that vacuums my whole house", "--defaults"]) == 1
    out = capsys.readouterr().out
    assert "out of scope" in out and "--nearest" in out and builds == []
    assert not home.exists() or list(home.iterdir()) == []      # nothing was created
    assert cli.main(["robot", "new", "a robot that vacuums my whole house", "--defaults", "--nearest"]) == 0
    assert len(builds) == 1


def test_robot_new_with_a_bare_name_still_builds_the_default_arm(home, builds):
    assert cli.main(["robot", "new", "arm-one"]) == 0
    assert projects.read_meta(str(home / "arm-one"))["origin"] == "default" and builds[-1][0].name == "arm-one"
    with pytest.raises(SystemExit, match="not both"):
        cli.main(["robot", "new", "help me sort socks", "--need", "socks"])


def test_export_writes_a_revision_or_the_history(home, builds, tmp_path, capsys):
    assert cli.main(["robot", "new", "arm-one"]) == 0
    p = RobotProject(str(home / "arm-one"))
    (home / "arm-one" / "r001" / "cad").mkdir()
    (home / "arm-one" / "r001" / "cad" / "robot.glb").write_bytes(b"glTF")
    out = tmp_path / "out"
    out.mkdir()
    assert cli.main(["export", "arm-one", "-o", str(out)]) == 0
    with zipfile.ZipFile(out / "arm-one-r001.zip") as z:
        names = set(z.namelist())
        assert {"export-manifest.json", "design.json", "results.json", "cad/robot.glb"} <= names
        assert not any("ledger" in n or "chat" in n for n in names)
    assert cli.main(["export", "arm-one", "--full", "-o", str(tmp_path / "h.zip")]) == 0
    with zipfile.ZipFile(tmp_path / "h.zip") as z:
        assert json.loads(z.read("export-manifest.json"))["kind"] == "project" and "r001/design.json" in z.namelist()
    with pytest.raises(SystemExit, match="Unknown revision"):
        cli.main(["export", "arm-one", "--rev", "r009"])
    for bad in ("nope", "../etc"):
        with pytest.raises(SystemExit):
            cli.main(["export", bad])
    assert p.revs() == ["r001"]


def test_export_from_a_team_folder_carries_the_teams_record(home, builds, tmp_path):
    from test_team_util import make_team
    assert cli.main(["robot", "new", "arm-one"]) == 0
    make_team(str(home))
    assert cli.main(["export", "arm-one", "-o", str(tmp_path / "t.zip")]) == 0
    with zipfile.ZipFile(tmp_path / "t.zip") as z:
        record = json.loads(z.read("team.json"))
    assert record and "member_id" not in json.dumps(record)


def test_world_simulates_in_a_starter_room_and_off_again(home, builds, capsys):
    assert cli.main(["robot", "new", "arm-one"]) == 0
    assert cli.main(["world", "arm-one"]) == 0 and "plain table" in capsys.readouterr().out
    assert cli.main(["world", "arm-one", "--starter", "desk", "--fast"]) == 0
    out = capsys.readouterr().out
    assert "room: Desk" in out and "simulates in desk" in out and "closest 41 mm" in out and "grey boxes" in out
    design, kw = builds[-1]
    assert design.world.id == "desk" and len(design.world.sha) == 16 and kw.get("fast_sim") is True
    assert (home / "arm-one" / "world" / "saved" / f"{design.world.sha}.json").is_file()
    assert cli.main(["world", "arm-one", "--starter", "desk"]) == 0 and "already simulates" in capsys.readouterr().out
    assert cli.main(["world", "arm-one", "--off"]) == 0 and builds[-1][0].world.id == ""
    entries = RobotProject(str(home / "arm-one")).entries()
    assert [e["room"] for e in entries if e.get("type") == "world"] == ["desk", "off"]


def test_doctor_lists_every_feature(home, capsys, monkeypatch):
    cli.cmd_doctor(argparse.Namespace(offline=True))
    out = capsys.readouterr().out
    for line in ("New robot (one-sentence brief): ready", "Import: ready", "xacro", "STEP files", "Show Morph the job: ready",
                 "Teams (preview): no team here", "World mode (beta): "):
        assert line in out, line
    from morph.studio import ffmpeg
    monkeypatch.setattr(ffmpeg, "status", lambda: {"available": False, "path": None, "version": None})
    from test_team_util import make_team
    make_team(str(home))
    cli.cmd_doctor(argparse.Namespace(offline=True))
    out = capsys.readouterr().out
    assert "no ffmpeg" in out and "-m pip install imageio-ffmpeg" in out and "Teams (preview): team" in out
    assert "run_studio.sh --team" in out or "run_studio.bat --team" in out  # the launcher of this checkout


@pytest.mark.parametrize("saved", [{"stretch_payload_g": 0.0, "stretch_holds_itself": False}, {"stretch_payload_g": -13.0}])
def test_the_terminal_never_prints_a_negative_stretched_out_payload(tmp_path, capsys, saved):
    # a revision saved before the summary clamped it holds the deficit (-13 g) itself
    p = RobotProject(str(tmp_path / "so101"))
    (tmp_path / "so101" / "r001").mkdir(parents=True)
    summary = {"stretch_reach_mm": 465.4, "stretch_limited_by": "shoulder_lift", "max_payload_g": 265.0, "issues": [], **saved}
    res = {"rev": "r001", "summary": summary, "stage_status": {n: "computed" for n in ORDER},
           **{OUTPUT_KEYS[n]: {"ok": True} for n in ORDER}}
    (tmp_path / "so101" / "r001" / "results.json").write_text(json.dumps(res), encoding="utf-8")
    cli._robot_summary(p, "r001")
    out = capsys.readouterr().out
    assert "stretch_payload_g=none" in out and "-13" not in out
    assert "stretched out at 465 mm the arm can't hold even its own weight (shoulder_lift stalls)" in out
