/**
 * Fork: the model policy's backstop in ThreadManagementService.dispatch. A command an agent's MCP
 * call dispatches with a model, or a thread fork, is checked unless the call's route checked its
 * picks itself or the command is a replay; the owner's client and the server's own work are not read.
 */
import type { ModelSelection, OrchestrationV2ServerCommand } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import type * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import {
  type OrchestratorV2,
  type OrchestratorV2Error,
  OrchestratorCommandRejectedError,
} from "../orchestration-v2/Orchestrator.ts";
import type * as ServerSettings from "../serverSettings.ts";
import * as ModelPolicyCheck from "./modelPolicyCheck.ts";

/** An agent's call: a thread bound to the session, or a linked machine's call naming its thread. */
const agentCall = (scope: McpInvocationContext.McpInvocationScope): boolean =>
  scope.thread !== undefined ||
  (scope.client?.linked === true && scope.client.senderThread !== undefined);

/** Each command type that carries a model (a `modelSelection` field, optional or not). */
type ModelCommandOf<C> = C extends unknown ? ("modelSelection" extends keyof C ? C : never) : never;
type ModelCommand = ModelCommandOf<OrchestrationV2ServerCommand>;

/**
 * Where each command's model runs: on this machine, or on the linked machine it was sent to (read
 * as a pick sent there, its driver unknown). Keyed by every type that carries a model, so a new or
 * extended command type fails the typecheck until it is listed here.
 */
export const MODEL_COMMANDS: { readonly [K in ModelCommand["type"]]: "here" | "linked machine" } = {
  "thread.create": "here",
  "thread.model-selection.set": "here",
  "provider.switch": "here",
  "delegated_task.request": "here",
  "message.dispatch": "here",
  "queued-run.edit": "here",
  "delegated_task.remote.request": "linked machine",
};

const isModelCommand = (command: OrchestrationV2ServerCommand): command is ModelCommand =>
  Object.hasOwn(MODEL_COMMANDS, command.type);

/**
 * A run of a scheduled task: upstream's ScheduledTaskService names its command
 * `scheduled-task:<fire key>`, and runs it on the task's stored model, not one the agent named.
 */
const SCHEDULED_RUN = "scheduled-task:";

/** The refusals the backstop made, so a caller that wraps one can still show its text. */
const refusals = new WeakSet<object>();

/**
 * The backstop's refusal text in `failure` or the causes it wraps (a scheduled run's ThreadLaunchError
 * wraps it), or `undefined`: for ScheduledTaskService's run error, which shows only the outer text.
 */
export const refusalText = (failure: unknown): string | undefined => {
  let current = failure;
  for (let depth = 0; depth < 8 && typeof current === "object" && current !== null; depth++) {
    if (refusals.has(current)) {
      return String((current as OrchestratorCommandRejectedError).cause);
    }
    current = (current as { readonly cause?: unknown }).cause;
  }
  return undefined;
};

/** Upstream's dispatchFailure (mcp/threadAccess.ts) cuts a rejected command's text here. */
export const REFUSAL_LIMIT = 1000;

type Orchestrator = Pick<OrchestratorV2["Service"], "getThreadRecords">;
type Receipts = Pick<CommandReceiptStore.CommandReceiptStoreV2["Service"], "getByCommandId">;

/** The model the command names, or the fork's source thread's: `undefined` when it names none. */
const selectionOf = (
  command: OrchestrationV2ServerCommand,
  orchestrator: Orchestrator,
): Effect.Effect<ModelSelection | undefined, OrchestratorV2Error> | undefined => {
  if (command.type === "thread.fork") {
    return Effect.map(
      orchestrator.getThreadRecords(command.sourceThreadId, []),
      (source): ModelSelection | undefined => source.thread.modelSelection,
    );
  }
  return isModelCommand(command) && command.modelSelection !== undefined
    ? Effect.succeed(command.modelSelection)
    : undefined;
};

/**
 * The pick, read only while a policy is on (a fork's source thread too): `undefined` for a replay
 * of a command already handled, which starts nothing (the orchestrator answers it from its receipt,
 * as it did the first time, whoever checked it then).
 */
const readPick = Effect.fn("ModelPolicyBackstop.readPick")(function* (
  command: OrchestrationV2ServerCommand,
  selection: Effect.Effect<ModelSelection | undefined, OrchestratorV2Error>,
  receipts: Receipts,
) {
  const replay = yield* receipts.getByCommandId(command.commandId).pipe(
    // A receipt that cannot be read is no receipt: the command is checked.
    Effect.orElseSucceed(() => Option.none()),
  );
  if (Option.isSome(replay)) return undefined;
  const model = yield* selection;
  if (model === undefined) return undefined;
  const providers = yield* ModelPolicyCheck.currentProviders;
  return isModelCommand(command) && MODEL_COMMANDS[command.type] === "linked machine"
    ? ModelPolicyCheck.pickOfRemote(providers, model)
    : ModelPolicyCheck.pickOfSelection(providers, model, true);
});

/**
 * The check before a command is dispatched, on the dispatching service's own settings and receipts:
 * refuses it (as upstream's rejected command, whose text the MCP reply carries) when the policy
 * refuses its model.
 */
export const check = Effect.fn("ModelPolicyBackstop.check")(function* (
  command: OrchestrationV2ServerCommand,
  orchestrator: Orchestrator,
  receipts: Receipts,
  serverSettings: ServerSettings.ServerSettingsService["Service"],
) {
  const scope = yield* Effect.serviceOption(McpInvocationContext.McpInvocationContext);
  if (Option.isNone(scope) || !agentCall(scope.value)) return;
  if (ModelPolicyCheck.routeCheckedCall(scope.value)) return;
  const selection = selectionOf(command, orchestrator);
  if (selection === undefined) return;
  const checkPick = ModelPolicyCheck.pickCheckOn(Option.some(serverSettings));
  const route = command.commandId.startsWith(SCHEDULED_RUN) ? "scheduled task" : "agent command";
  yield* checkPick(route, readPick(command, selection, receipts), {
    marksCall: false,
    refusalLimit: REFUSAL_LIMIT,
  }).pipe(
    Effect.catchTags({
      OrchestratorMcpFailure: (refused) => {
        const rejected = new OrchestratorCommandRejectedError({
          commandId: command.commandId,
          commandType: command.type,
          cause: refused.message,
        });
        refusals.add(rejected);
        return Effect.fail(rejected);
      },
    }),
  );
});
