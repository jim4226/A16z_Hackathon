"""Import end to end in a real browser, against a real Studio (offline, no keys).

- test_so101_folder_with_one_motor_family_builds_feasible: the SO-101's URDF folder (as shipped, without Morph's
  hand-written profile) through Choose folder: meshes, gripper, tool point and licence found; "Smart bus" picked for
  every joint; built with full simulations as a new project that passes its own checks; opened from the dialog, it is
  in the address and the project menu, marked imported (1600x900).
- test_so101_with_the_suggested_servos_says_why_it_is_not_feasible: the same files with Morph's suggestion (the 7.4 V
  STS3215 the files name): the dialog says the robot built but isn't feasible, why, and what to change (1280x720).
- test_an_egg_mesh_becomes_the_object_with_its_handling: an egg STL dropped on box-picker: Morph's raw egg preset
  (its damage force) and force-sensing fingers, applied as a new revision the build calls feasible (910 px wide).
- test_parts_lists_hostile_archives_and_links: a parts list read and given back as a CSV that a spreadsheet can't run;
  a zip that climbs out of its folder, a zip of a symlink, a link to another site and a link while offline are all
  refused with a reason, and nothing is made.
- test_an_export_comes_back_as_a_new_project: Export of box-picker from the Studio, imported again: rebuilt from
  scratch as a new project with the same design.

They run only with MORPH_E2E=1 (each builds at most one robot). See live_studio.py.
"""

import csv
import io
import json
import os
import shutil
import stat
import zipfile

import pytest

from live_studio import Page, Studio, demo_home, launch, until
from test_import_fixtures import egg_stl, so101_copy

pytestmark = pytest.mark.skipif(os.environ.get("MORPH_E2E") != "1",
                                reason="end-to-end browser test: set MORPH_E2E=1 (it builds robots and takes minutes)")

DIALOG = ".modal.on .imp"
# the review's picture of what was found, drawn on the Studio computer (no WebGL needed), has loaded
PICTURE = ("(() => { const i = document.querySelector('.modal.on .imp [data-prev] img'); "
           "return !!i && i.complete && i.naturalWidth > 0; })()")


@pytest.fixture(scope="module")
def browser():
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as p:
        b = launch(p)
        if b is None:
            pytest.skip("no Chromium for Playwright")
        yield b
        b.close()


@pytest.fixture
def studio(tmp_path, tmp_path_factory):
    home = str(tmp_path / "home")
    shutil.copytree(demo_home(tmp_path_factory), home)
    s = Studio(home, "egg-picker")
    yield s
    s.stop()
    assert not s.tracebacks(), s.tracebacks()


def view(pg: Page) -> str:
    """Which of the dialog's views is showing."""
    return pg.ev("""() => { const d = document.querySelector('.modal.on .imp'); if (!d) return '';
        if (d.querySelector('[data-drop]')) return 'pick'; if (d.querySelector('.imp-stages')) return 'building';
        if (d.querySelector('[data-a=commit]')) return 'review'; if (d.querySelector('.imp-bar')) return 'working'; return 'done'; }""")


def open_import(pg: Page):
    pg.pg.click("#importBtn")
    pg.pg.wait_for_selector(f"{DIALOG} [data-drop]")


def reviewed(pg: Page, timeout=180):
    until(lambda: view(pg) == "review" or pg.ev("!!document.querySelector('.modal.on .imp .imp-err')"), timeout,
          "Morph to read the files")
    return pg.text(DIALOG)


def refusal(pg: Page, timeout=60) -> str:
    return until(lambda: pg.text(f"{DIALOG} .imp-err"), timeout, "the dialog to say why")


def finished(pg: Page, timeout=240) -> str:
    """The building view once the build's verdict is in."""
    until(lambda: pg.ev("!!document.querySelector('.modal.on .imp [data-a=openp]')"), timeout, "the imported robot to build")
    return pg.text(DIALOG)


