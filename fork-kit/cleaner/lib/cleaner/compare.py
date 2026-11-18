"""`fork-cleaner compare`: what `fork-cleaner plan` would delete next to what the tools it replaces would delete.

Each source in `cleaner.daily.compare` of the machine file is one old tool's own dry run: a command (run without a
shell, with no input, read-only by its own promise) and a regular expression whose one group is a path that tool
would delete. The compare runs each, runs the cleaner's plan, and sorts every path into:

- new only: the cleaner would delete it and no old tool would. Each one is a bug until somebody explains it, and
  makes the exit code 1.
- old only: an old tool would delete it and the cleaner keeps it (its reason is named): the cleaner is stricter.
- both.

A path counts as the same when it is the other path or inside it (an old tool may name a folder the cleaner names
an entry of, or the other way round). A source that cannot run, or ends with an exit code other than 0, makes
the compare incomplete (exit code 1): what it printed is still used. Nothing is deleted.
"""
import os, re, subprocess

from . import engine

TIMEOUT = 1800                  # seconds one old dry run may take
WOULD = re.compile(r'^would delete (.+?) \((?:[0-9.]+ (?:B|KB|MB|GB)|size not known), (\w+): (.*)\)$')
KEPT = re.compile(r'^kept (.+?) \((\w+): ([^:)]+)(?:: (.*))?\)$')


def _inside(path, others):
    """The first of `others` that `path` is, is inside of, or holds; None when there is none."""
    for other in others:
        if path == other or path.startswith(other.rstrip('/') + '/') or other.startswith(path.rstrip('/') + '/'):
            return other
    return None


def read_plan(lines):
    """({path: (rule, why)} the plan would delete, {path: 'rule: code'} it keeps) from the lines of a plan."""
    would, kept = {}, {}
    for line in lines:
        found = WOULD.match(line)
        if found and found.group(1).startswith('/'):        # a Docker volume is no path an old tool names
            would[found.group(1)] = (found.group(2), found.group(3))
            continue
        found = KEPT.match(line)
        if found and found.group(1).startswith('/'):
            kept[found.group(1)] = f'{found.group(2)}: {found.group(3)}'
    return would, kept


def old_paths(source, run=subprocess.run):
    """(set of paths, problem or None) of one old tool's dry run."""
    try:
        done = run(list(source.command), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                   errors='surrogateescape', timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as ex:
        return set(), f'could not run {source.command[0]}: {ex}'
    paths = set()
    for line in done.stdout.splitlines():
        found = source.pattern.search(line)
        if found and found.group(1) and found.group(1).startswith('/'):
            paths.add(os.path.normpath(found.group(1)))
    if done.returncode != 0:
        first = (done.stderr.strip().splitlines() or [''])[-1][:200]
        return paths, f'exit {done.returncode}' + (f': {first}' if first else '')
    return paths, None


def compare(cfg, facts, say=print, run=subprocess.run, plan_lines=None):
    """Prints the compare; returns the exit code: 0 when every source ran and nothing is new only, 1 otherwise,
    2 when the plan itself refused. `plan_lines`: a plan's lines already made (daily.py), else it plans now."""
    plan_code = 0
    if plan_lines is None:
        plan_lines = []
        plan_code = engine.execute(cfg, facts, False, say=plan_lines.append)
        if plan_code == 2:
            for line in plan_lines:
                say(line)
            return 2
    would, kept = read_plan(plan_lines)
    olds, problems = {}, []
    for source in cfg.sources:
        paths, problem = old_paths(source, run)
        if problem is not None:
            problems.append(f'{source.name}: {problem}')
        for path in paths:
            olds.setdefault(path, []).append(source.name)

    new_only = sorted(path for path in would if _inside(path, olds) is None)
    both = sorted(path for path in would if _inside(path, olds) is not None)
    old_only = sorted(path for path in olds if _inside(path, would) is None)
    say(f'compare: fork-cleaner would delete {len(would)}, the old tools {len(olds)} '
        f'({", ".join(f"{s.name} {sum(s.name in n for n in olds.values())}" for s in cfg.sources) or "no source"})')
    say(f'new only (a bug until explained): {len(new_only)}')
    for path in new_only:
        rule, why = would[path]
        say(f'  {path} ({rule}: {why})')
    say(f'old only (fork-cleaner keeps it): {len(old_only)}')
    for path in old_only:
        reason = kept.get(path) or next((f'{v} (at {k})' for k, v in kept.items() if _inside(path, [k])), None)
        say(f'  {path} [{", ".join(olds[path])}] ({reason or "no kept line: no rule of fork-cleaner takes it, or it is too new for one"})')
    say(f'both: {len(both)}')
    for path in both:
        say(f'  {path}')
    for problem in problems:
        say(f'incomplete: {problem}')
    if plan_code:
        say(f'incomplete: the plan ended with exit code {plan_code} (something could not be read)')
    return 1 if new_only or problems or plan_code else 0
