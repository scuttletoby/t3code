// @effect-diagnostics nodeBuiltinImport:off - a plain async reader with an injected host; DS3 wraps it in Effect.
/**
 * Fork: dynamic slots' readings: the machine's live state as the capacity rule's input, read through
 * an injectable host (fixtures: `capacity/capacity-readings.json`). No timer, cache or write. A failed
 * or half reading is `null` / `"unreadable"`: never free room. OS gaps: README.md in `capacity/`.
 */
import * as NodeChildProcess from "node:child_process";
import * as NodeFSP from "node:fs/promises";
import * as NodeOS from "node:os";

import type { CapacityLevel, CapacityPsi } from "./capacityPressure.ts";
import type { CapacityFacts, CapacityPiece, CapacityReservation } from "./capacityRule.ts";

/** A file's text, or why there is none (`missing`: it does not exist). */
export type CapacityFileRead =
  | { readonly text: string }
  | { readonly error: "missing" | "unreadable" };

export interface CapacityHost {
  /** As `HostProcess.Platform` (`linux`, `darwin`, `win32`, ...). */
  readonly platform: string;
  readonly readFile: (path: string) => Promise<CapacityFileRead>;
  /** Standard output, or `null` when the command failed, timed out or does not exist. */
  readonly run: (file: string, args: ReadonlyArray<string>) => Promise<string | null>;
  readonly totalMemBytes: () => number;
  readonly freeMemBytes: () => number;
  readonly cpuCount: () => number;
}

/** The cgroup version that holds the memory controller; `null` off Linux or without cgroups. */
export type CapacityCgroup = "v2" | "v1" | null;

/** A reading this OS cannot give, and what stands in for it. */
export interface CapacityGap {
  readonly reading: "available" | "swap" | "psi" | "osLevel" | "demand";
  readonly instead: string;
}

/** One read of the machine. Total and available come from the same read (a consistent pair). */
export interface MachineReadings {
  readonly totalMb: number | null;
  readonly availableMb: number | null;
  readonly cpuCores: number | null;
  readonly swap: { readonly totalMb: number; readonly freeMb: number } | null | "unreadable";
  readonly psi: CapacityPsi | null | "unreadable";
  readonly osLevel: CapacityLevel | null | "unreadable";
  readonly cgroup: CapacityCgroup;
  readonly gaps: ReadonlyArray<CapacityGap>;
}

/** One piece's memory now. `gone`: its cgroup ended (between listing and reading): drop it. */
export type PieceDemand =
  | { readonly gone: true }
  | {
      readonly gone: false;
      /** anon + shmem + swap (page cache is not demand); `null` owes the piece's full cost. */
      readonly demandMb: number | null;
      /** `memory.current` and `memory.peak` (with page cache), for cost learning later. */
      readonly currentMb: number | null;
      readonly peakMb: number | null;
    };

const MiB = 1048576;
const CGROUP = "/sys/fs/cgroup";

/** Digits only and a safe integer, else `null`. */
function whole(text: string | undefined): number | null {
  if (text === undefined || !/^\d+$/.test(text)) return null;
  const value = Number(text);
  return Number.isSafeInteger(value) ? value : null;
}

const mbOf = (bytes: number | null): number | null =>
  bytes === null ? null : Math.floor(bytes / MiB);

async function readText(host: CapacityHost, path: string): Promise<CapacityFileRead> {
  try {
    return await host.readFile(path);
  } catch {
    return { error: "unreadable" };
  }
}

async function textOf(host: CapacityHost, path: string): Promise<string | null> {
  const read = await readText(host, path);
  return "text" in read ? read.text : null;
}

/** `Key:   123 kB` from `/proc/meminfo`, in bytes; `null` when absent, cut short or in another unit. */
function meminfoBytes(text: string, key: string): number | null {
  const kb = whole(new RegExp(`^${key}:[ \\t]+(\\d+) kB$`, "m").exec(text)?.[1]);
  return kb === null ? null : kb * 1024;
}

