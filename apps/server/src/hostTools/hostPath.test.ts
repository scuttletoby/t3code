// @effect-diagnostics nodeBuiltinImport:off - the adapter test makes real links in a temporary folder.
import * as NodeFSP from "node:fs/promises";
import * as NodeOS from "node:os";
import * as NodePath from "node:path";

import { it as effectIt } from "@effect/vitest";
import * as HostProcess from "@t3tools/shared/HostProcess";
import * as Effect from "effect/Effect";
import { describe, expect, it } from "vite-plus/test";

import {
  foldName,
  type HostPathFs,
  type HostPlatform,
  nodeHostPathFs,
  resolveHostPath,
} from "./hostPath.ts";

// A fake file system: "dir", "file", or a link's target text. Parents of every entry are folders. `shorts` maps a
// Windows short-name path to its long name. Lookups fold names the way the platform's volume compares them.
function fakeFs(
  platform: HostPlatform,
  tree: Record<string, string>,
  shorts: Record<string, string> = {},
  cs = false,
) {
  const norm = (p: string) =>
    p
      .split(platform === "win32" ? /[\\/]/ : /\//)
      .filter(Boolean)
      .map((n) => foldName(n, platform, cs))
      .join("/");
  const entries = new Map(Object.entries(tree).map(([k, v]) => [norm(k), v]));
  const short = new Map(Object.entries(shorts).map(([k, v]) => [norm(k), v]));
  const expand = (p: string) => {
    for (const [s, long] of short)
      if (`${p}/`.startsWith(`${s}/`))
        return norm(`${s.replace(/[^/]+$/, long)}${p.slice(s.length)}`);
    return p;
  };
  const calls = { count: 0 };
  const kindOf = (p: string) => {
    const k = expand(norm(p));
    const v = entries.get(k);
    if (v !== undefined) return v === "dir" || v === "file" ? v : "link";
    return k === "" || /^[a-z]:$/i.test(k) || [...entries.keys()].some((e) => e.startsWith(`${k}/`))
      ? "dir"
      : "missing";
  };
  const fs: HostPathFs = {
    // A walk that never ends (a missing loop guard) fails here instead of spinning: the fake never yields.
    kind: async (p) =>
      ++calls.count > 1e6 ? Promise.reject(new Error("runaway walk")) : (kindOf(p) as "dir"),
    readLink: async (p) => entries.get(expand(norm(p)))!,
    longName: async (p) => short.get(norm(p)),
  };
  return { fs, tree: entries, calls, set: (k: string, v: string) => entries.set(norm(k), v) };
}

// [input, expected compare key (or "!" + part of the refusal), missing names]
type Case = readonly [string, string, number?];
async function runTable(platform: HostPlatform, fs: HostPathFs, cases: ReadonlyArray<Case>) {
  for (const [input, expected, missing = 0] of cases) {
    const result = await resolveHostPath(input, { platform, fs });
    const got = result.ok
      ? { key: result.compareKey, missing: result.missing }
      : { refused: result.reason };
    const want = expected.startsWith("!")
      ? { refused: expect.stringContaining(expected.slice(1)) }
      : { key: expected, missing };
    expect({ input, ...got }).toEqual({ input, ...want });
  }
}

const WIN = fakeFs(
  "win32",
  {
    "C:\\Users\\Bob\\Documents\\notes.txt": "file",
    "C:\\Users\\Bob\\.ssh\\id_rsa": "file",
    "C:\\Users\\Bob\\Documents\\keys": "C:\\Users\\Bob\\.ssh", // a junction into a secret folder
    "C:\\Users\\Bob\\verb": "\\??\\C:\\Users\\Bob\\.ssh", // the junction's substitute-name form
    "C:\\Users\\Bob\\rel": "..\\Bob\\Documents",
    "C:\\Users\\Bob\\vol": "\\\\?\\Volume{0d1f}\\", // a mount point
    "C:\\Users\\Bob\\loop1": "C:\\Users\\Bob\\loop2",
    "C:\\Users\\Bob\\loop2": "loop1",
    "C:\\Program Files\\App\\app.exe": "file",
    "C:\\Users\\ODD~1\\x": "file", // a short-looking name the file system cannot expand
  },
  { "C:\\PROGRA~1": "Program Files" },
);
const NOTES_W = "C:/USERS/BOB/DOCUMENTS/NOTES.TXT";

const MAC = {
  "/Users/bob/Documents/notes.txt": "file",
  "/Users/bob/.ssh/id_rsa": "file",
  "/Users/bob/Documents/keys": "../.ssh",
  "/Users/bob/Caf\u00e9.txt": "file",
  "/Users/bob/loop": "loop",
  "/private/var/folders/x": "file",
  "/private/tmp/t": "file",
  "/var": "private/var",
};
const NOTES_M = "/users/bob/documents/notes.txt";

const LINUX = fakeFs("linux", {
  "/home/bob/notes.txt": "file",
  "/home/bob/.ssh/id_rsa": "file",
  "/home/bob/proj/keys": "/home/bob/.ssh",
  "/home/bob/a": "b",
  "/home/bob/b": "a",
  "/proc/self": "4242",
  "/proc/4242/root": "/",
  "/proc/4242/cwd": "/home/bob",
  "/dev/fd": "/proc/self/fd",
  "/home/bob/long": `/home/bob/${"d/".repeat(2040)}`,
  ...Object.fromEntries(Array.from({ length: 42 }, (_, i) => [`/home/bob/c${i}`, `c${i + 1}`])),
});

describe("hostTools.resolveHostPath", () => {
  it("Windows table", () =>
    runTable("win32", WIN.fs, [
      ["C:\\Users\\Bob\\Documents\\notes.txt", NOTES_W],
      ["c:/users/bob\\DOCUMENTS//Notes.TXT", NOTES_W],
      ["C:\\Users\\Bob\\Documents\\notes.txt. . .", NOTES_W],
      ["\\\\?\\C:\\Users\\Bob\\Documents\\notes.txt", NOTES_W],
      ["C:\\Users\\Bob\\rel\\notes.txt", NOTES_W],
      ["C:\\Users\\Bob\\Documents\\keys\\..\\Documents\\.\\notes.txt", NOTES_W],
      ["C:\\Users\\Bob\\Documents\\keys\\id_rsa", "C:/USERS/BOB/.SSH/ID_RSA"],
      ["C:\\Users\\Bob\\verb\\id_rsa", "C:/USERS/BOB/.SSH/ID_RSA"],
      ["C:\\PROGRA~1\\App\\app.exe", "C:/PROGRAM FILES/APP/APP.EXE"],
      ["C:\\Users\\Bob\\Documents\\new\\x.txt", "C:/USERS/BOB/DOCUMENTS/NEW/X.TXT", 2],
      ["C:\\Users\\GHOST~1\\x", "!short name that does not exist"],
      ["C:\\Users\\ODD~1\\x", "!cannot be expanded to its long name"],
      ["C:\\x\\...", "!only of spaces and dots"],
      ["C:\\x\\ . \\y", "!only of spaces and dots"],
      ["C:\\Users\\Bob\\Documents\\notes.txt:secret", "!alternate data streams"],
      ["C:\\Users\\Bob\\Documents\\notes.txt::$DATA", "!alternate data streams"],
      ["\\\\?\\C:\\Users\\Bob\\Documents\\notes.txt.", "!ending in a dot"],
      ["\\\\.\\C:\\x", "!device paths"],
      ["\\\\?\\UNC\\server\\share", "!not on a drive"],
      ["\\\\server\\share\\x", "!UNC"],
      ["//server/share/x", "!UNC"],
      ["C:\\Users\\CON", "!device names"],
      ["C:\\x\\nul.txt", "!device names"],
      ["C:\\COM1", "!device names"],
      ["C:\\x\\lpt1 .log", "!device names"],
      ["C:foo", "!drive-relative"],
      ["\\Users", "!without a drive letter"],
      ["Documents\\notes.txt", "!needs a base folder"],
      ["C:\\a<b", "!does not allow"],
      ["C:\\Users\\Bob\\loop1", "!link loop"],
      ["C:\\Users\\Bob\\vol\\x", "!not on a drive"],
      ["C:\\Users\\Bob\\Documents\\notes.txt\\x", "!is a file"],
    ]));

  it("macOS table", () =>
    runTable("darwin", fakeFs("darwin", MAC).fs, [
      ["/Users/bob/Documents/notes.txt", NOTES_M],
      ["/USERS/Bob/documents/NOTES.txt", NOTES_M],
      ["/Users/bob/Cafe\u0301.txt", "/users/bob/caf\u00e9.txt"],
      ["/Users/bob/CAF\u00c9.txt", "/users/bob/caf\u00e9.txt"],
      ["/Users/bob/Documents/keys/id_rsa", "/users/bob/.ssh/id_rsa"],
      ["/Users/bob/Documents/keys/../Documents/notes.txt", NOTES_M],
      ["/System/Volumes/Data/Users/bob/Documents/notes.txt", NOTES_M],
      ["/System/Volumes/Data/other", "/system/volumes/data/other", 4],
      ["/var/folders/x", "/private/var/folders/x"],
      ["/tmp/t", "/private/tmp/t"],
      ["/private/tmp/t", "/private/tmp/t"],
      ["/Users/bob/new/a", "/users/bob/new/a", 2],
      ["/Users/bob/loop", "!link loop"],
      ["/Users/bob/a\u0000b", "!NUL"],
      ["Documents/notes.txt", "!needs a base folder"],
      ["/Users/bob/Documents/notes.txt/x", "!is a file"],
    ]));

  it("macOS case-sensitive volume: case matters, Unicode forms still fold", async () => {
    const opts = {
      platform: "darwin" as const,
      fs: fakeFs("darwin", MAC, {}, true).fs,
      caseSensitiveVolume: true,
    };
    expect(await resolveHostPath("/Users/bob/Cafe\u0301.txt", opts)).toMatchObject({
      compareKey: "/Users/bob/Caf\u00e9.txt",
    });
    expect(await resolveHostPath("/USERS/bob/x", opts)).toMatchObject({
      compareKey: "/USERS/bob/x",
      missing: 3,
    });
  });

  it("Linux table", () =>
    runTable("linux", LINUX.fs, [
      ["/home/bob/notes.txt", "/home/bob/notes.txt"],
      ["//home/./bob//notes.txt", "/home/bob/notes.txt"],
      ["/home/bob/NOTES.txt", "/home/bob/NOTES.txt", 1],
      ["/home/bob/proj/keys/id_rsa", "/home/bob/.ssh/id_rsa"],
      ["/home/bob/proj/keys/../notes.txt", "/home/bob/notes.txt"],
      ["/home/bob/x\\y", "/home/bob/x\\y", 1],
      ["/home/bob/new/deep/f", "/home/bob/new/deep/f", 3],
      ["/home/bob/new/../x", "!does not exist"],
      ["/proc/self/root/home/bob/.ssh/id_rsa", "!under /proc"],
      ["/proc/4242/cwd/notes.txt", "!under /proc"],
      ["/dev/fd/3", "!under /proc"],
      ["/home/bob/a", "!link loop"],
      ["/home/bob/c0", "!more than 40 links"],
      [`/home/${"a".repeat(256)}`, "!longer than 255"],
      [`/${"a/".repeat(2100)}`, "!longer than 4096"],
      [`/home/bob/long/${"e/".repeat(20)}`, "!real path is longer than 4096"],
      ["", "!empty"],
      ["   ", "!empty"],
      ["/home/bob/notes.txt/x", "!is a file"],
    ]));

  it("two spellings of one real file give one compare key (seeded property test)", async () => {
    let seed = 7;
    const rnd = () => (seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31;
    const pick = <T>(xs: ReadonlyArray<T>) => xs[Math.floor(rnd() * xs.length)]!;
    const caseMix = (s: string) =>
      [...s].map((c) => (rnd() < 0.5 ? c.toUpperCase() : c.toLowerCase())).join("");
    // [platform, fs, the plain spelling, other spellings through a link or firmlink ("..": not after these)]
    const cases: ReadonlyArray<[HostPlatform, HostPathFs, string[], string[][]]> = [
      [
        "win32",
        WIN.fs,
        ["C:", "Users", "Bob", "Documents", "notes.txt"],
        [["C:", "Users", "Bob", "rel", "notes.txt"]],
      ],
      [
        "darwin",
        fakeFs("darwin", MAC).fs,
        ["", "Users", "bob", "Caf\u00e9.txt"],
        [["", "System", "Volumes", "Data", "Users", "bob", "Caf\u00e9.txt"]],
      ],
      [
        "linux",
        LINUX.fs,
        ["", "home", "bob", ".ssh", "id_rsa"],
        [["", "home", "bob", "proj", "keys", "id_rsa"]],
      ],
    ];
    for (const [platform, fs, plain, others] of cases) {
      const canonical = await resolveHostPath(plain.join("/"), { platform, fs });
      expect(canonical.ok).toBe(true);
      for (let i = 0; i < 150; i++) {
        const names = rnd() < 0.7 ? plain : pick(others);
        const segs = [names[0]!];
        for (let j = 1; j < names.length; j++) {
          let name = platform === "linux" ? names[j]! : caseMix(names[j]!);
          if (platform === "darwin" && rnd() < 0.5) name = name.normalize("NFD");
          segs.push(name);
          const dotDot = names === plain && j + 1 < names.length - 1 ? `${names[j + 1]!}/..` : ".";
          if (j < names.length - 1) segs.push(pick(["", ".", dotDot, "-"]));
        }
        let path = segs.filter((x) => x !== "-").join("/");
        if (platform === "win32")
          path =
            [...path].map((c) => (c === "/" && rnd() < 0.5 ? "\\" : c)).join("") +
            pick(["", ".", " ", ". ."]);
        const got = await resolveHostPath(path, { platform, fs });
        expect({ path, key: got.ok && got.compareKey }).toEqual({
          path,
          key: canonical.ok && canonical.compareKey,
        });
      }
    }
  });

  describe("state question", () => {
    it("resolving twice gives the same answer and reads the file system both times", async () => {
      const opts = { platform: "linux" as const, fs: LINUX.fs };
      const before = LINUX.calls.count;
      const first = await resolveHostPath("/home/bob/proj/keys/id_rsa", opts);
      const reads = LINUX.calls.count - before;
      expect(await resolveHostPath("/home/bob/proj/keys/id_rsa", opts)).toEqual(first);
      expect(LINUX.calls.count - before).toBe(2 * reads);
    });

    it("a link changed between two calls is resolved again, never cached", async () => {
      const f = fakeFs("linux", {
        "/home/bob/notes.txt": "file",
        "/home/bob/.ssh/id_rsa": "file",
        "/home/bob/l": "/home/bob",
      });
      const opts = { platform: "linux" as const, fs: f.fs };
      expect(await resolveHostPath("/home/bob/l/notes.txt", opts)).toMatchObject({
        compareKey: "/home/bob/notes.txt",
      });
      f.set("/home/bob/l", "/home/bob/.ssh");
      expect(await resolveHostPath("/home/bob/l/id_rsa", opts)).toMatchObject({
        compareKey: "/home/bob/.ssh/id_rsa",
      });
      f.set("/home/bob/l", "l");
      expect(await resolveHostPath("/home/bob/l/id_rsa", opts)).toMatchObject({ ok: false });
    });

    it("a relative path uses the base folder only when it is absolute; a refusal leaves nothing behind", async () => {
      const base = { platform: "win32" as const, fs: WIN.fs, base: "C:\\Users\\Bob" };
      expect(await resolveHostPath("Documents\\notes.txt", base)).toMatchObject({
        compareKey: NOTES_W,
      });
      expect(await resolveHostPath("..\\Bob\\Documents\\keys\\id_rsa", base)).toMatchObject({
        compareKey: "C:/USERS/BOB/.SSH/ID_RSA",
      });
      expect(await resolveHostPath("x", { ...base, base: "Users" })).toMatchObject({
        reason: expect.stringContaining("absolute"),
      });
      expect(await resolveHostPath("x", { ...base, base: "" })).toMatchObject({
        reason: expect.stringContaining("empty"),
      });
      expect(await resolveHostPath("C:\\Users\\Bob\\Documents\\notes.txt", base)).toMatchObject({
        compareKey: NOTES_W,
        parts: ["USERS", "BOB", "DOCUMENTS", "NOTES.TXT"],
      });
    });

    it("a file-system error is a refusal with a plain reason, not a throw", async () => {
      const fs = {
        ...LINUX.fs,
        kind: async () => Promise.reject(Object.assign(new Error("x"), { code: "EACCES" })),
      };
      expect(await resolveHostPath("/home/bob", { platform: "linux", fs })).toEqual({
        ok: false,
        reason: "the path could not be read (EACCES)",
      });
    });
  });

  effectIt.effect(
    "real file system adapter: links, a link into a secret folder, loops, missing names",
    () =>
      Effect.gen(function* () {
        const host = yield* HostProcess.Platform;
        if (host === "win32") return; // Windows runs through the fake tables; this needs POSIX symlinks
        yield* Effect.promise(async () => {
          const dir = await NodeFSP.realpath(
            await NodeFSP.mkdtemp(NodePath.join(NodeOS.tmpdir(), "host-path-")),
          );
          try {
            await NodeFSP.mkdir(NodePath.join(dir, "secret"));
            await NodeFSP.writeFile(NodePath.join(dir, "secret", "key"), "x");
            await NodeFSP.symlink(NodePath.join(dir, "secret"), NodePath.join(dir, "docs"));
            await NodeFSP.symlink("loop", NodePath.join(dir, "loop"));
            const opts = { platform: host as HostPlatform, fs: nodeHostPathFs };
            const got = await resolveHostPath(`${dir}/docs/key`, opts);
            expect(got).toMatchObject({ ok: true, real: `${dir}/secret/key`, missing: 0 });
            expect(await resolveHostPath(`${dir}/docs/new/x`, opts)).toMatchObject({
              real: `${dir}/secret/new/x`,
              missing: 2,
            });
            expect(await resolveHostPath(`${dir}/loop`, opts)).toMatchObject({ ok: false });
            expect(await nodeHostPathFs.longName(`${dir}/secret`)).toBe("secret");
          } finally {
            await NodeFSP.rm(dir, { recursive: true, force: true });
          }
        });
      }),
  );
});
