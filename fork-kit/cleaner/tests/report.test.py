"""`fork-cleaner report` (lib/cleaner/report.py): GB per class from the cleaner's own class list, sizes without
following links and with hard links counted once, "could not read" per class, and what runs freed since the last
report from the cleaner's log. Everything lives in the test's own temporary folder; Docker is a stub command.
RedCheck breaks each rule in a copy of the code and shows its test then fails.

  python3 -I -B fork-kit/cleaner/tests/report.test.py
"""
import json, os, sys, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cleaner_fixture as fx  # noqa: E402

MB = 1024 * 1024
GB = 1024 ** 3
HOST = 'host-1'


def stamp(seconds_ago=0):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - seconds_ago))


class Case(fx.Case):
    c = fx.load()

    def big(self, path, mb=1):
        """A file of `mb` MB of real (not sparse) data."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(b'\1' * (mb * MB))
        return path

    def classes(self, cfg, docker=None):
        """{class: Sized} as one report measures them."""
        sizer = self.c.report.Sizer()
        return {name: self.c.report.measure(cfg, sizer, name, source, docker or self.no_docker)
                for name, source in self.c.config.CLASSES}

    def no_docker(self):
        raise AssertionError('Docker was asked')

    def report(self, cfg, docker=None, disk=(100 * GB, 200 * GB, None)):
        """(exit code, lines) of one report. `disk` is (used, size, device) of the home folder's disk; device None
        means the home folder's own."""
        used, size, dev = disk
        lines = []
        code = self.c.report.report(cfg, lines.append, docker=docker or self.no_docker, host=HOST,
                                    disk_of=lambda cfg: (used, size, dev if dev is not None else os.stat(cfg.home).st_dev))
        return code, lines

    def part(self, line, name):
        """The text of one class in the line."""
        body = line.split(': ', 1)[1].split('; ')[0]
        found = [p for p in body.split(', ') if p.startswith(name + ' ')]
        self.assertEqual(len(found), 1, line)
        return found[0][len(name) + 1:]

    def write_log(self, cfg, lines, end='\n'):
        os.makedirs(os.path.dirname(cfg.log_file), exist_ok=True)
        with open(cfg.log_file, 'w') as f:
            f.write('\n'.join(lines) + end)

    def listing(self):
        return sorted(os.path.join(root, n) for root, dirs, files in os.walk(self.dir) for n in dirs + files)


