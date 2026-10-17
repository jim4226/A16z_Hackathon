"""A world is named by its id and pinned by its hash, never by a path or a file name. Hostile ids, hashes and file
names go through every route that can carry one: the world routes, Parameters, chat, new projects, forks, Morph-export
imports, Teams change requests and the simulation stage itself. Each is refused, or reduced to a plain id, and nothing
is read or written outside the project. Builds are stubbed (every stage "pending"), so these take seconds; offline.
The imported export's first build is also checked against the moment Windows refuses to open a file just renamed."""

import io
import json
import os
import threading
import zipfile
from urllib.parse import quote

import pytest

from morph.cli import EGG_ROBOT
from morph.robot.pipeline2 import RobotProject
from morph.services import durable
from morph.spec import Design
from morph.studio import api_team, server, worlds
from morph.studio.security import DESIGN_TEXT
from morph.studio.teams import activity, auth
from test_studio_features import caller, start, until
from test_studio_security import make_project
from test_team_util import make_team
from test_world_schema import _glb, _tri_doc

IDS = ["../../etc/passwd", "..", "/etc/passwd", "C:\\Windows\\win.ini", "..\\..\\x", "%2e%2e%2fx", "desk/../../x",
       "desk\x00.json", "CON", "aux", "\u202edesk", "a" * 300, "Desk", " desk", "~/.ssh/id_rsa", "file:///etc/passwd",
       "world.json", "saved/0123456789abcdef", "room.glb", ""]
BAD_IDS = [v for v in IDS if v and not DESIGN_TEXT["world.id"].fullmatch(v)]
SHAS = ["../../etc/passwd", "0123456789abcdef/../../x", "ZZZZ", "0123456789abcdeg", "0" * 65, "\\x"]
FILES = ["../world.json", "..%2fmeta.json", "room.glb/../../meta.json", "/etc/passwd", "C:\\x", "room.glb\x00",
         "world/room.glb", "world.json", "saved/x.json", "ROOM.GLB", "room.glb "]
NAMES = ["../../../pwned.glb", "..\\..\\pwned.glb", "/tmp/pwned.glb", "C:\\pwned.glb", "pwned\x00.glb", "%2e%2e/x.glb",
         "a" * 400 + ".glb", "\u202ebl g.glb"]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(RobotProject, "_compute", lambda self, *a, **k: False)
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    auth.THROTTLE.clear()
    activity.PRESENCE.seen.clear()
    yield
    api_team.configure(home=None, enabled=False)


@pytest.fixture
def studio(tmp_path):
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg"), str(home))
    httpd = start(st)
    yield st, caller(httpd), home
    httpd.shutdown()


def tree(path) -> set:
    out = set()
    for d, _, names in os.walk(path):
        out.update(os.path.relpath(os.path.join(d, n), path).replace(os.sep, "/") for n in names)
    return out


def idle(st, name="egg"):
    until(lambda: not st.room(name).jobs.busy())


def test_world_routes_reduce_hostile_ids_to_plain_ids_inside_the_project(studio, tmp_path):
    st, call, home = studio
    (tmp_path / "outside.txt").write_text("private", encoding="utf-8")
    before = tree(tmp_path)
    desk = worlds.starter("desk")
    saved = set()
    for bad in IDS + [None, 5, {"$ne": 1}, ["desk"]]:
        s, out, _ = call("POST", "/api/world", {"world": {**desk, "id": bad}})
        assert s in (200, 202), (bad, out)
        wid = out["world"]["id"]
        assert worlds.SLUG.fullmatch(wid) and not any(c in wid for c in "/\\.:%~\x00"), (bad, wid)
        saved.add(out["sha"])
        idle(st)                                     # one at a time: the room's queue holds ten
    for bad in IDS + [None, 5]:
        assert call("POST", "/api/world", {"starter": bad})[0] == 400, bad
    for bad in FILES:
        for q in {quote(bad, safe=""), quote(bad, safe="/%")}:
            s, out, _ = call("GET", f"/api/world?file={q}")
            assert s == 404 and out["ok"] is False, bad
    for bad in ("../egg", "..%2fegg", "/tmp", "egg/../egg", "EGG", "..\\egg"):
        assert call("GET", f"/api/world?project={quote(bad, safe='%/')}")[0] in (400, 404), bad
    for bad in ("../r001", "r001/../../x", "/r001", "r999", 5, ["r001"]):
        s, out, _ = call("POST", "/api/world/inspect", {"rev": bad})
        assert s == 404 and "revision" in out["error"], bad
    room = st.room("egg")
    assert len(room.p.revs()) > 1
    # every design the routes made names a plain id and a hash; the files are world/world.json and world/saved/<sha>
    for rev in room.p.revs():
        d = room.p.load_design(rev)
        assert DESIGN_TEXT["world.id"].fullmatch(d.world.id) and DESIGN_TEXT["world.sha"].fullmatch(d.world.sha)
    assert tree(home / "egg" / "world") == {"world.json"} | {f"saved/{h}.json" for h in saved}
    new = {p for p in tree(tmp_path) - before if not p.startswith(("projects/egg/", "morph-home/"))}
    assert new == set(), new
    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "private"


