"""The `worktrees` rule (lib/cleaner/rules.py): git worktrees and clones of a listed repository whose pull request
merged a day or more ago and that hold nothing GitHub does not have. Real git in a temporary folder; "GitHub" is a
bare repository plus pull request rows the test writes. RedCheck breaks each refusal in a copy of the code and
shows its test then fails.

  python3 -I -B fork-kit/cleaner/tests/worktrees.test.py
"""
import os, shutil, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cleaner_fixture as fx  # noqa: E402

import machine_file  # noqa: E402


class Case(fx.Case):
    c = fx.load()

    def planned(self, **over):
        return self.plan(self.repo_cfg(), **over)

    def git_dir(self, tree):
        return self.git(tree, 'rev-parse', '--absolute-git-dir')


class Worktrees(Case):
    def test_a_finished_worktree_is_removed(self):
        tree = self.finished()
        cfg = self.repo_cfg()
        item = self.assertDeletes(self.plan(cfg), tree, 'worktrees')
        self.assertEqual(item.kind, 'worktree')
        self.assertIn('#7', item.why)
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 0, lines)
        self.assertFalse(os.path.exists(tree))
        base = self.path('trees', 'base')
        self.assertNotIn(tree, self.git(base, 'worktree', 'list', '--porcelain'))
        self.assertEqual(self.git(base, 'rev-parse', '--verify', 'refs/heads/fix-1'), self.merged[0]['headRefOid'])
        code, lines = self.run_cleaner(cfg)                     # runs again: nothing left to do
        self.assertEqual(code, 0)
        self.assertEqual([line for line in lines if line.startswith('deleted ')], [])

    def test_an_open_pull_request_keeps_it(self):
        tree = self.finished()
        self.pr(tree, 8, state='open')
        self.assertKept(self.planned(), tree, 'open PR', '#8')

    def test_no_merged_pull_request_keeps_it(self):
        tree = self.finished()
        self.merged[0]['headRefName'] = 'some-other-branch'
        self.assertKept(self.planned(), tree, 'no merged PR')

    def test_merged_less_than_a_day_ago_keeps_it(self):
        young, old = self.finished('young', number=1, hours=23), self.finished('old', number=2, hours=25)
        plan = self.planned()
        self.assertKept(plan, young, 'merged under 24 h ago', '#1')
        self.assertDeletes(plan, old)
        plan = self.plan(self.cfg(worktrees={'repos': [{'root': self.trees, 'github': fx.REPO}], 'mergedHours': 72}))
        self.assertKept(plan, old, 'merged under 72 h ago')
        with self.assertRaises(machine_file.MachineFileError):
            self.cfg(worktrees={'mergedHours': 23})

    def test_a_merge_time_it_cannot_read_keeps_it(self):
        tree = self.finished()
        self.merged[0]['mergedAt'] = 'yesterday'
        self.assertKept(self.planned(), tree, 'could not read', 'GitHub')

    def test_uncommitted_or_untracked_files_keep_it(self):
        changed, added = self.finished('changed', number=1), self.finished('added', number=2)
        self.file(os.path.join(changed, 'README.md'), 'half done\n')
        self.file(os.path.join(added, 'new-idea.txt'))
        plan = self.planned()
        self.assertKept(plan, changed, 'uncommitted or untracked')
        self.assertKept(plan, added, 'uncommitted or untracked')

    def test_commits_the_merged_pull_request_does_not_have_keep_it(self):
        tree = self.finished()
        self.commit(tree, 'later.txt')
        self.assertKept(self.planned(), tree, 'commits not in the merged PR', '#7')

    def test_a_pull_request_head_it_cannot_compare_with_keeps_it(self):
        tree = self.finished()
        self.merged[0]['headRefOid'] = '1' * 40             # GitHub's head is a commit this clone does not have
        self.assertKept(self.planned(), tree, 'commits not in the merged PR')

    def test_a_worktree_behind_the_merged_head_may_go(self):
        tree = self.finished()
        first = self.head(tree)
        self.merged[0]['headRefOid'] = self.commit(tree, 'second.txt')
        self.push(tree)
        self.git(tree, 'reset', '-q', '--hard', first)
        self.assertDeletes(self.planned(), tree)

    def test_a_process_inside_keeps_it(self):
        tree = self.finished()
        self.busy.update(self.c.facts.ancestors(os.path.join(tree, 'apps')))
        self.assertKept(self.planned(), tree, 'in use')

    def test_github_down_keeps_everything(self):
        tree = self.finished()

        def down(repo):
            raise self.c.facts.Unreadable('GitHub (gh pr list: could not connect)')
        self.assertKept(self.planned(prs=down), tree, 'could not read', 'GitHub')
        code, lines = self.run_cleaner(self.repo_cfg(), prs=down)
        self.assertEqual(code, 1, lines)
        self.assertTrue(os.path.isdir(tree))

    def test_git_that_cannot_answer_keeps_it(self):
        tree = self.finished()
        with mock.patch.object(self.c.facts, 'GIT', self.path('no-such-git')):
            self.assertKept(self.planned(), tree, 'could not read', 'git')

    def test_git_that_stops_answering_half_way_keeps_it(self):
        tree = self.finished()

        def broken(path):
            raise self.c.facts.Unreadable('worktree: exit 128: fatal: bad object')
        with mock.patch.object(self.c.rules, 'worktree_list', broken):
            self.assertKept(self.planned(), tree, 'could not read', 'git')

    def test_ignored_files_that_are_not_build_output_keep_it(self):
        notes, log, built = (self.finished(n, number=i) for i, n in enumerate(('notes', 'log', 'built')))
        self.file(os.path.join(notes, 'local', 'findings.md'))
        self.file(os.path.join(log, 'debug.log'))
        self.file(os.path.join(built, 'node_modules', 'left-pad', 'index.js'))
        self.file(os.path.join(built, 'tools', '__pycache__', 'x.pyc'))
        plan = self.planned()
        self.assertKept(plan, notes, 'ignored files', 'local/')
        self.assertKept(plan, log, 'ignored files', 'debug.log')
        self.assertDeletes(plan, built)
        plan = self.plan(self.repo_cfg(regenerable=['*.log']))            # a project names its own build output
        self.assertDeletes(plan, log)
        self.assertDeletes(plan, built)                                   # ...on top of the built-in names
        self.assertKept(plan, notes, 'ignored files')

    def test_no_list_makes_a_credential_looking_file_build_output(self):
        tree = self.finished()
        self.file(os.path.join(tree, '.env'), 'made up for the test\n')
        self.assertEqual(self.git(tree, 'status', '--porcelain'), '')         # git ignores it
        for regenerable in (None, ['.env'], ['*'], ['.*']):
            self.assertKept(self.plan(self.repo_cfg(regenerable=regenerable)), tree, 'ignored files', '.env')

    def test_files_hidden_from_git_status_keep_it(self):
        for number, flag in enumerate(('--skip-worktree', '--assume-unchanged')):
            tree = self.finished(flag.strip('-'), number=number)
            self.git(tree, 'update-index', flag, 'README.md')
            self.file(os.path.join(tree, 'README.md'), 'a local change git no longer shows\n')
            self.assertEqual(self.git(tree, 'status', '--porcelain'), '')
            self.assertKept(self.planned(), tree, 'hidden from git status', 'README.md')

    def test_an_unfinished_git_operation_keeps_it(self):
        for number, mark in enumerate(('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG', 'rebase-merge', 'index.lock')):
            tree = self.finished(f'op-{number}', number=number)
            self.file(os.path.join(self.git_dir(tree), mark), self.head(tree) + '\n')
            self.assertKept(self.planned(), tree, 'unfinished git operation', mark)

    def test_refs_of_its_own_keep_it(self):
        tree = self.finished()
        self.git(tree, 'update-ref', 'refs/worktree/before-rework', 'HEAD')
        self.assertKept(self.planned(), tree, 'refs of its own', 'refs/worktree/before-rework')

    def test_a_commit_only_its_reflog_still_has_keeps_it(self):
        tree = self.finished()
        self.git(tree, 'checkout', '-q', '--detach')
        lost = self.commit(tree, 'made-with-no-branch.txt')                    # only this worktree's HEAD log names it
        self.git(tree, 'checkout', '-q', 'fix-1')
        self.assertKept(self.planned(), tree, 'commits only in its reflog', lost[:10])

    def test_a_commit_thrown_away_on_the_branch_stays_in_the_clone(self):
        tree = self.finished()
        merged = self.head(tree)
        thrown = self.commit(tree, 'thrown-away.txt')
        self.git(tree, 'reset', '-q', '--hard', merged)
        cfg = self.repo_cfg()
        self.assertDeletes(self.plan(cfg), tree)             # the branch's own log names it, and that log stays
        self.assertEqual(self.run_cleaner(cfg)[0], 0)
        self.assertFalse(os.path.exists(tree))
        self.assertEqual(self.git(self.path('trees', 'base'), 'cat-file', '-t', thrown), 'commit')
        self.assertIn(thrown, self.git(self.path('trees', 'base'), 'log', '-g', '--format=%H', 'fix-1'))

    def test_a_locked_or_detached_worktree_is_kept(self):
        locked, detached = self.finished('locked', number=1), self.finished('detached', number=2)
        self.git(locked, 'worktree', 'lock', '--reason', 'under review', locked)
        self.git(detached, 'checkout', '-q', '--detach')
        plan = self.planned()
        self.assertKept(plan, locked, 'locked', 'under review')
        self.assertKept(plan, detached, 'no branch')

    def test_another_git_folder_inside_keeps_it(self):
        tree = self.finished()
        self.clone('vendored', into=self.mkdir(tree, 'node_modules'))
        self.assertKept(self.planned(), tree, 'another git folder inside', 'node_modules/vendored')

    def test_only_folders_of_the_listed_repository_are_looked_at(self):
        other = self.clone('other-project', repo='acme/other')
        side = self.worktree(other, 'other-side')
        self.commit(side)
        self.push(side)
        self.pr(side, 9)
        plain = self.mkdir('trees', 'notes')
        os.symlink(self.finished(), self.path('trees', 'link'))
        outside = self.finished('outside', clone=self.path('trees', 'base'), number=3)
        shutil.move(outside, self.mkdir('elsewhere'))
        plan = self.planned()
        for path in (other, side, plain, self.path('trees', 'link'), self.path('elsewhere', 'outside')):
            self.assertNotCandidate(plan, path)
        self.assertDeletes(plan, self.path('trees', 'fix-1'))
        self.assertEqual(self.plan(self.cfg(worktrees={'repos': [{'root': self.path('no-such-root'), 'github': fx.REPO}]})).items, [])

    def test_everything_is_read_again_just_before_the_delete(self):
        tree = self.finished()
        cfg = self.repo_cfg()
        facts = self.facts(cfg)
        plan = self.c.rules.plan(cfg, facts)
        self.assertDeletes(plan, tree)
        self.file(os.path.join(tree, 'written-after-the-plan.txt'))
        lines = []
        self.c.engine.delete_all(cfg, facts, plan.items, True, lines.append)
        self.assertTrue(os.path.isfile(os.path.join(tree, 'written-after-the-plan.txt')))
        self.assertEqual([line.split()[0] for line in lines], ['REFUSED'], lines)
        self.assertIn('uncommitted or untracked', lines[0])

    def test_git_removes_it_without_force(self):
        tree = self.finished()
        cfg = self.repo_cfg()
        facts = self.facts(cfg)
        plan = self.c.rules.plan(cfg, facts)
        self.file(os.path.join(tree, 'written-after-the-last-check.txt'))
        lines = []
        with mock.patch.object(self.c.rules, 'recheck', lambda *a: None):          # as if the last check had passed
            self.c.engine.delete_all(cfg, facts, plan.items, True, lines.append)
        self.assertTrue(os.path.isfile(os.path.join(tree, 'written-after-the-last-check.txt')))
        self.assertEqual([line.split()[0] for line in lines], ['FAILED'], lines)


