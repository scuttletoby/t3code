/**
 * Fork: Antigravity limits from the CLI's own `/usage` report, never Google's quota API (it can
 * flag the account). Snapshots carry the last reading and, after a failed run, a short reason.
 * One run at a time: at most every 10 minutes, or a minute after the last on a refresh; 30 s cap.
 */
import type { AntigravitySettings, ServerProvider } from "@t3tools/contracts";
import type { ManagedServerProvider } from "@t3tools/provider-core/server/snapshot";
import { spawnAndCollect } from "@t3tools/provider-core/server/snapshotProbe";
import {
  makeUnavailableUsageLimits,
  makeUsageLimits,
} from "@t3tools/provider-core/server/usageLimits";
import * as Clock from "effect/Clock";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as PubSub from "effect/PubSub";
import * as Ref from "effect/Ref";
import * as Scope from "effect/Scope";
import * as Semaphore from "effect/Semaphore";
import * as Stream from "effect/Stream";
import * as ChildProcess from "effect/process/ChildProcess";
import * as ChildProcessSpawner from "effect/process/ChildProcessSpawner";

import {
  MESSAGES,
  parseUsageReport,
  USAGE_ARGS,
  type UsageOutcome,
  versionIssue,
} from "./usageReport.ts";

const SCHEDULED_GAP_MS = 10 * 60_000;
const ON_DEMAND_GAP_MS = 60_000;
const TICK = "1 minute";
const RUN_TIMEOUT = "30 seconds";

interface LimitsState {
  readonly reading?: { readonly checkedAt: string; readonly limits: ServerProvider["usageLimits"] };
  readonly failure?: { readonly checkedAt: string; readonly message: string };
  readonly stopped?: boolean;
  readonly lastStartedAt?: number;
}

function withLimits(provider: ServerProvider, state: LimitsState): ServerProvider {
  if (!provider.enabled || provider.auth.status !== "authenticated") return provider;
  const { reading, failure } = state;
  if (reading?.limits) {
    const limits = failure
      ? {
          ...reading.limits,
          unavailable: { reason: "probeFailed" as const, message: failure.message },
        }
      : reading.limits;
    return { ...provider, usageLimits: limits };
  }
  if (failure) {
    return {
      ...provider,
      usageLimits: makeUnavailableUsageLimits({
        checkedAt: failure.checkedAt,
        reason: "probeFailed",
        message: failure.message,
      }),
    };
  }
  return provider;
}

/** Wraps `base` so its snapshots carry the limits `run` reports. Runs live in the current scope. */
export const makeAntigravityLimits = Effect.fn("ForkAntigravityLimits.make")(function* (input: {
  readonly base: ManagedServerProvider;
  readonly run: Effect.Effect<UsageOutcome>;
}) {
  const { base } = input;
  const scope = yield* Effect.scope;
  const state = yield* Ref.make<LimitsState>({});
  const changes = yield* PubSub.unbounded<void>();
  const permit = yield* Semaphore.make(1);

  const eligible = base.getSnapshot.pipe(
    Effect.map(
      (snapshot) =>
        snapshot.enabled && snapshot.installed && snapshot.auth.status === "authenticated",
    ),
  );
  const runIfDue = Effect.fn("ForkAntigravityLimits.runIfDue")(function* (gapMs: number) {
    const now = yield* Clock.currentTimeMillis;
    const current = yield* Ref.get(state);
    if (current.stopped) return;
    if (current.lastStartedAt !== undefined && now - current.lastStartedAt < gapMs) return;
    if (!(yield* eligible)) return;
    yield* Ref.update(state, (previous) => ({ ...previous, lastStartedAt: now }));
    const outcome = yield* input.run;
    const checkedAt = DateTime.formatIso(yield* DateTime.now);
    yield* Ref.update(state, (previous) =>
      outcome._tag === "reading"
        ? {
            lastStartedAt: now,
            reading: {
              checkedAt,
              limits: makeUsageLimits({ checkedAt, windows: outcome.windows }),
            },
          }
        : {
            ...previous,
            failure: { checkedAt, message: outcome.message },
            ...(outcome.stop ? { stopped: true } : {}),
          },
    );
    yield* PubSub.publish(changes, undefined);
  });
  // A refresh or a timer tick that finds a run going leaves it be; refreshUsage waits for it.
  const attempt = (gapMs: number) =>
    permit.withPermitsIfAvailable(1)(runIfDue(gapMs)).pipe(Effect.asVoid);
  const overlay = (snapshot: ServerProvider) =>
    Ref.get(state).pipe(Effect.map((current) => withLimits(snapshot, current)));

  yield* Effect.sleep(TICK).pipe(
    Effect.andThen(attempt(SCHEDULED_GAP_MS)),
    Effect.forever,
    Effect.forkIn(scope),
  );

  return {
    ...base,
    getSnapshot: base.getSnapshot.pipe(Effect.flatMap(overlay)),
    refresh: base.refresh.pipe(
      Effect.tap(() => attempt(ON_DEMAND_GAP_MS).pipe(Effect.forkIn(scope))),
      Effect.flatMap(overlay),
    ),
    refreshUsage: permit.withPermits(1)(runIfDue(SCHEDULED_GAP_MS)),
    streamChanges: Stream.merge(
      base.streamChanges,
      Stream.fromPubSub(changes).pipe(Stream.mapEffect(() => base.getSnapshot)),
    ).pipe(Stream.mapEffect(overlay)),
  } satisfies ManagedServerProvider;
});

