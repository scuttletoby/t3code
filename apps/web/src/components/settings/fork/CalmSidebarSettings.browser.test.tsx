// Fork (calm sidebar): the Settings rows, the calm view's switch (this device) and the list of
// thread roles with its marks and Remove. Each test names the question it answers.
import { ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it, vi } from "vite-plus/test";
import { userEvent } from "vite-plus/test/browser";

import "~/index.css";
import { render } from "../../../../test/render";

const state = vi.hoisted(() => ({
  settings: {} as ServerSettings,
  targets: 1,
  calm: false,
  update: vi.fn((_patch: unknown) => {}),
  updateClient: vi.fn((_patch: unknown) => Promise.resolve()),
}));

function scope() {
  const targets = Array.from({ length: state.targets }, (_, index) => ({
    environmentId: `env-${index}`,
    label: `env-${index}`,
    projectId: null,
    settings: state.settings,
    sources: {},
  }));
  const environments = targets.map(({ environmentId }) => ({
    environmentId,
    label: environmentId,
    connection: { phase: "connected" },
    serverConfig: {
      settings: state.settings,
      environment: { capabilities: {}, platform: { os: "linux", arch: "x64", machine: "server" } },
    },
  }));
  return {
    scope: {
      kind: "all",
      label: "All",
      members: [],
      environmentIds: environments.map(({ environmentId }) => environmentId),
    },
    environments,
    connectedEnvironments: environments,
    environment: environments[0] ?? null,
    targets,
    target: null,
  };
}

vi.mock(import("../SettingsScopeContext"), async (original) => ({
  ...(await original()),
  useSettingsScope: scope as never,
  useOptionalSettingsScope: scope as never,
}));
vi.mock(import("../useScopedSettings"), async (original) => ({
  ...(await original()),
  useScopedSettingsWriteAllowed: () => true,
  useUpdateScopedSettings: (() => state.update) as never,
}));
vi.mock(import("../../../state/session"), async (original) => ({
  ...(await original()),
  useEnvironmentsWithScope: ((environments: ReadonlyArray<{ environmentId: string }>) =>
    new Set(environments.map(({ environmentId }) => environmentId))) as never,
}));
vi.mock(import("../../../hooks/useSettings"), async (original) => ({
  ...(await original()),
  useClientSettings: ((select: (settings: { calmSidebarEnabled: boolean }) => unknown) =>
    select({ calmSidebarEnabled: state.calm })) as never,
  updateClientSettings: state.updateClient as never,
}));
const topLevel = (id: string) => ({
  parentThreadId: null,
  relationshipToParent: null,
  rootThreadId: id,
});
vi.mock(import("../../../state/entities"), async (original) => ({
  ...(await original()),
  useThreadShells: (() => [
    {
      environmentId: "env-0",
      id: "lead",
      projectId: "project-game",
      title: "Game lead",
      lineage: topLevel("lead"),
      delegatedFrom: null,
      archivedAt: null,
      deletedAt: null,
    },
  ]) as never,
  useProjects: (() => [
    { environmentId: "env-0", id: "project-game", title: "Game" },
    { environmentId: "env-0", id: "project-site", title: "Site" },
  ]) as never,
}));

import { CalmSidebarSettings } from "./CalmSidebarSettings";

const decode = Schema.decodeUnknownSync(ServerSettings);

function setup(file: object, changes: { calm?: boolean; targets?: number } = {}) {
  state.settings = decode(file);
  state.calm = changes.calm ?? false;
  state.targets = changes.targets ?? 1;
  state.update.mockReset();
  state.updateClient.mockClear();
}

describe("CalmSidebarSettings", () => {
  it("the switch is off on a new device and saves this device's setting", async () => {
    setup({});
    await using screen = await render(<CalmSidebarSettings />);
    const toggle = screen.getByRole("switch", { name: "Calm sidebar" });
    await expect.element(toggle).not.toBeChecked();
    await userEvent.click(toggle);
    expect(state.updateClient).toHaveBeenCalledWith({ calmSidebarEnabled: true });
    expect(state.update).not.toHaveBeenCalled();
  });

  it("on, it offers a reset to off", async () => {
    setup({}, { calm: true });
    await using screen = await render(<CalmSidebarSettings />);
    await expect.element(screen.getByRole("switch", { name: "Calm sidebar" })).toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Reset calm sidebar to default" }));
    expect(state.updateClient).toHaveBeenCalledWith({ calmSidebarEnabled: false });
  });

  it("an old settings file has no roles: the list says so", async () => {
    setup({});
    await using screen = await render(<CalmSidebarSettings />);
    await expect.element(screen.getByText("No thread holds a role.")).toBeInTheDocument();
  });

  it("lists each role, marks the one whose thread is missing, and Remove clears that one alone", async () => {
    setup({
      threadRoles: {
        assistant: "gone",
        orchestrators: { "project-game": "lead", "project-site": "lead" },
      },
    });
    await using screen = await render(<CalmSidebarSettings />);
    const line = (key: string) => screen.getByTestId(`thread-role-${key}`).element().textContent;
    await expect.element(screen.getByTestId("thread-role-assistant")).toBeInTheDocument();
    expect(line("assistant")).toBe("Assistant: thread gone(missing: no such thread here)Remove");
    expect(line("orchestrator:project-game")).toBe("Orchestrator of Game: Game leadRemove");
    expect(line("orchestrator:project-site")).toBe(
      "Orchestrator of Site: Game lead(not used: the thread is in another project now)Remove",
    );

    await userEvent.click(
      screen
        .getByTestId("thread-role-orchestrator:project-site")
        .getByRole("button", { name: /Remove/ }),
    );
    expect(state.update).toHaveBeenCalledWith({
      threadRoles: { orchestrators: { "project-site": null } },
    });
  });

  it("with several environments selected, it asks for one instead of mixing their roles", async () => {
    setup({ threadRoles: { assistant: "lead" } }, { targets: 2 });
    await using screen = await render(<CalmSidebarSettings />);
    await expect
      .element(
        screen.getByText("Roles are set per environment. Select one environment to see them."),
      )
      .toBeInTheDocument();
    await expect.element(screen.getByRole("button", { name: /Remove/ })).not.toBeInTheDocument();
  });
});
