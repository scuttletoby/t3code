import { describe, expect, it } from "@effect/vitest";

import { buildRemoteOpenUrl, type EditorId } from "./editor.ts";

// The host of a remote editor URL lives in its path (`zed://ssh/<host>/...`,
// `vscode://vscode-remote/ssh-remote+<host>/...`), so a dot segment in the file path must never
// reach the URL: every URL parser removes it together with the segment before it, the host.
describe("buildRemoteOpenUrl keeps the host when the path climbs", () => {
  const climbs = [
    "/../evil.example/tmp/x.ts",
    "/../ssh-remote+evil.example/etc/x.ts",
    "/../../evil.example/x.ts",
    "/./../evil.example/x.ts",
    "/srv/../../evil.example/x.ts",
    String.raw`C:\..\evil.example\x.ts`,
  ];
  const editors: ReadonlyArray<EditorId> = ["vscode", "cursor", "zed"];

  it.each(editors.flatMap((editor) => climbs.map((absolutePath) => ({ editor, absolutePath }))))(
    "$editor: $absolutePath stays on dev@devbox",
    ({ editor, absolutePath }) => {
      const url = buildRemoteOpenUrl({
        editor,
        host: "devbox",
        username: "dev",
        absolutePath,
        isFile: true,
        line: 3,
      });
      expect(url).toBeDefined();
      // The built URL is exactly what a URL parser (Electron, the OS handler) reads.
      expect(new URL(url!).href).toBe(url);
      const segments = new URL(url!).pathname.split("/");
      expect(decodeURIComponent(segments[1]!)).toMatch(
        editor === "zed" ? /^dev@devbox$/ : /^ssh-remote\+dev@devbox$/,
      );
      expect(url).not.toMatch(/\/\.\.?(?:\/|$)/);
    },
  );

  it.each([
    { editor: "vscode", expected: "vscode://vscode-remote/ssh-remote+sol/evil.example/x.ts:1:1" },
    { editor: "zed", expected: "zed://ssh/sol/evil.example/x.ts" },
  ] as const)("$editor resolves a climb above the root to the root", ({ editor, expected }) => {
    expect(
      buildRemoteOpenUrl({
        editor,
        host: "sol",
        absolutePath: "/../evil.example/x.ts",
        isFile: true,
      }),
    ).toBe(expected);
  });

  it("keeps a climb that stays below the root on the same host", () => {
    expect(
      buildRemoteOpenUrl({ editor: "vscode", host: "sol", absolutePath: "/home/u/../v/a.ts" }),
    ).toBe("vscode://vscode-remote/ssh-remote+sol/home/v/a.ts");
  });

  it("keeps a Windows drive as the root a climb cannot leave", () => {
    expect(
      buildRemoteOpenUrl({ editor: "vscode", host: "sol", absolutePath: String.raw`C:\..\x.ts` }),
    ).toBe("vscode://vscode-remote/ssh-remote+sol/C%3A/x.ts");
    expect(
      buildRemoteOpenUrl({ editor: "zed", host: "sol", absolutePath: String.raw`C:\a\..\..\x.ts` }),
    ).toBe("zed://ssh/sol/x.ts");
  });

  it("encodes a literal %2E%2E name so it is never read as a climb", () => {
    const url = buildRemoteOpenUrl({ editor: "zed", host: "sol", absolutePath: "/%2E%2E/x.ts" });
    expect(url).toBe("zed://ssh/sol/%252E%252E/x.ts");
    expect(new URL(url!).href).toBe(url);
  });

  it.each([".", ".."])("refuses a Zed host of %s, which a URL parser would drop", (host) => {
    // `zed://ssh/../a.ts` parses to `zed://ssh/a.ts`: the host segment is gone, and only the
    // parse-to-itself check catches it.
    expect(buildRemoteOpenUrl({ editor: "zed", host, absolutePath: "/a.ts" })).toBeUndefined();
  });

  it("keeps a host with a slash inside its own encoded segment", () => {
    for (const host of ["evil/../x", "a/b", ".."]) {
      for (const editor of ["vscode", "zed"] as const) {
        const url = buildRemoteOpenUrl({ editor, host, username: "dev", absolutePath: "/a.ts" });
        expect(url).toBeDefined();
        expect(new URL(url!).href).toBe(url);
      }
    }
  });
});
