// Automatic updates for boot-service installs, once threads are quiet and the optional hook agrees.
// The handoff closes the WorkAdmission gate and writes a marker; held work starts once the
// restarted server's update is committed. The gate stays closed until exit or the launcher's
// record says no restart is coming (AUTO_UPDATE_MAX_HOLD_MS applies only while not pending).
import {
  ServerSelfUpdateError,
  type ServerSelfUpdateOutcome,
  type ServerSettingsError,
} from "@t3tools/contracts";
import { cliReleaseChannelOf } from "@t3tools/shared/cliRelease";
import { derivePendingBackgroundWork } from "@t3tools/shared/orchestrationV2PendingBackgroundWork";
import { latestUnheldRun } from "@t3tools/shared/orchestrationV2ThreadError";
import * as Clock from "effect/Clock";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as Predicate from "effect/Predicate";
import * as Ref from "effect/Ref";
import { HttpClient } from "effect/http";

import packageJson from "../../package.json" with { type: "json" };
import { resolveMirrorVersion, resolveNewestVersion } from "../cli/update.ts";
import * as ServerConfig from "../config.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import { managedUpdateMirror } from "../policy/managedUpdates.ts";
import * as ProcessRunner from "../processRunner.ts";
import { forkParked } from "../serverActivation.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as TerminalManager from "../terminal/Manager.ts";
import { WorkAdmission, type WorkAdmissionGate } from "../workAdmission.ts";
import { ServerSelfUpdate } from "./selfUpdate.ts";
import * as ServiceLauncherClient from "./serviceLauncherClient.ts";
import {
  compareExactServiceVersions,
  isExactServiceVersion,
  parseServiceState,
  type PendingServiceUpdate,
  SERVICE_STATE_FILE,
} from "./serviceProtocol.ts";

const MINUTE = 60_000;
/** The first look waits out startup recovery and any restart continuations it starts. */
export const AUTO_UPDATE_FIRST_CHECK_DELAY_MS = 5 * MINUTE;
export const AUTO_UPDATE_CHECK_INTERVAL_MS = 60 * MINUTE;
export const AUTO_UPDATE_QUIET_POLL_MS = MINUTE;
/** How long one found release waits for quiet before the wait ends until the next check. */
export const AUTO_UPDATE_MAX_QUIET_WAIT_MS = 2 * 60 * MINUTE;
/** After a failed attempt, so a release that cannot install is not fetched every hour. */
export const AUTO_UPDATE_FAILURE_BACKOFF_MS = 6 * 60 * MINUTE;
/** New work waits at most this long for the launcher to restart the server, then starts again,
 * unless the launcher's record still shows this update pending. */
export const AUTO_UPDATE_MAX_HOLD_MS = 30 * MINUTE;
/** When the launcher's answer was lost (its 30 s reply timeout), how long its record may take to
 * show the update pending before the handoff counts as dropped. */
export const AUTO_UPDATE_UNCONFIRMED_GRACE_MS = 5 * MINUTE;
const HOOK_TIMEOUT_MS = 30_000;
const HOOK_MAX_OUTPUT_BYTES = 16 * 1024;
const TICK = "1 minute";

export type AutoUpdateState =
  | { readonly phase: "idle"; readonly nextCheckAt: number }
  | {
      readonly phase: "waiting";
      readonly version: string;
      readonly since: number;
      readonly nextQuietCheckAt: number;
      readonly lastReason: string | null;
    }
  | {
      readonly phase: "handed-off";
      readonly version: string;
      /** When new work started waiting. */
      readonly since: number;
      /** The launcher said it accepted, or its record shows this update pending. */
      readonly confirmed: boolean;
    };

export const initialAutoUpdateState = (nowMs: number): AutoUpdateState => ({
  phase: "idle",
  nextCheckAt: nowMs + AUTO_UPDATE_FIRST_CHECK_DELAY_MS,
});

/** The launcher's last update record, from `runtime/service-state.json`. */
export type LauncherRecord =
  | { readonly type: "none" }
  | { readonly type: "unreadable"; readonly reason: string }
  | { readonly type: "update"; readonly update: PendingServiceUpdate | ServerSelfUpdateOutcome };

