// @effect-diagnostics nodeBuiltinImport:off - Reads the server sources to check how agent turns start.
// Fork (go-live kit): `serve --hold-work` holds the work gate from boot. This contract keeps every
// path that can start an agent turn behind that gate. An agent turn reaches a provider only
// through the services frozen below; the outbox daemon is their only caller and it claims
// effects only inside `admission.admit`. A new file that uses one of them, or a new place that
// opens a provider session or starts a turn, fails here until it is checked against the gate and
// added on purpose.
import * as NodeFS from "node:fs";
import * as NodePath from "node:path";

import { describe, expect, it } from "vite-plus/test";

const SRC = import.meta.dirname;

const isTestSupport = (file: string) =>
  /\.test\.ts$|\.testkit\.ts$|(^|\/)testkit\/|(^|\/)testing\//.test(file);

const sources: ReadonlyArray<{ readonly file: string; readonly text: string }> = (
  NodeFS.readdirSync(SRC, { recursive: true }) as string[]
)
  .map((file) => file.split(NodePath.sep).join("/"))
  .filter((file) => file.endsWith(".ts") && !isTestSupport(file))
  .map((file) => ({ file, text: NodeFS.readFileSync(NodePath.join(SRC, file), "utf8") }))
  .toSorted((a, b) => a.file.localeCompare(b.file));

const filesMatching = (pattern: RegExp, options: { readonly skipAdapters?: boolean } = {}) =>
  sources
    .filter(({ file }) => !(options.skipAdapters && file.startsWith("orchestration-v2/Adapters/")))
    .filter(({ text }) => pattern.test(text))
    .map(({ file }) => file);

const read = (file: string) => {
  const source = sources.find((candidate) => candidate.file === file);
  if (source === undefined) throw new Error(`missing source ${file}`);
  return source.text;
};

describe("agent turn start paths stay behind the work gate", () => {
  it("found the server sources", () => {
    expect(sources.length).toBeGreaterThan(100);
  });

  // Services that start, steer or continue a provider turn. Only the outbox executor in
  // EffectWorker.ts (and each service's own file) may use them.
  it.each([
    [
      "ProviderTurnStartServiceV2",
      ["orchestration-v2/EffectWorker.ts", "orchestration-v2/ProviderTurnStartService.ts"],
    ],
    [
      "RunExecutionServiceV2",
      ["orchestration-v2/ProviderTurnStartService.ts", "orchestration-v2/RunExecutionService.ts"],
    ],
    [
      "ProviderTurnControlServiceV2",
      ["orchestration-v2/EffectWorker.ts", "orchestration-v2/ProviderTurnControlService.ts"],
    ],
    [
      "CheckpointRollbackServiceV2",
      ["orchestration-v2/CheckpointRollbackService.ts", "orchestration-v2/EffectWorker.ts"],
    ],
    ["OrchestrationEffectExecutorV2", ["orchestration-v2/EffectWorker.ts"]],
    ["OrchestrationEffectWorkerV2", ["orchestration-v2/EffectWorker.ts"]],
  ] as const)("only the gated outbox uses %s", (service, files) => {
    expect(filesMatching(new RegExp(`\\b${service}\\b`))).toEqual([...files].sort());
  });

  // Direct provider calls outside the adapters that implement them.
  it.each([
    [
      "starts a turn",
      /\.startTurn\(/,
      [
        "orchestration-v2/ProviderSessionManager.ts",
        "orchestration-v2/ProviderTurnStartService.ts",
        "orchestration-v2/RunExecutionService.ts",
      ],
    ],
    [
      // The importer only reads native history on a user's import; it starts no turn.
      "opens a provider session",
      /\.openSession\(/,
      [
        "orchestration-v2/ProviderAdapterRegistry.ts",
        "orchestration-v2/ProviderSessionManager.ts",
        "project/AgentSessionImporter.ts",
      ],
    ],
    [
      "opens a managed provider session",
      /\b(?:providerSessions|sessions|sessionManager)\s*\.open\(/,
      [
        "orchestration-v2/CheckpointRollbackService.ts",
        "orchestration-v2/ProviderTurnStartService.ts",
      ],
    ],
  ] as const)("only known files call what %s", (_label, pattern, files) => {
    expect(filesMatching(pattern, { skipAdapters: true })).toEqual([...files].sort());
  });

  it("the outbox daemon claims effects only inside admission.admit", () => {
    const worker = read("orchestration-v2/EffectWorker.ts");
    const calls = [...worker.matchAll(/\bworker\.(runOnce|runRecoveryOnce|drain)\b/g)];
    expect(calls.map((call) => call[1])).toEqual(["runOnce"]);
    const before = worker.slice(0, calls[0]!.index);
    const admit = before.lastIndexOf(".admit(");
    expect(admit).toBeGreaterThan(-1);
    // Nothing closes the admit call between it and the claim.
    expect(before.slice(admit)).toMatch(
      /^\.admit\(\s*Deferred\.succeed\(admitted, undefined\)\.pipe\(\s*Effect\.andThen\($/,
    );
    expect(worker).toMatch(/admission\.register\("outbox"\)/);
  });

  it("worktree preparation for a launch runs only inside admission.admit", () => {
    const launch = read("orchestration-v2/ThreadLaunchService.ts");
    expect(launch).toMatch(
      /admission\.admit\(Ref\.set\(admitted, true\)\.pipe\(Effect\.andThen\(preparation\)\)\)/,
    );
    expect(launch).toMatch(/admission\.register\("thread-launch"\)/);
  });

  // Every timed source (scheduled tasks, webhook coalescing, waits, usage-limit recovery, usage
  // refresh) registers with the shared scheduler, which runs none of them when held from boot.
  it("timed sources run only through the shared scheduler, which the held gate pauses", () => {
    const scheduler = read("scheduling/Scheduler.ts");
    expect(scheduler).toMatch(/\(yield\* WorkAdmission\)\.heldFromBoot/);
    expect(filesMatching(/\bscheduler\.register\(/i).toSorted()).toEqual(
      [
        "orchestration-v2/UsageLimitRecoveryWorker.ts",
        "scheduledTasks/ScheduledTaskService.ts",
        "usage/HeadlessUsageRefresher.ts",
        "waits/WaitWorker.ts",
      ].sort(),
    );
  });
});
