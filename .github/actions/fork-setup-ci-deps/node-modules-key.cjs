#!/usr/bin/env node
// Fork only: the key, paths and save decision of fork-setup-ci-deps's node_modules cache (action.yml
// says why). Writes `key`, `paths` and `save` to $GITHUB_OUTPUT, or only `save=false` when
// node_modules cannot be cached here. Any lockfile or workspace line it cannot read for sure turns
// the cache off (fail closed); the action then installs from the pnpm store, as upstream's does.
"use strict";

const crypto = require("node:crypto");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

// Bump when what the cache holds changes in a way no hashed file shows.
const VERSION = "v2";
// The action and this script are in the key: a change to how the tree is built is another key.
const OWN_FILES = ["action.yml", "node-modules-key.cjs"];
const INPUTS = [
  "pnpm-lock.yaml",
  "pnpm-workspace.yaml",
  "package.json",
  ".npmrc",
  ".pnpmfile.cjs",
  ".pnpmfile.mjs",
];

class Unsure extends Error {}
const unsure = (message) => {
  throw new Unsure(message);
};

// A YAML mapping key or scalar as pnpm writes it: plain, 'single' ('' escapes) or "double" (no
// escapes). Anything else is unsure.
const SCALAR = String.raw`'(?:[^']|'')*'|"[^"\\]*"|[^\s'"#{}\[\],&*!|>%@\x60][^#]*?`;
const unquote = (text) =>
  text.startsWith("'")
    ? text.slice(1, -1).replace(/''/g, "'")
    : text.startsWith('"')
      ? text.slice(1, -1)
      : text;