/** `key 123` (bytes) from a cgroup `memory.stat`. */
const statBytes = (text: string, key: string): number | null =>
  whole(new RegExp(`^${key} (\\d+)$`, "m").exec(text)?.[1]);

/** A file holding one number (`memory.current`). */
const oneNumber = (text: string | null): number | null =>
  text === null ? null : whole(/^(\d+)\n?$/.exec(text)?.[1]);

const psiLine = (kind: string) =>
  new RegExp(`^${kind} avg10=(\\d+\\.\\d+) avg60=\\d+\\.\\d+ avg300=\\d+\\.\\d+ total=\\d+$`, "m");

/** A PSI file: `null` when it does not exist, `"unreadable"` when broken or cut short. */
function psiOf(read: CapacityFileRead): CapacityPsi | null | "unreadable" {
  if ("error" in read) return read.error === "missing" ? null : "unreadable";
  const some = psiLine("some").exec(read.text)?.[1];
  const full = psiLine("full").exec(read.text)?.[1];
  if (some === undefined || full === undefined) return "unreadable";
  const someAvg10Pct = Number(some);
  const fullAvg10Pct = Number(full);
  return someAvg10Pct <= 100 && fullAvg10Pct <= 100 ? { someAvg10Pct, fullAvg10Pct } : "unreadable";
}

/** A safe cgroup path (`/a.slice/b.scope`): below the root, never leaving the cgroup tree. */
function cgroupPath(path: string): string | null {
  if (!path.startsWith("/") || path.includes("\0")) return null;
  const parts = path.split("/").filter((part) => part !== "");
  return parts.length === 0 || parts.some((part) => part === "." || part === "..")
    ? null
    : parts.map((p) => `/${p}`).join("");
}

async function cgroupVersion(host: CapacityHost): Promise<CapacityCgroup> {
  if ((await textOf(host, `${CGROUP}/cgroup.controllers`)) !== null) return "v2";
  return (await textOf(host, `${CGROUP}/memory/memory.stat`)) !== null ? "v1" : null;
}

/**
 * The memory limit of the cgroup this process sees as its root (a container), in bytes: `null`
 * without one, `"unreadable"` when there is one that cannot be read. `usedBytes` without the page
 * cache the kernel reclaims first; `null` when it cannot be read.
 */
async function containerMemory(
  host: CapacityHost,
  cgroup: CapacityCgroup,
  totalBytes: number | null,
): Promise<
  { readonly limitBytes: number; readonly usedBytes: number | null } | null | "unreadable"
> {
  const used = (current: string | null, stat: string | null, inactiveKey: string) => {
    const now = oneNumber(current);
    const inactive = stat === null ? null : statBytes(stat, inactiveKey);
    return now === null || inactive === null ? null : Math.max(0, now - inactive);
  };
  if (cgroup === "v2") {
    // Only at the root of a cgroup namespace: a host process's own unit limit is not the machine's.
    if (!/^0::\/\n?$/m.test((await textOf(host, "/proc/self/cgroup")) ?? "")) return null;
    const max = await readText(host, `${CGROUP}/memory.max`);
    if ("error" in max) return max.error === "missing" ? null : "unreadable";
    if (/^max\n?$/.test(max.text)) return null;
    const limitBytes = oneNumber(max.text);
    if (limitBytes === null) return "unreadable";
    const current = await textOf(host, `${CGROUP}/memory.current`);
    const stat = await textOf(host, `${CGROUP}/memory.stat`);
    return { limitBytes, usedBytes: used(current, stat, "inactive_file") };
  }
  if (cgroup === "v1") {
    const limit = await readText(host, `${CGROUP}/memory/memory.limit_in_bytes`);
    if ("error" in limit) return limit.error === "missing" ? null : "unreadable";
    // "No limit" is a huge number (above 2^53 or above the machine).
    if (/^\d{16,}\n?$/.test(limit.text)) return null;
    const limitBytes = oneNumber(limit.text);
    if (limitBytes === null) return "unreadable";
    if (totalBytes !== null && limitBytes >= totalBytes) return null;
    const current = await textOf(host, `${CGROUP}/memory/memory.usage_in_bytes`);
    const stat = await textOf(host, `${CGROUP}/memory/memory.stat`);
    return { limitBytes, usedBytes: used(current, stat, "total_inactive_file") };
  }
  return null;
}

