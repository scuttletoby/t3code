/**
 * Fork: `modelPolicy` for calls that start Claude's own sub-agents, as each is made: the Agent tool
 * (Task in older Claude Code), and Skill for a skill that forks or names a model or effort. An alias
 * matches a row naming it or its full id here; a `fork` call must name a model too.
 */
import type {
  HookCallback,
  HookCallbackMatcher,
  PreToolUseHookInput,
} from "@anthropic-ai/claude-agent-sdk";
import type { AgentInput } from "@anthropic-ai/claude-agent-sdk/sdk-tools";
import {
  type ModelPolicyRow,
  modelPolicyRowsText,
  modelPolicyStance,
  ProviderDriverKind,
} from "@t3tools/contracts";
import * as ModelCatalog from "@t3tools/provider-core/server/ModelCatalog";
import * as HostProcess from "@t3tools/shared/HostProcess";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Option from "effect/Option";
import * as Path from "effect/Path";

import {
  BUNDLED_CLAUDE_MODEL_CATALOG,
  type ClaudeModelCatalog,
  resolveClaudeModelCatalog,
  resolveClaudeModelSlug,
} from "../provider/ClaudeModelCatalog.ts";
import { discoverClaudeSkills, parseSkillFrontmatter } from "../provider/Drivers/ClaudeSkills.ts";
import * as ServerSettings from "../serverSettings.ts";
import { makePickCheck, type PolicyRoute, type ServerPick } from "./modelPolicyCheck.ts";

const CLAUDE_DRIVER = "claudeAgent";
/** Claude Code's tools that start a sub-agent: Agent, and Task in older versions. */
export const CLAUDE_AGENT_TOOLS: ReadonlyArray<string> = ["Agent", "Task"];
/** Claude Code's tool that runs a skill, in a sub-agent when its frontmatter says `context: fork`. */
const CLAUDE_SKILL_TOOL = "Skill";

/** The Agent tool's model names and efforts (the SDK's `AgentInput`): a change there fails the typecheck. */
const AGENT_MODELS: Record<NonNullable<AgentInput["model"]>, true> = {
  sonnet: true,
  opus: true,
  haiku: true,
  fable: true,
};
const AGENT_EFFORTS: Record<NonNullable<AgentInput["effort"]>, true> = {
  low: true,
  medium: true,
  high: true,
  xhigh: true,
  max: true,
};

/**
 * The pick a Claude model name and effort make: `inherit` and a missing model name no model. An alias
 * is read as the full id this machine's Claude list maps it to, keeping the name given (`namedAs`),
 * so a row naming either matches.
 */
function claudeModelPick(catalog: ClaudeModelCatalog, model: unknown, effort: unknown): ServerPick {
  const named = typeof model === "string" ? model.trim() : "";
  const resolved =
    named === "" || named === "inherit" ? undefined : resolveClaudeModelSlug(catalog, named);
  return {
    driver: CLAUDE_DRIVER,
    model: resolved,
    ...(resolved !== undefined && resolved !== named ? { namedAs: named } : {}),
    // A value that is not a string reads as an effort with no name, which is refused.
    ...(effort === undefined || effort === null
      ? {}
      : { options: [{ id: "effort", value: effort }] }),
  };
}

const record = (value: unknown): Record<string, unknown> =>
  typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};

/** The pick an Agent call makes (a fork's too). */
export function claudeAgentCallPick(catalog: ClaudeModelCatalog, input: unknown): ServerPick {
  const args = record(input);
  return claudeModelPick(catalog, args.model, args.effort);
}

/**
 * What passes the rows on the Agent tool, which names only aliases: each alias whose name or full id
 * here a Claude row names, with the efforts the tool can name. None: says so, and where a row's model
 * can still run.
 */
