"""Append-only files survive a cut-off last line (ROB-4): a crash or a full disk can stop an append half way, leaving a
last line with no newline. The next record must start on a line of its own (never joined to the torn one and lost with
it), readers skip what doesn't parse, and the hash-chained audit logs call such a line cut off by a crash or a full disk,
not tampering."""

import hashlib
import json
import os

from morph.pipeline import Project
from morph.services import durable, robust
from morph.studio.rooms import Hub
from morph.studio.teams import audit as team_audit
from morph.studio.teams import cli as team_cli
from morph.studio.teams.store import append_jsonl, read_jsonl
from morph.studio.toolbox import audit as tool_audit

TORN = '{"type": "assistant", "text": "The reach is 39'


def _tear(path, text=TORN):
    with open(path, "a", encoding="utf-8", newline="\n") as fh:  # what an append stopped half way leaves: no newline
        fh.write(text)


def test_append_line_ends_a_cut_off_line_first(tmp_path):
    p = str(tmp_path / "x.jsonl")
    assert durable.append_line(p, '{"a": 1}') is False            # a new file
    _tear(p, '{"a": 2, "b"')
    assert durable.append_line(p, '{"a": 3}') is True             # the torn line was ended first
    assert durable.append_line(p, b'{"a": 4}') is False
    assert open(p, "rb").read() == b'{"a": 1}\n{"a": 2, "b"\n{"a": 3}\n{"a": 4}\n'


def test_a_chat_message_after_a_torn_line_is_kept(tmp_path):
    path = tmp_path / ".morph" / "chat.jsonl"
    hub = Hub("p", str(path))
    hub.send({"type": "user", "text": "question number 6: what is the reach?"})
    _tear(path)
    hub.send({"type": "user", "text": "please make the quantity 9"})
    texts = [e["text"] for e in Hub("p", str(path)).history]
    assert texts == ["question number 6: what is the reach?", "please make the quantity 9"]


def test_a_ledger_entry_after_a_torn_line_is_kept_and_the_ledger_still_reads(tmp_path):
    pr = Project(str(tmp_path / "p"))
    pr.log({"type": "revision", "rev": "r001"})
    _tear(pr.ledger_path, '{"ts": "2026-10-14T10:00:00Z", "type": "tool_ti')
    assert [e["rev"] for e in pr.entries() if e.get("type") == "revision"] == ["r001"]   # a torn line alone: still readable
    pr.log({"type": "revision", "rev": "r002"})
    with open(pr.ledger_path, "a", encoding="utf-8") as fh:
        fh.write("[1, 2]\n")                                      # not an entry: skipped, never a crash
    assert [e["rev"] for e in pr.entries() if e.get("type") == "revision"] == ["r001", "r002"]


def test_the_time_card_survives_a_torn_ledger_line(tmp_path):
    from morph.robot import timestudy2
    pr = Project(str(tmp_path / "p"))
    pr.log({"type": "human_time", "rev": "r001", "minutes": 5.0})
    _tear(pr.ledger_path, '{"ts": "2026-10-14T10:00:00Z", "type": "tool_ti')
    card = timestudy2.project(pr, timestudy2.load_baseline())
    assert "error" not in card


def test_a_study_row_after_a_torn_line_is_kept(tmp_path):
    p = str(tmp_path / "runs.jsonl")
    for i in range(3):
        robust._append(p, {"i": i})
    _tear(p, '{"i":3,"ok":tr')
    robust._append(p, {"i": 4})
    robust._append(p, {"i": 5})
    assert [r["i"] for r in robust.read_rows(p)] == [0, 1, 2, 4, 5]


def test_a_team_record_after_a_torn_line_is_kept(tmp_path):
    p = str(tmp_path / "comments.jsonl")
    append_jsonl(p, {"op": "add", "id": "c1"})
    _tear(p, '{"op":"add","id":"c2","bo')
    append_jsonl(p, {"op": "add", "id": "c3"})
    assert [o["id"] for o in read_jsonl(p)] == ["c1", "c3"]


def test_a_line_cut_inside_a_character_hides_nothing_else(tmp_path):
    p = str(tmp_path / "comments.jsonl")
    append_jsonl(p, {"op": "add", "id": "c1", "body": "Grip force 2 N → 1.5 N"})
    with open(p, "ab") as fh:
        fh.write('{"op":"add","id":"c2","body":"→'.encode("utf-8")[:-1])   # half of a three-byte character
    append_jsonl(p, {"op": "add", "id": "c3"})
    assert [o["id"] for o in read_jsonl(p)] == ["c1", "c3"]


def test_a_tool_request_after_a_torn_line_is_kept(tmp_path):
    from morph.studio import agents
    from morph.studio.toolbox import store
    home = str(tmp_path / "home")
    folder = store.tools_dir(home)
    os.makedirs(folder)
    path = os.path.join(folder, "requests.jsonl")
    _tear(path, '{"need": "a tor')
    ctx = {"home": home, "need_tool": lambda need, why: {"at": "2026-10-14T10:00:00Z", "need": need, "why": why}}
    assert agents._tools_request({"need": "a torque sensor price", "why": "the BOM"}, ctx)["noted"]
    assert [r["need"] for r in read_jsonl(path)] == ["a torque sensor price"]


# ------------------------------------------------------------------ the hash-chained audit logs


def _teams(home):
    for i in range(4):
        team_audit.append("policy_changed", by=f"m{i:010x}", home=home, keys=[f"k{i}"])
    assert team_audit.verify(home) == {"ok": True, "lines": 4, "line": None, "error": None}


