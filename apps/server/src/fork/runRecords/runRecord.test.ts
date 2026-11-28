/** Fork: one run record per ended run (issue 315): labels, usage sums, end status, reading lines. */
import { assert, describe, it } from "@effect/vitest";
import * as DateTime from "effect/DateTime";

import {
  buildRunRecord,
  MAX_TAG_CHARS,
  parseEstimate,
  parseRunRecordLines,
  parseTaskLabels,
  readEstimateTag,
  runRecordLane,
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
        kindRefused: null,
        estimate: { wallMs: 90 * MINUTE_MS, tokens: 2_500_000 },
        estimateRefused: null,
      },
    );
  });

  it("is empty for plain text and for no text", () => {
    assert.deepStrictEqual(parseTaskLabels("Please look at the logs."), {
      role: null,
      kind: null,
      kindRefused: null,
      estimate: null,
      estimateRefused: null,
    });
    assert.deepStrictEqual(parseTaskLabels(null), {
      role: null,
      kind: null,
      kindRefused: null,
      estimate: null,
      estimateRefused: null,
    });
  });

  it("reads an estimate of only time or only tokens, and none from garbage", () => {
    assert.deepStrictEqual(parseEstimate("[run-estimate: 45s]"), { wallMs: 45_000 });
    assert.deepStrictEqual(parseEstimate("[run-estimate: 300k tokens]"), { tokens: 300_000 });
    assert.strictEqual(parseEstimate("[run-estimate: soon]"), null);
    assert.strictEqual(parseEstimate("no tag"), null);
  });

  it("reads joined units, capitals, exponents and grouped numbers right", () => {
    const cases: ReadonlyArray<readonly [string, { wallMs?: number; tokens?: number }]> = [
      ["1h30m", { wallMs: 90 * MINUTE_MS }],
      ["1h 30m", { wallMs: 90 * MINUTE_MS }],
      ["1H30min", { wallMs: 90 * MINUTE_MS }],
      ["1.5h", { wallMs: 90 * MINUTE_MS }],
      ["90 min", { wallMs: 90 * MINUTE_MS }],
      ["2 hours and 5 minutes", { wallMs: 125 * MINUTE_MS }],
      ["1h 0m", { wallMs: 60 * MINUTE_MS }],
      ["~40m", { wallMs: 40 * MINUTE_MS }],
      ["2m", { wallMs: 2 * MINUTE_MS }],
      ["2M Tokens", { tokens: 2_000_000 }],
      ["2.5m tokens", { tokens: 2_500_000 }],
      ["2 million tokens", { tokens: 2_000_000 }],
      ["300K TOKENS", { tokens: 300_000 }],
      ["1e6 tokens", { tokens: 1_000_000 }],
      ["1E6 tokens", { tokens: 1_000_000 }],
      ["1,500,000 tokens", { tokens: 1_500_000 }],
      ["5000 tok", { tokens: 5000 }],
      ["40m, 2M tokens", { wallMs: 40 * MINUTE_MS, tokens: 2_000_000 }],
      ["2M tokens + 1h30m", { wallMs: 90 * MINUTE_MS, tokens: 2_000_000 }],
    ];
    for (const [tag, expected] of cases) {
      assert.deepStrictEqual(
        readEstimateTag(`[run-estimate: ${tag}]`),
        { estimate: expected, refused: null },
        tag,
      );
    }
  });

  it("refuses, never guesses, a tag it cannot read in full", () => {
    for (const tag of [
      "soon",
      "40",
      "2M",
      "300k",
      "30-40m",
      "about 40m",
      "40m or so",
      "1h 1h",
      "90min 30m",
      "2M tokens 3M tokens",
      "2,5M tokens",
      "2x tokens",
      "1e400 tokens",
      "0m",
      "0 tokens",
      "40 m tokens extra",
    ]) {
      assert.deepStrictEqual(
        readEstimateTag(`Act as the writer sub-agent for this task. [run-estimate: ${tag}]`),
        { estimate: null, refused: tag },
        tag,
      );
      assert.strictEqual(parseEstimate(`[run-estimate: ${tag}]`), null, tag);
    }
    assert.strictEqual(readEstimateTag("no tag here"), null);
    assert.deepStrictEqual(parseTaskLabels("[run-estimate: 2M]"), {
      role: null,
      kind: null,
      kindRefused: null,
      estimate: null,
      estimateRefused: "2M",
    });
  });

  it("refuses an estimate tag that is too long or never closed, keeping its text cut short", () => {
    const long = `40m ${"x".repeat(MAX_TAG_CHARS)}`;
    assert.deepStrictEqual(readEstimateTag(`[run-estimate: ${long}]`), {
      estimate: null,
      refused: `${long.slice(0, MAX_TAG_CHARS)}…`,
    });
    // Readable in every part, but longer than a tag may be: refused all the same.
    const wordy = `1 hour${" and".repeat(20)} 30 minutes`;
    assert.isAbove(wordy.length, MAX_TAG_CHARS);
    assert.strictEqual(readEstimateTag(`[run-estimate: ${wordy}]`)?.refused?.length, 81);
    assert.deepStrictEqual(readEstimateTag("[run-estimate: 40m\nDo it. ]"), {
      estimate: null,
      refused: "40m",
    });
    assert.deepStrictEqual(readEstimateTag("[run-estimate: ]"), { estimate: null, refused: "" });
    // Exactly the limit is still read.
    const atLimit = `40m${" ".repeat(MAX_TAG_CHARS - 12)}2M tokens`;
    assert.strictEqual(atLimit.length, MAX_TAG_CHARS);
    assert.deepStrictEqual(parseEstimate(`[run-estimate: ${atLimit}]`), {
      wallMs: 40 * MINUTE_MS,
      tokens: 2_000_000,
    });
  });

  it("reads the first closed tag that fits: an earlier unclosed or long mention never hides it", () => {
    const real = "[run-estimate: 40m]";
    for (const mention of [
      "see the [run-estimate: format below\n",
      "see the [run-estimate: format below\r\n",
      `[run-estimate: 40m ${"x".repeat(MAX_TAG_CHARS)}]\n`,
      // On the same line: the next `[` ends the unclosed mention.
      "see [run-estimate: ",
    ]) {
      assert.deepStrictEqual(
        readEstimateTag(`${mention}${real}`),
        { estimate: { wallMs: 40 * MINUTE_MS }, refused: null },
        JSON.stringify(mention),
      );
    }
    // With no closed tag that fits, the first one is refused, not a later one.
    assert.deepStrictEqual(readEstimateTag("see [run-estimate: \n[run-estimate: 40m"), {
      estimate: null,
      refused: "",
    });
    // Many unclosed openers before the real tag: each is passed over, the real one still read.
    const openers = "[run-estimate: ".repeat(200_000);
    assert.deepStrictEqual(readEstimateTag(`${openers}${real}`)?.estimate, {
      wallMs: 40 * MINUTE_MS,
    });
  });

  it("refuses the first closed tag that does not read: a later tag never replaces a typo", () => {
    // The review's case: the real tag has a typo, and a later example of the format reads.
    assert.deepStrictEqual(
      readEstimateTag("[run-estimate: 30-40m]\nTag format: [run-estimate: 1h 2M tokens]"),
      { estimate: null, refused: "30-40m" },
    );
    for (const [first, refused] of [
      ["[run-estimate: about 40m]", "about 40m"],
      ["[run-estimate: soon]", "soon"],
      // An empty tag (a template left blank) is refused too, as an empty kind tag is.
      ["[run-estimate: ]", ""],
      // After an unclosed mention, the first closed tag still decides.
      ["see [run-estimate: \n[run-estimate: 1-2h]", "1-2h"],
    ] as const) {
      assert.deepStrictEqual(
        readEstimateTag(`${first}\nold brief said [run-estimate: 2h]`),
        { estimate: null, refused },
        first,
      );
    }
  });

  it("decides the kind by the first closed tag too, and refuses an empty or unclosed one", () => {
    assert.strictEqual(parseTaskLabels("see [run-kind: format [run-kind: fix]").kind, "fix");
    assert.strictEqual(parseTaskLabels("see the [run-kind: format\n[run-kind: fix]").kind, "fix");
    const labels = (text: string) => {
      const { kind, kindRefused } = parseTaskLabels(text);
      return { kind, kindRefused };
    };
    // An empty closed tag is refused, for the kind as for the estimate; a later tag does not win.
    assert.deepStrictEqual(labels("[run-kind: ]\n[run-kind: fix]"), {
      kind: null,
      kindRefused: "",
    });
    assert.deepStrictEqual(parseTaskLabels("[run-estimate: ]\n[run-estimate: 40m]"), {
      ...parseTaskLabels(null),
      estimateRefused: "",
    });
    // A `[` ends the tag: a kind that holds one is refused (and logged), never silently dropped.
    assert.deepStrictEqual(labels("[run-kind: lens [x]]"), { kind: null, kindRefused: "lens" });
    assert.deepStrictEqual(labels("[run-kind: fix"), { kind: null, kindRefused: "fix" });
    assert.deepStrictEqual(labels(`[run-kind: ${"y".repeat(100)}`), {
      kind: null,
      kindRefused: `${"y".repeat(MAX_TAG_CHARS)}…`,
    });
    assert.deepStrictEqual(labels("no kind here"), { kind: null, kindRefused: null });
  });

  it("counts and cuts whole characters, never half an emoji or a letter's accent", () => {
    const emoji = "\u{1F600}"; // two UTF-16 units
    const family = "\u{1F468}\u200D\u{1F469}\u200D\u{1F467}"; // one character, eight units
    const accent = "e\u0301"; // e and a combining accent: one character
    for (const char of [emoji, family, accent]) {
      // 80 characters fit even though they are more than 80 units.
      const atLimit = char.repeat(MAX_TAG_CHARS);
      assert.strictEqual(parseTaskLabels(`[run-kind: ${atLimit}]`).kind, atLimit);
      const refused = readEstimateTag(`[run-estimate: ${atLimit}]`)?.refused;
      assert.strictEqual(refused, atLimit, "refused as unreadable, not as too long");
      // One more is cut back to 80 whole characters.
      const over = `${"a".repeat(MAX_TAG_CHARS - 1)}${char.repeat(2)}`;
      const cut = `${"a".repeat(MAX_TAG_CHARS - 1)}${char}…`;
      assert.strictEqual(readEstimateTag(`[run-estimate: ${over}]`)?.refused, cut);
      assert.strictEqual(parseTaskLabels(`[run-kind: ${over}]`).kind, cut);
      // The cut text survives a record line unchanged: no lone surrogate.
      assert.strictEqual(JSON.parse(JSON.stringify(cut)), cut);
      assert.isTrue(cut.isWellFormed());
    }
  });

  it("cuts a long kind tag rather than dropping it", () => {
    const long = `lens: ${"data ".repeat(30)}`.trim();
    assert.strictEqual(parseTaskLabels(`[run-kind: ${long}]`).kind, `${long.slice(0, 80)}…`);
    assert.strictEqual(parseTaskLabels("[run-kind:  lens: data ]").kind, "lens: data");
    assert.strictEqual(parseTaskLabels("[run-kind: ]").kind, null);
    assert.strictEqual(parseTaskLabels("[run-kind: ]").kindRefused, "");
  });

  it("reads a token scale written right before the word tokens", () => {
    const cases: ReadonlyArray<readonly [string, { wallMs?: number; tokens?: number }]> = [
      ["2ktokens", { tokens: 2000 }],
      ["2Mtokens", { tokens: 2_000_000 }],
      ["300ktok", { tokens: 300_000 }],
      ["2.5mtokens", { tokens: 2_500_000 }],
      ["2 milliontokens", { tokens: 2_000_000 }],
      ["1e6tokens", { tokens: 1_000_000 }],
      ["5000tok", { tokens: 5000 }],
      ["40m 2Mtokens", { wallMs: 40 * MINUTE_MS, tokens: 2_000_000 }],
    ];
    for (const [tag, expected] of cases) {
      assert.deepStrictEqual(parseEstimate(`[run-estimate: ${tag}]`), expected, tag);
    }
    for (const tag of ["2xtokens", "2 tokens tokens", "2Mtokens tokens", "2etokens"]) {
      assert.deepStrictEqual(readEstimateTag(`[run-estimate: ${tag}]`), {
        estimate: null,
        refused: tag,
      });
    }
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
      kindRefused: null,
      estimate: { wallMs: 40 * MINUTE_MS, tokens: 2_000_000 },
      estimateRefused: null,
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

  it("keeps a refused estimate tag's text, and takes no guess from the run's own message", () => {
    const record = buildRunRecord(
      input({
        task: task("parent", "child", "[run-estimate: 1-2h]\nDo it.", "Fix the cursor"),
        runMessage: "[run-estimate: 40m]",
      }),
    );
    assert.strictEqual(record?.estimate, null);
    assert.strictEqual(record?.estimateRefused, "1-2h");
    // Without a tag in the task, the run's message gives it.
    const fromRun = buildRunRecord(
      input({ task: task("parent", "child", "Do it.", "Fix"), runMessage: "[run-estimate: 40m]" }),
    );
    assert.deepStrictEqual(fromRun?.estimate, { wallMs: 40 * MINUTE_MS });
    assert.strictEqual(fromRun?.estimateRefused, null);
  });

  it("keeps a refused kind tag's text: no task title and no kind from the run's message", () => {
    const record = buildRunRecord(
      input({
        task: task("parent", "child", "[run-kind: ]\nDo it.", "Fix the cursor"),
        runMessage: "[run-kind: other]",
      }),
    );
    assert.strictEqual(record?.kind, null);
    assert.strictEqual(record?.kindRefused, "");
    // Without a tag in the task, the run's message gives it, and nothing is refused.
    const fromRun = buildRunRecord(
      input({ task: task("parent", "child", "Do it.", "Fix"), runMessage: "[run-kind: other]" }),
    );
    assert.strictEqual(fromRun?.kind, "other");
    assert.strictEqual(fromRun?.kindRefused, null);
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
    // Streamed lines read the same; `keep` drops records as they are read.
    assert.deepStrictEqual(
      parseRunRecordLines(text.split("\n").values()).map((record) => record.runId),
      ["run-1", "run-2"],
    );
    assert.deepStrictEqual(
      parseRunRecordLines(text.split("\n"), (record) => record.runId === "run-2").map(
        (record) => record.runId,
      ),
      ["run-2"],
    );
  });

  it("reads a record without the lane field (written before it) as no lane", () => {
    const record = buildRunRecord(input())!;
    // Nothing writes the field yet: a new record has none, as an old one.
    assert.isFalse("lane" in record);
    const [read] = parseRunRecordLines(JSON.stringify(record));
    assert.strictEqual(read?.runId, "run-1");
    assert.strictEqual(runRecordLane(read!), null);
  });

  it("reads a record's lane, keeps a name no longer defined, and reads a bad one as none", () => {
    const record = buildRunRecord(input())!;
    const lines = [
      { ...record, runId: "a", lane: "pr-queue" },
      { ...record, runId: "b", lane: "a-lane-since-removed" },
      { ...record, runId: "c", lane: null },
      { ...record, runId: "d", lane: 7 },
      { ...record, runId: "e", lane: "  " },
    ].map((line) => JSON.stringify(line));
    const records = parseRunRecordLines(lines.join("\n"));
    assert.deepStrictEqual(
      records.map((read) => [read.runId, runRecordLane(read)]),
      [
        ["a", "pr-queue"],
        ["b", "a-lane-since-removed"],
        ["c", null],
        ["d", null],
        ["e", null],
      ],
    );
  });
});
