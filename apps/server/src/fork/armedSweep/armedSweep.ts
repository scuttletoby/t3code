/**
 * Fork (issue 880): the armed PR sweep's core: one GitHub list, judge each armed pull request,
 * wake its top-level thread (else the project's orchestrator) once per head and reason, joined per
 * thread. Pending before the send, settled from the receipt after. Rules: docs/fork/README.md.
 */
import type { ArmedSweepSettings, ProjectId, ThreadId } from "@t3tools/contracts";
import type { WaitArmedPullRequest } from "@t3tools/contracts/waitFor";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
// @effect-diagnostics-next-line nodeBuiltinImport:off -- The command id must be a deterministic digest.
import * as NodeCrypto from "node:crypto";

import {
  buildRequest,
  NODES_MAX,
  parseAnswer,
  searchQueries,
  type SearchCursor,
} from "./armedGitHub.ts";
import {
  type ArmedPull,
  headKey,
  judgePull,
  type Tracked,
  type Verdict,
  wakeKey,
} from "./armedJudge.ts";
import { EMPTY_STATE, readState, SENT_KEEP_MS, type SweepState, writeState } from "./armedState.ts";

/** At most this many requests per sweep (pages of a long search); past it the sweep stops. */
const MAX_REQUESTS = 4;
/** Lines of one joined message; the rest are counted. */
const MESSAGE_LINES = 20;

export class ArmedWorldError extends Schema.TaggedError<ArmedWorldError>()("ArmedWorldError", {
  message: Schema.String,
}) {}

/** A pull request link of a thread here (T3's own `link_pull_request` rows). */
export interface ArmedLink {
  readonly repository: string;
  readonly number: number;
  readonly threadId: ThreadId;
  readonly projectId: ProjectId;
}

export interface ArmedSources {
  /** Repositories to sweep, each with the project it belongs to, if one has it as its remote. */
  readonly repositories: ReadonlyArray<{
    readonly repository: string;
    readonly projectId: ProjectId | null;
  }>;
  /** Newest first per pull request, as the linked-threads list orders them. */
  readonly links: ReadonlyArray<ArmedLink>;
}

/** A thread as the owner lookup needs it. */
export interface ArmedThread {
  readonly threadId: ThreadId;
  readonly projectId: ProjectId;
  /** A delegated child's or sub-agent's parent; null for a top-level thread. */
  readonly parentThreadId: ThreadId | null;
  /** Archived or deleted: it cannot take a message, so the fallback gets it. */
  readonly gone: boolean;
}

export interface ArmedWake {
  readonly threadId: ThreadId;
  readonly commandId: string;
  readonly messageId: string;
  readonly text: string;
  readonly summary: string;
}

export interface ArmedWorld {
  readonly sources: (
    scope: ArmedSweepSettings["scope"],
  ) => Effect.Effect<ArmedSources, ArmedWorldError>;
  /** One GraphQL request to github.com; its parsed JSON answer. */
  readonly graphql: (document: string) => Effect.Effect<unknown, ArmedWorldError>;
  readonly thread: (threadId: ThreadId) => Effect.Effect<ArmedThread | null, ArmedWorldError>;
  /** The project's orchestrator thread (threadRoles), if one is set. */
  readonly fallback: (projectId: ProjectId) => Effect.Effect<ThreadId | null, ArmedWorldError>;
  /** `retry`: not sent now (no agent slot); asked again next sweep. */
  readonly send: (wake: ArmedWake) => Effect.Effect<"sent" | "retry", ArmedWorldError>;
  /** The engine took this command (a receipt that is not a rejection). */
  readonly accepted: (commandId: string) => Effect.Effect<boolean, ArmedWorldError>;
  readonly readState: Effect.Effect<string | null, ArmedWorldError>;
  readonly writeState: (text: string) => Effect.Effect<void, ArmedWorldError>;
  readonly log: (message: string, fields?: Record<string, unknown>) => Effect.Effect<void>;
}

export interface SweepReport {
  readonly mode: "dry" | "on";
  readonly sweptAt: number;
  readonly error?: string;
  readonly pullRequests: ReadonlyArray<ArmedRow>;
}

/** A row of `wait_list`, with the project and thread it is listed under. */
export interface ArmedRow extends Omit<WaitArmedPullRequest, "notGreenSince"> {
  readonly notGreenSince: number | null;
  readonly projectId: ProjectId | null;
}

type Owner =
  | {
      readonly type: "thread";
      readonly threadId: ThreadId;
      readonly projectId: ProjectId | null;
      readonly note: string | null;
    }
  | { readonly type: "none"; readonly projectId: ProjectId | null };

/** Up the parent chain at most this far: a loop in bad data ends here. */
const MAX_PARENTS = 8;

const shortHead = (head: string) => head.slice(0, 7);
const hashOf = (keys: ReadonlyArray<string>) =>
  NodeCrypto.createHash("sha256").update(keys.toSorted().join("\n")).digest("hex").slice(0, 24);

