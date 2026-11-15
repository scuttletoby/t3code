// @effect-diagnostics nodeBuiltinImport:off - Boots the real CLI in a child process on a temp home.
/**
 * Fork (`serve --hold-work`): boots the real `t3 serve` twice on one seeded temp home. The held
 * boot must start no turn and leave the seeded work as it was (startup recovery aside); a normal
 * boot must then run it. Dropping either flag pass-through (config.ts, server.ts) turns it red.
 */
import * as NodeChildProcess from "node:child_process";
import * as NodeFS from "node:fs";
import * as NodeNet from "node:net";
import * as NodeOS from "node:os";
import * as NodePath from "node:path";
import * as NodeSqlite from "node:sqlite";
import * as NodeURL from "node:url";

import * as NodeServices from "@effect/platform-node/NodeServices";
import {
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

import * as ProjectionStore from "./orchestration-v2/ProjectionStore.ts";
import * as SqlitePersistence from "./persistence/Sqlite.ts";

const BIN = NodeURL.fileURLToPath(new URL("./bin.ts", import.meta.url));
const modelSelection = {
  instanceId: ProviderInstanceId.make("codex"),
  model: "gpt-5",
} satisfies ModelSelection;
const driver = ProviderDriverKind.make("codex");
const providerInstanceId = modelSelection.instanceId;
const projectId = ProjectId.make("project:hold-work");

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

interface Boot {
  readonly child: NodeChildProcess.ChildProcess;
  readonly output: () => string;
  readonly exited: () => boolean;
}

const boot = async (root: string, extraArgs: ReadonlyArray<string>): Promise<Boot> => {
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
      // A bare home and PATH: no provider CLI, git, credentials or user config is reachable.
      env: {
        HOME: NodePath.join(root, "home"),
        PATH: NodePath.dirname(process.execPath),
        XDG_CACHE_HOME: NodePath.join(root, "cache"),
        XDG_CONFIG_HOME: NodePath.join(root, "config"),
        XDG_DATA_HOME: NodePath.join(root, "data"),
        XDG_STATE_HOME: NodePath.join(root, "state"),
        T3CODE_TELEMETRY_ENABLED: "false",
        NO_COLOR: "1",
      },
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
    const watcher = NodeFS.watch(dir);
    let settled = false;
    const settle = (error?: unknown) => {
      if (settled) return;
      settled = true;
      watcher.close();
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
    watcher.on("change", probe);
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

const seedThreads = (dbPath: string, nowMs: number) => {
  const at = (offsetMs: number) => DateTime.makeUnsafe(nowMs + offsetMs);
  const iso = (offsetMs: number) => DateTime.formatIso(at(offsetMs));
  const thread = (suffix: string, createdAt: DateTime.Utc) => ({
    id: EventId.make(`event:hold-work:${suffix}:thread`),
    type: "thread.created" as const,
    threadId: ThreadId.make(`thread:hold-work:${suffix}`),
    occurredAt: createdAt,
    payload: {
      createdBy: "user" as const,
      creationSource: "web" as const,
      id: ThreadId.make(`thread:hold-work:${suffix}`),
      projectId,
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
    status: "running" | "interrupted" | "failed",
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
  return Effect.gen(function* () {
    const store = yield* ProjectionStore.ProjectionStoreV2;
    for (const event of [
      thread("mid-turn", twoHoursAgo),
      runEvent("mid-turn", midTurn, "run.created"),
      thread("continued", twoHoursAgo),
      runEvent("continued", continued, "run.created"),
      thread("limited", twoHoursAgo),
      runEvent("limited", limitedRunning, "run.created"),
      runEvent("limited", limited, "run.updated"),
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
      ProjectionStore.layer.pipe(
        Layer.provideMerge(SqlitePersistence.layerFromPath(dbPath)),
        Layer.provide(NodeServices.layer),
      ),
    ),
    Effect.runPromise,
  );
};

const seed = async (root: string) => {
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
  await seedThreads(dbPath, nowMs);
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
          key: "0123456789abcdef",
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
  };
};

describe("serve --hold-work on a seeded home", () => {
  it("a held boot consumes no seeded work; a normal boot on the same home then runs it", async () => {
    const root = NodeFS.mkdtempSync(NodePath.join(NodeOS.tmpdir(), "t3-hold-work-boot-"));
    try {
      const paths = await seed(root);
      const seeded = snapshot(paths);
      expect(seeded.task).toMatchObject({ last_run_status: "never", run_count: 0 });
      expect(seeded.effects).toEqual([
        expect.objectContaining({ effect_id: "effect:hold-work:continue", status: "pending" }),
      ]);
      expect(seeded.waits).toEqual([{ id: "w_abcdefghij", state: "waiting" }]);

      const held = await boot(root, ["--hold-work"]);
      const runtime = await ready(root, held);
      expect(runtime.workHeld).toBe(true);
      // A held scheduler never schedules a source, so there is no tick to wait out: each source
      // logs that it is held instead. A source that ignored the hold would run at activation and
      // never log this line, so the wait would fail.
      const heldSources = ["scheduled-tasks", "waits", "usage-limit-recovery"];
      await when("the held sources", held, paths.stateDir, () => {
        const output = held.output();
        return (
          output.includes("Work is held: every consumer uses the held gate (--hold-work)") &&
          heldSources.every((source) =>
            new RegExp(`Scheduler source held \\(--hold-work\\)[\\s\\S]{0,400}?${source}`).test(
              output,
            ),
          )
        );
      });
      await stop(held.child);

      const afterHeld = snapshot(paths);
      // Nothing was claimed, advanced or marked failed.
      expect(afterHeld.task).toEqual(seeded.task);
      expect(afterHeld.waits).toEqual(seeded.waits);
      expect(afterHeld.limitedThread).toEqual(seeded.limitedThread);
      expect(afterHeld.sessions).toBe(0);
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

      const normal = await boot(root, []);
      const normalRuntime = await ready(root, normal);
      expect(normalRuntime.workHeld).toBeUndefined();
      await when("the normal boot to pick up the seeded work", normal, paths.stateDir, () => {
        const now = snapshot(paths);
        return (
          now.task.last_run_status !== "never" &&
          now.waits.every((wait) => wait.state !== "waiting") &&
          now.limitedThread.limit_recovery !== null &&
          now.effects.some(
            (row) => row.effect_id === "effect:hold-work:continue" && row.status !== "pending",
          )
        );
      });
      expect(normal.output()).not.toContain("--hold-work");
      await stop(normal.child);
    } finally {
      for (const child of children) await stop(child);
      NodeFS.rmSync(root, { recursive: true, force: true });
    }
  });
});
