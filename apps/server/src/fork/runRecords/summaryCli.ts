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
  /** The file's text, or null when it is missing or unreadable. */
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
  const files = [NodePath.join(stateDir, "run-records", "runs.jsonl"), ...extraFiles];
  const records: Array<RunRecord> = files.flatMap((file) =>
    parseRunRecordLines(io.readFile(file) ?? ""),
  );
  const summary = summarizeRunRecords(records, {
    toMs,
    fromMs: toMs - days * DAY_MS,
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

const invokedDirectly =
  process.argv[1] !== undefined &&
  import.meta.url === NodeURL.pathToFileURL(NodePath.resolve(process.argv[1])).href;

if (invokedDirectly) {
  try {
    process.stdout.write(
      runSummaryCli(process.argv.slice(2), {
        readFile: (file) => {
          try {
            return NodeFS.readFileSync(file, "utf8");
          } catch {
            return null;
          }
        },
        env: process.env,
        homeDir: NodeOS.homedir(),
        nowMs: Date.now(),
      }),
    );
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 2;
  }
}
