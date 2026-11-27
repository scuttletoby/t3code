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
the ones no plan held and unittest would load (unloaded(): not a helper class's method, a property, a name set to
None, or a base's method every loaded subclass replaces): the runner counts each as skipped, by name.

FORK_CI_TEST_COUNTS names the file to add JSON lines to (unset or empty: none written): for each TextTestRunner run
{"plan": <the plan's size>} before it, and after it {"tests": <the plan's size>, "skipped": [<name> ...], "failed":
<failures, errors, unexpected successes>}; when the file has ended (returned, raised, or sys.exit, unittest.main's
end), {"unloaded": [<Class.test> ...]}. A hard stop (os._exit, a kill) leaves a plan without its counts, or no end
line, either of which the runner counts as nothing run: so a hard stop before the first plan, while the file loads,
leaves no line at all. A name is `Class.test` for a test of the file, `module.Class.test` for one of a module it loads.
FORK_CI_TEST_COUNTS is removed from the environment before the test starts, so no child can write there, and only this
process writes (a child made by os.fork keeps the shim, and writes nothing): a forked child's end line never stands in
for its parent's."""
import ast
import faulthandler
import json
import os
import runpy
import signal
import sys
import unittest

COUNTS = os.environ.pop("FORK_CI_TEST_COUNTS", "")
PID = os.getpid()


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


def dotted(node):
    """`A` or `A.B` for a name or an attribute of names, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = dotted(node.value)
        return head and f"{head}.{node.attr}"
    return None


def defined(path):
    """(names, bases): the tests (`test*`) the file's source puts in its classes, as `Class.test` (`Outer.Inner.test`
    for a nested class), and the last part of every class's bases' names. A test is a `def`, a name assigned in a
    class body (`test_x = make()`, `test_x = None`), or a `setattr(Class, "test_x", ...)` call outside a function;
    each in every branch of an if, try, with, for, while or match, in a class body too: a class or method that a
    condition leaves out is named all the same. None in a function: a class made there, a setattr there, or a
    setattr on a name that is not the class's (a loop variable) is not seen. unloaded() drops the names unittest would
    not load. None when the file cannot be parsed (it cannot run either)."""
    try:
        with open(path, "rb") as f:
            tree = ast.parse(f.read(), path)
    except (OSError, SyntaxError, ValueError):
        return None
    out = set()
    bases = set()

    def named(owner, target):
        if isinstance(target, (ast.Tuple, ast.List)):
            for each in target.elts:
                named(owner, each)
        elif isinstance(target, ast.Name) and target.id.startswith("test"):
            out.add(f"{owner}.{target.id}")

    def walk(nodes, prefix, owner):
        """`owner`: the class whose body this is (a method under an if in it is its method too), else None."""
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if owner is not None and node.name.startswith("test"):
                    out.add(f"{owner}.{node.name}")
                continue
            if isinstance(node, ast.ClassDef):
                here = f"{prefix}{node.name}"
                bases.update(base.rpartition(".")[2] for base in map(dotted, node.bases) if base)
                walk(node.body, here + ".", here)
                continue
            if owner is not None and isinstance(node, (ast.Assign, ast.AnnAssign)):
                if getattr(node, "value", None) is not None:
                    for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                        named(owner, target)
                continue
            call = node.value if isinstance(node, ast.Expr) else None
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "setattr"
                    and len(call.args) >= 2 and dotted(call.args[0])
                    and isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str)
                    and call.args[1].value.startswith("test")):
                out.add(f"{dotted(call.args[0])}.{call.args[1].value}")
                continue
            for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                inner = getattr(node, field, None)
                if isinstance(inner, list):
                    walk([each for each in inner if isinstance(each, ast.AST)], prefix, owner)

    walk(tree.body, "", None)
    return out, bases


def defining(test):
    """The `Class.test` of the file's source a planned test runs: its method, in the first of its class and bases
    that has it (a mixin's or base's test runs under each subclass), when that class is the file's (`__main__`)."""
    method = getattr(test, "_testMethodName", None)
    for klass in type(test).__mro__ if isinstance(method, str) else ():
        if method in vars(klass):
            return f"{klass.__qualname__}.{method}" if klass.__module__ == "__main__" else None
    return None


def first(klass, method):
    """The first of a class and its bases that has `method` itself (where unittest's getattr finds it), else None."""
    return next((each for each in klass.__mro__ if method in vars(each)), None)


def unloaded(names, bases):
    """The source's tests (defined()) that no run planned and that unittest would load all the same, so they did not
    run: their class or method is not there at the file's end (a condition left it out, or a del), a loaded class or the
    unittest.TestCase subclass it is did not load them (a load_tests, an own run of another class), or a class names
    their class as a base (a mixin of a class a condition left out). Dropped, as unittest never loads them: a method
    of a class that is no TestCase and no class's base (a helper), a name that is not callable on its class (a
    property, `test_x = None`), and a base's method that every loaded class with that base replaces. A file that
    started no run is not looked into (the runner counts no run as nothing run): every name is left."""
    left = names - PLANNED
    if not MAIN:
        return left
    module = MAIN[0]
    seen = {each.__qualname__: each for loaded in LOADED for each in loaded.__mro__
            if each.__module__ == "__main__"}
    out = set()
    for test in left:
        qualname, _, method = test.rpartition(".")
        klass = seen.get(qualname)
        if klass is None:
            klass = module
            for part in qualname.split("."):
                klass = getattr(klass, part, None)
        if not isinstance(klass, type):
            out.add(test)
            continue
        if not hasattr(klass, method):
            out.add(test)  # a method a condition in the class body left out
            continue
        if not callable(getattr(klass, method)):
            continue
        here = first(klass, method)
        if here is not None and here.__module__ == "__main__" and f"{here.__qualname__}.{method}" in PLANNED:
            continue  # the same test by another name (`X = T; setattr(X, "test_y", f)`)
        users = [loaded for loaded in LOADED if klass in loaded.__mro__]
        if users:
            if any(first(loaded, method) is here for loaded in users):
                out.add(test)
        elif issubclass(klass, unittest.TestCase) or klass.__name__ in bases:
            out.add(test)
    return out


PLANNED = set()
LOADED = set()
MAIN = []
STARTED = []
_start = unittest.TestResult.startTest


def start_test(self, test):
    STARTED.append(test.id())
    return _start(self, test)


unittest.TestResult.startTest = start_test
_run = unittest.TextTestRunner.run


def write(line):
    if COUNTS and os.getpid() == PID:
        with open(COUNTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")


def run(self, test):
    tests = list(leaves(test))
    PLANNED.update(defining(each) for each in tests)
    LOADED.update(type(each) for each in tests if isinstance(getattr(each, "_testMethodName", None), str))
    if not MAIN and sys.modules.get("__main__") is not None:
        MAIN.append(sys.modules["__main__"])  # runpy's module for the file: gone from sys.modules once it ends
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
    # A test the runner stops at its time limit (fork-ci-tests.cjs) shows where it was: each line it printed so far
    # (stdout line-buffered, so no whole line waits in a buffer the kill drops), and on SIGTERM every thread's stack,
    # before the signal's own action (chain). A test that sets its own SIGTERM handler replaces this one.
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(faulthandler, "register"):
        faulthandler.register(signal.SIGTERM, chain=True)
    try:
        runpy.run_path(test, run_name="__main__")
    finally:
        # Reached on a return, an exception and sys.exit (unittest.main), never on a hard stop. A file that cannot be
        # parsed writes nothing here (it cannot run, so it already fails).
        if source is not None:
            write({"unloaded": sorted(unloaded(*source))})


if __name__ == "__main__":
    main()
