// Fork (`serve --hold-work`, or any boot of a home a held boot marked): a copy shares the live
// server's Connect environment id, so it starts, registers and releases no tunnel, publishes no
// agent activity, and refuses to link, change or unlink (an unlink ends in a relay delete of that
// id). Every method is listed by name, never spread: a new upstream method is a type error here.
import * as Context from "effect/Context";
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

/** Why Connect is held: `--hold-work` (`markerPath` null), or the held-copy marker in this home. */
interface ConnectHold {
  readonly markerPath: string | null;
}

const markedCopy = (markerPath: string) =>
  `${markerPath} marks this home as a copy that shares another server's T3 Connect link`;

const holdLog = (hold: ConnectHold, part: string, effect: string) =>
  hold.markerPath === null
    ? `${part} held (--hold-work): ${effect}`
    : `${part} held: ${markedCopy(hold.markerPath)}, so ${effect}. Delete it only if this home is not a copy`;

/** What a client sees when it asks a held server, or one on a marked copy, to link or change its link. */
export const heldConnectRefusal = (markerPath?: string) =>
  new ManagedLimits.ManagedPolicyRefusedError(
    markerPath === undefined
      ? {
          organization: "This held server",
          action: "T3 Connect",
          detail: "while it runs with --hold-work; restart it without the flag to link it",
        }
      : {
          organization: "This copy",
          action: "T3 Connect",
          detail: `as ${markedCopy(markerPath)}; if it is not a copy, delete that file and restart`,
        },
  );

/** The CloudLink a held server uses: it reads the link state, and starts or releases nothing. */
const holdConnect = (
  cloudLink: CloudLink.CloudLink["Service"],
  hold: ConnectHold,
): CloudLink.CloudLink["Service"] => {
  const refuse = () => Effect.fail(heldConnectRefusal(hold.markerPath ?? undefined));
  return CloudLink.CloudLink.of({
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
};

/** Whether this boot holds Connect: held from boot, or a normal boot of a home a held boot marked. */
class ConnectHoldDecision extends Context.Service<
  ConnectHoldDecision,
  Option.Option<ConnectHold>
>()("t3/cloud/holdWorkConnect/ConnectHoldDecision") {}

/**
 * The boot's one read of the marker. `layer` and `agentActivityLayer` both provide this one layer
 * value, and a server builds a layer value once, so CloudLink and agent activity share the result:
 * a marker deleted or added by hand while the server starts cannot hold one and not the other.
 */
const connectHoldDecision = Layer.effect(
  ConnectHoldDecision,
  Effect.gen(function* () {
    if ((yield* WorkAdmission.WorkAdmission).heldFromBoot) {
      return Option.some<ConnectHold>({ markerPath: null });
    }
    const marker = yield* heldCopyMarker((yield* ServerConfig.ServerConfig).baseDir);
    return Option.map(marker, (markerPath): ConnectHold => ({ markerPath }));
  }),
);

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

/** `layer` as is, or held when the server is held from boot or its home is a marked copy. */
export const layer = <E, R>(cloudLinkLayer: Layer.Layer<CloudLink.CloudLink, E, R>) =>
  Layer.effect(
    CloudLink.CloudLink,
    Effect.gen(function* () {
      const cloudLink = yield* CloudLink.CloudLink;
      const hold = yield* ConnectHoldDecision;
      if (Option.isNone(hold)) return cloudLink;
      yield* Effect.logInfo(
        holdLog(
          hold.value,
          "T3 Connect",
          "the stored link is kept as it is; no tunnel starts, registers or is released",
        ),
      );
      return holdConnect(cloudLink, hold.value);
    }),
  ).pipe(
    Layer.provide(cloudLinkLayer),
    Layer.provide(markHeldCopy),
    Layer.provide(connectHoldDecision),
  );

/** The publisher a held server uses: it logs that it is held and sends nothing. */
const holdAgentActivity = (hold: ConnectHold): AgentAwarenessRelay.AgentAwarenessRelay["Service"] =>
  AgentAwarenessRelay.AgentAwarenessRelay.of({
    publishThread: Effect.fn("HoldWorkConnect.publishThread")(() => Effect.void),
    drain: Effect.void,
    requestCatchUp: Effect.fn("HoldWorkConnect.requestCatchUp")(() => Effect.void),
    start: Effect.fn("HoldWorkConnect.startAgentActivity")(() =>
      Effect.logInfo(
        holdLog(
          hold,
          "Agent activity publishing",
          "no thread state is sent to the T3 Connect relay",
        ),
      ),
    ),
  });

/**
 * `layer` as is, or held like `layer`. The publisher reads the copied relay address, credential
 * and signing key itself, not through CloudLink.
 */
export const agentActivityLayer = <E, R>(
  relayLayer: Layer.Layer<AgentAwarenessRelay.AgentAwarenessRelay, E, R>,
) =>
  Layer.effect(
    AgentAwarenessRelay.AgentAwarenessRelay,
    Effect.gen(function* () {
      const relay = yield* AgentAwarenessRelay.AgentAwarenessRelay;
      const hold = yield* ConnectHoldDecision;
      return Option.isSome(hold) ? holdAgentActivity(hold.value) : relay;
    }),
  ).pipe(Layer.provide(relayLayer), Layer.provide(connectHoldDecision));

/** Written in a home by a held boot. Every boot and the `t3 connect` refusals read it; nothing removes it. */
export const HELD_COPY_MARKER_FILE = "hold-work-copy.txt";

/**
 * The marker's path when `baseDir` is marked. Anything at the path counts, a dangling symlink too
 * (the held boot's exclusive create keeps one), and so does a check that fails.
 */
const heldCopyMarker = Effect.fn("HoldWorkConnect.heldCopyMarker")(function* (baseDir: string) {
  const fs = yield* FileSystem.FileSystem;
  const markerPath = (yield* Path.Path).join(baseDir, HELD_COPY_MARKER_FILE);
  const marked = yield* fs.exists(markerPath).pipe(
    Effect.flatMap((exists) =>
      exists
        ? Effect.succeed(true)
        : fs.readLink(markerPath).pipe(
            Effect.as(true),
            Effect.catchIf(
              (error) => error.reason._tag === "NotFound",
              () => Effect.succeed(false),
            ),
          ),
    ),
    // Fails closed on purpose: a check that errors (EIO, EACCES) counts as marked, so a copy is
    // never let reach Connect because its marker could not be read. On the live home that holds
    // Connect for that boot only, and the log names the file. Do not turn this into fail-open.
    Effect.orElseSucceed(() => true),
  );
  return marked ? Option.some(markerPath) : Option.none();
});

/**
 * `t3 connect` sign-in, link, unlink and logout, and `t3 up` turning Connect on or off: refused on
 * a held copy's home. A copy shares the live server's environment id and CLI sign-in.
 */
export const refuseCliOnHeldCopy = Effect.fn("HoldWorkConnect.refuseCliOnHeldCopy")(function* (
  config: Pick<ServerConfig.ServerConfig["Service"], "baseDir" | "serverRuntimeStatePath">,
) {
  const marker = yield* heldCopyMarker(config.baseDir);
  if (Option.isSome(marker)) {
    return yield* new HeldCopyRefusedError({ markerPath: marker.value });
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
      "Signing in, linking, unlinking or signing out here would act on the live server's link too. Nothing was sent, and the link and sign-in are as they were.",
      ...(this.markerPath === null
        ? []
        : [`If this home is not a copy, delete ${this.markerPath} and run the command again.`]),
    ].join("\n");
  }
}
