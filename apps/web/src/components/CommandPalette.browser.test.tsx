import { EnvironmentId, ProjectId, ThreadId } from "@t3tools/contracts";
import { describe, expect, it, vi } from "vite-plus/test";
import { page, userEvent } from "vite-plus/test/browser";

import { act, render } from "../../test/render";
import { openCommandPalette } from "../commandPaletteBus";
import { useCommandPaletteStore } from "../commandPaletteStore";
import { makeThreadFixture } from "../test-fixtures";
import type { Project, Thread } from "../types";
import { CommandPalette } from "./CommandPalette";

const entities = vi.hoisted(() => ({
  projects: [] as ReadonlyArray<Project>,
  threads: [] as ReadonlyArray<Thread>,
}));
vi.mock("../state/entities", async (original) => ({
  ...(await original<typeof import("../state/entities")>()),
  useProjects: () => entities.projects,
  useVisibleThreadShells: () => entities.threads,
}));

function setup(state: { projects?: ReadonlyArray<Project>; threads?: ReadonlyArray<Thread> } = {}) {
  useCommandPaletteStore.setState(useCommandPaletteStore.getInitialState(), true);
  entities.projects = state.projects ?? [];
  entities.threads = state.threads ?? [];
}

describe("CommandPalette open intents", () => {
  it("drags a searched thread into the current composer without navigating", async () => {
    const environmentId = EnvironmentId.make("remote");
    const threadId = ThreadId.make("dragged-thread");
    setup({
      threads: [makeThreadFixture({ environmentId, id: threadId, title: "Drag this thread" })],
    });
    const onDrop = vi.fn();
    await using screen = await render(
      <CommandPalette>
        <div
          data-thread-context-drop="true"
          ref={(node) => {
            node?.addEventListener("t3-thread-context-drop", onDrop);
          }}
          style={{ position: "fixed", bottom: 0, width: 300, height: 100 }}
        >
          Composer
        </div>
      </CommandPalette>,
    );
    const pathname = screen.router?.state.location.pathname;
    openCommandPalette({ query: "Drag this thread" });
    const row = page.getByRole("option", { name: /Drag this thread/ });
    await expect.element(row).toBeVisible();
    const bounds = row.element().getBoundingClientRect();
    const pointer = (type: string, x: number, y: number) =>
      new PointerEvent(type, {
        bubbles: true,
        cancelable: true,
        pointerId: 1,
        isPrimary: true,
        button: 0,
        buttons: type === "pointerup" ? 0 : 1,
        clientX: x,
        clientY: y,
      });
    await act(() => {
      row.element().dispatchEvent(pointer("pointerdown", bounds.x + 20, bounds.y + 10));
      document.dispatchEvent(pointer("pointermove", bounds.x + 30, bounds.y + 20));
    });
    expect(useCommandPaletteStore.getState().open).toBe(false);
    await act(() => {
      document.dispatchEvent(pointer("pointermove", 50, window.innerHeight - 30));
      document.dispatchEvent(pointer("pointerup", 50, window.innerHeight - 30));
    });
    expect(onDrop).toHaveBeenCalledOnce();
    expect((onDrop.mock.calls[0]![0] as CustomEvent).detail).toEqual([{ environmentId, threadId }]);
    expect(screen.router?.state.location.pathname).toBe(pathname);
  });

  it("opens New thread in... with focus in the search input, not the Back button", async () => {
    setup();
    await using _screen = await render(<CommandPalette>{null}</CommandPalette>);

    openCommandPalette({ open: "new-thread-in" });

    await expect.element(page.getByPlaceholder("Search...")).toHaveFocus();
  });

  it("highlights the first project when New thread in... opens from the root list", { repeats: 10 }, async () => {
    const environmentId = EnvironmentId.make("local");
    const project = (id: string, title: string): Project => ({
      id: ProjectId.make(id),
      environmentId,
      title,
      workspaceRoot: `/work/${id}`,
      repositoryIdentity: null,
      defaultModelSelection: null,
      createdAt: "2026-03-01T00:00:00.000Z",
      updatedAt: "2026-03-01T00:00:00.000Z",
      scripts: [],
    });
    setup({
      projects: [project("acme", "Acme"), project("beta", "Beta"), project("gamma", "Gamma")],
    });
    const log: string[] = [];
    (globalThis as { __paletteProbe?: string[] }).__paletteProbe = log;
    const snap = (tag: string) => {
      const opts = [...document.querySelectorAll('[role="option"]')];
      const hi = opts.filter((o) => o.hasAttribute("data-highlighted")).map((o) => o.textContent?.slice(0, 22));
      const act = opts.filter((o) => o.hasAttribute("data-active")).map((o) => o.textContent?.slice(0, 22));
      const el = document.activeElement as HTMLElement | null;
      log.push(`${performance.now().toFixed(1)} DOM ${tag} n=${opts.length} hi=${JSON.stringify(hi)} act=${JSON.stringify(act)} focus=${el?.tagName}:${el?.getAttribute("placeholder") ?? ""} hasFocus=${document.hasFocus()} vis=${document.visibilityState}`);
    };
    const mo = new MutationObserver(() => snap("mut"));
    mo.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["data-highlighted", "data-active"] });
    const onKey = (e: KeyboardEvent) => snap(`keydown:${e.key} target=${(e.target as HTMLElement).tagName}`);
    const onPointer = (e: PointerEvent) => snap(`${e.type} ${e.clientX},${e.clientY} ${(e.target as HTMLElement).textContent?.slice(0, 15)}`);
    const onFocus = (e: FocusEvent) => snap(`${e.type} ${(e.target as HTMLElement).tagName}`);
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointermove", onPointer, true);
    document.addEventListener("pointerover", onPointer, true);
    document.addEventListener("focusin", onFocus, true);
    document.addEventListener("focusout", onFocus, true);
    window.addEventListener("blur", () => snap("window-blur"));
    try {
    await using _screen = await render(<CommandPalette>{null}</CommandPalette>);

    openCommandPalette({});
    await expect
      .element(page.getByPlaceholder("Search commands, projects, and threads..."))
      .toHaveFocus();
    snap("focused");
    await userEvent.keyboard("{ArrowDown}");
    snap("after-key");
    try {
      await expect
        .element(page.getByRole("option", { name: /^New thread in\.\.\./ }), { timeout: 5000 })
        .toHaveAttribute("data-highlighted");
    } catch (error) {
      snap("fail");
      console.log("PALETTE-PROBE-FAIL\n" + log.join("\n"));
      throw error;
    }
    await userEvent.keyboard("{Enter}");

    await expect.element(page.getByPlaceholder("Search...")).toHaveFocus();
    await expect.element(page.getByRole("option").first()).toHaveAttribute("data-highlighted");
    await expect.element(page.getByRole("option").nth(1)).not.toHaveAttribute("data-highlighted");
    } finally {
      mo.disconnect();
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointermove", onPointer, true);
      document.removeEventListener("pointerover", onPointer, true);
      document.removeEventListener("focusin", onFocus, true);
      document.removeEventListener("focusout", onFocus, true);
      (globalThis as { __paletteProbe?: string[] }).__paletteProbe = undefined;
    }
  });

  it("opens a search intent with its query in the focused input", async () => {
    setup();
    await using _screen = await render(<CommandPalette>{null}</CommandPalette>);

    openCommandPalette({ query: "acme" });

    const input = page.getByPlaceholder("Search commands, projects, and threads...");
    await expect.element(input).toHaveFocus();
    await expect.element(input).toHaveValue("acme");
  });

  it("opens a searched project's latest listed thread, not a newer subagent", async () => {
    const environmentId = EnvironmentId.make("local");
    const projectId = ProjectId.make("acme");
    const parentId = ThreadId.make("parent");
    setup({
      projects: [
        {
          id: projectId,
          environmentId,
          title: "Acme",
          workspaceRoot: "/work/acme",
          repositoryIdentity: null,
          defaultModelSelection: null,
          createdAt: "2026-03-01T00:00:00.000Z",
          updatedAt: "2026-03-01T00:00:00.000Z",
          scripts: [],
        },
      ],
      threads: [
        makeThreadFixture({
          id: parentId,
          environmentId,
          projectId,
          latestUserMessageAt: "2026-03-02T00:00:00.000Z",
        }),
        makeThreadFixture({
          id: ThreadId.make("subagent"),
          environmentId,
          projectId,
          updatedAt: "2026-03-03T00:00:00.000Z",
          lineage: {
            rootThreadId: parentId,
            parentThreadId: parentId,
            relationshipToParent: "subagent",
          },
        }),
      ],
    });
    await using screen = await render(<CommandPalette>{null}</CommandPalette>);

    openCommandPalette({ query: "Acme" });
    await page.getByRole("option", { name: /Acme/ }).first().click();

    await vi.waitFor(() => expect(screen.router?.state.location.pathname).toBe("/local/parent"));
  });
});
