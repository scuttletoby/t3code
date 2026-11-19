// @effect-diagnostics nodeBuiltinImport:off globalFetch:off globalTimers:off - a plain read-only CLI
// probe like forkReport.ts: it must not start the server runtime (migrations, lock, writes).
/**
 * Fork: asks the T3 server running in a home whether it is busy, on loopback via its activity probe.
 * Only the server's own "quiet" is quiet; every state it cannot be sure of is not.
 */
import * as NodeFS from "node:fs";
import * as NodePath from "node:path";

import {
  ACTIVITY_PROBE_PATH,
  ACTIVITY_PROBE_VERSION,
  type ActivityProbeAnswer,
} from "../fork/activityProbe.ts";
import { isProcessAlive } from "../serverRuntimeState.ts";

export type ActivityProbeReadState =
  | "quiet"
  | "busy"
  | "stopped"
  | "off"
  | "unreachable"
  | "unknown";

/** One line of output; `quiet` and `reason` first, so a hook that keeps the first line's start keeps them. */
export interface ActivityProbeReading {
  readonly quiet: boolean;
  readonly reason: string;
  readonly state: ActivityProbeReadState;
  /** The server process the home names, or null when there is none to name. */
  readonly pid: number | null;
  /** The server's own answer, when it gave one this reader understands. */
  readonly answer: ActivityProbeAnswer | null;
}

export interface ActivityProbeReadDeps {
  readonly fetch: (url: string, init: { readonly signal: AbortSignal }) => Promise<Response>;
  readonly isAlive: (pid: number) => boolean;
  readonly timeoutMs: number;
}

const defaultActivityProbeReadDeps: ActivityProbeReadDeps = {
  fetch: (url, init) => globalThis.fetch(url, { signal: init.signal, redirect: "manual" }),
  isAlive: isProcessAlive,
  timeoutMs: 5_000,
};

const notQuiet = (
  state: Exclude<ActivityProbeReadState, "quiet">,
  reason: string,
  pid: number | null,
  answer: ActivityProbeAnswer | null = null,
): ActivityProbeReading => ({ quiet: false, reason, state, pid, answer });

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isCount = (value: unknown) => Number.isInteger(value) && (value as number) >= 0;

/** The answer, if it is one this reader knows and it agrees with itself; else null. */
function decodeAnswer(body: unknown): ActivityProbeAnswer | null {
  if (!isRecord(body) || body.probeVersion !== ACTIVITY_PROBE_VERSION) return null;
  const { quiet, reason, state, pid, serverVersion, activity } = body;
  if (typeof quiet !== "boolean" || (reason !== null && typeof reason !== "string")) return null;
  if (state !== "running" && state !== "stopping") return null;
  if (!Number.isInteger(pid) || typeof serverVersion !== "string") return null;
  if (
    activity !== null &&
    !(
      isRecord(activity) &&
      ["activeRuns", "queuedRuns", "pendingRequests", "backgroundTasks", "terminalCommands"].every(
        (key) => isCount(activity[key]),
      )
    )
  ) {
    return null;
  }
  // Quiet exactly when there is no reason, and only while running.
  if (quiet !== (reason === null) || (quiet && state !== "running")) return null;
  return body as unknown as ActivityProbeAnswer;
}

/** Where to ask: the recorded origin when it is loopback, else loopback at the recorded port. */
function probeUrl(origin: string, port: number): string {
  let hostname = "127.0.0.1";
  try {
    const recorded = new URL(origin).hostname;
    if (/^(127\.\d+\.\d+\.\d+|\[::1\]|localhost)$/i.test(recorded)) hostname = recorded;
  } catch {
    // An origin that is no URL: loopback at the port.
  }
  return `http://${hostname}:${port}${ACTIVITY_PROBE_PATH}`;
}

type RuntimeRecord = { readonly pid: number; readonly port: number; readonly origin: string };