def test_so101_folder_with_one_motor_family_builds_feasible(browser, studio, tmp_path):
    folder = so101_copy(str(tmp_path / "so101_new_calib"))
    pg = Page(browser, studio, (1600, 900), "maker")
    pg.open()
    open_import(pg)
    pg.fits("Import")
    pg.shot("e2e_imp_01_pick")
    pg.pg.set_input_files(f'{DIALOG} input[data-in="folder"]', folder)
    text = reviewed(pg)
    rows = pg.texts(f"{DIALOG} .imp-check .imp-row")
    assert any(r.startswith("Meshes ") for r in rows) and not any("missing" in r for r in rows), rows
    assert any(r.startswith("Gripper: ") for r in rows) and any(r.startswith("Tool point: ") for r in rows), rows
    licence = next(r for r in rows if r.startswith("Licence: "))
    assert "Apache" in licence, licence
    assert "Actuators" in text
    until(lambda: pg.ev(PICTURE), 90, "the ready-pose picture (no WebGL needed)")
    pg.fits("Import, the robot's review")
    pg.shot("e2e_imp_02_review")

    pg.pg.click(f'{DIALOG} [data-fam="smart_bus"]')
    whys = pg.texts(f"{DIALOG} .imp-acts td.why")
    assert whys and all(w == "Smart bus family, sized for the worst-case pose" for w in whys), whys
    pg.pg.fill(f'{DIALOG} [data-k="name"]', "so101-bus")
    pg.pg.click(f'{DIALOG} [data-a="commit"]')
    until(lambda: view(pg) == "building", 60, "the build to start")
    pg.shot("e2e_imp_03_building")
    text = finished(pg)
    assert "so101-bus is ready" in text and "isn't feasible" not in text, text
    stages = pg.ev("[...document.querySelectorAll('.modal.on .imp-stage')].map((s) => s.className.split(' ').pop())")
    assert stages and all(s in ("computed", "reused") for s in stages), stages
    pg.fits("Import, built")
    pg.shot("e2e_imp_04_done")

    pg.pg.click(f'{DIALOG} [data-a="openp"]')
    until(lambda: pg.project() == "so101-bus" and pg.rev() == "r001", 60, "the imported robot to open")
    assert "project=so101-bus" in pg.pg.url
    pg.pg.click("#projBtn")
    item = pg.text('#projList button[data-p="so101-bus"]')
    assert "imported" in item.lower(), item
    rev, d, s = studio.latest("so101-bus")
    assert s["feasible"] is True and s["pick_place_success"] is True, s.get("issues")
    robot = os.path.join(studio.home, ".robots", d.arm.source)
    with open(os.path.join(robot, "morph_profile.json"), encoding="utf-8") as fh:
        acts = json.load(fh)["actuators"]
    assert set(acts.values()) == {"sts3215"}, acts       # one 12 V bus: the 7.4 V servo can't hold the shoulder
    assert os.path.isfile(os.path.join(robot, "licenses", "LICENSE"))
    pg.check()
    pg.close()


def test_so101_with_the_suggested_servos_says_why_it_is_not_feasible(browser, studio, tmp_path):
    folder = so101_copy(str(tmp_path / "so101_new_calib"))
    pg = Page(browser, studio, (1280, 720), "maker")
    pg.open()
    open_import(pg)
    pg.pg.set_input_files(f'{DIALOG} input[data-in="folder"]', folder)
    reviewed(pg)
    pg.pg.fill(f'{DIALOG} [data-k="name"]', "so101-as-named")
    pg.pg.click(f'{DIALOG} [data-a="commit"]')
    text = finished(pg)
    assert "so101-as-named is built (r001), but its software checks found issues:" in text, text
    issues = pg.texts(f"{DIALOG} .imp-warn li")
    assert any("N·m with margin" in i for i in issues), issues
    assert "A joint needs a stronger servo" in pg.text(f"{DIALOG} .imp-note"), text
    pg.fits("Import, built but not feasible")
    pg.shot("e2e_imp_05_not_feasible_1280")
    rev, d, s = studio.latest("so101-as-named")
    assert s["feasible"] is False and issues == [i[:240] for i in s["issues"][:4]], (issues, s["issues"])  # the build's own
    pg.check()
    pg.close()


def test_an_egg_mesh_becomes_the_object_with_its_handling(browser, studio, tmp_path):
    egg = egg_stl(str(tmp_path / "egg.stl"), metres=False)
    rev0, d0, _ = studio.latest("box-picker")
    assert (d0.object.name, d0.finger.sensor) == ("small box", "none")
    pg = Page(browser, studio, (910, 800), "maker")
    pg.open("project=box-picker")
    open_import(pg)
    pg.pg.set_input_files(f'{DIALOG} input[data-in="files"]', egg)
    reviewed(pg)
    kv = pg.ev("""() => Object.fromEntries([...document.querySelectorAll('.modal.on .imp-kv dt')].map((t) =>
        [t.textContent.trim(), t.nextElementSibling.textContent.replace(/\\s+/g, ' ').trim()]))""")
    assert kv["Name"] == "egg" and kv["Handling"] == "damaged above 25 N (Morph's raw egg preset)", kv
    assert kv["Fingers"].startswith("force-sensing (FSR 402)"), kv
    assert kv["Size"].startswith("44.0 × 57.0 mm"), kv
    until(lambda: pg.ev(PICTURE), 90, "the object's picture")
    pg.fits("Import, an object at 910 px")
    pg.shot("e2e_imp_06_egg_910")
    pg.pg.click(f'{DIALOG} [data-a="commit"]')
    until(lambda: "is applied to this project" in pg.text(DIALOG), 60, "the object to be applied")
    pg.pg.click(f'{DIALOG} [data-a="close"]')
    until(lambda: pg.rev() not in (None, rev0) and not pg.ev("window.morph.state.busy"), 240, "box-picker's new revision")
    rev, d, s = studio.latest("box-picker")
    assert rev != rev0 and (d.object.name, d.object.max_force_n, d.object.friction) == ("egg", 25.0, 0.35)
    assert d.finger.sensor == "fsr402" and s["feasible"] is True, s.get("issues")
    pg.check()
    pg.close()


