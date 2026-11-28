/**
 * Fork: what a delegated task's thread here waits on for a person
 * (fork/localTaskPrompts.ts), and which tasks are read for it.
 */
import { assert, describe, it } from "@effect/vitest";
import { NodeId, RunId, RuntimeRequestId, ThreadId, TurnItemId } from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as SqlClient from "effect/sql/SqlClient";

import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as LocalTaskPrompts from "./localTaskPrompts.ts";

const taskId = NodeId.make("node:local-prompts-task");
const parentThread = ThreadId.make("thread:local-prompts-parent");
const childThread = ThreadId.make("thread:local-prompts-child");
const task = { id: taskId, threadId: parentThread, childThreadId: childThread };
const otherTask = NodeId.make("node:local-prompts-other");
const run = RunId.make("run:local-prompts-task");
const node = NodeId.make("node:local-prompts-run-node");

const at = (ms: number) => DateTime.makeUnsafe(ms);

const request = (
  id: string,
  fields: { kind?: string; status?: string; capability?: string; createdAt?: number } = {},
) => ({
  id: RuntimeRequestId.make(id),
  nodeId: node,
  kind: fields.kind ?? "command",
  status: fields.status ?? "pending",
  responseCapability: { type: fields.capability ?? "live" },
  createdAt: at(fields.createdAt ?? 1_000),
});

const child = (
  fields: {
    runs?: ReadonlyArray<{ id: RunId; status: string; delegatedTaskId?: NodeId }>;
    requests?: ReadonlyArray<ReturnType<typeof request>>;
    items?: ReadonlyArray<Record<string, unknown>>;
    transfers?: ReadonlyArray<Record<string, unknown>>;
  } = {},
) =>
  ({
    thread: { archivedAt: null, deletedAt: null },
    runs: fields.runs ?? [{ id: run, status: "running", delegatedTaskId: taskId }],
    nodes: [{ id: node, runId: run }],
    runtimeRequests: fields.requests ?? [],
    turnItems: fields.items ?? [],
    contextTransfers: fields.transfers ?? [],
  }) as never as LocalTaskPrompts.ChildRecords;

describe("what a task's thread here waits on", () => {
  it("live questions and approvals of the task's runs, approvals oldest first", () => {
    const prompts = LocalTaskPrompts.localPromptsOf(
      task,
      child({
        requests: [
          request("request:later", { kind: "file-change", createdAt: 2_000 }),
          request("request:question", { kind: "user_input" }),
          request("request:first", { kind: "command", createdAt: 1_000 }),
          // Answered, or left over from before a restart: not waited on.
          request("request:resolved", { status: "resolved" }),
          request("request:message", { capability: "message" }),
          // Not an approval and not a question.
          request("request:tool", { kind: "dynamic_tool_call" }),
        ],
      }),
    );
    assert.deepStrictEqual(prompts.questions, [
      { requestId: RuntimeRequestId.make("request:question"), runId: run },
    ]);
    assert.deepStrictEqual(
      prompts.approvals.map((approval) => [approval.requestId, approval.kind]),
      [
        ["request:first", "command"],
        ["request:later", "file-change"],
      ],
    );
  });

  it("a run that ended, or belongs to another task, waits on nothing", () => {
    for (const runs of [
      [{ id: run, status: "completed", delegatedTaskId: taskId }],
      [{ id: run, status: "interrupted", delegatedTaskId: taskId }],
      [{ id: run, status: "running", delegatedTaskId: otherTask }],
    ]) {
      const prompts = LocalTaskPrompts.localPromptsOf(
        task,
        child({
          runs,
          requests: [request("request:approval")],
          items: [
            {
              id: TurnItemId.make("item:secret"),
              type: "secret_request",
              runId: run,
              secretStatus: "pending",
              label: "Token",
              reason: "to ship",
            },
          ],
        }),
      );
      assert.deepStrictEqual(prompts, LocalTaskPrompts.NO_PROMPTS, JSON.stringify(runs));
    }
  });

  it("an older task with no runs marked: only its original run, as upstream's status reads it", () => {
    const later = RunId.make("run:local-prompts-later");
    const spawn = {
      type: "subagent_spawn",
      sourceThreadId: parentThread,
      targetThreadId: childThread,
      targetRunId: run,
    };
    // Its spawn started the run the request is in; no spawn known: the thread's first run.
    for (const transfers of [[spawn], []]) {
      const prompts = LocalTaskPrompts.localPromptsOf(
        task,
        child({
          runs: [{ id: run, status: "running" }],
          transfers,
          requests: [request("request:a")],
        }),
      );
      assert.strictEqual(prompts.approvals.length, 1);
    }
    // A later turn in the thread is not the task's.
    const prompts = LocalTaskPrompts.localPromptsOf(
      task,
      child({
        runs: [
          { id: later, status: "running" },
          { id: run, status: "running" },
        ],
        transfers: [{ ...spawn, targetRunId: later }],
        requests: [request("request:a")],
      }),
    );
    assert.deepStrictEqual(prompts, LocalTaskPrompts.NO_PROMPTS);
  });

  it("pending secret and pairing cards: label and reason only; a pairing card named as it can be", () => {
    const prompts = LocalTaskPrompts.localPromptsOf(
      task,
      child({
        items: [
          {
            id: TurnItemId.make("item:secret"),
            type: "secret_request",
            runId: run,
            secretStatus: "pending",
            label: "Deploy token",
            reason: "to ship",
          },
          {
            id: TurnItemId.make("item:secret-given"),
            type: "secret_request",
            runId: run,
            secretStatus: "saved",
            label: "Old token",
            reason: "done",
          },
          {
            id: TurnItemId.make("item:pairing-label"),
            type: "link_request",
            runId: run,
            linkStatus: "pending",
            label: "build box",
            hint: "the box",
            reason: "to build there",
          },
          {
            id: TurnItemId.make("item:pairing-hint"),
            type: "link_request",
            runId: run,
            linkStatus: "pending",
            label: " ",
            hint: "the box",
            reason: "",
          },
          {
            id: TurnItemId.make("item:pairing-bare"),
            type: "link_request",
            runId: run,
            linkStatus: "pending",
            reason: "",
          },
          {
            id: TurnItemId.make("item:pairing-linked"),
            type: "link_request",
            runId: run,
            linkStatus: "linked",
            label: "done box",
            reason: "",
          },
        ],
      }),
    );
    assert.deepStrictEqual(prompts.secrets, [
      { itemId: TurnItemId.make("item:secret"), label: "Deploy token", reason: "to ship" },
    ]);
    assert.deepStrictEqual(
      prompts.pairings.map((pairing) => pairing.label),
      ["build box", "the box", null],
    );
  });
});

