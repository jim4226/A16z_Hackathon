"""Conversational brief: an ambiguous one-line request -> a few chip questions -> a whole-robot design.

The canonical Brief of RELEASE_CONTRACTS section 7. SubQ triage, Claude extraction (agents.py), video/photo
analysis and the offline rules here all fill the same slots; this module is deterministic and offline and is
where the decisions are made: what to ask (at most 3 rounds of at most 3 questions, each with 2-4 chips whose
values are inside the capability envelope), what is out of scope and the nearest robot that can be built, how
slots map to design keys (brief_to_changes), and a ~30 ms preflight of the generator's own checks with a repair
loop. Every default, inference and repair becomes a plain-sentence assumption on the confirm card.

    b = brief.start("something to pick strawberries")      # b.status == "asking", b.questions[...]
    brief.answer(b, {"layout": "side_to_side"}); brief.progress(b, defaults_now=True)
    b.status, b.changes, b.preflight                          # "ready", {...dotted design keys...}, {"issues": []}
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field

from . import capability as cap
from .intent import OBJECTS, fit_finger, normalize_units, object_alias

SLOTS = ("task", "object", "fragility", "layout", "throughput", "environment", "people_nearby", "precision", "budget",
         "perception", "connectivity", "quantity", "existing_hardware")
SOURCES = ("user", "inferred", "default", "memory", "video", "photo")
STATUSES = ("asking", "ready", "building", "built", "out_of_scope", "refused")
RANK = {"default": 0, "inferred": 1, "memory": 2, "user": 3, "video": 3, "photo": 3}
MAX_ROUNDS, MAX_QUESTIONS = 3, 3
MIN_COST_USD = 280          # cheapest arm the generator makes (smart-bus 5-joint, printed): research builds $278-$287
DYNAMIXEL_BUDGET = 1000     # a 5-joint Dynamixel arm is about $990 in parts
DRAFT_TTL_S = 7 * 24 * 3600
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ID_RE = re.compile(r"^[a-f0-9]{16}$")

TASKS = {"pick_place": "pick and place", "sort": "sort into bins", "hand_over": "hand things over",
         "transfer": "move between stations", "dispense": "carry to a feeding spot"}
FRAGILITY_N = {"robust": 0.0, "care": 10.0, "fragile": 3.0}
ENVIRONMENTS = {"home": "home", "kitchen": "kitchen counter", "lab": "lab bench", "workshop": "workshop bench",
                "office": "office desk", "classroom": "classroom"}
PEOPLE = {"no": "it works on its own", "yes": "people work next to it", "children": "children are nearby", "pets": "pets are nearby"}
PRECISION = {"loose": "about ±5 mm", "tight": "about ±2 mm", "very_tight": "about ±1 mm"}
LAYOUTS = {  # pick -> place on the table plane (base frame, mm); all three size to a 417-480 mm arm
    "tray_to_bin": {"label": "Tray in front, bin on the left", "pick": (220.0, -80.0), "place": (160.0, 170.0),
                    "from": "from a tray in front", "to": "into a bin on its left"},
    "side_to_side": {"label": "Right side to left side", "pick": (180.0, -150.0), "place": (180.0, 150.0),
                     "from": "from its right side", "to": "to its left side"},
    "short_hop": {"label": "Two spots 12 cm apart", "pick": (230.0, -60.0), "place": (230.0, 60.0),
                  "from": "from one spot", "to": "to another 12 cm away"},
    # the mirror image, for "from the left to the right" in someone's own words (never offered as a chip)
    "left_to_right": {"label": "Left side to right side", "pick": (180.0, 150.0), "place": (180.0, -150.0),
                      "from": "from its left side", "to": "to its right side", "typed": True},
}

# Objects the archetypes offer (presets from intent.OBJECTS are used by key). Every one is inside the envelope and
# passes preflight after repair (tests/test_brief.py). key: name, shape, width mm, length mm, g, friction, damage N, slug, label
_CAT = (
    ("tube_50ml", "50 ml centrifuge tube", "cylinder", 30, 115, 60, 0.4, 10, "tube", "50 ml tubes"),
    ("glass_vial", "glass sample vial", "cylinder", 28, 60, 25, 0.35, 8, "vial", "Glass vials"),
    ("tube_carrier", "tube carrier block", "box", 40, 80, 120, 0.5, 0, "carrier", "Tube carrier blocks"),
    ("socks", "rolled pair of socks", "box", 45, 90, 50, 0.9, 0, "sock", "Rolled socks"),
    ("plush_toy", "small plush toy", "sphere", 60, 60, 50, 0.8, 0, "plush", "Small plush toys"),
    ("washcloth", "folded washcloth", "box", 40, 100, 60, 0.9, 0, "cloth", "Folded washcloths"),
    ("duplo", "Duplo-size brick", "box", 32, 32, 12, 0.4, 0, "brick", "Duplo-size bricks"),
    ("parts_bin", "small parts bin", "box", 50, 80, 150, 0.5, 0, "bin", "Small parts bins"),
    ("large_fitting", "large pipe fitting", "cylinder", 40, 50, 80, 0.4, 0, "fitting", "Large fittings"),
    ("toy_block", "wooden toy block", "box", 40, 40, 30, 0.5, 0, "block", "Wooden toy blocks"),
    ("screwdriver", "screwdriver", "cylinder", 30, 160, 80, 0.6, 0, "screwdriver", "Screwdrivers"),
    ("pliers", "pliers", "box", 30, 170, 180, 0.5, 0, "pliers", "Pliers"),
    ("hex_case", "hex key case", "box", 40, 120, 150, 0.5, 0, "tool", "Hex key cases"),
    ("espresso_cup", "espresso cup", "cylinder", 60, 60, 120, 0.5, 20, "cup", "Espresso cups"),
    ("ramekin", "small ramekin", "cylinder", 70, 70, 150, 0.5, 20, "ramekin", "Small ramekins"),
    ("glue_stick", "glue stick", "cylinder", 30, 100, 40, 0.5, 0, "glue-stick", "Glue sticks"),
    ("stapler", "stapler", "box", 40, 150, 200, 0.5, 0, "stapler", "Staplers"),
    ("pen_cup", "pen cup", "cylinder", 70, 100, 150, 0.5, 0, "pen-cup", "Pen cups"),
    ("bottle_330", "330 ml bottle", "cylinder", 60, 180, 360, 0.4, 0, "bottle", "330 ml bottles"),
    ("empty_bottle", "empty plastic bottle", "cylinder", 65, 200, 30, 0.4, 0, "bottle", "Empty plastic bottles"),
    ("empty_can", "empty drink can", "cylinder", 66, 120, 15, 0.35, 0, "can", "Empty cans"),
    ("watering_bottle", "small watering bottle", "cylinder", 60, 150, 250, 0.4, 0, "watering", "A small watering bottle"),
    ("spray_bottle", "spray bottle", "cylinder", 50, 160, 200, 0.4, 0, "spray", "A spray bottle"),
    ("food_cup", "cup of pet food", "cylinder", 60, 70, 120, 0.5, 0, "food-cup", "A cup of food"),
    ("treat_ball", "treat ball", "sphere", 45, 45, 40, 0.6, 0, "treat", "Treat balls"),
    ("small_box", "small box", "box", 45, 45, 100, 0.5, 0, "box", "Small boxes"),
    ("jar", "glass jar", "cylinder", 65, 90, 300, 0.4, 0, "jar", "Glass jars"),
    ("pill_bottle", "pill bottle", "cylinder", 35, 75, 40, 0.5, 0, "pill-bottle", "Pill bottles"),
)
CATALOG = {r[0]: dict(zip(("name", "shape", "diameter_mm", "length_mm", "mass_g", "friction", "max_force_n", "slug", "label"), r[1:]))
           for r in _CAT}
PRESET_INFO = {"raw egg": ("egg", "Eggs"), "strawberry": ("strawberry", "Strawberries"), "tomato": ("tomato", "Tomatoes"),
               "grape": ("grape", "Grapes"), "ping pong ball": ("ping-pong", "Ping pong balls"),
               "light bulb": ("bulb", "Light bulbs"), "soda can": ("can", "Full drink cans"), "marker": ("marker", "Markers"),
               "water bottle": ("bottle", "Water bottles")}


@dataclass(frozen=True)
class Archetype:
    key: str
    label: str
    task: str
    keywords: tuple            # (regex, weight)
    objects: tuple             # catalog / preset keys offered as chips, default first
    optional: tuple            # optional questions in order (object is always the required one)
    environment: str = "home"
    people: str = "no"
    layout: str = "tray_to_bin"
    throughput: float = 6.0
    perception: str = "none"
    object_q: str = "What will it pick up?"
    things: str = "the objects"
    name: str = "{obj}-picker"
    note: str = ""             # scope note when the archetype itself is an adjusted version of the request
    place: str = ""            # what the tray-to-bin layout sets things down by, when it isn't a bin ("plant")
    more: tuple = ()           # objects it builds when the request names them (jars, pill bottles), not offered as chips


_FOOD = (r"\b(strawberr(?:y|ies)|berr(?:y|ies)|eggs?|tomato(?:e?s)?|fruits?|grapes?|cherr(?:y|ies)|peach(?:es)?|plums?|apricots?|"
         r"kiwis?|mushrooms?|cupcakes?|muffins?|pastr(?:y|ies)|cookies?|donuts?|doughnuts?|chocolates?|truffles?|produce|harvest\w*|"
         r"punnets?|cartons?|bakery)\b")
ARCHETYPES = {a.key: a for a in (
    Archetype("fragile_food", "Fragile food", "pick_place", ((_FOOD, 3),), ("strawberry", "raw egg", "tomato"),
              ("layout", "throughput", "people_nearby"), environment="kitchen", object_q="Which food will it handle?",
              things="the food"),
    Archetype("lab_transfer", "Lab transfer", "transfer",
              ((r"\b(labs?|laboratory)\b", 2), (r"\btest[- ]?tubes?\b", 3),
               (r"\b(vials?|pipettes?|petri|cuvettes?|falcon|centrifuge|eppendorf|samples?|specimens?|reagents?|assays?|microplates?)\b", 2)),
              ("tube_50ml", "glass_vial", "tube_carrier"), ("people_nearby", "layout", "throughput"), environment="lab",
              people="yes", throughput=8.0, object_q="Which tubes or holders will it move?", things="the tubes",
              name="{obj}-mover"),
    Archetype("small_parts", "Sorting small parts", "sort",
              ((r"\bsort(?:s|ing|er)?\b", 1),
               (r"\b(screws?|bolts?|nuts|washers?|lego|beads?|coins?|resistors?|components|parts|fasteners?|marbles?|pills?|duplo|bricks?)\b", 2)),
              ("parts_bin", "duplo", "large_fitting"), ("perception", "layout", "throughput"), environment="workshop", more=("pill_bottle",),
              perception="wrist_camera", throughput=5.0, object_q="What will it pick up and sort?", things="the parts",
              name="{obj}-sorter"),
    Archetype("soft_goods", "Soft goods", "sort",
              ((r"\bsocks?\b", 3), (r"\b(laundry|clothes|clothing|garments?|towels?|washcloths?|fabric|cloth|t-?shirts?|shirts?|"
                                     r"textiles?|plush\w*|stuffed)\b", 2)),
              ("socks", "washcloth", "plush_toy"), ("layout", "throughput"), object_q="What will it pick up?",
              things="the items", name="{obj}-sorter"),
    Archetype("containers", "Cans, bottles and jars", "pick_place",
              ((r"\b(cans|sodas?|beer|bottles?|jars?|tins?|beverages?|drinks?|recycl\w*|pour\w*)\b", 2),
               (r"\b(?:a|the|empty|soda|drink)\s+can\b", 2)),
              ("bottle_330", "empty_can", "empty_bottle", "soda can"), ("layout", "quantity"), more=("jar",),
              object_q="Which containers will it move?", things="the containers", name="{obj}-mover"),
    Archetype("plant_watering", "Plant watering", "transfer",
              ((r"\bwater(?:s|ing)?\s+(?:my |the |our )?(?:house)?(?:plants?|garden|flowers?|herbs?|seedlings?)\b", 5),
               (r"\b(plants?|garden\w*|flowers?|herbs?|seedlings?|watering)\b", 2)),
              ("watering_bottle", "spray_bottle"), ("layout", "people_nearby"), throughput=10.0,
              object_q="What should it carry to the plants?", things="the bottle", name="plant-helper", place="plant",
              note="Pouring is not simulated: the robot carries the bottle to the plant and sets it down."),
    Archetype("pet_feeding", "Pet feeding", "dispense",
              ((r"\bfeed(?:s|ing|er)?\s+(?:my |the |our )?(?:cats?|dogs?|pets?|fish|hamsters?|rabbits?|birds?)\b", 5),
               (r"\b(kibble|pet food|cat food|dog food|treats?|feeder|feeding)\b", 2)),
              ("food_cup", "treat_ball"), ("people_nearby", "layout"), people="pets", throughput=10.0,
              object_q="What should it bring to the bowl?", things="the food", name="pet-helper", place="feeding spot",
              note="Tipping food out is not simulated: the robot carries the food to the feeding spot and sets it down."),
    Archetype("dishes", "Dishes", "pick_place",
              ((r"\b(dish(?:es)?|dishwasher|plates?|bowls?|cutlery|silverware|mugs?|(?<!suction )cups?|glasses|ramekins?|saucers?)\b", 2),),
              ("espresso_cup", "ramekin"), ("people_nearby", "layout"), environment="kitchen", people="yes",
              object_q="Which dishes will it move?", things="the dishes", name="cup-mover"),
    Archetype("tool_handover", "Tool hand-over", "hand_over",
              ((r"\b(?:hands?|pass(?:es)?|gives?|brings?)\s+(?:me\s+)?(?:the\s+|my\s+)?tools?\b", 4), (r"\bhand(?:s)? me\b", 3),
               (r"\b(tools?|screwdrivers?|wrench(?:es)?|pliers|spanners?|hammers?|workbench|hex keys?|allen keys?)\b", 2)),
              ("screwdriver", "pliers", "hex_case"), ("layout", "people_nearby"), environment="workshop", people="yes",
              layout="side_to_side", object_q="Which tools will it hand you?", things="the tools", name="tool-helper"),
    Archetype("desk_items", "Desk items", "pick_place",
              ((r"\b(pens?|pencils?|markers?|staplers?|stationery|glue sticks?|erasers?|highlighters?)\b", 3),
               (r"\b(desk|office|notebooks?|paper ?clips?)\b", 1)),
              ("pen_cup", "glue_stick", "stapler"), ("layout",), environment="office", people="yes", layout="short_hop",
              object_q="Which desk items will it move?", things="the items", name="desk-helper"),
    Archetype("toys", "Toys", "pick_place",
              ((r"\b(toys?|blocks?|ping[- ]?pong|balls?|plush(?:ies)?|stuffed animals?|teddy|kids?|children|playroom|tid(?:y|ies) up)\b", 2),),
              ("toy_block", "ping pong ball", "plush_toy"), ("people_nearby", "layout"), people="children",
              object_q="Which toys will it pick up?", things="the toys", name="toy-tidier"),
    Archetype("generic_arm", "General pick and place", "pick_place", (), ("small_box", "strawberry", "raw egg", "empty_can"),
              ("task", "layout", "budget"), object_q="What will it pick up?"),
    Archetype("so101", "Your SO-101 arm", "pick_place", ((r"\bso[- ]?10[01]\b", 6), (r"\ble ?robot\b", 6)),
              ("raw egg", "strawberry", "ping pong ball"), ("layout",), object_q="What will your SO-101 pick up?",
              name="so101-{obj}-picker"),
)}

# Things people name that are outside the gripper's band (cap.min_width for the shape, up to 75 mm wide; 500 g), and what
# the archetype offers instead. Every width in a note is the gripper's real minimum for that shape (narrowest with the
# force-sensing finger): 26 mm for round things, 21.5 mm for flat-sided ones.
_ROUND, _FLAT = f"{cap.min_width('cylinder'):g} mm", f"{cap.min_width('box'):g} mm"
IMPLICIT = (
    (r"\btest[- ]?tubes?\b(?!.*\b(?:50 ?ml|falcon|centrifuge)\b)", "tube_50ml",
     f"Standard test tubes (12 to 20 mm) are thinner than the {_ROUND} this gripper closes on; 50 ml tubes (30 mm) or a tube "
     "carrier fit."),
    (r"\bpills?\b|\btablets?\b|\bcapsules?\b|\bmedications?\b|\bmedicines?\b", "pill_bottle",
     f"Single pills (5 to 15 mm) are far smaller than the {_ROUND} this gripper closes on; the robot can move the pill bottle "
     "(35 mm) instead."),
    (r"\b(screws?|bolts?|nuts|washers?|beads?|coins?|resistors?|components|fasteners?|marbles?)\b", "parts_bin",
     f"Screws, nuts and similar parts are far smaller than the {_FLAT} the gripper closes on; the robot can move them in small "
     "parts bins instead."),
    (r"\blego\b(?!.*duplo)", "duplo", f"LEGO bricks (16 mm) are narrower than the {_FLAT} this gripper closes on for a "
     "flat-sided part; Duplo-size bricks (32 mm) fit."),
    (r"\b(pens?|pencils?|markers?|highlighters?)\b", "pen_cup",
     f"Single pens and pencils (8 to 18 mm) are thinner than the {_ROUND} this gripper closes on; it can move the pen cup "
     "(70 mm) instead."),
    (r"\bgrapes?\b", "strawberry", f"Grapes (20 mm) are below the {_ROUND} the gripper closes on; strawberries, eggs and "
     "tomatoes fit."),
    (r"\b(plates?|dinner plates?|pans?|pots?|books?|laptops?|shoes?)\b", None,
     f"Plates, pans, books and shoes are wider than the {cap.MAX_WIDTH_MM:g} mm this gripper opens around."),
    (r"\bwater bottles?\b|\bbottles? of water\b|\bfull bottles?\b", "bottle_330",
     f"A full 0.5 l water bottle (520 g) is too heavy: a desk arm here lifts {cap.MAX_MASS_G} g at most; a 330 ml bottle "
     "(360 g) fits."),
)
# Jobs inside a pick and place that the gripper can't do: the robot is built for the moving part and the note says what
# is left out, so the scope reads "Adjusted". Kept narrow: a verb, never the thing ("open-top bins", "an open box" and
# "open boxes arrive" open nothing; "twist ties", "screw caps", "brass inserts" and "plugs" are things to move; an
# "assembly line" is a place).
_DET = "".join(rf"(?<!\b{d} )" for d in ("a", "an", "the", "my", "her", "his", "its", "our", "their", "these", "those", "some",
                                          "up", "move", "moves", "carry", "carries", "grab", "grabs", "sort", "sorts", "hold",
                                          "holds", "lift", "lifts", "handle", "handles", "place", "of", "with", "in", "into", "on",
                                          "onto", "from"))
# a plural that is also a verb ("inserts", "plugs") is the verb only after the one doing it: "a robot that inserts pins"
_DOER = r"(?:\b(?:it|that|which|robot|arm)\s+)"
# "open boxes arrive on the left": the boxes are the subject of a verb, so "open" describes them
_SUBJECT = (r"(?!\s+(?:are|is|were|was|arriv\w*|come|comes|coming|sit|sits|sitting|lie|lies|lying|wait\w*|stand\w*|roll\w*|"
            r"ride|rides|riding|travel\w*|go|goes|going|pile\w*|stack\w*|move|moves|moving|get|gets|show|shows)\b)")
ADJUSTED = (
    ("twisting", r"\b(?:unscrew\w*|" + _DET + r"twist(?:s|ing)?\b(?![- ]?ties?\b|-)|" + _DET +
                 r"screw(?:ing)? (?:on|off)\b|" + _DET + r"screw(?:ing)? (?:the |a |an )?(?:caps?|lids?|tops?|bulbs?)\b|"
                 r"turn(?:s|ing)? (?:the |a |an )?(?:caps?|lids?|knobs?|valves?|taps?)\b|tighten\w*|loosen\w*)",
     "Unscrewing, screwing on and twisting need a wrist that turns while it grips; this gripper only picks things up and sets "
     "them down, so the robot moves them without turning them."),
    # "open" as a verb only: "an open box" and "the open drawer" describe a thing, "open-top bins" never matches
    ("opening", _DET + r"\b(?:open(?:s|ing)?|prys?|pries|prying)\s+(?:up\s+)?(?:[\w']+\s+){0,3}?(?:jars?|bottles?|lids?|caps?|"
                r"tins?|cans?|containers?|tubs?|packages?|packets?|"
                r"parcels?|box(?:es)?|cartons?|bags?|envelopes?|letters?|mail|doors?|drawers?|cupboards?|cabinets?)\b" + _SUBJECT +
                r"|\b(?:take|pull)s?\s+(?:the |a )?(?:lids?|caps?|tops?)\s+off\b|\bremov\w*\s+(?:the |a )?(?:lids?|caps?)\b",
     "Opening jars, bottles, lids or packaging takes a twist or a pull this gripper can't make; it only picks things up and "
     "sets them down, so the robot moves them and the opening is left to a person."),
    ("assembly", r"(?<!-)\bassembl(?:e|es|ing|y)\b(?!\s+(?:lines?|stations?|areas?|tables?|bench(?:es)?|cells?|floors?|plants?|"
                 r"rooms?|jigs?|drawings?|instructions?|kits?|halls?|shops?)\b)|"
                 r"\bput(?:s|ting)? (?:\w+ )?together|\bsnap(?:s|ping)? (?:\w+ )?(?:together|into)\b|"
                 r"(?:" + _DET + r"\bplug(?:ging)?|" + _DOER + r"plugs)\s+(?:\w+\s+){0,2}into\b|"
                 + _DET + r"\binsert(?:ing|ed)?\b|" + _DOER + r"inserts\b|\bpress(?:es|ing)? (?:\w+ )?(?:together|into)\b|"
                 r"\bpress[- ]fit\w*|\bbuild(?:s|ing)? (?:\w+ )?(?:with|out of) (?:lego|duplo|bricks?|blocks?)",
     "Assembling, inserting and pressing parts together need force control and alignment this gripper and its simulator "
     "don't have; the robot moves the parts, it doesn't fit them together."),
)
# Words for a person the robot works for or hands things to: they are next to it, so it gets an emergency stop
_PERSON = (r"(?:grand(?:ma|pa|mother|father|parents?|mum|mom|dad)|granny|gran|nan(?:a|na)?|elderly \w+|"
           r"my (?:mum|mom|mother|dad|father|parents?|partner|wife|husband|boyfriend|girlfriend|roommate|flatmate|friend|"
           r"aunt|uncle|brother|sister|patient|client)s?)")
# a hand-over goes to a person: "hand me", "pass my dad his pills", "give it to her", "bring the cups back to us". "them" or
# "it" alone are the things: "pass them to the packing station" is a pick and place
_RECEIVER = r"(?:me|him|her|us|" + _PERSON + r")\b"
HAND_TO_PERSON = r"\b(?:hand|pass|give|bring)(?:s|es|ing)?\s+(?:" + _RECEIVER + r"|(?:[\w']+\s+){0,4}?to\s+" + _RECEIVER + r")"
PERSON_NEAR = (r"\b" + _PERSON + r"\b|\b(?:help|assist|serve)(?:s|es|ing)?\s+(?:me|him|her|us|them)\b|" + HAND_TO_PERSON +
               r"|\b(?:for|to)\s+(?:me|him|her)\b|\b(?:carers?|caregivers?|disabled|wheelchair|bedridden|patients?)\b")
OUT_OF_SCOPE = (  # (kind, regex, plain reason); the first kind found picks the nearest robot
    ("drone", r"\b(drones?|quadcopters?|multirotors?|uavs?|fly|flies|flying)\b", "Morph doesn't design drones or anything that flies."),
    ("humanoid", r"\b(humanoids?|androids?|bipeds?|robot butler|two arms|dual[- ]arm|bimanual|multi-?finger\w*|dexterous|robotic hand|"
                 r"five fingers?)\b",
     "Morph builds one arm with a two-finger gripper, not humanoids or multi-finger hands."),
    ("mobile", r"\b(mobile|wheels?|wheeled|rover|drives? around|driving around|roam\w*|vacuum(?:s|ing)?(?! (?:gripper|cups?))|roomba|"
               r"lawn ?mower|mows?|delivery robot|follows? me|navigat\w+|patrol\w*|legged|quadruped|walks? around|robot dog|"
               r"self[- ]driving|around the house)\b",
     "Morph designs stationary desk arms; it can't build robots that drive, walk or roam."),
    ("suction", r"\b(suction|vacuum (?:gripper|cups?))\b", "The gripper is a two-finger parallel gripper; suction grippers aren't generated."),
    ("pouring", r"\b(pour(?:s|ing)?|fill(?:s|ing)? (?:a |the |my )?(?:glass|cup|bottle|bowl|mug)|mix(?:es|ing)? (?:drinks|cocktails)|"
                r"bartend\w*)\b",
     "Pouring isn't simulated: the arm only picks objects and places them on the table."),
    ("folding", r"\b(fold(?:s|ing)?)\b", "Folding cloth isn't something this gripper or its simulator can do."),
    ("cooking", r"\b(cook(?:s|ing)?|fry|fries|frying|stir(?:s|ring)?|chop(?:s|ping)?|bake(?:s)?|baking|grill(?:s|ing)?|"
                r"flip(?:s|ping)? (?:pancakes|burgers|eggs)|make (?:me )?(?:breakfast|dinner|lunch|a meal|a sandwich|coffee))\b",
     "Cooking isn't in scope: the arm picks and places objects, it doesn't handle heat, stirring or cutting."),
    ("above_table", r"\b(stack(?:s|ing)?|shel(?:f|ves)|cupboards?|top shelf|hang(?:s|ing)?|on the wall|dishwasher)\b",
     "Places are on the table plane only; stacking, shelves and racks above or below the table aren't generated."),
)
_TARGET = (r"(?:my |the |a |an |our |his |her |their |some )?(people|persons?|someone|somebody|anyone|humans?|kids?|children|"
           r"brothers?|sisters?|siblings?|neighbou?rs?|intruders?|strangers?|cats?|dogs?|animals?|birds?|pets?|squirrels?|faces?)\b")
HARM = (  # local pre-filter (SubQ's harm noul and the designer's flag catch the rest); tuned against fragile-food phrasing
    r"\b(weapons?|firearms?|rifles?|pistols?|tasers?|stun guns?|(?<!bath )bombs?|explosives?|grenades?|missiles?|ammunition|ammo)\b",
    r"\b(?<!glue )(?<!heat )(?<!caulk )(?<!caulking )(?<!paint )(?<!spray )(?<!nail )(?<!staple )(?<!massage )(?<!label )guns?\b",
    r"\b(shoot\w*|launch\w*|fir(?:e|es|ing)|throw\w*|fling\w*|spray\w*)\b[^.]{0,40}\b(?:at|on)\s+(?:him\b|her\b|them\b|" + _TARGET + ")",
    r"(?<!without )(?<!not )(?<!never )(?<!avoid )(?<!no )\b(hurt\w*|harm\w*|injur\w*|attack\w*|punch\w*|kick\w*|stab\w*|kill\w*|slap\w*|"
    r"shoot\w*|zap\w*|tase\w*|electrocut\w*|"
    r"chok\w*|strangl\w*|poison\w*|tortur\w*)\s+" + _TARGET,
    r"\b(restrain\w*|handcuff\w*|tie (?:up|down) " + _TARGET + r"|hold (?:someone|a person|people|him|her|them) (?:down|still))",
    r"\b(spy(?:ing)? on|secretly (?:watch|record|film)\w*|covert(?:ly)? (?:watch|record|film|surveil)\w*)\b",
)
_STUB = "This robot sounds like it's meant to hurt, threaten or restrain people or animals. Morph doesn't design that."


@dataclass
class Slot:
    value: object = None
    source: str = "default"
    confidence: float = 1.0
    evidence: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Slot":
        d = d or {}
        return cls(value=d.get("value"), source=d.get("source") if d.get("source") in SOURCES else "default",
                   confidence=float(d.get("confidence", 1.0)), evidence=str(d.get("evidence", ""))[:200])


@dataclass
class Brief:
    id: str
    request: str = ""
    status: str = "asking"
    round: int = 0
    turns: list = field(default_factory=list)
    route: dict = field(default_factory=lambda: {"value": "new_robot", "confidence": 1.0, "source": "rules"})
    archetype: dict = field(default_factory=lambda: {"key": "generic_arm", "confidence": 0.0, "source": "rules"})
    slots: dict = field(default_factory=dict)          # name -> Slot
    assumptions: list = field(default_factory=list)    # {text, kind: default|inferred|repair|scope, slot?, alternatives?}
    scope: dict = field(default_factory=lambda: {"status": "ok", "notes": [], "nearest": None})
    preflight: dict = field(default_factory=lambda: {"issues": [], "ms": 0})
    changes: dict = field(default_factory=dict)
    project: str | None = None
    rev: str | None = None
    questions: list = field(default_factory=list)      # the open round
    asked: list = field(default_factory=list)          # slots already asked
    hints: dict = field(default_factory=dict)          # design keys the user named ("6 axis", "Dynamixel")
    flags: dict = field(default_factory=dict)          # oos [[kind, evidence]], implicit [notes], harm, notes
    name: str = ""
    summary: str = ""
    estimate: dict | None = None
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def get(self, name: str, default=None):
        s = self.slots.get(name)
        return s.value if s is not None and s.value is not None else default

    def to_dict(self) -> dict:
        d = asdict(self)
        d["slots"] = {k: asdict(v) for k, v in self.slots.items()}
        d["card"] = card(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Brief":
        d = dict(d or {})
        known = {f for f in cls.__dataclass_fields__}
        b = cls(**{k: v for k, v in d.items() if k in known and k != "slots"})
        b.slots = {k: Slot.from_dict(v) for k, v in (d.get("slots") or {}).items() if k in SLOTS}
        if b.status not in STATUSES:
            b.status = "asking"
        return b


def new_id() -> str:
    return secrets.token_hex(8)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ------------------------------------------------------------------ objects
def object_value(key: str, **over) -> dict:
    """Slot value for a catalog or preset object (presets carry intent.OBJECTS values: an egg is 25 N)."""
    if key in OBJECTS:
        o = {"key": key, "name": key, **OBJECTS[key]}
    elif key in CATALOG:
        o = {"key": key, **{k: v for k, v in CATALOG[key].items() if k not in ("slug", "label")}}
    else:
        raise KeyError(key)
    o.update({k: v for k, v in over.items() if v is not None})
    return o


def unfit_note(o: dict) -> str:
    """Why a named preset is outside the gripper's band, in the words of the limit it breaks."""
    lab, w, m = PRESET_INFO.get(o.get("key"), (None, _plural(str(o.get("name") or "object")).capitalize()))[1], \
        float(o.get("diameter_mm", 0)), float(o.get("mass_g", 0))
    lo = cap.min_width(o.get("shape", "cylinder"), "fsr402")
    why = [f"too heavy: a desk arm here lifts {cap.MAX_MASS_G} g at most" if m > cap.MAX_MASS_G else "",
           f"too narrow: this gripper closes on {lo:g} mm and up" if w < lo else "",
           f"too wide: this gripper opens around {cap.MAX_WIDTH_MM:g} mm at most" if w > cap.MAX_WIDTH_MM else ""]
    return f"{lab} ({w:g} mm, {m:g} g) are " + " and ".join(x for x in why if x) + "."


