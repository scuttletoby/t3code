/**
 * Fork: the capacity rule beside the slot gate (`agentSlots.mode: "observe"`): it never changes or
 * delays a decision, reads nothing in `fixed` mode, and the state question (old settings, an unknown
 * mode, readings that fail, hang or come back half, a restart, two decisions at once).
 */
import { assert, describe, it } from "@effect/vitest";
import {
  DEFAULT_SERVER_SETTINGS,
  type OrchestrationV2ThreadShell,
  type OrchestrationV2ThreadShellSnapshot,
  ProviderInstanceId,
  type ServerSettings as ServerSettingsValue,
  ServerSettingsError,
  ThreadId,
} from "@t3tools/contracts";
import { type SlotRuleStatus, WaitToolFailure } from "@t3tools/contracts/waitFor";
import { AgentScope, type AgentScopeShape } from "@t3tools/shared/AgentScope";
import type { CapacityFileRead, CapacityHost } from "@t3tools/shared/capacityReadings";
import * as HostProcess from "@t3tools/shared/HostProcess";
import type { DeepPartial } from "@t3tools/shared/Struct";
import { DISPATCH_SAFETY_MS, type SlotReservation } from "@t3tools/shared/agentSlots";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import { TestClock } from "effect/testing";

import * as OrchestratorV2 from "../orchestration-v2/Orchestrator.ts";
import * as HostResources from "../resourceTelemetry/HostResources.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as ActiveRunShells from "./ActiveRunShells.ts";
import * as SlotGate from "./SlotGate.ts";
import {
  makeSlotObserver,
  OBSERVE_MAX_HANGING,
  OBSERVE_MAX_WAITING,
  OBSERVE_READ_TIMEOUT,
  SlotCapacityHost,
  type SlotObserveInput,
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
  /** The thread list cannot be read (the count read fails). */
  snapshotFails?: boolean;
  /** The machine: files by path; a path not listed is missing. */
  files: Map<string, string>;
  hostReads: number;
  host: "ok" | "hang" | "throw";
  /** No agent scopes of their own (Linux without systemd, macOS, Windows). */
  noScopes?: boolean;
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
  ...(world.noScopes === true
    ? {}
    : {
        ownScope: (threadId: string) =>
          Effect.sync(() => {
            const unit = world.threads.find((thread) => thread.id === threadId)?.unit;
            return unit === undefined ? undefined : { unit, slice: SLICE };
          }),
      }),
});

