"""What an import leaves unexplained, explained (W3 UX-13): a parts list's 12 V STS3215 is the 12 V servo (its
description counts, and its MPN shows), an object whose grip would bend the fingers gets the thicker beam New robot
would give it, and an import whose build isn't feasible says why in its project's chat, with the fixes Morph can make
(the page names a fix; the changes are the server's own)."""

import json
import os
import threading
import time
import types

import pytest

from morph.importers import bom
from morph.spec import Design
from morph.studio import api_import, projects
from morph.studio.rooms import Hub

MUG = {"object.name": "mug60", "object.shape": "cylinder", "object.diameter_mm": 60.0, "object.length_mm": 80.0,
       "object.mass_g": 225.8, "finger.length_mm": 60.0, "finger.pad_height_mm": 27.0, "finger.pad_depth_mm": 7.5,
       "object.friction": 0.5, "object.max_force_n": 0.0}               # mesh_object.object_changes() of a 60 x 80 mm mug
SO101_TORQUE = "shoulder_lift: needs 2.02 N·m with margin, sts3215_7v4 gives 1.91 N·m"   # the review's SO-101 import


def test_a_parts_list_line_for_a_12_v_sts3215_is_the_12_v_servo_with_its_mpn_and_description(tmp_path):
    p = tmp_path / "bom.csv"
    p.write_text("Part,Manufacturer Part Number,Manufacturer,Quantity,Unit Price,Description\n"
                 "Servo,STS3215,Feetech,6,17.00,Serial bus servo 12V\n"
                 "Servo,STS3215,Feetech,1,13.89,Serial bus servo 7.4V C001\n"
                 "Bearing,608-2RS,Generic,4,0.80,8x22x7 ball bearing\n", encoding="utf-8")
    res = bom.parse(str(p))
    r12, r7, bearing = res["rows"]
    assert (r12["item"], r12["mpn"], r12["description"]) == ("Servo", "STS3215", "Serial bus servo 12V")
    assert r12["catalog"] == "sts3215" and "12 V" in r12["catalog_name"]
    assert r7["catalog"] == "sts3215_7v4" and "7.4 V" in r7["catalog_name"]
    assert bearing["catalog"] is None and bearing["catalog_name"] is None and bearing["category"] == "purchased"
    assert res["columns"]["item"] == "Part" and res["columns"]["description"] == "Description"
    assert bom.actuator_keys(res["rows"]) == {"sts3215": 6, "sts3215_7v4": 1}


def room_with(design, results=None, tmp_path=None):
    """A room as api_import uses it: its newest revision's design (and results), its chat hub."""
    p = types.SimpleNamespace(load_design=lambda rev: design, load_results=lambda rev: results or {})
    return types.SimpleNamespace(name="egg-picker", latest=lambda: "r001", p=p,
                                 hub=Hub("egg-picker", str(tmp_path / "chat.jsonl") if tmp_path else None))


def test_an_imported_object_that_would_bend_the_fingers_gets_new_robots_thicker_beam():
    """The review's mug on the egg-picker: "tip deflection 1.11 mm > 1 mm" and no repair. New robot's rule (1.5 mm at a
    time) makes the beam 7.5 mm, which its own preflight passes; a light object leaves the fingers as they are."""
    room = room_with(projects.template("egg-picker"))
    changes, note = api_import._fit_fingers(room, MUG)
    assert changes == {**MUG, "finger.thickness_mm": 7.5}
    assert "6 mm finger beams too far" in note and "7.5 mm thick now" in note
    assert not api_import._bends(projects.template("egg-picker").with_changes(changes).flat())
    same = {"object.friction": 0.4}                                      # the egg it was built for: nothing to repair
    assert api_import._fit_fingers(room, same) == (same, "")


def test_fixes_for_a_builds_issues():
    so101 = Design().with_changes({"arm.source": "so101"})
    fx = api_import.fixes_for(so101, [SO101_TORQUE])
    assert [(f["label"], f["changes"]) for f in fx] == [("Use 12 V STS3215 servos", {"arm.actuator_overrides": "all=sts3215"})]
    gen = projects.template("egg-picker")
    fx = api_import.fixes_for(gen.with_changes(MUG), ["j2: needs 3.10 N·m with margin, 1× mg996r gives 1.08 N·m",
                                                      "tip deflection 1.11 mm > 1 mm"])
    assert [(f["label"], f["changes"]) for f in fx] == [
        ("Swap to Dynamixel servos", {"arm.actuator_family": "dynamixel", "arm.actuator_overrides": ""}),
        ("Thicker fingers (7.5 mm)", {"finger.thickness_mm": 7.5})]
    assert len({f["id"] for f in fx}) == 2 and all(len(f["id"]) == 12 for f in fx)
    assert api_import.fixes_for(gen, ["the PCB fails DRC"]) == []


