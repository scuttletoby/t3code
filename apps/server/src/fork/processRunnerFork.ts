/**
 * Fork: the fork's parts of the process runner (processRunner.ts), kept here so that upstream file
 * holds only the lines that call them.
 */
import * as Effect from "effect/Effect";
import * as Stream from "effect/Stream";
import {
  decodeUtf8,
  type CollectedUint8StreamText,
} from "@t3tools/provider-core/server/collectStreamText";

/** Playtest: the last `maxBytes` of a stream, where a failing step prints its error. */
export const collectTail = <E>(stream: Stream.Stream<Uint8Array, E>, maxBytes: number) =>
  stream.pipe(
    Stream.runFold(
      () => ({ chunks: [] as Uint8Array[], bytes: 0, dropped: false }),
      (state, chunk) => {
        state.chunks.push(chunk);
        let bytes = state.bytes + chunk.byteLength;
        let dropped = state.dropped;
        // Whole chunks only; the cut inside the first one is made at the end.
        while (state.chunks.length > 1 && bytes - state.chunks[0]!.byteLength >= maxBytes) {
          bytes -= state.chunks.shift()!.byteLength;
          dropped = true;
        }
        return { chunks: state.chunks, bytes, dropped };
      },
    ),
    Effect.map((state): CollectedUint8StreamText => {
      let kept = Buffer.concat(state.chunks, state.bytes);
      const truncated = state.dropped || kept.byteLength > maxBytes;
      if (kept.byteLength > maxBytes) kept = kept.subarray(kept.byteLength - maxBytes);
      // Not from the middle of a character: skip UTF-8 continuation bytes at the cut.
      let start = 0;
      if (truncated) {
        while (start < kept.byteLength && start < 3 && (kept[start]! & 0xc0) === 0x80) start += 1;
      }
      return { ...decodeUtf8(kept.subarray(start)), bytes: kept.byteLength - start, truncated };
    }),
  );

/**
 * True when `error` (or a cause under it) is EPIPE: the child closed its stdin before the runner
 * wrote it, e.g. `timeout` exiting 127 at once for a missing program. Not a failure to run it;
 * its exit code and output say how it ended.
 */
export function isClosedStdin(error: unknown): boolean {
  let current: unknown = error;
  for (let depth = 0; depth < 6 && typeof current === "object" && current !== null; depth += 1) {
    if ((current as { readonly code?: unknown }).code === "EPIPE") return true;
    const reason = (current as { readonly reason?: unknown }).reason;
    current =
      typeof reason === "object" && reason !== null && "cause" in reason
        ? (reason as { readonly cause: unknown }).cause
        : (current as { readonly cause?: unknown }).cause;
  }
  return false;
}
