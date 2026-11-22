/**
 * Fork (calm sidebar): which thread holds a role, as a person-only settings pointer (one orchestrator
 * per project). A pointer can outlive its thread: readers check the thread and treat a failing one
 * as "no role". Docs: docs/fork/README.md, `server.threadRoles`.
 */
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import { ProjectId, ThreadId } from "./baseSchemas.ts";

/**
 * The role pointers: the assistant thread, and one orchestrator thread per project. Each field has
 * its default, since the settings writer saves the value sparse (a field at its default left out).
 */
export const ThreadRoles = Schema.Struct({
  assistant: Schema.NullOr(ThreadId).pipe(Schema.withDecodingDefault(Effect.succeed(null))),
  orchestrators: Schema.Record(ProjectId, ThreadId).pipe(
    Schema.withDecodingDefault(Effect.succeed({})),
  ),
});
export type ThreadRoles = typeof ThreadRoles.Type;

/** No roles: upstream's behaviour, and what an old settings file reads as. */
export const EMPTY_THREAD_ROLES: ThreadRoles = { assistant: null, orchestrators: {} };

const isRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** A text a thread or project id may be: non-blank, without outer spaces. */
const isIdText = (value: unknown): value is string =>
  typeof value === "string" && value.length > 0 && value.trim() === value;

/**
 * The settings file's `threadRoles`, read leniently like `hiddenThreadTags`: a bad entry drops
 * alone, so a hand edit never resets the other settings. Not an object (or missing): no roles. A bad
 * `assistant` reads as none; an orchestrator entry whose key or value is not an id is dropped.
 */
function readThreadRoles(raw: unknown): {
  readonly assistant: string | null;
  readonly orchestrators: { readonly [projectId: string]: string };
} {
  if (!isRecord(raw)) return { assistant: null, orchestrators: {} };
  const orchestrators = isRecord(raw.orchestrators) ? raw.orchestrators : {};
  return {
    assistant: isIdText(raw.assistant) ? raw.assistant : null,
    orchestrators: Object.fromEntries(
      Object.entries(orchestrators).filter(
        (entry): entry is [string, string] => isIdText(entry[0]) && isIdText(entry[1]),
      ),
    ),
  };
}

/** {@link ThreadRoles} as the settings file holds it: read by {@link readThreadRoles}. */
export const ThreadRolesFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    ThreadRoles,
    SchemaTransformation.transform({
      decode: (raw: unknown): typeof ThreadRoles.Encoded => readThreadRoles(raw),
      encode: (roles: typeof ThreadRoles.Encoded): unknown => roles,
    }),
  ),
  Schema.withDecodingDefault(Effect.succeed(EMPTY_THREAD_ROLES)),
);

/**
 * A save's change to the roles. A field left out keeps its value. `assistant: null` removes the
 * assistant; an orchestrator entry is set or (`null`) removed on its own, so two people changing
 * different projects at once keep both. Strict: a bad id is refused. The server also refuses a
 * pointer to a thread that cannot hold the role (fork/threadRoleTargets.ts).
 */
export const ThreadRolesPatch = Schema.Struct({
  assistant: Schema.optionalKey(Schema.NullOr(ThreadId)),
  orchestrators: Schema.optionalKey(Schema.Record(ProjectId, Schema.NullOr(ThreadId))),
});
export type ThreadRolesPatch = typeof ThreadRolesPatch.Type;
