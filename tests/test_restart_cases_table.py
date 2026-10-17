"""Every failing case of the restart / two-Studios / Teams hunt (tests/fixtures/restart/hunt_cases.json, found with
real Studios) names the regression tests that reproduce it: a case whose tests are renamed or removed fails here."""

import importlib
import json
import os

import pytest

CASES = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "restart", "hunt_cases.json"),
                       encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_each_hunted_case_has_its_regression_tests(case):
    assert case["tests"], case["id"]
    for ref in case["tests"]:
        module, name = ref.split("::")
        assert callable(getattr(importlib.import_module(module), name, None)), f"{case['id']}: {ref} is gone"
