#!/usr/bin/env python3
"""The head check as a gate: head_check.py's rules where nothing of the pull request is checked out, posted as one
check run of the gate's own GitHub App (fork-head-gate.yml; README.md, "The gate").

A pull request can change the workflow its own head check runs in, and that job reports as GitHub Actions, which any
workflow file of the pull request can also report as. The gate runs from the default branch's workflow
(pull_request_target, issue_comment, workflow_dispatch), with code from the fork's pinned release, reads the pull
request only as data, and posts with a key that lives only in an Environment that only the default branch may use. A
ruleset that binds the required check to the gate App's id then counts this check run and nothing else.

One subcommand per workflow step. Every one reads GitHub with gh and the job's own token, except post, which gets the
App token and makes the one write:
  guard  refuse unless the key can only be the Environment's: no repository or organisation secret of its name
         (REPO_SECRET_PRESENT=true|false: the workflow passes only whether one is set), the Environment exists, and
         its deployment branches are exactly one rule, the default branch. --environment-only: the Environment part
         alone, for the gate job right before the token is made (a re-run can come long after the guard job).
  prs    the pull requests to judge, as a JSON list for the matrix (GITHUB_OUTPUT prs=...): the event's one (none when
         it is no open pull request) or, for a dispatch without one, every open pull request (the backfill, paged).
  judge  read the head and the base tip from the API, fetch both by sha into a new bare repository, run head_check's
         rules in bare mode (head_check.decide, the same code as the action) and write the verdict for post. A
         refusal is a failed verdict. GITHUB_OUTPUT: post=true|false, sha, base.
  post   one API call: a completed check run with the verdict, on the head that was judged. No retry: an error is a
         red job, and the next event or dispatch posts again.
  drift  warn (never fail) when the base branch's rules do not bind the required check to the gate App.
Exit codes: 0 done (judge: a verdict is written, or there is nothing to judge), 1 refused or failed, 2 bad arguments.
"""
import argparse, json, os, re, subprocess, sys, tempfile, time, urllib.parse

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import head_check  # noqa: E402
import pr_queue  # noqa: E402  (../pr-queue, put on the path by head_check)

EVENTS = ('pull_request_target', 'issue_comment', 'workflow_dispatch')
MAX_PRS = 256   # a matrix runs at most 256 jobs
TITLE_MAX, SUMMARY_MAX = 200, 60000   # GitHub takes a summary of up to 65535 characters
CONCLUSIONS = ('success', 'failure')
NUMBER = re.compile(r'[1-9][0-9]{0,9}')
BRANCH_PATTERN = re.compile(r'[][*?]')   # a rule with one of these is a pattern, not one branch
SERVER = 'https://github.com'


def gh_run(*a, stdin=None):
    return subprocess.run(['gh', *a], input=stdin, text=True, capture_output=True)


def said(r):
    return (r.stderr or r.stdout or '').strip()[-300:] or f'gh exited {r.returncode}'


