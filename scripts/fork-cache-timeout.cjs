// Fork: the macOS x64 build warms the server compile cache through Rosetta, which can outrun
// upstream's 120 s on the fork's 3-core Mac runner. prepare-server-compile-cache.cjs wraps its
// spawnSync with withRosetta below, so its own spawnSync line stays as upstream wrote it.
const ROSETTA_TIMEOUT_MS = 360_000;

/** `options` with a timeout of at least 360 s; no timeout (none set, or 0) stays none. */
const withRosettaTimeout = (options) =>
  typeof options?.timeout === "number" &&
  options.timeout > 0 &&
  options.timeout < ROSETTA_TIMEOUT_MS
    ? { ...options, timeout: ROSETTA_TIMEOUT_MS }
    : options;

/** `spawnSync`, with the longer limit when the step runs under Rosetta and as it is otherwise. */
const withRosetta = (spawnSync, runsWithRosetta) =>
  runsWithRosetta
    ? (file, args, options) => spawnSync(file, args, withRosettaTimeout(options))
    : spawnSync;

module.exports = { ROSETTA_TIMEOUT_MS, withRosetta, withRosettaTimeout };
