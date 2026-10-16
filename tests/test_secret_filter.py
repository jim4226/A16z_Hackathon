"""One key filter for every free-text field (security.looks_like_secret): chat, New robot (the request and every typed
answer), Show Morph the job's cards, every Teams text field, a project's description and a world's labels refuse a pasted
API key or Morph access code before anything is stored, queued, shown to other tabs or sent to SubQ or Claude."""

import http.client
import json
import os
import time

import pytest

from morph.robot.pipeline2 import RobotProject
from morph.studio import agents, api_brief, api_team, security, server
from morph.studio.teams import activity, auth, collab
from test_studio_features import caller, start
from test_studio_security import make_project
from test_team_util import make_team

TOOL_VALUE = "a1b2c3d4e5f6g7h8"  # a saved MORPH_TOOL_* key with no prefix: found by its value
KEYS = {
    "claude": "sk-ant-api03-ABCDEFGH012345",
    "subq": "sk-live-9f3kd02kfiq0alz7vnm4xq",
    # keys in groups of one size: the word in front doesn't make them a phrase
    "subq_grouped": "sk-live-4f8a-91bc-77de-12ab", "subq_grouped_digits": "sk-test-1234-5678-90ab-cdef",
    "subq_grouped_pairs": "sk-subq-a1b2-c3d4-e5f6-a7b8-c9d0",
    "keewano": "kwpat-ABCDEFGH0123",
    "team_script_token": "mt_" + "Q" * 43,
    "team_invite": "mi_inviteCode0123456789ab",
    "team_sign_in_link": "ml_linkCode0123456789abcd",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFU",
    "saved_tool_key": TOOL_VALUE,
}
ORDINARY = ("make it thin-and-light", "use an sk-thin-and-light finger", "format_quantity in smt_placement_list",
            "the mt_ prefix", "Mouser part 511-STM32F103C8T6", "Swap to Dynamixel servos", "sort socks by colour",
            "a robot that picks strawberries, 30 per minute", "uuid 123e4567-e89b-12d3-a456-426614174000",
            # snake_case names and part names with a key's prefix: a key is random, these are words
            "rename mt_shoulder_joint_torque_limit to something shorter",
            "the firmware flag ml_inference_pipeline_enabled is off", "set mi_gripper_force_threshold_value lower",
            "use sk-6x40-stainless-steel-screws", "order sk-m3x12-din912-a2-70-stainless-steel bolts",
            "mount it on sk-2020-aluminium-extrusion-500mm", "SO101 kit: sk-SO101-follower-arm-kit-v2",
            "mt_servo2_torque_limit_value and ml_model_v2_inference_enabled", "mi_wrist_roll_offset_deg = 3",
            # chip, board and model names, and camelCase
            "flash mt_stm32f405_firmware_build to the board", "mi_esp32s3_wifi_module_config",
            "load ml_resnet50_backbone_weights", "ml_yolov8n_grasp_detector_v2", "ml_mobilenetv3_small_quantized",
            "mt_joint_limits_so101arm", "mt_dynamixel_xl330_m288_limits", "ml_PID_gains_for_joint3_wrist",
            "mt_shoulderJointTorqueLimit", "ml_InferencePipelineEnabled", "SKU sk-NEMA17-stepper-42x40mm",
            "sk-M3x12-DIN912-socket-head-cap", "order sk-2024-10-15-order-12345", "part sk-608zz-bearing-pack-of-10",
            "sk-2x-M3-nylon-standoffs-20mm-long", "sk-0402-100nF-capacitors-reel", "sk-1N4148-diodes-x100",
            # numbered names: a series (servo1_servo2_servo3, xl330_xl430_xm430) or a word and its number (arm2)
            "set mt_servo1_servo2_servo3_torque_limit", "ml_joint1_joint2_joint3_gains",
            "mt_left_knee_arm2_torque_limit", "ml_pose_left_arm1_offset_deg", "mt_hip2_knee_gain_value",
            "mt_xl330_xl430_xm430_limits", "mt_m3x12_m4x12_m5x12_screws", "mt_j1_j2_j3_j4_j5_j6_limits_deg",
            # a part mixing capitals, small letters and a number: a version (yoloV8), camelCase words and a number
            # (jointLimit2), a model and a word (so101Arm); ml_policyV1_final_weights is as long as an invite code
            "ml_yoloV8_grasp_detector", "ml_resnetV2_backbone_weights", "ml_gripperV2_checkpoint_final",
            "ml_policyV1_final_weights", "mt_so101Arm_follower_calib", "mt_servoID3_offset_degrees",
            "ml_GraspNet1B_weights_file", "mt_jointLimit2_shoulder_gain", "use sk-GraspNet1B-weights-file-v2",
            "mt_Servo1_torque_limit_value")


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """A saved tool key, every stage "pending" (instant), network lookups off, fresh brief and Teams state."""
    monkeypatch.setenv("MORPH_TOOL_ACME_KEY", TOOL_VALUE)
    monkeypatch.setattr(RobotProject, "_compute", lambda self, *a, **k: False)
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    monkeypatch.setattr(api_brief, "STORE", api_brief.Store())
    auth.THROTTLE.clear()
    activity.PRESENCE.seen.clear()
    yield
    api_team.configure(home=None, enabled=False)


