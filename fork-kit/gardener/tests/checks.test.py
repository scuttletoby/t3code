"""The general checks shipped with the kit, each run the way the runner runs it (the contract context on stdin) on
fixture files, temporary git repositories, a fake `gh` and a fake `facts`. Every line they print must be a finding.
No network, no root.

  python3 -I -B fork-kit/gardener/tests/checks.test.py
"""
import hashlib, json, os, subprocess, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gardener_fixture as gf  # noqa: E402
from gardener_fixture import contract  # noqa: E402

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='t@example.invalid', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')


class Check(gf.Temp):

    env = {}

    def run_check(self, name, options=None, fix=False, repo='example-owner/app', gh='gh', code=0):
        ctx = {'version': contract.VERSION, 'project': {'name': 'example', 'repo': repo, 'root': self.root},
               'check': name, 'options': options or {}, 'fix': fix, 'today': '2026-11-23', 'gh': gh}
        r = subprocess.run([sys.executable, '-I', '-B', os.path.join(gf.CHECKS, name)], input=json.dumps(ctx),
                           capture_output=True, text=True, cwd=self.root, timeout=120,
                           env=dict(os.environ, **self.env))
        self.assertEqual(r.returncode, code, r.stderr)
        found = [contract.parse_finding(ln) for ln in r.stdout.splitlines() if ln.strip()]
        self.last_stderr = r.stderr
        return found

    def ids(self, found):
        return sorted(f['id'] for f in found)

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)
        return path

    def git(self, repo, *args):
        return subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True, text=True,
                              env=GIT_ENV).stdout.strip()


class ContextBudget(Check):

    def test_no_options_no_findings(self):
        self.assertEqual(self.run_check('context-budget'), [])

    def test_files_over_near_and_missing(self):
        self.write('AGENTS.md', 'x' * 1100)
        self.write('CLAUDE.md', 'x' * 900)
        self.write('small.md', 'x' * 10)
        found = self.run_check('context-budget', {'files': {'AGENTS.md': 1000, 'CLAUDE.md': 1000,
                                                            'small.md': 1000, 'gone.md': 1000}})
        by = {f['where']: f['id'] for f in found}
        self.assertEqual(by, {'AGENTS.md': 'context-budget.over', 'CLAUDE.md': 'context-budget.near',
                              'gone.md': 'context-budget.missing'})

    def test_memory_index(self):
        self.write('mem/MEMORY.md', '- [A](a.md) — ok\n- [Gone](gone.md) — x\n- ' + 'y' * 200 + '\n')
        self.write('mem/a.md', 'a')
        self.write('mem/orphan.md', 'o')
        found = self.run_check('context-budget', {'memoryIndexes': ['mem/MEMORY.md'], 'indexMaxChars': 100})
        self.assertEqual(self.ids(found), ['context-budget.long-line', 'context-budget.missing-memory',
                                           'context-budget.orphan-memory', 'context-budget.over'])

    def test_skills(self):
        self.write('skills/big/SKILL.md', 'x' * 9000)
        self.write('skills/ok/SKILL.md', 'x' * 10)
        found = self.run_check('context-budget', {'skillDirs': ['skills']})
        self.assertEqual([(f['id'], f['where']) for f in found],
                         [('context-budget.over', 'skills/big/SKILL.md')])

    def test_a_skill_file_that_is_a_broken_link_is_missing(self):
        self.write('skills/ok/SKILL.md', 'x' * 10)
        os.makedirs(os.path.join(self.root, 'skills', 'gone'))
        os.symlink(os.path.join(self.tmp, 'nowhere.md'), os.path.join(self.root, 'skills', 'gone', 'SKILL.md'))
        found = self.run_check('context-budget', {'skillDirs': ['skills']})
        self.assertEqual([(f['id'], f['where']) for f in found],
                         [('context-budget.missing', 'skills/gone/SKILL.md')])


