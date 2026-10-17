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

import bisect
import functools
import json
import os
import re
import secrets
import threading
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


# the SO-101 (also written SO-ARM101) or LeRobot arm, in a request
_SO = r"(?:\bso[- ]?(?:arm[- ]?)?10[01]\b|\ble ?robot\b)"


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
               # a workbench is where the robot stands, not a tool it hands over ("palletises boxes on my workbench")
               (r"\b(tools?|screwdrivers?|wrench(?:es)?|pliers|spanners?|hammers?|hex keys?|allen keys?)\b", 2)),
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
    Archetype("so101", "Your SO-101 arm", "pick_place", ((r"\bso[- ]?(?:arm[- ]?)?10[01]\b", 6), (r"\ble ?robot\b", 6)),
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
# ---- people: who the robot hands things to, and who works beside it (UX-3, E-N8). A person next to the robot gets an
# emergency stop, so when the words leave it unsure, the person is there: a missed hand-over drops the e-stop beside
# someone, a false one only adds an $8 switch.
# Family words that name a person on their own ("bring dad his slippers", "pass the remote to mum", "pass grandad his paper",
# "bring mother her shawl", "give grampa his pills")
_KIN = (r"(?:grand(?:ma|pa|mother|father|parents?|mum|mom|mummy|mommy|dad|daddy|ad|pop|pops)|grand-dad|"
        r"great[- ]grand(?:ma|pa|mother|father|parents?|mum|mom|dad|ad|-dad)|granny|grannie|gran|gramps|gram(?:ma|my|pa)|"
        r"gran(?:ma|pa)|meemaw|memaw|mawmaw|pop-?pop|nonn[ao]|abuel[ao]|nan(?:a|na|ny)?|mum(?:my)?|mom(?:my|ma)?|mam|mama|"
        r"dad(?:dy)?|dada|papa|mother|father|auntie|aunty|uncle|baby|"
        # family words from other languages and spellings ("a robot that helps my oma", "bring the tea to nonno")
        r"grandmama|grandmamma|grandda|granda|grandpaw|grandmaw|granma|granpa|grampy|oma|opa|omi|opi|nanaji|nani|dadi|"
        r"dadaji|ammi|abba|amma|appa|ammachi|achan|mami|papi|mamie|papie|mamá|papá|mamma|pappa|tita|tito|lolo|lola|"
        r"halmeoni|harabeoji|obaachan|ojiichan|obaasan|ojiisan|baba|babushka|dedushka|zayde|zaide|zaida|zeyde|bubbe|"
        r"bubby|bobe|babcia|dziadek|savta|saba|teta|jiddo|jaddah|nai ?nai|ye ?ye|po ?po|gong ?gong|lao ?lao|yiayia|yaya|"
        r"pappou|papou|abuelita|abuelito|vovó|vovô|vovo|avó|avô|farfar|farmor|mormor|morfar|mémé|pépé|mamaw|papaw|nonno)")
# ... and short ones that do only where a person is the one given something ("bring the tea to ma", "pass the salt to sis",
# "my bestie"): "pa" and "ma" are also units and abbreviations elsewhere
_KIN_SLANG = r"(?:ma|pa|pop|pops|sis|bro|bruv|hubby|wifey|missus|missis|bestie|roomie|kiddo|bf|gf|bae)"
# Words for a person that need a determiner or a possessive ("to the surgeon", "to our packers", "hand my colleague the
# stapler") or a plural ("serve coffee to customers"): care, work, shop and public, family, children
_ROLE = (r"(?:nurse|doctor|doc|surgeon|dentist|vet|paramedic|medic|pharmacist|midwife|physio(?:therapist)?|therapist|carer|"
         r"care ?giver|care (?:assistant|worker|aide)|aide|resident|(?:in|out)?patient|orderly|matron|"
         # abbreviated and short ones after a determiner ("to the RN", "the dr", "the GP", "the pt")
         r"rn|lpn|lvn|cna|hca|gp|fy1|f1|odp|slp|pt|ot|dr|kp|"
         # job titles that end like a machine or a container ("the hairdresser", "the pot washer", "the new starters")
         r"hairdresser|scaffolder|(?:pot|window|car) washer|french polisher|new (?:starter|joiner|hire)|newcomer|"
         r"(?:stone)?mason|family|household|landlord|landlady|ex|ex-(?:wife|husband|partner)|"
         r"operator|worker|packer|welder|assembler|inspector|technician|tech|machinist|engineer|mechanic|scientist|researcher|"
         r"postdoc|staffer|crew|team|member|colleague|co-?worker|teammate|boss|manager|supervisor|apprentice|trainee|intern|"
         r"associate|employee|(?:team|line|shift|tech|group|project|section|crew) lead|"
         r"(?:morning|night|day|evening|late|early|next|other|afternoon|weekend|first|second|third|graveyard) shift|"
         r"chef|cook|waiter|waitress|waitstaff|barista|bartender|busboy|busser|cashier|clerk|receptionist|assistant|helper|"
         r"(?<!vacuum )(?<!floor )(?<!steam )cleaner|gardener|housekeeper|babysitter|nanny|au pair|maid|butler|valet|chauffeur|concierge|secretary|"
         r"driver|courier|librarian|teacher|tutor|coach|student|pupil|volunteer|attendant|porter|guard|"
         r"steward(?:ess)?|host(?:ess)?|usher|janitor|caretaker|fishmonger|butcher|baker|grocer|florist|farmer|"
         r"customer|shopper|client|guest|visitor|attendee|passenger|user|owner|walker|sitter|groomer|diner|patron|"
         r"audience|crowd|bride|groom|bridesmaid|jockey|human|participant|(?:test|study|research) subject|recipient|"
         r"neighbou?r|friend|mate|buddy|pal|partner|lover|wife|husband|spouse|boyfriend|girlfriend|fianc[eé]e?|room-?mate|flat-?mate|"
         r"housemate|lodger|tenant|son|daughter|brother|sister|sibling|cousin|nephew|niece|"
         r"(?:mother|father|brother|sister|son|daughter)-in-law|in-laws|step-?(?:mum|mom|mother|dad|father|parent|son|daughter)|"
         r"god(?:mother|father|parent|child|son|daughter)|other half|significant other|sweetheart|darling|"
         r"parent|grandchild|grandkid|grandson|granddaughter|aunt|"
         r"child|kid|kiddie|baby|infant|newborn|toddler|twin|boy|girl|teen|teenager|tween|youngster|schoolkid|"
         r"(?:little|wee) one(?!s)|seamstress|actress|headmistress|princess|songstress|chiropractor|counsell?or|instructor|"
         r"professor|mentor|decorator|sailor|tailor|actor|author|editor|translator|interpreter|officer|lawyer|teller|"
         r"photographer|firefighter|fitter|stacker|stocker|riveter|fabricator|bricklayer|locksmith|blacksmith|goldsmith|"
         r"silversmith|architect|paralegal|attorney|judge|notary|accountant|analyst|executive|consultant|president|"
         r"principal|dean|chaplain|priest|vicar|pastor|rabbi|imam|monk|nun|bishop|captain|cop|sheriff|ranger|scout|"
         r"cadet|recruit|rookie|veteran|athlete|umpire|shepherd|ceo|vip|emt|passer-?by|bystander|onlooker|pedestrian|"
         r"spectator|commuter|tourist|"
         r"person|guy|man|woman|lady|gentleman|folk|adult|elder|senior|pensioner|" + _KIN_SLANG + "|" + _KIN + r")")
_PLURALS = (r"people|staff|crew|folks|children|schoolchildren|grandchildren|men|women|ladies|gentlemen|babies|midwives|wives|"
            r"passers-?by|bystanders|onlookers|pedestrians|spectators|commuters|tourists|fans|trick[- ]or[- ]treaters|"
            r"carol+ers|personnel|families|households|"
            # groups named by how they are ("helps the elderly", "for the homeless", "hands the public leaflets")
            r"elderly|disabled|sick|injured|wounded|blind|deaf|frail|infirm|aged|needy|homeless|poor|public|"
            r"members of the public|visually impaired|hard of hearing|less able|lads|gents|oldies|regulars|gang|"
            r"little ones|amputees|(?:para|quadri|tetra|hemi)plegics|diabetics|sufferers|survivors|"
            # "the two of us", "both of them"
            r"(?:the )?(?:two|three|four|both|all) of (?:us|them|you)")
_ROLES = r"(?:" + _PLURALS + "|" + _ROLE + r"(?:s|es)?)"
_ROLES_PL = r"(?:" + _PLURALS + "|" + _ROLE + r"(?:s|es))"
_ANYONE = r"(?:someone|somebody|anyone|anybody|everyone|everybody|whoever|whomever)"
# A place, a piece of furniture, a container or a machine the things go to (multi-word ones first). A seat or a bed is
# none: the person sitting or lying there takes the thing ("bring the tea to her bed")
_PLACE = (r"(?:garden beds?|flower beds?|raised beds?|cup ?holders?|car boot|"
          r"desk|room|bedroom|office|study|workshop|garage|shed|lab|studio|kitchen|pantry|bathroom|hall(?:way)?|"
          r"house|home|flat|apartment|porch|garden|yard|car|van|truck|door|doorstep|mailbox|letterbox|building|lobby|"
          r"table|bench|workbench|workstation|worktable|workspace|counter|shelf|shelves|bookshelf|bookcase|cupboard|cabinet|"
          r"closet|wardrobe|dresser|drawer|nightstand|locker|rack|station|bin|box|toolbox|basket|crate|tray|bowl|cart|trolley|"
          r"organi[sz]er|coaster|placemat|mat|lamp|line|conveyor|belt|machine|robot|press|lathe|saw|printer|cnc|oven|fridge|"
          r"freezer|microwave|dishwasher|sink|step|sill|windowsill|window|vise|cell|panel|console|area|zone|bay|gate|"
          r"entrance|exit|hook|backpack|rucksack|tote|bucket|chest|jig|fixture|charger|pegboard|holder|trunk|bag|case|pouch|"
          r"caddy|hopper|chute|pallet|dock|cradle|slot|staffroom|break ?room)s?")
_NOT_DESCRIBING = (r"(?:a|an|the|and|or|but|so|then|when|whenever|while|if|as|at|in|on|by|with|from|for|to|of|after|"
                   r"before|during|until|once|every|each|because|since|please|now|she|he|they|it|i|we|you|her|his|their|"
                   r"my|is|was|can|will|would|could|should|who|that|which)")
_ADVERB = (r"(?:twice|daily|nightly|weekly|hourly|today|tonight|tomorrow|again|also|too|first|later|soon|"
           r"always|usually|sometimes|often|here|there|upstairs|downstairs|quickly|slowly|carefully|gently|safely|"
           r"right|straight|directly|only|early|back|away|just|even|still)")
_PREPOSITION = (r"(?:near|beside|behind|below|beneath|under|underneath|over|above|into|onto|inside|outside|across|"
                r"around|round|through|next|between|against|without|within|towards?|up|down|off|out|along|past|beyond|"
                r"opposite|forth)")
_PHRASE_END = (r"(?=\s*(?:$|[.,;:!?()\[\]{}\"“”…/]|-(?![a-z])|['’](?!s\b)|\d|(?:" + _NOT_DESCRIBING + r"|" + _ADVERB + r"|"
               + _PREPOSITION + r")\b))")
# " desk", " bedside table", " room's door"; not " home nurse" or " office manager"
_A_PLACE = (r"\s+(?:(?:" + _PLACE + r"['’]s|(?!" + _NOT_DESCRIBING + r"\b)[a-z-]+)\s+){0,3}?" + _PLACE + r"\b" +
            _PHRASE_END)
_OWNS = r"(?:['’]s|(?<=s)['’])"                                # "the nurse's desk", "the nurses' station"
_NOT_THEIRS = r"(?![’']s\b)"                                   # "pass my dad's pills": the pills are his, he isn't the receiver
# a person's word is no person when it names or owns a place: "the operator station", "my dad's workshop"; the words
# between them describe the place, so "its", a possessive or a verb ends them ("the operator fills its hopper", "my dad
# swaps its tray", "workers fill trays")
_VERB_BASE = (r"(?:fill|refill|load|unload|reload|empty|swap|change|use|take|put|drop|tip|stack|sort|pack|move|clean|check|"
              r"carry|push|pull|pick|place|set|lay|hand|pass|give|bring|feed|stock|restock|top|open|close|lift|hold|grab|"
              r"collect|fetch|get|wipe|tidy|clear|replace|add|remove|run|work|stand|sit|wait|watch|need|want|reach|lean|"
              r"press|unjam|guide|train|operate|tend|mind|supervise|program|start|stop|reset|switch|turn|plug|charge|"
              r"service|repair|fix|adjust|inspect|monitor|oversee|teach|help|assist|serve|share|keep|leave|come|go|walk|"
              r"stop|look)")
_NOT_THEIR_PLACE = (r"(?!" + _OWNS + _A_PLACE + r"|(?!\s+" + _VERB_BASE + r"\s+(?:the|a|an|it|its|their|our|his|her|my|your|this|"
                    r"that|these|those|down|on|up|out|off)\b)\s+(?:(?!(?:" + _NOT_DESCRIBING + "|" + _PREPOSITION + "|" + _ADVERB +
                    r"|its|our|your|me|us|them|him)\b)(?![a-z-]+(?:s|ed)\b)(?!" + _VERB_BASE + r"\b)[a-z-]+\s+){0,2}?" + _PLACE +
                    r"\b" + _PHRASE_END + r")")
# "to her", "to my cousin", "to their nanny", "to our cleaner": a possessive is the receiver unless it owns a place,
# itself or through someone ("to her desk", "to my dad's workshop", "to her nurses' station", "to their bedroom shelf")
_NO_PLACE_OF_THEIRS = r"(?!(?:\s+[a-z-]+" + _OWNS + r")?" + _A_PLACE + r")"
_HER_HIS = r"(?:her|his)\b" + _NO_PLACE_OF_THEIRS
# an animal it gives things to ("toss balls to my dog", "hand the dog a bone") is a pet next to it, not a person it hands
# over to; "my dog walker", "the dog trainer" and "my dog's sitter" are people
_ANIMAL = (r"(?:dogs?|dogg(?:y|ie|ies|o)|pupp(?:y|ies)|pups?|cats?|kitt(?:y|ies|ens?)|pets?|rabbits?|bunn(?:y|ies)|"
           r"hamsters?|gerbils?|guinea pigs?|ferrets?|parrots?|budgies?|parakeets?|birds?|chickens?|hens?|ducks?|geese|"
           r"goose|goats?|horses?|pon(?:y|ies)|tortoises?|turtles?|lizards?|geckos?|snakes?|goldfish|fish|animals?|"
           r"lambs?|sheep|ewes?|cows?|cattle|calf|calves|pigs?|piglets?|donkeys?|alpacas?|llamas?|livestock)")
_ANIMAL_NP = (r"(?:(?:the|a|an|my|our|your|their|her|his|this|that|these|those)\s+)?(?:(?!" + _NOT_DESCRIBING +
              r"\b)[a-z'’-]+\s+){0,2}?" + _ANIMAL + r"\b")
_AN_ANIMAL = _ANIMAL_NP + r"(?:" + _PHRASE_END + r"|(?=\s+its\b))"
_POSS = r"(?:her|his|my|our|your|their)\b" + _NO_PLACE_OF_THEIRS + r"(?!\s+" + _AN_ANIMAL + ")"
# A word for someone by what they do that isn't on the lists (people_text writes it "morphagent") ends its noun phrase,
# or another one follows: "to the fitter", "hand the fitter the bolts", but "the trainer shoes" are shoes
_AGENT_END = (r"(?:(?<!morphagent)(?<!morphagents)|" + _PHRASE_END + r"|(?=\s+(?:some|this|these|those|its|one|two|three|more|"
              r"any|[0-9]+)\b))")
_PERSON = (r"(?:" + _KIN + r"|(?:elderly|disabled) [a-z]+|(?:my|our|your) (?:(?!" + _NOT_DESCRIBING + r"\b)[a-z0-9'’-]+ ){0,3}?"
           r"(?:" + _ROLES + r"|morphagents?)\b" + _AGENT_END + ")")
_ROBOT_WORD = r"(?:robot|robotic|arm|bot|cobot|machine|gripper|design|device|gadget|system|setup)s?"
# What can be given something without being a person: the things themselves, food, plants, places, the robot, animals
_THINGS = (r"(?:things?|items?|objects?|stuff|parts?|pieces?|components?|products?|goods|samples?|specimens?|tubes?|vials?|"
           r"cups?|mugs?|glass(?:es)?|plates?|bowls?|dish(?:es)?|cutlery|spoons?|forks?|knives|bottles?|cans?|jars?|tins?|"
           r"box(?:es)?|cartons?|packages?|parcels?|bags?|trays?|bins?|blocks?|bricks?|toys?|balls?|tools?|screws?|bolts?|"
           r"nuts|washers?|eggs?|fruits?|berr(?:y|ies)|strawberr(?:y|ies)|tomato(?:e?s)?|grapes?|apples?|oranges?|bananas?|"
           r"vegetables?|veg|food|meals?|snacks?|drinks?|cookies?|pills?|tablets?|meds|medicines?|medications?|letters?|"
           r"mail|post|papers?|files?|books?|pens?|pencils?|markers?|cards?|phones?|keys?|clothes|laundry|socks?|towels?|"
           r"shirts?|shoes?|plants?|flowers?|seedlings?|herbs?|crops?|trees?|lawn|pots?|lids?|caps?|labels?|stickers?|"
           r"pallets?|panels?|sheets?|boards?|pcbs?|chips?|coins?|beads?|marbles?|buttons?|pucks?|blanks?|rods?|pipes?|"
           r"cables?|wires?|bars?|castings?|dough|bread|cakes?|pies?|bins?|baskets?|crates?)")
_TITLED = (r"(?:(?:dr|mr|mrs|ms|miss|mx|prof)\.?|"
           # a role before a name with no determiner ("give the chart to nurse kelly", "to doctor okafor"); not a thing
           # ("a robot to nurse plants")
           r"(?:nurse|doctor|doc|sister|matron|chef|coach|officer|captain|professor))\s+(?!(?:" + _NOT_DESCRIBING + "|" +
           _THINGS + "|" + _PLACE + "|" + _ANIMAL + "|" + _ROBOT_WORD + r")\b)[a-z'’-]+")
# "the surgeon", "a worker", "our packers", "the next worker", "the old man", "their kitchen helper"
_NP_DET = (r"(?:the|a|an|my|our|your|their|her|his|this|that|these|those|each|every|any|some|another|both|all(?: the)?|"
           r"one|two|three|four|five|several|many|other)")
_WHO_NP = (_NP_DET + r"\s+(?:(?!" + _NOT_DESCRIBING + r"\b)[a-z0-9'’-]+\s+){0,3}?(?:" + _ROLES + r"|morphagents?)\b" +
           _AGENT_END)
# A word that names someone by what they do ("the fitter", "the riveter", "the optometrist", "the librarian", "the
# postman", "the salesperson", "the dog trainer") is a person unless it names a machine, a container, a tool, a part of
# the robot or something else that ends the same way ("the capper", "the dishwasher", "the coffee maker", "the other")
_NOT_AGENT_END = ("washer", "dryer", "drier", "mixer", "blender", "toaster", "roaster", "fryer", "steamer", "cooker", "boiler",
                  "heater", "warmer", "chiller", "cooler", "freezer", "refrigerator", "dispenser", "container", "hanger",
                  "hamper", "folder", "divider", "spacer", "organi[sz]er", "feeder", "sorter", "sealer", "labell?er", "capper",
                  "wrapper", "palleti[sz]er", "conveyor", "conveyer", "elevator", "escalator", "lifter", "shredder", "printer",
                  "plotter", "scanner", "copier", "laminator", "computer", "monitor", "router", "charger", "adapter", "adaptor",
                  "controller", "converter", "inverter", "amplifier", "transformer", "generator", "compressor", "separator",
                  "extractor", "reactor", "incubator", "sterili[sz]er", "purifier", "humidifier", "diffuser", "vapori[sz]er",
                  "nebuli[sz]er", "ventilator", "respirator", "radiator", "actuator", "motor", "rotor", "sensor", "detector",
                  "indicator", "connector", "accumulator", "injector", "ejector", "applicator", "extruder", "grinder", "cutter",
                  "slicer", "peeler", "grater", "crusher", "baler", "compactor", "stamper", "crimper", "trimmer", "mower", "sander",
                  "planer", "jointer", "polisher", "tumbler", "shaker", "strainer", "colander", "stapler", "opener", "sharpener",
                  "screwdriver", "hammer", "spanner", "ruler", "marker", "highlighter", "pointer", "eraser", "rubber", "timer",
                  "meter", "counter", "calculator", "projector", "recorder", "register", "locker", "drawer", "dresser",
                  "cylinder", "canister", "chamber", "planter", "saucer", "pitcher", "beaker", "liner", "filter", "burner",
                  "lighter", "cracker", "burger", "sticker", "poster", "paper", "letter", "number", "border", "corner", "center",
                  "quarter", "water", "shower", "tower", "flower", "finger", "shoulder", "gripper", "roller", "lever", "cover",
                  "trigger", "ladder", "trailer", "floor", "door", "mirror", "corridor", "interior", "exterior", "error",
                  "colou?r", "slipper", "sneaker", "sweater", "jumper", "diaper", "trouser", "loafer", "dinner", "supper",
                  "butter", "pepper", "cider", "lager", "liquor", "wafer", "platter", "playlist", "checklist", "wishlist",
                  "specimen", "abdomen", "regimen", "acumen", "albumen", "stamen", "ottoman", "talisman", "caiman", "median",
                  "meridian", "assist", "persist", "consist", "insist", "resist", "desist", "subsist", "enlist", "schist",
                  "cupholder", "penholder", "headquarter", "scraper", "spreader", "sprayer", "sprinkler", "slider", "spinner", "tweezer",
                  "plier", "clipper", "nipper", "stripper", "dicer", "skewer", "poker", "sifter", "digitizer", "transmitter",
                  "plunger", "stirrer", "igniter", "toner", "buffer", "shutter", "edger", "tiller", "digger", "excavator",
                  "bulldozer", "decanter", "conditioner", "juicer", "smoker", "booster", "starter", "master", "answer",
                  "corner", "remainder", "reminder", "trimmer", "printer", "spacer", "header", "footer", "chapter", "folder",
                  "wrapper", "voucher", "sleeper", "drawer", "filler", "liner", "batter", "blister", "cluster", "polyester",
                  "barrier", "carrier bag", "stroller", "pushchair", "walker frame", "clicker", "buzzer", "beeper", "pager",
                  "rocker", "skimmer", "corer", "zester", "masher", "squeezer", "holster", "spatter", "splitter", "adapter",
                  "dropper", "eyedropper", "atomi[sz]er", "inhaler", "spacer", "applicator", "scooper", "pooper", "kicker",
                  # lab and packing machines ("move the tubes to the sequencer")
                  "sequencer", "analy[sz]er", "cycler", "sonicator", "evaporator", "homogeni[sz]er", "vortexer",
                  "pipett?or", "degasser", "desiccator", "ioni[sz]er", "strapper", "checkweigher", "embosser",
                  # machines no one is called ("move the trays to the denester", "pass the bottles to the corker"); a grader,
                  # a candler, a stacker or a deburrer may be a person, so they stay one
                  "denester", "corker",
                  # ... and a bender, a slitter, a bonder, a seamer or a destacker ("move the parts to the bender"); a
                  # loader, a bagger or a tester may be a person, so they stay one
                  "bender", "slitter", "bonder", "stepper", "uncoiler", "decoiler", "recoiler", "seamer", "dimensioner",
                  "destacker", "unscrambler", "gluer", "blaster")
_NOT_AGENT_WORD = ("holder", "maker", "tractor", "order", "matter", "character", "chapter", "summer", "winter", "october",
                   "november", "december", "september", "power", "river", "silver", "copper", "leather", "feather",
                   "weather", "powder", "solder", "plaster", "glitter", "clutter", "litter", "timber", "lumber", "polymer",
                   "bladder", "rudder", "udder", "spider", "cucumber", "lobster", "oyster", "monster", "tiger", "beaver",
                   "otter", "panther", "badger", "gopher", "condor", "alligator", "raptor", "factor", "sector", "vector",
                   "major", "minor", "prior", "manor", "decor", "armou?r", "anchor", "harbou?r", "honou?r", "labou?r",
                   "favou?r", "flavou?r", "vapou?r", "odou?r", "humou?r", "tumou?r", "razor", "scissor",
                   "tensor", "other", "another", "either", "neither", "whether", "rather", "together", "former", "latter",
                   "upper", "lower", "inner", "outer", "under", "over", "after", "never", "ever", "however", "whatever",
                   "whoever", "whichever", "wherever", "whenever", "later", "sooner", "faster", "slower", "better", "bigger",
                   "smaller", "larger", "higher", "closer", "further", "farther", "easier", "harder", "softer", "safer",
                   "cheaper", "heavier", "wider", "narrower", "thicker", "thinner", "longer", "shorter", "nearer", "newer",
                   "fewer", "lesser", "greater", "deeper", "flatter", "quieter", "louder", "hotter", "colder", "wetter",
                   "parameter", "perimeter", "diameter", "kilometer", "centimeter", "millimeter",
                   "thermometer", "barometer", "liter", "calendar", "omen", "barrier", "frontier", "terrier", "dumpster",
                   # a hand-over, a changeover or a takeover is no one ("a handover robot")
                   "handover", "hand-over", "takeover", "changeover", "crossover", "leftover", "pullover", "rollover",
                   "turnover", "hangover", "stopover", "sleepover", "makeover", "layover", "flyover", "spillover", "carryover")
_NOT_AGENT = (r"(?:[a-z'’-]*(?:" + "|".join(_NOT_AGENT_END) + r")|(?:" + "|".join(w for w in _NOT_AGENT_WORD if w) +
              r")|" + _PLACE + r"|" + r"(?:robot|robotic|arm|bot|cobot|machine|gripper|design|device|gadget|system|setup)" +
              r")s?\b")
# people_text writes such a word as this one, so the patterns below stay small: "the fitter" -> "the morphagent"
_AGENT_SHAPE = re.compile(r"(?!" + _NOT_AGENT + r")(?:[a-z][a-z-]{2,}(?:er|or|ian)s?|[a-z][a-z-]{2,}(?<![^aceiy]l)ists?|"
                          r"[a-z][a-z-]+(?:m[ae]n|persons?|people|folks?)|(?:class|school|work|team|crew|ship|play|bunk|cell|"
                          r"room|flat|house|soul|in)-?mates?|(?:farm|deck|stage|ranch)-?hands?|(?:employ|refer|retir|evacu|"
                          r"refug|interview|nomin|invit|ment|train|attend|enroll|examin|detain|pay|devot)ees?)")
# A reader after one of these words is a machine ("the card reader", "the card-reader", "the plate reader"); "the reader"
# or "the proof reader" is a person.
_READER_MACHINE = re.compile(r"(?:card|smartcard|barcode|bar-code|code|qr|rfid|nfc|badge|tag|chip|fingerprint|ebook|e-book|e|"
                             r"plate|microplate|elisa|"
                             r"kindle|screen|sd|usb|magnetic|swipe|stripe|label|disc|disk|dvd|cd|microfiche|microfilm|optical|"
                             r"pass|ticket|id)")


# ... and an engraver or an erector after one of these is a machine too ("the laser engraver", "the case erector")
_MACHINE_BEFORE = {"engraver": re.compile(r"(?:laser|cnc|rotary|fib(?:er|re)|co2|diode|uv)"),
                   "erector": re.compile(r"(?:case|carton|box|tray|crate)")}


def _machine_reader(toks: list, i: int) -> bool:
    """Whether word i is a reader that is a machine: "card reader(s)", "card-reader(s)", "e-reader", or an engraver or
    an erector that is: "the laser engraver", "the case erector"."""
    head, _, last = toks[i].group(0).rpartition("-")
    if last.rstrip("s") in _MACHINE_BEFORE:
        before = _MACHINE_BEFORE[last.rstrip("s")]
        return bool(before.fullmatch(head) if head else i and before.fullmatch(toks[i - 1].group(0)))
    if last not in ("reader", "readers"):
        return False
    return bool(_READER_MACHINE.fullmatch(head) if head else i and _READER_MACHINE.fullmatch(toks[i - 1].group(0)))
# Common first names written in lower case ("give the pills to mary"); a capitalised name is read anywhere (people_text).
# Names that are also everyday words ("will", "mark", "rose", "max", "jack") are left out.
_NAMES = (r"(?:mary|john|sarah|james|david|michael|robert|william|richard|thomas|charles|daniel|matthew|anthony|paul|steven|"
          r"andrew|kenneth|joshua|kevin|brian|george|edward|ronald|timothy|jason|jeffrey|ryan|jacob|gary|nicholas|eric|"
          r"jonathan|stephen|larry|justin|scott|brandon|benjamin|samuel|gregory|alexander|patrick|raymond|dennis|jerry|"
          r"tyler|aaron|jose|adam|henry|nathan|douglas|zachary|peter|kyle|walter|ethan|jeremy|harold|keith|noah|gerald|"
          r"carl|terry|sean|arthur|lawrence|jesse|dylan|bryan|jordan|billy|bruce|albert|willie|gabriel|logan|alan|"
          r"juan|wayne|roy|ralph|randy|eugene|vincent|russell|elijah|louis|bobby|philip|johnny|bob|tom|tommy|jim|jimmy|"
          r"joe|joey|tim|timmy|sam|sammy|dan|danny|ben|benny|alex|chris|mike|mikey|steve|dave|pete|tony|nick|matt|ted|"
          r"teddy|fred|freddie|freddy|jon|ian|liam|oliver|ollie|harry|charlie|alfie|archie|leo|theo|finn|luke|jake|jamie|"
          r"ali|omar|raj|amit|ahmed|mohammed|muhammad|mohamed|carlos|luis|miguel|pedro|diego|jorge|antonio|marco|luca|"
          r"pierre|hans|lars|sven|ivan|dmitri|olga|anna|emma|olivia|ava|sophia|isabella|mia|charlotte|amelia|harper|"
          r"evelyn|abigail|emily|elizabeth|ella|avery|sofia|camila|scarlett|victoria|madison|chloe|penelope|layla|riley|"
          r"zoey|nora|eleanor|hannah|lillian|addison|aubrey|ellie|stella|natalie|zoe|leah|hazel|audrey|bella|claire|lucy|"
          r"caroline|emilia|samantha|maya|naomi|elena|ariana|allison|gabriella|alice|madelyn|cora|eva|adeline|hailey|"
          r"isla|eliana|sadie|piper|lydia|josephine|julia|sophie|madeline|patricia|jennifer|linda|barbara|susan|"
          r"jessica|karen|nancy|lisa|betty|margaret|sandra|ashley|kimberly|donna|michelle|dorothy|amanda|melissa|"
          r"deborah|stephanie|rebecca|sharon|laura|cynthia|kathleen|amy|shirley|angela|helen|brenda|pamela|nicole|"
          r"katherine|christine|debra|rachel|catherine|carolyn|janet|maria|diane|julie|joyce|kelly|christina|"
          r"lauren|joan|judith|megan|cheryl|andrea|martha|jacqueline|frances|gloria|ann|anne|teresa|kathryn|sara|janice|"
          r"doris|judy|beverly|denise|marilyn|danielle|brittany|diana|jane|lori|tiffany|kayla|alexis|marie|ruth|edith|"
          r"mildred|ethel|agnes|gladys|irene|gertrude|beatrice|bernard|stanley|leonard|norman|herbert|clarence|howard|"
          r"harvey|kate|katie|jen|jenny|beth|liz|lizzie|meg|maggie|molly|polly|annie|susie|rosie|tilly|milly|millie|"
          r"nellie|elsie|mabel|sylvia|maureen|pauline|wendy|tracy|stacy|mandy|priya|anjali|aisha|fatima|yuki|mei|ling|"
          r"sanjay|ravi|arjun|rohan|vikram|deepak|sunita|ananya|ahmad|hassan|hussein|yusuf|ibrahim|omar|khalid|leila|"
          r"zara|noor|sofia|lucia|carmen|rosa|ana|elena|marta|giulia|francesca|sara|ingrid|astrid|freya|ebony|kiara|"
          r"tyrone|darnell|jamal|malik|keisha|latoya|shanice)")
_NOT_NO = r"(?<!\bno )(?<!\bnot )(?<!\bwithout )(?<!\bnever )(?<!\bnot any )(?<!n't any )"   # "no people around it" is no one
_WHO = (r"(?:" + _ANYONE + "|" + _KIN + "|" + _NAMES + r"|(?:elderly|disabled) [a-z]+|" + _TITLED + "|" + _WHO_NP + "|(?:(?!(?:" +
        _NOT_DESCRIBING + "|" + _PREPOSITION + r"|no|not|none|without)\b)[a-z'’-]+\s+){0,2}?" + _NOT_NO + r"(?:" + _ROLES_PL +
        r"|morphagents\b" + _AGENT_END +
        r"))\b" + _NOT_THEIR_PLACE)
# what follows the receiver in "hand the nurse the gauze", "hands the operator parts": a thing, never a job done to them
# ("give them a shake", "give the parts a quick check", but "hand the nurse the wipes" and "the labels" are things), nor a
# robot ("get me a robot that sorts screws") or a request ("give me ideas for a robot", "get me a quote")
_ACTION = (r"(?:shake|twist|tap|wipe|rinse|check|clean|spin|push|nudge|squeeze|look|go|try|turn|stir|flip|rub|polish|dust|"
           r"wash|scrub|grip|reading|hand|break|rest|chance|call|ring|lift|boost|hint|wave|name|label|bath|shower|test|tidy|"
           r"knead|count|once-over|feedback|priority|time|space|room|attention|warning|signal)")
_THING_DET = (r"(?:the|a|an|some|his|her|their|my|your|our|its|this|that|these|those|one|two|three|four|five|six|another|"
              r"more|any|each|every|all|both|enough|[0-9]+)")
# a robot asked for is no thing handed over ("give me a robot that sorts screws", "get me a design that packs boxes",
# "give me robot arms": a robot word after a/an/some or none, said to the user, or before that, which or to and a verb);
# the device, the gripper or the machine parts handed to someone are ("hand the nurse the device", "hand the nurse a
# device", "hand the operator the gripper", "a robot that hands the operator the machine parts")
_ME = r"(?:(?<=\bme)|(?<=\bus)|(?<=\byou)|(?<=\bya)|(?<=\bu)|(?<=\bmyself)|(?<=\bourselves))"   # right after the user
_ROBOT_HEAD = r"\b(?!\s+(?!(?:that|which|who|to|for|with|like|and|or|of|from|in|on|at|by)\b)[a-z0-9])"  # ends its noun phrase
_INDEF_NP = (r"\s+(?:(?:a|an|some|any|another|one|two|three|more)\s+)?(?:(?!(?:the|my|your|his|her|their|our|its|this|that|"
             r"these|those)\b)[a-z-]+\s+){0,2}?")
_NOT_A_ROBOT = (r"(?!" + _ME + _INDEF_NP + _ROBOT_WORD + _ROBOT_HEAD + r")(?!" + _INDEF_NP +
                r"(?:robot|robotic|bot|cobot)s?(?:\s+" + _ROBOT_WORD + r")?" + _ROBOT_HEAD + r")(?!\s+(?:(?:the|a|an|some|his|her|their|my|your|our|its|this|that|these|"
                r"those)\s+)?(?:[a-z-]+\s+){0,2}?" + _ROBOT_WORD + r"\s+(?:that|which|who|like|capable|able|so|to\s+(?!(?:the|a|an|"
                r"my|your|his|her|their|our|me|him|us|them)\b)[a-z]+|for\s+[a-z]+ing)\b)")
# ... and someone else given a robot will work with it ("give the operator a system that sorts parts"): only the user
# asking for one is no person next to it
_NOT_A_ROBOT_FOR_ME = (r"(?!" + _ME + r"\s+(?:" + _THING_DET + r"\s+)?(?:[a-z-]+\s+){0,2}?" + _ROBOT_WORD + r"\b)")
# A request noun is a request only when a robot, its design or its job follows ("a list of parts for a robot", "a quote
# for an arm", "an option for moving eggs", "a way to move parts", "a breakdown of the cost"): "bring me my list", "give
# me the shopping list" and "bring me something for my headache" hand a thing to someone
_JOB = r"(?:build|make|design|automat|mov|sort|stack|pick|plac|lift|grip|grasp|transfer)"   # a robot's job, not a person's
_DESIGN_WORD = (r"(?:" + _ROBOT_WORD + r"|builds?|costs?|pric(?:e|es|ing)|budget|bom|bill of materials|servos?|motors?|"
                r"actuators?|fingers?|parts? list|" + _JOB + r"[a-z]*ing|to\s+" + _JOB + r"[a-z]*)\b")
_REQUEST = (r"(?:(?:quotes?|options?|ways?|ideas?|suggestions?|plans?|estimates?|prices?|recommendations?|advice|lists?|"
            r"overview|summary|breakdown|bom)\b(?=(?:\s+(?!(?:then|and|but|so|because|when|while)\b)[\w'’-]+){0,6}?\s+" +
            _DESIGN_WORD + r")|(?:started|going)\b|(?:some|any)thing\s+(?:that|which|for|like|to)\s+(?:can\s+|will\s+)?" +
            _JOB + r"[a-z]*\b)")
_NOT_REQUEST = r"(?!\s+(?:(?:a|an|the|some|any|your|my|me)\s+)?(?:[a-z-]+\s+)?" + _REQUEST + ")"
_NO_THING = (r"(?!\s+(?:(?:a|an|some|another|one|more|good|quick|little|gentle|final|firm|proper|light)\s+)*"
             r"(?:(?!(?:the|his|her|their|my|your|our|its|this|that|these|those)\b)[a-z-]+\s+)?" + _ACTION + r"\b" +
             _PHRASE_END + r")" + _NOT_A_ROBOT + _NOT_REQUEST)
_THING = (_NO_THING + r"(?=\s+(?:" + _THING_DET + r"\b|(?!(?:" + _NOT_DESCRIBING + "|" + _ADVERB + "|" + _PREPOSITION +
          r"|home|together|apart|closer|forward|started|going|ready|to|all|both|each|separately|individually)\b|"
          r"(?:done|finished|upright|level|flat|dry|wet|clean|last)\b" + _PHRASE_END + r")[a-z][a-z'’-]*\b(?<![a-z]{3}ed)"
          r"(?<![a-z]{2}ing)))")
_THING_DET_NEXT = _NO_THING + r"(?=\s+" + _THING_DET + r"\b)"
# "bring her the cup" and "hand the operator parts", but not "bring her cup to the sink" or "pass the guest towels to the
# shelf": a word that goes on to a place things are put away in or on is the thing's, not the receiver. A room or an
# area isn't such a place ("bring her tea to the garden": she may be there), and "in" or "on" says where the person is
# or how the thing comes ("bring her tea in the garden", "pass her pills on the tray"): when unsure, the hand-over stays.
_PUT_AWAY = (r"(?:cup ?holders?|car boot|desk|table|bench|workbench|workstation|worktable|workspace|counter|shelf|shelves|"
             r"bookshelf|bookcase|cupboard|cabinet|closet|wardrobe|dresser|drawer|nightstand|locker|rack|station|bin|box|"
             r"toolbox|basket|crate|tray|bowl|cart|trolley|organi[sz]er|coaster|placemat|mat|lamp|line|conveyor|belt|machine|"
             r"robot|press|lathe|saw|printer|cnc|oven|fridge|freezer|microwave|dishwasher|sink|step|sill|windowsill|window|"
             r"vise|cell|panel|console|hook|backpack|rucksack|tote|bucket|chest|jig|fixture|charger|pegboard|holder|trunk|bag|"
             r"case|pouch|caddy|hopper|chute|pallet|dock|cradle|slot|mailbox|letterbox|garden beds?|flower beds?|raised beds?)s?")
_THEN_A_PLACE = (r"(?!\s+(?!" + _NOT_DESCRIBING + r"\b)[a-z-]+(?:\s+[a-z-]+){0,2}?\s+(?:back\s+|over\s+)?(?:to|into|onto)"
                 r"\s+(?:(?:the|a|an|my|her|his|their|our|your)\s+)?(?:[a-z-]+\s+){0,2}?" + _PUT_AWAY + r"\b)")
# an animal's thing that goes to any place, "in" or "on" one too, is the animal's: "bring the dog bowl to her porch"
_THEN_ANY_PLACE = (r"(?!\s+(?!" + _NOT_DESCRIBING + r"\b)[a-z-]+(?:\s+[a-z-]+){0,2}?\s+(?:back\s+|over\s+)?(?:to|into|onto|in|on)"
                   r"\s+(?:(?:the|a|an|my|her|his|their|our|your)\s+)?(?:[a-z-]+\s+){0,2}?" + _PLACE + r"\b)")
_HER_DIRECT = r"her\b" + _THEN_A_PLACE
_YOU = r"(?:you|u|ya|y['’]?all|youse)"
_THEM = r"(?:them|['’]em)"
_GROUP_OF = r"(?:the\s+)?(?:two|three|four|five|both|all|each|some|either|one)\s+of\s+(?:us|you)"
_GROUP_OF_THEM = r"(?:the\s+)?(?:two|three|four|five|both|all|each|some|either|one)\s+of\s+(?:us|you|them)"
_NP1_HEAD_NOT = (r"(?:" + _THINGS + "|" + _PLACE + "|" + _ROBOT_WORD + "|" + _ANIMAL + "|" + _NOT_DESCRIBING + "|" + _ADVERB +
                 "|" + _PREPOSITION + r"|" + _NOT_AGENT + r"|it|its|them|me|him|us|you|this|these|those|one|ones|all|both|"
                 r"half|each|every|some|any|none|everything|something|anything|nothing|home|ready|together|apart|forward|"
                 r"same|other|right|wrong|way|side|end|time|room|space|priority|rid|hold|help|go)\b")
# "hand the bride her bouquet", "give the morning shift their parts", "hand bob the parts", "give grampa his pills": a noun
# phrase right after a giving verb with another one after it is who gets the second, unless it is a thing, a place, the
# robot or an animal; the second must start with a determiner or a possessive ("hand the salt shaker" is one thing) and
# be no way or side ("bring the tray the long way")
_THEN_THING = (r"(?=\s+(?:the|a|an|his|her|their|my|your|our|some|this|that|these|those|its)\b)" + _NO_THING +
               r"(?!\s+(?:the|a|an)\s+(?:(?:same|other|right|wrong|long|short|quick|quickest|fastest|best|usual)\s+)?(?:way|"
               r"route|side|direction|end)\b)")
_AFTER_NP1 = (r"(?:\s+(?:in|at|on|with|by|near|from|behind|beside)\s+(?:the|a|an|her|his|their|my|our|your)\s+"
              r"(?:[a-z-]+\s+)?[a-z-]+(?=\s+(?:the|a|an|his|her|their|my|your|our)\b)|"
              r"\s+(?:on\s+(?:duty|shift|call|reception|the\s+desk|the\s+door)|in\s+charge|next\s+door|upstairs|downstairs|"
              r"(?:in|at)\s+(?:bed|room|bay|seat|table|cubicle)\s+\d+[a-z]?|over\s+there)(?=\s+(?:the|a|an|his|her|their|my|"
              r"your|our|some)\b))?")
_ANY_NP1 = (r"(?:(?:the|a|an|my|our|your|their|this|that|these|those|each|every|both)\s+(?:(?!" + _NOT_DESCRIBING +
            r"\b)[a-z0-9'’-]+\s+){0,3}?|(?!(?:pick|wash|sort|label|stitch|sew|craft|make|paint|write|fold|load|place|put|"
            r"assemble|select|check|count|draw|tie|knit|crank|dry|clean|polish|press|cut|tighten|operate|lift|grip)\w*\b)"
            r"(?!(?!" + _NAMES + r"\b)[a-z]+ly\b)(?=[a-z]))(?!" + _NP1_HEAD_NOT + r")(?![a-z'’-]*(?:ing|ed)\b)(?![0-9]+\b)"
            r"[a-z0-9][a-z0-9'’-]*\b" + _NOT_THEIRS + _THEN_A_PLACE + _AFTER_NP1 + _THEN_THING)
# "bring her the cup", "hand me": the thing follows, or nothing
_RECEIVER = (r"(?:me|him|us|" + _HER_DIRECT + "|" + _PERSON + r")\b" + _NOT_THEIRS +
             _NOT_A_ROBOT + _NOT_REQUEST)
_RECEIVER_THING = (r"(?:(?:me|him|us|" + _YOU + "|" + _GROUP_OF + "|" + _HER_DIRECT + "|" + _PERSON + "|" + _TITLED + "|" +
                   _WHO_NP + "|(?:(?!" + _NOT_DESCRIBING + r"\b)[a-z-]+\s+){0,2}?" + _ROLES_PL + r")\b" + _NOT_THEIRS + _THEN_A_PLACE +
                   _AFTER_NP1 + _THING +
                   r"|(?:" + _THEM + r"|everyone|everybody|anybody|anyone|someone|somebody)\b(?!\s+(?:all|both|each|together|"
                   r"(?:one|two|three|four|five|six|[0-9]+)\s+(?:at a time|by|at once|or more)|clean|dry|wet|upright|level|"
                   r"flat|done|finished|sorted|straight|hot|cold|warm|safe|secure|tidy|neat|empty|full|open|closed|separate|"
                   r"aligned|lined up|in order)\b)" + _THEN_A_PLACE + _THING +
                   r"|whoever(?:['’]s)?(?:\s+[a-z]+){1,3}?" + _THING_DET_NEXT + "|" + _ANY_NP1 + ")")
_RECEIVER_TO = (r"(?:(?:me|myself|him|himself|us|ourselves|" + _YOU + "|yourself|yourselves|them|themselves" +
                r")\b|" + _GROUP_OF_THEM + r"\b|" + _POSS + "|" + _WHO + "|(?:" + _KIN_SLANG + "|" + _NAMES + r")\b" + _PHRASE_END + ")")
_RECEIVER_FOR = (r"(?:(?:me|myself|him|us|" + _YOU + r")\b|her\b" + _PHRASE_END + "|" + _WHO + "|(?:" + _KIN_SLANG + "|" +
                 _NAMES + r")\b" + _PHRASE_END + ")")
# what the thing is between the verb and "to": up to seven words, hyphenated ones too ("the x-ray films", "the freshly
# washed and dried coffee mugs", "the device"), but never the start of another clause
_GAP = r"(?:(?!(?:then|while|when|whenever|until|because|if|so)\b)[\w'’-]+\s+){0,7}?"
_GIVE = (r"(?:hand(?:s|ing)?|pass(?:es|ing)?|giv(?:e|es|ing)?|bring(?:s|ing)?|(?:spoon-?)?feed(?:s|ing)?)")  # "hand me", "feed me"
_GIVE_TO = (r"(?:hand(?:s|ed|ing)?|pass(?:es|ed|ing)?|giv(?:e|es|en|ing)?|gave|bring(?:s|ing)?|brought|fetch(?:es|ed|ing)?|"
            r"get(?:s|ting)?|got|grab(?:s|bed|bing)?|toss(?:es|ed|ing)?|throw(?:s|ing|n)?|threw|offer(?:s|ed|ing)?|"
            r"serv(?:e|es|ed|ing)|feed(?:s|ing)?|fed|chuck(?:s|ed|ing)?|lob(?:s|bed|bing)?|lend(?:s|ing)?|lent|loan(?:s|ed|ing)?|"
            r"bung(?:s|ed|ing)?|sling(?:s|ing)?|slung|flick(?:s|ed|ing)?|"
            r"(?:tak(?:e|es|ing)|took)(?!\s+(?:\w+\s+)?(?:(?:a|an|one|two|three|four|five|ten|twenty|thirty|[0-9]+(?:\.[0-9]+)?|"
            r"about|around|over|under|nearly|almost|half|a few|several|many|more|less|longer|so|too|very)\s+)*(?:seconds?|"
            r"secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?|while|moment|ages|forever|long|time|turns?|"
            r"breaks?)\b)|"   # "take her a cup of tea", "take the nurse the tray"; not "it takes me 10 minutes"
            r"send(?:s|ing)?(?!\s+(?:(?:me|him|her|us|them|you|the|a|an|my|your)\s+)?(?:\w+\s+)?(?:alerts?|messages?|"
            r"notifications?|emails?|e-mails?|texts?|signals?|data|reports?|results?|photos?|pictures?|images?|videos?|"
            r"updates?|pings?|sms|notices?|warnings?|reminders?|readings?|logs?|files?)\b)|sent)")                       # "get me a drink", "serve the guests coffee", "chuck me a beer"
_TAKE_TO = (_GIVE_TO[:-1] + r"|deliver(?:s|ed|ing)?|tak(?:e|es|en|ing)|took|carr(?:y|ies|ied|ying)|present(?:s|ed|ing)?|"
            r"transfer(?:s|red|ring)?|return(?:s|ed|ing)?|dispens(?:e|es|ed|ing)|distribut(?:e|es|ed|ing)|dish(?:es|ed|ing)?|"
            r"hold(?:s|ing)?|held|slid(?:e|es|ing)|drop(?:s|ped|ping)?|mov(?:e|es|ed|ing)|push(?:es|ed|ing)?|roll(?:s|ed|ing)?|"
            r"wheel(?:s|ed|ing)?|flick(?:s|ed|ing)?)")
# handing things out to whoever is there: "hand out the gloves", "pass around the snacks", "a robot that gives out
# flyers", "hand the biscuits round"; not round an obstacle or a place ("passes around the obstacle"), out of something
# ("pass the cup out of the box") or a part that gives out
_HANDS_OUT = (r"(?:hand(?:s|ed|ing)?|pass(?:es|ed|ing)?|giv(?:e|es|en|ing)|gave|dish(?:es|ed|ing)?|dol(?:e|es|ed|ing)|"
              r"shar(?:e|es|ed|ing)|deal(?:s|t|ing)?)")
# words that are capitalised for another reason than being someone's name
_NOT_NAMES = frozenset(("i", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january",
                        "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
                        "december", "christmas", "easter", "lego", "duplo", "ikea", "coke", "pepsi", "morph", "claude",
                        "arduino", "raspberry", "pi", "feetech", "dynamixel", "robotis", "lerobot", "so", "mg996r", "sg90",
                        "ok", "okay", "hi", "hello", "hey", "thanks", "please", "dr", "mr", "mrs", "ms", "miss", "mx", "prof",
                        "reception", "assembly", "shipping", "packing", "dispatch", "inspection", "storage", "maintenance",
                        "pathology", "radiology", "pharmacy", "haematology", "hematology", "microbiology", "histology",
                        "cytology", "virology", "immunology", "biochemistry", "oncology", "cardiology", "imaging", "recycling",
                        "goods", "despatch", "stores", "purchasing", "procurement", "finance", "logistics", "production",
                        "facilities", "catering", "laundry", "sterilisation", "sterilization", "outpatients", "admissions",
                        "security", "admin", "accounts", "receiving", "returns", "quality", "qa", "qc", "starbucks", "amazon",
                        "engineering", "sterile", "services", "theatres", "theatre", "theater", "medical", "records",
                        "housekeeping", "fulfilment", "fulfillment", "legal", "compliance", "final", "phlebotomy", "ward",
                        "hr", "estates", "marketing", "sales", "payroll", "operations", "ops", "warehouse", "assembly",
                        "clinic", "pharmacy", "it", "ict", "unit", "block", "wing", "bay", "building",
                        "haas", "ups", "fedex", "dhl", "usps", "costco", "walmart", "tesco", "aldi", "lidl", "target", "apple",
                        "google", "samsung", "bosch", "makita", "dewalt", "festool",
                        # common words read as a name in a Title Case or SHOUTING tagline ("... For Fun", "... With Care")
                        "fun", "steel", "care", "pack", "ease", "speed", "precision", "style", "class", "flair",
                        "confidence", "accuracy", "efficiency", "scale", "detail"))   # never a given name (Joy, Love)
# a word that may be someone's name, after "to" or "help" at the end of its phrase ("bring the pills to siobhan", "a robot
# that helps kwame at home"): not a thing, a place, a department, a job done, an animal or the robot
# process and station words read as a name after "to" ("move the parts to rework"): not names in lower case or in a
# Title Case request, but a capital in an ordinary sentence still is one ("hand the bolts to Buffer")
_PROCESS_WORDS = frozenset((
    "rework", "quarantine", "autoclave", "decon", "decontamination", "cleanup", "rinse", "overflow", "backup", "archive",
    "salvage", "refurb", "refurbishment", "aoi", "smt", "litho", "lithography", "anneal", "degrease", "etch", "passivation",
    "teardown", "rebuild", "prep", "buffer", "kitting", "despatch", "spares", "tooling", "wip", "finishing", "plating",
    "anodising", "anodizing", "deburr", "deburring", "reflow", "triage", "metrology", "regrind", "packout", "putaway",
    "compost", "retort", "cal", "calibration", "inspection", "qc", "qa", "test", "testing", "assembly", "packing",
    "shipping", "receiving", "goods-in", "dispatch", "sterilisation", "sterilization", "wash", "drying", "curing"))
_NOT_A_NAME = (r"(?:" + "|".join(sorted(_NOT_NAMES | _PROCESS_WORDS)) + r"|position|location|spot|side|front|back|top|bottom|left|right|centre|"
               r"center|middle|end|start|stock|waste|scrap|trash|rubbish|inventory|aisle|row|lane|level|floor|stage|unit|"
               r"platform|stack|pile|heap|output|input|outfeed|infeed|reject|rejects|bed|work|school|church|market|town|"
               r"city|lunch|dinner|breakfast|size|order|scale|spec|zero|completion|taste|plan|hand|hands|place|safety|"
               r"shore|land|sea|port|camp|base|hq|life|rest|boil|cool|dry|heat|light|dark|sleep|bed|use|mind|bear|"
               r"light|attention|account|court|trial|task|work|light|icu|hdu|nicu|picu|ccu|cssd|hr|ops|er|ed|or|ae|"
               r"qa|qc|rd|pr|ict|cad|cam|lab|dept|mro|wip|fg|dc|nhs|outpatients|theatre|theatres|ward|wards|bay|"
               r"cell|zone|line|area|dock|gate|door|desk|bench|counter|table|tray|bin|box|rack|shelf|truck|van|car|"
               r"kitchen|garage|basement|attic|loft|upstairs|downstairs|outside|inside|indoors|outdoors|there|here|"
               r"everyone|everybody|nobody|none|all|both|each|others|another|other|anywhere|somewhere|nowhere|"
               r"sort|pick|move|carry|clean|fold|lift|stack|pack|load|unload|feed|water|cook|tidy|organi[sz]e|assemble|"
               r"build|make|test|inspect|measure|weigh|label|wrap|cut|paint|glue|solder|weld|screw|drill|sand|polish|"
               r"mix|pour|stir|chop|serve|deliver|transport|transfer|store|count|check|monitor|find|grab|hold|put|get|"
               r"keep|manage|run|do|automate|speed|reduce|save|prevent|avoid|maintain|repair|fix|open|close|lock|"
               r"push|pull|turn|press|hang|stand|sit|walk|eat|drink|dress|bathe|wash|shower|read|write|learn|play|"
               r"see|hear|reach|grip|grasp|place|set|lay|lower|raise|level|stay|live|cope|manage|recover|heal|focus)")
_A_NAME = (r"(?!" + _NP1_HEAD_NOT + r")(?!(?:" + _NOT_A_NAME + r")\b)(?![a-z]+(?:ing|ed|ly|tion|ment|ness|ity|s)\b)"
           r"[a-z][a-z'’-]{1,14}\b(?![’'-])(?!\.[a-z])" + _PHRASE_END)
_NOT_ROUND_A_PLACE = (r"(?!\s+(?:(?:the|a|an|this|that|its|my|your|her|his|their|our)\s+)?(?:[a-z-]+\s+){0,2}?(?:obstacles?|"
                      r"corners?|edges?|bends?|sides?|backs?|fronts?|outside)\b)(?!\s+(?:the|a|an|this|that|its|my|your|her|his|"
                      r"their|our)\s+(?:[a-z-]+\s+){0,2}?" + _PLACE + r"\b)")
HANDS_OUT = (r"\b" + _HANDS_OUT + r"\s+(?:out|around|round|away)\b(?!\s+(?:of|from)\b)" + _NOT_ROUND_A_PLACE + _THING + r"|\b" +
             _HANDS_OUT + r"\s+(?:(?!(?:then|while|when|and|or|to|from|of)\b)[\w'’-]+\s+){1,4}?(?:out|around|round)\b"
             r"(?=\s*(?:$|[.,;:!?)]|(?:to|among|amongst|at|in|during|for|when|while|every|each|and|then)\b))")
HAND_TO_PERSON = (r"\b" + _GIVE + r"\s+" + _RECEIVER + r"|\b" + _GIVE_TO + r"\s+" + _RECEIVER_THING +
                  r"|\b" + _TAKE_TO + r"\s+" + _GAP + r"(?<!\bnext )(?<!\bclose )(?<!\bdue )(?<!\baccording )to\s+(?:" + _RECEIVER_TO +
                  "|" + _A_NAME + r"|next door\b)" +
                  r"|\b(?:leav(?:e|es|ing)|left|drop(?:s|ped|ping)?\s+off|drop(?:s|ped|ping)?|deposit(?:s|ed|ing)?)\s+(?:[\w'’-]+\s+){0,5}?"
                  r"(?:off\s+)?with\s+(?:(?:me|him|her|us|you)\b|" + _WHO + ")"
                  r"|\b(?:sell(?:s|ing)?|sold|issu(?:e|es|ed|ing)|rent(?:s|ed|ing)?(?:\s+out)?|hir(?:e|es|ed|ing)\s+out|deal(?:s|t|ing)?)"
                  r"\s+(?:[\w'’-]+\s+){0,5}?to\s+(?:(?:me|him|her|us|you|them)\b|" + _WHO + ")"
                  r"|\b(?:pass|hand|give|carr(?:y|ie)|mov|slid|send|ferr(?:y|ie)|shuttl)\w*\s+(?:[\w'’-]+\s+){0,5}?between\s+(?:us\b|"
                  r"me\s+and\b|(?:the\s+)?(?:two|three|four|both|all)\s+of\s+(?:us|you|them)\b|(?:" + _WHO + r")\s+and\b)"
                  r"|\b(?:passing|delivery|deliveries|hand-?offs?|handing\s+over)\s+(?:of\s+)?(?:[\w'’-]+\s+){0,2}?(?:to|for)\s+(?:(?:me|him|"
                  r"her|us|you|them)\b|" + _WHO + ")"
                  r"|\bturn(?:s|ed|ing)?\s+(?:[\w'’-]+\s+){0,5}?over\s+to\s+" + _RECEIVER_TO +
                  r"|\b(?:brought|handed|passed|given|fetched)\s+(?:over|across|to me|to us)\b"
                  r"|\b(?:pour|scoop|ladl|plat|dish|serv|wrap|bag)\w*\s+(?:(?!(?:for|to)\b)[\w'’-]+\s+){1,6}?for\s+(?:" + _WHO +
                  r"|(?:me|him|us|you)\b)"
                  r"|\b(?:" + _WHO + r")\s+(?:needs?|wants?|asks?\s+for|calls?\s+for)\b[^.;!?]{0,50}?,?\s+(?:it|the\s+(?:robot|arm))\s+"
                  r"(?:will\s+|then\s+|just\s+)?(?:hands?|pass(?:es)?|gives?|brings?|fetch(?:es)?)\s+(?:it|one|them|that|some)\b" +
                  # "give bed 6 her meds", "bring table 3 the bill": the person in that bed or at that table
                  r"|\b" + _GIVE_TO + r"\s+(?:bed|room|table|seat|booth|bay|cubicle)\s+\d+[a-z]?(?=\s+(?:the|a|an|his|her|their|"
                  r"some|its)\b)" +
                  # "give the runners water", "hand the players water bottles": a plural word for people takes a bare thing
                  r"|\b" + _GIVE_TO + r"\s+(?:the|our|my|your|their|these|those|all(?:\s+the)?)\s+(?:(?!" + _NOT_DESCRIBING +
                  r"\b)[a-z0-9'’-]+\s+){0,2}?(?:morphagents|" + _ROLES_PL + r")\b" + _NOT_THEIRS + _THEN_A_PLACE + _THING +
                  # "hand it over", "I need parts handed over"; not "hand it over to the conveyor"
                  r"|\bhand(?:s|ed|ing)?\s+(?:(?:it|them|this|that|one|those|these|everything|the\s+[a-z-]+|[a-z]+s)\s+)?over\b"
                  r"(?!\s+(?:to|into|onto)\s+(?:the|a|an|its)\s+(?:[a-z-]+\s+){0,2}?" + _PLACE + r"\b)" +
                  r"|\b(?:fetch|get|grab|bring)\w*\s+" + _GAP + r"for\s+" + _RECEIVER_FOR +
                  # "supply the welder with rods", "keep the operator stocked with screws", "passes parts back and forth with
                  # a human"
                  r"|\b(?:(?:suppl(?:y|ies|ying|ied)|provid(?:e|es|ing|ed)|equip(?:s|ped|ping)?|furnish(?:es|ed|ing)?)\s+"
                  r"(?=(?:[\w'’-]+\s+){1,5}?with\b)|"
                  r"keep(?:s|ing)?\s+(?=(?:[\w'’-]+\s+){1,4}?(?:supplied|stocked|topped up)\s+with\b)|"
                  r"(?:hand|pass)(?:s|es|ing|ed)?\s+" + _GAP + r"(?:back and forth|to and fro)\s+with\s+)(?:(?:me|him|her|us|" +
                  _THEM + "|" + _YOU + r")\b|" + _WHO + ")" +
                  # "the person it hands the cup to", "the operator it passes parts to"
                  r"|\b(?:" + _WHO + r")\s+(?:(?:who|whom|that)\s+)?(?:it|the (?:robot|arm))\s+(?:will\s+|can\s+)?"
                  r"(?:hands?|pass(?:es)?|gives?|brings?|serves?|delivers?)\b" +
                  # "drops the part into my hand", "puts the part in the operator's hand", "recognises my hand and drops
                  # the part into it"
                  r"|\b(?:in|into|onto|on)\s+(?:my|your|her|his|their|our|(?:(?:the|a|an|my|your|her|his|their|our|each|"
                  r"every)\s+(?:(?!" + _NOT_DESCRIBING + r"\b)[a-z-]+\s+){0,2}?(?:" + _ROLES + r"|morphagents?|person|people)|"
                  r"someone|somebody|" + _KIN + "|" + _KIN_SLANG + r"|" + _ROLES_PL + r"|morphagents)['’]s?)\s+"
                  r"(?:open\s+|outstretched\s+|waiting\s+)?(?:hand|palm)s?\b|\b(?:recogni[sz]es?|detects?|sees?|finds?|tracks?)\s+"
                  r"(?:my|your|her|his|their|a person['’]s|the person['’]s|someone['’]s)\s+hands?\b|" + HANDS_OUT)
# "bring the tools to his workbench", "to grandpa's shed": the things go to a place
_TO_A_PLACE = (r"\b" + _TAKE_TO + r"\s+" + _GAP + r"(?:to|for|into|onto)\s+"
               r"(?:(?:the|a|an|my|our|your|their|her|his|its)\s+|[a-z-]+['’]s\s+)*(?:[a-z-]+\s+){0,3}?" + _PLACE + r"\b")
# "hand the dog a bone", "give my parrot seeds", "toss balls to my dog", "bring a toy for the puppy"; "the dog trainer the
# clicker" is a person, who hands-over reading finds first
ANIMAL_GETS = (r"\b(?:brush|comb|wash|groom|bath|stroke|pet|scratch|dry|dries|milk|shear|muck out)\w*\s+" + _AN_ANIMAL + "|"
               # "plays with the cat", "collects eggs from the hens", "feeds the lambs with a bottle"
               r"\bplay(?:s|ed|ing)?\s+(?:\w+\s+)?with\s+" + _AN_ANIMAL + r"|\b(?:collect|gather|take|fetch|pick)\w*\s+"
               r"(?:[\w'’-]+\s+){0,3}?(?:from|under|out from under)\s+" + _AN_ANIMAL + r"|\bfeed(?:s|ing)?\s+" + _AN_ANIMAL +
               r"|\bbottle[- ]?feed\w*\s+" + _AN_ANIMAL + "|"
               r"\b" + _GIVE + r"\s+(?:" + _AN_ANIMAL + "|" + _ANIMAL_NP + _THEN_ANY_PLACE + _THING + r")|\b" + _TAKE_TO +
               r"\s+" + _GAP + r"(?:to|for)\s+" + _AN_ANIMAL)        # not "bring the hamster cage to the table"
_NOT_THEIR_THINGS = (r"(?!\s+(?:orders?|records?|files?|notes?|samples?|data|details|charts?|forms?|labels?|ids?|"
                     r"numbers?|accounts?|returns?|feedback|requests?|enquiries|queries|complaints|lists?|database|info|"
                     r"information|reviews?|surveys?|invoices?|payments?|names?|addresses?|photos?|wristbands?|manuals?|"
                     r"guides?|uniforms?|lockers?|badges?|towels?|letters?|mail|post|parcels?|deliveries)\b)")
# Someone the robot works for, helps or gives something to is next to it: "help my grandma", "for me", "give the operator a
# break" (a hand-over's receiver too: PERSON_NEAR)
_FOR_SOMEONE = (r"\b" + _PERSON + r"\b" + _NOT_THEIR_THINGS + _NOT_THEIR_PLACE + r"|\b(?:help|assist|serve|support|accompan(?:y|ies))(?:s|es|ing|ed)?"
                r"\s+them\b|\b(?:(?:help|assist|serve|support|accompan(?:y|ies))(?:s|es|ing|ed)?\s+|(?:for|to|with)\s+|" + _GIVE_TO +
                r"\s+)(?:(?:me|him|us)\b|" + _HER_HIS + "|" + _WHO + r")" + _NOT_THEIRS + _THEN_A_PLACE + _NOT_A_ROBOT_FOR_ME +
                _NOT_REQUEST + r"|\b(?:help|assist|serve|support|look after|care for|accompan(?:y|ies))(?:s|es|ing|ed)?\s+(?:"
                + _YOU + r"\b|" + _A_NAME + ")" +
                _NOT_REQUEST +
                r"|\b(?:carers?|caregivers?|disabled|wheelchair|bedridden|patients?)\b" + _NOT_THEIR_THINGS +
                # unsure, so someone is there: "pours me a coffee", "pass the salt over here", "bring that here", "pass the
                # butter", "a robot barista in a busy cafe", "serves drinks at a bar"
                r"|\b(?:pour|make|cook|fix|prepare|brew|mix|blend|cut|slice|peel|toast|warm|heat|open|unscrew)(?:s|es|ing)?\s+"
                r"(?:me|him|her|us|them)\s+(?:a|an|the|some|my|his|her|their|our|your)\b(?!\s+(?:[a-z-]+\s+)?" + _ROBOT_WORD +
                r"\b)" + _NOT_REQUEST + r"|\b(?:bring|pass|hand|give|fetch|get|toss|slide|move|carry|send|throw)(?:s|es|ing)?\s+"
                r"(?:[\w'’-]+\s+){0,5}?(?:over\s+)?here\b|^\W*(?:please\s+)?(?:pass|hand)\s+(?:the|a|an|some|my)\s+(?:[a-z-]+\s+)?"
                r"[a-z-]+\W*(?:please)?\W*$|\b(?:busy|crowded|packed)\s+(?:cafe|café|coffee shop|shop|store|kitchen|bar|pub|"
                r"restaurant|office|lab|workshop|ward|hospital|classroom|school|line|floor|warehouse|market|canteen|cafeteria|"
                r"bakery|lobby)\b|\bserv(?:e|es|ed|ing)\s+(?:(?:the|a|an|some|hot|cold|fresh|iced)\s+)?(?:drinks?|food|meals?|"
                r"coffees?|teas?|beers?|wines?|cocktails?|snacks?|dinner|lunch|breakfast|desserts?|ice cream|soup|cake)\b|"
                r"\brobot(?:ic)?\s+(?:barista|bartender|waiter|waitress|server|butler)\b|"
                # her or his thing brought somewhere is brought for her or him: "bring her tea to the table", "bring her cup
                # to the sink" (no hand-over: the thing goes to a place, but she is there)
                r"\b(?:" + _GIVE_TO + r"|deliver(?:s|ed|ing)?|carr(?:y|ies|ied|ying))\s+(?:her|his)\s+(?!(?:own\s+)?(?:" + _PLACE +
                r")\b)(?!" + _NOT_DESCRIBING + r"\b)[a-z]|"
                # it works on someone's body: "a robot that brushes my hair", "washes grandma's feet", "shaves him"
                r"\b(?:brush|comb|wash|massag|scratch|shav|trim|cut|styl|dry|dries|bath|wip|moisturi[sz]|plait|braid|"
                r"curl|groom)\w*\s+(?:(?:my|your|his|her|their|our|someone['’]s|somebody['’]s|" + _KIN + "|" + _KIN_SLANG +
                r"|(?:the|a|an|my|your|his|her|their|our)\s+(?:[a-z-]+\s+)?(?:" + _ROLES + r"|person|morphagents?))['’]?s?\s+)"
                r"(?:[a-z-]+\s+)?(?:hair|face|teeth|back|feet|foot|hands?|nails?|arms?|legs?|skin|body|shoulders?|neck|head|"
                r"beard|lips|eyes?|ears?|scalp|toes?)\b|\b(?:brush|comb|wash|massag|shav|bath|dress|undress|groom)\w*\s+"
                r"(?:me|him|her|them|us)\b(?!\s+(?:a|an|the|some)\b)")
PERSON_NEAR = _FOR_SOMEONE + "|" + HAND_TO_PERSON
_NEAR = (r"(?:near(?:by)?|next to|around|beside|alongside|close (?:by|to)|in front of|behind|opposite|across from|between|"
         r"among|amongst|amid|amidst|adjacent(?: to)?|facing|within (?:(?:an? )?arm['’]?s?['’]? )?reach(?: of)?|"
         r"(?:an? |one )?arm['’]?s?['’]? (?:length|reach) (?:from|of)|in (?:close )?proximity (?:to|of)|"
         r"(?:a|one|two|three|half a|\d+(?:\.\d+)?)\s*(?:m|metres?|meters?|feet|foot|ft|cm|centimet(?:re|er)s?|inch(?:es)?)\s+"
         r"(?:from|away from))")
_SUBJECT_ME = r"(?:(?:i|we|he|she)(?:['’](?:m|ll|re|ve|d|s))?\b)"     # "I", "I'm", "we'll", "she's" ("you" and "they" can be things)
_SOMEBODY = r"(?:" + _SUBJECT_ME + r"|they['’](?:re|ll|d|ve)\b|" + _WHO + ")"
_IT = r"(?:it|its\s+[a-z]+|the\s+(?:robot|arm|cobot|gripper|machine)|this\s+(?:robot|arm))\b"
# people beside it in so many words: "people work next to it", "next to the operator", "a worker stands next to it",
# "I'm next to it all day", "where customers pick up their coffee", "a bench shared with two workers", "a bin I fill by
# hand", "the operator takes the part from its gripper", "an operator feeds it parts", "students will use it"
_MODAL = r"(?:(?:will|can|could|would|may|might|must|to|then|also|just)\s+)?"
PEOPLE_BESIDE = (r"\b(?:next to me|beside me|near me|" + _NOT_NO + r"with people|" + _NOT_NO + r"around people|coworkers?|"
                 r"colleagues?|hands? me|pass(?:es)? me|while i work|" + _NOT_NO + r"people around)\b|" + _NOT_NO +
                 r"\b(?:people|someone|staff|workers|operators|users|humans|others|morphagents|" + _ROLES_PL + r")\b(?:\s+\w+){0,3}?\s*" +
                 _NEAR + r"\b|"
                 # "next to the operator", "between two packers", "no one near it but me"
                 r"\b(?:" + _NOT_NO + _NEAR + r"\s+(?:to\s+)?|(?:nobody|no ?one|no-one|no people|no humans?|no kids|no children)\b[^.;!?]{0,40}?"
                 r"\b(?:except|but|apart from|aside from|other than|besides|save|bar|just|only)\s+(?:for\s+)?"
                 r"(?:(?:when|while|if|whenever|once)\s+)?)"
                 r"(?:(?:people|staff|workers|operators|users|humans|others|me|us|him)\b|" + _HER_HIS + "|" + _WHO + ")|" +
                 # someone near it, taking from it, using, loading or feeding it: "I'm right next to it", "the operator takes
                 # the part from its gripper", "students will use it", "an operator loads the fixture", "workers load the
                 # bins by hand"
                 _NOT_NO + r"\b" + _SOMEBODY + r"(?:(?:\s+[\w'’]+){0,4}?\s+(?:" + _NEAR + r"|by|past)\s+" + _IT +
                 r"|\s+" + _MODAL + r"(?:take|grab|remove|pick|collect|get|receive|pull|lift)(?:s|es|ed|ing)?\s+(?:[\w'’-]+\s+){0,4}?"
                 r"(?:from|out of|off)\s+" + _IT +
                 r"|\s+" + _MODAL + r"(?:use|touch|operate|tend|watch|supervise|program|teach|play with|work with|share|clean|"
                 r"calibrate|maintain|service|repair|adjust|check|inspect|reset|restart|start|stop|empt(?:y|ie)|clear|fix|control|"
                 r"mind|configure|monitor|oversee|refill|wipe|oil)(?:s|es|ed|d|ing)?\s+" + _IT +
                 r"|\s+" + _MODAL + r"(?:check|look|attend|set|switch|turn|keep an eye)(?:s|es|ed|ing)?\s+(?:on|after|to|up|off|"
                 r"over)\s+" + _IT +
                 r"|\s+" + _MODAL + r"(?:load|unload|feed|fill|refill|restock|tend|top up)(?:s|es|ed|ing)?\b"
                 r"|\s+" + _MODAL + r"(?:stock|sort|pack|place|put|set|lay)(?:s|es|ed|ing)?\b(?:\s+[\w'’-]+){0,4}?\s+"
                 r"(?:by hand|for it|for the robot)\b)|"
                 r"\bacross\s+(?:the\s+\w+\s+)?from\s+(?:me|us|him|her|them)\b|"
                 # "the robot and I share a workbench"
                 r"\b(?:robot|arm|cobot|it)\s+and\s+(?:i|we|me|us)\b|\b(?:i|we|me|us)\s+and\s+(?:the\s+|my\s+|this\s+)?"
                 r"(?:robot|arm|cobot)\b|" + _NOT_NO +
                 # "customers collect their orders from the tray", "the operator takes his parts"
                 r"\b(?:" + _ANYONE + "|" + _WHO_NP + "|" + _ROLES_PL + r")\s+" + _MODAL + r"(?:collect|pick(?:s|ed|ing)? up|"
                 r"take|grab|receive|fetch|get)(?:s|es|ed|ing)?\s+(?:their|his|her|the|my|our|your|its)\b|"
                 r"\b(?:robot|arm|cobot|one|it|bench|table|desk|counter|station|line|cell)\s+(?:that\s+)?(?:i|we)\s+(?:\w+\s+)?"
                 r"(?:work|sit|stand)s?\s+(?:at|on|by|beside|next to|near|with|alongside)\b|"
                 r"\bwhere\s+(?:(?:i|we)\b|" + _ANYONE + "|" + _WHO_NP + "|" + _ROLES_PL + r"\b)\s*"
                 r"(?!(?:is|are|was|were|be)\b)[a-z]|"
                 r"\bi\s+(?:fill|load|refill|restock|top up|feed)\b|"
                 # "a bench shared with two workers", "the robot is used by my disabled son"
                 r"\b(?:shared?\s+with|(?:robot|arm|cobot|machine|it)\s+(?:that\s+)?(?:\w+\s+)?(?:is\s+|are\s+)?(?:used|operated|"
                 r"tended|loaded|fed)\s+by)\s+" + _SOMEBODY)
# Someone who does something at the robot, read from how the sentence is built rather than from a list of verbs (the
# round-6 hunt: 139 of 228 "<the operator|the nurse|I|my dad> <verb> it" were missed): a person at the start of a clause,
# maybe with helping words ("is", "will", "keeps", "comes by to"), then a verb that isn't about wanting, owning, making,
# telling or being away, and the robot ("it", "its tray", "the robot") within a few words: "the operator empties it",
# "the nurse swaps its tray", "I hand it the parts and it sorts them", "staff come by to refill it", "customers walk
# right up to it", "pickers drop the totes on its conveyor"
_CLAUSE = (r"(?:^|[.,;:!?()\[\]]\s*|\s-+\s+|\b(?:and|but|so|then|while|whilst|when|whenever|if|as|because|that|which|who|"
           r"what|whatever|everything|anything|"
           r"where|until|after|before|once|since|though|although|unless|or|plus|also|think|thinks|know|knows|guess|hope|"
           r"mean|said|says|believe|reckon|expect)\s+)")
_LEAD = (r"(?:(?:in|at|on|during|every|each|after|before|once|by|around|about)\s+(?:the\s+|a\s+)?[a-z0-9]+(?:\s+[a-z]+)?,?\s+)?")
_SUBJ_I = r"(?:(?:i|we|he|she)(?:['’](?:m|re|s|ve|ll|d))?|i\s+am|we\s+are)"
_SUBJ_PP = (r"(?:\s+(?:on|at|in|from|of|by|with|behind|near|beside)\s+(?:(?:the|a|an|our|my|this|that|each|every|their|his|"
            r"her)\s+)?[a-z-]+(?:\s+[a-z-]+)?)?")
_NEGATED = r"(?!(?:\s+[\w'’]+){0,3}?\s+(?:not|never|no longer|[a-z]+n['’]t)\b)"
_AUX = (r"(?:(?:will|would|can|could|may|might|must|should|shall|do|does|did|is|are|was|were|am|has|have|had|been|be|"
        r"being|then|also|just|always|usually|often|sometimes|only|still|regularly|occasionally|now|later|first|even|"
        r"really|normally|typically|generally|mostly|constantly|frequently|actually|probably|all|both|each|"
        r"['’](?:m|re|s|ve|ll|d)|keeps?|kept|starts?|started|tries|try|tried|needs?\s+to|has\s+to|have\s+to|had\s+to|"
        r"got\s+to|gotta|gets?\s+to|going\s+to|gonna|wants?\s+to|used\s+to|likes?\s+to|able\s+to|"
        r"(?:do|does|did|is|are|was|were|has|have|had|would|could|should|wo|ca|must|need)n['’]t|cannot|not|never)\s+)")
_WANT_OWN = (r"(?:want|need|like|love|hate|prefer|wish|hope|expect|think|thought|guess|know|knew|believe|imagine|suppose|plan|"
             r"intend|mean|meant|build|built|make|made|design|print|3d|buy|bought|order|sell|sold|have|has|had|own|got|get|"
             r"gets|find|found|call|name|see|saw|seen|describe|said|say|tell|told|ask|afford|pay|paid|fund|let|trust|"
             r"am|is|are|was|were|be|been|being|do|does|did|can|could|will|would|shall|should|may|might|must|"
             r"wonder|doubt|reckon|decide|choose|pick out|research|google|search|look for|wait for|assum|feel|felt|"
             r"understand|understood|realis|realiz|notic|remember|forget|forgot|read|heard|hear|learn|learnt|worry|"
             r"suspect|bet|figure|calculat|estimat|guarantee|promise|explain|mention|show|shown|seem|appear|consider)"
             r"(?:s|es|ed|d|ing)?")
_AWAY = (r"(?:leav(?:e|es|ing)|left|go|goes|going|went|gone|sleep(?:s|ing)?|slept|asleep|nap(?:s|ping)?|away|out|abroad|"
         r"elsewhere|absent|travel\w*|commut\w*|at\s+(?:work|school|the office|church|the gym|the shops)|"
         r"on\s+(?:holiday|vacation|leave|a trip|the road|nights|(?:a\s+|their\s+|his\s+|her\s+|my\s+|our\s+)?(?:lunch\s+)?break|"
         r"lunch)|off\s+(?:duty|shift|work|sick)|not\s+(?:home|here|there|around|in))")
_VERB = (r"(?!(?:" + _WANT_OWN + "|" + _AWAY + "|" + _NOT_DESCRIBING + "|" + _PREPOSITION + "|" + _ADVERB +
         r"|nor|this|these|those|some|any|no|not|its|our|your|their|me|us|them|him)\b)[a-z][a-z'’-]*\b")
_THE_ROBOT = (r"(?:it|its|itself|the\s+(?:robot|arm|cobot|machine|gripper|bot)s?|this\s+(?:robot|arm|machine|cobot)|"
              r"the\s+robot['’]s)\b")
_SUBJ = (r"(?!(?:that|which|this|these|those)\b)(?:" + _SUBJ_I + "|" + _WHO + r"|" + _NOT_NO + r"(?:(?:" + _NP_DET + r")\s+(?:(?!" + _NOT_DESCRIBING +
         r"\b)[a-z0-9'’-]+\s+){0,2}?)?morphagents?\b)")
PEOPLE_AT_IT = (_CLAUSE + _LEAD + _SUBJ + _SUBJ_PP + _NEGATED + r"\s+" + _AUX + r"{0,4}" + _VERB +
                r"(?:\s+[\w'’-]+){0,8}?\s+" + _THE_ROBOT + "|" +
                # "holds the board while I solder", "sorts mail while the receptionist answers the phone", "as the
                # packers seal them"; not "while I sleep", "while we're out", "when I'm at work", "as I said"
                r"\b(?:while|whilst|as|when|whenever|as soon as|each time|every time)\s+" + _SUBJ + _SUBJ_PP +
                r"(?!(?:\s+[\w'’]+){0,2}?\s+(?:not|never|[a-z]+n['’]t)\s+(?:home|here|there|around|in|at|on|nearby|close|about)\b)"
                r"\s+" + _AUX +
                r"{0,3}(?!(?:" + _AWAY + r"|said|say|mentioned|explained|described|asked|wrote|know|knew|see|thought|"
                r"expected|planned|wanted|needed|requested|promised|discussed|agreed|suggested|will|would|can|could|may|might|must|should|shall|do|does|did|is|are|was|were|am|has|have|had|been|be|being|"
                r"['’](?:m|re|s|ve|ll|d))\b)[a-z][a-z'’-]*\b(?![^.;,!?]{0,40}?\b(?:in|from)\s+(?:the\s+other|another|a\s+different|"
                r"the\s+next)\s+room\b)|"
                # "a robot the nurses can use", "a robot that the technicians maintain", "a robot I can play catch with"
                r"\b(?:robot|arm|cobot|machine|bot|one|gripper|device)s?\s+(?:that\s+|which\s+)?" + _SUBJ + _NEGATED + r"\s+" +
                _AUX + r"{0,3}" + _VERB + "|" +
                # "it'll be used by apprentices", "operated by students", "shared by the whole team"
                r"\b(?:used|operated|run|shared|maintained|tended|loaded|unloaded|fed|filled|refilled|emptied|cleaned|"
                r"supervised|watched|monitored|programmed|controlled|serviced|calibrated|checked|inspected|handled|touched|"
                r"reset|restarted|cleared|stocked|restocked|guided|driven|pushed|moved|carried|held|attended|staffed|manned|"
                r"accompanied|minded|overseen|set up|worked)\s+by\s+(?:me|us|him|her|" + _WHO + ")|" +
                # the robot takes or collects things from someone: "takes the parcel from the postman", "collects the
                # trays from the residents", "takes the phone from my hand", "takes the strain off my back"
                r"\b(?:tak(?:e|es|ing|en)|took|collect\w*|receiv\w*|accept\w*|grab\w*|fetch\w*|retriev\w*|gather\w*|"
                r"catch\w*|caught|get|gets|getting|pick(?:s|ed|ing)?\s+up)\s+(?:[\w'’-]+\s+){0,5}?(?:from|out of|off)\s+"
                r"(?:(?:me|him|us|you)\b|" + _HER_HIS + "|" + _WHO + r"|(?<=from )them\b(?!\s+(?:all|both|each)\b)|"
                r"(?:my|your|his|her|their|our|someone['’]s|a person['’]s|the person['’]s|people['’]s)\s+(?:[a-z-]+\s+)?"
                r"(?:hands?|palms?|grip|fingers?|lap|back|arms?|shoulders?|mouth|plate)\b)|" +
                # "I sit right there", "I'm in the same room", "on the same bench as the technician", "with 20 people"
                r"\b" + _SUBJ_I + r"\s+(?:\w+\s+){0,2}?(?:sit|stand|work|stay|wait|be|am)\w*\s+(?:right\s+)?"
                r"(?:there|here|nearby|close by|close)\b|\b" + _SUBJ_I + r"(?:\s+\w+){0,3}?\s+(?:in|at|on)\s+the\s+same\s+"
                r"(?:room|space|bench|table|desk|workbench|line|area|cell|lab|workshop|kitchen|office|floor)\b|"
                r"\b(?:in|on|at)\s+the\s+same\s+(?:room|space|bench|table|desk|workbench|line|area|cell|floor|counter)\s+as\s+"
                r"(?:me|us|him|her|" + _WHO + r")|\b(?:by|at)\s+(?:my|your|his|her|their|our)\s+side\b|"
                r"\b(?:with|alongside|among)\s+(?:\d+|two|three|four|five|six|ten|twenty|several|many|lots of|a few|dozens of|"
                r"a team of)\s+(?:other\s+)?(?:people|morphagents|" + _ROLES_PL + r")\b|"
                # someone it follows or shadows: "follows the nurse around", "shadows the technician"
                r"\b(?:follow|shadow|accompan(?:y|ie)|tail|buddy up with|buddies up with|escort)\w*\s+(?:me|us|him|her|" + _WHO +
                ")|(?<!made )(?<!built )(?<!designed )(?<!written )(?<!supplied )(?<!produced )(?<!created )(?<!invented )"
                r"(?<!developed )(?<!owned )(?<!funded )(?<!approved )(?<!sold )(?<!shipped )(?<!printed )(?<!sent )"
                r"\bby\s+(?:" + _HER_HIS + "|" + _WHO + ")" + _NOT_THEIRS)
# the word after a person that is their verb: after a helping word, or one shaped like a verb ("needs", "asked", "took",
# "swap"), never a noun the person word describes ("the machine operator manual goes in the drawer")
_VERB_SHAPED = (r"(?=[a-z'’-]+(?:s|ed|ing)\b|(?:took|put|held|brought|gave|came|sat|stood|ran|got|fed|led|met|kept|caught|threw|"
                r"drew|wrote|ate|drank|stuck|hung|swept|wore|" + _VERB_BASE + r"|shop|eat|drink|arrive|queue|order|pay|browse|"
                r"line up|visit|play|cook|scan|tap|swipe|call|ask|bend|lift|carry|sit|stand|wait|watch|walk|come)\b)")
# Places and jobs that leave people around it when the words name no one ("it works in a care home", "a robot that
# waits tables", "a cobot for our packing line", "pass the salt down the table"), and someone in a clause of their own
# ("if a customer wants a leaflet, it gives one", "the welder needs rods, the robot supplies them", "so the operators
# don't have to bend"): unsure, so the person is there
# a place that belongs to someone named ("put the parts into the operator's tray", "place the pills on dad's bedside
# table") is no hand-over, but it leaves them likely nearby: unsure, so a person, assumed ("her room" stays no person)
THEIR_PLACE = (r"\b(?:" + _KIN + "|" + _KIN_SLANG + "|" + _ROLES + "|" + _NAMES + r"|morphagents?|someone|somebody|person|"
               r"people)" + _OWNS + _A_PLACE)
PEOPLE_AROUND = (_CLAUSE + _LEAD + r"(?:" + _WHO + ")" + _SUBJ_PP + r"\s+(?:" + _AUX + r"{1,4}|" + _VERB_SHAPED +
                 r")(?!(?:" + _AWAY + "|" + _THINGS +
                 r"|" + _NOT_DESCRIBING + "|" + _PREPOSITION + "|" + _ADVERB + r"|nor|this|these|those|some|any|no|not|"
                 r"will|would|can|could|may|might|must|should|shall|do|does|did|is|are|was|were|am|has|have|had|been|be|"
                 r"being|['’](?:m|re|s|ve|ll|d)"
                 r"|orders|records|files|notes|forms|requests|returns|badges|charts|data|details|samples|meals|sharks|"
                 r"lockers|rooms|areas|stations)\b)[a-z][a-z'’-]*\b|" +
                 _CLAUSE + _LEAD + r"(?:" + _WHO + r")\s+(?:do|does|did|wo|ca|need)n['’]t\s+(?:have|need)\s+to\b|"
                 r"\bso\s+(?:that\s+)?" + _SUBJ_I + r"\s+(?:don['’]t|do not|won['’]t|no longer|doesn['’]t|never)\s+(?:have|has|need|needs)"
                 r"\s+to\s+(?:bend|lift|reach|stoop|kneel|climb|stretch|get up|stand up|walk|carry|strain)\b|"
                 r"\b(?:care|nursing|retirement|residential|old people['’]?s|old folks['’]?) homes?\b|\bhospices?\b|\bpubs?\b|"
                 r"\b(?:hair |nail |beauty )?salons?\b|\bbarber(?:s|shop)?\b|\brestaurants?\b|\b(?:operating )?theatres?\b|"
                 r"\boperating (?:room|table)s?\b|\bwards?\b(?!\s+off)|\b(?:demo|exhibit\w*|trade ?show|expo|our|my|fair|event|"
                 r"stand|ticket|information|info)\s+booths?\b|\b(?:science |trade |craft )?fairs?\b|"
                 r"\bexpos?\b|\bexhibitions?\b|\bopen days?\b|"
                 r"\bwait(?:s|ing)?\s+(?:on\s+(?!it\b)|tables?\b)|\btend(?:s|ing)?\s+(?:the\s+)?bar\b|"
                 r"(?<!\bis )(?<!\bare )(?<!\bbe )(?<!\bwas )\bserv(?:e|es|ed|ing)\b(?!\s+(?:as|by|(?:a|an|its|the|no|two|one)\s+"
                 r"(?:\w+\s+)?purpose|to\s+(?:hold|keep|stop|show|mark|separate)|(?:tray|spoon|plate|dish|bowl|platter|size|"
                 r"suggestion|hatch|utensil|ladle|board)s?\b))|"
                 r"\b(?:works?|working|man|mans|staff|staffs)\s+(?:the|a)\s+(?:till|counter|register|checkout|bar|door|"
                 r"front desk|reception|kiosk|stall)\b|\b(?:at|behind)\s+the\s+(?:counter|bar|till|checkout|register|"
                 r"reception|front desk)\b|\bdeal(?:s|t|ing)?\s+(?:out\s+)?(?:the\s+)?cards\b|\bcard[- ]dealing\b|"
                 r"\bvending\b|\bsell(?:s|ing)?\s+(?!(?:for|at|under|over|out|well|online|fast|quickly|on)\b)[a-z]|\b(?:issu(?:e|es|ing)|distribut(?:e|es|ing|ion of))\s+(?:the\s+)?(?:post|mail|"
                 r"leaflets?|flyers?|food|meals?|medicine|meds|medication|pills|drinks?|papers?|newspapers?|tickets?|badges?|"
                 r"ppe|gloves|masks?|keys?|tools?|snacks?|samples?|towels?|uniforms?|blankets?|water)\b|\bhands?\s+back\b|"
                 r"\bbag(?:s|ging)?\s+(?:the\s+)?groceries\b|\bcocktails?\b|\b(?:brings?|takes?|goes|go|pushes)\s+round\b|"
                 r"\b(?:pass|send|slide|hand)\w*\s+(?:[\w'’-]+\s+){0,4}?(?:down|across|around|round|along)\s+the\s+table\b|"
                 r"\b(?:to|at)\s+the\s+other\s+end\s+of\s+the\s+table\b|\b(?:down|across|along)\s+to\s+the\s+other\s+end\b|"
                 r"\baround\s+the\s+room\b|"
                 # "serves table 3", "carries the soup to table 3", "take the meds to bed 4", "a robot for table 3": a person
                 # served at a numbered table, bed or room, whatever the verb (when unsure, keep the person). Not one "of the
                 # greenhouse" or other station; person_signals also drops it in a greenhouse or workshop (_STATION_PLACE)
                 r"\b(?:(?:to|for|at)\s+(?:the\s+|their\s+)?(?:table|bed|room|seat|cubicle|booth)|(?:serves?|bring\w*|tak\w*|"
                 r"deliver\w*|wait\w*\s+on)\s+(?:[\w'’-]+\s+){0,4}?(?:the\s+|their\s+)?(?:table|bed|room|seat|cubicle|booth|bay))"
                 r"\s+\d+[a-z]?\b(?!\s+of\s+(?:the\s+)?(?:greenhouse|garden|nursery|"
                 r"workbench|bench|workshop|factory|warehouse|line|rack|shelf|polytunnel|plot|bed|allotment|cell|unit))|"
                 r"\bthe\s+tables\b(?=.*\b(?:restaurant|cafe|café|bistro|canteen|pub|bar)s?\b)|"
                 r"\b(?:beside|next to|by|at)\s+(?:the|a|her|his|my|their)\s+(?:hospital\s+)?bed\b|"
                 r"\b(?:robot(?:ic)?|cobot)\s+(?:server|cashier|receptionist|nurse|carer|caregiver|companion|sous[- ]chef|chef|"
                 r"host|hostess|concierge|teacher|tutor|assistant|helper|attendant|valet|porter|bellhop|baristas?|bartenders?|doorman|"
                 r"doormen|greeters?|ushers?|bouncers?|"
                 r"waiters?|waitress(?:es)?)\b|"
                 r"\b(?:cobots?|collaborative (?:robot|arm)s?|human[- ]robot|hrc|helping hands?|third hand|assistive|adl|"
                 r"activities of daily living|daily living|independent living|power ?chairs?|mobility aids?|hospital beds?|"
                 r"bed ?rails?|feeding (?:arm|robot|aid|assistant|device|tool)s?)\b|"
                 r"\bplay(?:s|ing)?\s+(?:\w+\s+){0,2}?(?:with|against)\s+(?:me|us|him|her|you)\b|"
                 r"\b(?:between|for|with|among)\s+(?:the\s+)?(?:two|three|four|both|all)\s+of\s+(?:us|you|them)\b|"
                 r"\bfree(?:s|d|ing)?\s+up\s+(?:" + _WHO + "|me|us|him|her)|"
                 r"\b(?:whoever|someone|somebody|anyone|anybody|everyone|everybody)['’]s\s+(?:there|here|around|nearby|in|at|on|"
                 r"by|near|next|working|sitting|standing|using|waiting)\b|"
                 # the robot sits, lives or is clamped on someone's bed, chair, tray table or desk: they are next to it ("it
                 # sits on grandma's tray table", "it'll live on my bedside table", "a robot arm clamped to my bed rail")
                 r"\b(?:sits?|sitting|sat|lives?|living|stands?|standing|stays?|mounted|clamped|fixed|attached|bolted|screwed|"
                 r"goes|go|be|kept|placed)\s+(?:on|to|at|beside|next to|by|onto|over|on the side of)\s+(?:my|our|your|her|his|"
                 r"their|(?:" + _KIN + "|" + _KIN_SLANG + r"|someone|(?:the|a)\s+(?:" + _ROLES + r"|morphagents?))['’]s?)\s+"
                 r"(?:[a-z-]+\s+){0,2}?(?:bed|bedside table|tray table|over-?bed table|nightstand|bed ?rail|wheelchair|chair|"
                 r"armchair|recliner|sofa|couch|desk|lap|dining table|kitchen table)s?\b")
# "it works on its own", "an enclosed cell", "the robot runs alone": no one next to it, unless a person is named. "Alone"
# said of a person ("she lives alone", "while he is alone") says nothing about the robot.
ROBOT_ALONE = (r"\b(?:on its own|by itself|unattended|enclosed|fenced(?: off| in)?|caged|lights[- ]out)\b|"
               r"\b(?:it|robot|arm|machine|cobot)\s+(?:\w+\s+){0,2}?alone\b|"
               r"(?<!\bshe )(?<!\bhe )(?<!\bi )(?<!\bwe )(?<!\bthey )(?<!\bwho )(?<!\byou )"
               r"\b(?:works?|working|operates?|operating|runs?|running)\s+alone\b")
# "nobody nearby", "no one works next to it"; not "no one near it but me", "no kids near it, just me", "no one works next
# to it except the operator"
NOBODY_NEAR = (r"\b(?:nobody|no ?one|no-one|no people|no humans?|no kids|no children)\b(?!\s+else\b)(?:\s+\w+){0,3}?\s*" + _NEAR +
               r"\b(?!(?:\s+(?:it|the robot|the arm|this))?\s*[,;:-]?\s*(?:except|but|apart from|aside from|other than|"
               r"besides|save|bar|just|only|barring|excluding)\b)")
# a bare "cobot"/"collaborative robot" hint is what the filter reads as people when no one is named: unsure, not a
# described person. An explicit "nobody around" beats it (see person_signals), but never a named person, and never a
# robot whose user is at it by construction (a power chair, hospital bed or feeding arm, assistive, ADL or mobility robot,
# helping hands): those keep the person and the e-stop whatever else the request says.
_COBOT_UNSURE = r"\b(?:cobots?|collaborative (?:robot|arm)s?|human[- ]robot|hrc)\b"
# a numbered bed, table or room that is a station, not someone served: in a greenhouse, nursery or workshop
# someone the robot serves or tends at arm's length, named by what it does to or for them: a double-object serving verb
# ("pours us coffee", "makes Tom a sandwich"), a drink topped up, body care ("paints my nails", "puts in my eye drops",
# "ties my shoelaces", "puts the straw in her mouth"), greeting visitors, their homework collected, or "whose bin I empty"
_POSS_WHO = r"(?:my|his|her|their|your|our|someone['’]s|(?:the\s+)?[a-z]+['’]s|(?:the\s+)?[a-z]+s['’](?!s))"
# food and drink served to people
_SERVED = (r"(?:coffees?|teas?|drinks?|beers?|wines?|juices?|milk|water|sodas?|colas?|lemonades?|smoothies?|milkshakes?|"
           r"lattes?|cappuccinos?|espressos?|cocoa|hot chocolates?|cocktails?|snacks?|food|meals?|lunch(?:es)?|dinners?|"
           r"breakfasts?|brunch(?:es)?|sandwich(?:es)?|cakes?|cookies?|biscuits?|pizzas?|soups?|desserts?|ice[- ]?creams?|"
           r"popcorn|fruit|pastr(?:y|ies)|muffins?|donuts?|doughnuts?|bagels?|toast|cereal|crisps|sweets|cand(?:y|ies)|"
           r"chocolates?|refreshments?|beverages?|nibbles|canap[eé]s|appeti[sz]ers?|starters?|platters?)")
_SERVES_SOMEONE = (
    r"\b(?:deliver|pour|make|makes|made|cook|fix|fetch|slide|serv(?:e|es|ed|ing)|bring|brings|brought|offer|pass|hand|"
    r"give|gives|gave|get|gets|got)\w*\s+(?:(?:me|us|him|her|them|you|someone|the\s+(?:[a-z-]+\s+)?(?:" + _ROLES + r"|morphagents?))\s+"
    r"(?:a|an|some|the|their|his|her|my|your|our|more|another|fresh|hot|cold|\d+)|(?:me|us|him|you|someone|the\s+"
    r"(?:[a-z-]+\s+)?(?:" + _ROLES + r"|morphagents?))\s+(?!(?:to|for|at|in|into|on|onto|over|back|up|down|out|off|away|along|across|"
    r"around|round|through|with|from|and|or|then|while|when|so|by|near)\b)(?![^.;!?]*\b(?:to|into|onto)\s+(?:the|a|its|their)\s))"
    r"\s*(?!(?:shake|turn|go|try|look|rest|wipe|clean|spin|push|nudge|squeeze|tap|robot|robotic|arm|bot|cobot|machine|"
    r"gripper|design|device|gadget|system|setup|plan|quote|list|way|idea|bom|estimate|price|summary|version|name|sketch|"
    r"model|drawing|parts?\s+list|hand)s?\b)[a-z]{3,}"
    # "them" served food or drink are people: "delivers them coffee", "carries them their lunch", "makes them tea"
    # ("deliver them to the bin" moves things)
    r"|\b(?:deliver|carr(?:y|ies|ied|ying)|tak(?:e|es|ing)|took|bring|brought|serv|pour|mak(?:e|es|ing)|made|fix|fetch|"
    r"get|got|giv|gave|hand|pass|offer|cook|prepar|brew|mix|send|sent|toss)\w*\s+(?:them|['’]em)\s+(?:(?:a|an|some|their|"
    r"fresh|hot|cold|iced|warm|more|another|two|three|[0-9]+|(?:cups?|glass(?:es)?|mugs?|plates?|bowls?|bottles?|cans?|"
    r"pints?|slices?|rounds?)\s+of)\s+)*(?:[a-z-]+\s+)?" + _SERVED + r"\b(?![-'’])(?!\s+(?:filters?|grounds|beans|pods?|"
    r"capsules?|stains?|machines?|makers?)\b)"
    r"|\b(?:top|tops|topping|topped)\s+up\s+" + _POSS_WHO + r"\s+(?:[a-z-]+\s+)?(?:glass|cup|mug|coffee|tea|drink|wine|water|"
    r"plate|bowl)(?:s|es)?\b"
    r"|\b(?:refill|fill)\w*\s+(?:up\s+)?" + _POSS_WHO + r"\s+(?:[a-z-]+\s+)?(?:glass|cup|mug|coffee|tea|drink|wine|water|"
    r"plate|bowl)(?:s|es)?\b"
    r"|\b(?:paint|polish|file|trim|brush|comb|wash|dry|dries|style|braid|plait|shav|massag|do|does|doing|appl)\w*\s+"
    + _POSS_WHO + r"\s+(?:[a-z-]+\s+)?(?:nails?|fingernails?|toenails?|hair|teeth|makeup|make-up|face|lips|eyebrows?|lashes|"
    r"beard|back|feet|eye\s?liner|lipstick)\b"
    r"|\b(?:tie|ties|tying|tied|lace|laces|lacing|button|buttons|buttoning|zip|zips|zipping|fasten\w*|do\w*\s+up)\s+"
    r"(?:up\s+)?" + _POSS_WHO + r"\s+(?:[a-z-]+\s+)?(?:shoelaces|laces|shoes|boots|buttons?|coat|jacket|shirt|bib|apron)\b"
    r"|\bput\w*\s+(?:in|on)\s+" + _POSS_WHO + r"\s+(?:eye\s?drops|contacts?|contact\s+lenses|lenses|glasses|hearing\s+aids?|"
    r"shoes|socks|gloves|coat|bib)\b"
    r"|\bput\w*\s+(?:a|an|the|some|my|his|her|their)\s+(?:[a-z-]+\s+){0,2}?(?:in|into|on|onto|around|round)\s+" + _POSS_WHO +
    r"\s+(?:mouth|head|neck|face|lap|feet|foot|ears?|eyes?|wrists?|hands?|arms?)\b"
    r"|\b(?:greet|welcom|usher|seat|check\w*\s+in|receiv|meet|wave\w*\s+(?:at|to))\w*\s+(?:the\s+|our\s+|my\s+)?"
    r"(?:[a-z-]+\s+)?(?:customers?|visitors?|guests?|patients?|people|everyone|staff|kids|children|students|pupils|diners|"
    r"shoppers|passengers|delegates|callers|clients|members|residents)\b"
    r"|\b(?:the|my|our|their)\s+(?:students|pupils|kids|children|guests|customers|patients|workers|staff|visitors|players|"
    r"diners|residents|clients|passengers|shoppers)['’]\s+(?:[a-z-]+\s+){0,2}?(?:cups?|glass(?:es)?|mugs?|plates?|bowls?|"
    r"drinks?|faces?|hands?|hair|nails?|homework|orders?|tickets?|coats?|trays?|meals?|lunch(?:es)?|food)\b"
    r"|\bwhose\s+[a-z-]+\s+(?:i|we|you|he|she|they|someone)\s+(?:empty|empties|fill|fills|load|loads|refill|refills|clean|"
    r"cleans|change|changes|swap|swaps|check|checks|feed|feeds|top)\b")
_STATION_PLACE = (r"\b(?:greenhouses?|polytunnels?|allotments?|seedlings?|seed trays?|potting|hydroponics?|grow (?:room|tent)s?|"
                  r"workshops?|workbench(?:es)?|kilns?|machine shop|cnc|lathes?|press shop)\b")
_NUMBERED_PLACE = r"\b(?:table|bed|room|seat|cubicle|booth|bay)\s+\d+[a-z]?\b"
_KIDS = (r"\b(?:kids?(?!['’])|kiddos?|kiddies|children(?!['’]s)|schoolchildren|grandchildren|grandkids?|schoolkids?|child|toddlers?|bab(?:y|ies)|"
         r"infants?|newborns?|twins|teens?|teenagers?|tweens?|youngsters?|my son|my daughter|"
         r"(?:the|a|my|our|her|his|their|little|young) (?:boy|girl)s?|"
         r"grand(?:sons?|daughters?)|god(?:sons?|daughters?)|foster (?:sons?|daughters?|kids?|child(?:ren)?)|pupils|"
         r"(?:my|our|her|his|their) (?:little|baby|younger|kid) (?:brother|sister)s?|"
         r"(?:my|our|her|his|their) (?!(?:grown|adult|older|eldest|oldest|married|grown-up)\b)(?:[a-z-]+ ){1,2}"
         r"(?:son|daughter)s?(?! in law\b)(?!-in-law)|(?:the|our|my|their|her|his) little ones|"
         r"robot(?:ic)? (?:babysitter|nanny|childminder|playmate)|"
         r"(?:my|our|her|his|their|the|a)\s+(?:[1-9]|1[0-2]|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
         r"\s*-?\s*(?:yo|y/o|yrs?[- ]old|years?[- ]old|year-olds?)"
         r"(?!\s+(?:(?:" + _ANIMAL + r"|robot|arm|machine|design|car|house|tree)\b)))\b")
# a role or a title before a capitalised name is part of the name: "Nurse Jones", "Doctor Patel", "Chef Marco"
_TITLE_WORDS = (r"(?:Nurse|Doctor|Doc|Dr|Chef|Professor|Prof|Father|Mother|Sister|Brother|Captain|Officer|Coach|Uncle|Aunt|"
                r"Auntie|Aunty|Grandma|Grandpa|Granny|Nan|Nana|Gran|Mr|Mrs|Ms|Miss|Mx|Sir|Dame|Lady|Lord|Rev|Reverend|"
                r"Pastor|Rabbi|Imam|Judge|Sergeant|Sgt|Detective|Inspector|Chief|Madam|Madame|Teacher|Coach|Agent|Pilot)")
# a few misspellings common enough to matter: "teh nurse", "brng me a beer", "pas me the salt"
_TYPOS = ((r"\bteh\b", "the"), (r"\bn(?:ures|ruse|usre|urce|ursse)(s?)\b", r"nurse\1"), (r"\bdoct(?:er|r)(s?)\b", r"doctor\1"), (r"\b(?:brng|brign|bribg)\b", "bring"), (r"\b(?:brngs|brigns)\b", "brings"),
          (r"\bpas\b", "pass"), (r"\b(?:hnd|hadn|hnad)\b", "hand"), (r"\b(?:gve|giev)\b", "give"), (r"\bgimme\b", "give me"),
          (r"\bgivs\b", "gives"),
          (r"\bop(?:ertor|rator|erater|perator|eratr)(s?)\b", r"operator\1"), (r"\basist(s?)\b", r"assist\1"),
          # contractions typed without an apostrophe ("im next to it", "ive got to load it", "theyre beside it"); "ill",
          # "id" and "were" are words too, so only where a person's verb or a place next to it follows
          (r"\bi m\b", "i'm"), (r"\bim\b", "i'm"), (r"\bive\b", "i've"), (r"\bhes\b", "he's"), (r"\bshes\b", "she's"),
          (r"\btheyre\b", "they're"), (r"\bwere(?=\s+(?:stood|standing|sat|sitting|working|always|right|next|beside|near|"
                                      r"by|in front|there|here|going|gonna)\b)", "we're"),
          (r"(?:^|(?<=[.,;:!?]\s))\bwere\b(?=\s+(?:all\s+)?[a-z]+ing\b)", "we're"),
          (r"\bill(?=\s+(?:be|have|stand|sit|work|stay|load|use|watch|need|always|probably|just)\b)", "i'll"),
          (r"\bid(?=\s+(?:be|have|stand|sit|work|stay|need|like|want|probably|just)\b)", "i'd"))


_RX_LOCK = threading.Lock()


@functools.lru_cache(maxsize=None)
def _compiled(pattern: str) -> re.Pattern:
    return re.compile(pattern)


def _rx(pattern: str) -> re.Pattern:
    """A long pattern compiled once per process (Python's own cache of compiled patterns can drop them). One thread
    compiles it while another that needs it waits, so warm() at Studio start and a first request never both do."""
    with _RX_LOCK:
        return _compiled(pattern)


def people_text(raw: str, agents: bool = True) -> str:
    """`raw` in lower case with each capitalised name written "someone" ("pass Sarah the salt", "give the pills to Mary",
    "hand Dr Smith the clamp", "hand Nurse Jones the gauze"), so the receiver rules read the name as a person, and a few
    common misspellings mended ("hand teh nurse the gauze"). A capital that starts a sentence, follows "the" or "a", or
    names a place, a person word, a department or a known product is no name, and Title Case or SHOUTING says nothing
    about names at all (unsure: a name read where there is none only adds a hand-over's emergency stop). With
    `agents` false, a word for someone by what they do stays as written (the harm filter reads "the weed killer")."""
    raw = raw or ""
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*", raw)
    if words and sum(w[0].isupper() for w in words) * 2 <= len(words):   # "the IT guy", "hand IT the laptops"
        raw = re.sub(r"\bIT\b(?!['’])", "tech", raw)
    raw = re.sub(r"\b((?:the|an?|our|my|your|their|to|from)\s+)it(?=\s+(?:guy|guys|team|department|dept|techs?|person|people|"
                 r"support|crew|staff|bloke|lady|manager|helpdesk|help desk)\b)", r"\1tech", raw, flags=re.I)
    if not words or sum(w[0].isupper() for w in words) * 2 > len(words):
        # Title Case or SHOUTING says nothing about names, except after to, for, with or help: "Bring The Pills To
        # Siobhan", "A ROBOT THAT HELPS SIOBHAN"
        out = re.sub(r"\b(to|for|with|helps?|helping|helped|assists?|assisting|from)\s+([A-Z][A-Za-z'’-]*)\b(?!['’])",
                     lambda m: m.group(1) + " " + ("someone" if _title_name(m.group(2)) else m.group(2)), raw, flags=re.I).lower()
    else:
        raw = re.sub(r"\b" + _TITLE_WORDS + r"\.?\s+(?=[A-Z][a-z]+\b)", "", raw)
        parts, last = [], 0
        for m in re.finditer(r"\b[A-Z][a-z]+\b", raw):
            before, w = raw[:m.start()].rstrip(), m.group(0).lower()
            if not before or before[-1] in ".!?:\"“(\n" or w in _NOT_NAMES or \
                    re.search(r"\b(?:the|a|an|this|that|these|those|its)$", before.lower()) or \
                    re.fullmatch(r"(?:" + _ROLES + "|" + _PLACE + "|" + _NOT_DESCRIBING + "|" + _ADVERB + "|" + _PREPOSITION +
                                 ")", w):
                continue
            parts += [raw[last:m.start()].lower(), "someone"]
            last = m.end()
        out = "".join(parts + [raw[last:].lower()])
    for pat, rep in _TYPOS:
        out = re.sub(pat, rep, out)
    out = re.sub(r"\bmason (jars?|lids?|bottles?)\b", r"glass \1", out)       # "the mason jars" are jars, "the mason" a person
    out = re.sub(r"\b((?:case|carton|cartoning|tray|bag|box|vertical|horizontal|flow)\s+pack)ers?\b", r"\1ing machine", out)
    out = re.sub(r"\b((?:spot|ultrasonic|laser|seam|arc|resistance|friction|projection|stud|mig|tig|robot)\s+weld)ers?\b",
                 r"\1ing machine", out)
    out = re.sub(r"\b((?:vision|x-?ray|optical|automated|camera|in-?line)\s+inspect)ors?\b", r"\1ion machine", out)
    out = re.sub(r"\b((?:washer|egg|fruit|optical|weight)\s+grad)ers?\b", r"\1ing machine", out)
    out = _baby_goods(out)
    return _agents(out) if agents else out


def _title_name(word: str) -> bool:
    """Whether a capitalised word after "to", "for", "with" or "help" in a Title Case or SHOUTING request is a name: not
    a word for a person, a place, a thing, an animal, the robot, a department or a function word."""
    w = word.lower()
    return not (w in _NOT_NAMES or w in _PROCESS_WORDS or re.fullmatch(r"(?:" + _ROLES + "|" + _KIN + "|" + _KIN_SLANG + "|" + _PLACE + "|" + _THINGS + "|" +
                                                 _ANIMAL + "|" + _ROBOT_WORD + "|" + _NOT_DESCRIBING + "|" + _ADVERB + "|" +
                                                 _PREPOSITION + r"|me|you|us|him|them|it|its|our|your|my|this|that|these|those|"
                                                 r"some|any|all|both|each|every|one|two|three|four|five|ten|home|work|school|"
                                                 r"bed|size|order|scale|position|storage|stock|waste|scrap|trash|rubbish|"
                                                 r"[a-z]+(?:ing|ed|ly|tion|ment|ness|ity|ies|es))", w))


# baby and infant products are things, not children ("sorts baby food jars", "move the baby wipes", "dispenses infant
# formula", "give the baby carrots to the rabbit"); after a giving or feeding verb with no one else to get them the baby
# may be the one given something, so it stays ("feed the baby food", "give the baby carrots")
_BABY_GOODS = (r"(?:carrots?|spinach|corn|sweetcorn|potato(?:es)?|tomato(?:es)?|leaf|leaves|greens|courgettes?|zucchinis?|"
               r"squash(?:es)?|beets?|beetroots?|aubergines?|kale|lettuces?|plum tomatoes|foods?|wipes?|oil|powder|lotion|"
               r"shampoo|formula|cereal|rice|purees?|purées?|jars?|pouches?|bottles? of formula|monitors?|gates?|clothes|"
               r"clothing|socks?|grows?|bouncers?|swings?|walkers?|wipes|blankets?|bibs?|toys?|books?|shoes?|hats?|"
               r"mittens?|vests?|sleepsuits?|onesies?|bottles?|cups?|spoons?|bowls?|plates?)")


def _baby_goods(text: str) -> str:
    """`text` with "baby"/"infant" before a product written "product" ("baby food jars" -> "product food jars")."""
    def product(m):
        given = re.search(r"\b(?:feed|giv|give|hand|pass|bring|brought|serv|offer|fetch|get)\w*\s+(?:(?:the|a|an|my|our|your|their|"
                          r"his|her|this|that)\s+)?$", text[:m.start()])
        if given and not re.match(r"(?:\s+[\w'’-]+){0,3}?\s+(?:to|for)\s+(?!(?:(?:the|a|an|my|our|your|their|his|her)\s+)?"
                                  r"(?:bab(?:y|ies)|infants?)\b)[a-z]", text[m.end():]):
            return m.group(0)
        return "product" + m.group(1)
    return re.sub(r"\b(?:bab(?:y|ies)|infants?)(\s+" + _BABY_GOODS + r")\b", product, text)


# where a word for someone can stand: after a determiner, a possessive or a preposition, through up to three describing
# words ("to the dog trainer", "with our new fitters"), or a plural at the start ("plumbers use it")
_AGENT_SLOT = frozenset(("the", "a", "an", "my", "our", "your", "their", "her", "his", "this", "that", "these", "those", "each",
                         "every", "any", "some", "another", "both", "all", "one", "two", "three", "four", "five", "several",
                         "many", "other", "to", "for", "with", "near", "beside", "between", "behind", "by", "from", "among",
                         "around", "alongside", "except", "like"))
# verbs that end like such a word: never a person ("delivers the meds", "offer the tray", "a robot to transfer tubes")
_AGENT_VERBS = re.compile(r"(?:deliver|transfer|offer|order|answer|enter|cover|recover|discover|uncover|gather|consider|"
                          r"remember|wonder|register|monitor|filter|lower|water|power|master|counter|alter|render|steer|cater|"
                          r"hover|scatter|shatter|stagger|linger|trigger|tinker|ponder|hinder|bother|smother|weather|tether|"
                          r"quiver|shiver|waver|sever|hamper|pamper|temper|tamper|mirror|honou?r|labou?r|favou?r|colou?r|"
                          r"flavou?r|anchor|harbou?r|censor|sponsor|conquer|encounter|foster|shelter|plaster|clutter|flutter|"
                          r"mutter|stutter|utter|batter|ever|whatever|whoever|whomever|wherever|whenever|forever|clever)"
                          r"(?:s|ed|ing)?")


def _agents(text: str) -> str:
    """Each word that names someone by what they do and isn't on the person-word lists ("the fitter", "a riveter", "the
    optometrist", "to the postman", "the salesperson") written "morphagent(s)", which the receiver patterns read as a
    person; a machine, a container, a tool, an animal, a verb or a name is left alone, and so is a word outside a noun
    phrase."""
    toks = list(re.finditer(r"[a-z][a-z-]*|[^a-z\s]+", text))
    out, last = [], 0
    for i, m in enumerate(toks):
        w = m.group(0)
        if not w[0].isalpha() or not _AGENT_SHAPE.fullmatch(w) or _AGENT_VERBS.fullmatch(w) or \
                re.fullmatch(_ROLES + "|" + _NAMES + "|" + _ANIMAL, w) or \
                _machine_reader(toks, i):
            continue
        plural = bool(re.search(r"(?:men|people|folks|s)$", w))
        slot = plural and (i == 0 or not toks[i - 1].group(0)[0].isalpha())
        for j in range(i - 1, max(-1, i - 5), -1):
            p = toks[j].group(0)
            if p in _AGENT_SLOT:
                slot = True
                break
            if not p[0].isalpha() or re.fullmatch(_NOT_DESCRIBING, p):
                break
        if slot:
            out += [text[last:m.start()], "morphagents" if plural else "morphagent"]
            last = m.end()
    return "".join(out + [text[last:]])


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
# ---- harm: the local pre-filter (SubQ's harm noul and the designer's flag catch the rest), offline and before
# anything is sent anywhere. It reads what is done to whom, not only words: a weapon, a tool that can hurt (any gun,
# a blade, heat, a chemical, a rock or a stick) aimed, swung, fired, poured or pressed at someone or something alive,
# someone hurt, tied or locked up, a drug in someone's drink. What can be hurt: someone or something alive
_H_LIVING = (r"(?:people|persons?|someone|somebody|anyone|anybody|everyone|everybody|whoever|whomever|humans?|ppl|peeps|"
           r"folks?|guys|dudes|crowds?|(?:(?<=\bthe )|(?<=\bmy )|(?<=\bour )|(?<=\bhis )|(?<=\bher )|(?<=\btheir )|"
           r"(?<=\bwhole ))class(?:es)?|intruders?|strangers?|burglars?|thie(?:f|ves)|trespassers?|robbers?|"
           r"attackers?|enem(?:y|ies)|prisoners?|hostages?|victims?|police|cops?|officers?|mail ?m[ae]n|post ?m[ae]n|"
           r"posties?|delivery (?:guy|man|men|driver|person|people)s?|protesters?|classmates?|referees?|hikers?|joggers?|"
           r"runners?|cyclists?|paperboys?|passers?-?by|neighbou?rs?|kids?|kiddos?|toddlers?|teens?|" + _ROLES + "|" +
           _KIN + "|" + _KIN_SLANG + r"|(?!tedd(?:y|ie)\b)" + _NAMES + "|" + _ANIMAL + r"|squirrels?|foxes|fox|deer|mice|"
           r"mouse|rats?|pigeons?|seagulls?|gulls?|raccoons?|badgers?|moles?|wildlife|wild animals|vermin)")
_H_BODY = (r"(?:heads?|faces?|eyes?|eyeballs?|foreheads?|temples?|necks?|throats?|mouths?|lips|ears?|nose|noses|chests?|"
         r"bell(?:y|ies)|backs?|skin|hair|bod(?:y|ies)|arms?|legs?|knees?|feet|foot|toes?|groins?|tails?|paws?|fur|"
         r"hands?|palms?|fingers?|thumbs?|wrists?|shoulders?|calves|calf|thighs?|stomach|guts?|spine|ribs?|teeth|tongue)")
# the living word names a thing when one of these follows it ("the dog house", "the cat flap", "the customer's phone")
_H_THING_HEAD = (r"(?:house|flap|bed|bowl|feeder|statue|toy|door|food|basket|cage|kennel|hutch|tree|carrier|crate|mover|"
               r"ornament|figurine|picture|poster|photo|drawing|painting|sculpture|model|doll|mannequin|dummy|silhouette|"
               r"target|shape|outline|cut-?out|sticker|logo|dish|collar|leash|blanket|mat|coop|shed|stable|barn|trough|"
               r"shampoo|bag|box|card|ticket|phone|badge|record|file|sample|manual|wristband|id|seat|chair|desk|station|"
               r"room|area|zone|table|tray|bin|car|bike|van|truck|window|gate|fence|wall|parcel|package|luggage|suitcase|"
               r"coat|clothes|shoe|hat|glasses|cup|plate|drink|coffee|tea|lunch|dinner|meal|fastball|ball|helmet|uniform|"
               r"locker|counter|booth|kiosk|stall|barcode|screen|monitor|laptop|computer|tablet|camera|lamp|vase|pot|"
               r"part(?:y|ies)|birthday|wedding|disco|fair|fete|event|show|gig|"
               r"plant|garden|lawn|hedge|yard|porch|roof|gutter|drain|pipe|engine|wheel|tyre|tire|panel|frame|bottle|jar|"
               r"wallet|purse|book|cushion|pillow|sofa|toolbox|tool|kit|equipment|gear|chip|implant|water|milk|hay|straw|"
               r"craft|project|homework|belt|conveyor)"
               r"(?:s|es)?\b")
_H_DET = r"(?:the|a|an|my|your|his|her|their|our|its|some|any|all|every|that|this|these|those|one|two|three)"
_H_MOD = r"(?:(?!(?:at|to|on|onto|in|into|and|or|with|from|for|of|by|so|then|while|when|until|because|if)\b)[a-z'’-]+\s+)"
_H_POSS = (r"(?:(?:my|your|his|her|their|our)\s+(?:" + _H_MOD + r"{0,2}?" + _H_LIVING + r"['’]s?\s+)?|(?:" + _H_DET + r"\s+)?" +
         _H_MOD + r"{0,2}?" + _H_LIVING + r"['’]s?\s+|(?:someone|somebody|people|a person|the person)['’]s?\s+)")
_H_A_LIVING = (r"(?:" + _H_DET + r"\s+)?" + _H_MOD + r"{0,2}?" + _H_LIVING + r"\b(?!['’]s?\s+" + _H_MOD + r"{0,1}?" + _H_THING_HEAD +
             r")(?!['’])(?!\s+" + _H_THING_HEAD + r")(?!-)")
_H_HER = r"her\b(?!\s+(?!(?:" + _NOT_DESCRIBING + "|" + _PREPOSITION + "|" + _ADVERB + r"|until|again|too|now)\b)[a-z])"
# someone (or their body): the object of a verb that hurts or ties
_H_SOMEONE = r"(?:(?:me|you|him|them|us|myself|yourself|himself|herself|themselves|ourselves|ya|u)\b|" + _H_HER + "|" + \
    _H_POSS + _H_MOD + r"{0,1}?" + _H_BODY + r"\b|" + _H_A_LIVING + ")"
_H_SOMEONE_NOT_THEM = _H_SOMEONE.replace("(?:me|you|him|them|us|", "(?:me|you|him|us|", 1)
# a person or a companion animal, for the cut/stab rules: not a farm or food animal (a cut "chicken" or "ham" is food,
# a cut "cat" is harm), not a possessive thing ("the cat's bed"), not a benefactive ("cut me a slice", handled by a
# lookahead on the rule)
_H_PET = (r"(?:dogs?|dogg(?:y|ie|ies|o)|pupp(?:y|ies)|pups?|cats?|kitt(?:y|ies|ens?)|pets?|rabbits?|bunn(?:y|ies)|"
          r"hamsters?|gerbils?|guinea\s+pigs?|ferrets?|parrots?|budgies?|parakeets?)")
_H_PP_CORE = (r"(?:people|persons?|someone|somebody|anyone|anybody|everyone|everybody|whoever|whomever|humans?|kids?|"
              r"kiddos?|toddlers?|teens?|bab(?:y|ies)|child|children|infants?|newborns?|strangers?|intruders?|"
              r"burglars?|classmates?|neighbou?rs?|" + _ROLES + "|" + _KIN + "|" + _KIN_SLANG + "|(?!tedd(?:y|ie)\\b)" +
              _NAMES + "|" + _H_PET + ")")
_H_PP = (r"(?:(?:me|you|him|them|us|myself|yourself|himself|herself|themselves|ourselves|ya|u)\b|" + _H_HER + r"|(?:" +
         _H_DET + r"\s+)?" + _H_MOD + r"{0,2}?" + _H_PP_CORE + r"\b(?!['’])(?!\s+" + _H_THING_HEAD + r")(?!-))")
# ... and after "at", "on" or "against" a body word ending its phrase too ("aims the heat gun at the arm"), never one a
# thing word describes ("at the robot arm", "at the screw heads")
_H_TARGET = r"(?:" + _H_SOMEONE + r"|(?:the\s+)?" + _H_BODY + r"\b(?=\s*(?:$|[.,;:!?)]|(?:and|or|until|while|when|so|then|with)\b)))"
# things that hurt: any gun, blades, heat, chemicals, things thrown or swung, shocks
_H_GUN = r"(?:[a-z-]+[- ])?guns?\b|\b(?:nail|glue|staple|blow|stun)guns?\b|\bblowpipes?"
_H_DANGER = (r"(?:" + _H_GUN + r"|knife|knives|machetes?|blades?|razors?|scalpels?|box ?cutters?|scissors|saws?|chainsaws?|"
           r"swords?|axes|axe|daggers?|spears?|pitchforks?|sickles?|cleavers?|bats?|clubs?|hammers?|crowbars?|sticks?|"
           r"rocks?|stones?|bricks?|needles?|syringes?|darts?|arrows?|blowtorch(?:es)?|torch(?:es)?|lighters?|"
           r"soldering irons?|hot irons?|curling irons?|irons?|flamethrowers?|flames?|fire|boiling water|hot water|hot oil|"
           r"acid|bleach|ammonia|lye|pepper spray|mace|poison|insecticide|pesticide|weed ?killer|cattle prods?|prods?|"
           r"tasers?|slingshots?|catapults?|crossbows?|bows?|fireworks?|chemicals?|petrol|gasoline|lasers?)\b")
_H_DANGER_OR_IT = r"(?:\b" + _H_DANGER + r"|\bit\b|\bthem\b)"
_H_AIM = (r"(?:aim|point|hold|held|fir(?:e|es|ed|ing)|shoot|shot|direct|train|level|turn|wav(?:e|es|ed|ing)|brandish|press|"
        r"push|put|jab|poke|stick|spray|squirt|blast|swing|swung|thrust|us(?:e|es|ed|ing)|swipe|slash|throw|threw|thrown|"
        r"hurl|fling|flung|drop|launch|touch|lunge|splash|pour|tip|dump|flick|lob|toss|swat|jam|drive|plunge|sink|"
        r"bring|brought|move)(?:s|es|ed|ing)?")
_H_NEG = r"(?<!\bnever )(?<!\bnot )(?<!n't )(?<!\bwithout )(?<!\bavoid )(?<!\bavoids )(?<!\bavoiding )(?<!\bno )(?<!\bnor )"
_H_VERB_USE = (r"(?<!\bthe )(?<!\ba )(?<!\ban )(?<!\bmy )(?<!\byour )(?<!\bhis )(?<!\bher )(?<!\btheir )(?<!\bour )"
             r"(?<!\bits )")         # a verb, not a noun ("the nail tech", "a tape measure")
_H_CL = r"[^.;!?\n]"
_H_AT = (r"(?:(?:at|on|onto|against|toward|towards|over|under|beneath|below)\s+|(?:in|into)\s+(?!(?:(?:my|your|his|her|their|"
       r"our|the|a|an)\s+)?(?:[a-z'’-]+['’]s?\s+){0,2}?(?:[a-z-]+\s+)?(?:hand|palm)s?\b))")
_H_HURT = (r"(?:hurt|harm|injur|attack|punch|kick|stab|kill|slap|shoot|shot|zap|tase|taser|electrocut|chok|strangl|poison|"
         r"tortur|hit|beat|whip|smack|strik|struck|thump|batter|spank|burn|scald|maim|wound|bite|bull(?:y|ies|ied)|abus|"
         r"drown|suffocat|smother|trip|shock|sting|stung|pelt|stun|gas|mace|pepper-?spray|drug|sedat|terrori|"
         r"club|clobber|crush|flatten|whack|wallop|thrash|clout|cosh|bludgeon|slaughter|headbutt|head-?butt|"
         r"threat|intimidat|menac|harass|torment|blinds|blinded|blinding)\w*")
_H_TIE = (r"(?:restrain|handcuff|cuff|gag|shackl|chain|strap|tie|ties|tied|tying|bind|bound|lock|cag|trap|pin|tape|taping|"
        r"seal|stapl|nail|glu|fasten|stick|stuck|zip-?ti|wrap|clamp|tether)\w*")
_H_FOOD = (r"(?:food|drink|coffee|tea|water|bowl|meal|dinner|lunch|breakfast|soup|juice|milk|wine|beer|cup|glass|bottle|"
         r"plate|snack|cereal|smoothie)s?")
# aimed, pointed, fired or swung at someone: "aims the rivet gun at the kid next door", "swings a knife at my brother",
# "holds a knife to his throat", "mount a screw gun and aim it at people": the verb, then within 150 characters of its
# clause something that hurts (or "it"), then within 150 more "at" someone or "to" their body; _aimed_at() reads it
_H_AIMED = _H_NEG + r"\b" + _H_AIM + r"\b"
_H_AIMED_AT = (r"(?=((?<!\baway )(?<!\bclear )(?<!\bfar )(?<!\bnot )(?<!\bnever )\b" + _H_AT + _H_TARGET + r"|\bto\s+" + _H_POSS +
               _H_MOD + r"{0,1}?" + _H_BODY + r"\b))")
HOSTILE = (
    # thrown, fired or sprayed at someone, whatever it is ("throws rocks at the cat", "sprays water at the cat"): as the
    # filter always read it; not sunscreen on one's own back
    _H_NEG + r"\b(?:shoot|launch|fir(?:e|es|ing)|throw|fling|hurl|pelt|lob|squirt|spray|pour|splash|dump)\w*\b" + _H_CL +
    r"{0,40}?\b(?:at|on|over)\s+" +
    r"(?:(?:him|them|us|you)\b|" + _H_HER + "|" + _H_A_LIVING + ")",
    # swung or slashed at someone ("slashes at the cat with a knife")
    _H_NEG + r"\b(?:slash|swip|lung|lash|strik|swing|swung|jab|pok|thrust|stab|hack|chop|cut|lash)\w*\s+at\s+" + _H_A_LIVING,
    # a blade used on a living target: a person or a pet cut, sliced, stabbed, pricked ("cuts my brother with a box
    # cutter", "slices the cat"), or a blade to someone's throat or wrist; grooming ("cut the cat's hair") and cooking
    # ("slices the ham") are not living targets and a benefactive ("cut me a slice") is excluded
    _H_NEG + r"\b(?:cut|slic|slash|saw|stab|jab|prick|slit|gash|gouge|lacerat|sever|skewer|impal|carv)\w*\s+(?:up\s+|open\s+)?"
    + _H_PP + r"(?=\s*(?:$|[.,;:!?)\]\"]|(?:with|using|by|in|into|on|onto|across|open|up|apart|again|twice|repeatedly|"
    r"deep|hard|until|till|while|when|whenever|if|so|then|and|or|but|for|because|as|after|before|to|from|at|right|"
    r"several|many|multiple|a\s+few|lots|badly|last|this|next|every|each|yesterday|today|tonight|tomorrow|now|once|"
    r"always|sometimes|again|upstairs|downstairs|nearby|outside|inside|here|there|over|behind|down|out|off)\b|[a-z]+ly\b|\d))",
    _H_NEG + r"\b(?:cut|slic|slash|slit|stab|saw|open|gash|gouge|sever|sink\s+a\s+[a-z]+\s+into)\w*\s+(?:open\s+)?" + _H_POSS +
    _H_MOD + r"{0,1}?(?:throats?|necks?|wrists?|bell(?:y|ies)|guts?|stomachs?|skin|flesh|arter(?:y|ies)|jugular|eyes?|"
    r"arms?|legs?|fingers?|hands?|cheeks?|tongue)\b",
    # smothered, or a head forced under water or into the toilet
    _H_NEG + r"\b(?:hold|holds|holding|held|press|presses|pressed|pressing|put|puts|putting|clamp|clamps|clamped|push|"
    r"pushes|pushed|force|forces|forced|shove|shoves|shoved|place|places|placed)\w*\s+(?:a\s+|the\s+|my\s+|his\s+|her\s+)?"
    r"(?:[a-z-]+\s+)?(?:pillow|cushion|bag|plastic\s+bag|cloth|rag|towel|blanket|duvet|hand|hands)\s+(?:over|on|across|onto)\s+"
    + _H_POSS + _H_MOD + r"{0,1}?(?:faces?|mouths?|noses?|heads?)\b",
    _H_NEG + r"\b(?:hold|holds|holding|held|push|pushes|pushed|shove|shoves|shoved|force|forces|forced|dunk|dunks|dunked|"
    r"plung\w+|submerg\w+|dip|dips|dipped|drown\w*)\s+(?:[\w'’-]+\s+){0,3}?(?:" + _H_POSS + _H_MOD + r"{0,1}?heads?\b|" +
    _H_SOMEONE + r")\s+(?:[\w'’-]+\s+){0,2}?(?:under|in|into|beneath)\s+(?:the\s+)?(?:water|bath|pool|toilet|sink|tank|"
    r"river|bucket|tub|pond|basin)\b",
    # a ligature tightened around the neck or throat; a neck squeezed
    _H_NEG + r"\b(?:tighten\w*|cinch\w*|pull\w*\s+(?:tight\s+)?)\s*(?:a\s+|the\s+|his\s+|her\s+|my\s+)?(?:[a-z-]+\s+)?"
    r"(?:belts?|ropes?|cords?|wires?|cables?|strings?|nooses?|chains?|leash\w*|zip[- ]?ties?|cable[- ]?ties?|flex|scarf|"
    r"tie|straps?|bands?)\s+(?:around|round|about)\s+" + _H_POSS + _H_MOD + r"{0,1}?(?:necks?|throats?)\b",
    _H_NEG + r"\b(?:wrap\w*|loop\w*|coil\w*|wind\w*|put\w*|pull\w*)\s+(?:a\s+|the\s+|his\s+|her\s+|my\s+)?(?:[a-z-]+\s+)?"
    r"(?:belts?|ropes?|cords?|wires?|cables?|strings?|nooses?|chains?|zip[- ]?ties?|cable[- ]?ties?|flex)\s+"
    r"(?:around|round|about)\s+" + _H_POSS + _H_MOD + r"{0,1}?(?:necks?|throats?)\b",
    _H_NEG + r"\b(?:squeez\w*|strangl\w*|throttl\w*|chok\w*|crush\w*)\s+(?:[\w'’-]+\s+){0,2}?" + _H_POSS + _H_MOD +
    r"{0,1}?(?:necks?|throats?|windpipes?)\b",
    # a hand or face forced onto a hot surface
    _H_NEG + r"\b(?:push\w*|press\w*|force\w*|shove\w*|hold\w*|pin\w*|place\w*|put\w*|jam\w*|mash\w*)\s+" + _H_POSS +
    _H_MOD + r"{0,1}?(?:hands?|palms?|fingers?|faces?|heads?|arms?|paws?|feet|foot)\s+(?:down\s+)?(?:onto|on|against|into|to)\s+(?:the\s+|a\s+)?"
    r"(?:red[- ]?hot\s+|burning\s+|hot\s+|scalding\s+|lit\s+)?(?:stove|hob|hotplate|hot\s+plate|cooker|oven|grill|griddle|"
    r"burner|element|ring|iron|fire|flames?|coals?|embers?|radiator|heater|kettle|pan|skillet|hotplate)s?\b",
    # grabbed by the throat, neck or hair, or dragged by the hair; stamped or sat on; run over; a shock or a beating given
    _H_NEG + r"\b(?:grab\w*|drag\w*|yank\w*|haul\w*|seiz\w*|snatch\w*|pull\w*|hoist\w*)\s+" + _H_SOMEONE +
    r"\s+(?:[\w'’-]+\s+){0,2}?by\s+(?:the|his|her|its|their)\s+(?:throat|neck|hair|scruff\s+of\s+(?:the\s+|its\s+|his\s+|her\s+)?neck)\b",
    _H_NEG + r"\b(?:stomp\w*|stamp\w*|trampl\w*|tread\w*|jump\w*|sit\w*|stand\w*|step\w*|lie\w*|kneel\w*)\s+on\s+(?:top\s+of\s+)?"
    + _H_SOMEONE,
    _H_NEG + r"\b(?:run|runs|ran|running|driv\w*|drove|back\w*|revers\w*|roll\w*|rolled)\s+(?:the\s+[a-z]+\s+)?over\s+" + _H_SOMEONE,
    _H_NEG + r"\b(?:giv|gav)\w*\s+" + _H_SOMEONE + r"\s+(?:a\s+|an\s+|some\s+|repeated\s+|electric\s+)?(?:[\w'’-]+\s+){0,2}?"
    r"(?:electric\s+shocks?|shocks?|a\s+beating|a\s+hiding|a\s+thrashing|a\s+kicking|a\s+hammering|a\s+good\s+hiding)\b",
    # done to someone with it: "burns the cat with a blowtorch", "touches the cat with a soldering iron"
    _H_NEG + r"\b(?:touch|press|pok|prod|jab|burn|brand|hit|strik|struck|spray|squirt|dous|zap|shock|pelt|whip|beat|poke)\w*\s+" +
    _H_SOMEONE + r"(?:\s+[\w'’-]+){0,4}?\s+(?:with|using)\s+(?:a|an|the|my|his|her|their|our|some)?\s*(?:[a-z-]+\s+){0,2}?" +
    _H_DANGER,
    r"\bsets?\s+fire\s+to\s+" + _H_SOMEONE + r"|\bsets?\s+" + _H_SOMEONE + r"\s+(?:on\s+fire|alight|ablaze)",
    # things hurled at property: "throws rocks at the cars"
    r"\b(?:throw|hurl|fling|launch|fire|shoot|lob)\w*\s+(?:[\w'’-]+\s+){0,3}?(?:rocks?|stones?|bricks?|fireworks?|eggs?)\s+"
    r"at\s+(?:the\s+|my\s+|our\s+|their\s+|passing\s+)?(?:[a-z-]+\s+)?(?:cars?|vehicles?|windows?|houses?|buses|trains?|"
    r"planes?|drones?|traffic|shops?)\b",
    # done to someone: "hits the kids with a stick", "poisons my husband", "kills mice", "blinds the guard"
    _H_NEG + _H_VERB_USE + r"\b" + _H_HURT + r"\s+(?:up\s+|down\s+)?" + _H_SOMEONE,
    # restrained: "ties up the dog", "holds him down", "tapes him to the chair", "locks the kids in their room"
    _H_NEG + _H_VERB_USE + r"\b" + _H_TIE + r"\s+(?:up\s+|down\s+)?" + _H_SOMEONE_NOT_THEM,
    # held down or still, but not for care: a care purpose ("while i trim its claws", "for the vet") is let through by
    # _benign_act, which reads the whole clause
    _H_NEG + r"\b(?:hold|holds|holding|held|pin|pins|pinned|pinning|keep|keeps|keeping)\s+" + _H_SOMEONE + r"\s+(?:down|still)\b"
    r"(?!\s+(?:for|while|so|until)\s+(?:the\s+|a\s+|her\s+|his\s+|its\s+|my\s+)?(?:vet|doctor|nurse|groomer|dentist|farrier|"
    r"check-?up|injection|jab|exam|examination|treatment|vaccination|haircut|trim)\b)",
    # a chemical put on or in someone, or in their food: "squirts bleach in the dog's eyes", "puts bleach in my husband's
    # coffee"; drugs only in their food or drink ("drugs my wife's drink", not "puts my dad's pills in his hand")
    r"\b(?:spray|squirt|pour|splash|throw|put|tip|add|mix|slip|drip|sprinkl|rub|smear)\w*\s+(?:[\w'’-]+\s+){0,3}?(?:bleach|"
    r"acid|ammonia|lye|poison|insecticide|pesticide|weed ?killer|chemicals?|petrol|gasoline|boiling water|hot oil|"
    r"pepper spray|mace|rat poison|antifreeze|caustic soda|drain cleaner)\s+(?:at|on|in|into|onto|over)\s+(?:" + _H_SOMEONE +
    "|" + _H_POSS + r"(?:[a-z-]+\s+)?" + _H_FOOD + r"\b)",
    r"\b(?:put|slip|add|mix|drop|crush|stir|sneak)\w*\s+(?:[\w'’-]+\s+){0,3}?(?:drugs?|sedatives?|sleeping (?:pills|tablets)|"
    r"laxatives?|roofies|rohypnol)\s+(?:in|into)\s+" + _H_POSS + r"(?:[a-z-]+\s+)?" + _H_FOOD + r"\b",
    r"\b(?:drug|spike|poison|lace)\w*\s+" + _H_POSS + r"(?:[a-z-]+\s+)?" + _H_FOOD + r"\b",
    # traps and falls: "sets traps for the neighbour's cat", "pushes my brother down the stairs"
    r"\b(?:set|lay)s?\s+(?:up\s+)?traps?\s+for\s+" + _H_SOMEONE,
    _H_NEG + r"\b(?:push|shov|knock|throw)\w*\s+" + _H_SOMEONE + r"\s+(?:down|off|over|out of|into)\b",
)
# no verb: "a robot with a rivet gun on people", "rivet gun at my brother", "a knife to his throat"
_H_NO_VERB = (r"\b" + _H_DANGER + r"(?:\s+[\w'’-]+){0,4}?\s+(?:(?:at|on|onto|against|toward|towards)\s+" + _H_TARGET +
              r"|for\s+" + _H_POSS + _H_MOD + r"{0,1}?" + _H_BODY + r"\b)",
              # "a knife to his throat": "to" names where it goes, not a purpose ("scissors to cut my son's hair"),
              # so the word after "to" must be a possessive or a determiner, never a bare verb
              r"\b" + _H_DANGER + r"(?:\s+[\w'’-]+){0,3}?\s+to\s+(?=(?:my|your|his|her|their|our|its|the|a|an|someone|"
              r"somebody|people|that|this|these|those|[a-z]+['’]s)\b)" + _H_POSS + _H_MOD + r"{0,1}?" + _H_BODY + r"\b")
_H_ANY_DANGER = r"\b" + _H_DANGER
_H_DANGER_HERE = r"(?=(" + _H_DANGER_OR_IT + "))"       # every place one starts, overlapping ("point gun")
# with a weapon or a tool that can hurt in the request: an only-there target, a purpose that turns it on someone, or
# someone sealed in ("never aims it at the wall, only at people", "uses the impact gun to shut my brother up", "to keep
# the cat off the counter", "goes off when someone opens the door", "seals them in the room")
# The danger has to be what the clause does it with: "uses the nail gun to keep the cat off", not "keeps the kids away
# from the nail gun" or "keeps the dog out of the kitchen while i use the knives"; a robot that stops, slows or switches
# off when someone comes near guards them, and one that hands over a knife when mum comes home does an errand.
# the danger must be the instrument of keeping/stopping the person, within a word or two of its use ("uses the nail gun
# to keep the cat off"), not a tool named later in a different clause ("keep the cat off the counter while i cook with
# knives", "use the screw gun to build a fence to keep the dog out").
_H_USE_TO = (_H_NEG + r"\b(?:us(?:e|es|ed|ing)|with|wielding|holding|operat\w+)\s+(?:[\w'’-]+\s+){0,3}?" + _H_DANGER +
             r"\s+(?:[\w'’-]+\s+){0,2}?to\s+")
_H_WITH_AFTER = (r"(?:(?!\b(?:while|when|as|so|because|and|but|then|if|for|to)\b).){0,40}?\b(?:with|using)\s+"
                 r"(?:[\w'’-]+\s+){0,3}?" + _H_DANGER)
# fired or set off, triggered by a person coming ("fires a nail gun through the door when someone knocks"); a robot that
# stops, slows or switches off when someone comes near is the opposite, a safety stop, so only discharge verbs count.
_H_FIRE_TRIG = (r"\b(?:fir(?:es|ing|ed)|shoot\w*|shot|discharg\w*|go(?:es)?\s+off|went\s+off|springs?|sprung|triggers?|"
                r"releas\w*|zaps?|blasts?|lash\w*|snaps?\s+shut)\b")
_H_COMES = (r"\s+(?:comes?|came|walks?|gets?|opens?|enters?|steps?|tries|try|goes|knocks?|rings?|arrives?|approaches|"
            r"approach|moves?|appears?|climbs?|jumps?|breaks?|reaches)\b")
HOSTILE_WITH_DANGER = (
    r"\b(?:only|but|just|instead|rather|except)\s+(?:at|on|towards?)\s+" + _H_TARGET,
    r"\bshut\s+" + _H_SOMEONE + r"\s+up\b",
    _H_NEG + r"\b(?:tag|label|rivet|screw|spray|squirt|heat|solder|paint|caulk|target|scar|chas|pok|jab|blast|punish|hunt|"
    r"frighten|sting|disciplin|deter|repel)\w*\s+" + _H_A_LIVING + r"(?!\s+(?:company|warm|safe|fed|happy|clean|dry|"
    r"busy|cool|entertained)\b)",
    _H_USE_TO + r"keep\s+" + _H_SOMEONE + r"\s+(?:off|out|away|back)\b",
    r"\bkeep(?:s|ing)?\s+" + _H_SOMEONE + r"\s+(?:off|out|away|back)\b" + _H_WITH_AFTER,
    _H_USE_TO + r"stop\s+" + _H_SOMEONE + r"\s+(?:from\s+)?[a-z]+ing\b",
    r"\bstop(?:s|ping)?\s+" + _H_SOMEONE + r"\s+(?:from\s+)?[a-z]+ing\b" + _H_WITH_AFTER,
    r"\b" + _H_DANGER + _H_CL + r"{0,40}?\bgo(?:es)?\s+off\s+(?:when|if|whenever)\s+" + _H_SOMEONE,
    _H_NEG + _H_FIRE_TRIG + r"\s+" + _H_CL + r"{0,50}?\b(?:when|if|whenever|as soon as)\s+" + _H_A_LIVING + _H_COMES,
    r"\b" + _H_TIE + r"\s+them\s+(?:in|into|inside)\s+(?:the\s+|a\s+|their\s+|his\s+|her\s+)?(?:room|house|basement|cellar|"
    r"closet|cupboard|cage|shed|garage|car|trunk|attic|bathroom|bedroom|office|toilet|cell)\b",
)
# context that makes any gun a weapon ("a staple gun turret", "a nail gun on a robot to defend my house", "darts")
GUN_CONTEXT = (r"\b(?:defend\w*|defen[cs]e|deter\w*|repel\w*|protect(?:s|ing|ion)?\s+(?:the\s+|my\s+|our\s+)?(?:[a-z-]+\s+)?(?:door|house|home|yard|garden|gate|property|shop|store|building|driveway|garage|car|premises|flat|apartment|office|farm|land)\b|guard(?:s|ed|ing)?\s+(?:the\s+|my\s+|our\s+|a\s+|his\s+|her\s+|"
               r"their\s+)?(?:[a-z-]+\s+)?(?:door|house|home|yard|garden|gate|entrance|fence|property|shop|store|building|"
               r"room|driveway|garage|car|base|perimeter|area|premises|porch|flat|apartment|office|land|farm|field)s?\b|"
               r"ward(?:s|ed|ing)?\s+off|keep(?:s|ing)?\s+(?:people|intruders|burglars|strangers|thieves|them|everyone)\s+"
               r"(?:out|away)|(?<!dress )(?<!fabric )(?<!sewing )(?<!bust )(?<!waist )(?<!chest )darts?|turrets?|sentry|sentries|"
               r"(?<!the )(?<!a )(?<!camp)(?<!bon)(?<!log )(?<!open )(?<!gas )(?<!coal )(?<!wood )(?<!warm )(?<!by the )"
               r"(?:shoot\w*|fir(?:e|es|ed|ing))\b(?!\s+(?:extinguisher|alarm|blanket|exit|door|safety|drill|station|hazard|risk|place|"
               r"wood|pit|truck|engine|starter|brigade|fighter|man|men|proof|resistant|retardant|works?|up)s?\b)(?!\s+(?:[\w'’-]+\s+)"
               r"{0,4}?(?:into|through|onto)\s+(?!" + _H_SOMEONE + r"))(?!\s+(?:the\s+|a\s+)?(?:[a-z-]+\s+)?(?:nails?|staples?|"
               r"brads?|rivets?|tacks?|pins?|screws?|bolts?|glue|clay|kilns?|pottery|photos?|pictures?|video|hoops?)\b)|"
               r"(?<!\bair )(?<!\bair-)blow[- ]?guns?|blowpipes?|bullets?|scar(?:e|es|ed|ing)|threat\w*|frighten\w*|"
               r"intimidat\w*|menac\w*|terrori[sz]\w*|gag(?:s|ged|ging)?|torment\w*|harass\w*|(?:intruders?|burglars?)"
               r"(?!\s+alarms?)|trespassers?|strangers?|robbers?|attackers?|enem(?:y|ies)|as\s+a\s+weapon|weaponi[sz]\w*)\b")
# the tool gun builds or installs a barrier or a security device, so "guard/protect/keep ... out" is construction, not a
# weapon: "a nail gun to build a fence to protect the garden", "a screw gun to install a door lock against break-ins"
_GUN_BUILDS = (r"\b(?:build\w*|fix\w*|mend\w*|repair\w*|erect\w*|put\w*\s+up|install\w*|assembl\w*|fit\w*|screw\w*|mount\w*|"
               r"attach\w*|fasten\w*|make\w*|replac\w*|hang\w*)\b(?:(?!\bshoot|\bfire|\bweapon|\bturret).){0,40}?"
               r"\b(?:fenc\w+|wall|gate|enclosure|pen|coop|hutch|run|railing|rail|deck\w*|frame|shed|door|lock|latch|bolt|"
               r"alarms?|hinge|panel|post|trellis|barrier|cabinet|shelf|shelving|bracket)s?\b")
# a gun's context that no construction frame makes harmless: turned into a weapon, or set to scare or threaten someone
_GUN_WEAPONIZE = (r"\b(?:turrets?|sentry|sentries|(?<!dress )(?<!fabric )(?<!sewing )darts?|as\s+a\s+weapon|weaponi[sz]\w*|"
                  r"bullets?|blow[- ]?guns?|blowpipes?|threat\w*|intimidat\w*|menac\w*|terrori[sz]\w*|scar(?:e|es|ed|ing)|"
                  r"frighten\w*|harass\w*|torment\w*|gag(?:s|ged|ging)?)\b")
WEAPONS = (r"\b(?:weapons?|firearms?|rifles?|pistols?|shotguns?|handguns?|revolvers?|ar-?15s?|ak-?47s?|glocks?|uzis?|"
           r"sub-?machine ?guns?|machine ?guns?|snipers?|tasers?|stun ?guns?|(?<!bath )bombs?|pipe ?bombs?|explosives?|"
           r"grenades?|missiles?|warheads?|rocket launchers?|bazookas?|ammunition|ammo|blowguns?|blowpipes?|crossbows?|"
           r"flamethrowers?|molotovs?|landmines?|land mines?|nerve (?:agents?|gas)|sarin|ricin|anthrax|mustard gas|napalm|"
           r"ieds?|harpoons?|cannons?|nunchucks?|brass knuckles|knuckle-?dusters?|switchblades?|flick ?knives?|"
           r"butterfly knives?|throwing (?:knives|stars?))\b")
SPY = r"\b(?:spy|spies|spied|spying)\s+on\b|\bsecretly\s+(?:watch|record|film)\w*|\bcovert(?:ly)?\s+(?:watch|record|film|surveil)\w*"
# tool guns: the ones the filter always let through, and the ones it refused before this beta, which are let through
# only in a harmless use: handed over, held, cleaned, loaded, stored or sorted, used on a thing, or named as a thing's part
_H_OLD_TOOL_GUN = (r"(?:hot\s+)?(?:glue|heat|caulk|caulking|paint|spray|nail|staple|massage|label|confetti|t-?shirt|bubble|snow|"
                 r"glitter|frosting|icing)")
_H_NEW_TOOL_GUN = (r"(?:stapler|labell?ing|tape|rivet|grease|scan|scanner|scanning|barcode|bar[- ]code|solder|soldering|price|"
                 r"pricing|tag|tagging|temp|temperature|thermometer|infrared|ir|impact|screw|foam|sealant|silicone|welding|mig|"
                 r"tig|cable[- ]tie|tie|radar|heat[- ]shrink|dispensing|grout|texture|air[- ]blow|wheel|lug|tyre|tire|torque)")
_H_HARMLESS_BEFORE = (r"\b(?:hand|pass|giv|gave|bring|brought|fetch|grab|get|got|toss|offer|return|suppl|lend|lent|deliver|put|"
                    r"tak(?:e|es|en|ing)|took|collect|receiv|accept|retriev|remov|unhook|unclip|"
                    r"hold|held|pick(?:s|ed|ing)? up|lift|carr|mov|stor|stow|hang|hung|clean|wip|load|reload|refill|fill|charg|"
                    r"sort|stack|box|pack|test|calibrat|swap|replac|repair|fix|check|inspect|lubricat|oil|greas|maintain|mount|"
                    r"attach|fit|equip|us(?:e|es|ed|ing)|operat|aim|point|press|direct|keep|kept|install|steady|switch|plug|"
                    r"unplug|recharg|buy|order|rent|borrow|need|want|find|label|rack|organi[sz]|tidy|shelve|count)\w*\s+"
                    r"(?:(?!(?:and|or|but|then|while|when|because|so)\b)[\w'’-]+\s+){0,6}?(?:[\w-]+\s+)?$")
_H_HARMLESS_AFTER = (r"^(?:s)?\s*(?:holders?|racks?|stands?|cases?|box(?:es)?|nozzles?|tips?|triggers?|refills?|cartridges?|"
                   r"tapes?|nails|staples|sticks|batter(?:y|ies)|chargers?|hooks?|mounts?|brackets?|parts|spares|reels?|"
                   r"rolls?|holsters?|sleeves?|covers?|manuals?|drawer|shelf|bins?|trays?|labels|tags|heads?|bits?|"
                   r"(?:robot|arm|attachment|end effector|tool|head)s?\s+(?:that|which|to|for)\b|(?:that|which|to|for)\s+"
                   r"(?!(?:shoot|fire|hurt|harm|scare|attack|defend|guard|threaten)\w*)[a-z]+)")


HARM = (WEAPONS, SPY)
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
              "load", "loads", "payload", "batch", "batches", "weight", "weights", "every", "one", "ones", "robot", "arm",
              "gripper", "budget", "total", "heavy", "light", "wide", "long", "tall", "high", "thick", "across", "diameter",
              "maybe", "about", "around", "weighing", "weigh", "weighs", "weighed", "weight", "dia", "deep", "depth", "big",
              "small", "large", "approx"}
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
def _gun_harmless(t: str, m: re.Match) -> bool:
    """Whether a tool gun the filter refused before this beta is in a harmless use where the request names it."""
    before, after = t[max(0, m.start() - 160):m.start()], t[m.end():m.end() + 80]
    return bool(re.search(_H_HARMLESS_BEFORE, before) or re.match(_H_HARMLESS_AFTER, after) or
                re.search(r"\bwith\s+(?:a|an|the|my|its|his|her|their|our)?\s*(?:[a-z-]+\s+){0,2}?$", before) and
                re.search(r"\b[a-z]+(?:s|es|ed|ing)?\s+(?:[\w'’-]+\s+){1,7}?with\s+", before) and
                not re.search(r"\b(?:robot|arm|bot|machine|one)\s+(?:that\s+has\s+|with\s+)(?:a|an|the)?\s*(?:[a-z-]+\s+){0,2}?$",
                              before))