/** CPU limit of a cgroup v2 namespace root (`cpu.max`); `null` none, `"unreadable"` broken. */
async function containerCpus(host: CapacityHost, cgroup: CapacityCgroup) {
  if (cgroup !== "v2") return null;
  if (!/^0::\/\n?$/m.test((await textOf(host, "/proc/self/cgroup")) ?? "")) return null;
  const max = await readText(host, `${CGROUP}/cpu.max`);
  if ("error" in max) return max.error === "missing" ? null : "unreadable";
  if (/^max \d+\n?$/.test(max.text)) return null;
  const [, quota, period] = /^(\d+) (\d+)\n?$/.exec(max.text) ?? [];
  const q = whole(quota);
  const p = whole(period);
  return q === null || p === null || p === 0 || q === 0 ? "unreadable" : q / p;
}

const nodeNumber = (read: () => number): number | null => {
  try {
    const value = read();
    return Number.isSafeInteger(value) && value > 0 ? value : null;
  } catch {
    return null;
  }
};

const cpuCoresOf = (host: CapacityHost): number | null => nodeNumber(host.cpuCount);

const NO_DEMAND: CapacityGap = {
  reading: "demand",
  instead: "no per-piece memory without cgroups: every piece owes its full cost",
};

async function readLinux(host: CapacityHost): Promise<MachineReadings> {
  const meminfo = (await textOf(host, "/proc/meminfo")) ?? "";
  let totalBytes = meminfoBytes(meminfo, "MemTotal");
  let availableBytes = meminfoBytes(meminfo, "MemAvailable");
  const cgroup = await cgroupVersion(host);
  const container = await containerMemory(host, cgroup, totalBytes);
  if (container === "unreadable") availableBytes = null;
  else if (container !== null) {
    if (totalBytes !== null) totalBytes = Math.min(totalBytes, container.limitBytes);
    availableBytes =
      container.usedBytes === null || availableBytes === null
        ? null
        : Math.min(availableBytes, Math.max(0, container.limitBytes - container.usedBytes));
  }
  const totalMb = mbOf(totalBytes);
  const availableMb = mbOf(availableBytes);

  const swapTotal = mbOf(meminfoBytes(meminfo, "SwapTotal"));
  const swapFree = mbOf(meminfoBytes(meminfo, "SwapFree"));
  const swap: MachineReadings["swap"] =
    swapTotal === null || swapFree === null || swapFree > swapTotal
      ? "unreadable"
      : swapTotal === 0
        ? null
        : { totalMb: swapTotal, freeMb: swapFree };

  const psi = psiOf(await readText(host, "/proc/pressure/memory"));
  const cpus = await containerCpus(host, cgroup);
  const hostCpus = cpuCoresOf(host);
  const cpuCores =
    cpus === "unreadable" || hostCpus === null
      ? null
      : cpus === null
        ? hostCpus
        : Math.min(hostCpus, cpus);

  const gaps: Array<CapacityGap> = [];
  if (psi === null) {
    gaps.push({
      reading: "psi",
      instead: "kernel without PSI: available memory against the margin only",
    });
  }
  if (cgroup === null) gaps.push(NO_DEMAND);
  return {
    totalMb,
    availableMb:
      availableMb !== null && totalMb !== null && availableMb > totalMb ? null : availableMb,
    cpuCores,
    swap,
    psi,
    osLevel: null,
    cgroup,
    gaps,
  };
}

