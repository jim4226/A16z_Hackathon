"""Release plumbing: the Studio page loads nothing from other sites, the version lives in one place, and an
installed wheel carries every file Morph reads (and keeps .env out of site-packages). Offline."""

import argparse
import ast
import http.client
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import morph
from morph import cli, darwin, keys, resources
from morph.robot import imported
from morph.studio import server

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: pytest depends on tomli there
    import tomli as tomllib

REPO = Path(__file__).resolve().parents[1]
STATIC = Path(server.STATIC)


# ------------------------------------------------------------------ the page

def test_the_page_loads_nothing_from_other_sites():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for text in (page, css):
        assert [u for u in re.findall(r"https?://[^\s\"'<>)]+", text) if "www.w3.org" not in u] == []
    faces = re.findall(r"url\((vendor/fonts/[^)]+)\)", css)
    assert len(faces) == 7
    for face in faces:
        assert (STATIC / face).read_bytes()[:4] == b"wOF2", face
    for name in ("OFL-Inter.txt", "OFL-JetBrainsMono.txt"):
        assert "SIL OPEN FONT LICENSE Version 1.1" in (STATIC / "vendor" / "fonts" / name).read_text(encoding="utf-8")
    csp = server.app_csp()
    assert "https:" not in csp and "'unsafe-hashes'" not in csp
    assert "font-src 'self' data:;" in csp


def test_fonts_are_served_as_fonts(tmp_path, monkeypatch):
    from test_studio_security import _serve, make_project

    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    st = server.Studio(make_project(tmp_path / "projects"), str(tmp_path / "projects"))
    httpd = _serve(st, None)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("GET", "/static/vendor/fonts/inter-latin-400.woff2")
        res = conn.getresponse()
        body = res.read()
        conn.close()
        assert res.status == 200 and res.getheader("Content-Type") == "font/woff2" and body[:4] == b"wOF2"
    finally:
        httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------------------------ one version, one package

def _pyproject() -> dict:
    with open(REPO / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def test_the_version_lives_only_in_morph_init():
    meta = _pyproject()
    assert "version" not in meta["project"] and meta["project"]["dynamic"] == ["version"]
    assert meta["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "morph.__version__"}
    assert re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+)?", morph.__version__)
    assert meta["project"]["requires-python"] == ">=3.10,<3.15"
    assert meta["project"]["license"] == "Apache-2.0"
    extras = meta["project"]["optional-dependencies"]
    assert extras["video"] == ["imageio-ffmpeg"] and extras["cad"] == ["cascadio"]
    assert extras["xacro"] == ["xacro", "PyYAML"] and extras["dev"] == ["pytest"]
    for pattern in meta["project"]["license-files"]:
        assert list(REPO.glob(pattern)), pattern


def test_the_wheel_carries_every_file_read_through_resources():
    """setup.py copies ASSETS into the wheel; every resources.path() in Morph must point inside one of them."""
    tree = ast.parse((REPO / "setup.py").read_text(encoding="utf-8"))
    shipped = next(ast.literal_eval(n.value) for n in ast.walk(tree)
                   if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "ASSETS")
    assert {"robots", "data/darwin-cache", "env.example", "LICENSE", "NOTICE"} <= set(shipped)
    used = set()
    for path in (REPO / "morph").rglob("*.py"):
        for call in re.findall(r"resources\.path\(([^)]*)\)", path.read_text(encoding="utf-8")):
            used.add("/".join(ast.literal_eval(p.strip()) for p in call.split(",") if p.strip()))
    assert {"robots", "data/darwin-cache", "env.example"} <= used
    for rel in used:
        assert any(rel == s or rel.startswith(s + "/") for s in shipped), rel
        assert (REPO / rel).exists(), rel


def test_a_checkout_reads_its_own_folders():
    assert Path(resources.ROOT) == REPO and Path(resources.checkout()) == REPO
    assert Path(keys.ENV_PATH) == REPO / ".env" and Path(keys.TEMPLATE) == REPO / "env.example"
    assert Path(imported.ROOT) == REPO / "robots" and Path(darwin.SHIPPED) == REPO / "data" / "darwin-cache"


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="the XDG branch")
def test_an_installed_wheel_keeps_env_out_of_site_packages(tmp_path, monkeypatch):
    monkeypatch.setattr(resources, "ROOT", resources.BUNDLED)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert resources.checkout() is None and resources.config_dir() == str(tmp_path / "config" / "morph")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert resources.config_dir() == os.path.join(os.path.expanduser("~"), ".config", "morph")


