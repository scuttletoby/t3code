/**
 * Fork (playtest): the one playtest service of a server and its sweep on the shared scheduler. Claim
 * and push run in the thread's worktree, renew and release in `<state>/playtest`. A due hint skips
 * idle ticks; every change, a settings change and an archived or deleted thread clear it.
 */
import * as Clock from "effect/Clock";
import * as Context from "effect/Context";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as Stream from "effect/Stream";
import { type OrchestrationV2DomainEvent, type ThreadId, TurnItemId } from "@t3tools/contracts";
import * as IdAllocator from "@t3tools/provider-core/server/IdAllocator";

import { ServerConfig } from "../config.ts";
import * as EventSink from "../orchestration-v2/EventSink.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as ProcessRunner from "../processRunner.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as DueHint from "../scheduling/DueHint.ts";
import * as Scheduler from "../scheduling/Scheduler.ts";
import { forkParked } from "../serverActivation.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import * as PlaytestService from "./PlaytestService.ts";
import { PLAYTEST_STORE_FILE } from "./PlaytestStore.ts";

export class Playtest extends Context.Service<Playtest, PlaytestService.PlaytestService>()(
  "t3/playtest/PlaytestLayer/Playtest",
) {}

const makePlaceOf = Effect.fn("PlaytestLayer.makePlaceOf")(function* () {
  const threads = yield* ThreadManagement.ThreadManagementService;
  const projects = yield* ProjectService.ProjectService;
  return (threadId: ThreadId): Effect.Effect<PlaytestService.PlaytestPlace> =>
    Effect.gen(function* () {
      const shell = yield* threads.getThreadShell(threadId).pipe(Effect.option);
      if (shell._tag === "None") {
        return { type: "unavailable", message: "The thread cannot be read now." } as const;
      }
      const thread = shell.value;
      if (thread === null || thread.archivedAt !== null || thread.deletedAt !== null) {
        return { type: "unavailable", message: "The thread is archived or gone." } as const;
      }
      const project = yield* projects.getById(thread.projectId).pipe(Effect.option);
      if (project._tag === "None" || project.value._tag === "None") {
        return {
          type: "unavailable",
          message: "The thread's project cannot be read now.",
        } as const;
      }
      return {
        type: "ok",
        projectId: thread.projectId,
        worktree: thread.worktreePath ?? project.value.value.workspaceRoot,
      } as const;
    });
});

/** True when the thread is archived, deleted or unknown; null when it cannot be read now. */
const makeThreadGone = Effect.fn("PlaytestLayer.makeThreadGone")(function* () {
  const threads = yield* ThreadManagement.ThreadManagementService;
  return (threadId: ThreadId): Effect.Effect<boolean | null> =>
    threads.getThreadShell(threadId).pipe(
      Effect.map(
        (thread) => thread === null || thread.archivedAt !== null || thread.deletedAt !== null,
      ),
      Effect.orElseSucceed(() => null),
    );
});

/** A message the person wrote (not an agent's or T3's own), as activity for its thread's session. */
export const personActivity = (
  event: OrchestrationV2DomainEvent,
): { readonly threadId: ThreadId; readonly atMs: number } | null =>
  event.type === "message.updated" &&
  event.payload.role === "user" &&
  event.payload.createdBy === "user"
    ? { threadId: event.payload.threadId, atMs: DateTime.toEpochMillis(event.payload.createdAt) }
    : null;

/**
 * The end notice for the person: a `system_notice` item (never a message), so it starts no agent
 * turn. Its fixed id keeps it one item however often it is written. A gone thread drops it; an
 * unreadable thread or a failed write is tried again later (`retry`).
 */
export const makeNotify = Effect.fn("PlaytestLayer.makeNotify")(function* () {
  const threads = yield* ThreadManagement.ThreadManagementService;
  const eventSink = yield* EventSink.EventSinkV2;
  const ids = yield* IdAllocator.IdAllocatorV2;
  return (
    notice: PlaytestService.PlaytestNotice,
  ): Effect.Effect<PlaytestService.PlaytestNoticeResult> =>
    Effect.gen(function* () {
      const shell = yield* threads.getThreadShell(notice.threadId);
      if (shell === null || shell.archivedAt !== null || shell.deletedAt !== null) {
        return "dropped" as const;
      }
      const threadId = notice.threadId;
      const runId = shell.latestRunId;
      const now = yield* DateTime.now;
      yield* eventSink.write({
        events: [
          {
            id: yield* ids.allocate.event({ threadId }),
            type: "turn-item.updated",
            threadId,
            // A thread with no run yet: the event has none; the item says null.
            ...(runId === null ? {} : { runId }),
            occurredAt: now,
            payload: {
              id: TurnItemId.make(notice.noticeId),
              threadId,
              runId,
              nodeId: null,
              providerThreadId: null,
              providerTurnId: null,
              nativeItemRef: null,
              parentItemId: null,
              // EventSink allocates the real position; an item already there keeps its own.
              ordinal: 0,
              status: "completed",
              title: notice.title,
              startedAt: now,
              completedAt: now,
              updatedAt: now,
              type: "system_notice",
              level: notice.level,
              message: notice.text,
            },
          },
        ],
      });
      return "delivered" as const;
    }).pipe(
      Effect.catchCause((cause) =>
        Effect.logWarning(
          `playtest: the notice for thread ${notice.threadId} could not be written now`,
          cause,
        ).pipe(Effect.as("retry" as const)),
      ),
    );
});

const isGone = (event: OrchestrationV2DomainEvent) =>
  event.type === "thread.archived" || event.type === "thread.deleted";

export const layer = Layer.effect(
  Playtest,
  Effect.gen(function* () {
    const config = yield* ServerConfig;
    const path = yield* Path.Path;
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    const settings = yield* ServerSettingsService;
    const scheduler = yield* Scheduler.Scheduler;
    const due = DueHint.make();
    const playtest = yield* PlaytestService.make({
      filePath: path.join(config.stateDir, PLAYTEST_STORE_FILE),
      stepsFolder: path.join(config.stateDir, "playtest"),
      placeOf: yield* makePlaceOf(),
      threadGone: yield* makeThreadGone(),
      notify: yield* makeNotify(),
      onChange: due.invalidate,
    });
    yield* forkParked(
      Stream.runForEach(orchestrator.streamDomainEvents, (event) => {
        if (isGone(event)) return due.invalidate;
        const activity = personActivity(event);
        return activity === null ? Effect.void : playtest.noteActivity(activity);
      }),
    );
    yield* forkParked(Stream.runForEach(settings.streamChanges, () => due.invalidate));
    yield* scheduler.register(
      "playtest",
      Effect.gen(function* () {
        const nowMs = yield* Clock.currentTimeMillis;
        if (yield* due.isIdle(nowMs)) return;
        const readVersion = yield* due.version;
        const { nextDueAt } = yield* playtest.runDue();
        yield* due.record(readVersion, nextDueAt, nowMs);
      }),
    );
    return playtest;
  }),
).pipe(Layer.provide(ProcessRunner.layer));
