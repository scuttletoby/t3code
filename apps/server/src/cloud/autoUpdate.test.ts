import * as NodeServices from "@effect/platform-node/NodeServices";
import { describe, expect, it } from "@effect/vitest";
import {
  ServerSelfUpdateError,
  type ServerSelfUpdateProgressStage,
  type ServerSelfUpdateResult,
  type TerminalMetadataStreamEvent,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";
import * as Ref from "effect/Ref";
import * as TestClock from "effect/testing/TestClock";

import {
  makeWorkAdmissionGate,
  memoryUpdateHoldStore,
  UpdateHoldMarkerError,
  type UpdateHoldStore,
  WORK_ADMISSION_CONSUMERS,
  WorkAdmission,
  type WorkAdmissionGate,
} from "../workAdmission.ts";
import { ServiceLauncherRejectedError } from "./serviceLauncherClient.ts";
import {
  activityReason,
  afterCheck,
  afterFailure,
  afterProbe,
  AUTO_UPDATE_CHECK_INTERVAL_MS,
  AUTO_UPDATE_FAILURE_BACKOFF_MS,
  AUTO_UPDATE_FIRST_CHECK_DELAY_MS,
  AUTO_UPDATE_MAX_HOLD_MS,
  AUTO_UPDATE_MAX_QUIET_WAIT_MS,
  AUTO_UPDATE_QUIET_POLL_MS,
  decideRelease,
  hookReason,
  initialAutoUpdateState,
  type LauncherRecord,
  makeAutoUpdater,
  nextAutoUpdateStep,
  noActivity,
  readLauncherRecord,
  runningTerminalCommands,
  threadActivity,
  AUTO_UPDATE_UNCONFIRMED_GRACE_MS,
} from "./autoUpdate.ts";
import { SERVICE_LAUNCHER_PROTOCOL } from "./serviceProtocol.ts";

const CURRENT = "0.1.9-fork.1";
const TARGET = "0.1.9-fork.2";

const outcome = (status: "committed" | "rolled-back" | "failed", id = "update-1") => ({
  id,
  fromVersion: CURRENT,
  targetVersion: TARGET,
  status,
  reason: `${status}-reason`,
});
const pending = {
  id: "update-2",
  fromVersion: CURRENT,
  targetVersion: TARGET,
  dbPath: "/data/state.sqlite",
  status: "pending" as const,
};

describe("decideRelease", () => {
  const decide = (candidate: string | undefined, current = CURRENT) =>
    decideRelease({ current, candidate, lastUpdate: undefined, backedOffFailureId: undefined });

  it.each([
    { current: "0.1.9-fork.1", candidate: "0.1.9-fork.2", expected: "0.1.9-fork.2" },
    { current: "0.1.9-fork.3", candidate: "0.1.10-fork.1", expected: "0.1.10-fork.1" },
    { current: "0.1.9", candidate: "0.1.10", expected: "0.1.10" },
    { current: "0.1.9-fork.2", candidate: "0.1.9-fork.2", expected: undefined },
    { current: "0.1.9-fork.2", candidate: "0.1.9-fork.1", expected: undefined },
    { current: "0.1.9", candidate: "0.1.10-nightly.20261112.1", expected: undefined },
    { current: "0.1.9", candidate: "latest", expected: undefined },
    { current: "0.1.9", candidate: undefined, expected: undefined },
  ])("$current -> $candidate", ({ current, candidate, expected }) => {
    const decision = decide(candidate, current);
    expect(decision.type === "offer" ? decision.version : undefined).toBe(expected);
  });

  it("skips the release the launcher rolled back, but not a newer one", () => {
    const lastUpdate = outcome("rolled-back");
    expect(
      decideRelease({ current: CURRENT, candidate: TARGET, lastUpdate, backedOffFailureId: "x" }),
    ).toEqual({ type: "skip-rolled-back", version: TARGET, reason: "rolled-back-reason" });
    expect(
      decideRelease({
        current: CURRENT,
        candidate: "0.1.9-fork.3",
        lastUpdate,
        backedOffFailureId: undefined,
      }),
    ).toEqual({ type: "offer", version: "0.1.9-fork.3" });
  });

  it("backs off once from a failed attempt, then offers the release again", () => {
    const lastUpdate = outcome("failed", "failed-1");
    expect(
      decideRelease({
        current: CURRENT,
        candidate: TARGET,
        lastUpdate,
        backedOffFailureId: undefined,
      }),
    ).toEqual({
      type: "back-off-failed",
      version: TARGET,
      updateId: "failed-1",
      reason: "failed-reason",
    });
    expect(
      decideRelease({
        current: CURRENT,
        candidate: TARGET,
        lastUpdate,
        backedOffFailureId: "failed-1",
      }),
    ).toEqual({ type: "offer", version: TARGET });
  });

  it("does not offer while the launcher has an update pending", () => {
    expect(
      decideRelease({
        current: CURRENT,
        candidate: TARGET,
        lastUpdate: pending,
        backedOffFailureId: undefined,
      }).type,
    ).toBe("launcher-busy");
  });
});

it.layer(NodeServices.layer)("readLauncherRecord", (it) => {
  it.effect("reads rolled-back and failed records, and reports a file it cannot use", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const dir = yield* fs.makeTempDirectoryScoped();
      const file = path.join(dir, "service-state.json");
      const write = (update: unknown) =>
        fs.writeFileString(
          file,
          JSON.stringify({ protocol: SERVICE_LAUNCHER_PROTOCOL, activeVersion: CURRENT, update }),
        );

      expect(yield* readLauncherRecord(fs, file)).toEqual({ type: "none" });
      yield* write(outcome("rolled-back"));
      expect(yield* readLauncherRecord(fs, file)).toEqual({
        type: "update",
        update: outcome("rolled-back"),
      });
      yield* write(outcome("failed"));
      expect(yield* readLauncherRecord(fs, file)).toEqual({
        type: "update",
        update: outcome("failed"),
      });
      yield* fs.writeFileString(file, "{ not json");
      expect((yield* readLauncherRecord(fs, file)).type).toBe("unreadable");
    }).pipe(Effect.scoped),
  );
});

