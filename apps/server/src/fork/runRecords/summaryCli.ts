// @effect-diagnostics nodeBuiltinImport:off globalDate:off - a plain node script, outside the Effect runtime.
/**
 * Fork: prints the run records summary (issue 315); reads files only, so it is safe beside a live
 * server. `node apps/server/src/fork/runRecords/summaryCli.ts [--days 7] [--to 2026-11-20] [--json]
 * [--state-dir <dir>] [more runs.jsonl files]`. See docs/fork/run-records.md.
 */
import * as NodeFS from "node:fs";
import * as NodeOS from "node:os";
import * as NodePath from "node:path";
import * as NodeURL from "node:url";

import { parseRateTable, type RateTable } from "../../usage/usagePricing.ts";
import { parseRunRecordLines, type RunRecord } from "./runRecord.ts";
import { formatRunRecordsSummary, summarizeRunRecords } from "./runRecordsSummary.ts";

const DAY_MS = 24 * 60 * 60 * 1000;

export interface SummaryCliIo {
  /** The file's lines as they are read, or null when it does not exist. Other errors throw. */
  readonly readLines: (path: string) => Iterable<string> | null;
  /** A small file's text, or null when it does not exist. Other errors throw. */
  readonly readFile: (path: string) => string | null;
  readonly env: Readonly<Record<string, string | undefined>>;
  readonly homeDir: string;
  readonly nowMs: number;
}

export function runSummaryCli(argv: ReadonlyArray<string>, io: SummaryCliIo): string {
  let days = 7;
  let toMs = io.nowMs;
  let json = false;
  let stateDir = NodePath.join(io.env.T3CODE_HOME ?? NodePath.join(io.homeDir, ".t3"), "userdata");
  const extraFiles: Array<string> = [];
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index]!;
    const value = () => {
      const next = argv[++index];
      if (next === undefined) throw new Error(`${arg} needs a value`);
      return next;
    };
    if (arg === "--days") days = Number(value());
    else if (arg === "--to") toMs = Date.parse(value()) + DAY_MS;
    else if (arg === "--state-dir") stateDir = value();
    else if (arg === "--json") json = true;
    else if (arg.startsWith("--")) throw new Error(`unknown option ${arg}`);
    else extraFiles.push(arg);
  }
  if (!Number.isFinite(days) || days <= 0) throw new Error("--days needs a positive number");
  if (!Number.isFinite(toMs)) throw new Error("--to needs a date like 2026-11-20");
  const fromMs = toMs - days * DAY_MS;
  const inWindow = (record: RunRecord) => {
    const ended = Date.parse(record.endedAt);
    return ended >= fromMs && ended < toMs;
  };
  // The state folder's file is missing before the first recorded run; a file named here is not.
  const ownFile = NodePath.join(stateDir, "run-records", "runs.jsonl");
  const sources = [
    { file: ownFile, lines: io.readLines(ownFile) },
    ...extraFiles.map((file) => {
      const lines = io.readLines(file);
      if (lines === null) throw new Error(`${file}: no such file`);
      return { file, lines };
    }),
  ];
  // Line by line, keeping only the window's records: the file can grow past what one string holds.
  const records: Array<RunRecord> = sources.flatMap(({ lines }) =>
    lines === null ? [] : parseRunRecordLines(lines, inWindow),
  );
  const summary = summarizeRunRecords(records, {
    toMs,
    fromMs,
    rates: readRates(io.readFile(NodePath.join(stateDir, "usage-model-rates.json"))),
  });
  return json ? `${JSON.stringify(summary, null, 2)}\n` : formatRunRecordsSummary(summary);
}

function readRates(text: string | null): RateTable | null {
  if (text === null) return null;
  try {
    const cache = JSON.parse(text) as { readonly document?: unknown };
    const table = parseRateTable(cache.document);
    return table.size === 0 ? null : table;
  } catch {
    return null;
  }
}

const isMissing = (error: unknown) => (error as NodeJS.ErrnoException | null)?.code === "ENOENT";

const CHUNK_BYTES = 1 << 20;
/** A longer line is no record (one is about 1 KB): it is skipped up to the next new line. */
const MAX_LINE_CHARS = 1 << 20;

/**
 * A file's lines, read a chunk at a time, so no string ever holds more than one chunk and one line.
 * Null when the file does not exist; any other error (unreadable, a folder) throws as it is read.
 */
export function readFileLines(file: string, chunkBytes = CHUNK_BYTES): Iterable<string> | null {
  let fd: number;
  try {
    fd = NodeFS.openSync(file, "r");
  } catch (error) {
    if (isMissing(error)) return null;
    throw error;
  }
  const openFd = fd;
  return (function* () {
    const decoder = new TextDecoder("utf-8");
    const buffer = new Uint8Array(chunkBytes);
    let pending = "";
    let skipping = false;
    try {
      for (;;) {
        const read = NodeFS.readSync(openFd, buffer, 0, chunkBytes, null);
        const text =
          read === 0
            ? decoder.decode()
            : decoder.decode(buffer.subarray(0, read), { stream: true });
        let start = 0;
        for (let end = text.indexOf("\n"); end !== -1; end = text.indexOf("\n", start)) {
          const line = pending + text.slice(start, end);
          start = end + 1;
          pending = "";
          if (skipping) skipping = false;
          else yield line;
        }
        if (!skipping) {
          pending += text.slice(start);
          if (pending.length > MAX_LINE_CHARS) {
            pending = "";
            skipping = true;
          }
        }
        if (read === 0) break;
      }
      if (!skipping && pending !== "") yield pending;
    } finally {
      NodeFS.closeSync(openFd);
    }
  })();
}

/** Reads the real files; used when the command runs. */
export const nodeSummaryIo = (): SummaryCliIo => ({
  readLines: (file) => readFileLines(file),
  readFile: (file) => {
    try {
      return NodeFS.readFileSync(file, "utf8");
    } catch (error) {
      if (isMissing(error)) return null;
      throw error;
    }
  },
  env: process.env,
  homeDir: NodeOS.homedir(),
  nowMs: Date.now(),
});

const invokedDirectly =
  process.argv[1] !== undefined &&
  import.meta.url === NodeURL.pathToFileURL(NodePath.resolve(process.argv[1])).href;

if (invokedDirectly) {
  try {
    process.stdout.write(runSummaryCli(process.argv.slice(2), nodeSummaryIo()));
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 2;
  }
}
