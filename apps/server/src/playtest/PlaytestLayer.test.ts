/** Fork (playtest): what the worker takes from thread events and from a delivery's answer. */
import { assert, describe, it } from "@effect/vitest";
import { EventId, MessageId, type OrchestrationV2DomainEvent, ThreadId } from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";

import { noticeResultOf, personActivity } from "./PlaytestLayer.ts";

const THREAD = ThreadId.make("thread-a");
const AT = DateTime.makeUnsafe("2026-11-17T11:00:00.000Z");

const message = (
  role: "user" | "assistant" | "system",
  createdBy: "user" | "agent" | "system",
): OrchestrationV2DomainEvent => ({
  id: EventId.make("event-1"),
  type: "message.updated",
  threadId: THREAD,
  occurredAt: AT,
  payload: {
    id: MessageId.make("message-1"),
    threadId: THREAD,
    runId: null,
    nodeId: null,
    role,
    text: "hello",
    attachments: [],
    streaming: false,
    createdBy,
    creationSource: "web",
    createdAt: AT,
    updatedAt: AT,
  },
});

describe("PlaytestLayer", () => {
  it("only a message the person wrote is activity, at the time it was written", () => {
    assert.deepStrictEqual(personActivity(message("user", "user")), {
      threadId: THREAD,
      atMs: DateTime.toEpochMillis(AT),
    });
    // An agent's message (a delegate's report, T3's own playtest message) and replies are not.
    assert.isNull(personActivity(message("user", "agent")));
    assert.isNull(personActivity(message("user", "system")));
    assert.isNull(personActivity(message("assistant", "user")));
    assert.isNull(
      personActivity({
        id: EventId.make("event-2"),
        type: "thread.archived",
        threadId: THREAD,
        occurredAt: AT,
      } as unknown as OrchestrationV2DomainEvent),
    );
  });

  it("a delivery is sent, tried again later, or dropped", () => {
    assert.strictEqual(noticeResultOf({ type: "delivered" }), "delivered");
    assert.strictEqual(noticeResultOf({ type: "retry", detail: "busy" }), "retry");
    assert.strictEqual(noticeResultOf({ type: "rejected", detail: "no" }), "dropped");
    assert.strictEqual(noticeResultOf({ type: "gone", parentThreadId: null }), "dropped");
    assert.strictEqual(noticeResultOf({ type: "stopped", parentThreadId: null }), "dropped");
  });
});
