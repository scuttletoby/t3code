"""The `tmp` and `docker` rules, the config, and `plan` / `run` themselves (lib/cleaner/rules.py, config.py,
engine.py). Folders live in the test's own temporary folder; Docker is a stub command. RedCheck breaks each rule
in a copy of the code and shows its test then fails.

  python3 -I -B fork-kit/cleaner/tests/engine.test.py
"""
import fcntl, json, os, re, subprocess, sys, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cleaner_fixture as fx  # noqa: E402

import machine_file  # noqa: E402


class Case(fx.Case):
    c = fx.load()

    def listing(self):
        return sorted(os.path.join(root, n) for root, dirs, files in os.walk(self.dir) for n in dirs + files)

    def log(self, cfg):
        with open(cfg.log_file) as f:
            return f.read().splitlines()


class Tmp(Case):
    def test_no_patterns_means_nothing_is_looked_at(self):
        self.old_dir('crash-test-1', 400)
        read = AssertionError('the temp folder was read')       # the first config names the real temp folder
        with mock.patch.object(self.c.rules, '_entries', side_effect=read):
            for cfg in (self.cfg(), self.cfg(tmp={'root': self.tmp}), self.cfg(tmp={'root': self.tmp, 'patterns': {}})):
                plan = self.plan(cfg)
                self.assertEqual((plan.items, plan.kept), ([], []))
                self.assertEqual(self.run_cleaner(cfg)[0], 0)
        self.assertTrue(os.path.isdir(self.path('tmp', 'crash-test-1')))

    def test_a_pattern_gives_the_days(self):
        cfg = self.tmp_cfg({'crash-test-*': 1, 'build-?': 7})
        gone = [self.old_dir('crash-test-1', 2), self.old_dir('build-a', 8),
                self.file(self.path('tmp', 'crash-test-log'), days=2)]
        stay = [self.old_dir('crash-test-2', 0.5), self.old_dir('build-b', 6), self.old_dir('other', 400),
                self.old_dir('build-long', 400)]
        plan = self.plan(cfg)
        for path in gone:
            self.assertDeletes(plan, path, 'tmp')
        for path in stay:
            self.assertNotCandidate(plan, path)
        self.assertIn('crash-test-*: 1 day', self.assertDeletes(plan, gone[0]).why)
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 0, lines)
        self.assertEqual([p for p in gone + stay if os.path.lexists(p)], stay)
        code, lines = self.run_cleaner(cfg)                      # runs again: nothing left to do
        self.assertEqual((code, [line for line in lines if line.startswith('deleted ')]), (0, []))

    def test_the_first_pattern_that_matches_counts(self):
        early, late = self.old_dir('keep-longer-1', 10), self.old_dir('job-1', 10)
        plan = self.plan(self.tmp_cfg({'keep-longer-*': 30, '*': 7}))
        self.assertNotCandidate(plan, early)
        self.assertDeletes(plan, late)

    def test_the_newest_change_inside_counts(self):
        d = self.old_dir('job-1', 30)
        self.assertDeletes(self.plan(self.tmp_cfg()), d)
        deep = self.file(os.path.join(d, 'a', 'b', 'still-written.log'))
        self.age(d, 30)
        self.age(deep, 2)
        self.assertNotCandidate(self.plan(self.tmp_cfg()), d)

    def test_a_folder_copied_today_with_old_dates_is_not_old(self):
        d = self.old_dir('job-1', 30)                            # the dates say 30 days; the files were written now
        self.assertDeletes(self.plan(self.tmp_cfg()), d)
        with mock.patch.object(self.c.guard, 'changed', self.real_changed):
            self.assertNotCandidate(self.plan(self.tmp_cfg()), d)

    def test_names_it_never_takes(self):
        names = ('.hidden', '.X11-unix', 'claude-1000', 'tmux-1000', 'ssh-XXXXabcd', 'systemd-private-0a1b-x.service-y',
                 'snap-private-tmp')
        kept = [self.old_dir(name, 400) for name in names]
        other = self.old_dir('job-1', 400)
        plan = self.plan(self.tmp_cfg({'*': 1, '.*': 1, 'claude-*': 1}))
        for path in kept:
            self.assertNotCandidate(plan, path)
        self.assertDeletes(plan, other)

    def test_the_hard_refusals_hold_here_too(self):
        cfg = self.tmp_cfg()
        held = self.old_dir('job-held')
        self.age(self.file(os.path.join(held, 'KEEP'), 'bench data\n'), 30)
        self.age(held, 30)
        link = self.path('tmp', 'job-link')
        target = self.old_dir('job-target', 1)
        os.symlink(target, link)
        os.utime(link, (self.now - 30 * fx.DAY,) * 2, follow_symlinks=False)
        used = self.old_dir('job-used')
        self.busy.update(self.c.facts.ancestors(used))
        plan = self.plan(cfg)
        self.assertKept(plan, held, 'KEEP file', 'bench data')
        self.assertKept(plan, link, 'symbolic link')
        self.assertKept(plan, used, 'in use')
        self.assertEqual(self.run_cleaner(cfg)[0], 0)
        self.assertTrue(os.path.isdir(held) and os.path.islink(link) and os.path.isdir(used))
        self.assertTrue(os.path.isfile(os.path.join(target, 'data.bin')))

    def test_everything_is_read_again_just_before_the_delete(self):
        cfg = self.tmp_cfg()
        d, other = self.old_dir('job-1'), self.old_dir('job-2')
        facts = self.facts(cfg)
        plan = self.c.rules.plan(cfg, facts)
        self.assertEqual(len(plan.items), 2)
        self.file(os.path.join(d, 'written-after-the-plan.txt'))               # no longer old
        self.busy.update(self.c.facts.ancestors(other))                        # a process moved in
        lines = []
        with mock.patch.object(self.c.facts, 'PROCESS_SECONDS', 0):
            self.assertEqual(self.c.engine.delete_all(cfg, facts, plan.items, True, lines.append), 0)
        self.assertEqual(sorted(line.split()[0] for line in lines), ['REFUSED', 'REFUSED'], lines)
        self.assertTrue(os.path.isdir(d) and os.path.isdir(other))

    def test_bad_patterns_are_refused(self):
        for patterns in ({'job-*': 0}, {'job-*': -1}, {'job-*': 1.5}, {'job-*': '7'}, {'a/b': 7}, {'': 7}, {'..': 7}, {'.': 7}):
            with self.subTest(patterns=patterns), self.assertRaises(machine_file.MachineFileError):
                self.cfg(tmp={'root': self.tmp, 'patterns': patterns})
        for root in ('/', 'tmp', self.home, os.path.dirname(self.home), self.tmp + '/../tmp'):
            with self.subTest(root=root), self.assertRaises(machine_file.MachineFileError):
                self.cfg(tmp={'root': root, 'patterns': {'*': 7}})


