"""Runs one Python test file as `python3 <test> [args]` runs it, and counts each test its unittest runs ran
(fork-ci-tests.cjs runs every Python test through this, #664). Usage: python3 [-I] [-B] <this> <test> [args].

unittest's own summary (`Ran N tests ... (skipped=K)`) counts a setUpClass or setUpModule that raises SkipTest as one
skip and leaves that class's tests out of N, so a whole class could go unrun inside a budget of one. Here each test is
counted by its own outcome: before a run, every test the suite holds (its plan); after it, a test that was started and
then skipped (or failed as expected), or never started at all (a class or module skip, a failed setUpClass, a soft
stop half-way: failfast, shouldStop), is skipped, by name.

A test the file never loads (a class defined only when a tool is there, a load_tests that drops it) is in no plan, so
no outcome counts it. A second source names those: the test methods the file's own source defines in a class
(defined(): any branch of an if, try or with, so a class a condition leaves out is still named), each of which a run's
plan must hold (the class that defines it, through the test's class and its bases). At the file's end, one line names
the ones no plan held: the runner counts each as skipped, by name.

FORK_CI_TEST_COUNTS names the file to add JSON lines to (unset or empty: none written): for each TextTestRunner run
{"plan": <the plan's size>} before it, and after it {"tests": <the plan's size>, "skipped": [<name> ...], "failed":
<failures, errors, unexpected successes>}; when the file has ended (returned, raised, or sys.exit, unittest.main's
end), {"unloaded": [<Class.test> ...]}. A hard stop (os._exit, a kill) leaves a plan without its counts, or no end
line, either of which the runner counts as nothing run: so a hard stop before the first plan, while the file loads,
leaves no line at all. A name is `Class.test` for a test of the file, `module.Class.test` for one of a module it loads.
FORK_CI_TEST_COUNTS is removed from the environment before the test starts, so no child can write there."""
import ast
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


def defined(path):
    """The test methods (`test*`) the file's source defines in its classes, as `Class.test` (`Outer.Inner.test` for a
    nested class), in every branch of an if, try, with, for, while or match, in a class body too: a class or method
    that a condition leaves out is named all the same. None in a function (a class made at run time is not seen). None when the file cannot be
    parsed (it cannot run either)."""
    try:
        with open(path, "rb") as f:
            tree = ast.parse(f.read(), path)
    except (OSError, SyntaxError, ValueError):
        return None
    out = set()

    def walk(nodes, prefix, owner):
        """`owner`: the class whose body this is (a method under an if in it is its method too), else None."""
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if owner is not None and node.name.startswith("test"):
                    out.add(f"{owner}.{node.name}")
                continue
            if isinstance(node, ast.ClassDef):
                here = f"{prefix}{node.name}"
                walk(node.body, here + ".", here)
                continue
            for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                inner = getattr(node, field, None)
                if isinstance(inner, list):
                    walk([each for each in inner if isinstance(each, ast.AST)], prefix, owner)

    walk(tree.body, "", None)
    return out


def defining(test):
    """The `Class.test` of the file's source a planned test runs: its method, in the first of its class and bases
    that has it (a mixin's or base's test runs under each subclass), when that class is the file's (`__main__`)."""
    method = getattr(test, "_testMethodName", None)
    for klass in type(test).__mro__ if isinstance(method, str) else ():
        if method in vars(klass):
            return f"{klass.__qualname__}.{method}" if klass.__module__ == "__main__" else None
    return None


PLANNED = set()
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
    tests = list(leaves(test))
    PLANNED.update(defining(each) for each in tests)
    plan = [each.id() for each in tests]
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
    source = defined(test)
    try:
        runpy.run_path(test, run_name="__main__")
    finally:
        # Reached on a return, an exception and sys.exit (unittest.main), never on a hard stop. A file that cannot be
        # parsed writes nothing here (it cannot run, so it already fails).
        if source is not None:
            write({"unloaded": sorted(source - PLANNED)})


if __name__ == "__main__":
    main()
