"""heavy-install on a fake host in a temporary folder (every fixed path under PREFIX, the current user as root).
No root, no systemd, no network.

  python3 -I -B fork-kit/heavy/tests/install.test.py
"""
# CI's checkout is shallow without tags, so the test that runs an older release's installer skips there (it runs locally):
# fork-ci: may skip Install.test_older_installers_install_and_uninstall_keep_the_hosts_t3_capacity
# fork-ci: may skip Install.test_a_rollback_with_a_real_older_installer_rolls_t3_capacity_back_too
import contextlib, importlib.machinery, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

OLDER_RELEASE = 'ci/v22'   # a released installer from before the t3-capacity link (its uninstall is run below)

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPACITY = os.path.join(os.path.dirname(os.path.dirname(KIT)), 'packages', 'shared', 'src', 'capacity')
SHA1, SHA2 = 'a' * 40, 'b' * 40
OLD_WRAPPER = b'#!/bin/sh\nexec systemd-run --user --scope --quiet --slice=heavy.slice --collect -- "$@"\n'
OLD_CAPACITY = b'#!/bin/sh\necho "available=9999MB swapfree=9999MB load=0/4cores"; echo OK\n'   # a host's own copy
CAPACITY_LINK = '/usr/local/libexec/heavy/current/bin/t3-capacity'   # the form every installer from ci/v23 on knows


