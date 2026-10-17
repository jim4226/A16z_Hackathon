"""Show Morph the job (F3): frames -> estimates -> design changes, images, Claude and SubQ (mocked), ffmpeg, the routes.

Offline: Claude and SubQ are fakes, ffmpeg tests make their own clips and skip when no ffmpeg is installed.
"""

import base64
import email.message
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import types

import pytest
from PIL import Image

from morph.spec import Design
from morph.studio import api_video, ffmpeg, vision
from morph.studio.intent import MODEL, OBJECTS

EGG_ROBOT = {"name": "egg-picker", "object.name": "raw egg", "object.shape": "cylinder", "object.diameter_mm": 44.0,
             "object.length_mm": 57.0, "object.mass_g": 60.0, "object.friction": 0.35, "object.max_force_n": 25.0,
             "finger.sensor": "fsr402", "finger.width_mm": 20.0, "finger.length_mm": 52.0, "finger.thickness_mm": 6.0,
             "finger.pad_depth_mm": 5.5, "finger.pad_height_mm": 19.8, "electronics.servo": "sts3215"}
HAS_FFMPEG = bool(shutil.which("ffmpeg"))


def jpeg(w=640, h=360, color=(40, 90, 160)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "JPEG", quality=80)
    return b.getvalue()


def frames(n=8, dt=0.75):
    return [{"t_s": round(i * dt + dt / 2, 2), "jpeg": jpeg(color=(20 * i, 90, 160))} for i in range(n)]


def b64frames(n=8):
    return [{"t_s": f["t_s"], "jpeg_b64": base64.b64encode(f["jpeg"]).decode()} for f in frames(n)]


def egg(**over) -> dict:
    """What Claude returns for a clip of someone moving eggs from a tray to a carton."""
    r = {"usable": True, "unusable_reason": None,
         "task": {"type": "pick_place", "summary": "Moves eggs from a tray into a carton.", "confidence": 0.85, "evidence_frames": [2, 5]},
         "object": {"name": "egg", "shape": "cylinder", "diameter_mm": 44, "length_mm": 57, "size_range_mm": [40, 50],
                    "scale_reference": "hand", "mass_g": 60, "fragility": "delicate", "surface": "smooth", "count_per_cycle": 1,
                    "confidence": 0.8, "evidence_frames": [3]},
         "motion": {"pick": {"x_mm": 230, "y_mm": -90, "z_mm": 0}, "place": {"x_mm": 170, "y_mm": 160, "z_mm": 0},
                    "positions_vary": False, "confidence": 0.7, "evidence_frames": [2, 6]},
         "timing": {"cycle_time_s": 6.5, "cycles_seen": 3, "confidence": 0.65, "evidence_frames": [1, 8]},
         "environment": {"setting": "food", "clutter": "low", "confidence": 0.9, "evidence_frames": [1]},
         "safety": {"other_people_visible": 0, "hands_share_workspace": False, "hazards": [], "confidence": 0.9, "evidence_frames": [1]},
         "questions": ["How many eggs are in one tray?"]}
    for k, v in over.items():
        sec, _, field = k.partition("__")
        if field:
            r[sec][field] = v
        else:
            r[sec] = v
    return r


def result(**over) -> dict:
    return vision.clean_result({**egg(**over), "kind": "video", "frames": 8})


class FakeClaude:
    """messages.create() records the request and answers like the API: text JSON, or a refusal, or a slow reply."""

    def __init__(self, payload=None, stop="end_turn", delay=0.0, raise_exc=None, text=None):
        self.payload, self.stop, self.delay, self.raise_exc, self.text, self.calls = payload, stop, delay, raise_exc, text, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.delay:
            time.sleep(self.delay)
        if self.raise_exc:
            raise self.raise_exc
        text = self.text if self.text is not None else json.dumps(self.payload if self.payload is not None else egg())
        return types.SimpleNamespace(stop_reason=self.stop, content=[types.SimpleNamespace(type="text", text=text)],
                                     usage=types.SimpleNamespace(input_tokens=10500, output_tokens=1800))


@pytest.fixture
def keys(monkeypatch):
    """No real keys anywhere; tests switch the fakes on themselves."""
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "SUBQ_API_KEY", "MORPH_OFFLINE", "MORPH_FFMPEG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(vision, "subq", None)
    return monkeypatch


@pytest.fixture
def claude(keys):
    def use(fake=None):
        fake = fake or FakeClaude()
        keys.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
        keys.setattr(vision, "_claude_client", lambda: fake)
        return fake
    return use


def fake_subq(answers, enabled=True, delay=0.0, calls=None):
    def decide(state, questions, images=None, **kw):
        if calls is not None:
            calls.append({"state": state, "questions": questions, "images": images, **kw})
        if delay:
            time.sleep(delay)
        return None if answers is None else {"answers": answers, "usage": {"input_tokens": 900}, "ms": 420, "source": "subq"}
    return types.SimpleNamespace(enabled=lambda: enabled, decide=decide)


AGREE = {"task_type": {"choice": "pick_place", "confidence": 0.9}, "fragility": {"score": 2.1, "confidence": 0.8},
         "object_visible": {"noul": 0.95}, "hand_for_scale": {"noul": 0.8}, "other_people": {"noul": 0.05},
         "food": {"noul": 0.9}, "repeats": {"noul": 0.9}}


# ------------------------------------------------------------------ mapping: estimates -> design changes
def test_egg_clip_resolves_to_the_raw_egg_preset():
    m = vision.map_to_changes(result(), Design())
    ch = m["changes"]
    assert ch["object.name"] == "raw egg" and ch["object.max_force_n"] == 25 and ch["object.friction"] == 0.35
    assert ch["object.diameter_mm"] == OBJECTS["raw egg"]["diameter_mm"] and ch["object.mass_g"] == 60
    assert (ch["task.pick_x_mm"], ch["task.pick_y_mm"], ch["task.place_x_mm"], ch["task.place_y_mm"]) == (230, -90, 170, 160)
    assert ch["task.cycle_time_s"] == 6.5 and "finger.length_mm" in ch
    assert {i["key"]: i["source"] for i in m["items"]}["object.max_force_n"] == "preset"
    assert [i["key"] for i in m["items"]][0] == "object.name"  # the list reads object first
    assert any("food-safe" in n for n in m["notes"])
    # the egg robot already has the egg: only the demonstrated layout and timing change
    m2 = vision.map_to_changes(result(), Design().with_changes(EGG_ROBOT))
    assert set(m2["changes"]) == {"task.pick_x_mm", "task.pick_y_mm", "task.place_x_mm", "task.place_y_mm", "task.cycle_time_s"}
    assert Design().with_changes(EGG_ROBOT).with_changes(m2["changes"]).task.cycle_time_s == 6.5
    assert vision.describe(m).startswith("From the video: object raw egg; pick (230, -90) mm, place (170, 160) mm")
    assert vision.describe(m, by_hand=True).startswith("From the cards you filled in: object raw egg")  # keyless: no model


def test_cards_filled_in_by_hand_say_so():
    """Keyless (no model looked at the frames): every value the person typed or chose reads "you entered it"."""
    res = vision._empty("video")
    res["manual"] = True
    m = vision.map_to_changes(res, Design(), {"object.name": "parcel", "object.shape": "box", "object.diameter_mm": 60,
                                              "object.mass_g": 120})
    why = {i["key"]: i["why"] for i in m["items"]}
    assert why["object.shape"] == why["object.name"] == why["object.diameter_mm"] == why["object.mass_g"] == "you entered it"


