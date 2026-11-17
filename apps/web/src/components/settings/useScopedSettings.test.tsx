import {
  DEFAULT_SERVER_SETTINGS,
  EnvironmentId,
  ProjectId,
  type ServerSettings,
  type ServerSettingsPatch,
} from "@t3tools/contracts";
import { keepLeftOutForkProjectKeys } from "@t3tools/shared/forkSettingsPatch";
import { applyServerSettingsPatch } from "@t3tools/shared/serverSettings";
import * as Cause from "effect/Cause";
import { AsyncResult } from "effect/reactivity";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vite-plus/test";

type Request = { environmentId: EnvironmentId; input: { patch: ServerSettingsPatch } };

// The fake environments: what each one stores, and which ones refuse a save.
const state = vi.hoisted(() => ({
  context: null as unknown,
  /** Never reset: environment ids stay unique across tests. */
  lastId: 0,
  stored: new Map<string, unknown>(),
  refusing: new Set<string>(),
  sent: [] as Array<{ environmentId: string; patch: unknown }>,
  /** Runs after each answered save, once the fake environment stores its result. */
  answered: [] as Array<() => void>,
  toast: vi.fn((_toast: { type: string; title: string; description?: string }) => {}),
}));

vi.mock(import("./SettingsScopeContext"), async (original) => ({
  ...(await original()),
  useSettingsScope: (() => state.context) as never,
  useOptionalSettingsScope: (() => state.context) as never,
}));
vi.mock(import("../../state/environments"), async (original) => ({
  ...(await original()),
  readEnvironment: ((environmentId: string) =>
    state.stored.has(environmentId)
      ? { serverConfig: { settings: state.stored.get(environmentId) } }
      : null) as never,
}));
vi.mock(import("../../state/session"), async (original) => ({
  ...(await original()),
  readEnvironmentScope: () => true,
}));
vi.mock(import("../../state/use-atom-command"), async (original) => ({
  ...(await original()),
  // The server applies the patch the way the real one does, keeping fork keys an entry leaves out.
  useAtomCommand: (() =>
    async ({ environmentId, input: { patch } }: Request) => {
      state.sent.push({ environmentId, patch });
      if (state.refusing.has(environmentId)) {
        return AsyncResult.failure(Cause.fail(new Error("Connection lost")));
      }
      queueMicrotask(() => state.answered.splice(0).forEach((run) => run()));
      const settings = state.stored.get(environmentId) as ServerSettings;
      const saved = keepLeftOutForkProjectKeys(
        settings,
        patch,
        applyServerSettingsPatch(settings, patch),
      );
      state.stored.set(environmentId, saved);
      return AsyncResult.success(saved);
    }) as never,
}));
vi.mock(import("../ui/toast"), async (original) => ({
  ...(await original()),
  toastManager: { add: state.toast } as never,
}));

import { useClearScopedSettings, useUpdateScopedSettings } from "./useScopedSettings";

type Failure = { readonly failed: readonly string[]; readonly saved: readonly string[] };

/** Fresh environment ids per test: the hook's writer keeps per-environment state for the module. */
function setup(
  environments: ReadonlyArray<{
    readonly label: string;
    readonly settings?: Partial<ServerSettings>;
    readonly refuses?: boolean;
    readonly connected?: boolean;
  }>,
  scope: "all" | "project" = "all",
) {
  state.stored.clear();
  state.refusing.clear();
  state.sent.length = 0;
  state.answered.length = 0;
  state.toast.mockReset();
  const projectId = ProjectId.make("project");
  const targets = environments.map(({ label, settings, refuses, connected }) => {
    const environmentId = EnvironmentId.make(`${label}-${(state.lastId += 1)}`);
    const stored = { ...DEFAULT_SERVER_SETTINGS, ...settings } as ServerSettings;
    state.stored.set(environmentId, stored);
    if (refuses) state.refusing.add(environmentId);
    return {
      environmentId,
      label,
      connection: { phase: connected === false ? ("offline" as const) : ("connected" as const) },
      serverConfig: { settings: stored },
    };
  });
  const environmentIds = targets.map(({ environmentId }) => environmentId);
  state.context = {
    scope:
      scope === "all"
        ? { kind: "all", label: "All", environmentIds }
        : {
            kind: "project",
            group: {},
            environmentId: null,
            label: "project",
            members: environmentIds.map((environmentId) => ({ id: projectId, environmentId })),
            environmentIds,
          },
    environments: targets,
    deviceOnly: false,
  };
  return { projectId, environmentIds };
}

