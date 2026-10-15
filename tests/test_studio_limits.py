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


def test_a_client_turned_away_at_the_cap_reads_its_whole_503(live):
    """Past the cap the Studio answers before the client has sent anything. If the request then reached a socket the
    Studio had already closed, the connection would be reset, and Windows throws away a 503 the client hasn't read yet.
    So the Studio keeps reading what a turned-away client sends for a moment before it closes."""
    studio, httpd, port, connect = live
    held = [connect() for _ in range(4)]                  # every slot taken by connections that haven't asked yet
    assert wait_for(lambda: httpd.connections == 4)
    late = connect()
    time.sleep(0.3)                                       # the 503 is on its way before the client says anything
    late.sendall(f"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    time.sleep(0.2)                                       # and the request reaches the Studio before the client reads
    got = b""
    while True:
        data = late.recv(4096)                            # a reset raises here (ConnectionResetError, WinError 10053)
        if not data:
            break
        got += data
    assert got.startswith(b"HTTP/1.0 503") and got.endswith(server.BUSY)
    assert late.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0   # no reset arrived (Linux reads past one)
    for s in held:
        s.close()


@pytest.mark.parametrize("path,body,gap", [("/api/team/join", b'{"code": "mi_x"}', 0.05),  # Teams is off: answered unread
                                           ("/api/nope", b'{"a": 1}', 0.6)])                 # refused, the body trails
def test_an_answer_given_before_the_body_arrives_is_not_reset_away(live, path, body, gap):
    """http.client sends headers and body in two writes, and on Windows a small body can trail its headers by a
    delayed ACK. An answer sent before reading it, then a close, would reset the connection: Windows then throws the
    answer away (CI saw ConnectionAbortedError on the 404 for /api/team/join)."""
    studio, httpd, port, connect = live
    s = connect()
    s.sendall(f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Morph-Client: 1\r\nContent-Type: application/json\r\n"
              f"Content-Length: {len(body)}\r\n\r\n".encode())
    time.sleep(gap)
    s.sendall(body)
    got = b""
    while True:
        data = s.recv(4096)
        if not data:
            break
        got += data
    assert got.startswith(b"HTTP/1.0 404")
    assert s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0   # no reset arrived (Linux reads past one)


# ------------------------------------------------------------------ one address can't take every slot (remote mode)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A remote Studio (access code "the-access-code") where 127.0.0.2 plays another computer: the cap per address
    applies to every address here (normally loopback is exempt), 3 unsigned connections per address, 12 in all."""
    try:
        with socket.socket() as probe:  # Linux and Windows answer on all of 127/8; macOS only on 127.0.0.1 by default
            probe.bind(("127.0.0.2", 0))
    except OSError:
        pytest.skip("this computer has no second loopback address (127.0.0.2) to play another computer")
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(server, "HEADER_TIMEOUT", 10.0, raising=False)
    monkeypatch.setattr(server, "MAX_UNSIGNED_PER_ADDRESS", 3, raising=False)
    monkeypatch.setattr(server._Server, "max_connections", 12, raising=False)
    monkeypatch.setattr(server._Server, "exempt", staticmethod(lambda address: False))
    root = tmp_path / "projects" / "egg"
    (root / "r001").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot", "source": "", "need": "egg"}), encoding="utf-8")
    (root / "r001" / "design.json").write_text(Design().dumps(), encoding="utf-8")
    (root / "r001" / "results.json").write_text(json.dumps({"rev": "r001", "parent": None, "summary": {}}), encoding="utf-8")
    studio = server.Studio(RobotProject(str(root)), str(tmp_path / "projects"))
    httpd = server._Server(("0.0.0.0", 0), server.make_handler(studio, None, "the-access-code"))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    socks = []

    def connect(source="127.0.0.1"):
        s = socket.create_connection(("127.0.0.1", port), timeout=5, source_address=(source, 0))
        socks.append(s)
        return s

    yield studio, httpd, port, connect
    for s in socks:
        s.close()
    httpd.shutdown()
    httpd.server_close()


def signed(connect, port, path, source="127.0.0.1"):
    s = connect(source)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Morph-Token: the-access-code\r\n\r\n".encode())
    return s


def test_idle_connections_from_one_address_leave_room_for_everyone_else(remote):
    studio, httpd, port, connect = remote
    held = [connect("127.0.0.2") for _ in range(3)]       # someone without the code holds what one address may
    assert wait_for(lambda: httpd.connections == 3)
    t0 = time.monotonic()
    more = [connect("127.0.0.2") for _ in range(9)]       # and every further one is turned away at once
    assert all(head(s).startswith(b"HTTP/1.0 503") for s in more) and time.monotonic() - t0 < 2.0
    assert wait_for(lambda: httpd.connections == 3)
    t0 = time.monotonic()
    ok = signed(connect, port, "/api/state")               # the person with the code is answered straight away
    assert head(ok).startswith(b"HTTP/1.0 200") and time.monotonic() - t0 < 1.0
    streams = [signed(connect, port, "/api/events") for _ in range(5)]   # signed-in tabs don't count against an address
    assert all(head(s).startswith(b"HTTP/1.0 200") for s in streams)
    assert wait_for(lambda: httpd.streams == 5 and httpd.unsigned_by == {"127.0.0.2": 3})
    nocode = [connect("127.0.0.1") for _ in range(3)]      # the cap is per address: this one has its own 3
    assert wait_for(lambda: httpd.unsigned_by == {"127.0.0.2": 3, "127.0.0.1": 3})
    assert head(connect("127.0.0.1")).startswith(b"HTTP/1.0 503")
    for s in held + nocode + streams:
        s.close()
    assert wait_for(lambda: httpd.connections == 0 and httpd.unsigned_by == {} and httpd.unsigned == {}, timeout=5)


def test_a_wrong_code_does_not_sign_a_connection_in(remote):
    studio, httpd, port, connect = remote
    s = connect("127.0.0.2")
    s.sendall(f"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Morph-Token: wrong-code-1\r\n\r\n".encode())
    assert head(s).startswith(b"HTTP/1.0 401")
    assert wait_for(lambda: httpd.connections == 0 and httpd.unsigned_by == {})


def test_loopback_addresses_have_no_cap_per_address():
    assert server._Server.exempt("127.0.0.1") and server._Server.exempt("127.0.0.2") and server._Server.exempt("::1")
    assert server._Server.exempt("::ffff:127.0.0.1")
    assert not server._Server.exempt("192.168.1.20") and not server._Server.exempt("10.0.0.5")
    assert not server._Server.exempt("::ffff:192.168.1.20") and not server._Server.exempt("")
