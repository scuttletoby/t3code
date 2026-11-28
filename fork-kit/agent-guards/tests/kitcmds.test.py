"""Tests of the ready-made commands (fork-kit/agent-guards/COMMANDS.md, lib/agent_guards/kitcmds.py): fork-ci-why,
fork-clone and fork-precheck run as programs with stand-ins for gh, vp, pnpm and t3-capacity on PATH (each logs its
call) and real git repositories in a temporary folder; and the kit's table, which facts reads so agents find them.
No network, no root.

  python3 -I -B fork-kit/agent-guards/tests/kitcmds.test.py
"""
import json, os, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guards_fixture as fx  # noqa: E402
from agent_guards import facts  # noqa: E402

KIT = fx.KIT
SYSTEM_PATH = '/usr/bin:/bin'   # git, python3 and sh; never /usr/local/bin (a real t3-capacity or vp there)
GIT_ENV = {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}
# A stand-in that logs its name, folder and words as one JSON line to $FAKE_LOG, then does what the test set up.
LOGGER = ('import json, os, sys\n'
          'open(os.environ["FAKE_LOG"], "a").write(json.dumps([os.path.basename(sys.argv[0]), os.getcwd()]'
          ' + sys.argv[1:]) + "\\n")\n')


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    os.chmod(path, mode)
    return path


def git(*args, cwd):
    r = subprocess.run(['git', *args], cwd=cwd, text=True, capture_output=True,
                       env=dict(os.environ, **GIT_ENV, GIT_CONFIG_GLOBAL=os.devnull))
    if r.returncode != 0:
        raise AssertionError(f'git {" ".join(args)}: {r.stderr}')
    return r.stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='kitcmds-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, 'fakebin')
        self.log = os.path.join(self.tmp, 'calls.log')
        os.makedirs(self.bin)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'FORK_', 'FAKE_'))}
        self.env.update(GIT_ENV, PATH=f'{self.bin}:{SYSTEM_PATH}', HOME=self.tmp, FAKE_LOG=self.log,
                        GIT_CONFIG_GLOBAL=os.path.join(self.tmp, 'gitconfig'))

    def fake(self, name, body=''):
        write(os.path.join(self.bin, name), f'#!{sys.executable}\n{LOGGER}{body}\n', 0o755)

    def run_cmd(self, name, *args, cwd=None, env=None):
        return subprocess.run([sys.executable, '-I', '-B', os.path.join(KIT, 'bin', name), *args],
                              cwd=cwd or self.tmp, env=dict(self.env, **(env or {})), text=True, capture_output=True,
                              timeout=120)

    def calls(self, name=None):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            out = [json.loads(line) for line in f]
        return [c for c in out if name is None or c[0] == name]


# fork-ci-why ----------------------------------------------------------------------------------------------------

LOG = '\n'.join([
    '2026-11-27T10:00:00.1234567Z ##[group]Run vp test',
    '2026-11-27T10:00:01.0000000Z \x1b[32m✓\x1b[39m passes one',
    '2026-11-27T10:00:01.5000000Z ✔ a bad call gets one error line (2ms)',
    '2026-11-27T10:00:02.0000000Z \x1b[31m×\x1b[39m src/a.test.ts > adds',
    '2026-11-27T10:00:02.5000000Z AssertionError: expected 2 to be 3',
    '2026-11-27T10:00:02.6000000Z AssertionError: expected 2 to be 3',
    '2026-11-27T10:00:03.0000000Z Found 0 errors in 12 files',
    '2026-11-27T10:00:04.0000000Z ##[error]Process completed with exit code 1.',
    '2026-11-27T10:00:05.0000000Z ##[endgroup]',
])


def run(name, conclusion, job=None, status='COMPLETED', workflow='CI'):
    return {'__typename': 'CheckRun', 'name': name, 'status': status, 'conclusion': conclusion,
            'workflowName': workflow, 'detailsUrl': f'https://github.com/o/r/actions/runs/11/job/{job}' if job else ''}


CHECKS = [
    run('Unit tests', 'FAILURE', 22),
    run('Lint', 'SUCCESS', 23),
    {'__typename': 'StatusContext', 'context': 'gate', 'state': 'FAILURE', 'targetUrl': 'https://example.invalid/g'},
    run('Build', '', status='IN_PROGRESS'),
    run('Optional', 'FAILURE', 24),
]
REQUIRED = ['Unit tests', 'Lint', 'gate', 'Build', 'Docs']   # Docs: required, not reported yet


