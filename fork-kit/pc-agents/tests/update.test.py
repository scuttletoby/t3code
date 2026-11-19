"""pc-agents auto-update on a fake WSL2 distro (fake_distro.py; the fork's latest release, its release files and their
build attestations (fake_curl.py, fake_sigstore.py), T3's fake answer, fake processes in T3's control group): off does
nothing; a newer release built by the fork's fork-release.yml on main is downloaded, checked (never run), switched to
only at a quiet point, checked and rolled back with its database when it does not come up; a release that fails a
check is refused and not downloaded again; a version T3 does not come up on is not tried again, other failures a few
times; an alert stays until an update succeeds; a run again changes nothing; a run stopped at any step is finished or
rolled back by the next; no journal, a bad one, a bad machine file, no release or a GitHub error do nothing and say
so; old versions are pruned to the newest three.

  python3 -I -B fork-kit/pc-agents/tests/update.test.py
"""
import fcntl, json, os, shutil, stat, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_distro import CONFIG, SHA1, Case, cli, k  # noqa: E402
import fake_sigstore  # noqa: E402
import machine_file  # noqa: E402

FORK = fake_sigstore.FORK
AUTO = {**CONFIG, 't3ReleaseBaseUrl': f'https://github.com/{FORK}/releases/download',
        'autoUpdate': {'enabled': True, 'follow': FORK}}
VERSIONS = '/opt/pc-agents/t3/runtime/versions'
GROUP = '/sys/fs/cgroup/system.slice/t3code-agents.service'   # T3's unit's control group (the fake systemctl's)
DB = k.HOME + k.DB_DIR


def release(version, draft=False, prerelease=False):
    return {'tag_name': 'v' + version, 'draft': draft, 'prerelease': prerelease, 'name': version}


