#!/usr/bin/env python3
"""Tests of fork-fleet-launch.sh refresh (the runner template's refresh) and its timer, fork-fleet-refresh.timer.
A fake https release server on 127.0.0.1 (a certificate made here) and temp folders only: nothing is downloaded from
GitHub, nothing runs as root. Run: python3 <this>. Linux only; needs curl, openssl, tar and flock."""
import configparser, hashlib, http.server, io, json, os, platform, re, resource, shutil, signal, ssl, stat, subprocess
import sys, tarfile, tempfile, threading, time, unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCH = os.path.join(HERE, 'fork-fleet-launch.sh')
OLD, NEW = '2.330.0', '2.331.0'
PLATFORM = {'x86_64': 'linux-x64', 'aarch64': 'linux-arm64', 'arm64': 'linux-arm64',
            'armv7l': 'linux-arm'}.get(platform.machine())


def hostile(members, version=None, fmt=tarfile.DEFAULT_FORMAT, glob=None):
    """A runner-like tarball (gzip) with extra tarfile members: (TarInfo, data or None) pairs; `glob`, the keys of a
    global pax header at its start."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz', format=fmt, pax_headers=glob) as t:
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


def pax(keys):
    """The data of a pax header holding `keys`: one "<length> <key>=<value>\\n" record each."""
    out = b''
    for key, value in keys.items():
        rec = f' {key}={value}\n'.encode()
        n = len(rec)
        while n != len(rec) + len(str(n)):
            n = len(rec) + len(str(n))
        out += str(n).encode() + rec
    return out


def unpack_code():
    """The launcher's own unpacker (UNPACK_PY)."""
    with open(LAUNCH) as f:
        return f.read().split("read -r -d '' UNPACK_PY <<'EOF' || true\n", 1)[1].split('\nEOF\n', 1)[0]


def member(name, kind=tarfile.REGTYPE, mode=0o644, link=''):
    info = tarfile.TarInfo(name)
    info.type, info.mode, info.linkname = kind, mode, link
    return info


def tarball(version, extra=None):
    """An actions/runner-like release tarball (gzip): run.sh, bin/Runner.Listener and a file naming its version."""
    return layout({'run.sh': (b'#!/bin/sh\n', 0o755), 'bin/Runner.Listener': (b'x', 0o755),
                   'bin/version.txt': (version.encode(), 0o644), **(extra or {})})


