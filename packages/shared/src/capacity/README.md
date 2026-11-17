# Capacity rule: one source, shared vectors

How many agents and jobs a machine runs is not a setting: it falls out of one rule. Every number of that rule is
in [capacity-rule.json](capacity-rule.json). [capacity-vectors.json](capacity-vectors.json) holds the cases
every evaluator must answer the same way (each incident that led to today's fixed limits is one of them).
[capacity-format.json](capacity-format.json) is the shape of both files. Evaluators today: TypeScript
([../capacityRule.ts](../capacityRule.ts), format check [../capacityFormat.ts](../capacityFormat.ts), vector
runner [../capacityVectors.ts](../capacityVectors.ts)); the host kit's Python one comes later and runs the same vectors from
this folder (its installer copies the three files).

## The rule

```
margin   = max(margin.minMb, totalPercent of total)    x noSwapOrNoCapsPercent without swap or per-piece caps
owed     = sum over live pieces: max(0, cost(kind) - demand)
reserved = sum over reservations: max(0, mb - use)
free     = available - margin - owed - reserved
```

A piece is admitted, in this order:

1. **Refused at once:** unreadable readings; its group's ceiling reached; level `severe`; its kind's
   `maxAtOnce` reached.
2. **Admitted (`fits`)** when the level is `ok`, a writer is under the writers cap, a job's cores fit
   (`sum of live job cores + its cores <= jobCoresPercent of cores`) and `cost <= free`.
3. **Else admitted under the floor (`floor`)** when fewer than `floors[group]` pieces of its group run and
   `available - owed - min(cost, start cost) >= severe line` (`margin x severe.availableBelowMarginPercent`).
   The floor passes the budget, the elevated brake, reservations and a bad learned cost; never a ceiling,
   `severe` or the severe line, so on a machine smaller than the floor physical memory wins.
4. **Else refused** with the first failed check: `elevated`, `writers`, `cores`, `budget`.

Writers cap: `max(writers.min, writers.perBuildThatFits x builds that fit the idle machine)`, where the idle
machine is `available + demand of live pieces - margin - reserved`, and builds are also limited by job cores.

Kinds: unknown names are expensive, never cheap: a name starting with `unknownKinds.jobPrefix` counts as
`unknownKinds.job`, anything else as `unknownKinds.other` (a writer). A `plusDeclared` kind costs `startMb` plus
the size the piece declares (`declaredFallbackMb` when missing or impossible). Learned costs are clamped to
`learnedBounds` percent of the start cost; a broken one is the start cost.

**Pressure level.** `severe` or `elevated` when any of its limits is met (PSI, available against the margin,
swap free; the OS level where there is no PSI). PSI is the worst of the machine's (`psi`) and the box's the piece
goes into (`boxPsi`): a box can stall at its cap while the machine has free memory, and the machine can stall
while one box is calm. An unreadable PSI, box PSI, swap or OS level is `elevated`. Up at once; `severe` steps
down to `elevated` after `severeClearSeconds` without severe readings, `elevated` to `ok` after
`elevatedClearSeconds` of readings under the `calm` limits. The caller keeps `{level, calmSinceMs}` between
reads, one per box it asks for (the level includes that box's PSI); no previous state means the raw level; no
clock means no step down; a clock that went back restarts the wait.

## Units and rounding (every evaluator does exactly this)

- Memory is whole MB (1 MB = 1048576 bytes) in every key ending in `Mb`; CPU is hundredths of a core in keys
  ending in `CentiCores`, and `cpuCores` is read as `floor(cpuCores x 100)`.
- Readings round toward safety: total, available, demand, use and swap round down; reservation sizes,
  declared sizes and learned costs round up; the margin is an integer ceiling division
  (`ceil(max(minMb x 100, total x totalPercent) x percent / 10000)`). Percent comparisons multiply, never divide
  (`available x 100 < margin x 50`).
- Unit mix-ups are caught by bounds: a total outside `readingBounds` is unreadable. `maxTotalMb` is 1048575 (just
  under 1 TiB), so any machine of 1 GB or more read in kB or bytes (`/proc/meminfo` is in kB) is above it;
  `minTotalMb` is 1024, so GB given as MB is below it. The cost: a machine with 1 TiB of RAM or more reads as
  unreadable (nothing admitted) until its rule file raises `maxTotalMb`; the rule cannot tell it from a 1 GB
  machine read in kB, so it takes the safe side. A machine under 1 GB could not run anything anyway (the
  minimum margin is 1024 MB).
- Available above total, `cpuCores` outside `(0, maxCpuCores]` (1024: catches centi-cores given as cores on
  machines with more than 10 cores; on smaller ones the memory budget still holds) or a swap total above
  `maxTotalMb` is unreadable. A demand or use above the total or below 0 is not trusted: the piece owes in full,
  the reservation holds in full. A reservation size above `maxTotalMb` counts as `maxTotalMb`, so every sum
  stays a whole number below 2^53 in every language.
- A boolean is never a number: `cpuCores: true` is unreadable, `demandMb: true` owes in full (Python's `bool`
  is an `int`; check for it first). `2048.0` is `2048`. The `agreement:` vectors hold every evaluator to this.

## Facts (the evaluator's input)

| Key                                  | Value                                                                                    | Missing or broken                              |
| ------------------------------------ | ---------------------------------------------------------------------------------------- | ---------------------------------------------- |
| `totalMb`, `availableMb`, `cpuCores` | numbers                                                                                  | unreadable: nothing admitted                   |
| `pieceCaps`                          | each piece runs under its own memory cap                                                 | `false`: wider margin                          |
| `pieces`                             | `[{kind, demandMb, declaredMb?}]`, `demandMb` `null` while not measured                  | unreadable                                     |
| `reservations`                       | `[{mb, useMb}]`                                                                          | absent or `null`: none; not a list: unreadable |
| `swap`                               | `{totalMb, freeMb}`; `null` or `totalMb` 0: no swap                                      | `"unreadable"`: elevated + wider margin        |
| `psi`                                | the machine's memory PSI `{someAvg10Pct, fullAvg10Pct}` (0-100); `null`: the OS has none | `"unreadable"`: elevated                       |
| `boxPsi`                             | the same for the box (cgroup, slice) the asked piece goes into; absent or `null`: no box | `"unreadable"`: elevated                       |
| `osLevel`                            | `ok`, `elevated`, `severe`; `null`: none                                                 | anything else: elevated                        |

## Readings (where the facts come from)

[../capacityReadings.ts](../capacityReadings.ts) reads the machine through a small host (files,
commands, Node's numbers) that tests replace with the fixtures in
[capacity-readings.json](capacity-readings.json); the kit's Python reader runs the same cases later. It
keeps nothing: no timer, no cache, no file written. `readMachine` gives `totalMb`, `availableMb`,
`cpuCores`, `swap`, `psi`, `osLevel` (total and available from one read, total never kept);
`readBoxPsi` a box's PSI; `readPieceDemand` one piece's demand (`gone` when its cgroup ended between
the list and the read: drop it). `capacityFactsOf` joins them with what the caller knows (pieces,
`pieceCaps`); `capacityLevel` gives the level alone, with the caller's `{level, calmSinceMs}`.

A missing, unreadable, empty, cut-short, impossible (available above total, swap free above swap total,
PSI above 100) or wrongly-unitted value is `null` (total, available, cores, demand) or `"unreadable"`
(swap, PSI, OS level): the rule then admits nothing or brakes, never more. Only a file that does not
exist where the OS simply has no such signal means "none": `/proc/pressure/memory` (kernel without PSI)
and a v1 box's PSI. Fixture cases marked `failed` must never give more room than their healthy host.

| Reading           | Linux, cgroup v2                                                                                                                                               | Linux, cgroup v1                                                               | macOS                                                                                                         | Windows (and the rest)                                |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| total, available  | `/proc/meminfo` MemTotal, MemAvailable (kB); in a container (namespace root `0::/`) capped by `memory.max` and `memory.max - (memory.current - inactive_file)` | same; capped by the root `memory.limit_in_bytes` when below the total          | Node's total; `vm_stat` free + inactive + speculative pages. If that fails: Node's free pages (an under-read) | Node's `totalmem`, `freemem` (with standby)           |
| cores             | Node's, capped by the namespace root's `cpu.max`                                                                                                               | Node's (`cpu.cfs_quota_us` not read)                                           | Node's                                                                                                        | Node's                                                |
| swap              | SwapTotal, SwapFree; total 0 = none                                                                                                                            | same                                                                           | none: swap files grow on demand (wider margin; the OS level covers it)                                        | none: Node does not give the page file (wider margin) |
| PSI               | `/proc/pressure/memory`; missing = none (margin only)                                                                                                          | same                                                                           | none: the OS level `kern.memorystatus_vm_pressure_level` (1 ok, 2 elevated, 4 severe; else unreadable)        | none, no OS level: available against the margin only  |
| box PSI           | `<box>/memory.pressure`; missing = unreadable                                                                                                                  | none                                                                           | none                                                                                                          | none                                                  |
| demand of a piece | `memory.stat` anon + shmem + `memory.swap.current` (missing swap part = 0: owes more); `current`, `peak` kept for cost learning                                | `total_rss + total_shmem + total_swap`; `usage_in_bytes`, `max_usage_in_bytes` | `null`: owes its full cost                                                                                    | `null`: owes its full cost                            |

Not read yet (stated limits): a cgroup v1 container's CPU quota and a container's `memory.swap.max`; a
cgroup v2 box with PSI turned off by `cgroup.pressure` reads as unreadable (elevated). Process-tree
demand for macOS and Windows comes with the basic tier.

## Vector file

```json
{
  "format": "capacity-vectors/1",
  "factSets": { "<name>": { ...facts } },
  "vectors": [ { "name", "why", "facts": "<name>" | { ...facts }, "patch"?, "ask" | "fill",
                 "previous"?, "nowMs"?, "learned"?, "floors"?, "ceilings"?, "expect" } ]
}
```

- Facts: the named set (or the object), then `patch` replaces whole top-level keys.
- `floors` / `ceilings` replace the rule's for this vector.
- `ask: {kind, declaredMb?}` runs one decision. `expect` may hold `admit`, `reason`, `level`, `calmSinceMs`,
  `freeMb`, `marginMb`, `owedMb`, `reservedMb`, `costMb` (`null` for the numbers when unreadable); only the keys
  given are compared.
- `fill: [{kind, count, declaredMb?}]` asks again and again, as at second zero: each admitted piece joins the
  live pieces with `demandMb: null`; a step stops at its first refusal. `expect` holds `admitted` (per step)
  and `stoppedBy` (the first refusal's reason per step, `null` when all were admitted). `previous` and `nowMs`
  stay the same for every ask.
- Expected values are worked by hand from the rule, and each `why` says how; never paste an evaluator's output.

Not a vector: the incident where an unguarded delete waited for an approval nobody could give. It is not about
capacity.

## The format language

`capacity-format.json` names types under `types`; a file is checked as type `rule` or `vectors`. A spec is:

- a string of alternatives split by `|`: `any`, `null`, `bool`, `string`, `number` (finite), `number>=0`,
  `uint` (whole number, 0 to 2^53-1), `=text` (exactly that string), `@type` (another named type);
- `{"object": {key: spec}}`: a key ending in `?` is optional; any other key not listed is refused;
- `{"map": spec}`: any keys, each value a spec; `{"array": spec, "minItems"?: n}`.

**Agreement rules** (so a Python checker and the TypeScript one never differ):

- A boolean is never a number: `true` is not a `uint` or a `number`, and `1` is not a `bool`.
- `uint` is a finite number with no fraction from 0 to 9007199254740991 (2^53-1). `2048.0` and `1e3` are
  uints (JavaScript cannot tell `2048.0` from `2048`; Python parses it as a float, so check the value, not the
  type). Above 2^53-1 is refused.
- `NaN`, `Infinity` and `-Infinity` are not JSON and are refused when parsing (in Python pass a
  `parse_constant` that raises to `json.loads`). A number too large for a double (`1e400`) parses as infinity
  and is refused as not finite.

`cases` in the format file are `{spec, json, ok, why}`: parse `json` (a parse refusal is `ok: false`), check it
against `spec`, compare with `ok`. Every checker runs them.

After the format, the rule is cross-checked: the kinds `unknownKinds` and `writers` name exist in the right
group; every kind's `startMb` is at least 1; `learnedBounds.minPercent <= maxPercent`; `minTotalMb <=
maxTotalMb`. Notes inside `kinds` are dropped, never kinds.

Keys starting with `//` are notes and skipped everywhere. The check is strict on purpose: a file from an older
build (a key missing) or a newer one (another `format` or an unknown key) is refused by name, never half-read.
A caller that gets a refusal keeps today's fixed limits.
