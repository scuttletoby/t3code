"""The `worktrees` rule (lib/cleaner/rules.py): git worktrees and clones of a listed repository whose pull request
merged a day or more ago and that hold nothing GitHub does not have. Real git in a temporary folder; "GitHub" is a
bare repository plus pull request rows the test writes. RedCheck breaks each refusal in a copy of the code and
shows its test then fails.

  python3 -I -B fork-kit/cleaner/tests/worktrees.test.py
"""
import os, re, shutil, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cleaner_fixture as fx  # noqa: E402

import machine_file  # noqa: E402


STUB_GIT = r'''#!{python} -I
import json, os, sys
with open({state!r}) as f:
    state = json.load(f)
if 'worktree' in sys.argv and 'remove' in sys.argv:
    if state['remove'] == 'killed':
        os.kill(os.getpid(), 9)
    sys.stderr.write("fatal: failed to delete '" + sys.argv[-1] + "': Device or resource busy\n")
    sys.exit(128)
os.execv(state['git'], [state['git']] + sys.argv[1:])
'''


class Case(fx.Case):
    c = fx.load()

    def planned(self, **over):
        return self.plan(self.repo_cfg(), **over)

    def swap_after_the_last_check(self, path, swap):
        """rules.recheck as it is, then `swap(path)`: the folder changes between the last check and the delete."""
        real = self.c.rules.recheck

        def recheck(cfg, facts, item):
            refusal = real(cfg, facts, item)
            if refusal is None and item.path == path:
                swap(path)
            return refusal
        return mock.patch.object(self.c.rules, 'recheck', recheck)

    def precious(self):
        return self.file(self.path('home', '.claude', 'projects', 'p', 'session.jsonl'), 'transcript\n')

    def assertNothingRemoved(self, code, lines, *texts):
        self.assertEqual(code, 1, lines)
        failed = [line for line in lines if line.startswith('FAILED ')]
        self.assertEqual(len(failed), 1, lines)
        self.assertIn('is not what the last check saw', failed[0])
        self.assertNotIn('half deleted', failed[0])
        for path, text in texts:
            with open(path) as f:
                self.assertEqual(f.read(), text)

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
        plan = self.plan(self.repo_cfg(regenerable=['local']))            # a project names its own build folders
        self.assertDeletes(plan, notes)
        self.assertDeletes(plan, built)                                   # ...on top of the built-in names
        self.assertKept(plan, log, 'ignored files')
        plan = self.plan(self.repo_cfg(regenerable=['debug.log']))        # a name frees folders, never a file
        self.assertKept(plan, log, 'ignored files', 'debug.log')

    def test_the_list_takes_only_plain_names(self):
        """The list can only free more: a pattern such as * would make every ignored file build output."""
        for name in ('*', '*.log', '.*', 'a?', '[ab]', 'build/out', 'a\\b', ''):
            with self.subTest(name=name), self.assertRaises(machine_file.MachineFileError):
                self.repo_cfg(regenerable=[name])
        self.assertIn('dist', self.repo_cfg(regenerable=['dist']).repos[0].regenerable)

    def test_no_list_makes_a_credential_looking_file_build_output(self):
        tree = self.finished()
        self.file(os.path.join(tree, '.env'), 'made up for the test\n')
        self.assertEqual(self.git(tree, 'status', '--porcelain'), '')         # git ignores it
        for regenerable in (None, ['.env']):
            self.assertKept(self.plan(self.repo_cfg(regenerable=regenerable)), tree, 'ignored files', '.env')
        os.unlink(os.path.join(tree, '.env'))
        self.file(os.path.join(tree, '.env', 'production'), 'made up for the test\n')     # a folder of them
        self.assertEqual(self.git(tree, 'status', '--porcelain'), '')
        self.assertKept(self.plan(self.repo_cfg(regenerable=['.env'])), tree, 'ignored files', '.env')

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

    def test_a_worktree_whose_branch_has_no_commit_is_kept(self):
        tree = self.finished('gone', number=3)
        self.git(tree, 'update-ref', '-d', 'refs/heads/gone')        # the branch deleted under the worktree
        self.assertKept(self.planned(), tree, 'no commit', 'gone has no commit')

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
        with mock.patch.object(self.c.rules, 'recheck', lambda *a: None):          # as if the last check had passed
            failed, lines = self.delete_all(cfg, facts, plan.items)
        self.assertTrue(os.path.isfile(os.path.join(tree, 'written-after-the-last-check.txt')))
        self.assertEqual([line.split()[0] for line in lines], ['FAILED'], lines)
        self.assertNotIn('half deleted', lines[0])                     # git removed nothing: no note for the next run
        self.assertFalse(os.path.exists(cfg.journal_file))

    def test_a_fork_pull_request_with_the_same_branch_name_does_not_finish_it(self):
        tree = self.finished()
        self.merged[0]['isCrossRepository'] = True
        self.assertKept(self.planned(), tree, 'no merged PR')

    def test_a_branch_pull_requests_merge_into_is_kept(self):
        lower = self.finished('lower', number=1)
        self.finished('upper', number=2)
        self.merged[1]['baseRefName'] = 'lower'
        self.assertKept(self.planned(), lower, 'base branch', '#2')

    def test_a_worktree_that_was_moved_is_kept(self):
        tree = self.finished()
        os.rename(tree, self.path('trees', 'moved'))
        self.assertKept(self.planned(), self.path('trees', 'moved'), 'could not read', 'moved')

    def test_a_worktree_whose_main_repository_is_gone_is_kept_as_orphaned(self):
        """Its .git file names a git folder that is gone: git can never answer for it. It is kept, under its own name
        (no "could not read", which would mark every daily run failed)."""
        clone = self.clone('elsewhere-base', into=self.mkdir('elsewhere'))
        tree = self.finished(clone=clone)
        shutil.rmtree(clone)
        plan = self.planned()
        self.assertKept(plan, tree, 'orphaned worktree', 'is gone (the main repository was removed)')
        self.assertTrue(os.path.isdir(tree))

    def test_the_last_check_wants_the_same_kind(self):
        tree = self.finished()
        cfg = self.repo_cfg()
        self.assertIsNone(self.c.rules.recheck(cfg, self.facts(cfg), self.c.rules.Item('worktree', tree, 'worktrees', '', 0)))
        refusal = self.c.rules.recheck(cfg, self.facts(cfg), self.c.rules.Item('clone', tree, 'worktrees', '', 0))
        self.assertEqual(refusal.code, 'not a candidate')

    def test_a_folder_it_cannot_read_is_kept_and_named(self):
        tree = self.finished()
        closed = self.mkdir('trees', 'closed', 'inner')
        os.chmod(os.path.dirname(closed), 0)
        cfg = self.repo_cfg()
        self.assertKept(self.plan(cfg), os.path.dirname(closed), 'could not read', 'Permission denied')
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 1, lines)
        self.assertFalse(os.path.exists(tree))

    def test_a_worktree_swapped_after_the_last_check_is_never_followed(self):
        """The lens probe for worktrees: after the last check, the folder is replaced by a link to another worktree
        that must stay (an open pull request), or to a must-keep folder. Git is never asked to remove it."""
        tree = self.finished()
        other = self.finished('fix-2', number=8)
        self.pr(other, 9, state='open')
        mark = self.file(os.path.join(other, 'kept.txt'), 'other work\n')
        precious = self.precious()
        cfg = self.repo_cfg()
        for target in (other, os.path.dirname(precious)):
            with self.subTest(target=target):
                def swap(path):
                    os.rename(path, self.path('moved'))
                    os.symlink(target, path)
                with self.swap_after_the_last_check(tree, swap):
                    code, lines = self.run_cleaner(cfg)
                self.assertNothingRemoved(code, lines, (mark, 'other work\n'), (precious, 'transcript\n'))
                self.assertIn(other, self.git(other, 'worktree', 'list', '--porcelain'))
                os.unlink(tree)
                os.rename(self.path('moved'), tree)

    def test_git_that_cannot_find_the_clone_removes_nothing(self):
        """The last step before git removes a worktree asks git where its clone is: when that fails, nothing was
        touched, so it is no "half deleted" but a plain "could not read"."""
        tree = self.finished()
        cfg = self.repo_cfg()
        facts = self.facts(cfg)
        plan = self.c.rules.plan(cfg, facts)
        real = self.c.rules.git

        def git(where, *args, **kw):
            if '--git-common-dir' in args:
                raise self.c.facts.Unreadable('exit 128: fatal: not a git repository')
            return real(where, *args, **kw)
        with mock.patch.object(self.c.rules, 'recheck', lambda *a: None), mock.patch.object(self.c.rules, 'git', git):
            failed, lines = self.delete_all(cfg, facts, plan.items)
        self.assertEqual([line.split()[0] for line in lines], ['FAILED'], lines)
        self.assertIn('could not read', lines[0])
        self.assertNotIn('half deleted', lines[0])
        self.assertFalse(os.path.exists(cfg.journal_file))
        self.assertTrue(os.path.isdir(tree))

    def test_a_git_remove_that_was_killed_or_failed_half_way_is_half_deleted(self):
        """Git stops with exit 128 both when a check refuses (nothing removed) and when deleting a file failed (some
        removed); a killed git has no exit code of its own. Only the first is "nothing removed"."""
        cfg = self.repo_cfg()
        for how in ('killed', 'failed to delete'):
            with self.subTest(how=how):
                tree = self.finished(f'fix-{len(how)}', number=len(how))
                exe, state = self.stub('git', STUB_GIT, {'remove': how, 'git': shutil.which('git')})
                with mock.patch.object(self.c.facts, 'GIT', exe):
                    code, lines = self.run_cleaner(cfg)
                self.assertEqual(code, 1, lines)
                failed = [line for line in lines if line.startswith('FAILED ')]
                self.assertEqual(len(failed), 1, lines)
                self.assertIn('half deleted', failed[0])
                self.assertEqual([entry['path'] for entry in self.c.engine.read_journal(cfg)], [tree])
                os.unlink(cfg.journal_file)
                self.git(tree, 'worktree', 'remove', tree)

    def test_the_pull_request_lists_are_read_for_the_plan_and_again_before_each_delete(self):
        self.finished()
        self.finished('fix-2', number=8)
        cfg = self.repo_cfg()
        reads = []

        def prs(repo):
            reads.append(repo)
            return {'merged': list(self.merged), 'open': list(self.open)}
        code, lines = self.run_cleaner(cfg, prs=prs)
        self.assertEqual(code, 0, lines)
        self.assertEqual(reads, [fx.REPO] * 3)                  # the plan, then the last check of each of the two

    def test_a_deinitialised_submodule_in_a_worktree_keeps_it(self):
        tree = self.finished()
        self.origin('acme/sub')
        self.git(tree, 'submodule', 'add', '-q', 'https://github.com/acme/sub.git', 'sub')
        self.git(tree, 'commit', '-q', '-m', 'add sub')
        self.push(tree)
        self.merged[0]['headRefOid'] = self.head(tree)
        lost = self.commit(os.path.join(tree, 'sub'), 'only-here.txt')
        self.git(tree, 'submodule', 'deinit', '-q', '-f', 'sub')
        self.assertEqual(os.listdir(os.path.join(tree, 'sub')), [])
        self.assertKept(self.planned(), tree, 'git: unpushed commits', lost[:10])
        modules = os.path.join(self.git_dir(tree), 'modules', 'sub')
        self.git(modules, 'push', '-q', 'origin', f'{lost}:refs/heads/kept')
        self.assertDeletes(self.planned(), tree)


