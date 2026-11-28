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
  /**
   * The estimate tag's text when it could not be read (then `estimate` is null): a refused tag is
   * kept, never guessed at. Absent in records written before this field.
   */
  readonly estimateRefused?: string | null;
  /**
   * The kind tag's text when it was refused (empty, or never closed); then `kind` is null, not the
   * task title. Absent in records written before this field.
   */
  readonly kindRefused?: string | null;
  /** The run this one continues after a restart, a Continue, or a usage-limit reset. */
  readonly continuationOf: string | null;
  /**
   * The lane the run worked for (issue 1156): a lane thread's run, or one of its workers'. Absent in
   * records written before this field, and not written yet; read it with {@link runRecordLane}.
   */
  readonly lane?: string | null;
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
/**
 * Longest kind or estimate text kept, in characters a person sees (graphemes); a longer kind is cut,
 * a longer estimate refused.
 */
export const MAX_TAG_CHARS = 80;
/**
 * Every kind or estimate tag in a text, up to its `]`, any length: one pass over the line, no
 * backtracking. One without its `]` (on the same line, before the next `[`) is read too, so that it
 * can be refused, not lost; the next `[` ends it, so an unclosed mention (`see the [run-kind:
 * format`) never swallows a later tag on its line. So a tag's text cannot hold `[` or `]`.
 */
const KIND_TAGS = /\[run-kind:([^\][\n]*)(\]?)/giu;
const ESTIMATE_TAGS = /\[run-estimate:([^\][\n]*)(\]?)/giu;
/**
 * The tag that decides: the first closed tag that `fits`, passing over unclosed openers (and tags
 * that do not fit), which are mentions of the tag rather than the tag. With none, the first opener,
 * to be refused. Null when the text has no tag at all.
 */
function decidingTag(
  text: string,
  tags: RegExp,
  fits: (tag: string) => boolean,
): { readonly text: string; readonly closed: boolean } | null {
  let first: string | null = null;
  for (const match of text.matchAll(tags)) {
    const tag = match[1]!.trim();
    if (match[2] === "]" && fits(tag)) return { text: tag, closed: true };
    first ??= tag;
  }
  return first === null ? null : { text: first, closed: false };
}
const GRAPHEMES = new Intl.Segmenter(undefined, { granularity: "grapheme" });
/**
 * The text's first `MAX_TAG_CHARS` characters, or null when it has no more than that. Characters
 * are what a person sees (an emoji, a letter with its accent), never half of one.
 */
const overTagLimit = (text: string): string | null => {
  // Never more characters than UTF-16 units: a short text needs no segmenting.
  if (text.length <= MAX_TAG_CHARS) return null;
  let count = 0;
  // Stops at the first character past the limit, so a long text costs no more than a short one.
  for (const { index } of GRAPHEMES.segment(text)) {
    if (count === MAX_TAG_CHARS) return text.slice(0, index);
    count += 1;
  }
  return null;
};
/** Cuts a long tag text to `MAX_TAG_CHARS` characters, marking the cut with `…`. */
const capTagText = (text: string) => {
  const cut = overTagLimit(text);
  return cut === null ? text : `${cut}…`;
};
const DURATION_UNITS: Readonly<Record<string, number>> = {
  h: 3_600_000,
  hr: 3_600_000,
  hrs: 3_600_000,
  hour: 3_600_000,
  hours: 3_600_000,
  m: 60_000,
  min: 60_000,
  mins: 60_000,
  minute: 60_000,
  minutes: 60_000,
  s: 1000,
  sec: 1000,
  secs: 1000,
  second: 1000,
  seconds: 1000,
};
/** Scales a token count takes; only before the word "tokens", so `2m` alone stays two minutes. */
const TOKEN_SCALES: Readonly<Record<string, number>> = {
  "": 1,
  k: 1e3,
  thousand: 1e3,
  m: 1e6,
  mil: 1e6,
  million: 1e6,
  b: 1e9,
  billion: 1e9,
};
const TOKEN_WORDS = ["tok", "toks", "token", "tokens"] as const;
/** `2ktokens`, `2Mtokens`, `300ktok`: a scale written right before the word "tokens". */
const tokenWordAtEnd = (unit: string) => TOKEN_WORDS.find((word) => unit.endsWith(word));
/** Joins parts: spaces, commas, `;`, `+`, `~` (about) and the word "and". */
const ESTIMATE_GAP = /(?:[\s,;+~]|and(?![a-z]))*/iuy;
/** A number (`2`, `2.5`, `1e6`, `1,500,000`), its unit, and the word "tokens" if one follows. */
const ESTIMATE_PART =
  /(\d{1,3}(?:,\d{3})+(?![\d.])|\d+(?:\.\d+)?(?:e\+?\d+)?)\s*([a-z]*)(?:\s*(tokens?|toks?)(?![a-z]))?(?![a-z])/iuy;

