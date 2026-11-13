// Fork: the fork's file link menu entries and remote editor links (#17); upstream's tests stay
// in MarkdownFileLink.logic.test.ts.
import { describe, expect, it } from "vite-plus/test";

import { resolveMarkdownFileLinkMeta } from "../../markdown-links";
import {
  forkMarkdownFileMenu,
  forkMarkdownFileMenuItems,
  markdownFileRemoteEditorUrl,
} from "./forkMarkdownFileLinkMenu";

const NOTHING = { hasLine: false, canOpenInPanel: false, canDownload: false };
// What upstream's MarkdownFileLink.tsx builds: its own entries, then its copies.
const UPSTREAM_OPENS = [
  { id: "preview-media", label: "Preview media" },
  { id: "open", label: "Open in VS Code" },
  { id: "open-in-browser", label: "Open in integrated browser" },
  { id: "reveal", label: "Reveal in Files" },
] as const;
const UPSTREAM_COPIES = [
  { id: "copy-relative", label: "Copy relative path" },
  { id: "copy-full", label: "Copy full path" },
] as const;

describe("forkMarkdownFileMenuItems", () => {
  it("offers every way to open a workspace file at a line, then the copies", () => {
    expect(
      forkMarkdownFileMenuItems([UPSTREAM_OPENS[1], UPSTREAM_OPENS[3], ...UPSTREAM_COPIES], {
        hasLine: true,
        canOpenInPanel: true,
        canDownload: true,
      }),
    ).toEqual([
      { id: "open-in-panel", label: "Open in files viewer" },
      { id: "open-full-size", label: "Open full size" },
      { id: "open", label: "Open in VS Code" },
      { id: "reveal", label: "Reveal in Files" },
      { id: "download", label: "Download to this device" },
      { id: "copy-relative", label: "Copy relative path", separatorBefore: true },
      { id: "copy-full", label: "Copy full path" },
      { id: "copy-path-only", label: "Copy full path without line" },
    ]);
  });

  it("puts the files viewer after the media preview and the download last of the opens", () => {
    expect(
      forkMarkdownFileMenuItems([...UPSTREAM_OPENS, ...UPSTREAM_COPIES], {
        hasLine: false,
        canOpenInPanel: true,
        canDownload: true,
      }).map((item) => item.id),
    ).toEqual([
      "preview-media",
      "open-in-panel",
      "open-full-size",
      "open",
      "open-in-browser",
      "reveal",
      "download",
      "copy-relative",
      "copy-full",
    ]);
  });

  it("keeps only the full path copy when nothing can open the file", () => {
    expect(forkMarkdownFileMenuItems([UPSTREAM_COPIES[1]], NOTHING)).toEqual([
      { id: "copy-full", label: "Copy full path", separatorBefore: false },
    ]);
  });

  it("changes nothing in upstream's entries beyond the separator before the copies", () => {
    expect(forkMarkdownFileMenuItems([...UPSTREAM_OPENS, ...UPSTREAM_COPIES], NOTHING)).toEqual([
      ...UPSTREAM_OPENS,
      { ...UPSTREAM_COPIES[0], separatorBefore: true },
      UPSTREAM_COPIES[1],
    ]);
  });

  it("keeps an entry upstream adds later, with everything upstream set on it", () => {
    const added = { id: "open-in-terminal", label: "Open in terminal", disabled: true } as const;
    expect(
      forkMarkdownFileMenuItems([UPSTREAM_OPENS[1], added, ...UPSTREAM_COPIES], {
        ...NOTHING,
        canDownload: true,
      }),
    ).toEqual([
      UPSTREAM_OPENS[1],
      added,
      { id: "download", label: "Download to this device" },
      { ...UPSTREAM_COPIES[0], separatorBefore: true },
      UPSTREAM_COPIES[1],
    ]);
  });
});

