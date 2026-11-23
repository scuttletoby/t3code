/**
 * Fork: `lens_start` on a server: its reads (gh, git, the stored children), built with the wait service, and its
 * starts through the MCP server's own `delegate_task` (installed by fork/mcpT3ToolRule.ts), so every delegate check
 * applies. Until installed, an after-CI start is held, never lost.
 */
import {
  effectiveReviewRules,
  OrchestratorMcpDelegateTaskInput,
  type EnvironmentId,
  type ProjectId,
  ProviderInstanceId,
  ThreadId,
} from "@t3tools/contracts";
import type { WaitRecord } from "@t3tools/contracts/waitFor";
import { resolveProjectSettings } from "@t3tools/shared/projectSettings";
import * as Clock from "effect/Clock";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import { McpSchema } from "effect/ai";

import { ServerConfig } from "../../config.ts";
import * as McpInvocationContext from "../../mcp/McpInvocationContext.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import * as ProcessRunner from "../../processRunner.ts";
import * as ProjectEnrichmentService from "../../project/ProjectEnrichmentService.ts";
import * as ProjectService from "../../project/ProjectService.ts";
import { ServerSettingsService } from "../../serverSettings.ts";
import type { WaitAction } from "../../waits/WaitService.ts";
import type { LensStartChild } from "./lensStart.ts";
import * as LensChildren from "./lensStartChildren.ts";
import {
  delegateInputOf,
  LensReadError,
  lensStartNamespace,
  runLensStart,
  type CheckoutState,
  type LensStartWorld,
  type PullRead,
  type RawPost,
  type StartAnswer,
  type StoredChild,
} from "./lensStartRun.ts";

/** What lens_start needs of the MCP server: its own `tools/call`. */
export interface LensStartMcp {
  readonly callTool: (request: {
    readonly name: string;
    readonly arguments: Record<string, unknown>;
  }) => Effect.Effect<
    McpSchema.CallToolResult,
    McpSchema.McpErrorBase | McpSchema.InternalError | McpSchema.InvalidParams,
    McpSchema.McpServerClient
  >;
}

/** The MCP server the children start through; set once the MCP layer is built. */
let server: LensStartMcp | undefined;
let environmentId: EnvironmentId | undefined;

/** Installed by the MCP layer for its life (fork/mcpT3ToolRule.ts). */
export const installLensStartServer = (mcp: LensStartMcp, here: EnvironmentId | undefined) =>
  Effect.acquireRelease(
    Effect.sync(() => {
      server = mcp;
      environmentId = here;
    }),
    () =>
      Effect.sync(() => {
        if (server === mcp) server = undefined;
      }),
  );

/** The server's own `delegate_task` stands in for no MCP client: the after-CI start has none. */
const serverClient = McpSchema.McpServerClient.of({
  clientId: 0,
  protocolVersion: "2025-06-18",
  clientCapabilities: {},
  clientInfo: { name: "t3-lens-start", version: "1" },
  initializePayload: {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "t3-lens-start", version: "1" },
  },
  getClient: Effect.die("lens_start's own delegate_task call has no MCP client"),
});

const encodeDelegate = Schema.encodeSync(OrchestratorMcpDelegateTaskInput);
const decodeJson = Schema.decodeUnknownEffect(Schema.fromJsonString(Schema.Unknown));
/** How long the after-CI start waits for the thread's run (the wake starts it): 20 tries, 2 s apart. */
const PARENT_RUN_TRIES = 20;

/** One `delegate_task` call's answer, read from its tool result. */
function startAnswerOf(result: McpSchema.CallToolResult): StartAnswer {
  const content = result.structuredContent as Record<string, unknown> | undefined;
  if (result.isError === true || content === undefined) {
    const text = result.content
      .flatMap((part) => (part.type === "text" ? [part.text] : []))
      .join(" ");
    return {
      type: "refused",
      code: typeof content?.code === "string" ? content.code : "orchestration_error",
      message:
        typeof content?.message === "string" ? content.message : text || "delegate_task refused",
    };
  }
  return {
    type: "started",
    taskId: String(content.taskId),
    childThreadId: typeof content.childThreadId === "string" ? content.childThreadId : null,
  };
}

