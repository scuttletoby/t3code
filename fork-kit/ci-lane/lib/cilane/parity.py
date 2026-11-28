"""The parity report: the lane's result for each part against the Actions step that runs the same call.

For a finished head the tick reads the Fork tests run of that commit (the plan's workflow) and its jobs: two calls,
at most PER_TICK heads a tick, every RETRY_MINUTES until the run is complete (at most TRIES times). Each lane job is
compared with its step (`check` is the job's name, `step` the step's): passed = success, failed = failure. A job
lost twice, or a step skipped, cancelled or missing, is not compared. The report counts heads and parts, lists
every difference (one the config's `explained` names is explained), and the two runners' times.

Phase 3 waits for GATE_HEADS compared heads with no unexplained difference and the lane not slower than Actions.
"""
import datetime, statistics

from .github import ApiError
from .lane import iso, parse_iso

PER_TICK = 2
RETRY_MINUTES = 15
TRIES = 12
GATE_HEADS = 200
SAME = {('passed', 'success'), ('failed', 'failure')}


def _seconds(a, b):
    a, b = parse_iso(a), parse_iso(b)
    return (b - a).total_seconds() if a and b else None


def actions_side(gh, sha, workflow, jobs):
    """{job key: {conclusion, seconds}} from the newest complete run of the workflow on the commit, or None when
    there is no complete run yet."""
    runs = [r for r in gh.actions_runs(sha) if r.get('path') == workflow and r.get('head_sha') == sha]
    done = [r for r in runs if r.get('status') == 'completed']
    if not done:
        return None
    run = max(done, key=lambda r: (r.get('run_attempt') or 0, r.get('id') or 0))
    by_name = {j.get('name'): j for j in gh.run_jobs(run['id'])}
    side = {}
    for key, job in jobs.items():
        got = by_name.get(job['check'])
        steps = [s for s in (got or {}).get('steps') or [] if isinstance(s, dict) and s.get('name') == job['step']]
        if len(steps) != 1:
            side[key] = {'conclusion': 'missing', 'seconds': None}
            continue
        step = steps[0]
        side[key] = {'conclusion': step.get('conclusion'),
                     'seconds': _seconds(step.get('started_at'), step.get('completed_at'))}
    return {'run': run['id'], 'jobs': side}


def fetch(state, gh, now, log):
    """Fills in the Actions side of up to PER_TICK finished heads. Returns the number of errors."""
    errors, done = 0, 0
    for entry in state.get('history', []):
        if done >= PER_TICK:
            break
        if entry.get('actions') is not None or not entry.get('jobs') or not entry.get('workflow'):
            continue
        last = parse_iso(entry.get('actionsAsked'))
        if last and now - last < datetime.timedelta(minutes=RETRY_MINUTES):
            continue
        done += 1
        entry['actionsAsked'] = iso(now)
        entry['actionsTries'] = entry.get('actionsTries', 0) + 1
        try:
            side = actions_side(gh, entry['sha'], entry['workflow'], entry['jobs'])
        except ApiError as e:
            errors += 1
            log(f'ERROR parity {entry["sha"][:12]}: {e}')
            continue
        if side is not None:
            entry['actions'] = side
        elif entry['actionsTries'] >= TRIES:
            entry['actions'] = 'none'
            log(f'PARITY {entry["sha"][:12]}: no complete Actions run after {TRIES} looks; not compared')
    return errors


def report(state, explained=None):
    explained = explained or {}
    heads, parts, same, differ, own_t, actions_t = 0, 0, 0, [], [], []
    for entry in state.get('history', []):
        side = entry.get('actions')
        if not isinstance(side, dict):
            continue
        compared = 0
        for key, job in sorted(entry['jobs'].items()):
            theirs = side['jobs'].get(key) or {}
            if job['state'] not in ('passed', 'failed') or theirs.get('conclusion') not in ('success', 'failure'):
                continue
            compared += 1
            if (job['state'], theirs['conclusion']) in SAME:
                same += 1
            else:
                differ.append({'sha': entry['sha'], 'what': entry['what'], 'part': key, 'own': job['state'],
                               'actions': theirs['conclusion'],
                               'explained': explained.get(f'{entry["sha"]}:{key}')})
            if isinstance(job.get('seconds'), (int, float)) and isinstance(theirs.get('seconds'), (int, float)):
                own_t.append(job['seconds'])
                actions_t.append(theirs['seconds'])
        if compared:
            heads += 1
            parts += compared
    unexplained = [d for d in differ if not d['explained']]
    own_median = statistics.median(own_t) if own_t else None
    actions_median = statistics.median(actions_t) if actions_t else None
    slower = own_median is None or own_median > actions_median
    return {'heads': heads, 'parts': parts, 'same': same, 'differences': differ, 'unexplained': len(unexplained),
            'ownMedianSeconds': own_median, 'actionsMedianSeconds': actions_median,
            'gate': {'heads': GATE_HEADS, 'ready': heads >= GATE_HEADS and not unexplained and not slower}}


def text(r):
    lines = [f'compared heads: {r["heads"]} (parts: {r["parts"]}), the same: {r["same"]}, '
             f'different: {len(r["differences"])} ({r["unexplained"]} unexplained)']
    if r['ownMedianSeconds'] is not None:
        lines.append(f'median part time: own lane {r["ownMedianSeconds"]:.0f} s, '
                     f'Actions {r["actionsMedianSeconds"]:.0f} s')
    for d in r['differences']:
        lines.append(f'  {d["sha"][:12]} {d["what"]} {d["part"]}: own {d["own"]}, Actions {d["actions"]}'
                     f'{" (explained: " + d["explained"] + ")" if d["explained"] else ""}')
    lines.append(f'phase 3 gate ({r["gate"]["heads"]} heads, no unexplained difference, not slower): '
                 f'{"ready" if r["gate"]["ready"] else "not yet"}')
    return '\n'.join(lines)
