"""gh-apps installer, admin command and bootstrap.sh, on a fake host with fake users, a fake visudo and a fake curl:
run twice, stop after each step then run again, a fresh machine with zero Apps, upgrade keeps keys, uninstall leaves
nothing, a bad sudoers file is never placed, a sha that is not a protected release is refused.
python3 -I -B fork-kit/gh-apps/tests/install.test.py   (the same with real users and sudo: tests/root.test.py, CI)"""
import contextlib, hashlib, io, json, os, shutil, stat, subprocess, sys, tarfile, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kit_fixture as kf  # noqa: E402
from ghapps import admin, common as c, install  # noqa: E402
from ghapps.common import Refused  # noqa: E402

SHA_A, SHA_B = 'a' * 40, 'b' * 40
FAKE_PEM = '-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n'


class FakeOps(install.Ops):
    """Fake users, groups, homes, visudo and run-as. rm commands (cleanup of old copies) really run."""

    def __init__(self, host):
        self.host, self.calls = host, []
        self.groups = {'agent': {'agent'}}
        self.homes = {'agent': host.path('/home/agent')}
        self.visudo = lambda path: True
        self.mint = lambda name, flags: ((0, '22\n', '') if flags == ['--find-installation']
                                         else (0, '', f'ok: app {name}, token valid for 60 min'))

    def add_system_user(self, name):
        self.calls.append(('useradd', name))
        self.host.ids[name] = (os.getuid(), os.getgid())

    def del_user(self, name):
        self.calls.append(('userdel', name, os.path.exists(self.host.path(c.SUDOERS))))
        self.host.ids.pop(name, None)

    def groups_of(self, user):
        return self.groups[user]

    def home_of(self, user):
        return self.homes[user]

    def visudo_ok(self, path):
        self.calls.append(('visudo', path and os.path.basename(path)))
        return self.visudo(path)

    def run_as(self, user, cmd):
        self.calls.append(('run_as', user, cmd))
        if cmd[0] == '/bin/rm':
            return subprocess.run(cmd).returncode, '', ''
        assert cmd[0] == c.p(c.MINT) and cmd[1] == '--app', cmd
        return self.mint(cmd[2], cmd[3:])


