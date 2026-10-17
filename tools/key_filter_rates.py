"""Measure the key filter (morph.studio.security) on random keys, on Teams codes and on plausible names.

    python tools/key_filter_rates.py            # the figures RELEASE_CONTRACTS.md section 9 (Keys) quotes
    python tools/key_filter_rates.py --quick    # a tenth of the samples
    python tools/key_filter_rates.py --links    # only the links (a password in a link, an @ after a port)

Keys: the share of random keys of each shape that has_key() misses. "with a digit" counts the misses of the phrase
rule (security._phrase: the key read as words and part numbers); "all" adds the keys with no digit at all, which the
sk- rule never takes for keys. Codes: how often a Teams code drawn as secrets.token_urlsafe draws it is drawn again by
auth.new_code, and whether the codes it keeps are caught cut short or with text joined to them. Names: how many
plausible names looks_like_secret() refuses, under mt_/ml_/mi_ (which only judge a run with a capital: every Teams code
has one) and as sk- part numbers. Links: how many with an @ after their host and port are refused as links with a
password, and how many with a password are missed. Every draw is seeded, so a run gives the same figures until the filter changes. Keys
saved in the Keys dialog are ignored (their environment variables are cleared first).
"""

from __future__ import annotations

import base64
import os
import random
import string
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _k in ("ANTHROPIC_API_KEY", "SUBQ_API_KEY", "KEEWANO_API_KEY", "KEEWANO_PAT", "ANTHROPIC_AUTH_TOKEN", "MORPH_TOKEN"):
    os.environ.pop(_k, None)
for _k in [k for k in os.environ if k.startswith("MORPH_TOOL_")]:
    os.environ.pop(_k, None)

from morph.studio import security  # noqa: E402
from morph.studio.words import WORDS as KNOWN  # noqa: E402

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
# the release review's part codes and firmware words (a part code next to words of its length reads like a key's groups)
CODES = ["SG90", "M3x8", "M2x6", "6x40", "XL330", "xl330", "M3x12", "M4x16", "AS5600", "RP2040", "MG996R", "CH340G",
         "608zz", "1N4148", "A4988", "TMC2209", "DRV8825", "MPU6050", "BNO055", "INA219", "HX711", "L298N", "MX28",
         "AX12A", "ST3215", "STS3215", "XM430", "MR85", "esp32", "ESP32", "stm32", "GT2", "M5x20", "2020", "4040"]
VOCAB = ("arm base gain horn trim limit speed angle servo motor joint wrist elbow torque offset sensor driver weights "
         "config enable timeout current voltage encoder gripper shoulder position velocity default maximum minimum "
         "home pose step rate mode zero tune calib value bias scale range").split()
UNITS = ["rpm", "kbps", "Mbps", "kHz", "MHz", "GHz", "mAh", "fps", "deg", "bit", "Vdc", "kOhm", "ohm", "cpr", "ppr",
         "kgcm", "Ncm", "sec", "min", "pcs", "baud", "dps", "Wh", "mm", "Hz", "ms", "V", "A", "N", "kg"]
SIZES = ["640x480", "1280x720", "1920x1080", "3840x2160", "224x224", "42x42x48mm", "200x300x5mm"]
MODELS = ["YOLOv8n", "YOLOv8s", "YOLOv5s", "YOLO11n", "YOLOv10n", "ViT", "UNet", "DINOv2", "OWLv2", "SmolVLA", "SmolVLM",
          "OpenVLA", "GroundingDINO", "MiDaS", "LoFTR", "ConvNeXt", "NeRF", "MobileViT", "DeiT", "EfficientNetB0",
          "EfficientDet", "PointNet2", "ResNet50", "MobileNetV3", "DepthAnythingV2", "RTMDet", "CenterNet", "FCOS",
          "DETR", "SegFormer", "BiSeNetV2", "PIDNet", "SAM2", "CLIP", "SigLIP", "PaliGemma", "OctoBase", "pi0FAST",
          "GR00T", "RDT1B", "HPT", "VC1", "R3M", "MVP", "DiffusionPolicy", "ACT", "VQBeT", "TDMPC2", "DreamerV3"]
