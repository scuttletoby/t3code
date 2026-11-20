// @effect-diagnostics globalDate:off - pure and free of the Effect runtime: summaryCli.ts runs it under plain node.
/**
 * Fork: one record per agent run that ended (issue 315). Pure: `buildRunRecord` turns the run and
 * the records the projection already keeps (its provider turns' token usage, its thread, the parent's
 * task) into one line of `runs.jsonl`; `parseRunRecordLines` reads the lines back. No Effect runtime
 * and only type imports, so `summaryCli.ts` runs it under plain `node`.
 */
import type {
  OrchestrationV2Run,
  OrchestrationV2Subagent,
  TurnTokenUsage,
} from "@t3tools/contracts";

/** Bumped only when a field changes meaning; readers skip versions they do not know. */
const RUN_RECORD_VERSION = 1;

/**
 * - `completed`: the run ended normally with a final answer.
 * - `cut_off`: it started but stopped half-way: stopped, interrupted (a restart), or ended
 *   without a final answer. Its usage is whatever the provider reported until then.
 * - `failed`: the provider or the app failed it.
 */
export type RunEndStatus = "completed" | "cut_off" | "failed";

export interface RunUsage {
  /** `complete` when every provider turn reported usage, `unavailable` when none did. */
  readonly status: "complete" | "partial" | "unavailable";
  readonly providerTurns: number;
  /** All prompt tokens, cache reads and writes included (as providers report them). */
  readonly inputTokens: number;
  readonly cachedInputTokens: number;
  readonly cacheCreationTokens: number;
  /** Includes `reasoningTokens`. */
  readonly outputTokens: number;
  readonly reasoningTokens: number;
  /** Provider-ledger work beyond the main agent (native subagents, compaction). */
  readonly otherInputTokens: number;
  readonly otherOutputTokens: number;
  readonly hasSubagents: boolean;
}

export interface RunEstimate {
  readonly wallMs?: number;
  readonly tokens?: number;
}

export interface RunRecord {
  readonly v: typeof RUN_RECORD_VERSION;
  readonly runId: string;
  readonly threadId: string;
  readonly threadTitle: string;
  readonly projectId: string;
  readonly projectTitle: string | null;
  /** The thread that delegated this one, here or (for a task from a linked machine) there. */
  readonly parent: {
    readonly threadId: string;
    readonly taskId: string | null;
    readonly environmentId: string | null;
  } | null;
  /** The delegate call's role (`Act as the <role> sub-agent`), when it gave one. */
  readonly role: string | null;
  /** `[run-kind: ...]` in the task, else the delegated task's title; null for other threads. */
  readonly kind: string | null;
  /** `[run-estimate: ...]` in the task: the parent's guess before it ran. */
  readonly estimate: RunEstimate | null;
  /** The run this one continues after a restart, a Continue, or a usage-limit reset. */
  readonly continuationOf: string | null;
  readonly providerInstanceId: string;
  readonly model: string;
  readonly effort: string | null;
  readonly modelOptions: Readonly<Record<string, string | number | boolean>>;
  /** Models the provider reported running, which can differ from `model`. */
  readonly executedModels: ReadonlyArray<string>;
  readonly requestedAt: string;
  readonly startedAt: string;
  readonly endedAt: string;
  readonly wallMs: number;
  readonly queuedMs: number;
  /** The run's own status: completed, interrupted, cancelled or failed. */
  readonly status: string;
  readonly endStatus: RunEndStatus;
  readonly finalMessage: boolean;
  readonly usage: RunUsage;
}

/** Run statuses after which a run never works again. `rolled_back` follows `completed`. */
export const TERMINAL_RUN_STATUSES: ReadonlySet<OrchestrationV2Run["status"]> = new Set([
  "completed",
  "interrupted",
  "cancelled",
  "failed",
]);

const ROLE_PREFIX = /^Act as the ([^\n]{1,60}?) sub-agent for this task\./u;
const KIND_TAG = /\[run-kind:\s*([^\]\n]{1,80}?)\s*\]/iu;
const ESTIMATE_TAG = /\[run-estimate:\s*([^\]\n]{1,80}?)\s*\]/iu;
const DURATION = /(\d+(?:\.\d+)?)\s*(h|m|min|s)\b/giu;
const TOKENS = /(\d+(?:\.\d+)?)\s*([kKmM])?\s*tok(?:en)?s?\b/u;

