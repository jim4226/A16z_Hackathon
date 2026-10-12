"""Local HTTP boundary and job isolation, with no provider/network calls."""

import http.client
import json
import threading
from pathlib import Path

import pytest

from morph.robot.pipeline2 import RobotProject
from morph.spec import Design
from morph.studio import server
from morph.studio.security import MAX_BODY, contained_path


def make_project(home, name="egg"):
    root = home / name
    (root / "r001" / "cad").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot"}), encoding="utf-8")
    (root / "r001" / "design.json").write_text(Design().dumps(), encoding="utf-8")
    (root / "r001" / "results.json").write_text(json.dumps({"parent": None, "summary": {}}), encoding="utf-8")
    (root / "r001" / "cad" / "example.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    return RobotProject(str(root))


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("MORPH_CONFIG_DIR", str(tmp_path / "config"))
    st = server.Studio(make_project(tmp_path / "projects"))
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(st))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]

    def request(path="/", method="GET", body=None, headers=None, auth=True):
        supplied = {"Cookie": f"rcubed_session={st.session.token}"} if auth else {}
        if body is not None and not isinstance(body, (str, bytes)):
            body = json.dumps(body)
            supplied["Content-Type"] = "application/json"
        supplied.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=supplied)
            res = conn.getresponse()
            return res.status, dict(res.getheaders()), res.read()
        finally:
            conn.close()

    yield st, request, port
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


def test_home_bootstraps_cookie_without_exposing_token(local):
    st, request, _ = local
    code, headers, body = request(auth=False)
    assert code == 200
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert st.session.token.encode() not in body
    assert headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.parametrize("path", ["/api/state", "/api/provider", "/api/rev?rev=r001", "/api/events", "/files/r001/design.json"])
def test_sensitive_gets_require_session(local, path):
    _, request, _ = local
    assert request(path, auth=False)[0] == 401


def test_other_launch_cookie_is_invalid(local):
    _, request, _ = local
    assert request("/api/state", headers={"Cookie": "rcubed_session=from-an-old-launch"})[0] == 401


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example"}, {"Origin": "https://attacker.example"}, {"Origin": "null"},
    {"Origin": "http://[invalid"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
])
def test_rebinding_and_cross_origin_fail_even_during_bootstrap(local, headers):
    _, request, _ = local
    code, response, _ = request(headers=headers, auth=False)
    assert code == 403 and "Set-Cookie" not in response


def test_valid_origin_is_exact_host_and_port(local):
    _, request, port = local
    assert request("/api/state", headers={"Origin": f"http://127.0.0.1:{port}"})[0] == 200
    assert request("/api/state", headers={"Origin": f"http://localhost:{port}"})[0] == 403


def test_unauthorized_post_never_dispatches(local, monkeypatch):
    st, request, _ = local
    monkeypatch.setattr(st, "submit", lambda *a: pytest.fail("unauthorized dispatch"))
    assert request("/api/chat", "POST", {"message": "hello"}, auth=False)[0] == 401
    assert request("/api/chat", "POST", {"message": "hello"}, headers={"Origin": "https://evil.example"})[0] == 403


def test_repeated_unauthorized_posts_return_status_without_connection_reset(local, monkeypatch):
    st, request, _ = local
    monkeypatch.setattr(st, "submit", lambda *a: pytest.fail("unauthorized dispatch"))
    for _ in range(40):
        assert request("/api/chat", "POST", {"message": "hello" * 100}, auth=False)[0] == 401
        assert request("/api/chat", "POST", {"message": "hello" * 100}, headers={"Origin": "https://evil.example"})[0] == 403
        assert request("/api/chat", "POST", "x" * 500, headers={"Content-Type": "text/plain"})[0] == 415


def test_rejected_incomplete_or_oversized_body_does_not_hold_request_thread(local):
    import time
    _, request, _ = local
    for declared in (500, MAX_BODY + 1, "invalid"):
        started = time.monotonic()
        code, _, _ = request("/api/chat", "POST", "", auth=False,
                             headers={"Content-Type": "application/json", "Content-Length": str(declared)})
        assert code == 401
        assert time.monotonic() - started < 1.5  # never wait for the regular 30-second request timeout


@pytest.mark.parametrize("body,headers,code", [
    ("{}", {"Content-Type": "text/plain"}, 415),
    ("{", {"Content-Type": "application/json"}, 400),
    ("[]", {"Content-Type": "application/json"}, 400),
    ('{"v":NaN}', {"Content-Type": "application/json"}, 400),
    ("", {"Content-Type": "application/json", "Content-Length": str(MAX_BODY + 1)}, 413),
    ("", {"Content-Type": "application/json", "Content-Length": "-1"}, 413),
    ("", {"Content-Type": "application/json", "Content-Length": "invalid"}, 413),
])
def test_bounded_json_before_dispatch(local, body, headers, code):
    _, request, _ = local
    assert request("/api/chat", "POST", body, headers=headers)[0] == code


