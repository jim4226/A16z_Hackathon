"""Server limits for a Studio other computers can reach: a connection gets a deadline for its request line and headers
(however slowly it trickles them), and there is a cap on open connections and on event streams; past either, the
answer is 503 at once. Event streams themselves stay open well past the header deadline."""

import json
import socket
import threading
import time

import pytest

from morph import keys
from morph.robot.pipeline2 import RobotProject
from morph.spec import Design
from morph.studio import server


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(server, "HEADER_TIMEOUT", 1.0, raising=False)
    monkeypatch.setattr(server._Server, "max_connections", 4, raising=False)
    monkeypatch.setattr(server._Server, "max_streams", 2, raising=False)
    root = tmp_path / "projects" / "egg"
    (root / "r001").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot", "source": "", "need": "egg"}), encoding="utf-8")
    (root / "r001" / "design.json").write_text(Design().dumps(), encoding="utf-8")
    (root / "r001" / "results.json").write_text(json.dumps({"rev": "r001", "parent": None, "summary": {}}), encoding="utf-8")
    studio = server.Studio(RobotProject(str(root)), str(tmp_path / "projects"))
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(studio, None))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(studio, {f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    socks = []

    def connect():
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        socks.append(s)
        return s

    yield studio, httpd, port, connect
    for s in socks:
        s.close()
    httpd.shutdown()
    httpd.server_close()


def request(connect, port, path):
    s = connect()
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    return s


def head(s) -> bytes:
    """The status line and headers of an answer (reads until the blank line, or until the server closes)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        data = s.recv(4096)
        if not data:
            break
        buf += data
    return buf


def closed_within(s, seconds) -> bool:
    s.settimeout(0.2)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if not s.recv(4096):
                return True
        except socket.timeout:
            continue
        except OSError:
            return True
    return False


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_a_client_that_trickles_its_headers_is_cut_off(live):
    studio, httpd, port, connect = live
    slow = connect()
    slow.sendall(f"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n".encode())
    t0 = time.monotonic()
    for _ in range(12):                                   # one byte every 0.25 s: each read is quick, the total isn't
        try:
            slow.sendall(b"X")
        except OSError:
            break
        if closed_within(slow, 0.25):
            break
    assert time.monotonic() - t0 < 2.5 and closed_within(slow, 1.0)
    idle = connect()                                      # and a connection that never says anything
    assert closed_within(idle, 2.5)
    assert wait_for(lambda: httpd.connections == 0)
    ok = request(connect, port, "/api/state")             # a normal request is untouched
    assert head(ok).startswith(b"HTTP/1.0 200")


def test_event_streams_stay_open_past_the_header_deadline(live):
    studio, httpd, port, connect = live
    es = request(connect, port, "/api/events")
    assert b"200" in head(es).split(b"\r\n")[0]
    time.sleep(2.0)                                       # twice the header deadline
    studio.room().hub.send({"type": "assistant", "text": "still here"})
    es.settimeout(5)
    got = b""
    while b"still here" not in got:
        data = es.recv(4096)
        assert data, "the event stream was closed"
        got += data


def test_connections_past_the_cap_get_503_at_once(live):
    studio, httpd, port, connect = live
    streams = [request(connect, port, "/api/events") for _ in range(2)]
    assert all(b" 200 " in head(s).split(b"\r\n")[0] + b" " for s in streams)
    third = request(connect, port, "/api/events")          # past the stream cap: a 503, and the slot is free again
    assert head(third).startswith(b"HTTP/1.0 503")
    assert wait_for(lambda: httpd.streams == 2 and httpd.connections == 2)
    idle = [connect() for _ in range(2)]                  # 2 streams + 2 connections that haven't asked yet = the cap
    assert wait_for(lambda: httpd.connections == 4)
    t0 = time.monotonic()
    extra = request(connect, port, "/api/state")
    answer = head(extra)
    assert answer.startswith(b"HTTP/1.0 503") and b"Retry-After" in answer and time.monotonic() - t0 < 1.0
    for s in idle:
        s.close()
    assert wait_for(lambda: httpd.connections == 2)
    assert head(request(connect, port, "/api/state")).startswith(b"HTTP/1.0 200")   # room again
    for s in streams:
        s.close()
    assert wait_for(lambda: httpd.streams == 0 and httpd.connections == 0, timeout=5)   # closed tabs free their slots
