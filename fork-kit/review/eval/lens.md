## Your lens: correctness + state + data integrity + concurrency (one read-only reviewer)

Goal: find the bugs early and fix them at their root, so the same bug cannot come back in a slightly different case.

**Correctness** (always): null, empty and bounds, error paths, the unhappy branch of every `if`, units, guard
clauses, and _is this the root cause, and where else does the pattern occur?_

**State question** (always, answered on the `state:` line): What happens when this runs again, stops half-way, or
meets existing, old or empty state?

**Data integrity** (anything that saves, moves or counts data): attacks such as a double submit, two processes at
once, a disconnect mid-action, a kill after each write, a retry, input at the wrong moment. Each finding comes with a
failing test.

**Concurrency** (threads, async work, schedulers, messages between processes): races, ordering, shutdown.

In this exercise you cannot run builds, tests or servers. Replace "failing test" with an **exact trace** (file:line,
the inputs and state, step by step, to the wrong result) plus the name and content of the test you would add.

Severity: **blocker** = loses or duplicates stored state, or breaks deploy or startup for everyone; **major** = a
realistic path to wrong data, a hang, a security hole, or a failed rollback; **minor** = real but unlikely or low
impact. Style nits go in a separate optional list.

## Output format (write exactly this to your result file)

```
lens: correctness+state+data+concurrency · reviewer: RUN-ID <your run id> · read at: <the case's reviewed sha>
covered: <every file and function read in full>        # anything not listed counts as not reviewed
checks: <what was traced, including the hardest case>
state: <the answer to the state question>
finding: <blocker|major|minor> <file:line> <inputs/state -> wrong result> (confidence) [test: <name>: <what it does>]
findings: none                                        # instead, when there are none
nits: <optional>
```

"Looks fine" is not a result. One `finding:` line per distinct bug.