def test_a_build_that_isnt_feasible_is_explained_in_the_chat_and_a_fix_is_built_by_name(tmp_path):
    so101 = Design().with_changes({"arm.source": "so101"})
    room = room_with(so101, {"summary": {"feasible": False, "issues": [SO101_TORQUE]}}, tmp_path)
    api_import._explain(room, "r001", "The imported robot so101-imp")
    msg = room.hub.history[-1]
    assert msg["type"] == "assistant" and msg["text"].startswith("The imported robot so101-imp built r001, but it isn't feasible")
    assert SO101_TORQUE in msg["text"] and [f["label"] for f in msg["fixes"]] == ["Use 12 V STS3215 servos"]
    with open(tmp_path / "chat.jsonl", encoding="utf-8") as fh:              # kept: a tab that opens it later sees it
        assert json.loads(fh.readlines()[-1])["fixes"] == msg["fixes"]
    queued = []

    def queue_changes(r, changes, **kw):
        queued.append((r, changes, kw))
        return types.SimpleNamespace(info=lambda: {"id": "j1", "status": "queued"})
    req = types.SimpleNamespace(room=room, body={"fix": msg["fixes"][0]["id"]}, member=None,
                                studio=types.SimpleNamespace(queue_changes=queue_changes))
    assert api_import.fix(req) == (202, {"ok": True, "job": {"id": "j1", "status": "queued"}})
    assert queued[0][1] == {"arm.actuator_overrides": "all=sts3215"} and queued[0][2]["label"] == "Use 12 V STS3215 servos"
    for body, status in (({"fix": "0123456789ab"}, 404), ({"fix": "../x"}, 400), ({}, 400), ([1], 400)):
        assert api_import.fix(types.SimpleNamespace(room=room, body=body, member=None, studio=req.studio))[0] == status
    # feasible, or nothing to say: no message
    n = len(room.hub.history)
    api_import._explain(room_with(so101, {"summary": {"feasible": True, "issues": []}}, tmp_path), "r002", "x")
    api_import._explain(room_with(so101, {"summary": {"feasible": False, "issues": []}}, tmp_path), "r002", "x")
    assert len(room_with(so101, None, tmp_path).hub.history) == n


@pytest.fixture
def page_on_studio(tmp_path, monkeypatch):
    """A real Studio page (no WebGL) on an egg-picker project whose chat holds an import's explanation."""
    sync_api = pytest.importorskip("playwright.sync_api")
    from morph import keys
    from morph.robot.pipeline2 import RobotProject
    from morph.studio import server
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    home = tmp_path / "projects"
    root = home / "egg-picker"
    (root / "r001").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot", "source": "", "need": "egg"}), encoding="utf-8")
    d = projects.template("egg-picker").with_changes(MUG)
    (root / "r001" / "design.json").write_text(d.dumps(), encoding="utf-8")
    (root / "r001" / "results.json").write_text(json.dumps({
        "rev": "r001", "parent": None, "label": "imported object", "summary": {"feasible": False, "issues": ["tip deflection 1.11 mm > 1 mm"]},
        "provenance": {}, "stage_status": {}}), encoding="utf-8")
    st = server.Studio(RobotProject(str(root)), str(home))
    queued = []
    monkeypatch.setattr(st, "queue_changes", lambda room, changes, **kw: queued.append((changes, kw)) or
                        types.SimpleNamespace(info=lambda: {"id": "j9", "status": "queued"}))
    api_import._explain(st.room(), "r001", "With the imported mug60, egg-picker")
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(st, None))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(st, {f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    chrome = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=chrome if os.path.exists(chrome) else None,
                                        args=["--no-sandbox", "--disable-webgl", "--disable-3d-apis"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright ({exc.__class__.__name__})")
        try:
            yield browser.new_page(viewport={"width": 1280, "height": 720}), f"http://127.0.0.1:{port}", queued
        finally:
            browser.close()
            httpd.shutdown()
            httpd.server_close()


def test_the_chat_shows_the_reasons_and_a_fix_chip_builds_it(page_on_studio):
    page, base, queued = page_on_studio
    page.goto(f"{base}/?tour=0")
    page.wait_for_function("() => window.morph && window.morph.state.loaded === true", timeout=60000)
    text = page.inner_text("#messages")
    assert "isn't feasible yet" in text and "tip deflection 1.11 mm > 1 mm" in text
    chip = page.locator("#messages .fixacts button")
    assert chip.count() == 1 and chip.inner_text().strip() == "Thicker fingers (7.5 mm)"
    chip.click()
    assert page.evaluate("() => document.querySelector('#messages .fixacts button').disabled")   # pressed once
    for _ in range(300):                                                   # the request on its way to the Studio
        if queued:
            break
        time.sleep(0.1)
    assert queued == [({"finger.thickness_mm": 7.5}, {"label": "Thicker fingers (7.5 mm)",
                                                       "reply": "Thicker fingers (7.5 mm): building it as a new revision.",
                                                       "source": "import", "user_text": "Thicker fingers (7.5 mm)", "by": None})]
