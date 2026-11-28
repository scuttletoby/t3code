"""One tick of the own CI lane (fork-kit/ci-lane/README.md): heads in, jobs out, one shadow check per head.

The journal (state.json) holds the open heads and their jobs, keyed by commit, `<part>:<shard>` and attempt. The
check runs on GitHub are the lasting record: a head the journal does not know is first looked up there, so a lost
journal is rebuilt and nothing runs twice.

What a check can never claim: `success` needs the full commit id, a job this lane started itself (by the launcher,
under a name of this head, part and attempt), and for every planned job a records file of that commit, shard and
attempt in which every owed test has a passing result. The check is posted `in_progress` before any job starts, so a
crash leaves "not passed". A real test failure is `failure`, and nothing turns it into a pass. A job that cannot
finish (start refused three times, its unit gone, its lease out) is lost: one retry, then Actions counts for that
part (`neutral`, never `success`).
"""
import datetime, json, re, subprocess

from . import plan as plans
from .github import ApiError

SHA = re.compile(r'[0-9a-f]{40}')
NEW_PER_TICK = 3            # heads planned per tick: each is a check-run lookup, a plan read and a check post
START_TRIES = 3             # a start the launcher refuses this many times is lost
HISTORY = 500               # finished heads kept for the parity report
CANCEL_TRIES = 5
FINAL = ('passed', 'failed', 'fallback')

QUERY = '''query($owner: String!, $name: String!) {
  rateLimit { remaining resetAt }
  repository(owner: $owner, name: $name) {
    nameWithOwner
    defaultBranchRef { name target { ... on Commit { oid } } }
    pullRequests(states: OPEN, first: 100, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount
      nodes { number isCrossRepository headRefName headRefOid headRepository { nameWithOwner } }
    }
  }
}'''


def iso(when):
    return when.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_iso(text):
    try:
        return datetime.datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


class LaunchError(Exception):
    pass


class Launcher:
    """The fleet launcher's lane verbs (README, The launcher's side): argv is the config's `launch` (with sudo)."""

    def __init__(self, argv, run=subprocess.run, timeout=120):
        self.argv, self.run, self.timeout = list(argv), run, timeout

    def _call(self, verb, name, stdin=''):
        try:
            r = self.run([*self.argv, verb, name], input=stdin, capture_output=True, text=True,
                         timeout=self.timeout)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            raise LaunchError(f'{verb} {name}: {type(e).__name__}: {e}') from None
        if r.returncode != 0:
            raise LaunchError(f'{verb} {name}: exit {r.returncode}: {(r.stderr or "").strip()[-300:]}')
        return r.stdout or ''

    def start(self, name, sha, job, attempt):
        lines = [f'FORK_LANE_SHA={sha}', f'FORK_LANE_PART={job.part}', f'FORK_LANE_SHARD={job.shard}/{job.of}',
                 f'FORK_LANE_ATTEMPT={attempt}']
        self._call('lane-start', name, ''.join(f'{line}\n' for line in lines))

    def result(self, name):
        out = self._call('lane-result', name).strip()
        try:
            got = json.loads(out)
        except ValueError:
            raise LaunchError(f'lane-result {name}: not JSON') from None
        if not isinstance(got, dict) or got.get('state') not in ('running', 'done', 'gone'):
            raise LaunchError(f'lane-result {name}: no state')
        if got['state'] == 'done' and type(got.get('exit')) is not int:
            raise LaunchError(f'lane-result {name}: done without an exit code')
        return got

    def stop(self, name):
        self._call('stop', name)


def unit_name(sha, job, attempt):
    """The launcher's NAME (its rule: [a-z0-9][a-z0-9-]{0,62}-[0-9]{1,20}): this head, part, shard and attempt."""
    return f'lane-{sha[:12]}-{job.part}-{job.shard}-{attempt}'


def judge(result, sha, job, attempt):
    """(state, why) of a finished job from the launcher's answer. Only a complete record of this very run passes."""
    code = result['exit']
    rec = result.get('records')
    if code != 0:
        return 'failed', f'exit {code}'
    if not isinstance(rec, dict):
        return 'failed', 'exit 0 without a records file'
    want = {'v': 1, 'sha': sha, 'job': f'lane-{job.part}', 'run_attempt': attempt}
    for key, value in want.items():
        if rec.get(key) != value:
            return 'failed', f'records: {key} is {rec.get(key)!r}, not {value!r}'
    call = rec.get('call')
    if not isinstance(call, dict) or call.get('shard') != f'{job.shard}/{job.of}':
        return 'failed', f'records: not of shard {job.shard}/{job.of}'
    owed, results = rec.get('owed'), rec.get('results')
    if not isinstance(owed, list) or not owed or not isinstance(results, list):
        return 'failed', 'records: no owed tests'
    passed = {r.get('key') for r in results if isinstance(r, dict) and r.get('status') == 0}
    bad = [r.get('key') for r in results if not isinstance(r, dict) or r.get('status') != 0]
    if bad:
        return 'failed', f'records: {len(bad)} failed ({", ".join(map(str, bad[:3]))})'
    missing = [k for k in owed if k not in passed]
    if missing:
        return 'failed', f'records: {len(missing)} owed test(s) with no result ({", ".join(map(str, missing[:3]))})'
    return 'passed', f'{len(owed)} tests'


