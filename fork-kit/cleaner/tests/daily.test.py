"""`fork-cleaner daily` (lib/cleaner/daily.py), what the kit's timer runs, and `fork-cleaner compare`
(lib/cleaner/compare.py). Mode "compare" plans, reports and compares and never deletes; mode "run" refuses while an
old tool's timer is on. The state questions of #491 each have a test: both tools installed, the old tool's log and
state left behind, an empty config. systemd and the old tools are stub commands. RedCheck breaks each rule in a copy
of the code and shows its test then fails.

  python3 -I -B fork-kit/cleaner/tests/daily.test.py
"""
import json, os, sys, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cleaner_fixture as fx  # noqa: E402

import machine_file  # noqa: E402

# `systemctl --user|--system is-enabled|is-active UNIT` and `show -p Unit --value TIMER`: the state file is
# {unit: [is-enabled, is-active]} for the user scope and {"system": {...}} for the system scope; a unit not in it is
# not-found/inactive, as systemd answers. A timer starts <name>.service unless {"starts": {timer: service}} says
# otherwise. {"fail": true}: systemd cannot be asked.
STUB_SYSTEMCTL = '''#!{python} -I
import json, sys
with open({state!r}) as f:
    state = json.load(f)
with open({state!r} + '.calls', 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
if state.get('fail'):
    sys.stderr.write('Failed to connect to bus\\n')
    sys.exit(1)
scope = sys.argv[1]
assert scope in ('--user', '--system'), sys.argv
units = state.get('system', {{}}) if scope == '--system' else state
if sys.argv[2] == 'show':
    assert sys.argv[3:6] == ['-p', 'Unit', '--value'], sys.argv
    unit = sys.argv[6]
    print(state.get('starts', {{}}).get(unit, unit[:-len('.timer')] + '.service') if unit in units else '')
    sys.exit(0)
enabled, active = units.get(sys.argv[3], ['not-found', 'inactive'])
answer = enabled if sys.argv[2] == 'is-enabled' else active
print(answer)
sys.exit(0 if answer in ('enabled', 'active', 'static') else 4 if answer == 'not-found' else 1)
'''

# Both old timers there and off, in the user scope.
OFF = {'old-retention.timer': ['disabled', 'inactive'], 'old-prune.timer': ['disabled', 'inactive']}

# An old tool's dry run: prints the lines of its state file, ends with its exit code.
STUB_OLD = '''#!{python} -I
import json, sys
with open({state!r}) as f:
    state = json.load(f)
with open({state!r} + '.calls', 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
for line in state['lines']:
    print(line)
sys.exit(state.get('code', 0))
'''
OLD_PATTERN = r'^\S+ would delete (/.+?) \(\d'


def read(path):
    with open(path) as f:
        return f.read()


class Case(fx.Case):
    c = fx.load()

    def setUp(self):
        super().setUp()
        self.systemctl, self.units = self.stub('systemctl', STUB_SYSTEMCTL, {})
        self.old, self.old_state = self.stub('old', STUB_OLD, {'lines': []})

    def set_units(self, units):
        with open(self.units, 'w') as f:
            json.dump(units, f)

    def set_old(self, lines, code=0):
        with open(self.old_state, 'w') as f:
            json.dump({'lines': lines, 'code': code}, f)

    def daily_cfg(self, mode=None, timers=None, sources=None, **section):
        daily = {'outDir': self.path('state', 'daily')}
        if mode:
            daily['mode'] = mode
        if timers is not None:
            daily['oldTimers'] = timers
        if sources is not None:
            daily['compare'] = sources
        return self.tmp_cfg(daily=daily, **section)

    def source(self, name='old-retention', pattern=OLD_PATTERN):
        return {'name': name, 'command': [self.old, 'plan'], 'pattern': pattern}

    def daily(self, cfg, at=None):
        lines = []
        report = lambda cfg, say, out=None: self.c.report.report(          # noqa: E731
            cfg, say, docker=lambda: {}, host='host-1', mounts=lambda: {'/'}, out=out,
            disk_of=lambda cfg: (1 << 30, 1 << 31, os.stat(cfg.home).st_dev))
        code = self.c.daily.daily(cfg, lines.append, facts=self.facts, systemctl=self.systemctl, report=report,
                                  now=lambda: at or self.now)
        return code, lines

    def out(self, name):
        return read(self.path('state', 'daily', name))

    def today(self):
        return time.strftime('%Y-%m-%d', time.gmtime(self.now))


