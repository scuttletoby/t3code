"""Runs one Python test file as `python3 <test> [args]` runs it, and counts each test its unittest runs ran
(fork-ci-tests.cjs runs every Python test through this, #664). Usage: python3 [-I] [-B] <this> <test> [args].

unittest's own summary (`Ran N tests ... (skipped=K)`) counts a setUpClass or setUpModule that raises SkipTest as one
skip and leaves that class's tests out of N, so a whole class could go unrun inside a budget of one. Here each test is
counted by its own outcome: before a run, every test the suite holds (its plan); after it, a test that was started and
then skipped (or failed as expected), or never started at all (a class or module skip, a failed setUpClass, a soft
stop half-way: failfast, shouldStop), is skipped, by name.

FORK_CI_TEST_COUNTS names the file to add two JSON lines to for each TextTestRunner run (unset or empty: none written):
{"plan": <the plan's size>} before it, and after it {"tests": <the plan's size>, "skipped": [<name> ...], "failed":
<failures, errors, unexpected successes>}. A hard stop (os._exit, a kill) leaves a plan without its counts, which the
runner counts as nothing run. A name is `Class.test` for a test of the file, `module.Class.test` for one of a module it
loads. FORK_CI_TEST_COUNTS is removed from the environment before the test starts, so no child can write there."""
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
    """A test's name: its id (`module.Class.test`), with `__main__.` dropped for a test of the file itself, so two
    modules' `T.test_x` never share a name."""
    return test_id[len("__main__."):] if test_id.startswith("__main__.") else test_id


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


def write(line):
    if COUNTS:
        with open(COUNTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")


def run(self, test):
    plan = [each.id() for each in leaves(test)]
    write({"plan": len(plan)})
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
    write(line)
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
