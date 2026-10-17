"""What a later message in the New robot conversation changes (the 0.2.0b1 known issues): a size that corrects the
objects', a size that names something else, a new arm asked for after "use my SO-101", people served food or drink,
and a mass per hour or a batch's beside the object's own. Offline: no keys, no network."""

import os

import pytest

from morph.studio import agents
from morph.studio import brief as B


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("ANTHROPIC", "KEEWANO", "SUBQ")):
            monkeypatch.delenv(k, raising=False)


def talk(*turns):
    """The conversation through agents.run_turn, to a robot: the defaults when it still asks, the nearest robot when
    it is out of scope (as the dialog's buttons would)."""
    b = None
    for t in turns:
        b = agents.run_turn(b, text=t, deep=False) if b else agents.run_turn(text=t, deep=False)
    for _ in range(3):
        if b.status not in ("asking", "out_of_scope"):
            break
        b = agents.run_turn(b, action="nearest" if b.status == "out_of_scope" else "defaults", deep=False)
    assert b.status == "ready", (turns, b.status)
    return b


def so101(b) -> bool:
    return b.changes.get("arm.source") == "so101"


def width(b) -> float:
    return float(b.changes["object.diameter_mm"])


def mass(b) -> float:
    return float(b.changes["object.mass_g"])


# ---- 1. a message that only corrects a size
@pytest.mark.parametrize("fix", ["sorry, they're 60 mm", "they are 60 mm", "actually 60 mm", "60 mm actually",
                                 "the cylinders are 60 mm", "each one is 60 mm", "they're actually 60 mm big",
                                 "make that 60 mm", "they measure 60 mm", "it's 60 mm", "they're 6 cm"])
def test_a_later_size_correction_is_read_and_the_so101_is_not_kept_for_what_its_jaws_dont_hold(fix):
    # the stock SO-101's jaws hold round objects up to about 46 mm: 60 mm cylinders get a Morph-generated arm, at 50 g
    b = agents.run_turn(text="use my SO-101 to move 30 mm cylinders weighing 50 g", deep=False)
    b = agents.run_turn(b, text=fix, deep=False)
    assert b.status == "out_of_scope" and any("46 mm" in n for n in b.scope["notes"]), (fix, b.scope)
    b = talk("use my SO-101 to move 30 mm cylinders weighing 50 g", fix)
    assert not so101(b) and (width(b), mass(b)) == (60.0, 50.0), fix


def test_a_correction_of_a_named_object_is_read_too():
    b = talk("use my SO-101 to pick up eggs", "the eggs are 60 mm")
    assert not so101(b) and width(b) == 60.0 and b.changes["object.name"] == "raw egg"
    b = talk("use my SO-101 to pick up eggs", "sorry, they're 60 mm")
    assert not so101(b) and width(b) == 60.0


def test_a_correction_down_names_the_objects_or_widens_nothing():
    # "the cylinders are 20 mm" is the objects: below the SO-101's 26 mm, and the gripper's, so the nearest robot
    b = talk("use my SO-101 to move 30 mm cylinders weighing 50 g", "the cylinders are 20 mm")
    assert not so101(b) and any("20 mm" in a["text"] for a in b.assumptions)
    # "they're 20 mm" may be a lid's or a gap's: the 30 mm stays, but the SO-101 isn't kept for 20 mm things
    b = talk("use my SO-101 to move 30 mm cylinders weighing 50 g", "sorry, they're 20 mm")
    assert not so101(b) and width(b) == 30.0
    b = talk("move 30 mm cylinders weighing 50 g", "they're 20 mm")
    assert width(b) == 30.0


# ---- 2. a size in a later message that names something else
@pytest.mark.parametrize("other", ["the lids are 35 mm across", "the caps, 35 mm", "35 mm caps", "the caps are 35 mm wide",
                                   "the labels are 35 mm wide", "the small ones are 35 mm across", "the lids? they're 35 mm",
                                   "the stickers, 35 mm"])
def test_a_size_that_names_something_else_never_narrows_the_objects(other):
    # 60 mm cylinders are wider than the SO-101's jaws hold: reading the lids' 35 mm as theirs kept the stock arm
    b = talk("use my SO-101 to move 60 mm cylinders weighing 50 g", other)
    assert not so101(b) and width(b) == 60.0, (other, width(b))
    b = talk("move 60 mm cylinders weighing 50 g", other)
    assert width(b) == 60.0, other   # a generated gripper sized for 35 mm would not open around them


@pytest.mark.parametrize("first, later, w", [
    ("use my SO-101 to move pill bottles", "the pills are 30 mm across", 35.0),
    ("use my SO-101 to move 40 mm candle jars weighing 80 g", "the candles are 30 mm across", 40.0),
    ("use my SO-101 to move 40 mm cylinders weighing 50 g", "the lids are 30 mm across", 40.0)])