/** `vm_stat`: free + inactive + speculative pages (what can be used without swapping). */
function darwinAvailableBytes(output: string): number | null {
  const pageSize = whole(/page size of (\d+) bytes/.exec(output)?.[1]);
  const pages = (name: string) =>
    whole(new RegExp(`^Pages ${name}:\\s+(\\d+)\\.$`, "m").exec(output)?.[1]);
  const free = pages("free");
  const inactive = pages("inactive");
  const speculative = pages("speculative");
  if (
    pageSize === null ||
    pageSize === 0 ||
    free === null ||
    inactive === null ||
    speculative === null
  )
    return null;
  const bytes = (free + inactive + speculative) * pageSize;
  return Number.isSafeInteger(bytes) ? bytes : null;
}

const DARWIN_LEVELS: Readonly<Record<string, CapacityLevel>> = {
  "1": "ok",
  "2": "elevated",
  "4": "severe",
};

async function readDarwin(host: CapacityHost): Promise<MachineReadings> {
  const gaps: Array<CapacityGap> = [
    { reading: "psi", instead: "no PSI: the OS memory pressure level" },
    {
      reading: "swap",
      instead: "swap files grow on demand (no fixed size): counted as no swap (wider margin)",
    },
    NO_DEMAND,
  ];
  const totalBytes = nodeNumber(host.totalMemBytes);
  const vmStat = await run(host, "/usr/bin/vm_stat", []);
  let availableBytes = vmStat === null ? null : darwinAvailableBytes(vmStat);
  if (availableBytes === null) {
    // Free pages alone: always less than really available, never more.
    availableBytes = nodeNumber(host.freeMemBytes);
    gaps.push({ reading: "available", instead: "vm_stat failed: free pages only (an under-read)" });
  }
  const level = (
    await run(host, "/usr/sbin/sysctl", ["-n", "kern.memorystatus_vm_pressure_level"])
  )?.trim();
  const totalMb = mbOf(totalBytes);
  const availableMb = mbOf(availableBytes);
  return {
    totalMb,
    availableMb:
      availableMb !== null && totalMb !== null && availableMb > totalMb ? null : availableMb,
    cpuCores: cpuCoresOf(host),
    swap: null,
    psi: null,
    osLevel:
      level !== undefined && Object.hasOwn(DARWIN_LEVELS, level)
        ? DARWIN_LEVELS[level]!
        : "unreadable",
    cgroup: null,
    gaps,
  };
}

/** Windows and the rest: what Node gives (on Windows `freemem` is available memory with standby). */
function readNodeOnly(host: CapacityHost): MachineReadings {
  const totalMb = mbOf(nodeNumber(host.totalMemBytes));
  const availableMb = mbOf(nodeNumber(host.freeMemBytes));
  return {
    totalMb,
    availableMb:
      availableMb !== null && totalMb !== null && availableMb > totalMb ? null : availableMb,
    cpuCores: cpuCoresOf(host),
    swap: null,
    psi: null,
    osLevel: null,
    cgroup: null,
    gaps: [
      {
        reading: "swap",
        instead: "Node does not give the page file: counted as no swap (wider margin)",
      },
      { reading: "psi", instead: "no PSI: available memory against the margin only" },
      {
        reading: "osLevel",
        instead: "no OS pressure level from Node: available memory against the margin only",
      },
      NO_DEMAND,
    ],
  };
}

async function run(
  host: CapacityHost,
  file: string,
  args: ReadonlyArray<string>,
): Promise<string | null> {
  try {
    return await host.run(file, args);
  } catch {
    return null;
  }
}

/** Reads the machine once: total (live, never kept), available, cores, swap, PSI, OS level. */
export function readMachine(host: CapacityHost): Promise<MachineReadings> {
  if (host.platform === "linux") return readLinux(host);
  if (host.platform === "darwin") return readDarwin(host);
  return Promise.resolve(readNodeOnly(host));
}

