/**
 * Fork (issue 880): the armed PR sweep's judge. Pure: one pull request now and what the last sweep
 * remembered in; a verdict (wake: conflict, red, stalled, disarmed, closed; waiting; green; drop)
 * and what to remember out. The rules: docs/fork/README.md, `server.armedSweep`.
 */
import type { ArmedSweepSettings } from "@t3tools/contracts";

/** How long auto-merge must stay off before it is a reason (the queue's own off-and-on is shorter). */
export const DISARM_GRACE_MS = 15 * 60_000;

export type CheckState = "pending" | "success" | "failure";

export interface ArmedCheck {
  readonly name: string;
  readonly state: CheckState;
  /** When it ended (ms), for a failure; null while running or when GitHub gave none. */
  readonly completedAt: number | null;
}

/** One pull request as this sweep's list call showed it. */
export interface ArmedPull {
  readonly id: string;
  readonly repository: string;
  readonly number: number;
  readonly url: string;
  readonly title: string;
  readonly state: "open" | "closed" | "merged";
  readonly armed: boolean;
  readonly head: string;
  readonly mergeable: "mergeable" | "conflicting" | "unknown";
  readonly checks: ReadonlyArray<ArmedCheck>;
}

/** What the sweep remembers of a followed pull request between sweeps (the state file). */
export interface Tracked {
  readonly id: string;
  readonly repository: string;
  readonly number: number;
  readonly url: string;
  readonly title: string;
  readonly head: string;
  /** First sweep this head was seen not green; null while green. */
  readonly notGreenSince: number | null;
  /** First sweep auto-merge was seen off; null while on. */
  readonly offSince: number | null;
}

export type WakeReason = "conflict" | "red" | "stalled" | "disarmed" | "closed";

export type Verdict =
  | { readonly type: "green" }
  /** Not green, not (yet) a reason: listed, nothing sent. */
  | { readonly type: "waiting"; readonly detail: string }
  | { readonly type: "wake"; readonly reason: WakeReason; readonly detail: string }
  /** Not followed (any more): merged, or never armed. */
  | { readonly type: "drop" };

export interface Judged {
  readonly verdict: Verdict;
  /** What to remember for the next sweep; null forgets it. */
  readonly next: Tracked | null;
}

type JudgeSettings = Pick<ArmedSweepSettings, "stallMinutes" | "rerunGraceMinutes">;

const minutes = (ms: number) => `${Math.max(0, Math.floor(ms / 60_000))} min`;

const trackedOf = (pull: ArmedPull, previous: Tracked | undefined): Tracked => ({
  id: pull.id,
  repository: pull.repository,
  number: pull.number,
  url: pull.url,
  title: pull.title,
  head: pull.head,
  notGreenSince: previous?.head === pull.head ? previous.notGreenSince : null,
  offSince: previous?.offSince ?? null,
});

/** One check per name: a pending run wins, else the one that ended last (a re-run's attempt). */
export function latestChecks(checks: ReadonlyArray<ArmedCheck>): ReadonlyArray<ArmedCheck> {
  const byName = new Map<string, ArmedCheck>();
  for (const check of checks) {
    const seen = byName.get(check.name);
    if (
      seen === undefined ||
      (seen.state !== "pending" &&
        (check.state === "pending" || (check.completedAt ?? 0) >= (seen.completedAt ?? 0)))
    ) {
      byName.set(check.name, check);
    }
  }
  return [...byName.values()];
}

export function judgePull(input: {
  readonly pull: ArmedPull;
  readonly previous: Tracked | undefined;
  readonly nowMs: number;
  readonly settings: JudgeSettings;
}): Judged {
  const { pull, previous, nowMs, settings } = input;
  if (pull.state === "merged") return { verdict: { type: "drop" }, next: null };
  if (pull.state === "closed") {
    return previous === undefined
      ? { verdict: { type: "drop" }, next: null }
      : {
          verdict: { type: "wake", reason: "closed", detail: "closed without merging" },
          next: null,
        };
  }
  if (!pull.armed) {
    // Never followed: someone else's pull request, nothing to say.
    if (previous === undefined) return { verdict: { type: "drop" }, next: null };
    const offSince = previous.offSince ?? nowMs;
    if (nowMs - offSince >= DISARM_GRACE_MS) {
      return {
        verdict: {
          type: "wake",
          reason: "disarmed",
          detail: `auto-merge turned off ${minutes(nowMs - offSince)} ago`,
        },
        next: null,
      };
    }
    return {
      verdict: { type: "waiting", detail: `auto-merge off for ${minutes(nowMs - offSince)}` },
      next: { ...trackedOf(pull, previous), offSince },
    };
  }
  const base = { ...trackedOf(pull, previous), offSince: null };
  const notGreen = (detail: string, reason?: WakeReason): Judged => {
    const since = base.notGreenSince ?? nowMs;
    const next = { ...base, notGreenSince: since };
    if (reason !== undefined) return { verdict: { type: "wake", reason, detail }, next };
    if (nowMs - since >= settings.stallMinutes * 60_000) {
      return {
        verdict: {
          type: "wake",
          reason: "stalled",
          detail: `${detail} for ${minutes(nowMs - since)}`,
        },
        next,
      };
    }
    return { verdict: { type: "waiting", detail }, next };
  };
  if (pull.mergeable === "conflicting") return notGreen("conflict with its base", "conflict");
  const checks = latestChecks(pull.checks);
  if (checks.length === 0) return notGreen("no checks");
  if (checks.some((check) => check.state === "pending")) {
    const pending = checks.filter((check) => check.state === "pending").map((check) => check.name);
    return notGreen(`checks pending: ${pending.join(", ")}`);
  }
  const failed = checks.filter((check) => check.state === "failure");
  if (failed.length === 0)
    return { verdict: { type: "green" }, next: { ...base, notGreenSince: null } };
  const names = `failing: ${failed.map((check) => check.name).join(", ")}`;
  // A failure without an end time counts from the sweep that first saw this head not green.
  const endedAt = Math.max(
    ...failed.map((check) => check.completedAt ?? base.notGreenSince ?? nowMs),
  );
  const graceLeft = endedAt + settings.rerunGraceMinutes * 60_000 - nowMs;
  if (graceLeft > 0) {
    return notGreenOnly(
      base,
      nowMs,
      `${names}; waiting ${minutes(graceLeft)} more for its one re-run`,
    );
  }
  return notGreen(names, "red");
}

/** Red inside its re-run grace: listed and its time counted, never stalled (the re-run decides). */
function notGreenOnly(base: Tracked, nowMs: number, detail: string): Judged {
  return {
    verdict: { type: "waiting", detail },
    next: { ...base, notGreenSince: base.notGreenSince ?? nowMs },
  };
}

/** The once-per key: one wake per pull request, head and reason. */
export const wakeKey = (
  pull: Pick<Tracked, "repository" | "number" | "head">,
  reason: WakeReason,
) => `${headKey(pull)}:${reason}`;

/** The cap's key: every reason of one head counts against `maxWakesPerHead`. */
export const headKey = (pull: Pick<Tracked, "repository" | "number" | "head">) =>
  `${pull.repository}#${pull.number}@${pull.head}`;
