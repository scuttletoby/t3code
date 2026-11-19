"""Law tests: a small seeded runner, standard library only (the contract it keeps: README.md in this folder).

A law test generates lists of steps (any order of the things that can happen: a run, a crash half-way, a retry, a
push, old or empty state at the start) and plays each list against the real code. After every step the test checks its
laws and raises Broken when one does not hold. check() runs a capped number of tries from a fixed seed; on a failure it
shrinks the list (drops steps while the same law still breaks, capped too) and fails with the seed, the try, the line
that runs that one try again and the shortest failing list, ready to paste into a normal fixed test.

    import laws
    def test_the_laws(self):
        laws.check('queue', generate, play, seed=464, tries=12)

generate(rng) returns a list of steps (plain values whose repr can be pasted back into a test); play(steps) plays them
from a fresh start and raises Broken(law, detail) for a law that does not hold. Any other exception counts as a
failure too, keyed by its type. Nothing here sleeps or reads the clock for the code under test: time is a step.
"""
import os
import random
import sys
import time

SHRINK_RUNS = 300   # plays at most while shrinking one failure: the cap. A test with slow plays passes runs= so
SHRINK_SECONDS = 20   # that runs x one play stays under this backstop, which only ends a shrink that got slower
MAX_TRIES = 100000


class Broken(AssertionError):
    """A law that does not hold. `law`: its short name; shrinking keeps only lists that break the same law."""

    def __init__(self, law, detail=''):
        super().__init__(f'{law}: {detail}' if detail else law)
        self.law, self.detail = law, detail


def settings(seed, tries, env=None):
    """(seed, tries, only) from the test's fixed seed and tries, and the environment: LAWS_SEED (another seed),
    LAWS_TRIES (more or fewer tries), LAWS_TRY (only this one try, as a failure report names it). A value that is not
    a whole number in range is an error, never quietly fewer tries."""
    env = os.environ if env is None else env

    def number(name, default, low, high):
        raw = env.get(name, '')
        if raw == '':
            return default
        if not (raw.isascii() and raw.isdigit()) or not low <= int(raw) <= high:   # isdigit alone takes '²', '٣'
            raise ValueError(f'{name}={raw!r} is not a whole number from {low} to {high}')
        return int(raw)
    seed = number('LAWS_SEED', seed, 0, 2 ** 63)
    tries = number('LAWS_TRIES', tries, 1, MAX_TRIES)
    only = number('LAWS_TRY', None, 0, MAX_TRIES - 1)
    if only is not None and only >= tries:
        raise ValueError(f'LAWS_TRY={only} is not one of the {tries} tries (0 to {tries - 1}); set LAWS_TRIES too')
    return seed, tries, only


def rng_for(name, seed, i):
    """The generator of try `i`: the same steps for the same name, seed and try on every machine and Python 3."""
    return random.Random(f'{name}/{seed}/{i}')


def failure_key(e):
    """What shrinking keeps: the law's name, or for a crash its exception type. So two different crashes of one type
    (two KeyErrors) count as the same failure, and a shrunk list may show the other one: its report names the crash
    it ended with."""
    return e.law if isinstance(e, Broken) else type(e).__name__


def play_once(play, steps):
    """None when every law held, else the exception."""
    try:
        play(steps)
    except Exception as e:   # noqa: BLE001 - a crash of the code under test is a failure like a broken law
        return e
    return None


def shrink(play, steps, key, runs=SHRINK_RUNS, seconds=SHRINK_SECONDS, clock=time.monotonic):
    """(shortest list found, its exception, whether shrinking finished): drops runs of steps, halving the run length,
    and keeps a shorter list only when it breaks the same law (key). Capped by `runs` plays (the cap a test sizes to
    its plays) and by `seconds` (a backstop)."""
    best, error, done = list(steps), None, True
    end, used = clock() + seconds, 0
    chunk = max(len(best) // 2, 1)
    while chunk >= 1:
        i, progressed = 0, False
        while i < len(best):
            if used >= runs or clock() > end:
                return best, error, False
            candidate = best[:i] + best[i + chunk:]
            used += 1
            e = play_once(play, candidate)
            if e is not None and failure_key(e) == key:
                best, error, progressed = candidate, e, True
            else:
                i += chunk
        if not progressed:
            if chunk == 1:
                break
            chunk //= 2
    return best, error, done


def report(name, seed, i, tries, steps, best, error, done, rerun, show=repr):
    shown = '\n'.join(f'        {show(s)},' for s in best)
    how = 'shrinking finished' if done else 'shrinking stopped at its cap: a shorter list may exist'
    return (f'law broken: {error}\n'
            f'  law test {name!r}, seed {seed}, try {i} of {tries} (0-based)\n'
            f'  run this try again: LAWS_SEED={seed} LAWS_TRY={i} LAWS_TRIES={tries} {rerun}\n'
            f'  shortest failing steps ({len(best)} of {len(steps)}; {how}):\n'
            f'    [\n{shown}\n    ]\n'
            '  Before its fix merges, paste these steps into a normal fixed test (fork-ci/laws/README.md, "A failure").')


def check(name, generate, play, *, seed, tries, env=None, rerun=None, show=repr, runs=SHRINK_RUNS,
          seconds=SHRINK_SECONDS):
    """Play `tries` generated lists of steps (or only LAWS_TRY's); raise AssertionError with report() on the first
    that fails. Returns the number of lists played."""
    seed, tries, only = settings(seed, tries, env)
    rerun = rerun or f'python3 -I -B {sys.argv[0]}'
    played = 0
    for i in ([only] if only is not None else range(tries)):
        steps = generate(rng_for(name, seed, i))
        played += 1
        e = play_once(play, steps)
        if e is None:
            continue
        best, error, done = shrink(play, steps, failure_key(e), runs, seconds)
        raise AssertionError(report(name, seed, i, tries, steps, best, error or e, done, rerun, show)) from e
    return played


class Coverage:
    """What the generated steps must reach with the fixed seed: a list of kinds (step kinds, outcomes, code paths the
    test can tell apart). A kind the fixed seed stops reaching fails the test, so a generator that drifts into
    proving nothing goes red. Check it only for the full default run (all tries, no LAWS_TRY)."""

    def __init__(self, must_see):
        self.must_see, self.seen = tuple(must_see), {}

    def saw(self, kind):
        self.seen[kind] = self.seen.get(kind, 0) + 1

    def missing(self):
        return [k for k in self.must_see if not self.seen.get(k)]

    def check(self):
        gone = self.missing()
        if gone:
            raise AssertionError('the generated steps no longer reach: ' + '; '.join(gone)
                                 + ' (change the generator or the seed, never drop the kind quietly)')
