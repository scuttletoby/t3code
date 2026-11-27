/**
 * Fork: a task delegated to a linked environment on another version still completes with
 * its reply when that environment's thread details no longer decode here. Upstream's
 * `RemoteDelegation.test.ts` covers following itself.
 */
import { assert, it } from "@effect/vitest";
import {
  type EnvironmentId,
  NodeId,
  OrchestratorMcpFailure,
  OrchestratorMcpThreadReadResult,
  ProviderInstanceId,
  RunId,
  ThreadId,
} from "@t3tools/contracts";
import * as Context from "effect/Context";
import * as Deferred from "effect/Deferred";
import * as SecureRandom from "effect/crypto/SecureRandom";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import { HttpClient } from "effect/http";
import * as Schedule from "effect/Schedule";
import * as Schema from "effect/Schema";
import * as Stream from "effect/Stream";

import * as ServerEnvironment from "../environment/ServerEnvironment.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as RepositoryIdentityResolver from "../project/RepositoryIdentityResolver.ts";

const box = "environment-box" as EnvironmentId;
const parentThreadId = ThreadId.make("thread:parent");
const childThreadId = ThreadId.make("thread:box-child");
const taskId = NodeId.make("node:task");
const runId = RunId.make("run:box");
const instanceId = ProviderInstanceId.make("codex");
const at = "2026-11-19T10:00:00.000Z";
const reply = "The review found two bugs; both are fixed on the branch.";

/** A thread read as this version answers it, with one linked pull request. */
const currentAnswer = {
  thread: {
    threadId: childThreadId,
    projectId: "project:box",
    title: "Review",
    createdBy: "agent",
    creationSource: "mcp",
    status: "completed",
    latestRunId: runId,
    activeRunId: null,
    providerInstanceId: instanceId,
    model: "gpt-5.4",
    runtimeMode: "full-access",
    interactionMode: "default",
    linkedPullRequest: null,
    pullRequests: [
      {
        host: "github.com",
        repository: "acme/app",
        number: 7,
        url: "https://github.com/acme/app/pull/7",
        source: "agent",
        state: "open",
        watching: false,
        watchStartedAt: null,
      },
    ],
    titleRegeneration: null,
    branch: null,
    worktreePath: null,
    parentThreadId: null,
    relationshipToParent: null,
    runCount: 1,
    itemCount: 1,
    pendingRequestCount: 0,
    archived: false,
    settled: false,
    settledAt: null,
    snoozed: false,
    snoozedUntil: null,
    createdAt: at,
    updatedAt: at,
  },
  recentRuns: [
    {
      runId,
      ordinal: 1,
      status: "completed",
      providerInstanceId: instanceId,
      model: "gpt-5.4",
      requestedAt: at,
      startedAt: at,
      completedAt: at,
    },
  ],
  items: [
    {
      position: 0,
      visibility: "local",
      sourceThreadId: childThreadId,
      itemId: "item:reply",
      runId,
      messageId: null,
      createdBy: "agent",
      creationSource: "provider",
      type: "assistant_message",
      status: "completed",
      title: null,
      text: reply,
      textTruncated: false,
      updatedAt: at,
    },
  ],
  nextPosition: null,
  hasMore: false,
};

/** The same read from a build before linked pull requests carried their watch. */
const olderAnswer = {
  ...currentAnswer,
  thread: {
    ...currentAnswer.thread,
    pullRequests: currentAnswer.thread.pullRequests.map(
      ({ watching: _watching, watchStartedAt: _since, ...pullRequest }) => pullRequest,
    ),
  },
};

/** An older answer whose reply item itself lacks a field this version requires. */
const unreadableAnswer = {
  ...olderAnswer,
  items: olderAnswer.items.map(({ textTruncated: _truncated, ...item }) => item),
};

/**
 * An older version's thread of 250 items, read over pages: its first page (after position 0) has
 * no reply, and its last page (after position 199) has it. Any other page is not expected.
 */
const olderPagedAnswer = (input: { readonly afterPosition?: number }) => {
  const thread = { ...olderAnswer.thread, itemCount: 250 };
  // Upstream's scan starts after position 0, the thread's first message, which older peers need.
  if (input.afterPosition === 0) {
    return { ...olderAnswer, thread, items: [], nextPosition: 99, hasMore: true };
  }
  if (input.afterPosition === 199) {
    const items = olderAnswer.items.map((item) => ({ ...item, position: 249 }));
    return { ...olderAnswer, thread, items, nextPosition: null, hasMore: false };
  }
  return { unexpectedPage: input.afterPosition };
};

/** What the peer client does with an answer: decode it here or fail as it does. */
const decodeAnswer = (success: Schema.Top, answer: unknown) =>
  Schema.decodeUnknownEffect(success as Schema.Decoder<unknown>)(answer).pipe(
    Effect.mapError(
      () =>
        new OrchestratorMcpFailure({
          code: "orchestration_error",
          message:
            "The linked environment's answer did not match this version. Update both environments.",
        }),
    ),
  );

/**
 * The delegation service following one open task on the box, whose run there has ended and
 * whose thread answers `answer` (or `answer(input)` per read). `betaOff`: this machine's linked
 * environments are off, so a call (not a follower's read) is refused.
 */
