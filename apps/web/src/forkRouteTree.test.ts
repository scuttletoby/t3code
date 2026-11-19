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
  readonly getConfig: (input: Readonly<Record<string, string>>, root: string) => unknown;
}

const web = NodePath.join(import.meta.dirname, "..");
const repo = NodePath.join(web, "../..");
const git = (...args: string[]) =>
  NodeChildProcess.execFileSync("git", ["-C", repo, ...args], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });

// Fork-only. The seam check does not judge routeTree.gen.ts line by line when a route file changes
// (.github/scripts/fork-seam-check.cjs, GENERATED), so this test is what refuses a hand edit there.
// It reads both from the commit: every vite run (this test's too) rewrites the working tree's copy.
it("the committed routeTree.gen.ts is what TanStack's generator writes for the committed routes", async () => {
  // The generator is the router plugin's own dependency, the version dev and builds run.
  const fromPlugin = NodeModule.createRequire(
    NodeModule.createRequire(NodePath.join(web, "package.json")).resolve(
      "@tanstack/router-plugin/package.json",
    ),
  );
  const { Generator, getConfig } = fromPlugin("@tanstack/router-generator") as RouteGenerator;
  const root = NodeFS.mkdtempSync(NodePath.join(NodeOS.tmpdir(), "fork-route-tree-"));
  try {
    const routes = git(
      "ls-tree",
      "-r",
      "-z",
      "--name-only",
      "--full-tree",
      "HEAD",
      "apps/web/src/routes/",
    )
      .split("\0")
      .filter(Boolean);
    expect(routes.length).toBeGreaterThan(0);
    for (const file of routes) {
      const target = NodePath.join(root, NodePath.relative("apps/web", file));
      NodeFS.mkdirSync(NodePath.dirname(target), { recursive: true });
      NodeFS.writeFileSync(target, git("show", `HEAD:${file}`));
    }
    const config = getConfig(
      {
        routesDirectory: "./src/routes",
        generatedRouteTree: "./src/routeTree.gen.ts",
        tmpDir: NodePath.join(root, "tmp"),
      },
      root,
    );
    await new Generator({ config, root }).run();
    expect(NodeFS.readFileSync(NodePath.join(root, "src/routeTree.gen.ts"), "utf8")).toBe(
      git("show", "HEAD:apps/web/src/routeTree.gen.ts"),
    );
  } finally {
    NodeFS.rmSync(root, { recursive: true, force: true });
  }
});