def api(gh, path):
    """(data, None, False), or (None, why, missing): missing when GitHub said 404."""
    r = gh('api', path)
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
    """0 when the key can only come from the Environment (see the module's doc), else 1 with every step to fix it."""
    settings = f'Settings > Environments > {environment} > Deployment branches and tags'
    if not environment_only:
        if secret_present == 'true':
            return refuse(out, env, f'A repository or organisation secret named {secret_name} exists, so the gate does '
                          'not make a token.',
                          'A workflow file on any pushed branch can read a repository or organisation secret, and a job '
                          f'in the Environment {environment} falls back to it when the Environment has none.',
                          f'Delete it (Settings > Secrets and variables > Actions) and keep the key only as a secret of '
                          f'the Environment {environment}.')
        if secret_present != 'false':
            return refuse(out, env, f'The run could not tell whether a repository secret {secret_name} exists '
                          f'(REPO_SECRET_PRESENT={secret_present!r}).',
                          f"This is a fault in the workflow: its guard job must pass `secrets.{secret_name} != ''`, "
                          'and the caller must pass `secrets: inherit`.')
    repository, why, _ = api(gh, f'repos/{repo}')
    branch = repository.get('default_branch') if isinstance(repository, dict) else None
    if not isinstance(branch, str) or not branch:
        return refuse(out, env, f'Could not read the default branch of {repo}: {why or "no default_branch"}')
    one_rule = f'pick "Selected branches and tags" and add one rule: branch {branch} (nothing else)'
    found, why, missing = api(gh, f'repos/{repo}/environments/{urllib.parse.quote(environment, safe="")}')
    if missing:
        return refuse(out, env, f'The Environment {environment} does not exist, so the gate does not make a token.',
                      f'Create it under Settings > Environments, named {environment}; under Deployment branches and '
                      f'tags {one_rule}. Then add the variable HEAD_GATE_APP_ID and the secret {secret_name} there.')
    if not isinstance(found, dict):
        return refuse(out, env, f'Could not read the Environment {environment}: {why}')
    policy = found.get('deployment_branch_policy')
    if policy is None:
        return refuse(out, env, f'Any branch may deploy to the Environment {environment}, so a workflow file on any '
                      f'pushed branch could get its key; the gate does not make a token.', f'Under {settings} {one_rule}.')
    if not isinstance(policy, dict) or policy.get('protected_branches') is not False \
            or policy.get('custom_branch_policies') is not True:
        return refuse(out, env, f'The Environment {environment} allows "Protected branches only" or a rule this guard '
                      f'does not know, which the API cannot prove means only {branch}; the gate does not make a token.',
                      f'Under {settings} {one_rule}.')
    rules, why, _ = api(gh, f'repos/{repo}/environments/{urllib.parse.quote(environment, safe="")}'
                            '/deployment-branch-policies?per_page=100')
    if not isinstance(rules, dict) or not isinstance(rules.get('branch_policies'), list):
        return refuse(out, env, f'Could not read the deployment branch rules of the Environment {environment}: '
                      f'{why or "no branch_policies"}')
    listed = [f'{p.get("type") or "branch"} {p.get("name")}' for p in rules['branch_policies'] if isinstance(p, dict)]
    if rules.get('total_count') != 1 or listed != [f'branch {branch}'] or BRANCH_PATTERN.search(branch):
        return refuse(out, env, f'The Environment {environment} must let only the branch {branch} deploy; its rules now: '
                      f'{"; ".join(listed) or "(none)"} ({rules.get("total_count")} in all).',
                      f'Under {settings} keep exactly one rule, branch {branch}, and delete the others (a tag rule or '
                      'a pattern lets other refs deploy too).')
    out(f'Only {branch} may deploy to {environment}' + (' (the repository secret was checked by the guard job).'
                                                        if environment_only else f', and {secret_name} is no repository '
                                                        'or organisation secret.'))
    return 0


# --- prs ------------------------------------------------------------------------------------------------------------
def open_pr(repo, n, gh):
    """(True, None) for an open pull request, (False, why) for anything else GitHub answers, (None, why) when it
    cannot be read."""
    pr, why, missing = api(gh, f'repos/{repo}/pulls/{n}')
    if missing:
        return False, f'#{n} is no pull request'
    if not isinstance(pr, dict):
        return None, why or 'not a pull request object'
    if pr.get('state') != 'open':
        return False, f'#{n} is {pr.get("state")}'
    return True, None


