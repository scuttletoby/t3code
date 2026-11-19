// @effect-diagnostics nodeBuiltinImport:off - a synchronous, read-only look at a T3 home; it must
// not start the server runtime, which would migrate, take the ownership lock and write files.
import * as NodeFS from "node:fs";
import * as NodePath from "node:path";
import * as NodeSqlite from "node:sqlite";
import * as NodeURL from "node:url";

import {
  effectiveReviewRules,
  FORK_PROJECT_SCOPED_KEYS,
  FORK_SERVER_SETTING_KEYS,
  type HandoffSettings,
  type PlaytestSettings,
  type ReviewRules,
  type ServerSettings,
} from "@t3tools/contracts";
import * as Cause from "effect/Cause";
import * as Duration from "effect/Duration";
import * as Exit from "effect/Exit";

import packageJson from "../../package.json" with { type: "json" };
import { BUILD_MIGRATION_HEAD } from "../persistence/databaseMigrationHead.ts";
import { migrationManifest, skippedV2Migrations } from "../persistence/Migrations.ts";
import { reconciledV2PreviewHistory } from "../persistence/reconcileV2PreviewMigration.ts";
import {
  decodeSettingsFileText,
  parseSettingsFileText,
  settingsFileLegacyKeysUnreadable,
} from "../serverSettings.ts";
import { readSettingsFile } from "../fork/settingsFileRead.ts";
import { type ManagedSettings, unmanagedSettings } from "../policy/managedSettings.ts";
import { isProcessAlive } from "../serverRuntimeState.ts";

/**
 * Fork (go-live kit): what a T3 home holds, read without writing a byte. It never migrates, never
 * takes the ownership lock and never creates a file or folder, so it can run on a stopped home, on
 * a copy, or beside a running server. Not while one stops: see `readDatabase` for the empty `-wal`.
 * Bump `reportVersion` when a key the kit reads changes.
 */
export const FORK_REPORT_VERSION = 1;

export type ForkReportProblemCode =
  | "home-missing"
  | "settings-unreadable"
  | "policy-unreadable"
  | "database-unreadable"
  | "database-needs-recovery"
  | "database-newer"
  | "database-incompatible";

export interface ForkReport {
  readonly reportVersion: number;
  readonly version: string;
  readonly home: { readonly baseDir: string; readonly exists: boolean };
  readonly database: {
    readonly path: string;
    readonly state: "missing" | "empty" | "present" | "unreadable";
    /** How it was opened: `immutable` when no WAL holds data, `readonly-shm` beside a WAL. */
    readonly openMode: "immutable" | "readonly-shm" | null;
    readonly buildMigrationHead: number;
    readonly appliedMigrations: ReadonlyArray<{ readonly id: number; readonly name: string }>;
    /** This build's migrations the database lacks: a start of this build would run them. */
    readonly pendingMigrations: ReadonlyArray<number>;
    /** Recorded migrations this build does not know: a newer build wrote them. */
    readonly unknownMigrations: ReadonlyArray<string>;
    /** With no statev2.sqlite: the V1 `state.sqlite` a start would import into it; else null. */
    readonly v1Import: string | null;
    readonly counts: {
      readonly projects: number | null;
      readonly projectsArchived: number | null;
      readonly threads: number | null;
      readonly threadsArchived: number | null;
    } | null;
  };
  readonly settings: {
    readonly path: string;
    readonly state: "missing" | "empty" | "present";
    /** Why a load would ignore the file and use defaults; null when it reads cleanly. */
    readonly parseWarning: string | null;
    /**
     * Each interval the file holds that no run could use, with the value: a load reads it as its
     * default and keeps the rest of the file, and the next save removes it from the file.
     */
    readonly intervalsReadAsDefaults: ReadonlyArray<string>;
    readonly providerInstances: ReadonlyArray<{
      readonly id: string;
      readonly driver: string;
      readonly enabled: boolean;
    }>;
  };
  readonly fork: {
    /** Each fork feature's switch as the server would read it; a missing key reads as off. */
    readonly features: {
      readonly agentSlots: "on" | "off";
      readonly waitFor: "on" | "off";
      readonly usagePacingGate: "off" | "advise" | "enforce";
      readonly automaticUpdates: "on" | "off";
      /** On when the server's or any project's rules are on (a project override can switch it on). */
      readonly reviewRules: "on" | "off";
      /** On when any mode (server, project or one thread's) is `handoff`. */
      readonly handoff: "on" | "off";
      /** On when the server's or any project's adapter is enabled. */
      readonly playtest: "on" | "off";
      /** The policy's mode; an invalid value reads as `enforce` (nothing allowed), as the load does. */
      readonly modelPolicy: "off" | "advise" | "enforce";
    };
    readonly featuresOn: ReadonlyArray<string>;
    /**
     * For each feature in `featuresOn`, where it is on: `server` (settings.json's own value, or a
     * default of the administrator's policy), `policy` (on only because the policy locks it),
     * `project:<id>` (that project's override), `thread:<id>` (one thread's handoff entry in the
     * server's value) or `project:<id>/thread:<id>` (one in a project's override).
     */
    readonly featuresWhere: Readonly<Record<string, ReadonlyArray<string>>>;
    /** Fork keys written in the file (whatever their value). */
    readonly keysInFile: ReadonlyArray<string>;
    /** Every fork setting, decoded. Commands are reduced to whether they are set. */
    readonly settings: Readonly<Record<string, unknown>>;
  };
  readonly problems: ReadonlyArray<{
    readonly code: ForkReportProblemCode;
    readonly message: string;
  }>;
  readonly ok: boolean;
}

