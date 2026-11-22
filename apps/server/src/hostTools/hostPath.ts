// @effect-diagnostics nodeBuiltinImport:off - the thin real file-system adapter at the end reads links with node:fs.
// Fork only (host tools, design docs/fork/host-tools.md): the per-OS path layer. Pure and OS-neutral: the platform and
// every file-system call are injected, so the Windows, macOS and Linux cases all run on any CI machine.
//
// Contract (what the policy core's `decide`, stage 1.2, and the reader, stage 1.4, build on):
// - `real` is the path after every link, junction and mount point was followed to its final target, `.` and `..`
//   applied to that real location (Windows: `..` in the typed input is collapsed first, as Win32 does), macOS
//   firmlinks mapped to the root form (`/System/Volumes/Data/Users` is `/Users`), Windows short names expanded and
//   Windows trailing dots and spaces removed. A caller opens `real`, never its own input.
// - The operating system checks the model: the existing part of `real` must be what the OS itself resolves it to
//   (`realPath`, Node's native realpath). Any difference (a Windows volume mount point, subst or mapped drive, a short
//   name without `~`, a name the volume folds differently from `foldName`) is refused with a plain reason.
// - Refused outright: Linux /proc, /sys, /dev and the document portal /run/user/<uid>/doc; macOS /dev and /.vol; any
//   device, pipe or socket; Windows devices, UNC, volume and device paths and alternate data streams.
// - Two keys, both with `/` as the only separator:
//   - `compareKey`, for reach (allow) matching: exact per OS. NFC on macOS, then case folded with `foldName` where the
//     volume ignores case (Windows always: upper case, root `C:/`; macOS unless `caseSensitiveVolume`: lower case;
//     never on Linux). An unfolded key only under-matches an allow, which is safe.
//   - `secretKey`, for secret, deny and auto-run matching: always folded with `secretFold` on every OS and every path,
//     whatever `caseSensitiveVolume` says (Linux casefold folders, vfat, exFAT, NTFS and SMB mounts and WSL /mnt/c
//     ignore case too). Root `/` or `c:/`. It over-folds, which only over-matches a deny.
//   - Patterns: fold only their literal parts (never the glob or regex syntax), with `foldName` for `compareKey`
//     patterns and `secretFold` for `secretKey` patterns.
//   - The home folder, profile folder, reach-map roots and anchored secret places must go through `resolveHostPath`
//     too; their keys are what get matched (a home behind a link or on another volume has a different key).
//   - `~` is never expanded: `~/.ssh` with a base is a folder literally named `~`. The caller expands it first.
// - `parts` are `compareKey`'s names after the root; the last `missing` of them do not exist yet (a name for stat or
//   search). Their parent exists and is real; a missing name is kept as typed, never guessed.
// - Every call reads the file system again; nothing is cached, so a link changed between two calls is followed anew.
// - The reader (1.4) must, because `decide` is pure and cannot re-check:
//   - open `real` with no link followed at any part (Linux openat2 RESOLVE_NO_SYMLINKS|RESOLVE_NO_MAGICLINKS, macOS
//     O_NOFOLLOW_ANY, Windows FILE_FLAG_OPEN_REPARSE_POINT per part, opened as `\\?\` + `real`) and refuse when it
//     meets a link (one was swapped in after this call, also a `missing` name created as a link later); or open it,
//     read the handle's final path, and run it through `resolveHostPath` and `decide` again, refusing any change;
//   - check `isFile()` on the opened handle and refuse a link count above 1 (hard links).
// - Not handled (cannot be seen by path): bind mounts, nullfs, FUSE aliases other than the portal, procfs or sysfs
//   mounted elsewhere, and hard links (1.4 above). Windows `$UpCase` is per volume; the OS check covers existing parts.
import * as NodeFSP from "node:fs/promises";

import * as Schema from "effect/Schema";

export type HostPlatform = "win32" | "darwin" | "linux";
/** "other" is a device, pipe or socket. */
export type EntryKind = "file" | "dir" | "link" | "other" | "missing";

export interface HostPathFs {
  /** The entry itself, links not followed (symlinks and the junctions the OS reports as links are "link"). */
  readonly kind: (path: string) => Promise<EntryKind>;
  /** The target text of a link, as stored (absolute or relative to the link's folder). */
  readonly readLink: (path: string) => Promise<string>;
  /** Windows: the long name of an existing entry whose name may be a short (8.3) name; undefined when unknown. */
  readonly longName: (path: string) => Promise<string | undefined>;
  /** The OS's own final path of an existing path (native realpath: every link, mount point, firmlink and drive). */
  readonly realPath: (path: string) => Promise<string>;
}

