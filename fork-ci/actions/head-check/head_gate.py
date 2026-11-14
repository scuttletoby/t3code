#!/usr/bin/env python3
"""The head check as a gate: head_check.py's rules where nothing of the pull request is checked out, posted as a
check run of the gate's own GitHub App (fork-head-gate.yml; README.md, "The gate").

A pull request can change the workflow its own head check runs in, and that job reports as GitHub Actions, which any
workflow file of the pull request can also report as. The gate runs from the default branch's workflow
(pull_request_target, issue_comment, workflow_dispatch), with code from the fork's pinned release, reads the pull
request only as data, and posts with a key that lives only in an Environment that only the default branch may use. A
ruleset that binds the required check to the gate App's id then counts this check run and nothing else.

A check run belongs to a commit, not to a pull request: the newest one of the name on a head counts for every pull
request with that head. So the gate judges a head, never one pull request alone: every open, ready pull request into
the default branch whose head it is must pass (the strictest verdict wins), and a draft or a pull request into any
other branch is never judged: a head only such pull requests have gets a failure. Runs for one head wait for each other
(concurrency per head and check name), and each one reads every such pull request again.

One subcommand per workflow step. Every one reads GitHub with gh and the job's own token, except start and finish,
which get the App token and make the writes. Every read tries again on a 5xx or a rate limit (READ_TRIES in all).
Every way a run can end leaves the head "not passed" unless it judged and passed: a run that cannot judge posts a
failure. A run posts nothing only when it cannot post at all (README.md, "Stops before start", lists every case):
GitHub does not take the first write (start), no token can be made (no key, no App, no Environment), the guard job
cannot read the Environment (it may not exist; when REST says 5xx or a rate limit every try, GraphQL, with its own
rate limit, may still show it exists), the head is unknown (on an event that does not carry it, the pull
request read fails every try and so does git's refs/pull/N/head), the backfill cannot list the open pull requests, a
step before start fails, or the run never reaches start.
  guard   GITHUB_OUTPUT guard=ok when the key can only be the Environment's: no repository or organisation secret of
          its name (REPO_SECRET_PRESENT=true|false: the workflow passes only whether one is set), the Environment
          exists, and its deployment branches are exactly one rule, the default branch. Else exit 1 and guard=fail
          (the gate posts only failures: a failure never passes, whoever could reach the key), or no output when the
          Environment does not exist or was not seen to exist (REST, then GraphQL after a 5xx or a rate limit; a
          job that named a missing one would create it, open to every branch).
          --environment-only: the Environment part alone, for the gate job right before the token is made.
  prs     the heads to judge, as a JSON list of {pr, sha} for the matrix (GITHUB_OUTPUT prs=...): the head of the
          event's pull request unless it is merged (a draft's, another base's or a closed one's too: each gets a
          failure, so a pass it had cannot survive to a later ready, retarget or reopen), or, for a dispatch without
          one, the head of every open pull request (the backfill, paged, one entry per head). When the read fails
          every try: the head the event carries (--event-sha), else refs/pull/N/head by git (not the REST API, so
          not its rate limit).
  target  before any token: post=false only for bad arguments. Else post=true: the head gets a failure (judge writes
          it) when the guard did not pass, on a re-run (run attempt 2 or later), for a caller that is not the default
          branch's current one or does not pin this gate release, for an empty lens list, for a head no open, ready
          pull request into the default branch has, or when GitHub did not say. GITHUB_OUTPUT: post, base.
  start   an in-progress check run on the head (it never passes), so a run that stops or fails from here on leaves
          "not passed", never an older pass. Up to 3 tries. GITHUB_OUTPUT: id.
  judge   read the base tip and the pull requests with the head from the API, fetch both commits by sha into a new
          bare repository, run head_check's rules in bare mode (head_check.decide, the same code as the action) for
          each pull request, and write the strictest verdict for finish. Anything target would fail is a failed verdict.
  finish  complete start's check run with the verdict, or with a failure when there is none (the run stopped or
          failed before it judged). Up to 3 tries; when all fail, the job is red and the check stays in progress.
  drift   warn (never fail) when the base branch's rules do not bind the required check to the gate App.
Exit codes: 0 done (judge: a verdict is written), 1 refused or failed, 2 bad arguments.
"""
import argparse, base64, binascii, json, os, re, subprocess, sys, tempfile, time, urllib.parse

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import head_check  # noqa: E402
import pr_queue  # noqa: E402  (../pr-queue, put on the path by head_check)

EVENTS = ('pull_request_target', 'issue_comment', 'workflow_dispatch')
MAX_PRS = 256   # a matrix runs at most 256 jobs
MAX_SAME_HEAD = 20   # open, ready pull requests one head may have; each is judged
RETRY_SECONDS = head_check.RETRY_SECONDS   # between two tries of a write
READ_TRIES, READ_BACKOFF = 3, (3, 10)   # a read that gets a 5xx or a rate limit: seconds before the 2nd and 3rd try
TRANSIENT = re.compile(r'HTTP (?:5[0-9][0-9]|429)\b|rate limit', re.I)
TITLE_MAX, SUMMARY_MAX = 200, 60000   # GitHub takes a summary of up to 65535 characters
CONCLUSIONS = ('success', 'failure')
NUMBER = re.compile(r'[1-9][0-9]{0,9}')
BRANCH_PATTERN = re.compile(r'[][*?]')   # a rule with one of these is a pattern, not one branch
CALLER_PATH = re.compile(r'\.github/workflows/[A-Za-z0-9._-]+\.ya?ml')
GATE_WORKFLOW = 'fork-head-gate.yml'
GATE_FILES = ('.github/workflows/' + GATE_WORKFLOW, 'fork-ci/actions/head-check', 'fork-ci/actions/pr-queue')
LIST_JQ = '.[] | [.number, .draft, .head.sha, .base.ref] | @json'
SERVER = 'https://github.com'


