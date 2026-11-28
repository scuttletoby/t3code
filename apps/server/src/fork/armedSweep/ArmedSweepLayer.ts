/**
 * Fork (issue 880): the armed PR sweep's live world (settings, T3's pull request links, `gh api
 * graphql`, the slot-gated send, receipts, the state file) and its loop: one sweep per interval
 * while `armedSweep.mode` is `dry` or `on`; a held server (`serve --hold-work`) does not sweep.
 */
import { CommandId, MessageId, type ProjectId, type ThreadId } from "@t3tools/contracts";
import * as NodeSqliteClient from "@t3tools/shared/nodeSqliteClient";
import { writeFileStringAtomically } from "@t3tools/shared/atomicWrite";
import * as Clock from "effect/Clock";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as Schema from "effect/Schema";
import * as SqlClient from "effect/sql/SqlClient";

import { ServerConfig } from "../../config.ts";
import * as CommandReceiptStore from "../../orchestration-v2/CommandReceiptStore.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import * as ProcessRunner from "../../processRunner.ts";
import * as ProjectService from "../../project/ProjectService.ts";
import { forkParked } from "../../serverActivation.ts";
import { ServerSettingsService } from "../../serverSettings.ts";
import * as SlotGate from "../../slots/SlotGate.ts";
import * as WorkAdmission from "../../workAdmission.ts";
import { admitSend } from "../agentSlotRoutes.ts";
import { publishArmedReport } from "./armedRows.ts";
import {
  type ArmedLink,
  type ArmedWorld,
  ArmedWorldError,
  makeSweep,
  type SweepReport,
} from "./armedSweep.ts";
import { STATE_FILE } from "./armedState.ts";

const GH_TIMEOUT = Duration.seconds(60);
const PROJECT_PAGE = 200;
/** While off, the settings are read again this often. */
const OFF_POLL = Duration.seconds(60);

const decodeJson = Schema.decodeUnknownEffect(Schema.fromJsonString(Schema.Unknown));

const worldError = (message: string) => (cause: unknown) =>
  new ArmedWorldError({
    message: `${message}: ${cause instanceof Error ? cause.message : String(cause)}`.slice(0, 300),
  });

