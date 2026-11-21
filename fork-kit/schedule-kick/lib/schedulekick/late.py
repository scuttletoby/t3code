"""schedule-kick late-check: the first job of a listed workflow, in GitHub Actions. GitHub's `schedule` run for a
slot often comes hours late, after schedule-kick already started that slot by workflow_dispatch; this check tells
such a late run to end early, so a slot runs once (fork-kit/schedule-kick/README.md, Late schedule runs).

On a `schedule` run: slot = the run's cron line (the event's `schedule`) read at the run's own created time, the
slot the run belongs to. A `workflow_dispatch` run of the same workflow on the same branch created at or after that
slot means the slot already ran: `skip=true` goes to $GITHUB_OUTPUT. Anything else (another event, no such run)
gives `skip=false`. A check that cannot decide (no token, an API error, a cron line it does not read) also gives
`skip=false` with a warning: the run goes ahead, as it would without the check.

Reads only: GITHUB_EVENT_NAME, GITHUB_EVENT_PATH, GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_API_URL, GITHUB_OUTPUT
(set by the runner) and SCHEDULE_KICK_TOKEN (the job passes `${{ github.token }}`; it needs `actions: read`).
"""
import json, re

from . import cron as cronmod
from .github import API, TokenApi, iso

REPO_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}\Z')
MAX_EVENT_BYTES = 1024 * 1024


class Undecided(Exception):
    pass


def _time(text):
    from .kick import parse_time
    return parse_time(text)


def decide(env, api):
    """('true' | 'false', why). Raises Undecided (the caller says skip=false) when it cannot tell."""
    if env.get('GITHUB_EVENT_NAME') != 'schedule':
        return 'false', f'a {env.get("GITHUB_EVENT_NAME") or "unknown"} run, not a schedule run'
    repo, run_id = env.get('GITHUB_REPOSITORY', ''), env.get('GITHUB_RUN_ID', '')
    if not REPO_RE.match(repo) or repo.split('/')[1] in ('.', '..') or not re.fullmatch(r'[0-9]{1,20}', run_id):
        raise Undecided('GITHUB_REPOSITORY or GITHUB_RUN_ID is not set right')
    try:
        with open(env.get('GITHUB_EVENT_PATH', ''), 'rb') as f:
            event = json.loads(f.read(MAX_EVENT_BYTES + 1)[:MAX_EVENT_BYTES])
        line = cronmod.Cron(event.get('schedule'))
    except (OSError, ValueError, AttributeError) as e:
        raise Undecided(f'no cron line in the event ({e})') from None
    run = api.run(repo, run_id)
    created, branch, workflow = _time(run.get('created_at')), run.get('head_branch'), run.get('workflow_id')
    if created is None or not isinstance(branch, str) or type(workflow) is not int:
        raise Undecided(f'run {run_id}: no created time, branch or workflow id')
    slot = line.last_slot(created)
    if slot is None:
        raise Undecided(f'{line.line!r} has no slot before this run')
    for row in api.runs_since(repo, workflow, branch, slot, event='workflow_dispatch'):
        when = _time(row['created'])
        if row['event'] == 'workflow_dispatch' and row['branch'] == branch and when is not None and when >= slot:
            return 'true', (f'a workflow_dispatch run on {branch} was created at {iso(when)}, since this run\'s slot '
                            f'{iso(slot)} ({line.line}): this late schedule run ends here')
    return 'false', f'no workflow_dispatch run on {branch} since this run\'s slot {iso(slot)} ({line.line})'


def _command_text(text):
    """`text` as one workflow-command value: no line break can start a command of its own."""
    return text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def main(env, out, err, api_factory=None):
    output = env.get('GITHUB_OUTPUT')
    if not output:
        print('schedule-kick late-check: GITHUB_OUTPUT is not set: run it as a step of a GitHub Actions job',
              file=err)
        return 2
    token = env.get('SCHEDULE_KICK_TOKEN', '')
    try:
        if not token or not re.fullmatch(r'[\x21-\x7e]{1,4096}', token):
            raise Undecided('SCHEDULE_KICK_TOKEN is not set (pass ${{ github.token }}, with actions: read)')
        api = (api_factory or (lambda: TokenApi(token, base=env.get('GITHUB_API_URL') or API)))()
        skip, why = decide(env, api)
    except Exception as e:          # noqa: BLE001 - undecided: the run goes ahead, as without the check
        skip, why = 'false', f'cannot tell, so this run goes ahead: {type(e).__name__}: {e}'
        print(f'::warning title=schedule-kick late-check::{_command_text(why)}', file=out)
    print(f'schedule-kick late-check: skip={skip}: {_command_text(why)}', file=out)
    with open(output, 'a', encoding='utf-8') as f:
        f.write(f'skip={skip}\n')
    return 0
