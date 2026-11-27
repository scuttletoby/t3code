import { createFileRoute } from "@tanstack/react-router";

import { IssuesPage } from "../components/issue/IssuesPage";
import { issuesSearchFromUrl } from "../components/issue/issuesSearch.logic";
import { ForkIssuesRoute } from "../components/fork/ownerQuestions/ForkIssuesRoute"; // Fork (owner questions)

export const Route = createFileRoute("/_chat/issues")({
  validateSearch: issuesSearchFromUrl,
  component: IssuesPage,
  ...ForkIssuesRoute, // Fork (owner questions): the fork page while ownerQuestionsPage is on
});
