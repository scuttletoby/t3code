// @effect-diagnostics nodeBuiltinImport:off - tests the plain node summary script against real files.
/** Fork: the weekly run records summary and its command (issue 315). */
import * as NodeFS from "node:fs";
import * as NodeOS from "node:os";
import * as NodePath from "node:path";

import { afterEach, assert, describe, it } from "@effect/vitest";

import { parseRateTable } from "../../usage/usagePricing.ts";
import type { RunRecord } from "./runRecord.ts";
import {
  formatRunRecordsSummary,
  median,
  pricedModel,
  summarizeRunRecords,
  WEEK_MS,
} from "./runRecordsSummary.ts";
import { iso, MINUTE_MS, NOW_MS } from "./runRecords.testkit.ts";
import { nodeSummaryIo, readFileLines, runSummaryCli } from "./summaryCli.ts";

const DAY_MS = 24 * 60 * MINUTE_MS;

const record = (runId: string, patch: Partial<RunRecord> = {}): RunRecord => ({
  v: 1,
  runId,
  threadId: `thread:${runId}`,
  threadTitle: "Task",
  projectId: "project-1",
  projectTitle: "t3",
  parent: { threadId: "parent", taskId: null, environmentId: null },
  role: "writer",
  kind: "fix",
  estimate: null,
  continuationOf: null,
  providerInstanceId: "claudeAgent",
  model: "claude-opus-5-5",
  effort: "high",
  modelOptions: { effort: "high" },
  executedModels: [],
  requestedAt: iso(NOW_MS - DAY_MS - 11 * MINUTE_MS),
  startedAt: iso(NOW_MS - DAY_MS - 10 * MINUTE_MS),
  endedAt: iso(NOW_MS - DAY_MS),
  wallMs: 10 * MINUTE_MS,
  queuedMs: MINUTE_MS,
  status: "completed",
  endStatus: "completed",
  finalMessage: true,
  usage: {
    status: "complete",
    providerTurns: 1,
    inputTokens: 1000,
    cachedInputTokens: 0,
    cacheCreationTokens: 0,
    outputTokens: 100,
    reasoningTokens: 0,
    otherInputTokens: 0,
    otherOutputTokens: 0,
    hasSubagents: false,
  },
  ...patch,
});

const rates = parseRateTable({
  "claude-opus-5-5": { input_cost_per_token: 0.000001, output_cost_per_token: 0.00001 },
});

