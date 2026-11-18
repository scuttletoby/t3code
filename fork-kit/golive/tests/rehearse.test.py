"""t3-golive rehearse (R0-R5) on a fake machine (tests/golive_fixture.py): the T3 home is a temp folder, the old
build (the live home's runtime) and the fork build (the staged release) are one stub `t3` that keeps its migrations
in a real SQLite file. Nothing here starts, stops or reads a real T3 or ~/.t3; every stub run is checked to stay in
the rehearsal home.  python3 -I -B fork-kit/golive/tests/rehearse.test.py"""
import errno, hashlib, itertools, json, os, shutil, socket, sqlite3, subprocess, sys, time, types, unittest
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
copy_file = coldcopy._copy_file
def killing_copy_file(*args):
    if at == 'mid-copy':
        os.kill(os.getpid(), 9)
    return copy_file(*args)
coldcopy._copy_file = killing_copy_file
leaves = coldcopy.link_leaves
def capped_leaves(rel, look):  # one that never stops (no hop limit) fails at once, not at the parent's timeout
    calls = [0]
    def counted(path):
        calls[0] += 1
        if calls[0] > 1000:
            sys.exit('link_leaves(%r) looked up more than 1000 paths: it does not stop at a loop' % rel)
        return look(path)
    return leaves(rel, counted)
