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
    b = talk("move 20 mm cubes", "the cubes are 30 mm wide", "use my SO-101")   # the cubes themselves: the SO-101 holds them
    assert so101(b) and width(b) == 30.0
    b = talk("move 20 mm cubes", "the cubes are 30 mm", "use my SO-101")   # a phrasing never read before only widens them
    assert not so101(b) and width(b) == 30.0


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


@pytest.mark.parametrize("later", ["the cans are 30 mm at the neck", "the cans have 45 mm necks", "the neck of each can is 30 mm",
                                   "the holes in the cans are 47 mm", "the caps on the cans are 60 mm",
                                   "the base of each can is 50 mm", "the pills in the cans are 47 mm", "each can is 20 mm"])
def test_the_object_named_again_is_never_smaller_than_before(later):
    # naming the cans again kept the 30 mm, 60 g the person gave, and so the stock SO-101, where Morph's 66 mm, 370 g
    # can had kept it off: a neck, a hole or a cap as wide as the cans, or 20 mm ones, may not fit its jaws
    b = talk("I have an SO-101. it should pick up 30 mm cans of 60 g", later)
    assert not so101(b), (later, width(b), mass(b))


@pytest.mark.parametrize("first, later", [
    # the round-1 review of this change: a part, some of the objects, a question, a lower bound, a part's location or
    # "an inch and a half" was taken as the objects' own size and kept the stock SO-101 for 60 mm things
    ("use my SO-101 to move 60 mm jars weighing 50 g", "the lids of the jars are 30 mm"),
    ("use my SO-101 to move 60 mm jars weighing 50 g", "the lid on each jar is 30 mm"),
    ("use my SO-101 to move 60 mm jars weighing 50 g", "the candles in the jars are 30 mm"),
    ("use my SO-101 to move 60 mm jars weighing 50 g", "the gap between the jars is 30 mm"),
    ("use my SO-101 to move 60 mm, 50 g bottles", "the neck of each bottle is 40 mm"),
    ("use my SO-101 to pick up 60 mm balls weighing 50 g", "some of the balls are 40 mm"),
    ("use my SO-101 to pick up 60 mm balls weighing 50 g", "half the balls are 40 mm"),
    ("use my SO-101 to pick up 60 mm balls weighing 50 g", "not all the balls are 40 mm"),
    ("use my SO-101 to pick up 60 mm balls weighing 50 g", "what if the balls are 40 mm?"),
    ("use my SO-101 to pick up 60 mm balls weighing 50 g", "suppose the balls are 40 mm"),
    ("use my SO-101 to pick up 60 mm balls weighing 50 g", "the balls are 40 mm and up"),
    ("use my SO-101 to pick up 60 mm balls weighing 50 g", "the balls are at least 40 mm wide"),
    ("use my SO-101 to pick up 60 mm balls weighing 50 g", "the balls are 40 mm, some are 60 mm"),
    ("use my SO-101 to pick up 60 mm balls weighing 50 g", "the balls are 40 mm but some are bigger"),
    ("use my SO-101 to move 60 mm jars weighing 50 g", "the jars are 30 mm wide at the top"),
    ("use my SO-101 to move 60 mm jars weighing 50 g", "the jars measure 30 mm, at the neck")])
def test_a_size_that_may_not_be_all_the_objects_own_never_narrows_them(first, later):
    b = talk(first, later)
    assert not so101(b) and width(b) >= 60.0, (later, so101(b), width(b))


def test_a_size_said_after_some_of_them_keeps_their_range():
    b = talk("use my SO-101 to pick up 60 mm balls weighing 50 g", "the small ones are 20 mm", "the balls are 40 mm")
    assert not so101(b) and width(b) == 60.0
    b = talk("use my SO-101 to sort 40 mm blocks into bins", "the blocks are an inch and a half")   # 38 mm, never 25.4
    assert not so101(b) and width(b) == 40.0
    b = talk("use my SO-101 to pick up 60 mm balls weighing 50 g", "between 15 and 35 mm")   # the 15 mm ones too
    assert not so101(b)


@pytest.mark.parametrize("first, later, m", [
    ("use my SO-101 to move 30 mm jars weighing 150 g", "the lids of the jars are 50 g", 150.0),
    ("use my SO-101 to move 30 mm jars weighing 150 g", "some of the jars are 50 g", 150.0),
    ("use my SO-101 to move 30 mm jars weighing 150 g", "what if they weigh 50 g?", 150.0)])
def test_a_mass_that_may_not_be_the_objects_own_never_lightens_them(first, later, m):
    b = talk(first, later)
    assert not so101(b) and mass(b) >= m, (later, mass(b))


@pytest.mark.parametrize("later", ["keep my SO-101", "I have an SO-101", "use my SO-101 after all"])
def test_the_so101_named_after_a_generated_arm_was_accepted_never_holds_the_stand_in(later):
    # 15 mm beads are below the SO-101's 26 mm: the nearest robot is built around a 30 mm stand-in, and naming the
    # SO-101 again put the stock arm on that stand-in
    b = agents.run_turn(text="use my SO-101 to pick up 15 mm beads", deep=False)
    b = agents.run_turn(b, action="nearest", deep=False)
    assert b.status == "ready" and not so101(b)
    b = agents.run_turn(b, text=later, deep=False)
    b = talk_on(b)
    assert not so101(b), later


def talk_on(b):
    for _ in range(3):
        if b.status not in ("asking", "out_of_scope"):
            break
        b = agents.run_turn(b, action="nearest" if b.status == "out_of_scope" else "defaults", deep=False)
    return b


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
    b = talk("use my SO-101 to sort 24 mm, 80 g blocks into bins", "the blocks are 34 mm wide")
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
                                  "sort 50 g blocks in 2 kg batches", "sort 2 kg of 50 g cubes", "pick up cubes weighing 50 g, 2 kg/h",
                                  "move 50 g cubes, 2 kg of them a day", "move 50 g cubes at 500 g per min",
                                  "sort 50 g cookies in 2 kg batches", "sort 2 kg of 50 g cubes per shift"])
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


