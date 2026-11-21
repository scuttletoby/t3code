"""Shared by the schedule-kick tests: where the code is loaded from, a fake GitHub, and RedCheck.

RedCheck copies lib/ to a temporary folder, breaks one text in one module, and runs one named test of a test file
against that copy (in a child Python, SCHEDULE_KICK_TEST_LIB naming the copy). The test must then fail."""
import datetime, os, shutil, subprocess, sys, tempfile

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB_ENV = 'SCHEDULE_KICK_TEST_LIB'
LIB = os.environ.get(LIB_ENV) or os.path.join(KIT, 'lib')
sys.path.insert(0, LIB)

from schedulekick import cron, github, kick  # noqa: E402,F401

UTC = datetime.timezone.utc


def at(text):
    return datetime.datetime.fromisoformat(text).replace(tzinfo=UTC)


class FakeApi:
    """GitHub's run list and dispatch for some workflows. GitHub has its own clock (`github_now`): a run a
    dispatch makes is created then, whatever the host's clock says."""

    def __init__(self, github_now):
        self.github_now = github_now
        self.runs = {}              # (repo, workflow) -> [(created ISO, event)]
        self.calls = []
        self.fail = {}              # 'list' | 'dispatch' -> ApiError to raise
        self.ignore_filter = False  # answer every run, as if GitHub ignored `created`
        self.remaining = None
        self.after_dispatch = None  # called after a dispatch is made (a stop half-way)

    def add_run(self, repo, workflow, created, event='schedule'):
        self.runs.setdefault((repo, workflow), []).append((created, event))

    def runs_since(self, repo, workflow, since):
        self.calls.append(('list', repo, workflow, github.iso(since)))
        if 'list' in self.fail:
            raise self.fail['list']
        found = sorted(self.runs.get((repo, workflow), []), reverse=True)
        return [c for c, _ in found if self.ignore_filter or c >= github.iso(since)][:github.LIST_LIMIT]

    def dispatch(self, repo, workflow, ref, inputs):
        self.calls.append(('dispatch', repo, workflow, ref, dict(inputs)))
        if 'dispatch' in self.fail:
            raise self.fail['dispatch']
        self.add_run(repo, workflow, github.iso(self.github_now), 'workflow_dispatch')
        if self.after_dispatch:
            self.after_dispatch()

    def dispatches(self):
        return [c for c in self.calls if c[0] == 'dispatch']

    def lists(self):
        return [c for c in self.calls if c[0] == 'list']


def red_check(case, test_file, mutations):
    """mutations: {rule: (module, text, broken text, 'Class.test_name')}."""
    for rule, (module, text, broken, test) in mutations.items():
        with case.subTest(rule=rule):
            tmp = tempfile.mkdtemp(prefix='schedule-kick-red-')
            try:
                lib = os.path.join(tmp, 'lib')
                shutil.copytree(os.path.join(KIT, 'lib'), lib, ignore=shutil.ignore_patterns('__pycache__'))
                path = os.path.join(lib, 'schedulekick', f'{module}.py')
                with open(path, encoding='utf-8') as f:
                    source = f.read()
                case.assertEqual(source.count(text), 1, f'{rule}: the text is not in {module}.py exactly once')
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(source.replace(text, broken))
                r = subprocess.run([sys.executable, '-I', '-B', test_file, test], capture_output=True, text=True,
                                   timeout=300, env=dict(os.environ, **{LIB_ENV: lib}), stdin=subprocess.DEVNULL)
                case.assertIn('Ran 1 test', r.stderr, f'{rule}: {test} did not run:\n{r.stderr[-800:]}')
                case.assertNotEqual(r.returncode, 0, f'{test} still passes with {rule!r} broken')
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