/** What a `[run-estimate: ...]` tag says: the estimate, or the tag text when it could not be read. */
export interface EstimateTag {
  readonly estimate: RunEstimate | null;
  /** The tag's text when any part of it was not a time or a token count. Never a guess. */
  readonly refused: string | null;
}

/**
 * Reads a tag's text: times (`1h30m`, `1h 30m`, `90 min`, `1.5h`, `45s`) and one token count
 * (`2M tokens`, `2.5m tokens`, `300k tokens`, `1e6 tokens`, `1,500,000 tokens`). Anything else in the
 * text (a range, a lone `2M`, an unknown unit, two token counts, a time unit twice) refuses the whole
 * tag rather than guessing.
 */
function readEstimateText(text: string): RunEstimate | null {
  let at = 0;
  let wallMs = 0;
  let tokens: number | null = null;
  const timeUnits = new Set<number>();
  const skipGap = () => {
    ESTIMATE_GAP.lastIndex = at;
    ESTIMATE_GAP.exec(text);
    at = ESTIMATE_GAP.lastIndex;
  };
  skipGap();
  while (at < text.length) {
    ESTIMATE_PART.lastIndex = at;
    const part = ESTIMATE_PART.exec(text);
    if (part === null) return null;
    at = ESTIMATE_PART.lastIndex;
    const value = Number(part[1]!.replaceAll(",", ""));
    const unit = part[2]!;
    const lower = unit.toLowerCase();
    if (!Number.isFinite(value)) return null;
    const joinedWord = tokenWordAtEnd(lower);
    if (part[3] !== undefined || joinedWord !== undefined) {
      // `2M tokens` (the word apart) or `2Mtokens` (joined); never both, as in `2 tokens tokens`.
      const scale =
        part[3] !== undefined
          ? joinedWord === undefined
            ? TOKEN_SCALES[lower]
            : undefined
          : TOKEN_SCALES[lower.slice(0, lower.length - joinedWord!.length)];
      if (scale === undefined || tokens !== null) return null;
      tokens = value * scale;
    } else {
      const unitMs = DURATION_UNITS[lower];
      // A capital `M` alone reads as "million" as easily as "minutes": refused.
      if (unitMs === undefined || unit === "M" || timeUnits.has(unitMs)) return null;
      timeUnits.add(unitMs);
      wallMs += value * unitMs;
    }
    skipGap();
  }
  const estimate: { wallMs?: number; tokens?: number } = {};
  if (timeUnits.size > 0) estimate.wallMs = Math.round(wallMs);
  if (tokens !== null) estimate.tokens = Math.round(tokens);
  const safe = Object.values(estimate).every((n) => Number.isSafeInteger(n) && n > 0);
  return safe && Object.keys(estimate).length > 0 ? estimate : null;
}

/**
 * `[run-estimate: 1h 30m 2.5M tokens]` in a text, null when there is none. The first closed tag
 * that fits `MAX_TAG_CHARS` decides; a later one never replaces it, so a typo is refused, not hidden.
 * Empty or unreadable, it is refused; with no such tag, the first (unclosed or too long) is.
 */