const layerFor = (
  world: World,
  settings: DeepPartial<ServerSettingsValue>,
  settingsLayer: Layer.Layer<
    ServerSettings.ServerSettingsService,
    ServerSettingsError
  > = ServerSettings.layerTest(settings),
) =>
  SlotGate.layerOver.pipe(
    Layer.provideMerge(
      Layer.mergeAll(
        settingsLayer,
        Layer.mock(ActiveRunShells.ActiveRunShells)({
          read: Effect.suspend(() => {
            world.snapshotReads += 1;
            if (world.snapshotFails) {
              return Effect.fail(
                new OrchestratorV2.OrchestratorProjectionError({ threadId: caller }),
              );
            }
            const shells = world.threads.map(
              (thread) =>
                ({
                  id: ThreadId.make(thread.id),
                  providerInstanceId: MAIN,
                  activeRunId: `run:${thread.id}`,
                }) as unknown as OrchestrationV2ThreadShell,
            );
            const snapshot = {
              schemaVersion: 1,
              snapshotSequence: 1,
              threads: shells,
              archivedThreads: [],
            } satisfies OrchestrationV2ThreadShellSnapshot;
            return Effect.succeed(snapshot);
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

/** A dispatch that ends with no child; its answer. */
const acquireAndRelease = Effect.tap(acquire, release).pipe(Effect.map((answer) => answer.type));

/** Lets forked observations and the fake host's promises finish (scheduler turns, no real wait). */
const settle = Effect.forEach(Array.from({ length: 20 }), () => Effect.yieldNow, { discard: true });

/** `status` once `observed + skipped` reached `count` (scheduler turns only, at most 200). */
const counted = <S extends { readonly observed: number; readonly skipped: number }>(
  status: Effect.Effect<S | null>,
  count: number,
  name = "",
) =>
  Effect.gen(function* () {
    for (let turn = 0; turn < 200; turn += 1) {
      const current = yield* status;
      if (current !== null && current.observed + current.skipped >= count) return current;
      yield* settle;
    }
    return yield* Effect.die(new Error(`${name}fewer than ${count} observations`));
  });

/** The gate's status once `observed + skipped` reached `count`. */
const statusAfter = (count: number) =>
  SlotGate.SlotGate.pipe(Effect.flatMap((gate) => counted(gate.observed, count)));

const observe = (patch: Record<string, unknown> = {}) => ({
  agentSlots: { mode: "observe", ...patch },
});

/** Two running agents without scopes of their own: past the rule's floor of two. */
const TWO_RUNNING = [{ id: "a" }, { id: "b" }];

/** The calm wait after a (re)start: the level is "elevated" until this much calm has passed. */
const CALM_WAIT = Duration.seconds(61);

/** The observer alone, its projection read as `readRunning` (the order rows drive it by hand). */
const observerFor = (
  world: World,
  readRunning: Effect.Effect<
    { readonly visible: ReadonlySet<string> } | undefined
  > = Effect.succeed({ visible: new Set<string>() }),
) =>
  makeSlotObserver(readRunning).pipe(
    Effect.provide(
      Layer.mergeAll(
        Layer.succeed(SlotCapacityHost, hostOf(world)),
        Layer.succeed(AgentScope, scopesOf(world)),
        Layer.succeed(HostProcess.UserId, UID),
        Layer.succeed(HostProcess.Platform, "linux"),
      ),
    ),
  );

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

  it.effect("off in observe mode observes; the answer stays today's", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const status = yield* run(
        world,
        observe(),
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

  it.effect(
    "a thread list that cannot be read: refused in observe mode too; the rule sees it",
    () =>
      Effect.gen(function* () {
        const world = newWorld();
        world.snapshotFails = true;
        const [refused, first, allowed, second] = yield* run(
          world,
          {
            agentSlots: { enabled: true, mode: "observe", maxPerMachine: 5, minFreeMemoryMb: 100 },
          },
          Effect.gen(function* () {
            const refused = yield* acquire;
            const first = yield* statusAfter(1);
            // Runs again: the list reads now, and the refusal left no record behind.
            world.snapshotFails = false;
            const allowed = yield* acquire;
            return [refused, first, allowed, yield* statusAfter(2)] as const;
          }),
        );
        assert.deepStrictEqual(refused, {
          type: "refuse",
          message: SlotGate.countUnreadRefusalText("error"),
          refusal: { type: "unread", problem: "error" },
        });
        // The rule beside it got today's refusal and could not read the pieces either.
        assert.deepInclude(first.last, { today: "refuse", readings: "failed", pieces: null });
        assert.strictEqual(allowed.type, "allow");
        // The refusal left no start in the gate's records: the allowed call sees none before it.
        assert.deepInclude(second.last, { today: "allow", pieces: 0 });
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

  it.effect("a held slot its holder's start used up counts once, slots off as on (#381)", () =>
    Effect.gen(function* () {
      const HOLD = { key: "w_held", threadId: "waiter", untilMs: 100_000 };
      const waiter = ThreadId.make("waiter");
      const cases = [
        { name: "slots on", agentSlots: { enabled: true, maxPerMachine: 5 }, slowFirst: false },
        // Slots off: the gate never uses the hold up, so it stays in the store.
        { name: "slots off", agentSlots: {}, slowFirst: false },
        // The holder's own observation reads the store too late: the next one still sees the start.
        { name: "slots off, first read slow", agentSlots: {}, slowFirst: true },
      ];
      for (const entry of cases) {
        const world = newWorld();
        let reads = 0;
        const counts = yield* run(
          world,
          {
            agentSlots: { mode: "observe", startGraceSeconds: 30, ...entry.agentSlots },
            waitFor: { enabled: true },
          },
          Effect.gen(function* () {
            const gate = yield* SlotGate.SlotGate;
            yield* gate.attachReservations({
              read: Effect.suspend(() => {
                reads += 1;
                return entry.slowFirst && reads === 1 ? Effect.never : Effect.succeed([HOLD]);
              }),
              used: () => Effect.void,
            });
            const answers: Array<string> = [];
            // The holder delegates; another thread delegates 5 s later.
            const own = yield* gate.acquire({ callerThreadId: waiter, targetInstanceId: MAIN });
            answers.push(own.type);
            if (entry.slowFirst) {
              yield* settle;
              yield* TestClock.adjust(OBSERVE_READ_TIMEOUT);
              yield* statusAfter(1);
            }
            yield* TestClock.adjust(Duration.seconds(5));
            answers.push(yield* acquireAndRelease);
            const started = (yield* statusAfter(2)).last?.pieces;
            // Its child shows its run: counted as that run, the hold still used up.
            const token = own.type === "allow" ? own.token : null;
            yield* gate.started(token, ThreadId.make("child"));
            world.threads = [{ id: "child" }];
            yield* TestClock.adjust(Duration.seconds(5));
            answers.push(yield* acquireAndRelease);
            const running = (yield* statusAfter(3)).last?.pieces;
            // The child's run ends: nothing of it counts, nor the hold it used.
            yield* gate.release(token);
            world.threads = [];
            yield* TestClock.adjust(Duration.seconds(5));
            answers.push(yield* acquireAndRelease);
            const ended = (yield* statusAfter(4)).last?.pieces;
            // Observe data only: every answer stays today's allow.
            assert.deepStrictEqual(answers, ["allow", "allow", "allow", "allow"], entry.name);
            return [started, running, ended];
          }),
        );
        assert.deepStrictEqual(counts, [1, 1, 0], entry.name);
      }
      // The holder's dispatch ends with no child before anyone else asks: nothing counts.
      for (const agentSlots of [{ enabled: true, maxPerMachine: 5 }, {}]) {
        const status = yield* run(
          newWorld(),
          { agentSlots: { mode: "observe", ...agentSlots }, waitFor: { enabled: true } },
          Effect.gen(function* () {
            const gate = yield* SlotGate.SlotGate;
            yield* gate.attachReservations({
              read: Effect.succeed([HOLD]),
              used: () => Effect.void,
            });
            yield* release(yield* gate.acquire({ callerThreadId: waiter, targetInstanceId: MAIN }));
            yield* TestClock.adjust(Duration.seconds(5));
            yield* acquireAndRelease;
            return yield* statusAfter(2);
          }),
        );
        assert.strictEqual(status.last?.pieces, 0, JSON.stringify(agentSlots));
      }
    }),
  );

  it.effect("slots off: a start uses its thread's hold up in every order", () =>
    Effect.gen(function* () {
      const HOLD = { key: "w_held", threadId: "waiter", untilMs: 100_000 };
      // What another thread's decision 5 s after the holder's start counts. Slots on reads the
      // same, as the gate uses the hold up at the holder's decision (the gate test above: 0 once
      // the dispatch ended with no child, 1 while the child runs).
      const cases = [
        // The holder's own store read is slow, or its projection read fails; then its dispatch
        // ends with no child, so the start stops counting before any read sees it.
        { own: "slow", after: "no child", pieces: 0 },
        { own: "fails", after: "no child", pieces: 0 },
        // The holder's observation is skipped (the queue is full); its child shows its run
        // before the next decision, while the queued observations still read.
        { own: "skipped", after: "child runs", pieces: 1 },
        // The child's run shows before the next store read: the start no longer counts, but it
        // still used its hold up (counted as the child's run, once).
        { own: "slow", after: "child runs", pieces: 1 },
      ] as const;
      const got: Array<string> = [];
      for (const entry of cases) {
        const name = `own ${entry.own}, ${entry.after}`;
        const world = newWorld();
        let visible = new Set<string>();
        let runningFails = false;
        const observer = yield* observerFor(
          world,
          Effect.sync(() => (runningFails ? undefined : { visible })),
        );
        const decide = (atMs: number, patch: Partial<SlotObserveInput>) =>
          observer.observe({
            today: ["allow"],
            atMs,
            starting: [],
            held: Effect.succeed([HOLD]),
            startGraceMs: 30_000,
            gateRecords: false,
            ...patch,
          });
        const done = (count: number) => counted(observer.status, count, `${name}: `);
        let decisions = 0;
        if (entry.own === "skipped") {
          // Other threads' observations fill the queue; the first one's store read takes 1 s.
          for (let index = 0; index < OBSERVE_MAX_WAITING; index += 1) {
            yield* decide(0, {
              callerThreadId: `other-${index}`,
              held: Effect.as(Effect.sleep(Duration.seconds(1)), [HOLD]),
            });
            decisions += 1;
          }
        }
        runningFails = entry.own === "fails";
        yield* decide(0, {
          callerThreadId: "waiter",
          ownStart: { token: 1, instanceId: MAIN, atMs: 0 },
          held: entry.own === "slow" ? Effect.never : Effect.succeed([HOLD]),
        });
        decisions += 1;
        yield* settle;
        if (entry.own === "slow") yield* TestClock.adjust(OBSERVE_READ_TIMEOUT);
        if (entry.own !== "skipped") yield* done(decisions);
        runningFails = false;
        if (entry.after === "no child") {
          yield* observer.released(1, 1_000);
        } else {
          yield* observer.started(1, "child");
          visible = new Set(["child"]);
        }
        for (let index = 0; entry.own === "skipped" && index < OBSERVE_MAX_WAITING; index += 1) {
          yield* TestClock.adjust(Duration.seconds(1));
          yield* settle;
        }
        yield* done(decisions);
        yield* decide(5_000, { callerThreadId: "other" });
        const status = yield* done(decisions + 1);
        assert.strictEqual(status.skipped, entry.own === "skipped" ? 1 : 0, name);
        assert.strictEqual(status.last?.readings, "ok", name);
        got.push(`${name}: ${status.last?.pieces}`);
      }
      assert.deepStrictEqual(
        got,
        cases.map((entry) => `own ${entry.own}, ${entry.after}: ${entry.pieces}`),
      );
    }).pipe(Effect.scoped),
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

  it.effect("the rule's memory keeps only what still counts (#381)", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const HOLD = { key: "w_held", threadId: "waiter", untilMs: 60_000 };
      const observer = yield* observerFor(world);
      let decided = 0;
      /** One decision, observed to the end. */
      const decide = (input: Partial<SlotObserveInput> & { atMs: number }) =>
        Effect.gen(function* () {
          yield* observer.observe({
            today: ["allow"],
            starting: [],
            held: Effect.succeed([HOLD]),
            startGraceMs: 30_000,
            gateRecords: false,
            ...input,
          });
          decided += 1;
          yield* counted(observer.status, decided);
        });
      // Slots off: the holder starts (a dispatch that never ends: a lost release).
      yield* decide({
        atMs: 0,
        callerThreadId: "waiter",
        ownStart: { token: 1, instanceId: MAIN, atMs: 0 },
      });
      assert.deepStrictEqual(yield* observer.kept, { starts: 1, usedHolds: 1, notes: 0 }, "kept");
      // Past its hold's end and the dispatch's safety limit: the next decision lets both go.
      yield* decide({ atMs: DISPATCH_SAFETY_MS, held: Effect.succeed([]) });
      assert.deepStrictEqual(yield* observer.kept, { starts: 0, usedHolds: 0, notes: 0 }, "ended");
      // Slots switched on (the gate keeps the records): the rule's own go at once.
      yield* decide({
        atMs: DISPATCH_SAFETY_MS + 1,
        callerThreadId: "waiter",
        ownStart: { token: 2, instanceId: MAIN, atMs: DISPATCH_SAFETY_MS + 1 },
        held: Effect.succeed([{ ...HOLD, untilMs: 2 * DISPATCH_SAFETY_MS }]),
      });
      yield* decide({ atMs: DISPATCH_SAFETY_MS + 2, gateRecords: true });
      assert.deepStrictEqual(
        yield* observer.kept,
        { starts: 0, usedHolds: 0, notes: 0 },
        "slots on",
      );
      // Switched on while a slots-off observation still waits: the slots-on one counts the hold
      // as the gate does (the gate never used it up).
      const ON = DISPATCH_SAFETY_MS + 3;
      yield* observer.observe({
        today: ["allow"],
        atMs: ON,
        starting: [],
        held: Effect.succeed([{ ...HOLD, untilMs: 2 * DISPATCH_SAFETY_MS }]),
        callerThreadId: "waiter",
        startGraceMs: 30_000,
        gateRecords: false,
        ownStart: { token: 3, instanceId: MAIN, atMs: ON },
      });
      decided += 1;
      yield* decide({
        atMs: ON,
        gateRecords: true,
        held: Effect.succeed([{ ...HOLD, untilMs: 2 * DISPATCH_SAFETY_MS }]),
      });
      assert.strictEqual((yield* observer.status).last?.pieces, 1, "switched on");
      // A start's note that no store read could take (every read hangs): it goes after the
      // dispatch's safety limit, or at once when slots switch on.
      const slow = yield* observerFor(world);
      let slowDecided = 0;
      const decideSlow = (input: Partial<SlotObserveInput> & { atMs: number }) =>
        Effect.gen(function* () {
          yield* slow.observe({
            today: ["allow"],
            starting: [],
            held: Effect.never,
            startGraceMs: 30_000,
            gateRecords: false,
            ...input,
          });
          slowDecided += 1;
          yield* settle;
          yield* TestClock.adjust(OBSERVE_READ_TIMEOUT);
          yield* counted(slow.status, slowDecided);
        });
      const startAt = (atMs: number, token: number) => ({
        atMs,
        callerThreadId: "waiter",
        ownStart: { token, instanceId: MAIN, atMs },
      });
      yield* decideSlow(startAt(0, 1));
      assert.deepStrictEqual(yield* slow.kept, { starts: 1, usedHolds: 0, notes: 1 }, "unread");
      yield* decideSlow({ atMs: DISPATCH_SAFETY_MS });
      assert.deepStrictEqual(yield* slow.kept, { starts: 0, usedHolds: 0, notes: 0 }, "limit");
      yield* decideSlow(startAt(DISPATCH_SAFETY_MS + 1, 2));
      yield* decideSlow({ atMs: DISPATCH_SAFETY_MS + 2, gateRecords: true });
      assert.deepStrictEqual(yield* slow.kept, { starts: 0, usedHolds: 0, notes: 0 }, "slots on");
    }).pipe(Effect.scoped),
  );

  it.effect("a wait store that cannot be read is no good read: the start's note stays (#415)", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const HOLD = { key: "w_held", threadId: caller, untilMs: 100_000 };
      let failing = true;
      const statuses = yield* run(
        world,
        { ...observe(), waitFor: { enabled: true } },
        Effect.gen(function* () {
          const gate = yield* SlotGate.SlotGate;
          yield* gate.attachReservations({
            read: Effect.suspend(() =>
              failing
                ? Effect.fail(
                    new WaitToolFailure({ code: "store_unavailable", message: "newer version" }),
                  )
                : Effect.succeed([HOLD]),
            ),
            used: () => Effect.void,
          });
          // The holder starts while the store cannot be read; its dispatch keeps running.
          assert.strictEqual((yield* acquire).type, "allow");
          const unread = yield* statusAfter(1);
          failing = false;
          // Another thread's decision: the start counts once, its hold is used up (not both).
          yield* gate.acquire({ callerThreadId: ThreadId.make("other"), targetInstanceId: MAIN });
          return [unread, yield* statusAfter(2)] as const;
        }),
      );
      assert.deepInclude(statuses[0].last, { readings: "failed", pieces: null });
      assert.deepInclude(statuses[1].last, { readings: "ok", pieces: 1 });
    }),
  );

  it.effect(
    "a decision outside observe mode drops the notes: a later hold still counts (#415)",
    () =>
      Effect.gen(function* () {
        const world = newWorld();
        let hang = true;
        let inStore: ReadonlyArray<SlotReservation> = [];
        const status = yield* run(
          world,
          { ...observe(), waitFor: { enabled: true } },
          Effect.gen(function* () {
            const gate = yield* SlotGate.SlotGate;
            const settings = yield* ServerSettings.ServerSettingsService;
            yield* gate.attachReservations({
              read: Effect.suspend(() => (hang ? Effect.never : Effect.succeed(inStore))),
              used: () => Effect.void,
            });
            // Slots off: the caller starts (a note), its store read hangs so no read takes the
            // note, and its dispatch ends with no child.
            yield* release(yield* acquire);
            yield* settle;
            yield* TestClock.adjust(OBSERVE_READ_TIMEOUT);
            yield* statusAfter(1);
            // Fixed mode with a limit: the same thread is granted a new hold, which is written.
            hang = false;
            yield* settings.updateSettings({
              agentSlots: { mode: "fixed", enabled: true, maxPerMachine: 5 },
            });
            const grants = yield* gate.grant([{ key: "w_new", threadId: caller }]);
            assert.deepStrictEqual(
              grants.map((grant) => grant.type),
              ["granted"],
            );
            yield* gate.keep(["w_new"]);
            inStore = [
              {
                key: "w_new",
                threadId: caller,
                untilMs: (grants[0] as { untilMs: number }).untilMs,
              },
            ];
            // Back to observe with slots off, well inside the note's 10 minutes.
            yield* settings.updateSettings({ agentSlots: { mode: "observe", enabled: false } });
            yield* gate.acquire({ callerThreadId: ThreadId.make("other"), targetInstanceId: MAIN });
            return yield* statusAfter(2);
          }),
        );
        // The new hold was granted after the start: it counts.
        assert.deepInclude(status.last, { readings: "ok", pieces: 1 });
      }),
  );

  it.effect("a second start of the same thread renews its note's time (#415)", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const slow = yield* observerFor(world);
      let decided = 0;
      const decideSlow = (input: Partial<SlotObserveInput> & { atMs: number }) =>
        Effect.gen(function* () {
          yield* slow.observe({
            today: ["allow"],
            starting: [],
            held: Effect.never,
            startGraceMs: 30_000,
            gateRecords: false,
            ...input,
          });
          decided += 1;
          yield* settle;
          yield* TestClock.adjust(OBSERVE_READ_TIMEOUT);
          yield* counted(slow.status, decided);
        });
      const startAt = (atMs: number, token: number) => ({
        atMs,
        callerThreadId: "waiter",
        ownStart: { token, instanceId: MAIN, atMs },
      });
      // A hold may outlive the note's limit (`reserveSeconds` over 600): the later start's note
      // must still take it.
      yield* decideSlow(startAt(0, 1));
      yield* decideSlow(startAt(DISPATCH_SAFETY_MS / 2, 2));
      yield* decideSlow({ atMs: DISPATCH_SAFETY_MS });
      assert.strictEqual((yield* slow.kept).notes, 1, "renewed");
      yield* decideSlow({ atMs: DISPATCH_SAFETY_MS * 1.5 });
      assert.strictEqual((yield* slow.kept).notes, 0, "ended");
    }).pipe(Effect.scoped),
  );

  it("nests a systemd slice the way systemd does", () => {
    assert.strictEqual(
      slicePath("app-t3code-agents-0123.slice"),
      "/app.slice/app-t3code.slice/app-t3code-agents.slice/app-t3code-agents-0123.slice",
    );
  });
});

describe("SlotGate enforce mode (the default)", () => {
  // Literal names (capacity-rule.json), so these fail on a build without the kinds.
  const LIGHT_KIND = "agent-light";
  const WRITER_KIND = "agent-writer";
  /** `availableMb` free on the 16 GB machine, and the kernel's OOM kill count. */
  const enforceWorld = (availableMb: number, oomKills = 0) => {
    const world = newWorld();
    world.files.set(
      "/proc/meminfo",
      `MemTotal:       16777216 kB\nMemFree:         1048576 kB\nMemAvailable:   ${availableMb * 1024} kB\nSwapCached:            0 kB\nSwapTotal:       4194304 kB\nSwapFree:        4194304 kB\n`,
    );
    world.files.set("/proc/vmstat", `nr_free_pages 1\noom_kill ${oomKills}\n`);
    return world;
  };
  const ask = (kind?: string) =>
    SlotGate.SlotGate.pipe(
      Effect.flatMap((gate) =>
        gate.acquire({
          callerThreadId: caller,
          targetInstanceId: MAIN,
          ...(kind === undefined ? {} : { kind }),
        }),
      ),
    );
  const status = SlotGate.SlotGate.pipe(Effect.flatMap((gate) => gate.observed));
  /**
   * The first decision after a (re)start is "elevated" (under the floor of two it may still start:
   * released at once); past the calm wait the rule decides. Its answer.
   */
  const warmUp = Effect.gen(function* () {
    const first = yield* ask(LIGHT_KIND);
    yield* SlotGate.SlotGate.pipe(
      Effect.flatMap((gate) => gate.release(first.type === "allow" ? first.token : null)),
    );
    yield* TestClock.adjust(CALM_WAIT);
    return first;
  });

  it.effect("no settings: the rule decides, and wait_list shows it with the fallback", () =>
    Effect.gen(function* () {
      const world = enforceWorld(12_000);
      world.threads = TWO_RUNNING;
      const [allowed, shown] = yield* run(
        world,
        {},
        Effect.gen(function* () {
          const first = yield* warmUp;
          assert.strictEqual(first.type, "refuse");
          // Right after a start the reason says so, not "under pressure".
          if (first.type === "refuse") {
            assert.include(first.message, "started a short while ago");
            assert.notInclude(first.message, "under pressure");
          }
          return [yield* ask(), yield* status] as const;
        }),
      );
      assert.strictEqual(allowed.type, "allow");
      assert.deepInclude(shown, { mode: "enforce", fallback: true, observed: 2 });
      assert.deepInclude(shown?.last, { rule: "allow", reason: "fits", pieces: 2 });
    }),
  );

  it.effect("two starts in one second: the second counts the first's whole cost", () =>
    Effect.gen(function* () {
      // Margin 2458 MB; two running agents with no reading owe 768 MB: 574 MB left, one agent.
      const world = enforceWorld(3_800);
      world.threads = TWO_RUNNING;
      const [first, second, shown] = yield* run(
        world,
        {},
        Effect.gen(function* () {
          yield* warmUp;
          const first = yield* ask(LIGHT_KIND);
          const second = yield* ask(LIGHT_KIND);
          return [first, second, yield* status] as const;
        }),
      );
      assert.strictEqual(first.type, "allow");
      assert.strictEqual(second.type, "refuse");
      if (second.type === "refuse") {
        assert.include(second.message, "only 190 MB of memory is free for new work");
        assert.deepInclude(second.refusal, { type: "full" });
      }
      assert.deepInclude(shown?.last, { rule: "refuse", reason: "budget", owedMb: 1152 });
    }),
  );

  it.effect(
    "a restart mid-run counts every running agent as a writer, elevated and in fallback",
    () =>
      Effect.gen(function* () {
        // 8 cores and room for four builds: four writers at most (two per build that fits, by cores).
        const world = enforceWorld(12_000);
        world.threads = [];
        const before = yield* run(
          world,
          {},
          Effect.gen(function* () {
            const gate = yield* SlotGate.SlotGate;
            yield* warmUp;
            for (const id of ["l1", "l2", "l3", "l4"]) {
              const allowed = yield* ask(LIGHT_KIND);
              assert.strictEqual(allowed.type, "allow", id);
              yield* gate.started(
                allowed.type === "allow" ? allowed.token : null,
                ThreadId.make(id),
              );
              // Their own scopes read small: they stay light (unread growth would count as writers).
              world.threads.push({ id, unit: `${id}.scope` });
              scopeFiles(world, `${id}.scope`, 200);
            }
            // The four run as light agents: a writer still fits.
            return yield* ask(WRITER_KIND);
          }),
        );
        assert.strictEqual(before.type, "allow");
        // A new gate (a server restart, the four still running) knows no kinds: four writers.
        const after = yield* run(
          world,
          {},
          Effect.gen(function* () {
            yield* warmUp;
            // A light start beside them: counted, but no writer.
            const light = yield* ask(LIGHT_KIND);
            assert.strictEqual(light.type, "allow");
            const writer = yield* ask(WRITER_KIND);
            // Runs again: the same answer, no record left behind by the refusal.
            return [writer, yield* ask(WRITER_KIND), yield* status] as const;
          }),
        );
        assert.deepStrictEqual([after[0].type, after[1].type], ["refuse", "refuse"]);
        // The true number: 4 writers (5 agents counted).
        if (after[0].type === "refuse") {
          assert.include(after[0].message, "4 writing agents are running or starting");
        }
        assert.deepInclude(after[2]?.last, { reason: "writers", pieces: 5 });
        assert.strictEqual(after[2]?.fallback, true);
      }),
  );

  it.effect("a light role whose growth cannot be read counts as a writer", () =>
    Effect.gen(function* () {
      // Four agents started as light, with no scopes to read (growth unseen): four writers.
      const world = enforceWorld(12_000);
      world.threads = [];
      const [light, writer] = yield* run(
        world,
        {},
        Effect.gen(function* () {
          const gate = yield* SlotGate.SlotGate;
          yield* warmUp;
          for (const id of ["l1", "l2", "l3", "l4"]) {
            const allowed = yield* ask(LIGHT_KIND);
            assert.strictEqual(allowed.type, "allow", id);
            yield* gate.started(allowed.type === "allow" ? allowed.token : null, ThreadId.make(id));
            world.threads.push({ id });
          }
          return [yield* ask(LIGHT_KIND), yield* ask(WRITER_KIND)] as const;
        }),
      );
      // Another light start fits; a writer does not: the four count as writers.
      assert.strictEqual(light.type, "allow");
      assert.strictEqual(writer.type, "refuse");
      if (writer.type === "refuse") {
        assert.include(writer.message, "writing agents are running or starting");
      }
    }),
  );

  it.effect("the fallback ends after ten calm minutes and trips again on a new OOM kill", () =>
    Effect.gen(function* () {
      const world = enforceWorld(12_000, 7);
      world.threads = Array.from({ length: 10 }, (_, index) => ({ id: `r${index}` }));
      const [fixed, lifted, tripped, shown] = yield* run(
        world,
        {},
        Effect.gen(function* () {
          yield* warmUp;
          // Ten running: the fixed ceiling while in fallback (after a start too).
          const fixed = yield* ask(LIGHT_KIND);
          yield* TestClock.adjust(Duration.minutes(10));
          // Kills from before the start are no new kill: the derived ceiling (24) applies.
          const lifted = yield* ask(LIGHT_KIND);
          yield* SlotGate.SlotGate.pipe(
            Effect.flatMap((gate) => gate.release(lifted.type === "allow" ? lifted.token : null)),
          );
          world.files.set("/proc/vmstat", "oom_kill 8\n");
          const tripped = yield* ask(LIGHT_KIND);
          return [fixed, lifted, tripped, yield* status] as const;
        }),
      );
      assert.strictEqual(fixed.type, "refuse");
      if (fixed.type === "refuse") assert.include(fixed.message, "the limit is 10 now");
      assert.strictEqual(lifted.type, "allow");
      assert.strictEqual(tripped.type, "refuse");
      assert.deepInclude(shown, { fallback: true });
      assert.deepInclude(shown?.last, { reason: "ceiling" });
    }),
  );

  it.effect("readings that fail refuse (unreadable), never fit; it recovers after calm", () =>
    Effect.gen(function* () {
      const world = enforceWorld(12_000);
      world.threads = TWO_RUNNING;
      const [thrown, shown, calming, back] = yield* run(
        world,
        {},
        Effect.gen(function* () {
          yield* warmUp;
          world.host = "throw";
          const thrown = yield* ask(LIGHT_KIND);
          const shown = yield* status;
          world.host = "ok";
          // Unreadable left the level elevated: calm again before the rule says yes.
          const calming = yield* ask(LIGHT_KIND);
          yield* TestClock.adjust(CALM_WAIT);
          return [thrown, shown, calming, yield* ask(LIGHT_KIND)] as const;
        }),
      );
      assert.strictEqual(thrown.type, "refuse");
      if (thrown.type === "refuse") {
        assert.include(
          thrown.message,
          "cannot be read now and 2 agents are running or starting; only 2 start without readings",
        );
      }
      assert.deepInclude(shown?.last, { rule: "refuse", reason: "unreadable", level: "elevated" });
      assert.strictEqual(calming.type, "refuse");
      assert.strictEqual(back.type, "allow");
    }),
  );

  it.effect("a restart lifts on the gate's own readings, with no asks in between", () =>
    Effect.gen(function* () {
      const world = enforceWorld(12_000);
      world.threads = TWO_RUNNING;
      const first = yield* run(
        world,
        {},
        Effect.gen(function* () {
          // No ask for a minute and a half: the timer reads the machine every 30 s.
          yield* TestClock.adjust(Duration.seconds(91));
          return yield* ask(LIGHT_KIND);
        }),
      );
      assert.strictEqual(first.type, "allow");
    }),
  );

  it.effect(
    "readings that fail: the floor starts with none running, then the fixed 10 by count",
    () =>
      Effect.gen(function* () {
        const huge = enforceWorld(12_000);
        // More than 1 TiB of RAM: read, but out of the rule's range (unreadable).
        huge.files.set(
          "/proc/meminfo",
          "MemTotal:       2147483648 kB\nMemFree:         1048576 kB\nMemAvailable:   1073741824 kB\nSwapTotal:       0 kB\nSwapFree:        0 kB\n",
        );
        const thrown = enforceWorld(12_000);
        thrown.host = "throw";
        for (const [name, world] of [
          ["throw", thrown],
          ["over 1 TiB", huge],
        ] as const) {
          world.threads = [];
          const answers = yield* run(
            world,
            {},
            Effect.gen(function* () {
              const gate = yield* SlotGate.SlotGate;
              const one = (id: string) =>
                Effect.gen(function* () {
                  const answer = yield* ask(LIGHT_KIND);
                  if (answer.type === "allow") {
                    yield* gate.started(answer.token, ThreadId.make(id));
                    world.threads.push({ id });
                  }
                  return answer;
                });
              // None running, a restart, nothing readable: the floor of two starts at once.
              const floor = [yield* one("f1"), yield* one("f2")];
              const third = yield* one("f3");
              // Runs again: the same refusal.
              const again = yield* one("f3");
              // Past the 2 minutes unread (the timer reads every 30 s meanwhile).
              yield* TestClock.adjust(Duration.seconds(125));
              const counted: Array<SlotGate.SlotAcquire> = [];
              for (let index = 3; index <= 11; index += 1) counted.push(yield* one(`f${index}`));
              return { floor, third, again, counted };
            }),
          );
          assert.deepStrictEqual(
            answers.floor.map((answer) => answer.type),
            ["allow", "allow"],
            name,
          );
          assert.strictEqual(answers.third.type, "refuse", name);
          assert.strictEqual(answers.again.type, "refuse", name);
          if (answers.third.type === "refuse") {
            assert.include(answers.third.message, "only 2 start without readings", name);
          }
          // After 2 minutes unread: by count up to the fixed 10, never a wait without end.
          assert.deepStrictEqual(
            answers.counted.map((answer) => answer.type),
            [...Array.from({ length: 8 }, () => "allow"), "refuse"],
            name,
          );
          const last = answers.counted.at(-1)!;
          if (last.type === "refuse") assert.include(last.message, "the limit is 10 now", name);
        }
      }),
  );

  it.effect("hung readings: the floor still starts, no ask waits past the read's time limit", () =>
    Effect.gen(function* () {
      const world = enforceWorld(12_000);
      world.host = "hang";
      world.threads = [];
      const answers = yield* run(
        world,
        {},
        Effect.gen(function* () {
          const both = yield* Effect.forkChild(Effect.all([ask(LIGHT_KIND), ask(LIGHT_KIND)]));
          // Each read gives up after its time limit; with two hanging, no new read starts (busy).
          for (let turn = 0; turn < 6; turn += 1) {
            yield* TestClock.adjust(OBSERVE_READ_TIMEOUT);
            yield* settle;
          }
          return yield* Fiber.join(both);
        }),
      );
      assert.deepStrictEqual(
        answers.map((answer) => answer.type),
        ["allow", "allow"],
      );
    }),
  );

  it.effect("no OOM counter (basic tier): the fixed 10 stays, however long it is calm", () =>
    Effect.gen(function* () {
      // The control first: with the counter (full tier) the rule's own ceiling lets an 11th start.
      // Then no counter line, no /proc/vmstat, and a counter but no agent scopes (no systemd).
      for (const [vmstat, noScopes] of [
        ["nr_free_pages 1\noom_kill 0\n", false],
        ["nr_free_pages 1\n", false],
        [undefined, false],
        ["nr_free_pages 1\noom_kill 0\n", true],
      ] as const) {
        const world = enforceWorld(12_000);
        world.noScopes = noScopes;
        if (vmstat === undefined) world.files.delete("/proc/vmstat");
        else world.files.set("/proc/vmstat", vmstat);
        world.threads = Array.from({ length: 10 }, (_, index) => ({ id: `r${index}` }));
        const [answer, shown] = yield* run(
          world,
          {},
          Effect.gen(function* () {
            yield* warmUp;
            yield* TestClock.adjust(Duration.minutes(30));
            return [yield* ask(LIGHT_KIND), yield* status] as const;
          }),
        );
        if (vmstat?.includes("oom_kill") === true && !noScopes) {
          assert.strictEqual(answer.type, "allow", "full tier");
          assert.deepInclude(shown, { fallback: false });
          continue;
        }
        assert.strictEqual(answer.type, "refuse", `${vmstat} ${noScopes}`);
        if (answer.type === "refuse") assert.include(answer.message, "the limit is 10 now");
      }
    }),
  );

  it.effect("a held slot owes a writer's cost: a start that fits only without it is refused", () =>
    Effect.gen(function* () {
      // Margin 2458 MB; two running agents with no reading owe 768 MB, 968 MB more is free: a
      // light start (384) fits, not beside a held slot (a writer's 768).
      const results: Array<string> = [];
      for (const holds of [
        [],
        [{ key: "w_held", threadId: ThreadId.make("waiter"), untilMs: 600_000 }],
      ]) {
        const world = enforceWorld(2458 + 768 + 968);
        world.threads = TWO_RUNNING;
        const answer = yield* run(
          world,
          { waitFor: { enabled: true } },
          Effect.gen(function* () {
            const gate = yield* SlotGate.SlotGate;
            yield* gate.attachReservations({
              read: Effect.succeed(holds),
              used: () => Effect.void,
            });
            yield* TestClock.adjust(CALM_WAIT);
            return yield* ask(LIGHT_KIND);
          }),
        );
        results.push(
          answer.type === "refuse" ? `refuse: ${answer.message}` : `allow (${holds.length})`,
        );
      }
      assert.strictEqual(results[0], "allow (0)");
      assert.include(results[1], "only 200 MB of memory is free for new work");
    }),
  );

  it.effect("a start that never completes gives its owed cost back when its dispatch ends", () =>
    Effect.gen(function* () {
      // Past the floor: two running owe 768 MB; 500 MB more: one light start (384) fits, not two.
      const world = enforceWorld(2458 + 768 + 500);
      world.threads = TWO_RUNNING;
      const answers = yield* run(
        world,
        {},
        Effect.gen(function* () {
          const gate = yield* SlotGate.SlotGate;
          yield* TestClock.adjust(CALM_WAIT);
          const first = yield* ask(LIGHT_KIND);
          const second = yield* ask(LIGHT_KIND);
          // Stopped half-way: the dispatch ended with no child.
          yield* gate.release(first.type === "allow" ? first.token : null);
          return [first, second, yield* ask(LIGHT_KIND)].map((answer) => answer.type);
        }),
      );
      assert.deepStrictEqual(answers, ["allow", "refuse", "allow"]);
    }),
  );

  it.effect(
    "an OOM kill in the agents' slice raises the costs; one elsewhere does not; calm lowers them",
    () =>
      Effect.gen(function* () {
        const world = enforceWorld(12_000);
        world.threads = [{ id: "w1", unit: "w1.scope" }];
        scopeFiles(world, "w1.scope", 500);
        world.files.set(`${BOX}/memory.events`, "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n");
        const costs = yield* run(
          world,
          {},
          Effect.gen(function* () {
            yield* warmUp;
            const cost = Effect.gen(function* () {
              const answer = yield* ask(WRITER_KIND);
              yield* SlotGate.SlotGate.pipe(
                Effect.flatMap((gate) =>
                  gate.release(answer.type === "allow" ? answer.token : null),
                ),
              );
              return (yield* status)?.last?.costMb;
            });
            const before = yield* cost;
            // A kill in the agents' slice (the kernel counts it too): one raise.
            world.files.set(`${BOX}/memory.events`, "oom 1\noom_kill 1\n");
            world.files.set("/proc/vmstat", "oom_kill 1\n");
            const agentKill = yield* cost;
            // A kill elsewhere (a build box at its own cap): the fallback, no raise.
            world.files.set("/proc/vmstat", "oom_kill 2\n");
            const otherKill = yield* cost;
            // Ten calm minutes (the timer's readings): the fallback lifts, one raise comes off.
            yield* TestClock.adjust(Duration.minutes(11));
            const calm = yield* cost;
            return { before, agentKill, otherKill, calm, shown: yield* status };
          }),
        );
        assert.deepStrictEqual(
          [costs.before, costs.agentKill, costs.otherKill, costs.calm],
          [768, 960, 960, 768],
        );
        assert.deepInclude(costs.shown, { fallback: false });
      }),
  );

  it.effect("settings that cannot be read: the fixed limit of 10, never no limit", () =>
    Effect.gen(function* () {
      const world = enforceWorld(12_000);
      world.threads = Array.from({ length: 9 }, (_, index) => ({ id: `r${index}` }));
      let unreadable = true;
      const failing = Layer.mock(ServerSettings.ServerSettingsService)({
        getSettings: Effect.suspend(() =>
          unreadable
            ? Effect.fail(
                new ServerSettingsError({ settingsPath: "<test>", operation: "read-file" }),
              )
            : Effect.succeed(DEFAULT_SERVER_SETTINGS),
        ),
      });
      const [nine, ten, readAgain] = yield* SlotGate.SlotGate.pipe(
        Effect.flatMap((gate) =>
          Effect.gen(function* () {
            const nine = yield* gate.acquire({ callerThreadId: caller, targetInstanceId: MAIN });
            world.threads.push({ id: "r9" });
            const ten = yield* gate.acquire({ callerThreadId: caller, targetInstanceId: MAIN });
            unreadable = false;
            // Read again: the default (enforce) decides once more.
            yield* TestClock.adjust(CALM_WAIT);
            const readAgain = yield* gate.observed;
            return [nine, ten, readAgain] as const;
          }),
        ),
        Effect.provide(layerFor(world, {}, failing)),
      );
      assert.strictEqual(nine.type, "allow");
      assert.strictEqual(ten.type, "refuse");
      if (ten.type === "refuse") assert.include(ten.message, "the limit is 10");
      assert.deepInclude(readAgain, { mode: "enforce" });
    }),
  );

  it.effect("old and empty settings, and mode fixed: each keeps its own way", () =>
    Effect.gen(function* () {
      const cases = [
        // A file from before `mode`, switched on with typed numbers: fixed, exactly those numbers.
        { settings: { agentSlots: { enabled: true, maxPerMachine: 3 } }, mode: null, limit: 3 },
        { settings: { agentSlots: { mode: "fixed" } }, mode: null, limit: null },
        // Empty: the default, the capacity rule.
        { settings: {}, mode: "enforce", limit: null },
      ];
      for (const entry of cases) {
        const world = enforceWorld(12_000);
        world.threads = [{ id: "a" }, { id: "b" }, { id: "c" }];
        const [answer, shown] = yield* run(
          world,
          entry.settings,
          Effect.gen(function* () {
            yield* TestClock.adjust(CALM_WAIT);
            return [yield* ask(LIGHT_KIND), yield* status] as const;
          }),
        );
        assert.strictEqual(shown?.mode ?? null, entry.mode, JSON.stringify(entry.settings));
        assert.strictEqual(
          answer.type,
          entry.limit === 3 ? "refuse" : "allow",
          JSON.stringify(entry.settings),
        );
        if (answer.type === "refuse") assert.include(answer.message, "the limit is 3");
      }
    }),
  );
});