def test_egg_clip_changes_run_through_all_nine_stages(tmp_path):
    """Acceptance: the egg clip resolves to the raw egg preset, all 9 stages run, and the simulation uses the clip's
    pick and place spots."""
    from morph.robot.pipeline2 import ORDER, RobotProject
    from morph.studio.intent import object_changes

    base = Design().with_changes({**EGG_ROBOT, **object_changes("tomato")})
    m = vision.map_to_changes(result(), base)
    assert m["changes"]["object.name"] == "raw egg"
    p = RobotProject(str(tmp_path / "egg"))
    rev = p.run(base.with_changes(m["changes"]), n_trials=1, verbose=False, fast_sim=True)
    res = p.load_results(rev)
    assert set(res["provenance"]) == set(ORDER) and not res.get("errors")
    d = p.load_design(rev)
    assert (d.task.pick_x_mm, d.task.pick_y_mm, d.task.place_x_mm, d.task.place_y_mm) == (230, -90, 170, 160)
    assert d.object.max_force_n == 25 and d.object.friction == 0.35 and d.task.cycle_time_s == 6.5
    pp = res["sims"]["scenarios"]["pick_place"]["metrics"]
    assert pp["reachable"] and pp["cycle_target_s"] == 6.5


def test_a_preset_beats_the_models_guesses():
    m = vision.map_to_changes(result(object__fragility="robust", object__surface="dry", object__mass_g=95, object__name="Eggs"), Design())
    assert m["changes"]["object.max_force_n"] == 25 and m["changes"]["object.friction"] == 0.35 and m["changes"]["object.mass_g"] == 60
    assert vision.preset_for("a soda can") == "soda can" and vision.preset_for("can it lift 2 kg") is None
    assert vision.preset_for("glass jar") is None
    # ...but what the person types beats the preset
    m = vision.map_to_changes(result(), Design(), {"object.mass_g": 65, "object.diameter_mm": 48, "object.fragility": "very_delicate"})
    ch, src = m["changes"], {i["key"]: i["source"] for i in m["items"]}
    assert (ch["object.mass_g"], ch["object.diameter_mm"], ch["object.max_force_n"]) == (65, 48, 3)
    assert src["object.mass_g"] == src["object.max_force_n"] == "user" and src["object.friction"] == "preset"
    assert ch["finger.length_mm"] == 54.0 and not any("preset treats it as robust" in n for n in m["notes"])


def test_low_confidence_becomes_questions_never_changes():
    r = result(object__name="glass jar", object__confidence=0.4, object__scale_reference="none", object__mass_g=None,
               motion__confidence=0.5, timing__confidence=0.3)
    m = vision.map_to_changes(r, Design())
    assert not any(k.startswith(("object.", "task.", "finger.")) for k in m["changes"])
    assert len(m["questions"]) == 3 and [q["id"] for q in m["questions"]] == ["object.name", "object.diameter_mm", "object.mass_g"]
    width = m["questions"][1]
    assert [c["value"] for c in width["chips"]][-1] == "@measure" and width["why"] == "no size reference in view"
    # answering moves them into changes, one by one; the later questions come next
    m = vision.map_to_changes(r, Design(), {"object.name": "@accept", "object.diameter_mm": 70, "object.mass_g": 350})
    assert m["changes"]["object.name"] == "glass jar" and m["changes"]["object.diameter_mm"] == 70 and m["changes"]["object.mass_g"] == 350
    assert {q["id"] for q in m["questions"]} >= {"motion", "timing.cycle_time_s"}
    assert {i["key"]: i["source"] for i in m["items"]}["object.mass_g"] == "user"
    m = vision.map_to_changes(r, Design(), {"object": "@keep", "motion": "@accept", "timing.cycle_time_s": 9})
    assert not any(k.startswith("object.") for k in m["changes"]) and m["changes"]["task.pick_x_mm"] == 230
    assert m["changes"]["task.cycle_time_s"] == 9
    m = vision.map_to_changes(r, Design(), {"motion": "@keep", "object": "@keep", "timing": "@keep"})
    assert not any(k.startswith("task.") for k in m["changes"])


def test_answers_are_checked_like_any_input():
    res, confirmed, kept, notes = vision.apply_answers(result(), {"object.mass_g": 1e9, "object.shape": "cube", "arm.source": "evil",
                                                                  "object.name": "<script>x</script>", "motion": "@accept"})
    assert res["object"]["mass_g"] == 1500 and "object.mass_g" in confirmed and "motion" in confirmed
    assert res["object"]["name"] == "scriptx/script"  # no markup survives (the page escapes it too)
    assert any("object.shape" in n for n in notes) and any("arm.source" in n for n in notes)


def test_reach_is_clamped_with_a_warning():
    r = result(motion__pick={"x_mm": 450, "y_mm": 320, "z_mm": 0}, motion__place={"x_mm": 380, "y_mm": -350, "z_mm": 120},
               motion__confidence=0.9)
    m = vision.map_to_changes(r, Design())
    ch = m["changes"]
    assert (ch["task.pick_x_mm"], ch["task.pick_y_mm"], ch["task.place_y_mm"]) == (400, 300, -300)
    assert ch["arm.reach_mm"] == 500
    notes = " ".join(m["notes"])
    assert "off the table area" in notes and "reach setting stops at 500 mm" in notes and "table height" in notes
    # a layout the current reach covers leaves the reach alone
    assert "arm.reach_mm" not in vision.map_to_changes(result(), Design())["changes"]


def test_people_in_the_workspace_add_an_emergency_stop():
    m = vision.map_to_changes(result(safety__other_people_visible=1), Design().with_changes({"electronics.extras": "wrist_camera"}))
    assert m["changes"]["electronics.extras"] == "wrist_camera,estop"
    assert any("emergency stop" in n for n in m["notes"])
    m = vision.map_to_changes(result(safety__hands_share_workspace=True, safety__confidence=0.4), Design())
    assert "electronics.extras" not in m["changes"]
    q = next(q for q in m["questions"] if q["id"] == "safety.hands_share_workspace")
    assert [c["value"] for c in q["chips"]] == [True, False]
    assert vision.map_to_changes(result(safety__hands_share_workspace=True, safety__confidence=0.4), Design(),
                                 {"safety.hands_share_workspace": True})["changes"]["electronics.extras"] == "estop"
    assert "electronics.extras" not in vision.map_to_changes(result(safety__other_people_visible=2), Design(),
                                                             {"safety.hands_share_workspace": False})["changes"]


def test_varying_positions_add_a_wrist_camera_and_people_an_estop_together():
    m = vision.map_to_changes(result(motion__positions_vary=True, motion__confidence=0.9, safety__other_people_visible=1), Design())
    assert m["changes"]["electronics.extras"] == "wrist_camera,estop"
    assert any("no vision code" in n for n in m["notes"])


