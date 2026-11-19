import { ProjectId, ServerSettings, ServerSettingsError, ThreadId } from "@t3tools/contracts";
import { assert, describe, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Schema from "effect/Schema";
import * as TestClock from "effect/testing/TestClock";

import * as ServerSettingsService from "../serverSettings.ts";
import {
  CHECK_SESSION_TIMEOUT,
  ClaudeMcpProfileError,
  type ClaudeMcpProfilePlan,
  type LineageThread,
  checkClaudeSession,
  claudeMcpCallOutsideProfile,
  claudeMcpProfileForThread,
  claudeMcpProfilePlan,
  claudeMcpServersOutsidePlan,
  makeClaudeMcpProfiles,
  planKeyFor,
} from "./claudeMcpProfile.ts";

const fromFile = Schema.decodeSync(ServerSettings);
const isRefusal = Schema.is(ClaudeMcpProfileError);
const T3 = ["t3-code"];

const thread = (id: string, parent: string | null = null): LineageThread => ({
  id: ThreadId.make(id),
  projectId: ProjectId.make("project"),
  lineage: {
    parentThreadId: parent === null ? null : ThreadId.make(parent),
    relationshipToParent: parent === null ? null : "subagent",
    rootThreadId: ThreadId.make("root"),
  },
});
const store = (threads: ReadonlyArray<LineageThread>) => (threadId: ThreadId) =>
  Effect.succeed(threads.find((entry) => entry.id === threadId) ?? null);
const noReads = () => Effect.die("read a parent thread");

const outcome = (result: ClaudeMcpProfilePlan | ClaudeMcpProfileError) =>
  isRefusal(result) ? result.message : result.options;

describe("claudeMcpProfilePlan", () => {
  it("refuses a profile that limits T3's own tools, which nothing applies yet", () => {
    const result = claudeMcpProfilePlan("lean", { servers: [], t3Tools: ["threads"] }, T3);
    assert.include(String(outcome(result)), 'MCP profile "lean" limits T3\'s own tools');
  });
});

describe("claudeMcpServersOutsidePlan", () => {
  const statuses = [
    { name: "t3-code", status: "connected" as const },
    { name: "docs", status: "pending" as const },
    { name: "claude.ai Calendar", status: "needs-auth" as const },
    { name: "off", status: "disabled" as const },
  ];
  it.each([
    { servers: "all" as const, outside: [] },
    { servers: [], outside: ["docs", "claude.ai Calendar"] },
    { servers: ["docs"], outside: ["claude.ai Calendar"] },
  ])("names what $servers leaves out, at any status but disabled", ({ servers, outside }) => {
    const plan = { name: "p", options: {}, servers };
    assert.deepStrictEqual(claudeMcpServersOutsidePlan(plan, T3, statuses), outside);
  });
});

describe("claudeMcpCallOutsideProfile", () => {
  const call = (tool_name: string, extra: object = {}) => ({ tool_name, tool_input: {}, ...extra });
  it.each([
    { name: "a tool of a named server", call: call("mcp__docs__search"), outside: undefined },
    { name: "a tool of T3's own server", call: call("mcp__t3-code__x"), outside: undefined },
    { name: "a tool of another server", call: call("mcp__github__x"), outside: "github" },
    {
      name: "a server Claude names, by its configured name",
      call: call("mcp__claude_ai_Calendar__x", { mcp_server: { name: "claude.ai Calendar" } }),
      outside: "claude.ai Calendar",
    },
    {
      name: "a named server whose name Claude rewrites in tool names",
      call: call("mcp__claude_ai_Drive__x"),
      outside: undefined,
    },
    {
      name: "a resource read from another server",
      call: { tool_name: "ReadMcpResourceTool", tool_input: { server: "github", uri: "x" } },
      outside: "github",
    },
    { name: "a tool that is not MCP", call: call("Bash"), outside: undefined },
  ])("$name", ({ call: input, outside }) => {
    assert.strictEqual(
      claudeMcpCallOutsideProfile(["docs", "claude.ai Drive"], T3, input),
      outside,
    );
  });
});

describe("planKeyFor", () => {
  it("gives overrides with no worked-out plan a key no other turn has, never all's", () => {
    const first = planKeyFor({});
    assert.isDefined(first);
    assert.notStrictEqual(planKeyFor({}), first);
  });
});

describe("makeClaudeMcpProfiles", () => {
  const readFailure = new ServerSettingsError({
    settingsPath: "settings.json",
    operation: "read-secret",
    cause: new Error("secret store unreadable"),
  });
  /** Plans a thread once per entry: a settings file, or a failed read (it reads secrets too). */
  const plansFor = (reads: ReadonlyArray<Record<string, unknown> | "fails">) =>
    Effect.gen(function* () {
      const service = yield* ServerSettingsService.ServerSettingsService;
      let next = 0;
      const getSettings = Effect.suspend(() => {
        const read = reads[next++];
        return read === "fails" ? Effect.fail(readFailure) : Effect.succeed(fromFile(read ?? {}));
      });
      const plan = yield* makeClaudeMcpProfiles.pipe(
        Effect.provideService(ServerSettingsService.ServerSettingsService, {
          ...service,
          getSettings,
        }),
      );
      const outcomes: Array<string> = [];
      for (const _ of reads) {
        outcomes.push(
          yield* plan(thread("t"), {}).pipe(
            Effect.match({
              onFailure: (refusal) => refusal.message,
              onSuccess: (result) => result.name,
            }),
          ),
        );
      }
      return outcomes;
    }).pipe(Effect.provide(ServerSettingsService.ServerSettingsService.layerTest()));

  it.effect("with no profile set, a failed settings read gives all instead of refusing", () =>
    Effect.gen(function* () {
      assert.deepStrictEqual(yield* plansFor(["fails", {}, "fails"]), ["all", "all", "all"]);
    }),
  );

  it.effect("with a profile set when last read, a failed settings read refuses", () =>
    Effect.gen(function* () {
      const [first, second] = yield* plansFor([{ mcpProfile: "t3-only" }, "fails"]);
      assert.strictEqual(first, "t3-only");
      assert.include(second, "could not read this thread's MCP profile settings");
    }),
  );
});

describe("checkClaudeSession", () => {
  it.effect("refuses and closes a session that does not list its servers in time", () =>
    Effect.gen(function* () {
      let closed = 0;
      const plan = { name: "t3-only", options: {}, servers: [] };
      const check = yield* checkClaudeSession(
        plan,
        {},
      )({
        mcpServerStatus: Effect.never,
        close: Effect.sync(() => void closed++),
      }).pipe(Effect.flip, Effect.forkChild);
      yield* TestClock.adjust(CHECK_SESSION_TIMEOUT);
      const refusal = yield* Fiber.join(check);
      assert.include(refusal.message, "could not check which MCP servers Claude loaded");
      assert.strictEqual(closed, 1);
    }),
  );
});

describe("claudeMcpProfileForThread", () => {
  const forThread = (
    file: Record<string, unknown>,
    self: LineageThread,
    findThread?: (threadId: ThreadId) => Effect.Effect<LineageThread | null>,
  ) =>
    claudeMcpProfileForThread(fromFile(file), self, T3, findThread).pipe(
      Effect.match({ onFailure: (refusal) => refusal.message, onSuccess: (plan) => plan.servers }),
    );

  it.effect("with no profile set, a child gets all and no parent is read", () =>
    Effect.gen(function* () {
      assert.strictEqual(yield* forThread({}, thread("child", "gone"), noReads), "all");
    }),
  );

  it.effect("an inheriting child is cut to its parent's pick, read from the store", () =>
    Effect.gen(function* () {
      const file = { mcpThreadProfiles: { parent: "t3-only" } };
      const child = yield* forThread(file, thread("child", "parent"), store([thread("parent")]));
      assert.deepStrictEqual(child, []);
    }),
  );

  it.effect("a parent that cannot be read refuses the child, unless the child has a pick", () =>
    Effect.gen(function* () {
      const orphan = yield* forThread(
        { mcpThreadProfiles: { other: "t3-only" } },
        thread("c", "x"),
      );
      assert.include(String(orphan), "parent thread cannot be read");
      const picked = yield* forThread({ mcpThreadProfiles: { c: "t3-only" } }, thread("c", "x"));
      assert.deepStrictEqual(picked, []);
    }),
  );

  it.effect("a looping lineage is refused, never widened to all", () =>
    Effect.gen(function* () {
      const file = { mcpThreadProfiles: { other: "all" } };
      const looping = yield* forThread(file, thread("a", "b"), store([thread("b", "a")]));
      assert.include(String(looping), "repeat a thread");
    }),
  );
});
