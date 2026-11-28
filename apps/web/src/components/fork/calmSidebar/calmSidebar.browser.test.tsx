// Fork (calm sidebar): the calm view's rows in the sidebar and the role badge in the thread header,
// as a person sees them. Each test names the question it answers.
import type { EnvironmentThreadShell } from "@t3tools/client-runtime/state/models";
import { EnvironmentId, ProjectId, ThreadId } from "@t3tools/contracts";
import { Atom } from "effect/reactivity";
import { describe, expect, it, vi } from "vite-plus/test";
import { userEvent } from "vite-plus/test/browser";

import "~/index.css";
import { render } from "../../../../test/render";

const local = EnvironmentId.make("local");
const game = ProjectId.make("project-game");

const shell = (id: string, changes: Record<string, unknown> = {}): EnvironmentThreadShell =>
  ({
    environmentId: local,
    id: ThreadId.make(id),
    projectId: game,
    title: `Thread ${id}`,
    lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: ThreadId.make(id) },
    delegatedFrom: null,
    archivedAt: null,
    deletedAt: null,
    settledOverride: null,
    snoozedUntil: null,
    snoozedAt: null,
    pinnedAt: null,
    hasPendingApprovals: false,
    hasPendingUserInput: false,
    ...changes,
  }) as unknown as EnvironmentThreadShell;

const state = vi.hoisted(() => ({ shell: null as unknown, calm: false }));

vi.mock(import("./threadRolesAtom"), () => ({
  forkThreadRolesAtom: Atom.make(
    new Map([
      [
        "local",
        {
          assistant: "helper",
          orchestrators: new Map([["project-game", "lead"]]),
        },
      ],
    ]),
  ) as never,
}));
vi.mock(import("../../../state/entities"), async (original) => ({
  ...(await original()),
  useThreadShell: (() => state.shell) as never,
  useDelegatedUnderParent: (() => new Set<string>()) as never,
}));
vi.mock(import("../../../hooks/useSettings"), async (original) => ({
  ...(await original()),
  useClientSettings: ((select: (settings: { calmSidebarEnabled: boolean }) => unknown) =>
    select({ calmSidebarEnabled: state.calm })) as never,
}));

import { CalmSidebarTop } from "./CalmSidebarTop";
import { calmSidebarView, calmThreadKey } from "./calmSidebar.logic";
import { ForkThreadRoleBadge } from "./ForkThreadRoleBadge";
import { CALM_OTHER_EXPANDED_KEY, useForkCalmSidebar } from "./useForkCalmSidebar";

const ROLES = new Map([
  [
    local,
    {
      assistant: ThreadId.make("helper"),
      orchestrators: new Map([[game, ThreadId.make("lead")]]),
    },
  ],
]);

const renderRow = (thread: EnvironmentThreadShell, section: string) => (
  <li key={thread.id} data-testid={`row-${thread.id}`}>
    {thread.title} ({section})
  </li>
);

