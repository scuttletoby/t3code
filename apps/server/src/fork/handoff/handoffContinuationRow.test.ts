/**
 * Fork: upstream does not export its restart continuation row's id, so this runs upstream's real
 * writer (the recovery pass over a write turn a restart cut short) and checks the handoff's id is
 * the one it writes: a rename upstream fails here, not as a handoff that stops waiting.
 */
import { describe, expect, it } from "@effect/vitest";
import {
  MessageId,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  ProviderSessionId,
  ProviderThreadId,
  ProviderTurnId,
  RunAttemptId,
  RunId,
  ThreadId,
  type OrchestrationV2ThreadProjection,
} from "@t3tools/contracts";
import * as IdAllocator from "@t3tools/provider-core/server/IdAllocator";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as EffectOutbox from "../../orchestration-v2/EffectOutbox.ts";
import * as EffectWorker from "../../orchestration-v2/EffectWorker.ts";
import * as EventSink from "../../orchestration-v2/EventSink.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ProviderRuntimeRecovery from "../../orchestration-v2/ProviderRuntimeRecoveryService.ts";
import * as ServerSettings from "../../serverSettings.ts";
import { continuationEffectId } from "./HandoffService.ts";
import { handoffWriteIds } from "./handoffFacts.ts";

const threadId = ThreadId.make("handoff-restart-thread");
const writeRunId = RunId.make("write-2");
const instanceId = ProviderInstanceId.make("codex");
const driver = ProviderDriverKind.make("codex");
const providerThreadId = ProviderThreadId.make("provider-thread:handoff");
const sessionId = ProviderSessionId.make("session:handoff");
const attemptId = RunAttemptId.make("attempt:handoff");

/** A write turn running when the server stopped: its native thread saved, its turn live. */
const cutWriteTurn = (now: DateTime.Utc) =>
  ({
    thread: {
      id: threadId,
      projectId: ProjectId.make("handoff-project"),
      providerInstanceId: instanceId,
      archivedAt: null,
      deletedAt: null,
      lineage: { parentThreadId: null, relationshipToParent: null },
      forkedFrom: null,
    },
    runs: [
      {
        id: writeRunId,
        ordinal: 2,
        providerInstanceId: instanceId,
        modelSelection: { instanceId, model: "gpt-6" },
        providerThreadId,
        userMessageId: MessageId.make(
          handoffWriteIds(threadId, "run-1", { fallback: true }).messageId,
        ),
        activeAttemptId: attemptId,
        status: "running",
        requestedAt: now,
        completedAt: null,
      },
    ],
    providerThreads: [
      {
        id: providerThreadId,
        appThreadId: threadId,
        ownerNodeId: null,
        driver,
        providerInstanceId: instanceId,
        providerSessionId: sessionId,
        nativeThreadRef: { driver, nativeId: "native-thread", strength: "strong" },
        status: "active",
      },
    ],
    providerSessions: [
      { id: sessionId, driver, providerInstanceId: instanceId, status: "running" },
    ],
    providerTurns: [
      {
        id: ProviderTurnId.make("turn:handoff"),
        providerThreadId,
        runAttemptId: attemptId,
        status: "running",
      },
    ],
    runtimeRequests: [],
    attempts: [],
    nodes: [],
    subagents: [],
    messages: [],
    turnItems: [],
  }) as unknown as OrchestrationV2ThreadProjection;

describe("restart continuation row id (#973)", () => {
  it.effect("is the id upstream's restart writes for the write turn's continuation", () =>
    Effect.gen(function* () {
      const projection = cutWriteTurn(yield* DateTime.now);
      const written: Array<EffectOutbox.PendingOrchestrationEffectV2> = [];
      const recovery = yield* ProviderRuntimeRecovery.make.pipe(
        Effect.provide(
          Layer.mergeAll(
            ServerSettings.layerTest({ continueThreadsAfterServerUpdate: true }),
            Layer.mock(ProjectionStore.ProjectionStoreV2)({
              getRecoveryThreadIds: () => Effect.succeed([threadId]),
              getRuntimeRecoveryProjection: () => Effect.succeed(projection),
            }),
            Layer.mock(EventSink.EventSinkV2)({
              commitCommand: (input) => {
                written.push(...input.effects);
                return Effect.succeed({ committed: true, cancelledEffectCount: 0 } as never);
              },
            }),
            IdAllocator.layer,
            Layer.mock(EffectWorker.OrchestrationEffectWorkerV2)({}),
            Layer.mock(EffectOutbox.EffectOutboxV2)({
              get: () => Effect.succeedNone,
              listByCommandId: () => Effect.succeed([]),
              reconcileAfterProcessLoss: Effect.succeed({ requeued: 0, cancelled: 0 }),
            }),
          ),
        ),
      );
      yield* recovery.reconcile("startup");
      const continuations = written.filter(
        (effect) => effect.request.type === "provider-runtime.continue",
      );
      expect(continuations.map((effect) => effect.request)).toMatchObject([
        { type: "provider-runtime.continue", sourceRunId: writeRunId },
      ]);
      expect(continuations.map((effect) => effect.id)).toEqual([continuationEffectId(writeRunId)]);
    }),
  );
});