class Used(Case):
    """The refusal "used under N h ago", with the real reader: files, the git files a command writes, and the
    times in the HEAD reflog. Commits are made with a committer time three days back, so their reflog lines are old."""

    def setUp(self):
        super().setUp()
        real = mock.patch.object(self.c.rules, 'last_used', self.real_last_used)
        real.start()
        self.addCleanup(real.stop)
        os.environ['GIT_COMMITTER_DATE'] = f'@{int(self.now - 3 * fx.DAY)} +0000'

    def idle(self, tree, days=3):
        """Nothing in the tree or its own git files changed for `days` days."""
        self.age(tree, days)
        git_dir = self.git_dir(tree)
        for name in self.c.rules.USE_MARKS + ('logs/HEAD',):
            if os.path.exists(os.path.join(git_dir, name)):
                self.age(os.path.join(git_dir, name), days)
        return tree

    def test_a_worktree_used_in_the_last_day_is_kept(self):
        tree = self.finished()
        self.assertKept(self.planned(), tree, 'used under 24 h ago')
        self.idle(tree)
        self.assertDeletes(self.planned(), tree)
        self.idle(tree, days=1.5)
        plan = self.plan(self.cfg(worktrees={'repos': [{'root': self.trees, 'github': fx.REPO}], 'usedHours': 48}))
        self.assertKept(plan, tree, 'used under 48 h ago')
        with self.assertRaises(machine_file.MachineFileError):
            self.cfg(worktrees={'usedHours': 23})

    def test_each_sign_of_use_keeps_it(self):
        tree = self.finished()
        git_dir = self.git_dir(tree)
        head = self.head(tree)
        fresh_line = f'{head} {head} test <test@example.invalid> {int(self.now - 3600)} +0000\tcheckout: moving\n'
        signs = {'a file in it': lambda: os.utime(os.path.join(tree, 'README.md')),
                 'its HEAD reflog': lambda: self.file(os.path.join(git_dir, 'logs', 'HEAD'), fresh_line) and
                 self.age(os.path.join(git_dir, 'logs', 'HEAD'), 3)}
        for name in self.c.rules.USE_MARKS:
            signs[f'git {name}'] = lambda name=name: (self.file(os.path.join(git_dir, name), head + '\n') if not
                                                      os.path.exists(os.path.join(git_dir, name)) else None,
                                                      os.utime(os.path.join(git_dir, name)))
        for what, use in signs.items():
            with self.subTest(sign=what):
                self.idle(tree)
                self.assertDeletes(self.planned(), tree)
                use()
                self.assertKept(self.planned(), tree, 'used under 24 h ago', what)
                if what == 'its HEAD reflog':     # the line stays; take it away again for the next sign
                    self.git(tree, 'reflog', 'expire', '--expire=now', '--all')

    def test_a_rewritten_reflog_file_is_not_use(self):
        tree = self.idle(self.finished())
        os.utime(os.path.join(self.git_dir(tree), 'logs', 'HEAD'))     # `git gc` rewrote it; its lines are old
        self.assertDeletes(self.planned(), tree)

    def test_a_reflog_line_it_cannot_read_keeps_it(self):
        tree = self.finished()
        with open(os.path.join(self.git_dir(tree), 'logs', 'HEAD'), 'a') as f:
            f.write('not a reflog line\n')
        self.idle(tree)
        self.assertKept(self.planned(), tree, 'could not read', 'reflog')

    def test_a_clone_used_in_the_last_day_is_kept(self):
        clone = self.clone('solo')
        self.git(clone, 'checkout', '-q', '-b', 'solo-work')
        self.commit(clone)
        self.push(clone)
        self.pr(clone, 20, 48)
        self.assertKept(self.planned(), clone, 'used under 24 h ago')
        self.idle(clone)
        self.assertDeletes(self.planned(), clone)
        os.utime(os.path.join(clone, 'README.md'))
        self.assertKept(self.planned(), clone, 'used under 24 h ago', 'a file in it')


