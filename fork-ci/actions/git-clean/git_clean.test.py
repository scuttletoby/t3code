"""Tests of git_clean.py, its action and the git calls of every fork-ci action that reads a checkout
(run: python3 fork-ci/actions/git-clean/git_clean.test.py).

A workspace an earlier job left behind is planted the way pull request code could: a .git/config with a command git
runs (core.fsmonitor, core.hooksPath, attr.tree with a merge driver, an include of another file, ...). On a self-hosted
runner `prepare` empties the workspace, so a checkout after it runs nothing planted; on a GitHub-hosted one (or after
checkout, `check`) a config that actions/checkout does not write is refused. Every marker file is one the planted
command would write: a test passes only when it is absent. Nothing here reaches the network.
"""
import io, importlib.util, json, os, shutil, stat, subprocess, sys, tempfile, unittest, unittest.mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ACTIONS = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import git_clean as G  # noqa: E402

GIT_ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@t', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_TERMINAL_PROMPT': '0'}
UUID = '0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(cwd, *a, env=None):
    r = subprocess.run(['git', *a], cwd=cwd, text=True, capture_output=True, env=env or GIT_ENV)
    if r.returncode != 0:
        raise RuntimeError(f'git {a}: {r.stderr}')
    return r.stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp(prefix='git-clean-'))
        self.addCleanup(self.rm, self.d)
        self.runner = os.path.join(self.d, '_work', 'proj')        # RUNNER_WORKSPACE
        self.ws = os.path.join(self.runner, 'proj')                # GITHUB_WORKSPACE
        self.temp = os.path.join(self.d, '_work', '_temp')         # RUNNER_TEMP
        self.outside = os.path.join(self.d, 'outside')             # never touched
        for p in (self.ws, self.temp, self.outside):
            os.makedirs(p)
        self.marker = os.path.join(self.outside, 'MARKER')
        with open(os.path.join(self.outside, 'keep'), 'w') as f:
            f.write('keep\n')

    @staticmethod
    def rm(path):
        for root, dirs, _ in os.walk(path):
            for x in dirs:
                p = os.path.join(root, x)
                if not os.path.islink(p):
                    os.chmod(p, 0o700)
        shutil.rmtree(path, True)

    def env(self, runner='self-hosted', **kw):
        e = {'GITHUB_WORKSPACE': self.ws, 'RUNNER_WORKSPACE': self.runner, 'RUNNER_TEMP': self.temp,
             'HOME': self.d, 'PATH': os.environ['PATH']}
        if runner is not None:
            e['RUNNER_ENVIRONMENT'] = runner
        e.update(kw)
        return {k: v for k, v in e.items() if v is not None}

    def run_cli(self, *argv, env=None):
        """(exit code, stdout+stderr) of git_clean.py as the action runs it."""
        r = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'git_clean.py'), *argv], cwd=self.ws,
                           text=True, capture_output=True, env=env or self.env())
        return r.returncode, r.stdout + r.stderr

    def checkout(self, path='.'):
        """A repository the way actions/checkout leaves it (one commit, origin, gc.auto 0, a credentials include)."""
        repo = os.path.normpath(os.path.join(self.ws, path))
        os.makedirs(repo, exist_ok=True)
        git(repo, 'init', '-q', '-b', 'main')
        with open(os.path.join(repo, 'a.txt'), 'w') as f:
            f.write('a\n')
        git(repo, 'add', 'a.txt')
        git(repo, 'commit', '-q', '-m', 'a')
        git(repo, 'config', '--local', 'gc.auto', '0')
        git(repo, 'remote', 'add', 'origin', 'https://github.com/owner/repo')
        git(repo, 'config', 'branch.main.remote', 'origin')
        git(repo, 'config', 'branch.main.merge', 'refs/heads/main')
        creds = os.path.join(self.temp, f'git-credentials-{UUID}.config')
        if os.path.exists(creds):
            os.remove(creds)   # a new job: the runner empties RUNNER_TEMP
        git(repo, 'config', '--file', creds, 'http.https://github.com/.extraheader', 'AUTHORIZATION: basic eDp5')
        gitdir = os.path.join(repo, '.git')
        git(repo, 'config', f'includeIf.gitdir:{gitdir}.path', creds)
        git(repo, 'config', f'includeIf.gitdir:{gitdir}/worktrees/*.path', creds)
        git(repo, 'config', 'includeIf.gitdir:/github/workspace/.git.path',
            f'/github/runner_temp/git-credentials-{UUID}.config')
        return repo

    def plant(self, repo, *pairs):
        for key, value in pairs:
            git(repo, 'config', '--local', key, value)

    def touch_cmd(self):
        return f"sh -c 'echo planted > {self.marker}'"


