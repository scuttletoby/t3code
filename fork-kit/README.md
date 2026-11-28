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

| Kit                                          | What                                                                                                                                                                                                                       |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [agent-guards](agent-guards/README.md)       | Claude Code hooks that guard every agent on a machine: no disabling Actions, no piped capacity check, a memory index in budget, no stop with a background command, agent slots (Linux)                                     |
| [cleaner](cleaner/README.md)                 | `fork-cleaner`: free disk space by rules (`worktrees` with merged branches, `tmp`, `agent-scratch`, `docker`; `compress-cold` and `dedupe` delete no content) and never what could be needed; daily timer (Linux, systemd) |
| [desk](desk/README.md)                       | A desktop's side of a fork fleet desk pool: runner users, walls, desk agent; one folder (Linux, WSL2)                                                                                                                      |
| [gardener](gardener/README.md)               | `gardener`: each project's checks once a day (small scripts, JSON-line findings; general ones shipped, project ones by path), one report issue per project updated in place; report only (Linux, systemd)                  |
| [gh-apps](gh-apps/README.md)                 | GitHub App tokens for agents through `gh-as`, without giving agents the keys (Linux)                                                                                                                                       |
| [golive](golive/README.md)                   | Move a T3 boot service to a new build with a cold copy first, and back; installs from `ci-release` (Linux)                                                                                                                 |
| [heavy](heavy/README.md)                     | `heavy <command>`: run builds and tests in a capped slice, stop what they leave behind (Linux, systemd)                                                                                                                    |
| [homelab](homelab/README.md)                 | `homelab check` of the private machine file's machines, and test-machine leases any project can take; from a checkout for now (Linux, macOS)                                                                               |
| [model-guard](model-guard/README.md)         | A Claude Code hook that keeps agents' model picks (T3 tools and `Agent`) inside a policy file (Linux)                                                                                                                      |
| [pc-agents](pc-agents/README.md)             | Run T3's agents on a Windows PC in a walled WSL2 distro, checked before every start (WSL2)                                                                                                                                 |
| [placement-facts](placement-facts/README.md) | Example `factsCommand` programs for placement (Linux, macOS, Windows): busy file, busy programs, battery, `gpu`, `ci-runner`; no installer, copied by hand                                                                 |
| [review](review/README.md)                   | A review skill for the merge gate: lenses picked by changed paths, the state question, a comment format checked against the gate's parser, a model eval; no installer, used from a checkout                                |
| [schedule-kick](schedule-kick/README.md)     | `schedule-kick`: start scheduled GitHub workflows on time by `workflow_dispatch` when no run exists for the slot; timer every 5 minutes (Linux, systemd)                                                                   |
| [slot-filler](slot-filler/README.md)         | `slot-filler`: start queued agent tasks (writers, lenses rendered when CI goes green) into free slots per machine, within caps, capacity and usage meters; timer every minute (Linux, systemd)                             |
| [update-policy](update-policy/README.md)     | Restart only when idle at the quiet hour (dev, personal), alert only (live); daily baseline and exposure (KEV, EPSS) checks that report only drift and what needs action (Linux, Debian/Ubuntu, systemd)                   |

[common](common/README.md) is code the kits share, copied into each kit's version folder: the loader of the one
private machine file (`/etc/fork-kit/machine.json`) where the facts of one machine live, and the installer and
bootstrap of the agent-guards, cleaner, gardener, golive, schedule-kick and update-policy kits.

[review](review/README.md) and [placement-facts](placement-facts/README.md) have no installer: review is a skill,
docs and tools an agent uses from a checkout of the fork, beside the merge gate whose parser it imports;
placement-facts holds example scripts a person copies and edits.
