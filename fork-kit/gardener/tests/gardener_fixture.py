"""Shared by the gardener tests: a machine file of their own, project checks written on the fly, a fake GitHub, and
RedCheck. No root, no systemd, no network.

The kit reads the machine file at the fixed path; a test points config.MACHINE_PATH at a file in a temporary folder
owned by the user who runs it (config.OWNER_UID). RedCheck copies lib/ to a temporary folder, breaks one text in
one module, and runs one named test against that copy (GARDENER_TEST_LIB names it). The test must then fail.
A module named "checks/NAME" is a general check: it is broken in a copy of checks/ (GARDENER_TEST_CHECKS)."""
import json, os, shutil, subprocess, sys, tempfile, textwrap, unittest
from unittest import mock

sys.dont_write_bytecode = True

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB_ENV = 'GARDENER_TEST_LIB'
LIB = os.environ.get(LIB_ENV) or os.path.join(KIT, 'lib')
CHECKS_ENV = 'GARDENER_TEST_CHECKS'
CHECKS = os.environ.get(CHECKS_ENV) or os.path.join(KIT, 'checks')
sys.path.insert(0, LIB)

from gardener import cli, config, contract, judge, report, runner  # noqa: E402,F401

NOW = '2026-11-23T03:30:00+00:00'      # 03:30 in Europe/London in November (UTC+0)
REPO = 'example-owner/app'      # the test projects' repository and report repository


class FakeGitHub:
    """The calls report.publish and the visibility check make, on an in-memory list of issues (each in a repository;
    the projects' default is REPO)."""

    def __init__(self):
        self.issues = {}
        self.calls = []
        self.fail = None          # a call name that raises GitHubError
        self.body_search_blind = False   # True: a body search finds nothing (search may skip HTML comments)
        self.visibility = {}      # repository: private True/False; a missing one fails the read

    def _maybe_fail(self, what):
        self.calls.append(what)
        if self.fail == what:
            raise report.GitHubError(f'gh api {what}: HTTP 403: API rate limit exceeded (rate limit: the next run '
                                     'tries again)')

    def add(self, body, state='open', title='Gardener report: example', repo=REPO):
        n = len(self.issues) + 1
        self.issues[n] = {'number': n, 'title': title, 'body': body, 'state': state, 'repo': repo}
        return n

    def private(self, repo):
        self._maybe_fail('private')
        if repo not in self.visibility:
            raise report.GitHubError(f'gh api GET repos/{repo}: HTTP 404: Not Found')
        return self.visibility[repo]

    def issue(self, repo, number):
        self._maybe_fail('issue')
        if number not in self.issues or self.issues[number]['repo'] != repo:
            raise report.GitHubError(f'gh api GET repos/{repo}/issues/{number}: HTTP 404: Not Found')
        return dict(self.issues[number])

    def search_open(self, repo, text, field='body'):
        self._maybe_fail('search')
        if field == 'body' and self.body_search_blind:
            return []
        return [dict(i) for i in self.issues.values() if i['state'] == 'open' and i['repo'] == repo
                and text in (i[field] or '')]

    def create(self, repo, title, body):
        self._maybe_fail('create')
        n = self.add(body, title=title, repo=repo)
        return dict(self.issues[n])

    def edit(self, repo, number, body):
        self._maybe_fail('edit')
        self.issues[number]['body'] = body
        return dict(self.issues[number])

    def writes(self):
        return [c for c in self.calls if c in ('create', 'edit')]


class Temp(unittest.TestCase):
    """A temporary folder, a machine file in it, the clock at NOW and a fake GitHub."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='gardener-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, 'repo')
        os.makedirs(self.root)
        self.state_dir = os.path.join(self.tmp, 'state')
        self.machine = os.path.join(self.tmp, 'machine.json')
        for p in (mock.patch.object(config, 'MACHINE_PATH', self.machine),
                  mock.patch.object(config, 'OWNER_UID', os.getuid()),
                  mock.patch.dict(os.environ, {'GARDENER_TEST_NOW': NOW})):
            p.start()
            self.addCleanup(p.stop)
        self.gh = FakeGitHub()

    def script(self, name, body, mode=0o755):
        """An executable Python check script in the temporary folder; its path."""
        path = os.path.join(self.tmp, name)
        with open(path, 'w') as f:
            f.write(f'#!{sys.executable}\n' + textwrap.dedent(body))
        os.chmod(path, mode)
        return path

    def finding_script(self, name, *findings):
        lines = '\n'.join(f'print({json.dumps(json.dumps(f))})' for f in findings)
        return self.script(name, f'import sys\nsys.stdin.read()\n{lines}\n')

    def project(self, checks, **more):
        p = {'name': 'example', 'repo': REPO, 'reportRepo': REPO, 'root': self.root,
             'timeZone': 'Europe/London', 'checks': checks}
        p.update(more)
        return p

    def write_machine(self, *projects, **section):
        data = {'version': 1, 'gardener': dict({'stateDir': self.state_dir, 'projects': list(projects)}, **section)}
        with open(self.machine, 'w') as f:
            json.dump(data, f)
        os.chmod(self.machine, 0o644)

    def run_cli(self, *argv):
        """(exit code, stdout) of cli.main in this process, with the fake GitHub."""
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(list(argv), gh_factory=lambda gh: self.gh)
        return code, out.getvalue()

    def state(self, name='example'):
        with open(os.path.join(self.state_dir, name, 'state.json')) as f:
            return json.load(f)


GATE_FILES = ('config.py', 'lens.py', 'review_config.py', 'review_glob.py', 'review_judge.py', 'review.vectors.json')


def red_check(case, test_file, mutations):
    """mutations: {rule: (module, text, broken text, 'Class.test_name')}."""
    for rule, (module, text, broken, test) in mutations.items():
        with case.subTest(rule=rule):
            tmp = tempfile.mkdtemp(prefix='gardener-red-')
            try:
                lib = os.path.join(tmp, 'lib')
                shutil.copytree(os.path.join(KIT, 'lib'), lib, ignore=shutil.ignore_patterns('__pycache__'))
                os.makedirs(os.path.join(tmp, 'common'))      # where config finds machine_file in an installed copy
                shutil.copy(os.path.join(os.path.dirname(KIT), 'common', 'machine_file.py'),
                            os.path.join(tmp, 'common'))
                env = {LIB_ENV: lib}
                if module.startswith('checks/'):
                    shutil.copytree(os.path.join(KIT, 'checks'), os.path.join(tmp, 'checks'), symlinks=True)
                    gate = os.path.join(os.path.dirname(os.path.dirname(KIT)), 'fork-ci', 'actions', 'pr-queue')
                    os.makedirs(os.path.join(tmp, 'gate'))     # where review-tiers finds it in an installed copy
                    for name in GATE_FILES:
                        shutil.copy(os.path.join(gate, name), os.path.join(tmp, 'gate'))
                    path = env[CHECKS_ENV] = os.path.join(tmp, module)
                else:
                    path = os.path.join(lib, 'gardener', f'{module}.py')
                with open(path, encoding='utf-8') as f:
                    source = f.read()
                case.assertEqual(source.count(text), 1, f'{rule}: the text is not in {module} exactly once')
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(source.replace(text, broken))
                r = subprocess.run([sys.executable, '-I', '-B', test_file, test], capture_output=True, text=True,
                                   timeout=300, env=dict(os.environ, **env), stdin=subprocess.DEVNULL)
                case.assertIn('Ran 1 test', r.stderr, f'{rule}: {test} did not run:\n{r.stderr[-800:]}')
                case.assertNotEqual(r.returncode, 0, f'{test} still passes with {rule!r} broken')
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
