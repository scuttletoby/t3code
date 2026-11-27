"""The example factsCommand scripts (fork-kit/placement-facts), run as T3 runs them: one path, no arguments, exit 0
with label lines or exit 1 with the reason first on the error output. Stand-in `pgrep`, `nvidia-smi`, `timeout` and
`pmset` on PATH, a stand-in power-supply folder and busy file, so no real machine state is read.

  python3 -I -B fork-kit/placement-facts/tests/scripts.test.py

The state question: the scripts store nothing; each run reads the machine afresh, so a second run, a run stopped
half-way or a missing or empty busy file or power-supply folder gives the answer of that moment (tested below).
"""
import os, re, shutil, stat, subprocess, tempfile, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_PGREP = """#!/bin/sh
# Stand-in pgrep: "running" lists one process name or -f pattern per line.
pattern=""
for arg in "$@"; do pattern=$arg; done
grep -qxF -- "$pattern" "$FAKE_DIR/running" 2>/dev/null
"""
FAKE_SMI = """#!/bin/sh
[ "$1" = "-L" ] || exit 9
cat "$FAKE_DIR/gpus" 2>/dev/null
exit "$(cat "$FAKE_DIR/smi-code" 2>/dev/null || echo 0)"
"""
FAKE_TIMEOUT = """#!/bin/sh
shift
exec "$@"
"""
FAKE_PMSET = """#!/bin/sh
cat "$FAKE_DIR/pmset"
"""


class ScriptCase(unittest.TestCase):
    SCRIPT = ''

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='t3-placement-facts-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.bin = os.path.join(self.dir, 'bin')
        os.mkdir(self.bin)
        for name, body in (('pgrep', FAKE_PGREP), ('nvidia-smi', FAKE_SMI), ('timeout', FAKE_TIMEOUT),
                           ('pmset', FAKE_PMSET)):
            self.tool(name, body)
        self.power = os.path.join(self.dir, 'power')
        os.mkdir(self.power)
        self.busy_file = os.path.join(self.dir, 'busy')
        self.write('pmset', "Now drawing from 'AC Power'\n -InternalBattery-0 100%; charged\n")

    def tool(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, 'w') as f:
            f.write(body)
        os.chmod(path, 0o755)

    def write(self, name, text):
        with open(os.path.join(self.dir, name), 'w') as f:
            f.write(text)

    def supply(self, name, kind, online=None, scope=None):
        folder = os.path.join(self.power, name)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'type'), 'w') as f:
            f.write(kind + '\n')
        if scope is not None:
            with open(os.path.join(folder, 'scope'), 'w') as f:
                f.write(scope + '\n')
        if online is not None:
            with open(os.path.join(folder, 'online'), 'w') as f:
                f.write(f'{online}\n')

    def configured(self, **settings):
        """A copy of the script with its settings lines replaced, as a person edits them."""
        with open(os.path.join(KIT, self.SCRIPT)) as f:
            text = f.read()
        settings.setdefault('BUSY_FILE', self.busy_file)
        settings.setdefault('POWER_SUPPLY_DIR', self.power)
        for key, value in settings.items():
            if key == 'POWER_SUPPLY_DIR' and 'POWER_SUPPLY_DIR=' not in text:
                continue  # macOS reads power from pmset
            text, count = re.subn(rf'^{key}=.*$', f'{key}="{value}"', text, count=1, flags=re.M)
            self.assertEqual(count, 1, key)
        path = os.path.join(self.dir, 'facts')
        with open(path, 'w') as f:
            f.write(text)
        os.chmod(path, 0o755)
        return path

    def only_stand_ins_on_path(self):
        """PATH holds the stand-ins and the few plain tools the scripts use, so a removed stand-in is missing."""
        core = os.path.join(self.dir, 'core')
        os.mkdir(core)
        for name in ('cat', 'head', 'grep'):
            os.symlink(shutil.which(name), os.path.join(core, name))
        self.path = f'{self.bin}:{core}'

    def run_facts(self, **settings):
        path = self.configured(**settings)
        env = {'PATH': getattr(self, 'path', f'{self.bin}:/usr/bin:/bin'), 'FAKE_DIR': self.dir, 'HOME': self.dir}
        done = subprocess.run([path], env=env, capture_output=True, text=True, timeout=10)
        return done.returncode, done.stdout.splitlines(), done.stderr.strip().splitlines()[:1]


class CommonRules:
    """What the Linux and macOS scripts both do."""

    def test_free_prints_extra_labels(self):
        code, labels, _ = self.run_facts(EXTRA_LABELS='owner-logins big-disk')
        self.assertEqual((code, labels), (0, ['owner-logins', 'big-disk']))

    def test_free_with_nothing_set_prints_nothing(self):
        self.assertEqual(self.run_facts()[:2], (0, []))

    def test_busy_file_is_not_available_with_its_first_line(self):
        self.write('busy', 'gaming tonight\nsecond line\n')
        self.assertEqual(self.run_facts(EXTRA_LABELS='gpu'), (1, [], ['busy: gaming tonight']))

    def test_empty_busy_file_still_says_why(self):
        self.write('busy', '')
        code, labels, said = self.run_facts()
        self.assertEqual((code, labels), (1, []))
        self.assertEqual(said, [f'busy: {self.busy_file} is there'])

    def test_busy_process_is_not_available(self):
        self.write('running', 'steam\n')
        self.assertEqual(self.run_facts(BUSY_PROCESSES='zoom steam'), (1, [], ['busy: steam is running']))
        # Not in the list: free.
        self.assertEqual(self.run_facts(BUSY_PROCESSES='zoom')[0], 0)

    def test_missing_pgrep_with_a_busy_list_is_not_available(self):
        self.only_stand_ins_on_path()
        os.remove(os.path.join(self.bin, 'pgrep'))
        self.assertEqual(self.run_facts(BUSY_PROCESSES='steam'),
                         (1, [], ['busy: pgrep is missing, so BUSY_PROCESSES cannot be checked']))
        # An empty busy list needs no pgrep: free, and no ci-runner label either.
        self.assertEqual(self.run_facts()[:2], (0, []))

    def test_runner_is_a_label(self):
        self.write('running', 'Runner.Listener\n')
        self.assertEqual(self.run_facts()[:2], (0, ['ci-runner']))

    def test_runs_again_reads_afresh(self):
        self.write('busy', 'back soon\n')
        self.assertEqual(self.run_facts()[0], 1)
        os.remove(self.busy_file)
        self.assertEqual(self.run_facts()[0], 0)

    def test_answers_quickly(self):
        import time
        start = time.monotonic()
        self.run_facts(EXTRA_LABELS='gpu')
        self.assertLess(time.monotonic() - start, 2)