class Classes(Case):
    def test_the_classes_are_the_cleaners_own_list(self):
        """One source: the line names exactly config.CLASSES, in order, then everything else; a class added there is
        in the line with no other change."""
        code, lines = self.report(self.cfg())
        self.assertEqual(code, 0, lines)
        body = lines[0].split(': ', 1)[1].split('; ')[0]
        names = [name for name, _ in self.c.config.CLASSES] + ['everything else']
        self.assertEqual([p for p in body.split(', ') if not any(p.startswith(n + ' ') for n in names)], [], body)
        self.assertEqual([n for n in names if f'{n} ' in body], names)
        self.assertEqual(sorted(body.index(f'{n} ') for n in names), [body.index(f'{n} ') for n in names])
        extra = self.c.config.CLASSES + (('sample class', ('~/sample',)),)
        self.big(self.path('home', 'sample', 'a.bin'), 2)
        with mock.patch.object(self.c.report, 'CLASSES', extra):
            line = self.report(self.cfg())[1][0]
        self.assertEqual(self.part(line, 'sample class'), '0.0 GB')

    def test_every_rule_and_every_must_keep_path_has_a_class(self):
        cf = self.c.config
        sources = [s for _, s in cf.CLASSES]
        self.assertEqual(sorted(s for s in sources if isinstance(s, str)), ['rule:docker', 'rule:tmp', 'rule:worktrees'])
        self.assertEqual(sorted(p for s in sources if isinstance(s, tuple) for p in s), sorted(cf.MUST_KEEP))
        self.assertEqual(cf.MUST_KEEP, cf.AGENT_HOMES + cf.T3_DATA)
        # every rule plan() runs is one of these: a plan names only these rules
        self.assertEqual(set(self.c.report.RULES), {'worktrees', 'docker', 'tmp'})
        self.assertEqual(dict(cf.CLASSES)['build caches'], None)
        line = self.report(self.cfg())[1][0]
        self.assertEqual(self.part(line, 'build caches'), 'no rule yet')

    def test_worktrees_are_the_folders_the_rule_looks_at(self):
        clone = self.clone('base')
        tree = self.worktree(clone, 'fix-1')
        self.big(os.path.join(tree, 'build.bin'), 2)
        self.big(self.path('trees', 'not-git', 'a.bin'), 3)            # in the root, but no worktree of the repository
        other = self.clone('other', repo='acme/other')
        self.big(os.path.join(other, 'b.bin'), 4)                      # a clone of another repository
        sized = self.classes(self.repo_cfg())['worktrees']
        self.assertEqual(sized.problems, [])
        self.assertGreaterEqual(sized.total, 2 * MB)
        self.assertLess(sized.total, 3 * MB)
        self.assertEqual(self.classes(self.cfg())['worktrees'].total, 0)

    def test_temp_is_the_entries_a_pattern_takes(self):
        self.big(self.path('tmp', 'job-1', 'a.bin'), 2)                # taken by the pattern, whatever its age
        self.big(self.path('tmp', 'other', 'a.bin'), 3)
        self.big(self.path('tmp', 'claude-1', 'a.bin'), 4)              # a name no pattern may take
        sized = self.classes(self.tmp_cfg({'job-*': 7, 'claude-*': 1}))['temp']
        self.assertGreaterEqual(sized.total, 2 * MB)
        self.assertLess(sized.total, 3 * MB)
        self.assertEqual(self.classes(self.cfg())['temp'].total, 0)    # no pattern: the rule looks at nothing

    def test_agent_transcripts_and_t3_data_are_the_must_keep_groups(self):
        self.big(self.path('home', '.claude', 'projects', 'a.jsonl'), 1)
        self.big(self.path('home', '.claude-2', 'b.jsonl'), 2)
        self.big(self.path('home', '.codex', 'sessions', 'c.jsonl'), 1)
        self.big(self.path('home', '.t3', 'userdata', 'state.sqlite'), 3)
        self.big(self.path('home', '.t3', 'projects', 'big.bin'), 5)     # not T3's data: everything else
        got = self.classes(self.cfg())
        self.assertGreaterEqual(got['agent transcripts'].total, 4 * MB)
        self.assertLess(got['agent transcripts'].total, 5 * MB)
        self.assertGreaterEqual(got['T3 data'].total, 3 * MB)
        self.assertLess(got['T3 data'].total, 4 * MB)

    def test_docker_volumes_are_dockers_own_measure_and_off_unless_turned_on(self):
        exe, state = self.stub('docker', fx.STUB_DOCKER, {'volumes': {fx.HEX: {'size': '1.5GB'}, 'named': {'size': '500MB'}},
                                                          'root': self.dir})
        with mock.patch.object(self.c.facts, 'DOCKER', exe):
            code, lines = self.report(self.cfg(), docker=self.c.facts.docker_volume_sizes)
            self.assertEqual(self.part(lines[0], 'docker volumes'), 'off')
            self.assertEqual(self.calls(state), [])                     # off: Docker is not even asked
            code, lines = self.report(self.cfg(docker={'enabled': True}), docker=self.c.facts.docker_volume_sizes)
            self.assertEqual(code, 0, lines)
            self.assertEqual(self.part(lines[0], 'docker volumes'), f'{2e9 / GB:.1f} GB')
            with open(state) as f:
                broken = {**json.load(f), 'fail': True}
            with open(state, 'w') as f:
                json.dump(broken, f)
            code, lines = self.report(self.cfg(docker={'enabled': True}), docker=self.c.facts.docker_volume_sizes)
        self.assertEqual(code, 1)
        self.assertIn('could not read (Docker', self.part(lines[0], 'docker volumes'))

    def test_a_size_docker_cannot_tell_is_could_not_read(self):
        exe, _ = self.stub('docker', fx.STUB_DOCKER, {'volumes': {fx.HEX: {'size': 'N/A'}}, 'root': self.dir})
        with mock.patch.object(self.c.facts, 'DOCKER', exe):
            code, lines = self.report(self.cfg(docker={'enabled': True}), docker=self.c.facts.docker_volume_sizes)
        self.assertEqual(code, 1)
        self.assertIn('could not read', self.part(lines[0], 'docker volumes'))

    def test_everything_else_is_the_rest_of_the_used_disk(self):
        cfg = self.cfg(docker={'enabled': True})
        docker = lambda: ({fx.HEX: 5 * GB}, self.dir)   # noqa: E731  (on the test folder's disk)
        line = self.report(cfg, docker=docker, disk=(10 * GB, 50 * GB, None))[1][0]
        self.assertEqual(self.part(line, 'docker volumes'), '5.0 GB')
        self.assertEqual(self.part(line, 'everything else'), '5.0 GB (10.0 GB of 50.0 GB used)')
        line = self.report(cfg, docker=docker, disk=(10 * GB, 50 * GB, -1))[1][0]
        self.assertEqual(self.part(line, 'docker volumes'), '5.0 GB (on another disk)')
        self.assertEqual(self.part(line, 'everything else'), '10.0 GB (10.0 GB of 50.0 GB used)')

        self.big(self.path('home', '.claude', 'a.jsonl'), 2)
        counted = self.classes(self.cfg())['agent transcripts'].total
        line = self.report(self.cfg(), disk=(10 * GB, 50 * GB, None))[1][0]
        self.assertEqual(self.part(line, 'everything else'), f'{(10 * GB - counted) / GB:.1f} GB (10.0 GB of 50.0 GB used)')
        # A class on another disk is shown, and not taken off this disk's rest.
        line = self.report(self.cfg(), disk=(10 * GB, 50 * GB, -1))[1][0]
        self.assertEqual(self.part(line, 'agent transcripts'), '0.0 GB (on another disk)')
        self.assertEqual(self.part(line, 'everything else'), '10.0 GB (10.0 GB of 50.0 GB used)')

    def test_the_report_deletes_and_changes_nothing(self):
        d = self.old_dir('job-1', 400)
        self.finished('fix-1', hours=400)
        self.big(self.path('home', '.claude', 'a.jsonl'))
        cfg = self.repo_cfg(tmp={'root': self.tmp, 'patterns': {'job-*': 1}})
        before = self.listing()
        code, lines = self.report(cfg)
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.listing(), before)                       # no log yet: not even a mark
        self.assertTrue(os.path.isdir(d))


