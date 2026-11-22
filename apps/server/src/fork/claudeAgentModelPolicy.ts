/**
 * Fork: `modelPolicy` for Claude's own Agent tool (Task in older Claude Code), checked as each call
 * is made, like the MCP routes (modelPolicyCheck.ts). The alias is read as this machine's full id; a
 * `fork` call must name a model too, as the model guard reads it. Workflow `agent()` calls are the model guard's.
 */
import type { HookCallback, HookCallbackMatcher } from "@anthropic-ai/claude-agent-sdk";
import { ProviderDriverKind } from "@t3tools/contracts";
import * as ModelCatalog from "@t3tools/provider-core/server/ModelCatalog";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import {
  BUNDLED_CLAUDE_MODEL_CATALOG,
  type ClaudeModelCatalog,
  resolveClaudeModelCatalog,
  resolveClaudeModelSlug,
} from "../provider/ClaudeModelCatalog.ts";
import { makePickCheck, type ServerPick } from "./modelPolicyCheck.ts";

const CLAUDE_DRIVER = "claudeAgent";
/** Claude Code's tools that start a sub-agent: Agent, and Task in older versions. */
export const CLAUDE_AGENT_TOOLS: ReadonlyArray<string> = ["Agent", "Task"];
/** The pick an Agent call makes (a fork's too). `inherit` and a missing model name no model. */
export function claudeAgentCallPick(catalog: ClaudeModelCatalog, input: unknown): ServerPick {
  const args =
    typeof input === "object" && input !== null ? (input as Record<string, unknown>) : {};
  const named = typeof args.model === "string" ? args.model.trim() : "";
  const model =
    named === "" || named === "inherit" ? undefined : resolveClaudeModelSlug(catalog, named);
  const effort = args.effort;
  return {
    driver: CLAUDE_DRIVER,
    model,
    // A value that is not a string reads as an effort with no name, which is refused.
    ...(effort === undefined ? {} : { options: [{ id: "effort", value: effort }] }),
  };
}

const deny = (reason: string) => ({
  hookSpecificOutput: {
    hookEventName: "PreToolUse" as const,
    permissionDecision: "deny" as const,
    permissionDecisionReason: reason,
  },
});

const note = (text: string) => ({
  hookSpecificOutput: { hookEventName: "PreToolUse" as const, additionalContext: text },
});

/**
 * The hook, built on the services of the adapter that opens Claude's queries: the server settings
 * (none: nothing is checked, as on the MCP routes) and the model catalog (none: the bundled one).
 * Fails closed: any error while checking refuses the call (a hook that throws would count as a
 * non-blocking error and let the call through).
 */
export const makeClaudeAgentModelPolicyHook = Effect.gen(function* () {
  const check = yield* makePickCheck;
  const catalogService = yield* Effect.serviceOption(ModelCatalog.ModelCatalog);
  const loadCatalog = Option.match(catalogService, {
    onNone: () => Effect.succeed(BUNDLED_CLAUDE_MODEL_CATALOG),
    onSome: (service) =>
      service.current(ProviderDriverKind.make(CLAUDE_DRIVER)).pipe(
        Effect.map(resolveClaudeModelCatalog),
        Effect.orElseSucceed(() => BUNDLED_CLAUDE_MODEL_CATALOG),
      ),
  });
  const run = Effect.runPromiseWith(yield* Effect.context<never>());

  const hook: HookCallback = async (input) => {
    try {
      if (input.hook_event_name !== "PreToolUse") return {};
      if (!CLAUDE_AGENT_TOOLS.includes(input.tool_name)) return {};
      const answer = await run(
        Effect.gen(function* () {
          const catalog = yield* loadCatalog;
          const pick = claudeAgentCallPick(catalog, input.tool_input);
          return { note: yield* check("Agent", Effect.succeed(pick)) };
        }).pipe(
          Effect.catchTags({
            OrchestratorMcpFailure: (failure) => Effect.succeed({ refuse: failure.message }),
          }),
        ),
      );
      if ("refuse" in answer) return deny(answer.refuse);
      return answer.note === undefined ? {} : note(answer.note);
    } catch {
      return deny(
        "Model policy: T3 could not check this Agent call against the model policy, so it refused it. Try again; if it stays, name a model and effort from the policy's rows.",
      );
    }
  };
  const matcher: HookCallbackMatcher = {
    matcher: `^(${CLAUDE_AGENT_TOOLS.join("|")})$`,
    hooks: [hook],
  };
  return matcher;
});
