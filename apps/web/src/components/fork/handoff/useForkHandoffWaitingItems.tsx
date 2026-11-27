/**
 * Fork (HO5): the "handoff waiting on" chip above the composer. A handoff that is due but finds the
 * thread busy waits; the chip names what it waits on and goes once it no longer waits (it was sent,
 * a newer turn replaced it, or handoff was switched off). Read again when the thread changes, and
 * every few seconds while it shows. Only for a thread on the app's own server.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import type { HandoffRouteBlocker, ScopedThreadRef } from "@t3tools/contracts";
import { HourglassIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { useQuietThreadShell } from "../../../state/entities";
import type { ComposerBannerStackItem } from "../../chat/ComposerBannerStack";
import { waitingOnText } from "./handoffMenu";

/** While the chip shows: its blockers may end without a change the thread shell carries. */
export const HANDOFF_CHIP_POLL_MS = 10_000;

export function handoffWaitingItem(
  blockers: ReadonlyArray<HandoffRouteBlocker>,
): ComposerBannerStackItem | null {
  if (blockers.length === 0) return null;
  return {
    id: "fork-handoff-waiting",
    variant: "info",
    icon: <HourglassIcon />,
    title: `Handoff waiting on ${waitingOnText(blockers)}`,
    description: "The thread hands off to a fresh session once it is quiet.",
  };
}

const NONE: ReadonlyArray<ComposerBannerStackItem> = [];

export function useForkHandoffWaitingItems(
  threadRef: ScopedThreadRef | null,
): ReadonlyArray<ComposerBannerStackItem> {
  const environmentId = threadRef?.environmentId ?? null;
  const threadId = threadRef?.threadId ?? null;
  const shell = useQuietThreadShell(threadRef);
  // The thread's change marker: a turn end, a request, background work.
  const changed = shell?.updatedAt ?? null;
  const [read, setRead] = useState<{
    readonly key: string;
    readonly blockers: ReadonlyArray<HandoffRouteBlocker>;
  } | null>(null);
  const key = environmentId !== null && threadId !== null ? `${environmentId}:${threadId}` : null;
  const blockers = read !== null && read.key === key ? read.blockers : [];

  // oxlint-disable-next-line t3code/no-unreasoned-state-sync -- reads the server's handoff state for this thread when it changes, and polls while the chip shows
  useEffect(() => {
    if (key === null || environmentId === null || threadId === null || changed === null) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const readOnce = async () => {
      const { readHandoffState } = await import("./handoffApi");
      const state = await readHandoffState(
        { environmentId, threadId },
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      const next = state?.waitingOn ?? [];
      setRead({ key, blockers: next });
      if (next.length > 0) timer = setTimeout(() => void readOnce(), HANDOFF_CHIP_POLL_MS);
    };
    // A read that finds the chip gone stops the polling; a shown chip keeps it going.
    void readOnce();
    return () => {
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [key, environmentId, threadId, changed]);

  const item = handoffWaitingItem(blockers);
  return item === null ? NONE : [item];
}
