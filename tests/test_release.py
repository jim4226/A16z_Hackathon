"""Release plumbing: the Studio page loads nothing from other sites, the version lives in one place, and an
installed wheel carries every file Morph reads (and keeps .env out of site-packages). Offline."""

import argparse
import ast
import http.client
import os
import re
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
