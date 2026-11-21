#!/usr/bin/env python3
"""Fork only: one automatic re-run of a failed Fork tests run, only for a known infrastructure sign.

  python3 -I fork-ci-rerun.py sweep [--run-id N]   .github/workflows/fork-ci-rerun.yml (every 10 minutes, GitHub-hosted,
                                                   actions: write, checks: read, issues: write): each failed trusted run
                                                   that ended in the last catch_up_minutes (or only run N) whose every
                                                   failed job shows a sign gets GitHub's own "re-run failed jobs", once.
  python3 -I fork-ci-rerun.py check                checks CONFIG (fork-ci-rerun.test.py runs it).

THE rules, in one place (the facts, patterns and numbers are CONFIG's, .github/fork-ci-rerun.json; none is here):
- Only a trusted run, by the fleet's own trust rule (fork-fleet.py run_refused, with the config CONFIG `trust`
  names): never looser than the rule that decides what may run on the fleet.
- Never twice: a run any of whose attempts was triggered by someone else (GitHub's re-run from this script, or the
  fleet sweep's, is triggered by github-actions) is left red. A repeat red goes to an issue and its fix first.
- Each failed job (failure, cancelled, timed out) but CONFIG's followers (jobs that need all the others and fail
  only because one of them did) must show a sign. Text alone is never a sign: a step can print any annotation
  (::error::, ::notice title=...::), so a message counts only in the form the runner itself writes (failure level,
  no title, on .github, the whole message one of CONFIG's patterns), and only when the job's own step record
  agrees:
    lost         GitHub says the runner lost communication or was shut down (signs.lost), the job ended failure
                 on that runner, none of its steps failed, no time limit was hit, and the probe's end step
                 (probe_end_step) never completed;
    memory full  the job ended failure on a runner that is not GitHub-hosted (hosted_runner_groups: a kill there is
                 the job's own), and its own pressure note shows a box with a memory limit stalled (full) at least
                 memory_full.box_full_share_percent of the job AND OOM kills, memory.max hits or memory.high
                 throttling in it. A machine-wide OOM kill, or one event without the stall, is no sign;
    limit        GitHub says the job or a step ran past its time limit (signs.limit, with the minutes), the job (or
                 that step) really ran that long, no other step failed, and the note shows the machine or the box
                 stalled (CONFIG stalled).
  The note counts only when the probe's end step completed (so the job wrote its own) and is the job's only one.
  One failed job without a sign (a test failure, no note, a garbled note, two notes) leaves the run red: fail
  closed. GitHub's "canceled by" (signs.cancelled_by) on any failed job leaves it red: a person's cancel stands.
  A run whose branch has moved on, or is gone, is not re-run.
- With CONFIG skip_when (fleet mode), a run that qualifies is only reported, once.
Visible and durable: every re-run (and every fleet-mode report) is first a comment on one tracking issue (CONFIG
notice: opened once, by github-actions, then a comment per re-run with the run, the signs and the numbers), then a
warning annotation and a row in this job's summary, which also counts the issue's re-runs of the last window_hours.
The comment is written before the request, so a reply lost after GitHub took it still leaves the reason. Nothing
else is kept between passes: each reads GitHub, so a pass that stopped half-way is finished by the next, and one that
runs again finds the bot's attempt (and the issue's marker) and does nothing.
Runbook: .github/fork-ci-rerun.md. Standard library only.
"""
import argparse, calendar, http.client, importlib.util, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request

sys.dont_write_bytecode = True   # loading fork-fleet.py must not leave __pycache__ in the tree

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(os.path.dirname(HERE), 'fork-ci-rerun.json')
API = os.environ.get('GITHUB_API_URL') or 'https://api.github.com'
NOTE_TITLE = 'fork-ci-pressure'          # fork-ci-pressure.py's
NOTE_HEAD = 'fork-ci-pressure v1 '
RED = ('failure', 'cancelled', 'timed_out')
CHECK_RUN = re.compile(r'/check-runs/([0-9]{1,20})$')
MARK = re.compile(r'<!-- fork-ci-rerun run=([0-9]{1,20}) attempt=([0-9]{1,5}) what=(rerun|report) -->')
# Each sign's named groups: `minutes` is how long GitHub says the limit was; `step` (when there) names the step.
GROUPS = {'lost': set(), 'limit': {'minutes'}, 'cancelled_by': set()}
LONGEST = 60000   # characters of a comment (GitHub takes 65536)

