// Fork (`serve --hold-work`): a held copy shares the live server's Connect link and environment id,
// so it starts, registers and releases no tunnel, publishes no agent activity, and refuses to
// link, change or unlink (an unlink is followed by a relay delete of that id). It only reads.
// Every method is listed by name, never spread: a new upstream method is a type error here.
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";

import * as ManagedLimits from "../policy/managedLimits.ts";
import * as AgentAwarenessRelay from "../relay/AgentAwarenessRelay.ts";
import { readLiveServerRuntimeState } from "../serverRuntimeState.ts";
import * as WorkAdmission from "../workAdmission.ts";
import * as CloudLink from "./CloudLink.ts";

const HOLD_CONNECT_LOG =
  "T3 Connect held (--hold-work): the stored link is kept as it is; no tunnel starts, registers or is released";

/** What a client sees when it asks a held server to link or change its link. */
export const heldConnectRefusal = () =>
  new ManagedLimits.ManagedPolicyRefusedError({
    organization: "This held server",
    action: "T3 Connect",
    detail: "while it runs with --hold-work; restart it without the flag to link it",
  });

const refuse = () => Effect.fail(heldConnectRefusal());

/** The CloudLink a held server uses: it reads the link state, and starts or releases nothing. */
const holdConnect = (cloudLink: CloudLink.CloudLink["Service"]): CloudLink.CloudLink["Service"] =>
  CloudLink.CloudLink.of({
    linkState: cloudLink.linkState,
    // The web Unlink sends a relay delete for this environment id once the server agrees, and a
    // copy shares the live server's id: refusing here stops it before that delete.
    unlink: refuse,
    linkProof: refuse,
    applyRelayConfig: refuse,
    updatePreferences: refuse,
    answerHealthRequest: refuse,
    mintCredential: refuse,
    reconcileDesiredLinkIfStillDesired: Effect.fn("HoldWorkConnect.reconcileDesiredLink")(() =>
      Effect.succeed(null),
    ),
    registerManagedTunnelRecovery: Effect.fn("HoldWorkConnect.registerManagedTunnelRecovery")(() =>
      Effect.succeed({ status: "not_linked" as const }),
    ),
    recoverManagedTunnel: Effect.fn("HoldWorkConnect.recoverManagedTunnel")(() =>
      Effect.succeed(false),
    ),
    startManagedTunnelIfOriginConfirmed: Effect.fn("HoldWorkConnect.startManagedTunnel")(() =>
      Effect.succeed(false),
    ),
    releaseManagedTunnelOnShutdown: Effect.fn("HoldWorkConnect.releaseManagedTunnel")(() =>
      Effect.succeed(false),
    ),
  });

/** `layer` as is, or held when the shared work gate is held from boot. */
export const layer = <E, R>(cloudLinkLayer: Layer.Layer<CloudLink.CloudLink, E, R>) =>
  Layer.effect(
    CloudLink.CloudLink,
    Effect.gen(function* () {
      const cloudLink = yield* CloudLink.CloudLink;
      if (!(yield* WorkAdmission.WorkAdmission).heldFromBoot) return cloudLink;
      yield* Effect.logInfo(HOLD_CONNECT_LOG);
      return holdConnect(cloudLink);
    }),
  ).pipe(Layer.provide(cloudLinkLayer));

const HOLD_AGENT_ACTIVITY_LOG =
  "Agent activity publishing held (--hold-work): no thread state is sent to the T3 Connect relay";

/** The publisher a held server uses: it logs that it is held and sends nothing. */
const holdAgentActivity = (): AgentAwarenessRelay.AgentAwarenessRelay["Service"] =>
  AgentAwarenessRelay.AgentAwarenessRelay.of({
    publishThread: Effect.fn("HoldWorkConnect.publishThread")(() => Effect.void),
    drain: Effect.void,
    requestCatchUp: Effect.fn("HoldWorkConnect.requestCatchUp")(() => Effect.void),
    start: Effect.fn("HoldWorkConnect.startAgentActivity")(() =>
      Effect.logInfo(HOLD_AGENT_ACTIVITY_LOG),
    ),
  });

/**
 * `layer` as is, or held when the shared work gate is held from boot. The publisher reads the
 * copied relay address, credential and signing key itself, not through CloudLink.
 */
export const agentActivityLayer = <E, R>(
  relayLayer: Layer.Layer<AgentAwarenessRelay.AgentAwarenessRelay, E, R>,
) =>
  Layer.effect(
    AgentAwarenessRelay.AgentAwarenessRelay,
    Effect.gen(function* () {
      const relay = yield* AgentAwarenessRelay.AgentAwarenessRelay;
      return (yield* WorkAdmission.WorkAdmission).heldFromBoot ? holdAgentActivity() : relay;
    }),
  ).pipe(Layer.provide(relayLayer));

/**
 * `t3 connect unlink` / `logout`: refused while this home's server runs held. They also send the
 * relay delete for the environment id, which a copy shares with the live server. A copy that is
 * not running cannot be told apart from the live home; docs/fork/updates.md warns about that.
 */
export const refuseCliUnlinkWhileHeld = Effect.fn("HoldWorkConnect.refuseCliUnlinkWhileHeld")(
  function* (serverRuntimeStatePath: string) {
    const running = yield* readLiveServerRuntimeState(serverRuntimeStatePath).pipe(
      Effect.orElseSucceed(() => Option.none()),
    );
    if (Option.isSome(running) && running.value.workHeld === true) {
      return yield* new HeldCliUnlinkRefusedError();
    }
  },
);

export class HeldCliUnlinkRefusedError extends Schema.TaggedError<HeldCliUnlinkRefusedError>()(
  "HeldCliUnlinkRefusedError",
  {},
) {
  override get message(): string {
    return "Refused: this home's server runs with --hold-work, so it is a copy that shares the live server's T3 Connect environment id. Unlinking or signing out here would also remove the live server's link on the relay. Nothing was changed.";
  }
}