def test_secret_paste_never_reaches_history_or_ai(local, monkeypatch):
    st, request, _ = local
    monkeypatch.setattr(server, "interpret", lambda *a: pytest.fail("secret sent to AI"))
    secret = "sk-ant-not-a-real-key"
    assert request("/api/chat", "POST", {"message": secret})[0] == 400
    assert st.chat(secret) is False
    assert secret not in json.dumps(st.hub.history)


@pytest.mark.parametrize("relative", ["../sibling/secret", "/absolute", "C:/secret", "a/../../secret", "..%2fsibling", "a\\..\\secret"])
def test_canonical_path_validation(tmp_path, relative):
    assert contained_path(str(tmp_path), relative) is None


def test_symlink_escape_cannot_be_served_or_registered(tmp_path):
    home = tmp_path / "projects"
    project = make_project(home)
    outside = make_project(tmp_path / "outside")
    try:
        (home / "escape").symlink_to(outside.root, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks requires Windows developer mode or privilege")
    assert contained_path(str(home), "escape/r001/design.json") is None
    studio = server.Studio(project)
    assert "escape" not in {p["name"] for p in studio.projects()}
    assert not studio.switch("escape")


def test_project_switch_does_not_create_traversal_directory(local):
    st, request, _ = local
    escape = Path(st.home).parent / "new-escape"
    assert request("/api/project", "POST", {"name": "../new-escape"})[0] == 409
    assert not escape.exists()
    assert not st.switch(str(escape))


def test_files_are_revision_only_and_active_artifacts_sandboxed(local):
    _, request, _ = local
    assert request("/files/meta.json")[0] == 403
    assert request("/files/r001/../../secret.txt")[0] == 403
    code, headers, _ = request("/files/r001/cad/example.html")
    assert code == 200 and "sandbox" in headers["Content-Security-Policy"]
    assert "allow-same-origin" not in headers["Content-Security-Policy"]


def test_public_reset_is_disabled(local, monkeypatch):
    st, request, _ = local
    monkeypatch.setattr(st, "reset", lambda: pytest.fail("reset called"))
    assert request("/api/reset", "POST", {})[0] == 403


def test_demo_reset_is_blocked_during_sourcing(local):
    st, request, _ = local
    st.demo_mode = True
    st.sourcing.acquire()
    try:
        assert request("/api/reset", "POST", {})[0] == 409
        assert st.p.revs() == ["r001"]
        assert not st.busy.locked()
    finally:
        st.sourcing.release()


def test_busy_requests_do_not_spend_tokens(local, monkeypatch):
    st, request, _ = local
    monkeypatch.setattr(server, "interpret", lambda *a: pytest.fail("busy request called AI"))
    st.busy.acquire()
    try:
        assert request("/api/chat", "POST", {"message": "smaller"})[0] == 409
        assert st.chat("smaller") is False
    finally:
        st.busy.release()


def test_chat_target_is_captured_before_ai(tmp_path, monkeypatch):
    home = tmp_path / "projects"
    original = make_project(home, "first")
    other = make_project(home, "second")
    st = server.Studio(original)
    interpreting, proceed, done = threading.Event(), threading.Event(), threading.Event()
    targets = []

    def interpret(*args):
        interpreting.set()
        assert proceed.wait(3)
        return {"changes": {"object.mass_g": 20}, "reply": "Ready", "source": "rules"}

    def apply(p, *args, **kwargs):
        targets.append(p.root)
        done.set()

    monkeypatch.setattr(server, "interpret", interpret)
    monkeypatch.setattr(st, "_apply", apply)
    assert st.submit("chat", "make it lighter")
    assert interpreting.wait(3)
    assert not st.switch("second")
    st.p = other  # Even an out-of-band direct assignment cannot redirect the captured job.
    proceed.set()
    assert done.wait(3)
    assert targets == [original.root]


def test_sse_and_history_are_bounded(local):
    st, request, _ = local
    st.hub.MAX_CLIENTS = 1
    q = st.hub.subscribe()
    assert request("/api/events")[0] == 429
    for i in range(1000):
        st.hub.send({"type": "assistant", "text": str(i)})
    assert q.qsize() <= st.hub.MAX_EVENTS
    assert len(st.hub.history) == 200
    st.hub.unsubscribe(q)


def test_provider_routes_are_authenticated_serialized_and_sanitized(local, monkeypatch):
    from morph import providers
    st, request, _ = local
    monkeypatch.setattr(providers, "save_key", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("sk-ant-sensitive-error")))
    assert request("/api/provider/save", "POST", {"key": "dummy"}, auth=False)[0] == 401
    code, _, body = request("/api/provider/save", "POST", {"key": "dummy"})
    assert code == 503 and b"sk-ant" not in body
    st.busy.acquire()
    try:
        assert request("/api/provider/remove", "POST", {})[0] == 409
    finally:
        st.busy.release()


