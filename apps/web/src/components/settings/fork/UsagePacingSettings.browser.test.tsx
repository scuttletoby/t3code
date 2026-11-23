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
    providers: Array<{
      instanceId: string;
      driver: string;
      displayName: string;
      models?: Array<{ slug: string; aliases?: Array<string> }>;
    }>;
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
        {
          instanceId: "claude-a",
          driver: "claudeAgent",
          displayName: "Claude A",
          models: [{ slug: "claude-opus-5-5", aliases: ["opus"] }],
        },
        { instanceId: "codex-b", driver: "codex", displayName: "Codex B" },
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
    // The chosen option is named in text too, not by colour alone.
    await expect
      .element(screen.getByText("Off: tasks start as asked and usage is not checked. (chosen)"))
      .toBeInTheDocument();

    await userEvent.click(screen.getByRole("combobox", { name: "Usage pacing gate" }));
    await userEvent.click(screen.getByRole("option", { name: "Advise" }));
    expect(state.update.mock.calls[0]![0]).toEqual({ usagePacingGate: "advise" });

    // Lowering effort is a rarely changed option, folded under Advanced.
    await userEvent.click(screen.getByRole("button", { name: "Advanced" }));
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
    await userEvent.click(screen.getByRole("combobox", { name: "Models agents may use: mode" }));
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

    await expect.element(screen.getByRole("alert")).toMatchTextContent("cannot be read");
    await expect.element(screen.getByText('{"mode":"always"}')).toBeVisible();
    await expect
      .element(screen.getByRole("combobox", { name: "Models agents may use: mode" }))
      .toMatchTextContent("Enforce");
    await userEvent.click(screen.getByRole("button", { name: "Reset model policy to default" }));
    expect(state.update.mock.calls[0]![0]).toEqual({ modelPolicy: { mode: "off", allow: [] } });
  });

  it("edits an unreadable value from its own rows, and saves only with a mode the person chose", async () => {
    const typo = { driver: "codex", model: "gpt-mini", effort: "low" };
    setup({ modelPolicy: { mode: "advise", allow: [typo] } });
    await using screen = await render(<ModelPolicySettings />);

    await userEvent.click(screen.getByRole("button", { name: "Edit allowed models" }));
    const dialog = page.getByRole("dialog");
    await expect.element(dialog.getByText(/cannot be read/)).toBeVisible();
    await expect
      .element(dialog.getByText(JSON.stringify({ mode: "advise", allow: [typo] })))
      .toBeVisible();
    const rows = dialog.getByRole("textbox", { name: "Allowed models" });
    await expect.element(rows).toHaveValue(JSON.stringify([typo], null, 2));

    const fixed = { driver: "codex", model: "gpt-mini", efforts: ["low"] };
    await userEvent.fill(rows, JSON.stringify([fixed]));
    const replace = dialog.getByRole("button", { name: "Replace the unreadable value" });
    await userEvent.click(replace);
    await expect.element(dialog.getByRole("alert")).toMatchTextContent("Choose the mode");
    expect(state.update).not.toHaveBeenCalled();

    await userEvent.click(dialog.getByRole("combobox", { name: "Mode to save" }));
    await userEvent.click(page.getByRole("option", { name: "Advise (as the file says)" }));
    await userEvent.click(replace);
    expect(state.update.mock.calls[0]![0]).toEqual({
      modelPolicy: { mode: "advise", allow: [fixed] },
    });
  });

  it("marks the person's own model pick outside the rows, and only while the policy is on", async () => {
    const allow = [
      { driver: "claudeAgent", model: "claude-opus-5-5", efforts: ["high"], defaultEffort: "high" },
    ];
    const picks = {
      defaultModelSelection: {
        instanceId: "claude-a",
        model: "claude-opus-5-5",
        options: [{ id: "effort", value: "high" }],
      },
      reviewGuideModelSelection: { instanceId: "codex-b", model: "gpt-mini" },
    };
    setup({ modelPolicy: { mode: "advise", allow }, ...picks });
    {
      await using screen = await render(<ModelPolicySettings />);
      await expect.element(screen.getByText("Review guide model")).toBeVisible();
      await expect
        .element(screen.getByText("outside policy (gpt-mini (codex) is in no row)"))
        .toBeVisible();
      // The pick inside the rows is listed without the mark.
      await expect.element(screen.getByText("claude-opus-5-5, effort high")).toBeVisible();
      expect(screen.getByText(/outside policy \(claude-opus-5-5/).elements()).toHaveLength(0);
    }
    setup({ modelPolicy: { mode: "off", allow }, ...picks });
    await using off = await render(<ModelPolicySettings />);
    await expect.element(off.getByText("Models agents may use").first()).toBeVisible();
    expect(off.getByText(/^outside policy/).elements()).toHaveLength(0);
  });

  it("flags a row whose driver no provider here has", async () => {
    setup({
      modelPolicy: {
        mode: "enforce",
        allow: [
          { driver: "claudAgent", model: "opus" },
          { driver: "codex", model: "gpt-mini" },
        ],
      },
    });
    await using screen = await render(<ModelPolicySettings />);

    await expect
      .element(screen.getByRole("note"))
      .toMatchTextContent("No provider here uses the driver claudAgent");
    await expect.element(screen.getByText("gpt-mini")).toBeVisible();
  });

  it("flags a row whose model is an alias, and not one that names the full id", async () => {
    setup({
      modelPolicy: {
        mode: "enforce",
        allow: [
          { driver: "claudeAgent", model: "opus", efforts: ["high"] },
          { driver: "claudeAgent", model: "claude-opus-5-5", efforts: ["high"] },
        ],
      },
    });
    await using screen = await render(<ModelPolicySettings />);

    await expect
      .element(screen.getByRole("note"))
      .toMatchTextContent("opus is an alias of claude-opus-5-5; name the full id.");
    expect(screen.getByRole("note").elements()).toHaveLength(1);
  });

  it("flags a name Claude Code resolves by itself that no model list holds (#780)", async () => {
    setup({
      modelPolicy: {
        mode: "enforce",
        allow: [{ driver: "claudeAgent", model: "sonnet", efforts: ["high"] }],
      },
    });
    await using screen = await render(<ModelPolicySettings />);

    await expect
      .element(screen.getByRole("note"))
      .toMatchTextContent("sonnet is an alias claudeAgent resolves by itself; name the full id.");
  });
});
