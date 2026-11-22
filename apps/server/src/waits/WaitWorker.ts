/**
 * Fork (waits): the one wait service of a server, its judges, and its sweep on the shared
 * scheduler. The sweep reads nothing while waits are off; a due hint skips idle ticks, and is
 * cleared by a register or cancel, a settings change, a long check's answer, and the thread and
 * run events that can move work earlier.
 */
import type { OrchestrationV2DomainEvent } from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as Stream from "effect/Stream";

import { ServerConfig } from "../config.ts";
import * as ContinuationSlot from "../fork/continuationSlot.ts";
import * as LimitResumeSlot from "../fork/limitResumeSlot.ts";
import * as LensStartServer from "../fork/reviewRules/lensStartServer.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as ProcessRunner from "../processRunner.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as DueHint from "../scheduling/DueHint.ts";
import * as Scheduler from "../scheduling/Scheduler.ts";
import { forkParked } from "../serverActivation.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as WaitDelivery from "./WaitDelivery.ts";
import {
  type CheckPlace,
  makeCheckJudge,
  makeUnitJudge,
  type WaitJudgeContext,
} from "./WaitJudges.ts";
import { makePrJudge } from "./WaitPrJudge.ts";
import * as WaitService from "./WaitService.ts";
import { WAIT_STORE_FILE } from "./WaitStore.ts";

/**
 * The server's wait service. Fork (review rules): it also carries lens_start's reads and starts
 * (`lensStart`), built here with the waits whose `then` it runs; absent where none was built.
 */
export class Waits extends Context.Service<
  Waits,
  WaitService.WaitService & { readonly lensStart?: LensStartServer.LensStartServer }
>()("t3/waits/WaitWorker/Waits") {}

/** A thread that ran, stopped, was archived or deleted may let a held message go. */
const affectsWaits = (event: OrchestrationV2DomainEvent) =>
  event.type.startsWith("thread.") || event.type === "run.created" || event.type === "run.updated";

/**
 * Where a check runs: the project the thread is in now (the stored one when the thread is gone;
 * its wait is then handed over, not judged), and that project's main workspace root.
 */
const makeCheckPlaceOf = Effect.fn("WaitWorker.makeCheckPlaceOf")(function* () {
  const threads = yield* ThreadManagement.ThreadManagementService;
  const projects = yield* ProjectService.ProjectService;
  return (context: WaitJudgeContext): Effect.Effect<CheckPlace> =>
    Effect.gen(function* () {
      const shell = yield* threads.getThreadShell(context.threadId).pipe(Effect.option);
      if (shell._tag === "None") {
        return { type: "unreadable", message: "The thread cannot be read now." } as const;
      }
      const projectId = shell.value?.projectId ?? context.projectId;
      const project = yield* projects.getById(projectId).pipe(Effect.option);
      if (project._tag === "None") {
        return { type: "unreadable", message: "The project cannot be read now." } as const;
      }
      if (project.value._tag === "None") {
        return { type: "gone", message: "The project of this wait is gone." } as const;
      }
      return { type: "ok", projectId, cwd: project.value.value.workspaceRoot } as const;
    });
});

/** A thread that is gone hands its `waiting` waits to its parent. */
const isGone = (event: OrchestrationV2DomainEvent) =>
  event.type === "thread.archived" || event.type === "thread.deleted";

export const layer = Layer.effect(
  Waits,
  Effect.gen(function* () {
    const config = yield* ServerConfig;
    const path = yield* Path.Path;
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    const settings = yield* ServerSettingsService;
    const scheduler = yield* Scheduler.Scheduler;
    const due = DueHint.make();
    const checkPlaceOf = yield* makeCheckPlaceOf();
    const lensStart = yield* LensStartServer.makeLensStartServer();
    const waits = yield* WaitService.make({
      filePath: path.join(config.stateDir, WAIT_STORE_FILE),
      // The wake-ups it sends take a slot like any start (the same gate as below).
      delivery: yield* WaitDelivery.make(Option.some(yield* SlotGate.SlotGate)),
      judges: {
        check: yield* makeCheckJudge({ placeOf: checkPlaceOf }),
        unit: yield* makeUnitJudge(),
        pr: yield* makePrJudge(),
      },
      onChange: due.invalidate,
      // Each run is timed by the project its thread is in now.
      checkPlaceOf,
      // The server's one gate (layers memoize by reference): slot waits and delegate_task share it.
      slots: yield* SlotGate.SlotGate,
      // lens_start {afterCi}: starts the head's lenses once its wake is in the thread.
      action: lensStart.action,
    });
    yield* forkParked(
      Stream.runForEach(orchestrator.streamDomainEvents, (event) =>
        !affectsWaits(event)
          ? Effect.void
          : isGone(event)
            ? waits.receiverChanged(event.threadId).pipe(Effect.andThen(due.invalidate))
            : due.invalidate,
      ),
    );
    yield* forkParked(Stream.runForEach(settings.streamChanges, () => due.invalidate));
    yield* scheduler.register(
      "waits",
      Effect.gen(function* () {
        const nowMs = yield* Clock.currentTimeMillis;
        if (yield* due.isIdle(nowMs)) return;
        const readVersion = yield* due.version;
        const { nextDueAt } = yield* waits.runDue();
        yield* due.record(readVersion, nextDueAt, nowMs);
      }),
    );
    return { ...waits, lensStart };
  }),
).pipe(
  Layer.provide(ProcessRunner.layer),
  // The server's one slot gate: layers memoize by reference.
  Layer.provide(SlotGate.layer),
);

/**
 * Upstream's usage-limit resume, which waits for a slot as a wait's wake-up does
 * (fork/limitResumeSlot.ts). runtimeLayer.ts gives it to upstream's recovery worker from here, so
 * that file needs no import of a fork module more than it has.
 */
export const limitResumeSlot = LimitResumeSlot.layer;

/**
 * Upstream's provider continuation, which waits for a slot as a wait's wake-up does
 * (fork/continuationSlot.ts). runtimeLayer.ts gives it to upstream's continuation worker from
 * here, so that file needs no import of a fork module more than it has.
 */
export const continuationSlot = ContinuationSlot.layer;
