/**
 * Fork: upstream's idle compaction and a delegated task's question to its idle parent start a run,
 * so each takes an agent slot. A refused compaction is skipped (it only pays before the prompt
 * cache expires). A refused question wake is kept in `HELD_WAKES_FILE` and sent once with a slot.
 */
import {
  type CommandId,
  OrchestrationV2Command,
  type OrchestrationV2ServerCommand,
  type ProviderInstanceId,
  type RuntimeRequestId,
  type ThreadId,
} from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as Schema from "effect/Schema";
import * as Semaphore from "effect/Semaphore";

import { ServerConfig } from "../config.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import { forkParked } from "../serverActivation.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import { admitSend, reasonHere } from "./agentSlotRoutes.ts";

type MessageDispatch = Extract<OrchestrationV2ServerCommand, { readonly type: "message.dispatch" }>;

/** In the server's state folder. */
export const HELD_WAKES_FILE = "fork-held-wakes.json";
/** As a wait's wake-up. */
const HELD_WAKE_RETRY = Duration.seconds(5);

const QUESTION_COMMAND = "server:delegated-task-question:";
const QUESTION_MESSAGE = "message:delegated-task-question:";
const SETTLED_TASK = new Set(["completed", "failed", "cancelled", "interrupted"]);
const NOTHING_STORED = { sequence: 0, storedEvents: [] };

const isIdleCompaction = (command: OrchestrationV2ServerCommand): command is MessageDispatch =>
  command.type === "message.dispatch" && command.idleCompaction !== undefined;

const isQuestionWake = (command: OrchestrationV2ServerCommand): command is MessageDispatch =>
  command.type === "message.dispatch" && command.commandId.startsWith(QUESTION_COMMAND);

/** The question a wake is about, from upstream's ids (`DelegatedTaskQuestionReactor.ts`). */
const questionOf = (command: MessageDispatch) => {
  const source = command.notification?.source;
  const prefix = `${QUESTION_MESSAGE}${command.threadId}:`;
  if (source?.kind !== "delegated_task" || source.childThreadId === undefined) return null;
  if (!command.messageId.startsWith(prefix)) return null;
  return {
    childThreadId: source.childThreadId,
    requestId: command.messageId.slice(prefix.length) as RuntimeRequestId,
    taskIds: source.taskIds,
  };
};

const HeldWakesFile = Schema.fromJsonString(
  Schema.Struct({ version: Schema.Literal(1), wakes: Schema.Array(Schema.Unknown) }),
);
const decodeFile = Schema.decodeUnknownOption(HeldWakesFile);
const encodeFile = Schema.encodeEffect(HeldWakesFile);
const decodeCommand = Schema.decodeUnknownOption(OrchestrationV2Command);
const encodeCommand = Schema.encodeSync(OrchestrationV2Command);

/** The parent as the gate needs it: `null` when gone (deleted or archived) or unknown. */
export type WakeThread = { readonly instanceId: ProviderInstanceId } | null;

/** Where the held wakes are kept; `null` keeps them in memory only (tests). */
export interface HeldWakesStore {
  readonly load: Effect.Effect<ReadonlyArray<MessageDispatch>>;
  readonly save: (wakes: ReadonlyArray<MessageDispatch>) => Effect.Effect<void>;
}

export interface WorkerWakeInput<E, F> {
  readonly orchestrator: Orchestrator.OrchestratorV2["Service"];
  readonly gate: Option.Option<SlotGate.SlotGate["Service"]>;
  readonly threadOf: (threadId: ThreadId) => Effect.Effect<WakeThread, E>;
  /** Whether the question is still open: pending, and its task not settled. */
  readonly questionOpen: (
    parentThreadId: ThreadId,
    question: NonNullable<ReturnType<typeof questionOf>>,
  ) => Effect.Effect<boolean, F>;
  readonly store: HeldWakesStore;
}

/**
 * Upstream's orchestrator with the two gated, and `retry`, which sends the held wakes that have a
 * slot now (the layer runs it every `HELD_WAKE_RETRY`). With no gate it is upstream's own.
 */