_spec = importlib.util.spec_from_file_location('fork_fleet', os.path.join(HERE, 'fork-fleet.py'))
FLEET = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(FLEET)


def say(msg, out=None):
    print(msg, file=out or sys.stdout, flush=True)


# ---------------------------------------------------------------- config

def load(path=CONFIG):
    with open(path, encoding='utf-8') as f:
        cfg = json.load(f)
    problems = check_config(cfg)
    if problems:
        raise ValueError(f'{path}: ' + '; '.join(problems))
    fleet = FLEET.load(os.path.join(os.path.dirname(os.path.abspath(path)), cfg['trust']))
    workflows = [os.path.basename(w) for w in fleet['trusted']['workflows']]
    if cfg['workflow'] not in workflows:
        raise ValueError(f'{path}: workflow {cfg["workflow"]!r} is not one the trust config trusts ({workflows})')
    return dict(cfg, fleet=fleet)


def number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def names(x, empty=False):
    return isinstance(x, list) and (empty or bool(x)) and all(isinstance(s, str) and s for s in x)


def check_config(cfg):
    out = []
    if not isinstance(cfg, dict):
        return ['not a JSON object']
    known = {'_doc', 'trust', 'workflow', 'followers', 'list_hours', 'catch_up_minutes', 'window_hours', 'skip_when',
             'hosted_runner_groups', 'probe_end_step', 'limit_slack_seconds', 'signs', 'stalled', 'memory_full',
             'notice'}
    out += [f'unknown key {k!r}' for k in sorted(set(cfg) - known)]
    out += [f'missing key {k!r}' for k in sorted(known - {'_doc'} - set(cfg))]
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.json', str(cfg.get('trust', ''))):
        out.append('trust must be the file name of the fleet config beside this one')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.ya?ml', str(cfg.get('workflow', ''))):
        out.append('workflow must be a workflow file name')
    if not names(cfg.get('followers'), empty=True):
        out.append('followers must be a list of job names')
    for key in ('list_hours', 'catch_up_minutes', 'window_hours', 'limit_slack_seconds'):
        if not number(cfg.get(key)) or cfg[key] <= 0:
            out.append(f'{key} must be a positive number')
    if number(cfg.get('list_hours')) and number(cfg.get('catch_up_minutes')) \
            and cfg['list_hours'] * 60 <= cfg['catch_up_minutes']:
        out.append('list_hours must reach further back than catch_up_minutes')
    skip = cfg.get('skip_when')
    if not (isinstance(skip, dict) and re.fullmatch(r'[A-Z][A-Z0-9_]*', str(skip.get('variable', '')))
            and names(skip.get('is'))):
        out.append('skip_when must be {"variable": NAME, "is": [values]}')
    if not names(cfg.get('hosted_runner_groups')):
        out.append('hosted_runner_groups must be a list of runner group names')
    if not (isinstance(cfg.get('probe_end_step'), str) and cfg['probe_end_step']):
        out.append('probe_end_step must be the name of the probe\'s end step')
    signs = cfg.get('signs')
    if not isinstance(signs, dict) or set(signs) != set(GROUPS):
        out.append(f'signs must have exactly {", ".join(sorted(GROUPS))}')
    else:
        for kind, patterns in signs.items():
            if not names(patterns):
                out.append(f'signs.{kind} must be a list of patterns')
                continue
            for p in patterns:
                try:
                    groups = set(re.compile(p).groupindex)
                except re.error as e:
                    out.append(f'signs.{kind}: {p!r}: {e}')
                    continue
                if not GROUPS[kind] <= groups:
                    out.append(f'signs.{kind}: {p!r} lacks the group(s) {sorted(GROUPS[kind] - groups)}')
                if not p.startswith('The '):
                    out.append(f'signs.{kind}: {p!r}: a whole GitHub message, from its first word')
    for key, fields in (('stalled', ('full_share_percent', 'full_avg300_percent')),
                        ('memory_full', ('box_full_share_percent', 'oom_kills', 'max_events', 'high_events'))):
        got = cfg.get(key)
        if not isinstance(got, dict) or set(got) != set(fields) or not all(number(got[f]) and got[f] > 0
                                                                           for f in fields):
            out.append(f'{key} must have exactly {", ".join(fields)}, each a positive number')
    notice = cfg.get('notice')
    if not (isinstance(notice, dict) and set(notice) == {'title', 'labels', 'author'}
            and isinstance(notice.get('title'), str) and 0 < len(notice['title']) <= 200
            and names(notice.get('labels'), empty=True) and re.fullmatch(r'[a-z0-9-]+\[bot\]', str(notice.get('author')))):
        out.append('notice must be {"title": TEXT, "labels": [names], "author": "<app>[bot]"}')
    return out