coldcopy.link_leaves = capped_leaves
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
        self.ids = itertools.count(80)

    def mount(self, path):
        """A mount point at `path` in the fake mount list (no root here to mount one)."""
        with open(self.mountinfo, 'a') as f:
            f.write('%d 22 0:44 /x %s rw,relatime - tmpfs tmpfs rw\n' % (next(self.ids), escaped(path)))

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

    def test_each_r2_check_fails_on_its_own(self):
        """R2's report must show every migration applied, none pending, none unknown: each alone stops before R3."""
        def pending(d):
            d['pendingMigrations'] = [84]

        def applied(d):  # 82 left out, nothing pending: only the applied list is wrong
            d['appliedMigrations'] = [m for m in d['appliedMigrations'] if m['id'] != 82]

        def unknown(d):
            d['unknownMigrations'] = [99]

        def no_pending_key(d):
            del d['pendingMigrations']
        real = rehearse._report
        for change, needle in ((pending, 'pending [84]'), (applied, 'applied [78, 79, 80, 81, 83]'),
                               (unknown, 'unknown [99]'), (no_pending_key, 'pending None')):
            with self.subTest(change.__name__):
                calls = []

                def after_r2(p):
                    got = real(p)
                    calls.append(got)
                    if len(calls) == 2:  # R1's report, then R2's
                        self.assertEqual(got['database']['pendingMigrations'], [], 'the stub migrated')
                        change(got['database'])
                    return got
                with mock.patch.object(rehearse, '_report', after_r2):
                    text = self.failed('R2: ' + ' '.join(rehearse.MIGRATE) + ' left the clone')
                self.assertIn(needle, text)
                self.assertNotIn('serve', [s for s, _ in self.steps()], 'R3 never starts')

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
        for source in ('home', 'home/userdata'):  # the live home or its userdata, bound at the backups folder
            with self.subTest(source):
                cfg = config.load()
                cfg.backup_dir = self.m.path(f'bound-{source.replace("/", "-")}')
                os.makedirs(cfg.backup_dir)
                with self.bind(cfg.backup_dir, self.m.path(source)), self.assertRaises(journal.Refused) as e:
                    rehearse.run_rehearse(cfg, copy_dir=self.copy_dir, say=self.said.append)
                self.assertIn('is the live T3 home', str(e.exception))
                self.assertEqual(os.listdir(cfg.backup_dir), [])

    def test_a_backups_folder_bound_from_inside_the_live_home_is_refused_by_the_mount_list(self):
        """A bind of a folder inside the live home keeps the device (and gives no id the kit compares): the mount
        list names the folder it came from (field 4)."""
        def bound(source, at, dev='8:1'):  # '/' is 8:1 in MOUNTS
            with open(self.mountinfo, 'w') as f:
                f.write(MOUNTS + '91 22 %s %s %s rw,relatime shared:1 - ext4 /dev/sda1 rw\n' % (
                    dev, escaped(source), escaped(at)))
        home, root = self.m.home, self.root()
        for source, at in ((home + '/userdata/attachments', self.m.backups), (home + '/userdata', self.m.backups),
                           (home + '/userdata/x y', root), (root + '/x', home)):
            with self.subTest(source=source, at=at):
                bound(source, at)
                self.refused('bind mount')
                self.assertFalse(os.path.lexists(root))
        bound(self.m.path('elsewhere'), self.m.backups)  # a bind of a folder apart from the live home: fine
        self.rehearse()
        bound(home + '/userdata', self.m.backups, dev='8:2')  # the same path on another filesystem: fine
        self.rehearse()
        bound(home + '/userdata', self.m.backups)  # covered by a later mount on the same point: that one counts
        with open(self.mountinfo, 'a') as f:
            f.write('92 91 0:50 / %s rw,relatime - tmpfs tmpfs rw\n' % escaped(self.m.backups))
        self.rehearse()
        os.rename(root, root + '.done')
        for case in ('missing', 'no root mount'):  # the mount list cannot tell: refused, even with no old folder
            with self.subTest(case):
                if case == 'missing':
                    os.unlink(self.mountinfo)
                else:
                    self.m.write('mountinfo', MOUNTS.split('\n', 1)[1])
                self.refused('mount list')
                self.assertFalse(os.path.lexists(root))

    def lines(self, *mounts):
        """The fake mount list: MOUNTS, then each (device, folder of that filesystem, mount point) in order."""
        with open(self.mountinfo, 'w') as f:
            f.write(MOUNTS + ''.join('%d 22 %s %s %s rw,relatime - ext4 /dev/sdb rw\n' % (
                95 + i, dev, escaped(source), escaped(at)) for i, (dev, source, at) in enumerate(mounts)))

    def tree(self, *mounts):
        """The fake mount list: MOUNTS, then each (mount id, parent id, device, folder, mount point) in order."""
        with open(self.mountinfo, 'w') as f:
            f.write(MOUNTS + ''.join('%d %d %s %s %s rw,relatime - ext4 /dev/sdb rw\n' % (
                mid, parent, dev, escaped(source), escaped(at)) for mid, parent, dev, source, at in mounts))

    def test_a_backups_folder_bound_from_where_the_live_userdata_really_is_is_refused(self):
        """The live userdata (or any folder under the home) on its own partition, or a link to a folder elsewhere:
        a bind of a folder of it at the backups folder is on another device than the home, or in a folder the
        home's location does not cover. The mount list maps it too, and every mount at or under the home."""
        ud, root = self.m.home + '/userdata', self.root()
        for name, mounts in (
                ('userdata on its own partition, bound whole', [('8:17', '/', ud), ('8:17', '/', self.m.backups)]),
                ('a subfolder of it bound', [('8:17', '/', ud), ('8:17', '/attachments', self.m.backups)]),
                ('a deeper folder of it bound', [('8:17', '/', ud), ('8:17', '/a/b', self.m.backups)]),
                ('a mount under userdata', [('8:18', '/', ud + '/attachments'), ('8:18', '/x', self.m.backups)]),
                ('the backups folder around it', [('8:17', '/t3-rehearsal/x', ud), ('8:17', '/', self.m.path('bk'))])):
            with self.subTest(name):
                self.lines(*mounts)
                cfg = config.load()
                if name == 'the backups folder around it':
                    os.makedirs(self.m.path('bk'), exist_ok=True)
                    cfg.backup_dir = self.m.path('bk')
                with self.assertRaises(journal.Refused) as e:
                    rehearse.run_rehearse(cfg, copy_dir=self.copy_dir, say=self.said.append)
                self.assertIn('bind mount', str(e.exception))
                self.assertFalse(os.path.lexists(root))
        # userdata a link to a folder elsewhere on the same filesystem; a subfolder of that bound at the backups
        real_ud = self.m.path('data/t3u')
        os.makedirs(os.path.dirname(real_ud))
        os.rename(ud, real_ud)
        os.symlink(real_ud, ud)
        self.lines(('8:1', real_ud + '/attachments', self.m.backups))
        self.refused('bind mount')
        self.assertFalse(os.path.lexists(root))
        # Apart: no needless refusal
        self.lines()  # userdata a link, nothing bound
        self.rehearse()
        self.lines(('8:1', self.m.path('data/other'), self.m.backups))  # a folder beside the real userdata
        self.rehearse()
        self.lines(('8:17', '/ud', real_ud), ('8:17', '/backups', self.m.backups))  # apart on that partition
        self.rehearse()
        self.lines(('8:17', '/', real_ud), ('8:18', '/', self.m.backups))  # another partition
        self.rehearse()

    def test_the_mount_on_top_counts_not_the_longest_mount_point(self):
        """A mount on a parent made after a mount below it hides that one: the mount tree (parent ids) says which."""
        root, att = self.root(), self.m.home + '/userdata/attachments'
        self.tree((95, 22, '0:44', '/', root), (96, 22, '8:1', att, self.m.backups))
        self.refused('bind mount')
        self.assertFalse(os.path.lexists(root))
        self.tree((95, 22, '8:1', att, self.m.backups), (96, 95, '0:44', '/', root))
        self.rehearse()  # the tmpfs mounted on the bind: on top of it, apart from the live home
        Mount = fx.osadapter.Mount
        entries = [Mount(1, 0, '8:1', '/', '/'), Mount(3, 1, '0:44', '/', '/a/b'), Mount(2, 1, '8:2', '/r', '/a')]
        self.assertEqual(fx.osadapter.source('/a/b/x', entries), ('8:2', '/r/b/x'))  # /a hides /a/b, listed first
        entries[1] = Mount(3, 2, '0:44', '/', '/a/b')  # mounted on /a: on top of it
        self.assertEqual(fx.osadapter.source('/a/b/x', entries), ('0:44', '/x'))
        self.assertEqual(fx.osadapter.source('/c', entries), ('8:1', '/c'))
        self.assertIsNone(fx.osadapter.source('/c', entries[1:]))

    def test_the_mount_on_top_is_found_by_the_tree_not_by_the_list_order(self):
        """A moved mount keeps its place in the list, and MOVE_MOUNT_BENEATH puts a later mount under an earlier
        one: the list's order would pick the hidden tmpfs and allow a rehearsal folder inside live userdata."""
        root, att = self.root(), self.m.home + '/userdata/attachments'
        for name, mounts in (
                # a bind of attachments (95) made first, a tmpfs at the rehearsal folder (96), then the bind moved
                # onto the backups folder: it hides the tmpfs, yet comes first in the list
                ('a bind moved over a later tmpfs', [(95, 22, '8:1', att, self.m.backups), (96, 22, '0:44', '/', root)]),
                # the same, the bind moved onto a tmpfs mounted later at the backups folder
                ('a bind moved onto a later tmpfs', [(95, 96, '8:1', att, root), (96, 22, '0:44', '/', self.m.backups)]),
                # a bind of attachments at the backups folder; a tmpfs put beneath it later (MOVE_MOUNT_BENEATH)
                ('a tmpfs put beneath the bind', [(95, 96, '8:1', att, self.m.backups),
                                                  (96, 22, '0:44', '/', self.m.backups)])):
            with self.subTest(name):
                self.tree(*mounts)
                self.refused('bind mount')
                self.assertFalse(os.path.lexists(root))

    def test_a_mount_tree_that_cannot_tell_the_top_is_refused(self):
        """A cycle, a parent missing from the list, two mounts on one point with one parent, an id twice: the kit
        cannot tell which mount is on top, so it refuses (even with a tmpfs apart from the live home listed last)."""
        root, att = self.root(), self.m.home + '/userdata/attachments'
        for name, needle, mounts in (
                ('a cycle', 'cycle', [(95, 96, '8:1', att, self.m.backups), (96, 95, '0:44', '/', self.m.backups)]),
                ('a missing parent', 'a parent missing', [(95, 22, '8:1', att, self.m.backups),
                                                (96, 77, '0:44', '/', self.m.backups)]),
                ('two on one point', 'one point', [(95, 22, '8:1', att, self.m.backups),
                                                   (96, 22, '0:44', '/', self.m.backups)]),
                ('a parent below its child', 'parent', [(95, 96, '0:44', '/', self.m.backups),
                                                        (96, 22, '8:1', att, root)]),
                ('an id twice', 'twice', [(95, 22, '8:1', att, self.m.backups), (95, 22, '0:44', '/', root)])):
            with self.subTest(name):
                self.tree(*mounts)
                text = self.refused('cannot tell')
                self.assertIn(needle, text)
                self.assertFalse(os.path.lexists(root))
        self.tree((95, 22, '0:44', '/', self.m.backups), (96, 77, '0:45', '/', self.m.path('elsewhere')))
        self.rehearse()  # a parent missing far from the folders: not in the way

    def test_the_mount_list_fields_are_decoded_and_a_home_with_a_space_is_mapped(self):
        """mountinfo writes a space in fields 4 and 5 as \\040: undone in both, so a live home with a space in its
        path is still found inside a bind's source folder."""
        home = self.m.path('my t3')
        os.makedirs(os.path.join(home, 'userdata', 'attachments'))
        self.lines(('8:1', home + '/userdata/attachments', self.m.backups))
        self.assertIn((95, 22, '8:1', home + '/userdata/attachments', self.m.backups), fx.osadapter.mounts())
        with self.assertRaises(journal.Refused) as e:
            rehearse._not_live(self.root(), home)
        self.assertIn('bind mount', str(e.exception))
        self.lines(('8:1', self.m.path('my t3x') + '/userdata/attachments', self.m.backups))
        rehearse._not_live(self.root(), home)  # beside it: fine

    def test_an_empty_mount_list_is_unreadable_for_any_caller(self):
        """mounts() and mount_points() raise on an empty list: a caller about to delete never reads it as 'no mount
        point' (rehearse would refuse anyway: no mount of either folder)."""
        self.m.write('mountinfo', '')
        for read in (fx.osadapter.mounts, fx.osadapter.mount_points):
            with self.subTest(read.__name__), self.assertRaises(fx.osadapter.Unsupported) as e:
                read()
            self.assertIn('empty', str(e.exception))

    def test_a_pseudo_filesystem_root_is_never_apart(self):
        """A mount whose folder is not a path ('net:[...]', nsfs) on the home's device: never taken as apart."""
        for a, b in (('net:[4026531840]', '/h'), ('/h', 'net:[4026531840]'), ('net:[1]', 'net:[2]')):
            with self.subTest(a=a, b=b):
                self.assertFalse(rehearse._apart(a, b))
        self.assertTrue(rehearse._apart('/a', '/b'))
        self.lines(('8:1', 'net:[4026531840]', self.m.backups))
        self.refused('bind mount')
        self.assertFalse(os.path.lexists(self.root()))

    def test_no_mount_of_where_userdata_really_is_is_refused(self):
        """The home and the rehearsal folder are on a mount, but the folder its userdata links to is on none."""
        home, userdata = self.m.path('h2'), self.m.path('h2/userdata')
        os.makedirs(self.m.path('data/t3u'))
        os.makedirs(home)
        os.symlink(self.m.path('data/t3u'), userdata)
        with open(self.mountinfo, 'w') as f:  # one mount: the machine's folder, not '/'
            f.write('22 1 8:1 / %s rw,relatime - ext4 /dev/sda1 rw\n' % escaped(self.m.root))
        rehearse._not_live(self.root(), home)  # the list maps all three: fine
        os.unlink(userdata)
        os.symlink('/nonexistent-golive-test/userdata', userdata)
        with self.assertRaises(journal.Refused) as e:
            rehearse._not_live(self.root(), home)
        self.assertIn('holds no mount', str(e.exception))

    def test_an_error_opening_the_mount_list_says_what_failed_and_refuses(self):
        """A passing error (too many open files) is named; it refuses, without saying rehearse cannot run here."""
        self.rehearse()

        def failing_open(*a, **kw):
            raise OSError(errno.EMFILE, 'Too many open files')
        with mock.patch.object(fx.osadapter, 'open', failing_open, create=True):
            text = self.refused('Too many open files')
        self.assertIn('run again', text)
        self.assertNotIn('cannot run on this system', text)
        self.assertTrue(os.path.isfile(os.path.join(self.root(), 'home', 'userdata', 'statev2.sqlite')))
        self.rehearse()

    def test_rehearse_refuses_an_older_python_before_any_step(self):
        """The removal needs shutil.rmtree(dir_fd=) (Python 3.11): an older one would fail half-way through it on
        every run. Refused before any step, the old folder kept."""
        self.rehearse()
        old = types.SimpleNamespace(version_info=(3, 10, 12))
        with mock.patch.object(rehearse, 'sys', old, create=True):
            text = self.refused('Python 3.11')
        self.assertIn('3.10.12', text)
        self.assertTrue(os.path.isfile(os.path.join(self.root(), 'home', 'userdata', 'statev2.sqlite')))
        self.rehearse()

    def test_a_swap_of_the_old_folder_during_the_guard_is_refused(self):
        """The guard takes the folder's identity first, then looks by path: a swap during it is caught at its end."""
        self.rehearse()
        root, walk = self.root(), os.walk
        self.m.write('other/.golive-rehearsal', '{}')

        def swapping_walk(top, *a, **kw):
            if top != root:  # the walk of the live userdata's links comes first
                return walk(top, *a, **kw)
            os.rename(root, root + '.moved')
            os.rename(self.m.path('other'), root)
            return walk(top, *a, **kw)
        with mock.patch.object(rehearse.os, 'walk', swapping_walk), self.assertRaises(journal.Refused) as e:
            rehearse._guard_old(config.load(), root)
        self.assertIn('changed during the check', str(e.exception))
        self.assertTrue(os.path.isfile(os.path.join(root + '.moved', 'home', 'userdata', 'statev2.sqlite')))

    def test_an_old_folder_gone_at_the_end_of_the_guard_is_refused(self):
        """Moved away during the guard and not back: the guard ends in a refusal, not in 'nothing to remove'."""
        self.rehearse()
        root, walk = self.root(), os.walk

        def moving_walk(top, *a, **kw):
            if top != root:  # the walk of the live userdata's links comes first
                return walk(top, *a, **kw)
            os.rename(root, root + '.moved')
            return walk(root + '.moved', *a, **kw)
        with mock.patch.object(rehearse.os, 'walk', moving_walk), self.assertRaises(journal.Refused) as e:
            rehearse._guard_old(config.load(), root)
        self.assertIn('changed during the check', str(e.exception))
        self.assertTrue(os.path.isfile(os.path.join(root + '.moved', 'home', 'userdata', 'statev2.sqlite')))

    def test_a_clone_that_turns_out_bound_into_the_live_home_fails_and_says_the_clone_was_made(self):
        """R0 on the clone runs after the clone step: its refusal is a failure, never "nothing was changed"."""
        append, root = journal.Run._append, self.root()

        def bind_after_clone(run, event, **fields):
            append(run, event, **fields)
            if fields.get('step') == 'clone' and fields.get('phase') == 'done':
                self.lines(('8:1', self.m.home + '/userdata/sub', root))
        with mock.patch.object(journal.Run, '_append', bind_after_clone):
            text = self.failed('bind mount')
        self.assertNotIn('nothing was changed', text)
        self.assertIn('nothing ran on it', text)
        self.assertIn('any previous rehearsal folder was removed', text)
        self.assertFalse([c for c in self.stub_calls() if c['argv'][0] == '__service-preflight'])

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
            self.assertIsNotNone(fx.osadapter.source(self.m.home, fx.osadapter.mounts()))
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

    def test_a_swap_after_the_last_check_never_redirects_the_removal(self):
        """The removal works on the folder the guard checked, opened without following links and emptied through
        that handle: a swap after the check (to a link, or to another folder, also mid-removal) fails the clone
        step and removes nothing of what is at that path now."""
        root, victim, guard, listdir = self.root(), self.m.path('victim'), rehearse._guard_old, os.listdir
        for how in ('link', 'folder', 'link-mid-removal'):
            with self.subTest(how):
                self.rehearse()
                self.m.write('victim/home/keep.txt', 'x')
                self.m.write('victim/.golive-rehearsal', '{}')
                guarded = []

                def swap():
                    os.rename(root, root + '.moved')
                    if how == 'folder':
                        os.rename(victim, root)
                    else:
                        os.symlink(victim, root)

                def swap_after_guard(cfg, path):
                    got = guard(cfg, path)
                    guarded.append(path)
                    if len(guarded) == 2 and how != 'link-mid-removal':  # R0's guard, then the clone step's
                        swap()
                    return got

                def swap_at_listing(path='.'):
                    if len(guarded) == 2 and how == 'link-mid-removal' and not os.path.lexists(root + '.moved'):
                        swap()
                    return listdir(path)
                try:
                    with mock.patch.object(rehearse, '_guard_old', swap_after_guard), \
                            mock.patch.object(rehearse.os, 'listdir', swap_at_listing):
                        self.failed({'link': 'a link now', 'folder': 'another folder now',
                                 'link-mid-removal': 'changed during the removal'}[how])
                    kept = os.path.join(root if how == 'folder' else victim, 'home', 'keep.txt')
                    self.assertTrue(os.path.isfile(kept), 'what is at the path now is never removed')
                finally:  # put the machine back for the next case
                    if os.path.lexists(root + '.moved'):
                        os.unlink(root) if os.path.islink(root) else shutil.rmtree(root, ignore_errors=True)
                        os.rename(root + '.moved', root)
                    shutil.rmtree(victim, ignore_errors=True)
        self.rehearse()

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