/** Retired fork key names a load still renames, each after the name it became. */
const RETIRED_FORK_SETTINGS_KEYS: Readonly<Record<string, string>> = {
  automaticUpdates: "autoUpdateServer",
  automaticUpdatesQuietCommand: "autoUpdateQuietCommand",
};

/** Fork keys in `settings.json` (forkSettings.ts's list), with the retired names. */
const FORK_SETTINGS_KEYS = FORK_SERVER_SETTING_KEYS.flatMap((key) => {
  const retired = RETIRED_FORK_SETTINGS_KEYS[key];
  return retired === undefined ? [key] : [key, retired];
});

type Problem = ForkReport["problems"][number];

const errorText = (cause: unknown) => (cause instanceof Error ? cause.message : String(cause));
const errorCode = (cause: unknown) => (cause as NodeJS.ErrnoException | undefined)?.code;
/** A path's stat (`lstat` with `follow: false`); undefined when it is absent or runs through a file. */
function statOf(path: string, follow = true): NodeFS.Stats | undefined {
  try {
    return follow ? NodeFS.statSync(path) : NodeFS.lstatSync(path);
  } catch (cause) {
    if (errorCode(cause) === "ENOENT" || errorCode(cause) === "ENOTDIR") return undefined;
    throw cause;
  }
}
/** `statOf`, with any other error (such as `EACCES` or `ELOOP`) as its text instead of thrown. */
function lookAt(path: string, follow = true): NodeFS.Stats | undefined | string {
  try {
    return statOf(path, follow);
  } catch (cause) {
    return errorText(cause);
  }
}

/**
 * A read-only handle that writes nothing. A plain read-only open of a WAL database creates its
 * `-wal` and `-shm` files and writes read marks into `-shm`. With no WAL data the file alone is the
 * whole database (`immutable`); beside a WAL, `readonly_shm` reads it without writing the index.
 */
function openWithoutWriting(
  databasePath: string,
): { db: NodeSqlite.DatabaseSync; mode: "immutable" | "readonly-shm" } | Problem {
  // An empty journal is never hot; one with data may be, and only a writer may roll it back.
  if ((NodeFS.statSync(`${databasePath}-journal`, { throwIfNoEntry: false })?.size ?? 0) > 0) {
    return {
      code: "database-needs-recovery",
      message:
        "A rollback journal is present; only a writing open can recover it, so it was not read.",
    };
  }
  const walSize = NodeFS.statSync(`${databasePath}-wal`, { throwIfNoEntry: false })?.size;
  const hasShm = NodeFS.existsSync(`${databasePath}-shm`);
  if (walSize !== undefined && walSize > 0 && !hasShm) {
    return {
      code: "database-needs-recovery",
      message:
        "A WAL with data but no -shm index is present; reading it would write, so it was not read.",
    };
  }
  const mode = walSize === undefined || (walSize === 0 && !hasShm) ? "immutable" : "readonly-shm";
  const url = NodeURL.pathToFileURL(databasePath);
  url.search = mode === "immutable" ? "mode=ro&immutable=1" : "mode=ro&readonly_shm=1";
  try {
    return { db: new NodeSqlite.DatabaseSync(url, { readOnly: true }), mode };
  } catch (cause) {
    return {
      code: "database-unreadable",
      message: `Could not open the database: ${errorText(cause)}`,
    };
  }
}

