// @effect-diagnostics nodeBuiltinImport:off
// Fork: pins the ORDER of the delegation-check seats in the upstream file (delegateChecks.test.ts
// counts them). A seat moved after its dispatch, or the early release dropped, turns this red.
import * as NodeFS from "node:fs";
import { assert, describe, it } from "@effect/vitest";

const source = NodeFS.readFileSync(
  new URL("../mcp/OrchestratorMcpService.ts", import.meta.url),
  "utf8",
);
const body = (method: string) => {
  const start = source.indexOf(`${method}: Effect.fn("OrchestratorMcpService.${method}")`);
  assert.isAtLeast(start, 0);
  const next = source.indexOf(': Effect.fn("OrchestratorMcpService.', start + method.length + 20);
  return source.slice(start, next === -1 ? undefined : next);
};
const ordered = (text: string, needles: ReadonlyArray<string>) => {
  const at = needles.map((needle) => text.indexOf(needle));
  for (const [i, pos] of at.entries()) assert.isAtLeast(pos, 0, `missing ${needles[i]}`);
  for (let i = 1; i < at.length; i++)
    assert.isBelow(at[i - 1]!, at[i]!, `${needles[i - 1]} before ${needles[i]}`);
};

describe("fork delegation checks: seat order", () => {
  it("delegate_task: seat 1, workspace, seat 2, dispatch, started, release", () =>
    ordered(body("delegateTask"), [
      "delegateChecks.beforeWorkspace(",
      "workspaceStrategy = {",
      "delegateChecks.afterWorkspace(",
      'type: "delegated_task.request"',
      "forkHold.started(",
      "forkHold.release",
    ]));
  it("create_threads: target lookup, seat, runtime mode, thread.create dispatch", () =>
    ordered(body("createThreads"), [
      "resolveTargetRechecking(",
      "delegateChecks.beforeCreateThread(",
      "resolveRuntimeMode(",
      'type: "thread.create"',
    ]));
});
