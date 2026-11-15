#!/usr/bin/env python3
"""Tests of fork-fleet-walls.py (OS detection, refusal of an OS without walls, the walls-file contract it enforces) and
the contract test every walls file in fork-fleet-walls/ must pass, on fake machines from its <os>.fixture.json. A new
walls file (and its fixture) is tested here without a change to this file. Run: python3 <this>. Linux only."""
import importlib.util, json, os, re, shutil, stat, subprocess, sys, tempfile, unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'fork-fleet-walls.py')
FOLDER = os.path.join(HERE, 'fork-fleet-walls')
_spec = importlib.util.spec_from_file_location('fork_fleet_walls', SCRIPT)
fw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fw)
OSES = sorted(n for n in os.listdir(FOLDER) if fw.OS_NAME.fullmatch(n))


def slurp(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


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
        if isinstance(value, dict):
            os.chmod(full, int(value['mode'], 8))
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
        return build(os.path.join(self.tmp, f'root{self.n}'), files)

    def install(self, walls=None):
        """A copy of fork-fleet-walls.py with a walls folder of its own: {os: walls file text}; None: the real ones."""
        bin_dir = os.path.join(self.tmp, f'bin{self.n}')
        self.n += 1
        os.makedirs(os.path.join(bin_dir, 'fork-fleet-walls'))
        shutil.copy(SCRIPT, bin_dir)
        os.chmod(os.path.join(bin_dir, 'fork-fleet-walls.py'), 0o755)
        if walls is None:
            walls = {n: slurp(os.path.join(FOLDER, n)) for n in OSES}
        for name, text in walls.items():
            path = os.path.join(bin_dir, 'fork-fleet-walls', name)
            with open(path, 'w') as f:
                f.write(text)
            os.chmod(path, 0o755)
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
        self.assertIn(repr(fw.WSL_MARKS), text)

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


class Contract(Case):
    """What fork-fleet-walls.py makes of a walls file's answer (a fake `linux` that prints OUT and exits CODE)."""
    def answer(self, verb, out, code):
        script = self.install({'linux': f'#!/bin/sh\nprintf %s {shlex(out)}\nexit {code}\n'})
        return run([script, verb], self.root(LINUX))

    def test_answers(self):
        fail = 'FAIL  mounts  nothing mounted: mounted: /mnt\n'
        ok = 'PASS  os  a plain Linux kernel\n'
        cases = [   # verb, stdout, exit of the walls file -> exit of fork-fleet-walls.py
            ('check', '', 0, 0), ('check', fail, 1, 1), ('describe', ok, 0, 0), ('describe', ok + fail, 1, 1),
            ('check', fail, 0, 2),            # a failing wall with exit 0 is not a pass
            ('check', '', 1, 2),              # exit 1 that names no wall
            ('check', fail, 3, 2),            # any other exit: a crash is not a FAIL list
            ('check', ok, 0, 2),              # check prints only the walls that fail
            ('check', 'FAIL mounts x\n', 1, 2), ('check', 'FAIL  Mounts  x\n', 1, 2), ('check', 'all good\n', 0, 2),
            ('check', fail + fail, 1, 2),     # a wall named twice
            ('describe', 'PASS  kernel  ok\n', 0, 2),   # no `os` wall
            ('describe', ok + ok, 0, 2),
        ]
        for verb, out, code, want in cases:
            with self.subTest(verb=verb, out=out, code=code):
                p = self.answer(verb, out, code)
                self.assertEqual(p.returncode, want, p.stderr)
                self.assertEqual(p.stdout, out if want in (0, 1) else '')

    def test_timeout(self):
        script = self.install({'linux': '#!/bin/sh\nexec sleep 30\n'})
        old, fw.TIMEOUT = fw.TIMEOUT, 1
        self.addCleanup(setattr, fw, 'TIMEOUT', old)
        with self.assertRaisesRegex(fw.Broken, 'took more than 1 s'):
            fw.run_walls(os.path.join(os.path.dirname(script), 'fork-fleet-walls', 'linux'), 'check')

    def test_it_gets_no_stdin_and_a_clean_environment(self):
        root = self.root(LINUX)
        script = self.install({'linux': '#!/bin/bash\nenv > "$FORK_FLEET_WALLS_ROOT/env.out"\ncat > '
                                        '"$FORK_FLEET_WALLS_ROOT/stdin.out"\nexit 0\n'})
        p = subprocess.run([script, 'check'], input='secret', capture_output=True, text=True,
                           env={'PATH': fw.PATH, 'FORK_FLEET_WALLS_ROOT': root, 'GH_TOKEN': 'x', 'PYTHONPATH': '/x'})
        self.assertEqual(p.returncode, 0, p.stderr)
        keys = {line.split('=')[0] for line in slurp(f'{root}/env.out').splitlines()} - {'PWD', 'SHLVL', '_'}
        self.assertEqual(keys, {'PATH', 'LC_ALL', 'FORK_FLEET_WALLS_ROOT'})
        self.assertEqual(slurp(f'{root}/stdin.out'), '')

    def test_unsafe_walls_files_are_refused(self):
        def group_writable(f, d): os.chmod(f, 0o775)
        def not_executable(f, d): os.chmod(f, 0o644)
        def a_link(f, d): os.rename(f, f + '.real'); os.symlink(f + '.real', f)
        def open_folder(f, d): os.chmod(d, 0o777)
        def no_folder(f, d): shutil.rmtree(d)
        for change in (group_writable, not_executable, a_link, open_folder, no_folder):
            with self.subTest(change.__name__):
                script = self.install({'linux': '#!/bin/sh\nexit 0\n'})
                folder = os.path.join(os.path.dirname(script), 'fork-fleet-walls')
                change(os.path.join(folder, 'linux'), folder)
                p = run([script, 'check'], self.root(LINUX))
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn('no runner starts here', p.stderr)


def shlex(text):
    return "'" + text.replace("'", "'\\''") + "'"


class EveryWallsFile(Case):
    """The contract every walls file must pass (fork-fleet-walls.py's docstring), run on each file in the folder."""
    def walls(self, name, verb, root):
        p = run([os.path.join(FOLDER, name), verb], root)
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
                    p = run([os.path.join(FOLDER, name), *args], self.root({}))
                    self.assertEqual((p.returncode, p.stdout), (2, ''), f'{name} {args}')
                ids, fails = self.both(name, self.root({}))   # an empty machine fails closed, `os` among them
                self.assertIn('os', fails)
                good = self.root(fixture['pass'])
                ids, fails = self.both(name, good)
                self.assertEqual(fails, [], f'{name}: walls fail on its passing fixture')
                self.assertEqual(fw.detect(good), name, f'{name}: its passing fixture is detected as another OS')
                p = run([self.install(), 'check'], good)   # and through fork-fleet-walls.py
                self.assertEqual((p.returncode, p.stdout), (0, ''), p.stderr)
                self.assertEqual(sorted(fixture['fail']), sorted(ids), f'{name}: each wall needs a failing change')
                for wall, changes in fixture['fail'].items():
                    for change in changes if isinstance(changes, list) else [changes]:
                        with self.subTest(name=name, wall=wall, change=change):
                            root = self.root(overlay(fixture['pass'], change))
                            self.assertIn(wall, self.both(name, root)[1])
                            p = run([self.install(), 'check'], root)
                            self.assertIn(p.returncode, (1, 3), p.stderr)   # 3: the change made it another OS

    def test_this_machine_gets_an_answer(self):
        for name in OSES:   # on the real machine: exit 0 or 1 in the contract, never a crash
            with self.subTest(name):
                p = subprocess.run([os.path.join(FOLDER, name), 'describe'], capture_output=True, text=True,
                                   env={'PATH': fw.PATH, 'LC_ALL': 'C'}, timeout=120)
                self.assertIn(p.returncode, (0, 1), p.stderr)
                self.assertTrue(all(fw.LINE.fullmatch(x) for x in p.stdout.splitlines()), p.stdout)


if __name__ == '__main__':
    unittest.main()
