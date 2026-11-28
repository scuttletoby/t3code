/**
 * Fork: one line per agent run that ends (issue 315), every thread and project, appended to
 * `<stateDir>/run-records/runs.jsonl` and never pruned. Follows stored `run.updated` events from a
 * cursor, so runs that end while the server is down are recorded at the next start; with no cursor
 * it starts at the latest event (older runs get no record). See docs/fork/run-records.md.
 */
import type { OrchestrationV2Run, OrchestrationV2StoredEvent } from "@t3tools/contracts";
import { writeFileStringAtomically } from "@t3tools/shared/atomicWrite";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as Schedule from "effect/Schedule";
import * as Schema from "effect/Schema";
import * as Stream from "effect/Stream";

import { ServerConfig } from "../../config.ts";
import * as Orchestrator from "../../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ProjectStore from "../../orchestration-v2/ProjectStore.ts";
import * as OrchestrationEventStore from "../../persistence/OrchestrationEventStore.ts";
import { forkParked } from "../../serverActivation.ts";
import { LaneStore } from "../lanes/LaneStore.ts";
import { buildRunRecord, type RunRecord, TERMINAL_RUN_STATUSES } from "./runRecord.ts";

const RUN_RECORDS_DIRECTORY = "run-records";
export const RUNS_FILE = "runs.jsonl";
export const CURSOR_FILE = "cursor.json";
/** Runs recorded last, kept in the cursor so a repeated end of the same run writes no second line. */
const RECENT_LIMIT = 1000;
/** Reads of one run's records that may fail (a busy database) before the run is skipped. */
export const READ_ATTEMPTS = 3;

/** A run's records could not be read: the follower stops and retries from its cursor. */
export class RunRecordReadError extends Schema.TaggedError<RunRecordReadError>()(
  "RunRecordReadError",
  { runId: Schema.String, attempt: Schema.Number, cause: Schema.Defect() },
) {
  override get message(): string {
    return `Could not read the records of run ${this.runId} (attempt ${this.attempt}).`;
  }
}

interface Cursor {
  readonly v: 1;
  /** Every stored event up to this one has been handled. */
  readonly sequence: number;
  readonly recent: ReadonlyArray<string>;
}

function decodeCursor(text: string): Cursor | null {
  try {
    const value = JSON.parse(text) as Partial<Cursor>;
    return value.v === 1 &&
      typeof value.sequence === "number" &&
      Number.isInteger(value.sequence) &&
      value.sequence >= 0 &&
      Array.isArray(value.recent) &&
      value.recent.every((id) => typeof id === "string")
      ? { v: 1, sequence: value.sequence, recent: value.recent }
      : null;
  } catch {
    return null;
  }
}

