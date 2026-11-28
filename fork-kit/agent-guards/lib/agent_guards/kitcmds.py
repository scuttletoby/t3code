"""Ready-made commands agents would otherwise rebuild as long pipelines, each costing several turns (COMMANDS.md):
fork-ci-why (why a pull request's required checks fail), fork-clone (a blob-less clone with its dependencies),
fork-precheck (format, lint and test what a change touches). Python 3 standard library only; each command's
`main(argv)` returns its exit code. Tests: fork-kit/agent-guards/tests/kitcmds.test.py."""
import fcntl, filecmp, hashlib, json, os, re, shlex, shutil, subprocess, sys, tempfile, time

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
FORK_REPO = 'scuttletoby/t3code'   # the fork: gh is always told the repository (its own pick may be another remote)
CI_WHY_USAGE = """usage: fork-ci-why PR [-R OWNER/REPO] [--all] [--lines N]

The pull request is read from OWNER/REPO, by default the fork (""" + FORK_REPO + """), never from the repository gh
would pick; its URL is the first line printed. For each failing required check on the pull request's head: its
name, its job link, and only the error lines of its job log (ANSI colours and timestamps stripped; at most N lines a
job, 40 by default). A check that is not an Actions job (a status or an app's check) is its name and link alone.
Required: the base branch's rules and branch protection; --all: every failing check, required or not (also used, with
a note, when no required check can be read). Exit 0: none failing; 1: failing checks shown; 2: a wrong call or gh
failed."""


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
    repo = repo or FORK_REPO
    code, out, err = _run([GH, 'pr', 'view', str(pr), '--json', 'url,baseRefName,statusCheckRollup', '-R', repo],
                          timeout=60)
    try:
        view = json.loads(out)
        checks = [_check(i) for i in view.get('statusCheckRollup') or []]
        m = PR_URL.match(view.get('url') or '')
        repo = m[1] if m else repo
    except (ValueError, AttributeError):
        _say(prog, f'cannot read the checks of {pr} in {repo}: {(err or out).strip()[:300] or f"gh exit {code}"}',
             err=True)
        return 2
    _say(prog, f'PR {view.get("url") or f"{pr} in {repo}"}')
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
INSTALLED = 'fork-clone-installed'   # in the clone's .git: the sha256 of the pnpm-lock.yaml last installed
LOCK = 'pnpm-lock.yaml'
CLONE_USAGE = """usage: fork-clone NAME [BRANCH] [--repo URL] [--into DIR]

A blob-less clone (file contents download on demand) at DIR/NAME; DIR is $FORK_WORK_DIR, else ~/work. The repository
is URL, else $FORK_CLONE_REPO, else the origin of the checkout you are in. BRANCH: checked out when the remote has it,
else made from the remote's default branch. With a pnpm-lock.yaml: when another clone in DIR has the same lockfile
and its own installed node_modules, the install is offline from the package store those use (seconds); else one
install. Run again: a clone of the same repository at DIR/NAME is fetched and reused; its checked-out branch is
fast-forwarded to the remote's when it has no uncommitted changes and no commits of its own (else it is left as it
was, with a note); the install runs again when it did not finish or when pnpm-lock.yaml is not the one installed. A
folder that is something else is never touched. A clone stopped half-way is made again."""


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


