/** Fork: the `agent` source's write turn text and the checks on its reply. */
import { describe, expect, it } from "@effect/vitest";
import { MessageId, RunId } from "@t3tools/contracts";

import { runId, threadId, turnItem } from "./handoff.testkit.ts";
import {
  HANDOFF_AGENT_INSTRUCTIONS,
  HANDOFF_AGENT_MARKER,
  HANDOFF_WRITE_STOP_REASON,
  handoffAgentReply,
  handoffWriteText,
} from "./handoffAgent.ts";
import { HANDOFF_COMMAND_MAX_BYTES } from "./handoffCommand.ts";

const M = HANDOFF_AGENT_MARKER;
const writeId = MessageId.make(`handoff-write:${threadId}:${runId}`);
const writeRun = RunId.make("write-run");
const ended = (status: "completed" | "interrupted" | "failed" | "cancelled" = "completed") => ({
  status,
  userMessageId: writeId,
});
const items = (...replies: ReadonlyArray<string>) => [
  turnItem(writeRun, 1, { type: "user_message", messageId: writeId, text: "Write the handoff." }),
  ...replies.map((text, index) =>
    turnItem(writeRun, 10 + index, { type: "assistant_message", text }),
  ),
];
const failed = (reason: string) => ({ ok: false, reason });

describe("handoffWriteText", () => {
  it("is the instructions, else the built-in text, then the marker line and the cap", () => {
    expect(handoffWriteText(" List the open PRs. ")).toBe(
      `List the open PRs.\n\nStart your reply with this line, alone: ${M}\n\nKeep the reply under 32 KB. Reply with the handoff only.`,
    );
    expect(handoffWriteText("").startsWith(`${HANDOFF_AGENT_INSTRUCTIONS}\n\n`)).toBe(true);
  });
});

describe("handoffAgentReply", () => {
  it("is the last reply, without its marker line", () => {
    expect(
      handoffAgentReply(ended(), items("Reading the state.", `${M}\nGoal: X.\nNext: Y.`)),
    ).toEqual({ ok: true, text: "Goal: X.\nNext: Y." });
    // Exactly at the cap still counts.
    const full = `${M}\n${"x".repeat(HANDOFF_COMMAND_MAX_BYTES - M.length - 1)}`;
    expect(handoffAgentReply(ended(), items(full)).ok).toBe(true);
  });

  it("a reply without the marker line, over the cap, empty or only the marker does not count", () => {
    const reply = (text?: string) =>
      handoffAgentReply(ended(), text === undefined ? items() : items(text));
    const noMarker = failed("its reply does not start with the marker line");
    expect(reply("Goal: X.")).toEqual(noMarker);
    expect(reply(`Goal: X.\n${M}`)).toEqual(noMarker);
    // The cap is in UTF-8 bytes, as the command source's: fewer characters than the cap.
    expect(reply(`${M}\n${"é".repeat(HANDOFF_COMMAND_MAX_BYTES / 2)}`)).toEqual(
      failed("its reply is over 32 KB"),
    );
    expect(reply()).toEqual(failed("it wrote no reply"));
    expect(reply(" \n ")).toEqual(failed("it wrote no reply"));
    expect(reply(`${M}\n  `)).toEqual(failed("its reply has only the marker line"));
  });

  it("is the last reply that starts with the marker: a short word after the notes does not hide them", () => {
    expect(handoffAgentReply(ended(), items(`${M}\nGoal: X.`, "Done."))).toEqual({
      ok: true,
      text: "Goal: X.",
    });
    expect(
      handoffAgentReply(ended(), items(`${M}\nOld.`, "Thinking.", `${M}\nNew.`, "Done.")),
    ).toEqual({ ok: true, text: "New." });
  });

  it("a write turn a person stopped, or one joined by a message, means no handoff", () => {
    const stoppedBy = (message: string) => [
      ...items(`${M}\nGoal: X.`),
      turnItem(writeRun, 20, { type: "run_interrupt_request", message }),
    ];
    for (const status of ["interrupted", "cancelled"] as const) {
      expect(handoffAgentReply(ended(status), stoppedBy("Stopped by user"))).toEqual({
        ok: false,
        reason: `the write turn ended ${status}`,
        noHandoff: true,
      });
    }
    // Stopped without any stop request (a restart): the old session carries on too.
    expect(handoffAgentReply(ended("interrupted"), items(`${M}\nGoal: X.`))).toMatchObject({
      noHandoff: true,
    });
    const joined = [
      ...items(`${M}\nGoal: X.`),
      turnItem(writeRun, 5, { type: "user_message", messageId: "message-2", text: "And X?" }),
    ];
    expect(handoffAgentReply(ended(), joined)).toEqual({
      ok: false,
      reason: "another message joined the write turn",
      noHandoff: true,
    });
    // Joined decides first: a person's message in a failed turn is theirs, not a fallback's.
    expect(handoffAgentReply(ended("failed"), joined)).toMatchObject({ noHandoff: true });
  });

  it("a failed write turn, or one T3 stopped at its timeout, does not count (the fallback)", () => {
    expect(handoffAgentReply(ended("failed"), items(`${M}\nGoal: X.`))).toEqual(
      failed("the write turn failed"),
    );
    const timedOut = [
      ...items(),
      turnItem(writeRun, 20, { type: "run_interrupt_request", message: HANDOFF_WRITE_STOP_REASON }),
    ];
    expect(handoffAgentReply(ended("interrupted"), timedOut)).toEqual(
      failed("the write turn did not end in time"),
    );
  });
});
