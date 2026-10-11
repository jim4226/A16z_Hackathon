"""MuJoCo grasp test: close, lift, shake, measure slip.

Collision geometry is built from the same 2D profile as the CAD: the beam and
mount are boxes, and the pad is sliced into thin convex strips so a concave
contour or V-groove is represented faithfully (MuJoCo convex-hulls each mesh,
so a single mesh would erase the groove).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from shapely.geometry import box as sbox

from .design import MOUNT_H, FingerGeometry, object_radius
from .materials import MATERIALS
from .spec import Design

STRIP_MM = 1.0
CLEARANCE_MM = 8.0
JAW_MASS_KG = 0.12      # carriage + finger, a typical small parallel jaw
LIFT_MM = 80.0
SHAKE_MM = 15.0
SHAKE_HZ = 3.0          # ~5.3 m/s^2 peak lateral acceleration


@dataclass
class Trial:
    dx_mm: float = 0.0
    dy_mm: float = 0.0
    friction_scale: float = 1.0
    mass_scale: float = 1.0


def _fmt(vals):
    return " ".join(f"{v:.6g}" for v in vals)


def _pad_pieces(geom: FingerGeometry, design: Design):
    f = design.finger
    u0 = f.length_mm - min(geom.pad_height_mm or f.pad_height_mm, f.length_mm)
    pieces = []
    u = u0
    while u < f.length_mm - 1e-6:
        strip = sbox(u, -1, min(u + STRIP_MM, f.length_mm), geom.pad_face_v + 1)
        part = geom.profile.intersection(strip)
        polys = [part] if part.geom_type == "Polygon" else [g for g in getattr(part, "geoms", []) if g.geom_type == "Polygon"]
        for p in polys:
            if p.area > 0.05:
                pieces.append(np.asarray(p.convex_hull.exterior.coords)[:-1])
        u += STRIP_MM
    return pieces, u0


def build_mjcf(design: Design, geom: FingerGeometry, trial: Trial = Trial()) -> str:
    f, o = design.finger, design.object
    mm = 0.001
    R = object_radius(design)
    mu = o.friction * trial.friction_scale
    mass = o.mass_g / 1000.0 * trial.mass_scale
    w2 = f.width_mm / 2.0

    if o.shape == "cylinder":
        obj_geom = (f'<geom name="obj" type="capsule" size="{R*mm:.5f} {max(o.length_mm/2 - R, 1)*mm:.5f}" '
                    f'euler="90 0 0"')
    elif o.shape == "sphere":
        obj_geom = f'<geom name="obj" type="sphere" size="{R*mm:.5f}"'
    else:
        obj_geom = f'<geom name="obj" type="box" size="{R*mm:.5f} {o.length_mm/2*mm:.5f} {R*mm:.5f}"'
    obj_geom += (f' mass="{mass:.5f}" friction="{mu:.3f} 0.005 0.0001" condim="4" solref="0.004 1" solimp="0.95 0.99 0.001" '
                 f'contype="3" conaffinity="3" rgba="0.85 0.3 0.25 1"/>')

    pieces, pad_u0 = _pad_pieces(geom, design)
    z_top = R + geom.pad_center_u  # pad centre level with object centre
    x_open = R + CLEARANCE_MM + geom.pad_face_v

    assets, fingers = [], []
    for side, sgn in (("l", 1.0), ("r", -1.0)):
        geoms = []
        common = f'friction="{mu:.3f} 0.005 0.0001" condim="4" solref="0.004 1" solimp="0.95 0.99 0.001" contype="2" conaffinity="2" rgba="0.3 0.55 0.9 1"'
        # beam (above the pad) and mount block as boxes
        geoms.append(f'<geom type="box" pos="{_fmt([sgn*f.thickness_mm/2*mm, 0, -pad_u0/2*mm])}" '
                     f'size="{_fmt([f.thickness_mm/2*mm, w2*mm, max(pad_u0/2, 0.1)*mm])}" {common}/>')
        for i, pts in enumerate(pieces):
            name = f"pad_{side}_{i}"
            verts = []
            for (u, v) in pts:
                for y in (-w2, w2):
                    verts += [sgn * v * mm, y * mm, -u * mm]
            assets.append(f'<mesh name="{name}" vertex="{_fmt(verts)}"/>')
            geoms.append(f'<geom type="mesh" mesh="{name}" {common}/>')
        fingers.append(f"""
      <body name="finger_{side}" pos="{_fmt([-sgn*x_open*mm, 0, 0])}" gravcomp="1">
        <joint name="j_{side}" type="slide" axis="{_fmt([sgn, 0, 0])}" range="0 {(R+CLEARANCE_MM)*mm:.4f}" damping="40" armature="0.05"/>
        <inertial pos="0 0 {-geom.pad_center_u/2*mm:.4f}" mass="{JAW_MASS_KG}" diaginertia="1e-4 1e-4 1e-4"/>
        {''.join(geoms)}
      </body>""")

    F = design.gripper.force_n
    return f"""<mujoco model="morph_grasp">
  <option timestep="0.001" integrator="implicitfast" cone="elliptic" impratio="10"/>
  <asset>{''.join(assets)}</asset>
  <worldbody>
    <light pos="0 -0.4 0.6" dir="0 0.5 -0.8"/>
    <geom name="floor" type="plane" size="0.5 0.5 0.01" contype="1" conaffinity="1" friction="0.8"/>
    <body name="object" pos="{_fmt([trial.dx_mm*mm, trial.dy_mm*mm, R*mm + 0.0005])}">
      <freejoint/>
      {obj_geom}
    </body>
    <body name="hand" pos="0 0 {z_top*mm:.5f}" gravcomp="1">
      <joint name="hz" type="slide" axis="0 0 1" damping="400"/>
      <joint name="hx" type="slide" axis="1 0 0" damping="400"/>
      <inertial pos="0 0 0" mass="1.0" diaginertia="0.01 0.01 0.01"/>
      <geom type="box" size="{_fmt([(x_open+MOUNT_H)*mm, (w2+4)*mm, MOUNT_H/2*mm])}" pos="0 0 {(MOUNT_H/2+1)*mm}" contype="0" conaffinity="0" rgba="0.2 0.2 0.2 1"/>
      {''.join(fingers)}
    </body>
  </worldbody>
  <actuator>
    <position name="a_hz" joint="hz" kp="20000" ctrlrange="-1 1"/>
    <position name="a_hx" joint="hx" kp="20000" ctrlrange="-1 1"/>
    <position name="a_l" joint="j_l" kp="4000" forcelimited="true" forcerange="{-F} {F}" ctrlrange="0 0.2"/>
    <position name="a_r" joint="j_r" kp="4000" forcelimited="true" forcerange="{-F} {F}" ctrlrange="0 0.2"/>
  </actuator>