export const make = Effect.fn("RunRecorder.make")(function* (options: {
  readonly directory: string;
}) {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const orchestrator = yield* Orchestrator.OrchestratorV2;
  const projections = yield* ProjectionStore.ProjectionStoreV2;
  const projects = yield* ProjectStore.ProjectStoreV2;
  const events = yield* OrchestrationEventStore.OrchestrationEventStore;
  // Optional: without it (or with lanes off) records carry no lane, as before issue 1156.
  const lanes = Option.getOrNull(yield* Effect.serviceOption(LaneStore));
  const runsFile = path.join(options.directory, RUNS_FILE);
  const cursorFile = path.join(options.directory, CURSOR_FILE);
  const writeAtomically = (filePath: string, contents: string) =>
    writeFileStringAtomically({ filePath, contents }).pipe(
      Effect.provideService(FileSystem.FileSystem, fs),
      Effect.provideService(Path.Path, path),
    );

  let state: Cursor | null = null;
  // The first append of a process starts a new line: the file may end in a torn line.
  let cleanEnd = false;

  const saveCursor = (cursor: Cursor) =>
    writeAtomically(cursorFile, `${JSON.stringify(cursor)}\n`).pipe(
      Effect.tap(() => Effect.sync(() => (state = cursor))),
    );

  /** The saved cursor, or (none, or unreadable) a new one at the latest stored event. */
  const resume = Effect.fn("RunRecorder.resume")(function* () {
    if (state !== null) return state;
    const saved = yield* fs.readFileString(cursorFile).pipe(
      Effect.map(decodeCursor),
      Effect.orElseSucceed(() => null),
    );
    const latest = yield* events.latestAgentSequence();
    if (saved !== null && saved.sequence <= latest) {
      state = saved;
      return saved;
    }
    // Ahead of the database (it was restored or replaced beside old records): every new event
    // would be skipped until the sequence caught up, so start again at the latest event.
    if (saved !== null) {
      yield* Effect.logWarning(
        "run records cursor is ahead of the event store; starting at latest",
        {
          cursorSequence: saved.sequence,
          latestSequence: latest,
        },
      );
    }
    const cursor: Cursor = { v: 1, sequence: latest, recent: saved?.recent ?? [] };
    yield* fs.makeDirectory(options.directory, { recursive: true });
    yield* saveCursor(cursor);
    return cursor;
  });

  const readRecord = Effect.fn("RunRecorder.readRecord")(function* (run: OrchestrationV2Run) {
    const thread = yield* projections.getThread(run.threadId);
    const { attempts } = yield* projections.getThreadRecords(run.threadId, ["attempts"]);
    const attemptIds = attempts.filter((attempt) => attempt.runId === run.id).map((a) => a.id);
    const { providerTurns } = yield* projections.getThreadRecords(run.threadId, ["providerTurns"], {
      providerTurnAttemptIds: attemptIds,
    });
    const { messages: answers } = yield* projections.getThreadRecords(run.threadId, ["messages"], {
      messageRunIds: [run.id],
      messageRoles: ["assistant"],
    });
    const { messages: prompts } = yield* projections.getThreadRecords(run.threadId, ["messages"], {
      messageIds: [run.userMessageId],
    });
    const parentThreadId =
      thread.lineage.relationshipToParent === "subagent" ? thread.lineage.parentThreadId : null;
    // The parent may be gone; the run is still recorded, without its task's labels.
    const task =
      parentThreadId === null
        ? null
        : ((yield* projections
            .getThreadRecords(parentThreadId, ["subagents"], {
              subagentChildThreadIds: [run.threadId],
            })
            .pipe(
              Effect.map(({ subagents }) => subagents[0] ?? null),
              Effect.orElseSucceed(() => null),
            )) ?? null);
    const project = yield* projects
      .getShell(thread.projectId)
      .pipe(Effect.orElseSucceed(() => Option.none()));
    const lane =
      lanes === null
        ? null
        : yield* lanes.laneOfRun(thread.projectId, run.threadId, parentThreadId);
    const ms = (value: DateTime.Utc) => DateTime.toEpochMillis(value);
    const endedMs =
      run.completedAt === null ? DateTime.toEpochMillis(yield* DateTime.now) : ms(run.completedAt);
    return buildRunRecord({
      run,
      thread: {
        title: thread.title,
        projectId: thread.projectId,
        parentThreadId,
        delegatedFrom: thread.delegatedFrom ?? null,
      },
      projectTitle: Option.isSome(project) ? project.value.title : null,
      task,
      lane,
      runMessage: prompts[0]?.text ?? null,
      finalMessage: answers.some((message) => message.text.trim() !== ""),
      providerTurns,
      times: {
        requestedMs: ms(run.requestedAt),
        startedMs: run.startedAt === null ? endedMs : ms(run.startedAt),
        endedMs,
      },
    });
  });

  const append = Effect.fn("RunRecorder.append")(function* (record: RunRecord) {
    yield* fs.makeDirectory(options.directory, { recursive: true });
    const separator = cleanEnd ? "" : "\n";
    cleanEnd = false;
    // @effect-diagnostics-next-line globalJsonInEffect:off - a record is plain JSON data.
    yield* fs.writeFileString(runsFile, `${separator}${JSON.stringify(record)}\n`, { flag: "a" });
    cleanEnd = true;
  });

  /** Failed reads per run in this process; a run is skipped after `READ_ATTEMPTS`. */
  const readFailures = new Map<string, number>();

  /**
   * The run's record. A thread that is gone skips the run. Any other failed read (a busy or locked
   * database) fails, so the follower retries from its cursor, until the run has failed
   * `READ_ATTEMPTS` times; then it is skipped with a warning and the next run is recorded.
   */
  const readOrSkip = (run: OrchestrationV2Run) =>
    readRecord(run).pipe(
      Effect.tap(() => Effect.sync(() => readFailures.delete(run.id))),
      Effect.catchTags({
        ProjectionStoreThreadNotFoundError: () =>
          Effect.logWarning("run record skipped: its thread is gone", { runId: run.id }).pipe(
            Effect.as(null),
          ),
      }),
      Effect.catchCause((cause) => {
        const attempt = (readFailures.get(run.id) ?? 0) + 1;
        if (attempt < READ_ATTEMPTS) {
          readFailures.set(run.id, attempt);
          return Effect.fail(new RunRecordReadError({ runId: run.id, attempt, cause }));
        }
        readFailures.delete(run.id);
        return Effect.logWarning("run record skipped", { runId: run.id, attempt, cause }).pipe(
          Effect.as(null),
        );
      }),
    );

  /** One stored `run.updated` event: records the run if it just ended, then moves the cursor. */
  const handle = Effect.fn("RunRecorder.handle")(function* (stored: OrchestrationV2StoredEvent) {
    const cursor = yield* resume();
    if (stored.sequence <= cursor.sequence) return;
    const event = stored.event;
    if (event.type !== "run.updated") return;
    const run = event.payload;
    if (!TERMINAL_RUN_STATUSES.has(run.status) || cursor.recent.includes(run.id)) return;
    const record = yield* readOrSkip(run);
    const recent = [...cursor.recent, run.id].slice(-RECENT_LIMIT);
    // The line first, then the cursor: a crash between them leaves a duplicate line (readers keep
    // the first), never a lost record.
    if (record !== null) {
      if (record.estimateRefused != null) {
        yield* Effect.logWarning("run estimate tag refused: not a time or a token count", {
          runId: run.id,
          tag: record.estimateRefused,
        });
      }
      if (record.kindRefused != null) {
        yield* Effect.logWarning("run kind tag refused: empty or not closed", {
          runId: run.id,
          tag: record.kindRefused,
        });
      }
      yield* append(record);
      // Known in this process at once: if the cursor save fails (a full disk), a retry from the
      // old cursor writes no second line.
      state = { ...cursor, recent };
    }
    yield* saveCursor({ v: 1, sequence: stored.sequence, recent });
  });

  /** Follows stored `run.updated` events from the cursor until the stream ends (never, live). */
  const follow = Effect.gen(function* () {
    const cursor = yield* resume();
    yield* Stream.runForEach(
      orchestrator.streamStoredEventsFrom({
        afterSequence: cursor.sequence,
        eventType: "run.updated",
      }),
      handle,
    );
  });

  return { resume, handle, follow, runsFile, cursorFile };
});

/** Started with the server's other workers, from `ForkLayers.layerRuntimeServices` (fork/forkLayers.ts). */
export const layer = Layer.effectDiscard(
  Effect.gen(function* () {
    const config = yield* ServerConfig;
    const path = yield* Path.Path;
    const recorder = yield* make({ directory: path.join(config.stateDir, RUN_RECORDS_DIRECTORY) });
    // A failed read or write stops only this follower: it logs and starts again from its cursor.
    yield* forkParked(
      recorder.follow.pipe(
        Effect.onExit((exit) =>
          Exit.isFailure(exit)
            ? Effect.logWarning("run records follower stopped", { cause: exit.cause })
            : Effect.void,
        ),
        Effect.retry(
          Schedule.exponential("1 second").pipe(
            Schedule.modifyDelay(({ duration }) =>
              Effect.succeed(Duration.min(duration, Duration.minutes(1))),
            ),
          ),
        ),
        Effect.ignore,
      ),
    );
  }),
).pipe(Layer.provide(ProjectionStore.layer));
