import { describe, expect, it } from "vite-plus/test";

import formatJson from "./capacity/capacity-format.json" with { type: "json" };
import ruleJson from "./capacity/capacity-rule.json" with { type: "json" };
import vectorsJson from "./capacity/capacity-vectors.json" with { type: "json" };
import { capacityFormatErrors, capacitySpecErrors } from "./capacityFormat.ts";
import {
  CAPACITY_RULE,
  capacityDecision,
  capacityFill,
  type CapacityFacts,
  parseCapacityRule,
} from "./capacityRule.ts";
import { type CapacityVectorsFile, runCapacityVector } from "./capacityVectors.ts";

const vectorsFile = vectorsJson as unknown as CapacityVectorsFile;
const clone = <T>(value: T): T => structuredClone(value);
const pick = (actual: Record<string, unknown>, keys: ReadonlyArray<string>) =>
  Object.fromEntries(keys.map((key) => [key, actual[key]]));

describe("capacity files", () => {
  it("the shipped rule and vectors are well formed", () => {
    expect(capacityFormatErrors(ruleJson, "rule")).toEqual([]);
    expect(capacityFormatErrors(vectorsJson, "vectors")).toEqual([]);
    expect(parseCapacityRule(ruleJson).ok).toBe(true);
  });

  it("refuses an old rule file without a key the build needs", () => {
    const old: Record<string, unknown> = clone(ruleJson);
    delete old.writers;
    expect(parseCapacityRule(old)).toEqual({ ok: false, errors: ["rule.writers: missing"] });
  });

  it("refuses a rule file from a newer build, by its format and by an unknown key", () => {
    expect(capacityFormatErrors({ ...ruleJson, format: "capacity-rule/2" }, "rule")).toEqual([
      'rule.format: expected =capacity-rule/1, got "capacity-rule/2"',
    ]);
    expect(capacityFormatErrors({ ...ruleJson, budgets: {} }, "rule")).toEqual([
      "rule.budgets: unknown key",
    ]);
  });

  it("refuses wrong types, fractions and negative counts, naming the path", () => {
    const bad = clone(ruleJson) as Record<string, any>;
    bad.margin.minMb = -1;
    bad.kinds["job-build"].startMb = 2048.5;
    bad.floors.agent = "2";
    expect(capacityFormatErrors(bad, "rule")).toEqual([
      "rule.margin.minMb: expected uint, got -1",
      "rule.kinds.job-build.startMb: expected uint, got 2048.5",
      'rule.floors.agent: expected uint, got "2"',
    ]);
  });

  it("refuses kinds the rule names but does not have", () => {
    const bad = clone(ruleJson) as Record<string, any>;
    bad.writers.kind = "job-build";
    bad.unknownKinds.job = "job-missing";
    expect(parseCapacityRule(bad)).toEqual({
      ok: false,
      errors: [
        'rule.unknownKinds.job: "job-missing" is not a job kind',
        'rule.writers.kind: "job-build" is not a agent kind',
      ],
    });
  });

  it("refuses an empty or broken vector file", () => {
    expect(
      capacityFormatErrors({ format: "capacity-vectors/1", factSets: {}, vectors: [] }, "vectors"),
    ).toEqual(["vectors.vectors: needs at least 1 entries, has 0"]);
    expect(capacityFormatErrors({}, "vectors")).toEqual([
      "vectors.format: missing",
      "vectors.factSets: missing",
      "vectors.vectors: missing",
    ]);
    const typo = clone(vectorsJson) as Record<string, any>;
    typo.vectors[0].expect.admitt = true;
    expect(capacityFormatErrors(typo, "vectors")).toEqual([
      "vectors.vectors[0].expect.admitt: unknown key",
    ]);
  });

  it("holds every evaluator to the same number rules (the format file's cases)", () => {
    const cases = (formatJson as { cases?: ReadonlyArray<Record<string, unknown>> }).cases ?? [];
    expect(cases.length).toBeGreaterThan(0);
    for (const entry of cases) {
      expect(Object.keys(entry).toSorted()).toEqual(["json", "ok", "spec", "why"]);
      // A parser that refuses the text (NaN, Infinity) refuses the value.
      let ok = false;
      try {
        const value: unknown = JSON.parse(entry.json as string);
        ok = capacitySpecErrors(value, entry.spec).length === 0;
      } catch {
        ok = false;
      }
      expect(ok, `${String(entry.spec)} ${String(entry.json)}: ${String(entry.why)}`).toBe(
        entry.ok,
      );
    }
  });

  it("never treats a note in kinds as a kind, and refuses a kind that costs nothing", () => {
    const noted = clone(ruleJson) as Record<string, any>;
    noted.kinds["//agent-light"] = "a note about light agents";
    const parsed = parseCapacityRule(noted);
    if (!parsed.ok) throw new Error(parsed.errors.join("; "));
    expect(Object.keys(parsed.rule.kinds)).not.toContain("//agent-light");
    const decision = capacityDecision(parsed.rule, {
      facts: vectorsFile.factSets["host-12gb-idle"]!,
      ask: { kind: "//agent-light" },
    });
    expect({ kind: decision.kind, costMb: decision.costMb }).toEqual({
      kind: "agent-writer",
      costMb: 768,
    });
    const free = clone(ruleJson) as Record<string, any>;
    free.kinds["agent-light"].startMb = 0;
    expect(parseCapacityRule(free)).toEqual({
      ok: false,
      errors: ["rule.kinds.agent-light.startMb: must be at least 1"],
    });
  });

  it("refuses a format file it does not know", () => {
    expect(capacityFormatErrors(ruleJson, "rule", { format: "capacity-format/2" })).toEqual([
      "format file: not capacity-format/1",
    ]);
    const unknownType = clone(formatJson) as Record<string, any>;
    unknownType.types.perGroup.object.agent = "count";
    expect(capacityFormatErrors(ruleJson, "rule", unknownType)).toContain(
      'rule.floors.agent: the format has an unknown type "count"',
    );
  });
});

