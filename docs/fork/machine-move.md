# Moving an agent machine to new hardware (runbook template)

Fork only. A general template: copy it into the machine's private notes and fill in the facts there (host names,
users, projects, sizes). Nothing in this page names a real machine. Placeholders are in CAPITALS: `OLD` and `NEW` are
the machines, `AGENT` is the user the agents run as.

The move has four parts. **Inventory** and **set-up** happen while OLD still runs, the **switch** is a short stop,
and **after** checks NEW before OLD is wiped. Every step can be run again.

## 1. Inventory (on OLD)

- **Tools with no source.** Run the gardener's [`machine-tools`](../../fork-kit/gardener/README.md#general-checks)
  check on OLD (`gardener run --dry-run`, or the check alone). Every program and unit it reports as `no-source`
  or `untracked` was put there by hand. For each one, decide whether to:
  - move it into a fork kit (general);
  - move it into the project's repository (project-only);
  - declare it in the check's `tools` with where it comes from (for example a vendor's installer);
  - remove it.

  Only a tool that has a source can be set up on NEW without guessing.

- **Tools NEW needs.** List each tool the projects need in the check's `tools` (`path` and `source`). On NEW, the
  same check then names every one that is still missing, with its source, so nothing is found missing the first
  time an agent needs it.
- **Kits.** `install-drift` on OLD lists the installed kits and the release each one runs. NEW gets the same kits
  from the same release.
- **Logins.** List each account a tool on OLD is signed in to (the provider CLIs, `gh`, package registries). Each
  needs a sign-in on NEW, or a copied credential file whose path is written down.
- **Scheduled work.** List the timers and services the agents rely on (`systemctl list-timers`, also with
  `--user`), so NEW can be checked against the list.

## 2. Set-up (on NEW, OLD still running)

1. Write NEW's machine file (`/etc/fork-kit/machine.json`, see [common](../../fork-kit/common/README.md#the-machine-file))
   by hand from OLD's, and change each fact that differs on NEW (users, folders, disks, roles). Include the
   `"alert"` section.
2. Install each kit with the bootstrap from the release that OLD runs, then run each one's `self-check`.
3. Run `update-policy test-alert` (as root) and check that the alert arrived. NEW's baseline check must not say
   `no alert channel`.
4. **Copy ahead** the agent's home and the projects' folders while OLD still runs, with `rsync -a`. Run it again as
   often as needed; each run copies only what changed. The caches are in [step 3](#3-caches) below.
5. Install each tool from the inventory from its source, until `machine-tools` on NEW reports nothing missing.

## 3. Caches

**Gradle (decision for [#1060](https://github.com/scuttletoby/t3code/issues/1060)): copy only the downloads, size
capped, in the copy-ahead. Warm the rest on purpose after the switch.**

- **Copy ahead** only two folders:
  - `~/.gradle/caches/modules-2/`: downloaded dependencies, stored by coordinates and checksum, the same on any
    machine and the expensive part to download again;
  - `~/.gradle/wrapper/dists/`: the Gradle distributions the wrappers fetched.

  Gradle writes some files in `modules-2` in place while it builds (its `metadata-*` stores, under the `*.lock`
  files left out below), so a copy taken while OLD builds can be torn: rsync makes each file whole on NEW, not
  consistent with the rest. Copy ahead as often as you like, but take **the last copy-ahead run with no Gradle
  running on OLD**: hold the builds and stop the daemons first (`./gradlew --stop` in a project, or stop every
  `GradleDaemon` process of the agent's user), then copy, then let builds run again. That run copies again each
  file that changed since an earlier run, so it also mends what an earlier run tore. Dependency verification
  (`gradle/verification-metadata.xml`), where a project has it, catches a torn jar or POM but not a torn metadata
  store, so it does not replace this stopped run.

  Leave out the lock files and Gradle's own housekeeping:

  ```
  rsync -a --exclude='*.lock' --exclude='gc.properties' AGENT@OLD:.gradle/caches/modules-2/ ~/.gradle/caches/modules-2/
  rsync -a --exclude='*.lock' AGENT@OLD:.gradle/wrapper/dists/ ~/.gradle/wrapper/dists/
  ```

- **Size cap: 10 GiB for the two together** (measure on OLD with `du -sh`). Over the cap, or on a slow link, do not
  copy them: warm instead (below). A dependency that was not copied costs one download, never a wrong build.
- **Never copy** the rest of `~/.gradle`:
  - `daemon/`, `native/` and `build-scan-data/`;
  - the per-version folders in `caches/` (`caches/<gradle version>/`, `transforms-*`, `jars-*`, `kotlin-dsl`);
  - the local build cache (`caches/build-cache-*`).

  They depend on the Gradle version, the JDK and the paths, so Gradle rebuilds them in minutes, and a copy taken
  while a build runs can be half-written.

- **Not in the final delta.** The copy-ahead is enough: the dependencies do not change in the last hour. A cache
  copied during the stop would only make the stop longer.
- **Warm after the switch, before agents start.** Run one build per project through the project's own build wrapper
  and its usual way to run builds on that machine (for example inside `heavy`), one project at a time. The first
  agent build is then as fast as on OLD, and a project that does not build on NEW is found before an agent needs it.
- **State:**
  - A copy that runs again copies only what changed.
  - A copy that stops half-way leaves only whole files: rsync writes each file under a temporary name and renames
    it at the end. Run it again.
  - A copy over an existing cache adds the files that are missing and never deletes any (no `--delete`).
  - A broken copy is not repaired by Gradle on its own: without dependency verification a torn jar or metadata
    file fails the build. Recovery: delete the copied `~/.gradle/caches/modules-2/` on NEW and warm (below).

Other build caches (npm, pnpm, pip, Maven) follow the same rule: copy the content-addressed download store ahead
if it fits under the cap, and never copy a tool's per-version or per-path state.

## 4. The switch

1. On OLD, stop the agents and the scheduled work from the inventory. Then stop the services.
2. Run the **final delta**: the same rsync runs once more, now with nothing running on OLD. Leave out the caches.
3. Start the services and timers on NEW and check them against the inventory list.
4. Warm the caches (above).
5. Start the agents.

## 5. After (on NEW)

- `gardener run --dry-run` on NEW:
  - `machine-tools` reports nothing missing, and no new `no-source` entries compared with OLD;
  - `install-drift` is clean and on the expected release;
  - the baseline check (`update-policy baseline`) reports no drift.
- Sign in to each login from the inventory and check it with one real call.
- Keep OLD stopped, not wiped, until NEW has run a full day of normal work (one of every timer). Moving back means
  starting OLD's services again, and nothing on OLD has changed since the switch.