interface ScopedSettingsHooks {
  readonly update: ReturnType<typeof useUpdateScopedSettings>;
  readonly clear: ReturnType<typeof useClearScopedSettings>;
}

function Probe(props: { readonly onResult: (hooks: ScopedSettingsHooks) => void }) {
  props.onResult({ update: useUpdateScopedSettings(), clear: useClearScopedSettings() });
  return null;
}

/** The hooks read no React state, so one server render hands back the functions they return. */
function hooks(): ScopedSettingsHooks {
  let result: ScopedSettingsHooks | undefined;
  renderToStaticMarkup(<Probe onResult={(next) => (result = next)} />);
  return result!;
}

/** Saves through the hook; resolves with `"saved"` or with what `onFailed` was given. */
function save(patch: ServerSettingsPatch) {
  return new Promise<"saved" | { readonly failure: Failure | undefined }>((resolve) =>
    hooks().update(patch, {
      onSaved: () => resolve("saved"),
      onFailed: (failure) => resolve({ failure }),
    }),
  );
}

/** Resolves once `count` saves have been answered and stored. */
function answeredSaves(count: number) {
  return new Promise<void>((resolve) => {
    const check = () => (state.sent.length >= count ? resolve() : void state.answered.push(check));
    state.answered.push(check);
  });
}

const recommended = { reviewRules: { preset: "recommended" as const } };

describe("useUpdateScopedSettings failure labels (#357)", () => {
  it("all saved: onSaved, no failure and no toast", async () => {
    // An empty settings file reads as the defaults.
    setup([{ label: "Laptop" }, { label: "Server" }]);
    await expect(save(recommended)).resolves.toBe("saved");
    expect(state.sent).toHaveLength(2);
    expect(state.toast).not.toHaveBeenCalled();
  });

  it("all failed: every environment is named as failed, none as saved", async () => {
    setup([
      { label: "Laptop", refuses: true },
      { label: "Server", refuses: true },
    ]);
    await expect(save(recommended)).resolves.toEqual({
      failure: { failed: ["Laptop", "Server"], saved: [] },
    });
    expect(state.toast).toHaveBeenCalledWith(
      expect.objectContaining({ type: "error", title: "Setting not saved" }),
    );
  });

  it("some failed: names where it failed and where it saved", async () => {
    setup([{ label: "Laptop" }, { label: "Server", refuses: true }]);
    await expect(save(recommended)).resolves.toEqual({
      failure: { failed: ["Server"], saved: ["Laptop"] },
    });
    expect(state.toast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Setting saved on some environments" }),
    );
  });

  it("nothing sent: onFailed with no details, and nothing reaches a server", async () => {
    setup([{ label: "Laptop", connected: false }]);
    await expect(save(recommended)).resolves.toEqual({ failure: undefined });
    expect(state.sent).toHaveLength(0);
    expect(state.toast).toHaveBeenCalledWith(
      expect.objectContaining({ type: "warning", title: "Setting not saved" }),
    );
  });
});

describe("useClearScopedSettings state", () => {
  it("a reset clicked twice over an unreadable override removes it and keeps the other override", async () => {
    const unreadable = { preset: "off" as const, unreadable: { preset: "strict" } };
    const { projectId, environmentIds } = setup(
      [
        {
          label: "Laptop",
          settings: {
            projectSettingsOverrides: {
              [ProjectId.make("project")]: {
                reviewRules: unreadable,
                pullRequestMergeMethod: "squash" as const,
              },
            },
          },
        },
      ],
      "project",
    );
    // Both clicks come from the same render, so the second plans from the old settings.
    const { clear } = hooks();
    clear(["reviewRules"]);
    clear(["reviewRules"]);
    await answeredSaves(2);
    const stored = state.stored.get(environmentIds[0]!) as ServerSettings;
    expect(stored.projectSettingsOverrides[projectId]).toEqual({
      pullRequestMergeMethod: "squash",
    });
    // The second reset finds nothing left to clear and resends the other override unchanged.
    expect(state.sent).toHaveLength(2);
  });
});