class Facts(Check):

    def fake_facts(self, out, code):
        return self.script('facts', f'''
            import sys
            assert sys.argv[1:] == ['--check', '--no-records'], sys.argv
            sys.stdout.write({out!r})
            sys.exit({code})
        ''')

    def test_not_installed_is_a_note(self):
        found = self.run_check('facts', {'command': os.path.join(self.tmp, 'no-facts')})
        self.assertEqual(self.ids(found), ['facts.not-installed'])

    def test_problem_lines_become_findings_and_summaries_are_skipped(self):
        facts = self.fake_facts('facts check: facts/STATE.md: row "Disk": 40 days old\n'
                                'facts check: facts/STATE.md: a row without a "Thing" cell\n'
                                'unrelated line\nfacts check: 2 finding(s)\n', 1)
        found = self.run_check('facts', {'command': facts})
        self.assertEqual([(f['id'], f['where']) for f in found],
                         [('facts.stale', 'facts/STATE.md'), ('facts.table', 'facts/STATE.md')])
        facts = self.fake_facts('facts check: OK (3 tables)\n', 0)
        self.assertEqual(self.run_check('facts', {'command': facts}), [])

    def test_a_facts_crash_fails_the_check(self):
        facts = self.fake_facts('Traceback ...\n', 2)
        self.run_check('facts', {'command': facts}, code=1)
        self.assertIn('exited with 2', self.last_stderr)