const makeLiveWorld = Effect.fn("ArmedSweep.makeLiveWorld")(function* () {
  const settingsService = yield* ServerSettingsService;
  const threads = yield* ThreadManagement.ThreadManagementService;
  const receipts = yield* CommandReceiptStore.CommandReceiptStoreV2;
  const projects = yield* ProjectService.ProjectService;
  const runner = yield* ProcessRunner.ProcessRunner;
  const sql = yield* SqlClient.SqlClient;
  const fs = yield* FileSystem.FileSystem;
  const config = yield* ServerConfig;
  const gate = yield* Effect.serviceOption(SlotGate.SlotGate);
  const path = yield* Path.Path;
  const statePath = path.join(config.stateDir, STATE_FILE);

  /** Every github.com pull request link of a thread that is not deleted, newest thread first. */
  const links = sql<{ repository: string; number: number; threadId: string; projectId: string }>`
    SELECT json_extract(link.value, '$.repository') AS repository,
      json_extract(link.value, '$.number') AS number,
      t.thread_id AS "threadId", t.project_id AS "projectId"
    FROM orchestration_v2_projection_threads AS t
    JOIN json_each(t.payload_json, '$.pullRequests') AS link
    WHERE t.deleted_at IS NULL
      AND json_extract(link.value, '$.host') = 'github.com'
      AND json_extract(link.value, '$.source') IS NOT 'stack-dismissed'
      AND json_extract(t.payload_json, '$.lineage.sideChat') IS NOT 1
    ORDER BY t.updated_at DESC, t.thread_id ASC
  `.pipe(NodeSqliteClient.readOnly);

  const projectRepositories = Effect.gen(function* () {
    const found: Array<{ repository: string; projectId: ProjectId }> = [];
    let cursor: number | null = 0;
    while (cursor !== null) {
      const page: Effect.Success<ReturnType<typeof projects.listPage>> = yield* projects.listPage({
        cursor,
        limit: PROJECT_PAGE,
      });
      for (const project of page.projects) {
        const identity = project.repositoryIdentity;
        if (identity?.provider === "github" && identity.owner && identity.name) {
          found.push({ repository: `${identity.owner}/${identity.name}`, projectId: project.id });
        }
      }
      cursor = page.nextCursor;
    }
    return found;
  });

  const world: ArmedWorld = {
    sources: Effect.fn("ArmedSweep.sources")(
      function* (scope) {
        const rows = yield* links;
        const linked: ReadonlyArray<ArmedLink> = rows
          .filter((row) => typeof row.repository === "string" && typeof row.number === "number")
          .map((row) => ({
            repository: row.repository,
            number: row.number,
            threadId: row.threadId as ThreadId,
            projectId: row.projectId as ProjectId,
          }));
        const owned = yield* projectRepositories;
        const linkedRepositories = new Set(linked.map((link) => link.repository));
        // Scope `linked` sweeps only repositories a thread links a pull request of; the project's
        // own is still named, for the fallback thread of a link whose thread is gone.
        const repositories = [
          ...owned.filter((entry) => scope === "all" || linkedRepositories.has(entry.repository)),
          ...[...linkedRepositories]
            .filter((repository) => !owned.some((entry) => entry.repository === repository))
            .map((repository) => ({ repository, projectId: null })),
        ];
        return { repositories, links: linked };
      },
      Effect.mapError(worldError("the pull request links could not be read")),
    ),
    graphql: Effect.fn("ArmedSweep.graphql")(function* (document) {
      const out = yield* runner
        .run({
          command: "gh",
          args: ["api", "graphql", "--hostname", "github.com", "-f", `query=${document}`],
          cwd: config.stateDir,
          timeout: GH_TIMEOUT,
          timeoutBehavior: "timedOutResult",
        })
        .pipe(
          Effect.mapError(
            (error) => new ArmedWorldError({ message: `gh could not run: ${error.message}` }),
          ),
        );
      if (out.timedOut) return yield* new ArmedWorldError({ message: "gh timed out" });
      // gh exits non-zero on a GraphQL error but still prints the answer: the parser judges it.
      return yield* decodeJson(out.stdout).pipe(
        Effect.mapError(
          () =>
            new ArmedWorldError({
              message: `gh api graphql failed: ${out.stderr.trim().slice(0, 300)}`,
            }),
        ),
      );
    }),
    thread: (threadId) =>
      threads.getThreadShell(threadId).pipe(
        Effect.map((shell) =>
          shell === null
            ? null
            : {
                threadId: shell.id,
                projectId: shell.projectId,
                parentThreadId:
                  shell.lineage.relationshipToParent === "subagent"
                    ? shell.lineage.parentThreadId
                    : null,
                gone: shell.archivedAt !== null,
              },
        ),
        Effect.mapError(worldError("a thread could not be read")),
      ),
    fallback: (projectId) =>
      settingsService.getSettings.pipe(
        Effect.map(
          (settings) =>
            (settings.threadRoles.orchestrators[projectId] as ThreadId | undefined) ?? null,
        ),
        Effect.mapError(worldError("the settings could not be read")),
      ),
    send: Effect.fn("ArmedSweep.send")(
      function* (wake) {
        const shell = yield* threads.getThreadShell(wake.threadId);
        if (shell === null || shell.archivedAt !== null) return "retry" as const;
        const slot = yield* admitSend(gate, {
          callerThreadId: wake.threadId,
          threadId: wake.threadId,
          instanceId: shell.modelSelection.instanceId,
          keepsHeld: true,
          place: { key: `armed-sweep:${wake.commandId}` },
        });
        if (slot.type === "refuse") return "retry" as const;
        yield* threads.dispatch({
          type: "message.dispatch",
          commandId: CommandId.make(wake.commandId),
          threadId: wake.threadId,
          messageId: MessageId.make(wake.messageId),
          text: wake.text,
          notification: { source: { kind: "monitor" }, outcome: "failed", summary: wake.summary },
          attachments: [],
          dispatchMode: { type: "queue_after_active" },
          createdBy: "agent",
          creationSource: "server",
        });
        return "sent" as const;
      },
      Effect.mapError(worldError("the wake could not be sent")),
    ),
    accepted: (commandId) =>
      receipts.getByCommandId(CommandId.make(commandId)).pipe(
        Effect.map((receipt) => Option.isSome(receipt) && receipt.value.status !== "rejected"),
        Effect.mapError(worldError("a receipt could not be read")),
      ),
    readState: fs.readFileString(statePath).pipe(
      Effect.map((text): string | null => text),
      Effect.catchIf(
        (error) => error._tag === "PlatformError" && error.reason._tag === "NotFound",
        () => Effect.succeed(null),
      ),
      Effect.mapError(worldError("the state file could not be read")),
    ),
    writeState: (contents) =>
      writeFileStringAtomically({ filePath: statePath, contents }).pipe(
        Effect.provideService(FileSystem.FileSystem, fs),
        Effect.provideService(Path.Path, path),
        Effect.mapError(worldError("the state file could not be written")),
      ),
    log: (message, fields) => Effect.logInfo(message, fields ?? {}),
  };
  return { world, settings: settingsService };
});

/** Sweeps forever: once per interval while dry or on, the settings read again each time. */
const run = Effect.fn("ArmedSweep.run")(function* () {
  const { world, settings } = yield* makeLiveWorld();
  const { sweep } = makeSweep(world);
  let last: SweepReport | null = null;
  const once = Effect.gen(function* () {
    const current = (yield* settings.getSettings).armedSweep;
    const now = yield* Clock.currentTimeMillis;
    const report = yield* sweep(current, now).pipe(
      Effect.catchCause((cause) =>
        Effect.logWarning("armed sweep: the sweep failed; nothing was sent", { cause }).pipe(
          Effect.as<SweepReport | null>(
            current.mode === "off"
              ? null
              : {
                  mode: current.mode,
                  sweptAt: last?.sweptAt ?? now,
                  error:
                    "the last sweep failed (see the server log); these rows are from the one before",
                  pullRequests: last?.pullRequests ?? [],
                },
          ),
        ),
      ),
    );
    if (report !== null && report.error === undefined) last = report;
    publishArmedReport(report);
    return current.mode === "off" ? OFF_POLL : Duration.seconds(current.intervalSeconds);
  }).pipe(Effect.catchCause(() => Effect.succeed(OFF_POLL)));
  return yield* Effect.forever(once.pipe(Effect.flatMap((wait) => Effect.sleep(wait))), {
    disableYield: false,
  } as never);
});

/** In the routes (fork/forkLayers.ts), beside the other fork wakers. */
export const layer = Layer.effectDiscard(
  Effect.gen(function* () {
    if ((yield* WorkAdmission.WorkAdmission).heldFromBoot) return;
    yield* forkParked(run());
  }),
).pipe(
  Layer.provide(
    Layer.mergeAll(
      // The server's one gate: layers memoize by reference.
      SlotGate.layer,
      CommandReceiptStore.layerFromApplicationReceipts,
    ),
  ),
);
