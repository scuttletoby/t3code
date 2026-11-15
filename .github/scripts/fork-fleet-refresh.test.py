#!/usr/bin/env python3
"""Tests of fork-fleet-launch.sh refresh (the runner template's refresh) and its timer, fork-fleet-refresh.timer.
A fake https release server on 127.0.0.1 (a certificate made here) and temp folders only: nothing is downloaded from
GitHub, nothing runs as root. Run: python3 <this>. Linux only; needs curl, openssl, tar and flock."""
import configparser, hashlib, http.server, io, json, os, platform, shutil, signal, ssl, stat, subprocess, sys
import tarfile, tempfile, threading, time, unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCH = os.path.join(HERE, 'fork-fleet-launch.sh')
OLD, NEW = '2.330.0', '2.331.0'
PLATFORM = {'x86_64': 'linux-x64', 'aarch64': 'linux-arm64', 'arm64': 'linux-arm64',
            'armv7l': 'linux-arm'}.get(platform.machine())


def tarball(version, extra=None):
    """An actions/runner-like release tarball (gzip): run.sh, bin/Runner.Listener and a file naming its version."""
    files = {'run.sh': (b'#!/bin/sh\n', 0o755), 'bin/Runner.Listener': (b'x', 0o755),
             'bin/version.txt': (version.encode(), 0o644), **(extra or {})}
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz') as t:
        for d in ('./', './bin/'):
            info = tarfile.TarInfo(d)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            t.addfile(info)
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo('./' + name)
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
    return out.getvalue()


def record(version, data, digest=True, notes=True, plat=None):
    """The release record GitHub's /releases/latest gives, for a tarball `data`."""
    plat = plat or PLATFORM
    sha = hashlib.sha256(data).hexdigest()
    name = f'actions-runner-{plat}-{version}.tar.gz'
    asset = {'name': name, 'browser_download_url': f'https://example.invalid/{name}'}
    if digest:
        asset['digest'] = 'sha256:' + (sha if digest is True else digest)
    body = '## SHA-256 Checksums\n'
    if notes:
        body += f'- {name} <!-- BEGIN SHA {plat} -->{sha if notes is True else notes}<!-- END SHA {plat} -->\n'
    return {'tag_name': f'v{version}', 'body': body, 'assets': [asset]}