# ---- the round-1 review of this change: masses, people, the SO-101's job
@pytest.mark.parametrize("turns", [
    # a rate beside a part's, contents', some of them' or a lower bound's mass is not dropped: that mass isn't the object's
    ("use my SO-101 to move 30 mm pill bottles at 2 kg per hour; the pills weigh 1 g each",),
    ("use my SO-101 to move 30 mm candle jars at 2 kg per hour; the candles are 60 g",),
    ("use my SO-101 to move 30 mm bottles at 2 kg per hour; the pumps are 20 g each",),
    ("use my SO-101 to move 30 mm jars at 2 kg per hour, at least 20 g each",),
    ("use my SO-101 to move 30 mm jars at 2 kg per hour, the glass is 20 g",),
    ("use my SO-101 to move 30 mm jars at 2 kg per hour; the jars themselves are 80 g, the jam 100 g",),
    ("use my SO-101 to move 30 mm jars at 2 kg per hour, 20 g net",),
    ("use my SO-101 to move 30 mm jars", "2 kg per hour; more than 20 g"),
    ("use my SO-101 to move 30 mm bottles", "about 2 kg a day; the sleeves are 5 g"),
    ("use my SO-101 to move 30 mm seed trays", "300 g per tray, the pots are 20 g"),
    # "lots of", "trays of" and "every minute" beside an object's own mass are no batch and no rate
    ("use my SO-101 to sort lots of 300 g 30 mm jars and some 50 g cubes",),
    ("use my SO-101 to move 30 mm pouches of 300 g every minute and 40 g sachets",),
    # sizes said of two kinds of thing: the 25 g may not be the objects'
    ("I have an SO-101. it should pack 30 mm tubes at 2 kg per hour; the bulbs are 25 g",),
    # a first message's part mass, then the object named again
    ("use my SO-101 to hand me pliers; the grips are 10 g", "the pliers are heavy"),
    # "they" before any object is described: the stand-in's size is not narrowed
    ("use my SO-101 to pour them water", "they're 40 mm"),
    ("use my SO-101 to pour me a beer", "it's 40 mm")])
def test_a_mass_or_size_that_may_not_be_the_objects_keeps_the_so101_off(turns):
    b = talk(*turns)
    assert not so101(b), (turns, width(b), mass(b))


@pytest.mark.parametrize("text, m", [("a robot that sorts a lot of 300 g jars and 50 g tins", 300.0),
                                     ("a robot that unloads trays of 300 g mugs and 150 g bowls", 300.0),
                                     ("a robot that moves sacks weighing 400 g, the beans are 1 g each", 400.0)])
def test_many_of_them_is_no_batch_total(text, m):
    assert mass(talk(text)) == m, text


def test_a_shake_is_no_drink():
    assert not B.parse("give them a shake")["person"]


# ---- the round-2 review: a rate beside a mass the objects may weigh more than, what the arm lifts at once, things in
# containers, a part's mass after its name, and the people of a job left for a new arm after the nearest robot
@pytest.mark.parametrize("turns", [
    ("use my SO-101 to pick up 30 mm, 50 g cubes at 2 kg per hour. A few are heavier.",),
    ("use my SO-101 to pick up 30 mm, 50 g cubes at 2 kg per hour. Others weigh more.",),
    ("use my SO-101 to pick up 30 mm 50 g cubes and up, 2 kg per hour",),
    ("use my SO-101 to pick up 30 mm 50 g cubes on average at 2 kg per hour",),
    ("use my SO-101 to pick up 30 mm 50 g cubes or so at 2 kg per hour",),
    ("use my SO-101 to pick up 30 mm 50 g bottles of 500 ml at 2 kg per hour",),
    ("use my SO-101 to pick up 30 mm 50 g jars of honey at 2 kg per hour",),
    ("use my SO-101 to pick up 30 mm 50 g cubes (dry) at 2 kg per hour",),
    ("use my SO-101 to pick up 30 mm 50 g cubes at 2 kg per hour. Some are 3 times that.",),
    ("use my SO-101 to pick up 30 mm 50 g cubes at 2 kg per hour. That's on the light side.",),
    ("use my SO-101 to move 40 mm 50 g cylinders", "we put 150 g of these beans in each"),
    ("use my SO-101 to move 40 mm cylinders weighing 50 g", "some are heavier. about 2 kg an hour"),
    ("use my SO-101 to pick up 30 mm 50 g cylinders", "2 kg an hour. before filling, that is"),
    # what it lifts at once, and things in containers
    ("use my SO-101 to pick up 30 mm 50 g cubes, 2 kg of them at a time",),
    ("use my SO-101 to pick up 30 mm 50 g cubes, it lifts them in 2 kg batches",),
    ("use my SO-101 to pick up 30 mm 50 g components in 2 kg batches",),
    ("use my SO-101 to pick up bags of 30 mm balls of 50 g about five pounds an hour",),
    ("use my SO-101 to pick up cartons of 50 g eggs at 2 kg per hour",),
    ("use my SO-101 to pick up punnets of 20 g strawberries at 2 kg per hour",),
    # a correction of the only mass so far, and a part's noun that parse() would have named the objects with
    ("use my SO-101 to pick up 30 mm pouches of 50 g", "sorry, 300 g every minute"),
    ("use my SO-101 to move 40 mm cylinders weighing 150 g", "they come in 30 mm wide tubes",
     "the tubes are 20 g, 2 kg per hour")])
def test_a_rate_beside_a_mass_they_may_weigh_more_than_stays_as_read(turns):
    b = talk(*turns)
    assert not so101(b) and mass(b) >= 150.0, (turns, mass(b))


@pytest.mark.parametrize("later", ["that's the empty weight", "they hold 300 ml of water", "they're full of soup",
                                   "when full they weigh more", "they're heavier with lids", "some are heavier"])
def test_a_rate_left_out_comes_back_when_a_later_message_says_they_weigh_more(later):
    first = "use my SO-101 to pick up 30 mm cups weighing 50 g at 2 kg per hour"
    assert so101(talk(first)) and mass(talk(first)) == 50.0
    b = talk(first, later)
    assert not so101(b) and mass(b) == 500.0, later


@pytest.mark.parametrize("later", ["the lids: 20 g", "lids, 20 g", "20 g each for the lids", "the lids? 20 g"])
def test_a_parts_mass_after_its_name_never_lightens_the_object(later):
    assert mass(talk("use my SO-101 to pick up 30 mm jars weighing 150 g", later)) == 150.0
    assert mass(talk("use my SO-101 to pick up 30 mm jars weighing 150 g", "sorry, 60 g")) == 60.0