class CiWhy(Base):
    def gh(self, checks=CHECKS, required=REQUIRED, logs=None, view_fails=False):
        """A gh that answers `pr view` with `checks`, the base branch's rules with `required` (branch protection: 404,
        as for a user who may not read it), and `api .../jobs/<id>/logs` with logs[id]."""
        data = {'view': {'url': 'https://github.com/o/r/pull/42', 'baseRefName': 'main', 'statusCheckRollup': checks},
                'rules': [{'type': 'pull_request'}] + ([{'type': 'required_status_checks', 'parameters': {
                    'required_status_checks': [{'context': n} for n in required]}}] if required else []),
                'logs': logs if logs is not None else {'22': LOG}}
        write(os.path.join(self.tmp, 'gh.json'), json.dumps(data))
        self.fake('gh', f'''
d, a = json.load(open({os.path.join(self.tmp, 'gh.json')!r})), sys.argv[1:]
if a[:2] == ['pr', 'view']:
    if {view_fails!r}:
        sys.stderr.write('gh: not logged in\\n'); sys.exit(4)
    print(json.dumps(d['view'])); sys.exit(0)
if a[0] == 'api' and '/rules/branches/' in a[1]:
    print(json.dumps(d['rules'])); sys.exit(0)
if a[0] == 'api' and a[1].endswith('/logs') and a[1].split('/')[-2] in d['logs']:
    sys.stdout.write(d['logs'][a[1].split('/')[-2]]); sys.exit(0)
sys.stderr.write('HTTP 404: Not Found\\n'); sys.exit(1)''')

    def test_a_failing_required_job_prints_its_link_and_only_its_error_lines(self):
        self.gh()
        r = self.run_cmd('fork-ci-why', '42', '-R', 'o/r')
        self.assertEqual(r.returncode, 1, r.stderr)
        out = r.stdout
        self.assertIn('FAILED Unit tests (CI): https://github.com/o/r/actions/runs/11/job/22', out)
        self.assertIn('  × src/a.test.ts > adds', out)
        self.assertEqual(out.count('AssertionError: expected 2 to be 3'), 1, 'a repeated line once')
        self.assertIn('  Process completed with exit code 1.', out)
        for noise in ('passes one', 'a bad call', '0 errors', '##[', '\x1b', '2026-11-27T'):
            self.assertNotIn(noise, out)
        self.assertNotIn('Lint', out)
        self.assertNotIn('Optional', out, 'not required')
        self.assertIn('FAILED gate: https://example.invalid/g\n  not an Actions job', out)
        self.assertIn('not reported yet (required): Docs', out)
        self.assertIn('2 failing required check(s) of 5, 2 pending', out)
        calls = [c[2:] for c in self.calls('gh')]
        self.assertIn(['pr', 'view', '42', '--json', 'url,baseRefName,statusCheckRollup', '-R', 'o/r'], calls)
        self.assertIn(['api', 'repos/o/r/rules/branches/main'], calls)
        self.assertIn(['api', 'repos/o/r/actions/jobs/22/logs'], calls)

    def test_without_r_it_reads_the_fork_s_pr_and_says_which_one_first(self):
        self.gh()
        r = self.run_cmd('fork-ci-why', '42')
        self.assertEqual(r.stdout.splitlines()[0], 'fork-ci-why: PR https://github.com/o/r/pull/42')
        self.assertEqual(self.calls('gh')[0][2:], ['pr', 'view', '42', '--json', 'url,baseRefName,statusCheckRollup',
                                                   '-R', 'scuttletoby/t3code'])

    def test_all_shows_failing_checks_that_are_not_required(self):
        self.gh(logs={'22': LOG, '24': 'Error: optional broke\n'})
        r = self.run_cmd('fork-ci-why', '42', '--all')
        self.assertIn('FAILED Optional (CI)', r.stdout)
        self.assertIn('  Error: optional broke', r.stdout)
        self.assertNotIn('rules/branches', json.dumps(self.calls('gh')), '--all reads no rules')

    def test_lines_caps_each_job_and_says_how_many_more(self):
        self.gh()
        r = self.run_cmd('fork-ci-why', '42', '--lines', '1')
        self.assertIn('  +2 more error lines', r.stdout)

    def test_no_failing_check_is_exit_0(self):
        self.gh(checks=[CHECKS[1], CHECKS[3]], required=['Lint', 'Build'])
        r = self.run_cmd('fork-ci-why', '42')
        self.assertEqual((r.returncode, r.stdout.strip().splitlines()[-1]),
                         (0, 'fork-ci-why: no failing required check (2 required checks, 1 pending)'))

    def test_no_readable_required_checks_falls_back_to_every_check_with_a_note(self):
        self.gh(required=[])
        r = self.run_cmd('fork-ci-why', '42')
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('note: no required check can be read', r.stdout)
        self.assertIn('FAILED Optional', r.stdout)

    def test_a_log_with_no_error_line_shows_its_last_lines_and_an_unreadable_log_says_so(self):
        self.gh(logs={'22': '2026-11-27T10:00:00Z step one\n2026-11-27T10:00:01Z step two\n'})
        r = self.run_cmd('fork-ci-why', '42')
        self.assertIn("no error line found; the log's last lines:\n  step one\n  step two", r.stdout)
        self.gh(logs={})
        r = self.run_cmd('fork-ci-why', '42')
        self.assertIn('the job log cannot be read: HTTP 404', r.stdout)

    def test_gh_failing_or_a_wrong_call_is_exit_2(self):
        self.gh(view_fails=True)
        r = self.run_cmd('fork-ci-why', '42')
        self.assertEqual(r.returncode, 2)
        self.assertIn('cannot read the checks of 42 in scuttletoby/t3code: gh: not logged in', r.stderr)
        self.assertEqual(self.run_cmd('fork-ci-why').returncode, 2)
        self.assertEqual(self.run_cmd('fork-ci-why', '42', '--lines', 'x').returncode, 2)
        self.assertEqual(self.run_cmd('fork-ci-why', '--help').returncode, 0)