class Branches(Case):
    def test_only_listed_branches_are_removed(self):
        clone = self.clone('base')
        trees = {}
        for name, branch in (('a', 'work/a'), ('b', 'fix-b'), ('c', 'work/nested/c')):
            tree = trees[branch] = self.worktree(clone, name, branch)
            self.commit(tree)
            self.push(tree)
            self.pr(tree, len(trees), 48)
        cfg = self.cfg(worktrees={'repos': [{'root': self.trees, 'github': fx.REPO, 'branches': ['work/*']}]})
        plan = self.plan(cfg)
        self.assertDeletes(plan, trees['work/a'])
        self.assertDeletes(plan, trees['work/nested/c'])      # * crosses /, as in a shell glob of a branch name
        self.assertKept(plan, trees['fix-b'], 'branch not listed', 'work/*')
        plan = self.planned()                                 # no list: any branch
        self.assertDeletes(plan, trees['fix-b'])
        for bad in ([''], ['ok', 3], 'work/*'):
            with self.subTest(bad=bad), self.assertRaises(machine_file.MachineFileError):
                self.cfg(worktrees={'repos': [{'root': self.trees, 'github': fx.REPO, 'branches': bad}]})


class MergedBranches(Case):
    """`mergedBranches` (#519): a local branch of a clone in the root, named by a `branches` pattern, whose pull
    request merged and whose worktree is gone already. It goes only when its tip is in the merged pull request's head
    or in GitHub's default branch, and every commit it and its reflog name is on GitHub right now."""

    def branch_cfg(self, branches=('work/*',), on=True):
        repo = {'root': self.trees, 'github': fx.REPO, 'branches': list(branches)}
        if on is not None:
            repo['mergedBranches'] = on
        return self.cfg(worktrees={'repos': [repo]})

    def base(self):
        return self.path('trees', 'base') if os.path.isdir(self.path('trees', 'base')) else self.clone('base')

    def merged_branch(self, name='work/a', number=7, hours=48, then=None):
        """A branch of trees/base, pushed, whose pull request merged `hours` ago; its worktree (outside the root) was
        removed, as a run removes it. `then(tree)` runs before that."""
        clone = self.base()
        tree = self.worktree(clone, name.replace('/', '-'), name, into=self.mkdir('elsewhere'))
        self.commit(tree)
        self.push(tree)
        self.pr(tree, number, hours)
        if then is not None:
            then(tree)
        self.git(clone, 'worktree', 'remove', '--force', tree)
        return clone

    def branches(self, clone):
        return sorted(self.git(clone, 'for-each-ref', '--format=%(refname:short)', 'refs/heads').split())

    def label(self, clone, name):
        return f'branch {name} in {clone}'

    def branch_items(self, plan):
        return [(i.path, i.branch) for i in plan.items if i.kind == 'branch']

    def test_a_merged_branch_with_no_worktree_is_deleted_and_a_second_run_finds_nothing(self):
        clone = self.merged_branch()
        self.git(clone, 'branch', 'other')                          # no pattern names it: never looked at
        cfg = self.branch_cfg()
        plan = self.plan(cfg)
        self.assertEqual(self.branch_items(plan), [(clone, 'work/a')])
        self.assertEqual([k.code for k in plan.kept if k.path == clone], ['branch not listed'])   # the clone stays (main)
        self.assertEqual([k.path for k in plan.kept if k.path.startswith('branch ')], [])
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 0, lines)
        self.assertIn(f'deleted {self.label(clone, "work/a")} (0 B, worktrees: #7 merged 2 days ago', '\n'.join(lines))
        self.assertIn('  worktrees: deleted 1 branch (0 B)', lines)
        self.assertEqual(self.branches(clone), ['main', 'other'])
        self.assertFalse(os.path.lexists(os.path.join(clone, '.git', 'logs', 'refs', 'heads', 'work', 'a')))
        self.assertNotIn('branch.work/a.', self.git(clone, 'config', '--local', '--list'))     # its upstream too
        code, lines = self.run_cleaner(cfg)                         # runs again: nothing left to do
        self.assertEqual(code, 0, lines)
        self.assertEqual([line for line in lines if line.startswith('deleted ')], [])
        self.assertEqual(self.branches(clone), ['main', 'other'])

    def test_without_the_key_no_branch_is_looked_at(self):
        """An older machine file (no mergedBranches) deletes no branch after an update; the key needs patterns."""
        clone = self.merged_branch()
        for cfg in (self.branch_cfg(on=None), self.branch_cfg(on=False), self.repo_cfg()):
            plan = self.plan(cfg)
            self.assertEqual(self.branch_items(plan), [])
            self.assertEqual([k.path for k in plan.kept if k.path.startswith('branch ')], [])
            self.assertEqual(self.run_cleaner(cfg)[0], 0)
            self.assertIn('work/a', self.branches(clone))
        for bad in ({'branches': [], 'on': True}, {'on': 'yes'}):
            with self.subTest(bad=bad), self.assertRaises(machine_file.MachineFileError):
                self.branch_cfg(bad.get('branches', ('work/*',)), bad['on'])
        with self.assertRaises(machine_file.MachineFileError):
            self.cfg(worktrees={'repos': [{'root': self.trees, 'github': fx.REPO, 'mergedBranches': True}]})

    def test_a_branch_checked_out_in_a_worktree_goes_in_a_later_run_than_it(self):
        tree = self.finished('fix-1')
        clone = self.path('trees', 'base')
        cfg = self.branch_cfg(['fix-*'])
        plan = self.plan(cfg)
        self.assertDeletes(plan, tree)
        self.assertEqual(self.branch_items(plan), [])               # the worktree's: not listed as a branch
        self.assertEqual(self.run_cleaner(cfg)[0], 0)
        self.assertFalse(os.path.exists(tree))
        self.assertIn('fix-1', self.branches(clone))                # git worktree remove leaves the branch
        self.assertEqual(self.branch_items(self.plan(cfg)), [(clone, 'fix-1')])
        self.assertEqual(self.run_cleaner(cfg)[0], 0)
        self.assertEqual(self.branches(clone), ['main'])

    def test_a_branch_a_worktree_rebases_or_bisects_is_not_a_candidate(self):
        clone = self.merged_branch()
        other = self.worktree(clone, 'other', 'side', into=self.mkdir('elsewhere'))
        git_dir = self.git_dir(other)
        for name in ('rebase-merge/head-name', 'rebase-apply/head-name', 'BISECT_START'):
            with self.subTest(name=name):
                self.file(os.path.join(git_dir, name), 'refs/heads/work/a\n' if 'rebase' in name else 'work/a\n')
                self.assertEqual(self.branch_items(self.plan(self.branch_cfg())), [])
                os.unlink(os.path.join(git_dir, name))
        self.assertEqual(self.branch_items(self.plan(self.branch_cfg())), [(clone, 'work/a')])

    def test_each_reason_keeps_the_branch(self):
        clone = self.merged_branch('work/done', number=1)
        self.merged_branch('work/open', number=2)
        self.pr(clone, 3, state='open', branch='work/open', oid=self.git(clone, 'rev-parse', 'work/open'))
        self.merged_branch('work/fresh', number=4, hours=2)
        self.merged_branch('work/fork', number=5)
        self.merged[-1]['isCrossRepository'] = True
        self.merged_branch('work/lower', number=6)
        self.pr(clone, 8, branch='work/upper', base='work/lower', oid=self.git(clone, 'rev-parse', 'work/lower'))
        self.merged_branch('work/later', number=9, then=lambda tree: self.commit(tree, 'later.txt'))

        def reset_away(tree):
            lost = self.commit(tree, 'lost.txt')                    # never pushed, then reset away: only in its reflog
            self.git(tree, 'reset', '-q', '--hard', 'HEAD~1')
            self.assertNotIn(lost, self.git(tree, 'ls-remote', 'origin'))
        self.merged_branch('work/reflog', number=10, then=reset_away)
        self.git(clone, 'branch', 'work/none')                      # no pull request
        plan = self.plan(self.branch_cfg())
        self.assertEqual(self.branch_items(plan), [(clone, 'work/done')])
        self.assertKept(plan, self.label(clone, 'work/open'), 'open PR', '#3')
        self.assertKept(plan, self.label(clone, 'work/fresh'), 'merged under 24 h ago', '#4')
        self.assertKept(plan, self.label(clone, 'work/fork'), 'no merged PR')
        self.assertKept(plan, self.label(clone, 'work/lower'), 'base branch', '#8')
        self.assertKept(plan, self.label(clone, 'work/later'), 'commits not in the merged PR', '#9')
        self.assertKept(plan, self.label(clone, 'work/reflog'), 'commits GitHub does not have', 'reflog')
        self.assertKept(plan, self.label(clone, 'work/none'), 'no merged PR')
        self.git(clone, 'checkout', '-q', '-b', 'local')               # main checked out nowhere
        self.assertKept(self.plan(self.branch_cfg(['*'])), self.label(clone, 'main'), 'base branch', 'default branch')

    def test_a_tip_in_githubs_default_branch_may_go(self):
        """Squash or rebase merges: the pull request's head is a commit this clone never had, but GitHub's default
        branch holds the tip right now."""
        clone = self.merged_branch('work/sq', then=lambda tree: self.git(tree, 'push', '-q', 'origin', 'HEAD:main'))
        self.merged[-1]['headRefOid'] = fx.HEX[:40]
        plan = self.plan(self.branch_cfg())
        item = [i for i in plan.items if i.kind == 'branch'][0]
        self.assertIn('main on GitHub', item.why)
        self.git(self.origin(), 'update-ref', 'refs/heads/main', self.git(clone, 'rev-parse', 'work/sq~1'))
        self.assertKept(self.plan(self.branch_cfg()), self.label(clone, 'work/sq'), 'commits not in the merged PR')

    def test_a_branch_used_in_the_last_day_is_kept(self):
        clone = self.merged_branch()
        with mock.patch.object(self.c.rules, 'branch_used', self.real_branch_used):
            self.assertKept(self.plan(self.branch_cfg()), self.label(clone, 'work/a'), 'used under 24 h ago', 'h ago')
            log = os.path.join(clone, '.git', 'logs', 'refs', 'heads', 'work', 'a')
            with open(log) as f:
                text = re.sub(r'> \d+ ([+-]\d{4})', lambda m: f'> {int(self.now - 2 * fx.DAY)} {m.group(1)}', f.read())
            self.file(log, text, days=2)
            self.age(os.path.join(clone, '.git', 'refs', 'heads', 'work', 'a'), 2)
            self.assertEqual(self.branch_items(self.plan(self.branch_cfg())), [(clone, 'work/a')])

    def test_the_clone_says_what_keeps_all_its_branches(self):
        clone = self.merged_branch()
        self.file(os.path.join(clone, 'KEEP'), 'my notes\n')
        self.assertKept(self.plan(self.branch_cfg()), self.label(clone, 'work/a'), 'KEEP file', 'my notes')
        os.unlink(os.path.join(clone, 'KEEP'))
        here = self.path('gh', *fx.REPO.split('/')) + '.git'
        self.git(clone, 'config', f'url.{here}.insteadOf', f'https://github.com/{fx.REPO}.git')
        self.assertKept(self.plan(self.branch_cfg()), self.label(clone, 'work/a'), 'origin on this machine', here)
        self.git(clone, 'config', '--unset', f'url.{here}.insteadOf')

        def down(repo):
            raise self.c.facts.Unreadable('GitHub (gh pr list: could not connect)')
        self.assertKept(self.plan(self.branch_cfg(), prs=down), f'branches in {clone}', 'could not read', 'GitHub')
        code, lines = self.run_cleaner(self.branch_cfg(), prs=down)
        self.assertEqual(code, 1, lines)
        self.assertIn('work/a', self.branches(clone))

    def test_the_last_check_reads_the_branch_again_and_git_deletes_only_what_it_judged(self):
        clone = self.merged_branch()
        cfg = self.branch_cfg()
        facts = self.facts(cfg)
        plan = self.c.rules.plan(cfg, facts)
        self.assertEqual(self.branch_items(plan), [(clone, 'work/a')])
        main = self.git(clone, 'rev-parse', 'main')
        scratch = self.worktree(clone, 'w', 'scratch', into=self.mkdir('w2'))
        self.commit(scratch, 'new.txt')
        self.push(scratch)                                          # on GitHub, but in no merged pull request
        self.git(clone, 'branch', '-f', 'work/a', self.head(scratch))
        failed, lines = self.delete_all(cfg, facts, plan.items)
        self.assertEqual([line.split()[0] for line in lines], ['REFUSED'], lines)
        self.assertIn('commits not in the merged PR', lines[0])
        self.assertIn('work/a', self.branches(clone))
        # Moved after the last check: git compares the branch with the commit that check judged, and removes nothing.
        self.git(clone, 'branch', '-f', 'work/a', self.merged[0]['headRefOid'])
        plan = self.c.rules.plan(cfg, facts)
        with self.swap_after_the_last_check(clone, lambda path: self.git(path, 'branch', '-f', 'work/a', main)):
            failed, lines = self.delete_all(cfg, facts, plan.items)
        self.assertEqual(failed, 1, lines)
        self.assertEqual([line.split()[0] for line in lines], ['FAILED'], lines)
        self.assertIn('nothing removed', lines[0])
        self.assertEqual(self.git(clone, 'rev-parse', 'work/a'), main)
        self.assertFalse(os.path.exists(cfg.journal_file))          # one git step: nothing half gone

    def test_the_log_line_names_the_commit_so_one_line_undoes_it(self):
        """#524: `(was <oid>)`, as `git branch -d` says it, and the commit the last check judged, not the plan's."""
        clone = self.merged_branch()
        cfg = self.branch_cfg()
        facts = self.facts(cfg)
        plan = self.c.rules.plan(cfg, facts)
        main = self.git(clone, 'rev-parse', 'main')
        self.git(clone, 'branch', '-f', 'work/a', main)             # moved, and may still go: main is on GitHub
        failed, lines = self.delete_all(cfg, facts, plan.items)
        self.assertEqual(failed, 0, lines)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith(f'deleted {self.label(clone, "work/a")} (0 B, worktrees: '), lines)
        self.assertTrue(lines[0].endswith(f'(was {main}))'), lines)
        with open(cfg.log_file) as f:
            logged = [line for line in f.read().splitlines() if 'deleting branch work/a' in line]
        self.assertEqual(len(logged), 1, logged)
        self.assertTrue(logged[0].endswith(f'(was {main}))'), logged)
        self.git(clone, 'branch', 'work/a', main)                   # the undo the line gives
        self.assertIn('work/a', self.branches(clone))

    def test_the_config_section_goes_only_while_it_is_the_deleted_branchs(self):
        """#524: between `update-ref -d` and removing `branch.<b>.*`, a new branch of that name (or a new section)
        may appear: then the section is the new branch's, and stays."""
        clone = self.merged_branch('work/a', 7)
        self.merged_branch('work/b', 8)
        self.assertIn('branch.work/a.remote=origin', self.git(clone, 'config', '--local', '--list'))
        real = self.c.facts._git

        def racing(cwd, args, bare, stdin):
            done = real(cwd, args, bare, stdin)
            if args[0] == 'update-ref' and args[3] == 'refs/heads/work/a':
                self.git(clone, 'branch', '--no-track', 'work/a', 'main')             # a branch of that name again
            if args[0] == 'update-ref' and args[3] == 'refs/heads/work/b':
                self.git(clone, 'config', 'branch.work/b.description', 'mine')         # the section changed
            return done
        with mock.patch.object(self.c.facts, '_git', racing):
            code, lines = self.run_cleaner(self.branch_cfg())
        self.assertEqual(code, 0, lines)
        config = self.git(clone, 'config', '--local', '--list')
        self.assertIn('branch.work/a.remote=origin', config)
        self.assertIn('branch.work/b.description=mine', config)
        self.assertEqual(self.branches(clone), ['main', 'work/a'])

    def test_a_branch_another_name_points_at_is_kept(self):
        """#524: `refs/heads/work/alias -> refs/heads/work/a` (git symbolic-ref) would point at nothing."""
        clone = self.merged_branch()
        self.git(clone, 'symbolic-ref', 'refs/heads/work/alias', 'refs/heads/work/a')
        cfg = self.branch_cfg()
        self.assertKept(self.plan(cfg), self.label(clone, 'work/a'), 'symbolic ref names it', 'refs/heads/work/alias')
        self.assertNotCandidate(self.plan(cfg), self.label(clone, 'work/alias'))     # no branch of its own
        self.git(clone, 'symbolic-ref', '--delete', 'refs/heads/work/alias')
        self.assertEqual(self.branch_items(self.plan(cfg)), [(clone, 'work/a')])

    def test_two_repositories_in_one_folder(self):
        """#524: the last check asks the repository whose clone it is, not the first one listed for the folder."""
        clone = self.merged_branch()
        other = {'root': self.trees, 'github': 'someone/other', 'branches': ['work/*'], 'mergedBranches': True}
        mine = {'root': self.trees, 'github': fx.REPO, 'branches': ['work/*'], 'mergedBranches': True}
        code, lines = self.run_cleaner(self.cfg(worktrees={'repos': [other, mine]}))
        self.assertEqual(code, 0, lines)
        self.assertNotIn('work/a', self.branches(clone))

    def test_a_delete_stopped_half_way_leaves_nothing_to_judge(self):
        """Killed after git removed the branch and before its reflog or config section went: the next run sees no
        branch, judges nothing and exits 0. The reflog left behind still counts for the clone (git log --reflog)."""
        clone = self.merged_branch()
        log = os.path.join(clone, '.git', 'logs', 'refs', 'heads', 'work', 'a')
        with open(log) as f:
            text = f.read()
        self.git(clone, 'update-ref', '-d', 'refs/heads/work/a')
        self.file(log, text)
        self.git(clone, 'config', 'branch.work/a.remote', 'origin')
        code, lines = self.run_cleaner(self.branch_cfg())
        self.assertEqual(code, 0, lines)
        self.assertEqual([line for line in lines if 'work/a' in line], [])
        self.assertTrue(os.path.isfile(log))

    def test_a_half_deleted_clone_has_no_branch_judged(self):
        clone = self.merged_branch()
        cfg = self.branch_cfg()
        os.makedirs(os.path.dirname(cfg.journal_file), exist_ok=True)
        self.c.engine.write_journal(cfg, [{'kind': 'clone', 'path': clone, 'rule': 'worktrees', 'at': '2026-01-02T03:04:05Z'}])
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 1, lines)
        self.assertIn('work/a', self.branches(clone))
        self.assertEqual([line for line in lines if line.startswith(('deleted ', 'would delete '))], [])


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
        self.assertKept(plan, on_main, 'base branch')

    def test_a_clone_of_the_default_branch_is_kept(self):
        """A merged pull request whose head branch is main (from the same repository, into another branch) does not
        finish a clone of main, even when no pull request in the list merges into main."""
        on_main = self.clone('on-main')
        self.pr(on_main, 40, base='release')
        self.assertEqual(self.branch(on_main), 'main')
        self.assertKept(self.planned(), on_main, 'base branch', 'default branch')
        self.merged[0]['isCrossRepository'] = True                   # a fork's main
        self.assertKept(self.planned(), on_main, 'base branch')

    def test_only_githubs_own_head_names_the_default_branch(self):
        """`git ls-remote --symref origin HEAD` also prints every other symbolic ref whose name ends in /HEAD. When
        GitHub's own HEAD names no branch, such a ref does not stand in for it: the default branch is not known."""
        on_main = self.clone('on-main')
        self.pr(on_main, 40, base='release')
        bare = self.origin()
        self.git(bare, 'branch', 'side', 'main')
        self.git(bare, 'symbolic-ref', 'refs/heads/team/HEAD', 'refs/heads/side')
        self.assertKept(self.planned(), on_main, 'base branch', 'default branch')
        self.git(bare, 'update-ref', '--no-deref', 'HEAD', self.head(on_main))
        self.assertKept(self.planned(), on_main, 'could not read', 'named no default branch')

    def test_a_locked_worktree_whose_folder_is_gone_keeps_the_clone(self):
        clone = self.finished_clone()
        tree = self.finished(clone=clone)
        self.git(clone, 'worktree', 'lock', '--reason', 'on a removable disk', tree)
        shutil.rmtree(tree)
        self.assertKept(self.planned(), clone, 'worktrees hang off it')

    def test_a_commit_only_a_stale_tracking_ref_names_keeps_the_clone(self):
        """The branch was pushed, then deleted on GitHub: origin/side still names its commit, GitHub does not."""
        clone = self.finished_clone()
        work = self.branch(clone)
        self.git(clone, 'checkout', '-q', '-b', 'side')
        lost = self.commit(clone, 'side.txt')
        self.push(clone)
        self.git(clone, 'checkout', '-q', work)
        self.git(clone, 'branch', '-q', '-D', 'side')
        self.git(self.origin(), 'branch', '-q', '-D', 'side')
        self.assertEqual(self.git(clone, 'rev-parse', 'origin/side'), lost)
        self.assertKept(self.planned(), clone, 'commits GitHub does not have', lost[:10])

    def test_tags_github_does_not_have_keep_the_clone(self):
        for how in (('-a', '-m', 'notes only here'), ()):
            with self.subTest(annotated=bool(how)):
                clone = self.finished_clone(f'c{len(how)}', number=60 + len(how))
                self.git(clone, 'tag', *how, f'v{len(how)}')
                self.assertKept(self.planned(), clone, 'tags GitHub does not have', f'v{len(how)}')
                self.git(clone, 'push', '-q', 'origin', f'v{len(how)}')
                self.assertDeletes(self.planned(), clone)

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
        self.merged.append({**self.merged[0], 'number': 30, 'headRefName': 'idea', 'headRefOid': lonely})
        self.assertDeletes(self.planned(), clone)               # it is a pull request's head: GitHub has it

    def test_a_commit_only_a_reflog_still_has_keeps_the_clone(self):
        clone = self.finished_clone()
        merged = self.head(clone)
        self.commit(clone, 'thrown-away.txt')
        self.git(clone, 'reset', '-q', '--hard', merged)
        self.assertKept(self.planned(), clone, 'commits GitHub does not have')

    def test_a_clone_swapped_after_the_last_check_is_never_followed(self):
        """The lens probe: after the last check passed, the clone is replaced by a link to a folder that must be kept,
        or by another folder. Nothing is removed, the line says why, the run fails."""
        clone = self.finished_clone()
        precious = self.precious()
        other = self.file(self.path('elsewhere', 'other', 'notes.txt'), 'notes\n')
        cfg = self.repo_cfg()
        for name, swap, undo in (
                ('a link', lambda p: (os.rename(p, self.path('moved')), os.symlink(os.path.dirname(precious), p)),
                 lambda p: os.unlink(p)),
                ('another folder', lambda p: (os.rename(p, self.path('moved')), os.rename(os.path.dirname(other), p)),
                 lambda p: os.rename(p, os.path.dirname(other)))):
            with self.subTest(swap=name):
                with self.swap_after_the_last_check(clone, swap):
                    code, lines = self.run_cleaner(cfg)
                undo(clone)
                os.rename(self.path('moved'), clone)
                self.assertNothingRemoved(code, lines, (precious, 'transcript\n'), (other, 'notes\n'))
                self.assertFalse(os.path.exists(cfg.journal_file))
                self.assertTrue(os.path.isdir(os.path.join(clone, '.git')))

    def test_a_deinitialised_submodule_keeps_the_clone(self):
        clone = self.finished_clone()
        self.origin('acme/sub')
        self.git(clone, 'submodule', 'add', '-q', 'https://github.com/acme/sub.git', 'sub')
        self.git(clone, 'commit', '-q', '-m', 'add sub')
        self.push(clone)
        self.merged[0]['headRefOid'] = self.head(clone)
        lost = self.commit(os.path.join(clone, 'sub'), 'only-here.txt')
        self.git(clone, 'submodule', 'deinit', '-q', '-f', 'sub')
        self.assertEqual(os.listdir(os.path.join(clone, 'sub')), [])
        self.assertKept(self.planned(), clone, 'git: unpushed commits', lost[:10])
        modules = os.path.join(self.git_dir(clone), 'modules', 'sub')
        self.git(modules, 'push', '-q', 'origin', f'{lost}:refs/heads/kept')
        self.assertDeletes(self.planned(), clone)

    def test_an_origin_that_git_turns_into_a_folder_here_keeps_the_clone(self):
        """origin says github.com, but url.<base>.insteadOf makes git use a folder on this machine: what that
        folder has is not on GitHub."""
        clone = self.finished_clone()
        self.assertDeletes(self.planned(), clone)
        here = self.path('gh', *fx.REPO.split('/')) + '.git'
        self.git(clone, 'config', f'url.{here}.insteadOf', f'https://github.com/{fx.REPO}.git')
        self.assertKept(self.planned(), clone, 'origin on this machine', here)

    def test_a_tag_moved_since_it_was_pushed_keeps_the_clone(self):
        clone = self.finished_clone()
        self.git(clone, 'tag', 'v1')
        self.git(clone, 'push', '-q', 'origin', 'v1')
        self.assertDeletes(self.planned(), clone)
        self.git(clone, 'tag', '-f', 'v1', 'origin/main')
        self.assertKept(self.planned(), clone, 'tags GitHub does not have', 'v1')


