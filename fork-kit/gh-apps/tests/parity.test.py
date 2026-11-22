"""gh-apps parity with the older per-user gh-as, and the state cases: the call forms it took (gh-as NAME -- gh ...,
--repo=, --app=, --help), a GH_REPO for another repository or a GH_HOST other than github.com refused, a cache that is expired, half-written or left
over from a killed run, config missing or empty, two agents minting at once, token values never in messages, and the
modes of the cache. python3 -I -B fork-kit/gh-apps/tests/parity.test.py"""
import contextlib, http.server, io, json, os, stat, sys, tempfile, threading, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kit_fixture as kf  # noqa: E402
from ghapps import common as c, mint, wrapper  # noqa: E402

NEW = 'ghs_' + 'n' * 36      # what the fake GitHub API hands out
OLD = 'ghs_' + 'o' * 36      # a token already in the cache


class Calls(unittest.TestCase):
    """The wrapper's call forms (launch is replaced, so nothing runs)."""

    def setUp(self):
        self.host = kf.FakeHost()
        self.host.lay_out()
        self.host.add_app('demo', 'octo/demo')
        self.work = tempfile.mkdtemp(dir=self.host.path('/home/agent'))
        self.launched = []
        kf.patch(wrapper, launch=lambda cmd, fd: self.launched.append(cmd))
        self.env = {k: os.environ.pop(k, None) for k in ('GH_REPO', 'GH_HOST')}

    def tearDown(self):
        for k, v in self.env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        self.host.close()

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), kf.chdir(self.work):
            code = wrapper.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def want(self):
        return wrapper.priv_command('demo', 'octo/demo', ['pr', 'list'])

    def test_every_form_of_the_older_gh_as_picks_the_same_app(self):
        for args in (['demo', '--', 'gh', 'pr', 'list'],
                     ['--app=demo', 'gh', 'pr', 'list'],
                     ['--app=demo', '--', 'gh', 'pr', 'list'],
                     ['--repo=octo/demo', '--', 'gh', 'pr', 'list'],
                     ['--repo=https://github.com/octo/demo.git', 'gh', 'pr', 'list'],
                     ['--repo', 'octo/demo', '--', 'gh', 'pr', 'list'],
                     ['--app', 'demo', 'gh', 'pr', 'list']):
            self.launched.clear()
            code, _, err = self.call(*args)
            self.assertEqual((code, self.launched), (0, [self.want()]), (args, err))

    def test_bad_forms_are_usage_errors_and_run_nothing(self):
        for args in (['demo', 'gh', 'pr', 'list'],              # a bare name needs the --
                     ['demo', '--', 'pr', 'list'],              # gh must follow
                     ['--app=', 'gh', 'pr', 'list'],
                     ['--repo=', '--', 'gh', 'pr', 'list'],
                     ['demo', '--', 'demo', '--', 'gh', 'pr', 'list'],
                     ['--app=demo', 'demo', '--', 'gh', 'pr', 'list'],
                     ['--repo=octo/demo', '--repo=octo/demo', 'gh', 'pr', 'list'],
                     ['demo', '--', '--repo', 'octo/demo', 'gh', 'pr', 'list'],
                     ['--repo=octo/demo', 'demo', '--', 'gh', 'pr', 'list'],
                     ['-x', '--', 'gh', 'pr', 'list']):
            code, out, err = self.call(*args)
            self.assertEqual((code, out, self.launched), (2, '', []), args)
            self.assertIn('usage: gh-as', err)

    def test_unknown_app_by_bare_name_is_refused(self):
        code, _, err = self.call('nosuch', '--', 'gh', 'pr', 'list')
        self.assertEqual((code, self.launched), (1, []))
        self.assertIn("no app 'nosuch'", err)

    def test_help_goes_to_stdout_and_exits_0(self):
        for flag in ('--help', '-h'):
            code, out, err = self.call(flag)
            self.assertEqual((code, err, self.launched), (0, '', []))
            self.assertIn('usage: gh-as', out)
            self.assertIn('gh-as NAME -- gh', out)

    def test_gh_repo_for_another_repository_is_refused(self):
        for named in ('octo/other', 'github.com/octo/other', 'https://example.com/octo/demo', 'not a repo'):
            os.environ['GH_REPO'] = named
            code, _, err = self.call('--app', 'demo', 'gh', 'pr', 'list')
            self.assertEqual((code, self.launched), (1, []), named)
            self.assertIn('GH_REPO=', err)
            self.assertIn('octo/demo', err)

    def test_gh_repo_for_the_same_repository_or_empty_is_fine(self):
        for named in ('octo/demo', 'OCTO/Demo', 'https://github.com/octo/demo.git', ''):
            self.launched.clear()
            os.environ['GH_REPO'] = named
            code, _, err = self.call('demo', '--', 'gh', 'pr', 'list')
            self.assertEqual((code, self.launched), (0, [self.want()]), (named, err))

    def test_gh_repo_is_masked_before_it_is_cut(self):
        """Lens B: the refusal quotes at most 120 characters; a token that the cut would split is masked first, so
        not even its start is echoed."""
        for pad in (110, 116, 0):
            os.environ['GH_REPO'] = 'x' * pad + NEW
            code, _, err = self.call('--app', 'demo', 'gh', 'pr', 'list')
            self.assertEqual((code, self.launched), (1, []), pad)
            self.assertNotIn('ghs_', err)
            self.assertNotIn('nnnn', err)
            self.assertIn('[red', err)

    def test_gh_host_other_than_github_com_is_refused(self):
        for host in ('ghe.example', 'api.github.com', 'github.com.evil', ' github.com', 'x' * 116 + NEW):
            os.environ['GH_HOST'] = host
            code, _, err = self.call('demo', '--', 'gh', 'pr', 'list')
            self.assertEqual((code, self.launched), (1, []), host)
            self.assertIn('GH_HOST=', err)
            self.assertNotIn('ghs_', err)

    def test_gh_host_github_com_or_empty_is_fine(self):
        for host in ('github.com', 'GitHub.com', ''):
            self.launched.clear()
            os.environ['GH_HOST'] = host
            code, _, err = self.call('demo', '--', 'gh', 'pr', 'list')
            self.assertEqual((code, self.launched), (0, [self.want()]), (host, err))

    def test_gh_repo_never_picks_the_app(self):
        """GH_REPO is checked against the app the call picked; it never picks one itself (no --repo and no origin:
        refused for the missing origin, not run against GH_REPO's repository)."""
        os.environ['GH_REPO'] = 'octo/demo'
        code, _, _ = self.call('gh', 'pr', 'list')
        self.assertEqual((code, self.launched), (1, []))


