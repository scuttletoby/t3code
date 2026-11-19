"""pc-agents auto-update on a fake WSL2 distro (fake_distro.py; a fake release list, T3's fake answer, fake processes
under T3): off does nothing; a newer published release is downloaded, switched to only at a quiet point, checked
and rolled back when it does not come up (and not tried again); a run again changes nothing; a run stopped at any
step is finished or rolled back by the next; no journal, a bad one, a bad machine file, an empty release list or an
API error do nothing and say so; old versions are pruned to the newest three.

  python3 -I -B fork-kit/pc-agents/tests/update.test.py
"""
import fcntl, json, os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_distro import CONFIG, SHA1, Case, cli, k  # noqa: E402
import machine_file  # noqa: E402

FORK = 'example/t3code'
AUTO = {**CONFIG, 't3ReleaseBaseUrl': f'https://github.com/{FORK}/releases/download',
        'autoUpdate': {'enabled': True, 'follow': FORK}}
VERSIONS = '/opt/pc-agents/t3/runtime/versions'


def release(version, draft=False, prerelease=False):
    return {'tag_name': 'v' + version, 'draft': draft, 'prerelease': prerelease, 'name': version}


class Update(Case):
    def setUp(self):
        super().setUp()
        self.no_watch()
        k.HTTP_WAIT, k.HTTP_STEP = 0.2, 0.05   # 180 s and 5 s on a machine
        self.machine(AUTO)
        self.ready()
        self.releases(release('1.2.3'))

    def releases(self, *items):
        self.write('/log/releases.json', json.dumps(list(items)))

    def update(self):
        return self.call(cli.main, ['auto-update'])

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
        self.assertNotIn('phase', self.state())
        self.assertIn(f'T3 {to} runs (was {was})', self.state()['result'])
        code, out = self.check()
        self.assertEqual(code, 0, out)

    def process(self, pid, parent, exe, name='claude'):
        self.write(f'/proc/{pid}/stat', f'{pid} ({name}) S {parent} {pid} {pid} 0 -1 4194560\n')
        self.write(f'/proc/{pid}/comm', name + '\n')
        os.symlink(exe, self.path(f'/proc/{pid}/exe'))

    def main_pid(self):
        return self.read('/log/units/t3code-agents.service.pid').strip()

    def test_newer_release_is_downloaded_switched_to_and_checked(self):
        self.write('/etc/fork-kit/machine.json', json.dumps({'version': 1, 'pc-agents': AUTO}, indent=4) + '\n')
        before = self.read('/etc/fork-kit/machine.json')
        self.releases(release('1.2.4'), release('1.2.3'))
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.updated()
        self.assertIn(f'T3CODE_VERSION=1.2.4 T3CODE_RELEASE_BASE_URL={AUTO["t3ReleaseBaseUrl"]}',
                      self.read('/log/install'))
        self.assertEqual(self.read('/etc/fork-kit/machine.json.bak'), before)   # the copy, as it was
        self.assertEqual(self.read('/etc/fork-kit/machine.json'),   # only the version changed, the layout kept
                         before.replace('"t3Version": "1.2.3"', '"t3Version": "1.2.4"'))
        self.assertTrue(os.path.isdir(self.path(VERSIONS + '/1.2.3')))   # kept: the one to go back to
        self.assertIn(f'{VERSIONS}/1.2.4/t3', k.record())
        self.assertIn(f'https://api.github.com/repos/{FORK}/releases?per_page=30', self.read('/log/curl'))
        self.assertIn('http://127.0.0.1:3800/.well-known/t3/environment', self.read('/log/curl'))

    def test_off_does_nothing(self):
        self.machine(CONFIG)
        before = self.snapshot()
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('autoUpdate is off', out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.log('curl'), [])

    def test_up_to_date_and_a_run_again_change_nothing(self):
        self.releases(release('1.2.4'))
        self.assertEqual(self.update()[0], 0)
        self.assertFalse(os.path.exists(self.path(k.PENDING)))
        mine = (k.STATE, k.UPDATE_STATE)   # the journal (its result and time), and its folder's mtime
        before = {p: v for p, v in self.snapshot().items() if p not in mine}
        installs, starts = self.log('install'), self.log()
        for _ in range(2):
            code, out = self.update()
            self.assertEqual(code, 0, out)
            self.assertIn('T3 1.2.4 is up to date', out)
        self.assertEqual({p: v for p, v in self.snapshot().items() if p not in mine}, before)
        self.assertEqual(self.log('install'), installs)
        self.assertFalse([x for x in self.log()[len(starts):] if x.startswith(('restart', 'stop', 'enable'))])

    def test_only_published_full_releases_on_the_stable_track(self):
        self.releases(release('9.0.0', draft=True), release('8.0.0', prerelease=True),
                      release('7.0.0-nightly.20261118.12'), release('1.2.2'), release('1.3.0'))
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('T3 1.2.3 is up to date (the newest release of example/t3code is 1.2.2)', out)
        self.assertEqual(self.version(), '1.2.3')
        nightly = '0.1.11-nightly.20261118.4890.fork.1'   # the fork's build of a nightly: a full release, stable track
        self.assertEqual(k.newest_release([{'tag_name': 'v' + nightly, 'draft': False, 'prerelease': False}]), nightly)
        self.assertIsNone(k.newest_release([{'tag_name': 'v1.2.4'}, {'tag_name': 'x', 'draft': False,
                                                                     'prerelease': False}, 'v1.2.5', None]))
        order = ['1.2.3-fork.1', '1.2.3-fork.2', '1.2.3-fork.10', '1.2.3', '1.2.4-fork.1',   # "fork" < "nightly"
                 '1.2.4-nightly.20261118.1.fork.1', '1.2.4-nightly.20261119.1.fork.1', '1.2.4']
        self.assertEqual(sorted(reversed(order), key=k.semver_key), order)

    def test_nothing_to_read_does_nothing_and_says_so(self):
        cases = (('releases-fail', '', 1, 'cannot read the release list'),
                 ('releases.json', '[]', 0, 'has no published release T3 can take'),
                 ('releases.json', '{"message": "Not Found"}', 1, 'the release list is not a list'),
                 ('releases.json', '<html>', 1, 'the release list is not JSON'))
        for name, text, want, said in cases:
            with self.subTest(name, text=text):
                self.write('/log/' + name, text)
                code, out = self.update()
                self.assertEqual(code, want, out)
                self.assertIn(said, out)
                self.assertIn(said, self.state()['result'])
                self.assertEqual(self.version(), '1.2.3')
                self.assertNotIn('1.2.4', self.read('/log/install'))
                os.unlink(self.path('/log/' + name))

    def test_no_journal_a_bad_one_and_a_bad_machine_file(self):
        self.assertFalse(os.path.exists(self.path(k.UPDATE_STATE)))   # none yet: a first run
        self.write(k.UPDATE_STATE, '{"phase": "switching", "from"')
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('cannot be read', out)
        self.assertIn('up to date', self.state()['result'])
        self.write(k.UPDATE_STATE, '["switching"]')
        self.assertIn('not a JSON object', self.update()[1])
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
        code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertIn('t3ReleaseBaseUrl: is https://example.com/releases, but autoUpdate follows the releases of '
                      f'{FORK}: set it to {AUTO["t3ReleaseBaseUrl"]}', out)
        self.assertEqual(self.log('curl'), [])
        code, out = self.setup_run()   # setup refuses it too
        self.assertEqual(code, 2, out)

    def test_waits_for_a_quiet_point(self):
        self.releases(release('1.2.4'))
        main = self.main_pid()
        self.process(4001, main, VERSIONS + '/1.2.3/t3', 't3-helper')   # T3's own helper: not work
        self.process(4002, 4001, '/home/agent/.local/bin/claude')   # an agent's session, below that helper
        self.process(4003, 1, '/usr/bin/node', 'node')   # a dev server outside T3's tree: holds nothing
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('T3 1.2.4 is downloaded; waiting for a quiet point (T3 runs 1 other process(es): claude (4002))',
                      out)
        self.assertEqual((self.version(), self.serving(), self.main_pid()), ('1.2.3', '1.2.3', main))
        self.assertTrue(os.path.isfile(self.path(VERSIONS + '/1.2.4/t3')))   # downloaded ahead
        os.chmod(self.path('/proc/4002/stat'), 0)   # unreadable (only as a user: root reads it): busy
        if os.geteuid():
            self.assertIn('cannot read process 4002', self.update()[1])
        os.chmod(self.path('/proc/4002/stat'), 0o644)
        os.unlink(self.path('/proc/4002/stat'))   # it ended
        installs = len(self.log('install'))
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.updated()
        self.assertEqual(len(self.log('install')), installs)   # not downloaded twice

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
                self.assertEqual(self.check()[0], 0)
                installs = len(self.log('install'))
                code, out = self.update()
                self.assertEqual(code, 0, out)
                self.assertIn('T3 1.2.4 did not come up before: not trying it again', out)
                self.assertEqual(len(self.log('install')), installs)
                os.unlink(self.path('/log/' + name))
                os.unlink(self.path(k.UPDATE_STATE))
        self.write('/log/t3-crash-version', '1.2.4')
        self.update()
        self.releases(release('1.2.5'), release('1.2.4'))   # a newer one is tried
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.updated('1.2.5')
        self.assertEqual(self.state()['failed'], '1.2.4')

    def test_old_version_not_coming_back_says_so(self):
        self.write('/log/t3-crash-version', '1.2.4')
        self.releases(release('1.2.4'))
        real = cli.setup
        with mock.patch.object(cli, 'setup', lambda *a: (self.write('/log/t3-crash', ''), real(*a))[1]):
            code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertIn('Back on 1.2.3, but that does not come up either (setup failed: T3 did not start',
                      self.state()['result'])
        self.assertEqual(self.version(), '1.2.3')

    def test_a_download_that_fails_or_was_cut_is_tried_again(self):
        self.releases(release('1.2.4'))
        self.write('/log/install-fail', '')
        code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertIn('T3 1.2.4 did not download', out)
        self.assertEqual((self.version(), self.serving()), ('1.2.3', '1.2.3'))
        os.unlink(self.path('/log/install-fail'))
        os.makedirs(self.path(VERSIONS + '/1.2.4/lib'))   # a download cut before its t3 was there
        os.makedirs(self.path(VERSIONS + '/.removing-77'))   # a removal cut half-way
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('no t3: removing it to install T3 1.2.4 again', out)
        self.assertFalse(os.path.exists(self.path(VERSIONS + '/.removing-77')))
        self.updated()

    def test_stopped_half_way_is_finished_or_rolled_back(self):
        kept = self.keep()
        cuts = (('before the machine file', 'set_version', lambda v: v == '1.2.4'),
                ('after the machine file', 'setup', lambda *a: True),
                ('in setup, after its writes', 'install_t3', lambda c, *a, prefetch=False: c['t3Version'] == '1.2.4'
                 and not prefetch),
                ('before the check', 'serves', lambda *a: True))
        for name, fn, when in cuts:
            with self.subTest(name):
                self.restore(kept)
                self.releases(release('1.2.4'))
                self.cut(fn, when)
                self.assertEqual(self.state()['phase'], 'switching')
                code, out = self.update()
                self.assertEqual(code, 0, out)
                self.updated()

    def test_cut_rollback_is_finished(self):
        self.write('/log/t3-crash-version', '1.2.4')
        self.releases(release('1.2.4'))
        self.cut('set_version', lambda v: v == '1.2.3')
        self.assertEqual(self.state()['phase'], 'rolling-back')
        self.assertEqual(self.version(), '1.2.4')
        code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertEqual((self.version(), self.serving(), self.state()['failed']), ('1.2.3', '1.2.3', '1.2.4'))
        self.assertIn('Back on 1.2.3, running', self.state()['result'])

    def test_a_switch_cut_too_often_is_rolled_back(self):
        self.releases(release('1.2.4'))
        for _ in range(1 + k.UPDATE_TRIES):
            self.cut('setup')
        self.assertEqual(self.state()['tries'], k.UPDATE_TRIES)
        code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertIn(f'did not come up (its switch was cut {k.UPDATE_TRIES} times)', out)
        self.assertEqual((self.version(), self.serving(), self.state()['failed']), ('1.2.3', '1.2.3', '1.2.4'))

    def test_a_cut_switch_the_owner_changed_is_left_alone(self):
        self.write(k.UPDATE_STATE, json.dumps({'phase': 'switching', 'from': '1.2.2', 'to': '1.2.4', 'tries': 1}))
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn('a cut update (1.2.2 to 1.2.4) is left alone: the machine file says t3Version 1.2.3 now', out)
        self.assertNotIn('phase', self.state())
        self.assertEqual(self.version(), '1.2.3')

    def test_only_from_a_finished_setup_a_running_t3_and_holding_walls(self):
        self.releases(release('1.2.4'))
        for name, change, said in (
                ('pending', lambda: self.write(k.PENDING, 'x'), 'setup has not finished'),
                ('stopped', lambda: k.run(['/usr/bin/systemctl', 'stop', k.UNIT]), 'T3 is not running'),
                ('wall', lambda: os.unlink(self.path('/log/nft-loaded')), 'a wall does not hold (FAIL  firewall')):
            with self.subTest(name):
                kept = self.keep()
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

    def test_one_at_a_time(self):
        os.makedirs(self.path('/run/pc-agents'), exist_ok=True)
        with open(self.path(k.LOCK), 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)   # a setup that runs
            for argv in (['auto-update'], ['setup', '--source', '/', '--sha', SHA1]):
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