describe("threadActivity", () => {
  const run = (status: string, extra: Record<string, unknown> = {}) =>
    ({ id: `run-${status}`, ordinal: 1, status, ...extra }) as never;
  const projection = (
    input: {
      readonly runs?: ReadonlyArray<never>;
      readonly runtimeRequests?: ReadonlyArray<never>;
      readonly providerThreads?: ReadonlyArray<never>;
      readonly turnItems?: ReadonlyArray<never>;
    } = {},
  ) => ({
    thread: { activeProviderThreadId: null } as never,
    runs: input.runs ?? [],
    runtimeRequests: input.runtimeRequests ?? [],
    providerThreads: input.providerThreads ?? [],
    turnItems: input.turnItems ?? [],
  });

  it("counts waiting, preparing, starting and running runs as busy", () => {
    for (const status of ["preparing", "starting", "running", "waiting"]) {
      expect(threadActivity(projection({ runs: [run(status)] })).activeRuns).toBe(1);
    }
    expect(threadActivity(projection({ runs: [run("completed")] }))).toEqual(noActivity);
    expect(threadActivity(projection({ runs: [run("rolled_back")] }))).toEqual(noActivity);
  });

  it("counts unheld queued runs, not held ones", () => {
    expect(threadActivity(projection({ runs: [run("queued")] })).queuedRuns).toBe(1);
    expect(
      threadActivity(projection({ runs: [run("queued", { queueHeld: true })] })).queuedRuns,
    ).toBe(0);
  });

  it("counts pending approvals and questions", () => {
    const requests = [{ status: "pending" }, { status: "resolved" }] as never[];
    expect(threadActivity(projection({ runtimeRequests: requests })).pendingRequests).toBe(1);
  });

  it("counts background work the way upstream does, for every provider", () => {
    // A Codex-style command left running after its turn completed: a turn item, no roster.
    const codexCommand = {
      id: "item-1",
      type: "command_execution",
      status: "running",
      title: "npm run build",
      nativeItemRef: null,
      input: "npm run build",
    } as never;
    expect(
      threadActivity(projection({ runs: [run("completed")], turnItems: [codexCommand] }))
        .backgroundTasks,
    ).toBe(1);
    // A Claude-style roster on the provider thread.
    const roster = {
      id: "pt-1",
      pendingBackgroundTasks: [{ taskId: "bg-1", kind: "command" }],
    } as never;
    expect(
      threadActivity(projection({ runs: [run("completed")], providerThreads: [roster] }))
        .backgroundTasks,
    ).toBe(1);
  });
});

describe("activityReason", () => {
  it("is null only when nothing is working", () => {
    expect(activityReason(noActivity)).toBeNull();
    expect(activityReason({ ...noActivity, activeRuns: 1 })).toBe("1 running turn");
    expect(activityReason({ ...noActivity, pendingRequests: 2, backgroundTasks: 1 })).toBe(
      "2 pending approval or questions, 1 background task",
    );
    expect(activityReason({ ...noActivity, queuedRuns: 1, terminalCommands: 2 })).toBe(
      "1 queued turn, 2 running terminal commands",
    );
  });
});