class Server(http.server.ThreadingHTTPServer):
    """GET /release: `self.release` (bytes or a dict). GET /download/v<ver>/<name>: `self.files[name]`. `self.cut`
    sends half a tarball and closes; `self.hold` (an Event) holds a tarball's answer until it is set."""
    daemon_threads = True

    def __init__(self):
        super().__init__(('127.0.0.1', 0), Handler)
        self.release, self.files, self.cut, self.hold = {}, {}, False, None
        self.asked, self.downloading = [], threading.Event()

    def handle_error(self, request, client_address):
        pass   # a client killed or cut half-way (the tests do that on purpose)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        s = self.server
        s.asked.append(self.path)
        if self.path == '/release':
            body = s.release if isinstance(s.release, bytes) else json.dumps(s.release).encode()
        else:
            body = s.files.get(self.path.rsplit('/', 1)[-1])
            if not self.path.startswith('/download/v') or body is None:
                self.send_error(404)
                return
            s.downloading.set()
            if s.hold is not None:
                s.hold.wait(30)
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if s.cut and self.path != '/release':
            self.wfile.write(body[:len(body) // 2])
            self.wfile.flush()
            self.connection.shutdown(2)   # the connection drops half-way
            return
        self.wfile.write(body)


@unittest.skipUnless(sys.platform.startswith('linux') and PLATFORM, 'Linux on a machine actions/runner has a release for')
class Refresh(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ('curl', 'openssl', 'flock', 'tar', 'sha256sum'):
            if not shutil.which(tool):
                raise unittest.SkipTest(f'no {tool}')
        cls.certs = tempfile.mkdtemp()
        cls.cert = os.path.join(cls.certs, 'cert.pem')
        key = os.path.join(cls.certs, 'key.pem')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key, '-out', cls.cert,
                        '-days', '1', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'],
                       check=True, capture_output=True)
        cls.server = Server()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cls.cert, key)
        cls.server.socket = ctx.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'https://127.0.0.1:{cls.server.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.certs, True)

    def setUp(self):
        s = self.server
        s.asked.clear()
        s.downloading.clear()
        s.cut, s.hold = False, None
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        os.makedirs(self.path('bin'))
        os.makedirs(self.path('opt'), 0o755)
        self.template = self.path('opt/runner-template')
        self.offer(NEW)
        self.conf()

    def path(self, rel):
        return os.path.join(self.d, rel)

    def write(self, rel, text, mode=0o644):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w') as f:
            f.write(text)
        os.chmod(p, mode)
        return p

    def offer(self, version, data=None, **kw):
        """The release server's newest release: `version`, with tarball `data`."""
        data = tarball(version) if data is None else data
        self.server.files = {f'actions-runner-{PLATFORM}-{version}.tar.gz': data}
        self.server.release = record(version, data, **kw)

    def conf(self, **over):
        settings = dict(RUNNER_USERS='nobody', TEMPLATE=self.template, WORK_ROOT=self.path('var/runners'),
                        RUN_DIR=self.path('run'), RUNNER_RELEASE=f'{self.base}/release',
                        RUNNER_DOWNLOAD=f'{self.base}/download')
        settings.update(over)
        self.write('launch.conf', ''.join(f'{k}={v}\n' for k, v in settings.items()), 0o600)

    def env(self):
        return {'PATH': f'{self.path("bin")}:/usr/bin:/bin', 'FORK_FLEET_LAUNCH_CONF': self.path('launch.conf'),
                'CURL_CA_BUNDLE': self.cert}

    def launch(self, *args):
        return subprocess.run(['bash', LAUNCH, *args], env=self.env(), capture_output=True, text=True, timeout=120)

    def popen(self, *args):
        return subprocess.Popen(['bash', LAUNCH, *args], env=self.env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)

    def old_template(self):
        """An installed template of OLD, as host step 7 leaves it."""
        os.makedirs(f'{self.template}/bin', 0o755)
        self.write('opt/runner-template/run.sh', '#!/bin/sh\n', 0o755)
        self.write('opt/runner-template/bin/Runner.Listener', 'x', 0o755)
        self.write('opt/runner-template/bin/version.txt', OLD)
        self.write('opt/runner-template/.fork-fleet-version', f'{OLD}\n')
        os.chmod(self.template, 0o755)
        return os.stat(self.template).st_ino

    def version(self):
        with open(f'{self.template}/.fork-fleet-version') as f:
            return f.read()

    def leftovers(self):
        return sorted(n for n in os.listdir(self.path('opt')) if n != 'runner-template')

    def downloads(self):
        return [p for p in self.server.asked if p.startswith('/download/')]

    def assert_kept(self, got, word, ino):
        """The refresh failed with `word` and left the old template as it was, and no temp folder."""
        self.assertNotEqual(got.returncode, 0)
        self.assertIn(word, got.stderr)
        self.assertIn('the template stays as it is', got.stderr)
        self.assertEqual(os.stat(self.template).st_ino, ino)
        self.assertEqual(self.version(), f'{OLD}\n')
        self.assertEqual(self.leftovers(), [])

    def test_no_template_closes_the_pool_and_refresh_makes_one(self):
        got = self.launch('ready', NEW)
        self.assertNotEqual(got.returncode, 0)
        self.assertIn(f'there is no runner template ({self.template})', got.stderr)
        self.assertIn('fork-fleet-launch.sh refresh', got.stderr)
        got = self.launch('refresh')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn(f'the template is actions/runner {NEW} (was none)', got.stdout)
        self.assertEqual(self.version(), f'{NEW}\n')
        self.assertEqual(stat.S_IMODE(os.stat(self.template).st_mode), 0o755)
        for root, dirs, files in os.walk(self.template):
            for n in dirs + files:
                self.assertFalse(os.lstat(os.path.join(root, n)).st_mode & 0o022, n)   # nobody else may write it
        self.assertTrue(os.access(f'{self.template}/run.sh', os.X_OK))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.launch('ready', NEW).returncode, 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.path('run')).st_mode), 0o700)

    def test_an_older_template_is_swapped_and_running_copies_keep_theirs(self):
        self.old_template()
        copy = self.path('var/runners/fleet-vm-t3code-1')
        os.makedirs(os.path.dirname(copy), 0o755)
        subprocess.run(['cp', '-al', '--', self.template, copy], check=True)   # a job's copy, as start makes it
        got = self.launch('refresh')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn(f'actions/runner {NEW} (was {OLD})', got.stdout)
        self.assertEqual(self.version(), f'{NEW}\n')
        with open(f'{self.template}/bin/version.txt') as f:
            self.assertEqual(f.read(), NEW)
        with open(f'{copy}/bin/version.txt') as f:
            self.assertEqual(f.read(), OLD)   # the running job's copy is untouched
        self.assertEqual(self.leftovers(), [])   # the old template went with the temp folder
        self.assertEqual(self.launch('ready', NEW).returncode, 0)

    def test_the_same_version_does_nothing(self):
        for have in (NEW, '2.331.1', '3.0.0'):   # the same, or newer than the release (never downgraded)
            with self.subTest(have):
                ino = self.old_template()
                self.write('opt/runner-template/.fork-fleet-version', f'{have}\n')
                self.server.asked.clear()
                got = self.launch('refresh')
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertIn('nothing to do', got.stdout)
                self.assertEqual(self.downloads(), [])
                self.assertEqual(os.stat(self.template).st_ino, ino)
                shutil.rmtree(self.template)

    def test_a_template_without_a_good_version_file_is_replaced(self):
        for text in (None, 'latest\n'):
            with self.subTest(text):
                self.old_template()
                if text is None:
                    os.remove(f'{self.template}/.fork-fleet-version')
                else:
                    self.write('opt/runner-template/.fork-fleet-version', text)
                got = self.launch('refresh')
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertEqual(self.version(), f'{NEW}\n')
                shutil.rmtree(self.template)

    def test_a_cut_download_keeps_the_old_template(self):
        ino = self.old_template()
        self.server.cut = True
        self.assert_kept(self.launch('refresh'), 'the download of', ino)

    def test_a_bad_checksum_is_refused_before_anything_is_unpacked(self):
        ino = self.old_template()
        self.write('bin/tar', f'#!/bin/sh\ntouch {self.path("tar-ran")}\nexec /usr/bin/tar "$@"\n', 0o755)
        self.offer(NEW, digest='0' * 64, notes=False)
        self.assert_kept(self.launch('refresh'), "does not match the release's sha256", ino)
        self.assertEqual(len(self.downloads()), 1)
        self.assertFalse(os.path.exists(self.path('tar-ran')))   # checked before tar ever saw it
        self.offer(NEW)   # the right one: the same path unpacks it
        self.assertEqual(self.launch('refresh').returncode, 0)
        self.assertTrue(os.path.exists(self.path('tar-ran')))

    def test_a_release_record_without_one_good_sha256_is_refused(self):
        data = tarball(NEW)
        sha = hashlib.sha256(data).hexdigest()
        other = hashlib.sha256(b'other').hexdigest()
        cases = {
            'no sha256': (record(NEW, data, digest=False, notes=False), 'gives no sha256'),
            'two sha256': (record(NEW, data, digest=sha, notes=other), 'two different sha256'),
            'a bad digest': (record(NEW, data, digest='md5'), 'is not a sha256'),
            'a bad note': (record(NEW, data, notes='xyz'), 'in its notes is not one'),
            'no asset': (dict(record(NEW, data), assets=[]), f'has no actions-runner-{PLATFORM}-{NEW}.tar.gz'),
            'not a version': (dict(record(NEW, data), tag_name='latest'), "is 'latest', not a version"),
            'not JSON': (b'<html>', 'not JSON'),
        }
        ino = self.old_template()
        for name, (rec, word) in cases.items():
            with self.subTest(name):
                self.server.release = rec
                self.server.asked.clear()
                self.assert_kept(self.launch('refresh'), word, ino)
                self.assertEqual(self.downloads(), [])
        for digest, notes in ((True, False), (False, True)):   # either one alone is enough
            with self.subTest(digest=digest, notes=notes):
                self.server.release = record(NEW, data, digest=digest, notes=notes)
                self.assertEqual(self.launch('refresh').returncode, 0)
                self.assertEqual(self.version(), f'{NEW}\n')
                shutil.rmtree(self.template)
                ino = self.old_template()

    def test_an_unreadable_release_keeps_the_old_template(self):
        ino = self.old_template()
        self.conf(RUNNER_RELEASE=f'{self.base}/nothing-here')
        self.assert_kept(self.launch('refresh'), 'could not read the newest release', ino)

    def test_a_tarball_that_is_no_runner_is_refused(self):
        for name, data in (('not a tarball', b'not gzip'),
                           ('configured', tarball(NEW, {'.runner': (b'{}', 0o644)}))):
            with self.subTest(name):
                ino = self.old_template()
                self.offer(NEW, data)
                self.assert_kept(self.launch('refresh'), 'refresh:', ino)
                shutil.rmtree(self.template)

    def test_a_failed_swap_keeps_the_old_template(self):
        """No fallback to two renames (a moment with no template): when the exchange fails, nothing changes."""
        ino = self.old_template()
        self.write('bin/python3', '#!/bin/bash\n[[ $3 != *renameat2* ]] || { echo "Invalid argument" >&2; exit 1; }\n'
                                  'exec /usr/bin/python3 "$@"\n', 0o755)
        self.assert_kept(self.launch('refresh'), 'could not swap', ino)

    def test_the_swap_waits_for_the_launch_lock(self):
        """No start copies a template that is being swapped: the swap happens under start's lock."""
        ino = self.old_template()
        os.makedirs(self.path('run'), 0o700)
        with open(self.path('run/launch.lock'), 'w') as lock:
            subprocess.run(['flock', '-n', str(lock.fileno())], pass_fds=(lock.fileno(),), check=True)
            proc = self.popen('refresh')
            try:
                deadline = time.time() + 60
                while not any(os.path.exists(self.path(f'opt/{n}/new/.fork-fleet-version')) for n in self.leftovers()):
                    self.assertLess(time.time(), deadline, 'the new template was never ready')
                    time.sleep(0.05)
                time.sleep(1)
                self.assertIsNone(proc.poll())
                self.assertEqual(os.stat(self.template).st_ino, ino)   # held back by the lock
            finally:
                subprocess.run(['flock', '-u', str(lock.fileno())], pass_fds=(lock.fileno(),), check=True)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(self.version(), f'{NEW}\n')

    def test_two_refreshes_at_once_run_one_after_the_other(self):
        self.old_template()
        self.server.hold = threading.Event()
        first = self.popen('refresh')
        try:
            self.assertTrue(self.server.downloading.wait(60))
            second = self.popen('refresh')   # by hand, while the timer's refresh downloads
            time.sleep(1)
            self.assertIsNone(second.poll())   # it waits
        finally:
            self.server.hold.set()
        outs = [p.communicate(timeout=60) for p in (first, second)]
        self.assertEqual([first.returncode, second.returncode], [0, 0], outs)
        self.assertIn(f'(was {OLD})', outs[0][0])
        self.assertIn('nothing to do', outs[1][0])
        self.assertEqual(len(self.downloads()), 1)
        self.assertEqual(self.leftovers(), [])

    def test_a_refresh_stopped_half_way_leaves_the_old_template(self):
        ino = self.old_template()
        self.server.hold = threading.Event()
        proc = self.popen('refresh')
        try:
            self.assertTrue(self.server.downloading.wait(60))
            self.assertEqual(len(self.leftovers()), 1)   # its temp folder, beside the template
            os.killpg(proc.pid, signal.SIGTERM)   # as systemd stops the unit: the whole group
            proc.communicate(timeout=60)
        finally:
            self.server.hold.set()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(os.stat(self.template).st_ino, ino)
        self.assertEqual(self.leftovers(), [])
        # Killed with no chance to clean up (SIGKILL, a power cut): the next refresh removes what is left.
        self.write('opt/.runner-template.refresh.AbC123/new/run.sh', '#!/bin/sh\n', 0o755)
        got = self.launch('refresh')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.version(), f'{NEW}\n')

    def test_unsafe_settings_and_places_are_refused(self):
        ino = self.old_template()
        for name, conf, word in (
                ('http release', dict(RUNNER_RELEASE=f'http://127.0.0.1:{self.server.server_address[1]}/release'),
                 'are https URLs'),
                ('http download', dict(RUNNER_DOWNLOAD='http://127.0.0.1/download'), 'are https URLs'),
                ('relative template', dict(TEMPLATE='opt/runner-template'), 'not a plain absolute path'),
                ('template with a space', dict(TEMPLATE=self.path('opt/a b')), 'not a setting')):
            with self.subTest(name):
                self.conf(**conf)
                got = self.launch('refresh')
                self.assertNotEqual(got.returncode, 0)
                self.assertIn(word, got.stderr)
        self.conf()
        os.chmod(self.path('opt'), 0o775)   # others could plant the temp folder
        got = self.launch('refresh')
        self.assertIn('must be writable by its owner only', got.stderr)
        os.chmod(self.path('opt'), 0o755)
        link = self.path('opt/link')
        os.symlink(self.template, link)
        self.conf(TEMPLATE=link)
        self.assertIn('missing or a symlink', self.launch('refresh').stderr)
        self.assertEqual(self.downloads(), [])
        self.assertEqual(os.stat(self.template).st_ino, ino)

    def test_the_machine_decides_the_platform(self):
        self.write('bin/uname', '#!/bin/sh\ncase $1 in -s) echo "${FAKE_OS:-Linux}" ;; -m) echo "${FAKE_ARCH}" ;; esac\n',
                   0o755)
        for os_name, arch, word in (('Linux', 'riscv64', 'no release for riscv64'),
                                    ('Darwin', 'arm64', 'no runner template for Darwin')):
            with self.subTest(arch):
                env = dict(self.env(), FAKE_OS=os_name, FAKE_ARCH=arch)
                got = subprocess.run(['bash', LAUNCH, 'refresh'], env=env, capture_output=True, text=True, timeout=60)
                self.assertNotEqual(got.returncode, 0)
                self.assertIn(word, got.stderr)
        self.assertEqual(self.server.asked, [])
        plat = 'linux-arm64'
        data = tarball(NEW)
        self.server.files = {f'actions-runner-{plat}-{NEW}.tar.gz': data}
        self.server.release = record(NEW, data, plat=plat)
        env = dict(self.env(), FAKE_ARCH='aarch64')
        got = subprocess.run(['bash', LAUNCH, 'refresh'], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.downloads(), [f'/download/v{NEW}/actions-runner-{plat}-{NEW}.tar.gz'])

    def test_usage_names_refresh(self):
        self.assertIn('| refresh |', self.launch().stderr)
        self.assertIn('usage', self.launch('refresh', 'extra').stderr)


