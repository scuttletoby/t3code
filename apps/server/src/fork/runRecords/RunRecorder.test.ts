/**
 * Fork: the run recorder (issue 315) against stored `run.updated` events and projection records,
 * with real files in a temporary state folder. Each `make` is one server start.
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import {
  EventId,
  type OrchestrationV2AppThread,
  type OrchestrationV2ConversationMessage,
  type OrchestrationV2ProviderTurn,
  type OrchestrationV2Run,
  type OrchestrationV2RunAttempt,
  type OrchestrationV2StoredEvent,
  type OrchestrationV2Subagent,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as Path from "effect/Path";
import * as Stream from "effect/Stream";

import * as Orchestrator from "../../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import { ProjectionStoreThreadNotFoundError } from "../../orchestration-v2/ProjectionStore.ts";
import * as ProjectStore from "../../orchestration-v2/ProjectStore.ts";
import * as OrchestrationEventStore from "../../persistence/OrchestrationEventStore.ts";
import { LaneStore } from "../lanes/LaneStore.ts";
import { parseRunRecordLines, runRecordLane } from "./runRecord.ts";
import * as RunRecorder from "./RunRecorder.ts";
import { summarizeRunRecords } from "./runRecordsSummary.ts";
import {
  at,
  attempt,
  childThread,
  iso,
  message,
  NOW_MS,
  providerTurn,
  run,
  task,
  thread,
  usage,
} from "./runRecords.testkit.ts";

interface World {
  events: Array<OrchestrationV2StoredEvent>;
  latestSequence: number;
  threads: Map<string, OrchestrationV2AppThread>;
  attempts: Array<OrchestrationV2RunAttempt>;
  providerTurns: Array<OrchestrationV2ProviderTurn>;
  messages: Array<OrchestrationV2ConversationMessage>;
  subagents: Array<OrchestrationV2Subagent>;
  /** Thread reads that fail (a busy database), by thread: how many more times. */
  failReads: Map<string, number>;
  /** Threads that are gone from the projection. */
  missing: Set<string>;
}

const newWorld = (): World => ({
  events: [],
  latestSequence: 0,
  threads: new Map([
    ["parent", thread("parent", { title: "Orchestrator" })],
    ["child", childThread("child", "parent", "Fix the cursor")],
  ]),
  attempts: [],
  providerTurns: [],
  messages: [],
  subagents: [
    task(
      "parent",
      "child",
      "Act as the writer sub-agent for this task.\n\n[run-kind: fix] [run-estimate: 20m]\nFix it.",
      "Fix the cursor",
    ),
  ],
  failReads: new Map(),
  missing: new Set(),
});

/** A run that ended: its attempt, usage, answer and stored `run.updated` event. */
const endRun = (
  world: World,
  theRun: OrchestrationV2Run,
  options: {
    readonly answer?: string;
    readonly turns?: ReadonlyArray<OrchestrationV2ProviderTurn>;
  } = {},
) => {
  world.attempts.push(attempt(theRun.id));
  world.providerTurns.push(...(options.turns ?? []));
  world.messages.push(message(theRun.threadId, theRun.id, "user", "Fix it."));
  if (options.answer !== undefined) {
    world.messages.push(message(theRun.threadId, theRun.id, "assistant", options.answer));
  }
  storeUpdate(world, theRun);
};

const storeUpdate = (world: World, theRun: OrchestrationV2Run) => {
  world.latestSequence += 1;
  world.events.push({
    sequence: world.latestSequence,
    commandId: null,
    event: {
      id: EventId.make(`event:${world.latestSequence}`),
      threadId: theRun.threadId,
      runId: theRun.id,
      occurredAt: at(),
      type: "run.updated",
      payload: theRun,
    },
  } as OrchestrationV2StoredEvent);
};

