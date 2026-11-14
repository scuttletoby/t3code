"""gh-apps wrapper (runs as the agent): stdin through the wrapper works, files are read as the agent, a fake git
first in PATH is never run, zero apps fail with no fallback. python3 -I -B fork-kit/gh-apps/tests/wrapper.test.py"""
import contextlib, io, os, stat, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kit_fixture as kf  # noqa: E402
from ghapps import common as c, wrapper  # noqa: E402


class Wrapper(unittest.TestCase):
    def setUp(self):
        self.host = kf.FakeHost()
        self.host.lay_out()
        self.host.add_app('demo', 'octo/demo')
        self.work = tempfile.mkdtemp(dir=self.host.path('/home/agent'))
        self.launched = []

        def launch(cmd, fd):
            data = None
            if fd is not None:
                data = os.read(fd, 1 << 20)
                os.close(fd)
            self.launched.append((cmd, data))
        kf.patch(wrapper, launch=launch)

    def tearDown(self):
        self.host.close()

    def call(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), kf.chdir(self.work):
            code = wrapper.main(list(args))
        return code, err.getvalue()

    def test_privileged_call_is_fixed(self):
        cmd = wrapper.priv_command('demo', 'octo/demo', ['pr', 'list'])
        self.assertEqual(cmd, ['/usr/bin/sudo', '-n', '-u', 'ghrun', '--', '/usr/local/libexec/gh-apps/gh-as',
                               '--app', 'demo', '--repo', 'octo/demo', '--', 'gh', 'pr', 'list'])

    def test_file_flag_goes_on_stdin(self):
        body = os.path.join(self.work, 'body.md')
        with open(body, 'w') as f:
            f.write('hello from a file\n')
        for args, want in ((['-F', body], ['-F', '-']), ([f'--body-file={body}'], ['--body-file=-'])):
            self.launched.clear()
            code, err = self.call('--repo', 'octo/demo', 'gh', 'pr', 'create', '-t', 'T', *args)
            self.assertEqual(code, 0, err)
            (cmd, data), = self.launched
            self.assertEqual(cmd[-len(want):], want)
            self.assertEqual(data, b'hello from a file\n')
            self.assertNotIn(body, ' '.join(cmd))

    def test_graphql_query_file_goes_on_stdin(self):
        q = os.path.join(self.work, 'q.graphql')
        with open(q, 'w') as f:
            f.write('query { viewer { login } }')
        code, err = self.call('--app', 'demo', '--', 'gh', 'api', 'graphql', '-F', f'query=@{q}')
        self.assertEqual(code, 0, err)
        (cmd, data), = self.launched
        self.assertEqual(cmd[-2:], ['-F', 'query=@-'])
        self.assertEqual(data, b'query { viewer { login } }')

    @unittest.skipIf(os.geteuid() == 0, 'root reads every file')
    def test_file_the_agent_cannot_read_is_refused(self):
        secret = os.path.join(self.work, 'secret')
        with open(secret, 'w') as f:
            f.write('x')
        os.chmod(secret, 0)
        code, err = self.call('--repo', 'octo/demo', 'gh', 'pr', 'create', '-F', secret)
        self.assertEqual((code, self.launched), (1, []))
        self.assertIn('cannot read', err)

    def test_fifo_and_folder_are_refused(self):
        fifo = os.path.join(self.work, 'fifo')
        os.mkfifo(fifo)
        for path in (fifo, self.work):
            code, err = self.call('--repo', 'octo/demo', 'gh', 'issue', 'create', '-F', path)
            self.assertEqual((code, self.launched), (1, []), err)

    @unittest.skipUnless(os.path.exists(c.GIT), 'needs git at its fixed path')
    def test_repo_from_origin_and_fake_git_in_path_never_runs(self):
        subprocess.run([c.GIT, 'init', '-q', self.work], check=True)
        subprocess.run([c.GIT, '-C', self.work, 'remote', 'add', 'origin', 'git@github.com:octo/demo.git'],
                       check=True)
        fakebin = tempfile.mkdtemp(dir=self.host.root)
        marker = os.path.join(self.host.root, 'fake-git-ran')
        with open(os.path.join(fakebin, 'git'), 'w') as f:
            f.write(f'#!/bin/sh\necho ran > {marker}\necho https://github.com/octo/evil.git\n')
        os.chmod(os.path.join(fakebin, 'git'), 0o755)
        old = os.environ.get('PATH', '')
        os.environ['PATH'] = fakebin + os.pathsep + old
        try:
            code, err = self.call('gh', 'pr', 'list')
        finally:
            os.environ['PATH'] = old
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(marker), 'the git found in PATH ran')
        (cmd, _), = self.launched
        self.assertIn('octo/demo', cmd)

    def test_zero_apps_fail_with_no_fallback(self):
        os.unlink(self.host.path(f'{c.APPS}/demo.json'))
        code, err = self.call('--repo', 'octo/demo', 'gh', 'pr', 'list')
        self.assertEqual((code, self.launched), (1, []))
        self.assertIn('no fallback', err)

    def test_repo_without_app_is_refused(self):
        code, err = self.call('--repo', 'octo/other', 'gh', 'pr', 'list')
        self.assertEqual((code, self.launched), (1, []))
        self.assertIn('no GitHub App is set up for octo/other', err)

    def test_refused_command_never_reaches_sudo(self):
        for args in (['gh', 'auth', 'token'], ['gh', 'api', 'x', '--hostname', 'evil.example'],
                     ['gh', 'pr', 'list', '-R', 'octo/other']):
            code, _ = self.call('--repo', 'octo/demo', *args)
            self.assertEqual((code, self.launched), (1, []), args)

    def test_app_by_name_refused_while_another_app_file_is_or_could_be_for_the_repo(self):
        self.host.add_app('strong', 'octo/demo', app_id=12, installation_id=23)
        code, err = self.call('--app', 'strong', 'gh', 'pr', 'list')
        self.assertEqual((code, self.launched), (1, []))
        self.assertIn('one app per repository', err)
        os.unlink(self.host.path(f'{c.APPS}/strong.json'))
        self.host.add_app('broken', 'octo/other', app_id=13)
        os.chmod(self.host.path(f'{c.APPS}/broken.json'), 0o666)
        code, err = self.call('--app', 'demo', 'gh', 'pr', 'list')
        self.assertEqual((code, self.launched), (1, []))
        self.assertIn('broken (broken)', err)
        os.chmod(self.host.path(f'{c.APPS}/broken.json'), 0o644)
        code, err = self.call('--app', 'demo', 'gh', 'pr', 'list')
        self.assertEqual(code, 0, err)

    def test_usage(self):
        for args in ([], ['pr', 'list'], ['--repo', 'octo/demo', '--app', 'demo', 'gh', 'pr', 'list']):
            code, _ = self.call(*args)
            self.assertEqual(code, 2, args)

    def test_app_config_must_be_root_owned_and_not_writable(self):
        os.chmod(self.host.path(f'{c.APPS}/demo.json'), 0o666)
        code, err = self.call('--repo', 'octo/demo', 'gh', 'pr', 'list')
        self.assertEqual((code, self.launched), (1, []))
        self.assertIn('writable by no one else', err)
        os.chmod(self.host.path(f'{c.APPS}/demo.json'), 0o644)
        os.chmod(self.host.path(c.APPS), 0o775)
        code, _ = self.call('--repo', 'octo/demo', 'gh', 'pr', 'list')
        self.assertEqual(code, 1)

    def test_list(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(wrapper.main(['--list']), 0)
        self.assertIn('demo\tocto/demo', out.getvalue())
        self.assertEqual(stat.S_IMODE(os.stat(self.host.path(c.KEYS)).st_mode), 0o750)


if __name__ == '__main__':
    unittest.main(verbosity=1)
