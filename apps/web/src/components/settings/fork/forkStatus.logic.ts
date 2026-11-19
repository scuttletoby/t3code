/**
 * Fork: one line per fork feature for the top of the Fork page, on or off as the selected scope
 * reads its settings. Same rules as the fork report (apps/server/src/golive/forkReport.ts).
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own settings are English only; upstream's translation catalogs hold no fork keys. */
import { type ServerSettings, effectiveReviewRules } from "@t3tools/contracts";

export interface ForkFeatureStatus {
  readonly key: string;
  readonly label: string;
  readonly on: boolean;
  readonly detail: string;
}

type StatusSettings = Pick<
  ServerSettings,
  | "waitFor"
  | "agentSlots"
  | "usagePacingGate"
  | "reviewRules"
  | "automaticUpdates"
  | "handoff"
  | "playtest"
>;

export function forkFeatureStatuses(settings: StatusSettings): ReadonlyArray<ForkFeatureStatus> {
  const handoffThreads = Object.values(settings.handoff.threads).filter(
    (mode) => mode === "handoff",
  ).length;
  const handoffOn = settings.handoff.mode === "handoff" || handoffThreads > 0;
  const reviewRulesOn = effectiveReviewRules(settings.reviewRules).preset !== "off";
  return [
    onOff("waitFor", "Waits", settings.waitFor.enabled),
    onOff("agentSlots", "Agent slots", settings.agentSlots.enabled),
    {
      key: "usagePacingGate",
      label: "Usage pacing",
      on: settings.usagePacingGate !== "off",
      detail: { off: "Off", advise: "On: advise", enforce: "On: enforce" }[
        settings.usagePacingGate
      ],
    },
    onOff("reviewRules", "Review rules", reviewRulesOn),
    onOff("automaticUpdates", "Automatic updates", settings.automaticUpdates),
    {
      key: "handoff",
      label: "Handoff",
      on: handoffOn,
      detail:
        settings.handoff.mode === "handoff"
          ? "On"
          : handoffThreads > 0
            ? `On for ${handoffThreads} thread${handoffThreads === 1 ? "" : "s"}`
            : "Off",
    },
    onOff("playtest", "Playtest", settings.playtest.enabled),
  ];
}

const onOff = (key: string, label: string, on: boolean): ForkFeatureStatus => ({
  key,
  label,
  on,
  detail: on ? "On" : "Off",
});
