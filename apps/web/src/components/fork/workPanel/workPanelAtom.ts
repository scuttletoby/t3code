/**
 * Fork (work panel, fork issue 1136): the `fork.workPanel` read of one environment, refreshed
 * once a minute while the panel shows. The server answers every viewer from one shared cache, so
 * this costs no GitHub call of its own.
 */
import { FORK_WORK_PANEL_METHOD } from "@t3tools/contracts";
import { createEnvironmentRpcQueryAtomFamily } from "@t3tools/client-runtime/state/runtime";

import { connectionAtomRuntime } from "../../../connection/runtime";

const WORK_PANEL_REFRESH_MS = 60_000;

export const forkWorkPanelAtom = createEnvironmentRpcQueryAtomFamily(connectionAtomRuntime, {
  label: "environment-data:fork:work-panel",
  tag: FORK_WORK_PANEL_METHOD,
  staleTimeMs: WORK_PANEL_REFRESH_MS / 2,
  refreshIntervalMs: WORK_PANEL_REFRESH_MS,
  idleTtlMs: 0,
});