def test_an_uploaded_room_is_always_world_room_glb(studio, tmp_path):
    st, call, home = studio
    data = _glb(_tri_doc(), b"\0" * 36)
    before = tree(tmp_path)
    for name in NAMES:
        s, out, _ = call("PUT", f"/api/world/upload?name={quote(name, safe='')}", raw=data, ctype="application/octet-stream")
        assert s == 200, (name, out)
        d = out["draft"]
        assert d["id"] == "room" and d["asset"] == "room.glb" and len(d["name"]) <= 60
        assert not any(c in d["name"] for c in "/\\\x00"), d["name"]
        assert out["asset_url"] == f"/api/world?project=egg&file=room.glb&v={d['asset_sha']}"
    assert tree(home / "egg" / "world") == {"room.glb"}
    new = {p for p in tree(tmp_path) - before if not p.startswith("morph-home/")}
    assert "projects/egg/world/room.glb" in new and all(p.startswith("projects/egg/") for p in new), new
    assert not any("pwned" in p for p in tree(tmp_path))
    s, out, _ = call("POST", "/api/world/obstacles/auto",
                     {"world": {**worlds.starter("desk"), "id": "../../x", "asset": "room.glb"}})
    assert s in (200, 400) and tree(home / "egg" / "world") == {"room.glb"}      # proposals only: nothing saved


def test_parameters_and_chat_never_set_the_world(studio, monkeypatch):
    st, call, home = studio
    for changes in ({"world.id": "../../etc"}, {"world.sha": "../x"}, {"world.id": "desk"}, {"world.sha": "0123456789abcdef"}):
        s, out, _ = call("POST", "/api/params", {"changes": changes})
        assert s == 400 and "not an editable parameter" in out["error"], changes
    monkeypatch.setattr(server, "interpret", lambda *a: {"changes": {"world.id": "../../etc", "world.sha": "zz"},
                                                         "reply": "Done.", "source": "claude"})
    monkeypatch.setattr(RobotProject, "run", lambda *a, **k: pytest.fail("must not build"))
    sent = []
    monkeypatch.setattr(st.hub, "send", sent.append)
    st.chat("simulate it in ../../etc")
    assert sent[-1]["type"] == "assistant" and sent[-1]["noop"] is True
    assert st.p.load_design("r001").world.id == ""


@pytest.mark.parametrize("key,value", [("id", v) for v in BAD_IDS] + [("sha", v) for v in SHAS])
def test_a_new_project_or_a_fork_naming_a_hostile_world_is_refused(studio, key, value):
    st, call, home = studio
    design = Design().with_changes({**EGG_ROBOT, "name": "other", f"world.{key}": value}).to_dict()
    s, out, _ = call("POST", "/api/projects", {"from": "design", "name": "other", "design": design})
    assert s == 400 and f"world.{key}" in out["error"] and "never a path" in out["error"], out
    assert not (home / "other").exists()
    # a design.json edited by hand to name one: the fork is refused and nothing is created
    p = home / "egg" / "r001" / "design.json"
    keep = p.read_text(encoding="utf-8")
    p.write_text(json.dumps(design), encoding="utf-8")
    try:
        s, out, _ = call("POST", "/api/projects/fork", {"project": "egg", "name": "egg-copy"})
        assert s == 400 and "never a path" in out["error"], out
        assert not (home / "egg-copy").exists()
    finally:
        p.write_text(keep, encoding="utf-8")


