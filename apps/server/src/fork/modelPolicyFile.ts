/**
 * Fork: the model policy when the settings file fails to load. Upstream then runs on its defaults,
 * whose policy is off, so a broken file would turn an enforced policy off. Instead the policy is
 * read from the file alone when it can be, and fails closed when it cannot (nothing is written back).
 */
import {
  type ModelPolicy,
  MODEL_POLICY_FAIL_CLOSED,
  ModelPolicyFromSettingsFile,
} from "@t3tools/contracts";
import { fromLenientJson } from "@t3tools/shared/schemaJson";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as Schema from "effect/Schema";

import { readSettingsFile } from "./settingsFileRead.ts";

/** What `unreadable` holds for a file that does not parse: the policy in it cannot be seen. */
export const SETTINGS_FILE_UNPARSEABLE = "the settings file does not parse";
/** The server log's warning for a file that does not parse. */
export const SETTINGS_FILE_UNPARSEABLE_WARNING =
  "settings: settings.json does not parse, so the model policy cannot be read: every agent model pick is refused until the file is fixed";

const decodePolicy = Schema.decodeUnknownExit(ModelPolicyFromSettingsFile);
/** The load's lenient parse (comments, trailing commas), as upstream's load parses the file. */
const parseFile = Schema.decodeUnknownExit(fromLenientJson(Schema.Unknown));

/**
 * The policy after a failed load: the file's own decoded policy when only another part failed;
 * its `modelPolicy` key read alone when the file parses; the defaults' when the file names none;
 * enforce with nothing allowed when it does not parse or is no object. Pure, as upstream's
 * decodeSettingsFile is; the load warns through its own logger (warnAfterLoadFailure).
 */
export function afterLoadFailure(
  decoded: Exit.Exit<unknown, unknown>,
  raw: string,
  defaults: { readonly modelPolicy: ModelPolicy },
): ModelPolicy {
  return afterLoadFailureQuiet(decoded, parseFile(raw), defaults.modelPolicy);
}

/** The load's warning, through the server's logger, when the file did not parse. */
export const warnAfterLoadFailure = (policy: ModelPolicy) =>
  policy.unreadable === SETTINGS_FILE_UNPARSEABLE
    ? Effect.logWarning(SETTINGS_FILE_UNPARSEABLE_WARNING)
    : Effect.void;

function afterLoadFailureQuiet(
  decoded: Exit.Exit<unknown, unknown>,
  parsed: Exit.Exit<unknown, unknown>,
  fallback: ModelPolicy,
): ModelPolicy {
  // Upstream's own decode of the file's policy (its settings schema holds the same ModelPolicy).
  if (Exit.isSuccess(decoded))
    return (decoded.value as { readonly modelPolicy: ModelPolicy }).modelPolicy;
  const file = Exit.isSuccess(parsed) ? readSettingsFile(parsed.value).settings : undefined;
  if (typeof file !== "object" || file === null || Array.isArray(file)) {
    return { ...MODEL_POLICY_FAIL_CLOSED, unreadable: SETTINGS_FILE_UNPARSEABLE };
  }
  if (!Object.hasOwn(file, "modelPolicy")) return fallback;
  const policy = decodePolicy((file as { readonly modelPolicy: unknown }).modelPolicy);
  return Exit.isSuccess(policy) ? policy.value : MODEL_POLICY_FAIL_CLOSED;
}
