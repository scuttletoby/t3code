// Fork: file links that name the environment holding the file.
import {
  AssetWorkspaceAssetNotFoundError,
  ENVIRONMENT_FILE_LINK_THREAD_ID,
  EnvironmentId,
  ThreadId,
} from "@t3tools/contracts";
import * as Cause from "effect/Cause";
import { AsyncResult } from "effect/reactivity";
import { describe, expect, it, vi } from "vite-plus/test";

import { resolveMarkdownFileLinkMeta } from "../../markdown-links";
import {
  DOWNLOAD_LABEL,
  environmentFileAccess,
  environmentFileDownloadUrl,
  environmentFileLinkProps,
  type CreateAssetUrl,
  type EnvironmentFileLinkChat,
  type EnvironmentFileAccessReaders,
} from "./forkEnvironmentFileLink";

const THERE = EnvironmentId.make("env-there");

const readers = (
  overrides: Partial<EnvironmentFileAccessReaders> = {},
): EnvironmentFileAccessReaders => ({
  httpBaseUrl: (id) => (id === THERE ? "https://there.example:3773/" : null),
  canReadFiles: () => true,
  label: (id) => (id === THERE ? "Build box" : null),
  reconnecting: () => false,
  ...overrides,
});

const signed = () =>
  vi.fn<CreateAssetUrl>(async () =>
    AsyncResult.success({ relativeUrl: "/api/assets/token123/app.log", expiresAt: 0 }),
  );

describe("environmentFileAccess", () => {
  it("uses the named environment's own connection", () => {
    expect(environmentFileAccess(THERE, readers())).toEqual({
      httpBaseUrl: "https://there.example:3773/",
    });
  });

  it("names an environment this device is not connected to", () => {
    expect(environmentFileAccess(THERE, readers({ httpBaseUrl: () => null }))).toEqual({
      unavailable:
        "This file is on Build box, which this device is not connected to. Connect to Build box in Settings > Connections, then try again.",
    });
  });

  it("never shows the id from the link for an environment this device does not know", () => {
    const crafted = EnvironmentId.make("Your-bank-says-call-0800");
    const access = environmentFileAccess(crafted, readers());
    expect(access).toEqual({
      unavailable:
        "This file is on an environment this device does not know. Add it in Settings > Connections, then try again.",
    });
    expect(JSON.stringify(access)).not.toContain("0800");
  });

  it("says to try again while it reconnects, not to connect or that it may not read", () => {
    const reconnecting = { reconnecting: () => true };
    const message = {
      unavailable: "This device is reconnecting to Build box. Try again in a moment.",
    };
    expect(
      environmentFileAccess(THERE, readers({ ...reconnecting, httpBaseUrl: () => null })),
    ).toEqual(message);
    expect(
      environmentFileAccess(THERE, readers({ ...reconnecting, canReadFiles: () => false })),
    ).toEqual(message);
    expect(environmentFileAccess(THERE, readers(reconnecting))).toEqual({
      httpBaseUrl: "https://there.example:3773/",
    });
  });

  it("refuses without file read there", () => {
    expect(environmentFileAccess(THERE, readers({ canReadFiles: () => false }))).toEqual({
      unavailable: "This device may not read files on Build box.",
    });
  });
});

