#!/usr/bin/env python3
"""Tests of fork-fleet-walls.py (OS detection, refusal of an OS without walls, the walls-file contract it enforces) and
the contract test every walls file in fork-fleet-walls/ must pass, on fake machines from its <os>.fixture.json. A new
walls file (and its fixture) is tested here without a change to this file. Run: python3 <this>. Linux only."""
import contextlib, importlib.util, io, json, os, re, shutil, stat, subprocess, sys, tempfile, unittest
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
    return root


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


def fixture_problems(fixture, walls):
    """What makes a fixture unfit for a walls file whose describe named walls: each wall needs a failing change."""
    fail = fixture.get('fail')
    if not isinstance(fail, dict) or sorted(fail) != sorted(walls):
        return [f'its `fail` names {sorted(fail or [])}, not the walls {sorted(walls)}']
    return [f'wall {wall} has no failing change' for wall, changes in fail.items()
            if not (changes if isinstance(changes, list) else [changes])
            or not all(isinstance(c, dict) and c for c in (changes if isinstance(changes, list) else [changes]))]


class EveryWallsFile(Case):
    """The contract every walls file must pass (fork-fleet-walls.py's docstring), run on each file in the folder (an
    installed copy, beside the launcher's allow list, with the modes an install gives)."""
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

    def test_contract(self):
        for name in OSES:
            with self.subTest(name):
                fixture = json.loads(slurp(os.path.join(FOLDER, f'{name}.fixture.json')))
                for args in ([], ['start'], ['check', 'describe']):   # usage: exit 2, nothing on stdout
                    p = run([os.path.join(self.folder, name), *args], self.root({}))
                    self.assertEqual((p.returncode, p.stdout), (2, ''), f'{name} {args}')
                ids, fails = self.both(name, self.root({}))   # an empty machine fails closed, `os` among them
                self.assertIn('os', fails)
                good = self.root(fixture['pass'])
                ids, fails = self.both(name, good)
                self.assertEqual(fails, [], f'{name}: walls fail on its passing fixture')
                self.assertEqual(fw.detect(good), name, f'{name}: its passing fixture is detected as another OS')
                p = run([self.install(), 'check'], good)   # and through fork-fleet-walls.py
                self.assertEqual((p.returncode, p.stdout), (0, ''), p.stderr)
                self.assertEqual(fixture_problems(fixture, ids), [], name)
                for wall, changes in fixture['fail'].items():
                    for change in changes if isinstance(changes, list) else [changes]:
                        with self.subTest(name=name, wall=wall, change=change):
                            root = self.root(overlay(fixture['pass'], change))
                            self.assertIn(wall, self.both(name, root)[1])
                            p = run([self.install(), 'check'], root)
                            self.assertIn(p.returncode, (1, 3), p.stderr)   # 3: the change made it another OS

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

    def etc_secrets(self, change):
        """The linux etc-secrets line on the passing fixture with change, through describe."""
        p = run([os.path.join(self.folder, 'linux'), 'describe'], self.root(overlay(LINUX, change)))
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
            'a credentials path': {'etc/environment': 'GOOGLE_APPLICATION_CREDENTIALS=/opt/gcp/key.json\n'},
            'a key file path': {'etc/profile.d/ssh.sh': 'export SSH_KEY=~/.ssh/id_ed25519 GPG_KEY=/etc/x.gpg\n'},
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
                             'a lower case token': {'etc/profile.d/x.sh': 'github_token=x1\n'}}.items():
            with self.subTest(name):
                self.assertTrue(self.etc_secrets(change).startswith('FAIL'), name)

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
        self.assertIn("[LAUNCHER, 'paths']", linux)
        self.assertNotIn('launch.conf', linux)   # no second copy of the conf lookup
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
                    os.makedirs(os.path.dirname(os.path.join(root, path)), exist_ok=True)
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
