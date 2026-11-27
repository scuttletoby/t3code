// Fork (work panel, fork issue 1136): the panel in a browser, with the shells, the setting and the
// server's answer faked. Each test names the question it answers.
import type { ForkWorkPanelAnswer, ThreadPullRequestLink } from "@t3tools/contracts";
import * as Cause from "effect/Cause";
import { AsyncResult, Atom } from "effect/reactivity";
import { describe, expect, it, vi } from "vite-plus/test";
import { userEvent } from "vite-plus/test/browser";

import "~/index.css";
import { render } from "../../../../test/render";

const NOW = Date.parse("2026-11-27T12:00:00Z");
const MINUTE = 60_000;

const state = vi.hoisted(() => ({
  enabled: false,
  shells: [] as Array<unknown>,
  result: null as unknown,
  open: vi.fn(async (_url: string) => {}),
  updateClient: vi.fn(async (_patch: unknown) => {}),
}));

vi.mock(import("../../../hooks/useSettings"), async (original) => ({
  ...(await original()),
  useClientSettings: ((select: (settings: { workPanelEnabled: boolean }) => unknown) =>
    select({ workPanelEnabled: state.enabled })) as never,
  updateClientSettings: state.updateClient as never,
}));
vi.mock(import("../../../state/entities"), async (original) => ({
  ...(await original()),
  useThreadShells: (() => state.shells) as never,
}));
vi.mock(import("../../../browser/useOpenLink"), () => ({
  useOpenLink: (() => state.open) as never,
}));
vi.mock(import("./workPanelAtom"), () => ({
  forkWorkPanelAtom: (() => Atom.make(state.result)) as never,
}));

import { ForkWorkPanel, REASON_SERVER_UNANSWERED } from "./ForkWorkPanel";
import { WorkPanelSettings } from "../../settings/fork/WorkPanelSettings";

const link = (number: number, over: Partial<ThreadPullRequestLink> = {}) =>
  ({
    host: "github.com",
    repository: "owner/repo",
    number,
    url: `https://github.com/owner/repo/pull/${number}`,
    source: "agent",
    linkedAt: new Date(NOW).toISOString(),
    snapshot: { title: `Change ${number}`, headBranch: `b${number}`, checksState: "pending" },
    stack: null,
    ...over,
  }) as unknown as ThreadPullRequestLink;

const shell = (id: string, parent: string | null, pullRequests: Array<ThreadPullRequestLink>) => ({
  environmentId: "env",
  id,
  title: `Thread ${id}`,
  lineage: { parentThreadId: parent },
  pullRequests,
});

const answer = (over: Partial<ForkWorkPanelAnswer> = {}): ForkWorkPanelAnswer => ({
  answeredAt: NOW,
  repoQueues: [
    {
      host: "github.com",
      repository: "owner/repo",
      state: "answered",
      readAt: NOW - MINUTE,
      queued: 3,
      running: 2,
      oldestQueuedAt: NOW - 12 * MINUTE,
      oldestQueuedUrl: "https://github.com/owner/repo/actions/runs/9",
      scanned: 100,
      nextReadAt: NOW,
    },
  ],
  pullRequests: [
    {
      host: "github.com",
      repository: "owner/repo",
      number: 1,
      queued: 1,
      running: 1,
      failed: 1,
      passed: 4,
      oldestQueuedAt: NOW - 12 * MINUTE,
      url: "https://github.com/owner/repo/actions/runs/9",
    },
  ],
  ...over,
});

const panel = (density = "full") => (
  <ForkWorkPanel environmentId={"env" as never} threadId={"parent" as never} density={density} />
);

const setUp = (input: { enabled: boolean; result: unknown; shells?: Array<unknown> }) => {
  state.enabled = input.enabled;
  state.result = input.result;
  state.shells = input.shells ?? [
    shell("parent", null, [link(1)]),
    shell("child", "parent", [
      link(2, {
        watch: {
          failedChecks: ["lint"],
          passedChecks: ["unit"],
          checksPending: true,
          checksAwaitedSince: null,
        } as never,
      }),
    ]),
    shell("unrelated", null, [link(3)]),
  ];
  vi.setSystemTime(NOW);
};

