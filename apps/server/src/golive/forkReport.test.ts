// @effect-diagnostics nodeBuiltinImport:off - the tests build real SQLite homes and child processes.
import * as NodeChildProcess from "node:child_process";
import * as NodeCrypto from "node:crypto";
import * as NodeFS from "node:fs";
import * as NodeHttp from "node:http";
import * as NodeOS from "node:os";
import * as NodePath from "node:path";
import * as NodeSqlite from "node:sqlite";

import * as NodeCryptoLayer from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { afterEach, describe, expect, it } from "@effect/vitest";
import {
  DEFAULT_SERVER_SETTINGS,
  FORK_PROJECT_SCOPED_KEYS,
  FORK_SERVER_SETTING_KEYS,
  forkFeaturesWhere,
  ForkProjectOverrideFields,
  type ManagedPolicyDocument,
} from "@t3tools/contracts";
import * as NetService from "@t3tools/shared/Net";
import * as ConfigProvider from "effect/ConfigProvider";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as TestConsole from "effect/testing/TestConsole";
import { Command } from "effect/cli";
import { vi } from "vite-plus/test";

import { cli } from "../binCli.ts";
import { ACTIVITY_PROBE_PATH, ACTIVITY_PROBE_VERSION } from "../fork/activityProbe.ts";
import * as ServerSecretStore from "../auth/ServerSecretStore.ts";
import * as ServerConfig from "../config.ts";
import { BUILD_MIGRATION_HEAD } from "../persistence/databaseMigrationHead.ts";
import { migrationManifest } from "../persistence/Migrations.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import { makeManagedSettings } from "../policy/managedSettings.ts";
import * as ServerSettingsModule from "../serverSettings.ts";
import {
  FORK_REPORT_PROJECT_FEATURES,
  FORK_REPORT_VERSION,
  formatForkReport,
  readForkReport,
} from "./forkReport.ts";

/** Runs once, right after the report's last WAL check reads the path it names (the -shm). */
const afterCheck = vi.hoisted(() => ({ path: "", run: () => {} }));
/** A stat of this path finds nothing and a look at the path itself (lstat) fails with EACCES. */
const lstatFails = vi.hoisted(() => ({ path: "" }));
/** The next stat of this path finds nothing, as when a first start creates it just after the stat. */
const statMissesOnce = vi.hoisted(() => ({ path: "" }));
vi.mock("node:fs", async (importOriginal) => {
  const actual = await importOriginal<typeof NodeFS>();
  const existsSync: typeof actual.existsSync = (path) => {
    const exists = actual.existsSync(path);
    if (path === afterCheck.path) {
      afterCheck.path = "";
      afterCheck.run();
    }
    return exists;
  };
  const lstatSync = ((path: NodeFS.PathLike, ...rest: Array<unknown>) => {
    if (path === lstatFails.path) {
      throw Object.assign(new Error(`EACCES: permission denied, lstat '${String(path)}'`), {
        code: "EACCES",
      });
    }
    return (actual.lstatSync as (...args: Array<unknown>) => unknown)(path, ...rest);
  }) as typeof actual.lstatSync;
  const statSync = ((path: NodeFS.PathLike, ...rest: Array<unknown>) => {
    const missOnce = path === statMissesOnce.path;
    if (missOnce) statMissesOnce.path = "";
    if (missOnce || path === lstatFails.path) {
      throw Object.assign(new Error(`ENOENT: no such file or directory, stat '${String(path)}'`), {
        code: "ENOENT",
      });
    }
    return (actual.statSync as (...args: Array<unknown>) => unknown)(path, ...rest);
  }) as typeof actual.statSync;
  return { ...actual, existsSync, lstatSync, statSync };
});

/** The next `remaining` database opens throw, as an open can in a server's stop race. */
const openFails = vi.hoisted(() => ({ remaining: 0 }));
vi.mock("node:sqlite", async (importOriginal) => {
  const actual = await importOriginal<typeof NodeSqlite>();
  class DatabaseSync extends actual.DatabaseSync {
    constructor(...args: ConstructorParameters<typeof actual.DatabaseSync>) {
      if (openFails.remaining > 0) {
        openFails.remaining -= 1;
        throw new Error("unable to open database file");
      }
      super(...args);
    }
  }
  return { ...actual, DatabaseSync };
});

const homes: Array<string> = [];
const children: Array<NodeChildProcess.ChildProcess> = [];
afterEach(() => {
  openFails.remaining = 0;
  lstatFails.path = "";
  statMissesOnce.path = "";
  for (const child of children.splice(0)) child.kill("SIGKILL");
  for (const home of homes.splice(0)) NodeFS.rmSync(home, { recursive: true, force: true });
});

const tempDir = () => {
  const dir = NodeFS.mkdtempSync(NodePath.join(NodeOS.tmpdir(), "t3-fork-report-"));
  homes.push(dir);
  return dir;
};
const dbPathOf = (home: string) => NodePath.join(home, "userdata", "statev2.sqlite");
const settingsPathOf = (home: string) => NodePath.join(home, "userdata", "settings.json");

const SETTINGS = {
  waitFor: { enabled: false },
  providerInstances: {
    "claude-one": { driver: "claudeAgent" },
    "claude-two": { driver: "claudeAgent", enabled: false },
  },
};

/** A home with a closed WAL-mode database: two projects (one archived), three threads (one archived, one deleted). */
function makeHome(
  options: { migrations?: ReadonlyArray<readonly [number, string]>; settings?: string } = {},
) {
  const home = tempDir();
  NodeFS.mkdirSync(NodePath.join(home, "userdata"));
  const db = new NodeSqlite.DatabaseSync(dbPathOf(home));
  db.exec(`
    PRAGMA journal_mode = WAL;
    CREATE TABLE effect_sql_migrations (migration_id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE projection_projects (project_id TEXT PRIMARY KEY, archived_at TEXT, deleted_at TEXT);
    CREATE TABLE orchestration_v2_projection_threads (thread_id TEXT PRIMARY KEY, archived_at TEXT, deleted_at TEXT);
    INSERT INTO projection_projects VALUES ('p1', NULL, NULL), ('p2', 'x', NULL), ('p3', NULL, 'x');
    INSERT INTO orchestration_v2_projection_threads VALUES ('t1', NULL, NULL), ('t2', 'x', NULL), ('t3', NULL, 'x');
  `);
  const insert = db.prepare("INSERT INTO effect_sql_migrations VALUES (?, ?)");
  for (const [id, name] of options.migrations ?? migrationManifest) insert.run(id, name);
  db.close();
  NodeFS.writeFileSync(settingsPathOf(home), options.settings ?? `${JSON.stringify(SETTINGS)}\n`);
  return home;
}

/** Every path under `dir` with its bytes' hash, size and mtime (a link: its target, not followed). */
function snapshot(dir: string): Record<string, string> {
  const out: Record<string, string> = {};
  const walk = (current: string) => {
    for (const entry of NodeFS.readdirSync(current, { withFileTypes: true })) {
      const full = NodePath.join(current, entry.name);
      const stat = NodeFS.lstatSync(full);
      const rel = NodePath.relative(dir, full);
      if (entry.isSymbolicLink()) {
        out[rel] = `-> ${NodeFS.readlinkSync(full)} ${stat.mtimeMs}`;
      } else if (entry.isDirectory()) {
        out[`${rel}/`] = String(stat.mtimeMs);
        walk(full);
      } else {
        const hash = NodeCrypto.createHash("sha256")
          .update(NodeFS.readFileSync(full))
          .digest("hex");
        out[rel] = `${hash} ${stat.size} ${stat.mtimeMs}`;
      }
    }
  };
  walk(dir);
  return out;
}

/** Runs a script in another node process (SQLite shares one -shm map per process). */
const childScript = (dbPath: string, body: string) => `
  const { DatabaseSync } = require("node:sqlite");
  const db = new DatabaseSync(${JSON.stringify(dbPath)});
  db.exec("PRAGMA journal_mode = WAL; PRAGMA wal_autocheckpoint = 0;");
  db.exec("INSERT INTO orchestration_v2_projection_threads VALUES ('t4', NULL, NULL), ('t5', NULL, NULL)");
  ${body}
`;

