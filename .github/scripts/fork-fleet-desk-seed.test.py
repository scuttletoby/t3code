#!/usr/bin/env python3
"""Tests of fork-fleet-desk-seed.py (a desk pool's warm caches) and the `seeds` check of fork-fleet.json: the host's
offer in-process and the forced command's divert in fork-fleet-desk.py. Temp folders only; no root, no ssh, no
network. Run: python3 -I -B <this>. Linux only."""
import fcntl, hashlib, importlib.util, io, json, os, shutil, subprocess, sys, tarfile, tempfile, threading, time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


seed, ff = load('fork_fleet_desk_seed', 'fork-fleet-desk-seed.py'), load('fork_fleet', 'fork-fleet.py')
with open(os.path.join(HERE, '..', 'fork-fleet.json'), encoding='utf-8') as _f:
    CONFIG = json.load(_f)
POOL = CONFIG['pools'][0]['name']
SEEDS = [{'name': 'm2', 'every_hours': 6, 'max_age_hours': 1000, 'max_mb': 2},
         {'name': 'npm', 'every_hours': 1, 'max_age_hours': 48, 'max_mb': 1}]

class Base(unittest.TestCase):
    def setUp(self):
        self.t = os.path.realpath(tempfile.mkdtemp(prefix='seed-test-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        for d in ('files', f'files/{POOL}', 'run', 'src'):
            os.mkdir(os.path.join(self.t, d), 0o755)
        self.config(SEEDS)

    def config(self, seeds):
        cfg = json.loads(json.dumps(CONFIG))
        cfg['pools'][0]['seeds'] = seeds
        with open(os.path.join(self.t, 'fork-fleet.json'), 'w', encoding='utf-8') as f:
            json.dump(cfg, f)

    def make(self, name, members, mtime=None):
        """FILES/POOL/NAME.tar of `members` ({path: bytes}), made as a project would: a temp file, then a rename."""
        tmp = os.path.join(self.t, 'files', POOL, f'.{name}.tmp')
        with tarfile.open(tmp, 'w', format=tarfile.PAX_FORMAT) as tar:
            for path, data in members.items():
                info = tarfile.TarInfo(path)
                info.size, info.mode = len(data), 0o644
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


if __name__ == '__main__':
    unittest.main()
