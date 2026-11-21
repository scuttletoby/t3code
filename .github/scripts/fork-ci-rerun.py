#!/usr/bin/env python3
"""Fork only: one automatic re-run of a failed Fork tests run, only for a known infrastructure sign.

  python3 -I fork-ci-rerun.py sweep [--run-id N]   .github/workflows/fork-ci-rerun.yml (every 10 minutes, GitHub-hosted,
                                                   actions: write, checks: read): each failed trusted run of the last
                                                   catch_up_minutes (or only run N) whose every failed job shows a sign
                                                   gets GitHub's own "re-run failed jobs", once.
  python3 -I fork-ci-rerun.py check                checks CONFIG (fork-ci-rerun.test.py runs it).

THE rules, in one place (the facts, patterns and numbers are CONFIG's, .github/fork-ci-rerun.json; none is here):
- Only a trusted run: of CONFIG's repository and workflow, started and triggered by one of its actors, a push to one
  of its branches or a pull request whose head is this repository. Anything else is left alone.
- Never twice: a run any of whose attempts was triggered by someone else (GitHub's re-run from this script, or the
  fleet sweep's, is triggered by github-actions) is left red. A repeat red goes to an issue and its fix first.
- Each failed job (failure, cancelled, timed out) but CONFIG's followers (jobs that need all the others and fail
  only because one of them did) must show a sign, from its annotations (GitHub's own messages) and its pressure
  note (fork-ci-pressure.py):
    lost         the runner lost communication or was shut down (CONFIG signs.lost): enough on its own;
    memory full  any failure, when the note shows the job's box or machine full (CONFIG memory_full);
    limit        a job or step past its time limit (CONFIG signs.limit), only when the note shows the machine or
                 box stalled (CONFIG stalled).
  One failed job without a sign (a test failure, a person's cancel, no note, a garbled note) leaves the run red:
  fail closed. A run whose branch has moved on, or is gone, is not re-run.
- With CONFIG skip_when (fleet mode), a run that qualifies is only reported, with an error annotation.
Visible and counted: each re-run is a warning annotation with its signs and numbers, a line in this job's summary,
and every pass counts the runs a bot re-ran in the window. Nothing is kept between passes: each reads GitHub, so a
pass that stopped half-way is finished by the next, and one that runs again finds the bot's attempt and does nothing.
Runbook: .github/fork-ci-rerun.md. Standard library only.
"""
import argparse, calendar, http.client, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(os.path.dirname(HERE), 'fork-ci-rerun.json')
API = os.environ.get('GITHUB_API_URL') or 'https://api.github.com'
NOTE_TITLE = 'fork-ci-pressure'          # fork-ci-pressure.py's
NOTE_HEAD = 'fork-ci-pressure v1 '
RED = ('failure', 'cancelled', 'timed_out')
CHECK_RUN = re.compile(r'/check-runs/([0-9]{1,20})$')


def say(msg, out=None):
    print(msg, file=out or sys.stdout, flush=True)


# ---------------------------------------------------------------- config

def load(path=CONFIG):
    with open(path, encoding='utf-8') as f:
        cfg = json.load(f)
    problems = check_config(cfg)
    if problems:
        raise ValueError(f'{path}: ' + '; '.join(problems))
    return cfg


def number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def names(x):
    return isinstance(x, list) and bool(x) and all(isinstance(s, str) and s for s in x)