/** Starts one child through `delegate_task` in the context it is given (the caller's request, or the wait's). */
const delegate = (mcp: LensStartMcp, child: LensStartChild) =>
  mcp
    .callTool({
      name: "delegate_task",
      arguments: encodeDelegate(delegateInputOf(child)) as Record<string, unknown>,
    })
    .pipe(
      Effect.map(startAnswerOf),
      Effect.catch((error) =>
        Effect.succeed<StartAnswer>({
          type: "refused",
          code: "orchestration_error",
          message: `delegate_task failed: ${String((error as { message?: unknown }).message ?? error)}`,
        }),
      ),
    );

const GH_TIMEOUT = Duration.seconds(60);
const MAX_PAGES = 20;
const POST_FIELDS =
  "body url authorAssociation includesCreatedEdit isMinimized author { login __typename }";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const postOf = (node: Record<string, unknown>, when: unknown): RawPost => {
  const author = isRecord(node.author) ? node.author : {};
  return {
    body: typeof node.body === "string" ? node.body : "",
    when: typeof when === "string" ? when : "",
    url: typeof node.url === "string" ? node.url : null,
    login: typeof author.login === "string" ? author.login : null,
    bot: author.__typename === "Bot",
    association: typeof node.authorAssociation === "string" ? node.authorAssociation : null,
    edited: typeof node.includesCreatedEdit === "boolean" ? node.includesCreatedEdit : null,
    hidden: typeof node.isMinimized === "boolean" ? node.isMinimized : null,
  };
};

/** The `git diff --name-status -M` letter of one compare-API file. */
function changeOf(file: Record<string, unknown>): ReadonlyArray<string> | null {
  const path = typeof file.filename === "string" ? file.filename : null;
  if (path === null) return null;
  const old = typeof file.previous_filename === "string" ? file.previous_filename : path;
  switch (file.status) {
    case "added":
      return ["A", path];
    case "removed":
      return ["D", path];
    case "renamed":
      return ["R100", old, path];
    case "copied":
      return ["C100", old, path];
    case "unchanged":
      return null;
    default:
      return ["M", path];
  }
}

/** The compare API lists at most this many files: at this many lens_start cannot know it read them all. */
const COMPARE_FILES_MAX = 300;