const UNIT_MS: Readonly<Record<string, number>> = { h: 3_600_000, m: 60_000, min: 60_000, s: 1000 };
const TOKEN_SCALE: Readonly<Record<string, number>> = { k: 1e3, m: 1e6 };

/** `[run-estimate: 1h 30m 2.5M tokens]`: a wall time, a token count, or both. Null when neither. */
export function parseEstimate(text: string): RunEstimate | null {
  const tag = ESTIMATE_TAG.exec(text)?.[1];
  if (tag === undefined) return null;
  const tokens = TOKENS.exec(tag);
  const withoutTokens = tokens === null ? tag : tag.replace(tokens[0], " ");
  let wallMs = 0;
  for (const match of withoutTokens.matchAll(DURATION)) {
    wallMs += Number(match[1]) * (UNIT_MS[match[2]!.toLowerCase()] ?? 0);
  }
  const tokenCount =
    tokens === null ? 0 : Number(tokens[1]) * (TOKEN_SCALE[(tokens[2] ?? "").toLowerCase()] ?? 1);
  const estimate = {
    ...(wallMs > 0 ? { wallMs: Math.round(wallMs) } : {}),
    ...(tokenCount > 0 ? { tokens: Math.round(tokenCount) } : {}),
  };
  return Object.keys(estimate).length === 0 ? null : estimate;
}

/** The labels a parent puts in a delegated task's text. */
export function parseTaskLabels(text: string | null): {
  readonly role: string | null;
  readonly kind: string | null;
  readonly estimate: RunEstimate | null;
} {
  if (text === null) return { role: null, kind: null, estimate: null };
  return {
    role: ROLE_PREFIX.exec(text.trimStart())?.[1]?.trim() ?? null,
    kind: KIND_TAG.exec(text)?.[1] ?? null,
    estimate: parseEstimate(text),
  };
}

type OptionValue = string | number | boolean;

function modelOptions(run: OrchestrationV2Run): Record<string, OptionValue> {
  const options: Record<string, OptionValue> = {};
  for (const option of run.modelSelection.options ?? []) {
    const value: unknown = option.value;
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      options[option.id] = value;
    }
  }
  return options;
}

const EFFORT_OPTION_IDS = ["effort", "reasoningEffort", "reasoning_effort"] as const;

export interface ProviderTurnUsageInput {
  readonly turnTokenUsage?: TurnTokenUsage | undefined;
  readonly model?: string | undefined;
}

/** Sums the provider turns of one run. A turn without usage makes the sum `partial`. */
export function sumRunUsage(turns: ReadonlyArray<ProviderTurnUsageInput>): {
  readonly usage: RunUsage;
  readonly executedModels: ReadonlyArray<string>;
} {
  const models = new Set<string>();
  let reported = 0;
  let complete = 0;
  const totals = {
    inputTokens: 0,
    cachedInputTokens: 0,
    cacheCreationTokens: 0,
    outputTokens: 0,
    reasoningTokens: 0,
    otherInputTokens: 0,
    otherOutputTokens: 0,
  };
  let hasSubagents = false;
  for (const turn of turns) {
    if (turn.model !== undefined) models.add(turn.model);
    const usage = turn.turnTokenUsage;
    if (usage === undefined || usage.localCommand === true) continue;
    for (const model of usage.models ?? []) models.add(model);
    if (usage.usageStatus === "unavailable") continue;
    reported += 1;
    if (usage.usageStatus === "complete") complete += 1;
    totals.inputTokens += usage.inputTokens ?? 0;
    totals.cachedInputTokens += usage.cachedInputTokens ?? 0;
    totals.cacheCreationTokens += usage.cacheCreationTokens ?? 0;
    totals.outputTokens += usage.outputTokens ?? 0;
    totals.reasoningTokens += usage.reasoningTokens ?? 0;
    totals.otherInputTokens += usage.otherUsage?.inputTokens ?? 0;
    totals.otherOutputTokens += usage.otherUsage?.outputTokens ?? 0;
    hasSubagents ||= usage.hasSubagents;
  }
  const status =
    reported === 0
      ? "unavailable"
      : complete === turns.filter((turn) => turn.turnTokenUsage?.localCommand !== true).length
        ? "complete"
        : "partial";
  return {
    usage: { status, providerTurns: turns.length, ...totals, hasSubagents },
    executedModels: [...models].toSorted(),
  };
}

function endStatusOf(status: OrchestrationV2Run["status"], finalMessage: boolean): RunEndStatus {
  if (status === "failed") return "failed";
  return status === "completed" && finalMessage ? "completed" : "cut_off";
}

