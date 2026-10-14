"""Damaged switches fail closed (ROB-7): a settings file, the tools' state or a project's tolerances that can't be read
(a power cut while saved, a hand edit) never turn back on what someone turned off, and never get written over without a
word. The Keys dialog and `morph doctor` say why everything is off."""

import json
import os

import pytest

from morph import settings
from morph.robot.pipeline2 import RobotProject
from morph.services import robust
from morph.studio import server
from morph.studio.toolbox import catalog
from test_studio_rooms import make_project
from toolbox_helpers import FIXTURES, ScriptedClaude, toolbox


@pytest.mark.parametrize("damage", ["", '{\n  "network_enabled": false,\n  "sourcing_ena', "[false]", "null"])
def test_a_damaged_settings_file_keeps_network_lookups_and_agent_tools_off(monkeypatch, damage):
    monkeypatch.setenv("SUBQ_API_KEY", "sk-test-not-a-real-key-0000000000")
    from morph.studio import subq
    assert settings.network_allowed()                                       # no file: the defaults
    settings.save({"network_enabled": False, "agent_tools_enabled": False})
    with open(settings.path(), "w", encoding="utf-8") as fh:
        fh.write(damage)
    assert not settings.network_allowed() and not settings.agent_tools_enabled() and not subq.enabled()
    assert not any(settings.load().values())
    assert "couldn't read its settings file (.settings.json" in settings.problem()
    assert "network lookups off" in settings.describe() and "couldn't read" in settings.describe()
    values = settings.save({"telemetry_enabled": False})                    # saving a switch writes them all, as shown
    assert values == {k: False for k in settings.DEFAULTS} and settings.problem() is None
    assert json.load(open(settings.path(), encoding="utf-8"))["network_enabled"] is False


def test_the_keys_dialog_is_told_why_every_switch_is_off(tmp_path):
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg"), str(home))
    os.makedirs(settings.home(), exist_ok=True)
    with open(settings.path(), "w", encoding="utf-8") as fh:
        fh.write("{")
    status, out = server._api_settings(type("R", (), {"studio": st})())
    assert status == 200 and out["settings_problem"] and out["integrations"]["network"] is False
    assert server._keys_state(st)["settings_problem"] == out["settings_problem"]
    keys_js = open(os.path.join(os.path.dirname(server.__file__), "static", "keys.js"), encoding="utf-8").read()
    assert "d.settings_problem" in keys_js


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "CURATED_DIRS", [FIXTURES])
    return toolbox(tmp_path / "home", ScriptedClaude())


def test_a_damaged_tool_state_keeps_every_tool_off_until_the_owner_turns_one_on(tools):
    on = {t["name"] for t in tools.tools()["tools"] if t["enabled"]}
    assert {"fixparts_offers", "fixparts_categories"} <= on
    tools.set_enabled("fixparts_categories", False)
    state = os.path.join(tools.home, ".tools", "state.json")
    with open(state, "w", encoding="utf-8") as fh:
        fh.write('{"enabled": {"fixparts_categories": fa')
    listed = tools.tools()
    assert not any(t["enabled"] for t in listed["tools"])                    # never Morph's defaults back on
    assert any(e["source"] == ".tools/state.json" and "can't be read" in e["error"] for e in listed["errors"])
    tools.set_enabled("fixparts_offers", True)                              # the owner turns one on
    listed = tools.tools()
    assert {t["name"] for t in listed["tools"] if t["enabled"]} == {"fixparts_offers"}   # the one they chose, only
    assert not any(e["source"] == ".tools/state.json" for e in listed["errors"])


def test_damaged_pins_keep_the_owners_drafted_tools_off(tools):
    pins = os.path.join(tools.home, ".tools", "pins.json")
    os.makedirs(os.path.dirname(pins), exist_ok=True)
    with open(pins, "w", encoding="utf-8") as fh:
        fh.write("{")
    assert any(e["source"] == ".tools/pins.json" for e in tools.tools()["errors"])
    cat = tools.catalog()
    assert all(cat.tools[n].curated for n in catalog.enabled_names(cat, tools.home))


def _studyable(tmp_path):
    home = tmp_path / "projects"
    make_project(home, "egg")
    return str(home / "egg")


def test_damaged_tolerances_are_never_studied_or_written_over(tmp_path):
    root = _studyable(tmp_path)
    robust.save_tolerances(root, {"pad_friction": {"lo": 0.6, "hi": 1.1}, "object_mass_g": {"on": False}})
    path = os.path.join(root, ".studies", "tolerances.json")
    raw = open(path, "rb").read()
    open(path, "wb").write(raw[:-20])
    with pytest.raises(robust.TolerancesUnreadable, match="can't be read"):
        robust.tolerances(root)
    with pytest.raises(robust.TolerancesUnreadable):
        robust.save_tolerances(root, {"grip_force": {"on": False}})     # it used to write a file with only this edit
    assert open(path, "rb").read() == raw[:-20]
    spec = robust.save_tolerances(root, {"reset": True})                 # a reset starts again, on purpose
    assert not any(e.get("edited") for e in spec["params"]) and robust.tolerances(root)["sha"] == spec["sha"]


def test_the_study_card_says_why_it_cant_read_the_tolerances(tmp_path):
    from morph.studio import api_study
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg"), str(home))
    os.makedirs(os.path.join(st.room().p.root, ".studies"))
    with open(os.path.join(st.room().p.root, ".studies", "tolerances.json"), "w", encoding="utf-8") as fh:
        fh.write("{")
    req = type("R", (), {"studio": st, "room": st.room(), "arg": lambda self, k: None})()
    status, out = api_study.get_tolerances(req)
    assert status == 409 and "tolerances.json) can't be read" in out["error"]
    assert RobotProject(st.room().p.root).finished() == ["r001"]
