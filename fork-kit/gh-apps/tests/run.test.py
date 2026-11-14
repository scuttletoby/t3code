"""gh-apps privileged gh-as (runs as ghrun): a fake gh that prints its environment never shows the token, the
environment is clean, file paths are refused, kill -9 then a sweep keeps the live gh folder.
python3 -I -B fork-kit/gh-apps/tests/run.test.py"""
import io, json, os, signal, sys, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kit_fixture as kf  # noqa: E402
from ghapps import common as c, run  # noqa: E402

ARGS = ['--app', 'demo', '--repo', 'octo/demo', '--', 'gh']


class Run(unittest.TestCase):
    def setUp(self):
        self.host = kf.FakeHost()
        self.host.lay_out()
        self.host.add_app('demo', 'octo/demo')

    def tearDown(self):
        self.host.close()

    def gh_ran(self):
        return os.path.exists(self.host.gh_marker)

    def test_token_never_reaches_the_output_and_env_is_clean(self):
        proc = kf.drive(self.host, 'run', ARGS + ['pr', 'list'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        both = proc.stdout + proc.stderr
        self.assertNotIn(kf.TOKEN.encode(), both)
        self.assertIn(b'[redacted]', proc.stderr)
        out = json.loads(proc.stdout)
        env = out['env']
        self.assertEqual(env['GH_TOKEN'], '[redacted]')
        self.assertEqual((env['GH_HOST'], env['GH_REPO'], env['PATH']), ('github.com', 'octo/demo', c.SAFE_PATH))
        for k in env:
            self.assertFalse(k.startswith(('LD_', 'GIT_DIR', 'PYTHON', 'GH_DEBUG')), k)
        self.assertNotIn('agentsown', json.dumps(env))
        self.assertEqual(out['cwd'], self.host.path(c.EMPTY))
        self.assertEqual(out['args'], ['pr', 'list'])
        self.assertEqual(os.listdir(self.host.path(c.RUN)), ['.sweep.lock'])   # its run folder is gone

    def test_stdin_reaches_gh_and_is_read_by_the_guard(self):
        proc = kf.drive(self.host, 'run', ARGS + ['pr', 'create', '-t', 'T', '-F', '-'], stdin=b'body text')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['stdin'], 'body text')
        os.unlink(self.host.gh_marker)
        proc = kf.drive(self.host, 'run', ARGS + ['api', 'graphql', '-F', 'query=@-'],
                        stdin=b'mutation { deleteRepository(input:{}) { clientMutationId } }')
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(self.gh_ran())

    def test_file_paths_are_refused_before_gh_or_a_token(self):
        link = self.host.path('/link-to-key')
        os.symlink(self.host.path(f'{c.KEYS}/demo.pem'), link)
        for path in (f'{c.KEYS}/demo.pem', f'{c.CACHE}/demo.token', '/proc/self/environ', link):
            proc = kf.drive(self.host, 'run', ARGS + ['pr', 'create', '-F', path], token='not-a-token-at-all-x')
            self.assertEqual(proc.returncode, 1, path)
            self.assertIn(b'file paths are refused here', proc.stderr)
        self.assertFalse(self.gh_ran())

    def test_app_repo_and_user_are_checked(self):
        for args in (['--app', 'demo', '--repo', 'octo/other', '--', 'gh', 'pr', 'list'],
                     ['--app', 'nope', '--repo', 'octo/demo', '--', 'gh', 'pr', 'list'],
                     ['--app', 'demo', '--repo', 'octo/demo', '--', 'gh', 'auth', 'token']):
            self.assertEqual(kf.drive(self.host, 'run', args).returncode, 1, args)
        ids = dict(self.host.ids, ghrun=(os.getuid() + 1, os.getgid()))
        kf.patch(c, FAKE_IDS=ids)
        proc = kf.drive(self.host, 'run', ARGS + ['pr', 'list'])
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b'runs only as ghrun', proc.stderr)
        self.assertFalse(self.gh_ran())

    def test_gh_must_be_trusted(self):
        os.chmod(self.host.path('/usr/bin'), 0o777)
        proc = kf.drive(self.host, 'run', ARGS + ['pr', 'list'])
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b'writable by no one else', proc.stderr)
        os.chmod(self.host.path('/usr/bin'), 0o755)

    def test_bad_token_from_mint_is_not_used(self):
        proc = kf.drive(self.host, 'run', ARGS + ['pr', 'list'], token='bad token with spaces')
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(self.gh_ran())

    def test_exit_code_and_signal(self):
        self.host.write_gh('import os, sys\nsys.exit(7)\n')
        self.assertEqual(kf.drive(self.host, 'run', ARGS + ['pr', 'list']).returncode, 7)
        self.host.write_gh('import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n')
        self.assertEqual(kf.drive(self.host, 'run', ARGS + ['pr', 'list']).returncode, 128 + signal.SIGTERM)

    def test_kill_9_then_sweep_keeps_the_live_gh_folder(self):
        pidfile = self.host.path('/gh.pid')
        self.host.write_gh(f'import os, time\nopen({pidfile!r}, "w").write(str(os.getpid()))\ntime.sleep(60)\n')
        proc = kf.drive(self.host, 'run', ARGS + ['pr', 'list'], popen=True)
        deadline = time.time() + 30
        while not os.path.exists(pidfile) or not kf.read(pidfile):
            self.assertLess(time.time(), deadline, proc.stderr.read1() if proc.poll() is not None else 'no gh')
            time.sleep(0.05)
        gh_pid = int(kf.read(pidfile))
        proc.kill()
        proc.communicate()
        root = self.host.path(c.RUN)
        try:
            folders = [n for n in os.listdir(root) if n.startswith('gh-as-')]
            self.assertEqual(len(folders), 1)
            with run.sweep_lock(root):
                self.assertEqual(run.sweep(root), 0)
            self.assertTrue(os.path.isdir(os.path.join(root, folders[0])), 'the live gh folder was removed')
        finally:
            os.kill(gh_pid, signal.SIGKILL)
        deadline = time.time() + 30
        while True:
            with run.sweep_lock(root):
                if run.sweep(root) == 1:
                    break
            self.assertLess(time.time(), deadline, 'the dead folder was never swept')
            time.sleep(0.05)
        self.assertEqual([n for n in os.listdir(root) if n.startswith('gh-as-')], [])

    def test_sweep_removes_a_folder_without_lock(self):
        root = self.host.path(c.RUN)
        os.mkdir(os.path.join(root, 'gh-as-broken'))
        os.mkdir(os.path.join(root, 'not-ours'))
        with run.sweep_lock(root):
            self.assertEqual(run.sweep(root), 1)
        self.assertEqual(sorted(os.listdir(root)), ['.sweep.lock', 'not-ours'])