const unreadDatabase = (path: string) =>
  ({
    path,
    openMode: null,
    buildMigrationHead: BUILD_MIGRATION_HEAD,
    appliedMigrations: [],
    pendingMigrations: [],
    unknownMigrations: [],
    v1Import: null,
    counts: null,
  }) as const;

function readDatabase(databasePath: string, problems: Array<Problem>): ForkReport["database"] {
  const empty = unreadDatabase(databasePath);
  if (statOf(databasePath, false) === undefined) {
    return { ...empty, state: "missing", v1Import: v1ToImport(databasePath, problems) };
  }
  // SQLite keeps -wal, -shm and -journal beside the file a link resolves to, so every check and the
  // open use the real path. A link that leads nowhere is not "no database": the data is elsewhere.
  let realPath: string;
  try {
    realPath = NodeFS.realpathSync(databasePath);
  } catch (cause) {
    const code = errorCode(cause);
    problems.push({
      code: "database-unreadable",
      message:
        code === "ENOENT"
          ? `statev2.sqlite is a link that does not lead to a file (${errorText(cause)}); a start would create a new, empty database where it points.`
          : code === "ELOOP"
            ? `statev2.sqlite is a link that does not lead to a file (${errorText(cause)}).`
            : `statev2.sqlite could not be followed (${errorText(cause)}).`,
    });
    return { ...empty, state: "unreadable" };
  }
  const real = NodeFS.statSync(realPath);
  if (!real.isFile()) {
    problems.push({
      code: "database-unreadable",
      message: "statev2.sqlite is not a file (a folder, or a link to one); a start cannot open it.",
    });
    return { ...empty, state: "unreadable" };
  }
  if (real.size === 0) return { ...empty, state: "empty" };

  // A stop between the WAL checks and the read deletes the WAL; SQLite creates an empty -wal (left:
  // removing it could unlink a starting server's WAL) and the read fails. A failed read runs once
  // more, at once; a stop still tidying can fail that too, so the problem names the live server.
  let read = readOnce(realPath, empty);
  if (read.database.state === "unreadable") read = readOnce(realPath, empty);
  const server = read.database.state === "unreadable" ? liveServerOf(databasePath) : null;
  problems.push(
    ...read.problems.map((problem) =>
      server === null
        ? problem
        : {
            ...problem,
            message: `${problem.message.replace(/\.$/, "")}. A T3 server (process ${server}) is using this home; if it was stopping, run the report again once it has stopped.`,
          },
    ),
  );
  return read.database;
}

/**
 * The process of a server still using the home of `databasePath`, or null: its `server-runtime.json`
 * names a live one. A server removes that record only after it has closed the database.
 */
function liveServerOf(databasePath: string): number | null {
  try {
    const record: unknown = JSON.parse(
      NodeFS.readFileSync(
        NodePath.join(NodePath.dirname(databasePath), "server-runtime.json"),
        "utf8",
      ),
    );
    const pid =
      typeof record === "object" && record !== null && "pid" in record ? record.pid : undefined;
    return typeof pid === "number" && isProcessAlive(pid) ? pid : null;
  } catch {
    return null;
  }
}

/**
 * As a start: with no statev2.sqlite, a `state.sqlite` beside it (followed through links) is the V1
 * database it copies in. One it cannot copy from stops the start, so it is a problem.
 */