/** A server that stopped without closing: its WAL holds two more threads and is never checkpointed. */
function leaveCrashedWal(home: string) {
  NodeChildProcess.execFileSync(process.execPath, [
    "-e",
    childScript(dbPathOf(home), "process.exit(0);"),
  ]);
  expect(NodeFS.statSync(`${dbPathOf(home)}-wal`).size).toBeGreaterThan(0);
}

/** A running server: it holds the ownership lock and has two threads in its WAL, never checkpointed. */
async function startWriter(home: string) {
  const lockPath = NodePath.join(home, "userdata", "server-owner.sqlite");
  const child = NodeChildProcess.spawn(
    process.execPath,
    [
      "-e",
      childScript(
        dbPathOf(home),
        `const lock = new DatabaseSync(${JSON.stringify(lockPath)});
         lock.exec("BEGIN EXCLUSIVE");
         process.stdout.write("ready\\n");
         setInterval(() => {}, 1000);`,
      ),
    ],
    { stdio: ["ignore", "pipe", "inherit"] },
  );
  children.push(child);
  await new Promise<void>((resolve, reject) => {
    child.stdout!.on("data", (data) => String(data).includes("ready") && resolve());
    child.on("exit", (code) => reject(new Error(`writer exited early (${code})`)));
  });
}

/** A home whose statev2.sqlite is only a link (`target` is what the link says). */
function homeWithDatabaseLink(target: string) {
  const home = tempDir();
  NodeFS.mkdirSync(NodePath.join(home, "userdata"));
  NodeFS.symlinkSync(target, dbPathOf(home));
  return home;
}

/** A policy's settings overlay; a test's policy must be one this build can apply. */
function managedOf(settings: ManagedPolicyDocument["settings"]) {
  const managed = makeManagedSettings(settings ?? {});
  if (managed._tag === "Invalid") throw new Error(managed.reason);
  return managed.settings;
}

/** No managed policy, whatever this machine has: a missing file means none. */
const NO_POLICY = "/nonexistent/t3-fork-report-policy.json";

const runCli = (args: ReadonlyArray<string>, policyPath: string = NO_POLICY) =>
  Command.runWith(cli, { version: "0.0.0" })(args).pipe(
    Effect.andThen(TestConsole.logLines),
    Effect.provide(
      Layer.mergeAll(
        NodeServices.layer,
        NodeCryptoLayer.layer,
        NetService.layer,
        // Fresh: only this command's lines, not those an earlier step of the test logged.
        Layer.fresh(TestConsole.layer),
        ConfigProvider.layer(
          ConfigProvider.fromEnv({ env: { T3CODE_DEV_MANAGED_POLICY_PATH: policyPath } }),
        ),
      ),
    ),
  );

/** Runs `__fork-report` through the real CLI, keeping the exit code it sets out of the test run. */
const reportThroughCli = (home: string, policyPath?: string) =>
  Effect.gen(function* () {
    const before = process.exitCode;
    process.exitCode = 0;
    let exitCode: number | string | null | undefined;
    const lines = yield* runCli(["__fork-report", "--base-dir", home], policyPath).pipe(
      Effect.ensuring(
        Effect.sync(() => {
          exitCode = process.exitCode;
          process.exitCode = before;
        }),
      ),
    );
    return { report: JSON.parse(lines.join("\n")), exitCode };
  });

describe("__fork-report writes nothing", () => {
  it.effect("reads a stopped home and leaves every file byte-identical with the same mtime", () =>
    Effect.gen(function* () {
      const home = makeHome();
      const before = snapshot(home);

      const { report } = yield* reportThroughCli(home);

      expect(snapshot(home)).toEqual(before);
      // No -wal/-shm created, no ownership lock taken.
      expect(NodeFS.existsSync(`${dbPathOf(home)}-wal`)).toBe(false);
      expect(NodeFS.existsSync(NodePath.join(home, "userdata", "server-owner.sqlite"))).toBe(false);
      expect(report).toMatchObject({
        ok: true,
        problems: [],
        database: {
          state: "present",
          openMode: "immutable",
          pendingMigrations: [],
          unknownMigrations: [],
          counts: { projects: 2, projectsArchived: 1, threads: 2, threadsArchived: 1 },
        },
        settings: {
          state: "present",
          parseWarning: null,
          providerInstances: [
            { id: "claude-one", driver: "claudeAgent", enabled: true },
            { id: "claude-two", driver: "claudeAgent", enabled: false },
          ],
        },
        fork: {
          features: {
            agentSlots: "off",
            waitFor: "off",
            usagePacingGate: "off",
            automaticUpdates: "off",
          },
          featuresOn: [],
          keysInFile: ["waitFor"],
        },
      });
      expect(report.database.appliedMigrations.map((row: { id: number }) => row.id)).toEqual(
        migrationManifest.map(([id]) => id),
      );
    }),
  );

  it("reads the WAL a crashed server left, without writing -wal or -shm", () => {
    const home = makeHome();
    leaveCrashedWal(home);
    const before = snapshot(home);

    const report = readForkReport(home);

    expect(snapshot(home)).toEqual(before);
    expect(report.database.openMode).toBe("readonly-shm");
    expect(report.database.counts?.threads).toBe(4);
    expect(report.ok).toBe(true);
  });

  it("reads beside a running writer that holds the ownership lock, writing nothing", async () => {
    const home = makeHome();
    await startWriter(home);
    const before = snapshot(home);

    const report = readForkReport(home);

    expect(snapshot(home)).toEqual(before);
    expect(report.database.openMode).toBe("readonly-shm");
    expect(report.database.counts?.threads).toBe(4);
    expect(report.ok).toBe(true);
  });

  it("reads a home whose server stops cleanly between the WAL check and the open", () => {
    const home = makeHome();
    const server = new NodeSqlite.DatabaseSync(dbPathOf(home));
    server.exec("PRAGMA wal_autocheckpoint = 0");
    server.exec("INSERT INTO orchestration_v2_projection_threads VALUES ('t4', NULL, NULL)");
    expect(NodeFS.statSync(`${dbPathOf(home)}-wal`).size).toBeGreaterThan(0);
    // The last connection's close checkpoints the WAL into the file and deletes -wal and -shm.
    afterCheck.path = `${dbPathOf(home)}-shm`;
    afterCheck.run = () => server.close();

    const report = readForkReport(home);

    expect(afterCheck.path).toBe("");
    expect(report.problems).toEqual([]);
    expect(report.database.openMode).toBe("immutable");
    expect(report.database.counts?.threads).toBe(3);
    expect(report.ok).toBe(true);
  });

  it("refuses a WAL with data but no -shm index instead of recovering it", () => {
    const home = makeHome();
    leaveCrashedWal(home);
    NodeFS.rmSync(`${dbPathOf(home)}-shm`);
    const before = snapshot(home);

    const report = readForkReport(home);

    expect(snapshot(home)).toEqual(before);
    expect(report.database.state).toBe("unreadable");
    expect(report.problems.map((problem) => problem.code)).toEqual(["database-needs-recovery"]);
    expect(report.ok).toBe(false);
  });

  it.each([
    ["a stopped home", "immutable", 2],
    ["a crashed WAL", "readonly-shm", 4],
  ] as const)("retries an open of %s that fails once, writing nothing", (name, mode, threads) => {
    const home = makeHome();
    if (name === "a crashed WAL") leaveCrashedWal(home);
    const before = snapshot(home);
    openFails.remaining = 1;

    const report = readForkReport(home);

    expect(openFails.remaining).toBe(0);
    expect(snapshot(home)).toEqual(before);
    expect(report.problems).toEqual([]);
    expect(report.database.openMode).toBe(mode);
    expect(report.database.counts?.threads).toBe(threads);
    expect(report.ok).toBe(true);
  });

  it("reports an open that fails twice once, as the second attempt's problem", () => {
    const home = makeHome();
    openFails.remaining = 2;

    const report = readForkReport(home);

    expect(openFails.remaining).toBe(0);
    expect(report.database.state).toBe("unreadable");
    expect(report.problems).toEqual([
      {
        code: "database-unreadable",
        message: "Could not open the database: unable to open database file",
      },
    ]);
    expect(report.ok).toBe(false);
  });

  // The retry runs at once; a stop still tidying the database can fail it too. The process it names
  // is this test's own, a live one, as a stopping server's is until it has released the home.
  it("names the live server of a home whose open fails twice, so the report can be run again", () => {
    const home = makeHome();
    leaveCrashedWal(home);
    const runtimePath = NodePath.join(home, "userdata", "server-runtime.json");
    NodeFS.writeFileSync(runtimePath, JSON.stringify({ version: 1, pid: process.pid, port: 1 }));
    // The server stops between the WAL checks and the open: it deletes its -wal and -shm.
    afterCheck.path = `${dbPathOf(home)}-shm`;
    afterCheck.run = () => {
      NodeFS.rmSync(`${dbPathOf(home)}-wal`);
      NodeFS.rmSync(`${dbPathOf(home)}-shm`);
    };
    openFails.remaining = 2;

    const report = readForkReport(home);

    expect(report.problems).toEqual([
      {
        code: "database-unreadable",
        message: `Could not open the database: unable to open database file. A T3 server (process ${process.pid}) is using this home; if it was stopping, run the report again once it has stopped.`,
      },
    ]);
    // A record whose process has exited is no server: the message stays as it was.
    NodeFS.writeFileSync(runtimePath, JSON.stringify({ version: 1, pid: 2 ** 31 - 1, port: 1 }));
    leaveCrashedWal(home);
    afterCheck.path = `${dbPathOf(home)}-shm`;
    openFails.remaining = 2;
    expect(readForkReport(home).problems[0]!.message).toBe(
      "Could not open the database: unable to open database file",
    );
  });

  // A server running steadily keeps the same -wal and -shm open: a read that fails beside it fails
  // for the database's own reason, which running the report again would not change.
  it("does not ask for a run again beside a server whose WAL files stayed the same", async () => {
    const home = makeHome();
    await startWriter(home);
    const runtimePath = NodePath.join(home, "userdata", "server-runtime.json");
    NodeFS.writeFileSync(runtimePath, JSON.stringify({ version: 1, pid: process.pid, port: 1 }));
    openFails.remaining = 2;

    const report = readForkReport(home);

    expect(report.problems).toEqual([
      {
        code: "database-unreadable",
        message: "Could not open the database: unable to open database file",
      },
    ]);
    // Nor beside a home whose database has no WAL files at all.
    const plain = makeHome();
    NodeFS.writeFileSync(
      NodePath.join(plain, "userdata", "server-runtime.json"),
      JSON.stringify({ version: 1, pid: process.pid, port: 1 }),
    );
    openFails.remaining = 2;
    expect(readForkReport(plain).problems[0]!.message).toBe(
      "Could not open the database: unable to open database file",
    );
  });
});

