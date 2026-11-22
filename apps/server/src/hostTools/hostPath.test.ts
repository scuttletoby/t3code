// @effect-diagnostics nodeBuiltinImport:off - the adapter tests make real links, sockets and drives in a temporary folder.
import * as NodeChildProcess from "node:child_process";
import * as NodeFSP from "node:fs/promises";
import * as NodeNet from "node:net";
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
  secretFold,
} from "./hostPath.ts";

// A fake file system with its own name rules, never the code's folds (so a wrong fold fails): `ci` folds ASCII A-Z only;
// `aliases` maps a typed name to the stored one; `hidden` are aliases lstat does not show (firmlinks, mount points,
// subst); `osNames` is what realPath gives: stored names (Windows, macOS) or the typed path (Linux). Tree values:
// "dir", "file", "other" (device, pipe, socket) or a link's target text.
interface FakeOptions {
  readonly ci?: boolean;
  readonly aliases?: Record<string, string>;
  readonly hidden?: Record<string, string>;
  readonly osNames?: "stored" | "typed";
}
function fakeFs(platform: HostPlatform, tree: Record<string, string>, o: FakeOptions = {}) {
  const split = (p: string) =>
    platform === "win32" ? p.split(/[\\/]/).filter(Boolean) : ["", ...p.split("/").filter(Boolean)];
  const join = (names: ReadonlyArray<string>) =>
    platform === "win32"
      ? `${names[0]}\\${names.slice(1).join("\\")}`
      : `/${names.slice(1).join("/")}`;
  const ascii = (s: string) =>
    o.ci ? s.replace(/[A-Z]/g, (c) => String.fromCharCode(c.charCodeAt(0) + 32)) : s;
  const entries = new Map<string, string>();
  const hidden = new Map(
    Object.entries(o.hidden ?? {}).map(([k, v]) => [split(k).join("/"), split(v)]),
  );
  const set = (path: string, value: string) => {
    const names = split(path);
    for (let i = 1; i < names.length; i++)
      if (!entries.has(names.slice(0, i).join("/")))
        entries.set(names.slice(0, i).join("/"), "dir");
    entries.set(names.join("/"), value);
  };
  for (const [k, v] of Object.entries(tree)) set(k, v);
  for (const k of Object.keys(o.hidden ?? {})) set(k, "dir");
  const children = (parent: ReadonlyArray<string>) => {
    const prefix = `${parent.join("/")}/`;
    return [...entries.keys()]
      .filter((k) => k.startsWith(prefix) && !k.slice(prefix.length).includes("/"))
      .map((k) => k.slice(prefix.length));
  };
  // The stored names of a typed path, or undefined when missing. The model never walks through a link.
  const lookup = (typed: string) => {
    const names = split(typed);
    let cur = [platform === "win32" ? names[0]!.toUpperCase() : names[0]!];
    cur = hidden.get(cur.join("/")) ?? cur;
    for (const name of names.slice(1)) {
      const value = entries.get(cur.join("/"));
      if (value !== undefined && value !== "dir")
        throw new Error(`fake: walked through ${cur.join("/")}`);
      const kids = children(cur);
      const alias = Object.entries(o.aliases ?? {}).find(([a]) => ascii(a) === ascii(name))?.[1];
      const stored =
        kids.find((k) => k === name) ??
        kids.find((k) => ascii(k) === ascii(name)) ??
        kids.find((k) => k === alias);
      if (stored === undefined) return undefined;
      cur = [...cur, stored];
      cur = hidden.get(cur.join("/")) ?? cur;
    }
    return cur;
  };
  const calls = { count: 0 };
  const fs: HostPathFs = {
    // A walk that never ends (a missing loop guard) fails here instead of spinning: the fake never yields.
    kind: async (p) => {
      if (++calls.count > 1e6) throw new Error("runaway walk");
      const stored = lookup(p);
      if (stored === undefined) return "missing";
      const v = entries.get(stored.join("/")) ?? "dir";
      return v === "dir" || v === "file" || v === "other" ? v : "link";
    },
    readLink: async (p) => entries.get(lookup(p)!.join("/"))!,
    longName: async (p) => lookup(p)?.at(-1),
    realPath: async (p) => {
      const stored = lookup(p);
      if (stored === undefined) throw Object.assign(new Error("gone"), { code: "ENOENT" });
      return o.osNames === "typed" ? p : join(stored);
    },
  };
  return { fs, calls, set };
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
    "C:\\Users\\Bob\\vol": "\\\\?\\Volume{0d1f}\\", // a mount point the OS reports as a link
    "C:\\Users\\Bob\\loop1": "C:\\Users\\Bob\\loop2",
    "C:\\Users\\Bob\\loop2": "loop1",
    "C:\\Users\\Bob\\vault.kdbx": "file",
    "C:\\Users\\Bob\\\u1FBC\\f": "file",
    "C:\\Program Files\\App\\app.exe": "file",
    "Z:\\x": "file",
  },
  {
    ci: true,
    aliases: {
      "PROGRA~1": "Program Files",
      KEYFILE: "id_rsa", // a short name set by hand, without "~"
      "vault.\u212Adbx": "vault.kdbx", // Kelvin sign: NTFS upper-cases k to K, toUpperCase keeps K
      "\u1FB3": "\u1FBC", // ᾳ and ᾼ: one name under NTFS's simple upper case
    },
    hidden: {
      "C:\\Users\\Bob\\Documents\\cvol": "C:\\", // the C: volume mounted again; lstat says "dir"
      "S:\\": "C:\\Users\\Bob\\.ssh", // subst S: C:\Users\Bob\.ssh
    },
    osNames: "stored",
  },
);
// A mapped drive: the OS answer is a network path.
const WIN_MAPPED: HostPathFs = {
  ...WIN.fs,
  realPath: async (p) => p.replace(/^Z:\\/i, "\\\\server\\share\\"),
};
const NOTES_W = "C:/USERS/BOB/DOCUMENTS/NOTES.TXT";

