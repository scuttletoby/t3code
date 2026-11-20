/**
 * Fork: a tool argument field only a linked machine sets (the MCP profile carried in
 * `delegatedFrom`) is left out of the input schema agents see, so it costs no thread any tokens and
 * upstream's tool budget stays upstream's. Decoding still reads it.
 */
import * as Predicate from "effect/Predicate";

type JsonObject = { [key: string]: unknown };

/** `schema` without `delegatedFrom.mcpProfile` among its properties; the same object without one. */
export const withoutMachineOnlyFields = (schema: JsonObject): JsonObject => {
  const properties = schema.properties;
  if (!Predicate.isObject(properties)) return schema;
  const delegatedFrom = properties.delegatedFrom;
  if (!Predicate.isObject(delegatedFrom) || !Predicate.isObject(delegatedFrom.properties)) {
    return schema;
  }
  if (!("mcpProfile" in delegatedFrom.properties)) return schema;
  const { mcpProfile: _machineOnly, ...kept } = delegatedFrom.properties;
  return {
    ...schema,
    properties: { ...properties, delegatedFrom: { ...delegatedFrom, properties: kept } },
  };
};
