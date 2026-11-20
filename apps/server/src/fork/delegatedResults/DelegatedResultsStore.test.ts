/**
 * Fork (issue 625): the reminder's own reads over the real schema. The state question: rows from
 * before delivery states (no state), settled and running rows, rows older than the window, a
 * provider's own subagent, an empty table, and a send whose message row is missing but whose
 * receipt was kept (or the other way round).
 */
import { assert, it } from "@effect/vitest";
import { EnvironmentId, ThreadId } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as SqlClient from "effect/sql/SqlClient";

import * as SqlitePersistence from "../../persistence/Sqlite.ts";
import * as DelegatedResultsStore from "./DelegatedResultsStore.ts";

const layer = DelegatedResultsStore.layer.pipe(Layer.provideMerge(SqlitePersistence.layerMemory));

const SINCE = "2026-11-19T12:00:00.000Z";
const RECENT = "2026-11-20T11:59:00.000Z";
const OLD = "2026-11-19T11:59:59.999Z";

const insert = (row: {
  readonly id: string;
  readonly thread: string;
  readonly status?: string;
  readonly origin?: string;
  readonly delivery?: string | null;
  readonly completedAt?: string | null;
  readonly updatedAt?: string;
  readonly childThreadId?: string | null;
  readonly remote?: { readonly environmentId: string; readonly threadId: string };
}) =>
  Effect.gen(function* () {
    const sql = yield* SqlClient.SqlClient;
    const payload = row.remote === undefined ? {} : { remoteChild: { ...row.remote, label: "b" } };
    yield* sql`
      INSERT INTO orchestration_v2_projection_subagents (
        subagent_id, thread_id, run_id, parent_node_id, provider, driver, provider_instance_id,
        provider_thread_id, child_thread_id, origin, status, started_at, completed_at, updated_at,
        completion_delivery_state, payload_json
      ) VALUES (
        ${row.id}, ${row.thread}, NULL, ${`node-${row.id}`}, 'codex', 'codex', 'codex', NULL,
        ${row.childThreadId === undefined ? `child-${row.id}` : row.childThreadId},
        ${row.origin ?? "app_owned"}, ${row.status ?? "completed"}, ${RECENT},
        ${row.completedAt === undefined ? RECENT : row.completedAt},
        ${row.updatedAt ?? RECENT},
        ${row.delivery === undefined ? "claimed" : row.delivery},
        ${JSON.stringify(payload)}
      )
    `;
  });

it.layer(layer)("DelegatedResultsStore", (it) => {
  it.effect("an empty table names no parent and no linked child", () =>
    Effect.gen(function* () {
      const store = yield* DelegatedResultsStore.DelegatedResultsStore;
      assert.deepStrictEqual(yield* store.parentsWithUnreadResults(SINCE), []);
      assert.deepStrictEqual(yield* store.remoteChildrenOf(ThreadId.make("p-none")), []);
      assert.deepStrictEqual(yield* store.sentIds(ThreadId.make("p-none"), "delegated-orphan"), []);
    }),
  );

  it.effect("names each parent once with a recent unread or stateless result", () =>
    Effect.gen(function* () {
      yield* insert({ id: "owed-1", thread: "p-owed" });
      yield* insert({ id: "owed-2", thread: "p-owed", delivery: "pending" });
      // No delivery state: an old row, or a wait that never acknowledged it.
      yield* insert({ id: "none-1", thread: "p-none", delivery: null });
      // No end time recorded: its last update counts.
      yield* insert({ id: "no-end", thread: "p-no-end", completedAt: null });
      yield* insert({ id: "read", thread: "p-read", delivery: "acknowledged" });
      yield* insert({ id: "sent", thread: "p-read", delivery: "delivered" });
      yield* insert({ id: "running", thread: "p-running", status: "running", delivery: null });
      yield* insert({ id: "old", thread: "p-old", completedAt: OLD, updatedAt: OLD });
      yield* insert({ id: "native", thread: "p-native", origin: "provider_native" });
      const store = yield* DelegatedResultsStore.DelegatedResultsStore;
      assert.deepStrictEqual(yield* store.parentsWithUnreadResults(SINCE), [
        ThreadId.make("p-no-end"),
        ThreadId.make("p-none"),
        ThreadId.make("p-owed"),
      ]);
    }),
  );

  it.effect("lists the linked children a parent's tasks follow, settled ones too", () =>
    Effect.gen(function* () {
      const parent = "p-linked";
      yield* insert({
        id: "r-1",
        thread: parent,
        childThreadId: null,
        remote: { environmentId: "env-b", threadId: "remote-1" },
      });
      yield* insert({
        id: "r-2",
        thread: parent,
        childThreadId: null,
        delivery: "acknowledged",
        remote: { environmentId: "env-c", threadId: "remote-2" },
      });
      // A local child, and another parent's linked child.
      yield* insert({ id: "local", thread: parent });
      yield* insert({
        id: "r-other",
        thread: "p-other",
        childThreadId: null,
        remote: { environmentId: "env-b", threadId: "remote-3" },
      });
      const store = yield* DelegatedResultsStore.DelegatedResultsStore;
      const children = yield* store.remoteChildrenOf(ThreadId.make(parent));
      assert.deepStrictEqual(
        children?.toSorted((a, b) => (a.threadId < b.threadId ? -1 : 1)),
        [
          { environmentId: EnvironmentId.make("env-b"), threadId: ThreadId.make("remote-1") },
          { environmentId: EnvironmentId.make("env-c"), threadId: ThreadId.make("remote-2") },
        ],
      );
    }),
  );

  it.effect("reads a thread's sent ids of one kind from its messages and accepted receipts", () =>
    Effect.gen(function* () {
      const sql = yield* SqlClient.SqlClient;
      const message = (id: string, thread: string) => sql`
        INSERT INTO orchestration_v2_projection_messages (
          message_id, thread_id, run_id, node_id, role, streaming, created_at, updated_at, payload_json
        ) VALUES (${id}, ${thread}, NULL, NULL, 'user', 0, ${RECENT}, ${RECENT}, '{}')
      `;
      const receipt = (id: string, thread: string, status: string) => sql`
        INSERT INTO orchestration_v2_command_receipts (
          command_id, thread_id, command_type, accepted_at, result_sequence, status, error
        ) VALUES (${id}, ${thread}, 'message.dispatch', ${RECENT}, 1, ${status}, NULL)
      `;
      yield* message("delegated-orphan:p-sent:aaaaaaaaaaaa", "p-sent");
      yield* receipt("delegated-orphan:p-sent:aaaaaaaaaaaa:0", "p-sent", "accepted");
      // Accepted, but its message row is gone: the receipt still records it.
      yield* receipt("delegated-orphan:p-sent:bbbbbbbbbbbb:0", "p-sent", "accepted");
      // A refused command told nobody.
      yield* receipt("delegated-orphan:p-sent:cccccccccccc:0", "p-sent", "rejected");
      // Another kind of message, and another thread's reminder.
      yield* message("delegated-reminder:p-sent:dddd", "p-sent");
      yield* message("delegated-orphan:p-other:eeeeeeeeeeee", "p-other");
      const store = yield* DelegatedResultsStore.DelegatedResultsStore;
      const ids = yield* store.sentIds(ThreadId.make("p-sent"), "delegated-orphan");
      assert.deepStrictEqual(ids?.toSorted(), [
        "delegated-orphan:p-sent:aaaaaaaaaaaa",
        "delegated-orphan:p-sent:aaaaaaaaaaaa:0",
        "delegated-orphan:p-sent:bbbbbbbbbbbb:0",
      ]);
    }),
  );
});