def gh_run(*a, stdin=None):
    return subprocess.run(['gh', *a], input=stdin, text=True, capture_output=True)


def said(r):
    return (r.stderr or r.stdout or '').strip()[-300:] or f'gh exited {r.returncode}'


def pause(seconds):
    time.sleep(seconds)


def transient(r):
    """A read worth trying again: GitHub said 5xx or 429, or a rate limit (a 403 that says so)."""
    return r.returncode != 0 and bool(TRANSIENT.search(r.stderr or r.stdout or ''))


def read(gh, *args):
    """gh api *args, tried again (READ_TRIES in all, READ_BACKOFF between) while GitHub says 5xx or a rate limit."""
    for attempt in range(READ_TRIES):
        if attempt:
            pause(READ_BACKOFF[min(attempt, len(READ_BACKOFF)) - 1])
        r = gh('api', *args)
        if not transient(r):
            return r
    return r


def api(gh, path):
    """(data, None, False), or (None, why, missing): missing when GitHub said 404."""
    r = read(gh, path)
    if r.returncode != 0:
        return None, said(r), 'HTTP 404' in (r.stderr or '')
    try:
        return json.loads(r.stdout), None, False
    except ValueError:
        return None, f'gh api {path} gave no JSON', False


def set_output(env, **values):
    path = env.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a', encoding='utf-8') as f:
            for k, v in values.items():
                f.write(f'{k}={v}\n')


def summary(env, text):
    path = env.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(text.rstrip('\n') + '\n')


def refuse(out, env, first, *more):
    """The refusal in the log (one ::error:: line, nothing from GitHub can start another command) and the summary."""
    out('::error::head gate: ' + head_check.escape(first))
    for line in more:
        out(head_check.escape(line))
    summary(env, '\n'.join(['### Head gate refused', '', first, *more]))
    return 1


# --- guard ----------------------------------------------------------------------------------------------------------
def guard(repo, environment, secret_name, secret_present, environment_only, gh, out, env):
    """0 and guard=ok when the key can only come from the Environment (see the module's doc). Else 1 with every step
    to fix it, and guard=fail: the gate then posts only failures, which never pass whoever could reach the key. No
    output at all when the Environment does not exist or was not read (guard job only): there may be no key, and a
    job that named a missing Environment would create it, open to every branch."""
    settings = f'Settings > Environments > {environment} > Deployment branches and tags'
    problem = None
    if not environment_only:
        if secret_present == 'true':
            problem = (f'A repository or organisation secret named {secret_name} exists, so the gate judges nothing.',
                       'A workflow file on any pushed branch can read a repository or organisation secret, and a job '
                       f'in the Environment {environment} falls back to it when the Environment has none.',
                       f'Delete it (Settings > Secrets and variables > Actions) and keep the key only as a secret of '
                       f'the Environment {environment}.')
        elif secret_present != 'false':
            problem = (f'The run could not tell whether a repository secret {secret_name} exists '
                       f'(REPO_SECRET_PRESENT={secret_present!r}).',
                       f"This is a fault in the workflow: its guard job must pass `secrets.{secret_name} != ''`, "
                       'and the caller must pass `secrets: inherit`.')
    repository, why, _ = api(gh, f'repos/{repo}')
    branch = repository.get('default_branch') if isinstance(repository, dict) else None
    if not isinstance(branch, str) or not branch:
        branch = None
        problem = problem or (f'Could not read the default branch of {repo}: {why or "no default_branch"}',)
    one_rule = f'pick "Selected branches and tags" and add one rule: branch {branch or "(the default branch)"} ' \
               '(nothing else)'
    found, why, missing = api(gh, f'repos/{repo}/environments/{urllib.parse.quote(environment, safe="")}')
    if missing and not environment_only:   # never name it in the gate job: GitHub would create it, open to all
        return refuse(out, env, f'The Environment {environment} does not exist, so the gate makes no token and posts '
                      'nothing.', f'Create it under Settings > Environments, named {environment}; under Deployment '
                      f'branches and tags {one_rule}. Then add the variable HEAD_GATE_APP_ID and the secret '
                      f'{secret_name} there.')
    if not isinstance(found, dict):
        # Never seen to exist: a gate job that named it could create it, open to all. GraphQL has its own rate limit,
        # so a spent REST limit alone does not stop the gate from posting failures on an Environment that exists.
        if not environment_only and not (transient_why(why) and environment_exists_by_graphql(repo, environment, gh)):
            return refuse(out, env, *(problem or ()), f'Could not read the Environment {environment} ({why}), so '
                          'the gate job does not start and nothing is posted: a job that named an Environment that '
                          'does not exist would make GitHub create it, open to every branch.', 'An older check run on '
                          'the head stays until a later run can read it: post an issue comment on the pull request '
                          'or dispatch the gate again (the job token\'s rate limit resets each hour).')
        problem = problem or (f'Could not read the Environment {environment}: {why}',)
    elif not problem:
        problem = environment_problem(repo, environment, found.get('deployment_branch_policy'), branch, settings,
                                      one_rule, gh)
    if problem:
        set_output(env, guard='fail')
        return refuse(out, env, *problem, 'Until this is fixed the gate posts a failure on every head it runs for.')
    set_output(env, guard='ok')
    out(f'Only {branch} may deploy to {environment}' + (' (the repository secret was checked by the guard job).'
                                                        if environment_only else f', and {secret_name} is no repository '
                                                        'or organisation secret.'))
    return 0


