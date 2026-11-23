"""The general checks shipped with the kit, each run the way the runner runs it (the contract context on stdin) on
fixture files, temporary git repositories, a fake `gh` (the GitHub API, GraphQL search too) and a fake `facts`. Every line they print must be a finding.
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

    def run_check(self, name, options=None, fix=False, repo='example-owner/app', gh='gh', code=0, **more):
        ctx = {'version': contract.VERSION, 'project': {'name': 'example', 'repo': repo, 'root': self.root},
               'check': name, 'options': options or {}, 'fix': fix, 'today': '2026-11-23', 'gh': gh, **more}
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

    def test_a_branch_named_like_another_ref_is_read_by_its_full_name(self):
        """refname:short writes `heads/x` when a tag or remote has the short name x too; git then reads
        refs/heads/heads/x, and the whole check failed on a real repository."""
        self.merged('pr7')
        self.git(self.root, 'update-ref', 'refs/remotes/pr7', 'HEAD')
        self.git(self.root, 'tag', 'pr7')
        found = self.run_check('worktrees')
        self.assertEqual([(f['id'], f['text'].split(':')[0]) for f in found], [('branches.merged', 'branch pr7')])

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

SHA = 'a' * 40
QUEUE = {'required_checks': ['ci', 'head_check'], 'merge_method': 'squash', 'bot_logins': ['helper-bot'],
         'lens_author_associations': ['OWNER', 'MEMBER'],
         'override': {'prefix': 'override for head', 'author_association': 'OWNER'},
         'ready_command': 'gh pr merge {pr} --auto --squash', 'push_command': 'git push',
         'lens_format_doc': 'README.md', 'head_check': 'head_check'}
REVIEW = {'version': 1, 'mode': 'enforce',
          'rules': [{'name': 'gate', 'weight': 'critical', 'paths': ['ci/**'], 'lenses': ['merge-path', 'state']},
                    {'name': 'code', 'weight': 'normal', 'paths': ['**'], 'lenses': ['code']}],
          'lenses': {'merge-path': {'prompt': 'l/merge-path.md'}, 'state': {'prompt': 'l/state.md'},
                     'code': {'prompt': 'l/code.md'}},
          'min_lenses': {'critical': 2, 'normal': 1, 'light': 0}, 'findings': True, 'state_line': False,
          'rounds': {'approach_at': 3, 'max': 4, 'approach_lens': 'code'}}
TIERS = {'rules': [{'paths': ['**/*.md'], 'tier': 0}],
         'registry': {'file': 'modules.json', 'rows': 'modules', 'paths': 'paths', 'tier': 'tier', 'default': 2},
         'unmatched': 0,
         'requires': {'2': {'minLenses': 2, 'lenses': [{'name': 'correctness', 'match': 'correct'}]},
                      '3': {'minLenses': 3, 'lenses': [{'name': 'data integrity', 'match': 'data.?integrity',
                                                        'count': 2}]}}}
MODULES = {'modules': [{'id': 'shop', 'paths': ['mods/shop'], 'tier': 3}, {'id': 'chat', 'paths': ['mods/chat']},
                       {'id': 'web', 'paths': 'mods/web', 'tier': 1}]}
MERGED = '2026-11-20T12:00:00Z'


def post(body, when='2026-11-20T11:00:00Z', assoc='OWNER', login='owner-one', kind='User', edited=False,
         hidden=False):
    return {'body': body, 'createdAt': when, 'authorAssociation': assoc, 'includesCreatedEdit': edited,
            'isMinimized': hidden, 'url': 'https://example.invalid/c', 'author': {'login': login, '__typename': kind}}


def lens_post(name, **kw):
    return post(f'Lens: {name} · read at {SHA}\n\nNo findings.', **kw)


def approval(when='2026-11-20T11:00:00Z', assoc='OWNER', rid='r1'):
    return {'id': rid, 'body': '', 'submittedAt': when, 'state': 'APPROVED', 'authorAssociation': assoc,
            'includesCreatedEdit': False, 'isMinimized': False, 'url': 'https://example.invalid/r',
            'author': {'login': 'owner-one', '__typename': 'User'}}


def pr(number, paths, comments=(), reviews=(), merged=MERGED, labels=(), author='dev-one', files_total=None,
       comments_total=None):
    return {'number': number, 'mergedAt': merged, 'author': {'login': author, '__typename': 'User'},
            'labels': {'nodes': [{'name': n} for n in labels]},
            'files': {'totalCount': len(paths) if files_total is None else files_total,
                      'nodes': [{'path': p} for p in paths]},
            'comments': {'totalCount': len(comments) if comments_total is None else comments_total,
                         'nodes': list(comments)},
            'reviews': {'totalCount': len(reviews), 'nodes': list(reviews)}}


class ReviewTiers(Check):
    """review-tiers against a fake gh: `api rate_limit` and paged `api graphql` searches, every call logged."""

    def setUp(self):
        super().setUp()
        self.cache = os.path.join(self.tmp, 'cache', 'review-tiers')
        self.log = os.path.join(self.tmp, 'gh-calls.jsonl')
        self.repo(queue=dict(QUEUE), modules=MODULES)

    def repo(self, queue=None, modules=None):
        os.makedirs(self.root, exist_ok=True)
        if not os.path.isdir(os.path.join(self.root, '.git')):
            self.git(self.root, 'init', '-q', '-b', 'main')
        if queue is not None:
            self.write('.github/pr-queue.json', json.dumps(queue))
        if modules is not None:
            self.write('modules.json', json.dumps(modules))
        self.git(self.root, 'add', '-A')
        self.git(self.root, 'commit', '-q', '--allow-empty', '-m', 'files')

    def fake_gh(self, pages, remaining=4000, fail_page=None):
        """pages: lists of pull request nodes, one per search page. fail_page: that page fails (1 = the first)."""
        data = os.path.join(self.tmp, 'gh-pages.json')
        with open(data, 'w') as f:
            json.dump(pages, f)
        return self.script('gh', f"""
            import json, sys
            a = sys.argv[1:]
            with open({self.log!r}, 'a') as log:
                log.write(json.dumps(a) + '\\n')
            if a == ['api', 'rate_limit']:
                print(json.dumps({{'resources': {{'graphql': {{'remaining': {remaining}}}}}}}))
                sys.exit(0)
            assert a[:2] == ['api', 'graphql'], a
            opts = dict(x.split('=', 1) for x in a[2:] if '=' in x)
            page = int(opts.get('after', 'c0')[1:])
            if page + 1 == {fail_page!r}:
                sys.stderr.write('HTTP 502: Bad Gateway\\n')
                sys.exit(1)
            pages = json.load(open({data!r}))
            more = page + 1 < len(pages)
            print(json.dumps({{'data': {{'rateLimit': {{'cost': 1, 'remaining': {remaining} - page}},
                              'search': {{'pageInfo': {{'hasNextPage': more, 'endCursor': 'c%d' % (page + 1)}},
                                         'nodes': pages[page] if pages else []}}}}}}))
        """)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(ln) for ln in f]

    def queries(self):
        return [dict(x.split('=', 1) for x in c[2:] if '=' in x and x.startswith('q='))['q']
                for c in self.calls() if c[:2] == ['api', 'graphql']]

    def check(self, gh, options=None, code=0, **more):
        opts = {'tiers': TIERS} if options is None else options
        more.setdefault('cacheDir', self.cache)
        return self.run_check('review-tiers', opts, gh=gh, code=code, **more)

    def by_pr(self, found):
        return {f['where']: f for f in found if f['id'] == 'review-tiers.skipped'}

    # --- verdicts --------------------------------------------------------------------------------------------------

    def test_each_tier_asks_for_its_lenses(self):
        gh = self.fake_gh([[
            pr(1, ['docs/a.md', 'mods/chat/README.md']),                                    # tier 0: nothing needed
            pr(2, ['mods/chat/Chat.java'], [lens_post('correctness')]),                     # tier 2, one lens
            pr(3, ['mods/chat/Chat.java'], [lens_post('correctness-ops'), lens_post('design-tests')]),   # enough
            pr(4, ['mods/shop/Shop.java'], [lens_post(n) for n in ('correctness', 'design', 'tests',
                                                                    'data-integrity-1')]),  # tier 3: one DI
            pr(5, ['mods/shop/Shop.java'], [lens_post(n) for n in ('correctness', 'data-integrity-1',
                                                                    'data integrity 2')]),  # tier 3: enough
            pr(6, ['tools/x.sh']),                                                          # unmatched: tier 0
            pr(7, ['mods/web/a.js']),                                                       # tier 1: asks nothing
            pr(8, ['mods/chat/Chat.java'], [lens_post('design'), lens_post('tests')]),      # no correctness
            pr(9, ['mods/chatter/x.java', 'mods/chat'], [lens_post('correctness'), lens_post('design')]),
        ]])
        found = self.check(gh)
        got = self.by_pr(found)
        self.assertEqual(sorted(got), ['#2', '#4', '#8'])
        self.assertIn('tier 2 asks: needs 2 lens(es), found 1 (lenses before the merge: correctness)', got['#2']['text'])
        self.assertIn('tier 3 asks: needs 2 data integrity lens(es), found 1', got['#4']['text'])
        self.assertIn('needs 1 correctness lens(es), found 0', got['#8']['text'])
        self.assertTrue(all(f['severity'] == 'warn' for f in got.values()))

    def test_only_counted_authors_and_comments_before_the_merge_count(self):
        gh = self.fake_gh([[
            pr(1, ['mods/chat/C.java'], [lens_post('correctness'), lens_post('design', assoc='NONE')]),
            pr(2, ['mods/chat/C.java'], [lens_post('correctness'), lens_post('design', login='helper-bot')]),
            pr(3, ['mods/chat/C.java'], [lens_post('correctness'), lens_post('design', when='2026-11-21T09:00:00Z')]),
            pr(4, ['mods/chat/C.java'], [lens_post('correctness'), lens_post('design', assoc='MEMBER')]),
        ]])
        got = self.by_pr(self.check(gh))
        self.assertEqual(sorted(got), ['#1', '#2', '#3'])
        self.assertIn('lens after the merge: design', got['#3']['text'])

    def test_approvals_count_only_when_asked(self):
        node = pr(1, ['mods/chat/C.java'], [lens_post('correctness')], [approval()])
        self.assertEqual(sorted(self.by_pr(self.check(self.fake_gh([[node]])))), ['#1'])
        tiers = dict(TIERS, approvalsCount=True)
        os.remove(self.log)
        self.assertEqual(self.by_pr(self.check(self.fake_gh([[node]]), {'tiers': tiers}, cacheDir='')), {})

    def test_too_many_files_is_judged_at_the_top_tier(self):
        gh = self.fake_gh([[pr(1, ['docs/a.md'], [lens_post('correctness'), lens_post('design')], files_total=300,
                               comments_total=150)]])
        got = self.by_pr(self.check(gh))
        self.assertIn('tier 3 asks', got['#1']['text'])
        self.assertIn('it changed more than 100 files', got['#1']['text'])
        self.assertIn('only its last 100 comments and 50 reviews were read', got['#1']['text'])

    def test_override_waiver_and_exemptions_settle_a_pull_request(self):
        gh = self.fake_gh([[
            pr(1, ['mods/chat/C.java'], [post(f'override for head {SHA}: urgent fix')]),
            pr(2, ['mods/chat/C.java'], [post(f'override for head {SHA}: urgent fix', edited=True)]),
            pr(3, ['mods/chat/C.java'], [post(f'override for head {SHA}: urgent fix', assoc='MEMBER')]),
            pr(4, ['mods/chat/C.java'], [post('Retro waiver: a version bump only\nmore',
                                              when='2026-11-22T08:00:00Z')]),
            pr(5, ['mods/chat/C.java'], [post('Retro waiver: a version bump only', assoc='NONE')]),
            pr(6, ['mods/chat/C.java'], [post('see the retro waiver: below')]),       # not the first words
            pr(7, ['mods/chat/C.java'], labels=['no-review']),
            pr(8, ['mods/chat/C.java'], author='Release-Bot'),
            pr(9, ['mods/chat/C.java'], [post(f'override for head {SHA}: late', when='2026-11-21T09:00:00Z')]),
        ]])
        got = self.by_pr(self.check(gh, {'tiers': TIERS, 'waivers': ['retro waiver'], 'exemptLabels': ['no-review'],
                                         'exemptAuthors': ['release-bot']}))
        self.assertEqual(sorted(got), ['#2', '#3', '#5', '#6', '#9'])

    def test_the_gate_review_block_judges_without_tiers(self):
        self.repo(queue=dict(QUEUE, review=REVIEW))
        gh = self.fake_gh([[
            pr(1, ['ci/run.sh'], [lens_post('merge-path')]),
            pr(2, ['ci/run.sh'], [lens_post('merge-path'), lens_post('state')]),
            pr(3, ['src/a.ts'], [lens_post('code')]),
            pr(4, ['src/a.ts']),
        ]])
        got = self.by_pr(self.check(gh, {}))
        self.assertEqual(sorted(got), ['#1', '#4'])
        self.assertIn('less review than critical asks: needs lens state; 2 lens(es), found 1', got['#1']['text'])
        self.assertIn('less review than normal asks: needs lens code', got['#4']['text'])

    def test_the_registry_and_queue_config_come_from_git_not_the_working_tree(self):
        self.write('modules.json', json.dumps({'modules': [{'id': 'chat', 'paths': ['mods/chat'], 'tier': 0}]}))
        self.write('.github/pr-queue.json', json.dumps(dict(QUEUE, lens_author_associations=['NONE'])))
        gh = self.fake_gh([[pr(1, ['mods/chat/C.java'], [lens_post('correctness')])]])
        got = self.by_pr(self.check(gh))
        self.assertIn('found 1', got['#1']['text'])                 # tier 2 from git; OWNER counted, as in git

    def test_no_rules_no_repo_and_bad_options(self):
        gh = self.fake_gh([[]])
        self.assertEqual(self.ids(self.check(gh, {})), ['review-tiers.no-rules'])
        self.assertEqual(self.ids(self.check(gh, repo='')), ['review-tiers.no-repo'])
        self.assertEqual(self.calls(), [])                           # neither asked GitHub anything
        for opts, why in (({'tiers': dict(TIERS, colour=1)}, "unknown key 'colour'"),
                          ({'tiers': dict(TIERS, requires={})}, 'must name at least one tier'),
                          ({'tiers': TIERS, 'days': 0}, 'days must be'),
                          ({'tiers': TIERS, 'queueConfig': 'gone.json'}, 'not in the project root'),
                          ({'tiers': dict(TIERS, registry=dict(TIERS['registry'], file='gone.json'))},
                           'not in the project root')):
            with self.subTest(why=why):
                self.check(gh, opts, code=1)
                self.assertIn(why, self.last_stderr)

    def test_a_comment_the_lens_rules_cannot_read_is_a_warning(self):
        bad = post('x')
        bad['body'] = 5                                              # not text: the lens rules raise
        got = self.check(self.fake_gh([[pr(1, ['mods/chat/C.java'], [bad])]]))
        self.assertEqual(self.ids(got), ['review-tiers.unread'])
        self.assertEqual(got[0]['where'], '#1')

    # --- the GitHub budget -------------------------------------------------------------------------------------------

    def test_one_search_and_the_query(self):
        self.check(self.fake_gh([[pr(1, ['docs/a.md'])], [pr(2, ['docs/b.md'])]]))
        self.assertEqual([c[:2] for c in self.calls()], [['api', 'rate_limit'], ['api', 'graphql'], ['api', 'graphql']])
        self.assertEqual(self.queries()[0],
                         'repo:example-owner/app is:pr is:merged base:main merged:>=2026-11-17 sort:updated-desc')

    def test_a_low_budget_searches_nothing_and_judges_the_cache(self):
        self.check(self.fake_gh([[pr(1, ['mods/chat/C.java'])]]))
        os.remove(self.log)
        found = self.check(self.fake_gh([[]], remaining=500))
        self.assertEqual(self.ids(found), ['review-tiers.budget', 'review-tiers.skipped'])
        self.assertEqual(self.queries(), [])

    def test_too_many_pages_is_partial_and_the_next_run_searches_the_days_again(self):
        pages = [[pr(i, ['mods/chat/C.java'])] for i in range(1, 4)]
        found = self.check(self.fake_gh(pages), {'tiers': TIERS, 'maxPages': 2})
        self.assertIn('review-tiers.partial', self.ids(found))
        self.assertEqual(sorted(self.by_pr(found)), ['#1', '#2'])
        with open(os.path.join(self.cache, 'cache.json')) as f:
            self.assertEqual(json.load(f)['searchedDay'], '1970-01-01')
        os.remove(self.log)
        self.check(self.fake_gh(pages), {'tiers': TIERS, 'maxPages': 5})
        self.assertNotIn('updated:', self.queries()[0])               # the whole window again

    def test_the_budget_falling_while_searching_stops_the_search(self):
        pages = [[pr(i, ['docs/a.md'])] for i in range(1, 4)]
        found = self.check(self.fake_gh(pages, remaining=1000))   # 999 left after page 1
        self.assertIn('review-tiers.partial', self.ids(found))
        self.assertEqual(len(self.queries()), 2)

    # --- state: runs again, stops half-way, old or empty state ------------------------------------------------------

    def cache_file(self):
        return os.path.join(self.cache, 'cache.json')

    def test_runs_again_the_same_day_and_the_next_day_search_only_what_changed(self):
        self.check(self.fake_gh([[pr(1, ['mods/chat/C.java']), pr(2, ['docs/a.md'])]]))
        os.remove(self.log)
        found = self.check(self.fake_gh([[pr(2, ['mods/chat/C.java'])]]))   # #2 updated; #1 from the cache
        self.assertIn('updated:>=2026-11-22', self.queries()[0])
        self.assertEqual(sorted(self.by_pr(found)), ['#1', '#2'])
        os.remove(self.log)
        found = self.check(self.fake_gh([[]]), today='2026-11-24')
        self.assertIn('merged:>=2026-11-18 sort:updated-desc updated:>=2026-11-22', self.queries()[0])
        self.assertEqual(sorted(self.by_pr(found)), ['#1', '#2'])
        os.remove(self.log)
        found = self.check(self.fake_gh([[]]), today='2026-11-28')          # #1 and #2 fell out of the window
        self.assertEqual(self.by_pr(found), {})
        with open(self.cache_file()) as f:
            self.assertEqual(json.load(f)['prs'], {})

    def test_a_wider_window_searches_all_of_it(self):
        self.check(self.fake_gh([[]]))
        os.remove(self.log)
        self.check(self.fake_gh([[]]), {'tiers': TIERS, 'days': 30})
        self.assertEqual(self.queries()[0], 'repo:example-owner/app is:pr is:merged base:main merged:>=2026-10-25 '
                                            'sort:updated-desc')

    def test_stopped_half_way_leaves_the_cache_as_it_was(self):
        self.check(self.fake_gh([[pr(1, ['mods/chat/C.java'])]]))
        with open(self.cache_file(), 'rb') as f:
            before = f.read()
        self.check(self.fake_gh([[pr(2, ['docs/a.md'])], [pr(3, ['docs/a.md'])]], fail_page=2), code=1)
        self.assertIn('HTTP 502', self.last_stderr)
        with open(self.cache_file(), 'rb') as f:
            self.assertEqual(f.read(), before)
        self.assertEqual([n for n in os.listdir(self.cache) if n != 'cache.json'], [])    # no temporary file left

    def test_old_broken_empty_or_other_caches_are_read_as_empty(self):
        good = {'mergedAt': MERGED, 'author': 'a', 'bot': False, 'labels': [], 'paths': ['mods/chat/C.java'],
                'pathsComplete': True, 'commentsComplete': True}
        self.check(self.fake_gh([[]]))
        with open(self.cache_file()) as f:
            key = json.load(f)['key']
        cases = {'empty file': '', 'not JSON': '{', 'a list': '[]', 'no version': {'prs': {}},
                 'an older version': {'version': 0, 'key': key, 'prs': {}, 'from': '2026-11-17',
                                      'searchedDay': '2026-11-23'},
                 'an entry without the new keys': {'version': 1, 'key': key, 'prs': {'1': good}, 'from': '2026-11-17',
                                                   'searchedDay': '2026-11-23'},
                 'another key': {'version': 1, 'key': 'x', 'prs': {}, 'from': '2026-11-17',
                                 'searchedDay': '2026-11-23'},
                 'no search day': {'version': 1, 'key': key, 'prs': {}, 'from': '2026-11-17'},
                 'a day in the future': {'version': 1, 'key': key, 'prs': {}, 'from': '2026-11-17',
                                         'searchedDay': '2026-12-01'}}
        for why, data in cases.items():
            with self.subTest(why=why):
                with open(self.cache_file(), 'w') as f:
                    f.write(data if isinstance(data, str) else json.dumps(data))
                os.remove(self.log)
                found = self.check(self.fake_gh([[pr(5, ['mods/chat/C.java'])]]))
                self.assertNotIn('updated:', self.queries()[0])        # the whole window
                self.assertEqual(sorted(self.by_pr(found)), ['#5'])     # nothing from the bad cache
                with open(self.cache_file()) as f:
                    fresh = json.load(f)
                self.assertEqual((fresh['version'], fresh['key'], fresh['searchedDay'], sorted(fresh['prs'])),
                                 (1, key, '2026-11-23', ['5']))

    def test_a_newer_cache_is_not_used_and_not_overwritten(self):
        os.makedirs(self.cache)
        with open(self.cache_file(), 'w') as f:
            f.write('{"version": 9, "prs": {"1": {}}}')
        found = self.check(self.fake_gh([[pr(5, ['mods/chat/C.java'])]]))
        self.assertEqual(sorted(self.by_pr(found)), ['#5'])
        self.assertNotIn('updated:', self.queries()[0])
        with open(self.cache_file()) as f:
            self.assertEqual(f.read(), '{"version": 9, "prs": {"1": {}}}')

    def test_a_dry_run_and_no_cache_folder_write_nothing(self):
        self.check(self.fake_gh([[pr(1, ['mods/chat/C.java'])]]), dryRun=True)
        self.assertFalse(os.path.exists(self.cache))
        self.check(self.fake_gh([[pr(1, ['mods/chat/C.java'])]]), cacheDir='')
        self.assertFalse(os.path.exists(self.cache))

    def test_the_cache_keeps_no_comment_text(self):
        self.check(self.fake_gh([[pr(1, ['mods/chat/C.java'], [post('Lens: correctness\nsecret words here')])]]))
        with open(self.cache_file()) as f:
            self.assertNotIn('secret words', f.read())


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
    'review-tiers: only lens comments from before the merge count': (
        'checks/review-tiers', "        keep = [c for c in comments if ((c.get('createdAt') or '') <= merged) == before]",
        "        keep = comments if before else []",
        'ReviewTiers.test_only_counted_authors_and_comments_before_the_merge_count'),
    'review-tiers: an override counts only before the merge': (
        'checks/review-tiers', "        posts = [p for _, _, p in review.lens_posts(before)]", "        posts = comments",
        'ReviewTiers.test_override_waiver_and_exemptions_settle_a_pull_request'),
    'review-tiers: a waiver counts only from a counted author': (
        'checks/review-tiers', "    return review.counts(post) and not review.is_bot(post)",
        "    return not review.is_bot(post)", 'ReviewTiers.test_override_waiver_and_exemptions_settle_a_pull_request'),
    'review-tiers: too many files is the top tier': (
        'checks/review-tiers', "        tier = max(tier, tiers['top'])\n", "        pass\n",
        'ReviewTiers.test_too_many_files_is_judged_at_the_top_tier'),
    'review-tiers: a partial search does not move the search day': (
        'checks/review-tiers', "        if complete:\n            searched = ctx['today']", "        searched = ctx['today']",
        'ReviewTiers.test_too_many_pages_is_partial_and_the_next_run_searches_the_days_again'),
    'review-tiers: a wider window searches all of it': (
        'checks/review-tiers', "and good_day(cache.get('from'), start)", 'and True',
        'ReviewTiers.test_a_wider_window_searches_all_of_it'),
    'review-tiers: a dry run writes no cache': (
        'checks/review-tiers', 'if path and not dry and not newer:', 'if path and not newer:',
        'ReviewTiers.test_a_dry_run_and_no_cache_folder_write_nothing'),
    'review-tiers: a newer cache is not overwritten': (
        'checks/review-tiers', 'if path and not dry and not newer:', 'if path and not dry:',
        'ReviewTiers.test_a_newer_cache_is_not_used_and_not_overwritten'),
    'review-tiers: a cache entry of another shape is not used': (
        'checks/review-tiers', "and good_fact(f) for k, f in data['prs'].items()", "for k, f in data['prs'].items()",
        'ReviewTiers.test_old_broken_empty_or_other_caches_are_read_as_empty'),
    'review-tiers: a low budget does not search': (
        'checks/review-tiers', '    if left < min_left:\n        emit(', '    if False:\n        emit(',
        'ReviewTiers.test_a_low_budget_searches_nothing_and_judges_the_cache'),
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
