// Fork: a chat file link naming an environment downloads from that environment's own connection.
import { EnvironmentId } from "@t3tools/contracts";
import { AsyncResult } from "effect/reactivity";
import { expect, it, vi } from "vite-plus/test";

import { act, render } from "../../../test/render";
import { assetEnvironment } from "../../state/assets";
import { filesystemEnvironment } from "../../state/filesystem";

const state = vi.hoisted(() => ({
  createAssetUrl: vi.fn(),
  metadata: vi.fn(),
  search: vi.fn(),
  download: vi.fn(),
  connected: new Set<string>(),
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
vi.mock(import("../../remoteOpen"), async (original) => ({
  ...(await original()),
  useRemoteOpenResolution: () => ({ state: { mode: "local-exec" }, isResolved: true }) as never,
}));
vi.mock(import("../media/mediaContent"), async (original) => ({
  ...(await original()),
  downloadFile: state.download,
}));

import ChatMarkdown from "../ChatMarkdown";

const HERE = EnvironmentId.make("env-here");

function setup(connected: ReadonlyArray<string>) {
  state.connected = new Set(connected);
  state.createAssetUrl.mockReset().mockResolvedValue(
    AsyncResult.success({ relativeUrl: "/api/assets/token/app.log", expiresAt: 0 }),
  );
  state.metadata.mockReset().mockResolvedValue(AsyncResult.success({ entries: [null] }));
  state.search.mockReset().mockResolvedValue(AsyncResult.success({ entries: [] }));
  state.download.mockReset().mockResolvedValue(undefined);
}

const renderText = (text: string) =>
  render(<ChatMarkdown cwd="/tmp/project" environmentId={HERE} text={text} />);

const chips = (container: HTMLElement) => [
  ...container.querySelectorAll<HTMLElement>(".chat-markdown-file-link"),
];

it("downloads a file on another environment through that environment's connection", async () => {
  setup(["env-there"]);
  await using screen = await renderText("See [app.log](/t3-environment/env-there/srv/app.log:12)");
  const [chip, ...rest] = chips(screen.container);
  expect(rest).toEqual([]);
  expect(chip?.textContent).toContain("app.log");
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

it("asks nothing of an environment this device is not connected to", async () => {
  setup([]);
  await using screen = await renderText("[app.log](/t3-environment/env-there/srv/app.log)");
  await act(async () => chips(screen.container)[0]!.click());
  await vi.waitFor(() =>
    expect(document.body.textContent).toContain(
      "This file is on env-there, which this device is not connected to.",
    ),
  );
  expect(state.createAssetUrl).not.toHaveBeenCalled();
  expect(state.download).not.toHaveBeenCalled();
});

it("leaves a plain path link as it was", async () => {
  setup(["env-there"]);
  await using screen = await renderText("[a.ts](/tmp/project/a.ts)");
  await act(async () => chips(screen.container)[0]!.click());
  expect(state.createAssetUrl).not.toHaveBeenCalled();
  expect(state.download).not.toHaveBeenCalled();
});
