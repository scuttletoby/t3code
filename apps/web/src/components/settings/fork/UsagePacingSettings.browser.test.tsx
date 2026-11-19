import { type ManagedPolicySummary, ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it, vi } from "vite-plus/test";
import { page, userEvent } from "vite-plus/test/browser";

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
  useScopedSettingsWriteAllowed: () => true,
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
vi.mock(import("../../../state/session"), async (original) => ({
  ...(await original()),
  useEnvironmentsWithScope: ((environments: ReadonlyArray<{ environmentId: string }>) =>
    new Set(environments.map(({ environmentId }) => environmentId))) as never,
}));

import { ModelPolicySettings } from "./ModelPolicySettings";
import { UsagePacingSettings } from "./UsagePacingSettings";

const decodeServerSettings = Schema.decodeUnknownSync(ServerSettings);

/** The settings a server reads from `file`, so an old or empty file is tested as it loads. */
function setup(file: object, managedPolicy?: ManagedPolicySummary) {
  state.settings = decodeServerSettings(file);
  state.environments = [
    {
      environmentId: "env-a",
      providers: [
        { instanceId: "claude-a", displayName: "Claude A" },
        { instanceId: "codex-b", displayName: "Codex B" },
      ],
      ...(managedPolicy ? { managedPolicy } : {}),
    },
  ];
  state.update.mockReset();
}

describe("UsagePacingSettings", () => {
  it("lists each account by name from an empty settings file, and writes one account's reserve", async () => {
    setup({});
    await using screen = await render(<UsagePacingSettings />);

    await expect.element(screen.getByText("Reserve on Claude A")).toBeVisible();
    await expect.element(screen.getByText("Reserve on Codex B")).toBeVisible();
    const reserve = screen.getByRole("textbox", { name: "Reserve on Codex B in percent" });
    await expect.element(reserve).toHaveValue("0");
    // The number field saves when it loses focus.
    await userEvent.type(reserve, "{Backspace}20");
    await userEvent.tab();
    expect(state.update.mock.calls[0]![0]).toEqual({
      usagePacing: { "codex-b": { reservePercent: 20 } },
    });

    await userEvent.click(screen.getByRole("switch", { name: "Spend Claude A first" }));
    expect(state.update.mock.calls[1]![0]).toEqual({
      usagePacing: { "claude-a": { burnFirst: true } },
    });
  });

  it("reads a stored entry, resets it, and removes one for an account that was removed", async () => {
    setup({
      usagePacing: { "claude-a": { reservePercent: 15, noLimits: true }, old_account: {} },
    });
    await using screen = await render(<UsagePacingSettings />);

    await expect
      .element(screen.getByRole("textbox", { name: "Reserve on Claude A in percent" }))
      .toHaveValue("15");
    await expect
      .element(screen.getByRole("switch", { name: "Claude A has no usage limits" }))
      .toBeChecked();
    await userEvent.click(
      screen.getByRole("button", { name: "Reset usage pacing for Claude A to default" }),
    );
    expect(state.update.mock.calls[0]![0]).toEqual({ usagePacing: { "claude-a": null } });

    await expect.element(screen.getByText("Reserve on old_account")).toBeVisible();
    await userEvent.click(
      screen.getByRole("button", { name: "Remove usage pacing for old_account" }),
    );
    expect(state.update.mock.calls[1]![0]).toEqual({ usagePacing: { old_account: null } });
  });

  it("writes the gate and the lower effort map, and refuses effort text it cannot read", async () => {
    setup({});
    await using screen = await render(<UsagePacingSettings />);

    await userEvent.click(screen.getByRole("combobox", { name: "Usage pacing gate" }));
    await userEvent.click(screen.getByRole("option", { name: "Advise" }));
    expect(state.update.mock.calls[0]![0]).toEqual({ usagePacingGate: "advise" });

    const effort = screen.getByRole("textbox", { name: "Lower effort when ahead of pace" });
    await userEvent.type(effort, "high{Enter}");
    await expect.element(screen.getByRole("alert")).toBeVisible();
    expect(state.update).toHaveBeenCalledTimes(1);
    await userEvent.type(effort, "=medium{Enter}");
    expect(state.update.mock.calls[1]![0]).toEqual({
      usagePacingLowerEffort: { high: "medium" },
    });
  });

  it("shows the saved reserve when a typed one is above 100", async () => {
    setup({ usagePacing: { "claude-a": { reservePercent: 100 } } });
    await using screen = await render(<UsagePacingSettings />);

    const reserve = screen.getByRole("textbox", { name: "Reserve on Claude A in percent" });
    await userEvent.clear(reserve);
    await userEvent.type(reserve, "150");
    await userEvent.tab();
    expect(state.update.mock.calls[0]![0]).toEqual({
      usagePacing: { "claude-a": { reservePercent: 100 } },
    });
    await expect
      .element(screen.getByRole("textbox", { name: "Reserve on Claude A in percent" }))
      .toHaveValue("100");
  });

  it("greys out every control of a locked row, the switches below its title too", async () => {
    setup({}, { organization: "Example Corp", lockedSettings: ["usagePacing"] });
    await using screen = await render(<UsagePacingSettings />);

    await expect.element(screen.getByText("Reserve on Claude A")).toBeVisible();
    for (const label of [
      "Reserve on Claude A in percent",
      "Spend Claude A first",
      "Claude A has no usage limits",
    ]) {
      const control = screen.container.querySelector(`[aria-label="${label}"]`);
      expect(control?.closest("[inert]"), label).not.toBeNull();
    }
    // The gate is not locked.
    expect(
      screen.container.querySelector('[aria-label="Usage pacing gate"]')?.closest("[inert]"),
    ).toBeNull();
  });
});

