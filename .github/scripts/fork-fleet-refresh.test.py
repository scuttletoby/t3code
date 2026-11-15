#!/usr/bin/env python3
"""Tests of fork-fleet-launch.sh refresh (the runner template's refresh) and its timer, fork-fleet-refresh.timer.
A fake https release server on 127.0.0.1 (a certificate made here) and temp folders only: nothing is downloaded from
GitHub, nothing runs as root. Run: python3 <this>. Linux only; needs curl, openssl, tar and flock."""
import configparser, hashlib, http.server, io, json, os, platform, re, shutil, signal, ssl, stat, subprocess, sys
import tarfile, tempfile, threading, time, unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCH = os.path.join(HERE, 'fork-fleet-launch.sh')
OLD, NEW = '2.330.0', '2.331.0'
PLATFORM = {'x86_64': 'linux-x64', 'aarch64': 'linux-arm64', 'arm64': 'linux-arm64',
            'armv7l': 'linux-arm'}.get(platform.machine())


def hostile(members, version=None):
    """A runner-like tarball (gzip) with extra tarfile members: (TarInfo, data or None) pairs."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz') as t:
        for d in ('./', './bin/'):
            info = tarfile.TarInfo(d)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            t.addfile(info)
        for name, data, mode in (('run.sh', b'#!/bin/sh\n', 0o755), ('bin/Runner.Listener', b'x', 0o755)):
            info = tarfile.TarInfo('./' + name)
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
        for info, data in members:
            if data is not None:
                info.size = len(data)
            t.addfile(info, io.BytesIO(data) if data is not None else None)
    return out.getvalue()


def member(name, kind=tarfile.REGTYPE, mode=0o644, link=''):
    info = tarfile.TarInfo(name)
    info.type, info.mode, info.linkname = kind, mode, link
    return info


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
        self.release, self.files, self.cut, self.hold, self.redirect = {}, {}, False, None, None
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
            if s.redirect:
                self.send_response(302)
                self.send_header('Location', s.redirect + self.path[len('/download'):])
                self.end_headers()
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
        s.cut, s.hold, s.redirect = False, None, None
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
        ino = self.old_template()
        self.write('opt/runner-template/.fork-fleet-version', f'{NEW}\n')
        got = self.launch('refresh')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn('nothing to do', got.stdout)
        self.assertEqual(self.downloads(), [])
        self.assertEqual(os.stat(self.template).st_ino, ino)

    def test_a_template_newer_than_the_newest_release_is_rolled_back(self):
        """A release GitHub withdrew (say a compromised v99999.0.0) must not stay, nor make every later one older."""
        for have in ('2.331.1', '3.0.0', '99999.0.0'):
            with self.subTest(have):
                self.old_template()
                self.write('opt/runner-template/.fork-fleet-version', f'{have}\n')
                got = self.launch('refresh')
                try:
                    self.assertEqual(got.returncode, 0, got.stderr)
                    self.assertIn(f'newer than the newest release {NEW}', got.stderr)
                    self.assertIn(f'rolling it back to {NEW}', got.stderr)
                    self.assertEqual(self.version(), f'{NEW}\n')
                finally:
                    shutil.rmtree(self.template, True)

    def test_a_jump_of_more_than_one_major_version_is_refused(self):
        ino = self.old_template()   # 2.330.0
        for newest in ('4.0.0', '99999.0.0'):
            with self.subTest(newest):
                self.offer(newest)
                self.server.asked.clear()
                self.assert_kept(self.launch('refresh'), 'more than one major version ahead', ino)
                self.assertEqual(self.downloads(), [])
        self.offer('3.0.0')   # one major: taken
        got = self.launch('refresh')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.version(), '3.0.0\n')

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
        self.write('bin/python3', f'#!/bin/bash\n[[ $3 != *fork-fleet-unpack* ]] || touch {self.path("tar-ran")}\n'
                                  'exec /usr/bin/python3 "$@"\n', 0o755)
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

    def test_the_lock_is_held_during_the_swap_and_not_during_the_last_sync(self):
        """Waiting for the launch lock is not enough: it is still held at the exchange (no start copies mid-swap),
        and no longer at the whole-filesystem flush after it (that need not hold up start)."""
        self.old_template()
        lock, mark = self.path('run/launch.lock'), self.path('saw')
        held = f'if flock -n {lock} true; then echo "$1 free" >> {mark}; else echo "$1 held" >> {mark}; fi\n'
        self.write('bin/python3', '#!/bin/bash\nf() {\n' + held + '}\n'
                   '[[ $3 != *renameat2* ]] || f exchange\nexec /usr/bin/python3 "$@"\n', 0o755)
        self.write('bin/sync', '#!/bin/bash\nf() {\n' + held + '}\n'
                   f'[[ $3 != {self.path("opt")} ]] || f sync\nexec /usr/bin/sync "$@"\n', 0o755)
        got = self.launch('refresh')
        self.assertEqual(got.returncode, 0, got.stderr)
        with open(mark) as f:
            self.assertEqual(f.read().split('\n'), ['exchange held', 'sync free', ''])

    def test_a_curlrc_cannot_turn_off_the_certificate_check(self):
        """curl -q: a root ~/.curlrc with `insecure` (or a proxy) is not read."""
        ino = self.old_template()
        self.write('home/.curlrc', 'insecure\n')
        env = dict(self.env(), HOME=self.path('home'))
        del env['CURL_CA_BUNDLE']   # the fake server's certificate is then unknown: only `insecure` would pass it
        got = subprocess.run(['bash', LAUNCH, 'refresh'], env=env, capture_output=True, text=True, timeout=120)
        self.assert_kept(got, 'could not read the newest release', ino)

    def test_a_redirect_to_plain_http_is_refused(self):
        ino = self.old_template()
        self.server.redirect = f'http://127.0.0.1:{self.server.server_address[1]}/download'
        self.assert_kept(self.launch('refresh'), 'the download of', ino)

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

    def refused_tarball(self, members, word):
        """A tarball with `members` (it passes the sha256 check: a compromised release) is refused: the old template
        stays and nothing is written outside it."""
        victim = self.write('victim', 'SECRET\n', 0o600)
        ino = self.old_template()
        try:
            self.offer(NEW, hostile([(m(victim) if callable(m) else m, data) for m, data in members]))
            got = self.launch('refresh')
            with open(victim) as f:
                self.assertEqual(f.read(), 'SECRET\n')
            self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o600)
            self.assertEqual(sorted(os.listdir(self.d)), ['bin', 'launch.conf', 'opt', 'run', 'victim'])
            self.assert_kept(got, word, ino)
        finally:   # the next case starts from an old template again
            shutil.rmtree(self.template, True)
            for n in os.listdir(self.path('opt')):
                shutil.rmtree(self.path(f'opt/{n}'), True)

    def test_a_tarball_that_reaches_outside_the_template_is_refused(self):
        sym, hard, d = tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE
        cases = {
            # refresh would write its version through it (as root: any file on the host)
            'version file a symlink': ([(lambda v: member('./.fork-fleet-version', sym, link=v), None)],
                                       'has a .fork-fleet-version of its own'),
            'version file': ([(member('./.fork-fleet-version'), b'9.9.9\n')], 'has a .fork-fleet-version of its own'),
            'absolute name': ([(lambda v: member(v), b'x')], 'is an absolute path'),
            'dot-dot name': ([(member('./bin/../../victim'), b'x')], 'has a ".."'),
            'symlink to an absolute path': ([(lambda v: member('./bin/out', sym, link=v), None)], 'leads outside'),
            'symlink up and out': ([(member('./bin/out', sym, link='../../victim'), None)], 'leads outside'),
            'symlink out through a symlink': ([(member('./bin/up', sym, link='..'), None),
                                               (member('./bin/out', sym, link='up/../victim'), None)], 'leads outside'),
            'symlink loop': ([(member('./a', sym, link='b'), None), (member('./b', sym, link='a'), None)],
                             'leads outside'),
            'a file under a symlink': ([(member('./bin/up', sym, link='..'), None), (member('./bin/up/x'), b'x')],
                                       'which is no directory'),
            'a file twice': ([(member('./bin/x'), b'a'), (member('./bin/x', sym, link='/etc'), None)], 'twice'),
            'hard link outside': ([(lambda v: member('./bin/h', hard, link=v), None)], 'is an absolute path'),
            'hard link to a later file': ([(member('./bin/h', hard, link='./bin/later'), None),
                                           (member('./bin/later'), b'x')], 'is not to a file before it'),
            'a configured runner via a symlink': ([(member('./.runner', sym, link='bin/Runner.Listener'), None)],
                                                  'has a .runner'),
            'a FIFO': ([(member('./bin/node', tarfile.FIFOTYPE, 0o666), None)], 'is a FIFO file'),
            'a device': ([(member('./bin/disk', tarfile.BLKTYPE, 0o644), None)], 'is a device file'),
            'a dir in place of a file': ([(member('./run.sh', d, 0o755), None)], 'twice'),
        }
        for name, (members, word) in cases.items():
            with self.subTest(name):
                self.refused_tarball(members, word)

    def test_modes_are_cleaned_and_safe_links_kept(self):
        """setuid, setgid, sticky and group/other write go (root's tar would keep them); links inside stay."""
        sym = tarfile.SYMTYPE
        self.offer(NEW, hostile([
            (member('./bin/su-helper', mode=0o6755), b'#!/bin/sh\n'),
            (member('./bin/open', mode=0o777), b'x'),
            (member('./bin/group', mode=0o664), b'x'),
            (member('./wide/', tarfile.DIRTYPE, 0o1777), None),
            (member('./shut/', tarfile.DIRTYPE, 0o500), None),
            (member('./shut/f'), b'x'),
            (member('./externals/node20/lib/node_modules/npm/bin/npm-cli.js', mode=0o755), b'x'),
            (member('./externals/node20/bin/npm', sym, link='../lib/node_modules/npm/bin/npm-cli.js'), None),
            (member('./bin/same', tarfile.LNKTYPE, link='./bin/Runner.Listener'), None),
        ]))
        got = self.launch('refresh')
        self.assertEqual(got.returncode, 0, got.stderr)
        mode = lambda rel: stat.S_IMODE(os.lstat(f'{self.template}/{rel}').st_mode)
        self.assertEqual([mode(r) for r in ('bin/su-helper', 'bin/open', 'bin/group', 'wide', 'shut', 'shut/f')],
                         [0o755, 0o755, 0o644, 0o755, 0o755, 0o644])
        self.assertEqual(os.readlink(f'{self.template}/externals/node20/bin/npm'),
                         '../lib/node_modules/npm/bin/npm-cli.js')
        self.assertTrue(os.path.samefile(f'{self.template}/bin/same', f'{self.template}/bin/Runner.Listener'))
        self.assertEqual(self.launch('ready', NEW).returncode, 0)

    def test_the_unpacker_caps_entries_and_size(self):
        """MAX_ENTRIES and MAX_BYTES (a gzip bomb in a compromised release would fill the jobs' filesystem): the
        launcher's own unpacker, with small caps."""
        with open(LAUNCH) as f:
            code = f.read().split("read -r -d '' UNPACK_PY <<'EOF' || true\n", 1)[1].split('\nEOF\n', 1)[0]
        self.assertIn('MAX_ENTRIES, MAX_BYTES = 100000, 4 << 30\n', code)
        code = code.replace('MAX_ENTRIES, MAX_BYTES = 100000, 4 << 30\n', 'MAX_ENTRIES, MAX_BYTES = 8, 100\n')
        for name, members, word in (('entries', [(member(f'./f{i}'), b'') for i in range(5)], 'more than 8 entries'),
                                    ('bytes', [(member('./big'), b'x' * 101)], 'more than 100 bytes'),
                                    ('fits', [(member('./f'), b'x' * 50)], None)):
            with self.subTest(name):
                src, dest = self.write('t.tar.gz', ''), self.path(f'new-{name}')
                with open(src, 'wb') as f:
                    f.write(hostile(members))
                os.mkdir(dest)
                got = subprocess.run(['python3', '-I', '-c', code, src, dest, NEW], capture_output=True, text=True,
                                     timeout=60)
                if word:
                    self.assertNotEqual(got.returncode, 0)
                    self.assertIn(word, got.stderr)
                else:
                    self.assertEqual(got.returncode, 0, got.stderr)

    def test_start_refuses_what_refresh_refuses(self):
        """Defence in depth: start's own check of the template (a template made by hand, or an older refresh)."""
        self.old_template()
        bad = self.path('opt/runner-template/bin/bad')
        for name, make, word in (('FIFO', lambda: os.mkfifo(bad, 0o644), 'a special file'),
                                 ('setuid', lambda: self.write('opt/runner-template/bin/bad', 'x', 0o4755), 'setuid'),
                                 ('setgid', lambda: self.write('opt/runner-template/bin/bad', 'x', 0o2755), 'setuid'),
                                 ('world-writable', lambda: self.write('opt/runner-template/bin/bad', 'x', 0o646),
                                  'others may write')):
            with self.subTest(name):
                make()
                try:
                    got = subprocess.run(['bash', LAUNCH, 'start', 'fleet-vm-t3code-1'], env=self.env(),
                                         input='ACTIONS_RUNNER_INPUT_JITCONFIG=abc\nFORK_FLEET_RUN=1\n'
                                               'FORK_FLEET_ATTEMPT=1\n', capture_output=True, text=True, timeout=60)
                finally:
                    os.remove(bad)   # before the next case (a FIFO left would block its write)
                self.assertNotEqual(got.returncode, 0)
                self.assertIn(word, got.stderr)
                self.assertIn('bin/bad', got.stderr)

    def test_a_real_run_in_the_services_sandbox(self):
        """The service's sandbox (as a transient user unit) lets a real refresh through. Only the places differ: the
        test's folder in place of /opt/fork-fleet and /run/fork-fleet, and so ProtectHome and PrivateTmp (they would
        hide it), and the capability bound (a user manager cannot set it). Skipped where the user manager applies no
        sandbox (no user namespaces): there the root proof in the runbook (host step 7) covers it."""
        if not shutil.which('systemd-run'):
            self.skipTest('no systemd-run')
        probe = subprocess.run(['systemd-run', '--user', '--wait', '--pipe', '--quiet', '--collect',
                                '-p', 'ProtectSystem=strict', '--', 'sh', '-c', 'touch /usr/.fork-fleet-probe'],
                               capture_output=True, text=True, timeout=60)
        if 'Read-only file system' not in probe.stderr:
            self.skipTest('this user manager applies no sandbox (no user namespaces, or no user manager)')
        self.old_template()
        skip = {'Type', 'ExecStart', 'ReadWritePaths', 'RuntimeDirectory', 'RuntimeDirectoryMode',
                'RuntimeDirectoryPreserve', 'ProtectHome', 'PrivateTmp', 'CapabilityBoundingSet', 'TimeoutStartSec'}
        props = [f'{k}={v}' for k, v in unit('fork-fleet-refresh.service')['Service'].items() if k not in skip]
        cmd = ['systemd-run', '--user', '--wait', '--pipe', '--quiet', '--collect', '-p', f'ReadWritePaths={self.d}']
        for prop in props:
            cmd += ['-p', prop]
        for k, v in self.env().items():
            cmd += ['--setenv', f'{k}={v}']
        got = subprocess.run(cmd + ['--', 'bash', LAUNCH, 'refresh'], capture_output=True, text=True, timeout=300)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertEqual(self.version(), f'{NEW}\n')

    def test_usage_names_refresh(self):
        self.assertIn('| refresh |', self.launch().stderr)
        self.assertIn('usage', self.launch('refresh', 'extra').stderr)


def unit(name):
    p = configparser.ConfigParser(strict=False, interpolation=None)
    p.optionxform = str
    p.read(os.path.join(HERE, name))
    return p


# The service's sandbox: it may write the template's folder and its locks only, make no device, setuid or setgid file
# and reach the network by IP only (a compromised release unpacked by a bug in refresh still cannot reach the host).
SANDBOX = {'ProtectSystem': 'strict', 'ReadWritePaths': '/opt/fork-fleet', 'RuntimeDirectory': 'fork-fleet',
           'RuntimeDirectoryMode': '0700', 'RuntimeDirectoryPreserve': 'yes', 'ProtectHome': 'yes',
           'PrivateTmp': 'yes', 'PrivateDevices': 'yes', 'NoNewPrivileges': 'yes', 'RestrictSUIDSGID': 'yes',
           'RestrictAddressFamilies': 'AF_INET AF_INET6 AF_UNIX', 'CapabilityBoundingSet': 'CAP_DAC_OVERRIDE'}


class Timer(unittest.TestCase):
    """fork-fleet-refresh.service and .timer: hourly with jitter, catching up after a missed run, one refresh each,
    sandboxed."""

    def test_the_units(self):
        service, timer = unit('fork-fleet-refresh.service'), unit('fork-fleet-refresh.timer')
        self.assertEqual(service['Service']['Type'], 'oneshot')
        self.assertEqual(service['Service']['ExecStart'], '/opt/fork-fleet/fork-fleet-launch.sh refresh')
        # Hourly: the dispatcher closes the pool once it reads (hourly) a newer release, and a failed run (no network
        # yet at boot, a GitHub error) is tried again within the hour, not the next day.
        self.assertEqual(timer['Timer']['OnCalendar'], 'hourly')
        self.assertEqual(timer['Timer']['RandomizedDelaySec'], '10min')
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

    def test_the_service_is_sandboxed(self):
        service = unit('fork-fleet-refresh.service')['Service']
        self.assertEqual({k: service.get(k) for k in SANDBOX}, SANDBOX)
        # RUN_DIR is the dispatcher's lock folder too: the unit must never remove it when it stops.
        self.assertEqual(service['RuntimeDirectoryPreserve'], 'yes')

    def test_systemd_rates_the_sandbox_ok(self):
        """systemd-analyze security, offline (reads the file only): exposure 4.0 or less, "OK"."""
        if not shutil.which('systemd-analyze'):
            self.skipTest('no systemd-analyze')
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(HERE, 'fork-fleet-refresh.service')) as f:
            text = f.read().replace('/opt/fork-fleet/fork-fleet-launch.sh', LAUNCH)
        path = os.path.join(d, 't3code-fleet-refresh.service')
        with open(path, 'w') as f:
            f.write(text)
        got = subprocess.run(['systemd-analyze', 'security', '--offline=true', '--no-pager', path],
                             capture_output=True, text=True, timeout=60)
        m = re.search(r'Overall exposure level for \S+: ([0-9.]+)', got.stdout)
        if not m:
            self.skipTest(f'this systemd-analyze has no offline security check: {got.stderr.strip()[:200]}')
        self.assertLessEqual(float(m.group(1)), 4.0, got.stdout[-2000:])


if __name__ == '__main__':
    unittest.main()