describe("environmentFileDownloadUrl", () => {
  const download = (
    createAssetUrl: CreateAssetUrl,
    overrides: Partial<EnvironmentFileAccessReaders> = {},
  ) =>
    environmentFileDownloadUrl({
      environmentId: THERE,
      path: "/home/a/app.log",
      readers: readers(overrides),
      createAssetUrl,
    });

  it("asks the file's environment and resolves the URL against that connection", async () => {
    const createAssetUrl = signed();
    await expect(download(createAssetUrl)).resolves.toBe(
      "https://there.example:3773/api/assets/token123/app.log",
    );
    expect(createAssetUrl).toHaveBeenCalledWith({
      environmentId: THERE,
      input: {
        resource: {
          _tag: "media-file",
          threadId: ENVIRONMENT_FILE_LINK_THREAD_ID,
          path: "/home/a/app.log",
          disposition: "attachment",
        },
      },
    });
  });

  it("asks nothing when the environment is not connected", async () => {
    const createAssetUrl = vi.fn<CreateAssetUrl>();
    await expect(download(createAssetUrl, { httpBaseUrl: () => null })).rejects.toThrow(
      "This file is on Build box, which this device is not connected to.",
    );
    expect(createAssetUrl).not.toHaveBeenCalled();
  });

  it("refuses a home-folder path by name and asks nothing", async () => {
    const createAssetUrl = vi.fn<CreateAssetUrl>();
    await expect(
      environmentFileDownloadUrl({
        environmentId: THERE,
        path: "~/notes.txt",
        readers: readers(),
        createAssetUrl,
      }),
    ).rejects.toThrow(
      "A link to a file on Build box needs the file's absolute path; ~/notes.txt starts at a home folder.",
    );
    expect(createAssetUrl).not.toHaveBeenCalled();
  });

  it("says where it looked when the file is missing there", async () => {
    const createAssetUrl = vi.fn<CreateAssetUrl>(async () =>
      AsyncResult.failure(
        Cause.fail(
          new AssetWorkspaceAssetNotFoundError({
            resource: {
              _tag: "media-file",
              threadId: ThreadId.make("t"),
              path: "/home/a/app.log",
            },
            checkedPath: "/home/a/app.log",
          }),
        ),
      ),
    );
    await expect(download(createAssetUrl)).rejects.toThrow(
      "The file was not found on Build box at /home/a/app.log.",
    );
  });
});

describe("environmentFileLinkProps", () => {
  const deps = (createAssetUrl = signed()) => ({
    readers: readers(),
    createAssetUrl,
    downloadFile: vi.fn(async () => undefined),
  });

  it("is null for a link that names no environment", () => {
    expect(environmentFileLinkProps(resolveMarkdownFileLinkMeta("/srv/app.log")!, deps())).toBe(
      null,
    );
  });

  it("downloads from the named environment and drops this device's actions", async () => {
    const meta = resolveMarkdownFileLinkMeta("/t3-environment/env-there/srv/app.log:12")!;
    const linkDeps = deps();
    const props = environmentFileLinkProps(meta, linkDeps)!;
    expect(props).toMatchObject({
      environmentId: null,
      label: "app.log on Build box",
      href: "/srv/app.log",
      targetPath: "/srv/app.log",
      panelPath: null,
      workspaceRelativePath: null,
      openInEditorMenuLabel: DOWNLOAD_LABEL,
      onDownload: undefined,
      onOpenMedia: undefined,
      onOpenInBrowser: undefined,
      onReveal: undefined,
    });
    expect((await props.onOpen())._tag).toBe("Success");
    expect(linkDeps.downloadFile).toHaveBeenCalledWith(
      "https://there.example:3773/api/assets/token123/app.log",
      "app.log",
    );
    expect((await props.resolveCopyPaths()).pathOnly).toBe("/srv/app.log");
    expect(await props.resolveDragPath()).toBe("/srv/app.log");
  });

  it("reports a failure through the link's own open result, naming no unknown id", async () => {
    const meta = resolveMarkdownFileLinkMeta("/t3-environment/env-gone/srv/app.log")!;
    const props = environmentFileLinkProps(meta, deps())!;
    expect(props.label).toBe("app.log on unknown environment");
    const result = await props.onOpen();
    if (result._tag !== "Failure") throw new Error("Expected the open to fail");
    const error = Cause.squash(result.cause);
    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toContain("does not know");
    expect((error as Error).message).not.toContain("env-gone");
  });
});

