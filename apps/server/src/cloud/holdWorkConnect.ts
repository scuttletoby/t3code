// Fork (`serve --hold-work`): a held copy of a T3 home carries the live server's Connect link, so
// it starts, registers and releases no tunnel (any of these could take over or drop remote access)
// and refuses a client's request to link. Reading the link and unlinking touch only its own files.
// Every method is listed by name, never spread: a new upstream method is a type error here.
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as ManagedLimits from "../policy/managedLimits.ts";
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

/** The CloudLink a held server uses: it reads and unlinks, and starts or releases nothing. */
const holdConnect = (cloudLink: CloudLink.CloudLink["Service"]): CloudLink.CloudLink["Service"] =>
  CloudLink.CloudLink.of({
    linkState: cloudLink.linkState,
    unlink: cloudLink.unlink,
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
