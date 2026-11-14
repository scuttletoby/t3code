# fork-kit: things this fork installs on a host

Each folder here is one kit: code that runs on a machine (not in CI and not inside T3), with its own installer,
self-check and tests. Kits are general: nothing in them names a project, person or machine; those facts are
options the owner passes at install time, or files the installer writes on that host.

Every kit follows the same pattern:

- installed only from a commit on the fork's protected release branch (`ci-release`), by a small bootstrap that
  checks this first and prints the sha and hashes;
- code in a root-owned folder, one sub-folder per version with a hash manifest, switched by one link;
- an install record (`installed.tsv`) and a `--self-check` that reports drift;
- safe to run again, and safe to stop at any step;
- an uninstall that leaves nothing.

| Kit | What |
|---|---|
| [gh-apps](gh-apps/README.md) | GitHub App tokens for agents through `gh-as`, without giving agents the keys (Linux) |
