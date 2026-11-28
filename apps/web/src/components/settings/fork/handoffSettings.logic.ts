/** Fork: the pure parts of the handoff panel (`handoff`, server and project). */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own settings are English only; upstream's translation catalogs hold no fork keys. */
import {
  HANDOFF_MAX_TOKENS,
  HANDOFF_MIN_TOKENS,
  HANDOFF_THREADS_MAX,
  type HandoffMode,
  type HandoffSettings,
} from "@t3tools/contracts";

export const HANDOFF_MODE_OPTIONS: ReadonlyArray<{
  readonly value: HandoffMode;
  readonly label: string;
  readonly description: string;
}> = [
  {
    value: "off",
    label: "Off",
    description: "Off: long threads compact as T3's own settings say.",
  },
  {
    value: "handoff",
    label: "Handoff",
    description:
      "Handoff: a long thread moves to a fresh session in the same thread with a short handoff note, so its sub-agents, pull requests and waits keep working.",
  },
  {
    value: "compact",
    label: "Compact",
    description:
      "Compact: same as Off; use it to keep one project or thread out of a Handoff set above it.",
  },
];

export const handoffModeLabel = (mode: HandoffMode) =>
  HANDOFF_MODE_OPTIONS.find((option) => option.value === mode)?.label ?? mode;

/** The roles row's choice: a mode, or `inherit` (no `roles`: the mode above decides). */
export type HandoffRolesChoice = HandoffMode | "inherit";

export const HANDOFF_ROLES_INHERIT_LABEL = "Same as above";

export const handoffRolesLabel = (choice: HandoffRolesChoice) =>
  choice === "inherit" ? HANDOFF_ROLES_INHERIT_LABEL : handoffModeLabel(choice);

/** The whole new value with the roles mode set, or removed with `inherit`. */
export function withRolesMode(
  settings: HandoffSettings,
  choice: HandoffRolesChoice,
): HandoffSettings {
  const { roles: _old, ...rest } = settings;
  return choice === "inherit" ? rest : { ...rest, roles: choice };
}

/**
 * A typed handoff size as the server's save takes it: between the minimum and the server's limit,
 * so the field never offers a value the save refuses. `allowZero` keeps the idle size's 0 (never).
 */
export function boundHandoffTokens(value: number, allowZero: boolean): number {
  if (allowZero && value === 0) return 0;
  return Math.min(HANDOFF_MAX_TOKENS, Math.max(HANDOFF_MIN_TOKENS, value));
}

/** The whole new value with one thread's mode set, or removed with `null`. */
export function withThreadMode(
  settings: HandoffSettings,
  threadId: string,
  mode: HandoffMode | null,
): HandoffSettings {
  const { [threadId]: _old, ...rest } = settings.threads;
  return { ...settings, threads: mode === null ? rest : { ...rest, [threadId]: mode } };
}

/** Why a thread cannot be added, or null. */
export function threadModeProblem(settings: HandoffSettings, threadId: string): string | null {
  const id = threadId.trim();
  if (id === "") return "Enter a thread id.";
  if (Object.hasOwn(settings.threads, id)) return "This thread already has its own mode.";
  if (Object.keys(settings.threads).length >= HANDOFF_THREADS_MAX) {
    return `At most ${HANDOFF_THREADS_MAX} threads can have their own mode. Remove one first.`;
  }
  return null;
}

export interface HandoffThreadRow {
  readonly threadId: string;
  readonly title: string | null;
  readonly mode: HandoffMode;
}

/** The per-thread modes, titled where this client knows the thread, by title then id. */
export function handoffThreadRows(
  threads: Readonly<Record<string, HandoffMode>>,
  titles: ReadonlyMap<string, string>,
): ReadonlyArray<HandoffThreadRow> {
  return Object.entries(threads)
    .map(([threadId, mode]) => ({ threadId, title: titles.get(threadId) ?? null, mode }))
    .toSorted(
      (a, b) =>
        Number(a.title === null) - Number(b.title === null) ||
        (a.title ?? "").localeCompare(b.title ?? "") ||
        a.threadId.localeCompare(b.threadId),
    );
}

/** One argument per line, the program first; blank lines dropped. Empty: no command. */
export function parseArgvLines(text: string): ReadonlyArray<string> | undefined {
  const argv = text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
  return argv.length === 0 ? undefined : argv;
}

export const formatArgvLines = (argv: ReadonlyArray<string> | undefined) => (argv ?? []).join("\n");

/** Why the source and command cannot be saved together, or null. */
export function handoffSourceProblem(
  source: HandoffSettings["source"],
  command: ReadonlyArray<string> | undefined,
): string | null {
  return source === "command" && command === undefined
    ? "Written by a command needs a command: its program on the first line, one argument per line."
    : null;
}
