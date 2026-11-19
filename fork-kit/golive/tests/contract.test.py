"""The go-live kit reads files, units and a report that T3's own code defines. This test fails when the code that
defines one of them changes or moves, so the kit is updated in the same PR instead of breaking on a live machine.
It reads source files only.  python3 -I -B fork-kit/golive/tests/contract.test.py

Names the kit would fail open on are pinned too: if upstream renamed the pending update status, check would pass
with an update in flight, so the exact name is pinned here. The report command is the fork's own
(apps/server/src/golive/forkReport.ts and apps/server/src/cli/forkReport.ts).
"""
import os, re, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golive_fixture  # noqa: E402,F401  (puts the kit on sys.path)
from golive import coldcopy, handoff, live, osadapter  # noqa: E402

REPO = os.path.dirname(os.path.dirname(golive_fixture.KIT))


def source(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as f:
        return f.read()


# (file, what must be in it, what the kit depends on)
UPSTREAM = [
    ('apps/server/src/cloud/bootService.ts', 'const BOOT_SERVICE_NAME = "t3code";', f'unit {osadapter.UNIT}'),
    ('apps/server/src/cloud/bootService.ts', '`Environment=T3CODE_HOME=${quoteSystemdValue(plan.baseDir)}`',
     'the unit names its home'),
    ('apps/server/src/cloud/bootService.ts', 'path.join(input.baseDir, "runtime", SERVICE_STATE_FILE)',
     'service state under runtime/'),
    ('apps/server/src/cloud/serviceProtocol.ts', 'export const SERVICE_STATE_FILE = "service-state.json";',
     'service state file'),
    ('apps/server/src/cloud/serviceProtocol.ts', 'export const SERVICE_STOP_MARKER_FILE = ".service-stopping";',
     'stop marker'),
    ('apps/server/src/cloud/serviceProtocol.ts', 'export const SERVICE_RESTART_PENDING_FILE = ".restart-pending";',
     'restart marker'),
    ('apps/server/src/cloud/serviceProtocol.ts', 'readonly status: "pending";',
     'live.service_state: an update in flight is status "pending" (fails open if renamed)'),
    ('apps/server/src/cloud/serviceProtocol.ts', 'update?.status === "pending"', 'pending status in use'),
    ('apps/server/src/serverRuntimeState.ts', 'version: Schema.Literal(1),', 'server-runtime.json record version 1'),
    ('apps/server/src/serverRuntimeState.ts', '  pid: Schema.Int,', 'server-runtime.json pid'),
    ('apps/server/src/serverRuntimeState.ts', '  port: Schema.Int,', 'server-runtime.json port'),
    ('apps/server/src/serverRuntimeState.ts', '  serverVersion: Schema.optional(Schema.String),',
     'server-runtime.json serverVersion'),
    ('apps/server/src/cloud/serviceProtocol.ts', 'protocol === 3 || protocol === SERVICE_LAUNCHER_PROTOCOL',
     'service state protocols 3 and 4'),
    ('apps/server/src/cloud/serviceProtocol.ts',
     """raw.slice(1, -1).replaceAll('\\\\"', '"').replaceAll("\\\\\\\\", "\\\\")""", 'unquote_systemd'),
    ('apps/server/src/cloud/serviceProtocol.ts', '.replaceAll("%%", "%")', 'unquote_systemd'),
    ('apps/server/src/serviceLauncher.ts', 'const RESTORE_MARKER = ".restore-pending";', 'restore marker'),
    ('apps/server/src/serviceLauncher.ts', 'NodePath.join(baseDir, "runtime", "db-backup", updateId)',
     'restore markers under runtime/db-backup/<id>/'),
    ('apps/server/src/serviceLauncher.ts', 'NodePath.join(baseDir, "runtime", "versions", version)',
     'runtimes under runtime/versions/<v>/'),
    ('apps/server/src/serviceLauncher.ts', 'process.platform === "win32" ? "t3.exe" : "t3"', 'runtime binary t3'),
    ('apps/server/src/cli/update.ts', 'export const updateCommand = Command.make("update", {', 'switch: t3 update'),
    ('apps/server/src/cli/update.ts', '...projectLocationFlags,', 'switch: update takes --base-dir'),
    ('apps/server/src/cli/update.ts', 'allowDowngrade: Flag.Boolean("allow-downgrade")', 'way back: --allow-downgrade'),
    ('apps/server/src/cli/update.ts', 'yes: Flag.Boolean("yes")', 'switch: update --yes restarts the service'),
    ('apps/server/src/cli/servicePreflight.ts', 'Command.make("__service-preflight", {', 'switch: old preflight'),
    ('apps/server/src/cli/servicePreflight.ts', 'databasePath: Flag.String("database-path"),', 'preflight flag'),
    ('apps/server/src/cli/servicePreflight.ts', 'launcherProtocol: Flag.Int("launcher-protocol"),', 'preflight flag'),
    ('apps/server/src/cloud/servicePreflight.ts', 'return { status: "ready", version, launcherProtocol };',
     'switch: preflight says ready'),
    ('apps/server/src/cloud/pinnedRuntime.ts', 'const PINNED_RUNTIME_LAUNCHERS_FILE = "launchers";',
     'switch.launchers: runtime/launchers'),
    ('apps/server/src/cloud/pinnedRuntime.ts',
     'environment["T3CODE_INSTALL_BIN_DIR"] || path.join(home, ".local", "bin"),', 'switch.launchers: default'),
    ('apps/server/src/config.ts', 'join(stateDir, "server-runtime.json")', 'server runtime file'),
    ('apps/server/src/config.ts', f'join(stateDir, "{live.DATABASE}")', 'database file'),
    ('apps/server/src/config.ts', ': "userdata",', 'state folder userdata/'),
    ('apps/server/src/serverOwnership.ts',
     f'const ownershipLockPath = (stateDir: string) => NodePath.join(stateDir, "{coldcopy.OWNER_LOCK}");',
     'coldcopy.OWNER_LOCK: the ownership lock copy and restore hold'),
    ('apps/server/src/serverOwnership.ts', 'db.exec("PRAGMA busy_timeout = 250; BEGIN EXCLUSIVE;");',
     'coldcopy.hold_owner_lock: an exclusive transaction; handoff.HANDOVER_POLL lets go well inside its 250 ms'),
    ('packages/shared/src/cliRelease.ts', 'export const CLI_RELEASE_CHECKSUMS_FILE = "SHA256SUMS";', 'SHA256SUMS'),
    ('packages/shared/src/cliRelease.ts',
     'return `t3-${version}-${platformKey}.${platformKey.startsWith("win32") ? "zip" : "tar.gz"}`;',
     'archive name t3-<version>-<platform>.tar.gz'),
    # back and status (back.py)
    # switch._install_target: the rehearsed build installed by the kit, so the update downloads nothing
    ('apps/server/src/cli/update.ts', '      ? Effect.succeed(input.requestedVersion)',
     'switch: update <exact version> looks no version up'),
    ('apps/server/src/cloud/pinnedRuntime.ts',
     'entryExists && Option.isSome(sentinel) && sentinel.value.trim() === input.version;',
     'switch._install_target: a runtime with t3 and .install-complete naming the version is used, not downloaded'),
    ('apps/server/src/cli/update.ts', '    requirePublishedRelease: mirror !== undefined,',
     'switch: only a managed update mirror makes the update ask the network about an installed runtime'),
    ('apps/server/src/cloud/pinnedRuntime.ts', 'sentinelPath: path.join(versionDir, ".install-complete"),',
     'back.installed_t3: a whole runtime has runtime/versions/<v>/.install-complete'),
    ('apps/server/src/cloud/pinnedRuntime.ts', 'const PINNED_RUNTIME_LAUNCHERS_FILE = "launchers";',
     'status: the launcher links upstream recorded in runtime/launchers'),
    ('apps/server/src/cli/update.ts', 'allowDowngrade: Flag.Boolean("allow-downgrade")',
     'back: update --allow-downgrade'),
    ('apps/server/src/cli/update.ts', 'yes: Flag.Boolean("yes")', 'back: update --yes restarts the service'),
    ('apps/server/src/cli/config.ts', 'export const baseDirFlag = Flag.String("base-dir")', 'back: update --base-dir'),
    # keptruntime.py: upstream prunes old runtimes at the end of every update, so W1 keeps its own copy
    ('apps/server/src/cli/update.ts', '      keep: [currentVersion, targetVersion],',
     'keptruntime: every update prunes the runtimes it does not keep (the old build after the switch, W2, W3)'),
    ('apps/server/src/cloud/pinnedRuntime.ts', 'const PINNED_RUNTIME_PRUNE_GRACE = Duration.hours(1);',
     'keptruntime.put_back: a fresh .install-complete is spared by a prune for 1 h'),
    ('apps/server/src/cloud/pinnedRuntime.ts',
     'entryExists && Option.isSome(sentinel) && sentinel.value.trim() === input.version;',
     'keptruntime.put_back: a whole runtime put back is used as it is, not downloaded again'),
    ('apps/server/src/cloud/pinnedRuntime.ts', '    if (!isExactServiceVersion(version)) continue;',
     'keptruntime: its .golive-* folders in runtime/versions are no version, never pruned'),
    ('apps/server/src/cloud/serviceProtocol.ts', 'const SEMVER_NUMBER = "(?:0|[1-9]\\\\d*)";',
     'config.EXACT_VERSION'),
    ('apps/server/src/cloud/serviceProtocol.ts',
     'const SEMVER_PRERELEASE = `(?:${SEMVER_NUMBER}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)`;', 'config.EXACT_VERSION'),
    ('apps/server/src/serverRuntimeState.ts', '  workHeld: Schema.optional(Schema.Boolean),', 'rehearse R3: work held'),
    ('apps/server/src/config.ts', 'secretsDir: join(stateDir, "secrets"),', 'rehearse.NOT_CLONED: secrets/'),
    # handoff.py: the gap while the kit has T3 stopped (root cause: the update marker; then the serving lock)
    ('packages/contracts/src/desktopBootstrap.ts',
     f'export const DESKTOP_UPDATE_IN_PROGRESS_MARKER_FILE = "{handoff.MARKER}";',
     'handoff.MARKER: runtime/desktop-update-in-progress'),
    ('packages/contracts/src/desktopBootstrap.ts',
     f'export const DESKTOP_UPDATE_IN_PROGRESS_TTL_MS = {handoff.TTL_MS:_};',
     'handoff.TTL_MS: the marker runs out 60 s after it is written'),
    ('apps/server/src/desktopUpdate/DesktopUpdateMarker.ts',
     'Schema.Struct({ startedAt: Schema.Number, expiresAt: Schema.Number }),',
     'handoff.publish: {"startedAt": ms, "expiresAt": ms}'),
    ('apps/server/src/desktopUpdate/DesktopUpdateMarker.ts',
     'expiresAt: startedAt + DESKTOP_UPDATE_IN_PROGRESS_TTL_MS,', 'handoff.publish: expiresAt = startedAt + TTL'),
    ('apps/server/src/desktopUpdate/DesktopUpdateMarker.ts', 'const temporary = `${markerFile}.tmp`;',
     'handoff.publish: written to <marker>.tmp'),
    ('apps/server/src/desktopUpdate/DesktopUpdateMarker.ts', 'yield* fileSystem.rename(temporary, markerFile);',
     'handoff.publish: then renamed into place'),
    ('apps/server/src/cli/sshHelper.ts', 'const homeOwned = Command.make("home-owned", {',
     'handoff: an SSH launch asks home-owned before it starts a server'),
    ('apps/server/src/cli/sshHelper.ts',
     'if (!((await serviceLauncherIsServing(baseDir)) || (await stateDirOwned(baseDir)))) {',
     'handoff.Gap.starting: the serving lock or the owner lock makes the home owned'),
    ('packages/ssh/src/tunnel.ts',
     '"$RUNNER_FILE" __ssh-helper update-remaining "$DEFAULT_SERVER_HOME/runtime/@@T3_UPDATE_MARKER_FILE@@"',
     'handoff: a launch waits while the marker is valid'),
    ('packages/ssh/src/tunnel.ts', '"$RUNNER_FILE" __ssh-helper home-owned "$DEFAULT_SERVER_HOME"',
     'handoff: then while the home is owned'),
    ('apps/server/src/cloud/serviceProtocol.ts',
     f'export const SERVICE_SERVING_LOCK_FILE = "{handoff.SERVING_LOCK}";',
     'handoff.SERVING_LOCK: runtime/service-serving.lock'),
    ('apps/server/src/serviceLauncher.ts', 'async #claimServing(): Promise<void> {',
     'handoff.Gap.starting: the launcher takes the serving lock before it starts its server'),
]

# The commands and flags `rehearse` runs (R1-R5). Each file is in the kit_tests and workflows jobs' sparse checkouts
# (.github/workflows/fork-tests.yml); a missing one fails, never skips.
REHEARSE_UPSTREAM = [
    ('apps/server/src/cli/servicePreflight.ts', 'Command.make("__service-preflight", {', 'R1/R5 preflight'),
    ('apps/server/src/cli/servicePreflight.ts', 'databasePath: Flag.String("database-path"),', 'preflight flag'),
    ('apps/server/src/cli/servicePreflight.ts', 'launcherProtocol: Flag.Int("launcher-protocol"),', 'preflight flag'),
    ('apps/server/src/cloud/servicePreflight.ts', 'readonly status: "blocked";', 'preflight answers blocked'),
    ('apps/server/src/cloud/servicePreflight.ts', 'readonly status: "ready";', 'preflight answers ready'),
    ('apps/server/src/cli/auth.ts', 'const sessionListCommand = Command.make("list", {', 'R2 auth session list'),
    ('apps/server/src/cli/auth.ts', 'Command.withSubcommands([sessionIssueCommand, sessionListCommand, '
     'sessionRevokeCommand]),', 'R2 auth session list'),
    ('apps/server/src/auth/EnvironmentAuth.ts',
     'const layerStorage = Layer.mergeAll(ServerSecretStore.layer, SqlitePersistence.layerCliConfig);',
     'R2: auth commands open the database through layerCliConfig'),
    ('apps/server/src/persistence/Sqlite.ts', 'export const layerCliConfig = makeLayerConfig(\n  '
     'makeLayerFromPath(() => makeLayerSetup(runMigrationsAsOwner())),', 'R2: layerCliConfig migrates as owner'),
    ('apps/server/src/cli/server.ts', 'const holdWorkFlag = Flag.Boolean("hold-work")', 'R3 serve --hold-work'),
    ('apps/server/src/cli/config.ts', 'const portFlag = Flag.Int("port")', 'R3 --port'),
    ('apps/server/src/cli/config.ts', 'const hostFlag = Flag.String("host")', 'R3 --host'),
    ('apps/server/src/cli/config.ts', 'const noBrowserFlag = Flag.Boolean("no-browser")', 'R3 --no-browser'),
    ('apps/server/src/cli/config.ts', 'export const baseDirFlag = Flag.String("base-dir")', '--base-dir'),
]

REPORT_FILE = 'apps/server/src/golive/forkReport.ts'
REPORT_CLI = 'apps/server/src/cli/forkReport.ts'
# Every key path check.py, live.py and back.py read from the report, with where it sits.
REPORT_PATHS = ['reportVersion', 'version', 'ok', 'problems', 'problems.code', 'problems.message',
                'database.pendingMigrations', 'database.counts', 'database.counts.threads',
                'database.counts.projects', 'database.counts.threadsArchived', 'database.counts.projectsArchived',
                'fork.featuresOn', 'fork.featuresWhere', 'fork.features', 'database.appliedMigrations', 'database.appliedMigrations.id',
                'database.unknownMigrations', 'settings.parseWarning', 'settings.providerInstances',
                'settings.providerInstances.id', 'settings.providerInstances.driver',
                'settings.providerInstances.enabled']
# What live.compare_counts relies on (fork #181): both reports count the same rows, every project and every thread
# not deleted (sub-agent and archived threads included), so one report is compared with another, never with
# `t3 thread list` (one project, no sub-agents).
REPORT_COUNTS = ['projects: count("projection_projects", "deleted_at IS NULL", ["deleted_at"]),',
                 'threads: count(threads, "deleted_at IS NULL", ["deleted_at"]),']


def interface_paths(text, name):
    """The key paths of `export interface <name> { ... }` (nested objects and arrays of objects), e.g.
    'database.counts.threads'. Enough TypeScript for the report's interface, nothing more."""
    body = text[text.index(f'export interface {name} {{'):]
    paths, stack = set(), []
    for line in body.splitlines()[1:]:
        keys = re.findall(r'\breadonly (\w+)\??:', line)
        if keys:
            paths.add('.'.join(stack + [keys[0]]))
            for inner in keys[1:]:  # an object on one line: { readonly a: x; readonly b: y }
                paths.add('.'.join(stack + [keys[0], inner]))
        opened = line.count('{') - line.count('}')
        if opened > 0:
            stack.append(keys[0] if keys else '?')
        elif opened < 0:
            if not stack:
                break  # the interface's own closing brace
            stack.pop()
    return paths


class Upstream(unittest.TestCase):
    def test_the_files_and_names_the_kit_reads_are_still_there(self):
        for rel, text, why in UPSTREAM:
            with self.subTest(why, file=rel):
                self.assertIn(text, source(rel), f'{rel} changed: update the go-live kit ({why})')

    def test_the_commands_rehearse_runs_are_still_there(self):
        for rel, text, why in REHEARSE_UPSTREAM:
            with self.subTest(why, file=rel):
                self.assertIn(text, source(rel), f'{rel} changed: update rehearse ({why})')

    def test_platform_keys(self):
        keys = re.search(r'CLI_ARCHIVE_PLATFORM_KEYS = \[(.*?)\]', source('packages/shared/src/cliRelease.ts'),
                         re.S).group(1)
        self.assertEqual(re.findall(r'"([^"]+)"', keys),
                         ['darwin-arm64', 'linux-arm64', 'linux-x64', 'win32-arm64', 'win32-x64'])
        self.assertIn(live.platform_key(), [None, 'darwin-arm64', 'linux-arm64', 'linux-x64'])


class Report(unittest.TestCase):
    def test_the_report_command_and_its_keys(self):
        self.assertTrue(os.path.isfile(os.path.join(REPO, REPORT_FILE)),
                        f'{REPORT_FILE} is missing: the kit needs the fork PR "read-only report command" on main')
        text = source(REPORT_FILE)
        self.assertIn(f'export const FORK_REPORT_VERSION = {live.REPORT_VERSION};', text)
        paths = interface_paths(text, 'ForkReport')
        for key in REPORT_PATHS:
            with self.subTest(key):
                self.assertIn(key, paths, f'{REPORT_FILE}: ForkReport.{key} moved or is gone')
        for line in REPORT_COUNTS:
            with self.subTest(line):
                self.assertIn(line, text, f'{REPORT_FILE}: what a count counts changed: update live.compare_counts')
        self.assertEqual(set(live.COUNTS), {p.split('.')[-1] for p in paths if p.startswith('database.counts.')})
        cli = source(REPORT_CLI)
        self.assertIn('Command.make("__fork-report"', cli)
        self.assertIn('Flag.String("base-dir")', cli)  # live.fork_report passes --base-dir <home>

    def test_the_interface_reader_sees_nesting(self):
        paths = interface_paths('export interface X {\n  readonly a: {\n    readonly b: number;\n  };\n'
                                '  readonly c: ReadonlyArray<{\n    readonly d: string;\n  }>;\n'
                                '  readonly e: { readonly f: string; readonly g: boolean };\n}\nconst z = { y: 1 };\n',
                                'X')
        self.assertEqual(paths, {'a', 'a.b', 'c', 'c.d', 'e', 'e.f', 'e.g'})


if __name__ == '__main__':
    unittest.main()