const MAC_TREE = {
  "/Users/bob/Documents/notes.txt": "file",
  "/Users/bob/.ssh/id_rsa": "file",
  "/Users/bob/Documents/keys": "../.ssh",
  "/Users/bob/Caf\u00e9.txt": "file",
  "/Users/bob/loop": "loop",
  "/Users/bob/sv": "/System/Volumes", // a link to /System/Volumes
  "/Users/bob/sv2": "../../System/Volumes/Data", // a relative link to the data volume
  "/Users/bob/devlink": "/dev",
  "/System/Volumes/Data/.fseventsd": "dir",
  "/System/Volumes/Preboot": "dir",
  "/System/Library/Caches/c": "file",
  "/System/Library/AssetsV2/a": "file",
  "/private/var/folders/x": "file",
  "/private/tmp/t": "file",
  "/var": "private/var",
};
// The firmlinks, as the OS sees them: not links, the same folders.
const MAC_HIDDEN = Object.fromEntries(
  ["Users", "private", "System/Library/Caches", "System/Library/AssetsV2"].map((h) => [
    `/System/Volumes/Data/${h}`,
    `/${h}`,
  ]),
);
const MAC_ALIASES = {
  "Cafe\u0301.txt": "Caf\u00e9.txt",
  "caf\u00c9.txt": "Caf\u00e9.txt",
  "cafe\u0301.txt": "Caf\u00e9.txt",
};
const MAC = fakeFs("darwin", MAC_TREE, {
  ci: true,
  aliases: MAC_ALIASES,
  hidden: MAC_HIDDEN,
  osNames: "stored",
});
const NOTES_M = "/users/bob/documents/notes.txt";
const SSH_M = "/users/bob/.ssh/id_rsa";

const LINUX = fakeFs(
  "linux",
  {
    "/home/bob/notes.txt": "file",
    "/home/bob/.ssh/id_rsa": "file",
    "/home/bob/proj/keys": "/home/bob/.ssh",
    "/home/bob/a": "b",
    "/home/bob/b": "a",
    "/home/bob/env": "/proc/self/environ",
    "/home/bob/fifo": "other",
    "/proc/self": "4242",
    "/proc/4242/environ": "file",
    "/dev/fd": "/proc/self/fd",
    "/home/bob/long": `/home/bob/${"d/".repeat(2040)}`,
    ...Object.fromEntries(Array.from({ length: 42 }, (_, i) => [`/home/bob/c${i}`, `c${i + 1}`])),
  },
  { osNames: "typed" },
);