export type ReleaseDecision =
  | { readonly type: "none" }
  | { readonly type: "offer"; readonly version: string }
  /** The launcher rolled this release back: it does not run here, so only a newer one is tried. */
  | { readonly type: "skip-rolled-back"; readonly version: string; readonly reason?: string }
  /** The last attempt at this release failed around it (backup, start, state): retry after a wait. */
  | {
      readonly type: "back-off-failed";
      readonly version: string;
      readonly updateId: string;
      readonly reason?: string;
    }
  | { readonly type: "launcher-busy"; readonly version: string };

/** Whether `candidate` is worth installing over `current`: newer, on the same track, not rolled
 * back, and not a failure this process has not waited out yet (`backedOffFailureId`). */
export function decideRelease(input: {
  readonly current: string;
  readonly candidate: string | undefined;
  readonly lastUpdate: PendingServiceUpdate | ServerSelfUpdateOutcome | undefined;
  readonly backedOffFailureId: string | undefined;
}): ReleaseDecision {
  const { current, candidate, lastUpdate } = input;
  if (candidate === undefined || !isExactServiceVersion(candidate)) return { type: "none" };
  if (cliReleaseChannelOf(candidate) !== cliReleaseChannelOf(current)) return { type: "none" };
  if (compareExactServiceVersions(candidate, current) <= 0) return { type: "none" };
  if (lastUpdate?.status === "pending") return { type: "launcher-busy", version: candidate };
  if (lastUpdate !== undefined && lastUpdate.targetVersion === candidate) {
    const reason = lastUpdate.reason === undefined ? {} : { reason: lastUpdate.reason };
    if (lastUpdate.status === "rolled-back") {
      return { type: "skip-rolled-back", version: candidate, ...reason };
    }
    if (lastUpdate.status === "failed" && lastUpdate.id !== input.backedOffFailureId) {
      return { type: "back-off-failed", version: candidate, updateId: lastUpdate.id, ...reason };
    }
  }
  return { type: "offer", version: candidate };
}

export interface AutoUpdateActivity {
  readonly activeRuns: number;
  readonly queuedRuns: number;
  readonly pendingRequests: number;
  readonly backgroundTasks: number;
  readonly terminalCommands: number;
}

export const noActivity: AutoUpdateActivity = {
  activeRuns: 0,
  queuedRuns: 0,
  pendingRequests: 0,
  backgroundTasks: 0,
  terminalCommands: 0,
};

/** Why thread and terminal state says "not now", or null when nothing is working. Held queued
 * runs do not count: they wait for the user, across restarts too. */
export function activityReason(activity: AutoUpdateActivity): string | null {
  const parts = [
    [activity.activeRuns, "running turn"],
    [activity.queuedRuns, "queued turn"],
    [activity.pendingRequests, "pending approval or question"],
    [activity.backgroundTasks, "background task"],
    [activity.terminalCommands, "running terminal command"],
  ] as const;
  const busy = parts
    .filter(([count]) => count > 0)
    .map(([count, label]) => `${count} ${label}${count === 1 ? "" : "s"}`);
  return busy.length === 0 ? null : busy.join(", ");
}

/** One thread's work, from the same projection startup recovery reads, and counted with the
 * background-work rule the thread list and idle compaction use (every provider's tasks and turn
 * items). Pull request watches are left out: they survive a restart. */
export function threadActivity(
  projection: Pick<
    ProjectionStore.ProjectionRuntimeRecoveryState,
    "thread" | "runs" | "runtimeRequests" | "providerThreads" | "turnItems"
  >,
): AutoUpdateActivity {
  const runs = projection.runs.filter((run) => run.status !== "rolled_back");
  return {
    ...noActivity,
    activeRuns: runs.filter(
      (run) =>
        run.status === "preparing" ||
        run.status === "starting" ||
        run.status === "running" ||
        run.status === "waiting",
    ).length,
    queuedRuns: runs.filter((run) => run.status === "queued" && run.queueHeld !== true).length,
    pendingRequests: projection.runtimeRequests.filter((request) => request.status === "pending")
      .length,
    backgroundTasks: derivePendingBackgroundWork({
      latestRun: latestUnheldRun(runs),
      runs,
      providerThreads: projection.providerThreads,
      turnItems: projection.turnItems,
      activeProviderThreadId: projection.thread.activeProviderThreadId,
    }).length,
  };
}

