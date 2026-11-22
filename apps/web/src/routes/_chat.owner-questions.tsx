import { createFileRoute } from "@tanstack/react-router";
import { WaitingOnYouPage } from "../components/fork/ownerQuestions/WaitingOnYouPage";

// Fork: the "Waiting on you" page. The Issues page swaps to it at one seam in
// _chat.issues.tsx (announced in .github/fork-seams.json).
export const Route = createFileRoute("/_chat/owner-questions")({
  component: WaitingOnYouPage,
});
