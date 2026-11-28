/**
 * Fork: reads the Antigravity CLI's own `/usage` report (`antigravity -p /usage --output-format
 * json`). It is the CLI's supported command, so T3 calls no Google API and reads no token.
 */
import type { ServerProviderUsageWindow } from "@t3tools/contracts";
import { compareSemverVersions } from "@t3tools/shared/semver";
import * as DateTime from "effect/DateTime";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";

/** The oldest CLI whose `/usage` report was checked: it answers without a model turn. */
const MINIMUM_USAGE_CLI_VERSION = "1.5.18";

export const USAGE_ARGS = ["-p", "/usage", "--output-format", "json", "--print-timeout", "25s"];

const WINDOW_MINS: Record<string, number> = { "5h": 5 * 60, weekly: 7 * 24 * 60 };

const Bucket = Schema.Struct({
  id: Schema.NonEmptyString,
  window: Schema.optional(Schema.String),
  name: Schema.optional(Schema.String),
  remaining_fraction: Schema.optional(Schema.Number),
  reset_time: Schema.optional(Schema.String),
  disabled: Schema.optional(Schema.Boolean),
});
const Report = Schema.Struct({
  status: Schema.optional(Schema.String),
  num_turns: Schema.optional(Schema.Number),
  usage: Schema.optional(Schema.Struct({ total_tokens: Schema.optional(Schema.Number) })),
  command: Schema.optional(
    Schema.Struct({
      name: Schema.optional(Schema.String),
      data: Schema.optional(
        Schema.Struct({
          groups: Schema.optional(
            Schema.Array(
              Schema.Struct({
                name: Schema.optional(Schema.String),
                buckets: Schema.optional(Schema.Array(Bucket)),
              }),
            ),
          ),
        }),
      ),
    }),
  ),
});
const decodeReport = Schema.decodeUnknownOption(Schema.fromJsonString(Report));

export type UsageOutcome =
  | { readonly _tag: "reading"; readonly windows: ReadonlyArray<ServerProviderUsageWindow> }
  /** `stop` marks a CLI that must not run `/usage` again: it would spend a model turn. */
  | { readonly _tag: "failed"; readonly message: string; readonly stop?: boolean };

const failed = (message: string, stop?: boolean): UsageOutcome =>
  stop ? { _tag: "failed", message, stop } : { _tag: "failed", message };

export const MESSAGES = {
  unreadable: "Antigravity's /usage report could not be read. Refresh limits to retry.",
  noLimits: "Antigravity's /usage report had no limits.",
  signedOut: "Antigravity says it is not signed in. Sign in again to see limits.",
  spentTurn: "This Antigravity CLI answers /usage with a model turn, so T3 stopped asking it.",
  timedOut: "Antigravity's /usage report took longer than 30 seconds. Refresh limits to retry.",
  noVersion: "T3 could not read the Antigravity CLI version, so it does not ask it for limits.",
  tooOld: (version: string) =>
    `Antigravity CLI ${version} is older than ${MINIMUM_USAGE_CLI_VERSION}; update it to see limits.`,
} as const;

/** null when the CLI may report usage; otherwise why not. */
export function versionIssue(version: string | null | undefined): string | null {
  if (!version) return MESSAGES.noVersion;
  return compareSemverVersions(version, MINIMUM_USAGE_CLI_VERSION) < 0
    ? MESSAGES.tooOld(version)
    : null;
}

const SIGNED_OUT =
  /sign[ -]?in|log[ -]?in|not authenticated|unauthenticated|authentication required/i;

function family(groupName: string | undefined, bucketId: string): string {
  if (bucketId.startsWith("gemini-argon-")) return groupName?.trim() || "Gemini Argon";
  if (bucketId.startsWith("gemini-carbon-")) return groupName?.trim() || "Gemini Carbon";
  if (bucketId.startsWith("gemini-")) return "Gemini";
  if (bucketId.startsWith("3p-")) return "Claude & GPT (shared)";
  return groupName?.trim() || bucketId;
}

function isoTime(value: string | undefined): string | undefined {
  const parsed = value ? DateTime.make(value) : Option.none();
  return Option.isSome(parsed) ? DateTime.formatIso(parsed.value) : undefined;
}

/**
 * Same window ids, labels and kinds as the quota summary T3 read before, so stored readings and
 * pacing keep matching. Only the shape is ever logged: the report names the account.
 */
export function parseUsageReport(stdout: string, stderr = ""): UsageOutcome {
  const report = decodeReport(stdout.trim());
  if (Option.isNone(report)) {
    return failed(SIGNED_OUT.test(stderr) ? MESSAGES.signedOut : MESSAGES.unreadable);
  }
  const { status, num_turns: turns, usage, command } = report.value;
  if ((turns ?? 0) > 0 || (usage?.total_tokens ?? 0) > 0) return failed(MESSAGES.spentTurn, true);
  if (status !== "SUCCESS" || command?.name !== "usage" || !command.data?.groups) {
    return failed(SIGNED_OUT.test(stderr) ? MESSAGES.signedOut : MESSAGES.unreadable);
  }
  const windows = new Map<string, ServerProviderUsageWindow>();
  for (const group of command.data.groups) {
    for (const bucket of group.buckets ?? []) {
      const remaining = bucket.remaining_fraction;
      if (bucket.disabled || remaining === undefined || !Number.isFinite(remaining)) continue;
      const weekly = bucket.window === "weekly";
      const session = bucket.window === "5h";
      const windowDurationMins = WINDOW_MINS[bucket.window ?? ""];
      const resetsAt = isoTime(bucket.reset_time);
      windows.set(bucket.id, {
        id: bucket.id,
        label: `${family(group.name, bucket.id)} · ${weekly ? "Weekly" : session ? "5-hour" : bucket.name?.trim() || "Quota"}`,
        kind: weekly ? "weekly" : session ? "session" : "other",
        usedPercent: (1 - Math.max(0, Math.min(1, remaining))) * 100,
        ...(resetsAt ? { resetsAt } : {}),
        ...(windowDurationMins === undefined ? {} : { windowDurationMins }),
      });
    }
  }
  return windows.size === 0
    ? failed(MESSAGES.noLimits)
    : { _tag: "reading", windows: [...windows.values()] };
}
