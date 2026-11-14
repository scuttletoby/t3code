/**
 * Fork (waits): an in-memory file system for wait service tests. Every call answers at once, so a
 * test that moves the test clock never races a real disk write.
 */
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";

/** Holds files by path; writes, renames and removes are immediate. */
export const memoryFileSystem = () => {
  const files = new Map<string, string>();
  const missing = FileSystem.makeNoop({});
  let temps = 0;
  const fs = FileSystem.makeNoop({
    exists: (path) => Effect.succeed(files.has(path)),
    readFileString: (path) => {
      const contents = files.get(path);
      return contents === undefined ? missing.readFileString(path) : Effect.succeed(contents);
    },
    writeFileString: (path, contents) => Effect.sync(() => void files.set(path, contents)),
    makeDirectory: () => Effect.void,
    makeTempDirectory: (options) =>
      Effect.sync(() => `${options?.directory ?? "/tmp"}/${options?.prefix ?? ""}${(temps += 1)}`),
    remove: (path) =>
      Effect.sync(() => {
        for (const name of files.keys()) {
          if (name === path || name.startsWith(`${path}/`)) files.delete(name);
        }
      }),
    rename: (from, to) => {
      const contents = files.get(from);
      if (contents === undefined) return missing.rename(from, to);
      return Effect.sync(() => {
        files.delete(from);
        files.set(to, contents);
      });
    },
  });
  return { fs, files };
};