def _zip(path, entries):
    with zipfile.ZipFile(path, "w") as z:
        for name, (data, link) in entries.items():
            info = zipfile.ZipInfo(name)
            if link:
                info.create_system, info.external_attr = 3, (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, data)
    return path


def test_parts_lists_hostile_archives_and_links(browser, studio, tmp_path):
    before = set(studio.projects())
    parts = tmp_path / "parts.csv"
    parts.write_text("Part,Qty,Unit price,Link\nFeetech STS3215 servo,6,15.90,https://www.feetechrc.com/\n"
                     "12 V 5 A power supply,1,12.50,https://www.digikey.com/\nM3 screw kit,1,8.00,\n"
                     '"=HYPERLINK(""http://example.invalid"")",1,0.01,\n', encoding="utf-8")
    slip = _zip(tmp_path / "slip.zip", {"robot/robot.urdf": ('<robot name="x"><link name="a"/></robot>', False),
                                        "../../escaped.txt": ("escaped", False)})
    link = _zip(tmp_path / "link.zip", {"robot/link.urdf": ("/etc/passwd", True)})
    # a page that gets a refusal logs the request's 400; that is the refusal, not a script error
    pg = Page(browser, studio, (1600, 900), "maker", quiet=("status of 400",))
    pg.open()

    open_import(pg)
    pg.pg.set_input_files(f'{DIALOG} input[data-in="files"]', str(parts))
    reviewed(pg)
    assert pg.text(f'{DIALOG} [data-a="commit"]') == "Read this parts list"
    assert "a parts list doesn't change the robot" in pg.text(DIALOG)
    pg.pg.click(f'{DIALOG} [data-a="commit"]')
    until(lambda: view(pg) == "done", 30, "the parts list to be read")
    assert "Read 4 parts totalling $115.91." in pg.text(DIALOG), pg.text(DIALOG)
    with pg.pg.expect_download() as dl:
        pg.pg.click(f'{DIALOG} [data-a="csv"]')
    rows = list(csv.reader(io.StringIO(open(dl.value.path(), encoding="utf-8-sig").read())))
    assert rows[0][0] == "item" and len(rows) == 5, rows
    assert not any(c.startswith(("=", "+", "-", "@")) for r in rows for c in r), rows   # a spreadsheet runs nothing
    pg.pg.click(f'{DIALOG} [data-a="close"]')

    for path, why in ((slip, "The archive has a '..' path ('../../escaped.txt') that would write outside its folder."),
                      (link, "Nothing left to import: Morph skipped the symlink robot/link.urdf.")):
        open_import(pg)
        pg.pg.set_input_files(f'{DIALOG} input[data-in="files"]', str(path))
        msg = refusal(pg)
        assert why in msg, msg
        pg.pg.click(f'{DIALOG} [data-a="close"]')

    for url, why in (("https://example.com/robot.zip", "example.com is not on the import list"),
                     ("github.com/TheRobotStudio/SO-ARM100", "Morph is offline (network turned off)")):
        open_import(pg)
        pg.pg.fill(f'{DIALOG} form[data-a="url"] input', url)
        pg.pg.click(f'{DIALOG} form[data-a="url"] button[type=submit]')
        msg = refusal(pg)
        assert why in msg, msg
        pg.fits("Import, a refused link")
        pg.pg.click(f'{DIALOG} [data-a="close"]')
    pg.shot("e2e_imp_07_refused")
    assert not list(tmp_path.rglob("escaped.txt"))   # nothing was unpacked anywhere: the home is in tmp_path too
    assert set(studio.projects()) == before
    pg.check()
    pg.close()


def test_an_export_comes_back_as_a_new_project(browser, studio, tmp_path):
    pg = Page(browser, studio, (1280, 720), "maker")
    pg.open("project=box-picker")
    with pg.pg.expect_download() as dl:
        if not pg.ev("!!document.querySelector('#tabsInfo .xport')"):
            pg.pg.click("#inspBtn")
        pg.pg.click("#tabsInfo .xport")
    zpath = str(tmp_path / "box-picker.zip")
    dl.value.save_as(zpath)
    with zipfile.ZipFile(zpath) as z:
        assert "export-manifest.json" in z.namelist()
    open_import(pg)
    pg.pg.set_input_files(f'{DIALOG} input[data-in="files"]', zpath)
    reviewed(pg)
    assert pg.text(f'{DIALOG} [data-a="commit"]') == "Import project"
    pg.pg.fill(f'{DIALOG} [data-k="name"]', "box-picker-again")
    pg.pg.click(f'{DIALOG} [data-a="commit"]')
    text = finished(pg)
    assert "box-picker-again is ready: the export's latest design was rebuilt here" in text, text
    pg.shot("e2e_imp_08_export_back_1280")
    _, d0, s0 = studio.latest("box-picker")
    _, d1, s1 = studio.latest("box-picker-again")
    assert d1.to_dict()["object"] == d0.to_dict()["object"] and d1.to_dict()["arm"] == d0.to_dict()["arm"]
    assert s1["feasible"] is True and abs(s1["unit_cost"] - s0["unit_cost"]) < 0.01
    pg.check()
    pg.close()
