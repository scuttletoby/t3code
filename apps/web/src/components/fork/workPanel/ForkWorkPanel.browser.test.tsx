// Fork (work panel, fork issue 1136): the panel in a browser, with the shells, the setting and the
// server's answer faked. Each test names the question it answers.
import type { ForkWorkPanelAnswer, ThreadPullRequestLink } from "@t3tools/contracts";
import * as Cause from "effect/Cause";
import * as Option from "effect/Option";
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
  fleet: [] as Array<unknown>,
  accounts: [] as Array<unknown>,
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
  forkFleetAtom: (() => Atom.make(state.fleet)) as never,
  fleetKey: () => "fleet",
  useForkUsageAccounts: (() => state.accounts) as never,
}));

import { ForkWorkPanel } from "./ForkWorkPanel";
import { REASON_SERVER_UNANSWERED } from "./workPanel.logic";
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

const setUp = (input: {
  enabled: boolean;
  result: unknown;
  shells?: Array<unknown>;
  fleet?: Array<unknown>;
  accounts?: Array<unknown>;
}) => {
  state.enabled = input.enabled;
  state.result = input.result;
  state.fleet = input.fleet ?? [];
  state.accounts = input.accounts ?? [];
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
    // Where the line comes from, and when the server reads again.
    expect(queue.element().textContent).toContain("Next read in 20 min");
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

  it("answered once, then the refresh fails: grey unknown, not the old counts", async () => {
    setUp({
      enabled: true,
      result: AsyncResult.failure(Cause.fail(new Error("offline")), {
        previousSuccess: Option.some(AsyncResult.success(answer())),
      }),
    });
    await using screen = await render(panel());
    const queue = screen.getByTestId("fork-work-panel-repo-queue");
    await expect.element(queue).toHaveAttribute("data-state", "unknown");
    expect(queue.element().textContent).toContain(REASON_SERVER_UNANSWERED);
    expect(queue.element().textContent).not.toContain("3 queued");
    const rows = screen.getByTestId("fork-work-panel-pr").elements();
    expect(rows.map((row) => row.getAttribute("data-source"))).not.toContain("runs");
  });

  it("an answer received over three refreshes ago (a hung read): unknown with its age", async () => {
    setUp({
      enabled: true,
      result: AsyncResult.success(answer({ answeredAt: NOW - 5 * MINUTE }), {
        timestamp: NOW - 5 * MINUTE,
      }),
    });
    await using screen = await render(panel());
    const queue = screen.getByTestId("fork-work-panel-repo-queue");
    await expect.element(queue).toHaveAttribute("data-state", "unknown");
    expect(queue.element().textContent).toContain("last answer is 5 min old");
  });

  it("ages keep counting after the answer arrived", async () => {
    // Received two minutes ago; GitHub had answered the server a minute before that.
    setUp({
      enabled: true,
      result: AsyncResult.success(
        answer({
          answeredAt: NOW - 2 * MINUTE,
          repoQueues: [
            {
              host: "github.com",
              repository: "owner/repo",
              state: "answered",
              readAt: NOW - 3 * MINUTE,
              queued: 3,
              running: 2,
              oldestQueuedAt: NOW - 14 * MINUTE,
              scanned: 100,
            },
          ],
        }),
        { timestamp: NOW - 2 * MINUTE },
      ),
    });
    await using screen = await render(panel());
    const queue = screen.getByTestId("fork-work-panel-repo-queue");
    await expect.element(queue).toBeVisible();
    expect(queue.element().textContent).toContain("GitHub answered 3 min ago");
    expect(queue.element().textContent).toContain("oldest queued 14 min");
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

const GB = 1024;
const withCapacity = answer({
  capacity: {
    agents: { state: "answered", used: 3, total: 8, source: "This machine's agent slot gate." },
    memoryMb: { state: "answered", used: 6 * GB, total: 16 * GB, source: "Host sampler." },
    memoryFloorMb: 2 * GB,
    factsAt: NOW,
  },
});
const fleetMachine = (id: string, status: unknown) => ({
  environmentId: id,
  label: `Machine ${id}`,
  isThisMachine: id === "env",
  status,
});

describe("the capacity bar", () => {
  it("one bar split per machine; an unreachable and an old machine are grey and unknown", async () => {
    setUp({
      enabled: true,
      result: AsyncResult.success(withCapacity),
      fleet: [
        fleetMachine("env", { kind: "answered", answer: withCapacity }),
        fleetMachine("off", { kind: "unreachable" }),
        fleetMachine("old", { kind: "answered", answer: answer() }),
      ],
    });
    await using screen = await render(panel());
    await expect
      .element(screen.getByTestId("fork-work-panel-agents"))
      .toHaveTextContent("Agents: 3 of 8 in use · 2 machines unknown");
    await expect
      .element(screen.getByTestId("fork-work-panel-memory"))
      .toHaveTextContent("Memory free: 10 GB of 16 GB · 2 machines unknown");
    const segments = screen.getByRole("listitem", { name: /^Machine / }).elements();
    expect(segments.map((segment) => segment.getAttribute("data-state"))).toEqual([
      "answered",
      "unknown",
      "unknown",
    ]);
    await userEvent.click(screen.getByRole("button", { name: "Per machine" }));
    const rows = screen.getByTestId("fork-work-panel-machine").elements();
    expect(rows[0]!.textContent).toContain("Agents 3 of 8 in use");
    expect(rows[1]!.textContent).toContain("Not connected to this machine.");
    expect(rows[2]!.textContent).toContain("This machine's version does not report capacity.");
    expect(rows.slice(1).map((row) => row.getAttribute("data-state"))).toEqual([
      "unknown",
      "unknown",
    ]);
  });

  it("an empty fleet: unknown, never 0 or 100", async () => {
    setUp({ enabled: true, result: AsyncResult.success(answer()), fleet: [] });
    await using screen = await render(panel());
    await expect.element(screen.getByTestId("fork-work-panel-no-fleet")).toBeVisible();
    expect(screen.container.querySelector("[data-testid=fork-work-panel-agents]")).toBeNull();
  });

  it("CI runners: busy from the repo line, free unknown", async () => {
    setUp({ enabled: true, result: AsyncResult.success(answer()) });
    await using screen = await render(panel());
    await expect
      .element(screen.getByTestId("fork-work-panel-ci-runners"))
      .toHaveTextContent("CI runners: 2 busy · free unknown");
  });

  it("usage: each account's session and weekly room left; a missing one is unknown", async () => {
    setUp({
      enabled: true,
      result: AsyncResult.success(answer()),
      accounts: [
        {
          key: "a",
          driver: "claudeAgent",
          displayName: "Claude",
          email: undefined,
          limits: { windows: [{ id: "s", kind: "session", label: "5h", usedPercent: 38 }] },
        },
      ],
    });
    await using screen = await render(panel());
    await expect
      .element(screen.getByTestId("fork-work-panel-usage-session"))
      .toHaveTextContent("Session62% left");
    const weekly = screen.getByTestId("fork-work-panel-usage-weekly");
    await expect.element(weekly).toHaveAttribute("data-state", "unknown");
    await expect.element(weekly).toHaveTextContent("Weeklyunknown");
  });

  it("no account reports limits: says unknown", async () => {
    setUp({ enabled: true, result: AsyncResult.success(answer()) });
    await using screen = await render(panel());
    await expect.element(screen.getByTestId("fork-work-panel-no-usage")).toBeVisible();
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
