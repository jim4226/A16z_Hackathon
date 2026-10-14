"""What the inspector says about a revision, in a real browser against a real Studio server: the labels of the
numbers it shows say what was measured. The revision is written by hand (no pipeline runs); skipped without
Playwright's Chromium."""

import json
import os
import threading

import pytest

from morph import keys
from morph.robot.pipeline2 import ORDER, RobotProject
from morph.spec import Design
from morph.studio import server

pw = pytest.importorskip("playwright.sync_api")
CHROME = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

SUMMARY = {"dof": 5, "reach_mm": 475.2, "payload_g": 60.0, "actuators": "sts3215", "min_torque_margin": 3.6,
           "torque_margin_basis": None, "pick_place_success": True, "cycle_time_s": 5.9, "max_payload_g": 1728.0,
           "payload_test_reach_mm": 216.1, "stretch_payload_g": 720.0, "stretch_reach_mm": 475.6,
           "workspace_reach_mm": 474.8, "top_down_reach_mm": 280.0, "max_torque_pct": 41.0, "robot_mass_kg": 1.9,
           "unit_cost": 302.0, "feasible": True, "issues": []}


def make_project(home, name, results):
    root = home / name
    (root / "r001").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot", "source": "", "need": name}), encoding="utf-8")
    (root / "r001" / "design.json").write_text(Design().dumps(), encoding="utf-8")
    res = {"rev": "r001", "parent": None, "label": "first design", "provenance": {n: "computed" for n in ORDER},
           "stage_status": {n: "computed" for n in ORDER}, **results}
    (root / "r001" / "results.json").write_text(json.dumps(res), encoding="utf-8")
    return RobotProject(str(root))


@pytest.fixture
def inspect(tmp_path, monkeypatch):
    """open(results) -> the inspector's text for a revision with those results."""
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=CHROME if os.path.exists(CHROME) else None, args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright ({exc.__class__.__name__})")
        servers, errors = [], []

        def open_(results, name="egg-picker"):
            home = tmp_path / f"projects{len(servers)}"
            st = server.Studio(make_project(home, name, results), str(home))
            httpd = server._Server(("127.0.0.1", 0), server.make_handler(st, None))
            port = httpd.server_address[1]
            httpd.RequestHandlerClass = server.make_handler(st, {f"127.0.0.1:{port}", f"localhost:{port}"})
            threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            servers.append(httpd)
            pg = browser.new_page(viewport={"width": 1400, "height": 900})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"http://127.0.0.1:{port}/?tour=0", wait_until="domcontentloaded")
            pg.wait_for_selector("#insp .mt-table", timeout=60000)
            return pg

        try:
            yield open_
            assert errors == []
        finally:
            browser.close()
            for h in servers:
                h.shutdown()


def test_the_payload_and_reach_labels_say_what_was_measured(inspect):
    pg = inspect({"summary": SUMMARY})
    rows = dict(pg.evaluate("[...document.querySelectorAll('#insp .mt-table tr')].map(r => [...r.cells].map(c => c.innerText))"))
    # the sweep measures top-down 216 mm out (45 % of the reach); stretched straight out the arm holds 720 g
    assert rows["Max payload, top-down at 216 mm"].startswith("1,728") and rows["Payload stretched out at 476 mm"].startswith("720")
    assert rows["Top-down reach"].startswith("280") and rows["Reach, any pose"].startswith("475")
    text = pg.inner_text("#insp")
    assert "Payload at full reach" not in text and "before a servo stalls" in text
    facts = pg.inner_text("#insp .facts")
    assert "OBJECT MASS" in facts.upper() and "\nPAYLOAD" not in facts.upper()


def test_the_inspector_says_what_sizing_changed_from_the_request(inspect):
    # the egg-picker asks for 320 mm and is built at 475 mm: the parameter, the inspector and its issues say so
    note = "reach raised from 320 to 475 mm: top-down pick/place at 234 / 233 mm with a 175 mm wrist + hand"
    pg = inspect({"summary": {**SUMMARY, "notes": [note]},
                  "sizing": {"reach_mm": 475.2, "links_mm": {"base_h": 95.0}, "notes": [note]}})
    issues = pg.inner_text("#insp")
    assert f"Morph adjusted {note}" in " ".join(issues.split()) and "No issues" in issues
    reach = pg.inner_text('#paramsBody .prow[data-key="arm.reach_mm"] label')
    assert "built at 475 mm" in reach, reach
    assert pg.input_value('#paramsBody .prow[data-key="arm.reach_mm"] input[type=number]') == "320"
    # a value built as asked has no hint
    assert "built at" not in pg.inner_text('#paramsBody .prow[data-key="arm.base_height_mm"] label')
    # an older revision without summary notes still shows sizing's
    pg2 = inspect({"summary": SUMMARY, "sizing": {"reach_mm": 475.2, "notes": [note]}})
    assert "Morph adjusted reach raised from 320 to 475 mm" in " ".join(pg2.inner_text("#insp").split())
