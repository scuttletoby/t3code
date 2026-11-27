// @effect-diagnostics nodeBuiltinImport:off - Boots the real CLI in a child process on a temp home.
/**
 * Fork (`serve --hold-work`): boots the real `t3 serve` on one seeded temp home (work, a Connect link
 * and analytics on a local fake server, a task in a linked environment): held, which starts none of
 * it and marks the home; then normally, which runs the work but still holds Connect on the marked
 * home; then normally with the marker deleted, which releases the tunnel.
 */
import * as NodeChildProcess from "node:child_process";
import * as NodeCrypto from "node:crypto";
import * as NodeEvents from "node:events";
import * as NodeFS from "node:fs";
import * as NodeHttps from "node:https";
import * as NodeNet from "node:net";
import * as NodeOS from "node:os";
import * as NodePath from "node:path";
import * as NodeSqlite from "node:sqlite";
import * as NodeURL from "node:url";

import * as NodeServices from "@effect/platform-node/NodeServices";
import {
  EnvironmentId,
  EventId,
  MessageId,
  type ModelSelection,
  NodeId,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  RunId,
  ThreadId,
  TurnItemId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import { afterEach, describe, expect, it } from "vite-plus/test";

import * as HoldWorkConnect from "./cloud/holdWorkConnect.ts";
import * as ProjectStore from "./orchestration-v2/ProjectStore.ts";
import * as ProjectionStore from "./orchestration-v2/ProjectionStore.ts";
import * as SqlitePersistence from "./persistence/Sqlite.ts";
import * as WorkAdmission from "./workAdmission.ts";

const BIN = NodeURL.fileURLToPath(new URL("./bin.ts", import.meta.url));
const modelSelection = {
  instanceId: ProviderInstanceId.make("codex"),
  model: "gpt-5",
} satisfies ModelSelection;
const driver = ProviderDriverKind.make("codex");
const providerInstanceId = modelSelection.instanceId;
const projectId = ProjectId.make("project:hold-work");
// The project of the one thread the agent activity publisher would send; nothing else uses it.
const activityProjectId = ProjectId.make("project:hold-work-activity");

const children = new Set<NodeChildProcess.ChildProcess>();
afterEach(async () => {
  for (const child of children) await stop(child);
});

const freePort = () =>
  new Promise<number>((resolve, reject) => {
    const server = NodeNet.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() =>
        typeof address === "object" && address !== null
          ? resolve(address.port)
          : reject(new Error("no port")),
      );
    });
  });

/** Fires on every request the fake remote gets, so a wait re-checks. */
const remoteRequests = new NodeEvents.EventEmitter();

/** A local stand-in for the T3 Connect relay and the analytics endpoint; it records every request. */
interface FakeRemote {
  readonly url: string;
  readonly requests: Array<{
    readonly method: string;
    readonly path: string;
    readonly body: string;
  }>;
  readonly close: () => Promise<void>;
}

// The fake serves https with the localhost test certificate, which the booted servers trust:
// T3 Connect sends a link's token only to its configured https relay.
const REMOTE_CERT = NodeURL.fileURLToPath(
  new URL("./peer/fixtures/localhost-cert.pem", import.meta.url),
);
const REMOTE_KEY = NodeURL.fileURLToPath(
  new URL("./peer/fixtures/localhost-key.pem", import.meta.url),
);

const fakeRemote = () =>
  new Promise<FakeRemote>((resolve, reject) => {
    const requests: FakeRemote["requests"] = [];
    const tls = { cert: NodeFS.readFileSync(REMOTE_CERT), key: NodeFS.readFileSync(REMOTE_KEY) };
    const server = NodeHttps.createServer(tls, (request, response) => {
      let body = "";
      request.on("data", (chunk: Buffer) => (body += chunk.toString("utf8")));
      request.on("end", () => {
        const path = request.url ?? "";
        requests.push({ method: request.method ?? "", path, body });
        remoteRequests.emit("request");
        // Analytics accepts its batch. The relay refuses everything, so a server that reaches it
        // goes no further (no tunnel, no download of the tunnel client).
        const accepted = path === "/batch/";
        response.writeHead(accepted ? 200 : 401, { "content-type": "application/json" });
        response.end(accepted ? '{"status":1}' : "{}");
      });
    });
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (typeof address !== "object" || address === null) return reject(new Error("no port"));
      resolve({
        url: `https://127.0.0.1:${address.port}`,
        requests,
        close: () =>
          new Promise<void>((done) => {
            server.closeAllConnections();
            server.close(() => done());
          }),
      });
    });
  });

const relayRequests = (remote: FakeRemote) =>
  remote.requests.filter((request) => request.path.startsWith("/v1/"));
const activityPublishes = (remote: FakeRemote) =>
  remote.requests.filter(
    (request) =>
      request.path.endsWith("/agent-activity") &&
      decodeURIComponent(request.path).includes("/threads/thread:hold-work:finished/"),
  );
const heartbeats = (remote: FakeRemote) =>
  remote.requests.filter(
    (request) => request.path === "/batch/" && request.body.includes('"server.boot.heartbeat"'),
  );

/**
 * What node itself may need to start on some machines (a shared-library path; Windows' system
 * folder). The child gets these from this process and nothing else of its environment.
 */
const NODE_START_VARS = [
  "LD_LIBRARY_PATH",
  "DYLD_LIBRARY_PATH",
  "DYLD_FALLBACK_LIBRARY_PATH",
  "SystemRoot",
  "windir",
] as const;

