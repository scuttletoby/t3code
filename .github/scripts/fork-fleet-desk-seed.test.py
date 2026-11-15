#!/usr/bin/env python3
"""Tests of fork-fleet-desk-seed.py (a desk pool's warm caches) and the `seeds` check of fork-fleet.json: the host's
offer in-process, the desktop's run end to end through a fake ssh that runs the host's side, and the forced command's
divert in fork-fleet-desk.py. Temp folders only; no root, no ssh, no network. Run: python3 -I -B <this>. Linux only."""
import fcntl, hashlib, importlib.util, io, json, os, shutil, stat, subprocess, sys, tarfile, tempfile, time, unittest
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
kw = dict(config={t!r} + '/fork-fleet.json', files={t!r} + '/files')
if words[0] == 'seed' and mode in ('cut', 'trailer'):
    with open({t!r} + '/sent', 'w+b') as out:
        err = io.StringIO()
        code = seed.offer({pool!r}, ' '.join(words), {t!r} + '/run', out=out.fileno(), err=err, **kw)
        out.seek(0)
        data = out.read()
    if mode == 'cut':
        os.write(1, data[:len(data) // 2])
        sys.exit(255)
    os.write(1, data)
    print(err.getvalue().rsplit(' ', 2)[0] + ' ' + '0' * 64 + ' 1', file=sys.stderr)   # a wrong sha256
    sys.exit(code)
sys.exit(seed.offer({pool!r}, ' '.join(words), {t!r} + '/run', **kw))
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.t = os.path.realpath(tempfile.mkdtemp(prefix='seed-test-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        for d in ('files', 'run', 'src'):
            os.mkdir(os.path.join(self.t, d), 0o755)
        self.config(SEEDS)

    def config(self, seeds):
        cfg = json.loads(json.dumps(CONFIG))
        cfg['pools'][0]['seeds'] = seeds
        with open(os.path.join(self.t, 'fork-fleet.json'), 'w', encoding='utf-8') as f:
            json.dump(cfg, f)

    def make(self, name, members, mtime=None):
        """FILES/NAME.tar of `members` ({path: bytes}), made as a project would: a temp file, then a rename."""
        tmp = os.path.join(self.t, 'files', f'.{name}.tmp')
        with tarfile.open(tmp, 'w', format=tarfile.PAX_FORMAT) as tar:
            for path, data in members.items():
                info = tarfile.TarInfo(path)
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
        if mtime is not None:
            os.utime(tmp, (mtime, mtime))
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(self.t, 'files', f'{name}.tar'))
        with open(os.path.join(self.t, 'files', f'{name}.tar'), 'rb') as f:
            return f.read()

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
        good = SEEDS[0]
        for bad in ({}, [{**good, 'name': 'M2'}], [{**good, 'name': '../x'}], [{**good, 'every_hours': 0}],
                    [{**good, 'max_mb': True}], [{**good, 'max_age_hours': 5}], [{**good, 'max_mb': '2'}],
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
        st = os.stat(os.path.join(self.t, 'files', 'm2.tar'))
        self.assertEqual(m2, {'name': 'm2', 'everyHours': 6, 'maxAgeHours': 1000, 'maxMb': 2, 'bytes': st.st_size,
                              'made': int(st.st_mtime)})
        self.assertNotIn('made', npm)
        self.assertIn('not made yet', npm['why'])

    def test_a_seed_streams_whole_with_its_trailer(self):
        data = self.make('m2', {'a/b': b'hello', 'c': b'x' * 5000})
        code, out, err = self.offer('seed m2')
        self.assertEqual(code, 0, err)
        self.assertEqual(out, data)
        st = os.stat(os.path.join(self.t, 'files', 'm2.tar'))
        self.assertEqual(err, f'seed m2 {len(data)} {hashlib.sha256(data).hexdigest()} {int(st.st_mtime)}\n')

    def test_other_commands_and_names_are_refused_and_send_nothing(self):
        self.make('m2', {'a': b'x'})
        for command in ('seed', 'seed ../m2', 'seed /etc/passwd', 'seed m2 x', 'seed other', 'seeds x', 'serve x',
                        'rm -rf /', 'seed M2'):
            with self.subTest(command):
                code, out, err = self.offer(command)
                self.assertEqual((code, out), (2, b''), err)

    def test_a_file_that_may_not_be_sent_is_exit_4(self):
        files = os.path.join(self.t, 'files')
        now = time.time()
        cases = {'missing': lambda: None,
                 'a link': lambda: os.symlink(os.path.join(self.t, 'src', 'x.tar'), f'{files}/m2.tar'),
                 'group-writable': lambda: (self.make('m2', {'a': b'x'}), os.chmod(f'{files}/m2.tar', 0o664)),
                 'too big': lambda: self.make('m2', {'a': os.urandom(3 << 20)}),
                 'too old': lambda: self.make('m2', {'a': b'x'}, mtime=now - 1001 * 3600),
                 'a folder': lambda: os.mkdir(f'{files}/m2.tar'),
                 'folder others write': lambda: (self.make('m2', {'a': b'x'}), os.chmod(files, 0o777))}
        with open(os.path.join(self.t, 'src', 'x.tar'), 'wb') as f:
            f.write(b'x')
        os.chmod(os.path.join(self.t, 'src', 'x.tar'), 0o644)   # only the link itself may be the reason
        for name, make in cases.items():
            with self.subTest(name):
                make()
                code, out, err = self.offer('seed m2')
                self.assertEqual((code, out), (4, b''), err)
                os.chmod(files, 0o755)
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
        path, send = os.path.join(self.t, 'files', 'm2.tar'), seed.send

        def changing(fd, out):
            with open(path, 'ab') as f:   # made in place, not by rename
                f.write(b'\0' * 512)
            return send(fd, out)
        with mock.patch.object(seed, 'send', changing):
            code, _, err = self.offer('seed m2')
        self.assertEqual(code, 4)
        self.assertIn('changed while it was sent', err)

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
        sync = mock.patch.object(seed.os, 'sync')
        sync.start()
        self.addCleanup(sync.stop)

    def run_seed(self, now=None):
        self.said = []
        return seed.run(agent, self.conf, self.root, now=now or time.time, say=self.said.append)

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
        label = json.load(open(os.path.join(seed.current(self.root, 'm2')[0], 'label.json')))
        self.assertEqual(label['bytes'], os.path.getsize(os.path.join(self.t, 'files', 'm2.tar')))
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

    def test_a_tar_that_would_leave_its_folder_writes_nothing_outside(self):
        bad = {'a ../ path': ('../../escaped', tarfile.REGTYPE, ''), 'a device': ('escaped', tarfile.CHRTYPE, ''),
               'an absolute link': ('escaped', tarfile.SYMTYPE, '/'),
               'a link up': ('escaped', tarfile.SYMTYPE, '../../..'),
               'a hard link out': ('escaped', tarfile.LNKTYPE, '/etc/hostname')}
        for why, (name, kind, target) in bad.items():
            def evil():
                tmp = os.path.join(self.t, 'files', '.m2.tmp')
                with tarfile.open(tmp, 'w') as tar:   # one bad member each: the filter stops at the first
                    ok = tarfile.TarInfo('ok')
                    ok.size = 1
                    tar.addfile(ok, io.BytesIO(b'x'))
                    info = tarfile.TarInfo(name)
                    info.type, info.linkname = kind, target
                    tar.addfile(info)
                os.utime(tmp, (time.time() + 5,) * 2)
                os.replace(tmp, os.path.join(self.t, 'files', 'm2.tar'))
            with self.subTest(why):
                shutil.rmtree(self.root, True)
                self.torn(evil, 'Error')
                for d, dirs, names in os.walk(self.t):
                    self.assertNotIn('escaped', dirs + names, d)

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
        self.config([SEEDS[1]])
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
        os.symlink('/etc', os.path.join(self.root, 'npm'))                    # not a link this program makes
        os.unlink(os.path.join(seed.current(self.root, 'm2')[0], 'label.json'))
        self.run_seed()
        self.assertEqual(os.listdir(os.path.join(self.root, '.new')), ['.lock'])
        self.assertFalse(os.path.lexists(os.path.join(self.root, 'npm')))
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
