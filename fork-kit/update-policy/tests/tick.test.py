"""update-policy tick and booted: the restart policy with a fixed clock, fake idle facts and a fake host in a temporary
folder (state file, /run/reboot-required, the machine file). No root, no systemd, no restart. RedCheck breaks each
rule in a copy of the code and shows its test then fails.

  python3 -I -B fork-kit/update-policy/tests/tick.test.py
"""
import datetime, functools, json, os, re, shutil, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixture as fx  # noqa: E402
from fixture import at, settings, tick, window  # noqa: E402
import alert  # noqa: E402
from updatepolicy import exposure  # noqa: E402

MIN = datetime.timedelta(minutes=1)
# a fake GitHub token for the alertCommand refusal, built at run time so the source holds no token-shaped string
# (the secret scan's generic-api-key rule; the first push held it whole, allowed by value in .gitleaks.toml)
FAKE_GH_TOKEN = 'ghp' + '_' + 'abcdefgh' + '12345678'
SINCE = at('2026-11-23 06:40')
HANG = '/usr/local/bin/notify-hangs'
# seconds a run needs besides its alerts: the look, apt-config, the last look and systemctl reboot (60 s of its own)
REST_OF_THE_RUN = 120


def unit_seconds(name, key, default=None):
    """A time limit of one of the kit's systemd units, in seconds ('5min', '90s', '300')."""
    with open(os.path.join(fx.KIT, 'systemd', name)) as f:
        found = re.search(rf'^{key}=([0-9]+)(min|s)?$', f.read(), re.M)
    if not found:
        return default
    return int(found.group(1)) * (60 if found.group(2) == 'min' else 1)


def largest_alert_timeout():
    """The largest timeoutSeconds the machine file's "alert" section accepts."""
    check = alert.SPEC['timeoutSeconds'][2]
    return max(v for v in range(1, 3601) if check(v) is None)


def hang_alerts(m, host):
    """The alert command HANG never answers: each run of it takes its whole timeout on a fake clock (returned: a
    list holding the seconds gone). m runs the real Machine.alert."""
    clock, run = [0], host.run

    def hanging(argv, timeout=20, **kw):
        if argv[0] == HANG:
            clock[0] += timeout
            return None, '', f'{argv[0]}: no answer within {timeout} s'
        return run(argv, timeout=timeout, **kw)
    host.run, m.monotonic = hanging, lambda: clock[0]
    m.alert = functools.partial(tick.Machine.alert, m)
    return clock
PENDING = tick.Pending(SINCE, ['linux-image-6.8.0-50-generic'])