def _self_massage(s: str) -> bool:
    """A massage gun on the requester's own body is what it is for ("uses the massage gun on my back")."""
    return bool(re.search(r"\bmassage[- ]?guns?\b", s) and
                re.search(r"\b(?:at|on|onto|against|over|to|into)\s+(?:(?:me|myself)|my\s+(?:[a-z-]+\s+)?" + _H_BODY + r")\s*$", s))


# everyday and safety-minded framings the harm filter must never refuse: a game, a tool doing its ordinary job on a
# person's hair or a body part, a comfort item, taking a temperature, a light, and the robot's own arm or wrist.
_H_GAME = (r"\b(?:chess|checkers|chequers|draughts|backgammon|dominoes|scrabble|monopoly|cards|poker|tennis|squash|"
           r"table ?tennis|ping[- ]?pong|pool|snooker|darts|golf|bowling|a game|the game|games|a match|the match|"
           r"video ?games?|fifa|mario kart|mario|wii|console)\b")
_H_HAIR_TOOL = r"(?:curling\s+(?:irons?|tongs?|wands?)|hair\s+(?:irons?|dryers?|straighteners?)|straighteners?|flat\s+irons?)"
_H_AIM_OK = (  # the matched clause is one of these ordinary, body-care or illumination uses
    r"\b" + _H_HAIR_TOOL + r"\b(?:(?!\bthroat\b).){0,30}?\bhair\b"
    r"|\b(?:curling\s+(?:irons?|tongs?|wands?)|flat\s+irons?)\b(?:(?!\bthroat\b|\bface\b|\beye).){0,24}?\b(?:hair|head|fringe|curls?)\b"
    r"|\bhot[- ]water[- ]?bottles?\b"
    r"|(?<!blow)(?<!blow[- ])\btorch(?:es)?\b(?:(?!\bface\b|\beyes?\b).){0,40}?\b(?:my|your|the|his|her|our|their)\s+"
    r"(?:[a-z'’-]+\s+){0,2}?(?:hand|hands|workbench|bench|project|board|car|sink|desk|table|work|cake)s?\b"
    r"|(?<!blow)(?<!blow[- ])\btorch(?:es)?\b(?:(?!\beyes?\b).){0,40}?\b(?:mouth|teeth|ear|ears|throat)\b"
    r"(?:(?!\bburn|\bhurt).){0,40}?\b(?:dentist|doctor|nurse|gp|check\w*|see|look\w*|exam\w*)\b"
    r"|(?<!blow)(?<!blow[- ])\btorch(?:es)?\b(?:(?!\bface\b).){0,40}?\bwhile\s+(?:i|we|you|they|he|she)\b"
    r"|\b(?:temp|temperature|thermometer|infrared|ir)[- ]?guns?\b\s+(?:at|on|to|over|near|against)\s+(?:my|your|the|his|"
    r"her|their|a|each|every)\s+(?:[a-z-]+\s+){0,2}?(?:forehead|temple|wrist)s?\b"
    r"|\bheat[- ]?guns?\b(?:(?!\bface\b|\beye|\bthroat\b|\bhead\b).){0,40}?\b(?:warm|dry|thaw|defrost|de-?ice)\w*\b")