function claudeAgentPassesText(
  catalog: ClaudeModelCatalog,
  allow: ReadonlyArray<ModelPolicyRow>,
): string {
  const aliases = Object.keys(AGENT_MODELS);
  const passes = aliases.flatMap((alias) => {
    const id = resolveClaudeModelSlug(catalog, alias);
    const rows = allow.filter(
      (row) => row.driver === CLAUDE_DRIVER && (row.model === alias || row.model === id),
    );
    const efforts = [
      ...new Set(
        rows.flatMap(
          (row) => row.efforts?.filter((one) => Object.hasOwn(AGENT_EFFORTS, one)) ?? [],
        ),
      ),
    ];
    const ways = [
      ...(efforts.length === 0 ? [] : [`effort ${efforts.join(", ")}`]),
      ...(rows.some((row) => row.efforts === undefined) ? ["no effort"] : []),
    ];
    return ways.length === 0
      ? []
      : [`model "${alias}"${id === alias ? "" : ` (${id} here)`} with ${ways.join(" or ")}`];
  });
  return passes.length > 0
    ? `Agent calls that pass: ${passes.join("; ")}.`
    : `No Agent call passes: no ${CLAUDE_DRIVER} row names a model that an Agent alias (${aliases.join(", ")}) runs here, at an effort the Agent tool can name. Rows: ${modelPolicyRowsText(allow)}. A row's model can still run as a task through delegate_task, which takes the full id.`;
}

/**
 * Claude Code's config folder for this call: the folder above `projects/` in the hook's transcript
 * path (`<config>/projects/<project>/<session>.jsonl`, or a sub-agent's
 * `<config>/projects/<project>/<session>/subagents/<agent>.jsonl`). `undefined` for another shape.
 */
export function claudeConfigDirOfTranscript(
  path: Path.Path,
  transcript: unknown,
): string | undefined {
  if (typeof transcript !== "string" || !path.isAbsolute(transcript)) return undefined;
  const up = (from: string, levels: number): string =>
    levels === 0 ? from : up(path.dirname(from), levels - 1);
  const subagent = path.basename(path.dirname(transcript)) === "subagents";
  const projects = up(transcript, subagent ? 4 : 2);
  return path.basename(projects) === "projects" ? path.dirname(projects) : undefined;
}

/**
 * A SKILL.md's frontmatter as T3's skill list reads it (`parseSkillFrontmatter`, which repairs the
 * unquoted `: ` in a value that Claude Code accepts); `undefined` when it cannot be read.
 */
function skillFrontmatter(text: string): Record<string, unknown> | undefined {
  const read = parseSkillFrontmatter(text);
  // Something that looks like frontmatter but is not read as such here may be read by Claude Code.
  if (read.kind === "missing") return /^\uFEFF?\s*---/.test(text) ? undefined : {};
  return read.kind === "parsed" && !Array.isArray(read.record) ? read.record : undefined;
}

const unreadableSkill = (why: string): ServerPick => ({
  driver: CLAUDE_DRIVER,
  model: undefined,
  unreadable: `${why}, so T3 cannot tell whether it starts a sub-agent (context: fork) on a model outside the policy. Do the work without it, or a person puts a copy under another name in the project's .claude/skills that names a model from the rows in its frontmatter model and effort (some skills bundled with Claude Code cannot be replaced by a copy of the same name: Claude Code runs its own)`,
});

/**
 * The pick a Skill call makes: its skill's frontmatter `model` and `effort` when it runs in a
 * sub-agent (`context: fork`) or names either; `undefined` for a skill that runs inline and names
 * neither (it runs on the session's own model). Found as T3 finds Claude's skills
 * (`discoverClaudeSkills`, first name wins); a skill that cannot be found or read is unreadable.
 */