@pytest.mark.parametrize("leave", [{"answers": {"existing_hardware": "none"}}, {"text": "actually, build a new arm"},
                                   {"text": "I don't have an SO-101"}])
def test_leaving_the_so101_after_the_nearest_robot_keeps_the_jobs_people(leave):
    b = agents.run_turn(text="a robot that recycles 80 mm espresso cups", deep=False)
    b = agents.run_turn(b, action="nearest", deep=False)
    b = agents.run_turn(b, text="use my SO-101", deep=False)
    b = talk_on(agents.run_turn(b, deep=False, **leave))
    assert B.value(b, "people_nearby") != "no" and "estop" in (b.changes.get("electronics.extras") or ""), leave


@pytest.mark.parametrize("text", ["a robot that makes them food-safe", "a robot that makes them water-tight",
                                  "a robot that hands them coffee filters to the machine"])
def test_a_word_made_with_food_is_no_food_served(text):
    assert not B.parse(text)["person"]


@pytest.mark.parametrize("turns", [
    ("use my SO-101 to move 40 mm bottles weighing 50 g", "they're 40 mm at the neck"),   # their widest part isn't given
    ("use my SO-101 to move 30 mm cylinders", "these are 15 mm"),
    ("use my SO-101 to move 30 mm cylinders", "they're exactly 15 mm"),
    ("use my SO-101 to move 30 mm cylinders", "they all measure 15 mm"),
    ("use my SO-101 to move 50 mm cylinders weighing 50 g", "30 mm and up")])
def test_a_size_the_so101_may_not_hold_keeps_it_off(turns):
    b = talk(*turns)
    assert not so101(b), (turns, width(b))


def test_a_correction_after_a_size_of_some_of_them_only_widens():
    # "sorry, 40 mm" may correct the 20 mm some of them are, not the 60 mm the rest are
    b = agents.run_turn(text="use my SO-101 to move 60 mm cylinders weighing 50 g", deep=False)
    for t in ("some of them are 20 mm wide", "@nearest", "sorry, 40 mm", "keep my SO-101"):
        b = agents.run_turn(b, action=t[1:], deep=False) if t.startswith("@") else agents.run_turn(b, text=t, deep=False)
    b = talk_on(b)
    assert not so101(b) and width(b) == 60.0


# ---- the round-3 review: a rate is left out only when the whole conversation is plain, since no list of the words
# that say an object may weigh more, or that the arm lifts more at once, is ever complete
_FILLER = "people nearby. " * 140   # plain, and long enough to push the first message out of the last 2,000 characters


@pytest.mark.parametrize("turns, m", [
    (("use my SO-101 to pick up 30 mm 50 g jars 2 kg every hour, but some are quite heavy",), 150.0),
    (("use my SO-101 to move 30 mm jars weighing 50 g at 2 kg per hour, some weigh 3x that",), 150.0),
    (("use my SO-101 to sort 30 mm 1.8 oz pouches about five pounds an hour. Some are bigger.",), 150.0),
    (("use my SO-101 to pick up 30 mm 50 g cubes at 2 kg per hour, or a little over",), 150.0),
    (("use my SO-101 to pick up 30 mm 50 g cubes at 2 kg per hour, some are heavyer",), 150.0),
    # a second number or a symbol beside the own mass
    (("Use my SO-101 to pick up 30 mm cubes weighing 90 g, up to 120, at about 2 kg per hour.",), 150.0),
    (("use my SO-101 to pick up 30 mm 120/90 g cubes at 2 kg per hour",), 150.0),
    (("use my SO-101 to pick up 30 mm cubes weighing ≥50 g at 2 kg per hour",), 150.0),
    # lifted at once, or a batch the arm handles
    (("use my SO-101 to pick up 30 mm jars weighing 50 g; it carries 2 kg of them",), 150.0),
    (("use my SO-101 to pick up 30 mm 50 g cubes two kilos a day, 40 cubes a lift",), 150.0),
    (("Use my SO-101 to lift 2 kg of 50 g cubes onto the scale.",), 150.0),
    (("use my SO-101 to pick up 2 kg of 50 g cubes",), 150.0),
    (("use my SO-101 to sort 50 g cubes in 2 kg batches, it picks them up",), 150.0),
    (("use my SO-101 to unload 30 mm fifty gram jars in 2 kg batches",), 150.0),
    (("use my SO-101 to sort 30 mm 50 g cubes in 2 kg batches, then carry the batches to the shelf",), 150.0),
    # contents and exclusions
    (("use my SO-101 to sort 30 mm 50 g cubes 2 kg of them a day, with jam in them",), 150.0),
    (("Use my SO-101 to pick up 30 mm 50 g cups at 2 kg per hour and pour lemonade into them.",), 150.0),
    (("use my SO-101 to pick up 30 mm jars, each jar is 50 g, 2 kg of them, excl. jam",), 150.0),
    # a correction of the only mass so far
    (("use my SO-101 to pick up 30 mm fifty gram jars", "oh wait, 300 grams every minute"), 300.0),
    (("use my SO-101 to pick up 30 mm jars, each jar is 50 g", "300 grams every minute, my bad"), 300.0),
    # a later message that isn't plain brings the rate back
    (("use my SO-101 to pick up 30 mm jars, each jar is 50 g", "around 2 kg/h", "they're bigger than I said"), 150.0),
    (("use my SO-101 to pick up 30 mm balls weighing 50 g", "in 2 kg batches", "some are 3x that"), 150.0),
    (("use my SO-101 to pick up 30 mm jars weighing 50 g", "2 kg of them", "it lifts the crate"), 150.0),
    # an early message beyond the last 2,000 characters still counts
    (("use my SO-101 to pick up 30 mm 50 g pouches; that's their empty weight", _FILLER,
      "the pouches are 50 g, 2 kg an hour"), 150.0)])
def test_a_rate_is_left_out_only_in_a_plain_conversation(turns, m):
    b = talk(*turns)
    assert not so101(b) and mass(b) >= m, (turns, mass(b))


# ---- the round-3 review: a new shape in a later message, an emergency stop kept when leaving the SO-101 in any order,
# and sizes said before any object is described
def steps(*turns):
    """talk() with the dialog's buttons ("@nearest", "@defaults") and chip answers ("#slot=value;slot=value")."""
    b = None
    for t in turns:
        if t.startswith("@"):
            b = agents.run_turn(b, action=t[1:], deep=False)
        elif t.startswith("#"):
            b = agents.run_turn(b, answers=dict(kv.split("=", 1) for kv in t[1:].split(";")), deep=False)
        else:
            b = agents.run_turn(b, text=t, deep=False) if b else agents.run_turn(text=t, deep=False)
    return talk_on(b)


