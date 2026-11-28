import type { HookCallbackMatcher, HookInput } from "@anthropic-ai/claude-agent-sdk";
import { assert, describe, it } from "@effect/vitest";
import { EnvironmentId, ThreadId } from "@t3tools/contracts";
import { T3_CODE_ORCHESTRATION_INSTRUCTIONS } from "@t3tools/provider-core/server/orchestrationInstructions";

import {
  CLAUDE_BACKGROUND_AGENT_GUIDANCE,
  REPLACED_ORCHESTRATION_TEXT,
  withBackgroundAgentGuidance,
} from "./claudeBackgroundAgents.ts";
import type { ClaudeMcpProfilePlan, LineageThread } from "./claudeMcpProfile.ts";

const T3_SERVERS = { "t3-code": {} };
const plan = (options: ClaudeMcpProfilePlan["options"] = {}): ClaudeMcpProfilePlan => ({
  name: "all",
  options,
  servers: "all",
  t3Tools: "all",
});
/** A thread's lineage: top-level, or with a parent it is a `subagent` or `fork` of. */
const thread = (relationship: "subagent" | "fork" | null = null): LineageThread["lineage"] => ({
  parentThreadId: relationship === null ? null : ThreadId.make("parent"),
  relationshipToParent: relationship,
  rootThreadId: ThreadId.make("parent"),
});
const ROOT = { lineage: thread() };
const baseInput = (session: string) => ({
  session_id: session,
  transcript_path: "/dev/null",
  cwd: "/repo",
});
const signal = { signal: new AbortController().signal };

/** What each UserPromptSubmit hook of the options adds to one prompt's context. */
const promptContexts = async (options: ClaudeMcpProfilePlan["options"], session = "session") => {
  const contexts: Array<unknown> = [];
  const input = {
    ...baseInput(session),
    hook_event_name: "UserPromptSubmit",
    prompt: "Fix the flaky test.",
  } as HookInput;
  for (const matcher of options.hooks?.UserPromptSubmit ?? []) {
    for (const hook of matcher.hooks) {
      const answer = await hook(input, undefined, signal);
      contexts.push(
        (answer as { hookSpecificOutput?: { additionalContext?: unknown } }).hookSpecificOutput
          ?.additionalContext,
      );
    }
  }
  return contexts;
};
/** Runs the options' PreCompact hooks, as Claude Code does before it compacts the session. */
const compact = async (options: ClaudeMcpProfilePlan["options"], session = "session") => {
  const input = { ...baseInput(session), hook_event_name: "PreCompact", trigger: "auto" };
  for (const matcher of options.hooks?.PreCompact ?? []) {
    for (const hook of matcher.hooks) await hook(input as HookInput, undefined, signal);
  }
};

describe("CLAUDE_BACKGROUND_AGENT_GUIDANCE", () => {
  // Does the fork's override still name words upstream's orchestration text has? If upstream
  // rewords its Subagents line, this fails so the override is rewritten with it, not left stale.
  it("quotes only text upstream's T3 Code orchestration line still contains", () => {
    // The whole Claude clause, not the bare `run_in_background:false` token another line could keep.
    assert.include(REPLACED_ORCHESTRATION_TEXT, "(Claude `Agent`, `run_in_background:false`;");
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
  // Does a top-level thread with T3's MCP server (the only one given T3's orchestration text) get
  // the guidance with its first prompt, next to the hooks it already had?
  it("adds the guidance to a top-level thread with T3's tools, keeping its hooks", async () => {
    const preToolUse: HookCallbackMatcher = { matcher: "^Agent$", hooks: [async () => ({})] };
    const result = withBackgroundAgentGuidance(
      plan({ hooks: { PreToolUse: [preToolUse] } }),
      ROOT,
      { mcpServers: T3_SERVERS },
    );
    assert.deepStrictEqual(result.options.hooks?.PreToolUse, [preToolUse]);
    assert.deepStrictEqual(await promptContexts(result.options), [
      CLAUDE_BACKGROUND_AGENT_GUIDANCE,
    ]);
  });

  // Does the text stay once in a long thread? It is added to a session's first prompt in a
  // process, not again, and again only after a compaction may have summarised it away. A new
  // session (a /clear, a retry under a new id) or a new process (a resume or rollback opens one,
  // with a new plan) starts with nothing added.
  it("adds the guidance once per session and process, and again after a compaction", async () => {
    const options = withBackgroundAgentGuidance(plan(), ROOT, { mcpServers: T3_SERVERS }).options;
    assert.deepStrictEqual(await promptContexts(options), [CLAUDE_BACKGROUND_AGENT_GUIDANCE]);
    assert.deepStrictEqual(await promptContexts(options), [undefined]);
    await compact(options, "other session");
    assert.deepStrictEqual(await promptContexts(options), [undefined]);
    await compact(options);
    assert.deepStrictEqual(await promptContexts(options), [CLAUDE_BACKGROUND_AGENT_GUIDANCE]);
    assert.deepStrictEqual(await promptContexts(options), [undefined]);
    assert.deepStrictEqual(await promptContexts(options, "cleared"), [
      CLAUDE_BACKGROUND_AGENT_GUIDANCE,
    ]);
    const reopened = withBackgroundAgentGuidance(plan(), ROOT, { mcpServers: T3_SERVERS }).options;
    assert.deepStrictEqual(await promptContexts(reopened), [CLAUDE_BACKGROUND_AGENT_GUIDANCE]);
  });

  // Is the guidance left out where it does not apply: no T3 tools (no orchestration text to
  // override), a profile with no built-in tools (no Agent tool to start), or a delegated child,
  // local or from another machine, whose woken run would never report to its parent?
  // A thread the user forked stays top-level and gets it.
  it("adds nothing without T3's tools, with no built-in tools, or to a delegated child", () => {
    const bare: ClaudeMcpProfilePlan = { ...plan(), builtinTools: "none" };
    const noT3 = plan();
    const t3 = { mcpServers: T3_SERVERS };
    assert.strictEqual(withBackgroundAgentGuidance(noT3, ROOT, {}), noT3);
    assert.strictEqual(withBackgroundAgentGuidance(bare, ROOT, t3), bare);
    const all = plan();
    assert.strictEqual(withBackgroundAgentGuidance(all, { lineage: thread("subagent") }, t3), all);
    const remote = {
      lineage: thread(),
      delegatedFrom: {
        environmentId: EnvironmentId.make("other"),
        threadId: ThreadId.make("theirs"),
        title: "Their task",
      },
    };
    assert.strictEqual(withBackgroundAgentGuidance(all, remote, t3), all);
    const forked = withBackgroundAgentGuidance(all, { lineage: thread("fork") }, t3);
    assert.lengthOf(forked.options.hooks?.UserPromptSubmit ?? [], 1);
  });
});