class Sizes(Case):
    def test_links_are_never_followed(self):
        self.big(self.path('elsewhere', 'big.bin'), 8)
        os.makedirs(self.path('home', '.claude'))
        os.symlink(self.path('elsewhere', 'big.bin'), self.path('home', '.claude', 'link.bin'))
        os.symlink(self.path('elsewhere'), self.path('home', '.claude', 'link-dir'))
        os.symlink(self.path('elsewhere'), self.path('home', '.claude-linked'))    # a whole agent home that is a link
        os.makedirs(self.path('home', '.t3'))
        os.symlink(self.path('elsewhere'), self.path('home', '.t3', 'userdata'))
        got = self.classes(self.cfg())
        self.assertLess(got['agent transcripts'].total, MB)
        self.assertLess(got['T3 data'].total, MB)
        os.symlink(self.path('elsewhere'), self.path('home', '.t3x'))             # a link in the middle of a pattern
        os.makedirs(self.path('elsewhere', 'userdata'))
        self.big(self.path('elsewhere', 'userdata', 'db'), 4)
        self.assertLess(self.classes(self.cfg())['T3 data'].total, MB)

    def test_hard_links_count_once_in_a_class_and_across_classes(self):
        one = self.big(self.path('home', '.claude', 'a.bin'), 4)
        os.link(one, self.path('home', '.claude', 'b.bin'))
        os.makedirs(self.path('home', '.t3', 'userdata'))
        os.link(one, self.path('home', '.t3', 'userdata', 'c.bin'))
        got = self.classes(self.cfg())
        self.assertGreaterEqual(got['agent transcripts'].total, 4 * MB)
        self.assertLess(got['agent transcripts'].total, 5 * MB)
        self.assertLess(got['T3 data'].total, MB)                      # counted in the first class that reached it

    @unittest.skipIf(os.geteuid() == 0, 'root reads every folder')
    def test_a_class_that_cannot_be_read_says_so_and_the_rest_still_prints(self):
        self.big(self.path('home', '.claude', 'a.jsonl'))
        self.big(self.path('home', '.t3', 'userdata', 'locked', 'db'))
        os.chmod(self.path('home', '.t3', 'userdata', 'locked'), 0)
        code, lines = self.report(self.cfg())
        self.assertEqual(code, 1, lines)
        self.assertIn('could not read', self.part(lines[0], 'T3 data'))
        self.assertIn('Permission denied', self.part(lines[0], 'T3 data'))
        self.assertTrue(self.part(lines[0], 'agent transcripts').endswith(' GB'))
        self.assertIn('(with what could not be read)', self.part(lines[0], 'everything else'))
        self.assertIn('freed', lines[0])

    @unittest.skipIf(os.geteuid() == 0, 'root reads every folder')
    def test_an_unreadable_root_or_pattern_folder_is_could_not_read(self):
        self.mkdir('tmp', 'job-1')
        os.chmod(self.tmp, 0o300)
        self.assertNotEqual(self.classes(self.tmp_cfg({'job-*': 1}))['temp'].problems, [])
        os.chmod(self.tmp, 0o700)
        os.makedirs(self.path('home', '.t3-locked'))
        os.chmod(self.path('home', '.t3-locked'), 0o300)            # it may be passed through: nothing missed
        self.assertEqual(self.classes(self.cfg())['T3 data'].problems, [])
        os.chmod(self.path('home', '.t3-locked'), 0)                # whether its userdata is there cannot be told
        self.assertNotEqual(self.classes(self.cfg())['T3 data'].problems, [])
        os.chmod(self.path('home', '.t3-locked'), 0o700)
        os.chmod(self.home, 0o300)                                  # the * of ~/.t3* cannot be listed
        try:
            self.assertNotEqual(self.classes(self.cfg())['T3 data'].problems, [])
        finally:
            os.chmod(self.home, 0o755)

    def test_a_folder_that_is_gone_counts_nothing(self):
        self.assertEqual(self.c.report.Sizer().size(self.path('no-such')), (0, None, []))