@pytest.mark.parametrize("turns", [
    ("use my SO-101 to move 30 mm cylinders", "sorry, they come as 40 mm cubes"),        # flat-sided: over 34 mm
    ("use my SO-101 to move 30 mm cylinders", "we'll go with 40 mm cubes instead"),
    ("use my SO-101 to move 30 mm cylinders weighing 50 g", "sorry, I'm working with 40 mm cubes"),
    ("a robot to move 30 mm cylinders", "sorry, they come as 40 mm cubes", "use my SO-101"),
    ("use my SO-101 to move 30 mm cubes", "sorry, they come as 24 mm cylinders"),        # round: under 26 mm
    ("use my SO-101 to move 40 mm cylinders", "not all of them are 30 mm, some are 20 mm"),
    ("use my SO-101 to stack 30 mm bottles", "45 mm necks", "@nearest", "keep my SO-101"),
    ("use my SO-101 to pour me a beer", "they're 50 mm")])
def test_a_later_shape_or_size_the_so101_may_not_hold_keeps_it_off(turns):
    b = steps(*turns)
    assert not so101(b), (turns, width(b), b.changes.get("object.shape"))


@pytest.mark.parametrize("turns", [
    ("use my SO-101 to sort screws", "I don't have an SO-101", "#object=espresso_cup"),
    ("use my SO-101 to sort screws", "I don't have an SO-101", "toy blocks"),
    ("use my SO-101 to sort screws onto a shelf", "#existing_hardware=none", "#object=screwdriver"),
    ("use my SO-101 to stack jars on a shelf", "actually, build a new arm", "#object=espresso_cup"),
    ("I have an SO-101. it should sort screws onto a shelf", "@nearest", "#existing_hardware=none"),   # a kitchen's stop
    ("use my SO-101 to sort things onto the shelf", "#object=strawberry", "@nearest", "#existing_hardware=none"),
    ("use my SO-101 to tidy up the playroom shelves", "#object=glass_vial", "@nearest", "#people_nearby=no",
     "#existing_hardware=none"),                                                                        # a lab's stop
    ("use my SO-101 to load the dishwasher", "they're 60 mm", "#existing_hardware=none")])
def test_leaving_the_so101_in_any_order_keeps_an_emergency_stop(turns):
    b = steps(*turns)
    assert "estop" in (b.changes.get("electronics.extras") or "") and not so101(b), (turns, b.archetype)


def test_a_size_before_any_object_never_makes_the_object_lighter_or_narrower():
    # "they're 60 mm" before any object is described is read as before (not at all): leaving the SO-101 for the
    # dishes gives their own object, never below the general arm's 45 mm, 100 g, and the nearest robot's stand-in still
    # decides when the request is out of scope (a 330 ml bottle for a beer)
    b = steps("use my SO-101 to load the dishwasher", "they're 60 mm", "#existing_hardware=none")
    assert width(b) >= 60.0 and mass(b) >= 100.0
    b = steps("use my SO-101 to pour me a beer", "it's 40 mm")
    assert width(b) >= 60.0 and mass(b) >= 360.0 and not so101(b)


# ---- the full comparison after the third review: a preset's other words, corrections built from plain words, the
# objects' own noun as what holds them, and Morph's assumed object once the SO-101 is left for a job
@pytest.mark.parametrize("turns, m", [
    (("use my SO-101 to pick up 30 mm 50 g vials at 2 kg per hour with samples",), 150.0),
    (("use my SO-101 to pick up 30 mm 50 g pill bottles at 2 kg per hour",), 150.0),
    (("use my SO-101 to pick up 30 mm boxes, each box is 50 g, 2 kg of them a day, in their box",), 150.0),
    (("use my SO-101 to pick up 30 mm fifty gram jars", "should be 300 g every minute"), 300.0),
    (("use my SO-101 to pick up 30 mm fifty gram jars", "it's 300 g every minute"), 300.0)])
def test_a_rate_beside_plain_looking_words_that_say_more_stays_as_read(turns, m):
    b = talk(*turns)
    assert not so101(b) and mass(b) >= m, (turns, mass(b))


@pytest.mark.parametrize("turns", [
    ("use my SO-101 to tidy up the playroom", "#existing_hardware=none"),
    ("use my SO-101 to tidy up the playroom", "actually, build a new arm"),
    ("use my SO-101 to put the kids' toys on the top shelf", "I don't have an SO-101"),
    ("use my SO-101 to pass me tools from the top shelf", "my SO-101 is broken")])
def test_the_object_assumed_for_a_job_left_for_the_so101_is_never_smaller_than_before(turns):
    # leaving the SO-101 gave the general arm's 45 mm, 100 g object; the job now decides who is around, but its own
    # object (a 30 g toy block, a screwdriver) is never planned narrower or lighter than that
    b = steps(*turns)
    assert width(b) >= 45.0 and mass(b) >= 100.0 and "estop" in (b.changes.get("electronics.extras") or ""), turns


# ---- the round-4 review: what is said after a size before any object, a holder's noun, every way of leaving the
# SO-101, and the rate rule's remaining gaps
@pytest.mark.parametrize("turns", [
    ("use my SO-101", "they're 60 mm", "they weigh 600 g"),
    ("use my SO-101", "they're 60 mm", "sorry, 90 mm"),
    ("use my SO-101 to stack things", "they're 30 mm", "300 g"),
    ("use my SO-101 to move toy blocks", "they sit in 40 mm holders"),      # 40 mm flat-sided blocks: over 34 mm
    ("use my SO-101 to move small boxes", "they come in 45 mm sleeves")])
def test_a_size_said_later_still_keeps_the_so101_off_for_what_it_cannot_hold(turns):
    b = steps(*turns)
    assert not so101(b), (turns, width(b), mass(b))


