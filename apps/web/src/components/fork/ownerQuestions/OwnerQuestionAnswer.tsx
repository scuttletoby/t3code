/**
 * Fork (Inbox, fork issue 887 row 5): under a waiting owner question, its recommendation and an
 * answer box. Posting sends the "Owner decision: ..." comment through upstream's issue comment
 * call, then takes the waiting label off (the whole label set, as trackers write it), as the
 * owner allowed in issue 829. The question leaves the list once the rule reads the answer.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this page is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { useAtomValue } from "@effect/atom-react";
import type { EnvironmentIssueEntry } from "@t3tools/client-runtime/issue-list";
import { issueWriteAccess } from "@t3tools/client-runtime/issue-presentation";
import { squashAtomCommandFailure } from "@t3tools/client-runtime/state/runtime";
import type { OwnerQuestionRule } from "@t3tools/contracts";
import type * as Cause from "effect/Cause";
import { useState } from "react";

import { readableFailure } from "~/components/pullRequest/pullRequestDetail.logic";
import { Button } from "~/components/ui/button";
import { Textarea } from "~/components/ui/textarea";
import { toastManager } from "~/components/ui/toast";
import { issueEnvironment, refreshIssueLists } from "~/state/issues";
import { useEnvironmentQuery } from "~/state/query";
import { useAtomCommand } from "~/state/use-atom-command";

import { ownerDecisionBody, postOwnerAnswer, recommendationOf } from "./ownerAnswer.logic";

/** A failed comment or label write, as upstream's toast reads it. */
type CommandFailure = { readonly _tag: "Failure"; readonly cause: Cause.Cause<unknown> };

export function OwnerQuestionAnswer({
  entry,
  rule,
}: {
  readonly entry: EnvironmentIssueEntry;
  readonly rule: OwnerQuestionRule;
}) {
  const { environmentId } = entry;
  const reference = {
    projectId: entry.projectId,
    tracker: entry.tracker,
    host: entry.host,
    scope: entry.scope,
    key: entry.key,
  };
  const detailQuery = useEnvironmentQuery(
    issueEnvironment.detail({ environmentId, input: reference }),
  );
  const canWrite = useAtomValue(issueEnvironment.comment.permissionAtom(environmentId));
  const comment = useAtomCommand(issueEnvironment.comment, { reportFailure: false });
  const update = useAtomCommand(issueEnvironment.update, { reportFailure: false });
  const [open, setOpen] = useState(false);
  const [answer, setAnswer] = useState("");
  const [posting, setPosting] = useState(false);

  const detail = detailQuery.data;
  if (detail === null) return null;
  const recommendation = recommendationOf(detail.body);
  const access = issueWriteAccess(detail, canWrite);
  const body = ownerDecisionBody(answer, rule);

  const post = async () => {
    if (body === null || posting || !access.comment) return;
    setPosting(true);
    const outcome = await postOwnerAnswer<CommandFailure>({
      body,
      labels: detail.labels,
      rule,
      mayChangeLabels: access.labels,
      comment: (text) => comment({ environmentId, input: { ...reference, body: text } }),
      setLabels: (labels) => update({ environmentId, input: { ...reference, labels } }),
    });
    if (outcome.kind === "not-posted") {
      setPosting(false);
      toastManager.add({
        type: "error",
        title: "The answer could not be posted",
        description: readableFailure(
          squashAtomCommandFailure(outcome.failure),
          "The tracker refused it.",
        ),
      });
      return;
    }
    if (outcome.labelLeft !== null) {
      toastManager.add({
        type: "warning",
        title: `Answer posted; the label ${rule.label} is still on the issue`,
        description:
          "reason" in outcome.labelLeft
            ? "You may not change this issue's labels from here."
            : readableFailure(
                squashAtomCommandFailure(outcome.labelLeft.failure),
                "The tracker refused it.",
              ),
      });
    }
    setPosting(false);
    setAnswer("");
    setOpen(false);
    // The fresh read carries the decision, so the rule takes the question off the list.
    detailQuery.refresh();
    refreshIssueLists(environmentId);
  };

  return (
    <div className="flex flex-col gap-2 px-3 pb-2 text-xs" data-testid="owner-question-answer">
      {recommendation === null ? null : (
        <p className="text-muted-foreground">
          <span className="font-medium text-foreground">Recommendation:</span> {recommendation}
        </p>
      )}
      {access.blocked !== null ? (
        <p className="text-muted-foreground">{access.blocked}</p>
      ) : !access.comment ? (
        <p className="text-muted-foreground">You may not comment on this issue from here.</p>
      ) : !open ? (
        <div>
          <Button size="compact" variant="outline" onClick={() => setOpen(true)}>
            Answer
          </Button>
        </div>
      ) : (
        <form
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void post();
          }}
        >
          <Textarea
            size="sm"
            aria-label={`Answer to ${entry.title}`}
            placeholder={
              recommendation === null
                ? "Your decision"
                : `Your decision (recommended: ${recommendation})`
            }
            value={answer}
            disabled={posting}
            onChange={(event) => setAnswer(event.target.value)}
          />
          <p className="text-muted-foreground">
            {body === null
              ? `Posts a comment starting "${rule.decisionPrefix}:" as the account this server signs in with.`
              : `Posts "${body.length > 120 ? `${body.slice(0, 120)}…` : body}" as the account this server signs in with.`}
          </p>
          <div className="flex gap-2">
            <Button size="compact" type="submit" disabled={body === null || posting}>
              {posting ? "Posting…" : "Post answer"}
            </Button>
            <Button
              size="compact"
              variant="ghost"
              type="button"
              disabled={posting}
              onClick={() => setOpen(false)}
            >
              Cancel
            </Button>
          </div>
        </form>
      )}
    </div>
  );
}