export const makeWorkerWakeSlots = Effect.fn("WorkerWakeSlots.make")(function* <E, F>(
  input: WorkerWakeInput<E, F>,
) {
  const { orchestrator, gate } = input;
  if (Option.isNone(gate))
    return { orchestrator, retry: Effect.void, held: (): ReadonlyArray<MessageDispatch> => [] };
  const held = new Map<CommandId, MessageDispatch>();
  for (const wake of yield* input.store.load) held.set(wake.commandId, wake);
  if (held.size > 0)
    yield* Effect.logInfo("held delegated task questions loaded: sent when a slot frees", {
      count: held.size,
    });
  const saving = yield* Semaphore.make(1);
  // One save at a time, its snapshot taken under the lock: the last write is what is held now.
  const save = saving.withPermits(1)(Effect.suspend(() => input.store.save([...held.values()])));
  /** Held wakes whose parent or question could not be read: logged once. */
  const unreadLogged = new Set<CommandId>();
  const unread = Effect.fn("WorkerWakeSlots.unread")(function* (command: MessageDispatch) {
    if (unreadLogged.has(command.commandId)) return false;
    unreadLogged.add(command.commandId);
    yield* Effect.logWarning(
      "held delegated task question waits: its parent or question could not be read",
      { threadId: command.threadId },
    );
    return false;
  });

  /** `null`: it may start now; else why it waits. */
  const slotFor = Effect.fn("WorkerWakeSlots.slotFor")(function* (command: MessageDispatch) {
    const thread = yield* input
      .threadOf(command.threadId)
      .pipe(Effect.orElseSucceed(() => "unreadable" as const));
    if (thread === "unreadable") return "the thread could not be read to check for a slot";
    if (thread === null) return null;
    const slot = yield* admitSend(gate, {
      callerThreadId: command.threadId,
      threadId: command.threadId,
      instanceId: thread.instanceId,
      keepsHeld: true,
    });
    return slot.type === "refuse" ? reasonHere(slot.refusal) : null;
  });

  const compaction = Effect.fn("WorkerWakeSlots.compaction")(function* (command: MessageDispatch) {
    const detail = yield* slotFor(command);
    if (detail === null) return yield* orchestrator.dispatch(command);
    yield* Effect.logInfo("idle compaction skipped: it waits for no agent slot", {
      threadId: command.threadId,
      detail,
    });
    return NOTHING_STORED;
  });

  const question = Effect.fn("WorkerWakeSlots.question")(function* (command: MessageDispatch) {
    // Held already: the retry sends it, once.
    if (held.has(command.commandId)) return NOTHING_STORED;
    const detail = yield* slotFor(command);
    if (detail === null) return yield* orchestrator.dispatch(command);
    held.set(command.commandId, command);
    yield* save;
    yield* Effect.logInfo("delegated task question waits for an agent slot", {
      threadId: command.threadId,
      detail,
    });
    return NOTHING_STORED;
  });

  /** One held wake: `true` when it leaves the file (sent, or no longer owed). */
  const retryOne = Effect.fn("WorkerWakeSlots.retryOne")(function* (command: MessageDispatch) {
    const thread = yield* input.threadOf(command.threadId).pipe(Effect.option);
    if (Option.isNone(thread)) return yield* unread(command);
    if (thread.value === null) return true;
    const asked = questionOf(command);
    if (asked !== null) {
      const open = yield* input.questionOpen(command.threadId, asked).pipe(Effect.option);
      if (Option.isNone(open)) return yield* unread(command);
      if (!open.value) return true;
    }
    if ((yield* slotFor(command)) !== null) return false;
    yield* orchestrator.dispatch(command).pipe(
      Effect.catchCause((cause) =>
        Effect.logWarning("waking the parent of a delegated task question failed", {
          threadId: command.threadId,
          cause,
        }),
      ),
    );
    return true;
  });

  const retry = Effect.gen(function* () {
    let changed = false;
    for (const command of held.values()) {
      if (!(yield* retryOne(command))) continue;
      held.delete(command.commandId);
      unreadLogged.delete(command.commandId);
      changed = true;
    }
    if (changed) yield* save;
  }).pipe(Effect.withSpan("WorkerWakeSlots.retry"));

  return {
    orchestrator: {
      ...orchestrator,
      dispatch: (command: OrchestrationV2ServerCommand) =>
        isIdleCompaction(command)
          ? compaction(command)
          : isQuestionWake(command)
            ? question(command)
            : orchestrator.dispatch(command),
    } satisfies Orchestrator.OrchestratorV2["Service"],
    retry,
    held: (): ReadonlyArray<MessageDispatch> => [...held.values()],
  };
});