@pytest.mark.parametrize("turns", [
    ("use my SO-101 to tidy up the playroom", "#object=small_box", "@nearest", "#object=espresso_cup", "#people_nearby=no"),
    ("use my SO-101 to cook breakfast", "@nearest", "actually, build a new arm", "#object=pliers", "#environment=workshop"),
    ("use my SO-101 to put the toys on the shelf", "@nearest", "#existing_hardware=none", "#object=toy_block",
     "nobody is around"),
    ("use my SO-101 to pick up 30 mm 60 g screwdrivers at 2 kg per hour, nobody nearby",)])
def test_every_way_of_leaving_the_so101_keeps_the_emergency_stop(turns):
    b = steps(*turns)
    assert "estop" in (b.changes.get("electronics.extras") or ""), (turns, b.archetype)


@pytest.mark.parametrize("turns, m", [
    (("use my SO-101 to pick up 1 inch 50 g cubes in 2 kg batches",), 150.0),          # a decimal point: 25.4 mm
    (("use my SO-101 to pick up 30 mm 2 oz cubes in 2 kg batches",), 150.0),
    (("use my SO-101 to get 30 mm 50 g cubes in 2 kg batches",), 150.0),
    (("use my SO-101 to insert 30 mm 50 g cubes in 2 kg batches",), 150.0),
    (("use my SO-101 to pick up 30 mm 50 g cubes", "it moves 2 kg of them"), 150.0),
    (("use my SO-101 to pick up 30 mm 50 g cubes and move 2 kg of them every 10 s",), 150.0),
    (("use my SO-101 to pick up 30 mm 50 g cubes and the bins they come in at 2 kg per hour",), 150.0),
    (("use my SO-101 to move the bin with 30 mm 50 g cubes at 2 kg per hour",), 150.0),
    (("use my SO-101 to move 30 mm 50 g cubes with their bin at 2 kg per hour",), 150.0),
    (("use my SO-101 to pick up batches of 50 g cubes at 2 kg per hour, 30 mm wide",), 150.0),
    (("use my SO-101 to pick up 30 mm 50 g cubes and 34 mm cubes at 2 kg per hour",), 150.0),
    (("use my SO-101 to pick up 30 mm 50 g cubes in 10s at 2 kg per hour",), 150.0),
    (("use my SO-101 to pick up 30 mm 50 g cubes", "they're 300 g every minute"), 300.0),
    (("use my SO-101 to sort 30 mm 50 g cubes in 2 kg batches", "it picks up each batch"), 150.0),
    (("use my SO-101 to pick up 30 mm 50 g cubes at 2 kg per hour", "it also moves the bins"), 150.0),
    (("use my SO-101 to pick up 80 mm 50 g cubes at 2 kg per hour", "@nearest", "some are heavier"), 150.0),
    (("use my SO-101 to pick up 30 mm 50 g cubes at 2 kg per hour. " + "people nearby. " * 140,), 150.0)])
def test_a_rate_stays_as_read_wherever_the_conversation_may_say_more(turns, m):
    b = steps(*turns)
    assert not so101(b) and mass(b) >= m, (turns[:2], mass(b))


def test_the_rate_rule_still_leaves_plain_rates_out():
    for turns in (("use my SO-101 to move 30 mm 50 g cubes at 2 kg per hour",),
                  ("use my SO-101 to sort 30 mm, 40 g blocks into bins", "2 kg per hour"),
                  ("sort 50 g blocks in 2 kg batches",), ("move 50 g cubes, about 2 kg an hour",)):
        assert mass(talk(*turns)) <= 50.0, turns


# ---- round 4's corpora on the fixed rules: the objects' noun for what they come in, a dangling "each", a batch taken
# somewhere or with no sorting, a batch said to be something, and the job's object after leaving and re-joining
@pytest.mark.parametrize("turns, m", [
    (("I want a robot to pick up 40 mm 90 g cylinders and the cylinders they come in at 3 kg per hour",), 150.0),
    (("I want a robot to pick up 45 mm 50 g eggs in the bin they come in at half a kilo per hour",), 150.0),
    (("build a robot arm to pick up 40 mm 40 g bottles at 2 lb per hour (each)",), 150.0),
    (("Use my SO-101 to tidy 28 mm 20 g cups, 2 kg per batch on the table",), 150.0),
    (("build a robot arm to organise 26 mm 30 g cubes into the bin, 1 kg batches",), 150.0),
    (("build a robot arm to use 30 mm 30 g cubes in 2 kg batches",), 150.0),
    (("build a robot arm to sort 30 mm 40 g pucks we bake in 2 kg batches", "the batch is one puck"), 150.0)])
def test_a_batch_or_a_rate_beside_more_than_plain_words_stays_as_read(turns, m):
    assert mass(steps(*turns)) >= m, turns


@pytest.mark.parametrize("turns", [
    ("use my SO-101 to hand me tools", "#existing_hardware=none", "they weigh 90 g", "use my SO-101"),
    ("use my SO-101 to hand me tools", "#existing_hardware=none", "40 mm", "use my SO-101")])
def test_the_job_object_assumed_after_leaving_is_checked_as_the_general_one_on_rejoining(turns):
    # the release planned the general arm's 45 mm flat-sided box there: re-joining the SO-101 with Morph's own guess for
    # the tools (a round screwdriver widened to 45 mm) kept the stock arm, which the flat-sided box never allowed
    assert not so101(steps(*turns)), turns


# ---- the round-5 review: the SO-101 named without "my", Morph's guessed object of no known shape once the SO-101 is
# left, a part's width after its name, a rename that keeps a stated mass, and the few messages a rate is left out in
@pytest.mark.parametrize("turns", [
    ("SO-101 to move jars", "the lids are 20 mm wide"),
    ("I want the SO-101 to move 40 mm cylinders", "some of them are 20 mm wide"),
    ("SO-101 to sort 36 mm 80 g cubes at 2 kg per hour",),
    ("feed my cat with my SO-101", "#existing_hardware=none", "40 mm, 90 g", "keep my SO-101"),
    ("use my SO-101 to tidy my desk", "I do not have an SO-101", "they are 40 mm wide and weigh 90 g", "use my SO-101"),
    ("use my SO-101 to pick things up", "let's design a new one", "24 mm, 30 g", "actually, use my SO-101"),
    ("use my SO-101 to cook breakfast", "#existing_hardware=none", "@nearest", "24 mm, 30 g", "use my SO-101")])
def test_the_so101_is_checked_whenever_it_is_built(turns):
    assert not so101(steps(*turns)), turns


