/**
 * Fork: the handoff's `command` source against real processes: its output is the handoff, and a
 * non-zero exit, a timeout, empty output or output over 32 KB is a failure, never a partial
 * handoff. No shell; the thread and project ids are in its environment.
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import { ProjectId, ThreadId } from "@t3tools/contracts";
import * as HostProcess from "@t3tools/shared/HostProcess";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Path from "effect/Path";

import * as ProjectStore from "../../orchestration-v2/ProjectStore.ts";
import * as ProcessRunner from "../../processRunner.ts";
import {
  HANDOFF_COMMAND_ENV_KEYS,
  HANDOFF_COMMAND_MAX_BYTES,
  HANDOFF_COMMAND_TIMEOUT_SECONDS,
  HANDOFF_PROJECT_ENV,
  HANDOFF_THREAD_ENV,
  type HandoffCommandResult,
  make,
} from "./handoffCommand.ts";

const threadId = ThreadId.make("handoff-thread");
const projectId = ProjectId.make("handoff-project");
const node = (script: string, ...args: ReadonlyArray<string>) => [
  process.execPath,
  "-e",
  script,
  ...args,
];

/** A project store that knows one project, at `root` (none: it is gone). */
const projectAt = (root: string | null) =>
  Layer.mock(ProjectStore.ProjectStoreV2)({
    get: () =>
      Effect.succeed(
        root === null
          ? Option.none()
          : Option.some({ workspaceRoot: root } as unknown as ProjectStore.ProjectRow),
      ),
  });

/**
 * The keys CI's test runner puts into every process it starts. `vp run test:cached` traces the
 * files each test reads: it preloads a library into each child and adds these again on every
 * exec, also after `env -i`, so a command run under it sees them although the server passed
 * nothing. Ignored only while this test itself runs under that tracer; nowhere else.
 */
const TEST_TRACER_ENV_KEYS: ReadonlyArray<string> =
  process.env.FSPY_PAYLOAD === undefined ? [] : ["LD_PRELOAD", "FSPY_PAYLOAD"];

const services = ProcessRunner.layer.pipe(Layer.provideMerge(NodeServices.layer));

/** Runs `command` in a fresh temporary project folder; the source times out at `timeoutSeconds`. */
const runIn = (
  command: ReadonlyArray<string>,
  options: { readonly timeoutSeconds?: number; readonly root?: "temp" | "missing" | null } = {},
) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const root = yield* fs.makeTempDirectoryScoped({ prefix: "handoff-command-" });
    const source = yield* make(
      options.timeoutSeconds === undefined ? {} : { timeoutSeconds: options.timeoutSeconds },
    ).pipe(
      Effect.provide(
        projectAt(
          options.root === null ? null : options.root === "missing" ? `${root}/gone` : root,
        ),
      ),
    );
    const result = yield* source.run({ command, threadId, projectId });
    return { result, root, source };
  });

const failedWith = (result: HandoffCommandResult, reason: string) => {
  assert.isFalse(result.ok);
  assert.include(result.ok ? "" : result.reason, reason);
};