// The entries of every top-level `<name>:` block mapping (a lockfile can hold more than one YAML
// document). `line` reads one entry at two spaces' indent; deeper lines belong to the entry above.
function blockEntries(text, name, line) {
  const entries = [];
  let inside = false;
  for (const raw of text.split(/\r?\n/)) {
    if (/^\s*(#.*)?$/.test(raw)) continue;
    if (raw.startsWith("\t")) unsure(`a tab-indented line: \`${raw}\``);
    if (!raw.startsWith(" ")) {
      inside = raw === `${name}:`;
      if (!inside && raw.startsWith(`${name}:`)) {
        if (new RegExp(`^${name}:\\s*\\{\\}\\s*$`).test(raw)) continue;
        unsure(`\`${raw}\` is not a block mapping`);
      }
      continue;
    }
    if (!inside) continue;
    const indent = raw.match(/^ */)[0].length;
    if (indent > 2) continue;
    if (indent !== 2) unsure(`unexpected indent under ${name}: \`${raw}\``);
    const entry = line(raw);
    if (!entry) unsure(`cannot read this ${name} line: \`${raw}\``);
    entries.push(entry);
  }
  return entries;
}

// Every project pnpm installs into, `{}` (an empty importer) too.
const IMPORTER = new RegExp(String.raw`^  (${SCALAR})\s*:(?:\s+\{\})?\s*$`);
function importers(lockfile) {
  const dirs = blockEntries(lockfile, "importers", (raw) => {
    const m = raw.match(IMPORTER);
    return m && unquote(m[1].trim());
  });
  if (!dirs.length) unsure("pnpm-lock.yaml names no importers");
  for (const dir of dirs) {
    if (!dir || path.isAbsolute(dir) || /[\n\\]/.test(dir) || dir.split("/").includes("..")) {
      unsure(`unexpected importer path in pnpm-lock.yaml: ${JSON.stringify(dir)}`);
    }
  }
  return [...new Set(dirs)].sort();
}

// Patch files are applied at install time: pnpm-workspace.yaml's and package.json's
// `patchedDependencies`.
const PATCH = new RegExp(String.raw`^  (${SCALAR})\s*:\s+(${SCALAR})\s*(?:\s#.*)?$`);
function patches(workspaceYaml, packageJson) {
  const files = blockEntries(workspaceYaml ?? "", "patchedDependencies", (raw) => {
    const m = raw.match(PATCH);
    return m && unquote(m[2].trim());
  });
  if (packageJson !== undefined) {
    let manifest;
    try {
      manifest = JSON.parse(packageJson);
    } catch {
      unsure("package.json is not JSON");
    }
    const listed = manifest?.pnpm?.patchedDependencies ?? {};
    for (const file of Object.values(listed)) {
      if (typeof file !== "string")
        unsure("package.json pnpm.patchedDependencies is not a map of paths");
      files.push(file);
    }
  }
  for (const file of files) {
    if (!file || /[\n\\]/.test(file)) unsure(`unexpected patch path: ${JSON.stringify(file)}`);
  }
  return files;
}

// Whether this run may save: only a push to the default branch that tests the pushed commit. Any
// other run (a pull request, a dispatch or call with another ref, a schedule) only restores.
function saveDecision(env, head) {
  if (env.GITHUB_EVENT_NAME !== "push") return [false, `event ${env.GITHUB_EVENT_NAME}`];
  if (!env.DEFAULT_BRANCH || env.GITHUB_REF !== `refs/heads/${env.DEFAULT_BRANCH}`) {
    return [false, `ref ${env.GITHUB_REF} is not the default branch`];
  }
  if (!env.GITHUB_SHA || head !== env.GITHUB_SHA) {
    return [false, `the checkout (${head}) is not the pushed commit (${env.GITHUB_SHA})`];
  }
  return [true, "a push to the default branch, testing the pushed commit"];
}

const sha256 = (data) => crypto.createHash("sha256").update(data).digest("hex");

// { key, paths, save, why } or { skip }. read(file) gives a file's bytes or undefined.
function compute({ env, read, cwd, ownDir, head, osRelease }) {
  try {
    // Fleet runners give each job its own workspace and store path ($WORK_ROOT/<pool>-<job id>):
    // a key there would never repeat, and each save would be a one-time ~0.75 GB entry.
    if (env.RUNNER_ENVIRONMENT !== "github-hosted") {
      unsure(`the runner is ${env.RUNNER_ENVIRONMENT || "unknown"}, not github-hosted`);
    }
    const lockfile = read("pnpm-lock.yaml");
    if (lockfile === undefined) unsure("no pnpm-lock.yaml");
    const workspaceYaml = read("pnpm-workspace.yaml")?.toString();
    const npmrc = read(".npmrc")?.toString() ?? "";
    if (/pnpmfile/i.test(`${workspaceYaml ?? ""}\n${npmrc}`)) {
      unsure("a pnpmfile is configured by path");
    }
    const dirs = importers(lockfile.toString());
    const inputs = [...INPUTS];
    const paths = [];
    for (const dir of dirs) {
      if (dir === ".") {
        paths.push("node_modules");
      } else {
        paths.push(`${dir}/node_modules`);
        inputs.push(`${dir}/package.json`);
      }
    }
    inputs.push(...patches(workspaceYaml, read("package.json")?.toString()));

    // pnpm's own settings from the environment change what it installs.
    const settings = Object.keys(env)
      .filter((name) => /^(npm|pnpm)_config_|^NODE_ENV$/i.test(name))
      .sort()
      .map((name) => `env ${name}=${env[name]}`);
    // .modules.yaml holds the store path, and native builds depend on the OS release.
    const lines = [
      `fork-node-modules ${VERSION}`,
      `os ${osRelease}`,
      `workspace ${cwd}`,
      `store ${env.STORE_PATH ?? ""}`,
      `vp ${env.VP_VERSION ?? ""}`,
      `node ${env.NODE_VERSION ?? ""}`,
      `pnpm ${env.PNPM ?? ""}`,
      `namespace ${env.NAMESPACE ?? ""}`,
      ...settings,
      ...OWN_FILES.map((file) => {
        const bytes = fs.readFileSync(path.join(ownDir, file));
        return `own ${sha256(bytes)} ${file}`;
      }),
      ...[...new Set(inputs)].sort().map((file) => {
        const bytes = read(file);
        return bytes === undefined ? `absent ${file}` : `${sha256(bytes)} ${file}`;
      }),
    ];
    const [save, why] = saveDecision(env, head);
    const key = [
      `fork-node-modules-${VERSION}`,
      env.NAMESPACE || "full",
      env.RUNNER_OS ?? "",
      env.RUNNER_ARCH ?? "",
      `node${env.NODE_VERSION ?? ""}`,
      env.PNPM ?? "",
      sha256(`${lines.join("\n")}\n`),
    ].join("-");
    return { key, paths, save, why };
  } catch (error) {
    if (error instanceof Unsure) return { skip: error.message };
    throw error;
  }
}

function osRelease() {
  try {
    const text = fs.readFileSync("/etc/os-release", "utf8");
    const field = (name) => text.match(new RegExp(`^${name}=("?)(.*)\\1$`, "m"))?.[2] ?? "";
    return `${field("ID")}-${field("VERSION_ID")}`;
  } catch {
    const os = require("node:os");
    return `${os.type()}-${os.release()}`;
  }
}

function main() {
  const out = process.env.GITHUB_OUTPUT;
  if (!out) throw new Error("GITHUB_OUTPUT is not set");
  const cwd = process.cwd();
  let head = "";
  try {
    head = execFileSync("git", ["rev-parse", "HEAD"], { cwd, encoding: "utf8" }).trim();
  } catch {}
  const result = compute({
    env: process.env,
    read: (file) => {
      try {
        return fs.readFileSync(path.join(cwd, file));
      } catch (error) {
        if (error.code === "ENOENT") return undefined;
        throw error;
      }
    },
    cwd,
    ownDir: __dirname,
    head,
    osRelease: osRelease(),
  });
  if (result.skip) {
    console.log(`node_modules is not cached: ${result.skip}`);
    fs.appendFileSync(out, "save=false\n");
    return;
  }
  console.log(
    `node_modules key ${result.key}; ${result.save ? "saves" : "restores only"}: ${result.why}`,
  );
  fs.appendFileSync(
    out,
    `key=${result.key}\nsave=${result.save}\npaths<<FORK_NODE_MODULES_PATHS\n${result.paths.join("\n")}\nFORK_NODE_MODULES_PATHS\n`,
  );
}

module.exports = { compute, importers, patches, saveDecision, VERSION };

if (require.main === module) main();
