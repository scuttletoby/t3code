"""Tests of the state tables (fork-kit/agent-guards/FACTS.md): agent_guards.facts (read, search, index, the
gardener's check, project matching), the `facts` command and the `facts-hook` hook (SessionStart: the things;
PreCompact: keep names, not rows). Tables are made up in a temporary folder; the machine file is the test's own.

  python3 -I -B fork-kit/agent-guards/tests/facts.test.py
"""
import datetime, json, os, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guards_fixture as fx  # noqa: E402
from agent_guards import config, facts  # noqa: E402

HEAD = '| Thing | What we made / how to use it | State now | Why | Updated |\n|---|---|---|---|---|\n'
TABLE = ('# What we have\n\nSome text first.\n\n' + HEAD
         + '| Example GPU | `gpu on\\|off\\|status` helper; off at boot | OFF on purpose; the vendor tool fails while '
           'off | quiet fans | 11-23 |\n'
         + '| Example backup job | `backup-ctl run` nightly | Paused until the new disk | disk alarm | 2026-11-20 |\n'
         + '| Example web cache | none yet | not made | | 11-01 |\n'
         + '\nA closing line.\n')
TODAY = datetime.date(2026, 11, 23)
HOOK = 'facts-hook'


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    return path


class Tables(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='facts-test-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.place = os.path.join(self.tmp, 'machine-facts')
        write(os.path.join(self.place, 'STATE.md'), TABLE)

    def found(self, *entries):
        return [facts.read_place(e, 'test') for e in entries]

    def test_a_table_is_read_with_escaped_pipes_and_the_text_around_it_skipped(self):
        [t] = facts.parse(TABLE, 'STATE.md')
        self.assertEqual([r.thing for r in t.rows], ['Example GPU', 'Example backup job', 'Example web cache'])
        self.assertEqual(t.rows[0].cell('What we made / how to use it'), '`gpu on|off|status` helper; off at boot')
        self.assertEqual(t.rows[0].cell('updated'), '11-23')
        self.assertEqual(t.rows[0].lineno, 7)
        self.assertEqual(t.warnings, [])

    def test_a_broken_row_is_still_listed_with_a_warning(self):
        text = HEAD + '| Broken thing | only two cells |\n| Good thing | a | b | c | 11-23 |\n'
        [t] = facts.parse(text, 'S.md')
        self.assertEqual([r.thing for r in t.rows], ['Broken thing', 'Good thing'])
        self.assertEqual(t.warnings, ['S.md:3: row "Broken thing" has 2 cells, the header has 5'])
        [t] = facts.parse('| Thing | State now |\n| No line under the header | on |\n', 'S.md')
        self.assertEqual([r.thing for r in t.rows], ['No line under the header'])
        self.assertIn('the header has no |---| line under it', t.warnings[0])

    def test_two_tables_and_other_tables(self):
        text = ('| Name | x |\n|---|---|\n| not a state row | y |\n\n' + HEAD + '| A | a | on | w | 11-23 |\n\n'
                + HEAD + '| B | b | off | w | 11-22 |\n')
        self.assertEqual([[r.thing for r in t.rows] for t in facts.parse(text)], [['A'], ['B']])

    def test_search_prints_the_header_and_the_matching_rows_thing_hits_first(self):
        found = self.found(self.place)
        lines = facts.search(found, ['GPU'])
        self.assertEqual(lines[0], os.path.join(self.place, 'STATE.md') + ':')
        self.assertEqual(lines[1:3], HEAD.rstrip('\n').split('\n'))
        self.assertEqual(len(lines), 4)
        self.assertIn('| Example GPU |', lines[3])
        lines = facts.search(found, ['disk'])                     # in the State and Why columns only
        self.assertIn('| Example backup job |', lines[3])
        lines = facts.search(found, ['off'])                      # Thing column hits first, then the others
        self.assertEqual([ln.split('|')[1].strip() for ln in lines[3:]], ['Example GPU'])
        lines = facts.search(found, ['example', 'cache'])          # every word
        self.assertEqual(len(lines), 4)
        self.assertIn('Example web cache', lines[3])

    def test_search_with_no_row_holding_every_word_shows_rows_with_some_and_says_so(self):
        found = self.found(self.place)
        lines = facts.search(found, ['gpu', 'broken'])
        self.assertEqual(lines[0], 'No row holds all of: gpu broken. Rows with some of them:')
        self.assertIn('| Example GPU |', lines[-1])
        lines = facts.search(found, ['nothing-like-this'])
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('No row matches: nothing-like-this (in 1 table file(s)'), lines)

    def test_the_index_lists_every_thing_and_caps(self):
        found = self.found(self.place)
        self.assertEqual(facts.index(found), [f'{os.path.join(self.place, "STATE.md")} (3 rows):',
                                              '  - Example GPU', '  - Example backup job', '  - Example web cache'])
        self.assertEqual(facts.index(found, 2)[-1], '  ... and 1 more (`facts --index`)')
        self.assertEqual(len(facts.index(found, 2)), 4)

    def test_a_missing_or_empty_folder_or_a_file_with_no_table_is_a_note(self):
        empty = os.path.join(self.tmp, 'empty')
        os.makedirs(empty)
        plain = write(os.path.join(self.tmp, 'plain.md'), '# notes\nno table here\n')
        found = self.found(self.place, os.path.join(self.tmp, 'gone'), empty, plain, 'relative/path')
        self.assertEqual([len(p.rows()) for p in found], [3, 0, 0, 0, 0])
        self.assertEqual(facts.notes(found), [
            f'{os.path.join(self.tmp, "gone")}: not there',
            f'{empty}: no STATE.md in this folder yet',
            f'{plain}: no state table in it (a header row starting "| Thing |")',
            'relative/path: not an absolute path (or ~/...): skipped'])
        self.assertEqual(len(facts.search(found, ['gpu'])), 4)      # the rest is still searched
        self.assertEqual(facts.index(self.found(empty)), [])

    def test_updated_dates(self):
        self.assertEqual(facts.updated('11-23', TODAY), datetime.date(2026, 11, 23))
        self.assertEqual(facts.updated('2025-02-03', TODAY), datetime.date(2025, 2, 3))
        self.assertEqual(facts.updated(' 2026-11-01 (moved) ', TODAY), datetime.date(2026, 11, 1))
        new_year = datetime.date(2027, 1, 2)
        self.assertEqual(facts.updated('12-30', new_year), datetime.date(2026, 12, 30))   # across the year's end
        self.assertEqual(facts.updated('01-03', new_year), datetime.date(2027, 1, 3))     # tomorrow: a time zone
        for bad in ('', 'soon', '13-40', '2026-02-30', None):
            self.assertIsNone(facts.updated(bad, TODAY), bad)

    def test_the_gardener_check_reports_stale_rows_no_dates_and_big_tables(self):
        found = self.found(self.place)
        self.assertEqual(facts.check(found, 30, 60, TODAY), [])
        out = facts.check(found, 5, 2, TODAY)
        self.assertEqual(len(out), 2, out)
        self.assertIn('3 rows, over the cap of 2 (maxRows)', out[0])
        self.assertIn(':9: "Example web cache": Updated 11-01 is 22 days old (staleDays 5)', out[1])
        write(os.path.join(self.place, 'STATE.md'), HEAD + '| A | a | on | w | soon |\n| B | b | c |\n')
        out = facts.check(self.found(self.place, os.path.join(self.tmp, 'gone')), 30, 60, TODAY)
        self.assertEqual(len(out), 4, out)
        self.assertIn('not there', out[0])
        self.assertIn('row "B" has 3 cells', out[1])
        self.assertIn('"A": no date in Updated (\'soon\')', out[2])
        self.assertIn('"B": no date in Updated (\'\')', out[3])
        write(os.path.join(self.place, 'STATE.md'), '| Thing | State now |\n|---|---|\n| A | on |\n')
        self.assertEqual(facts.check(self.found(self.place), 30, 60, TODAY)[0].split(': ', 1)[1],
                         'the table has no "Updated" column')


