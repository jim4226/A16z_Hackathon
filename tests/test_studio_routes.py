"""The route table: every API request goes through one dispatcher with the access checks, the X-Morph-Client header
on changes, a per-route body cap, the role hook Teams installs, exact and prefix paths, and streamed raw bodies."""

import http.client
import json
import os
import socket
import threading

import pytest

from morph import keys
from morph.robot.pipeline2 import RobotProject
from morph.spec import Design
from morph.studio import routes, server
from morph.studio.routes import HTTPError, route

SEEN = []


@route("GET", "/api/test-echo", role="viewer")
def _echo(req):
    SEEN.append(("echo", req.member, req.body))
    return 200, {"member": req.member, "room": req.room.name, "q": req.arg("q")}


@route("POST", "/api/test-echo")
def _echo_post(req):
    SEEN.append(("post", req.member, req.body))
    return 201, {"body": req.body}


@route("DELETE", "/api/test-echo", role="owner")
def _echo_delete(req):
    return 200, {"deleted": req.arg("project"), "body": req.body}


@route("GET", "/api/test-prefix/*", role="viewer")
def _prefix(req):
    return 200, {"route": "prefix", "path": req.path}


@route("GET", "/api/test-prefix/deeper/*", role="viewer")
def _deeper(req):
    return 200, {"route": "deeper", "path": req.path}


@route("GET", "/api/test-prefix/exact", role="viewer")
def _exact(req):
    return 200, b"plain text", "text/plain; charset=utf-8"


@route("GET", "/api/test-raw", role="viewer")
def _raw(req):
    return 200, b"\x00\x01binary", "application/octet-stream"


@route("PUT", "/api/test-upload", raw_body=True, max_body=64 << 10)
def _upload(req):
    path = os.path.join(req.studio.home, ".upload.bin")
    if req.arg("mode") == "chunks":
        return 200, {"chunks": [len(c) for c in req.stream(chunk=10_000)]}
    size = req.save(path)
    with open(path, "rb") as fh:
        data = fh.read()
    os.remove(path)
    SEEN.append(("upload", req.body, size))
    return 200, {"bytes": size, "first": data[:4].decode()}


@route("POST", "/api/test-big", max_body=4 << 20)
def _big(req):
    return 200, {"keys": len(req.body)}


@route("GET", "/api/test-boom", role="viewer")
def _boom(req):
    raise RuntimeError("route bug")


