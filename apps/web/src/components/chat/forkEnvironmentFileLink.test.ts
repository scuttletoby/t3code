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