class Worktrees(Check):

    def setUp(self):
        super().setUp()
        self.git(self.root, 'init', '-q', '-b', 'main')
        self.write('a.txt', 'a')
        self.git(self.root, 'add', '.')
        self.git(self.root, 'commit', '-q', '-m', 'one')

    def branch_with_commit(self, name, date=None):
        self.git(self.root, 'checkout', '-q', '-b', name)
        self.write(f'{name}.txt', name)
        self.git(self.root, 'add', '.')
        env = dict(GIT_ENV, GIT_AUTHOR_DATE=date or '', GIT_COMMITTER_DATE=date or '') if date else GIT_ENV
        subprocess.run(['git', '-C', self.root, 'commit', '-q', '-m', name], check=True, env=env)
        self.git(self.root, 'checkout', '-q', 'main')

    def test_clean_repo_no_findings(self):
        self.assertEqual(self.run_check('worktrees'), [])

    def merged(self, name):
        """A branch with a commit of its own, merged into main (fast-forward)."""
        self.branch_with_commit(name)
        self.git(self.root, 'merge', '-q', '--ff-only', name)

    def test_merged_stale_prunable_and_branches(self):
        self.merged('merged-branch')
        self.branch_with_commit('old-work', '2026-09-01T12:00:00+00:00')
        self.branch_with_commit('fresh-work')
        self.merged('merged-no-tree')
        self.git(self.root, 'branch', 'keep-me')
        self.git(self.root, 'branch', 'gone-tree')
        trees = os.path.join(self.tmp, 'trees')
        for b in ('merged-branch', 'old-work', 'fresh-work', 'gone-tree'):
            self.git(self.root, 'worktree', 'add', '-q', os.path.join(trees, b), b)
        import shutil
        shutil.rmtree(os.path.join(trees, 'gone-tree'))
        found = self.run_check('worktrees', {'ignore': ['keep-*'], 'staleDays': 14})
        got = sorted((f['id'], f['text'].split(':')[0]) for f in found)
        self.assertEqual(got, [('branches.merged', 'branch merged-no-tree'),
                               ('worktrees.merged', 'worktree of branch merged-branch'),
                               ('worktrees.prunable', 'worktree record whose folder is gone'),
                               ('worktrees.stale', 'worktree of branch old-work')])
        self.assertTrue(os.path.isdir(os.path.join(trees, 'merged-branch')))      # report only

    def test_fix_prunes_only_records_of_gone_folders(self):
        trees = os.path.join(self.tmp, 'trees')
        self.git(self.root, 'branch', 'gone-tree')
        self.merged('merged-branch')
        self.git(self.root, 'worktree', 'add', '-q', os.path.join(trees, 'gone-tree'), 'gone-tree')
        self.git(self.root, 'worktree', 'add', '-q', os.path.join(trees, 'merged-branch'), 'merged-branch')
        import shutil
        shutil.rmtree(os.path.join(trees, 'gone-tree'))
        found = self.run_check('worktrees', fix=True)
        self.assertIn('worktrees.pruned', self.ids(found))
        self.assertIn('worktrees.merged', self.ids(found))
        listed = self.git(self.root, 'worktree', 'list', '--porcelain')
        self.assertNotIn('gone-tree', listed)
        self.assertIn('merged-branch', listed)                          # a merged worktree is never removed
        self.assertIn('gone-tree', self.git(self.root, 'branch', '--list'))      # nor a branch

    def test_a_fresh_worktree_is_not_merged(self):
        """A branch just made from main (a worktree just started) has its tip in main but no work of its own: not
        merged. Made longer ago than staleDays it is a note."""
        trees = os.path.join(self.tmp, 'trees')
        self.git(self.root, 'worktree', 'add', '-q', '-b', 'just-started', os.path.join(trees, 'just-started'))
        self.git(self.root, 'branch', 'new-branch')
        self.assertEqual(self.run_check('worktrees'), [])
        old = dict(GIT_ENV, GIT_COMMITTER_DATE='2026-09-01T12:00:00+00:00')
        subprocess.run(['git', '-C', self.root, 'worktree', 'add', '-q', '-b', 'old-start',
                        os.path.join(trees, 'old-start')], check=True, capture_output=True, env=old)
        subprocess.run(['git', '-C', self.root, 'branch', 'old-branch'], check=True, env=old)
        found = self.run_check('worktrees', {'staleDays': 14})
        self.assertEqual(sorted((f['id'], f['text']) for f in found),
                         [('branches.no-commits', 'branch old-branch: no commits of its own, made 83 days ago'),
                          ('worktrees.no-commits',
                           'worktree of branch old-start: no commits of its own, made 83 days ago')])

    def test_a_worktree_made_from_origin_main_is_not_merged_until_pushed(self):
        """`git worktree add -b NAME PATH origin/main` (how agents start work) sets origin/main as the branch's
        upstream: that is not work of its own. Pushed under its own name, it is."""
        origin = os.path.join(self.tmp, 'origin.git')
        self.git(self.tmp, 'init', '-q', '--bare', origin)
        self.git(self.root, 'remote', 'add', 'origin', origin)
        self.git(self.root, 'push', '-q', 'origin', 'main')
        self.git(self.root, 'fetch', '-q', 'origin')
        self.git(self.root, 'worktree', 'add', '-q', '-b', 'fresh', os.path.join(self.tmp, 'trees', 'fresh'),
                 'origin/main')
        self.assertEqual(self.git(self.root, 'config', 'branch.fresh.merge').strip(), 'refs/heads/main')
        self.assertEqual(self.run_check('worktrees'), [])
        self.git(self.root, 'branch', 'pushed', 'main')
        self.git(self.root, 'push', '-q', '-u', 'origin', 'pushed')
        self.assertEqual([(f['id'], f['text']) for f in self.run_check('worktrees')],
                         [('branches.merged', 'branch pushed: merged into origin/main')])

    def test_a_repos_option_under_home_stays_expandable_in_hints(self):
        """A repos option given as ~/... is quoted after its ~/, so the hint still works when pasted."""
        import shlex
        home = os.path.join(self.tmp, 'home')
        os.makedirs(home)
        os.rename(self.root, os.path.join(home, 'my repo'))
        self.root = os.path.join(home, 'my repo')
        self.merged('plain')
        with mock.patch.dict(os.environ, {'HOME': home}):
            found = {f['id']: f for f in self.run_check('worktrees', {'repos': ['~/my repo']})}
        fix = found['branches.merged']['fix']
        self.assertTrue(fix.startswith("git -C ~/'my repo' branch -d plain"), fix)
        self.assertEqual(shlex.split(fix)[2], '~/my repo')

    def test_fix_hints_quote_names(self):
        """Git allows $, ;, ( and quotes in branch names: a hint is a command a person (or later an agent) may run,
        so every name in it is quoted."""
        import shlex
        name = "a;b$(c)'d`e`"
        self.merged(name)
        trees = os.path.join(self.tmp, 'my trees')
        self.git(self.root, 'worktree', 'add', '-q', os.path.join(trees, 'x'), name)
        self.merged('plain')
        found = {f['id']: f for f in self.run_check('worktrees')}
        self.assertEqual(shlex.split(found['worktrees.merged']['fix']),
                         ['git', '-C', self.root, 'worktree', 'remove', os.path.join(trees, 'x'), '&&', 'git', '-C',
                          self.root, 'branch', '-d', name])
        self.assertEqual(shlex.split(found['branches.merged']['fix']),
                         ['git', '-C', self.root, 'branch', '-d', 'plain'])

    def test_missing_and_not_a_repo(self):
        os.makedirs(os.path.join(self.tmp, 'plain'))
        found = self.run_check('worktrees', {'repos': [os.path.join(self.tmp, 'plain'),
                                                       os.path.join(self.tmp, 'none')]})
        self.assertEqual(self.ids(found), ['worktrees.not-a-repo', 'worktrees.repo-missing'])

    def test_an_empty_repo_with_no_commits(self):
        empty = os.path.join(self.tmp, 'empty')
        os.makedirs(empty)
        self.git(empty, 'init', '-q', '-b', 'main')
        self.assertEqual(self.run_check('worktrees', {'repos': [empty]}), [])


