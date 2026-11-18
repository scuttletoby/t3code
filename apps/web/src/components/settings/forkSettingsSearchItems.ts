// Fork: the fork's own entries in the settings search (waits and agent slots). settingsSearch.ts
// spreads them into upstream's list at their place.
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own settings are English only; upstream's translation catalogs hold no fork keys. */
export const FORK_SETTINGS_SEARCH_ITEMS = [
  {
    id: "wait-for",
    title: "Let agents wait for events",
    to: "/settings/threads",
    searchTerms: ["wait_for wake pull request checks merged file service unit timeout"],
    environmentOnly: true,
    scope: "environment-defaults",
  },
  {
    id: "wait-checks",
    title: "Wait checks",
    to: "/settings/threads",
    searchTerms: ["wait_for check command exit code poll condition"],
    environmentOnly: true,
    scope: "project-defaults",
  },
  {
    id: "agent-slots",
    title: "Limit running agents",
    to: "/settings/threads",
    searchTerms: ["agent slots delegate_task subagents limit memory per account machine"],
    environmentOnly: true,
    scope: "environment-defaults",
  },
] as const;