def _benign_aim(pt: str, words: str) -> bool:
    """Whether an aimed-at / verb-less match is one of these everyday uses, or a tool mounted on the robot's own arm,
    wrist or hand ("a robot with a heat gun on the arm"), rather than a tool turned on a person or animal. The purpose
    ("to warm them") can trail the matched words, so the whole clause is read."""
    idx = pt.find(words)
    clause = re.split(r"[.;!?\n]", pt[idx:])[0] if idx >= 0 else words
    if re.search(_H_AIM_OK, clause):
        return True
    m = re.search(r"\b(?:mounted\s+)?on\s+(?:the|its)\s+(?:robot\s+|arm['’]s\s+)?(?:arm|wrist|hand|end|effector|gripper|"
                  r"tool[- ]?head|mount|flange)s?\b\s*$", words)
    return bool(m and "robot" in pt and not re.search(r"\b(?:my|your|his|her|our|their|someone|somebody)\b|['’]s\b",
                                                       words[:m.start()]))


# benign idioms for the hurt and restraint verb families: a game, whipping up or burning a disc, feeding sports balls,
# hair pinned or tied, a baby swaddled or strapped into a seat, a joint taped, pests and poultry secured.
_H_BENIGN_ACT = (
    r"^whip\w*\s+(?:me|him|her|us|them|my\s+[a-z-]+|the\s+[a-z-]+|[a-z-]+)\s+up\b"
    r"|^burn\w*\s+(?:(?:me|him|her|us|them)\s+|[a-z-]+\s+)?(?:a|an|the|some|another|\d+)\s+(?:[a-z-]+\s+){0,2}?"
    r"(?:cds?|dvds?|discs?|disks?|records?|mixtapes?|albums?|playlists?|blu-?rays?)\b"
    r"|^hit\w*\s+(?:me|him|her|us|them|[a-z-]+)\s+(?:[a-z-]+\s+){0,2}?(?:balls?|serves?|grounders?|flies|fly\s+balls?|"
    r"pop[- ]?ups?|lobs?|pucks?|shuttlecocks?|birdies?|returns?)\b"
    r"|^whip\w*\s+(?:me|him|her|us|them|my\s+[a-z-]+|the\s+[a-z-]+|[a-z-]+)\s+(?:up|one|another|something)\b"
    r"|^(?:pin|pins|pinned|pinning|tie|ties|tied|tying|clip|clips|braid\w*|plait\w*|put\w*)\s+(?:up\s+|back\s+)?"
    r"(?:my|your|his|her|their|the)\s+(?:[a-z-]+['’]s?\s+){0,2}?(?:hair|fringe|ponytail|bun|curls?)\b"
    r"|^(?:wrap|wraps|wrapped|wrapping|swaddl\w+)\s+(?:up\s+)?(?:the|my|your|his|her|their|a)\s+(?:[a-z-]+\s+){0,2}?"
    r"(?:bab(?:y|ies)|newborns?|infants?|child|toddlers?|hands?)\b(?!(?:(?!\btape\b|\bcling|\brope\b|\bcord\b|around).)*?"
    r"\b(?:neck|throat|mouth|face|head)s?\b)"
    r"|^strap\w*\s+(?:[a-z'’-]+\s+){0,3}?(?:in|into)\s+(?:a|an|the|his|her|their|its)\s+(?:[a-z-]+\s+){0,2}?"
    r"(?:wheelchair|car[- ]?seat|carseat|child\s+seat|booster|high[- ]?chair|seat|pushchair|pram|buggy|stroller|harness|"
    r"carrier|sling|cot|crib)\b"
    r"|^tap(?:e|es|ed|ing)\s+(?:up\s+)?(?:my|your|his|her|their|the)\s+(?:[a-z-]+['’]s?\s+){0,2}?"
    r"(?:knee|ankle|wrist|finger|thumb|hand|foot|toe|shoulder|hamstring|calf|elbow|arm|rib)s?\b"
    r"|^lock\w*\s+(?:up\s+)?(?:the\s+)?(?:chickens?|hens?|chooks?|coop|ducks?|geese|rabbits?|goats?|sheep|cattle|"
    r"livestock|poultry|animals?|birds?)\b"
    r"|^trap\w*\s+(?:the\s+)?(?:mice|mouse|rats?|moles?|vermin|pests?|flies|fly|wasps?|hornets?|insects?|bugs?|roaches?|"
    r"cockroaches?|ants?|slugs?|snails?)\b")


