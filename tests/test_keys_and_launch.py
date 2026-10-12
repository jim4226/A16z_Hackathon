"""Launching on a new computer: API keys from the Studio, the server's request checks, `morph doctor`."""

import argparse
import http.client
import json
import os
import socket
import sys
import threading
import types

import pytest

from morph import cli, keewano, keys

NAMES = ["ANTHROPIC_API_KEY", "KEEWANO_API_KEY", "KEEWANO_PAT", "KEEWANO_PROJECT_ID"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """No keys in the environment, and .env in a temp folder (restored after the test)."""
    for k in NAMES:
        monkeypatch.delenv(k, raising=False)
    path = tmp_path / ".env"
    monkeypatch.setattr(keys, "ENV_PATH", str(path))
    return path


def _rev(root, rev, provenance=None, errors=None):
    from morph.robot.pipeline2 import pipeline_version
    from morph.spec import Design

    (root / rev).mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot"}))
    res = {"parent": None, "label": "", "provenance": provenance or {}, "timings_s": {}, "pipeline_version": pipeline_version()}
    if errors:
        res["errors"] = errors
    (root / rev / "results.json").write_text(json.dumps(res))
    (root / rev / "design.json").write_text(json.dumps(Design().to_dict()))


def test_saving_keys_keeps_the_rest_of_the_file(env):
    env.write_text("# my notes\nANTHROPIC_API_KEY=\nKEEWANO_PAT=old\nKEEWANO_PAT=duplicate\nOTHER_TOOL=1\n", encoding="utf-8")
    assert keys.save({"ANTHROPIC_API_KEY": " sk-ant-test ", "KEEWANO_PAT": "kwpat-new"}) == ["ANTHROPIC_API_KEY", "KEEWANO_PAT"]
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines == ["# my notes", "ANTHROPIC_API_KEY=sk-ant-test", "KEEWANO_PAT=kwpat-new", "OTHER_TOOL=1"]
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test" and os.environ["KEEWANO_PAT"] == "kwpat-new"
    assert not os.path.exists(str(env) + ".tmp")
    status = keys.status()
    assert {k["name"]: k["set"] for k in status} == {"ANTHROPIC_API_KEY": True, "KEEWANO_API_KEY": False,
                                                    "KEEWANO_PAT": True, "KEEWANO_PROJECT_ID": False}
    assert "sk-ant-test" not in json.dumps(status) and "kwpat-new" not in json.dumps(status)  # never the values
    assert keys.save({"KEEWANO_PAT": ""}) == ["KEEWANO_PAT"]  # Remove
    assert "KEEWANO_PAT" not in os.environ and "KEEWANO_PAT=" in env.read_text(encoding="utf-8").splitlines()
    assert dict(keys.parse(env.read_text(encoding="utf-8")))["ANTHROPIC_API_KEY"] == "sk-ant-test"


def test_saving_keys_rejects_what_is_not_a_key(env):
    env.write_text("KEEWANO_PAT=keep\n", encoding="utf-8")
    for bad in ({"OPENAI_API_KEY": "x"}, {"ANTHROPIC_API_KEY": "a\nKEEWANO_PAT=evil"}, {"ANTHROPIC_API_KEY": 'a"b'},
                {"ANTHROPIC_API_KEY": "x" * 501}):
        with pytest.raises(ValueError):
            keys.save(bad)
    assert env.read_text(encoding="utf-8") == "KEEWANO_PAT=keep\n" and "ANTHROPIC_API_KEY" not in os.environ


def test_first_launch_creates_env_from_the_template(env, tmp_path, monkeypatch):
    template = tmp_path / "env.example"
    template.write_text("# Claude\nANTHROPIC_API_KEY=\n", encoding="utf-8")
    monkeypatch.setattr(keys, "TEMPLATE", str(template))
    assert keys.ensure_env_file() and not keys.ensure_env_file()  # once only
    assert env.read_text(encoding="utf-8") == "# Claude\nANTHROPIC_API_KEY=\n"
    env.unlink()
    keys.save({"KEEWANO_PROJECT_ID": "p1"})  # no .env yet: start from the template
    assert env.read_text(encoding="utf-8") == "# Claude\nANTHROPIC_API_KEY=\nKEEWANO_PROJECT_ID=p1\n"


def test_key_check_reports_a_rejected_key_without_raising(env, monkeypatch):
    assert keys.check() == {}  # nothing set: nothing to try

    class AuthenticationError(Exception):
        status_code = 401

    class Models:
        def list(self, **kw):
            raise AuthenticationError("invalid x-api-key")

    fake = types.SimpleNamespace(Anthropic=lambda **kw: types.SimpleNamespace(models=Models()))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setattr(keewano, "query", lambda sql, max_rows=100: [[42]])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-wrong")
    monkeypatch.setenv("KEEWANO_PAT", "kwpat-x")
    monkeypatch.setenv("KEEWANO_PROJECT_ID", "p1")
    out = keys.check()
    assert out["Claude"] == {"ok": False, "detail": "Anthropic rejected this key: copy it again from console.anthropic.com"}
    assert out["Keewano"]["ok"] and "42 events" in out["Keewano"]["detail"]
    assert list(keys.check(["Keewano"])) == ["Keewano"]
    monkeypatch.setattr(keewano, "query", lambda sql, max_rows=100: None)
    assert not keys.check(["Keewano"])["Keewano"]["ok"]


def _start(tmp_path, token=None):
    """A real Studio server on a free port: local (Host allowlist) or remote (access token)."""
    from morph.robot.pipeline2 import RobotProject
    from morph.studio import server

    egg = tmp_path / "projects" / "egg-picker"
    if not egg.exists():
        _rev(egg, "r001")
    st = server.Studio(RobotProject(str(egg)), str(tmp_path / "projects"))
    st.remote = bool(token)
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(st, None, token))
    port = httpd.server_address[1]
    if not token:
        httpd.RequestHandlerClass = server.make_handler(st, {f"127.0.0.1:{port}", f"localhost:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        conn.request(method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})})
        r = conn.getresponse()
        out = r.status, r.read(), dict(r.getheaders())
        conn.close()
        return out

    return call, port, httpd


@pytest.fixture
def studio_server(tmp_path, env):
    call, port, httpd = _start(tmp_path)
    yield (lambda *a, **k: call(*a, **k)[:2]), port
    httpd.shutdown()
    httpd.server_close()


def test_studio_api_only_answers_its_own_page(studio_server, env):
    call, port = studio_server
    assert call("GET", "/api/keys")[0] == 200
    assert call("GET", "/api/keys", headers={"Host": f"localhost:{port}"})[0] == 200
    assert call("GET", "/api/keys", headers={"Host": f"attacker.example:{port}"})[0] == 403  # DNS rebinding
    code, _ = call("POST", "/api/keys", {"values": {"ANTHROPIC_API_KEY": "sk-ant-x"}},
                   headers={"Origin": "http://attacker.example"})
    assert code == 403 and not env.exists() and "ANTHROPIC_API_KEY" not in os.environ
    assert call("POST", "/api/reset", {}, headers={"Origin": "null"})[0] == 403
    assert call("POST", "/api/keys", b"not json")[0] == 400
    assert call("POST", "/api/keys", [1, 2])[0] == 400


def test_keys_from_the_studio_dialog(studio_server, env, monkeypatch):
    call, port = studio_server
    code, body = call("POST", "/api/keys", {"values": {"ANTHROPIC_API_KEY": "sk-ant-dialog"}},
                      headers={"Origin": f"http://127.0.0.1:{port}"})
    out = json.loads(body)
    assert code == 200 and out["ok"] and out["changed"] == ["ANTHROPIC_API_KEY"]
    assert out["integrations"]["claude"]["chat"] and b"sk-ant-dialog" not in body
    assert "ANTHROPIC_API_KEY=sk-ant-dialog" in env.read_text(encoding="utf-8")
    code, body = call("GET", "/api/keys")
    got = json.loads(body)
    assert got["file"] == ".env" and b"sk-ant-dialog" not in body
    assert {k["name"]: k["set"] for k in got["keys"]}["ANTHROPIC_API_KEY"]
    code, body = call("POST", "/api/keys", {"values": {"NOT_A_KEY": "1"}})
    assert code == 400 and "unknown key" in json.loads(body)["error"]
    monkeypatch.setattr(keys, "check", lambda services=None: {"Claude": {"ok": True, "detail": f"checked {services}"}})
    code, body = call("POST", "/api/keys/check", {"services": ["Claude"]})
    assert code == 200 and json.loads(body)["checks"]["Claude"]["detail"] == "checked ['Claude']"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_studio_explains_a_gripper_only_project(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "HOME", str(tmp_path))
    (tmp_path / "raw-egg").mkdir()
    (tmp_path / "raw-egg" / "meta.json").write_text(json.dumps({"need": "pick up a raw egg"}))
    a = argparse.Namespace(name="raw-egg", host="127.0.0.1", port=_free_port(), open=False)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_studio(a)
    assert "gripper-only project" in str(exc.value) and "report.html" in str(exc.value)


def test_doctor_names_the_failed_stage_and_never_prints_keys(tmp_path, env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "HOME", str(tmp_path / "projects"))
    monkeypatch.setattr(keys, "TEMPLATE", str(tmp_path / "no-template"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
    _rev(tmp_path / "projects" / "egg-picker", "r001", provenance={"cad": "built", "electronics": "error"},
         errors={"electronics": "UnicodeEncodeError: 'charmap' codec can't encode character"})
    cli.cmd_doctor(argparse.Namespace(offline=True))
    out = capsys.readouterr().out
    assert "sk-ant-secret-value" not in out
    assert "Anthropic API key is set" in out and "Darwin: no key needed" in out
    assert "egg-picker r001: electronics error (UnicodeEncodeError" in out
    assert "so101: not built yet" in out
    assert out.rstrip().endswith("Fix the lines marked FIX, then run this again.")


def test_only_a_studio_other_computers_can_reach_needs_a_token():
    from morph.studio.server import access_token

    assert access_token("127.0.0.1") is None and access_token("localhost") is None
    assert access_token("127.0.0.1", "fixed-code-1") is None  # local Studio: never a sign-in page
    t = access_token("0.0.0.0")
    assert len(t) >= 16 and t != access_token("0.0.0.0")      # a fresh code each start
    assert access_token("192.168.1.20", "fixed-code-1") == "fixed-code-1"


def test_remote_studio_only_serves_browsers_with_its_access_link(tmp_path, env):
    call, port, httpd = _start(tmp_path, token="test-code-0123456")
    try:
        code, body, _ = call("GET", "/")
        assert code == 401 and b"Access code" in body              # sign-in page, not the app
        assert call("GET", "/api/state")[0] == 401
        assert call("GET", "/static/app.js")[0] == 401
        assert call("POST", "/api/keys", {"values": {"ANTHROPIC_API_KEY": "sk-ant-x"}})[0] == 401
        assert not env.exists() and "ANTHROPIC_API_KEY" not in os.environ
        code, body, _ = call("GET", "/?token=wrong-code")
        assert code == 401 and b"did not match" in body
        code, _, headers = call("GET", "/?token=test-code-0123456&tour=0")
        assert code == 302 and headers["Location"] == "/?tour=0"   # the code leaves the address bar
        cookie = headers["Set-Cookie"]
        assert cookie.startswith(f"morph_access_{port}=test-code-0123456;") and "HttpOnly" in cookie and "SameSite=Lax" in cookie
        signed_in = {"Cookie": cookie.split(";")[0], "Host": f"gpu-box.local:{port}"}  # any name the laptop uses
        code, body, _ = call("GET", "/api/state", headers=signed_in)
        assert code == 200 and json.loads(body)["compute"]["remote"] is True
        assert call("GET", "/", headers=signed_in)[0] == 200
        assert call("GET", "/api/keys", headers={"X-Morph-Token": "test-code-0123456"})[0] == 200  # scripts
        code, body, _ = call("POST", "/api/keys", {"values": {"KEEWANO_PROJECT_ID": "p1"}},
                             headers={**signed_in, "Origin": f"https://gpu-box.local:{port}"})  # behind an HTTPS proxy
        assert code == 200 and json.loads(body)["changed"] == ["KEEWANO_PROJECT_ID"]
        assert call("POST", "/api/reset", {}, headers={**signed_in, "Origin": "http://attacker.example"})[0] == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def _robots(home):
    for name in ("egg-picker", "so101"):
        _rev(home / name, "r001")


def test_remote_flag_opens_the_studio_to_other_computers(tmp_path, env, monkeypatch):
    from morph.studio import server

    _robots(tmp_path / "projects")
    monkeypatch.setattr(cli, "HOME", str(tmp_path / "projects"))
    got = {}
    monkeypatch.setattr(server, "serve", lambda p, host, port, home=None, token=None: got.update(host=host, token=token))
    monkeypatch.delenv("MORPH_TOKEN", raising=False)
    a = lambda **k: argparse.Namespace(**{"name": "egg-picker", "host": "127.0.0.1", "port": _free_port(), "open": False,
                                          "remote": False, **k})
    cli.cmd_studio(a())
    assert got == {"host": "127.0.0.1", "token": None}               # the default stays local-only
    cli.cmd_studio(a(remote=True))
    assert got["host"] == "0.0.0.0" and len(got["token"]) >= 16
    monkeypatch.setenv("MORPH_TOKEN", "team-code-2026")
    cli.cmd_studio(a(remote=True))
    assert got["token"] == "team-code-2026"                         # same link after a restart
    monkeypatch.setenv("MORPH_TOKEN", "a b;c")
    with pytest.raises(SystemExit):
        cli.cmd_studio(a(remote=True))
