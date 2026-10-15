"""An upload cut off by a stopped Studio never leaves an import session claiming a file it doesn't have (ROB-12): a file
counts once it is all there; what is being streamed is only reserved, and a Studio that starts again clears the
half-written file and the reservation, so the session's status and detect tell the truth."""

import io
import json
import os

import pytest

from morph.importers import ImportFailed, session as sess
from test_import_fixtures import egg_stl


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "projects"
    h.mkdir()
    monkeypatch.setenv("MORPH_HOME", str(h))
    from morph.importers import common
    monkeypatch.setattr(common, "_HOME", None)
    return str(h)


class Stalls(io.RawIOBase):
    """A request body that sends `n` bytes, then the Studio is stopped (the stream raises)."""

    def __init__(self, n):
        self.left = n

    def read(self, size=-1):
        if self.left <= 0:
            raise KeyboardInterrupt  # what a Ctrl+C mid-read looks like to the copying thread
        k = min(self.left, size if size and size > 0 else self.left)
        self.left -= k
        return b"x" * k


def test_a_file_counts_only_once_it_is_all_there(home):
    s = sess.Session.create(home)
    seen = []
    real = s._save
    s._save = lambda st: (seen.append(json.loads(json.dumps(st))), real(st))[1]
    with pytest.raises(KeyboardInterrupt):
        s.add_file("big.stl", Stalls(3 << 20), 40 << 20)
    assert all("big.stl" not in st["files"] for st in seen)              # never claimed while it streamed
    st = s.state()
    assert st["files"] == {} and st["bytes"] == 0 and not st.get("pending")
    assert not [n for n in os.listdir(s.files) if ".part-" in n]


def test_a_studio_stopped_mid_upload_leaves_nothing_claimed_after_a_restart(home, tmp_path):
    s = sess.Session.create(home)
    path = egg_stl(str(tmp_path / "egg.stl"))
    with open(path, "rb") as fh:
        s.add_file("egg.stl", fh, os.path.getsize(path))
    st = s.state()                                                       # what a SIGKILL mid-stream leaves on disk:
    st.setdefault("pending", {})["deadbeef"] = ["big.stl", 40 << 20]     # a reservation and a half-written file
    s._save(st)
    with open(os.path.join(s.files, "big.stl.part-deadbeef"), "wb") as fh:
        fh.write(b"x" * (12 << 20))
    old = sess.Session.create(home)                                      # an older session: the file was recorded first
    st = old.state()
    st["files"]["arm.stl"], st["bytes"] = 30 << 20, 30 << 20
    old._save(st)
    with open(os.path.join(old.files, "arm.stl.part-0b240937"), "wb") as fh:
        fh.write(b"x" * 1000)
    assert sess.cleanup(home) == 0                                       # the Studio starts: both kept, both tidied
    st = s.state()
    assert st["files"] == {"egg.stl": os.path.getsize(path)} and not st.get("pending")
    assert sorted(os.listdir(s.files)) == ["egg.stl"]
    st = old.state()
    assert st["files"] == {} and st["bytes"] == 0 and os.listdir(old.files) == []
    with pytest.raises(ImportFailed, match="Nothing to import yet"):   # it said "found nothing it can import here"
        old.detect()
    det = s.detect()
    assert any(c["kind"] == "object" for c in det["candidates"])


def test_reservations_still_count_against_the_limits(home, monkeypatch):
    from morph.importers import common
    monkeypatch.setitem(common.LIMITS, "upload_total", 10 << 20)
    s = sess.Session.create(home)
    st = s.state()
    st["pending"] = {"aaaa0000": ["a.stl", 8 << 20]}                    # another upload under way
    s._save(st)
    with pytest.raises(ImportFailed, match="over the 10 MB limit"):
        s.add_file("b.stl", io.BytesIO(b"x" * (3 << 20)), 3 << 20)


def test_an_upload_another_running_studio_streams_keeps_its_reservation(home, tmp_path):
    """Two Studios on one projects folder: the one starting tidies the import sessions, but an upload the other one is
    still streaming (its lock is held, rooms.claim) keeps its reservation and its part file; a stopped one's go."""
    from test_restart_recovery import hold
    s = sess.Session.create(home)
    st = s.state()
    st["pending"] = {"0badc0de": ["live.stl", 4 << 20, "feedfacecafebeef"],     # the other Studio, still running
                     "deadbeef": ["dead.stl", 4 << 20, "0123456789abcdef"]}     # a Studio that has stopped
    s._save(st)
    for tok, name in (("0badc0de", "live.stl"), ("deadbeef", "dead.stl")):
        with open(os.path.join(s.files, f"{name}.part-{tok}"), "wb") as fh:
            fh.write(b"x" * 1000)
    other = hold(os.path.join(home, ".studios", "feedfacecafebeef.lock"))
    try:
        sess.cleanup(home)
        assert list(s.state()["pending"]) == ["0badc0de"] and os.listdir(s.files) == ["live.stl.part-0badc0de"]
    finally:
        other.stdin.close()
        other.wait(10)
    sess.cleanup(home)                                                       # it stopped too: now it goes
    assert not s.state().get("pending") and os.listdir(s.files) == []