def _as_wheel(tmp_path, monkeypatch) -> Path:
    """An installed wheel (no checkout) started in an empty folder, with the user's folders under tmp_path."""
    monkeypatch.setattr(resources, "ROOT", resources.BUNDLED)
    monkeypatch.setattr(resources, "_HOMES", {})
    monkeypatch.delenv("MORPH_HOME", raising=False)
    for var, sub in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("LOCALAPPDATA", "local"),
                     ("HOME", "home"), ("USERPROFILE", "home")):
        monkeypatch.setenv(var, str(tmp_path / sub))
    from morph.importers import common
    monkeypatch.setattr(common, "_HOME", None)  # what a Studio in this process set
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def test_an_installed_wheel_keeps_projects_in_the_users_data_folder(tmp_path, monkeypatch):
    """A wheel used to keep projects in ./projects of whatever folder it started in, and moved into someone's own folder
    of that name (README's "delete the projects folder" then meant theirs). Now: MORPH_HOME, else the per-user data
    folder, unless ./projects already holds Morph projects. Every module agrees, and doctor prints the absolute path."""
    from morph import settings
    from morph.importers import common
    from morph.studio.teams import store as team_store
    from morph.studio.toolbox import store as tool_store

    monkeypatch.delenv("MORPH_HOME", raising=False)
    assert resources.projects_home() == str(REPO / "projects")  # a checkout: its own folder's
    work = _as_wheel(tmp_path, monkeypatch)
    (work / "projects" / "thesis").mkdir(parents=True)
    (work / "projects" / "thesis" / "notes.txt").write_text("mine", encoding="utf-8")
    want = os.path.join(resources.data_dir(), "projects")
    assert os.path.isabs(want) and not want.startswith(str(work)) and "site-packages" not in want
    if sys.platform.startswith("linux"):
        assert want == str(tmp_path / "data" / "morph" / "projects")
    elif os.name == "nt":
        assert want == str(tmp_path / "local" / "Morph" / "projects")
    assert resources.projects_home() == want == settings.home() == common.home()
    assert tool_store.home_dir() == want == team_store.home_dir()
    assert resources.projects_home_note(want) == ""
    env = {k: v for k, v in os.environ.items() if k != "MORPH_HOME"}
    env.update(PYTHONPATH=str(REPO), MORPH_OFFLINE="1")
    code = ("import argparse, morph.resources as r; r.ROOT = r.BUNDLED; import morph.cli as c; print('HOME=' + c.HOME); "
            "c.cmd_doctor(argparse.Namespace(offline=True))")
    out = subprocess.run([sys.executable, "-c", code], cwd=work, env=env, capture_output=True, text=True, timeout=600)
    assert f"HOME={want}\n" in out.stdout and f"projects folder {want}: can write it" in out.stdout, out.stdout + out.stderr
    assert sorted(os.listdir(work / "projects")) == ["thesis"] and os.listdir(work / "projects" / "thesis") == ["notes.txt"]

    (work / "projects" / "egg-picker").mkdir()  # projects an older wheel made here: Morph keeps using them and says so
    (work / "projects" / "egg-picker" / "ledger.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(resources, "_HOMES", {})
    assert resources.projects_home() == "projects" == settings.home()
    assert tool_store.home_dir() == str(work / "projects")
    note = resources.projects_home_note("projects")
    assert "already holds Morph projects, so Morph keeps using it" in note and want in note and "MORPH_HOME" in note
    monkeypatch.setenv("MORPH_HOME", str(tmp_path / "mine"))
    assert resources.projects_home() == str(tmp_path / "mine") == settings.home()
    assert resources.projects_home_note(str(tmp_path / "mine")) == " (MORPH_HOME)"


def test_a_checkouts_projects_are_in_its_folder_whatever_folder_morph_starts_in(tmp_path, monkeypatch):
    """The launchers install a `morph` command into .venv (README says to activate it): run from another folder,
    `morph doctor` or `morph studio` used that folder's ./projects, someone's own folder of that name too. A checkout's
    projects are in its own folder, an absolute path, from any folder; another folder's Morph projects are left where
    they are, and Morph says how to open them."""
    from morph import settings
    from morph.importers import common
    from morph.studio.teams import store as team_store
    from morph.studio.toolbox import store as tool_store

    monkeypatch.delenv("MORPH_HOME", raising=False)
    monkeypatch.setattr(common, "_HOME", None)
    root = tmp_path / "Morph"                              # a checkout somewhere else (its projects folder not made yet)
    root.mkdir()
    monkeypatch.setattr(resources, "ROOT", str(root))
    work = tmp_path / "work"
    (work / "projects" / "thesis").mkdir(parents=True)
    (work / "projects" / "thesis" / "notes.txt").write_text("mine", encoding="utf-8")
    monkeypatch.chdir(work)
    want = str(root / "projects")
    assert resources.checkout() == str(root) and os.path.isabs(want)
    assert resources.projects_home() == want == settings.home() == common.home() == tool_store.home_dir() == \
        team_store.home_dir()
    assert resources.projects_home_note(want) == ""      # someone's own ./projects: not Morph's, nothing to say
    (work / "projects" / "egg-picker").mkdir()           # Morph projects made here before: left alone, and named
    (work / "projects" / "egg-picker" / "ledger.jsonl").write_text("", encoding="utf-8")
    assert resources.projects_home() == want
    note = resources.projects_home_note(want)
    assert f"the Morph projects in {work / 'projects'} stay where they are" in note and \
        f"MORPH_HOME={work / 'projects'}" in note, note
    monkeypatch.chdir(root)                              # the launchers' folder: nothing to say
    assert resources.projects_home() == want and resources.projects_home_note(want) == ""
    env = {k: v for k, v in os.environ.items() if k != "MORPH_HOME"}
    env.update(PYTHONPATH=str(REPO), MORPH_OFFLINE="1")
    code = (f"import argparse, morph.resources as r; r.ROOT = {str(root)!r}; import morph.cli as c; "
            "print('HOME=' + c.HOME); c.cmd_doctor(argparse.Namespace(offline=True))")
    out = subprocess.run([sys.executable, "-c", code], cwd=work, env=env, capture_output=True, text=True, timeout=600)
    assert f"HOME={want}\n" in out.stdout and f"projects folder {want} (the Morph folder's;" in out.stdout, \
        out.stdout + out.stderr
    assert sorted(os.listdir(work / "projects")) == ["egg-picker", "thesis"]
    assert os.listdir(work / "projects" / "egg-picker") == ["ledger.jsonl"] and os.path.isdir(want)


def test_the_studio_says_where_the_projects_are(tmp_path, monkeypatch, capsys):
    from morph.studio import server
    from test_keys_and_launch import _free_port, _robots

    _robots(tmp_path / "projects")
    monkeypatch.setattr(cli, "HOME", str(tmp_path / "projects"))
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(server, "serve", lambda *a, **k: None)
    cli.cmd_studio(argparse.Namespace(name="egg-picker", host="127.0.0.1", port=_free_port(), open=False, remote=False))
    assert f"Projects folder: {tmp_path / 'projects'}\n" in capsys.readouterr().out


def test_saving_keys_creates_the_settings_folder(tmp_path, monkeypatch):
    target = tmp_path / "config" / "morph" / ".env"
    monkeypatch.setattr(keys, "TEMPLATE", str(tmp_path / "no-template"))
    assert keys.save({"ANTHROPIC_API_KEY": "sk-ant-test-0000"}, path=str(target)) == ["ANTHROPIC_API_KEY"]
    assert target.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=sk-ant-test-0000\n"


def test_doctor_points_wheel_users_at_the_morph_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "HOME", str(tmp_path / "projects"))
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(keys, "TEMPLATE", str(tmp_path / "no-template"))
    monkeypatch.setattr(resources, "ROOT", resources.BUNDLED)
    cli.cmd_doctor(argparse.Namespace(offline=True))
    out = capsys.readouterr().out
    assert "All good: run `morph studio --open`." in out and "run_studio" not in out and "(not a git checkout)" in out


