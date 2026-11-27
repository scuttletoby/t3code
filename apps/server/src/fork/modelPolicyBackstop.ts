/**
 * Fork: the model policy's backstop in ThreadManagementService.dispatch. A command an agent's MCP
 * call dispatches with a model, or a thread fork, is checked unless the call's route checked its
 * picks itself; the owner's client and the server's own work are not read (docs/fork/model-policy.md).
 */
import type { ModelSelection, OrchestrationV2ServerCommand } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import {
  type OrchestratorV2,
  OrchestratorCommandRejectedError,
} from "../orchestration-v2/Orchestrator.ts";
import * as ModelPolicyCheck from "./modelPolicyCheck.ts";

/** An agent's call: a thread bound to the session, or a linked machine's call naming its thread. */
const agentCall = (scope: McpInvocationContext.McpInvocationScope): boolean =>
  scope.thread !== undefined ||
  (scope.client?.linked === true && scope.client.senderThread !== undefined);

/** The model the command starts or changes a thread on, or `undefined` when it names none. */
const selectionOf = (
  command: OrchestrationV2ServerCommand,
  orchestrator: Pick<OrchestratorV2["Service"], "getThreadRecords">,
) => {
  switch (command.type) {
    case "thread.create":
    case "thread.model-selection.set":
    case "provider.switch":
    case "delegated_task.request":
    case "message.dispatch":
      return Effect.succeed<ModelSelection | undefined>(command.modelSelection);
    case "thread.fork":
      return Effect.map(
        orchestrator.getThreadRecords(command.sourceThreadId, []),
        (source): ModelSelection | undefined => source.thread.modelSelection,
      );
    default:
      return Effect.undefined;
  }
};

/**
 * The check before a command is dispatched: refuses it (as upstream's rejected command, whose text
 * the MCP reply carries) when the policy refuses its model.
 */
export const checkAgentCommand = Effect.fn("ModelPolicyBackstop.checkAgentCommand")(function* (
  command: OrchestrationV2ServerCommand,
  orchestrator: Pick<OrchestratorV2["Service"], "getThreadRecords">,
) {
  const scope = yield* Effect.serviceOption(McpInvocationContext.McpInvocationContext);
  if (Option.isNone(scope) || !agentCall(scope.value)) return;
  if (ModelPolicyCheck.routeCheckedCall(scope.value)) return;
  const selection = yield* selectionOf(command, orchestrator);
  if (selection === undefined) return;
  const check = yield* ModelPolicyCheck.makePickCheck;
  yield* check(
    "agent command",
    Effect.map(ModelPolicyCheck.currentProviders, (providers) =>
      ModelPolicyCheck.pickOfSelection(providers, selection, true),
    ),
    { marksCall: false },
  ).pipe(
    Effect.catchTags({
      OrchestratorMcpFailure: (refused) =>
        Effect.fail(
          new OrchestratorCommandRejectedError({
            commandId: command.commandId,
            commandType: command.type,
            cause: refused.message,
          }),
        ),
    }),
  );
});