def transient_why(why):
    return bool(why and TRANSIENT.search(why))


ENVIRONMENT_QUERY = ('query($owner: String!, $name: String!, $environment: String!) '
                     '{ repository(owner: $owner, name: $name) { environment(name: $environment) { name } } }')


def environment_exists_by_graphql(repo, environment, gh):
    """True only when GitHub's GraphQL API names the Environment (it exists); anything else is False."""
    owner, _, name = repo.partition('/')
    r = read(gh, 'graphql', '-f', f'query={ENVIRONMENT_QUERY}', '-f', f'owner={owner}', '-f', f'name={name}',
             '-f', f'environment={environment}')
    try:
        found = ((json.loads(r.stdout).get('data') or {}).get('repository') or {}).get('environment') or {}
    except (ValueError, AttributeError, TypeError):
        return False
    return isinstance(found, dict) and found.get('name') == environment


def environment_problem(repo, environment, policy, branch, settings, one_rule, gh):
    """None when only `branch` may deploy to the Environment, else the refusal's lines."""
    if policy is None:
        return (f'Any branch may deploy to the Environment {environment}, so a workflow file on any pushed branch '
                'could get its key; the gate judges nothing.', f'Under {settings} {one_rule}.')
    if not isinstance(policy, dict) or policy.get('protected_branches') is not False \
            or policy.get('custom_branch_policies') is not True:
        return (f'The Environment {environment} allows "Protected branches only" or a rule this guard does not know, '
                f'which the API cannot prove means only {branch}; the gate judges nothing.', f'Under {settings} '
                f'{one_rule}.')
    rules, why, _ = api(gh, f'repos/{repo}/environments/{urllib.parse.quote(environment, safe="")}'
                            '/deployment-branch-policies?per_page=100')
    if not isinstance(rules, dict) or not isinstance(rules.get('branch_policies'), list):
        return (f'Could not read the deployment branch rules of the Environment {environment}: '
                f'{why or "no branch_policies"}',)
    listed = [f'{p.get("type") or "branch"} {p.get("name")}' for p in rules['branch_policies'] if isinstance(p, dict)]
    if rules.get('total_count') != 1 or listed != [f'branch {branch}'] or BRANCH_PATTERN.search(branch):
        return (f'The Environment {environment} must let only the branch {branch} deploy; its rules now: '
                f'{"; ".join(listed) or "(none)"} ({rules.get("total_count")} in all).',
                f'Under {settings} keep exactly one rule, branch {branch}, and delete the others (a tag rule or a '
                'pattern lets other refs deploy too).')
    return None


# --- prs ------------------------------------------------------------------------------------------------------------
def default_branch(repo, gh):
    """(branch, None) or (None, why)."""
    found, why, _ = api(gh, f'repos/{repo}')
    branch = found.get('default_branch') if isinstance(found, dict) else None
    if not isinstance(branch, str) or not branch:
        return None, f'could not read the default branch of {repo}: {why or "no default_branch"}'
    return branch, None


def open_heads(repo, gh, base=None):
    """([(number, draft, head sha, base ref)] of every open pull request, None) or (None, why); with `base`, only
    those into it."""
    query = f'repos/{repo}/pulls?state=open&per_page=100'
    if base is not None:
        query += '&base=' + urllib.parse.quote(base, safe='')
    r = read(gh, '--paginate', query, '--jq', LIST_JQ)
    if r.returncode != 0:
        return None, f'could not list the open pull requests: {said(r)}'
    found = []
    for line in r.stdout.splitlines():
        try:
            n, draft, sha, ref = json.loads(line)
        except (ValueError, TypeError):
            return None, 'GitHub listed something that is not a pull request'
        if not (type(n) is int and n > 0 and isinstance(draft, bool) and isinstance(sha, str)
                and pr_queue.SHA.fullmatch(sha.lower()) and isinstance(ref, str)):
            return None, 'GitHub listed something that is not a pull request'
        found.append((n, draft, sha.lower(), ref))
    return found, None


