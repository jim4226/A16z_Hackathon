"""Run tests with external DNS and sockets blocked, while allowing loopback server tests."""
import ipaddress
import os
from pathlib import Path
import socket
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def local(host):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host in {None, "", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main():
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo
    original_sendto = socket.socket.sendto

    def check(address):
        if isinstance(address, tuple) and not local(address[0]):
            raise OSError("External network access is disabled during tests")

    def connect(self, address):
        check(address)
        return original_connect(self, address)

    def connect_ex(self, address):
        check(address)
        return original_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if not local(host):
            raise OSError("External DNS is disabled during tests")
        return original_getaddrinfo(host, *args, **kwargs)

    def sendto(self, data, *args):
        check(args[-1])
        return original_sendto(self, data, *args)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo
    socket.socket.sendto = sendto
    for name in ("ANTHROPIC_API_KEY", "KEEWANO_API_KEY", "KEEWANO_PAT", "KEEWANO_PROJECT_ID",
                 "MORPH_TELEMETRY_ENABLED", "MORPH_OFFLINE"):
        os.environ.pop(name, None)
    os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
    with tempfile.TemporaryDirectory(prefix="rcubed-tests-") as directory:
        os.environ["MORPH_CONFIG_DIR"] = str(Path(directory) / "config")
        os.environ["MORPH_HOME"] = str(Path(directory) / "projects")
        import pytest
        return pytest.main(sys.argv[1:] or ["-q"])


if __name__ == "__main__":
    raise SystemExit(main())
