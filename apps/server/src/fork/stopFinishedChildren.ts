/**
 * Fork (issue 925): stops a finished delegated child's own provider session after
 * `stopFinishedChildren.graceSeconds` idle, through upstream's idle release. Event-driven, one
 * in-memory timer per thread; a restart ends every session anyway. Docs: docs/fork/README.md.
 */
import {
  isLiveSubagentShell,
  type OrchestrationV2DomainEvent,
  type OrchestrationV2Run,
  type OrchestrationV2Subagent,
  type ProviderSessionId,
  type ThreadId,
} from "@t3tools/contracts";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FiberMap from "effect/FiberMap";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Scope from "effect/Scope";
import * as Stream from "effect/Stream";

import * as ProviderSessionManager from "../orchestration-v2/ProviderSessionManager.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import { forkParked } from "../serverActivation.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import { Waits } from "../waits/WaitWorker.ts";
import { isDelegatedTask, isEndedTask } from "./delegatedResults/delegatedResults.ts";

/** What one check did; tests read it. */
export type CheckOutcome =
  | { readonly type: "off" }
  /** Not a delegated child: neither a local `delegate_task` child nor one a linked machine sent. */
  | { readonly type: "notChild" }
  | { readonly type: "gone" }
  | { readonly type: "unreadable" }
  /** Working, a pending question or approval, background work, or an open task (or one it gave). */
  | { readonly type: "busy" }
  /** A `wait_for` wait that will wake it (or a wait store that cannot be read). */
  | { readonly type: "waiting" }
  /** No live session of its own; a shared one (Codex, OpenCode) is left to upstream. */
  | { readonly type: "none" }
  | { readonly type: "stopped"; readonly providerSessionIds: ReadonlyArray<ProviderSessionId> };

const WORKING_RUN_STATUSES = ["preparing", "queued", "starting", "running", "waiting"] as const;
const TERMINAL_RUN_STATUSES: ReadonlySet<string> = new Set([
  "completed",
  "failed",
  "interrupted",
  "cancelled",
  "rolled_back",
]);
/** Events on a thread found busy that may leave it idle without a run ending. */
const WATCHED_EVENT_TYPES: ReadonlySet<string> = new Set([
  "runtime-request.updated",
  "provider-thread.updated",
]);

const STOP_DETAIL = "Delegated task finished; its idle provider session was stopped.";

