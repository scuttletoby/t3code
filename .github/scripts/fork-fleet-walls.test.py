#!/usr/bin/env python3
"""Tests of fork-fleet-walls.py (OS detection, refusal of an OS without walls, the walls-file contract it enforces) and
of every walls file in fork-fleet-walls/; the contract each must pass on fake machines from its <os>.fixture.json is
in fork-fleet-walls-contract.test.py, which uses this file's helpers. A new walls file (and its fixture) is tested
without a change to either file. Run: python3 <this>. Linux only."""
import ast, base64, contextlib, errno, hashlib, importlib.machinery, importlib.util, io, json, os, pathlib, re, shutil, signal, stat, subprocess, sys, tempfile, time, unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'fork-fleet-walls.py')
FOLDER = os.path.join(HERE, 'fork-fleet-walls')
LAUNCHER = os.path.join(HERE, 'fork-fleet-launch.sh')
JOB_PATHS = 'fork-fleet-job-paths.txt'   # the launcher's allow list of what jobs see, which the walls read too
FREE = 'free_contract.py'   # the launcher's free-memory check, beside it in an installed kit
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


class Waited(Exception):
    """What no_wait raises in a call that blocks: neither a ValueError nor an OSError, so no assertRaises takes it."""


@contextlib.contextmanager
def no_wait(seconds=10):
    """Fails a call that blocks (a FIFO opened without O_NONBLOCK waits for a writer forever) instead of hanging the
    run: SIGALRM after seconds raises Waited in it (issue #365 item 4). A block whose Waited was caught or reworded
    inside (a wrapper that turns it into launcher_safe's own refusal, so an assertRaisesRegex in the block passes)
    still fails: the alarm is recorded, and the block's end raises Waited again (issue #584 item 3), also when the
    reworded exception leaves the block for an assertRaises outside no_wait (the #597 lenses)."""
    fired = []
    again = (f'blocked for {seconds}s, and its Waited was caught or reworded in the call: opened without '
             'O_NONBLOCK?')

    def late(*_):
        fired.append(seconds)
        raise Waited(f'blocked for {seconds}s: opened without O_NONBLOCK?')
    old = signal.signal(signal.SIGALRM, late)
    signal.alarm(seconds)
    try:
        yield
    except BaseException as e:
        if fired and not isinstance(e, Waited):
            raise Waited(again) from e
        raise
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    if fired:
        raise Waited(again)


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


class NoWait(unittest.TestCase):
    """no_wait itself (issue #584 item 3, the #579 lens B nit): a wrapper that rewords a block into the refusal the
    test expects does not pass."""

    def test_a_block_caught_and_reworded_in_the_call_still_fails(self):
        def wrapped():
            try:
                time.sleep(5)
            except Waited as e:
                raise ValueError(f'/x is a link, or not owned by uid 0, or writable by others: {e}')
        with self.assertRaisesRegex(Waited, 'caught or reworded'):
            with no_wait(1), self.assertRaisesRegex(ValueError, 'is a link, or not owned by uid'):
                wrapped()

    def test_a_reworded_block_that_leaves_it_still_fails_whatever_the_order(self):
        """The #597 lenses: the reworded exception travels through no_wait to an assertRaisesRegex outside it."""
        def wrapped():
            try:
                time.sleep(5)
            except Waited as e:
                raise ValueError(f'/x is a link, or not owned by uid 0, or writable by others: {e}')
        with self.assertRaisesRegex(Waited, 'caught or reworded'):
            with self.assertRaisesRegex(ValueError, 'is a link, or not owned by uid'), no_wait(1):
                wrapped()
        with self.assertRaisesRegex(ValueError, 'not blocked'):   # an error with no alarm passes through as it is
            with no_wait(1):
                raise ValueError('not blocked')

    def test_runs_again_and_leaves_no_alarm(self):
        """Runs again: a block that fired leaves no state behind; the next one that does not block passes, and the
        alarm and the old SIGALRM handler are back as they were."""
        with self.assertRaises(Waited):
            with no_wait(1):
                time.sleep(5)
        before = signal.getsignal(signal.SIGALRM)
        with no_wait(1):
            pass
        self.assertEqual(signal.alarm(0), 0)
        self.assertIs(signal.getsignal(signal.SIGALRM), before)


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
        write(os.path.join(bin_dir, FREE), '# the free-memory check (the launcher\'s; the walls never run it)\n')
        os.chmod(os.path.join(bin_dir, FREE), 0o644)
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