def with_key(kind: str) -> str:
    return f"a robot that sorts socks; my key is {KEYS[kind]} by the way"


# ------------------------------------------------------------------ one definition


@pytest.mark.parametrize("kind", sorted(KEYS))
def test_chat_and_every_teams_field_use_one_filter(kind):
    text = with_key(kind)
    assert security.looks_like_secret(text), f"chat lets a {kind} through"
    with pytest.raises(collab.CollabError) as exc:
        collab._text(text, "comment", 4000)
    assert exc.value.status == 400 and "Keys dialog" in str(exc.value)
    with pytest.raises(ValueError):
        auth.clean_name(f"Ed {KEYS[kind]}"[:40] if kind != "jwt" else KEYS[kind])
    assert security.holds_secret({"answers": [{"name": "x"}, {"note": text}]})


def test_every_code_morph_makes_is_caught_and_snake_case_names_are_not(tmp_path):
    """Teams codes are random base64url of a fixed length with a capital letter (auth.new_code makes sure): the filter
    knows each one by that, so a name with the same prefix (mt_shoulder_joint_torque_limit) is not taken for one."""
    home = str(tmp_path / "projects")
    owner = auth.init_team("Jim", home=home)
    made = [owner["code"], auth.create_invite(owner["member"]["id"], "editor", [], "", home=home)["code"],
            auth.create_link(owner["member"]["id"], home=home)["code"],
            auth.create_token(owner["member"]["id"], "nightly", home=home)["token"]]
    made += [auth.new_code(prefix, n) for prefix, n in (("mi_", 16), ("ml_", 16), ("mt_", 32)) for _ in range(3000)]
    for code in made:
        assert auth.CODE_RX.match(code)
        for text in (code, f"my invite is {code}.", f"http://studio:8765/?invite={code}&x=1", f"token={code}"):
            assert security.looks_like_secret(text), text
    token = "mt_Xq3_Lp9ZbN2wV7aR5kT1mC8yH4uF6jD0sE2gW9oK3iB"
    assert security.TEAM_CODE.fullmatch(token)
    for cut in (token[:30], token + "_and_more", f"Ed {token}"[:40]):  # a code cut short or run on
        assert security.looks_like_secret(cut), cut
    for name in ("mt_" + "shoulder_joint_torque_limit_x", "mi_" + "gripper_force_threshold", "ml_" + "a" * 22,
                 "mt_" + "w" * 43, "mt_PWM_frequency_limit_hz", "xmi_inviteCode0123456789ab"):
        assert not security.looks_like_secret(name), name  # words, no capital or digit, or inside a longer word