def pull_head_by_git(repo, number, env):
    """(sha, None) or (None, why): refs/pull/N/head by git ls-remote (the job's token as a header from the
    environment, never on the command line), for when the REST API is down or rate limited. Up to READ_TRIES tries."""
    clean = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'LANG', 'TMPDIR')}
    clean.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    token = env.get('GH_TOKEN', '')
    if token:
        basic = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
        clean.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.extraheader',
                     GIT_CONFIG_VALUE_0=f'AUTHORIZATION: basic {basic}')
    why = ''
    for attempt in range(READ_TRIES):
        if attempt:
            pause(READ_BACKOFF[min(attempt, len(READ_BACKOFF)) - 1])
        try:
            r = subprocess.run(['git', *pr_queue.GIT_SAFE, 'ls-remote', '--', f'{SERVER}/{repo}.git',
                                f'refs/pull/{number}/head'], text=True, capture_output=True, env=clean, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            why = f'git ls-remote: {e}'
            continue
        fields = r.stdout.split()
        if r.returncode == 0 and len(fields) == 2 and pr_queue.SHA.fullmatch(fields[0].lower()) \
                and fields[1] == f'refs/pull/{number}/head':
            return fields[0].lower(), None
        why = f'git ls-remote: {(r.stderr or r.stdout or "no such ref").strip()[-200:]}'
    return None, why


def prs(repo, event, pr_text, gh, out, env, event_sha='', git_head=None):
    """The heads to judge, [{pr, sha}] (see the module's doc). target and judge judge a head as a whole; the pr only
    names the job. When the pull request read fails every try: `event_sha`, the head the event carries
    (pull_request_target), else `git_head(repo, number, env)` (the CLI's: pull_head_by_git)."""
    pr_text, event_sha = (pr_text or '').strip(), (event_sha or '').strip().lower()
    if event not in EVENTS:
        return refuse(out, env, f'refused on a {event or "unnamed"} event: the gate runs only on {", ".join(EVENTS)}')
    if pr_text and not NUMBER.fullmatch(pr_text):
        return refuse(out, env, f'{pr_text!r} is not a pull request number')
    if not pr_text and event != 'workflow_dispatch':
        return refuse(out, env, f'a {event} event that names no pull request')
    if not pr_queue.REPO_RE.fullmatch(repo):
        return refuse(out, env, f'--repo {repo!r} is not owner/name')
    found = []
    if pr_text:
        pr, why, missing = api(gh, f'repos/{repo}/pulls/{pr_text}')
        head = str((pr.get('head') or {}).get('sha') or '').lower() if isinstance(pr, dict) else ''
        if missing:
            out(f'nothing to judge: #{pr_text} is no pull request')
        elif isinstance(pr, dict) and pr.get('merged') is True:
            out(f'nothing to judge: #{pr_text} is merged')
        elif isinstance(pr, dict) and pr_queue.SHA.fullmatch(head):
            found = [{'pr': int(pr_text), 'sha': head}]
            if pr.get('state') != 'open' or pr.get('draft') is not False:
                out(f'#{pr_text} is {"a draft" if pr.get("state") == "open" else pr.get("state")}: its head gets a '
                    'failure unless an open, ready pull request into the default branch has it')
        else:
            sha, how = (event_sha, 'the head the event carries') if pr_queue.SHA.fullmatch(event_sha) else (None, '')
            if sha is None and git_head is not None:
                sha, git_why = git_head(repo, pr_text, env)
                how, why = 'refs/pull/{}/head by git'.format(pr_text), f'{why or "no head sha"}; {git_why}'
            if sha is None:
                return refuse(out, env, f'could not read #{pr_text} from GitHub: {why or "no head sha"}',
                              'Its head is unknown, so nothing was posted: the next event on the pull request, or a '
                              'dispatch with its number, tries again.')
            out(f'::warning::head gate: could not read #{pr_text} ({head_check.escape(why or "no head")}): '
                f'{how}, {sha}, gets judged')
            found = [{'pr': int(pr_text), 'sha': sha}]
    else:   # the backfill: every head of an open pull request, once (drafts and other bases get a failure)
        listed, why = open_heads(repo, gh)
        if listed is None:
            return refuse(out, env, why)
        heads = {}
        for n, _, sha, _ in sorted(listed):
            heads.setdefault(sha, n)
        found = [{'pr': n, 'sha': sha} for sha, n in sorted(heads.items(), key=lambda kv: kv[1])]
        if len(found) > MAX_PRS:
            return refuse(out, env, f'{len(found)} heads to judge: a matrix runs at most {MAX_PRS}. Dispatch the gate '
                          'once per pull request (input pr) instead.')
        out(f'backfill: {len(found)} head(s) of {len(listed)} open pull request(s) (a head no ready pull request into '
            'the default branch has gets a failure)')
    set_output(env, prs=json.dumps(found, separators=(',', ':')))
    return 0


# --- the run and its caller -----------------------------------------------------------------------------------------
def caller_pins(repo, caller_ref, caller_sha, gate_repo, gate_sha, branch, tip, gh):
    """(True, how) when this run's caller is the default branch's current one and pins this gate release; (False, why)
    when it is not; (None, why) when GitHub did not say. A run can start long after its event (it waited for an
    earlier run on the head), and it runs the release its caller named then."""
    m = re.fullmatch(r'([^/@]+/[^/@]+)/(.+)@(.+)', caller_ref or '')
    if not m or m.group(1).lower() != repo.lower() or not CALLER_PATH.fullmatch(m.group(2)):
        return False, f'the caller {caller_ref!r} is no workflow file of {repo}'
    if m.group(3) != f'refs/heads/{branch}':
        return False, f'the caller ran from {m.group(3)}, not from the default branch {branch}'
    if not pr_queue.SHA.fullmatch(caller_sha or '') or not pr_queue.SHA.fullmatch(gate_sha or '') \
            or not pr_queue.REPO_RE.fullmatch(gate_repo or ''):
        return False, 'the run does not say which caller commit or gate release it is'
    path, files = m.group(2), {}
    for name, ref in (('then', caller_sha), ('now', tip)):
        if name == 'now' and tip == caller_sha:
            files['now'] = files['then']
            break
        found, why, missing = api(gh, f'repos/{repo}/contents/{urllib.parse.quote(path)}?ref={ref}')
        if missing:
            return False, f'{path} is not on {"the default branch tip" if name == "now" else "the caller commit"}'
        if not isinstance(found, dict) or not isinstance(found.get('sha'), str):
            return None, f'could not read {path} at {ref[:12]}: {why or "no file"}'
        files[name] = found
    if files['then']['sha'] != files['now']['sha']:
        return False, (f'{path} changed on {branch} since this run started ({caller_sha[:12]} -> {tip[:12]}): a run '
                       'of an older caller')
    if gate_repo.lower() == repo.lower() and gate_sha == caller_sha:
        if caller_sha == tip:
            return True, 'a local gate at the default branch tip'
        return local_gate_same(repo, caller_sha, tip, branch, gh)
    try:
        text = base64.b64decode(files['now'].get('content') or '').decode('utf-8')
    except (binascii.Error, ValueError) as e:
        return None, f'could not read {path}: {e}'
    uses = re.compile(r"""^\s*(?:-\s*)?uses:\s*['"]?""" + re.escape(gate_repo) + r'/\.github/workflows/'
                      + re.escape(GATE_WORKFLOW) + '@' + gate_sha + r"""['"]?\s*(?:#.*)?$""", re.I | re.M)
    if not uses.search(text):
        return False, f'{path} on {branch} no longer pins the gate release {gate_repo}@{gate_sha[:12]}'
    return True, f'{path} on {branch} pins this gate release {gate_sha[:12]}'


def gate_files(repo, ref, gh):
    """({path: git object sha} of the gate's own files at `ref`, None, False), or (None, why, missing)."""
    found = {}
    workflow, why, missing = api(gh, f'repos/{repo}/contents/{GATE_FILES[0]}?ref={ref}')
    if not isinstance(workflow, dict) or not isinstance(workflow.get('sha'), str):
        return None, why or f'no {GATE_FILES[0]}', missing
    found[GATE_FILES[0]] = workflow['sha']
    folder = GATE_FILES[1].rsplit('/', 1)[0]
    listing, why, missing = api(gh, f'repos/{repo}/contents/{folder}?ref={ref}')
    if not isinstance(listing, list):
        return None, why or f'{folder} is no folder', missing
    trees = {f'{folder}/{e.get("name")}': e.get('sha') for e in listing if isinstance(e, dict) and e.get('type') == 'dir'}
    for path in GATE_FILES[1:]:
        if not isinstance(trees.get(path), str):
            return None, f'no {path}', True
        found[path] = trees[path]
    return found, None, False


def local_gate_same(repo, caller_sha, tip, branch, gh):
    """A local gate (the repository calls its own fork-head-gate.yml at the caller commit) counts while its own
    files (the workflow and the toolkit folders it checks out) are the same at the default branch tip: any other
    merge to the default branch changes nothing it runs."""
    files = {}
    for name, ref in (('then', caller_sha), ('now', tip)):
        found, why, missing = gate_files(repo, ref, gh)
        if missing:
            return False, f'a local gate whose files are not all on {ref[:12]} ({why})'
        if found is None:
            return None, f'could not read the gate files at {ref[:12]}: {why}'
        files[name] = found
    changed = [p for p in GATE_FILES if files['then'][p] != files['now'][p]]
    if changed:
        return False, (f'a local gate of {caller_sha[:12]}, and {", ".join(changed)} changed on {branch} since '
                       f'({tip[:12]} now)')
    return True, f'a local gate whose files are the same at the default branch tip {tip[:12]}'


def preflight(a, gh):
    """(state, why, branch, tip, [pr numbers]): everything a run checks before it may post. 'bad' posts nothing (the
    arguments name no head); 'fail' posts a failure (judge writes it: a failure never passes, so a run that may not
    judge, even an old one, may post it); 'go' judges the numbers."""
    if not pr_queue.REPO_RE.fullmatch(a.repo) or not pr_queue.SHA.fullmatch(a.sha):
        return 'bad', f'--repo {a.repo!r} or --sha {a.sha!r} is not owner/name or a commit sha', None, None, []
    if (a.guard, a.environment_guard) != ('ok', 'ok'):
        return 'fail', (f'the guard did not pass (guard job {a.guard or "no answer"}, gate job '
                        f'{a.environment_guard or "no answer"}): see the run\'s Guard steps for the fix'), None, None, []
    if a.run_attempt != '1':
        return 'fail', (f'this is attempt {a.run_attempt!r} of the run. A re-run runs the caller and the gate '
                        'release of the first attempt, whatever the default branch names now, and a re-run of the '
                        'gate job alone skips the guard job: dispatch the gate with the pull request number '
                        'instead (Actions > the caller > Run workflow)'), None, None, []
    required, problems = head_check.required_names(a.required_lenses)
    if problems or not required:
        return 'fail', ('required-lenses names no lens (' + ('; '.join(problems) or 'it is empty') + '): with none, '
                        'a head with no lens comment would pass. Give one lens name per line'), None, None, []
    branch, why = default_branch(a.repo, gh)
    if branch is None:
        return 'fail', why, None, None, []
    ref, why, _ = api(gh, f'repos/{a.repo}/git/ref/heads/{urllib.parse.quote(branch, safe="/")}')
    tip = str(((ref or {}).get('object') or {}).get('sha') or '').lower() if isinstance(ref, dict) else ''
    if not pr_queue.SHA.fullmatch(tip):
        return 'fail', f'could not read the tip of {branch}: {why or "no sha"}', branch, None, []
    pinned, why = caller_pins(a.repo, a.caller_ref, a.caller_sha, a.gate_repo, a.gate_sha, branch, tip, gh)
    if pinned is False:
        return 'fail', (f"not the default branch's current gate: {why}. The next event on the pull request, or a "
                        'dispatch with its number, judges it with the current one'), branch, tip, []
    if pinned is None:
        return 'fail', why, branch, tip, []
    listed, why = open_heads(a.repo, gh, branch)
    if listed is None:
        return 'fail', why, branch, tip, []
    numbers = sorted(n for n, draft, sha, base in listed if sha == a.sha and not draft and base == branch)
    if not numbers:
        return 'fail', (f'no open, ready pull request into {branch} has the head {a.sha[:12]} now (pushed again, '
                        'closed, a draft or into another branch): not passed until one has it'), branch, tip, []
    if len(numbers) > MAX_SAME_HEAD:
        return 'fail', f'{len(numbers)} pull requests have this head; the gate judges at most {MAX_SAME_HEAD}', \
            branch, tip, numbers
    return 'go', None, branch, tip, numbers


# --- target ---------------------------------------------------------------------------------------------------------
def target(a, gh, out, env):
    """Before any token: 1 and post=false (nothing posted) only for bad arguments, else 0 with post=true; base is set
    when the head is judged (the drift warning reads its rules)."""
    state, why, branch, _, numbers = preflight(a, gh)
    if state == 'bad':
        set_output(env, post='false')
        return refuse(out, env, why)
    if state == 'fail':
        out(f'::warning::head gate: {head_check.escape(why)}: the head gets a failed check')
        set_output(env, post='true', base='')
        return 0
    out(f'judging {a.sha}: ' + ', '.join(f'#{n}' for n in numbers) + f' (into {branch})')
    set_output(env, post='true', base=branch)
    return 0


# --- start and finish: the only writes ------------------------------------------------------------------------------
def write(gh, args, body, sleep, tries=3):
    """(answer, None) or (None, why) after `tries` calls. Both writes may repeat: a second in-progress check run never
    passes either, and completing one check run is the same each time."""
    why = ''
    for attempt in range(tries):
        if attempt:
            sleep(RETRY_SECONDS)
        r = gh('api', *args, '--input', '-', stdin=json.dumps(body))
        if r.returncode == 0:
            try:
                return json.loads(r.stdout or '{}'), None
            except ValueError:
                why = 'GitHub gave no JSON'
                continue
        why = said(r)
    return None, why


def start(repo, sha, check_name, gh, out, env, sleep=time.sleep):
    """An in-progress check run on the head, before anything is judged: it never passes, and as the newest of its name
    it stops an older pass from counting, whatever happens to the run from here on. GITHUB_OUTPUT id."""
    if not check_name or not pr_queue.REPO_RE.fullmatch(repo) or not pr_queue.SHA.fullmatch(sha):
        return refuse(out, env, f'--check-name {check_name!r}, --repo {repo!r} or --sha {sha!r} is not usable')
    body = {'name': check_name, 'head_sha': sha, 'status': 'in_progress',
            'output': {'title': 'judging', 'summary': 'The head gate is judging this head. Until it finishes, this '
                       'check is not passed; if it stays like this, the run stopped: see its log.'}}
    run_id, server = env.get('GITHUB_RUN_ID', ''), env.get('GITHUB_SERVER_URL', SERVER)
    if NUMBER.fullmatch(run_id):
        body['details_url'] = f'{server}/{repo}/actions/runs/{run_id}'
    made, why = write(gh, ('-X', 'POST', f'repos/{repo}/check-runs'), body, sleep)
    run = str(made.get('id') or '') if isinstance(made, dict) else ''
    if not NUMBER.fullmatch(run):
        return refuse(out, env, f'GitHub did not take the in-progress check run {check_name!r} on {sha[:12]}: '
                      f'{why or "no id"}', 'Nothing was judged, and nothing on this head changed: the next event on the '
                      'pull request, or a dispatch with its number, tries again.')
    set_output(env, id=run)
    out(f'{check_name}: in progress on {sha} (check run {run})')
    return 0


def finish(repo, sha, run, verdict_path, gh, out, env, sleep=time.sleep):
    """Complete start's check run with judge's verdict, or with a failure when there is none (the run stopped, failed
    or was cancelled before judge wrote one): 1 then, so the job is red too."""
    if not pr_queue.REPO_RE.fullmatch(repo) or not pr_queue.SHA.fullmatch(sha) or not NUMBER.fullmatch(run or ''):
        return refuse(out, env, f'--repo {repo!r}, --sha {sha!r} or --id {run!r} is not usable')
    try:
        with open(verdict_path, encoding='utf-8') as f:
            v = json.load(f)
        why = None if (isinstance(v, dict) and v.get('sha') == sha and v.get('conclusion') in CONCLUSIONS
                       and isinstance(v.get('title'), str) and isinstance(v.get('summary'), str)) \
            else 'the verdict file is not one judge wrote for this head'
    except (OSError, ValueError) as e:
        why = f'no verdict: {e}'
    if why:
        v = {'conclusion': 'failure', 'title': 'refused: the gate stopped before its verdict',
             'summary': f'The head gate stopped before it judged this head ({head_check.cell(why)}). Not passed: the '
                        'next event on the pull request, or a dispatch with its number, judges it again.'}
    body = {'status': 'completed', 'conclusion': v['conclusion'],
            'output': {'title': v['title'][:TITLE_MAX], 'summary': v['summary'][:SUMMARY_MAX]}}
    done, error = write(gh, ('-X', 'PATCH', f'repos/{repo}/check-runs/{run}'), body, sleep)
    if done is None:
        return refuse(out, env, f'GitHub did not complete the check run {run} on {sha[:12]}: {error}',
                      'It stays in progress, which never passes: the next event on the pull request, or a dispatch '
                      'with its number, judges the head again.')
    out(f'check run {run}: {v["conclusion"]} on {sha}')
    if why:
        return refuse(out, env, f'the gate stopped before its verdict ({why}): the head got a failure')
    return 0


# --- judge ----------------------------------------------------------------------------------------------------------
def bare_repository(work, name):
    """(path, None): a new, empty bare repository in `work` with the queue's own config (pr_queue.CACHE_CONFIG: no
    remote, no hook path, no helper), never one an earlier run left; or (None, why it could not be made)."""
    try:
        path = tempfile.mkdtemp(prefix=name + '-', dir=work)
    except OSError as e:
        return None, f'could not make a folder in {work}: {e}'
    r = subprocess.run(['git', *pr_queue.GIT_SAFE, 'init', '-q', '--bare', '--template=', path], text=True,
                       capture_output=True)
    if r.returncode != 0:
        return None, f'git init failed: {r.stderr.strip()[:300]}'
    with open(os.path.join(path, 'config'), 'w', encoding='utf-8') as f:
        f.write(pr_queue.CACHE_CONFIG)
    return path, None


def fetch(checkout, base, head):
    """The base tip and the head, by sha, with their history, into refs only the gate names. None or why not."""
    r = checkout.git('-c', 'gc.auto=0', 'fetch', '-q', '--no-tags', '--no-recurse-submodules', '--no-write-fetch-head',
                     checkout.remote, f'{base}:refs/gate/base', f'{head}:refs/gate/head')
    return None if r.returncode == 0 else f'git fetch failed: {r.stderr.strip()[:300]}'


def verdict_text(ok, reason, results, head, base):
    """(title, summary) of the head's verdict; results is [(n, code, ok, reason, rows)] per pull request."""
    failed = [r for r in results if not r[2]]
    word = 'pass' if ok else ('refused' if failed and all(r[1] == head_check.REFUSED for r in failed) else 'fail')
    title = f'{word}: {reason}'
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX - 3] + '...'
    lines = [f'Head check of {head}: **{word}**', '', head_check.cell(reason), '',
             f'Judged against the base tip {base} (the config and the merges are read from there). A check run counts '
             'for every pull request with its head, so every open, ready pull request into the default branch with '
             'this head must pass.', '']
    for n, _, good, why, rows in results:
        lines += [f'#### #{n}: {"pass" if good else "fail"}', '', head_check.cell(why), '']
        if rows:
            lines += ['| lens | | reason |', '| --- | --- | --- |']
            lines += [f'| {head_check.cell(name)} | {"ok" if g else "fail"} | {head_check.cell(text)} |'
                      for name, g, text in rows]
            lines.append('')
    text = '\n'.join(lines)
    if len(text) > SUMMARY_MAX:
        text = text[:SUMMARY_MAX - 40] + '\n\n(cut: the run log has every row)'
    return title, text