# ------------------------------------------------------------------ launchers

def _launcher(name: str) -> str:
    return (REPO / name).read_bytes().decode("utf-8")


def test_launchers_install_tested_pins_and_never_pull():
    for name in ("run_studio.bat", "run_studio.sh"):
        text = _launcher(name)
        assert "-r requirements.txt -c constraints.txt" in text, name
        commands = [line for line in text.splitlines() if not re.match(r"\s*(REM\b|#|echo\b)", line, re.I)]
        assert not [line for line in commands if "git pull" in line] and "--update" in text, name
        assert "studio egg-picker --open" in text and "python.org/downloads" in text, name
    bat = _launcher("run_studio.bat")
    assert "for %%v in (3.13 3.12 3.11 3.10 3.14) do" in bat and "py -%%v" in bat
    assert "sys.version_info[:2] <= (3, 14)" in bat and "activate.bat" in bat
    sh = _launcher("run_studio.sh")
    assert "for c in python3.13 python3.12 python3.11 python3.10 python3.14 python3; do" in sh
    assert "sys.version_info[:2] <= (3, 14)" in sh and ".venv/bin/activate" in sh
    assert not [n for n in (REPO / "constraints.txt").read_text(encoding="utf-8").splitlines() if n.startswith("-")]


def test_launcher_line_endings():
    assert "\r" not in _launcher("run_studio.sh")
    lines = _launcher("run_studio.bat").split("\n")
    assert all(line.endswith("\r") for line in lines[:-1]) and lines[-1] == "", "run_studio.bat needs CRLF"
    if shutil.which("git") and (REPO / ".git").exists():
        out = subprocess.run(["git", "-C", str(REPO), "check-attr", "eol", "--", "run_studio.bat", "run_studio.sh"],
                             capture_output=True, text=True).stdout
        assert "run_studio.bat: eol: crlf" in out and "run_studio.sh: eol: lf" in out


