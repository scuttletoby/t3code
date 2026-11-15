// @effect-diagnostics nodeBuiltinImport:off - Reads the server sources to check how agent turns start.
// Fork (go-live kit): `serve --hold-work` holds the work gate from boot. This contract keeps every
// path that can start an agent turn behind that gate. An agent turn reaches a provider only
// through the services frozen below; the outbox daemon is their only caller and it claims
// effects only inside `admission.admit`. The checks are keyed on those services and on the
// `startTurn` name, not on how a call is written, so a renamed variable or a method taken into a
// local still fails here. A new file that uses one of them fails until it is checked against the
// gate and added on purpose. The runtime check (`checkHoldWork` in serverRuntimeStartup.ts) is the
// fail-closed guard; this test catches a new path before it ships.
//
// Out of scope: one-shot model calls through `TextGeneration` (thread titles, commit messages, pull
// request guides). They are not agent turns: no tools, no session, no repo changes. Titles go
// through the outbox, so a held server holds them; commit messages and guides run only when a user
// asks. A future automatic one-shot call on a held server would not be caught here.
import * as NodeFS from "node:fs";
import * as NodePath from "node:path";

import { describe, expect, it } from "vite-plus/test";

import { WORK_ADMISSION_CONSUMERS } from "./workAdmission.ts";

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

  // Services that hold provider sessions or adapters, outside the adapters themselves. Each file
  // was checked: it only reads, releases or shuts down sessions, reads adapter capabilities, or
  // reaches a provider only from the gated outbox executor.
  it.each([
    [
      "ProviderSessionManagerV2",
      [
        // Reads pending app requests of a live session.
        "mcpApps/McpAppRequests.ts",
        // Opens a session only from an outbox effect (rollback).
        "orchestration-v2/CheckpointRollbackService.ts",
        "orchestration-v2/EffectWorker.ts",
        // Stops and releases sessions; turns are outbox effects.
        "orchestration-v2/Orchestrator.ts",
        "orchestration-v2/ProviderSessionManager.ts",
        "orchestration-v2/ProviderTurnControlService.ts",
        "orchestration-v2/ProviderTurnStartService.ts",
        // Answers runtime requests of a running turn.
        "orchestration-v2/RuntimeRequestService.ts",
        // Releases sessions of archived or deleted threads.
        "orchestration-v2/ThreadManagementService.ts",
        // Stops sessions after a sign-out.
        "provider/ProviderAuthService.ts",
        // Shuts sessions down at exit.
        "serverRuntimeStartup.ts",
        "storageCleanup.ts",
        // Reads session state for clients.
        "ws.ts",
      ],
    ],
    [
      "ProviderAdapterRegistryV2",
      [
        // Lists provider instances.
        "mcp/OrchestratorMcpService.ts",
        // Reads capabilities; turns are outbox effects.
        "orchestration-v2/Orchestrator.ts",
        "orchestration-v2/ProviderAdapterRegistry.ts",
        "orchestration-v2/ProviderSessionManager.ts",
        // Reads metadata to check a provider switch.
        "orchestration-v2/ProviderSwitchService.ts",
        // Opens a session only to read native history on a user's import; starts no turn.
        "project/AgentSessionImporter.ts",
      ],
    ],
  ] as const)("only known files use %s", (service, files) => {
    expect(filesMatching(new RegExp(`\\b${service}\\b`), { skipAdapters: true })).toEqual(
      [...files].sort(),
    );
  });

  // The name, not a call: a method taken into a local or passed on still counts.
  it("only the turn services mention startTurn outside the adapters", () => {
    expect(filesMatching(/\bstartTurn\b/, { skipAdapters: true })).toEqual(
      [
        "orchestration-v2/ProviderSessionManager.ts",
        "orchestration-v2/ProviderTurnStartService.ts",
        "orchestration-v2/RunExecutionService.ts",
      ].sort(),
    );
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
    expect(scheduler).toMatch(/admission\.register\("scheduler"\)/);
    expect(scheduler).toMatch(/if \(admission\.heldFromBoot\)/);
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

// A held boot must prove the flag reached the gate every consumer uses, and fail closed if not.
// From the design review of the first version, where removing the pass-through left every test
// green; holdWork.boot.test.ts boots the real server for the same reason.
describe("hold-work boot is fail-closed", () => {
  it("the scheduler is a registered consumer of the shared gate", () => {
    expect(WORK_ADMISSION_CONSUMERS as ReadonlyArray<string>).toContain("scheduler");
  });

  it("a held boot checks that every consumer uses the held gate before serving", () => {
    const boot = read("server.ts") + read("serverRuntimeStartup.ts");
    expect(boot).toMatch(/(holdWork|heldFromBoot)[\s\S]{0,600}missingConsumers/);
    // Before activation: the check fails startup, which aborts activation, so nothing parked runs.
    const startup = read("serverRuntimeStartup.ts");
    const check = startup.indexOf('runStartupPhase("hold-work.check"');
    expect(check).toBeGreaterThan(-1);
    expect(check).toBeLessThan(startup.indexOf("options?.activate"));
    // The outbox registers before activation too, with the gate its daemon admits through.
    expect(startup).toMatch(/EffectWorker\.prepareDaemon\b/);
  });
});
