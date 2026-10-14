"""Material + process reference data.

Values are typical datasheet numbers, good enough for ranking designs; they are
not a substitute for a coupon test. Printed parts get a layer-adhesion derate.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Material:
    name: str
    E_gpa: float            # Young's modulus
    density_g_cc: float
    yield_mpa: float
    cost_per_kg: float      # USD, stock/filament
    processes: tuple        # processes that can make a finger from it
    print_derate: float = 1.0


MATERIALS = {
    "PLA": Material("PLA", 3.5, 1.24, 50, 20, ("fdm_print",), 0.6),
    "PETG": Material("PETG", 2.1, 1.27, 50, 22, ("fdm_print",), 0.6),
    "Nylon-CF": Material("Nylon-CF", 6.0, 1.15, 70, 60, ("fdm_print",), 0.6),
    "Acrylic": Material("Acrylic", 3.0, 1.18, 65, 12, ("laser_cut", "cnc_router")),
    "Delrin": Material("Delrin", 2.8, 1.41, 65, 18, ("laser_cut", "cnc_router")),
    "HDPE": Material("HDPE", 0.9, 0.95, 25, 8, ("cnc_router",)),
    "Al6061": Material("Al6061", 69.0, 2.70, 276, 8, ("cnc_router",)),
}
# Finger materials only a robot arm's production build uses (morph.robot.arm.LOOK: molded polycarbonate): the gripper
# analysis reads them, the standalone gripper never offers them (none of its processes makes a finger from them).
MOLDED = {"PC": Material("PC", 2.35, 1.20, 62, 4, ("injection_mold",))}

# 1/8" (3.175 mm) end mill on a hobby/desktop router. feed = rpm * flutes * chipload.
CNC_FEEDS = {
    "Al6061": dict(rpm=18000, flutes=1, chipload=0.025, doc=0.4, plunge=150),
    "Delrin": dict(rpm=16000, flutes=2, chipload=0.045, doc=1.5, plunge=400),
    "HDPE": dict(rpm=16000, flutes=2, chipload=0.05, doc=2.0, plunge=400),
    "Acrylic": dict(rpm=16000, flutes=1, chipload=0.05, doc=1.0, plunge=300),
}

# CO2 laser (~60 W) cut speeds for ~6 mm sheet, mm/s.
LASER_SPEED = {"Acrylic": 8.0, "Delrin": 6.0}

PRINT_FLOW_MM3_S = 12.0   # sustained volumetric flow, modern desktop FDM
PRINT_FILL_FRACTION = 0.6  # small parts are mostly perimeters
MACHINE_RATE_PER_HR = {"fdm_print": 2.0, "laser_cut": 30.0, "cnc_router": 40.0}
