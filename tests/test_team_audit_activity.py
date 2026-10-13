"""Teams audit log (hash chain, tamper detection), the activity feed, inboxes with mentions, presence, telemetry ids."""

import json
import os
import time

import pytest

from morph.studio.teams import activity, audit, auth, collab
from test_team_util import build, make_team, members


@pytest.fixture(autouse=True)
def _fresh():
    auth.THROTTLE.clear()


def _log(home):
    return os.path.join(home, ".team", "audit.jsonl")


def _lines(home):
    with open(_log(home), "rb") as fh:
        return fh.read().split(b"\n")[:-1]


def _write(home, lines):
    with open(_log(home), "wb") as fh:
        fh.write(b"".join(ln + b"\n" for ln in lines))


@pytest.fixture
def logged(tmp_path):
    home = str(tmp_path / "projects")
    for i in range(6):
        audit.append("policy_changed", by=f"m{i:010x}", home=home, keys=[f"k{i}"])
    assert audit.verify(home) == {"ok": True, "lines": 6, "line": None, "error": None}
    return home


def test_audit_detects_an_edited_line(logged):
    lines = _lines(logged)
    lines[2] = lines[2].replace(b"k2", b"k9")
    _write(logged, lines)
    v = audit.verify(logged)
    assert not v["ok"] and v["line"] == 4 and "edited, removed or moved" in v["error"]


def test_audit_detects_a_deleted_line(logged):
    lines = _lines(logged)
    del lines[3]
    _write(logged, lines)
    v = audit.verify(logged)
    assert not v["ok"] and v["line"] == 4


def test_audit_detects_reordered_lines(logged):
    lines = _lines(logged)
    lines[1], lines[2] = lines[2], lines[1]
    _write(logged, lines)
    assert audit.verify(logged)["line"] == 2


def test_audit_detects_an_edited_or_dropped_last_line(logged):
    lines = _lines(logged)
    _write(logged, lines[:-1])
    assert not audit.verify(logged)["ok"]          # truncated: audit.head remembers 6 lines
    lines[-1] = lines[-1].replace(b"k5", b"k7")
    _write(logged, lines)
    v = audit.verify(logged)
    assert not v["ok"] and "last line changed" in v["error"]
    audit.append("reset", by=None, home=logged)    # appending afterwards doesn't launder it
    assert not audit.verify(logged)["ok"]


def test_audit_detects_a_deleted_head_and_garbage(logged):
    os.remove(os.path.join(logged, ".team", "audit.head"))
    assert "audit.head is missing" in audit.verify(logged)["error"]
    audit.append("reset", by=None, home=logged)
    assert not audit.verify(logged)["ok"]


def test_audit_entries_are_utc_newest_first_and_never_hold_secrets(tmp_path):
    home = str(tmp_path / "projects")
    audit.append("keys_changed", by="m0123456789", home=home, names=["ANTHROPIC_API_KEY"], value="sk-ant-secret", code="mi_x",
                 token="mt_y", nested={"secret": "s", "ok": 1})
    audit.append("export", by="m0123456789", home=home, project="egg-picker", rev="r003")
    rows = audit.entries(home)
    assert [r["event"] for r in rows] == ["export", "keys_changed"]
    assert rows[1]["names"] == ["ANTHROPIC_API_KEY"] and rows[1]["nested"] == {"ok": 1}
    raw = open(_log(home), encoding="utf-8").read()
    assert "sk-ant" not in raw and "mi_x" not in raw and "mt_y" not in raw
    assert all(r["ts"].endswith("Z") for r in rows) and "prev" not in rows[0]
    assert audit.verify(home)["ok"]


def test_team_actions_land_in_the_audit_log(tmp_path):
    home = str(tmp_path / "projects")
    ids, _ = make_team(home)
    auth.revoke_member(ids["Vic"], ids["Jim"], home=home)
    events = [e["event"] for e in audit.entries(home, limit=1000)]
    for ev in ("team_created", "link_created", "sign_in", "invite_created", "member_revoked"):
        assert ev in events, ev
    assert audit.verify(home)["ok"]


# ------------------------------------------------------------------ activity and inbox


