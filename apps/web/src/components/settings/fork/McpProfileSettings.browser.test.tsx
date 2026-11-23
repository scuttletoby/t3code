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
  shells: [] as Array<object>,
}));

const shellOf = (id: string, title: string, parent: string | null = null) => ({
  environmentId: "env-a",
  id,
  title,
  projectId: "project-1",
  lineage: {
    parentThreadId: parent,
    relationshipToParent: parent === null ? null : "subagent",
    rootThreadId: parent ?? id,
  },
  delegatedFrom: null,
});

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
  useThreadShells: (() => state.shells) as never,
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
  state.shells = [shellOf("thread-1", "Fix the login page")];
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

  it("says under the T3 tool groups that a profile is not a sandbox", async () => {
    setup({});
    await using screen = await render(<McpProfileSettings isProjectScope={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Add profile" }));
    await expect
      .element(screen.getByText(/^Profiles are not a sandbox: only requests/))
      .toBeVisible();
    await expect.element(screen.getByText(/the `t3 thread` CLI/)).toBeVisible();
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
    await userEvent.click(screen.getByRole("button", { name: "Advanced" }));
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

  it("adds a thread pick only once a profile is chosen, in one save of that key", async () => {
    setup({
      mcpProfiles: { lean: { servers: [], t3Tools: "all" } },
      mcpThreadProfiles: { "thread-2": "t3-only" },
    });
    await using screen = await render(<McpProfileSettings isProjectScope={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Advanced" }));
    await userEvent.click(screen.getByRole("button", { name: "Add thread" }));
    const id = screen.getByRole("textbox", { name: "Thread id for an MCP profile pick" });
    await userEvent.fill(id, "thread-1");
    // A typed id alone writes nothing: not on Enter, not on Save.
    await userEvent.keyboard("{Enter}");
    await userEvent.click(screen.getByRole("button", { name: "Save pick" }));
    await expect
      .element(screen.getByRole("alert"))
      .toHaveTextContent("Choose a profile for this thread.");
    await expect
      .element(screen.getByRole("combobox", { name: "MCP profile for the new thread pick" }))
      .toHaveTextContent("Choose a profile");
    expect(state.update).not.toHaveBeenCalled();

    await userEvent.click(
      screen.getByRole("combobox", { name: "MCP profile for the new thread pick" }),
    );
    await userEvent.click(screen.getByRole("option", { name: "lean" }));
    expect(state.update).not.toHaveBeenCalled();
    await expect
      .element(screen.getByText("With this pick the thread gets Servers: none · T3 tools: all."))
      .toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Save pick" }));
    expect(state.update).toHaveBeenCalledTimes(1);
    expect(state.update.mock.calls[0]![0]).toEqual({ mcpThreadProfiles: { "thread-1": "lean" } });
  });

  it("writes nothing on cancel, also after a profile was chosen", async () => {
    setup({});
    await using screen = await render(<McpProfileSettings isProjectScope={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Advanced" }));
    await userEvent.click(screen.getByRole("button", { name: "Add thread" }));
    await userEvent.fill(
      screen.getByRole("textbox", { name: "Thread id for an MCP profile pick" }),
      "thread-1",
    );
    await userEvent.click(
      screen.getByRole("combobox", { name: "MCP profile for the new thread pick" }),
    );
    await userEvent.click(screen.getByRole("option", { name: "t3-only" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await expect
      .element(screen.getByRole("textbox", { name: "Thread id for an MCP profile pick" }))
      .not.toBeInTheDocument();
    // Opened again, the form starts empty: the cancelled choice is gone.
    await userEvent.click(screen.getByRole("button", { name: "Add thread" }));
    await expect
      .element(screen.getByRole("combobox", { name: "MCP profile for the new thread pick" }))
      .toHaveTextContent("Choose a profile");
    expect(state.update).not.toHaveBeenCalled();
  });

  it("says when a pick gives a limited child more than its parent has", async () => {
    setup({
      mcpProfiles: { narrow: { servers: ["docs"], t3Tools: "all" } },
      mcpThreadProfiles: { parent: "narrow", child: "all" },
    });
    state.shells = [shellOf("parent", "Lead"), shellOf("child", "Helper", "parent")];
    await using screen = await render(<McpProfileSettings isProjectScope={false} />);
    // Shown with Advanced still folded.
    await expect
      .element(
        screen.getByText("Under Advanced: 1 thread pick gives more than the thread's parent has."),
      )
      .toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Advanced" }));
    await expect
      .element(
        screen.getByText(
          "With this pick the thread gets Servers: all · T3 tools: all. That is more than the parent's narrow (Servers: docs · T3 tools: all): a pick is never cut down to the parent's, so this thread gets the wider set.",
        ),
      )
      .toBeVisible();
    // The parent's own pick is within everything: no note for it.
    expect(screen.getByText(/That is more than/).elements()).toHaveLength(1);
    expect(state.update).not.toHaveBeenCalled();
  });

  it("shows a blank pick from a hand edit as broken, and clears it", async () => {
    setup({ mcpThreadProfiles: { "thread-1": "  " } });
    await using screen = await render(<McpProfileSettings isProjectScope={false} />);
    // Shown with Advanced still folded.
    await expect
      .element(screen.getByText("Under Advanced: 1 thread pick is blank and ignored."))
      .toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Advanced" }));
    await expect
      .element(screen.getByText(/Its pick in settings.json is blank, so it is ignored/))
      .toBeVisible();
    await expect
      .element(screen.getByRole("combobox", { name: "MCP profile for thread thread-1" }))
      .toHaveTextContent("Choose a profile");
    await userEvent.click(
      screen.getByRole("button", { name: "Clear the MCP profile pick of thread thread-1" }),
    );
    expect(state.update.mock.calls[0]![0]).toEqual({ mcpThreadProfiles: { "thread-1": null } });
  });

  it("does not offer to clear thread picks while an administrator locks them", async () => {
    setup({
      mcpProfiles: { lean: { servers: [], t3Tools: "all" } },
      mcpThreadProfiles: { "thread-1": "lean" },
    });
    state.environments = [
      {
        environmentId: "env-a",
        providers: [],
        managedPolicy: { organization: "Example Corp", lockedSettings: ["mcpThreadProfiles"] },
      },
    ];
    await using screen = await render(<McpProfileSettings isProjectScope={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Delete MCP profile lean" }));
    await expect
      .element(screen.getByRole("button", { name: "Clear the thread picks that use it" }))
      .toBeDisabled();
    await expect.element(screen.getByText("Thread picks: Managed by Example Corp.")).toBeVisible();
    expect(state.update).not.toHaveBeenCalled();
  });
});