def judge(a, gh, out, env, decide=head_check.decide, sleep=time.sleep):
    """See the module's doc. Always writes a verdict on the head and returns 0; what is not proved is a failure. The
    checks of preflight run again here: they may have changed since target."""
    results, base = [], None
    try:
        state, why, _, base, numbers = preflight(a, gh)
        if state != 'go':
            ok, reason = False, f'not judged: {why}'
        else:
            path, problem = bare_repository(a.work, f'head-{a.sha[:12]}')
            checkout = head_check.Checkout(path or a.work, bare=(base, a.sha), remote=a.remote)
            problem = problem or fetch(checkout, base, a.sha)
            if problem:
                ok, reason = False, f'could not fetch the base and the head: {problem}'
            else:
                shared = list(filter(None, [pr_queue.git_problem(), checkout.problem()]))
                required, _ = head_check.required_names(a.required_lenses)
                for n in numbers:   # every one: the strictest verdict is the head's
                    code, good, why, rows = decide(a.repo, str(n), a.config, checkout, required, False, gh, sleep, out,
                                                   list(shared))
                    results.append((n, code, good, why, rows))
                bad = [(n, why) for n, _, good, why, _ in results if not good]
                ok = bool(results) and not bad
                reason = (f'all {len(results)} pull request(s) with this head pass' if ok
                          else '; '.join(f'#{n}: {why}' for n, why in bad))
    except Exception as e:   # unknown never passes: the head gets a failed check that says why
        ok, reason = False, f'the gate failed: {type(e).__name__}: {e}'
    step = {k: v for k, v in env.items() if k == 'GITHUB_STEP_SUMMARY'}
    for n, _, good, why, rows in results:
        head_check.report(good, why, rows, str(n), step, out)
    if not results:
        out(('' if ok else '::error::head gate: ') + head_check.escape(reason))
    title, text = verdict_text(ok, reason, results, a.sha, base or '(unknown)')
    with open(a.verdict, 'w', encoding='utf-8') as f:
        json.dump({'sha': a.sha, 'prs': [r[0] for r in results], 'conclusion': 'success' if ok else 'failure',
                   'title': title, 'summary': text}, f)
    return 0


