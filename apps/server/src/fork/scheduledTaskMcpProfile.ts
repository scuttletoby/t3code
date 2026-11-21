/**
 * Fork: who saved each scheduled task (the owner, or a thread and its MCP profile), in a JSON file
 * in the state folder, not a column (the fork changes no schema). A run that starts a new thread is
 * refused when that thread would get more than the recorded profile.
 */
// @effect-diagnostics-next-line nodeBuiltinImport:off -- The digest must be deterministic, not random bytes.
import * as NodeCrypto from "node:crypto";

import {
  type ProjectId,
  type ScheduledTask,
  ScheduledTaskError,
  ThreadId,
} from "@t3tools/contracts";
import { writeFileStringAtomically } from "@t3tools/shared/atomicWrite";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as Semaphore from "effect/Semaphore";
import * as SqlClient from "effect/sql/SqlClient";

import * as ServerConfig from "../config.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import type * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import { type LineageThread, makeThreadMcpProfileReads } from "./claudeMcpProfile.ts";
import {
  carryMcpProfile,
  isEveryMcpTool,
  isWithinMcpProfile,
  type McpProfileCarried,
  type McpProfileResolved,
  readCarried,
} from "./mcpProfile.ts";

/** The record's file name, in the server's state folder. */
const SCHEDULED_TASK_MCP_PROFILES_FILE = "scheduled-task-mcp-profiles.json";

const VERSION = 1;

/** Who saved a task: `profile` is `null` for the owner (the app, or a client with no thread). */
export interface SavedBy {
  /** {@link savedDigest} of the row the save wrote. */
  readonly digest: string;
  readonly threadId: ThreadId | null;
  readonly profile: McpProfileCarried | null;
  readonly recordedAt: string;
}

const ENTRY_FIELDS: ReadonlySet<string> = new Set(["digest", "threadId", "profile", "recordedAt"]);

/** What a run that starts a new thread does with: its prompt, in its project. */
const savedDigest = (task: Pick<ScheduledTask, "prompt" | "projectId">): string =>
  NodeCrypto.createHash("sha256")
    .update(JSON.stringify([task.prompt, task.projectId]))
    .digest("hex");

type Loaded =
  | { readonly ok: true; readonly tasks: ReadonlyMap<string, SavedBy> }
  | { readonly ok: false; readonly issue: string };

const isRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** The file's text, checked strictly: a field this version does not know refuses, never dropped. */
function decodeSavedBy(text: string): Loaded {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return { ok: false, issue: "it is not JSON" };
  }
  if (!isRecord(value)) return { ok: false, issue: "it is not an object" };
  if (value.version !== VERSION) {
    return { ok: false, issue: `its version is not ${VERSION}` };
  }
  const extra = Object.keys(value).filter((key) => key !== "version" && key !== "tasks");
  if (extra.length > 0) {
    return { ok: false, issue: `it has fields this version does not know (${extra.join(", ")})` };
  }
  if (!isRecord(value.tasks)) return { ok: false, issue: "`tasks` is not an object" };
  const tasks = new Map<string, SavedBy>();
  for (const [taskId, entry] of Object.entries(value.tasks)) {
    const bad = (issue: string): Loaded => ({ ok: false, issue: `task ${taskId}: ${issue}` });
    if (!isRecord(entry)) return bad("its entry is not an object");
    const unknown = Object.keys(entry).filter((key) => !ENTRY_FIELDS.has(key));
    if (unknown.length > 0) {
      return bad(`its entry has fields this version does not know (${unknown.join(", ")})`);
    }
    if (typeof entry.digest !== "string" || entry.digest === "") return bad("it has no digest");
    if (typeof entry.recordedAt !== "string") return bad("it has no recordedAt");
    if (entry.threadId !== null && typeof entry.threadId !== "string") {
      return bad("its threadId is not a string or null");
    }
    let profile: McpProfileCarried | null = null;
    if (entry.profile !== null) {
      const read = readCarried(entry.profile);
      if (!read.ok) return bad(`its profile cannot be read: ${read.issue}`);
      profile = { name: read.name, ...read.profile };
    }
    tasks.set(taskId, {
      digest: entry.digest,
      threadId: entry.threadId === null ? null : ThreadId.make(entry.threadId),
      profile,
      recordedAt: entry.recordedAt,
    });
  }
  return { ok: true, tasks };
}