describe("hookReason", () => {
  it("allows only a clean exit", () => {
    expect(hookReason({ code: 0, stdout: "ok", stderr: "", timedOut: false })).toBeNull();
    expect(
      hookReason({ code: 1, stdout: "\n  deploy running  \nmore", stderr: "", timedOut: false }),
    ).toBe("the quiet hook said not now: deploy running");
    expect(hookReason({ code: 3, stdout: "", stderr: "busy", timedOut: false })).toBe(
      "the quiet hook said not now: busy",
    );
    expect(hookReason({ code: 1, stdout: "", stderr: "", timedOut: false })).toBe(
      "the quiet hook said not now",
    );
    expect(hookReason({ code: null, stdout: "", stderr: "", timedOut: true })).toBe(
      "the quiet hook did not answer in time",
    );
    expect(hookReason({ error: "ProcessSpawnError" })).toBe(
      "the quiet hook could not run (ProcessSpawnError)",
    );
  });
});

describe("auto-update schedule", () => {
  const start = 1_000_000;

  it("checks first after the startup delay, then hourly when nothing is newer", () => {
    const state = initialAutoUpdateState(start);
    expect(nextAutoUpdateStep(state, start)).toEqual({ type: "none" });
    const due = start + AUTO_UPDATE_FIRST_CHECK_DELAY_MS;
    expect(nextAutoUpdateStep(state, due)).toEqual({ type: "check" });
    const after = afterCheck(due, undefined);
    expect(after).toEqual({ phase: "idle", nextCheckAt: due + AUTO_UPDATE_CHECK_INTERVAL_MS });
  });

  it("waits for quiet once a minute, then updates", () => {
    const waiting = afterCheck(start, TARGET);
    expect(nextAutoUpdateStep(waiting, start)).toEqual({ type: "probe", version: TARGET });
    if (waiting.phase !== "waiting") throw new Error("expected waiting");
    const busy = afterProbe(waiting, start, "1 running turn");
    expect(busy.outcome).toBe("wait");
    expect(nextAutoUpdateStep(busy.state, start + 1)).toEqual({ type: "none" });
    expect(nextAutoUpdateStep(busy.state, start + AUTO_UPDATE_QUIET_POLL_MS)).toEqual({
      type: "probe",
      version: TARGET,
    });
    if (busy.state.phase !== "waiting") throw new Error("expected waiting");
    expect(afterProbe(busy.state, start + AUTO_UPDATE_QUIET_POLL_MS, null).outcome).toBe("update");
  });

  it("never forces: a bounded wait gives up until the next check", () => {
    const waiting = afterCheck(start, TARGET);
    if (waiting.phase !== "waiting") throw new Error("expected waiting");
    const late = start + AUTO_UPDATE_MAX_QUIET_WAIT_MS;
    const gaveUp = afterProbe(waiting, late, "1 running turn");
    expect(gaveUp.outcome).toBe("give-up");
    expect(gaveUp.state).toEqual({
      phase: "idle",
      nextCheckAt: late + AUTO_UPDATE_CHECK_INTERVAL_MS,
    });
    // Quiet at the bound still updates.
    expect(afterProbe(waiting, late, null).outcome).toBe("update");
  });

  it("backs off after a failed install, longer than a normal check", () => {
    const failed = afterFailure(start);
    expect(failed).toEqual({ phase: "idle", nextCheckAt: start + AUTO_UPDATE_FAILURE_BACKOFF_MS });
    expect(AUTO_UPDATE_FAILURE_BACKOFF_MS).toBeGreaterThan(AUTO_UPDATE_CHECK_INTERVAL_MS);
    expect(nextAutoUpdateStep(failed, start + AUTO_UPDATE_CHECK_INTERVAL_MS)).toEqual({
      type: "none",
    });
    expect(nextAutoUpdateStep(failed, start + AUTO_UPDATE_FAILURE_BACKOFF_MS)).toEqual({
      type: "check",
    });
  });
});

/** A gate as the server builds it: a marker store, and every consumer admits through it. */
const activeGate = Effect.fn("test.activeGate")(function* (
  marker?: UpdateHoldStore,
  consumers: ReadonlyArray<(typeof WORK_ADMISSION_CONSUMERS)[number]> = WORK_ADMISSION_CONSUMERS,
) {
  const gate = yield* makeWorkAdmissionGate({ marker: marker ?? (yield* memoryUpdateHoldStore) });
  for (const consumer of consumers) yield* gate.register(consumer);
  // Startup has started whatever the last update held.
  yield* gate.finishHeldWork;
  return gate;
});