describe("summarizeRunRecords", () => {
  it("groups by kind, role and model with medians, cut-off runs and list prices", () => {
    const summary = summarizeRunRecords(
      [
        record("a"),
        record("b", { wallMs: 30 * MINUTE_MS, endStatus: "cut_off", status: "interrupted" }),
        record("c", {
          kind: "lens",
          role: "lens",
          model: "gpt-5",
          effort: null,
          wallMs: MINUTE_MS,
        }),
        record("d", {
          kind: null,
          role: null,
          parent: null,
          usage: { ...record("d").usage, status: "partial" },
        }),
      ],
      { toMs: NOW_MS, rates },
    );
    assert.strictEqual(summary.runs, 4);
    assert.deepStrictEqual(
      summary.byKind.map((group) => [group.key, group.runs]),
      [
        ["fix", 2],
        ["(none)", 1],
        ["lens", 1],
      ],
    );
    const fix = summary.byKind[0]!;
    assert.strictEqual(fix.cutOff, 1);
    assert.strictEqual(fix.medianWallMs, 20 * MINUTE_MS);
    assert.strictEqual(fix.medianTokens, 1100);
    assert.strictEqual(fix.totalTokens, 2200);
    // Two runs of 1000 input and 100 output tokens at $1 and $10 per million.
    assert.closeTo(fix.listCostUsd!, 2 * (0.001 + 0.001), 1e-9);
    assert.strictEqual(summary.byKind[1]!.usageIncomplete, 1);
    assert.deepStrictEqual(
      summary.byModel.map((group) => group.key),
      ["claude-opus-5-5 (high)", "gpt-5"],
    );
    // A model without a rate has no price, and is counted as unpriced rather than as $0.
    assert.strictEqual(summary.byModel[1]!.listCostUsd, null);
    assert.strictEqual(summary.byModel[1]!.unpricedRuns, 1);
    assert.strictEqual(summary.byModel[0]!.unpricedRuns, 0);
    assert.strictEqual(
      summarizeRunRecords([record("a")], { toMs: NOW_MS }).byKind[0]!.listCostUsd,
      null,
    );
  });

  it("says how many runs of a group had no rate, and prices a run at the model that ran", () => {
    const summary = summarizeRunRecords(
      [record("priced"), record("unpriced", { model: "gpt-5", effort: null })],
      { toMs: NOW_MS, rates },
    );
    const fix = summary.byKind[0]!;
    assert.closeTo(fix.listCostUsd!, 0.002, 1e-9);
    assert.strictEqual(fix.unpricedRuns, 1);
    assert.include(formatRunRecordsSummary(summary), "| $0.00 (+1 unpriced) |");
    // With a fallback model too, the requested model is priced, not the first name alphabetically.
    const withFallback = record("fallback", {
      executedModels: ["a-fallback-model", "claude-opus-5-5"],
    });
    assert.strictEqual(pricedModel(withFallback), "claude-opus-5-5");
    assert.strictEqual(
      summarizeRunRecords([withFallback], { toMs: NOW_MS, rates }).byKind[0]!.unpricedRuns,
      0,
    );
    // The one model the provider reported running is priced, even when it was not the one asked for.
    assert.strictEqual(
      pricedModel(record("swapped", { model: "gpt-5", executedModels: ["claude-opus-5-5"] })),
      "claude-opus-5-5",
    );
  });

  it("counts runs whose estimate tag was refused, and gives them no estimate row", () => {
    const summary = summarizeRunRecords(
      [record("refused", { estimateRefused: "1-2h" }), record("plain")],
      { toMs: NOW_MS },
    );
    assert.strictEqual(summary.refusedEstimates, 1);
    assert.strictEqual(summary.estimates.length, 0);
    assert.include(
      formatRunRecordsSummary(summary),
      "1 runs had an estimate tag that could not be read",
    );
    assert.notInclude(
      formatRunRecordsSummary(summarizeRunRecords([record("plain")], { toMs: NOW_MS })),
      "could not be read",
    );
  });

  it("counts only runs that ended in the window, each run once", () => {
    const summary = summarizeRunRecords(
      [
        record("in"),
        record("in", { wallMs: 99 }),
        record("old", { endedAt: iso(NOW_MS - WEEK_MS - 1) }),
        record("later", { endedAt: iso(NOW_MS) }),
      ],
      { toMs: NOW_MS },
    );
    assert.strictEqual(summary.runs, 1);
    assert.strictEqual(summary.byKind[0]!.totalWallMs, 10 * MINUTE_MS);
  });

  it("sets estimate against actual per delegated task, its re-runs summed", () => {
    const summary = summarizeRunRecords(
      [
        record("first", {
          threadId: "task-1",
          estimate: { wallMs: 10 * MINUTE_MS, tokens: 1000 },
          endStatus: "cut_off",
        }),
        // The re-run after a restart is its own record, in the same task.
        record("rerun", { threadId: "task-1", estimate: { wallMs: 10 * MINUTE_MS, tokens: 1000 } }),
        record("other", { threadId: "task-2", estimate: { wallMs: 20 * MINUTE_MS } }),
        record("unestimated", { threadId: "task-3" }),
        record("top", { threadId: "task-4", parent: null, estimate: { wallMs: MINUTE_MS } }),
      ],
      { toMs: NOW_MS },
    );
    assert.deepStrictEqual(
      summary.estimates.map((row) => [row.threadId, row.runs, row.actualWallMs, row.actualTokens]),
      [
        ["task-1", 2, 20 * MINUTE_MS, 2200],
        ["task-2", 1, 10 * MINUTE_MS, 1100],
      ],
    );
    assert.strictEqual(summary.medianWallRatio, median([2, 0.5]));
    assert.strictEqual(summary.medianTokenRatio, 2.2);
  });

  it("is empty, not failing, with no records", () => {
    const summary = summarizeRunRecords([], { toMs: NOW_MS });
    assert.strictEqual(summary.runs, 0);
    assert.strictEqual(summary.medianWallRatio, null);
    assert.include(formatRunRecordsSummary(summary), "0 recorded runs");
  });

  it("formats Markdown tables", () => {
    const text = formatRunRecordsSummary(
      summarizeRunRecords([record("a", { kind: "a|b", estimate: { wallMs: 5 * MINUTE_MS } })], {
        toMs: NOW_MS,
        rates,
      }),
    );
    assert.include(text, "| a/b | 1 | 0 | 0 | 10.0m | 1.1k | 1.1k | $0.00 | 0 |");
    assert.include(text, "List price = public per-token price, not what a plan bills.");
    assert.include(text, "time 2.00x, tokens -");
    assert.include(text, "| a/b | 1 | 5.0m | 10.0m | - | 1.1k |");
  });
});