interface SpawnPlan {
  readonly executable: { readonly version: string | null };
  readonly spawn: {
    readonly command: string;
    readonly cwd?: string;
    readonly env?: NodeJS.ProcessEnv;
  };
}

type PrepareSpawn<E> = (
  cwd: string,
  baseEnv: NodeJS.ProcessEnv,
) => Effect.Effect<SpawnPlan, E, Scope.Scope>;

/** One `/usage` run through the driver's own spawn setup: its executable, environment and temp. */
export const makeCliRun = Effect.fn("ForkAntigravityLimits.cliRun")(
  function* <E>(
    prepareSpawn: PrepareSpawn<E>,
    processEnvironment: NodeJS.ProcessEnv,
    _timeout: Duration.Input = RUN_TIMEOUT,
  ) {
    const { executable, spawn } = yield* prepareSpawn(".", processEnvironment);
    const issue = versionIssue(executable.version);
    if (issue !== null) return { _tag: "failed", message: issue } satisfies UsageOutcome;
    // Run inside the per-run temp folder the driver made (and removes): no project folder involved.
    const cwd = spawn.env?.TMPDIR ?? spawn.env?.TEMP ?? spawn.cwd;
    const result = yield* spawnAndCollect(
      spawn.command,
      ChildProcess.make(spawn.command, USAGE_ARGS, {
        ...(cwd ? { cwd } : {}),
        ...(spawn.env ? { env: spawn.env } : {}),
        extendEnv: false,
      }),
    );
    return parseUsageReport(result.stdout, result.stderr);
  },
  (effect, _prepareSpawn, _environment, timeout = RUN_TIMEOUT) =>
    effect.pipe(Effect.scoped, Effect.timeoutOption(timeout)),
  Effect.map(
    Option.getOrElse((): UsageOutcome => ({ _tag: "failed", message: MESSAGES.timedOut })),
  ),
  // Never log the report or the error's text: either may name the account.
  Effect.catch(() =>
    Effect.logWarning("Could not run Antigravity's /usage report.").pipe(
      Effect.as<UsageOutcome>({ _tag: "failed", message: MESSAGES.unreadable }),
    ),
  ),
);

/**
 * The driver's seam: `...(yield* overlay(...))` in its instance. Google account instances get the
 * wrapped snapshot; API key and Vertex ones keep theirs, as `/usage` reports Google plans only.
 */
export const overlay = Effect.fn("ForkAntigravityLimits.overlay")(function* <E>(
  snapshot: ManagedServerProvider,
  settings: Pick<AntigravitySettings, "authMethod">,
  prepareSpawn: PrepareSpawn<E>,
  processEnvironment: NodeJS.ProcessEnv,
): Effect.fn.Return<
  { readonly snapshot?: ManagedServerProvider },
  never,
  Scope.Scope | ChildProcessSpawner.ChildProcessSpawner
> {
  if (settings.authMethod !== "oauth-personal" && settings.authMethod !== "oauth-business") {
    return {};
  }
  const spawner = yield* ChildProcessSpawner.ChildProcessSpawner;
  const run = makeCliRun(prepareSpawn, processEnvironment).pipe(
    Effect.provideService(ChildProcessSpawner.ChildProcessSpawner, spawner),
  );
  return { snapshot: yield* makeAntigravityLimits({ base: snapshot, run }) };
});