class WorkflowsOff(Check):

    def fake_gh(self, workflows, code=0):
        return self.script('gh', f'''
            import json, sys
            assert sys.argv[1:] == ['api', 'repos/example-owner/app/actions/workflows?per_page=100'], sys.argv
            if {code}:
                sys.stderr.write('HTTP 403: API rate limit exceeded\\n')
                sys.exit({code})
            print(json.dumps({{"total_count": 3, "workflows": {workflows!r}}}))
        ''')

    def test_off_workflows_are_reported_and_ignored_ones_not(self):
        gh = self.fake_gh([{'name': 'CI', 'path': '.github/workflows/ci.yml', 'state': 'active'},
                           {'name': 'Old', 'path': '.github/workflows/old.yml', 'state': 'disabled_manually'},
                           {'name': 'Nightly', 'path': '.github/workflows/nightly.yml',
                            'state': 'disabled_inactivity'},
                           {'name': 'Pages', 'path': 'dynamic/pages/pages-build-deployment', 'state': 'disabled'}])
        found = self.run_check('workflows-off', {'ignore': ['old.yml']}, gh=gh)
        self.assertEqual([(f['id'], f['text']) for f in found],
                         [('workflows.off', 'workflow Nightly is disabled_inactivity')])
        self.assertIn('gh workflow enable nightly.yml -R example-owner/app', found[0]['fix'])

    def test_the_fix_hint_quotes_the_workflow_name(self):
        import shlex
        gh = self.fake_gh([{'name': 'X', 'path': '.github/workflows/$(touch x);y.yml', 'state': 'disabled_manually'}])
        found = self.run_check('workflows-off', gh=gh)
        hint = found[0]['fix'].split(' (or ')[0]
        self.assertEqual(shlex.split(hint), ['gh', 'workflow', 'enable', '$(touch x);y.yml', '-R', 'example-owner/app'])

    def test_a_gh_failure_fails_the_check(self):
        self.run_check('workflows-off', gh=self.fake_gh([], code=1), code=1)
        self.assertIn('rate limit', self.last_stderr)

    def test_no_repo_is_a_note(self):
        self.assertEqual(self.ids(self.run_check('workflows-off', repo='')), ['workflows.no-repo'])


class InstallDrift(Check):

    def setUp(self):
        super().setUp()
        self.prefix = os.path.join(self.tmp, 'host')
        os.makedirs(self.prefix + '/etc/kit-a')
        os.makedirs(self.prefix + '/opt/kit-a/versions/' + 'a' * 40)
        os.symlink('/opt/kit-a/versions/' + 'a' * 40, self.prefix + '/opt/kit-a/current')
        with open(self.prefix + '/opt/kit-a/tool', 'w') as f:
            f.write('tool')
        good = hashlib.sha256(b'tool').hexdigest()
        self.rows = [('/opt/kit-a/current', 'link', '/opt/kit-a/versions/' + 'a' * 40),
                     ('/opt/kit-a/tool', 'file', good)]
        self.record()

    def record(self, extra=()):
        with open(self.prefix + '/etc/kit-a/installed.tsv', 'w') as f:
            f.write('# path\tkind\twant\n')
            for row in list(self.rows) + list(extra):
                f.write('\t'.join(row) + '\n')

        self.env = {'GARDENER_TEST_PREFIX': self.prefix}

    def opts(self, **kw):
        return dict({'etc': '/etc'}, **kw)

    def test_as_installed_no_findings(self):
        self.assertEqual(self.run_check('install-drift', self.opts()), [])

    def test_changed_files_and_links_and_bad_rows(self):
        with open(self.prefix + '/opt/kit-a/tool', 'w') as f:
            f.write('changed by hand')
        self.record([('/opt/kit-a/gone', 'file', '0' * 64), ('a', 'b')])
        found = self.run_check('install-drift', self.opts())
        self.assertEqual(self.ids(found), ['install-drift.changed'])
        self.assertIn('3 installed path(s)', found[0]['text'])
        self.assertEqual(found[0]['severity'], 'error')

    def test_old_release_and_missing_record(self):
        found = self.run_check('install-drift', self.opts(release='b' * 40, kits=['kit-a', 'kit-b']))
        self.assertEqual(self.ids(found), ['install-drift.no-record', 'install-drift.old-release'])
        self.assertEqual(self.run_check('install-drift', self.opts(release='a' * 40)), [])

    def test_gh_apps_rows_have_six_columns(self):
        self.record([('/opt/kit-a/tool', 'file', 'root', 'root', '0644',
                      hashlib.sha256(b'tool').hexdigest())])
        self.assertEqual(self.run_check('install-drift', self.opts()), [])

    def test_no_kits_installed(self):
        self.env = {'GARDENER_TEST_PREFIX': os.path.join(self.tmp, 'none')}
        self.assertEqual(self.run_check('install-drift', {'etc': '/etc'}), [])

    def test_a_prefix_option_is_not_read(self):
        """The folder everything is read under is for tests only, so it is an env var, not a check option."""
        self.env = {}
        found = self.run_check('install-drift', {'etc': '/etc', 'prefix': self.prefix,
                                                 'kits': ['gardener-test-no-such-kit']})
        self.assertEqual(self.ids(found), ['install-drift.no-record'])
        self.env = {'GARDENER_TEST_PREFIX': self.prefix}
        self.assertEqual(self.run_check('install-drift', self.opts(kits=['kit-a'])), [])

    @unittest.skipIf(os.geteuid() == 0, 'root reads everything')
    def test_unreadable_paths_are_a_note_not_drift(self):
        os.makedirs(self.prefix + '/opt/kit-a/private')
        with open(self.prefix + '/opt/kit-a/private/key', 'w') as f:
            f.write('k')
        os.chmod(self.prefix + '/opt/kit-a/private', 0)
        self.addCleanup(os.chmod, self.prefix + '/opt/kit-a/private', 0o755)
        self.record([('/opt/kit-a/private/key', 'file', '0' * 64)])
        found = self.run_check('install-drift', self.opts())
        self.assertEqual(self.ids(found), ['install-drift.unreadable'])