/**
 * The memory PSI of a box (the cgroup a piece goes into, `/a.slice`). `null` where the machine has
 * no PSI or no cgroup v2; `"unreadable"` when the box's file is missing, broken or cut short.
 */
export async function readBoxPsi(
  host: CapacityHost,
  machine: Pick<MachineReadings, "cgroup" | "psi">,
  box: string,
): Promise<CapacityPsi | null | "unreadable"> {
  if (machine.psi === null || machine.cgroup !== "v2") return null;
  const path = cgroupPath(box);
  if (path === null) return "unreadable";
  const psi = psiOf(await readText(host, `${CGROUP}${path}/memory.pressure`));
  return psi === null ? "unreadable" : psi;
}

/** One piece's memory from its cgroup (`/a.slice/b.scope`). A missing swap part counts 0 (owes more). */
export async function readPieceDemand(
  host: CapacityHost,
  cgroup: CapacityCgroup,
  scope: string,
): Promise<PieceDemand> {
  const unknown = { gone: false, demandMb: null, currentMb: null, peakMb: null } as const;
  const path = cgroupPath(scope);
  if (cgroup === null || path === null) return unknown;
  const v2 = cgroup === "v2";
  const base = v2 ? `${CGROUP}${path}` : `${CGROUP}/memory${path}`;
  const stat = await readText(host, `${base}/memory.stat`);
  if ("error" in stat) return stat.error === "missing" ? { gone: true } : unknown;
  const anon = statBytes(stat.text, v2 ? "anon" : "total_rss");
  const shmem = statBytes(stat.text, v2 ? "shmem" : "total_shmem");
  const swap = v2
    ? oneNumber(await textOf(host, `${base}/memory.swap.current`))
    : statBytes(stat.text, "total_swap");
  const current = oneNumber(
    await textOf(host, `${base}/${v2 ? "memory.current" : "memory.usage_in_bytes"}`),
  );
  const peak = oneNumber(
    await textOf(host, `${base}/${v2 ? "memory.peak" : "memory.max_usage_in_bytes"}`),
  );
  return {
    gone: false,
    demandMb: anon === null || shmem === null ? null : mbOf(anon + shmem + (swap ?? 0)),
    currentMb: mbOf(current),
    peakMb: mbOf(peak),
  };
}

/** The rule's input: one machine read plus what the caller knows (its pieces, caps, box). */
export function capacityFactsOf(
  machine: MachineReadings,
  known: {
    readonly pieceCaps: boolean;
    readonly pieces: ReadonlyArray<CapacityPiece> | null;
    readonly boxPsi?: CapacityPsi | null | "unreadable";
    readonly reservations?: ReadonlyArray<CapacityReservation> | null;
  },
): CapacityFacts {
  const { totalMb, availableMb, cpuCores, swap, psi, osLevel } = machine;
  return { totalMb, availableMb, cpuCores, swap, psi, osLevel, ...known };
}

/** The real host: files, commands (2 s limit) and Node's numbers; `platform` from HostProcess. */
export function nodeCapacityHost(platform: string): CapacityHost {
  return {
    platform,
    readFile: async (path) => {
      try {
        return { text: await NodeFSP.readFile(path, "utf8") };
      } catch (error) {
        const code = (error as NodeJS.ErrnoException).code;
        return { error: code === "ENOENT" || code === "ENOTDIR" ? "missing" : "unreadable" };
      }
    },
    run: (file, args) =>
      new Promise((resolve) => {
        NodeChildProcess.execFile(
          file,
          [...args],
          { timeout: 2000, maxBuffer: MiB, windowsHide: true },
          (error, stdout) => resolve(error ? null : stdout),
        );
      }),
    totalMemBytes: () => NodeOS.totalmem(),
    freeMemBytes: () => NodeOS.freemem(),
    cpuCount: () => NodeOS.availableParallelism(),
  };
}
