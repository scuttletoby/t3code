"""t3-golive rehearse (R0-R5) on a fake machine (tests/golive_fixture.py): the T3 home is a temp folder, the old
build (the live home's runtime) and the fork build (the staged release) are one stub `t3` that keeps its migrations
in a real SQLite file. Nothing here starts, stops or reads a real T3 or ~/.t3; every stub run is checked to stay in
the rehearsal home.  python3 -I -B fork-kit/golive/tests/rehearse.test.py"""
import hashlib, json, os, socket, sqlite3, subprocess, sys, time, types, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golive_fixture as fx  # noqa: E402
from golive import check, coldcopy, config, journal, rehearse  # noqa: E402

OLD_HEAD, NEW_HEAD = 81, 83

# The stub t3. Its version and migration head are fixed per copy; <root>/stub-mode.json (keyed by version) makes it
# misbehave: "noHoldWork" (help without the flag), "neverReady", "dieInReport" (the report kills the server: dying
# mid-R4), "touchSettings" (the server rewrites settings.json), "report" (keys laid over the report while a server
# runs, that is in R4), "migrateNothing" (R2's command exits 0 and migrates nothing), "serveMigrates" (the server
# migrates at start, as upstream's does when R2 left migrations pending).
STUB = r'''#!{python} -I
import json, os, signal, socket, sqlite3, sys, time
VERSION, HEAD, ROOT = {version!r}, {head!r}, {root!r}
try:
    with open(os.path.join(ROOT, 'stub-mode.json')) as f:
        M = json.load(f).get(VERSION, {{}})
except FileNotFoundError:
    M = {{}}
a = sys.argv[1:]
with open(os.path.join(ROOT, 'stub-t3-calls.jsonl'), 'a') as f:
    f.write(json.dumps({{'version': VERSION, 'argv': a, 'env': dict(os.environ), 'cwd': os.getcwd()}}) + '\n')
def arg(n):
    return a[a.index(n) + 1]
def ids(db):
    c = sqlite3.connect('file:%s?mode=ro' % db, uri=True)
    try:
        return sorted(r[0] for r in c.execute('SELECT id FROM migrations'))
    finally:
        c.close()
if a[0] == '__service-preflight':
    got = ids(arg('--database-path'))
    newer = [i for i in got if i > HEAD]
    print(json.dumps({{'status': 'blocked', 'version': VERSION, 'reason': 'unknown migrations %s' % newer}} if newer
                     else {{'status': 'ready', 'version': VERSION,
                           'launcherProtocol': int(arg('--launcher-protocol'))}}))
elif a[:3] == ['auth', 'session', 'list']:
    ud = os.path.join(arg('--base-dir'), 'userdata')
    if os.path.exists(os.path.join(ud, 'server-runtime.json')):
        sys.exit('a server runs: live mode')
    if M.get('migrateNothing'):
        print(json.dumps([]))
        sys.exit(0)
    c = sqlite3.connect(os.path.join(ud, 'statev2.sqlite'))
    for i in range(max(ids(os.path.join(ud, 'statev2.sqlite'))) + 1, HEAD + 1):
        c.execute('INSERT INTO migrations VALUES (?)', (i,))
    c.execute('CREATE TABLE IF NOT EXISTS grown (x)')
    c.execute('INSERT INTO grown VALUES (zeroblob(65536))')
    c.commit()
    c.close()
    print(json.dumps([]))
elif a[0] == '__fork-report':
    ud = os.path.join(arg('--base-dir'), 'userdata')
    db = os.path.join(ud, 'statev2.sqlite')
    got = ids(db)
    c = sqlite3.connect('file:%s?mode=ro' % db, uri=True)
    counts = {{k: c.execute('SELECT COUNT(*) FROM %s' % k).fetchone()[0] for k in ('projects', 'threads')}}
    c.close()
    with open(os.path.join(ud, 'settings.json')) as f:
        s = json.load(f)
    r = {{'reportVersion': 1, 'version': VERSION,
         'database': {{'state': 'present', 'appliedMigrations': [{{'id': i, 'name': 'm%d' % i}} for i in got],
                      'pendingMigrations': list(range(max(got) + 1, HEAD + 1)), 'unknownMigrations': [],
                      'counts': dict(counts, projectsArchived=0, threadsArchived=0)}},
         'settings': {{'state': 'present', 'parseWarning': None, 'providerInstances': s.get('providerInstances', [])}},
         'fork': {{'featuresOn': [], 'features': {{}}}}, 'problems': [], 'ok': True}}
    rt = os.path.join(ud, 'server-runtime.json')
    if os.path.exists(rt):
        r.update(M.get('report', {{}}))
        if M.get('dieInReport'):
            with open(rt) as f:
                os.kill(json.load(f)['pid'], signal.SIGKILL)
    print(json.dumps(r))
elif a[0] == 'serve' and '--help' in a:
    print('USAGE t3 serve [--host] [--port] [--no-browser] [--base-dir]'
          + ('' if M.get('noHoldWork') else ' [--hold-work]'))
elif a[0] == 'serve':
    with open(os.path.join(ROOT, 'serve-pids'), 'a') as f:
        f.write('%d\n' % os.getpid())
    if M.get('neverReady'):
        time.sleep(600)
    ud = os.path.join(arg('--base-dir'), 'userdata')
    s = socket.socket()
    s.bind((arg('--host'), int(arg('--port'))))  # a port taken: the server exits, as upstream's does
    s.listen()
    owner = sqlite3.connect(os.path.join(ud, 'server-owner.sqlite'), isolation_level=None)
    owner.execute('BEGIN EXCLUSIVE')
    if M.get('serveMigrates'):
        c = sqlite3.connect(os.path.join(ud, 'statev2.sqlite'))
        for i in range(max(ids(os.path.join(ud, 'statev2.sqlite'))) + 1, HEAD + 1):
            c.execute('INSERT INTO migrations VALUES (?)', (i,))
        c.commit()
        c.close()
    rt = os.path.join(ud, 'server-runtime.json')
    def stop(*_):
        os.unlink(rt)
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    with open(rt, 'w') as f:
        json.dump({{'version': 1, 'pid': os.getpid(), 'port': int(arg('--port')), 'serverVersion': VERSION,
                   'workHeld': ('--hold-work' in a and not M.get('workNotHeld')) or None}}, f)
    if M.get('touchSettings'):
        with open(os.path.join(ud, 'settings.json'), 'a') as f:
            f.write('\n')
    while True:
        time.sleep(1)
'''

