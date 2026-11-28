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
import type * as AgentAwarenessRelay from "../relay/AgentAwarenessRelay.ts";
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";
import * as PlaytestRoutes from "../playtest/playtestRoutes.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as HeadlessUsageRefresher from "../usage/HeadlessUsageRefresher.ts";
import * as UsagePacer from "../usage/UsagePacer.ts";
import * as UsageReadingsHistory from "../usage/UsageReadingsHistory.ts";
import * as WorkAdmission from "../workAdmission.ts";
import * as ActivityProbe from "./activityProbe.ts";
import * as DelegatedOrphanScan from "./delegatedResults/DelegatedOrphanScan.ts";
import * as DelegatedResultReminder from "./delegatedResults/DelegatedResultReminder.ts";
import * as FactsCommands from "./factsCommand.ts";
import * as FillerDelegate from "./fillerDelegate.ts";
import * as ForkFeatures from "./forkFeatures.ts";
import * as HandoffOwner from "./handoff/handoffOwner.ts";
import * as HandoffService from "./handoff/HandoffService.ts";
import * as McpProfileTurnCheck from "./mcpProfileTurnCheck.ts";
import * as PullRequestWatchSlot from "./pullRequestWatchSlot.ts";
import * as RemoteTaskPrompts from "./remoteTaskPrompts.ts";
import * as LocalTaskPromptWakes from "./localTaskPromptWakes.ts";
import * as PromptMirror from "./promptMirror.ts";
import * as RemoteTaskPromptWakes from "./remoteTaskPromptWakes.ts";
import * as RunRecorder from "./runRecords/RunRecorder.ts";
import * as StopFinishedChildren from "./stopFinishedChildren.ts";
import * as ThreadRoleTargets from "./threadRoleTargetsLayer.ts";
import * as WorkerWakeSlots from "./workerWakeSlots.ts";
import * as WorkPanel from "./workPanel/workPanel.ts";

/**
 * Wraps upstream's agent activity relay: a held server (`serve --hold-work`) publishes none. A
 * completed handoff run does not alert through upstream's own shell read (fork/handoff/handoffShell.ts).
 */
export const holdWorkAgentActivity = <E, R>(
  relayLayer: Layer.Layer<AgentAwarenessRelay.AgentAwarenessRelay, E, R>,
) => HoldWorkConnect.agentActivityLayer(relayLayer);

/**
 * Upstream's Connect link with its reachability merged in, as server.ts had it, except that a held
 * server starts, registers and releases no tunnel.
 */
export const holdWorkCloudLink = <E, R, ROut2, E2, R2>(
  cloudLinkLayer: Layer.Layer<CloudLink.CloudLink, E, R>,
  reachabilityLayer: Layer.Layer<ROut2, E2, R2>,
) => HoldWorkConnect.layer(cloudLinkLayer).pipe(Layer.provideMerge(reachabilityLayer));

/**
 * In server.ts's `layerRuntimeCoreDependenciesBase`: usage pacer and history, slot gate, usage
 * refresher, handoff service (beside upstream's idle compaction worker), run records, result
 * reminder, finished children's session stop, the `threadRoles` save check's thread lookup, the
 * facts read's `factsCommand` runs, and the work panel's shared runs read.
 */
export const layerRuntimeServices = Layer.mergeAll(
  UsagePacer.layer.pipe(Layer.provideMerge(UsageReadingsHistory.layer)),
  // fork.workPanel: the thread side panel's shared GitHub runs read and this machine's capacity,
  // which reads this one slot gate (fork issue 1136).
  WorkPanel.layer.pipe(Layer.provideMerge(SlotGate.layer)),
  HeadlessUsageRefresher.layer,
  HandoffService.layer,
  RunRecorder.layer,
  DelegatedResultReminder.layer,
  StopFinishedChildren.layer,
  ThreadRoleTargets.layer,
  FactsCommands.layer,
);

/**
 * For the runtime's own workers only (first in server.ts's `layerRuntimeCoreDependenciesBase`):
 * idle compaction skips a thread the handoff service owns, then a run into an idle thread from a
 * pull request watch, idle compaction or a delegated task's question takes an agent slot.
 */
export const handoffThreadsSkipIdleCompaction = PullRequestWatchSlot.layer.pipe(
  Layer.provide(HandoffOwner.layer),
  Layer.provide(WorkerWakeSlots.layer),
);

/**
 * Right below upstream's orchestration layer in server.ts, so every turn reads its adapter from it
 * (fork/mcpProfileTurnCheck.ts): off Claude it refuses a profile that leaves out an outside server
 * and fixes the turn's T3 tool groups. holdWork.boot.test.ts checks the reads go through it.
 */
export const mcpProfileTurnCheck = McpProfileTurnCheck.layer;

/** In the routes, after upstream's follower of tasks in linked environments. */
export const layerRoutes = Layer.mergeAll(
  // /mcp reads the usage pacer and the slot gate only if they are there (the usage pacing gate,
  // agentSlots). Asking for them beside /mcp makes a server wired without one a type error
  // instead of a silent gate. The span shows every boot found both (holdWork.boot.test.ts): a
  // server.ts that lost this layer and layerRuntimeServices together still type-checks and boots.
  Layer.effectDiscard(
    Effect.gen(function* () {
      yield* UsagePacer.UsagePacer;
      yield* SlotGate.SlotGate;
    }).pipe(Effect.withSpan("ForkLayers.mcpGatesPresent")),
  ),
  // Off unless automaticUpdates is on; shares the Update button's ServerSelfUpdate in server.ts.
  ServerAutoUpdate.layer,
  // GET /api/fork/activity: 404 unless activityProbe is on; loopback programs only.
  ActivityProbe.layer,
  // GET /api/fork/features: what this version does for a linked machine; its token only.
  ForkFeatures.layer,
  // GET /api/fork/playtest/state and POST /api/fork/playtest/end: the thread's playtest chip and
  // its End button (playtest F5); a signed-in session only.
  PlaytestRoutes.layer,
  // Linked threads a parent here launched but whose task the last stop lost: one reminder each.
  DelegatedOrphanScan.layer,
  // GET /api/fork/task-prompts: a task's open prompts here, for its parent's machine; its token only.
  RemoteTaskPrompts.layer,
  // A parent here whose task on a linked machine waits there on a prompt: woken once per prompt.
  RemoteTaskPromptWakes.layer,
  // POST/GET /api/fork/delegate: the slot filler starts and counts a thread's children; a session token only.
  FillerDelegate.layer,
  // A parent here whose task's thread here waits on an approval, secret or pairing card.
  LocalTaskPromptWakes.layer,
  // GET /api/fork/prompt-mirror: what a parent's open tasks wait on, here and on linked machines.
  PromptMirror.layer,
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