# ---------------------------------------------------------------- GitHub

class GitHub:
    def __init__(self, token, repo):
        self.token, self.repo = token, repo

    def call(self, path, method='GET', data=None):
        """path: below /repos/<repo>/."""
        if not self.token:
            raise ValueError(f'no token for {path}')
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(f'{API}/repos/{self.repo}/{path}', data=body, method=method,
                                     headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'fork-ci-rerun',
                                              'Authorization': f'Bearer {self.token}'})
        with urllib.request.urlopen(req, timeout=30) as r:
            got = r.read()
        return json.loads(got) if got else {}

    def pages(self, path, key=None, most=10):
        """Every item of a listing (got[key], or the reply itself), page after page until a short one; each id once
        (an item that moved down a page while new ones arrived is listed twice). More than `most` full pages is a
        ValueError: a listing cut short would be read as complete."""
        out, seen = [], set()
        for page in range(1, most + 1):
            got = self.call(f'{path}{"&" if "?" in path else "?"}per_page=100&page={page}')
            items = got[key] if key else got
            if not isinstance(items, list):
                raise ValueError(f'{path}: page {page} is not a list')
            for item in items:
                ident = item.get('id') if isinstance(item, dict) else None
                if ident is None or ident not in seen:
                    seen.add(ident)
                    out.append(item)
            if len(items) < 100:
                return out
        raise ValueError(f'{path}: more than {most} pages of 100; not read as complete')


# A truncated reply is http.client.IncompleteRead (an HTTPException), a garbled one a ValueError.
TROUBLE = (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, KeyError, TypeError,
           AttributeError)


def stamp(text):
    try:
        return calendar.timegm(time.strptime(text, '%Y-%m-%dT%H:%M:%SZ'))
    except (TypeError, ValueError):
        return None


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def login(x):
    return (x or {}).get('login')


def took(x):
    """Seconds from a job's or step's started_at to its completed_at, or None."""
    a, b = stamp(x.get('started_at')), stamp(x.get('completed_at'))
    return b - a if a is not None and b is not None and b >= a else None


# ---------------------------------------------------------------- the rules

def from_runner(a):
    """An annotation in the form the runner writes its own job messages: failure level, no title, on .github. A step
    can print one that looks the same (::error::), so a sign read from one needs the job's step record too."""
    return a.get('annotation_level') == 'failure' and not a.get('title') and a.get('path') == '.github'


def github_says(cfg, kind, annotations):
    """(message, match) of the first runner-form annotation whose whole message is one of signs[kind], or None."""
    for a in annotations:
        if not from_runner(a):
            continue
        msg = str(a.get('message') or '').strip()
        for p in cfg['signs'][kind]:
            m = re.fullmatch(p, msg, re.S)
            if m:
                return msg, m
    return None


def end_step_ran(cfg, job):
    """Did the probe's end step complete? Then the runner lived to the job's end, and the note (if any) is its."""
    return any(s.get('name') == cfg['probe_end_step'] and s.get('status') == 'completed'
               and s.get('conclusion') == 'success' for s in job.get('steps') or [])


