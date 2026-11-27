# About this fork

Fork only. This page says what the fork is, how to install and update it, what it adds today and
what is planned. Every change, one line each, is in [FORK_CHANGES.md](../../FORK_CHANGES.md); every
setting with its default is in [README.md](README.md).

## What it is

This fork is [T3 Code](https://github.com/pingdotgg/t3code) with a thin set of changes on top, for
people who run many coding agents on their own machines and plan limits.

- **It follows upstream.** Once a day, fork-follow rebases the fork's changes onto upstream's newest
  release tag, runs the tests, and releases only when they pass. On trouble it opens one issue
  instead.
- **Versions** are upstream's version plus `.fork.<n>`, for example
  `v0.1.12-nightly.20261121.5051.fork.1`: the first fork release built on that upstream release.
- **Off by default.** A machine whose settings never name a fork setting behaves like upstream's
  T3 Code, except that it looks for updates in the fork's releases. Every fork switch starts off; the
  one exception only reads usage limits.
- **Thin.** Fork code lives in its own files where it can, so following upstream stays cheap.

## Install and update

See [Install the fork today](README.md#install-the-fork-today): a fork release (Linux x64 CLI
archive with the install script, Windows x64 CLI archive, unsigned desktop apps), or a build from
source. A release build's `t3 update` takes only the fork's releases. A server that starts with the
computer (a boot service) can also update itself (**Settings → Fork → Automatic updates**, below). A machine that already runs
T3 as a boot service moves over with the go-live kit ([go-live.md](go-live.md)).

## What it adds

Most settings are on the app's **Settings → Fork** page, grouped by what you want to do. The top row,
**What the fork has on**, lists every feature with on or off. "Settings file only" means the setting
has no row yet (usage history, busy check, placement, thread roles): set it in `settings.json`
([where settings live](README.md#where-settings-live)).

**Plan usage and machine load**

- Plan usage pacing: keeps a share of each plan's usage window free for you and paces agents evenly
  across the window. Settings → Fork → Plan usage pacing.
- Usage history: the server records each account's usage-limit readings and reads them every 10
  minutes while no app is open. On by default (read only); settings file only
  (`headlessUsageRefreshInterval`).
- Limit on running agents: a new sub-agent is refused above a number of running agents per machine
  or account, or below a free-memory floor, and can wait for a free slot. Settings → Fork → Limit on
  running agents.
- Stop finished sub-agents: a delegated sub-agent's process stops once its task is over and it has
  been idle a short while. Settings → Fork → Stop finished sub-agents.
- Machine facts: `t3_environment_read` also returns the machine's CPU, cores and memory. Always on.

**Waiting instead of polling**

- Agent waits: an agent waits for a pull request, a file, a system unit, a custom check or a free
  agent slot, ends its turn, and is woken once when it happens. Settings → Fork → Agent waits.
- Custom wait checks: named commands an agent can wait on, per machine or per project. Settings →
  Fork → Custom wait checks.
- Delegated results: `task_list` lists a thread's delegated tasks whose result it has not read, and
  its running sub-agents. Always on.

**Models and tools agents get**

- Models agents may use: which models and efforts agents may pick, as advice or enforced. Settings
  → Fork → Models agents may use.
- Tool profiles: named sets of outside tool servers (MCP) and T3 tool groups, with one for new
  threads, one for sub-agents, and a pick per thread. Settings → Fork → Tool profiles.

**Long threads**

- Handoff instead of compaction: a big or idle thread hands its work to a fresh session in the same
  thread, with a short handoff note. Settings → Fork → Handoff instead of compaction
  ([handoff.md](handoff.md)).

**Reviews**

- Review rules: how this machine runs a review lens; the Recommended preset turns the fork's defaults
  on. While on, agents can start the lenses a pull request still needs (`lens_start`). Settings →
  Fork → Review rules.

**Assistant and your threads**

- Assistant: Settings offers **Make assistant**, one thread that can reach every project.
  Settings → Fork → Assistant ([assistant.md](assistant.md)).
- Thread roles: which thread is the assistant and which is each project's orchestrator. Only you
  set them (**Make assistant** sets the assistant); the app does not show them yet. Settings file
  only (`threadRoles`).
- Waiting on you: the Issues entry lists the open owner questions of every project on every
  connected server, read only. Settings → Fork → Issues page turns it off for upstream's Issues page
  (on by default).

**Playtesting**

- Playtest sessions: an agent claims a test place, pushes its build there in steps, and releases it,
  using commands only you set. Settings → Fork → Playtest sessions ([playtest.md](playtest.md)).

**Updates and machines**

- Fork releases: the server, CLI and desktop app look for updates in the fork's releases. Always on.
- Automatic updates: a server that starts with the computer updates itself once no thread is working, after an
  optional "is it quiet?" program agrees. Settings → Fork → Automatic updates
  ([updates.md](updates.md)).
- `t3 serve --hold-work`: try a build on a copy of a T3 home with all agent work held.
- Fork report: `t3 __fork-report --base-dir <home>` prints which fork features are on, and where,
  without changing anything; `--activity` says whether T3 is busy.
- Busy check: a local read-only "is T3 busy?" answer. Settings file only (`activityProbe`).
- Placement: labels for which machine may run which work. Settings file only (`placement`,
  `placementNeeds`); not acted on yet.
- Run records: one line per finished agent run (model, effort, times, tokens) and a weekly summary
  ([run-records.md](run-records.md)). Always on.
- Delegate prepare: a script merges the base, installs and runs the baseline tests of the touched
  files before a delegated agent starts, by the project's `.t3code/prepare.json`
  ([delegate-prepare.md](delegate-prepare.md)). Run by hand or as a project's worktree setup script.
- File links: a file link in chat opens in the files viewer or an editor, or downloads, also from
  another linked machine. Always on.
- Linked machines: a task delegated to another linked machine is still followed if the call is cut
  off, and reports what its thread there waits on. Always on.

**Repository, CI and releases** (for people who build the fork)

- fork-follow and the fork release workflow: the daily rebase and `.fork.<n>` releases.
- Fleet runners: one-job CI runners started on demand on your own machines, each behind walls.
- fork-ci: CI actions any repository can pin, a pull request queue, and a head gate posted by a
  GitHub App ([`fork-ci/README.md`](../../fork-ci/README.md)).
- Host kits: GitHub App tokens for agents, capped build slices, the go-live kit, opt-in PC updates,
  agent guard hooks, a model guard, a disk cleaner, on-time scheduled workflows, an update and
  security baseline policy, and a review skill ([`fork-kit/README.md`](../../fork-kit/README.md)).
- Guards: checks that keep fork lines inside agreed places in upstream files, refuse schema changes,
  scan for secrets and keep outside pull requests from running anything.

## Planned

From the fork's open issues, grouped. Each is one line; the issue has the detail.

**Plan usage and machine load**

- Account failover: move work to the next account before one runs out
  ([#881](https://github.com/scuttletoby/t3code/issues/881)).
- Dynamic agent slots: slot counts come from the machine, not from instructions
  ([#882](https://github.com/scuttletoby/t3code/issues/882)); one source for counting them
  ([#849](https://github.com/scuttletoby/t3code/issues/849)).
- Placement: rules decide which machine runs work
  ([#883](https://github.com/scuttletoby/t3code/issues/883), what blocks turning it on:
  [#902](https://github.com/scuttletoby/t3code/issues/902)).
- Machine loss: drain a machine, tell the parent, move threads
  ([#884](https://github.com/scuttletoby/t3code/issues/884)).
- Memory reservations that hold only what is used
  ([#861](https://github.com/scuttletoby/t3code/issues/861)).
- Hard budgets and a loop detector, so unattended work stops itself
  ([#860](https://github.com/scuttletoby/t3code/issues/860)).
- Waste report: usage, CI minutes and idle memory made visible
  ([#871](https://github.com/scuttletoby/t3code/issues/871)).

**Models, tools and reviews**

- Models agents may use: the server-side backstop, sub-agents and handover
  ([#876](https://github.com/scuttletoby/t3code/issues/876)).
- Tool profiles: the remaining T3 tool groups, Codex and optional narrowing
  ([#875](https://github.com/scuttletoby/t3code/issues/875)).
- Review rules: the rest of the review runner
  ([#888](https://github.com/scuttletoby/t3code/issues/888)); one review kit with thin project
  adapters ([#1011](https://github.com/scuttletoby/t3code/issues/1011)).
- Check a sub-agent's final report against the real pull request and CI
  ([#810](https://github.com/scuttletoby/t3code/issues/810)).

**Threads and the app**

- Handoff: an agent-written note, a thread menu entry and more
  ([#877](https://github.com/scuttletoby/t3code/issues/877)).
- Calm sidebar: show only the assistant and the project orchestrators
  ([#878](https://github.com/scuttletoby/t3code/issues/878)).
- Personal assistant across projects ([#879](https://github.com/scuttletoby/t3code/issues/879)).
- Playtest: a thread chip with an End button
  ([#889](https://github.com/scuttletoby/t3code/issues/889)) and what blocks turning it on
  ([#901](https://github.com/scuttletoby/t3code/issues/901)).
- Waiting on you: the Issues page opens it
  ([#887](https://github.com/scuttletoby/t3code/issues/887)).
- Every question, approval or sign-in from any machine shows up in the thread you read
  ([#808](https://github.com/scuttletoby/t3code/issues/808)).
- Open and download files that live on another linked machine
  ([#1046](https://github.com/scuttletoby/t3code/issues/1046)).
- Guaranteed wake-ups from one mechanism ([#852](https://github.com/scuttletoby/t3code/issues/852));
  a red or conflicted auto-merge pull request wakes its thread
  ([#880](https://github.com/scuttletoby/t3code/issues/880)).
- Orchestrator state and logic as fork features, not instructions
  ([#851](https://github.com/scuttletoby/t3code/issues/851),
  [#870](https://github.com/scuttletoby/t3code/issues/870)).
- `delegate_task` runs the prepare itself and puts its result in the child's prompt
  ([#991](https://github.com/scuttletoby/t3code/issues/991)).

**Updates, machines and safety**

- Updates in each machine's own hours, only when quiet, and a view of machines that are behind
  ([#886](https://github.com/scuttletoby/t3code/issues/886)).
- The rest of the security baseline on every machine: automatic updates, exposure and hardening checks
  ([#1045](https://github.com/scuttletoby/t3code/issues/1045)).
- Host tools: agents use your other computers safely
  ([#885](https://github.com/scuttletoby/t3code/issues/885)).
- Leak guard: refuse posts and pushes with personal details
  ([#892](https://github.com/scuttletoby/t3code/issues/892)).
- One general way to run, list and restart services and on-demand jobs
  ([#994](https://github.com/scuttletoby/t3code/issues/994)).
- Capacity rules enforced by the tools ([#866](https://github.com/scuttletoby/t3code/issues/866)).

**CI and repository tooling**

- The roadmap of every known gap, grouped by goal
  ([#873](https://github.com/scuttletoby/t3code/issues/873)); general tooling moves into the fork
  ([#834](https://github.com/scuttletoby/t3code/issues/834)).
- One merge queue and merge command ([#848](https://github.com/scuttletoby/t3code/issues/848));
  batched merges and shared build caches ([#862](https://github.com/scuttletoby/t3code/issues/862)).
- Runners: one routing, one sandbox ([#853](https://github.com/scuttletoby/t3code/issues/853)); a
  general test-lane pool ([#890](https://github.com/scuttletoby/t3code/issues/890)).
- A required check a pull request cannot rewrite
  ([#34](https://github.com/scuttletoby/t3code/issues/34)).
- Upstream sync for any fork, from config ([#854](https://github.com/scuttletoby/t3code/issues/854));
  starting a new project without hand work ([#850](https://github.com/scuttletoby/t3code/issues/850)).
- General flows for deploy and release with rollback
  ([#856](https://github.com/scuttletoby/t3code/issues/856)), cleanup
  ([#855](https://github.com/scuttletoby/t3code/issues/855)), and leases, scheduled jobs and backups
  ([#857](https://github.com/scuttletoby/t3code/issues/857)).
- Watch every workflow and alert when one is switched off
  ([#867](https://github.com/scuttletoby/t3code/issues/867)); one keeper for the interaction limit
  ([#858](https://github.com/scuttletoby/t3code/issues/858)).
- Root-cause fixes enforced by a check ([#864](https://github.com/scuttletoby/t3code/issues/864)),
  evidence for big recommendations ([#868](https://github.com/scuttletoby/t3code/issues/868)), a
  scheduled blind-spot audit ([#869](https://github.com/scuttletoby/t3code/issues/869)) and a tooling
  check when a project starts ([#859](https://github.com/scuttletoby/t3code/issues/859)).
