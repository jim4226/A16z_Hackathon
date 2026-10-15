"""Windows privacy for Morph's private files (morph/services/winacl.py), checked on any system with a stand-in for icacls: a new
.runs, .tools or .team folder and every .env Morph writes are limited to the user running Morph, the grant before the
inherited permissions go (so a failure can't lock the user out), and nothing ever raises. On macOS and Linux it never
runs: 0600 and 0700 do the job there."""

import subprocess
import types

import pytest

from morph import keys
from morph.services import winacl
from morph.studio.teams import store as team_store
from morph.studio.toolbox import store as tool_store

ENV = {"USERNAME": "Jaron Mohammed", "USERDOMAIN": "LAB-PC"}


class Icacls:
    """Records each icacls command line; `fail` makes the n-th call (1-based) return that exit code or raise."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail or {}

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        what = self.fail.get(len(self.calls), 0)
        if isinstance(what, BaseException):
            raise what
        return types.SimpleNamespace(returncode=what, stdout=b"", stderr=b"")


def test_a_folder_gets_this_user_only_grant_first_then_no_inherited_permissions(tmp_path):
    run = Icacls()
    assert winacl.restrict(str(tmp_path), run=run, nt=True, env=ENV) is True
    assert [argv for argv, _ in run.calls] == [
        ["icacls", str(tmp_path), "/grant:r", "LAB-PC\\Jaron Mohammed:(OI)(CI)F", "/Q"],  # files made in it inherit it
        ["icacls", str(tmp_path), "/inheritance:r", "/Q"]]
    assert all(kw["timeout"] == winacl.TIMEOUT_S and kw["capture_output"] for _, kw in run.calls)


def test_a_file_gets_this_user_only(tmp_path):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    run = Icacls()
    assert winacl.restrict(str(env), run=run, nt=True, env={"USERNAME": "jim"}) is True
    assert run.calls[0][0] == ["icacls", str(env), "/grant:r", "jim:F", "/Q"]


@pytest.mark.parametrize("fail", [{1: 5}, {1: FileNotFoundError("icacls")}, {1: subprocess.TimeoutExpired("icacls", 15)},
                                  {2: 1332}])
def test_a_refused_or_missing_icacls_changes_nothing_and_never_raises(tmp_path, fail):
    run = Icacls(fail)
    assert winacl.restrict(str(tmp_path), run=run, nt=True, env=ENV) is False
    if 1 in fail:  # the grant failed: the inherited permissions stay, so the user can't be locked out
        assert len(run.calls) == 1


@pytest.mark.parametrize("env", [{}, {"USERNAME": ""}, {"USERNAME": 'a"b'}, {"USERNAME": "x", "USERDOMAIN": "a:b"}])
def test_no_usable_user_name_means_nothing_is_run(tmp_path, env):
    run = Icacls()
    assert winacl.restrict(str(tmp_path), run=run, nt=True, env=env) is False and run.calls == []


def test_other_systems_and_missing_paths_run_nothing(tmp_path):
    run = Icacls()
    assert winacl.restrict(str(tmp_path), run=run, nt=False, env=ENV) is False
    assert winacl.restrict(str(tmp_path / "gone"), run=run, nt=True, env=ENV) is False
    assert run.calls == []


@pytest.fixture
def restricted(monkeypatch):
    seen = []
    monkeypatch.setattr(winacl, "restrict", lambda path, **kw: seen.append(path) or True)
    return seen


def test_morph_restricts_each_private_folder_once_when_it_makes_it_and_every_env_it_writes(tmp_path, restricted,
                                                                                           monkeypatch):
    home = tmp_path / "projects"
    tool_store.write_json(str(home / ".runs" / "run_0123456789abcdef.json"), {"status": "working"})
    tool_store.write_json(str(home / ".runs" / "run_fedcba9876543210.json"), {"status": "working"})
    tool_store.write_json(str(home / ".tools" / "state.json"), {"enabled": {}})
    assert restricted == [str(home / ".runs"), str(home / ".tools")]  # once per folder, when it is made
    team_store.ensure_dir(str(home / ".team"), private=True)
    team_store.ensure_dir(str(home / ".team"), private=True)
    team_store.ensure_dir(str(home / "egg-picker" / ".team"))  # comments and reviews: not private, left alone
    assert restricted[2:] == [str(home / ".team")]
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setenv("SUBQ_API_KEY", "unset")
    keys.save({"SUBQ_API_KEY": "sq-test-0000000000"})
    keys.save({"SUBQ_API_KEY": "sq-test-1111111111"})
    assert restricted[3:] == [str(tmp_path / ".env")] * 2  # a save replaces the file, so each one is limited again
