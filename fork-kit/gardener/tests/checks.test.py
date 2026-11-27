"""The general checks shipped with the kit, each run the way the runner runs it (the contract context on stdin) on
fixture files, temporary git repositories, a fake `gh` (the GitHub API, GraphQL search too) and a fake `facts`. Every line they print must be a finding.
No network, no root.

  python3 -I -B fork-kit/gardener/tests/checks.test.py
"""
import hashlib, json, os, shutil, subprocess, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gardener_fixture as gf  # noqa: E402
from gardener_fixture import contract  # noqa: E402

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='t@example.invalid', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')


class Check(gf.Temp):

    env = {}

    def run_check(self, name, options=None, fix=False, repo='example-owner/app', gh='gh', code=0, script=None,
                  **more):
        ctx = {'version': contract.VERSION, 'project': {'name': 'example', 'repo': repo, 'root': self.root},
               'check': name, 'options': options or {}, 'fix': fix, 'today': '2026-11-23', 'gh': gh, **more}
        r = subprocess.run([sys.executable, '-I', '-B', script or os.path.join(gf.CHECKS, name)], input=json.dumps(ctx),
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


class MachineTools(Check):
    """A made-up machine under a prefix: a kit with its record, a git checkout, hand-made files and units."""

    HOME = '/home/example'

    def setUp(self):
        super().setUp()
        self.prefix = os.path.join(self.tmp, 'host')
        self.env = {'GARDENER_TEST_PREFIX': self.prefix, 'HOME': self.HOME}
        os.makedirs(self.prefix + '/etc/kit-a')
        with open(self.prefix + '/etc/kit-a/installed.tsv', 'w') as f:
            f.write('# path\tkind\twant\n'
                    '/usr/local/libexec/kit-a/current\tlink\t/usr/local/libexec/kit-a/versions/' + 'a' * 40 + '\n'
                    '/usr/local/bin/kit-a\tlink\t/usr/local/libexec/kit-a/current/bin/kit-a\n')
        self.file('/usr/local/libexec/kit-a/versions/' + 'a' * 40 + '/bin/kit-a')
        self.link('/usr/local/libexec/kit-a/current', '/usr/local/libexec/kit-a/versions/' + 'a' * 40)
        self.link('/usr/local/bin/kit-a', '/usr/local/libexec/kit-a/current/bin/kit-a')
        self.checkout = self.HOME + '/src/tools'
        self.file(self.checkout + '/bin/committed')
        self.file(self.checkout + '/bin/scratch')
        self.git(self.prefix + self.checkout, 'init', '-q')
        self.git(self.prefix + self.checkout, 'add', 'bin/committed')
        self.git(self.prefix + self.checkout, 'commit', '-q', '-m', 'tools')
        self.file('/lib/systemd/system/ssh.service')

    def file(self, path):
        os.makedirs(os.path.dirname(self.prefix + path), exist_ok=True)
        with open(self.prefix + path, 'w') as f:
            f.write('#!/bin/sh\n')

    def link(self, path, target):
        """A link as the machine would have it, its target inside the prefix."""
        os.makedirs(os.path.dirname(self.prefix + path), exist_ok=True)
        os.symlink(target if not target.startswith('/') else self.prefix + target, self.prefix + path)

    def found(self, **opts):
        return sorted((f['id'], f['where']) for f in self.run_check('machine-tools', opts))

    def test_a_tool_the_machine_needs_that_is_missing_names_its_source(self):
        tools = [{'path': '/usr/local/libexec/editor-build', 'source': 'example-owner/app: tools/editor-build'},
                 {'path': '~/.local/bin/helper', 'source': 'the helper kit'}]
        found = self.run_check('machine-tools', {'tools': tools, 'scan': []})
        self.assertEqual(sorted((f['id'], f['severity'], f['where']) for f in found),
                         [('machine-tools.missing', 'error', '/home/example/.local/bin/helper'),
                          ('machine-tools.missing', 'error', '/usr/local/libexec/editor-build')])
        self.assertIn('example-owner/app: tools/editor-build', ' '.join(f['text'] + f['fix'] for f in found))
        self.file('/usr/local/libexec/editor-build')
        self.file(self.HOME + '/.local/bin/helper')
        self.assertEqual(self.run_check('machine-tools', {'tools': tools, 'scan': []}), [])

    def test_every_entry_needs_a_source(self):
        self.link(self.HOME + '/.local/bin/from-repo', self.checkout + '/bin/committed')
        self.link(self.HOME + '/.local/bin/not-committed', self.checkout + '/bin/scratch')
        self.link(self.HOME + '/.local/bin/gone', self.HOME + '/src/old/tool')
        self.file(self.HOME + '/.local/bin/declared')
        self.file(self.HOME + '/.local/bin/by-hand')
        self.file('/usr/local/bin/by-hand-too')
        self.link('/etc/systemd/system/sshd.service', '/lib/systemd/system/ssh.service')
        self.link('/etc/systemd/system/multi-user.target.wants/hand.service', '/etc/systemd/system/hand.service')
        os.makedirs(self.prefix + '/etc/systemd/system/hand.service.d')
        self.file('/etc/systemd/system/hand.service')
        self.file('/etc/systemd/system/snap-core-1.mount')
        self.file(self.HOME + '/.config/systemd/user/watch.service')
        self.link(self.HOME + '/.config/systemd/user/watch-alias.service', 'watch.service')
        self.assertEqual(self.found(tools=[{'path': '~/.local/bin/declared', 'source': 'example-owner/app: bin'}]), [
            ('machine-tools.broken-link', '/home/example/.local/bin/gone'),
            ('machine-tools.no-source', '/etc/systemd/system/hand.service'),
            ('machine-tools.no-source', '/home/example/.config/systemd/user/watch.service'),
            ('machine-tools.no-source', '/home/example/.local/bin/by-hand'),
            ('machine-tools.no-source', '/usr/local/bin/by-hand-too'),
            ('machine-tools.untracked', '/home/example/.local/bin/not-committed')])

    def test_scan_and_ignore_options(self):
        self.file('/usr/local/bin/by-hand')
        self.file('/usr/local/bin/vendor-tool')
        self.assertEqual(self.found(scan=[]), [])
        self.assertEqual(self.found(scan=['/usr/local/bin'], ignore=['vendor-*']),
                         [('machine-tools.no-source', '/usr/local/bin/by-hand')])
        self.assertEqual(self.found(scan=['/no/such/folder']), [])

    def test_bad_options_fail_the_check(self):
        for opts in ({'tools': [{'path': '/x'}]}, {'tools': [{'path': '/x', 'source': 's', 'extra': 1}]},
                     {'tools': 'x'}, {'scan': '/usr/local/bin'}, {'ignore': [1]},
                     {'tools': {}}, {'tools': ''}, {'tools': 0},
                     {'tools': [{'path': 'rel/x', 'source': 's'}]}, {'scan': ['rel']}, {'etc': 'etc'}):
            with self.subTest(opts=opts):
                self.run_check('machine-tools', opts, code=1)

    def test_a_checkout_git_cannot_read_is_no_source(self):
        """Fails closed: a link into a removed worktree, or an empty .git folder, is never taken as committed."""
        self.file(self.HOME + '/src/wt/bin/tool')
        with open(self.prefix + self.HOME + '/src/wt/.git', 'w') as f:
            f.write(f'gitdir: {self.prefix}{self.checkout}/.git/worktrees/wt\n')      # its main checkout is gone
        self.link(self.HOME + '/.local/bin/wt-tool', self.HOME + '/src/wt/bin/tool')
        self.assertEqual(self.found(scan=['~/.local/bin']),
                         [('machine-tools.unknown-source', '/home/example/.local/bin/wt-tool')])
        os.makedirs(self.prefix + self.HOME + '/.local/bin/.git')                      # an empty .git folder
        self.file(self.HOME + '/.local/bin/by-hand')
        found = self.run_check('machine-tools', {'scan': ['~/.local/bin']})
        self.assertEqual(sorted((f['id'], f['where']) for f in found),
                         [('machine-tools.unknown-source', '/home/example/.local/bin/by-hand'),
                          ('machine-tools.unknown-source', '/home/example/.local/bin/wt-tool')])
        self.assertIn('not a git repository', ' '.join(f['text'] for f in found))

    def test_each_kit_record_form_is_a_source(self):
        """pc-agents writes sha256<TAB>path rows, gh-apps 6-column rows with the link target last."""
        os.makedirs(self.prefix + '/etc/pc-agents')
        with open(self.prefix + '/etc/pc-agents/installed.tsv', 'w') as f:
            f.write('b' * 64 + '\t/etc/systemd/system/pc-agents-walls.service\n')
        os.makedirs(self.prefix + '/etc/gh-apps')
        with open(self.prefix + '/etc/gh-apps/installed.tsv', 'w') as f:
            f.write('/opt/gh-apps/current\tlink\troot\troot\t0755\t/opt/gh-apps/versions/' + 'c' * 40 + '\n')
        self.file('/etc/systemd/system/pc-agents-walls.service')
        self.file('/opt/gh-apps/versions/' + 'c' * 40 + '/bin/gh-app')
        self.link('/usr/local/bin/gh-app', '/opt/gh-apps/versions/' + 'c' * 40 + '/bin/gh-app')
        self.assertEqual(self.found(scan=['/etc/systemd/system', '/usr/local/bin']), [])

    def test_unit_folders_masks_subfolders_and_aliases(self):
        """A mask needs no source; only .wants/.requires/.upholds/.d folders are skipped; an alias is a link to a
        unit in the same folder, not anywhere below it."""
        self.link('/etc/systemd/system/cron.service', '/dev/null')
        os.makedirs(self.prefix + '/etc/systemd/system/default.target.upholds')
        self.file('/etc/systemd/system/x/hand.service')
        self.link('/etc/systemd/system/hand.service', '/etc/systemd/system/x/hand.service')
        self.assertEqual(self.found(scan=['/etc/systemd/system']),
                         [('machine-tools.no-source', '/etc/systemd/system/hand.service'),
                          ('machine-tools.no-source', '/etc/systemd/system/x')])

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
       comments_total=None, updated='2026-11-20T12:30:00Z'):
    return {'number': number, 'mergedAt': merged, 'updatedAt': updated, 'author': {'login': author, '__typename': 'User'},
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

    def fake_gh(self, pages, remaining=4000, fail_page=None, fail_first=()):
        """pages: lists of pull request nodes, one per search page. fail_page: that page fails (1 = the first).
        fail_first: pages of these sizes get a GraphQL error. A query with an "updated" range (a resumed search) gets
        only the nodes in it, paged again at the same size (the search is newest first, like GitHub's)."""
        data = os.path.join(self.tmp, 'gh-pages.json')
        with open(data, 'w') as f:
            json.dump(pages, f)
        return self.script('gh', f"""
            import json, re, sys
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
            if int(opts['first']) in {tuple(fail_first)!r}:
                print(json.dumps({{'errors': [{{'message': 'Something went wrong while executing your query.'}}]}}))
                sys.exit(0)
            pages = json.load(open({data!r}))
            m = re.search(r'updated:(?:<=|(\\S+)\\.\\.)(\\S+)', opts['q'])
            if m:
                lo, hi = [x and x.replace('+00:00', 'Z') for x in m.groups()]
                size = max([len(p) for p in pages] + [1])
                keep = [n for p in pages for n in p if n['updatedAt'] <= hi and (not lo or n['updatedAt'] >= lo)]
                pages = [keep[i:i + size] for i in range(0, len(keep), size)] or [[]]
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
        return {f['where']: f for f in found if f['id'] in ('review-tiers.skipped', 'review-tiers.short')}

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
        self.assertEqual({w: (f['id'], f['severity']) for w, f in got.items()},     # a kind missing: warn at the top
                         {'#2': ('review-tiers.short', 'info'), '#4': ('review-tiers.short', 'warn'),
                          '#8': ('review-tiers.short', 'info')})

    def test_only_counted_authors_and_comments_before_the_merge_count(self):
        gh = self.fake_gh([[
            pr(1, ['mods/chat/C.java'], [lens_post('correctness'), lens_post('design', assoc='NONE')]),
            pr(2, ['mods/chat/C.java'], [lens_post('correctness'), lens_post('design', login='helper-bot')]),
            pr(3, ['mods/chat/C.java'], [lens_post('correctness'), lens_post('design', when='2026-11-21T09:00:00Z')]),
            pr(4, ['mods/chat/C.java'], [lens_post('correctness'), lens_post('design', assoc='MEMBER')]),
        ]])
        found = self.check(gh)
        self.assertEqual(sorted(self.by_pr(found)), ['#1', '#2'])
        late = [f for f in found if f['id'] == 'review-tiers.late']           # #3: enough only with the late lens
        self.assertEqual([(f['where'], f['severity']) for f in late], [('#3', 'info')])
        self.assertIn('lenses after the merge: design', late[0]['text'])

    def test_a_review_after_the_merge_settles_the_warning_as_the_hint_says(self):
        late = dict(when='2026-11-21T09:00:00Z')
        gh = self.fake_gh([[
            pr(1, ['mods/chat/C.java'], [lens_post('correctness', **late), lens_post('design', **late)]),   # enough
            pr(2, ['mods/chat/C.java'], [lens_post('design', **late)]),                    # still not enough
            pr(3, ['mods/shop/S.java'], [lens_post(n, **late) for n in ('correctness', 'data integrity 1',
                                                                        'data-integrity-2')]),
        ]])
        found = self.check(gh, {'tiers': TIERS, 'waivers': ['Retro waiver']})
        self.assertEqual([(f['id'], f['where'], f['severity']) for f in found],
                         [('review-tiers.late', '#1', 'info'), ('review-tiers.skipped', '#2', 'warn'),
                          ('review-tiers.late', '#3', 'info')])
        self.assertIn('lens after the merge: design', found[1]['text'])
        self.assertEqual(found[1]['fix'], 'review it now: lens comments on the pull request settle this, even after '
                                          'the merge; if it needed no (more) review, a comment starting "Retro '
                                          'waiver: why"')

    def test_no_lens_at_all_is_told_apart_from_a_kind_missing(self):
        gh = self.fake_gh([[
            pr(1, ['mods/chat/C.java']),
            pr(2, ['mods/chat/C.java'], [lens_post('design')]),
            pr(3, ['mods/shop/S.java']),
            pr(4, ['mods/chat/C.java'], reviews=[approval()]),
        ]])
        got = {f['where']: (f['id'], f['severity']) for f in self.check(gh)}
        self.assertEqual(got, {'#1': ('review-tiers.skipped', 'warn'), '#2': ('review-tiers.short', 'info'),
                               '#3': ('review-tiers.skipped', 'warn'), '#4': ('review-tiers.skipped', 'warn')})
        os.remove(self.log)
        got = {f['where']: f['id'] for f in self.check(gh, {'tiers': dict(TIERS, approvalsCount=True)}, cacheDir='')}
        self.assertEqual(got['#4'], 'review-tiers.short')                 # a counted approval is some review

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
        self.assertEqual(got['#4']['text'], 'merged with no review: needs lens code; 1 lens(es), found 0 (normal)')

    def test_too_many_files_is_judged_critical_by_the_review_block_too(self):
        self.repo(queue=dict(QUEUE, review=REVIEW))
        got = self.by_pr(self.check(self.fake_gh([[pr(1, ['src/a.ts'], [lens_post('code')], files_total=300)]]), {}))
        self.assertIn('less review than critical asks: needs 2 lens(es), found 1', got['#1']['text'])
        self.assertEqual(got['#1']['severity'], 'warn')

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
                          ({'tiers': TIERS, 'repo': 'other-owner/secret'}, 'there is no "repo" option'),
                          ({'tiers': TIERS, 'repo': 'example-owner/app'}, 'there is no "repo" option'),
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

    def test_a_cut_off_search_goes_on_where_it_stopped_until_every_pull_request_is_judged(self):
        """Newest first, one page a run: three runs read three pages (each run reads its cut-off second again), then the
        next day searches only what changed."""
        nodes = [pr(i, ['mods/chat/C.java'], updated=f'2026-11-22T{20 - i}:00:00Z') for i in range(1, 8)]
        pages = [nodes[:3], nodes[3:6], nodes[6:]]
        opts = {'tiers': TIERS, 'maxPages': 1}
        seen = []
        for run in range(3):
            if os.path.exists(self.log):
                os.remove(self.log)
            found = self.check(self.fake_gh(pages), opts)
            seen.append((sorted(self.by_pr(found)), [f['severity'] for f in found if f['id'] == 'review-tiers.partial'],
                         self.queries()))
        base = 'repo:example-owner/app is:pr is:merged base:main merged:>=2026-11-17 sort:updated-desc'
        self.assertEqual(seen, [
            (['#1', '#2', '#3'], ['warn'], [base]),
            (['#1', '#2', '#3', '#4', '#5'], ['warn'], [base + ' updated:<=2026-11-22T17:00:00+00:00']),
            ([f'#{i}' for i in range(1, 8)], [], [base + ' updated:<=2026-11-22T15:00:00+00:00'])])
        with open(self.cache_file()) as f:
            data = json.load(f)
        self.assertEqual((data['searchedDay'], 'resume' in data), ('2026-11-23', False))
        os.remove(self.log)
        self.check(self.fake_gh([[]]), opts, today='2026-11-24')
        self.assertEqual(self.queries(), [base.replace('11-17', '11-18') + ' updated:>=2026-11-22'])

    def test_a_resumed_search_keeps_its_lower_day_and_a_finished_one_searches_on_in_the_same_run(self):
        self.check(self.fake_gh([[pr(1, ['docs/a.md'])]]), today='2026-11-20')       # search day 11-20
        os.remove(self.log)
        pages = [[pr(i, ['docs/a.md'], updated=f'2026-11-2{3 - i}T10:00:00Z')] for i in range(1, 3)]
        self.check(self.fake_gh(pages), {'tiers': TIERS, 'maxPages': 1})
        with open(self.cache_file()) as f:
            self.assertEqual(json.load(f)['resume'], {'day': '2026-11-23', 'since': '2026-11-19',
                                                      'before': '2026-11-22T10:00:00Z'})
        os.remove(self.log)
        self.check(self.fake_gh(pages), {'tiers': TIERS, 'maxPages': 3})
        self.assertEqual([q.split('sort:updated-desc ')[1] for q in self.queries()],
                         ['updated:2026-11-19T00:00:00+00:00..2026-11-22T10:00:00+00:00'] * 2
                         + ['updated:>=2026-11-22'])

    def test_a_page_of_one_second_does_not_stop_the_search_for_ever(self):
        same = '2026-11-22T10:00:00Z'
        pages = [[pr(1, ['docs/a.md'], updated=same), pr(2, ['docs/a.md'], updated=same)],
                 [pr(3, ['docs/a.md'], updated='2026-11-21T10:00:00Z')]]
        for run in range(2):
            self.check(self.fake_gh(pages), {'tiers': TIERS, 'maxPages': 1})
        with open(self.cache_file()) as f:
            self.assertEqual(json.load(f)['resume']['before'], '2026-11-22T09:59:59Z')
        self.check(self.fake_gh(pages), {'tiers': TIERS, 'maxPages': 1})
        with open(self.cache_file()) as f:
            self.assertEqual(sorted(json.load(f)['prs']), ['1', '2', '3'])

    def test_a_broken_resume_point_is_dropped(self):
        self.check(self.fake_gh([[]]))
        with open(self.cache_file()) as f:
            data = json.load(f)
        for bad in ({'day': '2026-11-23', 'since': '', 'before': 'yesterday'}, {'day': '2026-11-23'}, [],
                    {'day': '2026-12-30', 'since': '', 'before': '2026-11-22T10:00:00Z'}):
            with self.subTest(bad=bad):
                with open(self.cache_file(), 'w') as f:
                    json.dump(dict(data, resume=bad), f)
                os.remove(self.log)
                self.check(self.fake_gh([[]]))
                self.assertIn('resume point in the cache is broken', self.last_stderr)
                self.assertEqual([q.split('sort:updated-desc')[1] for q in self.queries()], [' updated:>=2026-11-22'])

    def test_a_page_github_cannot_answer_is_asked_again_smaller(self):
        found = self.check(self.fake_gh([[pr(1, ['mods/chat/C.java'])]], fail_first=[20]))
        self.assertEqual(sorted(self.by_pr(found)), ['#1'])
        firsts = [dict(x.split('=', 1) for x in c[2:] if '=' in x)['first'] for c in self.calls() if c[1] == 'graphql']
        self.assertEqual(firsts, ['20', '5'])
        self.check(self.fake_gh([[]], fail_first=[20, 5]), code=1)
        self.assertIn('Something went wrong', self.last_stderr)

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
        good = {'mergedAt': MERGED, 'author': 'a', 'labels': [], 'paths': ['mods/chat/C.java'],
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

    def test_a_change_to_the_gate_code_starts_the_cache_afresh(self):
        """The cache keeps lens names the gate's code read: other code may read other names."""
        kit = os.path.join(self.tmp, 'kit')
        shutil.copytree(gf.CHECKS, os.path.join(kit, 'checks'))
        shutil.copytree(gf.LIB, os.path.join(kit, 'lib'), ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(os.path.join(os.path.dirname(os.path.dirname(gf.KIT)), 'fork-ci', 'actions', 'pr-queue'),
                        os.path.join(kit, 'gate'), ignore=shutil.ignore_patterns('__pycache__'))
        script = os.path.join(kit, 'checks', 'review-tiers')
        self.check(self.fake_gh([[]]), script=script)
        os.remove(self.log)
        self.check(self.fake_gh([[]]), script=script)
        self.assertIn('updated:>=', self.queries()[0])                       # same code: the cache is used
        with open(os.path.join(kit, 'gate', 'lens.py'), 'a') as f:
            f.write('\n# a new release\n')
        os.remove(self.log)
        self.check(self.fake_gh([[]]), script=script)
        self.assertNotIn('updated:', self.queries()[0])                      # the whole window

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
    'review-tiers: a cut-off search does not move the search day': (
        'checks/review-tiers', "            if got['complete']:\n                searched = ctx['today']",
        "            if True:\n                searched = ctx['today']", 'ReviewTiers.test_a_cut_off_search_goes_on_where_it_stopped_until_every_pull_request_is_judged'),
    'review-tiers: a cut-off search goes on where it stopped': (
        'checks/review-tiers', '        if resume:    # first go on', '        if False:    # first go on', 'ReviewTiers.test_a_cut_off_search_goes_on_where_it_stopped_until_every_pull_request_is_judged'),
    'review-tiers: a cut-off search is a warning': (
        'checks/review-tiers', "emit('review-tiers.partial', 'warn'", "emit('review-tiers.partial', 'info'", 'ReviewTiers.test_a_cut_off_search_goes_on_where_it_stopped_until_every_pull_request_is_judged'),
    'review-tiers: a page of one second is stepped past': (
        'checks/review-tiers', "else second_before(resume['before']))", "else resume['before'])",
        'ReviewTiers.test_a_page_of_one_second_does_not_stop_the_search_for_ever'),
    'review-tiers: a broken resume point is dropped': (
        'checks/review-tiers', "    if resume is not None and not good_resume(resume, ctx['today']):",
        '    if False:', 'ReviewTiers.test_a_broken_resume_point_is_dropped'),
    'review-tiers: a page GitHub cannot answer is asked again smaller': (
        'checks/review-tiers', '        if err is not None and first == PAGE:', '        if False:',
        'ReviewTiers.test_a_page_github_cannot_answer_is_asked_again_smaller'),
    'review-tiers: a review after the merge settles it': (
        'checks/review-tiers', "        if f['lensesAfter'] and not verdict(", "        if False and not verdict(",
        'ReviewTiers.test_a_review_after_the_merge_settles_the_warning_as_the_hint_says'),
    'review-tiers: no lens at all is told apart': (
        'checks/review-tiers', "emit('review-tiers.skipped' if none else 'review-tiers.short',",
        "emit('review-tiers.short',", 'ReviewTiers.test_no_lens_at_all_is_told_apart_from_a_kind_missing'),
    'review-tiers: there is no repo option': (
        'checks/review-tiers', "    if 'repo' in opt:   #", '    if False:   #',
        'ReviewTiers.test_no_rules_no_repo_and_bad_options'),
    'review-tiers: the gate code is part of the cache key': (
        'checks/review-tiers', '                                     gate_hash()]).encode()',
        '                                     ]).encode()', 'ReviewTiers.test_a_change_to_the_gate_code_starts_the_cache_afresh'),
    'review-tiers: too many files is critical by the review block too': (
        'checks/review-tiers', "    if not f['pathsComplete']:   # the paths past", '    if False:   # the paths past',
        'ReviewTiers.test_too_many_files_is_judged_critical_by_the_review_block_too'),
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
    'machine-tools: a missing tool is an error': (
        'checks/machine-tools', "    if not os.path.exists(prefix + path):\n        emit('machine-tools.missing'",
        "    if False:\n        emit('machine-tools.missing'",
        'MachineTools.test_a_tool_the_machine_needs_that_is_missing_names_its_source'),
    'machine-tools: a kit record is a source': (
        'checks/machine-tools', "        if from_a_kit(path, target):\n", "        if False:\n",
        'MachineTools.test_every_entry_needs_a_source'),
    'machine-tools: only a committed file is a source': (
        'checks/machine-tools', "if os.path.relpath(target, folder) in files else 'untracked'",
        "if True else 'untracked'", 'MachineTools.test_every_entry_needs_a_source'),
    'machine-tools: a checkout git cannot read is no source': (
        'checks/machine-tools', "    if files is None:\n        return folder, why",
        "    if files is None:\n        return folder, 'tracked'",
        'MachineTools.test_a_checkout_git_cannot_read_is_no_source'),
    'machine-tools: the pc-agents record form is read': (
        'checks/machine-tools', "        if len(row) == 2:           # pc-agents", "        if False:           # pc",
        'MachineTools.test_each_kit_record_form_is_a_source'),
    'machine-tools: an alias is in the same folder': (
        'checks/machine-tools', "os.path.dirname(target) == here:", "inside(os.path.dirname(target), here):",
        'MachineTools.test_unit_folders_masks_subfolders_and_aliases'),
    'machine-tools: a plain file is no source': (
        'checks/machine-tools', "        emit('machine-tools.no-source', 'warn',",
        "        continue\n        emit('machine-tools.no-source', 'warn',",
        'MachineTools.test_every_entry_needs_a_source'),
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