def failed_steps(job):
    return [s.get('name') for s in job.get('steps') or [] if s.get('conclusion') == 'failure']


def note_of(cfg, job, annotations):
    """The job's own pressure note (fork-ci-pressure.py) as a dict, or (None, why)."""
    if not end_step_ran(cfg, job):
        return None, f'its "{cfg["probe_end_step"]}" step did not complete, so it has no note of its own'
    notes = [a for a in annotations if a.get('title') == NOTE_TITLE]
    if not notes:
        return None, 'no pressure note (the note was dropped)'
    if len(notes) > 1:
        return None, f'{len(notes)} pressure notes, not one (a step printed one too)'
    a = notes[0]
    msg = str(a.get('message') or '')
    if a.get('annotation_level') != 'notice' or a.get('path') != '.github' or not msg.startswith(NOTE_HEAD):
        return None, 'a garbled pressure note'
    try:
        note = json.loads(msg[len(NOTE_HEAD):])
    except ValueError:
        return None, 'a garbled pressure note'
    if not (isinstance(note, dict) and isinstance(note.get('host'), dict) and isinstance(note.get('cgroups'), list)
            and all(isinstance(c, dict) for c in note['cgroups']) and isinstance(note.get('status'), str)):
        return None, 'a garbled pressure note'
    return note, None


def at_least(value, bound):
    return number(value) and value >= bound


def stalled(cfg, note):
    """Evidence that the machine or the job's box was stalled for memory, or None."""
    st, host = cfg['stalled'], note['host']
    if at_least(host.get('full_share'), st['full_share_percent']):
        return f'the machine stalled (full) {host["full_share"]}% of the job'
    for c in note['cgroups']:
        if at_least(c.get('full_share'), st['full_share_percent']):
            return f'box {c.get("path")} stalled (full) {c["full_share"]}% of the job'
    if at_least(host.get('full_avg300'), st['full_avg300_percent']):
        return f'the machine\'s 5-minute full stall average was {host["full_avg300"]}% at the end'
    return None


def hosted(cfg, job):
    group = job.get('runner_group_name')
    return not (isinstance(group, str) and group) or group in cfg['hosted_runner_groups']


def memory_full(cfg, job, note):
    """Evidence that the job's own box was full and stalled, or None. Only a failure on a runner that is not
    GitHub-hosted, only a box with a memory limit, and only events together with that box's stall."""
    if job.get('conclusion') != 'failure' or note.get('status') != 'failure' or hosted(cfg, job):
        return None
    mf = cfg['memory_full']
    for c in note['cgroups']:
        if not (number(c.get('max_mb')) or number(c.get('high_mb'))):
            continue   # no limit: not a box
        if not at_least(c.get('full_share'), mf['box_full_share_percent']):
            continue
        where = (f'box {c.get("path")} (max {c.get("max_mb")} MB, high {c.get("high_mb")} MB), stalled (full) '
                 f'{c["full_share"]}% of the job')
        if at_least(c.get('oom_kill'), mf['oom_kills']):
            return f'{where}: {c["oom_kill"]} OOM kill(s) in it'
        if at_least(c.get('max'), mf['max_events']):
            return f'{where}: hit its memory.max {c["max"]} time(s)'
        if at_least(c.get('high'), mf['high_events']):
            return f'{where}: throttled at memory.high {c["high"]} time(s)'
    return None


def limit_hit(cfg, job, annotations):
    """GitHub's time-limit message, when the job's record agrees: the job (or the step it names) ran at least that
    long and no other step failed. (message, None), or (None, why) when there is no such message."""
    why = None
    for a in annotations:
        if not from_runner(a):
            continue
        msg = str(a.get('message') or '').strip()
        for p in cfg['signs']['limit']:
            m = re.fullmatch(p, msg, re.S)
            if not m:
                continue
            bound = int(m['minutes']) * 60 - cfg['limit_slack_seconds']
            step = m.groupdict().get('step')
            others = [s for s in failed_steps(job) if s != step]
            if others:
                why = f'GitHub\'s time-limit text, but step {others[0]!r} failed'
                continue
            if step is not None:
                ran = [took(s) or 0 for s in job.get('steps') or [] if s.get('name') == step
                       and s.get('conclusion') in ('failure', 'cancelled')]
                if not any(t >= bound for t in ran):
                    why = f'GitHub\'s time-limit text, but no step {step!r} ran {m["minutes"]} min and ended red'
                    continue
            elif (took(job) or 0) < bound:
                why = f'GitHub\'s time-limit text, but the job ran {int((took(job) or 0) // 60)} min, not {m["minutes"]}'
                continue
            return msg, None
    return None, why


