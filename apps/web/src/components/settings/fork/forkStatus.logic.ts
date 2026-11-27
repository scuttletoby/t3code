/**
 * Fork: one line per fork feature for the top of the Fork page. Where a feature is on comes from the
 * fork report's own rules (`forkFeaturesWhere`, also read by apps/server/src/golive/forkReport.ts):
 * on a server, a project's override or one thread's handoff entry switches a feature on too.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own settings are English only; upstream's translation catalogs hold no fork keys. */
import {
  FORK_FEATURES,
  type ForkFeature,
  type ForkFeatureSettings,
  forkFeaturesWhere,
} from "@t3tools/contracts";

export interface ForkFeatureStatus {
  readonly key: ForkFeature;
  readonly label: string;
  readonly on: boolean;
  readonly detail: string;
}

/** One selected server (`projectId` null) or one project on a server, as the scope reads it. */
export interface ForkStatusTarget {
  readonly projectId: string | null;
  readonly settings: ForkFeatureSettings;
}

/** Typed by every feature, so a feature the report counts cannot be left off the page. */
const LABELS: Record<ForkFeature, string> = {
  usagePacingGate: "Plan usage pacing",
  accountOrder: "Account order",
  agentSlots: "Limit on running agents",
  stopFinishedChildren: "Stop finished sub-agents",
  armedSweep: "Armed pull request sweep",
  waitFor: "Agent waits",
  modelPolicy: "Models agents may use",
  handoff: "Handoff instead of compaction",
  reviewRules: "Review rules",
  playtest: "Playtest sessions",
  automaticUpdates: "Automatic updates",
  activityProbe: "Busy check (settings file only)",
};

/** The page's order: the groups below it, top to bottom. */
const ORDER: ReadonlyArray<ForkFeature> = [
  "usagePacingGate",
  "accountOrder",
  "agentSlots",
  "stopFinishedChildren",
  "waitFor",
  "armedSweep",
  "modelPolicy",
  "handoff",
  "reviewRules",
  "playtest",
  "automaticUpdates",
  "activityProbe",
];

/**
 * Each feature's line for the selected targets. A project reads its effective value (its override,
 * else the server's). Targets that read differently show "Mixed".
 */
export function forkFeatureStatuses(
  targets: ReadonlyArray<ForkStatusTarget>,
): ReadonlyArray<ForkFeatureStatus> {
  const perTarget = targets.map(targetStatuses);
  return ORDER.map((key) => {
    const lines = perTarget.map((statuses) => statuses[key]);
    const first = lines[0] ?? { on: false, detail: "Off" };
    const same = lines.every((line) => line.detail === first.detail);
    const onCount = lines.filter((line) => line.on).length;
    return {
      key,
      label: LABELS[key],
      on: onCount > 0,
      detail: same ? first.detail : `Mixed: on for ${onCount} of ${lines.length}`,
    };
  });
}

function targetStatuses(
  target: ForkStatusTarget,
): Record<ForkFeature, { readonly on: boolean; readonly detail: string }> {
  // A project's settings are already its effective values; other projects' overrides do not apply.
  const settings =
    target.projectId === null
      ? target.settings
      : { ...target.settings, projectSettingsOverrides: {} };
  const where = forkFeaturesWhere(settings);
  const statuses = {} as Record<ForkFeature, { on: boolean; detail: string }>;
  for (const key of FORK_FEATURES) {
    statuses[key] = { on: where[key].length > 0, detail: placesDetail(where[key]) };
  }
  if (settings.usagePacingGate !== "off") {
    statuses.usagePacingGate.detail = `On: ${settings.usagePacingGate}`;
  }
  if (settings.modelPolicy.mode !== "off") {
    statuses.modelPolicy.detail = `On: ${settings.modelPolicy.mode}${
      "unreadable" in settings.modelPolicy ? " (unreadable)" : ""
    }`;
  }
  if (settings.armedSweep.mode === "dry") {
    statuses.armedSweep.detail = "On: dry (lists, wakes no thread)";
  }
  return statuses;
}

/** `On` when the value itself is on, else how many projects and threads switch it on. */
function placesDetail(places: ReadonlyArray<string>): string {
  if (places.length === 0) return "Off";
  if (places.includes("server")) return "On";
  const threads = places.filter((place) => place.includes("thread:")).length;
  const projects = places.length - threads;
  const parts = [
    ...(projects > 0 ? [`${projects} project${projects === 1 ? "" : "s"}`] : []),
    ...(threads > 0 ? [`${threads} thread${threads === 1 ? "" : "s"}`] : []),
  ];
  return `On for ${parts.join(", ")}`;
}
