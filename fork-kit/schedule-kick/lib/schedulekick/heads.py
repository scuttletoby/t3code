"""heads: every trusted head gets a run (fork-kit/schedule-kick/README.md, Heads).

GitHub drops the event of a workflow that is switched off at the time, so a pull request head (or the default
branch's tip) can end up with no run at all, and nothing ever tests it. Each tick, for each `heads` entry of the
config, this makes one GraphQL call: the default branch's tip and the open pull requests (the 100 most recently
updated), each head commit with the GitHub Actions check suites on it. A head counts only when it is trusted: the
default branch's tip, or the head of a pull request from a branch of this same repository (a pull request from
another fork is never dispatched). A head that has a run of the entry's workflow (any event, any state: one that
ran is enough here) needs nothing. One without:

1. first seen now: recorded, nothing sent (GitHub's own run may still be on its way);
2. seen less than graceMinutes ago: wait;
3. else one workflow_dispatch on its branch with inputs ref=<the head's commit> and head=true (the workflow's head
   run: it tests the run's own commit, where its checks land), at most maxPerPass a pass, the default branch first,
   then the pull requests updated last first. The head is recorded as sent before the call, so it is sent once.

A sent head whose run does not show within LOST_MINUTES is reported once ("lost", also through the alert channel)
and not sent again: something else is wrong (a dispatch GitHub took and dropped, or a pass stopped between
recording and sending), and a loop would only hide it. A branch whose workflow file refuses the inputs (an older
copy without `head`, or one without workflow_dispatch: PERMANENT) is recorded as refused for that commit and not
sent again (alerted once); its next push brings the new file. Any other 422 is about that one commit too: that head
alone waits (5 minutes, then 10) and is refused after HEAD_TRIES, while the heads after it go on in the same pass.
Any other error is about the whole entry (a 422 for a workflow that is switched off at that moment, #517, a 5xx, no
network, the login): the entry waits (5 minutes, doubling, at most an hour; the count is kept across passes whose
GraphQL read works, and ends only with a pass that has no such error) and tries again; a rate limit pauses the tick
(kick.py), and a REST budget under kick.py's minimum stops the dispatches of the pass. Every attempt counts toward
maxPerPass. Marks of a head missing from one snapshot (moved while GitHub answered, off the first page) are kept
for MARK_DAYS, so it is not sent again when it comes back.

A head that has a run in any state counts as done: a run cancelled by hand, lost with its runner or stuck queued is
never sent again here (fork-ci-rerun and the watch and sweep handle those), and a head run whose fork-checks run is
also missing fails in fork_checks_ran and is not sent again either. Every head gets a run, not a result.
"""
import datetime, re

from .github import ApiError, iso

ACTIONS_APP_ID = 15368          # GitHub Actions' App id on github.com: its check suites are workflow runs
PR_LIMIT = 100                  # the most one GraphQL page holds
SUITE_LIMIT = 50                # check suites read per head commit (a commit has a few: one per workflow run)
LOST_MINUTES = 30
GRACE_MINUTES = 10
MAX_GRACE_MINUTES = 60
PER_PASS = 3
MAX_PER_PASS = 20
MARK_DAYS = 7                   # a sent, lost or refused mark outlives its head's absence from a snapshot this long
HEAD_TRIES = 3                  # a 422 about one commit (not PERMANENT, not ENTRY_WIDE): tries before it is refused
# The dispatch refusals that cannot change for that commit: its workflow file has no `head` input, or no
# workflow_dispatch. Any other refusal (a 422 "Cannot trigger a 'workflow_dispatch' on a disabled workflow" while
# the workflow is switched off, #517) waits and tries again.
PERMANENT = (re.compile(r'\bUnexpected inputs\b', re.I), re.compile(r"does not have 'workflow_dispatch' trigger", re.I))
# The 422 that is about the workflow, not the commit: every head of the entry would get it, so the entry waits.
ENTRY_WIDE = (re.compile(r'\bdisabled workflow\b', re.I),)