describe("calm sidebar top rows", () => {
  it("shows each role row under its heading, then Other threads with the count and waiting count", async () => {
    const view = calmSidebarView({
      visible: [
        shell("worker"),
        shell("lead", { pinnedAt: "2026-11-01T00:00:00.000Z" }),
        shell("asking", { hasPendingApprovals: true }),
        shell("helper"),
      ],
      roles: ROLES,
      enabled: true,
      searching: false,
    });
    if (view.kind === "off") throw new Error("expected the calm view");
    const onToggle = vi.fn();
    await using screen = await render(
      <ul>
        <CalmSidebarTop view={view} expanded={false} onToggle={onToggle} render={renderRow} />
      </ul>,
    );
    const text = screen.container.textContent ?? "";
    expect(text.indexOf("Assistant")).toBeLessThan(text.indexOf("Thread helper"));
    expect(text.indexOf("Thread helper")).toBeLessThan(text.indexOf("Orchestrator"));
    expect(text.indexOf("Orchestrator")).toBeLessThan(text.indexOf("Thread lead"));
    // A role row is drawn as its own section, so its row actions (unpin here) stay right.
    await expect.element(screen.getByTestId("row-lead")).toHaveTextContent("Thread lead (pinned)");
    await expect.element(screen.getByTestId("row-worker")).not.toBeInTheDocument();

    const toggle = screen.getByRole("button", { name: /Other threads \(2\)/ });
    await expect.element(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle.element().textContent).toBe("Other threads (2)1 waiting");
    await userEvent.click(toggle);
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("calm on with no role held: the hint names where to make one, and nothing folds", async () => {
    await using screen = await render(
      <ul>
        <CalmSidebarTop
          view={{ kind: "no-roles" }}
          expanded={false}
          onToggle={() => {}}
          render={renderRow}
        />
      </ul>,
    );
    const hint = screen.getByTestId("fork-calm-no-roles");
    await expect.element(hint).toBeInTheDocument();
    expect(hint.element().textContent).toContain("no thread holds a role yet");
    expect(hint.element().textContent).toContain("pick Role");
    await expect.element(screen.getByRole("button")).not.toBeInTheDocument();
  });
});

describe("thread header role badge", () => {
  const ref = { environmentId: local, threadId: ThreadId.make("lead") };

  it("the project's orchestrator shows Orchestrator", async () => {
    state.shell = shell("lead");
    await using screen = await render(<ForkThreadRoleBadge threadRef={ref} />);
    await expect
      .element(screen.getByTestId("fork-thread-role-badge"))
      .toHaveTextContent("Orchestrator");
  });

  it("the assistant shows Assistant", async () => {
    state.shell = shell("helper");
    await using screen = await render(
      <ForkThreadRoleBadge
        threadRef={{ environmentId: local, threadId: ThreadId.make("helper") }}
      />,
    );
    await expect
      .element(screen.getByTestId("fork-thread-role-badge"))
      .toHaveTextContent("Assistant");
  });

  it("a thread without a role, or an archived role thread, shows no badge", async () => {
    state.shell = shell("worker");
    await using plain = await render(<ForkThreadRoleBadge threadRef={ref} />);
    await expect.element(plain.getByTestId("fork-thread-role-badge")).not.toBeInTheDocument();
    state.shell = shell("lead", { archivedAt: "2026-11-01T00:00:00.000Z" });
    await using archived = await render(<ForkThreadRoleBadge threadRef={ref} />);
    await expect.element(archived.getByTestId("fork-thread-role-badge")).not.toBeInTheDocument();
  });
});

const SETTLED = { settledOverride: "settled" };

describe("the sidebar's calm hook", () => {
  const LIST = [shell("worker"), shell("lead"), shell("helper"), shell("done", SETTLED)];

  function Probe(props: { readonly routeKey: string | null }) {
    const calm = useForkCalmSidebar({
      listedThreads: LIST,
      scopedProjectKeys: null,
      isSearchingThreads: false,
    });
    const input = calm.sidebarInput(props.routeKey);
    return (
      <div>
        <p data-testid="input">
          {input.threads === undefined
            ? "upstream list"
            : input.threads.map((thread) => thread.id).join(",")}
        </p>
        <ul>{calm.topRows(renderRow)}</ul>
        <p data-testid="role-thread">{calm.roleThread(calmThreadKey(LIST[1]!))?.id ?? "none"}</p>
      </div>
    );
  }

  it("calm off: the sidebar's list input is left as it is and nothing is drawn on top", async () => {
    state.calm = false;
    await using screen = await render(<Probe routeKey={null} />);
    await expect.element(screen.getByTestId("input")).toHaveTextContent("upstream list");
    await expect.element(screen.getByRole("button")).not.toBeInTheDocument();
  });

  it("calm on: role rows leave the list, and the folded threads too until the group is opened", async () => {
    state.calm = true;
    localStorage.removeItem(CALM_OTHER_EXPANDED_KEY);
    await using screen = await render(<Probe routeKey={null} />);
    // Settled threads stay in upstream's own Settled shelf.
    await expect.element(screen.getByTestId("input")).toHaveTextContent("done");
    expect(screen.getByTestId("input").element().textContent).toBe("done");
    await expect.element(screen.getByTestId("role-thread")).toHaveTextContent("lead");
    await userEvent.click(screen.getByRole("button", { name: /Other threads/ }));
    await expect.element(screen.getByTestId("input")).toHaveTextContent("worker,done");
    localStorage.removeItem(CALM_OTHER_EXPANDED_KEY);
  });
});
