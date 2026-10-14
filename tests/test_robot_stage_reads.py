"""Stage reuse is only safe when GRAPH names every design key a stage reads: a stage reruns when a key in its patterns
(or an upstream stage's) changes, and is reused otherwise. Each stage is computed here on a recording design that
logs every key read; anything outside the stage's patterns and its upstream stages' is a stale-result bug (a cycle
time the manual printed from the parent, a pinion radius sizing never saw). One generated arm and the SO-101."""

import dataclasses
import fnmatch

import pytest

from morph.robot import pipeline2 as P
from morph.studio.projects import template

# Read, but deliberately no reason to rerun the stage: the simulation's model is named after the project (robot.xml's
# model name), which changes nothing it computes.
IGNORED = {"sim": ("name",)}


class Reads:
    """A Design that logs every key read: dotted names ("object.mass_g"), "<section>.*" for a whole-section dump.
    Keys a stage overrides with with_changes() are its own values, not reads of the design."""
    __slots__ = ("_o", "_p", "_log", "_own")

    def __init__(self, o, p, log, own=frozenset()):
        for k, v in (("_o", o), ("_p", p), ("_log", log), ("_own", own)):
            object.__setattr__(self, k, v)

    def __getattr__(self, name):
        v = getattr(self._o, name)
        key = f"{self._p}.{name}" if self._p else name
        if dataclasses.is_dataclass(v) and not isinstance(v, type):
            return Reads(v, key, self._log, self._own)
        if callable(v):
            if name == "with_changes":
                return lambda ch: Reads(self._o.with_changes(ch), "", self._log, self._own | set(ch))
            if name in ("flat", "to_dict", "dumps"):
                self._log.add(f"{self._p}.*" if self._p else "*")
            return v
        if key not in self._own:
            self._log.add(key)
        return v

    def __setattr__(self, k, v):
        raise AttributeError("a stage never writes to the design")


def allowed(node) -> set:
    pats, seen, todo = set(IGNORED.get(node, ())), set(), [node]
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            pats |= set(P.GRAPH[n][0])
            todo += P.GRAPH[n][1]
    return pats


@pytest.fixture(scope="module", params=["egg-picker", "so101"])
def built(request, tmp_path_factory):
    d = template("egg-picker") if request.param == "egg-picker" else template("so101", "so101")
    p = P.RobotProject(str(tmp_path_factory.mktemp("reads") / request.param))
    rev = p.run(d, n_trials=1, verbose=False, fast_sim=True)
    return d, p, p.load_results(rev)


@pytest.mark.parametrize("node", P.ORDER)
def test_every_design_key_a_stage_reads_reruns_it(built, node, tmp_path):
    d, p, res = built
    log = set()
    spy = Reads(d, "", log)
    r, cache = dict(res), {}

    def asm():
        if "a" not in cache:
            from morph.robot import arm, imported
            cache["a"] = (imported.build if d.arm.source else arm.build)(spy, r["sizing"], r.get("eplan"))
        return cache["a"]

    p._compute(node, spy, r, str(tmp_path), asm, 1, True)
    assert log, f"{node} read nothing: the recording design isn't reaching it"
    pats = allowed(node)
    stale = sorted(k for k in log if not any(fnmatch.fnmatch(k, pt) for pt in pats))
    assert not stale, f"{node} reads {stale} but GRAPH reruns it for none of them"
