"""Backfill Keewano with two weeks of synthetic gripper builds, for dashboards and demos.

Every backfilled project is named `synthetic-*`, so it can be told apart from real builds in any
query (WHERE "Project" NOT LIKE 'synthetic-%'), and the grip-force calibration (keewano.hold_ratio)
never uses them. The made-up part is the measured/predicted holding-force ratio per pad + material
(TRUE_RATIO below, plus noise); everything else follows the shape of a real Morph ledger. Running
with --send is the opt-in to Keewano events for this run (MORPH_TELEMETRY_ENABLED=1).

  python tools/keewano_backfill.py            # dry run: print what would be sent
  python tools/keewano_backfill.py --send     # needs KEEWANO_API_KEY
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

sys.path.insert(0, ".")
from morph import keewano  # noqa: E402
from morph.cli import load_env  # noqa: E402

load_env()  # the same .env the Studio reads

# Assumed real-vs-sim holding force: the sim's rigid fingers and capsule objects overrate grip,
# most for flat pads and slick PLA. These are the invented numbers; label them as such on stage.
TRUE_RATIO = {("flat", "PLA"): 1.55, ("flat", "PETG"): 1.40, ("v", "PLA"): 1.30,
              ("v", "PETG"): 1.20, ("contour", "PLA"): 1.25, ("contour", "PETG"): 1.12}
OBJECTS = [("raw egg", 0.8), ("strawberry", 0.3), ("soda can", 3.6), ("tomato", 1.0), ("light bulb", 0.6)]
SERVOS = ["mg996r", "sg90", "mg90s", "ds3218"]


def build_history(n_projects: int, days: int, seed: int = 7) -> list:
    """[(project, ts, [(event, value), ...]), ...] in time order per project."""
    rng = random.Random(seed)
    now = int(time.time())
    out = []
    for i in range(n_projects):
        obj, sim_hold = rng.choice(OBJECTS)
        name = f"synthetic-{obj.replace(' ', '-')}-{i:02d}"
        t = now - rng.randint(2, days) * 86400 + rng.randint(0, 36000)
        design = {"object": {"name": obj}, "electronics": {"servo": rng.choice(SERVOS)},
                  "finger": {"sensor": "fsr402"}}
        out.append((name, t, keewano.events_for({"type": "human_time", "step": "spec", "minutes": rng.randint(1, 4)})))
        for r in range(1, rng.randint(2, 5)):
            pad, mat = rng.choice(list(TRUE_RATIO))
            design["finger"].update(pad=pad, material=mat)
            pred = round(sim_hold * rng.uniform(0.9, 1.1), 3)
            rev = f"r{r:03d}"
            t += rng.randint(60, 600)
            out.append((name, t, keewano.events_for({
                "type": "revision", "rev": rev, "design": design, "recomputed": ["cad", "sim", "electronics"],
                "predicted": {"min_hold_force_n": pred, "target_force_n": round(pred * 2.2, 2),
                              "servo": design["electronics"]["servo"]}})))
            t += rng.randint(30, 120) * 60  # print, assemble, bench
            meas = round(pred * TRUE_RATIO[(pad, mat)] * rng.gauss(1, 0.08), 3)
            ok = rng.randint(7, 10) if meas < pred * 2.2 else rng.randint(2, 6)
            out.append((name, t, keewano.events_for(
                {"type": "measurement", "rev": rev, "actual": {"min_hold_force_n": meas, "grasp_success_rate": ok / 10},
                 "error_vs_prediction": {"min_hold_force_n": round(meas - pred, 3)}}, design)))
            out.append((name, t + 60, keewano.events_for(
                {"type": "human_time", "step": rng.choice(["review", "test", "corrections"]),
                 "minutes": rng.randint(3, 20)})))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", type=int, default=24)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    hist = build_history(a.projects, a.days)
    n_ev = sum(len(ev) for _, _, ev in hist)
    print(f"{len(hist)} batches, {n_ev} events, {a.projects} synthetic projects over {a.days} days")
    if not a.send:
        for name, t, ev in hist[:3]:
            print(name, time.strftime("%Y-%m-%d %H:%M", time.localtime(t)), ev)
        sys.exit(0)
    os.environ["MORPH_TELEMETRY_ENABLED"] = "1"  # --send is this run's explicit opt-in
    sent = sum(keewano.send(name, ev, ts=t) for name, t, ev in hist)
    print(f"sent {sent}/{len(hist)} batches; give Keewano ~5 minutes before querying")