_H_CARE = (r"\b(?:for|while|so|until|as|when)\b(?:(?!\bhit\b|\bhurt\b|\bbeat\b|\btickl).){0,40}?\b(?:vet|"
           r"doctor|nurse|groomer|dentist|farrier|trim\w*|clip\w*|groom\w*|brush\w*|bath\w*|wash\w*|exam\w*|check\w*|"
           r"treat\w*|medic\w*|inject\w*|vaccin\w*|haircut|claws?|nails?|blood|x-?ray|physio|dress(?:es|ed|ing)?\s+"
           r"(?:the\s+|a\s+|my\s+|his\s+|her\s+|its\s+)?(?:wound|cut|graze|leg|arm|paw)|bandag\w*|shear\w*|shav\w*)\b")


def _benign_act(pt: str, m: "re.Match") -> bool:
    """Whether a hurt or restraint match is a game, a kitchen or hair idiom, safe securing, or holding an animal or a
    person still for care ("holds the cat still while i trim its claws"), not harm."""
    seg = re.split(r"[.;!?\n]", pt[m.start():])[0][:110]
    if re.search(_H_BENIGN_ACT, seg):
        return True
    if re.match(r"(?:hold|holds|holding|held|pin|pins|pinned|pinning|keep|keeps|keeping)\b", seg) and \
            re.search(r"\b(?:down|still)\b", seg) and re.search(_H_CARE, seg):
        return True
    if re.match(r"beat\w*\b", seg) and re.search(_H_GAME, pt) and \
            not re.search(r"\b(?:up|down|senseless|unconscious|black\s+and\s+blue|to\s+a\s+pulp)\b|\bwith\s+a\b", seg):
        return True
    return bool(_rx(_H_BENIGN_IDIOM).search(seg)) and not _rx(_H_IDIOM_HARM).search(seg)