def test_contents_and_parts_are_not_the_objects(first, later, w):
    b = talk(first, later)
    assert width(b) == w, (later, width(b))


def test_a_part_wider_than_the_object_is_what_the_jaws_open_around():
    # a lid wider than the jar is the widest thing the gripper must clear: the larger size stays, as before
    b = talk("move 40 mm jars weighing 80 g", "the lids are 50 mm across")
    assert width(b) == 50.0


@pytest.mark.parametrize("later", ["the small ones are 20 mm", "the small ones are 20 mm across", "some of them are 20 mm",
                                   "the small ones? they're 20 mm", "they sit on pegs. they're 20 mm"])
def test_a_smaller_size_that_may_be_the_objects_keeps_the_so101_off_and_the_gripper_wide(later):
    # some of the cylinders may be 20 mm, below the SO-101's 26 mm: a generated arm, still sized for the 30 mm ones
    b = talk("use my SO-101 to move 30 mm cylinders weighing 50 g", later)
    assert not so101(b) and width(b) == 30.0, (later, so101(b), width(b))
    assert any("messages also give 20 mm" in n for n in b.scope["notes"]), b.scope["notes"]


@pytest.mark.parametrize("later", ["the lids? they're 20 mm", "the table: it's 600 mm", "the fingers? it's 60 mm wide"])
def test_they_after_something_else_is_that_thing(later):
    # "the lids? they're 20 mm": the lids, so the 30 mm cylinders stay on the SO-101 and keep their width
    b = talk("use my SO-101 to move 30 mm cylinders weighing 50 g", later)
    assert so101(b) and width(b) == 30.0, (later, so101(b), width(b))


def test_the_so101_named_after_the_sizes_still_sees_the_smaller_one():
    # "they're 30 mm" may be the pegs: the 20 mm cubes it may leave are below the SO-101's 22.5 mm for flat sides
    b = talk("move 20 mm cubes", "sorry, they're 30 mm", "use my SO-101")
    assert not so101(b)
    b = talk("move 30 mm cylinders weighing 50 g", "the small ones are 20 mm", "use my SO-101")
    assert not so101(b) and width(b) == 30.0
    b = talk("move 20 mm cubes", "the cubes are 30 mm", "use my SO-101")   # the cubes themselves: the SO-101 holds them
    assert so101(b) and width(b) == 30.0


@pytest.mark.parametrize("text", ["use my SO-101 to move cylinders between 20 and 40 mm wide weighing 50 g",
                                  "use my SO-101 to move 40 mm and 20 mm cylinders weighing 50 g",
                                  "use my SO-101 to move 20 to 40 mm cylinders weighing 50 g"])
def test_a_ranges_lower_end_is_checked_against_the_so101s_jaws(text):
    # the stock SO-101 drops round objects under 26 mm: the 20 mm ones get a generated arm, sized for the 40 mm ones
    b = talk(text)
    assert not so101(b) and width(b) == 40.0, text
    assert so101(talk("use my SO-101 to move 30 to 40 mm cylinders weighing 50 g"))   # both ends in its jaws


def test_the_object_named_again_keeps_what_the_person_said_about_it():
    # "the eggs are fragile" named the eggs again: Morph's 44 mm, 60 g preset replaced the 150 g or 60 mm the person gave
    b = talk("use my SO-101 to pick up eggs weighing 150 g", "the eggs are fragile")
    assert not so101(b) and mass(b) == 150.0
    b = talk("use my SO-101 to pick up 60 mm eggs", "eggs, please")
    assert not so101(b) and width(b) == 60.0


def test_a_guard_keeps_what_the_person_stated():
    # the object keeps its stated width, mass and shape, so the flat-sided minimum and the unknown-shape checks still run
    b = agents.run_turn(text="use my SO-101 to move 30 mm cubes weighing 50 g", deep=False)
    b = agents.run_turn(b, text="the lids are 20 mm across", deep=False)
    o = b.get("object")
    assert {"diameter_mm", "mass_g", "shape"} <= set(o["stated"]) and o["diameter_mm"] == 30.0 and o["least_mm"] == 20.0
    assert b.status == "out_of_scope" and any("22.5 mm" in n for n in b.scope["notes"]), b.scope


@pytest.mark.parametrize("first, later, m", [
    ("use my SO-101 to move 30 mm cylinders weighing 50 g", "the lids are 20 g each", 50.0),
    ("move 30 mm cylinders weighing 150 g", "the lids are 20 g each", 150.0),
    ("move 40 mm cups weighing 150 g", "each holds 60 g of water", 150.0)])
def test_a_parts_mass_never_lightens_the_object(first, later, m):
    assert mass(talk(first, later)) == m


def test_a_later_mass_of_the_objects_still_replaces_theirs():
    assert mass(talk("move 30 mm cylinders weighing 150 g", "sorry, they weigh 60 g")) == 60.0
    assert mass(talk("move 30 mm cylinders weighing 50 g", "they're 150 g")) == 150.0


