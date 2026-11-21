"use strict";
// A node --test reporter (fork-ci-tests.cjs adds it to every node test) that writes one JSON line at the end:
// tests that ended, skipped (or todo), failed, started (queued), stopped (queued, never ended), and file_test: node
// reported the file itself as a test, as it does when the file reported none of its own (empty, a top-level `return`,
// a process.exit() before its events reached node). Suites (describe) must end but are not tests, as in node's totals.
const path = require("node:path");

// The test node makes for the file itself: at the top, named by the file's path from the folder node ran in.
function isFile(data) {
  return (
    data.nesting === 0 &&
    typeof data.file === "string" &&
    data.name === path.relative(process.cwd(), data.file)
  );
}

function kind(data) {
  return data.details?.type ?? data.type;
}

module.exports = async function* counts(source) {
  const out = { tests: 0, skipped: 0, failed: 0, started: 0, file_test: false };
  let queued = 0;
  let ended = 0;
  for await (const event of source) {
    const data = event.data ?? {};
    if (event.type === "test:enqueue") {
      if (!isFile(data)) {
        queued += 1;
        if (kind(data) !== "suite") out.started += 1;
      }
    } else if (event.type === "test:pass" || event.type === "test:fail") {
      if (isFile(data)) {
        out.file_test = true;
        if (event.type === "test:fail") out.failed += 1;
        continue;
      }
      ended += 1;
      if (kind(data) === "suite") continue;
      out.tests += 1;
      if (event.type === "test:fail") out.failed += 1;
      else if (data.skip || data.todo) out.skipped += 1;
    }
  }
  out.stopped = queued - ended;
  yield `${JSON.stringify(out)}\n`;
};