class Clones(Case):
    def finished_clone(self, name='base', number=20, hours=48):
        clone = self.clone(name)
        self.git(clone, 'checkout', '-q', '-b', f'{name}-work')
        self.commit(clone)
        self.push(clone)
        self.pr(clone, number, hours)
        return clone

    def test_a_finished_clone_is_removed(self):
        clone = self.finished_clone()
        cfg = self.repo_cfg()
        item = self.assertDeletes(self.plan(cfg), clone, 'worktrees')
        self.assertEqual(item.kind, 'clone')
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 0, lines)
        self.assertFalse(os.path.exists(clone))

    def test_a_clone_goes_only_after_its_worktrees(self):
        clone = self.finished_clone()
        tree = self.finished(clone=clone)
        cfg = self.repo_cfg()
        plan = self.plan(cfg)
        self.assertDeletes(plan, tree)
        self.assertKept(plan, clone, 'worktrees hang off it', '1')
        self.assertEqual(self.run_cleaner(cfg)[0], 0)
        self.assertTrue(os.path.isdir(clone))
        self.assertFalse(os.path.exists(tree))
        self.assertDeletes(self.plan(cfg), clone)                # the next run: its worktrees are gone
        self.assertEqual(self.run_cleaner(cfg)[0], 0)
        self.assertFalse(os.path.exists(clone))

    def test_a_worktree_anywhere_keeps_the_clone(self):
        clone = self.finished_clone()
        self.worktree(clone, 'far-away', into=self.mkdir('elsewhere'))
        self.assertKept(self.planned(), clone, 'worktrees hang off it')

    def test_a_worktree_whose_folder_is_gone_does_not(self):
        clone = self.finished_clone()
        tree = self.finished(clone=clone)
        shutil.rmtree(tree)
        self.assertDeletes(self.planned(), clone)

    def test_the_same_checks_as_a_worktree(self):
        dirty, open_pr, no_pr = (self.finished_clone(n, number=i) for i, n in enumerate(('dirty', 'open', 'none')))
        self.file(os.path.join(dirty, 'new.txt'))
        self.pr(open_pr, 50, state='open')
        self.merged[:] = [row for row in self.merged if row['headRefName'] != 'none-work']
        on_main = self.clone('on-main')
        plan = self.planned()
        self.assertKept(plan, dirty, 'uncommitted or untracked')
        self.assertKept(plan, open_pr, 'open PR')
        self.assertKept(plan, no_pr, 'no merged PR')
        self.assertKept(plan, on_main, 'no merged PR')

    def test_stash_entries_keep_the_clone(self):
        clone = self.finished_clone()
        self.file(os.path.join(clone, 'README.md'), 'half done\n')
        self.git(clone, 'stash')
        self.assertKept(self.planned(), clone, 'stash entries')

    def test_a_commit_on_any_branch_that_github_does_not_have_keeps_the_clone(self):
        clone = self.finished_clone()
        work = self.branch(clone)
        self.git(clone, 'checkout', '-q', '-b', 'idea', 'origin/main')
        lonely = self.commit(clone, 'idea.txt')
        self.git(clone, 'checkout', '-q', work)
        self.assertKept(self.planned(), clone, 'commits GitHub does not have', lonely[:10])
        self.merged.append({'number': 30, 'headRefName': 'idea', 'headRefOid': lonely, 'mergedAt': self.merged[0]['mergedAt']})
        self.assertDeletes(self.planned(), clone)               # it is a pull request's head: GitHub has it

    def test_a_commit_only_a_reflog_still_has_keeps_the_clone(self):
        clone = self.finished_clone()
        merged = self.head(clone)
        self.commit(clone, 'thrown-away.txt')
        self.git(clone, 'reset', '-q', '--hard', merged)
        self.assertKept(self.planned(), clone, 'commits GitHub does not have')


