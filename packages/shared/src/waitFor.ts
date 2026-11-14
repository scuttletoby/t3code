/**
 * Fork: pure parts of waits (`wait_for`): checking a request, the dedupe key, the judges, every
 * state change of a stored wait and the message texts. Nothing here reads a clock, a file or a
 * process; the wait service passes `now` and what it read.
 */
import { sha256 } from "@noble/hashes/sha2";
import type {
  PullRequestCheck,
  PullRequestCheckStatus,
  PullRequestState,
  ThreadId,
  WaitChecks,
  WaitForInput,
  WaitForSettings,
  WaitOutcome,
  WaitRecord,
  WaitResult,
  WaitState,
  WaitTarget,
  WaitToolFailureCode,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Hex from "effect/encoding/Hex";

/** A chain of gone or stopped threads ends here, so a loop cannot go round for ever. */
export const MAX_HAND_OVERS = 5;
/** Waits for one receiver go in one message, at most this many. */
const MAX_JOINED = 10;
const UNIT_NAME_PATTERN = /^[A-Za-z0-9@_.:][A-Za-z0-9@_.:-]{0,199}$/u;
const DETAIL_MAX = 300;

const MS_PER_SECOND = 1000;
const MS_PER_HOUR = 3_600_000;

export type WaitRefusal = {
  readonly ok: false;
  readonly code: Extract<WaitToolFailureCode, "invalid" | "unknown_check" | "not_supported">;
  readonly message: string;
};

export interface ValidateWaitSpecInput {
  readonly input: WaitForInput;
  /** The checks of the project the thread is in now. */
  readonly checks: WaitChecks;
  readonly platform: NodeJS.Platform;
  /** Provider instance ids a slot wait may name. */
  readonly accounts: ReadonlySet<string>;
}

const refuse = (code: WaitRefusal["code"], message: string): WaitRefusal => ({
  ok: false,
  code,
  message,
});

function isAbsolutePath(path: string, platform: NodeJS.Platform): boolean {
  if (platform === "win32") return /^(?:[A-Za-z]:[\\/]|\\\\)/u.test(path);
  return path.startsWith("/");
}

/** Checks a `wait_for` call and fills in every default, so equal conditions give equal targets. */
export function validateWaitSpec({
  input,
  checks,
  platform,
  accounts,
}: ValidateWaitSpecInput): { readonly ok: true; readonly target: WaitTarget } | WaitRefusal {
  const fields = {
    pr: input.pr,
    file: input.file,
    unit: input.unit,
    check: input.check,
    slot: input.slot,
  };
  const given = Object.entries(fields).filter(([, value]) => value !== undefined);
  const other = given.find(([kind]) => kind !== input.kind);
  if (other !== undefined) {
    return refuse(
      "invalid",
      `Give only "${input.kind}" for kind "${input.kind}", not "${other[0]}".`,
    );
  }
  switch (input.kind) {
    case "pr": {
      if (input.pr === undefined) return refuse("invalid", 'Kind "pr" needs "pr": {number}.');
      const { number, repository, until } = input.pr;
      return {
        ok: true,
        target: {
          kind: "pr",
          spec: {
            number,
            ...(repository === undefined ? {} : { repository }),
            until: until ?? "checks",
          },
        },
      };
    }
    case "file": {
      if (input.file === undefined) return refuse("invalid", 'Kind "file" needs "file": {path}.');
      const { path, until } = input.file;
      if (path.includes("\0") || !isAbsolutePath(path, platform)) {
        return refuse("invalid", "The file path must be absolute.");
      }
      return { ok: true, target: { kind: "file", spec: { path, until: until ?? "exists" } } };
    }
    case "unit": {
      if (input.unit === undefined) return refuse("invalid", 'Kind "unit" needs "unit": {name}.');
      if (platform !== "linux") {
        return refuse("not_supported", "Unit waits need systemd on Linux. Use a check command.");
      }
      const { name, scope } = input.unit;
      if (!UNIT_NAME_PATTERN.test(name))
        return refuse("invalid", "That is not a systemd unit name.");
      return { ok: true, target: { kind: "unit", spec: { name, scope: scope ?? "system" } } };
    }
    case "check": {
      if (input.check === undefined)
        return refuse("invalid", 'Kind "check" needs "check": {name}.');
      const { name } = input.check;
      const args = input.check.args ?? [];
      const check = Object.hasOwn(checks, name) ? checks[name] : undefined;
      if (check === undefined) {
        const names = Object.keys(checks).sort();
        return refuse(
          "unknown_check",
          names.length === 0
            ? `No check "${name}": this project has no checks.`
            : `No check "${name}". Checks: ${names.join(", ")}.`,
        );
      }
      if (args.length > check.maxArgs) {
        return refuse("invalid", `Check "${name}" takes at most ${check.maxArgs} argument(s).`);
      }
      // The whole argument must match, whatever anchors the pattern has.
      const pattern = new RegExp(`^(?:${check.argPattern})$`, "u");
      const bad = args.find((arg) => arg.startsWith("-") || !pattern.test(arg));
      if (bad !== undefined) {
        return refuse(
          "invalid",
          `Argument ${JSON.stringify(bad)} is not allowed for check "${name}".`,
        );
      }
      return { ok: true, target: { kind: "check", spec: { name, args } } };
    }
    case "slot": {
      const account = input.slot?.account;
      if (account !== undefined && !accounts.has(account)) {
        return refuse("invalid", `No provider instance "${account}".`);
      }
      return { ok: true, target: { kind: "slot", spec: account === undefined ? {} : { account } } };
    }
  }
}

/** The deadline in epoch ms, or a refusal when the caller asks for more than the maximum. */
export function waitDeadline(
  now: number,
  timeoutMinutes: number | undefined,
  settings: Pick<WaitForSettings, "defaultTimeoutHours" | "maxTimeoutHours">,
): { readonly ok: true; readonly deadlineAt: number } | WaitRefusal {
  const maxMinutes = settings.maxTimeoutHours * 60;
  if (timeoutMinutes !== undefined && timeoutMinutes > maxMinutes) {
    return refuse("invalid", `timeoutMinutes is at most ${maxMinutes}.`);
  }
  const minutes = timeoutMinutes ?? Math.min(settings.defaultTimeoutHours * 60, maxMinutes);
  return { ok: true, deadlineAt: now + minutes * 60_000 };
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value)
      .filter(([, entry]) => entry !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([key, entry]) => `${JSON.stringify(key)}:${canonicalJson(entry)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

/** 16 hex characters: the same open condition from the same thread gives the same wait. */
export function waitKey(threadId: ThreadId, target: WaitTarget): string {
  const text = canonicalJson({ threadId, kind: target.kind, spec: target.spec });
  return Hex.encode(sha256(new TextEncoder().encode(text))).slice(0, 16);
}

export type Judgement =
  | { readonly type: "met"; readonly outcome: WaitOutcome; readonly detail: string }
  | { readonly type: "notYet"; readonly detail?: string }
  | { readonly type: "failed"; readonly message: string; readonly permanent: boolean };

function firstLine(text: string): string {
  const line = text.split(/\r?\n/u).find((candidate) => candidate.trim() !== "") ?? "";
  return clip(line.trim());
}

/** Ends `text` with a full stop unless it already ends a sentence. */
function sentence(text: string): string {
  return /[.!?…]$/u.test(text) ? text : `${text}.`;
}

function clip(text: string, max = DETAIL_MAX): string {
  const characters = Array.from(text);
  return characters.length <= max ? text : `${characters.slice(0, max - 1).join("")}…`;
}

/** Exit 0 = met, 1 = not yet, anything else (or a kill) = error. */
export function judgeExit(input: {
  readonly exitCode: number | null;
  readonly stdout: string;
  readonly stderr: string;
  readonly timedOut?: boolean;
  readonly outputTruncated?: boolean;
}): Judgement {
  if (input.timedOut === true) {
    return { type: "failed", message: "Timed out.", permanent: false };
  }
  if (input.outputTruncated === true) {
    return { type: "failed", message: "Printed too much output.", permanent: false };
  }
  if (input.exitCode === 0) return { type: "met", outcome: "met", detail: firstLine(input.stdout) };
  if (input.exitCode === 1) return { type: "notYet" };
  const reason = firstLine(input.stderr);
  const code = input.exitCode === null ? "killed" : `exit ${input.exitCode}`;
  return {
    type: "failed",
    message: reason === "" ? `${code}.` : `${code}: ${reason}`,
    permanent: false,
  };
}

/** What one check status means for a wait. No default: a new upstream status fails the build. */
function checkStatusClass(
  status: PullRequestCheckStatus,
): "pending" | "failed" | "needs_action" | "passed" {
  switch (status) {
    case "pending":
      return "pending";
    case "failure":
    case "cancelled":
      return "failed";
    case "action-required":
      return "needs_action";
    case "success":
    case "skipped":
    case "neutral":
      return "passed";
  }
}

/**
 * The newest head seen with every check finished (or with no checks at all, `noChecks`), and when
 * it was first seen so.
 */
export interface PrSeen {
  readonly headSha: string | null;
  readonly finishedAt: number;
  readonly noChecks?: true;
}

export type PrJudgement = Judgement & { readonly seen: PrSeen | null };

function headText(headSha: string | null): string {
  return headSha === null ? "Head unknown." : `Head ${headSha.slice(0, 7)}.`;
}

function names(checks: ReadonlyArray<PullRequestCheck>): string {
  const listed = checks.slice(0, 5).map((check) => check.name);
  return `${listed.join(", ")}${checks.length > 5 ? ", …" : ""}`;
}

/**
 * Met only when one head reads all finished (or no checks, after `noChecksGraceSeconds` from when
 * it was first seen with none) at two reads at least `prSweepSeconds` apart: a fresh push lists
 * few or no checks until its workflows are queued. A missing `headSha` cannot tell heads apart.
 */
export function judgePrChecks(input: {
  readonly detail: { readonly state: PullRequestState; readonly headSha?: string | undefined };
  /** `null`: the host could not read them, which is not the same as no checks. */
  readonly checks: { readonly checks: ReadonlyArray<PullRequestCheck> } | null;
  readonly until: "checks" | "merged";
  readonly seen: PrSeen | null;
  readonly now: number;
  readonly settings: Pick<WaitForSettings, "prSweepSeconds" | "noChecksGraceSeconds">;
}): PrJudgement {
  const { detail, until, seen, now, settings } = input;
  if (detail.state === "merged")
    return { type: "met", outcome: "merged", detail: "Merged.", seen: null };
  if (detail.state === "closed") {
    return { type: "met", outcome: "closed", detail: "Closed without a merge.", seen: null };
  }
  if (until === "merged") return { type: "notYet", detail: "Open.", seen: null };
  if (input.checks === null) {
    return { type: "failed", message: "The checks could not be read.", permanent: false, seen };
  }
  const headSha = detail.headSha ?? null;
  const head = headText(headSha);
  if (input.checks.checks.some((check) => check.placeholder === true)) {
    return { type: "notYet", detail: `${head} Some checks could not be listed.`, seen: null };
  }
  const checks = input.checks.checks;
  if (checks.length === 0) {
    if (seen === null || seen.headSha !== headSha || seen.noChecks !== true) {
      return {
        type: "notYet",
        detail: `${head} No checks yet.`,
        seen: { headSha, finishedAt: now, noChecks: true },
      };
    }
    const settleMs =
      Math.max(settings.noChecksGraceSeconds, settings.prSweepSeconds) * MS_PER_SECOND;
    return now - seen.finishedAt >= settleMs
      ? { type: "met", outcome: "no_checks", detail: `${head} No checks.`, seen }
      : { type: "notYet", detail: `${head} No checks yet.`, seen };
  }
  const byClass = { pending: [], failed: [], needs_action: [], passed: [] } as Record<
    ReturnType<typeof checkStatusClass>,
    PullRequestCheck[]
  >;
  for (const check of checks) byClass[checkStatusClass(check.status)].push(check);
  if (byClass.pending.length > 0) {
    return {
      type: "notYet",
      detail: `${head} ${byClass.pending.length} of ${checks.length} checks still running.`,
      seen: null,
    };
  }
  if (seen === null || seen.headSha !== headSha || seen.noChecks === true) {
    return {
      type: "notYet",
      detail: `${head} All checks finished once.`,
      seen: { headSha, finishedAt: now },
    };
  }
  if (now - seen.finishedAt < settings.prSweepSeconds * MS_PER_SECOND) {
    return { type: "notYet", detail: `${head} All checks finished once.`, seen };
  }
  const total = checks.length;
  if (byClass.failed.length > 0) {
    return {
      type: "met",
      outcome: "failed",
      detail: `${head} ${byClass.failed.length} of ${total} failed: ${names(byClass.failed)}.`,
      seen,
    };
  }
  if (byClass.needs_action.length > 0) {
    return {
      type: "met",
      outcome: "needs_action",
      detail: `${head} ${byClass.needs_action.length} of ${total} need action: ${names(byClass.needs_action)}.`,
      seen,
    };
  }
  return { type: "met", outcome: "passed", detail: `${head} All ${total} checks passed.`, seen };
}

export type WaitEvent =
  /** A judge's answer for a `waiting` wait. */
  | { readonly type: "judged"; readonly judgement: Judgement }
  /** The sweep gave a `waiting` slot wait a free slot. */
  | { readonly type: "slotGranted"; readonly detail: string }
  | { readonly type: "cancel" }
  /** Written before the first send to the current receiver. */
  | { readonly type: "sendStarted"; readonly messageId: string }
  /** The send was accepted or replayed, or the message was found in the thread. */
  | { readonly type: "delivered" }
  /** The command id was refused; the next try needs a new one. */
  | { readonly type: "rejected" }
  /**
   * The receiver is archived or deleted. `parentThreadId` is the parent its lineage names now
   * (subagent parents only; a fork has none), or null when unknown.
   */
  | { readonly type: "threadGone"; readonly parentThreadId: ThreadId | null }
  /** The receiver's latest run was stopped by a person or its parent. */
  | { readonly type: "threadStopped"; readonly parentThreadId: ThreadId | null }
  /** The receiver has run since it was stopped. */
  | { readonly type: "threadRunning" };

export interface WaitTransition {
  readonly record: WaitRecord;
  /** A line for the server log; `error` when a wake reached nobody. */
  readonly log?: { readonly level: "warn" | "error"; readonly message: string };
}

export type WaitStateSettings = Pick<
  WaitForSettings,
  "errorGraceSeconds" | "keepEndedHours" | "stoppedHandOverHours"
> & {
  /** `agentSlots.reserveSeconds`. */
  readonly reserveSeconds: number;
};

const ENDED_STATES: ReadonlySet<WaitState> = new Set([
  "delivered",
  "cancelled",
  "thread_gone",
  "thread_stopped",
  "dropped",
]);

/** Open waits count toward `maxPerThread` and are what a repeated call finds again. */
function isOpenWait(state: WaitState): boolean {
  return state === "waiting" || state === "delivering";
}

function isEndedWait(state: WaitState): boolean {
  return ENDED_STATES.has(state);
}

const iso = (ms: number) => DateTime.formatIso(DateTime.makeUnsafe(ms));

type RecordKey = Exclude<keyof WaitRecord, "kind" | "spec">;

/** Every record shape shares these fields, so changing them keeps the kind and its spec. */
function update(
  wait: WaitRecord,
  changes: Partial<Pick<WaitRecord, RecordKey>>,
  remove: ReadonlyArray<RecordKey> = [],
): WaitRecord {
  const next: Record<string, unknown> = { ...wait, ...changes };
  for (const key of remove) delete next[key];
  return next as WaitRecord;
}

function freeze(wait: WaitRecord, outcome: WaitOutcome, detail: string, now: number): WaitRecord {
  const result: WaitResult = { outcome, detail: clip(detail), at: iso(now) };
  return update(wait, { state: "delivering", result }, ["errorSince", "lastError"]);
}

function handOver(
  wait: WaitRecord,
  because: "gone" | "stopped",
  namedParent: ThreadId | null,
  now: number,
): WaitTransition {
  const parent = namedParent ?? wait.parentThreadId ?? null;
  const via = wait.via ?? [];
  const what = because === "gone" ? "is gone (archived or deleted)" : "stayed stopped";
  if (
    parent === null ||
    parent === wait.threadId ||
    via.includes(parent) ||
    via.length >= MAX_HAND_OVERS
  ) {
    return {
      record: update(
        wait,
        { state: because === "gone" ? "thread_gone" : "thread_stopped", endedAt: iso(now) },
        ["stoppedSince"],
      ),
      log: {
        level: "error",
        message: `Wait ${wait.id}: thread ${wait.threadId} ${what} and no parent can take its message.`,
      },
    };
  }
  return {
    // The receiver's own stop starts its own limit, and its own sends start from attempt 0.
    record: update(
      wait,
      { threadId: parent, via: [...via, wait.threadId], handedOverBecause: because, attempt: 0 },
      ["messageId", "stoppedSince", "parentThreadId"],
    ),
    log: {
      level: "warn",
      message: `Wait ${wait.id}: thread ${wait.threadId} ${what}; its message goes to parent ${parent}.`,
    },
  };
}

/**
 * The next record after `event`, or null when nothing changes (and nothing is written). Every
 * change of a stored wait goes through here.
 */
export function nextWaitState(
  wait: WaitRecord,
  event: WaitEvent,
  now: number,
  settings: WaitStateSettings,
): WaitTransition | null {
  const deadline = Date.parse(wait.deadlineAt);
  switch (event.type) {
    case "judged": {
      if (wait.state !== "waiting") return null; // a cancel or another sweep came first
      const { judgement } = event;
      if (judgement.type === "met") {
        return { record: freeze(wait, judgement.outcome, judgement.detail, now) };
      }
      if (judgement.type === "failed") {
        if (judgement.permanent) {
          return { record: freeze(wait, "check_error", judgement.message, now) };
        }
        const errorSince = wait.errorSince === undefined ? now : Date.parse(wait.errorSince);
        if (now - errorSince >= settings.errorGraceSeconds * MS_PER_SECOND) {
          const minutes = Math.round((now - errorSince) / 60_000);
          return {
            record: freeze(
              wait,
              "check_error",
              `No good answer for ${minutes} min. Last: ${sentence(judgement.message)}`,
              now,
            ),
          };
        }
        // The deadline is tested after the judge, so a condition met while the server was down
        // is reported as met, not as a timeout.
        if (now >= deadline) {
          return { record: freeze(wait, "timeout", `Last: ${sentence(judgement.message)}`, now) };
        }
        return wait.errorSince !== undefined && wait.lastError === judgement.message
          ? null
          : { record: update(wait, { errorSince: iso(errorSince), lastError: judgement.message }) };
      }
      if (now >= deadline) {
        return {
          record: freeze(wait, "timeout", judgement.detail ?? "Not met by the deadline.", now),
        };
      }
      return wait.errorSince === undefined && wait.lastError === undefined
        ? null
        : { record: update(wait, {}, ["errorSince", "lastError"]) };
    }
    case "slotGranted":
      return wait.state === "waiting" && wait.kind === "slot"
        ? { record: freeze(wait, "free", event.detail, now) }
        : null;
    case "cancel":
      return wait.state === "waiting"
        ? { record: update(wait, { state: "cancelled", endedAt: iso(now) }) }
        : null;
    case "sendStarted":
      return wait.state === "delivering" && wait.messageId === undefined
        ? { record: update(wait, { messageId: event.messageId }) }
        : null;
    case "delivered": {
      if (wait.state !== "delivering") return null;
      const reservation =
        wait.kind === "slot"
          ? { reservedUntil: iso(now + settings.reserveSeconds * MS_PER_SECOND) }
          : {};
      return {
        record: update(wait, { state: "delivered", endedAt: iso(now), ...reservation }, [
          "stoppedSince",
        ]),
      };
    }
    case "rejected": {
      if (wait.state !== "delivering") return null;
      if (now >= deadline + settings.keepEndedHours * MS_PER_HOUR) {
        return {
          record: update(wait, { state: "dropped", endedAt: iso(now) }),
          log: {
            level: "error",
            message: `Wait ${wait.id}: thread ${wait.threadId} refused its message until long after the deadline.`,
          },
        };
      }
      return { record: update(wait, { attempt: wait.attempt + 1 }) };
    }
    case "threadGone":
      return wait.state === "delivering" ? handOver(wait, "gone", event.parentThreadId, now) : null;
    case "threadStopped": {
      if (wait.state !== "delivering") return null;
      if (wait.stoppedSince === undefined) {
        return { record: update(wait, { stoppedSince: iso(now) }) };
      }
      return now - Date.parse(wait.stoppedSince) >= settings.stoppedHandOverHours * MS_PER_HOUR
        ? handOver(wait, "stopped", event.parentThreadId, now)
        : null;
    }
    case "threadRunning":
      return wait.stoppedSince !== undefined && isOpenWait(wait.state)
        ? { record: update(wait, {}, ["stoppedSince"]) }
        : null;
  }
}

/** Ended waits leave the file after `keepEndedHours`, a slot wait not before its reservation. */
export function shouldPruneWait(
  wait: WaitRecord,
  now: number,
  settings: Pick<WaitForSettings, "keepEndedHours">,
): boolean {
  if (!isEndedWait(wait.state) || wait.endedAt === undefined) return false;
  if (wait.reservedUntil !== undefined && Date.parse(wait.reservedUntil) > now) return false;
  return now - Date.parse(wait.endedAt) >= settings.keepEndedHours * MS_PER_HOUR;
}

/** The pause before the next try after `attempt` refused sends. */
export function deliveryRetryDelayMs(attempt: number): number {
  return Math.min(5_000 * 2 ** Math.min(attempt, 10), 300_000);
}

export interface DeliveryGroup {
  readonly threadId: ThreadId;
  readonly messageId: string;
  /** Not yet written on the waits: the caller writes it (`sendStarted`) before it sends. */
  readonly fresh: boolean;
  readonly waits: ReadonlyArray<WaitRecord>;
}

/**
 * Groups `delivering` waits into messages: one per receiver, at most `MAX_JOINED` waits each. A
 * wait that was already sent keeps its message id, so a retry is the same message.
 */
export function deliveryGroups(waits: ReadonlyArray<WaitRecord>): ReadonlyArray<DeliveryGroup> {
  const delivering = waits
    .filter((wait) => wait.state === "delivering")
    .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const groups: DeliveryGroup[] = [];
  const started = new Map<string, WaitRecord[]>();
  const fresh = new Map<ThreadId, WaitRecord[]>();
  for (const wait of delivering) {
    if (wait.messageId !== undefined) {
      started.set(wait.messageId, [...(started.get(wait.messageId) ?? []), wait]);
    } else {
      fresh.set(wait.threadId, [...(fresh.get(wait.threadId) ?? []), wait]);
    }
  }
  for (const [messageId, members] of started) {
    groups.push({ threadId: members[0]!.threadId, messageId, fresh: false, waits: members });
  }
  for (const [threadId, members] of fresh) {
    for (let index = 0; index < members.length; index += MAX_JOINED) {
      const chunk = members.slice(index, index + MAX_JOINED);
      const first = chunk[0]!;
      // The hop count keeps a parent's message id apart from the one its child had.
      const hop = (first.via ?? []).length;
      const base =
        chunk.length === 1 ? `wait:${first.id}` : `wait:${first.id}:joined:${chunk.length}`;
      groups.push({
        threadId,
        messageId: hop === 0 ? base : `${base}:via:${hop}`,
        fresh: true,
        waits: chunk,
      });
    }
  }
  return groups;
}

/** A refused command id stays refused, so each attempt has its own. */
export function deliveryCommandId(group: DeliveryGroup): string {
  return `${group.messageId}:${Math.max(...group.waits.map((wait) => wait.attempt))}`;
}

/** The kind and target as people read them, such as `pr 123 checks`. */
function waitTargetText(target: WaitTarget): string {
  switch (target.kind) {
    case "pr":
      return `pr ${target.spec.repository === undefined ? "" : `${target.spec.repository}#`}${target.spec.number} ${target.spec.until}`;
    case "file":
      return `file ${target.spec.path}`;
    case "unit":
      return `unit ${target.spec.name}${target.spec.scope === "user" ? " (user)" : ""}`;
    case "check":
      return ["check", target.spec.name, ...target.spec.args].join(" ");
    case "slot":
      return target.spec.account === undefined ? "slot" : `slot ${target.spec.account}`;
  }
}

function wakeLine(wait: WaitRecord): string {
  const via = wait.via ?? [];
  const last = via.at(-1);
  const registeredBy = via.length > 1 ? ` (registered by ${via[0]})` : "";
  const handedOver =
    last === undefined
      ? ""
      : wait.handedOverBecause === "stopped"
        ? `Thread ${last}, which you started, was stopped and has not run again, so its wake comes to you (it was not told)${registeredBy}: `
        : `Thread ${last}, which you started, is gone (archived or deleted), so its wait comes to you${registeredBy}: `;
  const result =
    wait.result === undefined
      ? "not ended yet."
      : `${wait.result.outcome}.${wait.result.detail === "" ? "" : ` ${clip(wait.result.detail)}`}`;
  return `[wait ${wait.id}] ${handedOver}${waitTargetText(wait)}: ${result}`;
}

/** One wait's message: `[wait <id>] <target>: <outcome>. <detail>`, and its note on a new line. */
export function wakeMessage(wait: WaitRecord): string {
  const line = wakeLine(wait);
  return wait.note === undefined ? line : `${line}\nNote: ${wait.note}`;
}

/** Several waits for one receiver, one line each; every line starts with its own marker. */
export function joinedWakeMessage(waits: ReadonlyArray<WaitRecord>): string {
  if (waits.length === 0 || waits.length > MAX_JOINED) {
    throw new RangeError(`A joined message holds 1 to ${MAX_JOINED} waits, not ${waits.length}.`);
  }
  if (waits.length === 1) return wakeMessage(waits[0]!);
  return waits
    .map((wait) => {
      const line = wakeLine(wait);
      return wait.note === undefined ? line : `${line} Note: ${wait.note}`;
    })
    .join("\n");
}
