"""Ready-made commands agents would otherwise rebuild as long pipelines, each costing several turns (COMMANDS.md):
fork-ci-why (why a pull request's required checks fail), fork-clone (a blob-less clone with its dependencies),
fork-precheck (format, lint and test what a change touches). Python 3 standard library only; each command's
`main(argv)` returns its exit code. Tests: fork-kit/agent-guards/tests/kitcmds.test.py."""
import fcntl, filecmp, json, os, re, shutil, subprocess, sys, time

from agent_guards import facts

GH = 'gh'        # tests point these at stand-ins
GIT = 'git'


def _run(cmd, cwd=None, timeout=None, capture=True):
    """(exit code, stdout, stderr); a command that is missing or does not finish in time is exit 127 or 124."""
    try:
        r = subprocess.run(cmd, cwd=cwd, timeout=timeout, text=True, errors='replace',
                           stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None)
        return r.returncode, r.stdout or '', r.stderr or ''
    except FileNotFoundError:
        return 127, '', f'{cmd[0]}: not found'
    except subprocess.TimeoutExpired:
        return 124, '', f'{cmd[0]}: no answer within {timeout} s'


def _say(prog, text, err=False):
    print(f'{prog}: {text}', file=sys.stderr if err else sys.stdout, flush=True)


# fork-ci-why ----------------------------------------------------------------------------------------------------

ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07')
STAMP = re.compile(r'^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?')
# What counts as an error line in a job log: GitHub's own error annotations, and the words test runners, linters,
# compilers and shells print for a failure. Group markers, lines that only say "0 errors" and passing tests' lines
# (✔ ... error) are not.
ERROR = re.compile(r'##\[error\]|\b(?:error|ERROR|Error|FAIL(?:ED|URE)?|Failed|failed|fatal|Traceback|panicked)\b'
                   r'|AssertionError|✖|✗|×|\bnot ok\b|exit (?:code|status) [1-9]')
NOT_ERROR = re.compile(r'\b0 (?:errors?|failed|failures?)\b|^##\[(?:group|endgroup)\]|--no-error-on|^\s*[✔✓√]')
JOB_LINK = re.compile(r'^https://[^/]+/([^/]+/[^/]+)/actions/runs/\d+/job/(\d+)')
CI_WHY_USAGE = """usage: fork-ci-why PR [-R OWNER/REPO] [--all] [--lines N]

For each failing required check on the pull request's head: its name, its job link, and only the error lines of
its job log (ANSI colours and timestamps stripped; at most N lines a job, 40 by default). A check that is not an
Actions job (a status or an app's check) is its name and link alone. Required: the base branch's rules and branch
protection; --all: every failing check, required or not (also used, with a note, when no required check can be read). Exit 0: none failing; 1: failing checks
shown; 2: a wrong call or gh failed."""


def clean_log(text):
    return [STAMP.sub('', ANSI.sub('', line)).rstrip() for line in text.splitlines()]


def error_lines(lines, limit):
    """(the error lines, at most `limit`, consecutive repeats once; how many more there were)."""
    found = []
    for line in lines:
        if ERROR.search(line) and not NOT_ERROR.search(line):
            line = line.replace('##[error]', '').strip()
            if line and (not found or found[-1] != line):
                found.append(line)
    return found[:limit], max(0, len(found) - limit)


FAILED = {'FAILURE', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'STARTUP_FAILURE', 'ERROR'}
PR_URL = re.compile(r'^https://[^/]+/([^/]+/[^/]+)/pull/\d+')


def _check(item):
    """One statusCheckRollup item as {name, workflow, link, bucket}: a check run (name, status, conclusion,
    detailsUrl) or a commit status (context, state, targetUrl)."""
    if item.get('__typename') == 'StatusContext' or 'context' in item:
        state = (item.get('state') or '').upper()
        bucket = 'fail' if state in FAILED else 'pending' if state in ('PENDING', 'EXPECTED', '') else 'pass'
        return {'name': item.get('context') or '?', 'workflow': '', 'link': item.get('targetUrl') or '',
                'bucket': bucket}
    conclusion = (item.get('conclusion') or '').upper()
    if (item.get('status') or '').upper() != 'COMPLETED':
        bucket = 'pending'
    else:
        bucket = ('cancel' if conclusion == 'CANCELLED' else 'fail') if conclusion in FAILED else 'pass'
    return {'name': item.get('name') or '?', 'workflow': item.get('workflowName') or '',
            'link': item.get('detailsUrl') or '', 'bucket': bucket}


