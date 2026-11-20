// @effect-diagnostics globalDate:off - pure and free of the Effect runtime: summaryCli.ts runs it under plain node.
/**
 * Fork: the weekly summary of run records (issue 315): runs per kind, role and model, the median
 * wall time and tokens, cut-off runs, and estimate against actual per delegated task. Pure and free
 * of runtime imports, like `runRecord.ts`, so `summaryCli.ts` runs it under plain `node`. Only
 * recorded runs count: runs that ended before recording began have no record and are absent.
 */
import { priceUsage, type RateTable } from "../../usage/usagePricing.ts";
import type { RunRecord } from "./runRecord.ts";

export const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

export interface GroupSummary {
  readonly key: string;
  readonly runs: number;
  readonly cutOff: number;
  readonly failed: number;
  readonly medianWallMs: number;
  readonly totalWallMs: number;
  /** Input (cache included) plus output, per run. */
  readonly medianTokens: number;
  readonly totalTokens: number;
  /** Runs whose usage the provider reported only in part, or not at all. */
  readonly usageIncomplete: number;
  /**
   * At list price from the saved model rates, over the runs that have a rate; null without rates
   * or when no run had one. Not what a plan bills.
   */
  readonly listCostUsd: number | null;
  /** Runs left out of `listCostUsd`: their model has no saved rate. */
  readonly unpricedRuns: number;
}

export interface EstimateRow {
  /** The delegated task's thread: all its runs (continuations too) count as one task. */
  readonly threadId: string;
  readonly kind: string | null;
  readonly estimate: { readonly wallMs?: number; readonly tokens?: number };
  readonly actualWallMs: number;
  readonly actualTokens: number;
  readonly runs: number;
}

export interface RunRecordsSummary {
  readonly fromMs: number;
  readonly toMs: number;
  readonly runs: number;
  readonly byKind: ReadonlyArray<GroupSummary>;
  readonly byRole: ReadonlyArray<GroupSummary>;
  readonly byModel: ReadonlyArray<GroupSummary>;
  readonly estimates: ReadonlyArray<EstimateRow>;
  /** Median of actual / estimate over the tasks that gave one; null when none did. */
  readonly medianWallRatio: number | null;
  readonly medianTokenRatio: number | null;
  /** Runs whose `[run-estimate: ...]` tag could not be read; they count as giving none. */
  readonly refusedEstimates: number;
  /** Runs whose `[run-kind: ...]` tag was refused (empty or not closed); they count as no kind. */
  readonly refusedKinds: number;
}

const runTokens = (record: RunRecord) => record.usage.inputTokens + record.usage.outputTokens;

