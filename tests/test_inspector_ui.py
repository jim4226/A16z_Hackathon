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


def tab(pg, name):
    """Open a tab the way a click does (a loaded test machine can keep Playwright waiting for the header to settle)."""
    pg.wait_for_selector(f'#tabs .tab[data-tab="{name}"]', state="visible", timeout=60000)
    pg.evaluate("n => document.querySelector(`#tabs .tab[data-tab='${n}']`).click()", name)


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


def test_the_studio_says_generated_files_are_not_bench_validated(inspect, tmp_path):
    # the firmware pane, the export button and its toast: generated and software-checked, never "validated"
    elec = {"firmware": "firmware/egg_picker.ino", "schematic_svg": "schematic.svg", "nets": [], "harness": [],
            "plan": {"modules": []}}
    pg = inspect({"summary": SUMMARY, "electronics": elec})
    fw = tmp_path / "projects0" / "egg-picker" / "r001" / "electronics" / "firmware"
    assert fw.parent.parent.is_dir()
    fw.mkdir(parents=True)
    (fw / "egg_picker.ino").write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
    (fw.parent / "schematic.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>', encoding="utf-8")
    pg.reload(wait_until="domcontentloaded")          # the page fetched the firmware before the file was there
    pg.wait_for_selector("#insp .mt-table", timeout=60000)
    assert "not bench-validated" in pg.get_attribute("#tabsInfo .xport", "title")
    tab(pg, "elec")
    pg.click('.subtabs button[data-p="fw"]')
    pg.wait_for_selector(".code .fw-note", timeout=20000)
    note = pg.inner_text(".code .fw-note")
    assert "not bench-tested" in note and "compiled" in note, note
    assert "void loop" in pg.inner_text(".code")


def _cfg(parts, tooling, nq=1):
    return {"unit_cost": round(parts + 209.94 + tooling / nq, 2), "tooling_cost": tooling, "parts_cost": parts,
            "purchased_cost": 209.94, "setup_cost": 0.0, "lead_days": 30 if tooling else 7, "summary": "", "curve": []}


def test_production_cost_shows_mold_tooling_apart(inspect):
    # production at quantity 1 read "$77,742" per unit: $72,971 of it is the molds, paid once
    configs = {"prototype": _cfg(94.87, 0.0), "pilot": _cfg(330.61, 0.0), "production": _cfg(4561.15, 72971.29)}
    rows = [{"item": "Base shell", "category": "fabricated", "qty": 1, "make_or_buy": "make", "vendors": [],
             "selected": {"vendor": "protolabs", "unit_price": 4561.15}, "ext_price": 4561.15},
            {"item": "Injection-mold tooling (9 tools, $72,971 amortized over 1 units)", "category": "fabricated", "qty": 1,
             "make_or_buy": "make", "vendors": [], "selected": None, "ext_price": 72971.29},
            {"item": "STS3215 servo", "category": "actuator", "qty": 1, "make_or_buy": "buy", "vendors": [],
             "selected": {"vendor": "feetech", "unit_price": 209.94}, "ext_price": 209.94}]
    res = {"summary": {**SUMMARY, "unit_cost": 77742.38, "tooling_cost": 72971.29, "build_config": "production"},
           "manufacturing": {"config": "production", "quantity": 1, "configs": configs, "parts": [],
                             "totals": {"tooling_cost": 72971.29, "parts_cost": 4561.15, "purchased_cost": 209.94,
                                        "unit_cost": 77742.38, "lead_days": 30}},
           "bom": {"rows": rows, "totals": {"buy": 209.94, "make": 77532.44, "total": 77742.38, "tooling": 72971.29,
                                            "per_config": {k: c["unit_cost"] for k, c in configs.items()}, "quantity": 1,
                                            "config": "production"}}}
    pg = inspect(res)
    rows_ = dict(pg.evaluate("[...document.querySelectorAll('#insp .mt-table tr')].map(r => [...r.cells].map(c => c.innerText))"))
    assert "Unit cost (incl. $72,971 tooling over 1 unit)" in rows_, list(rows_)
    tab(pg, "bom")
    pg.wait_for_selector(".pcfg .row", timeout=20000)
    pcfg = " ".join(pg.inner_text(".pcfg").split())
    assert "Per unit at qty 1" in pcfg and "Production $4,771" in pcfg and "+ $72,971 tooling, once" in pcfg, pcfg
    assert "$77,742" not in pcfg
    assert "incl. $72,971 tooling over 1 unit" in pg.inner_text(".bm .stats .stat")
    tab(pg, "mfg")
    pg.wait_for_selector('.ccard[data-v="production"]', timeout=20000)
    card = " ".join(pg.inner_text('.ccard[data-v="production"]').split())
    assert "$4,771/ unit" in card.replace(" / unit", "/ unit") and "+ tooling $72,971 once" in card, card
    assert "$77,742 each at qty 1 with tooling" in card, card
    assert "tooling $0" in " ".join(pg.inner_text('.ccard[data-v="prototype"]').split())