def _required(repo, base):
    """The status check names the base branch's rules and branch protection require ([] when none can be read)."""
    names = []
    code, out, _ = _run([GH, 'api', f'repos/{repo}/rules/branches/{base}'], timeout=60)
    try:
        for rule in json.loads(out) if code == 0 else []:
            if rule.get('type') == 'required_status_checks':
                names += [c.get('context') for c in rule.get('parameters', {}).get('required_status_checks', [])]
    except (ValueError, AttributeError, TypeError):
        pass
    code, out, _ = _run([GH, 'api', f'repos/{repo}/branches/{base}/protection/required_status_checks'], timeout=60)
    try:
        names += json.loads(out).get('contexts', []) if code == 0 else []
    except (ValueError, AttributeError):
        pass
    return sorted({n for n in names if n})


def ci_why(argv):
    prog = 'fork-ci-why'
    args, repo, every, limit = list(argv), None, False, 40
    if not args or args[0] in ('-h', '--help'):
        print(CI_WHY_USAGE)
        return 0 if args else 2
    pr = None
    while args:
        a = args.pop(0)
        if a == '-R' and args:
            repo = args.pop(0)
        elif a == '--all':
            every = True
        elif a == '--lines' and args and args[0].isdigit() and int(args[0]) > 0:
            limit = int(args.pop(0))
        elif pr is None and not a.startswith('-'):
            pr = a
        else:
            print(CI_WHY_USAGE, file=sys.stderr)
            return 2
    if pr is None:
        print(CI_WHY_USAGE, file=sys.stderr)
        return 2
    code, out, err = _run([GH, 'pr', 'view', str(pr), '--json', 'url,baseRefName,statusCheckRollup']
                          + (['-R', repo] if repo else []), timeout=60)
    try:
        view = json.loads(out)
        checks = [_check(i) for i in view.get('statusCheckRollup') or []]
        m = PR_URL.match(view.get('url') or '')
        repo = repo or (m[1] if m else None)
    except (ValueError, AttributeError):
        _say(prog, f'cannot read the checks of {pr}: {(err or out).strip()[:300] or f"gh exit {code}"}', err=True)
        return 2
    missing = []
    if not every:
        required = _required(repo, view.get('baseRefName') or '') if repo else []
        if required:
            reported = {c['name'] for c in checks}
            missing = [n for n in required if n not in reported]
            checks = [c for c in checks if c['name'] in required]
        else:
            _say(prog, 'note: no required check can be read for the base branch: every failing check is shown')
            every = True
    failing = [c for c in checks if c['bucket'] in ('fail', 'cancel')]
    pending = sum(1 for c in checks if c['bucket'] == 'pending') + len(missing)
    kind = 'check' if every else 'required check'
    if missing:
        _say(prog, f'not reported yet (required): {", ".join(missing)}')
    if not failing:
        _say(prog, f'no failing {kind} ({len(checks) + len(missing)} {kind}s, {pending} pending)')
        return 0
    for c in failing:
        link = c['link']
        what = 'cancelled' if c['bucket'] == 'cancel' else 'failed'
        flow = f' ({c["workflow"]})' if c['workflow'] else ''
        print(f'{what.upper()} {c["name"]}{flow}: {link or "no link"}')
        m = JOB_LINK.match(link)
        if not m:
            print('  not an Actions job: no log to read here')
            continue
        code, out, err = _run([GH, 'api', f'repos/{m[1]}/actions/jobs/{m[2]}/logs'], timeout=120)
        if code != 0:
            print(f'  the job log cannot be read: {(err or out).strip()[:200]}')
            continue
        lines = clean_log(out)
        found, more = error_lines(lines, limit)
        if not found:
            print('  no error line found; the log\'s last lines:')
            found = [ln for ln in lines if ln.strip()][-15:]
        for line in found:
            print(f'  {line[:400]}')
        if more:
            print(f'  +{more} more error lines (--lines N shows more)')
    _say(prog, f'{len(failing)} failing {kind}(s) of {len(checks) + len(missing)}, {pending} pending')
    return 1


# fork-clone -----------------------------------------------------------------------------------------------------

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
PENDING = 'fork-clone-pending'    # in the clone's .git: written before the install, removed once it finished
LOCK = 'pnpm-lock.yaml'
CLONE_USAGE = """usage: fork-clone NAME [BRANCH] [--repo URL] [--into DIR]

A blob-less clone (file contents download on demand) at DIR/NAME; DIR is $FORK_WORK_DIR, else ~/work. The repository
is URL, else $FORK_CLONE_REPO, else the origin of the checkout you are in. BRANCH: checked out when the remote has it,
else made from the remote's default branch. With a pnpm-lock.yaml: when another clone in DIR has the same lockfile
and its own installed node_modules, the install is offline from the package store those use (seconds); else one
install. Run again: a clone of the same repository at DIR/NAME is fetched and reused (a half-done install is done
again); a folder that is something else is never touched. A clone stopped half-way is made again."""


