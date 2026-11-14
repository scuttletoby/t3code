#!/usr/bin/env python3
"""Can the change BASE..HEAD change what one job of a workflow does?

A change of the workflow file counts for the job only when it can change what the job does: the job's own block, the
block of every job it needs (directly or not: their outputs and results feed it) or the workflow's on, env, defaults
or permissions differ, compared PARSED (PyYAML: comments, blank lines, key order and quoting that keeps the type
('x' -> "x" -> x) never count; aliases are resolved first). A part counts as changed when either of two views differs:
its text (every scalar as written, so 1 -> true or 0755 -> 493 counts) or its types (3.10 -> '3.10' is the number 3.1
-> a string, 1 -> '1' an int -> a string), compared strictly (1 != True != 1.0). Neither view alone is GitHub's
reading; together they lose nothing it sees. name, run-name and concurrency never change what a job builds or tests.
With --paths, a changed file (git diff BASE HEAD, no rename detection: a rename is its old and its new path) that one
of these regular expressions finds (re.search) counts too; the workflow file itself is judged only by the rule above.

Fails closed (changed=true, and the log says why): no BASE or HEAD commit here (first push, force push, a base that is
gone, a shallow checkout), the workflow file is not at HEAD (a wrong path must not mean "never changed"), either side
cannot be parsed in either view (an alias without its anchor, a date like 2026-13-45 or an unknown !tag the typed
view cannot build), no PyYAML on the runner (it installs nothing: python3-yaml is the runner's job), the job or one it
needs is missing on either side (renamed, new, deleted), a top-level key this script does not know changed, a git
error, a bad --paths expression, or any other error. Printed text escapes control characters, so a job or file name
cannot start a workflow command or add an output.

  python3 -I job_changed.py --base SHA [--head HEAD] --workflow .github/workflows/ci.yml --job NAME [--paths 'RE<newline>RE...']
Writes changed=true|false (the workflow part or a path), workflow=true|false (the workflow part alone) and
reason=<one line> to $GITHUB_OUTPUT; exits 0 unless the arguments are wrong.
"""
import argparse, os, re, subprocess, sys

SHARED = ('on', 'env', 'defaults', 'permissions')   # every job runs under these
IGNORED = ('name', 'run-name', 'concurrency')        # change no job's steps, inputs or environment
NO_YAML = 'PyYAML is not installed on this runner (install python3-yaml), the change cannot be read'


def git(*args):
    """stdout, or None when git fails."""
    r = subprocess.run(['git', *args], capture_output=True)
    return r.stdout.decode('utf-8', 'surrogateescape') if r.returncode == 0 else None


def shown(text):
    """One printable line: control characters, line separators and non-UTF-8 bytes escaped."""
    return ''.join(c if c.isprintable() else ascii(c)[1:-1] for c in text)


def load(text, yaml):
    """The text view: the workflow as a dict with every scalar as its text (BaseLoader). Typed values alone would make 1
    and true, yes and true, 0 and false, 0755 and 493 equal (Python: 1 == True), and read the key `on` as True. Aliases
    are still resolved (the composer does that, before any loader)."""
    return workflow(yaml.load(text, Loader=yaml.BaseLoader))