describe("__fork-report on old, newer and empty state", () => {
  it.effect("reports a database from a newer build and exits 1, still writing nothing", () =>
    Effect.gen(function* () {
      const home = makeHome({
        migrations: [...migrationManifest, [BUILD_MIGRATION_HEAD + 1, "FromTheFuture"]],
      });
      const before = snapshot(home);

      const { report, exitCode } = yield* reportThroughCli(home);

      expect(snapshot(home)).toEqual(before);
      expect(exitCode).toBe(1);
      expect(report.ok).toBe(false);
      expect(report.database.unknownMigrations).toEqual([
        `${BUILD_MIGRATION_HEAD + 1}_FromTheFuture`,
      ]);
      expect(report.problems.map((problem: { code: string }) => problem.code)).toEqual([
        "database-newer",
      ]);
    }),
  );

  it("lists the migrations an older database still needs, and does not run them", () => {
    const home = makeHome({ migrations: migrationManifest.slice(0, -3) });
    const before = snapshot(home);

    const report = readForkReport(home);

    expect(snapshot(home)).toEqual(before);
    expect(report.database.pendingMigrations).toEqual(
      migrationManifest.slice(-3).map(([id]) => id),
    );
    expect(report.ok).toBe(true);
  });

  it.effect.each([
    ["a missing home", "no-such-home"],
    ["a file", "a-file"],
    ["a path through a file", NodePath.join("a-file", "sub")],
  ] as const)("reports %s as no home, creating nothing", ([, name]) =>
    Effect.gen(function* () {
      const parent = tempDir();
      NodeFS.writeFileSync(NodePath.join(parent, "a-file"), "x");
      const before = snapshot(parent);

      const { report, exitCode } = yield* reportThroughCli(NodePath.join(parent, name));

      expect(snapshot(parent)).toEqual(before);
      expect(exitCode).toBe(1);
      expect(report.home.exists).toBe(false);
      expect(report.database.state).toBe("missing");
      expect(report.settings.state).toBe("missing");
      expect(report.fork.featuresOn).toEqual([]);
      expect(report.problems.map((problem: { code: string }) => problem.code)).toEqual([
        "home-missing",
      ]);
      expect(report.ok).toBe(false);
    }),
  );

  it.each([
    ["a file", (userdata: string) => NodeFS.writeFileSync(userdata, "x")],
    [
      "a link to nothing",
      (userdata: string) => NodeFS.symlinkSync(NodePath.join(tempDir(), "gone"), userdata),
    ],
  ] as const)("reports a userdata that is %s as no home, not as a fresh one", (_, layOut) => {
    const home = tempDir();
    layOut(NodePath.join(home, "userdata"));
    const before = snapshot(home);

    const report = readForkReport(home);

    expect(snapshot(home)).toEqual(before);
    expect(report.home.exists).toBe(true);
    expect(report.database.state).toBe("missing");
    expect(report.settings.state).toBe("missing");
    expect(report.problems.map((problem) => problem.code)).toEqual(["home-missing"]);
    expect(report.problems[0]!.message).toContain("userdata");
    expect(report.ok).toBe(false);
  });

  it.each([
    ["nothing", () => {}, (home: string) => `No T3 home folder at ${home}.`],
    [
      "a link to nothing",
      (home: string) => NodeFS.symlinkSync(NodePath.join(NodePath.dirname(home), "gone"), home),
      (home: string) => `${home} is a link that leads nowhere; a start would fail.`,
    ],
  ] as const)("names a home path that holds %s, creating nothing", (_, layOut, message) => {
    const parent = tempDir();
    const home = NodePath.join(parent, "home");
    layOut(home);
    const before = snapshot(parent);

    const report = readForkReport(home);

    expect(snapshot(parent)).toEqual(before);
    expect(report.home.exists).toBe(false);
    expect(report.problems).toEqual([{ code: "home-missing", message: message(home) }]);
    expect(report.ok).toBe(false);
  });

  it.each([
    ["a home that is a link loop", (home: string) => NodeFS.symlinkSync(home, home), false],
    [
      "a userdata that is a link loop",
      (home: string) => {
        NodeFS.mkdirSync(home);
        NodeFS.symlinkSync("userdata", NodePath.join(home, "userdata"));
      },
      true,
    ],
  ] as const)("reports %s as no home instead of failing", (_, layOut, exists) => {
    const parent = tempDir();
    const home = NodePath.join(parent, "home");
    layOut(home);
    const before = snapshot(parent);

    const report = readForkReport(home);

    expect(snapshot(parent)).toEqual(before);
    expect(report.home.exists).toBe(exists);
    expect(report.problems[0]!.code).toBe("home-missing");
    expect(report.problems[0]!.message).toContain("ELOOP");
    expect(report.ok).toBe(false);
  });

  // Root bypasses folder permissions, so chmod cannot force EACCES.
  it.skipIf(process.getuid?.() === 0).each([
    ["a home it may not look into", "home", 0o600, true],
    ["a home inside a folder it may not enter", "parent", 0o600, false],
  ] as const)("reports %s as no home instead of failing", (_, lock, mode, exists) => {
    const parent = tempDir();
    const home = NodePath.join(parent, "home");
    NodeFS.mkdirSync(NodePath.join(home, "userdata"), { recursive: true });
    const locked = lock === "home" ? home : parent;
    const before = snapshot(parent);
    NodeFS.chmodSync(locked, mode);
    try {
      const report = readForkReport(home);

      expect(report.home.exists).toBe(exists);
      expect(report.problems[0]!.code).toBe("home-missing");
      expect(report.problems[0]!.message).toContain("EACCES");
      expect(report.ok).toBe(false);
    } finally {
      NodeFS.chmodSync(locked, 0o755);
    }
    expect(snapshot(parent)).toEqual(before);
  });

  it("names the V1 database a start would import into a home without statev2.sqlite", () => {
    const home = tempDir();
    NodeFS.mkdirSync(NodePath.join(home, "userdata"));
    const v1 = NodePath.join(home, "userdata", "state.sqlite");
    new NodeSqlite.DatabaseSync(v1).close();
    const before = snapshot(home);

    const report = readForkReport(home);

    expect(snapshot(home)).toEqual(before);
    expect(report.database.state).toBe("missing");
    expect(report.database.v1Import).toBe(v1);
    expect(report.ok).toBe(true);

    // A V1 path a start cannot copy from stops the start.
    NodeFS.rmSync(v1);
    NodeFS.mkdirSync(v1);
    const folder = readForkReport(home);
    expect(folder.database.v1Import).toBeNull();
    expect(folder.problems).toEqual([
      {
        code: "database-unreadable",
        message: `${v1} is not a file; a start would try to import it as the V1 database and fail.`,
      },
    ]);
  });

  // Root bypasses file permissions, so chmod cannot force EACCES.
  it.skipIf(process.getuid?.() === 0)(
    "reports a V1 database it may not read as one a start could not import, writing nothing",
    () => {
      const home = tempDir();
      NodeFS.mkdirSync(NodePath.join(home, "userdata"));
      const v1 = NodePath.join(home, "userdata", "state.sqlite");
      new NodeSqlite.DatabaseSync(v1).close();
      const before = snapshot(home);
      NodeFS.chmodSync(v1, 0o000);
      try {
        const report = readForkReport(home);

        expect(report.database.state).toBe("missing");
        expect(report.database.v1Import).toBeNull();
        expect(report.problems.map((problem) => problem.code)).toEqual(["database-unreadable"]);
        const { message } = report.problems[0]!;
        expect(message.startsWith(`${v1} cannot be read (EACCES`)).toBe(true);
        expect(
          message.endsWith("); a start would try to import it as the V1 database and fail."),
        ).toBe(true);
        expect(report.ok).toBe(false);
      } finally {
        NodeFS.chmodSync(v1, 0o644);
      }
      expect(snapshot(home)).toEqual(before);
    },
  );

  it("reads an empty home as no database and every fork feature off", () => {
    const home = tempDir();
    NodeFS.mkdirSync(NodePath.join(home, "userdata"));
    NodeFS.writeFileSync(dbPathOf(home), "");
    const before = snapshot(home);

    const report = readForkReport(home);

    expect(snapshot(home)).toEqual(before);
    expect(report.database.state).toBe("empty");
    expect(report.settings.state).toBe("missing");
    expect(report.settings.parseWarning).toBeNull();
    expect(report.fork.features).toEqual({
      agentSlots: "off",
      waitFor: "off",
      usagePacingGate: "off",
      automaticUpdates: "off",
      reviewRules: "off",
      handoff: "off",
      playtest: "off",
      modelPolicy: "off",
      activityProbe: "off",
      stopFinishedChildren: "off",
    });
    expect(report.ok).toBe(true);
  });
});