def check_config(cfg):
    out = []
    if not isinstance(cfg, dict):
        return ['not a JSON object']
    known = {'_doc', 'repository', 'workflow', 'actors', 'events', 'push_branches', 'followers', 'window_hours',
             'catch_up_minutes', 'skip_when', 'signs', 'stalled', 'memory_full'}
    out += [f'unknown key {k!r}' for k in sorted(set(cfg) - known)]
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', str(cfg.get('repository', ''))):
        out.append('repository must be owner/name')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.ya?ml', str(cfg.get('workflow', ''))):
        out.append('workflow must be a workflow file name')
    for key in ('actors', 'events', 'push_branches'):
        if not names(cfg.get(key)):
            out.append(f'{key} must be a list of names')
    if not set(cfg.get('events') or []) <= {'push', 'pull_request'}:
        out.append('events: only push and pull_request')
    if not isinstance(cfg.get('followers'), list) or not all(isinstance(s, str) for s in cfg.get('followers', [])):
        out.append('followers must be a list of job names')
    for key in ('window_hours', 'catch_up_minutes'):
        if not number(cfg.get(key)) or cfg[key] <= 0:
            out.append(f'{key} must be a positive number')
    skip = cfg.get('skip_when')
    if not (isinstance(skip, dict) and re.fullmatch(r'[A-Z][A-Z0-9_]*', str(skip.get('variable', '')))
            and names(skip.get('is'))):
        out.append('skip_when must be {"variable": NAME, "is": [values]}')
    signs = cfg.get('signs')
    if not isinstance(signs, dict) or set(signs) != {'lost', 'limit'}:
        out.append('signs must have exactly lost and limit')
    else:
        for kind, patterns in signs.items():
            if not names(patterns):
                out.append(f'signs.{kind} must be a list of patterns')
                continue
            for p in patterns:
                try:
                    re.compile(p)
                except re.error as e:
                    out.append(f'signs.{kind}: {p!r}: {e}')
    for key, fields in (('stalled', ('full_share_percent', 'full_avg300_percent')),
                        ('memory_full', ('oom_kills', 'max_events', 'high_events_with_full_share_percent'))):
        got = cfg.get(key)
        if not isinstance(got, dict) or set(got) != set(fields) or not all(number(got[f]) and got[f] > 0
                                                                           for f in fields):
            out.append(f'{key} must have exactly {", ".join(fields)}, each a positive number')
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

    def pages(self, path, key, most=10):
        out = []
        for page in range(1, most + 1):
            got = self.call(f'{path}{"&" if "?" in path else "?"}per_page=100&page={page}')[key]
            out += got
            if len(got) < 100:
                return out
        return out


# A truncated reply is http.client.IncompleteRead (an HTTPException), a garbled one a ValueError.
TROUBLE = (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, KeyError, TypeError,
           AttributeError)


def stamp(text):
    try:
        return calendar.timegm(time.strptime(text, '%Y-%m-%dT%H:%M:%SZ'))
    except (TypeError, ValueError):
        return None


def login(x):
    return (x or {}).get('login')


# ---------------------------------------------------------------- the rules

def untrusted(cfg, run):
    """Why GitHub's record of a run is not one this script may re-run, or None."""
    repo = cfg['repository']
    if (run.get('repository') or {}).get('full_name') != repo:
        return f'of another repository ({(run.get("repository") or {}).get("full_name")!r})'
    if os.path.basename((run.get('path') or '').split('@')[0]) != cfg['workflow']:
        return f'another workflow ({run.get("path")!r})'
    if run.get('event') not in cfg['events']:
        return f'event {run.get("event")!r}'
    if login(run.get('actor')) not in cfg['actors']:
        return f'started by {login(run.get("actor"))!r}'
    if run.get('event') == 'push' and run.get('head_branch') not in cfg['push_branches']:
        return f'a push to {run.get("head_branch")!r}'
    if run.get('event') == 'pull_request' and (run.get('head_repository') or {}).get('full_name') != repo:
        return 'a pull request from another repository'
    return None


def note_of(annotations):
    """The job's pressure note (fork-ci-pressure.py) as a dict, or (None, why)."""
    notes = [a for a in annotations if a.get('title') == NOTE_TITLE]
    if not notes:
        return None, 'no pressure note (the job had no probe, or the note was dropped)'
    if len(notes) > 1:
        return None, f'{len(notes)} pressure notes, not one'
    msg = notes[0].get('message') or ''
    if not msg.startswith(NOTE_HEAD):
        return None, 'a garbled pressure note'
    try:
        note = json.loads(msg[len(NOTE_HEAD):])
    except ValueError:
        return None, 'a garbled pressure note'
    if not (isinstance(note, dict) and isinstance(note.get('host'), dict) and isinstance(note.get('cgroups'), list)
            and all(isinstance(c, dict) for c in note['cgroups'])):
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