def _lock_hash(dest):
    try:
        with open(os.path.join(dest, LOCK), 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _fast_forward(prog, dest):
    """A reused clone's checked-out branch, moved to its remote branch when that is only ahead and nothing is
    uncommitted; else left as it was, with a note."""
    code, up, _ = _git(dest, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}')
    if code != 0 or not up.strip():
        return     # no remote branch (a branch made here)
    up = up.strip()
    if _git(dest, 'merge-base', '--is-ancestor', up, 'HEAD')[0] == 0:
        return     # up to date, or only ahead
    _, cur, _ = _git(dest, 'rev-parse', '--abbrev-ref', 'HEAD')
    code, status, _ = _git(dest, 'status', '--porcelain', '--untracked-files=no')
    if code == 0 and not status.strip() and _git(dest, 'merge', '--ff-only', '-q', up)[0] == 0:
        _say(prog, f'{cur.strip()} fast-forwarded to {up}')
        return
    _say(prog, f'note: {cur.strip()} is left as it was, not at {up} (uncommitted changes or commits of its own)')


def _checkout(prog, dest, branch, reused):
    code, cur, _ = _git(dest, 'rev-parse', '--abbrev-ref', 'HEAD')
    if not branch or code == 0 and cur.strip() == branch:
        if reused:
            _fast_forward(prog, dest)
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
    if reused and has_local:
        _fast_forward(prog, dest)
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
    pending, record = os.path.join(dest, '.git', PENDING), os.path.join(dest, '.git', INSTALLED)
    mods, lock_hash = os.path.join(dest, 'node_modules'), _lock_hash(dest)
    try:
        with open(record) as f:
            installed = f.read().strip()
    except OSError:
        installed = None
    if reused and not os.path.exists(pending) and (lock_hash is None or os.path.isdir(mods) and
                                                   installed == lock_hash):
        how = 'kept (already installed)' if lock_hash else 'skipped (no pnpm-lock.yaml)'
    else:
        if reused and not os.path.exists(pending) and os.path.isdir(mods):
            _say(prog, f'{LOCK} is not the one installed: installing again')
        open(pending, 'w').close()
        how = _install(prog, work, dest)
        if how is None:
            return 1
        if lock_hash:
            with open(record, 'w') as f:
                f.write(lock_hash + '\n')
        os.unlink(pending)
    _, head, _ = _git(dest, 'log', '-1', '--format=%h %s')
    _, cur, _ = _git(dest, 'rev-parse', '--abbrev-ref', 'HEAD')
    print(f'{dest}\n  branch {cur.strip()}, at {head.strip()[:100]}\n  dependencies: {how}')
    return 0


# fork-precheck --------------------------------------------------------------------------------------------------

TEST_FILE = re.compile(r'\.test\.(ts|tsx|mts|cts|js|jsx|mjs|cjs|py|sh)$')
NODE_TEST_DIRS = ('.github/', 'fork-ci/', 'fork-kit/')   # node --test there (the fork's runner); vitest elsewhere
SOURCE_EXT = ('.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs', '.py', '.sh')
PRECHECK_USAGE = """usage: fork-precheck [--base REF] [--no-tests] [--no-fork-checks] [--kind KIND]
       fork-precheck --push [--base REF]

In a checkout: the files changed against REF (default the remote's default branch, fetched first; the base's commit
and age are printed), from its merge-base: commits, staged, unstaged and new files; deleted ones skipped) are
formatted (vp fmt), then linted (vp lint). Then the fork checks behind most "Fork checks" reds run as that workflow
runs them (.github/workflows/fork-checks.yml at REF, with REF's scripts): the schema check, the upstream-files check
and the seam check, on the commits from the merge-base to HEAD (uncommitted changes are not in them); only a failing
check's lines are printed. Then the test files the change touches (changed test files, and each changed source file's
own NAME.test.* beside it, else in the nearest tests/ folder: its own folder's, then each one above) run, once the
machine's capacity check (t3-capacity [--kind KIND], where it is installed) says OK. *.test.py: python3 -B;
*.test.sh: bash;
*.test.{js,cjs,mjs} under .github/, fork-ci/ or fork-kit/: node --test; other *.test.*: vp test run.
--no-fork-checks skips the fork checks (a repository without them; a missing check otherwise fails).
Exit 0: all passed; 1: a step failed (each step still runs; also when the capacity check then said NOT OK); 2: a
wrong call, not a checkout, or the capacity check could not answer (any exit but 0 or 1, as a t3-capacity that takes
no --kind gives) when nothing had failed; 3: the capacity check said NOT OK and nothing had failed (fmt, lint and
the fork checks ran, no test did: run it again later).
--push: the step before every push (docs/fork/agent-rules.md), the cheap checks CI would otherwise fail: it fetches
REF first when it is a remote branch (origin/main), then checks the committed changes only (what the push sends):
vp fmt --check (it writes nothing), vp lint and the fork checks; no tests. Exit 1: CI would be red, do not push."""

# The fork checks fork-checks.yml runs on every pull request (37 of 40 "Fork checks" reds were one of these): every
# step of REF's workflow whose run: line is `node .github/scripts/fork-NAME[-check].cjs WORDS` (the workflow is the
# one list; `node --test` is not such a line), named NAME. Each runs from REF's script with the words of its step:
# "$NAME" words come from CI's names for the default branch, the base and the head, or from the workflow's own env
# lines (UPSTREAM_FILES).
FORK_CHECK_STEP = re.compile(r'^\s+run: node \.github/scripts/(fork-([\w.-]+?)(?:-check)?\.cjs)((?: .*)?)$', re.M)
FORK_CHECKS_WORKFLOW = '.github/workflows/fork-checks.yml'
FORK_SCRIPTS = '.github/scripts/'
LOCAL_REQUIRE = re.compile(r'require\(\s*["\']\./([^"\']+)["\']\s*\)')
FORK_CHECK_LINES = 40     # a failing check's lines printed at most


def _show(top, ref, path):
    code, out, _ = _run([GIT, '-C', top, 'show', f'{ref}:{path}'], timeout=60)
    return out if code == 0 else None


def fork_check_commands(top, ref, values, tmp):
    """[(name, argv)] to run each fork check as REF's fork-checks.yml does, with REF's scripts copied into tmp;
    or (None, problem). values: CI's $NAME words (DEFAULT_SHA, BASE_SHA, HEAD_SHA)."""
    wf = _show(top, ref, FORK_CHECKS_WORKFLOW)
    if wf is None:
        return None, f'{FORK_CHECKS_WORKFLOW} is not in {ref}: the fork checks cannot run (--no-fork-checks skips them)'
    env = dict(values)
    for m in re.finditer(r'^\s+([A-Z_]+): (?![$][{])(.+?)\s*$', wf, re.M):
        env.setdefault(m[1], m[2])
    cmds = []
    steps = FORK_CHECK_STEP.findall(wf)
    if not steps:
        return None, f'{FORK_CHECKS_WORKFLOW} in {ref} runs no node {FORK_SCRIPTS}fork-*.cjs check: none can run as CI does'
    for script, name, rest in steps:
        try:
            words = shlex.split(rest)
        except ValueError as e:
            return None, f'{FORK_CHECKS_WORKFLOW} in {ref}: the {script} step cannot be read: {e}'
        argv = []
        for w in words:
            if '$' in w:
                var = re.fullmatch(r'[$]\{?([A-Z_]+)\}?', w)
                if not var or var[1] not in env:
                    return None, f'{FORK_CHECKS_WORKFLOW} in {ref}: the {script} step uses {w}, unknown here'
                w = env[var[1]]
            argv.append(w)
        todo = [script]
        while todo:   # the script and the scripts it requires beside it, as REF has them
            f = todo.pop()
            dest = os.path.join(tmp, FORK_SCRIPTS, f)
            if os.path.exists(dest):
                continue
            text = _show(top, ref, FORK_SCRIPTS + f)
            if text is None:
                return None, f'{FORK_SCRIPTS}{f} is not in {ref}: the {name} check cannot run'
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'w') as fh:
                fh.write(text)
            todo += [r if r.endswith(('.cjs', '.js', '.json')) else r + '.cjs' for r in LOCAL_REQUIRE.findall(text)]
        cmds.append((name, ['node', os.path.join(tmp, FORK_SCRIPTS, script), *argv]))
    return cmds, None