class Daily(Case):
    def test_compare_mode_plans_reports_and_compares_and_deletes_nothing(self):
        job = self.old_dir('job-1')
        self.set_old([f'2026-01-01T00:00:00Z would delete {job} (1.0 MB, tmp: old)'])
        cfg = self.daily_cfg(sources=[self.source()])
        code, lines = self.daily(cfg)
        self.assertEqual(code, 0, lines)
        self.assertTrue(os.path.isdir(job))                                 # compare mode deletes nothing
        self.assertIn(f'would delete {job}', self.out(f'plan-{self.today()}.txt'))
        self.assertTrue(self.out('report.log').startswith('disk on host-1: '))
        self.assertIn('new only (a bug until explained): 0', self.out(f'compare-{self.today()}.txt'))
        self.assertIn('both: 1', self.out(f'compare-{self.today()}.txt'))
        self.assertTrue(lines[-1].startswith('fork-cleaner daily (compare): plan exit 0'), lines)
        self.assertFalse(os.path.lexists(cfg.log_file))                     # a plan writes no log
        self.assertEqual(self.calls(self.units), [])                        # no old timer named: systemd not asked

    def test_both_installed_compare_mode_runs_beside_the_old_timer(self):
        """#491, state: the old tool's timer is still on. Compare mode runs anyway (it deletes nothing) and says so."""
        job = self.old_dir('job-1')
        self.set_units({'old-retention.timer': ['enabled', 'active']})
        code, lines = self.daily(self.daily_cfg(timers=['old-retention.timer']))
        self.assertEqual(code, 0, lines)
        self.assertTrue(os.path.isdir(job))
        self.assertIn('old timers on: old-retention.timer (user: enabled, active)', lines[0])

    def test_run_mode_refuses_while_an_old_timer_is_on(self):
        job = self.old_dir('job-1')
        cfg = self.daily_cfg('run', ['old-retention.timer', 'old-prune.timer'])
        prune_gone = {'old-retention.timer': OFF['old-retention.timer']}
        for units, why in (({**OFF, 'old-retention.timer': ['enabled', 'active']}, 'user: enabled, active'),
                           ({**OFF, 'old-prune.timer': ['disabled', 'active']}, 'user: disabled, active'),
                           ({**OFF, 'old-prune.timer': ['enabled', 'inactive']}, 'user: enabled, inactive'),
                           ({**OFF, 'old-prune.timer': ['static', 'inactive']}, 'user: static, inactive'),
                           # the system scope is asked too
                           ({**OFF, 'system': {'old-prune.timer': ['enabled', 'active']}}, 'system: enabled, active'),
                           ({**prune_gone, 'system': {'old-prune.timer': ['enabled', 'inactive']}},
                            'system: enabled, inactive'),
                           # the timer is off, but the run it started is still deleting
                           ({**OFF, 'old-prune.service': ['static', 'active']}, 'its old-prune.service is active'),
                           ({**OFF, 'system': {'old-prune.timer': ['disabled', 'inactive'],
                                               'prune-job.service': ['static', 'activating']},
                             'starts': {'old-prune.timer': 'prune-job.service'}},
                            'its prune-job.service is activating'),
                           # a timer no scope knows: off cannot be proven (a misspelled name, or another user's)
                           (prune_gone, 'old-prune.timer (not found in the user or the system scope'),
                           ({'fail': True}, 'cannot tell whether')):
            with self.subTest(units=units):
                self.set_units(units)
                code, lines = self.daily(cfg)
                self.assertEqual(code, 2, lines)
                self.assertIn('cannot tell whether' if 'fail' in units else 'while an old timer is on', lines[0])
                self.assertIn(why, lines[0])
                self.assertTrue(os.path.isdir(job))
                self.assertFalse(os.path.lexists(self.path('state', 'daily')))   # nothing written either
                self.assertFalse(os.path.lexists(cfg.log_file))

    def test_run_mode_deletes_once_the_old_timers_are_off(self):
        job = self.old_dir('job-1')
        self.set_units({'old-retention.timer': ['disabled', 'inactive'], 'old-prune.timer': ['masked', 'failed'],
                        'old-prune.service': ['static', 'failed'],
                        'system': {'old-sweep.timer': ['disabled', 'inactive']}})
        cfg = self.daily_cfg('run', ['old-retention.timer', 'old-prune.timer', 'old-sweep.timer'])
        code, lines = self.daily(cfg)
        self.assertEqual(code, 0, lines)
        self.assertFalse(os.path.lexists(job))
        self.assertIn(f'deleted {job}', self.out(f'run-{self.today()}.txt'))
        self.assertIn('freed since', self.out('report.log'))
        self.assertFalse(os.path.lexists(self.path('state', 'daily', f'compare-{self.today()}.txt')))

    def test_an_empty_config_does_nothing(self):
        """#491, state: no rule turned on (an empty section). daily plans nothing, deletes nothing, still reports."""
        keep = self.old_dir('job-1')
        for section in ({}, {'daily': {'mode': 'run'}}):
            with self.subTest(section=section):
                cfg = self.c.config.Config({**section, 'logFile': self.path('state', 'cleaner.log')}, home=self.home)
                lines = []
                code = self.c.daily.daily(cfg, lines.append, facts=self.facts, systemctl=self.systemctl,
                                          report=lambda cfg, say, out=None: 0)
                self.assertEqual(code, 0, lines)
                self.assertTrue(os.path.isdir(keep))
                self.assertIn('total: would delete 0', ''.join(read(os.path.join(cfg.daily_dir, n))
                                                               for n in os.listdir(cfg.daily_dir) if n.endswith('.txt')))
        self.assertTrue(os.path.isdir(os.path.join(self.home, '.local', 'state', 'fork-cleaner', 'daily')))

    def test_the_old_tools_log_and_state_left_behind_are_not_touched(self):
        """#491, state: the old tool's log, its plan file and its lock stay as they are, in compare and in run mode,
        and its deletes are not counted as the cleaner's."""
        state = self.mkdir('home', '.local', 'state')
        old = {name: self.file(os.path.join(state, name), '2026-01-01T00:00:00Z deleted /tmp/x (9.0 GB, tmp: old)\n', days=3)
               for name in ('old-retention.log', 'old-retention.plan', 'old-retention.lock')}
        before = {path: (read(path), os.stat(path).st_mtime) for path in old.values()}
        self.set_units(OFF)
        for mode in ('compare', 'run'):
            code, lines = self.daily(self.daily_cfg(mode, ['old-retention.timer'], logFile='~/.local/state/fork-cleaner/cleaner.log'))
            self.assertEqual(code, 0, lines)
        self.assertEqual({path: (read(path), os.stat(path).st_mtime) for path in old.values()}, before)
        self.assertNotIn('9.0 GB', self.out('report.log'))

    def test_old_dated_files_are_trimmed_and_nothing_else(self):
        out = self.mkdir('state', 'daily')
        names = ['plan-2020-01-01.txt', 'compare-2020-01-01.txt', 'run-2020-01-01.txt', 'notes-2020-01-01.txt',
                 'plan-2020-01-01.txt.bak', 'report.log']
        for name in names:
            self.file(os.path.join(out, name), 'x')
        os.symlink(self.path('home'), os.path.join(out, 'plan-2020-01-02.txt'))
        code, lines = self.daily(self.daily_cfg())
        self.assertEqual(code, 0, lines)
        left = sorted(os.listdir(out))
        self.assertEqual([n for n in names if n not in left], names[:3])
        self.assertNotIn('plan-2020-01-02.txt', left)                       # the link went, never what it names
        self.assertTrue(os.path.isdir(self.path('home')))
        self.assertIn(f'plan-{self.today()}.txt', left)
        code, lines = self.daily(self.daily_cfg())                          # again the same day: a second file
        self.assertEqual(code, 0, lines)
        self.assertIn(f'plan-{self.today()}.2.txt', os.listdir(out))

    def test_a_second_run_the_same_day_never_writes_over_the_first(self):
        first = self.old_dir('job-1')
        self.set_units(OFF)
        cfg = self.daily_cfg('run', ['old-retention.timer'])
        self.assertEqual(self.daily(cfg)[0], 0)
        run = self.path('state', 'daily', f'run-{self.today()}.txt')
        before = read(run)
        self.assertIn(f'deleted {first}', before)
        second = self.old_dir('job-2')
        code, lines = self.daily(cfg)
        self.assertEqual(code, 0, lines)
        self.assertEqual(read(run), before)                                 # what the first run deleted stays on record
        self.assertIn(f'deleted {second}', self.out(f'run-{self.today()}.2.txt'))
        self.assertIn(f'run-{self.today()}.2.txt', lines[-1])
        self.assertFalse(any(n.startswith('.') for n in os.listdir(self.path('state', 'daily'))))   # no temp file left
        code, lines = self.daily(cfg)
        self.assertEqual(code, 0, lines)
        self.assertEqual(read(run), before)
        self.assertTrue(os.path.isfile(self.path('state', 'daily', f'run-{self.today()}.3.txt')))

    def test_new_only_is_exit_3_and_a_failure_hides_it(self):
        new = self.old_dir('job-1')
        cfg = self.daily_cfg(sources=[self.source()])
        code, lines = self.daily(cfg)
        self.assertEqual(code, 3, lines)
        self.assertTrue(lines[-1].endswith('exit 3: done, the compare found new only paths'), lines)
        self.assertTrue(os.path.isdir(new))
        out = self.path('state', 'daily')
        self.file(os.path.join(out, 'plan-2020-01-01.txt'), 'x')
        os.chmod(out, 0o500)                                                # nothing can be written or trimmed
        try:
            code, lines = self.daily(cfg)
        finally:
            os.chmod(out, 0o700)
        self.assertEqual(code, 1, lines)
        self.assertTrue(lines[-1].endswith('exit 1: something failed'), lines)
        self.set_old([], code=1)                                            # an old tool fails: incomplete, not new only
        code, lines = self.daily(cfg)
        self.assertEqual(code, 1, lines)

    def test_an_orphaned_worktree_in_the_temp_folder_goes_by_age_and_is_no_error(self):
        """A worktree whose main repository was removed holds no commit (#519): the tmp rule deletes it by its
        pattern's age, and the daily run is not marked failed for it."""
        tree = self.old_dir('agent-tree')
        self.file(os.path.join(tree, '.git'), f'gitdir: {self.path("gone-repo", ".git", "worktrees", "agent-tree")}\n')
        self.age(tree, 30)
        job = self.old_dir('job-1')
        self.set_units(OFF)
        code, lines = self.daily(self.daily_cfg('run', ['old-retention.timer']))
        self.assertEqual(code, 0, lines)
        self.assertFalse(os.path.lexists(tree))
        self.assertFalse(os.path.lexists(job))
        text = self.out(f'run-{self.today()}.txt')
        self.assertIn(f'deleted {tree} (', text)
        self.assertIn('orphaned worktree: its git folder', text)

    def test_the_out_folder_is_never_a_must_keep_path_a_root_or_the_home(self):
        for out in ('~/.claude/daily', '~/notes/repair-backups/x', self.tmp + '/daily', self.tmp, '~', '/'):
            with self.subTest(out=out), self.assertRaises(machine_file.MachineFileError):
                self.tmp_cfg(daily={'outDir': out})
        # The home folder or one above it: the must-keep paths in the home refuse it too, so they are put aside
        # here to show this check refuses it on its own.
        with mock.patch.object(self.c.config, 'must_keep_match', lambda patterns, path: None), \
                mock.patch.object(self.c.config, 'MUST_KEEP_NAMES', ()):
            for out in (self.home, os.path.dirname(self.home), '/'):
                with self.subTest(out=out), self.assertRaises(machine_file.MachineFileError):
                    self.c.config.Config({'daily': {'outDir': out}, 'logFile': self.path('state', 'cleaner.log')},
                                         home=self.home)
        self.c.config.Config({'daily': {'outDir': '~/.local/state/x'}, 'logFile': self.path('state', 'cleaner.log')},
                             home=self.home)
        for bad in ({'mode': 'delete'}, {'oldTimers': ['old.service']}, {'oldTimers': ['../x.timer']},
                    {'keepDays': 0}, {'what': 1}):
            with self.subTest(bad=bad), self.assertRaises(machine_file.MachineFileError):
                self.tmp_cfg(daily=bad)


