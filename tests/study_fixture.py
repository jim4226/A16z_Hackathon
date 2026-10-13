"""A finished egg-picker revision with only what a robustness study reads (design.json, the gripper stage's grip
target, the pick-and-place simulation and its results), for tests/test_robust.py and tests/test_study_jobs.py. Built
once per test session, even across pytest-xdist workers (a lock file in the shared temp folder), with its robot packed
for study workers at 2 ms; each test module copies it into its own projects folder."""

import json
import os
import pickle
import shutil
import time

from morph.robot import arm, electronics2, simulate
from morph.services import robust
from morph.studio import projects


def make_revision(home, name="egg-picker", fast=True) -> str:
    info = projects.create(str(home), name, origin="default", demo=False, unique=False)
    root = info["root"]
    d = projects.template(name)
    sz = arm.size(d)
    asm = arm.build(d, sz, electronics2.plan(d, {**sz, "grip_target_n": 2.34}))
    rev = os.path.join(root, "r001")
    os.makedirs(rev)
    with open(os.path.join(rev, "design.json"), "w", encoding="utf-8") as fh:
        fh.write(d.dumps())
    doc = simulate.run(asm, d, "pick_place", os.path.join(rev, "sim"), fast=fast)
    pp = {"file": "sim_pick_place.json", "title": "Pick and place", "metrics": doc["metrics"]}
    res = {"rev": "r001", "kind": "robot", "stage_status": {"sim": "computed"}, "provenance": {"sim": "computed"},
           "gripper": {"force": {"target_force_n": 2.34}}, "sims": {"mjcf": "robot.xml", "scenarios": {"pick_place": pp}}}
    with open(os.path.join(rev, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    return root


def _shared(tmp_path_factory) -> str:
    """The folder holding the session's template project and its packed robot (built by whoever gets here first)."""
    base = tmp_path_factory.getbasetemp()
    top = str(base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base)
    done, lock = os.path.join(top, "morph-study-fixture"), os.path.join(top, "morph-study-fixture.lock")
    end = time.time() + 600
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() > end:
                raise TimeoutError(lock) from None
            time.sleep(0.1)
    try:
        if not os.path.exists(os.path.join(done, "ready")):
            shutil.rmtree(done, ignore_errors=True)
            root = make_revision(os.path.join(done, "home"))
            with open(os.path.join(done, "packed.pickle"), "wb") as fh:   # test-only: Morph never reads one from disk
                pickle.dump(robust.prepare(root, "r001", 0.002), fh)
            open(os.path.join(done, "ready"), "w").close()
    finally:
        os.close(fd)
        os.unlink(lock)
    return done


def egg_project(tmp_path_factory, home, name="egg-picker", cache=None) -> str:
    """A copy of the session's egg-picker revision as project `name` in `home`; its packed robot goes into `cache`."""
    src = _shared(tmp_path_factory)
    os.makedirs(home, exist_ok=True)
    root = os.path.join(str(home), name)
    shutil.copytree(os.path.join(src, "home", "egg-picker"), root)
    if cache is not None:
        with open(os.path.join(src, "packed.pickle"), "rb") as fh:
            cache[robust.cache_key(root, "r001", 0.002)] = pickle.load(fh)
    return root
