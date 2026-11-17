#!/usr/bin/env python3
"""trace-check: does test-select pick every test whose files change? Runs each test under strace and fails when a
test reads or runs a tracked file whose change would NOT pick that test on a pull request.

test-select works out a test's inputs by reading its code. This check measures them: every test runs once under
`strace -f` (open, exec and stat calls of the test and every process it starts), and every tracked file it touched is
put to test-select as if a pull request changed only that file. A file whose change would leave the test out is a
miss: fix test-select (a general rule) or name the test in the config's `always_run`. Run it where a miss is cheap
to see and fix: on a schedule or after each push to the default branch, never as the gate of a pull request.

  python3 -I trace_check.py --config .github/test-select.json [--run 'python3 -I -B {test}'] [--jobs 2]
      [--only GLOB]... [--root .]
Needs strace on PATH and ptrace of its own children (kernel.yama.ptrace_scope 0 or 1; no root). Without strace, or
when a test's trace does not show its own file read, the check fails (it never passes unchecked). A test's own result does not matter here
(its job runs it); only the files it touched. Not seen: a folder it lists (the files it then reads are), files read
through a process that is not its child (a server already running), and files read through git (git reads .git).
Exit 0: no miss; 1: a miss, or a test that could not be traced.
"""
import argparse, codecs, os, posixpath, re, shlex, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_select as ts   # noqa: E402  (the same release: the rules this check measures)
docs_scope = ts.docs_scope

CALLS = ('open,openat,openat2,creat,execve,execveat,stat,lstat,newfstatat,statx,access,faccessat,faccessat2,'
         'readlink,readlinkat,chdir,fchdir,clone,clone3,fork,vfork')
LINE = re.compile(r'(\d+)\s+(.*)$')
CALL = re.compile(r'(\w+)\((.*)\)\s+=\s+(-?\d+|\?)(.*)$')
STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
FD = re.compile(r'^(\d+|AT_FDCWD)(?:<([^>]*)>)?')
USES = {'open': 'reads', 'openat': 'reads', 'openat2': 'reads', 'creat': 'writes', 'execve': 'runs',
        'execveat': 'runs', 'readlink': 'reads', 'readlinkat': 'reads'}   # the rest: 'checks' (stat, access)


def text(s):
    return codecs.escape_decode(s.encode('latin-1', 'backslashreplace'))[0].decode('utf-8', 'surrogateescape')


def touched(log, root):
    """{absolute path: what (reads/runs/checks)} from one strace -f log; only calls that succeeded."""
    out, cwd, pending, first = {}, {}, {}, None
    for raw in log:
        m = LINE.match(raw.rstrip('\n'))
        if not m:
            continue
        pid, rest = m.groups()
        if first is None:
            first = pid
            cwd[pid] = root
        if rest.endswith('<unfinished ...>'):
            pending[pid] = rest[:-len('<unfinished ...>')]
            continue
        r = re.match(r'<\.\.\. \w+ resumed>(.*)$', rest)
        if r:
            rest = pending.pop(pid, '') + r.group(1)
        c = CALL.match(rest)
        if not c:
            continue
        name, args, ret, tail = c.groups()
        here = cwd.get(pid, root)
        if name in ('clone', 'clone3', 'fork', 'vfork'):
            if ret.isdigit():
                cwd[ret] = here
            continue
        if ret == '?' or int(ret) < 0:
            continue
        if name == 'fchdir':
            fd = FD.match(args)
            if fd and fd.group(2):
                cwd[pid] = fd.group(2)
            continue
        s = STRING.search(args)
        if not s:
            continue
        path, base = text(s.group(1)), here
        if name.endswith('at') or name in ('openat2', 'statx', 'faccessat2'):
            fd = FD.match(args)
            if fd and fd.group(1) != 'AT_FDCWD' and fd.group(2):
                base = fd.group(2)
        path = posixpath.normpath(posixpath.join(base, path))
        if name == 'chdir':
            cwd[pid] = path
            continue
        what = USES.get(name, 'checks')
        if out.get(path) not in ('reads', 'runs'):
            out[path] = what
        fdpath = re.match(r'\s*<([^>]*)>', tail)   # -y: the opened file as the kernel resolved it (links followed)
        if fdpath and name in ('open', 'openat', 'openat2'):
            out.setdefault(fdpath.group(1), what)
    return out