const addActivity = (left: AutoUpdateActivity, right: AutoUpdateActivity): AutoUpdateActivity => ({
  activeRuns: left.activeRuns + right.activeRuns,
  queuedRuns: left.queuedRuns + right.queuedRuns,
  pendingRequests: left.pendingRequests + right.pendingRequests,
  backgroundTasks: left.backgroundTasks + right.backgroundTasks,
  terminalCommands: left.terminalCommands + right.terminalCommands,
});

/** The quiet hook's answer: exit 0 allows the update; anything else is "not now", with the first
 * line it printed as the reason. */
export function hookReason(
  result:
    | {
        readonly code: number | null;
        readonly stdout: string;
        readonly stderr: string;
        readonly timedOut: boolean;
      }
    | { readonly error: string },
): string | null {
  if ("error" in result) return `the quiet hook could not run (${result.error})`;
  if (result.timedOut) return "the quiet hook did not answer in time";
  if (result.code === 0) return null;
  const said = `${result.stdout}\n${result.stderr}`
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => line !== "");
  return `the quiet hook said not now${said === undefined ? "" : `: ${said.slice(0, 200)}`}`;
}

export type AutoUpdateStep =
  | { readonly type: "none" }
  | { readonly type: "check" }
  | { readonly type: "probe"; readonly version: string }
  | { readonly type: "watch-handoff"; readonly version: string };

/** What a tick does in `state` at `nowMs`. */
export function nextAutoUpdateStep(state: AutoUpdateState, nowMs: number): AutoUpdateStep {
  if (state.phase === "idle")
    return nowMs >= state.nextCheckAt ? { type: "check" } : { type: "none" };
  if (state.phase === "waiting") {
    return nowMs >= state.nextQuietCheckAt
      ? { type: "probe", version: state.version }
      : { type: "none" };
  }
  return { type: "watch-handoff", version: state.version };
}

/** The state after a release check found `version`, or none to install. */
export function afterCheck(nowMs: number, version: string | undefined): AutoUpdateState {
  return version === undefined
    ? { phase: "idle", nextCheckAt: nowMs + AUTO_UPDATE_CHECK_INTERVAL_MS }
    : { phase: "waiting", version, since: nowMs, nextQuietCheckAt: nowMs, lastReason: null };
}

/** After a quiet probe: keep waiting (bounded), give up until the next check, or update now. */
export function afterProbe(
  state: Extract<AutoUpdateState, { phase: "waiting" }>,
  nowMs: number,
  reason: string | null,
): { readonly state: AutoUpdateState; readonly outcome: "update" | "wait" | "give-up" } {
  if (reason === null) return { state, outcome: "update" };
  if (nowMs - state.since >= AUTO_UPDATE_MAX_QUIET_WAIT_MS) {
    return {
      state: { phase: "idle", nextCheckAt: nowMs + AUTO_UPDATE_CHECK_INTERVAL_MS },
      outcome: "give-up",
    };
  }
  return {
    state: { ...state, nextQuietCheckAt: nowMs + AUTO_UPDATE_QUIET_POLL_MS, lastReason: reason },
    outcome: "wait",
  };
}

/** After an install attempt failed: back off longer than a normal check. */
export function afterFailure(nowMs: number): AutoUpdateState {
  return { phase: "idle", nextCheckAt: nowMs + AUTO_UPDATE_FAILURE_BACKOFF_MS };
}

export const readLauncherRecord = (
  fs: FileSystem.FileSystem,
  statePath: string,
): Effect.Effect<LauncherRecord> =>
  fs.readFileString(statePath).pipe(
    Effect.map((contents): LauncherRecord => {
      const state = parseServiceState(contents);
      if (state === undefined) {
        return { type: "unreadable", reason: `${SERVICE_STATE_FILE} is not valid launcher state` };
      }
      return state.update === undefined
        ? { type: "none" }
        : { type: "update", update: state.update };
    }),
    Effect.catch((error) =>
      Effect.succeed<LauncherRecord>(
        error.reason._tag === "NotFound"
          ? { type: "none" }
          : {
              type: "unreadable",
              reason: `${SERVICE_STATE_FILE} could not be read (${error.reason._tag})`,
            },
      ),
    ),
  );

