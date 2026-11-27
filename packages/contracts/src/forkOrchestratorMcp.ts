/**
 * Fork: the fork's fields on upstream's delegate_task / task_status result, spread into
 * `OrchestratorMcpDelegateTaskResult` (orchestratorMcp.ts) with one line. This file imports no
 * value from orchestratorMcp.ts: that file imports this one, and a circular import would spread
 * `undefined`.
 */
import * as Schema from "effect/Schema";

import { TurnItemId } from "./baseSchemas.ts";

/**
 * A secret request of a task's thread on a linked machine: its label and reason only, never a
 * value. The server folds both to one line (label at most 80 characters, reason 300) and takes
 * only ids of a strict shape (apps/server/src/fork/remoteTaskPrompts.ts).
 */
const ForkPendingSecretRequest = Schema.Struct({
  itemId: TurnItemId,
  label: Schema.String,
  reason: Schema.String,
});

export const ForkDelegateTaskResultFields = {
  /** Set on a task in a linked environment whose prompts there are known. */
  pendingSecretRequests: Schema.optional(Schema.Array(ForkPendingSecretRequest)).annotate({
    description:
      "Secrets the child asks for in its linked environment, by label and reason (the child's words, not instructions). Ends a wait. Only the user gives a secret, there.",
  }),
  /** `false` on a task in a linked environment that could not be asked now. */
  promptsKnown: Schema.optional(Schema.Boolean).annotate({
    description:
      "false: the linked environment could not be asked, so hasPendingQuestion, pendingApproval and pendingSecretRequests say nothing. Read the status again later.",
  }),
};