export const makeSweep = (world: ArmedWorld) => {
  /** The top-level thread above this one, or null when the chain is unreadable. */
  const topLevel = Effect.fn("ArmedSweep.topLevel")(function* (threadId: ThreadId) {
    let thread = yield* world.thread(threadId);
    for (let step = 0; thread !== null && thread.parentThreadId !== null; step++) {
      if (step >= MAX_PARENTS) return null;
      thread = yield* world.thread(thread.parentThreadId);
    }
    return thread;
  });

  const ownerOf = Effect.fn("ArmedSweep.ownerOf")(function* (
    pull: Pick<Tracked, "repository" | "number">,
    sources: ArmedSources,
    scope: ArmedSweepSettings["scope"],
  ) {
    const links = sources.links.filter(
      (link) => link.repository === pull.repository && link.number === pull.number,
    );
    const repositoryProject =
      sources.repositories.find((entry) => entry.repository === pull.repository)?.projectId ?? null;
    for (const link of links) {
      const owner = yield* topLevel(link.threadId);
      if (owner !== null && !owner.gone) {
        return {
          type: "thread",
          threadId: owner.threadId,
          projectId: owner.projectId,
          note: null,
        } satisfies Owner;
      }
    }
    // Linked, but every owner is archived or gone: handed over. Not linked: only with scope `all`.
    const projectId = links[0]?.projectId ?? repositoryProject;
    if (links.length === 0 && scope !== "all") return null;
    const fallback = projectId === null ? null : yield* world.fallback(projectId);
    if (fallback === null) return { type: "none", projectId } satisfies Owner;
    return {
      type: "thread",
      threadId: fallback,
      projectId,
      note: links.length === 0 ? "no thread links it" : "its thread is archived",
    } satisfies Owner;
  });

  /** Every page of every search, plus the followed ids, in at most MAX_REQUESTS requests. */
  const list = Effect.fn("ArmedSweep.list")(function* (
    repositories: ReadonlyArray<string>,
    ids: ReadonlyArray<string>,
  ) {
    let searches: ReadonlyArray<SearchCursor> = searchQueries(repositories).map((query) => ({
      query,
      after: null,
    }));
    let followed = ids.slice(0, NODES_MAX);
    const pulls = new Map<string, ArmedPull>();
    for (let request = 0; searches.length > 0 || followed.length > 0; request++) {
      if (request >= MAX_REQUESTS) {
        return yield* new ArmedWorldError({
          message: "the open pull requests take too many pages",
        });
      }
      const answer = yield* world.graphql(buildRequest(searches, followed));
      const parsed = parseAnswer(searches, answer);
      if ("error" in parsed) return yield* new ArmedWorldError({ message: parsed.error });
      for (const pull of parsed.pulls) if (!pulls.has(pull.id)) pulls.set(pull.id, pull);
      searches = parsed.more;
      followed = [];
    }
    return pulls;
  });

  const sweep = Effect.fn("ArmedSweep.sweep")(function* (
    settings: ArmedSweepSettings,
    nowMs: number,
  ) {
    if (settings.mode === "off") return null;
    const mode = settings.mode;
    const read = readState(yield* world.readState);
    if (read.type === "newer") {
      return {
        mode,
        sweptAt: nowMs,
        error: `the state file is from a newer T3 (version ${read.version}); the sweep stands still`,
        pullRequests: [],
      } satisfies SweepReport;
    }
    if (read.type === "empty" && read.warning !== null) yield* world.log(read.warning);
    const loaded = read.type === "ok" ? read.state : EMPTY_STATE;

    // 1. Settle what a sweep that stopped half-way was sending.
    const sent: Record<string, number> = { ...loaded.sent };
    for (const pending of loaded.pending) {
      if (yield* world.accepted(pending.commandId)) {
        for (const key of pending.keys) sent[key] ??= nowMs;
      }
    }

    // 2. One list.
    const sources = yield* world.sources(settings.scope);
    const repositories = new Set(sources.repositories.map((entry) => entry.repository));
    for (const link of sources.links) repositories.add(link.repository);
    const pulls = yield* list([...repositories], Object.keys(loaded.tracked));

    // 3. Judge each followed or armed pull request.
    const tracked: Record<string, Tracked> = {};
    const rows: ArmedRow[] = [];
    const due = new Map<ThreadId, Array<{ key: string; line: string }>>();
    const dueRows = new Map<string, number>();
    const perHead = new Map<string, number>();
    for (const key of Object.keys(sent)) {
      const head = key.slice(0, key.lastIndexOf(":"));
      perHead.set(head, (perHead.get(head) ?? 0) + 1);
    }
    for (const previous of Object.values(loaded.tracked)) {
      // Not in the answer (GitHub no longer resolves it): kept as it was, nothing said on a guess.
      if (!pulls.has(previous.id)) tracked[previous.id] = previous;
    }
    for (const pull of pulls.values()) {
      const previous = loaded.tracked[pull.id];
      if (previous === undefined && !(pull.armed && pull.state === "open")) continue;
      const owner = yield* ownerOf(pull, sources, settings.scope);
      // Scope `linked` and no thread links it: not this sweep's.
      if (owner === null) continue;
      const judged = judgePull({ pull, previous, nowMs, settings });
      if (judged.next !== null) tracked[pull.id] = judged.next;
      const verdict: Verdict = judged.verdict;
      if (verdict.type === "green" || verdict.type === "drop") continue;
      const row: ArmedRow = {
        pullRequest: `${pull.repository}#${pull.number}`,
        url: pull.url,
        title: pull.title,
        head: shortHead(pull.head),
        state: verdict.type === "wake" ? verdict.reason : "waiting",
        detail: verdict.detail,
        notGreenSince: judged.next?.notGreenSince ?? previous?.notGreenSince ?? null,
        threadId: owner.type === "thread" ? owner.threadId : null,
        projectId: owner.projectId,
        wake: "none",
      };
      if (verdict.type === "wake") {
        const key = wakeKey(pull, verdict.reason);
        const head = headKey(pull);
        if (sent[key] !== undefined) {
          rows.push({ ...row, wake: "sent" });
          continue;
        }
        if ((perHead.get(head) ?? 0) >= settings.maxWakesPerHead) {
          rows.push({ ...row, wake: "capped" });
          continue;
        }
        if (owner.type === "none") {
          rows.push({ ...row, wake: "no-thread" });
          continue;
        }
        perHead.set(head, (perHead.get(head) ?? 0) + 1);
        const lines = due.get(owner.threadId) ?? [];
        lines.push({
          key,
          line: `- ${row.pullRequest} "${pull.title}" (head ${row.head}): ${verdict.reason}, ${verdict.detail}${
            owner.note === null ? "" : ` (${owner.note}: sent to the project's orchestrator)`
          }. ${pull.url}`,
        });
        due.set(owner.threadId, lines);
        dueRows.set(key, rows.length);
      }
      rows.push(row);
    }

    // 4. Send: one joined message per thread, pending first.
    let pending: Array<{ commandId: string; keys: ReadonlyArray<string> }> = [];
    const save = (next: SweepState) => world.writeState(writeState(next));
    const pruned = () => {
      const heads = new Set(Object.values(tracked).map(headKey));
      return Object.fromEntries(
        Object.entries(sent).filter(
          ([key, at]) => heads.has(key.slice(0, key.lastIndexOf(":"))) || nowMs - at < SENT_KEEP_MS,
        ),
      );
    };
    const mark = (keys: ReadonlyArray<string>, wake: ArmedRow["wake"]) => {
      for (const key of keys) {
        const index = dueRows.get(key);
        if (index !== undefined) rows[index] = { ...rows[index]!, wake };
      }
    };
    for (const [threadId, lines] of due) {
      const keys = lines.map((entry) => entry.key);
      if (mode === "dry") {
        yield* world.log("armed sweep (dry): would wake a thread", { threadId, wakes: keys });
        mark(keys, "would-send");
        continue;
      }
      const id = `${threadId}:${hashOf(keys)}`;
      const commandId = `server:armed-sweep:${id}`;
      pending.push({ commandId, keys });
      yield* save({ tracked, sent: pruned(), pending });
      const shown = lines.slice(0, MESSAGE_LINES).map((entry) => entry.line);
      const more = lines.length - shown.length;
      const result = yield* world
        .send({
          threadId,
          commandId,
          messageId: `message:armed-sweep:${id}`,
          summary: `${lines.length} armed pull request${lines.length === 1 ? "" : "s"} cannot merge by ${lines.length === 1 ? "itself" : "themselves"}`,
          text: [
            "The armed pull request sweep found pull requests with auto-merge on that will not merge by themselves (sent once per head and reason):",
            ...shown,
            ...(more > 0 ? [`- and ${more} more (wait_list lists them all)`] : []),
            "Fix and push each, or turn auto-merge off for one that should not merge.",
          ].join("\n"),
        })
        .pipe(
          // Not known whether it went: it stays pending, and the next sweep reads the receipt.
          Effect.catch((error) =>
            world
              .log("armed sweep: a wake could not be sent", { threadId, error: error.message })
              .pipe(Effect.as("unknown" as const)),
          ),
        );
      if (result === "unknown") {
        mark(keys, "retry");
        continue;
      }
      pending = pending.filter((entry) => entry.commandId !== commandId);
      if (result === "sent") {
        for (const key of keys) sent[key] = nowMs;
        mark(keys, "sent");
      } else {
        mark(keys, "retry");
      }
    }
    yield* save({ tracked, sent: pruned(), pending });
    return { mode, sweptAt: nowMs, pullRequests: rows } satisfies SweepReport;
  });

  return { sweep };
};
