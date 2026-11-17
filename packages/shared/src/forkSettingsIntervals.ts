/**
 * Fork: every interval (Duration) in the server settings, found by walking the settings schema, so
 * a new one, the fork's or upstream's, is covered without being listed anywhere. A save refuses an
 * interval no run could use, and a settings file that holds one loads with that one interval at its
 * default instead of failing whole (a failed decode resets every setting).
 */
import { ServerSettings } from "@t3tools/contracts";
import * as Duration from "effect/Duration";
import * as Schema from "effect/Schema";
import * as SchemaAST from "effect/SchemaAST";

/** Largest interval a setting takes, in milliseconds: beyond it a millisecond count is not exact. */
const MAX_SETTINGS_INTERVAL_MILLIS = Number.MAX_SAFE_INTEGER;

/** True for a millisecond count an interval setting can hold and the settings file can save. */
const isUsableIntervalMillis = (millis: number): boolean =>
  Number.isFinite(millis) && millis >= 0 && millis <= MAX_SETTINGS_INTERVAL_MILLIS;

const isDuration = (ast: SchemaAST.AST): boolean =>
  ast._tag === "Declaration" && ast.run === Schema.Duration.ast.run;

/**
 * Every Duration in `schema`'s decoded type: `paths` by key from the top, and `outOfReach` for one
 * inside a record, an array or a declaration, where no fixed key path leads (written with `*`), or
 * inside a union, where the same path may hold a value that is not an interval.
 */
export function findDurationPaths(schema: Schema.Top): {
  readonly paths: ReadonlyArray<ReadonlyArray<string>>;
  readonly outOfReach: ReadonlyArray<string>;
} {
  const paths: Array<ReadonlyArray<string>> = [];
  const outOfReach: Array<string> = [];
  const found = new Set<string>();
  const suspended = new Set<SchemaAST.AST>();
  const walk = (node: SchemaAST.AST, path: ReadonlyArray<string>, reachable: boolean): void => {
    const ast = SchemaAST.toType(node);
    if (isDuration(ast)) {
      const name = path.join(".");
      if (found.has(name)) return;
      found.add(name);
      if (reachable) paths.push(path);
      else outOfReach.push(name);
      return;
    }
    switch (ast._tag) {
      case "Objects":
        for (const property of ast.propertySignatures) {
          walk(property.type, [...path, String(property.name)], reachable);
        }
        for (const index of ast.indexSignatures) walk(index.type, [...path, "*"], false);
        return;
      case "Union": {
        // `undefined` (an `optional` key) never sits in a file. With two other members, the key
        // path can also hold a non-interval (`null` or "off", another member's field): out of reach.
        // Stricter than needed (`NullOr(Struct)` or variants sharing the key were safe); widen
        // this walk only when the `outOfReach` test fails for such a setting.
        const members = ast.types.filter((member) => SchemaAST.toType(member)._tag !== "Undefined");
        for (const member of members) walk(member, path, reachable && members.length === 1);
        return;
      }
      case "Arrays":
        for (const element of [...ast.elements, ...ast.rest]) walk(element, [...path, "*"], false);
        return;
      case "Declaration":
        for (const parameter of ast.typeParameters) walk(parameter, [...path, "*"], false);
        return;
      case "Suspend":
        // A recursive schema: once is enough to find a Duration in it.
        if (suspended.has(ast)) return;
        suspended.add(ast);
        walk(ast.thunk(), [...path, "*"], false);
        return;
      default:
        return;
    }
  };
  walk(schema.ast, [], true);
  return { paths, outOfReach };
}

/**
 * Every interval setting's path, from the server settings schema. Never widen one (to `NullOr` or
 * "off" for "never"); give the new encoding a new key: an older build drops a value it cannot read
 * and its next save deletes it, so a rollback loses it (docs/fork/updates.md).
 */
export const SETTINGS_INTERVAL_PATHS = findDurationPaths(ServerSettings).paths;

const isRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const getPath = (value: unknown, path: ReadonlyArray<string>): unknown =>
  path.reduce<unknown>((node, key) => (isRecord(node) ? node[key] : undefined), value);

const intervalMillis = (value: unknown): number | undefined =>
  Duration.isDuration(value) ? Duration.toMillis(value) : undefined;

/**
 * Why a save of `next` must be refused for an interval, or `undefined`: an interval the save sets
 * that is negative, not finite (a file cannot hold it: JSON writes it as `null`) or too large. One
 * the save leaves as it was is never refused, so it cannot block saving anything else.
 */
export function settingsIntervalProblem(
  current: ServerSettings,
  next: ServerSettings,
): string | undefined {
  for (const path of SETTINGS_INTERVAL_PATHS) {
    const millis = intervalMillis(getPath(next, path));
    if (millis === undefined || isUsableIntervalMillis(millis)) continue;
    if (Object.is(millis, intervalMillis(getPath(current, path)))) continue;
    return `${path.join(".")} must be from 0 to ${MAX_SETTINGS_INTERVAL_MILLIS} milliseconds, not ${millis}: pick a smaller interval (0 turns it off where the setting allows).`;
  }
  return undefined;
}

/**
 * The parsed settings file with each interval it holds that no run could use (not a number, `null`,
 * negative, not finite or too large) left out, so that one setting decodes as its default and the
 * rest load; `dropped` names each one with what the file held. A path through a non-object is left
 * to the decode.
 */
export function dropUnusableStoredIntervals(parsed: unknown): {
  readonly settings: unknown;
  readonly dropped: ReadonlyArray<string>;
} {
  let settings = parsed;
  const dropped: Array<string> = [];
  for (const path of SETTINGS_INTERVAL_PATHS) {
    const parent = getPath(settings, path.slice(0, -1));
    const key = path.at(-1);
    if (key === undefined || !isRecord(parent) || !Object.hasOwn(parent, key)) continue;
    const value = parent[key];
    if (typeof value === "number" && isUsableIntervalMillis(value)) continue;
    dropped.push(
      `${path.join(".")} (${typeof value === "number" ? String(value) : (JSON.stringify(value) ?? typeof value)})`,
    );
    settings = withoutPath(settings, path);
  }
  return { settings, dropped };
}

/** A copy of `value` without the key at `path`; each object on the way is copied, not changed. */
function withoutPath(value: unknown, path: ReadonlyArray<string>): unknown {
  const [key, ...rest] = path;
  if (key === undefined || !isRecord(value)) return value;
  if (rest.length === 0) {
    const { [key]: _removed, ...kept } = value;
    return kept;
  }
  return { ...value, [key]: withoutPath(value[key], rest) };
}