def lose(job, why, log, sha):
    """One retry, then Actions counts for this part."""
    if job['attempt'] == 1:
        job.update(attempt=2, state='queued', name=None, tries=0, why=f'lost ({why}); one retry')
        log(f'LOST {sha[:12]} {job["key"]}: {why}; retry (attempt 2)')
    else:
        job.update(state='fallback', why=f'lost twice ({why}): Actions counts for this part')
        log(f'ALERT {sha[:12]} {job["key"]}: lost twice ({why}); Actions counts for it')


def snapshot(data, repo):
    """{sha: {branch, what, main}} of the trusted heads (the default branch's tip, same-repository PR heads), and
    whether the list is whole (every open PR was read)."""
    r = data.get('repository') if isinstance(data, dict) else None
    if not isinstance(r, dict) or str(r.get('nameWithOwner', '')).lower() != repo.lower():
        raise ApiError(f'GraphQL for {repo}: no such repository in the answer')
    heads = {}
    default = r.get('defaultBranchRef') or {}
    tip = (default.get('target') or {}).get('oid')
    if isinstance(tip, str) and SHA.fullmatch(tip):
        heads[tip] = {'branch': default.get('name'), 'what': 'main', 'main': True}
    prs = r.get('pullRequests') or {}
    nodes = prs.get('nodes') if isinstance(prs.get('nodes'), list) else []
    for n in nodes:
        if not isinstance(n, dict) or n.get('isCrossRepository') is not False:
            continue        # a pull request from another fork is never run here
        if str((n.get('headRepository') or {}).get('nameWithOwner', '')).lower() != repo.lower():
            continue
        sha = n.get('headRefOid')
        if isinstance(sha, str) and SHA.fullmatch(sha) and sha not in heads:
            heads[sha] = {'branch': n.get('headRefName'), 'what': f'PR #{n.get("number")}', 'main': False}
    whole = type(prs.get('totalCount')) is int and prs['totalCount'] <= len(nodes)
    return heads, whole


def summary(head):
    rows = ['| Part | Attempt | Result | Time | Note |', '| --- | --- | --- | --- | --- |']
    for key, job in sorted(head['jobs'].items()):
        secs = f'{job["seconds"]:.0f} s' if isinstance(job.get('seconds'), (int, float)) else ''
        rows.append(f'| `{key}` | {job["attempt"]} | {job["state"]} | {secs} | {job.get("why") or ""} |')
    for name, why in head.get('left', []):
        rows.append(f'| `{name}` | | on Actions | | {why} |')
    return '\n'.join(rows)


def verdict(head):
    """(conclusion, title) of a head whose jobs are all final."""
    states = [j['state'] for j in head['jobs'].values()]
    if head.get('note'):
        return 'neutral', head['note']
    if 'failed' in states:
        return 'failure', f'{states.count("failed")} of {len(states)} parts failed'
    if not states:
        return 'neutral', 'no part of the plan runs on this lane'
    if 'fallback' in states:
        return 'neutral', f'{states.count("fallback")} part(s) lost twice: Actions counts for them'
    return 'success', f'all {len(states)} parts passed'


