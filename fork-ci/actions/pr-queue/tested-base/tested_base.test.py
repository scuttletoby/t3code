"""Tests of tested_base.py and its action (run: python3 fork-ci/actions/pr-queue/tested-base/tested_base.test.py).

Real commits: a merge commit made the way GitHub makes a pull request's (parents: the base, then the PR head), checked
out fully and as a depth-1 clone. The note it writes is fed to pr_queue.py's reader, so the writer and the reader
cannot drift apart. Nothing here starts gh or reaches the network.
"""
import json, os, shutil, subprocess, sys, tempfile, unittest, unittest.mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import tested_base as T  # noqa: E402
import pr_queue as Q  # noqa: E402

GIT_ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@t', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_ALLOW_PROTOCOL': 'file', 'GIT_TERMINAL_PROMPT': '0'}
os.environ.update({k: GIT_ENV[k] for k in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM', 'GIT_ALLOW_PROTOCOL')})


def git(cwd, *a):
    r = subprocess.run(['git', *a], cwd=cwd, text=True, capture_output=True, env=GIT_ENV)
    if r.returncode != 0:
        raise RuntimeError(f'git {a}: {r.stderr}')
    return r.stdout.strip()


class TestedBase(unittest.TestCase):
    """base B0 -> B1 (the tip the merge was made on); the PR head H on B0; M = GitHub's merge (parents B1, H)."""
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='tested-base-')
        self.addCleanup(shutil.rmtree, self.d, True)
        self.repo = os.path.join(self.d, 'repo')
        git(self.d, 'init', '-q', '-b', 'main', self.repo)
        self.B0 = self.commit({'a': '1\n'})
        self.B1 = self.commit({'a': '1\n', 'b': '2\n'}, self.B0)
        self.H = self.commit({'a': '1\n', 'c': '3\n'}, self.B0)
        self.M = self.commit({'a': '1\n', 'b': '2\n', 'c': '3\n'}, self.B1, self.H)
        git(self.repo, 'branch', 'merge', self.M)
        git(self.repo, 'checkout', '-q', 'merge')
        self.event = os.path.join(self.d, 'event.json')
        self.write_event({'pull_request': {'head': {'sha': self.H}}})

    def commit(self, files, *parents):
        for name in os.listdir(self.repo):
            if name != '.git':
                os.remove(os.path.join(self.repo, name))
        for name, text in files.items():
            with open(os.path.join(self.repo, name), 'w') as f:
                f.write(text)
        git(self.repo, 'add', '-A')
        tree = git(self.repo, 'write-tree')
        return git(self.repo, 'commit-tree', tree, *sum((['-p', p] for p in parents), []), '-m', 'c')

    def write_event(self, payload, raw=None):
        with open(self.event, 'w') as f:
            f.write(raw if raw is not None else json.dumps(payload))

    def run_it(self, path=None, event='pull_request', sha=None):
        env = {'GITHUB_EVENT_NAME': event, 'GITHUB_EVENT_PATH': self.event, 'GITHUB_SHA': sha or self.M}
        r = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'tested_base.py'), '--path', path or self.repo],
                           text=True, capture_output=True, env={**GIT_ENV, **env})
        self.assertEqual(r.returncode, 0, r.stderr)   # never fails the job: no note is read as "unknown"
        return r.stdout

    def assertNoNote(self, out, said):
        self.assertNotIn(f'title={T.TITLE}::', out)
        self.assertIn(f'::warning title={T.TITLE} unknown::', out)
        self.assertIn(said, out)

    def test_the_merge_commit_names_its_first_parent(self):
        self.assertEqual(self.run_it(), f'::notice title={T.TITLE}::{self.B1}\n')

    def test_a_depth_1_clone_still_names_it(self):   # actions/checkout's default fetch-depth
        shallow = os.path.join(self.d, 'shallow')
        git(self.d, 'clone', '-q', '--depth', '1', '--branch', 'merge', 'file://' + self.repo, shallow)
        with self.assertRaises(RuntimeError):
            git(shallow, 'rev-parse', '--verify', 'HEAD^1^{commit}')   # the parent is not there
        self.assertEqual(self.run_it(shallow), f'::notice title={T.TITLE}::{self.B1}\n')

    def test_other_events_write_no_note_and_no_warning(self):
        for event in ('push', 'pull_request_target', 'merge_group', 'schedule', ''):
            with self.subTest(event=event):
                out = self.run_it(event=event)
                self.assertNotIn('::', out)
                self.assertIn('no note', out)

    def test_another_checkout_writes_no_note(self):
        git(self.repo, 'checkout', '-q', '--detach', self.H)   # a job that checked out the PR head: no merge tested
        self.assertNoNote(self.run_it(), f'has {self.H} checked out, not the run\'s merge commit')
        self.assertNoNote(self.run_it(os.path.join(self.d, 'nothing-here')), 'has no commit checked out')

    def test_a_commit_that_is_no_merge_of_the_pr_head_writes_no_note(self):
        git(self.repo, 'checkout', '-q', '--detach', self.B1)
        self.assertNoNote(self.run_it(sha=self.B1), 'has 1 parents')
        other = self.commit({'d': '4\n'}, self.B1, self.B0)   # a merge, but not of the event's PR head
        git(self.repo, 'checkout', '-q', '--detach', other)
        self.assertNoNote(self.run_it(sha=other), f'not the pull request head {self.H[:12]}')

    def test_an_odd_event_file_or_sha_writes_no_note(self):
        for payload, raw in (({}, None), ({'pull_request': {'head': {'sha': 'main'}}}, None), (None, '{')):
            with self.subTest(payload=payload, raw=raw):
                self.write_event(payload, raw)
                self.assertNoNote(self.run_it(), 'names no pull request head')
        self.write_event({'pull_request': {'head': {'sha': self.H}}})
        self.assertNoNote(self.run_it(sha='HEAD'), "GITHUB_SHA 'HEAD' is no commit")

    def test_the_queue_reads_the_note_and_ignores_the_warning(self):   # the writer and the reader agree
        self.assertEqual(T.TITLE, Q.TESTED_BASE)

        def annotations(out):   # what GitHub keeps of each `::level title=...::message` line
            notes = []
            for line in out.splitlines():
                if line.startswith('::'):
                    head, message = line[2:].split('::', 1)
                    notes.append({'title': head.split(' ', 1)[1].split('=', 1)[1], 'message': message})
            return notes
        q = object.__new__(Q.Queue)   # only tested_base(): no config, STATE or cache
        q.repo = 'owner/proj'
        job = {'name': 'build', 'detailsUrl': 'https://github.com/owner/proj/actions/runs/9/job/5'}
        for out, want in ((self.run_it(), self.B1), (self.run_it(sha=self.B1), None)):
            q.gh = lambda *a, n=annotations(out): Q.subprocess.CompletedProcess(a, 0, json.dumps(n), '')
            with open(os.devnull, 'w') as null, unittest.mock.patch('sys.stdout', null):
                self.assertEqual(q.tested_base(job), want)

    def test_the_action_runs_the_script_on_its_path_input(self):
        with open(os.path.join(HERE, 'action.yml')) as f:
            text = f.read()
        self.assertIn('CHECKOUT_PATH: ${{ inputs.path }}', text)
        run = 'python3 -I "$GITHUB_ACTION_PATH/tested_base.py" --path "$CHECKOUT_PATH"'
        self.assertIn(f'run: {run}\n', text)
        r = subprocess.run(['bash', '-c', run], text=True, capture_output=True,
                           env={**GIT_ENV, 'GITHUB_ACTION_PATH': HERE, 'CHECKOUT_PATH': self.repo,
                                'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_EVENT_PATH': self.event, 'GITHUB_SHA': self.M})
        self.assertEqual((r.returncode, r.stdout), (0, f'::notice title={T.TITLE}::{self.B1}\n'), r.stderr)


if __name__ == '__main__':
    unittest.main()