def test_the_team_audit_log_calls_a_torn_line_a_crash_and_keeps_the_next_entry(tmp_path, capsys):
    home = str(tmp_path / "projects")
    _teams(home)
    torn = '{"ts":"2026-10-14T12:30:00Z","event":"invite_created","by":"mc4950f533c","inv'
    _tear(team_audit.path(home), torn)
    v = team_audit.verify(home)                                    # cut off and not yet followed by anything
    assert v["ok"] and v["cut"] == [5] and "crash or a full disk" in v["note"], v
    team_audit.append("sign_in", by="me90a1db897", home=home, via="link")
    v = team_audit.verify(home)
    assert v["ok"] and v["lines"] == 5 and v["cut"] == [5], v
    rows = team_audit.entries(home, limit=10)
    assert [r["event"] for r in rows[:2]] == ["sign_in", "policy_changed"] and not any(r.get("tamper") for r in rows)
    assert rows[0]["cut"] == {"bytes": len(torn), "sha256": hashlib.sha256(torn.encode()).hexdigest()}
    assert team_cli.run(type("A", (), {"team_cmd": "audit", "verify": True, "home": home})()) == 0
    out = capsys.readouterr().out
    assert "Audit log OK: 5 entries" in out and "line 5 was cut off by a crash or a full disk" in out
    team_audit.append("reset", by=None, home=home)                # and the chain goes on
    assert team_audit.verify(home)["ok"]


def test_a_cut_off_line_edited_afterwards_is_still_caught(tmp_path):
    home = str(tmp_path / "projects")
    _teams(home)
    _tear(team_audit.path(home), '{"ts":"2026-10-14T12:30:00Z","event":"invite_cre')
    team_audit.append("sign_in", by="me90a1db897", home=home, via="link")
    raw = open(team_audit.path(home), "rb").read()
    open(team_audit.path(home), "wb").write(raw.replace(b"invite_cre", b"invite_xyz"))   # the cut-off bytes are chained too
    v = team_audit.verify(home)
    assert not v["ok"] and v["line"] == 5, v
    open(team_audit.path(home), "wb").write(raw.replace(b'{"ts":"2026-10-14T12:30:00Z","event":"invite_cre\n', b""))
    v = team_audit.verify(home)                                    # the cut-off line removed: the next entry says it was there
    assert not v["ok"] and v["line"] == 5 and "cut off" in v["error"], v


def test_garbage_in_the_middle_is_still_reported_as_an_edit(tmp_path):
    home = str(tmp_path / "projects")
    _teams(home)
    lines = open(team_audit.path(home), "rb").read().split(b"\n")
    lines.insert(2, b'{"not": "chained"')
    open(team_audit.path(home), "wb").write(b"\n".join(lines))
    v = team_audit.verify(home)
    assert not v["ok"] and v["line"] == 3 and "not valid JSON" in v["error"], v


def test_a_stop_between_an_entry_and_audit_head_is_no_tampering(tmp_path):
    """The entry is written, then audit.head: a stop between the two leaves a whole, chained entry that audit.head
    doesn't count yet."""
    home = str(tmp_path / "projects")
    _teams(home)
    head = open(os.path.join(home, ".team", "audit.head"), "rb").read()
    team_audit.append("sign_in", by="me90a1db897", home=home, via="link")
    open(os.path.join(home, ".team", "audit.head"), "wb").write(head)   # as if the Studio stopped before updating it
    assert team_audit.verify(home)["ok"]
    team_audit.append("reset", by=None, home=home)
    v = team_audit.verify(home)
    assert v["ok"] and v["lines"] == 6 and not any(e.get("tamper") for e in team_audit.entries(home)), v


def test_the_tools_audit_log_handles_a_torn_line_the_same_way(tmp_path):
    home = str(tmp_path / "home")
    for i in range(3):
        tool_audit.append("tool_call", home=home, tool=f"t{i}", ok=True)
    _tear(tool_audit.path(home), '{"ts":"2026-10-14T12:30:00Z","event":"tool_ca')
    tool_audit.append("run_done", home=home, run="run_1")
    v = tool_audit.verify(home)
    assert v["ok"] and v["lines"] == 4 and v["cut"] == [4], v
    assert [e["event"] for e in tool_audit.entries(home)][:2] == ["run_done", "tool_call"]
    assert not any(e.get("tamper") for e in tool_audit.entries(home))
    with open(tool_audit.path(home), encoding="utf-8") as fh:
        assert json.loads(fh.read().splitlines()[-1])["event"] == "run_done"


def test_each_audit_entry_reaches_the_disk_before_audit_head_names_it(tmp_path, monkeypatch):
    """audit.head is synced to the disk; the entry it names must be there first, or a power cut leaves a head ahead of
    the log and `morph team audit --verify` calls it tampering ("the log ends early")."""
    home = str(tmp_path / "projects")
    _teams(home)
    logs = {os.path.join(home, ".team", "audit.head"): os.path.join(home, ".team", "audit.jsonl"),
            os.path.join(tmp_path, "tools", ".tools", "audit.head"): tool_audit.path(str(tmp_path / "tools"))}
    synced, checked = set(), []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        st = os.fstat(fd)
        synced.add((st.st_ino, st.st_size))
        return real_fsync(fd)

    def replace(src, dst, *a, **k):
        if os.path.abspath(dst) in logs:
            st = os.stat(logs[os.path.abspath(dst)])
            checked.append((st.st_ino, st.st_size) in synced)  # the log as it is now was synced
        return real_replace(src, dst, *a, **k)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    team_audit.append("sign_in", by="me90a1db897", home=home, via="link")
    tool_audit.append("tool_call", home=str(tmp_path / "tools"), tool="t", ok=True)
    tool_audit.append("run_done", home=str(tmp_path / "tools"), run="run_1")
    assert checked == [True, True, True]