def fork_checks(prog, top, base):
    """Runs the fork checks; returns the failed steps' names (printing only their lines)."""
    code, default, err = _run([GIT, '-C', top, 'rev-parse', '--verify', '--quiet', f'{base}^{{commit}}'], timeout=30)
    code2, mb, err2 = _run([GIT, '-C', top, 'merge-base', base, 'HEAD'], timeout=30)
    code3, head, err3 = _run([GIT, '-C', top, 'rev-parse', 'HEAD'], timeout=30)
    if code or code2 or code3:
        _say(prog, f'fork checks: cannot resolve {base}, its merge-base or HEAD: '
                   f'{(err or err2 or err3).strip()[:200]}', err=True)
        return ['fork checks (no base)']
    values = {'DEFAULT_SHA': default.strip(), 'BASE_SHA': mb.strip(), 'HEAD_SHA': head.strip()}
    tmp = tempfile.mkdtemp(prefix='fork-precheck-')
    try:
        cmds, problem = fork_check_commands(top, base, values, tmp)
        if cmds is None:
            _say(prog, f'fork checks FAILED: {problem}', err=True)
            return ['fork checks (not runnable)']
        failed = []
        for name, cmd in cmds:
            code, out, err = _run(cmd, cwd=top, timeout=900)
            if code == 0:
                continue
            lines = [ln for ln in (err or out).splitlines() if ln.strip()] or [f'exit {code}, no output']
            _say(prog, f'{name} check FAILED (exit {code}):', err=True)
            for ln in lines[:FORK_CHECK_LINES]:
                print(f'  {ln}', file=sys.stderr)
            if len(lines) > FORK_CHECK_LINES:
                print(f'  ... +{len(lines) - FORK_CHECK_LINES} more', file=sys.stderr)
            failed.append(f'{name} check (exit {code})')
        return failed
    finally:
        shutil.rmtree(tmp, True)


