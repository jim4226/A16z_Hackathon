"""Show Morph the job as applied in the Studio (found by driving the dialog): a fragile or narrow object the video shows
gets the force-sensing finger in the list of changes, so Apply builds a robot its own checks pass. Offline."""

from morph.spec import Design
from morph.studio import vision

BOX_ARM = {"object.name": "small box", "object.shape": "box", "object.diameter_mm": 45.0, "object.length_mm": 45.0,
           "object.mass_g": 100.0, "object.friction": 0.5, "object.max_force_n": 0.0, "finger.sensor": "none",
           "finger.pad": "flat", "fab.process": "auto", "finger.width_mm": 20.0, "finger.material": "PETG"}  # as New robot made it


def seen(name, shape, width, fragility="robust", **over):
    r = {"usable": True, "unusable_reason": None,
         "task": {"type": "pick_place", "summary": "Moves things into a punnet.", "confidence": 0.85, "evidence_frames": [2]},
         "object": {"name": name, "shape": shape, "diameter_mm": width, "length_mm": width + 5, "size_range_mm": [width - 4, width + 4],
                    "scale_reference": "hand", "mass_g": 30, "fragility": fragility, "surface": "dry", "count_per_cycle": 1,
                    "confidence": 0.8, "evidence_frames": [3]},
         "timing": {"cycle_time_s": 6.0, "cycles_seen": 3, "confidence": 0.7, "evidence_frames": [1]}, "questions": []}
    r.update(over)
    return vision.clean_result({**r, "kind": "video", "frames": 8})


def keys(m, prefix=("finger.sensor", "finger.width_mm", "fab.process", "finger.material")):
    return {k: v for k, v in m["changes"].items() if k in prefix}


def test_a_strawberry_on_a_box_arm_gets_the_force_sensing_finger():
    m = vision.map_to_changes(seen("strawberry", "sphere", 30), Design().with_changes(BOX_ARM))
    assert m["changes"]["object.name"] == "strawberry" and m["changes"]["object.max_force_n"] == 3
    assert keys(m) == {"finger.sensor": "fsr402", "fab.process": "fdm_print"}   # width and PETG were already right
    why = {i["key"]: (i["why"], i["source"]) for i in m["items"]}
    assert why["finger.sensor"] == ("a 30 mm sphere is narrower than the plain finger closes on", "rules")
    assert not any("FSR 402 finger" in n for n in m["notes"])     # the envelope has nothing left to say about it
    clean, notes = vision.validate_changes(m["changes"])          # Apply accepts every key the review listed
    assert clean == m["changes"] and notes == []
    line = vision.describe(m)
    assert "force-sensing fingers" in line and "finger process" not in line


def test_fragile_but_wide_still_gets_the_sensor_and_robust_wide_does_not():
    m = vision.map_to_changes(seen("peach", "sphere", 60, fragility="delicate"), Design().with_changes(BOX_ARM))
    assert keys(m)["finger.sensor"] == "fsr402"
    assert vision.map_to_changes(seen("wooden block", "box", 50), Design().with_changes(BOX_ARM))["changes"].get("finger.sensor") is None
    # an arm that already has it changes nothing about the finger
    has = Design().with_changes({**BOX_ARM, "finger.sensor": "fsr402", "fab.process": "fdm_print"})
    assert keys(vision.map_to_changes(seen("strawberry", "sphere", 30), has)) == {}


def test_only_the_force_sensing_finger_values_pass_apply():
    clean, _ = vision.validate_changes({"finger.sensor": "fsr402", "finger.width_mm": 6.0, "fab.process": "cnc_router",
                                        "finger.material": "Al6061"})
    assert clean == {"finger.sensor": "fsr402", "finger.width_mm": 20.0}   # no CNC fingers from a video
