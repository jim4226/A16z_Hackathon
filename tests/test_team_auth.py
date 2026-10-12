"""Teams identity: the owner link, invites, signed sessions, revocation, device links, script tokens, throttling."""

import json
import os
import subprocess
import sys
import time

import pytest

from morph.studio.teams import auth, store
from morph.studio.teams.store import TeamStore
from test_team_util import make_team

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _fresh():
    auth.THROTTLE.clear()
    yield
    auth.THROTTLE.clear()


@pytest.fixture
def home(tmp_path):
    return str(tmp_path / "projects")


def _team_json(home):
    with open(os.path.join(home, ".team", "team.json"), encoding="utf-8") as fh:
        return fh.read()


def test_init_makes_the_creator_owner_and_generalist_with_private_files(home):
    r = auth.init_team("Jim", "R Cubed", home=home)
    m = r["member"]
    assert m["role"] == "owner" and m["disciplines"] == ["generalist"] and m["generalist"] and m["watch"]
    assert r["path"] == f"/?link={r['code']}" and r["code"].startswith("ml_")
    assert store.exists(home)
    if os.name != "nt":
        for f in ("team.json", "secret"):
            assert os.stat(os.path.join(home, ".team", f)).st_mode & 0o077 == 0, f
    with pytest.raises(auth.AuthError) as e:
        auth.init_team("Someone", home=home)
    assert e.value.status == 409
    member, cookie = auth.redeem_link(r["code"], home=home)
    assert member["id"] == m["id"] and auth.verify_session(cookie, home=home)["role"] == "owner"


def test_invites_are_single_use_hashed_and_carry_role_disciplines_and_name_hint(home):
    owner = auth.init_team("Jim", home=home)["member"]["id"]
    inv = auth.create_invite(owner, "editor", ["manufacturing", "electronics"], "Ana", home=home)
    code = inv["code"]
    assert code.startswith("mi_") and len(code) >= 3 + 22  # 128-bit code
    assert code not in _team_json(home)  # only its SHA-256 is stored
    assert inv["invite"]["disciplines"] == ["electronics", "manufacturing"] and inv["invite"]["state"] == "pending"
    st = auth.invite_status(code, home=home)
    assert st["role"] == "editor" and st["name_hint"] == "Ana" and st["invited_by"] == "Jim" and "hash" not in st
    m, cookie = auth.redeem_invite(code, "", home=home)  # empty name falls back to the hint
    assert m["name"] == "Ana" and m["role"] == "editor" and m["disciplines"] == ["electronics", "manufacturing"]
    assert auth.verify_session(cookie, home=home)["id"] == m["id"]
    with pytest.raises(auth.AuthError) as e:
        auth.redeem_invite(code, "Eve", home=home)
    assert e.value.status == 410 and "already used" in str(e.value)
    assert len(TeamStore(home).members()) == 2


def test_invites_expire_after_seven_days(home):
    owner = auth.init_team("Jim", home=home)["member"]["id"]
    t0 = time.time()
    inv = auth.create_invite(owner, "viewer", [], "Kim", home=home, now=t0)
    long = auth.create_invite(owner, "viewer", [], "Lee", home=home, days=30, now=t0)  # capped at 7 days
    with pytest.raises(auth.AuthError) as e:
        auth.redeem_invite(inv["code"], "Kim", home=home, now=t0 + 7 * 86400 + 1)
    assert e.value.status == 410 and "expired" in str(e.value)
    assert auth.invite_status(inv["code"], home=home, now=t0 + 8 * 86400)["state"] == "expired"
    m, _ = auth.redeem_invite(long["code"], "Lee", home=home, now=t0 + 6.9 * 86400)
    assert m["name"] == "Lee"
    assert auth.public_invite(TeamStore(home).read()["invites"][long["invite"]["id"]], t0)["expires_utc"] == \
        store.utc(t0 + 7 * 86400)


def test_cancelled_and_unknown_invites_are_refused(home):
    owner = auth.init_team("Jim", home=home)["member"]["id"]
    inv = auth.create_invite(owner, "editor", [], "", home=home)
    auth.revoke_invite(inv["invite"]["id"], owner, home=home)
    with pytest.raises(auth.AuthError) as e:
        auth.redeem_invite(inv["code"], "X", home=home)
    assert e.value.status == 410 and "cancelled" in str(e.value)
    for bad in ("mi_" + "A" * 22, "nope", "", None, "ml_" + "B" * 22):
        with pytest.raises(auth.AuthError):
            auth.redeem_invite(bad, "X", home=home)
    with pytest.raises(ValueError):
        auth.create_invite(owner, "admin", [], "", home=home)
    with pytest.raises(ValueError):
        auth.create_invite(owner, "editor", ["plumbing"], "", home=home)