class Linux(CommonRules, ScriptCase):
    SCRIPT = 'facts-linux.sh'

    def test_on_battery_is_not_available(self):
        self.supply('BAT0', 'Battery')
        self.supply('AC', 'Mains', online=0)
        self.assertEqual(self.run_facts(), (1, [], ['busy: on battery']))
        self.assertEqual(self.run_facts(BUSY_ON_BATTERY='0')[0], 0)

    def test_on_power_or_no_battery_is_free(self):
        self.supply('AC', 'Mains', online=1)
        self.assertEqual(self.run_facts()[0], 0)
        self.supply('BAT0', 'Battery')
        self.assertEqual(self.run_facts()[0], 0)
        # USB-C power counts too.
        self.supply('AC', 'Mains', online=0)
        self.supply('ucsi', 'USB', online=1)
        self.assertEqual(self.run_facts()[0], 0)

    def test_a_device_battery_is_not_the_machine_on_battery(self):
        # A desktop with a wireless mouse: the mouse's battery (scope Device) and no Mains entry.
        self.supply('hid-00:11:22:33:44:55-battery', 'Battery', scope='Device')
        self.assertEqual(self.run_facts()[0], 0)
        # A laptop's own battery (scope System, or no scope file) beside it still counts.
        self.supply('BAT0', 'Battery', scope='System')
        self.assertEqual(self.run_facts(), (1, [], ['busy: on battery']))

    def test_busy_process_longer_than_15_characters_matches_its_kernel_name(self):
        # Linux keeps 15 characters of a process name, and `pgrep -x` matches those.
        self.write('running', 'gnome-character\n')
        self.assertEqual(self.run_facts(BUSY_PROCESSES='gnome-characters'),
                         (1, [], ['busy: gnome-characters is running']))

    def test_missing_power_folder_is_free(self):
        self.assertEqual(self.run_facts(POWER_SUPPLY_DIR=os.path.join(self.dir, 'nope'))[0], 0)

    def test_nvidia_gpu_is_gpu_and_cuda(self):
        self.write('gpus', 'GPU 0: Some GPU (UUID: GPU-0000)\n')
        self.assertEqual(self.run_facts(EXTRA_LABELS='big-disk')[:2], (0, ['big-disk', 'gpu', 'cuda']))

    def test_nvidia_smi_that_fails_or_lists_none_is_no_gpu(self):
        self.write('gpus', 'No devices were found\n')
        self.assertEqual(self.run_facts()[:2], (0, []))
        self.write('gpus', 'GPU 0: Some GPU\n')
        self.write('smi-code', '9\n')
        self.assertEqual(self.run_facts()[:2], (0, []))
        os.remove(os.path.join(self.bin, 'nvidia-smi'))
        self.assertEqual(self.run_facts()[:2], (0, []))


class MacOS(CommonRules, ScriptCase):
    SCRIPT = 'facts-macos.sh'

    def test_on_battery_is_not_available(self):
        self.write('pmset', "Now drawing from 'Battery Power'\n -InternalBattery-0 80%; discharging\n")
        self.assertEqual(self.run_facts(), (1, [], ['busy: on battery']))
        self.assertEqual(self.run_facts(BUSY_ON_BATTERY='0')[0], 0)

    def test_no_pmset_answer_is_free(self):
        self.write('pmset', '')
        self.assertEqual(self.run_facts()[0], 0)


class Windows(unittest.TestCase):
    """Not built here: its settings are where the README says, and it uses nothing newer than C# 5 (csc.exe of
    .NET Framework 4 builds only that)."""

    def setUp(self):
        with open(os.path.join(KIT, 'facts-windows.cs')) as f:
            self.source = f.read()

    def test_settings_at_the_top(self):
        for setting in ('static readonly string[] BusyProcesses', 'static readonly bool BusyOnBattery',
                        'static readonly string[] ExtraLabels'):
            self.assertIn(setting, self.source)

    def test_no_syntax_newer_than_csharp_5(self):
        code = re.sub(r'//.*', '', self.source)
        code = re.sub(r'"(?:[^"\\]|\\.)*"', '""', code)
        for newer in ('$"', '?.', '=>', 'nameof(', 'out var ', 'is not ', '?[', 'record '):
            self.assertNotIn(newer, code, newer)

    def test_same_labels_and_reasons_as_the_scripts(self):
        for text in ('"gpu"', '"cuda"', '"ci-runner"', '"busy: on battery"', '" is running"', '"t3-placement"',
                     '"busy"', '"Runner.Listener"'):
            self.assertIn(text, self.source)


if __name__ == '__main__':
    unittest.main()
