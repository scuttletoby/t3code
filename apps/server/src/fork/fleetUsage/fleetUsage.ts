/**
 * Fork (fleet usage, fork issue 1146): `fork.fleetUsage`, read from each linked machine's
 * `t3_environment_read` over its link (no new tool or token), one shared round a minute, each
 * machine for at most 10 s. ws.ts's handler `read` finds the service only if it is there.
 */
import {
  type EnvironmentId,
  type ForkFleetUsageAnswer,
  type ForkFleetUsageInput,
  ForkUsageSnapshot,
} from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as Context from "effect/Context";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Ref from "effect/Ref";
import * as Schema from "effect/Schema";

import * as Environment from "../../environment/ServerEnvironment.ts";
import * as PeerLinks from "../../peer/PeerLinks.ts";
import * as PeerMcpClient from "../../peer/PeerMcpClient.ts";
import { type MachineRound, mergeFleet } from "./fleetMerge.ts";
import { readSnapshot } from "./usageSnapshot.ts";

/** One round serves every reader this long; the Usage tab asks once a minute while it shows. */
export const ROUND_TTL = Duration.minutes(1);
/** A linked machine that takes longer is `unknown` for this round. */
export const MACHINE_TIMEOUT = Duration.seconds(10);
/** The narrowest modes: the read is a read on every link's access. */
const READ_LIMITS = { runtimeMode: "approval-required", interactionMode: "default" } as const;

export const REASON_TIMED_OUT = "It did not answer within 10 seconds.";
const REASON_NO_CLIENT = "This server cannot call its linked machines now.";

type Answer =
  | Extract<MachineRound["now"], { _tag: "answered" }>
  | { _tag: "failed"; reason: string };

/** What a round needs; each part never fails. */
export interface FleetDeps {
  /** This server, or `null` when it cannot name itself (it is then left out). */
  readonly here: Effect.Effect<{
    readonly environmentId: EnvironmentId;
    readonly label: string;
  } | null>;
  readonly snapshotHere: Effect.Effect<ForkUsageSnapshot | null>;
  /** The linked machines; empty when Linked environments is off or none is linked. */
  readonly links: Effect.Effect<
    ReadonlyArray<{ readonly environmentId: EnvironmentId; readonly label: string }>
  >;
  /** One machine's answer: its snapshot (`null`: its version reports none), or why there is none. */
  readonly ask: (environmentId: EnvironmentId) => Effect.Effect<Answer>;
}

/** The shared round, cached for `ROUND_TTL`, and each machine's last snapshot for silent rounds. */
export const makeCollector = Effect.fn("ForkFleetUsage.makeCollector")(function* (deps: FleetDeps) {
  const lastAnswers = yield* Ref.make(
    new Map<EnvironmentId, { readonly snapshot: ForkUsageSnapshot; readonly receivedAt: number }>(),
  );
  const askOne = (link: { readonly environmentId: EnvironmentId; readonly label: string }) =>
    deps.ask(link.environmentId).pipe(
      Effect.timeoutOption(MACHINE_TIMEOUT),
      Effect.map(Option.getOrElse((): Answer => ({ _tag: "failed", reason: REASON_TIMED_OUT }))),
      Effect.flatMap((now) =>
        Effect.gen(function* () {
          if (now._tag === "answered" && now.snapshot !== null) {
            const answer = { snapshot: now.snapshot, receivedAt: yield* Clock.currentTimeMillis };
            yield* Ref.update(lastAnswers, (map) => new Map(map).set(link.environmentId, answer));
          }
          const last = (yield* Ref.get(lastAnswers)).get(link.environmentId) ?? null;
          return { ...link, here: false, now, last } satisfies MachineRound;
        }),
      ),
    );
  const round = Effect.gen(function* () {
    const [here, snapshotHere, links] = yield* Effect.all(
      [deps.here, deps.snapshotHere, deps.links],
      { concurrency: 3 },
    );
    const others = links.filter((link) => link.environmentId !== here?.environmentId);
    const startedAt = yield* Clock.currentTimeMillis;
    const remote = yield* Effect.forEach(others, askOne, { concurrency: "unbounded" });
    const hereRound: ReadonlyArray<MachineRound> =
      here === null
        ? []
        : [
            {
              ...here,
              here: true,
              now: { _tag: "answered", snapshot: snapshotHere },
              last:
                snapshotHere === null ? null : { snapshot: snapshotHere, receivedAt: startedAt },
            },
          ];
    return mergeFleet({
      answeredAt: yield* Clock.currentTimeMillis,
      linked: others.length > 0,
      machines: [...hereRound, ...remote],
    });
  }).pipe(Effect.withSpan("ForkFleetUsage.round"));
  return { read: yield* Effect.cachedWithTTL(round, ROUND_TTL) };
});

