/**
 * Fork (waits): the one wait service of a server, and its sweep on the shared scheduler. The
 * sweep reads nothing while waits are off; a due hint skips idle ticks, and is cleared by a
 * register or cancel, a settings change, and the thread and run events that can move work earlier.
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
import * as DueHint from "../scheduling/DueHint.ts";
import * as Scheduler from "../scheduling/Scheduler.ts";
import { forkParked } from "../serverActivation.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import * as WaitDelivery from "./WaitDelivery.ts";
import * as WaitService from "./WaitService.ts";
import { WAIT_STORE_FILE } from "./WaitStore.ts";

export class Waits extends Context.Service<Waits, WaitService.WaitService>()(
  "t3/waits/WaitWorker/Waits",
) {}

/** A thread that ran, stopped, was archived or deleted may let a held message go. */
const affectsWaits = (event: OrchestrationV2DomainEvent) =>
  event.type.startsWith("thread.") || event.type === "run.created" || event.type === "run.updated";

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
    const waits = yield* WaitService.make({
      filePath: path.join(config.stateDir, WAIT_STORE_FILE),
      delivery: yield* WaitDelivery.make(),
      onChange: due.invalidate,
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
);