/** Everything the update loop reads or does, so tests drive it with fakes. */
export interface AutoUpdateDeps {
  readonly currentVersion: string;
  /** Desktop-managed servers update with their app; others have no launcher to hand off to. */
  readonly bootService: boolean;
  readonly gate: WorkAdmissionGate;
  readonly preferences: Effect.Effect<
    { readonly automaticUpdates: boolean; readonly automaticUpdatesQuietCommand: string },
    ServerSettingsError
  >;
  readonly newestRelease: Effect.Effect<string | undefined, { readonly reason: string }>;
  readonly launcherRecord: Effect.Effect<LauncherRecord>;
  /** Null when no thread or terminal is working, else why not; read failures are a reason. */
  readonly activity: Effect.Effect<string | null>;
  /** Null when the quiet hook (if any) allows the update, else why not. */
  readonly askHook: (command: string, targetVersion: string) => Effect.Effect<string | null>;
  readonly update: ServerSelfUpdate["Service"]["update"];
  readonly resetAfterDroppedHandoff: Effect.Effect<void>;
}

type InstallResult =
  /** Unconfirmed: the launcher was asked but its answer was lost, so it may still restart. */
  | { readonly type: "handed-off"; readonly since: number; readonly confirmed: boolean }
  /** Work started while staging; the staged runtime is reused by the next attempt. */
  | { readonly type: "busy"; readonly reason: string }
  /** Refused before it began, e.g. the Update button's update is running. */
  | { readonly type: "not-started"; readonly reason: string }
  | { readonly type: "failed"; readonly reason: string };

