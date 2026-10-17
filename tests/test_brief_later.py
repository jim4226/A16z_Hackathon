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
                                  "sort 50 g blocks in 2 kg batches", "move 2 kg of 50 g cubes", "pick up cubes weighing 50 g, 2 kg/h",
                                  "move 50 g cubes, 2 kg of them a day", "move 50 g cubes at 2 kg per min",
                                  "pick up the 50 g cookies we bake in 2 kg batches"])
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
    first = "use my SO-101 to pick up 30 mm 50 g cups at 2 kg per hour"
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
    ("use my SO-101 to move samples in the lab", "they're 60 mm"),                         # wider than the assumed one
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
