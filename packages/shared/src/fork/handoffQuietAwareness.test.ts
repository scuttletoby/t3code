import { describe, expect, it } from "@effect/vitest";

import type { EnvironmentId, OrchestrationV2ThreadShell, ThreadId } from "@t3tools/contracts";
import { ProviderInstanceId } from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";

import { projectThreadAwarenessV2 } from "../agentAwareness.ts";

// A handoff write turn the fork marks stop-requested stays quiet whether it completes or fails
// (fork/handoff/handoffShell.ts marks it).
describe("projectThreadAwarenessV2 for a quiet handoff write turn", () => {
  const thread = (status: OrchestrationV2ThreadShell["status"], latestRunStopRequested?: true) => ({
    id: "thread-1" as ThreadId,
    lineage: {
      rootThreadId: "thread-1" as ThreadId,
      parentThreadId: null,
      relationshipToParent: null,
    },
    title: "Example thread",
    modelSelection: { instanceId: ProviderInstanceId.make("codex"), model: "gpt-5.4" },
    status,
    pendingRuntimeRequest: null,
    settledOverride: null,
    updatedAt: DateTime.makeUnsafe("2026-05-22T12:00:00.000Z"),
    ...(latestRunStopRequested ? { latestRunStopRequested } : {}),
  });
  const project = (status: OrchestrationV2ThreadShell["status"], latestRunStopRequested?: true) =>
    projectThreadAwarenessV2({
      environmentId: "env-1" as EnvironmentId,
      project: { title: "Example project" },
      thread: thread(status, latestRunStopRequested),
    });

  it.each(["completed", "failed"] as const)("alerts a %s run that is not marked", (status) => {
    expect(project(status)?.phase).toBe(status);
  });

  it.each(["completed", "failed"] as const)("stays quiet for a marked %s run", (status) => {
    expect(project(status, true)).toBeNull();
  });
});
