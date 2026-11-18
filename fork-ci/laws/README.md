# Law tests: the contract

A law test plays many generated orders of events against the real code and checks rules ("laws") that must
hold in every order: a run, a crash half-way, a rerun, a push, old or empty state at the start. It finds the
order nobody wrote a test for. This page is the contract every project's law tests keep, written here once.
Projects link to it (pinned, like the actions: [../README.md](../README.md), "Pinning") and do not copy it.

[`laws.py`](laws.py) is a small runner, Python standard library only, that keeps the rules below. Projects in
another language write their own small runner to the same rules: no outside property-test library (owner
decision, 2026-11-18). The fork's first user is the merge queue's
[`pr_queue_laws.test.py`](../actions/pr-queue/pr_queue_laws.test.py).

## The rules

1. **A fixed seed in CI.** CI plays the same steps on every run: a test's seed and number of tries are constants
   in the test. Any other seed (`LAWS_SEED`) or number of tries (`LAWS_TRIES`) is for a person or a nightly job,
   never the pull-request check. A red is then always the change's, never luck.
2. **On a failure, the seed and the shortest failing step list are printed**: the seed, the try, one command that
   plays only that try again (`LAWS_SEED=… LAWS_TRY=… LAWS_TRIES=…`), and the steps, shrunk to a shortest list
   that still breaks the same law, written so they paste back into a test.
3. **Capped tries.** A fixed number of tries, and shrinking is capped too (number of plays and a time backstop).
   A test sized for the CI job, not "until it finds something".
4. **No sleeps.** Time is a step the test controls (a fake clock, a no-op sleep); nothing waits for real time
   and nothing reads the wall clock to decide.
5. **A failure becomes a normal fixed test before its fix merges.** Paste the printed steps into a fixed test
   (a list played on every run, whatever the seed) in the same pull request as the fix, red before the fix.
   The random search finds it once; the fixed test keeps it found.

And the questions every law test answers with steps, because these are where state breaks: what happens
when the code **runs again**, **stops half-way** (killed at each call that changes something, and after a
write that took effect), and **meets existing, old or empty state** (a file of an older version, an empty
or broken cache, folders a killed run left).

Bad input to the runner is an error, never fewer tries: a `LAWS_*` value that is not a whole number in
range fails the test.

A coverage list (`laws.Coverage`) names what the fixed seed must reach (each kind of step, each outcome
that matters). A kind the fixed seed stops reaching fails the test, so a generator that drifts into
testing nothing goes red instead of green.

## Writing one with `laws.py`

```python
import laws  # sys.path: fork-ci/laws (it is test-side only, never imported by an action)

def generate(rng):            # rng: random.Random of this try; steps: plain values, repr pastes back
    return [('push', rng.randint(1, 3)), ('run', ())]

def play(steps):              # a fresh start every time; raise laws.Broken('L1 name', 'detail')
    ...

laws.check('my-laws', generate, play, seed=464, tries=40)
```

`play` raises `laws.Broken(law, detail)` when a law does not hold; any other exception fails too (keyed by
its type). Shrinking keeps a shorter list only when it breaks the same law, so the printed list shows that
law and not another bug met on the way.

## A failure

```text
law broken: L1 arms only what was read: #1 armed at c35fa0bc7d85, which is no change of ['8b760be9dd6d'] on main
  law test 'pr-queue', seed 464, try 4 of 60 (0-based)
  run this try again: LAWS_SEED=464 LAWS_TRY=4 LAWS_TRIES=60 python3 -I -B fork-ci/actions/pr-queue/pr_queue_laws.test.py
  shortest failing steps (4 of 35; shrinking finished):
    [
        ('arm', 1),
        ('lens', 1, 'b', 'old'),
        ('main', 'file', 3),
        ('push', 1, 'new', 0),
    ]
```

Run the printed command to see it again. Then copy the list into the test's fixed histories under a name that
says what happened, see it red, fix the code, see it green, and merge both together. A law that is wrong (the
code does what its docs say) is fixed in the law, with the doc quoted next to it.
