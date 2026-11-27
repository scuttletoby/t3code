/**
 * Fork (playtest F5): the thread header's chip while the thread has a playtest session on, with End
 * (the same end as the tool's, as the person). It asks the server every 15 seconds while the thread
 * is open, and only where playtest is on, so it goes when the session ends any other way.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import { runAtomCommand } from "@t3tools/client-runtime/state/runtime";
import {
  AuthOrchestrationOperateScope,
  type ForkPlaytestChipSession,
  type ScopedThreadRef,
} from "@t3tools/contracts";
import { useAtomValue } from "@effect/atom-react";
import { useEffect, useState } from "react";

import { appAtomRegistry } from "../../../rpc/atomRegistry";
import { serverEnvironment } from "../../../state/server";
import { useEnvironmentScope } from "../../../state/session";
import { Badge } from "../../ui/badge";
import { Button } from "../../ui/button";
import { toastManager } from "../../ui/toast";
import { Tooltip, TooltipPopup, TooltipTrigger } from "../../ui/tooltip";
import {
  PLAYTEST_CHIP_POLL_MS,
  playtestChipText,
  playtestEndMessage,
  playtestMayRun,
} from "./playtestChip.logic";
import { forkPlaytestEnd, forkPlaytestStatus } from "./playtestChipRpc";

const readStatus = async (ref: Pick<ScopedThreadRef, "environmentId" | "threadId">) => {
  const result = await runAtomCommand(
    appAtomRegistry,
    forkPlaytestStatus,
    { environmentId: ref.environmentId, input: { threadId: ref.threadId } },
    { reportFailure: false },
  );
  // A failed read keeps what the chip showed; the next poll asks again.
  return result._tag === "Success" ? { session: result.value } : null;
};

export function ForkPlaytestChip(props: { readonly threadRef: ScopedThreadRef }) {
  const { threadRef } = props;
  const settings = useAtomValue(serverEnvironment.settingsValueAtom(threadRef.environmentId));
  const canEnd = useEnvironmentScope(threadRef.environmentId, AuthOrchestrationOperateScope);
  const mayRun = playtestMayRun(settings);
  const [shown, setShown] = useState<{
    readonly key: string;
    readonly session: ForkPlaytestChipSession | null;
  } | null>(null);
  const [ending, setEnding] = useState(false);
  const { environmentId, threadId } = threadRef;
  const key = `${environmentId}:${threadId}`;

  // oxlint-disable-next-line t3code/no-unreasoned-state-sync -- polls the server for this thread's playtest session while the thread is open and playtest is on
  useEffect(() => {
    if (!mayRun) return;
    let live = true;
    const read = () =>
      void readStatus({ environmentId, threadId }).then((next) => {
        if (live && next !== null) setShown({ key, session: next.session });
      });
    read();
    const timer = setInterval(read, PLAYTEST_CHIP_POLL_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [environmentId, threadId, key, mayRun]);

  const session = mayRun && shown?.key === key ? shown.session : null;
  if (session === null) return null;
  const text = playtestChipText(session);

  const end = () => {
    if (ending) return;
    setEnding(true);
    void runAtomCommand(
      appAtomRegistry,
      forkPlaytestEnd,
      { environmentId: threadRef.environmentId, input: { threadId: threadRef.threadId } },
      { reportFailure: false },
    )
      .then((result) => {
        if (result._tag !== "Success") {
          toastManager.add({ type: "error", title: "Could not end the playtest." });
          return;
        }
        const message = playtestEndMessage(result.value.outcome);
        if (message !== null) toastManager.add(message);
      })
      // Read again at once, so the chip goes (or shows "ending") without waiting for the poll.
      .then(() => readStatus(threadRef))
      .then((next) => {
        if (next !== null) setShown({ key, session: next.session });
      })
      .finally(() => setEnding(false));
  };

  return (
    <span className="inline-flex shrink-0 items-center gap-1" data-testid="fork-playtest-chip">
      <Tooltip>
        <TooltipTrigger render={<Badge variant="warning" size="sm" />}>{text.label}</TooltipTrigger>
        <TooltipPopup side="bottom">{text.hint}</TooltipPopup>
      </Tooltip>
      {canEnd && text.canEnd ? (
        <Button
          size="compact"
          variant="outline"
          disabled={ending}
          onClick={end}
          aria-label={`End the playtest on ${session.target}`}
        >
          {ending ? "Ending…" : "End"}
        </Button>
      ) : null}
    </span>
  );
}