MPNS = ["STM32F405RGT6", "STM32F103C8T6", "STM32G431CBU6", "STM32H743VIT6", "STM32F411CEU6", "ATmega328P", "ATmega2560",
        "ATSAMD21G18A", "SAMD21G18A", "nRF52840", "nRF52832", "ESP32-S3-WROOM-1-N16R8", "ESP32-C3-MINI-1", "RP2040",
        "PIC16F877A", "LPC1768", "GD32F303CCT6", "CH32V003F4P6", "TB6612FNG", "DRV8825", "TMC2209", "A4988", "L298N",
        "SN65HVD230DR", "MCP2515", "TJA1050", "MAX485ESA", "XC6206P332MR", "AMS1117", "LM2596S", "MP1584EN",
        "MPU6050", "ICM20948", "BNO055", "BNO085", "VL53L0X", "VL53L1X", "AS5600", "AS5048A", "INA219", "INA226",
        "HX711", "ADS1115IDGSR", "PCA9685PW", "CP2102N", "CH340G", "FT232RL", "W25Q128JVSIQ", "AT24C256",
        "STS3215", "SCS0009", "XL330-M288-T", "XM430-W350-T", "XC430-W150-T", "MX-28AR", "MG996R", "SG90", "DS3218MG",
        "NEMA17", "17HS4401", "42BYGH48", "GT2", "MR85ZZ", "608ZZ", "6205-2RS", "LM8UU", "SK6812MINI-E", "WS2812B-2020"]
ACRONYMS = ["GPU", "IMU", "ADC", "PWM", "CAN", "USB", "DMA", "SPI", "I2C", "LED", "RPM", "ONNX", "UART", "ID"]


def run(rnd, alphabet, length):
    return "".join(rnd.choice(alphabet) for _ in range(length))


def keys(n: int):
    rnd = random.Random(41)

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
        print(f"{shape:38} {kind:30} {rate(lambda: 'sk-live-' + '-'.join(run(rnd, alphabet, g) for _ in range(k)), n)}")
    for kind, alphabet, g, k in (("lower-case and digits", LOWDIG, 5, 3), ("lower-case and digits", LOWDIG, 4, 4),
                                 ("hex", HEX, 4, 4)):  # no word in front: under 20 characters, judged as groups only
        shape = "sk-" + "-".join("x" * g for _ in range(k))
        print(f"{shape:38} {kind:30} {rate(lambda: 'sk-' + '-'.join(run(rnd, alphabet, g) for _ in range(k)), n)}")
    for length in (20, 24, 32):
        print(f"{'sk-' + 'x' * length:38} {'base64url':30} {rate(lambda: 'sk-' + run(rnd, B64URL, length), 2 * n)}")
    for kind, alphabet in (("capitals and digits", UPDIG), ("lower-case and digits", LOWDIG)):
        shape = "sk-<word>-<word>-xxxxxxxxxxxx"
        print(f"{shape:38} {kind:30} "
              f"{rate(lambda: 'sk-' + '-'.join(rnd.sample(VOCAB, 2)) + '-' + run(rnd, alphabet, 12), n)}")


def codes(n: int):
    rnd = random.Random(42)
    for prefix, nbytes, count in (("mi_", 16, 10 * n), ("mt_", 32, 2 * n)):
        lower = phrase = no_random = kept = 0
        missed = {"cut to 20 characters": 0, "-thanks": 0, "_backup_copy": 0, "-please-join-the-team": 0,
                  "-v2": 0, "_old": 0}
        while kept < count:  # what secrets.token_urlsafe(nbytes) makes, from the seeded generator
            code = prefix + base64.urlsafe_b64encode(rnd.randbytes(nbytes)).rstrip(b"=").decode()
            if not security.TEAM_CODE.fullmatch(code):
                lower += 1  # no capital: auth.new_code draws again
            elif not all(security.has_key(code[:n]) for n in range(23, len(code) + 1)):
                phrase += 1  # it, or a piece of it cut short, reads as a phrase
            elif not security.random_part(code[3:]):
                no_random += 1
            else:
                kept += 1
                missed["cut to 20 characters"] += not security.has_key(code[:23])
                for tail in ("-thanks", "_backup_copy", "-please-join-the-team", "-v2", "_old"):
                    missed[tail] += not security.has_key(code + tail)
        drawn = lower + phrase + no_random + kept
        print(f"{prefix} + {nbytes} random bytes in base64url, {drawn:,} drawn: auth.new_code draws again {lower:,} with no "
              f"capital, {phrase:,} that (or a piece of which cut short) read as a phrase and {no_random:,} with no "
              f"random part; of the {kept:,} kept, "
              + ", ".join(f"{v:,} missed {k}" for k, v in missed.items()))


