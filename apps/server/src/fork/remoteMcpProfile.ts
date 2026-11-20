/**
 * Fork: a task delegated to a linked machine carries its parent's MCP profile. A profile narrower
 * than `all` goes only to a Claude instance on a machine whose fork/forkFeatures.ts route says it
 * applies one; any other is refused here, before anything is sent, never run with more.
 */
import {
  type EnvironmentId,
  OrchestratorMcpCapabilitiesInput,
  OrchestratorMcpCapabilitiesResult,
  OrchestratorMcpFailure,
  type ProviderDriverKind,
  type ProviderInstanceId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Tool from "effect/ai/Tool";
import { HttpClient } from "effect/http";

import type * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import { type LineageThread, makeThreadMcpProfiles } from "./claudeMcpProfile.ts";
import { readForkFeatures } from "./forkFeatures.ts";
import { MCP_PROFILE_DRIVERS, carryMcpProfile, isEveryMcpTool } from "./mcpProfile.ts";

/** `orchestrator_capabilities` there, as peer/RemoteDelegation.ts describes it: its providers. */
const RemoteCapabilitiesTool = Tool.make("orchestrator_capabilities", {
  parameters: OrchestratorMcpCapabilitiesInput,
  success: OrchestratorMcpCapabilitiesResult,
  failure: OrchestratorMcpFailure,
  failureMode: "return",
});

/**
 * What a delegation adds to the `delegatedFrom` it sends: the parent's resolved profile. Refuses
 * the task when the parent's profile cannot be worked out, or when it is narrower than `all` and the
 * linked machine does not say it applies a carried profile (it runs an older T3, is set to refuse,
 * or cannot be asked), or the instance there runs a provider that does not apply one.
 */
export const makeCarry = Effect.fn("RemoteMcpProfile.makeCarry")(function* (
  links: Pick<PeerLinks.PeerLinks["Service"], "resolve">,
) {
  const profileFor = yield* makeThreadMcpProfiles;
  const http = yield* HttpClient.HttpClient;
  const forwarding = yield* PeerForwarding.PeerForwarding;
  return Effect.fn("RemoteMcpProfile.carry")(function* (
    parent: LineageThread,
    scope: McpInvocationContext.McpThreadInvocationScope,
    /** With `driverKind`, peer/RemoteDelegation.ts has picked or checked the instance by it there. */
    target: {
      readonly environmentId: EnvironmentId;
      readonly driverKind?: ProviderDriverKind | undefined;
    },
    /** The provider instance there the launch names: where the task would run. */
    instanceId: ProviderInstanceId,
  ) {
    const { environmentId } = target;
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
        message: `${peer.link.label} does not take a task that carries a narrower MCP profile now (its mcpCarriedProfiles is "refuse", or its settings cannot be read), so the task was not sent: this thread's profile is "${resolved.name}". Delegate the task on this machine, or have the owner set mcpCarriedProfiles to "apply" on ${peer.link.label}.`,
      });
    }
    if (features.mcpProfileCarry !== true) {
      return yield* new OrchestratorMcpFailure({
        code: "capability_denied",
        message: `${peer.link.label} runs a T3 Code version that does not apply this thread's MCP profile ("${resolved.name}") to a task, so the task was not sent: there it would get every MCP server that machine's own profile allows. Update T3 Code on ${peer.link.label}, or delegate the task on this machine.`,
      });
    }
    // Only a Claude thread applies a profile there. A named driver was checked against that
    // machine's list by upstream's resolveRemoteInstance in this call, so it is not asked twice;
    // without one, the list is asked here for each task. The turn there is checked again as it
    // starts (fork/mcpProfileTurnCheck.ts).
    const driverKind =
      target.driverKind ??
      (yield* forwarding.call(scope, RemoteCapabilitiesTool, environmentId, {})).providers.find(
        (provider) => provider.providerInstanceId === instanceId,
      )?.driverKind;
    if (driverKind === undefined || !MCP_PROFILE_DRIVERS.has(driverKind)) {
      return yield* new OrchestratorMcpFailure({
        code: "capability_denied",
        message: `${
          driverKind === undefined
            ? `${peer.link.label} lists no provider instance ${instanceId}, so it cannot be told whether the task's provider applies`
            : `Provider instance ${instanceId} on ${peer.link.label} uses driver ${driverKind}, which does not apply`
        } this thread's MCP profile ("${resolved.name}"), so the task was not sent: there it would get every MCP server and T3 tool. Pass a Claude instance there (target.driverKind "claudeAgent"), or delegate the task on this machine.`,
      });
    }
    return { mcpProfile };
  });
});