export interface RunRecordInput {
  readonly run: OrchestrationV2Run;
  readonly thread: {
    readonly title: string;
    readonly projectId: string;
    readonly parentThreadId: string | null;
    readonly delegatedFrom: { readonly environmentId: string; readonly threadId: string } | null;
  };
  readonly projectTitle: string | null;
  /** The parent's record of this thread's task, when a thread here delegated it. */
  readonly task: Pick<OrchestrationV2Subagent, "id" | "prompt" | "title"> | null;
  /** The text of the message that started this run. */
  readonly runMessage: string | null;
  readonly finalMessage: boolean;
  readonly providerTurns: ReadonlyArray<ProviderTurnUsageInput>;
  /** Epoch millis of `run.requestedAt`, `run.startedAt` and `run.completedAt`. */
  readonly times: {
    readonly requestedMs: number;
    readonly startedMs: number;
    readonly endedMs: number;
  };
}

/** One record, or null for a run that never ended or never started (it did no work). */
export function buildRunRecord(input: RunRecordInput): RunRecord | null {
  const { run, thread, task } = input;
  if (!TERMINAL_RUN_STATUSES.has(run.status) || run.startedAt === null) return null;
  // The task's own text first: a later run in the child thread keeps the task's labels.
  const fromTask = parseTaskLabels(task?.prompt ?? null);
  const fromRun = parseTaskLabels(input.runMessage);
  const options = modelOptions(run);
  const effort = EFFORT_OPTION_IDS.map((id) => options[id]).find((value) => value !== undefined);
  const { usage, executedModels } = sumRunUsage(input.providerTurns);
  const parent =
    thread.delegatedFrom !== null
      ? {
          threadId: thread.delegatedFrom.threadId,
          taskId: null,
          environmentId: thread.delegatedFrom.environmentId,
        }
      : thread.parentThreadId !== null
        ? { threadId: thread.parentThreadId, taskId: task?.id ?? null, environmentId: null }
        : null;
  const delegated = parent !== null;
  const iso = (ms: number) => new Date(ms).toISOString();
  return {
    v: RUN_RECORD_VERSION,
    runId: run.id,
    threadId: run.threadId,
    threadTitle: thread.title,
    projectId: thread.projectId,
    projectTitle: input.projectTitle,
    parent,
    role: fromTask.role ?? fromRun.role,
    kind:
      fromTask.kind ??
      fromRun.kind ??
      (delegated ? (task?.title ?? (task === null ? thread.title : null)) : null),
    estimate: fromTask.estimate ?? fromRun.estimate,
    continuationOf:
      run.restartContinuationOfRunId ??
      run.manualContinuationOfRunId ??
      run.usageLimitContinuationOfRunId ??
      null,
    providerInstanceId: run.providerInstanceId,
    model: run.modelSelection.model,
    effort: effort === undefined ? null : String(effort),
    modelOptions: options,
    executedModels,
    requestedAt: iso(input.times.requestedMs),
    startedAt: iso(input.times.startedMs),
    endedAt: iso(input.times.endedMs),
    wallMs: Math.max(0, input.times.endedMs - input.times.startedMs),
    queuedMs: Math.max(0, input.times.startedMs - input.times.requestedMs),
    status: run.status,
    endStatus: endStatusOf(run.status, input.finalMessage),
    finalMessage: input.finalMessage,
    usage,
  };
}

/**
 * The records in a `runs.jsonl` text: each run once (the first line wins), lines of an unknown
 * version and torn or foreign lines skipped.
 */
export function parseRunRecordLines(text: string): ReadonlyArray<RunRecord> {
  const seen = new Set<string>();
  const records: Array<RunRecord> = [];
  for (const line of text.split("\n")) {
    if (line.trim() === "") continue;
    let value: unknown;
    try {
      value = JSON.parse(line);
    } catch {
      continue;
    }
    if (!isRunRecord(value) || seen.has(value.runId)) continue;
    seen.add(value.runId);
    records.push(value);
  }
  return records;
}

function isRunRecord(value: unknown): value is RunRecord {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Partial<RunRecord>;
  return (
    record.v === RUN_RECORD_VERSION &&
    typeof record.runId === "string" &&
    typeof record.endedAt === "string" &&
    typeof record.wallMs === "number" &&
    typeof record.usage === "object" &&
    record.usage !== null
  );
}