# fork-clone -----------------------------------------------------------------------------------------------------

class Clone(Base):
    def setUp(self):
        super().setUp()
        self.origin = os.path.join(self.tmp, 'origin')
        os.makedirs(self.origin)
        git('init', '-q', '-b', 'main', cwd=self.origin)
        git('config', 'uploadpack.allowFilter', 'true', cwd=self.origin)
        write(os.path.join(self.origin, 'pnpm-lock.yaml'), 'lockfileVersion: 9\n')
        write(os.path.join(self.origin, 'README.md'), 'x\n')
        write(os.path.join(self.origin, '.gitignore'), 'node_modules\n')   # as the fork's own
        git('add', '-A', cwd=self.origin)
        git('commit', '-q', '-m', 'base', cwd=self.origin)
        git('branch', 'feature', cwd=self.origin)
        self.url = f'file://{self.origin}'
        self.work = os.path.join(self.tmp, 'work')
        # vp install: logged; makes node_modules/.modules.yaml as a real install would. FAKE_INSTALL_FAIL fails it,
        # FAKE_OFFLINE_FAIL fails only an offline one.
        self.fake('vp', '''
if os.environ.get('FAKE_INSTALL_FAIL') or ('--offline' in sys.argv and os.environ.get('FAKE_OFFLINE_FAIL')):
    sys.exit(1)
os.makedirs('node_modules', exist_ok=True)
open('node_modules/.modules.yaml', 'w').close()''')

    def clone(self, *args, env=None):
        return self.run_cmd('fork-clone', *args, '--repo', self.url, '--into', self.work, env=env)

    def installs(self):
        return [c for c in self.calls('vp') if c[2] == 'install']

    def test_a_new_clone_is_blob_less_installed_once_and_leaves_nothing_half_made(self):
        r = self.clone('a')
        self.assertEqual(r.returncode, 0, r.stderr)
        dest = os.path.join(self.work, 'a')
        self.assertEqual(git('config', 'remote.origin.partialclonefilter', cwd=dest), 'blob:none')
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=dest), 'main')
        self.assertEqual(self.installs(), [['vp', dest, 'install', '--frozen-lockfile']])
        self.assertFalse(os.path.exists(os.path.join(dest, '.git', 'fork-clone-pending')))
        self.assertFalse(os.path.exists(os.path.join(self.work, '.a.partial')))
        self.assertIn('dependencies: installed', r.stdout)

    def test_a_second_clone_with_the_same_lockfile_installs_offline_from_the_store(self):
        self.clone('a')
        r = self.clone('b')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.installs()[-1][2:], ['install', '--frozen-lockfile', '--offline'])
        self.assertIn('dependencies: offline from the store a uses', r.stdout)
        self.assertFalse(os.path.islink(os.path.join(self.work, 'b', 'node_modules')))

    def test_an_offline_install_that_fails_installs_online(self):
        self.clone('a')
        r = self.clone('b', env={'FAKE_OFFLINE_FAIL': '1'})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c[3:] for c in self.installs()[-2:]], [['--frozen-lockfile', '--offline'],
                                                                  ['--frozen-lockfile']])

    def test_run_again_reuses_the_clone_and_installs_nothing(self):
        self.clone('a')
        git('commit', '-q', '--allow-empty', '-m', 'newer', cwd=self.origin)
        r = self.clone('a')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('reusing', r.stdout)
        self.assertIn('dependencies: kept (already installed)', r.stdout)
        self.assertEqual(len(self.installs()), 1)
        self.assertEqual(git('log', '-1', '--format=%s', 'origin/main', cwd=os.path.join(self.work, 'a')), 'newer')
        self.assertEqual(git('log', '-1', '--format=%s', 'main', cwd=os.path.join(self.work, 'a')), 'newer',
                         'the checked-out branch is fast-forwarded')
        self.assertIn('newer', r.stdout)

    def test_run_again_leaves_a_branch_with_its_own_commits_and_says_so(self):
        self.clone('a')
        dest = os.path.join(self.work, 'a')
        git('commit', '-q', '--allow-empty', '-m', 'mine', cwd=dest)
        git('commit', '-q', '--allow-empty', '-m', 'newer', cwd=self.origin)
        r = self.clone('a')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(git('log', '-1', '--format=%s', 'main', cwd=dest), 'mine')
        self.assertIn('note: main is left as it was, not at origin/main', r.stdout)

    def test_run_again_on_a_branch_with_another_lockfile_installs_again(self):
        git('checkout', '-q', 'feature', cwd=self.origin)
        write(os.path.join(self.origin, 'pnpm-lock.yaml'), 'lockfileVersion: 9\nother: 1\n')
        git('commit', '-q', '-am', 'other lock', cwd=self.origin)
        git('checkout', '-q', 'main', cwd=self.origin)
        self.clone('a', 'main')
        r = self.clone('a', 'feature')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('pnpm-lock.yaml is not the one installed: installing again', r.stdout)
        self.assertIn('dependencies: installed', r.stdout)
        self.assertEqual(len(self.installs()), 2)
        r = self.clone('a', 'feature')
        self.assertIn('dependencies: kept (already installed)', r.stdout)
        self.assertEqual(len(self.installs()), 2)

    def test_a_branch_the_remote_has_is_checked_out_and_a_new_one_is_made_from_the_default_branch(self):
        self.clone('a', 'feature')
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=os.path.join(self.work, 'a')), 'feature')
        r = self.clone('b', 'mine')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('new branch mine from origin/main', r.stdout)
        dest = os.path.join(self.work, 'b')
        self.assertEqual(git('rev-parse', 'mine', cwd=dest), git('rev-parse', 'origin/main', cwd=dest))

    def test_a_clone_stopped_half_way_is_made_again(self):
        write(os.path.join(self.work, '.a.partial', 'junk'), 'half a clone\n')
        r = self.clone('a')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('removing', r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.work, '.a.partial')))
        self.assertTrue(os.path.isdir(os.path.join(self.work, 'a', '.git')))

    def test_an_install_stopped_half_way_is_done_again_on_the_next_run(self):
        r = self.clone('a', env={'FAKE_INSTALL_FAIL': '1'})
        self.assertEqual(r.returncode, 1)
        self.assertTrue(os.path.exists(os.path.join(self.work, 'a', '.git', 'fork-clone-pending')))
        r = self.clone('a')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('its last install did not finish', r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.work, 'a', '.git', 'fork-clone-pending')))
        self.assertEqual(len(self.installs()), 2)

    def test_a_folder_that_is_something_else_is_left_alone_and_an_empty_one_is_used(self):
        other = write(os.path.join(self.work, 'notes', 'keep.txt'), 'mine\n')
        r = self.clone('notes')
        self.assertEqual(r.returncode, 2)
        self.assertIn('is not a git clone: left alone', r.stderr)
        with open(other) as f:
            self.assertEqual(f.read(), 'mine\n')
        elsewhere = os.path.join(self.work, 'elsewhere')
        os.makedirs(elsewhere)
        git('init', '-q', cwd=elsewhere)
        git('remote', 'add', 'origin', 'https://example.invalid/other/repo.git', cwd=elsewhere)
        r = self.clone('elsewhere')
        self.assertEqual(r.returncode, 2)
        self.assertIn('not file://', r.stderr)
        os.makedirs(os.path.join(self.work, 'empty'))
        self.assertEqual(self.clone('empty').returncode, 0)

    def test_a_linked_node_modules_is_never_installed_into(self):
        self.clone('a')
        dest = os.path.join(self.work, 'b')
        git('clone', '-q', self.url, dest, cwd=self.tmp)
        os.symlink(os.path.join(self.work, 'a', 'node_modules'), os.path.join(dest, 'node_modules'))
        open(os.path.join(dest, '.git', 'fork-clone-pending'), 'w').close()
        r = self.clone('b')
        self.assertEqual(r.returncode, 1)
        self.assertIn('is a link to', r.stderr)
        self.assertEqual(len(self.installs()), 1)

    def test_no_lockfile_no_install_and_no_repository_is_a_wrong_call(self):
        git('rm', '-q', 'pnpm-lock.yaml', cwd=self.origin)
        git('commit', '-q', '-m', 'no lock', cwd=self.origin)
        r = self.clone('a')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('dependencies: skipped (no pnpm-lock.yaml)', r.stdout)
        self.assertEqual(self.installs(), [])
        r = self.run_cmd('fork-clone', 'b', '--into', self.work)
        self.assertEqual(r.returncode, 2)
        self.assertIn('no repository', r.stderr)
        self.assertEqual(self.run_cmd('fork-clone', '../x', '--into', self.work).returncode, 2)

    def test_two_runs_for_one_name_do_not_overlap(self):
        os.makedirs(self.work)
        holder = subprocess.Popen([sys.executable, '-c', 'import fcntl, sys, time; f = open(sys.argv[1], "a"); '
                                   'fcntl.flock(f, fcntl.LOCK_EX); print("held", flush=True); time.sleep(60)',
                                   os.path.join(self.work, '.a.lock')], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        r = self.clone('a')
        self.assertEqual(r.returncode, 2)
        self.assertIn('another fork-clone of a is running', r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.work, 'a')))