@route("GET", "/api/test-refuse", role="viewer")
def _refuse(req):
    raise HTTPError(418, "no tea here")


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    home = tmp_path / "projects"
    for name in ("egg", "so101"):
        (home / name / "r001").mkdir(parents=True)
        (home / name / "meta.json").write_text(json.dumps({"kind": "robot"}), encoding="utf-8")
        (home / name / "r001" / "design.json").write_text(Design().dumps(), encoding="utf-8")
        (home / name / "r001" / "results.json").write_text(json.dumps({"parent": None, "summary": {}}), encoding="utf-8")
    st = server.Studio(RobotProject(str(home / "egg")), str(home))
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(st, None))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(st, {f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    SEEN.clear()

    def call(method, path, body=None, headers=None, client=True):
        hdrs = {"X-Morph-Client": "1"} if client and method != "GET" else {}
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            res = conn.getresponse()
            data = res.read()
            return res.status, (json.loads(data) if res.getheader("Content-Type") == "application/json" else data)
        finally:
            conn.close()

    yield st, call, port
    routes.set_role_hook(None)
    httpd.shutdown()
    httpd.server_close()


def test_routes_dispatch_with_rooms_and_bodies(api):
    _, call, _ = api
    assert call("GET", "/api/test-echo?q=1") == (200, {"member": None, "room": "egg", "q": "1"})
    assert call("GET", "/api/test-echo?project=so101")[1]["room"] == "so101"
    assert call("GET", "/api/test-echo?project=nope")[0] == 404
    assert call("GET", "/api/test-echo?project=..%2Fx")[0] == 400
    assert call("POST", "/api/test-echo", {"a": 1}) == (201, {"body": {"a": 1}})
    assert call("POST", "/api/test-echo") == (201, {"body": {}})
    assert call("DELETE", "/api/test-echo?project=so101", {"why": "x"}) == (200, {"deleted": "so101", "body": {"why": "x"}})
    assert call("PUT", "/api/test-echo", {"a": 1})[0] == 404          # no PUT registered for that path
    assert call("GET", "/api/test-raw") == (200, b"\x00\x01binary")
    assert call("GET", "/api/nothing-here")[0] == 404
    assert call("GET", "/api/test-boom") == (500, {"ok": False, "error": "The Studio hit an error: RuntimeError: route bug"})
    assert call("GET", "/api/test-refuse") == (418, {"ok": False, "error": "no tea here"})


def test_prefix_routes_and_exact_paths(api):
    _, call, _ = api
    assert call("GET", "/api/test-prefix/a/b") == (200, {"route": "prefix", "path": "/api/test-prefix/a/b"})
    assert call("GET", "/api/test-prefix/deeper/x")[1]["route"] == "deeper"   # the longest prefix wins
    assert call("GET", "/api/test-prefix/exact") == (200, b"plain text")              # exact beats any prefix
    assert call("GET", "/api/test-prefix/")[0] == 404 and call("GET", "/api/test-prefix")[0] == 404
    assert routes.find("GET", "/api/test-prefix/exact").fn is _exact
    with pytest.raises(ValueError):
        route("GET", "/api/test-echo")(lambda req: None)              # two features can't claim one route
    with pytest.raises(ValueError):
        route("GET", "/api/test-prefix/*")(lambda req: None)
    with pytest.raises(ValueError):
        route("PATCH", "/api/x")
    with pytest.raises(ValueError):
        route("GET", "/api/x", role="admin")


def test_changes_need_the_client_header(api):
    _, call, _ = api
    for method in ("POST", "PUT", "DELETE"):
        code, out = call(method, "/api/test-echo", {"a": 1}, client=False)
        assert code == 403 and "X-Morph-Client" in out["error"]
    assert [s for s in SEEN if s[0] != "echo"] == []
    for path, body in (("/api/reset", {}), ("/api/keys", {"values": {"ANTHROPIC_API_KEY": "sk-ant-x"}}),
                       ("/api/chat", {"message": "hi"}), ("/api/settings", {"values": {"network_enabled": False}})):
        assert call("POST", path, body, client=False)[0] == 403, path
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert call("GET", "/api/test-echo")[0] == 200                    # reads don't need it
    assert call("POST", "/api/test-echo", {"a": 1}, headers={"Origin": "http://evil.example"})[0] == 403


def test_role_hook_sees_every_routed_request(api):
    _, call, _ = api
    asked = []

    def hook(req, role):
        asked.append((req.method, req.path, role))
        member = {"id": "m-viewer", "name": "Vi", "role": "viewer", "disciplines": []}
        if routes.ROLES.index(role) > routes.ROLES.index(member["role"]):
            raise HTTPError(403, f"needs {role}")
        return member

    routes.set_role_hook(hook)
    assert call("GET", "/api/test-echo")[1]["member"]["id"] == "m-viewer"
    assert call("POST", "/api/test-echo", {"a": 1}) == (403, {"ok": False, "error": "needs editor"})
    assert call("POST", "/api/reset", {}) == (403, {"ok": False, "error": "needs owner"})
    assert call("POST", "/api/keys", {"values": {}})[0] == 403 and call("GET", "/api/keys")[0] == 403
    assert call("GET", "/api/state")[0] == 200 and call("GET", "/files/egg/r001/design.json")[0] == 200
    assert ("POST", "/api/test-echo", "editor") in asked and ("GET", "/files/egg/r001/design.json", "viewer") in asked
    assert [s for s in SEEN if s[0] == "post"] == []                  # refused before the handler ran
    routes.set_role_hook(None)
    assert call("POST", "/api/test-echo", {"a": 1})[0] == 201


def test_every_feature_module_loaded():
    assert server.FEATURE_ERRORS == {}


def test_core_routes_carry_their_roles():
    table = {(r.method, r.path): r.role for r in routes.routes()}
    for key, role in {("GET", "/api/state"): "viewer", ("GET", "/api/events"): "viewer", ("GET", "/files/*"): "viewer",
                      ("GET", "/api/export"): "viewer", ("POST", "/api/chat"): "editor", ("POST", "/api/params"): "editor",
                      ("POST", "/api/reset"): "owner", ("POST", "/api/keys"): "owner", ("GET", "/api/keys"): "owner",
                      ("POST", "/api/settings"): "owner", ("POST", "/api/jobs/resolve"): "editor"}.items():
        assert table[key] == role, key


def test_raw_bodies_stream_within_their_cap(api):
    st, call, port = api
    data = b"MORPH" + os.urandom(40_000)
    code, out = call("PUT", "/api/test-upload", data, headers={"Content-Type": "application/octet-stream"})
    assert code == 200 and out == {"bytes": len(data), "first": "MORP"} and SEEN[-1] == ("upload", None, len(data))
    assert call("PUT", "/api/test-upload?mode=chunks", b"x" * 25_000)[1] == {"chunks": [10_000, 10_000, 5_000]}
    code, out = call("PUT", "/api/test-upload", b"x" * (64 << 10) + b"y")
    assert code == 413 and "64 KiB" in out["error"]
    assert not os.path.exists(os.path.join(st.home, ".upload.bin"))
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:   # promises 50,000 bytes, sends 10
        s.sendall(f"PUT /api/test-upload HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Morph-Client: 1\r\n"
                  "Content-Length: 50000\r\n\r\n0123456789".encode())
        s.shutdown(socket.SHUT_WR)
        answer = b""
        while chunk := s.recv(65536):
            answer += chunk
    assert answer.startswith(b"HTTP/1.0 400") and b"cut off" in answer
    assert not os.path.exists(os.path.join(st.home, ".upload.bin"))     # the partial file is removed


def test_json_routes_can_raise_their_own_cap(api):
    _, call, _ = api
    big = {f"k{i}": "x" * 1000 for i in range(1500)}                   # about 1.5 MiB
    assert call("POST", "/api/test-big", big) == (200, {"keys": 1500})
    assert call("POST", "/api/test-echo", big)[0] == 413               # the default stays 1 MiB
