// Fork only: fails when an upstream composite action the fork copies changed and its copy's
// `# upstream-blob: <id>` line (git blob id of the upstream file it follows) was not updated, or a
// fork workflow uses the upstream action. Port the change into the copy first, then `--record`.
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

// upstream: the action upstream owns (unchanged in the fork). copy: the fork's copy of it.
const COPIES = [
  {
    upstream: ".github/actions/setup-ci-deps/action.yml",
    copy: ".github/actions/fork-setup-ci-deps/action.yml",
  },
];

const RECORD = /^# upstream-blob: ([0-9a-f]{40})$/gm;

/** The id `git hash-object` gives these bytes. */
const blobId = (bytes) =>
  crypto
    .createHash("sha1")
    .update(Buffer.concat([Buffer.from(`blob ${bytes.length}\0`), bytes]))
    .digest("hex");

// `uses: ./.github/actions/<name>` (optionally quoted), the form a workflow names a local action by.
const usesPattern = (dir) =>
  new RegExp(
    `^\\s*(?:-\\s+)?uses:\\s*["']?\\./${dir.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/?["']?\\s*(?:#.*)?$`,
    "m",
  );

/**
 * Problems for `root` (one line each, empty when all is well). `read(file)` gives a file's bytes or
 * undefined; `workflows` the fork workflow paths to check.
 */
function check({ read, workflows, copies = COPIES }) {
  const problems = [];
  for (const { upstream, copy } of copies) {
    const upstreamBytes = read(upstream);
    const copyBytes = read(copy);
    if (upstreamBytes === undefined) {
      problems.push(
        `${upstream} is gone (upstream removed or moved it): check what upstream did, follow it in ${copy}, and update COPIES in .github/scripts/fork-setup-copy-check.cjs.`,
      );
      continue;
    }
    if (copyBytes === undefined) {
      problems.push(`${copy} is missing: the fork's copy of ${upstream}.`);
      continue;
    }
    const recorded = [...copyBytes.toString("utf8").matchAll(RECORD)].map((m) => m[1]);
    const now = blobId(upstreamBytes);
    if (recorded.length !== 1) {
      problems.push(
        `${copy} must have exactly one "# upstream-blob: <40-hex id>" line (found ${recorded.length}); it records which ${upstream} the copy follows (now ${now}).`,
      );
    } else if (recorded[0] !== now) {
      problems.push(
        `${upstream} changed upstream (copy follows blob ${recorded[0]}, the tree has ${now}). ` +
          `Port the change into ${copy} first (see it with \`git diff ${recorded[0]} ${now}\`, ` +
          `or \`git log -p -- ${upstream}\`), then run \`node .github/scripts/fork-setup-copy-check.cjs --record\`. ` +
          "Do not just record: the fork's workflows run the copy, not upstream's file.",
      );
    }
    const upstreamDir = path.posix.dirname(upstream);
    for (const workflow of workflows) {
      const text = read(workflow)?.toString("utf8") ?? "";
      if (usesPattern(upstreamDir).test(text)) {
        problems.push(
          `${workflow} uses ./${upstreamDir}: fork workflows use the fork's copy, ./${path.posix.dirname(copy)}.`,
        );
      }
    }
  }
  return problems;
}

/** Rewrites each copy's record to its upstream file's current id; returns what changed. */
function record({ read, write, copies = COPIES }) {
  const changed = [];
  for (const { upstream, copy } of copies) {
    const upstreamBytes = read(upstream);
    const copyBytes = read(copy);
    if (upstreamBytes === undefined || copyBytes === undefined) continue;
    const now = blobId(upstreamBytes);
    const text = copyBytes.toString("utf8");
    const next = text.replace(RECORD, `# upstream-blob: ${now}`);
    if (next !== text) {
      write(copy, next);
      changed.push(`${copy}: upstream-blob ${now}`);
    }
  }
  return changed;
}

function fromTree(root) {
  const read = (file) => {
    try {
      return fs.readFileSync(path.join(root, file));
    } catch (error) {
      if (error.code === "ENOENT") return undefined;
      throw error;
    }
  };
  const dir = path.join(root, ".github/workflows");
  const workflows = fs
    .readdirSync(dir)
    .filter((name) => /^fork-.*\.ya?ml$/.test(name))
    .sort()
    .map((name) => `.github/workflows/${name}`);
  return { read, workflows, write: (file, text) => fs.writeFileSync(path.join(root, file), text) };
}

if (require.main === module) {
  const root = path.resolve(__dirname, "../..");
  const tree = fromTree(root);
  if (process.argv.includes("--record")) {
    const changed = record(tree);
    console.log(changed.length ? changed.join("\n") : "Every copy's record is current.");
  }
  const problems = check(tree);
  if (problems.length) {
    for (const problem of problems) console.error(`::error::${problem}`);
    process.exit(1);
  }
  console.log(`Fork copies follow upstream: ${COPIES.map((c) => c.copy).join(", ")}.`);
}

module.exports = { COPIES, blobId, check, record, fromTree };
