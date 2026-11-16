#!/usr/bin/env python3
"""Tests of fork-fleet-desk-seed.py (a desk pool's warm caches) and the `seeds` check of fork-fleet.json: the host's
offer in-process, the desktop's run end to end through a fake ssh that runs the host's side, and the forced command's
divert in fork-fleet-desk.py. Temp folders only; no root, no ssh, no network. Run: python3 -I -B <this>. Linux only."""
import contextlib, fcntl, hashlib, importlib.util, io, json, os, shutil, stat, subprocess, sys, tarfile, tempfile, threading
import signal, time, unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


seed, ff = load('fork_fleet_desk_seed', 'fork-fleet-desk-seed.py'), load('fork_fleet', 'fork-fleet.py')
agent = load('fork_fleet_desk_agent', 'fork-fleet-desk-agent.py')
with open(os.path.join(HERE, '..', 'fork-fleet.json'), encoding='utf-8') as _f:
    CONFIG = json.load(_f)
POOL = CONFIG['pools'][0]['name']
SEEDS = [{'name': 'm2', 'every_hours': 6, 'max_age_hours': 1000, 'max_mb': 2},
         {'name': 'npm', 'every_hours': 1, 'max_age_hours': 48, 'max_mb': 1}]

FAKE_SSH = '''#!{python} -I
import io, os, sys
sys.dont_write_bytecode = True
import importlib.util
spec = importlib.util.spec_from_file_location('s', {module!r})
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)
args = sys.argv[1:]
words = args[args.index('--') + 2:]
with open({t!r} + '/calls', 'a') as f:
    f.write(' '.join(words) + '\\n')
try:
    with open({t!r} + '/mode') as f:
        mode = f.read().strip()
except FileNotFoundError:
    mode = ''
if mode == 'down':
    print('ssh: connect to host h port 22: No route to host', file=sys.stderr)
    sys.exit(255)
if words[0] == 'seeds' and mode == 'oldhost':   # a host on an older kit: a serve session, not a list
    print('fork-fleet-desk: serving')
    sys.exit(0)
if words[0] == 'seeds' and mode == 'deep':      # a list nested deeper than json can go
    print('[' * 200000)
    sys.exit(0)
if words[0] == 'seeds' and mode == 'exit1':     # a host stopped half-way through its install
    print('fork-fleet-desk: no config', file=sys.stderr)
    sys.exit(1)
kw = dict(config={t!r} + '/fork-fleet.json', files={t!r} + '/files')
if words[0] == 'seeds' and mode == 'biglist':
    os.write(1, b' ' * (2 << 20))
if mode == 'noisy':
    for _ in range(64):
        os.write(2, b'x' * (1 << 16) + b'\\n')
if words[0] == 'seed' and mode in ('cut', 'trailer', 'odd', 'made1'):
    with open({t!r} + '/sent', 'w+b') as out:
        err = io.StringIO()
        code = seed.offer({pool!r}, ' '.join(words), {t!r} + '/run', out=out.fileno(), err=err, **kw)
        out.seek(0)
        data = out.read()
    if mode == 'cut':
        os.write(1, data[:len(data) // 2])
        sys.exit(255)
    os.write(1, data)
    if mode == 'odd':   # a MADE that str.isdigit() takes but int() does not
        print(err.getvalue().rsplit(' ', 1)[0] + ' \u00b2', file=sys.stderr)
    elif mode == 'made1':   # bytes and sha256 right, MADE not the list's
        print(err.getvalue().rsplit(' ', 1)[0] + ' 1', file=sys.stderr)
    else:
        print(err.getvalue().rsplit(' ', 2)[0] + ' ' + '0' * 64 + ' 1', file=sys.stderr)   # a wrong sha256
    sys.exit(code)
sys.exit(seed.offer({pool!r}, ' '.join(words), {t!r} + '/run', **kw))
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.t = os.path.realpath(tempfile.mkdtemp(prefix='seed-test-'))
        self.addCleanup(seed.remove, self.t)   # a tree of any depth
        for d in ('files', f'files/{POOL}', 'run', 'src'):
            os.mkdir(os.path.join(self.t, d), 0o755)
        self.config(SEEDS)

    def config(self, seeds):
        cfg = json.loads(json.dumps(CONFIG))
        cfg['pools'][0]['seeds'] = seeds
        with open(os.path.join(self.t, 'fork-fleet.json'), 'w', encoding='utf-8') as f:
            json.dump(cfg, f)

    def make(self, name, members, mtime=None, mode=0o644):
        """FILES/POOL/NAME.tar of `members` ({path: bytes}), made as a project would: a temp file, then a rename."""
        tmp = os.path.join(self.t, 'files', POOL, f'.{name}.tmp')
        with tarfile.open(tmp, 'w', format=tarfile.PAX_FORMAT) as tar:
            for path, data in members.items():
                info = tarfile.TarInfo(path)
                info.size, info.mode = len(data), mode
                tar.addfile(info, io.BytesIO(data))
        if mtime is not None:
            os.utime(tmp, (mtime, mtime))
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.tar(name))
        with open(self.tar(name), 'rb') as f:
            return f.read()

    def tar(self, name):
        return os.path.join(self.t, 'files', POOL, f'{name}.tar')

    def offer(self, command, **kw):
        r, w = os.pipe()
        err = io.StringIO()
        with os.fdopen(r, 'rb') as rf:
            try:
                code = seed.offer(POOL, command, os.path.join(self.t, 'run'), out=w, err=err,
                                  config=os.path.join(self.t, 'fork-fleet.json'), files=os.path.join(self.t, 'files'),
                                  **kw)
            finally:
                os.close(w)
            return code, rf.read(), err.getvalue()


class Config(unittest.TestCase):
    def test_seeds_are_checked(self):
        def problems(seeds):
            cfg = json.loads(json.dumps(CONFIG))
            cfg['pools'][0]['seeds'] = seeds
            return [p for p in ff.check_config(cfg) if 'seed' in p]
        self.assertEqual(problems(SEEDS), [])
        self.assertEqual(problems([]), [])
        self.assertEqual(problems([{**SEEDS[0], 'max_mb': ff.SEED_MAX_MB}]), [])
        good = SEEDS[0]
        for bad in ({}, [{**good, 'name': 'M2'}], [{**good, 'name': '../x'}], [{**good, 'every_hours': 0}],
                    [{**good, 'max_mb': True}], [{**good, 'max_age_hours': 5}], [{**good, 'max_mb': '2'}],
                    [{**good, 'max_mb': ff.SEED_MAX_MB + 1}],
                    [{k: v for k, v in good.items() if k != 'max_mb'}], ['m2'], [good, good]):
            with self.subTest(bad):
                self.assertEqual(len(problems(bad)), 1, bad)


class Host(Base):
    def test_the_list_says_what_is_offered_and_why_not(self):
        self.make('m2', {'a': b'x' * 100})
        code, out, err = self.offer('seeds')
        self.assertEqual(code, 0, err)
        got = json.loads(out)
        self.assertEqual(got['v'], 1)
        m2, npm = got['seeds']
        st = os.stat(self.tar('m2'))
        self.assertEqual(m2, {'name': 'm2', 'everyHours': 6, 'maxAgeHours': 1000, 'maxMb': 2, 'bytes': st.st_size,
                              'made': int(st.st_mtime)})
        self.assertNotIn('made', npm)
        self.assertIn('not made yet', npm['why'])

    def test_a_seed_streams_whole_with_its_trailer(self):
        data = self.make('m2', {'a/b': b'hello', 'c': b'x' * 5000})
        code, out, err = self.offer('seed m2')
        self.assertEqual(code, 0, err)
        self.assertEqual(out, data)
        st = os.stat(self.tar('m2'))
        self.assertEqual(err, f'seed m2 {len(data)} {hashlib.sha256(data).hexdigest()} {int(st.st_mtime)}\n')

    def test_other_commands_and_names_are_refused_and_send_nothing(self):
        self.make('m2', {'a': b'x'})
        for command in ('seed', 'seed ../m2', 'seed /etc/passwd', 'seed m2 x', 'seed other', 'seeds x', 'serve x',
                        'rm -rf /', 'seed M2'):
            with self.subTest(command):
                code, out, err = self.offer(command)
                self.assertEqual((code, out), (2, b''), err)

    def test_a_file_that_may_not_be_sent_is_exit_4(self):
        top, files = os.path.join(self.t, 'files'), os.path.join(self.t, 'files', POOL)
        now = time.time()
        cases = {'not made yet': lambda: None,
                 'Too many levels of symbolic links': lambda: os.symlink(os.path.join(self.t, 'src', 'x.tar'),
                                                                         f'{files}/m2.tar'),
                 'others may write it': lambda: (self.make('m2', {'a': b'x'}), os.chmod(f'{files}/m2.tar', 0o664)),
                 'above max_mb': lambda: self.make('m2', {'a': os.urandom(3 << 20)}),
                 'older than max_age_hours': lambda: self.make('m2', {'a': b'x'}, mtime=now - 1001 * 3600),
                 'dated in the future': lambda: self.make('m2', {'a': b'x'}, mtime=now + 10 * 365 * 86400),
                 f'{top} must be a folder': lambda: (self.make('m2', {'a': b'x'}), os.chmod(top, 0o777)),
                 'not a plain file': lambda: os.mkdir(f'{files}/m2.tar'),
                 f'{files} must be a folder': lambda: (self.make('m2', {'a': b'x'}), os.chmod(files, 0o777))}
        with open(os.path.join(self.t, 'src', 'x.tar'), 'wb') as f:
            f.write(b'x')
        os.chmod(os.path.join(self.t, 'src', 'x.tar'), 0o644)   # only the link itself may be the reason
        for name, make in cases.items():
            with self.subTest(name):
                make()
                code, out, err = self.offer('seed m2')
                self.assertEqual((code, out), (4, b''), err)
                self.assertIn(name, err)   # refused for this reason, read where the pool's files are
                os.chmod(files, 0o755)
                os.chmod(top, 0o755)
                p = f'{files}/m2.tar'
                if os.path.isdir(p) and not os.path.islink(p):
                    os.rmdir(p)
                elif os.path.lexists(p):
                    os.unlink(p)

    def test_a_second_stream_for_the_pool_is_exit_3(self):
        self.make('m2', {'a': b'x'})
        with open(os.path.join(self.t, 'run', f'{POOL}.seed.lock'), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.offer('seed m2')[:2], (3, b''))
        self.assertEqual(self.offer('seed m2')[0], 0)   # the lock is gone with its holder

    def test_a_file_changed_while_sent_is_exit_4(self):
        self.make('m2', {'a': b'x' * 3000})
        path, send = self.tar('m2'), seed.send

        def changing(fd, out, size):
            with open(path, 'ab') as f:   # made in place, not by rename
                f.write(b'\0' * 512)
            return send(fd, out, size)
        with mock.patch.object(seed, 'send', changing):
            code, out, err = self.offer('seed m2')
        self.assertEqual(code, 4)
        self.assertIn('changed while it was sent', err)
        self.assertEqual(len(out), os.stat(path).st_size - 512)   # cut at the size that was checked

    def test_a_rename_while_sent_is_fine(self):
        old, send = self.make('m2', {'a': b'x' * 3000}), seed.send

        def renaming(fd, out, size):
            self.make('m2', {'a': b'y' * 3000})   # the documented refresh: a temp file, then a rename
            return send(fd, out, size)
        with mock.patch.object(seed, 'send', renaming):
            code, out, err = self.offer('seed m2')
        self.assertEqual((code, out), (0, old), err)
        self.assertTrue(err.startswith(f'seed m2 {len(old)} {hashlib.sha256(old).hexdigest()} '), err)

    def test_a_lock_of_another_user_is_refused(self):
        self.make('m2', {'a': b'x'})
        open(os.path.join(self.t, 'run', f'{POOL}.seed.lock'), 'a').close()
        with mock.patch.object(seed.os, 'geteuid', return_value=os.geteuid() + 4242):
            code, out, err = self.offer('seed m2')
        self.assertEqual((code, out), (1, b''), err)
        self.assertIn(f'belongs to uid {os.geteuid()}', err)

    def test_a_same_size_rewrite_with_its_old_date_put_back_is_exit_4(self):
        self.make('m2', {'a': b'x' * 3000})
        path, send = self.tar('m2'), seed.send

        def rewriting(fd, out, size):
            st = os.stat(path)
            with open(path, 'r+b') as f:   # in place, same size, the old mtime put back: only ctime tells
                f.write(b'y' * 512)
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
            return send(fd, out, size)
        with mock.patch.object(seed, 'send', rewriting):
            code, _, err = self.offer('seed m2')
        self.assertEqual(code, 4)
        self.assertIn('changed while it was sent', err)

    def test_a_desktop_that_stops_reading_lets_the_lock_go(self):
        self.make('m2', {'a': os.urandom(1 << 20)})
        r, w = os.pipe()   # nobody reads r: the pipe fills and the write waits
        err = io.StringIO()
        try:
            with mock.patch.object(seed, 'STALL', 1):
                t = time.monotonic()
                code = seed.offer(POOL, 'seed m2', os.path.join(self.t, 'run'), out=w, err=err,
                                  config=os.path.join(self.t, 'fork-fleet.json'), files=os.path.join(self.t, 'files'))
            self.assertLess(time.monotonic() - t, 30)
        finally:
            os.close(r)
            os.close(w)
        self.assertEqual(code, 1)
        self.assertIn('took less than 64 KiB in 1 seconds', err.getvalue())
        self.make('m2', {'a': b'x'})   # small enough for the pipe: offer() below has no reader until it returns
        self.assertEqual(self.offer('seed m2')[0], 0)   # the pool's lock was let go

    def test_a_desktop_that_stops_reading_a_one_piece_seed_is_exit_1(self):
        """lens236r3: the guard's `wrote == sent` half was untested. A seed of one piece (under PIECE) has
        sent == size at the stall, so only that half refuses the cut stream."""
        self.make('m2', {'a': os.urandom(512 << 10)})
        r, w = os.pipe()   # nobody reads r
        err = io.StringIO()
        try:
            with mock.patch.object(seed, 'STALL', 1):
                code = seed.offer(POOL, 'seed m2', os.path.join(self.t, 'run'), out=w, err=err,
                                  config=os.path.join(self.t, 'fork-fleet.json'), files=os.path.join(self.t, 'files'))
        finally:
            os.close(r)
            os.close(w)
        self.assertEqual(code, 1, err.getvalue())
        self.assertNotIn('seed m2 ', err.getvalue())   # no trailer for a cut stream

    def test_a_slow_desktop_meets_the_deadline(self):
        self.make('m2', {'a': os.urandom(1 << 20)})
        r, w = os.pipe()   # nobody reads r; the stall wait alone (100 s) would hold the lock much longer
        err = io.StringIO()
        try:
            with mock.patch.multiple(seed, STALL=100, GRACE=1, MIN_RATE=1 << 40):
                t = time.monotonic()
                code = seed.offer(POOL, 'seed m2', os.path.join(self.t, 'run'), out=w, err=err,
                                  config=os.path.join(self.t, 'fork-fleet.json'), files=os.path.join(self.t, 'files'))
            self.assertLess(time.monotonic() - t, 30)
        finally:
            os.close(r)
            os.close(w)
        self.assertEqual(code, 1, err.getvalue())
        self.assertIn('within 1 seconds', err.getvalue())

    def test_a_desktop_reading_slowly_but_steadily_is_not_dropped(self):
        data = self.make('m2', {'a': os.urandom(300 << 10)})
        r, w = os.pipe()
        got = bytearray()

        def reader():
            with os.fdopen(r, 'rb', buffering=0) as f:
                while chunk := f.read(16 << 10):   # 16 KiB, then a pause: under a stall wait per 64 KiB piece
                    got.extend(chunk)
                    time.sleep(0.3)
        th = threading.Thread(target=reader)
        th.start()
        err = io.StringIO()
        try:
            with mock.patch.object(seed, 'STALL', 2):
                code = seed.offer(POOL, 'seed m2', os.path.join(self.t, 'run'), out=w, err=err,
                                  config=os.path.join(self.t, 'fork-fleet.json'), files=os.path.join(self.t, 'files'))
        finally:
            os.close(w)
            th.join(60)
        self.assertEqual((code, bytes(got)), (0, data), err.getvalue())

    def test_an_alarm_that_comes_after_the_last_write_is_not_a_stall(self):
        data = self.make('m2', {'a': b'y' * 3000})
        real = seed.signal
        fired = []

        def alarm(n):   # the last stall alarm goes off just as the stream ends: the race between the loop and alarm(0)
            got = real.alarm(n)
            if n == 0 and not fired:
                fired.append(True)
                os.kill(os.getpid(), real.SIGALRM)
            return got
        fake = type('signal', (), {'SIGALRM': real.SIGALRM, 'signal': staticmethod(real.signal),
                                   'alarm': staticmethod(alarm)})
        with mock.patch.object(seed, 'signal', fake):
            code, out, err = self.offer('seed m2')
        self.assertTrue(fired)
        self.assertEqual((code, out), (0, data), err)

    def test_a_pool_without_seeds_lists_nothing(self):
        cfg = json.loads(json.dumps(CONFIG))
        cfg['pools'][0].pop('seeds', None)
        with open(os.path.join(self.t, 'fork-fleet.json'), 'w', encoding='utf-8') as f:
            json.dump(cfg, f)
        code, out, err = self.offer('seeds')
        self.assertEqual((code, json.loads(out)), (0, {'v': 1, 'seeds': []}), err)
        self.assertEqual(self.offer('seed m2')[:2], (2, b''))

    def test_pools_never_share_a_seed_file(self):
        cfg = json.loads(json.dumps(CONFIG))
        cfg['pools'][0]['seeds'] = SEEDS
        cfg['pools'].append({**cfg['pools'][0], 'name': 'other-pool', 'label': 'fleet-other-t3code',
                             'beacon': 'FLEET_BEACON_OTHER'})
        self.assertEqual(ff.check_config(cfg), [])
        with open(os.path.join(self.t, 'fork-fleet.json'), 'w', encoding='utf-8') as f:
            json.dump(cfg, f)
        self.make('m2', {'a': b'x'})
        shutil.copy(self.tar('m2'), os.path.join(self.t, 'files', 'm2.tar'))   # one file for all pools, as before
        os.mkdir(os.path.join(self.t, 'files', 'other-pool'), 0o755)
        r, w = os.pipe()
        err = io.StringIO()
        try:
            code = seed.offer('other-pool', 'seed m2', os.path.join(self.t, 'run'), out=w, err=err,
                              config=os.path.join(self.t, 'fork-fleet.json'), files=os.path.join(self.t, 'files'))
        finally:
            os.close(w)
            os.close(r)
        self.assertEqual(code, 4)
        self.assertIn('not made yet', err.getvalue())

    def test_never_as_root_and_a_bad_config_is_exit_2(self):
        with mock.patch.object(seed.os, 'geteuid', return_value=0):
            self.assertEqual(self.offer('seeds')[:2], (2, b''))
        with open(os.path.join(self.t, 'fork-fleet.json'), 'w') as f:
            f.write('{')
        self.assertEqual(self.offer('seeds')[:2], (2, b''))

    def test_the_forced_command_diverts_seed_requests(self):
        opt = os.path.join(self.t, 'opt')   # the host's install: the scripts and fork-fleet.json in one folder
        os.mkdir(opt)
        for f in ('fork-fleet-desk.py', 'fork-fleet-desk-wire.py', 'fork-fleet-desk-seed.py', 'fork-fleet.py'):
            shutil.copy(os.path.join(HERE, f), opt)
        shutil.copy(os.path.join(self.t, 'fork-fleet.json'), opt)
        desk = os.path.join(opt, 'fork-fleet-desk.py')
        base = {'PATH': os.environ['PATH'], 'FORK_FLEET_DESK_DIR': os.path.join(self.t, 'run2')}
        got = subprocess.run([sys.executable, '-I', '-B', desk, 'serve', POOL], capture_output=True, text=True,
                             env={**base, 'SSH_ORIGINAL_COMMAND': 'seeds'}, timeout=30, stdin=subprocess.DEVNULL)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual([s['name'] for s in json.loads(got.stdout)['seeds']], ['m2', 'npm'])
        got = subprocess.run([sys.executable, '-I', '-B', desk, 'serve', POOL], capture_output=True, text=True,
                             env={**base, 'SSH_ORIGINAL_COMMAND': 'seed nothing-like-this'}, timeout=30,
                             stdin=subprocess.DEVNULL)
        self.assertEqual((got.returncode, got.stdout), (2, ''))
        self.assertFalse(os.path.exists(os.path.join(self.t, 'run2', f'{POOL}.sock')))   # no session was served
        for command in ('seeds x', 'seed', 'seedsx', 'seeds  '):
            with self.subTest(command):
                got = subprocess.run([sys.executable, '-I', '-B', desk, 'serve', POOL], capture_output=True,
                                     text=True, env={**base, 'SSH_ORIGINAL_COMMAND': command}, timeout=30,
                                     stdin=subprocess.DEVNULL)
                self.assertEqual((got.returncode, got.stdout), (2, ''), got.stderr)
                self.assertIn('fork-fleet-desk seed: refused', got.stderr)
                self.assertFalse(os.path.exists(os.path.join(self.t, 'run2', f'{POOL}.sock')))


class Desktop(Base):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.t, 'seeds')
        ssh = os.path.join(self.t, 'ssh')
        with open(ssh, 'w') as f:
            f.write(FAKE_SSH.format(python=sys.executable, module=seed.__file__, t=self.t, pool=POOL))
        os.chmod(ssh, 0o755)
        self.conf = {'SSH': ssh, 'KEY': '/nonexistent/key', 'KNOWN_HOSTS': '/nonexistent/known', 'PORT': '22',
                     'USER': 'fleet-desk', 'HOST': 'fleet-host.example', 'POOL': POOL}
        self.said = []
        self.own(SEEDS)
        sync = mock.patch.object(seed.os, 'sync')
        sync.start()
        self.addCleanup(sync.stop)

    def own(self, seeds):
        """The desktop's own installed fleet config (/opt/fork-fleet/fork-fleet.json); self.config is the host's."""
        cfg = json.loads(json.dumps(CONFIG))
        cfg['pools'][0]['seeds'] = seeds
        with open(os.path.join(self.t, 'own.json'), 'w', encoding='utf-8') as f:
            json.dump(cfg, f)

    def run_seed(self, now=None):
        self.said = []
        return seed.run(agent, self.conf, self.root, now=now or time.time, say=self.said.append,
                        config=os.path.join(self.t, 'own.json'))

    def mode(self, text):
        with open(os.path.join(self.t, 'mode'), 'w') as f:
            f.write(text)

    def fetches(self):
        try:
            with open(os.path.join(self.t, 'calls')) as f:
                return [line for line in f.read().splitlines() if line.startswith('seed ')]
        except FileNotFoundError:
            return []

    def files(self, name):
        top = os.path.join(self.root, name)
        if not os.path.exists(top):
            return None
        out = {}
        for d, _, names in os.walk(top):
            for n in names:
                with open(os.path.join(d, n), 'rb') as f:
                    out[os.path.relpath(os.path.join(d, n), top)] = f.read()
        return out

    def test_fetched_checked_and_swapped_in_then_not_again(self):
        self.make('m2', {'repo/a.jar': b'jar', 'repo/b/c.pom': b'pom'})
        self.assertEqual(self.run_seed(), 0)
        self.assertEqual(self.files('m2'), {'repo/a.jar': b'jar', 'repo/b/c.pom': b'pom'})
        self.assertTrue(os.readlink(os.path.join(self.root, 'm2')).startswith('.v/m2.'))
        self.assertIsNone(self.files('npm'))
        self.assertTrue(any('m2:' in s and 'swapped in' in s for s in self.said), self.said)
        self.assertTrue(any('npm' in s and 'jobs run cold' in s for s in self.said), self.said)
        for d, dirs, names in os.walk(self.root):
            for n in dirs + names:
                st = os.lstat(os.path.join(d, n))
                if not stat.S_ISLNK(st.st_mode):   # a link's own mode is always 0777 and means nothing
                    self.assertFalse(st.st_mode & 0o022, os.path.join(d, n))
        with open(os.path.join(seed.current(self.root, 'm2')[0], 'label.json')) as f:
            label = json.load(f)
        self.assertEqual(label['bytes'], os.path.getsize(self.tar('m2')))
        self.assertEqual(self.run_seed(), 0)   # the same seed on the host: nothing fetched
        self.assertEqual(self.fetches(), ['seed m2'])
        self.assertEqual(os.listdir(os.path.join(self.root, '.new')), ['.lock'])

    def test_a_new_seed_waits_for_every_hours_and_the_old_stays_for_running_jobs(self):
        self.make('m2', {'a': b'one'})
        self.run_seed()
        old = seed.current(self.root, 'm2')[0]
        self.make('m2', {'a': b'two'}, mtime=time.time() + 5)
        self.run_seed()
        self.assertEqual(self.files('m2'), {'a': b'one'})   # under every_hours since the last fetch
        later = time.time() + seed.KEEP_OLD + 3600   # a swap long after the old one came: kept from the swap on
        self.run_seed(now=lambda: later)
        self.assertEqual(self.files('m2'), {'a': b'two'})
        self.assertTrue(os.path.isdir(old))                 # a job still reading it keeps it
        self.run_seed(now=lambda: later + seed.KEEP_OLD + 60)
        self.assertFalse(os.path.exists(old))
        self.assertEqual(self.files('m2'), {'a': b'two'})
        self.assertEqual(os.listdir(os.path.join(self.root, '.v')), [os.path.basename(seed.current(self.root,
                                                                                                    'm2')[0])])

    def torn(self, setup, want_in_say):
        self.make('m2', {'a': b'one'})
        self.run_seed()
        setup()
        self.run_seed(now=lambda: time.time() + 7 * 3600)
        self.assertEqual(self.files('m2'), {'a': b'one'})
        self.assertEqual(os.listdir(os.path.join(self.root, '.new')), ['.lock'])
        self.assertEqual(len(os.listdir(os.path.join(self.root, '.v'))), 1)
        self.assertTrue(any('keeps the old one' in s and any(w in s for w in want_in_say.split('|'))
                            for s in self.said), self.said)

    def test_a_cut_stream_leaves_the_old_seed(self):
        self.torn(lambda: (self.make('m2', {'a': os.urandom(200000)}, mtime=time.time() + 5), self.mode('cut')),
                  'Torn|ReadError')   # cut in the tar or after it

    def test_a_trailer_that_does_not_match_leaves_the_old_seed(self):
        self.torn(lambda: (self.make('m2', {'a': b'two'}, mtime=time.time() + 5), self.mode('trailer')),
                  'trailer does not match')

    def test_more_than_max_mb_leaves_the_old_seed(self):
        def bigger():
            self.config([{**SEEDS[0], 'max_mb': 3}, SEEDS[1]])   # the host offers it; the list the desktop sees
            self.make('m2', {'a': os.urandom(5 << 19)}, mtime=time.time() + 5)
            with mock.patch.object(seed, 'listing', lambda a, c, real=seed.listing: {
                    k: {**v, 'maxMb': 2} for k, v in real(a, c).items()}):
                self.run_seed(now=lambda: time.time() + 7 * 3600)
        self.make('m2', {'a': b'one'})
        self.run_seed()
        bigger()
        self.assertEqual(self.files('m2'), {'a': b'one'})
        self.assertTrue(any('max_mb' in s for s in self.said), self.said)

    def test_max_mb_is_the_desktops_not_the_hosts(self):
        self.config([{**SEEDS[0], 'max_mb': 3}, SEEDS[1]])   # the host lists 3; the desktop's own copy says 2
        self.make('m2', {'a': os.urandom(5 << 19)})            # 2.5 MB
        self.run_seed()
        self.assertIsNone(self.files('m2'), self.said)
        self.assertTrue(any('max_mb' in s for s in self.said), self.said)

    def test_every_and_max_age_hours_are_the_desktops_and_unknown_names_are_ignored(self):
        self.config([{**SEEDS[0], 'every_hours': 1, 'max_age_hours': 100000}, SEEDS[1],
                     {'name': 'extra', 'every_hours': 1, 'max_age_hours': 2, 'max_mb': 1}])
        self.make('m2', {'a': b'one'})
        self.make('extra', {'a': b'x'})
        self.run_seed()
        self.assertIsNone(self.files('extra'))                 # not in the desktop's own list: never fetched
        self.assertNotIn('seed extra', self.fetches())
        self.make('m2', {'a': b'two'}, mtime=time.time() + 5)
        self.run_seed(now=lambda: time.time() + 2 * 3600)     # the host says every 1 h; the desktop's own, 6 h
        self.assertEqual(self.files('m2'), {'a': b'one'})
        self.mode('down')
        self.run_seed(now=lambda: time.time() + 1001 * 3600)  # the desktop's own max_age_hours (1000), not 100000
        self.assertIsNone(self.files('m2'))

    def test_stderr_is_never_kept_whole_and_a_huge_list_is_refused(self):
        self.mode('noisy')                                     # 4 MB on stderr before the trailer
        self.make('m2', {'a': b'one'})
        with mock.patch('tempfile.TemporaryFile', side_effect=AssertionError('stderr kept on disk')):
            self.assertEqual(self.run_seed(), 0)
        self.assertEqual(self.files('m2'), {'a': b'one'}, self.said)
        self.mode('biglist')
        self.make('m2', {'a': b'two'}, mtime=time.time() + 5)
        self.run_seed(now=lambda: time.time() + 7 * 3600)
        self.assertTrue(any('above' in s and 'bytes' in s for s in self.said), self.said)
        self.assertEqual(self.files('m2'), {'a': b'one'})

    def test_a_0600_file_is_readable_by_jobs(self):
        self.make('m2', {'cache/entry': b'x', 'cache/tool': b'#!/bin/sh\n'}, mode=0o600)
        self.run_seed()
        self.assertEqual(self.files('m2'), {'cache/entry': b'x', 'cache/tool': b'#!/bin/sh\n'}, self.said)
        top = os.path.realpath(os.path.join(self.root, 'm2'))
        for d, dirs, names in os.walk(top):
            for n in dirs + names:
                mode = os.lstat(os.path.join(d, n)).st_mode
                self.assertEqual(mode & 0o444, 0o444, (n, oct(mode)))
                self.assertFalse(mode & 0o022, (n, oct(mode)))
                if stat.S_ISDIR(mode):
                    self.assertEqual(mode & 0o111, 0o111, (n, oct(mode)))

    def test_a_link_out_of_the_seed_is_refused_even_past_the_filter(self):
        self.make('m2', {'a': b'one'})
        self.run_seed()
        tmp = os.path.join(self.t, 'files', POOL, '.m2.tmp')
        with tarfile.open(tmp, 'w') as tar:
            for name, target in (('inside', 'a'), ('escaped', '/')):
                info = tarfile.TarInfo(name)
                info.type, info.linkname = tarfile.SYMTYPE, target
                tar.addfile(info)
        os.utime(tmp, (time.time() + 5,) * 2)
        os.replace(tmp, self.tar('m2'))
        with mock.patch.object(seed.tarfile, 'data_filter', lambda member, path: member):   # a filter bypass
            self.run_seed(now=lambda: time.time() + 7 * 3600)
        self.assertEqual(self.files('m2'), {'a': b'one'})
        self.assertTrue(any('leads out of the seed' in s for s in self.said), self.said)
        for d, dirs, names in os.walk(self.root):
            self.assertNotIn('escaped', dirs + names, d)

    def test_an_odd_trailer_keeps_the_old_seed_and_the_run_goes_on(self):
        self.torn(lambda: (self.make('m2', {'a': b'two'}, mtime=time.time() + 5), self.mode('odd')),
                  'trailer does not match')

    def test_too_many_members_keep_the_old_seed(self):
        with mock.patch.object(seed, 'MEMBERS', 2):
            self.torn(lambda: self.make('m2', {'a': b'1', 'b': b'2', 'c': b'3'}, mtime=time.time() + 5), 'members')

    def test_a_tar_that_would_leave_its_folder_writes_nothing_outside(self):
        bad = {'a ../ path': ('../../escaped', tarfile.REGTYPE, ''), 'a device': ('escaped', tarfile.CHRTYPE, ''),
               'an absolute link': ('escaped', tarfile.SYMTYPE, '/'),
               'a link up': ('escaped', tarfile.SYMTYPE, '../../..'),
               'a hard link out': ('escaped', tarfile.LNKTYPE, '/etc/hostname')}
        for why, (name, kind, target) in bad.items():
            def evil():
                tmp = os.path.join(self.t, 'files', POOL, '.m2.tmp')
                with tarfile.open(tmp, 'w') as tar:   # one bad member each: the filter stops at the first
                    ok = tarfile.TarInfo('ok')
                    ok.size = 1
                    tar.addfile(ok, io.BytesIO(b'x'))
                    info = tarfile.TarInfo(name)
                    info.type, info.linkname = kind, target
                    tar.addfile(info)
                os.utime(tmp, (time.time() + 5,) * 2)
                os.replace(tmp, self.tar('m2'))
            with self.subTest(why):
                shutil.rmtree(self.root, True)
                self.torn(evil, 'Error')
                for d, dirs, names in os.walk(self.t):
                    self.assertNotIn('escaped', dirs + names, d)

    def deep_tar(self, name, depth, count=1, size=0):
        """FILES/POOL/NAME.tar of COUNT members d{i}/x/x/.../f, DEPTH folders deep, newer than any made before."""
        tmp = os.path.join(self.t, 'files', POOL, f'.{name}.tmp')
        with tarfile.open(tmp, 'w', format=tarfile.GNU_FORMAT) as tar:
            for i in range(count):
                info = tarfile.TarInfo(f'd{i}/' + 'x/' * depth + 'f')
                info.size = size
                tar.addfile(info, io.BytesIO(b'z' * size))
        os.utime(tmp, (time.time() + 5,) * 2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.tar(name))

    def deep_folder(self, top, depth):
        """TOP/x/x/.../f, DEPTH folders deep, made folder by folder (a path that long is past PATH_MAX)."""
        os.makedirs(top)
        fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for _ in range(depth):
                os.mkdir('x', dir_fd=fd)
                child = os.open('x', os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
            os.close(os.open('f', os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd))
        finally:
            os.close(fd)

    def test_a_deep_path_does_not_stop_the_run(self):
        self.make('npm', {'a': b'n'})
        self.deep_tar('m2', 1500)
        self.assertEqual(self.run_seed(), 0)
        self.assertEqual(self.files('npm'), {'a': b'n'})
        self.assertIsNone(self.files('m2'))
        self.assertTrue(any('m2' in s and 'folders deep' in s for s in self.said), self.said)
        self.assertEqual(os.listdir(os.path.join(self.root, '.new')), ['.lock'])

    def test_any_failure_of_one_seed_never_stops_the_run_and_old_versions_are_still_pruned(self):
        os.makedirs(os.path.join(self.root, '.v', 'm2.1', 'data'))   # a version no link names, long replaced
        os.utime(os.path.join(self.root, '.v', 'm2.1'), (1, 1))
        self.make('npm', {'a': b'n'})
        self.deep_tar('m2', 1500)
        real = seed.fetch

        def broken(agent_, conf, item, root, now, **kw):
            if item['name'] == 'm2':
                raise RuntimeError('anything at all')
            return real(agent_, conf, item, root, now, **kw)
        with mock.patch.object(seed, 'fetch', broken):
            self.assertEqual(self.run_seed(), 0)
        self.assertEqual(self.files('npm'), {'a': b'n'})
        self.assertTrue(any('m2' in s and 'RuntimeError' in s for s in self.said), self.said)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.v', 'm2.1')))
        # a path deeper than Python's recursion allows, past the depth check: said, cleaned up, the run goes on
        shutil.rmtree(self.root)
        with mock.patch.object(seed, 'DEPTH', 10 ** 6), mock.patch.object(seed, 'NAME_MOST', 10 ** 6), \
                mock.patch.object(seed, 'ENTRY', 0):
            self.assertEqual(self.run_seed(), 0)
        self.assertTrue(any('m2' in s and 'RecursionError' in s for s in self.said), self.said)
        self.assertEqual(self.files('npm'), {'a': b'n'})
        self.assertIsNone(self.files('m2'))
        self.assertEqual(os.listdir(os.path.join(self.root, '.new')), ['.lock'])

    def raw(self, name, blob):
        """FILES/POOL/NAME.tar holding BLOB (tar blocks made by hand), newer than any made before."""
        tmp = os.path.join(self.t, 'files', POOL, f'.{name}.tmp')
        with open(tmp, 'wb') as f:
            f.write(blob + b'\0' * 1024)
        os.chmod(tmp, 0o644)
        os.utime(tmp, (time.time() + 5,) * 2)
        os.replace(tmp, self.tar(name))

    @staticmethod
    def block(info, data=b'', fmt=tarfile.GNU_FORMAT):
        """One member: INFO's header (its size is DATA's) and DATA padded to whole blocks."""
        info.size = len(data)
        return info.tobuf(format=fmt) + data + b'\0' * (-len(data) % 512)

    @staticmethod
    def pax(**records):
        out = b''
        for k, v in records.items():
            line = f' {k}={v}\n'.encode()
            n = len(line) + 1
            while n != len(line) + len(str(n)):   # the length counts its own digits
                n = len(line) + len(str(n))
            out += str(n).encode() + line
        return out

    def test_a_run_killed_in_every_fetch_still_prunes(self):   # lens237sr2: the MUST
        os.makedirs(os.path.join(self.root, '.v', 'm2.1', 'data'))   # long replaced, no link names it
        os.utime(os.path.join(self.root, '.v', 'm2.1'), (1, 1))
        self.make('m2', {'a': b'm'})
        self.make('npm', {'a': b'n'})
        real = seed.fetch

        def oom(agent_, conf, item, root, now, **kw):   # MemoryMax or TimeoutStartSec: SIGKILL, no finally runs
            if item['name'] == 'npm':
                os.kill(os.getpid(), signal.SIGKILL)
            return real(agent_, conf, item, root, now, **kw)
        pid = os.fork()
        if pid == 0:
            try:
                with mock.patch.object(seed, 'fetch', oom):
                    self.run_seed()
            finally:
                os._exit(0)
        self.assertEqual(os.waitpid(pid, 0)[1] & 0x7f, signal.SIGKILL)
        self.assertEqual(self.files('m2'), {'a': b'm'})
        self.assertFalse(os.path.exists(os.path.join(self.root, '.v', 'm2.1')))
        with open(os.path.join(self.root, '.torn.json')) as f:   # the killed fetch counts as torn
            self.assertEqual(json.load(f)['npm']['tries'], 1)

    def test_a_pax_header_seed_stays_under_the_units_memory(self):   # lens237sr2: the MUST
        big = [{**SEEDS[0], 'max_mb': 64}, SEEDS[1]]   # a realistic max_mb (fork-fleet.md shows 4096)
        self.config(big)
        self.own(big)
        body = b''.join(b'%d a.%07d=x\n' % (len(b' a.0000000=x\n') + 2, i) for i in range(2_500_000))
        info = tarfile.TarInfo('pax')
        info.type = tarfile.XGLTYPE
        self.raw('m2', self.block(info, body, tarfile.USTAR_FORMAT))
        del body
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            import resource
            code = 2
            try:
                os.close(r)
                self.run_seed()
                os.write(w, json.dumps(self.said).encode())
                code = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss >= 512 << 10   # MemoryMax=512M, in KB
            finally:
                os._exit(code)
        os.close(w)
        with os.fdopen(r) as f:
            said = f.read()
        self.assertEqual(os.waitpid(pid, 0)[1] >> 8, 0, said)   # a ~35 MB stream (under max_mb) within 512 MB
        self.assertIn('extended (pax or long-name) headers hold more than 1 MB', said)

    def test_extended_headers_and_sparse_members_are_held_to_limits(self):
        long_name = tarfile.TarInfo('././@LongLink')
        long_name.type = tarfile.GNUTYPE_LONGNAME
        chain = tarfile.TarInfo('pax')
        chain.type = tarfile.XHDTYPE
        old_sparse = tarfile.TarInfo('s')
        old_sparse.type = tarfile.GNUTYPE_SPARSE
        pax_sparse = tarfile.TarInfo('pax')
        pax_sparse.type = tarfile.XHDTYPE
        cases = {
            'a GNU long name of 1.5 MB': (self.block(long_name, b'n' * (3 << 19) + b'\0')
                                        + self.block(tarfile.TarInfo('f'), b'x'), 'extended'),
            'pax headers of 400 KB each, three before one member': (
                self.block(chain, self.pax(comment='c' * (400 << 10))) * 3 + self.block(tarfile.TarInfo('f'), b'x'),
                'extended'),
            'an old GNU sparse member': (self.block(old_sparse), 'sparse member'),
            'a pax (1.0) sparse member': (
                self.block(pax_sparse, self.pax(**{'GNU.sparse.major': 1, 'GNU.sparse.minor': 0,
                                                   'GNU.sparse.name': 's', 'GNU.sparse.realsize': 0}))
                + self.block(tarfile.TarInfo('s'), b'0\n'.ljust(512, b'\0')), 'sparse member'),
        }
        for what, (blob, why) in cases.items():
            with self.subTest(what):
                self.raw('m2', blob)
                self.run_seed()
                self.assertIsNone(self.files('m2'))
                self.assertTrue(any('m2' in x and why in x for x in self.said), self.said)
                with contextlib.suppress(FileNotFoundError):   # each case tried at once, not after a wait
                    os.unlink(os.path.join(self.root, '.torn.json'))

    def test_members_are_let_go_as_they_are_unpacked(self):
        self.make('m2', {f'd/{i}': b'x' for i in range(300)})
        most, real = [0], tarfile.TarFile.next

        def counted(t):
            got = real(t)
            most[0] = max(most[0], len(t.members))
            return got
        with mock.patch.object(tarfile.TarFile, 'next', counted):
            self.run_seed()
        self.assertEqual(len(self.files('m2')), 300)
        self.assertLessEqual(most[0], 2)

    def test_a_hard_link_must_name_a_file_unpacked_before_it(self):
        def hard(name, target):
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.LNKTYPE, target
            return self.block(info)
        self.raw('m2', self.block(tarfile.TarInfo('a'), b'one') + hard('b', 'a'))
        self.run_seed()
        self.assertEqual(self.files('m2'), {'a': b'one', 'b': b'one'})
        self.raw('m2', self.block(tarfile.TarInfo('a'), b'two') + hard('b', 'later') +
                 self.block(tarfile.TarInfo('later'), b'x'))
        self.run_seed(now=lambda: time.time() + 7 * 3600)
        self.assertEqual(self.files('m2'), {'a': b'one', 'b': b'one'})   # the old one stays
        self.assertTrue(any('hard link' in x for x in self.said), self.said)

    def test_the_private_tarfile_parts_the_limits_rely_on_are_there(self):   # lens237sr3: fail loud, not silently
        for k in ('_proc_member', '_proc_sparse', '_proc_gnusparse_00', '_proc_gnusparse_01', '_proc_gnusparse_10'):
            self.assertTrue(callable(getattr(tarfile.TarInfo, k, None)), f'tarfile.TarInfo.{k} is gone: see Info')
        self.assertTrue(callable(getattr(tarfile.TarFile, '_find_link_target', None)),
                        'tarfile.TarFile._find_link_target is gone: see Tar')
        self.assertIs(seed.Tar._find_link_target, seed.Tar.__dict__['_find_link_target'])

    def forked(self):
        """run_seed() in a child: 0 when it stayed under the unit's MemoryMax=512M, 1 when not (its peak RSS, or a
        MemoryError: the child may map 1 GB at most, so the test never takes the machine's memory)."""
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            import resource
            code = 2
            try:
                os.close(r)
                resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
                self.run_seed()
                os.write(w, json.dumps(self.said).encode())
                code = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss >= 512 << 10
                           or any('MemoryError' in x for x in self.said))
            finally:
                os._exit(code)
        os.close(w)
        with os.fdopen(r) as f:
            self.said = json.loads(f.read() or '[]')
        return os.waitpid(pid, 0)[1] >> 8

    def test_a_link_that_cannot_be_made_never_loads_the_stream(self):   # lens237sr3: the MUST
        big = [{**SEEDS[0], 'max_mb': 128}, SEEDS[1]]
        self.config(big)
        self.own(big)
        x = tarfile.TarInfo('pax')
        x.type = tarfile.XHDTYPE
        body = b''.join(b'15 a.%07d=x\n' % i for i in range(65_000))   # ~0.98 MB, under HEADER_MOST
        rest = b''.join(self.block(x, body) + self.block(tarfile.TarInfo(f'f{i}')) for i in range(110))   # ~108 MB
        del body
        with open(os.path.join(self.t, 'rest'), 'wb') as f:   # kept on disk: the child must not inherit it
            f.write(rest)
        del rest

        def rest():
            with open(os.path.join(self.t, 'rest'), 'rb') as f:
                return f.read()

        def link(name, target, kind):
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, target
            return self.block(info)
        cases = {   # a link whose path is there: os.link fails; os.unlink of a folder fails
            'a hard link over a file': self.block(tarfile.TarInfo('a'), b'1') + self.block(tarfile.TarInfo('b'), b'2')
            + link('b', 'a', tarfile.LNKTYPE),
            'a symlink over a folder': self.block(tarfile.TarInfo('d/x'), b'1') + link('d', 'x', tarfile.SYMTYPE),
        }
        for i, (what, head) in enumerate(cases.items()):
            with self.subTest(what):
                self.raw('m2', head + rest())
                self.assertEqual(self.forked(), 0, self.said[-3:])   # hard link: over 512 MB before round 4
                self.assertIsNone(self.files('m2'))
                self.assertTrue(any('m2' in x and 'already there' in x for x in self.said), self.said)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(self.root, '.torn.json'))
        self.raw('m2', self.block(tarfile.TarInfo('a'), b'1') + link('b', 'a', tarfile.LNKTYPE))
        with mock.patch.object(os, 'link', side_effect=OSError(18, 'Invalid cross-device link')):   # any other
            self.run_seed()                                                                         # link failure
        self.assertIsNone(self.files('m2'))
        self.assertTrue(any('m2' in x and 'cannot be made' in x for x in self.said), self.said)

    def test_a_negative_size_never_lowers_the_disk_count(self):   # lens237sr3: the MUST
        d = bytearray(tarfile.TarInfo('z').tobuf(format=tarfile.GNU_FORMAT))
        d[156:157] = tarfile.DIRTYPE
        d[124:136] = b'\xff' + b'\0' * 11                     # a base-256 size below zero
        d[148:156] = b' ' * 8
        d[148:156] = b'%06o\0 ' % sum(d[:512])
        self.raw('m2', bytes(d) + b''.join(self.block(tarfile.TarInfo(f'e{i}')) for i in range(1500)))
        self.run_seed()
        self.assertIsNone(self.files('m2'))   # before round 4: 1,500 files (6 MB of entries) under max_mb 2
        self.assertTrue(any('m2' in x and 'size' in x for x in self.said), self.said)
        f = bytearray(tarfile.TarInfo('y').tobuf(format=tarfile.GNU_FORMAT))   # a folder with a size: refused too
        f[156:157] = tarfile.DIRTYPE
        f[124:136] = b'%011o\0' % 512
        f[148:156] = b' ' * 8
        f[148:156] = b'%06o\0 ' % sum(f[:512])
        self.raw('m2', bytes(f) + b'\0' * 512 + self.block(tarfile.TarInfo('e'), b'x'))
        self.run_seed(now=lambda: time.time() + 2 * 3600)
        self.assertIsNone(self.files('m2'))
        self.assertTrue(any('m2' in x and 'size' in x for x in self.said), self.said)

    def test_one_lost_list_does_not_reset_the_torn_record(self):   # lens237cr3
        self.make('m2', {'a': b'one'})
        start, codes, real, tried = int(time.time()), {}, seed.listing, []

        def torn(agent_, conf, item, root, now, **kw):
            tried.append(round((now() - start) / 3600))
            raise seed.Torn('cut off')

        def lost(h):
            def listing(a, c):
                if h % 12 == 11:   # the 30 s list lost twice a day
                    raise seed.Torn('no list in 30 s')
                return real(a, c)
            return listing
        with mock.patch.object(seed, 'fetch', torn):
            for h in range(49):
                with mock.patch.object(seed, 'listing', lost(h)):
                    codes[h] = self.run_seed(now=lambda h=h: start + h * 3600)
        self.assertEqual(tried, [0, 1, 3, 7, 15, 31])   # the back-off goes on
        self.assertEqual({h for h, c in codes.items() if c}, set(range(25, 49)) - {35, 47})   # late, list or not

    def test_a_torn_record_that_cannot_be_saved_still_counts_and_fails_the_run(self):   # lens237cr3: disk full
        self.make('m2', {'a': b'one'})
        os.makedirs(os.path.join(self.root, '.torn.json.tmp'))   # the record cannot be written, as on a full disk
        tried = []

        def torn(agent_, conf, item, root, now, **kw):
            tried.append(item['name'])
            raise seed.Torn('No space left on device')
        with mock.patch.object(seed, 'fetch', torn):
            self.assertEqual(self.run_seed(), 1)
        self.assertEqual(tried, ['m2'])   # tried, and the try counted in memory
        self.assertTrue(any('.torn.json' in x and 'cannot be written' in x for x in self.said), self.said)
        os.rmdir(os.path.join(self.root, '.torn.json.tmp'))
        with mock.patch.object(seed, 'fetch', torn):
            self.assertEqual(self.run_seed(now=lambda: time.time() + 3600), 0)
        with open(os.path.join(self.root, '.torn.json')) as f:
            self.assertEqual(json.load(f)['m2']['tries'], 1)   # the lost record starts again, no worse

    def test_a_new_copy_on_the_host_is_tried_at_once_after_torn_tries(self):   # lens237cr3 nit
        start, tried = int(time.time()), []
        self.make('m2', {'a': b'one'}, mtime=start - 3600)

        def torn(agent_, conf, item, root, now, **kw):
            tried.append(round((now() - start) / 3600))
            raise seed.Torn('cut off')
        with mock.patch.object(seed, 'fetch', torn):
            for h in (0, 1, 2):
                self.run_seed(now=lambda h=h: start + h * 3600)
            self.make('m2', {'a': b'two!'}, mtime=start - 60)   # the host made a fixed one
            self.run_seed(now=lambda: start + 2 * 3600 + 120)
            self.run_seed(now=lambda: start + 2 * 3600 + 180)
        self.assertEqual(tried, [0, 1, 2])   # at once, then the back-off again (now 4 h)
        self.assertTrue(any('torn 3 times' in x for x in self.said), self.said)

    def test_a_deeply_nested_list_means_no_list(self):   # lens237cr2
        self.make('m2', {'a': b'one'})
        self.run_seed()
        self.mode('deep')
        self.assertEqual(self.run_seed(now=lambda: time.time() + 7 * 3600), 0)
        self.assertEqual(self.files('m2'), {'a': b'one'})
        self.assertTrue(any('not well formed' in x for x in self.said), self.said)

    def test_a_folder_that_cannot_be_listed_tears_the_seed(self):
        self.make('m2', {'a/b/c': b'one'})
        real = os.scandir

        def scandir(path='.'):
            if isinstance(path, str) and path.endswith(os.sep + 'b'):
                raise OSError(36, 'File name too long', path)
            return real(path)
        with mock.patch.object(os, 'scandir', scandir):
            self.run_seed()
        self.assertIsNone(self.files('m2'))
        self.assertTrue(any('cannot be listed' in x for x in self.said), self.said)

    def test_a_run_stops_fetching_when_its_time_is_used(self):
        self.make('m2', {'a': b'one'})
        with mock.patch.object(seed, 'RUN_WAIT', 30):
            self.assertEqual(self.run_seed(), 0)
        self.assertIsNone(self.files('m2'))
        self.assertEqual(self.fetches(), [])
        self.assertTrue(any('this run has used its' in x for x in self.said), self.said)
        waits = []
        real = seed.fetch

        def timed(agent_, conf, item, root, now, wait=None):
            waits.append(wait)
            return real(agent_, conf, item, root, now, wait=wait)
        with mock.patch.object(seed, 'RUN_WAIT', 600), mock.patch.object(seed, 'fetch', timed):
            self.run_seed()
        self.assertEqual(self.files('m2'), {'a': b'one'})
        self.assertTrue(waits and all(500 < w <= 600 for w in waits), waits)   # each fetch within what is left

    def test_a_seed_torn_every_time_backs_off_then_fails_the_run_after_a_day(self):
        self.make('m2', {'a': b'one'})
        start, tried = int(time.time()), []

        def torn(agent_, conf, item, root, now, **kw):
            tried.append(now() - start)
            raise seed.Torn('cut off')
        with mock.patch.object(seed, 'fetch', torn):
            codes = {h: self.run_seed(now=lambda h=h: start + h * 3600) for h in range(0, 33)}
        self.assertEqual([round(x / 3600) for x in tried], [0, 1, 3, 7, 15, 31])   # 1, 2, 4, 8, 16 h apart
        self.assertEqual({h for h, c in codes.items() if c}, set(range(25, 33)))   # torn for more than 24 h: exit 1
        self.assertTrue(any('torn on every try' in x for x in self.said), self.said)
        self.assertEqual(self.run_seed(now=lambda: start + 63 * 3600), 0)   # fetched at last: the record goes
        self.assertEqual(self.files('m2'), {'a': b'one'})
        self.assertEqual(self.run_seed(now=lambda: start + 64 * 3600), 0)

    def test_a_tree_deeper_than_recursion_left_by_an_older_run_is_removed(self):
        self.make('m2', {'a': b'one'})
        self.run_seed()
        self.deep_folder(os.path.join(self.root, '.new', 'm2.5'), 1500)          # a torn unpack of an older release
        self.deep_folder(os.path.join(self.root, '.v', 'm2.6', 'data'), 1500)    # a version it left, long replaced
        os.utime(os.path.join(self.root, '.v', 'm2.6'), (1, 1))
        self.assertEqual(self.run_seed(), 0)
        self.assertEqual(os.listdir(os.path.join(self.root, '.new')), ['.lock'])
        self.assertEqual(os.listdir(os.path.join(self.root, '.v')),
                         [os.path.basename(seed.current(self.root, 'm2')[0])])
        self.assertEqual(self.files('m2'), {'a': b'one'})

    def test_folders_count_against_max_mb(self):
        self.deep_tar('m2', 900, count=150)
        self.run_seed()
        self.assertLess(sum(len(d) for _, d, _ in os.walk(self.root)), 100000)
        self.assertIsNone(self.files('m2'))
        cases = [('1220 folders', 'max_mb', lambda: self.deep_tar('m2', 60, count=20)),
                 ('600 empty files', 'max_mb', lambda: self.make('m2', {f'f{i}': b'' for i in range(600)},
                                                                 mtime=time.time() + 5)),
                 ('a long name', 'over 1024 bytes', lambda: self.make('m2', {'/'.join(['y' * 120] * 10): b'x'},
                                                                      mtime=time.time() + 5))]
        for case, why, setup in cases:
            with self.subTest(case):
                shutil.rmtree(self.root, True)
                self.torn(setup, why)

    def test_the_trailers_bytes_and_made_must_be_the_lists(self):
        self.torn(lambda: (self.make('m2', {'a': b'two'}, mtime=time.time() + 5), self.mode('made1')),
                  'does not match the list')
        t0 = time.time() + 7 * 3600
        for h in range(5):   # each hour tries again; a copy dated 1 is never swapped in, so none piles up in .v
            self.run_seed(now=lambda: t0 + h * 3600)
        self.assertEqual(self.files('m2'), {'a': b'one'})
        self.assertEqual(len(os.listdir(os.path.join(self.root, '.v'))), 1)
        os.unlink(os.path.join(self.t, 'mode'))
        shutil.rmtree(self.root)
        real = seed.listing

        def lying(*a):
            out = real(*a)
            out['m2'] = {**out['m2'], 'bytes': out['m2']['bytes'] + 512}
            return out
        lie = mock.patch.object(seed, 'listing', lying)
        self.addCleanup(lie.stop)
        self.torn(lambda: (self.make('m2', {'a': b'two'}, mtime=time.time() + 5), lie.start()),
                  'does not match the list')

    def test_a_broken_or_pool_less_own_config_still_drops_a_too_old_seed(self):
        for why in ('broken', 'pool-less'):
            with self.subTest(why):
                shutil.rmtree(self.root, True)
                self.own(SEEDS)
                self.make('m2', {'a': b'one'})
                self.run_seed()
                with open(os.path.join(self.t, 'own.json'), 'w') as f:
                    if why == 'broken':
                        f.write('{')
                    else:
                        cfg = json.loads(json.dumps(CONFIG))
                        cfg['pools'][0]['name'] = 'not-this-pool'
                        json.dump(cfg, f)
                fetched = len(self.fetches())
                with self.assertRaisesRegex(agent.Bad, 'no seeds fetched'):
                    self.run_seed(now=lambda: time.time() + 999 * 3600)
                self.assertEqual(self.files('m2'), {'a': b'one'})   # under its label's max_age_hours: kept
                with self.assertRaises(agent.Bad):
                    self.run_seed(now=lambda: time.time() + 1001 * 3600)
                self.assertIsNone(self.files('m2'))
                self.assertTrue(any('older than 1000 h' in s for s in self.said), self.said)
                self.assertEqual(len(self.fetches()), fetched)

    def test_an_older_or_half_installed_host_means_no_seeds(self):
        for mode in ('oldhost', 'exit1'):
            with self.subTest(mode):
                shutil.rmtree(self.root, True)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(self.t, 'mode'))
                self.make('m2', {'a': b'one'})
                self.run_seed()
                fetched = len(self.fetches())
                self.mode(mode)
                self.make('m2', {'a': b'two'}, mtime=time.time() + 5)
                self.assertEqual(self.run_seed(now=lambda: time.time() + 999 * 3600), 0)
                self.assertEqual(self.files('m2'), {'a': b'one'})
                self.assertTrue(any('stays until its age drops it' in s for s in self.said), self.said)
                self.assertEqual(self.run_seed(now=lambda: time.time() + 1001 * 3600), 0)
                self.assertIsNone(self.files('m2'))
                self.assertEqual(len(self.fetches()), fetched)

    def test_dropped_when_no_longer_listed_or_too_old_or_from_the_future(self):
        self.make('m2', {'a': b'one'})
        self.run_seed()
        self.run_seed(now=lambda: time.time() - 2 * 3600)   # the desktop's clock is behind: made in the future
        self.assertIsNone(self.files('m2'))
        self.assertTrue(any('clocks disagree' in s for s in self.said), self.said)
        self.run_seed()
        self.assertIsNotNone(self.files('m2'))
        old = seed.current(self.root, 'm2')[0]
        self.run_seed(now=lambda: time.time() + 1001 * 3600)   # over max_age_hours, and the host's is too
        self.assertIsNone(self.files('m2'))
        self.assertTrue(os.path.isdir(old))                 # dropped, but a job still reading it keeps it
        self.assertTrue(any('older than 1000 h' in s for s in self.said), self.said)
        self.run_seed()
        self.config([SEEDS[1]])   # the host no longer lists it: the desktop's own list still does, it stays
        self.run_seed()
        self.assertIsNotNone(self.files('m2'))
        self.own([SEEDS[1]])      # the desktop's own list no longer does: dropped
        self.run_seed()
        self.assertIsNone(self.files('m2'))
        self.assertTrue(any('no longer one of the pool' in s for s in self.said), self.said)

    def test_host_down_keeps_each_seed_until_its_own_age_drops_it(self):
        self.make('m2', {'a': b'one'})
        self.run_seed()
        self.mode('down')
        self.assertEqual(self.run_seed(now=lambda: time.time() + 999 * 3600), 0)
        self.assertEqual(self.files('m2'), {'a': b'one'})
        self.assertTrue(any('no list' in s for s in self.said), self.said)
        self.run_seed(now=lambda: time.time() + 1001 * 3600)
        self.assertIsNone(self.files('m2'))

    def test_a_run_that_stopped_half_way_and_bad_links_or_labels(self):
        self.make('m2', {'a': b'one'})
        self.run_seed()
        os.makedirs(os.path.join(self.root, '.new', 'm2.123', 'data'))         # a torn unpack
        os.symlink('.v/m2.999/data', os.path.join(self.root, '.m2.link'))      # a link made, not yet swapped in
        os.symlink('.v/npm.999/data', os.path.join(self.root, '.npm.link'))    # one of a seed not fetched now
        os.symlink('/etc', os.path.join(self.root, 'npm'))                    # not a link this program makes
        os.unlink(os.path.join(seed.current(self.root, 'm2')[0], 'label.json'))
        self.run_seed()
        self.assertEqual(os.listdir(os.path.join(self.root, '.new')), ['.lock'])
        self.assertFalse(os.path.lexists(os.path.join(self.root, 'npm')))
        self.assertFalse(os.path.lexists(os.path.join(self.root, '.npm.link')))
        self.assertTrue(os.path.isdir('/etc'))
        self.assertEqual(self.files('m2'), {'a': b'one'})   # fetched again in the same run
        self.assertEqual(len(self.fetches()), 2)
        self.assertTrue(any('label is missing' in s for s in self.said), self.said)

    def test_one_run_at_a_time(self):
        os.makedirs(os.path.join(self.root, '.new'))
        with open(os.path.join(self.root, '.new', '.lock'), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.run_seed(), 1)
        self.assertEqual(self.fetches(), [])

    def test_bad_settings_are_exit_2(self):
        bad = os.path.join(self.t, 'desk.conf')
        with open(bad, 'w') as f:
            f.write('POOL=x\n')
        os.chmod(bad, 0o666)
        with mock.patch('sys.stderr', io.StringIO()) as err:
            self.assertEqual(seed.main(['--config', bad, 'run']), 2)
        self.assertIn('fork-fleet-desk-seed:', err.getvalue())


if __name__ == '__main__':
    unittest.main()
