import { describe, expect, it } from "vite-plus/test";

import readingsJson from "./capacity/capacity-readings.json" with { type: "json" };
import vectorsJson from "./capacity/capacity-vectors.json" with { type: "json" };
import {
  type CapacityFileRead,
  type CapacityHost,
  capacityFactsOf,
  type MachineReadings,
  nodeCapacityHost,
  readBoxPsi,
  readMachine,
  readPieceDemand,
} from "./capacityReadings.ts";
import {
  CAPACITY_RULE,
  capacityDecision,
  capacityLevel,
  type CapacityLevel,
} from "./capacityRule.ts";
import type { CapacityVectorsFile } from "./capacityVectors.ts";

type FixtureFile = string | { readonly error: "unreadable" };
interface FixtureHost {
  readonly platform: string;
  readonly totalMemBytes: number;
  readonly freeMemBytes: number;
  readonly cpuCount: number;
  readonly files: Readonly<Record<string, FixtureFile>>;
  readonly commands: Readonly<Record<string, string | null>>;
}
type FixturePatch = Partial<Omit<FixtureHost, "files" | "commands">> & {
  readonly files?: Readonly<Record<string, FixtureFile | null>>;
  readonly commands?: Readonly<Record<string, string | null>>;
};
interface FixtureCase {
  readonly name: string;
  readonly why: string;
  readonly host: string;
  readonly failed?: boolean;
  readonly patch?: FixturePatch;
  readonly read: "machine" | { readonly piece: string } | { readonly box: string };
  readonly expect: Readonly<Record<string, unknown>>;
}
const fixtures = readingsJson as unknown as {
  readonly format: string;
  readonly hosts: Readonly<Record<string, FixtureHost>>;
  readonly cases: ReadonlyArray<FixtureCase>;
};

function hostOf(name: string, patch: FixturePatch = {}): CapacityHost {
  const base = fixtures.hosts[name];
  if (base === undefined) throw new Error(`unknown host ${name}`);
  const { files: filePatch = {}, commands: commandPatch = {}, ...numbers } = patch;
  const merged = { ...base, ...numbers };
  const files: Record<string, FixtureFile | null> = { ...base.files, ...filePatch };
  const commands: Record<string, string | null> = { ...base.commands, ...commandPatch };
  return {
    platform: merged.platform,
    readFile: async (path): Promise<CapacityFileRead> => {
      const file = Object.hasOwn(files, path) ? files[path] : null;
      if (file === null || file === undefined) return { error: "missing" };
      return typeof file === "string" ? { text: file } : file;
    },
    run: async (file, args) => {
      const key = [file, ...args].join(" ");
      return Object.hasOwn(commands, key) ? (commands[key] ?? null) : null;
    },
    totalMemBytes: () => merged.totalMemBytes,
    freeMemBytes: () => merged.freeMemBytes,
    cpuCount: () => merged.cpuCount,
  };
}

async function runCase(fixture: FixtureCase): Promise<Record<string, unknown>> {
  const host = hostOf(fixture.host, fixture.patch);
  const machine = await readMachine(host);
  if (fixture.read === "machine") {
    return { ...machine, gaps: machine.gaps.map((gap) => gap.reading) };
  }
  if ("piece" in fixture.read)
    return await readPieceDemand(host, machine.cgroup, fixture.read.piece);
  return { psi: await readBoxPsi(host, machine, fixture.read.box) };
}

const RANK: Readonly<Record<CapacityLevel, number>> = { ok: 0, elevated: 1, severe: 2 };
const decide = (machine: MachineReadings) =>
  capacityDecision(CAPACITY_RULE, {
    facts: capacityFactsOf(machine, { pieceCaps: true, pieces: [] }),
    ask: { kind: "agent-light" },
  });

describe("capacity readings: shared fixtures", () => {
  it("the fixture file is well formed", () => {
    expect(fixtures.format).toBe("capacity-readings/1");
    expect(fixtures.cases.length).toBeGreaterThanOrEqual(50);
    const names = fixtures.cases.map((fixture) => fixture.name);
    expect(new Set(names).size).toBe(names.length);
    for (const fixture of fixtures.cases) {
      expect(Object.hasOwn(fixtures.hosts, fixture.host), fixture.name).toBe(true);
      expect(fixture.why.length, fixture.name).toBeGreaterThan(0);
      expect(Object.keys(fixture.expect).length, fixture.name).toBeGreaterThan(0);
      if (fixture.failed === true) expect(fixture.read, fixture.name).toBe("machine");
    }
  });

  it.each(fixtures.cases.map((fixture) => [fixture.name, fixture] as const))(
    "%s",
    async (_name, fixture) => {
      const actual = await runCase(fixture);
      const keys = Object.keys(fixture.expect);
      expect(Object.fromEntries(keys.map((key) => [key, actual[key]]))).toEqual(fixture.expect);
    },
  );
});