class Installed(Case):
    """Issue #584 item 2: in an installed kit (a MANIFEST.sha256 beside fork-fleet-walls.py), the walls' own files
    must be what the manifest records, so a desk-install stopped half-way (the old manifest beside some new files)
    never runs a mix of walls files; the next install ends the swap and writes the new manifest."""

    def record(self, script, skip=()):
        """The manifest desk-install's swap() writes (`<sha256>  <path>` lines, sorted), of every file in the kit
        folder but skip, as it is now."""
        here = os.path.dirname(script)
        walls = os.listdir(os.path.join(here, 'fork-fleet-walls'))
        rels = sorted(r for r in ['fork-fleet-walls.py', 'fork-fleet-launch.sh', JOB_PATHS, FREE,
                                  *(f'fork-fleet-walls/{n}' for n in walls)] if r not in skip)
        path = os.path.join(here, 'MANIFEST.sha256')
        lines = []
        for r in rels:
            with open(os.path.join(here, r), 'rb') as f:
                lines.append(f'{hashlib.sha256(f.read()).hexdigest()}  {r}\n')
        write(path, ''.join(lines))
        os.chmod(path, 0o644)
        return path

    def os_verb(self, script):
        return run([script, 'os'], self.root(LINUX))

    def test_a_kit_as_recorded_runs(self):
        """Also old state: a manifest that names other files of the kit too (the agent, the units) is fine."""
        script = self.install()
        self.record(script)
        with open(os.path.join(os.path.dirname(script), 'MANIFEST.sha256'), 'a') as f:
            f.write('0' * 64 + '  units/fork-fleet-desk-agent.service\n')
        p = self.os_verb(script)
        self.assertEqual((p.returncode, p.stdout), (0, 'linux\n'), p.stderr)
        p = run([script, 'describe'], self.root(LINUX))
        self.assertNotIn('MANIFEST', p.stderr)
        self.assertIn(p.returncode, (0, 1), p.stderr)

    def test_no_manifest_is_no_install(self):
        """Missing state: the repository and a staged copy have no manifest, and are not held to one."""
        p = self.os_verb(self.install())
        self.assertEqual((p.returncode, p.stdout), (0, 'linux\n'), p.stderr)

    def test_each_mix_a_stopped_install_leaves_is_refused_until_the_next_run_ends_it(self):
        """Stopped half-way: the old manifest beside a changed walls file (a new wsl2 beside the old linux, the mix
        the pin order leaves open), a changed fixture, dispatcher or launcher, a walls file the new release adds, or
        one it removes. Runs again: the next install writes the new manifest, and the kit runs."""
        here = lambda script: os.path.dirname(script)
        mixes = {
            'new wsl2 beside the old linux': lambda s: write(os.path.join(here(s), 'fork-fleet-walls/wsl2'),
                                                              slurp(os.path.join(FOLDER, 'wsl2')) + '# new\n'),
            'new fixture': lambda s: write(os.path.join(here(s), 'fork-fleet-walls/linux.fixture.json'),
                                           slurp(os.path.join(FOLDER, 'linux.fixture.json')) + '\n'),
            'new dispatcher': lambda s: write(s, slurp(SCRIPT) + '# new\n'),
            'new launcher': lambda s: write(os.path.join(here(s), 'fork-fleet-launch.sh'), slurp(LAUNCHER) + '# n\n'),
            # the #597 lenses: what the launcher reads beside itself (what jobs see; the free-memory check)
            'new job-path list': lambda s: write(os.path.join(here(s), JOB_PATHS), slurp(os.path.join(HERE, JOB_PATHS))
                                                 + '/opt/new\n'),
            'new free-memory check': lambda s: write(os.path.join(here(s), FREE), '# new\n'),
            'a walls file the release adds': lambda s: [write(os.path.join(here(s), 'fork-fleet-walls', n), '{}')
                                                        for n in ('macos', 'macos.fixture.json')],
            'a walls file the release removes': lambda s: [os.unlink(os.path.join(here(s), 'fork-fleet-walls', n))
                                                           for n in ('wsl2', 'wsl2.fixture.json')],
        }
        for mix, change in mixes.items():
            with self.subTest(mix):
                script = self.install()
                self.record(script)
                change(script)
                for verb in ('os', 'check', 'describe'):
                    p = run([script, verb], self.root(LINUX))
                    self.assertEqual((p.returncode, p.stdout), (2, ''), f'{verb}: {p.stderr}')
                    self.assertIn('MANIFEST.sha256', p.stderr)
                    self.assertRegex(p.stderr, r'stopped half-way .*: install the kit again|Run the install again')
                self.record(script)   # the next run ends the swap: the new manifest
                p = self.os_verb(script)
                self.assertEqual((p.returncode, p.stdout), (0, 'linux\n'), p.stderr)

    def test_a_file_the_manifest_does_not_name_is_refused(self):
        """A manifest that leaves out a walls file, the dispatcher, the launcher or one of its inputs: the refusal
        names the way out, which differs by what is there (the #597 lenses): a plain file of this user an install
        removes (walls folder) or replaces; a link, folder or FIFO it leaves for a person to remove."""
        for rel in ('fork-fleet-walls/wsl2.fixture.json', 'fork-fleet-walls.py', 'fork-fleet-launch.sh', JOB_PATHS,
                    FREE):
            with self.subTest(rel):
                script = self.install()
                self.record(script, skip=[rel])
                p = self.os_verb(script)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn(f'{rel} is not in ', p.stderr)
                self.assertIn('install the kit again (desk-install removes it)', p.stderr)
        script = self.install()
        self.record(script, skip=[FREE])
        os.unlink(os.path.join(os.path.dirname(script), FREE))
        p = self.os_verb(script)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn(f'{FREE} is not in ', p.stderr)
        self.assertIn('nor there', p.stderr)
        walls = lambda s: os.path.join(os.path.dirname(s), 'fork-fleet-walls')
        others = {'a link': lambda s: os.symlink(os.path.join(walls(s), 'linux'), os.path.join(walls(s), 'macos')),
                  'a folder': lambda s: os.mkdir(os.path.join(walls(s), '__pycache__'), 0o755),
                  'a FIFO': lambda s: os.mkfifo(os.path.join(walls(s), 'macos'), 0o644)}
        for what, put in others.items():
            with self.subTest(what):
                script = self.install()
                self.record(script)
                put(script)
                with no_wait():
                    p = self.os_verb(script)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn('is not in ', p.stderr)
                self.assertIn('so an install leaves it: remove it by hand, then install the kit again', p.stderr)

    def test_a_link_or_fifo_at_a_recorded_file_is_refused_and_never_waited_on(self):
        """The #597 lenses: plain_bytes opens a recorded file with O_NOFOLLOW (a link to a file with the very bytes
        the manifest records is still refused) and O_NONBLOCK (a FIFO is refused at once, not waited on), at a walls
        file of another OS (safe() checks only this OS's) and at a launcher input. Without either flag this fails."""
        for rel in ('fork-fleet-walls/wsl2', JOB_PATHS):
            for what in ('link', 'fifo'):
                with self.subTest(rel=rel, what=what):
                    script = self.install()
                    self.record(script)
                    path = os.path.join(os.path.dirname(script), rel)
                    if what == 'link':   # the very bytes recorded, outside every folder the check lists
                        real = os.path.join(self.tmp, f'real{self.n}')
                        os.rename(path, real)
                        os.symlink(real, path)
                    else:
                        os.unlink(path)
                        os.mkfifo(path, 0o644)
                    spec = importlib.util.spec_from_file_location(f'walls_installed_{self.n}', script)
                    m = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(m)   # its HERE: the kit folder beside the manifest
                    with no_wait(5), self.assertRaisesRegex(m.Broken, f'{re.escape(rel)} cannot be read'):
                        m.as_installed()

    def test_an_empty_or_unsafe_manifest_is_refused(self):
        """Empty or broken state: an empty manifest, one that is not UTF-8, a link, a FIFO (never waited on), or one
        others can write. An OS without walls is still exit 3 first (asked before any safety check)."""
        cases = {
            'empty': lambda m: write(m, ''),
            'not utf-8': lambda m: pathlib.Path(m).write_bytes(b'\xff\xfe  fork-fleet-walls.py\n'),
            'link': lambda m: (os.rename(m, m + '.real'), os.symlink(m + '.real', m)),
            'fifo': lambda m: (os.unlink(m), os.mkfifo(m, 0o644)),
            'writable by others': lambda m: os.chmod(m, 0o666),
        }
        for name, spoil in cases.items():
            with self.subTest(name):
                script = self.install()
                spoil(self.record(script))
                p = self.os_verb(script)
                self.assertEqual((p.returncode, p.stdout), (2, ''), p.stderr)
                self.assertIn('MANIFEST.sha256', p.stderr)
        script = self.install({'linux': '#!/bin/sh\nexit 0\n'})
        write(self.record(script), '')
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

    def test_linux_launcher_pin_is_the_launchers(self):   # change the launcher, then the one pin with it
        """Issue #316: the linux walls pass only the launcher they ship with (LAUNCHER_SHA256, which the wsl2 walls
        read too), whose start() the test above and fork-fleet.test.py's test_start read. The pin is the sha256 of the
        launcher in this repo, so a launcher change without a pin change fails here, not on a host as cannot tell."""
        linux = slurp(os.path.join(FOLDER, 'linux'))
        self.assertEqual(len(re.findall(r"^LAUNCHER_SHA256 = '[0-9a-f]{64}'$", linux, re.M)), 1)
        with open(LAUNCHER, 'rb') as f:
            self.assertEqual(fw_linux().LAUNCHER_SHA256, hashlib.sha256(f.read()).hexdigest(), 'fork-fleet-launch.sh '
                             'changed: set LAUNCHER_SHA256 in fork-fleet-walls/linux (the one pin, which wsl2 reads) '
                             'to its sha256 (and read the change with the security lens)')

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
        """Issue #297 item 3, issue #316: the walls count systemd's defaults out only for the launcher they ship with
        (LAUNCHER_SHA256), whose start() gives every job unit stop timeouts of its own. Any other launcher, an older one
        (a kit not installed whole, whose start() set none) too, or one whose start() sets them otherwise: cannot tell,
        whatever the defaults. The launcher itself PASSES with an endless default and with a finite one."""
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
            ('an older launcher, a finite default', real.replace(both, ' '), {}, 'FAIL'),
            ('the launcher, quoted', real.replace(both, ' -p "TimeoutStopSec=90" -p \'TimeoutAbortSec=90\' '), endless,
             'FAIL'),
            ('the launcher with a comment added', real + '# a comment\n', {}, 'FAIL'),
            ('an empty launcher', '', {}, 'FAIL'),
            ('the launcher, an endless default', real, endless, 'PASS'),
            ('the launcher, a finite default', real, {}, 'PASS'),
        ]
        launcher = os.path.join(os.path.dirname(self.folder), 'fork-fleet-launch.sh')
        for name, text, change, want in cases:
            with self.subTest(name):
                write(launcher, text)
                got = self.job_units(change)
                self.assertTrue(got.startswith(want), got)
                if want == 'FAIL':
                    self.assertIn('cannot tell (ValueError', got)
                    self.assertIn('is not the launcher these walls ship with', got)
        os.chmod(launcher, 0o775)   # not safe (launcher_shipped and ask both refuse it; test_linux_launcher_shipped_*
        write(launcher, real)       # tests launcher_shipped's own refusal): the wall cannot tell
        got = self.job_units({})
        self.assertTrue(got.startswith('FAIL'), got)
        self.assertIn('cannot tell', got)
        os.remove(launcher)         # no launcher: cannot tell
        got = self.job_units({})
        self.assertTrue(got.startswith('FAIL'), got)
        self.assertIn('cannot tell (FileNotFoundError', got)

    def test_linux_job_units_refuses_stop_timeouts_start_never_passes(self):
        """Issue #316 item 1 and its lenses: a stop timeout that start()'s systemd-run never gets (one in a comment,
        a quoted string, a heredoc, a $(...) or ${...} in a string, a [[ -p ]] test, an array, after a return), an
        endless one it does get in a form a reading of bash misses (-pKey=, --property=, a quote inside the word, a
        \\ + newline, a # inside a word), or a second start() anywhere (bash runs the last one): each PASSed some
        reading of the launcher's bash. The walls read no bash now: a launcher that is not the one they ship with
        (LAUNCHER_SHA256) cannot be told, whatever it changed, with an endless default or a finite one. Only the
        launcher itself PASSES."""
        real = slurp(LAUNCHER)
        both = ' -p TimeoutStopSec=90 -p TimeoutAbortSec=90 '
        self.assertEqual(real.count(both), 1)
        old = real.replace(both, ' ')   # a start() that gives none of its own
        at = '\n  # Type=exec:'
        self.assertEqual(old.count(at), 1)
        endless = {'etc/systemd/system.conf': '[Manager]\nDefaultTimeoutStopSec=infinity\n'}   # the abort one:
        fake = '-p TimeoutStopSec=90 -p TimeoutAbortSec=90'
        second = (f'start() {{\n  "$SYSTEMD_RUN" -p TimeoutStopSec=infinity -p TimeoutAbortSec=infinity -- x\n}}\n')
        infinite = real.replace(both, ' -p TimeoutStopSec=infinity -p TimeoutAbortSec=infinity ')
        run_end = ' -- "${job_cmd[@]}"'   # the end of start()'s systemd-run line
        self.assertEqual(old.count(run_end), 1)
        fails = {
            'in an end-of-line comment': old.replace(at, f'\n  true # {fake}' + at),
            'in a heredoc': old.replace(at, f'\n  cat >/dev/null <<EOF\n  {fake}\nEOF' + at),
            'in a quoted heredoc': old.replace(at, f"\n  cat >/dev/null <<'EOF'\n  {fake}\nEOF" + at),
            "in a $'...' with an escaped quote": old.replace(at, f"\n  echo $'\\' {fake} \\'' # '" + at),
            'in a comment in backticks': old.replace(at, f'\n  : `# {fake} `' + at),
            'in a string in a $(...) in a string': old.replace(at, f'\n  x="$(echo " {fake} ")"' + at),
            'in a string, after a # line in a string': old.replace(
                at, f'\n  echo "a\n# " >/dev/null\n  echo " {fake} " >/dev/null # "' + at),
            'in a string, after a \\" in code': old.replace(at, f'\n  echo \\"; echo " {fake} " # "' + at),
            'in a string, after a \\" in it': old.replace(at, f'\n  echo "x\\" {fake} \\"" # "' + at),
            'in a string, after a case in a $(...)': old.replace(
                at, f'\n  x="$(case a in a) echo " {fake} ";; esac)"' + at),
            'in a string in a ${...} in a string': old.replace(at, f'\n  x="${{y:-" {fake} "}}"' + at),
            'in a string in a $(...) after a $((...))': old.replace(at, f'\n  x="$(echo $((1)) " {fake} ")"' + at),
            'in a ${...} with a quote': old.replace(at, f'\n  x=${{y:-"}}"" {fake} "}} # "' + at),
            'before a } line in a string': infinite.replace(at, f'\n  : {fake}\n  echo "\n}}\n" >/dev/null' + at),
            'a second start() after a ;': real.replace('\ncheck_net() {\n', '\n: ; ' + second.replace(
                '\n', ' ').replace(' x }', ' x; }') + '\ncheck_net() {\n'),
            'in a double-quoted string': old.replace(at, f'\n  echo "use {fake}" >/dev/null' + at),
            'in a single-quoted string': old.replace(at, f"\n  echo 'use {fake}' >/dev/null" + at),
            'in a string across lines': old.replace(at, f'\n  echo "use\n  {fake}" >/dev/null' + at),
            'a second start()': real.replace('\ncheck_net() {\n', '\n' + second + '\ncheck_net() {\n'),
            'a second start, function keyword': real.replace('\ncheck_net() {\n', '\n' + second.replace(
                'start() {', 'function start {') + '\ncheck_net() {\n'),
            # Round 2's lens: a # after an escaped character or a closing ) is no comment to bash.
            'after a \\ + newline, a # at a line start': old.replace(at, f'\n  : a\\\n#"\n  {fake}\n  " #"' + at),
            'after a \\;, a #': old.replace(at, f'\n  : \\;#"\n  {fake}\n  " #"' + at),
            'after a \\ + blank, a #': old.replace(at, f'\n  : a\\ #"\n  {fake}\n  " #"' + at),
            'after a $(...), a #': old.replace(at, f'\n  : x$(true)#"\n  {fake}\n  " #"' + at),
            'after a $((...)), a #': old.replace(at, f'\n  : x$((1))#"\n  {fake}\n  " #"' + at),
            'after a <(...), a #': old.replace(at, f'\n  : x<(true)#"\n  {fake}\n  " #"' + at),
            'an endless one after a -p a\\ #b': real.replace(both, both + '-p Description=a\\ #b '
                                                           '-p TimeoutStopSec=infinity -p TimeoutAbortSec=infinity '),
            'an endless one after a -p a$(true)#b': real.replace(
                both, both + '-p Description=a$(true)#b -p TimeoutStopSec=infinity -p TimeoutAbortSec=infinity '),
            'an endless one after a -p a#b': real.replace(both, both + '-p Description=a#b -p TimeoutStopSec=infinity '
                                                      '-p TimeoutAbortSec=infinity '),
            'a second start ( )': real.replace('\ncheck_net() {\n', '\n' + second.replace(
                'start() {', 'start ( ) {') + '\ncheck_net() {\n'),
            'a second start\\ + newline + ()': real.replace('\ncheck_net() {\n', '\n' + second.replace(
                'start() {', 'start\\\n() {') + '\ncheck_net() {\n'),
            # The audit: plain bash, no quoting trick. Nothing reaches systemd-run (1-3), or an endless one does, last.
            'in a [[ -p ]] test': old.replace(at, f'\n  [[ {fake.replace(" -p", " && -p")} ]] || true' + at),
            'in an array': old.replace(at, f'\n  local -a note=( {fake} )' + at),
            'after a return': old.replace(run_end, run_end + f'\n  return\n  : {fake}'),
            'an endless -pKey=': real.replace(both, both + '-pTimeoutStopSec=infinity '),
            'an endless --property=': real.replace(both, both + '--property=TimeoutStopSec=infinity '),
            'an endless one after -p \\ + newline': real.replace(both, both + '-p \\\n    TimeoutStopSec=infinity '),
            'an endless one with "" in its value': real.replace(both, both + '-p TimeoutStopSec=infi""nity '),
            'an endless one with a quoted key part': real.replace(both, both + '-p Time"out"StopSec=infinity '),
            # The round-2 lens' follow-ups: a whole property from a variable, an inner function never called.
            'a property from a variable': real.replace(both, both + '-p "$T" '),
            'in an inner function never called': old.replace(at, f'\n  f() {{ : {fake}; }}' + at),
        }
        launcher = os.path.join(os.path.dirname(self.folder), 'fork-fleet-launch.sh')
        for name, text in fails.items():
            for change in (endless, {}):
                with self.subTest(name, default='endless' if change else 'finite'):
                    self.assertNotEqual(text, real)
                    write(launcher, text)
                    got = self.job_units(change)
                    self.assertTrue(got.startswith('FAIL'), got)
                    self.assertIn('cannot tell (ValueError', got)
                    self.assertIn('is not the launcher these walls ship with', got)
        with self.subTest('the launcher'):
            write(launcher, real)
            got = self.job_units(endless)
            self.assertTrue(got.startswith('PASS'), got)

    def test_linux_job_units_cannot_tell_an_unreadable_job_unit(self):
        """Issue #316 item 2: only a job unit's transient file that is gone (FileNotFoundError: the unit ended) is
        skipped; one that cannot be read for any other reason (mode 000, an I/O error) still counts: cannot tell."""
        unit = 'run/systemd/transient/fork-fleet-ci-0.service'
        m = fw_linux()
        m.ROOT = self.root(overlay(LINUX, {**OLD_JOB, 'etc/systemd/system.conf': SLOW_DEFAULTS}))
        read = m.file_lines

        def eio(path, section=None):
            if path == '/' + unit:
                raise OSError(5, 'Input/output error', path)
            return read(path, section)
        with mock.patch.object(m, 'file_lines', eio), self.assertRaises(OSError):
            m.stop_defaults_used()
        if os.geteuid() == 0:
            self.skipTest('root reads a mode 000 file')
        got = self.job_units({unit: {'text': OLD_JOB[unit], 'mode': '000'}})
        self.assertTrue(got.startswith('FAIL'), got)
        self.assertIn('cannot tell (PermissionError', got)

    def test_linux_launcher_shipped_refuses_an_unsafe_launcher_itself(self):
        """Issue #316 item 3 (the #306 lens nit): launcher_shipped refuses a launcher that is a link (O_NOFOLLOW), not
        this uid's alone (launcher_safe on what it opened), or no plain file (a FIFO: opened without blocking), before
        it reads it; ask() refuses the same later, so this tests launcher_shipped alone. A safe copy of the launcher
        passes; a safe one with other bytes does not. Issue #365 item 4: a call that blocks (the FIFO opened without
        O_NONBLOCK) fails in no_wait's time, never hangs the run, and never passes as a refusal (issue #408 item 2)."""
        m = fw_linux()
        folder = os.path.join(self.tmp, 'kit')
        os.makedirs(folder, mode=0o755)
        safe = os.path.join(folder, 'safe.sh')
        write(safe, slurp(LAUNCHER))
        os.chmod(safe, 0o755)
        m.LAUNCHER = safe
        self.assertIsNone(m.launcher_shipped())
        other = os.path.join(folder, 'other.sh')
        write(other, slurp(LAUNCHER) + '# a comment\n')
        os.chmod(other, 0o755)
        m.LAUNCHER = other
        with self.assertRaisesRegex(ValueError, 'is not the launcher these walls ship with'):
            m.launcher_shipped()
        m.LAUNCHER = safe
        link = os.path.join(folder, 'link.sh')
        os.symlink(safe, link)
        writable = os.path.join(folder, 'writable.sh')
        shutil.copy(safe, writable)
        os.chmod(writable, 0o775)
        fifo = os.path.join(folder, 'fifo.sh')
        os.mkfifo(fifo, 0o755)
        # Each refusal by its own message (issue #408 item 2, the #390 security lens's M7b): a Waited reworded into a
        # ValueError (a FIFO opened without O_NONBLOCK, behind a wrapper) is not launcher_safe's refusal.
        with self.subTest('link.sh'):
            m.LAUNCHER = link
            with no_wait(), self.assertRaises(OSError) as got:
                m.launcher_shipped()
            self.assertEqual(got.exception.errno, errno.ELOOP)   # O_NOFOLLOW's own refusal
        for path in (writable, fifo):
            with self.subTest(os.path.basename(path)):
                m.LAUNCHER = path
                with no_wait(), self.assertRaisesRegex(ValueError, 'is a link, or not owned by uid'):
                    m.launcher_shipped()

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
        got = self.etc_secrets({'usr/lib/systemd/system/b.service':   # a unit jobs see
                                '[Service]\nDescription=x\\\\\nEnvironment=RESTIC_PASSWORD=hunter2\n'})
        self.assertTrue(got.startswith('FAIL'), got)

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

    def test_linux_public_only_is_tight(self):
        """Issue #226 item 5: a file passes as public only when each block's body is base64 of what that block is
        (DER for a certificate, a CRL or a public key; an OpenPGP public key packet), and each SSH public key line is
        a known type, base64 of that key, and at most a short plain comment: no options (environment="..."). A
        password inside the markers, or in a long comment, FAILS: here as a .key file (a secret's name) dropped into
        /etc/ssl/certs, which jobs see (issue #370 item 2)."""
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
                got = self.etc_secrets({'etc/ssl/certs/k.key': text})
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
                got = self.etc_secrets({'etc/ssl/certs/k.key': text})
                self.assertTrue(got.startswith('PASS'), got)

    def test_linux_mounts_refuses_an_etc_entry_that_holds_secrets(self):
        """Issue #370 item 1: ALLOW_PATHS may name any one /etc entry; one that holds this host's secrets (shadow,
        sudoers, ssh, /etc/default, cron, systemd, ...), is in one, or is a folder that holds one (/etc/ssl holds
        ssl/private), or any folder named private fails `mounts` before every start, by its path as written and as
        it really is. The list's own entries and a project's harmless one pass. State: an entry the host lacks is
        bound by no one (the launcher skips it), so it passes; no ALLOW_PATHS, or an empty one, passes; the same
        answer again on a second run."""
        conf = 'etc/fork-fleet/launch.conf'
        fails = {'/etc/shadow': {'etc/shadow': {'text': 'root:*:1::::::\n', 'mode': '600'}},
                 '/etc/sudoers.d': {'etc/sudoers.d/ci': {'text': 'ci ALL=(ALL) NOPASSWD: ALL\n', 'mode': '440'}},
                 '/etc/ssh': {'etc/ssh/sshd_config': 'PermitRootLogin no\n'},
                 '/etc/ssl': {'etc/ssl/certs/ca.pem': 'x\n'},   # holds ssl/private, there or not
                 '/etc/ssl/private': {'etc/ssl/private/': ''},
                 '/etc/pki': {'etc/pki/tls/certs/': ''},
                 '/etc/default/backup': {'etc/default/backup': 'X=1\n'},
                 '/etc/cron.daily': {'etc/cron.daily/': ''},
                 '/etc/systemd/system': {'etc/systemd/system/': ''},
                 '/etc/auto.master': {'etc/auto.master': '/net -hosts\n'},
                 '/etc/myapp/private': {'etc/myapp/private/': ''},
                 '/etc/nsswitch.conf,/etc/fstab': {}}   # a harmless entry beside one that is not
        for allow, files in fails.items():
            with self.subTest(allow):
                p = run([os.path.join(self.folder, 'linux'), 'check'],
                        self.root(overlay(LINUX, {**files, conf: f'ALLOW_PATHS={allow}\n'})))
                self.assertRegex(p.stdout, r'FAIL  mounts  .*a path that holds this host\'s secrets or account names')
                self.assertIn(allow.split(',')[-1], p.stdout)
                # Issue #613 item 3: the way out is the owner's line, not the installer.
                self.assertIn('take it out of ALLOW_PATHS, or of the JOB_PATHS file', p.stdout)
        # Issues #594, #613 item 2: what names this host's accounts or LAN hosts, and each folder of one: the launcher
        # refuses each itself now (it used to start a job with ALLOW_PATHS=/etc/passwd-), so `mounts` cannot tell
        # and names the launcher's refusal, which names ALLOW_PATHS.
        accounts = {'/etc/passwd-': {'etc/passwd-': 'alice:x:1000:1000:Alice Example:/home/alice:/bin/bash\n'},
                    '/etc/group-': {'etc/group-': 'sudo:x:27:alice\n'},
                    '/etc/subuid': {'etc/subuid': 'alice:100000:65536\n'},
                    '/etc/subgid-': {'etc/subgid-': 'alice:100000:65536\n'},
                    '/etc/hosts.allow': {'etc/hosts.allow': 'sshd: 192.0.2.0/24\n'},
                    '/var/backups': {'var/backups/passwd.bak': 'alice:x:1000:1000:Alice:/home/alice:/bin/bash\n'},
                    '/var/backups/passwd.bak': {'var/backups/passwd.bak': 'alice:x:1000:1000::/home/alice:/bin/sh\n'},
                    '/var/log/wtmp': {'var/log/wtmp': 'alice\n'},
                    '/var/lib/extrausers': {'var/lib/extrausers/passwd': 'alice:x:1000:1000::/home/alice:/bin/sh\n'},
                    '/var': {'var/backups/group.bak': 'sudo:x:27:alice\n'},   # a folder that holds it
                    # Issue #636: more of the usual places.
                    '/etc/passwd.dpkg-old': {'etc/passwd.dpkg-old': 'alice:x:1000:1000::/home/alice:/bin/sh\n'},
                    '/etc/group.OLD': {'etc/group.OLD': 'sudo:x:27:alice\n'},
                    '/etc/hosts.equiv': {'etc/hosts.equiv': 'build-host alice\n'},
                    '/var/mail': {'var/mail/alice': ''},
                    '/var/spool/mail/alice': {'var/spool/mail/alice': ''},
                    '/var/lib/sss/mc': {'var/lib/sss/mc/passwd': 'alice\n'},
                    '/var/lib/dhcp': {'var/lib/dhcp/dhclient.leases': 'lease { option host-name "nas"; }\n'},
                    # Issue #656: the other DHCP clients' leases.
                    '/var/lib/dhclient': {'var/lib/dhclient/dhclient.leases': 'lease { option host-name "nas"; }\n'},
                    '/var/lib/dhcpcd': {'var/lib/dhcpcd/eth0.lease': 'nas\n'},
                    '/var/lib/NetworkManager': {'var/lib/NetworkManager/internal-x.lease': 'HOSTNAME=nas\n'},
                    '/var/lib/misc': {'var/lib/misc/dnsmasq.leases': '1 aa:bb:cc:dd:ee:ff 192.0.2.9 nas *\n'}}
        for allow, files in accounts.items():
            with self.subTest(allow):
                p = run([os.path.join(self.folder, 'linux'), 'check'],
                        self.root(overlay(LINUX, {**files, conf: f'ALLOW_PATHS={allow}\n'})))
                self.assertIn('FAIL  mounts  ', p.stdout)
                self.assertIn(f'the launcher refuses what jobs would see: fork-fleet-launch: {allow}: names this host\'s accounts or LAN '
                              'hosts', p.stdout)
                self.assertIn('take it out of ALLOW_PATHS', p.stdout)
        for allow in ('', '/etc/myapp', '/etc/java-17-openjdk', '/etc/ssl/certs', '/var/cache', '/etc/host.conf',
                      '/etc/shadow', '/etc/ssh'):
            with self.subTest(allow or 'none'):   # the last two are not on this host: nothing bound
                root = self.root(overlay(LINUX, {conf: f'ALLOW_PATHS={allow}\n', 'etc/myapp/app.conf': 'A=1\n',
                                                 'etc/java-17-openjdk/': '', 'etc/ssl/certs/': '',
                                                 'var/cache/apt/': '', 'etc/host.conf': 'multi on\n'}))
                for _ in range(2):
                    self.assertNotIn('mounts', self.walls('linux', 'check', root)[1])
        root = self.root(overlay(LINUX, {conf: None}))   # no launch.conf at all
        self.assertNotIn('mounts', self.walls('linux', 'check', root)[1])

    def test_linux_mounts_reads_the_real_path_and_only_the_last_line_is_resolv(self):
        """The deny list reads each `paths` line's real path too (a link above it), and skips only the last line, the
        launcher's own /etc/resolv.conf for jobs, which it keeps in /etc/fork-fleet (a secret folder: its conf)."""
        launcher = os.path.join(os.path.dirname(self.folder), 'fork-fleet-launch.sh')
        resolv = '/etc/resolv.conf /etc/fork-fleet/resolv.conf\\n'
        for lines, bad in (('/usr /usr\\n' + resolv, False),
                           ('/usr /usr\\n/opt/x /etc/shadow\\n' + resolv, True),
                           ('/usr /usr\\n/etc/x /etc/fork-fleet/launch.conf\\n' + resolv, True),
                           (resolv + '/usr /usr\\n', True),   # not last: not the launcher's own
                           ('/usr /usr\\n/etc/resolv.conf /etc/ssl/private/r\\n', True),
                           ('/usr /usr\\n/etc/resolv.conf /etc/fork-fleet/launch.conf\\n', True),
                           ('/usr /usr\\n/etc/resolv.conf /etc/dns/resolv.conf\\n', False),   # RESOLV_CONF set
                           # Issue #594: an older launcher that binds the host's own passwd, group or hosts, or a
                           # backup of them through a link.
                           ('/usr /usr\\n/etc/passwd /etc/passwd\\n' + resolv, True),
                           ('/usr /usr\\n/etc/hosts /etc/hosts\\n' + resolv, True),
                           ('/usr /usr\\n/opt/b /var/backups\\n' + resolv, True),
                           # Issue #613 item 1: each of these alone (no other line names it).
                           ('/usr /usr\\n/etc/group /etc/group\\n' + resolv, True),
                           ('/usr /usr\\n/etc/subuid- /etc/subuid-\\n' + resolv, True),
                           ('/usr /usr\\n/etc/subgid- /etc/subgid-\\n' + resolv, True),
                           ('/usr /usr\\n/opt/p /var/backups/passwd.bak\\n' + resolv, True),
                           ('/usr /usr\\n/var/log/wtmp /var/log/wtmp\\n' + resolv, True),
                           ('/usr /usr\\n/var/log/lastlog /var/log/lastlog\\n' + resolv, True),
                           ('/usr /usr\\n/opt/x /var/lib/extrausers/passwd\\n' + resolv, True),
                           ('/usr /usr\\n/etc/hosts.allow /etc/hosts.allow\\n' + resolv, True),
                           ('/usr /usr\\n/etc/hosts.deny /etc/hosts.deny\\n' + resolv, True),
                           ('/usr /usr\\n/etc/networks /etc/networks\\n' + resolv, True),
                           ('/usr /usr\\n/etc/ethers /etc/ethers\\n' + resolv, True),
                           ('/usr /usr\\n/var/lib/apt /var/lib/apt\\n' + resolv, False),
                           ('/usr /usr\\n/var/cache /var/cache\\n' + resolv, False)):
            with self.subTest(lines):
                write(launcher, f'#!/bin/sh\ncase $1 in paths) printf "{lines}" ;; *) exit 1 ;; esac\n')
                os.chmod(launcher, 0o755)
                p = run([os.path.join(self.folder, 'linux'), 'check'], self.root(LINUX))
                (self.assertRegex if bad else self.assertNotRegex)(p.stdout, r'FAIL  mounts  .*holds this host')

    def fake_paths(self, line, root):
        """`linux check` on root with a launcher whose `paths` prints /usr, line and the jobs' resolv.conf."""
        launcher = os.path.join(os.path.dirname(self.folder), 'fork-fleet-launch.sh')
        write(launcher, f'#!/bin/sh\ncase $1 in paths) printf "/usr /usr\\n{line}\\n'
                        '/etc/resolv.conf /etc/fork-fleet/resolv.conf\\n" ;; *) exit 1 ;; esac\n')
        os.chmod(launcher, 0o755)
        return run([os.path.join(self.folder, 'linux'), 'check'], root).stdout

    def test_linux_mounts_names_where_the_line_is(self):
        """Issue #613 item 3: a secret entry today's launcher refuses itself (LAUNCHER_REFUSES) is bound only by an
        older launcher: re-run the installer, whose launcher then names the line. Any other (/etc/shadow) is the
        owner's own line: ALLOW_PATHS or the JOB_PATHS file the conf names, never the installer's list."""
        older = 'bound by an older launcher, which does not refuse it itself: re-run the installer'
        owner = 'take it out of ALLOW_PATHS, or of the JOB_PATHS file the launcher\'s conf names'
        for line, want in (('/etc/passwd /etc/passwd', older), ('/opt/p /etc/passwd-', older),
                           ('/var/log /var/log', older), ('/etc/shadow /etc/shadow', owner),
                           ('/opt/k /etc/ssl/private', owner), ('/etc/ssh /etc/ssh', owner)):
            with self.subTest(line):
                got = self.fake_paths(line, self.root(LINUX))
                self.assertIn('FAIL  mounts  ', got)
                self.assertIn(want, got)
                self.assertNotIn(owner if want == older else older, got)
        # Issue #636: both kinds at once name both ways out in one run (it took a run each).
        for line in ('/etc/passwd /etc/passwd\\n/etc/shadow /etc/shadow', '/etc/ssh /etc/ssh\\n/opt/l /var/log'):
            with self.subTest(line):
                got = self.fake_paths(line, self.root(LINUX))
                self.assertIn('FAIL  mounts  ', got)
                self.assertIn('an older launcher binds some: re-run the installer; take the rest out of ALLOW_PATHS or '
                              'JOB_PATHS; the paths: ', got)
        # Issue #656: the ways out come before the paths, so long paths that fill the line's 400-character cap never
        # cut the owner's half off (it was after three of them).
        long = '/opt/' + 'v' * 150
        for line in (f'/etc/passwd /etc/passwd\\n{long}/a /etc/shadow\\n{long}/b /etc/ssh',
                     f'{long}/a /etc/shadow\\n{long}/b /etc/ssh\\n{long}/c /etc/passwd'):
            with self.subTest('long paths'):
                got = self.fake_paths(line, self.root(LINUX))
                said = next(x for x in got.splitlines() if x.startswith('FAIL  mounts  '))
                self.assertGreater(len(said), 390, said)   # the cap did cut it
                self.assertIn('re-run the installer; take the rest out of ALLOW_PATHS or JOB_PATHS', said)

    def test_linux_mounts_finds_a_secret_at_another_path_by_its_inode(self):
        """Issue #613 item 7: a bind mount or hard link of a secret entry at another path is the same device and
        inode, so `mounts` fails on it whatever its name: the entry itself (a hard link of /etc/shadow, a bind of
        /etc/ssl/private), a file in such a folder seen through the other path, and a folder that holds one (a bind of
        /etc). A file or folder that only shares a parent (/etc/ssl/certs) passes. State: a host with no secret entry
        has nothing to match; one that cannot be read cannot be told."""
        def root_with(links=()):
            root = self.root(overlay(LINUX, {'etc/shadow': {'text': 'root:*:1::::::\n', 'mode': '600'},
                                             'etc/ssl/private/k.key': 'k\n', 'etc/ssl/certs/ca.pem': 'c\n',
                                             'opt/keys/k.key': 'k\n', 'opt/e/': ''}))
            for target, link in links:
                os.link(os.path.join(root, target), os.path.join(root, link))
            return root
        got = self.fake_paths('/opt/s /opt/s', root_with([('etc/shadow', 'opt/s')]))
        self.assertRegex(got, r'FAIL  mounts  .*: jobs see a bind mount or hard link .*: take it out of '
                              r'ALLOW_PATHS.*; the paths: /opt/s \(the same as /etc/shadow\)')
        got = self.fake_paths('/opt/c /opt/c', root_with([('etc/ssl/certs/ca.pem', 'opt/c')]))   # not a secret
        self.assertNotIn('FAIL  mounts', got)
        got = self.fake_paths('/opt/certs /etc/ssl/certs', root_with())
        self.assertNotIn('FAIL  mounts', got)
        # A folder bound elsewhere: os.stat of the other path gives the folder's device and inode, as a bind does.
        real_stat = os.stat
        for other, want in (('opt/keys', '/etc/ssl/private'),
                            ('opt/keys/k.key', '/etc/ssl/private, a folder that holds it'),
                            ('opt/e', '/etc, a folder that holds '),
                            # Issue #636: any folder named private under /etc, as secret_etc has it, not only the
                            # listed ones.
                            ('opt/app', '/etc/myapp/private'),
                            ('opt/app/k.key', '/etc/myapp/private, a folder that holds it'),
                            ('opt/deep', '/etc/a/b/c/private')):
            with self.subTest(other):
                root = root_with()
                os.makedirs(os.path.join(root, 'etc/myapp/private'))
                write(os.path.join(root, 'etc/myapp/private/k.key'), 'k\n')
                os.makedirs(os.path.join(root, 'etc/a/b/c/private'))
                os.makedirs(os.path.join(root, 'opt/app'))
                write(os.path.join(root, 'opt/app/k.key'), 'k\n')
                m = fw_linux()
                m.ROOT = root
                bound = {os.path.join(root, 'opt/keys'): os.path.join(root, 'etc/ssl/private'),
                         os.path.join(root, 'opt/e'): os.path.join(root, 'etc'),
                         os.path.join(root, 'opt/app'): os.path.join(root, 'etc/myapp/private'),
                         os.path.join(root, 'opt/deep'): os.path.join(root, 'etc/a/b/c/private')}

                def fake_stat(p, *a, **k):
                    return real_stat(bound.get(p, p), *a, **k)
                with mock.patch.object(m.os, 'stat', fake_stat):
                    self.assertIn(want, m.same_as_secret('/' + other, m.secret_inodes()) or '')
        # Missing state: a host with none of the secret entries has nothing to match; an entry that cannot be read
        # cannot be told.
        m = fw_linux()
        m.ROOT = self.root({'usr/': ''})
        self.assertIsNone(m.same_as_secret('/usr', m.secret_inodes()))
        m.ROOT = root_with()
        with mock.patch.object(m.os, 'stat', side_effect=PermissionError(13, 'Permission denied')):
            with self.assertRaisesRegex(ValueError, 'cannot read /etc/shadow'):
                m.secret_inodes()
        # Issue #636: a folder of /etc that cannot be listed may hold a private one: cannot tell. No /etc: none.
        locked = os.path.join(m.ROOT, 'etc/locked')
        os.makedirs(locked)
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        if os.geteuid() != 0:
            with self.assertRaisesRegex(ValueError, 'cannot list /etc/locked'):
                m.secret_inodes()
        m.ROOT = self.root({'usr/': ''})
        self.assertEqual(m.private_entries(), ([], []))
        # Issue #656: a file named private under /etc is one too. A link named private (#667's lens A, minor 2) makes
        # what it leads to secret, and what is in that, but not the folders above it: /etc/private -> /usr/share must
        # not refuse /usr or / (the false refusal of #649's nit), and /etc/b/private -> /srv/app-keys still protects
        # the keys; the link's own path is secret by name (secret_etc). A dangling one has nothing to protect.
        m.ROOT = self.root({'etc/app/private': 'k\n', 'usr/share/doc/': '', 'srv/app-keys/sub/key': 'k\n'})
        os.symlink('../usr/share', os.path.join(m.ROOT, 'etc/private'))
        os.makedirs(os.path.join(m.ROOT, 'etc/b'))
        os.symlink('../../srv/app-keys', os.path.join(m.ROOT, 'etc/b/private'))
        os.makedirs(os.path.join(m.ROOT, 'etc/c'))
        os.symlink('/gone', os.path.join(m.ROOT, 'etc/c/private'))
        self.assertEqual(m.private_entries(), ([os.path.join(m.ROOT, 'etc/app/private')],
                                               [os.path.join(m.ROOT, f'etc/{d}private') for d in ('b/', 'c/', '')]))
        inodes = m.secret_inodes()
        for p in ('/usr', '/srv'):
            self.assertIsNone(m.same_as_secret(p, inodes), p)
        for p, what in (('/usr/share', '/etc/private, a link named private'),
                        ('/usr/share/doc', '/etc/private, a link named private'),
                        ('/srv/app-keys', '/etc/b/private, a link named private'),
                        ('/srv/app-keys/sub', '/etc/b/private, a link named private'),
                        ('/etc/app/private', '/etc/app/private')):
            self.assertIn(what, m.same_as_secret(p, inodes) or '', p)
        self.assertTrue(m.secret_etc('/etc/private') and m.secret_etc('/etc/b/private'))
        # Runs again: the same answer.
        self.assertEqual(m.secret_inodes(), inodes)
        # Runs again: the same answer.
        root = root_with([('etc/shadow', 'opt/s')])
        self.assertEqual(self.fake_paths('/opt/s /opt/s', root), self.fake_paths('/opt/s /opt/s', root))

    def test_linux_secret_lists_and_the_launchers_own_refusals_are_one(self):
        """Issue #613 item 2: the launcher refuses the account files itself (it used to start a job with
        ALLOW_PATHS=/etc/passwd- or /var/backups; only the walls refused). The walls' LAUNCHER_REFUSES is exactly what
        it refuses of their lists (it is how the walls tell an older launcher's line from the owner's), and each is in
        the walls' own lists too, for an older launcher. Runs the launcher's `paths` on each."""
        m = fw_linux()
        launcher = os.path.join(os.path.dirname(self.folder), 'fork-fleet-launch.sh')
        conf = 'etc/fork-fleet/launch.conf'
        each = {p.replace('*', 'x') for p in (*m.SECRET_ETC, *m.SECRET_ELSEWHERE, *m.LAUNCHER_REFUSES)}
        each |= {'/var/log/wtmp', '/var/lib/extrausers/passwd', '/var/backups/group.bak', '/etc/ssl', '/var/cache',
                 # Issue #636: more of the usual places, and their neighbours that are not.
                 '/etc/passwd.bak', '/etc/hosts.equiv', '/var/mail/alice', '/var/spool/mail/alice',
                 '/var/lib/sss/mc/passwd', '/var/lib/sss/db', '/var/lib/dhcp/dhclient.leases', '/etc/hostname',
                 '/etc/host.conf', '/etc/groff', '/var/lib/misc/other', '/var/lib/apt',
                 # Issue #656: the other DHCP clients' leases, and a neighbour that is not one.
                 '/var/lib/dhclient/dhclient.leases', '/var/lib/dhcpcd/eth0.lease',
                 '/var/lib/NetworkManager/internal-x.lease', '/var/lib/NetworkManager', '/var/lib/dpkg'}
        for p in sorted(each):
            with self.subTest(p):
                got = run([launcher, 'paths'], self.root(overlay(LINUX, {conf: f'ALLOW_PATHS={p}\n'})))
                refused = any(x in got.stderr for x in ('a job never sees it', 'never in a job\'s view',
                                                        'a job has the launcher\'s own'))
                self.assertEqual(refused, m.launcher_refuses(p), got.stderr)
                self.assertEqual(got.returncode != 0, refused, got.stderr)
                if refused:
                    self.assertIn('take it out of ALLOW_PATHS', got.stderr)
                if p != '/etc' and m.launcher_refuses(p):
                    self.assertTrue(m.secret_etc(p), p)
        # Issue #636 item 4: and the other way: every pattern never() refuses (but its own-or-none and data roots) is
        # one of LAUNCHER_REFUSES. A refusal added to the launcher alone stayed green before, and the walls then took
        # an older launcher's line for the owner's.
        with open(launcher) as f:
            body = re.search(r'^never\(\) \{\n(.*?)^\}\n', f.read(), re.M | re.S).group(1)
        theirs = set(re.findall(r'\[\[ \$1 != (/\S+) \]\]', body))
        blocks = re.findall(r'case \$1 in\s*(.*?)\)\s*die', body, re.S)
        for block in blocks:
            theirs |= {x.strip() for x in block.replace('\\\n', ' ').split('|')}
        self.assertIn('/var/lib/NetworkManager/*', theirs)   # the parse found the whole list
        self.assertEqual(theirs, set(m.LAUNCHER_REFUSES))
        # Issue #656: and nothing the parse skips. A refusal in another shape (an `if`, a `[[ $1 == ...`) stayed green:
        # every line that reads $1 is one of the shapes above (or the data roots' no_data), and each die is one of
        # theirs, or the own-or-none roots' one.
        own = re.findall(r'case \$1/ in', body)
        self.assertEqual(body.count('die '), len(re.findall(r'\[\[ \$1 != /\S+ \]\] \|\| die ', body)) + len(blocks) +
                         len(own), body)
        for line in body.splitlines():
            if '$1' in line:
                self.assertRegex(line.strip(), r'^(\[\[ \$1 != /\S+ \]\] \|\| die |case \$1/? in( |$)|'
                                 r'no_data "\$1" "\$2"$)', line)

    def test_linux_etc_secrets_reads_only_what_jobs_see(self):
        """Issue #243 items 5 and 6: jobs see an allow list of /etc, so a secret in a login file, a unit or
        /etc/default, cron, fstab, a credentials file or a backup tool's folder there is no job's to read: each
        PASSES now (each FAILED before, when the lint read all of /etc). The same secret in what jobs do see FAILS:
        a file of the /etc view (issue #370 item 2: ssl/certs, an ALLOW_PATHS folder), /usr/local's programs and
        settings, the packages' units and environment files, systemd's credential stores in /usr."""
        out_of_view = {'etc/environment': 'GITHUB_TOKEN=ghp_example\n',
                       'etc/profile.d/tools.sh': "export OPENAI_API_KEY='sk-example'\n",
                       'etc/default/backup': 'RESTIC_PASSWORD=hunter2\n',
                       'etc/fstab': '//nas.example/share /srv/share cifs username=u,password=hunter2 0 0\n',
                       'etc/nas.cred': 'password=p\n',
                       'etc/cron.d/backup': '0 3 * * * root RESTIC_PASSWORD=hunter2 restic backup\n',
                       'etc/systemd/system/backup.service': '[Service]\nEnvironment=RESTIC_PASSWORD=hunter2\n',
                       'etc/restic/pw': 'gh' + 'p_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n',
                       'usr/lib/systemd/system/b.service': '[Service]\nEnvironmentFile=/etc/default/backup\n'
                                                           'LoadCredential=pw:/etc/restic/pw\n'
                                                           'Environment=RESTIC_PASSWORD_FILE=/etc/restic/pw\n'}
        got = self.etc_secrets(out_of_view)
        self.assertTrue(got.startswith('PASS'), got)
        key = '-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIE\n-----END PRIVATE KEY-----\n'
        in_view = {'etc/ssl/certs/server.pem': '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n' + key,
                   'etc/pki/tls/certs/server.crt': key,
                   'etc/ssl/certs/backup.p12': 'x',
                   'usr/local/sbin/backup': {'text': '#!/bin/bash\nRESTIC_PASSWORD=hunter2 restic backup\n',
                                             'mode': '755'},
                   'usr/local/bin/b.sh': {'text': 'setenv NAS_PASSWORD hunter2\n', 'mode': '755'},
                   'usr/local/etc/restic.conf': 'repo=s3://me:hunter2@s3.example/b\n',
                   'usr/local/lib/systemd/system/b.service': '[Service]\nEnvironment="A=1" DB_PASSWORD=hunter2\n',
                   'usr/lib/systemd/user/b.service': '[Service]\nEnvironment=NPM_TOKEN=npm_' + 'a' * 36 + '\n',
                   'usr/local/lib/environment.d/50-x.conf': 'DEPLOY_TOKEN=not-a-real-token\n',
                   'usr/local/share/fish/vendor_conf.d/x.fish': 'set -gx GH_TOKEN abc123secretvalue\n',
                   'usr/local/lib/credstore/db': 'hunter2\n'}
        for path, text in in_view.items():
            with self.subTest(path):
                got = self.etc_secrets({path: text})
                self.assertTrue(got.startswith('FAIL'), got)
                self.assertIn('/' + path, got)
        got = self.etc_secrets({'etc/fork-fleet/launch.conf': 'ALLOW_PATHS=/etc/myapp\n',
                                'etc/myapp/conf.d/app.conf': 'API_KEY=hunter2hunter2\n'})
        self.assertRegex(got, r'^FAIL .*/etc/myapp/conf.d/app.conf line 1 \(API_KEY: a secret\)')
        got = self.etc_secrets({'etc/myapp/conf.d/app.conf': 'API_KEY=hunter2hunter2\n'})   # not allowed: unseen
        self.assertTrue(got.startswith('PASS'), got)

    def test_linux_etc_secrets_what_a_job_can_read(self):
        """A file a job cannot read is fine: no other bit, or a folder above it (from the path the launcher binds)
        it cannot enter. A group bit counts when the group is not root's (the runner may be in it; issue #243 open
        form 15), or when an ACL is set (its mask is the group bits). Links are not read (their target is, if a job
        sees it), and a *_FILE through a link is read where the link leads: out of the view is fine."""
        secret = 'DB_PASSWORD=hunter2\n'
        for mode, want in (('600', 'PASS'), ('640', 'FAIL'), ('604', 'FAIL'), ('644', 'FAIL')):
            with self.subTest(mode):   # the test user's own group is not root's
                got = self.etc_secrets({'usr/local/etc/app.env': {'text': secret, 'mode': mode}})
                self.assertTrue(got.startswith(want), got)
        got = self.etc_secrets({'usr/local/etc/app/app.env': secret},
                               then=lambda r: os.chmod(os.path.join(r, 'usr/local/etc/app'), 0o700))
        self.assertTrue(got.startswith('PASS'), got)
        got = self.etc_secrets({'usr/local/etc/app/app.env': secret},
                               then=lambda r: os.chmod(os.path.join(r, 'usr/local/etc/app'), 0o750))
        self.assertTrue(got.startswith('FAIL'), got)
        m = fw_linux()
        st = os.stat_result((0o100640, 0, 0, 1, 0, 0, 0, 0, 0, 0))   # root's file, root's group, 0640
        path = os.path.join(self.tmp, 'f')
        write(path, 'x')
        self.assertFalse(m.may(path, st, 0o004))
        with mock.patch.object(m.os, 'getxattr', return_value=b'\x02\x00\x00\x00'):
            self.assertTrue(m.may(path, st, 0o004))
        self.assertFalse(m.may(path, os.stat_result((0o100600, 0, 0, 1, 0, 5, 0, 0, 0, 0)), 0o004))
        self.assertTrue(m.may(path, os.stat_result((0o100640, 0, 0, 1, 0, 5, 0, 0, 0, 0)), 0o004))

        def link(name, target):
            def make(root):
                os.symlink(target, os.path.join(root, name))
            return make
        unit = {'usr/lib/systemd/system/r.service': '[Service]\nEnvironment=RESTIC_PASSWORD_FILE=/usr/local/etc/pw\n'}
        for target, files, want in (('../../../etc/default/backup', {'etc/default/backup': 'hunter2\n'}, 'PASS'),
                                    ('../../share/restic/pw', {'usr/share/restic/pw': 'hunter2\n'}, 'FAIL'),
                                    ('/usr/share/restic/pw', {'usr/share/restic/pw': 'hunter2\n'}, 'FAIL')):
            with self.subTest(target):
                got = self.etc_secrets({**unit, **files, 'usr/local/etc/': ''},
                                       then=link('usr/local/etc/pw', target))
                self.assertTrue(got.startswith(want), got)

    def test_linux_etc_secrets_unit_paths_it_cannot_know_fail_closed_in_view_only(self):
        """A unit's path systemd fills in (a specifier, a glob) fails closed when the folder it is in for sure is in
        a job's view or holds part of it; out of the view (%E/default: /etc/default), in a home (%h, ~) or in the
        unit's own credentials (%d, $CREDENTIALS_DIRECTORY) it passes. A .., a relative path, a backslash or a
        *_COMMAND fails closed; a credential by name is systemd's store's (CREDSTORES, read on their own)."""
        cases = {'EnvironmentFile=-%E/default/%i.env': 'PASS',
                 'EnvironmentFile=%E/%i': 'FAIL',
                 'LoadCredential=pw:/usr/share/%i/pw': 'FAIL',
                 'LoadCredential=pw:/usr/share/x*/pw': 'FAIL',
                 'LoadCredential=pw': 'PASS',
                 'LoadCredential=pw:restic.pw': 'PASS',
                 'Environment=RESTIC_PASSWORD_FILE=%d/restic': 'PASS',
                 'Environment=RESTIC_PASSWORD_FILE=${CREDENTIALS_DIRECTORY}/restic': 'PASS',
                 'Environment=RESTIC_PASSWORD_FILE=%h/.restic': 'PASS',
                 'Environment=RESTIC_PASSWORD_FILE=%d/../../usr/share/pw': 'FAIL',
                 'Environment=RESTIC_PASSWORD_FILE=restic.pw': 'FAIL',
                 'Environment=RESTIC_PASSWORD=hunter\\x32': 'FAIL',
                 'Environment="RESTIC_PASSWORD_COMMAND=cat /x"': 'FAIL',
                 'Environment=RESTIC_PASSWORD_FILE=/usr/share/none': 'PASS',   # not there: nothing to read
                 'Environment=RESTIC_PASSWORD=/usr/share/none': 'FAIL',   # a password that starts with /
                 'Environment=RESTIC_PASSWORD=': 'PASS',
                 'Environment=RESTIC_PASSWORD=$PW': 'PASS',
                 'Environment=PASSWORD_MIN_LENGTH=12': 'PASS'}
        for line, want in cases.items():
            with self.subTest(line):
                got = self.etc_secrets({'usr/lib/systemd/system/b.service': f'[Service]\n{line}\n'})
                self.assertTrue(got.startswith(want), got)
                if want == 'FAIL':
                    self.assertIn('/usr/lib/systemd/system/b.service line 2', got)

    def test_linux_etc_secrets_programs_and_empty_or_missing_state(self):
        """Only a shell script's variables are read in /usr/local/bin (a Python program's key=lambda is no secret);
        any program's token shapes are. State: a host with no /usr/local, no units folder or an empty credential
        store passes; the same answer on a second run (nothing kept between runs); a file gone between the listing
        and the read is skipped, one the walls cannot read but a job may fails closed."""
        py = {'text': '#!/usr/bin/python3\nPASSWORD=input()\nkey=lambda x: x\n', 'mode': '755'}
        sh = {'text': '#!/usr/bin/env -S bash -e\nexport PASSWORD=hunter2\n', 'mode': '755'}
        self.assertTrue(self.etc_secrets({'usr/local/bin/tool': py}).startswith('PASS'))
        self.assertTrue(self.etc_secrets({'usr/local/bin/tool': sh}).startswith('FAIL'))
        tok = {'text': '#!/usr/bin/python3\nT = "gh' + 'p_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"\n', 'mode': '755'}
        self.assertTrue(self.etc_secrets({'usr/local/bin/tool': tok}).startswith('FAIL'))
        empty = self.root(overlay(LINUX, {'usr/local/': None, 'usr/lib/': None, 'usr/lib/credstore/': ''}))
        for _ in range(2):
            self.assertNotIn('etc-secrets', self.walls('linux', 'check', empty)[1])
        m = fw_linux()
        root = self.root({'usr/local/etc/a.env': 'X=1\n'})
        with mock.patch.object(m, 'ROOT', root):
            os.remove(os.path.join(root, 'usr/local/etc/a.env'))
            self.assertEqual(m.file_secrets([('/usr', '/usr')], '/usr/local/etc/a.env', 'env'), [])
            write(os.path.join(root, 'usr/local/etc/b.env'), 'X=1\n')
            with mock.patch('builtins.open', side_effect=PermissionError(13, 'Permission denied')):
                self.assertEqual(m.file_secrets([('/usr', '/usr')], '/usr/local/etc/b.env', 'env'),
                                 [('/usr/local/etc/b.env', 'cannot read it (Permission denied)')])

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
        def lists_etc(): write(allow, '/usr\n/etc\n')   # an older kit's list (issue #243 item 4)
        def none(): pass   # in place: a desktop with shares and data disks mounted outside the list holds
        for change in (none, gone, open_list, a_link, lists_home, lists_dots, lists_etc):
            with self.subTest(change.__name__):
                self.folder = os.path.join(os.path.dirname(self.install()), 'fork-fleet-walls')
                allow = os.path.join(os.path.dirname(self.folder), JOB_PATHS)
                change()
                fails = self.walls('linux', 'check', self.root(LINUX))[1]
                (self.assertNotIn if change is none else self.assertIn)('mounts', fails)

    def test_linux_mounts_fails_on_all_of_etc_from_an_older_launcher(self):
        """Issue #243 items 3 and 4: jobs see an allow list of /etc, never all of it. A launcher whose `paths` still
        binds /etc whole (an older kit: today's launcher refuses that list itself) fails `mounts`, before every start
        (fork-fleet-walls.py check), with what to do: re-run the installer. Its listed entries pass."""
        launcher = os.path.join(os.path.dirname(self.folder), 'fork-fleet-launch.sh')
        for lines, bad in (('/usr /usr\\n/etc /etc\\n', True), ('/usr /usr\\n/etc/x /etc\\n', True),
                           ('/usr /usr\\n/etc/nsswitch.conf /etc/nsswitch.conf\\n/etc/ssl/certs /etc/ssl/certs\\n', False)):
            with self.subTest(lines):
                write(launcher, f'#!/bin/sh\ncase $1 in paths) printf "{lines}" ;; *) exit 1 ;; esac\n')
                os.chmod(launcher, 0o755)
                p = run([os.path.join(self.folder, 'linux'), 'check'], self.root(LINUX))
                if bad:
                    self.assertRegex(p.stdout, r'FAIL  mounts  .*jobs see all of /etc \(.*\): an older kit\'s list; '
                                               r're-run the installer')
                else:
                    self.assertNotIn('FAIL  mounts', p.stdout)

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