class Planted(Base):
    """Each planted key: a real command first shown to run (so the plant is no dud), then refused or wiped."""

    def assert_plant_runs(self, repo, *cmd):
        git(repo, *cmd)
        self.assertTrue(os.path.exists(self.marker), 'the planted command did not run: the test plants nothing')
        os.remove(self.marker)

    def assert_refused_then_wiped(self, repo, key):
        code, out = self.run_cli('check', '--path', os.path.relpath(repo, self.ws), env=self.env('github-hosted'))
        self.assertEqual(code, 2, out)
        self.assertIn(key, out)
        self.assertFalse(os.path.exists(self.marker))
        code, out = self.run_cli('prepare', '--path', '.', env=self.env('github-hosted'))   # hosted: no wipe, refused
        self.assertEqual(code, 2, out)
        self.assertTrue(os.path.isdir(os.path.join(repo, '.git')))
        code, out = self.run_cli('prepare', '--path', '.')                                  # self-hosted: wiped
        self.assertEqual(code, 0, out)
        self.assertEqual(os.listdir(self.ws), [])
        # what actions/checkout then runs in the emptied workspace: nothing planted is left to run
        fresh = self.checkout()
        git(fresh, 'status')
        git(fresh, 'checkout', '-q', '-B', 'other')
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(self.run_cli('check', '--path', '.', '--require')[0], 0)

    def test_fsmonitor_command(self):
        repo = self.checkout()
        self.plant(repo, ('core.fsmonitor', self.touch_cmd()))
        self.assert_plant_runs(repo, 'status')
        self.assert_refused_then_wiped(repo, 'core.fsmonitor')

    def test_hooks_path(self):
        repo = self.checkout()
        hooks = os.path.join(self.ws, 'hooks')
        os.makedirs(hooks)
        with open(os.path.join(hooks, 'post-checkout'), 'w') as f:
            f.write(f'#!/bin/sh\necho planted > {self.marker}\n')
        os.chmod(os.path.join(hooks, 'post-checkout'), 0o755)
        self.plant(repo, ('core.hooksPath', hooks))
        self.assert_plant_runs(repo, 'checkout', '-q', '-B', 'x')
        self.assert_refused_then_wiped(repo, 'core.hookspath')

    def test_attr_tree_and_merge_driver(self):
        repo = self.checkout()
        with open(os.path.join(repo, '.gitattributes'), 'w') as f:
            f.write('* merge=evil\n')
        git(repo, 'add', '.gitattributes')
        git(repo, 'commit', '-q', '-m', 'attrs')
        git(repo, 'tag', 'attrs')
        git(repo, 'rm', '-q', '.gitattributes')
        git(repo, 'commit', '-q', '-m', 'attrs gone from the tree; attr.tree still reads them')
        self.plant(repo, ('attr.tree', 'attrs'), ('merge.evil.driver', self.touch_cmd()))
        base = git(repo, 'rev-parse', 'HEAD')
        for b, text in (('one', '1\n'), ('two', '2\n')):
            git(repo, 'checkout', '-q', '-b', b, base)
            with open(os.path.join(repo, 'a.txt'), 'w') as f:
                f.write(text)
            git(repo, 'commit', '-q', '-am', b)
        subprocess.run(['git', 'merge', '-q', 'one'], cwd=repo, env=GIT_ENV, capture_output=True)
        self.assertTrue(os.path.exists(self.marker), 'the planted merge driver did not run')
        os.remove(self.marker)
        subprocess.run(['git', 'merge', '--abort'], cwd=repo, env=GIT_ENV, capture_output=True)
        self.assert_refused_then_wiped(repo, 'attr.tree')

    def test_merge_driver_alone_is_refused(self):
        repo = self.checkout()
        self.plant(repo, ('merge.evil.driver', self.touch_cmd()))
        self.assert_refused_then_wiped(repo, 'merge.evil.driver')

    def test_include_of_another_file(self):
        repo = self.checkout()
        other = os.path.join(self.ws, 'other.config')
        git(self.ws, 'config', '--file', other, 'core.fsmonitor', self.touch_cmd())
        self.plant(repo, ('include.path', other))
        self.assert_plant_runs(repo, 'status')
        self.assert_refused_then_wiped(repo, 'include.path')

    def test_an_included_file_is_never_read(self):   # a FIFO no one writes would hang the step
        repo = self.checkout()
        fifo = os.path.join(self.ws, 'pipe.config')
        os.mkfifo(fifo)
        self.plant(repo, ('include.path', fifo))
        r = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'git_clean.py'), 'check', '--path', '.'],
                           cwd=self.ws, text=True, capture_output=True, env=self.env('github-hosted'), timeout=30)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('include.path', r.stdout)

    def test_include_if_to_a_planted_file(self):
        repo = self.checkout()
        other = os.path.join(self.ws, 'other.config')
        git(self.ws, 'config', '--file', other, 'core.fsmonitor', self.touch_cmd())
        self.plant(repo, (f'includeIf.gitdir:{repo}/.git.path', other))
        self.assert_plant_runs(repo, 'status')
        self.assert_refused_then_wiped(repo, 'includeif.gitdir:')

    def test_credentials_include_with_more_than_the_header(self):
        """The include checkout writes is allowed, but only to a file that holds nothing but the token header."""
        repo = self.checkout()
        creds = os.path.join(self.temp, f'git-credentials-{UUID}.config')
        git(self.ws, 'config', '--file', creds, 'core.fsmonitor', self.touch_cmd())
        self.assert_plant_runs(repo, 'status')
        self.assert_refused_then_wiped(repo, 'core.fsmonitor')

    def test_ssh_command_filter_and_diff_drivers(self):
        for key in ('core.sshCommand', 'filter.evil.smudge', 'diff.evil.textconv', 'diff.evil.command',
                    'core.pager', 'remote.origin.uploadpack', 'core.attributesFile', 'alias.st'):
            with self.subTest(key=key):
                self.rm(self.ws)
                os.makedirs(self.ws)
                repo = self.checkout()
                self.plant(repo, (key, self.touch_cmd()))
                self.assert_refused_then_wiped(repo, key.lower())

    def test_config_worktree_is_read_too(self):
        repo = self.checkout()
        self.plant(repo, ('extensions.worktreeConfig', 'true'))
        git(repo, 'config', '--worktree', 'core.fsmonitor', self.touch_cmd())
        self.assert_plant_runs(repo, 'status')
        self.assert_refused_then_wiped(repo, 'config.worktree')

    def test_commondir_points_git_elsewhere(self):
        repo = self.checkout()
        with open(os.path.join(repo, '.git', 'commondir'), 'w') as f:
            f.write(self.outside + '\n')
        self.assert_refused_then_wiped(repo, 'commondir')

    def test_dot_git_file_and_symlink(self):
        """A worktree's or submodule's .git FILE points git elsewhere; a .git symlink too."""
        elsewhere = os.path.join(self.outside, 'repo')
        git(self.outside, 'init', '-q', elsewhere)
        for kind in ('file', 'symlink'):
            with self.subTest(kind=kind):
                self.rm(self.ws)
                os.makedirs(self.ws)
                dot = os.path.join(self.ws, '.git')
                if kind == 'file':
                    with open(dot, 'w') as f:
                        f.write(f'gitdir: {elsewhere}/.git\n')
                else:
                    os.symlink(os.path.join(elsewhere, '.git'), dot)
                code, out = self.run_cli('check', '--path', '.', env=self.env('github-hosted'))
                self.assertEqual(code, 2, out)
                self.assertIn('not a folder', out)
                self.assertEqual(self.run_cli('prepare', '--path', '.')[0], 0)
                self.assertFalse(os.path.lexists(dot))
                self.assertTrue(os.path.isdir(os.path.join(elsewhere, '.git')))   # the target is outside: kept

    def test_files_that_change_history_or_objects_are_refused(self):
        """grafts change a commit's parents (--no-replace-objects does not turn them off), alternates add another
        repository's objects, info/attributes changes how a file merges or diffs. actions/checkout writes none."""
        repo = self.checkout()
        git(repo, 'commit', '-q', '--allow-empty', '-m', 'b')
        os.makedirs(os.path.join(repo, '.git', 'info'), exist_ok=True)
        with open(os.path.join(repo, '.git', 'info', 'grafts'), 'w') as f:
            f.write(git(repo, 'rev-parse', 'HEAD') + '\n')
        self.assertEqual(git(repo, *G.GIT_SAFE, 'rev-list', '--count', 'HEAD'), '1', 'the planted graft did nothing')
        self.assert_refused_then_wiped(repo, 'info/grafts')
        for name, text in (('info/attributes', '* merge=union\n'), ('objects/info/alternates', self.outside + '\n'),
                           ('objects/info/http-alternates', 'https://example.com/objects\n')):
            with self.subTest(name=name):
                self.rm(self.ws)
                os.makedirs(self.ws)
                repo = self.checkout()
                path = os.path.join(repo, '.git', *name.split('/'))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'w') as f:
                    f.write(text)
                self.assert_refused_then_wiped(repo, name)

    def test_the_refusal_says_what_to_do_either_way(self):   # an earlier job's key, or one checkout itself wrote
        repo = self.checkout()
        self.plant(repo, ('core.sshCommand', 'ssh -i key'))
        code, out = self.run_cli('check', '--path', '.', env=self.env('github-hosted'))
        self.assertEqual(code, 2, out)
        self.assertIn('run fork-ci/actions/git-clean before actions/checkout', out)
        self.assertIn('Written by actions/checkout itself', out)

    def test_unreadable_config_is_refused(self):
        repo = self.checkout()
        with open(os.path.join(repo, '.git', 'config'), 'a') as f:
            f.write('[core\n\tbroken\n')
        code, out = self.run_cli('check', '--path', '.', env=self.env('github-hosted'))
        self.assertEqual(code, 2, out)
        self.assertIn('cannot be read', out)