const dependencies = (world: World) =>
  Layer.mergeAll(
    Layer.mock(Orchestrator.OrchestratorV2)({
      streamStoredEventsFrom: (input) =>
        Stream.fromIterable(
          world.events.filter(
            (stored) =>
              stored.sequence > (input?.afterSequence ?? 0) &&
              (input?.eventType === undefined || stored.event.type === input.eventType),
          ),
        ),
    }),
    Layer.mock(ProjectionStore.ProjectionStoreV2)({
      getThread: (threadId) =>
        Effect.suspend(() => {
          if (world.missing.has(threadId)) {
            return Effect.fail(new ProjectionStoreThreadNotFoundError({ threadId }));
          }
          const failures = world.failReads.get(threadId) ?? 0;
          if (failures <= 0) return Effect.succeed(world.threads.get(threadId)!);
          world.failReads.set(threadId, failures - 1);
          return Effect.die(new Error("database is locked"));
        }),
      getThreadRecords: (threadId, _fields, filter) =>
        Effect.sync(
          () =>
            ({
              thread: world.threads.get(threadId),
              attempts: world.attempts,
              providerTurns: world.providerTurns.filter(
                (turn) =>
                  filter?.providerTurnAttemptIds === undefined ||
                  filter.providerTurnAttemptIds.includes(turn.runAttemptId!),
              ),
              messages: world.messages.filter(
                (entry) =>
                  entry.threadId === threadId &&
                  (filter?.messageRunIds === undefined ||
                    filter.messageRunIds.includes(entry.runId!)) &&
                  (filter?.messageRoles === undefined ||
                    filter.messageRoles.includes(entry.role)) &&
                  (filter?.messageIds === undefined || filter.messageIds.includes(entry.id)),
              ),
              subagents: world.subagents.filter(
                (entry) =>
                  entry.threadId === threadId &&
                  (filter?.subagentChildThreadIds === undefined ||
                    filter.subagentChildThreadIds.includes(entry.childThreadId!)),
              ),
            }) as never,
        ),
    }),
    Layer.mock(ProjectStore.ProjectStoreV2)({
      getShell: () => Effect.succeedSome({ title: "t3" } as never),
    }),
    Layer.mock(OrchestrationEventStore.OrchestrationEventStore)({
      latestAgentSequence: () => Effect.sync(() => world.latestSequence),
    }),
  );

const harness = Effect.gen(function* () {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const directory = path.join(yield* fs.makeTempDirectoryScoped({ prefix: "run-records-" }), "rr");
  const world = newWorld();
  /** One server process's recorder over the folder; its `follow` again is the follower's retry. */
  const make = RunRecorder.make({ directory }).pipe(Effect.provide(dependencies(world)));
  /** One server start: a new recorder over the same folder, run until the stored events end. */
  const start = Effect.gen(function* () {
    const recorder = yield* make;
    yield* recorder.follow;
    return recorder;
  });
  const runsFile = path.join(directory, RunRecorder.RUNS_FILE);
  const cursorFile = path.join(directory, RunRecorder.CURSOR_FILE);
  const readText = (file: string) => fs.readFileString(file).pipe(Effect.orElseSucceed(() => ""));
  const records = readText(runsFile).pipe(Effect.map(parseRunRecordLines));
  const lines = readText(runsFile).pipe(
    Effect.map((text) => text.split("\n").filter((line) => line.trim() !== "")),
  );
  const cursor = readText(cursorFile).pipe(
    Effect.map((text) => JSON.parse(text) as { sequence: number; recent: Array<string> }),
  );
  /** Records begin when the folder is first seen: an empty start before any test's runs. */
  yield* start;
  return { fs, path, world, make, start, records, lines, cursor, runsFile, cursorFile };
});