def memory_full(cfg, note):
    """Evidence that the job's box (or the machine) was full, or None."""
    mf, host = cfg['memory_full'], note['host']
    if at_least(host.get('oom_kill'), mf['oom_kills']):
        return f'the kernel OOM-killed {host["oom_kill"]} process(es) on the machine during the job'
    for c in note['cgroups']:
        where = f'box {c.get("path")} (max {c.get("max_mb")} MB, high {c.get("high_mb")} MB)'
        if at_least(c.get('oom_kill'), mf['oom_kills']):
            return f'{where}: {c["oom_kill"]} OOM kill(s) during the job'
        if at_least(c.get('max'), mf['max_events']):
            return f'{where}: hit its memory.max {c["max"]} time(s) during the job'
        share = max([s for s in (c.get('full_share'), host.get('full_share')) if number(s)], default=None)
        if at_least(c.get('high'), 1) and at_least(share, mf['high_events_with_full_share_percent']):
            return f'{where}: throttled at memory.high {c["high"]} time(s), stalled (full) {share}% of the job'
    return None


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


def matches(patterns, messages):
    for m in messages:
        for p in patterns:
            if re.search(p, m):
                return m
    return None


def job_sign(cfg, annotations):
    """(sign, evidence) when a failed job shows an infrastructure sign, else (None, why not)."""
    messages = [str(a.get('message') or '') for a in annotations if a.get('title') != NOTE_TITLE]
    hit = matches(cfg['signs']['lost'], messages)
    if hit:
        return 'lost', f'GitHub: "{hit.strip()[:200]}"'
    note, why = note_of(annotations)
    full = memory_full(cfg, note) if note else None
    if full:
        return 'memory full', f'{full} ({numbers(note)})'
    hit = matches(cfg['signs']['limit'], messages)
    if hit and note:
        stall = stalled(cfg, note)
        if stall:
            return 'limit while stalled', f'GitHub: "{hit.strip()[:200]}"; {stall} ({numbers(note)})'
        return None, f'ran past its time limit, but the machine was not stalled ({numbers(note)})'
    if hit:
        return None, f'ran past its time limit, and {why}: cannot tell it was stalled'
    return None, 'no infrastructure sign (a failure of its own, or a cancel)' + (f'; {why}' if not note else '')


def annotations_of(gh, job):
    m = CHECK_RUN.search(job.get('check_run_url') or '')
    got = gh.call(f'check-runs/{int(m[1]) if m else int(job["id"])}/annotations?per_page=100')
    if not isinstance(got, list):
        raise ValueError('annotations: not a list')
    return got


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
    why = untrusted(cfg, run)
    if why:
        return 'leave', f'not a trusted run: {why}'
    by = login(run.get('triggering_actor'))
    if by not in cfg['actors']:
        return 'leave', (f'attempt {attempt} was triggered by {by!r} (an automatic re-run): a repeat red stays red'
                         if attempt > 1 else f'triggered by {by!r}: not a trusted run')
    for n in range(attempt - 1, 0, -1):
        by = login(gh.call(f'actions/runs/{rid}/attempts/{n}').get('triggering_actor'))
        if by not in cfg['actors']:
            return 'leave', f'attempt {n} was triggered by {by!r} (an automatic re-run): never twice'
    jobs = gh.pages(f'actions/runs/{rid}/jobs?filter=latest', 'jobs')
    red = [j for j in jobs if j.get('conclusion') in RED and j.get('name') not in cfg['followers']]
    if not red:
        return 'leave', 'no failed job of its own (only followers failed, or nothing did)'
    evidence = []
    for j in red:
        sign, why = job_sign(cfg, annotations_of(gh, j))
        if not sign:
            return 'leave', f'{j.get("name")} ended {j.get("conclusion")}: {why}'
        evidence.append(f'{j.get("name")} ({j.get("conclusion")}): {sign}: {why}')
    if not current(gh, run):
        return 'leave', f'{run.get("head_branch")!r} has moved on or is gone; it qualified: ' + ' | '.join(evidence)
    skip = cfg['skip_when']
    if (env.get(skip['variable']) or '').strip() in skip['is']:
        return 'report', ' | '.join(evidence)
    return 'rerun', ' | '.join(evidence)


