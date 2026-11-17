/**
 * Fork: the handoff message (design 4.3). Part 1 is T3's own state, read at send time so it is never
 * stale; part 3 is upstream's transcript selection with a smaller budget; part 4 the rules. Part 2
 * (a source's handoff) comes with the `command` and `agent` sources; until then the message says
 * that no source wrote one. Pure.
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

/** Half of upstream's 16K cap: the new session needs the latest exchanges, not the whole story. */
const HANDOFF_TRANSCRIPT_BUDGET = 8_000;

const EFFORT_OPTION_IDS = ["effort", "reasoningEffort"];

const OPEN_CHILD = new Set(["idle", "pending", "running", "waiting"]);

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
    "No handoff source is set up yet, so this handoff has T3's state and the recent conversation only.",
    `Recent conversation:\n${renderHistory(selected.messages, selected.context)}`,
    HANDOFF_RULES,
  ].join("\n\n");
}