function readRuntimeRecord(
  home: string,
): { readonly record: RuntimeRecord } | { readonly missing: true } | { readonly bad: string } {
  const path = NodePath.join(home, "userdata", "server-runtime.json");
  let text: string;
  try {
    text = NodeFS.readFileSync(path, "utf8");
  } catch (error) {
    const code = error instanceof Error && "code" in error ? error.code : undefined;
    if (code === "ENOENT" || code === "ENOTDIR") return { missing: true };
    return { bad: `${path} could not be read (${String(code ?? error)})` };
  }
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return { bad: `${path} is not JSON` };
  }
  if (
    !isRecord(value) ||
    value.version !== 1 ||
    !Number.isInteger(value.pid) ||
    !Number.isInteger(value.port) ||
    (value.port as number) <= 0 ||
    (value.port as number) > 65_535 ||
    typeof value.origin !== "string"
  ) {
    return { bad: `${path} is not a runtime record this T3 knows` };
  }
  return { record: { pid: value.pid as number, port: value.port as number, origin: value.origin } };
}

/** Asks with a deadline of its own, so a fetch that ignores the signal still ends. */
async function fetchWithin(
  deps: ActivityProbeReadDeps,
  url: string,
): Promise<{ readonly status: number; readonly text: string } | { readonly failed: string }> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<{ readonly failed: string }>((resolve) => {
    timer = setTimeout(() => {
      controller.abort();
      resolve({ failed: `it did not answer within ${deps.timeoutMs} ms` });
    }, deps.timeoutMs);
  });
  try {
    return await Promise.race([
      deps.fetch(url, { signal: controller.signal }).then(
        async (response) => ({ status: response.status, text: await response.text() }),
        (error: unknown) => ({
          failed: `the request failed (${error instanceof Error ? error.message : String(error)})`,
        }),
      ),
      deadline,
    ]);
  } finally {
    clearTimeout(timer);
  }
}

/** The reading as `__fork-report --activity` prints it: one line of JSON. */
export const formatActivityReading = (reading: ActivityProbeReading): string =>
  JSON.stringify(reading);

export async function readActivityProbe(
  baseDir: string,
  deps: ActivityProbeReadDeps = defaultActivityProbeReadDeps,
): Promise<ActivityProbeReading> {
  const home = NodePath.resolve(baseDir);
  const read = readRuntimeRecord(home);
  if ("missing" in read) return notQuiet("stopped", `no T3 server is running in ${home}`, null);
  if ("bad" in read) return notQuiet("unknown", read.bad, null);
  const { pid, port, origin } = read.record;
  if (!deps.isAlive(pid)) {
    return notQuiet("stopped", `the T3 server ${pid} recorded in ${home} is not running`, null);
  }

  const url = probeUrl(origin, port);
  const asked = await fetchWithin(deps, url);
  if ("failed" in asked) {
    return notQuiet("unreachable", `T3 (process ${pid}) at ${url}: ${asked.failed}`, pid);
  }
  const { status } = asked;
  let body: unknown = null;
  try {
    body = JSON.parse(asked.text);
  } catch {
    // Not JSON: an older server's web app, or something else on the port.
  }
  if (status === 404 && isRecord(body) && typeof body.error === "string") {
    if (body.error.includes("server.activityProbe")) {
      return notQuiet("off", `T3 (process ${pid}) has ${body.error}`, pid);
    }
  }
  if (status !== 200) {
    const said = isRecord(body) && typeof body.error === "string" ? `: ${body.error}` : "";
    return notQuiet(
      "unknown",
      `T3 (process ${pid}) answered ${url} with HTTP ${status}${said} (an older T3 has no activity probe)`,
      pid,
    );
  }
  const answer = decodeAnswer(body);
  if (answer === null) {
    return notQuiet(
      "unknown",
      `T3 (process ${pid}) gave an answer this reader does not know (probe version ${ACTIVITY_PROBE_VERSION} expected)`,
      pid,
    );
  }
  if (answer.pid !== pid) {
    return notQuiet(
      "unknown",
      `process ${answer.pid} answered on port ${port}, but the home's T3 server is ${pid}`,
      pid,
    );
  }
  if (answer.quiet)
    return { quiet: true, reason: "nothing is running", state: "quiet", pid, answer };
  return notQuiet("busy", answer.reason ?? "busy", pid, answer);
}
