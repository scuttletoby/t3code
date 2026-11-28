/**
 * Fork: Claude orchestrators start long native sub-agents in the background; their completion wakes
 * the thread. Sent with each prompt (UserPromptSubmit), as Claude Code drops an SDK SessionStart
 * hook's context and the system prompt is upstream's.
 */ import type { HookCallbackMatcher } from "@anthropic-ai/claude-agent-sdk";

import type { ClaudeMcpProfilePlan } from "./claudeMcpProfile.ts";

/** Upstream's words this guidance replaces, quoted in it; a test checks upstream still has them. */
export const REPLACED_ORCHESTRATION_TEXT = [
  "`run_in_background:false`",
  "Background agent: `delegate_task`, never a background `Agent`.",
] as const;

export const CLAUDE_BACKGROUND_AGENT_GUIDANCE = `T3 fork, replacing "${REPLACED_ORCHESTRATION_TEXT[0]}" and "${REPLACED_ORCHESTRATION_TEXT[1]}" in the T3 Code orchestration Subagents line: start a native \`Agent\` child that may take more than a few minutes with \`run_in_background:true\`. T3 wakes this thread with its result when it finishes, even after your turn ends, so keep working or end your turn instead of waiting; don't poll it, sleep, or call \`t3_background_wait\` for it. Use \`run_in_background:false\` only for a short child whose result your very next step needs. A child on another provider, with user-asked settings, or in its own checkout still goes through \`delegate_task\`.`;

const guidanceHook: HookCallbackMatcher = {
  hooks: [
    async () => ({
      hookSpecificOutput: {
        hookEventName: "UserPromptSubmit" as const,
        additionalContext: CLAUDE_BACKGROUND_AGENT_GUIDANCE,
      },
    }),
  ],
};

/**
 * The plan with the guidance added to each prompt, when the query carries T3's MCP server (the
 * adapter adds T3's orchestration text only then) and keeps Claude's built-in tools (its Agent).
 */
export const withBackgroundAgentGuidance = (
  plan: ClaudeMcpProfilePlan,
  overrides: { readonly mcpServers?: Readonly<Record<string, unknown>> | undefined },
): ClaudeMcpProfilePlan =>
  overrides.mcpServers === undefined || plan.builtinTools === "none"
    ? plan
    : {
        ...plan,
        options: {
          ...plan.options,
          hooks: {
            ...plan.options.hooks,
            UserPromptSubmit: [...(plan.options.hooks?.UserPromptSubmit ?? []), guidanceHook],
          },
        },
      };