it.layer(NodeServices.layer)("RunRecorder", (it) => {
  it.effect("records a normal delegated run with its labels, usage and times", () =>
    Effect.gen(function* () {
      const { world, start, records, cursor } = yield* harness;
      endRun(world, run("child", "run-1"), {
        answer: "Done.",
        turns: [
          providerTurn("run-1", usage(1000, 100, { cachedInputTokens: 700 })),
          providerTurn("run-1", usage(500, 50)),
        ],
      });
      // Another run's turns in the thread do not count toward this one.
      world.providerTurns.push(providerTurn("run-0", usage(9999, 9999)));
      yield* start;
      const [record, ...rest] = yield* records;
      assert.strictEqual(rest.length, 0);
      assert.deepInclude(record, {
        runId: "run-1",
        threadId: "child",
        projectTitle: "t3",
        role: "writer",
        kind: "fix",
        estimate: { wallMs: 20 * 60_000 },
        model: "claude-opus-5-5",
        effort: "high",
        status: "completed",
        endStatus: "completed",
        wallMs: 10 * 60_000,
        endedAt: iso(NOW_MS),
      });
      assert.deepStrictEqual(record?.parent, {
        threadId: "parent",
        taskId: "task:child",
        environmentId: null,
      });
      assert.strictEqual(record?.usage.inputTokens, 1500);
      assert.strictEqual(record?.usage.cachedInputTokens, 700);
      assert.strictEqual(record?.usage.outputTokens, 150);
      assert.strictEqual(record?.usage.providerTurns, 2);
      assert.deepStrictEqual(yield* cursor, { v: 1, sequence: 1, recent: ["run-1"] } as never);
    }),
  );

  it.effect("records a run stopped half-way as cut off with the usage seen so far", () =>
    Effect.gen(function* () {
      const { world, start, records } = yield* harness;
      // Stopped mid-work: no final message, the last turn reported only part of its usage.
      endRun(world, run("child", "stopped", { status: "interrupted" }), {
        turns: [
          providerTurn("stopped", usage(400, 40)),
          providerTurn("stopped", usage(100, 0, { usageStatus: "partial" })),
        ],
      });
      // Completed but said nothing: also cut off.
      endRun(world, run("child", "silent"), { answer: "  " });
      // Never started (cancelled while queued): no work, no record.
      endRun(world, run("child", "queued", { status: "cancelled", startedAt: null }));
      // Still going: not ended, no record yet.
      storeUpdate(world, run("child", "going", { status: "running", completedAt: null }));
      yield* start;
      const recorded = yield* records;
      assert.deepStrictEqual(
        recorded.map((record) => [
          record.runId,
          record.status,
          record.endStatus,
          record.usage.status,
        ]),
        [
          ["stopped", "interrupted", "cut_off", "partial"],
          ["silent", "completed", "cut_off", "unavailable"],
        ],
      );
      assert.strictEqual(recorded[0]!.usage.inputTokens, 500);
      assert.strictEqual(recorded[0]!.finalMessage, false);
    }),
  );

  it.effect("a re-run is a new record; the same run ending again, or a replay, adds none", () =>
    Effect.gen(function* () {
      const { world, start, records, lines } = yield* harness;
      endRun(world, run("child", "first", { status: "interrupted" }));
      yield* start;
      // The run reports its end again (e.g. `rolled_back` follows, or a repeat update).
      storeUpdate(world, run("child", "first", { status: "interrupted" }));
      // The restart's continuation is a new run, and a new record.
      endRun(
        world,
        run("child", "rerun", { restartContinuationOfRunId: "first" as never, ordinal: 2 }),
        {
          answer: "Done now.",
        },
      );
      // The server was down while this one ended: it is recorded at the next start.
      endRun(world, run("child", "while-down"), { answer: "Yes." });
      yield* start;
      yield* start;
      assert.deepStrictEqual(
        (yield* records).map((record) => [record.runId, record.endStatus, record.continuationOf]),
        [
          ["first", "cut_off", null],
          ["rerun", "completed", "first"],
          ["while-down", "completed", null],
        ],
      );
      assert.strictEqual((yield* lines).length, 3);
    }),
  );

  it.effect(
    "a crash after the line but before the cursor writes a second line that readers skip",
    () =>
      Effect.gen(function* () {
        const { world, start, records, lines, fs, cursorFile } = yield* harness;
        const before = yield* fs.readFileString(cursorFile);
        endRun(world, run("child", "run-1"), { answer: "Done." });
        yield* start;
        // Put the old cursor back, as if the process died between the append and the cursor write.
        yield* fs.writeFileString(cursorFile, before);
        yield* start;
        assert.strictEqual((yield* lines).length, 2);
        assert.deepStrictEqual(
          (yield* records).map((record) => record.runId),
          ["run-1"],
        );
      }),
  );

  it.effect("old runs without records are absent: a first start begins at the latest event", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const directory = path.join(
        yield* fs.makeTempDirectoryScoped({ prefix: "run-records-" }),
        "rr",
      );
      const world = newWorld();
      // Runs that ended before this version ran.
      endRun(world, run("child", "old-1"), { answer: "Old." });
      endRun(world, run("child", "old-2"), { answer: "Old." });
      const start = RunRecorder.make({ directory }).pipe(
        Effect.flatMap((recorder) => recorder.follow),
        Effect.provide(dependencies(world)),
      );
      yield* start;
      endRun(world, run("child", "new"), { answer: "New." });
      yield* start;
      const recorded = parseRunRecordLines(
        yield* fs.readFileString(path.join(directory, RunRecorder.RUNS_FILE)),
      );
      assert.deepStrictEqual(
        recorded.map((record) => record.runId),
        ["new"],
      );
      const summary = summarizeRunRecords(recorded, { toMs: NOW_MS + 1 });
      assert.strictEqual(summary.runs, 1);
      assert.deepStrictEqual(
        summary.byKind.map((group) => [group.key, group.runs]),
        [["fix", 1]],
      );
    }),
  );

  it.effect(
    "an unreadable cursor starts again at the latest event; a torn line spoils no record",
    () =>
      Effect.gen(function* () {
        const { world, start, records, fs, runsFile, cursorFile } = yield* harness;
        endRun(world, run("child", "run-1"), { answer: "Done." });
        yield* start;
        // A crash tore a line (no newline), and the cursor file is unreadable.
        yield* fs.writeFileString(runsFile, '{"v":1,"runId":"torn","endedAt":"2026', { flag: "a" });
        yield* fs.writeFileString(cursorFile, "{not json");
        // Atomic cursor writes make this unlikely; if it happens, runs since the last good start are lost.
        endRun(world, run("child", "ended-before-the-reset"), { answer: "Lost." });
        yield* start;
        endRun(world, run("child", "run-2"), { answer: "Done." });
        yield* start;
        assert.deepStrictEqual(
          (yield* records).map((record) => record.runId),
          ["run-1", "run-2"],
        );
      }),
  );

  it.effect(
    "a run whose records cannot be read is retried, skipped after 3 tries, and the next one recorded",
    () =>
      Effect.gen(function* () {
        const { world, make, records, cursor } = yield* harness;
        world.threads.set("broken", childThread("broken", "parent", "Broken"));
        world.failReads.set("broken", Number.POSITIVE_INFINITY);
        endRun(world, run("broken", "unreadable"), { answer: "?" });
        endRun(world, run("child", "fine"), { answer: "Done." });
        const recorder = yield* make;
        // The follower stops and retries from its cursor; the third failure skips the run.
        for (let attempt = 1; attempt < RunRecorder.READ_ATTEMPTS; attempt++) {
          assert.isTrue(Exit.isFailure(yield* Effect.exit(recorder.follow)), `try ${attempt}`);
          assert.deepStrictEqual(yield* records, []);
        }
        yield* recorder.follow;
        assert.deepStrictEqual(
          (yield* records).map((record) => record.runId),
          ["fine"],
        );
        assert.strictEqual((yield* cursor).sequence, 2);
      }),
  );

  it.effect("a passing read failure (a busy database) loses no record", () =>
    Effect.gen(function* () {
      const { world, make, records } = yield* harness;
      world.failReads.set("child", 1);
      endRun(world, run("child", "busy"), { answer: "Done." });
      const recorder = yield* make;
      assert.isTrue(Exit.isFailure(yield* Effect.exit(recorder.follow)));
      yield* recorder.follow;
      assert.deepStrictEqual(
        (yield* records).map((record) => record.runId),
        ["busy"],
      );
    }),
  );

  it.effect("a run whose thread is gone is skipped at once", () =>
    Effect.gen(function* () {
      const { world, start, records, cursor } = yield* harness;
      world.threads.set("deleted", childThread("deleted", "parent", "Deleted"));
      world.missing.add("deleted");
      endRun(world, run("deleted", "gone"), { answer: "?" });
      endRun(world, run("child", "fine"), { answer: "Done." });
      yield* start;
      assert.deepStrictEqual(
        (yield* records).map((record) => record.runId),
        ["fine"],
      );
      assert.strictEqual((yield* cursor).sequence, 2);
    }),
  );

  it.effect("a failed append moves no cursor: the record lands on the retry", () =>
    Effect.gen(function* () {
      const { fs, world, start, records, runsFile } = yield* harness;
      // The line cannot be written (here a folder stands in the file's place).
      yield* fs.makeDirectory(runsFile);
      endRun(world, run("child", "run-1"), { answer: "Done." });
      assert.isTrue(Exit.isFailure(yield* Effect.exit(start)));
      yield* fs.remove(runsFile, { recursive: true });
      yield* start;
      assert.deepStrictEqual(
        (yield* records).map((record) => record.runId),
        ["run-1"],
      );
    }),
  );

  it.effect("a failed cursor save after the line writes no second line on retries", () =>
    Effect.gen(function* () {
      const { fs, path, world, make, lines, cursorFile } = yield* harness;
      const recorder = yield* make;
      yield* recorder.follow;
      // The cursor cannot be saved (a full disk; here a folder in its place), the line can.
      yield* fs.remove(cursorFile);
      yield* fs.makeDirectory(path.join(cursorFile, "blocked"), { recursive: true });
      endRun(world, run("child", "run-1"), { answer: "Done." });
      // The first try fails at the cursor; the follower's retries find the run already written.
      assert.isTrue(Exit.isFailure(yield* Effect.exit(recorder.follow)));
      yield* Effect.exit(recorder.follow);
      yield* Effect.exit(recorder.follow);
      assert.strictEqual((yield* lines).length, 1);
      // Space again: the next run moves the cursor past both.
      yield* fs.remove(cursorFile, { recursive: true });
      endRun(world, run("child", "run-2"), { answer: "Done." });
      yield* recorder.follow;
      assert.strictEqual((yield* lines).length, 2);
    }),
  );

  it.effect("a run that ends again after other runs ended is still not written twice", () =>
    Effect.gen(function* () {
      const { world, start, lines } = yield* harness;
      endRun(world, run("child", "a"), { answer: "A." });
      endRun(world, run("child", "b"), { answer: "B." });
      endRun(world, run("child", "c"), { answer: "C." });
      // A repeat end of run a, after b and c: only the recent ids can tell.
      storeUpdate(world, run("child", "a"));
      yield* start;
      storeUpdate(world, run("child", "b"));
      yield* start;
      assert.strictEqual((yield* lines).length, 3);
    }),
  );

  it.effect("a cursor ahead of the event store (a restored database) starts again at latest", () =>
    Effect.gen(function* () {
      const { fs, world, start, records, cursor, cursorFile } = yield* harness;
      endRun(world, run("child", "before"), { answer: "Done." });
      yield* start;
      // The database goes back to an older copy; the records folder stays.
      world.events = [];
      world.latestSequence = 0;
      yield* fs.writeFileString(
        cursorFile,
        JSON.stringify({ v: 1, sequence: 50, recent: ["before"] }),
      );
      yield* start;
      assert.strictEqual((yield* cursor).sequence, 0);
      endRun(world, run("child", "after"), { answer: "Done." });
      yield* start;
      assert.deepStrictEqual(
        (yield* records).map((record) => record.runId),
        ["before", "after"],
      );
      assert.deepStrictEqual((yield* cursor).recent, ["before", "after"]);
    }),
  );

  it.effect("a refused estimate tag is recorded as refused, with no estimate", () =>
    Effect.gen(function* () {
      const { world, start, records } = yield* harness;
      world.subagents[0] = task(
        "parent",
        "child",
        "Act as the writer sub-agent for this task.\n\n[run-estimate: 2M]\nFix it.",
        "Fix the cursor",
      );
      endRun(world, run("child", "run-1"), { answer: "Done." });
      yield* start;
      const [record] = yield* records;
      assert.strictEqual(record?.estimate, null);
      assert.strictEqual(record?.estimateRefused, "2M");
    }),
  );

  it.effect("an empty estimate tag is recorded, logged and counted as refused", () => {
    const refusedTags: Array<unknown> = [];
    const logger = Logger.make(({ logLevel, message }) => {
      const [text, fields] = Array.isArray(message) ? message : [message];
      if (logLevel === "Warn" && String(text).startsWith("run estimate tag refused")) {
        refusedTags.push((fields as { tag?: unknown } | undefined)?.tag);
      }
    });
    return Effect.gen(function* () {
      const { world, start, records } = yield* harness;
      world.subagents[0] = task(
        "parent",
        "child",
        "Act as the writer sub-agent for this task.\n\n[run-estimate: ]\nFix it.",
        "Fix the cursor",
      );
      endRun(world, run("child", "run-1"), { answer: "Done." });
      yield* start;
      // Runs again (a restart replays the end): still one line, logged once.
      storeUpdate(world, run("child", "run-1"));
      yield* start;
      const all = yield* records;
      assert.deepStrictEqual(
        all.map((record) => [record.runId, record.estimate, record.estimateRefused]),
        [["run-1", null, ""]],
      );
      assert.deepStrictEqual(refusedTags, [""]);
      assert.strictEqual(summarizeRunRecords(all, { toMs: NOW_MS + 1 }).refusedEstimates, 1);
    }).pipe(Effect.provide(Logger.layer([logger], { mergeWithExisting: false })));
  });

  it.effect("a typo in the real tag is refused and logged, not replaced by a later example", () => {
    const warnings: Array<[string, unknown]> = [];
    const logger = Logger.make(({ logLevel, message }) => {
      const [text, fields] = Array.isArray(message) ? message : [message];
      if (logLevel === "Warn" && /^run (estimate|kind) tag refused/u.test(String(text))) {
        warnings.push([
          String(text).split(" ")[1]!,
          (fields as { tag?: unknown } | undefined)?.tag,
        ]);
      }
    });
    return Effect.gen(function* () {
      const { world, start, records } = yield* harness;
      world.subagents[0] = task(
        "parent",
        "child",
        "Act as the writer sub-agent for this task.\n\n[run-kind: lens [x]] [run-estimate: 30-40m]\nTag format: [run-estimate: 1h 2M tokens]\nFix it.",
        "Fix the cursor",
      );
      endRun(world, run("child", "run-1"), { answer: "Done." });
      yield* start;
      // Runs again (a restart replays the end): still one line, each warning once.
      storeUpdate(world, run("child", "run-1"));
      yield* start;
      const all = yield* records;
      assert.deepStrictEqual(
        all.map((record) => [
          record.estimate,
          record.estimateRefused,
          record.kind,
          record.kindRefused,
        ]),
        [[null, "30-40m", null, "lens"]],
      );
      assert.deepStrictEqual(warnings, [
        ["estimate", "30-40m"],
        ["kind", "lens"],
      ]);
      const summary = summarizeRunRecords(all, { toMs: NOW_MS + 1 });
      assert.strictEqual(summary.refusedEstimates, 1);
      assert.strictEqual(summary.refusedKinds, 1);
    }).pipe(Effect.provide(Logger.layer([logger], { mergeWithExisting: false })));
  });

  it.effect("an earlier mention of the tag does not hide the real one, and is not logged", () => {
    const refusedTags: Array<unknown> = [];
    const logger = Logger.make(({ logLevel, message }) => {
      const [text, fields] = Array.isArray(message) ? message : [message];
      if (logLevel === "Warn" && String(text).startsWith("run estimate tag refused")) {
        refusedTags.push((fields as { tag?: unknown } | undefined)?.tag);
      }
    });
    return Effect.gen(function* () {
      const { world, start, records } = yield* harness;
      world.subagents[0] = task(
        "parent",
        "child",
        "Act as the writer sub-agent for this task.\n\nSee the [run-estimate: format below.\n[run-estimate: 40m]\nFix it.",
        "Fix the cursor",
      );
      endRun(world, run("child", "run-1"), { answer: "Done." });
      yield* start;
      const [record] = yield* records;
      assert.deepStrictEqual(record?.estimate, { wallMs: 40 * 60_000 });
      assert.strictEqual(record?.estimateRefused, null);
      assert.deepStrictEqual(refusedTags, []);
    }).pipe(Effect.provide(Logger.layer([logger], { mergeWithExisting: false })));
  });

  it.effect("a too-long estimate tag is refused once, even when its run ends again or stops", () =>
    Effect.gen(function* () {
      const { world, start, records, lines } = yield* harness;
      const long = `40m ${"x".repeat(100)}`;
      world.subagents[0] = task(
        "parent",
        "child",
        `Act as the writer sub-agent for this task.\n\n[run-estimate: ${long}]\nFix it.`,
        "Fix the cursor",
      );
      endRun(world, run("child", "done"), { answer: "Done." });
      // Stopped half-way, then the same end stored again after a restart.
      endRun(world, run("child", "stopped", { status: "interrupted" }));
      yield* start;
      storeUpdate(world, run("child", "done"));
      storeUpdate(world, run("child", "stopped", { status: "interrupted" }));
      yield* start;
      assert.strictEqual((yield* lines).length, 2);
      const refused = `${long.slice(0, 80)}…`;
      assert.deepStrictEqual(
        (yield* records).map((record) => [
          record.runId,
          record.endStatus,
          record.estimate,
          record.estimateRefused,
        ]),
        [
          ["done", "completed", null, refused],
          ["stopped", "cut_off", null, refused],
        ],
      );
      const summary = summarizeRunRecords(yield* records, { toMs: NOW_MS + 1 });
      assert.strictEqual(summary.refusedEstimates, 2);
    }),
  );

  it.effect("records a top-level run (an orchestrator) without a parent or labels", () =>
    Effect.gen(function* () {
      const { world, start, records } = yield* harness;
      endRun(world, run("parent", "orchestrating"), { answer: "Planned." });
      yield* start;
      const [record] = yield* records;
      assert.strictEqual(record?.parent, null);
      assert.strictEqual(record?.kind, null);
      assert.strictEqual(record?.threadTitle, "Orchestrator");
    }),
  );
  it.effect("writes a lane run's lane and keeps reading records written before lanes", () =>
    Effect.gen(function* () {
      const { world, start, records, runsFile, path } = yield* harness;
      // Written by a recorder without the lane store: as every record before issue 1156.
      endRun(world, run("child", "old-run"), { answer: "Done." });
      yield* start;
      // The orchestrator thread now runs the pr-queue lane; its child is one of its workers.
      const lanes = Layer.mock(LaneStore)({
        laneOfRun: (_projectId, threadId, parentThreadId) =>
          Effect.succeed(threadId === "parent" || parentThreadId === "parent" ? "pr-queue" : null),
      });
      const withLanes = RunRecorder.make({ directory: path.dirname(runsFile) }).pipe(
        Effect.provide(Layer.merge(dependencies(world), lanes)),
      );
      endRun(world, run("child", "worker-run"), { answer: "Done." });
      endRun(world, run("parent", "lane-run"), { answer: "Done." });
      world.threads.set("alone", thread("alone"));
      endRun(world, run("alone", "no-lane-run"), { answer: "Done." });
      yield* (yield* withLanes).follow;
      const byRun = new Map((yield* records).map((record) => [record.runId, record]));
      assert.deepStrictEqual(
        ["old-run", "worker-run", "lane-run", "no-lane-run"].map((id) => {
          const record = byRun.get(id);
          return record === undefined ? "missing" : runRecordLane(record);
        }),
        [null, "pr-queue", "pr-queue", null],
      );
      // Runs outside a lane, old ones included, carry no lane field at all.
      assert.isFalse("lane" in byRun.get("old-run")!);
      assert.isFalse("lane" in byRun.get("no-lane-run")!);
      const summary = summarizeRunRecords([...byRun.values()], { toMs: NOW_MS + 1 });
      assert.strictEqual(summary.runs, 4);
    }),
  );
});