def fits(o: dict) -> bool:
    """Inside the gripper's band (26-75 mm wide; boxes from 21.5 mm) and the heaviest payload (500 g)."""
    return (cap.min_width(o.get("shape", "cylinder"), "fsr402") <= float(o.get("diameter_mm", 0)) <= cap.MAX_WIDTH_MM
            and float(o.get("mass_g", 0)) <= cap.MAX_MASS_G)


def _plural(w: str) -> str:
    w = w.strip() or "object"
    if re.search(r"(s|x|ch|sh)$", w):
        return w + "es" if not w.endswith("s") else w
    return w[:-1] + "ies" if re.search(r"[^aeiou]y$", w) else w + "s"


def _singular(w: str) -> str:
    if re.search(r"(?:ch|sh|x|ss)es$", w) or (len(w) > 5 and w.endswith("oes")):
        return w[:-2]
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    return w[:-1] if w.endswith("s") and not w.endswith("ss") and len(w) > 3 else w


_NOUN_STOP = {"each", "per", "and", "the", "for", "with", "from", "into", "onto", "that", "than", "less", "more", "max", "maximum",
              "minimum", "min", "object", "objects", "thing", "things", "item", "items", "stuff", "piece", "pieces", "part", "parts",
              "load", "loads", "payload", "weight", "weights", "every", "one", "ones", "robot", "arm", "gripper", "budget", "total",
              "heavy", "light", "wide", "long", "tall", "high", "thick", "across", "diameter", "maybe", "about", "around"}