def test_custom_objects_get_force_and_friction_from_fragility_and_surface():
    m = vision.map_to_changes(result(object__name="plum", object__fragility="very_delicate", object__surface="wet",
                                     object__shape="irregular", object__diameter_mm=48, object__length_mm=52), Design())
    ch = m["changes"]
    assert ch["object.name"] == "plum" and ch["object.max_force_n"] == 3 and ch["object.friction"] == 0.35
    assert ch["object.shape"] == "sphere" and any("irregular" in n for n in m["notes"])
    assert ch["finger.length_mm"] == 54.0  # fitted to a 48 mm object (radius + 30)
    m = vision.map_to_changes(result(object__name="spanner", object__fragility="robust", object__surface="dry",
                                     object__diameter_mm=20, object__mass_g=None), Design().with_changes(EGG_ROBOT))
    assert m["changes"]["object.max_force_n"] == 0 and m["changes"]["object.friction"] == 0.6
    # Morph's own range note, or the capability envelope's when it is there (F1): either way the width is called out
    assert any("20 mm" in n for n in m["notes"]) and m["questions"][0]["id"] == "object.mass_g"


def test_cycle_time_from_marks_and_clamps():
    r = vision.clean_result({**egg(), "kind": "video", "frames": 8, "marks": {"grasp_s": 2.0, "release_s": 3.1}})
    vision._timing_from_marks(r, frames(8), r["marks"])
    assert r["timing"]["basis"] == "marks" and r["timing"]["cycle_time_s"] == 2.2 and r["timing"]["confidence"] == 0.8
    m = vision.map_to_changes(r, Design())
    assert m["changes"]["task.cycle_time_s"] == 4.0 and any("at least about 4 s" in n for n in m["notes"])
    assert {i["key"]: i["source"] for i in m["items"]}["task.cycle_time_s"] == "marks"
    # cards filled in by hand: the marks still time the cycle (the server works it out, not the browser)
    m = vision.map_to_changes({"kind": "video", "usable": True, "frames": 6, "marks": {"grasp_s": 1.0, "release_s": 4.5}}, Design())
    assert m["changes"] == {"task.cycle_time_s": 7.0}


def test_unusable_results_change_nothing():
    m = vision.map_to_changes(result(usable=False, unusable_reason="No hands or objects are visible."), Design())
    assert m["changes"] == {} and m["notes"][0] == "No hands or objects are visible." and m["slots"] == {}
    m = vision.map_to_changes(None, Design())
    assert m == {"changes": {}, "items": [], "notes": [], "questions": [], "slots": {}}


def test_out_of_scope_tasks_are_said_plainly():
    m = vision.map_to_changes(result(task__type="pour"), Design())
    assert any("isn't something they do" in n for n in m["notes"])
    m = vision.map_to_changes(result(task__type="sort"), Design())
    assert any("Simulated as a pick and place" in n for n in m["notes"])


def test_validate_changes_only_takes_what_a_video_may_set():
    out, notes = vision.validate_changes({"object.mass_g": 99999, "arm.source": "../../etc", "name": "x", "object.shape": "box",
                                          "electronics.extras": "estop,laser,wrist_camera,estop", "task.cycle_time_s": "7.5"})
    assert out == {"object.mass_g": 1500.0, "object.shape": "box", "electronics.extras": "estop,wrist_camera", "task.cycle_time_s": 7.5}
    assert any("arm.source" in n for n in notes) and any("name" in n for n in notes)
    for bad in (None, {}, [], {"arm.source": "so101"}, {k: 1 for k in [f"x{i}" for i in range(41)]}):
        with pytest.raises(ValueError):
            vision.validate_changes(bad)
    Design().with_changes(out)  # every accepted key is a real design key


def test_clean_result_bounds_anything_a_client_sends():
    r = vision.clean_result({"kind": "video", "frames": 3, "object": {"name": "x" * 500, "diameter_mm": float("nan"), "mass_g": -5,
                                                                     "confidence": 7, "evidence_frames": [1, 2, 99, True, "3"]},
                             "motion": {"pick": {"x_mm": 1e9, "y_mm": "a"}, "confidence": "high"}, "evil": {"x": 1},
                             "second_opinion": {"disagree": {"task.type": {"claude": "pick_place", "subq": "<b>"}}}})
    o = r["object"]
    assert len(o["name"]) == 60 and o["diameter_mm"] is None and o["mass_g"] == 0.1 and o["confidence"] == 1.0
    assert o["evidence_frames"] == [1, 2] and r["motion"]["pick"] is None and r["motion"]["confidence"] == 0.0
    assert "evil" not in r and r["second_opinion"]["disagree"] == {}


def test_slots_for_the_brief_engine_only_carry_what_is_sure(monkeypatch):
    monkeypatch.setitem(sys.modules, "morph.studio.brief", None)  # without F1's brief the layout is the spots themselves
    s = vision.to_slots(result())
    assert s["object"]["value"]["key"] == "raw egg" and s["object"]["value"]["max_force_n"] == 25
    assert s["object"]["source"] == "video" and s["layout"]["value"] == {"pick": [230.0, -90.0], "place": [170.0, 160.0]}
    assert s["task"]["value"] == "pick_place" and s["environment"]["value"] == "kitchen" and s["throughput"]["value"] == 6.5
    s = vision.to_slots(result(object__confidence=0.3, motion__confidence=0.2))
    assert "object" not in s and "layout" not in s
    s = vision.to_slots(result(object__confidence=0.3), answers={"object.name": "@accept"})
    assert s["object"]["confidence"] == 1.0


def test_the_layout_slot_names_the_brief_layout_nearest_the_demonstrated_spots(monkeypatch):
    fake = types.ModuleType("morph.studio.brief")
    fake.LAYOUTS = {"tray_to_bin": {"pick": (220.0, -80.0), "place": (160.0, 170.0)},
                    "side_to_side": {"pick": (180.0, -150.0), "place": (180.0, 150.0)}}
    monkeypatch.setitem(sys.modules, "morph.studio.brief", fake)
    s = vision.to_slots(result())
    assert s["layout"]["value"] == "tray_to_bin" and s["layout"]["source"] == "video"
    far = result(motion={"pick": {"x_mm": 390, "y_mm": 250, "z_mm": 0}, "place": {"x_mm": 100, "y_mm": -250, "z_mm": 0},
                         "positions_vary": False, "confidence": 0.9, "evidence_frames": [1]})
    assert "layout" not in vision.to_slots(far)  # no named layout is close: the brief asks, the spots go as changes
    assert vision.map_to_changes(far)["changes"]["task.pick_x_mm"] == 390


def test_the_capability_envelope_has_the_last_word_on_size(monkeypatch):
    import morph.studio as pkg
    said = []
    fake = types.ModuleType("morph.studio.capability")
    fake.check = lambda design: {"status": "adjusted", "notes": list(said)}
    monkeypatch.setitem(sys.modules, "morph.studio.capability", fake)
    monkeypatch.setattr(pkg, "capability", fake, raising=False)
    pan = result(object={**egg()["object"], "name": "cast iron pan", "diameter_mm": 250, "mass_g": 1400, "fragility": "robust",
                         "surface": "dry"})
    rough = "A 250 mm object is outside the gripper's usual 26 to 75 mm range."
    assert rough in vision.map_to_changes(pan, Design())["notes"]  # the envelope said nothing: Morph's own note stays
    said[:] = ["250 mm is wider than the 75 mm the gripper opens around", "1400 g is more than any desk arm here lifts"]
    notes = vision.map_to_changes(pan, Design())["notes"]
    assert rough not in notes and said[0] in notes and said[1] in notes
    said[:] = ["1400 g is more than any desk arm here lifts"]
    assert rough in vision.map_to_changes(pan, Design())["notes"]  # a note about something else doesn't replace it