class Scrub(unittest.TestCase):
    def scrub(self, chunks, token=kf.TOKEN):
        out = io.BytesIO()
        s = run.Scrubber(out, token)
        for ch in chunks:
            s.feed(ch)
        s.close()
        return out.getvalue()

    def test_token_split_at_every_point(self):
        text = b'x' * 700 + b'token=' + kf.TOKEN.encode() + b' end ' + b'y' * 900
        for cut in range(0, len(text), 7):
            got = self.scrub([text[:cut], text[cut:]])
            self.assertNotIn(kf.TOKEN.encode(), got, cut)
            self.assertNotIn(kf.TOKEN.encode()[4:20], got, cut)
            self.assertEqual(got.count(b'[redacted]'), 1, cut)
        got = self.scrub([bytes([b]) for b in text])
        self.assertEqual(got, text.replace(kf.TOKEN.encode(), b'[redacted]'))

    def test_other_token_shapes(self):
        for secret in (b'ghp_' + b'a' * 36, b'github_pat_' + b'b' * 70, b'gho_' + b'c' * 30,
                       b'-----BEGIN RSA PRIVATE KEY-----'):
            got = self.scrub([b'q' * 600 + secret[:9], secret[9:] + b'\n'])
            self.assertNotIn(secret, got)


if __name__ == '__main__':
    unittest.main(verbosity=1)