class Freed(Case):
    def deleted(self, path, size='1.0 GB', rule='tmp', ago=0):
        return f'{stamp(ago)} deleted {path} ({size}, {rule}: not changed for 9 days (x: 1 day))'

    def tail(self, line):
        return line.split('; ', 1)[1]

    def test_no_log_means_nothing_freed(self):
        cfg = self.cfg()
        code, lines = self.report(cfg)
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.tail(lines[0]), 'freed 0.0 GB (no delete in the log yet)')
        self.assertFalse(os.path.lexists(cfg.report_file))
        self.assertFalse(os.path.lexists(os.path.dirname(cfg.log_file)))    # nothing made where the log would be

    def test_the_deletes_in_the_log_add_up(self):
        cfg = self.cfg()
        self.write_log(cfg, [f'{stamp(99)} deleting /tmp/a (1.0 GB, tmp: old)', self.deleted('/tmp/a', '1.5 GB', ago=99),
                             self.deleted('docker volume ' + fx.HEX, 'size not known', 'docker'),
                             self.deleted('/w/x', '512.0 MB', 'worktrees'), f'{stamp()} total: deleted 3 (2.0 GB), kept 0, failed 0'])
        code, lines = self.report(cfg)
        self.assertEqual(code, 0, lines)
        self.assertIn(f'freed since the log began at {stamp(99)[:10]}', self.tail(lines[0]))
        self.assertIn('2.0 GB (3 deletes, 1 of unknown size)', self.tail(lines[0]))
        self.assertNotIn('not understood', lines[0])

    def test_the_next_report_counts_only_what_came_after(self):
        cfg = self.cfg()
        log = [self.deleted('/tmp/a', '2.0 GB')]
        self.write_log(cfg, log)
        self.assertIn('2.0 GB (1 delete)', self.report(cfg)[1][0])
        self.assertTrue(os.path.isfile(cfg.report_file))
        line = self.report(cfg)[1][0]
        self.assertIn('freed since the last report at', line)
        self.assertIn(' 0.0 GB (0 deletes)', line)
        log += [self.deleted('/tmp/b', '3.0 GB'), f'{stamp()} total: deleted 1 (3.0 GB), kept 0, failed 0']
        self.write_log(cfg, log)
        self.assertIn(' 3.0 GB (1 delete)', self.report(cfg)[1][0])
        # The log was trimmed past the last report's line: every line left is newer than that report.
        log = [self.deleted('/tmp/c', '1.0 GB'), self.deleted('/tmp/d', '1.0 GB')]
        self.write_log(cfg, log)
        self.assertIn(' 2.0 GB (2 deletes)', self.report(cfg)[1][0])

    def test_lines_of_an_older_version_are_skipped_and_counted(self):
        cfg = self.cfg()
        self.write_log(cfg, [self.deleted('/tmp/a', '1.0 GB'),
                             f'{stamp()} removed /tmp/old (rule tmp)',               # a word this version does not write
                             f'{stamp()} deleted /tmp/b 1.0 GB',                    # a delete without its size and rule
                             f'{stamp()} deleted /tmp/c (1.0 GB, scratch: old)',      # a rule this version does not have
                             'a line without a time'])
        code, lines = self.report(cfg)
        self.assertEqual(code, 0, lines)
        self.assertIn(' 1.0 GB (1 delete); 4 log lines not understood, skipped', lines[0])

    def test_a_line_still_being_written_waits_for_the_next_report(self):
        cfg = self.cfg()
        self.write_log(cfg, [self.deleted('/tmp/a', '1.0 GB'), self.deleted('/tmp/b', '4.0 GB')[:30]], end='')
        self.assertIn(' 1.0 GB (1 delete)', self.report(cfg)[1][0])
        self.write_log(cfg, [self.deleted('/tmp/a', '1.0 GB'), self.deleted('/tmp/b', '4.0 GB')])
        self.assertIn(' 4.0 GB (1 delete)', self.report(cfg)[1][0])

    def test_a_log_that_is_not_the_cleaners_is_could_not_read(self):
        cfg = self.cfg()
        self.write_log(cfg, ['somebody else\'s file'])
        code, lines = self.report(cfg)
        self.assertEqual(code, 1)
        self.assertIn('freed could not read (the log', lines[0])
        self.assertFalse(os.path.lexists(cfg.report_file))

    def test_a_mark_it_cannot_use_counts_the_whole_log_and_says_so(self):
        cfg = self.cfg()
        self.write_log(cfg, [self.deleted('/tmp/a', '1.0 GB')])
        with open(cfg.report_file, 'w') as f:
            f.write('{"version": 99}\n')
        code, lines = self.report(cfg)
        self.assertEqual(code, 1, lines)
        self.assertIn(' 1.0 GB (1 delete) (the last report\'s mark could not be read', lines[0])
        self.assertEqual(self.report(cfg)[0], 0)                       # the mark was written anew

    def test_a_mark_that_cannot_be_written_is_said(self):
        cfg = self.cfg()
        self.write_log(cfg, [self.deleted('/tmp/a', '1.0 GB')])
        os.mkdir(cfg.report_file)
        code, lines = self.report(cfg)
        self.assertEqual(code, 1, lines)
        self.assertIn('the next report counts these deletes again', lines[-1])