# everyday idioms the hurt, smother, drown, ligature and sit-on rules would read as harm, matched from the rule's verb:
# a message shot over, whack-a-mole, a baby held in the bath, a warm towel on a face, a necklace, a robot on a shoulder,
# play throws and splashes. _H_IDIOM_HARM in the same clause keeps the refusal.
_H_BENIGN_IDIOM = (
    r"^shoot\w*\s+(?:me|him|her|us|them|you|[a-z]+)\s+(?:a|an|another)\s+(?:quick\s+|short\s+|brief\s+)?(?:message|text|"
    r"email|e-mail|note|line|dm|ping|reply|update|alert|notification|sms|photo|picture|pic|link|look|glance|smile|wave)\b"
    r"|^whack\w*\s+(?:the\s+)?moles?\b(?=.*\b(?:fair|game|arcade|carnival|funfair|kids|fun|play\w*)\b)"
    r"|^(?:hold|holds|holding|held|dip|dips|dipped|dipping|lower\w*|put\w*|place\w*)\s+(?:[\w'’-]+\s+){0,3}?"
    r"(?:in|into)\s+(?:the\s+|a\s+|her\s+|his\s+|their\s+)?(?:warm\s+)?(?:bath|tub|baby\s+bath|paddling\s+pool|basin)\b"
    r"|^(?:put|puts|putting|place|places|placed|placing|lay|lays|laid|laying|rest\w*|drap\w*)\s+(?:a|an|the|my|his|her|"
    r"some)\s+(?:[a-z-]+\s+){0,2}?(?:cloth|towel|flannel|washcloth|face\s?cloth|compress|rag|mask)s?\s+on\s+"
    r"|^(?:put|puts|putting|place|places|placed|placing|hang\w*|fasten\w*|clasp\w*|do\w*\s+up)\s+(?:a|an|the|my|his|her|"
    r"their)\s+(?:[a-z-]+\s+){0,2}?(?:chain|necklace|pendant|locket|lanyard|medal|scarf|collar|choker|beads)s?\s+"
    r"(?:around|round|on)\b"
    r"|^(?:sit|sits|sitting|sat|perch\w*|ride|rides|riding|rode|rest\w*|stand|stands|standing|lie|lies|lying)\s+on\s+"
    r"(?:top\s+of\s+)?(?:my|your|his|her|their|our|the|a)\s+(?:[a-z'’-]+\s+){0,1}?(?:shoulders?|laps?|wrists?|forearms?|"
    r"arms?|hands?|palms?|desks?|knees?)\b"
    r"|^(?:splash\w*|throw\w*|threw|toss\w*|lob\w*|flick\w*|sprinkl\w*|shower\w*)\s+(?:a\s+|the\s+|some\s+)?"
    r"(?:[a-z-]+\s+){0,1}?(?:water|confetti|petals?|rice|bubbles?|streamers?|glitter|balls?|beach\s?balls?|frisbees?|"
    r"bean\s?bags?|balloons?|snow)\b(?=.*\b(?:play\w*|game|fun|gently|softly|pool|paddling|catch|fetch|wedding|bride|"
    r"groom|party|celebrat\w*|birthday|beach)\b)(?!.*\b(?:face|eyes?|head)\b)")
_H_IDIOM_HARM = (r"\b(?:hurt\w*|harm\w*|injur\w*|hard|harder|eyes?|under|beneath|drown\w*|until|struggl\w*|"
                 r"breath\w*|tight\w*|pull\w*|chok\w*|press\w*|force\w*|hold\w*\s+down|boiling|scald\w*|hot\s+water|"
                 r"rocks?|stones?|bricks?|knives|knife|acid|bleach)\b")


def _negated(t: str, start: int) -> bool:
    """A "never", "not" or "avoid" earlier in the clause, before a verb-less match ("never points the gun at anyone")."""
    clause = re.split(r"[.;!?\n]", t[:start])[-1]
    return bool(re.search(r"\b(?:never|not|without|avoid\w*|no)\b|n't\b", clause[-80:]))


def _aimed_at(pt: str):
    """Each "aims it at someone" in `pt` (verb, then within 150 characters of the clause something that hurts or "it",
    then within 150 more "at" someone or "to" their body), as the words from the verb to the target. One pattern with
    two open gaps took most of a second on 2,000 characters of repeated verbs; this finds every verb, danger and
    target once and pairs them by position."""
    aims = list(_rx(_H_AIMED).finditer(pt))
    if not aims:
        return
    named = [m.start() for m in _rx(r"\b" + _H_DANGER).finditer(pt)]
    dangers = [(m.start(1), m.end(1)) for m in _rx(_H_DANGER_HERE).finditer(pt)]
    targets = [(m.start(1), m.end(1)) for m in _rx(_H_AIMED_AT).finditer(pt)]
    if not dangers or not targets:
        return
    ends = [m.start() for m in re.finditer(r"[.;!?\n]", pt)] + [len(pt)]
    d_starts, t_starts = [d[0] for d in dangers], [t[0] for t in targets]
    for a in aims:
        stop = ends[bisect.bisect_left(ends, a.end())]          # the clause ends here
        # "puts them in my mum's hand" (pills): after a carrying verb, "it"/"them" is a danger only when one was named
        # before it; after aim, point, fire, swing and the like it always is ("mount a bear trap ... aim it at my dad")
        carry = re.match(r"(?:put|hold|held|press|push|drop|bring|brought|move|touch|us(?:e|es|ed|ing)|tip|dump|pour)", a.group(0))
        k = bisect.bisect_left(d_starts, a.end())
        while k < len(dangers) and dangers[k][0] <= min(a.end() + 150, stop):
            d_start, d_end = dangers[k]
            if carry and pt[d_start:d_end] in ("it", "them") and not (named and named[0] < d_start):
                k += 1
                continue
            n = bisect.bisect_left(t_starts, d_end)
            while n < len(targets) and targets[n][0] <= min(d_end + 150, stop):
                yield pt[a.start():targets[n][1]]
                n += 1
            k += 1


@functools.lru_cache(maxsize=4096)
def harmful(text: str) -> str | None:
    """The words that make a request one Morph refuses, or None: a weapon, spying, something that can hurt turned on
    someone or something alive (aimed, swung, fired, thrown, poured, pressed, with or without a verb, "only at people",
    to "shut my brother up" or "keep the cat off"), someone hurt, tied, locked up or drugged, a gun that is no tool, a
    tool gun with a weapon's context (darts, a turret, guarding the door), and one of the tool guns this filter refused
    before this beta (tape, rivet, impact, scan and the like) in no harmless use the request names: handed over, held,
    cleaned, loaded, stored or sorted, used on a thing, or named as a thing's part."""
    raw = " ".join((text or "").split())[:2000]
    t = raw.lower().replace("@", " at ")
    pt = people_text(raw, agents=False).replace("@", " at ")    # names read as "someone"
    for pat in (WEAPONS, SPY):
        m = re.search(pat, t)
        if m:
            return m.group(0)
    for words in _aimed_at(pt):
        if not _self_massage(words) and not re.search(_H_AIM_OK, words) and not _benign_aim(pt, words):
            return words.strip()
    for pat in HOSTILE:
        for m in _rx(pat).finditer(pt):
            if not _self_massage(m.group(0)) and not _benign_act(pt, m):
                return m.group(0).strip()
    for pat in _H_NO_VERB:
        for m in _rx(pat).finditer(pt):
            if not _self_massage(m.group(0)) and not _negated(pt, m.start()) and not _benign_aim(pt, m.group(0)):
                return m.group(0).strip()
    if _rx(_H_ANY_DANGER).search(pt):
        for pat in HOSTILE_WITH_DANGER:
            m = _rx(pat).search(pt)
            if m:
                return m.group(0).strip()
    guns = list(re.finditer(r"\b(?:[a-z-]+[- ])?guns?\b|\b(?:nail|glue|staple)guns?\b", t))
    if guns:
        m = _rx(GUN_CONTEXT).search(t)
        if m:
            tool_only = all(re.search(r"\b(?:" + _H_OLD_TOOL_GUN + "|" + _H_NEW_TOOL_GUN + r")[- ]?guns?$",
                                      t[max(0, g.start() - 25):g.end()]) for g in guns)
            # a tool gun that builds a fence, lock or alarm guards a place by construction, not as a weapon; and a tool
            # gun handed or passed to people names them as who receives it, not a target
            recipient = (re.match(r"(?:strangers?|people|customers?|visitors?|guests?|clients?|passers)", m.group(0)) and
                         re.search(r"\b(?:hand|hands|pass|passes|give|gives|offer|offers|lend|lends|sell|sells)\w*\b.*"
                                   r"\b(?:to|for)\s+$", t[:m.start()]))
            if not (tool_only and ((_rx(_GUN_BUILDS).search(t) and not _rx(_GUN_WEAPONIZE).search(t)) or recipient)):
                return m.group(0).strip()
        for g in guns:
            near = t[max(0, g.start() - 25):g.end()]
            if not re.search(r"\b(?:" + _H_OLD_TOOL_GUN + "|" + _H_NEW_TOOL_GUN + r")[- ]?guns?$", near):
                return g.group(0)          # a gun that is no tool
            if re.search(r"\b" + _H_NEW_TOOL_GUN + r"[- ]?guns?$", near) and not _gun_harmless(t, g):
                return g.group(0)          # a tool gun refused before this beta, in no harmless use the request names
    return None


def warm(block: bool = True) -> None:
    """Compile the long request patterns once per process. They take about 2 s on a small machine, which the first
    request would otherwise wait for; the Studio calls this at start with block=False, as it does capability.warm."""
    def run():
        try:
            for pat in (*HOSTILE, *HOSTILE_WITH_DANGER, *_H_NO_VERB, GUN_CONTEXT, _H_AIMED, _H_AIMED_AT, _H_DANGER_HERE,
                        _SERVES_SOMEONE, _H_BENIGN_IDIOM, _H_IDIOM_HARM, r"\b" + _H_DANGER,
                        _H_ANY_DANGER, HAND_TO_PERSON, PEOPLE_AT_IT, PEOPLE_BESIDE, PEOPLE_AROUND, _FOR_SOMEONE,
                        THEIR_PLACE, ANIMAL_GETS):
                _rx(pat)
            for text in ("a robot that hands the operator a cup while I work next to it",
                         "the nurse loads its tray, my kids watch and it gives my dog a treat"):
                parse(text)
        except Exception:  # noqa: BLE001  a warm-up never stops the Studio; the first request compiles what is left
            pass

    if block:
        run()
    else:
        threading.Thread(target=run, daemon=True, name="brief-warm").start()


def person_signals(pt: str, t: str = "") -> dict:
    """Every sign of a person next to the robot in a request, read apart from what the robot does: {kind: words} for
    children, beside (someone works next to it, takes from, loads or uses it, "next to me"), receiver (someone it hands
    things to), near (someone it helps, serves or works for, or an unsure request that leaves someone there) and else
    ("no one else is around"). `pt` is people_text(request), `t` the request in lower case. Empty means no person."""
    found = {}
    kids = re.search(_KIDS, re.sub(r"\b(?:no|without|not any)\s+(?:kids|children|toddlers|babies)\b", " ", pt))
    receiver = _rx(HAND_TO_PERSON).search(pt) or re.search(r"\bhand[- ]?(?:overs?|offs?)\b", pt)   # "a handover robot"
    school = re.search(r"\b(?:classrooms?|(?:primary|elementary|junior|infant|nursery|middle|high|secondary|grade|pre-?)\s*schools?|"
                       r"schools?\b(?!\s+of\b)|nurser(?:y|ies)|kindergartens?|daycares?|day care|playgroups?|creche)\b", pt)
    # someone the robot helps, serves or works for is a person named; an unsure setting or "cobot" only leaves someone
    # likely there. An explicit "nobody around" beats the unsure one ("a cobot that runs overnight with nobody around"),
    # but never a person the request names.
    for_someone = (_rx(_FOR_SOMEONE).search(pt) or _rx(_SERVES_SOMEONE).search(pt) or   # "a robot doorman" greets people
                   re.search(r"\b(?:robot(?:ic)?|cobot)\s+(?:doorman|doormen|greeters?|ushers?|bouncers?|baristas?|bartenders?|"
                             r"waiters?|waitress(?:es)?|receptionists?|concierges?)\b", t or pt))
    ambient = _rx(PEOPLE_AROUND).search(pt) or _rx(THEIR_PLACE).search(pt)
    if ambient and re.search(_NUMBERED_PLACE, ambient.group(0)) and re.search(_STATION_PLACE, t or pt):
        # "moves the seedlings to table 4" in a greenhouse: a station; look again without it
        rest = pt[:ambient.start()] + " " + pt[ambient.end():]
        ambient = _rx(PEOPLE_AROUND).search(rest) or _rx(THEIR_PLACE).search(rest)
    if ambient and not for_someone and (t or pt) and re.search(NOBODY_NEAR, t or pt):
        # explicit "nobody around" beats a bare "cobot"/"assistive" hint, but never a person the request describes
        # ("the operator swaps the bins, nobody near it" keeps the operator)
        hits = list(_rx(PEOPLE_AROUND).finditer(pt)) + list(_rx(THEIR_PLACE).finditer(pt))
        if all(re.fullmatch(_COBOT_UNSURE, h.group(0).strip()) for h in hits):   # "a cobot barista" serves someone
            ambient = None
    for kind, m in (("children", kids), ("beside", _rx(PEOPLE_BESIDE).search(pt) or _rx(PEOPLE_AT_IT).search(pt)),
                    ("receiver", receiver), ("near", for_someone or ambient),
                    ("school", school), ("else", re.search(r"\b(?:no ?one|nobody|no-one)\s+else\b", t or pt))):
        if m:
            found[kind] = m.group(0).strip()
    return found


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


# where a size or a shape word names a destination, not the object ("into 100 mm cups", "sort parts into bins")
_DET = r"(?:a|an|the|my|your|our|their|its|each|every|two|three|some)"
_TO_A_THING = re.compile(r"(?:\b(?:into|onto|inside|out of|next to|beside)\s+(?:" + _DET + r"\s+)?|\b(?:in|on|to|from|off)\s+" +
                         _DET + r"\s+)(?:[a-z-]+\s+){0,2}$")
_NOT_A_SIZE = re.compile(r"reach|finger|hole|mount|spacing|longer|shorter|apart|away|from the base|travel|stroke|clearance|"
                         r"\bgaps?\b|height|depth|length|\blift|\braise|\blower")
_NOT_A_SIZE_JUST_BEFORE = re.compile(r"accura|precis|toleran|resolution|repeatab|within|error|offset")
# a length the request gives the object: "50 mm wide", "Ø50 mm", "(50 mm)", "tubes of 50 mm", "measuring 50 mm", "50 mm,
# 30 g tubes", "50 mm oranges", "about 50 mm"; not a distance, a reach or a part of the robot
_WIDTH = re.compile(
    r"(?:ø|⌀|diameter(?: of)?|dia\.?|measuring|\(|\bof)\s*(\d+(?:\.\d+)?)\s*mm(?!\s*(?:away|apart|from|reach|long|high|tall|"
    r"deep|of travel|travel|stroke))"
    r"|(\d+(?:\.\d+)?)\s*mm\s*(?:(?:glass|plastic|metal|wooden|paper|steel|heavy|light|round|square)\s+)*(?:wide|across|"
    r"in diameter|diameter|dia\b|thick|ball|sphere|cube|box(?:es)?|block|brick|cylinder|tubes?|objects?|cans?|bottles?|jars?|"
    r"cups?|parts?)"
    r"|(\d+(?:\.\d+)?)\s*mm\s*(?=(?:[,/]|\s+and)?\s*\d+(?:\.\d+)?\s*g\b)"                       # "50 mm, 30 g tubes", "40 mm 300 g cans"
    r"|(\d+(?:\.\d+)?)\s*mm\s*,\s*(?=[a-z]+(?:\s+[a-z]+)?\s*,?\s*(?:weighing|that\s+weighs?|each)\b)"   # "60 mm, cylinders weighing 85 g"
    r"|(\d+(?:\.\d+)?)\s*mm\s*(?:,|and|or|to|-|–)\s*(?=\d+(?:\.\d+)?\s*mm\b)"            # "60 mm and 30 mm cylinders"
    r"|(\d+(?:\.\d+)?)\s*mm\s+(?:on\s+)?(?:each|a|every|per)\s+side\b"                    # "boxes 40 mm on each side"
    r"|(?:\bx|\bby|×)\s*(\d+(?:\.\d+)?)\s*mm\b"                                            # "boxes that are 40 by 40 mm"
    r"|(\d+(?:\.\d+)?)\s*mm\s+(?!(?:away|apart|from|to|into|onto|in|on|at|of|per|each|and|or|up|down|left|right|forwards?|"
    r"backwards?|back|higher|lower|further|farther|more|less|above|below|over|under|off|high|tall|long|deep|reach|travel|"
    r"stroke|gap|clearance|spacing|steps?|increments?|accuracy|precision|tolerance|resolution|then|so|but|with|by|for|is|are|"
    r"was|were|be|it|them|the|a|an|max|maximum|min|minimum|radius|x|holes?|arm|robot|gripper|base|table|bench|desk|shelf|"
    r"shelves|conveyor|belt|rail|track|workspace|area|zone|stand|platform|column|link|fingers?|jaws?|camera|servos?|motors?|"
    r"screws?|bolts?|drill)\b)[a-z][a-z-]+"
    r"|\b(?:about|around|roughly|approximately|~)\s*(\d+(?:\.\d+)?)\s*mm\b(?!\s*(?:away|apart|from|reach|long|high|tall))")


def _width_reads(t: str) -> list:
    """[(mm, start, end)] for each length _sizes reads as its object's width; start and end span the "50 mm"."""
    out = []
    for m in _WIDTH.finditer(t):
        if _NOT_A_SIZE.search(t[max(0, m.start() - 24):m.end()]) or \
                _NOT_A_SIZE_JUST_BEFORE.search(t[max(0, m.start() - 16):m.start()]) or \
                _TO_A_THING.search(t[max(0, m.start() - 40):m.start()]):
            continue
        i = next(k for k, g in enumerate(m.groups(), 1) if g)
        out.append((float(m.group(i)), m.start(i), m.end(i) + len(re.match(r"\s*mm", t[m.end(i):]).group(0))))
    return out


_RANGE_LOW = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:mm\s*)?(?:to|-|–|and|or)\s*$")


def _least_width(t: str) -> float | None:
    """The smallest width the words give the objects when they give several or a range ("40 mm and 20 mm cylinders",
    "cylinders between 20 and 40 mm wide", "20 to 40 mm balls"): what the stock SO-101's jaws must still hold."""
    ws = []
    for v, s, _e in _width_reads(t):
        low = _RANGE_LOW.search(t[max(0, s - 24):s])
        ws += [v] + ([float(low.group(1))] if low and 0 < float(low.group(1)) < v else [])
    return min(ws) if len(ws) > 1 and min(ws) < max(ws) else None


def _mass_reads(t: str) -> list:
    """[(g, start, end)] for each mass _sizes reads as its object's: never one that names where things go."""
    if re.search(r"\bg-?force|\bgs\b", t):
        return []
    return [(float(m.group(1)), m.start(), m.end()) for m in re.finditer(r"(\d+(?:\.\d+)?)\s*g\b", t)
            if not _TO_A_THING.search(t[max(0, m.start() - 40):m.start()])]


# A mass per hour, minute, day or shift ("2 kg per hour", "2 kg/h", "2 kg of them a day") or a batch's ("in 2 kg
# batches", "a batch of 2 kg", "2 kg of 50 g cubes", "2 kg of them") is a rate or a total, not one object's
_RATE_AFTER = re.compile(r"\s*(?:of\s+(?:them|those|these|[a-z-]+)\s+)?(?:/\s*|(?:per|an?|each|every)\s+)(?:h|hrs?|hours?|mins?|"
                         r"minutes?|days?|shifts?|weeks?|months?)\b|\s*(?:of\s+(?:them|those|these|[a-z-]+)\s+)?(?:hourly|daily|"
                         r"weekly)\b")
_BATCH_AFTER = re.compile(r"\s*-?\s*batch(?:es)?\b|\s+(?:per|a|each|every|in\s+a|in\s+each)\s+batch\b|\s+of\s+(?:them|those|these)"
                          r"(?!\s+(?:[a-z-]+\s+)?(?:go|goes|in|inside|into|per\s+(?:jar|bottle|cup|box|bag|tin|can|pot|tub)|each)\b)"
                          r"(?!\s+(?!a\b|an\b|per\b|every\b|each\b|an?\s|in\s+(?:a|an|one|total)\b)[a-z]{3,})|\s+of\s+"
                          r"\d+(?:\.\d+)?\s*g\b|\s+for\s+(?:all\s+of\s+them|them\s+all|the\s+(?:lot|batch|whole\s+batch))\b")
_BATCH_BEFORE = re.compile(r"\bbatch(?:es)?\s+of\s+(?:about\s+|around\s+|roughly\s+|up\s+to\s+)?$")
# a mass tied to the noun before it ("pouches of 300 g every minute", "cubes weighing 50 g"): that thing's own
_TIED_BEFORE = re.compile(r"(?:\b(?:of|weighing|weighs?|weighed|is|are|was|were)|['’](?:re|s))\s+(?:about\s+|around\s+|"
                          r"roughly\s+)?$")
# ... and words that say an object weighs more than a mass the request gives it: empty, what it holds, a lid or a cap,
# some of them ("the small ones"). With any of them the rate stays as read: dropping it could leave a part's, some of
# the objects' or the empty weight as the object's.
_MORE_THAN_SAID = re.compile(r"\b(?:empty|full|unfilled|fill\w*|holds?|holding|contain\w*|contents?|plus|includ\w*|exclud\w*|"
                             r"same|not\s+all|identical|uniform|range|ranging|between|times|thrice|x\s*\d+|\d+\s*x|"
                             r"(?:light|heavy|small|low|bottom)(?:er)?\s+(?:side|end)|lowest|least|baseline|floor|starting|"
                             r"starts|from\s+\d|bigger|larger|denser|thicker|taller|longer|"
                             # what they sit in or on, or come in: the arm may lift it with them ("they're stored in 30 mm
                             # cups", "jars in sleeves", "the tray has pockets", "they hang on hooks")
                             r"(?:in|on|inside|within)\s+(?:their\s+|the\s+|its\s+|his\s+|her\s+|my\s+|our\s+|some\s+)?"
                             r"(?:\d+(?:\.\d+)?\s*mm\s+)?(?:(?:wide|deep|tall|long|big|small|plastic|paper|cardboard)\s+)?"
                             r"(?!batch)[a-z]+s|(?:has|have|had)\s+(?:\d+(?:\.\d+)?\s*mm\s+)?(?:wide\s+)?[a-z]+s|"
                             r"without|lids?|caps?|tops?|corks?|stoppers?|labels?|handles?|packaging|wrappers?|liquids?|water|"
                             r"coffee|tea|juice|milk|soup|sauce|ones|(?:some|most|few|several|many|half|rest|one|two|three)\s+of|"
                             r"smallest|lightest|biggest|heaviest|largest|varies|vary|varying|heavier|lighter|more|double|triple|"
                             r"twice|differ\w*|different|average|avg|typical\w*|mostly|usually|normally|generally|roughly\s+between|"
                             r"(?<!per\s)(?<!/)(?<!/\s)(?<!\ba\s)(?<!every\s)min|minimum|at\s+(?:the\s+)?(?:very\s+)?least|upwards?|or\s+more|and\s+up|inside|loaded|loads?|"
                             r"before|after|when|whenever|once|until|wet|damp|soak\w*|dry|dried|net|tare|gross|nothing|others?|"
                             r"odd|occasional\w*|sometimes|can\s+be|may\s+be|might\s+be|could\s+be|or\s+so|give\s+or\s+take|"
                             r"ish|low(?:er)?\s+end|bottom\s+end|ml|cl|l|lit(?:re|er)s?|millilit\w+|cc|at\s+a\s+time|at\s+once|"
                             r"in\s+one|per\s+(?:grab|lift|pick|trip|load|scoop|grasp|move|cycle|carry|go|run|tray|bag|box)|"
                             r"together|simultaneous\w*|scoop\w*|handfuls?|armfuls?|go(?:es)?\s+in|put\s+in|"
                             r"with\s+the\s+[a-z]+\s+in|of\s+(?!them\b|those\b|these\b|\d)[a-z]+|"
                             r"(?!g\s|batch(?:es)?\s)[a-z]+\s+of\s+(?:about\s+|around\s+|roughly\s+|approx\w*\s+)?\d+)\b|\+")
# a later message's clause that is a rate alone ("about 2 kg an hour", "in 2 kg batches", "2 kg of them a day")
_RATE_ALONE = re.compile(r"\s*(?:(?:about|around|roughly|approximately|approx|at|and|also|plus|~)\s*)*(?:in\s+)?"
                         r"(?:(?:a\s+)?batch(?:es)?\s+of\s+)?\d+(?:\.\d+)?\s*g\s*(?:-?\s*batch(?:es)?|(?:of\s+(?:them|these|"
                         r"those)\s*)?(?:/\s*(?:h|hrs?|hours?|mins?|minutes?|days?|shifts?|weeks?)|(?:per|an?|each|every)\s+"
                         r"(?:h|hrs?|hours?|mins?|minutes?|days?|shifts?|weeks?|months?)|hourly|daily|weekly|monthly)|of\s+(?:them|"
                         r"these|those))?\s*(?:approx\.?|approximately|roughly|or\s+so)?\s*[.!]*\s*")


def _clause(t: str, at: int) -> str:
    """The clause of `t` around position `at`: between commas, semicolons, sentence ends and line breaks (never a
    decimal point)."""
    ends = list(re.finditer(r"[,;!?\n]|\.(?!\d)", t))
    start = max([m.end() for m in ends if m.start() < at], default=0)
    end = min([m.start() for m in ends if m.start() >= at], default=len(t))
    return t[start:end]


# a mass per hour, minute, day or shift, not a batch or a total ("2 kg of 50 g cubes per shift", "2 kg of them a day")
_TIMED = re.compile(r"[^.;!?,]{0,40}?(?:\b(?:per|an?|each|every)\s+(?:h|hrs?|hours?|mins?|minutes?|days?|shifts?|weeks?|"
                    r"months?)\b|/\s*(?:h|hrs?|hours?|mins?|minutes?|days?|shifts?|weeks?)\b|\b(?:hourly|daily|weekly|monthly)\b)")
# a later message about their weight beside a rate left out before ("they weigh more when wet"): the rate comes back
_WEIGHT_TALK = re.compile(r"\b(?:weigh\w*|weight\w*|heav\w*|light\w*|mass|masses|kg|g|grams?|oz|lbs?|pounds?|ounces?)\b")
# a batch said of how the robot handles them is what it lifts at once ("it lifts them in 2 kg batches", "pick up 50 g
# cubes in 2 kg batches"), not a batch they are made or come in ("sort the 50 g blocks we bake in 2 kg batches")
_HANDLES = re.compile(r"\b(?:(?P<h>lift|carr|grab|grasp|pick|hold|mov|transfer|scoop|load|tip|pour|dump|tak|took|bring|brought|"
                      r"fetch|handl|shift|haul|hoist|rais|tote|lower|plac|put|drop|deliver|stack|push|pull|throw|toss)\w*|"
                      r"(?P<p>make|makes|made|making|bakes?|baked|baking|cooks?|cooked|cooking|produced?|produces|producing|"
                      r"cast|casts|mou?lded|printed|mixed|brewed|roasted|dried|comes?|came|arrives?|arrived|shipped|ordered|"
                      r"bought|sold|prepared|supplied)\b)")


def _rate_or_batch(t: str, s: int, e: int) -> bool:
    """Whether the mass spanning t[s:e] is a rate or a batch's total ("2 kg per hour", "in 2 kg batches", "2 kg of them"),
    never one tied to the thing it weighs ("300 g pouches every minute", "pouches of 300 g every minute")."""
    if _BATCH_AFTER.match(t, e) or _BATCH_BEFORE.search(t[max(0, s - 40):s]):
        if re.search(r"batch", t[max(0, s - 40):e + 30]):
            verbs = list(_HANDLES.finditer(re.split(r"[;!?]|\.(?!\d)", t[:s])[-1]))
            if verbs and verbs[-1].group("h"):
                return False   # "it lifts them in 2 kg batches": what it lifts at once
        return True
    after = _np_after(t, e)
    if after and after[0] != "of" or _TIED_BEFORE.search(t[max(0, s - 30):s]) and _np_before(t, s)[0]:
        return False
    return bool(_RATE_AFTER.match(t, e))


