// Settings → Fork → Calm sidebar (CAL3, CAL4): does the switch save the device's own setting, does
// the roles list mark a role whose thread cannot hold it, and does Remove save just that entry?
import { ProjectId, ThreadId, type ThreadRoles } from "@t3tools/contracts";
import { Atom } from "effect/reactivity";
import { describe, expect, it, vi } from "vite-plus/test";
import { userEvent } from "vite-plus/test/browser";

import "~/index.css";
import { render } from "../../../../test/render";

const game = ProjectId.make("project-game");
const site = ProjectId.make("project-site");

const state = vi.hoisted(() => ({
  calm: false,
  roles: undefined as ThreadRoles | undefined,
  updateClient: vi.fn(),
  saveRoles: vi.fn(),
}));

vi.mock(import("../../../hooks/useSettings"), async (original) => ({
  ...(await original()),
  useClientSettings: ((select: (settings: { calmSidebarEnabled: boolean }) => unknown) =>
    select({ calmSidebarEnabled: state.calm })) as never,
  updateClientSettings: ((patch: unknown) => {
    state.updateClient(patch);
    return Promise.resolve();
  }) as never,
}));
vi.mock(import("../SettingsScopeContext"), async (original) => ({
  ...(await original()),
  useSettingsScope: (() => ({
    connectedEnvironments: [
      {
        environmentId: "env-a",
        label: "env-a",
        serverConfig: { settings: { threadRoles: state.roles } },
      },
    ],
  })) as never,
}));
vi.mock(import("../../../state/entities"), async (original) => ({
  ...(await original()),
  useProjects: (() => [{ environmentId: "env-a", id: "project-game", title: "Game" }]) as never,
}));
const threadIndex = Atom.make(
  new Map([
    [
      ThreadId.make("t-game"),
      {
        id: ThreadId.make("t-game"),
        projectId: game,
        title: "Game orchestrator",
        lineage: {
          parentThreadId: null,
          relationshipToParent: null,
          rootThreadId: ThreadId.make("t-game"),
        },
        delegatedFrom: null,
        archivedAt: null,
        deletedAt: null,
      },
    ],
  ]),
);
vi.mock(import("../../../state/threads"), async (original) => ({
  ...(await original()),
  environmentThreadShells: {
    ...(await original()).environmentThreadShells,
    environmentThreadIndexAtom: (() => threadIndex) as never,
  },
}));
vi.mock(import("../../fork/calmSidebar/roleMenu"), async (original) => ({
  ...(await original()),
  saveThreadRoles: ((environmentId: string, patch: unknown) => {
    state.saveRoles(environmentId, patch);
    return Promise.resolve(true);
  }) as never,
}));

const { CalmSidebarSettings } = await import("./CalmSidebarSettings");

describe("Calm sidebar settings (CAL3, CAL4)", () => {
  it("switches the calm view on for this device", async () => {
    state.calm = false;
    state.roles = { assistant: null, orchestrators: {} };
    state.updateClient.mockClear();
    await using screen = await render(<CalmSidebarSettings />);

    const toggle = screen.getByRole("switch", { name: "Calm sidebar" });
    await expect.element(toggle).not.toBeChecked();
    await userEvent.click(toggle);
    expect(state.updateClient).toHaveBeenCalledWith({ calmSidebarEnabled: true });
    await expect.element(screen.getByText("No thread has a role.")).toBeVisible();
  });

  it("lists each role, marks one that holds none, and removes just that entry", async () => {
    state.calm = true;
    state.roles = {
      assistant: ThreadId.make("t-deleted"),
      orchestrators: { [game]: ThreadId.make("t-game"), [site]: ThreadId.make("t-game") },
    } as ThreadRoles;
    state.saveRoles.mockClear();
    await using screen = await render(<CalmSidebarSettings />);

    await expect.element(screen.getByText("Orchestrator of Game:")).toBeVisible();
    await expect
      .element(screen.getByText("Missing: the thread is deleted or not on this server."))
      .toBeVisible();
    // Site's pointer names Game's thread: kept, marked, holds no role.
    await expect.element(screen.getByText("In another project now.")).toBeVisible();

    const removes = screen.getByRole("button", { name: "Remove" });
    await userEvent.click(removes.nth(0));
    await userEvent.click(removes.nth(2));
    expect(state.saveRoles.mock.calls).toEqual([
      ["env-a", { assistant: null }],
      ["env-a", { orchestrators: { [site]: null } }],
    ]);
  });

  it("says so for a server without thread roles (an older build)", async () => {
    state.roles = undefined;
    await using screen = await render(<CalmSidebarSettings />);
    await expect.element(screen.getByText("This server does not have thread roles.")).toBeVisible();
  });
});
