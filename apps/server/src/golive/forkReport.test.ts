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
import * as NetService from "@t3tools/shared/Net";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as TestConsole from "effect/testing/TestConsole";
import { Command } from "effect/cli";

import { cli } from "../binCli.ts";
import { BUILD_MIGRATION_HEAD } from "../persistence/databaseMigrationHead.ts";
import { migrationManifest } from "../persistence/Migrations.ts";
import { FORK_REPORT_VERSION, formatForkReport, readForkReport } from "./forkReport.ts";

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

/** Every path under `dir` with its bytes' hash, size and mtime. */
function snapshot(dir: string): Record<string, string> {
  const out: Record<string, string> = {};
  const walk = (current: string) => {
    for (const entry of NodeFS.readdirSync(current, { withFileTypes: true })) {
      const full = NodePath.join(current, entry.name);
      const stat = NodeFS.statSync(full);
      const rel = NodePath.relative(dir, full);
      if (entry.isDirectory()) {
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
    const before = snapshot(home);

    const report = readForkReport(home);

    expect(snapshot(home)).toEqual(before);
    expect(report.database.openMode).toBe("readonly-shm");
    expect(report.database.counts?.threads).toBe(4);
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

  it("reports a missing home without creating it", () => {
    const home = NodePath.join(tempDir(), "no-such-home");

    const report = readForkReport(home);

    expect(NodeFS.existsSync(home)).toBe(false);
    expect(report.home.exists).toBe(false);
    expect(report.database.state).toBe("missing");
    expect(report.settings.state).toBe("missing");
    expect(report.fork.featuresOn).toEqual([]);
    expect(report.problems.map((problem) => problem.code)).toEqual(["home-missing"]);
    expect(report.ok).toBe(false);
  });

  it("reads an empty home as no database and every fork feature off", () => {
    const home = tempDir();
    NodeFS.mkdirSync(NodePath.join(home, "userdata"));
    NodeFS.writeFileSync(settingsPathOf(home), "");
    NodeFS.writeFileSync(dbPathOf(home), "");
    const before = snapshot(home);

    const report = readForkReport(home);

    expect(snapshot(home)).toEqual(before);
    expect(report.database.state).toBe("empty");
    expect(report.settings.state).toBe("empty");
    expect(report.fork.features).toEqual({
      agentSlots: "off",
      waitFor: "off",
      usagePacingGate: "off",
      automaticUpdates: "off",
    });
    expect(report.ok).toBe(true);
  });
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
});

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
    expect(keys(report.settings)).toEqual(["parseWarning", "path", "providerInstances", "state"]);
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
