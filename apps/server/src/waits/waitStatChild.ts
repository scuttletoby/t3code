// @effect-diagnostics nodeBuiltinImport:off -- runs alone in the short-lived file-check process.
/**
 * Fork (waits): the file check that runs in its own short-lived process. It is used when the file
 * checks in the server are stuck (a dead network mount): a process can be killed, and its `stat`
 * holds no thread of the server's I/O pool. It prints one line of JSON and ends.
 */
import * as NodeFS from "node:fs";

/** The path to check; passed in the environment, so every host reads it the same way. */
export const WAIT_STAT_PATH_ENV = "T3_WAIT_STAT_PATH";

/** The check as code for `-e` (Node, and Electron as Node); the same as `runWaitStat`. */
export const WAIT_STAT_SCRIPT = `require("node:fs").stat(process.env.${WAIT_STAT_PATH_ENV} || "", (error) => process.stdout.write(JSON.stringify(error ? { code: String((error && error.code) || "UNKNOWN") } : { exists: true })));`;

/** The check in a packaged single executable, which takes no `-e` (`__wait-stat`). */
export function runWaitStat(): void {
  NodeFS.stat(process.env[WAIT_STAT_PATH_ENV] || "", (error) => {
    process.stdout.write(
      JSON.stringify(error ? { code: String(error.code || "UNKNOWN") } : { exists: true }),
    );
  });
}