@pytest.mark.skipif(not shutil.which("bash") or os.name == "nt", reason="needs bash")
def test_shell_launcher_parses():
    assert subprocess.run(["bash", "-n", str(REPO / "run_studio.sh")], capture_output=True).returncode == 0


# ------------------------------------------------------------------ what the README says about keys and switches

def _keys_js_settings() -> list:
    """The titles of the switches under Keys, Privacy and network (static/keys.js SETTINGS)."""
    js = (STATIC / "keys.js").read_text(encoding="utf-8")
    body = js[js.index("const SETTINGS = ["):js.index("];", js.index("const SETTINGS = ["))]
    return re.findall(r"^\s*\['([a-z_]+)', '((?:[^'\\]|\\.)*)'", body, re.M)


def test_the_readme_lists_every_privacy_switch_the_keys_dialog_has():
    """README said "three switches" when the dialog had five; it names each one now, by the dialog's own title."""
    from morph import settings
    switches = _keys_js_settings()
    assert [k for k, _ in switches] == [k for k in settings.DEFAULTS if k != "sourcing_enabled"]  # sourcing: Find agents
    readme = " ".join((REPO / "README.md").read_text(encoding="utf-8").split())
    para = readme[readme.index("**Privacy and network**"):readme.index("`MORPH_OFFLINE=1`")]
    words = {3: "three", 4: "four", 5: "five", 6: "six"}
    assert f"has {words[len(switches)]} switches" in para, para
    for key, title in switches:
        name = title.split(" (")[0]  # "Use Keewano calibration (demo data unless ...)": the name before its note
        assert f"*{name}" in para, (key, name)
    assert "*Allow network lookups (Darwin, SubQ, Claude)* is on" in para
    assert "*Let agents use tools (beta)* is on" in para and "purchased parts* is off" in para


def test_the_readme_says_what_test_keys_covers_and_lists_tool_keys():
    from morph import keys
    readme = " ".join((REPO / "README.md").read_text(encoding="utf-8").split())
    checked = sorted({"Claude", "Keewano", "SubQ"})
    src = (REPO / "morph" / "keys.py").read_text(encoding="utf-8")
    body = src[src.index("def check("):src.index("if not jobs:", src.index("def check("))]
    assert sorted(re.findall(r'jobs\["(\w+)"\]', body)) == checked  # what Test keys tries
    assert "**Test keys** in the same dialog tries the Claude, Keewano and SubQ keys" in readme
    assert "| `MORPH_TOOL_*` |" in readme and "docs/TOOLS.md" in readme
    assert {k["service"] for k in keys.KEYS} <= {"Claude", "Keewano", "SubQ"}


def test_no_doc_or_message_tells_people_to_pip_install_morph_with_extras():
    """Installing "morph" with the video, cad or xacro extra from PyPI fetched an unrelated project called morph (and
    zsh refused the brackets): commands name the packages themselves. Developer installs of this checkout are fine."""
    bad = re.compile(r"pip3?\s+install\s+(-\S+\s+)*[\"']?morph\[(video|cad|xacro|machines|dev)")
    hits = [str(p.relative_to(REPO)) for p, text in _shipped_text_files()
            if p.suffix in (".md", ".py", ".js", ".html", ".bat", ".sh", ".txt", ".toml", ".in") and bad.search(text)]
    assert hits == []


# ------------------------------------------------------------------ constraints