def changed_files(top, base, committed=False):
    """The files changed against base's merge-base: commits, staged, unstaged and new files; committed: the commits'
    only (what a push sends). Deleted files are left out."""
    code, mb, err = _run([GIT, '-C', top, 'merge-base', base, 'HEAD'], timeout=30)
    if code != 0:
        return None, f'no merge-base with {base}: {err.strip()[:200]}'
    files = []
    for cmd in ((['diff', '--name-only', '-z', '--diff-filter=ACMR', mb.strip(), 'HEAD'],) if committed else
                (['diff', '--name-only', '-z', '--diff-filter=ACMR', mb.strip()],
                 ['ls-files', '-z', '--others', '--exclude-standard'])):
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
            exts = ('ts', 'tsx', 'mts', 'js', 'mjs', 'cjs', 'py', 'sh')
            folder, name = os.path.split(f.rsplit('.', 1)[0])
            cands = [c for c in (f'{os.path.join(folder, name)}.test.{e}' for e in exts)
                     if os.path.isfile(os.path.join(top, c))]
            up = folder
            while not cands:    # else the nearest tests/ folder: its own folder's, then each one above (the kit's)
                cands = [c for c in (os.path.join(up, 'tests', f'{name}.test.{e}') for e in exts)
                         if os.path.isfile(os.path.join(top, c))]
                if not up:
                    break
                up = os.path.dirname(up)
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


def fetch_base(prog, top, base):
    """The default base, and --push's: fetched when it names a remote's branch (origin/main), so the merge-base, the
    fork checks' default branch and their scripts are the remote's now, as CI's are; a fetch that fails is a note
    (the last fetch is used)."""
    code, remotes, _ = _run([GIT, '-C', top, 'remote'], timeout=10)
    remote, _, branch = base.partition('/')
    if code != 0 or not branch or remote not in remotes.split():
        return
    code, _, err = _run([GIT, '-C', top, 'fetch', '-q', '--no-tags', remote,
                         f'+refs/heads/{branch}:refs/remotes/{remote}/{branch}'], timeout=120)
    if code != 0:
        _say(prog, f'note: cannot fetch {base} ({(err.strip().splitlines() or [f"exit {code}"])[-1][:200]}): '
                   'it is as last fetched')


