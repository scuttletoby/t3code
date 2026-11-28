/**
 * Fork: where each fork feature is on in a server's settings. One set of rules for the fork report
 * (`t3 __fork-report`, apps/server/src/golive/forkReport.ts) and the Fork settings page's overview,
 * so the two cannot drift. A bad value already reads as off (or, for the model policy, as enforce)
 * when the settings decode.
 */
import type { FORK_PROJECT_SCOPED_KEYS } from "./forkSettings.ts";
import type { HandoffSettings } from "./handoff.ts";
import type { PlaytestSettings } from "./playtest.ts";
import { effectiveReviewRules, type ReviewRules } from "./reviewRules.ts";
import type { ServerSettings } from "./settings.ts";

/** Every fork feature the report counts, in the order it lists them. */
export const FORK_FEATURES = [
  "agentSlots",
  "waitFor",
  "usagePacingGate",
  "accountOrder",
  "automaticUpdates",
  "reviewRules",
  "handoff",
  "playtest",
  "modelPolicy",
  "activityProbe",
  "stopFinishedChildren",
  "armedSweep",
] as const;
export type ForkFeature = (typeof FORK_FEATURES)[number];

/**
 * The counted features a project's override can switch on: each one's override is read in every
 * project. A test holds it to every counted feature that is also a project-scoped setting.
 */
export const FORK_REPORT_PROJECT_FEATURES = [
  "reviewRules",
  "handoff",
  "playtest",
] as const satisfies ReadonlyArray<ForkFeature & (typeof FORK_PROJECT_SCOPED_KEYS)[number]>;
type ForkProjectFeature = (typeof FORK_REPORT_PROJECT_FEATURES)[number];

export type ForkFeatureSettings = Pick<
  ServerSettings,
  ForkFeature | "usagePacingGate" | "projectSettingsOverrides"
>;

/**
 * Where each counted feature is on in `settings`: `server` (the value itself), `project:<id>` (that
 * project's override), `thread:<id>` (one thread's handoff entry in the server's value) or
 * `project:<id>/thread:<id>` (one in a project's override). Empty: off.
 */
export function forkFeaturesWhere(
  settings: ForkFeatureSettings,
): Record<ForkFeature, ReadonlyArray<string>> {
  // A project's override replaces the server's value for that project, so it can switch a feature
  // on while the server's is off.
  const overrides = <K extends ForkProjectFeature>(key: K) =>
    Object.entries(settings.projectSettingsOverrides).map(
      ([projectId, project]) => [projectId, project[key]] as const,
    );
  const server = (on: boolean) => (on ? ["server"] : []);
  const perProject = {
    reviewRules: whereOn(settings.reviewRules, overrides("reviewRules"), reviewRulesOn),
    handoff: whereOn(settings.handoff, overrides("handoff"), handoffWhere),
    playtest: whereOn(settings.playtest, overrides("playtest"), playtestOn),
  } satisfies Record<ForkProjectFeature, ReadonlyArray<string>>;
  return {
    agentSlots: server(settings.agentSlots.enabled),
    waitFor: server(settings.waitFor.enabled),
    usagePacingGate: server(settings.usagePacingGate !== "off"),
    accountOrder: server(settings.accountOrder.length > 0),
    automaticUpdates: server(settings.automaticUpdates),
    ...perProject,
    modelPolicy: server(settings.modelPolicy.mode !== "off"),
    activityProbe: server(settings.activityProbe),
    stopFinishedChildren: server(settings.stopFinishedChildren.enabled),
    armedSweep: server(settings.armedSweep.mode !== "off"),
  };
}

/** Where a value switches its feature on, below `at` (`server` or `project:<id>`). */
type WhereOn<T> = (value: T, at: string) => ReadonlyArray<string>;
const when =
  <T>(isOn: (value: T) => boolean): WhereOn<T> =>
  (value, at) =>
    isOn(value) ? [at] : [];
const reviewRulesOn = when((value: ReviewRules) => effectiveReviewRules(value).preset !== "off");
const playtestOn = when((value: PlaytestSettings) => value.enabled);
/** The value's own mode, and each thread whose entry is `handoff` (it wins over the mode). */
const handoffWhere: WhereOn<HandoffSettings> = (value, at) => [
  ...(value.mode === "handoff" ? [at] : []),
  ...Object.entries(value.threads)
    .filter(([, mode]) => mode === "handoff")
    .map(([threadId]) => `${at === "server" ? "" : `${at}/`}thread:${threadId}`),
];

/** Where the server's value and each project's (not set or null: the server's) is on. */
function whereOn<T>(
  server: T,
  projects: ReadonlyArray<readonly [string, T | null | undefined]>,
  where: WhereOn<T>,
): ReadonlyArray<string> {
  return [
    ...where(server, "server"),
    ...projects.flatMap(([projectId, value]) =>
      value == null ? [] : where(value, `project:${projectId}`),
    ),
  ];
}