# ---- 3. a new arm asked for after "use my SO-101"
@pytest.mark.parametrize("later", ["actually, build a new arm", "build a new one instead", "design a new arm", "I want a custom arm",
                                   "a Morph-generated arm please", "build it from scratch"])
def test_a_new_arm_asked_for_later_drops_the_so101(later):
    b = talk("use my SO-101 to pick up eggs", later)
    assert not so101(b) and B.value(b, "existing_hardware") == "none" and b.archetype["key"] == "fragile_food", later


@pytest.mark.parametrize("later", ["no need for a new arm", "do I need a new arm?", "a new arm would cost too much",
                                   "don't build a new arm", "pick up another one", "the new one is 40 mm"])
def test_wording_that_asks_for_no_new_arm_keeps_the_so101(later):
    assert so101(talk("use my SO-101 to pick up eggs", later)), later


@pytest.mark.parametrize("first, people", [("use my SO-101 to move cups to the sink", "yes"),
                                           ("use my SO-101 to tidy up the playroom", "children")])
def test_a_new_arm_keeps_the_people_the_request_puts_next_to_it(first, people):
    b = talk(first, "actually, build a new arm")
    assert not so101(b) and B.value(b, "people_nearby") == people and "estop" in b.changes["electronics.extras"]
    b = talk(first, "I don't have an SO-101")
    assert not so101(b) and B.value(b, "people_nearby") == people and "estop" in b.changes["electronics.extras"]


def test_the_so101_keeps_the_people_and_the_place_of_the_job_it_does():
    # the SO-101 brief took no one nearby and a home desk whatever the job: a correction that rightly kept the SO-101
    # then dropped the emergency stop the same job had on a generated arm
    b = talk("use my SO-101 to tidy up the playroom")
    assert so101(b) and B.value(b, "people_nearby") == "children" and "estop" in b.changes["electronics.extras"]
    b = talk("use my SO-101 to sort 24 mm, 80 g blocks into bins", "the blocks are 34 mm")
    assert so101(b) and width(b) == 34.0 and mass(b) == 80.0 and "estop" in b.changes["electronics.extras"]


# ---- 4. people served food or drink
@pytest.mark.parametrize("text", ["a robot that delivers them coffee", "a robot that carries them their lunch",
                                  "a robot that makes them tea", "a robot that delivers them sandwiches",
                                  "a robot that pours them a beer", "a robot that delivers 'em a cup of tea"])
def test_them_served_food_or_drink_are_people_next_to_it(text):
    b = talk(text)
    assert b.flags.get("person") and B.value(b, "people_nearby") == "yes" and "estop" in b.changes["electronics.extras"], text


@pytest.mark.parametrize("text", ["a robot that delivers them to the bin", "a robot that takes them to the coffee machine",
                                  "sort cans and carry them to the recycling"])
def test_them_moved_somewhere_are_things(text):
    assert not B.parse(text)["person"], text


# ---- 5. a mass per hour or a batch's beside the object's own
@pytest.mark.parametrize("text", ["move 50 g cubes at 2 kg per hour", "move 50 g cubes, about 2 kg an hour",
                                  "sort 50 g blocks in 2 kg batches", "move 2 kg of 50 g cubes", "pick up cubes weighing 50 g, 2 kg/h",
                                  "move 50 g cubes, 2 kg of them a day"])
def test_a_rate_or_a_batch_is_not_the_objects_mass(text):
    b = talk(text)
    assert mass(b) == 50.0 and b.scope["status"] != "out_of_scope", (text, mass(b))


def test_a_rate_in_a_later_message_is_not_the_objects_mass():
    assert mass(talk("move 40 mm boxes weighing 50 g", "2 kg per hour")) == 50.0
    assert mass(talk("move 40 mm boxes weighing 50 g", "about 3 kg an hour, people nearby")) == 50.0


@pytest.mark.parametrize("text, m", [
    # a total is often one object's full weight, and "each" may mark a part's: these keep the larger mass
    ("pick up bottles, 60 g empty, 350 g in total when full", 350.0),
    ("pick up cups: each cup is 40 g and holds 90 g of water, 130 g in total", 130.0),
    ("pick up bottles, 60 g empty, 2 kg per hour", 500.0),               # the full weight isn't given: the rate stays
    ("pick up cups that weigh 40 g and hold 90 g of water, 3 kg per hour", 500.0),
    ("move cubes with 20 g lids at 2 kg per hour", 500.0),
    ("move cubes at 2 kg per hour", 500.0)])                             # no mass of their own: read as before
def test_a_total_an_empty_weight_or_a_part_keeps_the_larger_mass(text, m):
    # 500 g is the nearest robot Morph offers for a mass it can't lift
    assert mass(talk(text)) == m, text
