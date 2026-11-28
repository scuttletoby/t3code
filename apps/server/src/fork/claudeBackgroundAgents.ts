/**
 * Fork: Claude orchestrators start long native sub-agents in the background; their completion wakes
 * the thread. Sent with a prompt (UserPromptSubmit), as Claude Code drops an SDK SessionStart
 * hook's context and the system prompt is upstream's. Top-level threads only: a delegated child
 * keeps upstream's foreground-only rule, as its woken run never reports to its parent.
 */ import type { HookCallbackMatcher } from "@anthropic-ai/claude-agent-sdk";
import { isDelegatedAppThread } from "@t3tools/contracts";

import type { ClaudeMcpProfilePlan, LineageThread } from "./claudeMcpProfile.ts";

/** Upstream's words this guidance replaces, quoted in it; a test checks upstream still has them. */
export const REPLACED_ORCHESTRATION_TEXT = [
  "(Claude `Agent`, `run_in_background:false`;",
  "Background agent: `delegate_task`, never a background `Agent`.",
] as const;

export const CLAUDE_BACKGROUND_AGENT_GUIDANCE = `T3 fork, replacing "${REPLACED_ORCHESTRATION_TEXT[0]}" and "${REPLACED_ORCHESTRATION_TEXT[1]}" in the T3 Code orchestration Subagents line: start a native \`Agent\` child that may take more than a few minutes with \`run_in_background:true\`. T3 wakes this thread with its result when it finishes, even after your turn ends, so keep working or end your turn instead of waiting; don't poll it, sleep, or call \`t3_background_wait\` for it. Use \`run_in_background:false\` only for a short child whose result your very next step needs. A child on another provider, with user-asked settings, or in its own checkout still goes through \`delegate_task\`.`;

/**
 * Hooks that add the guidance to a session's first prompt in this Claude process, and again to the
 * first prompt after a compaction (which may summarise it away), so it does not pile up in a long
 * thread. One set per plan: the adapter reads a plan's options only to open a new process, so each
 * process (a resume or a rollback to an earlier turn opens one) starts with nothing added yet.
 */
const guidanceHooks = () => {
  const guided = new Set<string>();
  const onPrompt: HookCallbackMatcher = {
    hooks: [
      async (input) => {
        if (guided.has(input.session_id)) return {};
        guided.add(input.session_id);
        return {
          hookSpecificOutput: {
            hookEventName: "UserPromptSubmit" as const,
            additionalContext: CLAUDE_BACKGROUND_AGENT_GUIDANCE,
          },
        };
      },
    ],
  };
  const onCompact: HookCallbackMatcher = {
    hooks: [
      async (input) => {
        guided.delete(input.session_id);
        return {};
      },
    ],
  };
  return { onPrompt, onCompact };
};

/**
 * The plan with the guidance added, when the query carries T3's MCP server (the adapter adds T3's
 * orchestration text only then), keeps Claude's built-in tools (its Agent) and is a top-level
 * thread: a local subagent or a task another machine delegated keeps upstream's rule.
 */
export const withBackgroundAgentGuidance = (
  plan: ClaudeMcpProfilePlan,
  thread: Pick<LineageThread, "lineage" | "delegatedFrom">,
  overrides: { readonly mcpServers?: Readonly<Record<string, unknown>> | undefined },
): ClaudeMcpProfilePlan => {
  if (
    overrides.mcpServers === undefined ||
    plan.builtinTools === "none" ||
    isDelegatedAppThread(thread)
  ) {
    return plan;
  }
  const { onPrompt, onCompact } = guidanceHooks();
  const hooks = plan.options.hooks;
  return {
    ...plan,
    options: {
      ...plan.options,
      hooks: {
        ...hooks,
        UserPromptSubmit: [...(hooks?.UserPromptSubmit ?? []), onPrompt],
        PreCompact: [...(hooks?.PreCompact ?? []), onCompact],
      },
    },
  };
};