class Docker(Case):
    def docker(self, volumes, **state):
        exe, self.state = self.stub('docker', fx.STUB_DOCKER, {'volumes': volumes, **state})
        patch = mock.patch.object(self.c.facts, 'DOCKER', exe)
        patch.start()
        self.addCleanup(patch.stop)

    def volume(self, days=30, **more):
        return {'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.now - days * fx.DAY)), **more}

    def volumes(self):
        with open(self.state) as f:
            return sorted(json.load(f)['volumes'])

    def on(self, **docker):
        return self.cfg(docker={'enabled': True, **docker})

    def planned(self, cfg=None):
        return self.plan(cfg or self.on(), dangling=self.c.facts.docker_dangling)

    def ran(self, cfg=None):
        return self.run_cleaner(cfg or self.on(), dangling=self.c.facts.docker_dangling)

    def test_it_is_off_unless_the_config_turns_it_on(self):
        self.docker({fx.HEX: self.volume(400)})
        for cfg in (self.cfg(), self.cfg(docker={}), self.cfg(docker={'enabled': False, 'days': 30})):
            plan = self.planned(cfg)
            self.assertEqual((plan.items, plan.kept), ([], []))
            self.assertEqual(self.ran(cfg)[0], 0)
        self.assertEqual(self.calls(self.state), [])                         # Docker is not even asked
        self.assertEqual(self.volumes(), [fx.HEX])

    def test_an_old_unused_anonymous_volume_is_removed(self):
        self.docker({fx.HEX: self.volume(8)})
        item = self.assertDeletes(self.planned(), fx.HEX, 'docker')
        self.assertEqual(item.kind, 'volume')
        code, lines = self.ran()
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.volumes(), [])
        self.assertEqual(len([line for line in lines if line.startswith(f'deleted docker volume {fx.HEX} ')]), 1, lines)
        self.assertEqual(self.ran()[0], 0)                                    # runs again: nothing left to do

    def test_a_named_volume_is_never_removed(self):
        names = ('pgdata', 'app_db-data', fx.HEX[:63], fx.HEX + 'a', fx.HEX.upper(), 'g' + fx.HEX[1:])
        self.docker({name: self.volume(400) for name in names})
        plan = self.planned()
        self.assertEqual((plan.items, plan.kept), ([], []))
        self.assertEqual(self.ran()[0], 0)
        self.assertEqual(self.volumes(), sorted(names))

    def test_a_volume_any_container_uses_is_never_removed(self):
        self.docker({fx.HEX: self.volume(400, used=True)})                   # a stopped container counts
        self.assertEqual(self.planned().items, [])
        self.assertEqual(self.ran()[0], 0)
        self.assertEqual(self.volumes(), [fx.HEX])
        self.assertIn(['volume', 'ls', '-q', '--filter', 'dangling=true'], self.calls(self.state))

    def test_a_volume_under_seven_days_old_is_kept(self):
        young = fx.HEX[::-1]
        self.docker({fx.HEX: self.volume(7.1), young: self.volume(6.9)})
        plan = self.planned()
        self.assertDeletes(plan, fx.HEX)
        self.assertKept(plan, young, 'under 7 days old')
        self.assertKept(self.planned(self.on(days=30)), fx.HEX, 'under 30 days old')
        with self.assertRaises(machine_file.MachineFileError):
            self.on(days=6)

    def test_docker_that_cannot_answer_means_keep(self):
        for state in ({'fail': True}, {}):
            self.docker({fx.HEX: self.volume(400) if state else {'created': 'long ago'}}, **state)
            self.assertKept(self.planned(), 'docker', 'could not read', 'Docker')
            code, lines = self.ran()
            self.assertEqual(code, 1, lines)
            self.assertEqual(self.volumes(), [fx.HEX])
        with mock.patch.object(self.c.facts, 'DOCKER', self.path('no-such-docker')):
            self.assertKept(self.planned(), 'docker', 'could not read', 'Docker')

    def test_a_process_in_the_volume_keeps_it(self):
        mount = self.mkdir('volumes', fx.HEX, '_data')
        self.docker({fx.HEX: self.volume(400, mountpoint=mount)})
        self.busy.update(self.c.facts.ancestors(os.path.join(mount, 'db')))
        self.assertKept(self.planned(), fx.HEX, 'in use')

    def test_docker_is_asked_again_just_before_the_delete(self):
        self.docker({fx.HEX: self.volume(400)})
        cfg = self.on()
        facts = self.facts(cfg, dangling=self.c.facts.docker_dangling)
        plan = self.c.rules.plan(cfg, facts)
        self.assertDeletes(plan, fx.HEX)
        self.stub('docker', fx.STUB_DOCKER, {'volumes': {fx.HEX: self.volume(400, used=True)}})   # a container took it
        lines = []
        self.assertEqual(self.c.engine.delete_all(cfg, facts, plan.items, True, lines.append), 0)
        self.assertEqual([line.split()[0] for line in lines], ['REFUSED'], lines)
        self.assertNotIn('rm', [call[1] for call in self.calls(self.state)])
        with mock.patch.object(self.c.rules, 'recheck', lambda *a: None):     # and Docker itself refuses as well
            self.assertEqual(self.c.engine.delete_all(cfg, facts, plan.items, True, lines.append), 1)
        self.assertEqual(lines[-1].split()[0], 'FAILED', lines)
        self.assertEqual(self.calls(self.state)[-1], ['volume', 'rm', fx.HEX])       # never with --force
        self.assertEqual(self.volumes(), [fx.HEX])