def layout(files):
    """A tarball (gzip) of ./, ./bin/ and `files` ({name: (data, mode)})."""
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
                        '-days', '1', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost'],
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

    def floor(self):
        with open(f'{self.template}/.fork-fleet-floor') as f:
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
        """A release GitHub withdrew (say a compromised v99999.0.0) must not stay, nor make every later one older:
        back to its floor (the version it replaced), or without a floor file one major version down."""
        for have, floor in (('2.331.1', None), ('3.0.0', None), ('99999.0.0', '2.330.0'), ('2.331.1', NEW)):
            with self.subTest(have=have, floor=floor):
                self.old_template()
                self.write('opt/runner-template/.fork-fleet-version', f'{have}\n')
                if floor:
                    self.write('opt/runner-template/.fork-fleet-floor', f'{floor}\n')
                got = self.launch('refresh')
                try:
                    self.assertEqual(got.returncode, 0, got.stderr)
                    self.assertIn(f'newer than the newest release {NEW}', got.stderr)
                    self.assertIn(f'rolling it back to {NEW}', got.stderr)
                    self.assertEqual(self.version(), f'{NEW}\n')
                    self.assertEqual(self.floor(), f'{NEW}\n')   # no second step back below it
                finally:
                    shutil.rmtree(self.template, True)

    def test_a_rollback_never_goes_below_the_floor(self):
        """An old release marked "latest" by mistake (or by someone with write access) is not installed by itself:
        not below the version the template replaced, not more than one major version down without a floor file, never
        below RUNNER_MIN (first templates too)."""
        ino = self.old_template()   # 2.330.0, no floor file
        got = self.launch('refresh')   # forward to 2.331.0: its floor is 2.330.0
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual((self.version(), self.floor()), (f'{NEW}\n', f'{OLD}\n'))
        self.offer(OLD)   # 2.331.0 withdrawn: back to 2.330.0, the floor
        got = self.launch('refresh')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual((self.version(), self.floor()), (f'{OLD}\n', f'{OLD}\n'))
        ino = os.stat(self.template).st_ino
        for newest in ('2.329.9', '1.0.0'):   # below the floor
            with self.subTest(newest):
                self.offer(newest)
                self.server.asked.clear()
                self.assert_kept(self.launch('refresh'), f'older than {OLD}, the oldest it may go back to', ino)
                self.assertEqual(self.downloads(), [])
        os.remove(f'{self.template}/.fork-fleet-floor')   # an older template: one major version down at most
        self.write('opt/runner-template/.fork-fleet-version', '3.0.0\n')
        self.offer('1.999.0')
        got = self.launch('refresh')
        self.assertNotEqual(got.returncode, 0)
        self.assertIn('older than 2.0.0, the oldest it may go back to', got.stderr)
        self.write('opt/runner-template/.fork-fleet-version', f'{OLD}\n')
        self.conf(RUNNER_MIN='2.332.0')   # RUNNER_MIN: never below it, forward or back
        self.offer(NEW)
        self.assert_kept(self.launch('refresh'), 'older than RUNNER_MIN 2.332.0', ino)
        shutil.rmtree(self.template)
        got = self.launch('refresh')
        self.assertNotEqual(got.returncode, 0)
        self.assertIn('older than RUNNER_MIN 2.332.0', got.stderr)
        self.assertFalse(os.path.exists(self.template))
        self.conf(RUNNER_MIN=NEW)
        got = self.launch('refresh')   # a first template: taken, and no floor (nothing it replaced)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.version(), f'{NEW}\n')
        self.assertFalse(os.path.exists(f'{self.template}/.fork-fleet-floor'))

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
        no_runner = 'has no run.sh or bin/Runner.Listener'
        for name, data, word in (('not a tarball', b'not gzip', 'did not unpack'),
                                 ('configured', tarball(NEW, {'.runner': (b'{}', 0o644)}), 'has a .runner'),
                                 ('no run.sh', layout({'bin/Runner.Listener': (b'x', 0o755)}), no_runner),
                                 ('no Listener', layout({'run.sh': (b'#!/bin/sh\n', 0o755)}), no_runner),
                                 ('run.sh not executable', layout({'run.sh': (b'#!/bin/sh\n', 0o644),
                                                                   'bin/Runner.Listener': (b'x', 0o755)}), no_runner)):
            with self.subTest(name):
                ino = self.old_template()
                self.offer(NEW, data)
                self.assert_kept(self.launch('refresh'), word, ino)
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

    def run_dir_read_only(self, lock=True):
        """RUN_DIR as the sandboxed service sees it: read-only (here: no write bit), with or without the launch lock
        the dispatcher's prune made."""
        run = self.path('run')
        os.makedirs(run, 0o700)
        if lock:
            self.write('run/launch.lock', '')
        os.chmod(run, 0o500)
        self.addCleanup(lambda: os.path.isdir(run) and os.chmod(run, 0o700))
        return run

    def test_refresh_writes_nothing_in_run_dir(self):
        """The service cannot write RUN_DIR (the root launcher's lock and the jobs' environment files are there):
        refresh takes its own lock on the template's folder and only opens the launch lock."""
        self.old_template()
        run = self.run_dir_read_only()
        got = self.launch('refresh')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.version(), f'{NEW}\n')
        self.assertEqual(os.listdir(run), ['launch.lock'])

    def test_refresh_without_a_launch_lock_it_may_not_make_waits_for_the_dispatcher(self):
        """Right after a boot, before the dispatcher's first prune: refused before any download, template kept."""
        ino = self.old_template()
        self.run_dir_read_only(lock=False)
        self.assert_kept(self.launch('refresh'), "the dispatcher's next prune or start makes it", ino)
        self.assertEqual(self.server.asked, [])
        os.chmod(self.path('run'), 0o700)
        os.rmdir(self.path('run'))
        os.chmod(self.path('opt'), 0o555)   # no RUN_DIR at all, and none can be made
        self.addCleanup(os.chmod, self.path('opt'), 0o755)
        self.conf(RUN_DIR=self.path('opt/run'))
        self.assert_kept(self.launch('refresh'), "the dispatcher's next prune or start makes it", ino)

    def test_a_link_planted_at_the_launch_lock_is_refused(self):
        """Root's refresh by hand never writes through a link a taken-over service could once plant in RUN_DIR."""
        ino = self.old_template()
        os.makedirs(self.path('run'), 0o700)
        target = self.write('hook.sh', '#!/bin/sh\nexec gate\n', 0o755)
        for to in (target, self.path('made-by-root')):
            with self.subTest(to=to):
                os.symlink(to, self.path('run/launch.lock'))
                try:
                    self.assert_kept(self.launch('refresh'), 'is a symlink or not a plain file', ino)
                finally:
                    os.remove(self.path('run/launch.lock'))
                with open(target) as f:
                    self.assertEqual(f.read(), '#!/bin/sh\nexec gate\n')
                self.assertFalse(os.path.lexists(self.path('made-by-root')))

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
                ('template with a space', dict(TEMPLATE=self.path('opt/a b')), 'not a setting'),
                ('template in the root', dict(TEMPLATE='/runner-template'), 'not a plain absolute path in a folder'),
                ('RUNNER_MIN not a version', dict(RUNNER_MIN='2.x'), 'RUNNER_MIN is not a version')):
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
        os.mkdir(self.path('links'), 0o755)
        link = self.path('links/link')
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
            # the floor decides how far back a later refresh may roll: the release must not set it
            'floor file': ([(member('./.fork-fleet-floor'), b'0.0.1\n')], 'has a .fork-fleet-floor of its own'),
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
                got = subprocess.run(['python3', '-I', '-c', code, src, dest, NEW, ''], capture_output=True,
                                     text=True, timeout=60)
                if word:
                    self.assertNotEqual(got.returncode, 0)
                    self.assertIn(word, got.stderr)
                else:
                    self.assertEqual(got.returncode, 0, got.stderr)

    def test_the_unpacker_bounds_memory_and_time(self):
        """A compromised release cannot hold root's refresh for minutes or take the host's memory: fanned-out
        symlinks (link k goes through link k-1 four times: 4**k steps unbounded), a GNU long-name or pax header of
        1 MiB (tarfile would read it whole, however big), a sparse file of any format (its map is read into memory),
        a name over 4096, too many bytes of long names or pax headers in all (empty ones too), too many bytes of names
        and link targets in all (as kept in memory), a global pax header of too many keys or with a key tarfile sets
        on each entry, a size the data does not have (each file after it would decompress the tarball again), a file
        or special file over MAX_BYTES, headers in a row deeper than tarfile can follow. Each is refused quickly,
        under a 192 MiB address-space limit; at each budget's exact limit the tarball still gets through."""
        sym, xhd, xgl = tarfile.SYMTYPE, tarfile.XHDTYPE, tarfile.XGLTYPE
        budget = 64 << 20   # the launcher's MAX_NAMES (its MAX_GLOBAL is 64)
        fan = []
        for k in range(30):
            fan.append(member(f'./l{k}', sym, link='bin' if k == 0 else '/../'.join([f'l{k - 1}'] * 4)))

        def with_pax(m, keys):
            m.pax_headers = keys
            return m

        def names(over, wide=False):
            """`budget` (+ `over`) bytes of names and link targets, 7000 entries of them in pax headers (64512000
            bytes counted as headers: under the header budget) and the rest in ustar headers, then a device file: at
            the limit the read loop reaches the device file, which is what is refused. `wide`: 3000 such entries
            instead, each link target with one 4-byte character, so all its 4096 characters take 4 bytes in memory
            (its header has 4099 bytes of it)."""
            n, link = (3000, '\U0001F600' + 'b/' * 2047 + 'b') if wide else (7000, 'b/' * 2048)
            out = [(member(f'./{i:05d}'.ljust(4096, 'a'), sym, link=link), None) for i in range(n)]
            base = sum(len(m.name) + len(m.linkname) for m in tarfile.open(fileobj=io.BytesIO(hostile([]))))
            k, r = divmod(budget - base - len('./dev') - n * (4096 + len(link) * (4 if wide else 1)) - len('./z'), 200)
            out += [(member(f'./{i:06d}'.ljust(100, 'c'), sym, link='d' * 100), None) for i in range(k)]
            out += [(member('./z', sym, link='d' * (r + over)), None), (member('./dev', tarfile.CHRTYPE), None)]
            return hostile(out, fmt=tarfile.PAX_FORMAT)

        def past(local=None, glob=None, n=20):
            """A 64 MiB file of zeros, then `n` empty files, then 256 KiB of zeros (so a file read past its data
            does not reach the end). `local`: the pax keys of each empty file (the first one's with GNU.sparse.realsize
            0, so it would count as 0 bytes); `glob`: a global pax header's. A size its data does not have makes every
            file after it seek back, and gzip seeks back by decompressing from the start again: about 0.1 s each here,
            days from a 1 GiB download with 100000 entries."""
            first = with_pax(member('./bin/filler'), {'GNU.sparse.realsize': '0'} if local else {})
            files = [(with_pax(member(f'./bin/f{i}'), dict(local or {})), b'') for i in range(n)]
            return hostile([(first, bytes(64 << 20))] + files + [(member('./bin/tail'), bytes(256 << 10))],
                           fmt=tarfile.PAX_FORMAT, glob=glob)

        def unwritten(m, size):
            """`m` with a header of `size` bytes and no data after it (the stream ends first)."""
            m.size = size
            return m

        # 1024 pax headers of 64915 bytes: each 65536 bytes read (its own block and 127 of data), MAX_NAMES in all.
        at_limit = [(with_pax(member(f'./bin/c{i}'), {'comment': 'x' * 64900}), b'') for i in range(1024)]
        cases = {
            'fanned-out symlinks': (hostile([(m, None) for m in fan]), 'steps to follow'),
            'GNU long name': (hostile([(member('./bin/' + 'a' * (1 << 20)), b'')], fmt=tarfile.GNU_FORMAT),
                              'a header of'),
            'pax header': (hostile([(member('./bin/' + 'a' * (1 << 20)), b'')], fmt=tarfile.PAX_FORMAT),
                           'a header of'),
            'sparse file': (hostile([(member('./bin/s', tarfile.GNUTYPE_SPARSE), b'')], fmt=tarfile.GNU_FORMAT),
                            'is a sparse file'),
            # Pax sparse 1.0: a map of 20 million pairs in the entry's data, read 512 bytes at a time into a list
            # (640 MB of numbers from about 80 KB of gzip) before any other check.
            'pax sparse 1.0': (hostile([(with_pax(member('./bin/s'), {'GNU.sparse.major': '1',
                                                                      'GNU.sparse.minor': '0'}),
                                         b'20000000\n' + b'0\n' * 40000000)], fmt=tarfile.PAX_FORMAT),
                               'a pax sparse file'),
            # Pax sparse 0.1: maps under every budget (64 MB of pax headers) become lists of tuples ~17 times bigger.
            'pax sparse 0.1': (hostile([(with_pax(member(f'./bin/s{i}'), {'GNU.sparse.map': ','.join(['0'] * 32000)}),
                                         b'') for i in range(1000)], fmt=tarfile.PAX_FORMAT), 'a pax sparse file'),
            'pax sparse 0.0': (hostile([(with_pax(member('./bin/s'), {'GNU.sparse.size': '0', 'GNU.sparse.offset': '0',
                                                                      'GNU.sparse.numbytes': '0'}), b'')],
                                       fmt=tarfile.PAX_FORMAT), 'a pax sparse file'),
            'long name': (hostile([(member('./bin/' + 'a' * 5000), b'')], fmt=tarfile.PAX_FORMAT),
                          'longer than 4096'),
            'long link target': (hostile([(member('./bin/l', sym, link='a/' * 2500), None)], fmt=tarfile.PAX_FORMAT),
                                 'longer than 4096'),
            # Each pax header under 64 KiB, but 4000 of them: ~240 MB of names, unless each is checked as it is read
            # (a refresh by hand has no MemoryMax).
            'many long names': (hostile([(member(f'./{i:05d}' + 'a' * 60000), b'') for i in range(4000)],
                                        fmt=tarfile.PAX_FORMAT), 'longer than 4096'),
            # Each name and target under 4096, but 30000 symlinks: ~240 MB of names from a small download, unless
            # all the long-name and pax headers together are counted (MAX_NAMES) as they are read. The GNU format is
            # a runner release's own (its long names come in GNU long-name headers, its link targets in long-link
            # ones); both kinds are counted, or the names total below refuses it first.
            'many long-name symlinks': (hostile([(member(f'./{i:05d}' + 'a' * 4000, sym, link='b/' * 2000), None)
                                                 for i in range(30000)], fmt=tarfile.PAX_FORMAT),
                                        'bytes of long names and pax headers'),
            'many long-name symlinks, GNU format': (
                hostile([(member(f'./{i:05d}' + 'a' * 4000, sym, link='b/' * 2000), None) for i in range(30000)],
                        fmt=tarfile.GNU_FORMAT), 'bytes of long names and pax headers'),
            # Global headers count too: 1100 of 61 KB (one key each, under MAX_GLOBAL), each before an entry.
            'many global headers': (
                hostile([x for i in range(1100) for x in ((member('./g', xgl), pax({'comment': 'x' * 61000})),
                                                          (member(f'./bin/e{i}'), b''))], fmt=tarfile.PAX_FORMAT),
                'bytes of long names and pax headers'),
            # Empty headers cost their block: 1100 entries after 120 empty pax headers each (132000 headers) would
            # all be parsed for nothing, unless the blocks read are counted rather than the sizes.
            'many empty headers': (
                hostile([x for i in range(1100) for x in [(member('./x', xhd), b'')] * 120 + [(member(f'./bin/e{i}'),
                                                                                                b'')]],
                        fmt=tarfile.PAX_FORMAT), 'bytes of long names and pax headers'),
            'headers of MAX_NAMES bytes': (hostile(at_limit, fmt=tarfile.PAX_FORMAT), None),
            'headers of one block more': (hostile(at_limit + [(member('./x', xhd), b''), (member('./bin/z'), b'')],
                                                  fmt=tarfile.PAX_FORMAT), 'bytes of long names and pax headers'),
            # More headers in a row than tarfile can follow (it recurses on each): refused, not a traceback.
            'a chain of headers': (hostile([(member('./x', xhd), b'')] * 300 + [(member('./bin/z'), b'')],
                                           fmt=tarfile.PAX_FORMAT), 'more headers in a row'),
            # Names and link targets of every entry, in all: the global path below, or plain ustar names on top of
            # long ones, would get past the header budget.
            'names of MAX_NAMES bytes': (names(0), 'is a device file'),
            'names of one byte more': (names(1), 'bytes of names and link targets'),
            # Global pax keys are copied into every entry read after them: 6000 keys (one 60 KiB header) and 100000
            # entries would be ~30 GB, or 600 million steps without those copies kept.
            'a global pax header of many keys': (
                hostile([(member(f'./bin/{i}'), None) for i in range(100000)], fmt=tarfile.PAX_FORMAT,
                        glob={f'k{i}': '' for i in range(6000)}), 'global pax keys'),
            'a global pax header of MAX_GLOBAL keys': (
                hostile([(member(f'./bin/{i}'), b'') for i in range(3)], fmt=tarfile.PAX_FORMAT,
                        glob={f'k{i}': '' for i in range(64)}), None),
            'a global pax header of one key more': (
                hostile([(member(f'./bin/{i}'), b'') for i in range(3)], fmt=tarfile.PAX_FORMAT,
                        glob={f'k{i}': '' for i in range(65)}), 'global pax keys'),
            # Under that cap (a runner release has none; git archive writes one), each entry's copy of them is
            # dropped once read: 100001 copies of 60 keys would be ~230 MB before the entry count refuses.
            'global pax keys on each entry': (
                hostile([(member(f'./bin/{i}'), None) for i in range(100001)], fmt=tarfile.PAX_FORMAT,
                        glob={f'k{i}': '' for i in range(60)}), 'more than 100000 entries'),
            # A global path ending in "/" is a new 4000-byte copy in each of 100000 entries (~400 MB), from one key.
            'a global pax path': (hostile([(member(f'./bin/{i}'), None) for i in range(100000)],
                                          fmt=tarfile.PAX_FORMAT, glob={'path': 'a' * 4000 + '/'}),
                                  'a global pax header with path'),
            'a global pax linkpath': (hostile([(member('./bin/l', sym), None)], fmt=tarfile.PAX_FORMAT,
                                              glob={'linkpath': 'a'}), 'a global pax header with linkpath'),
            'a global pax sparse name': (hostile([(member('./bin/s'), b'')], fmt=tarfile.PAX_FORMAT,
                                                 glob={'GNU.sparse.name': 'a'}),
                                         'a global pax header with GNU.sparse.name'),
            # Any key tarfile sets on each entry: a uid of 4300 digits is a new int of ~1.9 KB in each of 100000
            # entries (~190 MB), from a 470 KB download.
            'a global pax uid': (hostile([(member(f'./bin/{i}', tarfile.DIRTYPE), None) for i in range(99990)],
                                         fmt=tarfile.PAX_FORMAT, glob={'uid': '9' * 4300}),
                                 'a global pax header with uid'),
            'a global pax gid': (hostile([(member('./bin/g'), b'')], fmt=tarfile.PAX_FORMAT, glob={'gid': '0'}),
                                 'a global pax header with gid'),
            # A size the data does not have (see past): a global size, or GNU.sparse.realsize, global or on an entry.
            'a global pax size': (past(glob={'size': '200000'}), 'a global pax header with size'),
            'a global pax GNU.sparse.realsize': (past(glob={'GNU.sparse.realsize': '200000'}),
                                                 'a global pax header with GNU.sparse.realsize'),
            'a pax realsize past the data': (past({'GNU.sparse.realsize': '200000'}),
                                             "'./bin/filler' has a size its data does not have"),
            'a pax realsize just past the buffer': (past({'GNU.sparse.realsize': '10000'}),
                                                    "'./bin/filler' has a size its data does not have"),
            # Even one that matches the data (so the offset check passes): a runner release has none.
            'a pax realsize': (hostile([(with_pax(member('./bin/r'), {'GNU.sparse.realsize': '1'}), b'x')],
                                       fmt=tarfile.PAX_FORMAT), 'has a pax GNU.sparse.realsize'),
            # int() refuses more than 4300 digits with a ValueError: refused, not a traceback.
            'a realsize of 5000 digits': (hostile([(with_pax(member('./bin/r'), {'GNU.sparse.realsize': '9' * 5000}),
                                                    b'')], fmt=tarfile.PAX_FORMAT), 'not a gzip tarball'),
            # Refused at its header, before its data is decompressed (here the stream ends first, so a check after
            # the read loop would never see it).
            'a file over MAX_BYTES': (hostile([(unwritten(member('./bin/big'), 5 << 30), None)]),
                                      'more than 4294967296 bytes of files'),
            'a special file over MAX_BYTES': (hostile([(unwritten(member('./bin/odd', b'Z'), 5 << 30), None)]),
                                              "'./bin/odd' is a special file"),
            # The names total counts what a name takes in memory: one 4-byte character makes the whole str 4 bytes a
            # character (and costs its header 3 bytes more).
            'wide names of MAX_NAMES bytes': (names(0, wide=True), 'is a device file'),
            'wide names of one byte more': (names(1, wide=True), 'bytes of names and link targets'),
        }
        # Refused at once (a fraction of a second of CPU), not after the work they would cause.
        quick = {'a global pax size', 'a global pax GNU.sparse.realsize', 'a pax realsize past the data',
                 'a pax realsize just past the buffer', 'a file over MAX_BYTES', 'a special file over MAX_BYTES'}
        limit = lambda: resource.setrlimit(resource.RLIMIT_AS, (192 << 20, 192 << 20))
        for name, (data, word) in cases.items():
            with self.subTest(name):
                src, dest = self.write('t.tar.gz', ''), self.path(f'new-{name.replace(" ", "-")}')
                with open(src, 'wb') as f:
                    f.write(data)
                os.mkdir(dest)
                t, cpu = time.monotonic(), resource.getrusage(resource.RUSAGE_CHILDREN)
                got = subprocess.run(['python3', '-I', '-c', unpack_code(), src, dest, NEW, ''], capture_output=True,
                                     text=True, timeout=60, preexec_fn=limit)
                self.assertNotIn('Traceback', got.stderr)
                self.assertNotIn('MemoryError', got.stderr)
                self.assertLess(time.monotonic() - t, 20)
                if name in quick:   # CPU time, not wall clock: a loaded machine does not make it flaky
                    now = resource.getrusage(resource.RUSAGE_CHILDREN)
                    self.assertLess(now.ru_utime + now.ru_stime - cpu.ru_utime - cpu.ru_stime, 0.5, got.stderr)
                if word is None:
                    self.assertEqual(got.returncode, 0, got.stderr)
                    self.assertIn('run.sh', os.listdir(dest))
                else:
                    self.assertNotEqual(got.returncode, 0)
                    self.assertIn(word, got.stderr)
                    self.assertEqual(os.listdir(dest), [])

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

    def test_start_refuses_a_configured_template_even_as_a_dangling_link(self):
        """start tests .env, .runner and the like as refresh does: a symlink to nothing counts too."""
        self.old_template()
        for f in ('.runner', '.env', '.credentials'):
            with self.subTest(f):
                os.symlink('/nonexistent/x', f'{self.template}/{f}')
                try:
                    got = subprocess.run(['bash', LAUNCH, 'start', 'fleet-vm-t3code-1'], env=self.env(),
                                         input='ACTIONS_RUNNER_INPUT_JITCONFIG=abc\nFORK_FLEET_RUN=1\n'
                                               'FORK_FLEET_ATTEMPT=1\n', capture_output=True, text=True, timeout=60)
                finally:
                    os.remove(f'{self.template}/{f}')
                self.assertNotEqual(got.returncode, 0)
                self.assertIn(f'{self.template}/{f} exists', got.stderr)

    def test_a_template_in_a_shared_folder_is_refused_until_moved(self):
        """The service may write the template's own folder only. A template set up before (beside the launcher, the
        trust config and the job hook, which root runs) is refused by refresh, before any download, and keeps
        working for ready and start; the runbook's move (host step 7) makes refresh work again."""
        ino = self.old_template()
        for f in ('fork-fleet-launch.sh', 'hook.sh', 'fork-fleet.json'):
            self.write(f'opt/{f}', 'root runs this\n', 0o755)
        self.offer(OLD)
        self.assertEqual(self.launch('ready', OLD).returncode, 0)
        self.offer(NEW)
        got = self.launch('refresh')
        self.assertNotEqual(got.returncode, 0)
        self.assertIn('not only the template: the template must have a folder of its own', got.stderr)
        self.assertIn('host step 7', got.stderr)
        self.assertEqual(self.server.asked, [])
        self.assertEqual(os.stat(self.template).st_ino, ino)
        self.assertEqual(sorted(os.listdir(self.path('opt'))),
                         ['fork-fleet-launch.sh', 'fork-fleet.json', 'hook.sh', 'runner-template'])
        # The move, as the runbook gives it: a folder of its own, the template into it, launch.conf after.
        os.mkdir(self.path('opt/template'), 0o755)
        os.rename(self.template, self.path('opt/template/runner-template'))
        self.template = self.path('opt/template/runner-template')
        self.conf()
        got = self.launch('refresh')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.version(), f'{NEW}\n')
        self.assertEqual(sorted(os.listdir(self.path('opt/template'))), ['runner-template'])

    def test_the_templates_folder_is_made_on_the_first_run(self):
        self.template = self.path('opt/fresh/runner-template')
        self.conf()
        got = self.launch('refresh')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(self.path('opt/fresh')).st_mode), 0o755)
        self.assertEqual(self.version(), f'{NEW}\n')
        self.template = self.path('opt/missing/deeper/runner-template')   # only the template's own folder is made
        self.conf()
        got = self.launch('refresh')
        self.assertNotEqual(got.returncode, 0)
        self.assertIn('missing or a symlink', got.stderr)

    def test_refresh_needs_no_unix_socket(self):
        """The service's RestrictAddressFamilies (no AF_UNIX: no way to ask systemd or the bus for a unit) and its
        limits, for real, as a user unit (seccomp needs no root): a refresh that resolves a name still works."""
        if not shutil.which('systemd-run'):
            self.skipTest('no systemd-run')
        service = unit('fork-fleet-refresh.service')['Service']
        props = [f'{k}={service[k]}' for k in ('RestrictAddressFamilies', 'SystemCallArchitectures', 'TasksMax',
                                                 'MemoryMax', 'CPUQuota', 'NoNewPrivileges')]
        run = ['systemd-run', '--user', '--wait', '--pipe', '--quiet', '--collect'] + [
            a for p in props for a in ('-p', p)]
        probe = subprocess.run(run + ['--', 'python3', '-c', 'import socket; socket.socket(socket.AF_UNIX)'],
                               capture_output=True, text=True, timeout=60)
        if 'Address family not supported' not in probe.stderr:
            self.skipTest(f'this user manager applies no seccomp filter: {probe.stderr.strip()[-200:]}')
        self.old_template()
        port = self.server.server_address[1]
        self.conf(RUNNER_RELEASE=f'https://localhost:{port}/release', RUNNER_DOWNLOAD=f'https://localhost:{port}/download')
        cmd = list(run)
        for k, v in self.env().items():
            cmd += ['--setenv', f'{k}={v}']
        got = subprocess.run(cmd + ['--', 'bash', LAUNCH, 'refresh'], capture_output=True, text=True, timeout=300)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertEqual(self.version(), f'{NEW}\n')

    def test_a_real_run_in_the_services_sandbox(self):
        """The service's sandbox (as a transient user unit) lets a real refresh through. Only the places differ: the
        test's template folder in place of /opt/fork-fleet/template, and so ProtectHome and PrivateTmp (they would
        hide it), and the capability bound (a user manager cannot set it). Skipped where the user manager applies no
        sandbox (no user namespaces): there the root proof in the runbook (host step 7) covers it. RUN_DIR is read-only
        in it, as on the host."""
        if not shutil.which('systemd-run'):
            self.skipTest('no systemd-run')
        probe = subprocess.run(['systemd-run', '--user', '--wait', '--pipe', '--quiet', '--collect',
                                '-p', 'ProtectSystem=strict', '--', 'sh', '-c', 'touch /usr/.fork-fleet-probe'],
                               capture_output=True, text=True, timeout=60)
        if 'Read-only file system' not in probe.stderr:
            self.skipTest('this user manager applies no sandbox (no user namespaces, or no user manager)')
        self.old_template()
        os.makedirs(self.path('run'), 0o700)
        self.write('run/launch.lock', '')   # made by the dispatcher's prune; the service may only read RUN_DIR
        skip = {'Type', 'ExecStart', 'ReadWritePaths', 'ProtectHome', 'PrivateTmp', 'CapabilityBoundingSet',
                'TimeoutStartSec'}
        props = [f'{k}={v}' for k, v in unit('fork-fleet-refresh.service')['Service'].items() if k not in skip]
        cmd = ['systemd-run', '--user', '--wait', '--pipe', '--quiet', '--collect', '-p',
               f'ReadWritePaths={self.path("opt")}']
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


