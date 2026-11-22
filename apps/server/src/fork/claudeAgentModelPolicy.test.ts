/**
 * Fork: the model policy's check of Claude's own Agent tool (claudeAgentModelPolicy.ts): each
 * policy state, the alias read as the full id, a fork, and failing closed. The adapter test
 * (claudeMcpProfile.adapter.test.ts) shows every Claude query carries the hook.
 */
import type { HookInput } from "@anthropic-ai/claude-agent-sdk";
import { assert, describe, it } from "@effect/vitest";
import {
  DEFAULT_SERVER_SETTINGS,
  MODEL_POLICY_START_ROWS,
  type ServerSettings,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";

import { BUNDLED_CLAUDE_MODEL_CATALOG } from "../provider/ClaudeModelCatalog.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import {
  CLAUDE_AGENT_TOOLS,
  claudeAgentCallPick,
  makeClaudeAgentModelPolicyHook,
} from "./claudeAgentModelPolicy.ts";

type Policy = ServerSettings["modelPolicy"];
const ENFORCE = { mode: "enforce", allow: MODEL_POLICY_START_ROWS } as unknown as Policy;
const ADVISE = { mode: "advise", allow: MODEL_POLICY_START_ROWS } as unknown as Policy;
const OFF = { mode: "off", allow: MODEL_POLICY_START_ROWS } as unknown as Policy;

const callInput = (toolInput: unknown, tool = "Agent") =>
  ({
    hook_event_name: "PreToolUse",
    tool_name: tool,
    tool_input: toolInput,
    tool_use_id: "toolu_agent",
    session_id: "s",
    transcript_path: "/tmp/t.jsonl",
    cwd: "/workspace",
  }) as unknown as HookInput;

/** The hook's answer for one call, under these settings (`undefined`: a host without settings). */
const answer = (
  modelPolicy: Policy | "no-settings" | "unreadable" | { readonly held: unknown },
  toolInput: unknown,
  tool = "Agent",
) =>
  Effect.gen(function* () {
    const matcher = yield* makeClaudeAgentModelPolicyHook;
    return yield* Effect.promise(() =>
      matcher.hooks[0]!(callInput(toolInput, tool), "toolu_agent", {
        signal: new AbortController().signal,
      }),
    );
  }).pipe((effect) =>
    modelPolicy === "no-settings"
      ? effect
      : modelPolicy === "unreadable"
        ? Effect.provideService(effect, ServerSettingsService.ServerSettingsService, {
            getSettings: Effect.die("settings read failed"),
          } as never)
        : "held" in modelPolicy
          ? // As the server holds a policy from the file that does not parse.
            Effect.provideService(effect, ServerSettingsService.ServerSettingsService, {
              getSettings: Effect.succeed({
                ...DEFAULT_SERVER_SETTINGS,
                modelPolicy: modelPolicy.held,
              }),
            } as never)
          : Effect.provide(effect, ServerSettingsService.layerTest({ modelPolicy } as never)),
  );

const decision = (output: unknown) =>
  (output as { hookSpecificOutput?: { permissionDecision?: string } }).hookSpecificOutput
    ?.permissionDecision;
const reason = (output: unknown) =>
  (output as { hookSpecificOutput?: { permissionDecisionReason?: string } }).hookSpecificOutput
    ?.permissionDecisionReason ?? "";
const context = (output: unknown) =>
  (output as { hookSpecificOutput?: { additionalContext?: string } }).hookSpecificOutput
    ?.additionalContext;

describe("claudeAgentCallPick", () => {
  it("reads the alias as the full id this machine's Claude list maps it to", () => {
    assert.deepStrictEqual(
      claudeAgentCallPick(BUNDLED_CLAUDE_MODEL_CATALOG, { model: "opus", effort: "high" }),
      {
        driver: "claudeAgent",
        model: "claude-opus-5-5",
        options: [{ id: "effort", value: "high" }],
      },
    );
    // A name the list does not map stays as given; inherit and none name no model.
    assert.strictEqual(
      claudeAgentCallPick(BUNDLED_CLAUDE_MODEL_CATALOG, { model: "unknown-x" })?.model,
      "unknown-x",
    );
    for (const input of [{}, { model: "inherit" }, { model: " " }, null, "text"]) {
      assert.isUndefined(claudeAgentCallPick(BUNDLED_CLAUDE_MODEL_CATALOG, input)?.model);
    }
    // A fork is read like any call (Claude runs it on the session's model; the owner's decision
    // and the model guard still have every call name an allowed one).
    assert.strictEqual(
      claudeAgentCallPick(BUNDLED_CLAUDE_MODEL_CATALOG, { subagent_type: "fork", model: "sonnet" })
        .model,
      "claude-sonnet-5-5",
    );
  });
});