class Engine(Case):
    def jobs(self):
        """Three old folders the plan takes, one young one, one with a KEEP file."""
        cfg = self.tmp_cfg({'job-*': 7})
        old = [self.old_dir(f'job-{i}') for i in range(3)]
        young = self.old_dir('job-young', 1)
        held = self.mkdir('tmp', 'job-held')
        self.file(os.path.join(held, 'KEEP'), 'needed for the report\n')
        self.age(held, 30)
        return cfg, old, young, held

    def test_plan_changes_nothing_and_writes_no_log(self):
        cfg, old, young, held = self.jobs()
        before = self.listing()
        code, lines = self.run_cleaner(cfg, really=False)
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.listing(), before)                             # not even a log file or its folder
        self.assertEqual(sorted(line.split()[2] for line in lines if line.startswith('would delete ')), old)
        self.assertEqual([line for line in lines if line.startswith('deleted ')], [])
        self.assertIn(f'kept {held} (tmp: KEEP file: needed for the report)', lines)
        self.assertRegex(lines[-1], r'^total: would delete 3 \(\d+(\.\d)? [KMG]?B\), kept 1$')
        self.assertEqual([line for line in lines if young in line], [])

    def test_run_writes_one_log_line_for_each_delete(self):
        cfg, old, young, held = self.jobs()
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 0, lines)
        self.assertEqual([p for p in old + [young, held] if os.path.lexists(p)], [young, held])
        log = self.log(cfg)
        deleted = sorted(line for line in log if 'Z deleted ' in line)
        self.assertEqual(len(deleted), 3, log)
        for path, line in zip(old, deleted):
            self.assertRegex(line, r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ deleted ' + re.escape(path)
                             + r' \(\d+(\.\d)? [KMG]?B, tmp: not changed for 30 days \(job-\*: 7 days\)\)$')
        self.assertRegex(lines[-1], r'^total: deleted 3 \(\d+(\.\d)? [KMG]?B\), kept 1, failed 0$')
        self.assertIn(lines[-1], log[-1])
        self.assertEqual(os.stat(cfg.log_file).st_mode & 0o777, 0o600)
        self.assertEqual(self.run_cleaner(cfg)[0], 0)                        # runs again: no new delete line
        self.assertEqual(len([line for line in self.log(cfg) if 'Z deleted ' in line]), 3)

    def test_the_log_keeps_its_newest_lines(self):
        cfg = self.tmp_cfg({'job-*': 7}, logLines=10)
        for i in range(25):
            self.old_dir(f'job-{i:02}')
        self.assertEqual(self.run_cleaner(cfg)[0], 0)
        log = self.log(cfg)
        self.assertEqual(len(log), 10)
        self.assertIn('total: deleted 25', log[-1])
        self.assertIn(self.path('tmp', 'job-24'), log[-2])
        with self.assertRaises(machine_file.MachineFileError):
            self.cfg(logLines=9)

    @unittest.skipIf(os.getuid() == 0, 'root deletes from a read-only folder')
    def test_a_delete_that_fails_is_reported_and_the_run_goes_on(self):
        cfg = self.tmp_cfg({'job-*': 7})
        stuck, fine = self.old_dir('job-1'), self.old_dir('job-2')
        fx.read_only(stuck)
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 1, lines)
        self.assertTrue(os.path.isfile(os.path.join(stuck, 'data.bin')))
        self.assertFalse(os.path.exists(fine))
        self.assertEqual(len([line for line in self.log(cfg) if f' FAILED {stuck} (tmp: ' in line]), 1)
        self.assertRegex(lines[-1], r'^total: deleted 1 \(.*\), kept 0, failed 1$')

    def test_what_it_could_not_read_makes_the_run_fail(self):
        cfg, old, young, held = self.jobs()

        def no_list():
            raise self.c.facts.Unreadable('/proc: Permission denied')
        code, lines = self.run_cleaner(cfg, processes=no_list)
        self.assertEqual(code, 1, lines)
        self.assertEqual([p for p in old if not os.path.isdir(p)], [])
        self.assertIn(f'kept {old[0]} (tmp: could not read: the process list (/proc: Permission denied))', lines)

    def test_two_runs_at_once(self):
        cfg, old, young, held = self.jobs()
        os.makedirs(os.path.dirname(cfg.lock_file))
        with open(cfg.lock_file, 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            code, lines = self.run_cleaner(cfg)
            self.assertEqual(code, 1, lines)
            self.assertIn('another run', lines[-1])
            self.assertEqual([p for p in old if not os.path.isdir(p)], [])
            self.assertEqual(self.run_cleaner(cfg, really=False)[0], 0)          # a plan needs no lock
        self.assertEqual(self.run_cleaner(cfg)[0], 0)
        self.assertEqual([p for p in old if os.path.isdir(p)], [])

    def test_no_log_file_means_no_delete(self):
        cfg, old, young, held = self.jobs()
        os.makedirs(os.path.dirname(cfg.log_file))
        elsewhere = self.file(self.path('elsewhere.txt'), 'not a log\n')
        os.symlink(elsewhere, cfg.log_file)
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 1, lines)
        self.assertIn('log', lines[-1])
        self.assertEqual([p for p in old if not os.path.isdir(p)], [])
        with open(elsewhere) as f:
            self.assertEqual(f.read(), 'not a log\n')

    def test_the_totals_name_each_reason(self):
        cfg, old, young, held = self.jobs()
        self.busy.update(self.c.facts.ancestors(old[0]))
        code, lines = self.run_cleaner(cfg, really=False)
        self.assertRegex('\n'.join(lines), r'(?m)^  tmp: would delete 2 paths \(.*\)$')
        self.assertRegex('\n'.join(lines), r'(?m)^  tmp: kept 1 \(.*\): KEEP file$')
        self.assertRegex('\n'.join(lines), r'(?m)^  tmp: kept 1 \(.*\): in use$')


