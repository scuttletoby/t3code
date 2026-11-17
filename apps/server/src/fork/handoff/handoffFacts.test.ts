/** Fork: the pure handoff facts (the quiet rule, the message ids) and the message text. */
import { describe, expect, it } from "@effect/vitest";
import {
  MessageId,
  type OrchestrationV2ThreadProjection,
  ProviderThreadId,
  RunId,
  ThreadId,
} from "@t3tools/contracts";

import {
  at,
  handoffMessageId,
  modelSelection,
  projection,
  run,
  runId,
  threadId,
} from "./handoff.testkit.ts";
import { handoffBlockers, handoffThreadFacts, parseHandoffMessageId } from "./handoffFacts.ts";
import { handoffMessageText } from "./handoffMessage.ts";

const base = projection({ usedTokens: 160_000 });
type Records = OrchestrationV2ThreadProjection;
const kinds = (records: Records) => handoffBlockers(records).map((blocker) => blocker.kind);

describe("parseHandoffMessageId", () => {
  it("reads the source run and the retry, and nothing else", () => {
    expect(parseHandoffMessageId(threadId, handoffMessageId)).toEqual({
      runId,
      retry: false,
    });
    expect(parseHandoffMessageId(threadId, `${handoffMessageId}:retry1`)).toEqual({
      runId,
      retry: true,
    });
    expect(parseHandoffMessageId(ThreadId.make("other"), handoffMessageId)).toBeNull();
    expect(parseHandoffMessageId(threadId, "message-1")).toBeNull();
    expect(parseHandoffMessageId(threadId, `handoff:${threadId}:`)).toBeNull();
  });
});

describe("handoffBlockers", () => {
  it("is empty for a quiet thread", () => {
    expect(handoffBlockers(base)).toEqual([]);
  });

  it.each([
    ["run-active", { runs: [run({ status: "running", completedAt: null })] }],
    ["run-active", { runs: [run(), run({ id: RunId.make("run-2"), status: "queued" })] }],
    ["request-pending", { runtimeRequests: [{ status: "pending" }] }],
    [
      "child-result-waiting",
      {
        runs: [
          run({
            delegatedCompletion: {
              disposition: "open",
              nextGeneration: 2,
              delivery: { generation: 1, messageId: MessageId.make("due"), taskIds: [] },
            },
          }),
        ],
      },
    ],
    ["fork-or-merge", { contextTransfers: [{ status: "pending" }] }],
    [
      "background-work",
      {
        providerThreads: [
          {
            ...base.providerThreads[0]!,
            pendingBackgroundTasks: [{ taskId: "bg-1", kind: "command", description: "tests" }],
          },
        ],
      },
    ],
    [
      "goal-active",
      { providerThreads: [{ ...base.providerThreads[0]!, goal: { status: "active" } }] },
    ],
  ] as ReadonlyArray<readonly [string, unknown]>)("names %s", (kind, patch) => {
    expect(kinds({ ...base, ...(patch as Partial<Records>) })).toEqual([kind]);
  });

  it("does not count a delivered child result that already landed, or another session's work", () => {
    const landed = {
      ...base,
      runs: [
        run({
          delegatedCompletion: {
            disposition: "open",
            nextGeneration: 2,
            delivery: { generation: 1, messageId: MessageId.make("message-1"), taskIds: [] },
          },
        }),
      ],
    };
    expect(kinds(landed)).toEqual([]);
    const otherSession = {
      ...base,
      providerThreads: [
        ...base.providerThreads,
        {
          ...base.providerThreads[0]!,
          id: ProviderThreadId.make("old-session"),
          pendingBackgroundTasks: [{ taskId: "bg-1", kind: "command" }],
        },
      ],
    } as Records;
    expect(kinds(otherSession)).toEqual([]);
  });

  it("does not count a running T3 child, but counts the session's own sub-agent", () => {
    const item = (origin: "app_owned" | "provider_native") =>
      ({
        id: `item-${origin}`,
        runId,
        type: "subagent",
        origin,
        status: "running",
        subagentId: `node-${origin}`,
        prompt: "a child still running",
      }) as unknown as Records["turnItems"][number];
    expect(kinds({ ...base, turnItems: [item("app_owned")] })).toEqual([]);
    expect(kinds({ ...base, turnItems: [item("provider_native")] })).toEqual(["background-work"]);
  });
});