export const makeAutoUpdater = Effect.fn("cloud.auto_update.make")(function* (
  deps: AutoUpdateDeps,
) {
  const state = yield* Ref.make<AutoUpdateState>(
    initialAutoUpdateState(yield* Clock.currentTimeMillis),
  );
  const loggedUnavailable = yield* Ref.make(false);
  const loggedUnsettled = yield* Ref.make(false);
  const loggedPastCap = yield* Ref.make(false);
  const lastUnreadable = yield* Ref.make<string | null>(null);
  const backedOffFailureId = yield* Ref.make<string | undefined>(undefined);
  const hookCommand = deps.preferences.pipe(
    Effect.map((preferences) => preferences.automaticUpdatesQuietCommand),
  );

  const logUnreadable = (reason: string, message: string) =>
    Ref.getAndSet(lastUnreadable, reason).pipe(
      Effect.flatMap((previous) =>
        previous === reason ? Effect.void : Effect.logWarning(message, { reason }),
      ),
    );

  /** Null when the server may restart now, else why not. The hook first, thread state last. */
  const quietReason = Effect.fn("cloud.auto_update.quietReason")(function* (targetVersion: string) {
    const command = yield* hookCommand.pipe(Effect.orElseSucceed(() => null));
    if (command === null) return "settings could not be read";
    const hook = yield* deps.askHook(command, targetVersion);
    if (hook !== null) return hook;
    return yield* deps.activity;
  });

  /** The last check, right before the handoff: closes the gate, and opens it again unless quiet. */
  const holdIfQuiet = Effect.fn("cloud.auto_update.holdIfQuiet")(function* (targetVersion: string) {
    const preferences = yield* deps.preferences.pipe(Effect.orElseSucceed(() => null));
    if (preferences === null) return "settings could not be read";
    if (!preferences.automaticUpdates) return "automatic server updates were turned off";
    const command = preferences.automaticUpdatesQuietCommand;
    const hook = yield* deps.askHook(command, targetVersion);
    if (hook !== null) return hook;
    // Anything saved after the gate closes is either counted below (and stops the update) or held,
    // and the marker lets startup recovery keep and later start the held part. Recovery decides by
    // that order, not by request times; `heldAt` only tells a fresh marker from a stale one.
    const heldAt = DateTime.formatIso(yield* DateTime.now);
    const starting = yield* deps.gate.hold;
    let reason =
      starting > 0
        ? `${starting} piece${starting === 1 ? "" : "s"} of work starting`
        : yield* deps.activity;
    if (reason === null) {
      reason = yield* deps.gate.markHeldForUpdate({ heldAt, targetVersion }).pipe(
        Effect.as(null),
        Effect.catch((error) =>
          Effect.succeed(`the update hold could not be recorded (${error.reason})`),
        ),
      );
    }
    if (reason !== null) yield* deps.gate.release;
    else {
      yield* Effect.logInfo(
        "Automatic server update: new work waits for the restart and starts after it.",
        { targetVersion, heldAt, maxHoldMinutes: AUTO_UPDATE_MAX_HOLD_MS / MINUTE },
      );
    }
    return reason;
  });

  const install = Effect.fn("cloud.auto_update.install")(function* (targetVersion: string) {
    const lateReason = yield* Ref.make<string | null>(null);
    const started = yield* Ref.make(false);
    /** When the gate closed for good and the launcher is being asked. */
    const heldSince = yield* Ref.make<number | null>(null);
    const accepted = yield* Ref.make(false);
    return yield* deps
      .update(
        { targetVersion },
        (stage) =>
          Ref.set(started, true).pipe(
            Effect.andThen(
              stage !== "installing"
                ? Effect.void
                : holdIfQuiet(targetVersion).pipe(
                    Effect.flatMap((reason) =>
                      reason === null
                        ? Clock.currentTimeMillis.pipe(
                            Effect.flatMap((nowMs) => Ref.set(heldSince, nowMs)),
                          )
                        : Ref.set(lateReason, reason).pipe(
                            Effect.andThen(Effect.fail(new ServerSelfUpdateError({ reason }))),
                          ),
                    ),
                  ),
            ),
          ),
        () => Ref.set(accepted, true),
      )
      .pipe(
        Effect.andThen(
          Effect.gen(function* () {
            const since = (yield* Ref.get(heldSince)) ?? (yield* Clock.currentTimeMillis);
            return { type: "handed-off", since, confirmed: true } satisfies InstallResult;
          }),
        ),
        Effect.catch((error) =>
          Effect.gen(function* () {
            const late = yield* Ref.get(lateReason);
            // holdIfQuiet opened the gate again itself.
            if (late !== null) return { type: "busy", reason: late } satisfies InstallResult;
            const since = yield* Ref.get(heldSince);
            // The launcher's reply timed out or the IPC failed after it was asked: it may still
            // restart this server, so new work keeps waiting while watchHandoff follows its record.
            if (
              since !== null &&
              ((yield* Ref.get(accepted)) ||
                !Predicate.isTagged(error.cause, "ServiceLauncherRejectedError"))
            ) {
              yield* Effect.logWarning(
                "Automatic server update: the launcher's answer was lost; new work keeps waiting while its state is watched.",
                { targetVersion, reason: error.reason },
              );
              return {
                type: "handed-off",
                since,
                confirmed: yield* Ref.get(accepted),
              } satisfies InstallResult;
            }
            // Refused, or failed before the launcher was asked: no restart is coming.
            yield* deps.gate.release;
            return (yield* Ref.get(started))
              ? ({ type: "failed", reason: error.reason } satisfies InstallResult)
              : ({ type: "not-started", reason: error.reason } satisfies InstallResult);
          }),
        ),
        // Defects and interruptions: nothing says a restart is coming either.
        Effect.onError(() => deps.gate.release),
      );
  });

  const check = Effect.fn("cloud.auto_update.check")(function* (nowMs: number) {
    const record = yield* deps.launcherRecord;
    if (record.type === "unreadable") {
      // Without the record a rolled-back release could be tried again and again.
      yield* logUnreadable(
        record.reason,
        "Automatic server update cannot read the launcher state; not updating.",
      );
      yield* Ref.set(state, afterCheck(nowMs, undefined));
      return;
    }
    yield* Ref.set(lastUnreadable, null);
    const candidate = yield* deps.newestRelease.pipe(
      Effect.tapError((error) =>
        Effect.logWarning("Automatic server update could not list releases.", {
          reason: error.reason,
        }),
      ),
      Effect.orElseSucceed(() => undefined),
    );
    const decision = decideRelease({
      current: deps.currentVersion,
      candidate,
      lastUpdate: record.type === "update" ? record.update : undefined,
      backedOffFailureId: yield* Ref.get(backedOffFailureId),
    });
    switch (decision.type) {
      case "none":
        yield* Ref.set(state, afterCheck(nowMs, undefined));
        return;
      case "launcher-busy":
        yield* Effect.logInfo(
          "Automatic server update waits; the launcher has an update pending.",
          {
            targetVersion: decision.version,
          },
        );
        yield* Ref.set(state, afterCheck(nowMs, undefined));
        return;
      case "skip-rolled-back":
        yield* Effect.logInfo("Automatic server update skips a release that was rolled back.", {
          targetVersion: decision.version,
          reason: decision.reason,
        });
        yield* Ref.set(state, afterCheck(nowMs, undefined));
        return;
      case "back-off-failed":
        yield* Effect.logWarning(
          "Automatic server update: the last attempt at this release failed; retrying later.",
          { targetVersion: decision.version, reason: decision.reason },
        );
        yield* Ref.set(backedOffFailureId, decision.updateId);
        yield* Ref.set(state, afterFailure(nowMs));
        return;
      case "offer":
        yield* Effect.logInfo("Automatic server update found a newer release; waiting for quiet.", {
          currentVersion: deps.currentVersion,
          targetVersion: decision.version,
        });
        yield* Ref.set(state, afterCheck(nowMs, decision.version));
    }
  });

  const dropHandoff = Effect.fn("cloud.auto_update.dropHandoff")(function* (
    current: Extract<AutoUpdateState, { phase: "handed-off" }>,
    nowMs: number,
    why: string,
  ) {
    yield* deps.gate.release;
    yield* deps.resetAfterDroppedHandoff;
    yield* Effect.logWarning(
      "Automatic server update: the launcher did not restart this server; held work starts now.",
      {
        targetVersion: current.version,
        reason: why,
        heldMinutes: Math.round((nowMs - current.since) / MINUTE),
      },
    );
    yield* Ref.set(state, afterFailure(nowMs));
  });

  /** The gate stays closed until exit. It opens again only when the launcher's record shows it
   * will not restart this server, or after AUTO_UPDATE_MAX_HOLD_MS unless the record shows this
   * update pending (the restart is still coming: work that started now would be cut off by it).
   * Before the cap an unreadable record or a lost answer keeps the hold too. */
  const watchHandoff = Effect.fn("cloud.auto_update.watchHandoff")(function* (
    current: Extract<AutoUpdateState, { phase: "handed-off" }>,
    nowMs: number,
  ) {
    const heldMs = nowMs - current.since;
    const pastCap = heldMs >= AUTO_UPDATE_MAX_HOLD_MS;
    const record = yield* deps.launcherRecord;
    if (
      record.type === "update" &&
      record.update.status === "pending" &&
      record.update.targetVersion === current.version
    ) {
      yield* Ref.set(lastUnreadable, null);
      if (!current.confirmed) yield* Ref.set(state, { ...current, confirmed: true });
      if (pastCap && !(yield* Ref.getAndSet(loggedPastCap, true))) {
        yield* Effect.logWarning(
          "Automatic server update: the restart is taking long; new work keeps waiting while the launcher has it pending.",
          { targetVersion: current.version, heldMinutes: Math.round(heldMs / MINUTE) },
        );
      }
      return;
    }
    if (pastCap) {
      return yield* dropHandoff(
        current,
        nowMs,
        `no restart after ${Math.round(heldMs / MINUTE)} minutes`,
      );
    }
    if (record.type === "unreadable") {
      return yield* logUnreadable(
        record.reason,
        "Automatic server update cannot read the launcher state; new work keeps waiting.",
      );
    }
    yield* Ref.set(lastUnreadable, null);
    if (!current.confirmed && heldMs < AUTO_UPDATE_UNCONFIRMED_GRACE_MS) return;
    yield* dropHandoff(
      current,
      nowMs,
      record.type === "update"
        ? `the launcher's record says ${record.update.status} for ${record.update.targetVersion}`
        : "the launcher has no update pending",
    );
  });

  const probe = Effect.fn("cloud.auto_update.probe")(function* (
    current: Extract<AutoUpdateState, { phase: "waiting" }>,
    nowMs: number,
  ) {
    const version = current.version;
    const reason = yield* quietReason(version);
    const probed = afterProbe(current, nowMs, reason);
    if (probed.outcome === "give-up") {
      yield* Effect.logInfo(
        "Automatic server update stopped waiting; it retries at the next check.",
        {
          targetVersion: version,
          reason,
          waitedMinutes: Math.round((nowMs - current.since) / MINUTE),
        },
      );
      yield* Ref.set(state, probed.state);
      return;
    }
    if (probed.outcome === "wait") {
      if (reason !== current.lastReason) {
        yield* Effect.logInfo("Automatic server update is waiting.", {
          targetVersion: version,
          reason,
        });
      }
      yield* Ref.set(state, probed.state);
      return;
    }
    yield* Effect.logInfo("Automatic server update starting; the server is quiet.", {
      currentVersion: deps.currentVersion,
      targetVersion: version,
    });
    const result = yield* install(version);
    const after = yield* Clock.currentTimeMillis;
    switch (result.type) {
      case "handed-off":
        yield* Ref.set(state, {
          phase: "handed-off",
          version,
          since: result.since,
          confirmed: result.confirmed,
        });
        return;
      case "busy":
        yield* Effect.logInfo("Automatic server update is waiting; not quiet at the handoff.", {
          targetVersion: version,
          reason: result.reason,
        });
        yield* Ref.set(state, afterProbe(current, after, result.reason).state);
        return;
      case "not-started":
        yield* Effect.logInfo("Automatic server update is waiting; another update is running.", {
          targetVersion: version,
          reason: result.reason,
        });
        yield* Ref.set(state, afterProbe(current, after, result.reason).state);
        return;
      case "failed":
        yield* Effect.logWarning("Automatic server update failed; it retries later.", {
          targetVersion: version,
          reason: result.reason,
        });
        yield* Ref.set(state, afterFailure(after));
    }
  });

  const tick = Effect.gen(function* () {
    const nowMs = yield* Clock.currentTimeMillis;
    const current = yield* Ref.get(state);
    // A handoff in progress is followed whatever the settings say now.
    if (current.phase === "handed-off") return yield* watchHandoff(current, nowMs);
    const preferences = yield* deps.preferences;
    if (!preferences.automaticUpdates) {
      if (current.phase === "waiting") {
        yield* Effect.logInfo("Automatic server update turned off; not waiting any more.", {
          targetVersion: current.version,
        });
        yield* Ref.set(state, initialAutoUpdateState(nowMs));
      }
      return;
    }
    if (!deps.bootService) {
      if (!(yield* Ref.getAndSet(loggedUnavailable, true))) {
        yield* Effect.logInfo(
          "Automatic server updates need the T3 Code background service; desktop servers update with the app.",
        );
      }
      return;
    }
    // Holding a gate that the outbox, thread launches or recovery do not use would let work start
    // and then be cancelled by the restart.
    const missing = deps.gate.enforced ? yield* deps.gate.missingConsumers : ["shared gate"];
    if (missing.length > 0) {
      if (!(yield* Ref.getAndSet(loggedUnavailable, true))) {
        yield* Effect.logError(
          "Automatic server updates are off: this server cannot hold new work for the restart.",
          { notUsingTheWorkGate: missing },
        );
      }
      return;
    }
    // Work an earlier update held must start before this server holds work again; startup starts
    // it once its own update is committed.
    if (!(yield* deps.gate.heldWorkSettled)) {
      if (!(yield* Ref.getAndSet(loggedUnsettled, true))) {
        yield* Effect.logInfo(
          "Automatic server update waits until work held by the last update has started.",
        );
      }
      return;
    }
    const step = nextAutoUpdateStep(current, nowMs);
    if (step.type === "check") return yield* check(nowMs);
    if (step.type === "probe" && current.phase === "waiting") return yield* probe(current, nowMs);
  }).pipe(
    Effect.catchCause((cause) =>
      Effect.logWarning("Automatic server update tick failed.", { cause }),
    ),
  );

  return { tick, state: Ref.get(state) };
});

