/**
 * Fork: `GET /api/fork/prompt-mirror?threadId=<parent>`, what a parent's open tasks wait on, as
 * read-only cards (machine, kind, label, status). Each prompt keeps its one record where it was
 * asked; this reads it there and keeps nothing. No secret value or pairing code passes through.
 */
import {
  AuthOrchestrationReadScope,
  EnvironmentId,
  NodeId,
  type OrchestrationV2Subagent,
  ThreadId,
} from "@t3tools/contracts";
import { reportLabel } from "@t3tools/provider-core/server/notification";
import * as Clock from "effect/Clock";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import { HttpClient, HttpRouter, HttpServerRequest, HttpServerResponse } from "effect/http";

import * as EnvironmentAuth from "../auth/EnvironmentAuth.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import * as LocalTaskPrompts from "./localTaskPrompts.ts";
import * as RemoteTaskPrompts from "./remoteTaskPrompts.ts";

export const PROMPT_MIRROR_PATH = "/api/fork/prompt-mirror";

/** A linked machine's answer is used again for this long: many open views ask it once. */
const REMOTE_FRESH = Duration.seconds(5);
/** Labels are one plain line of at most this many characters. */
const LABEL_MAX = 80;
/** At most this many tasks are read at once. */
const READ_CONCURRENCY = 4;

export const MirrorKind = Schema.Literals(["question", "approval", "secret", "pairing"]);
/** Why a task's prompts are not known now: a linked machine's reasons, or a thread here unreadable. */
export const MirrorUnknownReason = Schema.Literals([
  "unlinked",
  "unreachable",
  "older",
  "refused",
  "unreadable",
]);

/** One card: a prompt that is open, or a task whose prompts are not known now. */
export const MirrorEntry = Schema.Struct({
  taskId: NodeId,
  /** The task's title or prompt, short. */
  task: Schema.NullOr(Schema.String),
  /** `null`: this machine. */
  machine: Schema.NullOr(Schema.Struct({ environmentId: EnvironmentId, label: Schema.String })),
  /** The task's thread, on its machine: where the prompt is answered. */
  childThreadId: ThreadId,
  /** The prompt's id on its machine (request or card); `null` when not known. */
  promptId: Schema.NullOr(Schema.String),
  kind: Schema.NullOr(MirrorKind),
  /** A secret's or pairing card's label, an approval's kind; never a value. */
  label: Schema.NullOr(Schema.String),
  status: Schema.Literals(["open", "unknown"]),
  reason: Schema.NullOr(MirrorUnknownReason),
});
export type MirrorEntry = typeof MirrorEntry.Type;

export const PromptMirror = Schema.Struct({
  threadId: ThreadId,
  entries: Schema.Array(MirrorEntry),
});
export type PromptMirror = typeof PromptMirror.Type;

const decodeThreadId = Schema.decodeUnknownOption(ThreadId);
const SETTLED_TASK = new Set(["completed", "failed", "cancelled", "interrupted"]);

const foldLabel = (text: string | null): string | null => {
  if (text === null) return null;
  const line = text.replace(/[\s\p{Cc}\p{Cf}\p{Zl}\p{Zp}]+/gu, " ").trim();
  if (line === "") return null;
  const chars = Array.from(line);
  return chars.length <= LABEL_MAX
    ? line
    : `${chars
        .slice(0, LABEL_MAX - 1)
        .join("")
        .trimEnd()}…`;
};

type Base = Pick<MirrorEntry, "taskId" | "task" | "machine" | "childThreadId">;
const open = (
  base: Base,
  kind: MirrorEntry["kind"],
  promptId: string,
  label: string | null,
): MirrorEntry => ({
  ...base,
  promptId,
  kind,
  label: foldLabel(label),
  status: "open",
  reason: null,
});
const unknown = (base: Base, reason: MirrorEntry["reason"]): MirrorEntry => ({
  ...base,
  promptId: null,
  kind: null,
  label: null,
  status: "unknown",
  reason,
});

/** A task's prompts here, as cards. Questions too: the mirror shows all a person is waited on. */
const localEntries = (base: Base, prompts: LocalTaskPrompts.LocalTaskPrompts) => [
  ...prompts.questions.map((question) => open(base, "question", question.requestId, null)),
  ...prompts.approvals.map((approval) => open(base, "approval", approval.requestId, approval.kind)),
  ...prompts.secrets.map((secret) => open(base, "secret", secret.itemId, secret.label)),
  ...prompts.pairings.map((pairing) => open(base, "pairing", pairing.itemId, pairing.label)),
];

/** A task's prompts on a linked machine, as cards; a thread gone there has none to list. */
const remoteEntries = (
  base: Base,
  read: RemoteTaskPrompts.PromptsRead,
): ReadonlyArray<MirrorEntry> => {
  if (read.type === "unknown") return [unknown(base, read.reason)];
  const { questions, approval, secrets } = read.prompts;
  return [
    ...questions.map((question) => open(base, "question", question.requestId, null)),
    ...(approval === null ? [] : [open(base, "approval", approval.requestId, approval.kind)]),
    ...secrets.map((secret) => open(base, "secret", secret.itemId, secret.label)),
  ];
};