_MATERIAL = r"(?:glass |plastic |metal |wooden |paper )?"
_NOUN_PATTERNS = (  # the thing a custom object is called: "450 g candles", "candles of 450 g", "picks up glass cups"
    r"\d(?:\.\d+)?\s*(?:g|mm)\s+(?:(?:heavy|light|wide|big|small|large|full|empty)\s+)?(" + _MATERIAL + r"[a-z][a-z-]{2,20})\b",
    r"\b([a-z][a-z-]{2,20})\s+(?:of|weighing|that weigh)\s+(?:about |around |up to )?\d+(?:\.\d+)?\s*(?:g|mm)\b",
    r"\b(?:pick(?:s|ing)?(?: up)?|move[sd]?|moving|handles?|carr(?:y|ies)|sorts?|stacks?|lifts?|grabs?)\s+(?:up\s+)?"
    r"(?:the |some |my |our |small |big )?(" + _MATERIAL + r"[a-z][a-z-]{2,20})\b",
)


def _noun(t: str) -> str | None:
    """What an object the presets don't know is called, singular ("450 g candles" -> "candle"), or None."""
    for pat in _NOUN_PATTERNS:
        for m in re.finditer(pat, t):
            words = m.group(1).split()
            if words[-1] not in _NOUN_STOP and not words[-1].isdigit():
                return " ".join(words[:-1] + [_singular(words[-1])])[:24]
    return None


def _object_label(o: dict, short: bool = False) -> str:
    k = o.get("key")
    base = (PRESET_INFO.get(k, (None, None))[1] if k in OBJECTS else (CATALOG.get(k) or {}).get("label")) or \
        _plural(str(o.get("name") or "object")).capitalize()
    if short:
        return base
    dims = f"{'Ø' if o.get('shape') != 'box' else ''}{o.get('diameter_mm', 0):g} mm, {o.get('mass_g', 0):g} g"
    return f"{base} ({dims})"


def _slug_of(o: dict) -> str:
    k = o.get("key")
    if k in OBJECTS:
        return PRESET_INFO[k][0]
    if k in CATALOG:
        return CATALOG[k]["slug"]
    return re.sub(r"[^a-z0-9]+", "-", str(o.get("name") or "object").lower()).strip("-")[:16] or "object"


def sanitize_object(v) -> dict | None:
    """A model's or client's object proposal -> a bounded object dict (presets win; units are mm and g)."""
    if isinstance(v, str):
        k = v.strip()
        if k in OBJECTS or k in CATALOG:
            return object_value(k)
        a = object_alias(k.lower())
        return object_value(a) if a else None
    if not isinstance(v, dict):
        return None
    k = v.get("key")
    if isinstance(k, str) and (k in OBJECTS or k in CATALOG):
        base = object_value(k)
    else:
        alias = object_alias(str(v.get("name", "")).lower())
        base = object_value(alias) if alias else {"key": None, "name": "object", "shape": "cylinder", "diameter_mm": 45.0,
                                                    "length_mm": 60.0, "mass_g": 100.0, "friction": 0.5, "max_force_n": 0.0}
    out = dict(base)
    name = re.sub(r"[^\w .,'()/+-]", "", str(v.get("name") or base["name"]))[:60].strip()
    out["name"] = name or base["name"]
    if v.get("shape") in cap.SHAPES:
        out["shape"] = v["shape"]
    for k2, (lo, hi) in (("diameter_mm", (1.0, 1000.0)), ("length_mm", (1.0, 2000.0)), ("mass_g", (0.1, 100000.0)),
                         ("friction", (0.1, 1.2)), ("max_force_n", (0.0, 100.0))):
        n = cap._num(v.get(k2))
        if n is not None and not (base.get("key") in OBJECTS and k2 in ("friction", "max_force_n")):
            out[k2] = float(min(max(n, lo), hi))
    return out


# ------------------------------------------------------------------ rules: read a message
def harmful(text: str) -> str | None:
    t = (text or "").lower()
    for pat in HARM:
        m = re.search(pat, t)
        if m:
            return m.group(0)
    return None


def adjusted_kinds(text: str) -> list:
    """[[kind, words]] for the jobs in ADJUSTED a request asks for (unscrewing says it all for an opening that twists)."""
    t = (text or "").lower()
    found = [[kind, m.group(0)] for kind, pat, _why in ADJUSTED for m in [re.search(pat, t)] if m]
    if any(k == "twisting" for k, _ in found):
        found = [f for f in found if f[0] != "opening"]
    return found


def out_of_scope_kinds(text: str) -> list:
    t = (text or "").lower()
    found = []
    for kind, pat, _why in OUT_OF_SCOPE:
        m = re.search(pat, t)
        if m and not (kind == "drone" and re.search(r"\bfruit fl(?:y|ies)\b", t)):
            found.append([kind, m.group(0)])
    return found


def detect_archetype(text: str) -> tuple[str, float]:
    t = (text or "").lower()
    scores = {}
    for a in ARCHETYPES.values():
        s = sum(w * len(re.findall(p, t)) for p, w in a.keywords)
        if s:
            scores[a.key] = s
    if not scores:
        return "generic_arm", 0.3
    best = max(scores, key=lambda k: (scores[k], -list(ARCHETYPES).index(k)))
    total = sum(scores.values())
    return best, round(min(0.95, 0.45 + 0.5 * scores[best] / total * min(scores[best], 4) / 4), 2)


def _num_after(pat: str, t: str):
    m = re.search(pat, t)
    return float(m.group(1)) if m else None


_SPECIFIC = ((r"\b(?:50 ?ml|falcon|centrifuge) tubes?\b", "tube_50ml"), (r"\bvials?\b", "glass_vial"),
             (r"\btube (?:carriers?|racks?)\b", "tube_carrier"), (r"\bsocks?\b", "socks"),
             (r"\b(?:plush\w*|stuffed animals?|teddy)\b", "plush_toy"), (r"\bwashcloths?\b|\btowels?\b", "washcloth"),
             (r"\bduplo\b", "duplo"), (r"\bparts? bins?\b", "parts_bin"), (r"\bfittings?\b", "large_fitting"),
             (r"\bscrewdrivers?\b", "screwdriver"), (r"\bpliers\b", "pliers"), (r"\b(?:hex|allen) keys?\b", "hex_case"),
             (r"\bespresso cups?\b", "espresso_cup"), (r"\bramekins?\b", "ramekin"), (r"\bglue sticks?\b", "glue_stick"),
             (r"\bstaplers?\b", "stapler"), (r"\bpen (?:cups?|holders?)\b", "pen_cup"), (r"\b330 ?ml\b", "bottle_330"),
             (r"\b(?:empty|plastic) bottles?\b", "empty_bottle"), (r"\bempty (?:drink |soda )?cans?\b", "empty_can"),
             (r"\bjars?\b", "jar"), (r"\bpill (?:bottles?|boxe?s?|organi[sz]ers?)\b", "pill_bottle"),
             (r"\bwatering (?:can|bottle)s?\b", "watering_bottle"), (r"\bspray bottles?\b", "spray_bottle"),
             (r"\b(?:cups? of (?:cat |dog |pet )?food|kibble|food cups?)\b", "food_cup"), (r"\btreat balls?\b", "treat_ball"))