def _install_cmd():
    return ['vp', 'install', '--frozen-lockfile'] if shutil.which('vp') else ['pnpm', 'install', '--frozen-lockfile']


def same_lock_sibling(work, dest):
    """Another clone in `work` with the same pnpm-lock.yaml and its own installed node_modules, or None."""
    lock = os.path.join(dest, LOCK)
    try:
        names = sorted(os.listdir(work))
    except OSError:
        return None
    for n in names:
        other = os.path.join(work, n)
        if n.startswith('.') or os.path.realpath(other) == os.path.realpath(dest):
            continue
        mods = os.path.join(other, 'node_modules')
        try:
            if (os.path.isfile(os.path.join(other, LOCK)) and not os.path.islink(mods)
                    and os.path.isfile(os.path.join(mods, '.modules.yaml'))
                    and filecmp.cmp(lock, os.path.join(other, LOCK), shallow=False)):
                return other
        except OSError:
            continue
    return None


def _install(prog, work, dest):
    """'skipped', 'offline from <clone>'s store', 'installed' or None (failed, said why)."""
    if not os.path.isfile(os.path.join(dest, LOCK)):
        return 'skipped (no pnpm-lock.yaml)'
    mods = os.path.join(dest, 'node_modules')
    if os.path.islink(mods):
        _say(prog, f'{mods} is a link to {os.readlink(mods)}: an install would write into that clone; remove the '
                   'link and run fork-clone again', err=True)
        return None
    sib, cmd = same_lock_sibling(work, dest), _install_cmd()
    started = time.monotonic()
    if sib:
        code, _, err = _run(cmd + ['--offline'], cwd=dest, capture=False)
        if code == 0:
            return f'offline from the store {os.path.basename(sib)} uses ({time.monotonic() - started:.0f} s)'
        _say(prog, f'note: the offline install failed (exit {code}): installing online')
    code, _, _ = _run(cmd, cwd=dest, capture=False)
    if code != 0:
        _say(prog, f'{" ".join(cmd)} failed (exit {code}): run fork-clone again to retry', err=True)
        return None
    return f'installed ({time.monotonic() - started:.0f} s)'


def _git(dest, *args, timeout=600):
    return _run([GIT, '-C', dest, *args], timeout=timeout)