def test_an_sk_key_is_told_from_a_part_name_by_its_random_run():
    for key in ("sk-live-9f3kd02kfiq0alz7vnm4xq", "sk-AbCdEf0123456789ghIJkl", "sk-test0123456789abcdefghij",
                "sk-proj-Ab3_dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3z-a5B7c9D1", "sk-" + "a1b2c3d4" * 4,
                "sk-ant-api03-abcdefghijkl", "kwpat-ABCDEFGH0123", "sk-live-4f8a-91bc-77de-12ab",
                "sk-test-1234-5678-90ab-cdef", "sk-subq-a1b2-c3d4-e5f6-a7b8-c9d0", "sk-123e4567-e89b-12d3-a456-426614174000",
                "sk-live-k3f9-abqx-77de-zkfp", "sk-proj-5gzHO1-GcH-mropdskmtOkmw", "sk-cO-11pR4F4s_xyt-VLCB"):
        assert security.has_key(f"key: {key}") and security.looks_like_secret(key), key
    random = __import__("random").Random(1)
    hexes = "0123456789abcdef"
    for _ in range(2000):  # a key in groups of one size, with or without a word in front, is never a phrase
        groups = "-".join("".join(random.choice(hexes) for _ in range(4)) for _ in range(5))
        assert security.has_key(f"sk-live-{groups}") and security.has_key(f"sk-{groups}"), groups
    random, lowdig = __import__("random").Random(2), "abcdefghijklmnopqrstuvwxyz0123456789"
    keys = ["sk-live-" + "-".join("".join(random.choice(lowdig) for _ in range(5)) for _ in range(4)) for _ in range(2000)]
    missed = [key for key in keys if any(c.isdigit() for c in key) and not security.has_key(key)]
    assert len(missed) <= 20, missed  # 14: every group with a digit reads as a numbered word (sk-live-ehro0-taky8-...)
    for name in ("sk-6x40-stainless-steel-screws", "sk-thin-and-light-but-strong", "sk-M6x40-DIN912-A2-70-stainless",
                 "sk-2020-aluminium-extrusion-500mm", "sk-short1", "sk-NEMA17-stepper-42x40mm", "sk-1N4148-diodes-x100",
                 "sk-2020-2040-4040-extrusion-set", "SKU sk-M3x10-M3x12-M3x16-screws"):
        assert not security.has_key(name), name


def test_mixed_case_names_with_a_number_pass_and_keys_in_their_shape_are_still_caught():
    """<word>V<n> and <word><Word><n> names were refused whole (a part mixing capitals, small letters and digits read
    as a random run); keys whose groups happen to look like that are still caught."""
    random = __import__("random").Random(3)
    words = ("joint servo motor link wrist elbow shoulder gripper finger camera axis layer encoder sensor torque limit "
             "gain offset speed current force threshold weights model config calib pose home left right base arm knee "
             "policy detector backbone").split()
    for _ in range(1500):
        part = random.choice(words) + (f"V{random.randint(1, 9)}" if random.random() < 0.5
                                       else random.choice(words).capitalize() + str(random.randint(1, 9)))
        name = random.choice(("mt_", "ml_", "mi_")) + "_".join([part] + random.sample(words, 2))
        assert not security.looks_like_secret(name), name
    random, b64 = __import__("random").Random(4), "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    alnum = b64[:-2]
    keys = ["sk-live-" + "-".join("".join(random.choice(alnum) for _ in range(5)) for _ in range(4)) for _ in range(3000)]
    keys += ["sk-" + "".join(random.choice(b64) for _ in range(24)) for _ in range(3000)]
    missed = [key for key in keys if any(c.isdigit() for c in key) and not security.has_key(key)]
    assert len(missed) <= 2, missed  # 0 when this was written: 0 of 100,000 of each in the simulation
    for key in ("sk-live-AbcD1-EfgH2-IjkL3-MnoP4", "sk-live-Xq3Lp9-Ab7cD2-k9QzWm", "ml_pfuek3Ly-Novmd8ibockj-umcoithJ11"):
        assert security.has_key(key), key  # a single capital before a number is no version, a word after it no camelCase


