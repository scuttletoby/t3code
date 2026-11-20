/**
 * Fork: the MCP profile keys of a settings.json that does not decode as a whole (upstream's load
 * then gives defaults, so `all`), read from its text as a full decode reads them. Text that names a
 * key but cannot be read as an object refuses, never `all` on a guess; text naming none sets none.
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
  /** `keys` is `undefined` when the text names no MCP profile key, so it sets none. */
  | { readonly ok: true; readonly keys: McpProfileFileKeys | undefined }
  | { readonly ok: false; readonly issue: string };

/**
 * Text that names none of these keys sets no profile, whatever else is wrong with it. JSON can
 * spell a key's letters only as themselves or as a `\u` escape, so text with an escape counts.
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
 * The MCP profile keys of settings.json's text, with the policy's defaults filled in as a load
 * fills them (`withDefaults`); the policy's locks are the caller's to lay over. Pure.
 */
export function readMcpProfileKeysFromText(
  raw: string,
  withDefaults: (persisted: unknown) => unknown,
): McpProfileFileKeysRead {
  if (!NAMES_A_PROFILE_KEY.test(raw)) return { ok: true, keys: undefined };
  const parsed = parseSettingsFileText(raw);
  if (Exit.isFailure(parsed)) return { ok: false, issue: "it is not valid JSON" };
  if (!isRecord(parsed.value)) {
    return { ok: false, issue: `it holds ${kind(parsed.value)}, not an object` };
  }
  const filled = withDefaults(parsed.value);
  if (!isRecord(filled)) return { ok: false, issue: "the managed policy's defaults do not apply" };
  const server = decodeServerKeys(filled);
  if (Exit.isFailure(server)) return { ok: false, issue: "its MCP profile keys do not read" };
  const projects: unknown = filled.projectSettingsOverrides;
  if (projects !== undefined && projects !== null && !isRecord(projects)) {
    return { ok: false, issue: `its projectSettingsOverrides holds ${kind(projects)}, not a map` };
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
  return { ok: true, keys: { ...server.value, projectSettingsOverrides } };
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