def typed(text, yaml):
    """The typed view: SafeLoader's values (3.10 a float, '3.10' a str, 1 an int, null None) with every mapping key kept
    as its text, so both views have the same keys (`on` stays 'on', not True). The text view alone drops quoting, which
    can change the type the runner sees. A value SafeLoader cannot build (2026-13-45, an unknown !tag) raises."""
    class Typed(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            self.flatten_mapping(node)   # << merge keys, as SafeLoader does
            return {k.value if isinstance(k, yaml.ScalarNode) else self.construct_object(k, deep=deep):
                    self.construct_object(v, deep=deep) for k, v in node.value}
    return workflow(yaml.load(text, Loader=Typed))


def workflow(doc):
    if not isinstance(doc, dict) or not isinstance(doc.get('jobs'), dict):
        raise ValueError('not a workflow (no jobs mapping)')
    return doc


def same(a, b):
    """Equal and of the same type all the way down: 1 != True != 1.0, '3.10' != 3.1 (Python's == says 1 == True)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b or (a != a and b != b)   # .nan != .nan, yet the same text and type


def needed(jobs, job):
    """job and every job it needs, directly or not; raises KeyError for a job that is not there."""
    seen, todo = set(), [job]
    while todo:
        j = todo.pop()
        if j in seen:
            continue
        seen.add(j)
        block = jobs[j]
        wants = block.get('needs', []) if isinstance(block, dict) else []
        todo += [wants] if isinstance(wants, str) else list(wants)
    return seen


def workflow_reason(old_text, new_text, job):
    """Why a change of the workflow file counts for job, or None when it cannot change what job does.
    A text is None when the file is not at that side. Importable: a test-scope or other caller can ask the same."""
    try:
        import yaml
    except ImportError:
        return NO_YAML
    if old_text is None or new_text is None:
        return 'the file is new or deleted'
    try:
        views = [(view(old_text, yaml), view(new_text, yaml)) for view in (load, typed)]
    except (yaml.YAMLError, ValueError, TypeError, OverflowError, RecursionError) as e:   # TypeError: unhashable key
        return f'it cannot be parsed ({(str(e).splitlines() or [type(e).__name__])[0]})'
    (old, new), _ = views

    def differs(get):
        return any(not same(get(o), get(n)) for o, n in views)
    for key in sorted(set(old) | set(new) | set(views[1][0]) | set(views[1][1]), key=str):
        if key == 'jobs' or key in IGNORED or not differs(lambda d: d.get(key)):
            continue
        return f'workflow-level {key} changed' if key in SHARED else f'the top-level key {key!r}, unknown here, changed'
    try:
        names = needed(old['jobs'], job) | needed(new['jobs'], job)
    except (KeyError, TypeError) as e:
        return f'job {e} is missing on one side (renamed, new or deleted) or not a mapping'
    for name in sorted(names, key=str):
        if differs(lambda d: d['jobs'].get(name)):
            return f'job {name}{"" if name == job else f" (which {job} needs)"} changed'
    return None


def changed_paths(a, b, run=git):
    """Paths that differ between commits a and b, renames as delete + add; None on a git error."""
    # --ignore-submodules=none: a .gitmodules `ignore = all` or a runner's diff.ignoreSubmodules hides no gitlink bump
    out = run('diff', '--raw', '--no-renames', '--no-abbrev', '-z', '--ignore-submodules=none', a, b)
    if out is None:
        return None
    parts = out.split('\0')
    if len(parts) % 2 != 1 or any(not m.startswith(':') for m in parts[0:-1:2]):
        return None   # not the format we expect: say "unclear"
    return parts[1:-1:2]


def decide(base, head, wf, job, patterns, run=git):
    """(changed, workflow part changed, log lines); the first line is the reason."""
    def fail(why):
        return True, True, [f'changed: {why}']
    for name, ref in (('base', base), ('head', head)):
        if not ref or ref.startswith('-') or run('rev-parse', '-q', '--verify', f'{ref}^{{commit}}') is None:
            return fail(f'the {name} {ref or "(empty)"!r} is not a commit in this checkout (first push, force push, '
                        f'a shallow checkout?): everything counts')
    wf = os.path.normpath(wf) if wf else ''
    if not wf or wf.startswith(('/', '../')) or wf == '..':
        return fail(f'the workflow path {wf!r} is not a path inside the repository')
    try:
        found = [re.compile(p) for p in patterns]
    except re.error as e:
        return fail(f'a paths expression is not a regular expression ({e})')
    old_id, new_id = (run('rev-parse', '-q', '--verify', f'{ref}:{wf}') for ref in (base, head))
    if new_id is None:
        return fail(f'{wf} is not in {head} (a wrong workflow path would hide every change)')
    lines, why = [], None
    if old_id != new_id:
        why = workflow_reason(run('show', f'{base}:{wf}'), run('show', f'{head}:{wf}'), job)
        if why == NO_YAML:
            lines.append(f'::warning title=job-changed::{NO_YAML}')
    wf_line = (f'{wf} counts for job {job}: {why}' if why else
               f'{wf} is the same at both commits' if old_id == new_id else
               f'{wf} changed, but not job {job}, the jobs it needs or the workflow-level {"/".join(SHARED)}')
    hit = None
    if found and not why:
        paths = changed_paths(base, head, run)
        if paths is None:
            return fail(f'git diff {base} {head} failed: everything counts')
        hit = next(((p, r.pattern) for p in paths if p != wf for r in found if r.search(p)), None)
    if why:
        lines.insert(0, f'changed: {wf_line}')
    elif hit:
        lines += [f'changed: {hit[0]} matches {hit[1]}', wf_line]
    else:
        lines += [f'unchanged: {wf_line}' + ('; no changed file matches the paths' if found else '')]
    return bool(why or hit), bool(why), lines


def main(argv=None, run=git):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--base', default='')
    p.add_argument('--head', default='HEAD')
    p.add_argument('--workflow', required=True, help='the workflow file, e.g. .github/workflows/ci.yml')
    p.add_argument('--job', required=True, help='the job id, e.g. "$GITHUB_JOB"')
    p.add_argument('--paths', help='one regular expression per line (# comments and blank lines skipped)')
    a = p.parse_args(argv)
    try:
        patterns = [s for s in (line.strip() for line in (a.paths or '').splitlines()) if s and not s.startswith('#')]
        changed, wf_changed, lines = decide(a.base, a.head, a.workflow, a.job, patterns, run)
    except Exception as e:   # an error nobody foresaw is "changed" too, never a step without outputs
        changed, wf_changed, lines = True, True, [f'changed: job-changed failed ({type(e).__name__}: {e})']
    lines = [line if line.startswith('::warning title=job-changed::') else shown(line) for line in lines]
    print('\n'.join(lines))
    reason = next(line for line in lines if not line.startswith('::'))
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        with open(out, 'a') as f:
            f.write(f'changed={"true" if changed else "false"}\nworkflow={"true" if wf_changed else "false"}\n'
                    f'reason={reason}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
