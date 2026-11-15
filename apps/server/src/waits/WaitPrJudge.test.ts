import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import {
  type OrchestrationProjectShell,
  ProjectId,
  type PullRequestCheck,
  type PullRequestChecks,
  type PullRequestDetail,
  PullRequestOperationError,
  type PullRequestRef,
  PullRequestUnavailableError,
  type RepositoryIdentity,
  SourceControlProviderKind,
  ThreadId,
} from "@t3tools/contracts";
import type { WaitForInput, WaitRecord } from "@t3tools/contracts/waitFor";
import { PullRequestProviderError } from "@t3tools/source-control-core/server/PullRequestProvider";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import { TestClock } from "effect/testing";

import * as ProjectEnrichmentService from "../project/ProjectEnrichmentService.ts";
import * as ProjectFaviconResolver from "../project/ProjectFaviconResolver.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as RepositoryIdentityResolver from "../project/RepositoryIdentityResolver.ts";
import * as PullRequestService from "../pullRequest/PullRequestService.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import type { WaitDelivery } from "./WaitDelivery.ts";
import type { WaitJudge } from "./WaitJudges.ts";
import { makePrJudge } from "./WaitPrJudge.ts";
import * as WaitService from "./WaitService.ts";
import { WAIT_STORE_FILE } from "./WaitStore.ts";

const NOW = Date.parse("2026-11-15T12:00:00.000Z");
const MINUTE = 60_000;
/** The default `prSweepSeconds`. */
const SWEEP = 2 * MINUTE;
const THREAD = ThreadId.make("thread-a");
const PROJECT = ProjectId.make("project-a");
const BARE_PROJECT = ProjectId.make("project-bare");
const HEAD_A = "a".repeat(40);
const HEAD_B = "b".repeat(40);

const ON = { waitFor: { enabled: true } } as const;

const identityOf = (repository: string): RepositoryIdentity => ({
  canonicalKey: `github.com/${repository}`,
  locator: {
    source: "git-remote" as const,
    remoteName: "origin",
    remoteUrl: `https://github.com/${repository}.git`,
  },
  provider: SourceControlProviderKind.make("github"),
  displayName: repository,
});

/** `repository` null: the shell shows no remote (none, or not looked up yet). */
const shell = (id: ProjectId, repository: string | null): OrchestrationProjectShell => ({
  id,
  title: "Project",
  workspaceRoot: `/work/${id}`,
  ...(repository === null ? {} : { repositoryIdentity: identityOf(repository) }),
  defaultModelSelection: null,
  scripts: [],
  createdAt: "2026-11-01T00:00:00Z",
  updatedAt: "2026-11-01T00:00:00Z",
});

const check = (name: string, status: PullRequestCheck["status"]) =>
  ({ name, status }) as PullRequestCheck;

type PrState = {
  state: "open" | "closed" | "merged";
  headSha?: string;
  /** `null`: the host could not read them. */
  checks?: ReadonlyArray<PullRequestCheck> | null;
  /** Set: every read of this pull request fails with it. */
  error?: PullRequestService.PullRequestError;
};

const providerError = (reason: PullRequestProviderError["reason"], detail: string) =>
  new PullRequestOperationError({
    operation: "detail",
    detail,
    ...(reason === "not-found" ? { reason: "not-found" as const } : {}),
    cause: new PullRequestProviderError({
      provider: SourceControlProviderKind.make("github"),
      operation: "getChangeRequest",
      reason,
      detail,
    }),
  });

/**
 * A pull request host as T3's pull request service answers for it, without its caches: every
 * judge call reads it. `prs` is keyed `owner/repo#N` (lowercase, as targets are resolved).
 * `remotes` is what the remote lookup finds, by workspace root; a shell in `projects` shows a
 * remote only once the background lookup has run (`shell(id, null)` before that).
 */
