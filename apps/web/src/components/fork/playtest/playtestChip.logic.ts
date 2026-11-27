/**
 * Fork (playtest F5): the thread chip's rules. The chip asks only a server where playtest is on
 * (for the server or any project), so a server with it off costs nothing. Docs: docs/fork/playtest.md.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import type { ForkPlaytestChipSession } from "@t3tools/contracts";

/** How often an open thread asks again, so an end from elsewhere (agent, idle, limit) shows. */
export const PLAYTEST_CHIP_POLL_MS = 15_000;

interface PlaytestOnSettings {
  readonly playtest?: { readonly enabled?: boolean } | null;
  readonly projectSettingsOverrides?: Readonly<
    Record<string, { readonly playtest?: { readonly enabled?: boolean } | null } | undefined>
  >;
}

/** Whether a session can be on anywhere on this server (its settings, or a project's). */
export function playtestMayRun(settings: PlaytestOnSettings | null | undefined): boolean {
  if (settings == null) return false;
  if (settings.playtest?.enabled === true) return true;
  return Object.values(settings.projectSettingsOverrides ?? {}).some(
    (project) => project?.playtest?.enabled === true,
  );
}

/** The chip's words for a session. */
export function playtestChipText(session: ForkPlaytestChipSession): {
  readonly label: string;
  readonly hint: string;
  readonly canEnd: boolean;
} {
  const rounds = session.rounds === 1 ? "1 round" : `${session.rounds} rounds`;
  const note = session.note === "" ? "" : ` (${session.note})`;
  switch (session.state) {
    case "claiming":
      return {
        label: `Playtest: claiming ${session.target}`,
        hint: `T3 is claiming ${session.target}${note}. End stops the claim and releases it.`,
        canEnd: true,
      };
    case "ending":
      return {
        label: `Playtest: ending ${session.target}`,
        hint: `The session on ${session.target} is ending; T3 runs its release and tries again if it fails.`,
        canEnd: false,
      };
    default:
      return {
        label: `Playtest: ${session.target}`,
        hint: `A playtest on ${session.target}${note} is on, ${rounds} so far. End releases the target; the worktree stays.`,
        canEnd: true,
      };
  }
}

/** What to tell the person after End, or nothing when it ended. */
export function playtestEndMessage(outcome: "ended" | "nothing-to-end" | "release-failed"): {
  readonly type: "info" | "warning";
  readonly title: string;
} | null {
  switch (outcome) {
    case "ended":
      return null;
    case "nothing-to-end":
      return { type: "info", title: "The playtest had already ended." };
    case "release-failed":
      return {
        type: "warning",
        title: "The playtest is ending, but its release has not worked yet. T3 tries again.",
      };
  }
}
