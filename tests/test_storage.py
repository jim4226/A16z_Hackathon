"""Project storage: revisions in numeric order, exclusive revision numbers, UTC ledger times, and the pipeline
version that decides when the demo robots rebuild."""

import os
import time

import pytest

from morph.pipeline import Project, ledger_time
from morph.robot import pipeline2
from morph.robot.pipeline2 import RobotProject, pipeline_version


def test_revisions_sort_by_number(tmp_path):
    p = Project(str(tmp_path / "p"))
    for name in ("r002", "r999", "r1000", "r010", "r001.deleted-123", "r003.incomplete-5", "rx1", "r", ".r004"):
        os.mkdir(os.path.join(p.root, name))
    (tmp_path / "p" / "r5000").write_text("a file, not a revision", encoding="utf-8")
    assert p.revs() == ["r002", "r010", "r999", "r1000"]
    assert p.latest() == "r1000"
    assert p.claim_rev() == "r1001" and os.path.isdir(os.path.join(p.root, "r1001"))


def test_revision_numbers_are_never_reused_or_shared(tmp_path, monkeypatch):
    p = Project(str(tmp_path / "p"))
    assert p.claim_rev() == "r001"
    os.mkdir(os.path.join(p.root, "r003"))       # r002 was set aside or deleted: the next number follows r003
    assert p.claim_rev() == "r004"
    stale = p.revs()
    real = Project.revs
    calls = []

    def racing(self):                             # another process claims r005 between our listing and mkdir
        calls.append(1)
        if len(calls) == 1:
            os.mkdir(os.path.join(self.root, "r005"))
            return stale
        return real(self)

    monkeypatch.setattr(Project, "revs", racing)
    assert p.claim_rev() == "r006"


def test_deleted_project_has_no_revisions(tmp_path):
    p = Project(str(tmp_path / "gone"))
    os.rmdir(p.root)
    assert p.revs() == [] and p.latest() is None


def test_ledger_times_are_utc_and_old_ones_still_parse(tmp_path):
    p = Project(str(tmp_path / "p"))
    p.log({"type": "tool_time", "cmd": "test", "seconds": 1})
    ts = p.entries()[-1]["ts"]
    assert ts.endswith("Z") and abs(ledger_time(ts) - time.time()) < 5
    assert ledger_time("2026-10-12T19:05:00Z") == 1791831900.0
    local = time.mktime(time.strptime("2026-10-12T19:05:00", "%Y-%m-%dT%H:%M:%S"))
    assert ledger_time("2026-10-12T19:05:00") == local  # before 0.2: local time, no zone
    for bad in (None, "", "yesterday", 5, "2026-13-40T99:00:00Z"):
        assert ledger_time(bad) is None


def test_pipeline_version_hashes_only_pipeline_code():
    names = {os.path.relpath(f, os.path.dirname(os.path.dirname(pipeline2.__file__))) for f in pipeline2._code_files()}
    names = {n.replace(os.sep, "/") for n in names}
    assert {"robot/pipeline2.py", "robot/arm.py", "robot/imported.py", "spec.py", "pipeline.py", "electronics.py"} <= names
    for not_pipeline in ("cli.py", "keys.py", "keewano.py", "settings.py", "resources.py", "darwin.py", "__init__.py"):
        assert not_pipeline not in names
    assert not any(n.startswith("studio/") for n in names)
    assert pipeline_version() == pipeline_version() and len(pipeline_version()) == 12


def test_imported_robots_hash_their_bundle(tmp_path):
    bundle = tmp_path / "morph-home" / ".robots" / "testbot"   # MORPH_HOME is tmp_path/morph-home in every test
    (bundle / "meshes").mkdir(parents=True)
    (bundle / "morph_profile.json").write_text("{}", encoding="utf-8")
    mesh = bundle / "meshes" / "link.stl"
    mesh.write_bytes(b"solid a")
    code = pipeline_version()
    first = pipeline_version("testbot")
    assert first != code and pipeline_version("testbot") == first
    mesh.write_bytes(b"solid b, edited")
    assert pipeline_version("testbot") not in (first, code)
    assert pipeline_version("so101") not in (code, first)
    assert pipeline_version("no-such-robot") == pipeline_version("other-missing-robot") != code


def test_robot_runs_record_who_asked(tmp_path, monkeypatch):
    p = RobotProject(str(tmp_path / "egg"))
    monkeypatch.setattr(RobotProject, "_compute", lambda self, *a, **k: False)  # every stage "pending": instant
    rev = p.run(pipeline2.Design(), label="first", verbose=False, by="m-ana")
    assert rev == "r001" and p.load_results(rev)["by"] == "m-ana"
    assert p.load_results(rev)["pipeline_version"] == pipeline_version()
    entry = [e for e in p.entries() if e["type"] == "revision"][-1]
    assert entry["by"] == "m-ana" and entry["ts"].endswith("Z")
    rev2 = p.run(pipeline2.Design(), parent=rev, verbose=False)
    assert rev2 == "r002" and "by" not in p.load_results(rev2)


@pytest.mark.parametrize("bad", ["r" + "9" * 12, "r-1", "R001", "r001 ", "r０01"])
def test_odd_folder_names_are_not_revisions(tmp_path, bad):
    p = Project(str(tmp_path / "p"))
    try:
        os.mkdir(os.path.join(p.root, bad))
    except OSError:
        pytest.skip("this file system refuses the name")
    assert p.revs() == []
