import { describe, expect, it } from "vite-plus/test";

import { resolveMarkdownFileLinkMeta } from "../../markdown-links";
import { markdownFileMenuItems, markdownFileRemoteEditorUrl } from "./MarkdownFileLink.logic";

const NOTHING = {
  hasLine: false,
  workspaceRelativePath: null,
  canOpenMedia: false,
  canOpenInPanel: false,
  openInEditorLabel: null,
  canOpenInBrowser: false,
  revealLabel: null,
  canDownload: false,
};

describe("markdownFileMenuItems", () => {
  it("offers every way to open a workspace file at a line, then the copies", () => {
    expect(
      markdownFileMenuItems({
        ...NOTHING,
        hasLine: true,
        workspaceRelativePath: "src/app.ts",
        canOpenInPanel: true,
        openInEditorLabel: "Open in VS Code",
        revealLabel: "Reveal in Files",
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

  it("keeps only the full path copy when nothing can open the file", () => {
    expect(markdownFileMenuItems(NOTHING)).toEqual([
      { id: "copy-full", label: "Copy full path", separatorBefore: false },
    ]);
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