# ------------------------------------------------------------------ images
def test_photos_lose_exif_and_gps_and_come_out_upright():
    im = Image.new("RGB", (400, 200), (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    exif[0x0112] = 6  # stored sideways: rotate 90 degrees to view
    gps = exif.get_ifd(0x8825)
    gps.update({1: "N", 2: (40.0, 26.0, 46.0), 3: "W", 4: (79.0, 58.0, 56.0)})
    b = io.BytesIO()
    im.save(b, "JPEG", exif=exif)
    assert b"PhoneMaker" in b.getvalue()
    out = vision.prepare_image(b.getvalue())
    got = Image.open(io.BytesIO(out["jpeg"]))
    assert (out["w"], out["h"]) == got.size == (200, 400)
    assert not dict(got.getexif()) and b"PhoneMaker" not in out["jpeg"] and b"Exif" not in out["jpeg"]
    big = vision.prepare_image(jpeg(3000, 2000), long_edge=1280)
    assert (big["w"], big["h"]) == (1280, 853)
    cropped = vision.prepare_image(jpeg(1000, 500), crop={"x": 0.5, "y": 0, "w": 0.5, "h": 0.5})
    assert (cropped["w"], cropped["h"]) == (500, 250)


def test_image_limits():
    with pytest.raises(vision.ImageError, match="larger than 4 MiB"):
        vision.prepare_image(b"\xff\xd8\xff" + b"0" * (4 << 20))
    with pytest.raises(vision.ImageError, match="JPEG, PNG or WebP"):
        vision.prepare_image(b"GIF89a" + b"0" * 100)
    with pytest.raises(vision.ImageError, match="couldn't read"):
        vision.prepare_image(b"\xff\xd8\xff\xe0" + b"garbage" * 10)
    bomb = io.BytesIO()
    Image.new("1", (10000, 10000)).save(bomb, "PNG")  # 12 KB on disk, 100 megapixels decoded
    with pytest.raises(vision.ImageError, match="50 megapixels"):
        vision.prepare_image(bomb.getvalue(), max_bytes=1 << 30)
    with pytest.raises(vision.ImageError, match="crop box"):
        vision.prepare_image(jpeg(), crop={"x": "left"})


def test_frames_are_checked_before_they_are_decoded(monkeypatch):
    good = b64frames(2)
    assert [f["t_s"] for f in vision.decode_frames(good)] == [0.38, 1.12]
    with pytest.raises(ValueError, match="at most 12"):
        vision.decode_frames(b64frames(1) * 13)
    with pytest.raises(ValueError, match="at least one"):
        vision.decode_frames([])
    calls = []
    real = base64.b64decode
    monkeypatch.setattr(vision.base64, "b64decode", lambda *a, **k: calls.append(1) or real(*a, **k))
    with pytest.raises(ValueError, match="larger than 4 MiB"):
        vision.decode_frames([{"t_s": 1, "jpeg_b64": "A" * (6 << 20)}])
    assert not calls  # refused on its length alone
    with pytest.raises(ValueError, match="isn't a JPEG"):
        vision.decode_frames([{"t_s": 1, "jpeg_b64": base64.b64encode(b"\x89PNG\r\n\x1a\n....").decode()}])
    with pytest.raises(ValueError, match="valid base64"):
        vision.decode_frames([{"t_s": 1, "jpeg_b64": "not base64!"}])
    for t in (-1, float("inf"), True, "3"):
        with pytest.raises(ValueError, match="timestamp"):
            vision.decode_frames([{"t_s": t, "jpeg_b64": good[0]["jpeg_b64"]}])
    assert vision.decode_frames([{"t_s": None, "jpeg_b64": "data:image/jpeg;base64," + good[0]["jpeg_b64"]}])[0]["t_s"] is None


# ------------------------------------------------------------------ Claude (mocked)
def test_no_key_means_no_network_and_the_manual_form(keys, monkeypatch):
    monkeypatch.setattr(vision, "_claude_client", lambda: pytest.fail("Claude must not be called without a key"))
    assert vision.analyze_frames(frames()) is None and vision.analyze_photo(jpeg()) is None
    assert "no Claude key" in vision.offline_reason()
    keys.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    keys.setenv("MORPH_OFFLINE", "1")
    monkeypatch.setattr(vision, "_network_allowed", lambda: False)
    assert vision.analyze_frames(frames()) is None and "Network lookups are off" in vision.offline_reason()


def test_claude_success_uses_structured_output(claude):
    fake = claude()
    events = []
    res = vision.analyze_frames(frames(), marks={"grasp_s": 1.1, "release_s": 3.6}, on_event=events.append)
    call = fake.calls[0]
    assert call["model"] == MODEL and call["output_config"]["format"]["type"] == "json_schema"
    assert "tool_choice" not in call and "tools" not in call and call["output_config"]["effort"] == "low"
    props = call["output_config"]["format"]["schema"]["properties"]
    assert set(props) == {"usable", "unusable_reason", "task", "object", "motion", "timing", "environment", "safety", "questions"}
    assert "never describe or identify them" in call["system"] and "Frame 1, t = 0.4 s" in json.dumps(call["messages"])
    content = call["messages"][0]["content"]
    assert sum(1 for c in content if c["type"] == "image") == 8 and "marked the grasp at t = 1.1 s" in content[-1]["text"]
    assert res["usable"] and res["source"] == "claude" and res["cost_usd_est"] == pytest.approx(0.078)
    assert res["confidence"]["object"] == 0.8 and res["evidence_frames"]["task"] == [2, 5]
    assert res["timing"]["basis"] == "marks" and res["timing"]["cycle_time_s"] == 5.0
    kinds = [(e["type"], e.get("provider"), e["status"]) for e in events]
    assert ("agent", "claude", "start") in kinds and ("agent", "claude", "done") in kinds and kinds[-1] == ("video", None, "done")
    assert all(e.get("role") == "vision" for e in events if e["type"] == "agent")
    assert events[-1]["result"] is res


def test_claude_client_has_a_timeout_and_one_retry(monkeypatch):
    seen = {}
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: seen.update(kw) or "client")
    assert vision._claude_client() == "client" and seen == {"timeout": vision.CLAUDE_TIMEOUT_S, "max_retries": 1}
    assert 2 * vision.CLAUDE_TIMEOUT_S < vision.TIMEOUT_S


@pytest.mark.parametrize("fake,code", [
    (FakeClaude(stop="refusal", text=""), "refusal"),
    (FakeClaude(text="{not json"), "invalid"),
    (FakeClaude(payload={"usable": True}), "invalid"),
    (FakeClaude(stop="max_tokens", text="{\"usable\": tr"), "cut_off"),
    (FakeClaude(raise_exc=type("AuthenticationError", (Exception,), {})("401")), "failed"),
])
def test_claude_failures_fall_back_to_the_cards(claude, fake, code):
    claude(fake)
    res = vision.analyze_frames(frames(4))
    assert res["usable"] is False and res["error"] == code and res["source"] is None
    assert "Fill in the cards yourself" in res["unusable_reason"]
    assert vision.map_to_changes(res, Design())["changes"] == {}
    if code == "failed":
        assert "the key was rejected" in res["unusable_reason"]


def test_claude_timeout_gives_up_in_time_with_progress(claude, monkeypatch):
    claude(FakeClaude(delay=3.0))
    monkeypatch.setattr(vision, "TIMEOUT_S", 0.6)
    monkeypatch.setattr(vision, "PROGRESS_S", 0.2)
    events, t0 = [], time.time()
    res = vision.analyze_frames(frames(3), on_event=events.append)
    assert time.time() - t0 < 1.5
    assert res["error"] == "timeout" and "didn't answer within" in res["unusable_reason"]
    assert any(e["type"] == "video" and e["status"] == "progress" for e in events)
    assert res["cost_usd_est"] > 0  # the frames were sent, so they may be billed


def test_photo_analysis_fills_the_object_card_only(claude):
    fake = claude(FakeClaude(payload={"usable": True, "unusable_reason": None, "object": egg()["object"], "questions": []}))
    res = vision.analyze_photo(jpeg(3000, 2000))
    assert res["kind"] == "photo" and res["object"]["name"] == "egg" and res["task"] is None and res["motion"] is None
    img = next(c for c in fake.calls[0]["messages"][0]["content"] if c["type"] == "image")
    w, h = Image.open(io.BytesIO(base64.b64decode(img["source"]["data"]))).size
    assert max(w, h) == 2576 and "object analyst" in fake.calls[0]["system"]
    m = vision.map_to_changes(res, Design())
    assert m["changes"]["object.name"] == "raw egg" and m["slots"]["object"]["source"] == "photo"


# ------------------------------------------------------------------ SubQ second opinion (mocked)
def test_subq_agreement(claude, monkeypatch):
    claude()
    calls = []
    monkeypatch.setattr(vision, "subq", fake_subq(AGREE, calls=calls))
    res = vision.analyze_frames(frames(8), marks={"grasp_s": 2.6, "release_s": 4.1}, use_subq=True)
    op = res["second_opinion"]
    assert res["source"] == "claude+subq" and op["agree"] == {"task": True, "fragility": True} and op["disagree"] == {}
    c = calls[0]
    assert len(c["images"]) == 4 and c["cache"] is False and c["timeout"] == vision.SUBQ_TIMEOUT_S
    assert all(set(i) == {"mediaType", "base64"} and not i["base64"].startswith("data:") for i in c["images"])
    assert [f["role"] for f in c["state"]["frames"]] == ["overview", "grasp", "frame", "release"]  # only what is known
    assert [f["i"] for f in c["state"]["frames"]] == [1, 4, 5, 6]  # the marked grasp and release frames
    assert set(c["questions"]) == {"task_type", "fragility", "object_visible", "hand_for_scale", "other_people", "food", "repeats"}
    assert not [q for q in vision.map_to_changes(res, Design())["questions"] if q["why"] == "second opinion"]


def test_subq_disagreement_becomes_a_question(claude, monkeypatch):
    claude(FakeClaude(payload=egg(object__name="plum", object__fragility="delicate")))
    answers = {**AGREE, "task_type": {"choice": "sort", "confidence": 0.8}, "fragility": {"score": 0.2, "confidence": 0.9},
               "other_people": {"noul": 0.8}, "repeats": {"noul": 0.1}}
    monkeypatch.setattr(vision, "subq", fake_subq(answers))
    res = vision.analyze_frames(frames(8), use_subq=True)
    assert set(res["second_opinion"]["disagree"]) == {"task.type", "object.fragility", "safety.other_people_visible", "timing.cycle_time_s"}
    assert res["task"]["confidence"] <= 0.5 and res["object"]["confidence"] <= 0.5
    assert any("crop them out" in n for n in res["notes"])
    m = vision.map_to_changes(res, Design())
    q = {x["id"]: x for x in m["questions"]}
    assert [c["value"] for c in q["task.type"]["chips"]] == ["pick_place", "sort"]
    assert [c["value"] for c in q["object.fragility"]["chips"]] == ["delicate", "robust"]
    assert "object.max_force_n" not in m["changes"] and "task.cycle_time_s" not in m["changes"]
    # the browser sends the result back with the person's answer: the disagreement survives the round trip
    back = json.loads(json.dumps(res))
    m = vision.map_to_changes(back, Design().with_changes(EGG_ROBOT), {"object.fragility": "robust", "task.type": "pick_place",
                                                                      "object": "@accept"})
    assert m["changes"]["object.max_force_n"] == 0 and not [x for x in m["questions"] if x["why"] == "second opinion"]


def test_subq_alone_and_subq_missing(keys, monkeypatch):
    monkeypatch.setattr(vision, "subq", fake_subq(AGREE))
    res = vision.analyze_frames(frames(5), use_subq=True)
    assert res["source"] == "subq" and res["task"]["type"] == "pick_place" and res["object"]["fragility"] == "delicate"
    assert res["object"]["confidence"] <= 0.5 and res["motion"] is None
    monkeypatch.setattr(vision, "subq", fake_subq(None))
    assert vision.analyze_frames(frames(2), use_subq=True)["usable"] is False
    monkeypatch.setattr(vision, "subq", fake_subq(AGREE, enabled=False))
    assert vision.analyze_frames(frames(2), use_subq=True) is None


def test_subq_failure_keeps_claudes_answer(claude, monkeypatch):
    claude()
    monkeypatch.setattr(vision, "subq", fake_subq(None))
    events = []
    res = vision.analyze_frames(frames(4), use_subq=True, on_event=events.append)
    assert res["usable"] and res["source"] == "claude" and res["second_opinion"] is None
    assert ("agent", "subq", "error") in [(e["type"], e.get("provider"), e["status"]) for e in events]


def test_subq_frames():
    fr = frames(8)
    assert vision.subq_frames(fr, None) == [0, 2, 4, 5]
    assert vision.subq_frames(fr[:3], None) == [0, 1, 2]
    assert vision.subq_frames(fr, {"grasp_s": 4.9, "release_s": 5.6}) == [0, 5, 6, 7]  # carrying = grasp: the nearest stands in


# ------------------------------------------------------------------ ffmpeg
@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg is not installed")
    d = tmp_path_factory.mktemp("clips")
    src = ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=6"]
    made = {}
    for name, codec in (("vp9.webm", ["-c:v", "libvpx-vp9", "-b:v", "800k", "-deadline", "realtime", "-cpu-used", "8"]),
                        ("h264.mp4", ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])):
        out = d / name
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *src, *codec, str(out)], capture_output=True, timeout=120)
        if r.returncode == 0:
            made[name] = out
    rot = d / "h264_rot90.mp4"
    if "h264.mp4" in made:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", "90", "-i", str(made["h264.mp4"]),
                            "-c", "copy", str(rot)], capture_output=True, timeout=60)
        if r.returncode == 0:
            made["rot"] = rot
    if not made:
        pytest.skip("this ffmpeg can't encode VP9 or H.264")
    return made


