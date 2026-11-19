import { ProjectId, ServerSettings, ThreadId } from "@t3tools/contracts";
import { assert, describe, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import {
  ClaudeMcpProfileError,
  type ClaudeMcpProfilePlan,
  type LineageThread,
  claudeMcpProfileForThread,
  claudeMcpProfilePlan,
  claudeMcpServersOutsidePlan,
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

describe("claudeMcpProfileForThread", () => {
  const forThread = (
    file: Record<string, unknown>,
    self: LineageThread,
    findThread?: (threadId: ThreadId) => Effect.Effect<LineageThread | null>,
  ) =>
    claudeMcpProfileForThread(fromFile(file), self, T3, findThread).pipe(
      Effect.match({ onFailure: (refusal) => refusal.message, onSuccess: (plan) => plan.options }),
    );

  it.effect("with no profile set, a child gets all and no parent is read", () =>
    Effect.gen(function* () {
      assert.deepStrictEqual(yield* forThread({}, thread("child", "gone"), noReads), {});
    }),
  );

  it.effect("an inheriting child is cut to its parent's pick, read from the store", () =>
    Effect.gen(function* () {
      const file = { mcpThreadProfiles: { parent: "t3-only" } };
      const child = yield* forThread(file, thread("child", "parent"), store([thread("parent")]));
      assert.deepStrictEqual(child, { strictMcpConfig: true });
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
      assert.deepStrictEqual(picked, { strictMcpConfig: true });
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
