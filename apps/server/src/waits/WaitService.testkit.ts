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
  const decoder = new TextDecoder();
  let temps = 0;
  // A handle opened to write starts the file empty; each write appends; sync has nothing to flush.
  const handle = (path: string, writable: boolean) => {
    if (writable) files.set(path, "");
    const append = (buffer: Uint8Array) =>
      Effect.sync(() => void files.set(path, (files.get(path) ?? "") + decoder.decode(buffer)));
    return {
      [FileSystem.FileTypeId]: FileSystem.FileTypeId,
      sync: Effect.void,
      writeAll: append,
      write: (buffer: Uint8Array) => Effect.as(append(buffer), buffer.length),
    } as unknown as FileSystem.File;
  };
  const fs = FileSystem.makeNoop({
    exists: (path) => Effect.succeed(files.has(path)),
    // Atomic saves write through one handle, keep the replaced file's mode and sync the directory.
    open: (path, options) => Effect.succeed(handle(path, options?.flag?.startsWith("w") ?? false)),
    chmod: () => Effect.void,
    stat: (path) =>
      files.has(path)
        ? Effect.succeed({ type: "File", mode: 0o600 } as FileSystem.File.Info)
        : missing.stat(path),
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
