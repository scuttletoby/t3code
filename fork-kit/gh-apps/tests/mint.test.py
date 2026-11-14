"""gh-apps gh-mint (runs as ghkeys): key checks, the JWT, the token cache, a rotated key, one-repo tokens, no
redirects or proxies. A fake GitHub API on 127.0.0.1 inside the test. python3 -I -B fork-kit/gh-apps/tests/mint.test.py"""
import base64, contextlib, http.server, io, json, os, subprocess, sys, tempfile, threading, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kit_fixture as kf  # noqa: E402
from ghapps import common as c, mint  # noqa: E402
from ghapps.common import Refused  # noqa: E402


class FakeAPI(http.server.BaseHTTPRequestHandler):
    calls = []
    answer = None   # (status, body, headers)

    def log_message(self, *a):
        pass

    def _reply(self):
        n = int(self.headers.get('Content-Length') or 0)
        FakeAPI.calls.append((self.command, self.path, self.headers.get('Authorization'),
                              json.loads(self.rfile.read(n)) if n else None))
        status, body, headers = FakeAPI.answer(self.command, self.path)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        data = json.dumps(body).encode()
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _reply


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def good(method, path, token='ghs_' + 'n' * 36, repos=('octo/demo',)):
    if path.startswith('/app/installations?'):
        return 200, [{'id': 22, 'account': {'login': 'Octo'}}, {'id': 9, 'account': {'login': 'other'}}], {}
    return 201, {'token': token, 'expires_at': iso(time.time() + 3600),
                 'repositories': [{'full_name': r} for r in repos]}, {}