describe("the Agent tool under the model policy (#128)", () => {
  it.effect("enforce: refuses a model outside the rows, an effort it does not allow, or none", () =>
    Effect.gen(function* () {
      for (const [input, why] of [
        [{ model: "sonnet", effort: "high" }, "claude-sonnet-5-5 (claudeAgent) is in no row"],
        [{ model: "opus", effort: "max" }, "claude-opus-5-5 does not allow effort max"],
        [
          { model: "opus" },
          "claude-opus-5-5 needs an effort from its row, and the pick names none",
        ],
        [{ effort: "high" }, "the call names no model"],
        [{ model: "inherit", effort: "high" }, "the call names no model"],
        [
          { model: "opus", effort: 3 },
          "claude-opus-5-5's effort option effort holds no effort name",
        ],
      ] as const) {
        // Runs again: the same answer.
        for (const _ of [1, 2]) {
          const output = yield* answer(ENFORCE, input);
          assert.strictEqual(decision(output), "deny", JSON.stringify(input));
          assert.include(reason(output), `Model policy refused this pick: ${why}.`);
          assert.include(reason(output), "Allowed: claudeAgent claude-opus-5-5");
          assert.include(reason(output), "the Agent call's model (an alias such as opus");
        }
      }
      // Task (older Claude Code) is read alike.
      assert.strictEqual(
        decision(yield* answer(ENFORCE, { model: "sonnet", effort: "high" }, "Task")),
        "deny",
      );
    }),
  );

  it.effect("enforce: an inside pick runs with its row's useFor; other tools pass", () =>
    Effect.gen(function* () {
      const inside = yield* answer(ENFORCE, { model: "opus", effort: "high" });
      assert.isUndefined(decision(inside));
      assert.strictEqual(
        context(inside),
        "Model policy: claude-opus-5-5 is for most work: building, routine work, review lenses.",
      );
      // The full id reads the same as its alias.
      assert.isUndefined(
        decision(yield* answer(ENFORCE, { model: "claude-fable-5-2", effort: "xhigh" })),
      );
      // A fork must name an allowed model too: none is refused, an inside one runs.
      assert.strictEqual(decision(yield* answer(ENFORCE, { subagent_type: "fork" })), "deny");
      assert.isUndefined(
        decision(yield* answer(ENFORCE, { subagent_type: "fork", model: "opus", effort: "high" })),
      );
      assert.deepStrictEqual(yield* answer(ENFORCE, { command: "ls" }, "Bash"), {});
      assert.deepStrictEqual(CLAUDE_AGENT_TOOLS, ["Agent", "Task"]);
    }),
  );

  it.effect("advise runs it with a note; off and a host without settings check nothing", () =>
    Effect.gen(function* () {
      const advised = yield* answer(ADVISE, { model: "sonnet", effort: "high" });
      assert.isUndefined(decision(advised));
      assert.include(
        context(advised) ?? "",
        "Model policy (advise): this pick is outside the policy",
      );
      assert.deepStrictEqual(yield* answer(OFF, { model: "sonnet" }), {});
      assert.deepStrictEqual(yield* answer("no-settings", { model: "sonnet" }), {});
    }),
  );

  it.effect("fails closed: unreadable policy or settings, and a check that throws, refuse", () =>
    Effect.gen(function* () {
      const bad = yield* answer(
        { held: { mode: "enforce", allow: [], unreadable: { mode: "on", allow: "x" } } },
        { model: "opus", effort: "high" },
      );
      assert.strictEqual(decision(bad), "deny");
      assert.include(reason(bad), "does not parse), so every agent pick is refused");
      // A settings read that dies: the hook refuses rather than letting the call through.
      const died = yield* answer("unreadable", { model: "opus", effort: "high" });
      assert.strictEqual(decision(died), "deny");
      const matcher = yield* makeClaudeAgentModelPolicyHook.pipe(
        Effect.provide(ServerSettingsService.layerTest({ modelPolicy: ENFORCE } as never)),
      );
      const throwing = {
        hook_event_name: "PreToolUse",
        get tool_name(): string {
          throw new Error("malformed hook input");
        },
      } as unknown as HookInput;
      const output = yield* Effect.promise(() =>
        matcher.hooks[0]!(throwing, undefined, { signal: new AbortController().signal }),
      );
      assert.strictEqual(decision(output), "deny");
      assert.include(reason(output), "could not check this Agent call");
    }),
  );
});
