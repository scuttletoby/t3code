/**
 * Fork (waits): the `pr` judge. It reads through T3's own pull request service (host tools, logins
 * and caches of link_pull_request and watch_pull_request) and judges with `judgePrChecks`. The
 * settle rule's "seen" head is kept in memory only: after a restart it takes two fresh reads.
 */
import type { PullRequestRef } from "@t3tools/contracts";
import { WaitPrSpec } from "@t3tools/contracts/waitFor";
import { type Judgement, judgePrChecks, type PrSeen } from "@t3tools/shared/waitFor";
import { PullRequestProviderError } from "@t3tools/source-control-core/server/PullRequestProvider";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Result from "effect/Result";
import * as Schema from "effect/Schema";

import * as ProjectEnrichmentService from "../project/ProjectEnrichmentService.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as PullRequestService from "../pullRequest/PullRequestService.ts";
import { type PullRequestTargetError, resolveTarget } from "../pullRequest/pullRequestTarget.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import { sourceControlDefinitions } from "../sourceControl/sourceControlDefinitions.ts";
import type { WaitJudge } from "./WaitJudges.ts";

/** A pull request's seen head is forgotten when no wait has read it for this long. */
const FORGET_SEEN_MS = 24 * 60 * 60_000;
const MESSAGE_MAX = 300;

const isProviderError = Schema.is(PullRequestProviderError);

const permanent = (message: string): Judgement => ({ type: "failed", message, permanent: true });
const transient = (message: string): Judgement => ({ type: "failed", message, permanent: false });
const clip = (text: string) =>
  text.length > MESSAGE_MAX ? `${text.slice(0, MESSAGE_MAX - 1)}…` : text;

/**
 * wait_for's own words for a target it cannot build (link_pull_request's say "pass url", a field
 * wait_for does not have). Each comes from the project's remote: it refuses a new wait, but for a
 * stored one it is a failed read, retried and reported after the error grace, since a remote can
 * be changed back and a remote lookup that timed out reads as none.
 */
const NO_REMOTE =
  "This thread's project has no remote on a pull request host T3 knows, so the host to read is unknown. Wait from a thread whose project's remote is on the pull request's host.";
const NO_REPOSITORY =
  "T3 cannot tell this project's repository from its remote. Name the pull request's repository.";
const NO_URL_SHAPE =
  "This thread's project is on a host whose pull requests T3 cannot find by repository and number, so a pull request wait cannot read them.";

function remoteProblem(message: string, registering: boolean): Judgement {
  return registering
    ? permanent(message)
    : transient(`${message} The project's remote is looked up again at the next read.`);
}

function targetProblem(error: PullRequestTargetError, registering: boolean): Judgement {
  switch (error._tag) {
    case "PullRequestHostRequiredError":
      return remoteProblem(NO_REMOTE, registering);
    case "PullRequestUrlUnavailableError":
      return remoteProblem(NO_URL_SHAPE, registering);
    // A wait always has repository and number and never a url, so these cannot happen.
    case "PullRequestTargetIncompleteError":
    case "PullRequestUrlInvalidError":
      return permanent("The pull request wait has no repository and number to read.");
  }
}

/**
 * What a failed read means; never met. Not found and a host without pull request reads are
 * permanent. A missing tool or login refuses a new wait, but for a stored one it is a failed read
 * like a rate limit or a host that is down: retried, and reported after the error grace.
 */
function failedRead(
  error: PullRequestService.PullRequestError,
  name: string,
  registering: boolean,
): Judgement {
  if (error._tag === "PullRequestUnavailableError") {
    return error.reason === "provider-unsupported" || registering
      ? permanent(error.message)
      : transient(error.message);
  }
  const provider = isProviderError(error.cause) ? error.cause : undefined;
  if (error.reason === "not-found" || provider?.reason === "not-found") {
    return permanent(
      `Pull request ${name} was not found: it may be deleted, its repository renamed or moved, or not readable with this host's login. Wait again with its current repository.`,
    );
  }
  if (provider?.reason === "rate-limited") {
    return transient(`The host is limiting reads of ${name} (rate limit); it is read again later.`);
  }
  if (registering && (provider?.reason === "forbidden" || provider?.reason === "unauthenticated")) {
    return permanent(clip(`Pull request ${name} cannot be read: ${error.detail}`));
  }
  return transient(clip(`Pull request ${name} could not be read: ${error.detail}`));
}

const isPrSpec = Schema.is(WaitPrSpec);

