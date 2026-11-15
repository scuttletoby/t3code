import { DEFAULT_SERVER_SETTINGS, type ManagedPolicySummary } from "@t3tools/contracts";
import { describe, expect, it, vi } from "vite-plus/test";
import { page, userEvent } from "vite-plus/test/browser";

import "~/index.css";
import { act, render } from "../../../test/render";

type Settings = typeof DEFAULT_SERVER_SETTINGS;
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

vi.mock(import("./SettingsScopeContext"), async (original) => ({
  ...(await original()),
  useSettingsScope: scope as never,
  useOptionalSettingsScope: scope as never,
}));
vi.mock(import("./useScopedSettings"), async (original) => ({
  ...(await original()),
  useScopedSettings: ((select: (settings: Settings) => unknown) => select(state.settings)) as never,
  useScopedSettingsMixed: () => false,
  useScopedSettingsWriteAllowed: () => true,
  useUpdateScopedSettings: (() => state.update) as never,
  useClearScopedSettings: (() => vi.fn()) as never,
  useClearProjectOverrides: (() => vi.fn()) as never,
}));
vi.mock(import("../../state/environments"), async (original) => ({
  ...(await original()),
  useEnvironmentProviders: (() =>
    new Map(
      state.environments.map(({ environmentId, providers }) => [environmentId, providers]),
    )) as never,
}));
vi.mock(import("../../state/session"), async (original) => ({
  ...(await original()),
  useEnvironmentsWithScope: ((environments: ReadonlyArray<{ environmentId: string }>) =>
    new Set(environments.map(({ environmentId }) => environmentId))) as never,
}));

import { WaitsAndSlotsSettings } from "./WaitsAndSlotsSettings";

const buildDone = {
  argv: ["test", "-f", "/work/done"],
  maxArgs: 0,
  argPattern: "^[^-=@+]",
  intervalSeconds: 30,
  timeoutSeconds: 20,
  description: "",
};

function setup(environments: (typeof state)["environments"]) {
  state.settings = {
    ...DEFAULT_SERVER_SETTINGS,
    waitChecks: { "build-done": buildDone },
  } as Settings;
  state.environments = environments;
  state.update.mockReset();
}

const onlyEnvironment = [
  { environmentId: "env-a", providers: [{ instanceId: "claude-a", displayName: "Claude A" }] },
];

describe("WaitsAndSlotsSettings (#105)", () => {
  it("lists every selected environment's accounts, not only the primary's", async () => {
    setup([
      ...onlyEnvironment,
      { environmentId: "env-b", providers: [{ instanceId: "codex-b", displayName: "Codex B" }] },
    ]);
    await using screen = await render(<WaitsAndSlotsSettings isProjectScope={false} />);

    await expect.element(screen.getByText("Agents on Claude A")).toBeVisible();
    await expect.element(screen.getByText("Agents on Codex B")).toBeVisible();
  });

  it("an administrator lock on waitChecks makes Edit and Remove inert, like Add check", async () => {
    setup([
      {
        ...onlyEnvironment[0]!,
        managedPolicy: {
          organization: "Example Corp",
          lockedSettings: ["waitChecks"],
        },
      },
    ]);
    await using screen = await render(<WaitsAndSlotsSettings isProjectScope={false} />);

    const button = (name: string) => screen.getByRole("button", { name }).element();
    await expect.element(screen.getByText("build-done")).toBeVisible();
    expect(button("Add check").closest("[inert]")).not.toBeNull();
    expect(button("Edit check build-done").closest("[inert]")).not.toBeNull();
    expect(button("Remove check build-done").closest("[inert]")).not.toBeNull();
  });

  it("keeps the check dialog and its draft open until the write is saved", async () => {
    setup(onlyEnvironment);
    await using screen = await render(<WaitsAndSlotsSettings isProjectScope={false} />);

    await userEvent.click(screen.getByRole("button", { name: "Edit check build-done" }));
    const dialog = page.getByRole("dialog");
    await expect.element(dialog).toBeVisible();
    const description = dialog.getByLabelText("Description");
    await userEvent.type(description, "Waits for the build");
    await userEvent.click(dialog.getByRole("button", { name: "Save check" }));

    expect(state.update).toHaveBeenCalledTimes(1);
    expect(state.update.mock.calls[0]![0]).toEqual({
      waitChecks: { "build-done": { ...buildDone, description: "Waits for the build" } },
    });
    // The server has not answered yet, so the dialog stays and cannot send twice.
    await expect.element(dialog).toBeVisible();
    await expect.element(dialog.getByRole("button", { name: "Saving…" })).toBeDisabled();

    // The server refuses the write: the typed draft is still there to try again.
    await act(() => state.update.mock.calls[0]![1]!.onFailed!());
    await expect.element(dialog.getByRole("alert")).toBeVisible();
    expect(dialog.getByRole("alert").element().textContent).toContain("draft is kept");
    await expect.element(description).toHaveValue("Waits for the build");
    await userEvent.click(dialog.getByRole("button", { name: "Save check" }));
    expect(state.update).toHaveBeenCalledTimes(2);

    // Saved: only now does the dialog close.
    await act(() => state.update.mock.calls[1]![1]!.onSaved!());
    await expect.element(dialog).not.toBeInTheDocument();
  });
});
