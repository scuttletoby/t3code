/**
 * Fork (calm sidebar): {@link ThreadRoleTargets} read from upstream's orchestrator, for the
 * `threadRoles` save check (threadRoleTargets.ts). Kept apart from that file so the settings file
 * never imports orchestration code. Wired in forkLayers.ts (`layerRuntimeServices`); its make span
 * shows holdWork.boot.test.ts the real server builds it.
 */
import { isListedThreadLineage, type ThreadId } from "@t3tools/contracts";
import { Effect, Layer } from "effect";

import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import { ThreadRoleTargets } from "./threadRoleTargets.ts";

export const layer = Layer.effect(
  ThreadRoleTargets,
  Effect.map(Orchestrator.OrchestratorV2, (orchestrator) =>
    ThreadRoleTargets.of({
      find: Effect.fn("ThreadRoleTargets.find")(function* (threadId: ThreadId) {
        const shell = yield* orchestrator.getThreadShell(threadId);
        return shell === null
          ? null
          : {
              projectId: shell.projectId,
              listed: isListedThreadLineage(shell.lineage),
              // `!= null` as upstream's `isDelegatedAppThread`: an MCP-launched thread may carry
              // `delegatedFrom: null`, and that thread is not delegated.
              delegated: shell.delegatedFrom != null,
            };
      }),
    }),
  ).pipe(Effect.withSpan("ThreadRoleTargets.make")),
);