describe("forkMarkdownFileMenu", () => {
  it("shows the joined entries where upstream asked and returns what was picked", async () => {
    const shown: unknown[][] = [];
    const menu = forkMarkdownFileMenu(
      {
        show: async (items, position, source) => {
          shown.push([items.map((item) => item.id), position, source]);
          return items.at(-1)?.id ?? null;
        },
      },
      { hasLine: true, canOpenInPanel: false, canDownload: false },
    );

    await expect(menu.show([UPSTREAM_COPIES[1]], { x: 4, y: 9 }, "keyboard")).resolves.toBe(
      "copy-path-only",
    );
    expect(shown).toEqual([[["copy-full", "copy-path-only"], { x: 4, y: 9 }, "keyboard"]]);
  });
});

describe("markdownFileRemoteEditorUrl", () => {
  const host = { kind: "ssh-alias", host: "devbox" } as const;
  const urlFor = (href: string, cwd: string | undefined) => {
    const meta = resolveMarkdownFileLinkMeta(href, cwd);
    if (!meta) throw new Error(`Expected a file link for ${href}`);
    return markdownFileRemoteEditorUrl({
      editor: "vscode",
      host,
      path: meta.filePath,
      cwd,
      line: meta.line,
      column: meta.column,
    });
  };

  it.each([
    {
      name: "a Linux path with spaces and a #L anchor",
      href: "/srv/my project/notes file.md#L12",
      cwd: "/srv/my project",
      expected: "vscode://vscode-remote/ssh-remote+devbox/srv/my%20project/notes%20file.md:12:1",
    },
    {
      name: "a workspace-relative path with line and column",
      href: "src/app.ts:30:5",
      cwd: "/home/dev/project",
      expected: "vscode://vscode-remote/ssh-remote+devbox/home/dev/project/src/app.ts:30:5",
    },
    {
      name: "a Windows server path",
      href: String.raw`C:\work\My Repo\setup.ps1:7`,
      cwd: String.raw`C:\work\My Repo`,
      expected: "vscode://vscode-remote/ssh-remote+devbox/C%3A/work/My%20Repo/setup.ps1:7:1",
    },
    {
      name: "a file without a line",
      href: "/srv/app/README.md",
      cwd: "/srv/app",
      expected: "vscode://vscode-remote/ssh-remote+devbox/srv/app/README.md:1:1",
    },
  ])("opens $name on the environment's host", ({ href, cwd, expected }) => {
    expect(urlFor(href, cwd)).toBe(expected);
  });

  it("carries the login user from the connection settings", () => {
    expect(
      markdownFileRemoteEditorUrl({
        editor: "vscode",
        host: { kind: "tailscale", host: "devbox.example.ts.net", username: "dev" },
        path: "/srv/app/a.ts",
        cwd: undefined,
        line: 3,
        column: undefined,
      }),
    ).toBe("vscode://vscode-remote/ssh-remote+dev%40devbox.example.ts.net/srv/app/a.ts:3:1");
  });

  it.each([
    { editor: "vscode", href: "/../ssh-remote+evil.example/etc/x.ts" },
    { editor: "vscode", href: "/%2E%2E/ssh-remote+evil.example/etc/x.ts" },
    { editor: "zed", href: "/../evil.example/tmp/x.ts" },
    { editor: "zed", href: "/%2E%2E/evil.example/tmp/x.ts" },
    { editor: "zed", href: "/.%2e/evil.example/tmp/x.ts" },
  ] as const)("keeps $editor on the environment's host for $href", ({ editor, href }) => {
    const meta = resolveMarkdownFileLinkMeta(href, "/srv/app");
    const url = meta
      ? markdownFileRemoteEditorUrl({
          editor,
          host,
          path: meta.filePath,
          cwd: "/srv/app",
          line: meta.line,
          column: meta.column,
        })
      : undefined;
    if (url === undefined) return;
    const parsed = new URL(url);
    expect(parsed.href).toBe(url);
    const authority = decodeURIComponent(parsed.pathname.split("/")[1]!);
    expect(authority).toBe(editor === "zed" ? "devbox" : "ssh-remote+devbox");
  });

  it("refuses a relative path it cannot anchor to a workspace", () => {
    expect(
      markdownFileRemoteEditorUrl({
        editor: "vscode",
        host,
        path: "src/app.ts",
        cwd: undefined,
        line: undefined,
        column: undefined,
      }),
    ).toBeUndefined();
  });
});