class Links(Base):
    """Links inside userdata (#441): the clone never holds a link out of its own tree, and the rehearsal folder never
    lands where a link of the live userdata leads."""

    LOOKS = 1000  # far more than LINK_HOPS links of any case here need

    def setUp(self):
        super().setUp()
        bounded = coldcopy.link_leaves

        def capped(rel, look):
            """link_leaves with its look()s counted: one that never stops (no hop limit) fails at once, not after
            CI's job timeout."""
            calls = itertools.count(1)

            def counted(path):
                if next(calls) > self.LOOKS:
                    raise AssertionError(f'link_leaves({rel!r}) looked up more than {self.LOOKS} paths: it does not '
                                         'stop at a loop (the hop limit)')
                return look(path)
            return bounded(rel, counted)
        patcher = mock.patch.object(coldcopy, 'link_leaves', capped)
        patcher.start()
        self.addCleanup(patcher.stop)

    def linked_copy(self, links, name=f't3-copy-20990105T000000Z-{fx.LIVE}'):
        """A finished copy whose userdata also holds these links ({path: target}), recorded as a copy records them
        (links as links); a file of the same path is replaced by the link."""
        dest = self.make_copy(name=name)
        path = os.path.join(dest, coldcopy.MANIFEST)
        m = read_json(path)
        for rel, target in links.items():
            full = os.path.join(dest, 'userdata', rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            if os.path.lexists(full):
                os.unlink(full)
            os.symlink(target, full)
            m['files'] = [e for e in m['files'] if e['path'] != rel] + [{'path': rel, 'kind': 'link', 'target': target}]
        with open(path, 'w') as f:
            json.dump(m, f)
        return dest

    def clone_links(self):
        ud = os.path.join(self.root(), 'home', 'userdata')
        found = {}
        for d, dirs, names in os.walk(ud):
            for n in dirs + names:
                if os.path.islink(os.path.join(d, n)):
                    found[os.path.relpath(os.path.join(d, n), ud)] = os.readlink(os.path.join(d, n))
        return found

    # (path, target, stays inside the copied tree)
    CASES = (
        ('abs-out', None, False),  # absolute, to a folder elsewhere (target set in the test)
        ('rel-out', '../../outside', False),  # relative, out by '..'
        ('dot-out', './../outside', False),  # the same after a '.': a '.' is no folder to go up from
        ('deep/er/rel-out', '../../../../x', False),  # the same, two levels below userdata
        ('chain-a', 'chain-b', False),  # a chain: inside, then out
        ('chain-b', '../outside', False),
        ('to-live', None, False),  # to the live home's own attachments (target set in the test)
        ('p/q/up', '../..', True),  # up to userdata itself: stays
        ('p/q/through', 'up/../x', False),  # through that link, then '..' above the tree: out
        ('loop-a', 'loop-b', False),  # a loop: cannot tell where it leads
        ('loop-b', 'loop-a', False),
        ('stays', 'settings.json', True),
        ('stays-dir', 'p/q', True),
        ('dangling-in', 'not-there/yet', True),  # dangling, inside: harmless
        ('dangling-out', '../gone/x', False),  # dangling, out: the clone server could make it
    )

    def cases(self):
        targets = {'abs-out': self.m.path('elsewhere'), 'to-live': self.m.home + '/userdata/attachments'}
        os.makedirs(self.m.path('elsewhere'), exist_ok=True)
        return {p: targets.get(p, t) for p, t, _ in self.CASES}

    def test_the_planned_links_out_of_the_copied_tree_are_found_at_any_depth_through_chains(self):
        entries = [{'path': p, 'kind': 'link', 'target': t} for p, t in self.cases().items()]
        entries += [{'path': 'p', 'kind': 'dir'}, {'path': 'p/q', 'kind': 'dir'}]
        out = dict(coldcopy.links_out(entries))
        self.assertEqual(set(out), {p for p, _, stays in self.CASES if not stays})
        self.assertEqual(coldcopy.links_out([]), [], 'empty userdata: nothing to leave out')

    def test_links_out_of_the_copy_are_left_out_of_the_clone_and_named(self):
        live = self.m.snapshot()
        copy = self.linked_copy(self.cases())
        result = self.rehearse()
        kept = {p: t for p, t, stays in self.CASES if stays}
        self.assertEqual(self.clone_links(), kept, 'the clone holds only the links that stay inside it')
        for p, _, stays in self.CASES:
            self.assertEqual(any(x.startswith(p + ' ') for x in result['leftOut']), not stays, p)
        clone = [s['data'] for s in self.runs()[-1]['steps'] if s['step'] == 'clone' and s['phase'] == 'done'][0]
        self.assertEqual(clone['leftOut'], result['leftOut'], 'journalled with the clone step')
        self.assertTrue(any('left out of the clone' in line for line in self.said), self.said)
        self.assertEqual(self.m.snapshot(), live)
        self.assertEqual(os.readlink(os.path.join(copy, 'userdata', 'abs-out')), self.m.path('elsewhere'),
                         'the copy is never changed')

    def test_a_copy_whose_settings_or_database_is_a_link_out_is_refused_before_any_step(self):
        # a left-out WAL or journal would quietly rehearse an older database; a copy folder that ignores case reads
        # STATEV2.SQLITE-WAL as the WAL, so names match whatever their case (a case-sensitive one merely refuses an
        # odd name)
        for i, name in enumerate(('settings.json', 'statev2.sqlite', 'statev2.sqlite-wal', 'statev2.sqlite-journal',
                                  'STATEV2.SQLITE-WAL', 'Settings.json')):
            with self.subTest(name):
                self.m.write('dotfiles/' + name, 'x')
                copy = self.linked_copy({name: self.m.path('dotfiles/' + name)},
                                        name=f't3-copy-2099012{i}T000000Z-{fx.LIVE}')
                self.refused(f'{name} in the copy', copy_dir=copy)
                self.assertFalse(os.path.lexists(self.root()))

    def test_a_link_added_to_the_copy_after_r0_is_left_out_by_the_copy_itself(self):
        append, copy = journal.Run._append, self.copy_dir

        def link_at_clone(run, event, **fields):
            if fields.get('step') == 'clone' and fields.get('phase') == 'started':
                os.symlink(self.m.home + '/userdata', os.path.join(copy, 'userdata', 'late'))
            append(run, event, **fields)
        with mock.patch.object(journal.Run, '_append', link_at_clone):
            result = self.rehearse()
        self.assertEqual(self.clone_links(), {})
        self.assertTrue(any(x.startswith('late ') for x in result['leftOut']), result['leftOut'])

    def test_r0_on_the_clone_finds_a_link_out_the_copy_let_through(self):
        """With the copy's leaving-out broken, R0 on the clone still finds a link out (any depth) before anything
        runs on the clone, and says when it leads into the live home."""
        for rel, target, needle in (('deep/er/to-live', None, 'the live T3 home'), ('rel', '../../x', 'leads out')):
            with self.subTest(rel):
                target = target or self.m.home + '/userdata'
                copy = self.linked_copy({rel: target}, name=f't3-copy-2099010{8 if target[0] == "/" else 9}'
                                                            f'T000000Z-{fx.LIVE}')
                with mock.patch.object(coldcopy, 'links_out', return_value=[]):
                    text = self.failed(needle, copy_dir=copy)
                self.assertIn(rel, text)
                self.assertIn('nothing ran on it', text)
                self.assertFalse([c for c in self.stub_calls() if c['argv'][0] == '__service-preflight'])

    def test_an_old_clone_holding_links_out_is_removed_without_following_them(self):
        self.rehearse()
        victim = self.m.write('victim/keep.txt', 'live data')
        ud = os.path.join(self.root(), 'home', 'userdata')
        os.symlink(os.path.dirname(victim), os.path.join(ud, 'abs-out'))
        os.symlink('../../../../victim', os.path.join(ud, 'rel-out'))
        os.symlink(self.m.home, os.path.join(ud, 'to-live'))
        live = self.m.snapshot()
        self.rehearse()
        self.assertEqual(self.clone_links(), {})
        self.assertTrue(os.path.isfile(victim))
        self.assertEqual(self.m.snapshot(), live)

    def test_the_link_walk_of_an_empty_userdata_finds_nothing(self):
        empty = self.m.path('empty-userdata')
        os.makedirs(empty)
        self.assertEqual(rehearse._links_out_of(empty), [])
        self.assertEqual(rehearse._live_links(self.m.path('empty-home')), [], 'no userdata at all')
        os.makedirs(self.m.path('empty-home/userdata'))
        self.assertEqual(rehearse._live_links(self.m.path('empty-home')), [])

    def test_a_clone_killed_half_way_runs_again_and_never_held_a_link_out(self):
        self.linked_copy(self.cases())
        code = KILLED.format(paths=[os.path.join(fx.KIT, 'lib'), fx.COMMON], machine=config.MACHINE_FILE,
                             at='mid-copy')
        p = subprocess.run([sys.executable, '-I', '-B', '-c', code], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, -9, p.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.root(), rehearse.MARK)), 'a half clone bears the mark')
        inside = {p: t for p, t, stays in self.CASES if stays}
        self.assertLessEqual(set(self.clone_links()), set(inside), 'no link out, even half-way')
        self.assertFalse(journal.needs_settling(self.runs()[-1]))
        self.rehearse()
        self.assertEqual(self.clone_links(), inside)

    def live_link(self, rel, target):
        full = os.path.join(self.m.home, 'userdata', rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        os.symlink(target, full)
        return full

    def test_a_rehearsal_folder_where_a_live_userdata_link_leads_is_refused_before_anything_changes(self):
        """The live T3 reads and writes where its userdata links lead: the rehearsal folder must be apart from each
        target (the backups folder inside one, or one inside the rehearsal folder), whatever depth the link is at."""
        root = self.root()
        os.makedirs(self.m.path('hop'))
        cases = (
            ('attachments-out', self.m.backups),  # the backups folder inside the target
            ('a/b/deep', self.m.backups),  # the same, two levels below userdata
            ('rel', '../../backups'),  # relative
            ('chain', self.m.path('hop/next')),  # through a link elsewhere
            ('into-old', root + '/home/userdata/attachments'),  # the target inside the rehearsal folder
            ('dangling-into', root + '/later/x'),  # dangling, inside the rehearsal folder: the live T3 may make it
        )
        os.symlink(self.m.backups, self.m.path('hop/next'))
        self.rehearse()  # an old rehearsal folder to keep
        old = self.m.snapshot(root)
        for rel, target in cases:
            with self.subTest(rel):
                link = self.live_link(rel, target)
                self.refused('where the live link')
                self.assertEqual(self.m.snapshot(root), old, 'the old rehearsal folder (the live data) is kept')
                os.unlink(link)
        self.rehearse()  # with no such link, the run passes again

    def test_a_backups_folder_bound_from_where_a_live_userdata_link_leads_is_refused(self):
        os.makedirs(self.m.path('other/sub'))
        self.live_link('attachments', self.m.path('other'))
        with open(self.mountinfo, 'a') as f:  # a bind of a folder of the link's target at the backups folder
            f.write('91 22 8:1 %s %s rw,relatime - ext4 /dev/sda1 rw\n' % (escaped(self.m.path('other/sub')),
                                                                        escaped(self.m.backups)))
        self.refused('bind mount')
        self.assertFalse(os.path.lexists(self.root()))

    def test_a_live_link_apart_from_the_rehearsal_folder_is_allowed(self):
        os.makedirs(self.m.path('elsewhere'))
        self.live_link('attachments-out', self.m.path('elsewhere'))
        self.live_link('dangling', self.m.path('gone/x'))
        self.live_link('inside', 'settings.json')
        self.rehearse()

    def test_a_live_link_added_after_r0_is_caught_by_the_clone_step_and_after_the_clone(self):
        self.rehearse()
        append, root = journal.Run._append, self.root()
        preflights = lambda: [c for c in self.stub_calls() if c['argv'][0] == '__service-preflight']  # noqa: E731
        ran = len(preflights())
        for at in ('started', 'done'):  # before the removal; after the clone was made
            with self.subTest(at):
                def link_at(run, event, **fields):
                    if fields.get('step') == 'clone' and fields.get('phase') == at:
                        self.live_link('late', root + '/home/userdata/attachments')
                    append(run, event, **fields)
                old = os.path.join(root, rehearse.MARK)
                with mock.patch.object(journal.Run, '_append', link_at):
                    text = self.failed('where the live link')
                if at == 'started':
                    self.assertIn('the clone step', text)
                    self.assertTrue(os.path.isfile(old), 'nothing was removed')
                else:
                    self.assertIn('nothing ran on it', text)
                self.assertEqual(len(preflights()), ran, 'nothing ran on the clone')
                os.unlink(os.path.join(self.m.home, 'userdata', 'late'))
        self.rehearse()

    def test_an_unreadable_folder_in_the_live_userdata_is_refused(self):
        if os.geteuid() == 0:
            self.skipTest('root reads any folder')
        shut = self.m.path('home/userdata/shut')
        os.makedirs(shut)
        os.chmod(shut, 0)
        self.addCleanup(os.chmod, shut, 0o700)
        self.refused('cannot tell where its links lead')

    def test_a_live_link_whose_target_cannot_be_looked_at_is_refused_by_name(self):
        """A loop, a link through a file, a link into a folder the kit may not read: where it leads cannot be told,
        so the run is refused naming the link (no traceback), the old rehearsal folder is kept, and a run after the
        link is fixed passes."""
        self.rehearse()  # an old rehearsal folder to keep
        root = self.root()
        old = self.m.snapshot(root)
        shut = self.m.path('shut')
        os.makedirs(shut)
        cases = [('loop', 'loop'), ('through-a-file', 'settings.json/x')]
        if os.geteuid() != 0:  # root reads any folder
            os.chmod(shut, 0)
            self.addCleanup(os.chmod, shut, 0o700)
            cases.append(('unreadable', shut + '/x'))
        for rel, target in cases:
            with self.subTest(rel):
                link = self.live_link(rel, target)
                text = self.refused(f'cannot tell where the live link {link} leads')
                self.assertIn('nothing was changed', text)
                self.assertEqual(self.m.snapshot(root), old, 'the old rehearsal folder is kept')
                self.assertFalse(journal.needs_settling(self.runs()[-1]))
                os.unlink(link)
        self.rehearse()  # with the link gone, the run passes again

    def test_a_live_folder_removed_or_made_a_file_during_any_of_the_three_walks_says_run_again(self):
        """The kit walks the live userdata three times: R0, the clone step's guard (before the old folder goes) and
        R0 on the clone. A folder the live T3 removes, or replaces with a file, during any of them refuses the run
        (after R0: fails it) saying run again, never "cannot be read": before the removal nothing was changed, after
        the clone only the rehearsal folder was redone and nothing ran on it. The run again passes."""
        userdata = os.path.join(self.m.home, 'userdata')
        goes = os.path.join(userdata, 'goes')
        walk, append = os.walk, journal.Run._append
        preflights = lambda: [c for c in self.stub_calls() if c['argv'][0] == '__service-preflight']  # noqa: E731
        armed = []  # the change to make in the next walk of the live userdata

        def walk_and_change(top, *a, **kw):
            for i, step in enumerate(walk(top, *a, **kw)):
                yield step
                if armed and i == 0 and os.path.normpath(top) == userdata and os.path.isdir(goes):
                    os.rmdir(goes)  # listed by the walk, gone (or a file) before it is read
                    if armed[0] == 'made a file':
                        with open(goes, 'w') as f:
                            f.write('x')
                    armed.clear()
        self.rehearse()  # an old rehearsal folder to keep
        for walk_no, at, needle, done in ((1, None, 'cannot tell where its links lead', 'nothing was changed'),
                                          (2, 'started', 'the clone step: ', 'nothing was changed'),
                                          (3, 'done', 'R0 on the clone: ', 'only the rehearsal folder was redone')):
            for change in ('removed', 'made a file'):
                with self.subTest(walk=walk_no, change=change):
                    armed.clear()
                    if os.path.isfile(goes):  # left by a subtest that failed
                        os.unlink(goes)
                    os.makedirs(goes, exist_ok=True)
                    old, ran = self.m.snapshot(self.root()), len(preflights())

                    def seen(snap, whole=walk_no == 1):  # past R0 (walk 2), its lock probe in the old clone has moved
                        return snap if whole else {k: v if v[0] else v[:2] + v[3:] for k, v in snap.items()}  # mtimes
                    if at is None:
                        armed.append(change)

                    def arm(run, event, **fields):
                        if fields.get('step') == 'clone' and fields.get('phase') == at:
                            armed.append(change)
                        append(run, event, **fields)
                    with mock.patch.object(os, 'walk', walk_and_change), \
                            mock.patch.object(journal.Run, '_append', arm):
                        text = self.refused(needle) if walk_no == 1 else self.failed(needle)
                    self.assertFalse(armed, 'the change was made during the walk')
                    self.assertIn('removed or replaced during the walk', text)
                    self.assertIn('run again', text)
                    self.assertIn(done, text)
                    if walk_no < 3:
                        self.assertEqual(seen(self.m.snapshot(self.root())), seen(old), 'the old folder is kept')
                    self.assertEqual(len(preflights()), ran, 'nothing ran on the clone')
                    if os.path.isfile(goes):
                        os.unlink(goes)
                    self.rehearse()  # the run again passes

    def test_a_live_link_to_the_backups_folder_under_another_name_is_caught_by_file_id(self):
        """A bind mount gives a folder a second name that real paths cannot see through; the mount list does not
        show it here either, so only the file id of where the link leads catches it (simulated: no root to mount)."""
        alias = self.m.path('alias')
        os.symlink(self.m.backups, alias)
        link = self.live_link('attachments-bound', alias)
        real = os.path.realpath

        def bind_name(p, *a, **kw):  # the link leads to `alias`, and `alias` resolves to itself, as a bind path does:
            # no real path shows it to be the backups folder, so a check by real path alone passes it
            return alias if os.path.normpath(p) in (link, alias) else real(p, *a, **kw)
        with mock.patch.object(os.path, 'realpath', bind_name):
            self.refused(f'where the live link {link} leads ({alias})')
        self.assertFalse(os.path.lexists(self.root()))

    def test_a_copy_that_cannot_be_read_is_refused_before_any_step(self):
        if os.geteuid() == 0:
            self.skipTest('root reads any folder')
        shut = os.path.join(self.copy_dir, 'userdata', 'shut')
        os.makedirs(shut)
        os.chmod(shut, 0)
        self.addCleanup(os.chmod, shut, 0o700)
        self.refused(f'{os.path.normpath(self.copy_dir)} cannot be read')
        self.assertFalse(os.path.lexists(self.root()))

    def test_a_clone_whose_links_cannot_be_read_fails_r0_before_anything_runs_on_it(self):
        if os.geteuid() == 0:
            self.skipTest('root reads any folder')
        append, shut = journal.Run._append, os.path.join(self.root(), 'home', 'userdata', 'shut')

        def shut_at_clone(run, event, **fields):
            if fields.get('step') == 'clone' and fields.get('phase') == 'done':
                os.mkdir(shut)
                os.chmod(shut, 0)
            append(run, event, **fields)
        with mock.patch.object(journal.Run, '_append', shut_at_clone):
            text = self.failed('R0 on the clone: its links cannot be read')
        self.assertIn('nothing ran on it', text)
        self.assertFalse([c for c in self.stub_calls() if c['argv'][0] == '__service-preflight'])
        os.chmod(shut, 0o700)
        self.rehearse()  # the failed run's clone is thrown away and redone

    def test_r0_on_the_clone_reads_links_as_a_case_folding_folder_resolves_them(self):
        """On a folder that ignores case (ext4 casefold, ntfs3; vfat holds no links), `x -> SUB/D/../..` with
        `sub/d -> ..` leads above the tree, but the plan's names are case-sensitive, so the copy keeps x. R0 on the
        clone reads each link from the disk, as the kernel resolves it, and finds it. Simulated: no case-folding
        filesystem here."""
        ud = self.m.path('fold/userdata')
        os.makedirs(os.path.join(ud, 'sub'))
        os.symlink('..', os.path.join(ud, 'sub', 'd'))
        os.symlink('SUB/D/../..', os.path.join(ud, 'x'))
        self.assertEqual(coldcopy.links_out(coldcopy.plan(ud)[0]), [], 'the plan keeps x (the documented limit)')
        islink, readlink = os.path.islink, os.readlink

        def folded(p):
            p = os.fspath(p)
            if not p.startswith(ud + os.sep):
                return p
            cur = ud
            for part in os.path.relpath(p, ud).split(os.sep):
                try:
                    names = {n.lower(): n for n in os.listdir(cur)}
                except OSError:
                    names = {}
                cur = os.path.join(cur, names.get(part.lower(), part))
            return cur
        with mock.patch.object(os.path, 'islink', lambda p: islink(folded(p))), \
                mock.patch.object(os, 'readlink', lambda p, *a, **kw: readlink(folded(p), *a, **kw)):
            out = rehearse._links_out_of(ud)
        self.assertEqual([(rel, why) for rel, _, why, _ in out], [('x', 'x goes above the tree')])


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
