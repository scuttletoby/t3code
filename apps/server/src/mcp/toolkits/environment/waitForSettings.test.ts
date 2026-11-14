import { expect, it } from "@effect/vitest";
import { Tool } from "effect/ai";

import { ProjectToolkit } from "../project/tools.ts";
import { EnvironmentToolkit } from "./tools.ts";

// Fork: waits run commands and hold agent slots, so only a person may change these settings.
const PERSON_ONLY_KEYS = ["waitFor", "waitChecks", "agentSlots"];

const propertyNames = (schema: unknown): ReadonlyArray<string> => {
  if (schema === null || typeof schema !== "object") return [];
  const record = schema as Record<string, unknown>;
  const own =
    record.properties !== null && typeof record.properties === "object"
      ? Object.keys(record.properties)
      : [];
  return [...own, ...Object.values(record).flatMap(propertyNames)];
};

it.each([
  ["t3_environment_preferences_update", EnvironmentToolkit.tools.t3_environment_preferences_update],
  ["t3_project_update", ProjectToolkit.tools.t3_project_update],
] as const)("%s cannot write wait or agent slot settings", (_name, tool) => {
  const names = propertyNames(Tool.getJsonSchema(tool));
  expect(names.length).toBeGreaterThan(0);
  for (const key of PERSON_ONLY_KEYS) expect(names).not.toContain(key);
});