def precheck(argv):
    prog = 'fork-precheck'
    args, base, tests_on, kind, fork_on, push, fetch = list(argv), None, True, None, True, False, False
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
        elif a == '--push':
            push, tests_on = True, False
        elif a == '--no-fork-checks':
            fork_on = False
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
        fetch = True
    if fetch or push:
        fetch_base(prog, top, base)
    code, when, _ = _run([GIT, '-C', top, 'log', '-1', '--format=%h, committed %cr', base, '--'], timeout=10)
    if code == 0 and when.strip():
        _say(prog, f'base {base}: {when.strip()}')
    files, problem = changed_files(top, base, committed=push)
    if files is None:
        _say(prog, problem, err=True)
        return 2
    if not files:
        _say(prog, f'no changed files against {base}: nothing to check')
        return 0
    _say(prog, f'{len(files)} changed file(s) against {base}')
    paths = [f if not f.startswith('-') else f'./{f}' for f in files]
    failed = []
    fmt = ['vp', 'fmt', '--check'] if push else ['vp', 'fmt']
    for label, step in (('fmt check' if push else 'fmt', fmt), ('lint', ['vp', 'lint'])):
        _say(prog, ' '.join(step))
        code, _, _ = _run(step + ['--no-error-on-unmatched-pattern'] + paths, cwd=top, capture=False)
        if code != 0:
            failed.append(f'{label} (exit {code})')
    code, dirty, _ = _run([GIT, '-C', top, 'status', '--porcelain', '--untracked-files=no'], timeout=60)
    if push and code == 0 and dirty.strip():
        _say(prog, 'note: uncommitted changes are not in the push; fmt and lint read the working tree, '
                   'the fork checks read the commits')
    if fork_on:
        _say(prog, 'fork checks (fork-checks.yml\'s node steps; commits to HEAD)')
        if not push and code == 0 and dirty.strip():
            _say(prog, 'note: uncommitted changes are not in the fork checks (they read commits, as CI does)')
        failed += fork_checks(prog, top, base)
    tests = touched_tests(top, files) if tests_on else []
    if tests:
        gate = shutil.which('t3-capacity')
        if gate:
            code, out, err = _run([gate] + (['--kind', kind] if kind else []), timeout=30)
            last = (out.strip().splitlines() or err.strip().splitlines() or [f'exit {code}'])[-1]
            if code not in (0, 1):     # not an answer: a wrong call (exit 2), or no answer in time (124)
                _say(prog, f'capacity check failed (exit {code}): {last[:200]}: the {len(tests)} test file(s) did '
                           'not run' + (f' (exit 2 is a wrong call: this {gate} likely does not take --kind; run '
                                        'fork-precheck without --kind)' if kind and code == 2 else '')
                     + (f'; already failed: {", ".join(failed)}' if failed else ''), err=True)
                if failed:     # a failure already seen wins over the wrong call
                    _say(prog, 'FAILED: ' + '; '.join(failed))
                    return 1
                return 2
            if code == 1 and failed:
                _say(prog, f'capacity check: {last}: the {len(tests)} test file(s) did not run')
                _say(prog, 'FAILED: ' + '; '.join(failed))
                return 1
            if code == 1:
                _say(prog, f'capacity check: {last}: the {len(tests)} test file(s) did not run; run fork-precheck '
                           'again later')
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
    done = ('fmt check' if push else 'fmt') + ', lint' + (', fork checks' if fork_on else '')
    _say(prog, f'OK: {done}' + (f' and {len(tests)} test file(s)' if tests else ''))
    return 0