function v1ToImport(databasePath: string, problems: Array<Problem>): string | null {
  const v1Path = NodePath.join(NodePath.dirname(databasePath), "state.sqlite");
  const v1 = lookAt(v1Path);
  if (v1 === undefined) return null;
  if (typeof v1 === "string") {
    problems.push({
      code: "database-unreadable",
      message: `${v1Path} could not be looked at (${v1}); a start would try to import it as the V1 database.`,
    });
    return null;
  }
  if (!v1.isFile()) {
    problems.push({
      code: "database-unreadable",
      message: `${v1Path} is not a file; a start would try to import it as the V1 database and fail.`,
    });
    return null;
  }
  return v1Path;
}

function readOnce(
  realPath: string,
  empty: ReturnType<typeof unreadDatabase>,
): { database: ForkReport["database"]; problems: Array<Problem> } {
  const problems: Array<Problem> = [];
  const opened = openWithoutWriting(realPath);
  if (!("db" in opened)) {
    return { database: { ...empty, state: "unreadable" }, problems: [opened] };
  }
  const { db, mode } = opened;
  try {
    const hasTable = (name: string) =>
      db.prepare("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?").get(name) !==
      undefined;
    const columns = (table: string) =>
      new Set(
        db
          .prepare("SELECT name FROM pragma_table_info(?)")
          .all(table)
          .map((row) => String(row.name)),
      );
    // Each count is null when its table or column is absent (an older or newer schema).
    const count = (table: string, where: string, needs: ReadonlyArray<string>) => {
      if (!hasTable(table)) return null;
      const have = columns(table);
      if (!needs.every((column) => have.has(column))) return null;
      return Number(db.prepare(`SELECT COUNT(*) AS n FROM ${table} WHERE ${where}`).get()?.n);
    };

    const applied = hasTable("effect_sql_migrations")
      ? db
          .prepare("SELECT migration_id, name FROM effect_sql_migrations ORDER BY migration_id")
          .all()
          .map((row) => ({ id: Number(row.migration_id), name: String(row.name) }))
      : [];
    const appliedIds = new Set(applied.map(({ id }) => id));
    const unknownMigrations = applied
      .filter(({ id }) => id > BUILD_MIGRATION_HEAD)
      .map(({ id, name }) => `${id}_${name}`);
    if (unknownMigrations.length > 0) {
      problems.push({
        code: "database-newer",
        message: `A newer build migrated this database (${unknownMigrations.join(", ")}); t3@${packageJson.version} must not run on it.`,
      });
    } else {
      // The same history check as the service preflight.
      const reconciled = reconciledV2PreviewHistory(applied);
      const incompatible =
        reconciled === null
          ? ["an unexpected V2 preview history"]
          : skippedV2Migrations(reconciled);
      if (incompatible.length > 0) {
        problems.push({
          code: "database-incompatible",
          message: `Incompatible migration history: ${incompatible.join(", ")}.`,
        });
      }
    }

    const threads = "orchestration_v2_projection_threads";
    const database: ForkReport["database"] = {
      ...empty,
      state: "present",
      openMode: mode,
      appliedMigrations: applied,
      pendingMigrations: migrationManifest.map(([id]) => id).filter((id) => !appliedIds.has(id)),
      unknownMigrations,
      counts: {
        projects: count("projection_projects", "deleted_at IS NULL", ["deleted_at"]),
        projectsArchived: count(
          "projection_projects",
          "deleted_at IS NULL AND archived_at IS NOT NULL",
          ["deleted_at", "archived_at"],
        ),
        threads: count(threads, "deleted_at IS NULL", ["deleted_at"]),
        threadsArchived: count(threads, "deleted_at IS NULL AND archived_at IS NOT NULL", [
          "deleted_at",
          "archived_at",
        ]),
      },
    };
    return { database, problems };
  } catch (cause) {
    return {
      database: { ...empty, state: "unreadable", openMode: mode },
      problems: [
        {
          code: "database-unreadable",
          message: `Could not read the database: ${errorText(cause)}`,
        },
      ],
    };
  } finally {
    try {
      db.close();
    } catch {
      // A read-only handle has nothing to flush.
    }
  }
}

