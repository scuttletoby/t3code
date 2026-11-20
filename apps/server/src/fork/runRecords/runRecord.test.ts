/** Fork: one run record per ended run (issue 315): labels, usage sums, end status, reading lines. */
import { assert, describe, it } from "@effect/vitest";
import * as DateTime from "effect/DateTime";

import {
  buildRunRecord,
  parseEstimate,
  parseRunRecordLines,
  parseTaskLabels,
  type RunRecordInput,
  sumRunUsage,
} from "./runRecord.ts";
import { iso, MINUTE_MS, NOW_MS, providerTurn, run, task, usage } from "./runRecords.testkit.ts";

const ms = (value: DateTime.Utc) => DateTime.toEpochMillis(value);

const input = (patch: Partial<RunRecordInput> = {}): RunRecordInput => {
  const theRun = patch.run ?? run("child", "run-1");
  return {
    run: theRun,
    thread: {
      title: "Fix the cursor",
      projectId: "project-1",
      parentThreadId: "parent",
      delegatedFrom: null,
    },
    projectTitle: "t3",
    task: task(
      "parent",
      "child",
      "Act as the writer sub-agent for this task.\n\n[run-kind: fix] [run-estimate: 40m 2M tokens]\nDo it.",
      "Fix the cursor",
    ),
    runMessage: null,
    finalMessage: true,
    providerTurns: [providerTurn("run-1", usage(1000, 200, { cachedInputTokens: 600 }))],
    times: {
      requestedMs: ms(theRun.requestedAt),
      startedMs: theRun.startedAt === null ? NOW_MS : ms(theRun.startedAt),
      endedMs: NOW_MS,
    },
    ...patch,
  };
};

describe("parseTaskLabels", () => {
  it("reads the delegate role, the kind tag and the estimate tag", () => {
    assert.deepStrictEqual(
      parseTaskLabels(
        "Act as the review lens sub-agent for this task.\n\n[run-kind: lens: data] [run-estimate: 1h 30m 2.5M tokens]",
      ),
      {
        role: "review lens",
        kind: "lens: data",
        estimate: { wallMs: 90 * MINUTE_MS, tokens: 2_500_000 },
      },
    );
  });

  it("is empty for plain text and for no text", () => {
    assert.deepStrictEqual(parseTaskLabels("Please look at the logs."), {
      role: null,
      kind: null,
      estimate: null,
    });
    assert.deepStrictEqual(parseTaskLabels(null), { role: null, kind: null, estimate: null });
  });

  it("reads an estimate of only time or only tokens, and none from garbage", () => {
    assert.deepStrictEqual(parseEstimate("[run-estimate: 45s]"), { wallMs: 45_000 });
    assert.deepStrictEqual(parseEstimate("[run-estimate: 300k tokens]"), { tokens: 300_000 });
    assert.strictEqual(parseEstimate("[run-estimate: soon]"), null);
    assert.strictEqual(parseEstimate("no tag"), null);
  });
});

describe("sumRunUsage", () => {
  it("adds every turn and reports the models that ran", () => {
    const summed = sumRunUsage([
      {
        turnTokenUsage: usage(1000, 100, {
          cachedInputTokens: 800,
          reasoningTokens: 40,
          models: ["opus"],
        }),
      },
      {
        turnTokenUsage: usage(500, 50, {
          cacheCreationTokens: 200,
          hasSubagents: true,
          otherUsage: { inputTokens: 70, outputTokens: 7 },
        } as never),
        model: "fable",
      },
    ]);
    assert.deepStrictEqual(summed, {
      usage: {
        status: "complete",
        providerTurns: 2,
        inputTokens: 1500,
        cachedInputTokens: 800,
        cacheCreationTokens: 200,
        outputTokens: 150,
        reasoningTokens: 40,
        otherInputTokens: 70,
        otherOutputTokens: 7,
        hasSubagents: true,
      },
      executedModels: ["fable", "opus"],
    });
  });

  it("is partial when a turn reported nothing, and unavailable when none did", () => {
    assert.strictEqual(sumRunUsage([{ turnTokenUsage: usage(10, 1) }, {}]).usage.status, "partial");
    assert.strictEqual(
      sumRunUsage([{ turnTokenUsage: usage(10, 1, { usageStatus: "partial" }) }]).usage.status,
      "partial",
    );
    assert.strictEqual(sumRunUsage([{}]).usage.status, "unavailable");
    assert.strictEqual(sumRunUsage([]).usage.status, "unavailable");
  });

  it("leaves out local commands, which used no model", () => {
    const summed = sumRunUsage([
      { turnTokenUsage: usage(10, 1) },
      { turnTokenUsage: usage(999, 999, { localCommand: true }) },
    ]);
    assert.strictEqual(summed.usage.inputTokens, 10);
    assert.strictEqual(summed.usage.status, "complete");
  });
});

