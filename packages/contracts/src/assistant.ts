/**
 * Fork (assistant): the `assistant` settings block. `enabled` offers "Make assistant" in Settings;
 * the assistant itself is the thread `threadRoles.assistant` names, run with the built-in MCP
 * profile `assistant`. Off by default. Docs: docs/fork/assistant.md.
 */
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

export const AssistantSettings = Schema.Struct({
  enabled: Schema.Boolean.pipe(Schema.withDecodingDefault(Effect.succeed(false))),
});
export type AssistantSettings = typeof AssistantSettings.Type;

const DEFAULT_ASSISTANT_SETTINGS: AssistantSettings = { enabled: false };

/** The settings file's `assistant`, read leniently: anything but `enabled: true` reads as off. */
export const AssistantSettingsFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    AssistantSettings,
    SchemaTransformation.transform({
      decode: (raw: unknown): typeof AssistantSettings.Encoded => ({
        enabled:
          typeof raw === "object" &&
          raw !== null &&
          (raw as { enabled?: unknown }).enabled === true,
      }),
      encode: (settings: typeof AssistantSettings.Encoded): unknown => settings,
    }),
  ),
  Schema.withDecodingDefault(Effect.succeed(DEFAULT_ASSISTANT_SETTINGS)),
);

/** A save's change: strict, so a bad value is refused; a field left out keeps its value. */
export const AssistantSettingsPatch = Schema.Struct({
  enabled: Schema.optionalKey(Schema.Boolean),
});
