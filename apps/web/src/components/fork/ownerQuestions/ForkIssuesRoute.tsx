/**
 * Fork: the Issues route's page while the client setting `ownerQuestionsPage` is on. Spread into
 * upstream's route options after its `component` (`apps/web/src/routes/_chat.issues.tsx`, a seam in
 * .github/fork-seams.json), so upstream's page files never change; off, upstream's page shows.
 */
import { IssuesPage } from "~/components/issue/IssuesPage";
import { useClientSettings, useClientSettingsHydrationStatus } from "~/hooks/useSettings";

import { issuesPageChoice } from "./forkIssuesRoute.logic";
import { WaitingOnYouPage } from "./WaitingOnYouPage";

export function ForkIssuesPage() {
  const hydration = useClientSettingsHydrationStatus();
  const ownerQuestionsPage = useClientSettings((settings) => settings.ownerQuestionsPage);
  const choice = issuesPageChoice(hydration, ownerQuestionsPage);
  if (choice === "none") return null;
  return choice === "fork" ? <WaitingOnYouPage /> : <IssuesPage />;
}

/**
 * Typed as optional so the spread may follow upstream's own `component` line (a required key would
 * make TypeScript flag that line as always overwritten).
 */
export const ForkIssuesRoute: { readonly component?: typeof ForkIssuesPage } = {
  component: ForkIssuesPage,
};