class Compare(Case):
    def test_new_only_is_a_bug_until_explained(self):
        both, new = self.old_dir('job-1'), self.old_dir('job-2')
        fresh, busy = self.old_dir('fresh', days=1), self.old_dir('busy')
        self.busy.add(busy)
        self.set_old([f'2026-01-01T00:00:00Z would delete {both} (1.0 MB, tmp: old)',
                      f'2026-01-01T00:00:00Z would delete {fresh} (1.0 MB, tmp: old)',
                      f'2026-01-01T00:00:00Z would delete {busy} (1.0 MB, tmp: old)',
                      '2026-01-01T00:00:00Z would delete 3 Gradle cache entries in /x (2.0 MB, gradle: cap)',
                      f'2026-01-01T00:00:00Z kept {new} (referenced)'])
        cfg = self.daily_cfg(sources=[self.source()])
        lines = []
        code = self.c.compare.compare(cfg, self.facts(cfg), lines.append)
        self.assertEqual(code, 3, lines)
        text = '\n'.join(lines)
        self.assertIn('new only (a bug until explained): 1', text)
        self.assertIn(f'  {new} (tmp: ', text)
        self.assertIn('old only (fork-cleaner keeps it): 2', text)
        self.assertIn(f'  {busy} [old-retention] (tmp: in use', text)
        self.assertIn(f'  {fresh} [old-retention] (no kept line: no rule of fork-cleaner takes it, or it is too new for one)', text)
        self.assertIn('both: 1', text)
        self.assertTrue(all(os.path.isdir(p) for p in (both, new, fresh, busy)))    # a compare deletes nothing

    def test_what_fork_cleaner_deletes_inside_an_old_folder_is_both(self):
        job = self.old_dir('job-1')
        self.set_old([f'x would delete {self.tmp} (1.0 MB, tmp: old)'])      # the old tool deletes all of it
        cfg = self.daily_cfg(sources=[self.source()])
        lines = []
        self.assertEqual(self.c.compare.compare(cfg, self.facts(cfg), lines.append), 0, lines)
        self.assertIn('both: 1', lines)
        self.assertIn('new only (a bug until explained): 0', lines)
        self.assertIn(f'  {self.tmp} [old-retention] (deletes more: fork-cleaner only {job} in it)', lines)

    def test_a_folder_holding_what_an_old_tool_deletes_is_new_only(self):
        """The old tool deletes /x/data.bin, fork-cleaner all of /x: the rest of /x is deleted only by the cleaner."""
        job = self.old_dir('job-1')
        self.set_old([f'x would delete {job}/data.bin (1.0 MB, tmp: old)'])
        cfg = self.daily_cfg(sources=[self.source()])
        lines = []
        self.assertEqual(self.c.compare.compare(cfg, self.facts(cfg), lines.append), 3, lines)
        self.assertIn('new only (a bug until explained): 1', lines)
        self.assertIn(f'  {job} (tmp: not changed for 30 days (*: 7 days); deletes more: the old tools only {job}/data.bin in it)', lines)
        self.assertIn('both: 0', lines)
        self.assertIn('old only (fork-cleaner keeps it): 0', lines)

    def test_a_source_that_fails_makes_it_incomplete(self):
        job = self.old_dir('job-1')
        self.set_old([f'x would delete {job} (1.0 MB, tmp: old)'], code=1)
        broken = {'name': 'missing', 'command': [self.path('no-such-tool')], 'pattern': OLD_PATTERN}
        cfg = self.daily_cfg(sources=[self.source(), broken])
        lines = []
        self.assertEqual(self.c.compare.compare(cfg, self.facts(cfg), lines.append), 1, lines)
        self.assertIn('incomplete: old-retention: exit 1', lines)
        self.assertTrue(any(line.startswith('incomplete: missing: could not run') for line in lines), lines)
        self.assertIn('both: 1', lines)                                     # what it printed still counts

    def test_bad_sources_are_refused(self):
        good = self.source()
        for bad in ({**good, 'command': ['relative/tool']}, {**good, 'command': []}, {**good, 'pattern': '(a)(b)'},
                    {**good, 'pattern': 'no group'}, {**good, 'pattern': '('}, {**good, 'name': '../x'},
                    {'name': 'x', 'command': [self.old]}):
            with self.subTest(bad=bad), self.assertRaises(machine_file.MachineFileError):
                self.daily_cfg(sources=[bad])
        with self.assertRaises(machine_file.MachineFileError):
            self.daily_cfg(sources=[good, good])