class Config(Case):
    def test_the_example_in_the_readme_is_a_config(self):
        with open(os.path.join(fx.KIT, 'README.md')) as f:
            example = re.search(r'```json\n(.*?)```', f.read(), re.S).group(1)
        data = json.loads(example)
        self.assertEqual(data['version'], 1)
        cfg = self.c.config.Config(data[self.c.config.KIT], home='/srv/example-home')    # the test's home is in /tmp
        self.assertTrue(cfg.docker_enabled)
        self.assertEqual([repo.github for repo in cfg.repos], ['example-owner/example-repo'])
        self.assertTrue(cfg.tmp_patterns)
        self.assertNotIn('/home/', example)

    def test_an_empty_section_does_nothing(self):
        cfg = self.c.config.Config({}, home=self.home)
        self.assertEqual(cfg.roots(), frozenset())
        self.assertEqual(cfg.log_file, os.path.join(self.home, '.local', 'state', 'fork-cleaner', 'cleaner.log'))
        self.old_dir('job-1', 400)
        self.finished()
        cfg = self.cfg()
        code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, lines[-1]), (0, 'total: deleted 0 (0 B), kept 0, failed 0'))

    def test_unknown_keys_and_wrong_values_are_refused(self):
        repo = {'root': self.trees, 'github': fx.REPO}
        for section in ({'rules': []}, {'tmp': {'days': 7}}, {'docker': {'named': True}}, {'docker': {'enabled': 'yes'}},
                        {'worktrees': {'repos': [{**repo, 'force': True}]}}, {'worktrees': {'repos': [{'root': self.trees}]}},
                        {'worktrees': {'repos': [{**repo, 'github': 'https://github.com/acme/widget'}]}},
                        {'worktrees': {'repos': [{**repo, 'root': 'trees'}]}}, {'worktrees': {'repos': [{**repo, 'root': '/'}]}},
                        {'worktrees': {'repos': [{**repo, 'regenerable': ['a/b']}]}}, {'worktrees': {'repos': ['x']}},
                        {'mustKeep': ['relative/path']}, {'mustKeep': ['~']}, {'mustKeep': [7]}, {'mustKeep': ['/a/../b']},
                        {'logFile': 'cleaner.log'}):
            with self.subTest(section=section), self.assertRaises(machine_file.MachineFileError):
                self.cfg(**section)

    def machine(self, section):
        """A machine file of the test's own, and no systemd units but the test's."""
        path = self.path('machine.json')
        with open(path, 'w') as f:
            json.dump({'version': 1, **section}, f)
        for module, name, value in ((self.c.config, 'MACHINE_FILE', path), (self.c.config, 'MACHINE_OWNER', os.getuid()),
                                    (self.c.facts, 'UNIT_DIRS', (self.mkdir('units'),))):
            patch = mock.patch.object(module, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def main(self, *argv):
        lines = []
        return self.c.engine.main(list(argv), say=lines.append), lines

    def test_the_command_reads_the_machine_file(self):
        d = self.old_dir('job-1')
        self.machine({'cleaner': {**self.section, 'tmp': {'root': self.tmp, 'patterns': {'job-*': 7}}}})
        code, lines = self.main('plan')
        self.assertEqual(code, 0, lines)
        self.assertTrue(os.path.isdir(d))
        self.assertIn('total: would delete 1', lines[-1])
        with mock.patch.object(self.c.guard, 'changed', self.real_changed):       # written a moment ago: it stays
            self.assertEqual(self.main('run')[0], 0)
            self.assertTrue(os.path.isdir(d))
        code, lines = self.main('run')
        self.assertEqual(code, 0, lines)
        self.assertFalse(os.path.exists(d))

    def test_no_usable_machine_file_means_nothing_is_done(self):
        d = self.old_dir('job-1')
        for section in ({}, {'cleaner': {'tmp': {'root': self.tmp, 'patterns': {'job-*': 0}}}}, {'cleaner': []}):
            self.machine(section)
            for command in ('plan', 'run'):
                code, lines = self.main(command)
                self.assertEqual(code, 2, lines)
                self.assertIn('machine.json', lines[-1])
        with mock.patch.object(self.c.config, 'MACHINE_FILE', self.path('no-such-file.json')):
            self.assertEqual(self.main('run')[0], 2)
        self.assertTrue(os.path.isdir(d))

    def test_the_command_takes_plan_or_run_and_nothing_else(self):
        self.machine({'cleaner': dict(self.section)})
        for argv in ((), ('delete',), ('run', '--force'), ('plan', '--config', self.path('machine.json')), ('plan', 'run')):
            code, lines = self.main(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn('usage', lines[-1])
        self.assertEqual(self.main('--help')[0], 0)

    def test_the_launcher_runs(self):
        exe = os.path.join(fx.KIT, 'bin', 'fork-cleaner')
        self.assertTrue(os.access(exe, os.X_OK))
        with open(exe) as f:
            self.assertEqual(f.readline(), '#!/usr/bin/python3 -IB\n')
        done = subprocess.run([sys.executable, '-I', '-B', exe, '--help'], capture_output=True, text=True, cwd=self.dir)
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertIn('fork-cleaner plan', done.stdout)


MUTATIONS = {
    'tmp: no patterns, nothing': ('rules', 'if not cfg.tmp_patterns:', 'if False:', Tmp, 'test_no_patterns_means_nothing_is_looked_at'),
    'tmp: the days of the pattern': ('rules', 'if age < days * DAY:', 'if False:', Tmp, 'test_a_pattern_gives_the_days'),
    'tmp: only names a pattern matches': ('rules', 'match = None', "match = ('*', 1)", Tmp, 'test_a_pattern_gives_the_days'),
    'tmp: the first pattern': ('rules', 'for pattern, days in cfg.tmp_patterns:', 'for pattern, days in reversed(cfg.tmp_patterns):',
                               Tmp, 'test_the_first_pattern_that_matches_counts'),
    'tmp: the newest change inside': ('guard', 'look.newest = max(look.newest, changed(est))', 'pass', Tmp, 'test_the_newest_change_inside_counts'),
    'tmp: the inode time counts': ('guard', 'return max(st.st_mtime, st.st_ctime)', 'return st.st_mtime',
                                   Tmp, 'test_a_folder_copied_today_with_old_dates_is_not_old'),
    'tmp: names it never takes': ('rules', 'if matches(name, TMP_NEVER):', 'if False:', Tmp, 'test_names_it_never_takes'),
    'tmp: the hard refusals': ('rules', "look = guard.examine(cfg, facts, path, 'path')", 'look = guard.Look(size=1, newest=0.0)',
                               Tmp, 'test_the_hard_refusals_hold_here_too'),
    'tmp: the last check before a delete': ('engine', 'refusal = rules.recheck(cfg, facts, item)', 'refusal = None',
                                            Tmp, 'test_everything_is_read_again_just_before_the_delete'),
    'tmp: at least one day': ('config', 'if type(days) is not int or days < 1:', 'if False:', Tmp, 'test_bad_patterns_are_refused'),
    'tmp: a root that is safe': ('config', 'if _holds(root, self.home):', 'if False:', Tmp, 'test_bad_patterns_are_refused'),
    'docker: off by default': ('rules', 'if not cfg.docker_enabled:', 'if False:', Docker, 'test_it_is_off_unless_the_config_turns_it_on'),
    'docker: off by default in the config': ('config', "'enabled': (bool, False, None)", "'enabled': (bool, True, None)",
                                             Docker, 'test_it_is_off_unless_the_config_turns_it_on'),
    'docker: named never': ('rules', 'if not ANONYMOUS.match(volume.name):', 'if False:', Docker, 'test_a_named_volume_is_never_removed'),
    'docker: used by a container': ('facts', "'--filter', 'dangling=true'", "'--filter', 'dangling=false'",
                                    Docker, 'test_a_volume_any_container_uses_is_never_removed'),
    'docker: seven days': ('rules', 'if age < cfg.docker_days * DAY:', 'if False:', Docker, 'test_a_volume_under_seven_days_old_is_kept'),
    'docker: the seven day floor': ('config', "'days': (int, MIN_DOCKER_DAYS, _at_least(MIN_DOCKER_DAYS))", "'days': (int, MIN_DOCKER_DAYS, None)",
                                    Docker, 'test_a_volume_under_seven_days_old_is_kept'),
    'docker: no answer': ('rules', "kept.append(Kept('docker', 'docker', 'could not read', str(ex)))", 'pass',
                          Docker, 'test_docker_that_cannot_answer_means_keep'),
    'docker: a failed command': ('facts', 'if done.returncode != 0:  # docker', 'if False:', Docker, 'test_docker_that_cannot_answer_means_keep'),
    'docker: a process in the volume': ('rules', 'if volume.mountpoint in processes.busy:', 'if False:', Docker, 'test_a_process_in_the_volume_keeps_it'),
    'docker: asked again before the delete': ('rules', 'return recheck_volume(cfg, facts, item)', 'return None', Docker, 'test_docker_is_asked_again_just_before_the_delete'),
    'docker: no --force': ('facts', "'volume', 'rm', name)", "'volume', 'rm', '--force', name)", Docker, 'test_docker_is_asked_again_just_before_the_delete'),
    'plan deletes nothing': ('engine', 'if not really:', 'if False:', Engine, 'test_plan_changes_nothing_and_writes_no_log'),
    'plan writes no log': ('engine', 'log = Log(cfg) if really else None', 'log = Log(cfg)', Engine, 'test_plan_changes_nothing_and_writes_no_log'),
    'one log line per delete': ('engine', "out(f'deleted {item.label} ({human(item.size)}, {item.rule}: {item.why})')", 'pass',
                                Engine, 'test_run_writes_one_log_line_for_each_delete'),
    'the log is trimmed': ('engine', 'if len(lines) > self.cfg.log_lines:', 'if False:', Engine, 'test_the_log_keeps_its_newest_lines'),
    'a failed delete fails the run': ('engine', 'failed += 1', 'pass', Engine, 'test_a_delete_that_fails_is_reported_and_the_run_goes_on'),
    'what it could not read fails the run': ('engine', "unread = any(k.code == 'could not read' for k in plan.kept)", 'unread = False',
                                             Engine, 'test_what_it_could_not_read_makes_the_run_fail'),
    'one run at a time': ('engine', 'fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)', 'pass', Engine, 'test_two_runs_at_once'),
    'the log is never a link': ('engine', '| os.O_NOFOLLOW', '', Engine, 'test_no_log_file_means_no_delete'),
    'config: unknown keys': ('config', 'values = machine_file.fields(section, SPEC, where)',
                             'values = machine_file.fields({k: v for k, v in section.items() if k in SPEC}, SPEC, where)',
                             Config, 'test_unknown_keys_and_wrong_values_are_refused'),
    'config: a bad machine file stops the command': ('engine', 'return 2  # config', 'return 0  # config',
                                                     Config, 'test_no_usable_machine_file_means_nothing_is_done'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
