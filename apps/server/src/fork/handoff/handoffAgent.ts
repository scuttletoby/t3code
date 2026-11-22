/**
 * Fork: the handoff's `agent` source (part 2). The old session gets one normal turn, the write turn,
 * with the person's `instructions`, and its reply is the handoff. The reply counts only when the
 * turn completed, no other message joined it, it starts with the marker line and it is within the
 * command source's cap (docs/fork/handoff.md). Pure.
 */
import type { OrchestrationV2Run, OrchestrationV2TurnItem } from "@t3tools/contracts";

import { HANDOFF_COMMAND_MAX_BYTES, type HandoffCommandResult } from "./handoffCommand.ts";

/** The reply's first line. Anything else means the agent did not write a handoff. */
export const HANDOFF_AGENT_MARKER = "[T3 handoff notes]";

/** Used when the setting's `instructions` is empty. */
export const HANDOFF_AGENT_INSTRUCTIONS =
  "T3 is about to move this thread to a fresh session that has none of this conversation. Write the handoff that session needs: the goal, what is done, what is open and what comes next, decisions and their reasons, and the ids of tasks, pull requests and files it must know. Facts, not a story. Use no tools unless you must.";

export function handoffWriteText(instructions: string): string {
  return [
    instructions.trim() === "" ? HANDOFF_AGENT_INSTRUCTIONS : instructions.trim(),
    `Start your reply with this line, alone: ${HANDOFF_AGENT_MARKER}`,
    `Keep the reply under ${HANDOFF_COMMAND_MAX_BYTES / 1024} KB. Reply with the handoff only.`,
  ].join("\n\n");
}

/**
 * The reason T3 gives when it stops a write turn that did not end in time. Upstream keeps it on the
 * run's `run_interrupt_request` item, so T3's own stop is told apart from a person's, after a
 * restart too.
 */
export const HANDOFF_WRITE_STOP_REASON =
  "T3 stopped the handoff write turn: it did not end in time.";

/** What the write turn gave: a handoff, a reply that does not count, or no handoff at all. */
export type HandoffAgentResult =
  | HandoffCommandResult
  /** Stopped by a person, or joined: the old session carries on and nothing is sent. */
  | { readonly ok: false; readonly reason: string; readonly noHandoff: true };

/** A message other than the write turn's own prompt went into its run (steered in). */
export const handoffWriteJoined = (
  run: Pick<OrchestrationV2Run, "userMessageId">,
  items: ReadonlyArray<OrchestrationV2TurnItem>,
) => items.some((item) => item.type === "user_message" && item.messageId !== run.userMessageId);

/** T3 stopped the write turn at its timeout (not a person's Stop). */
export const handoffWriteTimedOut = (items: ReadonlyArray<OrchestrationV2TurnItem>) =>
  items.some(
    (item) => item.type === "run_interrupt_request" && item.message === HANDOFF_WRITE_STOP_REASON,
  );

/**
 * The handoff in a write turn that ended. `items` are the run's turn items: its user messages, its
 * replies and its stop requests. A failed turn, or one T3 stopped at its timeout, does not count
 * (the size backstop then sends parts 1, 3 and 4); one a person stopped or steered into is no
 * handoff at all.
 */
export function handoffAgentReply(
  run: Pick<OrchestrationV2Run, "status" | "userMessageId">,
  items: ReadonlyArray<OrchestrationV2TurnItem>,
): HandoffAgentResult {
  // A message steered into the turn was answered there: the old session keeps that exchange.
  if (handoffWriteJoined(run, items)) {
    return { ok: false, reason: "another message joined the write turn", noHandoff: true };
  }
  if (run.status === "failed") return { ok: false, reason: "the write turn failed" };
  if (run.status === "interrupted" && handoffWriteTimedOut(items)) {
    return { ok: false, reason: "the write turn did not end in time" };
  }
  if (run.status !== "completed") {
    return { ok: false, reason: `the write turn ended ${run.status}`, noHandoff: true };
  }
  const replies = items
    .filter((item) => item.type === "assistant_message")
    .toSorted((left, right) => left.ordinal - right.ordinal)
    .map((item) => item.text.trim())
    .filter((text) => text !== "");
  // The last reply that starts with the marker: a short "Done." after the notes does not hide them.
  const reply =
    replies.findLast((text) => text.split("\n")[0]?.trim() === HANDOFF_AGENT_MARKER) ??
    replies.at(-1);
  if (reply === undefined) return { ok: false, reason: "it wrote no reply" };
  if (new TextEncoder().encode(reply).length > HANDOFF_COMMAND_MAX_BYTES) {
    return { ok: false, reason: `its reply is over ${HANDOFF_COMMAND_MAX_BYTES / 1024} KB` };
  }
  const [first, ...rest] = reply.split("\n");
  if (first?.trim() !== HANDOFF_AGENT_MARKER) {
    return { ok: false, reason: "its reply does not start with the marker line" };
  }
  const text = rest.join("\n").trim();
  return text === ""
    ? { ok: false, reason: "its reply has only the marker line" }
    : { ok: true, text };
}
