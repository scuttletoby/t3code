/**
 * Fork (work panel): `fork.workPanel` on the server. `read` is ws.ts's handler (one seam
 * line); it reads the service only if it is there, so ws.ts's requirements do not change, and a
 * server without it answers no lines (unknown in the panel). Built in forkLayers.ts.
 */
import {
  type ForkMachineCapacity,
  type ForkWorkPanelAnswer,
  type ForkWorkPanelInput,
  type OrchestrationV2ThreadShell,
  type ThreadId,
} from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as Context from "effect/Context";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";

import * as ThreadManagementService from "../../orchestration-v2/ThreadManagementService.ts";
import * as ProcessRunner from "../../processRunner.ts";
import * as HostResources from "../../resourceTelemetry/HostResources.ts";
import * as ServerSettings from "../../serverSettings.ts";
import * as SlotGate from "../../slots/SlotGate.ts";
import * as EnvironmentFacts from "../environmentFacts.ts";
import {
  threadPullRequestsOf,
  visibleThreadPullRequests,
} from "../../sourceControl/threadPullRequests.ts";
import {
  type GitHubRunsAnswer,
  type GitHubRunsSource,
  makeRepoQueueCache,
  pullRequestRuns,
  repoQueueKey,
} from "./repoQueue.ts";
import { capacityFromFacts } from "./capacity.ts";

/** The most repositories one read shows: those with the most linked pull requests first. */
const MAX_REPOSITORIES_PER_READ = 3;
const GH_TIMEOUT = Duration.seconds(30);
const GH_MAX_OUTPUT_BYTES = 32 * 1024 * 1024;

class ForkWorkPanel extends Context.Service<
  ForkWorkPanel,
  { readonly read: (input: ForkWorkPanelInput) => Effect.Effect<ForkWorkPanelAnswer> }
>()("t3/fork/workPanel/workPanel/ForkWorkPanel") {}

/**
 * The pull requests of `threadId` and every thread under it (by `lineage.parentThreadId`), as
 * the panel lists them: a sibling's or the root's are not this thread's, so they take none of the
 * repository reads.
 */
const subtreeLinks = (threadId: string, shells: ReadonlyArray<OrchestrationV2ThreadShell>) => {
  const inTree = new Set([threadId]);
  for (let grew = true; grew;) {
    grew = false;
    for (const shell of shells) {
      const parent = shell.lineage?.parentThreadId;
      if (parent != null && inTree.has(parent) && !inTree.has(shell.id)) {
        inTree.add(shell.id);
        grew = true;
      }
    }
  }
  return shells
    .filter((shell) => inTree.has(shell.id))
    .flatMap((shell) => visibleThreadPullRequests(threadPullRequestsOf(shell)));
};

/**
 * The answer: with `threadId`, CI for its family, given its shells and the shared cache; without
 * it, this machine's capacity (when `readCapacity` is given).
 */
export const makeRead = (
  readFamily: (
    threadId: ThreadId,
  ) => Effect.Effect<ReadonlyArray<OrchestrationV2ThreadShell> | null>,
  cache: Effect.Success<ReturnType<typeof makeRepoQueueCache>>,
  readCapacity?: Effect.Effect<ForkMachineCapacity>,
) =>
  Effect.fn("ForkWorkPanel.read")(function* (input: ForkWorkPanelInput) {
    // CI with a thread, capacity without one: never both, so neither waits on the other.
    const family = input.threadId === undefined ? null : yield* readFamily(input.threadId);
    const capacity =
      input.threadId === undefined && readCapacity !== undefined ? yield* readCapacity : undefined;
    const shells = family ?? [];
    const links = input.threadId === undefined ? [] : subtreeLinks(input.threadId, shells);
    const byRepo = new Map<string, { host: string; repository: string; count: number }>();
    for (const link of links) {
      const key = repoQueueKey(link.host, link.repository);
      const seen = byRepo.get(key);
      byRepo.set(key, {
        host: link.host,
        repository: link.repository,
        count: (seen?.count ?? 0) + 1,
      });
    }
    const repositories = [...byRepo.values()]
      .toSorted((a, b) => b.count - a.count)
      .slice(0, MAX_REPOSITORIES_PER_READ)
      .map(({ host, repository }) => ({ host, repository }));
    const { lines, runs } =
      repositories.length === 0 ? { lines: [], runs: new Map() } : yield* cache.read(repositories);
    const pullRequests = links.flatMap((link) => {
      const repoRuns = runs.get(repoQueueKey(link.host, link.repository));
      const found = repoRuns === undefined ? null : pullRequestRuns(repoRuns, link);
      return found === null ? [] : [found];
    });
    return {
      answeredAt: yield* Clock.currentTimeMillis,
      repoQueues: lines,
      pullRequests,
      ...(capacity === undefined ? {} : { capacity }),
    } satisfies ForkWorkPanelAnswer;
  });

