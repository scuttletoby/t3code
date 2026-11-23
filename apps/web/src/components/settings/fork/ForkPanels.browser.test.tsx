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
vi.mock(import("../../../state/entities"), async (original) => ({
  ...(await original()),
  useThreadShells: (() => [{ id: "thread-1", title: "Fix the login page" }]) as never,
}));
vi.mock(import("../../../state/session"), async (original) => ({
  ...(await original()),
  useEnvironmentsWithScope: ((environments: ReadonlyArray<{ environmentId: string }>) =>
    new Set(environments.map(({ environmentId }) => environmentId))) as never,
}));

import { HandoffSettings } from "./HandoffSettings";
import { PlaytestSettings } from "./PlaytestSettings";
import { UpdatesSettings } from "./UpdatesSettings";

const decodeServerSettings = Schema.decodeUnknownSync(ServerSettings);

/** The settings a server reads from `file`, so an old or empty file is tested as it loads. */
function setup(file: object, managedPolicy?: ManagedPolicySummary) {
  state.settings = decodeServerSettings(file);
  state.environments = [
    { environmentId: "env-a", providers: [], ...(managedPolicy ? { managedPolicy } : {}) },
  ];
  state.update.mockReset();
}

const STEPS = {
  claim: { argv: ["/usr/local/bin/game", "claim"] },
  renew: { argv: ["/usr/local/bin/game", "renew"] },
  push: [{ name: "deploy", argv: ["/usr/local/bin/game", "deploy"] }],
  release: { argv: ["/usr/local/bin/game", "release"] },
};

describe("UpdatesSettings", () => {
  it("writes automatic updates and the quiet check from an empty settings file", async () => {
    setup({});
    await using screen = await render(<UpdatesSettings />);

    const toggle = screen.getByRole("switch", { name: "Update automatically" });
    await expect.element(toggle).not.toBeChecked();
    await userEvent.click(toggle);
    expect(state.update.mock.calls[0]![0]).toEqual({ automaticUpdates: true });

    // The quiet check is a rarely changed option, folded under Advanced.
    await userEvent.click(screen.getByRole("button", { name: "Advanced" }));
    await userEvent.type(
      screen.getByRole("textbox", { name: "Quiet check before an update" }),
      " /usr/local/bin/quiet {Enter}",
    );
    expect(state.update.mock.calls[1]![0]).toEqual({
      automaticUpdatesQuietCommand: "/usr/local/bin/quiet",
    });
  });
});

describe("HandoffSettings", () => {
  it("writes the whole value with the new mode, and adds a thread by id", async () => {
    setup({});
    await using screen = await render(<HandoffSettings isProjectScope={false} />);
    const empty = state.settings.handoff;

    await userEvent.click(screen.getByRole("combobox", { name: "Handoff mode" }));
    await userEvent.click(page.getByRole("option", { name: "Handoff" }));
    expect(state.update.mock.calls[0]![0]).toEqual({ handoff: { ...empty, mode: "handoff" } });

    const threadId = screen.getByRole("textbox", { name: "Thread id" });
    await userEvent.type(threadId, "thread-1{Enter}");
    expect(state.update.mock.calls[1]![0]).toEqual({
      handoff: { ...empty, threads: { "thread-1": "compact" } },
    });
    // The typed id stays until the save is through: a refused save keeps it to try again.
    state.update.mock.calls[1]![1]?.onFailed?.();
    await expect.element(threadId).toHaveValue("thread-1");
    state.update.mock.calls[1]![1]?.onSaved?.();
    await expect.element(threadId).toHaveValue("");
  });

  it("greys out every control of a locked handoff, the thread list and command too", async () => {
    setup(
      { handoff: { mode: "handoff", threads: { "thread-1": "compact" } } },
      { organization: "Example Corp", lockedSettings: ["handoff"] },
    );
    await using screen = await render(<HandoffSettings isProjectScope={false} />);

    await expect.element(screen.getByText("Fix the login page")).toBeVisible();
    for (const selector of [
      '[aria-label="Handoff mode"]',
      '[aria-label="Handoff mode for thread thread-1"]',
      '[aria-label="Remove the handoff mode of thread thread-1"]',
      '[aria-label="Thread id"]',
    ]) {
      expect(screen.container.querySelector(selector)?.closest("[inert]"), selector).not.toBeNull();
    }
    const add = [...screen.container.querySelectorAll("button")].find(
      (button) => button.textContent === "Add thread",
    );
    expect(add?.closest("[inert]")).not.toBeNull();
  });

  it("lists a stored thread by its title and removes it", async () => {
    setup({ handoff: { mode: "handoff", threads: { "thread-1": "compact" } } });
    await using screen = await render(<HandoffSettings isProjectScope={false} />);

    await expect.element(screen.getByText("Fix the login page")).toBeVisible();
    await userEvent.click(
      screen.getByRole("button", { name: "Remove the handoff mode of thread thread-1" }),
    );
    expect(state.update.mock.calls[0]![0]).toEqual({
      handoff: { ...state.settings.handoff, threads: {} },
    });
  });
});

describe("PlaytestSettings", () => {
  it("keeps the switch off until the steps are set, then saves edited steps whole", async () => {
    setup({});
    await using screen = await render(<PlaytestSettings isProjectScope={false} />);

    await expect.element(screen.getByRole("switch", { name: "Playtest sessions" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Edit steps" }));
    const dialog = page.getByRole("dialog");
    const steps = dialog.getByRole("textbox", { name: "Playtest steps" });
    await userEvent.clear(steps);
    await userEvent.type(steps, "not json");
    await userEvent.click(dialog.getByRole("button", { name: "Save steps" }));
    await expect.element(dialog.getByRole("alert")).toBeVisible();
    expect(state.update).not.toHaveBeenCalled();

    await userEvent.clear(steps);
    await userEvent.fill(steps, JSON.stringify(STEPS));
    await userEvent.click(dialog.getByRole("button", { name: "Save steps" }));
    const saved = (state.update.mock.calls[0]![0] as { playtest: typeof state.settings.playtest })
      .playtest;
    expect(saved.enabled).toBe(false);
    expect(saved.push.map((step) => step.name)).toEqual(["deploy"]);
    expect(saved.release?.argv).toEqual(STEPS.release.argv);
  });

  it("switches a complete adapter on and off", async () => {
    setup({ playtest: STEPS });
    await using screen = await render(<PlaytestSettings isProjectScope={false} />);

    await userEvent.click(screen.getByRole("switch", { name: "Playtest sessions" }));
    expect(state.update.mock.calls[0]![0]).toEqual({
      playtest: { ...state.settings.playtest, enabled: true },
    });

    // The server now reads it as on.
    state.settings = decodeServerSettings({ playtest: { ...STEPS, enabled: true } });
    await screen.rerender(<PlaytestSettings isProjectScope={false} />);
    const toggle = screen.getByRole("switch", { name: "Playtest sessions" });
    await expect.element(toggle).toBeChecked();
    await userEvent.click(toggle);
    expect(state.update.mock.calls[1]![0]).toEqual({
      playtest: { ...state.settings.playtest, enabled: false },
    });
  });

  it("resets the server's playtest to the real default, steps included", async () => {
    setup({ playtest: { ...STEPS, enabled: true, renewMinutes: 3 } });
    await using screen = await render(<PlaytestSettings isProjectScope={false} />);

    await userEvent.click(screen.getByRole("button", { name: "Reset playtest to default" }));
    expect(state.update.mock.calls[0]![0]).toEqual({
      playtest: decodeServerSettings({}).playtest,
    });
  });
});