def _names_objects(t: str, s: int, e: int, heads: set, words: set) -> bool:
    """Whether the size spanning t[s:e] is plainly the objects' own by the noun it is said of ("50 g cubes", "cubes
    weighing 50 g", "each cube is 50 g"): never a part's, some of them', a pronoun's, an untied or a hedged one."""
    np = _np_after(t, e)
    lead = []
    if not np:
        np, lead = _np_before(t, s)
    if not np or np[0] == "of" or lead and (lead[-1] in _PREPS or lead[-1] in _QUANTS) or _not_plain(t, s, e):
        return False
    return _names_what(np, heads, words) == "obj"


def _rates_to_drop(t: str, masses: list, objects: tuple = (set(), set()), own_elsewhere: bool = False,
                   context: str = "", said=None) -> set:
    """The masses in `masses` (_mass_reads(t)) that are a rate or a batch's total beside the object's own mass, to
    leave out: only when every other mass the message gives is plainly the objects' own (_names_objects, `objects` is
    _object_words of them) and there is one, or `own_elsewhere` (an earlier message gave it), nothing in `context`
    (the conversation) says the object weighs more than a mass given for it, and the whole conversation (`said`, the
    messages as typed) is plain (_plain_talk)."""
    rates = {i for i, (_v, s, e) in enumerate(masses) if _rate_or_batch(t, s, e)}
    own = [i for i in range(len(masses)) if i not in rates]
    if not rates or not (own or own_elsewhere) or _MORE_THAN_SAID.search(context or t) or not own and _CORRECTS.match(t):
        return set()
    untimed = any(not _TIMED.match(t, masses[i][2]) for i in rates)
    if not _rate_drop_holds([t] if said is None else said, objects, untimed):
        return set()   # "some are quite heavy", "it carries 2 kg of them", "pick up 2 kg of 50 g cubes": stays as read
    if not own and not all(_RATE_ALONE.fullmatch(_clause(t, masses[i][1])) for i in rates):
        return set()   # "should be 300 g every minute", "it's 300 g every minute": it may correct their mass
    if not all(_names_objects(t, masses[i][1], masses[i][2], *objects) for i in own):
        return set()   # "the pills weigh 1 g each", "at least 20 g", "the jam 100 g": the rate stays as read
    for _v, s, e in _width_reads(t):   # "pack 30 mm tubes at 2 kg per hour; the bulbs are 25 g": two kinds of thing
        np = _np_after(t, e) or _np_before(t, s)[0]
        if np and _names_what(np, *objects) != "obj":
            return set()
    return rates


# Words that say nothing about how much an object weighs or how many the arm lifts at once: what the request asks for,
# where things go, the objects' sizes, a rate and who is around. A rate or a batch is left out only when the whole
# conversation is in these words (and the objects' own nouns), since no list of the words that do ("some are quite
# heavy", "excl. jam", "it carries 2 kg of them", "oh wait, 300 grams every minute") is ever complete.
_PLAIN_WORDS = frozenset("""
    a an the my our your this that these those it its it's they they're them their i i'm i've i'd we we're we've we'd
    you me us who which have has got want wants need needs would like please can could should will to is are was were
    robot robots arm build make design get for and also then help hi hello thanks thank let let's set
    use using pick picks picked picking up move moves moved moving sort sorts sorted sorting stack stacks stacking place
    places placing put puts putting transfer transfers transferring tidy tidies tidying arrange arranges arranging
    organise organize organises organizes organising organizing pack packs packing insert inserts inserting process
    processes work works run runs
    bake bakes baked baking cook cooks cooked produce produces produced print prints printed cast casts mould moulds
    moulded mold molds molded
    into onto in on at from of off out over across left right front back behind next beside here there where one
    mm g wide across diameter weighing weigh weighs each apiece about around roughly approximately approx exactly
    per every hour hours h hr hrs minute minutes min mins day days daily hourly shift shifts week weeks weekly month
    months monthly s sec secs second seconds batch batches
    people person nearby near kids children child staff workers colleagues family nobody gently carefully fast quickly
""".split())
# a number is a size, a mass or a pace ("30 mm", "50 g", "one every 6 s"), never one alone ("up to 120", "120 or 90 g")
# or a count ("in 10s")
_PLAIN_NUMBER = re.compile(r"\d+(?:\.\d+)?(?=\s*(?:mm|g)\b)")
_PLAIN_PACE = re.compile(r"\b(?:every|each|per)\s+(?:about\s+|around\s+|roughly\s+)?(\d+(?:\.\d+)?)\s*(?:s|secs?|seconds?)\b")
# a place, plain only as where things go or come from ("into the bins", "on the table", "from the left shelf"); said
# otherwise it may be what the arm lifts with them ("the bins they come in", "move the bin with ...")
_WHERE_THEY_GO = re.compile(r"\b(?:into|onto|to|on|in|at|from|off|out\s+of|of|over|across|next\s+to|beside|behind|"
                            r"in\s+front\s+of)\s+(?:(?:the|a|an|its|their|my|our|separate|different|another|other|same|"
                            r"each)\s+)*(?:(?:left|right|top|bottom|front|back|far|near|next|first|second|third|last)\s+)*"
                            r"(?:bins?|tables?|shel(?:f|ves)|conveyors?|belts?|lines?|stations?|areas?|spots?|sides?|ends?)\b")
# characters a plain message may have: "/" only in a rate ("2 kg/h"), never "120/90 g", "50±20 g", "≥50 g" or "~"
_PLAIN_CHARS = re.compile(r"[a-z0-9\s,.;:!?()'’\"-]|(?<![\d\s])/(?=\s*(?:h|hrs?|hours?|mins?|minutes?|days?|shifts?|weeks?)\b)")


def _plain_talk(said, objects: tuple = (set(), set())) -> bool:
    """Whether every message in `said` (the person's words, as typed) is plain (_PLAIN_WORDS, the objects' own head
    nouns from `objects` (_object_words; not the other words of a preset's name, such as the samples of "glass vial of
    samples"), the SO-101, sizes, masses and times), so that a rate or a batch beside the objects' own mass can be left
    out. The objects' own noun as what holds them ("50 g boxes, in their box") is not plain."""
    heads, _words = objects
    for raw in said:
        if len(raw or "") >= 1990:
            return False   # a message cut at 2,000 characters may have said more
        so = r"(?:\b(?:with|using|on|for|to)\s+(?:my|our|the|an?|this|their)\s+)?" + _SO
        x = re.sub(so, " ", (raw or "").lower())
        if _PLAIN_CHARS.sub("", x).strip():
            return False
        n = _WHERE_THEY_GO.sub(" ", re.sub(so, " ", normalize_units(x)))
        paces = {m.start(1) for m in _PLAIN_PACE.finditer(n)}
        for m in re.finditer(r"\d+(?:\.\d+)?", n):
            if not (_PLAIN_NUMBER.match(n, m.start()) or m.start() in paces) or \
                    m.start() and (n[m.start() - 1].isdigit() or n[m.start() - 1] == "."):
                return False
        for w in re.findall(r"[a-z]+(?:['’][a-z]+)?", n):
            w = w.replace("’", "'")
            if w not in _PLAIN_WORDS and _singular(w) not in heads and w not in heads:
                return False
        for m in re.finditer(r"\b(?:in|inside|within|on)\s+(?:a|an|the|their|its|one|each|my|our)\s+([a-z]+)\b", n):
            if _singular(m.group(1)) in heads:
                return False
    return True


# what the arm does that may lift several at once, or a batch ("pick up 2 kg of 50 g cubes", "it moves them in batches",
# "get 50 g cubes in 2 kg batches"): any verb of the plain words but sorting, tidying, arranging or making them
_LIFTS = re.compile(r"\b(?:pick\w*|mov\w*|stack\w*|plac\w*|put\w*|transfer\w*|pack\w*|insert\w*|process\w*|work\w*|run\w*|"
                    r"gets?|sets?|load\w*|unload\w*|lift\w*|carr\w*|grab\w*|grasp\w*|hold\w*|scoop\w*|tip\w*|pour\w*|"
                    r"dump\w*|tak\w*|took|bring\w*|brought|fetch\w*|handl\w*|shift\w*|haul\w*|hoist\w*|rais\w*|tote\w*|"
                    r"lower\w*|drop\w*|deliver\w*|push\w*|pull\w*|throw\w*|toss\w*|feed\w*|fill\w*|serv\w*|giv\w*|gave|"
                    r"hand\w*|pass\w*|offer\w*)\b")


# what is done to each object, never to a batch at once ("sort 50 g blocks in 2 kg batches"); tidying or arranging
# them may move a batch at once
_PER_ITEM = re.compile(r"\b(?:sort|sorts|sorted|sorting)\b")


# The few whole messages a rate or a batch is left out in: a request that gives the objects' own mass by their noun
# with a rate per hour, day or shift or a batch they are sorted in, a reply that is the rate alone, or who is around. No
# list of the words that say more ("move the stack", "one move per hour", "for a day", "the 95 g tins") is complete, so
# any other message keeps the rate as read.
_T_FRAME = (r"(?:(?:please\s+)?(?:use\s+my\s+SO\s+to|i\s+have\s+an?\s+SO\s*[.,;]?\s+(?:it\s+should|use\s+it\s+to)|i\s+have\s+an?\s+"
            r"SO\s+and\s+(?:i\s+)?want\s+it\s+to|my\s+SO\s+should|(?:with|using)\s+my\s+SO\s*,?|an?\s+(?:robot\s+)?arm\s+"
            r"(?:that|to)(?:\s+can)?|an?\s+robot\s+(?:that|to)(?:\s+can)?|build\s+(?:me\s+)?an?\s+robot\s+(?:arm\s+)?to|"
            r"i\s+(?:want|need)\s+an?\s+robot\s+(?:arm\s+)?to|help\s+(?:me|us))\s+)?")
_T_VERB = r"(?:pick(?:s|ing)?\s+up|mov(?:e|es|ing)|sort(?:s|ing)?|transfer(?:s|ring)?|plac(?:e|es|ing)|stack(?:s|ing)?)"
_T_SORT = r"(?:sort(?:s|ing)?|tid(?:y|ies|ying)|arrang(?:e|es|ing)|organi[sz](?:e|es|ing))"
_T_WIDTH = r"(?:\d+(?:\.\d+)?\s*mm(?:\s+wide)?,?\s+)?"
_T_MASS = r"\d+(?:\.\d+)?\s*g"
_T_ABOUT = r"(?:(?:about|around|roughly|approximately|approx\.?|~)\s*)?"
_T_UNIT = r"(?:hour|minute|min|day|shift|week)"
_T_TIME = r"(?:(?:per|an?)\s+(?P<unit>" + _T_UNIT + r")\b|/\s*(?P<unit2>h|hr|hour|min|day|shift)\b)"
_T_OWN = (r"(?:(?P<m1>" + _T_MASS + r")\s+(?P<n1>[a-z]+)|(?P<n2>[a-z]+)\s+(?:weighing|that\s+weigh|of)\s+(?P<m2>" + _T_MASS +
          r")(?:\s+each)?|(?P<n3>[a-z]+),?\s+each\s+(?P<n4>[a-z]+)\s+is\s+(?P<m3>" + _T_MASS + r"))")
_T_DEST = r"(?:\s+(?:into|onto|to|on)\s+(?:the\s+|a\s+)?(?:bins?|tables?|shel(?:f|ves)|conveyor|belt))?"
_T_RATE = (r"(?:,\s*|\s+)(?:at\s+)?" + _T_ABOUT + r"(?P<r>" + _T_MASS + r")\s*(?:of\s+them\s+)?" + _T_TIME)
_T_END = r"\s*[.!]?\s*$"
_T_WHO = r"(?:,\s*|\.\s*)(?:people|kids|children|staff|nobody|no\s+one)\s+(?:nearby|around)"
_T_REQUEST = [re.compile(r"^\s*" + _T_FRAME + v + r"\s+" + _T_WIDTH + _T_OWN + _T_DEST + tail + _T_DEST + r"(?:" + _T_WHO + r")?" + _T_END)
              for v, tail in ((_T_VERB, _T_RATE), (_T_SORT, _T_RATE), ("(?:" + _T_VERB + "|" + _T_SORT + ")", ""))] + [
    re.compile(r"^\s*" + _T_FRAME + _T_SORT + r"\s+" + _T_WIDTH + _T_OWN + r"\s+in\s+" + _T_ABOUT + r"(?P<r>" + _T_MASS +
               r")\s*-?\s*batches" + _T_END),
    re.compile(r"^\s*" + _T_FRAME + _T_SORT + r"\s+" + _T_ABOUT + r"(?P<r>" + _T_MASS + r")\s+of\s+" + _T_WIDTH +
               r"(?P<m1>" + _T_MASS + r")\s+(?P<n1>[a-z]+)(?:\s+" + _T_TIME + r")?" + _T_END)]
_T_REPLY = [re.compile(r"^\s*(?:at\s+)?" + _T_ABOUT + r"(?P<r>" + _T_MASS + r")\s*(?:of\s+them\s+)?" + _T_TIME + r"(?:" +
                       _T_WHO + r")?" + _T_END),
            re.compile(r"^\s*(?:in\s+)?" + _T_ABOUT + r"(?P<r>" + _T_MASS + r")\s*-?\s*batches" + _T_END),
            re.compile(r"^\s*(?:people|kids|children|staff|nobody|no\s+one)\s+(?:nearby|around|next\s+to\s+it)" + _T_END),
            re.compile(r"^\s*one\s+every\s+\d+(?:\.\d+)?\s*s(?:ec(?:ond)?s?)?" + _T_END)]
# what a mass said of ("50 g cubes", "cubes weighing 50 g", "each cube is 50 g") is plainly the whole weight of: solid
# things, never a container's label or what it holds ("95 g tins", "each jar is 45 g", "jams weighing 90 g"): beside
# those the rate stays as read
_SOLIDS = frozenset(("cube", "block", "ball", "cylinder", "disc", "disk", "rod", "puck", "brick", "tile", "pebble", "stone",
                     "marble", "bead", "nut", "bolt", "washer", "screw", "toy", "cookie", "biscuit", "coin", "token",
                     "peg", "dowel", "plug", "magnet", "egg", "widget", "gear", "bearing", "spring", "clip", "pin", "ring",
                     "button", "sphere", "prism", "figure", "figurine", "apple", "orange", "lemon", "lime", "potato",
                     "onion", "tomato", "peach", "plum", "muffin", "bun", "bagel", "donut", "doughnut"))


# what is weighed whole as it is handled: a container said to weigh a mass ("tins weighing 95 g") weighs that, with
# what it holds; never what it holds named alone ("jams weighing 90 g" may be the jam's)
_CONTAINER_NOUNS = frozenset(("box", "jar", "tin", "can", "bottle", "cup", "mug", "glass", "tub", "pot", "vial", "tube",
                              "pouch", "bag", "carton", "case", "tray", "crate", "basket", "bucket", "bowl", "flask",
                              "sachet", "packet", "pack", "container", "canister", "cartridge", "capsule", "pod", "spool",
                              "reel", "jug", "carafe", "beaker", "tumbler", "pail", "keg", "drum", "syringe", "sleeve",
                              "punnet"))


def _solid(noun: str) -> bool:
    return bool({noun, _singular(noun)} & _SOLIDS or noun.endswith("ies") and noun[:-1] in _SOLIDS)   # "cookies": cookie


def _held_whole(noun: str) -> bool:
    return bool({noun, _singular(noun)} & _CONTAINER_NOUNS)


# minutes the amount a rate gives is done in: an hour's in an hour, and a day's, a shift's or a week's maybe in one too
# (the arm may run only part of the day), so never more than one object a lift then either
_PER_MINUTE = {"hour": 60.0, "h": 60.0, "hr": 60.0, "minute": 1.0, "min": 1.0, "day": 60.0, "shift": 60.0, "week": 60.0}


def _rate_template(said, objects: tuple, cycle: float = 6.0) -> bool:
    """Whether every message in `said` is one of the few a rate or a batch is left out in (_T_REQUEST, _T_REPLY): the
    first one the request, said of the objects by their own head noun (`objects`), and no faster than the arm moves
    one object at a time (one every `cycle` s, the pace it is built at, or a slower one a reply gives, "one every 30 s";
    never more than 15 a minute); a reply with a batch only after a request to sort them."""
    heads = objects[0]
    msgs = [re.sub(_SO, " SO ", normalize_units((w or "").lower())).replace("’", "'") for w in said]
    if not msgs:
        return False
    first = next((m for m in (r.match(msgs[0]) for r in _T_REQUEST) if m), None)
    if first is None:
        return False
    nouns = [first.group(k) for k in ("n1", "n2", "n3", "n4") if k in first.re.groupindex and first.group(k)]
    own = [float(re.match(r"[\d.]+", first.group(k)).group(0)) for k in ("m1", "m2", "m3")
           if k in first.re.groupindex and first.group(k)]
    if not nouns or not own or max(own) <= 0 or any(_singular(n) not in heads for n in nouns):
        return False
    if len(re.findall(r"\d+(?:\.\d+)?\s*mm\b", msgs[0])) != len(_width_reads(msgs[0])):
        return False   # a length the rules don't read may be their width: the release's misread rate kept the SO-101 off
    weighed = re.search(r"\b(?:weighing|that\s+weigh)\s+" + _T_MASS, first.group(0))
    if not all(_solid(n) or weighed and _held_whole(n) for n in nouns):
        return False   # "95 g tins", "each jar is 45 g", "jams weighing 90 g": a label may give what they hold
    sorting = bool(re.search(r"\b" + _T_SORT, msgs[0]))
    rates = [first]
    for m in msgs[1:]:
        r = next((x for x in (t.match(m) for t in _T_REPLY) if x), None)
        if r is None or "batches" in m and not sorting:
            return False
        rates.append(r)
    every = [float(x) for m in msgs[1:] for x in re.findall(r"^\s*one\s+every\s+(\d+(?:\.\d+)?)\s*s", m)]
    most = min(15.0, 60.0 / max([float(cycle), 4.0] + every))   # objects a minute, one at a time
    for r in rates:
        unit = r.groupdict().get("unit") or r.groupdict().get("unit2")
        if r.groupdict().get("r") and unit and \
                float(re.match(r"[\d.]+", r.group("r")).group(0)) / _PER_MINUTE[unit] / max(own) > most:
            return False   # "50 g balls at 2 kg per minute", "600 g a minute" then "one every 30 s": several a lift
    return True


def _rate_drop_holds(said, objects: tuple, untimed: bool, cycle: float = 6.0, chips=frozenset()) -> bool:
    """Whether the whole conversation (`said`, as typed) still lets a rate or a batch beside the objects' own mass be
    left out: plain (_plain_talk), in the few whole messages of _rate_template at the pace the arm is built at
    (`cycle`), one width for the objects (two may be two kinds of them: "50 g cubes and 34 mm cubes"), no arm that may
    lift a batch or a total at once (`untimed`: a batch or a total, not per hour, day or shift, only where each object
    is sorted and no task or layout chip was answered, `chips`; or any batch said beside a verb that lifts), and no
    batches of the objects ("batches of 50 g cubes")."""
    if not _plain_talk(said, objects) or not _rate_template(said, objects, cycle):
        return False
    msgs = [normalize_units(re.sub(_SO, " ", (w or "").lower())) for w in said]
    if len({round(v, 1) for n in msgs for v, _s, _e in _width_reads(n)}) > 1:
        return False
    text = " ".join(msgs)
    if _LIFTS.search(text) and (untimed or re.search(r"\bbatch", text)):
        return False
    if untimed and (not _PER_ITEM.search(text) or _WHERE_THEY_GO.search(text) or {"task", "layout"} & set(chips)):
        return False   # a batch or a total left out only where each object is sorted, nowhere taken, the job not chosen
    return not re.search(r"\bbatch(?:es)?\s+of\s+\d+(?:\.\d+)?\s*g\s+[a-z]|\bbatch(?:es)?\s+(?:is|are|was|were)\b|"
                         r"\beach\s*(?:[).,;:!?]|$)", text)   # "batches of 50 g cubes", "the batch is one cube", "(each)"


def _said(b: Brief, text: str | None = None) -> list:
    """The person's words so far as typed, the request and every typed reply (and `text`), whole (conversation() keeps the
    last 2,000 characters)."""
    said = [x.get("text") or "" for x in b.turns if x.get("role") == "user"]
    if not said or said[0] != b.request:
        said.insert(0, b.request)
    return [w for w in said + [text or ""] if w.strip()]


def _sizes(t: str) -> tuple:
    """(width mm, mass g) the request gives its object, the largest of each when it gives several ("60 mm and 30 mm
    cylinders", "30 mm, 50 g cylinders and 300 g jars"), or None; never a size that names where things go ("into
    100 mm cups") or a distance, reach or part of the robot (parse() then leaves out a rate or a batch's total beside
    the object's own mass: "50 g cubes at 2 kg per hour"). `t` is normalize_units(text)."""
    widths = [v for v, _s, _e in _width_reads(t)]
    masses = [v for v, _s, _e in _mass_reads(t)]
    return (max(widths) if widths else None), (max(masses) if masses else None)


_FLAT_WORDS = (r"box(?:es)?|cubes?|blocks?|bricks?|cartons?|packages?|parcels?|bars?|phones?|smartphones?|tiles?|cards?|"
               r"wallets?|remotes?|cases?|trays?|crates?|bins?|dominos?|dominoes|dice|cuboids?|rectangular|square|hex|hexagonal|"
               r"cassettes?|erasers?|books?|packs?|flat|slabs?|boards?|panels?|sheets?|envelopes?|sachets?|pouches|pouch")
_BALL_WORDS = r"balls?|spheres?|spherical|fruits?|oranges?|apples?|marbles?|beads?|globes?|pebbles?"
_ROUND_WORDS = (r"cylinders?|cylindrical|tubes?|rods?|dowels?|pipes?|cans?|tins?|bottles?|jars?|cups?|mugs?|vials?|rolls?|"
                r"reels?|spools?|pucks?|discs?|disks?|coins?|pens?|pencils?|candles?|batter(?:y|ies)|round")


def _shape_said(t: str) -> str | None:
    """The shape the request's words give its object: "box" (flat-sided: boxes, bars, phones, tiles, square or hex ones),
    "sphere" (balls, oranges, marbles) or "cylinder" (tubes, cans, bottles, round ones), or None when they don't say;
    a word that names where things go ("into bins") is not the object's."""
    for shape, words in (("box", _FLAT_WORDS), ("sphere", _BALL_WORDS), ("cylinder", _ROUND_WORDS)):
        if any(not _TO_A_THING.search(t[max(0, m.start() - 40):m.start()]) for m in re.finditer(r"\b(?:" + words + r")\b", t)):
            return shape
    return None


# a reply that says the objects' shape and nothing else ("they're round", "the cubes are cylinders", "round ones")
_SHAPE_REPLY = re.compile(r"^\s*(?:(?:sorry|actually|oops|oh|ok|okay|and|also|so|no|well)[,!:]?\s+)*(?:(?:they|these|those|"
                          r"all\s+of\s+them|each\s+(?:one|of\s+them))\s*(?:'re|’re|\s+are|\s+is)|it\s*(?:'s|’s|\s+is)|"
                          r"the\s+(?P<noun>[a-z]+)\s+(?:are|is))?\s*(?:all\s+|actually\s+|really\s+|basically\s+|"
                          r"(?:more\s+)?like\s+|shaped\s+like\s+)?(?:a\s+|an\s+)?(?P<shape>[a-z]+)(?:\s+(?:ones?|shaped|"
                          r"shape))?\s*[.!]?\s*$")


def _says_shape_of_them(t: str, o: dict) -> bool:
    """Whether a later reply (`t`, lower case) says the shape of the objects (`o`) and nothing else: a round shape
    lets the stock SO-101 hold them up to 46 mm, so a shape word anywhere ("my SO-101 can do it", "the caps are round",
    "they go in cups") never gives it."""
    m = _SHAPE_REPLY.match(t)
    if not m or not _shape_said(m.group("shape")):
        return False
    return not m.group("noun") or _singular(m.group("noun")) in _object_words(o)[1]


def parse(text: str) -> dict:
    """Offline extraction: keywords, numbers and units only. Values here are what the user said (source user),
    except the layout, which is inferred from where things go."""
    raw = (text or "")[:2000]
    t = normalize_units(raw.lower())
    out = {"slots": {}, "inferred": {}, "hints": {}, "implicit": [], "oos": out_of_scope_kinds(t), "harm": harmful(raw), "said": {},
           "adjusted": adjusted_kinds(t), "person": {}}
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
    dia, mass = _sizes(t)
    if dia is not None or mass is not None:
        shape = None
        if obj is None:
            obj = {"key": None, "name": "object", "shape": "cylinder", "diameter_mm": 45.0, "length_mm": 60.0,
                   "mass_g": 100.0, "friction": 0.5, "max_force_n": 0.0}
            shape = _shape_said(t)
            if shape:
                obj.update(shape=shape, name={"box": "box", "sphere": "ball"}.get(shape, "object"))
            elif re.search(r"\b(dumbbells?|weights?)\b", t):
                obj.update(name="dumbbell")
            noun = _noun(t)
            if noun:
                obj["name"] = noun
        obj = dict(obj)
        if dia is not None:
            obj["diameter_mm"] = dia
            obj["length_mm"] = max(obj.get("length_mm", dia), dia)
            if _least_width(t):
                obj["least_mm"] = _least_width(t)
        if mass is not None:
            reads = _mass_reads(t)
            drop = _rates_to_drop(t, reads, _object_words(obj), said=[raw])   # "50 g cubes at 2 kg per hour": 50 g
            obj["mass_g"] = max(v for i, (v, _s, _e) in enumerate(reads) if i not in drop) if drop else mass
            if drop:
                obj["rate_g"] = max(reads[i][0] for i in drop)
                if any(not _TIMED.match(t, reads[i][2]) for i in drop):
                    obj["rate_untimed"] = True
        obj["stated"] = [k for k, v in (("diameter_mm", dia), ("mass_g", mass), ("shape", shape)) if v is not None]
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
    # who is next to it. The person signals are read on their own, apart from the task: someone it hands things to,
    # children, someone working beside it, taking from, loading or using it, someone it helps, serves or works for.
    # Any of them keeps a person next to it, and so the emergency stop; "nobody nearby" says no one only when none of
    # them is anywhere in the request (a missed person is a safety failure, an extra emergency stop a small cost):
    # "hand me tools, no one else is nearby", "nobody nearby, the nurse takes the tray from its gripper", "no one is
    # around, it helps my grandma eat her soup"
    pt = normalize_units(people_text(raw))         # names read as "someone", "the fitter" as "the morphagent"
    hands = _rx(HAND_TO_PERSON).search(pt)
    people = out["person"] = person_signals(pt, t)
    if people.get("children"):
        s["people_nearby"] = "children"
    elif people.get("beside"):
        s["people_nearby"] = "yes"  # "people nearby", "people work next to it", "near people", "next to the operator"
    elif people:
        # "help my grandma", "pass my dad his pills", "for me": that person is next to it (and a toy robot's children; a
        # pet feeder's person is a person, not a pet: "hand the dog walker the dog food")
        inf["people_nearby"] = "children" if ARCHETYPES[key].people == "children" or people.get("school") else "yes"
    elif _rx(ANIMAL_GETS).search(pt):
        inf["people_nearby"] = "pets"   # "hand the dog a bone": the animal takes it from the gripper
    elif re.search(NOBODY_NEAR, t):
        s["people_nearby"] = "no"   # "nobody nearby", "no one works next to it"; not "no one near it but me"
    elif re.search(ROBOT_ALONE, t):
        inf["people_nearby"] = "no"   # Morph's reading of "on its own" or "enclosed": an assumption on the card
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
    # existing hardware: read an SO-101 the user has or uses, not a past one, someone else's, a question, a comparison
    # ("like an SO-101") or a request for a new arm ("my SO-101 is broken, build a new arm")
    if re.search(_SO, t):
        so = r"(?:an?\s+|the\s+|my\s+|our\s+)?" + _SO
        none_ctx = re.search(r"\b(?:no|not|don'?t|do not|doesn'?t|haven'?t|have not|without|instead of|rather than|isn'?t|"
                             r"never)\s+(?:[a-z'’-]+\s+){0,2}?(?:an?\s+|the\s+|my\s+|any\s+)?" + _SO, t)
        # it is not an SO-101 the user has to use now, each read from the words next to the SO-101 itself: broken (its own
        # predicate, not "broken crayons"), a new arm asked for (not "don't build a new arm"), someone else's, past or sold,
        # a question whether to use it, or a comparison ("like an SO-101", not "I'd like my SO-101")
        broken = re.search(_SO + r"(?:['’]s(?:\s+[a-z-]+){0,2})?\s+(?:is|are|was|were|has|have|got|went|keeps|has been|"
                           r"have been)\s+(?:[a-z-]+\s+){0,1}?(?:broken|broke|bust|busted|faulty|dead|died|knackered|"
                           r"stopped working|not working|burnt out|burned out|fried)\b|"
                           r"\b(?:broken|busted|faulty|dead|knackered|fried)\s+" + so +
                           r"|" + _SO + r"(?:['’]s)?\s+(?:\w+\s+){0,2}?(?:doesn'?t|won'?t|does not|will not)\s+work\b", t)
        # "my SO-101 broke", "I lent my SO-101 to a friend", "forget the SO-101" (not "I can't forget my SO-101", "it
        # broke but it's fixed now")
        back = re.search(r"\b(?:fixed|repaired|mended|works?\s+(?:again|now|fine)|working\s+(?:again|now)|got\s+it\s+back|"
                         r"have\s+it\s+back|it'?s\s+(?:fine|ok|okay)\s+now)\b", t)
        gone = re.search(_SO + r"\s+(?:just\s+|recently\s+|has\s+)?(?:broke|died|failed|snapped|cracked|stopped\s+working)\b|"
                         r"\b(?:lent|loaned|lend|forget(?:\s+about)?|skip|ditch|scrap)\s+(?:[a-z-]+\s+){0,2}?" + _SO, t)
        if gone and not back and not re.search(r"\b(?:can'?t|cannot|won'?t|will\s+not|wouldn'?t|never|not|don'?t|do\s+not)\s+"
                                               r"(?:\w+\s+)?$", t[max(0, gone.start() - 24):gone.start()]):
            broken = broken or gone
        new_arm = re.search(r"(?<!\bnot )(?<!\bno )(?<!n't )(?<!\bnever )(?<!\bto )(?<!\binto )(?<!\bonto )(?<!\bfrom )"
                            r"(?<!\bwith )(?<!\bby )\b(?:build (?:me )?a new|a new arm|new arm|a different arm|another arm|"
                            r"a replacement arm|design a replacement)\b", t)
        new_arm = new_arm and not re.search(r"\b(?:don'?t|do not|no need to|not|never|without)\s+(?:\w+\s+){0,2}?" +
                                            re.escape(new_arm.group(0)), t)
        others = re.search(r"\b(?:a|my|our)\s+(?:friend|mate|colleague|neighbou?r|brother|sister|dad|mum|mom|cousin|boss|"
                           r"school|lab|uni|teacher|club)\s+(?:has|have|had|owns|owned|got|lent|lends|is\s+lending)\s+(?:an?\s+|the\s+|"
                           r"his\s+|her\s+|their\s+)?" + _SO, t) or \
            re.search(r"\b(?:his|her|their|someone(?:['’]?s)?|somebody['’]?s|the\s+[a-z]+['’]s|a\s+(?:friend|mate|colleague|"
                           r"neighbou?r)['’]?s|my\s+(?:friend|mate|colleague|neighbou?r|brother|sister|dad|mum|mom|cousin|"
                           r"boss|school|lab|uni)['’]s)\s+(?:[a-z-]+\s+)?" + _SO, t)
        past = re.search(r"\b(?:used to|previously|once had|no longer|sold|gave away|given away|lost|returned|replac\w+|"
                         r"upgrad\w+ from|swap\w* out|thinking of buying|want to buy|might buy|should i buy)\s+(?:[a-z-]+\s+){0,2}?" +
                         _SO, t)
        compare = re.search(r"(?<!\bi'd )(?<!\bi )(?<!\bwe'd )(?<!\bwe )(?<!\bwould )(?<!\bwould really )"
                            r"\b(?:like|similar to|such as|based on|akin to|modell?ed on|cheaper than|compared to|compared with|"
                            r"versus|vs\.?|alternative to|inspired by|(?:a\s+)?(?:bigger|smaller|better|cheaper|stronger)\s+"
                            r"version of|version of|instead of|better than|than)\s+(?:an?|the|my|it)?\s*" + _SO +
                            r"|" + _SO + r"[- ]?(?:style|like|ish)\b|\bwhich is better\b[^.;!?]{0,40}" + _SO +
                            r"|\bdo what an?\s+" + _SO, t)
        question = re.search(r"\b(?:should|shall|could|can)\s+(?:i|we)\s+(?:use|try|reuse|repurpose)\s+" + so + r"[^.;!?]*\?", t)
        not_mine = broken or new_arm or others or past or compare or question
        # possession or use: "I have / I've got / I own / I bought / use my / with my / my SO-101 / the SO-101 on my desk"
        owned = (re.search(r"\b(?:i\s+(?:have|own|possess|use|run|operate)|we\s+(?:have|own|use)|i'?ve\s+(?:got|had)|"
                           r"(?:i|we)\s+(?:just\s+)?(?:bought|got|built|assembled)|bought\s+(?:me|us)|"
                           r"got\s+(?:an?|my|a)|use\s+(?:my|an?|the|our|this)|using\s+(?:my|an?|the|our|this)|"
                           r"with\s+(?:my|an?|the|our|this)|on\s+(?:my|our)|(?:my|our)\s+own)\s+(?:[a-z-]+\s+){0,2}?" + _SO, t)
                 or re.search(r"\b(?:my|our)\s+(?:[a-z-]+\s+){0,1}?" + _SO, t)
                 or re.search(r"\bthe\s+" + _SO + r"\s+(?:on|at|in)\s+(?:my|our)\b", t))
        if none_ctx or (new_arm and not none_ctx) or broken:
            s["existing_hardware"] = "none"
        elif owned and not not_mine:
            s["existing_hardware"] = "so101"
        elif not_mine:
            s["existing_hardware"] = "none"   # a question, a comparison, past, or someone else's: not the user's stock arm
        if s.get("existing_hardware") == "none" and out["archetype"][0] == "so101":
            # the SO-101 keyword chose the archetype, but it is not the user's arm: build a new one for the object
            out["archetype"] = (_archetype_for_object(s.get("object")) or "generic_arm", out["archetype"][1])
    # tasks
    if re.search(r"\bsort(?:s|ing|er)?\b", t):
        s["task"] = "sort"
    elif re.search(r"\bhand[- ]?(?:overs?|offs?)\b", t) or hands:
        s["task"] = "hand_over"
    elif any(k == "assembly" for k, _ in out["adjusted"]):
        inf["task"] = "pick_place"    # the parts are moved to where they go, not sorted
    elif ARCHETYPES[key].task == "hand_over" and re.search(_TO_A_PLACE, pt):
        inf["task"] = "pick_place"    # "bring the tools to his workbench": the tools go to a place, not to a person
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
                 r"something (?:to|that)|an? (?:arm|machine) (?:that|to|for)|so[- ]?(?:arm[- ]?)?10[01]|le ?robot|"
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
    if s is None or s.value is None or s.source == "default" or (s.source == "inferred" and s.confidence < 0.6):
        return False
    # an object a model guessed for a request that names none ("I want a robot arm" -> raw egg) is asked, not assumed:
    # what the robot handles is the first thing to ask about an ambiguous request
    return not (name == "object" and s.source == "inferred" and s.confidence < 0.9 and not _object_named(b, s.value) and
                (b.flags.get("says_object") or 0) < 0.5)


# adjectives and units in an object's name that don't name the thing ("raw egg", "small box", "330 ml bottle")
_OBJECT_NAME_STOP = frozenset(("raw", "small", "large", "big", "ripe", "soft", "hard", "full", "empty", "plastic", "glass",
                               "metal", "paper", "rolled", "folded", "wooden", "round", "flat", "the", "and", "with", "for"))


def _object_named(b: Brief, obj) -> bool:
    """Whether the person's own words name a model-guessed object: any word of its name ("pills" for a pill bottle)."""
    if not isinstance(obj, dict):
        return False
    said = " ".join(str(t.get("text") or "") for t in b.turns if t.get("role") == "user").lower() or b.request.lower()
    words = re.findall(r"[a-z]{3,}", f"{obj.get('name') or ''} {obj.get('key') or ''}".lower().replace("_", " "))
    stems = {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in words if w not in _OBJECT_NAME_STOP}
    return any(re.search(r"\b" + re.escape(w), said) for w in stems)


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
    named = b.flags.get("person") or {}   # a person an earlier turn put next to it stays there ("nobody near it" later)
    for name, v in parsed.get("slots", {}).items():
        v2 = coerce(name, v)
        if name == "people_nearby" and v2 == "no" and named:
            continue
        if v2 is not None:
            if name == "object" and isinstance(v, dict) and v.get("stated"):
                v2["stated"] = list(v["stated"])
            if name == "object" and isinstance(v, dict) and cap._num(v.get("least_mm")):
                v2["least_mm"] = float(cap._num(v["least_mm"]))   # the least the person's words say they may be
            if name == "object" and isinstance(v, dict) and v.get("widest_unknown") is True:
                v2["widest_unknown"] = True                         # a size given at a neck, not at their widest
            if name == "object" and isinstance(v, dict) and v.get("shape_unknown") is True:
                v2["shape_unknown"] = True                          # Morph's guess for a job: its shape isn't known
            if name == "object" and isinstance(v, dict) and cap._num(v.get("rate_g")):
                v2["rate_g"] = float(cap._num(v["rate_g"]))         # a rate left out of their mass ("at 2 kg per hour")
                if v.get("rate_untimed") is True:
                    v2["rate_untimed"] = True                       # a batch or a total ("in 2 kg batches")
            set_slot(b, name, v2, source, 1.0, f"{evidence}: {said[name]}" if name in said else evidence)
    for name, v in parsed.get("inferred", {}).items():
        v2 = coerce(name, v)
        if name == "people_nearby" and v2 == "no" and named:
            continue
        if v2 is not None:
            set_slot(b, name, v2, "inferred", 0.7, evidence)
    for k, v in parsed.get("hints", {}).items():
        if k == "electronics.extras+":
            b.hints[k] = ",".join(dict.fromkeys((b.hints.get(k, "") + "," + v).strip(",").split(",")))
        else:
            b.hints[k] = v
    fl = b.flags
    if parsed.get("person"):
        fl["person"] = {**{k: str(w)[:80] for k, w in parsed["person"].items()}, **named}
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
    o = next((object_value(imp["alt"]) for imp in b.flags.get("implicit") or [] if imp.get("alt") in a.objects + a.more),
             None) or object_value(a.objects[0])
    return _left_so101_floor(b, o, a.key)


