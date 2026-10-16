"""intent.rules() fixes the brief engine relies on: whole-word object aliases, unit parsing, questions are not edits."""

import pytest

from morph.spec import Design
from morph.studio.intent import normalize_units, object_alias, rules

EGG = {"name": "t", "object.name": "raw egg", "object.shape": "cylinder", "object.diameter_mm": 44.0, "object.length_mm": 57.0,
       "object.mass_g": 60.0, "object.friction": 0.35, "object.max_force_n": 25.0, "finger.sensor": "fsr402", "finger.width_mm": 20.0}
D = Design().with_changes(EGG)


@pytest.mark.parametrize("msg", ["a robot that sorts candy", "pick up candles", "grab my penguin plush", "move a grapefruit",
                                 "an eggplant picker", "I can't lift heavy things, help", "a robot that can pour a glass of water",
                                 "a robot that can pick up a pencil", "it can do it"])
def test_aliases_need_whole_words(msg):
    ch, _ = rules(msg, D)
    assert "object.name" not in ch, (msg, ch)
    assert object_alias(msg.lower()) is None


@pytest.mark.parametrize("msg,key", [("pick up a can", "soda can"), ("grab the can.", "soda can"), ("a can of beans", "soda can"),
                                     ("sort cans and bottles", "soda can"), ("an empty can", "soda can"), ("pick up eggs", "raw egg"),
                                     ("strawberries please", "strawberry"), ("ripe tomatoes", "tomato"), ("ping-pong balls", "ping pong ball"),
                                     ("light bulbs", "light bulb"), ("move the pens", "marker"), ("a water bottle", "water bottle")])
def test_aliases_still_match_objects(msg, key):
    assert rules(msg, D)[0]["object.name"] == key


def test_capability_question_is_not_an_edit():
    assert rules("can it lift 2 kg?", D) == ({}, [])
    assert rules("Does it have a camera?", D) == ({}, [])
    assert rules("can you add a wrist camera?", D)[0] == {"electronics.extras": "wrist_camera"}  # a polite request still edits


@pytest.mark.parametrize("text,norm", [("lift 2 kg", "lift 2000 g"), ("lift 1.5 lb", "lift 680.4 g"), ("8 oz", "226.8 g"),
                                       ("200 grams", "200 g"), ("reach 38 cm", "reach 380 mm"), ("0.5 m reach", "0.5 m reach"),
                                       ("half a metre", "half a metre"), ("1 meter", "1000 mm"), ("a 2 in wide block", "a 50.8 mm wide block"),
                                       ('a 3" cube', "a 76.2 mm cube"), ("2 inches", "50.8 mm"), ("put 3 in the bin", "put 3 in the bin"),
                                       ("12 mm", "12 mm"), ("2kg", "2000 g")])
def test_units_normalize_to_mm_and_g(text, norm):
    assert normalize_units(text) == norm


@pytest.mark.parametrize("text,norm", [
    # a thousands separator was read as two numbers ("1,050 g" as 50 g), and number words, "half a kilo", "a pound",
    # "300 gm" and a hyphen between a number and its unit were not read at all
    ("weighing 1,050 g", "weighing 1050 g"), ("1,080 grams", "1080 g"), ("1 000 g", "1000 g"), ("1.000 g", "1000 g"),
    ("2,000 mm", "2000 mm"), ("half a kilo", "500 g"), ("a pound", "453.6 g"), ("one pound", "453.6 g"), ("300 gm", "300 g"),
    ("300-gram", "300 g"), ("three hundred grams", "300 g"), ("twenty-five grams", "25 g"), ("two-inch tubes", "50.8 mm tubes"),
    ("2-inch tubes", "50.8 mm tubes"), ("50-mm tubes", "50 mm tubes"), ("50mm-wide tubes", "50mm wide tubes"),
    ("weighing .3 kg", "weighing 300 g"),
    # and what stays as it was: two numbers ("3 100 g bags"), a decimal ("1.500 g", "1.5 kg"), a list, a price
    ("3 100 g bags", "3 100 g bags"), ("2 500 g weights", "2 500 g weights"), ("1.500 g", "1.500 g"), ("1.5 kg", "1500 g"),
    ("sort 1,2,3 parts", "sort 1,2,3 parts"), ("$1,500", "$1,500"), ("half a metre", "half a metre")])
def test_units_read_thousands_words_and_hyphens(text, norm):
    assert normalize_units(text) == norm


def test_units_reach_mass_and_dimensions():
    assert rules("lift 2 kg", D)[0] == {"object.mass_g": 2000.0}
    assert rules("lift 1 lb", D)[0]["object.mass_g"] == pytest.approx(453.6)
    assert rules("reach 38 cm", D)[0] == {"arm.reach_mm": 380.0}
    assert rules("make the fingers 1 cm longer", D)[0]["finger.length_mm"] == D.finger.length_mm + 10
    ch, _ = rules("pick up a 10 cm box", D)
    assert ch["object.diameter_mm"] == 100.0 and ch["object.shape"] == "box" and ch["finger.pad"] == "flat"
    ch, _ = rules("a 6 cm ball", D)
    assert ch["object.diameter_mm"] == 60.0 and ch["object.shape"] == "sphere" and ch["finger.length_mm"] == 60.0
    ch, _ = rules("a robot that moves test tubes 16 mm wide, 30 g", D)
    assert ch["object.diameter_mm"] == 16.0 and ch["object.shape"] == "cylinder" and ch["object.mass_g"] == 30.0
    assert rules("use 12 mm mount spacing", D)[0] == {"finger.mount_hole_spacing_mm": 12.0}  # not an object width


def test_existing_rules_unchanged():
    d = Design()
    assert rules("Swap to Dynamixel servos", d)[0] == {"arm.actuator_family": "dynamixel"}
    assert rules("Increase reach to 380 mm", d)[0] == {"arm.reach_mm": 380.0}
    assert rules("Make it 6-DOF", d)[0] == {"arm.dof": 6}
    assert rules("grip at 5 N", D)[0] == {"electronics.target_force_n": 5.0}
    ch, _ = rules("make it work for a strawberry", D)
    assert ch["object.name"] == "strawberry" and ch["object.max_force_n"] == 3