describe("handoffCommand", () => {
  it.live("prints the handoff in the project folder, with the thread and project ids", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const script = `console.log(JSON.stringify({ cwd: process.cwd(), thread: process.env.${HANDOFF_THREAD_ENV}, project: process.env.${HANDOFF_PROJECT_ENV} }))`;
      const { result, root } = yield* runIn(node(script));
      assert.isTrue(result.ok);
      const printed = JSON.parse(result.ok ? result.text : "{}");
      assert.strictEqual(yield* fs.realPath(printed.cwd), yield* fs.realPath(root));
      assert.strictEqual(printed.thread, threadId);
      assert.strictEqual(printed.project, projectId);
    }).pipe(Effect.scoped, Effect.provide(services)),
  );

  it.live("gets a short list of the server's environment, not the whole of it (#785)", () =>
    Effect.gen(function* () {
      const { result } = yield* runIn(node("console.log(JSON.stringify(process.env))"));
      const printed: Record<string, string> = JSON.parse(result.ok ? result.text : "{}");
      // Only the list and the two ids: none of the server's tokens, keys or test settings.
      const allowed = new Set<string>([
        ...HANDOFF_COMMAND_ENV_KEYS,
        HANDOFF_THREAD_ENV,
        HANDOFF_PROJECT_ENV,
        ...TEST_TRACER_ENV_KEYS,
      ]);
      assert.deepStrictEqual(
        Object.keys(printed).filter((key) => !allowed.has(key)),
        [],
      );
      assert.strictEqual(printed.PATH, (yield* HostProcess.Environment).PATH);
      assert.strictEqual(printed[HANDOFF_THREAD_ENV], threadId);
      const odd = yield* runIn(["/opt/a=b/notes"]);
      failedWith(odd.result, 'its program\'s path has an "="');
    }).pipe(Effect.scoped, Effect.provide(services)),
  );

  it.live("runs no shell: `;` and `$(...)` reach the program as they are, and run nothing", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const args = ["a; touch made-by-shell", "$(touch made-by-shell)", "`touch made-by-shell`"];
      const { result, root } = yield* runIn(
        node("console.log(JSON.stringify(process.argv.slice(1)))", ...args),
      );
      assert.deepStrictEqual(JSON.parse(result.ok ? result.text : "null"), args);
      assert.isFalse(yield* fs.exists(path.join(root, "made-by-shell")));
    }).pipe(Effect.scoped, Effect.provide(services)),
  );

  it.live("runs again with the same result: it only reads", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const root = yield* fs.makeTempDirectoryScoped({ prefix: "handoff-command-" });
      yield* fs.writeFileString(path.join(root, "state.md"), "# State\nPR 12 open\n");
      const source = yield* make().pipe(Effect.provide(projectAt(root)));
      const command = node("process.stdout.write(require('fs').readFileSync('state.md', 'utf8'))");
      const first = yield* source.run({ command, threadId, projectId });
      const second = yield* source.run({ command, threadId, projectId });
      assert.deepStrictEqual(first, { ok: true, text: "# State\nPR 12 open" });
      assert.deepStrictEqual(second, first);
    }).pipe(Effect.scoped, Effect.provide(services)),
  );

  it.live("a non-zero exit is a failure, also when it printed a handoff", () =>
    Effect.gen(function* () {
      const { result } = yield* runIn(node("console.log('STATE'); process.exit(3)"));
      failedWith(result, "exit code 3");
    }).pipe(Effect.scoped, Effect.provide(services)),
  );

  it.live(
    "a command past its time limit is killed and is a failure; what it printed is not used",
    () =>
      Effect.gen(function* () {
        assert.strictEqual(HANDOFF_COMMAND_TIMEOUT_SECONDS, 60);
        const { result } = yield* runIn(
          node("console.log('PARTIAL STATE'); setInterval(() => {}, 1000)"),
          { timeoutSeconds: 1 },
        );
        failedWith(result, "timed out after 1s");
      }).pipe(Effect.scoped, Effect.provide(services)),
  );

  it.live("its stdin is closed: a program that reads it ends instead of waiting", () =>
    Effect.gen(function* () {
      const { result } = yield* runIn(
        node("console.log('read ' + require('fs').readFileSync(0, 'utf8').length)"),
        { timeoutSeconds: 5 },
      );
      assert.deepStrictEqual(result, { ok: true, text: "read 0" });
    }).pipe(Effect.scoped, Effect.provide(services)),
  );

  it.live("empty output, or only blank lines, is a failure", () =>
    Effect.gen(function* () {
      failedWith((yield* runIn(node(""))).result, "printed nothing");
      failedWith((yield* runIn(node("console.log('\\n  \\n')"))).result, "printed nothing");
    }).pipe(Effect.scoped, Effect.provide(services)),
  );

  it.live("output over 32 KB is a failure; just under it is the handoff", () =>
    Effect.gen(function* () {
      assert.strictEqual(HANDOFF_COMMAND_MAX_BYTES, 32 * 1024);
      const over = yield* runIn(node(`process.stdout.write('x'.repeat(${32 * 1024 + 1}))`));
      failedWith(over.result, "printed too much output");
      const under = yield* runIn(node(`process.stdout.write('x'.repeat(${32 * 1024 - 1}))`));
      assert.isTrue(under.result.ok);
    }).pipe(Effect.scoped, Effect.provide(services)),
  );

  it.live("a program that cannot start, or a project that is gone, is a failure", () =>
    Effect.gen(function* () {
      // A spawn that fails (its project folder is gone) could not start. Not a missing program:
      // `timeout` can exit before its empty stdin is written, which then reads as an output error.
      failedWith(
        (yield* runIn(node("console.log('x')"), { root: "missing" })).result,
        "could not start",
      );
      // 126 and 127, as a launcher reports them, read the same on every platform.
      for (const code of [126, 127]) {
        failedWith(
          (yield* runIn(node(`process.exit(${code})`))).result,
          `its program could not start (exit code ${code})`,
        );
      }
      failedWith((yield* runIn(node("console.log('x')"), { root: null })).result, "project");
    }).pipe(Effect.scoped, Effect.provide(services)),
  );

  it.live("a missing program reads as not started every time, not as unreadable output", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const root = yield* fs.makeTempDirectoryScoped({ prefix: "handoff-command-" });
      const source = yield* make().pipe(Effect.provide(projectAt(root)));
      // `timeout` exits 127 at once, often before its closed stdin is written: that write then
      // failed with EPIPE in a few runs of ten, read as "its output could not be read".
      for (let run = 0; run < 30; run += 1) {
        const result = yield* source.run({
          command: ["/no/such/handoff-program"],
          threadId,
          projectId,
        });
        failedWith(result, "could not start");
      }
    }).pipe(Effect.scoped, Effect.provide(services)),
  );
});
