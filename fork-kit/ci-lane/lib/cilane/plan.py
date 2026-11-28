"""The plan file (.github/fork-ci-plan.json at the commit under test): which parts the lane runs.

Each part is one `fork-ci-tests.cjs ARGS` call of Fork tests (.github/scripts/fork-ci-plan.test.py keeps the two the
same). A part with `lane: true` becomes one job per shard, keyed `<part>:<shard>`; one with `lane: false` stays on
Actions (it says why). Anything this reader does not know refuses the whole plan: a part it misread could pass
without having run what Actions runs.
"""
import json, re

PATH = '.github/fork-ci-plan.json'
VERSION = 1
MAX_PARTS = 20
MAX_SHARDS = 10
MAX_BYTES = 64 * 1024
PART_NAME = re.compile(r'[a-z][a-z0-9-]{0,23}')
ARG = re.compile(r'[A-Za-z0-9_./!-]{1,200}')
KEYS = {'name', 'job', 'check', 'step', 'args', 'shards', 'lane', 'why'}


class PlanError(Exception):
    pass


class Job:
    """One shard of one part: what the lane starts, judges and reports."""

    def __init__(self, part, shard, of, check, step):
        self.part, self.shard, self.of, self.check, self.step = part, shard, of, check, step

    @property
    def key(self):
        return f'{self.part}:{self.shard}'

    @property
    def actions_check(self):
        """The Actions job (check run) name that runs the same shard, for the parity report."""
        return self.check.replace('{shard}', str(self.shard))


class Plan:
    def __init__(self, workflow, jobs, left):
        self.workflow, self.jobs, self.left = workflow, jobs, left


def _text(value, what):
    if not isinstance(value, str) or not value or len(value) > 200:
        raise PlanError(f'{what}: not a short string')
    return value


def parse(text):
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise PlanError(f'{PATH}: larger than {MAX_BYTES} bytes')
    try:
        data = json.loads(text)
    except ValueError as e:
        raise PlanError(f'{PATH}: not JSON ({e})') from None
    if not isinstance(data, dict) or set(data) != {'version', 'workflow', 'parts'}:
        raise PlanError(f'{PATH}: needs exactly version, workflow and parts')
    if data['version'] != VERSION:
        raise PlanError(f'{PATH}: version {data["version"]!r}, this lane reads {VERSION}')
    workflow = _text(data['workflow'], 'workflow')
    parts = data['parts']
    if not isinstance(parts, list) or not 0 < len(parts) <= MAX_PARTS:
        raise PlanError(f'{PATH}: parts must be a list of 1 to {MAX_PARTS}')
    jobs, left, names = [], [], set()
    for i, p in enumerate(parts):
        what = f'{PATH}: part {i + 1}'
        if not isinstance(p, dict) or not {'name', 'job', 'check', 'step', 'args', 'lane'} <= set(p) <= KEYS:
            raise PlanError(f'{what}: needs name, job, check, step, args and lane (and may have shards, why)')
        name = p['name']
        if not isinstance(name, str) or not PART_NAME.fullmatch(name) or name in names:
            raise PlanError(f'{what}: name {name!r} is not [a-z][a-z0-9-]{{0,23}} or is used twice')
        names.add(name)
        for key in ('job', 'check', 'step'):
            _text(p[key], f'{what} {key}')
        args = p['args']
        if not isinstance(args, list) or not args or not all(isinstance(a, str) and ARG.fullmatch(a) for a in args):
            raise PlanError(f'{what}: args must be a list of plain words')
        shards = p.get('shards', 1)
        if type(shards) is not int or not 1 <= shards <= MAX_SHARDS:
            raise PlanError(f'{what}: shards must be 1 to {MAX_SHARDS}')
        if ('{shard}' in p['check']) != (shards > 1):
            raise PlanError(f'{what}: check names {{shard}} exactly when the part has shards')
        if type(p['lane']) is not bool:
            raise PlanError(f'{what}: lane must be true or false')
        if not p['lane']:
            left.append((name, _text(p.get('why'), f'{what} why')))
            continue
        jobs += [Job(name, s, shards, p['check'], p['step']) for s in range(1, shards + 1)]
    return Plan(workflow, jobs, left)