class GitHub(Case):
    """The reader of the pull request lists (facts.github_prs), against a stub `gh`."""
    ROWS = {'merged': [{'number': 7, 'headRefName': 'fix-1', 'headRefOid': 'a' * 40, 'mergedAt': '2026-01-02T03:04:05Z'}],
            'open': [{'number': 8, 'headRefName': 'fix-2', 'headRefOid': 'b' * 40, 'mergedAt': None}]}

    def gh(self, state):
        exe, self.state = self.stub('gh', fx.STUB_GH, state)
        patch = mock.patch.object(self.c.facts, 'GH', exe)
        patch.start()
        self.addCleanup(patch.stop)

    def test_it_reads_merged_and_open_pull_requests_of_the_listed_repository(self):
        self.gh(self.ROWS)
        self.assertEqual(self.c.facts.github_prs(fx.REPO), self.ROWS)
        calls = self.calls(self.state)
        self.assertEqual(sorted(c[c.index('--state') + 1] for c in calls), ['merged', 'open'])
        for call in calls:
            self.assertEqual(call[:2], ['pr', 'list'])
            self.assertEqual(call[call.index('-R') + 1], fx.REPO)

    def test_no_answer_or_a_strange_answer_means_keep(self):
        for state in ({'fail': True}, {'raw': 'not json'}, {'raw': '{"message": "rate limited"}'},
                      {'merged': [{'number': 7}], 'open': []}, {'merged': [], 'open': ['x']},
                      {'merged': [{**self.ROWS['merged'][0], 'headRefOid': 'HEAD; rm'}], 'open': []}):
            with self.subTest(state=state):
                self.gh(state)
                with self.assertRaises(self.c.facts.Unreadable):
                    self.c.facts.github_prs(fx.REPO)
        tree = self.finished()
        self.assertKept(self.planned(prs=self.c.facts.github_prs), tree, 'could not read', 'GitHub')

    def test_a_list_that_may_be_cut_off_means_keep(self):
        self.gh({'merged': self.ROWS['merged'], 'open': self.ROWS['open'] * 3})
        with mock.patch.object(self.c.facts, 'PR_LIMIT', 3):
            with self.assertRaises(self.c.facts.Unreadable):
                self.c.facts.github_prs(fx.REPO)
        with mock.patch.object(self.c.facts, 'PR_LIMIT', 4):
            self.assertEqual(len(self.c.facts.github_prs(fx.REPO)['open']), 3)