describe("__fork-report through a linked statev2.sqlite", () => {
  // SQLite keeps -wal and -shm beside the file a link resolves to, not beside the link.
  it("reads the crashed WAL beside the real file of an absolute link, writing nothing", () => {
    const real = makeHome();
    leaveCrashedWal(real);
    const home = homeWithDatabaseLink(dbPathOf(real));
    const before = [snapshot(real), snapshot(home)];

    const report = readForkReport(home);

    expect([snapshot(real), snapshot(home)]).toEqual(before);
    expect(report.database.path).toBe(dbPathOf(home));
    expect(report.database.openMode).toBe("readonly-shm");
    expect(report.database.counts?.threads).toBe(4);
    expect(report.ok).toBe(true);
  });

  it("follows a relative link to another link before looking for the WAL", () => {
    const real = makeHome();
    leaveCrashedWal(real);
    const home = homeWithDatabaseLink(NodePath.join("..", "hop.sqlite"));
    NodeFS.symlinkSync(dbPathOf(real), NodePath.join(home, "hop.sqlite"));
    const before = [snapshot(real), snapshot(home)];

    const report = readForkReport(home);

    expect([snapshot(real), snapshot(home)]).toEqual(before);
    expect(report.database.openMode).toBe("readonly-shm");
    expect(report.database.counts?.threads).toBe(4);
    expect(report.ok).toBe(true);
  });

  it("reads beside a running writer through a link, writing nothing", async () => {
    const real = makeHome();
    await startWriter(real);
    const home = homeWithDatabaseLink(dbPathOf(real));
    const before = [snapshot(real), snapshot(home)];

    const report = readForkReport(home);

    expect([snapshot(real), snapshot(home)]).toEqual(before);
    expect(report.database.openMode).toBe("readonly-shm");
    expect(report.database.counts?.threads).toBe(4);
    expect(report.ok).toBe(true);
  });

  it("reports a link that leads nowhere as an error, not as no database, and creates nothing", () => {
    const elsewhere = tempDir();
    const dangling = homeWithDatabaseLink(NodePath.join(elsewhere, "gone.sqlite"));
    const looping = homeWithDatabaseLink(NodePath.join("..", "loop.sqlite"));
    NodeFS.symlinkSync(dbPathOf(looping), NodePath.join(looping, "loop.sqlite"));

    for (const [home, wouldCreate] of [
      [dangling, true],
      [looping, false],
    ] as const) {
      const before = [snapshot(elsewhere), snapshot(home)];

      const report = readForkReport(home);

      expect([snapshot(elsewhere), snapshot(home)]).toEqual(before);
      expect(report.database.state).toBe("unreadable");
      expect(report.problems.map((problem) => problem.code)).toEqual(["database-unreadable"]);
      expect(report.problems[0]!.message).toMatch(
        /^statev2\.sqlite is a link that does not lead to a file \((ENOENT|ELOOP)\b/,
      );
      expect(report.problems[0]!.message.includes("would create")).toBe(wouldCreate);
      expect(report.ok).toBe(false);
    }
  });

  it("reports a folder at statev2.sqlite, or a link to one, as an error, not as no database", () => {
    const folder = tempDir();
    const linked = homeWithDatabaseLink(folder);
    const plain = tempDir();
    NodeFS.mkdirSync(dbPathOf(plain), { recursive: true });

    for (const home of [linked, plain]) {
      const before = snapshot(home);

      const report = readForkReport(home);

      expect(snapshot(home)).toEqual(before);
      expect(report.database.state).toBe("unreadable");
      expect(report.problems.map((problem) => problem.code)).toEqual(["database-unreadable"]);
      expect(report.problems[0]!.message).toContain("not a file");
      expect(report.ok).toBe(false);
    }
  });

  // Root bypasses directory permissions, so chmod cannot force EACCES.
  it.skipIf(process.getuid?.() === 0)(
    "names a link it may not follow as unreadable, without claiming a start would create a database",
    () => {
      const locked = tempDir();
      NodeFS.writeFileSync(NodePath.join(locked, "statev2.sqlite"), "");
      const home = homeWithDatabaseLink(NodePath.join(locked, "statev2.sqlite"));
      NodeFS.chmodSync(locked, 0o000);
      try {
        const report = readForkReport(home);

        expect(report.database.state).toBe("unreadable");
        expect(report.problems.map((problem) => problem.code)).toEqual(["database-unreadable"]);
        expect(report.problems[0]!.message).toMatch(
          /^statev2\.sqlite could not be followed \(EACCES/,
        );
        expect(report.problems[0]!.message).not.toContain("would create");
      } finally {
        NodeFS.chmodSync(locked, 0o755);
      }
    },
  );

  // Root bypasses directory permissions, so chmod cannot force EACCES.
  it.skipIf(process.getuid?.() === 0).each([
    ["read or enter", 0o000],
    ["enter", 0o600],
  ] as const)("names a userdata it may not %s as its own problem instead of failing", (_, mode) => {
    const home = makeHome();
    const userdata = NodePath.join(home, "userdata");
    NodeFS.chmodSync(userdata, mode);
    try {
      const report = readForkReport(home);

      expect(report.home.exists).toBe(true);
      expect(report.database.state).toBe("unreadable");
      expect(report.problems.map((problem) => problem.code)).toEqual([
        "home-missing",
        "database-unreadable",
        "settings-unreadable",
      ]);
      const { message } = report.problems[0]!;
      expect(message.startsWith(`${userdata} cannot be entered (EACCES`)).toBe(true);
      expect(message.endsWith("); a start would fail.")).toBe(true);
      expect(report.ok).toBe(false);
    } finally {
      NodeFS.chmodSync(userdata, 0o755);
    }
  });
});

describe("__fork-report on a home a start could not write", () => {
  // Root bypasses directory permissions, so chmod cannot force EACCES.
  it.skipIf(process.getuid?.() === 0)(
    "names a userdata it may enter but not write, still reading the database",
    () => {
      const home = makeHome();
      const userdata = NodePath.join(home, "userdata");
      const before = snapshot(home);
      NodeFS.chmodSync(userdata, 0o500);
      try {
        const report = readForkReport(home);

        expect(report.database.state).toBe("present");
        expect(report.problems.map((problem) => problem.code)).toEqual(["home-missing"]);
        const { message } = report.problems[0]!;
        expect(message.startsWith(`${userdata} cannot be written (EACCES`)).toBe(true);
        expect(message.endsWith("); a start would fail.")).toBe(true);
        expect(report.ok).toBe(false);
      } finally {
        NodeFS.chmodSync(userdata, 0o755);
      }
      expect(snapshot(home)).toEqual(before);
    },
  );

  // Root bypasses directory permissions, so chmod cannot force EACCES.
  it.skipIf(process.getuid?.() === 0)(
    "names a home without userdata that a start could not create it in",
    () => {
      const home = tempDir();
      NodeFS.chmodSync(home, 0o500);
      try {
        const report = readForkReport(home);

        expect(report.home.exists).toBe(true);
        expect(report.problems.map((problem) => problem.code)).toEqual(["home-missing"]);
        const { message } = report.problems[0]!;
        expect(message.startsWith(`${home} cannot be written (EACCES`)).toBe(true);
        expect(message.endsWith("); a start could not create userdata in it.")).toBe(true);
      } finally {
        NodeFS.chmodSync(home, 0o755);
      }
    },
  );

  // Root bypasses directory permissions, so chmod cannot force EACCES.
  it.skipIf(process.getuid?.() === 0).each([
    ["worktrees nor caches", [], "worktrees and caches"],
    ["caches", ["worktrees"], "caches"],
    ["worktrees", ["caches"], "worktrees"],
  ] as const)(
    "names a home with userdata but no %s that a start could not create them in",
    (_, present, missing) => {
      const home = makeHome();
      for (const name of present) NodeFS.mkdirSync(NodePath.join(home, name));
      const before = snapshot(home);
      NodeFS.chmodSync(home, 0o500);
      try {
        const report = readForkReport(home);

        expect(report.database.state).toBe("present");
        expect(report.problems.map((problem) => problem.code)).toEqual(["home-missing"]);
        const { message } = report.problems[0]!;
        expect(message.startsWith(`${home} cannot be written (EACCES`)).toBe(true);
        expect(message.endsWith(`); a start could not create ${missing} in it.`)).toBe(true);
        expect(report.ok).toBe(false);
      } finally {
        NodeFS.chmodSync(home, 0o755);
      }
      expect(snapshot(home)).toEqual(before);
    },
  );

  // Root bypasses directory permissions, so chmod cannot force EACCES.
  it.skipIf(process.getuid?.() === 0)(
    "reads a home a start already set up as ready even when the home itself is not writable",
    () => {
      const home = makeHome();
      NodeFS.mkdirSync(NodePath.join(home, "worktrees"));
      NodeFS.mkdirSync(NodePath.join(home, "caches"));
      NodeFS.chmodSync(home, 0o500);
      try {
        const report = readForkReport(home);

        expect(report.problems).toEqual([]);
        expect(report.ok).toBe(true);
      } finally {
        NodeFS.chmodSync(home, 0o755);
      }
    },
  );

  it.each([
    ["the home", (home: string) => home],
    ["its userdata", (home: string) => NodePath.join(home, "userdata")],
  ] as const)(
    "reads %s that a first start creates just after the stat as it is now, not as a race",
    (_, pathOf) => {
      const home = makeHome();
      statMissesOnce.path = pathOf(home);

      const report = readForkReport(home);

      expect(statMissesOnce.path).toBe("");
      expect(report.home.exists).toBe(true);
      expect(report.problems).toEqual([]);
      expect(report.ok).toBe(true);
    },
  );

  it.each([
    ["the home", (home: string) => home],
    ["its userdata", (home: string) => NodePath.join(home, "userdata")],
  ] as const)(
    "names the error of a lookup of %s that fails another way, not as a link that leads nowhere",
    (_, pathOf) => {
      const parent = tempDir();
      const home = NodePath.join(parent, "home");
      NodeFS.mkdirSync(home);
      const path = pathOf(home);
      // As in a race: the stat finds nothing, then the look at the path itself fails.
      lstatFails.path = path;

      const report = readForkReport(home);

      expect(report.problems.map((problem) => problem.code)).toEqual(["home-missing"]);
      expect(report.problems[0]!.message).toBe(
        `${path} could not be looked at (EACCES: permission denied, lstat '${path}'); a start would fail.`,
      );
    },
  );
});

describe("__fork-report settings", () => {
  const settingsReport = (settings: unknown) =>
    readForkReport(makeHome({ settings: JSON.stringify(settings) }));

  it("names each fork feature that is on, including a retired key the load renames", () => {
    const report = settingsReport({
      agentSlots: { enabled: true, maxPerMachine: 4 },
      usagePacingGate: "advise",
      autoUpdateServer: true,
      automaticUpdatesQuietCommand: "/secret/path quiet",
    });

    expect(report.fork.featuresOn).toEqual(["agentSlots", "usagePacingGate", "automaticUpdates"]);
    expect(report.fork.featuresWhere).toEqual({
      agentSlots: ["server"],
      usagePacingGate: ["server"],
      automaticUpdates: ["server"],
    });
    expect(report.fork.keysInFile).toEqual([
      "autoUpdateServer",
      "automaticUpdatesQuietCommand",
      "usagePacingGate",
      "agentSlots",
    ]);
    expect(report.fork.settings).toMatchObject({
      automaticUpdatesQuietCommandSet: true,
      agentSlots: { enabled: true, maxPerMachine: 4 },
    });
    // A command line is never printed.
    expect(JSON.stringify(report)).not.toContain("/secret/path");
  });

  it("names the activity probe when on, as a server switch", () => {
    const report = settingsReport({ activityProbe: true });
    expect(report.fork.features.activityProbe).toBe("on");
    expect(report.fork.featuresOn).toEqual(["activityProbe"]);
    expect(report.fork.featuresWhere).toEqual({ activityProbe: ["server"] });
    expect(report.fork.settings).toMatchObject({ activityProbe: true });
  });

  it("names review rules, handoff and playtest when on, also from one project's override", () => {
    const step = (name: string) => ({ argv: ["/usr/local/bin/playtest", name] });
    const playtest = {
      enabled: true,
      claim: step("claim"),
      renew: step("renew"),
      push: [{ name: "deploy", ...step("deploy") }],
      release: step("release"),
    };

    expect(
      settingsReport({
        reviewRules: { preset: "recommended" },
        handoff: { mode: "handoff" },
        playtest,
      }).fork.featuresOn,
    ).toEqual(["reviewRules", "handoff", "playtest"]);
    // One thread's own mode is enough, in the server's value or in a project's override.
    const threadOn = settingsReport({
      handoff: { mode: "off", threads: { "thread-1": "handoff", "thread-2": "compact" } },
      projectSettingsOverrides: {
        "project-1": { handoff: { mode: "off", threads: { "thread-3": "handoff" } } },
      },
    }).fork;
    expect(threadOn.featuresOn).toEqual(["handoff"]);
    // Each place is named, so a stale thread entry can be found and cleared.
    expect(threadOn.featuresWhere).toEqual({
      handoff: ["thread:thread-1", "project:project-1/thread:thread-3"],
    });
    // Off on the server, on in one project: the project's value replaces the server's there.
    const projectOn = settingsReport({
      projectSettingsOverrides: {
        "project-1": { reviewRules: { preset: "recommended" }, handoff: { mode: "handoff" } },
        "project-2": { playtest },
      },
    }).fork;
    expect(projectOn.featuresOn).toEqual(["reviewRules", "handoff", "playtest"]);
    expect(projectOn.featuresWhere).toEqual({
      reviewRules: ["project:project-1"],
      handoff: ["project:project-1"],
      playtest: ["project:project-2"],
    });
    // A project that turns them off does not hide the server's being on elsewhere.
    const serverOn = settingsReport({
      reviewRules: { preset: "recommended" },
      projectSettingsOverrides: { "project-1": { reviewRules: { preset: "off" } } },
    }).fork;
    expect(serverOn.featuresOn).toEqual(["reviewRules"]);
    expect(serverOn.featuresWhere).toEqual({ reviewRules: ["server"] });
    // A project's null clears its override: the project uses the server's value, on or off.
    for (const [server, featuresOn] of [
      [{ preset: "recommended" }, ["reviewRules", "handoff", "playtest"]],
      [{ preset: "off" }, []],
    ] as const) {
      const cleared = settingsReport({
        reviewRules: server,
        ...(featuresOn.length > 0 ? { handoff: { mode: "handoff" }, playtest } : {}),
        projectSettingsOverrides: {
          "project-1": { reviewRules: null, handoff: null, playtest: null },
        },
      }).fork;
      expect(cleared.featuresOn).toEqual(featuresOn);
      expect(Object.values(cleared.featuresWhere).flat()).toEqual(featuresOn.map(() => "server"));
    }
    // A playtest value with a key this build does not know reads as off, as the load does: the key
    // may be a newer build's limit, so this build must not run playtest without it.
    expect(settingsReport({ playtest: { ...playtest, laterKey: 1 } }).fork.featuresOn).toEqual([]);
  });

  it("names the model policy by its mode, and reads an invalid one as enforce, as the load does", () => {
    const row = { driver: "claudeAgent", model: "claude-sonnet-5-5" };
    const modeOf = (modelPolicy: unknown) => {
      const report = settingsReport({ modelPolicy });
      expect(report.settings.parseWarning).toBeNull();
      return [report.fork.features.modelPolicy, report.fork.featuresWhere.modelPolicy] as const;
    };

    expect(readForkReport(makeHome()).fork.features.modelPolicy).toBe("off");
    expect(modeOf({ mode: "off", allow: [row] })).toEqual(["off", undefined]);
    expect(modeOf({ mode: "advise", allow: [row] })).toEqual(["advise", ["server"]]);
    // Saved sparse: `{mode: "enforce", allow: []}` is stored as `{mode: "enforce"}`.
    expect(modeOf({ mode: "enforce" })).toEqual(["enforce", ["server"]]);
    // A typo fails closed: enforce with nothing allowed, and the rest of the file still loads.
    expect(modeOf({ mode: "advise", allow: [{ ...row, effort: ["high"] }] })).toEqual([
      "enforce",
      ["server"],
    ]);
    expect(settingsReport({ modelPolicy: { mode: "advise" } }).fork.featuresOn).toEqual([
      "modelPolicy",
    ]);
  });

  it("names the windows each account spends first; old settings burn every window (#969)", () => {
    const report = settingsReport({
      usagePacing: {
        main: { reservePercent: 2 },
        extra: { burnFirst: true, burnFirstWindows: ["weekly", "monthly"] },
        old: { burnFirst: true },
        listOnly: { burnFirstWindows: ["weekly"] },
      },
    });
    expect(report.fork.settings.usagePacingBurnFirstWindows).toEqual({
      extra: ["weekly", "monthly"],
      old: ["session", "weekly", "monthly", "other"],
    });
    expect(readForkReport(makeHome()).fork.settings.usagePacingBurnFirstWindows).toEqual({});
  });

  // The report lists its features by hand, which once missed three. Each fork setting
  // is either a counted feature or here, with why it switches nothing on by itself.
  const NOT_A_SWITCH: Readonly<Record<string, string>> = {
    automaticUpdatesQuietCommand: "a step of automaticUpdates, which is counted",
    usagePacing: "numbers per account; only usagePacingGate (counted) acts on them",
    usagePacingLowerEffort: "how usagePacingGate (counted) holds a task",
    waitChecks: "named commands; only waitFor (counted) runs them",
    headlessUsageRefreshInterval: "on by default in every fork build; reads usage, changes no work",
    mcpProfiles: "MCP profiles: nothing applies a profile yet",
    mcpProfile: "MCP profiles: nothing applies a profile yet",
    mcpSubagentProfile: "MCP profiles: nothing applies a profile yet",
    mcpThreadProfiles: "MCP profiles: nothing applies a profile yet",
    mcpCarriedProfiles: "only narrows: refuses tasks another machine carried a profile with",
    placement: "placement: settings only so far; nothing reads them yet (later placement rows)",
    placementNeeds:
      "placement: settings only so far; nothing reads them yet (later placement rows)",
    threadRoles: "calm sidebar: settings only so far; nothing reads them yet",
    assistant: "only offers Make assistant; the assistant is the threadRoles.assistant thread",
    lanes: "lanes: settings only so far; nothing reads them yet (later lane phases)",
  };

  it("counts every fork setting as a feature or names why it is not a switch", () => {
    const counted = Object.keys(readForkReport(makeHome()).fork.features);
    const keys = new Set([...FORK_SERVER_SETTING_KEYS, ...Object.keys(ForkProjectOverrideFields)]);

    expect([...keys].filter((key) => !counted.includes(key) && !(key in NOT_A_SWITCH))).toEqual([]);
    expect(
      Object.keys(NOT_A_SWITCH).filter((key) => !keys.has(key) || counted.includes(key)),
    ).toEqual([]);
    expect(counted.filter((key) => !keys.has(key))).toEqual([]);
  });

  it("reads each project's override of every counted feature a project can set", () => {
    const counted = Object.keys(readForkReport(makeHome()).fork.features);
    const projectKeys = new Set<string>([
      ...FORK_PROJECT_SCOPED_KEYS,
      ...Object.keys(ForkProjectOverrideFields),
    ]);

    expect([...FORK_REPORT_PROJECT_FEATURES].toSorted()).toEqual(
      counted.filter((key) => projectKeys.has(key)).toSorted(),
    );
  });

  it("reads a bad review rules, handoff or playtest value as off, as the load does", () => {
    const report = settingsReport({
      reviewRules: { preset: "everything" },
      handoff: { mode: "handoff", typo: 1 },
      playtest: { enabled: true },
      projectSettingsOverrides: {
        "project-1": { reviewRules: { preset: "recommended", unknownKey: true } },
      },
    });

    expect(report.fork.featuresOn).toEqual([]);
    expect(report.settings.parseWarning).toBeNull();
  });

  it("warns when a load would ignore the file, and reports what the load would use", () => {
    const report = settingsReport({ agentSlots: { enabled: "yes" } });

    expect(report.settings.parseWarning).not.toBeNull();
    expect(report.problems.map((problem) => problem.code)).toEqual(["settings-unreadable"]);
    expect(report.fork.featuresOn).toEqual([]);
    expect(report.settings.providerInstances).toEqual([]);

    // Every interval is then at its default anyway, so none is named, whether the decode or the
    // legacy provider keys make the load ignore the file.
    for (const file of [
      { agentSlots: { enabled: "yes" }, headlessUsageRefreshInterval: -5 },
      { providers: "codex", headlessUsageRefreshInterval: -5 },
    ]) {
      expect(settingsReport(file).settings).toMatchObject({ intervalsReadAsDefaults: [] });
    }
    expect(
      settingsReport({ providers: "codex", headlessUsageRefreshInterval: -5 }).settings
        .parseWarning,
    ).toBe("The legacy provider or source control keys do not decode.");
  });

  it("names each interval a load reads as its default, without failing the report", () => {
    const report = settingsReport({
      automaticGitFetchInterval: 600_000,
      headlessUsageRefreshInterval: -5,
      backgroundActivity: { profile: "custom", overrides: { hostPowerMonitorIdleInterval: null } },
    });

    // The load keeps the rest of the file, so this is a note, not a problem.
    expect(report.ok).toBe(true);
    expect(report.settings.parseWarning).toBeNull();
    expect(report.settings.intervalsReadAsDefaults).toEqual([
      "headlessUsageRefreshInterval (-5)",
      "backgroundActivity.overrides.hostPowerMonitorIdleInterval (null)",
    ]);
    expect(report.fork.settings.headlessUsageRefreshIntervalMs).toBe(
      Duration.toMillis(DEFAULT_SERVER_SETTINGS.headlessUsageRefreshInterval),
    );
    expect(settingsReport({ automaticGitFetchInterval: 600_000 }).settings).toMatchObject({
      intervalsReadAsDefaults: [],
    });
  });

  it("warns on an empty, blank or cut-off file, which a load also ignores", () => {
    for (const text of ["", "  \n", '{ "agentSlots": { "enabled": true ']) {
      const report = readForkReport(makeHome({ settings: text }));

      expect(report.settings.parseWarning).not.toBeNull();
      expect(report.problems.map((problem) => problem.code)).toEqual(["settings-unreadable"]);
      // As the load's fallback: a file that does not parse may hold a policy, so it fails closed.
      expect(report.fork.featuresOn).toEqual(["modelPolicy"]);
      expect(report.ok).toBe(false);
    }
  });

  it("lists the fork keys of a file with comments and a trailing comma, as a load reads it", () => {
    const report = readForkReport(
      makeHome({ settings: `{\n  // note\n  "agentSlots": { "enabled": true },\n}\n` }),
    );

    expect(report.settings.parseWarning).toBeNull();
    expect(report.fork.keysInFile).toEqual(["agentSlots"]);
    expect(report.fork.featuresOn).toEqual(["agentSlots"]);
  });

  it("reports a settings.json it cannot read instead of failing", () => {
    const home = makeHome();
    NodeFS.rmSync(settingsPathOf(home));
    NodeFS.mkdirSync(settingsPathOf(home));

    const report = readForkReport(home);

    expect(report.settings.parseWarning).toContain("could not be read");
    expect(report.problems.map((problem) => problem.code)).toEqual(["settings-unreadable"]);
    expect(report.ok).toBe(false);
  });
});

// The report must warn exactly when the server's own load ignores settings.json. A load that ignores
// the file turns `continueThreadsAfterServerUpdate` (default on) off; no case below sets that key.
it.layer(Layer.mergeAll(NodeServices.layer, NodeCryptoLayer.layer))(
  "__fork-report settings warning matches the server's load",
  (it) => {
    const loadedSettings = (home: string, policy?: ManagedPolicyDocument) =>
      Effect.gen(function* () {
        const service = yield* ServerSettingsModule.ServerSettingsService;
        return yield* service.getSettings;
      }).pipe(
        Effect.provide(
          ServerSettingsModule.layer.pipe(
            Layer.provide(ServerSecretStore.layer),
            Layer.provide(ManagedPolicy.layerTest(policy)),
            Layer.provideMerge(Layer.fresh(SqlitePersistence.layerMemory)),
            Layer.provideMerge(Layer.fresh(ServerConfig.layerTest(process.cwd(), home))),
          ),
        ),
      );

    /** Each case lays out userdata/settings.json in a fresh home (`home` already has userdata). */
    const cases: ReadonlyArray<readonly [string, (home: string) => void]> = [
      ["no file", () => {}],
      ["an empty file", (home) => NodeFS.writeFileSync(settingsPathOf(home), "")],
      ["a blank file", (home) => NodeFS.writeFileSync(settingsPathOf(home), " \n\t\n")],
      ["a cut-off file", (home) => NodeFS.writeFileSync(settingsPathOf(home), '{ "waitFor": {')],
      [
        "a wrong value",
        (home) => NodeFS.writeFileSync(settingsPathOf(home), '{ "agentSlots": { "enabled": 1 } }'),
      ],
      [
        "legacy provider keys the load refuses",
        (home) => NodeFS.writeFileSync(settingsPathOf(home), '{ "providers": 5 }'),
      ],
      [
        "comments and a trailing comma",
        (home) =>
          NodeFS.writeFileSync(settingsPathOf(home), '{ // c\n "waitFor": { "enabled": true },\n}'),
      ],
      [
        "a link to an empty file",
        (home) => {
          const target = NodePath.join(tempDir(), "settings.json");
          NodeFS.writeFileSync(target, "");
          NodeFS.symlinkSync(target, settingsPathOf(home));
        },
      ],
      [
        "a link to a valid file",
        (home) => {
          const target = NodePath.join(tempDir(), "settings.json");
          NodeFS.writeFileSync(target, '{ "waitFor": { "enabled": true } }');
          NodeFS.symlinkSync(target, settingsPathOf(home));
        },
      ],
      [
        "a link to nothing",
        (home) => NodeFS.symlinkSync(NodePath.join(tempDir(), "gone.json"), settingsPathOf(home)),
      ],
    ];

    it.effect.each(cases)("with %s", ([, layOut]) =>
      Effect.gen(function* () {
        const home = tempDir();
        NodeFS.mkdirSync(NodePath.join(home, "userdata"));
        layOut(home);

        const report = readForkReport(home);
        const loaded = yield* loadedSettings(home);

        const loadIgnoredFile = !loaded.continueThreadsAfterServerUpdate;
        expect(report.settings.parseWarning !== null).toBe(loadIgnoredFile);
        expect(report.fork.features.waitFor).toBe(loaded.waitFor.enabled ? "on" : "off");
        expect(report.fork.features.agentSlots).toBe(loaded.agentSlots.enabled ? "on" : "off");
        // Each fork feature where the load has it, the fallback's model policy included.
        expect(report.fork.featuresWhere).toEqual(
          Object.fromEntries(
            Object.entries(forkFeaturesWhere(loaded)).filter(([, places]) => places.length > 0),
          ),
        );
        expect(report.fork.settings.automaticUpdates).toBe(loaded.automaticUpdates);
      }),
    );

    it.effect("with a managed policy that switches features on", () =>
      Effect.gen(function* () {
        const document = {
          schemaVersion: 1,
          organization: "Example",
          settings: {
            // A default fills a key the file leaves out; a lock wins over the file's value.
            "waitFor.enabled": { value: true, locked: false },
            "agentSlots.enabled": { value: true, locked: true },
          },
        } as const;
        const home = tempDir();
        NodeFS.mkdirSync(NodePath.join(home, "userdata"));
        NodeFS.writeFileSync(settingsPathOf(home), '{ "agentSlots": { "enabled": false } }');
        const managed = managedOf(document.settings);

        const report = readForkReport(home, managed);
        const loaded = yield* loadedSettings(home, document);

        expect([loaded.waitFor.enabled, loaded.agentSlots.enabled]).toEqual([true, true]);
        expect(report.fork.featuresOn).toEqual(["agentSlots", "waitFor"]);
        // A default is the file's to change; a lock that changed the value is the policy's alone.
        expect(report.fork.featuresWhere).toEqual({ agentSlots: ["policy"], waitFor: ["server"] });
        expect(readForkReport(home, { unreadable: "bad" }).problems).toEqual([
          { code: "policy-unreadable", message: "The managed policy would stop a start: bad" },
        ]);

        // A lock equal to the file's value changes nothing: the file's own value is named.
        NodeFS.writeFileSync(settingsPathOf(home), '{ "agentSlots": { "enabled": true } }');
        expect(readForkReport(home, managed).fork.featuresWhere.agentSlots).toEqual(["server"]);

        // Through the real command, which reads the policy a start would.
        const policyPath = NodePath.join(tempDir(), "policy.json");
        NodeFS.writeFileSync(policyPath, JSON.stringify(document));
        NodeFS.writeFileSync(settingsPathOf(home), '{ "agentSlots": { "enabled": false } }');
        const viaCli = yield* reportThroughCli(home, policyPath);
        expect(viaCli.exitCode).toBe(0);
        expect(viaCli.report.fork.featuresWhere).toEqual({
          agentSlots: ["policy"],
          waitFor: ["server"],
        });
      }),
    );

    it.effect("with a lock that also reaches each project's override", () =>
      Effect.gen(function* () {
        const document = {
          schemaVersion: 1,
          organization: "Example",
          settings: { reviewRules: { value: { preset: "recommended" }, locked: true } },
        } as const;
        const home = tempDir();
        NodeFS.mkdirSync(NodePath.join(home, "userdata"));
        NodeFS.writeFileSync(
          settingsPathOf(home),
          JSON.stringify({
            projectSettingsOverrides: { "project-1": { reviewRules: { preset: "off" } } },
          }),
        );
        const managed = managedOf(document.settings);

        const loaded = yield* loadedSettings(home, document);
        const report = readForkReport(home, managed);

        expect(Object.values(loaded.projectSettingsOverrides)[0]?.reviewRules).toEqual({
          preset: "recommended",
        });
        // On at the server and in project-1 only through the lock: one `policy`, nothing to edit.
        expect(report.fork.featuresWhere).toEqual({ reviewRules: ["policy"] });
      }),
    );

    it.effect("with a policy that does not load: exit 1, only JSON on stdout", () =>
      Effect.gen(function* () {
        const home = tempDir();
        NodeFS.mkdirSync(NodePath.join(home, "userdata"));
        const policyPath = NodePath.join(tempDir(), "policy.json");
        NodeFS.writeFileSync(policyPath, "{ not json");
        const before = snapshot(home);

        const { report, exitCode } = yield* reportThroughCli(home, policyPath);

        expect(snapshot(home)).toEqual(before);
        expect(exitCode).toBe(1);
        expect(report.ok).toBe(false);
        expect(report.problems.map((problem: { code: string }) => problem.code)).toEqual([
          "policy-unreadable",
        ]);
      }),
    );
  },
);

// Contract: the go-live kit reads these keys. Changing one means bumping FORK_REPORT_VERSION and
// changing the kit in the same pull request.
describe("__fork-report contract", () => {
  it("keeps the keys the go-live kit reads", () => {
    const report = readForkReport(makeHome());
    const keys = (value: object) => Object.keys(value).toSorted();

    expect(FORK_REPORT_VERSION).toBe(1);
    expect(report.reportVersion).toBe(1);
    expect(keys(report)).toEqual(
      [
        "reportVersion",
        "version",
        "home",
        "database",
        "settings",
        "fork",
        "problems",
        "ok",
      ].toSorted(),
    );
    expect(keys(report.home)).toEqual(["baseDir", "exists"]);
    expect(keys(report.database)).toEqual(
      [
        "path",
        "state",
        "openMode",
        "buildMigrationHead",
        "appliedMigrations",
        "pendingMigrations",
        "unknownMigrations",
        "counts",
        "v1Import",
      ].toSorted(),
    );
    expect(keys(report.database.counts!)).toEqual(
      ["projects", "projectsArchived", "threads", "threadsArchived"].toSorted(),
    );
    expect(keys(report.settings)).toEqual(
      ["intervalsReadAsDefaults", "parseWarning", "path", "providerInstances", "state"].toSorted(),
    );
    expect(keys(report.fork)).toEqual([
      "features",
      "featuresOn",
      "featuresWhere",
      "keysInFile",
      "settings",
    ]);
    expect(keys(report.fork.features)).toEqual([
      "activityProbe",
      "agentSlots",
      "automaticUpdates",
      "handoff",
      "modelPolicy",
      "playtest",
      "reviewRules",
      "stopFinishedChildren",
      "usagePacingGate",
      "waitFor",
    ]);
    expect(keys(report.fork.settings)).toEqual(
      [
        "activityProbe",
        "agentSlots",
        "automaticUpdates",
        "automaticUpdatesQuietCommandSet",
        "headlessUsageRefreshIntervalMs",
        "stopFinishedChildren",
        "usagePacing",
        "usagePacingBurnFirstWindows",
        "usagePacingGate",
        "usagePacingLowerEffort",
        "waitCheckNames",
        "waitFor",
      ].toSorted(),
    );
    expect(report.database.buildMigrationHead).toBe(BUILD_MIGRATION_HEAD);
    // The whole report is plain JSON: it survives a round trip unchanged.
    expect(JSON.parse(formatForkReport(report))).toEqual(report);
  });
});

describe("__fork-report --activity", () => {
  const activityThroughCli = (home: string) =>
    Effect.gen(function* () {
      const before = process.exitCode;
      process.exitCode = 0;
      let exitCode: number | string | null | undefined;
      const lines = yield* runCli(["__fork-report", "--base-dir", home, "--activity"]).pipe(
        Effect.ensuring(
          Effect.sync(() => {
            exitCode = process.exitCode;
            process.exitCode = before;
          }),
        ),
      );
      return { lines, reading: JSON.parse(lines.join("\n")), exitCode };
    });

  /** A local server answering the probe path as a running T3 would, recorded in the home. */
  const serveProbe = (
    home: string,
    body: unknown,
    respond: NodeHttp.RequestListener = (request, response) => {
      response.writeHead(request.url === ACTIVITY_PROBE_PATH ? 200 : 404, {
        "content-type": "application/json",
      });
      response.end(JSON.stringify(body));
    },
  ) =>
    Effect.acquireRelease(
      Effect.promise(
        () =>
          new Promise<NodeHttp.Server>((resolve) => {
            const server = NodeHttp.createServer(respond);
            server.listen(0, "127.0.0.1", () => resolve(server));
          }),
      ),
      (server) =>
        Effect.promise(() => new Promise<void>((resolve) => server.close(() => resolve()))),
    ).pipe(
      Effect.tap((server) =>
        Effect.sync(() => {
          const address = server.address();
          const port = typeof address === "object" && address !== null ? address.port : 0;
          NodeFS.writeFileSync(
            NodePath.join(home, "userdata", "server-runtime.json"),
            JSON.stringify({
              version: 1,
              pid: process.pid,
              port,
              origin: `http://127.0.0.1:${port}`,
              startedAt: "2026-11-19T10:00:00.000Z",
            }),
          );
        }),
      ),
    );

  const answer = (quiet: boolean) => ({
    probeVersion: ACTIVITY_PROBE_VERSION,
    quiet,
    reason: quiet ? null : "1 running turn",
    state: "running",
    pid: process.pid,
    serverVersion: "0.0.0",
    activity: {
      activeRuns: quiet ? 0 : 1,
      queuedRuns: 0,
      pendingRequests: 0,
      backgroundTasks: 0,
      terminalCommands: 0,
    },
  });

  it.effect("prints one line and exits 1 for a stopped home, writing nothing", () =>
    Effect.gen(function* () {
      const home = makeHome();
      const before = snapshot(home);
      const { lines, reading, exitCode } = yield* activityThroughCli(home);
      expect(lines).toHaveLength(1);
      expect(reading).toMatchObject({ quiet: false, state: "stopped" });
      expect(exitCode).toBe(1);
      expect(snapshot(home)).toEqual(before);
    }),
  );

  it.effect("exits 0 only when the running server says it is quiet", () =>
    Effect.gen(function* () {
      const quietHome = makeHome();
      yield* serveProbe(quietHome, answer(true));
      const quiet = yield* activityThroughCli(quietHome);
      expect(quiet.reading).toMatchObject({ quiet: true, state: "quiet" });
      expect(quiet.exitCode).toBe(0);

      const busyHome = makeHome();
      yield* serveProbe(busyHome, answer(false));
      const busy = yield* activityThroughCli(busyHome);
      expect(busy.reading).toMatchObject({ quiet: false, state: "busy", reason: "1 running turn" });
      expect(busy.exitCode).toBe(1);
      // The reason comes first, so a hook that keeps the first 200 characters keeps it.
      expect(String(busy.lines[0])).toMatch(/^\{"quiet":false,"reason":"1 running turn"/);
    }).pipe(Effect.scoped),
  );

  // T3 sends the headers and then closes mid-body (for example while shutting down).
  it.effect("prints its one JSON line and exits 1 when the answer is cut off mid-body", () =>
    Effect.gen(function* () {
      const home = makeHome();
      yield* serveProbe(home, null, (_request, response) => {
        response.writeHead(200, { "content-type": "application/json", "content-length": "500" });
        response.write('{"probeVersion":1,', () => response.socket?.destroy());
      });
      const { lines, reading, exitCode } = yield* activityThroughCli(home);
      expect(lines).toHaveLength(1);
      expect(reading).toMatchObject({ quiet: false, state: "unreachable" });
      expect(exitCode).toBe(1);
    }).pipe(Effect.scoped),
  );
});