def snapshot(root):
    """Every path under root: kind, mode, content hash or link target (the root folder name taken out)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            rel = os.path.relpath(path, root)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ('link', os.readlink(path).replace(root, ''))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ('dir', oct(stat.S_IMODE(st.st_mode)))
            else:
                with open(path, 'rb') as f:
                    data = f.read().replace(root.encode(), b'')
                out[rel] = ('file', oct(stat.S_IMODE(st.st_mode)), hashlib.sha256(data).hexdigest())
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.host = kf.FakeHost()
        os.makedirs(self.host.path('/usr/local/libexec'), mode=0o755)   # a standard folder, there before
        for name in (c.RUN_USER, c.KEYS_USER):                            # a fresh machine: no service users
            self.host.ids.pop(name)
        self.ops = FakeOps(self.host)
        self.err = ''

    def tearDown(self):
        self.host.close()

    def quiet(self, fn, *args, **kw):
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                return fn(*args, **kw)
        finally:
            self.err = err.getvalue() + out.getvalue()

    def install(self, sha=SHA_A, agent='agent', **kw):
        return self.quiet(install.install, kf.KIT, sha, agent, ops=self.ops, **kw)

    def add_key(self, name='demo', repo='octo/demo', **kw):
        return self.quiet(admin.add_key, self.ops, name, 11, repo, key_text=FAKE_PEM, **kw)

    def exists(self, path):
        return os.path.lexists(self.host.path(path))


class Install(Base):
    def test_fresh_machine_zero_apps(self):
        self.install()
        self.assertIn(('useradd', 'ghrun'), self.ops.calls)
        self.assertIn(('useradd', 'ghkeys'), self.ops.calls)
        self.assertIn('no app set up yet', self.err)
        self.assertEqual([x for x in self.ops.calls if x[0] == 'run_as'], [])
        self.assertEqual(install.self_check(), [])
        sudoers = self.host.path(c.SUDOERS)
        self.assertEqual(stat.S_IMODE(os.stat(sudoers).st_mode), 0o440)
        text = kf.read(sudoers)
        self.assertIn('agent ALL=(ghrun) NOPASSWD: /usr/local/libexec/gh-apps/gh-as\n', text)
        self.assertIn('ghrun ALL=(ghkeys) NOPASSWD: /usr/local/libexec/gh-apps/gh-mint\n', text)
        for cmd in (c.PRIV_AS, c.MINT):
            self.assertIn(f'Defaults!{cmd} env_reset, !setenv, secure_path="{c.SAFE_PATH}"\n', text)
        self.assertEqual(os.readlink(self.host.path(c.WRAPPER)), self.host.path(c.LIBEXEC) + '/current/bin/gh-as')
        self.assertEqual(json.loads(kf.read(self.host.path(c.CONFIG)))['agent_users'], ['agent'])
        for path, mode in ((c.KEYS, 0o750), (c.CACHE, 0o700), (c.RUN, 0o700), (c.EMPTY, 0o555)):
            self.assertEqual(stat.S_IMODE(os.stat(self.host.path(path)).st_mode), mode, path)

    def test_run_twice_changes_nothing(self):
        self.install()
        first = snapshot(self.host.root)
        inode = os.stat(self.host.path(c.SUDOERS)).st_ino
        self.ops.calls.clear()
        self.install()
        self.assertEqual(snapshot(self.host.root), first)
        self.assertEqual(os.stat(self.host.path(c.SUDOERS)).st_ino, inode)
        self.assertFalse([x for x in self.ops.calls if x[0] == 'useradd'])

    def test_stop_after_each_step_then_run_again(self):
        self.install()
        want = snapshot(self.host.root)
        for i, step in enumerate(install.STEPS):
            with self.subTest(step=step):
                self.tearDown()
                self.setUp()
                self.install(stop_after=step)
                if i < install.STEPS.index('sudoers'):
                    self.assertFalse(self.exists(c.SUDOERS), 'sudoers placed before the self-test passed')
                if i < install.STEPS.index('wrapper'):
                    self.assertFalse(self.exists(c.WRAPPER))
                self.install()
                self.assertEqual(snapshot(self.host.root), want)
                self.assertEqual(install.self_check(), [])

    def test_crash_half_way_leaves_leftovers_that_the_next_run_clears(self):
        self.install(stop_after='folders')
        versions = self.host.path(c.VERSIONS)
        os.makedirs(os.path.join(versions, '.tmp-aaaaaaaaaaaa-x/lib'))   # killed while copying
        self.install(stop_after='tool')
        os.unlink(os.path.join(versions, SHA_A, 'lib/ghapps/run.py'))     # a version folder that lost a file
        with open(self.host.path(c.SUDOERS) + '.new', 'w') as f:          # killed before visudo
            f.write('junk')
        self.install()
        self.assertIn('incomplete or changed: made again', self.err)
        self.assertEqual(os.listdir(versions), [SHA_A])
        self.assertFalse(self.exists(c.SUDOERS + '.new'))
        self.assertEqual(install.self_check(), [])

    def test_upgrade_keeps_keys_and_the_old_version(self):
        self.install()
        self.add_key()
        key = kf.read(self.host.path(f'{c.KEYS}/demo.pem'))
        app = kf.read(self.host.path(f'{c.APPS}/demo.json'))
        self.ops.calls.clear()
        self.install(sha=SHA_B)
        self.assertEqual(kf.read(self.host.path(f'{c.KEYS}/demo.pem')), key)
        self.assertEqual(kf.read(self.host.path(f'{c.APPS}/demo.json')), app)
        self.assertEqual(os.readlink(self.host.path(c.CURRENT)), f'versions/{SHA_B}')
        self.assertEqual(sorted(os.listdir(self.host.path(c.VERSIONS))), [SHA_A, SHA_B])
        self.assertIn(('run_as', 'ghkeys', [c.p(c.MINT), '--app', 'demo', '--check']), self.ops.calls)
        self.assertEqual(install.self_check(), [])

    def test_self_test_failure_never_places_sudoers(self):
        self.install(stop_after='folders')
        self.host.add_app('demo', 'octo/demo')
        self.ops.mint = lambda name, flags: (1, '', 'gh-mint: app demo: GitHub refused the key (401)')
        with self.assertRaisesRegex(Refused, 'app demo gets no token, sudoers not placed'):
            self.install()
        self.assertFalse(self.exists(c.SUDOERS))
        self.assertFalse(self.exists(c.WRAPPER))

    def test_bad_sudoers_is_never_placed(self):
        self.install(stop_after='selftest')
        self.ops.visudo = lambda path: path is None   # the config is fine, the new file is not
        with self.assertRaisesRegex(Refused, 'did not pass visudo -cf; nothing was placed'):
            self.install()
        self.assertEqual(os.listdir(self.host.path('/etc/sudoers.d')), [])
        whole = iter((True, False))   # the whole config passes before, and fails with the new file in place
        self.ops.visudo = lambda path: path is not None or next(whole)
        with self.assertRaisesRegex(Refused, 'whole configuration did not pass visudo -c with the new file'):
            self.install()
        self.assertEqual(os.listdir(self.host.path('/etc/sudoers.d')), [])
        self.assertFalse(self.exists(c.WRAPPER))

    def test_sudo_config_that_is_already_broken_is_named_and_nothing_placed(self):
        self.install(stop_after='selftest')
        self.ops.visudo = lambda path: path is not None   # another file in sudo's config is already wrong
        self.ops.calls.clear()
        with self.assertRaisesRegex(Refused, 'already fails visudo -c, before gh-apps changes it'):
            self.install()
        self.assertEqual([x for x in self.ops.calls if x[0] == 'visudo'], [('visudo', None)])
        self.assertEqual(os.listdir(self.host.path('/etc/sudoers.d')), [])
        self.assertFalse(self.exists(c.WRAPPER))

    def test_sudoers_needs_the_include_folder(self):
        with open(self.host.path('/etc/sudoers'), 'w') as f:
            f.write('Defaults env_reset\n')
        with self.assertRaisesRegex(Refused, 'does not include /etc/sudoers.d'):
            self.install()
        self.assertFalse(self.exists(c.SUDOERS))

    def test_sudoers_text_refuses_odd_user_names(self):
        for bad in ('ALL', 'a b', 'agent\nagent ALL=(ALL) ALL', 'Agent', '#1', 'a,b'):
            with self.subTest(bad=bad), self.assertRaises(Refused):
                install.sudoers_text([bad])

    def test_uninstall_leaves_nothing(self):
        before = snapshot(self.host.root)
        self.install()
        self.add_key()
        with open(self.host.path(f'{c.CACHE}/demo.token'), 'w') as f:
            f.write('{}')
        self.quiet(install.uninstall, self.ops)
        self.assertEqual(snapshot(self.host.root), before)
        dels = [x for x in self.ops.calls if x[0] == 'userdel']
        self.assertEqual([x[1] for x in dels], ['ghrun', 'ghkeys'])
        self.assertFalse(any(x[2] for x in dels), 'a user was removed while sudoers still named it')
        self.quiet(install.uninstall, self.ops)   # again: nothing left to do, no error

    def test_old_copies_removed_old_keys_reported_not_imported(self):
        home = self.host.path('/home/agent')
        for rel, text in (('.cache/gh-apps/old.token', 'ghs_old'), ('.local/bin/gh-as', '#!/bin/sh'),
                          ('.local/bin/gh-app-token', '#!/bin/sh'), ('.config/gh-apps/old.pem', FAKE_PEM)):
            os.makedirs(os.path.dirname(os.path.join(home, rel)), exist_ok=True)
            with open(os.path.join(home, rel), 'w') as f:
                f.write(text)
        self.install()
        for rel in ('.cache/gh-apps', '.local/bin/gh-as', '.local/bin/gh-app-token'):
            self.assertFalse(os.path.lexists(os.path.join(home, rel)), rel)
        self.assertTrue(os.path.exists(os.path.join(home, '.config/gh-apps/old.pem')))
        self.assertIn('count as leaked and were NOT imported', self.err)
        self.assertEqual(os.listdir(self.host.path(c.KEYS)), [])
        self.assertEqual({x[1] for x in self.ops.calls if x[0] == 'run_as'}, {'agent'})

    def test_refused_inputs(self):
        self.host.ids['toor'] = (0, 0)
        cases = [dict(agent='root'), dict(agent='ghrun'), dict(agent='ghkeys'), dict(agent='nobody-here'),
                 dict(agent='Bad Name'), dict(agent='toor'), dict(sha='abc'), dict(sha='A' * 40),
                 dict(gh_path='usr/bin/gh'), dict(gh_path='/usr/bin/hub')]
        for kw in cases:
            with self.subTest(kw=kw), self.assertRaises(Refused):
                self.install(**kw)
        self.ops.groups['agent'] = {'agent', 'ghkeys'}
        with self.assertRaisesRegex(Refused, 'is in group'):
            self.install()
        self.ops.groups['agent'] = {'agent'}
        os.chmod(self.host.path('/usr/bin'), 0o777)
        with self.assertRaisesRegex(Refused, 'writable by no one else'):
            self.install()
        os.chmod(self.host.path('/usr/bin'), 0o755)
        kf.patch(c, ROOT_UID=os.getuid() + 1)
        with self.assertRaisesRegex(Refused, 'run this as root'):
            self.install()
        kf.patch(c, ROOT_UID=os.getuid())
        self.assertFalse(self.exists(c.ETC))

    def test_source_missing_a_file_is_refused(self):
        src = tempfile.mkdtemp(dir=self.host.root)
        for rel in install.SOURCE_FILES[:-1]:
            os.makedirs(os.path.dirname(os.path.join(src, rel)), exist_ok=True)
            shutil.copyfile(os.path.join(kf.KIT, rel), os.path.join(src, rel))
        with self.assertRaisesRegex(Refused, 'README.md is missing'):
            self.quiet(install.install, src, SHA_A, 'agent', ops=self.ops)

    def test_link_or_file_where_the_kit_expects_its_own_is_refused(self):
        other = tempfile.mkdtemp(dir=self.host.root)
        os.symlink(other, self.host.path(c.ETC))
        with self.assertRaisesRegex(Refused, 'is not a folder'):
            self.install()
        os.unlink(self.host.path(c.ETC))
        with open(self.host.path(c.WRAPPER), 'w') as f:   # an old hand-made copy
            f.write('#!/bin/sh\n')
        with self.assertRaisesRegex(Refused, 'is not a link'):
            self.install()
        self.assertTrue(self.exists(c.SUDOERS))   # placed before the wrapper step; the next run finishes

    def test_self_check_finds_changes(self):
        def append(rel, text=' '):
            with open(self.host.path(rel), 'a') as f:
                f.write(text)
        version = f'{c.VERSIONS}/{SHA_A}'
        changes = [
            (lambda: os.chmod(self.host.path(version + '/lib/ghapps/run.py'), 0o666), 'writable by others'),
            (lambda: append(version + '/lib/ghapps/run.py', '\nimport os\n'), 'run.py: changed since install'),
            (lambda: append(version + '/lib/ghapps/extra.py'), 'not in the manifest'),
            (lambda: os.chmod(self.host.path(c.KEYS), 0o755), 'owner, group or mode'),
            (lambda: install.switch_link(c.WRAPPER, '/tmp/evil'), 'points to /tmp/evil'),
            (lambda: append(c.CONFIG), 'config.json: changed since install'),
        ]
        for change, want in changes:
            with self.subTest(want=want):
                self.tearDown()
                self.setUp()
                self.install()
                self.assertEqual(install.self_check(), [])
                change()
                problems = install.self_check()
                self.assertTrue(any(want in p for p in problems), problems)
                self.assertEqual(self.quiet(install.self_check_main), 1)


    @unittest.skipIf(os.geteuid() == 0, 'root sees into every folder')
    def test_self_check_as_an_agent_skips_what_it_cannot_see(self):
        self.install()
        folder = self.host.path(os.path.dirname(c.SUDOERS))
        mode = stat.S_IMODE(os.stat(folder).st_mode)
        os.chmod(folder, 0o600)   # like a root-only /etc/sudoers.d seen by an agent: lstat inside it is refused
        try:
            self.assertEqual(install.self_check(), [])
            self.assertEqual(self.quiet(install.self_check_main), 0)
        finally:
            os.chmod(folder, mode)


class Admin(Base):
    def setUp(self):
        super().setUp()
        self.install()

    def test_add_key_stores_key_and_app_and_drops_the_cached_token(self):
        cache = self.host.path(f'{c.CACHE}/demo.token')
        with open(cache, 'w') as f:
            f.write('{}')
        self.ops.mint = lambda name, flags: (0, '22\n', '') if flags == ['--find-installation'] else (0, '', 'ok')
        self.add_key()
        key = self.host.path(f'{c.KEYS}/demo.pem')
        self.assertEqual((kf.read(key), stat.S_IMODE(os.stat(key).st_mode)), (FAKE_PEM, 0o640))
        self.assertEqual(json.loads(kf.read(self.host.path(f'{c.APPS}/demo.json'))),
                         {'app_id': 11, 'installation_id': 22, 'repo': 'octo/demo'})
        self.assertFalse(os.path.exists(cache))
        flags = [x[2][3:] for x in self.ops.calls if x[0] == 'run_as']
        self.assertEqual(flags, [['--find-installation'], ['--check']])

    def test_rotate_and_failures(self):
        self.add_key(installation_id=5)
        self.ops.mint = lambda name, flags: (1, '', 'gh-mint: app demo: GitHub refused the key (401)')
        with self.assertRaisesRegex(Refused, 'test token failed'):
            self.quiet(admin.add_key, self.ops, 'demo', 11, 'octo/demo', 5, FAKE_PEM.replace('AAAA', 'BBBB'))
        self.assertIn('BBBB', kf.read(self.host.path(f'{c.KEYS}/demo.pem')))   # stored: fix and run again
        with self.assertRaisesRegex(Refused, 'one app per repository'):
            self.add_key(name='second', installation_id=5)
        for name, app_id, repo in (('Bad', 11, 'octo/x'), ('x', 0, 'octo/x'), ('x', 11, 'octo'), ('x', 11, 'a/b/c')):
            with self.subTest(name=name, app_id=app_id, repo=repo), self.assertRaises(Refused):
                admin.add_key(self.ops, name, app_id, repo, 5, FAKE_PEM)

    def test_key_on_stdin_must_be_a_private_key(self):
        def read(data):
            old = sys.stdin
            sys.stdin = io.TextIOWrapper(io.BytesIO(data))
            try:
                return admin.read_key()
            finally:
                sys.stdin = old
        self.assertEqual(read(FAKE_PEM.replace('\n', '\r\n').encode()), FAKE_PEM)
        for bad in (b'', b'ghs_' + b'x' * 36, b'-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n',
                    FAKE_PEM.encode() + b'extra', b'A' * (admin.KEY_LIMIT + 1)):
            with self.subTest(bad=bad[:30]), self.assertRaises(Refused):
                read(bad)

    def test_remove_app(self):
        self.add_key(installation_id=5)
        with open(self.host.path(f'{c.CACHE}/demo.token'), 'w') as f:
            f.write('{}')
        self.quiet(admin.remove_app, 'demo')
        self.assertEqual((os.listdir(self.host.path(c.APPS)), os.listdir(self.host.path(c.KEYS)),
                          os.listdir(self.host.path(c.CACHE))), ([], [], []))
        with self.assertRaises(Refused):
            admin.remove_app('demo')


@unittest.skipIf(os.geteuid() == 0, 'as root the real installer would run at the end')
class Bootstrap(unittest.TestCase):
    """bootstrap.sh with a fake curl and a fake id in front of PATH (a copy with only PATH and its temp folder
    changed). The installer it starts at the end is the real one, which refuses because this is not root."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='ghapps-boot-'))
        self.bin, self.fix = os.path.join(self.tmp, 'bin'), os.path.join(self.tmp, 'fix')
        os.mkdir(self.bin)
        os.mkdir(self.fix)
        self.log = os.path.join(self.tmp, 'curl.log')
        with open(os.path.join(kf.KIT, 'bootstrap.sh')) as f:
            text = f.read()
        for old, new in (('PATH=/usr/sbin:', f'PATH={self.bin}:/usr/sbin:'),
                         ('mktemp -d /tmp/', f'mktemp -d {self.tmp}/')):
            self.assertEqual(text.count(old), 1, old)
            text = text.replace(old, new)
        self.script = os.path.join(self.tmp, 'bootstrap.sh')
        with open(self.script, 'w') as f:
            f.write(text)
        self.write_bin('id', 'echo 0')
        self.write_bin('curl', f'''echo "$@" >> {self.log}
out='' url=''
while [ $# -gt 0 ]; do
  case "$1" in -o|-H|--proto|--max-redirs) [ "$1" = -o ] && out="$2"; shift 2 ;; -*) shift ;; *) url="$1"; shift ;; esac
done
case "$url" in
  */branches/*) cp {self.fix}/branch.json "$out" ;;
  */compare/*) cp {self.fix}/compare.json "$out" ;;
  https://codeload.github.com/*) cp {self.fix}/kit.tar.gz "$out" ;;
  *) exit 22 ;;
esac''')
        tar = os.path.join(self.fix, 'kit.tar.gz')
        with tarfile.open(tar, 'w:gz') as t:
            t.add(kf.KIT, f't3code-{SHA_A}/fork-kit/gh-apps',
                  filter=lambda ti: None if '__pycache__' in ti.name else ti)
        with open(tar, 'rb') as f:
            self.tar_sha = hashlib.sha256(f.read()).hexdigest()
        self.release(True, 'ahead', SHA_A)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write_bin(self, name, body):
        with open(os.path.join(self.bin, name), 'w') as f:
            f.write('#!/bin/sh\n' + body + '\n')
        os.chmod(os.path.join(self.bin, name), 0o755)

    def release(self, protected, status, base):
        for name, data in (('branch.json', {'name': 'ci-release', 'protected': protected}),
                           ('compare.json', {'status': status, 'base_commit': {'sha': base}})):
            with open(os.path.join(self.fix, name), 'w') as f:
                json.dump(data, f)

    def run_it(self, *extra, sha=SHA_A):
        args = ['--repo', 'octo/t3code', '--sha', sha, '--agent-user', 'agent', *extra]
        proc = subprocess.run(['/bin/sh', self.script, *args], capture_output=True, text=True, timeout=120,
                              env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith('gh-apps-bootstrap.')], [],
                         'the work folder was left behind')
        return proc

    def curl_urls(self):
        if not os.path.exists(self.log):
            return []
        return [line.split()[-1] for line in kf.read(self.log).splitlines()]

    def test_a_protected_release_is_downloaded_and_installed(self):
        proc = self.run_it('--gh-path', '/usr/bin/gh')
        self.assertIn(f'release {SHA_A} of octo/t3code, download sha256 {self.tar_sha}', proc.stdout + proc.stderr)
        self.assertIn('gh-apps-install: refused: run this as root', proc.stderr)   # the real installer started
        self.assertEqual(self.curl_urls(), ['https://api.github.com/repos/octo/t3code/branches/ci-release',
                                            f'https://api.github.com/repos/octo/t3code/compare/{SHA_A}...ci-release',
                                            f'https://codeload.github.com/octo/t3code/tar.gz/{SHA_A}'])
        for line in kf.read(self.log).splitlines():
            self.assertIn('--proto =https', line)
            self.assertIn('--max-redirs 0', line)

    def test_a_sha_that_is_not_a_protected_release_is_refused(self):
        for protected, status, base in ((False, 'ahead', SHA_A), (True, 'diverged', SHA_A), (True, 'behind', SHA_A),
                                        (True, 'ahead', SHA_B), ('true', 'identical', SHA_A)):
            with self.subTest(protected=protected, status=status, base=base[:4]):
                os.path.exists(self.log) and os.unlink(self.log)
                self.release(protected, status, base)
                proc = self.run_it()
                self.assertEqual(proc.returncode, 1)
                self.assertIn('is not a release', proc.stderr)
                self.assertFalse([u for u in self.curl_urls() if 'codeload' in u], 'downloaded anyway')

    def test_bad_input_and_not_root_refused_before_any_download(self):
        for args, sha in (((), 'abc'), ((), SHA_A.upper()), (('--release-branch', 'a b'), SHA_A),
                          (('--nope',), SHA_A)):
            with self.subTest(args=args, sha=sha[:4]):
                proc = self.run_it(*args, sha=sha)
                self.assertEqual(proc.returncode, 1)
        self.write_bin('id', 'echo 1000')
        proc = self.run_it()
        self.assertIn('run this as root', proc.stderr)
        self.assertEqual(self.curl_urls(), [])

    def test_a_download_without_the_kit_is_refused(self):
        tar = os.path.join(self.fix, 'kit.tar.gz')
        empty = os.path.join(self.tmp, 'empty')
        os.makedirs(os.path.join(empty, 'other'))
        with tarfile.open(tar, 'w:gz') as t:
            t.add(empty, f't3code-{SHA_A}')
            t.add(empty, 'second-top')
        self.assertIn('one top folder', self.run_it().stderr)
        with tarfile.open(tar, 'w:gz') as t:
            t.add(empty, f't3code-{SHA_A}')
        self.assertIn('has no fork-kit/gh-apps', self.run_it().stderr)


if __name__ == '__main__':
    unittest.main(verbosity=1)