# --- drift ----------------------------------------------------------------------------------------------------------
def drift(repo, branch, check_name, app_id, gh, out, env):
    """Warn when no active rule of `branch` requires `check_name` from the gate App alone. Never fails: the check run
    is posted either way, and the warning says what to bind (owner step: bind the check)."""
    def warn(text):
        out('::warning::head gate: ' + head_check.escape(text))
        summary(env, f'> **Head gate:** {text}')
        return 0
    if not NUMBER.fullmatch(app_id or ''):
        return warn(f'the variable HEAD_GATE_APP_ID ({app_id!r}) is no App id, so the binding cannot be checked')
    rules, why, _ = api(gh, f'repos/{repo}/rules/branches/{urllib.parse.quote(branch, safe="/")}?per_page=100')
    if not isinstance(rules, list):
        return warn(f'could not read the rules of {branch}: {why}')
    found = [c for r in rules if isinstance(r, dict) and r.get('type') == 'required_status_checks'
             for c in ((r.get('parameters') or {}).get('required_status_checks') or [])
             if isinstance(c, dict) and c.get('context') == check_name]
    if not found:
        return warn(f'no ruleset of {branch} requires {check_name!r} (classic branch protection cannot be read with '
                    f'this token: there, bind it with app_id {app_id}). Until it is bound to the gate App, a job named '
                    'like it in any workflow can meet it.')
    other = sorted({str(c.get('integration_id')) for c in found if str(c.get('integration_id')) != app_id})
    if other:
        return warn(f'{check_name!r} is required on {branch} from {", ".join(other)} ("None" = any source), not only '
                    f'from the gate App {app_id}: set integration_id {app_id} on it')
    out(f'{check_name!r} on {branch} is bound to the gate App {app_id}')
    return 0