const insertTask = (
  sql: SqlClient.SqlClient,
  fields: {
    id: string;
    parent: string;
    child: string | null;
    status: string;
    origin?: string;
    result?: string | null;
  },
) =>
  sql`
    INSERT INTO orchestration_v2_projection_subagents
      (subagent_id, thread_id, run_id, parent_node_id, provider, provider_thread_id,
       child_thread_id, origin, status, started_at, completed_at, updated_at, payload_json)
    VALUES (${fields.id}, ${fields.parent}, NULL, ${`node:${fields.id}-parent`}, 'codex', NULL,
      ${fields.child}, ${fields.origin ?? "app_owned"}, ${fields.status}, NULL, NULL,
      '2026-01-01T00:00:00.000Z', ${JSON.stringify({ result: fields.result ?? null })})
  `;

describe("the tasks read for it", () => {
  it.effect("open app-owned tasks with a thread here, none settled or with a result", () =>
    Effect.gen(function* () {
      const sql = yield* SqlClient.SqlClient;
      yield* insertTask(sql, {
        id: "task-a",
        parent: "thread:p1",
        child: "thread:c1",
        status: "running",
      });
      yield* insertTask(sql, {
        id: "task-b",
        parent: "thread:p2",
        child: "thread:c2",
        status: "running",
      });
      // On a linked machine: the remote watcher has it.
      yield* insertTask(sql, {
        id: "task-remote",
        parent: "thread:p1",
        child: null,
        status: "running",
      });
      yield* insertTask(sql, {
        id: "task-done",
        parent: "thread:p1",
        child: "thread:c3",
        status: "completed",
      });
      yield* insertTask(sql, {
        id: "task-result",
        parent: "thread:p1",
        child: "thread:c4",
        status: "running",
        result: "done",
      });
      yield* insertTask(sql, {
        id: "task-provider",
        parent: "thread:p1",
        child: "thread:c5",
        status: "running",
        origin: "provider_native",
      });
      const all = yield* LocalTaskPrompts.openLocalTasks();
      assert.deepStrictEqual(
        all.map((task) => [task.parentThreadId, task.taskId, task.childThreadId]),
        [
          ["thread:p1", "task-a", "thread:c1"],
          ["thread:p2", "task-b", "thread:c2"],
        ],
      );
    }).pipe(Effect.provide(SqlitePersistence.layerMemory)),
  );
});
