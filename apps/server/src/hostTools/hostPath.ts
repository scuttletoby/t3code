// @effect-diagnostics nodeBuiltinImport:off - the thin real file-system adapter at the end reads links with node:fs.
// Fork only (host tools, design docs/fork/host-tools.md): the per-OS path layer. Pure and OS-neutral: the platform and
// every file-system call are injected, so the Windows, macOS and Linux cases all run on any CI machine.
//
// Contract (what the policy core's `decide` builds on):
// - `real` is the path after every link, junction and mount point was followed to its final target, `.` and `..`
//   applied to that real location (never to the typed string), Windows short names expanded and Windows trailing dots
//   and spaces removed. A caller opens `real`, never its own input, so what was checked is what is opened.
// - `compareKey` is the one string `decide` matches reach kinds and secret patterns against: `real` with `/` as the
//   only separator, NFC on macOS (where both Unicode forms open one file), and case folded with `foldName` where the
//   volume ignores case (Windows always; macOS unless `caseSensitiveVolume`; never on Linux). Patterns must be folded
//   with the same `foldName`. Two spellings of one existing file give one key.
// - `parts` are the key's names after the root (`c:/` or `/`); the last `missing` of them do not exist yet (a name
//   for stat or search). Their parent exists and is real; a missing name is kept as typed, never guessed.
// - Every call reads the file system again; nothing is cached, so a link changed between two calls is followed anew.
// - Not handled here (by the reader, §3.4): a link swapped after this call, and hard links (link count above 1).
import * as NodeFSP from "node:fs/promises";

import * as Schema from "effect/Schema";

export type HostPlatform = "win32" | "darwin" | "linux";
export type EntryKind = "file" | "dir" | "link" | "missing";

export interface HostPathFs {
  /** The entry itself, links not followed (symlinks, junctions and mount points are all "link"). */
  readonly kind: (path: string) => Promise<EntryKind>;
  /** The target text of a link, as stored (absolute or relative to the link's folder). */
  readonly readLink: (path: string) => Promise<string>;
  /** Windows: the long name of an existing entry whose name may be a short (8.3) name; undefined when unknown. */
  readonly longName: (path: string) => Promise<string | undefined>;
}

export interface ResolveOptions {
  readonly platform: HostPlatform;
  readonly fs: HostPathFs;
  /** An absolute folder a relative input is taken from; without one a relative input is refused. */
  readonly base?: string;
  /** macOS only: the volume was formatted case-sensitive (APFS default is case-insensitive). */
  readonly caseSensitiveVolume?: boolean;
}

export type ResolvedPath =
  | {
      readonly ok: true;
      readonly real: string;
      readonly compareKey: string;
      readonly parts: ReadonlyArray<string>;
      readonly missing: number;
    }
  | { readonly ok: false; readonly reason: string };

const MAX_LINKS = 40;
const MAX_NAME = 255;
const maxLength = (platform: HostPlatform) => (platform === "win32" ? 32_767 : 4096);
const WIN_DEVICES = /^(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])$/i;
// macOS firmlinks: the data volume's copy of these folders is the same folder as the root one.
const MAC_FIRMLINKS = [
  "Applications",
  "Library",
  "Users",
  "Volumes",
  "cores",
  "opt",
  "private",
  "usr/local",
];

class Refused extends Schema.TaggedError<Refused>()("HostPathRefused", { reason: Schema.String }) {}
const isRefused = Schema.is(Refused);
const refuse = (reason: string): never => {
  throw new Refused({ reason });
};

/**
 * Folds one name for comparison: Windows upper case (as NTFS compares), macOS NFC then lower case (APFS), Linux as
 * is. A code point whose case form has another length (German ß, Turkish İ) is kept as typed.
 */