</mujoco>"""


def run_trial(design: Design, geom: FingerGeometry, trial: Trial = Trial(), frames=None) -> dict:
    import mujoco

    model = mujoco.MjModel.from_xml_string(build_mjcf(design, geom, trial))
    data = mujoco.MjData(model)
    obj = model.body("object").id
    hand = model.body("hand").id
    R = object_radius(design)
    close_to = (R + CLEARANCE_MM + 15.0) / 1000.0
    a_l, a_r = model.actuator("a_l").id, model.actuator("a_r").id
    a_hz, a_hx = model.actuator("a_hz").id, model.actuator("a_hx").id

    t_close, t_lift, t_shake, t_end = 0.3, 1.2, 2.2, 3.6
    rel0 = None
    max_slip = 0.0
    contact_at_lift = 0
    dt = model.opt.timestep
    render_every = max(int(1 / 30 / dt), 1)
    step = 0
    while data.time < t_end:
        t = data.time
        data.ctrl[a_l] = data.ctrl[a_r] = close_to if t >= t_close else 0.0
        z = x = 0.0
        if t >= t_lift:
            s = min((t - t_lift) / 0.8, 1.0)
            z = LIFT_MM / 1000.0 * (3 * s * s - 2 * s ** 3)
        if t_shake <= t < t_end - 0.4:
            x = SHAKE_MM / 1000.0 * math.sin(2 * math.pi * SHAKE_HZ * (t - t_shake))
        data.ctrl[a_hz], data.ctrl[a_hx] = z, x
        mujoco.mj_step(model, data)
        rel = data.xpos[obj] - data.xpos[hand]
        if rel0 is None and data.time >= t_lift:
            rel0 = rel.copy()
            contact_at_lift = int(data.ncon)
        if rel0 is not None:
            max_slip = max(max_slip, float(np.linalg.norm(rel - rel0)) * 1000.0)
        if frames is not None and step % render_every == 0:
            frames.append((model, data.qpos.copy()))
        step += 1

    lifted = float(data.xpos[obj][2] * 1000.0 - R)
    final_slip = float(np.linalg.norm((data.xpos[obj] - data.xpos[hand]) - rel0)) * 1000.0
    success = lifted > 0.8 * LIFT_MM and final_slip < 5.0
    return {"success": bool(success), "lift_mm": round(lifted, 1), "final_slip_mm": round(final_slip, 2),
            "max_slip_mm": round(max_slip, 2), "contacts_at_lift": contact_at_lift,
            "trial": trial.__dict__}


def default_trials(n: int = 8, seed: int = 0):
    rng = np.random.default_rng(seed)
    trials = [Trial()]
    for _ in range(n - 1):
        trials.append(Trial(dx_mm=float(rng.uniform(-5, 5)), dy_mm=float(rng.uniform(-10, 10)),
                            friction_scale=float(rng.uniform(0.7, 1.1)),
                            mass_scale=float(rng.uniform(0.9, 1.4))))
    return trials


def min_hold_force(design: Design, geom: FingerGeometry, iters: int = 6) -> float:
    """Bisect the smallest per-finger grip force that survives lift + shake (nominal trial)."""
    lo, hi = 0.0, design.gripper.force_n
    if not run_trial(design, geom)["success"]:
        return float("inf")
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if run_trial(design.with_changes({"gripper.force_n": mid}), geom)["success"]:
            hi = mid
        else:
            lo = mid
    return round(hi, 2)


def simulate(design: Design, geom: FingerGeometry, n_trials: int = 8) -> dict:
    results = [run_trial(design, geom, tr) for tr in default_trials(n_trials)]
    ok = [r for r in results if r["success"]]
    return {
        "trials": results,
        "success_rate": round(len(ok) / len(results), 3),
        "mean_slip_mm": round(float(np.mean([r["final_slip_mm"] for r in results])), 2),
        "worst_slip_mm": round(float(np.max([r["final_slip_mm"] for r in results])), 2),
        "min_hold_force_n": min_hold_force(design, geom),
    }


def render_gif(design: Design, geom: FingerGeometry, path: str, trial: Trial = Trial()) -> bool:
    """Best effort: needs an offscreen GL backend (MUJOCO_GL=egl or osmesa)."""
    try:
        import mujoco
        from PIL import Image

        frames = []
        run_trial(design, geom, trial, frames=frames)
        model = frames[0][0]
        data = mujoco.MjData(model)
        renderer = mujoco.Renderer(model, 360, 480)
        cam = mujoco.MjvCamera()
        cam.lookat[:] = [0, 0, 0.09]
        cam.distance, cam.azimuth, cam.elevation = 0.38, 35, -18
        imgs = []
        for _, qpos in frames:
            data.qpos[:] = qpos
            mujoco.mj_forward(model, data)
            renderer.update_scene(data, cam)
            imgs.append(Image.fromarray(renderer.render()))
        imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=33, loop=0)
        return True
    except Exception as exc:  # no GL in this environment
        print(f"  (gif skipped: {exc.__class__.__name__}: {exc})")
        return False