class Update(Case):
    def setUp(self):
        super().setUp()
        self.no_watch()
        k.HTTP_WAIT, k.HTTP_STEP = 0.2, 0.05   # 180 s and 5 s on a machine
        trust = mock.patch.object(k, 'TRUST', fake_sigstore.ca(self.path('/log/sigstore')))   # the fake Sigstore's
        trust.start()
        self.addCleanup(trust.stop)
        self.machine(AUTO)
        self.ready()
        self.releases(release('1.2.3'))

    def releases(self, *items):
        """The fork's releases, newest first (GitHub's latest: the first neither a draft nor a pre-release)."""
        self.write('/log/releases.json', json.dumps(list(items)))

    def update(self, *flags):
        return self.call(cli.main, ['auto-update', *flags])

    def cut(self, name, when=lambda *a, **kw: True):
        """The run is stopped (SIGTERM: the service's time limit, a shutdown) as it calls cli.name with arguments
        when() takes."""
        real = getattr(cli, name)

        def stop(*args, **kw):
            if when(*args, **kw):
                raise SystemExit(143)
            return real(*args, **kw)
        with mock.patch.object(cli, name, stop), self.assertRaises(SystemExit):
            self.update()

    def state(self):
        return json.loads(self.read(k.UPDATE_STATE))

    def version(self):
        return json.loads(self.read('/etc/fork-kit/machine.json'))['pc-agents']['t3Version']

    def serving(self):
        """The version the running T3 unit started with (None: not running)."""
        try:
            line = next(x for x in self.read('/log/units/t3code-agents.service.active').splitlines()
                        if x.startswith('ExecStart='))
        except FileNotFoundError:
            return None
        return line.split('/runtime/versions/')[1].split('/')[0]

    def updated(self, to='1.2.4', was='1.2.3'):
        self.assertEqual(self.version(), to)
        self.assertEqual(self.serving(), to)
        state = self.state()
        self.assertFalse({'phase', 'alert', 'waiting', 'retry', 'retries', 'refused'} & set(state), state)
        self.assertIn(f'T3 {to} runs (was {was})', state['result'])
        self.assertFalse(os.path.exists(self.path('/log/t3-ran')))   # root never ran what it downloaded
        code, out = self.check()
        self.assertEqual(code, 0, out)

    def process(self, pid, exe, name='claude', group=''):
        """A process (its program exe) in T3's unit's control group (or a group below it), or in none (None)."""
        self.write(f'/proc/{pid}/comm', name + '\n')
        os.symlink(exe, self.path(f'/proc/{pid}/exe'))
        if group is not None:
            os.makedirs(self.path(GROUP + group), exist_ok=True)
            with open(self.path(GROUP + group + '/cgroup.procs'), 'a') as f:
                f.write(f'{pid}\n')

    def main_pid(self):
        return self.read('/log/units/t3code-agents.service.pid').strip()

    def installs(self):
        return len(self.log('install'))

    def test_newer_release_is_downloaded_checked_switched_to_and_checked(self):
        self.write('/etc/fork-kit/machine.json', json.dumps({'version': 1, 'pc-agents': AUTO}, indent=4) + '\n')
        before = self.read('/etc/fork-kit/machine.json')
        self.write(DB + '/statev2.sqlite', 'the database')
        self.releases(release('1.2.4'), release('1.2.3'))
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.updated()
        self.assertEqual(self.log('install')[-1], f'1.2.4 {k.arch()}')
        self.assertEqual(self.read('/etc/fork-kit/machine.json.bak'), before)   # the copy, as it was
        self.assertEqual(self.read('/etc/fork-kit/machine.json'),   # only the version changed, the layout kept
                         before.replace('"t3Version": "1.2.3"', '"t3Version": "1.2.4"'))
        self.assertTrue(os.path.isdir(self.path(VERSIONS + '/1.2.3')))   # kept: the one to go back to
        self.assertIn(f'{VERSIONS}/1.2.4/t3', k.record())
        curl = self.read('/log/curl')
        self.assertIn(f'https://github.com/{FORK}/releases/latest', curl)   # not the API: its rate limit
        self.assertNotIn(f'repos/{FORK}/releases', curl)
        self.assertIn(f'https://api.github.com/repos/{FORK}/attestations/sha256:', curl)
        self.assertIn('http://127.0.0.1:3800/.well-known/t3/environment', curl)
        self.assertEqual(self.read(DB + '/statev2.sqlite'), 'the database')   # untouched
        self.assertEqual(self.read(k.DB_BACKUP + '/statev2.sqlite'), 'the database')   # copied, T3 stopped
        self.assertEqual(stat.S_IMODE(os.stat(self.path(k.DB_BACKUP)).st_mode), 0o700)
        self.assertLess(self.log().index('stop t3code-agents.service'),
                        max(i for i, x in enumerate(self.log()) if x.startswith('restart')))

    def test_off_does_nothing(self):
        self.machine(CONFIG)
        before, curl = self.snapshot(), self.log('curl')
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('autoUpdate is off', out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.log('curl'), curl)

    def test_up_to_date_and_a_run_again_change_nothing(self):
        self.releases(release('1.2.4'))
        self.assertEqual(self.update()[0], 0)
        self.assertFalse(os.path.exists(self.path(k.PENDING)))
        mine = (k.STATE, k.UPDATE_STATE)   # the journal (its result and time), and its folder's mtime
        before = {p: v for p, v in self.snapshot().items() if p not in mine}
        installs, starts = self.installs(), self.log()
        for _ in range(2):
            code, out = self.update()
            self.assertEqual(code, 0, out)
            self.assertIn('T3 1.2.4 is up to date', out)
        self.assertEqual({p: v for p, v in self.snapshot().items() if p not in mine}, before)
        self.assertEqual(self.installs(), installs)
        self.assertFalse([x for x in self.log()[len(starts):] if x.startswith(('restart', 'stop', 'enable'))])

    def test_only_the_latest_full_release_on_the_stable_track(self):
        self.releases(release('9.0.0', draft=True), release('8.0.0', prerelease=True), release('1.2.2'),
                      release('1.3.0'))
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('T3 1.2.3 is up to date (the latest release of example/t3code is 1.2.2)', out)
        self.releases(release('7.0.0-nightly.20261118.12'), release('1.3.0'))   # latest, but not T3's stable track
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('example/t3code has no latest release T3 can take', out)
        self.assertEqual(self.version(), '1.2.3')
        nightly = '0.1.11-nightly.20261118.4890.fork.1'   # the fork's build of a nightly: a full release, stable track
        self.assertEqual(k.latest_version({'tag_name': 'v' + nightly, 'id': 1}), nightly)
        for bad in ({'tag_name': '1.2.4'}, {'tag_name': 'vx'}, {'tag_name': 5}, {}, 'v1.2.5', None, [],
                    {'tag_name': 'v1.2.4-preview.20261118.1'}, {'tag_name': 'v1.2.4/../x'}):
            self.assertIsNone(k.latest_version(bad), bad)
        order = ['1.2.3-fork.1', '1.2.3-fork.2', '1.2.3-fork.10', '1.2.3', '1.2.4-fork.1',   # "fork" < "nightly"
                 '1.2.4-nightly.20261118.1.fork.1', '1.2.4-nightly.20261119.1.fork.1', '1.2.4']
        self.assertEqual(sorted(reversed(order), key=k.semver_key), order)

    def test_nothing_to_read_does_nothing_and_says_so(self):
        cases = (('releases-fail', '500', 0, 'cannot read the latest release'),   # GitHub down: waits
                 ('releases-fail', '', 0, 'cannot read the latest release'),   # 403: GitHub's rate limit, waits
                 ('releases-fail', '429', 0, 'cannot read the latest release'),
                 ('releases.json', '[]', 0, 'has no latest release T3 can take'),
                 ('latest-raw', '<html>', 1, 'the latest release is not JSON'),
                 ('latest-raw', '["v1.2.4"]', 1, 'the latest release is not a JSON object'))
        for name, text, want, said in cases:
            with self.subTest(name, text=text):
                self.write('/log/' + name, text)
                self.releases(release('1.2.4')) if name != 'releases.json' else None
                code, out = self.update()
                self.assertEqual(code, want, out)
                self.assertIn(said, out)
                self.assertIn(said, self.state()['result'])
                self.assertEqual('waiting' in self.state(), name == 'releases-fail')
                self.assertEqual(self.version(), '1.2.3')
                self.assertNotIn('1.2.4', self.read('/log/install'))
                os.unlink(self.path('/log/' + name))
                self.releases(release('1.2.3'))
                self.assertEqual(self.update()[0], 0)
                self.assertNotIn('waiting', self.state())

    def test_no_journal_a_bad_one_and_a_bad_machine_file(self):
        self.assertFalse(os.path.exists(self.path(k.UPDATE_STATE)))   # none yet: a first run
        self.write(k.UPDATE_STATE, '{"phase": "switching", "from"')
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('cannot be read', out)
        self.assertIn('up to date', self.state()['result'])
        self.write(k.UPDATE_STATE, '["switching"]')
        self.assertIn('not a JSON object', self.update()[1])
        self.write(k.UPDATE_STATE, json.dumps({'failed': '../../x', 'refused': 7, 'alert': 7, 'retries': '2',
                                               'waiting': 1.5, 'phase': 'switching', 'to': '1.2.4'}))
        code, out = self.update()   # values of the wrong kind are left out; a phase without from: left alone
        self.assertEqual(code, 0, out)
        self.assertIn('failed is not a version', out)
        self.assertFalse({'failed', 'refused', 'alert', 'retries', 'waiting', 'phase'} & set(self.state()))
        for text in ('{"version": 1, "pc-agents": ', json.dumps({'version': 1, 'pc-agents': {'t3Version': '1.2.4'}})):
            with self.subTest(text=text):
                self.write('/etc/fork-kit/machine.json', text)
                self.releases(release('1.2.4'))
                code, out = self.update()
                self.assertEqual(code, 1, out)
                self.assertRegex(out, r'FAILED: nothing done: \S*/etc/fork-kit/machine.json')
                self.assertEqual(self.serving(), '1.2.3')
                self.assertEqual(self.read('/etc/fork-kit/machine.json'), text)

    def test_the_followed_fork_must_be_where_t3_downloads_from(self):
        self.machine({**AUTO, 't3ReleaseBaseUrl': 'https://example.com/releases'})
        self.releases(release('1.2.4'))
        curl = self.log('curl')
        code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertIn('t3ReleaseBaseUrl: is https://example.com/releases, but autoUpdate follows the releases of '
                      f'{FORK}: set it to {AUTO["t3ReleaseBaseUrl"]}', out)
        self.assertEqual(self.log('curl'), curl)
        code, out = self.setup_run()   # setup refuses it too
        self.assertEqual(code, 2, out)

    def test_a_release_that_fails_a_check_is_refused_and_not_downloaded_again(self):
        self.assertEqual(cli.attestation.unsnappy(bytes.fromhex('0a00611501')), b'a' * 10)   # a copy, as GitHub's
        self.releases(release('1.2.4'))
        kept = self.keep()
        said = {'missing': 'has no build attestation on example/t3code', 'sums-bad': 'but SHA256SUMS says',
                'tar-outside': '/../evil is not under t3-1.2.4-linux-'}
        for fault in sorted(fake_sigstore.FAULTS) + ['sums-bad', 'tar-outside']:
            with self.subTest(fault):
                self.restore(kept)
                self.write('/log/sigstore/fault' if fault in fake_sigstore.FAULTS else '/log/' + fault, fault)
                code, out = self.update()
                self.assertEqual(code, 1, out)
                self.assertIn('FAILED: not updating: T3 1.2.4 refused: ', out)
                self.assertIn(said.get(fault, 'is not attested as built by example/t3code\'s fork-release.yml on '
                                                'main: '), out)
                state = self.state()
                self.assertEqual(state['refused'], '1.2.4')
                self.assertIn('not updating: T3 1.2.4 refused', state['alert'])
                self.assertEqual((self.version(), self.serving()), ('1.2.3', '1.2.3'))
                self.assertEqual(os.listdir(self.path(VERSIONS)), ['1.2.3'])   # nothing of it kept
                self.assertFalse([p for p in self.snapshot() if p.endswith('/evil')])
                installs = self.installs()
                code, out = self.update()
                self.assertEqual(code, 1, out)   # the alert stands
                self.assertIn('the release files of T3 1.2.4 were refused before: not downloading them again', out)
                self.assertEqual(self.installs(), installs)
                self.releases(release('1.2.5'), release('1.2.4'))   # a newer release is downloaded (and checked)
                self.update()
                self.assertEqual(self.installs(), installs + 1)
                self.releases(release('1.2.4'))
        self.restore(kept)
        self.write('/log/sigstore/by-url', '')   # a bundle GitHub serves at a bundle_url, snappy
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.updated()
        self.assertIn('https://attestations.example/', self.read('/log/curl'))

    def test_attestations_not_read_are_tried_again(self):
        self.releases(release('1.2.4'))
        self.write('/log/attestations-fail', '502')
        code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertIn('T3 1.2.4 did not download (T3 install failed: cannot read its attestations', out)
        self.assertFalse({'refused', 'alert'} & set(self.state()))
        os.unlink(self.path('/log/attestations-fail'))
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.updated()

    def test_waits_for_a_quiet_point(self):
        self.releases(release('1.2.4'))
        main = self.main_pid()
        self.process(4001, VERSIONS + '/1.2.3/t3', 't3-helper')   # T3's own helper: not work
        self.process(4002, '/home/agent/.local/bin/claude', group='/sub')   # setsid'd, in a group below: still T3's
        self.process(4003, '/usr/bin/node', 'node', group=None)   # a process outside T3's unit: holds nothing
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('T3 1.2.4 is downloaded; waiting for a quiet point (T3 runs 1 other process(es): claude (4002))',
                      out)
        self.assertEqual((self.version(), self.serving(), self.main_pid()), ('1.2.3', '1.2.3', main))
        self.assertTrue(os.path.isfile(self.path(VERSIONS + '/1.2.4/t3')))   # downloaded ahead
        state = self.state()
        self.write(k.UPDATE_STATE, json.dumps({**state, 'waiting': state['waiting'] - k.QUIET_WAIT_DAYS * 86400}))
        code, out = self.update()   # a week later: an alert
        self.assertEqual(code, 1, out)
        self.assertIn(f'an update has waited {k.QUIET_WAIT_DAYS} days', self.state()['alert'])
        if os.geteuid():   # unreadable (only as a user: root reads it): busy
            os.chmod(self.path('/proc/4002'), 0)
            try:
                self.assertIn('cannot read process 4002', self.update()[1])
            finally:
                os.chmod(self.path('/proc/4002'), 0o755)
        shutil.rmtree(self.path('/proc/4002'))   # it ended
        installs = self.installs()
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.updated()
        self.assertEqual(self.installs(), installs)   # not downloaded twice

    def test_a_version_that_does_not_come_up_is_rolled_back_and_not_tried_again(self):
        for name, value in (('t3-crash-version', '1.2.4'), ('http-mute', '1.2.4')):
            with self.subTest(name):
                self.write('/log/' + name, value)
                self.releases(release('1.2.4'))
                code, out = self.update()
                self.assertEqual(code, 1, out)
                self.assertEqual((self.version(), self.serving()), ('1.2.3', '1.2.3'))
                state = self.state()
                self.assertEqual(state['failed'], '1.2.4')
                self.assertNotIn('phase', state)
                self.assertRegex(state['result'], r'^T3 1\.2\.4 did not come up \((setup failed: T3 did not start'
                                 r'|no answer from T3).*\); not trying it again\. Back on 1\.2\.3, running$')
                self.assertIn(state['result'], state['alert'])
                self.assertFalse(os.path.exists(self.path(VERSIONS + '/1.2.4')))   # never reused
                self.assertEqual(self.check()[0], 0)
                bak = json.loads(self.read('/etc/fork-kit/machine.json.bak'))   # from before the update, kept
                self.assertEqual(bak['pc-agents']['t3Version'], '1.2.3')
                installs = self.installs()
                code, out = self.update()
                self.assertEqual(code, 1, out)   # the alert stands
                self.assertIn('T3 1.2.4 did not come up before: not trying it again', out)
                self.assertIn('ALERT (kept until an update succeeds', out)
                self.assertEqual(self.installs(), installs)
                os.unlink(self.path('/log/' + name))
                os.unlink(self.path(k.UPDATE_STATE))
        self.write('/log/t3-crash-version', '1.2.4')
        self.update()
        self.releases(release('1.2.5'), release('1.2.4'))   # a newer one is tried
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.updated('1.2.5')
        self.assertEqual(self.state()['failed'], '1.2.4')

    def test_other_failures_are_tried_again_a_few_times(self):
        self.releases(release('1.2.4'))
        real = cli.setup

        def setup(*a):   # setup fails on 1.2.4 for a reason of its own, not T3's (no network for apt-get)
            if self.version() == '1.2.4':
                print('pc-agents: apt-get failed: E: no network (test)')
                return 1
            return real(*a)
        with mock.patch.object(cli, 'setup', setup):
            for tries in range(1, k.SETUP_TRIES + 1):
                installs = self.installs()
                code, out = self.update()
                self.assertEqual(code, 1, out)
                self.assertEqual(self.installs(), installs + 1)   # downloaded and checked anew every time
                self.assertEqual((self.version(), self.serving()), ('1.2.3', '1.2.3'))
                state = self.state()
                if tries < k.SETUP_TRIES:
                    self.assertEqual((state['retry'], state['retries']), ('1.2.4', tries))
                    self.assertNotIn('failed', state)
                    self.assertIn(f'trying it again next hour (failed switch {tries} of {k.SETUP_TRIES})', out)
                else:
                    self.assertEqual(state['failed'], '1.2.4')
                    self.assertFalse({'retry', 'retries'} & set(state))
                    self.assertIn(f'not trying it again ({k.SETUP_TRIES} failed switches)', out)
                self.assertIn('setup failed: apt-get failed: E: no network (test)', state['alert'])

    def test_a_wall_that_does_not_hold_after_the_switch_rolls_it_back(self):
        self.releases(release('1.2.4'))
        real = cli.setup

        def setup(*a):   # the firewall is gone right after setup on 1.2.4 (another distro's root dropped it)
            code = real(*a)
            if self.version() == '1.2.4':
                os.unlink(self.path('/log/nft-loaded'))
            return code
        with mock.patch.object(cli, 'setup', setup):
            code, out = self.update()
        self.assertEqual(code, 1, out)
        state = self.state()
        self.assertIn('T3 1.2.4 did not come up (FAIL  firewall', state['result'])
        self.assertEqual((self.version(), self.serving()), ('1.2.3', '1.2.3'))
        self.assertEqual((state['retry'], state['retries']), ('1.2.4', 1))   # not T3's fault: tried again
        self.assertNotIn('failed', state)

    def test_an_alert_stays_until_an_update_succeeds_or_is_cleared(self):
        k.run(['/usr/bin/systemctl', 'stop', k.UNIT])
        code, out = self.update()   # up to date, but T3 is not running: is-active comes first
        self.assertEqual(code, 1, out)
        self.assertIn('T3 was not running', self.state()['alert'])
        k.run(['/usr/bin/systemctl', 'restart', k.UNIT])
        code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertIn('T3 1.2.3 is up to date', out)
        self.assertIn('ALERT (kept until an update succeeds, or pc-agents auto-update --clear): ', out)
        self.releases(release('1.2.4'))
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.updated()
        self.write('/log/t3-crash-version', '1.2.5')
        self.releases(release('1.2.5'))
        self.assertEqual(self.update()[0], 1)
        self.assertEqual(self.state()['failed'], '1.2.5')
        code, out = self.update('--clear')
        self.assertEqual(code, 0, out)
        self.assertFalse({'alert', 'failed', 'refused', 'retry', 'retries', 'waiting'} & set(self.state()))
        os.unlink(self.path('/log/t3-crash-version'))
        code, out = self.update()   # tried again after --clear
        self.assertEqual(code, 0, out)
        self.updated('1.2.5', '1.2.4')

    def test_the_database_is_put_back_on_rollback(self):
        self.write(DB + '/statev2.sqlite', 'old database')
        self.write(DB + '/statev2.sqlite-wal', 'old log')
        os.chmod(self.path(DB + '/statev2.sqlite'), 0o600)
        self.write('/log/t3-crash-version', '1.2.4')
        self.releases(release('1.2.4'))
        real = cli.setup

        def setup(*a):   # T3 1.2.4 changes its database in a way 1.2.3 cannot read, then dies
            if self.version() == '1.2.4':
                self.write(DB + '/statev2.sqlite', 'new database')
                self.write(DB + '/statev2.sqlite-wal', 'new log')
                self.write(DB + '/statev2.sqlite-shm', 'new index')
            return real(*a)
        with mock.patch.object(cli, 'setup', setup):
            code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertIn('Back on 1.2.3, running', self.state()['result'])
        self.assertEqual(self.read(DB + '/statev2.sqlite'), 'old database')
        self.assertEqual(self.read(DB + '/statev2.sqlite-wal'), 'old log')
        self.assertFalse(os.path.exists(self.path(DB + '/statev2.sqlite-shm')))   # the newer one's: removed
        self.assertEqual(stat.S_IMODE(os.stat(self.path(DB + '/statev2.sqlite')).st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(self.path(DB))), ['statev2.sqlite', 'statev2.sqlite-wal'])

    def test_a_database_that_cannot_be_copied_stops_the_update(self):
        os.makedirs(self.path(DB))
        self.write('/srv/elsewhere', 'not the database')
        os.symlink(self.path('/srv/elsewhere'), self.path(DB + '/statev2.sqlite'))   # a link the agent made
        self.releases(release('1.2.4'))
        code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertIn('nothing done: T3\'s database cannot be copied before the update (cannot open', out)
        self.assertNotIn('phase', self.state())
        self.assertEqual((self.version(), self.serving()), ('1.2.3', '1.2.3'))   # T3 back as it was
        os.unlink(self.path(DB + '/statev2.sqlite'))
        os.rmdir(self.path(DB))
        os.symlink(self.path('/srv'), self.path(DB))   # the folder a link: not followed either
        code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertIn('T3\'s database cannot be copied', out)
        self.assertEqual((self.version(), self.serving()), ('1.2.3', '1.2.3'))

    def test_a_download_that_fails_or_was_cut_is_tried_again(self):
        self.releases(release('1.2.4'))
        self.write('/log/install-fail', '')
        code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertIn('T3 1.2.4 did not download', out)
        self.assertEqual((self.version(), self.serving()), ('1.2.3', '1.2.3'))
        self.assertFalse({'refused', 'alert'} & set(self.state()))
        os.unlink(self.path('/log/install-fail'))
        os.makedirs(self.path(VERSIONS + '/1.2.4/lib'))   # a download cut before its t3 was there
        os.makedirs(self.path(VERSIONS + '/.removing-77'))   # a removal cut half-way
        os.makedirs(self.path(VERSIONS + '/.staging-kit-78/t3'))   # a download cut half-way
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('no t3: removing it to install T3 1.2.4 again', out)
        self.assertEqual(sorted(os.listdir(self.path(VERSIONS))), ['1.2.3', '1.2.4'])
        self.updated()

    def test_stopped_half_way_is_finished_or_rolled_back(self):
        kept = self.keep()
        cuts = (('T3 stopped, before the database copy', 'back_up_db', lambda *a: True),
                ('before the machine file', 'set_version', lambda v, *a, **kw: v == '1.2.4'),
                ('after the machine file', 'setup', lambda *a: True),
                ('in setup, after its writes', 'install_t3', lambda c, *a, prefetch=False: c['t3Version'] == '1.2.4'
                 and not prefetch),
                ('before the check', 'serves', lambda *a: True))
        for name, fn, when in cuts:
            for off in (False, True):
                with self.subTest(name, auto_update_off=off):
                    self.restore(kept)
                    self.write(DB + '/statev2.sqlite', 'the database')
                    self.releases(release('1.2.4'))
                    self.cut(fn, when)
                    self.assertEqual(self.state()['phase'], 'switching')
                    if off:   # the owner turns autoUpdate off: the cut switch is finished all the same
                        machine = json.loads(self.read('/etc/fork-kit/machine.json'))
                        machine['pc-agents']['autoUpdate']['enabled'] = False
                        self.write('/etc/fork-kit/machine.json', json.dumps(machine))
                    code, out = self.update()
                    self.assertEqual(code, 0, out)
                    self.assertEqual((self.version(), self.serving()), ('1.2.4', '1.2.4'))
                    self.assertNotIn('phase', self.state())
                    if not off:
                        self.updated()
                    self.assertEqual(self.read(DB + '/statev2.sqlite'), 'the database')

    def test_a_cut_switch_waits_for_a_quiet_point_when_t3_runs_again(self):
        self.releases(release('1.2.4'))
        self.cut('serves')   # T3 runs as 1.2.4 (setup started it), and may have taken work since
        self.process(4002, '/home/agent/.local/bin/claude')
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('the cut update to T3 1.2.4 is finished at a quiet point (T3 runs 1 other process(es)', out)
        self.assertEqual((self.state()['phase'], self.state()['tries']), ('switching', 1))
        shutil.rmtree(self.path('/proc/4002'))
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.updated()

    def test_a_cut_switch_after_a_boot_copies_the_database_again(self):
        self.write(DB + '/statev2.sqlite', 'before the update')
        self.releases(release('1.2.4'))
        self.cut('setup')   # the machine file says 1.2.4; T3 stopped, its unit still 1.2.3's
        k.run(['/usr/bin/systemctl', 'restart', k.UNIT])   # a boot starts it: 1.2.3 runs, and writes
        self.write(DB + '/statev2.sqlite', 'after the boot')
        self.write('/log/t3-crash-version', '1.2.4')
        code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertEqual((self.version(), self.serving()), ('1.2.3', '1.2.3'))
        self.assertEqual(self.read(DB + '/statev2.sqlite'), 'after the boot')   # not the stale copy

    def test_cut_rollback_is_finished(self):
        self.write('/log/t3-crash-version', '1.2.4')
        self.releases(release('1.2.4'))
        self.cut('set_version', lambda v, *a, **kw: v == '1.2.3')
        self.assertEqual(self.state()['phase'], 'rolling-back')
        self.assertEqual(self.version(), '1.2.4')
        code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertEqual((self.version(), self.serving(), self.state()['failed']), ('1.2.3', '1.2.3', '1.2.4'))
        self.assertIn('Back on 1.2.3, running', self.state()['result'])
        self.assertFalse(os.path.exists(self.path(VERSIONS + '/1.2.4')))

    def test_a_switch_cut_too_often_is_rolled_back(self):
        self.releases(release('1.2.4'))
        for _ in range(1 + k.UPDATE_TRIES):
            self.cut('setup')
        self.assertEqual(self.state()['tries'], k.UPDATE_TRIES)
        code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertIn(f'did not come up (its switch was cut {k.UPDATE_TRIES} times)', out)
        state = self.state()
        self.assertEqual((self.version(), self.serving()), ('1.2.3', '1.2.3'))
        self.assertEqual((state['retry'], state['retries']), ('1.2.4', 1))   # not T3's fault: tried again
        self.assertNotIn('failed', state)

    def test_a_cut_switch_the_owner_changed_is_left_alone(self):
        self.write(k.UPDATE_STATE, json.dumps({'phase': 'switching', 'from': '1.2.2', 'to': '1.2.4', 'tries': 1}))
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('a cut update (1.2.2 to 1.2.4) is left alone: the machine file says t3Version 1.2.3 now', out)
        self.assertNotIn('phase', self.state())
        self.assertEqual(self.version(), '1.2.3')

    def test_only_from_a_finished_setup_a_running_t3_and_holding_walls(self):
        for latest in ('1.2.4', '1.2.3'):
            for name, change, said in (
                    ('pending', lambda: self.write(k.PENDING, 'x'), 'setup has not finished'),
                    ('stopped', lambda: k.run(['/usr/bin/systemctl', 'stop', k.UNIT]), 'T3 is not running'),
                    ('wall', lambda: os.unlink(self.path('/log/nft-loaded')), 'a wall does not hold (FAIL  firewall')):
                if latest == '1.2.3' and name == 'wall':
                    continue   # up to date: the walls are the check's to see, not the update's
                with self.subTest(name, latest=latest):
                    kept = self.keep()
                    self.releases(release(latest))
                    change()
                    code, out = self.update()
                    self.assertEqual(code, 1, out)
                    self.assertIn('nothing done: ' + said, out)
                    self.assertEqual(self.version(), '1.2.3')
                    self.assertNotIn('1.2.4', self.read('/log/install'))
                    self.restore(kept)

    def test_old_versions_are_pruned_to_the_newest_three(self):
        record = self.read(k.RECORD)
        for v in ('1.1.0', '1.2.0', '1.10.0-fork.1'):
            self.write(f'{VERSIONS}/{v}/t3', {'text': '#!/bin/sh\n', 'mode': '755'})
            record += f'{k.file_sha(VERSIONS + "/" + v + "/t3")}\t{VERSIONS}/{v}/t3\n'
        self.write(k.RECORD, record)
        self.write(VERSIONS + '/notes', 'not a version')
        os.makedirs(self.path('/srv/1.0.0'))
        os.symlink('/srv/1.0.0', self.path(VERSIONS + '/1.0.0'))   # a link named as a version: never followed
        self.releases(release('1.2.4'))
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('removed T3 1.2.0, 1.1.0 (kept the newest 3)', out)
        self.assertEqual(sorted(os.listdir(self.path(VERSIONS))), ['1.0.0', '1.10.0-fork.1', '1.2.3', '1.2.4', 'notes'])
        self.assertTrue(os.path.isdir(self.path('/srv/1.0.0')))
        self.assertEqual([p for p in k.record() if '/versions/1.1.0/' in p or '/versions/1.2.0/' in p], [])
        self.assertEqual(self.check()[0], 0)

    def test_a_failed_version_takes_no_kept_place(self):
        record = self.read(k.RECORD)
        for v in ('1.2.4', '1.2.5', '1.2.6'):
            self.write(f'{VERSIONS}/{v}/t3', {'text': '#!/bin/sh\n', 'mode': '755'})
            record += f'{k.file_sha(VERSIONS + "/" + v + "/t3")}\t{VERSIONS}/{v}/t3\n'
        self.write(k.RECORD, record)
        self.write(k.UPDATE_STATE, json.dumps({'failed': '1.2.6'}))   # left by an older kit
        self.releases(release('1.2.7'))
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('removed T3 1.2.6, 1.2.4 (kept the newest 3)', out)
        self.assertEqual(sorted(os.listdir(self.path(VERSIONS))), ['1.2.3', '1.2.5', '1.2.7'])   # new, before, one

    def test_success_is_saved_before_the_prune(self):
        self.releases(release('1.2.4'))
        with mock.patch.object(cli, 'prune', mock.Mock(side_effect=OSError('disk error (test)'))):
            code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('removing old T3 versions failed (disk error (test))', out)
        self.updated()

    def test_one_at_a_time(self):
        os.makedirs(self.path('/run/pc-agents'), exist_ok=True)
        with open(self.path(k.LOCK), 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)   # a setup that runs
            for argv in (['auto-update'], ['auto-update', '--clear'], ['setup', '--source', '/', '--sha', SHA1]):
                code, out = self.call(cli.main, argv)
                self.assertEqual(code, 1, out)
                self.assertIn('another pc-agents setup or automatic update runs', out)


