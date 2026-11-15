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
import * as Path from "effect/Path";
import * as Stream from "effect/Stream";

import { ServerConfig } from "../config.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as ProcessRunner from "../processRunner.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as DueHint from "../scheduling/DueHint.ts";
import * as Scheduler from "../scheduling/Scheduler.ts";
import { forkParked } from "../serverActivation.ts";
import { ServerSettingsService } from "../serverSettings.ts";
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

export class Waits extends Context.Service<Waits, WaitService.WaitService>()(
  "t3/waits/WaitWorker/Waits",
) {}

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
    const waits = yield* WaitService.make({
      filePath: path.join(config.stateDir, WAIT_STORE_FILE),
      delivery: yield* WaitDelivery.make(),
      judges: {
        check: yield* makeCheckJudge({ placeOf: checkPlaceOf }),
        unit: yield* makeUnitJudge(),
        pr: yield* makePrJudge(),
      },
      onChange: due.invalidate,
      // Each run is timed by the project its thread is in now.
      checkPlaceOf,
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
    return waits;
  }),
).pipe(Layer.provide(ProcessRunner.layer));
