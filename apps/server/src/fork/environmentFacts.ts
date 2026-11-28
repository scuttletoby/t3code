/**
 * Fork (placement rows P3, P4): the facts `t3_environment_read` returns beside upstream's fields
 * ("Machine facts on t3_environment_read" in docs/fork/README.md). Slot facts come only from the
 * gate's status read, labels and "the machine says" from Settings and `factsCommand`.
 */
import {
  canonicalPlacementLabels,
  ENVIRONMENT_FILE_LINK_AGENT_HINT,
  ForkUsageSnapshot,
  HostResourcesSnapshot,
  NonNegativeInt,
  type ServerSettings,
} from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";

import * as HostResources from "../resourceTelemetry/HostResources.ts";
import * as Settings from "../serverSettings.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import { type FactsCommandResult, FactsCommands } from "./factsCommand.ts";
import { readPlacementForThisOs } from "./placementRead.ts";
import * as FleetUsageSnapshot from "./fleetUsage/usageSnapshot.ts";

/**
 * Placement facts. Every field but `labels`, `available` and `factsAt` is optional, so a machine on
 * a newer or older fork build that adds or drops one still decodes here.
 */
export const Placement = Schema.Struct({
  /** That machine's `placement.labels` and the lines its `factsCommand` printed, canonical. */
  labels: Schema.Array(Schema.String),
  /**
   * Whether the slot gate would take one more agent on this machine now and its `factsCommand`
   * (if set) says it is free. `false` also when either cannot be read: then only `reason` says why.
   */
  available: Schema.Boolean,
  /** Why `available` is what it is, when the counts alone do not say it. */
  reason: Schema.optionalKey(Schema.String),
  /** Agents running, starting or holding a slot here, as the slot gate counts them. */
  runningAgents: Schema.optionalKey(NonNegativeInt),
  /** Machine slots left before the gate refuses; `null`: no machine limit is set. */
  slotsFree: Schema.optionalKey(Schema.NullOr(NonNegativeInt)),
  /** The free memory the gate keeps (`agentSlots.minFreeMemoryMb`); `null`: none set. */
  memoryFloorMb: Schema.optionalKey(Schema.NullOr(NonNegativeInt)),
  /** Free memory the gate measured with the counts, in MB. */
  freeMemoryMb: Schema.optionalKey(Schema.NullOr(NonNegativeInt)),
  /** When these facts were read (ms since the epoch). */
  factsAt: NonNegativeInt,
});
export type Placement = typeof Placement.Type;

/** Spread into `t3_environment_read`'s success struct. */
export const ResultFields = {
  hostResources: Schema.optionalKey(HostResourcesSnapshot),
  placement: Schema.optionalKey(Placement),
  /** How to link a file on this environment in a reply on another one. */
  fileLinks: Schema.optionalKey(Schema.String),
  /**
   * This machine's provider accounts and their limits (fork issue 1146), for the fleet's Usage
   * tab: names and windows only, each account's id hashed. Left out on a server without them.
   */
  usage: Schema.optionalKey(ForkUsageSnapshot),
};

/** The gate's own read waits up to `COUNT_READ_TIMEOUT`; one second more, then it is unread. */
const USAGE_READ_TIMEOUT = Duration.sum(SlotGate.COUNT_READ_TIMEOUT, Duration.seconds(1));

/**
 * The host sample's own limit (a Linux read is a few files; macOS runs `vm_stat`). Longer, and the
 * answer leaves `hostResources` out, as for a failed read. It runs beside the slot read, so the
 * whole read stays within `USAGE_READ_TIMEOUT`.
 */
export const HOST_READ_TIMEOUT = Duration.seconds(2);

export const REASON_GATE_MISSING = "The agent slot gate is not running on this machine.";
export const REASON_GATE_UNREAD =
  "The agent slot gate could not count the agents here now, so no slot count is given.";
export const REASON_SETTINGS_UNREAD =
  "This machine's settings could not be read, so no slot count is given.";
export const REASON_SLOTS_OFF =
  "Agent slots are off on this machine: it sets no limit and does not count its agents.";
export const REASON_FULL = "Every machine slot is in use.";
export const REASON_LOW_MEMORY = "Free memory is below the floor the slot gate keeps.";

export const REASON_FACTS_RUNNER_MISSING =
  "This server cannot run its factsCommand (no process runner here).";
export const REASON_FACTS_UNREAD = "This machine's own facts could not be read.";
const reasonPlacementNotValid = (problem: string) =>
  `This machine's placement setting is not valid on it, so it reads as not available: ${problem}`;

const unavailable = (reason: string) => ({ available: false, reason }) as const;

/** The slot facts, read once; never fails, never dies. */
const slotFacts = Effect.fn("EnvironmentFacts.slotFacts")(
  function* (settings: Option.Option<ServerSettings>) {
    const gate = yield* Effect.serviceOption(SlotGate.SlotGate);
    if (Option.isNone(gate)) return unavailable(REASON_GATE_MISSING);
    if (Option.isNone(settings)) return unavailable(REASON_SETTINGS_UNREAD);
    const agentSlots = settings.value.agentSlots;
    if (!agentSlots.enabled) return { available: true, reason: REASON_SLOTS_OFF } as const;
    const usage = yield* gate.value.usage.pipe(Effect.timeoutOption(USAGE_READ_TIMEOUT));
    // `usage` is null while slots are off (checked above) or when its counts cannot be read.
    if (Option.isNone(usage) || usage.value === null) return unavailable(REASON_GATE_UNREAD);
    const { machine, freeMemoryMb } = usage.value;
    const slotsFree = machine.max === null ? null : Math.max(0, machine.max - machine.used);
    const memoryFloorMb = agentSlots.minFreeMemoryMb > 0 ? agentSlots.minFreeMemoryMb : null;
    const lowMemory =
      memoryFloorMb !== null && freeMemoryMb !== null && freeMemoryMb < memoryFloorMb;
    const reason = slotsFree === 0 ? REASON_FULL : lowMemory ? REASON_LOW_MEMORY : undefined;
    return {
      available: reason === undefined,
      ...(reason === undefined ? {} : { reason }),
      runningAgents: machine.used,
      slotsFree,
      memoryFloorMb,
      freeMemoryMb,
    };
  },
  Effect.catchCause(() => Effect.succeed(unavailable(REASON_GATE_UNREAD))),
);

