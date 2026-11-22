"""heavy-install on a fake host in a temporary folder (every fixed path under PREFIX, the current user as root).
No root, no systemd, no network.

  python3 -I -B fork-kit/heavy/tests/install.test.py
"""
import contextlib, importlib.machinery, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPACITY = os.path.join(os.path.dirname(os.path.dirname(KIT)), 'packages', 'shared', 'src', 'capacity')
SHA1, SHA2 = 'a' * 40, 'b' * 40
OLD_WRAPPER = b'#!/bin/sh\nexec systemd-run --user --scope --quiet --slice=heavy.slice --collect -- "$@"\n'
OLD_CAPACITY = b'#!/bin/sh\necho "available=9999MB swapfree=9999MB load=0/4cores"; echo OK\n'   # a host's own copy
CAPACITY_LINK = '/usr/local/libexec/heavy/current/bin/t3-capacity'


def load():
    path = os.path.join(KIT, 'sbin', 'heavy-install')
    loader = importlib.machinery.SourceFileLoader('heavy_install', path)
    spec = importlib.util.spec_from_loader('heavy_install', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Install(unittest.TestCase):
    def setUp(self):
        self.umask = os.umask(0o022)
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='heavy-install-test-'))
        self.addCleanup(self.close)
        self.m = load()
        self.m.PREFIX, self.m.ROOT_UID = self.root, os.geteuid()
        os.makedirs(self.path('/usr/local/bin'))

    def close(self):
        os.umask(self.umask)
        shutil.rmtree(self.root, ignore_errors=True)

    def path(self, p):
        return self.root + p

    def run_main(self, *argv, stop_after=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.m.main(list(argv), stop_after=stop_after)
            except SystemExit as e:
                code = str(e)
        return code, out.getvalue(), err.getvalue()

    def install(self, *extra, sha=SHA1, stop_after=None, source=KIT):
        return self.run_main('install', '--source', source, '--sha', sha, *extra, stop_after=stop_after)

    def config(self):
        with open(self.path('/etc/heavy/config.json')) as f:
            return json.load(f)

    def write_old(self, path, data):
        with open(self.path(path), 'wb') as f:
            f.write(data)
        os.chmod(self.path(path), 0o755)

    def resolve(self, path):
        """Follow the installed (absolute, unprefixed) links in every part of path inside the fake root."""
        parts, done = path.strip('/').split('/'), ''
        for _ in range(40):
            if not parts:
                return self.path(done)
            here = f'{done}/{parts.pop(0)}'
            if os.path.islink(self.path(here)):
                target = os.readlink(self.path(here))
                self.assertTrue(target.startswith('/'), here)
                parts, done = target.strip('/').split('/') + parts, ''
            else:
                done = here
        raise AssertionError(f'{path}: too many links')

    def run_old_capacity(self):
        """The command at /usr/local/bin/t3-capacity runs and is still the host's old copy."""
        real = self.path('/usr/local/bin/t3-capacity')
        self.assertFalse(os.path.islink(real))
        with open(real, 'rb') as f:
            self.assertEqual(f.read(), OLD_CAPACITY)
        r = subprocess.run([real], capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout.splitlines()[-1]), (0, 'OK'))

    def snapshot(self):
        """Every path under the fake root with its kind, mode, mtime and link target."""
        out = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                st = os.lstat(full)
                out[full[len(self.root):]] = (st.st_mode, st.st_mtime_ns,
                                              os.readlink(full) if os.path.islink(full) else None)
        return out

    def test_fresh_install_links_config_record_and_self_check(self):
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(os.readlink(self.path('/usr/local/bin/heavy')), '/usr/local/libexec/heavy/current/bin/heavy')
        self.assertEqual(os.readlink(self.path('/usr/local/libexec/heavy/current')),
                         f'/usr/local/libexec/heavy/versions/{SHA1}')
        self.assertEqual(self.config(), {'version': 1, 'slice': 'heavy.slice', 'grace_seconds': 30})
        self.assertFalse(os.path.exists(self.path('/etc/systemd/user')))
        self.assertEqual(self.run_main('self-check')[0], 0)
        # the installed wrapper runs from its version folder
        r = subprocess.run([sys.executable, '-I', '-B', self.path(f'/usr/local/libexec/heavy/versions/{SHA1}/bin/heavy'),
                            '--help'], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn('heavy <command>', r.stdout)

    def test_runs_again_and_changes_nothing(self):
        self.assertEqual(self.install('--memory-max', '6G')[0], 0)
        before = self.snapshot()
        code, _, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot(), before)

    def test_old_plain_wrapper_is_kept_and_put_back_by_uninstall(self):
        old = self.path('/usr/local/bin/heavy')
        with open(old, 'wb') as f:
            f.write(OLD_WRAPPER)
        os.chmod(old, 0o755)
        code, out, _ = self.install()
        self.assertEqual(code, 0)
        self.assertIn('is kept as /usr/local/libexec/heavy/replaced/heavy.', out)
        self.assertTrue(os.path.islink(old))
        self.assertEqual(self.install()[0], 0)                       # again: one backup only
        self.assertEqual(len(os.listdir(self.path('/usr/local/libexec/heavy/replaced'))), 1)
        code, out, _ = self.run_main('uninstall')
        self.assertEqual(code, 0)
        self.assertFalse(os.path.islink(old))
        with open(old, 'rb') as f:
            self.assertEqual(f.read(), OLD_WRAPPER)
        self.assertEqual(os.stat(old).st_mode & 0o777, 0o755)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy')))
        self.assertFalse(os.path.exists(self.path('/etc/heavy')))
        self.assertEqual(self.run_main('uninstall')[0], 0)            # again: nothing left, still fine

    def test_fresh_install_links_t3_capacity_to_the_release(self):
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        link = self.path('/usr/local/bin/t3-capacity')
        self.assertEqual(os.readlink(link), CAPACITY_LINK)
        self.assertEqual(self.resolve('/usr/local/bin/t3-capacity'),
                         self.path(f'/usr/local/libexec/heavy/versions/{SHA1}/bin/t3-capacity'))
        with open(self.resolve('/usr/local/bin/t3-capacity'), 'rb') as f, \
                open(os.path.join(KIT, 'bin', 't3-capacity'), 'rb') as g:
            self.assertEqual(f.read(), g.read())
        r = subprocess.run([sys.executable, '-I', '-B', self.resolve('/usr/local/bin/t3-capacity'), '--help'],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('t3-capacity [--kind KIND] [MB]', r.stdout)
        with open(self.path('/etc/heavy/installed.tsv')) as f:
            self.assertIn(f'/usr/local/bin/t3-capacity\tlink\t{CAPACITY_LINK}\n', f.read())
        self.assertEqual(self.run_main('self-check')[0], 0)
        self.assertEqual(self.install(sha=SHA2)[0], 0)               # an upgrade: the same link, the new release
        self.assertEqual(self.resolve('/usr/local/bin/t3-capacity'),
                         self.path(f'/usr/local/libexec/heavy/versions/{SHA2}/bin/t3-capacity'))
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy/replaced-capacity')))
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertFalse(os.path.lexists(link))

    def test_old_plain_t3_capacity_is_left_alone_without_the_option(self):
        self.write_old('/usr/local/bin/t3-capacity', OLD_CAPACITY)
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertIn('note: /usr/local/bin/t3-capacity is a plain file this kit did not write: left as it is', out)
        self.assertIn('--replace-t3-capacity', out)
        self.run_old_capacity()
        self.assertTrue(os.path.islink(self.path('/usr/local/bin/heavy')))
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy/replaced-capacity')))
        with open(self.path('/etc/heavy/installed.tsv')) as f:
            self.assertNotIn('/usr/local/bin/t3-capacity', f.read())
        self.assertEqual(self.run_main('self-check')[0], 0)
        before = self.snapshot()
        self.assertEqual(self.install()[0], 0)                       # again: still left alone, nothing changes
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.run_old_capacity()

    def test_old_plain_t3_capacity_is_kept_linked_and_put_back_by_uninstall(self):
        self.write_old('/usr/local/bin/heavy', OLD_WRAPPER)
        self.write_old('/usr/local/bin/t3-capacity', OLD_CAPACITY)
        code, out, err = self.install('--replace-t3-capacity')
        self.assertEqual(code, 0, err)
        self.assertIn('the old /usr/local/bin/t3-capacity (sha256 ', out)
        self.assertIn('is kept as /usr/local/libexec/heavy/replaced-capacity/t3-capacity.', out)
        self.assertEqual(os.readlink(self.path('/usr/local/bin/t3-capacity')), CAPACITY_LINK)
        kept = self.path('/usr/local/libexec/heavy/replaced-capacity')
        self.assertEqual(len(os.listdir(kept)), 1)
        with open(os.path.join(kept, os.listdir(kept)[0]), 'rb') as f:
            self.assertEqual(f.read(), OLD_CAPACITY)
        # heavy's own backup folder holds only heavy (an older installer's uninstall restores its newest file)
        self.assertEqual([n[:6] for n in os.listdir(self.path('/usr/local/libexec/heavy/replaced'))], ['heavy.'])
        self.assertEqual(self.run_main('self-check')[0], 0)
        before = self.snapshot()
        code, out, err = self.install()                               # again, the usual line: nothing changes
        self.assertEqual(code, 0, err)
        self.assertNotIn('is kept as', out)
        self.assertEqual(self.snapshot(), before)
        code, out, _ = self.run_main('uninstall')
        self.assertEqual(code, 0)
        self.run_old_capacity()
        self.assertEqual(os.stat(self.path('/usr/local/bin/t3-capacity')).st_mode & 0o777, 0o755)
        with open(self.path('/usr/local/bin/heavy'), 'rb') as f:
            self.assertEqual(f.read(), OLD_WRAPPER)
        self.assertEqual(self.run_main('uninstall')[0], 0)            # again: nothing left, still fine

    def test_t3_capacity_swap_fails_old_command_still_works_then_run_again(self):
        self.write_old('/usr/local/bin/t3-capacity', OLD_CAPACITY)
        real_switch = self.m.switch_link

        def broken(path, target):
            if path == self.m.CAPACITY:
                raise OSError('the swap did not happen (test)')
            return real_switch(path, target)

        self.m.switch_link = broken
        with self.assertRaises(OSError):
            self.install('--replace-t3-capacity')
        self.m.switch_link = real_switch
        self.run_old_capacity()                                       # the old copy until the swap, whole
        self.assertTrue(os.path.islink(self.path('/usr/local/bin/heavy')))
        code, out, err = self.install('--replace-t3-capacity')
        self.assertEqual(code, 0, err)
        self.assertNotIn('is kept as', out)                           # kept already by the run that stopped
        self.assertEqual(os.readlink(self.path('/usr/local/bin/t3-capacity')), CAPACITY_LINK)
        self.assertEqual(len(os.listdir(self.path('/usr/local/libexec/heavy/replaced-capacity'))), 1)
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_foreign_t3_capacity_link_or_folder_is_refused_before_anything_is_written(self):
        for make in (lambda p: os.symlink('/opt/other/t3-capacity', p), os.mkdir):
            with self.subTest(make=make):
                shutil.rmtree(self.root)
                os.makedirs(self.path('/usr/local/bin'))
                make(self.path('/usr/local/bin/t3-capacity'))
                code, _, err = self.install()
                self.assertEqual(code, 1)
                self.assertIn('/usr/local/bin/t3-capacity exists and is not the kit\'s link', err)
                self.assertEqual(sorted(os.listdir(self.path('/usr/local/bin'))), ['t3-capacity'])
                self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy')))
                self.assertFalse(os.path.exists(self.path('/etc/heavy')))

    def test_heavy_self_check_warns_when_t3_capacity_on_path_is_not_the_kits(self):
        self.assertEqual(self.install()[0], 0)
        version = self.path(f'/usr/local/libexec/heavy/versions/{SHA1}')
        ours, other, empty = (os.path.join(self.root, n) for n in ('ours', 'other', 'empty'))
        for d in (ours, other, empty):
            os.mkdir(d)
        os.symlink(os.path.join(version, 'bin', 't3-capacity'), os.path.join(ours, 't3-capacity'))
        with open(os.path.join(other, 't3-capacity'), 'wb') as f:
            f.write(OLD_CAPACITY)
        os.chmod(os.path.join(other, 't3-capacity'), 0o755)
        config = os.path.join(self.root, 'config.json')
        with open(config, 'w') as f:
            json.dump({'version': 1, 'slice': 'heavy.slice', 'grace_seconds': 30}, f)

        def self_check(path):
            env = dict(os.environ, PATH=path, HEAVY_CONFIG=config, HEAVY_SYSTEMCTL='/bin/false')
            r = subprocess.run([sys.executable, '-I', '-B', os.path.join(version, 'bin', 'heavy'), '--self-check'],
                               capture_output=True, text=True, timeout=60, env=env)
            return r.stdout

        out = self_check(f'{ours}:{other}')
        self.assertIn("t3-capacity on PATH: this kit's\n", out)
        self.assertNotIn('WARNING', out)
        out = self_check(f'{other}:{ours}')
        self.assertIn(f'WARNING: t3-capacity on PATH is {other}/t3-capacity, not this kit\'s ', out)
        self.assertIn('its readings can differ', out)
        out = self_check(empty)
        self.assertIn('WARNING: t3-capacity is not on PATH', out)

    def test_foreign_link_or_folder_is_refused_before_anything_is_written(self):
        os.symlink('/somewhere/else', self.path('/usr/local/bin/heavy'))
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn('is not the kit\'s link', err)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy')))
        self.assertFalse(os.path.exists(self.path('/etc/heavy')))

    def test_every_stop_half_way_then_run_again(self):
        for step in self.m.STEPS:
            with self.subTest(step=step):
                shutil.rmtree(self.root)
                os.makedirs(self.path('/usr/local/bin'))
                self.write_old('/usr/local/bin/heavy', OLD_WRAPPER)
                self.write_old('/usr/local/bin/t3-capacity', OLD_CAPACITY)
                code, _, _ = self.install('--memory-high', '5G', '--sweep-timer', '--replace-t3-capacity',
                                          stop_after=step)
                self.assertEqual(code, f'stopped after {step} (test)')
                # until the links step, the old wrapper and the old t3-capacity are what runs
                if self.m.STEPS.index(step) < self.m.STEPS.index('links'):
                    self.assertFalse(os.path.islink(self.path('/usr/local/bin/heavy')))
                    self.run_old_capacity()
                # a timer only once the release it runs is the one in use (#183 nit d)
                if os.path.exists(self.path('/etc/systemd/user/heavy-sweep.service')):
                    current = os.readlink(self.path('/usr/local/libexec/heavy/current'))     # absolute, as installed
                    self.assertTrue(os.path.isfile(self.path(current + '/lib/heavy_sweep.py')))
                    self.assertTrue(os.path.islink(self.path('/usr/local/bin/heavy')))
                code, _, err = self.install('--memory-high', '5G', '--sweep-timer', '--replace-t3-capacity')
                self.assertEqual(code, 0, err)
                self.assertTrue(os.path.isfile(self.path('/etc/systemd/user/heavy-sweep.timer')))
                self.assertEqual(self.run_main('self-check')[0], 0)
                self.assertEqual(len(os.listdir(self.path('/usr/local/libexec/heavy/replaced'))), 1)
                self.assertEqual(len(os.listdir(self.path('/usr/local/libexec/heavy/replaced-capacity'))), 1)
                self.assertEqual(os.readlink(self.path('/usr/local/bin/t3-capacity')), CAPACITY_LINK)

    def test_half_copied_version_is_made_again(self):
        self.assertEqual(self.install()[0], 0)
        bad = self.path(f'/usr/local/libexec/heavy/versions/{SHA1}/bin/heavy')
        with open(bad, 'a') as f:
            f.write('# changed\n')
        code, out, _ = self.run_main('self-check')
        self.assertEqual(code, 1)
        self.assertIn('changed since install', out)
        os.makedirs(self.path('/usr/local/libexec/heavy/versions/.tmp-leftover'))
        code, out, _ = self.install()
        self.assertEqual(code, 0)
        self.assertIn('made again', out)
        self.assertEqual(self.run_main('self-check')[0], 0)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy/versions/.tmp-leftover')))

    def test_broken_new_copy_leaves_the_version_in_use(self):
        self.assertEqual(self.install()[0], 0)
        src = os.path.join(self.root, 'src')
        shutil.copytree(KIT, src, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(os.path.join(os.path.dirname(KIT), 'common'), os.path.join(self.root, 'common'),
                        ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(CAPACITY, os.path.join(src, 'capacity'))
        with open(os.path.join(src, 'bin', 'heavy'), 'w') as f:
            f.write('this is not python\n')
        code, _, err = self.install(sha=SHA2, source=src)
        self.assertEqual(code, 1)
        self.assertIn('failed its self-test', err)
        self.assertEqual(os.readlink(self.path('/usr/local/libexec/heavy/current')),
                         f'/usr/local/libexec/heavy/versions/{SHA1}')

    def test_upgrade_keeps_config_and_the_old_version(self):
        self.assertEqual(self.install('--slice', 'builds.slice', '--grace', '2.5')[0], 0)
        self.assertEqual(self.install(sha=SHA2)[0], 0)
        self.assertEqual(self.config()['slice'], 'builds.slice')
        self.assertEqual(self.config()['grace_seconds'], 2.5)
        self.assertEqual(os.readlink(self.path('/usr/local/libexec/heavy/current')),
                         f'/usr/local/libexec/heavy/versions/{SHA2}')
        self.assertTrue(os.path.isdir(self.path(f'/usr/local/libexec/heavy/versions/{SHA1}')))

    def test_limits_write_the_slice_unit_and_uninstall_removes_it(self):
        code, out, err = self.install('--slice', 'builds.slice', '--memory-high', '5G', '--memory-max', '6G',
                                      '--swap-max', '1G', '--cpu-weight', '40')
        self.assertEqual(code, 0, err)
        unit = self.path('/etc/systemd/user/builds.slice')
        with open(unit) as f:
            text = f.read()
        for line in ('[Slice]', 'MemoryHigh=5G', 'MemoryMax=6G', 'MemorySwapMax=1G', 'CPUWeight=40'):
            self.assertIn(line + '\n', text)
        self.assertIn('daemon-reload', out)
        self.assertEqual(self.run_main('self-check')[0], 0)
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertFalse(os.path.exists(unit))

    def test_a_slice_unit_someone_else_wrote_is_not_overwritten(self):
        os.makedirs(self.path('/etc/systemd/user'))
        with open(self.path('/etc/systemd/user/heavy.slice'), 'w') as f:
            f.write('[Slice]\nMemoryMax=1G\n')
        code, _, err = self.install('--memory-max', '6G')
        self.assertEqual(code, 1)
        self.assertIn('was not written by this kit', err)
        with open(self.path('/etc/systemd/user/heavy.slice')) as f:
            self.assertEqual(f.read(), '[Slice]\nMemoryMax=1G\n')
        self.assertEqual(self.install()[0], 0)                      # without limits: used as it is

    def test_bad_options_are_refused(self):
        for extra in (['--slice', 'heavy'], ['--slice', '../x.slice'], ['--grace', '0'], ['--grace', 'soon'],
                      ['--memory-max', '6 GB'], ['--cpu-weight', '0'], ['--swap-max', '1G']):
            with self.subTest(extra=extra):
                code, _, err = self.install(*extra)
                self.assertEqual(code, 1, err)
        self.assertEqual(self.install(sha='abc')[0], 1)
        self.assertFalse(os.path.exists(self.path('/etc/heavy')))

    def test_installed_version_runs_the_sweep_from_its_own_copy_of_common(self):
        self.assertEqual(self.install()[0], 0)
        version = self.path(f'/usr/local/libexec/heavy/versions/{SHA1}')
        self.assertTrue(os.path.isfile(os.path.join(version, 'common', 'machine_file.py')))
        self.assertTrue(os.path.isfile(os.path.join(version, 'lib', 'heavy_sweep.py')))
        moved = os.path.join(self.root, 'elsewhere')   # nothing of the repository next to it
        shutil.copytree(version, moved)
        # docker-label with a process owner: a scope owner (this test run under heavy) would leave a note for the
        # heavy running it in /run/user/<uid>/heavy/labelled, which an installed heavy from before the notes keeps
        with open('/proc/self/cgroup') as f:
            leaf = ''.join(line[3:].strip() for line in f if line.startswith('0::')).rpartition('/')[2]
        home = os.path.join(self.root, 'home')          # never the tester's real home (#761 item 9)
        os.mkdir(home, 0o700)
        notes = [os.path.join(base, 'heavy', 'labelled', leaf) for base in
                 (f'/run/user/{os.getuid()}', os.path.join(home, '.local', 'state'))] if leaf.endswith('.scope') else []
        before = [n for n in notes if os.path.lexists(n)]
        for args in (['sweep', '--help'], ['facts', '--help'], ['docker-label', '--value', '--owner-pid', str(os.getpid())]):
            r = subprocess.run([sys.executable, '-I', '-B', os.path.join(moved, 'bin', 'heavy'), *args],
                               capture_output=True, text=True, timeout=60, env=dict(os.environ, HOME=home))
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, rf'^uid=\d+,pid={os.getpid()},start=\d+\n$')
        self.assertEqual([n for n in notes if os.path.lexists(n)], before, 'the test left a note behind')

    def test_sweep_timer_units_written_kept_and_removed(self):
        units = [self.path('/etc/systemd/user/heavy-sweep.service'), self.path('/etc/systemd/user/heavy-sweep.timer')]
        code, out, err = self.install('--sweep-timer')
        self.assertEqual(code, 0, err)
        self.assertIn('systemctl --user enable --now heavy-sweep.timer', out)
        with open(units[0]) as f:
            text = f.read()
            self.assertIn('ExecStart=/usr/bin/python3 -I -B /usr/local/libexec/heavy/current/lib/heavy_sweep.py '
                          'sweep\n', text)
            self.assertIn('ConditionPathExists=/usr/local/libexec/heavy/current/lib/heavy_sweep.py\n', text)
        with open(units[1]) as f:
            self.assertIn('WantedBy=timers.target\n', f.read())
        self.assertTrue(self.config()['sweep_timer'])
        self.assertEqual(self.run_main('self-check')[0], 0)
        before = self.snapshot()
        self.assertEqual(self.install(sha=SHA1)[0], 0)              # again, without the option: kept
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.install(sha=SHA2)[0], 0)              # upgrade keeps it
        self.assertTrue(all(os.path.isfile(u) for u in units))
        self.assertEqual(self.install('--no-sweep-timer', sha=SHA2)[0], 0)
        self.assertFalse(any(os.path.exists(u) for u in units))
        self.assertFalse(self.config().get('sweep_timer'))
        self.assertEqual(self.run_main('self-check')[0], 0)
        self.assertEqual(self.install(sha=SHA2)[0], 0)              # again, without the option: still off
        self.assertFalse(any(os.path.exists(u) for u in units))
        self.assertEqual(self.install('--sweep-timer', sha=SHA2)[0], 0)
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertFalse(any(os.path.exists(u) for u in units))

    def old_installer_config(self):
        """What an installer from before the sweep writes on a rollback: config.json without the sweep_timer key
        (it keeps the units it does not know)."""
        cfg = {k: v for k, v in self.config().items() if k != 'sweep_timer'}
        with open(self.path('/etc/heavy/config.json'), 'w') as f:
            json.dump(cfg, f)

    def test_roll_forward_after_a_rollback_keeps_the_sweep_timer(self):
        # #193: rollback with the old installer, then the usual install line (without --sweep-timer)
        units = [self.path('/etc/systemd/user/heavy-sweep.service'), self.path('/etc/systemd/user/heavy-sweep.timer')]
        self.assertEqual(self.install('--sweep-timer')[0], 0)
        self.old_installer_config()
        code, out, err = self.install(sha=SHA2)
        self.assertEqual(code, 0, err)
        self.assertTrue(all(os.path.isfile(u) for u in units), out)
        self.assertTrue(self.config()['sweep_timer'])
        self.assertIn('kept the sweep timer units', out)
        self.assertEqual(self.run_main('self-check')[0], 0)
        # an older config and only one unit left (a stop half-way): kept, and made whole again
        self.old_installer_config()
        os.unlink(units[1])
        self.assertEqual(self.install(sha=SHA2)[0], 0)
        self.assertTrue(all(os.path.isfile(u) for u in units))
        # --no-sweep-timer stopped after its config step, then the usual line again: still off
        code, _, _ = self.install('--no-sweep-timer', sha=SHA2, stop_after='config')
        self.assertEqual(code, 'stopped after config (test)')
        self.assertTrue(all(os.path.isfile(u) for u in units))
        self.assertEqual(self.install(sha=SHA2)[0], 0)
        self.assertFalse(any(os.path.exists(u) for u in units))
        # an older config and no units: nothing written
        self.old_installer_config()
        self.assertEqual(self.install(sha=SHA2)[0], 0)
        self.assertFalse(any(os.path.exists(u) for u in units))
        self.assertNotIn('sweep_timer', self.config())
        # an older config, one unit ours and one someone else's: refused, and the refusal names the way out
        self.assertEqual(self.install('--sweep-timer', sha=SHA2)[0], 0)
        self.old_installer_config()
        with open(units[1], 'w') as f:
            f.write('[Timer]\nOnCalendar=daily\n')
        code, _, err = self.install(sha=SHA2)
        self.assertEqual(code, 1)
        self.assertIn('heavy-sweep.timer exists and was not written by this kit', err)
        self.assertIn('--no-sweep-timer', err)
        self.assertEqual(self.install('--no-sweep-timer', sha=SHA2)[0], 0)
        self.assertFalse(os.path.exists(units[0]))
        with open(units[1]) as f:
            self.assertEqual(f.read(), '[Timer]\nOnCalendar=daily\n')

    def test_sweep_units_someone_else_wrote_are_refused_and_kept(self):
        os.makedirs(self.path('/etc/systemd/user'))
        with open(self.path('/etc/systemd/user/heavy-sweep.timer'), 'w') as f:
            f.write('[Timer]\nOnCalendar=daily\n')
        code, _, err = self.install('--sweep-timer')
        self.assertEqual(code, 1)
        self.assertIn('was not written by this kit', err)
        self.assertFalse(os.path.exists(self.path('/etc/heavy')))
        self.assertEqual(self.install()[0], 0)                      # without the option it is left alone
        self.assertEqual(self.run_main('uninstall')[0], 0)
        with open(self.path('/etc/systemd/user/heavy-sweep.timer')) as f:
            self.assertEqual(f.read(), '[Timer]\nOnCalendar=daily\n')

    def test_not_root_is_refused(self):
        self.m.ROOT_UID = os.geteuid() + 1
        self.assertEqual(self.install()[0], 1)
        self.assertEqual(self.run_main('uninstall')[0], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
