"""The suite's own network guard (tests/conftest.py): which test a thread belongs to, so that a connection from a
thread that outlived its test is refused with a warning and never fails the test running then."""

import threading

import conftest


def _start(name, target):
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    t.join(5)
    return t


def test_a_thread_one_of_morphs_background_threads_starts_belongs_to_that_threads_test():
    seen = {}

    def follow_up():  # a build's follow-up from a test that has ended starts its lookup pool now
        threading.current_thread()._morph_test = "tests/test_earlier.py::test_gone"
        seen["pool"] = _start("morph-lookup-0", lambda: None)._morph_test

    _start("morph-after-j1", follow_up)
    assert seen["pool"] == "tests/test_earlier.py::test_gone"


def test_a_thread_any_other_thread_starts_belongs_to_the_running_test():
    seen = {}

    def serve():  # a test server answering a request on a thread of its own
        threading.current_thread()._morph_test = "tests/test_earlier.py::test_gone"
        seen["request"] = _start("Thread-9 (process_request_thread)", lambda: None)._morph_test

    _start("serve-forever", serve)
    assert conftest.RUN["test"] is not None and seen["request"] == conftest.RUN["test"]