export function readEstimateTag(text: string): EstimateTag | null {
  const tag = decidingTag(text, ESTIMATE_TAGS, (candidate) => overTagLimit(candidate) === null);
  if (tag === null) return null;
  const estimate = tag.closed && tag.text !== "" ? readEstimateText(tag.text) : null;
  return estimate !== null
    ? { estimate, refused: null }
    : { estimate: null, refused: capTagText(tag.text) };
}

/** The estimate in a text, or null when there is none or it was refused. */
export function parseEstimate(text: string): RunEstimate | null {
  return readEstimateTag(text)?.estimate ?? null;
}

/**
 * `[run-kind: ...]` in a text, cut to `MAX_TAG_CHARS`; null when there is none. The first closed tag
 * decides, as for the estimate. It is refused when it is empty; with no closed tag, the first
 * unclosed one is refused (`[run-kind: lens [x]]`, whose `[` ends the tag).
 */
function readKindTag(
  text: string,
): { readonly kind: string | null; readonly refused: string | null } | null {
  const tag = decidingTag(text, KIND_TAGS, () => true);
  if (tag === null) return null;
  return tag.closed && tag.text !== ""
    ? { kind: capTagText(tag.text), refused: null }
    : { kind: null, refused: capTagText(tag.text) };
}

/** The labels a parent puts in a delegated task's text. */
export function parseTaskLabels(text: string | null): {
  readonly role: string | null;
  readonly kind: string | null;
  /** The kind tag's text when it was refused; see `readKindTag`. */
  readonly kindRefused: string | null;
  readonly estimate: RunEstimate | null;
  /** The estimate tag's text when it could not be read; see `readEstimateTag`. */
  readonly estimateRefused: string | null;
} {
  if (text === null) {
    return { role: null, kind: null, kindRefused: null, estimate: null, estimateRefused: null };
  }
  const kind = readKindTag(text);
  const estimate = readEstimateTag(text);
  return {
    role: ROLE_PREFIX.exec(text.trimStart())?.[1]?.trim() ?? null,
    kind: kind?.kind ?? null,
    kindRefused: kind?.refused ?? null,
    estimate: estimate?.estimate ?? null,
    estimateRefused: estimate?.refused ?? null,
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
  // The first text that has a tag decides; a refused tag is never replaced by a guess.
  const kindFrom = fromTask.kind !== null || fromTask.kindRefused !== null ? fromTask : fromRun;
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
      kindFrom.kind ??
      (kindFrom.kindRefused === null && delegated
        ? (task?.title ?? (task === null ? thread.title : null))
        : null),
    kindRefused: kindFrom.kindRefused,
    ...(fromTask.estimate !== null || fromTask.estimateRefused !== null
      ? { estimate: fromTask.estimate, estimateRefused: fromTask.estimateRefused }
      : { estimate: fromRun.estimate, estimateRefused: fromRun.estimateRefused }),
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
 * The records in a `runs.jsonl` text, or in its lines as a reader streams them: each run once (the
 * first line wins), lines of an unknown version and torn or foreign lines skipped. `keep` drops
 * records as they are read (a summary's window), so a large file never sits in memory whole.
 */
export function parseRunRecordLines(
  text: string | Iterable<string>,
  keep: (record: RunRecord) => boolean = () => true,
): ReadonlyArray<RunRecord> {
  const seen = new Set<string>();
  const records: Array<RunRecord> = [];
  for (const line of typeof text === "string" ? text.split("\n") : text) {
    if (line.trim() === "") continue;
    let value: unknown;
    try {
      value = JSON.parse(line);
    } catch {
      continue;
    }
    if (!isRunRecord(value) || !keep(value) || seen.has(value.runId)) continue;
    seen.add(value.runId);
    records.push(value);
  }
  return records;
}

/**
 * A record's lane, or null: for a record written before the field, a run outside any lane, or a
 * value that is not a name. A lane no longer in the settings is kept as written.
 */
export function runRecordLane(record: RunRecord): string | null {
  const lane: unknown = record.lane;
  return typeof lane === "string" && lane.trim() !== "" ? lane : null;
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
