/**
 * Fork: the MCP profile keys of a settings.json that does not decode (upstream's load then gives
 * defaults, so `all`), read from its text. Text naming a key but not an object is unreadable. Text
 * that is not an object and names no key is unknown (the caller decides) when it may be cut off
 * (empty, cut off, garbage); a whole object with a typo (braces balance, ends with `}`) sets none.
 */
import {
  ForkProjectOverrideFields,
  ForkServerSettingsFields,
  type ServerSettings,
} from "@t3tools/contracts";
import * as Exit from "effect/Exit";
import * as Schema from "effect/Schema";

import { parseSettingsFileText } from "../serverSettings.ts";

/** The settings keys MCP profiles read from the file, besides `mcpCarriedProfiles`. */
export type McpProfileFileKeys = Pick<
  ServerSettings,
  "mcpProfiles" | "mcpProfile" | "mcpSubagentProfile" | "mcpThreadProfiles"
> & {
  /** Each project's two MCP profile keys, as written; a project left out has none. */
  readonly projectSettingsOverrides: Readonly<
    Record<string, { readonly mcpProfile?: string; readonly mcpSubagentProfile?: string }>
  >;
};

export type McpProfileFileKeysRead =
  /** A JSON object: the keys it sets (none named: the defaults, so a valid file sets none). */
  | { readonly _tag: "keys"; readonly keys: McpProfileFileKeys }
  /**
   * Not a JSON object, naming no key, and maybe cut off: empty, cut off before its keys, or
   * garbage. It may have held a profile, so it says nothing either way; `issue` says what it is.
   */
  | { readonly _tag: "unknown"; readonly issue: string }
  /** Names a key but cannot be read: refuse, never `all` on a guess. */
  | { readonly _tag: "unreadable"; readonly issue: string };

/**
 * Whether text that cannot be read names a profile key. JSON can spell a key's letters only as
 * themselves or as a `\u` escape, so text with an escape counts.
 */
const NAMES_A_PROFILE_KEY = /mcpProfile|mcpSubagentProfile|mcpThreadProfiles|\\u/;

// The same field schemas a full decode uses: each reads leniently (a value it cannot read is a
// marker no profile has, which refuses), so one key never hides the others.
const ServerKeys = Schema.Struct({
  mcpProfiles: ForkServerSettingsFields.mcpProfiles,
  mcpProfile: ForkServerSettingsFields.mcpProfile,
  mcpSubagentProfile: ForkServerSettingsFields.mcpSubagentProfile,
  mcpThreadProfiles: ForkServerSettingsFields.mcpThreadProfiles,
});
const ProjectKeys = Schema.Struct({
  mcpProfile: ForkProjectOverrideFields.mcpProfile,
  mcpSubagentProfile: ForkProjectOverrideFields.mcpSubagentProfile,
});
const decodeServerKeys = Schema.decodeUnknownExit(ServerKeys);
const decodeProjectKeys = Schema.decodeUnknownExit(ProjectKeys);

const isRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const kind = (value: unknown) =>
  value === null ? "null" : Array.isArray(value) ? "a list" : typeof value;

/** A project entry that is not an object may have held a profile: a marker no profile has. */
const unreadableProject = (value: unknown) => {
  const marker = `(unreadable ${kind(value)})`;
  return { mcpProfile: marker, mcpSubagentProfile: marker };
};

/**
 * Whether text is one whole JSON object, typo or not: outside strings and comments it starts with
 * `{` and its braces first close back to depth 0 at its last character. A cut-off object never
 * does: its outermost `}` is its last character, so a prefix stays open or ends in a string.
 */
export function isWholeObjectText(raw: string): boolean {
  const text = raw.trim();
  if (!text.endsWith("}")) return false;
  let depth = 0;
  let started = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (char === '"') {
      if (!started) return false;
      index += 1;
      while (index < text.length && text[index] !== '"') {
        if (text[index] === "\\") index += 1;
        index += 1;
      }
      if (index >= text.length) return false;
    } else if (char === "/" && text[index + 1] === "/") {
      const end = text.indexOf("\n", index);
      if (end < 0) return false;
      index = end;
    } else if (char === "/" && text[index + 1] === "*") {
      const end = text.indexOf("*/", index + 2);
      if (end < 0) return false;
      index = end + 1;
    } else if (char === "{") {
      started = true;
      depth += 1;
    } else if (!started && !/\s/.test(char ?? "")) {
      return false;
    } else if (char === "}") {
      depth -= 1;
      if (depth === 0) return index === text.length - 1;
    }
  }
  return false;
}

/**
 * The MCP profile keys of settings.json's text, with the policy's defaults filled in as a load
 * fills them (`withDefaults`); the policy's locks are the caller's to lay over. Pure.
 */