const fakeHost = () => {
  const prs = new Map<string, PrState>();
  const reads: string[] = [];
  const lookups: string[] = [];
  const refreshes: string[] = [];
  const projects = new Map<ProjectId, OrchestrationProjectShell>([
    [PROJECT, shell(PROJECT, "acme/widgets")],
    [BARE_PROJECT, shell(BARE_PROJECT, null)],
  ]);
  const remotes = new Map<string, RepositoryIdentity | null>([
    [`/work/${PROJECT}`, identityOf("acme/widgets")],
    [`/work/${BARE_PROJECT}`, null],
  ]);
  const find = (ref: PullRequestRef, what: string) =>
    Effect.suspend(() => {
      const name = `${ref.repository}#${ref.number}`;
      reads.push(`${what} ${ref.host} ${name} stale:${String(ref.allowStale)}`);
      const pr = prs.get(name);
      if (pr === undefined) return Effect.fail(providerError("not-found", "Could not resolve."));
      return pr.error === undefined ? Effect.succeed(pr) : Effect.fail(pr.error);
    });
  const services = Layer.mergeAll(
    Layer.mock(PullRequestService.PullRequestService)({
      detail: (ref) =>
        find(ref, "detail").pipe(
          Effect.map(
            (pr) =>
              ({
                repository: ref.repository,
                number: ref.number,
                state: pr.state,
                ...(pr.headSha === undefined ? {} : { headSha: pr.headSha }),
              }) as PullRequestDetail,
          ),
        ),
      checks: (ref) =>
        find(ref, "checks").pipe(
          Effect.map((pr) =>
            pr.checks === null
              ? null
              : ({ state: pr.state, checks: pr.checks ?? [] } satisfies PullRequestChecks),
          ),
        ),
    }),
    Layer.mock(ProjectService.ProjectService)({
      getShell: (projectId) => Effect.succeed(Option.fromNullishOr(projects.get(projectId))),
    }),
  );
  const layer = Layer.merge(
    services,
    Layer.mock(ProjectEnrichmentService.ProjectEnrichmentService)({
      readRepositoryIdentity: (workspaceRoot) =>
        Effect.sync(() => (lookups.push(workspaceRoot), remotes.get(workspaceRoot) ?? null)),
      refreshRepositoryIdentity: (workspaceRoot) =>
        Effect.sync(() => void refreshes.push(workspaceRoot)),
    }),
  );
  return { prs, reads, lookups, refreshes, projects, remotes, services, layer };
};

/**
 * T3's own project enrichment over a remote lookup that timed out once: like the real resolver,
 * it then answers "none" from its cache for a minute unless asked to look again (`refresh`).
 */
const timedOutOnceRemote = (repository: string) => {
  const calls: string[] = [];
  let cachedNone = false;
  const resolver = Layer.succeed(RepositoryIdentityResolver.RepositoryIdentityResolver, {
    resolve: (workspaceRoot, options) =>
      Effect.sync(() => {
        const refresh = options?.refresh === true;
        calls.push(refresh ? "refresh" : "read");
        if (calls.length === 1) {
          cachedNone = true;
          return null;
        }
        if (cachedNone && !refresh) return null;
        cachedNone = false;
        return { ...identityOf(repository), rootPath: workspaceRoot };
      }),
  });
  const favicons = Layer.succeed(ProjectFaviconResolver.ProjectFaviconResolver, {
    invalidate: () => Effect.void,
    resolvePath: () => Effect.succeed(null),
  });
  const layer = Layer.effect(
    ProjectEnrichmentService.ProjectEnrichmentService,
    ProjectEnrichmentService.make(),
  ).pipe(Layer.provide(Layer.merge(resolver, favicons)));
  return { calls, layer };
};

/** Records each message; every thread is there. */
const recordingDelivery = () => {
  const sent: WaitService.WaitMessage[] = [];
  const delivery: WaitDelivery = {
    receiver: () => Effect.succeed({ type: "ok" }),
    deliver: (message) => Effect.sync(() => (sent.push(message), { type: "delivered" as const })),
  };
  return { sent, delivery };
};

const storePath = Effect.gen(function* () {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const root = yield* fs.makeTempDirectoryScoped({ prefix: "wait-pr-" });
  return path.join(root, WAIT_STORE_FILE);
});