export function foldName(
  name: string,
  platform: HostPlatform,
  caseSensitiveVolume = false,
): string {
  const formed = platform === "darwin" ? name.normalize("NFC") : name;
  if (platform === "linux" || (platform === "darwin" && caseSensitiveVolume)) return formed;
  let out = "";
  for (const ch of formed) {
    const cased = platform === "win32" ? ch.toUpperCase() : ch.toLowerCase();
    out += [...cased].length === 1 ? cased : ch;
  }
  return out;
}

interface Parsed {
  readonly root: string; // "C:\\" or "/"
  readonly names: ReadonlyArray<string>;
}

function parseWindows(input: string): Parsed {
  let path = input;
  const literal = /^(\\\\\?\\|\\\?\?\\)/.test(path);
  if (literal) {
    path = path.slice(4);
    if (!/^[A-Za-z]:\\/.test(path))
      refuse("a \\\\?\\ path that is not on a drive (UNC, volume or device) is refused");
  }
  path = path.replaceAll("/", "\\");
  if (path.startsWith("\\\\")) {
    refuse(
      /^\\\\[.?]\\/.test(path)
        ? "device paths (\\\\.\\) are refused"
        : "network (UNC) paths are refused",
    );
  }
  if (/^[A-Za-z]:(?!\\)/.test(path))
    refuse("a drive-relative path (like C:foo) is refused; give C:\\foo");
  if (!/^[A-Za-z]:\\/.test(path)) refuse("a path without a drive letter is refused");
  const names: string[] = [];
  for (const raw of path.slice(3).split("\\")) {
    if (raw === "" || raw === "." || raw === "..") {
      if (raw === "..") names.push(raw);
      continue;
    }
    if (raw.includes(":")) refuse(`alternate data streams are refused (${raw})`);
    if (/[<>"|?*\u0000-\u001f]/.test(raw))
      refuse(`a name with a character Windows does not allow is refused (${raw})`);
    if (literal && /[. ]$/.test(raw))
      refuse(`a \\\\?\\ name ending in a dot or space is refused (${raw})`);
    const name = raw.replace(/[. ]+$/, "");
    if (name === "") refuse("a name made only of spaces and dots is refused");
    if (WIN_DEVICES.test(name.split(".")[0]!.trimEnd()))
      refuse(`Windows device names are refused (${raw})`);
    names.push(name);
  }
  return { root: `${path[0]!.toUpperCase()}:\\`, names };
}

function parsePosix(path: string, options: ResolveOptions): Parsed {
  const names = path.split("/").filter((n) => n !== "" && n !== ".");
  if (options.platform !== "darwin") return { root: "/", names };
  const fold = (p: string) => foldName(p, "darwin", options.caseSensitiveVolume);
  const key = fold(names.join("/"));
  const data = fold("System/Volumes/Data/");
  const firm =
    key.startsWith(data) &&
    MAC_FIRMLINKS.map(fold).some((f) => `${key}/`.startsWith(`${data}${f}/`));
  return { root: "/", names: firm ? names.slice(3) : names };
}

function parse(path: string, options: ResolveOptions): Parsed {
  const parsed = options.platform === "win32" ? parseWindows(path) : parsePosix(path, options);
  if (parsed.names.some((n) => n.length > MAX_NAME))
    refuse(`a name is longer than ${MAX_NAME} characters`);
  return parsed;
}

const isAbsolute = (path: string, platform: HostPlatform) =>
  platform === "win32" ? /^([A-Za-z]:|[\\/])/.test(path) : path.startsWith("/");

function checkInput(input: string, platform: HostPlatform): void {
  if (typeof input !== "string" || input.trim() === "") refuse("the path is empty");
  if (input.includes("\u0000")) refuse("a path with a NUL byte is refused");
  if (input.length > maxLength(platform))
    refuse(`the path is longer than ${maxLength(platform)} characters`);
}

async function walk(start: Parsed, options: ResolveOptions) {
  const { platform, fs } = options;
  const sep = platform === "win32" ? "\\" : "/";
  let root = start.root;
  const done: string[] = [];
  const todo = [...start.names];
  const at = (names: ReadonlyArray<string>) => root + names.join(sep);
  let links = 0;
  while (todo.length > 0) {
    const name = todo.shift()!;
    if (name === "..") {
      done.pop();
      continue;
    }
    // macOS: /var, /tmp and /etc are /private/var, /private/tmp and /private/etc.
    if (
      platform === "darwin" &&
      done.length === 0 &&
      /^(var|tmp|etc)$/.test(foldName(name, platform, options.caseSensitiveVolume))
    ) {
      done.push("private");
    }
    const here = at([...done, name]);
    const kind = await fs.kind(here);
    if (kind === "missing") {
      if (todo.includes("..")) refuse(`".." after a name that does not exist (${name}) is refused`);
      const missing = [name, ...todo];
      if (platform === "win32" && missing.some((n) => /~\d/.test(n))) {
        refuse(`a short name that does not exist cannot be expanded (${name})`);
      }
      return {
        root,
        real: at([...done, ...missing]),
        names: [...done, ...missing],
        missing: missing.length,
      };
    }
    if (kind === "file" && todo.length > 0) refuse(`${here} is a file, not a folder`);
    if (kind === "link") {
      if (platform === "linux" && here.startsWith("/proc/"))
        refuse(`links under /proc are refused (${here})`);
      links += 1;
      if (links > MAX_LINKS)
        refuse(`a link loop or a chain of more than ${MAX_LINKS} links (at ${here})`);
      const target = await fs.readLink(here);
      checkInput(target, platform);
      const parsed = parse(isAbsolute(target, platform) ? target : at([...done, target]), options);
      root = parsed.root;
      done.length = 0;
      todo.unshift(...parsed.names);
      continue;
    }
    if (platform === "win32" && /~\d/.test(name)) {
      const long = await fs.longName(here);
      if (long === undefined || long === "" || /[\\/:]/.test(long)) {
        refuse(`the short name ${name} cannot be expanded to its long name`);
      }
      done.push(long!);
    } else {
      done.push(name);
    }
  }
  return { root, real: at(done), names: done, missing: 0 };
}

/** Resolves a path a host tool was given to its real target, or refuses it with a plain reason. Never caches. */
export async function resolveHostPath(
  input: string,
  options: ResolveOptions,
): Promise<ResolvedPath> {
  const { platform } = options;
  try {
    checkInput(input, platform);
    let path = input;
    if (!isAbsolute(input, platform)) {
      if (options.base === undefined) refuse("a relative path needs a base folder");
      checkInput(options.base!, platform);
      if (!isAbsolute(options.base!, platform)) refuse("the base folder must be an absolute path");
      path = `${options.base}${platform === "win32" ? "\\" : "/"}${input}`;
    }
    const { root, real, names, missing } = await walk(parse(path, options), options);
    if (real.length > maxLength(platform))
      refuse(`the real path is longer than ${maxLength(platform)} characters`);
    const parts = names.map((n) => foldName(n, platform, options.caseSensitiveVolume));
    const keyRoot = platform === "win32" ? root.replace("\\", "/") : "/";
    return { ok: true, real, compareKey: keyRoot + parts.join("/"), parts, missing };
  } catch (error) {
    if (isRefused(error)) return { ok: false, reason: error.reason };
    const code = (error as { code?: unknown })?.code;
    return {
      ok: false,
      reason: `the path could not be read${typeof code === "string" ? ` (${code})` : ""}`,
    };
  }
}

/** The real file system of this computer, for `resolveHostPath`. */
export const nodeHostPathFs: HostPathFs = {
  kind: async (path) => {
    try {
      const stat = await NodeFSP.lstat(path);
      return stat.isSymbolicLink() ? "link" : stat.isDirectory() ? "dir" : "file";
    } catch (error) {
      if ((error as { code?: unknown }).code === "ENOENT") return "missing";
      throw error;
    }
  },
  readLink: (path) => NodeFSP.readlink(path),
  longName: async (path) => {
    const real = await NodeFSP.realpath(path);
    return real.split(/[\\/]/).pop();
  },
};