export interface ResolveOptions {
  readonly platform: HostPlatform;
  readonly fs: HostPathFs;
  /** An absolute folder a relative input is taken from; without one a relative input is refused. */
  readonly base?: string;
  /** macOS only, `compareKey` only: the volume was formatted case-sensitive (APFS default is case-insensitive). */
  readonly caseSensitiveVolume?: boolean;
}

export type ResolvedPath =
  | {
      readonly ok: true;
      readonly real: string;
      readonly compareKey: string;
      readonly secretKey: string;
      readonly parts: ReadonlyArray<string>;
      readonly missing: number;
    }
  | { readonly ok: false; readonly reason: string };

const MAX_LINKS = 40;
const MAX_STEPS = 4096;
const MAX_NAME = 255;
const maxLength = (platform: HostPlatform) => (platform === "win32" ? 32_767 : 4096);
const WIN_DEVICES = /^(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])$/i;
// macOS firmlinks (the full list of /usr/share/firmlinks, macOS 14): `/System/Volumes/Data/<x>` is the same folder as
// `/<x>`. A head ending in `*` matches any name with that start (Assets, AssetsV2, PreinstalledAssets...).
const MAC_FIRMLINKS = [
  "AppleInternal",
  "Applications",
  "Library",
  "System/Library/Caches",
  "System/Library/Assets*",
  "System/Library/PreinstalledAssets*",
  "System/Library/CoreServices/CoreTypes.bundle/Contents/Library",
  "System/Library/Speech",
  "Users",
  "Volumes",
  "cores",
  "opt",
  "private",
  "usr/libexec/cups",
  "usr/local",
  "usr/share/snmp",
].map((head) => head.split("/"));

class Refused extends Schema.TaggedError<Refused>()("HostPathRefused", { reason: Schema.String }) {}
const isRefused = Schema.is(Refused);
const refuse = (reason: string): never => {
  throw new Refused({ reason });
};

/**
 * The `compareKey` fold of one name: Windows upper case (as NTFS compares), macOS NFC then lower case (APFS), Linux as
 * is. A code point whose case form has another length (German ß, Turkish İ) is kept as typed. Exact, not safe for
 * secrets: names this misses are caught by the OS check (existing parts) and by `secretFold`.
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

const IGNORABLE = /\p{Default_Ignorable_Code_Point}/gu;
/**
 * The `secretKey` fold; over-folds so names any file system treats as one meet. From the JS Unicode data, no hand
 * table: each code point's full upper then lower case (ß ẞ->ss, ᾳ ᾼ->αι, ς->σ, ſ->s, µ->μ, Kelvin->k), ignorables
 * dropped, NFKC (full-width), i + dot above -> i (İ ı), until stable. Tested against the regex engine's case folding.
 */
export function secretFold(name: string): string {
  let out = name;
  for (let round = 0; round < 4; round++) {
    let next = "";
    for (const ch of out) next += ch.toUpperCase().toLowerCase();
    next = next.replace(IGNORABLE, "").normalize("NFKC").replaceAll("i\u0307", "i");
    if (next === out) break;
    out = next;
  }
  return out;
}

/** macOS: `/System/Volumes/Data/<firmlink>/...` names to the root form; anything else unchanged. */
function macFirmlinks(names: ReadonlyArray<string>): ReadonlyArray<string> {
  const f = names.slice(0, 12).map(secretFold);
  if (f.length < 4 || f[0] !== "system" || f[1] !== "volumes" || f[2] !== "data") return names;
  const rest = f.slice(3);
  const hit = MAC_FIRMLINKS.some(
    (head) =>
      head.length <= rest.length &&
      head.every((h, i) =>
        h.endsWith("*")
          ? rest[i]!.startsWith(secretFold(h.slice(0, -1)))
          : rest[i] === secretFold(h),
      ),
  );
  return hit ? names.slice(3) : names;
}

/** Refuses a place whose files are not ordinary files or name another file by number (the walk and the OS answer). */
function refuseSpecial(names: ReadonlyArray<string>, platform: HostPlatform): void {
  const f = names.slice(0, 4).map(secretFold);
  if (platform === "linux") {
    if (/^(proc|sys|dev)$/.test(f[0] ?? ""))
      refuse(`paths under /${f[0]} are refused (process, kernel and device files)`);
    if (f[0] === "run" && f[1] === "user" && /^\d+$/.test(f[2] ?? "") && f[3] === "doc")
      refuse("the document portal (/run/user/<uid>/doc) is refused: it names files by number");
  }
  if (platform === "darwin" && /^(dev|\.vol)$/.test(f[0] ?? ""))
    refuse(`paths under /${f[0]} are refused (devices, open descriptors, files named by number)`);
}

interface Parsed {
  readonly root: string; // "C:\\" or "/"
  readonly names: ReadonlyArray<string>;
}