const claudeSkillCallPick = Effect.fn("claudeSkillCallPick")(function* (
  catalog: ClaudeModelCatalog,
  input: Pick<PreToolUseHookInput, "tool_input" | "transcript_path" | "cwd">,
) {
  const fileSystem = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const raw = record(input.tool_input).skill;
  const name = typeof raw === "string" ? raw.trim().replace(/^\//, "") : "";
  if (name === "") return unreadableSkill("the Skill call names no skill");
  const configDir = claudeConfigDirOfTranscript(path, input.transcript_path);
  if (configDir === undefined) {
    return unreadableSkill(`T3 cannot tell which Claude config folder holds the skill ${name}`);
  }
  const skills = yield* discoverClaudeSkills(
    { homePath: configDir },
    input.cwd,
    yield* HostProcess.Environment,
  );
  const skill = skills.find((candidate) => candidate.name === name);
  if (skill === undefined) {
    return unreadableSkill(
      `T3 does not find the skill ${name} among Claude's skills it reads (managed, user, project and plugin skills): a skill bundled with Claude Code, an MCP server's, a command or one in another folder cannot be read`,
    );
  }
  const text = yield* fileSystem
    .readFileString(skill.path)
    .pipe(Effect.orElseSucceed(() => undefined));
  const frontmatter = text === undefined ? undefined : skillFrontmatter(text);
  if (frontmatter === undefined) {
    return unreadableSkill(`T3 cannot read the frontmatter of the skill ${name}`);
  }
  const { context, model, effort } = frontmatter;
  const named = (value: unknown) => value !== undefined && value !== null;
  if (context !== "fork" && !named(model) && !named(effort)) return undefined;
  return claudeModelPick(catalog, model, effort);
});

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
 * The matcher each query opens with. Fails closed: an error while checking refuses (a throw would let
 * the call through), and while a policy is on `onFailure: "block"` refuses a timeout too; off, it is
 * left out, as a Claude Code older than 2.1.295 fails every query that sets it.
 */
export const makeClaudeAgentModelPolicyHook = Effect.gen(function* () {
  const check = yield* makePickCheck;
  const settings = yield* Effect.serviceOption(ServerSettings.ServerSettingsService);
  const catalogService = yield* Effect.serviceOption(ModelCatalog.ModelCatalog);
  const fileSystem = yield* Effect.serviceOption(FileSystem.FileSystem);
  const path = yield* Effect.serviceOption(Path.Path);
  const loadCatalog = Option.match(catalogService, {
    onNone: () => Effect.succeed(BUNDLED_CLAUDE_MODEL_CATALOG),
    onSome: (service) =>
      service.current(ProviderDriverKind.make(CLAUDE_DRIVER)).pipe(
        Effect.map(resolveClaudeModelCatalog),
        Effect.orElseSucceed(() => BUNDLED_CLAUDE_MODEL_CATALOG),
      ),
  });
  const readSkill = (catalog: ClaudeModelCatalog, input: PreToolUseHookInput) =>
    Option.isSome(fileSystem) && Option.isSome(path)
      ? claudeSkillCallPick(catalog, input).pipe(
          Effect.provideService(FileSystem.FileSystem, fileSystem.value),
          Effect.provideService(Path.Path, path.value),
        )
      : Effect.succeed(unreadableSkill("T3 cannot read files here"));
  const run = Effect.runPromiseWith(yield* Effect.context<never>());

  const hook: HookCallback = async (input) => {
    try {
      if (input.hook_event_name !== "PreToolUse") return {};
      const skill = input.tool_name === CLAUDE_SKILL_TOOL;
      if (!skill && !CLAUDE_AGENT_TOOLS.includes(input.tool_name)) return {};
      const route: PolicyRoute = skill ? "Skill" : "Agent";
      const answer = await run(
        Effect.gen(function* () {
          // Read only while a policy is on: `check` skips it when off.
          const readPick = Effect.flatMap(loadCatalog, (catalog) =>
            skill
              ? readSkill(catalog, input)
              : Effect.succeed({
                  ...claudeAgentCallPick(catalog, input.tool_input),
                  allowedText: (allow: ReadonlyArray<ModelPolicyRow>) =>
                    claudeAgentPassesText(catalog, allow),
                }),
          );
          return { note: yield* check(route, readPick) };
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
        "Model policy: T3 could not check this call against the model policy, so it refused it. Try again; if it stays, name a model and effort from the policy's rows.",
      );
    }
  };
  const matcher: HookCallbackMatcher = {
    matcher: `^(${[...CLAUDE_AGENT_TOOLS, CLAUDE_SKILL_TOOL].join("|")})$`,
    hooks: [hook],
  };
  const blocking: HookCallbackMatcher = { ...matcher, onFailure: "block" };
  const policyOn = Option.match(settings, {
    onNone: () => Effect.succeed(false),
    onSome: (service) =>
      Effect.map(Effect.exit(service.getSettings), (read) =>
        Exit.isSuccess(read) ? modelPolicyStance(read.value.modelPolicy).mode !== "off" : true,
      ),
  });
  return Effect.map(policyOn, (on) => (on ? blocking : matcher));
});
