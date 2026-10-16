"""Every test runs offline and keyless: API keys from the developer's environment are removed, settings live
in a per-test MORPH_HOME, and any connection to a non-loopback address fails the test (the connection is
refused and the test is failed at teardown, even when the code under test swallows the error). A test that
really needs the network says so with @pytest.mark.network.

The guard stays on for the whole run. A thread a test started that reaches out after that test ended (a
Studio request still being answered, a lookup in the background) is refused too, and named in a warning
with the test that started it: it fails neither that test, which has passed, nor the one running then."""

import ipaddress
import os
import socket
import threading
import warnings

import pytest

SECRETS = ("ANTHROPIC_", "KEEWANO_", "SUBQ_", "GEMINI_", "GOOGLE_API_KEY", "OPENAI_API_KEY", "MORPH_TOKEN",
           "MORPH_TELEMETRY_ENABLED", "MORPH_OFFLINE", "MORPH_LOAD_DOTENV", "MORPH_TOOL_")


def pytest_configure(config):
    config.addinivalue_line("markers", "network: the test may open connections beyond this computer")
    config.addinivalue_line("markers", "live: needs real keys and MORPH_LIVE=1; never part of a normal run")
    _guard()


def _loopback(address) -> bool:
    if not isinstance(address, tuple):  # AF_UNIX path
        return True
    host = address[0].decode() if isinstance(address[0], bytes) else str(address[0])
    host = host.strip("[]").split("%")[0].lower()
    if host in ("localhost", "localhost.", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a host name: resolving it is already network access


class NetworkBlocked(ConnectionRefusedError):
    pass


class StrayConnection(UserWarning):
    pass


# the test running now (None between tests), whether it may use the network, and what it tried
RUN = {"test": None, "network": False, "attempts": []}


def _owner(starter: threading.Thread):
    """The test a thread `starter` starts now belongs to: the running test, or, when `starter` is one of Morph's own
    background threads (morph-after-*, morph-room-*, morph-lookup-*, ...), the test that started it. A test's server
    answers each request on a thread its serve_forever thread starts: that one belongs to the running test."""
    if starter is not threading.main_thread() and starter.name.startswith("morph-"):
        mine = getattr(starter, "_morph_test", None)
        if mine is not None:
            return mine
    return RUN["test"]


def _guard():
    """Refuse non-loopback connections for the whole run. Each thread remembers the test it was started in; one
    that outlives its test is refused with a warning naming that test, never charged to the test running now. A
    thread one of Morph's own background threads starts (a build's follow-up starting its lookup pool) belongs to
    that thread's test, whenever it starts."""
    if getattr(socket.socket.connect, "_morph_guard", False):
        return
    connect, connect_ex, create_connection, start = (socket.socket.connect, socket.socket.connect_ex,
                                                     socket.create_connection, threading.Thread.start)

    def started(self, *a, **k):
        self._morph_test = _owner(threading.current_thread())
        return start(self, *a, **k)

    def check(address):
        if _loopback(address):
            return
        me, now = threading.current_thread(), RUN["test"]
        origin = now if me is threading.main_thread() else getattr(me, "_morph_test", None)
        if now is not None and origin in (now, None):
            if RUN["network"]:
                return
            RUN["attempts"].append(address)
            raise NetworkBlocked(f"test tried to reach {address!r}; mark it @pytest.mark.network if it must")
        warnings.warn(f"a thread started by {origin or 'no test'} tried to reach {address!r} after that test ended; "
                      "refused", StrayConnection, stacklevel=3)
        raise NetworkBlocked(f"{address!r} refused: the test that started this thread ({origin}) has ended")

    def guarded_connect(self, address):
        check(address)
        return connect(self, address)

    def guarded_connect_ex(self, address):
        check(address)
        return connect_ex(self, address)

    def guarded_create_connection(address, *args, **kwargs):
        check(address)
        return create_connection(address, *args, **kwargs)

    guarded_connect._morph_guard = True
    threading.Thread.start = started
    socket.socket.connect, socket.socket.connect_ex = guarded_connect, guarded_connect_ex
    socket.create_connection = guarded_create_connection


@pytest.fixture(autouse=True)
def _offline(request, monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(SECRETS):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MORPH_HOME", str(tmp_path / "morph-home"))
    from morph.importers import common as imports
    monkeypatch.setattr(imports, "_HOME", None)  # a Studio sets the importers' home to its projects folder: not across tests
    from morph.studio import security
    monkeypatch.setattr(security, "ACCESS_CODES", set())  # a remote Studio's access code: not across tests
    network = request.node.get_closest_marker("network") is not None
    if not network:
        for k in list(os.environ):  # a proxy on loopback would carry any request past the check below
            if k.lower() in ("http_proxy", "https_proxy", "all_proxy"):
                monkeypatch.delenv(k, raising=False)
    attempts = []
    RUN.update(test=request.node.nodeid, network=network, attempts=attempts)
    try:
        yield
    finally:
        RUN.update(test=None, network=False, attempts=[])
    if attempts:
        pytest.fail(f"non-loopback network connection attempted: {attempts}")