/** A fake server around the real update loop: launcher, thread state, hook and self-update. */
const makeWorld = Effect.fn("test.makeWorld")(function* (
  options: {
    readonly bootService?: boolean;
    readonly gate?: WorkAdmissionGate;
    readonly marker?: Effect.Success<typeof memoryUpdateHoldStore>;
    /**
     * What the launcher does when asked: accept, refuse, or take the request without the answer
     * coming back (the 30 s reply timeout), with or without its record then showing it pending.
     */
    readonly launcher?: "accepts" | "rejects" | "answer-lost" | "answer-lost-then-pending";
    /** Fails the update before any stage, like the Update button's update already running. */
    readonly refuseBeforeStart?: boolean;
  } = {},
) {
  const marker = options.marker ?? (yield* memoryUpdateHoldStore);
  const gate = options.gate ?? (yield* activeGate(marker));
  /** Whether the gate was closed each time thread state was read during the handoff. */
  const heldAtInstallRead = yield* Ref.make<Array<boolean>>([]);
  const resets = yield* Ref.make(0);
  const activity = yield* Ref.make<string | null>(null);
  /** Thread state seen at the last check, right before the handoff. */
  const activityAtInstall = yield* Ref.make<string | null>(null);
  const hook = yield* Ref.make<string | null>(null);
  const hookAtInstall = yield* Ref.make<string | null>(null);
  const installing = yield* Ref.make(false);
  const record = yield* Ref.make<LauncherRecord>({ type: "none" });
  const release = yield* Ref.make<string | undefined>(TARGET);
  const enabled = yield* Ref.make(true);
  /** The setting as read at the last check, right before the handoff. */
  const enabledAtInstall = yield* Ref.make(true);
  const calls = yield* Ref.make<Array<string>>([]);
  const note = (call: string) => Ref.update(calls, (all) => [...all, call]);

  const update = (
    input: { readonly targetVersion: string },
    report?: (stage: ServerSelfUpdateProgressStage) => Effect.Effect<void, ServerSelfUpdateError>,
    onHandoffAccepted?: () => Effect.Effect<void>,
  ): Effect.Effect<ServerSelfUpdateResult, ServerSelfUpdateError> =>
    Effect.gen(function* () {
      yield* note(`update ${input.targetVersion}`);
      if (options.refuseBeforeStart) {
        return yield* new ServerSelfUpdateError({
          reason: "A server update is already in progress.",
        });
      }
      yield* report?.("downloading") ?? Effect.void;
      yield* Ref.set(installing, true);
      yield* report?.("installing") ?? Effect.void;
      const launcher = options.launcher ?? "accepts";
      if (launcher === "rejects") {
        return yield* new ServerSelfUpdateError({
          reason: "Another server update is pending.",
          cause: new ServiceLauncherRejectedError({
            targetVersion: input.targetVersion,
            reason: "Another server update is pending.",
          }),
        });
      }
      if (launcher !== "accepts") {
        yield* note("launcher asked");
        if (launcher === "answer-lost-then-pending") {
          yield* Ref.set(record, { type: "update", update: pending });
        }
        return yield* new ServerSelfUpdateError({
          reason: "Could not ask the service launcher to activate the prepared update.",
          cause: new Error("timed out waiting for the launcher"),
        });
      }
      yield* note("handoff accepted");
      yield* Ref.set(record, { type: "update", update: pending });
      yield* onHandoffAccepted?.() ?? Effect.void;
      return { targetVersion: input.targetVersion, method: "boot-service" as const };
    }).pipe(Effect.ensuring(Ref.set(installing, false)));

  const updater = yield* makeAutoUpdater({
    currentVersion: CURRENT,
    bootService: options.bootService ?? true,
    gate,
    preferences: Effect.all([
      Ref.get(installing),
      Ref.get(enabled),
      Ref.get(enabledAtInstall),
    ]).pipe(
      Effect.map(([staging, enabledNow, enabledLate]) => (staging ? enabledLate : enabledNow)),
      Effect.map((automaticUpdates) => ({
        automaticUpdates,
        automaticUpdatesQuietCommand: "/usr/local/bin/quiet",
      })),
    ),
    newestRelease: Ref.get(release),
    launcherRecord: Ref.get(record),
    activity: Effect.gen(function* () {
      yield* note("activity");
      if (!(yield* Ref.get(installing))) return yield* Ref.get(activity);
      const held = yield* gate.isHeld;
      yield* Ref.update(heldAtInstallRead, (all) => [...all, held]);
      return yield* Ref.get(activityAtInstall);
    }),
    askHook: () =>
      Effect.gen(function* () {
        yield* note("hook");
        return (yield* Ref.get(installing)) ? yield* Ref.get(hookAtInstall) : yield* Ref.get(hook);
      }),
    update,
    resetAfterDroppedHandoff: Ref.update(resets, (count) => count + 1),
  });
  const at = (ms: number) => TestClock.setTime(ms).pipe(Effect.andThen(updater.tick));
  return {
    ...updater,
    gate,
    marker,
    heldAtInstallRead: Ref.get(heldAtInstallRead),
    resets: Ref.get(resets),
    activity,
    activityAtInstall,
    hook,
    hookAtInstall,
    record,
    release,
    enabled,
    enabledAtInstall,
    calls: Ref.get(calls),
    clearCalls: Ref.set(calls, []),
    at,
  };
});

