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
swap free; the OS level where there is no PSI). An unreadable PSI, swap or OS level is `elevated`. Up at once;
`severe` steps down to `elevated` after `severeClearSeconds` without severe readings, `elevated` to `ok` after
`elevatedClearSeconds` of readings under the `calm` limits. The caller keeps `{level, calmSinceMs}` between
reads; no previous state means the raw level; no clock means no step down; a clock that went back restarts
the wait.

## Units and rounding (every evaluator does exactly this)

- Memory is whole MB (1 MB = 1048576 bytes) in every key ending in `Mb`; CPU is hundredths of a core in keys
  ending in `CentiCores`, and `cpuCores` is read as `floor(cpuCores x 100)`.
- Readings round toward safety: total, available, demand, use and swap round down; reservation sizes,
  declared sizes and learned costs round up; the margin is an integer ceiling division
  (`ceil(max(minMb x 100, total x totalPercent) x percent / 10000)`). Percent comparisons multiply, never divide
  (`available x 100 < margin x 50`).
- Unit mix-ups are caught by bounds: a total outside `readingBounds` (bytes or KB given as MB, GB given as MB),
  available above total, or `cpuCores` outside `(0, maxCpuCores]` is unreadable. A demand or use above the total
  or below 0 is not trusted: the piece owes in full, the reservation holds in full.

## Facts (the evaluator's input)

| Key                                  | Value                                                                   | Missing or broken                              |
| ------------------------------------ | ----------------------------------------------------------------------- | ---------------------------------------------- |
| `totalMb`, `availableMb`, `cpuCores` | numbers                                                                 | unreadable: nothing admitted                   |
| `pieceCaps`                          | each piece runs under its own memory cap                                | `false`: wider margin                          |
| `pieces`                             | `[{kind, demandMb, declaredMb?}]`, `demandMb` `null` while not measured | unreadable                                     |
| `reservations`                       | `[{mb, useMb}]`                                                         | absent or `null`: none; not a list: unreadable |
| `swap`                               | `{totalMb, freeMb}`; `null` or `totalMb` 0: no swap                     | `"unreadable"`: elevated + wider margin        |
| `psi`                                | `{someAvg10Pct, fullAvg10Pct}` (0-100); `null`: the OS has none         | `"unreadable"`: elevated                       |
| `osLevel`                            | `ok`, `elevated`, `severe`; `null`: none                                | anything else: elevated                        |

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
  `uint` (whole number >= 0), `=text` (exactly that string), `@type` (another named type);
- `{"object": {key: spec}}`: a key ending in `?` is optional; any other key not listed is refused;
- `{"map": spec}`: any keys, each value a spec; `{"array": spec, "minItems"?: n}`.

Keys starting with `//` are notes and skipped everywhere. The check is strict on purpose: a file from an older
build (a key missing) or a newer one (another `format` or an unknown key) is refused by name, never half-read.
A caller that gets a refusal keeps today's fixed limits.