class NoConfig(unittest.TestCase):
    """Config missing or empty: every side refuses, nothing runs, no fallback."""

    def setUp(self):
        self.host = kf.FakeHost()
        self.host.lay_out()
        self.host.add_app('demo', 'octo/demo')
        self.launched = []
        kf.patch(wrapper, launch=lambda cmd, fd: self.launched.append(cmd))

    def tearDown(self):
        self.host.close()

    def wrapper_call(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = wrapper.main(list(args))
        return code, err.getvalue()

    def states(self):
        """(what, how to make it); each leaves config.json as it found it once undone by lay_out."""
        cfg = self.host.path(c.CONFIG)

        def write(text):
            with open(cfg, 'w') as f:
                f.write(text)
            os.chmod(cfg, 0o644)
        return (('missing', lambda: os.unlink(cfg)),
                ('empty', lambda: write('')),
                ('half-written', lambda: write('{"version": 1, "gh_pa')),
                ('empty object', lambda: write('{}')),
                ('no agent users', lambda: write('{"version": 1, "agent_users": []}')))

    def test_privileged_side_refuses_and_gh_never_runs(self):
        for what, make in self.states():
            make()
            proc = kf.drive(self.host, 'run', ['--app', 'demo', '--repo', 'octo/demo', '--', 'gh', 'pr', 'list'])
            self.assertEqual(proc.returncode, 1, (what, proc.stderr))
            self.assertIn(b'config', proc.stderr, what)
            self.assertFalse(os.path.exists(self.host.gh_marker), what)
            self.assertNotIn(kf.TOKEN.encode(), proc.stdout + proc.stderr)
            self.host.lay_out()

    def test_empty_or_half_written_app_file_is_refused_with_no_fallback(self):
        path = self.host.path(f'{c.APPS}/demo.json')
        for text in ('', '{"app_id": 11, "rep', '[]', '{}'):
            with open(path, 'w') as f:
                f.write(text)
            os.chmod(path, 0o644)
            code, err = self.wrapper_call('--app', 'demo', 'gh', 'pr', 'list')
            self.assertEqual((code, self.launched), (1, []), (text, err))
            code, err = self.wrapper_call('--repo', 'octo/demo', 'gh', 'pr', 'list')
            self.assertEqual((code, self.launched), (1, []), (text, err))

    def test_no_apps_folder_at_all(self):
        os.unlink(self.host.path(f'{c.APPS}/demo.json'))
        os.rmdir(self.host.path(c.APPS))
        code, err = self.wrapper_call('--repo', 'octo/demo', 'gh', 'pr', 'list')
        self.assertEqual((code, self.launched), (1, []))
        self.assertIn('no fallback', err)


class SlowAPI(http.server.BaseHTTPRequestHandler):
    """A fake GitHub API that answers one token per POST, after a pause long enough for a second caller to arrive."""
    posts = []
    pause = 0.0

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        self.rfile.read(n)
        SlowAPI.posts.append(self.path)
        time.sleep(SlowAPI.pause)
        body = json.dumps({'token': NEW, 'expires_at': time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                                                       time.gmtime(time.time() + 3600)),
                           'repositories': [{'full_name': 'octo/demo'}]}).encode()
        self.send_response(201)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CacheState(unittest.TestCase):
    """The token cache across runs: expired, half-written, a killed run's leftovers, two agents at once."""

    @classmethod
    def setUpClass(cls):
        cls.keydir = tempfile.mkdtemp()
        cls.key, _ = kf.make_key(cls.keydir)
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), SlowAPI)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.server.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for name in os.listdir(cls.keydir):
            os.unlink(os.path.join(cls.keydir, name))
        os.rmdir(cls.keydir)

    def setUp(self):
        self.host = kf.FakeHost()
        self.host.lay_out()
        self.host.add_app('demo', 'octo/demo', key=self.key)
        kf.patch(mint, API_URL=self.url)
        SlowAPI.posts, SlowAPI.pause = [], 0.0
        self.app = c.load_app('demo')
        self.cache = self.host.path(f'{c.CACHE}/demo.token')

    def tearDown(self):
        self.host.close()

    def put(self, text, mode=0o600):
        with open(self.cache, 'w') as f:
            f.write(text)
        os.chmod(self.cache, mode)

    def entry(self, **kw):
        e = {'token': OLD, 'expires_at': time.time() + 3600, 'app_id': 11, 'installation_id': 22,
             'repo': 'octo/demo'}
        e.update(kw)
        return json.dumps(e)

    def token(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = mint.token_for(self.app)['token']
        return got, err.getvalue()

    def assert_private_cache_of(self, token):
        st = os.lstat(self.cache)
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        with open(self.cache) as f:
            self.assertEqual(json.load(f)['token'], token)

    def test_fresh_cache_is_used_and_not_minted_again(self):
        self.put(self.entry())
        self.assertEqual(self.token()[0], OLD)
        self.assertEqual(SlowAPI.posts, [])

    def test_expired_cache_is_minted_again_and_overwritten_privately(self):
        for expires in (time.time() - 60, time.time() + 60, 0):   # past, within the renew window, the epoch
            SlowAPI.posts.clear()
            self.put(self.entry(expires_at=expires))
            got, err = self.token()
            self.assertEqual((got, len(SlowAPI.posts)), (NEW, 1), expires)
            self.assert_private_cache_of(NEW)
            self.assertNotIn('o' * 20, err)

    def test_half_written_cache_is_replaced_and_its_token_never_shown(self):
        whole = self.entry()
        for cut in (1, len(whole) // 3, len(whole) // 2, len(whole) - 1):
            SlowAPI.posts.clear()
            self.put(whole[:cut])
            got, err = self.token()
            self.assertEqual((got, len(SlowAPI.posts)), (NEW, 1), cut)
            self.assert_private_cache_of(NEW)
            self.assertNotIn('o' * 8, err)
            self.assertNotIn('n' * 8, err)

    def test_empty_cache_file_is_replaced(self):
        self.put('')
        self.assertEqual(self.token()[0], NEW)
        self.assert_private_cache_of(NEW)

    def test_killed_runs_leftover_temp_file_is_removed(self):
        """A run killed between writing the temp file and renaming it leaves .demo.token.XXXXXXXX.tmp (with a
        token in it): the next run removes it. Other files in the folder stay."""
        stale = self.host.path(f'{c.CACHE}/.demo.token.abcd1234.tmp')
        other = self.host.path(f'{c.CACHE}/.other.token.abcd1234.tmp')
        for path in (stale, other):
            with open(path, 'w') as f:
                f.write(self.entry())
            os.chmod(path, 0o600)
        self.assertEqual(self.token()[0], NEW)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(other))
        self.assertEqual([n for n in os.listdir(self.host.path(c.CACHE)) if n.endswith('.tmp')],
                         ['.other.token.abcd1234.tmp'])

    def test_cache_folder_too_open_is_refused(self):
        os.chmod(self.host.path(c.CACHE), 0o755)
        with self.assertRaises(c.Refused):
            mint.token_for(self.app)
        self.assertEqual(SlowAPI.posts, [])

    def test_lock_and_token_files_are_private(self):
        self.token()
        folder = self.host.path(c.CACHE)
        for name in os.listdir(folder):
            self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(folder, name)).st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE(os.lstat(folder).st_mode), 0o700)

    def test_two_agents_at_once_mint_once_and_get_the_same_token(self):
        """Two gh-mint processes start together with an empty cache; the API takes a second to answer. One mints,
        the other waits on the lock and then reads the cache: one POST, the same token, a whole cache file."""
        SlowAPI.pause = 1.0
        procs = [kf.drive(self.host, 'mint', ['--app', 'demo'], popen=True, extra={'mint.API_URL': self.url})
                 for _ in range(2)]
        outs = [p.communicate(timeout=60) for p in procs]
        for p, (out, err) in zip(procs, outs):
            self.assertEqual(p.returncode, 0, err)
            self.assertEqual(out, (NEW + '\n').encode())
            self.assertNotIn(b'nnnnnnnn', err)
        self.assertEqual(len(SlowAPI.posts), 1)
        self.assert_private_cache_of(NEW)

    def test_check_and_failures_never_print_the_token(self):
        self.put(self.entry())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mint.main(['--app', 'demo', '--check'])
        self.assertEqual((code, out.getvalue()), (0, ''))
        self.assertNotIn('o' * 8, err.getvalue())
        os.unlink(self.host.path(f'{c.KEYS}/demo.pem'))   # the key is gone: refused, the stale token not shown
        self.put(self.entry(expires_at=time.time() - 1))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mint.main(['--app', 'demo'])
        self.assertEqual((code, out.getvalue()), (1, ''))
        self.assertNotIn('o' * 8, err.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=1)