class Looks:
    """A fake look: busy reasons in turn ([] idle), and how often it was asked."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), 0

    def __call__(self):
        self.asked += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class Decide(unittest.TestCase):
    def setUp(self):
        self.s = fx.make_settings()     # dev, 05:00 UTC, 120 min, retry 15
        self.booted = at('2026-11-20 00:00')
        self.boot = 'boot-1'
        self.rebooting = True           # a restart asked for happens

    def run_ticks(self, start, end, looks, s=None, pending=PENDING, state=None, apt=None):
        """Ticks every 5 minutes from start to end (inclusive); (all actions, last state)."""
        s, state, acts, now = s or self.s, dict(state or {}), [], start
        while now <= end:
            a, state = tick.decide(now, s, state, pending, self.booted, looks, self.boot, apt)
            acts += [(now.strftime('%H:%M'),) + x for x in a]
            if any(x[0] == 'restart' for x in a) and self.rebooting:
                self.booted = now + 2 * MIN        # the restart happened: /run starts empty, a new boot id
                self.boot += '+'
                pending = None
            now += 5 * MIN
        return acts, state

    def test_outside_the_window_or_with_nothing_pending_nothing_happens(self):
        looks = Looks([])
        acts, state = self.run_ticks(at('2026-11-24 00:00'), at('2026-11-24 04:55'), looks)
        self.assertEqual((acts, looks.asked), ([], 0))
        acts, _ = self.run_ticks(at('2026-11-24 05:00'), at('2026-11-24 07:30'), looks, pending=None)
        self.assertEqual((acts, looks.asked), ([], 0))

    def test_idle_at_the_quiet_hour_restarts_once_and_says_why(self):
        looks = Looks([])
        acts, state = self.run_ticks(at('2026-11-24 04:50'), at('2026-11-24 07:30'), looks)
        self.assertEqual([a[:2] for a in acts], [('05:00', 'restart')])
        why = acts[0][2]
        self.assertIn('restart pending since 2026-11-23 06:40 UTC', why)
        self.assertIn('linux-image-6.8.0-50-generic', why)
        self.assertIn('idle in the quiet window 05:00-07:00 UTC (role dev)', why)
        self.assertEqual((state['restartWindow'], state['restartAt']), ('2026-11-24', '2026-11-24T05:00:00Z'))
        self.assertEqual(looks.asked, 1)

    def test_busy_looks_again_every_retry_and_restarts_when_idle(self):
        looks = Looks(['user 1000: 1 T3 agent scope(s) running'], ['heavy jobs'], [])
        acts, _ = self.run_ticks(at('2026-11-24 05:00'), at('2026-11-24 07:30'), looks)
        self.assertEqual([a[:2] for a in acts], [('05:00', 'log'), ('05:15', 'log'), ('05:30', 'restart')])
        self.assertIn('busy, so no restart now (next look in 15 min, window 05:00-07:00 UTC): user 1000: 1 T3 '
                      'agent scope(s) running', acts[0][3])
        self.assertEqual(looks.asked, 3)

    def test_busy_through_the_window_alerts_once_after_it_and_restarts_never(self):
        looks = Looks(['inhibitor lock: mutation-tests (uid 1000): hours left'])
        acts, state = self.run_ticks(at('2026-11-24 04:00'), at('2026-11-24 12:00'), looks)
        self.assertNotIn('restart', [a[1] for a in acts])
        self.assertEqual(looks.asked, 8)                         # 05:00, 05:15, ... 06:45
        alerts = [a for a in acts if a[1] == 'alert']
        self.assertEqual([(a[0], a[2]) for a in alerts], [('07:00', 'restart-pending')])
        self.assertIn('busy all through the quiet window of 2026-11-24 (last look: inhibitor lock: mutation-tests',
                      alerts[0][3])
        self.assertIn('Next try: 2026-11-25 05:00-07:00 UTC.', alerts[0][3])
        # the next night it tries again, and idle then, it restarts
        acts, _ = self.run_ticks(at('2026-11-25 04:00'), at('2026-11-25 05:30'), Looks([]), state=state)
        self.assertEqual([a[:2] for a in acts], [('05:00', 'restart')])

    def test_a_window_with_no_look_sends_no_alert(self):
        acts, state = self.run_ticks(at('2026-11-24 07:00'), at('2026-11-24 12:00'), Looks([]))
        self.assertEqual(acts, [])

    def test_a_machine_off_at_the_window_end_alerts_at_its_first_tick_then_looks_in_the_new_window(self):
        looks = Looks(['busy'])
        _, state = self.run_ticks(at('2026-11-24 05:00'), at('2026-11-24 05:10'), looks)
        acts, state = self.run_ticks(at('2026-11-25 05:20'), at('2026-11-25 05:20'), looks, state=state)
        self.assertEqual([a[1:3] for a in acts], [('alert', 'restart-pending'), ('log', 'info')])
        self.assertEqual(state['window'], '2026-11-25')

    def test_live_never_restarts_and_alerts_once_per_window(self):
        looks = Looks([])
        acts, _ = self.run_ticks(at('2026-11-24 04:00'), at('2026-11-25 07:30'), looks,
                                 s=fx.make_settings(role='live'))
        self.assertNotIn('restart', [a[1] for a in acts])
        self.assertEqual(looks.asked, 0)                         # idle or not does not matter
        self.assertEqual([(a[0], a[2]) for a in acts], [('05:00', 'live-restart-pending')] * 2)
        self.assertIn('Role live: this machine never restarts by itself', acts[0][3])

    def test_booted_inside_the_window_no_second_restart(self):
        self.booted = at('2026-11-24 05:03')             # a restart by hand, or the kit's before its state was lost
        acts, _ = self.run_ticks(at('2026-11-24 05:05'), at('2026-11-24 07:30'), Looks([]))
        self.assertEqual([a[1:3] for a in acts], [('log', 'notice')])
        self.assertIn('already restarted (or tried) in this window', acts[0][3])

    def test_a_restart_asked_for_in_this_window_is_not_asked_again(self):
        _, state = self.run_ticks(at('2026-11-24 05:00'), at('2026-11-24 05:00'), Looks([]))
        self.booted = at('2026-11-20 00:00')            # the restart failed: the machine is still up
        acts, _ = self.run_ticks(at('2026-11-24 05:05'), at('2026-11-24 07:30'), Looks([]), state=state)
        self.assertNotIn('restart', [a[1] for a in acts])

    def test_the_window_follows_the_time_zone_and_crosses_midnight(self):
        s = fx.make_settings(quietHour='23:30', timeZone='Europe/Berlin', windowMinutes=60)
        acts, _ = self.run_ticks(at('2026-11-24 22:00'), at('2026-11-24 23:30'), Looks([]), s=s)
        self.assertEqual([a[:2] for a in acts], [('22:30', 'restart')])     # 23:30 in Berlin (UTC+1 in winter)
        self.assertIn('23:30-00:30 Europe/Berlin', acts[0][2])
        summer = window.current(at('2026-07-01 21:45'), s)
        self.assertEqual((summer.start, summer.id), (at('2026-07-01 21:30'), '2026-07-01'))   # UTC+2 in summer

    def test_a_start_time_the_clock_skips_moves_later_and_one_it_passes_twice_is_the_first(self):
        s = fx.make_settings(quietHour='02:30', timeZone='Europe/Berlin', windowMinutes=30)
        w = window.current(at('2026-03-29 01:40'), s)            # 02:00 -> 03:00 that night: 02:30 is skipped
        self.assertEqual(w.start, at('2026-03-29 01:30'))        # 03:30 local
        self.assertIsNone(window.current(at('2026-03-29 00:45'), s))
        w = window.current(at('2026-10-25 00:40'), s)            # 03:00 -> 02:00: 02:30 comes twice
        self.assertEqual(w.start, at('2026-10-25 00:30'))        # the first, still UTC+2

    def test_a_restart_that_did_not_happen_is_one_restart_failed_alert(self):
        """The tick was killed after it wrote the state, or systemctl said yes and nothing came: the same boot id
        10 minutes on."""
        self.rebooting = False
        acts, state = self.run_ticks(at('2026-11-24 05:00'), at('2026-11-24 09:00'), Looks([]))
        self.assertEqual([a[:3] for a in acts if a[1] != 'log'],
                         [('05:00', 'restart', acts[0][2]), ('05:10', 'alert', 'restart-failed')])
        alert = [a for a in acts if a[1] == 'alert'][0]
        self.assertIn('the restart asked for at 2026-11-24T05:00:00Z did not happen (this is still the same boot)',
                      alert[3])
        self.rebooting = True                                    # the next night it tries again
        acts, _ = self.run_ticks(at('2026-11-25 05:00'), at('2026-11-25 06:00'), Looks([]), state=state)
        self.assertEqual([a[:2] for a in acts], [('05:00', 'restart')])

    def test_pending_since_is_when_this_boot_first_saw_it(self):
        """notify-reboot-required rewrites the file for every new package: its time is no age."""
        _, state = self.run_ticks(at('2026-11-24 12:00'), at('2026-11-24 12:00'), Looks([]))
        self.assertEqual(state['pendingSeen'], {'boot': 'boot-1', 'at': '2026-11-23T06:40:00Z'})
        rewritten = tick.Pending(at('2026-11-26 09:00'), ['linux-firmware'])
        self.s = fx.make_settings(role='live')
        acts, state = self.run_ticks(at('2026-11-27 05:00'), at('2026-11-27 05:00'), Looks([]), pending=rewritten,
                                     state=state)
        self.assertIn('restart pending since 2026-11-23 06:40 UTC (3.9 days) for linux-firmware', acts[0][3])
        self.boot = 'boot-2'                                     # a new boot: a new pending restart
        _, state = self.run_ticks(at('2026-11-27 12:00'), at('2026-11-27 12:00'), Looks([]), pending=rewritten,
                                  state=state)
        self.assertEqual(state['pendingSeen'], {'boot': 'boot-2', 'at': '2026-11-26T09:00:00Z'})
        _, state = self.run_ticks(at('2026-11-27 12:05'), at('2026-11-27 12:05'), Looks([]), pending=None,
                                  state=state)
        self.assertNotIn('pendingSeen', state)

    def test_apt_restarting_by_itself_is_alerted_at_once_then_daily(self):
        apt = 'unattended-upgrades restarts by itself (Unattended-Upgrade::Automatic-Reboot is "true"): ...'
        acts, state = self.run_ticks(at('2026-11-24 10:00'), at('2026-11-25 10:00'), Looks([]), pending=None,
                                     apt=apt)
        self.assertEqual([(a[0], a[2]) for a in acts], [('10:00', 'apt-restarts'), ('10:00', 'apt-restarts')])
        acts, state = self.run_ticks(at('2026-11-25 10:05'), at('2026-11-25 10:05'), Looks([]), pending=None,
                                     state=state)
        self.assertNotIn('aptAlertAt', state)                    # fixed: the next one alerts at once again

    def test_retry_after_a_clock_jump_back_looks_again(self):
        looks = Looks(['busy'])
        _, state = self.run_ticks(at('2026-11-24 05:30'), at('2026-11-24 05:30'), looks)
        self.run_ticks(at('2026-11-24 05:10'), at('2026-11-24 05:10'), looks, state=state)
        self.assertEqual(looks.asked, 2)


class Settings(unittest.TestCase):
    def test_role_quiet_hour_and_time_zone_are_required_and_checked(self):
        for missing in ('role', 'quietHour', 'timeZone'):
            base = {'role': 'dev', 'quietHour': '05:00', 'timeZone': 'UTC'}
            del base[missing]
            with self.subTest(missing=missing), self.assertRaises(settings.MachineFileError) as e:
                settings.check(base, 'test')
            self.assertIn(f'"{missing}" is missing', str(e.exception))
        for key, bad, words in (('role', 'server', 'dev, personal, live'), ('quietHour', '5:00', 'HH:MM'),
                                ('timeZone', 'Mars/Olympus', 'time zone'), ('windowMinutes', 0, 'from 5 to 720'),
                                ('alertCommand', ['notify'], 'absolute path'), ('containerUsers', ['A B'], 'user'),
                                ('heavySlice', 'heavy', '.slice'), ('busyScripts', ['busy'], 'absolute'),
                                ('baseline', {'ports': ['22']}, 'PROTO/PORT'), ('baseline', {'x': 1}, 'unknown key'),
                                ('typo', 1, 'unknown key'),
                                ('alertCommand', ['/usr/bin/notify', '--token', FAKE_GH_TOKEN], 'token'),
                                ('alertCommand', ['/usr/bin/curl', '-H', 'Authorization: Bearer ' + 'aB3' * 12],
                                 'root-only file'),
                                ('alertCommand', ['/usr/bin/notify', 'xoxb-1234-abcdefgh'], 'token'),
                                ('baseline', {'sshUsers': ['Bad User']}, 'user name')):
            with self.subTest(key=key, bad=bad), self.assertRaises(settings.MachineFileError) as e:
                fx.make_settings(**{key: bad})
            self.assertIn(words, str(e.exception))

    def test_plain_alert_arguments_are_fine(self):
        s = fx.make_settings(alertCommand=['/usr/local/bin/notify-owner', '--topic', 'updates',
                                           '--title=update-policy-on-this-very-long-machine-name-01',
                                           'https://ntfy.example/updates', '--key-file=/etc/notify/key'])
        self.assertEqual(len(s['alertCommand']), 6)

    def machine_file(self, content):
        folder = tempfile.mkdtemp(prefix='update-policy-machine-')
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        os.chmod(folder, 0o755)
        path = os.path.join(folder, 'machine.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(dict(content, version=1), f)
        os.chmod(path, 0o644)
        return path

    def test_the_shared_alert_channel_is_used_when_the_kit_has_no_command_of_its_own(self):
        base = {'role': 'dev', 'quietHour': '05:00', 'timeZone': 'UTC'}
        shared = {'command': ['/usr/local/bin/notify-all', '--topic', 'machine'], 'timeoutSeconds': 30}
        uid = os.getuid()
        s = settings.read(self.machine_file({'update-policy': base, 'alert': shared}), owner_uid=uid)
        self.assertEqual((s['alertCommand'], s['alertTimeout'], s['alertSource']),
                         (shared['command'], 30, 'alert'))
        own = dict(base, alertCommand=['/usr/local/bin/notify-owner'])
        s = settings.read(self.machine_file({'update-policy': own, 'alert': shared}), owner_uid=uid)
        self.assertEqual((s['alertCommand'], s['alertTimeout'], s['alertSource']),
                         (['/usr/local/bin/notify-owner'], 60, 'update-policy'))     # its own wins
        s = settings.read(self.machine_file({'update-policy': base}), owner_uid=uid)
        self.assertEqual((s['alertCommand'], s['alertSource']), ([], None))       # neither: the journal only
        for bad, words in (({'command': []}, 'must name a program'), ({'command': ['notify']}, 'absolute path'),
                           ({}, '"command" is missing'), ({'command': ['/x'], 'timeoutSeconds': 121}, '5 to 120'),
                           ({'command': ['/x'], 'retries': 3}, 'unknown key'),
                           ({'command': ['/usr/bin/notify', FAKE_GH_TOKEN]}, 'token')):
            with self.subTest(bad=bad), self.assertRaises(settings.MachineFileError) as e:
                settings.read(self.machine_file({'update-policy': base, 'alert': bad}), owner_uid=uid)
            self.assertIn(words, str(e.exception))       # a broken section is never taken for "no channel"

    def test_the_example_machine_file_section_parses(self):
        with open(os.path.join(fx.COMMON, 'machine.example.json')) as f:
            section = json.load(f)[settings.SECTION]
        s = settings.check(section, 'example')
        self.assertIn(s['role'], settings.ROLES)
        self.assertIsNotNone(s['baseline']['ports'])


class TickOnHost(fx.HostCase):
    """tick.tick and tick.booted: the state file, the lock, the machine file, alerts and the restart."""

    def setUp(self):
        super().setUp()
        self.m = fx.FakeMachine(self.host, at('2026-11-24 05:00'))
        self.s = fx.make_settings(alertCommand=['/usr/local/bin/notify'])
        self.looks = Looks([])

    def tick(self, s=None):
        s = s or self.s

        def read():
            if isinstance(s, Exception):
                raise s
            return s
        return tick.tick(self.m, read, lambda _s: self.looks())

    def state(self):
        with open(self.root + '/var/lib/update-policy/state.json') as f:
            return json.load(f)

    def test_the_state_is_written_before_the_restart(self):
        self.pending(SINCE, ['linux-image-6.8.0-50-generic'])
        seen = []
        self.m.reboot = lambda why: seen.append(self.state()) or None
        self.assertEqual(self.tick(), 0)
        self.assertEqual(seen[0]['restartAt'], '2026-11-24T05:00:00Z')
        self.assertIn('linux-image-6.8.0-50-generic', seen[0]['restartWhy'])
        self.assertIn(('notice', self.m.log[-1][1]), self.m.log)
        self.assertTrue(self.m.log.text().startswith('restarting now: restart pending since'))

    def machine(self):
        m = tick.Machine(self.host, fx.Logs())
        m.now = lambda: self.clock
        m.booted = lambda: at('2026-11-20 00:00')
        m.boot_id = lambda: 'boot-1'
        return m

    def test_a_lock_taken_after_the_look_stops_the_restart_with_one_alert(self):
        """The block locks are read again as the last step, after the state is written; systemctl itself is not
        asked to check them (--check-inhibitors=yes also refuses while another user is logged in)."""
        self.pending(SINCE)
        self.clock = at('2026-11-24 05:00')
        m = self.machine()
        locks = [json.dumps({'type': 'a(ssssuu)', 'data': [[]]}),
                 json.dumps({'type': 'a(ssssuu)', 'data': [[['shutdown', 'mutation-tests', 'hours left', 'block',
                                                            1000, 4242]]]})]
        self.host.answers['busctl'] = lambda argv: (0, locks.pop(0) if len(locks) > 1 else locks[0], '')
        self.assertEqual(tick.tick(m, lambda: self.s, lambda _s: fx.idle.look(self.host, _s)), 1)
        self.assertEqual([c[0] for c in self.host.calls if c[0][:2] == ['systemctl', 'reboot']], [])
        self.assertIn('the restart failed: not restarted: an inhibitor lock appeared after the look: mutation-tests '
                      '(uid 1000): hours left', m.log.text())
        for minutes in (10, 20, 60):                               # one restart-failed in the window, not two
            self.clock = at('2026-11-24 05:00') + minutes * MIN
            tick.tick(m, lambda: self.s, lambda _s: [])
        self.assertEqual(m.log.text().count('alert restart-failed'), 1)

    def test_systemctl_reboot_without_check_inhibitors_and_a_refusal_is_one_alert(self):
        self.pending(SINCE)
        self.clock = at('2026-11-24 05:00')
        m = self.machine()
        self.host.answers['systemctl reboot'] = (1, '', 'Failed to reboot system via logind: Access denied')
        self.assertEqual(tick.tick(m, lambda: self.s, lambda _s: []), 1)
        argv = [c[0] for c in self.host.calls if c[0][:2] == ['systemctl', 'reboot']][0]
        self.assertEqual(argv[:2], ['systemctl', 'reboot'])
        self.assertNotIn('--check-inhibitors=yes', argv)
        self.assertIn('alert restart-failed: the restart failed (Failed to reboot system via logind', m.log.text())
        for minutes in (10, 30):
            self.clock = at('2026-11-24 05:00') + minutes * MIN
            tick.tick(m, lambda: self.s, lambda _s: [])
        self.assertEqual(m.log.text().count('alert restart-failed'), 1)

    def test_the_tick_reads_apts_effective_value_every_time(self):
        self.host.answers['apt-config'] = (0, 'Unattended-Upgrade::Automatic-Reboot "false";\n'
                                              'Unattended-Upgrade::Automatic-Reboot "true";\n', '')
        self.assertEqual(self.tick(), 0)                           # nothing pending: still checked
        self.assertEqual([e for e, _ in self.m.alerts], ['apt-restarts'])
        self.assertIn('Automatic-Reboot is "true"', self.m.alerts[0][1])
        self.host.answers['apt-config'] = (None, '', 'apt-config: not installed')
        self.m.clock += datetime.timedelta(days=1)
        self.assertEqual(self.tick(), 0)
        self.assertIn('cannot tell whether unattended-upgrades restarts by itself', self.m.alerts[-1][1])

    def test_a_failed_restart_is_alerted_and_not_tried_again(self):
        self.pending(SINCE)
        self.m.reboot_error = 'Access denied'
        self.assertEqual(self.tick(), 1)
        self.assertEqual([e for e, _ in self.m.alerts], ['restart-failed'])
        self.m.clock += 15 * MIN
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.m.reboots), 1)

    def test_a_failed_alert_fails_the_tick_and_is_kept_for_the_baseline(self):
        self.pending(SINCE)
        self.s = fx.make_settings(role='live', alertCommand=['/usr/local/bin/notify'])
        self.m.alert_error = 'exit 7'
        self.assertEqual(self.tick(), 1)
        self.assertEqual(self.state()['lastAlertError'], 'exit 7')
        self.assertEqual(self.m.reboots, [])

    def test_hanging_alerts_leave_the_tick_time_to_save_that_they_failed(self):
        """With the largest timeout the machine file allows, two alerts that hang in one tick (apt restarts by
        itself, then the restart fails) still leave the tick inside update-policy.service's time limit with the
        failure in the state, so the baseline reports it and the next tick does not send the same alert again."""
        limit = unit_seconds('update-policy.service', 'TimeoutStartSec')
        s = settings.check({'role': 'dev', 'quietHour': '05:00', 'timeZone': 'UTC'}, 'test',
                           {'command': [HANG], 'timeoutSeconds': largest_alert_timeout()})
        self.pending(SINCE)
        self.host.answers['apt-config'] = (0, 'Unattended-Upgrade::Automatic-Reboot "true";\n', '')
        self.m.reboot_error = 'Access denied'
        clock, writes, write = hang_alerts(self.m, self.host), [], self.m.write_state
        self.m.write_state = lambda st: writes.append(clock[0]) or write(st)
        self.assertEqual(self.tick(s), 1)
        self.assertGreaterEqual(self.m.log.text().count('the alert command failed'), 2)
        self.assertLessEqual(clock[0] + REST_OF_THE_RUN, limit)
        self.assertTrue(writes)
        self.assertLessEqual(max(writes) + REST_OF_THE_RUN, limit)
        state = self.state()
        self.assertIn('aptAlertAt', state)                        # sent (and failed): not again on the next tick
        self.assertTrue(state['lastAlertError'])                  # the baseline reports it

    def test_no_alert_command_alerts_go_to_the_journal(self):
        self.pending(SINCE)
        self.assertEqual(self.tick(fx.make_settings(role='live')), 0)
        self.assertEqual(self.m.alerts, [])
        self.assertIn('alert live-restart-pending (no alert channel in the machine file: no "alert" section and no '
                      'alertCommand): restart pending', self.m.log.text())

    def test_the_alert_goes_out_with_the_channel_timeout(self):
        self.pending(SINCE)
        channel = {'command': ['/usr/local/bin/notify-all'], 'timeoutSeconds': 20}
        s = settings.check({'role': 'live', 'quietHour': '05:00', 'timeZone': 'UTC'}, 'test', channel)
        self.assertEqual(self.tick(s), 0)
        self.assertEqual(self.m.alert_calls, [(['/usr/local/bin/notify-all'], 'live-restart-pending', 20)])

    def test_no_machine_file_never_restarts_and_says_so_once_an_hour(self):
        broken = settings.MachineFileError('/etc/fork-kit/machine.json: no "update-policy" section')
        self.assertEqual(self.tick(broken), 0)                   # nothing pending: nothing to say
        self.assertEqual(self.m.log, [])
        self.pending(SINCE)
        for _ in range(13):                                      # an hour of ticks
            self.assertEqual(self.tick(broken), 1)
            self.m.clock += 5 * MIN
        self.assertEqual(self.m.reboots, [])
        self.assertEqual([lvl for lvl, _ in self.m.log], ['err', 'err'])
        self.assertIn('the machine file cannot be used, so nothing restarts', self.m.log[0][1])

    def test_broken_or_old_state_starts_empty(self):
        self.pending(SINCE)
        for text in ('{not json', json.dumps({'v': 99, 'restartWindow': '2026-11-24'}), '[]'):
            with self.subTest(text=text):
                self.write('/var/lib/update-policy/state.json', text)
                self.m.reboots.clear()
                self.assertEqual(self.tick(), 0)
                self.assertEqual(len(self.m.reboots), 1)
                self.assertIn('starting from empty state', self.m.log.text())

    def test_a_second_run_while_one_holds_the_lock_does_nothing(self):
        self.pending(SINCE)
        with self.m.lock() as mine:
            self.assertTrue(mine)
            self.assertEqual(self.tick(), 0)
        self.assertEqual(self.m.reboots, [])
        self.assertIn('another update-policy run holds the lock', self.m.log.text())

    def test_pending_reads_the_file_time_and_packages(self):
        self.pending(SINCE, ['linux-base', 'linux-image-6.8.0-50-generic', 'linux-base'])
        p = self.m.pending()
        self.assertEqual((p.since, p.packages), (SINCE, ['linux-base', 'linux-image-6.8.0-50-generic']))
        self.write('/proc/stat', 'cpu  1 2 3\nbtime 1795496400\n')
        self.assertEqual(tick.Machine.booted(self.m), at('2026-11-24 05:00'))

    def test_booted_says_once_that_the_restart_came_back(self):
        self.pending(SINCE)
        self.tick()
        self.m.boot, self.m.clock = at('2026-11-24 05:02'), at('2026-11-24 05:03')
        self.m.log.clear()
        s = fx.make_settings(alertOnBoot=True, alertCommand=['/usr/local/bin/notify'])
        self.assertEqual(tick.booted(self.m, lambda: s), 0)
        self.assertIn('back up: booted 2026-11-24T05:02:00Z, 2 min after the restart asked at 2026-11-24T05:00:00Z',
                      self.m.log.text())
        self.assertEqual([e for e, _ in self.m.alerts], ['back-up'])
        self.assertEqual(tick.booted(self.m, lambda: s), 0)       # once
        self.assertEqual(len(self.m.alerts), 1)

    def test_booted_without_a_restart_of_the_kit_says_nothing(self):
        self.assertEqual(tick.booted(self.m, lambda: self.s), 0)
        self.assertEqual((self.m.log, self.m.alerts), ([], []))


class Cli(fx.HostCase):
    def test_tick_runs_from_the_command(self):
        self.pending(SINCE)
        m = fx.FakeMachine(self.host, at('2026-11-24 12:00'))
        s = fx.make_settings()
        self.assertEqual(tick.tick(m, lambda: s, lambda _s: []), 0)
        self.assertEqual(m.reboots, [])

    def test_idle_and_status_commands(self):
        s = fx.make_settings(role='live')
        with mock.patch('builtins.print') as out:
            self.assertEqual(fx.cli.main(['idle'], host=self.host, read_settings=lambda: s), 0)
            self.process(9, '/usr/bin/apt-get', 'install', 'x')
            self.assertEqual(fx.cli.main(['idle', '--json'], host=self.host, read_settings=lambda: s), 75)
            self.assertEqual(fx.cli.main(['status'], host=self.host, read_settings=lambda: s), 0)
        printed = [c.args[0] for c in out.call_args_list]
        self.assertEqual(printed[0], 'idle')
        self.assertEqual(json.loads(printed[1])['busy'], ['package manager at work: apt-get (9)'])
        self.assertEqual(printed[2], 'role: live (never restarts; alerts)')
        self.assertTrue(printed[3].startswith('window: '))
        self.assertEqual(printed[4], 'pending: no restart pending')

        def broken():
            raise settings.MachineFileError('/etc/fork-kit/machine.json: not there')
        with mock.patch('builtins.print'):
            for cmd in ('idle', 'status', 'baseline', 'test-alert'):
                self.assertEqual(fx.cli.main([cmd], host=self.host, read_settings=broken), 1, cmd)

    def test_alert_command_gets_the_text_on_stdin_and_the_event(self):
        m = tick.Machine(self.host, fx.Logs())
        self.host.answers['/usr/local/bin/notify'] = (0, '', '')
        self.assertIsNone(m.alert(['/usr/local/bin/notify', '--topic', 'x'], 'drift', 'two\nlines'))
        argv, stdin, env = self.host.calls[-1]
        self.assertEqual((argv, stdin, env['UPDATE_POLICY_EVENT']), (['/usr/local/bin/notify', '--topic', 'x'],
                                                                      'two\nlines\n', 'drift'))
        self.assertEqual(sorted(env), ['FORK_KIT_ALERT_EVENT', 'FORK_KIT_ALERT_HOST', 'FORK_KIT_ALERT_KIT', 'LANG',
                                       'PATH', 'UPDATE_POLICY_EVENT', 'UPDATE_POLICY_HOST'])
        self.assertEqual((env['FORK_KIT_ALERT_KIT'], env['FORK_KIT_ALERT_EVENT']), ('update-policy', 'drift'))
        self.assertEqual(env['FORK_KIT_ALERT_HOST'], env['UPDATE_POLICY_HOST'])
        self.host.answers['/usr/local/bin/notify'] = (3, '', 'no network')
        self.assertEqual(m.alert(['/usr/local/bin/notify'], 'drift', 'x'), 'no network')

    def test_the_alert_budgets_fit_inside_the_units_time_limits(self):
        """One alert at the largest timeout fits a run's budget; the budget and the rest of the run fit each unit
        that alerts; exposure-alert (an ExecStopPost=, under TimeoutStopSec: 90 s by default) has its own."""
        self.assertLessEqual(largest_alert_timeout(), tick.ALERT_BUDGET)
        for unit in ('update-policy.service', 'update-policy-boot.service', 'update-policy-baseline.service'):
            with self.subTest(unit=unit):
                self.assertLessEqual(tick.ALERT_BUDGET + REST_OF_THE_RUN, unit_seconds(unit, 'TimeoutStartSec'))
        stop = unit_seconds('update-policy-exposure.service', 'TimeoutStopSec', default=90)
        self.assertLessEqual(exposure.ALERT_BUDGET + 10, stop)
        m = fx.FakeMachine(self.host, at('2026-11-24 07:00'))
        clock = hang_alerts(m, self.host)
        m.limit_alerts(exposure.ALERT_BUDGET)
        s = settings.check({'role': 'dev', 'quietHour': '05:00', 'timeZone': 'UTC'}, 'test',
                           {'command': [HANG], 'timeoutSeconds': largest_alert_timeout()})
        for _ in range(3):
            self.assertTrue(tick.send_alert(m, s, 'exposure', 'x'))
        self.assertLessEqual(clock[0], exposure.ALERT_BUDGET)
        self.assertIn('not sent: the alerts of this run already took their 75 s', m.log.text())

    def test_test_alert_says_which_channel_took_it(self):
        base = {'role': 'dev', 'quietHour': '05:00', 'timeZone': 'UTC'}
        m = fx.FakeMachine(self.host, at('2026-11-24 08:00'))
        cases = ((settings.check(dict(base, alertCommand=['/usr/local/bin/own']), 'test',
                                 {'command': ['/usr/local/bin/shared'], 'timeoutSeconds': 60}),
                  None, 0, 'sent through the update-policy alertCommand'),
                 (settings.check(base, 'test', {'command': ['/usr/local/bin/shared'], 'timeoutSeconds': 60}),
                  None, 0, 'sent through the machine file\'s "alert" section'),
                 (settings.check(base, 'test', {'command': ['/usr/local/bin/shared'], 'timeoutSeconds': 60}),
                  'exit 7', 1, 'and it failed: exit 7'),
                 (settings.check(base, 'test'), None, 1, 'no alert channel'))
        for s, error, code, words in cases:
            with self.subTest(words=words), mock.patch('builtins.print') as out, \
                    mock.patch.object(fx.cli, 'make', lambda host=None: (self.host, m)):
                m.alert_error, sent = error, len(m.alert_calls)
                self.assertEqual(fx.cli.main(['test-alert'], host=self.host, read_settings=lambda: s), code)
                self.assertIn(words, out.call_args_list[-1].args[0])
                self.assertEqual(len(m.alert_calls) - sent, 0 if words == 'no alert channel' else 1)
        self.assertEqual([c[0] for c in m.alert_calls],
                         [['/usr/local/bin/own'], ['/usr/local/bin/shared'], ['/usr/local/bin/shared']])
        self.assertEqual({c[1] for c in m.alert_calls}, {'test'})


MUTATIONS = {
    'the shared alert channel is the fallback': (
        'settings', "    if not out['alertCommand'] and channel:", "    if False:",
        'Settings.test_the_shared_alert_channel_is_used_when_the_kit_has_no_command_of_its_own'),
    'all the alerts of a run share one budget': (
        'tick', "        timeout = min(timeout, int(self.alert_left))", "        pass",
        'TickOnHost.test_hanging_alerts_leave_the_tick_time_to_save_that_they_failed'),
    'the alert runs with its channel timeout': (
        'tick', "timeout=s.get('alertTimeout') or alert_channel.TIMEOUT)", "timeout=None)",
        'TickOnHost.test_the_alert_goes_out_with_the_channel_timeout'),
    'live never restarts': (
        'tick', "    if s['role'] not in settings_mod.RESTARTS:\n        if 'live' not in alerted:",
        "    if False:\n        if 'live' not in alerted:",
        'Decide.test_live_never_restarts_and_alerts_once_per_window'),
    'only live roles skip restarts': (
        'settings', "RESTARTS = ('dev', 'personal')", "RESTARTS = ('dev', 'personal', 'live')",
        'Decide.test_live_never_restarts_and_alerts_once_per_window'),
    'busy: no restart': (
        'tick', "    if busy:\n        st['busy'] = busy", "    if False:\n        st['busy'] = busy",
        'Decide.test_busy_through_the_window_alerts_once_after_it_and_restarts_never'),
    'busy: looks wait for retryMinutes': (
        'tick', "    if last and last <= now < last", "    if False and last <= now < last",
        'Decide.test_busy_looks_again_every_retry_and_restarts_when_idle'),
    'the alert after the window is sent once': (
        'tick', "and 'pending' not in alerted", "and True",
        'Decide.test_busy_through_the_window_alerts_once_after_it_and_restarts_never'),
    'one restart per window: the boot time': (
        'tick', "    if booted >= w.start or", "    if False or",
        'Decide.test_booted_inside_the_window_no_second_restart'),
    'one restart per window: the state': (
        'tick', " or st.get('restartWindow') == w.id:", ":",
        'Decide.test_a_restart_asked_for_in_this_window_is_not_asked_again'),
    'outside the window: nothing': (
        'window', "        if w.start <= now < w.end:", "        if w.start <= now:",
        'Decide.test_outside_the_window_or_with_nothing_pending_nothing_happens'),
    'the state before the restart': (
        'tick', "    m.write_state(st)\n    if restart:", "    if restart:",
        'TickOnHost.test_the_state_is_written_before_the_restart'),
    'token-looking alert arguments are refused': (
        'settings', "    return alert.command_problem(value) if value else None", "    return None",
        'Settings.test_role_quiet_hour_and_time_zone_are_required_and_checked'),
    'a restart that did not come is reported': (
        'tick', "    if (asked and st.get('restartBoot') == boot", "    if (False and st.get('restartBoot') == boot",
        'Decide.test_a_restart_that_did_not_happen_is_one_restart_failed_alert'),
    'pending since: the first sight in this boot': (
        'tick', "    since = min(at, pending.since) if at else pending.since", "    since = pending.since",
        'Decide.test_pending_since_is_when_this_boot_first_saw_it'),
    'apt restarting by itself is alerted': (
        'tick', "    if apt_problem:\n", "    if False:\n",
        'Decide.test_apt_restarting_by_itself_is_alerted_at_once_then_daily'),
    'block locks are read again right before the restart': (
        'tick', "        err = m.last_look()\n", "        err = None\n",
        'TickOnHost.test_a_lock_taken_after_the_look_stops_the_restart_with_one_alert'),
    'one restart-failed per window': (
        'tick', "            st['restartMissed'] = st.get('restartAt')", "            pass",
        'TickOnHost.test_systemctl_reboot_without_check_inhibitors_and_a_refusal_is_one_alert'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        if os.environ.get(fx.LIB_ENV):
            self.skipTest('a red check run')
        fx.red_check(self, os.path.abspath(__file__), MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