def test_a_new_arm_the_release_ignored_keeps_the_emergency_stop_its_so101_gave():
    b = steps("use my SO-101 to sort screws", "actually, build a new arm", "600 g")
    assert "estop" in (b.changes.get("electronics.extras") or "")
    b = steps("use my SO-101 to pick things up", "let's design a new one", "24 mm, 30 g", "actually, use my SO-101")
    assert "estop" in (b.changes.get("electronics.extras") or "")


@pytest.mark.parametrize("later", ["the caps, 20 mm across", "lids: 20 mm wide", "and the lids? about 20 mm wide",
                                   "plus 20 mm wide jars"])
def test_a_parts_width_after_its_name_never_narrows_the_objects(later):
    assert width(steps("use my SO-101 to move 40 mm jars", later)) >= 40.0, later


def test_a_rename_keeps_the_mass_they_were_said_to_weigh():
    assert mass(steps("use my SO-101 to move toy blocks", "they weigh 300 g", "they come in 40 mm cups")) >= 300.0


@pytest.mark.parametrize("turns, m", [
    (("use my SO-101 to stack 30 mm 50 g cubes on the left, then move the stack onto the shelf, about 2 kg per hour",), 150),
    (("use my SO-101 to move 30 mm 50 g cubes at 2 kg per hour", "move the stack"), 150),
    (("use my SO-101 to move 50 g balls onto the shelf, 2 kg per hour, one move per hour",), 150),
    (("use my SO-101 to pick up 2 kg of 50 g balls and put them here for a day",), 150),
    (("use my SO-101 to sort 50 g balls at one per minute and 300 g per minute",), 300),
    (("use my SO-101 to move 50 g balls at 2 kg per minute",), 150),                  # 40 a minute: several a lift
    (("use my SO-101 to sort 50 g balls in 2 kg batches", "#task=pick_place"), 150),
    (("use my SO-101 to pick up 2 kg of 50 g balls every hour",), 150),
    (("use my SO-101 to sort rods weighing 50 g per 100 mm at 2 kg per hour",), 150),
    (("use my SO-101 to sort 50 g objects and rods at 2 kg per hour",), 150),
    (("use my SO-101 to sort 40 mm 95 g tins at 1 kg an hour",), 150)])               # a label's 95 g: what they hold
def test_a_rate_is_left_out_only_in_the_few_messages_that_say_nothing_more(turns, m):
    b = steps(*turns)
    assert mass(b) >= m and not so101(b), (turns, mass(b))


# ---- the round-6 review: Morph's guess for the job, once the SO-101 is left, keeps the job's preset (its people) and
# its unknown shape through the nearest robot and any later reply; a mass before a noun is the whole weight only of
# plainly solid things
@pytest.mark.parametrize("turns", [
    ("use my SO-101", "I don't have an SO-101", "it needs wheels", "@nearest", "keep my SO-101"),
    ("use my SO-101", "I don't have an SO-101", "it needs wheels", "@nearest", "use my SO-101"),
    ("use my SO-101", "#existing_hardware=none", "it should drive around the house", "@nearest", "#existing_hardware=none")])
def test_the_guess_for_a_job_left_for_the_so101_keeps_the_jobs_people_through_the_nearest_robot(turns):
    b = steps(*turns)
    assert not so101(b) and B.value(b, "people_nearby") == "children" and "estop" in b.changes["electronics.extras"], turns


@pytest.mark.parametrize("later", ["my SO-101 can do it", "my SO-101 can handle it", "my SO-101 can pick up cups"])
def test_a_round_sounding_word_never_gives_the_guess_a_shape_the_so101_holds(later):
    # "can", "cups": no shape of the 45 mm object Morph guessed once the person said they had no SO-101
    assert not so101(steps("use my SO-101", "I don't have an SO-101", "45 mm", later)), later


@pytest.mark.parametrize("text", ["use my SO-101 to move 30 mm 88 g cartridges at 2 kg per hour",
                                  "use my SO-101 to move 30 mm 60 g pods at 2 kg per hour",
                                  "use my SO-101 to move 30 mm 90 g canisters at 2 kg per hour",
                                  "use my SO-101 to move 30 mm 60 g capsules at 2 kg per hour",
                                  "use my SO-101 to sort 30 mm 80 g refills in 2 kg batches",
                                  "use my SO-101 to move 30 mm 90 g candles at 2 kg per hour",
                                  "use my SO-101 to move 30 mm 90 g spools at 2 kg per hour",
                                  "use my SO-101 to move 30 mm tins, each tin is 95 g, at 2 kg per hour"])
def test_a_mass_before_a_noun_is_left_beside_a_rate_only_for_plainly_solid_things(text):
    # a label's mass may be what they hold (a jar candle's wax, a spool's filament): the rate stays as read
    b = talk(text)
    assert mass(b) > 100.0 and not so101(b), (text, mass(b))


@pytest.mark.parametrize("text", ["use my SO-101 to move 30 mm 50 g cubes at 2 kg per hour",
                                  "use my SO-101 to sort 30 mm 50 g cookies in 2 kg batches",
                                  "use my SO-101 to move 30 mm cubes, each cube is 50 g, at 2 kg per hour",
                                  "use my SO-101 to move 30 mm cubes weighing 50 g at 2 kg per hour",
                                  "use my SO-101 to move 30 mm tins weighing 50 g at 2 kg per hour"])
def test_a_rate_beside_a_plainly_whole_own_mass_is_still_left_out(text):
    b = talk(text)
    assert mass(b) == 50.0 and so101(b), (text, mass(b))


@pytest.mark.parametrize("later", ["lids 20 mm wide", "caps 20 mm across", "lid 20 mm wide", "20 mm wide for the caps",
                                   "cap diameter 20 mm", "the caps 20 mm wide"])
def test_a_parts_width_right_beside_its_name_never_narrows_the_objects(later):
    # the release read these as the jars' own width: the SO-101 brief planned 20 mm jars
    assert width(steps("use my SO-101 to move 40 mm jars", later)) >= 40.0, later


@pytest.mark.parametrize("later", ["lids 20 g", "lid 20 g", "cap weight 20 g", "the lids 20 g"])
def test_a_parts_mass_right_beside_its_name_never_lightens_the_object(later):
    b = steps("use my SO-101 to move 40 mm jars weighing 300 g", later)
    assert mass(b) >= 300.0 and not so101(b), (later, mass(b))


