// The playtest chip (F5) above the composer: does a thread with a session that is on show it, does
// End end that session through the end route, and does the chip go when the session ends any other
// way? The server calls are stubbed; the chip renders in upstream's banner stack.
import type { PlaytestSessionView, ScopedThreadRef } from "@t3tools/contracts";
import { describe, expect, it, vi } from "vite-plus/test";
import { userEvent } from "vite-plus/test/browser";

import "~/index.css";
import { render } from "../../../../test/render";
import { ComposerBannerStack } from "../../chat/ComposerBannerStack";

const state = vi.hoisted(() => ({
  updatedAt: "2026-11-27T10:00:00.000Z" as string | null,
  session: null as PlaytestSessionView | null,
  read: vi.fn(),
  end: vi.fn(),
  toast: vi.fn(),
}));

vi.mock(import("../../ui/toast"), async (original) => ({
  ...(await original()),
  toastManager: { add: state.toast } as never,
}));

vi.mock(import("../../../state/entities"), async (original) => ({
  ...(await original()),
  useQuietThreadShell: (() =>
    state.updatedAt === null ? null : { updatedAt: state.updatedAt }) as never,
}));
vi.mock(import("./playtestApi"), () => ({
  onPrimaryEnvironment: () => true,
  readPlaytestSession: ((ref: ScopedThreadRef) => {
    state.read(ref);
    return Promise.resolve(state.session);
  }) as never,
  endPlaytest: ((ref: ScopedThreadRef) => {
    state.end(ref);
    // The server ends it; the next read finds none.
    const ended = state.session === null ? null : { ...state.session, state: "ended" as const };
    state.session = null;
    return Promise.resolve(
      ended === null
        ? { outcome: "nothing-to-end", session: null }
        : { outcome: "ended", session: ended },
    );
  }) as never,
}));

const { useForkPlaytestItems, playtestChipItem } = await import("./usePlaytestChipItems");

/** Well under the 10 s poll: the chip must go on the re-read, not on the next poll. */
const QUICKER_THAN_POLL = 2_000;

const REF = { environmentId: "env-a", threadId: "thread-a" } as unknown as ScopedThreadRef;

const session = (patch: Record<string, unknown> = {}) =>
  ({
    id: "pt0000000001",
    threadId: "thread-a",
    projectId: "project-a",
    target: "staging",
    note: "",
    worktree: "/work/tree",
    state: "active",
    startedAt: "2026-11-27T10:00:00.000Z",
    lastActivityAt: "2026-11-27T10:05:00.000Z",
    rounds: 2,
    lastRound: {
      number: 2,
      state: "done",
      step: "deploy",
      detail: "",
      startedAt: "2026-11-27T10:04:00.000Z",
      endedAt: "2026-11-27T10:05:00.000Z",
    },
    ...patch,
  }) as unknown as PlaytestSessionView;

function Chip({ threadRef }: { readonly threadRef: ScopedThreadRef | null }) {
  return <ComposerBannerStack items={useForkPlaytestItems(threadRef)} />;
}

describe("playtest chip (F5)", () => {
  it("shows a session that is on, and End ends it through the end route", async () => {
    state.updatedAt = "2026-11-27T10:00:00.000Z";
    state.session = session();
    state.end.mockClear();
    await using screen = await render(<Chip threadRef={REF} />, { router: false });

    await expect.element(screen.getByText("Playtest on: staging")).toBeVisible();
    await expect.element(screen.getByText("Round 2 done (deploy).")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "End" }));

    expect(state.end).toHaveBeenCalledWith(REF);
    expect(state.end).toHaveBeenCalledTimes(1);
    await vi.waitFor(() =>
      expect(state.toast).toHaveBeenCalledWith(
        expect.objectContaining({ title: "Playtest ended" }),
      ),
    );
    // Read again after the press: the session is gone, so is the chip.
    await expect
      .element(screen.getByText("Playtest on: staging"), { timeout: QUICKER_THAN_POLL })
      .not.toBeInTheDocument();
  });

  it("goes when the session ends another way, read again on the thread's next change", async () => {
    state.updatedAt = "2026-11-27T11:00:00.000Z";
    state.session = session();
    await using screen = await render(<Chip threadRef={REF} />, { router: false });
    await expect.element(screen.getByText("Playtest on: staging")).toBeVisible();

    // The agent's end (or idle, a time limit): T3's notice or the tool call changes the thread.
    state.session = null;
    state.updatedAt = "2026-11-27T11:01:00.000Z";
    await screen.rerender(<Chip threadRef={REF} />);
    await expect
      .element(screen.getByText("Playtest on: staging"), { timeout: QUICKER_THAN_POLL })
      .not.toBeInTheDocument();
  });

  it("shows nothing for a thread with no session, and nothing for a draft", async () => {
    state.updatedAt = "2026-11-27T12:00:00.000Z";
    state.session = null;
    state.read.mockClear();
    await using screen = await render(<Chip threadRef={REF} />, { router: false });
    await vi.waitFor(() => expect(state.read).toHaveBeenCalledWith(REF));
    expect(screen.container.textContent).not.toContain("Playtest");

    // A draft has no shell yet: nothing is read.
    state.updatedAt = null;
    state.read.mockClear();
    await using draft = await render(<Chip threadRef={REF} />, { router: false });
    expect(draft.container.textContent).not.toContain("Playtest");
    expect(state.read).not.toHaveBeenCalled();
  });

  it("names a session that starts or ends, and a release T3 still tries", () => {
    const onEnd = () => {};
    expect(playtestChipItem(null, { ending: false, onEnd })).toBeNull();
    expect(playtestChipItem(session({ state: "ended" }), { ending: false, onEnd })).toBeNull();
    expect(
      playtestChipItem(session({ state: "claiming", lastRound: undefined, running: "claim" }), {
        ending: false,
        onEnd,
      }),
    ).toMatchObject({
      title: "Playtest starting: staging",
      description: "No round yet. A claim step runs now.",
    });
    expect(
      playtestChipItem(session({ state: "ending", problem: "release: exit 1" }), {
        ending: false,
        onEnd,
      }),
    ).toMatchObject({
      title: "Playtest ending: staging",
      description:
        "Round 2 done (deploy). Its release has not worked yet; T3 tries again, or press End again.",
    });
  });
});