export const make = Effect.fn("StopFinishedChildren.make")(function* () {
  const threads = yield* ThreadManagement.ThreadManagementService;
  const sessions = yield* ProviderSessionManager.ProviderSessionManagerV2;
  const settings = yield* ServerSettingsService;
  const waits = yield* Waits;
  const scope = yield* Scope.Scope;
  // Children a check found busy, waiting or unreadable: their next request or background-work
  // change arms again (a run end always does). In memory, as the timers are.
  const watched = new Set<ThreadId>();

  const readSettings = settings.getSettings.pipe(
    Effect.map((current) => current.stopFinishedChildren),
    Effect.orElseSucceed(() => null),
  );

  /** The thread has no run, request, background work or open task, read from the store now. */
  const idleNow = Effect.fn("StopFinishedChildren.idleNow")(function* (threadId: ThreadId) {
    const shell = yield* threads.getThreadShell(threadId);
    if (shell === null || shell.deletedAt !== null) return "gone" as const;
    const local =
      shell.lineage.relationshipToParent === "subagent" && shell.lineage.parentThreadId !== null;
    if (!local && shell.delegatedFrom === undefined) return "notChild" as const;
    if (isLiveSubagentShell(shell)) return "busy" as const;
    // The shell leaves out silent runs (idle compaction) and queued ones.
    const { runs } = yield* threads.getThreadRecords(threadId, ["runs"], {
      runStatuses: [...WORKING_RUN_STATUSES],
    });
    if (runs.some((run) => (WORKING_RUN_STATUSES as ReadonlyArray<string>).includes(run.status))) {
      return "busy" as const;
    }
    if (local) {
      // A provider's own subagent shares its parent's process: only `delegate_task` children.
      const { subagents } = yield* threads.getThreadRecords(
        shell.lineage.parentThreadId!,
        ["subagents"],
        { subagentChildThreadIds: [threadId] },
      );
      const tasks = subagents.filter(
        (task) => task.childThreadId === threadId && isDelegatedTask(task),
      );
      if (tasks.length === 0) return "notChild" as const;
      if (tasks.some((task) => !isEndedTask(task))) return "busy" as const;
    }
    // A child that delegated a task of its own waits for that task's result.
    const own = yield* threads.getThreadRecords(threadId, ["subagents"]);
    if (own.subagents.some((task) => isDelegatedTask(task) && !isEndedTask(task))) {
      return "busy" as const;
    }
    const open = yield* waits.list({ threadId, projectId: shell.projectId }).pipe(
      Effect.map((result) => result.storeError !== undefined || result.waits.length > 0),
      Effect.orElseSucceed(() => true),
    );
    if (open) return "waiting" as const;
    return "idle" as const;
  });

  const check = Effect.fn("StopFinishedChildren.check")(function* (threadId: ThreadId) {
    const current = yield* readSettings;
    if (current === null || !current.enabled) {
      watched.delete(threadId);
      return { type: "off" } satisfies CheckOutcome;
    }
    const state = yield* idleNow(threadId).pipe(Effect.orElseSucceed(() => "unreadable" as const));
    if (state !== "idle") {
      if (state === "gone" || state === "notChild") watched.delete(threadId);
      else watched.add(threadId);
      return { type: state } satisfies CheckOutcome;
    }
    // Before the release: its own events must not arm this thread again.
    watched.delete(threadId);
    const records = yield* threads
      .getThreadRecords(threadId, ["providerSessions", "providerThreads"])
      .pipe(Effect.orElseSucceed(() => null));
    if (records === null) {
      watched.add(threadId);
      return { type: "unreadable" } satisfies CheckOutcome;
    }
    const own = new Set(
      records.providerThreads.flatMap((providerThread) =>
        providerThread.appThreadId === threadId && providerThread.providerSessionId !== null
          ? [providerThread.providerSessionId]
          : [],
      ),
    );
    // One process per thread only: a shared process serves other threads too.
    const candidates = records.providerSessions.filter(
      (session) =>
        own.has(session.id) &&
        !session.capabilities.sessions.supportsMultipleProviderThreadsPerSession,
    );
    const stopped: Array<ProviderSessionId> = [];
    for (const session of candidates) {
      if (!(yield* sessions.isLive(session.id))) continue;
      const runtime = yield* sessions
        .get(session.id)
        .pipe(Effect.orElseSucceed(() => Option.none()));
      if (Option.isNone(runtime)) continue;
      const pending =
        runtime.value.hasPendingBackgroundWork === undefined
          ? false
          : yield* runtime.value.hasPendingBackgroundWork.pipe(
              Effect.catchCause(() => Effect.succeed(true)),
            );
      if (pending) {
        watched.add(threadId);
        return { type: "busy" } satisfies CheckOutcome;
      }
      // Upstream checks its busy turns atomically with turn starts; `keepIf` reads the thread
      // again in that same step, so a message that arrived meanwhile keeps the session.
      const release = yield* sessions
        .releaseUnlessBusy({
          providerSessionId: session.id,
          ownProviderThreadIds: [],
          throughRunOrdinal: 0,
          keepIf: idleNow(threadId).pipe(
            Effect.map((again) => again !== "idle"),
            Effect.orElseSucceed(() => true),
          ),
          reason: "idle_timeout",
          detail: STOP_DETAIL,
        })
        .pipe(
          Effect.catchCause((cause) =>
            Effect.logWarning("stop finished child: release failed", {
              threadId,
              providerSessionId: session.id,
              cause,
            }).pipe(Effect.as(false)),
          ),
          // A newer event replaces this timer; the release it began still runs to its end.
          Effect.forkIn(scope),
        );
      if (yield* Fiber.join(release)) stopped.push(session.id);
    }
    // Kept by a recheck (it is busy again) or nothing live: either way its next run end arms.
    if (stopped.length === 0) return { type: "none" } satisfies CheckOutcome;
    yield* Effect.logInfo("stop finished child: provider session stopped", {
      threadId,
      providerSessionIds: stopped,
    });
    return { type: "stopped", providerSessionIds: stopped } satisfies CheckOutcome;
  });

  /** The thread an event arms, if any. */
  const armedThread = (event: OrchestrationV2DomainEvent): ThreadId | null => {
    if (event.type === "subagent.updated") {
      const task = event.payload as OrchestrationV2Subagent;
      return isDelegatedTask(task) && isEndedTask(task) && task.childThreadId !== null
        ? task.childThreadId
        : null;
    }
    if (event.type === "run.updated") {
      const run = event.payload as Pick<OrchestrationV2Run, "status">;
      return TERMINAL_RUN_STATUSES.has(run.status) ? event.threadId : null;
    }
    return WATCHED_EVENT_TYPES.has(event.type) && watched.has(event.threadId)
      ? event.threadId
      : null;
  };

  const start = Effect.fn("StopFinishedChildren.start")(function* () {
    // Keyed by child: a newer event restarts its grace, so one quiet moment gets one check.
    const timers = yield* FiberMap.make<ThreadId>();
    const onEvent = Effect.fnUntraced(function* (event: OrchestrationV2DomainEvent) {
      if (event.type === "run.created") {
        // Working again: the run's end arms a new grace.
        return yield* FiberMap.remove(timers, event.threadId);
      }
      const threadId = armedThread(event);
      if (threadId === null) return;
      const current = yield* readSettings;
      if (current === null || !current.enabled) return;
      yield* FiberMap.run(
        timers,
        threadId,
        Effect.sleep(Duration.seconds(current.graceSeconds)).pipe(
          Effect.andThen(check(threadId)),
          Effect.catchCause((cause) =>
            Effect.logWarning("stop finished child: check failed", { threadId, cause }),
          ),
        ),
      );
    });
    yield* forkParked(Stream.runForEach(threads.streamDomainEvents, onEvent));
  });

  return { check, start, armedThread, watched };
});

/** Started with the server's other workers, from `ForkLayers.layerRuntimeServices`. */
export const layer = Layer.effectDiscard(make().pipe(Effect.flatMap((service) => service.start())));