@pytest.mark.parametrize("later", ["cylinders 20 mm wide", "sorry 20 mm wide", "diameter 20 mm"])
def test_a_width_beside_their_own_name_or_a_correction_is_still_theirs(later):
    assert width(steps("use my SO-101 to move 40 mm cylinders", later)) < 40.0, later


# ---- the round-6 review of masses: a rate left out only at the pace the arm is built at, a batch kept beside a job
# chosen before or after it and beside a tidy, no error for a 0 g mass, and "in 2 kg batches" never names the objects
@pytest.mark.parametrize("turns", [
    ("use my SO-101 to move 30 mm, 50 g cubes at 600 g a minute", "one every 30 s"),
    ("use my SO-101 to move 30 mm, 50 g cubes at 12 kg per hour", "one every 60 s"),
    ("use my SO-101 to move 30 mm, 50 g cubes", "#throughput=60", "12 kg per hour"),
    ("use my SO-101 to move 30 mm, 50 g cubes at 45 kg per hour", "#throughput=10"),
    ("use my SO-101 to pick up 40 mm balls weighing 100 g, 90 kg per hour",)])           # 15 a minute: built one every 6 s
def test_a_rate_faster_than_the_arm_moves_them_one_at_a_time_stays_as_read(turns):
    b = steps(*turns)
    assert mass(b) >= 150.0 and not so101(b), (turns, mass(b))


@pytest.mark.parametrize("turns", [
    ("use my SO-101 to sort 30 mm, 50 g cubes", "#layout=tray_to_bin", "in 2 kg batches"),
    ("sort 30 mm, 50 g cubes", "#task=pick_place", "in 2 kg batches"),
    ("sort 30 mm, 50 g cubes", "#task=pick_place", "in 2 kg batches", "#existing_hardware=so101"),
    ("use my SO-101 to tidy 30 mm, 50 g toys in 2 kg batches",)])
def test_a_batch_beside_a_job_chosen_before_it_or_a_tidy_stays_as_read(turns):
    b = steps(*turns)
    assert mass(b) >= 150.0 and not so101(b), (turns, mass(b))


@pytest.mark.parametrize("text", ["move 0 g cubes at 2 kg per hour", "move 30 mm cubes weighing 0.00001 kg at 2 kg per hour",
                                  "sort 2 kg of 0 g cubes per hour"])
def test_a_zero_mass_beside_a_rate_still_gives_a_robot(text):
    assert talk(text).changes.get("object.mass_g"), text


def test_a_batch_reply_never_names_the_objects():
    b = steps("use my SO-101 to sort 30 mm 50 g cubes", "in 2 kg batches")
    assert B.value(b, "object")["name"] == "cube" and mass(b) == 50.0 and so101(b)


def test_a_rate_is_left_out_after_with_my_so101_too():
    b = talk("with my SO-101, move 30 mm 50 g cubes at 2 kg per hour")
    assert mass(b) == 50.0 and so101(b)


# ---- a later width said beside their own noun, "each" or a width word, ways the release never read ("the cylinders
# 60 mm", "width: 60 mm", "the diameter is 60 mm"): read, but only ever wider; a smaller one is the least they may be
@pytest.mark.parametrize("later", ["the cylinders 60 mm", "width 60 mm", "width: 60 mm", "60 mm for the cylinders",
                                   "60 mm (all of them)", "typo 60 mm", "the diameter is 60 mm", "their diameter is 60 mm",
                                   "the cylinders' diameter is 60 mm", "60 mm each", "it should say 60 mm", "approx 60 mm",
                                   "the width is 60 mm"])
def test_a_larger_width_said_any_of_these_ways_keeps_the_so101_off(later):
    b = steps("use my SO-101 to move 30 mm cylinders weighing 50 g", later)
    assert width(b) == 60.0 and not so101(b), later


@pytest.mark.parametrize("later", ["the cylinders 20 mm", "typo 20 mm", "width 20 mm", "20 mm for the cylinders",
                                   "20 mm (all of them)"])
def test_a_smaller_width_said_a_new_way_never_narrows_but_keeps_the_so101_off_below_its_jaws(later):
    b = steps("use my SO-101 to move 40 mm cylinders", later)
    assert width(b) == 40.0 and not so101(b), later


@pytest.mark.parametrize("later", ["60 mm each way", "the bin's width is 60 mm", "bin width 60 mm", "the width of the slot is 60 mm",
                                   "move the cylinders 60 mm to the left", "gripper width 60 mm", "the cylinders 60 mm apart"])
def test_a_distance_or_another_things_width_beside_them_is_not_theirs(later):
    b = steps("use my SO-101 to move 30 mm cylinders weighing 50 g", later)
    assert width(b) == 30.0 and so101(b), later


# ---- the round-7 review: a part's shape or size in a later message, a job's robot that kept the SO-101, Morph's guess
# through the nearest robot, round-sounding words, and the ways of no longer having the SO-101
@pytest.mark.parametrize("turns", [
    ("use my SO-101 to move 28 mm cylinders", "45 mm bases, they're square"),
    ("use my SO-101 to move 30 mm cylinders", "they have 40 mm bases, rectangular"),
    ("use my SO-101 to move 30 mm candles", "they stand on 44 mm bases, square ones"),
    ("use my SO-101 to move 30 mm tubes", "with 40 mm tops, square"),
    ("use my SO-101 to move 30 mm cylinders", "40 mm handles, square"),
    ("move 30 mm cylinders", "40 mm handles (they're flat)", "use my SO-101"),
    ("use my SO-101 to move 30 mm balls", "35 mm bottoms, flat"),
    ("use my SO-101 to move 30 mm cylinders", "square heads of 44 mm")])
def test_a_flat_sided_part_wider_than_the_so101s_flat_band_keeps_it_off(turns):
    # the objects stay round cylinders, but their widest part is a flat-sided base wider than the jaws' 34 mm
    assert not so101(steps(*turns)), turns


def test_a_parts_size_named_after_it_never_narrows_the_objects():
    b = steps("use my SO-101 to move 50 mm cylinders", "30 mm handles")
    assert width(b) == 50.0 and not so101(b)


