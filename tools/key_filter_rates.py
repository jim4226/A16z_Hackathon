"""Measure the key filter (morph.studio.security) on random keys and on plausible names.

    python tools/key_filter_rates.py            # the figures RELEASE_CONTRACTS.md section 9 (Keys) quotes
    python tools/key_filter_rates.py --quick    # a tenth of the samples

Keys: the share of random keys of each shape that has_key() misses. "with a digit" counts the misses of the phrase
rule (security._phrase: the key read as words and part numbers); "all" adds the keys with no digit at all, which the
sk- rule never takes for keys. Names: how many plausible names under mt_/ml_/mi_ looks_like_secret() refuses. Every
draw is seeded, so a run gives the same figures until the filter changes. Keys saved in the Keys dialog are ignored
(their environment variables are cleared first).
"""

from __future__ import annotations

import base64
import os
import random
import string
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _k in ("ANTHROPIC_API_KEY", "SUBQ_API_KEY", "KEEWANO_API_KEY", "KEEWANO_PAT"):
    os.environ.pop(_k, None)

from morph.studio import security  # noqa: E402

LOWDIG = string.ascii_lowercase + string.digits
UPDIG = string.ascii_uppercase + string.digits
HEX = "0123456789abcdef"
ALNUM = string.ascii_letters + string.digits
B64URL = ALNUM + "-_"

GROUPED = [("lower-case and digits", LOWDIG, g, n) for g, n in ((5, 3), (4, 4), (6, 3), (4, 5), (5, 4), (6, 4), (8, 3))]
GROUPED += [("capitals and digits", UPDIG, 5, 3), ("capitals and digits", UPDIG, 4, 4),
            ("hex", HEX, 5, 3), ("hex", HEX, 4, 4), ("hex", HEX, 4, 5),
            ("mixed-case letters and digits", ALNUM, 5, 3), ("mixed-case letters and digits", ALNUM, 4, 4)]

WORDS = ("joint servo motor link wrist elbow shoulder gripper finger camera cam axis layer encoder sensor torque limit "
         "gain offset speed current temp force threshold weights model config calib pose home max min left right base "
         "arm leg hip knee ankle").split()
CAMEL_WORDS = ("joint servo motor link wrist elbow shoulder gripper finger camera axis layer encoder sensor torque limit "
               "gain offset speed current force threshold weights model config calib pose home left right base arm knee "
               "policy detector backbone").split()
ABBR = "pwm rgb ctrl cfg msg pkt hdr lcd tft srv mtr jnt lnk cmd pid imu gps adc dac spi usb led cam can fps rpm".split()


def keys(n: int):
    rnd = random.Random(41)

    def run(alphabet, length):
        return "".join(rnd.choice(alphabet) for _ in range(length))

    def rate(make, count):
        digit = none = 0
        for _ in range(count):
            key = make()
            if not security.has_key(key):
                if any(c.isdigit() for c in key[3:]):
                    digit += 1
                else:
                    none += 1
        return f"with a digit {digit / count * 100:.3f}%   all {(digit + none) / count * 100:.3f}%   (of {count:,})"

    for kind, alphabet, g, k in GROUPED:
        shape = "sk-live-" + "-".join("x" * g for _ in range(k))
        print(f"{shape:38} {kind:30} {rate(lambda: 'sk-live-' + '-'.join(run(alphabet, g) for _ in range(k)), n)}")
    for length in (20, 24, 32):
        print(f"{'sk-' + 'x' * length:38} {'base64url':30} {rate(lambda: 'sk-' + run(B64URL, length), 2 * n)}")
    for prefix, nbytes, count in (("mi_", 16, 10 * n), ("mt_", 32, 2 * n)):
        missed = drawn = lower = 0
        while drawn < count:  # what secrets.token_urlsafe(nbytes) makes, from the seeded generator
            code = prefix + base64.urlsafe_b64encode(rnd.randbytes(nbytes)).rstrip(b"=").decode()
            if not security.TEAM_CODE.fullmatch(code):
                lower += 1  # no capital: auth.new_code draws again
                continue
            drawn += 1
            missed += not security.has_key(code)
        print(f"{prefix} + {nbytes} random bytes in base64url (a Teams code before auth.new_code's redraw): "
              f"{lower:,} of {drawn + lower:,} have no capital; {missed:,} of the {drawn:,} with one not caught")


def names(scale: int):
    refuse = security.looks_like_secret

    def count(label, make, tries, seed):
        rnd = random.Random(seed)
        total = refused = 0
        for _ in range(tries):
            name = rnd.choice(("mt_", "ml_", "mi_")) + "_".join(make(rnd))
            if len(name) < 23:  # shorter runs are never taken for keys
                continue
            total += 1
            refused += refuse(name)
        print(f"{label:62} refused {refused:,} of {total:,}")

    def series(rnd, n):
        w = rnd.choice(WORDS)
        return [f"{w}{i}" for i in range(1, n + 1)]

    def indexed(rnd):
        return f"{rnd.choice(WORDS)}{rnd.randint(1, 9)}"

    count("plain words (shoulder_joint_torque_limit)", lambda r: r.sample(WORDS, r.randint(3, 5)), 4000 // scale, 1)
    count("a series of 3 and 2 words (servo1_servo2_servo3_gain_left)",
          lambda r: series(r, 3) + r.sample(WORDS, 2), 4000 // scale, 2)
    count("a series of 4 and a word (cam1_cam2_cam3_cam4_gain)", lambda r: series(r, 4) + r.sample(WORDS, 1),
          4000 // scale, 3)
    count("two numbered words and 2 words (hip2_knee5_gain_offset)",
          lambda r: [indexed(r) for _ in range(2)] + r.sample(WORDS, 2), 4000 // scale, 4)
    count("versioned (gripperV2_offset_home)",
          lambda r: [r.choice(CAMEL_WORDS) + "V" + str(r.randint(1, 9))] + r.sample(CAMEL_WORDS, 2), 3000 // scale, 5)
    count("camelCase and a number (jointLimit2_offset_home)",
          lambda r: [r.choice(CAMEL_WORDS) + r.choice(CAMEL_WORDS).capitalize() + str(r.randint(1, 9))]
          + r.sample(CAMEL_WORDS, 2), 3000 // scale, 6)
    count("three numbered abbreviations and a word (pwm1_rgb2_cmd3_gain)",
          lambda r: [f"{r.choice(ABBR)}{r.randint(1, 9)}" for _ in range(3)] + r.sample(WORDS, 1), 4000 // scale, 7)


if __name__ == "__main__":
    quick = "--quick" in sys.argv
    keys(10000 if quick else 100000)
    names(10 if quick else 1)
