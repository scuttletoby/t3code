/**
 * Fork: the fork's own service layers, wired into upstream's server.ts in one place each, so that
 * file keeps as few fork lines as it can (.github/fork-seams.json). Each export says where it goes;
 * moving it keeps the same layers, built in the same places, as when they were inline there.
 */
import { Effect, Layer } from "effect";

import * as ServerAutoUpdate from "../cloud/autoUpdate.ts";
import type * as CloudLink from "../cloud/CloudLink.ts";
import * as HoldWorkConnect from "../cloud/holdWorkConnect.ts";
import type * as ServerConfig from "../config.ts";
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as HeadlessUsageRefresher from "../usage/HeadlessUsageRefresher.ts";
import * as UsagePacer from "../usage/UsagePacer.ts";
import * as UsageReadingsHistory from "../usage/UsageReadingsHistory.ts";
import * as WorkAdmission from "../workAdmission.ts";

/** Wraps upstream's agent activity relay: a held server (`serve --hold-work`) publishes none. */
export const holdWorkAgentActivity = HoldWorkConnect.agentActivityLayer;

/**
 * Upstream's Connect link with its reachability merged in, as server.ts had it, except that a held
 * server starts, registers and releases no tunnel.
 */
export const holdWorkCloudLink = <E, R, ROut2, E2, R2>(
  cloudLinkLayer: Layer.Layer<CloudLink.CloudLink, E, R>,
  reachabilityLayer: Layer.Layer<ROut2, E2, R2>,
) => HoldWorkConnect.layer(cloudLinkLayer).pipe(Layer.provideMerge(reachabilityLayer));

/** In the runtime's core dependencies: usage pacer (and its history), slot gate, usage refresher. */
export const layerRuntimeServices = Layer.mergeAll(
  UsagePacer.layer.pipe(Layer.provideMerge(UsageReadingsHistory.layer)),
  SlotGate.layer,
  HeadlessUsageRefresher.layer,
);

/** In the routes, after upstream's follower of tasks in linked environments. */
export const layerRoutes = Layer.mergeAll(
  // /mcp reads the usage pacer and the slot gate only if they are there (the usage pacing gate,
  // agentSlots). Asking for them beside /mcp makes a server wired without one a type error
  // instead of a silent gate.
  Layer.effectDiscard(UsagePacer.UsagePacer.pipe(Effect.asVoid)),
  Layer.effectDiscard(SlotGate.SlotGate.pipe(Effect.asVoid)),
  // Off unless automaticUpdates is on; shares the Update button's ServerSelfUpdate in server.ts.
  ServerAutoUpdate.layer,
);

/**
 * Upstream's start of the follower of delegated tasks in linked environments, except that a held
 * server (`serve --hold-work`) neither follows nor stops them. What it owes them stays owed for
 * the next start without the flag.
 */
export const startRemoteDelegation = Effect.gen(function* () {
  const remote = yield* RemoteDelegation.RemoteDelegation;
  if ((yield* WorkAdmission.WorkAdmission).heldFromBoot) {
    return yield* Effect.logInfo("Remote delegated task follower held (--hold-work)");
  }
  yield* remote.start();
});

/**
 * One gate for the whole server: automatic updates hold new work until the restart. Made where
 * server.ts makes its activation, before any layer is built; a held boot says so in the log.
 * Untraced, as it was inline: no span of its own.
 */
export const makeWorkAdmissionLayer = Effect.fnUntraced(function* (
  config: Pick<ServerConfig.ServerConfig["Service"], "baseDir" | "holdWork">,
) {
  const layer = Layer.succeed(
    WorkAdmission.WorkAdmission,
    yield* WorkAdmission.makeWorkAdmissionGate({
      marker: yield* WorkAdmission.makeFileUpdateHoldStore(config.baseDir),
      heldFromBoot: config.holdWork === true,
    }),
  );
  if (config.holdWork === true) {
    yield* Effect.logWarning(WorkAdmission.HOLD_WORK_BOOT_MESSAGE);
  }
  return layer;
});