describe("hostTools.resolveHostPath", () => {
  it("Windows table", () =>
    runTable("win32", WIN.fs, [
      ["C:\\Users\\Bob\\Documents\\notes.txt", NOTES_W],
      ["c:/users/bob\\DOCUMENTS//Notes.TXT", NOTES_W],
      ["C:\\Users\\Bob\\Documents\\notes.txt. . .", NOTES_W],
      ["\\\\?\\C:\\Users\\Bob\\Documents\\notes.txt", NOTES_W],
      ["C:\\Users\\Bob\\rel\\notes.txt", NOTES_W],
      // Win32 collapses ".." in the typed path before the junction is seen.
      ["C:\\Users\\Bob\\Documents\\keys\\..\\notes.txt", NOTES_W],
      ["C:\\Users\\Bob\\Documents\\keys\\id_rsa", "C:/USERS/BOB/.SSH/ID_RSA"],
      ["C:\\Users\\Bob\\verb\\id_rsa", "C:/USERS/BOB/.SSH/ID_RSA"],
      ["C:\\PROGRA~1\\App\\app.exe", "C:/PROGRAM FILES/APP/APP.EXE"],
      ["C:\\Users\\Bob\\Documents\\new\\x.txt", "C:/USERS/BOB/DOCUMENTS/NEW/X.TXT", 2],
      ["C:\\Users\\GHOST~1\\x", "!short name that does not exist"],
      ["C:\\x\\...", "!only of spaces and dots"],
      ["C:\\x\\ . \\y", "!only of spaces and dots"],
      ["C:\\Users\\Bob\\Documents\\notes.txt:secret", "!alternate data streams"],
      ["C:\\Users\\Bob\\Documents\\notes.txt::$DATA", "!alternate data streams"],
      ["\\\\?\\C:\\Users\\Bob\\Documents\\notes.txt.", "!ending in a dot"],
      ["\\\\?\\C:\\Users\\x\\..\\Bob", '!with ".."'],
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
      // The OS answer differs from the model: refused.
      ["C:\\Users\\Bob\\Documents\\cvol\\Users\\Bob\\.ssh\\id_rsa", "!another place"],
      ["S:\\id_rsa", "!another place"],
      ["C:\\Users\\Bob\\.ssh\\KEYFILE", "!another place"],
      ["C:\\Users\\Bob\\vault.\u212Adbx", "!another place"],
      ["C:\\Users\\Bob\\\u1FB3\\f", "!another place"],
    ]));

  it("Windows: a mapped drive is a network path; a short name the system cannot expand is refused", async () => {
    await runTable("win32", WIN_MAPPED, [["Z:\\x", "!network (UNC)"]]);
    const fs = { ...WIN.fs, longName: async () => undefined };
    await runTable("win32", fs, [["C:\\PROGRA~1\\App", "!cannot be expanded to its long name"]]);
  });

  it("macOS table", () =>
    runTable("darwin", MAC.fs, [
      ["/Users/bob/Documents/notes.txt", NOTES_M],
      ["/USERS/Bob/documents/NOTES.txt", NOTES_M],
      ["/Users/bob/Cafe\u0301.txt", "/users/bob/caf\u00e9.txt"],
      ["/Users/bob/CAF\u00c9.txt", "/users/bob/caf\u00e9.txt"],
      ["/Users/bob/Documents/keys/id_rsa", SSH_M],
      ["/Users/bob/Documents/keys/../Documents/notes.txt", NOTES_M],
      // Firmlinks are mapped on the resolved path, after ".." and links (lenses A-B1, B-B1).
      ["/System/Volumes/Data/Users/bob/Documents/notes.txt", NOTES_M],
      ["/System/Volumes/Preboot/../Data/Users/bob/.ssh/id_rsa", SSH_M],
      ["/System/Volumes/Data/../Data/Users/bob/.ssh/id_rsa", SSH_M],
      ["/Users/bob/sv/Data/Users/bob/.ssh/id_rsa", SSH_M],
      ["/Users/bob/sv2/Users/bob/.ssh/id_rsa", SSH_M],
      ["/System/Volumes/Data/private/var/folders/x", "/private/var/folders/x"],
      ["/System/Volumes/Data/System/Library/Caches/c", "/system/library/caches/c"],
      ["/System/Volumes/Data/System/Library/AssetsV2/a", "/system/library/assetsv2/a"],
      ["/System/Volumes/Data/.fseventsd", "/system/volumes/data/.fseventsd"],
      ["/System/Volumes/Data/other", "/system/volumes/data/other", 1],
      ["/var/folders/x", "/private/var/folders/x"],
      ["/tmp/t", "/private/tmp/t"],
      ["/private/tmp/t", "/private/tmp/t"],
      ["/Users/bob/new/a", "/users/bob/new/a", 2],
      ["/Users/bob/loop", "!link loop"],
      ["/Users/bob/a\u0000b", "!NUL"],
      ["Documents/notes.txt", "!needs a base folder"],
      ["/Users/bob/Documents/notes.txt/x", "!is a file"],
      // volfs and devices (lens A-B2, B-M2).
      ["/.vol/16777220/2/Users/bob/.ssh/id_rsa", "!under /.vol"],
      ["/.VOL/16777220/123456", "!under /.vol"],
      ["/dev/fd/3", "!under /dev"],
      ["/dev/stdin", "!under /dev"],
      ["/Users/bob/devlink/zero", "!under /dev"],
    ]));

  it("macOS case-sensitive volume: case matters for compareKey, Unicode forms still fold", async () => {
    const opts = {
      platform: "darwin" as const,
      fs: fakeFs("darwin", MAC_TREE, { aliases: MAC_ALIASES, osNames: "stored" }).fs,
      caseSensitiveVolume: true,
    };
    expect(await resolveHostPath("/Users/bob/Cafe\u0301.txt", opts)).toMatchObject({
      compareKey: "/Users/bob/Caf\u00e9.txt",
      secretKey: "/users/bob/caf\u00e9.txt",
    });
    expect(await resolveHostPath("/USERS/bob/x", opts)).toMatchObject({
      compareKey: "/USERS/bob/x",
      secretKey: "/users/bob/x",
      missing: 3,
    });
    // A wrong "case-sensitive" from the caller on a case-insensitive volume: the system's answer differs, refused.
    expect(await resolveHostPath("/USERS/bob/.SSH/ID_RSA", { ...opts, fs: MAC.fs })).toMatchObject({
      ok: false,
      reason: expect.stringContaining("another place"),
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
      // /proc, /sys, /dev and the portal (lens B-M2, B-M3, A minor).
      ["/proc/self/root/home/bob/.ssh/id_rsa", "!under /proc"],
      ["/proc/4242/environ", "!under /proc"],
      ["/proc/4242/mem", "!under /proc"],
      ["/home/bob/env", "!under /proc"],
      ["/sys/kernel/x", "!under /sys"],
      ["/dev/zero", "!under /dev"],
      ["/dev/fd/3", "!under /dev"],
      ["/run/user/1000/doc/ab12/f", "!document portal"],
      ["/home/bob/fifo", "!device, pipe or socket"],
      ["/home/bob/a", "!link loop"],
      ["/home/bob/c0", "!more than 40 links"],
      [`/home/${"a".repeat(256)}`, "!longer than 255"],
      [`/${"a/".repeat(2100)}`, "!longer than 4096"],
      [`/home/bob/long/${"e/".repeat(20)}`, "!real path is longer than 4096"],
      ["", "!empty"],
      ["   ", "!empty"],
      ["/home/bob/notes.txt/x", "!is a file"],
    ]));

  it("a chain of links with many names each stops at the step cap", async () => {
    const deep = `/home/bob/${"d/".repeat(100)}`;
    const tree: Record<string, string> = { [`${deep}f`]: "file" };
    for (let i = 0; i < 39; i++) tree[`/home/bob/s${i}`] = `${deep}${"../".repeat(100)}s${i + 1}`;
    const fs = fakeFs("linux", tree, { osNames: "typed" }).fs;
    await runTable("linux", fs, [["/home/bob/s0", "!more than 4096 names"]]);
  });

  it("names a volume folds unlike foldName: the OS check refuses them; secretKey folds them anyway", async () => {
    // [platform, typed name, stored name]: one file under the volume's own folding (lens A-B3, A-M2, B-M1).
    const pairs: ReadonlyArray<[HostPlatform, string, string]> = [
      ["darwin", ".\u017F\u017Fh", ".ssh"], // ſ
      ["darwin", ".\u00DFh", ".ssh"], // ß
      ["darwin", ".\u1E9Eh", ".ssh"], // ẞ
      ["darwin", "\u03C3\u03C2", "\u03C3\u03C3"], // ς
      ["darwin", "\u00B5m", "\u03BCm"], // µ
      ["darwin", "\u0130d", "id"], // İ
      ["darwin", "\u0131d", "id"], // ı
      ["darwin", "\uFF53\uFF53h", "ssh"], // full-width
      ["win32", "vault.\u212Adbx", "vault.kdbx"], // Kelvin sign
      ["win32", "\u1FB3x", "\u1FBCx"], // ᾳ ᾼ
      ["linux", ".SSH", ".ssh"], // an ext4 casefold folder, vfat, exFAT, ntfs3, SMB, WSL /mnt/c
    ];
    for (const [platform, typed, stored] of pairs) {
      const root = platform === "win32" ? "C:\\h\\" : "/h/";
      const tree = { [`${root}${stored}`]: "file" };
      const aliases = { [typed]: stored };
      const strict = fakeFs(platform, tree, { aliases, osNames: "stored" }).fs;
      const got = await resolveHostPath(`${root}${typed}`, { platform, fs: strict });
      if (platform !== "linux")
        expect({ typed, got }).toMatchObject({
          typed,
          got: { ok: false, reason: expect.stringContaining("another place") },
        });
      // An OS that keeps the typed name (Linux realpath): accepted, and secretKey is the stored name's.
      const loose = fakeFs(platform, tree, { aliases, osNames: "typed" }).fs;
      const typedGot = await resolveHostPath(`${root}${typed}`, { platform, fs: loose });
      const plain = await resolveHostPath(`${root}${stored}`, { platform, fs: loose });
      expect({ typed, key: typedGot.ok && typedGot.secretKey }).toEqual({
        typed,
        key: plain.ok && plain.secretKey,
      });
    }
    const linux = fakeFs(
      "linux",
      { "/mnt/c/Users/me/.ssh/id_rsa": "file" },
      { ci: true, osNames: "typed" },
    );
    expect(
      await resolveHostPath("/mnt/c/Users/me/.SSH/ID_RSA", { platform: "linux", fs: linux.fs }),
    ).toMatchObject({
      compareKey: "/mnt/c/Users/me/.SSH/ID_RSA",
      secretKey: "/mnt/c/users/me/.ssh/id_rsa",
    });
  });

  it("two spellings of one real file give one key (seeded property test)", async () => {
    let seed = 7;
    const rnd = () => (seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31;
    const pick = <T>(xs: ReadonlyArray<T>) => xs[Math.floor(rnd() * xs.length)]!;
    const caseMix = (s: string) =>
      [...s].map((c) => (rnd() < 0.5 ? c.toUpperCase() : c.toLowerCase())).join("");
    const casefold = fakeFs(
      "linux",
      { "/home/bob/.ssh/id_rsa": "file" },
      { ci: true, osNames: "typed" },
    );
    // [platform, fs, key compared, mix case, the plain spelling, other spellings through links, firmlinks, ".."]
    const cases: ReadonlyArray<
      [HostPlatform, HostPathFs, "compareKey" | "secretKey", boolean, string[], string[][]]
    > = [
      [
        "win32",
        WIN.fs,
        "compareKey",
        true,
        ["C:", "Users", "Bob", "Documents", "notes.txt"],
        [["C:", "Users", "Bob", "rel", "notes.txt"]],
      ],
      [
        "darwin",
        MAC.fs,
        "compareKey",
        true,
        ["", "Users", "bob", "Caf\u00e9.txt"],
        [
          ["", "System", "Volumes", "Data", "Users", "bob", "Caf\u00e9.txt"],
          ["", "System", "Volumes", "Preboot", "..", "Data", "Users", "bob", "Caf\u00e9.txt"],
          ["", "System", "Volumes", "Data", "..", "Data", "Users", "bob", "Caf\u00e9.txt"],
          ["", "Users", "bob", "sv", "Data", "Users", "bob", "Caf\u00e9.txt"],
          ["", "Users", "bob", "sv2", "Users", "bob", "Caf\u00e9.txt"],
        ],
      ],
      [
        "linux",
        LINUX.fs,
        "compareKey",
        false,
        ["", "home", "bob", ".ssh", "id_rsa"],
        [["", "home", "bob", "proj", "keys", "id_rsa"]],
      ],
      ["linux", casefold.fs, "secretKey", true, ["", "home", "bob", ".ssh", "id_rsa"], []],
    ];
    for (const [platform, fs, keyName, mix, plain, others] of cases) {
      const canonical = await resolveHostPath(plain.join("/"), { platform, fs });
      expect(canonical.ok).toBe(true);
      for (let i = 0; i < 150; i++) {
        const names = rnd() < 0.5 || others.length === 0 ? plain : pick(others);
        const segs = [names[0]!];
        for (let j = 1; j < names.length; j++) {
          let name = mix && names[j] !== ".." ? caseMix(names[j]!) : names[j]!;
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
        expect({ path, key: got.ok && got[keyName] }).toEqual({
          path,
          key: canonical.ok && canonical[keyName],
        });
      }
    }
  });

  describe("secretFold", () => {
    it("folds every name pair some file system treats as one", () => {
      const same: ReadonlyArray<ReadonlyArray<string>> = [
        ["ss", "SS", "\u00DF", "\u1E9E", "\u017F\u017F", "s\u017F"],
        ["\u03C3", "\u03C2", "\u03A3"],
        ["k", "K", "\u212A"],
        ["\u03BC", "\u00B5", "\u039C"],
        ["i", "I", "\u0130", "\u0131", "i\u0307"],
        ["\u03B1\u03B9", "\u1FB3", "\u1FBC", "\u03B1\u0345", "\u0391\u0399"],
        ["ssh", "\uFF53\uFF53\uFF48", "\uFF33\uFF33\uFF28", "s\u00ADsh"],
        ["caf\u00E9", "CAFE\u0301", "Caf\u00C9"],
        ["\u00E5", "\u212B", "\u00C5", "a\u030A"],
      ];
      for (const group of same)
        expect(group.map(secretFold)).toEqual(group.map(() => secretFold(group[0]!)));
      for (const group of same)
        for (const s of group) expect(secretFold(secretFold(s))).toBe(secretFold(s));
    });

    it("agrees with the regex engine's Unicode case folding for every cased code point", () => {
      // An independent oracle: /x/iu matches by Unicode simple case folding (CaseFolding.txt C+S).
      let checked = 0;
      for (let cp = 0; cp <= 0x10ffff; cp++) {
        if (cp >= 0xd800 && cp <= 0xdfff) continue;
        const c = String.fromCodePoint(cp);
        const candidates = new Set([
          c.toLowerCase(),
          c.toUpperCase(),
          c.toUpperCase().toLowerCase(),
          c.toLowerCase().toUpperCase(),
        ]);
        candidates.delete(c);
        for (const d of candidates) {
          if ([...d].length !== 1) continue;
          if (!new RegExp(`^\\u{${cp.toString(16)}}$`, "iu").test(d)) continue;
          checked += 1;
          expect({ c, fold: secretFold(c) }).toEqual({ c, fold: secretFold(d) });
        }
      }
      expect(checked).toBeGreaterThan(2000);
    });

    it("foldName stays exact per OS: Linux never folds, Windows upper case, macOS lower case", () => {
      expect(foldName(".SSH", "linux")).toBe(".SSH");
      expect(foldName("c:", "win32")).toBe("C:");
      expect(foldName("\u00DF", "win32")).toBe("\u00DF");
      expect(foldName("Cafe\u0301", "darwin")).toBe("caf\u00E9");
      expect(foldName("Cafe\u0301", "darwin", true)).toBe("Caf\u00E9");
    });
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
      const f = fakeFs(
        "linux",
        {
          "/home/bob/notes.txt": "file",
          "/home/bob/.ssh/id_rsa": "file",
          "/home/bob/l": "/home/bob",
        },
        { osNames: "typed" },
      );
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

    it("the system's answer is read on every call; a change between calls refuses", async () => {
      let alias = false;
      const fs = {
        ...LINUX.fs,
        realPath: async (p: string) => (alias ? p.replace("/home/bob", "/srv/other") : p),
      };
      const opts = { platform: "linux" as const, fs };
      expect(await resolveHostPath("/home/bob/notes.txt", opts)).toMatchObject({ ok: true });
      alias = true;
      expect(await resolveHostPath("/home/bob/notes.txt", opts)).toMatchObject({
        ok: false,
        reason: expect.stringContaining("another place"),
      });
    });

    it("an old, empty or odd system answer is a refusal", async () => {
      for (const [answer, reason] of [
        ["", "empty"],
        ["home/bob", "relative"],
        ["/home/x/../bob", '".."'],
        ["/proc/1/root/home/bob", "under /proc"],
      ] as const) {
        const fs = { ...LINUX.fs, realPath: async () => answer };
        expect(
          await resolveHostPath("/home/bob/notes.txt", { platform: "linux", fs }),
        ).toMatchObject({
          ok: false,
          reason: expect.stringContaining(reason),
        });
      }
    });

    it("a relative path uses the base folder only when it is absolute; a refusal leaves nothing behind", async () => {
      const base = { platform: "win32" as const, fs: WIN.fs, base: "C:\\Users\\Bob" };
      expect(await resolveHostPath("Documents\\notes.txt", base)).toMatchObject({
        compareKey: NOTES_W,
        secretKey: "c:/users/bob/documents/notes.txt",
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
      expect(await resolveHostPath("~\\.ssh\\id_rsa", base)).toMatchObject({
        compareKey: "C:/USERS/BOB/~/.SSH/ID_RSA",
        missing: 3,
      });
      expect(await resolveHostPath("C:\\Users\\Bob\\Documents\\notes.txt", base)).toMatchObject({
        compareKey: NOTES_W,
        parts: ["USERS", "BOB", "DOCUMENTS", "NOTES.TXT"],
      });
    });

    it("a file-system error, also half-way through a chain, is a refusal with a plain reason", async () => {
      const fs = {
        ...LINUX.fs,
        kind: async () => Promise.reject(Object.assign(new Error("x"), { code: "EACCES" })),
      };
      expect(await resolveHostPath("/home/bob", { platform: "linux", fs })).toEqual({
        ok: false,
        reason: "the path could not be read (EACCES)",
      });
      const gone = {
        ...LINUX.fs,
        realPath: async () => Promise.reject(Object.assign(new Error("x"), { code: "ENOENT" })),
      };
      expect(
        await resolveHostPath("/home/bob/proj/keys/id_rsa", { platform: "linux", fs: gone }),
      ).toEqual({
        ok: false,
        reason: "the path could not be read (ENOENT)",
      });
    });
  });

  const withTemp = async (body: (dir: string) => Promise<void>) => {
    const dir = await NodeFSP.realpath(
      await NodeFSP.mkdtemp(NodePath.join(NodeOS.tmpdir(), "host-path-")),
    );
    try {
      await body(dir);
    } finally {
      await NodeFSP.rm(dir, { recursive: true, force: true });
    }
  };

  effectIt.effect(
    "real file system adapter: links, a link into a secret folder, loops, missing names, sockets, OS check",
    () =>
      Effect.gen(function* () {
        const host = yield* HostProcess.Platform;
        if (host === "win32") return; // Windows has its own adapter test below; this needs POSIX symlinks
        yield* Effect.promise(() =>
          withTemp(async (dir) => {
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
            // A socket is "other" and refused.
            const sock = NodePath.join(dir, "s.sock");
            const server = NodeNet.createServer();
            await new Promise<void>((done) => server.listen(sock, done));
            try {
              expect(await nodeHostPathFs.kind(sock)).toBe("other");
              expect(await resolveHostPath(sock, opts)).toMatchObject({
                reason: expect.stringContaining("device, pipe or socket"),
              });
            } finally {
              await new Promise((done) => server.close(done));
            }
            expect(await nodeHostPathFs.kind("/dev/null")).toBe("other");
            expect(await resolveHostPath("/dev/zero", opts)).toMatchObject({ ok: false });
            // A link the adapter cannot see (as libuv reports a Windows volume junction): the OS answer refuses it.
            const blind: HostPathFs = {
              ...nodeHostPathFs,
              kind: async (p) => (p === `${dir}/docs` ? "dir" : nodeHostPathFs.kind(p)),
            };
            expect(await resolveHostPath(`${dir}/docs/key`, { ...opts, fs: blind })).toMatchObject({
              ok: false,
              reason: expect.stringContaining("another place"),
            });
          }),
        );
      }),
  );

  // The parts only a real Windows or macOS runner can show; skipped on other platforms.
  it.skipIf(HostProcess.Platform.defaultValue() !== "win32")(
    "Windows adapter: Kelvin sign on NTFS, a volume junction and a subst drive are refused or folded",
    () =>
      withTemp(async (dir) => {
        const opts = { platform: "win32" as const, fs: nodeHostPathFs };
        await NodeFSP.mkdir(NodePath.join(dir, ".ssh"));
        await NodeFSP.writeFile(NodePath.join(dir, ".ssh", "vault.kdbx"), "x");
        const kelvin = await resolveHostPath(`${dir}\\.ssh\\vault.\u212Adbx`, opts);
        expect(kelvin.ok ? kelvin.secretKey.endsWith("/.ssh/vault.kdbx") : true).toBe(true);
        const volume = NodeChildProcess.execFileSync("mountvol", [dir.slice(0, 2), "/L"], {
          encoding: "utf8",
        }).trim();
        const junction = NodePath.join(dir, "vol");
        NodeChildProcess.execFileSync("cmd", ["/c", "mklink", "/J", junction, volume]);
        const through = `${junction}\\${dir.slice(3)}\\.ssh\\vault.kdbx`;
        expect(await resolveHostPath(through, opts)).toMatchObject({ ok: false });
        const letter = "QRSTUVWXY"
          .split("")
          .find(
            (l) => !NodeChildProcess.spawnSync("cmd", ["/c", `if exist ${l}:\\ exit 1`]).status,
          );
        if (letter === undefined) return;
        NodeChildProcess.execFileSync("subst", [`${letter}:`, NodePath.join(dir, ".ssh")]);
        try {
          expect(await resolveHostPath(`${letter}:\\vault.kdbx`, opts)).toMatchObject({
            ok: false,
          });
        } finally {
          NodeChildProcess.execFileSync("subst", [`${letter}:`, "/D"]);
        }
      }),
  );

  it.skipIf(HostProcess.Platform.defaultValue() !== "darwin")(
    "macOS adapter: APFS folding of ſ, ς and µ, firmlinks after .. and /.vol",
    () =>
      withTemp(async (dir) => {
        const opts = { platform: "darwin" as const, fs: nodeHostPathFs };
        await NodeFSP.mkdir(NodePath.join(dir, ".ssh"));
        await NodeFSP.writeFile(NodePath.join(dir, ".ssh", "\u03C3\u03C3\u03BC"), "x");
        for (const typed of [".\u017F\u017Fh/\u03C3\u03C2\u00B5", ".\u00DFh/\u03C3\u03C3\u03BC"]) {
          const got = await resolveHostPath(`${dir}/${typed}`, opts);
          expect({
            typed,
            ok: got.ok ? got.secretKey.endsWith("/.ssh/\u03C3\u03C3\u03BC") : true,
          }).toEqual({
            typed,
            ok: true,
          });
        }
        const home = NodeOS.homedir();
        const plain = await resolveHostPath(home, opts);
        for (const other of [
          `/System/Volumes/Data${home}`,
          `/System/Volumes/Preboot/../Data${home}`,
        ]) {
          expect(await resolveHostPath(other, opts)).toMatchObject({
            compareKey: plain.ok ? plain.compareKey : "plain failed",
          });
        }
        expect(await resolveHostPath("/.vol/1/2", opts)).toMatchObject({ ok: false });
        expect(await resolveHostPath("/dev/fd/0", opts)).toMatchObject({ ok: false });
      }),
  );
});