const followTask = (answer: unknown, options: { readonly betaOff?: boolean } = {}) =>
  Effect.gen(function* () {
    const reads: Array<"read" | "reply-read"> = [];
    const answerTo = (input: { readonly afterPosition?: number }) =>
      typeof answer === "function" ? (answer as typeof olderPagedAnswer)(input) : answer;
    const completed = yield* Deferred.make<{ readonly status: string; readonly result: string }>();
    const task = {
      id: taskId,
      status: "running",
      result: null as string | null,
      startedAt: null,
      completedAt: null,
      remoteChild: {
        environmentId: box,
        threadId: childThreadId,
        label: "Box",
        runId,
        stoppedThere: undefined as boolean | undefined,
      },
    };

    const forwarding = Layer.mock(PeerForwarding.PeerForwarding)({
      waitForThread: () =>
        Effect.succeed({ threadId: childThreadId, runId, status: "completed", timedOut: false }),
      readThread: ((_scope: unknown, _environmentId: unknown, input: never) =>
        Effect.sync(() => reads.push("read")).pipe(
          Effect.andThen(decodeAnswer(OrchestratorMcpThreadReadResult, answerTo(input))),
        )) as never,
      call: ((
        _scope: unknown,
        tool: { readonly name: string; readonly successSchema: never },
        _environmentId: unknown,
        input: never,
      ) => {
        if (tool.name !== "t3_thread_read") return Effect.die(`unexpected call ${tool.name}`);
        if (options.betaOff) return Effect.fail(PeerForwarding.linkedEnvironmentsOff);
        return Effect.sync(() => reads.push("reply-read")).pipe(
          Effect.andThen(decodeAnswer(tool.successSchema, answerTo(input))),
        );
      }) as never,
    });

    const threads = Layer.mock(ThreadManagement.ThreadManagementService)({
      getThreadEventSequence: () => Effect.succeed(0) as never,
      getThreadRecords: (() =>
        Effect.sync(() => ({
          thread: { id: parentThreadId, providerInstanceId: instanceId },
          subagents: [structuredClone(task)],
        }))) as never,
      streamStoredEventsFrom: () => Stream.never,
      streamDomainEvents: Stream.never,
      dispatch: ((command: {
        readonly type: string;
        readonly status: string;
        readonly result: string;
      }) =>
        Effect.gen(function* () {
          if (command.type !== "delegated_task.remote.complete") {
            return yield* Effect.die(`unexpected command ${command.type}`);
          }
          task.status = command.status;
          task.result = command.result;
          task.remoteChild.stoppedThere = true;
          yield* Deferred.succeed(completed, { status: command.status, result: command.result });
          return { storedEvents: [] };
        })) as never,
    });

    const remote = yield* Layer.build(
      RemoteDelegation.layer.pipe(
        Layer.provide(
          Layer.mergeAll(
            forwarding,
            threads,
            Layer.mock(PeerLinks.PeerLinks)({ get: () => Effect.succeedNone }),
            Layer.mock(ProjectionStore.ProjectionStoreV2)({
              getOpenRemoteDelegatedTasks: Effect.succeed([{ parentThreadId, taskId }]),
            }),
            Layer.mock(ProjectService.ProjectService)({}),
            // Upstream's launch reads the providers to check an agent's mode pick.
            Layer.mock(ProviderRegistry.ProviderRegistry)({ getProviders: Effect.succeed([]) }),
            Layer.mock(RepositoryIdentityResolver.RepositoryIdentityResolver)({}),
            Layer.mock(ServerEnvironment.ServerEnvironment)({
              getEnvironmentId: Effect.succeed("environment-here" as EnvironmentId),
            }),
            Layer.succeed(SecureRandom.SecureRandom, {
              uuidV4: () => Effect.succeed("00000000-0000-4000-8000-000000000000"),
            } as never),
            // The features ask goes only with a profile narrower than `all`; none here.
            Layer.succeed(
              HttpClient.HttpClient,
              HttpClient.make(() => Effect.die("no features ask expected")),
            ),
          ),
        ),
      ),
    ).pipe(
      // Two retries, at once, before the follower gives up on the task.
      Effect.provideService(RemoteDelegation.FollowRetrySchedule, Schedule.recurs(2)),
      Effect.map(Context.get(RemoteDelegation.RemoteDelegation)),
    );
    yield* remote.start();
    return { completion: yield* Deferred.await(completed), reads };
  });

it.effect("this version's own answer is read once, as before", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const { completion, reads } = yield* followTask(currentAnswer);
      assert.deepStrictEqual(completion, { status: "completed", result: reply });
      assert.deepStrictEqual(reads, ["read"]);
    }),
  ),
);

it.effect("an older version's answer still completes the task with its reply", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const { completion, reads } = yield* followTask(olderAnswer);
      assert.deepStrictEqual(completion, { status: "completed", result: reply });
      assert.deepStrictEqual(reads, ["read", "reply-read"]);
    }),
  ),
);

it.effect("an older version's reply on its last page is read by the tail probe", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const { completion, reads } = yield* followTask(olderPagedAnswer);
      assert.deepStrictEqual(completion, { status: "completed", result: reply });
      // The first page and the tail page, each read again without the thread's other details.
      assert.deepStrictEqual(reads, ["read", "reply-read", "read", "reply-read"]);
    }),
  ),
);

it.effect.each([
  ["whose reply does not decode either", unreadableAnswer, {}],
  ["while linked environments are off here", olderAnswer, { betaOff: true }],
] as const)("an older version's answer %s fails the task as before", ([, answer, options]) =>
  Effect.scoped(
    Effect.gen(function* () {
      const { completion } = yield* followTask(answer, options);
      assert.strictEqual(completion.status, "failed");
      assert.include(completion.result, "failed repeatedly");
      // Never the second read's own refusal.
      assert.notInclude(completion.result, "Linked environments is off");
    }),
  ),
);
