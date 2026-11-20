/**
 * Fork: a task delegated to a linked machine carries its parent's MCP profile (cut to there by
 * fork/mcpProfile.ts). An older machine drops it, and one whose owner set `mcpCarriedProfiles` to
 * `refuse` refuses it, so a profile narrower than `all` goes only to one whose fork/forkFeatures.ts
 * route says it applies one; any other refuses, never runs with more.
 */
import { type EnvironmentId, OrchestratorMcpFailure } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import { HttpClient } from "effect/http";

import * as PeerLinks from "../peer/PeerLinks.ts";
import { type LineageThread, makeThreadMcpProfiles } from "./claudeMcpProfile.ts";
import { readForkFeatures } from "./forkFeatures.ts";
import { carryMcpProfile, isEveryMcpTool } from "./mcpProfile.ts";

/**
 * What a delegation adds to the `delegatedFrom` it sends: the parent's resolved profile. Refuses
 * the task when the parent's profile cannot be worked out, or when it is narrower than `all` and the
 * linked machine does not say it applies a carried profile (it runs an older T3) or cannot be asked.
 */
export const makeCarry = Effect.fn("RemoteMcpProfile.makeCarry")(function* (
  links: Pick<PeerLinks.PeerLinks["Service"], "resolve">,
) {
  const profileFor = yield* makeThreadMcpProfiles;
  const http = yield* HttpClient.HttpClient;
  return Effect.fn("RemoteMcpProfile.carry")(function* (
    parent: LineageThread,
    environmentId: EnvironmentId,
  ) {
    const resolved = yield* profileFor(parent).pipe(
      Effect.mapError(
        (refusal) =>
          new OrchestratorMcpFailure({
            code: "invalid_request",
            message: `This thread's MCP profile cannot be worked out, so it cannot delegate a task to another machine. ${refusal.reason}`,
          }),
      ),
    );
    const mcpProfile = carryMcpProfile(resolved);
    // `all` takes nothing away there, so an older machine, which gives the task its own profile
    // for its role as before, widens nothing; it is not asked.
    if (isEveryMcpTool(resolved.profile)) return { mcpProfile };
    const peer = yield* links
      .resolve(environmentId)
      .pipe(
        Effect.mapError(
          (error) =>
            new OrchestratorMcpFailure({ code: "orchestration_error", message: error.message }),
        ),
      );
    const features = yield* readForkFeatures(http, {
      url: peer.url,
      token: peer.token,
      timeout: PeerLinks.probeTimeout(peer.url),
    }).pipe(
      Effect.mapError(
        () =>
          new OrchestratorMcpFailure({
            code: "orchestration_error",
            message: `${peer.link.label} did not answer whether it applies a carried MCP profile, so the task was not sent.`,
          }),
      ),
    );
    if (features.mcpProfileCarry === false) {
      return yield* new OrchestratorMcpFailure({
        code: "capability_denied",
        message: `${peer.link.label} is set not to take a task that carries a narrower MCP profile (its mcpCarriedProfiles is "refuse"), so the task was not sent: this thread's profile is "${resolved.name}". Delegate the task on this machine, or have the owner set mcpCarriedProfiles to "apply" on ${peer.link.label}.`,
      });
    }
    if (features.mcpProfileCarry !== true) {
      return yield* new OrchestratorMcpFailure({
        code: "capability_denied",
        message: `${peer.link.label} runs a T3 Code version that does not apply this thread's MCP profile ("${resolved.name}") to a task, so the task was not sent: there it would get every MCP server that machine's own profile allows. Update T3 Code on ${peer.link.label}, or delegate the task on this machine.`,
      });
    }
    return { mcpProfile };
  });
});