const UsageAnswer = Schema.Struct({ usage: Schema.optionalKey(Schema.Unknown) });
const decodeSnapshot = Schema.decodeUnknownOption(ForkUsageSnapshot);

const oneLine = (text: string) => text.replaceAll(/\s+/gu, " ").trim().slice(0, 300);

/** The real parts, each read from the caller's context only if it is there. */
const liveDeps: FleetDeps = {
  here: Effect.serviceOption(Environment.ServerEnvironment).pipe(
    Effect.flatMap(
      Option.match({
        onNone: () => Effect.succeed(null),
        onSome: (environment) =>
          Effect.map(environment.getDescriptor, (descriptor) => ({
            environmentId: descriptor.environmentId,
            label: descriptor.label,
          })),
      }),
    ),
    Effect.orElseSucceed(() => null),
  ),
  snapshotHere: readSnapshot(),
  links: Effect.serviceOption(PeerLinks.PeerLinks).pipe(
    Effect.flatMap(
      Option.match({
        onNone: () => Effect.succeed([]),
        onSome: (links) =>
          Effect.flatMap(links.enabled, (enabled) => (enabled ? links.names : Effect.succeed([]))),
      }),
    ),
    Effect.orElseSucceed(() => []),
  ),
  ask: (environmentId) =>
    Effect.serviceOption(PeerMcpClient.PeerMcpClient).pipe(
      Effect.flatMap(
        Option.match({
          onNone: () => Effect.succeed<Answer>({ _tag: "failed", reason: REASON_NO_CLIENT }),
          onSome: (client) =>
            client
              .call({
                environmentId,
                tool: "t3_environment_read",
                arguments: {},
                limits: READ_LIMITS,
                success: UsageAnswer,
              })
              .pipe(
                Effect.map((answer): Answer => {
                  if (answer.usage === undefined) return { _tag: "answered", snapshot: null };
                  const snapshot = decodeSnapshot(answer.usage);
                  return Option.isSome(snapshot)
                    ? { _tag: "answered", snapshot: snapshot.value }
                    : { _tag: "failed", reason: "Its usage answer could not be read." };
                }),
                Effect.catch((error) =>
                  Effect.succeed<Answer>({
                    _tag: "failed",
                    reason: oneLine(error.message) || "It refused the read.",
                  }),
                ),
              ),
        }),
      ),
      Effect.catchCause(() =>
        Effect.succeed<Answer>({ _tag: "failed", reason: "The read failed on this server." }),
      ),
    ),
};

class ForkFleetUsage extends Context.Service<
  ForkFleetUsage,
  { readonly read: Effect.Effect<ForkFleetUsageAnswer> }
>()("t3/fork/fleetUsage/fleetUsage/ForkFleetUsage") {}

export const layer = Layer.effect(
  ForkFleetUsage,
  Effect.map(makeCollector(liveDeps), ForkFleetUsage.of),
);

/** The handler upstream's ws.ts calls. A server without the service answers as unlinked. */
export const read = (_input: ForkFleetUsageInput): Effect.Effect<ForkFleetUsageAnswer> =>
  Effect.serviceOption(ForkFleetUsage).pipe(
    Effect.flatMap(
      Option.match({
        onNone: () =>
          Effect.map(Clock.currentTimeMillis, (answeredAt) => ({
            answeredAt,
            linked: false,
            machines: [],
            accounts: [],
          })),
        onSome: (service) => service.read,
      }),
    ),
  );
