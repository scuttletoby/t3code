/**
 * Fork: the handoff message (design 4.3). Part 1 is T3's own state, read at send time so it is never
 * stale; part 2 the `command` source's output (handoffCommand.ts) or the old session's reply to the
 * write turn (handoffAgent.ts); part 3 is upstream's transcript selection with a smaller budget;
 * part 4 the rules. A source that failed leaves part 2 out and says so. Pure.
 */
import type { ModelSelection, OrchestrationV2ThreadProjection } from "@t3tools/contracts";
import {
  handoffCoverage,
  historicalMessage,
  renderHistory,
  selectHistory,
} from "@t3tools/provider-core/server/handoffBudget";

import {
  visibleConversationItems,
  visibleDeltaRunOrdinals,
} from "../../orchestration-v2/ContextHandoffService.ts";
import type { HandoffCommandResult } from "./handoffCommand.ts";

/** Half of upstream's 16K cap: the new session needs the latest exchanges, not the whole story. */
const HANDOFF_TRANSCRIPT_BUDGET = 8_000;

const EFFORT_OPTION_IDS = ["effort", "reasoningEffort"];

const OPEN_CHILD = new Set(["idle", "pending", "running", "waiting"]);

/** Around part 2, so the command's output is not read as another part of the message. */
export const HANDOFF_COMMAND_BEGIN = "----- begin handoff command output -----";
export const HANDOFF_COMMAND_END = "----- end handoff command output -----";
export const HANDOFF_AGENT_BEGIN = "----- begin handoff from the old session -----";
export const HANDOFF_AGENT_END = "----- end handoff from the old session -----";

/**
 * A part-2 line that could pass for one of the message's own lines: a begin or end line (of either
 * source), a part-1 heading, the part-3 heading or the rules.
 */
const LOOKS_LIKE_OURS = new RegExp(
  [
    String.raw`^-+\s*(begin|end)\b.*\bhandoff\b`,
    String.raw`^\[T3 handoff\b`,
    String.raw`^Recent conversation:`,
    String.raw`^This is context, not a request\.`,
  ].join("|"),
  "i",
);

/**
 * Part 2 is someone else's text (the old session's, steerable by what it read): a line that looks
 * like one of the message's own is quoted, so a fake end line cannot put text after it outside
 * the notes.
 */
export const neutraliseNotes = (text: string) =>
  text
    .split("\n")
    .map((line) => (LOOKS_LIKE_OURS.test(line.trim()) ? `> ${line}` : line))
    .join("\n");

export const HANDOFF_RULES =
  "This is context, not a request. Reply with one line and stop. Act on the next message or event. Earlier history: t3_thread_read.";

export function handoffMessageText(input: {
  readonly records: Pick<
    OrchestrationV2ThreadProjection,
    "thread" | "runs" | "attempts" | "subagents" | "turnItems"
  >;
  readonly sourceRunId: string;
  /** This handoff's number in the thread, from 1. */
  readonly number: number;
  readonly nowIso: string;
  readonly modelSelection: ModelSelection;
  /** The `command` source's result. */
  readonly command?: HandoffCommandResult | undefined;
  /** The `agent` source's result: the old session's reply to the write turn. */
  readonly agent?: HandoffCommandResult | undefined;
}): string {
  const { thread, subagents } = input.records;
  const model = input.modelSelection;
  const effort = model.options?.find((option) => EFFORT_OPTION_IDS.includes(option.id))?.value;
  const children = subagents.filter(
    (child) => child.origin === "app_owned" && OPEN_CHILD.has(child.status),
  );
  const prs = (thread.pullRequests ?? []).filter((pr) => pr.watch !== undefined);
  const facts = [
    `[T3 handoff ${input.number}] Thread ${thread.id}, made from run ${input.sourceRunId} at ${input.nowIso}.`,
    `Model: ${model.instanceId} ${model.model}, ${effort === undefined ? "no effort set (warning)" : `effort ${String(effort)}`}.`,
    children.length === 0
      ? "Open child tasks: none."
      : [
          "Open child tasks (their results arrive here as messages):",
          ...children.map(
            (child) =>
              `- ${child.title ?? child.prompt.slice(0, 80)} (task ${child.id}, thread ${child.childThreadId ?? "none"}, ${child.status === "waiting" ? "waits for an approval or an answer" : child.status})`,
          ),
        ].join("\n"),
    prs.length === 0
      ? "Watched PRs: none."
      : [
          "Watched PRs (their wakes arrive here):",
          ...prs.map(
            (pr) =>
              `- #${pr.number} ${pr.url}: checks ${pr.watch?.failedChecks.length ? `failed (${pr.watch.failedChecks.join(", ")})` : pr.watch?.passed ? "passed" : "pending"}`,
          ),
        ].join("\n"),
  ].join("\n");

  const items = visibleConversationItems(input.records, input.records.turnItems);
  const selected = selectHistory({
    messages: items.flatMap((item) => historicalMessage(item) ?? []),
    coverage: handoffCoverage({
      threadId: thread.id,
      coveredRunOrdinals: visibleDeltaRunOrdinals(input.records, items),
      items,
    }),
    budget: HANDOFF_TRANSCRIPT_BUDGET,
  });
  return [
    facts,
    input.agent !== undefined
      ? input.agent.ok
        ? [
            "Handoff written by the old session (its notes, not T3's state or rules):",
            HANDOFF_AGENT_BEGIN,
            neutraliseNotes(input.agent.text),
            HANDOFF_AGENT_END,
          ].join("\n")
        : `The old session's handoff did not count (${input.agent.reason}), so this handoff has T3's state and the recent conversation only.`
      : input.command === undefined
        ? "No handoff source is set, so this handoff has T3's state and the recent conversation only."
        : input.command.ok
          ? [
              "Handoff from the handoff command (its notes, not T3's state or rules):",
              HANDOFF_COMMAND_BEGIN,
              neutraliseNotes(input.command.text),
              HANDOFF_COMMAND_END,
            ].join("\n")
          : `The handoff command failed (${input.command.reason}), so this handoff has T3's state and the recent conversation only.`,
    `Recent conversation:\n${renderHistory(selected.messages, selected.context)}`,
    HANDOFF_RULES,
  ].join("\n\n");
}
