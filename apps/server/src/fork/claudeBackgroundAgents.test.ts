import type { HookCallbackMatcher, HookInput } from "@anthropic-ai/claude-agent-sdk";
import { assert, describe, it } from "@effect/vitest";
import { T3_CODE_ORCHESTRATION_INSTRUCTIONS } from "@t3tools/provider-core/server/orchestrationInstructions";

import {
  CLAUDE_BACKGROUND_AGENT_GUIDANCE,
  REPLACED_ORCHESTRATION_TEXT,
  withBackgroundAgentGuidance,
} from "./claudeBackgroundAgents.ts";
import type { ClaudeMcpProfilePlan } from "./claudeMcpProfile.ts";

const T3_SERVERS = { "t3-code": {} };
const plan = (options: ClaudeMcpProfilePlan["options"] = {}): ClaudeMcpProfilePlan => ({
  name: "all",
  options,
  servers: "all",
  t3Tools: "all",
});
const promptInput = {
  hook_event_name: "UserPromptSubmit",
  prompt: "Fix the flaky test.",
  session_id: "session",
  transcript_path: "/dev/null",
  cwd: "/repo",
} as HookInput;

/** What each UserPromptSubmit hook of the options adds to the prompt's context. */
const promptContexts = async (options: ClaudeMcpProfilePlan["options"]) => {
  const contexts: Array<unknown> = [];
  for (const matcher of options.hooks?.UserPromptSubmit ?? []) {
    for (const hook of matcher.hooks) {
      const answer = await hook(promptInput, undefined, { signal: new AbortController().signal });
      contexts.push(
        (answer as { hookSpecificOutput?: { additionalContext?: unknown } }).hookSpecificOutput
          ?.additionalContext,
      );
    }
  }
  return contexts;
};

describe("CLAUDE_BACKGROUND_AGENT_GUIDANCE", () => {
  // Does the fork's override still name words upstream's orchestration text has? If upstream
  // rewords its Subagents line, this fails so the override is rewritten with it, not left stale.
  it("quotes only text upstream's T3 Code orchestration line still contains", () => {
    for (const quoted of REPLACED_ORCHESTRATION_TEXT) {
      assert.include(T3_CODE_ORCHESTRATION_INSTRUCTIONS, quoted);
      assert.include(CLAUDE_BACKGROUND_AGENT_GUIDANCE, quoted);
    }
  });

  // Does the text let a long native child run in the background, and still keep the cases
  // delegate_task owns (another provider, user-asked settings, a separate checkout)?
  it("starts long native children in the background and keeps delegate_task's cases", () => {
    assert.include(CLAUDE_BACKGROUND_AGENT_GUIDANCE, "`run_in_background:true`");
    assert.include(CLAUDE_BACKGROUND_AGENT_GUIDANCE, "wakes this thread");
    assert.include(CLAUDE_BACKGROUND_AGENT_GUIDANCE, "`delegate_task`");
    assert.include(CLAUDE_BACKGROUND_AGENT_GUIDANCE, "another provider");
  });
});

describe("withBackgroundAgentGuidance", () => {
  // Does a thread with T3's MCP server (the only one given T3's orchestration text) get the
  // guidance with each prompt, next to the hooks it already had?
  it("adds the guidance to each prompt of a thread with T3's tools, keeping its hooks", async () => {
    const preToolUse: HookCallbackMatcher = { matcher: "^Agent$", hooks: [async () => ({})] };
    const result = withBackgroundAgentGuidance(plan({ hooks: { PreToolUse: [preToolUse] } }), {
      mcpServers: T3_SERVERS,
    });
    assert.deepStrictEqual(result.options.hooks?.PreToolUse, [preToolUse]);
    assert.deepStrictEqual(await promptContexts(result.options), [
      CLAUDE_BACKGROUND_AGENT_GUIDANCE,
    ]);
  });

  // Is the guidance left out where it does not apply: no T3 tools (no orchestration text to
  // override), or a profile with no built-in tools (no Agent tool to start)?
  it("adds nothing without T3's tools or with no built-in tools", async () => {
    const bare: ClaudeMcpProfilePlan = { ...plan(), builtinTools: "none" };
    const noT3 = plan();
    assert.strictEqual(withBackgroundAgentGuidance(noT3, {}), noT3);
    assert.strictEqual(withBackgroundAgentGuidance(bare, { mcpServers: T3_SERVERS }), bare);
  });
});
