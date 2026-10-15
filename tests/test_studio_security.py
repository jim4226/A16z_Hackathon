"""The Studio's HTTP boundary: paths stay inside the project, names and bodies are checked before anything is
created or read, events are bounded, every response carries the security headers. Offline, real sockets."""

import base64
import hashlib
import http.client
import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

from urllib.parse import quote

import pytest

from morph import keys
from morph.robot.pipeline2 import RobotProject
from morph.spec import Design
from morph.studio import intent, server
from morph.robot.pipeline2 import PARAMS
from morph.studio.security import MAX_BODY, contained_path, param_changes, project_name, robot_source


def make_project(home, name="egg"):
    root = home / name
    (root / "r001" / "cad").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot"}), encoding="utf-8")
    (root / "r001" / "design.json").write_text(Design().dumps(), encoding="utf-8")
    (root / "r001" / "results.json").write_text(json.dumps({"parent": None, "summary": {}}), encoding="utf-8")
    (root / "r001" / "cad" / "example.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    (root / "r001" / "cad" / "robot.json").write_text("{}", encoding="utf-8")
    return RobotProject(str(root))


def _serve(st, allowed, token=None):
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(st, allowed, token))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return httpd


@pytest.fixture
def local(tmp_path, monkeypatch):
    """A local Studio on a free port, over egg (r001) with a sibling folder egg-secret that must stay private."""
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    home = tmp_path / "projects"
    st = server.Studio(make_project(home), str(home))
    (home / "egg-secret").mkdir()
    (home / "egg-secret" / "secret.txt").write_text("private", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("private", encoding="utf-8")
    httpd = _serve(st, None)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(st, {f"127.0.0.1:{port}", f"localhost:{port}"})

    def request(path="/", method="GET", body=None, headers=None):
        hdrs = {"X-Morph-Client": "1"} if method != "GET" else {}
        if body is not None and not isinstance(body, (str, bytes)):
            body = json.dumps(body)
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            res = conn.getresponse()
            return res.status, dict(res.getheaders()), res.read()
        finally:
            conn.close()

    yield st, request, port
    httpd.shutdown()
    httpd.server_close()


def raw(port, data: bytes) -> bytes:
    """Send bytes exactly as given (http.client fixes up headers) and return the whole answer."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(data)
        out = b""
        while chunk := s.recv(65536):
            out += chunk
    return out


# ------------------------------------------------------------------ paths

@pytest.mark.parametrize("path", [
    "/files/..%2fegg-secret/secret.txt", "/files/../egg-secret/secret.txt", "/files/%2e%2e/egg-secret/secret.txt",
    "/files/r001/..%2f..%2fegg-secret/secret.txt", "/files/r001/../../outside.txt", "/files/r001/%2e%2e/%2e%2e/outside.txt",
    "/files/meta.json", "/files/ledger.jsonl", "/files/r001/.env", "/files/r001%2f..%2f..%2fouter.txt",
    "/files/r001/cad%5c..%5c..%5cmeta.json", "/files/C:%2fWindows%2fwin.ini", "/files/r001/cad/x%00.json",
    "/files/r001", "/files/", "/files//etc/passwd",
])
def test_files_never_leave_the_project_revisions(local, path):
    _, request, _ = local
    code, _, body = request(path)
    assert code == 404 and b"private" not in body


def test_files_serve_revision_artifacts_sandboxed(local):
    _, request, _ = local
    code, headers, body = request("/files/r001/cad/example.html")
    assert code == 200 and body == b"<script>alert(1)</script>"
    csp = headers["Content-Security-Policy"]
    assert csp.startswith("sandbox;") and "allow-scripts" not in csp and "allow-same-origin" not in csp
    assert request("/files/r001/cad/robot.json")[0] == 200


def test_symlink_out_of_a_revision_is_not_served(local, tmp_path):
    st, request, _ = local
    try:
        os.symlink(tmp_path / "outside.txt", os.path.join(st.p.root, "r001", "cad", "link.txt"))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need extra rights on this system")
    assert request("/files/r001/cad/link.txt")[0] == 404


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes are POSIX")
def test_pipes_and_folders_are_never_opened(local):
    st, request, _ = local
    os.mkfifo(os.path.join(st.p.root, "r001", "cad", "pipe.txt"))  # open() on it would wait for a writer forever
    started = time.monotonic()
    assert request("/files/r001/cad/pipe.txt")[0] == 404
    assert request("/files/r001/cad")[0] == 404
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("path", ["/static/../server.py", "/static/..%2fserver.py", "/static/%2e%2e/security.py",
                                  "/static/.hidden", "/static/vendor/..%2f..%2fserver.py"])
def test_static_files_stay_in_static(local, path):
    _, request, _ = local
    assert request(path)[0] == 404


def test_contained_path_rules(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj-secret").mkdir()
    root = str(tmp_path / "proj")
    for bad in ["../proj-secret/x", "..%2fproj-secret", "/abs", "a/../../x", "a\\..\\x", "C:/x", "a//b", "./a", ".env",
                "a/.git/config", "", "%00", "a/b/..", "x%2f..%2f..%2fproj-secret"]:
        assert contained_path(root, bad) is None, bad
    assert contained_path(root, "r001/cad/robot.glb") == os.path.join(os.path.realpath(root), "r001", "cad", "robot.glb")
    assert contained_path(root, "%2e%2e", decode=False) == os.path.join(os.path.realpath(root), "%2e%2e")  # a plain name


def test_names_are_validated():
    for good in ("egg-picker", "so101", "a", "x" * 40):
        assert project_name(good) == good
    for bad in ("../x", "/tmp/x", "Egg", "egg_picker", "-egg", "", "x" * 41, "con", "a/b", "a\\b", ".hidden", None, 5):
        with pytest.raises(ValueError):
            project_name(bad)
    assert robot_source("so_101-v2") == "so_101-v2"
    for bad in ("../so101", "/robots/so101", "C:\\robots", "So101", "", "x" * 49, "a b", ".robots", None):
        with pytest.raises(ValueError):
            robot_source(bad)


# ------------------------------------------------------------------ projects

def test_opening_a_bad_name_creates_nothing(local, tmp_path):
    st, request, _ = local
    home = tmp_path / "projects"
    before = sorted(os.listdir(home))
    for name in ("../pwned", str(tmp_path / "abs"), "Egg", "../egg", "nope", 7, ["egg"]):
        code, _, body = request("/api/project", "POST", {"name": name})
        assert code in (400, 404) and json.loads(body)["ok"] is False
        with pytest.raises((ValueError, LookupError)):
            st.room(name)
        if isinstance(name, str):
            for path in ("/api/state", "/api/events", "/api/rev?rev=r001"):
                code, _, body = request(f"{path}{'&' if '?' in path else '?'}project={quote(name, safe='')}")
                assert code in (400, 404) and json.loads(body)["ok"] is False, (path, name)
    assert sorted(os.listdir(home)) == before
    assert not (tmp_path / "pwned").exists() and not (tmp_path / "abs").exists()
    assert os.path.basename(st.p.root) == "egg" and list(st.rooms) == ["egg"]


def test_projects_list_only_real_robot_projects_inside_home(local, tmp_path):
    st, request, _ = local
    home = tmp_path / "projects"
    make_project(home, "so101")
    make_project(home, ".trash")          # dot folders are never projects
    make_project(home, "Bad_Name")        # not a valid project name
    outside = make_project(tmp_path, "elsewhere")
    try:
        os.symlink(outside.root, home / "linked", target_is_directory=True)
    except (OSError, NotImplementedError):
        pass
    assert [p["name"] for p in st.projects()] == ["egg", "so101"]
    for name in (".trash", "linked", "Bad_Name"):
        with pytest.raises((ValueError, LookupError)):
            st.room(name)
    assert request("/api/project", "POST", {"name": "so101"})[0] == 200
    assert os.path.basename(st.p.root) == "egg"   # a tab picks its project with ?project=; nobody else moves
    code, _, body = request("/api/state?project=so101")
    assert code == 200 and json.loads(body)["project"] == "so101" and json.loads(body)["start"] == "egg"


def test_opening_another_project_never_waits_for_a_build(local):
    st, request, _ = local
    make_project(Path(st.home), "so101")
    with st.room().lock:  # egg is building
        assert request("/api/project", "POST", {"name": "so101"})[0] == 200
        assert json.loads(request("/api/state?project=so101")[2])["project"] == "so101"


# ------------------------------------------------------------------ request bodies

@pytest.mark.parametrize("headers,code", [
    ({"Content-Type": "application/json", "Content-Length": "abc"}, 400),
    ({"Content-Type": "application/json", "Content-Length": "-1"}, 400),
    ({"Content-Type": "application/json", "Content-Length": "1e3"}, 400),
    ({"Content-Type": "application/json", "Content-Length": str(MAX_BODY + 1)}, 413),
    ({"Content-Type": "application/json", "Content-Length": str(2 ** 40)}, 413),
])
def test_content_length_is_checked_before_reading(local, headers, code):
    _, _, port = local
    head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    started = time.monotonic()
    answer = raw(port, f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Morph-Client: 1\r\n{head}\r\n".encode())
    assert answer.startswith(f"HTTP/1.0 {code}".encode())
    assert time.monotonic() - started < 2  # answered at once: nothing waited for a body that never comes
    assert b'"ok": false' in answer


def test_two_content_lengths_or_chunked_are_refused(local):
    _, _, port = local
    for extra in ("Content-Length: 2\r\nContent-Length: 2\r\n", "Transfer-Encoding: chunked\r\n"):
        answer = raw(port, f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
                           f"X-Morph-Client: 1\r\n{extra}\r\n{{}}".encode())
        assert answer.startswith(b"HTTP/1.0 400")


def test_a_body_that_stops_arriving_is_refused_quietly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    home = tmp_path / "projects"
    st = server.Studio(make_project(home), str(home))
    monkeypatch.setattr(st, "queue_chat", lambda *a, **k: pytest.fail("must not dispatch"))
    httpd = _serve(st, None)
    httpd.RequestHandlerClass.timeout = 0.5  # the Studio's is 30 s
    try:
        answer = raw(httpd.server_address[1], b"POST /api/chat HTTP/1.1\r\nHost: x\r\nX-Morph-Client: 1\r\n"
                                              b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{\"message\":")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert answer.startswith(b"HTTP/1.0 400") and b"cut off" in answer
    assert "Traceback" not in capsys.readouterr().err


class _Job:
    def info(self):
        return {"id": "j1", "status": "queued"}


@pytest.mark.parametrize("body,ctype,code", [
    ("{", "application/json", 400), ("[1, 2]", "application/json", 400), ('"text"', "application/json", 400),
    ('{"v": NaN}', "application/json", 400), (b"\xff\xfe{}", "application/json", 400),
    ('{"message": "hi"}', "text/plain", 415), ("message=hi", "application/x-www-form-urlencoded", 415),
])
def test_json_bodies_must_be_objects(local, monkeypatch, body, ctype, code):
    st, request, _ = local
    monkeypatch.setattr(st, "queue_chat", lambda *a, **k: pytest.fail("must not dispatch"))
    assert request("/api/chat", "POST", body, {"Content-Type": ctype})[0] == code


def test_a_body_at_the_limit_is_read(local, monkeypatch):
    st, request, _ = local
    monkeypatch.setattr(st, "queue_chat", lambda *a, **k: _Job())
    msg = json.dumps({"message": "hi", "pad": "x" * (MAX_BODY - 40)})
    assert len(msg) <= MAX_BODY
    assert request("/api/chat", "POST", msg, {"Content-Type": "application/json"})[0] == 202


def test_refused_posts_answer_instead_of_resetting(local, monkeypatch):
    st, request, port = local
    monkeypatch.setattr(st, "queue_chat", lambda *a, **k: pytest.fail("must not dispatch"))
    for _ in range(30):
        assert request("/api/chat", "POST", {"message": "hello" * 200}, {"Origin": "https://attacker.example"})[0] == 403
        assert request("/api/chat", "POST", "x" * 2000, {"Content-Type": "text/plain"})[0] == 415


def test_chat_messages_are_checked(local, monkeypatch):
    st, request, _ = local
    monkeypatch.setattr(server, "interpret", lambda *a: pytest.fail("must not reach the model"))
    sent = []
    monkeypatch.setattr(st.hub, "send", sent.append)
    for body in ({"message": ""}, {"message": "   "}, {"message": 5}, {"message": None}, {}, {"message": "x" * 8001}):
        assert request("/api/chat", "POST", body)[0] == 400
    code, _, out = request("/api/chat", "POST", {"message": "use this key sk-ant-api03-abcdefghijklmnop please"})
    assert code == 400 and "Keys" in json.loads(out)["error"]
    assert request("/api/chat", "POST", {"message": "kwpat-0123456789abcdef"})[0] == 400
    assert request("/api/chat", "POST", {"message": "subq: sk-AbCdEf0123456789ghIJkl"})[0] == 400  # SubQ keys are sk-...
    time.sleep(0.2)
    assert sent == [] and st.hub.history == []


def test_params_are_known_editor_keys_with_plain_values(local, monkeypatch):
    st, request, _ = local
    got = []
    monkeypatch.setattr(st, "queue_params", lambda room, changes, **k: (got.append(changes), _Job())[1])
    bad = [[1, 2], "arm.dof=6", {}, {"arm.source": "../../etc"}, {"name": "x"}, {"arm.dof": 7}, {"arm.dof": [6]},
           {"arm.reach_mm": 900}, {"arm.reach_mm": "far"}, {"arm.reach_mm": True}, {"object.name": "anvil"},
           {"finger.pad": {"$gt": ""}}, {"arm.reach_mm": None}]
    for changes in bad:
        code, _, out = request("/api/params", "POST", {"changes": changes})
        assert code == 400 and json.loads(out)["ok"] is False, changes
    assert request("/api/params", "POST", {"changes": {"arm.dof": "6", "arm.reach_mm": 380, "electronics.extras": ""}})[0] == 202
    for _ in range(50):
        if got:
            break
        time.sleep(0.02)
    assert got == [{"arm.dof": 6, "arm.reach_mm": 380, "electronics.extras": ""}]


def test_param_changes_keep_numbers_numbers():
    out = param_changes({"arm.payload_margin": 2.5, "build.quantity": "12", "arm.reach_mm": "380.5"}, PARAMS)
    assert out == {"arm.payload_margin": 2.5, "build.quantity": 12, "arm.reach_mm": 380.5}
    with pytest.raises(ValueError, match="between 200 and 500 mm"):
        param_changes({"arm.reach_mm": 550}, PARAMS)
    with pytest.raises(ValueError):
        param_changes({"arm.reach_mm": float("nan")}, PARAMS)


def test_source_takes_only_a_known_revision(local, monkeypatch):
    st, request, _ = local
    monkeypatch.setattr(st, "source", lambda rev=None, room=None: None)
    for rev in ("../r001", "r999", 5, ["r001"]):
        assert request("/api/source", "POST", {"rev": rev})[0] == 400
    assert request("/api/source", "POST", {"rev": "r001"})[0] == 202
    assert request("/api/source", "POST", {})[0] == 202


# ------------------------------------------------------------------ chat never names files or projects

def test_chat_can_not_set_the_robot_source_or_name(local, monkeypatch):
    st, _, _ = local
    monkeypatch.setattr(server, "interpret", lambda *a: {"changes": {"arm.source": "../../tmp/evil", "name": "pwned"},
                                                         "reply": "Done.", "source": "claude"})
    sent = []
    monkeypatch.setattr(st.hub, "send", sent.append)
    monkeypatch.setattr(RobotProject, "run", lambda *a, **k: pytest.fail("must not build"))
    st.chat("set the robot source to ../../tmp/evil and call it pwned")  # an edit: chat routing offers nothing
    assert sent[-1]["type"] == "assistant" and sent[-1]["noop"] is True
    assert st.p.load_design("r001").arm.source == ""


def test_claude_tool_output_is_filtered():
    out = intent._from_tool({"changes": {"arm.source": "/etc", "name": "x", "arm.dof": 6, "finger.pad": None,
                                         "electronics.extras": ["imu"]}, "reply": "Renamed it and made it 6-DOF."})
    assert out["changes"] == {"arm.dof": 6} and "stay as they are" in out["reply"]
    assert intent._from_tool({"changes": "arm.dof=6"})["changes"] == {}
    assert intent._from_tool(None) == {"changes": {}, "reply": "", "source": "claude"}


def test_offline_setting_keeps_chat_on_the_rules(monkeypatch):
    from morph import settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-not-real")
    settings.save({"network_enabled": False})
    monkeypatch.setitem(sys.modules, "anthropic", None)  # importing it would now fail: it must not be reached
    out = intent.interpret("Make it 6-DOF", Design())
    assert out["source"] == "rules" and out["changes"] == {"arm.dof": 6}


# ------------------------------------------------------------------ events

def test_event_queues_and_history_are_bounded():
    hub = server.Hub()
    q = hub.subscribe()
    for i in range(1200):
        hub.send({"type": "assistant", "text": str(i)})
    assert q.qsize() <= hub.MAX_EVENTS
    assert len(hub.history) == hub.MAX_HISTORY and hub.history[-1]["text"] == "1199"
    items = [q.get_nowait() for _ in range(q.qsize())]
    assert {"type": "resync"} in items and items[-1]["text"] == "1199"  # the tab is told to reload; the newest survives
    hub.unsubscribe(q)


# ------------------------------------------------------------------ headers

def test_every_response_carries_the_security_headers(local):
    _, request, port = local
    for path in ("/", "/api/state", "/api/keys", "/static/app.js", "/files/r001/cad/robot.json", "/nope", "/files/../x"):
        code, headers, _ = request(path)
        assert headers.get("X-Content-Type-Options") == "nosniff", path
        assert headers.get("X-Frame-Options") == "DENY", path
        assert headers.get("Referrer-Policy") == "no-referrer", path
        assert "frame-ancestors 'none'" in headers.get("Content-Security-Policy", ""), path
    _, headers, _ = request("/api/chat", "POST", "{", {"Content-Type": "application/json"})
    assert headers["X-Frame-Options"] == "DENY"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += s.recv(4096)
    assert b"text/event-stream" in head and b"X-Frame-Options: DENY" in head and b"nosniff" in head


def test_page_policy_allows_exactly_the_inline_loader(local):
    _, request, _ = local
    code, headers, body = request("/")
    assert code == 200
    csp = headers["Content-Security-Policy"]
    page = body.decode("utf-8").replace("\r\n", "\n")
    for script in re.findall(r"<script>(.*?)</script>", page, re.S):
        digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()
        assert f"'sha256-{digest}'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    assert "blob:" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp


def test_remote_sign_in_page_and_refusals_carry_headers(tmp_path, monkeypatch):
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    st = server.Studio(make_project(tmp_path / "projects"), str(tmp_path / "projects"))
    httpd = _serve(st, None, token="test-code-0123456")
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("GET", "/")
        res = conn.getresponse()
        res.read()
        assert res.status == 401 and res.getheader("X-Frame-Options") == "DENY"
        assert "form-action 'self'" in res.getheader("Content-Security-Policy")
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("GET", "/files/r001/cad/robot.json")
        res = conn.getresponse()
        res.read()
        assert res.status == 401 and res.getheader("Referrer-Policy") == "no-referrer"
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_wrong_access_codes_are_throttled_per_address(tmp_path, monkeypatch):
    """A short MORPH_TOKEN can't be found by trying many: after 10 different wrong codes from one address, its codes
    aren't compared for 15 minutes. A stale cookie sent again and again (a tab open across a restart) counts once."""
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    st = server.Studio(make_project(tmp_path / "projects"), str(tmp_path / "projects"))
    code = "test-code-0123456"
    httpd = _serve(st, None, token=code)

    def get(path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        try:
            conn.request("GET", path, headers=headers or {})
            res = conn.getresponse()
            return res.status, res.read()
        finally:
            conn.close()

    try:
        stale = {"Cookie": f"morph_access_{httpd.server_address[1]}=an-old-code-from-before"}
        assert all(get("/api/state", stale)[0] == 401 for _ in range(15))
        assert get(f"/?token={code}")[0] == 302                                     # the stale tab didn't lock anyone out
        for i in range(8):
            assert get(f"/?token=guess-{i}")[0] == 401
        assert get("/api/state", {"X-Morph-Token": "guess-8"})[0] == 401             # the 10th different wrong code
        status, body = get(f"/?token={code}")
        assert status == 429 and b"Too many wrong access codes" in body             # not even the right one is checked now
        assert get("/api/state", {"X-Morph-Token": code})[0] == 429
        server.Guesses.WINDOW, window = 0, server.Guesses.WINDOW
        try:
            assert get(f"/?token={code}")[0] == 302                                 # the window has passed
        finally:
            server.Guesses.WINDOW = window
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_wrong_guesses_never_lock_out_the_code_morph_made(tmp_path, monkeypatch):
    """The code Morph makes (96 random bits) can't be guessed, so it is always compared: after 10 different wrong codes
    from one address the wrong ones are told to stop (429), and the person at that address with the link still gets in."""
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    st = server.Studio(make_project(tmp_path / "projects"), str(tmp_path / "projects"))
    code = server.access_token("0.0.0.0")
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(st, None, code, chosen=False))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def get(path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        try:
            conn.request("GET", path, headers=headers or {})
            res = conn.getresponse()
            return res.status, res.read()
        finally:
            conn.close()

    try:
        assert [get(f"/?token=guess-{i}")[0] for i in range(10)] == [401] * 10
        status, body = get("/?token=guess-10")
        assert status == 429 and b"it still works" in body and b"Wait 15 minutes" not in body
        assert get(f"/?token={code}")[0] == 302 and get("/api/state", {"X-Morph-Token": code})[0] == 200
        assert get("/api/state", {"X-Morph-Token": "guess-11"})[0] == 429
    finally:
        httpd.shutdown()
        httpd.server_close()



def test_a_flood_of_wrong_codes_from_one_address_keeps_its_record_small(tmp_path, monkeypatch):
    """The code Morph made is compared even while its address is blocked, so wrong codes keep arriving; the address
    keeps only its last 10, so a flood costs the same memory and time per request as ten wrong codes, and the person
    there with the link still signs in."""
    g = server.Guesses()
    for i in range(20000):  # what _code does for each wrong code: blocked() then failed()
        g.blocked("192.0.2.2")
        g.failed("192.0.2.2", f"wrong-{i}")
    assert len(g.wrong["192.0.2.2"]) == g.LIMIT and g.blocked("192.0.2.2")
    g.failed("192.0.2.2", "wrong-19999")  # the same code again moves to the end, still one entry
    assert len(g.wrong["192.0.2.2"]) == g.LIMIT
    for i in range(g.ADDRESSES + 50):  # many addresses: the ones that failed longest ago are forgotten first
        g.failed(f"10.{i // 65536}.{i // 256 % 256}.{i % 256}", "x")
    assert len(g.wrong) == g.ADDRESSES and "192.0.2.2" not in g.wrong and "10.0.16.49" in g.wrong
    g.wrong.clear()
    g.failed("192.0.2.9", "old")
    monkeypatch.setattr(server.time, "monotonic", lambda real=time.monotonic: real() + g.WINDOW + 1)
    assert not g.blocked("192.0.2.9") and "192.0.2.9" not in g.wrong  # aged out and forgotten

    monkeypatch.undo()
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    st = server.Studio(make_project(tmp_path / "projects"), str(tmp_path / "projects"))
    code = server.access_token("0.0.0.0")
    handler = server.make_handler(st, None, code, chosen=False)
    httpd = server._Server(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    try:
        seen = set()
        for i in range(300):  # one keep-alive connection, a different wrong code each time
            conn.request("GET", "/api/projects", headers={"X-Morph-Token": f"wrong-{i}"})
            res = conn.getresponse()
            res.read()
            seen.add(res.status)
        assert seen == {401, 429} and len(handler.guesses.wrong["127.0.0.1"]) == server.Guesses.LIMIT
        conn.request("GET", "/api/projects", headers={"X-Morph-Token": code})
        res = conn.getresponse()
        res.read()
        assert res.status == 200
    finally:
        conn.close()
        httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------------------------ keys

@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_env_file_is_private(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("OTHER=1\n", encoding="utf-8")
    os.chmod(path, 0o644)
    monkeypatch.setattr(keys, "TEMPLATE", str(tmp_path / "env.example"))
    keys.save({"ANTHROPIC_API_KEY": "sk-ant-test"}, str(path))
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert [p for p in os.listdir(tmp_path) if p.endswith(".tmp")] == []
    (tmp_path / "env.example").write_text("ANTHROPIC_API_KEY=\n", encoding="utf-8")
    fresh = tmp_path / "fresh.env"
    assert keys.ensure_env_file(str(fresh))
    assert (os.stat(fresh).st_mode & 0o777) == 0o600
    if os.name != "nt":  # a .env from before 0.2 (0644) is made private the next time the Studio starts
        os.chmod(fresh, 0o644)
        assert not keys.ensure_env_file(str(fresh))
        assert (os.stat(fresh).st_mode & 0o777) == 0o600 and fresh.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=\n"


def test_unwritable_env_is_a_clear_error(local, monkeypatch):
    _, request, _ = local

    def denied(path, text):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(keys, "_write_private", denied)
    code, _, body = request("/api/keys", "POST", {"values": {"ANTHROPIC_API_KEY": "sk-ant-not-saved"}})
    out = json.loads(body)
    assert code == 500 and out["ok"] is False and "Permission denied" in out["error"] and "sk-ant" not in out["error"]
    assert "ANTHROPIC_API_KEY" not in os.environ
    for values in (["ANTHROPIC_API_KEY"], {"ANTHROPIC_API_KEY": 123}, {"ANTHROPIC_API_KEY": {"a": 1}}):
        assert request("/api/keys", "POST", {"values": values})[0] == 400


# ------------------------------------------------------------------ settings route

def test_settings_round_trip_and_validation(local):
    from morph import settings

    _, request, _ = local
    code, _, body = request("/api/settings")
    assert code == 200 and json.loads(body)["settings"] == settings.DEFAULTS
    code, _, body = request("/api/settings", "POST", {"values": {"telemetry_enabled": True}})
    out = json.loads(body)
    assert code == 200 and out["settings"]["telemetry_enabled"] is True and settings.load()["telemetry_enabled"] is True
    for values in ({"telemetry_enabled": "yes"}, {"api_key": True}, {}, None, [True]):
        assert request("/api/settings", "POST", {"values": values})[0] == 400
    code, _, body = request("/api/keys")
    assert json.loads(body)["settings"]["telemetry_enabled"] is True