/**
 * What this machine says about itself: its `placement.labels`, then its `factsCommand`'s answer.
 * A setting not valid on this OS, a runner that is not here or a failed run is "not available" with
 * the reason, never "free"; no command set changes nothing. Never fails, never dies.
 */
const selfFacts = Effect.fn("EnvironmentFacts.selfFacts")(
  function* (
    settings: Option.Option<ServerSettings>,
  ): Effect.fn.Return<{ readonly labels: ReadonlyArray<string>; readonly notAvailable?: string }> {
    if (Option.isNone(settings)) return { labels: [] };
    const placement = yield* readPlacementForThisOs(settings.value);
    if (placement.problem !== undefined) {
      return { labels: [], notAvailable: reasonPlacementNotValid(placement.problem) };
    }
    const commands = yield* Effect.serviceOption(FactsCommands);
    const answer: FactsCommandResult =
      placement.factsCommand === ""
        ? { available: true, labels: [] }
        : Option.isNone(commands)
          ? { available: false, reason: REASON_FACTS_RUNNER_MISSING }
          : yield* commands.value.run(placement.factsCommand);
    return answer.available
      ? { labels: canonicalPlacementLabels([...placement.labels, ...answer.labels]) }
      : { labels: canonicalPlacementLabels(placement.labels), notAvailable: answer.reason };
  },
  Effect.catchCause(() => Effect.succeed({ labels: [], notAvailable: REASON_FACTS_UNREAD })),
);

/**
 * The facts for this machine's `t3_environment_read`. `hostResources` is left out when the host
 * sampler is not there or its read fails; `placement` is always given.
 */
export const read = Effect.gen(function* () {
  const host = yield* Effect.serviceOption(HostResources.HostResources);
  const hostRead = Option.isNone(host)
    ? Effect.succeedNone
    : host.value.read.pipe(
        Effect.timeoutOption(HOST_READ_TIMEOUT),
        Effect.catchCause(() => Effect.succeedNone),
      );
  const settingsService = yield* Effect.serviceOption(Settings.ServerSettingsService);
  const settings = Option.isNone(settingsService)
    ? Option.none()
    : yield* settingsService.value.getSettings.pipe(
        Effect.option,
        Effect.catchCause(() => Effect.succeedNone),
      );
  const [hostResources, slots, self, usage] = yield* Effect.all(
    [hostRead, slotFacts(settings), selfFacts(settings), FleetUsageSnapshot.readSnapshot()],
    { concurrency: 4 },
  );
  // Not available if either says so; each "no" keeps its reason, slots first.
  const reasons = [slots.available ? undefined : slots.reason, self.notAvailable].filter(
    (reason) => reason !== undefined,
  );
  const placement: Placement = {
    ...slots,
    labels: self.labels,
    ...(reasons.length === 0 ? {} : { available: false, reason: reasons.join(" ") }),
    factsAt: yield* Clock.currentTimeMillis,
  };
  return {
    ...(Option.isSome(hostResources) ? { hostResources: hostResources.value } : {}),
    placement,
    fileLinks: ENVIRONMENT_FILE_LINK_AGENT_HINT,
    ...(usage === null ? {} : { usage }),
  };
}).pipe(Effect.withSpan("EnvironmentFacts.read"));

// Placement's decode leaves `usage` out: a usage answer it cannot read never refuses a machine.
const { usage: _usage, ...PlacementResultFields } = ResultFields;
const AnswerFacts = Schema.Struct(PlacementResultFields);
const decodeAnswerFacts = Schema.decodeUnknownSync(AnswerFacts);

/**
 * A linked machine's forwarded answer, decoded with these schemas, as the rule engine's
 * `MachineFacts`. No answer (`null` or `undefined`: the machine did not answer) or neither field
 * (an older build): `null`, no facts, never "not valid". Refused by the schemas: only
 * `problem`, so the engine refuses the machine, named too. A newer build's extra field is ignored.
 */
export const factsFromAnswer = (
  answer: unknown,
  receivedAt: number,
): {
  readonly hostResources?: HostResourcesSnapshot;
  readonly placement?: Placement;
  readonly problem?: string;
  readonly receivedAt: number;
} | null => {
  if (answer === null || answer === undefined) return null;
  let decoded: typeof AnswerFacts.Type;
  try {
    decoded = decodeAnswerFacts(answer);
  } catch (error) {
    const text = error instanceof Error ? error.message : String(error);
    const problem = text.replaceAll(/\s+/gu, " ").trim().slice(0, 300) || "not a valid answer";
    return { problem, receivedAt };
  }
  if (decoded.hostResources === undefined && decoded.placement === undefined) return null;
  return {
    ...(decoded.hostResources === undefined ? {} : { hostResources: decoded.hostResources }),
    ...(decoded.placement === undefined ? {} : { placement: decoded.placement }),
    receivedAt,
  };
};
