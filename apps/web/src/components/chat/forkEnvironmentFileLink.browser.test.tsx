// Fork: a chat file link naming an environment downloads from that environment's own connection.
import { EnvironmentId } from "@t3tools/contracts";
import { AsyncResult, type Atom } from "effect/reactivity";
import { expect, it, vi } from "vite-plus/test";

import { act, render } from "../../../test/render";
import { AppAtomRegistryProvider, appAtomRegistry } from "../../rpc/atomRegistry";
import { assetEnvironment } from "../../state/assets";
import { filesystemEnvironment } from "../../state/filesystem";

const state = vi.hoisted(() => ({
  createAssetUrl: vi.fn(),
  metadata: vi.fn(),
  search: vi.fn(),
  download: vi.fn(),
  connected: new Set<string>(),
  savedAtom: null as unknown,
}));

vi.mock(import("../../state/use-atom-query-runner"), async (original) => ({
  ...(await original()),
  useAtomQueryRunner: ((family: unknown) =>
    family === assetEnvironment.createUrl
      ? state.createAssetUrl
      : family === filesystemEnvironment.getMetadata
        ? state.metadata
        : state.search) as never,
}));
vi.mock(import("../../state/server"), async (original) => {
  const actual = await original();
  const { Atom } = await import("effect/reactivity");
  const noConfig = Atom.make(null);
  return {
    ...actual,
    serverEnvironment: { ...actual.serverEnvironment, configValueAtom: (() => noConfig) as never },
  };
});
vi.mock(import("../../state/session"), async (original) => {
  const { AuthStandardClientScopes } = await import("@t3tools/contracts");
  const granted = new Set<string>(AuthStandardClientScopes);
  const hasScope = (environmentId: unknown, scope: string) =>
    environmentId !== null && granted.has(scope);
  return {
    ...(await original()),
    useEnvironmentScope: hasScope as never,
    readEnvironmentScope: hasScope as never,
    readPreparedConnection: ((environmentId: string) =>
      state.connected.has(environmentId)
        ? { httpBaseUrl: `https://${environmentId}.example/` }
        : null) as never,
  };
});
vi.mock(import("../../state/presentation"), async (original) => {
  const actual = await original();
  const { Atom } = await import("effect/reactivity");
  // Writable, so each test sets its own saved environments.
  const presentations = Atom.make(new Map()).pipe(Atom.keepAlive);
  state.savedAtom = presentations;
  return {
    ...actual,
    environmentPresentations: {
      ...actual.environmentPresentations,
      presentationsAtom: presentations as never,
    },
  };
});
vi.mock(import("../../remoteOpen"), async (original) => ({
  ...(await original()),
  useRemoteOpenResolution: () => ({ state: { mode: "local-exec" }, isResolved: true }) as never,
}));
vi.mock(import("../media/mediaContent"), async (original) => ({
  ...(await original()),
  downloadFile: state.download,
}));

import ChatMarkdown from "../ChatMarkdown";
import { toastManager } from "../ui/toast";

const HERE = EnvironmentId.make("env-here");

function setup(
  connected: ReadonlyArray<string>,
  saved: ReadonlyArray<[string, { label: string; phase: string }]> = [],
) {
  state.connected = new Set(connected);
  appAtomRegistry.set(
    state.savedAtom as Atom.Writable<Map<string, unknown>>,
    new Map(
      saved.map(([id, { label, phase }]) => [
        id,
        {
          entry: {
            target: {
              _tag: "PrimaryConnectionTarget",
              label,
              httpBaseUrl: `https://${id}.example/`,
            },
          },
          connection: { phase, error: null, traceId: null },
          serverConfig: null,
          refusedRouteIds: [],
        },
      ]),
    ),
  );
  state.createAssetUrl
    .mockReset()
    .mockResolvedValue(
      AsyncResult.success({ relativeUrl: "/api/assets/token/app.log", expiresAt: 0 }),
    );
  state.metadata.mockReset().mockResolvedValue(AsyncResult.success({ entries: [null] }));
  state.search.mockReset().mockResolvedValue(AsyncResult.success({ entries: [] }));
  state.download.mockReset().mockResolvedValue(undefined);
}

const renderText = (text: string) =>
  render(
    <AppAtomRegistryProvider>
      <ChatMarkdown cwd="/tmp/project" environmentId={HERE} text={text} />
    </AppAtomRegistryProvider>,
  );

const chips = (container: HTMLElement) => [
  ...container.querySelectorAll<HTMLElement>(".chat-markdown-file-link"),
];

it("downloads a file on another environment through that environment's connection", async () => {
  setup(["env-there"], [["env-there", { label: "Build box", phase: "connected" }]]);
  await using screen = await renderText("See [app.log](/t3-environment/env-there/srv/app.log:12)");
  const [chip, ...rest] = chips(screen.container);
  expect(rest).toEqual([]);
  expect(chip?.textContent).toContain("app.log on Build box");
  expect(screen.container.textContent).not.toContain("t3-environment");
  await act(async () => chip!.click());
  await vi.waitFor(() => expect(state.download).toHaveBeenCalledOnce());
  expect(state.createAssetUrl).toHaveBeenCalledWith({
    environmentId: "env-there",
    input: {
      resource: {
        _tag: "media-file",
        threadId: "environment-file-link",
        path: "/srv/app.log",
        disposition: "attachment",
      },
    },
  });
  expect(state.download).toHaveBeenCalledWith(
    "https://env-there.example/api/assets/token/app.log",
    "app.log",
  );
});

async function clickAndReadToast(text: string) {
  const toast = vi.spyOn(toastManager, "add");
  await using screen = await renderText(text);
  await act(async () => chips(screen.container)[0]!.click());
  await vi.waitFor(() => expect(toast).toHaveBeenCalled());
  const description = String(
    (toast.mock.calls.at(-1)?.[0] as { description?: unknown } | undefined)?.description,
  );
  toast.mockRestore();
  return { description, chipText: chips(screen.container)[0]?.textContent ?? "" };
}

it("asks nothing of an environment this device does not know, and never shows its id", async () => {
  setup([]);
  const { description, chipText } = await clickAndReadToast(
    "[app.log](/t3-environment/env-there/srv/app.log)",
  );
  expect(description).toContain("This file is on an environment this device does not know.");
  expect(description).not.toContain("env-there");
  expect(chipText).toContain("app.log on unknown environment");
  expect(state.createAssetUrl).not.toHaveBeenCalled();
  expect(state.download).not.toHaveBeenCalled();
});

it("says to try again while this device reconnects to the environment", async () => {
  setup([], [["env-there", { label: "Build box", phase: "reconnecting" }]]);
  const { description } = await clickAndReadToast(
    "[app.log](/t3-environment/env-there/srv/app.log)",
  );
  expect(description).toBe("This device is reconnecting to Build box. Try again in a moment.");
  expect(state.createAssetUrl).not.toHaveBeenCalled();
});

it("leaves a plain path link as it was", async () => {
  setup(["env-there"]);
  await using screen = await renderText("[a.ts](/tmp/project/a.ts)");
  await act(async () => chips(screen.container)[0]!.click());
  expect(state.createAssetUrl).not.toHaveBeenCalled();
  expect(state.download).not.toHaveBeenCalled();
});