def numbers(note):
    """The note's numbers, for the log."""
    h = note['host']
    out = [f'machine: full {h.get("full_share")}% some {h.get("some_share")}% of {note.get("seconds")} s, '
           f'full avg10/60/300 {h.get("full_avg10")}/{h.get("full_avg60")}/{h.get("full_avg300")}%, '
           f'available {h.get("available_mb")} MB, swap used {h.get("swap_used_mb")} MB, OOM kills {h.get("oom_kill")}']
    out += [f'box {c.get("path")}: max {c.get("max_mb")} MB high {c.get("high_mb")} MB use {c.get("current_mb")} MB, '
            f'events high {c.get("high")} max {c.get("max")} oom_kill {c.get("oom_kill")}, full {c.get("full_share")}%'
            for c in note['cgroups']]
    return '; '.join(out)


def job_sign(cfg, job, annotations):
    """(sign, evidence) when a failed job shows an infrastructure sign, else (None, why not)."""
    cancel = github_says(cfg, 'cancelled_by', annotations)
    if cancel:
        return None, f'GitHub: "{cancel[0][:200]}": a person\'s cancel stands'
    note, why = note_of(cfg, job, annotations)
    lost = github_says(cfg, 'lost', annotations)
    if lost:
        msg = f'GitHub: "{lost[0][:200]}"'
        if job.get('conclusion') != 'failure':
            return None, f'{msg}, but the job ended {job.get("conclusion")}, not failure'
        if failed_steps(job):
            return None, f'{msg}, but step {failed_steps(job)[0]!r} failed: the text is a step\'s output'
        if github_says(cfg, 'limit', annotations):
            return None, f'{msg}, but GitHub also says it ran past a time limit: a step that hung, not a lost runner'
        runner = lost[1].groupdict().get('runner')
        if runner is not None and job.get('runner_name') and runner != job['runner_name']:
            return None, f'{msg}, but the job ran on {job["runner_name"]!r}: not its runner'
        if end_step_ran(cfg, job):
            return None, f'{msg}, but its "{cfg["probe_end_step"]}" step completed: the runner was not lost'
        return 'lost', f'{msg}; no step failed and the job never reached its end step (no numbers: the runner died)'
    full = memory_full(cfg, job, note) if note else None
    if full:
        return 'memory full', f'{full} ({numbers(note)})'
    hit, not_hit = limit_hit(cfg, job, annotations)
    if hit and note:
        stall = stalled(cfg, note)
        if stall:
            return 'limit while stalled', f'GitHub: "{hit[:200]}"; {stall} ({numbers(note)})'
        return None, f'ran past its time limit, but the machine was not stalled ({numbers(note)})'
    if hit:
        return None, f'ran past its time limit, and {why}: cannot tell it was stalled'
    return None, ('no infrastructure sign (a failure of its own, or a cancel)' + (f'; {not_hit}' if not_hit else '')
                  + (f'; {why}' if not note else ''))


def annotations_of(gh, job):
    m = CHECK_RUN.search(job.get('check_run_url') or '')
    return gh.pages(f'check-runs/{int(m[1]) if m else int(job["id"])}/annotations', most=3)


def current(gh, run):
    """Is the run's commit still the head of its branch? A newer push makes an old run moot."""
    try:
        branch = gh.call(f'branches/{urllib.parse.quote(run.get("head_branch") or "", safe="")}')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False   # the branch is gone (merged and deleted)
        raise
    return (branch.get('commit') or {}).get('sha') == run.get('head_sha')