export const makeLensStartServer = Effect.fn("LensStartServer.make")(function* () {
  const threads = yield* ThreadManagement.ThreadManagementService;
  const projects = yield* ProjectService.ProjectService;
  const enrichment = yield* ProjectEnrichmentService.ProjectEnrichmentService;
  const settingsService = yield* ServerSettingsService;
  const runner = yield* ProcessRunner.ProcessRunner;
  const config = yield* ServerConfig;
  const children = yield* LensChildren.make(config.stateDir);

  const run = (cwd: string, command: string, args: ReadonlyArray<string>) =>
    runner
      .run({ command, args, cwd, timeout: GH_TIMEOUT, timeoutBehavior: "timedOutResult" })
      .pipe(
        Effect.mapError(
          (error) => new LensReadError({ message: `${command} could not run: ${error.message}` }),
        ),
      );

  const gh = Effect.fnUntraced(function* (cwd: string, args: ReadonlyArray<string>) {
    const out = yield* run(cwd, "gh", args);
    if (out.timedOut) return yield* new LensReadError({ message: "gh timed out" });
    return out;
  });

  const ghJson = Effect.fnUntraced(function* (cwd: string, args: ReadonlyArray<string>) {
    const out = yield* gh(cwd, args);
    if (out.code !== 0) {
      return yield* new LensReadError({
        message: `gh ${args[0] ?? ""} failed: ${out.stderr.trim().slice(0, 300)}`,
      });
    }
    return yield* decodeJson(out.stdout).pipe(
      Effect.mapError(() => new LensReadError({ message: `gh ${args[0] ?? ""} gave no JSON` })),
    );
  });

  /**
   * One connection of the pull request, every page, via GraphQL. A pull request GitHub does not know: `pull` null. A
   * page that fails or is not the shape asked for, or more than MAX_PAGES pages: refused, never a partial list.
   */
  const pages = Effect.fnUntraced(function* (
    cwd: string,
    repo: string,
    number: number,
    connection: string,
  ) {
    const [owner, name] = repo.split("/") as [string, string];
    const key = connection.split("(")[0]!;
    const nodes: Array<Record<string, unknown>> = [];
    let pull: Record<string, unknown> | null = null;
    let after: string | null = null;
    for (let page = 0; ; page += 1) {
      if (page === MAX_PAGES) {
        return yield* new LensReadError({
          message: `#${number} has more than ${MAX_PAGES * 100} ${key}: not all read`,
        });
      }
      const query = `query($owner:String!,$name:String!,$number:Int!,$after:String){repository(owner:$owner,name:$name){pullRequest(number:$number){state headRefOid baseRefName headRefName ${connection.replace("$AFTER", "after:$after")}}}}`;
      const out: ProcessRunner.ProcessRunOutput = yield* gh(cwd, [
        "api",
        "graphql",
        "-f",
        `query=${query}`,
        "-f",
        `owner=${owner}`,
        "-f",
        `name=${name}`,
        "-F",
        `number=${number}`,
        ...(after === null ? [] : ["-f", `after=${after}`]),
      ]);
      if (out.code !== 0) {
        if (page === 0 && /Could not resolve to a PullRequest/u.test(out.stderr)) {
          return { pull: null, nodes };
        }
        return yield* new LensReadError({
          message: `page ${page + 1} of #${number}'s ${key} could not be read: ${out.stderr.trim().slice(0, 300)}`,
        });
      }
      const data: unknown = yield* decodeJson(out.stdout).pipe(
        Effect.mapError(
          () =>
            new LensReadError({ message: `page ${page + 1} of #${number}'s ${key} gave no JSON` }),
        ),
      );
      const repository: unknown =
        isRecord(data) && isRecord(data.data) ? data.data.repository : null;
      const pr: Record<string, unknown> | null =
        isRecord(repository) && isRecord(repository.pullRequest) ? repository.pullRequest : null;
      const conn: Record<string, unknown> | null =
        pr !== null && isRecord(pr[key]) ? pr[key] : null;
      if (pr === null || conn === null || !Array.isArray(conn.nodes)) {
        return yield* new LensReadError({
          message: `page ${page + 1} of #${number}'s ${key} is missing from GitHub's answer: not all read`,
        });
      }
      pull = pr;
      for (const node of conn.nodes) {
        if (isRecord(node)) nodes.push(node);
      }
      const info: Record<string, unknown> = isRecord(conn.pageInfo) ? conn.pageInfo : {};
      if (info.hasNextPage !== true) break;
      if (typeof info.endCursor !== "string") {
        return yield* new LensReadError({
          message: `#${number}'s ${key} has a next page GitHub gave no cursor for: not all read`,
        });
      }
      after = info.endCursor;
    }
    return { pull, nodes };
  });

  /** The world of one call, for the thread that makes it. */
  const worldFor = Effect.fn("LensStartServer.worldFor")(function* (args: {
    readonly threadId: ThreadId;
    readonly projectId: ProjectId;
    /** Starts one child in the right context (the caller's request, or the after-CI wait's). */
    readonly start: (child: LensStartChild) => Effect.Effect<StartAnswer>;
    readonly waitForCi?: LensStartWorld["waitForCi"];
  }) {
    const settings = yield* settingsService.getSettings.pipe(Effect.option);
    const value = Option.isSome(settings)
      ? resolveProjectSettings(settings.value, args.projectId).settings.reviewRules
      : undefined;
    const rules = effectiveReviewRules(value ?? undefined);
    const project = Option.flatten(yield* projects.getShell(args.projectId).pipe(Effect.option));
    const shell = yield* threads
      .getThreadShell(args.threadId)
      .pipe(Effect.orElseSucceed(() => null));
    const root = Option.isSome(project) ? project.value.workspaceRoot : null;
    const identity = Option.isSome(project)
      ? (project.value.repositoryIdentity ??
        (root === null ? null : yield* enrichment.readRepositoryIdentity(root)))
      : null;
    const repo =
      identity?.provider === "github" && identity.owner !== undefined && identity.name !== undefined
        ? `${identity.owner}/${identity.name}`
        : null;
    const cwd = root ?? ".";
    const checkoutDir = shell?.worktreePath ?? root;

    const readPull = Effect.fn("LensStartServer.readPull")(function* (number: number) {
      if (repo === null) return null;
      const comments = yield* pages(
        cwd,
        repo,
        number,
        `comments(first:100,$AFTER){pageInfo{hasNextPage endCursor} nodes{createdAt ${POST_FIELDS}}}`,
      );
      if (comments.pull === null) return null;
      // Reviews with their inline comments, as the gate reads them (pr_queue.review_flags, inline_post): a review or
      // comment not submitted yet (PENDING: only its author sees it) is no post; a comment's time is when it was
      // published.
      const reviews = yield* pages(
        cwd,
        repo,
        number,
        `reviews(first:100,$AFTER){pageInfo{hasNextPage endCursor} nodes{state submittedAt ${POST_FIELDS} comments(first:100){totalCount nodes{state createdAt publishedAt ${POST_FIELDS}}}}}`,
      );
      const reviewPosts: RawPost[] = [];
      const inlinePosts: RawPost[] = [];
      for (const review of reviews.nodes) {
        if (review.state === "PENDING") continue;
        reviewPosts.push(postOf(review, review.submittedAt));
        const conn = isRecord(review.comments) ? review.comments : {};
        if (typeof conn.totalCount === "number" && conn.totalCount > 100) {
          return yield* new LensReadError({
            message: `#${number} has a review of more than 100 inline comments: not all read`,
          });
        }
        for (const node of Array.isArray(conn.nodes) ? conn.nodes : []) {
          if (!isRecord(node) || node.state === "PENDING") continue;
          const published =
            typeof node.publishedAt === "string" && node.publishedAt !== ""
              ? node.publishedAt
              : node.createdAt;
          inlinePosts.push(postOf(node, published));
        }
      }
      const pr = comments.pull;
      const state = pr.state === "OPEN" ? "open" : pr.state === "MERGED" ? "merged" : "closed";
      return {
        number,
        state,
        head: String(pr.headRefOid ?? ""),
        base: String(pr.baseRefName ?? ""),
        headBranch: String(pr.headRefName ?? ""),
        posts: [
          ...comments.nodes.map((node) => postOf(node, node.createdAt)),
          ...reviewPosts,
          ...inlinePosts,
        ],
      } satisfies PullRead;
    });

    const readFile = Effect.fn("LensStartServer.readFile")(function* (ref: string, path: string) {
      if (repo === null) return null;
      const encoded = path.split("/").map(encodeURIComponent).join("/");
      const out = yield* gh(cwd, [
        "api",
        "-H",
        "Accept: application/vnd.github.raw+json",
        `repos/${repo}/contents/${encoded}?ref=${encodeURIComponent(ref)}`,
      ]);
      if (out.code === 0) return out.stdout;
      if (/HTTP 404|Not Found/u.test(out.stderr)) return null;
      return yield* new LensReadError({
        message: `${path} at ${ref} could not be read: ${out.stderr.trim().slice(0, 300)}`,
      });
    });

    const readChanges = Effect.fn("LensStartServer.readChanges")(function* (
      base: string,
      head: string,
    ) {
      if (repo === null) return [];
      const data = yield* ghJson(cwd, [
        "api",
        `repos/${repo}/compare/${encodeURIComponent(base)}...${head}`,
      ]);
      if (!isRecord(data) || !Array.isArray(data.files)) {
        return yield* new LensReadError({
          message: `GitHub's compare of ${base}...${head} lists no files: no lens is started rather than too few`,
        });
      }
      const files: ReadonlyArray<unknown> = data.files;
      if (files.some((file) => !isRecord(file) || typeof file.filename !== "string")) {
        return yield* new LensReadError({
          message: `GitHub's compare of ${base}...${head} lists a file without a name: no lens is started rather than too few`,
        });
      }
      if (files.length >= COMPARE_FILES_MAX) {
        return yield* new LensReadError({
          message: `the change lists ${files.length} files or more, the most GitHub's compare lists: lens_start cannot know it read every path, so no lens is started rather than too few`,
        });
      }
      return files.flatMap((file) => {
        const change = isRecord(file) ? changeOf(file) : null;
        return change === null ? [] : [change];
      });
    });

    const git = (dir: string, gitArgs: ReadonlyArray<string>) =>
      runner
        .run({
          command: "git",
          args: ["-C", dir, ...gitArgs],
          cwd: dir,
          timeout: Duration.seconds(20),
        })
        .pipe(Effect.option);

    const checkout = Effect.fn("LensStartServer.checkout")(function* (prHead: string) {
      if (checkoutDir === null) return null;
      const head = yield* git(checkoutDir, ["rev-parse", "HEAD"]);
      if (Option.isNone(head) || head.value.code !== 0) return null;
      const branch = yield* git(checkoutDir, ["rev-parse", "--abbrev-ref", "HEAD"]);
      const status = yield* git(checkoutDir, ["status", "--porcelain", "--untracked-files=no"]);
      // Commits the PR head lacks; with the PR head not fetched here, commits its upstream lacks.
      const vsHead = yield* git(checkoutDir, ["rev-list", "--count", `${prHead}..HEAD`]);
      const ahead =
        Option.isSome(vsHead) && vsHead.value.code === 0
          ? vsHead
          : yield* git(checkoutDir, ["rev-list", "--count", "@{upstream}..HEAD"]);
      const name = Option.isSome(branch) ? branch.value.stdout.trim() : "";
      return {
        branch: name === "" || name === "HEAD" ? null : name,
        head: head.value.stdout.trim(),
        dirty: Option.isSome(status) && status.value.stdout.trim() !== "",
        // Neither known (no upstream): not refused; the gate reads the pushed head anyway.
        ahead:
          Option.isSome(ahead) && ahead.value.code === 0 && Number(ahead.value.stdout.trim()) > 0,
      } satisfies CheckoutState;
    });

    const childRunning = Effect.fn("LensStartServer.childRunning")(function* (child: StoredChild) {
      // Just started: its run may not show yet.
      const now = yield* Clock.currentTimeMillis;
      if (now - Date.parse(child.startedAt) < 120_000) return true;
      if (child.childThreadId === null) return false;
      const shell = yield* threads
        .getThreadShell(ThreadId.make(child.childThreadId))
        .pipe(Effect.orElseSucceed(() => null));
      if (shell === null || shell.deletedAt !== null || shell.archivedAt !== null) return false;
      return shell.activeRunId !== null;
    });

    return {
      rules,
      repo,
      readPull,
      readFile,
      readChanges,
      checkout,
      children,
      childRunning,
      start: args.start,
      ...(args.waitForCi === undefined ? {} : { waitForCi: args.waitForCi }),
      parentThreadId: args.threadId,
      now: Clock.currentTimeMillis.pipe(
        Effect.map((ms) => DateTime.formatIso(DateTime.makeUnsafe(ms))),
      ),
    } satisfies LensStartWorld;
  });

  /** The start in the caller's own MCP request: `delegate_task` as that caller (its own scope). */
  const startInRequest =
    (scope: McpInvocationContext.McpInvocationScope) => (child: LensStartChild) =>
      server === undefined
        ? Effect.succeed<StartAnswer>({
            type: "refused",
            code: "orchestration_error",
            message: "this server's MCP tools are not ready yet; call lens_start again in a moment",
          })
        : delegate(server, child).pipe(
            // One namespace per thread, not per session: after a crash before the child was stored, a
            // new session's repeat is the same delegate_task command, so it returns that child.
            Effect.provideService(McpInvocationContext.McpInvocationContext, {
              ...scope,
              requestNamespace: lensStartNamespace(
                scope.thread?.threadId ?? scope.requestNamespace,
              ),
            }),
            Effect.provideService(McpSchema.McpServerClient, serverClient),
          );

  /**
   * The after-CI start, as the thread that registered the wait (the wake starts its run): `delegate_task` refuses a
   * child while that thread has no run, so it is asked again for up to 40 s, once per action.
   */
  const startForWait = (wait: WaitRecord) => {
    /** Waited once for the thread's run: the next child does not wait again. */
    let waited = false;
    return (child: LensStartChild) =>
      Effect.gen(function* () {
        const mcp = server;
        if (mcp === undefined) {
          return {
            type: "refused",
            code: "orchestration_error",
            message: "not ready",
          } as StartAnswer;
        }
        let answer: StartAnswer = { type: "refused", code: "parent_not_active", message: "no run" };
        const tries = waited ? 1 : PARENT_RUN_TRIES;
        waited = true;
        for (let attempt = 0; attempt < tries; attempt += 1) {
          const shell = yield* threads
            .getThreadShell(wait.threadId)
            .pipe(Effect.orElseSucceed(() => null));
          if (shell !== null) {
            const scope: McpInvocationContext.McpInvocationScope = {
              environmentId: (environmentId ?? "local") as EnvironmentId,
              requestNamespace: lensStartNamespace(wait.threadId),
              thread: {
                threadId: wait.threadId,
                providerSessionId: lensStartNamespace(wait.threadId),
                providerInstanceId: ProviderInstanceId.make(shell.providerInstanceId),
              },
              client: undefined,
              capabilities: new Set(["orchestration"]),
              issuedAt: yield* Clock.currentTimeMillis,
            };
            answer = yield* delegate(mcp, child).pipe(
              Effect.provideService(McpInvocationContext.McpInvocationContext, scope),
              Effect.provideService(McpSchema.McpServerClient, serverClient),
            );
            if (answer.type === "started" || answer.code !== "parent_not_active") return answer;
          }
          if (attempt + 1 < tries) yield* Effect.sleep(Duration.seconds(2));
        }
        return answer;
      });
  };

  /** The wait's action: lens_start for the wait's head, as its thread. Null: done; a text: why not. */
  const action: WaitAction = {
    ready: Effect.sync(() => server !== undefined),
    run: Effect.fn("LensStartServer.action")(
      function* (wait: WaitRecord) {
        if (wait.kind !== "pr" || wait.spec.head === undefined) {
          return "lens_start: this wait names no pull request head; nothing started";
        }
        const world = yield* worldFor({
          threadId: wait.threadId,
          projectId: wait.projectId,
          start: startForWait(wait),
        });
        const result = yield* runLensStart(world, { pr: wait.spec.number, head: wait.spec.head });
        if (result.refused.length === 0) return null;
        return `lens_start for #${wait.spec.number} at ${wait.spec.head.slice(0, 7)} ${result.started.length > 0 ? `started ${result.started.map((s) => s.lens).join(", ")}, but` : "started nothing:"} ${result.refused.join("; ")}`;
      },
      Effect.catchCause((cause) =>
        Effect.succeed(
          `lens_start could not run after CI: ${String(cause).slice(0, 300)}; call lens_start again`,
        ),
      ),
    ),
  };

  return { worldFor, startInRequest, startForWait, action, waitForCiAction: "lens_start" as const };
});

export type LensStartServer = Effect.Success<ReturnType<typeof makeLensStartServer>>;