HOLD_OWNER = '''import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute('BEGIN EXCLUSIVE')
print('ready', flush=True)
time.sleep(60)
'''

# A rehearsal in its own process, SIGKILLed where `at` says: when a step starts, or inside the restore.
KILLED = '''import os, sys
sys.path[:0] = {paths!r}
from golive import coldcopy, config, journal, rehearse
config.MACHINE_FILE, config.MACHINE_OWNER = {machine!r}, os.getuid()
at = {at!r}
append = journal.Run._append
def killing_append(self, event, **fields):
    append(self, event, **fields)
    if fields.get('step') == at and fields.get('phase') == 'started':
        os.kill(os.getpid(), 9)
journal.Run._append = killing_append
keep = coldcopy._keep
def killing_keep(*args):
    if at == 'mid-restore':
        os.kill(os.getpid(), 9)
    return keep(*args)
coldcopy._keep = killing_keep
rehearse.run_rehearse(config.load(), say=lambda line: None)
'''

# A rehearsal in its own process, SIGKILLed while it removes the old rehearsal folder: `mid-tree` inside the old
# clone's files, `last` at the removal of the (by then empty) folder itself.
KILLED_DELETE = '''import os, sys
sys.path[:0] = {paths!r}
from golive import config, osadapter, rehearse
config.MACHINE_FILE, config.MACHINE_OWNER = {machine!r}, os.getuid()
osadapter.MOUNTINFO = {mountinfo!r}
at = {at!r}
unlink, rmdir, listdir = os.unlink, os.rmdir, os.listdir
os.listdir = lambda p='.': sorted(listdir(p))  # the mark ('.golive-rehearsal') first: the worst order for it
def killing_unlink(p, *a, **kw):
    if at == 'mid-tree' and os.path.basename(os.fspath(p)) == 'statev2.sqlite':
        os.kill(os.getpid(), 9)
    return unlink(p, *a, **kw)
def killing_rmdir(p, *a, **kw):
    if at == 'last' and os.path.basename(os.fspath(p)) == rehearse.REHEARSAL:
        os.kill(os.getpid(), 9)
    return rmdir(p, *a, **kw)
os.unlink, os.rmdir = killing_unlink, killing_rmdir
rehearse.run_rehearse(config.load(), say=lambda line: None)
'''

MOUNTS = (  # a fake /proc/self/mountinfo: no mount near the machine's folders unless a test adds one
    '22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw,errors=remount-ro\n'
    '23 22 0:21 / /proc rw,nosuid,nodev,noexec,relatime shared:12 - proc proc rw\n'
    '24 22 0:31 / /mnt/with\\040space rw,relatime shared:13 - tmpfs tmpfs rw\n')