def decide(cfg, gh, run, env):
    """('quiet', None) for a run that is no candidate, ('leave', why), ('report', evidence) or ('rerun', evidence)."""
    rid, attempt = int(run['id']), int(run.get('run_attempt') or 1)
    if run.get('status') != 'completed' or run.get('conclusion') not in RED:
        return 'quiet', None
    actors = cfg['fleet']['trusted']['actors']
    by = login(run.get('triggering_actor'))
    if attempt > 1 and by not in actors:
        return 'leave', f'attempt {attempt} was triggered by {by!r} (an automatic re-run): a repeat red stays red'
    why = FLEET.run_refused(cfg['fleet'], gh, run)
    if why:
        return 'leave', f'not a trusted run: {why}'
    for n in range(attempt - 1, 0, -1):
        by = login(gh.call(f'actions/runs/{rid}/attempts/{n}').get('triggering_actor'))
        if by not in actors:
            return 'leave', f'attempt {n} was triggered by {by!r} (an automatic re-run): never twice'
    jobs = gh.pages(f'actions/runs/{rid}/jobs?filter=latest', 'jobs')
    red = [j for j in jobs if j.get('conclusion') in RED and j.get('name') not in cfg['followers']]
    if not red:
        return 'leave', 'no failed job of its own (only followers failed, or nothing did)'
    evidence = []
    for j in red:
        sign, why = job_sign(cfg, j, annotations_of(gh, j))
        if not sign:
            return 'leave', f'{j.get("name")} ended {j.get("conclusion")}: {why}'
        evidence.append(f'{j.get("name")} ({j.get("conclusion")}): {sign}: {why}')
    if not current(gh, run):
        return 'leave', f'{run.get("head_branch")!r} has moved on or is gone; it qualified: ' + ' | '.join(evidence)
    skip = cfg['skip_when']
    if (env.get(skip['variable']) or '').strip() in skip['is']:
        return 'report', ' | '.join(evidence)
    return 'rerun', ' | '.join(evidence)


# ---------------------------------------------------------------- the tracking issue