describe("the work panel", () => {
  it("is off by default: the side panel shows nothing new", async () => {
    setUp({ enabled: false, result: AsyncResult.success(answer()) });
    await using screen = await render(panel());
    expect(screen.container.querySelector("[data-fork-work-panel]")).toBeNull();
  });

  it("on: the repo queue line and each linked PR of the thread and its sub-agents", async () => {
    setUp({ enabled: true, result: AsyncResult.success(answer()) });
    await using screen = await render(panel());
    const queue = screen.getByTestId("fork-work-panel-repo-queue");
    await expect.element(queue).toBeVisible();
    expect(queue.element().textContent).toContain("3 queued · 2 running · oldest queued 12 min");
    await expect.element(queue).toHaveAttribute("data-state", "answered");
    const rows = screen.getByTestId("fork-work-panel-pr").elements();
    // The unrelated thread's PR #3 is not this thread's.
    expect(rows.map((row) => row.getAttribute("data-source"))).toEqual(["runs", "watch"]);
    expect(rows[0]!.textContent).toContain("1 queued (12 min) · 1 running · 1 failed · 4 passed");
    expect(rows[1]!.textContent).toContain(
      "checks pending · 1 failed · 1 passed · from Thread child",
    );
    await userEvent.click(screen.getByRole("link", { name: "run" }));
    expect(state.open).toHaveBeenCalledWith(
      "https://github.com/owner/repo/actions/runs/9",
      expect.anything(),
    );
  });

  it("GitHub rate-limited the server: the line is unknown with why, PRs fall back to the watch", async () => {
    setUp({
      enabled: true,
      result: AsyncResult.success(
        answer({
          repoQueues: [
            {
              host: "github.com",
              repository: "owner/repo",
              state: "unknown",
              reason: "GitHub refused the read: this server's rate limit is used up.",
              nextReadAt: NOW + 20 * MINUTE,
            },
          ],
          pullRequests: [],
        }),
      ),
    });
    await using screen = await render(panel());
    const queue = screen.getByTestId("fork-work-panel-repo-queue");
    await expect.element(queue).toHaveAttribute("data-state", "unknown");
    expect(queue.element().textContent).toContain("unknown: GitHub refused the read");
    expect(queue.element().getAttribute("title")).toContain("Next read in 20 min");
    const rows = screen.getByTestId("fork-work-panel-pr").elements();
    expect(rows.map((row) => row.getAttribute("data-source"))).toEqual(["snapshot", "watch"]);
  });

  it("the server does not answer (down, or a version without the panel): unknown, never 0", async () => {
    setUp({ enabled: true, result: AsyncResult.failure(Cause.fail(new Error("no such method"))) });
    await using screen = await render(panel());
    const queue = screen.getByTestId("fork-work-panel-repo-queue");
    await expect.element(queue).toBeVisible();
    expect(queue.element().textContent).toContain(`unknown: ${REASON_SERVER_UNANSWERED}`);
    expect(queue.element().textContent).not.toMatch(/\b0 queued/);
  });

  it("no PRs: says so, with no queue line", async () => {
    setUp({
      enabled: true,
      result: AsyncResult.success(answer({ repoQueues: [], pullRequests: [] })),
      shells: [shell("parent", null, [])],
    });
    await using screen = await render(panel());
    await expect
      .element(screen.getByText("No pull requests are linked to this thread or its sub-agents."))
      .toBeVisible();
    expect(screen.container.querySelector("[data-testid=fork-work-panel-repo-queue]")).toBeNull();
  });

  it("shows only in the full side panel", async () => {
    setUp({ enabled: true, result: AsyncResult.success(answer()) });
    await using screen = await render(panel("compact"));
    expect(screen.container.querySelector("[data-fork-work-panel]")).toBeNull();
  });
});

describe("the work panel's switch", () => {
  it("saves workPanelEnabled on this device", async () => {
    setUp({ enabled: false, result: null });
    await using screen = await render(<WorkPanelSettings />);
    const toggle = screen.getByRole("switch", { name: "Work panel" });
    await expect.element(toggle).not.toBeChecked();
    await userEvent.click(toggle);
    expect(state.updateClient).toHaveBeenCalledWith({ workPanelEnabled: true });
  });
});