def prs(repo, event, pr_text, gh, out, env):
    pr_text = (pr_text or '').strip()
    if event not in EVENTS:
        return refuse(out, env, f'refused on a {event or "unnamed"} event: the gate runs only on {", ".join(EVENTS)}')
    if pr_text:
        if not NUMBER.fullmatch(pr_text):
            return refuse(out, env, f'{pr_text!r} is not a pull request number')
        ok, why = open_pr(repo, pr_text, gh)
        if ok is None:
            return refuse(out, env, f'could not read #{pr_text} from GitHub: {why}')
        found = [int(pr_text)] if ok else []
        if not ok:
            out(f'nothing to judge: {why}')
    elif event != 'workflow_dispatch':
        return refuse(out, env, f'a {event} event that names no pull request')
    else:   # the backfill: every open pull request, every page
        r = gh('api', '--paginate', f'repos/{repo}/pulls?state=open&per_page=100', '--jq', '.[].number')
        if r.returncode != 0:
            return refuse(out, env, f'could not list the open pull requests: {said(r)}')
        lines = r.stdout.split()
        if not all(NUMBER.fullmatch(x) for x in lines):
            return refuse(out, env, 'GitHub listed something that is not a pull request number')
        found = sorted({int(x) for x in lines})
        if len(found) > MAX_PRS:
            return refuse(out, env, f'{len(found)} open pull requests: a matrix runs at most {MAX_PRS}. Dispatch the '
                          'gate once per pull request (input pr) instead.')
        out(f'backfill: {len(found)} open pull request(s)')
    set_output(env, prs=json.dumps(found, separators=(',', ':')))
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


def verdict_text(code, ok, reason, rows, head, base, n):
    word = 'pass' if ok else ('refused' if code == head_check.REFUSED else 'fail')
    title = f'{word}: {reason}'
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX - 3] + '...'
    lines = [f'Head check of #{n}: **{word}**', '', head_check.cell(reason), '',
             f'Head {head}, judged against the base tip {base} (the config and the merges are read from there).', '']
    if rows:
        lines += ['| lens | | reason |', '| --- | --- | --- |']
        lines += [f'| {head_check.cell(name)} | {"ok" if good else "fail"} | {head_check.cell(text)} |'
                  for name, good, text in rows]
    text = '\n'.join(lines)
    if len(text) > SUMMARY_MAX:
        text = text[:SUMMARY_MAX - 40] + '\n\n(cut: the run log has every row)'
    return title, text


def judge(a, gh, out, env, decide=head_check.decide, sleep=time.sleep):
    """See the module's doc. 1 (nothing posted, so the merge stays blocked) only when GitHub does not say which head to
    judge; every later problem is a failed verdict on that head."""
    if not pr_queue.REPO_RE.fullmatch(a.repo) or not NUMBER.fullmatch(a.pr):
        return refuse(out, env, f'--repo {a.repo!r} or --pr {a.pr!r} is not owner/name or a pull request number')
    pr, why, missing = api(gh, f'repos/{a.repo}/pulls/{a.pr}')
    if missing or (isinstance(pr, dict) and pr.get('state') != 'open'):
        out(f'#{a.pr} is {"no pull request" if missing else pr.get("state")}: nothing to judge')
        set_output(env, post='false')
        return 0
    head = str(((pr or {}).get('head') or {}).get('sha') or '').lower() if isinstance(pr, dict) else ''
    base_ref = ((pr or {}).get('base') or {}).get('ref') if isinstance(pr, dict) else None
    if not pr_queue.SHA.fullmatch(head):
        return refuse(out, env, f'could not read the head of #{a.pr} from GitHub: {why or "no head sha"}')
    base, why, _ = (None, 'no base branch', False) if not isinstance(base_ref, str) or not base_ref else \
        api(gh, f'repos/{a.repo}/git/ref/heads/{urllib.parse.quote(base_ref, safe="/")}')
    base = str(((base or {}).get('object') or {}).get('sha') or '').lower() if isinstance(base, dict) else ''
    required, problems = head_check.argument_problems(a.repo, a.pr, a.required_lenses)
    try:
        if not pr_queue.SHA.fullmatch(base):
            code, ok, reason, rows = 1, False, f'could not read the tip of the base branch from GitHub: {why}', []
        else:
            path, problem = bare_repository(a.work, f'pr-{a.pr}-{head[:12]}')
            checkout = head_check.Checkout(path or a.work, bare=(base, head), remote=a.remote)
            problem = problem or fetch(checkout, base, head)
            if problem:
                code, ok, reason, rows = 1, False, f'could not fetch the base and the head: {problem}', []
            else:
                problems += filter(None, [pr_queue.git_problem(), checkout.problem()])
                code, ok, reason, rows = decide(a.repo, a.pr, a.config, checkout, required, a.pass_drafts, gh,
                                                sleep, out, problems)
    except Exception as e:   # unknown never passes: the head gets a failed check that says why
        code, ok, reason, rows = 1, False, f'the gate failed: {type(e).__name__}: {e}', []
    head_check.report(ok, reason, rows, a.pr, {k: v for k, v in env.items() if k == 'GITHUB_STEP_SUMMARY'}, out)
    title, text = verdict_text(code, ok, reason, rows, head, base or '(unknown)', a.pr)
    with open(a.verdict, 'w', encoding='utf-8') as f:
        json.dump({'pr': int(a.pr), 'sha': head, 'conclusion': 'success' if ok else 'failure', 'title': title,
                   'summary': text}, f)
    set_output(env, post='true', sha=head, base=base_ref if isinstance(base_ref, str) else '')
    return 0