describe("ModelPolicySettings", () => {
  it("writes the mode with the stored rows, and saves edited rows whole", async () => {
    const row = { driver: "codex", model: "gpt-mini" };
    setup({ modelPolicy: { mode: "off", allow: [row] } });
    await using screen = await render(<ModelPolicySettings />);

    await expect.element(screen.getByText("gpt-mini")).toBeVisible();
    await userEvent.click(screen.getByRole("combobox", { name: "Model policy mode" }));
    await userEvent.click(page.getByRole("option", { name: "Enforce" }));
    expect(state.update.mock.calls[0]![0]).toEqual({
      modelPolicy: { mode: "enforce", allow: [row] },
    });

    await userEvent.click(screen.getByRole("button", { name: "Edit allowed models" }));
    const dialog = page.getByRole("dialog");
    const rows = dialog.getByRole("textbox", { name: "Allowed models" });
    await userEvent.fill(rows, JSON.stringify([{ ...row, effort: "low" }]));
    await userEvent.click(dialog.getByRole("button", { name: "Save rows" }));
    await expect.element(dialog.getByRole("alert")).toBeVisible();
    expect(state.update).toHaveBeenCalledTimes(1);

    await userEvent.fill(rows, JSON.stringify([]));
    await userEvent.click(dialog.getByRole("button", { name: "Save rows" }));
    expect(state.update.mock.calls[1]![0]).toEqual({ modelPolicy: { mode: "off", allow: [] } });
  });

  it("warns when the stored value cannot be read, and reads it as enforce", async () => {
    setup({ modelPolicy: { mode: "always" } });
    await using screen = await render(<ModelPolicySettings />);

    await expect.element(screen.getByRole("alert")).toHaveTextContent("cannot be read");
    await expect.element(screen.getByText('{"mode":"always"}')).toBeVisible();
    await expect
      .element(screen.getByRole("combobox", { name: "Model policy mode" }))
      .toHaveTextContent("Enforce");
    await userEvent.click(screen.getByRole("button", { name: "Reset model policy to default" }));
    expect(state.update.mock.calls[0]![0]).toEqual({ modelPolicy: { mode: "off", allow: [] } });
  });
});