def test_every_code_morph_makes_reads_as_no_phrase(monkeypatch):
    """A Teams code is drawn again in the rare case it reads as a phrase (33 in 1,000,000 invite and sign-in codes):
    the filter judges a run of an invite code's length by how random it reads, so a name such as
    ml_policyV1_final_weights passes, and every code Morph makes is still caught."""
    draws = iter(["policyV1_final_weights", "Xq3_Lp9ZbN2wV7aR5kT1mC"])
    monkeypatch.setattr(auth, "secrets", type("Draws", (), {"token_urlsafe": staticmethod(lambda n: next(draws))}))
    code = auth.new_code("ml_", 16)
    assert code == "ml_Xq3_Lp9ZbN2wV7aR5kT1mC" and security.looks_like_secret(code)
    assert not security.looks_like_secret("ml_policyV1_final_weights")


def test_a_saved_key_is_refused_whatever_its_shape(monkeypatch):
    """A key saved in the Keys dialog is refused by its value: a Keewano project key has no prefix to know it by."""
    from morph import keys
    assert set(security.SAVED_KEYS) == set(keys.NAMES) - {"KEEWANO_PROJECT_ID"} | {"ANTHROPIC_AUTH_TOKEN", "MORPH_TOKEN"}
    monkeypatch.setenv("KEEWANO_API_KEY", "proj7Hq2Zx9LmP4w")
    assert security.looks_like_secret("the keewano key proj7Hq2Zx9LmP4w, use it")
    monkeypatch.setenv("KEEWANO_PROJECT_ID", "12345678")
    assert not security.looks_like_secret("project 12345678")  # an id, not a key


def test_the_tool_runner_redacts_keys_and_keeps_part_names():
    from morph.studio.toolbox import http_tool
    text = "sk-6x40-stainless-steel-screws in stock; key sk-live-9f3kd02kfiq0alz7vnm4xq; mt_shoulder_joint_torque_limit"
    assert http_tool.redact(text) == ("sk-6x40-stainless-steel-screws in stock; key [redacted]; "
                                      "mt_shoulder_joint_torque_limit")


def test_ordinary_text_still_passes_everywhere():
    for t in ORDINARY:
        assert not security.looks_like_secret(t), t
        assert collab._text(t, "comment", 4000) == t
    assert auth.clean_name("Ed Smith") == "Ed Smith"
    assert not security.holds_secret({"layout": "side_to_side", "object": {"name": "egg", "diameter_mm": 45}, "n": [1, 2]})
    assert not security.holds_secret(None) and not security.looks_like_secret(None)


# ------------------------------------------------------------------ New robot


def brief_req(home, body):
    sent = []
    req = type("Req", (), {"body": body, "query": {}, "member": None, "handler": None,
                           "studio": type("S", (), {"home": str(home)})(),
                           "room": type("R", (), {"name": "egg-picker", "hub": type("H", (), {"send": sent.append})()})()})()
    return req, sent


@pytest.mark.parametrize("kind", sorted(KEYS))
def test_a_new_robot_request_with_a_key_leaves_no_draft_event_or_outbound_call(tmp_path, monkeypatch, kind):
    calls = []
    monkeypatch.setattr(agents, "run_turn", lambda *a, **k: calls.append(k) or pytest.fail("must not reach SubQ or Claude"))
    for body in ({"text": with_key(kind)}, {"text": "sort socks", "answers": {"layout": with_key(kind)}}):
        req, sent = brief_req(tmp_path, body)
        status, out = api_brief.post(req, sent.append, home=str(tmp_path), sync=True)
        assert status == 400 and out["ok"] is False and "Keys dialog" in out["error"]
        assert sent == [] and calls == []
    assert not os.path.exists(tmp_path / ".briefs") or os.listdir(tmp_path / ".briefs") == []


@pytest.mark.parametrize("answers", [{"layout": "left of the bin, " + KEYS["subq"]},
                                     {"object": {"name": "egg", "note": KEYS["claude"]}},
                                     {"layout": "side_to_side", "object": KEYS["team_invite"]}])
def test_a_typed_answer_with_a_key_is_refused_and_the_draft_keeps_what_it_had(tmp_path, answers):
    req, sent = brief_req(tmp_path, {"text": "a robot that picks strawberries"})
    status, out = api_brief.post(req, sent.append, home=str(tmp_path), sync=True)
    assert status == 200
    bid, before = out["brief_id"], open(tmp_path / ".briefs" / f"{out['brief_id']}.json", encoding="utf-8").read()
    req, sent = brief_req(tmp_path, {"brief_id": bid, "answers": answers})
    status, out = api_brief.post(req, sent.append, home=str(tmp_path), sync=True)
    assert status == 400 and "Keys dialog" in out["error"] and sent == []
    assert open(tmp_path / ".briefs" / f"{bid}.json", encoding="utf-8").read() == before