# The service's sandbox: it may write the template's own folder only (not RUN_DIR, the root launcher's), make no device, setuid or setgid
# file, reach the network by IP only (no unix socket: systemd and the bus are out of reach) and use 1 GiB of memory, one
# CPU and 64 tasks at most (a compromised release unpacked by a bug in refresh still cannot reach the host).
SANDBOX = {'ProtectSystem': 'strict', 'ReadWritePaths': '-/opt/fork-fleet/template', 'ProtectHome': 'yes',
           'PrivateTmp': 'yes', 'PrivateDevices': 'yes', 'NoNewPrivileges': 'yes', 'RestrictSUIDSGID': 'yes',
           'RestrictAddressFamilies': 'AF_INET AF_INET6', 'CapabilityBoundingSet': 'CAP_DAC_OVERRIDE',
           'MemoryMax': '1G', 'MemorySwapMax': '0', 'CPUQuota': '100%', 'TasksMax': '64'}


def default_template():
    """The launcher's default TEMPLATE (the one place the template's path is set)."""
    with open(LAUNCH) as f:
        return re.search(r"^RUNNER_USERS='' TEMPLATE=(\S+) ", f.read(), re.M).group(1)


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

    def test_the_service_cannot_write_run_dir(self):
        """/run/fork-fleet holds the root launcher's lock and the jobs' environment files (root opens them outside any
        sandbox): no RuntimeDirectory= (it would make the folder writable) and no other write path under /run."""
        service = unit('fork-fleet-refresh.service')['Service']
        self.assertFalse([k for k in service if k.startswith('RuntimeDirectory')])
        for key in ('ReadWritePaths', 'BindPaths', 'StateDirectory', 'LogsDirectory', 'CacheDirectory'):
            self.assertFalse([p for p in service.get(key, '').split() if p.lstrip('-+').startswith('/run')], key)

    def test_the_service_writes_the_templates_own_folder_only(self):
        """The writable folder is the default template's own folder, and none of the files root runs (the launcher,
        the dispatcher, its trust config, the job hook, the desk link and its wire format, seed and key tools) is in it
        or under it."""
        service = unit('fork-fleet-refresh.service')['Service']
        rw = [p.lstrip('-') for p in service['ReadWritePaths'].split()]
        self.assertEqual(rw, [os.path.dirname(default_template())])
        root_dir = os.path.dirname(service['ExecStart'].split()[0])
        for f in ('fork-fleet-launch.sh', 'fork-fleet.py', 'fork-fleet.json', 'hook.sh', 'fork-fleet-desk.py',
                  'fork-fleet-desk-wire.py', 'fork-fleet-desk-seed.py', 'fork-fleet-desk-key.py', 'SOURCE'):
            path = f'{root_dir}/{f}'
            self.assertFalse(any(path == w or path.startswith(w.rstrip('/') + '/') for w in rw), path)
        self.assertNotIn('AF_UNIX', service['RestrictAddressFamilies'].split())
        with open(os.path.join(os.path.dirname(HERE), 'fork-fleet.md')) as f:
            runbook = f.read()
        self.assertIn(f'mv -T /opt/fork-fleet/runner-template {default_template()}', runbook)   # the move, once

    def test_the_runbooks_move_is_safe_to_run_again(self):
        """Host step 7's one-time move, run as written (its places pointed at a temp folder): it finishes when run
        again after it stopped half-way (template moved, launch.conf not yet changed), does nothing harmful a third
        time, and removes the temp folders a refresh killed under the old layout left beside the launcher."""
        if not shutil.which('flock'):
            self.skipTest('no flock')
        with open(os.path.join(os.path.dirname(HERE), 'fork-fleet.md')) as f:
            line = next(x.strip()[2:].strip() for x in f if 'mv -T /opt/fork-fleet/runner-template' in x)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        line = line.replace('/opt/', f'{d}/opt/').replace('/run/', f'{d}/run/').replace('/etc/', f'{d}/etc/')
        os.makedirs(f'{d}/opt/fork-fleet/runner-template')
        os.makedirs(f'{d}/opt/fork-fleet/.runner-template.refresh.AbC123/new')
        os.makedirs(f'{d}/etc/fork-fleet')
        conf = f'{d}/etc/fork-fleet/launch.conf'

        def write_conf():
            with open(conf, 'w') as f:
                f.write('RUNNER_USERS=a\nTEMPLATE=/opt/fork-fleet/runner-template\n')

        def run():
            return subprocess.run(['bash', '-c', line], capture_output=True, text=True, timeout=60)

        write_conf()
        os.chmod(conf, 0o400)   # sed fails: it stops half-way, after the move
        os.chmod(f'{d}/etc/fork-fleet', 0o500)
        self.assertNotEqual(run().returncode, 0)
        os.chmod(f'{d}/etc/fork-fleet', 0o700)
        os.chmod(conf, 0o600)
        self.assertTrue(os.path.isdir(f'{d}/opt/fork-fleet/template/runner-template'))
        for _ in range(2):   # again: it finishes; and once more: still fine
            got = run()
            self.assertEqual(got.returncode, 0, got.stderr)
            with open(conf) as f:
                self.assertEqual(f.read(), 'RUNNER_USERS=a\n')
            self.assertEqual(sorted(os.listdir(f'{d}/opt/fork-fleet')), ['template'])
            self.assertEqual(os.listdir(f'{d}/opt/fork-fleet/template'), ['runner-template'])

    def test_the_runbook_names_no_file_count(self):
        """Host step 3's file list grows with each fleet part: a count in the text beside it goes stale."""
        with open(os.path.join(os.path.dirname(HERE), 'fork-fleet.md')) as f:
            runbook = f.read()
        self.assertNotRegex(runbook, r'All (two|three|four|five|six|seven|eight|nine|ten|\d+) are fetched')

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