def _left_so101_floor(b: Brief, o: dict, arch: str) -> dict:
    """The job's own object Morph assumes once the SO-101 is left ("I don't have an SO-101" on a playroom request), never
    narrower or lighter than the general arm's, which leaving the SO-101 gave before, and of no known shape."""
    if not b.flags.get("left_so101") or arch == "so101":
        return o
    g = object_value(ARCHETYPES["generic_arm"].objects[0])
    d = max(float(o["diameter_mm"]), float(g["diameter_mm"]))
    # Morph's guess for the job (its key keeps the job), not the person's object: of no known shape, so the stock
    # SO-101, if it is named again, holds it only where it holds both round and flat-sided objects, and a generated
    # gripper is planned for round ones, the narrowest band it closes on
    return dict(o, diameter_mm=d, mass_g=max(float(o["mass_g"]), float(g["mass_g"])),
                length_mm=max(float(o.get("length_mm") or 0), d), shape="cylinder", stated=[], shape_unknown=True)


def defaults(b: Brief) -> dict:
    """What every slot falls back to for this brief's archetype. An SO-101 brief does the request's own job, so who is
    next to it and where it works come from that job ("use my SO-101 to tidy up the playroom": children nearby)."""
    a = _arch(b)
    own = ARCHETYPES[_arch_without_so101(b)] if a.key == "so101" else a
    o = b.get("object") or _default_object(b)
    task = a.task
    if b.get("task") == "sort" or a.task == "sort":
        task = "sort"
    doing = b.get("task") or task   # what it does: a sorting archetype asked to hand things over gets no camera
    return {"task": task, "object": o, "fragility": "fragile" if o.get("max_force_n", 0) and o["max_force_n"] <= 5 else
            "care" if o.get("max_force_n", 0) else "robust",
            "layout": a.layout, "throughput": a.throughput, "environment": own.environment,
            "people_nearby": "yes" if doing == "hand_over" and own.people in ("no", "pets") else own.people,
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
        opt = [s for s in a.optional if not filled(b, s) and s not in b.asked and   # a hand-over has its receiver nearby
               not (s == "people_nearby" and value(b, "task") == "hand_over")]
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


def _so101_misfit(o: dict | None) -> str | None:
    """Why the stock SO-101 can't take object `o` (above its payload, wider than its jaws hold in Morph's own
    simulation, or narrower than they hold at the least the person's words say it may be: _so101_least), or None."""
    return _so101_misfit_size(o) or _so101_least(o)


def _so101_least(o: dict | None) -> str | None:
    """A smaller width the person's words give that may be the objects' (least_mm: "cylinders between 20 and 40 mm",
    a later "the small ones are 20 mm" or "they're 20 mm", where "they" may be a lid) below the stock SO-101's usable
    minimum (flat-sided 22.5 mm, round 26 mm)."""
    if (o or {}).get("widest_unknown") is True:
        x = _plural(str(o.get("name") or "object")).lower()
        return (f"Your messages give a size at a narrower part of the {x} (a neck, an opening or a lid), so their widest "
                f"part may be wider than the stock SO-101's jaws hold; it needs a Morph-generated arm (say \"the {x} are "
                f"{float(o['diameter_mm']):g} mm wide\" if that is their widest).")
    lo = cap._num((o or {}).get("least_mm"))
    if not lo:
        return None
    box = o.get("shape") == "box" and not (o.get("shape_unknown") and "shape" not in (o.get("stated") or []))
    lim = cap.SO101_MIN_FLAT_MM if box else cap.min_width("cylinder", "fsr402")
    if lo >= lim:
        return None
    kind, x = "flat-sided" if o.get("shape") == "box" else "round", _plural(str(o.get("name") or "object")).lower()
    return (f"In Morph's simulation the stock SO-101's jaws drop {kind} objects narrower than about {lim:g} mm, and your "
            f"messages also give {lo:g} mm, which may be the size of the {x} or some of them, so it needs a Morph-generated arm "
            f"(say \"the {x} are {float(o['diameter_mm']):g} mm\" if {lo:g} mm is something else's size).")


def _so101_misfit_size(o: dict | None) -> str | None:
    if not o:
        return None
    if o["mass_g"] > cap.SO101_PAYLOAD_G:
        return (f"The stock SO-101 lifts about {cap.SO101_PAYLOAD_G} g with a 2x torque margin; {o['mass_g']:g} g needs a "
                "stronger arm.")
    d, name = float(o["diameter_mm"]), o.get("name", "object")
    if (o.get("key") is None and o.get("stated") is not None or o.get("shape_unknown")) and \
            "shape" not in (o.get("stated") or []):
        # the request gave its object's size but not its shape: it may be flat-sided ("40 mm wide phones")
        if d > cap.SO101_MAX_WIDTH_MM["box"]:
            return (f"In Morph's simulation the stock SO-101's jaws hold round objects up to about "
                    f"{cap.SO101_MAX_WIDTH_MM['cylinder']:g} mm across but flat-sided ones only up to about "
                    f"{cap.SO101_MAX_WIDTH_MM['box']:g} mm; the request doesn't say whether a {d:g} mm {name} is round, so "
                    f"it needs a Morph-generated arm (say it is round, as in \"{d:g} mm cylinders\", to keep the SO-101).")
        if o.get("shape_unknown") and d < cap.min_width("cylinder", "fsr402"):
            # Morph's own guess for a job, of no known shape: it may be round, which the jaws drop under 26 mm
            return (f"In Morph's simulation the stock SO-101's jaws drop round objects narrower than about "
                    f"{cap.min_width('cylinder', 'fsr402'):g} mm; the request doesn't say whether a {d:g} mm {name} is "
                    "flat-sided, so it needs a Morph-generated arm.")
        return None
    widest = cap.SO101_MAX_WIDTH_MM.get(o.get("shape"), cap.SO101_MAX_WIDTH_MM["cylinder"])
    if d > widest:
        kind = "flat-sided" if o.get("shape") == "box" else "round"
        return (f"In Morph's simulation the stock SO-101's jaws hold {kind} objects up to about {widest:g} mm across; "
                f"a {d:g} mm {name} slips out of them, so it needs a Morph-generated arm.")
    if o.get("shape") == "box" and d < cap.SO101_MIN_FLAT_MM:
        return (f"In Morph's simulation the stock SO-101's jaws drop flat-sided objects narrower than about "
                f"{cap.SO101_MIN_FLAT_MM:g} mm; a {d:g} mm {name} slips out of them, so it needs a Morph-generated arm.")
    return None


def _so101_below_min(o: dict | None) -> str | None:
    """A stated object below the stock SO-101's own usable minimum (flat-sided under 22.5 mm, round under 26 mm): the
    jaws drop it, so even with the user's SO-101 a Morph-generated arm is built, not the stock arm on a stand-in."""
    if not o or "diameter_mm" not in (o.get("stated") or []):
        return _so101_least(o)   # only when the user stated the width (not a preset's default size)
    d, shape = float(o.get("diameter_mm", 0) or 0), o.get("shape")
    if o.get("shape_unknown") and "shape" not in (o.get("stated") or []):
        shape = "cylinder"   # Morph's guess of no known shape: it may be round
    lo = cap.SO101_MIN_FLAT_MM if shape == "box" else cap.min_width("cylinder", "fsr402")
    if d and d < lo:
        kind = "flat-sided" if shape == "box" else "round"
        return (f"In Morph's simulation the stock SO-101's jaws drop {kind} objects narrower than about {lo:g} mm; a "
                f"{d:g} mm {o.get('name', 'object')} is below that, so it needs a Morph-generated arm.")
    return _so101_least(o)


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
        mine = bool(o and fits(o))
        if mine:   # the person's own object, not the nearest robot's stand-in, whatever robot is offered
            slots["object"] = o
            po = b.get("object")
            if near_obj is not None and isinstance(po, dict) and isinstance(po.get("stated"), list):
                # with what they stated that the fit leaves as it was ("80 mm, 300 g cubes": the 300 g, not the 80 mm)
                slots["object"] = dict(o, stated=[k for k in po["stated"] if po.get(k) == o.get(k)])
        if near_obj and near_obj["mass_g"] > cap.payload_g("smart_bus", 4):
            slots["budget"] = 1500.0
        src = slots.get("object") or ARCHETYPES[arch].objects[0]
        obj = sanitize_object(src)
        if isinstance(src, dict) and obj is not None:   # what the copy drops, for the SO-101's checks below
            obj.update({k: src[k] for k in ("shape_unknown", "widest_unknown", "least_mm", "stated") if src.get(k) is not None})
        if not mine and b.flags.get("left_so101") and arch != "so101":
            obj = slots["object"] = _left_so101_floor(b, obj, arch)
        so101_here = b.get("existing_hardware") == "so101" or arch == "so101"
        # a stated object below the SO-101's own minimum gets a generated arm, not the stock arm on a bigger stand-in
        below = _so101_below_min(b.get("object")) if so101_here else None
        misfit = below or (_so101_misfit(obj) if so101_here else None)
        if misfit:   # the nearest robot gets the same SO-101 checks as a request inside scope
            notes.append(misfit)
            slots.update(existing_hardware="none", object=obj)
            if arch == "so101":   # the person's object's job, else the request's own (a playroom keeps its children)
                said = b.slots.get("object")
                arch = (_archetype_for_object(obj) if said is not None and said.source in ("user", "video", "photo") else
                        None) or _arch_without_so101(b)
        dims = f"{obj['diameter_mm']:g} mm, {obj['mass_g']:g} g"
        summary = (f"A desk arm that {_verb(ARCHETYPES[arch])} {_object_label(obj, short=True).lower()} ({dims}) on the table plane"
                   + (", with Dynamixel servos and 4 joints (about $1,500 in parts)" if slots.get("budget") else "") + ".")
        return {"status": "out_of_scope", "notes": notes, "nearest": {"archetype": arch, "slots": slots, "summary": summary}}
    ov = b.get("object")
    misfit = _so101_misfit(ov) if value(b, "existing_hardware") == "so101" else None   # "SO-101 to move jars" too
    if misfit:
        return {"status": "out_of_scope", "notes": [misfit],   # the SO-101's job keeps its emergency stop on a new arm
                "nearest": {"archetype": _arch_without_so101(b) if a.key == "so101" else _job_kept(b, a.key, ov),
                            "slots": {"existing_hardware": "none"},
                            "summary": f"A new Morph-generated desk arm for the {ov.get('name', 'object')}."}}
    why = {k: w for k, _p, w in ADJUSTED}
    notes = [why[k] for k, _ in b.flags.get("adjusted") or [] if k in why]
    lo, slot = cap._num((ov or {}).get("least_mm")), b.slots.get("object")
    lim = cap.min_width((ov or {}).get("shape", "cylinder"), "fsr402")
    if lo and slot is not None and slot.source in ("user", "video", "photo") and lo < lim:
        x = _plural(str(ov.get("name") or "object")).lower()
        notes.append(f"Your messages also give {lo:g} mm, which may be the size of the {x} or some of them: narrower than "
                     f"the {lim:g} mm this gripper closes on, so it is built for {float(ov['diameter_mm']):g} mm and those "
                     "would slip.")
    notes += [imp["note"] for imp in b.flags.get("implicit") or []]
    if a.note:
        notes.append(a.note)
    budget = b.slots.get("budget")
    if budget and budget.source == "user" and budget.value < MIN_COST_USD:
        notes.append(f"About ${MIN_COST_USD} in parts is the lowest-cost arm Morph generates (a printed smart-bus arm), above "
                     f"${budget.value:,.0f}.")
    notes += list(b.flags.get("notes") or [])
    return {"status": "adjusted" if notes else "ok", "notes": list(dict.fromkeys(notes)), "nearest": None}


def _job_kept(b: Brief, key: str, o: dict | None) -> str:
    """The job a new arm keeps when the SO-101 a job's robot kept (the nearest robot's: cooking, driving around) no
    longer holds the object: that job, or the object's when only it gives an emergency stop (_gives_estop)."""
    own = _archetype_for_object(o)
    for k in (key, own):
        if k and k != "generic_arm" and _gives_estop(b, k):
            return k
    return key if key != "generic_arm" else (own or "generic_arm")


def _verb(a: Archetype) -> str:
    return {"sort": "sorts", "hand_over": "hands you", "transfer": "moves", "dispense": "carries"}.get(a.task, "picks up and places")


def apply_nearest(b: Brief, nearest: dict):
    """Accept the nearest robot: its archetype and slot values, recorded as a scope assumption."""
    was_so101 = value(b, "existing_hardware") == "so101" or _arch(b).key == "so101"
    before = _archetype_for_object(b.get("object")) or "generic_arm"
    set_archetype(b, nearest.get("archetype", "generic_arm"), 1.0, "rules")
    said = b.get("object") or {}
    least = min([float(said[k]) for k in ("diameter_mm", "least_mm") if cap._num(said.get(k)) and
                 (k == "least_mm" or k in (said.get("stated") or []))], default=None)
    for k, v in (nearest.get("slots") or {}).items():
        v2 = coerce(k, v)
        if k == "object" and v2 is not None and least is not None and least < float(v2["diameter_mm"]):
            v2["least_mm"] = least   # the person's 15 mm beads: a later "keep my SO-101" never holds the 30 mm stand-in
        if k == "object" and v2 is not None and said.get("widest_unknown"):
            v2["widest_unknown"] = True   # "45 mm necks": their widest part is still not given
        if k == "object" and v2 is not None and isinstance(v, dict) and isinstance(v.get("stated"), list):
            v2["stated"] = list(v["stated"])   # the person's own object: "80 mm, 300 g cubes", then "60 g per hour"
        if k == "object" and v2 is not None and isinstance(v, dict) and v.get("shape_unknown") is True:
            v2["shape_unknown"] = True    # Morph's guess for the job once the SO-101 is left: of no known shape
        if k == "object" and v2 is not None and cap._num(said.get("rate_g")):
            v2["rate_g"] = float(said["rate_g"])   # a rate left out of their mass comes back with a later "some are heavier"
            if said.get("rate_untimed"):
                v2["rate_untimed"] = True
        if v2 is not None:
            b.slots[k] = Slot(v2, "inferred", 1.0, "nearest robot")
    if was_so101 and b.get("existing_hardware") == "none":
        b.flags["left_so101"] = before
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

# ---- a later message's sizes: what each one names. A size for the objects themselves replaces theirs ("sorry, 60 mm",
# "the cylinders are 60 mm", "eggs, 5 cm"); any other ("the lids are 35 mm across", "the small ones are 20 mm", "they're
# 60 mm", where "they" may be a lid, a gap, a peg or the fingers) can only widen or weigh the objects more, and a smaller
# width it gives is kept as the least they may be (least_mm), which the stock SO-101's jaws must still hold.
_FIX_WORDS = (r"(?:sorry|actually|oops|correction|my bad|my mistake|i meant|i mean|make (?:that|it)|more like|no|nope|wait|"
              r"um|erm|er|oh|ah|ok|okay|right|typo|(?:it\s+)?should\s+(?:say|be|read))")
# a later message that corrects the only mass so far ("sorry, 300 g every minute"): the mass may be the objects' own
_CORRECTS = re.compile(r"\s*(?:sorry|actually|oops|whoops|correction|my bad|my mistake|i meant|i mean|make (?:that|it)|"
                       r"more like|no|nope|wait|scratch that|rather)\b")
# a reply that is only a width, after a word that corrects one ("actually 60 mm", "60 mm actually")
_FIX_MM = re.compile(r"(?:" + _FIX_WORDS + r"[,!:]?\s+)?(?:about |around |roughly |~|approx\.? |approximately |circa )?"
                     r"(\d+(?:\.\d+)?)\s*mm"
                     r"(?:\s+(?:wide|across|in diameter))?(?:\s+(?:actually|instead|then|sorry))?")
_SIZE_END = (r"(?=\s*(?:$|[.,;:!?)]|(?:each|apiece|actually|then|instead|now|not|and|but|so|sorry|thanks|please|big|in\s+size|"
             r"or\s+so)\b))")
# "they're 60 mm", "it's 60 mm", "the cylinders are 60 mm", "the small ones measure 20 mm": the size of whatever the
# words before it name (_np_before)
_IS_MM = re.compile(r"(?:\s*['’](?:re|s)|\s+(?:are|is|were|was|measure[sd]?|come\s+in\s+at|comes\s+in\s+at))\s+"
                    r"(?:(?:actually|really|more\s+like|nearer|closer\s+to|about|around|roughly|approximately|~|only|just|"
                    r"all|each|now|exactly|precisely)\s+)*(\d+(?:\.\d+)?)\s*mm\b" + _SIZE_END)
_PRONOUNS = frozenset(("they", "these", "those", "it", "this", "them", "each", "both", "all", "everything"))
_DETS = frozenset(("the", "these", "those", "all", "my", "our", "their", "your", "each", "every", "both", "its", "his", "her",
                   "a", "an"))
# words that pick out some of the objects, not all of them ("the small ones", "some cylinders", "the red cups")
_SOME_WORDS = frozenset(("some", "most", "few", "several", "many", "half", "rest", "other", "others", "one", "ones", "two",
                         "three", "small", "smaller", "smallest", "little", "tiny", "big", "bigger", "biggest", "large",
                         "larger", "largest", "wide", "wider", "widest", "narrow", "narrower", "narrowest", "thin",
                         "thinner", "thinnest", "thick", "thicker", "thickest", "short", "shorter", "shortest", "tall",
                         "taller", "tallest", "long", "longer", "longest", "heavy", "heavier", "heaviest", "light", "lighter",
                         "lightest", "mini", "jumbo", "giant", "medium", "regular", "standard", "first", "last", "odd", "new",
                         "old", "majority", "certain"))
_GENERIC_THINGS = frozenset(("object", "objects", "item", "items", "thing", "things", "piece", "pieces"))
# words that end a noun phrase read backwards from a size ("move the cylinders", "i think the lids")
_NP_STOP = frozenset((
    "to", "into", "onto", "from", "with", "for", "of", "in", "on", "at", "by", "and", "or", "but", "so", "then", "that",
    "which", "who", "if", "when", "while", "as", "than", "is", "are", "was", "were", "be", "been", "am", "i", "we", "you",
    "he", "she", "me", "us", "him", "not", "no", "never", "also", "just", "only", "really", "actually", "sorry", "think",
    "thought", "guess", "mean", "meant", "said", "say", "know", "believe", "reckon", "means", "have", "has", "had", "got",
    "get", "move", "moves", "moving", "pick", "picks", "picking", "sort", "sorts", "carry", "carries", "handle", "handles",
    "use", "uses", "lift", "lifts", "hold", "holds", "grab", "grabs", "place", "places", "put", "puts", "stack", "stacks",
    "load", "loads", "need", "needs", "want", "wants", "like", "measure", "measures", "measuring", "weigh", "weighs",
    "weighing", "weighed", "about", "around", "roughly", "approximately", "up", "down", "out", "off", "over", "please",
    "thanks", "ok", "okay", "yes", "oh", "well", "wait", "um", "erm", "er", "here", "there", "now", "too", "very", "do",
    "does", "did", "can", "could", "will", "would", "should", "must", "may", "might", "every", "per", "each", "mm", "g"))
_TIES = ("are", "is", "were", "was", "measure", "measures", "measured", "measuring", "weigh", "weighs", "weighed", "weighing",
         "of", "at", "being")
_UNIT_WORDS = ("wide", "across", "thick", "diameter", "dia", "dia.", "heavy", "in")


def _object_words(o: dict) -> tuple:
    """(head nouns, every word) of what the object is called, singular: a glass jar is a "jar", a cup of pet food a
    "cup", rolled socks "socks"; a described object with no name of its own is called by its shape too."""
    k = o.get("key")
    phrases = [str(o.get("name") or "")]
    if k in PRESET_INFO:
        phrases.append(PRESET_INFO[k][1])
    elif k in CATALOG:
        phrases.append(CATALOG[k]["label"])
    elif str(o.get("name") or "object") in ("object", "box", "ball"):
        phrases += {"cylinder": ["cylinder", "tube", "rod"], "box": ["box", "cube", "block"],
                    "sphere": ["ball", "sphere"]}.get(o.get("shape"), [])
    heads, words = set(), set()
    for ph in phrases:
        ws = re.findall(r"[a-z]+", ph.lower())
        if ws:
            words |= {_singular(w) for w in ws}
            heads.add(_singular(ws[ws.index("of", 1) - 1] if "of" in ws[1:] else ws[-1]))
    return heads, words


def _names_what(np: list, heads: set, words: set) -> str:
    """What a noun phrase before or after a size names: "obj" (the objects: "the cylinders", "all the glass jars"),
    "pron" ("they", "each of them"), "some" (some of them: "the small ones", "the red cups", "a few of them") or "other"
    (a lid, the candles in candle jars, the pills in pill bottles, water in a cup, a bin, a gap, the fingers)."""
    if len(np) >= 3 and np[-2] == "of" and np[-1] in ("them", "these", "those"):
        return "pron" if np[-3] in ("each", "all", "both") else "some"
    head, mods = np[-1], [w for w in np[:-1] if w not in _DETS]
    if head in _PRONOUNS and not mods:
        return "pron"
    if head in ("ones", "one", "rest", "others") or head in _QUANTS and not mods or \
            any(w in _SOME_WORDS and _singular(w) not in words for w in mods):
        return "some"
    if _singular(head) in heads or head in _GENERIC_THINGS:
        return "obj" if all(_singular(w) in words for w in mods) else "some"
    return "other"


def _np_before(t: str, s: int) -> tuple:
    """(the noun phrase a size at `s` is said of, the words just before it): "the lids are 35 mm" (["the", "lids"], []),
    "the lids of the jars are 30 mm" (["the", "jars"], [..., "lids", "of"]), "cylinders weighing 50 g", "each cup is
    40 g", "they're 60 mm", "each of them is 60 mm"; ([], []) when the words before it tie it to nothing."""
    clause = re.split(r"[.,;:!?()\[\]]|\s-\s|\b(?:and|but|while|whereas|plus|then)\b", t[:s].replace("’", "'"))[-1]
    clause = re.sub(r"'s\b", " is", re.sub(r"'re\b", " are", clause))
    toks = re.findall(r"[a-z0-9~][a-z0-9'.-]*", clause)
    while toks and toks[-1] in ("actually", "really", "more", "like", "nearer", "closer", "to", "about", "around", "roughly",
                                "approximately", "~", "only", "just", "now", "all", "each", "exactly", "precisely"):
        toks.pop()
    if not toks or toks[-1] not in _TIES:
        return [], []
    toks.pop()
    while len(toks) > 1 and toks[-1] in ("all", "both", "each") and toks[-2] in _PRONOUNS:   # "they all measure"
        toks.pop()
    if toks and toks[-1] in _PRONOUNS and toks[-1] in _DETS | {"they", "it", "this"} and (len(toks) == 1 or toks[-2] not in
                                                                                         _DETS):
        return toks[-1:], toks[-4:-1]   # "these are 15 mm", "both are 15 mm"
    if toks[-2:] in (["come", "in"], ["comes", "in"]):
        del toks[-2:]
    if len(toks) >= 3 and toks[-2] == "of" and toks[-1] in ("them", "these", "those"):
        return toks[-3:], toks[:-3][-3:]
    np = []
    while toks and len(np) < 5 and not toks[-1][0].isdigit() and (toks[-1] not in _NP_STOP or toks[-1] in _DETS or (
            not np and toks[-1] in ("can", "cans", "will", "may", "must", "do", "well", "place", "places", "hold", "holds",
                                    "load", "loads", "stack", "stacks", "handle", "handles", "top", "tops"))):
        np.insert(0, toks.pop())
        if np[0] in _DETS - {"each", "every", "both", "all"}:
            break
    return (np, toks[-3:]) if np and np[-1] not in _DETS else ([], [])


def _looks_np(np: list) -> bool:
    """Whether words on their own are a noun phrase ("the caps", "eggs"), not a reply word ("sorry", "people nearby")."""
    return 0 < len(np) <= 4 and not any(w[0].isdigit() for w in np) and (
        np[0] in _DETS or (len(np) <= 2 and np[-1].endswith("s") and not any(w in _NP_STOP for w in np) and np[-1] not in (
            "yes", "thanks", "this", "its", "his", "is", "was", "has", "does", "always", "plus", "less", "unless", "perhaps",
            "sometimes", "nobody's")))


def _topic(t: str, s: int) -> list:
    """The noun phrase a pronoun before the size at `s` points back to in the same sentence ("the lids? they're 20 mm",
    "the table: it's 600 mm"), or []."""
    m = re.match(r"\s*(?:(?:and|oh|oh and|what about|as for)\s+)?([a-z][a-z' -]*?)\s*[?:,\-–]+\s*(?:they|these|those|it|this|"
                 r"each|both|all)\b", re.split(r"[.;!]", t[:s])[-1])
    np = m.group(1).split() if m else []
    return np if _looks_np(np) else []


def _np_after(t: str, e: int) -> list:
    """The noun a size ending at `e` comes before ("35 mm caps", "60 mm glass jars", "90 g of water"), or []."""
    rest = t[e:]
    m = re.match(r"\s+of\s+([a-z][a-z'-]*)", rest)
    if m:
        return ["of", m.group(1)]
    toks = re.findall(r"[a-z][a-z'-]*|[^\sa-z]", rest[:80])
    while toks and toks[0] in _UNIT_WORDS:
        toks.pop(0)
    np = []
    for w in toks[:3]:
        if not re.fullmatch(r"[a-z][a-z'-]*", w) or w in _NP_STOP or w in _DETS or w in _PRONOUNS or w in _UNIT_WORDS or \
                w in ("full", "empty", "total", "net", "gross", "max", "maximum", "min", "minimum", "tops", "apiece"):
            break
        np.append(w)
    return np


_PREPS = frozenset(("of", "on", "in", "inside", "for", "between", "through", "around", "at", "from", "under", "over",
                    "with", "without", "to", "into", "onto", "near", "by", "behind", "beside", "across", "along", "off", "per",
                    "than", "like", "beneath", "below", "above", "within", "among"))
_QUANTS = frozenset(("some", "one", "two", "three", "four", "five", "few", "many", "most", "half", "none", "several",
                     "couple", "part", "majority", "minority", "rest", "any", "not", "nearly", "almost"))
# parts a size can be of: a part's own size, read for the first time, only widens the objects ("the can lids are 60 mm")
_PARTS = frozenset(("lid", "cap", "top", "neck", "rim", "handle", "label", "sticker", "stopper", "cork", "spout", "lip",
                    "collar", "stem", "base", "bottom", "head", "foot", "shoulder", "knob", "tab", "seal", "wrapper"))
# a size that is not plainly all of the objects' own: a question or a supposition ("what if the balls are 40 mm?",
# "suppose they're 30 mm"), a lower bound ("40 mm and up", "at least 40 mm", "40 mm, some are bigger"), or the size
# at one part of them ("30 mm at the top", "30 mm across the lid")
_SUPPOSED = re.compile(r"^\s*(?:(?:and|but|so|oh|ok|okay|well|hmm)\s*,?\s+)?(?:can|could|would|will|does|do|did|is|are|should|"
                       r"might|may|what|how|which|why|whether)\b|\b(?:if|suppose|supposing|assuming|assume|imagine|in\s+case|"
                       r"unless|hypothetically|say|maybe|perhaps|possibly)\b")
_AT_LEAST_BEFORE = re.compile(r"\b(?:at\s+least|over|more\s+than|above|from|starting\s+at|upwards\s+of|no\s+less\s+than|"
                              r"minimum(?:\s+of)?|min\.?|bigger\s+than|larger\s+than|wider\s+than|heavier\s+than)\s+"
                              r"(?:about\s+|around\s+|roughly\s+)?$")
_AT_LEAST_AFTER = re.compile(r"\s*(?:wide|across|in\s+diameter|thick)?\s*,?\s*(?:and\s+(?:up|upwards?|over|above|bigger|larger|"
                             r"wider|heavier|more)|or\s+(?:more|bigger|larger|wider|heavier|above|over)|plus\b|\+|at\s+(?:the\s+)?"
                             r"(?:least|minimum|smallest|narrowest|thinnest|lightest)|minimum\b|min\b|upwards?\b|and\s+a\s+"
                             r"(?:half|quarter|bit))")
# parts narrower than the objects' widest ("40 mm at the neck", "45 mm necks", "the opening of the cans is 30 mm"): a
# size of one as wide as the objects says their widest is not given (widest_unknown)
_NARROW_PARTS = frozenset(("neck", "mouth", "opening", "top", "cap", "lid", "rim", "spout", "tip", "end", "lip", "collar",
                           "handle", "hole", "stem", "stopper", "cork", "knob", "nozzle", "waist", "shoulder", "narrowest",
                           "thinnest", "small"))
# what a part, or what is in or on the objects, is said of: "the lids of the jars", "the caps on the jars", "the pills in
# the cans" (not "the gap between the jars" or "the tray for the jars")
_BELONGS = frozenset(("of", "on", "in", "inside", "around", "at", "atop", "within"))
_EMPTY_AFTER = re.compile(r"\s*(?:each\s+)?,?\s*(?:when\s+|if\s+)?(?:empty|unfilled|dry|net|tare|without\s+(?:the\s+|its\s+|their\s+)?"
                          r"(?:lids?|caps?|contents?|water|liquid|filling|food))\b")
_AT_A_PART = re.compile(r"\s*(?:wide|across|in\s+diameter|thick)?\s*,?\s*(?:at|across|around|near|by|on|in|for)\s+(?:the|its|their|"
                        r"each|every)\s+(?:very\s+)?(?:top|lid|neck|rim|base|bottom|mouth|opening|cap|handle|ends?|tips?|middle|"
                        r"centre|center|narrowest|thinnest|label|stem|spout|head|foot|feet|edges?|lip|collar|shoulder|waist|"
                        r"smallest|small\s+end)\b")


def _not_plain(t: str, s: int, e: int) -> bool:
    """Whether a size spanning t[s:e] may not be the objects' own (_SUPPOSED, _AT_LEAST_*, _AT_A_PART)."""
    start = max([m.end() for m in re.finditer(r"[;!?]|\.(?!\d)", t[:s])], default=0)   # never a decimal point
    stop = next((e + m.start() for m in re.finditer(r"[;!?]|\.(?!\d)", t[e:])), len(t))
    return bool(stop < len(t) and t[stop] == "?" or _SUPPOSED.search(t[start:s]) or
                _AT_LEAST_BEFORE.search(t[max(0, s - 30):s]) or _AT_LEAST_AFTER.match(t, e) or _AT_A_PART.match(t, e) or
                re.match(r"\s*g\b", t[e - 1:]) and _EMPTY_AFTER.match(t, e) or
                re.search(r"\b(?:bigger|larger|wider|thicker|heavier)\b", t[e:stop]) or
                re.search(r"\b(?:plus|also|another|other|others|extra|more|too|as\s+well)\b", t[start:stop]) or
                re.search(r"\b(?:in\s+most\s+cases|mostly|usually|typically|generally|normally|on\s+average|for\s+the\s+most\s+"
                          r"part|most\s+of\s+them|the\s+majority)\b", t[start:stop]))


def _size_reads(b: Brief, t: str, base: dict | None, raw: str | None = None) -> list:
    """Each width and mass a later message gives, as {dim: "w"|"m", v, cls}. Only a phrasing the release read can
    replace the objects' size ("obj": "20 mm", "the cylinders are 20 mm wide", "they weigh 60 g"), and then only when it
    is plainly all of the objects' own; any other "maybe" one may be something else's, some of them' or a lower bound
    ("they're 20 mm", "the cylinders are 20 mm", "some of the balls are 40 mm", "the lids of the jars are 30 mm wide",
    "what if they're 40 mm?"), so it only widens them or weighs them more, and a smaller width is the least they may be.
    "wider" is a part's size read for the first time ("the can lids are 60 mm"): it only widens them. "rate" is a mass
    per hour or a batch's beside the objects' own. A size for something else that was never read is left out ("the
    lids are 35 mm", "the gap between the jars is 30 mm"). `base` is the object the message is about (None: no object
    yet, so a size the release read is the objects'); `raw` is the message as typed."""
    heads, words = _object_words(base) if base else (set(), set())

    def cls_of(np, lead=(), new=False, at=0, end=0):
        """(class, the narrow part the size is of or None) for a size spanning t[at:end] said of `np`."""
        what = _names_what(np, heads, words) if np else None
        dim = bool(np) and np[-1] in _DIM_WORDS
        if dim:   # "the diameter is 60 mm", "they come with a width of 45 mm": theirs, if nothing else's ("the gripper has a
            what = "pron" if _dim_theirs(t, at, words) else "other"   # width of 20 mm", "a tray with a width of 45 mm")
        part = _singular(np[-1]) if np and what == "other" and _singular(np[-1]) in _NARROW_PARTS else None
        loc = _AT_A_PART.match(t, end)
        if loc:
            part = part or next((w for w in re.findall(r"[a-z]+", loc.group(0)) if _singular(w) in _NARROW_PARTS), None)
        if what == "pron" and _topic(t, at):   # "the lids? they're 20 mm": the lids
            what = _names_what(_topic(t, at), heads, words)
            what = "obj" if what == "pron" else what
        belongs = False
        if np and lead and lead[-1] in _PREPS and not dim:   # "the lids of the jars", "the gap between the jars"
            q = lead[-2] if len(lead) > 1 else ""
            belongs = lead[-1] in _BELONGS and what in ("obj", "pron") and q not in _QUANTS
            if belongs and _singular(q) in _NARROW_PARTS:
                part = _singular(q)
            what = what if lead[-1] == "of" and (q in ("each", "all", "both", "every") or q in _DIM_WORDS) else \
                "some" if lead[-1] == "of" and q in _QUANTS else "other"
        elif np and lead and (lead[-1] in _QUANTS or lead[-1] in ("all", "every") and len(lead) > 1 and lead[-2] in _QUANTS):
            what = "some"                         # "half the bottles", "not all the balls"
        if what in (None, "obj", "pron") and _not_plain(t, at, end):
            what = "some"
        if re.search(r"(?:\bnot|n't|\bnever)\s+(?:(?:even|quite|exactly|about|around|over|more\s+than)\s+)?$", t[max(0, at - 24):at]):
            return ("maybe" if not new and base is not None else None), None   # "they're not 60 mm": never more than before
        if not new:
            return ("obj" if base is None or what in (None, "obj", "pron") else "maybe"), part
        if what == "other":   # a part's, or what is in or on them: only widens them ("the base of each block is 45 mm")
            return ("wider" if base is not None and (belongs or np and _singular(np[-1]) in _PARTS) else None), part
        if base is None:   # nothing described yet: "they're 40 mm" may be anything's, as before
            return None, part
        return "maybe", part

    reads, spans = [], []

    def unsure(cp, np):
        """A width that would replace theirs said without their noun, after a smaller one some of them or a range's low end
        may be (least_mm: "some of them are 20 mm wide", then "sorry, 40 mm"): it may correct that one, so it only widens."""
        if cp[0] == "obj" and base is not None and base.get("least_mm") and not (np and _names_what(np, heads, words) == "obj"):
            return "maybe", cp[1]
        return cp

    def add(dim, v, at, cls_part):
        cls, part = cls_part if isinstance(cls_part, tuple) else (cls_part, None)
        if cls is not None and not any(s <= at < e for s, e in spans):
            reads.append({"dim": dim, "v": float(v), "cls": cls, "part": part})
            spans.append((at, at + 1))

    def low_end(at, cls_part):
        cls = cls_part[0] if isinstance(cls_part, tuple) else cls_part   # "between 20 and 40 mm wide": the 20 mm too, as the least they may be
        low = _RANGE_LOW.search(t[max(0, at - 24):at])
        if low and cls not in (None, "wider") and 0 < float(low.group(1)) < float(re.match(r"[\d.]+", t[at:]).group(0)):
            add("w", low.group(1), max(0, at - 24) + low.start(1), "maybe" if base is not None else "obj")

    for v, s, e in _width_reads(t):
        np, lead = ([], []) if _np_after(t, e) else _np_before(t, s)
        cls = unsure(cls_of(_np_after(t, e) or np, lead, at=s, end=e), _np_after(t, e) or np)
        if cls[0] == "obj" and not (np or _np_after(t, e)) and _mass_of(t, s, e) and \
                _names_what(_mass_of(t, s, e), heads, words) not in ("obj", "pron"):
            cls = "maybe", cls[1]   # "the caps, 20 mm across", "lids: 20 mm wide": a part's, never narrower
        beside = _part_beside(t, s, e, heads, words) if cls[0] == "obj" and base is not None and not (np or _np_after(t, e)) \
            else []
        nxt = re.match(r"\s*(?:(?:wide|across|round|square|flat|plastic|metal|wooden|glass|steel)\s+)*([a-z]+)", t[e:])
        if not beside and cls[0] == "obj" and base is not None and nxt and \
                _singular(nxt.group(1)) in (_PARTS | _NARROW_PARTS) - (heads | words):
            beside = [nxt.group(1)]   # "30 mm handles", "40 mm tops": a part's ("handles" is a verb too, so no noun after)
        if beside and _names_what(beside, heads, words) not in ("obj", "pron"):
            cls = "maybe", cls[1] or (_singular(beside[-1]) if _singular(beside[-1]) in _NARROW_PARTS else None)
            # "lids 20 mm wide", "cap diameter 20 mm", "20 mm wide for the caps": a part's, never narrower
        add("w", v, s, cls)
        low_end(s, cls)
    # "the jars are 40 mm at the neck" (a phrasing never read as a width): a part as wide as the objects
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*mm\b", t):
        loc = _AT_A_PART.match(t, m.end())
        part = loc and next((w for w in re.findall(r"[a-z]+", loc.group(0)) if _singular(w) in _NARROW_PARTS), None)
        if part and base is not None and not any(s <= m.start(1) < e for s, e in spans):
            add("w", m.group(1), m.start(1), ("part", part))
    # a reply part that is only a width ("6 cm", "actually 60 mm"): the objects', unless the part before it names
    # something else ("the caps, 35 mm", "the lids of the jars, 35 mm")
    pos, prev = 0, ""
    for part in re.split(r"([,;]|\band\b|\bplus\b)", t):
        seg = part.strip(" .!")
        m = _FIX_MM.fullmatch(seg) if seg else None
        if m:
            at = t.index(seg, pos) + m.start(1)
            np, lead = _np_before(prev + " is ", len(prev) + 4) if re.match(r"(?:the|these|those|its|their|my|our|each|all|"
                                                                             r"some|half|one|a)\b", prev) else ([], [])
            if not np and _looks_np(prev.split()):
                np = prev.split()
            cls = unsure(cls_of(np, lead, new=not _BARE_MM.fullmatch(seg), at=at,
                                end=at + len(re.match(r"[\d.]+\s*mm", t[at:]).group(0))), np)
            add("w", m.group(1), at, cls)
            low_end(at, cls)
        if seg and not re.fullmatch(r"[,;]|and|plus", seg):
            prev = seg
        pos += len(part)
    for m in _IS_MM.finditer(t):
        np, lead = _np_before(t, m.start(1))
        if np and not (_NOT_A_SIZE.search(t[max(0, m.start(1) - 24):m.end()]) or
                       _NOT_A_SIZE_JUST_BEFORE.search(t[max(0, m.start(1) - 16):m.start(1)])):
            add("w", m.group(1), m.start(1), cls_of(np, lead, new=True, at=m.start(1), end=m.end(1)))
            spans.append((m.start(1), m.end(1)))   # said of what the words before it name: never read again below
    # a width said beside their own noun, "each" or a width word with no word tying them, a way never read before
    # ("the cylinders 60 mm", "60 mm for the cylinders", "60 mm (all of them)", "60 mm each", "width: 60 mm"): only
    # wider, or the least they may be; never a distance ("move the cylinders 60 mm to the left") or another thing's
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*mm\b", t):
        s0, e0 = m.start(1), m.end()
        if base is None or any(s <= s0 < e for s, e in spans) or _NOT_A_SIZE.search(t[max(0, s0 - 24):e0]) or \
                _NOT_A_SIZE_JUST_BEFORE.search(t[max(0, s0 - 16):s0]) or _TO_A_THING.search(t[max(0, s0 - 40):s0]) or \
                _MOVED_BY.search(t[_clause_start(t, s0):s0]) or _DISTANCE_AFTER.match(t, e0) or \
                re.search(r"\b(?:from|to|by|at|every|per|than|over|under|within|up|down)\s+(?:about\s+|around\s+)?$", t[:s0]):
            continue
        np = _np_after(t, e0)
        beside = _part_beside(t, s0, e0, heads, words)
        dim = re.search(r"\b(?:" + "|".join(_DIM_WORDS) + r")\s*(?:is|was|=|:|-|of)?\s*(?:about\s+|around\s+|approx\.?\s+|"
                        r"roughly\s+|~\s*)?$", t[max(0, s0 - 30):s0])
        other = bool(beside) and _names_what(beside, heads, words) not in ("obj", "pron")   # "the jaws 120 mm each"
        mine = np and _names_what(np, heads, words) in ("obj", "pron") or not np and not other and (
            beside or re.match(r"\s*(?:wide|across|in\s+diameter)?\s*(?:each|apiece)\b(?!\s+(?:way|side|time|step|direction|"
                               r"move|end|axis))", t[e0:]) or
            dim and _dim_theirs(t, s0, words))
        if mine:
            cls = cls_of([], (), new=True, at=s0, end=e0)
            add("w", m.group(1), s0, cls)
            low_end(s0, cls)   # "between 20 and 40 mm each": the 20 mm too, as the least they may be
    masses = _mass_reads(t)
    own_before = False
    if base is not None and "mass_g" in (base.get("stated") or []):
        x = normalize_units(conversation(b).lower())
        own_before = any(abs(v - float(base["mass_g"])) < 1e-6 and not _rate_or_batch(x, s, e) and
                         _names_objects(x, s, e, heads, words) for v, s, e in _mass_reads(x))
    drop = _rates_to_drop(t, masses, (heads, words), own_elsewhere=own_before,
                          context=normalize_units(conversation(b, t).lower()), said=_said(b, t if raw is None else raw))
    if drop and own_before and max((v for i, (v, _s, _e) in enumerate(masses) if i not in drop), default=1e9) < \
            float(base["mass_g"]):
        drop = set()   # a lighter mass beside the rate may be a part's: the rate stays as read
    for i, (v, s, e) in enumerate(masses):
        np, lead = ([], []) if _np_after(t, e) else _np_before(t, s)
        cls = "rate" if i in drop else cls_of(_np_after(t, e) or np, lead, at=s, end=e)
        if cls[0] == "obj" and not (np or _np_after(t, e)) and _mass_of(t, s, e) and \
                _names_what(_mass_of(t, s, e), heads, words) not in ("obj", "pron"):
            cls = "maybe"   # "the lids: 20 g", "lids, 20 g", "20 g each for the lids": a part's, never lighter
        beside = _part_beside(t, s, e, heads, words) if cls[0] == "obj" and base is not None and not (np or _np_after(t, e)) \
            else []
        if beside and _names_what(beside, heads, words) not in ("obj", "pron"):
            cls = "maybe"   # "lids 20 g", "cap weight 20 g": a part's, never lighter
        add("m", v, s, cls)
        if cls == "rate" and reads and reads[-1]["v"] == float(v) and not _TIMED.match(t, e):
            reads[-1]["untimed"] = True
    return reads


def _mass_of(t: str, s: int, e: int) -> list:
    """The noun phrase a mass spanning t[s:e] with no noun of its own is said of: the reply part before it ("the lids:
    20 g", "lids, 20 g", "the lids? about 20 g") or the one after it ("20 g each for the lids"), or [] (a bare "20 g")."""
    m = re.match(r"\s*(?:each|apiece)?\s*(?:for|of|on)\s+((?:the|their|its|my|our|these|those)\s+[a-z][a-z' -]*?)"
                 r"(?=\s*(?:$|[.,;:!?]|\band\b))", t[e:])
    if m:
        return m.group(1).split()
    parts = re.split(r"[,;:?!()\-–]|\band\b|\bplus\b", t[:s])
    if len(parts) > 1 and re.fullmatch(r"\s*(?:" + _FIX_WORDS + r"\s+)?(?:(?:about|around|roughly|approximately|~|each|"
                                       r"maybe|probably|only|just)\s+)*", parts[-1]):
        np = re.sub(r"^(?:(?:and|oh|so|also|plus|what about|how about|as for)\s+)+", "", parts[-2].strip(" .")).split()
        return np if _looks_np(np) else []   # "and the lids? about 20 mm": the lids
    return []


_DIM_WORDS = ("diameter", "width", "size", "dia", "thickness")
_DIM_LINKS = frozenset(("has", "have", "had", "with", "of", "is", "are", "was", "were", "got", "gets", "come", "comes",
                        "total", "overall", "outer", "outside", "full", "max", "maximum", "actual", "real"))
_FIX_WORD_SET = frozenset(("sorry", "actually", "oops", "correction", "typo", "update", "ok", "okay", "right", "no", "nope",
                           "wait", "um", "erm", "er", "oh", "ah", "also", "so", "well", "hmm", "mistake", "bad", "meant", "mean"))
# a verb that moves the objects a distance ("move the cylinders 60 mm"), and what after a length says it is one ("60 mm
# to the left", "60 mm higher")
_MOVED_BY = re.compile(r"\b(?:mov\w*|push\w*|slid\w*|shift\w*|lift\w*|rais\w*|lower\w*|drop\w*|carr\w*|turn\w*|rotat\w*|"
                       r"spac\w*|offset\w*|travel\w*|reach\w*|extend\w*|go|goes|went|bring\w*|brought|take|takes|took|"
                       r"put\w*|plac\w*|set|sets|nudg\w*|step\w*|stack\w*|hold\w*|grip\w*|open\w*|clos\w*)\b")
_DISTANCE_AFTER = re.compile(r"\s*(?:to\b|towards?\b|left\b|right\b|forwards?\b|backwards?\b|back\b|up\b|down\b|away\b|apart\b|"
                             r"from\b|off\b|along\b|over\b|across\s+the\b|higher\b|lower\b|further\b|farther\b|closer\b|"
                             r"sideways\b|in\s+front\b|behind\b|above\b|below\b|high\b|tall\b|long\b|deep\b|of\s+travel\b|"
                             r"in\s+(?:height|length|depth)\b|between\b|more\b|less\b|bigger\b|smaller\b|wider\b|narrower\b)")


def _dim_theirs(t: str, at: int, words: set) -> bool:
    """Whether the width word before a size at `at` ("the diameter is 60 mm", "width: 60 mm") is the objects': said
    of nothing else ("the bin's width", "bin width", "the width of the slot" are not)."""
    m, s0 = None, _clause_start(t, at, ";!?,")   # "width: 60 mm"
    for m in re.finditer(r"\b(?:" + "|".join(_DIM_WORDS) + r")\b", t[s0:at]):
        pass
    if m is None:
        return False
    before = [re.sub(r"'s?$", "", w) for w in re.findall(r"[a-z][a-z'-]*", t[s0:s0 + m.start()])]
    after = t[s0 + m.end():at]
    if re.match(r"\s+of\s+(?!them\b|each\b|these\b|those\b|the\s+(?:" + "|".join(sorted(words) or ["-"]) + r")s?\b)[a-z]",
                after):
        return False   # "the width of the slot is 60 mm" (not "a diameter of 28 mm")
    while before and (before[-1] in _DETS - {"their", "its"} or before[-1] in _DIM_LINKS):
        before.pop()   # "the gripper has a width of 60 mm": the gripper's
    owner = before[-1] if before else ""
    if owner in ("them", "these", "those") and len(before) > 2 and before[-2] == "of" and before[-3] in _QUANTS:
        return False   # "one of them has a diameter of 20 mm": some of them
    return not owner or owner in ("their", "its", "they", "them") or owner in _PRONOUNS or owner in _FIX_WORD_SET or \
        owner in _NP_STOP or _singular(owner) in words


def _clause_start(t: str, s: int, marks: str = ";!?,:") -> int:
    return max([m.end() for m in re.finditer(r"[" + marks + r"]|\.(?!\d)|\b(?:and|but|then|so)\b", t[:s])], default=0)


# a time or a guess is no thing a size is of ("at the end of the day 40 mm"), nor is a part of someone ("my head")
_NOT_THINGS = frozenset(("day", "days", "time", "times", "moment", "week", "morning", "evening", "night", "way", "case",
                         "rate", "least", "whole", "lot", "mind", "guess", "estimate", "memory"))
# what an "of"/"for" or a bracket after a size may name that is all of them ("for the lot", "(all of them)")
_THEIRS_TOO = frozenset(("lot", "set", "batch", "bunch", "whole", "them", "these", "those", "it"))
_SIZE_WORDS = frozenset(("diameter", "dia", "width", "widths", "size", "sizes", "measurement", "measurements", "weight",
                         "weights", "mass", "wide", "across", "thick", "approx", "approximately", "about", "around",
                         "roughly", "just", "only", "exactly", "precisely", "nearly", "almost", "maybe", "probably"))


def _part_beside(t: str, s: int, e: int, heads: set, words: set) -> list:
    """The part or the other thing a size spanning t[s:e] is said of with no word tying them: a part of theirs just
    before it ("lids 20 mm wide", "cap diameter 20 mm", "lids 20 g"), a noun phrase with "the" before it ("the caps
    20 mm") or one after it ("20 mm wide for the caps", "20 mm (the caps)"), or []."""
    q = re.match(r"\s*(?:wide|across|in\s+diameter|diameter|thick|big|heavy)?\s*(?:each\s+|apiece\s+)?(?:for|on|of)\s+"
                 r"(?:some|most|many|several|half|a\s+few|a\s+couple)\b", t[e:])
    if q:
        return ["some"]   # "20 mm wide for some (of them)"
    m = re.match(r"\s*(?:wide|across|in\s+diameter|diameter|thick|big|heavy)?\s*(?:each\s+|apiece\s+)?(?:for|on|of)\s+"
                 r"((?:the|their|its|these|those)\s+[a-z][a-z' -]*?)(?=\s*(?:$|[.,;:!?)]|\band\b))", t[e:])
    if not m:
        m = re.match(r"\s*(?:wide|across|in\s+diameter|diameter|thick|big|heavy)?\s*\(\s*((?:(?:the|their|its)\s+)?"
                     r"[a-z][a-z' -]*?)\s*\)", t[e:])
    if m:
        np = [w for w in m.group(1).split() if w not in ("themselves", "itself")]
        if not np or np[-1] in ("mistake", "bad", "typo", "error", "fault"):
            return []           # "sorry, 30 mm wide (my mistake)"
        return ["them"] if np[-1] in _THEIRS_TOO else np   # "for the lot", "(all of them)": theirs
    clause = re.split(r"[.,;:!?()\[\]]|\s-\s|\b(?:and|but|while|whereas|plus|then)\b", t[:s].replace("’", "'"))[-1]
    toks = [re.sub(r"'s$", "", w) for w in re.findall(r"[a-z][a-z'-]*", clause)]
    while toks and toks[-1] in _SIZE_WORDS:
        toks.pop()
    if not toks:
        return []
    head = toks[-1]
    if head in _NOT_THINGS or head == "head" and len(toks) > 1 and toks[-2] in ("my", "your", "our", "his", "her"):
        return []   # "at the end of the day 40 mm wide", "off the top of my head 40 mm"
    if _singular(head) in _PARTS | _NARROW_PARTS and _singular(head) not in heads | words:
        return [head]   # "lids 20 mm wide", "jar lids 20 mm", "cap diameter 20 mm"
    if _singular(head) in heads and not (len(toks) > 1 and (toks[-2] in _PREPS or toks[-2] in _QUANTS)):
        return [head]   # "cylinders 60 mm": theirs (never "some cylinders", "of the cylinders")
    if head in _NP_STOP or head in _DETS or head in _PRONOUNS or head in _TIES:
        return []
    for n in (2, 3):   # "the caps 20 mm", "the small ones 20 mm"
        if len(toks) >= n and toks[-n] in ("the", "their", "its", "these", "those") and \
                not any(w in _NP_STOP or w in _DETS for w in toks[1 - n:]):
            return toks[-n:]
    return []


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
    cur = b.get("object")
    if cur and cur.get("key") is None and cur.get("stated") is not None:   # a described object the person refines
        sh = _shape_said(t) if o is None and dia is None else None
        if sh and (sh == "box" or _says_shape_of_them(t, cur)):   # "they are round"
            s["object"] = dict(cur, shape=sh, stated=sorted(set(cur["stated"]) | {"shape"}))
            return
        if sh and not ({sh, cur.get("shape")} <= {"cylinder", "sphere"} and "shape" in cur["stated"]):
            # a round word not plainly said of them ("they're round, sorry", "the caps are round", "my SO-101 can do
            # it") on objects not already stated round: their shape is no longer known, so the stock SO-101 holds them
            # only from 26 to 34 mm, and a generated gripper is planned for round ones, its narrowest band
            s["object"] = dict(cur, shape="cylinder", shape_unknown=True, stated=sorted(set(cur["stated"]) - {"shape"}))
            return
        if o is not None and o.get("key") is None:     # "40 mm cylinders" after "40 mm containers weighing 90 g": 90 g stays
            for k in cur["stated"]:
                if k not in (o.get("stated") or []) and k in cur:
                    o[k] = cur[k]
            o["stated"] = sorted(set(o.get("stated") or []) | set(cur["stated"]))
            o["length_mm"] = max(float(o.get("length_mm") or 0), float(o["diameter_mm"]))
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


def _later_sizes(b: Brief, p: dict, t: str, named: str | None, raw: str | None = None):
    """A later message's widths and masses (_size_reads) on the object _typed_answer made of it: the objects' own size
    replaces theirs; a size that may be something else's only widens them or weighs them more, and a smaller width it
    gives is kept as the least they may be (least_mm, for the stock SO-101's jaws); a mass per hour or a batch's beside
    their own mass is left out. `named` is the Morph object the message itself names, if any; `raw` is the message as
    typed."""
    cur = b.get("object")
    if named and (cur is None or named != cur.get("key")):
        base = object_value(named)        # a Morph object the message names: its sizes, before the message's own
    elif named:
        # the object named again ("the eggs are fragile"): what the person stated about it, but never below Morph's
        # preset for it, which naming it again read before (150 g eggs were planned as 60 g ones on the SO-101)
        pre, base = object_value(named), dict(cur)
        for k in ("diameter_mm", "mass_g"):
            base[k] = max(float(cur[k]), float(pre[k]))
        if float(cur["diameter_mm"]) < base["diameter_mm"]:
            base["least_mm"] = min(float(cur["diameter_mm"]), float(cur.get("least_mm") or 1e9))
        base["length_mm"] = max(float(cur.get("length_mm") or 0), base["diameter_mm"])
    else:
        base = cur
    reads = _size_reads(b, t, base, raw)
    w_obj, w_may, w_wid = ([r["v"] for r in reads if r["dim"] == "w" and r["cls"] == c] for c in ("obj", "maybe", "wider"))
    m_obj, m_may, rates = ([r["v"] for r in reads if r["dim"] == "m" and r["cls"] == c] for c in ("obj", "maybe", "rate"))
    o = p["slots"].get("object")
    narrow = [r["v"] for r in reads if r["dim"] == "w" and r.get("part")]   # "40 mm at the neck", "45 mm necks"
    # a rate left out before (rate_g) comes back when a later message says the objects may weigh more than their mass
    # ("2 kg an hour" then "that's the empty weight", "some are heavier", "they hold 300 ml of water")
    rate = float(cur.get("rate_g") or 0) if cur is not None else 0.0   # also when the message names another object
    untimed = bool(rate and cur.get("rate_untimed")) or any(r.get("untimed") for r in reads if r["cls"] == "rate")
    back = rate and (_MORE_THAN_SAID.search(t) or not m_obj and _WEIGHT_TALK.search(t) or
                     not _rate_drop_holds(_said(b, t if raw is None else raw), _object_words(cur), untimed))
    if o is None:
        if not (w_obj or w_may or w_wid or narrow and cur is not None or back):
            return
        o = p["slots"]["object"] = {k: v for k, v in dict(cur or value(b, "object")).items()}
    elif named and cur is not None and named == cur.get("key"):
        for k in ("diameter_mm", "mass_g", "length_mm", "least_mm", "widest_unknown"):
            if k in base:
                o[k] = base[k]
        o["stated"] = sorted(set(o.get("stated") or []) | set(cur.get("stated") or []))
    if cur is not None and base is cur and cur.get("key") is None and o.get("key") is None and \
            str(o.get("name")) != str(cur.get("name")) and str(o.get("name")).split() and \
            _singular(str(o.get("name")).split()[-1]) in _PARTS | _NARROW_PARTS:
        # parse() named a part of them ("45 mm necks", "the lids are 35 mm"): the objects stay what they were, and keep
        # their shape ("they come as 40 mm cubes" still says what they are) and their size: a part's only widens them
        # ("30 mm handles" on 50 mm cylinders); a shape said of the part ("45 mm bases, they're square") may be their
        # widest part's, so theirs is no longer known
        part_shape = "shape" in (o.get("stated") or []) and o.get("shape") != cur.get("shape")
        for k in ("diameter_mm", "length_mm"):
            if cap._num(cur.get(k)) and float(cur[k]) > float(o.get(k) or 0):
                o[k] = float(cur[k])
        o["name"], o["shape"] = cur.get("name"), cur.get("shape")
        o["stated"] = sorted(set(o.get("stated") or []) - {"shape"} | ({"shape"} & set(cur.get("stated") or [])))
        if part_shape:
            o["shape_unknown"] = True
            o["stated"] = sorted(set(o["stated"]) - {"shape"})
    if cur is not None and o is not cur and (o.get("key") != cur.get("key") or str(o.get("name")) != str(cur.get("name"))):
        for k in ("diameter_mm", "mass_g"):   # "they weigh 300 g", then "they come in 40 mm cups": still 300 g or more
            if k in (cur.get("stated") or []) and cap._num(cur.get(k)) and float(cur[k]) > float(o.get(k) or 0):
                o[k] = float(cur[k])
                o["stated"] = sorted(set(o.get("stated") or []) | {k})
    ref = base or o
    was = set(ref.get("stated") or []) if base else set()
    stated = set(o.get("stated") or [])
    if w_obj or w_may or w_wid:
        bw = float(ref["diameter_mm"])
        w = max(w_obj + w_may + w_wid) if w_obj else max([bw] + w_may + w_wid)
        lo = min(w_obj + w_may) if w_obj else min([float(ref.get("least_mm") or bw)] + w_may)
        o["diameter_mm"], o["length_mm"] = w, max(float(o.get("length_mm") or 0), w)
        if w_obj or w != bw or "diameter_mm" in was:
            stated.add("diameter_mm")
        o.pop("least_mm", None)
        if lo < w:
            o["least_mm"] = lo
        if w_obj:
            o.pop("widest_unknown", None)
    elif base is not None and o.get("key") == base.get("key") and \
            float(o.get("diameter_mm") or 0) == float(base.get("diameter_mm") or 0):
        for k in ("least_mm", "widest_unknown"):
            if base.get(k):
                o[k] = base[k]
    if base is not None and base.get("widest_unknown") and not w_obj:
        o["widest_unknown"] = True
    if narrow and max(narrow) >= float(o["diameter_mm"]) - 0.5:
        o["widest_unknown"] = True   # a neck or an opening as wide as the objects: their widest part is not given
    if m_obj or m_may or rates:
        bm = float(ref["mass_g"])
        o["mass_g"] = max(m_obj + m_may) if m_obj else max([bm] + m_may)
        if m_obj or o["mass_g"] != bm or "mass_g" in was:
            stated.add("mass_g")
    o.pop("rate_g", None)
    o.pop("rate_untimed", None)
    if back:   # the earlier rate, and any this message gives ("450 g per minute" after "60 g per hour"), as their mass
        o["mass_g"] = max([float(o["mass_g"]), rate] + rates)
        stated.add("mass_g")
    elif rate or rates:
        o["rate_g"] = max([rate] + rates)
        if untimed:
            o["rate_untimed"] = True
    o["stated"] = sorted(stated)


# a new arm asked for in a later message ("actually, build a new arm", "design a new one instead"); not a question about
# one ("do I need a new arm?"), one turned down ("no need for a new arm", "rather than a new arm") or a remark ("a new arm
# would cost too much")
_NEW_ARM = re.compile(r"\b(?:(?:a|an)\s+)?(?:brand[- ]new|new|different|another|fresh|separate|custom|morph[- ](?:generated|made|"
                      r"designed))\s+(?:robot\s+)?arm\b|\b(?:build|make|design|generate|get|want|need|go\s+with|prefer|"
                      r"try)\s+(?:me\s+|us\s+)?(?:a|an)\s+(?:brand[- ]new|new|different|fresh|separate|custom)\s+one\b|"
                      r"\bfrom\s+scratch\b|\bmorph[- ]generated\b|"
                      r"\b(?:generate|design|build|make)\s+(?:me\s+|us\s+)?(?:a|an|the|my)\s+(?:robot\s+)?arm\s+(?:instead|for\s+me)\b")
_NOT_NEW_ARM = re.compile(r"\b(?:no|not|don'?t|do\s+not|doesn'?t|won'?t|wouldn'?t|never|without|instead\s+of|rather\s+than|"
                          r"no\s+need|needn'?t|keep|stick\s+with|can'?t|cannot|isn'?t|too\s+(?:much|expensive|costly))\b")


# what may come before the new arm in a request ("actually, build a new arm", "let's design a new one", "I'd rather have
# a new arm", "a new arm please"), not in a remark ("my friend built a new arm", "I'll build a new arm next year")
_ASKS_FOR = re.compile(r"\s*(?:(?:actually|ok|okay|no|so|then|instead|please|just|right|hmm|well|sorry|wait|oh|fine|change of plan)"
                       r"[,!:]?\s+)*(?:(?:let'?s|please|(?:i|we)(?:\s+(?:want|need|would\s+like|would\s+rather|prefer)|['’]d\s+"
                       r"(?:like|rather|prefer))|go\s+(?:ahead\s+and\s+)?|you\s+can|"
                       r"i\s+think\s+(?:we|you)\s+should|better)\s+)?(?:(?:build|make|design|generate|get|go\s+with|use|create|"
                       r"try|have|do)\s+(?:it|me|us)?\s*)?(?:with\s+|from\s+)?")


def _asks_new_arm(t: str) -> bool:
    """Whether a later message (lower case, without the SO-101 in it) asks for a Morph-generated arm instead."""
    if re.search(_SO, t):
        return False   # parse() reads a message that names the SO-101
    for m in _NEW_ARM.finditer(t):
        start = max(t.rfind(c, 0, m.start()) for c in ".;!?,") + 1
        end = min((i for i in (t.find(c, m.end()) for c in ".;!?") if i >= 0), default=len(t))
        clause = t[start:end + 1]
        if clause.rstrip().endswith("?") or _NOT_NEW_ARM.search(clause) or \
                re.match(r"\s+(?:is|was|would|will|could|might|costs?|sounds?|seems?|looks?|came|arrived)\b", t[m.end():]):
            continue
        if _ASKS_FOR.fullmatch(t[start:m.start()]):
            return True
    return False


def _arch_without_so101(b: Brief, text: str = "") -> str:
    """The archetype once the SO-101 is out: the object's, else the one the person's own words pick without the
    SO-101 (loading cups into the dishwasher is dishes, a playroom toys, with the people next to them), else general
    pick and place."""
    o = b.slots.get("object")
    mine = _archetype_for_object(o.value) if o is not None else None
    said = mine if mine and o.source in ("user", "video", "photo") else None
    words = detect_archetype(re.sub(_SO, " ", conversation(b, text).lower()))[0]
    before = b.flags.get("left_so101") if isinstance(b.flags.get("left_so101"), str) else None
    keys = [k for k in (said, words, mine, _arch(b).key, before) if k in ARCHETYPES and k not in ("generic_arm", "so101")]
    # the first with an emergency stop (people next to it, a kitchen, a lab or a classroom, a hand-over): an object Morph
    # assumed or chose ("@nearest", a chip) and the request's words may pick different jobs, and either one's emergency
    # stop stays (recycling espresso cups is dishes too); else the object's own job or the general arm, as before
    return next((k for k in keys if _gives_estop(b, k)), mine or "generic_arm")


def _leave_so101(b: Brief, text: str = ""):
    """The person leaves the SO-101 (a chip, "I don't have an SO-101", "actually, build a new arm", the nearest robot):
    marked with the job leaving it gave before (the object's, else the general arm), so that job's emergency stop is
    still one to keep (_arch_without_so101), and the job of the request's own words picked if the SO-101 was the job."""
    b.flags["left_so101"] = _archetype_for_object(b.get("object")) or "generic_arm"
    if _arch(b).key == "so101":
        set_archetype(b, _arch_without_so101(b, text), 0.9, "user")


def _gives_estop(b: Brief, key: str) -> bool:
    """Whether the brief would have an emergency stop with archetype `key`, by what the person said or else its
    defaults: people, children or pets next to it, a kitchen, a lab or a classroom, or a hand-over (build_changes)."""
    a = ARCHETYPES.get(key)
    if a is None:
        return False
    people, env, task = b.get("people_nearby") or a.people, b.get("environment") or a.environment, b.get("task") or a.task
    return people in ("yes", "children", "pets") or env in ("lab", "kitchen", "classroom") or task == "hand_over"


def answer(b: Brief, answers: dict | None, text: str | None = None) -> list:
    """Apply chip answers ({slot: value}) and an optional free-text answer. Returns notes for what was ignored."""
    notes = []
    for name, v in (answers or {}).items():
        v2 = coerce(name, v) if name in SLOTS else None
        if v2 is None:
            notes.append(f"ignored answer for {name}")
            continue
        was_so101 = value(b, "existing_hardware") == "so101" or _arch(b).key == "so101"
        set_slot(b, name, v2, "user", 1.0, "answer")
        if name not in b.asked:
            b.asked.append(name)
        if name == "object":
            a2 = _archetype_for_object(v2)
            if _arch(b).key == "generic_arm" and a2:
                set_archetype(b, a2, 0.9, "user")
        if name == "existing_hardware":   # leaving the SO-101 keeps the job's own people (_arch_without_so101)
            if v2 == "so101":
                b.flags.pop("left_so101", None)
                set_archetype(b, "so101", 0.9, "user")
            elif was_so101:
                _leave_so101(b)
            else:   # a job's robot keeps a job whose emergency stop the object's would drop ("tidy the playroom"),
                k = _archetype_for_object(b.get("object")) or "generic_arm"   # its object never below the one
                if not (_gives_estop(b, _arch(b).key) and not _gives_estop(b, k)):   # the release's reset gave
                    set_archetype(b, k, 0.9, "user")
                else:
                    b.flags["left_so101"] = k
    if text and text.strip():
        t = text.strip()[:2000]
        p = parse(t)
        # read harm over the whole conversation, not the reply alone: "a robot that holds a rivet gun" then "point it at
        # my brother" is caught; "a robot that plays chess" then "it should beat my dad" keeps the game context
        hit = harmful(conversation(b, t))
        if hit:
            refuse(b, hit, "rules")
            return notes
        named = (p["slots"].get("object") or {}).get("key")
        was_so101 = value(b, "existing_hardware") == "so101" or _arch(b).key == "so101"
        nt = normalize_units(t.lower())
        _typed_answer(b, p, nt)
        _later_sizes(b, p, nt, named, t)
        keep = {"slots": {k: v for k, v in p["slots"].items() if k in ("object", "throughput", "budget", "quantity", "precision",
                                                                         "fragility", "people_nearby", "perception", "connectivity",
                                                                         "environment", "task", "existing_hardware")},
                "inferred": p["inferred"], "hints": p["hints"], "oos": p["oos"], "implicit": p["implicit"], "said": p["said"],
                "adjusted": p["adjusted"], "person": p["person"]}   # a person a later message names stays, like the first's
        merge(b, keep, "user", "answer")
        hw = p["slots"].get("existing_hardware")   # "I have an SO-101" or "use my SO-101", typed in a later message
        if hw is None and value(b, "existing_hardware") == "so101" and _asks_new_arm(nt):
            hw = "none"                            # "actually, build a new arm" after "use my SO-101"
            # the release kept the SO-101 there, and one that can't take the object went to the nearest robot with the
            # SO-101's own guess (a raw egg in a kitchen): that job's emergency stop is kept (build_changes)
            b.flags["so101_guess"] = _archetype_for_object(b.get("object") or value(b, "object")) or "generic_arm"
            set_slot(b, "existing_hardware", hw, "user", 1.0, "answer")
        if hw == "so101":
            set_archetype(b, "so101", 0.9, "user")
            b.flags.pop("left_so101", None)
        elif hw == "none" and was_so101:
            # "my SO-101 broke", "forget the SO-101": the release kept the SO-101 for some of these, and one that can't
            # take the object went to the nearest robot with the SO-101's own guess (build_changes keeps its stop)
            b.flags.setdefault("so101_guess", _archetype_for_object(b.get("object") or value(b, "object")) or "generic_arm")
            _leave_so101(b, t)
        a2 = _archetype_for_object(b.get("object"))
        if _arch(b).key == "generic_arm" and a2:
            set_archetype(b, a2, 0.9, "user")
    if b.flags.get("left_so101") and _arch(b).key != "so101":
        # an object named after leaving the SO-101 ("I don't have an SO-101", then "espresso cups") brings its job's
        # emergency stop, as it did when leaving it set the general arm
        k = _arch_without_so101(b, text or "")
        if k != _arch(b).key and _gives_estop(b, k) and not _gives_estop(b, _arch(b).key):
            set_archetype(b, k, 0.9, "user")
    b.turns.append({"role": "user", "answers": dict(answers or {}), "text": (text or "")[:2000], "ts": _now_iso()})
    return notes


def conversation(b: Brief, text: str | None = None) -> str:
    """The person's words so far, the request and every typed reply (and `text`), as one passage for the harm filter: a
    tool held in one message and turned on someone in the next is read together, and a reply that goes on with the
    last message ("on the cat", "only at people", "and swings it at my sister") as the same sentence. The last 2,000
    characters."""
    said = [x.get("text") or "" for x in b.turns if x.get("role") == "user"]
    if not said or said[0] != b.request:
        said.insert(0, b.request)
    out = ""
    for w in said + [text or ""]:
        w = " ".join(w.split()).rstrip(".")
        if w:
            on = out and re.match(r"(?:at|on|onto|to|toward|towards|against|only|just|and|but|then|so|with|into|in|over|"
                                  r"under|for)\b", w, re.I)
            out += (" " if on else ". " if out else "") + w
    return out[-2000:]


def _recheck_rate(b: Brief):
    """A rate or a batch left out of the objects' mass (rate_g) comes back as their mass once the whole conversation no
    longer lets it be left out (_rate_drop_holds): at the pace the arm is built at (a later "one every 30 s", the pace
    chip, the job's own), with the chips answered (a task or a layout chip before or after a batch) and the object as it
    now is."""
    o = b.slots.get("object")
    ov = o.value if o is not None and isinstance(o.value, dict) else None
    if not ov or not cap._num(ov.get("rate_g")):
        return
    chips = frozenset(k for x in b.turns if x.get("role") == "user" for k in (x.get("answers") or {}))
    cycle = float(cap._num(value(b, "throughput")) or 6.0)
    if _rate_drop_holds(_said(b), _object_words(ov), bool(ov.get("rate_untimed")), cycle, chips):
        return
    nv = {k: v for k, v in ov.items() if k not in ("rate_g", "rate_untimed")}
    nv["mass_g"] = max(float(nv["mass_g"]), float(ov["rate_g"]))
    if o.source not in ("user", "video", "photo"):   # the nearest robot's stand-in, never checked again: Morph's largest
        nv["mass_g"] = min(nv["mass_g"], max(float(ov["mass_g"]), float(cap.MAX_MASS_G)))
    nv["stated"] = sorted(set(nv.get("stated") or []) | {"mass_g"})
    b.slots["object"] = Slot(nv, o.source, o.confidence, o.evidence)


def progress(b: Brief, *, defaults_now: bool = False, nearest: bool = False, deep: bool = True,
             home: str | None = None) -> Brief:
    """Decide the next state: refused / out_of_scope / asking (with questions) / ready (mapped and preflighted)."""
    if b.status in ("refused", "building", "built"):
        return b
    _recheck_rate(b)
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
        _recheck_rate(b)
        sc = assess_scope(b)
        if sc["status"] == "out_of_scope" and (sc.get("nearest") or {}).get("slots") == {"existing_hardware": "none"}:
            prior += sc["notes"]   # the stock SO-101 the nearest robot kept doesn't hold the object either: a new arm
            accepted, note = b.flags.get("oos_accepted"), b.flags.get("nearest_note")
            apply_nearest(b, sc["nearest"])
            b.flags["oos_accepted"], b.flags["nearest_note"] = accepted, note
            _recheck_rate(b)
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
    hands = value(b, "task") == "hand_over" or _arch(b).key == "so101" and ARCHETYPES[_arch_without_so101(b)].task == "hand_over"
    guess = b.flags.get("so101_guess")   # a new arm the release didn't read, for an object its SO-101 couldn't take
    hands = hands or bool(guess and value(b, "existing_hardware") == "none" and _gives_estop(b, guess) and
                          (_so101_misfit(b.get("object")) or _so101_below_min(b.get("object"))))
    if value(b, "people_nearby") in ("yes", "children", "pets") or value(b, "environment") in ("lab", "kitchen", "classroom") \
            or hands or b.flags.get("person"):
        extras.add("estop")   # a hand-over's receiver, or a person the request names, is next to it whatever was said
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
            f = imported.gripper_summary(d, sz, quick=True)["force"]   # the stock jaw's lever estimated, never searched
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
        from ..importers.common import home  # the projects folder (imported robots' files are in it)
        from ..services import durable
        issues.append(f"preflight could not run ({exc.__class__.__name__}: {durable.explain(exc, home())})")
    return {"issues": list(dict.fromkeys(issues)), "ms": round((time.perf_counter() - t0) * 1000), **out}


ISSUE_KINDS = (("torque", r"needs [\d.]+ N·m"), ("supply", r"^supply .*margin"), ("too_small", r"jaw travel fits|object too small"),
               ("deflection", r"tip deflection|bending safety factor"),
               ("grip", r"no safe grip force|grip force [\d.]+ N < required|reaches only .* per finger"),
               ("stroke", r"stroke too small"), ("printer", r"exceeds the .* (?:build volume|printer bed)"),
               # a hobby gripper servo, with no torque limit, can't cover the object's size with force sensors, or without
               # them hold it, keep every size of it under stall or close past it by less than it may squeeze
               # (electronics2.open_loop_squeeze)
               ("hobby_gripper", r"has no torque limit to back up the force sensors, and reaching|"
                                 r"hobby gripper servo has no torque limit and (?:holds only|closes by position only)|"
                                 r"has no torque limit, and the least it can close past"))


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
    FSR finger or a V pad; a hobby gripper servo that can't grip the object safely -> smart-bus servos (also when the
    person asked for hobby servos, as for torque); too big for the printer -> pilot build."""
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
        if "hobby_gripper" in kinds and ch.get("arm.actuator_family") == "hobby_pwm" and not ch.get("arm.source"):
            ch["arm.actuator_family"] = "smart_bus"
            notes.append({"kind": "repair", "slot": "budget", "text": "A hobby PWM gripper servo has no torque limit, so it "
                          "can't grip this object safely across its size (or hold it as hard as it needs); I used smart-bus "
                          "servos, whose torque limit sets or backs up the squeeze."})
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
        handed = slot == "people_nearby" and value(b, slot) == "no" and (value(b, "task") == "hand_over" or b.flags.get("person"))
        kept = (_HANDED if value(b, "task") == "hand_over" else _named_person(b)) if handed else ""
        if s is not None and s.source in ("user", "video", "photo") or (s is not None and s.source == "memory"):
            if handed:   # "It works on its own" on a hand-over or beside a person the request names: they are still there
                add(slot, kept, "inferred")
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
            elif handed:
                add(slot, kept, kind)
            elif env not in ("lab", "kitchen", "classroom"):   # the request asked for one
                add(slot, f"I assumed {PEOPLE[v]}; it has the emergency stop you asked for.", kind)
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


_HANDED = ("It hands things to a person, who takes them from the gripper within its reach, so it has an emergency stop "
           "even though no one else works next to it.")


def _named_person(b: Brief) -> str:
    """The card's line for a person the request puts next to the robot when the people answer says no one."""
    words = next(iter((b.flags.get("person") or {}).values()), "")
    return (f"Your request puts a person next to it (\"{words}\"), so it has an emergency stop even though no one else "
            "works next to it.")


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
            elif slot == "people_nearby":   # a hand-over's receiver is next to it whatever else was said
                txt = "Only the person it hands things to" if v == "no" and value(b, "task") == "hand_over" else \
                    "Only the person your request names" if v == "no" and b.flags.get("person") else PEOPLE[v].capitalize()
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
    p = os.path.join(root, bid + ".json")  # the id is 16 hex characters: the name itself can't leave the folder
    if not os.path.islink(p):
        # never realpath a draft itself: on Windows one being deleted while still open resolves into
        # \\?\C:\$Extend\$Deleted, and comparing that with the folder raised instead of answering
        return p
    real = os.path.realpath(p)  # a link someone put here is followed only while it stays in the folder
    try:
        return real if os.path.commonpath([root, real]) == root else None
    except ValueError:  # another drive on Windows
        return None


def save_draft(b: Brief, home: str) -> str | None:
    p = draft_path(home, b.id)
    if not p:
        return None
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(b.to_dict(), fh, ensure_ascii=False, default=str)
    _retry_on_windows(lambda: os.replace(tmp, p))
    return p


def _retry_on_windows(op):
    """op(), tried again for up to a second while it raises PermissionError: Windows refuses to replace or delete a
    file another thread or process has open (a page polling the brief's status reads its draft)."""
    for attempt in range(20):
        try:
            return op()
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


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
            _retry_on_windows(lambda: os.remove(p))   # else a built brief's draft stays, reading "building"
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