def names(scale: int):
    refuse = security.looks_like_secret

    def count(label, make, tries, seed, prefixes=("mt_", "ml_", "mi_"), sep="_"):
        rnd = random.Random(seed)
        total = refused = 0
        for _ in range(tries):
            name = rnd.choice(prefixes) + sep.join(make(rnd))
            if len(name) < 23:  # shorter runs are never taken for keys
                continue
            total += 1
            refused += refuse(name)
        print(f"{label:76} refused {refused:,} of {total:,}")

    def series(rnd, n):
        w = rnd.choice(WORDS)
        return [f"{w}{i}" for i in range(1, n + 1)]

    def indexed(rnd):
        return f"{rnd.choice(WORDS)}{rnd.randint(1, 9)}"

    def cap(words):  # Servo1, Gain; an abbreviation all in capitals: PWM1
        return [w.upper() if w.rstrip("0123456789") in ABBR else w[:1].upper() + w[1:] for w in words]

    sets = [("plain words (shoulder_joint_torque_limit)", lambda r: r.sample(WORDS, r.randint(3, 5)), 4000),
            ("a series of 3 and 2 words (servo1_servo2_servo3_gain_left)", lambda r: series(r, 3) + r.sample(WORDS, 2), 4000),
            ("a series of 4 and a word (cam1_cam2_cam3_cam4_gain)", lambda r: series(r, 4) + r.sample(WORDS, 1), 4000),
            ("two numbered words and 2 words (hip2_knee5_gain_offset)",
             lambda r: [indexed(r) for _ in range(2)] + r.sample(WORDS, 2), 4000),
            ("three numbered abbreviations and a word (pwm1_rgb2_cmd3_gain)",
             lambda r: [f"{r.choice(ABBR)}{r.randint(1, 9)}" for _ in range(3)] + r.sample(WORDS, 1), 4000)]
    for i, (label, make, tries) in enumerate(sets):
        count(label + ", lower-case under mt_/ml_/mi_", make, tries // scale, i + 1)
        count(label + ", capitalized (Servo1_Servo2, PWM1)", lambda r, make=make: cap(make(r)), tries // scale, i + 1)
        count(label + ", as sk- part numbers", make, tries // scale, i + 1, prefixes=("sk-",), sep="-")
    count("versioned (gripperV2_offset_home)",
          lambda r: [r.choice(CAMEL_WORDS) + "V" + str(r.randint(1, 9))] + r.sample(CAMEL_WORDS, 2), 3000 // scale, 6)
    count("camelCase and a number (jointLimit2_offset_home)",
          lambda r: [r.choice(CAMEL_WORDS) + r.choice(CAMEL_WORDS).capitalize() + str(r.randint(1, 9))]
          + r.sample(CAMEL_WORDS, 2), 3000 // scale, 7)

    def coded(rnd, vocab):  # a part code among 2 or 3 words: mt_SG90_horn_trim_offset
        ws = rnd.sample(vocab, rnd.choice([2, 3]))
        pos = rnd.randrange(len(ws) + 1)
        return ws[:pos] + [rnd.choice(CODES)] + ws[pos:]
    syl = random.Random(8)  # sayable words of 4 to 6 letters that aren't in words.WORDS: bakar, tosin, vemu
    made_up = sorted({"".join(syl.choice(c) for c in ("bdfgklmnprstvz", "aeiou") * 3)[:syl.randint(4, 6)]
                      for _ in range(200)} - KNOWN)[:60]
    count("a part code and 2 or 3 firmware words (mt_SG90_horn_trim_offset)", lambda r: coded(r, VOCAB), 20000 // scale, 9)
    count("the same with made-up words Morph doesn't know (mt_SG90_bakar_tosin)",
          lambda r: coded(r, made_up), 20000 // scale, 10)
    count("a number and a unit (mt_motor_max_speed_3000rpm, mt_i2c_clock_400kHz)",
          lambda r: r.sample(WORDS, 2) + [f"{r.choice(['5', '12', '30', '400', '3000', '115200'])}{r.choice(UNITS)}"],
          4000 // scale, 11)
    count("a size (mi_camera_intrinsics_1280x720)", lambda r: r.sample(WORDS, 2) + [r.choice(SIZES)], 2000 // scale, 12)
    count("a commit hash of 7 to 12 hex digits (ml_policy_weights_3f9a2c1e)",
          lambda r: r.sample(WORDS, 2) + [run(r, HEX, r.randint(7, 12))], 4000 // scale, 13)
    count("camelCase with an acronym (mt_maxPWMFreq_default_value, ml_useGPU_flag)",
          lambda r: [r.choice(["use", "enable", "read", "set", "max", "init", "send", "get", "load", "run"])
                     + r.choice(ACRONYMS) + r.choice(["", "Freq", "Data", "Frame", "Model"])] + r.sample(WORDS, 2),
          4000 // scale, 14)
    count("a word and an axis or unit capital (mt_camera_offsetX_mm_value)",
          lambda r: [r.choice(WORDS), r.choice(["offset", "pos", "vel", "accel", "gyro", "rot", "center", "temp", "width"])
                     + r.choice(["X", "Y", "Z", "C", "MM"]), r.choice(WORDS)], 4000 // scale, 15)
    for label, items in (("model names as their authors write them (ml_YOLOv8n_grasp_detector)", MODELS),
                         ("whole part numbers (mt_STM32F405RGT6_firmware_build_config)", MPNS)):
        total = refused = 0
        for m in items:
            for name in (f"ml_{m}_grasp_detector", f"ml_{m}_backbone_weights", f"mt_{m}_firmware_build_config",
                         f"sk-{m}-breakout-board-module"):
                if len(name) >= 23:
                    total, refused = total + 1, refused + refuse(name)
        print(f"{label:76} refused {refused:,} of {total:,}")


def links(n: int):
    """Links that carry an e-mail address or an @ after their host and port (in a query, a fragment or a path, right
    after the port as in http://10.0.0.5:8765@evil.com, or in Chinese or Japanese text written on after the link with
    no space): how many the user:password@ rule refuses. Links with a user and a password: how many it misses, alone and
    in a sentence; passwords of 8 to 24 characters RFC 3986 allows there (letters, digits, -._~!$&'()*+,;= and
    %-escapes such as %40), also with an empty user (redis://:password@host) or an e-mail address as the user; and
    passwords of 12 to 20 characters drawn as password managers draw them, with at least one symbol of a set, raw
    (RFC 3986 says to %-escape ?, #, @ and /, but people paste them as they are)."""
    rnd = random.Random(43)

    def host():
        kind = rnd.randrange(5)
        if kind == 0:
            return ".".join(str(rnd.randint(1, 254)) for _ in range(4))
        if kind == 1:
            return rnd.choice(["localhost", "robot.local", "studio.lab.example.com", "pi4", "jetson-nano.lan"])
        return rnd.choice([f"[2001:db8::{rnd.randint(1, 0xffff):x}]", "[::1]", f"[fe80::{rnd.randint(1, 0xffff):x}]"])

    port = lambda: rnd.choice(["", f":{rnd.randint(1, 65535)}", ":8765", ":443", ":554"])  # noqa: E731
    schemes = ["http", "https", "rtsp", "mqtt", "ws", "ftp", "postgres", "mysql", "redis", "amqp", "ssh", "mongodb+srv"]
    mails = ["bob@example.com", "ops@lab.example.org", "jim.k@robotics.example.edu", "a@b.co", "@team"]

    def refused(label, make):
        count = sum(security.looks_like_secret(make()) for _ in range(n))
        print(f"{label:76} refused {count:,} of {n:,}")

    tails = ["?user={}", "?project=egg-picker&owner={}", "#contact={}", "/?share={}", "/people/{}", "?next=/home#{}"]
    refused("links with an @ after the host (http://10.0.0.5:8765?user=bob@example.com)",
            lambda: f"{rnd.choice(schemes[:5])}://{host()}{port()}{rnd.choice(tails).format(rnd.choice(mails))}")
    after_port = ["@evil.com", "@evil.com/", "@evil.com?user={}", "@evil.com#{}", "@{}"]
    refused("links with an @ right after the port (http://10.0.0.5:8765@evil.com)",
            lambda: f"{rnd.choice(schemes[:5])}://{host()}:{rnd.randint(1, 65535)}"
                    f"{rnd.choice(after_port).format(rnd.choice(mails))}")
    cjk = ["，有问题请发邮件到{}", "。联系{}", "です。連絡先は{}", "、メールは{}まで", "，电话:13800138000，邮箱{}"]
    refused("a link, then Chinese or Japanese and an e-mail address with no space",
            lambda: f"我的Studio在{rnd.choice(schemes[:5])}://{host()}{port()}{rnd.choice(cjk).format(rnd.choice(mails))}")

    def missed(label, user, password):
        alone = in_text = 0
        for _ in range(n):
            link = (f"{rnd.choice(schemes)}://{user()}:{password()}@{host()}{port()}"
                    + rnd.choice(["", "/morph", "/stream1", "/?x=1", "#top"]))
            alone += not security.looks_like_secret(link)
            in_text += not security.looks_like_secret(f"connect with {link} please")
        print(f"{label:76} missed {alone:,} of {n:,} alone, {in_text:,} in a sentence")

    users = lambda: rnd.choice(["admin", "root", "robot", "pi", "jim", "ops", "sensor",  # noqa: E731
                                run(rnd, LOWDIG, rnd.randint(3, 12))])
    allowed = ALNUM + "-._~!$&'()*+,;="
    rfc = lambda: "".join(rnd.choice(allowed) if rnd.random() > 0.05 else rnd.choice(["%40", "%23", "%3F", "%2F"])  # noqa: E731
                          for _ in range(rnd.randint(8, 24)))
    missed("links with a password of 8 to 24 characters (postgres://admin:...@db:5432)", users, rfc)
    missed("the same with an empty user (redis://:...@host:6379)", lambda: "", rfc)
    missed("the same with an e-mail address as the user (smtp://bob@gmail.com:...@)",
           lambda: rnd.choice(mails[:4]), rfc)

    def drawn(symbols):
        def make():
            chars = [rnd.choice(ALNUM + symbols) for _ in range(rnd.randint(11, 19))] + [rnd.choice(symbols)]
            rnd.shuffle(chars)
            return "".join(chars)
        return make

    for symbols, what in (("!@#$%^&*", "symbols !@#$%^&*"), ("!#$%&*+-.:;=?^_~", "symbols !#$%&*+-.:;=?^_~"),
                          ("?", "a raw ?"), ("#", "a raw #"), ("@", "a raw @"), ("/", "a raw /"),
                          ("éñüßж", "a letter outside ASCII (é, ñ, ü, ß, ж)")):
        missed(f"a password of 12 to 20 characters with {what}", users, drawn(symbols))


if __name__ == "__main__":
    quick = "--quick" in sys.argv
    if "--links" in sys.argv:  # only the links
        links(2000 if quick else 20000)
        sys.exit(0)
    keys(10000 if quick else 100000)
    codes(10000 if quick else 100000)
    names(10 if quick else 1)
    links(2000 if quick else 20000)
