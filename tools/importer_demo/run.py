"""Drive the Import dialog demo (tools/importer_demo/index.html) in Chromium and screenshot each state.

  python tools/importer_demo/run.py --out shots/ [--port 8851] [--chrome /path/to/chrome] [--testdata DIR]

Fixtures are real: the SO-101 (URDF only), a Koch-like arm with package:// paths (or Koch v1.1 itself when
--testdata holds koch_sim/), an egg STL and a README parts list go through morph.importers (detection, previews),
and the page's mocked studio serves those results. Needs the playwright Python package and a Chromium.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))


def make_fixtures(out: str, testdata: str | None) -> dict:
    from morph.importers import set_home
    from morph.importers.session import Session
    from test_import_fixtures import BOM_MD, arm_urdf, egg_stl, so101_copy

    home = tempfile.mkdtemp(prefix="importer-demo-")
    set_home(home)
    src = tempfile.mkdtemp(prefix="importer-src-")
    fx = {}

    def run(name, folder=None, files=None):
        s = Session.create(home)
        if folder:
            s.add_path(folder)
        for p in files or []:
            s.add_path(p)
        events = []
        det = s.detect(events.append)
        fx[name] = {"detect": det, "events": [e for e in events if e.get("type") == "import"]}
        for c in det["candidates"]:
            if c["kind"] in ("robot", "object") and not c.get("error"):
                for fmt in ("png", "glb"):
                    data, _ = s.preview(c["index"], fmt)
                    with open(os.path.join(out, f"{name}_{c['index']}.{fmt}"), "wb") as fh:
                        fh.write(data)

    run("robot_ok", so101_copy(os.path.join(src, "so101")))
    koch = os.path.join(testdata or "", "koch_sim")
    if testdata and os.path.isdir(koch):
        run("robot_ask", koch)
    else:
        k = os.path.join(src, "koch-v1-1")
        arm_urdf(os.path.join(k, "follower"), package="follower",
                 joint_names={"shoulder_pan": "joint_1", "shoulder_lift": "joint_2", "elbow_flex": "joint_3",
                              "wrist_flex": "joint_4", "wrist_roll": "joint_5", "gripper": "joint_6"})
        with open(os.path.join(k, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("Follower: 2x XL430-W250-T, 4x XL330-M288-T.\n")
        run("robot_ask", k)
    run("object", files=[egg_stl(os.path.join(src, "egg.stl"))])
    bom = os.path.join(src, "parts", "README.md")
    os.makedirs(os.path.dirname(bom))
    with open(bom, "w", encoding="utf-8") as fh:
        fh.write(BOM_MD)
    run("bom", files=[bom])
    with open(os.path.join(out, "fixtures.json"), "w", encoding="utf-8") as fh:
        json.dump(fx, fh)
    shutil.rmtree(home, ignore_errors=True)
    shutil.rmtree(src, ignore_errors=True)
    return fx


class Handler(http.server.SimpleHTTPRequestHandler):
    fixtures = ""

    def translate_path(self, path):
        p = path.split("?", 1)[0]
        if p.startswith("/fixtures/"):
            return os.path.join(self.fixtures, os.path.basename(p))
        return super().translate_path(path)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="shots")
    ap.add_argument("--port", type=int, default=8851)
    ap.add_argument("--chrome", default=os.environ.get("CHROME") or None)
    ap.add_argument("--testdata", default=os.environ.get("MORPH_IMPORT_TESTDATA"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    fxdir = tempfile.mkdtemp(prefix="importer-fixtures-")
    make_fixtures(fxdir, a.testdata)
    Handler.fixtures = fxdir
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), functools.partial(Handler, directory=ROOT))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    from playwright.sync_api import sync_playwright

    errors, shots = [], []
    base = f"http://127.0.0.1:{a.port}/tools/importer_demo/index.html"
    dummy = os.path.join(fxdir, "fixtures.json")

    def shot(page, name):
        path = os.path.join(a.out, f"f2_{name}.png")
        page.screenshot(path=path)
        shots.append(path)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=a.chrome) if a.chrome else pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 860}, device_scale_factor=1)

        def page_for(scenario, **q):
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(f"{scenario}: {e}"))
            page.on("console", lambda m: m.type == "error" and errors.append(f"{scenario}: console {m.text}"))
            qs = "&".join(f"{k}={v}" for k, v in {"scenario": scenario, **q}.items())
            page.goto(f"{base}?{qs}")
            page.wait_for_function("window.ready === true")
            return page

        # 1-6: a robot whose motors the files don't name, from drop to a built project
        p = page_for("robot_ask", slow=450, build=380)
        p.click("#importBtn")
        p.wait_for_selector(".imp-drop")
        p.wait_for_timeout(400)
        shot(p, "1_pick")
        p.set_input_files('input[data-in="files"]', [dummy])
        p.wait_for_selector(".imp-check .imp-row.busy, .imp-check .imp-row")
        p.wait_for_timeout(1100)
        shot(p, "2_detecting")
        p.wait_for_selector('[data-a="commit"]', timeout=20000)
        p.wait_for_timeout(300)
        shot(p, "3_robot_needs_actuators")
        p.click('[data-a="commit"]')
        p.wait_for_selector(".imp-ferr")
        assert p.is_visible(".imp-ferr") and p.is_visible("tr.miss select")
        shot(p, "4_robot_pick_first")
        p.click('[data-fam="dynamixel"]')
        p.wait_for_timeout(200)
        shot(p, "5_robot_family_picked")
        p.fill('[data-k="name"]', "Koch follower")
        p.click('[data-a="commit"]')
        p.wait_for_selector(".imp-stages")
        p.wait_for_timeout(1500)
        shot(p, "6_building")
        p.wait_for_selector('[data-a="openp"]', timeout=20000)
        shot(p, "7_built")
        log = p.evaluate("window.log")
        commit = next(x for x in log if x[0] == "commit")[1]
        assert commit["answers"] == {"family": "dynamixel"} and commit["name"] == "Koch follower", commit
        p.click('[data-a="openp"]')
        assert p.evaluate("window.log").pop() == ["open", "koch-follower"]
        p.close()

        # 8-9: a robot Morph fully understood (SO-101 from its URDF), thumbnail and 3D view
        p = page_for("robot_ok", slow=60)
        p.click("#importBtn")
        p.set_input_files('input[data-in="files"]', [dummy])
        p.wait_for_selector('[data-a="commit"]', timeout=20000)
        p.wait_for_function("document.querySelector('.imp-prev img')?.complete")
        shot(p, "8_robot_ok")
        if p.query_selector('[data-a="3d"]'):
            p.click('[data-a="3d"]')
            p.wait_for_timeout(2500)
            shot(p, "9_robot_3d")
        p.close()

        # 10-11: an object mesh and a parts list
        for name, scn in (("10_object", "object"), ("11_bom", "bom")):
            p = page_for(scn, slow=40)
            p.click("#importBtn")
            p.set_input_files('input[data-in="files"]', [dummy])
            p.wait_for_selector('[data-a="commit"]', timeout=20000)
            p.wait_for_timeout(400)
            shot(p, name)
            if scn == "object":
                p.select_option('[data-k="units"]', "cm")
                p.wait_for_timeout(100)
                shot(p, "10b_object_cm")
                p.click('[data-a="commit"]')
                p.wait_for_selector(".imp-row.done")
                shot(p, "10c_object_applied")
            if scn == "bom":
                p.click('[data-a="commit"]')
                p.wait_for_selector(".imp-row.done")
                with p.expect_download() as dl:
                    p.click('[data-a="csv"]')
                csv_path = dl.value.path()
                with open(csv_path, encoding="utf-8") as fh:
                    head = fh.readline().strip()
                if not head.startswith("item,qty"):
                    errors.append(f"bom csv header: {head!r}")
                shot(p, "11b_bom_done")
            p.close()

        # 10d: a Studio with no edit path for the object says so and shows the changes to type
        p = page_for("object", slow=20, applied=0)
        p.click("#importBtn")
        p.set_input_files('input[data-in="files"]', [dummy])
        p.wait_for_selector('[data-a="commit"]', timeout=20000)
        p.click('[data-a="commit"]')
        p.wait_for_selector(".imp-pre")
        shot(p, "10d_object_not_applied")
        p.close()

        # 12: a link Morph refuses, 13: the page-wide drop overlay
        p = page_for("robot_ok")
        p.click("#importBtn")
        p.fill('form[data-a="url"] input', "https://evil.example/robot.zip")
        p.click('form[data-a="url"] button')
        p.wait_for_selector(".imp-err")
        shot(p, "12_url_refused")
        p.click('[data-a="close"]')
        p.evaluate("""() => { const dt = new DataTransfer(); dt.items.add(new File(['x'], 'arm.urdf'));
            window.dispatchEvent(new DragEvent('dragenter', { dataTransfer: dt, bubbles: true })); }""")
        p.wait_for_selector(".imp-overlay.on")
        shot(p, "13_drop_overlay")
        p.close()
        browser.close()
    httpd.shutdown()
    print(json.dumps({"shots": shots, "errors": errors}, indent=1))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