class Allowed(Base):
    def test_what_checkout_writes_passes(self):
        repo = self.checkout()
        self.plant(repo, ('core.sparseCheckout', 'true'), ('core.sparseCheckoutCone', 'false'),
                   ('extensions.worktreeConfig', 'true'), ('remote.origin.promisor', 'true'),
                   ('remote.origin.partialclonefilter', 'blob:none'), ('lfs.repositoryformatversion', '0'),
                   ('filter.lfs.clean', 'git-lfs clean -- %f'), ('filter.lfs.smudge', 'git-lfs smudge -- %f'),
                   ('filter.lfs.process', 'git-lfs filter-process'), ('filter.lfs.required', 'true'),
                   ('http.https://github.com/.extraheader', 'AUTHORIZATION: basic eDp5'),
                   ('submodule.lib.url', 'https://github.com/owner/lib'), ('submodule.lib.active', 'true'),
                   ('submodule.ssh.url', 'git@github.com:owner/lib.git'),
                   ('user.name', 'ci'), ('user.email', 'ci@example.com'))
        git(repo, 'config', '--worktree', 'core.sparseCheckout', 'true')
        self.assertEqual(G.problems(repo, self.env()), [])
        code, out = self.run_cli('check', '--path', '.', '--require', env=self.env('github-hosted'))
        self.assertEqual(code, 0, out)

    def test_values_are_checked_not_only_keys(self):
        for key, value in (('gc.auto', '1'), ('remote.origin.url', 'ext::sh -c touch% /tmp/x'),
                           ('filter.lfs.smudge', 'sh -c x'), ('branch.main.remote', '.'),
                           ('core.bare', 'true'), ('user.name', 'a\nb'),
                           (f'includeIf.gitdir:{self.ws}/.git.path', f'{self.temp}/git-credentials-x.config'),
                           (f'includeIf.gitdir:{self.ws}/.git.path', f'{self.outside}/git-credentials-{UUID}.config'),
                           (f'includeIf.gitdir:{self.ws}/.git.path', f'{self.temp}/git-credentials-{UUID[:-1]}.config'),
                           ('include.path', self.creds), ('includeIf.onbranch:main.path', self.creds),
                           ('http.https://github.com/.extraheader', 'AUTHORIZATION: bearer eDp5'),
                           ('http.https://github.com/.extraheader', 'AUTHORIZATION: basic eDp5 x'),
                           ('submodule.lib.url', 'ext::sh -c touch% /tmp/x'), ('submodule.lib.url', 'file:///tmp/x'),
                           ('submodule.lib.url', 'https://user@example.com/x'),
                           ('submodule.lib.url', 'git@-oProxyCommand:y')):
            with self.subTest(key=key, value=value):
                self.rm(self.ws)
                os.makedirs(self.ws)
                repo = self.checkout()
                if 'git-credentials' in value and os.path.dirname(value) == self.temp:
                    self.header_only(value)
                self.plant(repo, (key, value))
                self.assertTrue(G.problems(repo, self.env()), (key, value))

    @property
    def creds(self):
        return os.path.join(self.temp, f'git-credentials-{UUID}.config')

    def header_only(self, path):
        if os.path.exists(path):
            os.remove(path)
        git(self.d, 'config', '--file', path, 'http.https://github.com/.extraheader', 'AUTHORIZATION: basic eDp5')

    def test_the_token_file_holds_only_the_token_header_value(self):
        repo = self.checkout()
        os.remove(self.creds)
        git(self.d, 'config', '--file', self.creds, 'http.https://github.com/.extraheader', 'AUTHORIZATION: bearer x')
        self.assertTrue(G.problems(repo, self.env()))

    def shallow_checkout(self, detached):
        """What actions/checkout does with fetch-depth 1, its default: init, fetch one ref with --depth=1, check it out
        (detached on pull_request, on a branch on push)."""
        src = os.path.join(self.d, 'src')
        git(self.d, 'init', '-q', '-b', 'main', src)
        for n in ('a', 'b', 'c'):
            git(src, 'commit', '-q', '--allow-empty', '-m', n)
        repo = self.ws
        git(repo, 'init', '-q')
        git(repo, 'remote', 'add', 'origin', 'https://github.com/owner/repo')
        git(repo, 'fetch', '-q', '--no-tags', '--depth=1', f'file://{src}', '+refs/heads/main:refs/remotes/origin/main')
        if detached:
            git(repo, 'checkout', '-q', '--detach', 'refs/remotes/origin/main')
        else:
            git(repo, 'checkout', '-q', '-B', 'main', 'refs/remotes/origin/main')
        return repo, src

    def test_a_fetch_depth_1_checkout_passes(self):
        for detached in (True, False):
            with self.subTest(detached=detached):
                self.rm(self.ws)
                self.rm(os.path.join(self.d, 'src'))
                os.makedirs(self.ws)
                repo, _ = self.shallow_checkout(detached)
                with open(os.path.join(repo, '.git', 'shallow')) as f:
                    self.assertEqual(f.read(), git(repo, 'rev-parse', 'HEAD') + '\n')   # what checkout leaves
                code, out = self.run_cli('check', '--path', '.', '--require', env=self.env('github-hosted'))
                self.assertEqual(code, 0, out)

    def test_any_other_shallow_file_is_refused(self):
        """shallow cuts history at the commits it names: only the one commit of a depth-1 checkout (HEAD) passes."""
        def deeper(repo, src):   # a depth-2 fetch: the boundary is HEAD's parent
            git(repo, 'fetch', '-q', '--no-tags', '--depth=2', f'file://{src}', '+refs/heads/main:refs/remotes/origin/main')

        def parent(repo, src):   # the history cut below HEAD, in a full clone
            self.rm(os.path.join(repo, '.git'))
            git(repo, 'init', '-q')
            git(repo, 'fetch', '-q', '--no-tags', f'file://{src}', '+refs/heads/main:refs/remotes/origin/main')
            git(repo, 'checkout', '-q', '--detach', 'refs/remotes/origin/main')
            self.write_shallow(repo, git(src, 'rev-parse', 'HEAD~1') + '\n')

        cases = (('depth 2', deeper), ('parent', parent),
                 ('two lines', lambda repo, src: self.write_shallow(repo, git(repo, 'rev-parse', 'HEAD') + '\n' +
                                                                     git(src, 'rev-parse', 'HEAD~1') + '\n')),
                 ('empty', lambda repo, src: self.write_shallow(repo, '')),
                 ('no newline', lambda repo, src: self.write_shallow(repo, git(repo, 'rev-parse', 'HEAD'))),
                 ('symlink', lambda repo, src: self.link_shallow(repo)))
        for name, make in cases:
            with self.subTest(case=name):
                self.rm(self.ws)
                self.rm(os.path.join(self.d, 'src'))
                os.makedirs(self.ws)
                repo, src = self.shallow_checkout(True)
                make(repo, src)
                code, out = self.run_cli('check', '--path', '.', '--require', env=self.env('github-hosted'))
                self.assertEqual(code, 2, out)
                self.assertIn('shallow', out)

    def write_shallow(self, repo, text):
        with open(os.path.join(repo, '.git', 'shallow'), 'w') as f:
            f.write(text)

    def link_shallow(self, repo):
        path = os.path.join(repo, '.git', 'shallow')
        real = os.path.join(self.d, 'shallow')
        os.rename(path, real)
        os.symlink(real, path)

    def test_no_checkout_yet(self):
        """GitHub-hosted, before actions/checkout: an empty workspace has nothing to check; --require wants a .git."""
        self.assertEqual(self.run_cli('prepare', '--path', '.', env=self.env('github-hosted'))[0], 0)
        code, out = self.run_cli('check', '--path', '.', '--require', env=self.env('github-hosted'))
        self.assertEqual(code, 2, out)
        self.assertIn('no .git', out)