const storedWaits = (filePath: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    return JSON.parse(yield* fs.readFileString(filePath)).waits as ReadonlyArray<WaitRecord>;
  });

const makeService = (filePath: string, judge: WaitJudge<"pr">, delivery?: WaitDelivery) =>
  WaitService.make({
    filePath,
    delivery: delivery ?? recordingDelivery().delivery,
    fileChildJudge: null,
    judges: { pr: judge },
  });

const prWait = (pr: NonNullable<WaitForInput["pr"]>): WaitForInput => ({ kind: "pr", pr });

const register = (
  service: WaitService.WaitService,
  input: WaitForInput,
  projectId: ProjectId = PROJECT,
) => service.register({ threadId: THREAD, projectId, input });

const refusal = (service: WaitService.WaitService, input: WaitForInput, projectId?: ProjectId) =>
  register(service, input, projectId).pipe(
    Effect.flip,
    Effect.map((error) => ({ code: error.code, message: error.message })),
  );

it.layer(NodeServices.layer)("WaitPrJudge", (it) => {
  it.effect("a pull request merged before the wait was made is met at once; nothing stored", () => {
    const host = fakeHost();
    return Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const fs = yield* FileSystem.FileSystem;
      host.prs.set("acme/widgets#7", { state: "merged", headSha: HEAD_A });
      const filePath = yield* storePath;
      const service = yield* makeService(filePath, yield* makePrJudge());

      const result = yield* register(service, prWait({ number: 7 }));
      assert.strictEqual(result.state, "met");
      if (result.state !== "met") return;
      assert.strictEqual(result.outcome, "merged");
      assert.isFalse(yield* fs.exists(filePath));
      // The project's repository and host; a fresh read, not a held stale one; no checks read.
      assert.deepStrictEqual(host.reads, ["detail github.com acme/widgets#7 stale:false"]);
    }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
  });

  it.effect("closed without a merge ends the wait as closed; a merged wait reads no checks", () => {
    const host = fakeHost();
    return Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_A, checks: [] });
      const filePath = yield* storePath;
      const service = yield* makeService(filePath, yield* makePrJudge());

      const result = yield* register(service, prWait({ number: 7, until: "merged" }));
      assert.strictEqual(result.state, "waiting");
      host.prs.set("acme/widgets#7", { state: "closed", headSha: HEAD_A });
      yield* TestClock.adjust(SWEEP);
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 1 });
      const [ended] = yield* storedWaits(filePath);
      assert.strictEqual(ended?.result?.outcome, "closed");
      assert.strictEqual(ended?.result?.detail, "Closed without a merge.");
      assert.isFalse(host.reads.some((read) => read.startsWith("checks")));
    }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
  });

  it.effect(
    "checks end only on two reads a sweep apart of one head; a force-push starts again",
    () => {
      const host = fakeHost();
      return Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const finished = [check("build", "success"), check("lint", "failure")];
        host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_A, checks: finished });
        const filePath = yield* storePath;
        const service = yield* makeService(filePath, yield* makePrJudge());

        const result = yield* register(service, prWait({ number: 7, repository: "Acme/Widgets" }));
        assert.strictEqual(result.state, "waiting");
        // Pushed again: the new head's checks look finished too, but only once so far.
        host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_B, checks: finished });
        yield* TestClock.adjust(SWEEP);
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 0 });

        yield* TestClock.adjust(SWEEP);
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 1 });
        const [ended] = yield* storedWaits(filePath);
        assert.strictEqual(ended?.result?.outcome, "failed");
        assert.strictEqual(ended?.result?.detail, "Head bbbbbbb. 1 of 2 failed: lint.");
        assert.isTrue(host.reads.every((read) => read.includes(" acme/widgets#7 ")));
      }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
    },
  );

  it.effect(
    "checks that cannot be read are a failed read, never met; none at all end later",
    () => {
      const host = fakeHost();
      return Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_A, checks: null });
        const filePath = yield* storePath;
        const service = yield* makeService(filePath, yield* makePrJudge());

        assert.strictEqual((yield* register(service, prWait({ number: 7 }))).state, "waiting");
        yield* TestClock.adjust(SWEEP);
        yield* service.judgeDue();
        const [unread] = yield* storedWaits(filePath);
        assert.strictEqual(unread?.state, "waiting");
        assert.strictEqual(unread?.lastError, "The checks could not be read.");

        host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_A, checks: [] });
        yield* TestClock.adjust(SWEEP);
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 0 });
        // No checks are met only after the no-checks grace (15 min), not after one sweep.
        yield* TestClock.adjust(SWEEP);
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 0 });
        yield* TestClock.adjust(14 * MINUTE);
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 1 });
        assert.strictEqual((yield* storedWaits(filePath))[0]?.result?.outcome, "no_checks");
      }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
    },
  );

  it.effect(
    "deleted or renamed away: refused when made; a stored wait ends with check_error at once",
    () => {
      const host = fakeHost();
      return Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const filePath = yield* storePath;
        const service = yield* makeService(filePath, yield* makePrJudge());

        const refused = yield* refusal(service, prWait({ number: 404 }));
        assert.strictEqual(refused.code, "invalid");
        assert.include(refused.message, "acme/widgets#404 was not found");
        assert.include(refused.message, "renamed");

        // A rename the host redirects still answers under the old name.
        host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_A });
        assert.strictEqual(
          (yield* register(service, prWait({ number: 7, until: "merged" }))).state,
          "waiting",
        );
        host.prs.delete("acme/widgets#7");
        yield* TestClock.adjust(SWEEP);
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 1 });
        const [ended] = yield* storedWaits(filePath);
        assert.strictEqual(ended?.result?.outcome, "check_error");
        assert.include(ended?.result?.detail ?? "", "was not found");
      }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
    },
  );

  it.effect(
    "rate limited or host down: kept waiting, then check_error after the grace, joined per thread, never met",
    () => {
      const host = fakeHost();
      const send = recordingDelivery();
      return Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_A });
        host.prs.set("acme/widgets#8", { state: "open", headSha: HEAD_A });
        const filePath = yield* storePath;
        const service = yield* makeService(filePath, yield* makePrJudge(), send.delivery);
        yield* register(service, prWait({ number: 7, until: "merged" }));
        yield* register(service, prWait({ number: 8, until: "merged" }));

        // The host answers with its rate limit for one, and is down for the other.
        host.prs.set("acme/widgets#7", {
          state: "merged",
          error: providerError("rate-limited", "API rate limit exceeded"),
        });
        host.prs.set("acme/widgets#8", {
          state: "merged",
          error: providerError("failed", "HTTP 502 from the host"),
        });
        yield* TestClock.adjust(SWEEP);
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 2, ended: 0 });
        const errors = (yield* storedWaits(filePath)).map((wait) => wait.lastError);
        assert.deepStrictEqual(errors, [
          "The host is limiting reads of acme/widgets#7 (rate limit); it is read again later.",
          "Pull request acme/widgets#8 could not be read: HTTP 502 from the host",
        ]);

        for (let minute = 0; minute < 16; minute += 2) {
          yield* TestClock.adjust(SWEEP);
          yield* service.judgeDue();
        }
        const ended = yield* storedWaits(filePath);
        assert.deepStrictEqual(
          ended.map((wait) => wait.result?.outcome),
          ["check_error", "check_error"],
        );
        yield* service.deliverDue();
        assert.strictEqual(send.sent.length, 1);
        assert.match(send.sent[0]!.messageId, /:joined:2$/u);
      }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
    },
  );

  it.effect("a missing login refuses a new wait but is only a failed read of a stored one", () => {
    const host = fakeHost();
    return Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const loggedOut = new PullRequestUnavailableError({ reason: "cli-unauthenticated" });
      host.prs.set("acme/widgets#7", { state: "open", error: loggedOut });
      const filePath = yield* storePath;
      const service = yield* makeService(filePath, yield* makePrJudge());

      const refused = yield* refusal(service, prWait({ number: 7 }));
      assert.deepStrictEqual(refused, {
        code: "invalid",
        message: "This host has no working credentials.",
      });

      host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_A });
      yield* register(service, prWait({ number: 7, until: "merged" }));
      host.prs.set("acme/widgets#7", { state: "open", error: loggedOut });
      yield* TestClock.adjust(SWEEP);
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 0 });
      assert.strictEqual(
        (yield* storedWaits(filePath))[0]?.lastError,
        "This host has no working credentials.",
      );
    }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
  });

  it.effect(
    "a project without a remote refuses a new wait in wait_for's words; a gone project ends the wait",
    () => {
      const host = fakeHost();
      return Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_A });
        const filePath = yield* storePath;
        const service = yield* makeService(filePath, yield* makePrJudge());

        // Naming the repository does not help: the host comes from the project's remote. The
        // refusal never says "pass url" or "pass host" (fields link_pull_request has, wait_for not).
        for (const input of [
          prWait({ number: 7 }),
          prWait({ number: 7, repository: "acme/widgets" }),
        ]) {
          const refused = yield* refusal(service, input, BARE_PROJECT);
          assert.strictEqual(refused.code, "invalid");
          assert.include(refused.message, "has no remote on a pull request host T3 knows");
          assert.notMatch(refused.message, /\bpass\b/iu);
        }
        assert.deepStrictEqual(host.reads, []);
        // Each new wait looked again past the cached "none" before it was refused.
        const bare = `/work/${BARE_PROJECT}`;
        assert.deepStrictEqual(host.lookups, [bare, bare, bare, bare]);
        assert.deepStrictEqual(host.refreshes, [bare, bare]);

        yield* register(service, prWait({ number: 7, until: "merged" }));
        host.projects.delete(PROJECT);
        yield* TestClock.adjust(SWEEP);
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 1 });
        const [ended] = yield* storedWaits(filePath);
        assert.strictEqual(ended?.result?.outcome, "check_error");
        assert.strictEqual(ended?.result?.detail, "The project of this wait is gone.");
      }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
    },
  );

  it.effect(
    "after a restart a stored wait waits for its project's remote lookup; it is never ended by it",
    () => {
      const host = fakeHost();
      return Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_A });
        const filePath = yield* storePath;
        const before = yield* makeService(filePath, yield* makePrJudge());
        // One wait names its repository, one takes the project's: both need the project's host.
        yield* register(before, prWait({ number: 7, until: "merged" }));
        yield* register(before, prWait({ number: 7, repository: "acme/widgets", until: "merged" }));
        assert.deepStrictEqual(host.lookups, [], "a shell with its remote needs no lookup");

        // Restart: the project shell shows no remote until its background lookup ends.
        host.projects.set(PROJECT, shell(PROJECT, null));
        const after = yield* makeService(filePath, yield* makePrJudge());
        yield* TestClock.adjust(SWEEP);
        assert.deepStrictEqual(yield* after.judgeDue(), { judged: 2, ended: 0 });
        const waiting = yield* storedWaits(filePath);
        assert.deepStrictEqual(
          waiting.map((wait) => [wait.state, wait.lastError ?? null]),
          [
            ["waiting", null],
            ["waiting", null],
          ],
        );
        assert.deepStrictEqual(host.lookups, [`/work/${PROJECT}`, `/work/${PROJECT}`]);

        host.prs.set("acme/widgets#7", { state: "merged", headSha: HEAD_A });
        yield* TestClock.adjust(SWEEP);
        assert.deepStrictEqual(yield* after.judgeDue(), { judged: 2, ended: 2 });
        assert.deepStrictEqual(
          (yield* storedWaits(filePath)).map((wait) => wait.result?.outcome),
          ["merged", "merged"],
        );
      }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
    },
  );

  it.effect("a new wait made before the project's remote is looked up waits for the lookup", () => {
    const host = fakeHost();
    return Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_A });
      host.projects.set(PROJECT, shell(PROJECT, null));
      const filePath = yield* storePath;
      const service = yield* makeService(filePath, yield* makePrJudge());
      assert.strictEqual((yield* register(service, prWait({ number: 7 }))).state, "waiting");
      assert.deepStrictEqual(host.reads.slice(0, 1), [
        "detail github.com acme/widgets#7 stale:false",
      ]);
    }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
  });

  it.effect(
    "a stored wait whose project's remote is gone is a failed read: check_error only after the grace",
    () => {
      const host = fakeHost();
      return Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        host.prs.set("acme/widgets#8", { state: "open", headSha: HEAD_A });
        const filePath = yield* storePath;
        const service = yield* makeService(filePath, yield* makePrJudge());
        yield* register(service, prWait({ number: 8, until: "merged" }));
        const readsAtRegistration = host.reads.length;

        // The lookup finds no remote now (removed, or `git remote` timed out on a busy host).
        host.projects.set(PROJECT, shell(PROJECT, null));
        host.remotes.set(`/work/${PROJECT}`, null);
        host.prs.set("acme/widgets#8", { state: "merged", headSha: HEAD_A });
        yield* TestClock.adjust(SWEEP);
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 0 });
        const [failed] = yield* storedWaits(filePath);
        assert.strictEqual(failed?.state, "waiting");
        assert.include(failed?.lastError ?? "", "has no remote on a pull request host T3 knows");
        assert.include(failed?.lastError ?? "", "looked up again");

        for (let minute = 0; minute < 16; minute += 2) {
          yield* TestClock.adjust(SWEEP);
          yield* service.judgeDue();
        }
        const [ended] = yield* storedWaits(filePath);
        assert.strictEqual(ended?.result?.outcome, "check_error");
        // Never met: the pull request merged meanwhile, but nothing was read without a host.
        assert.strictEqual(host.reads.length, readsAtRegistration);
      }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
    },
  );

  it.effect("a head is settled only by two reads of its own checks", () => {
    // T3 reads the head and the checks from two caches of different ages (60 s and 15 s), so one
    // read can pair the new head B with the old head's finished checks.
    const host = fakeHost();
    let checksOf: ReadonlyArray<PullRequestCheck> = [];
    const mixed = Layer.mock(PullRequestService.PullRequestService)({
      detail: (ref) =>
        Effect.sync(
          () =>
            ({
              repository: ref.repository,
              number: ref.number,
              state: "open",
              headSha: HEAD_B,
            }) as PullRequestDetail,
        ),
      checks: () => Effect.sync(() => ({ state: "open" as const, checks: checksOf })),
    });
    return Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      checksOf = [check("ci", "pending")];
      const filePath = yield* storePath;
      const service = yield* makeService(filePath, yield* makePrJudge());
      assert.strictEqual((yield* register(service, prWait({ number: 7 }))).state, "waiting");
      // First sweep: the head is B, the checks are still head A's, finished.
      checksOf = [check("ci", "success")];
      yield* TestClock.adjust(SWEEP);
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 0 });
      // Second sweep: B's own checks, read once: only its fast check is listed so far.
      checksOf = [check("title-lint", "success")];
      yield* TestClock.adjust(SWEEP);
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 0 });
      // B's main check is queued and finishes; two reads of that list settle it.
      checksOf = [check("title-lint", "success"), check("ci", "failure")];
      yield* TestClock.adjust(SWEEP);
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 0 });
      yield* TestClock.adjust(SWEEP);
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 1 });
      const [ended] = yield* storedWaits(filePath);
      assert.strictEqual(ended?.result?.outcome, "failed");
      assert.strictEqual(ended?.result?.detail, "Head bbbbbbb. 1 of 2 failed: ci.");
    }).pipe(Effect.provide(Layer.mergeAll(host.layer, mixed, ServerSettingsService.layerTest(ON))));
  });

  it.effect("after a restart the settle rule takes two fresh reads again", () => {
    const host = fakeHost();
    return Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      host.prs.set("acme/widgets#7", {
        state: "open",
        headSha: HEAD_A,
        checks: [check("build", "success")],
      });
      const filePath = yield* storePath;
      const before = yield* makeService(filePath, yield* makePrJudge());
      assert.strictEqual((yield* register(before, prWait({ number: 7 }))).state, "waiting");

      // A new server: a new judge with nothing seen, reading the same store.
      const after = yield* makeService(filePath, yield* makePrJudge());
      yield* TestClock.adjust(SWEEP);
      assert.deepStrictEqual(yield* after.judgeDue(), { judged: 1, ended: 0 });
      yield* TestClock.adjust(SWEEP);
      assert.deepStrictEqual(yield* after.judgeDue(), { judged: 1, ended: 1 });
      const [ended] = yield* storedWaits(filePath);
      assert.strictEqual(ended?.result?.outcome, "passed");
    }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
  });

  it.effect("with waits off nothing is read and nothing is written", () => {
    const host = fakeHost();
    return Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const fs = yield* FileSystem.FileSystem;
      host.prs.set("acme/widgets#7", { state: "merged", headSha: HEAD_A });
      const filePath = yield* storePath;
      const service = yield* makeService(filePath, yield* makePrJudge());

      assert.strictEqual((yield* refusal(service, prWait({ number: 7 }))).code, "waits_off");
      yield* TestClock.adjust(SWEEP);
      yield* service.runDue();
      assert.deepStrictEqual(host.reads, []);
      assert.isFalse(yield* fs.exists(filePath));
    }).pipe(
      Effect.provide(
        Layer.merge(host.layer, ServerSettingsService.layerTest({ waitFor: { enabled: false } })),
      ),
    );
  });
  it.effect(
    "a new wait looks past a timed-out remote lookup: it is not refused as having no remote",
    () => {
      const host = fakeHost();
      const remote = timedOutOnceRemote("acme/widgets");
      return Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_A });
        host.projects.set(PROJECT, shell(PROJECT, null));
        // Something read the remote first and its `git` lookup timed out: "none" is cached.
        const enrichment = yield* ProjectEnrichmentService.ProjectEnrichmentService;
        assert.isNull(yield* enrichment.readRepositoryIdentity(`/work/${PROJECT}`));
        const filePath = yield* storePath;
        const service = yield* makeService(filePath, yield* makePrJudge());

        yield* TestClock.adjust(10_000);
        const result = yield* register(service, prWait({ number: 7 }));
        assert.strictEqual(result.state, "waiting");
        assert.deepStrictEqual(remote.calls, ["read", "refresh", "read"]);
        assert.deepStrictEqual(host.reads.slice(0, 1), [
          "detail github.com acme/widgets#7 stale:false",
        ]);
        // Asked again: the same wait, and the remote, now known, is not looked up past the cache.
        assert.strictEqual((yield* register(service, prWait({ number: 7 }))).state, "waiting");
        assert.deepStrictEqual(remote.calls, ["read", "refresh", "read"]);
      }).pipe(
        Effect.provide(
          Layer.mergeAll(host.services, remote.layer, ServerSettingsService.layerTest(ON)),
        ),
      );
    },
  );

  it.effect(
    "a wait keeps the repository and host of its first read: a changed remote never ends it from another pull request",
    () => {
      const host = fakeHost();
      return Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_A });
        const filePath = yield* storePath;
        const service = yield* makeService(filePath, yield* makePrJudge());
        const made = yield* register(service, prWait({ number: 7, until: "merged" }));
        assert.strictEqual(made.state, "waiting");
        const [stored] = yield* storedWaits(filePath);
        assert.deepStrictEqual(stored?.spec, {
          number: 7,
          repository: "acme/widgets",
          host: "github.com",
          until: "merged",
        });

        // The project's remote flips to upstream, where the same number is merged.
        host.projects.set(PROJECT, shell(PROJECT, "upstream/widgets"));
        host.remotes.set(`/work/${PROJECT}`, identityOf("upstream/widgets"));
        host.prs.set("upstream/widgets#7", { state: "merged", headSha: HEAD_B });
        yield* TestClock.adjust(SWEEP);
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 0 });
        assert.isFalse(host.reads.some((read) => read.includes("upstream/")));
        // Asking the same again finds this wait (its key is the asked one), not a new one.
        const again = yield* register(service, prWait({ number: 7, until: "merged" }));
        assert.strictEqual(
          again.state === "waiting" ? again.waitId : null,
          made.state === "waiting" ? made.waitId : "",
        );

        host.prs.set("acme/widgets#7", { state: "merged", headSha: HEAD_A });
        yield* TestClock.adjust(SWEEP);
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 1 });
        assert.strictEqual((yield* storedWaits(filePath))[0]?.result?.outcome, "merged");
      }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
    },
  );

  it.effect(
    "a wait stored before the pin gets it at its first read after the upgrade, and keeps it after a restart",
    () => {
      const host = fakeHost();
      return Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const fs = yield* FileSystem.FileSystem;
        host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_A });
        host.prs.set("acme/widgets#8", { state: "open", headSha: HEAD_A });
        const filePath = yield* storePath;
        const before = yield* makeService(filePath, yield* makePrJudge());
        yield* register(before, prWait({ number: 7, until: "merged" }));
        yield* register(before, prWait({ number: 8, repository: "Acme/Widgets", until: "merged" }));
        // As an older build stored them: only what was asked, no host and no resolved repository.
        const file = JSON.parse(yield* fs.readFileString(filePath));
        file.waits[0].spec = { number: 7, until: "merged" };
        file.waits[1].spec = { number: 8, repository: "Acme/Widgets", until: "merged" };
        yield* fs.writeFileString(filePath, JSON.stringify(file));

        const after = yield* makeService(filePath, yield* makePrJudge());
        yield* TestClock.adjust(SWEEP);
        assert.deepStrictEqual(yield* after.judgeDue(), { judged: 2, ended: 0 });
        assert.deepStrictEqual(
          (yield* storedWaits(filePath)).map((wait) => wait.spec),
          [
            { number: 7, repository: "acme/widgets", host: "github.com", until: "merged" },
            { number: 8, repository: "acme/widgets", host: "github.com", until: "merged" },
          ],
        );

        // Restart, and the remote flips: the pinned waits still read acme/widgets.
        host.projects.set(PROJECT, shell(PROJECT, "upstream/widgets"));
        host.remotes.set(`/work/${PROJECT}`, identityOf("upstream/widgets"));
        host.prs.set("upstream/widgets#7", { state: "merged", headSha: HEAD_B });
        const restarted = yield* makeService(filePath, yield* makePrJudge());
        yield* TestClock.adjust(SWEEP);
        assert.deepStrictEqual(yield* restarted.judgeDue(), { judged: 2, ended: 0 });
        assert.isFalse(host.reads.some((read) => read.includes("upstream/")));
      }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
    },
  );

  it.effect("a pin never overwrites a wait cancelled while it was read", () => {
    const host = fakeHost();
    return Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_A });
      const filePath = yield* storePath;
      const fs = yield* FileSystem.FileSystem;
      const first = yield* makeService(filePath, yield* makePrJudge());
      const made = yield* register(first, prWait({ number: 7, until: "merged" }));
      const waitId = made.state === "waiting" ? made.waitId : "";
      // Stored by an older build, without the pin.
      const file = JSON.parse(yield* fs.readFileString(filePath));
      file.waits[0].spec = { number: 7, until: "merged" };
      yield* fs.writeFileString(filePath, JSON.stringify(file));

      // The read sees the wait waiting; it is cancelled before the read's pin is written.
      const prJudge = yield* makePrJudge();
      const holder: { service?: WaitService.WaitService } = {};
      const cancelFirst: WaitJudge<"pr"> = (target, context) =>
        prJudge(target, {
          ...context,
          pin: (next) =>
            holder
              .service!.cancel({ projectId: PROJECT, waitId })
              .pipe(Effect.ignore, Effect.andThen(context.pin!(next))),
        });
      const service = yield* makeService(filePath, cancelFirst);
      holder.service = service;
      yield* TestClock.adjust(SWEEP);
      yield* service.judgeDue();
      const [cancelled] = yield* storedWaits(filePath);
      assert.strictEqual(cancelled?.state, "cancelled");
      assert.deepStrictEqual(cancelled?.spec, { number: 7, until: "merged" });
    }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
  });
});