MUTATIONS = {
    'open PR': ('rules', 'if opened:', 'if False:', Worktrees, 'test_an_open_pull_request_keeps_it'),
    'no merged PR': ('rules', 'if not merged:', 'if False:', Worktrees, 'test_no_merged_pull_request_keeps_it'),
    'merged under 24 h ago': ('rules', 'if age < cfg.merged_hours * 3600:', 'if False:', Worktrees, 'test_merged_less_than_a_day_ago_keeps_it'),
    'the 24 h floor': ('config', "'mergedHours': (int, MIN_MERGED_HOURS, _at_least(MIN_MERGED_HOURS))", "'mergedHours': (int, MIN_MERGED_HOURS, None)",
                       Worktrees, 'test_merged_less_than_a_day_ago_keeps_it'),
    'an unreadable merge time': ('facts', 'return None  # no merge time', 'return 0',
                                 Worktrees, 'test_a_merge_time_it_cannot_read_keeps_it'),
    'uncommitted or untracked': ('rules', 'if changed:', 'if False:', Worktrees, 'test_uncommitted_or_untracked_files_keep_it'),
    'commits not in the merged PR': ('rules', "if git_code(path, 'merge-base', '--is-ancestor', head, oid) != 0:", 'if False:',
                                     Worktrees, 'test_commits_the_merged_pull_request_does_not_have_keep_it'),
    'a PR head that is not here': ('rules', 'if oid not in here:', 'if False:', Worktrees, 'test_a_pull_request_head_it_cannot_compare_with_keeps_it'),
    'a process inside': ('rules', 'look = guard.examine(cfg, facts, path, kind)', 'look = guard.Look()', Worktrees, 'test_a_process_inside_keeps_it'),
    'GitHub down': ('rules', "return kind, Refusal('could not read', str(ex)), '', 0  # GitHub", 'prs = EMPTY_PRS', Worktrees, 'test_github_down_keeps_everything'),
    'git cannot answer': ('rules', "return kind, Refusal('could not read', f'git: {ex}'), '', 0  # whose it is", 'return None',
                          Worktrees, 'test_git_that_cannot_answer_keeps_it'),
    'git stops answering half-way': ('rules', "refusal = Refusal('could not read', f'git: {ex}')", 'refusal = None',
                                     Worktrees, 'test_git_that_stops_answering_half_way_keeps_it'),
    'ignored: a credential name is never build output': ('rules', 'if guard.matches(parts[-1], CREDENTIAL_NAMES):', 'if False:',
                                                         Worktrees, 'test_no_list_makes_a_credential_looking_file_build_output'),
    'ignored files': ('rules', 'if precious:', 'if False:', Worktrees, 'test_ignored_files_that_are_not_build_output_keep_it'),
    'hidden from git status': ('rules', 'if hidden:', 'if False:', Worktrees, 'test_files_hidden_from_git_status_keep_it'),
    'unfinished git operation': ('rules', 'if marks:', 'if False:', Worktrees, 'test_an_unfinished_git_operation_keeps_it'),
    'refs of its own': ('rules', 'if own:', 'if False:', Worktrees, 'test_refs_of_its_own_keep_it'),
    'commits only in its reflog': ('rules', 'if alone:  # reflog', 'if False:', Worktrees, 'test_a_commit_only_its_reflog_still_has_keeps_it'),
    'locked': ('rules', "if entry.get('locked') is not None:", 'if False:', Worktrees, 'test_a_locked_or_detached_worktree_is_kept'),
    'no branch': ('rules', 'if not branch:', 'if False:', Worktrees, 'test_a_locked_or_detached_worktree_is_kept'),
    'another git folder inside': ('guard', 'if nested:', 'if False:', Worktrees, 'test_another_git_folder_inside_keeps_it'),
    'only the listed repository': ('rules', 'if not same_repo(url, repo.github):', 'if False:', Worktrees, 'test_only_folders_of_the_listed_repository_are_looked_at'),
    'the last check before a delete': ('engine', 'refusal = rules.recheck(cfg, facts, item)', 'refusal = None', Worktrees, 'test_everything_is_read_again_just_before_the_delete'),
    'no --force': ('facts', "'worktree', 'remove', path,", "'worktree', 'remove', '--force', path,", Worktrees, 'test_git_removes_it_without_force'),
    'clone: worktrees first': ('rules', 'if others:', 'if False:', Clones, 'test_a_clone_goes_only_after_its_worktrees'),
    'clone: a worktree outside the root': ('rules', 'if others:', 'if [o for o in others if os.path.dirname(o) == repo.root]:', Clones, 'test_a_worktree_anywhere_keeps_the_clone'),
    'clone: stash entries': ('rules', "if git_code(path, 'rev-parse', '-q', '--verify', 'refs/stash') == 0:", 'if False:', Clones, 'test_stash_entries_keep_the_clone'),
    'clone: commits GitHub does not have': ('rules', 'if alone:  # clone', 'if False:', Clones, 'test_a_commit_on_any_branch_that_github_does_not_have_keeps_the_clone'),
    'clone: reflog commits': ('rules', "lonely(path, ['--all', '--reflog'], has)", "lonely(path, ['--all'], has)", Clones, 'test_a_commit_only_a_reflog_still_has_keeps_the_clone'),
    'GitHub: a failed gh': ('facts', 'if done.returncode != 0:  # gh', 'if False:', GitHub, 'test_no_answer_or_a_strange_answer_means_keep'),
    'GitHub: a strange row': ('facts', 'if not valid_pr(row, state):', 'if False:', GitHub, 'test_no_answer_or_a_strange_answer_means_keep'),
    'GitHub: a cut-off list': ('facts', "if state == 'open' and len(rows) >= PR_LIMIT:", 'if False:', GitHub, 'test_a_list_that_may_be_cut_off_means_keep'),
}


class RedCheck(unittest.TestCase):
    def test_each_refusal_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