def escaped(path):
    """A path as mountinfo writes it: space, tab, newline and backslash as octal escapes."""
    return ''.join('\\%03o' % ord(ch) if ch in ' \t\n\\' else ch for ch in path)


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def read_json(path):
    with open(path) as f:
        return json.load(f)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.m = m = fx.Machine()
        self.addCleanup(self.m.close)
        m.standard()
        fx.kill(m.server)  # the live server is not needed: a rehearsal never looks at it; its binary becomes the stub
        m.server = None
        m.stage_release(fx.TARGET, files={'t3': self.stub(fx.TARGET, NEW_HEAD), 'lib/runtime.txt': 'runtime'})
        m.write(f'home/runtime/versions/{fx.LIVE}/t3', self.stub(fx.LIVE, OLD_HEAD), mode=0o755)
        self.db(m.path('home/userdata/statev2.sqlite'))
        m.write('home/userdata/settings.json', json.dumps({'providerInstances': [
            {'id': 'codex', 'driver': 'codex', 'enabled': True},
            {'id': 'claude', 'driver': 'claude', 'enabled': True}]}))
        m.write('home/userdata/secrets/token', 'not for a rehearsal', mode=0o600)
        m.write('home/userdata/server-owner.sqlite', b'')
        self.copy_dir = self.make_copy()
        patcher = mock.patch.multiple(rehearse, POLL=0.02, READY_WAIT=5, STOP_WAIT=5)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mountinfo = m.write('mountinfo', MOUNTS)
        patcher = mock.patch.object(fx.osadapter, 'MOUNTINFO', self.mountinfo, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.said = []

    def mount(self, path):
        """A mount point at `path` in the fake mount list (no root here to mount one)."""
        with open(self.mountinfo, 'a') as f:
            f.write('90 22 0:44 /x %s rw,relatime - tmpfs tmpfs rw\n' % escaped(path))

    def stub(self, version, head):
        return STUB.format(python=sys.executable, version=version, head=head, root=self.m.root)

    def db(self, path):
        os.unlink(path)
        c = sqlite3.connect(path)
        c.execute('CREATE TABLE migrations (id INTEGER)')
        c.executemany('INSERT INTO migrations VALUES (?)', [(i,) for i in range(1, OLD_HEAD + 1)])
        c.execute('CREATE TABLE projects (id)')
        c.execute('CREATE TABLE threads (id)')
        c.executemany('INSERT INTO projects VALUES (?)', [(i,) for i in range(3)])
        c.executemany('INSERT INTO threads VALUES (?)', [(i,) for i in range(40)])
        c.commit()
        c.close()

    def make_copy(self, name=f't3-copy-20990101T000000Z-{fx.LIVE}', version=fx.LIVE, extra=None):
        """A finished copy as `copy` writes it (coldcopy.plan of the home, every file hashed, manifest last)."""
        dest = self.m.path(f'backups/{name}')
        os.makedirs(os.path.join(dest, 'userdata'))
        src = self.m.path('home/userdata')
        entries = coldcopy.plan(src)[0]
        for e in entries + [dict(x) for x in (extra or [])]:
            target = os.path.join(dest, 'userdata', e['path'])
            if e['kind'] == 'dir':
                os.makedirs(target, exist_ok=True)
            else:
                with open(os.path.join(src, e['path']), 'rb') as f:
                    data = f.read()
                with open(target, 'wb') as f:
                    f.write(data)
                e['sha256'] = hashlib.sha256(data).hexdigest()
        files = entries + [dict(x, sha256=sha(os.path.join(dest, 'userdata', x['path']))) for x in (extra or [])]
        with open(os.path.join(dest, coldcopy.MANIFEST), 'w') as f:
            json.dump({'manifestVersion': 1, 'complete': True, 'home': self.m.home, 'liveVersion': version,
                       'files': files}, f)
        return dest

    def mode(self, version, **flags):
        path = self.m.path('stub-mode.json')
        modes = read_json(path) if os.path.exists(path) else {}
        modes.setdefault(version, {}).update(flags)
        self.m.write('stub-mode.json', json.dumps(modes))

    def rehearse(self, **kw):
        return rehearse.run_rehearse(config.load(), say=self.said.append, **kw)

    def root(self):
        return os.path.join(self.m.backups, rehearse.REHEARSAL)

    def runs(self):
        return journal.read(self.m.state)[0]

    def steps(self, run=None):
        return [(s['step'], s['phase']) for s in (run or self.runs()[-1])['steps']]

    def stub_calls(self):
        return self.m.calls('stub-t3-calls.jsonl')

    def refused(self, needle, **kw):
        with self.assertRaises(journal.Refused) as e:
            self.rehearse(**kw)
        self.assertIn(needle, str(e.exception))
        self.assertEqual(self.steps(), [], 'a refusal comes before the first step')
        return str(e.exception)

    def failed(self, needle, **kw):
        with self.assertRaises(rehearse.Failed) as e:
            self.rehearse(**kw)
        self.assertIn(needle, str(e.exception))
        last = self.runs()[-1]
        self.assertEqual(last['end']['result'], 'failed')
        self.assertFalse(journal.needs_settling(last), 'a rehearsal changes only its own clone')
        self.assertFalse(self.server_left())
        return str(e.exception)

    def server_left(self):
        """A rehearsal server still runs (every serve the stub started logs its pid)."""
        try:
            with open(self.m.path('serve-pids')) as f:
                pids = [int(line) for line in f]
        except FileNotFoundError:
            return False
        return any(self.alive(pid) for pid in pids)

    def alive(self, pid):
        for _ in range(100):  # a server killed with its run (PR_SET_PDEATHSIG) is gone within moments
            try:
                with open(f'/proc/{pid}/stat') as f:
                    if f.read().split(') ', 1)[1][0] == 'Z':
                        return False
            except OSError:
                return False
            time.sleep(0.02)
        return True


class Rehearse(Base):
    def test_a_rehearsal_runs_r0_to_r5_on_a_clone_and_journals_the_result(self):
        live, copy = self.m.snapshot(), self.m.snapshot(self.copy_dir)
        result = self.rehearse()
        self.assertEqual(self.m.snapshot(), live, 'the live home is never touched')
        self.assertEqual(self.m.snapshot(self.copy_dir), copy, 'the copy is never changed')
        self.assertEqual([s for s, phase in self.steps() if phase == 'done'],
                         ['clone', 'preflight', 'migrate', 'serve', 'checks', 'stop-server', 'old-build-blocked',
                          'restore', 'result'])
        self.assertEqual(self.runs()[-1]['end']['result'], 'ok')
        facts = self.runs()[-1]['steps'][-1]['data']
        self.assertEqual(facts, result)
        self.assertEqual((result['fromVersion'], result['targetVersion']), (fx.LIVE, fx.TARGET))
        self.assertEqual(result['migrations'], [82, 83])
        self.assertEqual(result['copySha256'], sha(os.path.join(self.copy_dir, coldcopy.MANIFEST)))
        self.assertEqual(result['copy'], self.copy_dir)
        self.assertGreater(result['bytesGrown'], 0)
        for key in ('migrateSeconds', 'readySeconds', 'totalSeconds'):
            self.assertIsInstance(result[key], float)
        # Every t3 ran on the rehearsal home only, with an empty HOME, a bare PATH and nothing else from this env.
        home = os.path.join(self.root(), 'home')
        calls = self.stub_calls()
        for c in calls:
            where = c['argv'][c['argv'].index('--base-dir') + 1] if '--base-dir' in c['argv'] else \
                os.path.dirname(os.path.dirname(c['argv'][c['argv'].index('--database-path') + 1])) \
                if '--database-path' in c['argv'] else home
            self.assertEqual(where, home, c['argv'])
            self.assertEqual(set(c['env']) - {'PWD', 'LC_CTYPE'}, {'HOME', 'PATH', 'LANG'}, c['argv'])
            self.assertEqual(c['env']['PATH'], '/usr/bin:/bin')
            self.assertNotIn(c['env']['HOME'], (os.path.expanduser('~'), self.m.home))
        serve = [c['argv'] for c in calls if c['argv'][0] == 'serve' and '--help' not in c['argv']]
        self.assertEqual(len(serve), 1)
        self.assertEqual(serve[0][:7], ['serve', '--hold-work', '--host', '127.0.0.1', '--port', str(result['port']),
                                        '--no-browser'])
        # R1 old ready, fork ready; R5 old blocked on the migrated clone, then ready on the restored one.
        pre = [(c['version'], c['argv'][2]) for c in calls if c['argv'][0] == '__service-preflight']
        self.assertEqual([v for v, _ in pre], [fx.LIVE, fx.TARGET, fx.LIVE, fx.LIVE])
        self.assertEqual(result['oldBuildOnMigrated'], 'blocked')
        r5 = [s['data'] for s in self.runs()[-1]['steps'] if s['step'] == 'old-build-blocked' and s['phase'] == 'done']
        self.assertIn('unknown migrations', r5[0]['reason'], 'why the old build is blocked stays in the journal')
        # The clone holds no secrets, no runtime record and no ownership lock of the live home.
        self.assertNotIn('secrets', os.listdir(os.path.join(home, 'userdata')))
        self.assertFalse(self.server_left())

    def test_a_run_again_throws_the_old_clone_away_and_redoes_it(self):
        self.rehearse()
        half = os.path.join(self.root(), 'home', 'userdata', 'statev2.sqlite')
        c = sqlite3.connect(half)
        c.execute('INSERT INTO migrations VALUES (82)')  # an old rehearsal home, half migrated
        c.commit()
        c.close()
        result = self.rehearse()
        self.assertEqual(result['migrations'], [82, 83], 'measured on a fresh clone, never the half-migrated one')

    def test_an_incomplete_copy_is_refused(self):
        os.unlink(os.path.join(self.copy_dir, coldcopy.MANIFEST))
        self.refused('incomplete')
        self.refused('incomplete', copy_dir=self.copy_dir)
        self.assertFalse(os.path.lexists(self.root()))

    def test_a_copy_that_differs_from_its_manifest_is_refused(self):
        with open(os.path.join(self.copy_dir, 'userdata', 'settings.json'), 'a') as f:
            f.write(' ')
        self.refused('does not match its manifest')

    def test_a_copy_from_another_build_is_refused(self):
        self.m.write('home/runtime/service-state.json', json.dumps({'protocol': 3, 'activeVersion': '0.1.9-other.1'}))
        self.refused('take a new copy')

    def test_a_copy_of_another_home_is_refused(self):
        other = self.make_copy(name='t3-copy-20990101T000001Z-x')
        m = read_json(os.path.join(other, coldcopy.MANIFEST))
        m['home'] = '/elsewhere'
        with open(os.path.join(other, coldcopy.MANIFEST), 'w') as f:
            json.dump(m, f)
        self.refused('not of')

    def test_the_newest_copy_is_rehearsed_by_default(self):
        newer = self.make_copy(name=f't3-copy-20990102T000000Z-{fx.LIVE}')
        self.assertEqual(self.rehearse()['copy'], newer)

    def test_a_target_not_newer_than_the_copy_is_refused(self):
        self.configure(targetVersion=fx.LIVE)
        self.refused('not newer')

    def configure(self, **over):
        self.m.config(**over)

    def test_a_build_without_hold_work_is_refused_before_any_step(self):
        self.mode(fx.TARGET, noHoldWork=True)
        self.refused('--hold-work')
        self.assertFalse(os.path.lexists(self.root()))

    def test_the_port_must_be_free_and_not_the_live_port(self):
        self.refused('the live port', port=3773)
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            s.listen()
            self.refused('in use', port=s.getsockname()[1])

    def test_a_port_taken_between_the_check_and_the_start_fails_and_the_rerun_passes(self):
        port = free_port()
        with socket.socket() as s, mock.patch.object(rehearse, '_port_free', return_value=True):
            s.bind(('127.0.0.1', port))
            s.listen()
            self.failed('exited before it was ready', port=port)
        self.assertEqual(self.rehearse(port=port)['port'], port)

    def test_a_server_never_ready_is_stopped_and_fails(self):
        self.mode(fx.TARGET, neverReady=True)
        self.failed('not ready within')
        calls = [c for c in self.stub_calls() if c['argv'][0] == 'serve' and '--help' not in c['argv']]
        self.assertEqual(len(calls), 1)

    def test_a_server_that_does_not_say_work_is_held_is_stopped_and_fails(self):
        self.mode(fx.TARGET, workNotHeld=True)
        self.failed('not port')

    def test_a_clone_that_does_not_read_back_as_the_manifest_fails(self):
        real = coldcopy._copy_file
        with mock.patch.object(coldcopy, '_copy_file', side_effect=lambda *a: (real(*a)[0], '0' * 64)):
            self.failed('does not read back')

    def test_a_server_dying_mid_r4_fails(self):
        self.mode(fx.TARGET, dieInReport=True)
        self.failed('the rehearsal server died')

    def test_settings_changed_by_the_run_fail(self):
        self.mode(fx.TARGET, touchSettings=True)
        self.failed('settings.json changed')

    def test_each_r4_check_fails_on_its_own(self):
        cases = [({'version': '9.9.9'}, 'version'),
                 ({'fork': {'featuresOn': ['waitFor'], 'features': {}}}, 'fork features on'),
                 ({'settings': {'state': 'present', 'parseWarning': 'bad', 'providerInstances': [
                     {'id': 'codex'}, {'id': 'claude'}]}}, 'parse warning'),
                 ({'settings': {'state': 'present', 'parseWarning': None, 'providerInstances': [{'id': 'codex'}]}},
                  'provider instances'),
                 ({'database': {'appliedMigrations': [{'id': i} for i in range(1, 84)], 'pendingMigrations': [],
                                'unknownMigrations': [], 'counts': {'projects': 3, 'projectsArchived': 0,
                                                                    'threads': 39, 'threadsArchived': 0}}}, 'threads'),
                 ({'database': {'appliedMigrations': [{'id': i} for i in range(1, 83)], 'pendingMigrations': [83],
                                'unknownMigrations': [], 'counts': {'projects': 3, 'projectsArchived': 0,
                                                                    'threads': 40, 'threadsArchived': 0}}},
                  'migrations')]
        for over, needle in cases:
            with self.subTest(needle):
                self.mode(fx.TARGET, report=over)
                self.failed(needle)

    def test_old_build_ready_on_the_migrated_clone_fails(self):
        self.m.write(f'home/runtime/versions/{fx.LIVE}/t3', self.stub(fx.LIVE, NEW_HEAD), mode=0o755)
        self.failed('should be blocked')

    def test_a_copy_the_old_build_itself_calls_blocked_fails_at_r1(self):
        c = sqlite3.connect(self.m.path('home/userdata/statev2.sqlite'))
        c.execute('INSERT INTO migrations VALUES (82)')
        c.commit()
        c.close()
        self.make_copy(name=f't3-copy-20990105T000000Z-{fx.LIVE}')
        self.failed('R1: the preflight of t3 ' + fx.LIVE)

    def test_with_no_migrations_the_old_build_stays_ready_and_the_rehearsal_passes(self):
        self.m.stage_release(fx.TARGET, files={'t3': self.stub(fx.TARGET, OLD_HEAD), 'lib/runtime.txt': 'runtime'})
        result = self.rehearse()
        self.assertEqual(result['migrations'], [])
        self.assertEqual(result['oldBuildOnMigrated'], 'ready')
        self.assertIn('R2: no migrations were pending', self.said)

    def test_with_no_migrations_an_old_build_saying_blocked_fails_and_says_it_should_be_ready(self):
        self.m.stage_release(fx.TARGET, files={'t3': self.stub(fx.TARGET, OLD_HEAD), 'lib/runtime.txt': 'runtime'})
        real, seen = rehearse._preflight, []

        def blocked_in_r5(binary, home, protocol, version):
            seen.append(version)
            got = real(binary, home, protocol, version)
            return ('blocked', 'something else') if len(seen) == 3 else got
        with mock.patch.object(rehearse, '_preflight', side_effect=blocked_in_r5):
            text = self.failed('says blocked; it should be ready (no migrations)')
        self.assertNotIn('should be blocked', text)
        self.assertIn('something else', text, 'the preflight\'s reason is named')

    def test_a_restore_that_leaves_the_clone_migrated_fails(self):
        with mock.patch.object(coldcopy, 'restore', return_value={}):  # the copy never put back
            self.failed('on the restored clone says blocked')

    def test_r2_that_migrates_nothing_fails_before_any_server_migrates_unmeasured(self):
        self.mode(fx.TARGET, migrateNothing=True, serveMigrates=True)
        self.failed('R2:')
        self.assertFalse([c for c in self.stub_calls() if c['argv'][0] == 'serve' and '--help' not in c['argv']],
                         'R3 never starts on a clone R2 did not migrate')

    def test_r4_migration_checks_each_fail_alone(self):
        counts = {'projects': 3, 'projectsArchived': 0, 'threads': 40, 'threadsArchived': 0}
        cases = [({'appliedMigrations': [{'id': i} for i in range(1, 84)], 'pendingMigrations': [84],
                   'unknownMigrations': [], 'counts': counts}, 'still pending [84]'),
                 ({'appliedMigrations': [{'id': i} for i in range(1, 83)], 'pendingMigrations': [],
                   'unknownMigrations': [], 'counts': counts}, 'applied [82], expected [82, 83]')]
        for over, needle in cases:
            with self.subTest(needle):
                self.mode(fx.TARGET, report={'database': over})
                self.failed(needle)

    def test_a_copy_with_no_provider_instance_fails(self):
        self.m.write('home/userdata/settings.json', json.dumps({}))
        self.make_copy(name=f't3-copy-20990106T000000Z-{fx.LIVE}')
        self.failed('provider instances')


class Guards(Base):
    def test_the_live_home_by_a_symlink_is_refused_and_never_removed(self):
        cfg = config.load()
        cfg.backup_dir = self.m.path('links')
        os.makedirs(cfg.backup_dir)
        os.symlink(self.m.home, os.path.join(cfg.backup_dir, rehearse.REHEARSAL))
        self.m.write('home/.golive-rehearsal', '{}')  # even with the kit's mark in it
        live = self.m.snapshot()
        with self.assertRaises(journal.Refused) as e:
            rehearse.run_rehearse(cfg, copy_dir=self.copy_dir, say=self.said.append)
        self.assertIn('is the live T3 home', str(e.exception))
        self.assertEqual(self.m.snapshot(), live)

    def bind(self, path, as_path):
        """os.stat of `path` gives the folder `as_path`: a bind mount (no root here to mount one)."""
        real, same = (os.stat, os.lstat), os.stat(as_path)

        def fake(i):
            return lambda p, *a, **kw: same if os.fspath(p) == path else real[i](p, *a, **kw)
        return mock.patch.multiple(rehearse.os, stat=fake(0), lstat=fake(1))

    def test_the_rehearsal_home_inside_the_live_home_by_a_bind_path_is_refused_by_file_id(self):
        cfg = config.load()
        cfg.backup_dir = self.m.path('bound')  # the live home, bound here
        os.makedirs(cfg.backup_dir)
        with self.bind(cfg.backup_dir, self.m.home), self.assertRaises(journal.Refused) as e:
            rehearse.run_rehearse(cfg, copy_dir=self.copy_dir, say=self.said.append)
        self.assertIn('is the live T3 home', str(e.exception))
        self.assertEqual(os.listdir(cfg.backup_dir), [])

    def test_the_live_home_inside_the_rehearsal_home_by_a_bind_path_is_refused_by_file_id(self):
        root = self.root()
        self.m.write(f'backups/{rehearse.REHEARSAL}/.golive-rehearsal', '{}')
        with self.bind(root, self.m.root), self.assertRaises(journal.Refused) as e:  # bound: the home's parent
            self.rehearse()
        self.assertIn('is the live T3 home', str(e.exception))
        self.assertTrue(os.path.exists(os.path.join(root, '.golive-rehearsal')))
        self.assertEqual(self.steps(), [])

    def test_the_live_home_bound_inside_an_old_rehearsal_folder_is_never_removed(self):
        self.rehearse()
        inside = os.path.join(self.root(), 'home', 'attachments-bound')  # the live home, bound in an old clone
        os.makedirs(inside)
        with self.bind(inside, self.m.home), self.assertRaises(journal.Refused) as e:
            self.rehearse()
        self.assertIn('the live T3 home or a mount point', str(e.exception))
        self.assertTrue(os.path.isdir(inside))
        self.assertEqual(self.steps(), [])

    def test_the_live_port_named_by_the_live_runtime_record_is_refused(self):
        port = free_port()
        self.m.write('home/userdata/server-runtime.json', json.dumps({'version': 1, 'pid': 1, 'port': port}))
        self.refused('the live port', port=port)

    def test_a_rehearsal_folder_that_is_a_link_is_refused_and_never_followed(self):
        self.m.write('backups/elsewhere/.golive-rehearsal', '{}')
        self.m.write('backups/elsewhere/keep.txt', 'x')
        os.symlink(self.m.path('backups/elsewhere'), self.root())
        self.refused('a link')
        self.assertEqual(sorted(os.listdir(self.m.path('backups/elsewhere'))), ['.golive-rehearsal', 'keep.txt'])

    def test_a_mount_point_in_an_old_rehearsal_folder_is_refused_and_nothing_removed(self):
        self.rehearse()
        home = os.path.join(self.root(), 'home')
        spaced = os.path.join(home, 'userdata', 'with space')
        os.makedirs(spaced)
        for where in (os.path.join(home, 'userdata', 'attachments'), spaced, home, self.root()):
            with self.subTest(where):
                with open(self.mountinfo, 'w') as f:
                    f.write(MOUNTS)
                self.mount(where)  # the same filesystem bound there: st_dev cannot tell
                self.refused('mount point')
                self.assertTrue(os.path.isfile(os.path.join(home, 'userdata', 'statev2.sqlite')))

    def test_a_mount_beside_the_rehearsal_folder_is_not_inside_it(self):
        self.rehearse()
        self.mount(self.root() + '-other')
        self.mount(os.path.dirname(self.root()))
        self.rehearse()

    def test_a_mount_list_that_cannot_be_read_refuses_the_removal(self):
        self.rehearse()
        for case in ('missing', 'empty', 'other-os'):
            with self.subTest(case):
                self.m.write('mountinfo', MOUNTS)
                if case == 'missing':
                    os.unlink(self.mountinfo)
                elif case == 'empty':
                    self.m.write('mountinfo', '')
                other_os = types.SimpleNamespace(platform='darwin' if case == 'other-os' else sys.platform)
                with mock.patch.object(fx.osadapter, 'sys', other_os):
                    self.refused('mount list')
                self.assertTrue(os.path.isfile(os.path.join(self.root(), 'home', 'userdata', 'statev2.sqlite')))
        self.m.write('mountinfo', MOUNTS)
        self.rehearse()

    def test_the_real_mount_list_reads(self):
        if not os.path.exists('/proc/self/mountinfo'):
            self.skipTest('no /proc here')
        with mock.patch.object(fx.osadapter, 'MOUNTINFO', '/proc/self/mountinfo'):
            self.assertIn('/', fx.osadapter.mount_points())
        self.assertIn('/mnt/with space', fx.osadapter.mount_points())

    def test_another_device_in_an_old_rehearsal_folder_is_refused(self):
        self.rehearse()
        inside = os.path.join(self.root(), 'home', 'userdata')
        st, real = os.lstat(inside), os.lstat
        other = os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, st.st_nlink, st.st_uid, st.st_gid,
                                st.st_size, int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))
        with mock.patch.object(rehearse.os, 'lstat', lambda p, *a, **kw: other if os.fspath(p) == inside
                               else real(p, *a, **kw)):
            self.refused('mount point')
        self.assertTrue(os.path.isfile(os.path.join(inside, 'statev2.sqlite')))

    def test_a_mount_or_a_swap_between_the_check_and_the_removal_is_caught(self):
        self.rehearse()
        home = os.path.join(self.root(), 'home')
        append = journal.Run._append

        def swap(how):
            def at_clone(run, event, **fields):
                append(run, event, **fields)
                if fields.get('step') == 'clone' and fields.get('phase') == 'started':
                    if how == 'mount':
                        self.mount(os.path.join(home, 'userdata'))
                    else:
                        os.rename(self.root(), self.root() + '.moved')
                        os.symlink(self.m.home, self.root())
            return mock.patch.object(journal.Run, '_append', at_clone)
        live = self.m.snapshot()
        with swap('mount'):
            self.failed('mount point')
        self.assertTrue(os.path.isfile(os.path.join(home, 'userdata', 'statev2.sqlite')))
        with open(self.mountinfo, 'w') as f:
            f.write(MOUNTS)
        with swap('link'):
            self.failed('live T3 home')
        self.assertEqual(self.m.snapshot(), live)
        self.assertTrue(os.path.islink(self.root()))

    def test_a_link_in_an_old_clone_never_takes_another_homes_lock(self):
        self.rehearse()
        elsewhere = self.m.path('elsewhere-userdata')
        os.makedirs(elsewhere)
        open(os.path.join(elsewhere, coldcopy.OWNER_LOCK), 'w').close()
        ud = os.path.join(self.root(), 'home', 'userdata')
        os.rename(ud, ud + '.old')
        os.symlink(elsewhere, ud)
        with mock.patch.object(coldcopy, 'hold_owner_lock', side_effect=AssertionError('lock taken')) as held:
            self.refused('a link')
        held.assert_not_called()

    def test_a_copy_inside_the_rehearsal_folder_is_refused(self):
        self.rehearse()
        inside = os.path.join(self.root(), os.path.basename(self.copy_dir))
        os.rename(self.copy_dir, inside)
        self.refused('inside the rehearsal folder', copy_dir=inside)
        self.assertTrue(os.path.isfile(os.path.join(inside, coldcopy.MANIFEST)))
        linked = self.m.path(f'backups/t3-copy-20990109T000000Z-{fx.LIVE}')
        os.symlink(inside, linked)  # newest by name: picked by default, a link into the rehearsal folder
        self.refused('inside the rehearsal folder')
        self.refused('inside the rehearsal folder', copy_dir=linked)
        self.assertTrue(os.path.isfile(os.path.join(inside, coldcopy.MANIFEST)))

    def test_device_and_relay_records_are_never_cloned(self):
        self.m.write('home/userdata/device/agent-device/daemon.json', '{"pid": 1}')
        self.m.write('home/userdata/device/agent-device/hub.json', '{"pid": 1}')
        self.m.write('home/userdata/relay-clients/1.json', '{"pid": 1}')
        self.make_copy(name=f't3-copy-20990107T000000Z-{fx.LIVE}')
        self.rehearse()
        names = os.listdir(os.path.join(self.root(), 'home', 'userdata'))
        self.assertFalse({'device', 'relay-clients'} & set(names))

    def test_a_folder_without_the_kit_mark_is_never_removed(self):
        self.m.write(f'backups/{rehearse.REHEARSAL}/someone-else.txt', 'x')
        self.refused('not made by a rehearsal')
        self.assertTrue(os.path.exists(os.path.join(self.root(), 'someone-else.txt')))

    def test_a_server_left_on_the_old_clone_is_refused(self):
        self.rehearse()
        owner = os.path.join(self.root(), 'home', 'userdata', 'server-owner.sqlite')
        p = subprocess.Popen([sys.executable, '-I', '-c', HOLD_OWNER, owner], stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.stdout.close)
        self.addCleanup(fx.kill, p)
        self.assertEqual(p.stdout.readline().strip(), 'ready')
        self.refused('still holds the old rehearsal home')

    def test_runtime_record_lock_and_secrets_are_never_cloned(self):
        # A hand-made copy holding a runtime record: it would point the CLI at the LIVE server.
        self.m.write('home/userdata/server-runtime.json', '{"version": 1, "pid": 1, "port": 3773}')
        self.make_copy(name=f't3-copy-20990103T000000Z-{fx.LIVE}', extra=[
            {'path': 'server-runtime.json', 'kind': 'file', 'bytes': 39, 'mode': 0o644}])
        self.rehearse()
        names = os.listdir(os.path.join(self.root(), 'home', 'userdata'))
        self.assertFalse({'secrets', 'server-runtime.json'} & set(names))

    def test_the_r0_guard_refuses_a_clone_holding_them(self):
        """With the clone's own leaving-out broken, R0 still finds each one before anything runs on the clone."""
        self.m.write('home/userdata/server-runtime.json', '{"version": 1, "pid": 1, "port": 3773}')
        self.m.write('home/userdata/device/agent-device/daemon.json', '{"pid": 1}')
        self.m.write('home/userdata/relay-clients/1.json', '{"pid": 1}')
        copy = self.make_copy(name=f't3-copy-20990103T000000Z-{fx.LIVE}', extra=[
            {'path': 'server-runtime.json', 'kind': 'file', 'bytes': 39, 'mode': 0o644},
            {'path': coldcopy.OWNER_LOCK, 'kind': 'file', 'bytes': 0, 'mode': 0o644}])
        with mock.patch.object(rehearse, 'NOT_CLONED', frozenset()), \
                mock.patch.object(coldcopy, 'SKIP', frozenset({'logs'})):
            with self.assertRaises(rehearse.Failed) as e:
                self.rehearse(copy_dir=copy)
        for name in rehearse.NEVER_IN_CLONE:
            self.assertIn(name, str(e.exception))
        self.assertFalse([c for c in self.stub_calls() if c['argv'][0] == '__service-preflight'])

    def test_a_stale_kit_lock_file_does_not_block(self):
        os.makedirs(self.m.state, exist_ok=True)
        with open(os.path.join(self.m.state, 'lock'), 'w') as f:
            f.write('{"pid": 999999, "command": "rehearse", "at": "2020-01-01T00:00:00Z"}')
        self.rehearse()

    def test_a_copy_run_that_needs_settling_still_blocks_a_rehearsal(self):
        with self.assertRaises(RuntimeError):
            with journal.run(self.m.state, 'copy') as r:
                with r.step('stop-service'):
                    raise RuntimeError('boom')
        with self.assertRaises(journal.Unfinished):
            self.rehearse()