@pytest.mark.parametrize("turns", [
    ("use my SO-101 to move 30 mm cylinders", "it should cook dinner", "@nearest", "lids 20 mm wide"),
    ("use my SO-101 to move 30 mm cups", "it should drive around the house", "@nearest", "caps 20 mm across"),
    ("use my SO-101 to move 32 mm bottles weighing 80 g", "it should cook dinner", "@nearest", "cap diameter 20 mm"),
    ("use my SO-101 to move 32 mm bottles weighing 80 g", "it should cook dinner", "@nearest", "necks 40 mm wide"),
    ("use my SO-101 to move 30 mm cylinders", "it should drive around the house", "@nearest", "some are 20 mm"),
    ("use my SO-101 to move 30 mm cups", "it should cook dinner", "@nearest", "they're 20 mm", "they're fragile"),
    ("use my SO-101 to move 32 mm balls", "it should cook dinner", "@nearest", "40 mm at the top"),
    ("use my SO-101 to move eggs", "it should cook dinner", "@nearest", "24 mm", "they're 30 mm balls")])
def test_a_jobs_robot_that_kept_the_so101_keeps_its_emergency_stop_on_a_new_arm(turns):
    assert "estop" in (steps(*turns).changes.get("electronics.extras") or ""), turns


@pytest.mark.parametrize("turns", [
    ("use my SO-101 to hand me tools", "I don't have an SO-101", "40 mm", "it should drive around the house", "keep my SO-101"),
    ("use my SO-101 in the lab", "I don't have an SO-101", "40 mm", "use a suction cup", "keep my SO-101"),
    ("use my SO-101 to load the dishwasher", "I don't have an SO-101", "35 mm, 40 g", "keep my SO-101"),
    ("use my SO-101 to move test tubes", "my SO-101 is broken", "they weigh 80 g", "use a suction cup", "keep my SO-101"),
    ("use my SO-101 to stack 40 mm widgets",)])
def test_the_so101_a_nearest_robot_keeps_is_checked_for_an_object_of_no_known_shape(turns):
    assert not so101(steps(*turns)), turns


def test_a_guess_of_no_known_shape_is_planned_round_on_a_generated_arm():
    b = steps("use my SO-101 to recycle bottles", "let's design a new one", "they're 25 mm wide")
    assert b.changes.get("object.shape") == "cylinder" and width(b) >= 26.0


@pytest.mark.parametrize("later", ["my SO-101 can do it", "the caps are round", "they go in cups"])
def test_a_round_word_in_a_later_reply_never_makes_flat_sided_objects_round(later):
    b = steps("use my SO-101 to move 40 mm cubes", later)
    assert b.changes.get("object.shape") == "box" and not so101(b), later


def test_a_later_reply_that_says_their_shape_still_gives_it():
    b = steps("use my SO-101 to move 40 mm cubes", "they're round")
    assert b.changes.get("object.shape") == "cylinder" and so101(b)


def test_the_no_so101_chip_on_a_jobs_robot_keeps_its_people():
    b = steps("a robot to tidy the playroom", "#existing_hardware=none")
    assert B.value(b, "people_nearby") == "children" and "estop" in b.changes["electronics.extras"]


@pytest.mark.parametrize("later", ["my SO-101 broke", "I lent my SO-101 to a friend", "forget the SO-101"])
def test_an_so101_the_person_can_no_longer_use_is_left(later):
    assert not so101(steps("use my SO-101 to sort screws", later)), later


@pytest.mark.parametrize("later, w", [("sorry, 40 mm wide for the cylinders themselves", 40.0),
                                      ("sorry, 30 mm wide (my mistake)", 30.0), ("30 mm wide for the lot", 30.0),
                                      ("off the top of my head 40 mm wide", 40.0), ("at the end of the day 40 mm wide", 40.0)])
def test_a_correction_beside_words_that_name_no_part_is_still_read(later, w):
    assert width(steps("use my SO-101 to move 60 mm cylinders", later)) == w, later


# ---- the round-7 review of masses: a rate a later reply gives after one was left out, every own mass beside a rate
# only for solid things, a day's amount maybe done in an hour, and a mass brought back on the nearest robot's stand-in
@pytest.mark.parametrize("turns", [
    ("use my SO-101 to move 30 mm, 50 g cubes at 60 g per hour", "#throughput=60", "450 g per minute"),
    ("use my SO-101 to move 30 mm, 50 g cubes at 60 g per hour", "450 g per minute", "#throughput=60"),
    ("use my SO-101 to sort 30 mm, 50 g cubes at 60 g per hour", "#task=pick_place", "in 2 kg batches"),
    ("use my SO-101 to sort 30 mm, 50 g cubes at 90 g per hour", "in 2 kg batches", "it picks up a whole batch at once"),
    ("use my SO-101 to move 30 mm, 50 g cubes at 60 g per hour", "2 kg per hour", "that's the empty weight")])
def test_a_rate_a_later_reply_gives_after_one_was_left_out_is_never_lost(turns):
    b = steps(*turns)
    assert mass(b) >= 450.0 and not so101(b), (turns, mass(b))


@pytest.mark.parametrize("text", ["use my SO-101 to move 45 mm jars, each jar is 45 g, at 2 kg per hour",
                                  "use my SO-101 to move 30 mm jams weighing 90 g at 2 kg per hour",
                                  "use my SO-101 to move 30 mm pastes weighing 90 g at 2 kg per hour",
                                  "use my SO-101 to move 30 mm, 50 g cubes at 100 kg a day"])
def test_a_rate_beside_a_containers_mass_or_a_days_large_amount_stays_as_read(text):
    b = talk(text)
    assert mass(b) >= 150.0 and not so101(b), (text, mass(b))


@pytest.mark.parametrize("turns", [
    ("use my SO-101 to move 80 mm, 50 g cubes at 6 kg per hour", "@nearest", "#throughput=60"),
    ("use my SO-101 to sort 80 mm, 50 g cubes in 2 kg batches", "@nearest", "#task=pick_place")])
def test_a_rate_brought_back_on_the_nearest_robots_object_is_held_to_morphs_largest(turns):
    b = steps(*turns)
    assert mass(b) == 500.0 and float(B.value(b, "object")["mass_g"]) <= 500.0, turns


def test_the_no_so101_chip_on_a_job_without_a_stop_offers_the_releases_nearest_robot():
    # keeping the folding job there gave the nearest robot the job's own 50 g socks, not the 60 g folded washcloth
    b = steps("a robot that folds laundry", "each one is 50 mm", "they're fragile", "#existing_hardware=none")
    assert mass(b) >= 60.0, mass(b)