# --- post -----------------------------------------------------------------------------------------------------------
def post(repo, check_name, verdict_path, gh, out, env):
    """The one write of the gate: a check run that is already completed. Exactly one call; an error is a red job."""
    try:
        with open(verdict_path, encoding='utf-8') as f:
            v = json.load(f)
    except (OSError, ValueError) as e:
        return refuse(out, env, f'no verdict to post: {e}')
    if not (isinstance(v, dict) and pr_queue.SHA.fullmatch(str(v.get('sha'))) and v.get('conclusion') in CONCLUSIONS
            and isinstance(v.get('title'), str) and isinstance(v.get('summary'), str)):
        return refuse(out, env, 'the verdict file is not one judge wrote')
    if not check_name or not pr_queue.REPO_RE.fullmatch(repo):
        return refuse(out, env, f'--check-name {check_name!r} is empty or --repo {repo!r} is not owner/name')
    body = {'name': check_name, 'head_sha': v['sha'], 'status': 'completed', 'conclusion': v['conclusion'],
            'output': {'title': v['title'][:TITLE_MAX], 'summary': v['summary'][:SUMMARY_MAX]}}
    run_id, server = env.get('GITHUB_RUN_ID', ''), env.get('GITHUB_SERVER_URL', SERVER)
    if NUMBER.fullmatch(run_id):
        body['details_url'] = f'{server}/{repo}/actions/runs/{run_id}'
    r = gh('api', '-X', 'POST', f'repos/{repo}/check-runs', '--input', '-', stdin=json.dumps(body))
    if r.returncode != 0:
        return refuse(out, env, f'GitHub did not take the check run {check_name!r} on {v["sha"][:12]}: {said(r)}',
                      'Nothing was retried: the next event on the pull request, or a dispatch with its number, '
                      'posts again.')
    out(f'{check_name}: {v["conclusion"]} on {v["sha"]}')
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
def cli(argv=None, env=None, gh=gh_run, out=print, decide=head_check.decide, sleep=time.sleep):
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
    j = sub.add_parser('judge')
    j.add_argument('--repo', required=True)
    j.add_argument('--pr', required=True)
    j.add_argument('--config', required=True)
    j.add_argument('--required-lenses', default='')
    j.add_argument('--pass-drafts', action='store_true')
    j.add_argument('--work', required=True, help='a folder for the bare repository (the runner temp)')
    j.add_argument('--verdict', required=True, help='where the verdict for post is written')
    j.add_argument('--remote', default='', help='where the commits come from (default: GitHub)')
    o = sub.add_parser('post')
    o.add_argument('--repo', required=True)
    o.add_argument('--check-name', required=True)
    o.add_argument('--verdict', required=True)
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
        return prs(a.repo, a.event, a.pr, gh, out, env)
    if a.cmd == 'judge':
        a.remote = a.remote or f'{SERVER}/{a.repo}.git'
        return judge(a, gh, out, env, decide, sleep)
    if a.cmd == 'post':
        return post(a.repo, a.check_name, a.verdict, gh, out, env)
    return drift(a.repo, a.branch, a.check_name, a.app_id, gh, out, env)


if __name__ == '__main__':
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(cli())