describe("runSummaryCli", () => {
  const files = (entries: Record<string, string>) => (path: string) => entries[path] ?? null;
  const io = (entries: Record<string, string>) => ({
    readLines: (path: string) => entries[path]?.split("\n") ?? null,
    readFile: files(entries),
    env: {},
    homeDir: "/home/someone",
    nowMs: NOW_MS,
  });
  const lines = (...records: ReadonlyArray<RunRecord>) =>
    records.map((entry) => JSON.stringify(entry)).join("\n");

  it("reads the state folder's records, more files from linked machines, and the saved rates", () => {
    const output = runSummaryCli(["--state-dir", "/state", "--json", "/far/runs.jsonl"], {
      ...io({
        "/state/run-records/runs.jsonl": lines(record("here")),
        "/far/runs.jsonl": lines(record("there"), record("here")),
        "/state/usage-model-rates.json": JSON.stringify({
          fetchedAtMs: 1,
          document: {
            "claude-opus-5-5": { input_cost_per_token: 0.000001, output_cost_per_token: 0.00001 },
          },
        }),
      }),
    });
    const summary = JSON.parse(output) as { runs: number; byKind: Array<{ listCostUsd: number }> };
    assert.strictEqual(summary.runs, 2);
    assert.closeTo(summary.byKind[0]!.listCostUsd, 0.004, 1e-9);
  });

  it("defaults to the T3 home's userdata and the last seven days", () => {
    const output = runSummaryCli([], {
      ...io({ "/t3home/userdata/run-records/runs.jsonl": lines(record("here")) }),
      env: { T3CODE_HOME: "/t3home" },
    });
    assert.include(output, "1 recorded runs");
    assert.include(output, "## Agent runs 2026-11-13 to 2026-11-20");
  });

  it("ends the window after the --to day and takes --days", () => {
    const old = record("old", { endedAt: "2026-11-01T10:00:00.000Z" });
    const read = io({ "/home/someone/.t3/userdata/run-records/runs.jsonl": lines(old) });
    assert.include(runSummaryCli(["--to", "2026-11-01", "--days", "1"], read), "1 recorded runs");
    assert.include(runSummaryCli(["--to", "2026-10-31"], read), "0 recorded runs");
  });

  it("summarizes nothing when no records exist yet: old runs without records are absent", () => {
    assert.include(runSummaryCli([], io({})), "0 recorded runs");
  });

  it("fails, rather than print 0 runs, when a records file cannot be read or is missing", () => {
    const failing = {
      ...io({}),
      readLines: (): Iterable<string> => {
        throw new Error("EIO: i/o error, read");
      },
    };
    assert.throws(() => runSummaryCli([], failing), /EIO/);
    // A file named on the command line must exist; only the state folder's may be missing.
    assert.throws(() => runSummaryCli(["/far/runs.jsonl"], io({})), /no such file/);
  });

  it("refuses unknown options and bad values", () => {
    assert.throws(() => runSummaryCli(["--weeks", "2"], io({})), /unknown option/);
    assert.throws(() => runSummaryCli(["--days", "0"], io({})), /positive/);
    assert.throws(() => runSummaryCli(["--to", "soon"], io({})), /date/);
    assert.throws(() => runSummaryCli(["--days"], io({})), /needs a value/);
  });
});

describe("reading real files", () => {
  const made: Array<string> = [];
  const tempDir = () => {
    const dir = NodeFS.mkdtempSync(NodePath.join(NodeOS.tmpdir(), "run-records-cli-"));
    made.push(dir);
    return dir;
  };
  afterEach(() => {
    for (const dir of made.splice(0)) NodeFS.rmSync(dir, { recursive: true, force: true });
  });
  const lines = (...records: ReadonlyArray<RunRecord>) =>
    records.map((entry) => `${JSON.stringify(entry)}\n`).join("");

  it("streams lines across chunks, multi-byte text and a torn last line included", () => {
    const dir = tempDir();
    const file = NodePath.join(dir, "runs.jsonl");
    const many = Array.from({ length: 200 }, (_, index) =>
      record(`run-${index}`, { threadTitle: `Prüfung ✓ ${index}` }),
    );
    NodeFS.writeFileSync(file, `${lines(...many)}{"v":1,"runId":"torn","endedAt":"2026`);
    // Chunks far smaller than a line: every line and every multi-byte letter is split somewhere.
    const read = [...readFileLines(file, 7)!];
    assert.strictEqual(read.length, 201);
    assert.strictEqual(read[0], JSON.stringify(many[0]));
    assert.strictEqual(read[199], JSON.stringify(many[199]));
    assert.strictEqual(read[200], '{"v":1,"runId":"torn","endedAt":"2026');
    assert.strictEqual(readFileLines(NodePath.join(dir, "missing.jsonl")), null);
  });

  it("skips a line too long to be a record and reads on", () => {
    const dir = tempDir();
    const file = NodePath.join(dir, "runs.jsonl");
    NodeFS.writeFileSync(file, `${"x".repeat(1.5 * 2 ** 20)}\n${lines(record("after"))}`);
    assert.deepStrictEqual([...readFileLines(file, 4096)!], [JSON.stringify(record("after"))]);
  });

  it("summarizes the state folder's file and fails on one it cannot read", () => {
    const dir = tempDir();
    NodeFS.mkdirSync(NodePath.join(dir, "run-records"));
    const own = NodePath.join(dir, "run-records", "runs.jsonl");
    NodeFS.writeFileSync(own, lines(record("here")));
    const real = { ...nodeSummaryIo(), nowMs: NOW_MS };
    assert.include(runSummaryCli(["--state-dir", dir], real), "1 recorded runs");
    // No records yet: a fresh state folder.
    assert.include(runSummaryCli(["--state-dir", tempDir()], real), "0 recorded runs");
    // Unreadable (here a folder in its place): an error and a failing exit, never "0 recorded runs".
    NodeFS.rmSync(own);
    NodeFS.mkdirSync(own);
    assert.throws(() => runSummaryCli(["--state-dir", dir], real), /EISDIR/);
  });
});
