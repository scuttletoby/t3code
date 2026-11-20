"""Runs one Python test file as `python3 <test> [args]` runs it, and counts each test its unittest runs ran
(fork-ci-tests.cjs runs every Python test through this, #664). Usage: python3 [-I] [-B] <this> <test> [args].

unittest's own summary (`Ran N tests ... (skipped=K)`) counts a setUpClass or setUpModule that raises SkipTest as one
skip and leaves that class's tests out of N, so a whole class could go unrun inside a budget of one. Here each test is
counted by its own outcome: before a run, every test the suite holds (its plan); after it, a test that was started and
then skipped (or failed as expected), or never started at all (a class or module skip, a failed setUpClass, a stop
half-way), is skipped, by name.

FORK_CI_TEST_COUNTS names the file to add one JSON line to for each TextTestRunner run (unset or empty: none written):
{"tests": <the plan's size>, "skipped": [<Class.test> ...], "failed": <failures, errors, unexpected successes>}.
It is removed from the environment before the test starts, so no process the test starts can write there."""
import json
import os
import runpy
import sys
import unittest

COUNTS = os.environ.pop("FORK_CI_TEST_COUNTS", "")


def leaves(test):
    """The tests a suite holds, in order (a suite drops each test once it ran, so this is read before)."""
    if isinstance(test, unittest.TestSuite):
        for each in test:
            yield from leaves(each)
    else:
        yield test


def name(test_id):
    """`Class.test` from a test's id (`__main__.Class.test`, or `module.Class.test` for a loaded module)."""
    return ".".join(test_id.split(".")[-2:])


def counted(test):
    """The test a result names: a subtest's own test; None for a class or module fixture's stand-in."""
    if type(test).__name__ == "_ErrorHolder":
        return None
    return getattr(test, "test_case", test)


STARTED = []
_start = unittest.TestResult.startTest


def start_test(self, test):
    STARTED.append(test.id())
    return _start(self, test)


unittest.TestResult.startTest = start_test
_run = unittest.TextTestRunner.run


def run(self, test):
    plan = [each.id() for each in leaves(test)]
    first = len(STARTED)
    result = _run(self, test)
    started = set(STARTED[first:])
    skipped = {each for each in plan if each not in started}
    for test_or_holder, _ in result.skipped + result.expectedFailures:
        test_case = counted(test_or_holder)
        if test_case is not None:
            skipped.add(test_case.id())
    line = {"tests": len(plan), "skipped": sorted(name(each) for each in set(plan) & skipped),
            "failed": len(result.failures) + len(result.errors) + len(result.unexpectedSuccesses)}
    if COUNTS:
        with open(COUNTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    return result


unittest.TextTestRunner.run = run


def main():
    if len(sys.argv) < 2:
        raise SystemExit(f"usage: python3 {sys.argv[0]} <test.py> [args]")
    test = sys.argv[1]
    # Run as `python3 <test>` would: its folder first on the path where this one's is (none under -I, -P).
    here = os.path.dirname(os.path.abspath(__file__))
    if sys.path and os.path.abspath(sys.path[0] or os.curdir) == here:
        sys.path[0] = os.path.dirname(os.path.abspath(test))
    sys.argv = sys.argv[1:]
    runpy.run_path(test, run_name="__main__")


if __name__ == "__main__":
    main()
