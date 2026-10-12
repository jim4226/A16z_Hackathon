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
    from morph.spec import Design

    (root / rev).mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot"}))
    res = {"parent": None, "label": "", "provenance": provenance or {}, "timings_s": {}}
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


@pytest.fixture
def studio_server(tmp_path, env):
    from morph.robot.pipeline2 import RobotProject
    from morph.studio import server

    egg = tmp_path / "projects" / "egg-picker"
    _rev(egg, "r001")
    st = server.Studio(RobotProject(str(egg)), str(tmp_path / "projects"))
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(st, None))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(st, {f"127.0.0.1:{port}", f"localhost:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        conn.request(method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})})
        r = conn.getresponse()
        out = r.status, r.read()
        conn.close()
        return out

    yield call, port
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
