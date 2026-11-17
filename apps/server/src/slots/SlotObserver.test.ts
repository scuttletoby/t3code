/**
 * Fork: the capacity rule beside the slot gate (`agentSlots.mode: "observe"`): it never changes or
 * delays a decision, reads nothing in `fixed` mode, and the state question (old settings, an unknown
 * mode, readings that fail, hang or come back half, a restart, two decisions at once).
 */
import { assert, describe, it } from "@effect/vitest";
import {
  type OrchestrationV2ThreadShell,
  type OrchestrationV2ThreadShellSnapshot,
  ProviderInstanceId,
  type ServerSettings as ServerSettingsValue,
  ThreadId,
} from "@t3tools/contracts";
import type { SlotRuleStatus } from "@t3tools/contracts/waitFor";
import { AgentScope, type AgentScopeShape } from "@t3tools/shared/AgentScope";
import type { CapacityFileRead, CapacityHost } from "@t3tools/shared/capacityReadings";
import * as HostProcess from "@t3tools/shared/HostProcess";
import type { DeepPartial } from "@t3tools/shared/Struct";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import { TestClock } from "effect/testing";

import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as HostResources from "../resourceTelemetry/HostResources.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as SlotGate from "./SlotGate.ts";
import {
  OBSERVE_MAX_HANGING,
  OBSERVE_MAX_WAITING,
  OBSERVE_READ_TIMEOUT,
  SlotCapacityHost,
  slicePath,
} from "./SlotObserver.ts";

const MAIN = ProviderInstanceId.make("codex");
const MB = 1024 * 1024;
const caller = ThreadId.make("thread:slot-caller");
const UID = 1000;
const SLICE = "app-t3code-agents-0123456789ab.slice";
const BOX = `/sys/fs/cgroup/user.slice/user-${UID}.slice/user@${UID}.service/app.slice/app-t3code.slice/app-t3code-agents.slice/${SLICE}`;
const CALM_PSI =
  "some avg10=0.00 avg60=0.00 avg300=0.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=1\n";

interface World {
  /** Threads the projection shows with an active run; `unit`: their own agent scope. */
  threads: Array<{ id: string; unit?: string }>;
  snapshotReads: number;
  /** The machine: files by path; a path not listed is missing. */
  files: Map<string, string>;
  hostReads: number;
  host: "ok" | "hang" | "throw";
}

/** 16 GB, 12 GB available, swap unused, no pressure: everything fits once the level is calm. */
const newWorld = (): World => ({
  threads: [],
  snapshotReads: 0,
  hostReads: 0,
  host: "ok",
  files: new Map([
    [
      "/proc/meminfo",
      "MemTotal:       16777216 kB\nMemFree:         8388608 kB\nMemAvailable:   12582912 kB\nSwapCached:            0 kB\nSwapTotal:       4194304 kB\nSwapFree:        4194304 kB\n",
    ],
    ["/proc/pressure/memory", CALM_PSI],
    [
      "/proc/self/cgroup",
      "0::/user.slice/user-1000.slice/user@1000.service/app.slice/t3.service\n",
    ],
    ["/sys/fs/cgroup/cgroup.controllers", "cpuset cpu io memory pids\n"],
  ]),
});

/** An agent scope using `mb` (anon), with its box. */
const scopeFiles = (world: World, unit: string, mb: number) => {
  world.files.set(`${BOX}/memory.pressure`, CALM_PSI);
  world.files.set(`${BOX}/${unit}/memory.stat`, `anon ${mb * MB}\nshmem 0\nfile 0\n`);
  world.files.set(`${BOX}/${unit}/memory.swap.current`, "0\n");
  world.files.set(`${BOX}/${unit}/memory.current`, `${mb * MB}\n`);
  world.files.set(`${BOX}/${unit}/memory.peak`, `${mb * MB}\n`);
};