def test_session_signature_expiry_and_shape(home):
    ids, cookies = make_team(home)
    c = cookies["Ana"]
    assert auth.verify_session(c, home=home)["name"] == "Ana"
    mid, issued, mac = c.split(".")
    assert auth.verify_session(f"{mid}.{issued}.{mac[:-1]}{'A' if mac[-1] != 'A' else 'B'}", home=home) is None
    assert auth.verify_session(f"{mid}.{int(issued) - 1}.{mac}", home=home) is None  # issued time is signed
    assert auth.verify_session(f"{ids['Raj']}.{issued}.{mac}", home=home) is None    # so is the member
    for junk in ("", "x", c + ".x", c.replace(".", ":"), "a" * 500, None, 5):
        assert auth.verify_session(junk, home=home) is None
    secret = TeamStore(home).secret()
    t = time.time()
    old = auth.sign_session(ids["Ana"], 0, secret, now=t - 31 * 86400)
    assert auth.verify_session(old, home=home, now=t) is None                      # 30-day sessions
    assert auth.verify_session(auth.sign_session(ids["Ana"], 0, secret, now=t - 29 * 86400), home=home, now=t)
    assert auth.verify_session(auth.sign_session(ids["Ana"], 0, secret, now=t + 3600), home=home, now=t) is None
    assert auth.verify_session(auth.sign_session(ids["Ana"], 0, b"x" * 32, now=t), home=home, now=t) is None


def test_revoking_bumps_the_epoch_and_ends_sessions_tokens_and_links(home):
    ids, cookies = make_team(home)
    tok = auth.create_token(ids["Ana"], "ci", home=home)["token"]
    link = auth.create_link(ids["Ana"], home=home)["code"]
    assert auth.verify_token(tok, home=home)["id"] == ids["Ana"]
    auth.revoke_member(ids["Ana"], ids["Jim"], home=home)
    assert auth.verify_session(cookies["Ana"], home=home) is None
    assert auth.verify_token(tok, home=home) is None
    with pytest.raises(auth.AuthError):
        auth.redeem_link(link, home=home)
    # a fresh cookie signed with the old epoch is refused too, and other members are untouched
    assert auth.verify_session(auth.sign_session(ids["Ana"], 0, TeamStore(home).secret()), home=home) is None
    assert auth.verify_session(cookies["Raj"], home=home)["name"] == "Raj"
    assert TeamStore(home).member(ids["Ana"])["name"] == "Ana"  # the name stays on past work


def test_sign_out_everywhere_ends_only_that_members_sessions(home):
    ids, cookies = make_team(home)
    auth.sign_out_everywhere(ids["Raj"], home=home)
    assert auth.verify_session(cookies["Raj"], home=home) is None
    assert auth.verify_session(cookies["Ana"], home=home)
    _, again = auth.redeem_link(auth.create_link(ids["Raj"], home=home)["code"], home=home)
    assert auth.verify_session(again, home=home)["id"] == ids["Raj"]


def test_sessions_survive_a_restart(home):
    """Nothing but the secret is needed: a brand-new Python process accepts the cookie."""
    ids, cookies = make_team(home)
    code = ("import json,sys; from morph.studio.teams import auth; "
            "print(json.dumps(auth.verify_session(sys.argv[1], home=sys.argv[2])))")
    out = subprocess.run([sys.executable, "-c", code, cookies["Ana"], home], cwd=ROOT, capture_output=True, text=True, timeout=60,
                         env={**os.environ, "PYTHONPATH": ROOT})
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["id"] == ids["Ana"]


def test_device_links_work_once_within_fifteen_minutes(home):
    ids, _ = make_team(home)
    t0 = time.time()
    ln = auth.create_link(ids["Raj"], home=home, now=t0)
    assert ln["code"] not in _team_json(home)
    assert auth.link_status(ln["code"], home=home, now=t0)["member"]["name"] == "Raj"
    late = auth.create_link(ids["Raj"], home=home, now=t0)
    with pytest.raises(auth.AuthError):
        auth.redeem_link(late["code"], home=home, now=t0 + 15 * 60 + 1)
    m, cookie = auth.redeem_link(ln["code"], home=home, now=t0 + 60)
    assert m["id"] == ids["Raj"] and auth.verify_session(cookie, home=home)
    with pytest.raises(auth.AuthError) as e:
        auth.redeem_link(ln["code"], home=home, now=t0 + 61)
    assert e.value.status == 410


