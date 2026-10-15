"""A damaged team.json (cut off, or a bad hand edit) stops the team where it is (ROB-6): every team command and the
team Studio say which line and column can't be read and what to do, nothing signs in, and `morph team init` never
writes a new team over it (it silently replaced every member, invite, token and the policy). Each change keeps the
copy before it as team.json.bak."""

import argparse
import http.client
import json
import os
import threading

import pytest

from morph import cli
from morph.studio import api_team, server
from morph.studio.teams import auth
from morph.studio.teams import cli as team_cli
from morph.studio.teams.store import TeamFileError, TeamStore
from test_studio_rooms import make_project
from test_team_util import make_team


@pytest.fixture(autouse=True)
def _fresh():
    auth.THROTTLE.clear()
    yield
    api_team.configure(home=None, enabled=False)


@pytest.fixture
def damaged(tmp_path):
    home = tmp_path / "projects"
    ids, cookies = make_team(str(home), (("Olivia", "owner", ["generalist"]), ("Ed", "editor", ["electronics"]),
                                         ("Vic", "viewer", [])))
    path = home / ".team" / "team.json"
    raw = path.read_bytes()
    path.write_bytes(raw[:len(raw) // 2])
    return home, ids, cookies, raw[:len(raw) // 2]


def run(home, cmd, **kw):
    a = argparse.Namespace(team_cmd=cmd, home=str(home), url=None, **kw)
    with pytest.raises(SystemExit) as exc:
        team_cli.run(a)
    return str(exc.value)


def test_every_team_command_names_the_damage_and_none_writes_over_it(damaged):
    home, ids, _, cut = damaged
    bak = str(home / ".team" / "team.json.bak")
    assert os.path.isfile(bak)                                         # the copy before the last change (the last join)
    for cmd, kw in (("members", {}), ("invite", {"role": "editor", "disciplines": "", "name": "Zed"}),
                    ("link", {"member": None}), ("init", {"owner": "Olivia2", "name": ""})):
        msg = run(home, cmd, **kw)
        assert "team.json can't be read at line" in msg and "column" in msg and bak in msg, (cmd, msg)
        assert "Create the team first" not in msg and "no owner" not in msg
    assert (home / ".team" / "team.json").read_bytes() == cut          # never replaced
    assert json.load(open(bak, encoding="utf-8"))["members"]           # and the backup still holds the team


def test_the_backup_is_the_team_before_the_last_change(tmp_path):
    home = str(tmp_path / "projects")
    auth.init_team("Olivia", "R Cubed", home=home)
    team = TeamStore(home).read()
    auth.create_invite(None, "editor", ["electronics"], "Ed", home=home)
    before = json.load(open(os.path.join(home, ".team", "team.json.bak"), encoding="utf-8"))
    assert before == team and len(TeamStore(home).read()["invites"]) == len(team["invites"]) + 1
    assert oct(os.stat(os.path.join(home, ".team", "team.json.bak")).st_mode & 0o777) == "0o600" or os.name == "nt"


def test_the_team_studio_refuses_to_start_with_the_reason(damaged, monkeypatch):
    home, *_ = damaged
    monkeypatch.setattr(cli, "HOME", str(home))
    make_project(home, "egg-picker")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_studio(argparse.Namespace(name="egg-picker", host="127.0.0.1", port=1, open=False, remote=False,
                                          team=True, allow_url_import=False))
    assert "team.json can't be read at line" in str(exc.value)


def test_a_running_team_studio_answers_503_with_the_reason_and_signs_nobody_in(tmp_path, capsys):
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg-picker"), str(home), team=True)
    ids, cookies = make_team(str(home), (("Olivia", "owner", ["generalist"]), ("Vic", "viewer", [])))
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(st, None, None))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    port = httpd.server_address[1]
    jar = f"{auth.cookie_name(port)}={cookies['Olivia']}"

    def get(path, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        conn.request("GET", path, headers={"Cookie": cookie} if cookie else {})
        r = conn.getresponse()
        body = r.read().decode("utf-8", "replace")
        conn.close()
        return r.status, body
    link = auth.create_link(ids["Olivia"], by=None, home=str(home))["path"]
    try:
        assert get("/api/state", jar)[0] == 200
        path = home / ".team" / "team.json"
        path.write_bytes(path.read_bytes()[:200])
        capsys.readouterr()
        for p, cookie in (("/api/state", jar), ("/", jar), ("/", None), (link, None), ("/?invite=mi_" + "Qx3" * 7 + "Q", None),
                          ("/api/team", jar)):
            status, body = get(p, cookie)
            assert status == 503 and "projects/.team/team.json can't be read" in body, (p, status, body[:300])
            assert str(tmp_path) not in body and "projects/.team/team.json.bak" in body  # never the server's own path
        out = capsys.readouterr().out                                    # the terminal has the full path, once
        assert out.count(f"{path} can't be read") == 1
    finally:
        httpd.shutdown()
    with pytest.raises(TeamFileError):
        auth.verify_session(cookies["Olivia"], home=str(home))
