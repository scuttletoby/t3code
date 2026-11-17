// Fork (`serve --hold-work`): a held copy shares the live server's Connect link and environment id,
// so it starts, registers and releases no tunnel, publishes no agent activity, and refuses to
// link, change or unlink (an unlink is followed by a relay delete of that id). It only reads.
// Every method is listed by name, never spread: a new upstream method is a type error here.
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as Schema from "effect/Schema";

import * as ServerConfig from "../config.ts";
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

/**
 * A held boot marks its home as a copy before the real CloudLink is built, so the CLI refusals
 * hold once it stops too. Created only when missing: a marker already there stays as it is.
 */
const markHeldCopy = Layer.effectDiscard(
  Effect.gen(function* () {
    if (!(yield* WorkAdmission.WorkAdmission).heldFromBoot) return;
    const markerPath = (yield* Path.Path).join(
      (yield* ServerConfig.ServerConfig).baseDir,
      HELD_COPY_MARKER_FILE,
    );
    const note = [
      `A server ran on this T3 home with --hold-work (first at ${DateTime.formatIso(yield* DateTime.now)}),`,
      "so it is taken to be a copy that shares the live server's T3 Connect link. While this file",
      "exists, `t3 connect` and `t3 up` refuse to sign in, link, unlink or sign out here.",
      "Delete it only if this home is not a copy.",
      "",
    ].join("\n");
    yield* (yield* FileSystem.FileSystem).writeFileString(markerPath, note, { flag: "wx" }).pipe(
      Effect.catchIf(
        (error) => error.reason._tag === "AlreadyExists",
        () => Effect.void,
      ),
    );
  }),
);

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
  ).pipe(Layer.provide(cloudLinkLayer), Layer.provide(markHeldCopy));

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

/** Written in a home by a held boot. The `t3 connect` refusals read it; nothing removes it. */
export const HELD_COPY_MARKER_FILE = "hold-work-copy.txt";

/**
 * `t3 connect` sign-in, link, unlink and logout, and `t3 up` turning Connect on or off: refused on
 * a held copy's home. A copy shares the live server's environment id and CLI sign-in.
 */
export const refuseCliOnHeldCopy = Effect.fn("HoldWorkConnect.refuseCliOnHeldCopy")(function* (
  config: Pick<ServerConfig.ServerConfig["Service"], "baseDir" | "serverRuntimeStatePath">,
) {
  const fs = yield* FileSystem.FileSystem;
  const markerPath = (yield* Path.Path).join(config.baseDir, HELD_COPY_MARKER_FILE);
  // Only whether it is there counts. A marker that cannot be checked counts as there.
  if (yield* fs.exists(markerPath).pipe(Effect.orElseSucceed(() => true))) {
    return yield* new HeldCopyRefusedError({ markerPath });
  }
  const running = yield* readLiveServerRuntimeState(config.serverRuntimeStatePath).pipe(
    Effect.orElseSucceed(() => Option.none()),
  );
  if (Option.isSome(running) && running.value.workHeld === true) {
    return yield* new HeldCopyRefusedError({ markerPath: null });
  }
});

export class HeldCopyRefusedError extends Schema.TaggedError<HeldCopyRefusedError>()(
  "HeldCopyRefusedError",
  { markerPath: Schema.NullOr(Schema.String) },
) {
  override get message(): string {
    const why =
      this.markerPath === null
        ? "this home's server runs with --hold-work, so it is a copy"
        : `${this.markerPath} marks this home as a copy that ran with --hold-work. A copy`;
    return [
      `Refused: ${why} that shares the live server's T3 Connect environment id and sign-in.`,
      "Signing in, linking, unlinking or signing out here would act on the live server's link too. Nothing was changed.",
      ...(this.markerPath === null
        ? []
        : [`If this home is not a copy, delete ${this.markerPath} and run the command again.`]),
    ].join("\n");
  }
}
