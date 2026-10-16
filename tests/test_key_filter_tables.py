"""The key filter (security.looks_like_secret) against the release review's tables, kept as data in
tests/fixtures/key_filter: every key shape in keys.json is refused in every context it lists (a sentence, an .env line,
JSON, quotes, glued to Chinese, Japanese, Korean, Russian, Greek or French text with no space), and every name in
names.json passes or is refused as it says. A remote Studio's own access code and link are refused in its chat, and a
tool key with a manifest's prefix is found wherever it sits in the text."""

import json
import os
import random
import string

import pytest

from morph.studio import security, server
from morph.studio.toolbox import catalog

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "key_filter")
KEYS = json.load(open(os.path.join(DATA, "keys.json"), encoding="utf-8"))
ALPHABETS = {"alnum": string.ascii_letters + string.digits, "alpha": string.ascii_letters,
             "upper": string.ascii_uppercase, "lowdig": string.ascii_lowercase + string.digits, "hex": "0123456789abcdef",
             "digits": string.digits, "b64url": string.ascii_letters + string.digits + "-_",
             "base64": string.ascii_letters + string.digits + "+/", "base32": string.ascii_uppercase + "234567"}


def make(template: str, seed) -> str:
    """A key from its template: {alphabet:length} runs drawn from a seeded generator, the rest as written."""
    rnd = random.Random(seed)
    out, rest = [], template
    while "{" in rest:
        head, _, tail = rest.partition("{")
        spec, _, rest = tail.partition("}")
        name, _, n = spec.partition(":")
        out += [head, "".join(rnd.choice(ALPHABETS[name]) for _ in range(int(n)))]
    return "".join(out + [rest])


@pytest.fixture(autouse=True)
def no_saved_keys(monkeypatch, tmp_path):
    for k in [*security.SAVED_KEYS, *[k for k in os.environ if k.startswith("MORPH_TOOL_")]]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MORPH_HOME", str(tmp_path / "projects"))


ROWS = [(row["family"], i, row) for row in KEYS["keys"] for i in range(3)]


@pytest.mark.parametrize("family,i,row", ROWS, ids=[f"{f}-{i}" for f, i, _ in ROWS])
def test_every_key_shape_is_refused_in_every_context(family, i, row):
    key = make(row["make"], f"{family}-{i}")
    contexts = KEYS["contexts"] if row["caught"] == "everywhere" else {"env_line": KEYS["contexts"]["env_line"]}
    missed = [name for name, ctx in contexts.items() if not security.looks_like_secret(ctx.format(key))]
    assert missed == [], (key, missed)
    assert security.holds_secret({"answers": [{"name": "x"}, {"note": key if row["caught"] == "everywhere"
                                                               else contexts["env_line"].format(key)}]})
    if row["caught"] == "everywhere" and family != "jwt":  # the tool runner's redaction uses the same shapes
        assert key not in security.redact_keys(f"answer: {key} end"), key


@pytest.mark.parametrize("glue", ["我的密钥是", "キーは", "는", "а", "κλειδί", "clé", "是"])
def test_a_key_glued_to_a_non_ascii_letter_is_refused_and_redacted(glue):
    """Python's \\b counts 是, は, 는, а and é as word characters: a key right after them, with no space (how Chinese,
    Japanese and Korean are written), was never matched."""
    for key in ("sk-ant-api03-" + "Ab3_" * 10, "sk-Q7w8E9r0T1y2U3i4O5p6A7s8", "kwpat-ABCDEFGH0123456789",
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFU",
                "mi_A2ta3LO9jpvDtlvdcQ4Qvg"):
        assert security.looks_like_secret(glue + key), key
        assert security.holds_secret({"note": glue + key + "です"}), key
        if not key.startswith("eyJ"):
            assert key not in security.redact_keys(glue + key), key
    for fine in ("task-a1b2c3d4e5f6g7h8i9j0k1", "desk-a1b2c3d4e5f6g7h8i9j0k1", "risk-a1b2c3d4e5f6g7h8i9j0k1",
                 "my_sk-a1b2c3d4e5f6g7h8i9j0k1", "sk-6x40-stainless-steel-screws", "我的螺丝是sk-6x40-stainless-steel-screws"):
        assert not security.looks_like_secret(fine), fine


def test_a_remote_studios_access_code_and_link_are_refused(tmp_path, monkeypatch):
    """The access code a remote Studio prints (and MORPH_TOKEN, when a person chose it) is what the docs call a Morph
    access code: chat refuses it, raw or in the access link."""
    code = server.access_token("0.0.0.0")
    assert not security.looks_like_secret(f"open http://192.168.1.20:8765/?token={code} on the laptop")
    security.remember_access_code(code)
    for text in (code, f"open http://192.168.1.20:8765/?token={code} on the laptop", f"the code is {code}."):
        assert security.looks_like_secret(text), text
    security.remember_access_code(None)  # a local Studio has none
    monkeypatch.setenv("MORPH_TOKEN", "robotlab2024secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "authTok_9f8e7d6c5b4a")
    assert security.looks_like_secret("the access code is robotlab2024secret")
    assert security.looks_like_secret("use authTok_9f8e7d6c5b4a for Claude")
    assert not security.looks_like_secret("the access code is in the terminal")


def test_a_tool_key_with_a_manifests_prefix_is_found_anywhere_in_the_text(monkeypatch):
    fixtures = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "tools")
    monkeypatch.setattr(catalog, "CURATED_DIRS", [fixtures])
    key = "fxp_Q7w8E9r0T1y2U3i4O5p6A7s8"
    for text in (key, f"my key is {key}.", f"'{key}'", json.dumps({"k": key}), f"[{key}]", f"key: {key}",
                 f"Authorization: Token {key}", f"MORPH_TOOL_FIXPARTS_KEY={key}", f"key={key}", f"key:{key}",
                 f"https://api.fixparts.com/v1/offers?api_key={key}&mpn=STM32", f"--header=X-Api-Key:{key}",
                 f"我的密钥是{key}", f"キーは{key}です"):
        assert catalog.looks_like_tool_key(text), text
        assert security.looks_like_secret(text), text
    for fine in ("fxp_short", "part fxp_" + "a" * 10, "afxp_Q7w8E9r0T1y2U3i4O5p6A7s8", "fxp_ in the docs"):
        assert not catalog.looks_like_tool_key(fine), fine



def test_a_remote_studios_chat_refuses_its_own_access_link(tmp_path):
    from test_keys_and_launch import _start
    code = server.access_token("0.0.0.0")
    call, port, httpd = _start(tmp_path, token=code)
    try:
        signed_in = {"X-Morph-Token": code}
        for text in (f"open http://192.168.1.20:{port}/?token={code} on the laptop", f"the access code is {code}"):
            status, body, _ = call("POST", "/api/chat", {"message": text}, headers=signed_in)
            assert status == 400 and b"Keys dialog" in body, (status, body)
        status, _, _ = call("POST", "/api/chat", {"message": "make the gripper lighter"}, headers=signed_in)
        assert status == 202
    finally:
        httpd.shutdown()
        httpd.server_close()