def _checkout(prog, dest, branch, reused):
    if not branch:
        return True
    code, cur, _ = _git(dest, 'rev-parse', '--abbrev-ref', 'HEAD')
    if code == 0 and cur.strip() == branch:
        return True
    if reused:
        code, status, _ = _git(dest, 'status', '--porcelain')
        if code != 0 or status.strip():
            _say(prog, f'{dest} has uncommitted changes: it stays on {cur.strip()}, not {branch}', err=True)
            return False
    has_local = _git(dest, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}')[0] == 0
    has_remote = _git(dest, 'rev-parse', '--verify', '--quiet', f'refs/remotes/origin/{branch}')[0] == 0
    if has_local or has_remote:
        code, _, err = _git(dest, 'checkout', '-q', branch)
    else:
        code, head, _ = _git(dest, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD')
        base = head.strip() if code == 0 and head.strip() else 'origin/main'
        code, _, err = _git(dest, 'checkout', '-q', '--no-track', '-b', branch, base)
        if code == 0:
            _say(prog, f'new branch {branch} from {base}')
    if code != 0:
        _say(prog, f'cannot check out {branch}: {err.strip()[:300]}', err=True)
        return False
    return True


def clone(argv):
    prog = 'fork-clone'
    args, repo, into, pos = list(argv), None, None, []
    if not args or args[0] in ('-h', '--help'):
        print(CLONE_USAGE)
        return 0 if args else 2
    while args:
        a = args.pop(0)
        if a == '--repo' and args:
            repo = args.pop(0)
        elif a == '--into' and args:
            into = args.pop(0)
        elif not a.startswith('-'):
            pos.append(a)
        else:
            pos = []
            break
    if not 1 <= len(pos) <= 2 or not NAME.match(pos[0]):
        print(CLONE_USAGE, file=sys.stderr)
        return 2
    name, branch = pos[0], (pos[1] if len(pos) > 1 else None)
    repo = repo or os.environ.get('FORK_CLONE_REPO')
    if not repo:
        code, out, _ = _run([GIT, 'remote', 'get-url', 'origin'], timeout=10)
        repo = out.strip() if code == 0 else ''
    if not repo:
        _say(prog, 'no repository: pass --repo URL, set FORK_CLONE_REPO, or run it in a checkout', err=True)
        return 2
    work = os.path.abspath(os.path.expanduser(into or os.environ.get('FORK_WORK_DIR') or '~/work'))
    os.makedirs(work, exist_ok=True)
    dest, partial = os.path.join(work, name), os.path.join(work, f'.{name}.partial')
    lock = open(os.path.join(work, f'.{name}.lock'), 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _say(prog, f'another fork-clone of {name} is running: wait for it', err=True)
        return 2
    reused = os.path.lexists(dest)
    if reused:
        if os.path.isdir(dest) and not os.path.islink(dest) and not os.listdir(dest):
            os.rmdir(dest)     # an empty folder: as if absent
            reused = False
        elif not os.path.isdir(os.path.join(dest, '.git')):
            _say(prog, f'{dest} is there and is not a git clone: left alone (pick another NAME)', err=True)
            return 2
        else:
            code, out, _ = _git(dest, 'remote', 'get-url', 'origin', timeout=10)
            if code != 0 or facts.remote_key(out) != facts.remote_key(repo):
                _say(prog, f'{dest} is a clone of {out.strip() or "no origin"}, not {repo}: left alone', err=True)
                return 2
    if reused:
        code, _, err = _git(dest, 'fetch', '-q', 'origin')
        if code != 0:
            _say(prog, f'note: fetch failed ({err.strip()[:200]}): the clone is reused as it is')
        _say(prog, f'reusing {dest}' + (' (its last install did not finish: installing again)'
                                         if os.path.exists(os.path.join(dest, '.git', PENDING)) else ''))
    else:
        if os.path.lexists(partial):   # a clone stopped half-way (the lock says none is running): made again
            _say(prog, f'note: removing {partial}, left by a clone stopped half-way')
            shutil.rmtree(partial, ignore_errors=True)
        code, _, err = _run([GIT, 'clone', '-q', '--filter=blob:none', repo, partial], timeout=1800)
        if code != 0:
            shutil.rmtree(partial, ignore_errors=True)
            _say(prog, f'git clone failed: {err.strip()[:400]}', err=True)
            return 1
        open(os.path.join(partial, '.git', PENDING), 'w').close()
        os.rename(partial, dest)
    if not _checkout(prog, dest, branch, reused):
        return 1
    pending = os.path.join(dest, '.git', PENDING)
    mods = os.path.join(dest, 'node_modules')
    if reused and not os.path.exists(pending) and (os.path.isdir(mods) or not os.path.isfile(
            os.path.join(dest, LOCK))):
        how = 'kept (already installed)' if os.path.isdir(mods) else 'skipped (no pnpm-lock.yaml)'
    else:
        open(pending, 'w').close()
        how = _install(prog, work, dest)
        if how is None:
            return 1
        os.unlink(pending)
    _, head, _ = _git(dest, 'log', '-1', '--format=%h %s')
    _, cur, _ = _git(dest, 'rev-parse', '--abbrev-ref', 'HEAD')
    print(f'{dest}\n  branch {cur.strip()}, at {head.strip()[:100]}\n  dependencies: {how}')
    return 0


# fork-precheck --------------------------------------------------------------------------------------------------

TEST_FILE = re.compile(r'\.test\.(ts|tsx|mts|cts|js|jsx|mjs|cjs|py|sh)$')
NODE_TEST_DIRS = ('.github/', 'fork-ci/', 'fork-kit/')   # node --test there (the fork's runner); vitest elsewhere
SOURCE_EXT = ('.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs', '.py', '.sh')
PRECHECK_USAGE = """usage: fork-precheck [--base REF] [--no-tests] [--kind KIND]

In a checkout: the files changed against REF (default the remote's default branch, from its merge-base: commits,
staged, unstaged and new files; deleted ones skipped) are formatted (vp fmt), then linted (vp lint); then the test
files the change touches (changed test files, and each changed source file's own NAME.test.* beside it) run, once
the machine's capacity check (t3-capacity [--kind KIND], where it is installed) says OK. *.test.py: python3 -B;
*.test.sh: bash; *.test.{js,cjs,mjs} under .github/, fork-ci/ or fork-kit/: node --test; other *.test.*: vp test run.
Exit 0: all passed; 1: a step failed (each step still runs); 2: a wrong call or not a checkout; 3: the capacity check
said NOT OK (fmt and lint ran, no test did: run it again later)."""


def changed_files(top, base):
    code, mb, err = _run([GIT, '-C', top, 'merge-base', base, 'HEAD'], timeout=30)
    if code != 0:
        return None, f'no merge-base with {base}: {err.strip()[:200]}'
    files = []
    for cmd in (['diff', '--name-only', '-z', '--diff-filter=ACMR', mb.strip()],
                ['ls-files', '-z', '--others', '--exclude-standard']):
        code, out, err = _run([GIT, '-C', top, *cmd], timeout=60)
        if code != 0:
            return None, err.strip()[:200]
        files += [f for f in out.split('\0') if f]
    seen, out = set(), []
    for f in files:
        if f not in seen and os.path.isfile(os.path.join(top, f)):
            seen.add(f)
            out.append(f)
    return out, None


def touched_tests(top, files):
    tests = []
    for f in files:
        if TEST_FILE.search(f):
            cands = [f]
        elif f.endswith(SOURCE_EXT):
            stem = f.rsplit('.', 1)[0]
            cands = [f'{stem}.test.{e}' for e in ('ts', 'tsx', 'mts', 'js', 'mjs', 'cjs', 'py', 'sh')]
        else:
            cands = []
        for c in cands:
            if c not in tests and os.path.isfile(os.path.join(top, c)):
                tests.append(c)
    return tests


def test_commands(tests):
    vitest, cmds = [], []
    for t in tests:
        ext = TEST_FILE.search(t)[1]
        if ext == 'py':
            cmds.append(['python3', '-B', t])
        elif ext == 'sh':
            cmds.append(['bash', t])
        elif ext in ('js', 'cjs', 'mjs') and t.startswith(NODE_TEST_DIRS):
            cmds.append(['node', '--test', t])
        else:
            vitest.append(t)
    return ([['vp', 'test', 'run', *vitest]] if vitest else []) + cmds


def precheck(argv):
    prog = 'fork-precheck'
    args, base, tests_on, kind = list(argv), None, True, None
    while args:
        a = args.pop(0)
        if a in ('-h', '--help'):
            print(PRECHECK_USAGE)
            return 0
        if a == '--base' and args:
            base = args.pop(0)
        elif a == '--kind' and args:
            kind = args.pop(0)
        elif a == '--no-tests':
            tests_on = False
        else:
            print(PRECHECK_USAGE, file=sys.stderr)
            return 2
    code, top, _ = _run([GIT, 'rev-parse', '--show-toplevel'], timeout=10)
    if code != 0:
        _say(prog, 'not in a git checkout', err=True)
        return 2
    top = top.strip()
    if not base:
        code, head, _ = _run([GIT, '-C', top, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD'], timeout=10)
        base = head.strip() if code == 0 and head.strip() else 'origin/main'
    files, problem = changed_files(top, base)
    if files is None:
        _say(prog, problem, err=True)
        return 2
    if not files:
        _say(prog, f'no changed files against {base}: nothing to check')
        return 0
    _say(prog, f'{len(files)} changed file(s) against {base}')
    paths = [f if not f.startswith('-') else f'./{f}' for f in files]
    failed = []
    for step in (['vp', 'fmt', '--no-error-on-unmatched-pattern'], ['vp', 'lint', '--no-error-on-unmatched-pattern']):
        _say(prog, ' '.join(step[:2]))
        code, _, _ = _run(step + paths, cwd=top, capture=False)
        if code != 0:
            failed.append(f'{step[1]} (exit {code})')
    tests = touched_tests(top, files) if tests_on else []
    if tests:
        gate = shutil.which('t3-capacity')
        if gate:
            code, out, err = _run([gate] + (['--kind', kind] if kind else []), timeout=30)
            last = (out.strip().splitlines() or [err.strip() or f'exit {code}'])[-1]
            if code != 0:
                _say(prog, f'capacity check: {last}: the {len(tests)} test file(s) did not run; run fork-precheck '
                           'again later' + (f'; already failed: {", ".join(failed)}' if failed else ''))
                return 3
            _say(prog, f'capacity check: {last}')
        else:
            _say(prog, 'note: no t3-capacity on this machine: the tests run without a capacity check')
        for cmd in test_commands(tests):
            _say(prog, ' '.join(cmd))
            code, _, _ = _run(cmd, cwd=top, capture=False)
            if code != 0:
                failed.append(f'{" ".join(cmd)} (exit {code})')
    elif tests_on:
        _say(prog, 'no test file is touched by the change')
    if failed:
        _say(prog, 'FAILED: ' + '; '.join(failed))
        return 1
    _say(prog, f'OK: fmt, lint' + (f' and {len(tests)} test file(s)' if tests else ''))
    return 0
