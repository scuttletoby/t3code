/**
 * Fork: how long the handoff service waits at a quiet thread before it sends. A completed quiet
 * handoff run does not alert: the thread shell shows it as upstream's stopped run, one rule in
 * `handoffShell.ts`, read by every shell reader, the relay included.
 */
import { RELAY_COMPLETION_HOLD_MS } from "./handoffShell.ts";

/**
 * The handoff service waits this long at a quiet thread before it sends (`HandoffService.ts`), so
 * the person's alert for their own turn has gone out first; twice the hold, for a relay that lags.
 */
export const HANDOFF_SETTLE_MS = 2 * RELAY_COMPLETION_HOLD_MS;

/** How often a settle checks the relay's view between events (`handoffRelayView.ts`). */
export const HANDOFF_SETTLE_CHECK_MS = 1_000;