function parseWindows(input: string, typed: boolean): Parsed {
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
      if (raw === ".." && literal) refuse('a \\\\?\\ path with ".." is refused');
      // Win32 collapses ".." in the typed path before any link is seen; a link target's ".." is walked.
      if (raw === ".." && typed) names.pop();
      else if (raw === "..") names.push(raw);
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

function parse(path: string, platform: HostPlatform, typed = false): Parsed {
  const parsed =
    platform === "win32"
      ? parseWindows(path, typed)
      : { root: "/", names: path.split("/").filter((n) => n !== "" && n !== ".") };
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
  let steps = 0;
  while (todo.length > 0) {
    if (++steps > MAX_STEPS) refuse(`more than ${MAX_STEPS} names to walk through links`);
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
    if (done.length < 4) refuseSpecial([...done, name], platform);
    const here = at([...done, name]);
    const kind = await fs.kind(here);
    if (kind === "missing") {
      if (todo.includes("..")) refuse(`".." after a name that does not exist (${name}) is refused`);
      const missing = [name, ...todo];
      if (platform === "win32" && missing.some((n) => /~\d/.test(n))) {
        refuse(`a short name that does not exist cannot be expanded (${name})`);
      }
      return { root, done, missing };
    }
    if (kind === "other") refuse(`${here} is a device, pipe or socket, not a file or folder`);
    if (kind === "file" && todo.length > 0) refuse(`${here} is a file, not a folder`);
    if (kind === "link") {
      links += 1;
      if (links > MAX_LINKS)
        refuse(`a link loop or a chain of more than ${MAX_LINKS} links (at ${here})`);
      const target = await fs.readLink(here);
      checkInput(target, platform);
      const parsed = parse(isAbsolute(target, platform) ? target : at([...done, target]), platform);
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
  return { root, done, missing: [] as string[] };
}

const compareKeyOf = (root: string, names: ReadonlyArray<string>, options: ResolveOptions) => {
  const parts = names.map((n) => foldName(n, options.platform, options.caseSensitiveVolume));
  return { parts, key: (options.platform === "win32" ? `${root[0]}:/` : "/") + parts.join("/") };
};

/** Resolves a path a host tool was given to its real target, or refuses it with a plain reason. Never caches. */
export async function resolveHostPath(
  input: string,
  options: ResolveOptions,
): Promise<ResolvedPath> {
  const { platform, fs } = options;
  try {
    checkInput(input, platform);
    let path = input;
    if (!isAbsolute(input, platform)) {
      if (options.base === undefined) refuse("a relative path needs a base folder");
      checkInput(options.base!, platform);
      if (!isAbsolute(options.base!, platform)) refuse("the base folder must be an absolute path");
      path = `${options.base}${platform === "win32" ? "\\" : "/"}${input}`;
    }
    const walked = await walk(parse(path, platform, true), options);
    const sep = platform === "win32" ? "\\" : "/";
    const done = platform === "darwin" ? macFirmlinks(walked.done) : walked.done;
    // The OS is the authority for the part that exists: its answer must give the same key, or the path is refused.
    const existing = walked.root + done.join(sep);
    const os = await fs.realPath(existing);
    checkInput(os, platform);
    if (!isAbsolute(os, platform)) refuse(`the system gave a relative real path (${os})`);
    const osParsed = parse(os, platform);
    if (osParsed.names.includes("..")) refuse(`the system gave a real path with ".." (${os})`);
    const osNames = platform === "darwin" ? macFirmlinks(osParsed.names) : osParsed.names;
    refuseSpecial(osNames, platform);
    if (
      compareKeyOf(osParsed.root, osNames, options).key !==
      compareKeyOf(walked.root, done, options).key
    ) {
      refuse(`the system resolves ${existing} to ${os}, another place; refused`);
    }
    const names = [...done, ...walked.missing];
    refuseSpecial(names, platform);
    const real = walked.root + names.join(sep);
    if (real.length > maxLength(platform))
      refuse(`the real path is longer than ${maxLength(platform)} characters`);
    const { parts, key } = compareKeyOf(walked.root, names, options);
    const secretRoot = platform === "win32" ? `${walked.root[0]!.toLowerCase()}:/` : "/";
    const secretKey = secretRoot + names.map(secretFold).join("/");
    return { ok: true, real, compareKey: key, secretKey, parts, missing: walked.missing.length };
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
      if (stat.isSymbolicLink()) return "link";
      return stat.isDirectory() ? "dir" : stat.isFile() ? "file" : "other";
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
  // fs/promises realpath is the native one (GetFinalPathNameByHandleW, realpath(3)).
  realPath: (path) => NodeFSP.realpath(path),
};