def test_activity_merges_ledger_chat_comments_proposals_reviews_and_releases_in_time_order(tmp_path):
    home = str(tmp_path / "projects")
    root = os.path.join(home, "egg-picker")
    ids, _ = make_team(home)
    ms = members(home)
    build(root, "r001", by=ids["Jim"], label="first design", ts="2026-10-11T08:00:00")      # an old local-time ledger line
    build(root, "r002", "r001", {"fab.process": "cnc_router"}, by=ids["Raj"], label="CNC", ts="2026-10-12T09:00:00Z")
    os.makedirs(os.path.join(root, ".morph"))
    with open(os.path.join(root, ".morph", "chat.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "text": "make it cheaper", "by": ids["Ana"], "ts": "2026-10-12T09:30:00Z"}) + "\n")
        fh.write(json.dumps({"type": "assistant", "text": "ok", "ts": "2026-10-12T09:30:05Z"}) + "\n")
    c = collab.add_comment(root, ids["Ana"], "Check the bracket", {"rev": "r002", "kind": "stage", "key": "cad"})
    p = collab.create_proposal(root, ids["Vic"], "Bigger fingers", {"finger.length_mm": 90}, members=ms)
    collab.decide(root, p["review"], ms[ids["Mo"]], "request_changes", members=ms)
    feed = activity.feed(root, ms, limit=50)
    kinds = [i["kind"] for i in feed]
    assert kinds[-3:] == ["chat", "revision", "revision"] and kinds[0] == "review"
    assert feed[-1]["text"].startswith("Jim made r001: first design")
    assert [i["t"] for i in feed] == sorted([i["t"] for i in feed], reverse=True)
    texts = " | ".join(i["text"] for i in feed)
    for want in ("Raj made r002: CNC", "Ana: make it cheaper", "Ana commented on r002 Robot CAD: Check the bracket",
                 "Vic opened change request #1: Bigger fingers", "Mo requested changes on change request #1 as Mechanical",
                 "asked for review of change request #1"):
        assert want in texts, want
    assert "ok" not in [i["text"] for i in feed]                      # assistant replies stay in the chat
    assert all(i["project"] == "egg-picker" for i in feed)
    older = activity.feed(root, ms, before=feed[1]["t"])
    assert older and all(i["t"] < feed[1]["t"] for i in older)
    assert c["id"] in {i["ref"].get("id") for i in feed}


def test_inbox_mentions_unread_counts_and_read_cursor(tmp_path):
    home = str(tmp_path / "projects")
    ids, _ = make_team(home)
    ms = members(home)
    ana, raj = ids["Ana"], ids["Raj"]
    who = collab.mentions("@Raj and @ana, also @Ana again; email raj@example.com", ms)
    assert who == [raj, ana]
    assert activity.notify(home, ana, "mention", "self", by=ana) is None          # never about your own action
    assert activity.notify(home, "../../etc", "mention", "x") is None
    for i in range(3):
        activity.notify(home, ana, "mention", f"Raj mentioned you #{i}", project="egg-picker", ref={"rev": "r001"}, by=raj)
    box = activity.inbox(home, ana)
    assert box["unread"] == 3 and [i["text"] for i in box["items"]][0] == "Raj mentioned you #2" and box["items"][0]["unread"]
    assert activity.mark_read(home, ana, upto=box["items"][-1]["seq"])["unread"] == 2
    assert activity.mark_read(home, ana)["unread"] == 0
    activity.notify(home, ana, "review_request", "Review please", by=raj)
    assert activity.unread(home, ana) == 1 and activity.unread(home, raj) == 0


def test_inbox_is_trimmed_but_sequence_numbers_keep_counting(tmp_path):
    home = str(tmp_path / "projects")
    mid = "m0123456789"
    for i in range(activity.INBOX_MAX + 5):
        activity.notify(home, mid, "job", f"job {i}")
    items = activity.inbox(home, mid, limit=1000)["items"]
    assert len(items) <= activity.INBOX_MAX and items[0]["text"] == f"job {activity.INBOX_MAX + 4}"
    assert items[0]["seq"] == activity.INBOX_MAX + 5


def test_presence_active_idle_and_gone():
    pr = activity.Presence()
    ms = {"a": {"id": "a", "name": "Ana", "role": "editor"}, "b": {"id": "b", "name": "Bo", "role": "viewer"},
          "x": {"id": "x", "name": "Gone", "role": "editor", "revoked": True}}
    t = time.time()
    pr.touch("egg", "a", tab="elec", rev="r003", now=t)
    pr.touch("egg", "b", now=t - 400)
    pr.touch("egg", "x", now=t)
    pr.touch("so101", "a", now=t)
    snap = pr.snapshot("egg", ms, now=t)
    assert [(s["name"], s["state"], s["tab"]) for s in snap] == [("Ana", "active", "elec"), ("Bo", "idle", None)]
    assert pr.snapshot("egg", ms, now=t + 2000) == []
    pr.connected("egg", "b", 1, now=t)
    assert [s["name"] for s in pr.snapshot("egg", ms, now=t + 2000)] == ["Bo"]   # an open tab keeps you listed
    pr.connected("egg", "b", -1)


def test_keewano_team_events_are_pseudonymous_and_in_the_teams_id_range():
    ids = [e["eventTypeId"] for e in activity.KEEWANO_EVENTS]
    assert all(2180 <= i <= 2229 for i in ids) and len(set(ids)) == len(ids)
    ev = activity.keewano_events({"type": "team", "action": "decide", "by": "m0123456789", "decision": "approve"}, b"s")
    names = dict(ev)
    assert names["ReviewDecision"] == "approve" and names["TeamActor"] != "m0123456789" and len(names["TeamActor"]) == 16
    assert activity.keewano_events({"type": "revision"}) == []