describe("capacity readings: a failed reading never admits more", () => {
  it.each(
    fixtures.cases
      .filter((f) => f.failed === true)
      .map((fixture) => [fixture.name, fixture] as const),
  )("%s", async (_name, fixture) => {
    const healthy = decide(await readMachine(hostOf(fixture.host)));
    const patched = decide(await readMachine(hostOf(fixture.host, fixture.patch)));
    if (patched.admit) expect(healthy.admit).toBe(true);
    expect(RANK[patched.level]).toBeGreaterThanOrEqual(RANK[healthy.level]);
    if (patched.freeMb !== null) expect(patched.freeMb).toBeLessThanOrEqual(healthy.freeMb!);
  });

  it("unreadable meminfo: nothing admitted, level elevated", async () => {
    const machine = await readMachine(hostOf("linux-v2", { files: { "/proc/meminfo": "" } }));
    const decision = decide(machine);
    expect([decision.admit, decision.reason, decision.level]).toEqual([
      false,
      "unreadable",
      "elevated",
    ]);
  });

  it("unreadable PSI or box PSI: elevated", async () => {
    const host = hostOf("linux-v2", {
      files: { "/proc/pressure/memory": { error: "unreadable" } },
    });
    const machine = await readMachine(host);
    expect(decide(machine).level).toBe("elevated");
    const calm = await readMachine(hostOf("linux-v2"));
    const facts = capacityFactsOf(calm, {
      pieceCaps: true,
      pieces: [],
      boxPsi: await readBoxPsi(hostOf("linux-v2"), calm, "/gone.slice"),
    });
    expect(capacityLevel(CAPACITY_RULE, { facts }).level).toBe("elevated");
  });

  it("a host that throws reads as unreadable, never as free", async () => {
    const boom = (): never => {
      throw new Error("boom");
    };
    const host: CapacityHost = {
      platform: "linux",
      readFile: async () => boom(),
      run: async () => boom(),
      totalMemBytes: boom,
      freeMemBytes: boom,
      cpuCount: boom,
    };
    const machine = await readMachine(host);
    expect(machine).toMatchObject({
      totalMb: null,
      availableMb: null,
      cpuCores: null,
      swap: "unreadable",
    });
    expect(await readPieceDemand(host, "v2", "/a.scope")).toMatchObject({
      gone: false,
      demandMb: null,
    });
    const darwin = await readMachine({ ...host, platform: "darwin" });
    expect(darwin).toMatchObject({ totalMb: null, availableMb: null, osLevel: "unreadable" });
  });

  it("a piece with unknown demand owes its full cost; an ended one is dropped", async () => {
    const host = hostOf("linux-v2", {
      files: { "/sys/fs/cgroup/work.slice/agent-a.scope/memory.stat": { error: "unreadable" } },
    });
    const machine = await readMachine(host);
    const pieces = [];
    for (const scope of ["/work.slice/agent-a.scope", "/work.slice/agent-gone.scope"]) {
      const demand = await readPieceDemand(host, machine.cgroup, scope);
      if (!demand.gone) pieces.push({ kind: "agent-writer", demandMb: demand.demandMb });
    }
    expect(pieces).toEqual([{ kind: "agent-writer", demandMb: null }]);
    const decision = capacityDecision(CAPACITY_RULE, {
      facts: capacityFactsOf(machine, { pieceCaps: true, pieces }),
      ask: { kind: "agent-light" },
    });
    expect(decision.owedMb).toBe(768);
  });
});

describe("capacity level: state passed in, not kept", () => {
  it("agrees with capacityDecision on every vector", () => {
    const file = vectorsJson as unknown as CapacityVectorsFile;
    for (const vector of file.vectors) {
      if (vector.ask === undefined) continue;
      const base = typeof vector.facts === "string" ? file.factSets[vector.facts]! : vector.facts;
      const input = {
        facts: { ...base, ...vector.patch },
        ...(vector.previous === undefined ? {} : { previous: vector.previous }),
        ...(vector.nowMs === undefined ? {} : { nowMs: vector.nowMs }),
      };
      const decision = capacityDecision(CAPACITY_RULE, { ...input, ask: vector.ask });
      expect(capacityLevel(CAPACITY_RULE, input), vector.name).toEqual({
        level: decision.level,
        pressure: decision.pressure,
        marginMb: decision.marginMb,
      });
    }
  });

  it("a stalling box goes up at once and comes down only after the calm wait", async () => {
    const host = hostOf("linux-v2");
    const machine = await readMachine(host);
    const stalled = capacityFactsOf(machine, {
      pieceCaps: true,
      pieces: [],
      boxPsi: await readBoxPsi(host, machine, "/work.slice"),
    });
    const calm = capacityFactsOf(machine, { pieceCaps: true, pieces: [], boxPsi: null });
    const first = capacityLevel(CAPACITY_RULE, { facts: stalled, nowMs: 0 });
    expect(first.level).toBe("elevated");
    const waiting = capacityLevel(CAPACITY_RULE, {
      facts: calm,
      previous: first.pressure,
      nowMs: 1000,
    });
    expect(waiting.pressure).toEqual({ level: "elevated", calmSinceMs: 1000 });
    const later = capacityLevel(CAPACITY_RULE, {
      facts: calm,
      previous: waiting.pressure,
      nowMs: 1000 + CAPACITY_RULE.pressure.elevatedClearSeconds * 1000,
    });
    expect(later.level).toBe("ok");
    expect(capacityLevel(CAPACITY_RULE, { facts: calm }).level).toBe("ok");
  });
});

describe("capacity readings: the real host", () => {
  it("tells a missing file from an unreadable one and a failed command from output", async () => {
    const host = nodeCapacityHost("linux");
    expect(await host.readFile("/nonexistent-capacity-readings-test/file")).toEqual({
      error: "missing",
    });
    expect(await host.run("/nonexistent-capacity-readings-test/command", [])).toBeNull();
  });

  it("reads this machine when it is Linux", async () => {
    const host = nodeCapacityHost("linux");
    if (!("text" in (await host.readFile("/proc/meminfo")))) return;
    const machine = await readMachine(host);
    expect(machine.totalMb).toBeGreaterThan(0);
    expect(machine.availableMb).toBeLessThanOrEqual(machine.totalMb!);
  });
});