/** The booted server's whole environment: a bare home on `root`, analytics and relay on `remoteUrl`. */
const childEnv = (
  root: string,
  remoteUrl: string,
  from: Readonly<Record<string, string | undefined>>,
): Record<string, string> => ({
  ...Object.fromEntries(
    NODE_START_VARS.flatMap((name) => {
      const value = from[name];
      return value === undefined ? [] : [[name, value]];
    }),
  ),
  // A bare home and PATH: no provider CLI, git, credentials or user config is reachable.
  // Analytics is on, but sent only to the local fake server.
  HOME: NodePath.join(root, "home"),
  PATH: NodePath.dirname(process.execPath),
  XDG_CACHE_HOME: NodePath.join(root, "cache"),
  XDG_CONFIG_HOME: NodePath.join(root, "config"),
  XDG_DATA_HOME: NodePath.join(root, "data"),
  XDG_STATE_HOME: NodePath.join(root, "state"),
  T3CODE_TELEMETRY_ENABLED: "true",
  T3CODE_TELEMETRY_HOST: remoteUrl,
  // A build with T3 Connect: startup then also reconciles the stored desired link. The build's
  // relay is the local fake server the stored link points at, so a held boot that reached it would
  // show there.
  T3CODE_RELAY_URL: remoteUrl,
  NODE_EXTRA_CA_CERTS: REMOTE_CERT,
  T3CODE_CLERK_PUBLISHABLE_KEY: "pk_test_hold_work",
  T3CODE_CLERK_CLI_OAUTH_CLIENT_ID: "client-hold-work",
  NO_COLOR: "1",
  // No managed policy, whatever this machine has (one that turns Connect off would hold the held
  // phase's wait forever): a missing file means none. Source runs are the dev build, which reads it.
  T3CODE_DEV_MANAGED_POLICY_PATH: NodePath.join(root, "no-policy.json"),
});

interface Boot {
  readonly child: NodeChildProcess.ChildProcess;
  readonly output: () => string;
  readonly exited: () => boolean;
}

