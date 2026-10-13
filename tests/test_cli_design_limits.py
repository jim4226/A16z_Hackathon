"""`morph robot new --need` and `morph robot change` check every value they set against the design limits before
anything is built or created: one absurd value (a kilometre-wide object) would hold the computer for hours. A change
the CLI can't read says why instead of printing a traceback. Offline; the pipeline is a stand-in."""

import json

import pytest

from morph import cli, keys
from morph.robot.pipeline2 import ORDER, OUTPUT_KEYS, RobotProject
from morph.spec import Design
from morph.studio import intent


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "HOME", str(tmp_path / "projects"))
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))  # main() must not load the developer's keys
    return tmp_path / "projects"


@pytest.fixture
def builds(monkeypatch):
    """RobotProject.run without the pipeline: records each design it was asked to build."""
    built = []

    def run(self, design, parent=None, label="", **kw):
        rev = self.claim_rev()
        with open(f"{self.rev_dir(rev)}/design.json", "w", encoding="utf-8") as fh:
            fh.write(design.dumps())
        res = {"rev": rev, "parent": parent, "label": label, "summary": {}, "stage_status": {n: "computed" for n in ORDER},
               **{OUTPUT_KEYS[n]: {"ok": True} for n in ORDER}}
        with open(f"{self.rev_dir(rev)}/results.json", "w", encoding="utf-8") as fh:
            json.dump(res, fh)
        built.append(design)
        return rev

    monkeypatch.setattr(RobotProject, "run", run)
    return built


def _exit_text(args):
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    return str(exc.value.code)


def test_robot_change_refuses_values_outside_the_design_limits(home, builds):
    assert cli.main(["robot", "new", "egg-picker"]) == 0 and len(builds) == 1
    for change in ("object.diameter_mm=1e9", "arm.reach_mm=99999", "object.mass_g=-5", "task.cycle_time_s=nan"):
        text = _exit_text(["robot", "change", "egg-picker", change])
        assert "Couldn't apply that" in text and change.split("=")[0] in text, text
    assert len(builds) == 1 and RobotProject(str(home / "egg-picker")).revs() == ["r001"]   # nothing was built
    assert cli.main(["robot", "change", "egg-picker", "arm.dof=6", "object.diameter_mm=60"]) == 0
    assert builds[-1].arm.dof == 6 and builds[-1].object.diameter_mm == 60.0


@pytest.mark.parametrize("change, says", [("foo.bar=1", "unknown parameter: foo.bar"), ("arm.dof", "dotted.key=value"),
                                          ("arm.dof=six", "arm.dof")])
def test_robot_change_explains_a_change_it_can_not_read(home, builds, change, says):
    assert cli.main(["robot", "new", "egg-picker"]) == 0
    text = _exit_text(["robot", "change", "egg-picker", change])
    assert says in text and "Traceback" not in text, text
    assert len(builds) == 1


def test_robot_new_checks_what_the_request_asks_for_before_creating_anything(home, builds, monkeypatch):
    monkeypatch.setattr(intent, "interpret", lambda msg, d, *a: {"changes": {"object.diameter_mm": 1e9}, "reply": "Huge.",
                                                                 "source": "claude"})
    text = _exit_text(["robot", "new", "big-one", "--need", "pick up a planet"])
    assert "Couldn't apply that" in text and "object.diameter_mm" in text, text
    assert builds == [] and not (home / "big-one").exists()
    monkeypatch.setattr(intent, "interpret", lambda msg, d, *a: {"changes": {"arm.dof": 6, "name": "evil", "arm.source": "x"},
                                                                 "reply": "Six axes.", "source": "rules"})
    assert cli.main(["robot", "new", "six", "--need", "six axes please"]) == 0
    assert builds[-1].arm.dof == 6 and builds[-1].name == "six" and builds[-1].arm.source == ""
    assert Design.from_dict(json.loads((home / "six" / "r001" / "design.json").read_text(encoding="utf-8"))).arm.dof == 6
