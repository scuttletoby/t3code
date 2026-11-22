/**
 * Fork: a child that closed its stdin before the runner wrote it (it exited at once) ends with its
 * own exit code, not as "its output could not be read". The handoff's `command` source writes an
 * empty stdin to a missing program under `timeout`, which exits 127 at once; the write then failed
 * with EPIPE in a few runs of a hundred (fork/processRunnerFork.ts).
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as PlatformError from "effect/PlatformError";
import * as Sink from "effect/Sink";
import * as Stream from "effect/Stream";
import { ChildProcessSpawner } from "effect/process";

import * as ProcessRunner from "../processRunner.ts";
import { isClosedStdin } from "./processRunnerFork.ts";

/** The error Node's stdin sink fails with when the child's end of the pipe is closed. */
const stdinFailure = (code: string) =>
  PlatformError.systemError({
    _tag: "Unknown",
    module: "ChildProcess",
    method: "fromWritable(stdin)",
    syscall: "write",
    cause: Object.assign(new Error(`write ${code}`), { code, syscall: "write" }),
  });

/** A child that exited with `code` and printed `stdout`; writing its stdin fails as `stdin` says. */
const exitedChild = (input: { readonly code: number; readonly stdout: string; stdin: string }) =>
  ChildProcessSpawner.make(() =>
    Effect.succeed(
      ChildProcessSpawner.makeHandle({
        pid: ChildProcessSpawner.ProcessId(1),
        exitCode: Effect.succeed(ChildProcessSpawner.ExitCode(input.code)),
        isRunning: Effect.succeed(false),
        kill: () => Effect.void,
        unref: Effect.succeed(Effect.void),
        stdin: Sink.forEach(() => Effect.fail(stdinFailure(input.stdin))),
        stdout: Stream.encodeText(Stream.make(input.stdout)),
        stderr: Stream.empty,
        all: Stream.empty,
        getInputFd: () => Sink.drain,
        getOutputFd: () => Stream.empty,
      }),
    ),
  );

const runOn = (
  spawner: ChildProcessSpawner.ChildProcessSpawner["Service"],
  input: ProcessRunner.ProcessRunInput,
) =>
  Effect.service(ProcessRunner.ProcessRunner).pipe(
    Effect.flatMap((runner) => runner.run(input)),
    Effect.provide(
      ProcessRunner.layer.pipe(
        Layer.provide(Layer.succeed(ChildProcessSpawner.ChildProcessSpawner, spawner)),
      ),
    ),
  );

describe("a child that closed its stdin", () => {
  it.effect("ends with its own exit code and output when its stdin pipe is broken (EPIPE)", () =>
    Effect.gen(function* () {
      const result = yield* runOn(exitedChild({ code: 127, stdout: "said", stdin: "EPIPE" }), {
        command: "timeout",
        args: ["60", "/no/such/program"],
        stdin: "",
      });
      assert.strictEqual(result.code, 127);
      assert.strictEqual(result.stdout, "said");
      assert.isFalse(result.timedOut);
    }),
  );

  it.effect("any other stdin failure is still a stdin error", () =>
    Effect.gen(function* () {
      const error = yield* runOn(exitedChild({ code: 0, stdout: "", stdin: "EIO" }), {
        command: "program",
        args: [],
        stdin: "input",
      }).pipe(Effect.flip);
      assert.strictEqual(error._tag, "ProcessStdinError");
    }),
  );

  it.live("a real child that exits before reading 1 MB of stdin reports its exit code", () =>
    Effect.gen(function* () {
      const runner = yield* ProcessRunner.ProcessRunner;
      // More than a pipe holds, so the write is still pending when the child exits.
      const result = yield* runner.run({
        command: process.execPath,
        args: ["-e", "process.exit(3)"],
        stdin: "x".repeat(1024 * 1024),
        timeout: "30 seconds",
      });
      assert.strictEqual(result.code, 3);
    }).pipe(Effect.provide(ProcessRunner.layer.pipe(Layer.provideMerge(NodeServices.layer)))),
  );

  it("is told by the broken pipe's code anywhere under the error", () => {
    assert.isTrue(isClosedStdin(stdinFailure("EPIPE")));
    assert.isTrue(isClosedStdin({ code: "EPIPE" }));
    assert.isFalse(isClosedStdin(stdinFailure("EIO")));
    assert.isFalse(isClosedStdin(undefined));
    assert.isFalse(isClosedStdin("EPIPE"));
  });
});
