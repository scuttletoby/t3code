import { DEFAULT_SERVER_SETTINGS, type ReviewRules } from "@t3tools/contracts";
import { describe, expect, it, vi } from "vite-plus/test";
import { page, userEvent } from "vite-plus/test/browser";

import "~/index.css";
import { act, render } from "../../../test/render";

type Settings = typeof DEFAULT_SERVER_SETTINGS;
type UpdateOptions = { readonly onFailed?: () => void; readonly onSaved?: () => void };

const state = vi.hoisted(() => ({
  settings: {} as Settings,
  projectId: null as string | null,
  source: "environment" as "environment" | "project",
  update: vi.fn((_patch: unknown, _options?: UpdateOptions) => {}),
  clear: vi.fn((_keys: unknown) => {}),
}));

function scope() {
  const environments = [
    {
      environmentId: "env-a",
      label: "env-a",
      connection: { phase: "connected" },
      serverConfig: {
        settings: state.settings,
        environment: {
          capabilities: {},
          platform: { os: "linux", arch: "x64", machine: "server" },
        },
      },
    },
  ];
  const isProject = state.projectId !== null;
  return {
    scope: isProject
      ? { kind: "project", label: "app", members: [], environmentIds: ["env-a"] }
      : { kind: "all", label: "All", members: [], environmentIds: ["env-a"] },
    environments,
    connectedEnvironments: environments,
    environment: environments[0],
    targets: [
      {
        environmentId: "env-a",
        label: "env-a",
        projectId: state.projectId,
        settings: state.settings,
        sources: { reviewRules: state.source },
      },
    ],
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
  useScopedSettingSource: (() => state.source) as never,
  useScopedSettingsWriteAllowed: () => true,
  useUpdateScopedSettings: (() => state.update) as never,
  useClearScopedSettings: (() => state.clear) as never,
  useClearProjectOverrides: (() => vi.fn()) as never,
}));
vi.mock(import("../../state/session"), async (original) => ({
  ...(await original()),
  useEnvironmentsWithScope: ((environments: ReadonlyArray<{ environmentId: string }>) =>
    new Set(environments.map(({ environmentId }) => environmentId))) as never,
}));

import { ReviewRulesSettings } from "./ReviewRulesSettings";

function setup(
  reviewRules: ReviewRules,
  project: { readonly overrides: boolean } | null = null,
): void {
  state.settings = { ...DEFAULT_SERVER_SETTINGS, reviewRules } as Settings;
  state.projectId = project === null ? null : "project-1";
  state.source = project?.overrides ? "project" : "environment";
  state.update.mockReset();
  state.clear.mockReset();
}

const recommendedButton = () => page.getByRole("button", { name: "Use recommended review rules" });

describe("ReviewRulesSettings (PK10 T6)", () => {
  it("turns the preset on with one click and blocks a second click until saved", async () => {
    setup({ preset: "off" });
    await using screen = await render(<ReviewRulesSettings isProjectScope={false} />);

    await expect.element(screen.getByText("Off", { exact: true })).toBeVisible();
    // Already off: Turn off has nothing to save.
    await expect.element(screen.getByRole("button", { name: "Turn off" })).toBeDisabled();
    await userEvent.click(recommendedButton());
    expect(state.update).toHaveBeenCalledTimes(1);
    expect(state.update.mock.calls[0]![0]).toEqual({ reviewRules: { preset: "recommended" } });
    await expect.element(recommendedButton()).toBeDisabled();

    await act(() => state.update.mock.calls[0]![1]!.onSaved!());
    await expect.element(recommendedButton()).toBeEnabled();
  });

  it("shows a failed save and keeps the old value", async () => {
    setup({ preset: "off" });
    await using screen = await render(<ReviewRulesSettings isProjectScope={false} />);

    await userEvent.click(recommendedButton());
    await act(() => state.update.mock.calls[0]![1]!.onFailed!());
    await expect.element(screen.getByRole("alert")).toBeVisible();
    expect(screen.getByRole("alert").element().textContent).toContain("Not saved");
    await expect.element(screen.getByText("Off", { exact: true })).toBeVisible();
    await expect.element(recommendedButton()).toBeEnabled();
  });

  it("shows the effective values, marking hand-set ones", async () => {
    setup({ preset: "recommended", minors: "free" });
    await using screen = await render(<ReviewRulesSettings isProjectScope={false} />);

    const list = screen.getByRole("group", { name: "Effective review rules" });
    await expect.element(list).toBeVisible();
    await expect.element(list.getByText("No rule (free)")).toBeVisible();
    await expect.element(list.getByText(".github/pr-queue.json")).toBeVisible();
    expect(list.getByText("set by hand").elements()).toHaveLength(1);
    // Already the preset with a hand-set field: the button asks before it drops the field.
    await userEvent.click(recommendedButton());
    const dialog = page.getByRole("alertdialog");
    await expect.element(dialog).toBeVisible();
    expect(dialog.element().textContent).toContain("small findings");
    expect(state.update).not.toHaveBeenCalled();
    await userEvent.click(dialog.getByRole("button", { name: "Replace" }));
    expect(state.update.mock.calls[0]![0]).toEqual({ reviewRules: { preset: "recommended" } });
  });

  it("warns about an unreadable value and replaces it only after a confirm", async () => {
    setup({ preset: "off", unreadable: { preset: "strict" } });
    await using screen = await render(<ReviewRulesSettings isProjectScope={false} />);

    await expect.element(screen.getByRole("status")).toBeVisible();
    expect(screen.getByRole("status").element().textContent).toContain("not valid");
    await expect.element(screen.getByText('{"preset":"strict"}')).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Turn off" }));
    const dialog = page.getByRole("alertdialog");
    await expect.element(dialog).toBeVisible();
    expect(dialog.element().textContent).toContain("cannot be read");
    await userEvent.click(dialog.getByRole("button", { name: "Cancel" }));
    expect(state.update).not.toHaveBeenCalled();
  });

  it("names a project's override and shows its effective values", async () => {
    setup({ preset: "recommended", reportMaxChars: 5000 }, { overrides: true });
    await using screen = await render(<ReviewRulesSettings isProjectScope />);

    await expect
      .element(screen.getByText(/^This project overrides the environment's review rules/u))
      .toBeVisible();
    await expect.element(screen.getByText("5000 characters")).toBeVisible();
  });
});
