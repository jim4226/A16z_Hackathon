"""Every test runs offline and keyless: API keys from the developer's environment are removed, settings live
in a per-test MORPH_HOME, and any connection to a non-loopback address fails the test (the connection is
refused and the test is failed at teardown, even when the code under test swallows the error). A test that
really needs the network says so with @pytest.mark.network."""

import ipaddress
import os
import socket

import pytest

SECRETS = ("ANTHROPIC_", "KEEWANO_", "SUBQ_", "GEMINI_", "GOOGLE_API_KEY", "OPENAI_API_KEY", "MORPH_TOKEN",
           "MORPH_TELEMETRY_ENABLED", "MORPH_OFFLINE", "MORPH_LOAD_DOTENV")


def pytest_configure(config):
    config.addinivalue_line("markers", "network: the test may open connections beyond this computer")


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


@pytest.fixture(autouse=True)
def _offline(request, monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(SECRETS):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MORPH_HOME", str(tmp_path / "morph-home"))
    from morph.importers import common as imports
    monkeypatch.setattr(imports, "_HOME", None)  # a Studio sets the importers' home to its projects folder: not across tests
    if request.node.get_closest_marker("network"):
        yield
        return
    attempts = []
    connect, connect_ex, create_connection = socket.socket.connect, socket.socket.connect_ex, socket.create_connection

    def check(address):
        if not _loopback(address):
            attempts.append(address)
            raise NetworkBlocked(f"test tried to reach {address!r}; mark it @pytest.mark.network if it must")

    def guarded_connect(self, address):
        check(address)
        return connect(self, address)

    def guarded_connect_ex(self, address):
        check(address)
        return connect_ex(self, address)

    def guarded_create_connection(address, *args, **kwargs):
        check(address)
        return create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    yield
    if attempts:
        pytest.fail(f"non-loopback network connection attempted: {attempts}")