class MachineFile(Case):
    def test_auto_update_key(self):
        self.assertEqual(k.config()['autoUpdate'], {'enabled': False, 'follow': ''})   # off unless set
        self.machine(AUTO)
        self.assertEqual(k.config()['autoUpdate'], {'enabled': True, 'follow': FORK})
        self.machine({**CONFIG, 'autoUpdate': {'enabled': False}})
        self.assertEqual(k.config()['autoUpdate'], {'enabled': False, 'follow': ''})
        for bad, said in (({'enabled': 'yes', 'follow': FORK}, 'enabled: must be bool'),
                          ({'follow': FORK}, '"enabled" is missing'),
                          ({'enabled': True}, 'needs "follow"'),
                          ({'enabled': True, 'follow': 'https://github.com/x/y'}, 'the fork as OWNER/NAME'),
                          ({'enabled': True, 'follow': 'x/..'}, 'the fork as OWNER/NAME'),
                          ({'enabled': True, 'follow': 'x/.'}, 'the fork as OWNER/NAME'),
                          ({'enabled': True, 'follow': '-x/y'}, 'the fork as OWNER/NAME'),
                          ({'enabled': True, 'follow': FORK, 'channel': 'x'}, "unknown key 'channel'"),
                          (True, 'must be dict')):
            with self.subTest(bad=bad):
                self.machine({**AUTO, 'autoUpdate': bad})
                with self.assertRaisesRegex(machine_file.MachineFileError, 'autoUpdate') as e:
                    k.config()
                self.assertIn(said, str(e.exception))

    def test_units(self):
        self.machine(AUTO)
        files = k.render(k.config())
        service = files['/etc/systemd/system/pc-agents-update.service'][0]
        for line in ('Type=oneshot', 'ExecStart=/opt/pc-agents/current/sbin/pc-agents auto-update',
                     'After=network-online.target'):
            self.assertIn('\n' + line + '\n', service)
        self.assertNotIn('User=', service)   # root's: the agent has no say in it
        timer = files['/etc/systemd/system/pc-agents-update.timer'][0]
        for line in ('OnUnitActiveSec=1h', 'RandomizedDelaySec=20min', 'WantedBy=timers.target'):
            self.assertIn('\n' + line + '\n', timer)
        self.assertEqual(files, k.render({**k.config(), 'autoUpdate': {'enabled': False, 'follow': ''}}))


if __name__ == '__main__':
    unittest.main()