def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_constraints_pin_everything_that_gets_installed():
    pins = {}
    for line in (REPO / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            m = re.fullmatch(r"([A-Za-z0-9._-]+)==([0-9][^\s;]*)(\s*;\s*\S.*)?", line)
            assert m, line
            pins.setdefault(_norm(m[1]), []).append(m[2])
    wanted = set()
    for src in ("requirements.txt", "tools/constraints.in"):
        for line in (REPO / src).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                wanted.add(_norm(re.match(r"[A-Za-z0-9._-]+", line)[0]))
    meta = _pyproject()["project"]
    for dep in meta["dependencies"] + [d for deps in meta["optional-dependencies"].values() for d in deps]:
        wanted.add(_norm(re.match(r"[A-Za-z0-9._-]+", dep)[0]))
    assert sorted(wanted - set(pins)) == []
    assert len(pins["numpy"]) == 3  # 3.10, 3.11 and 3.12+ each need their own numpy


# ------------------------------------------------------------------ release docs

def test_release_docs_exist_link_correctly_and_name_this_version():
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {morph.__version__} " in changelog
    docs = ["README.md", "SECURITY.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md", "docs/INSTALL.md", "docs/PRIVACY.md",
            "docs/RELEASE_CHECKLIST.md", "docs/BOARDREPO.md", "docs/SPONSORS.md", "docs/DEMO_SCRIPT.md", "docs/TEAMS.md",
            "docs/IMPORT.md", "docs/WORLD.md"]
    for doc in docs:
        text = (REPO / doc).read_text(encoding="utf-8")
        for link in re.findall(r"\]\(([^)\s]+)\)", text):
            path = link.split("#", 1)[0]
            if path and not re.match(r"[a-z]+:", path):
                assert (REPO / doc).parent.joinpath(path).resolve().exists(), (doc, link)
    assert not (REPO / "docs" / "HACKATHON_PLAN.md").exists() and (REPO / "docs" / "internal" / "HACKATHON_PLAN.md").exists()
    assert "github.com/jim4226/A16z_Hackathon/security/advisories/new" in (REPO / "SECURITY.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------ what ships: licences, fixtures, paths, build

RECORDED_FIXTURES = ("tests/fixtures/darwin", "tests/fixtures/tools/answers/mouser_wrong_key.json")  # from real services
OWN_FIXTURES = ("tests/fixtures/tools/answers", "tests/fixtures/tools/fixparts.json", "tests/fixtures/launchers")  # made up


def test_the_notices_name_every_vendored_folder_robot_and_recorded_fixture():
    notices = (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    notice = (REPO / "NOTICE").read_text(encoding="utf-8")
    vendor = STATIC / "vendor"
    for folder in [vendor, *(p for p in sorted(vendor.rglob("*")) if p.is_dir())]:
        rel = folder.relative_to(REPO).as_posix()
        assert f"`{rel}/" in notices and f"{rel}/" in notice, rel
    for robot in sorted(p for p in (REPO / "robots").iterdir() if p.is_dir()):
        rel = robot.relative_to(REPO).as_posix()
        assert f"`{rel}/`" in notices and f"{rel}/" in notice, rel
    assert "`data/darwin-cache/`" in notices and "data/darwin-cache/" in notice and "THIRD_PARTY_NOTICES.md" in notice
    for path in sorted(p for p in (REPO / "tests" / "fixtures").rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        rel = path.relative_to(REPO).as_posix()
        whose = next((k for k in RECORDED_FIXTURES + OWN_FIXTURES if rel == k or rel.startswith(k + "/")), None)
        assert whose, f"{rel} is new: say in THIRD_PARTY_NOTICES whether it was recorded from a service or made up"
        assert f"`{whose}" in notices, whose
    for rel in RECORDED_FIXTURES:
        assert rel in notice, rel


def _shipped_text_files():
    """Every text file of the repository (what a release zip or the source archive carries)."""
    if shutil.which("git") and (REPO / ".git").exists():
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, check=True).stdout
        paths = [REPO / p for p in out.decode("utf-8").split("\0") if p]
    else:  # a source archive: no .git
        skip = {".git", ".venv", "projects", "build", "dist", "__pycache__"}
        paths = [p for p in REPO.rglob("*") if p.is_file() and not skip & set(p.relative_to(REPO).parts)]
    for p in paths:
        if p.is_file() and p.suffix not in (".stl", ".glb", ".zip", ".gif", ".png", ".woff2", ".jpg", ".npz"):
            try:
                yield p, p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue


def test_no_development_machine_paths_ship():
    marker = "/tmp/" + "claude-0"  # split, so this file doesn't name it
    assert [str(p.relative_to(REPO)) for p, text in _shipped_text_files() if marker in text] == []


def test_the_wheel_lists_its_files_instead_of_taking_the_source_archives():
    """include-package-data off: the package-data globs carry every Studio file (tools/package_smoke.py checks the
    installed wheel), and setuptools no longer warns about each static/ folder that isn't a package."""
    tool = _pyproject()["tool"]["setuptools"]
    assert tool["include-package-data"] is False
    assert tool["package-data"]["morph.studio"] == ["static/**/*"]
    assert tool["package-data"]["morph.studio.toolbox"] == ["catalog/*.json"]