def test_find_ffmpeg_order(tmp_path, keys, monkeypatch):
    exe = tmp_path / "my-ffmpeg"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    keys.setenv("MORPH_FFMPEG", str(exe))
    assert ffmpeg.find() == str(exe)
    keys.setenv("MORPH_FFMPEG", str(tmp_path / "missing"))
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: None)
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", types.SimpleNamespace(get_ffmpeg_exe=lambda: str(exe)))
    assert ffmpeg.find() == str(exe)  # MORPH_FFMPEG broken, nothing on PATH: imageio-ffmpeg's copy
    assert "MORPH_FFMPEG" in ffmpeg.status().get("warning", "")
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", types.SimpleNamespace(get_ffmpeg_exe=lambda: (_ for _ in ()).throw(RuntimeError("no"))))
    assert ffmpeg.find() is None and ffmpeg.status()["available"] is False


def test_sniff_containers():
    assert ffmpeg.sniff(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 20) == "mov"
    assert ffmpeg.sniff(b"\x00\x00\x00\x08wide\x00\x00\x00\x00mdat") == "mov"
    assert ffmpeg.sniff(b"\x1a\x45\xdf\xa3" + b"\x00" * 20) == "matroska"
    assert ffmpeg.sniff(b"RIFF\x00\x00\x00\x00AVI LIST") == "avi"
    for bad in (b"#EXTM3U\nhttp://169.254.169.254/x", b"ffconcat version 1.0\nfile '/etc/passwd'", b"\xff\xd8\xff\xe0", b""):
        assert ffmpeg.sniff(bad) is None