class Notice:
    """The one tracking issue (CONFIG notice): the durable, visible record of every automatic re-run outside this
    sweep's own run. Only an open issue github-actions opened with exactly that title is ours, never one a person
    opened; with none open, the first re-run opens it. Each re-run (and each fleet-mode report) is one comment with a
    marker line (MARK: run, attempt, what), so a pass never writes the same one twice and the count reads them."""

    def __init__(self, cfg, gh, now):
        self.cfg, self.gh, self.now, self.hours = cfg['notice'], gh, now, cfg['window_hours']
        self.issue, self.comments = None, None

    def _ours(self):
        n = self.cfg
        listed = self.gh.pages(f'issues?state=open&creator={urllib.parse.quote(n["author"], safe="")}'
                               f'&sort=created&direction=asc')
        return sorted(int(i['number']) for i in listed if isinstance(i, dict) and 'pull_request' not in i
                      and i.get('title') == n['title'] and login(i.get('user')) == n['author'])

    def load(self, since=None):
        if self.comments is not None:
            return
        ours = self._ours()
        self.issue = ours[0] if ours else None
        start = iso(min(self.now() - self.hours * 3600, since or self.now()))
        self.comments = [] if self.issue is None else [
            c for c in self.gh.pages(f'issues/{self.issue}/comments?since={start}')
            if isinstance(c, dict) and login(c.get('user')) == self.cfg['author']]

    def marks(self):
        out = {}
        for c in self.comments or []:
            for m in MARK.finditer(str(c.get('body') or '')):
                out.setdefault((int(m[1]), int(m[2]), m[3]), c.get('created_at'))
        return out

    def has(self, rid, attempt, what):
        self.load()
        return (int(rid), int(attempt), what) in self.marks()

    def count(self):
        """Re-runs this sweep asked for in the last window_hours, from the issue's markers."""
        self.load()
        since = self.now() - self.hours * 3600
        return sum(1 for (_, _, what), at in self.marks().items() if what == 'rerun' and (stamp(at) or 0) >= since)

    def where(self):
        return f'issue #{self.issue}' if self.issue else 'no tracking issue yet'

    def _open(self):
        n = self.cfg
        got = self.gh.call('issues', method='POST', data={
            'title': n['title'], 'labels': n['labels'],
            'body': ('Each comment below is one automatic re-run of a failed run (or, in fleet mode, a run that '
                     'would have had one), with its jobs, the signs and the memory pressure numbers. Many of them '
                     'mean a machine that stalls or fills up: fix that, the re-run only hides it for one attempt. '
                     'Closing this issue is fine; the next re-run opens a new one. Rules and runbook: '
                     '`.github/fork-ci-rerun.md`.')})
        mine = int(got['number'])
        # A dispatched pass may run beside a scheduled one (fork-ci-rerun.yml); if both opened one, the lowest wins
        # and the other is closed, pointing at it, so there is only ever one.
        ours = self._ours()
        if ours and ours[0] < mine:
            self.gh.call(f'issues/{mine}/comments', method='POST', data={'body': f'Duplicate of #{ours[0]}.'})
            self.gh.call(f'issues/{mine}', method='PATCH', data={'state': 'closed', 'state_reason': 'not_planned'})
            return ours[0]
        return mine

    def add(self, run, what, text):
        """Write one comment for this run's attempt; returns the issue's number."""
        self.load()
        rid, attempt = int(run['id']), int(run.get('run_attempt') or 1)
        url = run.get('html_url') or f'run {rid}'
        head = (f'**Re-run asked** for [run {rid}]({url}) attempt {attempt}: GitHub\'s "re-run failed jobs" makes '
                f'attempt {attempt + 1}; if the run has no attempt {attempt + 1}, the request did not go through '
                '(the sweep\'s log says why).' if what == 'rerun' else
                f'**Not re-run (fleet mode)**: [run {rid}]({url}) attempt {attempt} qualifies for its one automatic '
                're-run; re-run it by hand.')
        body = (f'<!-- fork-ci-rerun run={rid} attempt={attempt} what={what} -->\n{head}\n\n'
                f'{run.get("event")} `{run.get("head_branch")}` `{str(run.get("head_sha"))[:12]}`\n\n'
                + ''.join(f'- {part.strip()}\n' for part in text.split(' | ')))[:LONGEST]
        if self.issue is None:
            self.issue = self._open()
        got = self.gh.call(f'issues/{self.issue}/comments', method='POST', data={'body': body})
        self.comments.append(dict(got if isinstance(got, dict) else {}, body=body,
                                  created_at=(got or {}).get('created_at') or iso(self.now())))
        return self.issue


def rerun(gh, run, record):
    """GitHub's own re-run of failed jobs (and the jobs after them), once: (True, how) when this pass asked for it.
    record() writes the durable notice first, so a reply lost after GitHub took the request still leaves the why."""
    rid, attempt = int(run['id']), int(run.get('run_attempt') or 1)
    fresh = gh.call(f'actions/runs/{rid}')
    if int(fresh.get('run_attempt') or 1) != attempt or fresh.get('status') != 'completed':
        return False, f'already re-run or running again (attempt {fresh.get("run_attempt")}, {fresh.get("status")})'
    record()
    try:
        gh.call(f'actions/runs/{rid}/rerun-failed-jobs', method='POST')
    except urllib.error.HTTPError as e:
        again = gh.call(f'actions/runs/{rid}')
        if int(again.get('run_attempt') or 1) != attempt or again.get('status') != 'completed':
            return False, f'already re-run (GitHub refused a second request: HTTP {e.code})'
        raise
    return True, f'attempt {attempt + 1} asked for'


def summary(env, lines):
    path = env.get('GITHUB_STEP_SUMMARY')
    if path and lines:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(''.join(f'{line}\n' for line in lines))


def cell(text):
    return str(text).replace('|', '\\|').replace('\n', ' ')


