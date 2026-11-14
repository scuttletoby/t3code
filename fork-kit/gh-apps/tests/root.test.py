"""gh-apps with real users, real sudo and the real installer. CI ONLY: a throw-away GitHub Actions runner, as root:

    sudo env GH_APPS_ROOT_TESTS=1 GITHUB_ACTIONS=true python3 -I -B fork-kit/gh-apps/tests/root.test.py

It refuses to run anywhere else (it adds users, writes /etc/sudoers.d and removes them again). A fake gh in
/opt/gh-apps-fake prints its arguments, environment and stdin; no request reaches GitHub except one test token
for a made-up App, which GitHub refuses. As the agent: reading keys or cache fails, writing tool or guard files
fails, gh-mint fails, sudo -l shows one command, LD_PRELOAD, GH_*, GIT_* and PYTHON* do not pass, stdin through
the wrapper works. The installer: zero Apps, run twice, bad sudoers never placed, uninstall leaves nothing."""
import grp, json, os, pwd, shutil, subprocess, sys, time, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KIT, 'lib'))
from ghapps import common as c, install  # noqa: E402
from ghapps.common import Refused  # noqa: E402

AGENT = 'ghagent'
SHA = '0' * 40
FAKE_DIR = '/opt/gh-apps-fake'
FAKE_GH = FAKE_DIR + '/gh'
TOKEN = 'ghs_' + 'R00tT3st' * 4 + 'xyzw'
INSTALLER = [sys.executable, '-I', '-B', os.path.join(KIT, 'sbin', 'gh-apps-install')]
ADMIN = [sys.executable, '-I', '-B', os.path.join(KIT, 'sbin', 'gh-apps-admin')]
ATTACK_ENV = {'LD_PRELOAD': '/evil/lib.so', 'LD_LIBRARY_PATH': '/evil', 'GH_DEBUG': 'api', 'GH_HOST': 'evil.example',
              'GH_ENTERPRISE_TOKEN': 'evil', 'GH_CONFIG_DIR': '/evil', 'GIT_DIR': '/evil', 'GIT_CONFIG_GLOBAL': '/evil',
              'PYTHONPATH': '/evil', 'PYTHONSTARTUP': '/evil/start.py', 'PYTHONINSPECT': '1'}
ENABLED = (os.geteuid() == 0 and os.environ.get('GH_APPS_ROOT_TESTS') == '1'
           and os.environ.get('GITHUB_ACTIONS') == 'true')


def run(cmd, **kw):
    kw.setdefault('capture_output', True)
    kw.setdefault('text', True)
    kw.setdefault('timeout', 180)
    return subprocess.run(cmd, **kw)


def as_agent(cmd, input=None, env=None, cwd=None):
    pw = pwd.getpwnam(AGENT)
    base = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': pw.pw_dir, 'LANG': 'C.UTF-8', 'USER': AGENT}
    return run(cmd, user=pw.pw_uid, group=pw.pw_gid, extra_groups=[], cwd=cwd or pw.pw_dir,
               env={**base, **(env or {})}, input=input)


def user_gone(name):
    try:
        pwd.getpwnam(name)
        return False
    except KeyError:
        pass
    try:
        grp.getgrnam(name)
        return False
    except KeyError:
        return True