def parse(text: str) -> dict:
    """Offline extraction: keywords, numbers and units only. Values here are what the user said (source user),
    except the layout, which is inferred from where things go."""
    raw = (text or "")[:2000]
    t = normalize_units(raw.lower())
    out = {"slots": {}, "inferred": {}, "hints": {}, "implicit": [], "oos": out_of_scope_kinds(t), "harm": harmful(t), "said": {},
           "adjusted": adjusted_kinds(t)}
    s, inf = out["slots"], out["inferred"]
    key, conf = detect_archetype(t)
    out["archetype"] = (key, conf)
    # object: a catalog object, a preset, a generic word ("boxes", "blocks", never the destination), or numbers
    obj = None
    preset = object_alias(t)
    for pat, k in _SPECIFIC:
        if re.search(pat, t):
            obj = object_value(k)
            break
    if obj is None and preset:
        obj = object_value(preset)
    if obj is None:
        for pat, k in ((r"\b(?:toy |wooden )?blocks?\b", "toy_block"), (r"\b(?:small )?box(?:es)?\b", "small_box")):
            if any(not re.search(r"\b(?:into|in|onto|to|on)\s+(?:a |an |the |my |your )?(?:small |wooden )?$", t[max(0, m.start() - 14):m.start()])
                   for m in re.finditer(pat, t)):
                obj = object_value(k)
                break
    if obj is not None and key == "containers" and obj.get("key") == "soda can" and re.search(r"recycl", t):
        obj = object_value("empty_can")
    unfit = obj is not None and obj.get("key") in OBJECTS and not fits(obj)  # a grape, a marker, a full water bottle
    if obj is None or unfit:
        for pat, alt, note in IMPLICIT:
            if re.search(pat, t):
                out["implicit"].append({"note": note, "alt": alt})
                obj, unfit = None, False
                break
    if unfit:
        out["implicit"].append({"note": unfit_note(obj), "alt": None})
        obj = None
    dia = None
    m = re.search(r"(?:ø|⌀|diameter(?: of)?|dia\.?)\s*(\d+(?:\.\d+)?)\s*mm|(\d+(?:\.\d+)?)\s*mm\s*(?:(?:glass|plastic|metal|wooden|paper)\s+)?"
                  r"(?:wide|across|diameter|dia\b|thick|"
                  r"ball|sphere|cube|box(?:es)?|block|brick|cylinder|tubes?|objects?|cans?|bottles?|jars?|cups?|parts?)|"
                  r"\b(?:about|around|roughly|approximately|~)\s*(\d+(?:\.\d+)?)\s*mm\b(?!\s*(?:away|apart|from|reach|long|high|tall))", t)
    if m and not re.search(r"reach|finger|hole|mount|spacing|longer|shorter|apart|away|from the base", t[max(0, m.start() - 24):m.end()]):
        dia = float(m.group(1) or m.group(2) or m.group(3))
    mass = None
    mm = re.search(r"(\d+(?:\.\d+)?)\s*g\b", t)
    if mm and not re.search(r"\bg-?force|\bgs\b", t):
        mass = float(mm.group(1))
    if dia is not None or mass is not None:
        if obj is None:
            obj = {"key": None, "name": "object", "shape": "cylinder", "diameter_mm": 45.0, "length_mm": 60.0,
                   "mass_g": 100.0, "friction": 0.5, "max_force_n": 0.0}
            if re.search(r"\b(box(?:es)?|cubes?|blocks?|bricks?|cartons?|packages?|parcels?)\b", t):
                obj.update(shape="box", name="box")
            elif re.search(r"\b(balls?|spheres?|fruits?|oranges?|apples?)\b", t):
                obj.update(shape="sphere", name="ball")
            elif re.search(r"\b(dumbbells?|weights?)\b", t):
                obj.update(name="dumbbell")
            noun = _noun(t)
            if noun:
                obj["name"] = noun
        obj = dict(obj)
        if dia is not None:
            obj["diameter_mm"] = dia
            obj["length_mm"] = max(obj.get("length_mm", dia), dia)
        if mass is not None:
            obj["mass_g"] = mass
        obj["stated"] = [k for k, v in (("diameter_mm", dia), ("mass_g", mass)) if v is not None]
    if obj is not None:
        s["object"] = obj
    # fragility (the words it came from are kept: a damage force they set is explained in those words)
    for pat, frag in ((r"\b(fragile|delicate|without (?:crushing|damaging|bruising|breaking|cracking)|bruis\w+|squash\w*|crush\w*|"
                       r"crack\w*)\b", "fragile"), (r"\b(gentle|gently|careful(?:ly)?|soft grip)\b", "care"),
                      (r"\b(robust|sturdy|tough|unbreakable|heavy[- ]duty)\b", "robust")):
        m = re.search(pat, t)
        if m:
            s["fragility"] = frag
            out["said"]["fragility"] = m.group(0)
            break
    # layout (inferred: the coordinates are always an assumption)
    lr = re.search(r"\b(left|right)(?:[- ]hand)?(?:\s+side)?\s+(?:to|into|onto|over to|across to)\s+(?:the\s+|its\s+|my\s+)?"
                   r"(left|right)\b", t)
    if lr and lr.group(1) != lr.group(2):  # "from the left side to the right side": the way the person said it
        inf["layout"] = "left_to_right" if lr.group(1) == "left" else "side_to_side"
    elif re.search(r"\b(side to side|one side to the other)\b", t):
        inf["layout"] = "side_to_side"
    elif re.search(r"\bfrom one (?:cup|bin|box|spot|tray|place|rack) to (?:another|the other)\b|"
                   r"\bbetween (?:two )?(?:racks|trays|spots|cups|bins|stations)\b", t):
        inf["layout"] = "short_hop"
    elif re.search(r"\b(?:in|into|onto) (?:a |the |my |an )?(?:bins?|boxe?s?|cartons?|punnets?|trays?|racks?|baskets?|bowls?|crates?|"
                   r"containers?)\b", t):
        inf["layout"] = "tray_to_bin"
    # throughput
    per_min = _num_after(r"(\d+(?:\.\d+)?)\s*(?:per|a|an|/|every)\s*min(?:ute)?s?\b", t)
    per_hour = _num_after(r"(\d+(?:\.\d+)?)\s*(?:per|an|a|/)\s*h(?:ou)?r\b", t)
    every = _num_after(r"every\s*(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?\b", t) or \
        _num_after(r"(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?\s*(?:each|per (?:pick|item|cycle))", t)
    if per_min:
        s["throughput"] = 60.0 / per_min
    elif per_hour:
        s["throughput"] = 3600.0 / per_hour
    elif every:
        s["throughput"] = every
    elif re.search(r"\b(fast|quickly|quick|high[- ]speed|rapid\w*)\b", t):
        inf["throughput"] = 4.0
    # where and who
    for pat, env in ((r"\blab(?:s|oratory)?\b", "lab"), (r"\b(kitchen|counter ?top|counter)\b", "kitchen"),
                     (r"\b(workshop|garage|workbench|work bench|shop floor|factory)\b", "workshop"),
                     (r"\b(classroom|school)\b", "classroom"), (r"\b(office|desk)\b", "office")):
        if re.search(pat, t):
            s["environment"] = env
            break
    near = r"(?:near(?:by)?|next to|around|beside|alongside|close (?:by|to))"
    if re.search(r"\b(?:nobody|no ?one|no-one|no people|no humans?|no kids|no children)\b(?:\s+\w+){0,3}?\s*" + near + r"\b", t):
        s["people_nearby"] = "no"   # "nobody nearby", "no one works next to it"
    elif re.search(r"\b(kids?|children|child|toddlers?|my son|my daughter)\b", t):
        s["people_nearby"] = "children"
    elif re.search(r"\b(next to me|beside me|near me|with people|around people|coworkers?|colleagues?|hands? me|pass(?:es)? me|"
                   r"while i work|people around)\b", t) or \
            re.search(r"\b(?:people|someone|staff|workers|operators|users|humans|others)\b(?:\s+\w+){0,3}?\s*" + near + r"\b", t) or \
            re.search(r"\b" + near + r"\s+(?:to\s+)?(?:people|staff|workers|operators|users|humans|someone|others|us)\b", t):
        s["people_nearby"] = "yes"  # "people nearby", "people work next to it", "near people"
    elif re.search(r"\b(alone|unattended|on its own|by itself|enclosed|fenced)\b", t):
        s["people_nearby"] = "no"
    elif re.search(PERSON_NEAR, t):
        inf["people_nearby"] = "yes"  # "help my grandma", "pass my dad his pills", "for me": that person is next to it
    # precision
    if re.search(r"(?:±|\+/-|plus or minus)\s*(?:0?\.\d+|1)\s*mm|\bsub[- ]?mm|\bsub[- ]?millimet\w*|\bvery (?:precise|accurate)\b|"
                 r"\b1 mm (?:accuracy|precision)\b", t):
        s["precision"] = "very_tight"
    elif re.search(r"\b(precise|precision|accurate|accuracy|repeatab\w*)\b|(?:±|\+/-)\s*2\s*mm", t):
        s["precision"] = "tight"
    elif re.search(r"\b(rough(?:ly)?|approximate(?:ly)?)\s+(?:placed|placement|position\w*)\b", t):
        s["precision"] = "loose"
    # budget
    b = re.search(r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(k\b)?|(\d[\d,]*)\s*(?:dollars|usd|bucks)\b", t)
    if b:
        v = float((b.group(1) or b.group(3)).replace(",", "")) * (1000 if b.group(2) else 1)
        if 20 <= v <= 1_000_000:
            s["budget"] = v
    elif re.search(r"\b(cheap(?:est)?|inexpensive|low[- ]cost|affordable|on a budget|lowest cost)\b", t):
        s["budget"] = 300.0
    elif re.search(r"\b(premium|industrial[- ]grade|high[- ]end|best possible)\b", t):
        s["budget"] = 2500.0
    # perception and connectivity
    if re.search(r"\b(no|without)( a)? camera\b", t):
        s["perception"] = "none"
    elif re.search(r"\b(camera|vision|computer vision|by colou?r|by size|detects?|recogni[sz]\w*|identif\w*)\b", t):
        s["perception"] = "wrist_camera"
    if re.search(r"\b(wi-?fi|wireless|from my phone|over the network|remote(?:ly)? control\w*|bluetooth)\b", t):
        s["connectivity"] = "wifi"
    # quantity
    q = re.search(r"(\d[\d,]*)\s*(?:units?|robots?|cop(?:y|ies)|pcs|pieces|of them|arms)\b", t)
    if q:
        s["quantity"] = int(q.group(1).replace(",", ""))
    elif re.search(r"\b(mass[- ]produc\w*|production run|to sell|for sale|as a product|thousands)\b", t):
        s["quantity"] = 1000
    elif re.search(r"\b(small batch|a few of them|pilot run)\b", t):
        s["quantity"] = 10
    # existing hardware
    if re.search(r"\bso[- ]?10[01]\b|\ble ?robot\b", t):
        s["existing_hardware"] = "so101"
    # tasks
    if re.search(r"\bsort(?:s|ing|er)?\b", t):
        s["task"] = "sort"
    elif re.search(r"\bhand[- ]?over\b", t) or re.search(HAND_TO_PERSON, t):
        s["task"] = "hand_over"
    elif any(k == "assembly" for k, _ in out["adjusted"]):
        inf["task"] = "pick_place"    # the parts are moved to where they go, not sorted
    # design keys named outright
    h = out["hints"]
    m = re.search(r"\b([456])\s*-?\s*(?:dof|axis|axes|joints?|degrees? of freedom)\b", t) or \
        re.search(r"\b(four|five|six)\s*-?\s*(?:dof|axis|axes|joints?|degrees? of freedom)\b", t)
    if m:
        h["arm.dof"] = {"four": 4, "five": 5, "six": 6}.get(m.group(1)) or int(m.group(1))
    if re.search(r"\b(dynamixel|robotis|xl430|xm430)\b", t):
        h["arm.actuator_family"] = "dynamixel"
    elif re.search(r"\b(feetech|sts3215|smart(?:[- ]bus)? servos?|serial bus|bus servos?)\b", t):
        h["arm.actuator_family"] = "smart_bus"
    elif re.search(r"\b(hobby servos?|hobby[- ]grade|pwm servos?|rc servos?|mg996r|ds3218|sg90)\b", t):
        h["arm.actuator_family"] = "hobby_pwm"
    if re.search(r"\b(custom pcb|custom board|custom controller|pcba)\b", t):
        h["electronics.config"] = "custom_pcb"
    if re.search(r"\b(e-?stop|emergency stop|kill switch)\b", t):
        h["electronics.extras+"] = "estop"
    if re.search(r"\bimu\b", t):
        h["electronics.extras+"] = (h.get("electronics.extras+", "") + ",imu").strip(",")
    if re.search(r"\b(pilot build|cnc|sheet[- ]metal|machined)\b", t):
        h["build.config"] = "pilot"
    elif re.search(r"\b(injection[- ]mou?lded|mou?lded|production build)\b", t):
        h["build.config"] = "production"
    out["route"] = route_rules(raw)
    return out


def route_rules(text: str) -> dict:
    """new_robot | edit_current | question | import_robot | other, from wording alone (for chat triage)."""
    t = (text or "").lower().strip()
    if re.search(r"\b(import|urdf|mjcf|github\.com|huggingface|koch|my own (?:arm|robot) design)\b", t):
        return {"value": "import_robot", "confidence": 0.7, "source": "rules"}
    if re.search(r"\b(new robot|a robot (?:that|to|for|which)|robot arm (?:that|to|for)|build (?:me )?a|make me a|i need a|i want a|design (?:me )?a|"
                 r"something (?:to|that)|an? (?:arm|machine) (?:that|to|for)|so[- ]?10[01]|le ?robot|"
                 r"to (?:pick|sort|move|carry|hand|place|feed|water|load|stack)\b)", t):
        return {"value": "new_robot", "confidence": 0.8, "source": "rules"}
    if re.match(r"(?:can|could|does|do|is|are|what|how|why|which|will|would)\b", t) and t.endswith("?"):
        return {"value": "question", "confidence": 0.7, "source": "rules"}
    if re.match(r"(?:make it|use|add|swap|switch|change|increase|decrease|reduce|remove|set|go back|undo|try)\b", t):
        return {"value": "edit_current", "confidence": 0.7, "source": "rules"}
    if len(t.split()) <= 2 and re.match(r"(?:hi|hello|hey|thanks|thank you|ok|yes|no)\b", t):
        return {"value": "other", "confidence": 0.6, "source": "rules"}
    return {"value": "new_robot", "confidence": 0.5, "source": "rules"}


# ------------------------------------------------------------------ slots
def set_slot(b: Brief, name: str, value, source: str = "user", confidence: float = 1.0, evidence: str = "") -> bool:
    """Write a slot unless a stronger source already filled it (a model never overwrites what the user said)."""
    if name not in SLOTS or value is None:
        return False
    cur = b.slots.get(name)
    if cur is not None and RANK.get(cur.source, 0) > RANK.get(source, 0):
        return False
    b.slots[name] = Slot(value, source, round(float(confidence), 2), str(evidence)[:200])
    return True


def filled(b: Brief, name: str) -> bool:
    s = b.slots.get(name)
    return s is not None and s.value is not None and s.source != "default" and (s.source != "inferred" or s.confidence >= 0.6)


def coerce(name: str, value):
    """Validate one slot value from a chip, a model or a client; None when it is not acceptable."""
    if name == "object":
        return sanitize_object(value)
    enums = {"task": TASKS, "fragility": FRAGILITY_N, "layout": LAYOUTS, "environment": ENVIRONMENTS, "people_nearby": PEOPLE,
             "precision": PRECISION, "perception": ("none", "wrist_camera"), "connectivity": ("none", "wifi"),
             "existing_hardware": ("none", "so101")}
    if name in enums:
        return value if isinstance(value, str) and value in enums[name] else None
    n = cap._num(value)
    if n is None:
        return None
    if name == "throughput":
        return float(min(max(n, cap.CYCLE_S[0]), cap.CYCLE_S[1]))
    if name == "budget":
        return float(min(max(n, 50.0), 1_000_000.0))
    if name == "quantity":
        return int(min(max(round(n), cap.QUANTITY[0]), cap.QUANTITY[1]))
    return None


def set_archetype(b: Brief, key: str, confidence: float, source: str):
    if key in ARCHETYPES:
        b.archetype = {"key": key, "confidence": round(float(confidence), 2), "source": source}


def _archetype_for_object(o: dict) -> str | None:
    k = (o or {}).get("key")
    for a in ARCHETYPES.values():
        if a.key not in ("generic_arm", "so101") and (k in a.objects or k in a.more):
            return a.key
    return None


def merge(b: Brief, parsed: dict, source: str = "user", evidence: str = ""):
    """Apply a parse() result: user-stated slots, inferred layout, design hints, scope flags."""
    said = parsed.get("said") or {}
    for name, v in parsed.get("slots", {}).items():
        v2 = coerce(name, v)
        if v2 is not None:
            if name == "object" and isinstance(v, dict) and v.get("stated"):
                v2["stated"] = list(v["stated"])
            set_slot(b, name, v2, source, 1.0, f"{evidence}: {said[name]}" if name in said else evidence)
    for name, v in parsed.get("inferred", {}).items():
        v2 = coerce(name, v)
        if v2 is not None:
            set_slot(b, name, v2, "inferred", 0.7, evidence)
    for k, v in parsed.get("hints", {}).items():
        if k == "electronics.extras+":
            b.hints[k] = ",".join(dict.fromkeys((b.hints.get(k, "") + "," + v).strip(",").split(",")))
        else:
            b.hints[k] = v
    fl = b.flags
    for kind in parsed.get("oos") or []:
        if kind not in fl.setdefault("oos", []):
            fl["oos"].append(kind)
    for imp in parsed.get("implicit") or []:
        if imp not in fl.setdefault("implicit", []):
            fl["implicit"].append(imp)
    for kind in parsed.get("adjusted") or []:
        if kind[0] not in [k for k, _ in fl.setdefault("adjusted", [])]:
            fl["adjusted"].append(kind)
    if parsed.get("harm"):
        fl["harm"] = parsed["harm"]


def _arch(b: Brief) -> Archetype:
    return ARCHETYPES.get(b.archetype.get("key"), ARCHETYPES["generic_arm"])


def _default_object(b: Brief) -> dict:
    a = _arch(b)
    for imp in b.flags.get("implicit") or []:
        if imp.get("alt") in a.objects + a.more:
            return object_value(imp["alt"])
    return object_value(a.objects[0])


def defaults(b: Brief) -> dict:
    """What every slot falls back to for this brief's archetype."""
    a = _arch(b)
    o = b.get("object") or _default_object(b)
    task = a.task
    if b.get("task") == "sort" or a.task == "sort":
        task = "sort"
    doing = b.get("task") or task   # what it does: a sorting archetype asked to hand things over gets no camera
    return {"task": task, "object": o, "fragility": "fragile" if o.get("max_force_n", 0) and o["max_force_n"] <= 5 else
            "care" if o.get("max_force_n", 0) else "robust",
            "layout": a.layout, "throughput": a.throughput, "environment": a.environment,
            "people_nearby": "yes" if doing == "hand_over" and a.people == "no" else a.people,
            "precision": "loose", "budget": 500.0,
            "perception": "wrist_camera" if doing == "sort" else "none" if a.task == "sort" else a.perception,
            "connectivity": "none", "quantity": 1,
            "existing_hardware": "so101" if a.key == "so101" else "none"}


def value(b: Brief, name: str):
    v = b.get(name)
    return v if v is not None else defaults(b)[name]


def specificity(b: Brief) -> int:
    """How much the user pinned down: stated slots plus named design keys (task and fragility come free)."""
    n = sum(1 for k in SLOTS if k not in ("task", "fragility") and filled(b, k))
    return n + min(len(b.hints), 2)


# ------------------------------------------------------------------ questions
def _chip(label, value, default=False, note=""):
    c = {"label": label, "value": value, "ok": True}
    if default:
        c["default"] = True
    if note:
        c["note"] = note
    return c


def make_question(b: Brief, slot: str) -> dict:
    a, d = _arch(b), defaults(b)
    things = a.things
    they = "they" if things.endswith("s") else "it"  # "the bottle" and "the food": where does it go
    if filled(b, "object"):  # "Where does it pick up the strawberries, and where do they go?"
        lab = _object_label(b.get("object"), short=True)
        things = "the " + re.sub(r"^(?:a|an) ", "", lab.lower())
        they = "it" if re.match(r"(?:a|an) ", lab.lower()) else "they"
    if slot == "object":
        dk = d["object"].get("key")
        keys = list(a.objects[:4]) if dk not in a.more else [dk, *a.objects[:3]]
        chips = [_chip(_object_label(object_value(k)), k, k == dk) for k in keys]
        return {"slot": "object", "text": a.object_q, "required": True, "chips": chips,
                "why": "The gripper and the arm are sized around one object: 26 to 75 mm wide, up to 500 g."}
    spec = {
        "task": ("What should it do with them?", "Sorting adds a wrist camera; handing things over adds an emergency stop.",
                 [_chip("Pick and place", "pick_place"), _chip("Sort into bins", "sort"), _chip("Hand things to me", "hand_over")]),
        "layout": (f"Where does it pick up {things}, and where do{'es' if they == 'it' else ''} {they} go?",
                   "Sets the pick and place points the arm is sized for.",
                   [_chip(_layout(b, k)["label"], k) for k in LAYOUTS if not LAYOUTS[k].get("typed")]),
        "throughput": ("How fast does it need to work?", "Sets the cycle time in the simulation; about 4 s is the fastest.",
                       [_chip("One every 10 s", 10.0), _chip("One every 6 s", 6.0), _chip("As fast as it can, about 4 s", 4.0)]),
        "people_nearby": ("Will people work right next to it?", "People, children or pets nearby add an emergency stop.",
                          [_chip("It works on its own", "no"), _chip("People next to it", "yes"),
                           _chip("Children or pets nearby", "pets" if a.key == "pet_feeding" else "children")]),
        "precision": ("How precisely does it need to place them?", "Tighter placement needs servos with position feedback.",
                      [_chip("Roughly, ±5 mm", "loose"), _chip("Precisely, ±2 mm", "tight"),
                       _chip("Very precisely, ±1 mm", "very_tight", note="Dynamixel servos, from about $700 in parts")]),
        "budget": ("What is the budget for parts?", "Decides between smart-bus and Dynamixel servos.",
                   [_chip("Lowest cost, about $300", 300.0), _chip("Up to $1,000", 1000.0), _chip("Up to $2,500", 2500.0)]),
        "perception": ("Should it have a camera?", "Morph adds the camera hardware and wiring; it does not write vision code.",
                       [_chip("Add a wrist camera", "wrist_camera", note="vision code is not generated"), _chip("No camera", "none")]),
        "connectivity": ("How will you send it commands?", "Wi-Fi needs an ESP32 controller.",
                         [_chip("USB cable", "none"), _chip("Wi-Fi", "wifi")]),
        "quantity": ("How many do you want to build?", "Larger runs move from printed parts to CNC or molded parts.",
                     [_chip("Just one", 1), _chip("A batch of 10", 10), _chip("A pilot run of 100", 100), _chip("1,000 units", 1000)]),
        "fragility": ("How easily is it damaged?", "Sets the most the fingers may squeeze it with.",
                      [_chip("It takes a firm squeeze", "robust"), _chip(f"Handle with care, about {FRAGILITY_N['care']:g} N", "care"),
                       _chip(f"Fragile, about {FRAGILITY_N['fragile']:g} N", "fragile")]),
        "existing_hardware": ("Design a new arm, or use one you have?", "An SO-101 keeps its own servos and gripper.",
                              [_chip("Design a new arm", "none"), _chip("I have an SO-101", "so101")]),
        "environment": ("Where will it work?", "Labs and kitchens add an emergency stop.",
                        [_chip("Home or office desk", "home"), _chip("Kitchen counter", "kitchen"), _chip("Lab bench", "lab"),
                         _chip("Workshop bench", "workshop")]),
    }[slot]
    dv = d[slot]
    chips = [dict(c, default=True) if c["value"] == dv else c for c in spec[2]]
    return {"slot": slot, "text": spec[0], "why": spec[1], "required": False, "chips": chips}


def questions_for(b: Brief) -> list:
    """Code decides when to ask (models only propose slot values): required slots first, then, in the first round
    only, the archetype's optional questions, fewer the more specific the request was."""
    if b.round >= MAX_ROUNDS:
        return []
    a = _arch(b)
    req = [s for s in ("object",) if not filled(b, s)]
    if b.round == 0:
        spec = specificity(b)
        n = 3 if spec == 0 else 2 if spec <= 2 else 1 if spec == 3 or req else 0
        opt = [s for s in a.optional if not filled(b, s) and s not in b.asked]
        slots = (req + opt)[:max(n, len(req))]
    else:
        slots = req
    return [make_question(b, s) for s in slots[:MAX_QUESTIONS]]


# ------------------------------------------------------------------ scope
NEAREST = {  # out-of-scope kind -> (archetype, slot overrides, the robot offered instead)
    "mobile": ("toys", {"object": "toy_block"}, "a desk arm that tidies small things (toy blocks, up to 40 mm) into a bin"),
    "drone": ("generic_arm", {"object": "small_box"}, "a desk arm that picks up small boxes (45 mm, 100 g) and places them"),
    "humanoid": ("soft_goods", {"object": "socks"}, "one arm with a two-finger gripper that sorts rolled socks into bins"),
    "suction": ("generic_arm", {"object": "small_box"}, "a two-finger gripper arm that picks up small boxes (45 mm, 100 g)"),
    "pouring": ("containers", {"object": "bottle_330"}, "an arm that carries a 330 ml bottle between two spots on the table"),
    "folding": ("soft_goods", {"object": "washcloth"}, "an arm that moves folded washcloths into a basket (it doesn't fold them)"),
    "cooking": ("fragile_food", {"object": "raw egg"}, "an arm that moves eggs from a tray into a bowl"),
    "above_table": ("generic_arm", {"object": "small_box"}, "an arm that sets small boxes down side by side on the table"),
}


def _numeric_fix(b: Brief) -> tuple[list, dict | None]:
    """Stated sizes and weights outside the envelope: (notes, the nearest object that fits) for user-given numbers."""
    o = b.slots.get("object")
    if o is None or not o.value or o.source not in ("user", "video", "photo"):
        return [], None
    ov, notes, fix = o.value, [], {}
    shape = ov.get("shape", "cylinder")
    lo = cap.min_width(shape, "fsr402")
    if ov["diameter_mm"] < lo:
        notes.append(f"{ov['diameter_mm']:g} mm is narrower than the {lo:g} mm this gripper closes on.")
        fix["diameter_mm"] = max(lo, 30.0) if shape != "box" else max(lo, 25.0)
    if ov["diameter_mm"] > cap.MAX_WIDTH_MM:
        notes.append(f"{ov['diameter_mm']:g} mm is wider than the {cap.MAX_WIDTH_MM:g} mm this gripper opens around.")
        fix["diameter_mm"] = cap.MAX_WIDTH_MM - 5
    if ov["mass_g"] > cap.MAX_MASS_G:
        notes.append(f"{ov['mass_g']:g} g is more than a desk arm here lifts: {cap.MAX_MASS_G} g at most, with Dynamixel servos and "
                     "4 joints (about $1,500 in parts).")
        fix["mass_g"] = float(cap.MAX_MASS_G)
    if not fix:
        return [], None
    no = {k: v for k, v in dict(ov, **fix).items() if k != "stated"}
    no["length_mm"] = max(min(no.get("length_mm", no["diameter_mm"]), 200.0), no["diameter_mm"])
    return notes, no


def assess_scope(b: Brief) -> dict:
    """{status: ok|adjusted|out_of_scope, notes, nearest}; nearest = {archetype, slots, summary} for out_of_scope."""
    a = _arch(b)
    oos = list(dict.fromkeys(k for k, _ in b.flags.get("oos") or []))
    num_notes, near_obj = _numeric_fix(b)
    if oos or num_notes:
        why = {k: w for k, _p, w in OUT_OF_SCOPE}
        notes = [why[k] for k in oos] + num_notes
        arch, slots = a.key, {}
        if oos:
            arch, over, _ = NEAREST[oos[0]]
            if oos[0] in ("above_table", "suction") and a.key != "generic_arm":
                arch = a.key  # a real desk task asked in an unsupported way: keep it, on the table plane
            slots = dict(over) if arch != a.key else {"object": a.objects[0]}
        o = near_obj or b.get("object")
        if o and fits(o) and arch == a.key:
            slots["object"] = o
        if near_obj and near_obj["mass_g"] > cap.payload_g("smart_bus", 4):
            slots["budget"] = 1500.0
        obj = sanitize_object(slots.get("object") or ARCHETYPES[arch].objects[0])
        dims = f"{obj['diameter_mm']:g} mm, {obj['mass_g']:g} g"
        summary = (f"A desk arm that {_verb(ARCHETYPES[arch])} {_object_label(obj, short=True).lower()} ({dims}) on the table plane"
                   + (", with Dynamixel servos and 4 joints (about $1,500 in parts)" if slots.get("budget") else "") + ".")
        return {"status": "out_of_scope", "notes": notes, "nearest": {"archetype": arch, "slots": slots, "summary": summary}}
    ov = b.get("object")
    if b.get("existing_hardware") == "so101" and ov and ov["mass_g"] > cap.SO101_PAYLOAD_G:
        return {"status": "out_of_scope",
                "notes": [f"The stock SO-101 lifts about {cap.SO101_PAYLOAD_G} g with a 2x torque margin; {ov['mass_g']:g} g needs a "
                          "stronger arm."],
                "nearest": {"archetype": _archetype_for_object(ov) or "generic_arm", "slots": {"existing_hardware": "none"},
                            "summary": f"A new Morph-generated desk arm for the {ov.get('name', 'object')}."}}
    why = {k: w for k, _p, w in ADJUSTED}
    notes = [why[k] for k, _ in b.flags.get("adjusted") or [] if k in why]
    notes += [imp["note"] for imp in b.flags.get("implicit") or []]
    if a.note:
        notes.append(a.note)
    budget = b.slots.get("budget")
    if budget and budget.source == "user" and budget.value < MIN_COST_USD:
        notes.append(f"About ${MIN_COST_USD} in parts is the lowest-cost arm Morph generates (a printed smart-bus arm), above "
                     f"${budget.value:,.0f}.")
    notes += list(b.flags.get("notes") or [])
    return {"status": "adjusted" if notes else "ok", "notes": list(dict.fromkeys(notes)), "nearest": None}


def _verb(a: Archetype) -> str:
    return {"sort": "sorts", "hand_over": "hands you", "transfer": "moves", "dispense": "carries"}.get(a.task, "picks up and places")


def apply_nearest(b: Brief, nearest: dict):
    """Accept the nearest robot: its archetype and slot values, recorded as a scope assumption."""
    set_archetype(b, nearest.get("archetype", "generic_arm"), 1.0, "rules")
    for k, v in (nearest.get("slots") or {}).items():
        v2 = coerce(k, v)
        if v2 is not None:
            b.slots[k] = Slot(v2, "inferred", 1.0, "nearest robot")
    b.flags["oos_accepted"] = [k for k, _ in b.flags.get("oos") or []]
    b.flags["nearest_note"] = nearest.get("summary", "")
    b.flags["oos"] = []


# ------------------------------------------------------------------ the turn
def start(text: str, *, bid: str | None = None) -> Brief:
    """Rules-only first turn (agents.run_turn adds SubQ and Claude around the same steps)."""
    b = Brief(id=bid or new_id(), request=(text or "").strip()[:2000])
    b.turns.append({"role": "user", "text": b.request, "ts": _now_iso()})
    p = parse(b.request)
    if p["harm"]:
        return refuse(b, p["harm"], "rules")
    b.route = p["route"]
    set_archetype(b, *p["archetype"], "rules")
    merge(b, p, "user", "request")
    if b.get("existing_hardware") == "so101":
        set_archetype(b, "so101", 0.9, "rules")
    return progress(b)


def refuse(b: Brief, evidence: str, source: str) -> Brief:
    """Status refused. `source` is who caught it: "rules" (the local filter, before anything was sent anywhere),
    "subq" or "claude" (the request already went to that service; nothing more is sent)."""
    b.status, b.questions, b.changes = "refused", [], {}
    b.flags["harm"], b.flags["harm_by"] = str(evidence)[:80], source
    b.scope = {"status": "out_of_scope", "notes": [_STUB], "nearest": None}
    b.turns.append({"role": "assistant", "text": _STUB, "source": source, "ts": _now_iso()})
    b.updated = time.time()
    return b


_BARE_MM = re.compile(r"(?:about |around |roughly |~)?(\d+(?:\.\d+)?)\s*mm(?:\s+(?:wide|across|in diameter))?")
_BARE_S = re.compile(r"(?:about |around |roughly |~|one every )?(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?(?:\s+each)?")


def _typed_answer(b: Brief, p: dict, t: str):
    """A typed reply to the questions ("6 cm, 200 g, 10 per minute, people nearby"), on top of parse(): a bare length is
    the object's width and a bare time the pace, and sizes alone change the object being discussed instead of replacing
    it with a generic one (strawberries stay strawberries, at 200 g). `t` is the lower-case text with units normalized."""
    s = p["slots"]
    o = s.get("object")
    generic = o is None or (o.get("key") is None and o.get("name") == "object")
    dia = None
    for part in re.split(r"[,;]|\band\b|\bplus\b", t):
        part = part.strip(" .!")
        m = _BARE_MM.fullmatch(part)
        if m and (o is None or "diameter_mm" not in (o.get("stated") or [])):
            dia = float(m.group(1))
        m = _BARE_S.fullmatch(part)
        if m and "throughput" not in s:
            s["throughput"] = float(m.group(1))
    if not generic:  # a named object ("eggs, 50 mm"): the bare width is its width
        if dia is not None:
            o.update(diameter_mm=dia, length_mm=max(float(o.get("length_mm") or 0), dia),
                     stated=sorted(set(o.get("stated") or []) | {"diameter_mm"}))
        return
    if o is None and dia is None:
        return
    base = dict(value(b, "object"))
    stated = set(base.pop("stated", None) or [])
    if o is not None:
        for k in o.get("stated") or []:
            base[k] = o[k]
            stated.add(k)
    if dia is not None:
        base["diameter_mm"] = dia
        stated.add("diameter_mm")
    base["length_mm"] = max(float(base.get("length_mm") or 0), float(base["diameter_mm"]))
    s["object"] = dict(base, stated=sorted(stated))


def answer(b: Brief, answers: dict | None, text: str | None = None) -> list:
    """Apply chip answers ({slot: value}) and an optional free-text answer. Returns notes for what was ignored."""
    notes = []
    for name, v in (answers or {}).items():
        v2 = coerce(name, v) if name in SLOTS else None
        if v2 is None:
            notes.append(f"ignored answer for {name}")
            continue
        set_slot(b, name, v2, "user", 1.0, "answer")
        if name not in b.asked:
            b.asked.append(name)
        if name == "object":
            a2 = _archetype_for_object(v2)
            if _arch(b).key == "generic_arm" and a2:
                set_archetype(b, a2, 0.9, "user")
        if name == "existing_hardware":
            set_archetype(b, "so101" if v2 == "so101" else (_archetype_for_object(b.get("object")) or "generic_arm"), 0.9, "user")
    if text and text.strip():
        t = text.strip()[:2000]
        p = parse(t)
        if p["harm"]:
            refuse(b, p["harm"], "rules")
            return notes
        _typed_answer(b, p, normalize_units(t.lower()))
        keep = {"slots": {k: v for k, v in p["slots"].items() if k in ("object", "throughput", "budget", "quantity", "precision",
                                                                         "fragility", "people_nearby", "perception", "connectivity",
                                                                         "environment", "task")},
                "inferred": p["inferred"], "hints": p["hints"], "oos": p["oos"], "implicit": p["implicit"], "said": p["said"],
                "adjusted": p["adjusted"]}
        merge(b, keep, "user", "answer")
        a2 = _archetype_for_object(b.get("object"))
        if _arch(b).key == "generic_arm" and a2:
            set_archetype(b, a2, 0.9, "user")
    b.turns.append({"role": "user", "answers": dict(answers or {}), "text": (text or "")[:2000], "ts": _now_iso()})
    return notes


def progress(b: Brief, *, defaults_now: bool = False, nearest: bool = False, deep: bool = True,
             home: str | None = None) -> Brief:
    """Decide the next state: refused / out_of_scope / asking (with questions) / ready (mapped and preflighted)."""
    if b.status in ("refused", "building", "built"):
        return b
    sc = assess_scope(b)
    if sc["status"] == "out_of_scope":
        if not (nearest and sc.get("nearest")):
            b.status, b.scope, b.questions = "out_of_scope", sc, []
            b.turns.append({"role": "assistant", "text": " ".join(sc["notes"]), "nearest": (sc.get("nearest") or {}).get("summary"),
                            "ts": _now_iso()})
            b.updated = time.time()
            return b
        prior = sc["notes"]
        apply_nearest(b, sc["nearest"])
        sc = assess_scope(b)
        sc = {"status": "adjusted", "notes": list(dict.fromkeys(prior + sc["notes"])), "nearest": None}
        b.assumptions = [x for x in b.assumptions if x.get("kind") != "scope"]
    b.scope = sc
    qs = [] if (defaults_now or nearest) else questions_for(b)
    if qs:
        b.questions, b.status = qs, "asking"
        b.round += 1
        b.asked += [q["slot"] for q in qs if q["slot"] not in b.asked]
        b.turns.append({"role": "assistant", "questions": [q["text"] for q in qs], "ts": _now_iso()})
        b.updated = time.time()
        return b
    return finalize(b, deep=deep, home=home)


# ------------------------------------------------------------------ slots -> design
def damage_force(b: Brief) -> tuple[float, str]:
    """(N, where it comes from) for the object's damage force. A measured value wins: Morph's preset or catalog object
    (an egg is 25 N), or the object a video, a photo or a model described. A stated category ("fragile", "gently")
    gives one only to an object without one of its own: saying an egg is fragile never makes it a 3 N strawberry,
    which no grip could both hold and spare. `where` finishes "the N N damage force is ..." on the confirm card."""
    o = value(b, "object")
    maxf, key = float(o.get("max_force_n") or 0.0), o.get("key")
    lab = _object_label(o, short=True).lower()
    if maxf > 0:
        if key in OBJECTS:
            return maxf, f"Morph's preset for {lab}"
        if key in CATALOG:
            return maxf, f"Morph's value for {lab}"
        s = b.slots.get("object")
        return maxf, {"video": "from the video", "photo": "from the photo"}.get(s.source if s else "", "the object's estimate")
    fr = b.slots.get("fragility")
    if fr is not None and fr.source in ("user", "video", "photo") and FRAGILITY_N.get(fr.value, 0) > 0:
        return FRAGILITY_N[fr.value], f"Morph's reading of '{_fragility_words(fr)}'"
    return 0.0, ""


def _fragility_words(fr: Slot) -> str:
    """The person's words for a stated fragility ("without cracking"), else the category's own."""
    words = fr.evidence.split(": ", 1)[1] if fr.source == "user" and ": " in fr.evidence else ""
    return words or {"fragile": "fragile", "care": "handle with care", "robust": "robust"}.get(fr.value, str(fr.value))


def _family_label(f: str) -> str:
    return {"hobby_pwm": "hobby PWM", "smart_bus": "smart-bus", "dynamixel": "Dynamixel"}.get(f, f)


def brief_to_changes(b: Brief) -> dict:
    """Deterministic slots -> dotted design keys (complete enough that Design().with_changes(...) is the robot)."""
    o = dict(value(b, "object"))
    key = o.get("key")
    ch = {"name": b.name or "new-robot", "arm.source": "", "arm.dof": 5, "arm.actuator_family": "smart_bus",
          "arm.reach_mm": 320.0, "electronics.config": "modules", "electronics.mcu": "auto", "electronics.servo": "auto",
          "electronics.target_force_n": 0.0, "finger.width_mm": 20.0, "finger.thickness_mm": 6.0, "finger.material": "PETG",
          "fab.process": "auto", "build.config": "prototype", "build.quantity": 1}
    if key in OBJECTS:
        from .intent import object_changes
        ch.update(object_changes(key))
    obj = {k: o[k] for k in ("shape", "diameter_mm", "length_mm", "mass_g", "friction", "max_force_n") if k in o}
    ch.update({f"object.{k}": v for k, v in obj.items()})
    ch["object.name"] = str(o.get("name") or key or "object")[:60]
    ch.update(fit_finger({"diameter_mm": obj.get("diameter_mm", 45.0)}))
    ch["finger.pad"] = "flat" if obj.get("shape") == "box" else "contour"
    maxf = damage_force(b)[0]
    ch["object.max_force_n"] = maxf
    small = obj.get("diameter_mm", 45.0) < cap.min_width(obj.get("shape", "cylinder"), "none")
    sensor = "fsr402" if (maxf > 0 or small) else "none"
    ch["finger.sensor"] = sensor
    if sensor == "fsr402":
        ch.update({"fab.process": "fdm_print", "finger.material": "PETG"})
    lay = LAYOUTS[value(b, "layout")]
    ch.update({"task.pick_x_mm": lay["pick"][0], "task.pick_y_mm": lay["pick"][1], "task.place_x_mm": lay["place"][0],
               "task.place_y_mm": lay["place"][1], "task.cycle_time_s": float(value(b, "throughput"))})
    extras = set()
    if value(b, "people_nearby") in ("yes", "children", "pets") or value(b, "environment") in ("lab", "kitchen", "classroom"):
        extras.add("estop")
    if value(b, "perception") == "wrist_camera":
        extras.add("wrist_camera")
    if value(b, "connectivity") == "wifi":
        extras.add("wifi")
    prec = value(b, "precision")
    if prec == "very_tight":
        ch["arm.actuator_family"] = "dynamixel"
    q = int(value(b, "quantity"))
    ch["build.quantity"] = q
    ch["build.config"] = "production" if q >= 500 else "pilot" if q >= 50 else "prototype"
    if q >= 500:
        ch["electronics.config"] = "custom_pcb"
    if value(b, "existing_hardware") == "so101":   # its stock jaws carry no force sensors (the servo's torque limit grips)
        ch.update({"arm.source": "so101", "electronics.servo": "sts3215", "finger.sensor": "none"})
    for k, v in b.hints.items():
        if k == "electronics.extras+":
            extras |= {e for e in v.split(",") if e}
        elif k in ("arm.dof", "arm.actuator_family", "electronics.config", "build.config"):
            ch[k] = v
    if prec in ("tight", "very_tight") and ch["arm.actuator_family"] == "hobby_pwm":
        b.flags.setdefault("notes", [])
        note = "Hobby PWM servos have no position feedback, so the precise placement you asked for is unlikely."
        if note not in b.flags["notes"]:
            b.flags["notes"].append(note)
    ch["electronics.extras"] = ",".join(e for e in cap.EXTRAS if e in extras)
    ch, _ = cap.clamp(ch, allow=("name", "arm.source"))
    return ch


# ------------------------------------------------------------------ preflight + repair
def _grip_force(gd, a: dict, **drive) -> dict:
    """electronics.plan without the Keewano hold-force query (preflight never waits on the network); `drive` is how the
    gripper servo is driven (pipeline2.gripper_drive: its stall on the servo rail, its torque limit), as the gripper
    stage plans it."""
    from .. import electronics, keewano
    from ..design import FSR

    pad_key = (gd.finger.pad, gd.finger.material)
    configured = os.environ.get("KEEWANO_PAT") and os.environ.get("KEEWANO_PROJECT_ID")
    if not configured or pad_key in getattr(keewano, "_ratio_cache", {}) or gd.electronics.target_force_n:
        return electronics.plan(gd, a, None, **drive)
    hold = electronics.static_hold(gd.object, a["wedge_gain"])
    target = electronics.force_window(gd.object, hold, gd.finger.sensor if gd.finger.sensor in FSR else None)[2]
    return electronics.plan(gd.with_changes({"electronics.target_force_n": round(max(target, 0.01), 3)}), a, None,
                            **drive)


_IMPORTED: dict = {}


def _imported_size(d):
    """imported.size() reloads the robot's URDF (about 1 s); it depends only on the arm, the payload, the wrist camera
    and the servo rail, so memoize."""
    from ..robot import imported

    a = d.arm
    key = (a.source, a.actuator_family, a.actuator_overrides, float(a.payload_margin), float(d.object.mass_g),
           float(d.electronics.servo_v), d.electronics.extras)
    if key not in _IMPORTED:
        if len(_IMPORTED) > 64:
            _IMPORTED.clear()
        _IMPORTED[key] = imported.size(d)
    return json.loads(json.dumps(_IMPORTED[key], default=float))


def preflight(changes: dict) -> dict:
    """The generator's cheap checks (sizing, gripper analysis, grip-force window, electronics plan) in ~20-40 ms
    (about 1 s for an imported SO-101): {issues, ms, dof, family, reach_mm, min_margin, joints, gripper_actuator}."""
    from .. import analysis
    from ..design import build_profile
    from ..robot import electronics2
    from ..robot.pipeline2 import gripper_design, gripper_drive
    from ..spec import Design

    t0 = time.perf_counter()
    issues, out = [], {}
    try:
        d = Design().with_changes(changes)
        if d.arm.source:
            from ..robot import imported
            sz = _imported_size(d)
            f = imported.gripper_summary(d, sz)["force"]
        else:
            from ..robot import arm
            sz = arm.size(d)
            gd, ga = gripper_design(d)   # the fingers and servo the gripper stage analyses, at the servo rail
            a = analysis.analyze(gd, build_profile(gd))
            f = _grip_force(gd, a, **gripper_drive(d, ga, sz))
            issues += a["issues"]
            out["deflection_mm"] = a["tip_deflection_mm"]
        issues += f["issues"]
        grip_t = f["target_force_n"]
        issues = list(sz["issues"]) + issues
        ep = electronics2.plan(d, sz, grip=f)
        out["grip"] = f
        issues += ep["issues"]
        out.update(dof=len(sz["joints"]), family=d.arm.actuator_family, reach_mm=sz.get("reach_mm"),
                   min_margin=min((j["margin"] for j in sz["joints"]), default=None), grip_target_n=grip_t,
                   joints=[[j["actuator"], j.get("count", 1)] for j in sz["joints"]],
                   gripper_actuator=sz.get("gripper_actuator") or (sz.get("gripper") or {}).get("actuator"),
                   supply=(ep.get("supply") or {}).get("key"))
    except Exception as exc:  # noqa: BLE001  a preflight bug must read as an issue, never as a pass
        issues.append(f"preflight could not run ({exc.__class__.__name__}: {exc})")
    return {"issues": list(dict.fromkeys(issues)), "ms": round((time.perf_counter() - t0) * 1000), **out}


ISSUE_KINDS = (("torque", r"needs [\d.]+ N·m"), ("supply", r"^supply .*margin"), ("too_small", r"jaw travel fits|object too small"),
               ("deflection", r"tip deflection|bending safety factor"),
               ("grip", r"no safe grip force|grip force [\d.]+ N < required|reaches only .* per finger"),
               ("stroke", r"stroke too small"), ("printer", r"exceeds the .* (?:build volume|printer bed)"))


def classify(issues: list) -> set:
    return {k for i in issues for k, p in ISSUE_KINDS if re.search(p, i)}


def explain_issues(b: Brief, issues: list) -> list:
    """Preflight issues as the confirm card shows them: a grip that no force fits says where the damage force came from
    (Morph's preset, the video, or the person's own words read as a category), so the person can tell what to change."""
    maxf, where = damage_force(b)
    if not (maxf and where):
        return list(issues)
    return [f"{i}; the {maxf:g} N damage force is {where}" if "grip" in classify([i]) else i for i in issues]


def deep_check(changes: dict) -> list:
    """Printed parts against the printer bed (builds the assembly, about 1 s). Only near the limit: measured base
    shells are 212-245 mm for modules builds and 239 mm with the custom PCB, against a 250 mm bed."""
    from ..robot import arm, electronics2
    from ..robot.manufacturing import FDM_BED
    from ..spec import Design

    d = Design().with_changes(changes)
    if d.arm.source or d.build.config != "prototype" or not (d.electronics.config == "custom_pcb" or int(d.arm.dof) == 6):
        return []
    try:
        s = arm.size(d)
        asm = arm.build(d, s, electronics2.plan(d, s))
    except Exception as exc:  # noqa: BLE001
        return [f"assembly check could not run ({exc.__class__.__name__})"]
    bed = sorted(FDM_BED, reverse=True)
    out = []
    for p in asm.parts:
        if p.process == "fdm_print":
            e = sorted((float(x) for x in p.mesh.extents), reverse=True)
            if any(a > b for a, b in zip(e, bed)):
                out.append(f"{p.name}: {e[0]:.0f} mm exceeds the {bed[0]:.0f} mm printer bed")
    return out


def repair(changes: dict, b: Brief, *, max_steps: int = 8, deep: bool = True) -> tuple[dict, list, dict]:
    """Preflight, then fix what the generator flags, one rule per issue kind (each fix is an assumption):
    torque -> Dynamixel servos when that arm fits a budget of $1,000 or more, else fewer joints first (the cheaper fix),
    then Dynamixel; finger deflection -> beam +1.5 mm up to 14 mm; object too small -> FSR finger; no safe grip ->
    FSR finger or a V pad; too big for the printer -> pilot build."""
    ch, notes = dict(changes), []
    locked = {k for k in b.hints if k in ("arm.dof", "arm.actuator_family")}
    if value(b, "precision") == "very_tight":
        locked.add("arm.actuator_family")
    budget = float(value(b, "budget"))
    allow_dxl, stated_budget = budget >= DYNAMIXEL_BUDGET, b.slots.get("budget") is not None
    t0, checks, thick0 = time.perf_counter(), 0, ch.get("finger.thickness_mm", 6.0)
    deep_done = False
    pre = {"issues": []}
    for _ in range(max_steps):
        pre = preflight(ch)
        checks += 1
        if not pre["issues"] and deep and not deep_done:
            deep_done = True
            big = deep_check(ch)
            if big:
                pre["issues"] = big
        if not pre["issues"]:
            break
        kinds, changed = classify(pre["issues"]), False
        if "too_small" in kinds and ch.get("finger.sensor") != "fsr402" and not ch.get("arm.source"):
            ch.update({"finger.sensor": "fsr402", "finger.width_mm": 20.0, "fab.process": "fdm_print", "finger.material": "PETG"})
            notes.append({"kind": "repair", "slot": "object", "text": "The object is narrower than plain fingers close on, so I used the "
                          "FSR 402 force-sensing finger: its thicker pad stack lets the gripper close on it."})
            changed = True
        if kinds & {"torque", "supply"} and not ch.get("arm.source"):
            fam, dof = ch["arm.actuator_family"], int(ch["arm.dof"])
            can_fam, can_dof = fam != "dynamixel" and "arm.actuator_family" not in locked, dof > 4 and "arm.dof" not in locked
            step = None
            if can_fam and allow_dxl:
                # Dynamixel servos keep every joint when that arm fits the budget; otherwise fewer joints come first (cheaper)
                trial = dict(ch, **{"arm.actuator_family": "dynamixel"})
                est = estimate_cost(trial, preflight(trial))
                checks += 1
                if not can_dof or (est is not None and est["unit_cost"] <= budget * 1.05):
                    step = ("arm.actuator_family", "dynamixel", f"The shoulder needs more torque than {_family_label(fam)} servos "
                            "give with a 2x margin, so I used Dynamixel servos.")
            if step is not None:
                pass
            elif can_dof:
                step = ("arm.dof", dof - 1, f"The shoulder needs more torque than {dof} joints of {_family_label(fam)} servos give with "
                        f"a 2x margin, so I used {dof - 1} joints: a shorter, lighter wrist.")
            elif can_fam:
                step = ("arm.actuator_family", "dynamixel", f"With {dof} joints, {_family_label(fam)} servos are too weak for this "
                        "load, so I used Dynamixel servos" + ("." if allow_dxl or stated_budget else
                                                             "; they cost more than the default budget (see the parts estimate)."))
            elif dof > 4:
                step = ("arm.dof", dof - 1, f"You asked for {dof} joints, but no servo family here holds this load with {dof}; "
                        f"I used {dof - 1}.")
            elif fam != "dynamixel":
                step = ("arm.actuator_family", "dynamixel", f"You asked for {_family_label(fam)} servos, but they can't hold this "
                        "load; I used Dynamixel servos.")
            if step:
                ch[step[0]] = step[1]
                notes.append({"kind": "repair", "slot": "budget", "text": step[2]})
                changed = True
        if "deflection" in kinds and ch.get("finger.thickness_mm", 6.0) < 14.0:
            ch["finger.thickness_mm"] = round(min(14.0, ch.get("finger.thickness_mm", 6.0) + 1.5), 1)
            changed = True
        if "grip" in kinds:
            if ch.get("finger.sensor") != "fsr402" and ch.get("object.max_force_n", 0) > 0 and not ch.get("arm.source"):
                ch.update({"finger.sensor": "fsr402", "finger.width_mm": 20.0, "fab.process": "fdm_print", "finger.material": "PETG"})
                notes.append({"kind": "repair", "slot": "fragility", "text": "No grip force was both firm enough and gentle enough "
                              "without feedback, so I added the FSR 402 force sensor: the firmware stops squeezing at the target."})
                changed = True
            elif ch.get("finger.pad") != "v" and ch.get("object.shape") != "box":
                ch["finger.pad"] = "v"
                notes.append({"kind": "repair", "slot": "object", "text": "A V-groove pad holds this object with less squeeze than "
                              "a contoured one, so it stays inside the force that would damage it."})
                changed = True
        if "printer" in kinds and ch.get("build.config") == "prototype":
            ch["build.config"] = "pilot"
            notes.append({"kind": "repair", "slot": "quantity", "text": "The base shell is larger than a 250 mm printer bed, so I set "
                          "a pilot build: CNC and sheet-metal parts instead of prints."})
            deep_done, changed = True, True
        if not changed:
            break
    else:
        pre = preflight(ch)
        checks += 1
    if ch.get("finger.thickness_mm", 6.0) != thick0:
        notes.append({"kind": "repair", "slot": "object", "text": f"The fingers bent more than 1 mm under the grip load, so I made "
                      f"the finger beam {ch['finger.thickness_mm']:g} mm thick instead of {thick0:g} mm."})
    pre = dict(pre, ms=round((time.perf_counter() - t0) * 1000), checks=checks)
    return ch, notes, pre


# ------------------------------------------------------------------ confirm card
REST_USD = {"hobby_pwm": 222.0, "smart_bus": 197.0, "dynamixel": 212.0}  # fitted to 13 research builds (prints, boards, supply)
EXTRA_USD = {"wrist_camera": 25.0, "estop": 8.0, "imu": 25.0, "wifi": 0.0}


def estimate_cost(changes: dict, pre: dict) -> dict | None:
    """Parts cost of one printed prototype from the sized actuators (±10 % against full builds); None otherwise."""
    from ..robot import catalog

    if changes.get("arm.source") or changes.get("build.config") != "prototype" or changes.get("electronics.config") != "modules" \
            or not pre.get("joints"):
        return None
    try:
        acts = sum(catalog.price(k) * int(n) for k, n in pre["joints"]) + catalog.price(pre.get("gripper_actuator") or "sts3215")
    except KeyError:
        return None
    extras = sum(EXTRA_USD.get(e, 0.0) for e in (changes.get("electronics.extras") or "").split(",") if e)
    total = acts + REST_USD.get(changes.get("arm.actuator_family"), 200.0) + extras
    return {"unit_cost": round(total, -1), "label": f"about ${round(total, -1):,.0f} in parts (estimated)",
            "basis": "catalog servo prices plus the measured cost of printed parts, boards and supply"}


def suggest_name(b: Brief, home: str | None = None) -> str:
    a = _arch(b)
    obj = _slug_of(value(b, "object"))
    base = a.name.format(obj=obj)
    base = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", base.lower())).strip("-")[:36] or "new-robot"
    if not NAME_RE.match(base) or base.startswith("."):
        base = "new-robot"
    name, n = base, 2
    while home and os.path.exists(os.path.join(home, name)):
        name, n = f"{base}-{n}", n + 1
    return name


def _layout(b: Brief, key: str | None = None) -> dict:
    """A layout in this brief's words: a plant or a feeding spot takes the bin's place where the archetype has one."""
    key = key or value(b, "layout")
    lay, place = LAYOUTS[key], _arch(b).place
    if place and key == "tray_to_bin":
        return dict(lay, label=f"Tray in front, {place} on the left", to=f"to the {place} on its left")
    return lay


def _phrase_layout(b: Brief) -> str:
    lay = _layout(b)
    return f"{lay['from']} {lay['to']}"


def assumptions_for(b: Brief, ch: dict) -> list:
    """One plain sentence per slot Morph filled in (defaults and inferences), with the alternatives as /api/params chips."""
    from ..robot.pipeline2 import PARAMS

    pkeys = {p[0] for p in PARAMS}
    out = []

    base = brief_to_changes(b)

    def alt_changes(slot, v):
        if slot == "object" and isinstance(v, str) and v in OBJECTS:
            return {"object.name": v}
        b2 = Brief.from_dict(b.to_dict())
        b2.slots[slot] = Slot(coerce(slot, v), "user")
        diff = {k: v2 for k, v2 in brief_to_changes(b2).items() if base.get(k) != v2 and k != "name"}
        return diff if diff and all(k in pkeys for k in diff) else None

    def add(slot, text, kind):
        q = make_question(b, slot) if slot in SLOTS else None
        # options: other answers for this slot before the build (POST /api/brief answers); alternatives: the same choices
        # as /api/params changes once the robot exists (only those that map onto Parameters keys)
        cur = value(b, slot)
        cur = cur.get("key") if isinstance(cur, dict) else cur
        opts = [] if slot == "budget" else [{"label": c["label"], "value": c["value"]} for c in (q or {}).get("chips", []) if c["value"] != cur]
        alts = []
        if slot == "budget":
            q = {"chips": [{"label": "Dynamixel servos (from about $700 in parts)", "value": "dynamixel"},
                           {"label": "Hobby PWM servos (no position feedback)", "value": "hobby_pwm"}]}
        for c in (q or {}).get("chips", []):
            if c.get("default") or c["value"] == value(b, slot):
                continue
            d = {"arm.actuator_family": c["value"]} if slot == "budget" else alt_changes(slot, c["value"])
            if d:
                alts.append({"label": c["label"], "changes": d})
        out.append({"slot": slot, "text": text, "kind": kind, "options": opts[:3], "alternatives": alts[:3]})

    def object_gap(o):
        """A custom object the user gave only some numbers for: the rest is an assumption too ("450 g candles" -> 45 mm)."""
        st = o.get("stated")
        if o.get("key") or not st:
            return
        clean = {k: v for k, v in o.items() if k != "stated"}
        x = _plural(str(o.get("name") or "object")).lower()
        if "diameter_mm" not in st:
            choices = [(f"{d} mm wide", dict(clean, diameter_mm=float(d), length_mm=max(float(clean.get("length_mm", d)), float(d))))
                       for d in (30, 55, 70) if d != clean["diameter_mm"]]
            text = f"I assumed the {x} are {clean['diameter_mm']:g} mm wide where the fingers close."
        else:
            choices = [(f"{m} g each", dict(clean, mass_g=float(m))) for m in (50, 200, 450) if m != clean["mass_g"]]
            text = f"I assumed the {x} weigh {clean['mass_g']:g} g each."
        alts = [{"label": lab, "changes": d} for lab, d in ((lab, alt_changes("object", v)) for lab, v in choices) if d]
        out.append({"slot": "object", "text": text, "kind": "default", "options": [{"label": lab, "value": v} for lab, v in choices][:3],
                    "alternatives": alts[:3]})

    for slot in ("object", "fragility", "layout", "throughput", "people_nearby", "perception", "budget", "quantity"):
        s = b.slots.get(slot)
        if slot == "object" and s is not None and s.source in ("user", "video", "photo"):
            object_gap(s.value)
            continue
        if slot == "fragility":  # words turned into newtons are Morph's reading of them: say so, with the other readings
            maxf, where = damage_force(b)
            if maxf and s is not None and s.source in ("user", "video", "photo") and not value(b, "object").get("max_force_n"):
                cat = {"fragile": "fragile", "care": "needing care"}[s.value]
                add(slot, f"I read '{_fragility_words(s)}' as {cat}: damaged above about {maxf:g} N per finger.", "inferred")
            continue
        if s is not None and s.source in ("user", "video", "photo") or (s is not None and s.source == "memory"):
            continue
        kind = "inferred" if s is not None and s.source == "inferred" else "default"
        v = value(b, slot)
        if slot == "object":
            o = v
            frag = f", damaged above about {o['max_force_n']:g} N" if o.get("max_force_n") else ""
            add(slot, f"I assumed {_object_label(o, short=True).lower()}: {o['diameter_mm']:g} mm wide, {o['mass_g']:g} g{frag}.", kind)
        elif slot == "layout":
            add(slot, f"I assumed it works {_phrase_layout(b)}; the pick and place points are under Task.", kind)
        elif slot == "throughput":
            add(slot, f"I assumed one pick and place every {float(v):g} s.", kind)
        elif slot == "people_nearby":
            env = value(b, "environment")
            if "estop" not in (ch.get("electronics.extras") or ""):
                add(slot, f"I assumed {PEOPLE[v]}, so there is no emergency stop.", kind)
            elif v in ("yes", "children", "pets"):
                add(slot, f"I assumed {PEOPLE[v]}, so it has an emergency stop.", kind)
            else:  # the e-stop comes with the place: say whether the place was the person's or Morph's guess
                es = b.slots.get("environment")
                said = es is not None and es.source in ("user", "video", "photo")
                add(slot, f"{'It works' if said else 'I assumed it works'} at a {ENVIRONMENTS[env]}, so it has an emergency stop.",
                    kind)
        elif slot == "perception" and v == "wrist_camera":
            add(slot, "I added a wrist camera for sorting: the hardware and wiring only; Morph does not write vision code.", kind)
        elif slot == "budget" and ch.get("arm.actuator_family") == "smart_bus" and not ch.get("arm.source"):
            add(slot, f"I assumed a parts budget of up to ${float(v):,.0f}, so it uses smart-bus servos (Feetech STS3215).", kind)
        elif slot == "quantity" and int(v) == 1:
            add(slot, "I assumed one 3D-printed prototype.", kind)
    return out


def summary_text(b: Brief, ch: dict, pre: dict) -> str:
    o, lay = value(b, "object"), _layout(b)
    if ch.get("arm.source") == "so101":
        head, grip = "Your SO-101 arm", "its own gripper"
    else:
        head = f"A {pre.get('dof') or ch.get('arm.dof')}-joint desk arm with {_family_label(ch.get('arm.actuator_family'))} servos"
        grip = "a force-sensing gripper" if ch.get("finger.sensor") == "fsr402" else "a two-finger gripper"
    x, frm, to = _object_label(o, short=True).lower(), lay["from"], lay["to"]
    task = value(b, "task")
    who = "you" if re.search(r"\b(?:hand|pass|give|bring)(?:s|es)?\s+(?:it\s+to\s+)?me\b", b.request.lower()) else "over"
    verb = {"sort": f"picks up {x} {frm} and sorts them into bins {to.replace('into a bin ', '')}",
            "hand_over": f"hands {who} {x}: it picks them up {frm} and sets them down {to}",
            "transfer": f"moves {x} {frm} {to}", "dispense": f"carries {x} {frm} {to}"}.get(task, f"picks up {x} {frm} and sets them down {to}")
    if task == "sort" and value(b, "layout") != "tray_to_bin":
        verb = f"picks up {x} {frm} and sorts them {to}"
    cam = " A wrist camera is fitted; Morph does not write the vision code." if "wrist_camera" in (ch.get("electronics.extras") or "") else ""
    return f"{head} and {grip} that {verb}, about one every {float(ch.get('task.cycle_time_s', 6)):g} s.{cam}"


def finalize(b: Brief, *, deep: bool = True, home: str | None = None) -> Brief:
    """Fill defaults, map to design keys, preflight and repair: the confirm card (status ready)."""
    if not (b.name and NAME_RE.match(b.name)) or (home and os.path.exists(os.path.join(home, b.name))):
        b.name = suggest_name(b, home)
    ch = brief_to_changes(b)
    ch, repairs, pre = repair(ch, b, deep=deep)
    pre["issues"] = explain_issues(b, pre["issues"])
    b.changes = ch
    b.preflight = {k: pre[k] for k in ("issues", "ms", "checks", "dof", "family", "reach_mm", "min_margin", "grip_target_n")
                   if k in pre}
    b.assumptions = assumptions_for(b, ch) + repairs + [{"kind": "scope", "text": n} for n in b.scope.get("notes", [])]
    if b.flags.get("nearest_note"):
        b.assumptions.insert(0, {"kind": "scope", "text": f"Building the nearest robot Morph can make: {b.flags['nearest_note']}"})
    b.estimate = estimate_cost(ch, pre)
    bud = b.slots.get("budget")
    if b.estimate and bud is not None and bud.source in ("user", "video", "photo") and \
            b.estimate["unit_cost"] > float(bud.value) * 1.05 and float(bud.value) >= MIN_COST_USD:
        dxl_fix = ch.get("arm.actuator_family") == "dynamixel" and any(r.get("slot") == "budget" for r in repairs)
        why = (": smaller servos can't hold this load with a 2x margin" if dxl_fix else
               ": placement to ±1 mm needs Dynamixel servos" if value(b, "precision") == "very_tight" else "")
        b.assumptions.append({"kind": "scope", "text": f"The parts estimate, about ${b.estimate['unit_cost']:,.0f}, is above your "
                              f"${float(bud.value):,.0f} budget{why}."})
    b.summary = summary_text(b, ch, pre)
    b.questions, b.status = [], "ready"
    b.turns.append({"role": "assistant", "text": b.summary, "ts": _now_iso()})
    b.updated = time.time()
    return b


def card(b: Brief) -> list:
    """Brief card rows for the UI: [{slot, label, text, source, badge}] ('you said' / 'assumed' / 'default')."""
    rows = []
    labels = {"task": "Task", "object": "Object", "layout": "Layout", "throughput": "Pace", "environment": "Where",
              "people_nearby": "People", "precision": "Precision", "budget": "Budget", "perception": "Camera",
              "connectivity": "Commands", "quantity": "Quantity", "existing_hardware": "Arm"}
    try:
        for slot, label in labels.items():
            s = b.slots.get(slot)
            src = s.source if s is not None and s.value is not None else "default"
            v = value(b, slot)
            if slot == "object":
                maxf, where = damage_force(b)   # what gets built, a stated category included
                txt = _object_label(v) + (f", damaged above {maxf:g} N" if maxf else "") + \
                    (f" ({where})" if maxf and not v.get("max_force_n") and where else "")
                gaps = [w for k, w in (("diameter_mm", "width"), ("mass_g", "weight")) if v.get("stated") and k not in v["stated"]]
                preset = "from Morph's preset"
                if gaps:  # what the person didn't give: Morph's preset for a named object, else an assumption
                    txt += f"; {' and '.join(gaps)} {preset if v.get('key') else 'assumed'}"
                elif v.get("key") and not v.get("stated") and src in ("user", "video", "photo"):
                    txt += f"; sizes {preset}"  # the person named it; the sizes are Morph's
            elif slot == "task":
                txt = TASKS.get(v, v).capitalize()
            elif slot == "layout":
                txt = _layout(b, v)["label"]
            elif slot == "throughput":
                txt = f"One every {float(v):g} s"
            elif slot == "environment":
                txt = ENVIRONMENTS[v].capitalize()
            elif slot == "people_nearby":
                txt = PEOPLE[v].capitalize()
            elif slot == "precision":
                txt = PRECISION[v].capitalize()
            elif slot == "budget":
                txt = f"Up to ${float(v):,.0f}"
            elif slot == "perception":
                txt = "Wrist camera (no vision code)" if v == "wrist_camera" else "No camera"
            elif slot == "connectivity":
                txt = "Wi-Fi" if v == "wifi" else "USB"
            elif slot == "quantity":
                txt = f"{int(v):,}"
            else:
                txt = "Your SO-101" if v == "so101" else "New Morph arm"
            badge = {"user": "you said", "video": "from video", "photo": "from photo", "memory": "assumed", "inferred": "assumed"}.get(src, "default")
            rows.append({"slot": slot, "label": label, "text": txt, "source": src, "badge": badge})
    except (KeyError, TypeError, ValueError):
        pass
    return rows


# ------------------------------------------------------------------ build hand-off + persistence
def build_payload(b: Brief, name: str | None = None, home: str | None = None) -> dict:
    """What the W0b project service needs to create and build the project (the brief never touches the disk layout)."""
    from ..spec import Design

    if b.status not in ("ready", "building") or not b.changes:
        raise ValueError("the brief is not ready to build")
    nm = name if name and NAME_RE.match(name) else (b.name or suggest_name(b, home))
    ch = dict(b.changes, name=nm)
    return {"name": nm, "changes": ch, "design": Design().with_changes(ch).to_dict(),
            "meta": {"kind": "robot", "source": ch.get("arm.source", ""), "need": (b.summary or b.request)[:300],
                     "origin": "brief", "demo": False, "brief_id": b.id},
            "brief": b.to_dict()}


def mark_built(b: Brief, project: str, rev: str | None = None):
    b.status, b.project, b.rev, b.updated = "built", project, rev, time.time()


def _drafts(home: str) -> str:
    return os.path.join(os.path.realpath(home), ".briefs")


def draft_path(home: str, bid: str) -> str | None:
    """<home>/.briefs/<id>.json, or None for an id that isn't ours (ids are 16 hex characters)."""
    if not isinstance(bid, str) or not ID_RE.match(bid):
        return None
    root = _drafts(home)
    p = os.path.realpath(os.path.join(root, bid + ".json"))
    return p if os.path.commonpath([root, p]) == root else None


def save_draft(b: Brief, home: str) -> str | None:
    p = draft_path(home, b.id)
    if not p:
        return None
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(b.to_dict(), fh, ensure_ascii=False, default=str)
    os.replace(tmp, p)
    return p


def load_draft(home: str, bid: str) -> Brief | None:
    p = draft_path(home, bid)
    if not p or not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return Brief.from_dict(json.load(fh))
    except (OSError, ValueError, TypeError):
        return None


def delete_draft(home: str, bid: str) -> bool:
    p = draft_path(home, bid)
    try:
        if p and os.path.isfile(p):
            os.remove(p)
            return True
    except OSError:
        pass
    return False


def cleanup_drafts(home: str, max_age_s: float = DRAFT_TTL_S, now: float | None = None) -> int:
    """Delete drafts older than 7 days (by modification time); returns how many."""
    root, n = _drafts(home), 0
    now = now or time.time()
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for fn in names:
        p = os.path.join(root, fn)
        if (fn.endswith(".json") or fn.endswith(".tmp")) and os.path.isfile(p):
            try:
                if now - os.path.getmtime(p) > max_age_s:
                    os.remove(p)
                    n += 1
            except OSError:
                pass
    return n


def write_final(b: Brief, project_root: str) -> str:
    """<project>/brief.json: the brief that created the project."""
    p = os.path.join(project_root, "brief.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(b.to_dict(), fh, ensure_ascii=False, indent=1, default=str)
    return p