def test_a_fork_takes_the_room_along_but_never_a_link(studio, tmp_path):
    st, call, home = studio
    s, out, _ = call("POST", "/api/world", {"starter": "desk"})
    assert s == 202
    idle(st)
    room = st.room("egg")
    assert room.latest() == "r002"
    (tmp_path / "secret.json").write_text('{"private": true}', encoding="utf-8")
    try:
        os.symlink(tmp_path / "secret.json", home / "egg" / "world" / "saved" / "link.json")
    except (OSError, NotImplementedError):
        pytest.skip("this system can't make links")
    s, out, _ = call("POST", "/api/projects/fork", {"project": "egg", "rev": "r002", "name": "egg-copy"})
    assert s in (201, 202), out
    copied = tree(home / "egg-copy" / "world")
    assert copied == {"world.json", f"saved/{room.p.load_design('r002').world.sha}.json"}, copied


def _export_zip(design: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("export-manifest.json", json.dumps({"format_version": 1, "project": "egg-export", "revision": "r003"}))
        zf.writestr("design.json", json.dumps(design))
        zf.writestr("results.json", json.dumps({"summary": {}}))
        zf.writestr("sim/world.json", json.dumps({"id": "../../etc", "obstacles": []}))
    return buf.getvalue()


@pytest.mark.parametrize("wid,sha", [("desk", "0123456789abcdef"), ("../../etc/passwd", "../../x"), ("..", "")])
def test_an_imported_export_is_simulated_on_the_table(studio, wid, sha):
    """Exports carry a revision's files, not the project's world/ folder: the import turns world mode off (and says
    so) instead of failing its simulation stage, whatever the export's design names."""
    st, call, home = studio
    design = Design().with_changes({**EGG_ROBOT, "name": "egg-export", "world.id": wid, "world.sha": sha}).to_dict()
    sid = call("POST", "/api/import", {})[1]["id"]
    s, out, _ = call("PUT", f"/api/import/{sid}/file?path=egg-export.zip", raw=_export_zip(design),
                     ctype="application/octet-stream")
    assert s == 200, out
    s, det, _ = call("POST", f"/api/import/{sid}/detect", {})
    assert s == 200 and det["candidates"][0]["kind"] == "project", det
    s, out, _ = call("POST", f"/api/import/{sid}/commit", {"candidate": 0})
    assert s == 202, out
    assert out["changes"]["world.id"] == "" and any("World mode room" in w for w in out["warnings"]), out
    room = st.room(out["project"])
    rev = until(lambda: room.latest())
    d = room.p.load_design(rev)
    assert d.world.id == "" and d.world.sha == ""
    assert not os.path.exists(os.path.join(room.p.root, "world"))


def test_an_imported_projects_first_build_shows_when_windows_held_its_results_for_a_moment(studio, monkeypatch):
    """Windows keeps a file os.replace has just renamed busy for a moment and refuses to open it meanwhile (this
    timed out on Windows CI). A tab, or a test, that reads the new results.json in that moment must not hide the
    revision for good: room.latest() returns it once the file reads."""
    st, call, _ = studio
    held, refused, real_replace, real_open = set(), [], os.replace, open
    target = os.path.join("egg-export", "r001", "results.json")

    def replace(src, dst, *a, **k):
        if not os.fspath(dst).endswith(target):
            return real_replace(src, dst, *a, **k)
        held.add(os.fspath(dst))                 # Windows keeps the file busy through the rename ...
        try:
            real_replace(src, dst, *a, **k)
            reader = threading.Thread(target=durable.readable_json, args=(os.fspath(dst),))
            reader.start()                       # ... and another thread reads the new name before it lets go
            reader.join()
        finally:
            held.discard(os.fspath(dst))

    def opener(path, *a, **k):
        if os.fspath(path) in held:
            refused.append(path)
            raise PermissionError(13, "The process cannot access the file because it is being used by another "
                                      "process", path)
        return real_open(path, *a, **k)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(durable, "open", opener, raising=False)
    design = Design().with_changes({**EGG_ROBOT, "name": "egg-export"}).to_dict()
    sid = call("POST", "/api/import", {})[1]["id"]
    s, out, _ = call("PUT", f"/api/import/{sid}/file?path=egg-export.zip", raw=_export_zip(design),
                     ctype="application/octet-stream")
    assert s == 200, out
    assert call("POST", f"/api/import/{sid}/detect", {})[0] == 200
    s, out, _ = call("POST", f"/api/import/{sid}/commit", {"candidate": 0})
    assert s == 202, out
    room = st.room(out["project"])
    assert until(lambda: room.latest()) == "r001" and refused


@pytest.fixture
def team_studio(tmp_path):
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg"), str(home), team=True)
    ids, cookies = make_team(str(home), (("Jim", "owner", ["generalist"]), ("Ana", "editor", ["electronics"]),
                                         ("Vic", "viewer", [])))
    httpd = start(st, token="team-code-ignored")
    port = httpd.server_address[1]
    jar = {n: f"{auth.cookie_name(port)}={c}" for n, c in cookies.items()}
    yield st, caller(httpd), jar
    httpd.shutdown()


def test_a_team_change_request_names_a_world_by_id_and_hash_only(team_studio):
    st, call, jar = team_studio
    vic = {"Cookie": jar["Vic"]}
    for key, value in [("world.id", v) for v in BAD_IDS] + [("world.sha", v) for v in SHAS] + [("world.id", 5), ("world.sha", None)]:
        s, out, _ = call("POST", "/api/proposals", {"title": "Room", "changes": {key: value}}, headers=vic)
        assert s == 400 and key in out["error"], (key, value, out)
    good = {"world.id": "desk", "world.sha": "0123456789abcdef"}
    s, out, _ = call("POST", "/api/proposals", {"title": "Desk", "changes": good}, headers=vic)
    assert s == 200 and out["proposal"]["changes"] == good


@pytest.mark.parametrize("wid", [v for v in IDS if v and not worlds.SLUG.fullmatch(v)])
def test_the_simulation_stage_never_opens_a_path_from_the_design(tmp_path, wid):
    """A design.json edited by hand (or from an older Morph) reaches the sim stage as it is: the stage takes world.id as
    an id or fails, and reads only world/world.json or world/saved/<16 hex>.json inside the project."""
    p = RobotProject(str(tmp_path / "egg"))
    os.makedirs(p.root, exist_ok=True)
    nd = tmp_path / "sim"
    nd.mkdir()
    with pytest.raises(RuntimeError, match="never a path"):
        p._sim_world(Design().with_changes({"world.id": wid, "world.sha": ""}), str(nd))
    assert os.listdir(nd) == []


def test_the_simulation_stage_reads_only_the_projects_own_world_files(tmp_path):
    p = RobotProject(str(tmp_path / "egg"))
    os.makedirs(p.root, exist_ok=True)
    nd = tmp_path / "sim"
    nd.mkdir()
    w, h = worlds.save(p.root, worlds.starter("desk"))
    for sha in SHAS:            # not a hash, so never a file name: world.json (the same id) is what it simulates in
        sw = p._sim_world(Design().with_changes({"world.id": "desk", "world.sha": sha}), str(nd))
        assert sw["id"] == "desk" and sw["sha"] == h and sw["design_sha"] == sha
    with pytest.raises(RuntimeError, match="isn't this project's world"):
        p._sim_world(Design().with_changes({"world.id": "counter", "world.sha": h}), str(nd))
    with pytest.raises(RuntimeError, match="isn't this project's world"):     # a hash with no saved copy is never
        p._sim_world(Design().with_changes({"world.id": "desk", "world.sha": "0123456789abcdef"}), str(nd))   # world.json
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(worlds.starter("desk")), encoding="utf-8")
    os.remove(worlds.world_path(p.root))
    try:
        os.symlink(outside, worlds.world_path(p.root))
    except (OSError, NotImplementedError):
        pytest.skip("this system can't make links")
    with pytest.raises(RuntimeError, match="inside the project folder"):
        p._sim_world(Design().with_changes({"world.id": "desk", "world.sha": "f" * 16}), str(nd))
