/**
 * Fork (fleet usage, fork issue 1146): each machine's own usage (`ForkUsageSnapshot`, on its
 * `t3_environment_read`) and the fleet answer of `fork.fleetUsage`. Accounts match across machines
 * by a hash of their stable id; no id, e-mail or credential leaves its machine.
 */
import * as Rpc from "effect/rpc/Rpc";
import * as Schema from "effect/Schema";

import { EnvironmentAuthorizationError } from "./auth.ts";
import {
  EnvironmentId,
  ForwardCompatibleArray,
  IsoDateTime,
  NonNegativeInt,
  TrimmedNonEmptyString,
} from "./baseSchemas.ts";
import { ServerProviderUsageWindow } from "./providerUsageLimits.ts";

/** The method name. Not in upstream's `WS_METHODS`: it is the fork's own. */
export const FORK_FLEET_USAGE_METHOD = "fork.fleetUsage" as const;

/** One account as the machine it lives on reads it. */
export const ForkUsageReading = Schema.Struct({
  /**
   * With `shared`, a hash of the account's stable id (its e-mail or credential fingerprint, with
   * the workspace), the same on every machine; without, a hash that names it on this machine only.
   */
  key: TrimmedNonEmptyString,
  /** `true`: `key` names the account on every machine, so the same account is shown once. */
  shared: Schema.Boolean,
  /** The provider driver (`claudeAgent`, `codex`, ...), a plain string so a new one still decodes. */
  driver: TrimmedNonEmptyString,
  /** The name the provider already shows for it (the instance's name, or the provider's). */
  name: TrimmedNonEmptyString,
  /** The plan the provider names, when it names one. */
  plan: Schema.optionalKey(TrimmedNonEmptyString),
  /** When this machine last read the limits (its own clock). */
  checkedAt: IsoDateTime,
  windows: ForwardCompatibleArray(ServerProviderUsageWindow),
  /** Set when the provider gives no limits for it now: why. Its `windows` are then empty. */
  unavailable: Schema.optionalKey(Schema.String),
});
export type ForkUsageReading = typeof ForkUsageReading.Type;

/** What a machine reports about its own accounts. */
export const ForkUsageSnapshot = Schema.Struct({
  /** That machine's clock when it read them (ms), so an age never mixes two machines' clocks. */
  readAt: NonNegativeInt,
  accounts: ForwardCompatibleArray(ForkUsageReading),
});
export type ForkUsageSnapshot = typeof ForkUsageSnapshot.Type;

export const ForkFleetMachineRef = Schema.Struct({
  environmentId: EnvironmentId,
  label: Schema.String,
});
export type ForkFleetMachineRef = typeof ForkFleetMachineRef.Type;

/** One machine of the fleet: the server answering (`here`) or a linked one. */
export const ForkFleetUsageMachine = Schema.Struct({
  ...ForkFleetMachineRef.fields,
  here: Schema.Boolean,
  state: Schema.Literals(["answered", "unknown"]),
  /** Why the machine is `unknown` (it did not answer, or its version does not report usage). */
  reason: Schema.optionalKey(Schema.String),
  /** How long before this answer the machine last answered (ms); absent: never since the start. */
  answeredAgoMs: Schema.optionalKey(NonNegativeInt),
});
export type ForkFleetUsageMachine = typeof ForkFleetUsageMachine.Type;

/** One account across the fleet, once, with every machine that uses it. */
export const ForkFleetUsageAccount = Schema.Struct({
  key: TrimmedNonEmptyString,
  driver: TrimmedNonEmptyString,
  name: TrimmedNonEmptyString,
  plan: Schema.optionalKey(TrimmedNonEmptyString),
  /** Every machine that reports the account, answering now or not. */
  machines: Schema.Array(ForkFleetMachineRef),
  /** `unknown`: no machine has a fresh reading now; `reason` says why and `windows` is empty. */
  state: Schema.Literals(["known", "unknown"]),
  reason: Schema.optionalKey(Schema.String),
  /** The machine the shown reading comes from: the freshest one. */
  readFrom: Schema.optionalKey(ForkFleetMachineRef),
  /** The shown reading's age when the server answered (ms). */
  ageMs: Schema.optionalKey(NonNegativeInt),
  windows: ForwardCompatibleArray(ServerProviderUsageWindow),
});
export type ForkFleetUsageAccount = typeof ForkFleetUsageAccount.Type;

export const ForkFleetUsageAnswer = Schema.Struct({
  /** The server's clock when it answered (ms). */
  answeredAt: NonNegativeInt,
  /**
   * `false`: the server has no linked machine (or Linked environments is off there), so the Usage
   * tab shows what it always did and nothing more.
   */
  linked: Schema.Boolean,
  machines: Schema.Array(ForkFleetUsageMachine),
  accounts: Schema.Array(ForkFleetUsageAccount),
});
export type ForkFleetUsageAnswer = typeof ForkFleetUsageAnswer.Type;

export const ForkFleetUsageInput = Schema.Struct({});
export type ForkFleetUsageInput = typeof ForkFleetUsageInput.Type;

/** Spread into upstream's `WsRpcGroup` (one seam line in rpc.ts). */
export const WsForkFleetUsageRpc = Rpc.make(FORK_FLEET_USAGE_METHOD, {
  payload: ForkFleetUsageInput,
  success: ForkFleetUsageAnswer,
  error: EnvironmentAuthorizationError,
});
