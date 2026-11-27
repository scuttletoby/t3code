/**
 * Fork (playtest F5): the chip above the composer of a thread whose playtest session is on, with
 * End (the tool's `end`, reason `person`). Read again when the thread changes and while it shows,
 * so it goes when the session ends any other way (docs/fork/playtest.md, "The thread chip").
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import {
  AuthOrchestrationOperateScope,
  type PlaytestChipSession,
  type PlaytestRouteEndAnswer,
  type ScopedThreadRef,
} from "@t3tools/contracts";
import { GamepadIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { useQuietThreadShell } from "../../../state/entities";
import { useEnvironmentScope } from "../../../state/session";
import type { ComposerBannerStackItem } from "../../chat/ComposerBannerStack";
import { Button } from "../../ui/button";
import { toastManager } from "../../ui/toast";
import { Tooltip, TooltipPopup, TooltipTrigger } from "../../ui/tooltip";
import { endPlaytest, readPlaytestSession } from "./playtestApi";

/** While the chip shows: an end by T3 itself (idle, time limit) changes nothing the shell carries. */
const PLAYTEST_CHIP_POLL_MS = 10_000;

const STATE_TEXT: Record<PlaytestChipSession["state"], string> = {
  claiming: "starting",
  active: "on",
  ending: "ending",
  ended: "ended",
};

/** The chip's line under the title: the last round, and the step that runs now. */
function roundText(session: PlaytestChipSession): string {
  const round = session.lastRound;
  const last =
    round === undefined ? "No round yet." : `Round ${round.number} ${round.state} (${round.step}).`;
  const running = session.running === undefined ? "" : ` A ${session.running} step runs now.`;
  const problem = session.releaseProblem
    ? ` Its release has not worked yet; T3 tries again, or press End again.`
    : "";
  return `${last}${running}${problem}`;
}

/** The title's tooltip: the agent's note and how many rounds ran. */
function playtestChipTooltip(session: PlaytestChipSession): string {
  const rounds = `${session.rounds} ${session.rounds === 1 ? "round" : "rounds"} so far`;
  return session.note === "" ? rounds : `${session.note} · ${rounds}`;
}

function PlaytestChipTitle({ session }: { readonly session: PlaytestChipSession }) {
  const tooltipId = `fork-playtest-tooltip-${session.id}`;
  const tooltip = playtestChipTooltip(session);
  return (
    <Tooltip>
      <TooltipTrigger render={<span aria-describedby={tooltipId} tabIndex={0} />}>
        {`Playtest ${STATE_TEXT[session.state]}: ${session.target}`}
      </TooltipTrigger>
      <span id={tooltipId} className="sr-only">
        {tooltip}
      </span>
      <TooltipPopup side="top">{tooltip}</TooltipPopup>
    </Tooltip>
  );
}

/**
 * The chip for `session`, or none when no session is on. End only for a connection that may change
 * threads (the operate scope, as the route asks); a read-only one sees the chip alone.
 */
export function playtestChipItem(
  session: PlaytestChipSession | null,
  options: { readonly ending: boolean; readonly canEnd: boolean; readonly onEnd: () => void },
): ComposerBannerStackItem | null {
  if (session === null || session.state === "ended") return null;
  return {
    id: "fork-playtest-session",
    variant: "info",
    icon: <GamepadIcon />,
    title: <PlaytestChipTitle session={session} />,
    description: roundText(session),
    ...(options.canEnd
      ? {
          actions: (
            <Button size="xs" variant="ghost" disabled={options.ending} onClick={options.onEnd}>
              {options.ending ? "Ending…" : "End"}
            </Button>
          ),
        }
      : {}),
  };
}

/** What a press of End did, for the toast. */
function endAnswerText(answer: PlaytestRouteEndAnswer): {
  readonly type: "success" | "info" | "error";
  readonly title: string;
  readonly description?: string;
} {
  switch (answer.outcome) {
    case "ended":
      return {
        type: "success",
        title: "Playtest ended",
        description: "The target is released. The worktree and its changes are kept.",
      };
    case "nothing-to-end":
      return { type: "info", title: "This playtest had already ended" };
    case "release-failed":
      return {
        type: "error",
        title: "Playtest is ending",
        description: "Its release has not worked yet; T3 tries again, or press End again.",
      };
    case "unavailable":
      return {
        type: "error",
        title: "Playtest not ended",
        ...(answer.message === undefined ? {} : { description: answer.message }),
      };
  }
}

const NONE: ReadonlyArray<ComposerBannerStackItem> = [];

export function useForkPlaytestItems(
  threadRef: ScopedThreadRef | null,
): ReadonlyArray<ComposerBannerStackItem> {
  const environmentId = threadRef?.environmentId ?? null;
  const threadId = threadRef?.threadId ?? null;
  const shell = useQuietThreadShell(threadRef);
  const canEnd = useEnvironmentScope(environmentId, AuthOrchestrationOperateScope);
  // The thread's change marker: a tool call, a turn end, T3's "Playtest ended" notice.
  const changed = shell?.updatedAt ?? null;
  const key = environmentId !== null && threadId !== null ? `${environmentId}:${threadId}` : null;
  const [read, setRead] = useState<{
    readonly key: string;
    readonly session: PlaytestChipSession | null;
  } | null>(null);
  const [endingKey, setEndingKey] = useState<string | null>(null);
  const session = read !== null && read.key === key ? read.session : null;

  // oxlint-disable-next-line t3code/no-unreasoned-state-sync -- reads the server's playtest session for this thread when it changes, and polls while the chip shows
  useEffect(() => {
    if (key === null || environmentId === null || threadId === null || changed === null) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const readOnce = async () => {
      const next = await readPlaytestSession({ environmentId, threadId }, controller.signal);
      if (controller.signal.aborted) return;
      setRead({ key, session: next });
      if (next !== null) timer = setTimeout(() => void readOnce(), PLAYTEST_CHIP_POLL_MS);
    };
    // A read that finds no session stops the polling; a shown chip keeps it going.
    void readOnce();
    return () => {
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [key, environmentId, threadId, changed]);

  const onEnd = () => {
    if (key === null || environmentId === null || threadId === null || endingKey === key) return;
    setEndingKey(key);
    void endPlaytest({ environmentId, threadId })
      .then(
        (answer) => {
          toastManager.add(endAnswerText(answer));
          // Ended (or already): no chip. Still ending: the chip says so. Not tried: as it was.
          if (answer.outcome !== "unavailable") {
            const next = answer.session?.state === "ended" ? null : answer.session;
            setRead((current) => (current?.key === key ? { key, session: next } : current));
          }
        },
        (error: unknown) =>
          toastManager.add({
            type: "error",
            title: "Playtest not ended",
            description: error instanceof Error ? error.message : "The server could not be asked.",
          }),
      )
      .finally(() => {
        setEndingKey((current) => (current === key ? null : current));
      });
  };

  const item = playtestChipItem(session, { ending: endingKey === key, canEnd, onEnd });
  return item === null ? NONE : [item];
}