class GitHub(Case):
    """The reader of the pull request lists (facts.github_prs), against a stub `gh`."""
    ROWS = {'merged': [{'number': 7, 'headRefName': 'fix-1', 'headRefOid': 'a' * 40, 'mergedAt': '2026-01-02T03:04:05Z',
                        'baseRefName': 'main', 'isCrossRepository': False}],
            'open': [{'number': 8, 'headRefName': 'fix-2', 'headRefOid': 'b' * 40, 'mergedAt': None,
                      'baseRefName': 'main', 'isCrossRepository': True}]}

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
                      {'merged': [{**self.ROWS['merged'][0], 'headRefOid': 'HEAD; rm'}], 'open': []},
                      {'merged': [{**self.ROWS['merged'][0], 'isCrossRepository': 'no'}], 'open': []},
                      {'merged': [{**self.ROWS['merged'][0], 'baseRefName': None}], 'open': []}):
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
    'orphaned worktree': ('rules', "gone = orphaned_worktree(path) if kind == 'worktree' else None", 'gone = None',
                          Worktrees, 'test_a_worktree_whose_main_repository_is_gone_is_kept_as_orphaned'),
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
    'GitHub down': ('rules', "return kind, Refusal('could not read', str(ex)), '', None  # GitHub", 'prs = EMPTY_PRS', Worktrees, 'test_github_down_keeps_everything'),
    'git cannot answer': ('rules', "return kind, Refusal('could not read', f'git: {ex}'), '', None  # whose it is", 'return None',
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
    'no commit': ('rules', "if not head.strip('0'):", 'if False:', Worktrees, 'test_a_worktree_whose_branch_has_no_commit_is_kept'),
    'another git folder inside': ('guard', 'if nested:', 'if False:', Worktrees, 'test_another_git_folder_inside_keeps_it'),
    'only the listed repository': ('rules', 'if not same_repo(url, repo.github):', 'if False:', Worktrees, 'test_only_folders_of_the_listed_repository_are_looked_at'),
    'the last check before a delete': ('engine', 'refusal = rules.recheck(cfg, facts, item)', 'refusal = None', Worktrees, 'test_everything_is_read_again_just_before_the_delete'),
    'no --force': ('facts', "('worktree', 'remove', path)", "('worktree', 'remove', '--force', path)", Worktrees, 'test_git_removes_it_without_force'),
    'a refused git remove leaves no note': ('engine', '(isinstance(ex, Untouched) and not prior)', '(False)',
                                            Worktrees, 'test_git_removes_it_without_force'),
    'regenerable: plain names only': ('config', "or any(c in v for c in '*?[]\\\\')", '', Worktrees, 'test_the_list_takes_only_plain_names'),
    'regenerable: a name frees folders only': ('rules', "folders = parts if name.endswith('/') else parts[:-1]", 'folders = parts',
                                               Worktrees, 'test_ignored_files_that_are_not_build_output_keep_it'),
    'a fork PR': ('rules', " and not row['isCrossRepository']", '', Worktrees, 'test_a_fork_pull_request_with_the_same_branch_name_does_not_finish_it'),
    'a base branch': ('rules', 'if based:', 'if False:', Worktrees, 'test_a_branch_pull_requests_merge_into_is_kept'),
    'the default branch': ('rules', 'if branch == default_branch(path):', 'if False:', Clones, 'test_a_clone_of_the_default_branch_is_kept'),
    'a moved worktree': ('rules', "if len(mine) != 1 or (kind == 'clone') != (mine[0] is entries[0]) or 'bare' in entries[0]:",
                         "if False:", Worktrees, 'test_a_worktree_that_was_moved_is_kept'),
    'the last check: the same kind': ('rules', 'if answer is None or answer[0] != item.kind:', 'if answer is None:',
                                      Worktrees, 'test_the_last_check_wants_the_same_kind'),
    'a folder it cannot read': ('rules', "raise Unreadable(f'{ex.filename or path}: {ex.strerror}') from None", 'return None',
                                Worktrees, 'test_a_folder_it_cannot_read_is_kept_and_named'),
    'clone: worktrees first': ('rules', 'if others:', 'if False:', Clones, 'test_a_clone_goes_only_after_its_worktrees'),
    'clone: a worktree outside the root': ('rules', 'if others:', 'if [o for o in others if os.path.dirname(o) == repo.root]:', Clones, 'test_a_worktree_anywhere_keeps_the_clone'),
    'clone: stash entries': ('rules', "if git_code(path, 'rev-parse', '-q', '--verify', 'refs/stash') == 0:", 'if False:', Clones, 'test_stash_entries_keep_the_clone'),
    'clone: commits GitHub does not have': ('rules', 'if alone:  # clone', 'if False:', Clones, 'test_a_commit_on_any_branch_that_github_does_not_have_keeps_the_clone'),
    'clone: a locked worktree whose folder is gone': ('rules', "e.get('locked') is not None or os.path.lexists", 'os.path.lexists',
                                                      Clones, 'test_a_locked_worktree_whose_folder_is_gone_keeps_the_clone'),
    'clone: a live answer, not tracking refs': ('rules', 'has = sorted(here | present(path, refs.values()))',
                                                "has = sorted(here | present(path, refs.values())) + ['--remotes']",
                                                Clones, 'test_a_commit_only_a_stale_tracking_ref_names_keeps_the_clone'),
    'clone: tags': ('rules', 'if missing:', 'if False:', Clones, 'test_tags_github_does_not_have_keep_the_clone'),
    'clone: reflog commits': ('rules', "lonely(path, ['--all', '--reflog'], has)", "lonely(path, ['--all'], has)", Clones, 'test_a_commit_only_a_reflog_still_has_keeps_the_clone'),
    'delete: a worktree still the one the last check judged': ('rules', 'if item.look is None or (st.st_dev, st.st_ino) != item.look.ident:',
                                                                'if False:', Worktrees, 'test_a_worktree_swapped_after_the_last_check_is_never_followed'),
    'delete: a clone still the one the last check judged': ('rules', 'if (st.st_dev, st.st_ino) != look.ident:  # ident', 'if False:',
                                                            Clones, 'test_a_clone_swapped_after_the_last_check_is_never_followed'),
    'delete: git cannot find the clone': ('rules', "raise Untouched(f'could not read: {ex}: nothing removed') from None", 'raise',
                                          Worktrees, 'test_git_that_cannot_find_the_clone_removes_nothing'),
    'git remove: killed is not refused': ('facts', "refused = done.returncode == 128 and 'failed to delete' not in done.stderr",
                                          'refused = True', Worktrees, 'test_a_git_remove_that_was_killed_or_failed_half_way_is_half_deleted'),
    'git remove: failed to delete is not refused': ('facts', " and 'failed to delete' not in done.stderr", '',
                                                    Worktrees, 'test_a_git_remove_that_was_killed_or_failed_half_way_is_half_deleted'),
    'git remove: only a refusal leaves no note': ('engine', '(isinstance(ex, Untouched) and not prior)', '(isinstance(ex, Unreadable) and not prior)',
                                                  Worktrees, 'test_a_git_remove_that_was_killed_or_failed_half_way_is_half_deleted'),
    'PR lists: read again before each delete': ('facts', 'self._prs = {}', "self._prs = getattr(self, '_prs', {})", Worktrees,
                                                'test_the_pull_request_lists_are_read_for_the_plan_and_again_before_each_delete'),
    'submodules: a worktree': ('rules', 'modules = guard.modules_refusal(path, git_dir)', 'modules = None', Worktrees,
                               'test_a_deinitialised_submodule_in_a_worktree_keeps_it'),
    'submodules: a clone': ('rules', 'modules = guard.modules_refusal(path, git_dir)', 'modules = None', Clones,
                            'test_a_deinitialised_submodule_keeps_the_clone'),
    'clone: an origin on this machine': ('rules', 'if local:  # origin here', 'if False:', Clones,
                                         'test_an_origin_that_git_turns_into_a_folder_here_keeps_the_clone'),
    'clone: a moved tag': ('rules', 'if refs.get(ref) != oid)', 'if ref not in refs)', Clones, 'test_a_tag_moved_since_it_was_pushed_keeps_the_clone'),
    'the default branch: GitHub HEAD only': ('facts', "and line.endswith('\\tHEAD')", '', Clones,
                                                   'test_only_githubs_own_head_names_the_default_branch'),
    'GitHub: a failed gh': ('facts', 'if done.returncode != 0:  # gh', 'if False:', GitHub, 'test_no_answer_or_a_strange_answer_means_keep'),
    'GitHub: a strange row': ('facts', 'if not valid_pr(row, state):', 'if False:', GitHub, 'test_no_answer_or_a_strange_answer_means_keep'),
    'used: kept when used': ('rules', 'if age < cfg.used_hours * 3600:', 'if False:', Used, 'test_a_worktree_used_in_the_last_day_is_kept'),
    'used: the git files a command writes': ('rules', 'for name in USE_MARKS:', 'for name in ():', Used, 'test_each_sign_of_use_keeps_it'),
    'used: the reflog lines': ('rules', "when = reflog_newest(os.path.join(git_dir, 'logs', 'HEAD'))", 'when = 0.0', Used,
                               'test_each_sign_of_use_keeps_it'),
    'used: a reflog line it cannot read': ('rules', "raise Unreadable(f'line {number} of the reflog {path} is no reflog line')",
                                           'pass', Used, 'test_a_reflog_line_it_cannot_read_keeps_it'),
    'used: a clone too': ('rules', 'used, what = look.newest, ', 'used, what = 0.0, ', Used, 'test_a_clone_used_in_the_last_day_is_kept'),
    'branches: only listed ones': ('rules', 'if repo.branches and not matches(branch, repo.branches):', 'if False:', Branches,
                                   'test_only_listed_branches_are_removed'),
    'GitHub: a cut-off list': ('facts', "if state == 'open' and len(rows) >= PR_LIMIT:", 'if False:', GitHub, 'test_a_list_that_may_be_cut_off_means_keep'),
    'branch: off without the key': ('rules', "if kind == 'clone' and repo.merged_branches and refusal is not None:",
                                    "if kind == 'clone' and refusal is not None:", MergedBranches, 'test_without_the_key_no_branch_is_looked_at'),
    'branch: the key needs patterns': ('config', "if repo['mergedBranches'] and not repo['branches']:", 'if False:', MergedBranches,
                                       'test_without_the_key_no_branch_is_looked_at'),
    'branch: only listed ones': ('rules', 'if matches(name, repo.branches) and (only is None or name == only)}',
                                 'if (only is None or name == only)}', MergedBranches,
                                 'test_a_merged_branch_with_no_worktree_is_deleted_and_a_second_run_finds_nothing'),
    'branch: checked out': ('rules', 'if name not in out and not branch_busy(git_dir, name)}', 'if not branch_busy(git_dir, name)}',
                            MergedBranches, 'test_a_branch_checked_out_in_a_worktree_goes_in_a_later_run_than_it'),
    'branch: rebased or bisected': ('rules', "if named in ('refs/heads/' + branch, branch):", 'if False:', MergedBranches,
                                    'test_a_branch_a_worktree_rebases_or_bisects_is_not_a_candidate'),
    'branch: open PR': ('rules', 'if open_rows:', 'if False:', MergedBranches, 'test_each_reason_keeps_the_branch'),
    'branch: the default branch': ('rules', 'if branch == default:  # the default branch: a branch', 'if False:', MergedBranches, 'test_each_reason_keeps_the_branch'),
    'branch: a base branch': ('rules', 'if bases:', 'if False:', MergedBranches, 'test_each_reason_keeps_the_branch'),
    'branch: no merged PR': ('rules', 'if not finished:', 'if False:', MergedBranches, 'test_each_reason_keeps_the_branch'),
    'branch: a fork PR': ('rules', " and row['isCrossRepository'] is False", '', MergedBranches, 'test_each_reason_keeps_the_branch'),
    'branch: commits not in the merged PR': ('rules', "if not holding:\n        return Refusal('commits not in the merged PR', f'{oid",
                                             "if False:\n        return Refusal('commits not in the merged PR', f'{oid", MergedBranches, 'test_each_reason_keeps_the_branch'),
    'branch: in the default branch on GitHub': ('rules', "if not holding and main in on_github and git_code(clone, 'merge-base', '--is-ancestor', oid, main) == 0:",
                                                'if not holding and main in on_github:', MergedBranches, 'test_a_tip_in_githubs_default_branch_may_go'),
    'branch: merged under 24 h ago': ('rules', 'if since < cfg.merged_hours * 3600:', 'if False:', MergedBranches, 'test_each_reason_keeps_the_branch'),
    'branch: commits only in its reflog': ('rules', 'if lost:', 'if False:', MergedBranches, 'test_each_reason_keeps_the_branch'),
    'branch: used': ('rules', 'if facts.now - used < cfg.used_hours * 3600:', 'if False:', MergedBranches,
                     'test_a_branch_used_in_the_last_day_is_kept'),
    'branch: a KEEP file in the clone': ('rules', 'if os.path.lexists(keep):', 'if False:', MergedBranches,
                                         'test_the_clone_says_what_keeps_all_its_branches'),
    'branch: an origin on this machine': ('rules', 'if here_urls:', 'if False:', MergedBranches,
                                          'test_the_clone_says_what_keeps_all_its_branches'),
    'branch: the last check': ('rules', '                item.why = branch_why(why, item.oid)\n            return refusal',
                               '                item.why = branch_why(why, item.oid)\n            return None', MergedBranches,
                               'test_the_last_check_reads_the_branch_again_and_git_deletes_only_what_it_judged'),
    'branch: the log names the commit': ('rules', "return f'{why} (was {oid})'", 'return why', MergedBranches,
                                         'test_the_log_line_names_the_commit_so_one_line_undoes_it'),
    'branch: the log names the last check\'s commit': ('engine', "told = f'{item.label} ({human(item.size)}, {item.rule}: {item.why})'   # as",
                                                      "pass  # as", MergedBranches, 'test_the_log_line_names_the_commit_so_one_line_undoes_it'),
    'branch: the commit the last check read': ('rules', "                item.why = branch_why(why, item.oid)\n", '                pass\n', MergedBranches,
                                               'test_the_log_line_names_the_commit_so_one_line_undoes_it'),
    'branch: config of a new branch of that name': ('facts', 'if not before or again or branch_config(cwd, branch) != before:',
                                                    'if not before or branch_config(cwd, branch) != before:', MergedBranches,
                                                    'test_the_config_section_goes_only_while_it_is_the_deleted_branchs'),
    'branch: a changed config section': ('facts', 'if not before or again or branch_config(cwd, branch) != before:',
                                         'if not before or again:', MergedBranches,
                                         'test_the_config_section_goes_only_while_it_is_the_deleted_branchs'),
    'branch: another name of it': ('rules', 'if named:   # another name of it', 'if False:  # another name of it', MergedBranches,
                                   'test_a_branch_another_name_points_at_is_kept'),
    'branch: two repositories in one folder': ('rules', "if not same_repo(origin_url(item.path), repo.github):\n                    continue",
                                               "if not same_repo(origin_url(item.path), repo.github):\n                    return Refusal('not a candidate', '')",
                                               MergedBranches, 'test_two_repositories_in_one_folder'),
    'branch: deleted only at the commit judged': ('facts', "('update-ref', '--no-deref', '-d', 'refs/heads/' + branch, oid)",
                                                  "('update-ref', '--no-deref', '-d', 'refs/heads/' + branch)", MergedBranches,
                                                  'test_the_last_check_reads_the_branch_again_and_git_deletes_only_what_it_judged'),
}


class RedCheck(unittest.TestCase):
    def test_each_refusal_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