/** Answers of linked machines, used again while fresh; a failed read is not kept. */
export const cachedReader = (
  read: RemoteTaskPrompts.ReadRemotePrompts,
  fresh: Duration.Input = REMOTE_FRESH,
): RemoteTaskPrompts.ReadRemotePrompts => {
  const freshMs = Duration.toMillis(Duration.fromInputUnsafe(fresh));
  const answers = new Map<
    string,
    { readonly at: number; readonly read: RemoteTaskPrompts.PromptsRead }
  >();
  return (remote) =>
    Effect.gen(function* () {
      const now = yield* Clock.currentTimeMillis;
      for (const [key, answer] of answers) if (now - answer.at >= freshMs) answers.delete(key);
      const key = `${remote.environmentId}\u0000${remote.threadId}`;
      const kept = answers.get(key);
      if (kept !== undefined) return kept.read;
      const result = yield* read(remote);
      if (result.type === "known") answers.set(key, { at: now, read: result });
      return result;
    });
};

const isOpenTask = (task: OrchestrationV2Subagent) =>
  task.origin === "app_owned" &&
  task.result === null &&
  !SETTLED_TASK.has(task.status) &&
  task.completionDelivery?.state !== "disposed";

/** The parent's open tasks' prompts; a task that cannot be read is one unknown card. */
const mirrorOf = Effect.fn("fork.promptMirror.read")(function* (
  threads: Pick<ThreadManagement.ThreadManagementService["Service"], "getThreadRecords">,
  readRemote: RemoteTaskPrompts.ReadRemotePrompts,
  parentThreadId: ThreadId,
) {
  const parent = yield* threads.getThreadRecords(parentThreadId, ["subagents"]);
  const tasks = parent.subagents.filter(isOpenTask);
  const entries = yield* Effect.forEach(
    tasks,
    (task): Effect.Effect<ReadonlyArray<MirrorEntry>> => {
      const label = reportLabel(task.title?.trim() || task.prompt) ?? null;
      if (task.childThreadId !== null) {
        const base: Base = {
          taskId: task.id,
          task: label,
          machine: null,
          childThreadId: task.childThreadId,
        };
        return LocalTaskPrompts.readChildRecords(threads, task.childThreadId).pipe(
          Effect.map((child) =>
            child.thread.archivedAt !== null || child.thread.deletedAt !== null
              ? []
              : localEntries(base, LocalTaskPrompts.localPromptsOf(task, child)),
          ),
          Effect.catchCause(() => Effect.succeed([unknown(base, "unreadable")])),
        );
      }
      if (task.remoteChild !== undefined) {
        const remote = task.remoteChild;
        const base: Base = {
          taskId: task.id,
          task: label,
          machine: { environmentId: remote.environmentId, label: remote.label },
          childThreadId: remote.threadId,
        };
        return readRemote(remote).pipe(
          Effect.map((read) => remoteEntries(base, read)),
          Effect.catchCause(() => Effect.succeed([unknown(base, "unreachable")])),
        );
      }
      return Effect.succeed([]);
    },
    { concurrency: READ_CONCURRENCY },
  );
  return { threadId: parentThreadId, entries: entries.flat() } satisfies PromptMirror;
});

const text = (body: string, status: number) =>
  HttpServerResponse.text(body, { status, headers: { "cache-control": "no-store" } });

/** The route's work: a session that may read orchestration, and a parent thread that is there. */
export const handlePromptMirror = Effect.fn("fork.promptMirror.handle")(function* (
  auth: Pick<EnvironmentAuth.EnvironmentAuth["Service"], "authenticateHttpRequest">,
  threads: Pick<
    ThreadManagement.ThreadManagementService["Service"],
    "getThreadShell" | "getThreadRecords"
  >,
  readRemote: RemoteTaskPrompts.ReadRemotePrompts,
) {
  const request = yield* HttpServerRequest.HttpServerRequest;
  const session = yield* auth.authenticateHttpRequest(request).pipe(Effect.option);
  if (Option.isNone(session)) return text("Unauthorized", 401);
  if (!session.value.scopes.includes(AuthOrchestrationReadScope)) return text("Forbidden", 403);
  const threadId = decodeThreadId(
    new URL(request.url, "http://mirror.invalid").searchParams.get("threadId"),
  );
  if (Option.isNone(threadId)) return text("threadId is required", 400);
  const answer = yield* Effect.gen(function* () {
    const shell = yield* threads.getThreadShell(threadId.value);
    if (shell === null || shell.deletedAt !== null) return Option.none<PromptMirror>();
    return Option.some(yield* mirrorOf(threads, readRemote, threadId.value));
  }).pipe(
    Effect.asSome,
    Effect.catchCause(() => Effect.succeedNone),
  );
  // A read that failed is no answer: a card shown as nothing pending would be a guess.
  if (Option.isNone(answer)) return text("Unavailable", 503);
  if (Option.isNone(answer.value)) return text("Not Found", 404);
  return HttpServerResponse.jsonUnsafe(answer.value.value, {
    headers: { "cache-control": "no-store" },
  });
});

/**
 * `GET /api/fork/prompt-mirror` in the server's routes (fork/forkLayers.ts). Linked tasks are read
 * only if the server has its links and an HTTP client; else they show as unknown, never as empty.
 */
export const layer = Layer.unwrap(
  Effect.gen(function* () {
    const auth = yield* EnvironmentAuth.EnvironmentAuth;
    const threads = yield* ThreadManagement.ThreadManagementService;
    const links = yield* Effect.serviceOption(PeerLinks.PeerLinks);
    const http = yield* Effect.serviceOption(HttpClient.HttpClient);
    const readRemote: RemoteTaskPrompts.ReadRemotePrompts =
      Option.isSome(links) && Option.isSome(http)
        ? cachedReader(RemoteTaskPrompts.makeReader(links.value, http.value))
        : () => Effect.succeed({ type: "unknown", reason: "unlinked" });
    return HttpRouter.add("GET", PROMPT_MIRROR_PATH, handlePromptMirror(auth, threads, readRemote));
  }),
);