const encodeSavedBy = (tasks: ReadonlyMap<string, SavedBy>): string =>
  `${JSON.stringify({ version: VERSION, tasks: Object.fromEntries(tasks) }, null, 2)}\n`;

const named = (name: string) => `"${name}"`;

/** A top-level thread in `projectId`, as a scheduled run starts it: no id yet, so no pick names it. */
const scheduledRun = (projectId: ProjectId): LineageThread => {
  const id = ThreadId.make(`scheduled-task-run:${projectId}`);
  return {
    id,
    projectId,
    lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: id },
  };
};

/**
 * Built inside `ScheduledTaskService` (from `makeScheduledRunSlots`). Without the server's config
 * (a test of the service alone) there is no file: every task runs as the owner's, and a save by a
 * thread is refused, as it cannot be recorded.
 */
export const makeScheduledTaskMcpProfiles = Effect.fnUntraced(function* (
  threads: ThreadManagementService.ThreadManagementService["Service"],
) {
  const config = yield* Effect.serviceOption(ServerConfig.ServerConfig);
  const fileSystem = yield* Effect.serviceOption(FileSystem.FileSystem);
  const paths = yield* Effect.serviceOption(Path.Path);
  const sql = yield* SqlClient.SqlClient;
  const reads = yield* makeThreadMcpProfileReads;
  const lock = yield* Semaphore.make(1);

  const store =
    Option.isSome(config) && Option.isSome(fileSystem) && Option.isSome(paths)
      ? {
          fs: fileSystem.value,
          path: paths.value,
          filePath: paths.value.join(config.value.stateDir, SCHEDULED_TASK_MCP_PROFILES_FILE),
        }
      : undefined;
  if (store === undefined) {
    yield* Effect.logWarning(
      "Scheduled task MCP profiles: no state folder here, so a thread's save of a task is refused and every task runs as the owner's",
    );
  } else {
    // The production wiring has the file; a boot test looks for this span.
    yield* Effect.void.pipe(Effect.withSpan("ScheduledTaskMcpProfiles.stored"));
  }

  const where = `${SCHEDULED_TASK_MCP_PROFILES_FILE} in the server's state folder`;

  // No file, or no entry (saved before this record): the owner's. A file that cannot be read, or
  // that this version does not know, refuses saves and runs that start a new thread.
  const load: Effect.Effect<Loaded> =
    store === undefined
      ? Effect.succeed({ ok: true, tasks: new Map() })
      : store.fs.readFileString(store.filePath).pipe(
          Effect.map(decodeSavedBy),
          Effect.catch((error) =>
            error._tag === "PlatformError" && error.reason._tag === "NotFound"
              ? Effect.succeed<Loaded>({ ok: true, tasks: new Map() })
              : Effect.succeed<Loaded>({
                  ok: false,
                  issue: `it cannot be read (${error.message})`,
                }),
          ),
        );

  const unreadable = (issue: string) =>
    `The MCP profiles scheduled tasks were saved with (${where}) cannot be read: ${issue}. Fix that file (removing it lets every task run as the owner's).`;

  /** Read, change and write the file as one step (one save at a time in this server). */
  const update = (change: (tasks: Map<string, SavedBy>) => boolean) =>
    lock.withPermits(1)(
      Effect.gen(function* () {
        if (store === undefined) return { ok: true as const };
        const loaded = yield* load;
        if (!loaded.ok) return { ok: false as const, issue: unreadable(loaded.issue) };
        const tasks = new Map(loaded.tasks);
        if (!change(tasks)) return { ok: true as const };
        return yield* writeFileStringAtomically({
          filePath: store.filePath,
          contents: encodeSavedBy(tasks),
        }).pipe(
          Effect.provideService(FileSystem.FileSystem, store.fs),
          Effect.provideService(Path.Path, store.path),
          Effect.as({ ok: true as const }),
          Effect.catch((error) =>
            Effect.succeed({
              ok: false as const,
              issue: `The MCP profile this save is made with could not be recorded (${where}): ${error.message}.`,
            }),
          ),
        );
      }),
    );

  // Entries of tasks deleted since: dropped at start, and a failure here only logs.
  if (store !== undefined) {
    yield* Effect.gen(function* () {
      const rows = yield* sql<{ task_id: string }>`SELECT task_id FROM scheduled_tasks`;
      const live = new Set(rows.map((row) => row.task_id));
      const pruned = yield* update((tasks) => {
        let changed = false;
        for (const taskId of tasks.keys()) {
          if (live.has(taskId)) continue;
          tasks.delete(taskId);
          changed = true;
        }
        return changed;
      });
      if (!pruned.ok) yield* Effect.logWarning(pruned.issue);
    }).pipe(
      Effect.catchCause((cause) =>
        Effect.logWarning("Scheduled task MCP profiles: could not drop deleted tasks", { cause }),
      ),
    );
  }

  const refuse = (task: Pick<ScheduledTask, "id">, message: string) =>
    new ScheduledTaskError({ message, taskId: task.id });

  /** The new thread's profile, or a refusal's reason. */
  const runProfile = (
    profileFor: (thread: LineageThread) => Effect.Effect<McpProfileResolved, { reason: string }>,
    projectId: ProjectId,
  ) => profileFor(scheduledRun(projectId));

  // Last before a save writes the row, on the row it writes and the one it read (no separate
  // read). Recorded first: a save stopped in between leaves a record that does not match the row,
  // refused at run. A narrower thread is refused when the runs would start a thread wider than it,
  // or when it changes the prompt or thread of a task that runs in a wider thread.
  const saving = Effect.fn("ScheduledTaskMcpProfiles.saving")(function* (
    task: ScheduledTask,
    existing: ScheduledTask | null,
  ) {
    const scope = yield* Effect.serviceOption(McpInvocationContext.McpInvocationContext);
    const callerId = Option.isSome(scope) ? scope.value.thread?.threadId : undefined;
    const recordedAt = DateTime.formatIso(DateTime.toUtc(yield* DateTime.now));
    const record = (savedBy: Omit<SavedBy, "digest" | "recordedAt">) =>
      update((tasks) => {
        tasks.set(task.id, { digest: savedDigest(task), ...savedBy, recordedAt });
        return true;
      }).pipe(
        Effect.flatMap((written) =>
          written.ok ? Effect.void : Effect.fail(refuse(task, written.issue)),
        ),
      );
    if (callerId === undefined) return yield* record({ threadId: null, profile: null });
    if (store === undefined) {
      return yield* refuse(
        task,
        "This server cannot record the MCP profile of the thread that saves a scheduled task, so the task was not saved.",
      );
    }
    const caller = yield* threads.getThreadShell(callerId).pipe(Effect.orElseSucceed(() => null));
    if (caller === null || caller.deletedAt !== null) {
      return yield* refuse(task, "The calling thread was not found, so the task was not saved.");
    }
    const unknown = (reason: string) =>
      refuse(
        task,
        `This thread's MCP profile cannot be worked out, so the task was not saved. ${reason}`,
      );
    const profileFor = yield* Effect.mapError(reads, (refusal) => unknown(refusal.reason));
    const own = yield* Effect.mapError(profileFor(caller), (refusal) => unknown(refusal.reason));
    if (!isEveryMcpTool(own.profile)) {
      const boundTo = existing?.threadId ?? null;
      const changesWhatRuns =
        existing !== null &&
        (existing.prompt !== task.prompt ||
          existing.threadId !== task.threadId ||
          existing.projectId !== task.projectId);
      if (boundTo !== null && boundTo !== callerId && changesWhatRuns) {
        const bound = yield* threads.getThreadShell(boundTo).pipe(Effect.orElseSucceed(() => null));
        const theirs =
          bound === null ? null : yield* profileFor(bound).pipe(Effect.orElseSucceed(() => null));
        if (theirs === null || !isWithinMcpProfile(theirs.profile, own.profile)) {
          const what =
            theirs === null
              ? `thread ${boundTo}, whose MCP profile cannot be worked out`
              : `thread ${boundTo}, whose MCP profile ${named(theirs.name)} keeps MCP servers or T3 tools ${named(own.name)} does not`;
          return yield* refuse(
            task,
            `This thread's MCP profile is ${named(own.name)}, and this task runs in ${what}, so its prompt and where it runs cannot be changed from here (reading it, pausing it or changing its schedule can). Ask the owner to change it, or delete_scheduled_task removes it.`,
          );
        }
      }
      if (task.threadId === null) {
        const theirs = yield* runProfile(profileFor, task.projectId).pipe(
          Effect.mapError((refusal) =>
            refuse(
              task,
              `The MCP profile of a thread this task's runs start cannot be worked out, so the task was not saved. ${refusal.reason}`,
            ),
          ),
        );
        if (!isWithinMcpProfile(theirs.profile, own.profile)) {
          return yield* refuse(
            task,
            `This thread's MCP profile is ${named(own.name)}, and a thread this task's runs start in its project would get ${named(theirs.name)}, which keeps MCP servers or T3 tools ${named(own.name)} does not, so the task was not saved. Bind the task to this thread (bindToCurrentThread: true), schedule it in a project whose MCP profile is within ${named(own.name)}, use delegate_task, or ask the owner to schedule it.`,
          );
        }
      }
    }
    return yield* record({ threadId: callerId, profile: carryMcpProfile(own) });
  });

  /**
   * Why a run of `task` that starts a new thread is refused, or `null` when it may start. A task
   * with no entry runs as the owner's (saved before this record); one saved by the owner, or by a
   * thread on `all`, is not checked.
   */
  const runRefusal = Effect.fn("ScheduledTaskMcpProfiles.runRefusal")(function* (
    task: ScheduledTask,
  ) {
    const loaded = yield* load;
    if (!loaded.ok) return `Not run: ${unreadable(loaded.issue)}`;
    const savedBy = loaded.tasks.get(task.id);
    if (savedBy === undefined) return null;
    // Changed since its record (a save stopped half-way, two saves at once): writer not known.
    if (savedBy.digest !== savedDigest(task)) {
      return "Not run: this task changed after the MCP profile it was saved with was recorded, so who wrote it is not known. Save the task again (in the T3 app, or from the thread that should own it) to run it.";
    }
    const recorded = savedBy.profile;
    if (recorded === null || isEveryMcpTool(recorded)) return null;
    const theirs = yield* reads.pipe(
      Effect.flatMap((profileFor) => runProfile(profileFor, task.projectId)),
      Effect.result,
    );
    if (theirs._tag === "Failure") {
      return `Not run: the MCP profile of the thread this run would start cannot be worked out. ${theirs.failure.reason}`;
    }
    if (isWithinMcpProfile(theirs.success.profile, recorded)) return null;
    return `Not run: this task was saved by a thread whose MCP profile is ${named(recorded.name)}, and the thread this run would start in its project gets ${named(theirs.success.name)}, which keeps MCP servers or T3 tools ${named(recorded.name)} does not. Bind the task to a thread, move it to a project whose MCP profile is within ${named(recorded.name)}, or save it in the T3 app to run it as the owner's.`;
  });

  return { saving, runRefusal };
});