class Command(Case):
    def test_the_command_reads_the_machine_file(self):
        path = self.path('machine.json')
        with open(path, 'w') as f:
            json.dump({'version': 1, 'cleaner': dict(self.section)}, f)
        lines = []
        with mock.patch.object(self.c.config, 'MACHINE_FILE', path), \
                mock.patch.object(self.c.config, 'MACHINE_OWNER', os.getuid()):
            self.assertEqual(self.c.engine.main(['report'], say=lines.append), 0)
            self.assertTrue(lines[0].startswith('disk on '), lines)
            with mock.patch.object(self.c.engine.os, 'geteuid', lambda: 0):
                self.assertEqual(self.c.engine.main(['report'], say=lines.append), 2)
            self.assertEqual(self.c.engine.main(['report', '--all'], say=lines.append), 2)


MUTATIONS = {
    'one class list': ('report', 'for name, source in CLASSES]', "for name, source in CLASSES[:1]]",
                       Classes, 'test_the_classes_are_the_cleaners_own_list'),
    'worktrees: the rule\'s own folders': ('report', 'if kind is not None:', 'if True:', Classes, 'test_worktrees_are_the_folders_the_rule_looks_at'),
    'temp: the rule\'s own entries': ('report', 'if rules.tmp_match(cfg, entry) is not None:', 'if True:',
                                      Classes, 'test_temp_is_the_entries_a_pattern_takes'),
    'docker: off is not asked': ('report', 'if not cfg.docker_enabled:', 'if False:',
                                 Classes, 'test_docker_volumes_are_dockers_own_measure_and_off_unless_turned_on'),
    'docker: no size is no answer': ('facts', 'if not found:\n            raise Unreadable(f\'Docker (docker system df',
                                     'if False:\n            raise Unreadable(f\'Docker (docker system df',
                                     Classes, 'test_a_size_docker_cannot_tell_is_could_not_read'),
    'everything else: only this disk': ('report', 'counted = sum(s.by_dev.get(home_dev, 0) for s in classes)',
                                        'counted = sum(s.total for s in classes)', Classes, 'test_everything_else_is_the_rest_of_the_used_disk'),
    'links: not followed': ('report', 'est = entry.stat(follow_symlinks=False)', 'est = entry.stat()', Sizes, 'test_links_are_never_followed'),
    'links: not in a pattern': ('report', 'st = _lstat(base)', 'st = os.stat(base) if os.path.exists(base) else None',
                                Sizes, 'test_links_are_never_followed'),
    'could not read: a path in a pattern': ('report', "raise Unreadable(f'{path}: {ex.strerror}') from None", 'return None',
                                            Sizes, 'test_an_unreadable_root_or_pattern_folder_is_could_not_read'),
    'hard links: once': ('report', 'if key in self.seen:', 'if False:', Sizes, 'test_hard_links_count_once_in_a_class_and_across_classes'),
    'could not read: a folder': ('report', "problems.append(f'{folder}: {ex.strerror}')", 'pass',
                                 Sizes, 'test_a_class_that_cannot_be_read_says_so_and_the_rest_still_prints'),
    'could not read: fails the report': ('report', 'failed = any(s.problems for s in classes) or', 'failed = False and',
                                         Sizes, 'test_a_class_that_cannot_be_read_says_so_and_the_rest_still_prints'),
    'could not read: a pattern folder': ('report', "raise Unreadable(f'{base}: {ex.strerror}') from None", 'continue',
                                         Sizes, 'test_an_unreadable_root_or_pattern_folder_is_could_not_read'),
    'freed: no log is 0': ('report', 'except FileNotFoundError:\n        return out\n    except OSError as ex:\n        out.problem',
                           'except FileNotFoundError:\n        out.since = \'x\'\n        return out\n    except OSError as ex:\n        out.problem',
                           Freed, 'test_no_log_means_nothing_freed'),
    'freed: deletes add up': ('report', 'out.bytes += size', 'pass', Freed, 'test_the_deletes_in_the_log_add_up'),
    'freed: after the mark only': ('report', 'start = hits[-1] + 1 if hits else 0', 'start = 0', Freed, 'test_the_next_report_counts_only_what_came_after'),
    'freed: the mark is moved': ('report', 'write_mark(cfg, got.mark)', 'pass', Freed, 'test_the_next_report_counts_only_what_came_after'),
    'freed: old lines counted': ('report', 'out.skipped += 1            # a line of an older', 'pass            # a line of an older',
                                 Freed, 'test_lines_of_an_older_version_are_skipped_and_counted'),
    'freed: a delete of an old format': ('report', 'if size is None:\n                out.skipped += 1', 'if size is None:\n                pass',
                                         Freed, 'test_lines_of_an_older_version_are_skipped_and_counted'),
    'freed: a half-written line waits': ('report', ".split('\\n')[:-1]", ".split('\\n')", Freed, 'test_a_line_still_being_written_waits_for_the_next_report'),
    'freed: only the cleaner\'s log': ('report', 'problem = log_problem(path=cfg.log_file)', 'problem = None',
                                       Freed, 'test_a_log_that_is_not_the_cleaners_is_could_not_read'),
    'freed: a bad mark is said': ('report', 'or got.mark_problem\n', '\n', Freed, 'test_a_mark_it_cannot_use_counts_the_whole_log_and_says_so'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