def git(*args, cwd=None):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL,
                   env=dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1'))


class Projects(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='facts-projects-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        git('init', '-q', '-b', 'main', cwd=self.repo)
        git('remote', 'add', 'origin', 'git@github.com:Example/Project.git', cwd=self.repo)
        git('-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'x',
            cwd=self.repo)
        self.other = os.path.join(self.tmp, 'other')
        os.makedirs(self.other)
        git('init', '-q', cwd=self.other)
        git('remote', 'add', 'origin', 'https://github.com/example/other.git', cwd=self.other)
        self.cfg = config.defaults('facts')
        self.cfg['dirs'] = ['/m']
        self.cfg['projects'] = [{'match': 'github.com/example/project', 'dirs': ['/p'], 'text': 'project text'},
                                {'match': os.path.join(self.tmp, 'notes'), 'dirs': ['/n']}]

    def entries(self, cwd, **kw):
        return [e for e, _s in facts.entries(self.cfg, cwd, env={}, **kw)]

    def test_remote_spellings(self):
        for url in ('git@github.com:example/project.git', 'https://github.com/Example/project', 'ssh://git@github.com/'
                    'example/project.git', 'https://user@github.com/example/project/', 'ssh://git@github.com:22/'
                    'example/project'):
            self.assertEqual(facts.remote_key(url), 'github.com/example/project', url)

    def test_a_checkout_and_its_linked_worktree_match_by_remote_another_project_does_not(self):
        self.assertEqual(self.entries(self.repo), ['/m', '/p'])
        os.makedirs(os.path.join(self.repo, 'sub'))
        self.assertEqual(self.entries(os.path.join(self.repo, 'sub')), ['/m', '/p'])
        wt = os.path.join(self.tmp, 'worktrees', 'repo-wt')
        git('worktree', 'add', '-q', wt, cwd=self.repo)
        self.assertEqual(self.entries(wt), ['/m', '/p'])
        self.assertEqual(self.entries(self.other), ['/m'])
        self.assertEqual([p['text'] for p in facts.projects_for(self.cfg, wt)], ['project text'])

    def test_a_folder_with_no_git_a_gone_folder_or_a_changed_remote_gets_the_machine_tables_only(self):
        plain = os.path.join(self.tmp, 'plain')
        os.makedirs(plain)
        self.assertEqual(self.entries(plain), ['/m'])
        self.assertEqual(self.entries(os.path.join(self.tmp, 'gone')), ['/m'])
        self.assertEqual(self.entries(''), ['/m'])
        git('remote', 'set-url', 'origin', 'https://github.com/example/renamed.git', cwd=self.repo)
        self.assertEqual(self.entries(self.repo), ['/m'])

    def test_a_folder_match_takes_that_folder_and_below_only(self):
        notes = os.path.join(self.tmp, 'notes')
        os.makedirs(os.path.join(notes, 'deep'))
        os.makedirs(os.path.join(self.tmp, 'notes-other'))
        self.assertEqual(self.entries(os.path.join(notes, 'deep')), ['/m', '/n'])
        self.assertEqual(self.entries(os.path.join(self.tmp, 'notes-other')), ['/m'])

    def test_all_and_the_env_override(self):
        self.assertEqual(self.entries(self.other, every=True), ['/m', '/p', '/n'])
        got = facts.entries(self.cfg, self.repo, env={'FACTS_DIRS': '/a::/b:/a'})
        self.assertEqual(got, [('/a', 'FACTS_DIRS'), ('/b', 'FACTS_DIRS')])


class Command(unittest.TestCase):
    """bin/facts and bin/facts-hook, run as programs with the test's machine file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='facts-command-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.place = os.path.join(self.tmp, 'facts')
        write(os.path.join(self.place, 'STATE.md'), TABLE)
        self.configure(dirs=[self.place])
        self.env = dict(os.environ, HOME=self.tmp)
        self.env.pop('FACTS_DIRS', None)

    def configure(self, **part):
        self.machine = fx.machine_file(self.tmp, {'facts': part})

    def facts(self, *args, env=None):
        return fx.run_hook('facts', '', self.machine, env=env or self.env, cwd=self.tmp, args=args,
                           hook=os.path.join(fx.BIN, 'facts'))

    def hook(self, event, **extra):
        return fx.run_hook(HOOK, dict({'hook_event_name': event, 'session_id': 's', 'cwd': self.tmp}, **extra),
                           self.machine, env=self.env)

    def test_lookup_index_dirs(self):
        r = self.facts('gpu')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split('\n')[1], HEAD.split('\n')[0])
        self.assertIn('| Example GPU |', r.stdout)
        self.assertNotIn('backup', r.stdout)
        r = self.facts('--index')
        self.assertEqual(r.stdout.count('  - '), 3)
        r = self.facts('--dirs')
        self.assertEqual(r.stdout.strip(), f'{self.place} (machine): {self.place}/STATE.md: 3 rows in 1 table(s)')

    def test_the_env_override_and_a_missing_folder(self):
        env = dict(self.env, FACTS_DIRS=f'{self.tmp}/gone:{self.place}')
        r = self.facts('backup', env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('| Example backup job |', r.stdout)
        self.assertIn(f'note: {self.tmp}/gone: not there', r.stdout)

    def test_nothing_configured_or_a_usage_error(self):
        self.configure()
        r = self.facts('gpu')
        self.assertEqual(r.returncode, 0)
        self.assertIn('no state table is configured', r.stdout)
        for args in ((), ('--index', 'gpu'), ('--bogus',), ('--index', '--check')):
            self.assertEqual(self.facts(*args).returncode, 1, args)

    def test_check_reports_and_exits_1(self):
        r = self.facts('--check')
        self.assertEqual(r.returncode, 0, r.stdout)          # rows of 11-01 and later: fresh for 30 days at most
        self.assertTrue(r.stdout.strip().startswith('facts check: OK') or 'days old' in r.stdout, r.stdout)
        write(os.path.join(self.place, 'STATE.md'), HEAD + '| Old thing | a | on | w | 2000-01-01 |\n')
        r = self.facts('--check')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('"Old thing": Updated 2000-01-01 is', r.stdout)
        self.assertIn('facts check: 1 finding(s)', r.stdout)

    def test_a_broken_part_is_named_and_the_defaults_used(self):
        self.machine = fx.machine_file(self.tmp, {'facts': {'dirs': 'not a list', 'staleDays': 0}})
        r = self.facts('gpu')
        self.assertIn('no state table is configured', r.stdout)
        self.assertIn('built-in defaults used', r.stdout)
        r = self.hook('SessionStart', source='startup')
        self.assertEqual(r.returncode, 0)
        self.assertNotIn('additionalContext', r.stdout)
        self.assertIn('built-in defaults used', json.loads(r.stdout)['systemMessage'])

    def test_a_bad_project_row_is_left_out_the_others_kept(self):
        fx.use_machine(fx.machine_file(self.tmp, {'facts': {'projects': [
            {'match': 'github.com/a/b', 'dirs': ['/x']}, {'match': 'not a remote'}, {'match': '/d', 'extra': 1}]}}))
        self.addCleanup(fx.use_machine, fx.NO_MACHINE)
        got, problem = config.load('facts')
        self.assertEqual(got['projects'], [{'match': 'github.com/a/b', 'dirs': ['/x']}])
        self.assertIn('facts.projects: entry 1', problem)
        self.assertIn('entry 2', problem)

    def test_the_hook_at_session_start_shows_the_things_capped(self):
        for source in ('startup', 'resume', 'clear', 'compact'):
            r = self.hook('SessionStart', source=source)
            self.assertEqual(fx.answer(r), 'context', r.stderr)
            text = json.loads(r.stdout)['hookSpecificOutput']['additionalContext']
            self.assertIn('Check the state table before diagnosing or redoing something: `facts <words>`', text)
            self.assertIn('  - Example GPU', text)
        self.configure(dirs=[self.place], indexLines=1)
        text = json.loads(self.hook('SessionStart').stdout)['hookSpecificOutput']['additionalContext']
        self.assertIn('  - Example GPU', text)
        self.assertNotIn('Example web cache', text)
        self.assertIn('... and 2 more', text)

    def test_the_hook_before_compaction_says_keep_names_not_rows(self):
        r = self.hook('PreCompact', trigger='auto')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('keep only the names of the things', r.stdout)
        self.assertIn('`facts <words>`', r.stdout)

    def test_the_hook_adds_the_project_text_in_that_project_only(self):
        proj = os.path.join(self.tmp, 'proj')
        write(os.path.join(proj, 'facts', 'STATE.md'), HEAD + '| Project thing | a | on | w | 11-23 |\n')
        self.configure(projects=[{'match': proj, 'dirs': [os.path.join(proj, 'facts')], 'text': 'Project rule.'}])
        r = self.hook('SessionStart', cwd=proj)
        text = json.loads(r.stdout)['hookSpecificOutput']['additionalContext']
        self.assertIn('  - Project thing', text)
        self.assertTrue(text.endswith('Project rule.'), text)
        self.assertIn('Project rule.', self.hook('PreCompact', cwd=proj).stdout)
        self.assertEqual(self.hook('SessionStart').stdout, '')           # another folder: no table, nothing

    def test_the_hook_is_quiet_with_nothing_to_show(self):
        for event in ('Stop', 'UserPromptSubmit'):
            self.assertEqual(self.hook(event).stdout, '')
        os.unlink(os.path.join(self.place, 'STATE.md'))
        for event in ('SessionStart', 'PreCompact'):
            r = self.hook(event)
            self.assertEqual((r.returncode, r.stdout), (0, ''), r.stderr)
        for text in ('', 'not json', '[]', '{"hook_event_name": "SessionStart", "cwd": 5}'):
            r = fx.run_hook(HOOK, text, self.machine, env=self.env)
            self.assertEqual((r.returncode, r.stdout), (0, ''), text)


if __name__ == '__main__':
    unittest.main()
