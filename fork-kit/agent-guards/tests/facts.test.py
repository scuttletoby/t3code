"""Tests of the state tables (fork-kit/agent-guards/FACTS.md): agent_guards.facts (read, search, index, the
gardener's check, project matching, the record repo search), the `facts` command and the `facts-hook` hook
(SessionStart: the things; PreCompact: keep names, not rows). Tables and record repos are made up in a temporary
folder; the machine file is the test's own. Dates are fixed (TODAY, FACTS_TODAY), so no test depends on the day it
runs.

  python3 -I -B fork-kit/agent-guards/tests/facts.test.py
"""
import datetime, hashlib, json, os, shutil, subprocess, sys, tempfile, time, unittest

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


def write(path, text, mode='w'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        f.write(text)
    return path


def script(path, body):
    write(path, '#!/bin/sh\n' + body + '\n')
    os.chmod(path, 0o755)
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

    def test_short_separators_and_a_second_header_row(self):
        [t] = facts.parse('| Thing | State now |\n|-|:-:|\n| A | on |\n', 'S.md')
        self.assertEqual(([r.thing for r in t.rows], t.warnings, t.sep), (['A'], [], '|-|:-:|'))
        text = HEAD + '| A | a | on | w | 11-23 |\n| Thing | State now |\n|---|---|\n| B | off |\n'
        a, b = facts.parse(text, 'S.md')                 # the second header row ends the first table
        self.assertEqual(([r.thing for r in a.rows], [r.thing for r in b.rows]), (['A'], ['B']))
        self.assertEqual((a.warnings, b.warnings, b.columns), ([], [], ['Thing', 'State now']))

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

    def test_the_index_lists_every_thing_and_caps_with_no_empty_file_lines(self):
        found = self.found(self.place)
        self.assertEqual(facts.index(found), [f'{os.path.join(self.place, "STATE.md")} (3 rows):',
                                              '  - Example GPU', '  - Example backup job', '  - Example web cache'])
        self.assertEqual(facts.index(found, 2)[-1], '  +1 more: run facts --index')
        self.assertEqual(len(facts.index(found, 2)), 4)
        second = write(os.path.join(self.tmp, 'second', 'STATE.md'), HEAD + '| Z | z | on | w | 11-23 |\n')
        lines = facts.index(self.found(self.place, second), 3)
        self.assertNotIn(f'{second} (1 rows):', lines)               # none of its things fit: no line for it
        self.assertEqual(lines[-1], '  +1 more: run facts --index')

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

    def test_a_fifo_is_never_opened_to_wait(self):
        folder = os.path.join(self.tmp, 'fifo')
        os.makedirs(folder)
        os.mkfifo(os.path.join(folder, 'STATE.md'))
        os.mkfifo(os.path.join(self.tmp, 'table.fifo'))
        start = time.monotonic()
        a, b = self.found(folder, os.path.join(self.tmp, 'table.fifo'))
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual((a.rows(), b.rows()), ([], []))
        self.assertIn('not a regular file: skipped', a.notes[0])
        self.assertIn('not a regular file: skipped', b.notes[0])

    def test_bytes_that_are_not_utf8_and_a_cut_file(self):
        path = write(os.path.join(self.tmp, 'bin', 'STATE.md'),
                     HEAD.encode() + b'| Caf\xe9 \xff thing | a | on | w | 11-23 |\n', 'wb')
        [p] = self.found(path)
        self.assertEqual(len(p.rows()), 1)
        self.assertIn('thing', p.rows()[0].thing)
        self.assertEqual(p.notes, [])
        old = facts.MAX_READ
        facts.MAX_READ = 200
        self.addCleanup(setattr, facts, 'MAX_READ', old)
        long_table = HEAD + ''.join(f'| Thing {k} | a | on | w | 11-23 |\n' for k in range(20))
        [p] = self.found(write(os.path.join(self.tmp, 'long', 'STATE.md'), long_table))
        self.assertTrue(0 < len(p.rows()) < 20)
        self.assertIn('(cut at 200 bytes)', p.notes[0])

    def test_updated_dates(self):
        self.assertEqual(facts.updated('11-23', TODAY), datetime.date(2026, 11, 23))
        self.assertEqual(facts.updated('2025-02-03', TODAY), datetime.date(2025, 2, 3))
        self.assertEqual(facts.updated(' 2026-11-01 (moved) ', TODAY), datetime.date(2026, 11, 1))
        self.assertEqual(facts.updated('11-20 (checked)', TODAY), datetime.date(2026, 11, 20))
        new_year = datetime.date(2027, 1, 2)
        self.assertEqual(facts.updated('12-30', new_year), datetime.date(2026, 12, 30))   # across the year's end
        self.assertEqual(facts.updated('01-03', new_year), datetime.date(2027, 1, 3))     # tomorrow: a time zone
        self.assertEqual(facts.updated('02-29', new_year), datetime.date(2024, 2, 29))    # the last leap year
        self.assertEqual(facts.updated('02-29', datetime.date(2028, 3, 1)), datetime.date(2028, 2, 29))
        for bad in ('', 'soon', '13-40', '2026-02-30', '11-23-5', None):
            self.assertIsNone(facts.updated(bad, TODAY), bad)

    def test_the_gardener_check_reports_stale_future_and_missing_dates_and_big_tables(self):
        found = self.found(self.place)
        self.assertEqual(facts.check(found, 30, 60, TODAY), [])
        out = facts.check(found, 5, 2, TODAY)
        self.assertEqual(len(out), 2, out)
        self.assertIn('3 rows, over the cap of 2 (maxRows)', out[0])
        self.assertIn(':9: "Example web cache": Updated 11-01 is 22 days old (staleDays 5)', out[1])
        write(os.path.join(self.place, 'STATE.md'),
              HEAD + '| A | a | on | w | soon |\n| B | b | c |\n| C | c | on | w | 2027-01-05 |\n')
        out = facts.check(self.found(self.place, os.path.join(self.tmp, 'gone')), 30, 60, TODAY)
        self.assertEqual(len(out), 5, out)
        self.assertIn('not there', out[0])
        self.assertIn('row "B" has 3 cells', out[1])
        self.assertIn('"A": no date in Updated (\'soon\')', out[2])
        self.assertIn('"B": no date in Updated (\'\')', out[3])
        self.assertIn('"C": Updated 2027-01-05 is in the future', out[4])
        write(os.path.join(self.place, 'STATE.md'), '| Thing | State now |\n|---|---|\n| A | on |\n')
        self.assertEqual(facts.check(self.found(self.place), 30, 60, TODAY)[0].split(': ', 1)[1],
                         'the table has no "Updated" column')

    def test_the_session_text_is_capped_as_a_whole(self):
        found = [facts.read_place(write(os.path.join(self.tmp, f't{k:02}', 'STATE.md'), HEAD + ''.join(
            f'| A rather long name of example thing {k}-{j} | a | on | w | 11-23 |\n' for j in range(10))), 'test')
            for k in range(30)]
        projects = [{'match': '/x', 'text': 'Project rule. ' * 400}]
        text = facts.session_text(found, projects, 1000, 4000)
        self.assertLessEqual(len(text), 4000)
        lines = text.split('\n')
        more = [ln for ln in lines if ln.startswith('  +')]
        self.assertEqual(len(more), 1, more)
        shown = sum(ln.startswith('  - ') for ln in lines)
        self.assertEqual(more[0], f'  +{300 - shown} more: run facts --index')
        before = lines[lines.index(more[0]) - 1]
        self.assertTrue(before.startswith('  - '), before)          # no file line left with none of its things
        self.assertIn('Project rule.', text)
        self.assertLessEqual(len(facts.compact_text(found, projects, 500)), 500)


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

    def fake_git(self, body):
        path = script(os.path.join(self.tmp, 'fake-bin', 'git'), body)
        old = facts.GIT
        facts.GIT = path
        self.addCleanup(setattr, facts, 'GIT', old)
        return path

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

    def test_a_git_that_hangs_or_fails_matches_no_remote_project(self):
        self.fake_git('exec sleep 30')
        start = time.monotonic()
        self.assertEqual(facts.origin(self.repo, timeout=0.5), '')
        self.assertLess(time.monotonic() - start, 5)
        self.fake_git('echo https://github.com/example/project; exit 1')
        self.assertEqual(self.entries(self.repo), ['/m'])
        self.fake_git('exit 0')
        self.assertEqual(self.entries(self.repo), ['/m'])

    def test_git_is_asked_once_per_hook_answer(self):
        count = os.path.join(self.tmp, 'count')
        self.fake_git(f'echo x >> {count}; echo https://github.com/example/project.git')
        place = os.path.join(self.tmp, 'p')
        write(os.path.join(place, 'STATE.md'), HEAD + '| A | a | on | w | 11-23 |\n')
        self.cfg['dirs'] = []
        self.cfg['projects'][0]['dirs'] = [place]
        self.cfg['projects'].append({'match': 'github.com/example/project', 'text': 'second text'})
        hook = fx.load_hook(HOOK)
        for event in ('SessionStart', 'PreCompact'):
            write(count, '')
            out, text = hook.decide({'hook_event_name': event, 'cwd': self.repo}, self.cfg)
            with open(count) as f:
                self.assertEqual(f.read(), 'x\n', event)
            self.assertIn('second text', json.dumps(out) if out else text)

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

    def test_one_place_by_its_real_path_is_read_once(self):
        real = os.path.join(self.tmp, 'facts')
        os.makedirs(real)
        os.symlink(real, os.path.join(self.tmp, 'link'))
        self.cfg['dirs'] = [real, real + '/', os.path.join(self.tmp, 'link'), os.path.join(self.tmp, 'x', '..',
                                                                                             'facts')]
        self.cfg['projects'] = [{'match': self.repo, 'dirs': [real]}]
        self.assertEqual(self.entries(self.repo), [real])

    def test_project_names_for_the_record_repo(self):
        self.assertEqual(facts.project_name({'match': 'github.com/example/project'}), 'project')
        self.assertEqual(facts.project_name({'match': '/srv/example/notes/'}), 'notes')
        self.assertEqual(facts.project_name({'match': 'github.com/example/project', 'name': 'own'}), 'own')
        # follow-up: a remote's .git is dropped; a match that ends in "." or ".." names no folder
        self.assertEqual(facts.project_name({'match': 'github.com/example/project.git'}), 'project')
        self.assertEqual(facts.project_name({'match': '/srv/example/notes.git'}), 'notes.git')
        for bad in ('/srv/example/..', '/srv/example/.', '/', 'github.com/example/.git'):
            self.assertIsNone(facts.project_name({'match': bad}), bad)
        recs = facts.records({'records': [{'path': self.tmp}]}, [{'match': '/srv/example/..'}])
        self.assertEqual(recs[0].folders, ['machines', 'orch', 'shared'])


class Command(unittest.TestCase):
    """bin/facts and bin/facts-hook, run as programs with the test's machine file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='facts-command-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.place = os.path.join(self.tmp, 'facts')
        write(os.path.join(self.place, 'STATE.md'), TABLE)
        self.configure(dirs=[self.place])
        self.env = dict(os.environ, HOME=self.tmp, FACTS_TODAY=TODAY.isoformat())
        self.env.pop('FACTS_DIRS', None)

    def configure(self, **part):
        self.machine = fx.machine_file(self.tmp, {'facts': part})

    def facts(self, *args, env=None, cwd=None):
        return fx.run_hook('facts', '', self.machine, env=env or self.env, cwd=cwd or self.tmp, args=args,
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
        for args in ((), ('--index', 'gpu'), ('--bogus',), ('--index', '--check'), ('--records',),
                     ('--records', '--no-records', 'x'), ('--check', 'x')):
            self.assertEqual(self.facts(*args).returncode, 1, args)

    def test_check_reports_and_exits_1(self):
        r = self.facts('--check')
        self.assertEqual((r.returncode, r.stdout.strip()), (0, 'facts check: OK (1 table file(s), 3 rows)'))
        r = self.facts('--check', env=dict(self.env, FACTS_TODAY='2027-01-30'))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(r.stdout.count('days old'), 3, r.stdout)
        write(os.path.join(self.place, 'STATE.md'), HEAD + '| Old thing | a | on | w | 2000-01-01 |\n')
        r = self.facts('--check')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('"Old thing": Updated 2000-01-01 is', r.stdout)
        self.assertIn('facts check: 1 finding(s)', r.stdout)

    def test_check_reads_this_folders_project_only_unless_all(self):
        mine, theirs = os.path.join(self.tmp, 'mine'), os.path.join(self.tmp, 'theirs')
        write(os.path.join(mine, 'facts', 'STATE.md'), HEAD + '| Mine | a | on | w | 11-23 |\n')
        write(os.path.join(theirs, 'facts', 'STATE.md'), HEAD + '| Private thing | a | on | w | 2000-01-01 |\n')
        self.configure(projects=[{'match': mine, 'dirs': [os.path.join(mine, 'facts')]},
                                 {'match': theirs, 'dirs': [os.path.join(theirs, 'facts')]}])
        r = self.facts('--check', cwd=mine)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn('Private', r.stdout)
        r = self.facts('--check', '--all', cwd=mine)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('"Private thing"', r.stdout)

    def test_a_broken_part_is_named_and_the_defaults_used(self):
        self.machine = fx.machine_file(self.tmp, {'facts': {'dirs': 'not a list', 'staleDays': 0}})
        r = self.facts('gpu')
        self.assertIn('no state table is configured', r.stdout)
        self.assertIn('built-in defaults used', r.stdout)
        r = self.hook('SessionStart', source='startup')
        self.assertEqual(r.returncode, 0)
        self.assertNotIn('additionalContext', r.stdout)
        self.assertIn('built-in defaults used', json.loads(r.stdout)['systemMessage'])

    def test_a_broken_key_beside_good_dirs_gives_the_tables_and_the_note(self):
        self.machine = fx.machine_file(self.tmp, {'facts': {'dirs': [self.place], 'staleDays': 0}})
        r = self.hook('SessionStart', source='startup')
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertIn('  - Example GPU', out['hookSpecificOutput']['additionalContext'])
        self.assertIn('staleDays', out['systemMessage'])
        self.assertIn('staleDays', r.stderr)

    def test_a_bad_project_or_record_row_is_left_out_the_others_kept(self):
        fx.use_machine(fx.machine_file(self.tmp, {'facts': {
            'projects': [{'match': 'github.com/a/b', 'dirs': ['/x']}, {'match': 'not a remote'},
                         {'match': '/d', 'extra': 1}, {'match': '/e', 'name': '../up'}],
            'records': [{'path': '/r'}, {'path': 'relative'}, {'path': '/r2', 'shared': ['../etc']},
                        {'path': '/r3', 'projectsDir': 'a/b'}, {'path': '/r4', 'extra': 1}]}}))
        self.addCleanup(fx.use_machine, fx.NO_MACHINE)
        got, problem = config.load('facts')
        self.assertEqual(got['projects'], [{'match': 'github.com/a/b', 'dirs': ['/x']}])
        self.assertEqual(got['records'], [{'path': '/r'}])
        for k in (1, 2, 3):
            self.assertIn(f'facts.projects: entry {k}', problem)
        for k in (1, 2, 3, 4):
            self.assertIn(f'facts.records: entry {k}', problem)

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
        self.assertIn('  +2 more: run facts --index', text)
        self.configure(dirs=[self.place], indexChars=300)
        text = json.loads(self.hook('SessionStart').stdout)['hookSpecificOutput']['additionalContext']
        self.assertLessEqual(len(text), 300)
        self.assertIn('more: run facts --index', text)

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

    def test_the_hook_with_a_git_that_hangs_still_answers_with_the_machine_tables(self):
        fake = os.path.join(self.tmp, 'fake-bin')
        script(os.path.join(fake, 'git'), 'exec sleep 30')
        self.configure(dirs=[self.place], projects=[{'match': 'github.com/example/project', 'text': 'Remote rule.'}])
        env = dict(self.env, PATH=f'{fake}:{os.environ.get("PATH", "/usr/bin:/bin")}')
        start = time.monotonic()
        r = fx.run_hook(HOOK, {'hook_event_name': 'SessionStart', 'cwd': self.tmp}, self.machine, env=env)
        self.assertLess(time.monotonic() - start, 10)
        text = json.loads(r.stdout)['hookSpecificOutput']['additionalContext']
        self.assertIn('  - Example GPU', text)
        self.assertNotIn('Remote rule.', text)

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


class RecordRepo(unittest.TestCase):
    """`facts WORDS` after the tables: the record repo's text files (this project's folder and the shared ones)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='facts-records-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.place = os.path.join(self.tmp, 'facts')
        write(os.path.join(self.place, 'STATE.md'), TABLE)
        self.proj = os.path.join(self.tmp, 'work', 'proj')
        os.makedirs(self.proj)
        r = self.records = os.path.join(self.tmp, 'records')
        write(f'{r}/projects/proj/notes.md', '# Notes\nThe example GPU fan curve: gpu-fan.sh\n')
        write(f'{r}/projects/other/notes.md', 'Other project: gpu private detail\n')
        write(f'{r}/machines/box/STATE.md', TABLE)                       # a mirrored copy of the table
        write(f'{r}/shared/runbook.txt', 'Example GPU: switch it on with gpu on\n')
        write(f'{r}/shared/image.png', 'gpu in a file of another type\n')
        write(f'{r}/shared/.git/notes.md', 'gpu inside .git\n')
        write(f'{r}/orch/big.md', 'gpu\n' + 'x' * (facts.RECORD_MAX + 10))
        os.mkfifo(f'{r}/orch/pipe.md')
        os.symlink(f'{r}/projects/other/notes.md', f'{r}/shared/link.md')
        self.configure()
        self.env = dict(os.environ, HOME=self.tmp, FACTS_TODAY=TODAY.isoformat())
        self.env.pop('FACTS_DIRS', None)

    def configure(self, records=None):
        self.machine = fx.machine_file(self.tmp, {'facts': {
            'dirs': [self.place], 'projects': [{'match': self.proj, 'name': 'proj'}],
            'records': [{'path': self.records}] if records is None else records}})

    def facts(self, *args, cwd=None):
        return fx.run_hook('facts', '', self.machine, env=self.env, cwd=cwd or self.proj, args=args,
                           hook=os.path.join(fx.BIN, 'facts'))

    def test_tables_first_then_this_projects_and_the_shared_record_files(self):
        r = self.facts('gpu')
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        self.assertLess(out.index('| Example GPU |'), out.index(f'Records in {self.records}:'))
        self.assertIn('projects/proj/notes.md:2: The example GPU fan curve: gpu-fan.sh', out)
        self.assertIn('shared/runbook.txt:1: Example GPU: switch it on with gpu on', out)
        for absent in ('projects/other', 'machines/box/STATE.md', 'image.png', '.git', 'big.md', 'pipe.md',
                       'link.md'):
            self.assertNotIn(absent, out)
        self.assertEqual(out.count('| Example GPU |'), 1)                # the mirrored table is not shown again
        r = self.facts('gpu', '--all')
        self.assertIn('projects/other/notes.md:1: Other project: gpu private detail', r.stdout)
        r = self.facts('gpu', '--no-records')
        self.assertNotIn('Records in', r.stdout)

    def test_hits_are_capped_and_lines_trimmed(self):
        write(f'{self.records}/shared/many.md', ''.join(f'gpu line {k} ' + 'y' * 300 + '\n' for k in range(70)))
        r = self.facts('gpu')
        hits = [ln for ln in r.stdout.split('\n') if ':' in ln and ('many.md' in ln or 'notes.md' in ln
                                                                    or 'runbook' in ln)]
        self.assertEqual(len(hits), 10, r.stdout)
        self.assertTrue(all(len(ln) <= 160 for ln in hits))
        self.assertIn('+62 more (facts --records gpu)', r.stdout)
        r = self.facts('--records', 'gpu')
        self.assertNotIn('| Example GPU |', r.stdout)
        self.assertEqual(r.stdout.count('many.md:'), 50)
        self.assertIn('+22 more (facts --records gpu)', r.stdout)

    def test_a_missing_record_repo_is_one_note_in_the_command_and_silent_in_the_hook(self):
        self.configure(records=[{'path': os.path.join(self.tmp, 'gone')}])
        r = self.facts('gpu')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('| Example GPU |', r.stdout)
        self.assertEqual([ln for ln in r.stdout.split('\n') if 'gone' in ln],
                         [f'note: record repo {self.tmp}/gone: not there (or not a folder): skipped'])
        r = fx.run_hook(HOOK, {'hook_event_name': 'SessionStart', 'cwd': self.proj}, self.machine, env=self.env)
        text = json.loads(r.stdout)['hookSpecificOutput']['additionalContext']
        self.assertNotIn('Records', text)
        self.assertNotIn('gone', r.stdout + r.stderr)

    def test_the_hook_names_the_record_repo_in_one_line_and_never_its_content(self):
        r = fx.run_hook(HOOK, {'hook_event_name': 'SessionStart', 'cwd': self.proj}, self.machine, env=self.env)
        text = json.loads(r.stdout)['hookSpecificOutput']['additionalContext']
        self.assertEqual([ln for ln in text.split('\n') if 'Records' in ln],
                         [f'Records: {self.records} (facts <words> searches it)'])
        self.assertNotIn('fan curve', text)
        os.unlink(os.path.join(self.place, 'STATE.md'))                  # no table: the line alone
        r = fx.run_hook(HOOK, {'hook_event_name': 'SessionStart', 'cwd': self.proj}, self.machine, env=self.env)
        self.assertEqual(json.loads(r.stdout)['hookSpecificOutput']['additionalContext'],
                         f'Records: {self.records} (facts <words> searches it)')

    def test_check_never_reads_the_record_repo(self):
        write(f'{self.records}/shared/STATE.md', HEAD + '| Stale record | a | on | w | 2000-01-01 |\n')
        r = self.facts('--check', '--all')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn('record', r.stdout.lower())

    def test_dirs_lists_the_record_folders(self):
        r = self.facts('--dirs')
        self.assertIn(f'{self.records} (record repo): searched: machines, orch, shared, projects/proj', r.stdout)

    def test_a_mirrored_table_is_skipped_by_its_bytes(self):
        found = [facts.read_place(self.place, 'test')]
        recs = facts.records({'records': [{'path': self.records}]}, [], every=True)
        skip = {found[0].digest: found[0].file}
        self.assertEqual(found[0].digest, hashlib.sha256(TABLE.encode()).hexdigest())
        lines, notes, copies = facts.search_records(recs, ['backup'], skip)
        self.assertEqual((lines, notes), ([], []))
        self.assertEqual(copies, [(f'{self.records}/machines/box/STATE.md', found[0].file)])
        lines, _, copies = facts.search_records(recs, ['backup'], {})
        self.assertIn('machines/box/STATE.md:', lines[1])
        self.assertEqual(copies, [])

    def test_records_only_a_copy_holds_the_words_and_says_which(self):
        r = self.facts('--records', 'backup')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('no line holds all of', r.stdout)
        self.assertIn('Records: only copies of state tables hold all of: backup; their rows: '
                      '`facts --no-records backup`', r.stdout)
        self.assertIn(f'  {self.records}/machines/box/STATE.md (a copy of {self.place}/STATE.md)', r.stdout)
        r = self.facts('--records', 'nothing-like-this')
        self.assertIn('Records: no line holds all of: nothing-like-this', r.stdout)

    def test_records_with_no_record_repo_configured_is_a_note(self):
        self.configure(records=[])
        r = self.facts('--records', 'gpu')
        self.assertEqual((r.returncode, r.stdout.strip()),
                         (0, 'note: no record repo is configured (machine file "agent-guards" part "facts": records)'))
        os.unlink(os.path.join(self.place, 'STATE.md'))
        self.assertEqual(self.facts('--records', 'gpu').returncode, 0)

    def test_a_folder_that_leads_out_of_the_repo_is_not_searched(self):
        outside = os.path.join(self.tmp, 'outside')
        write(f'{outside}/proj/secret.md', 'gpu outside the repo\n')
        write(f'{outside}/elsewhere.md', 'gpu outside too\n')
        os.rename(f'{self.records}/projects', f'{self.tmp}/old-projects')
        os.symlink(outside, f'{self.records}/projects')           # projectsDir is a link out of the repo
        os.symlink(outside, f'{self.records}/orch-link')
        recs = facts.records({'records': [{'path': self.records, 'shared': ['shared', 'orch-link']}]},
                             [{'match': self.proj, 'name': 'proj'}])
        lines, notes, _ = facts.search_records(recs, ['gpu'])
        self.assertTrue(lines and notes == [], (lines, notes))
        self.assertNotIn('outside', '\n'.join(lines))
        self.assertIn('shared/runbook.txt:1:', '\n'.join(lines))
        linked = os.path.join(self.tmp, 'linked-records')             # the repo itself reached by a link: searched
        os.symlink(self.records, linked)
        lines, _, _ = facts.search_records(facts.records({'records': [{'path': linked}]}, []), ['gpu'])
        self.assertIn('shared/runbook.txt:1:', '\n'.join(lines))

    def test_a_file_with_a_nul_byte_is_not_text(self):
        write(f'{self.records}/shared/blob.json', b'gpu\x00binary\n', 'wb')
        lines, _, _ = facts.search_records(facts.records({'records': [{'path': self.records}]}, []), ['gpu'])
        self.assertNotIn('blob.json', '\n'.join(lines))
        self.assertIn('runbook.txt', '\n'.join(lines))

    def test_the_time_limit_is_checked_for_each_folder_too(self):
        for k in range(30):
            os.makedirs(f'{self.records}/shared/empty/{k:02}')    # folders with no files: no file-level check
        seen = []
        real_walk = os.walk

        def walk(top, *a, **kw):
            for d, dirs, files in real_walk(top, *a, **kw):
                seen.append(d)
                yield d, dirs, files
        clock = iter(range(0, 10 ** 6, 1))
        old_walk, old_time = facts.os.walk, facts.time.monotonic
        facts.os.walk, facts.time.monotonic = walk, lambda: next(clock)
        try:
            recs = facts.records({'records': [{'path': self.records, 'shared': ['shared']}]}, [])
            _lines, notes, _ = facts.search_records(recs, ['gpu'], seconds=5)
        finally:
            facts.os.walk, facts.time.monotonic = old_walk, old_time
        self.assertLess(len(seen), 10, seen)
        self.assertIn('search stopped after 5 s', notes[0])


if __name__ == '__main__':
    unittest.main()