def trace(root, test, run, keep):
    """(touched paths, note or None)."""
    fd, log = tempfile.mkstemp(prefix='trace-', suffix='.log', dir=keep)
    os.close(fd)
    try:
        cmd = ['strace', '-f', '-qq', '-z', '-y', '-s', '4096', '--seccomp-bpf', '-e', 'trace=' + CALLS, '-o', log, '--',
               'sh', '-c', run.replace('{test}', shlex.quote(test))]
        r = subprocess.run(cmd, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=3600)
        with open(log, encoding='latin-1') as f:
            got = touched(f, root)
        if not got:
            return got, f'no trace (strace exit {r.returncode}: {r.stderr.decode(errors="replace").strip()[-300:]})'
        return got, None if r.returncode == 0 else f'the test exited {r.returncode} under strace (its result is not judged here)'
    except subprocess.TimeoutExpired:
        return {}, 'no trace: the test ran past an hour'
    finally:
        os.unlink(log)


def picks(config, rules, always_run, graph, test, rel):
    """Why test-select would pick `test` when only `rel` changed, or None."""
    for g, why in rules:
        if docs_scope.glob_re(g).match(rel):
            return f'every test ({why})'
    if test in always_run:
        return 'always_run'
    files, dirs, globs, marks = graph.inputs(test)
    if marks:
        return 'always'
    return 'takes it in' if ts.reaches([rel], files, dirs, globs) else None


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--config', default=ts.DEFAULT_CONFIG)
    p.add_argument('--root', default='.')
    p.add_argument('--run', default='python3 -I -B {test}', help='how one test runs; {test} is its path')
    p.add_argument('--jobs', type=int, default=2)
    p.add_argument('--only', action='append', default=[], help='trace only the tests matching this glob')
    a = p.parse_args(argv)
    if not shutil.which('strace'):
        print('trace-check: strace is not on PATH; nothing was checked (install strace on the runner)')
        return 1
    root = os.path.realpath(a.root)
    tracked = set(ts.git('ls-files', '-z', cwd=root).split('\0')) - {''}
    tests, always, import_roots, always_run = ts.load_config(root, a.config)
    every = ts.universe(tests, tracked)
    always_run = ts.always_run_of(always_run, every, a.config)
    if a.only:
        every = [t for t in every if any(docs_scope.glob_re(g).match(t) for g in a.only)]
    rules = [(a.config, 'the test-select config')] + list(ts.BUILTIN_ALL) + always
    repo = ts.Repo(root, tracked)
    graph = ts.Graph(repo, import_roots)
    keep = tempfile.mkdtemp(prefix='trace-check-')
    misses, broken = [], []
    try:
        with ThreadPoolExecutor(max(1, a.jobs)) as pool:
            traces = dict(zip(every, pool.map(lambda t: trace(root, t, a.run, keep), every)))
    finally:
        shutil.rmtree(keep, ignore_errors=True)
    for t in every:
        got, note = traces[t]
        if note:
            print(f'{t}: {note}')
            if not got:
                broken.append(t)
                continue
        seen = {}
        for path, what in got.items():
            rel = repo.rel(path) if path.startswith(root + '/') else None
            if rel in tracked and rel not in seen:
                seen[rel] = what
        if t not in seen:   # the run command never ran it: nothing was measured
            print(f'{t}: the trace never shows the test file read (does --run run it?)')
            broken.append(t)
            continue
        for rel, what in sorted(seen.items()):
            if not picks(a.config, rules, always_run, graph, t, rel):
                misses.append((t, rel, what))
        print(f'{t}: {len(seen)} tracked files touched')
    for t, rel, what in misses:
        print(f'miss: {t} {what} {rel}; a pull request that changes only {rel} would not run it')
    print(f'trace-check: {len(every)} tests, {len(misses)} misses, {len(broken)} not traced')
    return 1 if misses or broken else 0


if __name__ == '__main__':
    sys.exit(main())