const DUE = AUTO_UPDATE_FIRST_CHECK_DELAY_MS;

describe("auto-update loop", () => {
  it.effect(
    "updates when quiet, asking the hook first and thread state last, behind the gate",
    () =>
      Effect.gen(function* () {
        const world = yield* makeWorld();
        yield* world.at(DUE);
        expect((yield* world.state).phase).toBe("waiting");
        yield* world.clearCalls;
        yield* world.at(DUE + 1);
        expect(yield* world.calls).toEqual([
          "hook",
          "activity",
          `update ${TARGET}`,
          // The last check: hook, then the gate closes, then thread state.
          "hook",
          "activity",
          "handoff accepted",
        ]);
        expect(yield* world.state).toEqual({
          phase: "handed-off",
          version: TARGET,
          since: DUE + 1,
          confirmed: true,
        });
        expect(yield* world.gate.isHeld).toBe(true);
        // Thread state was read once more at the handoff, after the gate had closed.
        expect(yield* world.heldAtInstallRead).toEqual([true]);
        // The marker tells the next startup which work waited for the restart.
        expect(yield* world.marker.current).toEqual({
          heldAt: DateTime.formatIso(DateTime.makeUnsafe(DUE + 1)),
          targetVersion: TARGET,
        });
      }),
  );

  it.effect("holds new work from the last check until exit", () =>
    Effect.gen(function* () {
      const world = yield* makeWorld();
      yield* world.at(DUE);
      yield* world.at(DUE + 1);
      const started = yield* Ref.make(false);
      const fiber = yield* Effect.forkChild(world.gate.admit(Ref.set(started, true)));
      for (let i = 0; i < 10; i++) yield* Effect.yieldNow;
      expect(yield* Ref.get(started)).toBe(false);
      // Still pending at the launcher: the hold stays, whatever the settings say.
      yield* Ref.set(world.enabled, false);
      yield* world.at(DUE + AUTO_UPDATE_QUIET_POLL_MS * 5);
      for (let i = 0; i < 10; i++) yield* Effect.yieldNow;
      expect(yield* Ref.get(started)).toBe(false);
      yield* Fiber.interrupt(fiber);
    }),
  );

  it.effect(
    "opens the gate again when the launcher drops the handoff and the server lives on",
    () =>
      Effect.gen(function* () {
        const world = yield* makeWorld();
        yield* world.at(DUE);
        yield* world.at(DUE + 1);
        expect(yield* world.gate.isHeld).toBe(true);
        yield* Ref.set(world.record, { type: "update", update: outcome("failed", "update-2") });
        const later = DUE + 2 * AUTO_UPDATE_QUIET_POLL_MS;
        yield* world.at(later);
        expect(yield* world.gate.isHeld).toBe(false);
        expect(yield* world.state).toEqual(afterFailure(later));
        // Work released here starts now, not again after some later restart.
        expect(yield* world.marker.current).toBeUndefined();
        // The next update (the button's or an automatic one) may start.
        expect(yield* world.resets).toBe(1);
      }),
  );

  it.effect("re-checks thread state right before the handoff and waits if work started", () =>
    Effect.gen(function* () {
      const world = yield* makeWorld();
      yield* Ref.set(world.activityAtInstall, "1 running turn");
      yield* world.at(DUE);
      yield* world.at(DUE + 1);
      expect((yield* world.calls).includes("handoff accepted")).toBe(false);
      const state = yield* world.state;
      expect(state.phase === "waiting" && state.lastReason).toBe("1 running turn");
      expect(yield* world.gate.isHeld).toBe(false);
    }),
  );

  it.effect("re-asks the hook right before the handoff", () =>
    Effect.gen(function* () {
      const world = yield* makeWorld();
      yield* Ref.set(world.hookAtInstall, "the quiet hook said not now: deploy");
      yield* world.at(DUE);
      yield* world.at(DUE + 1);
      expect((yield* world.calls).includes("handoff accepted")).toBe(false);
      expect((yield* world.state).phase).toBe("waiting");
      expect(yield* world.gate.isHeld).toBe(false);
    }),
  );

  it.effect("stops at the handoff when the setting was turned off while staging", () =>
    Effect.gen(function* () {
      const world = yield* makeWorld();
      yield* Ref.set(world.enabledAtInstall, false);
      yield* world.at(DUE);
      yield* world.at(DUE + 1);
      expect((yield* world.calls).includes("handoff accepted")).toBe(false);
      expect(yield* world.gate.isHeld).toBe(false);
    }),
  );

  it.effect("counts work that was admitted before the gate closed", () =>
    Effect.gen(function* () {
      const gate = yield* activeGate();
      const world = yield* makeWorld({ gate });
      const fiber = yield* Effect.forkChild(gate.admit(Effect.never));
      for (let i = 0; i < 5; i++) yield* Effect.yieldNow;
      yield* world.at(DUE);
      yield* world.at(DUE + 1);
      expect((yield* world.calls).includes("handoff accepted")).toBe(false);
      const state = yield* world.state;
      expect(state.phase === "waiting" && state.lastReason).toBe("1 piece of work starting");
      expect(yield* gate.isHeld).toBe(false);
      yield* Fiber.interrupt(fiber);
    }),
  );

  it.effect("opens the gate again when the launcher rejects the handoff", () =>
    Effect.gen(function* () {
      const world = yield* makeWorld({ launcher: "rejects" });
      yield* world.at(DUE);
      yield* world.at(DUE + 1);
      expect(yield* world.gate.isHeld).toBe(false);
      expect(yield* world.state).toEqual(afterFailure(DUE + 1));
      expect(yield* world.marker.current).toBeUndefined();
    }),
  );

  it.effect("waits a minute, not six hours, when the Update button's update is running", () =>
    Effect.gen(function* () {
      const world = yield* makeWorld({ refuseBeforeStart: true });
      yield* world.at(DUE);
      yield* world.at(DUE + 1);
      const state = yield* world.state;
      expect(state.phase).toBe("waiting");
      expect(nextAutoUpdateStep(state, DUE + 1 + AUTO_UPDATE_QUIET_POLL_MS).type).toBe("probe");
    }),
  );

  it.effect("waits while thread state is busy and never calls the update", () =>
    Effect.gen(function* () {
      const world = yield* makeWorld();
      yield* Ref.set(world.activity, "1 waiting turn");
      yield* world.at(DUE);
      for (let minute = 1; minute <= 5; minute++) {
        yield* world.at(DUE + minute * AUTO_UPDATE_QUIET_POLL_MS);
      }
      expect((yield* world.calls).some((call) => call.startsWith("update"))).toBe(false);
    }),
  );

  it.effect("runs only for a background service whose work all goes through the gate", () =>
    Effect.gen(function* () {
      // The default when nothing provides the server's gate.
      const passThrough = yield* WorkAdmission;
      // All four start on the same clock, so each is due at the same times.
      const desktop = yield* makeWorld({ bootService: false });
      const ungated = yield* makeWorld({ gate: passThrough });
      // Recovery built on some other gate: held work would be cancelled at the restart.
      const partly = yield* makeWorld({
        gate: yield* activeGate(undefined, ["outbox", "thread-launch"]),
      });
      const service = yield* makeWorld();
      for (const world of [desktop, ungated, partly, service]) {
        yield* world.at(DUE);
        yield* world.at(DUE + 1);
      }
      expect(yield* desktop.calls).toEqual([]);
      expect(yield* ungated.calls).toEqual([]);
      expect(yield* partly.calls).toEqual([]);
      // The same steps do update a background service with an enforcing gate.
      expect(yield* service.calls).toContain(`update ${TARGET}`);
    }),
  );

  it.effect("skips a rolled-back release and backs off from a failed one", () =>
    Effect.gen(function* () {
      const rolledBack = yield* makeWorld();
      const failed = yield* makeWorld();
      yield* Ref.set(rolledBack.record, { type: "update", update: outcome("rolled-back") });
      yield* rolledBack.at(DUE);
      expect(yield* rolledBack.state).toEqual(afterCheck(DUE, undefined));

      yield* Ref.set(failed.record, { type: "update", update: outcome("failed") });
      yield* failed.at(DUE);
      expect(yield* failed.state).toEqual(afterFailure(DUE));
      // After the back-off the same release is tried again.
      yield* failed.at(DUE + AUTO_UPDATE_FAILURE_BACKOFF_MS);
      expect((yield* failed.state).phase).toBe("waiting");
    }),
  );

  it.effect("does not update when the launcher state cannot be read", () =>
    Effect.gen(function* () {
      const world = yield* makeWorld();
      yield* Ref.set(world.record, { type: "unreadable", reason: "bad file" });
      yield* world.at(DUE);
      expect(yield* world.state).toEqual(afterCheck(DUE, undefined));
    }),
  );

  it.effect("keeps new work waiting while the launcher state cannot be read", () =>
    Effect.gen(function* () {
      const world = yield* makeWorld();
      yield* world.at(DUE);
      yield* world.at(DUE + 1);
      yield* Ref.set(world.record, { type: "unreadable", reason: "bad file" });
      for (let minute = 2; minute <= 20; minute++) {
        yield* world.at(DUE + minute * AUTO_UPDATE_QUIET_POLL_MS);
      }
      expect(yield* world.gate.isHeld).toBe(true);
      expect((yield* world.state).phase).toBe("handed-off");
      expect(yield* world.resets).toBe(0);
    }),
  );

  it.effect("keeps holding past the maximum hold while the launcher has this update pending", () =>
    Effect.gen(function* () {
      const world = yield* makeWorld();
      yield* world.at(DUE);
      yield* world.at(DUE + 1);
      // A slow restart: the launcher's record still shows this update pending, so the restart is
      // still coming and work started now would be cut off by it.
      for (const extra of [0, 1, 10 * AUTO_UPDATE_QUIET_POLL_MS]) {
        yield* world.at(DUE + 1 + AUTO_UPDATE_MAX_HOLD_MS + extra);
        expect(yield* world.gate.isHeld).toBe(true);
      }
      expect((yield* world.state).phase).toBe("handed-off");
      expect(yield* world.resets).toBe(0);
      expect(yield* world.marker.current).toMatchObject({ targetVersion: TARGET });
      // Once the record says something else, the cap lets held work start.
      yield* Ref.set(world.record, { type: "update", update: outcome("rolled-back") });
      const due = DUE + 1 + AUTO_UPDATE_MAX_HOLD_MS + 11 * AUTO_UPDATE_QUIET_POLL_MS;
      yield* world.at(due);
      expect(yield* world.gate.isHeld).toBe(false);
      expect(yield* world.resets).toBe(1);
    }),
  );

  it.effect("starts held work after the maximum hold when the launcher record is unreadable", () =>
    Effect.gen(function* () {
      const world = yield* makeWorld();
      yield* world.at(DUE);
      yield* world.at(DUE + 1);
      // A hung launcher whose record cannot be read: nothing shows a restart is still coming.
      const almost = DUE + 1 + AUTO_UPDATE_MAX_HOLD_MS - 1;
      yield* Ref.set(world.record, { type: "unreadable", reason: "bad file" });
      yield* world.at(almost);
      expect(yield* world.gate.isHeld).toBe(true);
      const due = DUE + 1 + AUTO_UPDATE_MAX_HOLD_MS;
      yield* world.at(due);
      expect(yield* world.gate.isHeld).toBe(false);
      expect(yield* world.state).toEqual(afterFailure(due));
      expect(yield* world.resets).toBe(1);
      expect(yield* world.marker.current).toBeUndefined();
    }),
  );

  it.effect("keeps work waiting when the launcher's answer is lost but its record shows it", () =>
    Effect.gen(function* () {
      const world = yield* makeWorld({ launcher: "answer-lost-then-pending" });
      yield* world.at(DUE);
      yield* world.at(DUE + 1);
      expect(yield* world.gate.isHeld).toBe(true);
      expect(yield* world.state).toEqual({
        phase: "handed-off",
        version: TARGET,
        since: DUE + 1,
        confirmed: false,
      });
      yield* world.at(DUE + 2 * AUTO_UPDATE_QUIET_POLL_MS);
      expect((yield* world.state).phase === "handed-off" && (yield* world.state)).toMatchObject({
        confirmed: true,
      });
      // Past the grace period it is confirmed, so it keeps waiting for the restart.
      yield* world.at(DUE + 1 + AUTO_UPDATE_UNCONFIRMED_GRACE_MS + AUTO_UPDATE_QUIET_POLL_MS);
      expect(yield* world.gate.isHeld).toBe(true);
    }),
  );

  it.effect("drops a lost handoff only after the grace period shows nothing pending", () =>
    Effect.gen(function* () {
      const world = yield* makeWorld({ launcher: "answer-lost" });
      yield* world.at(DUE);
      yield* world.at(DUE + 1);
      expect(yield* world.calls).toContain("launcher asked");
      expect(yield* world.gate.isHeld).toBe(true);
      yield* world.at(DUE + 1 + AUTO_UPDATE_UNCONFIRMED_GRACE_MS - 1);
      expect(yield* world.gate.isHeld).toBe(true);
      const due = DUE + 1 + AUTO_UPDATE_UNCONFIRMED_GRACE_MS;
      yield* world.at(due);
      expect(yield* world.gate.isHeld).toBe(false);
      expect(yield* world.state).toEqual(afterFailure(due));
      expect(yield* world.resets).toBe(1);
    }),
  );

  it.effect("does not hold when the update marker cannot be written", () =>
    Effect.gen(function* () {
      const memory = yield* memoryUpdateHoldStore;
      const marker = {
        ...memory,
        write: () => Effect.fail(new UpdateHoldMarkerError({ reason: "disk full" })),
      };
      const world = yield* makeWorld({ marker });
      yield* world.at(DUE);
      yield* world.at(DUE + 1);
      expect((yield* world.calls).includes("handoff accepted")).toBe(false);
      const state = yield* world.state;
      expect(state.phase === "waiting" && state.lastReason).toContain("could not be recorded");
      expect(yield* world.gate.isHeld).toBe(false);
    }),
  );

  it.effect("does not check for updates until work held by the last update has started", () =>
    Effect.gen(function* () {
      const marker = yield* memoryUpdateHoldStore;
      const left = { heldAt: DateTime.formatIso(DateTime.makeUnsafe(DUE)), targetVersion: CURRENT };
      yield* marker.write(left);
      const gate = yield* makeWorkAdmissionGate({ marker });
      for (const consumer of WORK_ADMISSION_CONSUMERS) yield* gate.register(consumer);
      const world = yield* makeWorld({ gate, marker });
      // Startup recovery kept the held work; the update is not committed yet, so none started.
      yield* TestClock.setTime(DUE);
      expect(yield* gate.heldForUpdate).toBeDefined();
      for (let minute = 0; minute < 5; minute++) {
        yield* world.at(DUE + minute * QUIET_TICK);
      }
      expect(yield* world.state).toEqual(initialAutoUpdateState(0));
      expect(yield* world.calls).toEqual([]);
      expect(yield* gate.isHeld).toBe(false);
      expect(yield* marker.current).toEqual(left);
      // Startup started it: updates go on as usual.
      yield* gate.finishHeldWork;
      yield* world.at(DUE + 5 * QUIET_TICK);
      expect((yield* world.state).phase).toBe("waiting");
      expect(yield* marker.current).toBeUndefined();
    }),
  );
});