describe("buildRunRecord", () => {
  it("records a normal delegated run with its labels, model, effort, times and usage", () => {
    const record = buildRunRecord(input());
    assert.deepStrictEqual(record, {
      v: 1,
      runId: "run-1",
      threadId: "child",
      threadTitle: "Fix the cursor",
      projectId: "project-1",
      projectTitle: "t3",
      parent: { threadId: "parent", taskId: "task:child", environmentId: null },
      role: "writer",
      kind: "fix",
      estimate: { wallMs: 40 * MINUTE_MS, tokens: 2_000_000 },
      continuationOf: null,
      providerInstanceId: "claudeAgent",
      model: "claude-opus-5-5",
      effort: "high",
      modelOptions: { effort: "high" },
      executedModels: [],
      requestedAt: iso(NOW_MS - 11 * MINUTE_MS),
      startedAt: iso(NOW_MS - 10 * MINUTE_MS),
      endedAt: iso(NOW_MS),
      wallMs: 10 * MINUTE_MS,
      queuedMs: MINUTE_MS,
      status: "completed",
      endStatus: "completed",
      finalMessage: true,
      usage: {
        status: "complete",
        providerTurns: 1,
        inputTokens: 1000,
        cachedInputTokens: 600,
        cacheCreationTokens: 0,
        outputTokens: 200,
        reasoningTokens: 0,
        otherInputTokens: 0,
        otherOutputTokens: 0,
        hasSubagents: false,
      },
    });
  });

  it("records a run stopped half-way as cut off, with the usage seen until then", () => {
    for (const status of ["interrupted", "cancelled"] as const) {
      const record = buildRunRecord(
        input({
          run: run("child", "run-1", { status }),
          finalMessage: false,
          providerTurns: [providerTurn("run-1", usage(300, 20, { usageStatus: "partial" }))],
        }),
      );
      assert.strictEqual(record?.endStatus, "cut_off");
      assert.strictEqual(record?.status, status);
      assert.strictEqual(record?.usage.inputTokens, 300);
      assert.strictEqual(record?.usage.status, "partial");
    }
  });

  it("records a completed run without a final message as cut off, and a failed one as failed", () => {
    assert.strictEqual(buildRunRecord(input({ finalMessage: false }))?.endStatus, "cut_off");
    assert.strictEqual(
      buildRunRecord(
        input({ run: run("child", "run-1", { status: "failed" }), finalMessage: false }),
      )?.endStatus,
      "failed",
    );
  });

  it("records nothing for a run that is still going or never started", () => {
    assert.strictEqual(
      buildRunRecord(input({ run: run("child", "run-1", { status: "running" }) })),
      null,
    );
    assert.strictEqual(
      buildRunRecord(
        input({ run: run("child", "run-1", { status: "cancelled", startedAt: null }) }),
      ),
      null,
    );
  });

  it("names a continuation and keeps the task's labels for a later run in the same thread", () => {
    const record = buildRunRecord(
      input({
        run: run("child", "run-2", { restartContinuationOfRunId: "run-1" as never }),
        runMessage: "Continue. [run-kind: other]",
      }),
    );
    assert.strictEqual(record?.continuationOf, "run-1");
    assert.strictEqual(record?.kind, "fix");
  });

  it("uses the task title as the kind when no tag was given, and none for a top-level thread", () => {
    const untagged = input({ task: task("parent", "child", "Do it.", "Write the docs") });
    assert.strictEqual(buildRunRecord(untagged)?.kind, "Write the docs");
    assert.strictEqual(buildRunRecord(untagged)?.role, null);
    const topLevel = buildRunRecord(
      input({
        task: null,
        thread: {
          title: "Chat",
          projectId: "project-1",
          parentThreadId: null,
          delegatedFrom: null,
        },
        runMessage: "[run-kind: orchestrator] keep going",
      }),
    );
    assert.strictEqual(topLevel?.parent, null);
    assert.strictEqual(topLevel?.kind, "orchestrator");
    assert.strictEqual(
      buildRunRecord(
        input({
          task: null,
          thread: {
            title: "Chat",
            projectId: "project-1",
            parentThreadId: null,
            delegatedFrom: null,
          },
        }),
      )?.kind,
      null,
    );
  });

  it("names the linked machine's thread as the parent of a task delegated from there", () => {
    const record = buildRunRecord(
      input({
        task: null,
        thread: {
          title: "Remote task",
          projectId: "project-1",
          parentThreadId: null,
          delegatedFrom: { environmentId: "env-a", threadId: "far-parent" },
        },
        runMessage: "Act as the lens sub-agent for this task.\n\nLook.",
      }),
    );
    assert.deepStrictEqual(record?.parent, {
      threadId: "far-parent",
      taskId: null,
      environmentId: "env-a",
    });
    assert.strictEqual(record?.role, "lens");
    assert.strictEqual(record?.kind, "Remote task");
  });
});

describe("parseRunRecordLines", () => {
  it("keeps the first line of each run and skips torn, foreign and newer-version lines", () => {
    const first = buildRunRecord(input())!;
    const again = { ...first, wallMs: 1 };
    const other = buildRunRecord(input({ run: run("child", "run-2") }))!;
    const text = [
      JSON.stringify(first),
      '{"v":1,"runId":"torn","endedAt":"20',
      JSON.stringify(again),
      JSON.stringify({ ...other, runId: "future", v: 2 }),
      '{"hello":"world"}',
      "",
      JSON.stringify(other),
    ].join("\n");
    const records = parseRunRecordLines(text);
    assert.deepStrictEqual(
      records.map((record) => [record.runId, record.wallMs]),
      [
        ["run-1", 10 * MINUTE_MS],
        ["run-2", 10 * MINUTE_MS],
      ],
    );
    assert.deepStrictEqual(parseRunRecordLines(""), []);
  });
});
