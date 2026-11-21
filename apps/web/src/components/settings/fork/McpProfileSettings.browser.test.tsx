import { type ManagedPolicySummary, ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it, vi } from "vite-plus/test";
import { userEvent } from "vite-plus/test/browser";

import "~/index.css";
import { render } from "../../../../test/render";

type Settings = ServerSettings;
type UpdateOptions = { readonly onFailed?: () => void; readonly onSaved?: () => void };

const state = vi.hoisted(() => ({
  settings: {} as Settings,
  environments: [] as Array<{
    environmentId: string;
    providers: Array<{ instanceId: string; displayName: string }>;
    managedPolicy?: ManagedPolicySummary;
  }>,
  update: vi.fn((_patch: unknown, _options?: UpdateOptions) => {}),
  writeAllowed: true,
}));

function scope() {
  const environments = state.environments.map(({ environmentId, managedPolicy }) => ({
    environmentId,
    label: environmentId,
    connection: { phase: "connected" },
    serverConfig: {
      settings: state.settings,
      environment: { capabilities: {}, platform: { os: "linux", arch: "x64", machine: "server" } },
      ...(managedPolicy ? { managedPolicy } : {}),
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
    targets: environments.map(({ environmentId }) => ({
      environmentId,
      label: environmentId,
      projectId: null,
      settings: state.settings,
      sources: {},
    })),
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
  useScopedSettings: ((select: (settings: Settings) => unknown) => select(state.settings)) as never,
  useScopedSettingsMixed: () => false,
  useScopedSettingsWriteAllowed: () => state.writeAllowed,
  useScopedSettingSource: () => "environment",
  useUpdateScopedSettings: (() => state.update) as never,
  useClearScopedSettings: (() => vi.fn()) as never,
  useClearProjectOverrides: (() => vi.fn()) as never,
}));
vi.mock(import("../../../state/environments"), async (original) => ({
  ...(await original()),
  useEnvironmentProviders: (() =>
    new Map(
      state.environments.map(({ environmentId, providers }) => [environmentId, providers]),
    )) as never,
}));
vi.mock(import("../../../state/entities"), async (original) => ({
  ...(await original()),
  useThreadShells: (() => [{ id: "thread-1", title: "Fix the login page" }]) as never,
  useProjects: (() => []) as never,
}));
vi.mock(import("../../../state/session"), async (original) => ({
  ...(await original()),
  useEnvironmentsWithScope: ((environments: ReadonlyArray<{ environmentId: string }>) =>
    new Set(environments.map(({ environmentId }) => environmentId))) as never,
}));

import { McpProfileSettings } from "./McpProfileSettings";

const decodeServerSettings = Schema.decodeUnknownSync(ServerSettings);

function setup(file: object) {
  state.settings = decodeServerSettings(file);
  state.environments = [{ environmentId: "env-a", providers: [] }];
  state.writeAllowed = true;
  state.update.mockReset();
}

describe("McpProfileSettings", () => {
  it("shows all and inherit from an old settings file and writes nothing on open, twice", async () => {
    setup({});
    for (const _run of [1, 2]) {
      await using screen = await render(<McpProfileSettings isProjectScope={false} />);
      await expect
        .element(screen.getByRole("combobox", { name: "Default MCP profile" }))
        .toHaveTextContent("all");
      await expect
        .element(screen.getByRole("combobox", { name: "Sub-agent MCP profile" }))
        .toHaveTextContent("inherit");
      await expect.element(screen.getByText("t3-only")).toBeVisible();
    }
    expect(state.update).not.toHaveBeenCalled();
  });

  it("shows an entry from a newer build as broken, with no Edit", async () => {
    setup({ mcpProfiles: { newer: { servers: "all", t3Tools: "all", hosts: ["a"] } } });
    await using screen = await render(<McpProfileSettings isProjectScope={false} />);
    await expect
      .element(screen.getByText(/fields this version does not know \(hosts\)/))
      .toBeVisible();
    await expect
      .element(screen.getByRole("button", { name: "Edit MCP profile newer" }))
      .not.toBeInTheDocument();
    // Only the built-in `all` reads as everything; the broken entry shows no set at all.
    expect(screen.getByText("Servers: all · T3 tools: all").elements()).toHaveLength(1);
    expect(state.update).not.toHaveBeenCalled();
  });

  it("refuses to delete a profile a thread picks, then clears only that pick", async () => {
    setup({
      mcpProfiles: { lean: { servers: [], t3Tools: "all" } },
      mcpThreadProfiles: { "thread-1": "lean", "thread-2": "all" },
    });
    await using screen = await render(<McpProfileSettings isProjectScope={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Delete MCP profile lean" }));
    await expect
      .element(screen.getByText(/"lean" is in use by the pick of thread Fix the login page/))
      .toBeVisible();
    expect(state.update).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "Clear the thread picks that use it" }),
    );
    expect(state.update.mock.calls[0]![0]).toEqual({ mcpThreadProfiles: { "thread-1": null } });
  });

  it("offers no working controls to a client that cannot change settings", async () => {
    setup({ mcpProfiles: { lean: { servers: [], t3Tools: "all" } } });
    state.writeAllowed = false;
    await using screen = await render(<McpProfileSettings isProjectScope={false} />);
    await expect.element(screen.getByText("lean")).toBeVisible();
    for (const selector of [
      '[aria-label="Delete MCP profile lean"]',
      '[aria-label="Edit MCP profile lean"]',
      '[aria-label="Default MCP profile"]',
      '[aria-label="Sub-agent MCP profile"]',
      '[aria-label="Thread id for an MCP profile pick"]',
    ]) {
      expect(screen.container.querySelector(selector)?.closest("[inert]"), selector).not.toBeNull();
    }
    expect(state.update).not.toHaveBeenCalled();
  });
});
