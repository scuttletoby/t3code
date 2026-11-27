/**
 * Fork: which page the Issues entry opens. `fork` is "Waiting on you" (owner questions of every
 * project), `upstream` is upstream's Issues page, `none` while this device's settings are still
 * loading, so a device that turned the swap off never sees the fork page flash first.
 */
import type { useClientSettingsHydrationStatus } from "~/hooks/useSettings";

type ClientSettingsHydrationStatus = ReturnType<typeof useClientSettingsHydrationStatus>;

export type IssuesPageChoice = "fork" | "upstream" | "none";

export function issuesPageChoice(
  hydration: ClientSettingsHydrationStatus,
  ownerQuestionsPage: boolean,
): IssuesPageChoice {
  if (hydration === "pending") return "none";
  // A failed read keeps the defaults (useSettings), so the setting's default decides.
  return ownerQuestionsPage ? "fork" : "upstream";
}
