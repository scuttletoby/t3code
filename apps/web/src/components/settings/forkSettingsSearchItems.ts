// Fork: the label of the fork's Settings page and its rows in the settings search. settingsSearch.ts
// spreads them into upstream's list at one place.
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own settings are English only; upstream's translation catalogs hold no fork keys. */
import { AGENT_SLOTS_NUMBER_FIELDS, WAIT_FOR_NUMBER_FIELDS } from "./waitsAndSlotsSettings.logic";

export const FORK_SETTINGS_LABEL = "Fork";

const FORK_PAGE = "/settings/fork" as const;

export const FORK_SETTINGS_SEARCH_ITEMS = [
  {
    id: "fork-status",
    title: "Fork features on or off",
    to: FORK_PAGE,
    searchTerms: ["fork overview status enabled report"],
    environmentOnly: true,
  },
  {
    id: "wait-for",
    title: "Let agents wait for events",
    to: FORK_PAGE,
    searchTerms: ["wait_for wake pull request checks merged file service unit timeout"],
    environmentOnly: true,
    scope: "environment-defaults",
  },
  ...WAIT_FOR_NUMBER_FIELDS.map((field) => ({
    id: `waitFor-${field.key}` as const,
    title: field.title,
    to: FORK_PAGE,
    searchTerms: ["wait_for waits"],
    environmentOnly: true,
    scope: "environment-defaults" as const,
  })),
  {
    id: "wait-checks",
    title: "Wait checks",
    to: FORK_PAGE,
    searchTerms: ["wait_for check command exit code poll condition"],
    environmentOnly: true,
    scope: "project-defaults",
  },
  {
    id: "agent-slots",
    title: "Limit running agents",
    to: FORK_PAGE,
    searchTerms: ["agent slots delegate_task subagents limit memory per account machine"],
    environmentOnly: true,
    scope: "environment-defaults",
  },
  ...AGENT_SLOTS_NUMBER_FIELDS.map((field) => ({
    id: `agentSlots-${field.key}` as const,
    title: field.title,
    to: FORK_PAGE,
    searchTerms: ["agent slots delegate_task"],
    environmentOnly: true,
    scope: "environment-defaults" as const,
  })),
  {
    id: "usage-pacing-gate",
    title: "Usage pacing gate",
    to: FORK_PAGE,
    searchTerms: ["usagePacingGate delegate_task usage limits pace advise enforce"],
    environmentOnly: true,
    scope: "environment-defaults",
  },
  {
    id: "usage-pacing-lower-effort",
    title: "Lower effort when ahead of pace",
    to: FORK_PAGE,
    searchTerms: ["usagePacingLowerEffort reasoning effort high medium"],
    environmentOnly: true,
    scope: "environment-defaults",
  },
  {
    id: "usage-pacing",
    title: "Usage reserve per account",
    to: FORK_PAGE,
    searchTerms: ["usagePacing reservePercent burn first no limits api key provider account"],
    environmentOnly: true,
    scope: "environment-defaults",
  },
  {
    id: "review-rules",
    title: "Review rules",
    to: FORK_PAGE,
    searchTerms: ["reviewRules review lens runner model effort state question preset"],
    environmentOnly: true,
    scope: "project-defaults",
  },
] as const;