def events(port, timeout=15):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("GET", "/api/events")
    r = conn.getresponse()
    assert r.status == 200
    return conn, r


def next_event(r, kind, timeout=20.0) -> list:
    seen, t0 = [], time.time()
    while time.time() - t0 < timeout:
        line = r.fp.readline()
        if not line:
            break
        if line.startswith(b"data: "):
            ev = json.loads(line[6:])
            seen.append(ev)
            if ev.get("type") == kind:
                return seen
    raise AssertionError(f"no {kind} event: {[e.get('type') for e in seen]}")


@pytest.fixture
def studio(tmp_path):
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg-picker"), str(home))
    httpd = start(st)
    yield st, caller(httpd), httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def test_a_refused_new_robot_request_sends_no_event_to_any_tab(studio, monkeypatch):
    st, call, port = studio
    turns, real = [], agents.run_turn
    monkeypatch.setattr(agents, "run_turn", lambda *a, **k: turns.append(k.get("text")) or real(*a, **k))
    conn, r = events(port)
    for kind in ("claude", "subq", "team_script_token"):
        s, out, _ = call("POST", "/api/brief", {"text": with_key(kind)})
        assert s == 400 and "Keys dialog" in out["error"]
    s, out, _ = call("POST", "/api/brief", {"text": "a robot that sorts socks"})  # the next brief event is this one's
    assert s == 202
    seen = next_event(r, "brief")
    conn.close()
    assert seen[-1]["id"] == out["brief_id"] and turns == ["a robot that sorts socks"]
    assert not any(k in json.dumps(seen) for k in KEYS.values())
    assert sorted(os.listdir(os.path.join(st.home, ".briefs"))) == [out["brief_id"] + ".json"]


# ------------------------------------------------------------------ chat, a project's description, a world's labels


@pytest.mark.parametrize("kind", ["team_script_token", "team_invite", "team_sign_in_link", "jwt", "saved_tool_key"])
def test_chat_refuses_teams_codes_and_tool_keys_too(studio, kind):
    st, call, _ = studio
    s, out, _ = call("POST", "/api/chat", {"message": with_key(kind)})
    assert s == 400 and "Keys dialog" in out["error"]
    time.sleep(0.1)
    assert st.room().hub.history == [] and st.room().jobs.snapshot() == []


def test_a_project_description_and_a_worlds_labels_are_checked(studio):
    st, call, _ = studio
    s, out, _ = call("POST", "/api/projects", {"name": "sock-bot", "need": with_key("subq")})
    assert s == 400 and "Keys dialog" in out["error"] and not os.path.exists(os.path.join(st.home, "sock-bot"))
    world = {"id": "desk", "name": "Desk", "obstacles": [{"label": KEYS["claude"], "center": [0.3, 0, 0.1],
                                                          "half": [0.05, 0.05, 0.1]}]}
    s, out, _ = call("POST", "/api/world", {"world": world})
    assert s == 400 and "Keys dialog" in out["error"]
    assert not os.path.exists(os.path.join(st.room().p.root, "world"))


# ------------------------------------------------------------------ Show Morph the job


def test_the_video_cards_refuse_a_key_before_anything_is_queued(studio):
    st, call, _ = studio
    s, out, _ = call("POST", "/api/video/apply", {"preview": True, "result": None, "answers": {"object.name": KEYS["claude"]}})
    assert s == 400 and "Keys dialog" in out["error"]
    s, out, _ = call("POST", "/api/video/apply", {"changes": {"object.name": "egg " + KEYS["subq"]}})
    assert s == 400 and "Keys dialog" in out["error"]
    s, out, _ = call("POST", "/api/video/apply", {"changes": {"object.diameter_mm": 50}, "label": "video: " + KEYS["keewano"]})
    assert s == 400 and "Keys dialog" in out["error"]
    time.sleep(0.1)
    assert st.room().jobs.snapshot() == [] and st.room().hub.history == []
    s, out, _ = call("POST", "/api/video/apply", {"changes": {"object.diameter_mm": 50}, "label": "video: egg"})
    assert s == 202  # and an ordinary one still goes through