function readSettings(
  settingsPath: string,
  problems: Array<Problem>,
  managed: ManagedSettings,
): { settings: ForkReport["settings"]; fork: ForkReport["fork"] } {
  // As the load: a missing file (or a link to nothing) means every default, without a warning; a
  // file that is there, even empty, goes through the load's own decode.
  let raw: string | undefined;
  let readError: string | null = null;
  try {
    if (statOf(settingsPath) !== undefined) {
      raw = NodeFS.readFileSync(settingsPath, "utf8");
    }
  } catch (cause) {
    readError = errorText(cause);
  }
  const state =
    raw === undefined && readError === null
      ? "missing"
      : raw !== undefined && raw.trim() === ""
        ? "empty"
        : "present";
  const decoded = raw === undefined ? undefined : decodeSettingsFileText(raw, managed.withDefaults);
  let parseWarning: string | null = null;
  if (readError !== null) {
    parseWarning = `settings.json could not be read: ${readError}`;
  } else if (decoded !== undefined && Exit.isFailure(decoded)) {
    parseWarning = Cause.pretty(decoded.cause);
  } else if (raw !== undefined && settingsFileLegacyKeysUnreadable(raw)) {
    parseWarning = "The legacy provider or source control keys do not decode.";
  }
  if (parseWarning !== null) {
    problems.push({
      code: "settings-unreadable",
      message:
        readError === null
          ? `A load would ignore settings.json and use defaults: ${parseWarning}`
          : parseWarning,
    });
  }
  // As the load: the policy's defaults (with updates off when the file does not decode, as it may
  // hold an opt-out), then its locks laid over whatever was read.
  const unlocked =
    parseWarning !== null
      ? { ...managed.defaults, automaticUpdates: false }
      : decoded !== undefined && Exit.isSuccess(decoded)
        ? decoded.value
        : managed.defaults;
  const settings = managed.applyLocks(unlocked);

  // The keys come from the load's lenient parse, so comments and trailing commas hide none.
  const parsed = raw === undefined ? undefined : parseSettingsFileText(raw);
  const keysInFile =
    parsed !== undefined &&
    Exit.isSuccess(parsed) &&
    typeof parsed.value === "object" &&
    parsed.value !== null &&
    !Array.isArray(parsed.value)
      ? FORK_SETTINGS_KEYS.filter((key) => Object.hasOwn(parsed.value as object, key))
      : [];
  // When the load ignores the file, every interval is at its default anyway.
  const intervalsReadAsDefaults =
    parsed !== undefined && Exit.isSuccess(parsed) && parseWarning === null
      ? readSettingsFile(parsed.value).dropped
      : [];

  // Where each place is on as loaded; a place on only because of a lock (the server's value or a
  // project's, which locks also reach) is `policy`: no edit of settings.json switches it off.
  const loaded = whereEachFeature(settings);
  const own = whereEachFeature(unlocked);
  const where = { ...loaded };
  for (const key of Object.keys(loaded) as Array<ForkReportFeature>) {
    where[key] = [
      ...new Set(loaded[key].map((place) => (own[key].includes(place) ? place : "policy"))),
    ];
  }
  const onOff = (key: ForkReportFeature) => (where[key].length > 0 ? "on" : "off");
  const features = {
    agentSlots: onOff("agentSlots"),
    waitFor: onOff("waitFor"),
    usagePacingGate: settings.usagePacingGate,
    automaticUpdates: onOff("automaticUpdates"),
    reviewRules: onOff("reviewRules"),
    handoff: onOff("handoff"),
    playtest: onOff("playtest"),
    modelPolicy: settings.modelPolicy.mode,
  } as const;
  return {
    settings: {
      path: settingsPath,
      state,
      parseWarning,
      intervalsReadAsDefaults,
      providerInstances: Object.entries(settings.providerInstances).map(([id, instance]) => ({
        id,
        driver: String(instance.driver),
        enabled: instance.enabled !== false,
      })),
    },
    fork: {
      features,
      featuresOn: Object.entries(features)
        .filter(([, value]) => value !== "off")
        .map(([key]) => key),
      featuresWhere: Object.fromEntries(
        Object.entries(where).filter(([, places]) => places.length > 0),
      ),
      keysInFile,
      settings: {
        automaticUpdates: settings.automaticUpdates,
        automaticUpdatesQuietCommandSet: settings.automaticUpdatesQuietCommand !== "",
        usagePacing: settings.usagePacing,
        usagePacingGate: settings.usagePacingGate,
        usagePacingLowerEffort: settings.usagePacingLowerEffort,
        waitFor: settings.waitFor,
        waitCheckNames: Object.keys(settings.waitChecks).toSorted(),
        agentSlots: settings.agentSlots,
        headlessUsageRefreshIntervalMs: Duration.toMillis(settings.headlessUsageRefreshInterval),
      },
    },
  };
}

