"""`fork-cleaner compare`: what `fork-cleaner plan` would delete next to what the tools it replaces would delete.

Each source in `cleaner.daily.compare` of the machine file is one old tool's own dry run: a command (run without a
shell, with no input, read-only by its own promise) and a regular expression whose one group is a path that tool
would delete. The compare runs each, runs the cleaner's plan, and sorts every path into:

- new only: the cleaner would delete it and no old tool would. Each one is a bug until somebody explains it, and
  makes the exit code 3.
- old only: an old tool would delete it and the cleaner keeps it (its reason is named): the cleaner is stricter.
- both.

A cleaner path counts as "both" only when an old tool names that path or a folder it is in: then the old tool
deletes all of it too. A cleaner path that holds what an old tool names (the old tool deletes /x/cache, the cleaner
all of /x) is "new only", with what the old tool deletes of it: the rest is what only the cleaner deletes. In the
same way an old path that holds a cleaner path is "old only" (the old tool deletes more).

Exit codes: 0 every source ran and nothing is new only; 1 incomplete (a source could not run or ended with an exit
code other than 0, or the plan could not read something: what was printed is still used); 2 the plan refused;
3 complete, and something is new only. Nothing is deleted.
"""
import os, re, subprocess

from . import engine

TIMEOUT = 1800                  # seconds one old dry run may take
NEW_ONLY = 3                    # the exit code when the compare is complete and something is new only
WOULD = re.compile(r'^would delete (.+?) \((?:[0-9.]+ (?:B|KB|MB|GB)|size not known), (\w+): (.*)\)$')
KEPT = re.compile(r'^kept (.+?) \((\w+): ([^:)]+)(?:: (.*))?\)$')


def _within(path, folder):
    """True when `path` is `folder` or inside it."""
    return path == folder or path.startswith(folder.rstrip('/') + '/')


def _inside(path, others):
    """The first of `others` that `path` is or is inside of; None when there is none."""
    return next((other for other in others if _within(path, other)), None)


def _held(path, others):
    """The ones of `others` inside `path` (not `path` itself), sorted."""
    return sorted(other for other in others if other != path and _within(other, path))


def _more(path, others, who):
    """'; deletes more: ...' when `path` holds some of `others` (what `who` deletes of it), else ''."""
    held = _held(path, others)
    if not held:
        return ''
    return f'; deletes more: {who} only {", ".join(held[:3])}{" ..." if len(held) > 3 else ""} in it'


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
    """Prints the compare; returns the exit code (the module's text): 0, 1 incomplete, 2 the plan refused,
    3 new only. `plan_lines`: a plan's lines already made (daily.py), else it plans now."""
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
        say(f'  {path} ({rule}: {why}{_more(path, olds, "the old tools")})')
    say(f'old only (fork-cleaner keeps it): {len(old_only)}')
    for path in old_only:
        reason = kept.get(path) or next((f'{v} (at {k})' for k, v in kept.items() if _inside(path, [k])), None)
        if not reason and _held(path, would):
            reason = _more(path, would, 'fork-cleaner')[2:]
        say(f'  {path} [{", ".join(olds[path])}] ({reason or "no kept line: no rule of fork-cleaner takes it, or it is too new for one"})')
    say(f'both: {len(both)}')
    for path in both:
        say(f'  {path}')
    for problem in problems:
        say(f'incomplete: {problem}')
    if plan_code:
        say(f'incomplete: the plan ended with exit code {plan_code} (something could not be read)')
    return 1 if problems or plan_code else NEW_ONLY if new_only else 0
