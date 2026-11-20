/**
 * Fork: a tool argument field only a linked machine sets (the MCP profile carried in
 * `delegatedFrom`) is left out of the input schema agents see, so it costs no thread any tokens and
 * upstream's tool budget stays upstream's. Decoding still reads it.
 */
import * as Predicate from "effect/Predicate";

type JsonObject = { [key: string]: unknown };

/** An object schema without `mcpProfile`, also inside `anyOf` (nullable); the same value if none. */
const withoutCarriedProfile = (schema: unknown): unknown => {
  if (!Predicate.isObject(schema)) return schema;
  const anyOf: unknown = schema.anyOf;
  if (Array.isArray(anyOf)) {
    const branches = anyOf.map(withoutCarriedProfile);
    return branches.every((branch, index) => branch === anyOf[index])
      ? schema
      : { ...schema, anyOf: branches };
  }
  if (!Predicate.isObject(schema.properties) || !("mcpProfile" in schema.properties)) return schema;
  const { mcpProfile: _machineOnly, ...kept } = schema.properties;
  return { ...schema, properties: kept };
};

/** `schema` without `delegatedFrom.mcpProfile` among its properties; the same object without one. */
export const withoutMachineOnlyFields = (schema: JsonObject): JsonObject => {
  const properties = schema.properties;
  if (!Predicate.isObject(properties) || !("delegatedFrom" in properties)) return schema;
  const delegatedFrom = withoutCarriedProfile(properties.delegatedFrom);
  if (delegatedFrom === properties.delegatedFrom) return schema;
  return { ...schema, properties: { ...properties, delegatedFrom } };
};
