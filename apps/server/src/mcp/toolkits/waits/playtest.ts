/**
 * Fork (playtest): the `playtest` tool over the playtest service (docs/fork/playtest.md). Thin: the
 * service holds every rule (settings, one step at a time, busy, no session); this file only picks
 * the call and says the answer in plain words. The thread is the caller's own, from its MCP
 * credential; the tool takes no thread, so an agent never reaches another thread's session.
 */
import {
  type PlaytestSessionView,
  type PlaytestToolAction,
  type PlaytestToolInput,
  type PlaytestToolOutcome,
  type PlaytestToolResult,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";

import { Playtest } from "../../../playtest/PlaytestLayer.ts";
import { assertFullAccess } from "../../threadAccess.ts";
import { callingThread as threadOfCaller } from "./callingThread.ts";

/** Who calls: the server's record of the thread the credential names, never tool input. */
const callingThread = () =>
  threadOfCaller(
    "A playtest session belongs to a thread. This MCP client is not running inside a T3 thread.",
  );

/**
 * `start` and `push` run the person's steps outside the thread's sandbox, so they need a full-access
 * thread not in plan mode, by its modes as the server records them at this call. `status` and `end`
 * stay open, so a restricted thread can still release its target.
 */
const fullAccessNeeded =
  "Only a full-access thread, not in plan mode, can start or push a playtest; ask the owner to run it from such a thread.";

const roundText = (session: PlaytestSessionView) => {
  const round = session.lastRound;
  if (round === undefined) return "";
  const detail = round.detail === "" ? "" : `: ${round.detail}`;
  return `Round ${round.number} ${round.state} (step ${round.step})${detail}`;
};

/** What happened and what to do next, for the agent. */
function playtestMessage(
  action: PlaytestToolAction,
  outcome: PlaytestToolOutcome,
  session: PlaytestSessionView | null,
): string {
  if (session === null) {
    return action === "end"
      ? "This thread has no playtest session; nothing to end."
      : "This thread has no playtest session. Start one with action start and a target.";
  }
  const name = `${session.target} (${session.id})`;
  const problem = session.problem === undefined ? "" : ` ${session.problem}`;
  switch (outcome) {
    case "started":
      return `Playtest of ${name} is on. Change the worktree, then call push for a round.`;
    case "already-on":
      return `This thread already playtests ${name}; nothing new was claimed.`;
    case "refused":
      return action === "start"
        ? `The claim of ${session.target} was refused; nothing is claimed.${problem}`
        : `${roundText(session)}. The session stays on; fix it and push again.`;
    case "failed":
      return action === "start"
        ? `The claim of ${session.target} failed; T3 releases it.${problem}`
        : `${roundText(session)}. The session stays on; fix it and push again.`;
    case "stopped":
      return action === "start"
        ? `The session for ${session.target} ended while its claim ran.${problem}`
        : `${roundText(session)}. The session ended or T3 stopped during the round.`;
    case "done":
      return `${roundText(session)}. The person can try it now.`;
    case "on": {
      // Nothing runs for a claim or an end: T3 stopped during the claim, or the release failed.
      if (session.running === undefined && session.state === "claiming") {
        return `The claim of ${name} did not finish (T3 stopped); call end to release it.${problem}`;
      }
      if (session.running === undefined && session.state === "ending") {
        return `Playtest of ${name} is ending, but its release has not worked yet; T3 tries again, or call end again.${problem}`;
      }
      const running = session.running === undefined ? "" : ` A ${session.running} step runs now.`;
      const round = session.lastRound === undefined ? " No round yet." : ` ${roundText(session)}.`;
      return `Playtest of ${name} is ${session.state}.${round}${running}`;
    }
    case "ended":
      return action === "end"
        ? `Playtest of ${name} ended; the target is released. The worktree is kept.`
        : `This thread's last playtest, ${name}, ended (${session.endReason ?? "unknown"}).${problem} Start a new one to go on.`;
    case "release-failed":
      return `Playtest of ${name} is ending, but its release has not worked yet; T3 tries again, or call end again.${problem}`;
    case "none":
    case "nothing-to-end":
      return action === "end"
        ? `This thread has no playtest session on; nothing to end. Its last one, ${name}, ended.`
        : `This thread has no playtest session on. Its last one, ${name}, ended.`;
  }
}

const answer = (
  action: PlaytestToolAction,
  outcome: PlaytestToolOutcome,
  session: PlaytestSessionView | null,
): PlaytestToolResult => ({
  action,
  outcome,
  message: playtestMessage(action, outcome, session),
  session,
});

export const playtestCall = Effect.fn("playtest.call")(function* (input: PlaytestToolInput) {
  const caller = yield* callingThread();
  const threadId = caller.caller.id;
  const playtest = yield* Playtest;
  switch (input.action) {
    case "start": {
      yield* assertFullAccess(caller, fullAccessNeeded);
      const { outcome, session } = yield* playtest.start({
        threadId,
        target: input.target ?? "",
        note: input.note,
      });
      return answer("start", outcome, session);
    }
    case "push": {
      yield* assertFullAccess(caller, fullAccessNeeded);
      const { outcome, session } = yield* playtest.push({ threadId });
      return answer("push", outcome, session);
    }
    case "status": {
      const session = yield* playtest.status({ threadId });
      const outcome =
        session === null ? "none" : session.state === "ended" ? ("ended" as const) : "on";
      return answer("status", outcome, session);
    }
    case "end": {
      const { outcome, session } = yield* playtest.end({ threadId, reason: "agent" });
      return answer("end", outcome, session);
    }
  }
});
