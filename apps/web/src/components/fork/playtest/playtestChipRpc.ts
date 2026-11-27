/** Fork (playtest F5): the web's commands for the thread chip's two RPCs. */
import { createEnvironmentRpcCommand } from "@t3tools/client-runtime/state/runtime";
import { FORK_PLAYTEST_WS_METHODS } from "@t3tools/contracts";

import { connectionAtomRuntime } from "../../../connection/runtime";

export const forkPlaytestStatus = createEnvironmentRpcCommand(connectionAtomRuntime, {
  label: "fork:playtest:status",
  tag: FORK_PLAYTEST_WS_METHODS.status,
});

export const forkPlaytestEnd = createEnvironmentRpcCommand(connectionAtomRuntime, {
  label: "fork:playtest:end",
  tag: FORK_PLAYTEST_WS_METHODS.end,
});