# fork-precheck --------------------------------------------------------------------------------------------------

# The fork checks fork-precheck runs: the real scripts and workflow, so the test breaks when CI's steps change shape.
REPO_TOP = os.path.dirname(os.path.dirname(KIT))
FORK_CHECK_FILES = ('.github/workflows/fork-checks.yml', '.github/scripts/fork-schema-check.cjs',
                    '.github/scripts/fork-upstream-files-check.cjs', '.github/scripts/fork-seam-check.cjs')
with open(os.path.join(REPO_TOP, FORK_CHECK_FILES[0])) as _f:   # the upstream-files check's list, as CI has it
    UPSTREAM_FILES = next(ln.split(':', 1)[1].strip() for ln in _f if ln.strip().startswith('UPSTREAM_FILES:'))
ORCHESTRATOR = UPSTREAM_FILES.split()[0]   # one file it guards
# A stand-in check script (node): logs its name and words to $FAKE_LOG, then passes.
NODE_LOGGER = ('const fs = require("node:fs"), path = require("node:path");\n'
               'fs.appendFileSync(process.env.FAKE_LOG, JSON.stringify([path.basename(process.argv[1]), '
               'process.cwd(), ...process.argv.slice(2)]) + "\\n");\n')


class Precheck(Base):
    def setUp(self):
        super().setUp()
        node = shutil.which('node')
        self.assertTrue(node, 'the fork checks need node on PATH')
        real = subprocess.run([node, '-p', 'process.execPath'], capture_output=True, text=True).stdout.strip()
        os.symlink(real or node, os.path.join(self.bin, 'node'))
        bare = os.path.join(self.tmp, 'bare.git')
        git('init', '-q', '--bare', '-b', 'main', bare, cwd=self.tmp)
        self.repo = os.path.join(self.tmp, 'repo')
        git('clone', '-q', bare, self.repo, cwd=self.tmp)
        for f, text in {'src/a.ts': 'a\n', 'src/a.test.ts': 't\n', 'src/b.ts': 'b\n', 'tools/x.py': 'x\n',
                        'tools/x.test.py': 'print("x ok")\n', 'gone.ts': 'g\n', 'README.md': 'r\n',
                        **{f: 'up\n' for f in UPSTREAM_FILES.split()},
                        '.github/fork-seams.json': json.dumps({'files': {}, 'forkFiles': ['src/', 'tools/', 'gone.ts']})
                        }.items():
            write(os.path.join(self.repo, f), text)
        for f in FORK_CHECK_FILES:
            shutil.copy(os.path.join(REPO_TOP, f), os.path.join(os.makedirs(os.path.join(self.repo, os.path.dirname(f)),
                                                                             exist_ok=True) or self.repo, f))
        git('add', '-A', cwd=self.repo)
        git('commit', '-q', '-m', 'base', cwd=self.repo)
        git('push', '-q', 'origin', 'main', cwd=self.repo)
        git('remote', 'set-head', 'origin', 'main', cwd=self.repo)
        git('checkout', '-q', '-b', 'change', cwd=self.repo)
        write(os.path.join(self.repo, 'src/a.ts'), 'a2\n')           # committed on the branch
        git('rm', '-q', 'gone.ts', cwd=self.repo)                    # deleted: never passed
        git('commit', '-q', '-am', 'change', cwd=self.repo)
        write(os.path.join(self.repo, 'tools/x.test.py'), 'print("x ok 2")\n')   # unstaged
        write(os.path.join(self.repo, 'new notes.md'), 'n\n')                    # new, with a space
        # vp: logged; FAKE_LINT_FAIL fails lint. t3-capacity (made per test): FAKE_CAPACITY exit code.
        self.fake('vp', 'sys.exit(1 if sys.argv[1] == "lint" and os.environ.get("FAKE_LINT_FAIL") else 0)')

    def capacity(self):
        self.fake('t3-capacity', 'code = int(os.environ.get("FAKE_CAPACITY", "0"))\n'
                                 'print("available=9000MB"); print("OK" if code == 0 else "NOT OK: busy"); '
                                 'sys.exit(code)')

    def precheck(self, *args, env=None):
        return self.run_cmd('fork-precheck', *args, cwd=os.path.join(self.repo, 'src'), env=env)

    def words(self, name):
        return [c[2:] for c in self.calls(name)]

    def test_fmt_and_lint_get_the_changed_files_and_the_touched_tests_run_after_the_capacity_check(self):
        self.capacity()
        r = self.precheck()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        changed = ['src/a.ts', 'tools/x.test.py', 'new notes.md']
        self.assertEqual(self.words('vp'), [['fmt', '--no-error-on-unmatched-pattern', *changed],
                                            ['lint', '--no-error-on-unmatched-pattern', *changed],
                                            ['test', 'run', 'src/a.test.ts']])
        self.assertTrue(all(c[1] == self.repo for c in self.calls('vp')), 'run from the checkout\'s top')
        self.assertIn('x ok 2', r.stdout, 'the touched Python test ran')
        self.assertIn('capacity check: OK', r.stdout)
        self.assertIn('OK: fmt, lint, fork checks and 2 test file(s)', r.stdout)
        self.assertIn('note: uncommitted changes are not in the fork checks', r.stdout)
        self.assertNotIn('FAILED', r.stdout + r.stderr)
        self.assertNotIn('No database schema changes', r.stdout + r.stderr, 'a passing check prints nothing')

    def test_capacity_not_ok_runs_fmt_and_lint_and_no_test(self):
        self.capacity()
        r = self.precheck(env={'FAKE_CAPACITY': '1'})
        self.assertEqual(r.returncode, 3)
        self.assertIn('capacity check: NOT OK: busy: the 2 test file(s) did not run', r.stdout)
        self.assertEqual([w[0] for w in self.words('vp')], ['fmt', 'lint'])
        self.assertNotIn('x ok', r.stdout)

    def test_a_capacity_check_that_cannot_take_the_call_is_exit_2_never_the_retry_code(self):
        # the shell t3-capacity takes only [MB]: --kind is "not a number", exit 2
        self.fake('t3-capacity', 'print("NOT OK: \'--kind\' is not a number of MB", file=sys.stderr); sys.exit(2)')
        r = self.precheck('--kind', 'agent-writer')
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('capacity check failed (exit 2)', r.stderr)
        self.assertIn('does not take --kind', r.stderr)
        self.assertNotIn('again later', r.stdout + r.stderr)
        self.assertNotIn('x ok', r.stdout)

    def test_capacity_not_ok_after_a_lint_failure_is_exit_1(self):
        self.capacity()
        r = self.precheck(env={'FAKE_CAPACITY': '1', 'FAKE_LINT_FAIL': '1'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('FAILED: lint (exit 1)', r.stdout)
        self.assertIn('the 2 test file(s) did not run', r.stdout)

    def test_a_source_file_s_test_in_a_tests_folder_above_it_runs(self):
        self.commit({'tools/lib/pkg/y.py': 'y\n', 'tools/tests/y.test.py': 'print("y ok")\n'})
        write(os.path.join(self.repo, 'tools/lib/pkg/y.py'), 'y2\n')
        r = self.precheck('--base', 'HEAD', '--no-fork-checks')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('y ok', r.stdout)

    def test_the_default_base_is_fetched_first_and_its_commit_named(self):
        old = git('rev-parse', 'origin/main', cwd=self.repo)
        self.on_main(lambda: self.commit({'tools/z.py': 'z\n'}, 'later on main'))
        new = git('rev-parse', 'origin/main', cwd=self.repo)
        git('update-ref', 'refs/remotes/origin/main', old, cwd=self.repo)      # as last fetched long ago
        r = self.precheck('--no-tests', '--no-fork-checks')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(git('rev-parse', 'origin/main', cwd=self.repo), new)
        self.assertIn(f'base origin/main: {new[:7]}', r.stdout)
        git('remote', 'set-url', 'origin', os.path.join(self.tmp, 'gone.git'), cwd=self.repo)
        r = self.precheck('--no-tests', '--no-fork-checks')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('note: cannot fetch origin/main', r.stdout)
        self.assertRegex(r.stdout, rf'base origin/main: {new[:7]}.* ago')

    def test_no_capacity_check_on_the_machine_runs_the_tests_with_a_note(self):
        r = self.precheck()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('note: no t3-capacity on this machine', r.stdout)
        self.assertIn(['test', 'run', 'src/a.test.ts'], self.words('vp'))

    def test_a_lint_failure_still_runs_the_tests_and_fails_at_the_end(self):
        r = self.precheck(env={'FAKE_LINT_FAIL': '1'})
        self.assertEqual(r.returncode, 1)
        self.assertIn('FAILED: lint (exit 1)', r.stdout)
        self.assertIn('x ok 2', r.stdout)

    def test_no_tests_and_another_base(self):
        r = self.precheck('--no-tests', '--base', 'HEAD')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([w[-1] for w in self.words('vp')], ['new notes.md', 'new notes.md'],
                         'against HEAD: only the uncommitted files')

    def commit(self, files, msg='more'):
        for f, text in files.items():
            write(os.path.join(self.repo, f), text)
        git('add', '-A', '--', *files, cwd=self.repo)
        git('commit', '-q', '-m', msg, cwd=self.repo)

    def on_main(self, change):
        """Commits change(files) on main (pushed: origin/main moves past the merge-base) and comes back."""
        git('checkout', '-q', 'main', cwd=self.repo)
        change()
        git('push', '-q', 'origin', 'main', cwd=self.repo)
        git('checkout', '-q', 'change', cwd=self.repo)
        git('fetch', '-q', 'origin', cwd=self.repo)

    def test_each_fork_check_a_change_breaks_fails_with_its_message(self):
        cases = {
            'schema': ({'apps/server/src/persistence/Migrations/099_Fork.ts': 'm\n'},
                       'Fork changes must not change the database schema'),
            'upstream-files': ({ORCHESTRATOR: 'o2\n'},
                               f'- {ORCHESTRATOR}: this branch changes it'),
            'seam': ({'README.md': 'r2\n'}, 'README.md: upstream file with 1 fork line but no entry'),
        }
        for name, (files, message) in cases.items():
            with self.subTest(name):
                git('reset', '-q', '--hard', 'change', cwd=self.repo)
                git('checkout', '-q', '-B', f'break-{name}', 'change', cwd=self.repo)
                self.commit(files)
                r = self.precheck('--no-tests')
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f'fork-precheck: {name} check FAILED (exit 1):', r.stderr)
                self.assertIn(message, r.stderr)
                self.assertIn(f'FAILED: {name} check (exit 1)', r.stdout)
                for other in set(cases) - {name}:
                    if not (name == 'upstream-files' and other == 'seam'):   # that file is an upstream file too
                        self.assertNotIn(f'{other} check FAILED', r.stderr)
                git('checkout', '-q', 'change', cwd=self.repo)

    def test_the_fork_checks_get_ci_s_words_and_the_default_branch_s_scripts(self):
        def stand_ins():
            for f in FORK_CHECK_FILES[1:]:
                write(os.path.join(self.repo, f), NODE_LOGGER)
            git('commit', '-q', '-am', 'stand-in checks', cwd=self.repo)
        mb = git('rev-parse', 'HEAD~1', cwd=self.repo)
        self.on_main(stand_ins)
        r = self.precheck('--no-tests')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        head, main = git('rev-parse', 'HEAD', cwd=self.repo), git('rev-parse', 'origin/main', cwd=self.repo)
        self.assertEqual([c[2:] for c in self.calls() if c[0].endswith('.cjs')], [
            [mb, head], [mb, head, UPSTREAM_FILES], ['--config-ref', main, mb, head]])
        self.assertEqual([c[0] for c in self.calls() if c[0].endswith('.cjs')],
                         ['fork-schema-check.cjs', 'fork-upstream-files-check.cjs', 'fork-seam-check.cjs'])
        self.assertTrue(all(c[1] == self.repo for c in self.calls() if c[0].endswith('.cjs')))

    def test_a_missing_check_script_or_workflow_fails_never_passes(self):
        self.on_main(lambda: git('rm', '-q', '.github/scripts/fork-seam-check.cjs', cwd=self.repo) or
                     git('commit', '-q', '-m', 'no seam check', cwd=self.repo))
        r = self.precheck('--no-tests')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('.github/scripts/fork-seam-check.cjs is not in origin/main: the seam check cannot run', r.stderr)
        self.assertIn('FAILED: fork checks (not runnable)', r.stdout)
        self.assertNotIn('OK:', r.stdout)
        self.on_main(lambda: git('rm', '-q', FORK_CHECK_FILES[0], cwd=self.repo) or
                     git('commit', '-q', '-m', 'no workflow', cwd=self.repo))
        r = self.precheck('--no-tests')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('.github/workflows/fork-checks.yml is not in origin/main', r.stderr)
        r = self.precheck('--no-tests', '--no-fork-checks')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('OK: fmt, lint', r.stdout)
        self.assertNotIn('fork checks', r.stdout)

    def test_no_change_runs_nothing_and_outside_a_checkout_is_exit_2(self):
        git('reset', '-q', '--hard', 'origin/main', cwd=self.repo)
        git('clean', '-q', '-fd', cwd=self.repo)
        r = self.precheck()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('no changed files against origin/main', r.stdout)
        self.assertEqual(self.calls(), [])
        outside = os.path.join(self.tmp, 'plain')
        os.makedirs(outside)
        r = self.run_cmd('fork-precheck', cwd=outside, env={'GIT_CEILING_DIRECTORIES': self.tmp})
        self.assertEqual(r.returncode, 2)
        self.assertIn('not in a git checkout', r.stderr)


# The kit's table: each command lists itself where facts finds it -----------------------------------------------

class KitTable(Base):
    def setUp(self):
        super().setUp()
        self.machine = fx.machine_file(self.tmp, {})   # nothing configured: only the kit's own table
        self.env.pop(fx.KIT_TABLE_VAR, None)

    def facts(self, *args, env=None):
        return fx.run_hook('facts', '', self.machine, env=dict(self.env, **(env or {})), cwd=self.tmp, args=args,
                           hook=os.path.join(fx.BIN, 'facts'))

    def test_every_fork_command_has_a_bin_and_a_row_named_after_it(self):
        with open(os.path.join(KIT, 'sbin', 'agent-guards-install')) as f:
            installer = f.read()
        commands = ('fork-ci-why', 'fork-clone', 'fork-precheck')
        place = facts.read_place(os.path.join(KIT, 'COMMANDS.md'), 'kit')
        self.assertEqual(place.notes, [])
        self.assertEqual([r.thing.split(':')[0] for r in place.rows()], list(commands))
        for c in commands:
            self.assertTrue(os.access(os.path.join(KIT, 'bin', c), os.X_OK), c)
            self.assertIn(f"'{c}'", installer.split('COMMANDS = ', 1)[1].split('\n', 1)[0])
            r = self.run_cmd(c, '--help')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(r.stdout.startswith(f'usage: {c}'), r.stdout)

    def test_with_nothing_configured_facts_lists_and_finds_the_commands(self):
        r = self.facts('--index')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(os.path.join(KIT, 'COMMANDS.md') + ' (3 rows):', r.stdout)
        self.assertIn('  - fork-ci-why: why are a PR', r.stdout)
        r = self.facts('clone')
        self.assertIn('| fork-clone: a new clone with its install |', r.stdout)
        self.assertNotIn('note:', r.stdout)
        r = self.facts('--dirs')
        self.assertIn('(kit)', r.stdout)

    def test_the_session_start_context_names_them(self):
        r = fx.run_hook('facts-hook', {'hook_event_name': 'SessionStart', 'session_id': 's', 'cwd': self.tmp},
                        self.machine, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = json.loads(r.stdout)['hookSpecificOutput']['additionalContext']
        for c in ('fork-ci-why', 'fork-clone', 'fork-precheck'):
            self.assertIn(f'  - {c}:', text)

    def test_check_skips_it_and_facts_dirs_or_an_older_kit_without_it_leave_it_out(self):
        r = self.facts('--check')
        self.assertEqual((r.returncode, r.stdout.strip()), (0, 'facts check: OK (0 table file(s), 0 rows)'))
        own = write(os.path.join(self.tmp, 'own', 'STATE.md'), '| Thing | Updated |\n|-|-|\n| mine | 2026-11-20 |\n')
        r = self.facts('--index', env={'FACTS_DIRS': own})
        self.assertNotIn('fork-clone', r.stdout)
        r = self.facts('--index', env={fx.KIT_TABLE_VAR: fx.NO_KIT_TABLE})
        self.assertNotIn('fork-clone', r.stdout)
        self.assertNotIn('note:', r.stdout)


if __name__ == '__main__':
    unittest.main()