class Lane:
    def __init__(self, cfg, gh, launcher, state, now, log):
        self.cfg, self.gh, self.launcher, self.state, self.now, self.log = cfg, gh, launcher, state, now, log
        state.setdefault('heads', {})
        state.setdefault('history', [])

    @property
    def heads(self):
        return self.state['heads']

    def tick(self):
        """One pass. Errors of one head or job are logged and left for the next tick; returns how many there were."""
        errors = 0
        errors += self.collect()
        try:
            data = self.gh.graphql(QUERY, dict(zip(('owner', 'name'), self.cfg['repo'].split('/'))))
            current, whole = snapshot(data, self.cfg['repo'])
        except ApiError as e:
            self.log(f'ERROR heads: {e}')
            current, whole, errors = None, False, errors + 1
            if e.rate_limited:
                return errors
        if current is not None:
            errors += self.supersede(current, whole)
            errors += self.adopt(current)
        errors += self.start()
        errors += self.finish()
        return errors

    # -- running jobs ---------------------------------------------------------------------------------------------
    def collect(self):
        errors = 0
        for sha, head in self.heads.items():
            for job in head['jobs'].values():
                if job['state'] != 'running':
                    continue
                spec = self.job_spec(head, job)
                try:
                    got = self.launcher.result(job['name'])
                except LaunchError as e:
                    errors += 1
                    self.log(f'ERROR {sha[:12]} {job["key"]}: {e}')
                    got = {'state': 'running'}      # cannot tell: the lease decides
                if got['state'] == 'done':
                    job['state'], job['why'] = judge(got, sha, spec, job['attempt'])
                    started = parse_iso(job.get('started'))
                    job['seconds'] = (self.now - started).total_seconds() if started else None
                    self.log(f'{job["state"].upper()} {sha[:12]} {job["key"]} attempt {job["attempt"]}: {job["why"]}')
                elif got['state'] == 'gone':
                    lose(job, 'its unit is gone without a result', self.log, sha)
                elif self.now >= parse_iso(job['lease']):
                    try:
                        self.launcher.stop(job['name'])
                    except LaunchError as e:
                        self.log(f'ERROR {sha[:12]} {job["key"]}: stop after the lease: {e}')
                    lose(job, 'its lease ran out', self.log, sha)
        return errors

    def job_spec(self, head, job):
        part, shard = job['key'].rsplit(':', 1)
        return plans.Job(part, int(shard), job['of'], job['check'], job['step'])

    # -- heads --------------------------------------------------------------------------------------------------
    def supersede(self, current, whole):
        """A head that is no longer a trusted head (a new push, a closed PR) stops: its jobs are stopped and its
        check is cancelled. Heads outside an incomplete PR list are left alone (cannot tell)."""
        errors = 0
        branches = {h['branch']: sha for sha, h in current.items()}
        for sha, head in list(self.heads.items()):
            if sha in current or head['state'] == 'done':
                continue
            moved = branches.get(head['branch']) not in (None, sha)
            if not (whole or moved or head['main']):
                continue
            for job in head['jobs'].values():
                if job['state'] == 'running':
                    try:
                        self.launcher.stop(job['name'])
                    except LaunchError as e:
                        self.log(f'ERROR {sha[:12]} {job["key"]}: stop: {e}')
                if job['state'] not in FINAL:
                    job.update(state='cancelled', why='superseded')
            head['state'] = 'cancelled'
            try:
                if head.get('check'):
                    self.gh.update_check(head['check'], {
                        'status': 'completed', 'conclusion': 'cancelled', 'completed_at': iso(self.now),
                        'output': {'title': 'superseded: no longer a head', 'summary': summary(head)}})
                del self.heads[sha]
                self.log(f'CANCELLED {sha[:12]} ({head["what"]}): no longer a head')
            except ApiError as e:
                errors += 1
                head['cancelTries'] = head.get('cancelTries', 0) + 1
                self.log(f'ERROR {sha[:12]}: cancel its check: {e}')
                if head['cancelTries'] >= CANCEL_TRIES:
                    del self.heads[sha]
        return errors

    def adopt(self, current):
        errors, new = 0, 0
        order = sorted(current.items(), key=lambda kv: not kv[1]['main'])     # the default branch first
        for sha, h in order:
            if sha in self.heads or new >= NEW_PER_TICK or self.finished(sha):
                continue
            new += 1
            try:
                self.plan_head(sha, h)
            except ApiError as e:
                errors += 1
                self.log(f'ERROR {sha[:12]} ({h["what"]}): {e}')
        return errors

    def finished(self, sha):
        return any(entry['sha'] == sha for entry in self.state['history'])

    def plan_head(self, sha, h):
        """GitHub first (a check of ours means this head was taken before), then the plan, then the check in
        progress, and only then the head with its queued jobs."""
        ours = self.gh.our_checks(sha, self.cfg['checkName'], self.cfg['appId'])
        if any(c.get('status') == 'completed' for c in ours):
            self.state['history'].append({'sha': sha, 'what': h['what'], 'finished': iso(self.now),
                                          'conclusion': 'adopted', 'jobs': {}, 'actions': 'none'})
            self.log(f'KNOWN {sha[:12]} ({h["what"]}): its check is complete on GitHub; nothing runs again')
            return
        attempt = 2 if ours else 1      # in progress with no journal: the first run was lost
        text = self.gh.file_at(plans.PATH, sha)
        note, jobs, left, workflow = None, [], [], None
        if text is None:
            note = f'no {plans.PATH} at this commit: Actions counts'
        else:
            try:
                p = plans.parse(text)
                jobs, left, workflow = p.jobs, p.left, p.workflow
            except plans.PlanError as e:
                note = f'the plan cannot be read ({e}): Actions counts'
        head = {'sha': sha, 'branch': h['branch'], 'what': h['what'], 'main': h['main'], 'seen': iso(self.now),
                'state': 'open', 'note': note, 'left': left, 'check': None, 'workflow': workflow,
                'jobs': {j.key: {'key': j.key, 'of': j.of, 'check': j.actions_check, 'step': j.step,
                                 'attempt': attempt, 'state': 'queued', 'name': None, 'tries': 0,
                                 'why': 'retry: the journal had lost this head' if attempt == 2 else None}
                         for j in jobs}}
        body = {'status': 'in_progress', 'started_at': iso(self.now),
                'output': {'title': 'running on the own lane (shadow)', 'summary': summary(head)}}
        if ours:
            head['check'] = ours[0]['id']
            self.gh.update_check(head['check'], body)
        else:
            head['check'] = self.gh.create_check({'name': self.cfg['checkName'], 'head_sha': sha,
                                                  'external_id': f'ci-lane:{sha}', **body})
        self.heads[sha] = head
        self.log(f'PLANNED {sha[:12]} ({h["what"]}): {len(jobs)} job(s){", attempt 2" if attempt == 2 else ""}'
                 f'{"; " + note if note else ""}')

    # -- starting and finishing ---------------------------------------------------------------------------------
    def start(self):
        errors = 0
        running = sum(j['state'] == 'running' for h in self.heads.values() for j in h['jobs'].values())
        order = sorted(self.heads.items(), key=lambda kv: (not kv[1]['main'], kv[1]['seen']))
        for sha, head in order:
            if head['state'] != 'open' or not head.get('check'):
                continue        # never a job before its check is in progress
            for key in sorted(head['jobs']):
                job = head['jobs'][key]
                if job['state'] != 'queued' or running >= self.cfg['maxJobs']:
                    continue
                spec = self.job_spec(head, job)
                name = unit_name(sha, spec, job['attempt'])
                try:
                    self.launcher.start(name, sha, spec, job['attempt'])
                    how = 'STARTED'
                except LaunchError as e:
                    if not self.there(name):
                        errors += 1
                        job['tries'] += 1
                        self.log(f'ERROR {sha[:12]} {key}: {e}')
                        if job['tries'] >= START_TRIES:
                            lose(job, f'start refused {START_TRIES} times', self.log, sha)
                        continue
                    how = 'ADOPTED'
                lease = self.now + datetime.timedelta(minutes=self.cfg['leaseMinutes'])
                job.update(state='running', name=name, started=iso(self.now), lease=iso(lease))
                running += 1
                self.log(f'{how} {sha[:12]} {key} attempt {job["attempt"]} as {name}')
        return errors

    def there(self, name):
        """A refused start whose unit is there all the same: a start that went through before the tick that made it
        could write its journal (stopped half-way). The name is this commit, part, shard and attempt, and only this
        lane starts lane units, so it is this job: adopted (its lease from now), never started twice. A lookup that
        fails: not there (the refusal counts)."""
        try:
            return self.launcher.result(name)['state'] in ('running', 'done')
        except LaunchError:
            return False

    def finish(self):
        errors = 0
        for sha, head in list(self.heads.items()):
            if head['state'] != 'open' or not head.get('check'):
                continue
            if any(j['state'] not in FINAL for j in head['jobs'].values()):
                continue
            conclusion, title = verdict(head)
            try:
                self.gh.update_check(head['check'], {
                    'status': 'completed', 'conclusion': conclusion, 'completed_at': iso(self.now),
                    'output': {'title': title, 'summary': summary(head)}})
            except ApiError as e:
                errors += 1
                self.log(f'ERROR {sha[:12]}: post its verdict: {e}')
                continue
            del self.heads[sha]
            self.state['history'].append({
                'sha': sha, 'what': head['what'], 'finished': iso(self.now), 'conclusion': conclusion,
                'workflow': head.get('workflow'),
                'jobs': {k: {'state': j['state'], 'seconds': j.get('seconds'), 'check': j['check'],
                             'step': j['step']} for k, j in head['jobs'].items()},
                'actions': None, 'actionsTries': 0})
            del self.state['history'][:-HISTORY]
            self.log(f'DONE {sha[:12]} ({head["what"]}): {conclusion}: {title}')
        return errors