// Fork: inside a chat, a click previews media or opens the files viewer, both on
// the link's environment, and asks that environment nothing before the click.
describe("environmentFileLinkProps in a chat", () => {
  const chat = () => ({
    expandMedia: vi.fn<EnvironmentFileLinkChat["expandMedia"]>(),
    openInPanel: vi.fn<EnvironmentFileLinkChat["openInPanel"]>(),
    reportMediaError: vi.fn<EnvironmentFileLinkChat["reportMediaError"]>(),
  });
  const message = (reported: { mock: { calls: Array<Array<unknown>> } }) =>
    (reported.mock.calls[0]![0] as Error).message;
  const setup = (link: string, overrides: Partial<EnvironmentFileAccessReaders> = {}) => {
    const createAssetUrl = signed();
    const linkChat = chat();
    const props = environmentFileLinkProps(resolveMarkdownFileLinkMeta(link)!, {
      readers: readers(overrides),
      createAssetUrl,
      downloadFile: vi.fn(async () => undefined),
      chat: linkChat,
    })!;
    return { props, createAssetUrl, chat: linkChat };
  };

  it("previews an image from the link's environment, signed inline there, only on click", async () => {
    const { props, createAssetUrl, chat } = setup("/t3-environment/env-there/srv/shot.png");
    expect(props.panelPath).toBe(null);
    expect(createAssetUrl).not.toHaveBeenCalled();
    props.onOpenMedia!();
    await vi.waitFor(() => expect(chat.expandMedia).toHaveBeenCalledOnce());
    const resource = {
      _tag: "media-file",
      threadId: ENVIRONMENT_FILE_LINK_THREAD_ID,
      path: "/srv/shot.png",
    };
    expect(createAssetUrl).toHaveBeenCalledWith({ environmentId: THERE, input: { resource } });
    const src = "https://there.example:3773/api/assets/token123/app.log";
    expect(chat.expandMedia).toHaveBeenCalledWith({
      images: [
        {
          src,
          name: "shot.png",
          actionsSource: {
            kind: "image",
            name: "shot.png",
            src,
            asset: { environmentId: THERE, resource },
          },
        },
      ],
      index: 0,
    });
    expect(chat.reportMediaError).not.toHaveBeenCalled();
  });

  it("previews a video as a video", async () => {
    const { props, chat } = setup("/t3-environment/env-there/srv/run.mp4");
    props.onOpenMedia!();
    await vi.waitFor(() => expect(chat.expandMedia).toHaveBeenCalledOnce());
    expect(chat.expandMedia.mock.calls[0]?.[0].images[0]).toMatchObject({
      type: "video",
      autoPlay: false,
      actionsSource: { kind: "video" },
    });
  });

  it("previews nothing and asks nothing without file read there", async () => {
    const { props, createAssetUrl, chat } = setup("/t3-environment/env-there/srv/shot.png", {
      canReadFiles: () => false,
    });
    props.onOpenMedia!();
    await vi.waitFor(() => expect(chat.reportMediaError).toHaveBeenCalledOnce());
    expect(createAssetUrl).not.toHaveBeenCalled();
    expect(chat.expandMedia).not.toHaveBeenCalled();
    expect(message(chat.reportMediaError)).toBe("This device may not read files on Build box.");
  });

  it("previews nothing and asks nothing when the environment is not connected", async () => {
    const { props, createAssetUrl, chat } = setup("/t3-environment/env-there/srv/shot.png", {
      httpBaseUrl: () => null,
    });
    props.onOpenMedia!();
    await vi.waitFor(() => expect(chat.reportMediaError).toHaveBeenCalledOnce());
    expect(createAssetUrl).not.toHaveBeenCalled();
    expect(chat.expandMedia).not.toHaveBeenCalled();
    expect(message(chat.reportMediaError)).toContain(
      "Build box, which this device is not connected to",
    );
  });

  it("says where it looked when the media file is missing there", async () => {
    const { props, createAssetUrl, chat } = setup("/t3-environment/env-there/srv/shot.png");
    createAssetUrl.mockResolvedValueOnce(
      AsyncResult.failure(
        Cause.fail(
          new AssetWorkspaceAssetNotFoundError({
            resource: {
              _tag: "media-file",
              threadId: ThreadId.make("environment-file-link"),
              path: "/srv/shot.png",
            },
            checkedPath: "/srv/shot.png",
          }),
        ),
      ),
    );
    props.onOpenMedia!();
    await vi.waitFor(() => expect(chat.reportMediaError).toHaveBeenCalledOnce());
    expect(message(chat.reportMediaError)).toBe(
      "The file was not found on Build box at /srv/shot.png.",
    );
  });

  it("opens any other file in the files viewer under its link path, asking nothing itself", () => {
    const { props, createAssetUrl, chat } = setup("/t3-environment/env-there/srv/app.log:12");
    expect(props.onOpenMedia).toBe(undefined);
    expect(props.panelPath).toBe("/t3-environment/env-there/srv/app.log");
    props.onOpenInPanel(props.panelPath!, 12);
    expect(chat.openInPanel).toHaveBeenLastCalledWith(
      "/t3-environment/env-there/srv/app.log",
      12,
      false,
    );
    props.onOpenFullSize!();
    expect(chat.openInPanel).toHaveBeenLastCalledWith(
      "/t3-environment/env-there/srv/app.log",
      12,
      true,
    );
    expect(createAssetUrl).not.toHaveBeenCalled();
    // The menu still downloads it.
    expect(props.openInEditorMenuLabel).toBe(DOWNLOAD_LABEL);
  });
});