describe("capacity vectors", () => {
  it("holds every incident and the design's findings", () => {
    const names = new Set(vectorsFile.vectors.map((vector) => vector.name));
    for (const name of [
      "incident: 18 build agents and a test server at once",
      "incident: two CI runs and an agent build on 12 cores",
      "incident: two CI runs and an agent build, builds that learned 6 cores",
      "incident: CI runs and a build on 4 cores",
      "incident fan-out of 24 light agents",
      "incident: a capped box stalls while the machine has free memory",
      "incident: a leftover build daemon",
      "finding: per-box caps add up past RAM; one budget does not",
      "total grows to 18 GB: builds stop at 4 by cores",
      "total shrinks to 10 GB",
      "machine smaller than the floor: writers",
      "2 GB box read in kB",
      "4 GB box read in kB",
      "machine stall with a calm box is still severe",
      "box PSI elevated, machine calm",
    ]) {
      expect(names, name).toContain(name);
    }
    expect(names.size).toBe(vectorsFile.vectors.length);
  });

  it("shows its working in every vector", () => {
    for (const vector of vectorsFile.vectors) expect(vector.why.trim(), vector.name).not.toBe("");
  });

  it.each(vectorsFile.vectors.map((vector) => [vector.name, vector] as const))(
    "%s",
    (_, vector) => {
      const actual = runCapacityVector(CAPACITY_RULE, vectorsFile, vector);
      const keys = Object.keys(vector.expect);
      expect(keys.length).toBeGreaterThan(0);
      expect(pick(actual, keys)).toEqual(vector.expect);
    },
  );
});

describe("capacity rule", () => {
  const facts = (patch: Partial<CapacityFacts>): CapacityFacts => ({
    ...(vectorsFile.factSets["host-12gb-idle"] as CapacityFacts),
    ...patch,
  });

  it("never admits work that takes available minus owed under the severe line", () => {
    const severeLine = (marginMb: number) =>
      (marginMb * CAPACITY_RULE.pressure.severe.availableBelowMarginPercent) / 100;
    for (const name of Object.keys(vectorsFile.factSets)) {
      for (const kind of Object.keys(CAPACITY_RULE.kinds)) {
        const base = { ...vectorsFile.factSets[name]!, pieces: [] };
        const rule = { ...CAPACITY_RULE, ceilings: { agent: 500, job: 500 } };
        const { admitted } = capacityFill(rule, { facts: base }, [{ kind, count: 500 }]);
        const pieces = Array.from({ length: admitted[0]! }, () => ({ kind, demandMb: null }));
        const after = capacityDecision(rule, { facts: { ...base, pieces }, ask: { kind } });
        expect(after.admit, `${name} ${kind}`).toBe(false);
        expect(
          (base.availableMb as number) - after.owedMb!,
          `${name} ${kind}`,
        ).toBeGreaterThanOrEqual(severeLine(after.marginMb!));
      }
    }
  });

  it("reads any machine of 1 GB or more given in kB as unreadable", () => {
    for (const totalMb of [1024, 2048, 3900, 4096, 16_384, 1_048_575]) {
      const kb = { ...facts({}), totalMb: totalMb * 1024, availableMb: totalMb * 800, cpuCores: 2 };
      expect(
        capacityDecision(CAPACITY_RULE, { facts: kb, ask: { kind: "agent-light" } }).reason,
      ).toBe("unreadable");
    }
  });

  it("never admits on a non-finite reading", () => {
    for (const bad of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      for (const key of ["totalMb", "availableMb", "cpuCores"] as const) {
        const decision = capacityDecision(CAPACITY_RULE, {
          facts: facts({ [key]: bad }),
          ask: { kind: "agent-light" },
        });
        expect(decision.reason, `${key}=${bad}`).toBe("unreadable");
      }
    }
  });

  it("counts a non-finite demand as unmeasured (owes in full)", () => {
    const decision = capacityDecision(CAPACITY_RULE, {
      facts: facts({ pieces: [{ kind: "agent-light", demandMb: Number.NaN }] }),
      ask: { kind: "agent-light" },
    });
    expect(decision.owedMb).toBe(384);
  });

  it("gives back a pressure state the next read continues from", () => {
    const calm = facts({});
    const first = capacityDecision(CAPACITY_RULE, {
      facts: calm,
      ask: { kind: "agent-light" },
      previous: { level: "elevated", calmSinceMs: null },
      nowMs: 5_000,
    });
    expect(first.pressure).toEqual({ level: "elevated", calmSinceMs: 5_000 });
    const second = capacityDecision(CAPACITY_RULE, {
      facts: calm,
      ask: { kind: "agent-light" },
      previous: first.pressure,
      nowMs: 65_000,
    });
    expect(second).toMatchObject({ admit: true, level: "ok", pressure: { calmSinceMs: null } });
  });
});
