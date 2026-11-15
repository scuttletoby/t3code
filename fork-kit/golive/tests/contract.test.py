"""The go-live kit reads files, units and a report that T3's own code defines. This test fails when the code that
defines one of them changes or moves, so the kit is updated in the same PR instead of breaking on a live machine.
It reads source files only.  python3 -I -B fork-kit/golive/tests/contract.test.py

The report command comes from the fork PR "read-only report command" (apps/server/src/golive/forkReport.ts); until
that PR is on main, the report part fails on purpose: the kit cannot run without it.
"""
import os, re, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golive_fixture  # noqa: E402,F401  (puts the kit on sys.path)
from golive import live, osadapter  # noqa: E402

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
    ('apps/server/src/config.ts', 'join(stateDir, "server-runtime.json")', 'server runtime file'),
    ('apps/server/src/config.ts', f'join(stateDir, "{live.DATABASE}")', 'database file'),
    ('apps/server/src/config.ts', ': "userdata",', 'state folder userdata/'),
    ('packages/shared/src/cliRelease.ts', 'export const CLI_RELEASE_CHECKSUMS_FILE = "SHA256SUMS";', 'SHA256SUMS'),
    ('packages/shared/src/cliRelease.ts',
     'return `t3-${version}-${platformKey}.${platformKey.startsWith("win32") ? "zip" : "tar.gz"}`;',
     'archive name t3-<version>-<platform>.tar.gz'),
    ('apps/server/src/cloud/serviceProtocol.ts', 'const SEMVER_NUMBER = "(?:0|[1-9]\\\\d*)";',
     'config.EXACT_VERSION'),
    ('apps/server/src/cloud/serviceProtocol.ts',
     'const SEMVER_PRERELEASE = `(?:${SEMVER_NUMBER}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)`;', 'config.EXACT_VERSION'),
]

REPORT_FILE = 'apps/server/src/golive/forkReport.ts'
REPORT_CLI = 'apps/server/src/cli/forkReport.ts'
# Every key path check.py and live.py read from the report.
REPORT_KEYS = ['readonly reportVersion: number;', 'readonly version:', 'readonly pendingMigrations:',
               'readonly counts:', 'readonly threads:', 'readonly projects:', 'readonly featuresOn:',
               'readonly problems:', 'readonly ok: boolean;']


class Upstream(unittest.TestCase):
    def test_the_files_and_names_the_kit_reads_are_still_there(self):
        for rel, text, why in UPSTREAM:
            with self.subTest(why, file=rel):
                self.assertIn(text, source(rel), f'{rel} changed: update the go-live kit ({why})')

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
        for key in REPORT_KEYS:
            with self.subTest(key):
                self.assertIn(key, text)
        self.assertIn('Command.make("__fork-report"', source(REPORT_CLI))


if __name__ == '__main__':
    unittest.main()