class StoppedHalfWay(Base):
    def test_a_rehearsal_stopped_at_each_step_runs_again_with_nothing_to_settle(self):
        points = ['clone', 'preflight', 'migrate', 'serve', 'checks', 'stop-server', 'old-build-blocked', 'restore',
                  'mid-restore', 'result']
        for at in points:
            with self.subTest(at):
                code = KILLED.format(paths=[os.path.join(fx.KIT, 'lib'), fx.COMMON], machine=config.MACHINE_FILE,
                                     at=at)
                p = subprocess.run([sys.executable, '-I', '-B', '-c', code], stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(p.returncode, -9, p.stderr)
                last = self.runs()[-1]
                self.assertIsNone(last['end'])
                self.assertFalse(journal.needs_settling(last))
                self.assertFalse(self.server_left(), 'the server dies with the run that started it')
                code, result = check.run_check(config.load(), fx.FakeAdapter(self.m.root, self.m))
                self.assertNotIn('journal', [c['name'] for c in result['checks'] if not c['ok']])
                self.assertIn(f'(rehearse, pid {last["pid"]}', ' '.join(result['info']))
                self.assertIn('nothing to settle', ' '.join(result['info']))
                self.assertEqual(self.rehearse()['migrations'], [82, 83])

    def test_a_rehearsal_killed_while_it_removes_the_old_folder_runs_again(self):
        self.rehearse()
        for at in ('mid-tree', 'last'):
            with self.subTest(at):
                code = KILLED_DELETE.format(paths=[os.path.join(fx.KIT, 'lib'), fx.COMMON],
                                            machine=config.MACHINE_FILE, mountinfo=self.mountinfo, at=at)
                p = subprocess.run([sys.executable, '-I', '-B', '-c', code], stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(p.returncode, -9, p.stderr)
                self.assertTrue(os.path.isdir(self.root()), 'killed half-way: the folder is still there')
                if at == 'mid-tree':
                    self.assertTrue(os.path.isfile(os.path.join(self.root(), rehearse.MARK)), 'the mark goes last')
                self.assertFalse(journal.needs_settling(self.runs()[-1]))
                self.assertEqual(self.rehearse()['migrations'], [82, 83])


class CommandLine(Base):
    def test_rehearse_prints_its_result_and_exits_0_or_says_refused_and_exits_1(self):
        import io
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(check.osadapter, 'for_this_os', side_effect=AssertionError('no adapter needed')):
            self.assertEqual(check.main(['rehearse', '--json'], out, err), 0, err.getvalue())
            self.assertEqual(json.loads(out.getvalue())['migrations'], [82, 83])
            self.assertEqual(check.main(['rehearse', '--port', '3773'], out, err), 1)
        self.assertIn('Refused: port 3773 is the live port', err.getvalue())


class Journal(unittest.TestCase):
    def test_a_scratch_run_stopped_or_failed_needs_no_settling_and_says_so(self):
        m = fx.Machine()
        self.addCleanup(m.close)
        with self.assertRaises(RuntimeError):
            with journal.run(m.state, 'rehearse', scratch=True) as r:
                with r.step('clone'):
                    raise RuntimeError('boom')
        last = journal.read(m.state)[0][-1]
        self.assertTrue(last['scratch'])
        self.assertFalse(journal.needs_settling(last))
        with journal.run(m.state, 'copy'):
            pass  # not Unfinished


if __name__ == '__main__':
    unittest.main()