type ForkReportFeature = keyof ForkReport["fork"]["features"];
type ForkProjectScopedKey = (typeof FORK_PROJECT_SCOPED_KEYS)[number];

/**
 * The counted features a project's override can switch on: the report reads each one's override in
 * every project. A test holds it to every counted feature that is also a project-scoped setting.
 */
export const FORK_REPORT_PROJECT_FEATURES = [
  "reviewRules",
  "handoff",
  "playtest",
] as const satisfies ReadonlyArray<ForkReportFeature & ForkProjectScopedKey>;
type ForkReportProjectFeature = (typeof FORK_REPORT_PROJECT_FEATURES)[number];

/** Where each counted feature is on in `settings`: `server`, `project:<id>` or a thread's entry. */
function whereEachFeature(
  settings: ServerSettings,
): Record<ForkReportFeature, ReadonlyArray<string>> {
  // A project's override replaces the server's value for that project, so it can switch a feature
  // on while the server's is off. A bad value already reads as off (the settings decode).
  const overrides = <K extends ForkReportProjectFeature>(key: K) =>
    Object.entries(settings.projectSettingsOverrides).map(
      ([projectId, project]) => [projectId, project[key]] as const,
    );
  const server = (on: boolean) => (on ? ["server"] : []);
  const perProject = {
    reviewRules: whereOn(settings.reviewRules, overrides("reviewRules"), reviewRulesOn),
    handoff: whereOn(settings.handoff, overrides("handoff"), handoffWhere),
    playtest: whereOn(settings.playtest, overrides("playtest"), playtestOn),
  } satisfies Record<ForkReportProjectFeature, ReadonlyArray<string>>;
  return {
    agentSlots: server(settings.agentSlots.enabled),
    waitFor: server(settings.waitFor.enabled),
    usagePacingGate: server(settings.usagePacingGate !== "off"),
    automaticUpdates: server(settings.automaticUpdates),
    ...perProject,
    modelPolicy: server(settings.modelPolicy.mode !== "off"),
  };
}

/** Where a value switches its feature on, below `at` (`server` or `project:<id>`). */
type WhereOn<T> = (value: T, at: string) => ReadonlyArray<string>;
const when =
  <T>(isOn: (value: T) => boolean): WhereOn<T> =>
  (value, at) =>
    isOn(value) ? [at] : [];
const reviewRulesOn = when((value: ReviewRules) => effectiveReviewRules(value).preset !== "off");
const playtestOn = when((value: PlaytestSettings) => value.enabled);
/** The value's own mode, and each thread whose entry is `handoff` (it wins over the mode). */
const handoffWhere: WhereOn<HandoffSettings> = (value, at) => [
  ...(value.mode === "handoff" ? [at] : []),
  ...Object.entries(value.threads)
    .filter(([, mode]) => mode === "handoff")
    .map(([threadId]) => `${at === "server" ? "" : `${at}/`}thread:${threadId}`),
];

/** Where the server's value and each project's (not set or null: the server's) is on. */
function whereOn<T>(
  server: T,
  projects: ReadonlyArray<readonly [string, T | null | undefined]>,
  where: WhereOn<T>,
): ReadonlyArray<string> {
  return [
    ...where(server, "server"),
    ...projects.flatMap(([projectId, value]) =>
      value == null ? [] : where(value, `project:${projectId}`),
    ),
  ];
}

/** The report as the command prints it: indented JSON. */
export const formatForkReport = (report: ForkReport): string => JSON.stringify(report, null, 2);