class Wipe(Base):
    def fill(self):
        repo = self.checkout()
        os.makedirs(os.path.join(self.ws, 'sub', 'deep'))
        with open(os.path.join(self.ws, 'sub', 'deep', 'f'), 'w') as f:
            f.write('x')
        os.symlink(self.outside, os.path.join(self.ws, 'link-out'))                  # a symlink to outside
        os.symlink(os.path.join(self.outside, 'keep'), os.path.join(self.ws, 'sub', 'file-link'))
        ro = os.path.join(repo, '.git', 'readonly')                                   # a read-only file in .git
        with open(ro, 'w') as f:
            f.write('x')
        os.chmod(ro, 0o400)
        os.makedirs(os.path.join(repo, '.git', 'locked', 'in'))                       # a read-only folder in .git
        with open(os.path.join(repo, '.git', 'locked', 'in', 'f'), 'w') as f:
            f.write('x')
        os.chmod(os.path.join(repo, '.git', 'locked', 'in'), 0o500)
        os.chmod(os.path.join(repo, '.git', 'locked'), 0o000)
        return repo

    def assert_outside_kept(self):
        self.assertEqual(sorted(os.listdir(self.outside)), ['keep'])
        with open(os.path.join(self.outside, 'keep')) as f:
            self.assertEqual(f.read(), 'keep\n')
        self.assertTrue(os.path.isdir(self.temp))
        self.assertTrue(os.path.isdir(self.ws))

    def test_self_hosted_empties_the_workspace_only(self):
        self.fill()
        code, out = self.run_cli('prepare', '--path', '.')
        self.assertEqual(code, 0, out)
        self.assertEqual(os.listdir(self.ws), [])
        self.assert_outside_kept()

    def test_again_and_on_an_empty_workspace(self):
        self.fill()
        for _ in range(3):   # two jobs in a row on one runner, then an empty one
            self.assertEqual(self.run_cli('prepare', '--path', '.')[0], 0)
            self.assertEqual(os.listdir(self.ws), [])
        self.assert_outside_kept()

    def test_unknown_runner_counts_as_self_hosted(self):
        self.fill()
        self.assertEqual(self.run_cli('prepare', '--path', '.', env=self.env(None))[0], 0)
        self.assertEqual(os.listdir(self.ws), [])

    def test_github_hosted_keeps_files(self):
        self.checkout()
        self.assertEqual(self.run_cli('prepare', '--path', '.', env=self.env('github-hosted'))[0], 0)
        self.assertIn('a.txt', os.listdir(self.ws))

    def test_refuses_a_workspace_it_cannot_trust(self):
        self.fill()
        before = sorted(os.listdir(self.ws))
        for bad in ({'GITHUB_WORKSPACE': None}, {'GITHUB_WORKSPACE': 'proj'}, {'GITHUB_WORKSPACE': '/'},
                    {'GITHUB_WORKSPACE': self.d},                          # HOME
                    {'GITHUB_WORKSPACE': self.outside},                    # not inside RUNNER_WORKSPACE
                    {'GITHUB_WORKSPACE': self.runner},                     # RUNNER_WORKSPACE itself
                    {'GITHUB_WORKSPACE': os.path.join(self.ws, 'nope')},   # missing
                    {'GITHUB_WORKSPACE': os.path.join(self.d, '_work'), 'RUNNER_WORKSPACE': None},
                    {'RUNNER_WORKSPACE': None},                            # self-hosted: the runner always sets it
                    {'RUNNER_WORKSPACE': 'proj'},
                    {'HOME': os.path.join(self.ws, 'sub')},
                    {'RUNNER_TEMP': os.path.join(self.ws, 'sub')},
                    {'RUNNER_TOOL_CACHE': os.path.join(self.ws, 'sub')}):
            with self.subTest(bad=bad):
                code, out = self.run_cli('prepare', '--path', '.', env=self.env(**bad))
                self.assertEqual(code, 2, out)
                self.assertIn('refused', out)
                for name in ('HOME', 'RUNNER_TEMP', 'RUNNER_TOOL_CACHE'):
                    if name in bad:
                        self.assertIn(f'holds {name}', out)
                if bad.get('GITHUB_WORKSPACE') in (self.outside, self.runner):
                    self.assertIn('is not inside RUNNER_WORKSPACE', out)
                self.assertEqual(sorted(os.listdir(self.ws)), before)
        self.assert_outside_kept()

    def test_a_symlinked_runner_workspace_is_refused(self):
        """An earlier job swapped RUNNER_WORKSPACE (_work/<repo>) for a symlink: realpath follows it on both sides, so
        only the real path of the runner's work folder tells."""
        elsewhere = os.path.join(self.d, 'elsewhere')
        os.makedirs(os.path.join(elsewhere, 'proj'))
        with open(os.path.join(elsewhere, 'proj', 'f'), 'w') as f:
            f.write('x')
        self.rm(self.runner)
        os.symlink(elsewhere, self.runner)
        code, out = self.run_cli('prepare', '--path', '.')
        self.assertEqual(code, 2, out)
        self.assertIn('a symlink below RUNNER_WORKSPACE', out)
        self.assertEqual(os.listdir(os.path.join(elsewhere, 'proj')), ['f'])

    def test_a_symlink_between_the_runner_workspace_and_the_workspace_is_refused(self):
        other = os.path.join(self.runner, 'other', 'proj')
        os.makedirs(other)
        with open(os.path.join(other, 'f'), 'w') as f:
            f.write('x')
        os.symlink(os.path.join(self.runner, 'other'), os.path.join(self.runner, 'via'))
        code, out = self.run_cli('prepare', '--path', '.',
                                 env=self.env(GITHUB_WORKSPACE=os.path.join(self.runner, 'via', 'proj')))
        self.assertEqual(code, 2, out)
        self.assertEqual(os.listdir(other), ['f'])

    def test_a_very_deep_tree_is_removed(self):
        """Deeper than Python's recursion limit and longer than PATH_MAX: the next job is not stuck behind it."""
        fd = os.open(self.ws, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for _ in range(2500):
                os.mkdir('d', dir_fd=fd)
                down = os.open('d', os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = down
            os.close(os.open('f', os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd))
            os.symlink(self.outside, 'out', dir_fd=fd)
        finally:
            os.close(fd)
        code, out = self.run_cli('prepare', '--path', '.')
        self.assertEqual(code, 0, out)
        self.assertEqual(os.listdir(self.ws), [])
        self.assert_outside_kept()

    def test_symlinked_workspace_is_refused(self):
        link = os.path.join(self.runner, 'linked')
        os.symlink(self.outside, link)
        code, out = self.run_cli('prepare', '--path', '.', env=self.env(GITHUB_WORKSPACE=link))
        self.assertEqual(code, 2, out)
        self.assert_outside_kept()

    def test_symlink_to_a_folder_inside_the_runner_workspace_is_refused(self):   # never followed, even inside
        other = os.path.join(self.runner, 'other')
        os.makedirs(other)
        with open(os.path.join(other, 'f'), 'w') as f:
            f.write('x')
        link = os.path.join(self.runner, 'linked')
        os.symlink(other, link)
        code, out = self.run_cli('prepare', '--path', '.', env=self.env(GITHUB_WORKSPACE=link))
        self.assertEqual(code, 2, out)
        self.assertIn('is a symlink', out)
        self.assertEqual(os.listdir(other), ['f'])

    def test_workspace_that_holds_the_action_is_refused(self):   # e.g. a local action path run from the workspace
        self.fill()
        own = os.path.join(self.ws, 'action')
        os.makedirs(own)
        shutil.copy(os.path.join(HERE, 'git_clean.py'), own)
        before = sorted(os.listdir(self.ws))
        r = subprocess.run([sys.executable, '-I', os.path.join(own, 'git_clean.py'), 'prepare'], cwd=self.ws,
                           text=True, capture_output=True, env=self.env())
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('holds this action', r.stdout)
        self.assertEqual(sorted(os.listdir(self.ws)), before)

    def test_the_root_is_refused_by_name(self):   # checked without removing anything
        self.assertEqual(G.workspace({'GITHUB_WORKSPACE': '/'}), (None, 'GITHUB_WORKSPACE is /'))

    def test_stops_half_way_then_runs_again(self):
        """A delete that fails stops the step red (checkout never runs on a half-emptied workspace); the next run
        finishes the job."""
        self.fill()
        real = os.unlink
        calls = []

        def flaky(p, *a, **kw):
            calls.append(p)
            if len(calls) == 2:
                raise PermissionError(13, 'denied', p)
            return real(p, *a, **kw)

        with unittest.mock.patch.object(G.os, 'unlink', flaky):
            why = G.wipe(self.env())
        self.assertIn('could not remove', why)
        self.assertNotEqual(os.listdir(self.ws), [])
        self.assertIsNone(G.wipe(self.env()))
        self.assertEqual(os.listdir(self.ws), [])
        self.assert_outside_kept()

    def test_path_outside_the_workspace_is_refused(self):
        for p in ('/etc', '../x', 'a/../../x', self.ws, os.path.join(self.ws, 'sub')):   # absolute, even inside
            with self.subTest(path=p):
                code, out = self.run_cli('check', '--path', p, env=self.env('github-hosted'))
                self.assertEqual(code, 2, out)
                self.assertIn('inside the workspace', out)


class FixedGitConfig(Base):
    """Every fork-ci git call that reads a checkout runs with no global or system config and the safe flags: a broken
    global/system config (any key in it would be read) breaks nothing, a planted fsmonitor runs nothing."""

    def setUp(self):
        super().setUp()
        self.repo = self.checkout()
        self.plant(self.repo, ('core.fsmonitor', self.touch_cmd()))
        broken = os.path.join(self.outside, 'broken.config')
        with open(broken, 'w') as f:
            f.write('[core\n')
        self.hostile = {'GIT_CONFIG_GLOBAL': broken, 'GIT_CONFIG_SYSTEM': broken, 'GIT_CONFIG_NOSYSTEM': '0',
                        'HOME': self.outside}

    def helpers(self):
        """(name, call(*git args) -> stdout or None) of every module's own git helper."""
        jc = load('job_changed', os.path.join(ACTIONS, 'job-changed', 'job_changed.py'))
        ds = load('docs_scope', os.path.join(ACTIONS, 'docs-scope', 'docs_scope.py'))
        ts = load('test_select', os.path.join(ACTIONS, 'test-select', 'test_select.py'))
        tb = load('tested_base', os.path.join(ACTIONS, 'pr-queue', 'tested-base', 'tested_base.py'))
        sys.path.insert(0, os.path.join(ACTIONS, 'pr-queue'))
        hc = load('head_check', os.path.join(ACTIONS, 'head-check', 'head_check.py'))
        wt = load('weakened_tests', os.path.join(ACTIONS, 'weakened-tests', 'weakened_tests.py'))
        repo = self.repo

        def in_repo(fn):
            def call(*a):
                old = os.getcwd()
                os.chdir(repo)
                try:
                    return fn(*a)
                finally:
                    os.chdir(old)
            return call

        def test_select(*a):
            try:
                return ts.git(*a, cwd=repo)
            except ts.AllTests:
                return None

        def head_check(*a):
            r = hc.Checkout(repo).git(*a)
            return r.stdout if r.returncode == 0 else None

        return (('job_changed', in_repo(jc.git)), ('docs_scope', in_repo(ds.git)), ('test_select', test_select),
                ('tested_base', lambda *a: tb.git(repo, *a)), ('head_check', head_check),
                ('weakened_tests', wt.Git(repo)))

    def test_helpers_ignore_global_and_system_config(self):
        with unittest.mock.patch.dict(os.environ, self.hostile):
            for name, call in self.helpers():
                with self.subTest(module=name):
                    self.assertTrue(call('rev-parse', 'HEAD'), f'{name}: git read the global or system config')

    def test_helpers_ignore_the_home_attributes_file(self):
        """GIT_CONFIG_GLOBAL does not stop git reading ~/.config/git/attributes ($XDG_CONFIG_HOME/git/attributes)."""
        home, xdg = os.path.join(self.d, 'home'), os.path.join(self.d, 'xdg')
        for d in (os.path.join(home, '.config', 'git'), os.path.join(xdg, 'git')):
            os.makedirs(d)
            with open(os.path.join(d, 'attributes'), 'w') as f:
                f.write('a.txt evil=planted\n')
        rest = {k: v for k, v in os.environ.items() if k != 'XDG_CONFIG_HOME'}
        for extra in ({'HOME': home}, {'HOME': home, 'XDG_CONFIG_HOME': xdg}):
            with unittest.mock.patch.dict(os.environ, {**rest, **extra}, clear=True):
                plain = subprocess.run(['git', 'check-attr', 'evil', '--', 'a.txt'], cwd=self.repo, text=True,
                                       capture_output=True, env={**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull,
                                                                 'GIT_CONFIG_NOSYSTEM': '1'}).stdout
                self.assertIn('planted', plain, 'the planted attributes file is not read: the test plants nothing')
                for name, call in self.helpers():
                    with self.subTest(module=name, **extra):
                        out = call('--work-tree', self.repo, 'check-attr', 'evil', '--', 'a.txt')
                        out = out.decode() if isinstance(out, bytes) else out
                        self.assertIn('unspecified', out or '', f'{name}: git read the home attributes file')

    def test_no_system_attributes_either(self):   # $(prefix)/etc/gitattributes cannot be planted in a test
        self.assertEqual(G.GIT_ENV.get('GIT_ATTR_NOSYSTEM'), '1')
        self.assertEqual(G.GIT_SAFE[G.GIT_SAFE.index('core.attributesFile=/dev/null') - 1], '-c')

    def test_helpers_run_no_planted_fsmonitor(self):
        for name, call in self.helpers():
            if name == 'head_check':
                continue   # --git-dir with no work tree: status cannot run, so fsmonitor never could
            with self.subTest(module=name):
                call('status')
                self.assertFalse(os.path.exists(self.marker), f'{name}: git ran the planted core.fsmonitor')
            if os.path.exists(self.marker):
                os.remove(self.marker)

    def test_queue_and_gate_git_ignore_global_and_system_config(self):
        """pr-queue's cache git and init, and the head gate's init (bare repositories, not a checkout)."""
        sys.path.insert(0, os.path.join(ACTIONS, 'pr-queue'))
        sys.path.insert(0, os.path.join(ACTIONS, 'head-check'))
        import pr_queue
        import head_gate
        q = object.__new__(pr_queue.Queue)
        q.cache, q.read_token, q.remote = os.path.join(self.d, 'cache', 'repo.git'), None, 'https://github.com/o/r.git'
        with unittest.mock.patch.dict(os.environ, self.hostile), \
                unittest.mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertTrue(q.own_cache(), out.getvalue())                      # git init --bare
            self.assertEqual(q.git('rev-parse', '--is-bare-repository').stdout.strip(), 'true')   # Cache.git
            path, why = head_gate.bare_repository(self.d, 'gate')
            self.assertIsNone(why)
            self.assertTrue(os.path.isdir(path))

    def test_one_source(self):
        """The constants each self-contained module carries are git_clean's (docs_scope must stay one file for
        actions/pinned, so it cannot import them)."""
        sys.path.insert(0, os.path.join(ACTIONS, 'pr-queue'))
        import pr_queue
        self.assertEqual(pr_queue.GIT_ENV, G.GIT_ENV)
        self.assertEqual(pr_queue.GIT_SAFE, G.GIT_SAFE)
        self.assertEqual(set(pr_queue.UNTRUSTED_FILES), {*G.UNTRUSTED_FILES, 'shallow'})   # git_clean: shallow apart
        for folder, name in (('job-changed', 'job_changed'), ('docs-scope', 'docs_scope'),
                             ('test-select', 'test_select'), ('pr-queue/tested-base', 'tested_base'),
                             ('weakened-tests', 'weakened_tests')):
            with self.subTest(module=name):
                mod = load(name, os.path.join(ACTIONS, folder, f'{name}.py'))
                self.assertEqual(mod.GIT_ENV, G.GIT_ENV)
                self.assertEqual(mod.GIT_SAFE, G.GIT_SAFE)


class Actions(unittest.TestCase):
    """Every action whose code runs git in a checkout runs `git_clean.py check` first; git-clean's own action runs
    `prepare`. Found by reading the folders, so a new action that calls git is caught too."""

    # Actions whose git calls never read a checkout: pr-queue runs git only in its own bare cache (whole config
    # compared with CACHE_CONFIG), head-check's gate in a bare repository it makes.
    OWN_REPOSITORY = {'pr-queue'}

    def action_files(self):
        for root, _, files in os.walk(ACTIONS):
            if 'action.yml' in files and os.path.basename(root) != 'git-clean':
                yield root, os.path.join(root, 'action.yml')

    def test_actions_that_run_git_check_first(self):
        seen = []
        for root, path in self.action_files():
            code = ''
            for f in os.listdir(root):
                if f.endswith('.py') and not f.endswith('.test.py'):
                    with open(os.path.join(root, f), encoding='utf-8') as fh:
                        code += fh.read()
            if "['git'" not in code or os.path.relpath(root, ACTIONS) in self.OWN_REPOSITORY:
                continue
            seen.append(os.path.relpath(root, ACTIONS))
            with open(path, encoding='utf-8') as fh:
                text = fh.read()
            steps = text.split('\n  steps:\n', 1)[1]
            first = ('\n' + steps).split('\n    - ')[1]
            self.assertIn('git-clean/git_clean.py" check --path', first, f'{path}: the first step must be the check')
            self.assertIn('--require', first, path)
        self.assertEqual(sorted(seen), ['docs-scope', 'head-check', 'job-changed', 'pr-queue/tested-base',
                                        'test-select', 'weakened-tests'])

    def test_git_clean_action_runs_prepare(self):
        with open(os.path.join(HERE, 'action.yml'), encoding='utf-8') as fh:
            text = fh.read()
        self.assertIn('git_clean.py" prepare --path "$CHECKOUT_PATH"', text)


if __name__ == '__main__':
    unittest.main()