class Command(Case):
    def test_the_example_machine_file_section_loads(self):
        with open(os.path.join(fx.COMMON, 'machine.example.json')) as f:
            section = json.load(f)['cleaner']
        self.assertEqual(section['tmp']['root'], '/tmp')
        section['tmp']['root'] = self.tmp           # the test's home is inside /tmp, which no root may hold
        cfg = self.c.config.Config(section, home=self.home)
        self.assertEqual(cfg.daily_mode, 'compare')
        self.assertEqual([s.name for s in cfg.sources], ['example-retention'])
        self.assertEqual(cfg.sources[0].pattern.search('would remove /x/y (a, b)').group(1), '/x/y')

    def test_the_commands_read_the_machine_file(self):
        path = self.path('machine.json')
        self.old_dir('job-1')
        with open(path, 'w') as f:
            json.dump({'version': 1, 'cleaner': {**self.section, 'tmp': {'root': self.tmp, 'patterns': {'*': 7}},
                                                 'daily': {'outDir': self.path('state', 'daily'),
                                                           'compare': [self.source()]}}}, f)
        lines = []
        with mock.patch.object(self.c.config, 'MACHINE_FILE', path), \
                mock.patch.object(self.c.config, 'MACHINE_OWNER', os.getuid()), \
                mock.patch.object(self.c.facts, 'UNIT_DIRS', (self.mkdir('units'),)):
            self.assertEqual(self.c.engine.main(['compare'], say=lines.append), 3)       # job-1: new only
            self.assertIn('new only (a bug until explained): 1', lines)
            self.assertEqual(self.c.engine.main(['daily', 'now'], say=lines.append), 2)
        missing = self.path('no-machine.json')
        with mock.patch.object(self.c.config, 'MACHINE_FILE', missing):
            for command in (['daily'], ['compare']):
                self.assertEqual(self.c.engine.main(command, say=lines.append), 2)       # no machine file: nothing done
        self.assertTrue(os.path.isdir(self.path('tmp', 'job-1')))