@unittest.skipUnless(ENABLED, 'CI only: root on a throw-away GitHub Actions runner, GH_APPS_ROOT_TESTS=1')
class RealHost(unittest.TestCase):
    """One install for the whole class; the tests run in name order (test_1_..., test_2_...)."""

    @classmethod
    def setUpClass(cls):
        for name in (AGENT, c.RUN_USER, c.KEYS_USER):
            if not user_gone(name):
                raise RuntimeError(f'user {name} already exists: this is not a throw-away runner')
        if os.path.lexists(c.ETC) or os.path.lexists(c.SUDOERS):
            raise RuntimeError('gh-apps is already installed here: this is not a throw-away runner')
        run(['/usr/sbin/useradd', '--create-home', '--shell', '/bin/sh', AGENT], check=True)
        cls.home = pwd.getpwnam(AGENT).pw_dir
        os.makedirs(FAKE_DIR, mode=0o755)
        os.chmod(FAKE_DIR, 0o755)
        with open(FAKE_GH, 'w') as f:
            f.write('#!/usr/bin/python3 -I\nimport json, os, sys\n'
                    'data = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()\n'
                    'print(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd(), "uid": os.getuid(),'
                    ' "env": dict(os.environ), "stdin": data}))\n'
                    'print("debug: token " + os.environ.get("GH_TOKEN", ""), file=sys.stderr)\n')
        os.chmod(FAKE_GH, 0o755)
        # an old, pre-kit setup in the agent's home: cache, tools, keys
        pw = pwd.getpwnam(AGENT)
        for rel, text in (('.cache/gh-apps/old.token', 'ghs_old'), ('.local/bin/gh-as', '#!/bin/sh\n'),
                          ('.local/bin/gh-app-token', '#!/bin/sh\n'), ('.config/gh-apps/old.pem', 'old key\n')):
            path = os.path.join(cls.home, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                f.write(text)
        run(['/bin/chown', '-R', f'{pw.pw_uid}:{pw.pw_gid}', cls.home], check=True)

    @classmethod
    def tearDownClass(cls):
        if os.path.lexists(c.SUDOERS) or os.path.lexists(c.ETC):
            run(INSTALLER + ['uninstall'])
        run(['/usr/sbin/userdel', '--remove', AGENT])
        shutil.rmtree(FAKE_DIR, ignore_errors=True)

    def installer(self, *args):
        return run(INSTALLER + ['install', '--source', KIT, '--sha', SHA, '--agent-user', AGENT,
                                '--gh-path', FAKE_GH, *args])

    def wrapper(self, *args, input=None, env=None):
        return as_agent([c.WRAPPER, '--repo', 'octo/demo', 'gh', *args], input=input, env=env)

    def test_1_fresh_install_with_zero_apps(self):
        proc = self.installer()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('no app set up yet', proc.stderr)
        self.assertIn(f'gh-apps installed: release {SHA}', proc.stdout)
        proc = self.wrapper('pr', 'list')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('no fallback', proc.stderr)
        self.assertEqual(install.self_check(), [])

    def test_2_old_copies(self):
        for rel in ('.cache/gh-apps', '.local/bin/gh-as', '.local/bin/gh-app-token'):
            self.assertFalse(os.path.lexists(os.path.join(self.home, rel)), rel)
        self.assertTrue(os.path.exists(os.path.join(self.home, '.config/gh-apps/old.pem')))
        self.assertEqual(os.listdir(c.KEYS), [])

    def test_3_add_key_then_run_the_installer_again(self):
        key = run(['/usr/bin/openssl', 'genrsa', '2048'], check=True).stdout
        proc = run(ADMIN + ['add-key', 'demo', '--app-id', '1', '--repo', 'octo/demo', '--installation-id', '1'],
                   input=key)
        # a made-up App: GitHub refuses the test token, but the key and app are stored
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn('test token failed', proc.stderr)
        st = os.stat(f'{c.KEYS}/demo.pem')
        self.assertEqual((st.st_uid, grp.getgrgid(st.st_gid).gr_name, oct(st.st_mode & 0o777)),
                         (0, c.KEYS_USER, '0o640'))
        # a cached token for that app, as gh-mint would leave it
        cache = f'{c.CACHE}/demo.token'
        with open(cache, 'w') as f:
            json.dump({'token': TOKEN, 'expires_at': time.time() + 7200, 'app_id': 1, 'installation_id': 1,
                       'repo': 'octo/demo'}, f)
        keys = pwd.getpwnam(c.KEYS_USER)
        os.chown(cache, keys.pw_uid, keys.pw_gid)
        os.chmod(cache, 0o600)
        before = open(c.SUDOERS).read()
        proc = self.installer()   # run twice: the self-test now makes a token (from the cache) as ghkeys
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn('no app set up yet', proc.stderr)
        self.assertEqual(open(c.SUDOERS).read(), before)
        self.assertEqual(install.self_check(), [])

    def test_4_agent_cannot_read_keys_or_cache_or_write_tool_files(self):
        for path in (f'{c.KEYS}/demo.pem', f'{c.CACHE}/demo.token', c.SUDOERS):
            proc = as_agent(['/bin/cat', path])
            self.assertNotEqual(proc.returncode, 0, path)
            self.assertNotIn('BEGIN', proc.stdout)
            self.assertNotIn(TOKEN, proc.stdout)
        real = os.path.realpath(c.CURRENT)
        for path in (real + '/lib/ghapps/policy.py', real + '/lib/ghapps/new.py', f'{c.APPS}/demo.json',
                     f'{c.APPS}/evil.json', c.CONFIG, '/etc/sudoers.d/evil', c.LIBEXEC + '/evil'):
            proc = as_agent(['/bin/sh', '-c', 'echo x >> "$1"', 'sh', path])
            self.assertNotEqual(proc.returncode, 0, path)
        for link in (c.WRAPPER, c.PRIV_AS, c.CURRENT):
            self.assertNotEqual(as_agent(['/bin/ln', '-sfn', '/tmp', link]).returncode, 0, link)

    def test_5_agent_cannot_run_gh_mint(self):
        for cmd in ([c.MINT, '--app', 'demo'], [c.SUDO, '-n', '-u', c.KEYS_USER, c.MINT, '--app', 'demo'],
                    [c.SUDO, '-n', '-u', c.RUN_USER, c.MINT, '--app', 'demo'],
                    [c.SUDO, '-n', '-u', c.RUN_USER, '/bin/sh', '-c', 'id'],
                    [c.SUDO, '-n', '-u', c.KEYS_USER, '/bin/cat', f'{c.KEYS}/demo.pem']):
            proc = as_agent(cmd)
            self.assertNotEqual(proc.returncode, 0, cmd)
            self.assertNotIn(TOKEN, proc.stdout + proc.stderr)
            self.assertNotIn('BEGIN', proc.stdout)

    def test_6_sudo_l_shows_one_command(self):
        proc = as_agent([c.SUDO, '-n', '-l'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rules = proc.stdout.split('may run the following commands', 1)[1]
        lines = [line.strip() for line in rules.splitlines() if '/' in line]
        self.assertEqual(lines, [f'({c.RUN_USER}) NOPASSWD: {c.PRIV_AS}'], proc.stdout)

    def test_7_environment_does_not_pass(self):
        for proc in (self.wrapper('pr', 'list', env=ATTACK_ENV),
                     as_agent([c.SUDO, '-n', '-u', c.RUN_USER, c.PRIV_AS, '--app', 'demo', '--repo', 'octo/demo',
                               '--', 'gh', 'pr', 'list'], env=ATTACK_ENV)):
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn(TOKEN, proc.stdout + proc.stderr)
            self.assertIn('[redacted]', proc.stderr)
            out = json.loads(proc.stdout)
            env = out['env']
            self.assertEqual((env['GH_HOST'], env['GH_REPO'], env['GH_TOKEN']), ('github.com', 'octo/demo',
                                                                                  '[redacted]'))
            self.assertEqual(out['uid'], pwd.getpwnam(c.RUN_USER).pw_uid)
            self.assertEqual(out['cwd'], c.EMPTY)
            for k, v in env.items():
                self.assertNotIn('evil', v, k)
                self.assertFalse(k.startswith(('LD_', 'PYTHON')) or k == 'GH_DEBUG', k)
                self.assertFalse(k.startswith('GIT_') and k != 'GIT_TERMINAL_PROMPT', k)
        for flag in ('-E', '--preserve-env=GH_DEBUG', '--preserve-env'):
            proc = as_agent([c.SUDO, '-n', flag, '-u', c.RUN_USER, c.PRIV_AS, '--app', 'demo', '--repo',
                             'octo/demo', '--', 'gh', 'pr', 'list'], env=ATTACK_ENV)
            self.assertNotEqual(proc.returncode, 0, flag)
        proc = as_agent([c.SUDO, '-n', '-u', c.RUN_USER, 'GH_DEBUG=api', c.PRIV_AS, '--app', 'demo', '--repo',
                         'octo/demo', '--', 'gh', 'pr', 'list'])
        self.assertNotEqual(proc.returncode, 0)

    def test_8_stdin_and_files_through_the_wrapper(self):
        proc = self.wrapper('pr', 'create', '-t', 'T', '-F', '-', input='body from stdin')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['stdin'], 'body from stdin')
        body = os.path.join(self.home, 'body.md')
        as_agent(['/bin/sh', '-c', 'echo "body from a file" > "$1"', 'sh', body])
        proc = self.wrapper('pr', 'create', '-t', 'T', '-F', body)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual((out['args'][-2:], out['stdin']), (['-F', '-'], 'body from a file\n'))
        for path in (f'{c.KEYS}/demo.pem', f'{c.CACHE}/demo.token'):   # the wrapper opens them as the agent
            proc = self.wrapper('pr', 'create', '-t', 'T', '-F', path)
            self.assertEqual(proc.returncode, 1, path)
            self.assertIn('cannot read', proc.stderr)
            self.assertNotIn('BEGIN', proc.stdout + proc.stderr)
        for path in (f'{c.KEYS}/demo.pem', f'{c.CACHE}/demo.token', '/proc/self/environ'):   # the privileged side
            proc = as_agent([c.SUDO, '-n', '-u', c.RUN_USER, c.PRIV_AS, '--app', 'demo', '--repo', 'octo/demo',
                             '--', 'gh', 'pr', 'create', '-F', path])
            self.assertEqual(proc.returncode, 1, path)
            self.assertIn('file paths are refused here', proc.stderr)
            self.assertNotIn(TOKEN, proc.stdout + proc.stderr)
        self.assertEqual(as_agent([c.WRAPPER, '--self-check']).returncode, 0)

    def test_9a_bad_sudoers_is_never_placed(self):
        before = open(c.SUDOERS).read()
        saved = install.sudoers_text
        install.sudoers_text = lambda agents: 'this is not sudoers syntax (((\n'
        try:
            with self.assertRaisesRegex(Refused, 'did not pass visudo'):
                install.place_sudoers(install.Ops(), [AGENT])
        finally:
            install.sudoers_text = saved
        self.assertEqual(open(c.SUDOERS).read(), before)
        self.assertFalse(os.path.lexists(c.SUDOERS + '.new'))

    def test_9b_uninstall_leaves_nothing(self):
        proc = run(INSTALLER + ['uninstall'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for path in (c.SUDOERS, c.ETC, c.LIBEXEC, c.VARLIB, c.CACHE, c.WRAPPER, c.ADMIN):
            self.assertFalse(os.path.lexists(path), path)
        for name in (c.RUN_USER, c.KEYS_USER):
            self.assertTrue(user_gone(name), name)
        proc = as_agent([c.SUDO, '-n', '-l'])
        self.assertNotIn('gh-apps', proc.stdout)
        self.assertEqual(run(INSTALLER + ['uninstall']).returncode, 0)   # again: nothing to do


if __name__ == '__main__':
    unittest.main(verbosity=2)