# --- cli ------------------------------------------------------------------------------------------------------------
def run_arguments(p):
    """What target and judge both read about the head and the run."""
    p.add_argument('--repo', required=True)
    p.add_argument('--sha', required=True, help='the head to judge')
    p.add_argument('--required-lenses', required=True, help='lens names, one per line; none = refused')
    p.add_argument('--caller-ref', required=True, help='github.workflow_ref: the caller workflow file and its ref')
    p.add_argument('--caller-sha', required=True, help='github.workflow_sha: the caller commit')
    p.add_argument('--gate-repo', required=True, help='job.workflow_repository: where this gate comes from')
    p.add_argument('--gate-sha', required=True, help='job.workflow_sha: the gate release that runs')
    p.add_argument('--run-attempt', required=True, help='github.run_attempt: only the first attempt may judge')
    p.add_argument('--guard', required=True, help="the guard job's answer: ok, or anything else = only failures")
    p.add_argument('--environment-guard', required=True, help="the gate job's Environment check: ok, or not")


def cli(argv=None, env=None, gh=gh_run, out=print, decide=head_check.decide, sleep=time.sleep,
        git_head=pull_head_by_git):
    env = os.environ if env is None else env
    p = argparse.ArgumentParser(prog='head_gate.py', description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('guard')
    g.add_argument('--repo', required=True)
    g.add_argument('--environment', required=True)
    g.add_argument('--secret-name', default='HEAD_GATE_APP_KEY')
    g.add_argument('--environment-only', action='store_true')
    s = sub.add_parser('prs')
    s.add_argument('--repo', required=True)
    s.add_argument('--event', required=True)
    s.add_argument('--pr', default='')
    s.add_argument('--event-sha', default='', help='the head the event carries, used when the pull request read fails')
    run_arguments(sub.add_parser('target'))
    j = sub.add_parser('judge')
    run_arguments(j)
    j.add_argument('--config', required=True)
    j.add_argument('--work', required=True, help='a folder for the bare repository (the runner temp)')
    j.add_argument('--verdict', required=True, help='where the verdict for finish is written')
    j.add_argument('--remote', default='', help='where the commits come from (default: GitHub)')
    o = sub.add_parser('start')
    o.add_argument('--repo', required=True)
    o.add_argument('--sha', required=True)
    o.add_argument('--check-name', required=True)
    f = sub.add_parser('finish')
    f.add_argument('--repo', required=True)
    f.add_argument('--sha', required=True)
    f.add_argument('--id', required=True, help="start's check run")
    f.add_argument('--verdict', required=True)
    d = sub.add_parser('drift')
    d.add_argument('--repo', required=True)
    d.add_argument('--branch', required=True)
    d.add_argument('--check-name', required=True)
    d.add_argument('--app-id', default='')
    a = p.parse_args(argv)
    if a.cmd == 'guard':
        return guard(a.repo, a.environment, a.secret_name, env.get('REPO_SECRET_PRESENT', ''), a.environment_only, gh,
                     out, env)
    if a.cmd == 'prs':
        return prs(a.repo, a.event, a.pr, gh, out, env, a.event_sha, git_head)
    if a.cmd == 'target':
        return target(a, gh, out, env)
    if a.cmd == 'judge':
        a.remote = a.remote or f'{SERVER}/{a.repo}.git'
        return judge(a, gh, out, env, decide, sleep)
    if a.cmd == 'start':
        return start(a.repo, a.sha, a.check_name, gh, out, env, sleep)
    if a.cmd == 'finish':
        return finish(a.repo, a.sha, a.id, a.verdict, gh, out, env, sleep)
    return drift(a.repo, a.branch, a.check_name, a.app_id, gh, out, env)


if __name__ == '__main__':
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(cli())