def test_script_tokens_are_hashed_listed_without_secrets_and_revocable(home):
    ids, _ = make_team(home)
    t = auth.create_token(ids["Mo"], "nightly export", home=home)
    assert t["token"].startswith("mt_") and t["token"] not in _team_json(home)
    listed = auth.tokens(ids["Mo"], home=home)
    assert listed == [{"id": t["id"], "label": "nightly export", "created_utc": listed[0]["created_utc"]}]
    assert auth.verify_token(t["token"], home=home)["name"] == "Mo"
    assert auth.verify_token(t["token"] + "x", home=home) is None
    assert auth.revoke_token(ids["Mo"], t["id"], home=home)
    assert auth.verify_token(t["token"], home=home) is None


def test_wrong_codes_are_throttled_per_address(home):
    owner = auth.init_team("Jim", home=home)["member"]["id"]
    good = auth.create_invite(owner, "editor", [], "", home=home)["code"]
    for _ in range(auth.THROTTLE.limit):
        with pytest.raises(auth.AuthError):
            auth.redeem_invite("mi_" + "Z" * 22, "x", ip="10.0.0.9", home=home)
    with pytest.raises(auth.AuthError) as e:
        auth.redeem_invite(good, "x", ip="10.0.0.9", home=home)  # even a good code waits
    assert e.value.status == 429
    assert auth.verify_token("mt_" + "Q" * 43, ip="10.0.0.9", home=home) is None
    m, _ = auth.redeem_invite(good, "Ana", ip="10.0.0.10", home=home)  # other addresses are not affected
    assert m["name"] == "Ana"
    assert auth.THROTTLE.wait("10.0.0.9", now=time.time() + auth.THROTTLE.window + 1) == 0


def test_the_team_always_keeps_an_owner(home):
    ids, _ = make_team(home)
    with pytest.raises(auth.AuthError):
        auth.update_member(ids["Jim"], ids["Jim"], role="editor", home=home)
    with pytest.raises(auth.AuthError):
        auth.revoke_member(ids["Jim"], ids["Jim"], home=home)
    auth.update_member(ids["Ana"], ids["Jim"], role="owner", home=home)
    assert auth.update_member(ids["Jim"], ids["Jim"], role="editor", home=home)["role"] == "editor"


def test_names_are_cleaned_and_kept_unique(home):
    ids, _ = make_team(home)
    owner = ids["Jim"]
    for raw, want in (("  Ana  ", "Ana 2"), ("<b>Bob</b>\x00", "bBob/b"), ("Zoë Q", "Zoë Q")):
        m, _ = auth.redeem_invite(auth.create_invite(owner, "viewer", [], "", home=home)["code"], raw, home=home)
        assert m["name"] == want
    with pytest.raises(ValueError):
        auth.redeem_invite(auth.create_invite(owner, "viewer", [], "", home=home)["code"], "   ", home=home)


def test_member_updates_and_policy_are_validated_and_audited(home):
    from morph.studio.teams import audit

    ids, _ = make_team(home)
    m = auth.update_member(ids["Raj"], ids["Jim"], tags=["manufacturing", "sourcing"], home=home)
    assert m["disciplines"] == ["manufacturing", "sourcing"]
    with pytest.raises(ValueError):
        auth.update_member(ids["Raj"], ids["Jim"], tags=["wizardry"], home=home)
    pol = auth.set_policy({"viewers_can_propose": False, "specialist_required": {"production": ["electronics", "generalist"]}},
                          ids["Jim"], home=home)
    assert pol["viewers_can_propose"] is False and pol["specialist_required"]["production"] == ["electronics"]
    for bad in ({"viewers_can_propose": "yes"}, {"unreleased_downloads": "anyone"}, {"nope": True}, {}):
        with pytest.raises(ValueError):
            auth.set_policy(bad, ids["Jim"], home=home)
    events = [e["event"] for e in audit.entries(home)]
    assert "disciplines_changed" in events and "policy_changed" in events


def test_public_views_never_carry_secrets(home):
    ids, _ = make_team(home)
    auth.create_token(ids["Ana"], "x", home=home)
    raw = TeamStore(home).read()
    for m in raw["members"].values():
        pub = auth.public_member(m)
        assert not ({"epoch", "tokens", "hash"} & set(pub))
    for inv in raw["invites"].values():
        assert "hash" not in auth.public_invite(inv)
