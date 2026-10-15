"""Stage reuse is only safe when GRAPH names everything a stage reads: a stage reruns when a key in its patterns (or an
upstream stage's) changes, or when an upstream stage reruns, and is reused otherwise. Each stage is computed here on a
recording design that logs every key read, and on recording results that log every upstream result read: a design key
outside the stage's patterns and its upstream stages', or the result of a stage that isn't upstream of it, is a
stale-result bug (a cycle time the manual printed from the parent, a pinion radius sizing never saw, an imported
robot's grip limit from the servo rail before a supply change). One generated arm and the SO-101."""

import dataclasses
import fnmatch
import json

import pytest

from morph.robot import pipeline2 as P
from morph.studio.projects import template

# Read, but deliberately no reason to rerun the stage: the simulation's model is named after the project (robot.xml's
# model name), which changes nothing it computes.
IGNORED = {"sim": ("name",)}

# Upstream results a stage reads although GRAPH doesn't rerun it for them, and why that is safe. The simulation grips
# with the electronics plan's grip and welds the plan's boards into the base; both follow only the simulation's own
# inputs (test_the_simulation_reads_the_electronics_plan_only_for_what_its_own_inputs_decide checks it), so a
# controller or supply edit reuses the simulation.
FOREIGN = {"sim": ("eplan",)}

# A value to try for each design key the electronics plan reads that doesn't rerun the simulation.
OTHER = {"electronics.config": "custom_pcb", "electronics.mcu": "rp2040_pico"}

NODE_OF = {out: node for node, out in P.OUTPUT_KEYS.items()}


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


class Got(dict):
    """A revision's results (prefix "") or one stage's result in them (prefix its key, "sizing") that logs every key
    read: "sizing" when a stage takes sizing's result, "sizing.servo_rail" for a key in it, "sizing.*" for all of it."""

    def __init__(self, data, prefix, log):
        super().__init__(data)
        self._prefix, self._log = prefix, log

    def _hit(self, k):
        self._log.add(f"{self._prefix}.{k}" if self._prefix else str(k))

    def __getitem__(self, k):
        self._hit(k)
        return super().__getitem__(k)

    def get(self, k, default=None):
        self._hit(k)
        return super().get(k, default)

    def __contains__(self, k):
        self._hit(k)
        return super().__contains__(k)

    def __iter__(self):
        self._hit("*")
        return super().__iter__()

    def keys(self):
        self._hit("*")
        return super().keys()

    def items(self):
        self._hit("*")
        return super().items()

    def values(self):
        self._hit("*")
        return super().values()


def recording(res: dict, log: set) -> Got:
    top = Got({}, "", log)
    for k, v in res.items():
        dict.__setitem__(top, k, Got(v, k, log) if k in NODE_OF and isinstance(v, dict) else v)
    return top


def upstream(node) -> set:
    seen, todo = set(), list(P.GRAPH[node][1])
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo += P.GRAPH[n][1]
    return seen


def allowed(node) -> set:
    pats = set(IGNORED.get(node, ()))
    for n in upstream(node) | {node}:
        pats |= set(P.GRAPH[n][0])
    return pats


@pytest.fixture(scope="module", params=["egg-picker", "so101"])
def built(request, tmp_path_factory):
    d = template("egg-picker") if request.param == "egg-picker" else template("so101", "so101")
    p = P.RobotProject(str(tmp_path_factory.mktemp("reads") / request.param))
    rev = p.run(d, n_trials=1, verbose=False, fast_sim=True)
    return d, p, p.load_results(rev)


def compute(p, node, design, res, tmp_path):
    """One stage computed the way RobotProject.run computes it, on `design` and `res`; returns the results dict."""
    r, cache = res, {}

    def asm():
        if "a" not in cache:
            from morph.robot import arm, imported
            cache["a"] = (imported.build if design.arm.source else arm.build)(design, r["sizing"], r.get("eplan"))
        return cache["a"]

    p._compute(node, design, r, str(tmp_path), asm, 1, True)
    return r


@pytest.mark.parametrize("node", P.ORDER)
def test_every_design_key_a_stage_reads_reruns_it(built, node, tmp_path):
    d, p, res = built
    log = set()
    compute(p, node, Reads(d, "", log), dict(res), tmp_path)
    assert log, f"{node} read nothing: the recording design isn't reaching it"
    pats = allowed(node)
    stale = sorted(k for k in log if not any(fnmatch.fnmatch(k, pt) for pt in pats))
    assert not stale, f"{node} reads {stale} but GRAPH reruns it for none of them"


@pytest.mark.parametrize("node", P.ORDER)
def test_every_upstream_result_a_stage_reads_reruns_it(built, node, tmp_path):
    # the imported gripper stage read sizing's servo rail with no sizing upstream: an electronics.servo_v edit
    # re-sized the SO-101 and kept its old grip limit (GRIP_LIMIT_N 20.83 N in the firmware against 19.29 N fresh)
    d, p, res = built
    log = set()
    compute(p, node, d, recording(res, log), tmp_path)
    ups = upstream(node)
    stale = sorted({k.split(".")[0] for k in log} & set(NODE_OF) - {P.OUTPUT_KEYS[node]} - set(FOREIGN.get(node, ()))
                   - {P.OUTPUT_KEYS[u] for u in ups})
    assert not stale, f"{node} reads the {stale} result(s) but GRAPH has no such upstream stage"


@pytest.mark.parametrize("node", sorted(P.PARTIAL))
def test_a_stage_reused_on_part_of_an_upstream_result_computes_from_that_part_alone(built, node, tmp_path):
    # RobotProject.run reuses it when that part is unchanged (a wrist camera re-sizes the arm, the gripper stays)
    d, p, res = built
    whole = compute(p, node, d, dict(res), tmp_path / "whole")
    for up, part in P.PARTIAL[node].items():
        key = P.OUTPUT_KEYS[up]
        only = compute(p, node, d, {**res, key: part(d, res[key])}, tmp_path / up)
        assert json.dumps(only[P.OUTPUT_KEYS[node]], sort_keys=True, default=str) == \
            json.dumps(whole[P.OUTPUT_KEYS[node]], sort_keys=True, default=str), f"{node} reads more of {up} than PARTIAL"


def test_the_simulation_reads_the_electronics_plan_only_for_what_its_own_inputs_decide(built):
    """The simulation is reused across a controller or electronics-config edit (FOREIGN): the plan it grips with and
    welds into the base must then give the same simulated robot, whatever those keys are."""
    from morph.robot import arm, electronics2, imported, simulate
    from morph.services.robust import _norm_xml

    d, p, res = built
    log = set()
    electronics2.plan(Reads(d, "", log), res["sizing"], grip=res["gripper"]["force"])
    outside = sorted(k for k in log if not any(fnmatch.fnmatch(k, pt) for pt in allowed("sim")))
    assert set(outside) <= set(OTHER), f"the electronics plan reads {sorted(set(outside) - set(OTHER))}: add a value " \
                                       "to OTHER so this test checks the simulation doesn't depend on it"
    build = imported.build if d.arm.source else arm.build

    def model(design):
        a = build(design, res["sizing"], electronics2.plan(design, res["sizing"], grip=res["gripper"]["force"]))
        return _norm_xml(simulate.build_mjcf(a, design)), {k: v for k, v in a.meta.items() if k.startswith("grip")}

    base = model(d)
    assert base[1].get("grip_target_n"), "the simulated robot carries no grip"
    for k in outside:
        assert model(d.with_changes({k: OTHER[k]})) == base, f"{k}={OTHER[k]} changes the simulated robot"