class Mint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keydir = tempfile.mkdtemp()
        cls.key, cls.pub = kf.make_key(cls.keydir)
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeAPI)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

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
        kf.patch(mint, API_URL=f'http://127.0.0.1:{self.server.server_address[1]}')
        FakeAPI.calls, FakeAPI.answer = [], good
        self.app = c.load_app('demo')

    def tearDown(self):
        self.host.close()

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mint.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_jwt_is_signed_by_the_app_key(self):
        jwt = mint.make_jwt(self.app)
        head, body, sig = jwt.split('.')
        pad = lambda s: base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))  # noqa: E731
        claims = json.loads(pad(body))
        self.assertEqual(claims['iss'], '11')
        self.assertLessEqual(claims['exp'] - claims['iat'], 600)
        self.assertEqual(json.loads(pad(head))['alg'], 'RS256')
        sigfile = os.path.join(self.keydir, 'sig')
        with open(sigfile, 'wb') as f:
            f.write(pad(sig))
        proc = subprocess.run(['/usr/bin/openssl', 'dgst', '-sha256', '-verify', self.pub, '-signature', sigfile],
                              input=f'{head}.{body}'.encode(), capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_mints_once_then_uses_the_cache(self):
        code, out, err = self.main('--app', 'demo')
        self.assertEqual((code, out), (0, 'ghs_' + 'n' * 36 + '\n'), err)
        method, path, auth, body = FakeAPI.calls[0]
        self.assertEqual((method, path, body), ('POST', '/app/installations/22/access_tokens',
                                                {'repositories': ['demo']}))
        self.assertTrue(auth.startswith('Bearer ey'))
        cache = self.host.path(f'{c.CACHE}/demo.token')
        self.assertEqual(os.stat(cache).st_mode & 0o777, 0o600)
        self.assertEqual(self.main('--app', 'demo')[1], out)
        self.assertEqual(len(FakeAPI.calls), 1)
        code, out, err = self.main('--app', 'demo', '--check')
        self.assertEqual((code, out), (0, ''))
        self.assertIn('ok: app demo', err)
        self.assertNotIn('nnnnnnnn', err)

    def test_cache_that_is_old_corrupt_shared_or_for_another_app_is_not_used(self):
        cache = self.host.path(f'{c.CACHE}/demo.token')
        entries = [{'token': 'ghs_' + 'o' * 36, 'expires_at': time.time() + 600, 'app_id': 11,
                    'installation_id': 22, 'repo': 'octo/demo'},
                   {'token': 'ghs_' + 'o' * 36, 'expires_at': time.time() + 3600, 'app_id': 12,
                    'installation_id': 22, 'repo': 'octo/demo'},
                   'not json', {'token': 'has space in it ok', 'expires_at': time.time() + 3600}]
        for entry in entries:
            with open(cache, 'w') as f:
                f.write(entry if isinstance(entry, str) else json.dumps(entry))
            os.chmod(cache, 0o600)
            self.assertEqual(mint.token_for(self.app)['token'], 'ghs_' + 'n' * 36, entry)
            os.unlink(cache)
        good_entry = {'token': 'ghs_' + 'g' * 36, 'expires_at': time.time() + 3600, 'app_id': 11,
                      'installation_id': 22, 'repo': 'octo/demo'}
        with open(cache, 'w') as f:
            json.dump(good_entry, f)
        os.chmod(cache, 0o644)   # others could read it: not trusted, replaced
        self.assertEqual(mint.token_for(self.app)['token'], 'ghs_' + 'n' * 36)

    def test_rotated_key_names_the_app_and_cached_token_still_works(self):
        FakeAPI.answer = lambda m, p: (401, {'message': 'A JSON web token could not be decoded'}, {})
        code, out, err = self.main('--app', 'demo')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('app demo', err)
        self.assertIn('rotated', err)
        with open(self.host.path(f'{c.CACHE}/demo.token'), 'w') as f:
            json.dump({'token': 'ghs_' + 'c' * 36, 'expires_at': time.time() + 3000, 'app_id': 11,
                       'installation_id': 22, 'repo': 'octo/demo'}, f)
        os.chmod(self.host.path(f'{c.CACHE}/demo.token'), 0o600)
        self.assertEqual(self.main('--app', 'demo')[:2], (0, 'ghs_' + 'c' * 36 + '\n'))

    def test_token_must_be_limited_to_the_one_repo(self):
        for repos in ((), ('octo/demo', 'octo/other'), ('octo/other',)):
            FakeAPI.answer = lambda m, p, r=repos: good(m, p, repos=r)
            with self.assertRaises(Refused):
                mint.mint(self.app)

    def test_redirect_is_not_followed_and_proxy_not_used(self):
        FakeAPI.answer = lambda m, p: (302, {}, {'Location': 'http://127.0.0.1:1/steal'})
        with self.assertRaisesRegex(Refused, 'redirect'):
            mint.mint(self.app)
        FakeAPI.answer = good
        os.environ['http_proxy'] = os.environ['HTTP_PROXY'] = 'http://127.0.0.1:1'
        try:
            self.assertTrue(mint.mint(self.app)['token'])
        finally:
            del os.environ['http_proxy'], os.environ['HTTP_PROXY']

    def test_key_file_checks(self):
        key = self.host.path(f'{c.KEYS}/demo.pem')
        for mode in (0o644, 0o660, 0o604):
            os.chmod(key, mode)
            with self.subTest(mode=oct(mode)), self.assertRaisesRegex(Refused, 'nothing was minted'):
                mint.make_jwt(self.app)
        os.chmod(key, 0o640)
        os.chmod(self.host.path(c.KEYS), 0o755)
        with self.assertRaises(Refused):
            mint.make_jwt(self.app)
        os.chmod(self.host.path(c.KEYS), 0o750)
        os.rename(key, key + '.real')
        os.symlink(key + '.real', key)
        with self.assertRaises(Refused):
            mint.make_jwt(self.app)
        os.unlink(key)
        with self.assertRaisesRegex(Refused, 'no key .* gh-apps-admin add-key demo'):
            mint.make_jwt(self.app)

    def test_find_installation(self):
        self.host.add_app('demo', 'octo/demo', installation_id=None)
        app = c.load_app('demo')
        self.assertEqual(mint.find_installation(app), 22)
        self.assertEqual(FakeAPI.calls[-1][:2], ('GET', '/app/installations?per_page=100'))
        FakeAPI.answer = lambda m, p: (200, [], {})
        with self.assertRaisesRegex(Refused, '0 installations'):
            mint.find_installation(app)
        with self.assertRaisesRegex(Refused, 'no installation_id'):
            mint.mint(app)

    def test_only_as_the_keys_user(self):
        kf.patch(c, FAKE_IDS=dict(self.host.ids, ghkeys=(os.getuid() + 1, os.getgid())))
        code, out, err = self.main('--app', 'demo')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('runs only as ghkeys', err)
        self.assertEqual(FakeAPI.calls, [])

    def test_usage(self):
        for args in ([], ['--app'], ['--app', 'demo', '--check', '--check'], ['--app', 'demo', '--x']):
            self.assertIn(self.main(*args)[0], (1, 2), args)


if __name__ == '__main__':
    unittest.main(verbosity=1)