def rerun(gh, run):
    """GitHub's own re-run of failed jobs (and the jobs after them), once: (True, how) when this pass asked for it."""
    rid, attempt = int(run['id']), int(run.get('run_attempt') or 1)
    fresh = gh.call(f'actions/runs/{rid}')
    if int(fresh.get('run_attempt') or 1) != attempt or fresh.get('status') != 'completed':
        return False, f'already re-run or running again (attempt {fresh.get("run_attempt")}, {fresh.get("status")})'
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
    if run_id:
        runs = [gh.call(f'actions/runs/{int(run_id)}')]
    else:
        since = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now() - cfg['window_hours'] * 3600))
        runs = gh.pages(f'actions/workflows/{cfg["workflow"]}/runs?created=%3E%3D{since}', 'workflow_runs', most=3)
    recent = now() - cfg['catch_up_minutes'] * 60
    rows, trouble, asked = [], 0, 0
    for run in runs:
        rid = run.get('id')
        try:
            if not run_id and (stamp(run.get('updated_at')) or 0) < recent:
                continue   # ended before the catch-up window: too old to re-run now
            what, text = decide(cfg, gh, run, env)
            where = f'run {rid} ({run.get("event")} {run.get("head_branch")} {str(run.get("head_sha"))[:12]})'
            if what == 'quiet':
                continue
            if what == 'leave':
                say(f'{where}: left red: {text}', out)
                rows.append(f'| {rid} | left red | {cell(text)} |')
                continue
            if what == 'report':
                skip = cfg['skip_when']
                say(f'::error title=fork-ci re-run::{where} qualifies for its one automatic re-run, but '
                    f'{skip["variable"]}={env.get(skip["variable"])}: not re-run; re-run it by hand '
                    f'(.github/fork-ci-rerun.md). {text}', out)
                rows.append(f'| {rid} | not re-run ({skip["variable"]}={cell(env.get(skip["variable"]))}) '
                            f'| {cell(text)} |')
                continue
            done, how = rerun(gh, run)
            if done:
                asked += 1
                say(f'::warning title=fork-ci re-run::{where} re-run once ({how}), infrastructure: {text}', out)
                rows.append(f'| {rid} | **re-run** ({cell(how)}) | {cell(text)} |')
            else:
                say(f'{where}: {how}', out)
                rows.append(f'| {rid} | {cell(how)} | {cell(text)} |')
        except TROUBLE as e:
            trouble += 1
            say(f'::warning::fork-ci re-run: run {rid}: {e.__class__.__name__}: {e}; the next pass tries again', out)
            rows.append(f'| {rid} | not read | {cell(e.__class__.__name__)}: {cell(e)} |')
    bot = [r for r in runs if int(r.get('run_attempt') or 1) > 1 and login(r.get('triggering_actor'))
           not in cfg['actors']]
    count = (f'{asked} run(s) re-run by this pass; {len(bot)} run(s) whose latest attempt was an automatic re-run '
             f'(this script\'s or the fleet sweep\'s) in the last {cfg["window_hours"]} h'
             + (': ' + ', '.join(str(r.get('id')) for r in bot) if bot else ''))
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
    return sweep(cfg, env, GitHub(env.get('GITHUB_TOKEN'), cfg['repository']), run_id=a.run_id)


if __name__ == '__main__':
    sys.exit(main())