QUERY = '''query($owner: String!, $name: String!) {
  rateLimit { remaining resetAt }
  repository(owner: $owner, name: $name) {
    nameWithOwner
    defaultBranchRef { name target { ... on Commit { oid %(suites)s } } }
    pullRequests(states: OPEN, first: %(prs)d, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount
      nodes {
        number isCrossRepository headRefName headRefOid
        headRepository { nameWithOwner }
        commits(last: 1) { nodes { commit { oid %(suites)s } } }
      }
    }
  }
}''' % {'prs': PR_LIMIT, 'suites': f'checkSuites(first: {SUITE_LIMIT}, filterBy: {{appId: {ACTIONS_APP_ID}}}) '
                                    '{ totalCount nodes { workflowRun { workflow { resourcePath } } } }'}


class Head:
    def __init__(self, branch, sha, what, has_run):
        self.branch, self.sha, self.what, self.has_run = branch, sha, what, has_run


def _run_of(commit, workflow_path):
    """True when one of the commit's Actions check suites is a run of the workflow; None when the commit has more
    suites than were read and none of them is (cannot tell: nothing is sent)."""
    suites = commit.get('checkSuites') if isinstance(commit, dict) else None
    if not isinstance(suites, dict) or not isinstance(suites.get('nodes'), list):
        raise ApiError('a head commit without its check suites')
    for node in suites['nodes']:
        run = node.get('workflowRun') if isinstance(node, dict) else None
        workflow = run.get('workflow') if isinstance(run, dict) else None
        path = workflow.get('resourcePath') if isinstance(workflow, dict) else None
        if isinstance(path, str) and path.lower().endswith(workflow_path):
            return True
    total = suites.get('totalCount')
    return False if type(total) is int and total <= len(suites['nodes']) else None


def snapshot(data, repo, workflow):
    """(heads, notes) from one GraphQL answer: the trusted heads, the default branch first, one per commit."""
    r = data.get('repository') if isinstance(data, dict) else None
    if not isinstance(r, dict) or str(r.get('nameWithOwner', '')).lower() != repo.lower():
        raise ApiError(f'GraphQL for {repo}: no such repository in the answer')
    path = f'/{repo}/actions/workflows/{workflow}'.lower()
    heads, notes, seen = [], [], set()

    def add(branch, sha, what, commit):
        if not isinstance(sha, str) or len(sha) != 40 or sha in seen:
            return
        has = _run_of(commit, path)
        if has is None:
            notes.append(f'{what} {sha[:12]}: more check suites than read, none of them a run: not sent')
            return
        seen.add(sha)
        heads.append(Head(branch, sha, what, has))

    default = r.get('defaultBranchRef')
    target = default.get('target') if isinstance(default, dict) else None
    if isinstance(target, dict) and isinstance(default.get('name'), str):
        add(default['name'], target.get('oid'), default['name'], target)
    prs = r.get('pullRequests')
    if not isinstance(prs, dict) or not isinstance(prs.get('nodes'), list):
        raise ApiError(f'GraphQL for {repo}: no pull request list in the answer')
    if type(prs.get('totalCount')) is int and prs['totalCount'] > len(prs['nodes']):
        notes.append(f'{prs["totalCount"]} open pull requests: only the {len(prs["nodes"])} updated last are read')
    for pr in prs['nodes']:
        if not isinstance(pr, dict):
            continue
        head_repo = pr.get('headRepository')
        if pr.get('isCrossRepository') is not False or not isinstance(head_repo, dict) \
                or str(head_repo.get('nameWithOwner', '')).lower() != repo.lower():
            continue                            # another fork's pull request (or one GitHub no longer knows)
        commits = (pr.get('commits') or {}).get('nodes') if isinstance(pr.get('commits'), dict) else None
        commit = (commits[0] or {}).get('commit') if isinstance(commits, list) and len(commits) == 1 else None
        if not isinstance(commit, dict) or commit.get('oid') != pr.get('headRefOid'):
            notes.append(f'#{pr.get("number")}: its head moved while GitHub answered: next pass')
            continue
        if isinstance(pr.get('headRefName'), str) and pr['headRefName']:
            add(pr['headRefName'], pr.get('headRefOid'), f'#{pr.get("number")}', commit)
    return heads, notes


