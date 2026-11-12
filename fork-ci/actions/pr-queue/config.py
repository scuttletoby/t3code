"""The merge queue's project facts: one JSON file in the project's repository (README.md, "Config").

Strict on purpose: an unknown key (a typo such as `required_check`), a missing required key, an empty
`required_checks` or a value of the wrong type is an error, and the queue then touches no PR. A queue that read a
wrong list of required checks would update, wait for or pass over the wrong PRs, so there are no guessed defaults for
anything a project must decide; only timings have defaults.
"""
import json
from dataclasses import dataclass

MERGE_METHODS = ('merge', 'squash', 'rebase')
# GitHub's CommentAuthorAssociation values that only people with write access to the repository have.
ASSOCIATIONS = ('OWNER', 'MEMBER', 'COLLABORATOR')


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    required_checks: frozenset
    merge_method: str
    delete_branch: bool
    bot_logins: tuple
    override_prefix: str
    override_association: str
    ready_command: str
    push_command: str
    lens_format_doc: str
    head_check: str = None
    wait_seconds: int = 60
    poll_seconds: int = 5
    list_limit: int = 50

    def ready(self, n):
        """The command that re-arms PR n after a lens read, as the project writes it."""
        return self.ready_command.replace('{pr}', str(n))


def _text(v):
    return isinstance(v, str) and v.strip() == v and v != '' and '\n' not in v


def _int(lo, hi):
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


# key: (required, check, what a valid value is)
KEYS = {
    'required_checks': (True, lambda v: isinstance(v, list) and v != [] and all(map(_text, v)) and len(set(v)) == len(v),
                        'a non-empty list of distinct check names (exactly the branch ruleset\'s required checks)'),
    'merge_method': (True, lambda v: v in MERGE_METHODS, f'one of {", ".join(MERGE_METHODS)}'),
    'delete_branch': (True, lambda v: isinstance(v, bool), 'true or false'),
    'bot_logins': (True, lambda v: isinstance(v, list) and all(map(_text, v)),
                   'a list of logins whose comments never count as a lens (may be empty; `*[bot]` always counts as a bot)'),
    'override': (True, lambda v: isinstance(v, dict) and set(v) == {'prefix', 'author_association'}
                 and _text(v['prefix']) and v['author_association'] in ASSOCIATIONS,
                 f'{{"prefix": "<text before the sha>", "author_association": one of {", ".join(ASSOCIATIONS)}}}'),
    'ready_command': (True, lambda v: _text(v) and '{pr}' in v, 'the re-arm command with {pr} for the PR number'),
    'push_command': (True, _text, 'the command that pushes a fixed branch'),
    'lens_format_doc': (True, _text, 'where the project documents its lens comments'),
    'head_check': (False, lambda v: v is None or _text(v), 'null or the required check that checks lens reads'),
    'wait_seconds': (False, _int(5, 600), 'whole seconds, 5-600'),
    'poll_seconds': (False, _int(1, 60), 'whole seconds, 1-60'),
    'list_limit': (False, _int(1, 100), 'how many open PRs the queue reads, 1-100'),
}


def parse(text, where='config'):
    """A Config from the JSON text, or ConfigError naming every problem at once."""
    try:
        raw = json.loads(text)
    except ValueError as e:
        raise ConfigError(f'{where}: not JSON: {e}') from None
    if not isinstance(raw, dict):
        raise ConfigError(f'{where}: must be a JSON object')
    problems = [f'unknown key {k!r} (known: {", ".join(KEYS)})' for k in raw if k not in KEYS]
    for key, (required, ok, what) in KEYS.items():
        if key not in raw:
            if required:
                problems.append(f'{key} is missing: {what}')
        elif not ok(raw[key]):
            problems.append(f'{key} is {json.dumps(raw[key])[:80]}: must be {what}')
    if not problems and raw.get('head_check') and raw['head_check'] not in raw['required_checks']:
        problems.append(f'head_check {raw["head_check"]!r} is not one of required_checks')
    if not problems and raw.get('poll_seconds', 5) > raw.get('wait_seconds', 60):
        problems.append('poll_seconds is longer than wait_seconds')
    if problems:
        raise ConfigError(f'{where}: ' + '; '.join(problems))
    override = raw.pop('override')
    return Config(required_checks=frozenset(raw.pop('required_checks')), bot_logins=tuple(raw.pop('bot_logins')),
                  override_prefix=override['prefix'], override_association=override['author_association'], **raw)


def load(path):
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        raise ConfigError(f'{path}: cannot read it: {e}') from None
    return parse(text, path)
