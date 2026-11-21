/**
 * Fork: upstream's idle compaction and a delegated task's question to its idle parent start a run,
 * so each takes an agent slot, with a place in the gate's line. A refused compaction asks again
 * while the thread stays idle and its prompt cache window is open, then is skipped. A refused
 * question wake is kept in `HELD_WAKES_FILE` and sent once with a slot.
 */
import {
  type CommandId,
  OrchestrationV2Command,
  type OrchestrationV2ServerCommand,
  type ProviderInstanceId,
  resolveIdleCompaction,
  resolveProjectProviderInstanceEnabled,
  type RunId,
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
import type * as PlatformError from "effect/PlatformError";
import * as Schema from "effect/Schema";
import * as Semaphore from "effect/Semaphore";

import { ServerConfig } from "../config.ts";
import {
  idleCompactionWindow,
  type IdleCompactionRecords,
} from "../orchestration-v2/IdleCompaction.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import { forkParked } from "../serverActivation.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import { admitSend, leaveLine, reasonHere } from "./agentSlotRoutes.ts";

type MessageDispatch = Extract<OrchestrationV2ServerCommand, { readonly type: "message.dispatch" }>;

/** In the server's state folder. */
export const HELD_WAKES_FILE = "fork-held-wakes.json";
/** As a wait's wake-up. */
const HELD_WAKE_RETRY = Duration.seconds(5);
/** A refused idle compaction asks again after this, while its window is open. */
const COMPACTION_SLOT_RETRY = Duration.seconds(5);
const COMPACTION_COMMAND = "server:idle-compact:";

const QUESTION_COMMAND = "server:delegated-task-question:";
const QUESTION_MESSAGE = "message:delegated-task-question:";
const SETTLED_TASK = new Set(["completed", "failed", "cancelled", "interrupted"]);
const NOTHING_STORED = { sequence: 0, storedEvents: [] };

const isIdleCompaction = (command: OrchestrationV2ServerCommand): command is MessageDispatch =>
  command.type === "message.dispatch" && command.idleCompaction !== undefined;

const isQuestionWake = (command: OrchestrationV2ServerCommand): command is MessageDispatch =>
  command.type === "message.dispatch" && command.commandId.startsWith(QUESTION_COMMAND);

/** The completed run an idle compaction is for, from upstream's command id. */
const compactionRunOf = (command: MessageDispatch): RunId | null => {
  const prefix = `${COMPACTION_COMMAND}${command.threadId}:`;
  return command.commandId.startsWith(prefix)
    ? (command.commandId.slice(prefix.length) as RunId)
    : null;
};

/**
 * A refused idle compaction is still due: its thread is idle at its run, and the prompt cache
 * window is open, by the rule upstream's worker sent it by (`idleCompactionWindow`).
 */
export const compactionWindowOpen = (
  records: IdleCompactionRecords,
  command: MessageDispatch,
  nowMs: number,
): boolean => {
  const runId = compactionRunOf(command);
  const window = idleCompactionWindow(records, nowMs);
  return runId !== null && window?.runId === runId && nowMs < window.endMs;
};

/** Places in the gate's line: one per held question wake and per waiting compaction. */
const questionPlace = (command: MessageDispatch): SlotGate.SlotPlace => ({
  key: `question-wake:${command.commandId}`,
});
const compactionPlace = (command: MessageDispatch): SlotGate.SlotPlace => ({
  key: `idle-compaction:${command.commandId}`,
});

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

/** Where the held wakes are kept. */
export interface HeldWakesStore {
  /**
   * What is held; none when there is no file. Fails when the file cannot be checked or read (an
   * I/O error, never "missing"): its wakes may be there, so nothing is written over it.
   */
  readonly load: Effect.Effect<ReadonlyArray<MessageDispatch>, PlatformError.PlatformError>;
  readonly save: (wakes: ReadonlyArray<MessageDispatch>) => Effect.Effect<void>;
}

export interface WorkerWakeInput<E, F, G> {
  readonly orchestrator: Orchestrator.OrchestratorV2["Service"];
  readonly gate: Option.Option<SlotGate.SlotGate["Service"]>;
  readonly threadOf: (threadId: ThreadId) => Effect.Effect<WakeThread, E>;
  /** Whether the question is still open: pending, and its task not settled. */
  readonly questionOpen: (
    parentThreadId: ThreadId,
    question: NonNullable<ReturnType<typeof questionOf>>,
  ) => Effect.Effect<boolean, F>;
  /**
   * Whether a refused idle compaction is still due: its thread idle at its run, and the prompt
   * cache window open (`idleCompactionWindow`, as upstream's worker reads it).
   */
  readonly compactionDue: (command: MessageDispatch) => Effect.Effect<boolean, G>;
  readonly store: HeldWakesStore;
}

/**
 * Upstream's orchestrator with the two gated, and `retry`, which sends the held wakes that have a
 * slot now (the layer runs it every `HELD_WAKE_RETRY`). With no gate it is upstream's own.
 */
export const makeWorkerWakeSlots = Effect.fn("WorkerWakeSlots.make")(function* <E, F, G>(
  input: WorkerWakeInput<E, F, G>,
) {
  const { orchestrator, gate } = input;
  if (Option.isNone(gate))
    return { orchestrator, retry: Effect.void, held: (): ReadonlyArray<MessageDispatch> => [] };
  const held = new Map<CommandId, MessageDispatch>();
  /**
   * The file was read (or there is none). Until then nothing is written over it: a file that
   * cannot be checked or read may hold wakes. Each retry reads it again.
   */
  let loaded = false;
  let loadLogged = false;
  const saving = yield* Semaphore.make(1);
  // One save at a time, its snapshot taken under the lock: the last write is what is held now.
  const save = saving.withPermits(1)(
    Effect.suspend(() => (loaded ? input.store.save([...held.values()]) : Effect.void)),
  );
  const load = Effect.gen(function* () {
    const fromFile = yield* input.store.load.pipe(
      Effect.tapCause((cause) =>
        loadLogged
          ? Effect.void
          : Effect.logWarning(
              "held wakes file could not be read: nothing is written over it, read again in 5 s",
              { cause },
            ),
      ),
      Effect.option,
    );
    if (Option.isNone(fromFile)) {
      loadLogged = true;
      return;
    }
    // The file's wakes first, in its order (their place in the gate's line), then any held since.
    const since = [...held.values()];
    held.clear();
    for (const wake of [...fromFile.value, ...since]) {
      if (!held.has(wake.commandId)) held.set(wake.commandId, wake);
    }
    loaded = true;
    if (fromFile.value.length > 0)
      yield* Effect.logInfo("held delegated task questions loaded: sent when a slot frees", {
        count: fromFile.value.length,
      });
    if (since.length > 0) yield* save;
  });
  yield* load;
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

  /** `null`: it may start now; else why it waits (in its place in the gate's line). */
  const slotFor = Effect.fn("WorkerWakeSlots.slotFor")(function* (
    command: MessageDispatch,
    place: SlotGate.SlotPlace,
  ) {
    const thread = yield* input
      .threadOf(command.threadId)
      .pipe(Effect.orElseSucceed(() => "unreadable" as const));
    if (thread === "unreadable") return "the thread could not be read to check for a slot";
    if (thread === null) {
      yield* leaveLine(gate, [place.key]);
      return null;
    }
    const slot = yield* admitSend(gate, {
      callerThreadId: command.threadId,
      threadId: command.threadId,
      instanceId: thread.instanceId,
      keepsHeld: true,
      place,
    });
    return slot.type === "refuse" ? reasonHere(slot.refusal) : null;
  });

  /**
   * Held in upstream's own timer (a newer completed run replaces it, and a stop ends it): asked
   * again every `COMPACTION_SLOT_RETRY` while it is still due, else skipped. Late is worse than
   * never: after the window a compaction pays a full uncached read.
   */
  const compaction = Effect.fn("WorkerWakeSlots.compaction")(
    function* (command: MessageDispatch) {
      const place = compactionPlace(command);
      let detail = yield* slotFor(command, place);
      if (detail === null) return yield* orchestrator.dispatch(command);
      yield* Effect.logInfo("idle compaction waits for an agent slot", {
        threadId: command.threadId,
        detail,
      });
      while (detail !== null) {
        yield* Effect.sleep(COMPACTION_SLOT_RETRY);
        // Checked again in the fork before each ask: still idle at its run, window still open.
        const due = yield* input.compactionDue(command).pipe(Effect.orElseSucceed(() => false));
        if (!due) {
          yield* Effect.logInfo(
            "idle compaction skipped: no agent slot while its cache window was open",
            { threadId: command.threadId },
          );
          return NOTHING_STORED;
        }
        detail = yield* slotFor(command, place);
      }
      return yield* orchestrator.dispatch(command);
    },
    (effect, command) => Effect.ensuring(effect, leaveLine(gate, [compactionPlace(command).key])),
  );

  const question = Effect.fn("WorkerWakeSlots.question")(function* (command: MessageDispatch) {
    // Held already: the retry sends it, once.
    if (held.has(command.commandId)) return NOTHING_STORED;
    const detail = yield* slotFor(command, questionPlace(command));
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
    const place = questionPlace(command);
    // No longer owed: it leaves the line too, so it holds no slot from the waiters behind it.
    const owedNoMore = leaveLine(gate, [place.key]).pipe(Effect.as(true));
    const thread = yield* input.threadOf(command.threadId).pipe(Effect.option);
    if (Option.isNone(thread)) return yield* unread(command);
    if (thread.value === null) return yield* owedNoMore;
    const asked = questionOf(command);
    if (asked !== null) {
      const open = yield* input.questionOpen(command.threadId, asked).pipe(Effect.option);
      if (Option.isNone(open)) return yield* unread(command);
      if (!open.value) return yield* owedNoMore;
    }
    if ((yield* slotFor(command, place)) !== null) return false;
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
    if (!loaded) yield* load;
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
 * The held wakes in `filePath`: missing is none, a failed check or read is an error. A file that
 * does not decode is moved to `<file>.corrupt-<ms>`; a bad entry is logged and left out. A save is
 * synced before its rename and the folder after, so a power loss leaves one whole file.
 */
export const fileStore = Effect.fn("WorkerWakeSlots.fileStore")(function* (filePath: string) {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const load = Effect.gen(function* () {
    if (!(yield* fs.exists(filePath))) return [];
    const file = decodeFile(yield* fs.readFileString(filePath));
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
      yield* Effect.scoped(
        Effect.gen(function* () {
          const file = yield* fs.open(temporary, { flag: "w" });
          yield* file.writeAll(new TextEncoder().encode(text));
          yield* file.sync;
        }),
      );
      yield* fs.rename(temporary, filePath);
      // The rename itself, on disk. A folder cannot be opened on every system (Windows): skipped.
      yield* Effect.scoped(
        fs
          .open(path.dirname(filePath), { flag: "r" })
          .pipe(Effect.flatMap((folder) => folder.sync)),
      ).pipe(Effect.ignore);
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
    const settings = yield* ServerSettings.ServerSettingsService;
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
      compactionDue: Effect.fn("WorkerWakeSlots.compactionDue")(function* (command) {
        const runId = compactionRunOf(command);
        const current = yield* settings.getSettings;
        const thread = yield* projections.getThread(command.threadId);
        if (
          runId === null ||
          resolveIdleCompaction(current, thread.providerInstanceId) !== "beforeCacheExpires" ||
          !resolveProjectProviderInstanceEnabled(
            current,
            thread.projectId,
            thread.providerInstanceId,
          )
        )
          return false;
        const active =
          thread.activeProviderThreadId === null ? [] : [thread.activeProviderThreadId];
        // The records upstream's idle compaction worker reads for its window.
        const records = yield* projections.getThreadRecords(
          command.threadId,
          [
            "runs",
            "messages",
            "providerThreads",
            "providerTurns",
            "runtimeRequests",
            "turnItems",
            "contextTransfers",
          ],
          {
            messageRunIds: [runId],
            turnItemStatuses: ["pending", "running", "waiting"],
            runStatuses: ["preparing", "starting", "running", "rolled_back"],
            includeLatestRun: true,
            providerThreadIds: active,
            providerTurnThreadIds: active,
            latestProviderTurnUsageOnly: true,
            runtimeRequestStatuses: ["pending"],
            contextTransferStatuses: ["pending"],
          },
        );
        return compactionWindowOpen(records, command, yield* Clock.currentTimeMillis);
      }),
      store: yield* fileStore(path.join(config.stateDir, HELD_WAKES_FILE)),
    });
    // A stop waits for a send's file write: a send it cut off would be sent again (receipts dedupe).
    // The first pass runs at once, so wakes from the file take their places in line first.
    yield* forkParked(
      Effect.uninterruptible(slots.retry).pipe(
        Effect.andThen(Effect.sleep(HELD_WAKE_RETRY)),
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