def test_new_project_name_validation_and_existing_name_safety(local):
    st, request, _ = local
    assert request("/api/projects/create", "POST", {"name": "../outside"})[0] == 400
    assert request("/api/projects/create", "POST", {"name": "NUL"})[0] == 400
    assert request("/api/projects/create", "POST", {"name": "egg"})[0] == 409
    assert request("/api/projects/create", "POST", {"name": "new", "example": "arbitrary"})[0] == 400
    assert RobotProject(st.p.root).revs() == ["r001"]


def test_duplicate_preserves_source_and_selects_copy(tmp_path):
    st = server.Studio(make_project(tmp_path / "projects"))
    original = st.p
    st._create(original, {"name": "my-copy"}, duplicate=True)
    assert Path(st.p.root).name == "my-copy"
    assert st.p.load_design("r001").to_dict() == original.load_design("r001").to_dict()
    assert original.revs() == ["r001"]
    assert {p["name"] for p in st.projects()} == {"egg", "my-copy"}


def test_failed_project_creation_rolls_back_without_touching_existing(tmp_path, monkeypatch):
    st = server.Studio(make_project(tmp_path / "projects"))
    original = st.p
    monkeypatch.setattr(RobotProject, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("interrupted build")))
    with pytest.raises(RuntimeError):
        st._create(original, {"name": "new-project", "example": "egg-picker"})
    assert st.p is original
    assert original.revs() == ["r001"]
    assert not (Path(st.home) / "new-project").exists()
    assert not list(Path(st.home).glob(".creating-*"))


def test_new_project_is_published_only_after_success(tmp_path, monkeypatch):
    st = server.Studio(make_project(tmp_path / "projects"))
    original = st.p

    def fake_run(p, design, **kwargs):
        assert not (Path(st.home) / "new-project").exists()
        assert {r["name"] for r in st.projects()} == {"egg"}
        root = Path(p.root) / "r001"
        root.mkdir()
        (root / "design.json").write_text(design.dumps(), encoding="utf-8")
        (root / "results.json").write_text('{"parent": null}', encoding="utf-8")
        return "r001"

    monkeypatch.setattr(RobotProject, "run", fake_run)
    st._create(original, {"name": "new-project", "example": "so101"})
    assert Path(st.p.root).name == "new-project"
    assert st.p.load_design("r001").arm.source == "so101"
    assert original.revs() == ["r001"]


def test_submit_emits_idle_only_after_releasing_operation_lock(tmp_path, monkeypatch):
    st = server.Studio(make_project(tmp_path / "projects"))
    observed, done = [], threading.Event()
    monkeypatch.setattr(st, "_params", lambda *a: None)

    def receive(event):
        if event["type"] == "job_idle":
            observed.append((event["project"], event["kind"], st.busy.locked()))
            done.set()

    monkeypatch.setattr(st.hub, "send", receive)
    assert st.submit("params", {"object.mass_g": 10})
    assert done.wait(3)
    assert observed == [("egg", "params", False)]


def test_public_host_is_rejected_before_bind(tmp_path):
    with pytest.raises(SystemExit, match="local app"):
        server.serve(make_project(tmp_path), host="0.0.0.0", port=0)


def test_export_http_auth_attachment_and_operation_lock(local):
    import io
    import zipfile
    st, request, _ = local
    assert request("/api/export?rev=r001", auth=False)[0] == 401
    assert request("/api/export?rev=../r001")[0] == 404
    code, headers, body = request("/api/export?rev=r001")
    assert code == 200
    assert headers["Content-Type"] == "application/zip"
    assert headers["Content-Disposition"] == 'attachment; filename="egg-r001.zip"'
    with zipfile.ZipFile(io.BytesIO(body)) as bundle:
        assert json.loads(bundle.read("export-manifest.json"))["revision"] == "r001"
    st.busy.acquire()
    try:
        assert request("/api/export?rev=r001")[0] == 409
    finally:
        st.busy.release()
    st.sourcing.acquire()
    try:
        assert request("/api/export?rev=r001")[0] == 409
        assert not st.busy.locked()
    finally:
        st.sourcing.release()