def sweep(cfg, env, gh, now=time.time, run_id=None, out=None):
    notice = Notice(cfg, gh, now)
    rows, trouble, asked = [], 0, 0
    try:
        if run_id:
            runs = [gh.call(f'actions/runs/{int(run_id)}')]
        else:
            since = iso(now() - cfg['list_hours'] * 3600)
            runs = gh.pages(f'actions/workflows/{cfg["workflow"]}/runs?created=%3E%3D{since}', 'workflow_runs')
    except TROUBLE as e:
        runs, trouble = [], 1
        say(f'::warning::fork-ci re-run: the runs: {e.__class__.__name__}: {e}; the next pass tries again', out)
        rows.append(f'| - | not read | {cell(e.__class__.__name__)}: {cell(e)} |')
    recent = now() - cfg['catch_up_minutes'] * 60
    for run in runs:
        rid = run.get('id')
        try:
            if not run_id and (stamp(run.get('updated_at')) or 0) < recent:
                continue   # ended before the catch-up window: too old to re-run now
            what, text = decide(cfg, gh, run, env)
            where = f'run {rid} ({run.get("event")} {run.get("head_branch")} {str(run.get("head_sha"))[:12]})'
            attempt = int(run.get('run_attempt') or 1)
            if what == 'quiet':
                continue
            if what == 'leave':
                say(f'{where}: left red: {text}', out)
                rows.append(f'| {rid} | left red | {cell(text)} |')
                continue
            notice.load(since=stamp(run.get('created_at')))
            if what == 'report':
                skip = cfg['skip_when']
                state = f'{skip["variable"]}={env.get(skip["variable"])}'
                if notice.has(rid, attempt, 'report'):
                    say(f'{where}: qualifies, not re-run ({state}); reported already ({notice.where()})', out)
                    rows.append(f'| {rid} | not re-run ({cell(state)}), reported already | {cell(text)} |')
                    continue
                issue = notice.add(run, 'report', text)
                say(f'::error title=fork-ci re-run::{where} qualifies for its one automatic re-run, but {state}: '
                    f'not re-run; re-run it by hand (.github/fork-ci-rerun.md, issue #{issue}). {text}', out)
                rows.append(f'| {rid} | not re-run ({cell(state)}), issue #{issue} | {cell(text)} |')
                continue

            def record():
                if not notice.has(rid, attempt, 'rerun'):
                    notice.add(run, 'rerun', text)
            done, how = rerun(gh, run, record)
            if done:
                asked += 1
                say(f'::warning title=fork-ci re-run::{where} re-run once ({how}, {notice.where()}), '
                    f'infrastructure: {text}', out)
                rows.append(f'| {rid} | **re-run** ({cell(how)}, {notice.where()}) | {cell(text)} |')
            else:
                say(f'{where}: {how}', out)
                rows.append(f'| {rid} | {cell(how)} | {cell(text)} |')
        except TROUBLE as e:
            trouble += 1
            say(f'::warning::fork-ci re-run: run {rid}: {e.__class__.__name__}: {e}; the next pass tries again', out)
            rows.append(f'| {rid} | not read | {cell(e.__class__.__name__)}: {cell(e)} |')
    try:
        count = (f'{asked} run(s) re-run by this pass; {notice.count()} automatic re-run(s) asked in the last '
                 f'{cfg["window_hours"]} h ({notice.where()})')
    except TROUBLE as e:
        trouble += 1
        count = f'{asked} run(s) re-run by this pass; the tracking issue could not be read ({e.__class__.__name__})'
    say(f'fork-ci re-run: {count}', out)
    lines = ['### fork-ci re-run', '', count, '']
    if rows:
        lines += ['| Run | Outcome | Why |', '| --- | --- | --- |'] + rows
    summary(env, lines)
    return 1 if trouble else 0


def main(argv=None, env=os.environ):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('verb', choices=('sweep', 'check'))
    p.add_argument('--run-id', type=int)
    p.add_argument('--config', default=CONFIG)
    a = p.parse_args(argv)
    cfg = load(a.config)
    if a.verb == 'check':
        say(f'{a.config}: ok')
        return 0
    return sweep(cfg, env, GitHub(env.get('GITHUB_TOKEN'), cfg['fleet']['repository']), run_id=a.run_id)


if __name__ == '__main__':
    sys.exit(main())