def _bucket(state, key):
    heads = state.setdefault('heads', {})
    old = heads.get(key)
    if not isinstance(old, dict):
        old = {}
    old['seen'] = {k: v for k, v in (old.get('seen') or {}).items() if isinstance(v, str)} \
        if isinstance(old.get('seen'), dict) else {}
    old['sent'] = {k: v for k, v in (old.get('sent') or {}).items() if isinstance(v, dict)} \
        if isinstance(old.get('sent'), dict) else {}
    heads[key] = old
    return old


def fail(state, key, now, message, backoff, most):
    b = _bucket(state, key)
    failures = int(b.get('failures', 0)) + 1 if type(b.get('failures', 0)) is int else 1
    b.update(failures=failures, retryAfter=iso(now + min(backoff * 2 ** min(failures - 1, 10), most)), error=message)


def permanent(e):
    """True for a dispatch refusal that cannot change for that commit (PERMANENT)."""
    return e.status == 422 and any(p.search(str(e)) for p in PERMANENT)


def head_only(e):
    """True for a dispatch refusal about that one commit that may still change (a 422, not ENTRY_WIDE): that head
    waits on its own, the others go on."""
    return e.status == 422 and not e.rate_limited and not any(p.search(str(e)) for p in ENTRY_WIDE)


def reconcile(entry, api, state, now, parse_time, waiting_until, backoff, most, save=lambda: None,
              limited=lambda e: None, alert=lambda text: None, budget_low=lambda: False):
    """(result, message) for one heads entry; state['heads'][key] updated in place. save() writes the state (called
    before each dispatch, so a head is recorded as sent before it is sent); limited(e) on a rate limit (kick.py
    pauses the tick); alert(text) for a head that is lost or refused (once each); budget_low() true stops the
    dispatches of this pass (the REST budget; kick.py then pauses)."""
    b = _bucket(state, entry.key)
    wait = waiting_until(b.get('retryAfter'), now)
    if wait is not None:
        return 'waiting', f'after {b.get("failures", 1)} error(s), next try after {iso(wait)}'
    owner, name = entry.repo.split('/')
    try:
        data = api.graphql(entry.repo, QUERY, {'owner': owner, 'name': name})
        heads, notes = snapshot(data, entry.repo, entry.workflow)
    except ApiError as e:
        fail(state, entry.key, now, str(e), backoff, most)
        if e.rate_limited:
            limited(e)
        return 'error', str(e)
    limit = data.get('rateLimit') if isinstance(data, dict) else None
    if isinstance(limit, dict) and type(limit.get('remaining')) is int:
        # the GraphQL budget, apart from the REST one (api.remaining): kick.py pauses when either runs low
        api.graphql_remaining = limit['remaining']
        reset = parse_time(limit.get('resetAt'))
        api.graphql_reset = reset.timestamp() if reset else None
    current = {h.sha for h in heads}
    b['seen'] = {k: v for k, v in b['seen'].items() if k in current}     # an old head (force push, merge): gone
    keep_after = now - datetime.timedelta(days=MARK_DAYS)
    b['sent'] = {k: v for k, v in b['sent'].items()
                 if k in current or (parse_time(v.get('at')) or keep_after) > keep_after}
    sent, lost, waiting, errors, tried, entry_error = [], [], 0, [], 0, False
    for h in heads:
        if h.has_run:
            b['seen'].pop(h.sha, None)
            continue
        mark = b['sent'].get(h.sha)
        tries = 0
        if mark is not None and mark.get('result') == 'retry':
            # an earlier 422 about this commit alone: its own wait, then the next try (no new grace)
            if waiting_until(mark.get('retryAfter'), now) is not None or tried >= entry.per_pass or budget_low():
                waiting += 1
                continue
            tries = mark['tries'] if type(mark.get('tries')) is int and mark['tries'] > 0 else 1
        elif mark is not None:
            at = parse_time(mark.get('at'))
            if mark.get('result') == 'dispatched' and at is not None \
                    and now - at >= datetime.timedelta(minutes=LOST_MINUTES):
                mark['result'] = 'lost'
                lost.append(f'{h.what} {h.sha[:12]}')
                alert(f'{entry.repo} {entry.workflow}: {h.what} {h.sha} ({h.branch}) was dispatched {LOST_MINUTES}+ '
                      'minutes ago and has no run: not sent again')
            continue
        else:
            first = parse_time(b['seen'].get(h.sha))
            if first is None or first > now:
                b['seen'][h.sha] = iso(now)
                waiting += 1
                continue
            if now - first < entry.grace or tried >= entry.per_pass or budget_low():
                waiting += 1
                continue
        tried += 1
        b['sent'][h.sha] = {'at': iso(now), 'branch': h.branch, 'result': 'dispatched'}
        save()
        try:
            api.dispatch(entry.repo, entry.workflow, h.branch, {'ref': h.sha, 'head': 'true'})
        except ApiError as e:
            if permanent(e):
                b['sent'][h.sha] = {'at': iso(now), 'branch': h.branch, 'result': 'refused', 'error': str(e)[:300]}
                errors.append(f'{h.what} {h.sha[:12]} refused (not sent again): {e}')
                alert(f'{entry.repo} {entry.workflow}: {h.what} {h.sha} ({h.branch}) refused, not sent again: {e}')
                continue
            if head_only(e):
                tries += 1
                if tries >= HEAD_TRIES:
                    b['sent'][h.sha] = {'at': iso(now), 'branch': h.branch, 'result': 'refused', 'tries': tries,
                                        'error': str(e)[:300]}
                    errors.append(f'{h.what} {h.sha[:12]} refused {tries} times (not sent again): {e}')
                    alert(f'{entry.repo} {entry.workflow}: {h.what} {h.sha} ({h.branch}) refused {tries} times, '
                          f'not sent again: {e}')
                    continue
                b['sent'][h.sha] = {'at': iso(now), 'branch': h.branch, 'result': 'retry', 'tries': tries,
                                    'retryAfter': iso(now + min(backoff * 2 ** (tries - 1), most)),
                                    'error': str(e)[:300]}
                errors.append(f'{h.what} {h.sha[:12]} refused, tries again later ({tries} of {HEAD_TRIES}): {e}')
                continue
            if mark is not None:
                b['sent'][h.sha] = mark         # a retry mark keeps its count; the entry's wait comes first
            else:
                del b['sent'][h.sha]
            entry_error = True
            fail(state, entry.key, now, str(e), backoff, most)
            errors.append(f'{h.what} {h.sha[:12]}: {e}')
            if e.rate_limited:
                limited(e)
            break
        sent.append(f'{h.what} {h.sha[:12]} ({h.branch})')
        b['seen'].pop(h.sha, None)
    if not entry_error:
        # a pass with no error about the whole entry ends its wait; one with an error kept counting (fail)
        for k in ('failures', 'retryAfter', 'error'):
            b.pop(k, None)
    parts = [f'{len(heads)} heads']
    if sent:
        parts.append('workflow_dispatch sent for ' + ', '.join(sent))
    if waiting:
        parts.append(f'{waiting} without a run waiting (grace or {entry.per_pass} a pass)')
    if lost:
        parts.append('ALERT: dispatched but no run after '
                     f'{LOST_MINUTES} minutes (not sent again): ' + ', '.join(lost))
    parts += errors + notes
    result = 'error' if errors or lost else ('dispatched' if sent else 'ran')
    return result, '; '.join(parts)
