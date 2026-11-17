#!/usr/bin/env python3
"""Tests of fork-fleet-walls.py (OS detection, refusal of an OS without walls, the walls-file contract it enforces) and
of every walls file in fork-fleet-walls/; the contract each must pass on fake machines from its <os>.fixture.json is
in fork-fleet-walls-contract.test.py, which uses this file's helpers. A new walls file (and its fixture) is tested
without a change to either file. Run: python3 <this>. Linux only."""
import ast, base64, contextlib, importlib.machinery, importlib.util, io, json, os, re, shutil, stat, subprocess, sys, tempfile, unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'fork-fleet-walls.py')
FOLDER = os.path.join(HERE, 'fork-fleet-walls')
LAUNCHER = os.path.join(HERE, 'fork-fleet-launch.sh')
JOB_PATHS = 'fork-fleet-job-paths.txt'   # the launcher's allow list of what jobs see, which the walls read too
_spec = importlib.util.spec_from_file_location('fork_fleet_walls', SCRIPT)
fw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fw)
OSES = sorted(n for n in os.listdir(FOLDER) if fw.OS_NAME.fullmatch(n))


def slurp(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def write(path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


LINUX = json.loads(slurp(os.path.join(FOLDER, 'linux.fixture.json')))['pass']
WSL1 = {'proc/sys/kernel/ostype': 'Linux\n', 'proc/sys/kernel/osrelease': '4.4.0-19041-Microsoft\n'}


def build(root, files):
    """Makes a fake machine under root: a string is a file's text, a path ending in / a folder, a dict a program."""
    for path, value in files.items():
        full = os.path.join(root, path)
        if path.endswith('/'):
            os.makedirs(full, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(full), exist_ok=True)
        text = value['text'] if isinstance(value, dict) else value
        with open(full, 'w', encoding='utf-8') as f:
            f.write(text)
        os.chmod(full, int(value['mode'], 8) if isinstance(value, dict) else 0o644)   # as installed, whatever the umask
    for folder, _, _ in os.walk(root):   # folders too: a link in a group-writable one is refused (links_owned)
        if folder != root:
            os.chmod(folder, 0o755)
    return root


# Issue #217: a job unit an older launcher started (no stop timeout of its own: systemd's defaults decide it), and one
# today's start() made, as systemd writes their transient unit files (a time span as systemd formats it).
TRANSIENT = '# This is a transient unit file, created programmatically via the systemd API. Do not edit.\n'
OLD_JOB = {'run/systemd/transient/fork-fleet-ci-0.service': TRANSIENT + '[Service]\nRuntimeMaxSec=1h\n'}
NEW_JOB = {'run/systemd/transient/fork-fleet-ci-2.service': TRANSIENT + '[Service]\nRuntimeMaxSec=1h\n'
           'TimeoutStopSec=1min 30s\nTimeoutAbortSec=1min 30s\n'}
SLOW_DEFAULTS = '[Manager]\nDefaultTimeoutStopSec=1h\nDefaultTimeoutAbortSec=1h\n'


def overlay(files, change):
    out = dict(files)
    for path, value in change.items():
        for key in [k for k in out if k == path or k.startswith(path.rstrip('/') + '/')]:
            del out[key]
        if value is not None:
            out[path] = value
    return out


def run(cmd, root, **extra):
    env = {'PATH': fw.PATH, 'LC_ALL': 'C', 'FORK_FLEET_WALLS_ROOT': root, **extra}
    return subprocess.run(cmd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fork-fleet-walls-test-')
        os.chmod(self.tmp, 0o755)
        self.addCleanup(shutil.rmtree, self.tmp)
        self.n = 0

    def root(self, files):
        self.n += 1
        root = os.path.join(self.tmp, f'root{self.n}')
        os.makedirs(root, mode=0o755)
        os.chmod(root, 0o755)   # whatever the umask: fake_root takes only a folder this user alone can write
        return build(root, files)

    def install(self, walls=None, lists=None):
        """A copy of fork-fleet-walls.py with a walls folder of its own: {os: walls file text} (None: the real ones,
        with their fixtures), each with a fixture whose `fail` names the walls in lists[os] (default: os only)."""
        bin_dir = os.path.join(self.tmp, f'bin{self.n}')
        self.n += 1
        os.makedirs(os.path.join(bin_dir, 'fork-fleet-walls'))
        shutil.copy(SCRIPT, bin_dir)
        os.chmod(os.path.join(bin_dir, 'fork-fleet-walls.py'), 0o755)
        shutil.copy(os.path.join(HERE, JOB_PATHS), bin_dir)
        os.chmod(os.path.join(bin_dir, JOB_PATHS), 0o644)
        shutil.copy(LAUNCHER, bin_dir)   # the walls ask it what jobs see (`paths`)
        os.chmod(os.path.join(bin_dir, 'fork-fleet-launch.sh'), 0o755)
        os.chmod(bin_dir, 0o755)
        if walls is None:
            walls = {n: slurp(os.path.join(FOLDER, n)) for n in OSES}
            fixtures = {n: slurp(os.path.join(FOLDER, n + '.fixture.json')) for n in OSES}
        else:
            fixtures = {n: json.dumps({'fail': {w: [{}] for w in (lists or {}).get(n, ['os'])}}) for n in walls}
        for name, text in walls.items():
            path = os.path.join(bin_dir, 'fork-fleet-walls', name)
            with open(path, 'w') as f:
                f.write(text)
            os.chmod(path, 0o755)
            with open(path + '.fixture.json', 'w') as f:
                f.write(fixtures[name])
            os.chmod(path + '.fixture.json', 0o644)
        os.chmod(os.path.join(bin_dir, 'fork-fleet-walls'), 0o755)
        return os.path.join(bin_dir, 'fork-fleet-walls.py')


class Detect(Case):
    def test_names_the_os(self):
        cases = [
            (LINUX, 'linux'),
            (overlay(LINUX, {'proc/sys/kernel/osrelease': '6.6.36.3-microsoft-standard-WSL2\n'}), 'wsl2'),
            (overlay(LINUX, {'run/WSL/': ''}), 'wsl2'),   # a WSL2 kernel of one's own: WSL's mark
            (overlay(LINUX, {'proc/sys/fs/binfmt_misc/WSLInterop-late': 'enabled\n'}), 'wsl2'),
            (overlay(LINUX, {'usr/lib/wsl/': ''}), 'wsl2'),   # interop off: WSL's shared folders
            (overlay(LINUX, {'mnt/wsl/': ''}), 'wsl2'),
            (overlay(LINUX, {'proc/cmdline': 'initrd=\\initrd.img WSL_ROOT_INIT=1 panic=-1\n'}), 'wsl2'),
            (WSL1, 'wsl1'),
            ({'proc/sys/kernel/ostype': 'FreeBSD\n'}, 'freebsd'),
            ({'proc/sys/kernel/ostype': '../../etc\n'}, 'etc'),   # never a path
            ({'proc/sys/kernel/ostype': '...\n'}, 'unknown'),
            ({}, 'unknown'),   # a fake machine with no /proc
        ]
        for files, want in cases:
            with self.subTest(want=want, files=files):
                self.assertEqual(fw.detect(self.root(files)), want)

    def test_linux_walls_know_every_wsl_mark(self):   # else a WSL2 that detect calls linux could pass them
        text = slurp(os.path.join(FOLDER, 'linux'))
        self.assertIn(f'WSL_MARKS = {fw.WSL_MARKS!r}', text)
        self.assertIn(f'WSL_CMDLINE = {fw.WSL_CMDLINE!r}', text)

    def test_this_machine(self):
        self.assertRegex(fw.detect(''), fw.OS_NAME)


class Refuse(Case):
    def test_an_os_without_walls_is_not_supported_yet(self):
        script = self.install({'linux': '#!/bin/sh\nexit 0\n'})
        for files, name in [(WSL1, 'wsl1'), ({}, 'unknown'), (overlay(LINUX, {'run/WSL/': ''}), 'wsl2')]:
            for verb in ('os', 'check', 'describe'):
                with self.subTest(name=name, verb=verb):
                    p = run([script, verb], self.root(files))
                    self.assertEqual(p.returncode, 3, p.stderr)
                    self.assertEqual(p.stdout, '')
                    self.assertIn(f'this OS ({name}) is not supported yet', p.stderr)

    def test_the_real_folder_refuses_wsl1_and_others(self):
        for files in (WSL1, {'proc/sys/kernel/ostype': 'Darwin\n'}):
            p = run([self.install(), 'check'], self.root(files))
            self.assertEqual(p.returncode, 3, p.stderr)

    def test_os_prints_a_supported_name(self):
        p = run([self.install(), 'os'], self.root(LINUX))
        self.assertEqual((p.returncode, p.stdout), (0, 'linux\n'), p.stderr)

    def test_usage(self):
        for args in ([], ['start'], ['check', 'check']):
            p = run([self.install(), *args], self.root(LINUX))
            self.assertEqual((p.returncode, p.stdout), (2, ''))

    def test_native_windows_is_not_supported_yet(self):   # no os.geteuid there: exit 3, not a crash
        euid = os.geteuid
        with mock.patch.object(fw, 'detect', return_value='windows'), \
                mock.patch.dict(os.environ, {'FORK_FLEET_WALLS_ROOT': ''}):
            del os.geteuid
            try:
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(fw.main(['check']), 3)
                self.assertIn('this OS (windows) is not supported yet', err.getvalue())
                self.assertIsNone(fw.walls_file('linux'))
            finally:
                os.geteuid = euid

    def test_no_walls_is_exit_3_even_in_an_open_folder(self):   # a checkout with umask 002
        script = self.install({'linux': '#!/bin/sh\nexit 0\n'})
        os.chmod(os.path.dirname(script) + '/fork-fleet-walls', 0o775)
        p = run([script, 'check'], self.root(WSL1))
        self.assertEqual(p.returncode, 3, p.stderr)


class FakeRoot(Case):
    """FORK_FLEET_WALLS_ROOT is a test knob: never for root, and only a folder of this user that it alone can write."""
    def test_refused_for_root(self):
        root = self.root(LINUX)
        with mock.patch.dict(os.environ, {'FORK_FLEET_WALLS_ROOT': root}), mock.patch.object(os, 'geteuid',
                                                                                             return_value=0):
            with self.assertRaisesRegex(fw.Broken, 'for the tests only, never for root'):
                fw.fake_root()
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(fw.main(['check']), 2)
            self.assertIn('never for root', err.getvalue())

    def test_refused_unless_this_users_own_folder(self):
        def open_root(r): os.chmod(r, 0o777)
        def group_root(r): os.chmod(r, 0o775)
        def link_root(r): os.rename(r, r + '.real'); os.symlink(r + '.real', r)
        def file_root(r): shutil.rmtree(r); open(r, 'w').close()
        for change in (open_root, group_root, link_root, file_root):
            with self.subTest(change.__name__):
                root = self.root(LINUX)
                change(root)
                p = run([self.install(), 'check'], root)
                self.assertEqual((p.returncode, p.stdout), (2, ''), p.stderr)
        p = run([self.install(), 'check'], 'relative/root')
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn('not an absolute path', p.stderr)

    def test_said_on_stderr(self):
        root = self.root(LINUX)
        p = run([self.install(), 'os'], root)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"the walls of the fake machine {root!r}, not of this one", p.stderr)


class Contract(Case):
    """What fork-fleet-walls.py makes of a walls file's answer (a fake `linux` that prints OUT and exits CODE)."""
    def answer(self, verb, out, code, err=''):
        """A fake `linux` (walls os and kernel) that prints OUT to `describe` (and nothing to `check`)."""
        script = self.install({'linux': f'#!/bin/sh\nprintf %s {shlex(err)} >&2\n[ "$1" = describe ] && '
                                        f'printf %s {shlex(out)}\nexit {code}\n'}, {'linux': ['os', 'kernel']})
        return run([script, verb], self.root(LINUX))

    def test_answers(self):
        fail = 'FAIL  kernel  kernel 5.8 or newer: kernel 5.4\n'
        ok = 'PASS  os  a plain Linux kernel\n'
        good = ok + 'PASS  kernel  kernel 5.8 or newer\n'
        cases = [   # verb, stdout of describe, exit of the walls file -> exit of fork-fleet-walls.py, its stdout
            ('check', good, 0, 0, ''), ('describe', good, 0, 0, good),
            ('check', ok + fail, 1, 1, fail), ('describe', ok + fail, 1, 1, ok + fail),
            ('check', '', 0, 2, ''),             # it ran no wall (a cut-off or empty file): not a pass
            ('check', ok, 0, 2, ''),             # a wall of its list was not reported (stopped early, older file)
            ('check', fail, 1, 2, ''),           # no `os` wall
            ('check', good + 'PASS  extra  x\n', 0, 2, ''),   # a wall its list does not name
            ('check', ok + fail, 0, 2, ''),      # a failing wall with exit 0 is not a pass
            ('check', good, 1, 2, ''),           # exit 1 that names no wall
            ('check', ok + fail, 3, 2, ''),      # any other exit: a crash is not a FAIL list
            ('check', 'FAIL kernel x\n', 1, 2, ''), ('check', 'FAIL  Kernel  x\n', 1, 2, ''),
            ('check', 'all good\n', 0, 2, ''),
            ('check', ok + fail + fail, 1, 2, ''),   # a wall named twice
            ('describe', good + ok, 0, 2, ''),
            ('describe', ok + 'FAIL  kernel  a \x1b[2J b\n', 1, 2, ''),   # control characters: never raw
            ('describe', ok + 'FAIL  kernel  a \r b\n', 1, 2, ''),
            ('describe', ok + 'FAIL  kernel  a \u202e b\n', 1, 2, ''),
            ('describe', ok + 'FAIL  kernel  a \u200f b\n', 1, 2, ''),
            ('describe', ok + 'FAIL  kernel  a \u061c b\n', 1, 2, ''),
        ]
        for verb, out, code, want, stdout in cases:
            with self.subTest(verb=verb, out=out, code=code):
                p = self.answer(verb, out, code)
                self.assertEqual(p.returncode, want, p.stderr)
                self.assertEqual(p.stdout, stdout)
                self.assertNotRegex(p.stderr, '[\x1b\r\u202e\u200f\u061c]')

    def test_its_stderr_reaches_the_terminal_escaped(self):
        p = self.answer('check', 'PASS  os  x\nPASS  kernel  y\n', 0, err='a\x1b]0;title\x07 b\rc\n2nd line')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('a?]0;title? b?c\n2nd line', p.stderr)

    def test_a_cut_off_or_older_walls_file_is_refused(self):
        text = slurp(os.path.join(FOLDER, 'linux'))
        cut = text[:text.index("if __name__ == '__main__':")]   # an in-place copy that stopped at a line
        older = text.replace("    ('kernel', ", "    # ('kernel', ")   # a file older than its list
        bad = overlay(LINUX, {'proc/1/comm': 'init\n', '.dockerenv': ''})
        for name, walls in (('cut', cut), ('older', older), ('shebang only', '#!/usr/bin/python3 -I\n')):
            with self.subTest(name):
                script = self.install({'linux': walls}, {'linux': list(json.loads(
                    slurp(os.path.join(FOLDER, 'linux.fixture.json')))['fail'])})
                for verb in ('check', 'describe'):
                    p = run([script, verb], self.root(bad))
                    self.assertEqual((p.returncode, p.stdout), (2, ''), p.stderr)

    def test_timeout(self):
        script = self.install({'linux': '#!/bin/sh\nexec sleep 30\n'})
        old, fw.TIMEOUT = fw.TIMEOUT, 1
        self.addCleanup(setattr, fw, 'TIMEOUT', old)
        with self.assertRaisesRegex(fw.Broken, 'took more than 1 s'):
            fw.run_walls(os.path.join(os.path.dirname(script), 'fork-fleet-walls', 'linux'), 'check', '', {'os'})

    def test_it_gets_no_stdin_and_a_clean_environment(self):
        root = self.root(LINUX)
        script = self.install({'linux': '#!/bin/bash\nenv > "$FORK_FLEET_WALLS_ROOT/env.out"\ncat > '
                                        '"$FORK_FLEET_WALLS_ROOT/stdin.out"\necho "PASS  os  x"\n'})
        p = subprocess.run([script, 'check'], input='secret', capture_output=True, text=True,
                           env={'PATH': fw.PATH, 'FORK_FLEET_WALLS_ROOT': root, 'GH_TOKEN': 'x', 'PYTHONPATH': '/x'})
        self.assertEqual(p.returncode, 0, p.stderr)
        keys = {line.split('=')[0] for line in slurp(f'{root}/env.out').splitlines()} - {'PWD', 'SHLVL', '_'}
        self.assertEqual(keys, {'PATH', 'LC_ALL', 'FORK_FLEET_WALLS_ROOT'})
        self.assertEqual(slurp(f'{root}/stdin.out'), '')
        # the launcher's conf, when the caller set it, reaches the walls file (it hands it to the launcher's `paths`)
        p = run([script, 'check'], root, FORK_FLEET_LAUNCH_CONF='/etc/fork-fleet-proof/launch.conf', GH_TOKEN='x')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('FORK_FLEET_LAUNCH_CONF=/etc/fork-fleet-proof/launch.conf\n', slurp(f'{root}/env.out'))
        self.assertNotIn('GH_TOKEN', slurp(f'{root}/env.out'))
        for bad in ('etc/launch.conf', '/etc/../root/launch.conf', '/etc/a b', '/etc/x\n'):
            with self.subTest(bad):
                p = run([script, 'check'], root, FORK_FLEET_LAUNCH_CONF=bad)
                self.assertEqual((p.returncode, p.stdout), (2, ''), p.stderr)
                self.assertIn('FORK_FLEET_LAUNCH_CONF is not a plain absolute path', p.stderr)

    def test_the_os_wall_must_be_in_the_list(self):
        """A walls file and fixture that agree, and both without `os` (no proof this is that OS): refused."""
        script = self.install({'linux': '#!/bin/sh\necho "PASS  kernel  x"\n'}, {'linux': ['kernel']})
        p = run([script, 'check'], self.root(LINUX))
        self.assertEqual((p.returncode, p.stdout), (2, ''), p.stderr)
        self.assertIn('does not name the walls', p.stderr)

    def test_unsafe_walls_files_are_refused(self):
        def group_writable(f, d): os.chmod(f, 0o775)
        def not_executable(f, d): os.chmod(f, 0o644)
        def a_link(f, d): os.rename(f, f + '.real'); os.symlink(f + '.real', f)
        def open_folder(f, d): os.chmod(d, 0o777)
        def open_fixture(f, d): os.chmod(f + '.fixture.json', 0o664)
        def fixture_link(f, d): os.rename(f + '.fixture.json', f + '.x'); os.symlink(f + '.x', f + '.fixture.json')
        def no_fixture(f, d): os.remove(f + '.fixture.json')
        def fixture_not_json(f, d): write(f + '.fixture.json', '{"fail": {"os": [')
        def fixture_no_os(f, d): write(f + '.fixture.json', '{"fail": {"kernel": [{}]}}')
        def fixture_bad_name(f, d): write(f + '.fixture.json', '{"fail": {"os": [{}], "a  b": [{}]}}')
        launcher = lambda d: os.path.join(os.path.dirname(d), 'fork-fleet-launch.sh')
        def no_launcher(f, d): os.remove(launcher(d))
        def open_launcher(f, d): os.chmod(launcher(d), 0o775)
        def launcher_link(f, d): os.rename(launcher(d), launcher(d) + '.x'); os.symlink(launcher(d) + '.x', launcher(d))
        for change in (group_writable, not_executable, a_link, open_folder, open_fixture, fixture_link, no_fixture,
                       fixture_not_json, fixture_no_os, fixture_bad_name, no_launcher, open_launcher, launcher_link):
            with self.subTest(change.__name__):
                script = self.install({'linux': '#!/bin/sh\necho "PASS  os  x"\n'})
                folder = os.path.join(os.path.dirname(script), 'fork-fleet-walls')
                change(os.path.join(folder, 'linux'), folder)
                p = run([script, 'check'], self.root(LINUX))
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn('no runner starts here', p.stderr)


def shlex(text):
    return "'" + text.replace("'", "'\\''") + "'"


def fw_linux():
    """The linux walls file as a module, for a test of one of its functions."""
    loader = importlib.machinery.SourceFileLoader('fork_fleet_walls_linux', os.path.join(FOLDER, 'linux'))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def der(tag, content):
    """One DER element: its tag, its length (< 65536) and content."""
    n = len(content)
    return bytes([tag]) + (bytes([n]) if n < 0x80 else bytes([0x82, n >> 8, n & 0xff])) + content


def der_sequence(n):
    """A certificate's DER shape, n bytes of filler in it (n < 60000): a SEQUENCE of what is signed (a SEQUENCE), the
    algorithm (a SEQUENCE) and the signature (a BIT STRING)."""
    tbs = der(0x30, der(0x02, b'\x01') + der(0x04, bytes(i % 256 for i in range(n))))
    return der(0x30, tbs + der(0x30, der(0x06, b'\x2a\x86\x48')) + der(0x03, b'\x00' + b'\x11' * 16))


def pem(label, data, blank=False):
    """data in -----BEGIN label----- markers, as base64 lines; blank: as OpenPGP armors it (a blank line first and a
    checksum line last)."""
    body = base64.b64encode(data).decode()
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    lines = ['', *lines, '=AAAA'] if blank else lines
    return f'-----BEGIN {label}-----\n' + ''.join(x + '\n' for x in lines) + f'-----END {label}-----\n'


def ssh_public_line(kind='ssh-ed25519'):
    """An SSH public key line, its type and a blob of that type, no comment."""
    blob = len(kind).to_bytes(4, 'big') + kind.encode() + (32).to_bytes(4, 'big') + bytes(range(32))
    return f'{kind} {base64.b64encode(blob).decode()}'


def fixture_problems(fixture, walls):
    """What makes a fixture unfit for a walls file whose describe named walls: each wall needs a failing change."""
    fail = fixture.get('fail')
    if not isinstance(fail, dict) or sorted(fail) != sorted(walls):
        return [f'its `fail` names {sorted(fail or [])}, not the walls {sorted(walls)}']
    return [f'wall {wall} has no failing change' for wall, changes in fail.items()
            if not (changes if isinstance(changes, list) else [changes])
            or not all(isinstance(c, dict) and c for c in (changes if isinstance(changes, list) else [changes]))]


class WallsRunner(Case):
    """Runs the walls files of an installed copy (beside the launcher's allow list, with the modes an install gives)
    and checks each answer against the contract (fork-fleet-walls.py's docstring). The contract on every fixture is
    fork-fleet-walls-contract.test.py, so the test runner's workers run the two files side by side."""
    def setUp(self):
        super().setUp()
        self.folder = os.path.join(os.path.dirname(self.install()), 'fork-fleet-walls')

    def walls(self, name, verb, root):
        p = run([os.path.join(self.folder, name), verb], root)
        self.assertIn(p.returncode, (0, 1), f'{name} {verb}: exit {p.returncode}: {p.stderr}')
        marks = [fw.LINE.fullmatch(line) for line in p.stdout.splitlines()]
        self.assertTrue(all(marks), f'{name} {verb}: lines not in the contract: {p.stdout!r}')
        marks = [m.group(1, 2) for m in marks]
        ids = [w for _, w in marks]
        fails = [w for mark, w in marks if mark == 'FAIL']
        self.assertEqual(len(ids), len(set(ids)), f'{name} {verb}: a wall named twice')
        self.assertEqual(p.returncode, 1 if fails else 0, f'{name} {verb}: exit {p.returncode} with fails {fails}')
        if verb == 'check':
            self.assertEqual(ids, fails, f'{name} check printed a PASS line')
        return ids, fails

    def both(self, name, root):
        ids, fails = self.walls(name, 'describe', root)
        self.assertIn('os', ids, f'{name}: no wall `os`')
        self.assertEqual(self.walls(name, 'check', root)[1], fails, f'{name}: check and describe disagree')
        return ids, fails


class EveryWallsFile(WallsRunner):
    """The rest of the contract every walls file must pass, and the linux walls' own readings."""
    def test_there_is_a_linux_file(self):
        self.assertIn('linux', OSES)

    def test_folder_has_walls_files_and_their_fixtures_only(self):
        for entry in os.listdir(FOLDER):
            path = os.path.join(FOLDER, entry)
            with self.subTest(entry):
                if entry in OSES:
                    self.assertTrue(os.path.isfile(path) and not os.path.islink(path) and os.access(path, os.X_OK))
                    self.assertTrue(os.path.isfile(path + '.fixture.json'), f'{entry} has no {entry}.fixture.json')
                else:
                    self.assertRegex(entry, r'\.fixture\.json$')
                    self.assertIn(entry[:-len('.fixture.json')], OSES)

    def test_a_wall_without_a_failing_change_fails_the_contract(self):
        ids = ['os', 'mounts']
        self.assertEqual(fixture_problems({'fail': {'os': [{'a': 'b'}], 'mounts': [{'c': None}]}}, ids), [])
        for fail in ({'os': [{'a': 'b'}], 'mounts': []}, {'os': [{'a': 'b'}], 'mounts': [{}]},
                     {'os': [{'a': 'b'}], 'mounts': {}}, {'os': [{'a': 'b'}]}, {'os': [{'a': 'b'}], 'mounts': [1]}):
            with self.subTest(fail=fail):
                self.assertNotEqual(fixture_problems({'fail': fail}, ids), [])

    def test_python_walls_files_run_isolated(self):   # -I: nothing beside the file, nor PYTHON*, is imported
        for name in OSES:
            with self.subTest(name):
                first = slurp(os.path.join(FOLDER, name)).splitlines()[0]
                if 'python' in first:
                    self.assertEqual(first, '#!/usr/bin/python3 -I')
        self.assertEqual(slurp(SCRIPT).splitlines()[0], '#!/usr/bin/python3 -I')

    def test_linux_escapes_what_it_prints(self):   # a mount's fs type with ESC in it, as the cgroup2 wall prints it
        mountinfo = ('29 1 252:0 / / rw - ext4 /dev/vda1 rw\n'
                     '35 24 0:29 / /sys/fs/cgroup rw - cg\x1b[2J\u202eb\u200e\u200f\u061c x rw\n')
        root = self.root(overlay(LINUX, {'proc/self/mountinfo': mountinfo}))
        p = run([os.path.join(self.folder, 'linux'), 'describe'], root)
        self.assertEqual(p.returncode, 1, p.stderr)
        line = next(x for x in p.stdout.splitlines() if x.startswith('FAIL  cgroup2'))
        self.assertRegex(line, fw.LINE)
        self.assertIn('cg\\x1b[2J\\u202eb\\u200e\\u200f\\u061c', line)

    def test_linux_etc_secrets_reads_mount_units_and_the_login_environment(self):
        """Fix round 3 (#194 re-check S2): a share mounted by a systemd unit keeps its password in Options= (0644 as
        installed, so every job reads it), and /etc/environment and /etc/profile.d set every login's variables: a
        credential in either FAILS, as the same line in /etc/fstab does (the control)."""
        opts = 'username=me,password=hunter2,uid=1000'
        unit = f'[Mount]\nWhat=//nas/share\nWhere=/srv/nas\nType=cifs\nOptions={opts}\n'
        for name, change in (('fstab (control)', {'etc/fstab': f'//nas/share /srv/nas cifs {opts} 0 0\n'}),
                             ('mount unit', {'etc/systemd/system/srv-nas.mount': unit}),
                             ('automount drop-in', {'etc/systemd/system/srv-nas.automount.d/x.conf':
                                                    '[Mount]\nOptions=credentials=/etc/nas.txt\n',
                                                    'etc/nas.txt': 'username=me\npassword=hunter2\n'}),
                             ('environment', {'etc/environment': 'GITHUB_TOKEN="ghp_x"\n'}),
                             ('profile.d', {'etc/profile.d/x.sh': 'export AWS_SECRET_ACCESS_KEY=abc\n'})):
            with self.subTest(name):
                root = self.root(overlay(LINUX, change))
                self.assertIn('etc-secrets', self.walls('linux', 'describe', root)[1])
        for name, change in (('a mount unit only root reads', {'etc/systemd/system/srv-nas.mount':
                                                               {'text': unit, 'mode': '600'}}),
                             ('a variable named, not set', {'etc/profile.d/x.sh': 'export GH_TOKEN=$(cat ~/.t)\n'})):
            with self.subTest(name):
                root = self.root(overlay(LINUX, change))
                self.assertNotIn('etc-secrets', self.walls('linux', 'describe', root)[1])

    def test_linux_etc_secrets_reads_every_login_file_and_form(self):
        """Fix round 4 (#208 lenses S2 and C1): a secret every job can read in the login or session environment, in
        the places and forms a host has it: bash's, zsh's, csh's and pam_env's start files and systemd's
        environment.d; several assignments on a line, declare/typeset/readonly/export -n, a guard, a default in
        ${X:-...}; GitHub's PAT names; a password in a proxy URL; a known token's shape under any name (a comment
        too); a systemd unit's Environment= and an EnvironmentFile= others can read. Each FAILS; the forms with no
        literal secret still pass."""
        tok = 'ghp_' + 'FAKE' * 9   # a GitHub token's shape, obviously not one (built here: no scanner sees it whole)
        fails = {
            'environment.d': {'etc/environment.d/50-ci.conf': 'GH_TOKEN=x1\n'},
            'etc/profile': {'etc/profile': '\nexport GH_TOKEN=x1\n'},
            'etc/bash.bashrc': {'etc/bash.bashrc': '\nexport GH_TOKEN=x1\n'},
            'zsh': {'etc/zsh/zshenv': 'export GH_TOKEN=x1\n'},
            'pam_env': {'etc/security/pam_env.conf': 'GH_TOKEN DEFAULT=x1\n'},
            'two on a line': {'etc/profile.d/ci.sh': 'export EDITOR=vi GH_TOKEN=x1\n'},
            'declare -x': {'etc/profile.d/ci.sh': 'declare -x GH_TOKEN=x1\n'},
            'typeset -x': {'etc/profile.d/ci.sh': 'typeset -x GH_TOKEN=x1\n'},
            'readonly': {'etc/profile.d/ci.sh': 'readonly GH_TOKEN=x1\n'},
            'export -n': {'etc/profile.d/ci.sh': 'export -n GH_TOKEN=x1\n'},
            'guard ||': {'etc/profile.d/ci.sh': '[ -n "$GH_TOKEN" ] || export GH_TOKEN=x1\n'},
            'guard &&': {'etc/profile.d/ci.sh': '[ -z "$GH_TOKEN" ] && GH_TOKEN=x1\n'},
            'default :-': {'etc/profile.d/ci.sh': 'export GH_TOKEN="${GH_TOKEN:-x1}"\n'},
            'default :=': {'etc/profile.d/ci.sh': ': ${GH_TOKEN:=x1}\n'},
            'GH_PAT': {'etc/profile.d/ci.sh': 'GH_PAT=x1\n'},
            'GITHUB_PAT': {'etc/profile.d/ci.sh': 'export GITHUB_PAT=x1\n'},
            'proxy URL': {'etc/environment': 'https_proxy=http://me:hunter2@proxy.example:3128\n'},
            'token shape, other name': {'etc/profile.d/ci.sh': f'export MY_GH={tok}\n'},
            'token shape, a comment': {'etc/profile.d/ci.sh': f'# old: {tok}\n'},
            'csh setenv': {'etc/csh.cshrc': 'setenv GH_TOKEN x1\n'},
            'unit Environment=': {'etc/systemd/system/backup.service':
                                  '[Service]\nEnvironment=RESTIC_PASSWORD=hunter2\nExecStart=/usr/bin/restic\n'},
            'drop-in, quoted': {'etc/systemd/system/backup.service.d/env.conf':
                                '[Service]\nEnvironment="LANG=C" "RESTIC_PASSWORD=hunter 2"\n'},
            'user unit': {'etc/systemd/user/sync.service': '[Service]\nEnvironment=GH_TOKEN=x1\n'},
            'system.conf': {'etc/systemd/system.conf': '[Manager]\nDefaultEnvironment=GH_TOKEN=x1\n'},
            'EnvironmentFile': {'etc/systemd/system/backup.service': '[Service]\nEnvironmentFile=-/etc/backup.env\n',
                                'etc/backup.env': 'RESTIC_PASSWORD=hunter2\n'},
            'unit token shape': {'etc/systemd/system/x.service': f'[Service]\nExecStart=/usr/bin/x --auth {tok}\n'},
        }
        for name, change in fails.items():
            with self.subTest(name):
                root = self.root(overlay(LINUX, change))
                self.assertIn('etc-secrets', self.walls('linux', 'describe', root)[1])
        passes = {
            'from a command': {'etc/profile.d/ci.sh': 'export GH_TOKEN=$(cat ~/.t) EDITOR=vi\n'},
            'from a variable': {'etc/profile.d/ci.sh': 'export GH_TOKEN="$OTHER" PAT=${OTHER}\n'},
            'an empty default': {'etc/profile.d/ci.sh': 'export GH_TOKEN="${GH_TOKEN:-}"\n'},
            'a name in a comment': {'etc/profile.d/ci.sh': '# export GH_TOKEN=x1\n'},
            'PATH is no PAT': {'etc/profile.d/ci.sh': 'PATH=/x:$PATH\nPATTERN=a\n'},
            'proxy without a password': {'etc/environment': 'https_proxy=http://proxy.example:3128\n'},
            'proxy, password from a variable': {'etc/profile.d/ci.sh': 'export https_proxy=http://me:$PW@proxy:3128\n'},
            'unit, other names': {'etc/systemd/system/x.service': '[Service]\nEnvironment=LANG=C TZ=UTC\n'},
            'EnvironmentFile only root reads': {'etc/systemd/system/x.service': '[Service]\nEnvironmentFile=/etc/x.env\n',
                                                'etc/x.env': {'text': 'GH_TOKEN=x1\n', 'mode': '600'}},
            'an owner-only profile': {'etc/profile': {'text': 'export GH_TOKEN=x1\n', 'mode': '600'}},
        }
        for name, change in passes.items():
            with self.subTest(name):
                root = self.root(overlay(LINUX, change))
                self.assertNotIn('etc-secrets', self.walls('linux', 'describe', root)[1])

    def etc_secrets(self, change, then=None):
        """The linux etc-secrets line on the passing fixture with change, through describe; then(root), if given,
        changes the built root first (a link, a mode, raw bytes)."""
        root = self.root(overlay(LINUX, change))
        if then:
            then(root)
        p = run([os.path.join(self.folder, 'linux'), 'describe'], root)
        lines = [x for x in p.stdout.splitlines() if x.split('  ')[1:2] == ['etc-secrets']]
        self.assertEqual(len(lines), 1, p.stdout + p.stderr)
        return lines[0]

    def test_linux_etc_secrets_reads_systemd_unit_syntax(self):
        """Issue #211 (re-checks of #208, RS1 and the correctness lens): systemd reads a unit as an ini file, not one
        `Key=value` per line: blanks around the key's `=`, a value continued on the next line by a trailing backslash
        (a comment line inside it skipped), a literal SetCredential=, a LoadCredential= file others can read, and the
        EnvironmentFile= of a packaged unit (/usr/lib/systemd/system, enabled or started by a timer) that is in
        /etc/default. Each FAILS; the same forms with no secret pass."""
        u = '[Service]\nExecStart=/usr/bin/true\n'
        fails = {
            'Environment = (blanks)': {'etc/systemd/system/nightly.service':
                                       u + 'Environment = RESTIC_PASSWORD=hunter2\n'},
            'EnvironmentFile = (blanks)': {'etc/systemd/system/backup.service':
                                           u + 'EnvironmentFile = /etc/backup.env\n',
                                           'etc/backup.env': 'RESTIC_PASSWORD=hunter2\n'},
            'continued line': {'etc/systemd/system/nightly.service':
                               u + 'Environment=A=1 \\\n  RESTIC_PASSWORD=hunter2\n'},
            'continued, a comment inside': {'etc/systemd/system/nightly.service':
                                            u + 'Environment=A=1 \\\n# a note\n  RESTIC_PASSWORD=hunter2\n'},
            'SetCredential=': {'etc/systemd/system/nightly.service': u + 'SetCredential=restic.password:hunter2\n'},
            'LoadCredential= file others read': {'etc/systemd/system/nightly.service':
                                                 u + 'LoadCredential=restic.password:/etc/restic.pw\n',
                                                 'etc/restic.pw': 'hunter2\n'},
            'ManagerEnvironment=': {'etc/systemd/system.conf.d/x.conf': '[Manager]\nManagerEnvironment=GH_TOKEN=x1\n'},
            'vendor unit, timer, /etc/default': {
                'etc/default/nightly': 'RESTIC_PASSWORD=hunter2\n',
                'usr/lib/systemd/system/nightly.timer': '[Timer]\nOnCalendar=daily\n',
                'usr/lib/systemd/system/nightly.service': u + 'EnvironmentFile=/etc/default/nightly\n'},
            'vendor unit, -/etc/default': {
                'usr/lib/systemd/system/telegraf.service': u + 'EnvironmentFile=-/etc/default/telegraf\n',
                'etc/default/telegraf': 'INFLUX_TOKEN=hunter2hunter2\n'},
            'vendor unit, literal': {'usr/lib/systemd/system/x.service': u + 'Environment=GH_TOKEN=x1\n'},
        }
        for name, change in fails.items():
            with self.subTest(name):
                self.assertTrue(self.etc_secrets(change).startswith('FAIL'), name)
        passes = {
            'blanks, other names': {'etc/systemd/system/x.service': u + 'Environment = LANG=C TZ=UTC\n'},
            'continued, other names': {'etc/systemd/system/x.service': u + 'Environment=LANG=C \\\n  TZ=UTC\n'},
            'a continued comment is a comment': {'etc/systemd/system/x.service':
                                                 u + '# Environment=A=1 \\\nEnvironment=TZ=UTC\n'},
            'LoadCredential= file only root reads': {'etc/systemd/system/x.service':
                                                     u + 'LoadCredential=pw:/etc/restic.pw\n',
                                                     'etc/restic.pw': {'text': 'hunter2\n', 'mode': '600'}},
            'SetCredentialEncrypted=': {'etc/systemd/system/x.service': u + 'SetCredentialEncrypted=pw: \\\n  k6iUCUh0\n'},
            'EnvironmentFile= a specifier': {'etc/systemd/system/x@.service': u + 'EnvironmentFile=/etc/x/%i.env\n'},
            'vendor unit, /etc/default only root reads': {
                'usr/lib/systemd/system/telegraf.service': u + 'EnvironmentFile=-/etc/default/telegraf\n',
                'etc/default/telegraf': {'text': 'INFLUX_TOKEN=hunter2hunter2\n', 'mode': '600'}},
        }
        for name, change in passes.items():
            with self.subTest(name):
                self.assertTrue(self.etc_secrets(change).startswith('PASS'), name)

    def test_linux_etc_secrets_reads_fish_ansi_c_and_token_shapes_anywhere(self):
        """Issue #211: fish's config (`set -gx NAME value`), bash's $'...' quoting, and a known token's shape in any
        file of /etc others can read (/etc/default, fish's conf.d, a tool's own config), not only in the files listed
        by name. Each FAILS."""
        tok = 'ghp_' + 'FAKE' * 9
        fails = {
            'fish set -gx': {'etc/fish/conf.d/gh.fish': 'set -gx GH_TOKEN abc123secretvalue\n'},
            'fish config.fish': {'etc/fish/config.fish': 'set --export --global RESTIC_PASSWORD hunter2\n'},
            'fish, a token shape': {'etc/fish/conf.d/gh.fish': f'set -gx GH_TOKEN {tok}\n'},
            "$'...'": {'etc/profile.d/gh.sh': "export RESTIC_PASSWORD=$'hunter2'\n"},
            '$"..."': {'etc/profile.d/gh.sh': 'export RESTIC_PASSWORD=$"hunter2"\n'},
            'a token shape in /etc/default': {'etc/default/ci': f'GH_TOKEN={tok}\n'},
            'a name in /etc/default': {'etc/default/ci': 'GH_TOKEN=x1\n'},
            'a token shape in a tool config': {'etc/sometool/config.yaml': f'auth:\n  token: "{tok}"\n'},
        }
        for name, change in fails.items():
            with self.subTest(name):
                self.assertTrue(self.etc_secrets(change).startswith('FAIL'), name)
        passes = {
            'fish, from a command': {'etc/fish/conf.d/gh.fish': 'set -gx GH_TOKEN (cat ~/.t)\n'},
            'fish, from a variable': {'etc/fish/conf.d/gh.fish': 'set -gx GH_TOKEN $OTHER\n'},
            'fish, other names': {'etc/fish/config.fish': 'set -gx EDITOR vi\nset -U fish_greeting ""\n'},
            'a token shape only root reads': {'etc/sometool/config.yaml': {'text': f'token: {tok}\n', 'mode': '600'}},
        }
        for name, change in passes.items():
            with self.subTest(name):
                self.assertTrue(self.etc_secrets(change).startswith('PASS'), name)

    def test_linux_etc_secrets_names_are_words_not_substrings(self):
        """Issue #211 (re-checks of #208, RS2 and the correctness lens): a secret's name is a word of it that says so
        (GH_TOKEN, RESTIC_PASSWORD, OPENAI_API_KEY), not a substring (TOKENIZERS_PARALLELISM), and a value that is a
        file's path, an array or a yes/no holds no secret. Ubuntu's stock /etc/zsh/zshrc (zsh-common, on every host
        with zsh), /etc/environment and /etc/default files pass."""
        zshrc = ('if [[ "$TERM" != emacs ]]\nthen\n\n    typeset -A key\n    key=(\n'
                 '        BackSpace  "${terminfo[kbs]}"\n        Home       "${terminfo[khome]}"\n    )\nfi\n')
        passes = {
            'stock /etc/zsh/zshrc': {'etc/zsh/zshrc': zshrc},
            'TOKENIZERS_PARALLELISM': {'etc/environment': 'TOKENIZERS_PARALLELISM=false\n'},
            'a credentials path': {'etc/environment': 'GOOGLE_APPLICATION_CREDENTIALS=/opt/gcp/key.json\n',
                                   'opt/gcp/key.json': {'text': '{}\n', 'mode': '600'}},
            'a key file path': {'etc/profile.d/ssh.sh': 'export SSH_KEY=~/.ssh/id_ed25519 GPG_KEY=/etc/x.gpg\n',
                                'etc/x.gpg': {'text': 'x\n', 'mode': '600'}},
            'an id, not a key': {'etc/profile.d/aws.sh': 'export AWS_ACCESS_KEY_ID_FILE=/etc/x AWS_KEY_ID=AKIDX\n'},
            'a yes/no': {'etc/environment': 'GIT_TERMINAL_PROMPT_PASSWORD=false\nNO_TOKEN=1\n'},
            'lower case key, an array': {'etc/zsh/zshrc': 'typeset -A key\nkey=(Home x)\n'},
            'stock /etc/environment': {'etc/environment':
                                       'PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:'
                                       '/usr/local/games:/snap/bin"\n'},
            'stock /etc/default': {'etc/default/keyboard': 'XKBMODEL="pc105"\nXKBLAYOUT="us"\nBACKSPACE="guess"\n',
                                   'etc/default/locale': 'LANG=C.UTF-8\n',
                                   'etc/default/grub': 'GRUB_DEFAULT=0\nGRUB_CMDLINE_LINUX=""\n'},
        }
        for name, change in passes.items():
            with self.subTest(name):
                self.assertTrue(self.etc_secrets(change).startswith('PASS'), name)
        for name, change in {'a key, upper case': {'etc/profile.d/x.sh': 'export OPENAI_API_KEY=sk1\n'},
                             'TOKENS, a word': {'etc/profile.d/x.sh': 'export HF_TOKENS=x1\n'},
                             'a password in a word': {'etc/environment': 'DBPASSWORD=hunter2\n'},
                             'a lower case token': {'etc/profile.d/x.sh': 'github_token=x1\n'},
                             # PR #213 re-check RS N1: a path is no secret only when it is there and others cannot
                             # read it; one others can read holds the secret, and one that is not there may be a
                             # password that starts with /.
                             'a credentials path others can read': {
                                 'etc/environment': 'GOOGLE_APPLICATION_CREDENTIALS=/opt/gcp/key.json\n',
                                 'opt/gcp/key.json': '{}\n'},
                             'a key file path others can read': {'etc/profile.d/ssh.sh': 'export GPG_KEY=/etc/x.gpg\n',
                                                                 'etc/x.gpg': 'x\n'},
                             'a key path that is not there': {'etc/profile.d/ssh.sh': 'export GPG_KEY=/etc/x.gpg\n'},
                             }.items():
            with self.subTest(name):
                self.assertTrue(self.etc_secrets(change).startswith('FAIL'), name)

    def test_linux_etc_secrets_by_name_and_value_shape(self):
        """PR #213 lenses S4 and C1: a secret's name in any case and as one word (main's rule read them; the word rule
        of #211 lost db_pass, api_key and DJANGO_SECRETKEY) FAILS; a name whose secret word is followed by one that
        says the value is about it (DB_PASSWORD_FILE, TOKEN_TTL, PASSWORD_MIN_LENGTH, RESTIC_PASSWORD_COMMAND), a
        value that is systemd's %d path or a number, and fish's set -q and set -e (which set nothing) pass: the
        pattern systemd documents for credentials, which this wall's own advice points to, must not fail it.
        Re-check 2: a password command gives what it prints and any file others can read that it names, through a
        shell's -c, sed, grep or awk too (but its program, its script and a .gpg file); a PUBLIC KEY name, or a file of
        public keys and certificates only, is no secret, while a private key under any name still is."""
        CERT = pem('CERTIFICATE', der_sequence(300))
        PUBLIC_KEY = ('-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAGb9ECWmEzf6FQbrBZ9w7lshQhqowtrbLDFw4rXAxZuE=\n'
                      '-----END PUBLIC KEY-----\n')
        # A fake private key, its marker joined at run time so that no key-shaped text is in the repository (the
        # secret scan reads every commit).
        private = '-----{} PRIVATE KEY-----'
        PRIVATE_KEY = f'{private.format("BEGIN")}\nMC4CAQAw\n{private.format("END")}\n'
        pw = 'Hunter2-correct-horse'   # no known token's shape: found by its name only
        fails = {
            'lower case _pass': {'etc/profile.d/x.sh': f'db_pass={pw}\n'},
            'lower case _key': {'etc/profile.d/x.sh': f'export api_key={pw}\n'},
            'fish, lower case': {'etc/fish/conf.d/x.fish': f'set -gx smtp_pass {pw}\n'},
            'one word': {'etc/environment': f'DJANGO_SECRETKEY={pw}\n'},
            'secret word not last': {'etc/environment': f'SECRET_KEY_BASE={pw}\n'},
            'a long number': {'etc/environment': 'DB_PASSWORD=12345678\n'},
            'fish set after a query': {'etc/fish/conf.d/x.fish': f'set -q GH_TOKEN; or set -gx GH_TOKEN {pw}\n'},
            # PR #213 re-check RS N1: a shape clears a name only when the value truly is that kind.
            'a six-digit PIN': {'etc/environment': 'VNC_PASSWORD=482913\n'},
            'a password shaped like a path': {'etc/profile.d/app.sh': 'export DB_PASSWORD=/Xk9q2/Zp3m7\n'},
            'a password shaped like a specifier, in a login file': {
                'etc/profile.d/app.sh': 'export DB_PASSWORD=%Q/zP3m7Xk9\n'},
            'a password shaped like a specifier systemd has not': {
                'etc/systemd/system/app.service': '[Service]\nEnvironment=DB_PASSWORD=%Q/zP3m7Xk9\n'},
            'a literal password in a password command': {
                'etc/systemd/system/backup.service': f'[Service]\nEnvironment="RESTIC_PASSWORD_COMMAND=echo {pw}"\n'},
            'a password option in a password command': {
                'etc/profile.d/x.sh': f'export RESTIC_PASSWORD_COMMAND="tool --password={pw}"\n'},
            'a password variable in a token helper': {
                'etc/profile.d/x.sh': f'export VAULT_TOKEN_HELPER="env VAULT_TOKEN={pw} vault"\n'},
            'a cat of a file others can read, in a password command': {
                'etc/profile.d/x.sh': 'export RESTIC_PASSWORD_COMMAND="cat /etc/restic/pw"\n',
                'etc/restic/pw': f'{pw}\n'},
            'a password file others can read': {
                'etc/systemd/system/app.service': '[Service]\nEnvironment=DB_PASSWORD_FILE=/etc/app/db-password\n',
                'etc/app/db-password': f'{pw}\n'},
            'a password file others can read, by a specifier': {
                'etc/systemd/system/app@.service': '[Service]\nEnvironment=DB_PASSWORD_FILE=/etc/app/%i.pw\n',
                'etc/app/main.pw': f'{pw}\n'},
            # PR #213 re-check 2 (security N2): a command gives what it prints and what it reads, however it is run.
            **{f'a password command: {c}': {
                'etc/systemd/system/backup.service': f'[Service]\nEnvironment="RESTIC_PASSWORD_COMMAND={c}"\n',
                'etc/restic/pw': f'{pw}\n'} for c in (
                f"sh -c 'echo {pw}'", f"bash -c 'printf %s {pw}'", f"env -i sh -ec 'exec echo {pw}'",
                'sed -n 1p /etc/restic/pw', 'grep -m1 . /etc/restic/pw', 'awk NR==1 /etc/restic/pw',
                "sh -c 'tr -d x < /etc/restic/pw | head -1'", 'restic-pw --password-file=/etc/restic/pw', 'python3 -I -c pass /etc/restic/pw')},
            # PR #213 re-check 2 (correctness N1), the guard: only a PUBLIC key, or a file of public keys only, clears.
            'a private key file others can read': {
                'etc/systemd/system/app.service': '[Service]\nEnvironment=JWT_PRIVATE_KEY_FILE=/etc/app/jwt.pem\n',
                'etc/app/jwt.pem': PRIVATE_KEY},
            'a certificate with its private key after it': {
                'etc/systemd/system/app.service': '[Service]\nEnvironment=TLS_KEY_FILE=/etc/app/both.pem\n',
                'etc/app/both.pem': CERT + PRIVATE_KEY},
            'a public word that is not right before KEY': {
                'etc/environment': 'PUBLIC_API_KEY=Hunter2-correct-horse\nSECRET_PUBLIC_KEY=Hunter2-correct-horse\n'},
        }
        for name, change in fails.items():
            with self.subTest(name):
                self.assertTrue(self.etc_secrets(change).startswith('FAIL'), name)
        u = '[Service]\nExecStart=/usr/bin/app\n'
        passes = {
            'a credential by %d, as the advice says': {
                'etc/systemd/system/app.service':
                    u + 'LoadCredential=db:/etc/app/db-password\nEnvironment=DB_PASSWORD_FILE=%d/db\n',
                'etc/app/db-password': {'text': 'hunter2\n', 'mode': '600'}},
            'a password command': {
                'etc/systemd/system/backup.service':
                    u + 'LoadCredential=restic:/etc/restic/pw\n'
                        'Environment="RESTIC_PASSWORD_COMMAND=cat ${CREDENTIALS_DIRECTORY}/restic"\n',
                'etc/restic/pw': {'text': 'hunter2\n', 'mode': '600'}},
            'a time and a length': {'etc/environment.d/30-app.conf': 'TOKEN_TTL=3600\nPASSWORD_MIN_LENGTH=12\n'},
            'a %d value under a secret name': {'etc/systemd/system/app.service': u + 'Environment=DB_PASSWORD=%d/db\n'},
            'a short number': {'etc/environment': 'SMTP_PASS=0\n'},
            'fish: query and erase': {'etc/fish/conf.d/gh.fish':
                                      'set -q GH_TOKEN GITHUB_TOKEN; or echo no token\nset -e GH_TOKEN  # forget it\n'
                                      'set --erase GITHUB_TOKEN\nset -nS api_key x\n'},
            'csh setenv, a comment': {'etc/csh.cshrc': 'setenv GH_TOKEN # set by the login\n'},
            'words that only hold KEY or PASS': {'etc/environment': 'HOTKEY=x1\nBYPASS=x1\nKEYMAP=x1\n'},
            # PR #213 re-check RS N1, the other way: each shape that is truly its kind still passes.
            'a password file only root reads': {
                'etc/systemd/system/app.service': '[Service]\nEnvironment=DB_PASSWORD_FILE=/etc/app/db-password\n',
                'etc/app/db-password': {'text': 'hunter2\n', 'mode': '600'}},
            'a password file that is not here': {'etc/environment': 'DB_PASSWORD_FILE=/run/secrets/db\n'},
            'a password path only root reads': {'etc/environment': 'DB_PASSWORD=/etc/app/db-password\n',
                                                'etc/app/db-password': {'text': 'hunter2\n', 'mode': '600'}},
            'a path in a home folder': {'etc/profile.d/x.sh': 'export GH_TOKEN=~/.config/gh/token\n'},
            'a password command that reads a file only root reads': {
                'etc/profile.d/x.sh': 'export RESTIC_PASSWORD_COMMAND="cat /etc/restic/pw"\n',
                'etc/restic/pw': {'text': 'hunter2\n', 'mode': '600'}},
            'a password command that asks a tool': {
                'etc/profile.d/x.sh': 'export RESTIC_PASSWORD_COMMAND="pass show backup/restic"\n'
                                      'export GIT_PASSWORD_HELPER="secret-tool lookup app x"\n'},
            'an id file, not a key': {'etc/environment': 'AWS_ACCESS_KEY_ID_FILE=/etc/aws/id\n',
                                      'etc/aws/id': 'AKIDX\n'},
            # PR #213 re-check 2 (correctness N1): a public key is meant to be read.
            **{f'a public key: {line}': {'etc/systemd/system/app.service': f'[Service]\n{line}\n',
                                         'etc/app/jwt-public.pem': PUBLIC_KEY} for line in (
                'Environment=JWT_PUBLIC_KEY_FILE=/etc/app/jwt-public.pem',
                'Environment=OIDC_PUBLIC_KEY_PATH=/etc/app/jwt-public.pem',
                'Environment=SIGNING_PUBLIC_KEY=/etc/app/jwt-public.pem', 'Environment=SSH_PUB_KEY=x1')},
            'a certificate under a key name': {
                'etc/systemd/system/app.service': '[Service]\nEnvironment=TLS_KEY_FILE=/etc/app/ca.pem\n',
                'etc/app/ca.pem': '# the CA\n' + CERT + CERT},
            'an ssh public key under a key name': {
                'etc/environment': 'DEPLOY_KEY_FILE=/etc/app/deploy.pub\n',
                'etc/app/deploy.pub': ssh_public_line() + ' deploy\n'},
            # PR #213 re-check 2 (security N2), the other way: a command's own program, script or locked file.
            'a password command that runs a script others can read': {
                'etc/profile.d/x.sh': 'export RESTIC_PASSWORD_COMMAND="python3 -u /opt/app/get-pw.py --vault x"\n'
                                      'export GIT_PASSWORD_HELPER="/opt/app/get-pw.py"\n',
                'opt/app/get-pw.py': 'import vault\n'},
            'a password command that decrypts with a key jobs have not': {
                'etc/profile.d/x.sh': 'export RESTIC_PASSWORD_COMMAND="gpg -qd /etc/restic/pw.gpg"\n',
                'etc/restic/pw.gpg': 'x\n'},
        }
        for name, change in passes.items():
            with self.subTest(name):
                self.assertTrue(self.etc_secrets(change).startswith('PASS'), name)

    def test_linux_etc_secrets_reads_wildcards_specifiers_credstores_and_usr(self):
        """PR #213 lenses S5 and S8: systemd reads EnvironmentFile= as a wildcard, a template's path with a specifier
        (%i) on each instance, and a LoadCredential= by name from /etc/credstore; fish and systemd read the packages'
        login files in /usr (vendor_conf.d, /usr/lib/environment.d), which jobs see. A secret others can read in any
        of them FAILS; the same files only root reads pass."""
        pw = 'Hunter2-correct-horse'
        u = '[Service]\nExecStart=/usr/bin/app\n'
        fails = {
            'EnvironmentFile= a wildcard': {'etc/systemd/system/app.service': u + 'EnvironmentFile=/etc/app/*.env\n',
                                            'etc/app/app.env': f'DB_PASSWORD={pw}\n'},
            'EnvironmentFile= a specifier': {'etc/systemd/system/app@.service': u + 'EnvironmentFile=/etc/app/%i.env\n',
                                             'etc/app/main.env': f'DB_PASSWORD={pw}\n'},
            'LoadCredential= a specifier': {'etc/systemd/system/app@.service': u + 'LoadCredential=db:/etc/app/%i.pw\n',
                                            'etc/app/main.pw': f'{pw}\n'},
            '/etc/credstore': {'etc/systemd/system/app.service': u + 'LoadCredential=db-password\n',
                               'etc/credstore/db-password': f'{pw}\n'},
            '/usr/lib/credstore': {'usr/lib/credstore/db-password': f'{pw}\n'},
            'fish vendor_conf.d': {'usr/share/fish/vendor_conf.d/x.fish': f'set -gx DB_PASSWORD {pw}\n'},
            '/usr/lib/environment.d': {'usr/lib/environment.d/90-x.conf': f'DB_PASSWORD={pw}\n'},
        }
        for name, change in fails.items():
            with self.subTest(name):
                self.assertTrue(self.etc_secrets(change).startswith('FAIL'), name)
        passes = {
            'EnvironmentFile= a wildcard, root only': {
                'etc/systemd/system/app.service': u + 'EnvironmentFile=/etc/app/*.env\n',
                'etc/app/app.env': {'text': f'DB_PASSWORD={pw}\n', 'mode': '600'}},
            'a specifier naming nothing': {'etc/systemd/system/x@.service': u + 'EnvironmentFile=-/etc/x/%i.env\n'},
            '/etc/credstore, root only': {'etc/credstore/db-password': {'text': f'{pw}\n', 'mode': '600'}},
            'stock /usr/lib/environment.d': {'usr/lib/environment.d/990-snapd.conf':
                                             'PATH=$PATH:/snap/bin\nXDG_DATA_DIRS=${XDG_DATA_DIRS}:/snap/share\n'},
        }
        for name, change in passes.items():
            with self.subTest(name):
                self.assertTrue(self.etc_secrets(change).startswith('PASS'), name)

    def test_linux_job_units_reads_dropins_and_the_launchers_tools(self):
        """PR #213 lenses S1, C3 and S2: systemd adds every drop-in that applies to a job's unit (service.d, the
        fork-/fork-fleet- prefix folders, its own folder; in /etc, /run and /usr) to what the launcher's systemd-run
        asks, and a launch.conf SYSTEMD_RUN starts the units with another program: each turns a wall off where the
        launcher cannot see it, and FAILS job-units (the fixtures' failing changes have more). What changes no wall
        passes: a distro's timeout drop-in, the launcher's own IPAddressDeny (refresh-net's set-property), another
        service's drop-in, systemd's own programs."""
        safe = '# created by systemctl set-property\n[Service]\nIPAddressDeny=203.0.113.7/32\n'
        fails = {
            'set-property, a wall off': ({'run/systemd/system.control/fork-fleet-ci-1.service.d/50-x.conf':
                                          '[Service]\nPrivateDevices=no\n'}, {}),
            'attached, all services': ({'run/systemd/system.attached/service.d/x.conf':
                                        '[Service]\nRestrictAddressFamilies=\n'}, {}),
            'generator, a prefix': ({'run/systemd/generator/fork-fleet-ci-.service.d/x.conf':
                                     '[Service]\nNetworkNamespacePath=\n'}, {}),
            'a safe key, then an unsafe one': ({'usr/lib/systemd/system/service.d/10-timeout-abort.conf':
                                                '[Service]\nTimeoutStopFailureMode=abort\nProtectProc=default\n'}, {}),
            'FORK_FLEET_LAUNCH_CONF': ({'etc/fork-fleet-proof/launch.conf': 'SYSTEMCTL=/opt/x/systemctl\n'},
                                       {'FORK_FLEET_LAUNCH_CONF': '/etc/fork-fleet-proof/launch.conf'}),
            'a drop-in that cannot be read': ({'etc/systemd/system/service.d/x.conf': {'text': '', 'mode': '000'}},
                                              {}),
        }
        passes = {
            "a distro's timeout drop-in": {'usr/lib/systemd/system/service.d/10-timeout-abort.conf':
                                           '[Service]\nTimeoutStopFailureMode=abort\n'},
            "an IPAddressDeny that adds to the list": {
                'run/systemd/system.control/fork-fleet-ci-1.service.d/50-IPAddressDeny.conf': safe},
            "another service's drop-in": {'etc/systemd/system/ssh.service.d/x.conf': '[Service]\nPrivateDevices=no\n',
                                          'etc/systemd/system/forkd.service.d/x.conf': '[Service]\nUser=x\n'},
            'a description': {'etc/systemd/system/fork-fleet-.service.d/x.conf': '[Unit]\nDescription=CI job\n'},
            "systemd's own programs": {'etc/fork-fleet/launch.conf':
                                       'SYSTEMD_RUN=/usr/bin/systemd-run\nSYSTEMCTL=systemctl\n'},
        }
        for name, (change, env) in fails.items():
            with self.subTest(name):
                p = run([os.path.join(self.folder, 'linux'), 'describe'], self.root(overlay(LINUX, change)), **env)
                self.assertIn('FAIL  job-units', p.stdout, name)
        for name, change in passes.items():
            with self.subTest(name):
                self.assertNotIn('job-units', self.walls('linux', 'describe', self.root(overlay(LINUX, change)))[1])

    def launchers_deny(self):
        """The launcher's job unit IPAddressDeny ranges: PRIVATE4 and ::/0 (sandbox())."""
        text = slurp(LAUNCHER)
        m = re.search(r'^PRIVATE4=\(([^)]*)\)', text, re.M)
        self.assertIn('-p "IPAddressDeny=${PRIVATE4[*]} $own ::/0"', text)
        return m.group(1).split() + ['::/0']

    def test_linux_job_units_deny_list_is_the_launchers(self):
        """The ranges job-units wants after a reset (JOB_DENY) are the ones the launcher's sandbox() denies."""
        m = re.search(r'^JOB_DENY = (\(.*?\))$', slurp(os.path.join(FOLDER, 'linux')), re.M | re.S)
        self.assertEqual(sorted(ast.literal_eval(m.group(1))), sorted(self.launchers_deny()))

    def test_linux_job_units_stop_timeouts_are_the_launchers(self):
        """Issue #217: start() gives each job unit its own stop timeouts (JOB_STOP_TIMEOUTS), each at most STOP_MAX,
        so systemd's defaults (DefaultTimeoutStopSec=, DefaultTimeoutAbortSec=) decide nothing for the units it
        starts now."""
        linux = slurp(os.path.join(FOLDER, 'linux'))
        keys = ast.literal_eval(re.search(r'^JOB_STOP_TIMEOUTS = (\{.*?\})$', linux, re.M | re.S).group(1))
        stop_max = int(re.search(r'^STOP_MAX = (\d+)', linux, re.M).group(1))
        text = slurp(LAUNCHER)
        start = text[text.index('\nstart() {\n'):]
        got = dict(re.findall(r' -p (Timeout[A-Za-z]*)=(\S+)', start[:start.index('\n}\n')]))
        self.assertEqual(set(got), set(keys.values()))
        self.assertEqual(set(keys), {'DefaultTimeoutStopSec', 'DefaultTimeoutAbortSec'})
        for k, v in got.items():
            self.assertRegex(v, r'^[1-9][0-9]*$', k)
            self.assertLessEqual(int(v), stop_max, k)

    def test_linux_job_units_reads_resets_slices_and_stop_timeouts(self):
        """PR #213 re-checks (correctness N1; security N2, N3). systemd's own set-property drop-in (refresh-net's, in
        /run/systemd/transient for a transient unit) starts with an empty IPAddressDeny= and then lists the merged
        ranges: only what follows the last empty line counts, and it passes while that list still holds every range
        the launcher denies; a reset to a shorter list FAILS. A slice the job units sit in (system.slice, -.slice,
        every slice: slice.d) that allows more than localhost reopens the home network (allow beats deny, and a unit
        takes its slices' rules): FAILS. A stop timeout of infinity (or 0, or longer than STOP_MAX) lets a job that
        ignores SIGTERM run on past its limit: FAILS, in a drop-in or in systemd's own config.
        PR #213 re-check 2 (security N1, N3; correctness N2): systemd takes a list only in the section it belongs to
        ([Service], [Slice]) and drops it anywhere else, so a reset whose list sits in [X-Note] FAILS, and a reset that
        sits there passes. A reset passes only in set-property's own file for a running unit, and there only with all
        of that unit's own list (the host's own address too): one in /etc, in a prefix folder, for a unit that is not
        there, or one that drops the own address FAILS. systemd's main system.conf is the first of /etc, /run,
        /usr/local/lib and /usr/lib: an endless default in the one it reads FAILS."""
        header = ('# This is a drop-in unit file extension, created via "systemctl set-property"\n'
                  '# or an equivalent operation. Do not edit.\n')
        dropin = 'run/systemd/transient/fork-fleet-ci-1.service.d/50-IPAddressDeny.conf'

        def as_systemd_writes(prefixes):
            return header + '[Service]\nIPAddressDeny=\n' + ''.join(f'IPAddressDeny={p}\n' for p in prefixes) + '\n'
        deny = self.launchers_deny()
        own = ['198.51.100.7/32', '203.0.113.7/32']
        unit = {'run/systemd/transient/fork-fleet-ci-1.service':   # as systemd-run writes it, the own address in it
                '# This is a transient unit file, created programmatically via the systemd API. Do not edit.\n'
                '[Unit]\nDescription=CI job\n[Service]\nIPAddressDeny=\n' +
                ''.join(f'IPAddressDeny={p}\n' for p in deny[:-1] + own[:1] + deny[-1:]) + 'IPAddressAllow=localhost\n'}
        listed = ''.join(f'IPAddressDeny={p}\n' for p in deny)
        full = '[Service]\nIPAddressDeny=\n' + listed
        fails = {
            'a reset to only the own address': {dropin: as_systemd_writes(['203.0.113.7/32'])},
            'a reset that drops one range': {dropin: as_systemd_writes([d for d in deny if d != '100.64.0.0/10'])},
            'a reset that drops IPv6': {dropin: as_systemd_writes([d for d in deny if d != '::/0'])},
            'a full list, then a reset to less': {dropin: as_systemd_writes(deny) +
                                                  'IPAddressDeny=\nIPAddressDeny=10.0.0.0/8\n'},
            'a reset alone': {dropin: header + '[Service]\nIPAddressDeny=\n'},
            'system.slice allows any': {'etc/systemd/system/system.slice.d/10-lan.conf':
                                        '[Slice]\nIPAddressAllow=any\n'},
            '-.slice allows the home network': {'etc/systemd/system/-.slice.d/10-lan.conf':
                                                '[Slice]\nIPAddressAllow=192.168.0.0/16\n'},
            'every slice allows any, from /usr': {'usr/lib/systemd/system/slice.d/10-lan.conf':
                                                  '[Slice]\nIPAddressAllow=any\n'},
            'system.slice itself allows any': {'etc/systemd/system/system.slice':
                                               '[Slice]\nIPAddressAllow=link-local\n'},
            'a slice that sets a BPF program': {'run/systemd/system/system.slice.d/x.conf':
                                                '[Slice]\nIPIngressFilterPath=/sys/fs/bpf/x\n'},
            'an endless stop timeout': {'etc/systemd/system/service.d/10-slow.conf':
                                        '[Service]\nTimeoutStopSec=infinity\n'},
            'a stop timeout of 0': {'run/systemd/system/fork-fleet-.service.d/x.conf': '[Service]\nTimeoutSec=0\n'},
            'a stop timeout of a day': {'etc/systemd/system/service.d/x.conf': '[Service]\nTimeoutAbortSec=1d\n'},
            "systemd's default stop timeout endless": {**OLD_JOB, 'etc/systemd/system.conf.d/x.conf':
                                                       '[Manager]\nDefaultTimeoutStopSec=infinity\n'},
            "systemd's default, in system.conf, only root reads": {**OLD_JOB,
                'etc/systemd/system.conf': {'text': '[Manager]\nDefaultTimeoutStopSec=2h\n', 'mode': '600'}},
            # issue #217: a job unit's own stop timeout (start() sets one) beats systemd's default, not a drop-in's
            "a drop-in's 1h beats the job unit's own": {**NEW_JOB, 'etc/systemd/system/fork-fleet-.service.d/x.conf':
                                                         '[Service]\nTimeoutStopSec=1h\n'},
            "systemd's default abort endless, an older job unit": {**OLD_JOB, 'etc/systemd/system.conf':
                                                                   '[Manager]\nDefaultTimeoutAbortSec=infinity\n'},
            'a job unit with its own stop timeout but no abort one': {
                'run/systemd/transient/fork-fleet-ci-3.service': '[Service]\nTimeoutStopSec=1min 30s\n',
                'etc/systemd/system.conf': '[Manager]\nDefaultTimeoutAbortSec=1h\n'},
            # re-check 2: lists by section
            'a reset whose list sits in [X-Note]': {'etc/systemd/system/service.d/50-deny.conf':
                                                    '[Service]\nIPAddressDeny=\n[X-Note]\n' + listed},
            'a reset whose list sits in [Unit]': {**unit, dropin: '[Service]\nIPAddressDeny=\n[Unit]\n' + listed},
            'a reset whose list sits in [Install]': {**unit, dropin: '[Service]\nIPAddressDeny=\n[Install]\n' +
                                                     listed},
            'a slice allow whose reset sits in [X-Note]': {'etc/systemd/system/system.slice.d/10-lan.conf':
                                                           '[Slice]\nIPAddressAllow=any\n[X-Note]\nIPAddressAllow=\n'},
            # re-check 2: where a reset may be
            'a reset in /etc that drops the own address': {**unit, 'etc/systemd/system/service.d/10-deny.conf': full},
            "set-property's file, the own address dropped": {**unit, dropin: as_systemd_writes(deny)},
            'a reset in a prefix folder': {**unit, 'run/systemd/transient/fork-fleet-ci-.service.d/'
                                                   '50-IPAddressDeny.conf': as_systemd_writes(deny + own)},
            'a reset for a unit that is not there': {dropin: as_systemd_writes(deny + own)},
            # re-check 2: the main system.conf systemd reads
            **{f'an endless default in {p}': {**OLD_JOB, p: '[Manager]\nDefaultTimeoutStopSec=infinity\n'}
               for p in ('run/systemd/system.conf', 'usr/local/lib/systemd/system.conf', 'usr/lib/systemd/system.conf')},
        }
        passes = {
            "refresh-net's set-property, as systemd writes it": {**unit, dropin: as_systemd_writes(deny + own)},
            'a reset, then the list in another order': {**unit, dropin: as_systemd_writes(own + list(reversed(deny)))},
            'a reset, then the list in words': {**unit, dropin: as_systemd_writes(
                [d for d in deny if not d.startswith(('127.', '169.254.', '224.'))] +
                ['localhost', 'link-local', 'multicast'] + own)},
            'a reset where systemd drops it': {'etc/systemd/system/service.d/x.conf': '[X-Note]\nIPAddressDeny=\n'},
            'a slice allow where systemd drops it': {'etc/systemd/system/system.slice.d/x.conf':
                                                     '[Slice]\nIPAccounting=yes\n[X-Note]\nIPAddressAllow=any\n'},
            "/etc's system.conf before /usr's": {'etc/systemd/system.conf': '[Manager]\nDefaultTimeoutStopSec=90s\n',
                                                 'usr/lib/systemd/system.conf': '[Manager]\nDefaultTimeoutStopSec=infinity\n'},
            'a stock slice drop-in': {'usr/lib/systemd/system/system.slice.d/10-oomd.conf':
                                      '[Slice]\nManagedOOMMemoryPressure=kill\nIPAccounting=yes\n'},
            'a slice that allows only localhost': {'etc/systemd/system/system.slice.d/x.conf':
                                                   '[Slice]\nIPAddressAllow=localhost\n'},
            'a slice allow that is reset': {'etc/systemd/system/system.slice.d/x.conf':
                                            '[Slice]\nIPAddressAllow=any\nIPAddressAllow=\n'},
            "another slice's drop-in": {'etc/systemd/system/user.slice.d/x.conf': '[Slice]\nIPAddressAllow=any\n'},
            'a finite stop timeout': {'usr/lib/systemd/system/service.d/10-timeout.conf':
                                      '[Service]\nTimeoutStopSec=90s\nTimeoutAbortSec=3min\n'},
            "systemd's stock system.conf": {'etc/systemd/system.conf':
                                            '[Manager]\n#DefaultTimeoutStopSec=90s\nDefaultTimeoutStopSec=10s\n'},
            # issue #217: the launcher gives each job unit its own stop timeouts, so systemd's defaults decide nothing
            "systemd's defaults 1h, no job unit running": {'etc/systemd/system.conf': SLOW_DEFAULTS},
            "systemd's defaults 1h, a job unit with its own": {**NEW_JOB, 'etc/systemd/system.conf': SLOW_DEFAULTS},
            "systemd's defaults endless in a drop-in, a job unit with its own": {
                **NEW_JOB, 'etc/systemd/system.conf.d/x.conf': SLOW_DEFAULTS.replace('1h', 'infinity')},
            "set-property's drop-in folder beside a job unit": {
                **NEW_JOB,
                'run/systemd/transient/fork-fleet-ci-2.service.d/50-x.conf': '[Service]\nTimeoutStopSec=90\n'},
        }
        for name, change in fails.items():
            with self.subTest(name):
                p = run([os.path.join(self.folder, 'linux'), 'describe'], self.root(overlay(LINUX, change)))
                self.assertIn('FAIL  job-units', p.stdout, name + ': ' + p.stdout + p.stderr)
        for name, change in passes.items():
            with self.subTest(name):
                p = run([os.path.join(self.folder, 'linux'), 'describe'], self.root(overlay(LINUX, change)))
                self.assertIn('PASS  job-units', p.stdout, name + ': ' + p.stdout + p.stderr)

    def test_linux_job_units_reads_a_job_units_own_stop_timeouts_as_systemd(self):
        """Issue #297 item 4 (and the nit of item 5): a job unit's own stop timeout counts only as systemd reads its
        transient file: in [Service], and not reset by an empty one after it; an endless one of its own FAILS (as in a
        drop-in), and so do systemd's endless defaults when its own does not count."""
        endless = {'etc/systemd/system.conf': '[Manager]\nDefaultTimeoutStopSec=infinity\n'
                                              'DefaultTimeoutAbortSec=infinity\n'}
        unit = 'run/systemd/transient/fork-fleet-ci-4.service'
        fails = {
            'its own in [Unit]': {**endless, unit: TRANSIENT + '[Unit]\nTimeoutStopSec=90\nTimeoutAbortSec=90\n'
                                                               '[Service]\nRuntimeMaxSec=1h\n'},
            'its own reset by an empty one': {**endless, unit: TRANSIENT + '[Service]\nTimeoutStopSec=90\n'
                                                                           'TimeoutAbortSec=90\nTimeoutStopSec=\n'},
            'its own empty': {**endless, unit: TRANSIENT + '[Service]\nTimeoutStopSec=\nTimeoutAbortSec=90\n'},
            'its own endless': {unit: TRANSIENT + '[Service]\nTimeoutStopSec=infinity\nTimeoutAbortSec=90\n'},
            'its own abort a day': {unit: TRANSIENT + '[Service]\nTimeoutStopSec=90\nTimeoutAbortSec=1d\n'},
        }
        for name, change in fails.items():
            with self.subTest(name):
                got = self.job_units(change)
                self.assertTrue(got.startswith('FAIL'), got)
        got = self.job_units({**endless, unit: TRANSIENT + '[Service]\nTimeoutStopSec=\nTimeoutStopSec=90\n'
                                                           'TimeoutAbortSec=90\n'})
        self.assertTrue(got.startswith('PASS'), got)   # set again after the reset: its own

    def test_linux_job_units_skips_a_job_unit_that_ends_while_read(self):
        """Issue #297 item 1: a job unit's transient file goes when the unit ends (--collect). One that goes between
        the walls finding it and reading it leaves no default to judge: skipped (as reset_left does), not `cannot
        tell` (which refused the next start)."""
        m = fw_linux()
        root = self.root(overlay(LINUX, {**OLD_JOB, 'etc/systemd/system.conf': SLOW_DEFAULTS}))
        m.ROOT = root
        read = m.file_lines

        def ends_first(path, section=None):   # the unit is collected right after the walls saw its file
            if path.startswith('/run/systemd/transient/fork-fleet-ci-0.service'):
                os.remove(root + path)
            return read(path, section)
        with mock.patch.object(m, 'file_lines', ends_first):
            self.assertEqual(m.stop_defaults_used(), (set(), []))
        self.assertFalse(os.path.exists(os.path.join(root, 'run/systemd/transient/fork-fleet-ci-0.service')))

    def test_linux_job_units_checks_the_launchers_own_stop_timeouts(self):
        """Issue #297 item 3: the walls count systemd's defaults out only for units the installed launcher's start()
        gives stop timeouts of its own. Beside an older launcher (a kit not installed whole, whose start() set none),
        with no job unit loaded yet, an endless default FAILS (the next job unit would have it); a finite one, or the
        launcher's own, PASSES. A start() that sets an endless one FAILS, and one whose value cannot be read (an
        expansion) cannot be told."""
        real = slurp(LAUNCHER)
        both = ' -p TimeoutStopSec=90 -p TimeoutAbortSec=90 '
        self.assertEqual(real.count(both), 1)
        endless = {'etc/systemd/system.conf': '[Manager]\nDefaultTimeoutStopSec=infinity\n'}
        endless_abort = {'etc/systemd/system.conf': '[Manager]\nDefaultTimeoutAbortSec=infinity\n'}
        cases = [
            ('an older launcher, an endless default', real.replace(both, ' '), endless, 'FAIL'),
            ('an older launcher, an endless abort default', real.replace(both, ' '), endless_abort, 'FAIL'),
            ('a launcher without its abort one', real.replace(both, ' -p TimeoutStopSec=90 '), endless_abort, 'FAIL'),
            ('a launcher whose abort one is reset', real.replace(both, both + '-p TimeoutAbortSec= '), endless_abort,
             'FAIL'),
            ('a launcher that sets them only in a comment', real.replace(both, ' ').replace(
                '\n  # Type=exec:', '\n  # -p TimeoutStopSec=90 -p TimeoutAbortSec=90\n  # Type=exec:'), endless,
             'FAIL'),
            ('a launcher that sets them only in check-net', real.replace(both, ' ').replace(
                '\ncheck_net() {\n', '\ncheck_net() {\n  : -p TimeoutStopSec=90 -p TimeoutAbortSec=90\n'), endless,
             'FAIL'),
            ('a launcher that sets an endless one', real.replace(both, ' -p TimeoutStopSec=infinity '
                                                                 '-p TimeoutAbortSec=90 '), {}, 'FAIL'),
            ('a launcher that sets one through an expansion', real.replace(both, ' -p "TimeoutStopSec=$X" '
                                                                           '-p TimeoutAbortSec=90 '), {}, 'FAIL'),
            ('an older launcher, a finite default', real.replace(both, ' '), {}, 'PASS'),
            ('the launcher, an endless default', real, endless, 'PASS'),
            ('the launcher, quoted', real.replace(both, ' -p "TimeoutStopSec=90" -p \'TimeoutAbortSec=90\' '), endless,
             'PASS'),
        ]
        launcher = os.path.join(os.path.dirname(self.folder), 'fork-fleet-launch.sh')
        for name, text, change, want in cases:
            with self.subTest(name):
                write(launcher, text)
                got = self.job_units(change)
                self.assertTrue(got.startswith(want), got)
                if 'expansion' in name:
                    self.assertIn('cannot tell', got)
                elif want == 'FAIL':
                    self.assertIn("start()", got)
        os.chmod(launcher, 0o775)   # not as safe as ask() wants it: cannot tell, before anything is read from it
        write(launcher, real)
        got = self.job_units({})
        self.assertTrue(got.startswith('FAIL'), got)

    def job_units(self, change, links=()):
        """The linux job-units line on the passing fixture with change and links ((path, target)), through describe."""
        root = self.root(overlay(LINUX, change))
        for path, target in links:
            os.makedirs(os.path.dirname(os.path.join(root, path)), mode=0o755, exist_ok=True)
            os.symlink(target, os.path.join(root, path))
        p = run([os.path.join(self.folder, 'linux'), 'describe'], root)
        lines = [x for x in p.stdout.splitlines() if x.split('  ')[1:2] == ['job-units']]
        self.assertEqual(len(lines), 1, p.stdout + p.stderr)
        return lines[0]

    def test_linux_unit_lines_end_where_systemd_ends_them(self):
        """Issue #226 item 1: systemd ends a line at \\n, \\r (and \\r\\n) and \\0 only, strips only blanks, tabs and
        line ends, and splits a list at those alone. Python's splitlines() also ends one at \\x0b, \\x0c, \\x1c-\\x1e,
        \\x85, U+2028 and U+2029, and its split() and strip() take more: a root drop-in could hide a reset behind a
        header systemd reads as part of a comment, or make one word of a list read as many. Each FAILS job-units."""
        deny = self.launchers_deny()
        own = ['198.51.100.7/32', '203.0.113.7/32']
        unit = {'run/systemd/transient/fork-fleet-ci-1.service':
                '[Service]\nIPAddressDeny=\n' + ''.join(f'IPAddressDeny={p}\n' for p in deny[:-1] + own[:1] + deny[-1:])}
        dropin = 'run/systemd/transient/fork-fleet-ci-1.service.d/50-IPAddressDeny.conf'
        fails = {f'a reset after a comment that holds {c!r}': {
            'etc/systemd/system/service.d/50-x.conf': f'[Service]\n# n{c}[X-Note]\nIPAddressDeny=\n'}
            for c in ('\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\x85', ' ', ' ')}
        fails.update({
            'a header behind a NUL': {'etc/systemd/system/service.d/50-x.conf': '[X-Note]\0[Service]\nIPAddressDeny=\n'},
            'a header behind a CR': {'etc/systemd/system/service.d/50-x.conf': '[X-Note]\r[Service]\nIPAddressDeny=\n'},
            'a list in one word': {**unit, dropin: '[Service]\nIPAddressDeny=\nIPAddressDeny=' +
                                   '\x0c'.join(deny + own) + '\n'},
            'a key with a form feed before it': {**unit, dropin: '[Service]\nIPAddressDeny=\n\x0cIPAddressDeny=' +
                                                 ' '.join(deny + own) + '\n'},
            # PR #240 fix 1 (security 1): systemd skips a UTF-8 BOM at a file's start, so the header after it counts.
            'a header behind a BOM': {'etc/systemd/system/service.d/50-x.conf': '\ufeff[Service]\nIPAddressDeny=\n'},
            # PR #240 fix 2 (both lenses' item 1): systemd strips the BOM of the first line that has one, whichever.
            'a BOM on a later line, [Unit]': {
                'etc/systemd/system/service.d/50-x.conf': '[Unit]\n\ufeff[Service]\nIPAddressDeny=\n'},
            'a BOM on a later line, [X-Note]': {
                'etc/systemd/system/service.d/50-x.conf': '[X-Note]\n\ufeff[Service]\nIPAddressDeny=\n'},
            'a BOM on a later line, system.slice.d': {
                'etc/systemd/system/system.slice.d/50-x.conf': '[Unit]\n\ufeff[Slice]\nIPAddressAllow=any\n'},
            'a BOM on a later line, slice.d': {
                'etc/systemd/system/slice.d/50-x.conf': '[X-Note]\n\ufeff[Slice]\nIPAddressAllow=any\n'},
        })
        for name, change in fails.items():
            with self.subTest(name):
                self.assertTrue(self.job_units(change).startswith('FAIL'), name)
        self.assertEqual(fw_linux().unit_lines('[Service]\r\nA=1\r\n[X]\0B=2\rC=3\n', 'Service'), [(2, 'A', '1')])
        self.assertEqual(fw_linux().unit_lines('\ufeff[Service]\nA=1\n', 'Service'), [(2, 'A', '1')])
        self.assertEqual(fw_linux().unit_lines('[X]\n\ufeff[Service]\nA=1\n', 'Service'), [(3, 'A', '1')])
        # once per file, as systemd: a second BOM stays, so its line is no header (and no key systemd knows)
        self.assertEqual(fw_linux().unit_lines('\ufeff[X]\n\ufeff[Service]\nA=1\n', 'Service'), [])
        self.assertEqual(fw_linux().unit_lines('[Service]\n\ufeffA=1\n\ufeffB=2\n', 'Service'), [(2, 'A', '1'),
                                                                                                 (3, '\ufeffB', '2')])

    def test_linux_unit_lines_continue_on_an_odd_run_of_backslashes(self):
        """PR #240 fix 3 item 1 (correctness r2 finding 1): systemd continues a line only when the raw line ends in an
        odd run of backslashes with nothing after it (conf-parser.c); `x\\\\` and `x\\ ` end the line. Joining them
        would hide the next line's reset or allow: each FAILS job-units."""
        fails = ({'etc/systemd/system/service.d/50-x.conf': '[Service]\nTimeoutStartSec=90\\\\\nIPAddressDeny=\n'},
                 {'etc/systemd/system/service.d/50-x.conf': '[Service]\nTimeoutStartSec=90\\ \nIPAddressDeny=\n'},
                 {'etc/systemd/system/system.slice.d/50-x.conf': '[Slice]\nDescription=x\\\\\nIPAddressAllow=any\n'})
        for change in fails:
            with self.subTest(change):
                got = self.job_units(change)
                self.assertTrue(got.startswith('FAIL'), got)
        lines = fw_linux().unit_lines
        self.assertEqual(lines('[Service]\nA=x\\\\\nB=2\n', 'Service'), [(2, 'A', 'x\\\\'), (3, 'B', '2')])
        self.assertEqual(lines('[Service]\nA=x\\ \nB=2\n', 'Service'), [(2, 'A', 'x\\'), (3, 'B', '2')])
        self.assertEqual(lines('[Service]\nA=x\\\nB=2\n', 'Service'), [(2, 'A', 'x B=2')])
        self.assertEqual(lines('[Service]\nA=x\\\\\\\n# c\nB=2\n', 'Service'), [(2, 'A', 'x\\\\ B=2')])
        # the same in a secret: the second line is its own Environment= and is read
        got = self.etc_secrets({'etc/systemd/system/b.service':
                                '[Service]\nDescription=x\\\\\nEnvironment=RESTIC_PASSWORD=hunter2\n'})
        self.assertTrue(got.startswith('FAIL'), got)

    def test_linux_unit_forms_the_walls_cannot_read_fail_closed(self):
        """PR #240 fix 3 items 2-4 and 6 (security r2 findings 1-4, correctness r2 finding 2): a source path that
        starts with a specifier is expanded (LoadCredential=x:%E/..., EnvironmentFile=%E/...); a specifier that is
        many folders (%Y %y %S %C %L %V) is its real folder, one the walls cannot know (%h, %I) fails closed, as does
        any other relative source; a bare word is read from WorkingDirectory=, and env -C, --chdir and cd fail closed;
        any backslash (a systemd escape: p\\x77, \\s) in an Environment= item fails closed. Each FAILS etc-secrets,
        naming the line; the plain forms of the same, with the file private, pass."""
        pw = 'Hunter2-correct-horse\n'
        env = 'RESTIC_PASSWORD=Hunter2-correct-horse\n'
        cases = {'LoadCredential=restic:%E/restic/pw': {'etc/restic/pw': pw},
                 'EnvironmentFile=%E/restic/env': {'etc/restic/env': env},
                 'EnvironmentFile=-%E/restic/env': {'etc/restic/env': env},
                 'Environment=RESTIC_PASSWORD_FILE=%Y/restic.pw': {'etc/systemd/system/restic.pw': pw},
                 'Environment="RESTIC_PASSWORD_COMMAND=cat %Y/restic.pw"': {'etc/systemd/system/restic.pw': pw},
                 'Environment=RESTIC_PASSWORD_FILE=%S/restic/pw': {'var/lib/restic/pw': pw},
                 'Environment=RESTIC_PASSWORD_FILE=%L/restic/pw': {'var/log/restic/pw': pw},
                 'Environment=RESTIC_PASSWORD_FILE=%h/restic/pw': {},
                 'Environment=RESTIC_PASSWORD_FILE=/etc/%I/pw': {},
                 'LoadCredential=restic:etc/restic/pw': {},
                 'EnvironmentFile=restic.env': {},
                 'WorkingDirectory=/etc/restic\nEnvironment="RESTIC_PASSWORD_COMMAND=cat pw"': {'etc/restic/pw': pw},
                 'WorkingDirectory=~\nEnvironment="RESTIC_PASSWORD_COMMAND=cat pw"': {},
                 'Environment="RESTIC_PASSWORD_COMMAND=env -C /etc/restic cat pw"': {'etc/restic/pw': pw},
                 'Environment="RESTIC_PASSWORD_COMMAND=env --chdir=/etc/restic cat pw"': {'etc/restic/pw': pw},
                 'Environment="RESTIC_PASSWORD_COMMAND=cd /etc/restic && cat pw"': {'etc/restic/pw': pw},
                 'Environment=RESTIC_PASSWORD_FILE=/etc/restic/p\\x77': {'etc/restic/pw': pw},
                 'Environment=RESTIC_PASSWORD_COMMAND=cat\\s/etc/restic/pw': {'etc/restic/pw': pw}}
        for line, files in cases.items():
            with self.subTest(line):
                got = self.etc_secrets({**files, 'etc/systemd/system/b.service': f'[Service]\n{line}\n'})
                self.assertTrue(got.startswith('FAIL'), got)
        # a WorkingDirectory= in a drop-in counts for the unit's commands too
        got = self.etc_secrets({'etc/restic/pw': pw,
                                'etc/systemd/system/b.service.d/x.conf': '[Service]\nWorkingDirectory=/etc/restic\n',
                                'etc/systemd/system/b.service':
                                '[Service]\nEnvironment="RESTIC_PASSWORD_COMMAND=cat pw"\n'})
        self.assertTrue(got.startswith('FAIL'), got)
        got = self.etc_secrets({'etc/systemd/system/b.service': '[Service]\nEnvironment=RESTIC_PASSWORD_FILE=%h/pw\n'})
        self.assertIn('line 2', got)
        private = {'text': pw, 'mode': '600'}
        passes = {'LoadCredential=restic:%E/restic/pw': {'etc/restic/pw': private},
                  'EnvironmentFile=%E/restic/env': {'etc/restic/env': {'text': env, 'mode': '600'}},
                  'Environment=RESTIC_PASSWORD_FILE=%Y/restic.pw': {'etc/systemd/system/restic.pw': private},
                  'WorkingDirectory=/etc/restic\nEnvironment="RESTIC_PASSWORD_COMMAND=cat pw"':
                  {'etc/restic/pw': private},
                  'Environment="RESTIC_PASSWORD_COMMAND=cat /etc/restic/pw"': {'etc/restic/pw': private}}
        for line, files in passes.items():
            with self.subTest(line):
                got = self.etc_secrets({**files, 'etc/systemd/system/b.service': f'[Service]\n{line}\n'})
                self.assertTrue(got.startswith('PASS'), got)

    def test_linux_password_command_glued_paths_bytes_links_and_credentials(self):
        """PR #240 fix 3 items 5, 7-9: a path glued to an option (curl -sK/etc/restic/pw) or relative after : or @
        (-pass file:etc/restic/pw) is read; a word that is not valid UTF-8 fails closed; a link in a private folder to
        a file others can reach FAILS (the folders above the real file count); and in a unit
        ${CREDENTIALS_DIRECTORY}/name is the unit's own credential, not /name."""
        pw = 'Hunter2-correct-horse\n'
        for c in ('curl -sK/etc/restic/pw https://x', 'openssl enc -d -pass file:etc/restic/pw',
                  'tool --key @etc/restic/pw'):
            with self.subTest(c):
                got = self.etc_secrets({'etc/restic/pw': pw,
                                        'etc/profile.d/x.sh': f'export RESTIC_PASSWORD_COMMAND={shlex(c)}\n'})
                self.assertTrue(got.startswith('FAIL'), got)

        def raw_name(root):   # cat /etc/restic/p<0xff>w: python reads the byte as U+FFFD
            with open(os.path.join(root, 'etc/profile.d/x.sh'), 'wb') as f:
                f.write(b'export RESTIC_PASSWORD_COMMAND="cat /etc/restic/p\xffw"\n')
            with open(os.path.join(root.encode(), b'etc/restic/p\xffw'), 'wb') as f:
                f.write(pw.encode())
            os.chmod(os.path.join(root.encode(), b'etc/restic/p\xffw'), 0o644)
        with self.subTest('not UTF-8'):
            got = self.etc_secrets({'etc/restic/': '', 'etc/profile.d/x.sh': '\n'}, raw_name)
            self.assertTrue(got.startswith('FAIL'), got)

        def link(folder_mode, target_mode):
            def then(root):
                os.symlink('../restic-real/pw', os.path.join(root, 'etc/restic/pw'))
                os.chmod(os.path.join(root, 'etc/restic'), folder_mode)
                os.chmod(os.path.join(root, 'etc/restic-real'), target_mode)
            return then
        change = {'etc/restic/': '', 'etc/restic-real/pw': pw,
                  'etc/profile.d/x.sh': 'export RESTIC_PASSWORD_FILE=/etc/restic/pw\n'}
        with self.subTest('a private link to a public file'):
            got = self.etc_secrets(change, link(0o700, 0o755))
            self.assertTrue(got.startswith('FAIL'), got)
        with self.subTest('a public link to a private file'):
            got = self.etc_secrets(change, link(0o755, 0o700))
            self.assertTrue(got.startswith('PASS'), got)
        for c in ('cat ${CREDENTIALS_DIRECTORY}/restic', 'cat $CREDENTIALS_DIRECTORY/restic', 'cat %d/restic'):
            with self.subTest(c):   # a host with a readable /restic (a repository, say)
                got = self.etc_secrets({'restic': pw, 'etc/systemd/system/b.service':
                                        '[Service]\nEnvironment=' + shlex(f'RESTIC_PASSWORD_COMMAND={c}') + '\n'})
                self.assertTrue(got.startswith('PASS'), got)

    def test_linux_password_command_hash_inside_a_word(self):
        """PR #240 fix 4 item 2 (security r3 finding 1, correctness r3 finding 3): a # inside a word is no comment
        to a shell (cat /x#y reads the file /x#y), so the walls read the words after it too; in a login file and in a
        unit's Environment= both. A private file with a # in its name still passes."""
        pw = 'Hunter2-correct-horse\n'
        for command in ('cat /x#y /etc/restic/pw', 'cat a#b /etc/restic/pw', 'true a#b; cat /etc/restic/pw'):
            for where, files in (('login', {'etc/profile.d/x.sh': f'export RESTIC_PASSWORD_COMMAND="{command}"\n'}),
                                 ('unit', {'etc/systemd/system/b.service':
                                           f'[Service]\nEnvironment="RESTIC_PASSWORD_COMMAND={command}"\n'})):
                with self.subTest(command, where=where):
                    got = self.etc_secrets({'etc/restic/pw': pw, **files})
                    self.assertTrue(got.startswith('FAIL'), got)
        got = self.etc_secrets({'etc/restic/backup#2.pw': {'text': pw, 'mode': '600'},
                                'etc/profile.d/x.sh': 'export RESTIC_PASSWORD_COMMAND="cat /etc/restic/backup#2.pw"\n'})
        self.assertTrue(got.startswith('PASS'), got)

    def test_linux_system_conf_a_dangling_link_is_skipped(self):
        """Issue #226 item 3: systemd 256 and later reads the first system.conf of /etc, /run, /usr/local/lib and
        /usr/lib that is there; a dangling link is not, so it reads the next one (an endless default there FAILS),
        while a link to /dev/null masks all after it (passes)."""
        endless = {**OLD_JOB, 'usr/lib/systemd/system.conf': '[Manager]\nDefaultTimeoutStopSec=infinity\n'}
        got = self.job_units(endless, [('etc/systemd/system.conf', '/nonexistent/system.conf')])
        self.assertTrue(got.startswith('FAIL'), got)
        self.assertIn('/usr/lib/systemd/system.conf', got)
        got = self.job_units(endless, [('etc/systemd/system.conf', '/dev/null')])
        self.assertTrue(got.startswith('PASS'), got)
        # PR #240 fix 1 (security 6): a system.conf that is a folder must not end the search unseen: cannot tell.
        for change in ({**endless, 'etc/systemd/system.conf/': ''}, {'etc/systemd/system.conf/': ''}):
            with self.subTest(change):
                got = self.job_units(change)
                self.assertTrue(got.startswith('FAIL'), got)
                self.assertIn('/etc/systemd/system.conf is there but is no file', got)

    def test_linux_same_named_dropins_only_the_one_systemd_applies(self):
        """Issue #226 item 7: of drop-ins with one name, systemd applies only the one in the first folder of its search
        path (and there the most specific): a safe one, or a link to /dev/null, in /etc masks one in /usr that would
        fail. It masks for every job unit only from a folder that applies to every job unit (service.d, fork-.service.d,
        fork-fleet-.service.d): one in a single unit's folder leaves the others the one in /usr. PR #240 fix 1
        (correctness 1): systemd lists every dir's own and prefix folders before any dir's service.d
        (src/shared/dropin.c, unit_file_find_dropin_paths), so service.d in /etc never shadows a same-named file in a
        prefix or own folder in /run or /usr, and a prefix folder's file in /usr shadows service.d in /etc."""
        bad = {'usr/lib/systemd/system/service.d/10-x.conf': '[Service]\nPrivateDevices=no\n'}
        safe = '[Service]\nTimeoutStopFailureMode=abort\n'
        passes = {
            'a safe one in /etc, the same folder': ({**bad, 'etc/systemd/system/service.d/10-x.conf': safe}, ()),
            'a safe one in /run, a folder for every job unit': (   # a prefix folder: before every service.d
                {**bad, 'run/systemd/system/fork-fleet-.service.d/10-x.conf': safe}, ()),
            'a safe one in /usr, a prefix folder, before service.d in /etc': (
                {'etc/systemd/system/service.d/10-x.conf': '[Service]\nPrivateDevices=no\n',
                 'usr/lib/systemd/system/fork-.service.d/10-x.conf': safe}, ()),
            'a link to /dev/null in /etc': (bad, [('etc/systemd/system/service.d/10-x.conf', '/dev/null')]),
        }
        fails = {
            'a safe one in /usr, an unsafe one in /etc': (
                {'usr/lib/systemd/system/service.d/10-x.conf': safe,
                 'etc/systemd/system/service.d/10-x.conf': '[Service]\nPrivateDevices=no\n'}, ()),
            "a safe one in one unit's folder": ({**bad, 'etc/systemd/system/fork-fleet-ci-1.service.d/10-x.conf': safe},
                                                ()),
            'another name': ({**bad, 'etc/systemd/system/service.d/20-x.conf': safe}, ()),
        }
        for d in ('usr/lib/systemd/system/fork-.service.d', 'run/systemd/system/fork-fleet-.service.d',
                  'usr/lib/systemd/system/fork-fleet-ci-1.service.d'):   # the correctness lens's own test
            fails[f'an unsafe one in {d}, a safe one in /etc service.d'] = (
                {f'{d}/10-x.conf': '[Service]\nPrivateDevices=no\n', 'etc/systemd/system/service.d/10-x.conf': safe}, ())
        for name, (change, links) in passes.items():
            with self.subTest(name):
                got = self.job_units(change, links)
                self.assertTrue(got.startswith('PASS'), got)
        for name, (change, links) in fails.items():
            with self.subTest(name):
                got = self.job_units(change, links)
                self.assertTrue(got.startswith('FAIL'), got)

    def test_linux_dropin_folder_that_cannot_be_listed(self):
        """PR #240 fix 1 (correctness 3): a drop-in folder that is there but cannot be listed (mode 0311: it may be
        entered, not read) may hold anything: cannot tell, not an empty folder."""
        for folder in ('etc/systemd/system/service.d', 'run/systemd/system/fork-fleet-.service.d',
                       'usr/lib/systemd/system/slice.d'):
            with self.subTest(folder):
                root = self.root(overlay(LINUX, {f'{folder}/10-x.conf': '[Service]\nPrivateDevices=no\n'}))
                os.chmod(os.path.join(root, folder), 0o311)
                self.addCleanup(os.chmod, os.path.join(root, folder), 0o755)
                p = run([os.path.join(self.folder, 'linux'), 'describe'], root)
                got = [x for x in p.stdout.splitlines() if x.split('  ')[1:2] == ['job-units']]
                self.assertEqual(len(got), 1, p.stdout + p.stderr)
                self.assertRegex(got[0], rf'^FAIL .*cannot tell .*cannot list /{folder}')

    def test_linux_password_commands_read_redirects_and_inline_scripts(self):
        """Issue #226 item 2 (a regression against main before #213, which failed every literal *_COMMAND): a
        password command gives a file it reads by a redirect (cat</etc/restic/pw, head -n1 </etc/restic/pw), the text
        of a here-string (<<<text), and whatever an inline script reads (python3 -c, perl -e, awk 'BEGIN{...}',
        sh -c 'read p </file'): such a script may build the path from parts, so it fails closed, as main did."""
        pw = 'Hunter2-correct-horse'
        cmds = ('cat</etc/restic/pw', 'head -n1 </etc/restic/pw', 'head -n1 0</etc/restic/pw', f'cat <<<{pw}',
                f'tr -d x<<<{pw}', '''python3 -c 'print(open("/etc/restic/pw").read())' ''',
                '''perl -e 'print <STDIN>' ''', '''perl -ne 'print' ''', '''awk 'BEGIN{getline p<"/etc/restic/pw"; print p}' ''',
                '''sh -c 'read p </etc/restic/pw; echo "$p"' ''', '''ruby -e 'puts File.read(%q(/etc/restic/pw))' ''',
                '''node -e 'x' ''', '''timeout 5 python3 -c 'x' ''', "sed '1r /etc/restic/pw' /dev/null",
                '''sh -c "sh -c 'sh -c \\"sh -c \\\\\\"sh -c x\\\\\\"\\"'"''', "cat 'unbalanced",
                # PR #240 fix 1 (correctness 2, security 2): a word whose file only a shell knows fails closed (a
                # glob, a variable, ${IFS}, a brace, ~user); a quoted path with a blank, a non-ASCII one and python's
                # joined -m module (no script: its words are its input) are read.
                'cat /etc/restic/p*', 'cat /etc/restic/p?', 'cat /etc/restic/[p]w', 'p=/etc/restic; cat $p/pw',
                'f=/etc/restic/pw; cat "$f"', 'cat${IFS}/etc/restic/pw', 'cat /etc/restic/{pw,x}', 'cat ~backup/pw',
                'cat "/etc/restic/my pw"', 'cat /etc/restic/pässwort', 'python3 -mbase64 -d /etc/restic/pw.b64',
                'python3 -Im base64 -d /etc/restic/pw.b64')
        for c in cmds:
            with self.subTest(c):
                change = {'etc/profile.d/x.sh': f'export RESTIC_PASSWORD_COMMAND={shlex(c.strip())}\n',
                          'etc/restic/pw': f'{pw}\n', 'etc/restic/my pw': f'{pw}\n', 'etc/restic/pässwort': f'{pw}\n',
                          'etc/restic/pw.b64': base64.b64encode(pw.encode()).decode() + '\n'}
                got = self.etc_secrets(change)
                self.assertTrue(got.startswith('FAIL'), got)
        passes = ('cat </etc/restic/pw', 'cat </etc/restic/pw.gpg', 'pass show backup/restic 2>/dev/null',
                  'python3 -u /opt/app/get-pw.py --vault x', 'cat "/etc/restic/my pw"', 'cat /etc/restic/pässwort',
                  'python3 -mbase64 -d /etc/restic/pw')
        for c in passes:
            with self.subTest(c):
                change = {'etc/profile.d/x.sh': f'export RESTIC_PASSWORD_COMMAND={shlex(c)}\n',
                          'etc/restic/pw': {'text': f'{pw}\n', 'mode': '600'}, 'etc/restic/pw.gpg': 'x\n',
                          'etc/restic/my pw': {'text': f'{pw}\n', 'mode': '600'},
                          'etc/restic/pässwort': {'text': f'{pw}\n', 'mode': '600'},
                          'opt/app/get-pw.py': 'import vault\n'}
                got = self.etc_secrets(change)
                self.assertTrue(got.startswith('PASS'), got)

    def test_linux_password_command_paths_relative_odd_and_credentials(self):
        """PR #240 fix 2 (security 2-4, correctness 2-3): a password command's path is read whatever its form: from /
        when relative (cat etc/restic/pw), to the word's end past a character a plain path has not (pass,word; a
        blank), after an = or an @; one with .. in it fails closed. $CREDENTIALS_DIRECTORY (and %d) only in a unit,
        the rest of the word read as a path from / (it is empty where a job runs the command)."""
        pw = 'Hunter2-correct-horse'
        files = {'etc/restic/pw': f'{pw}\n', 'etc/restic/my pw': f'{pw}\n', 'etc/restic/pass,word': f'{pw}\n'}
        login = ('cat $CREDENTIALS_DIRECTORY/etc/restic/pw', 'cat $CREDENTIALS_DIRECTORY/../../../etc/restic/pw',
                 'cat ${CREDENTIALS_DIRECTORY}/restic', 'cat etc/restic/pw', 'cat ../../../../../../../etc/restic/pw',
                 'cat /etc/restic/pass,word', 'tool --password-file="/etc/restic/my pw"',
                 'cat -- "x/../etc/restic/my pw"', 'curl -sd @/etc/restic/pw https://x', 'dd if=etc/restic/pw',
                 'cat ~/../../etc/restic/pw', 'cat ~/.config/restic/pw', 'cat ~/../x.gpg',
                 # by design: a quoted literal a shell would not change still fails closed ([ is a glob's)
                 """sops -d --extract '["pw"]' /etc/restic/s.enc.yaml""")
        unit = ('cat $CREDENTIALS_DIRECTORY/etc/restic/pw', 'cat ${CREDENTIALS_DIRECTORY}/../../../etc/restic/pw',
                'cat %d/../../../etc/restic/pw', 'cat %E/restic/pw', 'cat /etc/restic/%%')
        for where, cmds in (('login', login), ('unit', unit)):
            for c in cmds:
                with self.subTest(where=where, c=c):
                    change = {**files, 'etc/restic/%': f'{pw}\n'}
                    if where == 'login':
                        change['etc/profile.d/x.sh'] = f'export RESTIC_PASSWORD_COMMAND={shlex(c)}\n'
                    else:
                        change['etc/systemd/system/b.service'] = (
                            '[Service]\nEnvironment=' + shlex(f'RESTIC_PASSWORD_COMMAND={c}') + '\n')
                    got = self.etc_secrets(change)
                    self.assertTrue(got.startswith('FAIL'), got)
        for c in ('RESTIC_PASSWORD_FILE=%d/../../../etc/restic/pw', 'RESTIC_PASSWORD_FILE=/etc/../etc/restic/pw'):
            with self.subTest(c):
                got = self.etc_secrets({**files, 'etc/systemd/system/b.service': f'[Service]\nEnvironment={c}\n'})
                self.assertTrue(got.startswith('FAIL'), got)
        private = {k: {'text': v, 'mode': '600'} for k, v in files.items()}
        login = ('cat ~/x.gpg', 'pass show backup/restic', 'cat /etc/restic/pass,word',
                 'curl -s https://vault:8200/v1/restic', 'tool --password-file="/etc/restic/my pw"')
        unit = ('cat ${CREDENTIALS_DIRECTORY}/restic', 'cat $CREDENTIALS_DIRECTORY/restic', 'cat %d/restic',
                'cat /etc/restic/pw')
        for where, cmds in (('login', login), ('unit', unit)):
            for c in cmds:
                with self.subTest(where=where, c=c):
                    change = dict(private)
                    if where == 'login':
                        change['etc/profile.d/x.sh'] = f'export RESTIC_PASSWORD_COMMAND={shlex(c)}\n'
                    else:
                        change['etc/systemd/system/b.service'] = (
                            '[Service]\nEnvironment=' + shlex(f'RESTIC_PASSWORD_COMMAND={c}') + '\n')
                    got = self.etc_secrets(change)
                    self.assertTrue(got.startswith('PASS'), got)
        got = self.etc_secrets({**private, 'etc/systemd/system/b.service':
                                '[Service]\nEnvironment=RESTIC_PASSWORD_FILE=%d/restic\n'})
        self.assertTrue(got.startswith('PASS'), got)

    def test_linux_public_only_is_tight(self):
        """Issue #226 item 5: a file passes as public only when each block's body is base64 of what that block is
        (DER for a certificate, a CRL or a public key; an OpenPGP public key packet), and each SSH public key line is
        a known type, base64 of that key, and at most a short plain comment: no options (environment="..."). A
        password inside the markers, or in a long comment, FAILS."""
        pw = 'Hunter2-correct-horse'
        cert = pem('CERTIFICATE', der_sequence(64))
        line = ssh_public_line()
        fails = {
            'a password inside certificate markers': '-----BEGIN CERTIFICATE-----\nHunter2-correct horse\n'
                                                     '-----END CERTIFICATE-----\n',
            'base64 that is no DER': '-----BEGIN CERTIFICATE-----\nSHVudGVyMmNvcnJlY3Q=\n-----END CERTIFICATE-----\n',
            'a DER sequence cut short': pem('CERTIFICATE', der_sequence(64)[:-3]),
            'a PGP block that is no public key': pem('PGP PUBLIC KEY BLOCK', b'\xc5\x05hello'),
            'a password in a comment': f'{line} {pw} and some more words of it\n',
            'ssh options before the key': f'environment="GH_TOKEN={pw}" {line}\n',
            'a password as the key': f'ssh-ed25519 SHVudGVyMmNvcnJlY3Q= x\n',
            'a key of another type than it says': line.replace('ssh-ed25519', 'ssh-rsa', 1) + '\n',
            # PR #240 fix 1 (security 3, correctness nit): each block read in full, its first inner element too.
            'a PKCS#8 private key between PUBLIC KEY markers': pem(
                'PUBLIC KEY', bytes.fromhex('30150201000410') + bytes(range(16))),
            'an RSA private key between RSA PUBLIC KEY markers': pem(
                'RSA PUBLIC KEY', der(0x30, b''.join(der(0x02, bytes([i + 1])) for i in range(9)))),
            'an encrypted private key between PUBLIC KEY markers': pem(
                'PUBLIC KEY', der(0x30, der(0x30, der(0x06, b'\x2a')) + der(0x04, bytes(range(16))))),
            'a certificate whose inside is no DER': pem('CERTIFICATE', der(0x30, der(0x30, b'\x99' * 8) +
                                                                         der(0x30, b'') + der(0x03, b'\x00'))),
            'a PGP public key, then a secret key packet': pem('PGP PUBLIC KEY BLOCK', b'\x99\x00\x05\x04abcd'
                                                              b'\x95\x00\x05\x04abcd', blank=True),
            'a PGP public key, then bytes left over': pem('PGP PUBLIC KEY BLOCK', b'\x99\x00\x04\x04abcd', blank=True),
            # PR #240 fix 1 (security 4): a known_hosts host field is 64 characters at most.
            'a 300-character host before a key': f'{"h" * 300} {line}\n',
            # PR #240 fix 2 (correctness 4): each host name 253 characters at most; a marker packet only as RFC 9580's.
            'a 254-character host name in a list': f'a.example,{"h" * 254} {line}\n',
            'a PGP marker packet that is not "PGP"': pem('PGP PUBLIC KEY BLOCK', b'\xca\x03XYZ\x99\x00\x05\x04abcd',
                                                         blank=True),
        }
        for name, text in fails.items():
            with self.subTest(name):
                got = self.etc_secrets({'etc/environment': 'TLS_KEY_FILE=/etc/app/k.pem\n', 'etc/app/k.pem': text})
                self.assertTrue(got.startswith('FAIL'), got)
        passes = {
            'certificates and a comment': '# the CA\n' + cert + cert,
            'a PGP public key': pem('PGP PUBLIC KEY BLOCK', b'\x99\x00\x05\x04abcd', blank=True),
            'a PGP public key, a user id and a signature': pem(
                'PGP PUBLIC KEY BLOCK', b'\x99\x00\x05\x04abcd\xcd\x03a@b\xc2\x02\x04\x00', blank=True),
            'a public key': pem('PUBLIC KEY', der(0x30, der(0x30, der(0x06, b'\x2b\x65\x70')) +
                                                 der(0x03, b'\x00' + bytes(range(32))))),
            'an RSA public key': pem('RSA PUBLIC KEY', der(0x30, der(0x02, b'\x00\xc1' + bytes(63)) +
                                                           der(0x02, b'\x01\x00\x01'))),
            'a hashed known_hosts line': f'|1|{"A" * 27}=|{"B" * 27}= {line}\n',
            'an ssh key with a short comment': f'{line} deploy@build-1\n',
            'an ssh key with no comment': f'{line}\n',
            # PR #240 fix 2 (correctness 4): a long known_hosts list (OpenSSH has no limit), RFC 9580's marker and
            # padding packets
            'a long known_hosts host list': f'{"build-1." + "a" * 60 + ".example.internal"},[2001:db8::1]:2222,'
                                            f'{"h" * 253},192.0.2.1 {line}\n',
            'a PGP public key with a marker and padding': pem(
                'PGP PUBLIC KEY BLOCK', b'\xca\x03PGP\x99\x00\x05\x04abcd\xd5\x04\x01\x02\x03\x04', blank=True),
        }
        for name, text in passes.items():
            with self.subTest(name):
                got = self.etc_secrets({'etc/environment': 'TLS_KEY_FILE=/etc/app/k.pem\n', 'etc/app/k.pem': text})
                self.assertTrue(got.startswith('PASS'), got)

    def test_linux_etc_secrets_advice_fits_the_file(self):
        """Issue #211: a file every login reads (/etc/environment, a shell's start file, fish's) cannot be made
        private: the advice is to move the secret out of it, never to chmod it 0600. A file only a service or
        mount reads may be made root's alone."""
        for name, change in (('environment', {'etc/environment': 'GH_TOKEN=x1\n'}),
                             ('bashrc', {'etc/bash.bashrc': 'export GH_TOKEN=x1\n'}),
                             ('zshrc', {'etc/zsh/zshrc': 'export GH_TOKEN=x1\n'}),
                             ('fish', {'etc/fish/conf.d/x.fish': 'set -gx GH_TOKEN x1\n'}),
                             ('profile.d', {'etc/profile.d/x.sh': 'export GH_TOKEN=x1\n'})):
            with self.subTest(name):
                got = self.etc_secrets(change)
                self.assertTrue(got.startswith('FAIL'), got)
                self.assertIn('move it out', got)
                self.assertNotRegex(got, r'0600|chmod')
        for name, change in (('a credentials file', {'etc/nas.cred': 'password=p\n'}),
                             ('an EnvironmentFile', {'etc/systemd/system/b.service':
                                                     '[Service]\nEnvironmentFile=/etc/b.env\n',
                                                     'etc/b.env': 'RESTIC_PASSWORD=hunter2\n'})):
            with self.subTest(name):
                got = self.etc_secrets(change)
                self.assertRegex(got, r'^FAIL .*0600')

    def test_linux_cgroup_bpf_from_the_kernel_config(self):   # the config in /boot decides before /proc/kallsyms
        for config, want in (('CONFIG_CGROUP_BPF=y\n', []), ('# CONFIG_CGROUP_BPF is not set\n', ['cgroup-bpf'])):
            with self.subTest(config):
                root = self.root(overlay(LINUX, {'proc/kallsyms': None, 'boot/config-6.8.0-47-generic': config}))
                self.assertEqual(EveryWallsFile.walls(self, 'linux', 'check', root)[1], want)

    def test_linux_mounts_needs_the_launchers_rule_in_place(self):   # its allow list, safe as owned_safe wants it
        allow = os.path.join(os.path.dirname(self.folder), JOB_PATHS)
        def gone(): os.remove(allow)
        def open_list(): os.chmod(allow, 0o664)
        def a_link(): os.rename(allow, allow + '.real'); os.symlink(allow + '.real', allow)
        def lists_home(): write(allow, '/usr\n/home\n')
        def lists_dots(): write(allow, '/usr/../home\n')
        def none(): pass   # in place: a desktop with shares and data disks mounted outside the list holds
        for change in (none, gone, open_list, a_link, lists_home, lists_dots):
            with self.subTest(change.__name__):
                self.folder = os.path.join(os.path.dirname(self.install()), 'fork-fleet-walls')
                allow = os.path.join(os.path.dirname(self.folder), JOB_PATHS)
                change()
                fails = self.walls('linux', 'check', self.root(LINUX))[1]
                (self.assertNotIn if change is none else self.assertIn)('mounts', fails)

    def test_linux_mounts_asks_the_launcher_and_its_conf(self):
        """One rule and one conf lookup (fix round 2): the walls hold what jobs see to `fork-fleet-launch.sh paths`,
        which reads FORK_FLEET_LAUNCH_CONF when set, as `start` does. Through the dispatcher too."""
        linux = slurp(os.path.join(FOLDER, 'linux'))
        self.assertIn("[LAUNCHER, verb]", linux)
        self.assertIn("ask('paths'", linux)
        self.assertNotIn('launch.conf', linux)   # no second copy of the conf lookup (job-units asks `tools`)
        other = {'etc/fork-fleet-proof/launch.conf': 'ALLOW_PATHS=/c\n', 'c/': ''}
        root = self.root(overlay(LINUX, other))
        self.assertNotIn('mounts', self.walls('linux', 'check', root)[1])   # the default conf: no /c
        p = run([os.path.join(self.folder, 'linux'), 'check'], root,
                FORK_FLEET_LAUNCH_CONF='/etc/fork-fleet-proof/launch.conf')
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertRegex(p.stdout, r'FAIL  mounts  .*/c \(on 9p at /c\)')
        script = os.path.join(os.path.dirname(self.folder), 'fork-fleet-walls.py')
        p = run([script, 'check'], root, FORK_FLEET_LAUNCH_CONF='/etc/fork-fleet-proof/launch.conf')
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn('FAIL  mounts', p.stdout)

    def test_linux_mounts_follows_a_link_in_a_folder_above(self):
        """The lenses' major: a path under a linked folder lands on the link's target. The launcher resolves it, so a
        Windows drive, a home folder or a share behind a link fails `mounts` (the launcher refuses it, or binds the
        share's real path), and a link a system path has itself (/var/run -> /run style) stays fine."""
        c_on_9p = LINUX['proc/self/mountinfo']
        cases = {
            'a link to the Windows drive': {'opt/win': '../c', 'c/Users/': '',
                                            'etc/fork-fleet/launch.conf': 'ALLOW_PATHS=/opt/win/Users\n'},
            'a link to /home': {'opt/h': '../home', 'home/someone/': '',
                                'etc/fork-fleet/launch.conf': 'ALLOW_PATHS=/opt/h/someone\n'},
            'the work folder under a link to a share': {'var/lib/fork-fleet': '../../data', 'data/runners/': '',
                                                        'etc/fork-fleet/launch.conf': ''},
            'the hook under a link to a share': {'opt/fork-fleet': '../data/kit', 'data/kit/hook.sh': ''},
            'resolv.conf under a link to the drive': {'etc/fork-fleet/resolv.conf': None, 'c/r/': '',
                                                      'etc/fork-fleet/launch.conf': 'RESOLV_CONF=/etc/ff/resolv.conf\n',
                                                      'etc/ff': '../c/r'},
        }
        for name, change in cases.items():
            with self.subTest(name):
                links = {k: v for k, v in change.items() if v and not k.endswith('/') and v.startswith('..')}
                root = self.root(overlay(LINUX, {k: v for k, v in change.items() if k not in links}))
                for path, target in links.items():
                    os.makedirs(os.path.dirname(os.path.join(root, path)), mode=0o755, exist_ok=True)
                    os.chmod(os.path.dirname(os.path.join(root, path)), 0o755)   # any umask: root's alone
                    os.symlink(target, os.path.join(root, path))
                self.assertIn('mounts', self.walls('linux', 'check', root)[1], c_on_9p)
                if name == 'a link to /home':   # refused outright: a refusal is never read as "nothing to bind"
                    p = run([os.path.join(self.folder, 'linux'), 'check'], root)
                    self.assertRegex(p.stdout, r'FAIL  mounts  .*the launcher refuses what jobs would see: .*home')

    def test_this_machine_gets_an_answer(self):
        for name in OSES:   # on the real machine: exit 0 or 1 in the contract, never a crash
            with self.subTest(name):
                p = subprocess.run([os.path.join(FOLDER, name), 'describe'], capture_output=True, text=True,
                                   env={'PATH': fw.PATH, 'LC_ALL': 'C'}, timeout=120)
                self.assertIn(p.returncode, (0, 1), p.stderr)
                self.assertTrue(all(fw.LINE.fullmatch(x) for x in p.stdout.splitlines()), p.stdout)


if __name__ == '__main__':
    unittest.main()