export const makePrJudge = Effect.fn("WaitPrJudge.make")(function* () {
  const settingsService = yield* ServerSettingsService;
  const projects = yield* ProjectService.ProjectService;
  const enrichment = yield* ProjectEnrichmentService.ProjectEnrichmentService;
  const pullRequests = yield* PullRequestService.PullRequestService;
  /** The settle rule's memory, by pull request: it is about the head, not about one wait. */
  const seenByPr = new Map<string, { readonly seen: PrSeen; readonly at: number }>();

  const read = Effect.fn("WaitPrJudge.read")(function* (ref: PullRequestRef, withChecks: boolean) {
    const detail = yield* pullRequests.detail(ref);
    const checks = withChecks && detail.state === "open" ? yield* pullRequests.checks(ref) : null;
    return { detail, checks };
  });

  const judge: WaitJudge<"pr"> = Effect.fn("WaitPrJudge.pr")(function* ({ spec }, context) {
    const registering = context.waitId === null;
    const settings = yield* settingsService.getSettings.pipe(Effect.option);
    if (Option.isNone(settings)) return transient("The settings cannot be read.");
    // The project the wait was registered in gives the host, the login and the default repository.
    const project = yield* projects.getShell(context.projectId).pipe(Effect.option);
    if (Option.isNone(project)) return transient("The project cannot be read now.");
    if (Option.isNone(project.value)) return permanent("The project of this wait is gone.");
    // A project shell shows only a remote already looked up: after a server start or a project
    // update it has none until a background lookup ends. Wait for that lookup (a cached
    // `git remote`), as T3's pull request service does, so "not looked up yet" never reads as
    // "no remote".
    const shell = project.value.value;
    let identity =
      shell.repositoryIdentity ?? (yield* enrichment.readRepositoryIdentity(shell.workspaceRoot));
    // A cached "none" may be a `git remote` lookup that timed out, kept for a minute: a
    // new wait looks again past that cache before it is refused. A stored wait only retries.
    if (identity === null && registering) {
      yield* enrichment.refreshRepositoryIdentity(shell.workspaceRoot);
      identity = yield* enrichment.readRepositoryIdentity(shell.workspaceRoot);
    }
    if (identity === null) return remoteProblem(NO_REMOTE, registering);
    const withRemote = { ...shell, repositoryIdentity: identity };
    const repository = spec.repository ?? sourceControlDefinitions.repositorySelector(identity);
    if (repository === null) return remoteProblem(NO_REPOSITORY, registering);
    // A wait keeps the host and repository of its first read: a later remote change (a
    // fork's remote flipped to upstream) must not end it from another pull request's number.
    const target = yield* Effect.result(
      resolveTarget(
        {
          repository,
          number: spec.number,
          ...(spec.host === undefined ? {} : { host: spec.host }),
        },
        withRemote,
      ),
    );
    if (Result.isFailure(target)) return targetProblem(target.failure, registering);
    const ref: PullRequestRef = {
      projectId: context.projectId,
      host: target.success.host,
      repository: target.success.repository,
      number: target.success.number,
      allowStale: false,
    };
    const pinnedSpec = {
      ...spec,
      repository: target.success.repository,
      host: target.success.host,
    };
    // Pinned only when it fits the stored format: a remote name too long to store stays
    // unpinned (read as before) instead of making the stored wait unreadable after a restart.
    if (spec.host === undefined && context.pin !== undefined && isPrSpec(pinnedSpec)) {
      yield* context.pin({ kind: "pr", spec: pinnedSpec });
    }
    const name = `${ref.repository}#${ref.number}`;
    const result = yield* Effect.result(read(ref, spec.until === "checks"));
    if (Result.isFailure(result)) return failedRead(result.failure, name, registering);

    const key = `${ref.projectId} ${ref.host} ${ref.repository}#${ref.number}`;
    for (const [held, entry] of seenByPr) {
      if (context.now - entry.at >= FORGET_SEEN_MS) seenByPr.delete(held);
    }
    const { detail, checks } = result.success;
    const { seen, ...judgement } = judgePrChecks({
      detail,
      checks,
      until: spec.until,
      seen: seenByPr.get(key)?.seen ?? null,
      now: context.now,
      settings: settings.value.waitFor,
      // Fork (review rules): a lens_start {afterCi} wait reads only the config's checks at its head.
      ...(spec.checks === undefined && spec.head === undefined
        ? {}
        : {
            only: {
              ...(spec.checks === undefined ? {} : { checks: spec.checks }),
              ...(spec.head === undefined ? {} : { head: spec.head }),
            },
          }),
    });
    // Only a checks read says anything about the head's checks; a `merged` wait leaves it be.
    if (spec.until === "checks") {
      if (seen === null) seenByPr.delete(key);
      else seenByPr.set(key, { seen, at: context.now });
    }
    return judgement;
  });
  return judge;
});