const hostOf = (world: World): CapacityHost => ({
  platform: "linux",
  readFile: (path) => {
    world.hostReads += 1;
    if (world.host === "hang") return new Promise<CapacityFileRead>(() => {});
    if (world.host === "throw") throw new Error("EIO");
    const text = world.files.get(path);
    return Promise.resolve(text === undefined ? { error: "missing" } : { text });
  },
  run: () => Promise.resolve(null),
  totalMemBytes: () => 16 * 1024 * MB,
  freeMemBytes: () => 8 * 1024 * MB,
  cpuCount: () => 8,
});

const scopesOf = (world: World): AgentScopeShape => ({
  wrap: ({ command, args }) => Effect.succeed({ command, args }),
  oomKilled: () => Effect.succeed(false),
  stop: () => Effect.void,
  kill: () => Effect.succeed(false),
  clear: () => Effect.void,
  ownScope: (threadId) =>
    Effect.sync(() => {
      const unit = world.threads.find((thread) => thread.id === threadId)?.unit;
      return unit === undefined ? undefined : { unit, slice: SLICE };
    }),
});

const layerFor = (world: World, settings: DeepPartial<ServerSettingsValue>) =>
  SlotGate.layer.pipe(
    Layer.provideMerge(
      Layer.mergeAll(
        ServerSettings.layerTest(settings),
        Layer.mock(ThreadManagementService.ThreadManagementService)({
          getShellSnapshot: () =>
            Effect.sync(() => {
              world.snapshotReads += 1;
              const shells = world.threads.map(
                (thread) =>
                  ({
                    id: ThreadId.make(thread.id),
                    providerInstanceId: MAIN,
                    activeRunId: `run:${thread.id}`,
                  }) as unknown as OrchestrationV2ThreadShell,
              );
              return {
                schemaVersion: 1,
                snapshotSequence: 1,
                threads: shells,
                archivedThreads: [],
              } satisfies OrchestrationV2ThreadShellSnapshot;
            }),
        }),
        Layer.mock(HostResources.HostResources)({
          read: Effect.succeed({
            sampledAt: 0,
            cpuUtilization: null,
            cpuCount: 8,
            availableMemoryBytes: 12_000 * MB,
            totalMemoryBytes: 16_000 * MB,
          }),
        }),
        Layer.succeed(SlotCapacityHost, hostOf(world)),
        Layer.succeed(AgentScope, scopesOf(world)),
        Layer.succeed(HostProcess.UserId, UID),
        Layer.succeed(HostProcess.Platform, "linux"),
      ),
    ),
  );

const run = <A, E>(
  world: World,
  settings: DeepPartial<ServerSettingsValue>,
  body: Effect.Effect<A, E, SlotGate.SlotGate | ServerSettings.ServerSettingsService>,
) => body.pipe(Effect.provide(layerFor(world, settings)));

const acquire = SlotGate.SlotGate.pipe(
  Effect.flatMap((gate) => gate.acquire({ callerThreadId: caller, targetInstanceId: MAIN })),
);

/** The dispatch ended without naming a child (it failed before one existed). */
const release = (answer: SlotGate.SlotAcquire) =>
  SlotGate.SlotGate.pipe(
    Effect.flatMap((gate) => gate.release(answer.type === "allow" ? answer.token : null)),
  );

/** Lets forked observations and the fake host's promises finish (scheduler turns, no real wait). */
const settle = Effect.forEach(Array.from({ length: 20 }), () => Effect.yieldNow, { discard: true });

/** The status once `observed + skipped` reached `count`. */
const statusAfter = (count: number) =>
  Effect.gen(function* () {
    const gate = yield* SlotGate.SlotGate;
    for (let turn = 0; turn < 200; turn += 1) {
      const status = yield* gate.observed;
      if (status !== null && status.observed + status.skipped >= count) return status;
      yield* settle;
    }
    return yield* Effect.die(new Error(`fewer than ${count} observations`));
  });

const observe = (patch: Record<string, unknown> = {}) => ({
  agentSlots: { mode: "observe", ...patch },
});

/** Two running agents without scopes of their own: past the rule's floor of two. */
const TWO_RUNNING = [{ id: "a" }, { id: "b" }];

