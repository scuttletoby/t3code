/**
 * Fork: the model policy's check of Claude's own Agent and Skill tools (claudeAgentModelPolicy.ts):
 * each policy state, the alias read as the full id and a row naming the alias, a fork, a skill that
 * runs in a sub-agent, and failing closed. The adapter test (claudeMcpProfile.adapter.test.ts) shows
 * every Claude query carries the hook once.
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import type { HookInput } from "@anthropic-ai/claude-agent-sdk";
import { assert, describe, it } from "@effect/vitest";
import {
  DEFAULT_SERVER_SETTINGS,
  MODEL_POLICY_START_ROWS,
  type ServerSettings,
} from "@t3tools/contracts";
import * as HostProcess from "@t3tools/shared/HostProcess";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";

import { BUNDLED_CLAUDE_MODEL_CATALOG } from "../provider/ClaudeModelCatalog.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import {
  CLAUDE_AGENT_TOOLS,
  claudeAgentCallPick,
  claudeConfigDirOfTranscript,
  makeClaudeAgentModelPolicyHook,
} from "./claudeAgentModelPolicy.ts";

type Policy = ServerSettings["modelPolicy"];
const policy = (mode: string, allow: unknown = MODEL_POLICY_START_ROWS) =>
  ({ mode, allow }) as unknown as Policy;
const ENFORCE = policy("enforce");
const ADVISE = policy("advise");
const OFF = policy("off");
/** Rows saved before the Agent tool was checked that name the alias, as Settings lets a person save. */
const ALIAS_ROWS = [{ driver: "claudeAgent", model: "opus", efforts: ["high"] }];

/** Where the call runs: Claude's transcript (under its config folder) and the session's folder. */
interface Place {
  readonly transcript: string;
  readonly cwd: string;
}
const NOWHERE: Place = { transcript: "/tmp/t.jsonl", cwd: "/workspace" };

const callInput = (toolInput: unknown, tool = "Agent", place = NOWHERE) =>
  ({
    hook_event_name: "PreToolUse",
    tool_name: tool,
    tool_input: toolInput,
    tool_use_id: "toolu_agent",
    session_id: "s",
    transcript_path: place.transcript,
    cwd: place.cwd,
  }) as unknown as HookInput;

type Settings = Policy | "no-settings" | "unreadable" | { readonly held: unknown };

/** Under these settings (`no-settings`: a host without them), with a file system. */
const withSettings =
  (modelPolicy: Settings) =>
  <A, E, R>(effect: Effect.Effect<A, E, R>) =>
    (modelPolicy === "no-settings"
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
          : Effect.provide(effect, ServerSettingsService.layerTest({ modelPolicy } as never))
    ).pipe(
      // No managed folder: the host's own skills are not read.
      Effect.provideService(HostProcess.Platform, "win32"),
      Effect.provideService(HostProcess.Environment, {}),
      Effect.provide(NodeServices.layer),
    );

/** The matcher a query opens with, under these settings. */
const matcherFor = (modelPolicy: Settings) =>
  Effect.flatten(makeClaudeAgentModelPolicyHook).pipe(withSettings(modelPolicy));

/** The hook's answer for one call, under these settings. */
const answer = (modelPolicy: Settings, toolInput: unknown, tool = "Agent", place = NOWHERE) =>
  Effect.gen(function* () {
    const matcher = yield* Effect.flatten(makeClaudeAgentModelPolicyHook);
    return yield* Effect.promise(() =>
      matcher.hooks[0]!(callInput(toolInput, tool, place), "toolu_agent", {
        signal: new AbortController().signal,
      }),
    );
  }).pipe(withSettings(modelPolicy));

const decision = (output: unknown) =>
  (output as { hookSpecificOutput?: { permissionDecision?: string } }).hookSpecificOutput
    ?.permissionDecision;
const reason = (output: unknown) =>
  (output as { hookSpecificOutput?: { permissionDecisionReason?: string } }).hookSpecificOutput
    ?.permissionDecisionReason ?? "";