MUTATIONS = {
    'daily: run refuses while an old timer is on': ('daily', '        if on:\n            say(f\'fork-cleaner daily: refuses',
                                                    '        if False:\n            say(f\'fork-cleaner daily: refuses',
                                                    Daily, 'test_run_mode_refuses_while_an_old_timer_is_on'),
    'daily: enabled alone is on': ('daily', 'if enabled not in OFF_ENABLED or active not in OFF_ACTIVE:',
                                   'if active not in OFF_ACTIVE:', Daily, 'test_run_mode_refuses_while_an_old_timer_is_on'),
    'daily: active alone is on': ('daily', 'if enabled not in OFF_ENABLED or active not in OFF_ACTIVE:',
                                  'if enabled not in OFF_ENABLED:', Daily, 'test_run_mode_refuses_while_an_old_timer_is_on'),
    'daily: systemd that cannot answer': ('daily', "    if not answer or '\\n' in answer:\n        raise",
                                          "    if False:\n        raise", Daily, 'test_run_mode_refuses_while_an_old_timer_is_on'),
    'daily: a timer no scope knows is on': ('daily', '        if not states:\n', '        if False:\n', Daily,
                                            'test_run_mode_refuses_while_an_old_timer_is_on'),
    'daily: the system scope is asked': ('daily', "SCOPES = ('--user', '--system')", "SCOPES = ('--user',)", Daily,
                                         'test_run_mode_refuses_while_an_old_timer_is_on'),
    'daily: a running service is on': ('daily', '            elif running not in OFF_ACTIVE:', '            elif False:',
                                       Daily, 'test_run_mode_refuses_while_an_old_timer_is_on'),
    'daily: compare mode never deletes': ('daily', "engine.execute(cfg, facts(cfg), mode == 'run', say=lines.append)",
                                          'engine.execute(cfg, facts(cfg), True, say=lines.append)', Daily,
                                          'test_compare_mode_plans_reports_and_compares_and_deletes_nothing'),
    'daily: trims only its own names': ('daily', r"DATED = re.compile(r'^(?:plan|run|compare)-(\d{4}-\d\d-\d\d)(?:\.\d+)?\.txt$')",
                                        r"DATED = re.compile(r'^\w+-(\d{4}-\d\d-\d\d)')", Daily,
                                        'test_old_dated_files_are_trimmed_and_nothing_else'),
    'daily: never over a file of the day': ('daily', 'os.link(new, name)', 'os.replace(new, name)', Daily,
                                            'test_a_second_run_the_same_day_never_writes_over_the_first'),
    'daily: new only has its own code': ('daily', 'else NEW_ONLY if NEW_ONLY in codes else 0', 'else 0', Daily,
                                         'test_new_only_is_exit_3_and_a_failure_hides_it'),
    'daily: a failure hides new only': ('daily', 'final = 1 if any(c not in (0, NEW_ONLY) for c in codes) else',
                                        'final = 1 if NEW_ONLY not in codes and any(c not in (0, NEW_ONLY) for c in codes) else',
                                        Daily, 'test_new_only_is_exit_3_and_a_failure_hides_it'),
    'daily: an orphaned worktree is its own state': ('guard', 'gone = None if bare else orphaned_worktree(where)',
                                                     'gone = None', Daily,
                                                     'test_an_orphaned_worktree_in_the_temp_folder_goes_by_age_and_is_no_error'),
    'daily: the out folder not in a root': ('config', "('daily.outDir', daily['outDir'], self.daily_dir)", '', Daily,
                                            'test_the_out_folder_is_never_a_must_keep_path_a_root_or_the_home'),
    'daily: the out folder is not the home or above it': ('config', '        if _holds(self.daily_dir, self.home):',
                                                          '        if False:', Daily,
                                                          'test_the_out_folder_is_never_a_must_keep_path_a_root_or_the_home'),
    'compare: new only has its own code': ('compare', 'else NEW_ONLY if new_only else 0', 'else 0', Compare,
                                           'test_new_only_is_a_bug_until_explained'),
    'compare: a failed source is incomplete': ('compare', 'if done.returncode != 0:', 'if False:', Compare,
                                               'test_a_source_that_fails_makes_it_incomplete'),
    'compare: inside an old folder counts as the same': ('compare', "return path == folder or path.startswith(folder.rstrip('/') + '/')",
                                                         'return path == folder', Compare,
                                                         'test_what_fork_cleaner_deletes_inside_an_old_folder_is_both'),
    'compare: holding an old path is new only': ('compare', 'if _within(path, other)), None)',
                                                 'if _within(path, other) or _within(other, path)), None)', Compare,
                                                 'test_a_folder_holding_what_an_old_tool_deletes_is_new_only'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