class Timer(unittest.TestCase):
    """fork-fleet-refresh.service and .timer: daily with jitter, catching up after a missed day, one refresh each."""

    def unit(self, name):
        p = configparser.ConfigParser(strict=False, interpolation=None)
        p.optionxform = str
        p.read(os.path.join(HERE, name))
        return p

    def test_the_units(self):
        service, timer = self.unit('fork-fleet-refresh.service'), self.unit('fork-fleet-refresh.timer')
        self.assertEqual(service['Service']['Type'], 'oneshot')
        self.assertEqual(service['Service']['ExecStart'], '/opt/fork-fleet/fork-fleet-launch.sh refresh')
        self.assertEqual(timer['Timer']['OnCalendar'], 'daily')
        self.assertEqual(timer['Timer']['RandomizedDelaySec'], '6h')
        self.assertEqual(timer['Timer']['Persistent'], 'true')
        self.assertEqual(timer['Install']['WantedBy'], 'timers.target')

    def test_systemd_accepts_them(self):
        """As user units (nothing is installed): systemd-analyze verify, with ExecStart pointed at this launcher."""
        if not shutil.which('systemd-analyze'):
            self.skipTest('no systemd-analyze')
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for ext in ('service', 'timer'):
            with open(os.path.join(HERE, f'fork-fleet-refresh.{ext}')) as f:
                text = f.read().replace('/opt/fork-fleet/fork-fleet-launch.sh', LAUNCH)
            with open(os.path.join(d, f't3code-fleet-refresh.{ext}'), 'w') as f:
                f.write(text)
        got = subprocess.run(['systemd-analyze', '--user', 'verify', '--man=no',
                              os.path.join(d, 't3code-fleet-refresh.timer'),
                              os.path.join(d, 't3code-fleet-refresh.service')],
                             capture_output=True, text=True, timeout=60)
        if got.returncode != 0 and 'Failed to' in got.stderr and 'bus' in got.stderr.lower():
            self.skipTest(f'no user manager here: {got.stderr.strip()[:200]}')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertNotIn('Unknown key', got.stderr)


if __name__ == '__main__':
    unittest.main()