/** Why a start could not use this home, or null; a stat error is reported, never thrown. */
function whyNoHome(
  home: string,
  homeStat: ReturnType<typeof lookAt>,
  stateStat: ReturnType<typeof lookAt> | null,
  stateDir: string,
): string | null {
  if (homeStat === undefined) return notThere(home, `No T3 home folder at ${home}.`);
  if (typeof homeStat === "string") return `${home} could not be looked at (${homeStat}).`;
  if (stateStat === null) return `${home} is not a folder.`;
  if (typeof stateStat === "string") {
    return `${stateDir} could not be looked at (${stateStat}); a start would fail.`;
  }
  if (stateStat === undefined) {
    // A start creates userdata, so the home must take a new folder.
    return (
      notThere(stateDir, null) ??
      cannot(home, NodeFS.constants.W_OK, "be written", "a start could not create userdata in it")
    );
  }
  if (!stateStat.isDirectory()) {
    return `${stateDir} is not a folder, so this home holds no T3 state; a start would fail.`;
  }
  // A folder it may not enter hides the database and settings.json; name the folder, not only them.
  // One it may enter but not write fails a start, which writes its lock, logs and database there.
  return (
    cannot(stateDir, NodeFS.constants.X_OK, "be entered", "a start would fail") ??
    cannot(stateDir, NodeFS.constants.W_OK, "be written", "a start would fail")
  );
}

/** Why `path`, which a stat did not find, is not there: `missing` when nothing is, else why. */
function notThere(path: string, missing: string | null): string | null {
  const itself = lookAt(path, false);
  if (itself === undefined) return missing;
  if (typeof itself === "string") {
    return `${path} could not be looked at (${itself}); a start would fail.`;
  }
  return itself.isSymbolicLink()
    ? `${path} is a link that leads nowhere; a start would fail.`
    : `${path} appeared while the report looked at it; run the report again.`;
}

/** Why this process may not use `path` in the `mode` way (an access check, which writes nothing). */
function cannot(path: string, mode: number, what: string, then: string): string | null {
  try {
    NodeFS.accessSync(path, mode);
    return null;
  } catch (cause) {
    return `${path} cannot ${what} (${errorText(cause)}); ${then}.`;
  }
}

/**
 * The report for the T3 home `baseDir` (an explicit `--base-dir`: state lives under `userdata`).
 * `policy` is this machine's managed policy as the server loads it (defaults filled in, locks laid
 * over; its provider limits change no fork feature and are left out), or why it could not be read:
 * a start would then stop.
 */
export function readForkReport(
  baseDir: string,
  policy: ManagedSettings | { readonly unreadable: string } = unmanagedSettings,
  version: string = packageJson.version,
): ForkReport {
  const home = NodePath.resolve(baseDir);
  const stateDir = NodePath.join(home, "userdata");
  const problems: Array<Problem> = [];
  const homeStat = lookAt(home);
  const exists = typeof homeStat === "object" && homeStat.isDirectory();
  const homeProblem = whyNoHome(home, homeStat, exists ? lookAt(stateDir) : null, stateDir);
  if (homeProblem !== null) problems.push({ code: "home-missing", message: homeProblem });
  const databasePath = NodePath.join(stateDir, "statev2.sqlite");
  let database: ForkReport["database"];
  try {
    database = readDatabase(databasePath, problems);
  } catch (cause) {
    problems.push({
      code: "database-unreadable",
      message: `Could not look at the database: ${errorText(cause)}`,
    });
    database = { ...unreadDatabase(databasePath), state: "unreadable" };
  }
  let managed: ManagedSettings = unmanagedSettings;
  if ("unreadable" in policy) {
    problems.push({
      code: "policy-unreadable",
      message: `The managed policy would stop a start: ${policy.unreadable}`,
    });
  } else {
    managed = policy;
  }
  const { settings, fork } = readSettings(
    NodePath.join(stateDir, "settings.json"),
    problems,
    managed,
  );
  return {
    reportVersion: FORK_REPORT_VERSION,
    version,
    home: { baseDir: home, exists },
    database,
    settings,
    fork,
    problems,
    ok: problems.length === 0,
  };
}