const QUIET_TICK = AUTO_UPDATE_QUIET_POLL_MS;

describe("runningTerminalCommands", () => {
  const terminal = (terminalId: string, hasRunningSubprocess: boolean) => ({
    threadId: "thread-1",
    terminalId,
    cwd: "/work",
    worktreePath: null,
    status: "running" as const,
    pid: 10,
    exitCode: null,
    exitSignal: null,
    hasRunningSubprocess,
    label: terminalId,
    updatedAt: "2026-01-01T00:00:00.000Z",
  });

  it.effect("follows snapshots, upserts and removals", () =>
    Effect.gen(function* () {
      const listeners: Array<(event: TerminalMetadataStreamEvent) => Effect.Effect<void>> = [];
      let unsubscribed = 0;
      const send = (event: TerminalMetadataStreamEvent) =>
        Effect.forEach(listeners, (listener) => listener(event), { discard: true });
      yield* Effect.scoped(
        Effect.gen(function* () {
          const count = yield* runningTerminalCommands({
            subscribeMetadata: (listener) =>
              Effect.sync(() => {
                listeners.push(listener);
                return () => {
                  unsubscribed++;
                };
              }),
          });
          yield* send({
            type: "snapshot",
            terminals: [terminal("a", true), terminal("b", false), terminal("c", true)],
          });
          expect(yield* count).toBe(2);
          yield* send({ type: "upsert", terminal: terminal("b", true) });
          yield* send({ type: "upsert", terminal: terminal("a", false) });
          expect(yield* count).toBe(2);
          yield* send({ type: "remove", threadId: "thread-1", terminalId: "c" });
          expect(yield* count).toBe(1);
          // A new snapshot replaces everything seen before.
          yield* send({ type: "snapshot", terminals: [terminal("d", false)] });
          expect(yield* count).toBe(0);
        }),
      );
      expect(unsubscribed).toBe(1);
    }),
  );
});