const boot = async (
  root: string,
  remote: FakeRemote,
  extraArgs: ReadonlyArray<string>,
): Promise<Boot> => {
  const port = await freePort();
  let output = "";
  let exited = false;
  const child = NodeChildProcess.spawn(
    process.execPath,
    [
      "--experimental-strip-types",
      BIN,
      "serve",
      "--base-dir",
      NodePath.join(root, "t3"),
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
      "--no-browser",
      ...extraArgs,
    ],
    {
      cwd: root,
      env: childEnv(root, remote.url, process.env),
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  children.add(child);
  child.stdout?.on("data", (chunk: Buffer) => (output += chunk.toString("utf8")));
  child.stderr?.on("data", (chunk: Buffer) => (output += chunk.toString("utf8")));
  child.once("exit", () => {
    exited = true;
    children.delete(child);
  });
  return { child, output: () => output, exited: () => exited };
};

const stop = async (child: NodeChildProcess.ChildProcess) => {
  if (child.exitCode !== null || child.signalCode !== null) return;
  const exit = new Promise<void>((resolve) => child.once("exit", () => resolve()));
  child.kill("SIGTERM");
  // @effect-diagnostics-next-line globalTimers:off - A watchdog for a hung child outside any Effect runtime; it must never fire.
  const watchdog = setTimeout(() => child.kill("SIGKILL"), 30_000);
  await exit;
  clearTimeout(watchdog);
};

/**
 * Resolves once `check` holds. It re-checks on every output chunk from the server and every change
 * in its state folder (SQLite and waits.json writes), so it waits on events, not on a clock.
 */
const when = (what: string, started: Boot, dir: string, check: () => boolean) =>
  new Promise<void>((resolve, reject) => {
    // The state dir, and its logs dir for the trace file.
    const logsDir = NodePath.join(dir, "logs");
    NodeFS.mkdirSync(logsDir, { recursive: true });
    const watchers = [NodeFS.watch(dir), NodeFS.watch(logsDir)];
    let settled = false;
    const settle = (error?: unknown) => {
      if (settled) return;
      settled = true;
      for (const watcher of watchers) watcher.close();
      remoteRequests.off("request", probe);
      started.child.stdout?.off("data", probe);
      started.child.stderr?.off("data", probe);
      started.child.off("exit", probe);
      if (error === undefined) resolve();
      else reject(error);
    };
    const holds = () => {
      try {
        return check();
      } catch {
        // A file caught mid-write reads as not yet; the write's own event re-checks.
        return false;
      }
    };
    const probe = () => {
      if (holds()) return settle();
      if (started.exited()) {
        settle(new Error(`server exited while waiting for ${what}\n${started.output()}`));
      }
    };
    for (const watcher of watchers) watcher.on("change", probe);
    remoteRequests.on("request", probe);
    started.child.stdout?.on("data", probe);
    started.child.stderr?.on("data", probe);
    started.child.once("exit", probe);
    probe();
  });

/** Waits until the server listens: it writes its runtime file with its own pid. */
const ready = async (root: string, started: Boot) => {
  const stateDir = NodePath.join(root, "t3", "userdata");
  const runtimePath = NodePath.join(stateDir, "server-runtime.json");
  const read = () => {
    try {
      return JSON.parse(NodeFS.readFileSync(runtimePath, "utf8")) as {
        readonly pid?: number;
        readonly workHeld?: boolean;
      };
    } catch {
      return undefined;
    }
  };
  await when("server-runtime.json", started, stateDir, () => read()?.pid === started.child.pid);
  return read() ?? {};
};

/**
 * The names of the spans the server wrote to its trace file, from byte `from` on, how often each,
 * and the names of the spans each ran inside (a child ends, and so is written, before its parent).
 */
const spansFrom = (stateDir: string, from: number) => {
  const tracePath = NodePath.join(stateDir, "logs", "server.trace.ndjson");
  const text = NodeFS.existsSync(tracePath) ? NodeFS.readFileSync(tracePath) : Buffer.alloc(0);
  const names = new Set<string>();
  const counts = new Map<string, number>();
  const nameById = new Map<string, string>();
  const parentIds: Array<readonly [name: string, parentSpanId: string]> = [];
  for (const line of text.subarray(from).toString("utf8").split("\n")) {
    try {
      const span = JSON.parse(line) as {
        readonly name?: unknown;
        readonly spanId?: unknown;
        readonly parentSpanId?: unknown;
      };
      const name = span.name;
      if (typeof name === "string") {
        names.add(name);
        counts.set(name, (counts.get(name) ?? 0) + 1);
        if (typeof span.spanId === "string") nameById.set(span.spanId, name);
        if (typeof span.parentSpanId === "string") parentIds.push([name, span.parentSpanId]);
      }
    } catch {
      // A blank or partial line.
    }
  }
  const parents = new Map<string, Set<string>>();
  for (const [name, parentSpanId] of parentIds) {
    const parent = nameById.get(parentSpanId);
    if (parent === undefined) continue;
    const known = parents.get(name) ?? new Set<string>();
    known.add(parent);
    parents.set(name, known);
  }
  const parentsOf = (name: string) => [...(parents.get(name) ?? [])].toSorted();
  return { names, counts, parentsOf, end: text.length };
};

/** What each hold outside the work gate logs instead of starting. */
const HELD_LINES = [
  "Automatic project pull held (--hold-work)",
  "Startup analytics heartbeat held (--hold-work)",
  "Remote delegated task follower held (--hold-work)",
  "T3 Connect held (--hold-work)",
  "Agent activity publishing held (--hold-work)",
];

/** What a publisher that started logs at once, before any of the held lines above. */
const PUBLISHER_STARTED = /agent activity publishing (enabled|disabled|standby)/;

/**
 * Startup registers the tunnel after a random wait of up to 30 s, then reconciles the desired link.
 * The held boot waits for the held calls' own spans, so both steps have been reached.
 */
const HELD_CONNECT_STEPS = [
  "HoldWorkConnect.registerManagedTunnelRecovery",
  "HoldWorkConnect.reconcileDesiredLink",
];

/** The real CloudLink's spans: a held boot, or any boot of a marked copy, runs none of them. */
const CONNECT_SPANS = [
  "environment.cloud.registerManagedCloudTunnelRecovery",
  "environment.cloud.releaseManagedTunnelOnShutdown",
  "environment.cloud.startManagedCloudTunnelIfOriginConfirmed",
  "environment.cloud.reconcileDesiredLinkIfStillDesired",
];

/** The spans that show a hold was not kept: each runs only when its part starts. */
const HELD_SPANS = ["autoPullProjects", "RemoteDelegation.followOnce", ...CONNECT_SPANS];

/**
 * Spans only the fork's own server layers make while built (fork/forkLayers.ts). A server.ts that
 * lost `ForkLayers.layerRuntimeServices` and `layerRoutes`, or `ForkLayers.mcpProfileTurnCheck`,
 * still boots and keeps every hold, so only these show the gates are gone.
 */
const FORK_LAYER_SPANS = [
  "ForkLayers.mcpGatesPresent",
  "HeadlessUsageRefresher.make",
  "cloud.auto_update.make",
  "McpProfileTurnCheck.make",
  // The MCP profile's T3 tool rule on /mcp (fork/mcpT3ToolRule.ts) in McpHttpServer's `layer`.
  "McpT3ToolRule.make",
  // The agent slot gate on scheduled runs: `Layer.provide(SlotGate.layer)` in runtimeLayer.ts.
  "AgentSlotRoutes.scheduledRunsGated",
  // The record of who saved each scheduled task, which needs the server's state folder.
  "ScheduledTaskMcpProfiles.stored",
  // The `threadRoles` save check's thread lookup in `ForkLayers.layerRuntimeServices`. Without it
  // the app refuses every new role ("cannot be checked here") and nothing else fails.
  "ThreadRoleTargets.make",
];

const missingForkLayers = (spans: ReadonlySet<string>) =>
  FORK_LAYER_SPANS.filter((name) => !spans.has(name));

/**
 * Where `ForkLayers.mcpProfileTurnCheck` sits: the orchestration layer's adapter registry and its
 * runtime policy read each instance from it (its span runs inside theirs). Moved above
 * `layerOrchestrationApplication` in server.ts, it would still be built and still emit its make
 * span, but both would read upstream's registry, so a turn would not be checked.
 */
const TURN_CHECK_READERS = ["ProviderAdapterRegistryV2.get", "RuntimePolicyV2.resolve"];

/** What a normal boot of a marked copy logs instead of starting Connect and agent activity. */
const markedLines = (marker: string) =>
  ["T3 Connect held: ", "Agent activity publishing held: "].map(
    (part) => `${part}${marker} marks this home as a copy`,
  );

const tunnelReleases = (remote: FakeRemote) =>
  relayRequests(remote).filter(
    (request) =>
      request.method === "DELETE" &&
      /^\/v1\/client\/environment-links\/[^/]+\/tunnel$/.test(request.path),
  );

const seedThreads = (dbPath: string, nowMs: number, activityRoot: string) => {
  const at = (offsetMs: number) => DateTime.makeUnsafe(nowMs + offsetMs);
  const iso = (offsetMs: number) => DateTime.formatIso(at(offsetMs));
  const thread = (suffix: string, createdAt: DateTime.Utc, inProject = projectId) => ({
    id: EventId.make(`event:hold-work:${suffix}:thread`),
    type: "thread.created" as const,
    threadId: ThreadId.make(`thread:hold-work:${suffix}`),
    occurredAt: createdAt,
    payload: {
      createdBy: "user" as const,
      creationSource: "web" as const,
      id: ThreadId.make(`thread:hold-work:${suffix}`),
      projectId: inProject,
      title: `hold-work ${suffix}`,
      providerInstanceId,
      modelSelection,
      runtimeMode: "full-access" as const,
      interactionMode: "default" as const,
      branch: null,
      worktreePath: null,
      activeProviderThreadId: null,
      lineage: {
        parentThreadId: null,
        relationshipToParent: null,
        rootThreadId: ThreadId.make(`thread:hold-work:${suffix}`),
      },
      forkedFrom: null,
      createdAt,
      updatedAt: createdAt,
      archivedAt: null,
      settledOverride: null,
      settledAt: null,
      lastVisitedAt: null,
      deletedAt: null,
    },
  });
  const run = (
    suffix: string,
    status: "running" | "interrupted" | "failed" | "completed",
    at: {
      requestedAt: DateTime.Utc;
      startedAt: DateTime.Utc | null;
      completedAt: DateTime.Utc | null;
    },
  ) => ({
    id: RunId.make(`run:hold-work:${suffix}`),
    threadId: ThreadId.make(`thread:hold-work:${suffix}`),
    ordinal: 1,
    providerInstanceId,
    modelSelection,
    providerThreadId: null,
    userMessageId: MessageId.make(`message:hold-work:${suffix}`),
    rootNodeId: NodeId.make(`node:hold-work:${suffix}`),
    activeAttemptId: null,
    status,
    ...at,
    checkpointId: null,
    contextHandoffId: null,
  });
  const runEvent = (suffix: string, payload: ReturnType<typeof run>, type: string) => ({
    id: EventId.make(`event:hold-work:${suffix}:${type}:${payload.status}`),
    type: type as "run.created",
    threadId: payload.threadId,
    runId: payload.id,
    nodeId: payload.rootNodeId,
    driver,
    providerInstanceId,
    occurredAt: payload.completedAt ?? payload.startedAt ?? payload.requestedAt,
    payload,
  });
  const twoHoursAgo = at(-2 * 60 * 60_000);
  const midTurn = run("mid-turn", "running", {
    requestedAt: twoHoursAgo,
    startedAt: twoHoursAgo,
    completedAt: null,
  });
  const continued = run("continued", "interrupted", {
    requestedAt: twoHoursAgo,
    startedAt: twoHoursAgo,
    completedAt: at(-60 * 60_000),
  });
  const limitedRunning = run("limited", "running", {
    requestedAt: twoHoursAgo,
    startedAt: twoHoursAgo,
    completedAt: null,
  });
  const limited = { ...limitedRunning, status: "failed" as const, completedAt: at(-90 * 60_000) };
  // Finished after the server started (dated ahead of the boot), so the agent activity publisher
  // sends it on its first snapshot: the phone shows "Agent finished".
  const finished = run("finished", "completed", {
    requestedAt: at(-5 * 60_000),
    startedAt: at(-5 * 60_000),
    completedAt: at(10 * 60_000),
  });
  return Effect.gen(function* () {
    yield* (yield* ProjectStore.ProjectStoreV2).apply({
      sequence: 0,
      eventId: EventId.make("event:hold-work:activity-project"),
      aggregateKind: "project",
      aggregateId: activityProjectId,
      occurredAt: iso(-2 * 60 * 60_000),
      commandId: null,
      causationEventId: null,
      correlationId: null,
      metadata: {},
      type: "project.created",
      payload: {
        projectId: activityProjectId,
        title: "hold-work activity",
        workspaceRoot: activityRoot,
        defaultModelSelection: null,
        scripts: [],
        createdAt: iso(-2 * 60 * 60_000),
        updatedAt: iso(-2 * 60 * 60_000),
      },
    });
    const store = yield* ProjectionStore.ProjectionStoreV2;
    for (const event of [
      thread("finished", at(-5 * 60_000), activityProjectId),
      runEvent("finished", finished, "run.created"),
      thread("mid-turn", twoHoursAgo),
      runEvent("mid-turn", midTurn, "run.created"),
      thread("continued", twoHoursAgo),
      runEvent("continued", continued, "run.created"),
      thread("limited", twoHoursAgo),
      runEvent("limited", limitedRunning, "run.created"),
      runEvent("limited", limited, "run.updated"),
      thread("remote", twoHoursAgo),
      {
        id: EventId.make("event:hold-work:remote:task"),
        type: "subagent.updated" as const,
        threadId: ThreadId.make("thread:hold-work:remote"),
        occurredAt: twoHoursAgo,
        payload: {
          id: NodeId.make("node:hold-work:remote-task"),
          threadId: ThreadId.make("thread:hold-work:remote"),
          runId: null,
          parentNodeId: NodeId.make("node:hold-work:remote"),
          origin: "app_owned" as const,
          createdBy: "agent" as const,
          driver,
          providerInstanceId,
          providerThreadId: null,
          childThreadId: null,
          // Running in an environment this home has no link to: a follower that starts looks
          // for it, and fails, without leaving the machine.
          remoteChild: {
            environmentId: EnvironmentId.make("environment-hold-work-remote"),
            threadId: ThreadId.make("thread:hold-work:there"),
            label: "linked",
          },
          nativeTaskRef: null,
          prompt: "Run there",
          title: "remote task",
          model: null,
          status: "running" as const,
          result: null,
          startedAt: twoHoursAgo,
          completedAt: null,
          updatedAt: twoHoursAgo,
        },
      },
      {
        id: EventId.make("event:hold-work:limited:error"),
        type: "turn-item.updated" as const,
        threadId: limited.threadId,
        runId: limited.id,
        nodeId: limited.rootNodeId,
        driver,
        occurredAt: limited.completedAt,
        payload: {
          id: TurnItemId.make("item:hold-work:limited:error"),
          threadId: limited.threadId,
          runId: limited.id,
          nodeId: limited.rootNodeId,
          providerThreadId: null,
          providerTurnId: null,
          nativeItemRef: null,
          parentItemId: null,
          ordinal: 1,
          status: "failed" as const,
          title: "Usage limit reached",
          startedAt: limited.completedAt,
          completedAt: limited.completedAt,
          updatedAt: limited.completedAt,
          type: "error" as const,
          failure: {
            class: "usage_limit" as const,
            message: "Plan limit reached.",
            // Reset an hour ago: an auto-resume is due now.
            resetAt: iso(-60 * 60_000),
            code: "usageLimitExceeded",
            retryable: null,
          },
        },
      },
    ]) {
      yield* store.apply(event as never);
    }
  }).pipe(
    Effect.provide(
      Layer.merge(ProjectionStore.layer, ProjectStore.layer).pipe(
        Layer.provideMerge(SqlitePersistence.layerFromPath(dbPath)),
        Layer.provide(NodeServices.layer),
      ),
    ),
    Effect.runPromise,
  );
};

/** A T3 Connect link as a full copy of a linked home carries it, with the relay on `relayUrl`. */
const CONNECT_SECRETS = (relayUrl: string): Record<string, string> => ({
  // The signing key a linked home has made (any boot makes it when it is missing).
  "cloud-link-ed25519-key-pair": JSON.stringify(
    NodeCrypto.generateKeyPairSync("ed25519", {
      privateKeyEncoding: { type: "pkcs8", format: "pem" },
      publicKeyEncoding: { type: "spki", format: "pem" },
    }),
  ),
  "cloud-endpoint-runtime-config": JSON.stringify({
    providerKind: "cloudflare_tunnel",
    connectorToken: "connector-hold-work",
    tunnelId: "tunnel-hold-work",
  }),
  "cloud-relay-url": relayUrl,
  "cloud-linked-user-id": "user-hold-work",
  "cloud-relay-environment-credential": "credential-hold-work",
  "cloud-cli-desired-link": "managed",
  // Agent activity publishing on, as `t3 connect publish` leaves it.
  "cloud-publish-agent-activity": "true",
  "cloud-cli-oauth-token": JSON.stringify({
    accessToken: "access-hold-work",
    refreshToken: "refresh-hold-work",
    // Far from expiry, so nothing refreshes it.
    expiresAtEpochMs: DateTime.toEpochMillis(DateTime.nowUnsafe()) + 7 * 24 * 60 * 60_000,
  }),
});

const seed = async (root: string, relayUrl: string) => {
  const stateDir = NodePath.join(root, "t3", "userdata");
  const dbPath = NodePath.join(stateDir, "statev2.sqlite");
  NodeFS.mkdirSync(stateDir, { recursive: true });
  for (const dir of ["home", "cache", "config", "data", "state"]) {
    NodeFS.mkdirSync(NodePath.join(root, dir), { recursive: true });
  }
  NodeFS.writeFileSync(
    NodePath.join(stateDir, "settings.json"),
    JSON.stringify({ waitFor: { enabled: true }, autoResumeLimitedThreads: true }),
  );
  // The child server reads the real clock, so the seed is dated from it (due, overdue, expired).
  const nowMs = DateTime.toEpochMillis(DateTime.nowUnsafe());
  const activityRoot = NodePath.join(root, "activity-project");
  NodeFS.mkdirSync(activityRoot, { recursive: true });
  await seedThreads(dbPath, nowMs, activityRoot);
  const iso = (offsetMs: number) => DateTime.formatIso(DateTime.makeUnsafe(nowMs + offsetMs));
  {
    using db = new NodeSqlite.DatabaseSync(dbPath);
    db.prepare(
      `INSERT INTO scheduled_tasks (task_id, title, prompt, enabled, schedule_json, project_id,
        thread_id, workspace_strategy_json, model_selection_json, runtime_mode, interaction_mode,
        created_by, creation_source, created_at, updated_at, next_run_at, last_run_at,
        last_run_status, last_run_error, run_count)
       VALUES ('task:hold-work', 'due task', 'Run task', 1, '{"type":"interval","everyMs":60000}',
        ?, NULL, '{"type":"root"}', ?, 'full-access', 'default', 'user', 'web', ?, ?, ?, NULL,
        'never', NULL, 0)`,
    ).run(
      projectId,
      JSON.stringify(modelSelection),
      iso(-60 * 60_000),
      iso(-60 * 60_000),
      iso(-60_000),
    );
    db.prepare(
      `INSERT INTO orchestration_v2_effect_outbox (effect_id, command_id, thread_id, effect_type,
        payload_json, status, attempt_count, available_at, lease_owner, lease_expires_at,
        created_at, updated_at, completed_at, last_error)
       VALUES ('effect:hold-work:continue', 'command:hold-work:continue',
        'thread:hold-work:continued', 'provider-runtime.continue', ?, 'pending', 0, ?, NULL, NULL,
        ?, ?, NULL, NULL)`,
    ).run(
      // Replay-safe, so startup recovery keeps it: only the outbox could start it.
      JSON.stringify({ type: "provider-runtime.continue", sourceRunId: "run:hold-work:continued" }),
      iso(-60_000),
      iso(-60_000),
      iso(-60_000),
    );
  }
  const secretsDir = NodePath.join(stateDir, "secrets");
  NodeFS.mkdirSync(secretsDir, { recursive: true, mode: 0o700 });
  for (const [name, value] of Object.entries(CONNECT_SECRETS(relayUrl))) {
    NodeFS.writeFileSync(NodePath.join(secretsDir, `${name}.bin`), value, { mode: 0o600 });
  }
  const watched = NodePath.join(root, "watched-file");
  NodeFS.writeFileSync(watched, "here");
  NodeFS.writeFileSync(
    NodePath.join(stateDir, "waits.json"),
    `${JSON.stringify({
      version: 1,
      waits: [
        {
          id: "w_abcdefghij",
          threadId: "thread:hold-work:mid-turn",
          projectId,
          key: "0123456789abcdef", // gitleaks:allow (a fake test value, not a key)
          createdAt: iso(-60 * 60_000),
          deadlineAt: iso(23 * 60 * 60_000),
          state: "waiting",
          attempt: 0,
          kind: "file",
          // Already met: any wait sweep that runs ends it.
          spec: { path: watched, until: "exists" },
        },
      ],
    })}\n`,
  );
  return { stateDir, dbPath };
};

interface Snapshot {
  readonly task: Record<string, unknown>;
  readonly effects: ReadonlyArray<Record<string, unknown>>;
  readonly runs: ReadonlyArray<Record<string, unknown>>;
  readonly limitedThread: Record<string, unknown>;
  readonly sessions: number;
  readonly waits: ReadonlyArray<{ readonly id: string; readonly state: string }>;
  readonly remoteTask: Record<string, unknown>;
  readonly connect: Record<string, string>;
}

const snapshot = (paths: { stateDir: string; dbPath: string }): Snapshot => {
  using db = new NodeSqlite.DatabaseSync(paths.dbPath, { readOnly: true });
  const waits = JSON.parse(
    NodeFS.readFileSync(NodePath.join(paths.stateDir, "waits.json"), "utf8"),
  ) as { waits: Array<{ id: string; state: string }> };
  return {
    task: db
      .prepare(
        `SELECT next_run_at, last_run_at, last_run_status, run_count FROM scheduled_tasks
         WHERE task_id = 'task:hold-work'`,
      )
      .get() as Record<string, unknown>,
    effects: db
      .prepare(
        `SELECT effect_id, thread_id, effect_type, status, attempt_count
         FROM orchestration_v2_effect_outbox ORDER BY effect_id`,
      )
      .all() as Array<Record<string, unknown>>,
    runs: db
      .prepare(
        `SELECT run_id, thread_id, status FROM orchestration_v2_projection_runs ORDER BY run_id`,
      )
      .all() as Array<Record<string, unknown>>,
    limitedThread: db
      .prepare(
        `SELECT json_extract(payload_json, '$.limitRecovery') AS limit_recovery
         FROM orchestration_v2_projection_threads WHERE thread_id = 'thread:hold-work:limited'`,
      )
      .get() as Record<string, unknown>,
    sessions: (
      db
        .prepare(`SELECT count(*) AS count FROM orchestration_v2_projection_provider_sessions`)
        .get() as {
        count: number;
      }
    ).count,
    waits: waits.waits.map(({ id, state }) => ({ id, state })),
    remoteTask: db
      .prepare(
        `SELECT payload_json FROM orchestration_v2_projection_subagents
         WHERE subagent_id = 'node:hold-work:remote-task'`,
      )
      .get() as Record<string, unknown>,
    // Every Connect file in the copy, by name, so a written, changed or removed one shows.
    connect: Object.fromEntries(
      NodeFS.readdirSync(NodePath.join(paths.stateDir, "secrets"))
        .filter((name) => name.startsWith("cloud-"))
        .map((name) => [
          name,
          NodeFS.readFileSync(NodePath.join(paths.stateDir, "secrets", name), "utf8"),
        ]),
    ),
  };
};

describe("serve --hold-work on a seeded home", () => {
  it("starts the server with only what node needs from this machine's environment", () => {
    const env = childEnv("/root-of-test", "https://127.0.0.1:1", {
      LD_LIBRARY_PATH: "/opt/node/lib",
      SystemRoot: "C:\\Windows",
      HOME: "/home/someone",
      PATH: "/usr/local/bin:/usr/bin",
      GH_TOKEN: "token-from-this-machine",
      T3CODE_HOME: "/home/someone/.t3",
      T3CODE_DEV_MANAGED_POLICY_PATH: "/etc/somewhere/policy.json",
    });
    // A node that links a shared library from LD_LIBRARY_PATH still starts.
    expect(env.LD_LIBRARY_PATH).toBe("/opt/node/lib");
    expect(env.SystemRoot).toBe("C:\\Windows");
    // Nothing else of this machine's: not its home, PATH, tokens, T3 home or policy.
    expect(env.HOME).toBe(NodePath.join("/root-of-test", "home"));
    expect(env.PATH).toBe(NodePath.dirname(process.execPath));
    expect(env.GH_TOKEN).toBeUndefined();
    expect(env.T3CODE_HOME).toBeUndefined();
    expect(env.T3CODE_DEV_MANAGED_POLICY_PATH).toBe(
      NodePath.join("/root-of-test", "no-policy.json"),
    );
    // A machine without them passes none on.
    expect(Object.keys(childEnv("/r", "https://127.0.0.1:1", {}))).not.toContain("LD_LIBRARY_PATH");
  });

  it("a held boot consumes no seeded work; a normal boot on the same home then runs it, Connect only once unmarked", async () => {
    const root = NodeFS.mkdtempSync(NodePath.join(NodeOS.tmpdir(), "t3-hold-work-boot-"));
    const remote = await fakeRemote();
    try {
      const paths = await seed(root, remote.url);
      const seeded = snapshot(paths);
      expect(seeded.task).toMatchObject({ last_run_status: "never", run_count: 0 });
      expect(seeded.effects).toEqual([
        expect.objectContaining({ effect_id: "effect:hold-work:continue", status: "pending" }),
      ]);
      expect(seeded.waits).toEqual([{ id: "w_abcdefghij", state: "waiting" }]);

      expect(Object.keys(seeded.connect)).toHaveLength(8);
      expect(seeded.remoteTask).toBeDefined();

      // A home linked before held copies were marked: no marker until it boots held.
      const marker = NodePath.join(root, "t3", HoldWorkConnect.HELD_COPY_MARKER_FILE);
      expect(NodeFS.existsSync(marker)).toBe(false);
      const held = await boot(root, remote, ["--hold-work"]);
      const runtime = await ready(root, held);
      expect(runtime.workHeld).toBe(true);
      // A held scheduler never schedules a source, so there is no tick to wait out: each source
      // logs that it is held instead. A source that ignored the hold would run at activation and
      // never log this line, so the wait would fail.
      const heldSources = ["scheduled-tasks", "waits", "usage-limit-recovery"];
      await when("the held sources", held, paths.stateDir, () => {
        const output = held.output();
        const spans = spansFrom(paths.stateDir, 0).names;
        // A leak ends the wait at once, so the checks below name it instead of a timeout.
        if (
          relayRequests(remote).length > 0 ||
          PUBLISHER_STARTED.test(output) ||
          HELD_SPANS.some((name) => spans.has(name))
        ) {
          return true;
        }
        return (
          output.includes("Work is held: every consumer uses the held gate (--hold-work)") &&
          heldSources.every((source) =>
            new RegExp(`Scheduler source held \\(--hold-work\\)[\\s\\S]{0,400}?${source}`).test(
              output,
            ),
          ) &&
          // The holds outside the work gate. Each part logs this instead of starting, so a part
          // that ignored the hold never logs it and the wait fails.
          HELD_LINES.every((line) => output.includes(line)) &&
          // Past the random wait before registration, and past the desired-link reconcile.
          HELD_CONNECT_STEPS.every((name) => spans.has(name))
        );
      });
      await stop(held.child);
      // The stopped copy stays marked, so `t3 connect` refuses to unlink or link it.
      const markerText = NodeFS.readFileSync(marker, "utf8");
      expect(markerText).toContain("--hold-work");
      const heldSpans = spansFrom(paths.stateDir, 0);
      // The held boot says so in its log, and still builds every fork layer (gates included).
      expect(held.output()).toContain(WorkAdmission.HOLD_WORK_BOOT_MESSAGE);
      expect(missingForkLayers(heldSpans.names)).toEqual([]);
      // The publisher logs its state when it starts, before any held line: a held one never does.
      expect(held.output()).not.toMatch(PUBLISHER_STARTED);

      const afterHeld = snapshot(paths);
      // Nothing was claimed, advanced or marked failed.
      expect(afterHeld.task).toEqual(seeded.task);
      expect(afterHeld.waits).toEqual(seeded.waits);
      expect(afterHeld.limitedThread).toEqual(seeded.limitedThread);
      expect(afterHeld.sessions).toBe(0);
      // The holds outside the work gate: no relay request (no tunnel registered, started or
      // released), no heartbeat, no pull or follow, and the copied link and the task there as
      // they were.
      expect(relayRequests(remote)).toEqual([]);
      expect(heartbeats(remote)).toEqual([]);
      expect(HELD_SPANS.filter((name) => heldSpans.names.has(name))).toEqual([]);
      expect(afterHeld.connect).toEqual(seeded.connect);
      expect(afterHeld.remoteTask).toEqual(seeded.remoteTask);
      expect(
        afterHeld.effects.find((row) => row.effect_id === "effect:hold-work:continue"),
      ).toEqual(seeded.effects[0]);
      // Startup recovery runs on any boot: the mid-turn run had no live provider session, so it
      // ends (here cancelled) exactly as a normal boot would end it. Anything recovery writes is
      // outbox work, which stays pending and unclaimed under the hold.
      expect(afterHeld.runs).toEqual(
        seeded.runs.map((row) =>
          row.run_id === "run:hold-work:mid-turn"
            ? { ...row, status: expect.stringMatching(/^(cancelled|interrupted)$/) }
            : row,
        ),
      );
      for (const effect of afterHeld.effects) {
        expect(effect).toMatchObject({ status: expect.stringMatching(/^(pending|cancelled)$/) });
        if (effect.status === "pending") expect(effect.attempt_count).toBe(0);
      }

      // A normal boot of the marked copy runs the work, but Connect and agent activity stay held:
      // its stop would release the running server's tunnel under the shared environment id.
      const marked = await boot(root, remote, []);
      const markedRuntime = await ready(root, marked);
      expect(markedRuntime.workHeld).toBeUndefined();
      await when(
        "the marked copy's normal boot to pick up the seeded work",
        marked,
        paths.stateDir,
        () => {
          const now = snapshot(paths);
          const spans = spansFrom(paths.stateDir, heldSpans.end).names;
          // A leak ends the wait at once, so the checks below name it instead of a timeout.
          if (
            activityPublishes(remote).length > 0 ||
            CONNECT_SPANS.some((name) => spans.has(name))
          ) {
            return true;
          }
          return (
            now.task.last_run_status !== "never" &&
            now.waits.every((wait) => wait.state !== "waiting") &&
            now.limitedThread.limit_recovery !== null &&
            now.effects.some(
              (row) => row.effect_id === "effect:hold-work:continue" && row.status !== "pending",
            ) &&
            heartbeats(remote).length > 0 &&
            spans.has("RemoteDelegation.followOnce") &&
            // The seeded turns asked the orchestration layer for their adapter (each such span
            // ends after the instance read inside it), so the check below has its spans.
            TURN_CHECK_READERS.every((name) => spans.has(name)) &&
            markedLines(marker).every((line) => marked.output().includes(line)) &&
            // Past the random wait before registration, and past the desired-link reconcile.
            HELD_CONNECT_STEPS.every((name) => spans.has(name))
          );
        },
      );
      await stop(marked.child);
      const markedSpans = spansFrom(paths.stateDir, heldSpans.end);
      expect(missingForkLayers(markedSpans.names)).toEqual([]);
      expect(markedSpans.parentsOf("McpProfileTurnCheck.getInstance")).toEqual(
        expect.arrayContaining(TURN_CHECK_READERS),
      );
      // No tunnel registered, started or released, and no agent activity sent.
      expect(tunnelReleases(remote)).toEqual([]);
      expect(activityPublishes(remote)).toEqual([]);
      expect(CONNECT_SPANS.filter((name) => markedSpans.names.has(name))).toEqual([]);
      expect(relayRequests(remote)).toEqual([]);
      expect(marked.output()).not.toMatch(PUBLISHER_STARTED);
      expect(marked.output()).not.toContain("--hold-work");
      expect(markedSpans.names).toContain("HoldWorkConnect.releaseManagedTunnel");
      // T3 Connect and agent activity share the boot's one marker read, so they cannot disagree.
      expect(markedSpans.counts.get("HoldWorkConnect.heldCopyMarker")).toBe(1);
      expect(markedSpans.names).toContain("autoPullProjects");
      expect(markedSpans.names).toContain("RemoteDelegation.followOnce");
      // A normal boot of the copy leaves its marker as it is.
      expect(NodeFS.readFileSync(marker, "utf8")).toBe(markerText);

      // With the marker deleted, the same home is no longer taken to be a copy.
      NodeFS.rmSync(marker);
      const normal = await boot(root, remote, []);
      await ready(root, normal);
      await when(
        "the unmarked home's normal boot to publish",
        normal,
        paths.stateDir,
        () =>
          // The finished thread's activity reaches the relay, so the held boots had it to send.
          activityPublishes(remote).length > 0,
      );
      expect(normal.output()).not.toContain("--hold-work");
      expect(normal.output()).not.toContain(HoldWorkConnect.HELD_COPY_MARKER_FILE);
      await stop(normal.child);
      // Its tunnel registration waits a random part of half a minute, but stopping releases the
      // tunnel at once: the relay has the release itself.
      const normalSpanCounts = spansFrom(paths.stateDir, markedSpans.end);
      const normalSpans = normalSpanCounts.names;
      expect(missingForkLayers(normalSpans)).toEqual([]);
      expect(normalSpanCounts.counts.get("HoldWorkConnect.heldCopyMarker")).toBe(1);
      expect(normalSpans).toContain("environment.cloud.releaseManagedTunnelOnShutdown");
      expect(tunnelReleases(remote)).toHaveLength(1);
      expect(NodeFS.existsSync(marker)).toBe(false);
    } finally {
      for (const child of children) await stop(child);
      await remote.close();
      NodeFS.rmSync(root, { recursive: true, force: true });
    }
  });
});