const firstLine = (text: string) => text.trim().split("\n")[0]?.trim().slice(0, 160) ?? "";

/** Reads `gh api -i` output: the status line, the headers, a blank line, the body. */
export const parseGhIncludeOutput = (stdout: string): GitHubRunsAnswer | null => {
  const text = stdout.replace(/\r\n/g, "\n");
  const match = /^HTTP\/[0-9.]+ (\d{3})[^\n]*\n/.exec(text);
  if (match === null) return null;
  const end = text.indexOf("\n\n", match[0].length - 1);
  const head = end === -1 ? text.slice(match[0].length) : text.slice(match[0].length, end);
  const headers: Record<string, string> = {};
  for (const line of head.split("\n")) {
    const colon = line.indexOf(":");
    if (colon > 0)
      headers[line.slice(0, colon).trim().toLowerCase()] = line.slice(colon + 1).trim();
  }
  return {
    _tag: "Response",
    status: Number(match[1]),
    headers,
    body: end === -1 ? "" : text.slice(end + 2),
  };
};

/** The GitHub CLI as the source: the server's own `gh` login, as the fork's other reads use. */
const ghRunsSource = (runner: ProcessRunner.ProcessRunner["Service"]): GitHubRunsSource =>
  Effect.fn("ForkWorkPanel.ghRuns")(function* ({ host, repository, perPage, status }) {
    const out = yield* runner
      .run({
        command: "gh",
        args: [
          "api",
          "-i",
          "--hostname",
          host,
          "-X",
          "GET",
          `repos/${repository}/actions/runs?per_page=${perPage}${status === undefined ? "" : `&status=${status}`}`,
        ],
        timeout: GH_TIMEOUT,
        timeoutBehavior: "timedOutResult",
        maxOutputBytes: GH_MAX_OUTPUT_BYTES,
      })
      .pipe(Effect.option);
    if (Option.isNone(out)) {
      return { _tag: "Unreachable", reason: "the GitHub CLI (gh) could not run on this server." };
    }
    if (out.value.timedOut) return { _tag: "Unreachable", reason: "the read timed out." };
    const parsed = parseGhIncludeOutput(out.value.stdout);
    if (parsed !== null) return parsed;
    return {
      _tag: "Unreachable",
      reason: firstLine(out.value.stderr) || "the GitHub CLI gave no answer.",
    } satisfies GitHubRunsAnswer;
  });

export const layer = Layer.effect(
  ForkWorkPanel,
  Effect.gen(function* () {
    const threads = yield* ThreadManagementService.ThreadManagementService;
    const runner = yield* ProcessRunner.ProcessRunner;
    const cache = yield* makeRepoQueueCache(ghRunsSource(runner));
    // The facts placement reads, with the services they read only if they are there, asked for
    // here so a server wired without the gate or the settings is a type error.
    const gate = yield* SlotGate.SlotGate;
    const settings = yield* ServerSettings.ServerSettingsService;
    const host = yield* Effect.serviceOption(HostResources.HostResources);
    const readCapacity = EnvironmentFacts.readWithSlotLimit.pipe(
      Effect.provideService(SlotGate.SlotGate, gate),
      Effect.provideService(ServerSettings.ServerSettingsService, settings),
      Option.isSome(host)
        ? Effect.provideService(HostResources.HostResources, host.value)
        : (effect) => effect,
      Effect.map(({ facts, slotLimit }) => capacityFromFacts(facts, slotLimit)),
    );
    const readFamily = (threadId: ThreadId) =>
      threads.readShellSnapshot({ familyOf: threadId }).pipe(
        Effect.flatten,
        Effect.map((snapshot) => snapshot.threads),
        // The thread cannot be read: no lines, which the panel shows as unknown.
        Effect.orElseSucceed(() => null),
      );
    return ForkWorkPanel.of({ read: makeRead(readFamily, cache, readCapacity) });
  }),
);

/** The handler upstream's ws.ts calls. */
export const read = (input: ForkWorkPanelInput): Effect.Effect<ForkWorkPanelAnswer> =>
  Effect.serviceOption(ForkWorkPanel).pipe(
    Effect.flatMap(
      Option.match({
        onNone: () =>
          Clock.currentTimeMillis.pipe(
            Effect.map((answeredAt) => ({ answeredAt, repoQueues: [], pullRequests: [] })),
          ),
        onSome: (service) => service.read(input),
      }),
    ),
  );