def load(path=os.path.join(KIT, 'sbin', 'heavy-install')):
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

    def run_main(self, *argv, stop_after=None, m=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = (m or self.m).main(list(argv), **({} if m else {'stop_after': stop_after}))
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
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy-kept')))
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertFalse(os.path.lexists(link))

    def test_old_plain_t3_capacity_is_left_alone_without_the_option(self):
        self.write_old('/usr/local/bin/t3-capacity', OLD_CAPACITY)
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertIn('note: /usr/local/bin/t3-capacity is a plain file this kit did not write: left as it is', out)
        self.assertIn('--replace-t3-capacity', out)
        closing = [line for line in out.splitlines() if line.startswith('heavy-install: installed release ')]
        self.assertEqual(len(closing), 1, out)
        self.assertIn("WARNING: /usr/local/bin/t3-capacity is still the host's own, not the kit's "
                      '(--replace-t3-capacity switches it)', closing[0])
        self.run_old_capacity()
        self.assertTrue(os.path.islink(self.path('/usr/local/bin/heavy')))
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy-kept')))
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
        self.assertIn('is kept as /usr/local/libexec/heavy-kept/t3-capacity.', out)
        self.assertNotIn('WARNING', out)                              # the closing line: the kit's now
        self.assertEqual(os.readlink(self.path('/usr/local/bin/t3-capacity')), CAPACITY_LINK)
        kept = self.path('/usr/local/libexec/heavy-kept')
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
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy-kept')))   # its copy is back
        self.assertEqual(self.run_main('uninstall')[0], 0)            # again: nothing left, still fine

    def test_kept_t3_capacity_is_outside_every_folder_an_uninstall_deletes(self):
        # every released uninstall (ci/v6 to ci/v22) deletes LIBEXEC and ETC whole, and unlinks only heavy and
        # heavy-install; it does not know the t3-capacity link or the kept copy
        for gone in (self.m.LIBEXEC, self.m.ETC):
            self.assertFalse((self.m.KEPT_CAPACITY + '/').startswith(gone + '/'), gone)
        self.write_old('/usr/local/bin/t3-capacity', OLD_CAPACITY)
        self.assertEqual(self.install('--replace-t3-capacity')[0], 0)
        for path in ('/usr/local/bin/heavy', '/usr/local/sbin/heavy-install'):
            os.unlink(self.path(path))
        for path in (self.m.LIBEXEC, self.m.ETC):
            shutil.rmtree(self.path(path))
        self.assert_older_uninstall_left_the_kept_copy_then_restore()

    def assert_older_uninstall_left_the_kept_copy_then_restore(self):
        link = self.path('/usr/local/bin/t3-capacity')
        self.assertEqual(os.readlink(link), CAPACITY_LINK)            # broken: an older uninstall does not know it
        self.assertFalse(os.path.lexists(self.path(CAPACITY_LINK)))   # (its target, inside the fake root)
        kept = self.path('/usr/local/libexec/heavy-kept')
        self.assertEqual(len(os.listdir(kept)), 1)
        with open(os.path.join(kept, os.listdir(kept)[0]), 'rb') as f:
            self.assertEqual(f.read(), OLD_CAPACITY)
        code, out, err = self.run_main('uninstall')                   # this release's uninstall puts it back
        self.assertEqual(code, 0, err)
        self.assertIn('put the old /usr/local/bin/t3-capacity back', out)
        self.run_old_capacity()
        self.assertFalse(os.path.exists(kept))
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.run_old_capacity()

    def test_older_installers_install_and_uninstall_keep_the_hosts_t3_capacity(self):
        m_old, source = self.older_installer(OLDER_RELEASE)
        if m_old is None:
            self.skipTest(f'{OLDER_RELEASE} is not in this checkout (a shallow clone without tags); '
                          f'test_kept_t3_capacity_is_outside_every_folder_an_uninstall_deletes covers its deletes')
        self.write_old('/usr/local/bin/t3-capacity', OLD_CAPACITY)
        self.assertEqual(self.install('--replace-t3-capacity')[0], 0)
        # the rollback: the older installer installs its own release over this state, then uninstalls
        code, _, err = self.run_main('install', '--source', source, '--sha', SHA2, m=m_old)
        self.assertEqual(code, 0, err)
        # the link names `current`: the older release's own t3-capacity runs, as its heavy does
        self.assertEqual(self.resolve('/usr/local/bin/t3-capacity'),
                         self.path(f'/usr/local/libexec/heavy/versions/{SHA2}/bin/t3-capacity'))
        code, _, err = self.run_main('uninstall', m=m_old)
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy')))
        self.assertFalse(os.path.exists(self.path('/etc/heavy')))
        self.assert_older_uninstall_left_the_kept_copy_then_restore()

    def older_installer(self, release):
        """The heavy-install of RELEASE (a ci/vN tag), loaded on the fake root, or None without the tag (CI)."""
        repo = os.path.dirname(os.path.dirname(KIT))
        r = subprocess.run(['git', '-C', repo, 'rev-parse', '--verify', '-q', release + '^{commit}'],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None, None
        older = os.path.join(self.root, 'older-' + release.replace('/', '-'))
        os.mkdir(older)
        paths = ['fork-kit']   # releases before heavy facts (ci/v20 and older) have no capacity rule folder
        if subprocess.run(['git', '-C', repo, 'cat-file', '-e', release + ':packages/shared/src/capacity'],
                          capture_output=True, timeout=60).returncode == 0:
            paths.append('packages/shared/src/capacity')
        tree = subprocess.run(['git', '-C', repo, 'archive', release, *paths],
                              capture_output=True, timeout=120, check=True).stdout
        subprocess.run(['tar', '-x', '-C', older], input=tree, check=True, timeout=120)
        m_old = load(os.path.join(older, 'fork-kit', 'heavy', 'sbin', 'heavy-install'))
        m_old.PREFIX, m_old.ROOT_UID = self.root, os.geteuid()
        return m_old, os.path.join(older, 'fork-kit', 'heavy')

    def run_kit_capacity(self):
        """/usr/local/bin/t3-capacity resolves to a kit copy that is there and starts."""
        real = self.resolve('/usr/local/bin/t3-capacity')
        self.assertTrue(os.path.isfile(real), real)
        r = subprocess.run([sys.executable, '-I', '-B', real, '--help'], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('t3-capacity [--kind KIND] [MB]', r.stdout)

    def test_a_rollback_to_a_release_without_t3_capacity_is_mended_by_either_line(self):
        # ci/v20 or older switches `current` to a release without bin/t3-capacity. The README says to run this
        # release's uninstall first; without it the link points at a missing file until one of the two lines below.
        # Here its switch is made by hand (CI has no tags; the real ci/v20 runs in the next test).
        for mend in ('install', 'uninstall'):
            with self.subTest(mend=mend):
                shutil.rmtree(self.root)
                os.makedirs(self.path('/usr/local/bin'))
                self.write_old('/usr/local/bin/t3-capacity', OLD_CAPACITY)
                self.assertEqual(self.install('--replace-t3-capacity')[0], 0)
                old_release = self.path(f'/usr/local/libexec/heavy/versions/{SHA2}')
                os.makedirs(old_release + '/bin')
                shutil.copy2(os.path.join(KIT, 'bin', 'heavy'), old_release + '/bin/heavy')
                os.unlink(self.path('/usr/local/libexec/heavy/current'))
                os.symlink(f'/usr/local/libexec/heavy/versions/{SHA2}', self.path('/usr/local/libexec/heavy/current'))
                self.assertFalse(os.path.exists(self.resolve('/usr/local/bin/t3-capacity')))   # the documented gap
                code, out, err = self.install() if mend == 'install' else self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                if mend == 'install':                                 # rolling forward again: the usual line
                    self.assertEqual(os.readlink(self.path('/usr/local/bin/t3-capacity')), CAPACITY_LINK)
                    self.run_kit_capacity()
                    self.assertEqual(self.run_main('self-check')[0], 0)
                else:                                                 # the broken kit link: the host's copy back
                    self.run_old_capacity()
                    self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy-kept')))

    def assert_release_in_use(self, sha):
        """heavy and t3-capacity on PATH are both release SHA's, and t3-capacity starts."""
        for cmd in ('heavy', 't3-capacity'):
            self.assertEqual(self.resolve(f'/usr/local/bin/{cmd}'),
                             self.path(f'/usr/local/libexec/heavy/versions/{sha}/bin/{cmd}'), cmd)
        self.run_kit_capacity()

    def test_a_rollback_with_a_real_older_installer_rolls_t3_capacity_back_too(self):
        # #1095: ci/v23 is the first release that links t3-capacity, ci/v33 the last before #933. Each must see this
        # release's link as its own: switch it with heavy, print no note or WARNING, take --replace-t3-capacity.
        for release in ('ci/v23', 'ci/v33', 'ci/v20'):
            with self.subTest(release=release):
                shutil.rmtree(self.root)
                os.makedirs(self.path('/usr/local/bin'))
                m_old, source = self.older_installer(release)
                if m_old is None:
                    self.skipTest(f'{release} is not in this checkout (a shallow clone without tags); '
                                  'test_a_rollback_to_a_release_without_t3_capacity_is_mended_by_either_line '
                                  'covers ci/v20 by hand')
                self.write_old('/usr/local/bin/t3-capacity', OLD_CAPACITY)
                self.assertEqual(self.install('--replace-t3-capacity')[0], 0)
                if release == 'ci/v20':
                    # no bin/t3-capacity there: the README's order is this release's uninstall first, which puts
                    # the host's copy back; ci/v20 does not know t3-capacity and leaves it running
                    self.assertEqual(self.run_main('uninstall')[0], 0)
                    code, out, err = self.run_main('install', '--source', source, '--sha', SHA2, m=m_old)
                    self.assertEqual(code, 0, err)
                    self.run_old_capacity()
                    self.assertEqual(self.install('--replace-t3-capacity')[0], 0)   # roll forward again
                    self.assert_release_in_use(SHA1)
                else:
                    code, out, err = self.run_main('install', '--source', source, '--sha', SHA2, m=m_old)
                    self.assertEqual(code, 0, err)
                    self.assertNotIn('note:', out)
                    self.assertNotIn('WARNING', out)
                    self.assert_release_in_use(SHA2)
                    code, out, err = self.run_main('install', '--source', source, '--sha', SHA2,
                                                   '--replace-t3-capacity', m=m_old)
                    self.assertEqual(code, 0, err)                    # runs again with the option: not refused
                    self.assertEqual(self.run_main('self-check', m=m_old)[0], 0)
                    code, out, err = self.install()                   # roll forward again
                    self.assertEqual(code, 0, err)
                    self.assertNotIn('note:', out)
                    self.assert_release_in_use(SHA1)
                    self.assertEqual(self.run_main('self-check')[0], 0)
                code, out, err = self.run_main('uninstall')           # this release's uninstall: the old copy back
                self.assertEqual(code, 0, err)
                self.run_old_capacity()
                self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy-kept')))

    def test_uninstall_never_leaves_the_command_missing(self):
        # #933 1: the kept copy replaces the kit's link in one rename; there is no step without a t3-capacity
        self.write_old('/usr/local/bin/heavy', OLD_WRAPPER)
        self.write_old('/usr/local/bin/t3-capacity', OLD_CAPACITY)
        self.assertEqual(self.install('--replace-t3-capacity')[0], 0)
        missing = []
        real_unlink, real_replace = os.unlink, os.replace

        def watch(fn):
            def call(*args, **kw):
                out = fn(*args, **kw)
                for path in ('/usr/local/bin/heavy', '/usr/local/bin/t3-capacity'):
                    if not os.path.lexists(self.path(path)):
                        missing.append(path)
                return out
            return call

        with mock.patch.object(os, 'unlink', watch(real_unlink)), mock.patch.object(os, 'replace', watch(real_replace)):
            code, out, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        self.assertEqual(missing, [])
        self.run_old_capacity()

    def test_uninstall_stopped_before_its_rename_then_run_again(self):
        # #933 1 and 2: a stop between the copy and its rename leaves the kit's link, which still runs
        self.write_old('/usr/local/bin/t3-capacity', OLD_CAPACITY)
        self.assertEqual(self.install('--replace-t3-capacity')[0], 0)
        real_replace = os.replace

        def stop(src, dst, *a, **kw):
            if dst == self.path('/usr/local/bin/t3-capacity'):
                raise OSError('stopped before the rename (test)')
            return real_replace(src, dst, *a, **kw)

        with mock.patch.object(os, 'replace', stop), self.assertRaises(OSError):
            self.run_main('uninstall')
        self.assertEqual(os.readlink(self.path('/usr/local/bin/t3-capacity')), CAPACITY_LINK)
        self.run_kit_capacity()
        code, out, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        self.run_old_capacity()
        self.assertEqual(sorted(os.listdir(self.path('/usr/local/bin'))), ['t3-capacity'])   # no .restore
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy-kept')))
        self.assertEqual(self.run_main('uninstall')[0], 0)            # again, on an empty prefix: fine
        self.run_old_capacity()

    def test_a_keep_stopped_before_its_rename_leaves_no_kept_folder_after_uninstall(self):
        # #933 2: the copy into heavy-kept/ stopped before its rename; then the owner uninstalls
        self.write_old('/usr/local/bin/t3-capacity', OLD_CAPACITY)
        real_replace = os.replace

        def stop(src, dst, *a, **kw):
            if src.endswith('.tmp') and '/heavy-kept/' in src:
                raise OSError('stopped before the rename (test)')
            return real_replace(src, dst, *a, **kw)

        with mock.patch.object(os, 'replace', stop), self.assertRaises(OSError):
            self.install('--replace-t3-capacity')
        self.assertEqual([n.endswith('.tmp') for n in os.listdir(self.path('/usr/local/libexec/heavy-kept'))],
                         [True])
        self.run_old_capacity()
        code, out, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        self.run_old_capacity()
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy-kept')))

    def test_a_link_swap_stopped_half_way_leaves_no_temporary_link(self):
        # #933 2: a stop between the new link and its rename leaves `<path>.new-link`; then the line runs again, or
        # the owner uninstalls
        real_replace = os.replace

        def stop(src, dst, *a, **kw):
            if src.endswith('/t3-capacity.new-link'):
                raise OSError('stopped before the rename (test)')
            return real_replace(src, dst, *a, **kw)

        for then in ('install', 'uninstall'):
            with self.subTest(then=then):
                shutil.rmtree(self.root)
                os.makedirs(self.path('/usr/local/bin'))
                with mock.patch.object(os, 'replace', stop), self.assertRaises(OSError):
                    self.install()
                self.assertTrue(os.path.islink(self.path('/usr/local/bin/t3-capacity.new-link')))
                code, out, err = self.install() if then == 'install' else self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                left = [n for n in os.listdir(self.path('/usr/local/bin')) if '.' in n]
                self.assertEqual(left, [])
        # one left beside a link that is right already (another run made the link after this one stopped)
        self.assertEqual(self.install()[0], 0)
        os.symlink('/nowhere', self.path('/usr/local/bin/t3-capacity.new-link'))
        self.assertEqual(self.install()[0], 0)
        self.assertFalse(os.path.lexists(self.path('/usr/local/bin/t3-capacity.new-link')))

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
        self.assertEqual(len(os.listdir(self.path('/usr/local/libexec/heavy-kept'))), 1)
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_foreign_t3_capacity_link_is_left_alone_without_the_option(self):
        link = self.path('/usr/local/bin/t3-capacity')
        os.symlink('/opt/other/t3-capacity', link)
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertIn('note: /usr/local/bin/t3-capacity is a link this kit did not make (to /opt/other/t3-capacity): '
                      'left as it is', out)
        self.assertIn("WARNING: /usr/local/bin/t3-capacity is still the host's own", out)
        self.assertEqual(os.readlink(link), '/opt/other/t3-capacity')
        self.assertTrue(os.path.islink(self.path('/usr/local/bin/heavy')))
        with open(self.path('/etc/heavy/installed.tsv')) as f:
            self.assertNotIn('/usr/local/bin/t3-capacity', f.read())
        self.assertEqual(self.run_main('self-check')[0], 0)
        before = self.snapshot()
        self.assertEqual(self.install()[0], 0)                       # again: still left alone, nothing changes
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertEqual(os.readlink(link), '/opt/other/t3-capacity')
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy-kept')))

    def test_foreign_t3_capacity_link_or_folder_is_refused_before_anything_is_written(self):
        # with the option a link elsewhere is refused (nothing to keep); a folder is refused with or without it
        for make, extra in ((lambda p: os.symlink('/opt/other/t3-capacity', p), ('--replace-t3-capacity',)),
                            (os.mkdir, ()), (os.mkdir, ('--replace-t3-capacity',))):
            with self.subTest(make=make, extra=extra):
                shutil.rmtree(self.root)
                os.makedirs(self.path('/usr/local/bin'))
                make(self.path('/usr/local/bin/t3-capacity'))
                code, _, err = self.install(*extra)
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
                self.assertEqual(len(os.listdir(self.path('/usr/local/libexec/heavy-kept'))), 1)
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

    def test_self_test_starts_each_installed_command(self):
        self.assertEqual(self.install()[0], 0)
        src = os.path.join(self.root, 'src')
        shutil.copytree(KIT, src, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(os.path.join(os.path.dirname(KIT), 'common'), os.path.join(self.root, 'common'),
                        ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(CAPACITY, os.path.join(src, 'capacity'))
        for tool, sha in (('t3-capacity', 'c' * 40), ('agent-slots', 'd' * 40)):   # a failed copy stays, per sha
            with self.subTest(tool=tool):
                path = os.path.join(src, 'bin', tool)
                with open(path) as f:
                    good = f.read()
                with open(path, 'w') as f:
                    f.write(good.replace('import ', 'import no_such_module, ', 1))
                code, _, err = self.install(sha=sha, source=src)
                with open(path, 'w') as f:
                    f.write(good)
                self.assertEqual(code, 1)
                self.assertIn(f'bin/{tool} --help failed', err)
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

    def test_a_source_without_a_file_is_refused_before_anything_is_written(self):
        self.assertEqual(self.m.missing_sources(KIT), [])
        top = tempfile.mkdtemp(prefix='heavy-source-')   # the kit alone: no fork-kit/common, no capacity rule
        self.addCleanup(shutil.rmtree, top, ignore_errors=True)
        src = os.path.join(top, 'fork-kit', 'heavy')
        shutil.copytree(KIT, src)
        missing = self.m.missing_sources(src)
        self.assertEqual(missing, [r for r in self.m.SOURCE_FILES if r.startswith(('common/', 'capacity/'))])
        code, _, err = self.install(source=src)
        self.assertEqual(code, 1, err)
        self.assertIn(f'{missing[0]} is missing', err)
        self.assertEqual(os.listdir(self.root), ['usr'])

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
