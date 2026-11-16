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

| Kit                              | What                                                                                                    |
| -------------------------------- | ------------------------------------------------------------------------------------------------------- |
| [desk](desk/README.md)           | A desktop's side of a fork fleet desk pool: runner users, walls, desk agent; one folder (Linux, WSL2)   |
| [gh-apps](gh-apps/README.md)     | GitHub App tokens for agents through `gh-as`, without giving agents the keys (Linux)                    |
| [golive](golive/README.md)       | Move a T3 boot service to a new build with a cold copy first; core and `check` so far (Linux)           |
| [heavy](heavy/README.md)         | `heavy <command>`: run builds and tests in a capped slice, stop what they leave behind (Linux, systemd) |
| [pc-agents](pc-agents/README.md) | Run T3's agents on a Windows PC in a walled WSL2 distro, checked before every start (WSL2)              |

[common](common/README.md) is code the kits share, copied into each kit's version folder: the loader of the one
private machine file (`/etc/fork-kit/machine.json`) where the facts of one machine live.