describe("handoffThreadFacts", () => {
  it("reads the latest run's kind and the active session's last usage", () => {
    expect(handoffThreadFacts(base)).toMatchObject({
      threadAvailable: true,
      lastRun: { id: runId, kind: "turn", state: "completed" },
      usedTokens: 160_000,
    });
    const compacted = {
      ...base,
      messages: [{ ...base.messages[0]!, text: " /COMPACT " }],
    };
    expect(handoffThreadFacts(compacted).lastRun?.kind).toBe("compact");
    const handedOff = { ...base, runs: [run({ userMessageId: handoffMessageId })] };
    expect(handoffThreadFacts(handedOff).lastRun?.kind).toBe("handoff");
    // A removed queued message leaves a cancelled run that never started: it is not the latest.
    const removed = {
      ...base,
      runs: [
        run(),
        run({ id: RunId.make("run-2"), ordinal: 2, status: "cancelled", startedAt: null }),
      ],
    };
    expect(handoffThreadFacts(removed).lastRun?.id).toBe(runId);
    const otherSession = {
      ...base,
      thread: { ...base.thread, activeProviderThreadId: ProviderThreadId.make("new-session") },
    };
    expect(handoffThreadFacts(otherSession).usedTokens).toBeUndefined();
    const deleted = { ...base, thread: { ...base.thread, deletedAt: at() } };
    expect(handoffThreadFacts(deleted).threadAvailable).toBe(false);
  });
});

describe("handoffMessageText", () => {
  const child = (patch: Record<string, unknown>) => ({
    id: "task-1",
    origin: "app_owned",
    status: "running",
    title: "Build the thing",
    prompt: "Build the thing in full",
    childThreadId: "child-thread",
    ...patch,
  });
  const text = (records: Partial<Records>, effort?: string) =>
    handoffMessageText({
      records: { ...base, ...records },
      sourceRunId: runId,
      number: 2,
      nowIso: "2026-11-15T12:00:00.000Z",
      modelSelection:
        effort === undefined
          ? modelSelection
          : { ...modelSelection, options: [{ id: "reasoningEffort", value: effort }] },
    });

  it("lists open T3 children, watched PRs and the effort; says no source wrote a handoff", () => {
    const message = text(
      {
        subagents: [
          child({}),
          child({ id: "task-2", status: "waiting", title: null, prompt: "Answer me" }),
          child({ id: "task-3", status: "completed", title: "Done already" }),
          child({ id: "task-4", origin: "provider_native", title: "Native helper" }),
        ] as unknown as Records["subagents"],
        thread: {
          ...base.thread,
          pullRequests: [
            { number: 7, url: "https://example.test/pull/7", watch: { failedChecks: ["lint"] } },
            {
              number: 8,
              url: "https://example.test/pull/8",
              watch: { failedChecks: [], passed: true },
            },
            { number: 9, url: "https://example.test/pull/9" },
          ],
        } as unknown as Records["thread"],
      },
      "xhigh",
    );
    expect(message).toContain(`[T3 handoff 2] Thread ${threadId}, made from run ${runId}`);
    expect(message).toContain("effort xhigh");
    expect(message).toContain("- Build the thing (task task-1, thread child-thread, running)");
    expect(message).toContain(
      "- Answer me (task task-2, thread child-thread, waits for an approval",
    );
    expect(message).not.toContain("Done already");
    expect(message).not.toContain("Native helper");
    expect(message).toContain("- #7 https://example.test/pull/7: checks failed (lint)");
    expect(message).toContain("- #8 https://example.test/pull/8: checks passed");
    expect(message).not.toContain("#9");
    expect(message).toContain("No handoff source is set up yet");
  });

  it("says none when there are no children or watched PRs", () => {
    const message = text({});
    expect(message).toContain("Open child tasks: none.");
    expect(message).toContain("Watched PRs: none.");
    expect(message).toContain("no effort set (warning)");
  });
});