/** How many terminals have a command running, kept current from the metadata stream the UI uses. */
export const runningTerminalCommands = Effect.fn("cloud.auto_update.runningTerminalCommands")(
  function* (terminals: Pick<TerminalManager.TerminalManager["Service"], "subscribeMetadata">) {
    const running = yield* Ref.make(new Map<string, boolean>());
    const terminalKey = (terminal: { readonly threadId: string; readonly terminalId: string }) =>
      `${terminal.threadId}\u0000${terminal.terminalId}`;
    const unsubscribe = yield* terminals.subscribeMetadata((event) =>
      Ref.update(running, (current) => {
        const next = event.type === "snapshot" ? new Map<string, boolean>() : new Map(current);
        if (event.type === "remove") next.delete(terminalKey(event));
        else {
          for (const terminal of event.type === "snapshot" ? event.terminals : [event.terminal]) {
            next.set(terminalKey(terminal), terminal.hasRunningSubprocess);
          }
        }
        return next;
      }),
    );
    yield* Effect.addFinalizer(() => Effect.sync(unsubscribe));
    return Ref.get(running).pipe(
      Effect.map((current) => [...current.values()].filter(Boolean).length),
    );
  },
);

const make = Effect.gen(function* () {
  const serverConfig = yield* ServerConfig.ServerConfig;
  const launcher = yield* ServiceLauncherClient.ServiceLauncherClient;
  const settings = yield* ServerSettings.ServerSettingsService;
  const selfUpdate = yield* ServerSelfUpdate;
  const policy = yield* ManagedPolicy.ManagedPolicy;
  const runner = yield* ProcessRunner.ProcessRunner;
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const projections = yield* ProjectionStore.ProjectionStoreV2;
  const terminals = yield* TerminalManager.TerminalManager;
  const gate = yield* WorkAdmission;
  const releaseContext = yield* Effect.context<HttpClient.HttpClient>();
  const mirror = managedUpdateMirror(policy.document);
  const channel = cliReleaseChannelOf(packageJson.version);
  const statePath = path.join(serverConfig.baseDir, "runtime", SERVICE_STATE_FILE);

  const terminalCommands = yield* runningTerminalCommands(terminals);

  const threadWork = projections.getRecoveryThreadIds("runtime").pipe(
    Effect.flatMap((threadIds) =>
      Effect.forEach(
        threadIds,
        (threadId) =>
          projections.getRuntimeRecoveryProjection(threadId).pipe(Effect.map(threadActivity)),
        { concurrency: 2 },
      ),
    ),
    Effect.map((each) => each.reduce(addActivity, noActivity)),
  );
  const activity = Effect.gen(function* () {
    const threads = yield* threadWork;
    return activityReason({ ...threads, terminalCommands: yield* terminalCommands });
  }).pipe(Effect.orElseSucceed(() => "thread state could not be read"));

  const askHook = (command: string, targetVersion: string) => {
    if (command === "") return Effect.succeed(null);
    if (!path.isAbsolute(command)) {
      return Effect.succeed(
        hookReason({ error: "automaticUpdatesQuietCommand must be an absolute path" }),
      );
    }
    return runner
      .run({
        command,
        args: [],
        cwd: serverConfig.baseDir,
        timeout: HOOK_TIMEOUT_MS,
        timeoutBehavior: "timedOutResult",
        maxOutputBytes: HOOK_MAX_OUTPUT_BYTES,
        outputMode: "truncate",
        env: {
          T3CODE_AUTO_UPDATE_CURRENT_VERSION: packageJson.version,
          T3CODE_AUTO_UPDATE_TARGET_VERSION: targetVersion,
        },
      })
      .pipe(
        Effect.map((result) => hookReason({ ...result, code: result.code ?? null })),
        Effect.catch((error) => Effect.succeed(hookReason({ error: error._tag }))),
        Effect.withSpan("cloud.auto_update.ask_hook"),
      );
  };

  const updater = yield* makeAutoUpdater({
    currentVersion: packageJson.version,
    bootService: serverConfig.mode !== "desktop" && launcher.managed,
    gate,
    preferences: settings.getSettings,
    newestRelease: (mirror === undefined
      ? resolveNewestVersion(channel)
      : resolveMirrorVersion(mirror, channel)
    ).pipe(Effect.provideContext(releaseContext)),
    launcherRecord: readLauncherRecord(fs, statePath),
    activity,
    askHook,
    update: selfUpdate.update,
    resetAfterDroppedHandoff: selfUpdate.resetAfterDroppedHandoff,
  });

  yield* forkParked(Effect.sleep(TICK).pipe(Effect.andThen(updater.tick), Effect.forever));
});

export const layer = Layer.effectDiscard(make).pipe(
  Layer.provide(Layer.merge(ProcessRunner.layer, ProjectionStore.layer)),
);
