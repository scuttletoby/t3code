"""gardener.contract: the check contract, version 1 (fork-kit/gardener/README.md, Checks).

A check is a small program. The runner starts it in the project's root folder (when the project has one), in a
process group of its own, and writes one JSON object to its stdin:

  {"version": 1, "project": {"name": "...", "repo": "OWNER/REPO" or "", "root": "/path" or ""},
   "check": "<id>", "options": {...}, "fix": false, "today": "YYYY-MM-DD", "gh": "gh",
   "cacheDir": "/state/<project>/cache/<id>", "dryRun": false}

cacheDir (added in version 1, so a check reads it with .get()) is a folder only this check of this project uses,
kept between runs, in the private state folder; the check makes it when it needs it. dryRun: the run is a dry run,
and the check writes nothing there.

It prints its findings to stdout, one JSON object per line, and exits 0 (with or without findings):

  {"id": "worktrees.merged", "severity": "warn", "where": "path or thing", "text": "what is wrong",
   "fix": "how to fix it (a hint)"}

`id` (a dotted name: letters, digits, `.`, `_`, `-`, at most 100) and `text` are required, `severity` is one of
error, warn, info, `where` and `fix` are optional strings; no other keys. A check changes nothing unless "fix" is true, and then only what its README says. Empty lines are
ignored; anything else on stdout (a line that is not such an object), an exit code other than 0, more than
MAX_OUTPUT bytes or a run past its timeout makes the whole check one failed-check finding: the runner never trusts
part of a broken check's output. stderr (its last 4 KiB) is kept only for the local report and log, never the issue.

Check programs written in Python can use read_context() and emit() below.
"""
import json, re, sys

VERSION = 1
SEVERITIES = ('error', 'warn', 'info')
KEYS = ('id', 'severity', 'where', 'text', 'fix')
MAX_OUTPUT = 256 << 10      # bytes of stdout; the runner stops reading (and the check) after this
MAX_FINDINGS = 200          # per check; more are folded into one finding
MAX_TEXT = 2000             # per field, in characters
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}')


def parse_finding(line):
    """A finding dict from one stdout line, or raise ValueError saying why it is not one."""
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError('not a JSON object')
    unknown = sorted(set(obj) - set(KEYS))
    if unknown:
        raise ValueError(f'unknown key {unknown[0]!r}')
    for key in ('id', 'text'):
        if not isinstance(obj.get(key), str) or not obj[key].strip():
            raise ValueError(f'"{key}" must be a non-empty string')
    if not ID.fullmatch(obj['id']):
        raise ValueError('"id" must be a dotted name (letters, digits, ".", "_", "-")')
    if obj.get('severity') not in SEVERITIES:
        raise ValueError(f'"severity" must be one of {", ".join(SEVERITIES)}')
    for key in ('where', 'fix'):
        if not isinstance(obj.get(key, ''), str):
            raise ValueError(f'"{key}" must be a string')
    return {k: (obj.get(k) or '')[:MAX_TEXT] for k in KEYS}


def read_context(stream=None):
    """The context the runner wrote to stdin (a dict)."""
    try:
        ctx = json.load(stream or sys.stdin)
    except ValueError:
        ctx = None
    if not isinstance(ctx, dict) or ctx.get('version') != VERSION:
        raise SystemExit(f'gardener check: stdin is not a version {VERSION} gardener context')
    return ctx


def emit(id, severity, text, where='', fix='', out=None):
    """Print one finding line."""
    assert severity in SEVERITIES
    print(json.dumps({'id': id, 'severity': severity, 'where': where, 'text': text, 'fix': fix},
                     ensure_ascii=False), file=out or sys.stdout, flush=True)
