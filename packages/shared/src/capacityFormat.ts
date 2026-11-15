/**
 * Fork: the small shape language of `capacity/capacity-format.json` (README.md beside it). Every
 * capacity evaluator checks the rule and vector files the same way; strict, so old or newer files are refused.
 */
import formatJson from "./capacity/capacity-format.json" with { type: "json" };

type FormatSpec =
  | string
  | { readonly object: Readonly<Record<string, FormatSpec>> }
  | { readonly map: FormatSpec }
  | { readonly array: FormatSpec; readonly minItems?: number };

export const isPlainObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
export const isFiniteNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

function checkAlternative(
  value: unknown,
  alternative: string,
  types: Readonly<Record<string, FormatSpec>>,
  path: string,
  errors: Array<string>,
): boolean {
  if (alternative.startsWith("=")) return value === alternative.slice(1);
  if (alternative.startsWith("@")) {
    const spec = types[alternative.slice(1)];
    if (spec === undefined) {
      errors.push(`${path}: the format names an unknown type ${alternative}`);
      return false;
    }
    const before = errors.length;
    checkSpec(value, spec, types, path, errors);
    return errors.length === before;
  }
  switch (alternative) {
    case "any":
      return true;
    case "null":
      return value === null;
    case "bool":
      return typeof value === "boolean";
    case "string":
      return typeof value === "string";
    case "number":
      return isFiniteNumber(value);
    case "number>=0":
      return isFiniteNumber(value) && value >= 0;
    case "uint":
      // Whole, 0 to 2^53-1; never a boolean (README "The format language": agreement rules).
      return Number.isSafeInteger(value) && (value as number) >= 0;
    default:
      errors.push(`${path}: the format has an unknown type "${alternative}"`);
      return false;
  }
}

function checkSpec(
  value: unknown,
  spec: FormatSpec,
  types: Readonly<Record<string, FormatSpec>>,
  path: string,
  errors: Array<string>,
): void {
  if (typeof spec === "string") {
    const tries: Array<Array<string>> = [];
    for (const alternative of spec.split("|")) {
      const own: Array<string> = [];
      if (checkAlternative(value, alternative, types, path, own) && own.length === 0) return;
      tries.push(own);
    }
    const detail = tries.find((own) => own.length > 0);
    errors.push(...(detail ?? [`${path}: expected ${spec}, got ${JSON.stringify(value)}`]));
    return;
  }
  if ("array" in spec) {
    if (!Array.isArray(value)) {
      errors.push(`${path}: expected a list`);
      return;
    }
    if (spec.minItems !== undefined && value.length < spec.minItems) {
      errors.push(`${path}: needs at least ${spec.minItems} entries, has ${value.length}`);
    }
    value.forEach((item, index) => checkSpec(item, spec.array, types, `${path}[${index}]`, errors));
    return;
  }
  if (!isPlainObject(value)) {
    errors.push(`${path}: expected an object`);
    return;
  }
  if ("map" in spec) {
    for (const [key, item] of Object.entries(value)) {
      if (!key.startsWith("//")) checkSpec(item, spec.map, types, `${path}.${key}`, errors);
    }
    return;
  }
  const known = new Set<string>();
  for (const [rawKey, fieldSpec] of Object.entries(spec.object)) {
    const optional = rawKey.endsWith("?");
    const key = optional ? rawKey.slice(0, -1) : rawKey;
    known.add(key);
    if (!Object.hasOwn(value, key)) {
      if (!optional) errors.push(`${path}.${key}: missing`);
      continue;
    }
    checkSpec(value[key], fieldSpec, types, `${path}.${key}`, errors);
  }
  for (const key of Object.keys(value)) {
    if (!known.has(key) && !key.startsWith("//")) errors.push(`${path}.${key}: unknown key`);
  }
}

/**
 * Problems with `value` as `spec` (a spec of the format language, e.g. `"uint"` or `"@rule"`); empty
 * when it fits. `path` names the value in the messages.
 */
export function capacitySpecErrors(
  value: unknown,
  spec: unknown,
  path = "value",
  format: unknown = formatJson,
): ReadonlyArray<string> {
  if (
    !isPlainObject(format) ||
    format.format !== "capacity-format/1" ||
    !isPlainObject(format.types)
  ) {
    return ["format file: not capacity-format/1"];
  }
  const types = format.types as Record<string, FormatSpec>;
  const errors: Array<string> = [];
  checkSpec(value, spec as FormatSpec, types, path, errors);
  return errors;
}

/**
 * Problems with `value` as the format's type `typeName` (`"rule"` or `"vectors"`); empty when it is
 * well formed. Strict: an unknown or missing key is a problem, so a file from a newer or older
 * build is refused by name, never half-read.
 */
export function capacityFormatErrors(
  value: unknown,
  typeName: "rule" | "vectors",
  format: unknown = formatJson,
): ReadonlyArray<string> {
  return capacitySpecErrors(value, `@${typeName}`, typeName, format);
}