# ------------------------------------------------------------------ Teams


@pytest.fixture
def team(tmp_path):
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg-picker"), str(home), team=True)
    ids, cookies = make_team(str(home), (("Jim", "owner", ["generalist"]), ("Ed", "editor", ["electronics"]),
                                         ("Vic", "viewer", [])))
    httpd = start(st)
    port = httpd.server_address[1]
    jar = {n: {"Cookie": f"{auth.cookie_name(port)}={c}"} for n, c in cookies.items()}
    yield st, caller(httpd), ids, jar, port
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("kind", ["subq", "jwt", "saved_tool_key", "team_script_token"])
def test_every_teams_text_field_refuses_a_key(team, kind):
    st, call, ids, jar, port = team
    key, root = KEYS[kind], st.room().p.root
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    conn.request("GET", "/api/events", headers=jar["Vic"])
    vic = conn.getresponse()
    assert vic.status == 200
    tries = [("POST", "/api/comments", {"body": f"see {key}", "anchor": {"rev": "r001"}}, "Vic"),
             ("POST", "/api/proposals", {"title": f"swap {key}"[:120], "changes": {"arm.dof": 6}}, "Ed"),
             ("POST", "/api/proposals", {"note": f"why: {key}", "changes": {"arm.dof": 6}}, "Ed"),
             ("POST", "/api/me", {"name": key}, "Ed"),
             ("POST", "/api/me/tokens", {"label": key}, "Ed"),
             ("POST", "/api/releases", {"tag": "v1", "rev": "r001", "note": key}, "Jim")]
    if kind == "subq":  # names with a fixed shape that a SubQ-shaped key still fits
        tries += [("POST", "/api/releases", {"tag": key, "rev": "r001"}, "Jim"),
                  ("POST", "/api/branches", {"action": "create", "name": key, "base_rev": "r001"}, "Ed")]
    for method, path, body, who in tries:
        s, out, _ = call(method, path, body, headers=jar[who])
        assert s == 400 and "Keys dialog" in out["error"], (path, body, s, out)
    s, out, _ = call("POST", "/api/comments", {"body": "Can it reach the top shelf?", "anchor": {"rev": "r001"}}, headers=jar["Vic"])
    assert s == 200
    seen = next_event(vic, "comment")
    conn.close()
    assert not any(key in json.dumps(e) for e in seen)
    for name in os.listdir(os.path.join(root, ".team")):
        with open(os.path.join(root, ".team", name), encoding="utf-8") as fh:
            assert key not in fh.read(), name
    team_doc = open(os.path.join(st.home, ".team", "team.json"), encoding="utf-8").read()
    assert key[:40] not in team_doc and auth.TeamStore(st.home).read()["members"][ids["Ed"]]["name"] == "Ed"


def test_a_key_is_never_a_team_or_display_name(tmp_path):
    with pytest.raises(ValueError, match="Keys dialog"):
        auth.init_team("Jim", f"team {KEYS['claude']}", home=str(tmp_path))
    with pytest.raises(ValueError, match="Keys dialog"):
        auth.init_team(KEYS["subq"], "R Cubed", home=str(tmp_path))
    out = auth.init_team("Jim", "R Cubed", home=str(tmp_path))
    with pytest.raises(ValueError, match="Keys dialog"):           # a key as the invite's name hint is refused, said
        auth.create_invite(out["member"]["id"], "editor", [], KEYS["keewano"], home=str(tmp_path))
    inv = auth.create_invite(out["member"]["id"], "editor", [], "Ed", home=str(tmp_path))
    assert inv["invite"]["name_hint"] == "Ed"
    with pytest.raises(ValueError, match="Keys dialog"):
        auth.redeem_invite(inv["code"], f"Ed {KEYS['claude']}", home=str(tmp_path))