/**
 * The held wakes in `filePath`. Missing is none. A file or entry that cannot be read is logged and
 * left out: the next save writes what is held then. A file that cannot be read is first moved to
 * `<file>.corrupt-<ms>`, so nothing is written over it.
 */
const fileStore = Effect.fn("WorkerWakeSlots.fileStore")(function* (filePath: string) {
  const fs = yield* FileSystem.FileSystem;
  const load = Effect.gen(function* () {
    if (!(yield* fs.exists(filePath).pipe(Effect.orElseSucceed(() => false)))) return [];
    const text = yield* fs.readFileString(filePath).pipe(Effect.option);
    const file = Option.flatMap(text, decodeFile);
    if (Option.isNone(file)) {
      const keptAs = `${filePath}.corrupt-${yield* Clock.currentTimeMillis}`;
      const kept = yield* fs.rename(filePath, keptAs).pipe(
        Effect.as(keptAs),
        Effect.orElseSucceed(() => null),
      );
      yield* Effect.logWarning("held wakes file unreadable; its wakes are not sent", {
        filePath,
        keptAs: kept,
      });
      return [];
    }
    const wakes = file.value.wakes.flatMap((entry) => {
      const command = decodeCommand(entry);
      return Option.isSome(command) && command.value.type === "message.dispatch"
        ? [command.value]
        : [];
    });
    if (wakes.length < file.value.wakes.length)
      yield* Effect.logWarning("held wakes left out: unreadable", {
        filePath,
        count: file.value.wakes.length - wakes.length,
      });
    return wakes;
  });
  // Called one at a time (`makeWorkerWakeSlots` holds a lock), so one temp name is enough.
  const save = Effect.fn("WorkerWakeSlots.save")(
    function* (wakes: ReadonlyArray<MessageDispatch>) {
      const temporary = `${filePath}.tmp`;
      const text = yield* encodeFile({
        version: 1,
        wakes: wakes.map((wake) => encodeCommand(wake)),
      });
      yield* fs.writeFileString(temporary, text);
      yield* fs.rename(temporary, filePath);
    },
    // Not written: they are still held in memory and written with the next change.
    Effect.catchCause((cause) => Effect.logWarning("held wakes not written", { filePath, cause })),
  );
  return { load, save } satisfies HeldWakesStore;
});

/** Upstream's orchestrator for the runtime's workers, gated as the file's doc says. */
export const layerOver = Layer.effect(
  Orchestrator.OrchestratorV2,
  Effect.gen(function* () {
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    const projections = yield* ProjectionStore.ProjectionStoreV2;
    const config = yield* ServerConfig;
    const path = yield* Path.Path;
    const slots = yield* makeWorkerWakeSlots({
      orchestrator,
      gate: yield* Effect.serviceOption(SlotGate.SlotGate),
      threadOf: (threadId) =>
        orchestrator
          .getThreadShell(threadId)
          .pipe(
            Effect.map((shell) =>
              shell === null || shell.deletedAt !== null || shell.archivedAt !== null
                ? null
                : { instanceId: shell.modelSelection.instanceId },
            ),
          ),
      questionOpen: Effect.fn("WorkerWakeSlots.questionOpen")(function* (parentThreadId, asked) {
        const request = yield* projections.getRuntimeRequest(asked.childThreadId, asked.requestId);
        if (request?.status !== "pending") return false;
        const { subagents } = yield* projections.getThreadRecords(parentThreadId, ["subagents"]);
        return !subagents.some(
          (task) => asked.taskIds.includes(task.id) && SETTLED_TASK.has(task.status),
        );
      }),
      store: yield* fileStore(path.join(config.stateDir, HELD_WAKES_FILE)),
    });
    // A stop waits for a send's file write: a send it cut off would be sent again (receipts dedupe).
    yield* forkParked(
      Effect.sleep(HELD_WAKE_RETRY).pipe(
        Effect.andThen(Effect.uninterruptible(slots.retry)),
        Effect.forever,
      ),
    );
    return slots.orchestrator;
  }),
);

/** {@link layerOver} over the server's one gate (layers memoize by reference). */
export const layer = layerOver.pipe(
  Layer.provide(Layer.merge(SlotGate.layer, ProjectionStore.layer)),
);