export function readMcpProfileKeysFromText(
  raw: string,
  withDefaults: (persisted: unknown) => unknown,
): McpProfileFileKeysRead {
  const parsed = parseSettingsFileText(raw);
  if (Exit.isFailure(parsed) || !isRecord(parsed.value)) {
    const issue = Exit.isSuccess(parsed)
      ? `it holds ${kind(parsed.value)}, not an object`
      : raw.trim() === ""
        ? "it is empty"
        : "it is not valid JSON";
    if (NAMES_A_PROFILE_KEY.test(raw)) return { _tag: "unreadable", issue };
    // A whole object with a typo names no key, so it sets none, as `{}` does: T3 keeps working,
    // as upstream does. Only text that may be cut off before its keys cannot say.
    if (Exit.isFailure(parsed) && isWholeObjectText(raw)) {
      return readMcpProfileKeysFromText("{}", withDefaults);
    }
    return { _tag: "unknown", issue };
  }
  const filled = withDefaults(parsed.value);
  if (!isRecord(filled)) {
    return { _tag: "unreadable", issue: "the managed policy's defaults do not apply" };
  }
  const server = decodeServerKeys(filled);
  if (Exit.isFailure(server)) {
    return { _tag: "unreadable", issue: "its MCP profile keys do not read" };
  }
  const projects: unknown = filled.projectSettingsOverrides;
  if (projects !== undefined && projects !== null && !isRecord(projects)) {
    return {
      _tag: "unreadable",
      issue: `its projectSettingsOverrides holds ${kind(projects)}, not a map`,
    };
  }
  const projectSettingsOverrides: Record<
    string,
    { readonly mcpProfile?: string; readonly mcpSubagentProfile?: string }
  > = {};
  for (const [projectId, entry] of Object.entries(projects ?? {})) {
    if (!isRecord(entry)) {
      projectSettingsOverrides[projectId] = unreadableProject(entry);
      continue;
    }
    const decoded = decodeProjectKeys(entry);
    projectSettingsOverrides[projectId] = Exit.isSuccess(decoded)
      ? decoded.value
      : unreadableProject(entry);
  }
  return { _tag: "keys", keys: { ...server.value, projectSettingsOverrides } };
}

/** The MCP profile keys of settings a load decoded, as {@link readMcpProfileKeysFromText} gives. */
export function mcpProfileFileKeysOf(settings: ServerSettings): McpProfileFileKeys {
  const projectSettingsOverrides: Record<
    string,
    { readonly mcpProfile?: string; readonly mcpSubagentProfile?: string }
  > = {};
  for (const [projectId, override] of Object.entries(settings.projectSettingsOverrides)) {
    const { mcpProfile, mcpSubagentProfile } = override ?? {};
    if (mcpProfile === undefined && mcpSubagentProfile === undefined) continue;
    projectSettingsOverrides[projectId] = {
      ...(mcpProfile === undefined ? {} : { mcpProfile }),
      ...(mcpSubagentProfile === undefined ? {} : { mcpSubagentProfile }),
    };
  }
  return {
    mcpProfiles: settings.mcpProfiles,
    mcpProfile: settings.mcpProfile,
    mcpSubagentProfile: settings.mcpSubagentProfile,
    mcpThreadProfiles: settings.mcpThreadProfiles,
    projectSettingsOverrides,
  };
}

/**
 * `base` with the file's MCP profile keys laid over it. A project's other overrides stay as `base`
 * has them; its two MCP keys are the file's, so a project the file does not list has none.
 */
export function withMcpProfileFileKeys(
  base: ServerSettings,
  keys: McpProfileFileKeys,
): ServerSettings {
  const projectSettingsOverrides: Record<string, unknown> = {};
  for (const [projectId, override] of Object.entries(base.projectSettingsOverrides)) {
    const { mcpProfile: _profile, mcpSubagentProfile: _subagent, ...rest } = override ?? {};
    projectSettingsOverrides[projectId] = rest;
  }
  for (const [projectId, override] of Object.entries(keys.projectSettingsOverrides)) {
    const kept = (projectSettingsOverrides[projectId] ?? {}) as Readonly<Record<string, unknown>>;
    projectSettingsOverrides[projectId] = { ...kept, ...override };
  }
  return {
    ...base,
    mcpProfiles: keys.mcpProfiles,
    mcpProfile: keys.mcpProfile,
    mcpSubagentProfile: keys.mcpSubagentProfile,
    mcpThreadProfiles: keys.mcpThreadProfiles,
    projectSettingsOverrides:
      projectSettingsOverrides as ServerSettings["projectSettingsOverrides"],
  };
}