/** The calm wait after a (re)start: the level is "elevated" until this much calm has passed. */
const CALM_WAIT = Duration.seconds(61);

describe("SlotGate observe mode", () => {
  it.effect("fixed mode, old files and unknown modes read nothing for the rule", () =>
    Effect.gen(function* () {
      for (const settings of [
        { agentSlots: { mode: "fixed" } },
        // A file from before `mode`: switched on with numbers = fixed, exactly those numbers.
        { agentSlots: { enabled: true, maxPerMachine: 5 } },
        // A mode of a newer build.
        { agentSlots: { mode: "dynamic", enabled: true, maxPerMachine: 5 } },
        { agentSlots: { mode: "dynamic" } },
      ]) {
        const world = newWorld();
        const status = yield* run(
          world,
          settings,
          Effect.gen(function* () {
            const gate = yield* SlotGate.SlotGate;
            assert.strictEqual((yield* acquire).type, "allow");
            yield* gate.grant([{ key: "w1", threadId: ThreadId.make("waiter") }]);
            yield* settle;
            return yield* gate.observed;
          }),
        );
        assert.strictEqual(status, null, JSON.stringify(settings));
        assert.strictEqual(world.hostReads, 0, JSON.stringify(settings));
      }
    }),
  );

  it.effect("off without a mode (the default) observes; the answer stays today's", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const status = yield* run(
        world,
        {},
        Effect.gen(function* () {
          const answer = yield* acquire;
          // A token for the rule's own record of the start (the gate keeps none while off).
          assert.strictEqual(answer.type, "allow");
          assert.isNumber(answer.type === "allow" ? answer.token : null);
          return yield* statusAfter(1);
        }),
      );
      assert.strictEqual(status.observed, 1);
      assert.strictEqual(status.last?.today, "allow");
      assert.isAbove(world.hostReads, 0);
    }),
  );

  it.effect("the observation starts after the decision returns, never on the caller's stack", () =>
    Effect.gen(function* () {
      const world = newWorld();
      // The fake projection read is synchronous: forked to start at once, it would run before
      // the caller got its answer (inside the gate's lock).
      const readsAtAnswer = yield* run(
        world,
        observe(),
        Effect.gen(function* () {
          assert.strictEqual((yield* acquire).type, "allow");
          const reads = world.snapshotReads;
          yield* statusAfter(1);
          return reads;
        }),
      );
      assert.strictEqual(readsAtAnswer, 0);
      assert.strictEqual(world.snapshotReads, 1);
    }),
  );

  it.effect("the same decisions in fixed and observe mode, even with a hanging machine", () =>
    Effect.gen(function* () {
      // Two slots: allow, allow, refuse; a release frees one; a slot wait is granted, then full.
      const script = Effect.gen(function* () {
        const gate = yield* SlotGate.SlotGate;
        const answers: Array<unknown> = [];
        const first = yield* acquire;
        const second = yield* acquire;
        answers.push(first, second, yield* acquire);
        if (second.type === "allow") yield* gate.release(second.token);
        answers.push(
          yield* gate.grant([
            { key: "w1", threadId: ThreadId.make("waiter-1") },
            { key: "w2", threadId: ThreadId.make("waiter-2") },
          ]),
        );
        answers.push(yield* acquire, yield* gate.usage);
        return answers;
      });
      const outcomes = [];
      for (const [mode, host] of [
        ["fixed", "ok"],
        ["observe", "ok"],
        ["observe", "hang"],
        ["observe", "throw"],
      ] as const) {
        const world = newWorld();
        world.host = host;
        world.threads = [{ id: "a", unit: "t3code-claude-aaaa0000.scope" }];
        scopeFiles(world, "t3code-claude-aaaa0000.scope", 300);
        // No clock is moved: a decision that waited for the readings would never end here.
        outcomes.push(
          yield* run(
            world,
            {
              agentSlots: { enabled: true, mode, maxPerMachine: 3, reserveSeconds: 60 },
              waitFor: { enabled: true },
            },
            script,
          ),
        );
      }
      for (const outcome of outcomes.slice(1)) assert.deepStrictEqual(outcome, outcomes[0]);
      assert.deepStrictEqual(
        (outcomes[0] as Array<{ type?: string }>).slice(0, 3).map((answer) => answer.type),
        ["allow", "allow", "refuse"],
      );
    }),
  );

  it.effect("a restart starts elevated: the rule refuses until the machine stayed calm", () =>
    Effect.gen(function* () {
      const world = newWorld();
      // Two agents run, so the rule's floor (the first two always pass) does not apply.
      world.threads = TWO_RUNNING;
      const [first, second] = yield* run(
        world,
        observe(),
        Effect.gen(function* () {
          yield* acquire;
          const first = yield* statusAfter(1);
          yield* TestClock.adjust(CALM_WAIT);
          yield* acquire;
          return [first, yield* statusAfter(2)] as const;
        }),
      );
      assert.deepInclude(first.last, {
        today: "allow",
        rule: "refuse",
        reason: "elevated",
        level: "elevated",
        readings: "ok",
        pieces: 2,
      });
      assert.strictEqual(first.disagreed, 1);
      assert.deepInclude(second.last, { rule: "allow", reason: "fits", level: "ok" });
      assert.strictEqual(second.observed, 2);
      // A new gate (a server restart) remembers nothing: elevated again.
      const after = yield* run(world, observe(), Effect.andThen(acquire, statusAfter(1)));
      assert.deepInclude(after.last, { rule: "refuse", reason: "elevated" });
      assert.strictEqual(after.observed, 1);
    }),
  );

  it.effect("a hanging machine: never delays, reads as slow, then busy; skips past the queue", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.host = "hang";
      const count = OBSERVE_MAX_WAITING + 2;
      const status = yield* run(
        world,
        observe(),
        Effect.gen(function* () {
          // All answered at once, with the first observation's read still hanging.
          for (let index = 0; index < count; index += 1) {
            assert.strictEqual((yield* acquire).type, "allow");
          }
          const gate = yield* SlotGate.SlotGate;
          yield* settle;
          const queued = yield* gate.observed;
          assert.strictEqual(queued?.observed, 0);
          assert.strictEqual(queued?.skipped, count - OBSERVE_MAX_WAITING);
          for (let index = 0; index < OBSERVE_MAX_WAITING; index += 1) {
            yield* TestClock.adjust(OBSERVE_READ_TIMEOUT);
            yield* settle;
          }
          return yield* statusAfter(count);
        }),
      );
      assert.strictEqual(status.observed, OBSERVE_MAX_WAITING);
      assert.deepInclude(status.last, {
        today: "allow",
        rule: "refuse",
        reason: "unreadable",
        level: "elevated",
        freeMb: null,
        // The first reads hang for good; then no new read starts.
        readings: OBSERVE_MAX_WAITING > OBSERVE_MAX_HANGING ? "busy" : "slow",
      });
    }),
  );

  it.effect("readings that fail or come back half are unreadable, never fits", () =>
    Effect.gen(function* () {
      for (const breakIt of [
        (world: World) => {
          world.host = "throw";
        },
        (world: World) => {
          world.files.set("/proc/meminfo", "MemTotal:       16777216 kB\nMemFree:  ");
        },
        (world: World) => {
          world.files.delete("/sys/fs/cgroup/cgroup.controllers");
          world.files.set("/proc/pressure/memory", "some avg10=0.00");
        },
      ]) {
        const world = newWorld();
        world.threads = TWO_RUNNING;
        breakIt(world);
        const status = yield* run(
          world,
          observe(),
          Effect.gen(function* () {
            yield* TestClock.adjust(CALM_WAIT);
            yield* acquire;
            yield* TestClock.adjust(CALM_WAIT);
            yield* acquire;
            return yield* statusAfter(2);
          }),
        );
        assert.strictEqual(status.last?.rule, "refuse");
        assert.include(["unreadable", "elevated"], status.last?.reason);
        assert.notStrictEqual(status.last?.level, "ok");
      }
    }),
  );

  it.effect("a scope that reads as gone keeps its piece while the records say it runs (#307)", () =>
    Effect.gen(function* () {
      const owed = [];
      for (const gone of [false, true]) {
        const world = newWorld();
        world.threads = [{ id: "a", unit: "t3code-claude-aaaa0000.scope" }];
        scopeFiles(world, "t3code-claude-aaaa0000.scope", 300);
        if (gone) world.files.delete(`${BOX}/t3code-claude-aaaa0000.scope/memory.stat`);
        const status = yield* run(world, observe(), Effect.andThen(acquire, statusAfter(1)));
        assert.strictEqual(status.last?.pieces, 1);
        owed.push(status.last?.owedMb);
      }
      // Measured at 300 MB it owes the rest of the writer's 768; gone, it owes all of it.
      assert.deepStrictEqual(owed, [468, 768]);
    }),
  );

  it.effect("no live piece of its own: no box pressure is read (a box that is not there yet)", () =>
    Effect.gen(function* () {
      const world = newWorld();
      // A running agent whose scope reads as gone, in a box with no pressure file.
      world.threads = [{ id: "a", unit: "t3code-claude-aaaa0000.scope" }];
      const status = yield* run(
        world,
        observe(),
        Effect.gen(function* () {
          yield* release(yield* acquire);
          yield* TestClock.adjust(CALM_WAIT);
          yield* acquire;
          return yield* statusAfter(2);
        }),
      );
      assert.deepInclude(status.last, { rule: "allow", reason: "fits", level: "ok", pieces: 1 });
    }),
  );

  it.effect("slots on: the gate's own records count a new start, once", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const status = yield* run(
        world,
        {
          agentSlots: { enabled: true, mode: "observe", maxPerMachine: 5 },
        },
        Effect.gen(function* () {
          yield* acquire;
          yield* acquire;
          return yield* statusAfter(2);
        }),
      );
      // The first allow is in the gate's records when the second is decided; not counted twice.
      assert.deepInclude(status.last, { pieces: 1, owedMb: 768 });
    }),
  );

  it.effect("slots off: a start counts until its dispatch ends, then for the start grace", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const [during, grace, gone] = yield* run(
        world,
        observe({ startGraceSeconds: 30 }),
        Effect.gen(function* () {
          const gate = yield* SlotGate.SlotGate;
          // A dispatch that has not named its child yet counts, however long it takes.
          const first = yield* acquire;
          yield* TestClock.adjust(Duration.seconds(45));
          yield* acquire;
          const during = yield* statusAfter(2);
          // The child is named and the dispatch ends; it shows no run yet (a queued run).
          const token = first.type === "allow" ? first.token : null;
          yield* gate.started(token, ThreadId.make("child-queued"));
          yield* gate.release(token);
          yield* TestClock.adjust(Duration.seconds(29));
          yield* release(yield* acquire);
          const grace = yield* statusAfter(3);
          yield* TestClock.adjust(Duration.seconds(2));
          yield* acquire;
          return [during, grace, yield* statusAfter(4)] as const;
        }),
      );
      assert.strictEqual(during.last?.pieces, 1);
      // At 74 s: the queued child is 29 s past its dispatch's end, inside its 30 s grace; the
      // second start (45 s) never ended.
      assert.strictEqual(grace.last?.pieces, 2);
      // At 76 s: the queued child's grace is over; the third start ended with no child at once.
      assert.strictEqual(gone.last?.pieces, 1);
    }),
  );

  it.effect("slots off: a start whose child shows its run counts once, as that run (#353)", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const status = yield* run(
        world,
        observe({ startGraceSeconds: 30 }),
        Effect.gen(function* () {
          const gate = yield* SlotGate.SlotGate;
          const first = yield* acquire;
          const token = first.type === "allow" ? first.token : null;
          yield* gate.started(token, ThreadId.make("child"));
          // The child shows its run (300 MB in its own scope) inside the 30 s window.
          world.threads = [{ id: "child", unit: "t3code-claude-cccc0000.scope" }];
          scopeFiles(world, "t3code-claude-cccc0000.scope", 300);
          yield* TestClock.adjust(Duration.seconds(5));
          // A start whose dispatch ends with no child, so only the first agent is left.
          yield* release(yield* acquire);
          const running = yield* statusAfter(2);
          // Its dispatch ends after the run showed: still counted once, as the run.
          yield* gate.release(token);
          yield* TestClock.adjust(Duration.seconds(5));
          yield* acquire;
          return [running, yield* statusAfter(3)] as const;
        }),
      );
      // Not also as starting: that would be 2 pieces, owing 768 MB more.
      for (const observed of status) assert.deepInclude(observed.last, { pieces: 1, owedMb: 468 });
    }),
  );

  it.effect("slots off: a start whose run showed and ended stops counting, grace or not", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const status = yield* run(
        world,
        observe({ startGraceSeconds: 30 }),
        Effect.gen(function* () {
          const gate = yield* SlotGate.SlotGate;
          const first = yield* acquire;
          const token = first.type === "allow" ? first.token : null;
          yield* gate.started(token, ThreadId.make("child"));
          yield* gate.release(token);
          // Its run shows, then ends, all inside the 30 s grace of its dispatch's end.
          world.threads = [{ id: "child", unit: "t3code-claude-cccc0000.scope" }];
          scopeFiles(world, "t3code-claude-cccc0000.scope", 300);
          yield* TestClock.adjust(Duration.seconds(5));
          yield* release(yield* acquire);
          yield* statusAfter(2);
          world.threads = [];
          yield* TestClock.adjust(Duration.seconds(5));
          yield* release(yield* acquire);
          return yield* statusAfter(3);
        }),
      );
      // A finished agent: not counted again as starting for the rest of its grace.
      assert.strictEqual(status.last?.pieces, 0);
    }),
  );

  it.effect("slots off: a child named while the observation waits is not counted twice", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const status = yield* run(
        world,
        observe({ startGraceSeconds: 30 }),
        Effect.gen(function* () {
          const gate = yield* SlotGate.SlotGate;
          const first = yield* acquire;
          yield* statusAfter(1);
          yield* TestClock.adjust(Duration.seconds(5));
          // Decided while the first start has no child yet; observed after it shows its run.
          yield* acquire;
          yield* gate.started(first.type === "allow" ? first.token : null, ThreadId.make("child"));
          world.threads = [{ id: "child", unit: "t3code-claude-cccc0000.scope" }];
          scopeFiles(world, "t3code-claude-cccc0000.scope", 300);
          return yield* statusAfter(2);
        }),
      );
      assert.deepInclude(status.last, { pieces: 1, owedMb: 468 });
    }),
  );

  it.effect("slots off: a dispatch that ends with no child stops counting at once", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const status = yield* run(
        world,
        observe({ startGraceSeconds: 30 }),
        Effect.gen(function* () {
          yield* release(yield* acquire);
          yield* TestClock.adjust(Duration.seconds(1));
          yield* acquire;
          return yield* statusAfter(2);
        }),
      );
      assert.deepInclude(status.last, { pieces: 0 });
    }),
  );

  it.effect("held slots in the wait store count as the gate counts them (a restart, #353)", () =>
    Effect.gen(function* () {
      const HOLD = { key: "w_held", threadId: "waiter", untilMs: 60_000 };
      const cases = [
        // After a restart the store has the only copy of a delivered slot wait's hold.
        { name: "slots off", agentSlots: {}, holds: [HOLD], caller, pieces: 1, reads: 1 },
        {
          name: "slots on",
          agentSlots: { enabled: true, maxPerMachine: 5 },
          holds: [HOLD],
          caller,
          pieces: 1,
          // The gate's own decision reads it too.
          reads: 2,
        },
        // The holder's own delegation uses its hold up: counted once, as the new start.
        {
          name: "the holder",
          agentSlots: {},
          holds: [HOLD],
          caller: "waiter",
          pieces: 0,
          reads: 1,
        },
        {
          name: "an ended hold",
          agentSlots: {},
          holds: [{ ...HOLD, untilMs: 0 }],
          caller,
          pieces: 0,
          reads: 1,
        },
        { name: "an empty store", agentSlots: {}, holds: [], caller, pieces: 0, reads: 1 },
        // Waits off: the gate counts no hold, and nothing of the store is read.
        {
          name: "waits off",
          agentSlots: {},
          holds: [HOLD],
          caller,
          pieces: 0,
          reads: 0,
          waitsOff: true,
        },
      ];
      for (const entry of cases) {
        const world = newWorld();
        let reads = 0;
        const status = yield* run(
          world,
          {
            agentSlots: { mode: "observe", ...entry.agentSlots },
            waitFor: { enabled: entry.waitsOff !== true },
          },
          Effect.gen(function* () {
            const gate = yield* SlotGate.SlotGate;
            yield* gate.attachReservations({
              read: Effect.sync(() => {
                reads += 1;
                return entry.holds;
              }),
              used: () => Effect.void,
            });
            const answer = yield* gate.acquire({
              callerThreadId: ThreadId.make(entry.caller),
              targetInstanceId: MAIN,
            });
            assert.strictEqual(answer.type, "allow", entry.name);
            return yield* statusAfter(1);
          }),
        );
        assert.strictEqual(status.last?.pieces, entry.pieces, entry.name);
        assert.strictEqual(reads, entry.reads, entry.name);
      }
    }),
  );

  it.effect("a wait store read that hangs never delays the decision; the rule reads slow", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const status = yield* run(
        world,
        { ...observe(), waitFor: { enabled: true } },
        Effect.gen(function* () {
          const gate = yield* SlotGate.SlotGate;
          yield* gate.attachReservations({ read: Effect.never, used: () => Effect.void });
          // No clock is moved before the answer: a decision that waited would never end here.
          assert.strictEqual((yield* acquire).type, "allow");
          yield* settle;
          yield* TestClock.adjust(OBSERVE_READ_TIMEOUT);
          return yield* statusAfter(1);
        }),
      );
      assert.deepInclude(status.last, { rule: "refuse", reason: "unreadable", readings: "slow" });
    }),
  );

  it.effect("two decisions at once: each observed in order, the second counting the first", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const status = yield* run(
        world,
        observe(),
        Effect.gen(function* () {
          yield* TestClock.adjust(CALM_WAIT);
          yield* release(yield* acquire);
          yield* TestClock.adjust(CALM_WAIT);
          const both = yield* Effect.all([acquire, acquire], { concurrency: "unbounded" });
          assert.deepStrictEqual(
            both.map((answer) => answer.type),
            ["allow", "allow"],
          );
          return yield* statusAfter(3);
        }),
      );
      assert.strictEqual(status.observed, 3);
      // The second of the pair saw the first one's starting entry: 768 MB more owed.
      assert.strictEqual(status.last?.pieces, 1);
      assert.strictEqual(status.last?.owedMb, 768);
    }),
  );

  it.effect("slot waits: each granted one counts for the next in the same call", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const status: SlotRuleStatus = yield* run(
        world,
        {
          agentSlots: { enabled: true, mode: "observe", maxPerMachine: 5, reserveSeconds: 60 },
          waitFor: { enabled: true },
        },
        Effect.gen(function* () {
          const gate = yield* SlotGate.SlotGate;
          const grants = yield* gate.grant([
            { key: "w1", threadId: ThreadId.make("waiter-1") },
            { key: "w2", threadId: ThreadId.make("waiter-2") },
          ]);
          assert.deepStrictEqual(
            grants.map((answer) => answer.type),
            ["granted", "granted"],
          );
          return yield* statusAfter(2);
        }),
      );
      assert.strictEqual(status.observed, 2);
      assert.strictEqual(status.last?.owedMb, 768);
      assert.strictEqual(status.last?.pieces, 0);
    }),
  );

  it("nests a systemd slice the way systemd does", () => {
    assert.strictEqual(
      slicePath("app-t3code-agents-0123.slice"),
      "/app.slice/app-t3code.slice/app-t3code-agents.slice/app-t3code-agents-0123.slice",
    );
  });
});
