"""Checks for everything a request, a design or a robot bundle can name: project names, robot sources,
file paths, request bodies and parameter edits.

Paths are compared canonically (os.path.realpath + os.path.commonpath), never as string prefixes, so
`proj-secret` is not inside `proj`, and a symlink pointing out of a folder does not count as inside it.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.request
from urllib.parse import unquote

MAX_BODY = 1 << 20          # JSON request bodies; routes that need more (video frames) raise it explicitly
MAX_MESSAGE = 8000          # characters in one chat message
PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
ROBOT_SOURCE = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
# Keys and codes nobody may paste outside the Keys dialog, told apart from ordinary words by how each one is made:
# - a Claude key starts sk-ant- and a Keewano personal access token kwpat-: the prefix is enough;
# - any other sk- key (SubQ's) is sk- and a random run: 20 or more token characters with a digit among them, unless
#   they read as a hyphenated phrase (_phrase), such as the part names sk-6x40-stainless-steel-screws or
#   sk-thin-and-light; a key in groups (sk-live-4f8a-91bc-77de-12ab) almost never does, numbered names
#   (servo1_servo2_servo3, left_knee_arm2) and versioned ones (yoloV8, jointLimit2: _versioned) do;
# - Morph Teams' codes, which act as a member, are random base64url (secrets.token_urlsafe): invite (mi_) and sign-in
#   link (ml_) codes 22 characters long and script tokens (mt_) 43, each with a capital letter (TEAM_CODE). Every
#   mi_/ml_/mt_ run of 20 or more characters (a code, part of one, a code with more text stuck to it) counts when it
#   has a capital and doesn't read as a phrase, and teams.auth.new_code draws again the rare code without a capital,
#   one that (or a piece of which cut short) reads as a phrase, or one with no part that reads as random (random_part:
#   with one, text joined to the code never makes it a phrase); 259 in 1,000,259 invite and sign-in codes and 35 in
#   200,035 script tokens (tools/key_filter_rates.py), so every code Morph makes is caught: names such as
#   mt_shoulder_joint_torque_limit, ml_policy_weights_3f9a2c1e, mt_shoulderJointTorqueLimit and
#   ml_policyV1_final_weights (as long as an invite code) are names, not codes;
# - the value of a key saved in the Keys dialog (SAVED_KEYS), whatever its shape, and the running remote Studio's
#   access code (ACCESS_CODES);
# - another service's key with a fixed prefix (GitHub's ghp_, Hugging Face's hf_, AWS's AKIA, ...) or text that carries a
#   key: OTHER_KEYS.
TEAM_CODE = re.compile(r"(?:m[il]_(?=[A-Za-z0-9_-]{0,21}[A-Z])[A-Za-z0-9_-]{22}"
                       r"|mt_(?=[A-Za-z0-9_-]{0,42}[A-Z])[A-Za-z0-9_-]{43})(?![A-Za-z0-9_-])")
# Where a key may start: after anything but an ASCII letter, digit or _. Not \b, which Python reads by Unicode: it
# counts 是, は, 는, а and é as letters, so a key glued to Chinese, Japanese or Korean text (written with no spaces) slipped by.
START = r"(?<![A-Za-z0-9_])"
# what may be a key or a code; _is_key decides for the sk- and m*_ runs
SECRET = re.compile(START + r"(?:sk-ant-[A-Za-z0-9_-]{8,}|kwpat-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{14,}(?![A-Za-z0-9_-]))"
                    r"|(?<![A-Za-z0-9])m[til]_[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")
# Other services' keys, known by a fixed prefix and a long random run, and text that carries a key: a private key block,
# a link with a password in it, a bearer token, an .env line (NAME_KEY=value, NAME_TOKEN=..., which is also how a key
# with no prefix of its own, such as an AWS secret key, is caught). A key with no prefix pasted on its own is not: only
# the value of one saved in the Keys dialog (SAVED_KEYS, MORPH_TOOL_*) is.
OTHER_KEYS = re.compile(
    START + r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_-]{20,}"  # GitHub, GitLab
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"                                                    # AWS key ids
    r"|AIza[A-Za-z0-9_-]{35}|GOCSPX-[A-Za-z0-9_-]{20,}|ya29\.[A-Za-z0-9_-]{20,}|1//0[A-Za-z0-9_-]{30,}"  # Google
    r"|[sr]k_(?:live|test)_[A-Za-z0-9]{20,}|whsec_[A-Za-z0-9+/=]{20,}"                            # Stripe
    r"|xox[abposr]-[A-Za-z0-9-]{20,}|xapp-[0-9]-[A-Za-z0-9-]{20,}"                                 # Slack
    r"|hf_[A-Za-z0-9]{30,}|r8_[A-Za-z0-9]{30,}|gsk_[A-Za-z0-9]{40,}|xai-[A-Za-z0-9]{40,}"       # Hugging Face, Replicate,
    r"|pplx-[A-Za-z0-9]{40,}|sk_[0-9a-f]{40,}"                                                     # Groq, xAI, Perplexity,
    r"|npm_[A-Za-z0-9]{36,}|pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{20,}"                                # ElevenLabs, npm, PyPI,
    r"|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{30,}|do[por]_v1_[0-9a-f]{64}"                       # SendGrid, DigitalOcean,
    r"|sk\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}|[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}"            # Mapbox, Telegram,
    r"|[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}|SK[0-9a-f]{32}(?![A-Za-z0-9])"  # Discord, Twilio
    r"|(?:(?:[A-Z][A-Z0-9]*_)+(?:KEY|TOKEN|SECRET|PASSWORD|PAT)|aws_secret_access_key|aws_session_token)"
    r"[ \t]*[=:][ \t]*[\"']?(?=[^\s\"'<>]{0,300}[0-9])(?=[^\s\"'<>]{0,300}[A-Za-z])[^\s\"'<>]{12,300})"  # NAME_KEY=value
    r"|hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]{16,}"
    r"|-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----|(?:AccountKey|SharedAccessKey)=[A-Za-z0-9+/]{30,}"
    # user:password@, a link's user (empty in redis://:password@host, an e-mail address in smtp://bob@gmail.com:...@)
    # and password. The password runs to the last @ before a space or a /, so one with a raw ?, # or @, as password
    # managers draw them and people paste them, is caught whole (and redacted whole); with letters such as é, ñ or ж,
    # but nothing from U+2000 on (CJK, full-width punctuation, emoji), so Chinese or Japanese written on after a link
    # with no space is not read as one. A port (1 to 5 digits) followed by ?, #, /, @, " or an angle bracket (the end
    # of a link in JSON or <...>) is no password: http://10.0.0.5:8765?user=bob@example.com and
    # http://10.0.0.5:8765@evil.com pass, and so does a password that starts that way (2024?x...). A link right after
    # a quote (JSON, a Python or JavaScript string, a shell argument) ends at a quote of that kind followed by , : } or
    # ] (one that closes its string; an escaped one, \", doesn't): in minified JSON the user and password ran on into
    # the next values ({"homepage":"https://example.com","email":"robert.smith@example.com"} was refused)
    # (one alternative, q set after a quote: two scanned a long JSON text twice as slowly)
    r"|(?:(?<=(?P<q>[\"']))|(?<![A-Za-z0-9+.\-\"']))[A-Za-z][A-Za-z0-9+.-]{0,30}://"
    r"(?(q)(?:(?!(?<!\\)(?P=q)[,:}\]])[^\s/?#:\[\]]){0,200}|[^\s/?#:\[\]]{0,200})"
    r":(?!\d{1,5}[?#/@\"<>])"
    r"(?(q)(?:(?!(?<!\\)(?P=q)[,:}\]])[^\s/\x7f-\x9f\u2000-\U0010ffff]){8,200}"
    r"|[^\s/\x7f-\x9f\u2000-\U0010ffff]{8,200})@"
    r"|(?i:bearer)[ \t]+(?=[A-Za-z0-9._~+/-]{0,300}[0-9])[A-Za-z0-9._~+/-]{20,}")
# keys whose value is refused whatever its shape, once set: keys.KEYS but the project id, Claude's other credential and
# the access code a person chose for remote mode (the one a remote Studio makes itself is in ACCESS_CODES)
SAVED_KEYS = ("ANTHROPIC_API_KEY", "SUBQ_API_KEY", "KEEWANO_API_KEY", "KEEWANO_PAT", "ANTHROPIC_AUTH_TOKEN", "MORPH_TOKEN")
ACCESS_CODES: set = set()  # the running remote Studio's access code (remember_access_code)
_WORD = re.compile(r"[a-z]+|[A-Z][a-z]*|[A-Z]+")                                  # egg, Steel, DIN
_CAMEL = re.compile(r"(?:[a-z]{2,}|[A-Z][a-z]{2,})(?:[A-Z][a-z]{2,})+")         # shoulderJointTorqueLimit
_SAYABLE = re.compile(r"(?=.*[aeiouy])(?!.*[^aeiouy]{5})", re.I)               # a vowel, no 5 other letters in a row
_MODEL = re.compile(r"(?=[a-z0-9]*[a-z]{3})[a-z][a-z0-9]*|(?=[A-Z0-9]*[A-Z]{3})[A-Z][A-Z0-9]*")  # stm32f405, NEMA17
_RUNS = re.compile(r"[0-9]+|[A-Za-z]+")
_NUMBERED = re.compile(r"(?:[a-z]{3,}|[A-Z][a-z]{2,}|[A-Z]{3,})[0-9]+")  # arm2, joint3, Servo1: a word and its number
_PIECE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")                     # yoloV8: yolo V 8; GraspNet1B: Grasp Net 1 B
# a number with a unit (3000rpm, 400kHz, 3300mAh, 10kOhm, 10pcs, 720p), a size (1280x720, 42x42x48mm) or a time stamp
# (20241015T153000Z): one more number
_QUANTITY = re.compile(r"[0-9]+(?:x[0-9]+){0,3}(?:rpm|k?hz|[mg]hz|[km]?bps|baud|m?ah|m?wh|fps|deg|dps|bits?|bytes?|[kmg]b"
                       r"|[mk]?v|vdc|vac|[mu]?a|[mk]?w|[km]?ohms?|cpr|ppr|[nm]?nm|ncm|kgf?cm|kgf?|g|n|[cmu]?m|[mun]?s|sec"
                       r"|min|hr?|pcs|px|p|c|f|[unp]f|[um]h|dbm?|lux|psi|k?pa|oz|lbs?)?|[0-9]{8}t[0-9]{4,6}z?", re.I)
_RELEASE = re.compile(r"[A-Z]{2,6}v[0-9]{1,2}[a-z]?|[A-Z]{3,6}[0-9]{1,2}[a-z]")   # YOLOv8n, DINOv2, OWLv2, YOLO11n
_PART_NUMBER = re.compile(r"[A-Z][A-Z0-9]{5,15}|[a-z][a-z0-9]{5,15}")              # STM32F405RGT6, ch32v003f4p6


def _words(part: str) -> int:
    """How many words `part` is: 1 for egg, Steel or DIN, one per word for camelCase words (shoulderJointTorqueLimit),
    and one per piece for camelCase words with capitals (maxPWMFreq, useGPU, offsetX, widthMM, OpenVLA, PIDNet: an
    acronym of 2 to 5 capitals, or 1 at the end, and a common word, words.WORDS, among the words), 0 for anything else
    (a random run's xkolpvMlx is not camelCase words: they need a vowel each)."""
    if _WORD.fullmatch(part):
        return 1
    if _CAMEL.fullmatch(part):
        words = re.findall(r"[A-Z]?[a-z]+", part)
        return len(words) if all(_SAYABLE.match(w) for w in words) else 0
    pieces = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", part)
    if "".join(pieces) != part or not 2 <= len(pieces) <= 5:
        return 0
    words = [(i, q) for i, q in enumerate(pieces) if not q.isupper()]
    caps = [(i, q) for i, q in enumerate(pieces) if q.isupper()]
    if not words or len(caps) > 2 or not any(len(q) >= 3 for _, q in words):
        return 0
    if any(not (2 <= len(q) <= 5 or (len(q) == 1 and i == len(pieces) - 1)) for i, q in caps):
        return 0
    if any(not _SAYABLE.match(q) or len(q) < (2 if i == 0 else 3) for i, q in words):
        return 0
    from .words import WORDS
    if not any(len(q) >= 3 and q.lower() in WORDS for _, q in words):
        return 0  # a random run's case often splits like this (RFLdtoULObm): one word must be a known one
    return len(pieces)


def _versioned(part: str) -> bool:
    """True for a part mixing capitals, small letters and one number that reads as a name: a capitalized word and its
    number (Servo1), a word and a version (yoloV8, gripperV2), a word and capitals with a number (servoID3), camelCase
    words and a number (jointLimit2, GraspNet1B), a model and a word (so101Arm). In pieces (_PIECE): 2 to 4, a word
    first; one number of up to 3 digits; at most one run of capitals, next to the number: V or 2-3 capitals before it,
    or one capital after it when two words come first; every word sayable (_SAYABLE), the first 2 letters or more (3
    when it is all but the number), the others capitalized and 3 or more. A key's random run rarely is all that
    (pfuek3Ly, umcoithJ11 and Xq3Lp9 are not)."""
    if not (part.isalnum() and re.search(r"[A-Z]", part) and re.search(r"[a-z]", part) and re.search(r"[0-9]", part)):
        return False
    pieces = _PIECE.findall(part)
    if "".join(pieces) != part or not 2 <= len(pieces) <= 4 or not pieces[0][-1].islower():
        return False
    nums = [i for i, q in enumerate(pieces) if q.isdigit()]
    caps = [i for i, q in enumerate(pieces) if q.isupper()]
    if len(nums) != 1 or len(pieces[nums[0]]) > 3 or len(caps) > 1:
        return False
    n = nums[0]
    words = [(i, q) for i, q in enumerate(pieces) if q[-1].islower()]
    for i in caps:
        if not ((i == n - 1 and (pieces[i] == "V" or 2 <= len(pieces[i]) <= 3))
                or (i == n + 1 and len(pieces[i]) == 1 and sum(1 for j, _ in words if j < n) >= 2)):
            return False
    first = 3 if len(pieces) == 2 else 2
    return all(_SAYABLE.match(q) and (len(q) >= first if i == 0 else (len(q) >= 3 and q[0].isupper())) for i, q in words)


def _series(a: str, b: str) -> bool:
    """True when `b` follows `a` in a numbered series: one length, and only digits changed or one character changed
    (servo1 servo2, m3x12 m4x12, xl430 xm430). Random groups of a key almost never do."""
    if len(a) != len(b) or a == b:
        return False
    changed = [(x, y) for x, y in zip(a, b) if x != y]
    return len(changed) == 1 or all(x.isdigit() and y.isdigit() for x, y in changed)


def _kinds(parts: list, code: bool = False) -> list:
    """What each part of a run split on - and _ reads as (see _phrase): "word" (with how many words it is), "number",
    "name", "code", or None for a random run. `code`: the run of an mi_/ml_/mt_ name, where a whole part number of one
    case and up to 8 runs of letters and digits (STM32F405RGT6, CH32V003F4P6) is a name too: every Teams code is drawn
    again unless it has a random part (auth.new_code), so this costs no code; after sk- it would hide the random part
    of one key in 2 behind two words (sk-live-test-K3F9ABQX77DE)."""
    in_series = set()
    for i in range(len(parts) - 2):
        if _series(parts[i], parts[i + 1]) and _series(parts[i + 1], parts[i + 2]):
            in_series.update((i, i + 1, i + 2))
    out = []
    for i, p in enumerate(parts):
        n = _words(p)
        if n and (_WORD.fullmatch(p) or _CAMEL.fullmatch(p)):
            out.append(("word", n))
        elif p.isdigit():
            out.append(("number", 1))
        elif _MODEL.fullmatch(p) and len(_RUNS.findall(p)) <= 4:
            out.append(("name", 1))
        elif len(p) <= 7 and p.isalnum() and not re.search(r"[A-Za-z]{3}", p):
            out.append(("name" if i in in_series else "code", 1))  # xl330 in xl330_xl430_xm430, m3x12 in a series
        elif _versioned(p):
            out.append(("versioned", 1))  # yoloV8, jointLimit2, so101Arm
        elif _QUANTITY.fullmatch(p):
            out.append(("number", 1))  # 3000rpm, 1280x720
        elif n:
            out.append(("word", n))  # maxPWMFreq, offsetX, SmolVLA
        elif _RELEASE.fullmatch(p) or (code and _PART_NUMBER.fullmatch(p) and len(_RUNS.findall(p)) <= 8):
            out.append(("name", 1))  # YOLOv8n; under mt_ a whole part number: STM32F405RGT6, SAMD21G18A
        else:
            out.append(None)  # a random run
        if out[-1] is not None and i in in_series:
            out[-1] = ("series", 1) if out[-1][0] != "word" else out[-1]
    return out


def _grouped(parts: list, kinds: list) -> bool:
    """A key split into groups of one size (4f8a-91bc-77de-12ab, 1234-5678-90ab-cdef): three parts in a row of one
    length (3 or more), one of them mixing letters and digits other than a numbered word (_NUMBERED: arm2, joint3), a
    versioned name or a part of a series, and none of them a common word (words.WORDS: horn and trim in
    mt_SG90_horn_trim_offset)."""
    from .words import WORDS
    keyish = [(k is None or k[0] not in ("series", "versioned")) and bool(re.search(r"[0-9]", p))
              and bool(re.search(r"[A-Za-z]", p)) and not _NUMBERED.fullmatch(p) for p, k in zip(parts, kinds)]
    return any(len(parts[i]) >= 3 and len({len(q) for q in parts[i:i + 3]}) == 1 and any(keyish[i:i + 3])
               and not any(q.lower() in WORDS for q in parts[i:i + 3]) for i in range(len(parts) - 2))


def _phrase(body: str, code: bool = False) -> bool:
    """True when the run after a key's prefix reads as words and part numbers, not a key's random run. Split on - and _,
    every part must be one of:
    - a word: egg, Steel, DIN, camelCase words (shoulderJointTorqueLimit counts as 4), or camelCase words with an
      acronym or an axis (maxPWMFreq, useGPU, offsetX, SmolVLA: one per piece);
    - a number: 2020, or with a unit or as a size (3000rpm, 400kHz, 3300mAh, 1280x720, 42x42x48mm) or a time stamp;
    - a model or part name: one case, starting with a letter, with a run of 3 letters and at most 4 runs of letters
      and digits (stm32f405, esp32s3, resnet50, din912, NEMA17), or at most 8 for a whole part number
      (STM32F405RGT6), or a model and its version (YOLOv8n, DINOv2);
    - a short code: at most 7 letters and digits mixed, with no run of 3 letters (6x40, m3x12, v2, 500mm, 42x40mm,
      1N4148), which is also what a key's random groups look like (4f8a, 91bc);
    - a versioned name (_versioned: Servo1, yoloV8, jointLimit2, servoID3, so101Arm, GraspNet1B), counted as a name.
    Three or more parts in a row that make a series (_series: servo1_servo2_servo3, xl330_xl430_xm430) count as names.
    A phrase has 3 or more words and parts, one a word of 4 letters or more, and at most one short code more than it
    has words and names; and it is not a key split into groups of one size (_grouped). So left_knee_arm2,
    joint1_joint2_joint3, baseV7_camera_torque and mt_SG90_horn_trim_offset pass.
    The price, measured by tools/key_filter_rates.py (100,000 random keys per shape), is in RELEASE_CONTRACTS.md
    section 9 (Keys), with the names it refuses."""
    parts = [p for p in re.split(r"[-_]+", body) if p]
    kinds = _kinds(parts, code)
    if any(k is None for k in kinds):
        return False  # a random run
    words = sum(n for k, n in kinds if k == "word")
    names = sum(1 for k, _ in kinds if k in ("name", "series", "versioned"))
    codes = sum(1 for k, _ in kinds if k == "code")
    numbers = sum(1 for k, _ in kinds if k == "number")
    long_word = any(k[0] == "word" and len(p) >= 4 for p, k in zip(parts, kinds))
    return (words + names + codes + numbers >= 3 and long_word and codes <= words + names + 1
            and not _grouped(parts, kinds))


def random_part(body: str) -> bool:
    """True when a part of `body` (split on - and _) reads as no word, number, name or code: a run that has one is a key
    or a code whatever text is joined to it (teams.auth.new_code draws again a code without one)."""
    return any(k is None for k in _kinds([p for p in re.split(r"[-_]+", body) if p], code=True))


def _is_key(match) -> bool:
    token = match.group(0)
    if token.startswith(("sk-ant-", "kwpat-")):
        return True
    body = token[3:]
    if token.startswith("sk-"):
        if not any(c.isdigit() for c in body):
            return False
        if len(body) < 20:  # too short to judge as a phrase: only a key in groups (sk-4f8a-91bc-77de-12ab)
            parts = [p for p in re.split(r"[-_]+", body) if p]
            return _grouped(parts, _kinds(parts))
        return not _phrase(body)
    # Morph's codes always have a capital (TEAM_CODE); a run as long as a Teams code is judged as any other:
    # auth.new_code draws again the rare code that reads as a phrase, so every code Morph makes is caught and
    # ml_policyV1_final_weights is a name
    return re.search(r"[A-Z]", body) is not None and not _phrase(body, code=True)


def has_key(text: str) -> bool:
    """True when `text` holds something shaped like a Claude, SubQ or Keewano key or a Morph Teams code (SECRET), or
    another service's key or key-bearing text (OTHER_KEYS)."""
    return isinstance(text, str) and (any(_is_key(m) for m in SECRET.finditer(text)) or bool(OTHER_KEYS.search(text)))


def redact_keys(text: str, mark: str = "[redacted]") -> str:
    """`text` with everything has_key() finds replaced by `mark`."""
    return OTHER_KEYS.sub(mark, SECRET.sub(lambda m: mark if _is_key(m) else m.group(0), text))


def remember_access_code(code) -> None:
    """A remote Studio's access code (server.serve): refused wherever people type, like a saved key. None: no code."""
    ACCESS_CODES.clear()
    if code:
        ACCESS_CODES.add(code)


def _saved_key_in(text: str, env=None) -> bool:
    env = os.environ if env is None else env
    values = [*((env.get(n) or "").strip() for n in SAVED_KEYS), *ACCESS_CODES]
    return any(len(v) >= 8 and v in text for v in values)


def secret_refused(where: str) -> str:
    """The refusal every free-text field gives when looks_like_secret() is true, e.g. where="in chat"."""
    return (f"That looks like an API key or a Morph access code. Keys go in the Keys dialog; never paste either {where}. "
            "This was not sent or saved.")


def project_name(value) -> str:
    """A project folder name: 1 to 40 lowercase letters, digits or hyphens. Raises ValueError."""
    if not isinstance(value, str) or not PROJECT_NAME.fullmatch(value) or value in WINDOWS_RESERVED:
        raise ValueError("Project names are 1 to 40 lowercase letters, digits or hyphens, starting with a letter or digit.")
    return value


def robot_source(value) -> str:
    """An arm.source id (a robot bundle folder): 1 to 48 lowercase letters, digits, _ or -. Raises ValueError."""
    if not isinstance(value, str) or not ROBOT_SOURCE.fullmatch(value) or value in WINDOWS_RESERVED:
        raise ValueError("A robot source is the name of a robot folder, such as so101 "
                         "(lowercase letters, digits, _ or -), never a path.")
    return value


def inside(root: str, path: str) -> bool:
    """True when `path` is `root` or below it, after resolving symlinks, '..' and case on both."""
    root, path = os.path.realpath(root), os.path.realpath(path)
    try:
        return os.path.normcase(os.path.commonpath([root, path])) == os.path.normcase(root)
    except ValueError:  # different drives on Windows
        return False


def contained_path(root: str, relative: str, decode: bool = True) -> str | None:
    """root/relative, or None unless every segment is a plain name (no '', '.', '..' or hidden '.x' segments,
    no backslashes, drive letters or NUL) and the canonical result stays strictly inside root.
    URL paths are percent-decoded once first (decode=False for names that never came from a URL)."""
    rel = unquote(relative) if decode and isinstance(relative, str) else relative
    if not isinstance(rel, str) or not rel or any(c in rel for c in "\\:\x00"):
        return None
    parts = rel.split("/")
    if any(not p or p.startswith(".") for p in parts):
        return None
    full = os.path.realpath(os.path.join(root, *parts))
    if not inside(root, full) or os.path.normcase(full) == os.path.normcase(os.path.realpath(root)):
        return None
    return full


def looks_like_secret(text: str) -> bool:
    """The one filter for free text people type (chat, New robot, Show Morph the job's cards, every Teams text field):
    a Claude, SubQ or Keewano key (by its shape, or the value of one saved in the Keys dialog), a Morph Teams script
    token, invite or sign-in code, the running remote Studio's access code (ACCESS_CODES) or a chosen MORPH_TOKEN,
    another service's key with a known prefix or text carrying a key (OTHER_KEYS), or a tool's key (toolbox: the value
    of a saved MORPH_TOOL_* key, a token with a manifest's key prefix that fits its pattern, a JWT). Ordinary names are
    not keys: see SECRET."""
    if not isinstance(text, str) or not text:
        return False
    if has_key(text) or _saved_key_in(text):
        return True
    from .toolbox import catalog
    return catalog.looks_like_tool_key(text)


def holds_secret(value) -> bool:
    """looks_like_secret() over every string in a request value (dict keys and values, lists, nested), so a key in a
    structured answer is caught too. The strings are checked as one text, a line each: no key pattern spans a line."""
    todo, texts = [value], []
    while todo:
        v = todo.pop()
        if isinstance(v, str):
            texts.append(v)
        elif isinstance(v, dict):
            todo.extend(v.keys())
            todo.extend(v.values())
        elif isinstance(v, (list, tuple)):
            todo.extend(v)
    return looks_like_secret("\n".join(texts))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # the redirect becomes an HTTPError: urllib would resend the key's header to the new address


_KEYED = urllib.request.build_opener(_NoRedirect)


def open_keyed(request: urllib.request.Request, timeout: float):
    """urlopen for a request that carries an API key (SubQ, Keewano): same proxies and TLS checks, but a redirect is
    refused (HTTPError 30x) instead of followed, so the key never goes to another server."""
    return _KEYED.open(request, timeout=timeout)


class BodyError(ValueError):
    """A request body the Studio refuses; `status` is the HTTP code, the message is safe to show."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def content_length(headers, max_body: int = MAX_BODY) -> int:
    """The declared body size, checked before a byte is read: 400 when missing-but-chunked or malformed,
    413 above max_body. No Content-Length means an empty body."""
    if headers.get("Transfer-Encoding"):
        raise BodyError(400, "Send the request body with a Content-Length header.")
    values = headers.get_all("Content-Length") or []
    if not values:
        return 0
    if len(values) > 1 or not re.fullmatch(r"[0-9]{1,15}", values[0].strip()):
        raise BodyError(400, "Malformed Content-Length header.")
    n = int(values[0])
    if n > max_body:
        raise BodyError(413, f"The request body is larger than the {max_body // 1024} KiB limit.")
    return n


def _no_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def json_object(raw: bytes) -> dict:
    """A JSON object from a request body (empty body = {}); NaN/Infinity, arrays and scalars are refused."""
    if not raw:
        return {}
    try:
        body = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
    except (ValueError, UnicodeError, RecursionError):
        body = None
    if not isinstance(body, dict):
        raise BodyError(400, "Send a JSON object.")
    return body


# What a design value can be at all: far wider than the editor's PARAMS ranges or what Morph can build, but the
# pipeline's work grows with these sizes (a 1 km egg means millions of gear teeth and hours of simulation), so a typo or
# a model's answer must never tie up the Studio. Sizes in mm, masses in g, forces in N, inclusive.
DESIGN_LIMITS = {
    "object.diameter_mm": (1, 250), "object.length_mm": (1, 300), "object.mass_g": (0, 100_000),
    "object.friction": (0.01, 5), "object.max_force_n": (0, 10_000),
    "finger.length_mm": (5, 300), "finger.thickness_mm": (0.5, 50), "finger.width_mm": (0.5, 100),
    "finger.pad_height_mm": (0.5, 300), "finger.pad_depth_mm": (0.1, 100), "finger.mount_hole_d_mm": (0.5, 50),
    "finger.mount_hole_spacing_mm": (0, 200), "gripper.force_n": (0, 10_000), "gripper.stroke_mm": (1, 1000),
    "fab.tool_d_mm": (0.1, 50), "fab.quantity": (1, 1_000_000), "electronics.supply_v": (0.5, 1000),
    "electronics.servo_v": (0, 1000),
    "electronics.pinion_radius_mm": (1, 200), "electronics.target_force_n": (0, 10_000), "arm.dof": (4, 6),
    "arm.reach_mm": (50, 2000), "arm.base_height_mm": (10, 1000), "arm.payload_margin": (0.1, 100),
    "task.pick_x_mm": (-2000, 2000), "task.pick_y_mm": (-2000, 2000), "task.place_x_mm": (-2000, 2000),
    "task.place_y_mm": (-2000, 2000), "task.cycle_time_s": (0.1, 3600), "build.quantity": (1, 1_000_000),
    "safety_factor": (0.1, 100),
}
MAX_TEXT = 500  # characters in a design's text value (names, extras, actuator overrides)
# text values with a fixed shape: world mode's room is named by id (never a path) and pinned by its hash
DESIGN_TEXT = {"world.id": re.compile(r"(?:[a-z0-9][a-z0-9-]{0,39})?"), "world.sha": re.compile(r"[0-9a-f]{0,64}")}


def design_values(flat: dict, defaults: dict, keys=None):
    """ValueError unless each value of `flat` (a Design.flat(); only `keys` when given) has its default's type, is
    finite and is inside DESIGN_LIMITS: for a design from outside, or one a chat or model edit is about to build."""
    for key in sorted(flat if keys is None else set(keys) & set(flat)):
        value, like = flat[key], defaults.get(key)
        if isinstance(like, str):
            if not isinstance(value, str) or len(value) > MAX_TEXT:
                raise ValueError(f"{key} must be text of at most {MAX_TEXT} characters.")
            if key in DESIGN_TEXT and not DESIGN_TEXT[key].fullmatch(value):
                raise ValueError(f"{key} must be a world id (lower-case letters, digits and -) or its hash, never a path.")
            continue
        if like is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} must be a finite number.")
        if isinstance(like, int) and value != int(value):
            raise ValueError(f"{key} must be a whole number.")
        lo, hi = DESIGN_LIMITS.get(key, (-math.inf, math.inf))
        if not lo <= value <= hi:
            num = lambda x: f"{x:,.0f}" if x == int(x) and abs(x) < 1e15 else f"{x:g}"  # noqa: E731
            raise ValueError(f"{key} = {num(value)} is outside what Morph can work with ({num(lo)} to {num(hi)}).")


def param_changes(changes, params) -> dict:
    """Parameter-editor changes: a non-empty dict of known PARAMS keys with in-range scalar values.
    `params` is pipeline2.PARAMS: (key, label, group, "select" | "number", {options | min, max, unit})."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Send the changes as {parameter: value}.")
    schema = {key: (label, typ, extra) for key, label, _, typ, extra in params}
    out = {}
    for key, value in changes.items():
        if key not in schema:
            raise ValueError(f"{key} is not an editable parameter.")
        label, typ, extra = schema[key]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)) or (isinstance(value, str) and len(value) > 200):
            raise ValueError(f"{label}: send a single number or option.")
        if typ == "select":
            match = next((o for o in extra.get("options", []) if value == o or str(value) == str(o)), None)
            if match is None:
                raise ValueError(f"{label}: choose one of " + ", ".join(str(o) or "(none)" for o in extra.get("options", [])) + ".")
            out[key] = match
            continue
        try:
            num = float(value)
        except ValueError:
            raise ValueError(f"{label}: send a number.") from None
        if not math.isfinite(num):
            raise ValueError(f"{label}: send a finite number.")
        lo, hi = extra.get("min", -math.inf), extra.get("max", math.inf)
        if not lo <= num <= hi:
            raise ValueError(f"{label} must be between {lo:g} and {hi:g}{(' ' + extra['unit']) if extra.get('unit') else ''}.")
        out[key] = int(num) if isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip())) else num
    return out
