// The calm view's rows in the sidebar (CAL3): role rows with their badge on top, the "Other
// threads" header with its counts and toggle, the hint when no thread has a role, and upstream's
// "Show more" of the Settled shelf hidden while the group is closed.
import type { EnvironmentThreadShell } from "@t3tools/client-runtime/state/models";
import { describe, expect, it, vi } from "vite-plus/test";
import { userEvent } from "vite-plus/test/browser";

import "~/index.css";
import { render } from "../../../../test/render";
import type { SidebarSection } from "../../Sidebar.logic";
import type { CalmView } from "./calmSidebar.logic";
import { calmTopItems } from "./CalmSidebar";

const shell = (id: string) =>
  ({ environmentId: "local", id, title: `Thread ${id}` }) as unknown as EnvironmentThreadShell;

const renderRow = (thread: EnvironmentThreadShell, section: SidebarSection) => (
  <li key={thread.id} className="list-none" data-section={section}>
    {thread.title}
  </li>
);

const calm = (open: boolean, routeOther: EnvironmentThreadShell | null = null) =>
  ({
    kind: "calm",
    roleRows: [
      { thread: shell("a"), role: "assistant" },
      { thread: shell("o"), role: "orchestrator" },
    ],
    roleKeys: new Set(["local:a", "local:o"]),
    otherCount: 5,
    waitingCount: 2,
    open,
    routeOther,
  }) as CalmView<EnvironmentThreadShell>;

function List(props: { readonly view: CalmView<EnvironmentThreadShell>; onToggle: () => void }) {
  return (
    <ul>
      {calmTopItems(props.view, renderRow, new Map([["local:o", "settled"]]), props.onToggle)}
      {/* Upstream's list follows; its Settled "Show more" sits after it. */}
      <li className="list-none">
        <button type="button" data-sidebar-row="settled:more">
          Show 25 more
        </button>
      </li>
    </ul>
  );
}

describe("calm sidebar rows (CAL3)", () => {
  it("puts the role rows on top with their badge, then Other threads with its counts", async () => {
    const onToggle = vi.fn();
    await using screen = await render(<List view={calm(false)} onToggle={onToggle} />, {
      router: false,
    });
    const text = screen.container.textContent ?? "";
    expect(text.indexOf("Assistant")).toBeLessThan(text.indexOf("Thread a"));
    expect(text.indexOf("Thread a")).toBeLessThan(text.indexOf("Orchestrator"));
    expect(text.indexOf("Orchestrator")).toBeLessThan(text.indexOf("Thread o"));
    // A role row keeps its own section (a settled orchestrator stays un-settle-able).
    await expect.element(screen.getByText("Thread o")).toHaveAttribute("data-section", "settled");
    await expect.element(screen.getByText("Thread a")).toHaveAttribute("data-section", "active");

    const toggle = screen.getByTestId("fork-calm-other-toggle");
    await expect.element(toggle).toHaveTextContent("Other threads2 waiting · 5");
    await expect.element(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("closed: hides the Settled shelf's Show more; open: leaves it", async () => {
    await using closed = await render(<List view={calm(false)} onToggle={() => {}} />, {
      router: false,
    });
    await expect.element(closed.getByText("Show 25 more")).not.toBeVisible();
    await closed.rerender(<List view={calm(true)} onToggle={() => {}} />);
    await expect.element(closed.getByText("Show 25 more")).toBeVisible();
  });

  it("closed: keeps the open thread in sight below the header", async () => {
    await using screen = await render(<List view={calm(false, shell("x"))} onToggle={() => {}} />, {
      router: false,
    });
    const text = screen.container.textContent ?? "";
    expect(text.indexOf("Other threads")).toBeLessThan(text.indexOf("Thread x"));
  });

  it("gives a hint while no thread has a role, and nothing while calm is off", async () => {
    await using screen = await render(<List view={{ kind: "hint" }} onToggle={() => {}} />, {
      router: false,
    });
    await expect
      .element(screen.getByText(/no thread has a role yet/))
      .toHaveTextContent(
        "Calm view is on, but no thread has a role yet. Give one from a thread's menu: Thread role.",
      );
    expect(calmTopItems({ kind: "off" }, renderRow, new Map(), () => {})).toEqual([]);
  });
});
