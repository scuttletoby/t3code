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

import * as ProjectService from "../project/ProjectService.ts";
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

const shell = (id: ProjectId, repository: string | null): OrchestrationProjectShell => ({
  id,
  title: "Project",
  workspaceRoot: "/work/project",
  ...(repository === null
    ? {}
    : {
        repositoryIdentity: {
          canonicalKey: `github.com/${repository}`,
          locator: {
            source: "git-remote" as const,
            remoteName: "origin",
            remoteUrl: `https://github.com/${repository}.git`,
          },
          provider: SourceControlProviderKind.make("github"),
          displayName: repository,
        },
      }),
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
 */
const fakeHost = () => {
  const prs = new Map<string, PrState>();
  const reads: string[] = [];
  const projects = new Map<ProjectId, OrchestrationProjectShell>([
    [PROJECT, shell(PROJECT, "acme/widgets")],
    [BARE_PROJECT, shell(BARE_PROJECT, null)],
  ]);
  const find = (ref: PullRequestRef, what: string) =>
    Effect.suspend(() => {
      const name = `${ref.repository}#${ref.number}`;
      reads.push(`${what} ${ref.host} ${name} stale:${String(ref.allowStale)}`);
      const pr = prs.get(name);
      if (pr === undefined) return Effect.fail(providerError("not-found", "Could not resolve."));
      return pr.error === undefined ? Effect.succeed(pr) : Effect.fail(pr.error);
    });
  const layer = Layer.mergeAll(
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
  return { prs, reads, projects, layer };
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

  it.effect("a project without a remote needs the repository; a gone project ends the wait", () => {
    const host = fakeHost();
    return Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      host.prs.set("acme/widgets#7", { state: "open", headSha: HEAD_A });
      const filePath = yield* storePath;
      const service = yield* makeService(filePath, yield* makePrJudge());

      const refused = yield* refusal(service, prWait({ number: 7 }), BARE_PROJECT);
      assert.strictEqual(refused.code, "invalid");
      assert.include(refused.message, "Name the pull request's repository");
      assert.deepStrictEqual(host.reads, []);

      yield* register(service, prWait({ number: 7, until: "merged" }));
      host.projects.delete(PROJECT);
      yield* TestClock.adjust(SWEEP);
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 1 });
      const [ended] = yield* storedWaits(filePath);
      assert.strictEqual(ended?.result?.outcome, "check_error");
      assert.strictEqual(ended?.result?.detail, "The project of this wait is gone.");
    }).pipe(Effect.provide(Layer.merge(host.layer, ServerSettingsService.layerTest(ON))));
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
});
