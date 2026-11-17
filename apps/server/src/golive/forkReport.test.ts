// @effect-diagnostics nodeBuiltinImport:off - the tests build real SQLite homes and child processes.
import * as NodeChildProcess from "node:child_process";
import * as NodeCrypto from "node:crypto";
import * as NodeFS from "node:fs";
import * as NodeOS from "node:os";
import * as NodePath from "node:path";
import * as NodeSqlite from "node:sqlite";

import * as NodeCryptoLayer from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { afterEach, describe, expect, it } from "@effect/vitest";
import { DEFAULT_SERVER_SETTINGS } from "@t3tools/contracts";
import * as NetService from "@t3tools/shared/Net";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as TestConsole from "effect/testing/TestConsole";
import { Command } from "effect/cli";
import { vi } from "vite-plus/test";

import { cli } from "../binCli.ts";
import * as ServerSecretStore from "../auth/ServerSecretStore.ts";
import * as ServerConfig from "../config.ts";
import { BUILD_MIGRATION_HEAD } from "../persistence/databaseMigrationHead.ts";
import { migrationManifest } from "../persistence/Migrations.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ServerSettingsModule from "../serverSettings.ts";
import { FORK_REPORT_VERSION, formatForkReport, readForkReport } from "./forkReport.ts";

/** Runs once, right after the report's last WAL check reads the path it names (the -shm). */
const afterCheck = vi.hoisted(() => ({ path: "", run: () => {} }));
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
  return { ...actual, existsSync };
});

const homes: Array<string> = [];
const children: Array<NodeChildProcess.ChildProcess> = [];
afterEach(() => {
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

const runCli = (args: ReadonlyArray<string>) =>
  Command.runWith(cli, { version: "0.0.0" })(args).pipe(
    Effect.andThen(TestConsole.logLines),
    Effect.provide(
      Layer.mergeAll(
        NodeServices.layer,
        NodeCryptoLayer.layer,
        NetService.layer,
        TestConsole.layer,
      ),
    ),
  );

/** Runs `__fork-report` through the real CLI, keeping the exit code it sets out of the test run. */
const reportThroughCli = (home: string) =>
  Effect.gen(function* () {
    const before = process.exitCode;
    process.exitCode = 0;
    let exitCode: number | string | null | undefined;
    const lines = yield* runCli(["__fork-report", "--base-dir", home]).pipe(
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
      expect(report.problems[0]!.message).toContain("link");
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
        expect(report.problems[0]!.message).toContain("EACCES");
        expect(report.problems[0]!.message).not.toContain("would create");
      } finally {
        NodeFS.chmodSync(locked, 0o755);
      }
    },
  );

  // Root bypasses directory permissions, so chmod cannot force EACCES.
  it.skipIf(process.getuid?.() === 0)(
    "reports a userdata it may not look into instead of failing",
    () => {
      const home = makeHome();
      const userdata = NodePath.join(home, "userdata");
      NodeFS.chmodSync(userdata, 0o000);
      try {
        const report = readForkReport(home);

        expect(report.database.state).toBe("unreadable");
        expect(report.problems.map((problem) => problem.code)).toEqual([
          "database-unreadable",
          "settings-unreadable",
        ]);
        expect(report.ok).toBe(false);
      } finally {
        NodeFS.chmodSync(userdata, 0o755);
      }
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

  it("warns when a load would ignore the file, and reports what the load would use", () => {
    const report = settingsReport({ agentSlots: { enabled: "yes" } });

    expect(report.settings.parseWarning).not.toBeNull();
    expect(report.problems.map((problem) => problem.code)).toEqual(["settings-unreadable"]);
    expect(report.fork.featuresOn).toEqual([]);
    expect(report.settings.providerInstances).toEqual([]);
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
      expect(report.fork.featuresOn).toEqual([]);
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
    const loadedSettings = (home: string) =>
      Effect.gen(function* () {
        const service = yield* ServerSettingsModule.ServerSettingsService;
        return yield* service.getSettings;
      }).pipe(
        Effect.provide(
          ServerSettingsModule.layer.pipe(
            Layer.provide(ServerSecretStore.layer),
            Layer.provide(ManagedPolicy.layerTest()),
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
      ].toSorted(),
    );
    expect(keys(report.database.counts!)).toEqual(
      ["projects", "projectsArchived", "threads", "threadsArchived"].toSorted(),
    );
    expect(keys(report.settings)).toEqual(
      ["intervalsReadAsDefaults", "parseWarning", "path", "providerInstances", "state"].toSorted(),
    );
    expect(keys(report.fork)).toEqual(["features", "featuresOn", "keysInFile", "settings"]);
    expect(keys(report.fork.features)).toEqual([
      "agentSlots",
      "automaticUpdates",
      "usagePacingGate",
      "waitFor",
    ]);
    expect(keys(report.fork.settings)).toEqual(
      [
        "agentSlots",
        "automaticUpdates",
        "automaticUpdatesQuietCommandSet",
        "headlessUsageRefreshIntervalMs",
        "usagePacing",
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