const context = (output: unknown) =>
  (output as { hookSpecificOutput?: { additionalContext?: string } }).hookSpecificOutput
    ?.additionalContext;

const START_PASSES =
  'Agent calls that pass: model "opus" (claude-opus-5-5 here) with effort medium, high, xhigh; model "fable" (claude-fable-5-2 here) with effort medium, high, xhigh.';

describe("claudeAgentCallPick", () => {
  it("reads the alias as the full id this machine's Claude list maps it to, keeping the alias", () => {
    assert.deepStrictEqual(
      claudeAgentCallPick(BUNDLED_CLAUDE_MODEL_CATALOG, { model: "opus", effort: "high" }),
      {
        driver: "claudeAgent",
        model: "claude-opus-5-5",
        namedAs: "opus",
        options: [{ id: "effort", value: "high" }],
      },
    );
    // A name the list does not map stays as given; inherit and none name no model.
    assert.deepStrictEqual(claudeAgentCallPick(BUNDLED_CLAUDE_MODEL_CATALOG, { model: "x-1" }), {
      driver: "claudeAgent",
      model: "x-1",
    });
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
          // The refusal says what passes on this tool, which names only aliases.
          assert.include(reason(output), START_PASSES);
          assert.include(
            reason(output),
            "Name one of these models in the Agent call's model, with one of its efforts in effort.",
          );
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

  it.effect("a row saved with the alias matches the alias the call names, in both modes", () =>
    Effect.gen(function* () {
      for (const mode of ["enforce", "advise"]) {
        const rows = policy(mode, ALIAS_ROWS);
        // Old state: a row saved as `opus` lets the Agent call `opus` run, as delegate_task does.
        const inside = yield* answer(rows, { model: "opus", effort: "high" });
        assert.isUndefined(decision(inside), mode);
        assert.isUndefined(context(inside), mode);
        const outside = yield* answer(rows, { model: "sonnet", effort: "high" });
        const text = mode === "enforce" ? reason(outside) : (context(outside) ?? "");
        assert.strictEqual(decision(outside), mode === "enforce" ? "deny" : undefined);
        assert.include(
          text,
          'Agent calls that pass: model "opus" (claude-opus-5-5 here) with effort high.',
        );
        // The effort is still the row's.
        const effort = yield* answer(rows, { model: "opus", effort: "max" });
        assert.strictEqual(decision(effort), mode === "enforce" ? "deny" : undefined);
      }
      // A full-id row matches the alias that names it (both modes: the start rows above).
      const advised = yield* answer(ADVISE, { model: "fable", effort: "high" });
      assert.isUndefined(decision(advised));
      assert.notInclude(context(advised) ?? "", "outside the policy");
    }),
  );

  it.effect(
    "rows no alias reaches: the refusal says no Agent call passes and what still runs",
    () =>
      Effect.gen(function* () {
        const rows = policy("enforce", [
          { driver: "claudeAgent", model: "claude-opus-4-7", efforts: ["high"] },
          { driver: "claudeAgent", model: "opus", efforts: ["ultracode"] },
          { driver: "codex", model: "gpt-5.5", efforts: ["high"] },
        ]);
        const output = yield* answer(rows, { model: "opus", effort: "high" });
        assert.strictEqual(decision(output), "deny");
        assert.include(
          reason(output),
          "No Agent call passes: no claudeAgent row names a model that an Agent alias (sonnet, opus, haiku, fable) runs here, at an effort the Agent tool can name.",
        );
        assert.include(reason(output), "through delegate_task, which takes the full id");
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
      const matcher = yield* matcherFor(ENFORCE);
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
      assert.include(reason(output), "could not check this call");
    }),
  );

  it.effect("a hook that fails or times out refuses while a policy is on (onFailure block)", () =>
    Effect.gen(function* () {
      for (const settings of [ENFORCE, ADVISE, "unreadable"] as const) {
        assert.strictEqual((yield* matcherFor(settings)).onFailure, "block", String(settings));
      }
      // Off: left out, so a Claude Code too old to know the option still opens its queries.
      for (const settings of [OFF, "no-settings"] as const) {
        assert.isUndefined((yield* matcherFor(settings)).onFailure, String(settings));
      }
      assert.strictEqual((yield* matcherFor(ENFORCE)).matcher, "^(Agent|Task|Skill)$");
    }),
  );
});

describe("claudeConfigDirOfTranscript", () => {
  it.effect("reads the config folder above projects/, for a session and a sub-agent", () =>
    Effect.gen(function* () {
      const path = yield* Path.Path;
      assert.strictEqual(
        claudeConfigDirOfTranscript(path, "/h/.claude/projects/-w/s.jsonl"),
        "/h/.claude",
      );
      assert.strictEqual(
        claudeConfigDirOfTranscript(path, "/h/cfg/projects/-w/s/subagents/agent-a.jsonl"),
        "/h/cfg",
      );
      for (const odd of ["/tmp/t.jsonl", "relative/projects/-w/s.jsonl", undefined, 3]) {
        assert.isUndefined(claudeConfigDirOfTranscript(path, odd), String(odd));
      }
    }).pipe(Effect.provide(NodeServices.layer)),
  );
});

describe("a skill that starts a sub-agent under the model policy (#128)", () => {
  /** A config folder and a project, each with skills; the call's place in that project. */
  const fixture = Effect.gen(function* () {
    const fileSystem = yield* FileSystem.FileSystem;
    const path = yield* Path.Path;
    const root = yield* fileSystem.makeTempDirectoryScoped({ prefix: "t3-policy-skills-" });
    const config = path.join(root, "claude");
    const project = path.join(root, "project");
    const write = (dir: string, name: string, text: string) =>
      Effect.gen(function* () {
        yield* fileSystem.makeDirectory(path.join(dir, name), { recursive: true });
        yield* fileSystem.writeFileString(path.join(dir, name, "SKILL.md"), text);
      });
    const user = path.join(config, "skills");
    const own = path.join(project, ".claude", "skills");
    yield* write(user, "fork-opus", "---\ncontext: fork\nmodel: opus\neffort: high\n---\nDo it.");
    yield* write(user, "fork-sonnet", "---\ncontext: fork\nmodel: sonnet\neffort: high\n---\n");
    yield* write(user, "fork-none", "---\ncontext: fork\nagent: Explore\n---\nLook.");
    yield* write(user, "fork-inherit", "---\ncontext: fork\nmodel: inherit\neffort: high\n---\n");
    yield* write(user, "inline", "---\ndescription: plain\n---\nInline.");
    yield* write(user, "no-frontmatter", "Just text.");
    yield* write(user, "inline-model", "---\nmodel: claude-sonnet-5-5\neffort: high\n---\n");
    // Frontmatter that does not start the file: Claude Code may still read it.
    yield* write(user, "late", "\n---\ncontext: fork\nmodel: sonnet\n---\n");
    yield* write(user, "bad-yaml", "---\ncontext: fork\ncontext: inline\n---\n");
    // An unquoted ": " in a value: Claude Code and T3's skill list read it (its repair step).
    const colon = "description: Review the code: bugs and style";
    yield* write(user, "inline-colon", `---\n${colon}\n---\nInline.`);
    yield* write(user, "fork-colon", `---\n${colon}\ncontext: fork\nmodel: sonnet\n---\n`);
    // Still broken after that repair (it leaves a value starting with "[" alone).
    yield* write(user, "colon-broken", `---\n${colon}\ncontext: fork\nmodel: [opus\n---\n`);
    yield* write(own, "project-fork", "---\ncontext: fork\nmodel: haiku\neffort: high\n---\n");
    yield* write(
      own,
      "project-alias",
      '---\ncontext: "fork"\nmodel: "fable"\neffort: xhigh\n---\n',
    );
    return {
      session: { transcript: path.join(config, "projects", "-p", "s.jsonl"), cwd: project },
      subagent: {
        transcript: path.join(config, "projects", "-p", "s", "subagents", "agent-a.jsonl"),
        cwd: project,
      },
    };
  });
  const skill = (settings: Settings, name: unknown, place: Place) =>
    answer(settings, { skill: name }, "Skill", place);

  it.effect("enforce: checks the frontmatter of a skill that runs forked or names a model", () =>
    Effect.gen(function* () {
      const { session, subagent } = yield* fixture;
      // Inside the rows, by alias or full id, from the user's skills or the project's.
      for (const name of ["fork-opus", "/fork-opus", "project-alias"]) {
        for (const place of [session, subagent]) {
          assert.isUndefined(decision(yield* skill(ENFORCE, name, place)), name);
        }
      }
      // Runs inline and names no model: it runs on the session's own model, nothing to check.
      for (const name of ["inline", "no-frontmatter", "inline-colon"]) {
        assert.deepStrictEqual(yield* skill(ENFORCE, name, session), {}, name);
        assert.deepStrictEqual(yield* skill(ADVISE, name, session), {}, name);
      }
      for (const [name, why] of [
        ["fork-sonnet", "claude-sonnet-5-5 (claudeAgent) is in no row"],
        ["fork-none", "the call names no model"],
        ["fork-inherit", "the call names no model"],
        ["inline-model", "claude-sonnet-5-5 (claudeAgent) is in no row"],
        ["fork-colon", "claude-sonnet-5-5 (claudeAgent) is in no row"],
        ["project-fork", "claude-haiku-5-5 (claudeAgent) is in no row"],
      ] as const) {
        // Runs again: the same answer.
        for (const _ of [1, 2]) {
          const output = yield* skill(ENFORCE, name, session);
          assert.strictEqual(decision(output), "deny", name);
          assert.include(reason(output), `Model policy refused this pick: ${why}.`, name);
          assert.include(reason(output), "the skill's frontmatter: model", name);
        }
      }
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect("fails closed: a skill T3 cannot find or read is refused under enforce", () =>
    Effect.gen(function* () {
      const { session } = yield* fixture;
      for (const [name, place, why] of [
        ["simplify", session, "T3 does not find the skill simplify among Claude's skills"],
        ["", session, "the Skill call names no skill"],
        [3, session, "the Skill call names no skill"],
        ["late", session, "T3 cannot read the frontmatter of the skill late"],
        ["bad-yaml", session, "T3 does not find the skill bad-yaml"],
        ["colon-broken", session, "T3 does not find the skill colon-broken"],
        [
          "fork-opus",
          { ...session, transcript: "/elsewhere/t.jsonl" },
          "T3 cannot tell which Claude config folder holds the skill fork-opus",
        ],
      ] as const) {
        const output = yield* skill(ENFORCE, name, place);
        assert.strictEqual(decision(output), "deny", String(name));
        assert.include(reason(output), `Model policy refused this call (fail closed): ${why}`);
      }
      // The way out names a copy under another name: Claude Code runs some bundled skills
      // (cannotBeShadowed) over a project copy of the same name, which T3 would read.
      const wayOut = reason(yield* skill(ENFORCE, "simplify", session)) ?? "";
      assert.include(wayOut, "under another name");
      assert.include(wayOut, "cannot be replaced by a copy of the same name");
      // Advise runs it with a note; off reads nothing.
      const advised = yield* skill(ADVISE, "simplify", session);
      assert.isUndefined(decision(advised));
      assert.include(context(advised) ?? "", "Model policy (advise): T3 does not find the skill");
      assert.deepStrictEqual(yield* skill(OFF, "fork-sonnet", session), {});
      assert.deepStrictEqual(yield* skill("no-settings", "simplify", session), {});
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );
});