export function median(values: ReadonlyArray<number>): number {
  if (values.length === 0) return 0;
  const sorted = values.toSorted((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1 ? sorted[middle]! : (sorted[middle - 1]! + sorted[middle]!) / 2;
}

/**
 * The model a run is priced at: the one the provider reported running when it reported exactly one,
 * else the requested model (with a fallback model too, the requested one did most of the work).
 */
export function pricedModel(record: RunRecord): string {
  return record.executedModels.length === 1 ? record.executedModels[0]! : record.model;
}

/** List price of one run's tokens, or null when the model has no rate. */
function listCostUsd(record: RunRecord, rates: RateTable): number | null {
  const { usage } = record;
  const priced = priceUsage(rates, {
    model: pricedModel(record),
    totals: {
      uncachedInputTokens: Math.max(
        0,
        usage.inputTokens - usage.cachedInputTokens - usage.cacheCreationTokens,
      ),
      cachedInputTokens: usage.cachedInputTokens,
      cacheCreationTokens: usage.cacheCreationTokens,
      outputTokens: usage.outputTokens,
      reasoningTokens: usage.reasoningTokens,
    },
    speed: "standard",
    reportedCostUsd: null,
  });
  return priced.costSource === "unpriced" ? null : priced.costUsd;
}

function group(
  records: ReadonlyArray<RunRecord>,
  keyOf: (record: RunRecord) => string,
  rates: RateTable | null,
): ReadonlyArray<GroupSummary> {
  const groups = new Map<string, Array<RunRecord>>();
  for (const record of records) {
    const key = keyOf(record);
    const members = groups.get(key);
    if (members === undefined) groups.set(key, [record]);
    else members.push(record);
  }
  return [...groups.entries()]
    .map(([key, members]) => {
      const costs = rates === null ? [] : members.map((record) => listCostUsd(record, rates));
      const priced = costs.filter((cost): cost is number => cost !== null);
      return {
        key,
        runs: members.length,
        cutOff: members.filter((record) => record.endStatus === "cut_off").length,
        failed: members.filter((record) => record.endStatus === "failed").length,
        medianWallMs: median(members.map((record) => record.wallMs)),
        totalWallMs: members.reduce((sum, record) => sum + record.wallMs, 0),
        medianTokens: median(members.map(runTokens)),
        totalTokens: members.reduce((sum, record) => sum + runTokens(record), 0),
        usageIncomplete: members.filter((record) => record.usage.status !== "complete").length,
        listCostUsd: priced.length === 0 ? null : priced.reduce((sum, cost) => sum + cost, 0),
        unpricedRuns: rates === null ? members.length : costs.length - priced.length,
      };
    })
    .toSorted((left, right) => right.runs - left.runs || left.key.localeCompare(right.key));
}

const NONE = "(none)";

export function summarizeRunRecords(
  records: ReadonlyArray<RunRecord>,
  options: { readonly toMs: number; readonly fromMs?: number; readonly rates?: RateTable | null },
): RunRecordsSummary {
  const fromMs = options.fromMs ?? options.toMs - WEEK_MS;
  const rates = options.rates ?? null;
  const seen = new Set<string>();
  const inWindow = records.filter((record) => {
    const ended = Date.parse(record.endedAt);
    if (!(ended >= fromMs && ended < options.toMs) || seen.has(record.runId)) return false;
    seen.add(record.runId);
    return true;
  });
  const tasks = new Map<string, Array<RunRecord>>();
  for (const record of inWindow) {
    if (record.parent === null) continue;
    const runs = tasks.get(record.threadId);
    if (runs === undefined) tasks.set(record.threadId, [record]);
    else runs.push(record);
  }
  const estimates: Array<EstimateRow> = [];
  for (const [threadId, runs] of tasks) {
    const estimate = runs.find((record) => record.estimate !== null)?.estimate;
    if (estimate === undefined || estimate === null) continue;
    estimates.push({
      threadId,
      kind: runs.find((record) => record.kind !== null)?.kind ?? null,
      estimate,
      actualWallMs: runs.reduce((sum, record) => sum + record.wallMs, 0),
      actualTokens: runs.reduce((sum, record) => sum + runTokens(record), 0),
      runs: runs.length,
    });
  }
  const ratios = (pick: (row: EstimateRow) => readonly [number, number | undefined]) => {
    const values = estimates.flatMap((row) => {
      const [actual, estimated] = pick(row);
      return estimated === undefined || estimated <= 0 ? [] : [actual / estimated];
    });
    return values.length === 0 ? null : median(values);
  };
  return {
    fromMs,
    toMs: options.toMs,
    runs: inWindow.length,
    byKind: group(inWindow, (record) => record.kind ?? NONE, rates),
    byRole: group(inWindow, (record) => record.role ?? NONE, rates),
    byModel: group(
      inWindow,
      (record) => `${record.model}${record.effort === null ? "" : ` (${record.effort})`}`,
      rates,
    ),
    estimates,
    medianWallRatio: ratios((row) => [row.actualWallMs, row.estimate.wallMs]),
    medianTokenRatio: ratios((row) => [row.actualTokens, row.estimate.tokens]),
    refusedEstimates: inWindow.filter((record) => (record.estimateRefused ?? null) !== null).length,
    refusedKinds: inWindow.filter((record) => (record.kindRefused ?? null) !== null).length,
  };
}

const minutes = (ms: number) => `${(ms / 60_000).toFixed(1)}m`;
const tokens = (count: number) =>
  count >= 1e6
    ? `${(count / 1e6).toFixed(2)}M`
    : count >= 1e3
      ? `${(count / 1e3).toFixed(1)}k`
      : `${count}`;

/** `-` when no run had a rate; `+N unpriced` when some had none, so a low sum says so. */
const listPrice = (row: GroupSummary) =>
  row.listCostUsd === null
    ? "-"
    : `$${row.listCostUsd.toFixed(2)}${row.unpricedRuns > 0 ? ` (+${row.unpricedRuns} unpriced)` : ""}`;

function table(title: string, groups: ReadonlyArray<GroupSummary>): string {
  const rows = groups.map(
    (row) =>
      `| ${row.key.replaceAll("|", "/")} | ${row.runs} | ${row.cutOff} | ${row.failed} | ${minutes(row.medianWallMs)} | ${tokens(row.medianTokens)} | ${tokens(row.totalTokens)} | ${listPrice(row)} | ${row.usageIncomplete} |`,
  );
  return [
    `### ${title}`,
    "",
    "| | runs | cut off | failed | median time | median tokens | tokens | list price | usage incomplete |",
    "|---|---|---|---|---|---|---|---|---|",
    ...rows,
  ].join("\n");
}

const day = (ms: number) => new Date(ms).toISOString().slice(0, 10);

/** The summary as Markdown, for a terminal or an issue comment. */
export function formatRunRecordsSummary(summary: RunRecordsSummary): string {
  const ratio = (value: number | null) => (value === null ? "-" : `${value.toFixed(2)}x`);
  const estimateRows = summary.estimates.map(
    (row) =>
      `| ${(row.kind ?? NONE).replaceAll("|", "/")} | ${row.runs} | ${row.estimate.wallMs === undefined ? "-" : minutes(row.estimate.wallMs)} | ${minutes(row.actualWallMs)} | ${row.estimate.tokens === undefined ? "-" : tokens(row.estimate.tokens)} | ${tokens(row.actualTokens)} |`,
  );
  return [
    // The window's end is exclusive: name the last day it includes (`--to 2026-11-20` ends 11-21 00:00).
    `## Agent runs ${day(summary.fromMs)} to ${day(summary.toMs - 1)}`,
    "",
    `${summary.runs} recorded runs. Tokens are input (cache included) plus output, as providers report them.`,
    "List price = public per-token price, not what a plan bills.",
    "",
    table("By kind", summary.byKind),
    ...(summary.refusedKinds === 0
      ? []
      : [
          "",
          `${summary.refusedKinds} runs had a kind tag that was refused (empty or not closed); they count as ${NONE}.`,
        ]),
    "",
    table("By role", summary.byRole),
    "",
    table("By model", summary.byModel),
    "",
    "### Estimate vs actual (delegated tasks that gave one)",
    "",
    `Median actual/estimate: time ${ratio(summary.medianWallRatio)}, tokens ${ratio(summary.medianTokenRatio)}.`,
    ...(summary.refusedEstimates === 0
      ? []
      : [
          `${summary.refusedEstimates} runs had an estimate tag that could not be read (not a time or a token count); they count as giving none.`,
        ]),
    ...(estimateRows.length === 0
      ? []
      : [
          "",
          "| kind | runs | est. time | time | est. tokens | tokens |",
          "|---|---|---|---|---|---|",
          ...estimateRows,
        ]),
    "",
  ].join("\n");
}