class Contract(unittest.TestCase):

    def test_parse_finding(self):
        f = contract.parse_finding('{"id": "a", "severity": "info", "text": "t"}')
        self.assertEqual(f, {'id': 'a', 'severity': 'info', 'text': 't', 'where': '', 'fix': ''})
        for bad in ('', 'x', '[]', '{"id": "", "severity": "info", "text": "t"}',
                    '{"id": "a b", "severity": "info", "text": "t"}', '{"id": "/home/x", "severity": "info", "text": "t"}',
                    '{"id": "a", "severity": "info", "text": "t", "where": 5}'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    contract.parse_finding(bad)

    def test_a_check_without_a_context_exits_with_a_message(self):
        r = subprocess.run([sys.executable, '-I', '-B', os.path.join(gf.CHECKS, 'worktrees')], input='junk',
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('not a version 1 gardener context', r.stderr)



MUTATIONS = {
    'a finding id is a dotted name': (
        'contract', "    if not ID.fullmatch(obj['id']):", "    if False:", 'Contract.test_parse_finding'),
    'a branch with no work of its own is not merged': (
        'checks/worktrees', "return ('merged', f'merged into {base}') if has else ('empty', days)",
        "return ('merged', f'merged into {base}')", 'Worktrees.test_a_fresh_worktree_is_not_merged'),
    'worktrees: names in hints are quoted': (
        'checks/worktrees', 'q = shlex.quote\n', 'q = str\n', 'Worktrees.test_fix_hints_quote_names'),
    'an upstream that is the base branch is no work of its own': (
        'checks/worktrees', "    if up and up_branch not in (base_branch, 'HEAD'):", '    if up:',
        'Worktrees.test_a_worktree_made_from_origin_main_is_not_merged_until_pushed'),
    'worktrees: a ~/ path stays expandable in hints': (
        'checks/worktrees', "    return '~/' + q(p[2:]) if p.startswith('~/') and len(p) > 2 else q(p)",
        '    return q(p)', 'Worktrees.test_a_repos_option_under_home_stays_expandable_in_hints'),
    'workflows-off: names in hints are quoted': (
        'checks/workflows-off', "f'gh workflow enable {shlex.quote(name)} -R {shlex.quote(repo)}",
        "f'gh workflow enable {name} -R {repo}", 'WorkflowsOff.test_the_fix_hint_quotes_the_workflow_name'),
    'context-budget: a broken skill link is a finding': (
        'checks/context-budget', '        except OSError:                              # a broken link',
        '        except ValueError:                              # a broken link',
        'ContextBudget.test_a_skill_file_that_is_a_broken_link_is_missing'),
    'install-drift: no prefix option': (
        'checks/install-drift', "prefix = os.environ.get('GARDENER_TEST_PREFIX', '')",
        "prefix = opt.get('prefix') or os.environ.get('GARDENER_TEST_PREFIX', '')",
        'InstallDrift.test_a_prefix_option_is_not_read'),
}


class RedCheck(unittest.TestCase):

    def test_each_guard_has_a_test_that_fails_without_it(self):
        gf.red_check(self, os.path.abspath(__file__), MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