def test_extract_frames_in_one_pass(clips, tmp_path):
    for name in ("vp9.webm", "h264.mp4"):
        if name not in clips:
            continue
        work = tmp_path / name.split(".")[0]
        work.mkdir()
        out = ffmpeg.extract(str(clips[name]), workdir=str(work))
        ts = [f["t_s"] for f in out["frames"]]
        assert len(ts) == 8 and ts == sorted(ts) and 0 <= ts[0] < 0.6 and 5.0 < ts[-1] <= 6.0, ts
        assert all(f["jpeg"][:3] == b"\xff\xd8\xff" and max(f["w"], f["h"]) <= 1280 for f in out["frames"])
        assert out["duration_s"] == pytest.approx(6.0, abs=0.1) and out["decoder"] == "ffmpeg"
        assert len(ffmpeg.extract(str(clips[name]), count=3, workdir=str(work))["frames"]) == 3
    if "rot" in clips:
        work = tmp_path / "rot"
        work.mkdir()
        f = ffmpeg.extract(str(clips["rot"]), count=2, workdir=str(work))["frames"][0]
        assert (f["w"], f["h"]) == (720, 1280)  # the display matrix is applied: portrait


def test_ffmpeg_runs_as_a_plain_argument_list(clips, tmp_path, monkeypatch):
    seen = []
    real = subprocess.run
    monkeypatch.setattr(ffmpeg.subprocess, "run", lambda args, **kw: seen.append((args, kw)) or real(args, **kw))
    name = next(iter(clips))
    ffmpeg.extract(str(clips[name]), count=2, workdir=str(tmp_path))
    for args, kw in seen:
        assert isinstance(args, list) and "shell" not in kw and kw["timeout"] <= ffmpeg.TIMEOUT_S
        assert set(kw["env"]) <= {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"} and kw["stdin"] == subprocess.DEVNULL
        assert args[args.index("-i") - 2:args.index("-i")] in (["-f", "mov"], ["-f", "matroska"])
        assert "-protocol_whitelist" in args


def test_ffmpeg_refusals(clips, tmp_path, monkeypatch):
    fake = tmp_path / "playlist.mp4"
    fake.write_bytes(b"#EXTM3U\n#EXTINF:1,\nhttp://example.com/a.ts\n")
    with pytest.raises(ffmpeg.FFmpegError) as e:
        ffmpeg.extract(str(fake), workdir=str(tmp_path))
    assert e.value.code == "not_video"
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\x00\x00\x00\x20ftypisom" + os.urandom(4096))
    with pytest.raises(ffmpeg.FFmpegError) as e:
        ffmpeg.extract(str(junk), workdir=str(tmp_path))
    assert e.value.code in ("not_video", "failed", "no_frames")

    def slow(*a, **kw):
        raise subprocess.TimeoutExpired(a[0], kw.get("timeout"))
    monkeypatch.setattr(ffmpeg.subprocess, "run", slow)
    with pytest.raises(ffmpeg.FFmpegError) as e:
        ffmpeg.extract(str(clips[next(iter(clips))]), workdir=str(tmp_path))
    assert e.value.code == "timeout"


def test_upload_folders_are_cleaned(tmp_path):
    home = str(tmp_path)
    old, new = ffmpeg.new_workdir(home), ffmpeg.new_workdir(home)
    (tmp_path / ".uploads" / "keep-me").mkdir()
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert ffmpeg.cleanup(home, older_than_s=3600) == 1 and not os.path.exists(old) and os.path.exists(new)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("x")
    try:
        os.symlink(outside, tmp_path / ".uploads" / "video-link")
    except (OSError, NotImplementedError):
        pass
    assert ffmpeg.cleanup(home) == 1  # startup: every work folder goes, links and other folders are left alone
    assert not os.path.exists(new) and (outside / "precious").exists() and (tmp_path / ".uploads" / "keep-me").exists()
    assert ffmpeg.cleanup(str(tmp_path / "nowhere")) == 0


# ------------------------------------------------------------------ the routes (called the way the router calls them)
class FakeProject:
    def __init__(self, root, design=None):
        self.root, self.design, self.ledger = str(root), design or Design().with_changes(EGG_ROBOT), []

    def latest(self):
        return "r001"

    def load_design(self, rev):
        return self.design

    def log(self, entry):
        self.ledger.append(entry)


class FakeStudio:
    def __init__(self, home):
        self.home, self.p, self.busy, self.remote, self.events, self.applied = str(home), FakeProject(home / "egg-picker"), threading.Lock(), False, [], []
        self.hub = types.SimpleNamespace(send=self.events.append)

    def apply(self, changes, label, user_text=None, reply=None, source=""):
        self.applied.append({"changes": changes, "label": label, "reply": reply, "source": source})


class FakeRFile:
    def __init__(self, data=b"", forbid=False):
        self.buf, self.forbid, self.read_bytes = io.BytesIO(data), forbid, 0

    def read(self, n=-1):
        if self.forbid:
            raise AssertionError("the body was read")
        out = self.buf.read(n)
        self.read_bytes += len(out)
        return out


def req(studio, body=None, method="POST", path="/api/video/analyze", query=None, headers=None, rfile=None, member=None,
        room=None):
    h = email.message.Message()
    for k, v in (headers or {}).items():
        h[k] = v
    handler = types.SimpleNamespace(headers=h, rfile=rfile or FakeRFile())
    return types.SimpleNamespace(method=method, path=path, query=query or {}, headers=h, body=body, studio=studio, room=room,
                                 member=member, handler=handler)


@pytest.fixture
def studio(tmp_path):
    return FakeStudio(tmp_path)


def test_routes_are_registered_per_contract():
    got = {(r["method"], r["path"]): r for r in api_video.ROUTES}
    assert set(got) == {("POST", "/api/video/analyze"), ("PUT", "/api/video/upload"), ("POST", "/api/video/apply"), ("GET", "/api/video/info")}
    assert got[("POST", "/api/video/analyze")]["max_body"] == 16 << 20
    assert got[("PUT", "/api/video/upload")]["raw_body"] is True and got[("PUT", "/api/video/upload")]["max_body"] == 500 << 20
    assert got[("GET", "/api/video/info")]["role"] == "viewer"
    assert all(got[k]["role"] == "editor" for k in got if k[0] != "GET")
    ids = [e["eventTypeId"] for e in api_video.KEEWANO_EVENTS]
    assert ids == sorted(set(ids)) and all(2160 <= i <= 2179 for i in ids)


def test_info_says_what_this_studio_can_do(studio, keys):
    st, out = api_video.info(req(studio, method="GET", path="/api/video/info"))
    assert st == 200 and out["claude"] is False and out["subq"] is False and "no Claude key" in out["manual_reason"]
    assert out["limits"]["frames"] == 12 and out["limits"]["upload_mb"] == 500 and "path" not in json.dumps(out)
    keys.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    assert api_video.info(req(studio, method="GET"))[1]["claude"] is True
    assert out["offline"] is False
    keys.setenv("MORPH_OFFLINE", "1")  # a key, but network lookups off: the page says that, not "no Claude key"
    off = api_video.info(req(studio, method="GET"))[1]
    assert off["claude"] is False and off["offline"] is True and off["manual_reason"].startswith("Network lookups are off")


def test_analyze_route(studio, claude):
    claude()
    st, out = api_video.analyze(req(studio, {"id": "abc12345", "frames": b64frames(6), "marks": {"grasp_s": 1, "release_s": 2.5}}))
    assert st == 200 and out["id"] == "abc12345" and out["result"]["usable"]
    assert out["mapping"]["changes"]["task.pick_x_mm"] == 230 and "slots" in out["mapping"]
    assert all(e.get("id") == "abc12345" and e["project"] == "egg-picker" for e in studio.events)
    assert {e["type"] for e in studio.events} == {"video", "agent"}
    entry = studio.p.ledger[-1]
    assert entry["type"] == "video_analysis" and entry["frames"] == 6 and entry["source"] == "claude"
    assert "egg" not in json.dumps(entry) and "jpeg" not in json.dumps(entry)  # counts and timings only
    assert api_video.keewano_events(entry)[0] == ("VideoAnalysis", "video")
    st, out = api_video.analyze(req(studio, {"frames": b64frames(1) * 13}))
    assert st == 400 and "at most 12" in out["error"]
    st, out = api_video.analyze(req(studio, {"frames": b64frames(2), "kind": "photo"}))
    assert st == 400 and "at most 1" in out["error"]


def test_analyze_route_without_a_key_sends_nothing(studio, keys):
    st, out = api_video.analyze(req(studio, {"frames": b64frames(3)}))
    assert st == 200 and out["result"] is None and out["mode"] == "manual" and out["mapping"]["changes"] == {}
    assert studio.p.ledger[-1]["kind"] == "manual"


def test_analyze_route_is_bounded(studio, claude):
    claude()
    held = [api_video._analyses.acquire(blocking=False) for _ in range(api_video.MAX_ANALYSES)]
    try:
        st, out = api_video.analyze(req(studio, {"frames": b64frames(2)}))
        assert st == 429
    finally:
        for h in held:
            if h:
                api_video._analyses.release()
    assert api_video.analyze(req(studio, {"frames": b64frames(2)}))[0] == 200


def test_upload_refuses_before_reading(studio, tmp_path, keys):
    big = req(studio, method="PUT", path="/api/video/upload", headers={"Content-Length": str(600 << 20)}, rfile=FakeRFile(forbid=True))
    st, out = api_video.upload(big)
    assert st == 413 and "500 MiB" in out["error"]
    for bad in ("12abc", "-1", "1, 2"):
        st, _ = api_video.upload(req(studio, method="PUT", headers={"Content-Length": bad}, rfile=FakeRFile(forbid=True)))
        assert st == 400
    keys.setattr(ffmpeg, "find", lambda: None)
    st, out = api_video.upload(req(studio, method="PUT", headers={"Content-Length": "1000"}, rfile=FakeRFile(forbid=True)))
    assert st == 501 and out["code"] == "no_ffmpeg" and "H.264" in out["error"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")
def test_upload_extracts_and_deletes(studio, clips, tmp_path):
    name = next(iter(clips))
    data = clips[name].read_bytes()
    uploads = tmp_path / ".uploads"
    r = req(studio, method="PUT", path="/api/video/upload", query={"id": ["upl12345"], "count": ["5"]},
            headers={"Content-Length": str(len(data))}, rfile=FakeRFile(data))
    st, out = api_video.upload(r)
    assert st == 200 and len(out["frames"]) == 5 and out["decoder"] == "ffmpeg"
    assert base64.b64decode(out["frames"][0]["jpeg_b64"])[:3] == b"\xff\xd8\xff"
    assert uploads.is_dir() and not list(uploads.iterdir())  # the clip and its frames are gone
    assert [e["stage"] for e in studio.events if e["type"] == "video"] == ["upload", "decode", "decode"]
    assert studio.p.ledger[-1]["type"] == "video_decode"
    # not a video: refused on its first bytes, and the folder still goes
    junk = b"#EXTM3U\n" + b"x" * 5000
    st, out = api_video.upload(req(studio, method="PUT", headers={"Content-Length": str(len(junk))}, rfile=FakeRFile(junk)))
    assert st == 415 and not list(uploads.iterdir())
    # cut off half way, and a clip ffmpeg can't decode: same
    st, _ = api_video.upload(req(studio, method="PUT", headers={"Content-Length": str(len(data) + 10)}, rfile=FakeRFile(data)))
    assert st == 400 and not list(uploads.iterdir())
    broken = b"\x00\x00\x00\x20ftypisom" + os.urandom(20000)
    st, out = api_video.upload(req(studio, method="PUT", headers={"Content-Length": str(len(broken))}, rfile=FakeRFile(broken)))
    assert st in (415, 422) and not list(uploads.iterdir())


def test_apply_route(studio):
    st, out = api_video.apply(req(studio, {"preview": True, "result": result(), "answers": {"object.mass_g": 70}}, path="/api/video/apply"))
    assert st == 200 and out["mapping"]["changes"]["task.pick_x_mm"] == 230
    assert api_video.apply(req(studio, {"preview": True, "result": "x"}))[0] == 400
    st, out = api_video.apply(req(studio, {"changes": {"arm.source": "so101", "name": "pwned"}}))
    assert st == 400 and not studio.applied
    st, out = api_video.apply(req(studio, {"changes": {"object.max_force_n": 25, "task.place_x_mm": 999, "arm.source": "x"},
                                           "label": "video: raw egg <b>"}))
    assert st == 202 and out["changes"] == {"object.max_force_n": 25.0, "task.place_x_mm": 400.0}
    for _ in range(50):
        if studio.applied:
            break
        time.sleep(0.01)
    a = studio.applied[0]
    assert a["changes"] == out["changes"] and a["source"] == "video" and a["label"] == "video: raw egg b"
    assert a["reply"].startswith("From the video:") and studio.p.ledger[-1] == {"type": "video_apply", "changes": 2}
    with studio.busy:
        st, out = api_video.apply(req(studio, {"changes": {"object.mass_g": 70}}))
    assert st == 409 and "Still regenerating" in out["error"]
    st, out = api_video.apply(types.SimpleNamespace(body={"changes": {"object.mass_g": 70}}, studio=None, room=None))
    assert st == 409 and "No project is open" in out["error"]


class FakeJob:
    def __init__(self, n):
        self.id = f"job{n}"

    def info(self, position=None):
        return {"id": self.id, "status": "queued", "position": position}


class FakeRoom:
    """Wave 0's Room, as far as the video routes use it: name, p, hub, jobs.snapshot(), completed(), latest()."""
    def __init__(self, project):
        self.name, self.p, self.events, self.asked = "egg-picker", project, [], []
        self.hub = types.SimpleNamespace(send=self.events.append)
        self.jobs = types.SimpleNamespace(snapshot=lambda: [{"id": "job0", "position": 0}, {"id": "job1", "position": 1}])

    def completed(self):
        return ["r001", "r002"]

    def latest(self):
        self.asked.append("latest")
        return "r002"


class QueueStudio:
    """Wave 0's Studio: queue_changes(room, changes, *, label, reply, source, user_text, base_rev, by, kind)."""
    def __init__(self, home):
        self.home, self.remote, self.queued, self.full = str(home), False, [], False

    def queue_changes(self, room, changes, *, label, reply=None, source="", user_text=None, base_rev=None, by=None,
                      kind="change"):
        if self.full:
            raise api_video.QueueFull("10 changes are already waiting for egg-picker.")
        self.queued.append({"room": room, "changes": changes, "label": label, "reply": reply, "source": source,
                            "base_rev": base_rev, "by": by})
        return FakeJob(len(self.queued))


def test_apply_queues_in_the_room_like_a_parameter_edit(tmp_path):
    proj = FakeProject(tmp_path / "egg-picker")
    room, st = FakeRoom(proj), QueueStudio(tmp_path)
    ana = {"id": "m1", "name": "Ana", "role": "editor", "color": "#c60"}
    code, out = api_video.apply(req(st, {"changes": {"object.max_force_n": 25}, "label": "video: raw egg", "base_rev": "r001"},
                                    path="/api/video/apply", member=ana, room=room))
    assert code == 202 and out["job"] == {"id": "job1", "position": 1} and out["changes"] == {"object.max_force_n": 25.0}
    q = st.queued[0]
    assert q["room"] is room and q["source"] == "video" and q["base_rev"] == "r001" and q["by"] == "m1"
    assert q["label"] == "video: raw egg" and q["reply"].startswith("From the video:")
    assert proj.ledger[-1] == {"type": "video_apply", "changes": 1, "by": "m1"}
    # a revision the room doesn't have is not passed on; a full queue answers 429, like /api/params
    api_video.apply(req(st, {"changes": {"object.mass_g": 70}, "base_rev": "r999"}, path="/api/video/apply", room=room))
    assert st.queued[-1]["base_rev"] is None and st.queued[-1]["by"] is None
    st.full = True
    code, out = api_video.apply(req(st, {"changes": {"object.mass_g": 70}}, path="/api/video/apply", room=room))
    assert code == 429 and "already waiting" in out["error"] and len(st.queued) == 2
    # the preview maps against the room's newest finished revision; events go to the room, with the member
    code, out = api_video.apply(req(st, {"preview": True, "result": result()}, path="/api/video/apply", room=room))
    assert code == 200 and out["mapping"]["items"] and room.asked == ["latest"]
    api_video.send(req(st, member=ana, room=room), {"type": "video", "id": "x"})
    assert room.events[-1]["by"] == "m1" and room.events[-1]["project"] == "egg-picker"  # the member id


class NoSuchProject:
    """The router's Request for ?project=nope: resolving the room raises (the router answers 404)."""
    def __init__(self, studio, body=None, length=0):
        self.studio, self.body, self.member, self.query, self.length = studio, body, None, {}, length
        h = email.message.Message()
        h["Content-Length"] = str(length)
        self.headers = h
        self.handler = types.SimpleNamespace(headers=h, rfile=FakeRFile(forbid=True))

    @property
    def room(self):
        raise LookupError("There is no robot project called nope.")


def test_an_unknown_project_is_refused_before_any_work(studio, keys):
    api_video.send(NoSuchProject(studio), {"type": "video"})  # events never raise
    for fn, body in ((api_video.analyze, {"frames": []}), (api_video.apply, {"changes": {"object.mass_g": 70}})):
        with pytest.raises(LookupError):
            fn(NoSuchProject(studio, body))
    with pytest.raises(LookupError):
        api_video.upload(NoSuchProject(studio, length=5000))  # before a byte of the body is read
    assert all(api_video._analyses.acquire(blocking=False) for _ in range(api_video.MAX_ANALYSES))
    for _ in range(api_video.MAX_ANALYSES):
        api_video._analyses.release()
    assert not studio.applied and not studio.events


def test_uploads_read_through_the_router_stream(studio, monkeypatch):
    """With Wave 0's Request the body comes from req.stream() (which marks it read), not from the socket."""
    monkeypatch.setattr(api_video.ffmpeg, "find", lambda: "/usr/bin/ffmpeg-not-run")
    seen = []

    def extract(path, **kw):
        with open(path, "rb") as fh:
            seen.append(fh.read())
        return {"frames": [{"t_s": 0.5, "jpeg": b"\xff\xd8x", "w": 4, "h": 3}], "duration_s": 1.0, "ms": 5}
    monkeypatch.setattr(api_video.ffmpeg, "extract", extract)
    clip = b"\x00\x00\x00\x20ftypisom" + b"v" * 3000
    r = req(studio, method="PUT", path="/api/video/upload", headers={"Content-Length": str(len(clip))},
            rfile=FakeRFile(forbid=True))
    r.length, r.stream = len(clip), lambda chunk: iter([clip[:1000], clip[1000:]])
    code, out = api_video.upload(r)
    assert code == 200 and seen == [clip] and out["frames"][0]["jpeg_b64"] == "/9h4"


def test_events_carry_the_member_when_teams_is_on(studio):
    api_video.send(req(studio, member={"id": "m1", "name": "Ana", "role": "editor"}), {"type": "video", "id": "x"})
    assert studio.events[-1]["by"] == "m1" and studio.events[-1]["project"] == "egg-picker"
    broken = FakeStudio(studio.home and __import__("pathlib").Path(studio.home))
    broken.hub = types.SimpleNamespace(send=lambda ev: (_ for _ in ()).throw(RuntimeError("closed")))
    api_video.send(req(broken), {"type": "video"})  # never raises


def test_one_copy_of_every_honesty_note_in_the_page():
    js = open(os.path.join(os.path.dirname(vision.__file__), "static", "video.js"), encoding="utf-8").read()
    for text in ("Morph picks up to 8 still frames from this video, here in your browser.",
                 "Estimated from a few frames. Sizes and positions are approximate",
                 "This browser can't play this video (often an iPhone HEVC .mov).",
                 "This Studio runs on another computer over an unencrypted (http) connection.",
                 "Second opinion (beta): also sends 4 frames to SubQ.", "Analyze with Claude", "Keep on this computer"):
        assert js.count(text) == 1, text
    assert not any(ord(c) > 0x2FFF for c in js)  # no emoji
