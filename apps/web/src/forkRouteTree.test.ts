// @effect-diagnostics nodeBuiltinImport:off - runs TanStack's route generator on the committed routes.
import * as NodeChildProcess from "node:child_process";
import * as NodeFS from "node:fs";
import * as NodeModule from "node:module";
import * as NodeOS from "node:os";
import * as NodePath from "node:path";
import { expect, it } from "vite-plus/test";

interface RouteGenerator {
  readonly Generator: new (options: { config: unknown; root: string }) => {
    run(): Promise<void>;
  };
}
interface RouterPlugin {
  readonly getConfig: (
    input: Readonly<Record<string, unknown>>,
    root: string,
  ) => { readonly routesDirectory: string; readonly generatedRouteTree: string };
}
interface SeamCheck {
  readonly GENERATED: ReadonlyMap<string, { readonly source: string; readonly proof: string }>;
}

const web = NodePath.join(import.meta.dirname, "..");
const repo = NodePath.join(web, "../..");
const git = (...args: string[]) =>
  NodeChildProcess.execFileSync("git", ["-C", repo, ...args], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
const committed = (file: string) =>
  git("ls-tree", "--name-only", "--full-tree", "HEAD", file).trim() === file
    ? git("show", `HEAD:${file}`)
    : undefined;

// The options vite.config.ts passes to tanstackRouter(...): dev and builds generate the tree with
// them, so the test must too. Fails when the call is gone, repeated or not a plain literal.
function viteRouterOptions(): Record<string, unknown> {
  const source = committed("apps/web/vite.config.ts") ?? "";
  const calls = [...source.matchAll(/\btanstackRouter\(/g)];
  const unreadable = (why: string) =>
    new Error(
      `apps/web/vite.config.ts: ${why}; teach forkRouteTree.test.ts to read the router plugin's options again.`,
    );
  if (calls.length !== 1) throw unreadable(`${calls.length} tanstackRouter( calls, not 1`);
  const start = calls[0]!.index + calls[0]![0].length;
  let depth = 1;
  let end = start;
  while (depth > 0 && end < source.length) {
    const char = source[end++];
    if (char === "(") depth += 1;
    if (char === ")") depth -= 1;
  }
  const text = source.slice(start, end - 1).trim();
  if (text === "") return {};
  try {
    return new Function(`"use strict"; return (${text});`)() as Record<string, unknown>;
  } catch (error) {
    throw unreadable(`its tanstackRouter(${text}) options are not a plain literal (${error})`);
  }
}

// Fork-only. The seam check does not judge routeTree.gen.ts line by line when a route file changes
// (.github/scripts/fork-seam-check.cjs, GENERATED), so this test is what refuses a hand edit there.
// It reads both from the commit: every vite run (this test's too) rewrites the working tree's copy.
it("the committed routeTree.gen.ts is what TanStack's generator writes for the committed routes", async () => {
  // The plugin's own config reader and generator: the versions dev and builds run.
  const fromWeb = NodeModule.createRequire(NodePath.join(web, "package.json"));
  const { getConfig } = fromWeb("@tanstack/router-plugin") as RouterPlugin;
  const fromPlugin = NodeModule.createRequire(
    fromWeb.resolve("@tanstack/router-plugin/package.json"),
  );
  const { Generator } = fromPlugin("@tanstack/router-generator") as RouteGenerator;
  const { GENERATED } = NodeModule.createRequire(import.meta.filename)(
    NodePath.join(repo, ".github/scripts/fork-seam-check.cjs"),
  ) as SeamCheck;
  const root = NodeFS.mkdtempSync(NodePath.join(NodeOS.tmpdir(), "fork-route-tree-"));
  try {
    // The generator also reads apps/web/tsr.config.json, as it does under vite.
    const tsrConfig = committed("apps/web/tsr.config.json");
    if (tsrConfig !== undefined)
      NodeFS.writeFileSync(NodePath.join(root, "tsr.config.json"), tsrConfig);
    const config = getConfig({ ...viteRouterOptions(), tmpDir: NodePath.join(root, "tmp") }, root);
    const inRepo = (file: string) =>
      NodePath.posix.join("apps/web", NodePath.relative(root, file).split(NodePath.sep).join("/"));
    const source = `${inRepo(config.routesDirectory)}/`;
    const tree = inRepo(config.generatedRouteTree);
    // The seam check's table must name the same files, or it skips the wrong ones.
    expect(GENERATED.get(tree)).toEqual({ source, proof: "apps/web/src/forkRouteTree.test.ts" });
    const routes = git("ls-tree", "-r", "-z", "--name-only", "--full-tree", "HEAD", source)
      .split("\0")
      .filter(Boolean);
    expect(routes.length).toBeGreaterThan(0);
    for (const file of routes) {
      const target = NodePath.join(root, NodePath.relative("apps/web", file));
      NodeFS.mkdirSync(NodePath.dirname(target), { recursive: true });
      NodeFS.writeFileSync(target, git("show", `HEAD:${file}`));
    }
    await new Generator({ config, root }).run();
    expect(NodeFS.readFileSync(config.generatedRouteTree, "utf8")).toBe(
      git("show", `HEAD:${tree}`),
    );
  } finally {
    NodeFS.rmSync(root, { recursive: true, force: true });
  }
});
