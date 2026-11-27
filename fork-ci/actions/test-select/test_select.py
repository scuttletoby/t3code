#!/usr/bin/env python3
"""test-select: which test files can this pull request's change reach? The rest need not run on it.

Each run derives every test file's inputs from the checkout (never a cache, so the map cannot go stale):
  - the test file itself;
  - Python: every module it imports that is a file of the repository (sys.path.insert/append and the importing
    file's folder are searched; `importlib` loads by literal path count too), followed to their own inputs;
  - every path it names that the script can work out: string literals, `__file__`, os.path.join/abspath/dirname/
    normpath/realpath, pathlib `/`, Path(...), .parent, .parents[n], .joinpath, f-strings and `+`, through names
    by Python's scope rule (every assignment of the name counts; self.x: every self.x assignment in the file).
    Text with a space, quote, ( or $ is not a path (a message, a shell line). A path that names a file of the
    repository is an input. A folder counts with everything under it only when the file lists it (os.walk,
    os.listdir, glob, .glob/.rglob/.iterdir, shutil.copytree); a folder only named is a base for other paths. A glob
    pattern with a known folder ('docs/*.md') takes in the changed paths it matches; one that starts with a wildcard
    names no place. A path with a known folder and an unknown rest (ROOT / 'plugins' / name) takes in that folder,
    except a folder the file itself is inside (HERE, ROOT, the repository root: the base of nearly every path);
  - shell and JavaScript files it reaches (.sh, .bash, .mjs, .cjs, .js, or such a #! line): every word that names a
    file of the repository, relative to the file or the root, followed the same way; a folder word takes in the
    folder, unless the file is inside it.
  - follow_calls (config): in a large shared module, what a plain top-level function names counts only when the
    function is reached: module-level code, a class, a decorated def, a reached function or another of the test's
    inputs says its name; a name it cannot work out (getattr by a variable, globals(), sys.modules) counts it whole.
A test file runs when a changed path is one of its inputs or lies under one of its input folders. A deleted path
counts as present, so a test that still names a deleted or renamed module or file is chosen (and fails).

Every test runs (fails closed) for: an event other than pull_request (main, merge queue, schedule, manual), a change
that is unknown or empty (docs-scope's rule: HEAD must be GitHub's merge commit), a change to the config, to a
workflow file (.github/workflows/) or to a path the config's `always_all` names (the runner, shared helpers,
fixtures), a Python file in a test's inputs that does not parse, a sys.path entry or importlib.import_module name it
cannot work out, an import that is neither the standard library, nor a repository file, nor clearly third-party (a
module of that name exists somewhere in the repository), and any error this script did not foresee.

A test always runs on a pull request (the log names why) when its own file lists the repository (git ls-files), lists
a folder or builds a path under its own folder or the root that this script cannot work out, or (shell) uses a path
from a variable not set from its own folder; when it reaches, in any file, a module run by a name it cannot work out
(python -m, runpy), a listing of the repository root, a value with more than CAP forms that names a repository path,
or a value deeper than DEPTH steps; and when the config's always_run names it.

What it cannot see: a path that exists only at run time in a module the test reaches (a folder the caller passes, a
name read from data), and a file read by a process the test did not start. A caller without a merge queue
(merge_group) merges such a miss untested; the default branch's next full run shows it. trace_check.py measures the
misses (every test under strace): run it on the default branch and close each one with a rule here or always_run.

  python3 -I test_select.py --config .github/test-select.json --event "$GITHUB_EVENT_NAME" [--changed FILE]
Reads $GITHUB_SHA and $GITHUB_EVENT_PATH (as docs-scope does) unless --changed gives the changed paths (one per line;
for a local check or a measure, never in CI). Prints why, writes the chosen test files (one per line, config order) to
--out (default $RUNNER_TEMP/test-select.txt), and writes all=true|false, count=<n>, total=<n>, list=<file> and
reason=<one line> to $GITHUB_OUTPUT. Exit 1 only when the config cannot be read (no test list: the job goes red).

  python3 -I test_select.py --check-config --runs 'tests/test_*.py' [--runs GLOB]...
Checks that the config's tests are exactly the files the full run runs (this script's matching, each tracked file
once, also with an unmerged index): exit 0 when they are, 1 naming each file when not.
"""
import argparse, ast, json, os, posixpath, re, shlex, subprocess, sys

sys.dont_write_bytecode = True   # leave no __pycache__ in the action folder
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'docs-scope'))
import docs_scope   # noqa: E402  (the same release: the change as docs-scope computes it, and its glob rule)

DEFAULT_CONFIG = '.github/test-select.json'
KEYS = {'tests', 'always_all', 'always_run', 'import_roots', 'inputs', 'follow_calls'}
BUILTIN_ALL = (('.github/workflows/**', 'a workflow file (how the tests run, which release judges them)'),)
SCRIPT_EXT = ('.sh', '.bash', '.mjs', '.cjs', '.js')
LISTING_CALLS = {'os.walk', 'os.listdir', 'os.scandir', 'glob.glob', 'glob.iglob', 'shutil.copytree', 'listdir',
                 'scandir', 'walk', 'glob', 'iglob', 'copytree'}
LISTING_METHODS = {'glob', 'rglob', 'iterdir', 'walk'}   # pathlib.Path's
JOINS = {'join'}
SAME = {'abspath', 'realpath', 'normpath', 'fspath', 'str', 'resolve', 'absolute', 'expanduser', 'Path', 'PurePath',
        'PosixPath', 'PurePosixPath'}
WORD = re.compile(r'[A-Za-z0-9_.@+-]*(?:/[A-Za-z0-9_.@+-]+)+/?|[A-Za-z0-9_@+-][A-Za-z0-9_.@+-]*\.[A-Za-z0-9]+')
PATHLIKE = re.compile(r'[A-Za-z0-9_.@+~=,/*?-]*\Z')   # what a path or a glob looks like (no space, quote, ( or $)
BINDERS = {ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.AugAssign, ast.For, ast.AsyncFor, ast.comprehension,
           ast.withitem, ast.ExceptHandler, ast.arg, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.alias,
           ast.Global}
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef) + COMPREHENSIONS
STDLIB = set(getattr(sys, 'stdlib_module_names', ())) | set(sys.builtin_module_names) | {'__future__'}
CAP, DEPTH = 64, 40   # values per expression, evaluation depth: reaching either runs the test (never a silent drop)
TEMP_CALLS = {'mkdtemp', 'gettempdir', 'TemporaryDirectory', 'mkstemp', 'NamedTemporaryFile', 'gettempdirb'}
GIT_LISTING = re.compile(r'\bgit\b[^\n;|&]*?\b(ls-files|ls-tree|grep)\b')   # a listing of the repository
LIST_COMMANDS = {'find', 'ls', 'tree', 'du', 'grep', 'egrep', 'fgrep', 'rgrep', 'rg', 'fd', 'fdfind'}
RECURSIVE = re.compile(r'-[A-Za-z]*[rR][A-Za-z]*\Z|--(dereference-)?recursive\Z')   # grep -r: a folder
SHELL_LISTING = re.compile(r'(?:^|[;&|(`\n])\s*(?:sudo\s+)?(?:%s)\s' % '|'.join(sorted(LIST_COMMANDS)))
PY_DASH_M = re.compile(r'(?:^|[\s/])python[\d.]*(?:\s+-[A-Za-z]+)*\s+-m\s+([A-Za-z_][\w.]*)')
SCRIPT_DIR = re.compile(r'dirname\b[^\n]*?(\$0\b|\$\{0\}|BASH_SOURCE)|\$\{BASH_SOURCE(?:\[0\])?%/\*\}')
IDENT = re.compile(r'[A-Za-z_]\w*')
# follow_calls: a file that calls one of these, or reads a name it cannot work out off an object, can reach any
# function of a followed module (the module then counts whole for that test).
REFLECT_CALLS = {'globals', 'locals', 'vars', 'eval', 'exec', 'dir', 'getmembers', 'getmembers_static'}
NAME_CALLS = {'getattr', 'setattr', 'hasattr', 'delattr'}
ASSIGN = re.compile(r'^[ \t]*(?:export[ \t]+|local[ \t]+|readonly[ \t]+|declare[ \t]+(?:-\w+[ \t]+)*)?([A-Za-z_]\w*)=(.*)$',
                    re.M)


class AllTests(Exception):
    """Every test runs, for this reason."""


class Partial:
    """A path whose start is known and whose rest is not (ROOT / name). prefix '' = nothing known."""
    __slots__ = ('prefix',)

    def __init__(self, prefix):
        self.prefix = prefix

    def __eq__(self, other):
        return isinstance(other, Partial) and other.prefix == self.prefix

    def __hash__(self):
        return hash(('Partial', self.prefix))


class Tail:
    """A path whose start is unknown and whose rest is known (os.path.join(root, 'docs', 'a.md') with root a parameter,
    an environment variable or x += '/a.md'): it ends with `rest` ('/docs/a.md'; no leading '/' = the rest starts inside
    a name). more: something unknown follows the rest (os.path.join(root, 'docs', name))."""
    __slots__ = ('rest', 'more')

    def __init__(self, rest, more=False):
        self.rest, self.more = rest, more

    def __eq__(self, other):
        return isinstance(other, Tail) and (other.rest, other.more) == (self.rest, self.more)

    def __hash__(self):
        return hash(('Tail', self.rest, self.more))


UNKNOWN = Partial('')
TEMP = Partial('\0temp')    # a temporary folder (tempfile): never a file of the repository
CAPPED = Partial('\0cap')   # a value the caps cut off


# Every git call runs as docs-scope's does (no global or system config, no hooks or fsmonitor of the repository).
GIT_ENV, GIT_SAFE = docs_scope.GIT_ENV, docs_scope.GIT_SAFE


def git(*args, cwd=None):
    r = subprocess.run(['git', *GIT_SAFE, *args], capture_output=True, cwd=cwd, env={**os.environ, **GIT_ENV})
    if r.returncode != 0:
        raise AllTests(f'git {args[0]} failed: {r.stderr.decode(errors="replace").strip()[:200]}')
    return r.stdout.decode('utf-8', 'surrogateescape')


# ---------------------------------------------------------------- the repository as a set of paths

class Repo:
    """The tracked files of the checkout (and the changed paths, so a deleted one still counts as present)."""

    def __init__(self, root, files, changed=()):
        self.root = os.path.abspath(root)
        self.files = set(files) | set(changed)
        self.dirs = {''}
        for f in self.files:
            d = posixpath.dirname(f)
            while d not in self.dirs:
                self.dirs.add(d)
                d = posixpath.dirname(d)
        self.modules = {posixpath.splitext(posixpath.basename(f))[0] for f in self.files if f.endswith('.py')}
        self.modules |= {posixpath.basename(d) for d in self.dirs if d + '/__init__.py' in self.files}
        self.tracked = set(files)   # on disk; a changed path that is not was deleted
        self.links = {f for f in self.files if os.path.islink(os.path.join(self.root, f))}
        self.memo = {}
        # A tracked symlink (to a file or a folder) counts as its target too: link -> the target's path in the
        # repository; target -> the links that reach it (a change to the link itself picks the target's tests).
        self.target, self.reached_by = {}, {}
        for link in sorted(self.links):
            real = os.path.realpath(os.path.join(self.root, link))
            if real.startswith(self.root + os.sep):
                t = real[len(self.root) + 1:].replace(os.sep, '/')
                if t in self.files or t in self.dirs:
                    self.target[link] = t
                    self.reached_by.setdefault(t, set()).add(link)

    def links_to(self, rel):
        """The tracked links whose target is rel or a folder above it."""
        out, d = set(), rel
        while True:
            out |= self.reached_by.get(d, set())
            if not d:
                return out
            d = posixpath.dirname(d)

    def rel(self, path):
        """A repository-relative path for an absolute or root-relative one; None outside the repository."""
        if path not in self.memo:
            self.memo[path] = self._rel(path)
        return self.memo[path]

    def _rel(self, path):
        a = os.path.normpath(path if os.path.isabs(path) else os.path.join(self.root, path))
        if a == self.root:
            return ''
        if not a.startswith(self.root + os.sep):
            return None
        r = a[len(self.root) + 1:].replace(os.sep, '/')
        if r in self.files or r in self.dirs:
            return r
        parts = r.split('/')
        if not any('/'.join(parts[:i]) in self.links for i in range(1, len(parts))):
            return r
        real = os.path.realpath(a)   # through a tracked symlink to a folder
        if real.startswith(self.root + os.sep):
            rr = real[len(self.root) + 1:].replace(os.sep, '/')
            if rr in self.files or rr in self.dirs:
                return rr
        return r

    def kind(self, rel):
        if rel is None:
            return None
        if self.target.get(rel) in self.dirs:
            return 'dir'   # a tracked link to a folder: a package or folder a test reaches through it
        return 'file' if rel in self.files else 'dir' if rel in self.dirs else None


# ---------------------------------------------------------------- Python: imports and the paths a file names

def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        d = dotted(node.value)
        return f'{d}.{node.attr}' if d else node.attr
    return None


def last_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def identifiers(tree):
    """Every name a piece of code says (follow_calls): variables, attributes, imports, keywords, words in strings."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.alias):
            out.update(IDENT.findall(n.name))
            out.add(n.asname or '')
        elif isinstance(n, ast.keyword) and n.arg:
            out.add(n.arg)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.update(IDENT.findall(n.value))
    return out - {''}


def reflection(tree, rel):
    """Why code can reach a function by a name it cannot work out (globals(), getattr(m, name), m.__dict__,
    sys.modules), or None."""
    for n in ast.walk(tree):
        why = None
        if isinstance(n, ast.Call):
            f = last_name(n.func)
            if f in REFLECT_CALLS:
                why = f'{f}()'
            elif f in NAME_CALLS and (len(n.args) < 2 or not (isinstance(n.args[1], ast.Constant) and
                                                             isinstance(n.args[1].value, str))):
                why = f'{f}() by a name it cannot work out'
        elif isinstance(n, ast.Attribute) and n.attr in ('__dict__', 'modules'):
            why = f'.{n.attr}'
        if why:
            return f'{rel}:{getattr(n, "lineno", "?")}: {why}'
    return None


class PyFile:
    """One Python file's imports, sys.path entries and named paths, worked out once per run."""

    def __init__(self, repo, rel, imported=None):
        self.repo, self.rel = repo, rel
        self.resolver = imported   # (this file, level, module, name, depth) -> a repository module's value of name
        self.abs = os.path.join(repo.root, rel)
        self.dir = os.path.dirname(self.abs)
        try:
            with open(self.abs, 'rb') as f:
                src = f.read()
            tree = ast.parse(src, filename=rel)
        except (OSError, SyntaxError, ValueError) as e:
            raise AllTests(f'cannot parse {rel} ({type(e).__name__}: {str(e)[:120]}), a test input')
        self.tree, self.parents, self.memo = tree, {}, {}
        self.always = []   # why every test that reaches this file runs (an input it cannot place)
        self.owner = {}   # (kind, value) -> the top-level functions (None: module-level code) that name it
        self._calls = None
        self.scope, self.up, self.globals = {}, {}, {}   # node -> its scope; scope -> the scope around it
        stack, nodes = [(tree, None)], []
        while stack:   # (no recursion: a long a + b + c chain is deep)
            node, scope = stack.pop()
            nodes.append(node)
            self.scope[node] = scope
            inner = node if isinstance(node, SCOPES) else scope
            if inner is not scope:
                self.up[inner] = scope
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
                stack.append((child, inner))
        # Every binding of a name, per scope: a value node, or None for one it cannot work out (a parameter, a loop
        # variable, x += ...). A name's values are all its bindings' in the scope Python looks it up in.
        self.binds, self.attrs, self.returns = {}, {}, {}
        self.defs = set()   # (scope, name) bindings that are only a def: a call of that name is that function
        for node in nodes:   # def path(self, p): return os.path.join(self.root, p): what a call of it gives
            if isinstance(node, ast.Return) and node.value is not None:
                f = self.scope[node]
                while isinstance(f, COMPREHENSIONS + (ast.Lambda,)):
                    f = self.up[f]
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.returns.setdefault(f.name, []).append(node.value)
        bind = lambda name, where, value: self.binds.setdefault((where, name), []).append(value)   # noqa: E731
        loops = []   # (target, the sequence): bound after the plain assignments, element by element
        for node in nodes:   # global x: x is bound and read in the module
            if isinstance(node, ast.Global):
                self.globals.setdefault(self.scope[node], set()).update(node.names)
        for node in nodes:
            if type(node) not in BINDERS:
                continue
            here = self.scope[node]
            pairs = []
            if isinstance(node, ast.Assign):
                pairs = [(t, node.value) for t in node.targets]
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                pairs = [(node.target, node.value)]
            elif isinstance(node, ast.NamedExpr):
                pairs = [(node.target, node.value)]
            elif isinstance(node, ast.AugAssign):   # x += '/a.json', p /= 'docs': x's values, then the operation
                t = node.target
                if isinstance(node.op, (ast.Add, ast.Div)) and isinstance(t, (ast.Name, ast.Attribute)):
                    left = ast.Name(id=t.id, ctx=ast.Load()) if isinstance(t, ast.Name) else \
                        ast.Attribute(value=t.value, attr=t.attr, ctx=ast.Load())
                    syn = ast.copy_location(ast.BinOp(left=ast.copy_location(left, t), op=node.op, right=node.value),
                                            node)
                    self.scope[syn] = self.scope[left] = self.scope[t]
                    self.parents[left] = syn
                    pairs = [(t, syn)]
                else:
                    pairs = [(t, None)]
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                loops.append((node.target, node.iter))
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                pairs = [(node.optional_vars, node.context_expr)]   # with TemporaryDirectory() as d: a temp folder
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bind(node.name, here, None)
            elif isinstance(node, ast.arg):   # a parameter: unknown (a path joined onto it keeps its known rest: Tail)
                bind(node.arg, here, None)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bind(node.name, here, None)
            elif isinstance(node, ast.alias):   # an import: its value is the name's value in that module
                bind((node.asname or node.name).split('.')[0], here, node)
            for t, v in pairs:
                if isinstance(t, (ast.Tuple, ast.List)) and isinstance(v, (ast.Tuple, ast.List)) and len(t.elts) == len(v.elts):
                    sub = list(zip(t.elts, v.elts))
                else:
                    sub = [(t, v)]
                for tt, vv in sub:
                    if isinstance(tt, ast.Name):
                        where = self.scope[tt]
                        if isinstance(node, ast.NamedExpr):   # := binds in the function around a comprehension
                            while isinstance(where, COMPREHENSIONS):
                                where = self.up[where]
                        if tt.id in self.globals.get(where, ()):
                            where = tree
                        bind(tt.id, where, vv)
                    elif isinstance(tt, ast.Attribute):
                        self.attrs.setdefault(tt.attr, []).append(vv)   # self.x: any assignment in the file
                    else:
                        for n in ast.walk(tt):
                            if isinstance(n, ast.Name):
                                bind(n.id, self.scope[n], None)
        for target, seq in loops:   # for name in ('a.conf', 'b.conf'): name is each of them
            elts = self.elements(seq)
            for n in ast.walk(target):
                if isinstance(n, ast.Name):
                    where = self.scope[n]
                    if elts is None:
                        bind(n.id, where, None)
                        continue
                    for el in elts:
                        bind(n.id, where, self.position(target, n, el))
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    self.binds.get((self.scope[node], node.name)) == [None]:
                self.defs.add((self.scope[node], node.name))
        self.modules = {(a.asname or a.name).split('.')[0] for node in nodes if isinstance(node, ast.Import)
                        for a in node.names}   # names that are a module here (import ast: ast.walk is not a listing)
        self.alias = {}   # import sys as _sys, from os import path as p: names that stand for a module
        for node in nodes:
            if type(node) not in (ast.Import, ast.ImportFrom):
                continue
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.alias[a.asname] = a.name
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                for a in node.names:
                    self.alias[a.asname or a.name] = f'{node.module}.{a.name}'
        self.imports, self.syspath, self.paths, self.dirs_listed = [], set(), set(), set()
        for node in nodes:
            if isinstance(node, ast.Import):
                self.imports += [(0, a.name, None, node.lineno) for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                self.imports.append((node.level, node.module or '', [a.name for a in node.names], node.lineno))
            elif isinstance(node, ast.Call):
                self.call(node)
            elif isinstance(node, (ast.List, ast.Tuple)):
                self.dash_m(node)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) or isinstance(node, ast.JoinedStr):
                self.listing_text(node)
        for node in nodes:
            if isinstance(node, ast.expr) and self.maximal(node):
                for v in self.ev(node):
                    self.own('paths', v, node)

    def own(self, kind, value, node):
        """Add a named path or a listed folder, with the code that names it (follow_calls)."""
        getattr(self, kind).add(value)
        self.owner.setdefault((kind, value), set()).add(self.unit_of(node))

    @staticmethod
    def unit(stmt):
        """A top-level function follow_calls can leave out: a plain def. A class (its body runs on import), a
        decorated def (a decorator may register it), a dunder (__getattr__ runs on any attribute) always count."""
        return isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and not stmt.decorator_list and \
            not (stmt.name.startswith('__') and stmt.name.endswith('__'))

    def unit_of(self, node):
        """The top-level function whose body holds node, or None (code that runs on import, or not placed)."""
        chain = [node]
        while self.parents.get(chain[-1]) is not None and self.parents[chain[-1]] is not self.tree:
            chain.append(self.parents[chain[-1]])
        top = chain[-1]
        if self.parents.get(top) is not self.tree or len(chain) < 2 or not self.unit(top):
            return None
        return top.name if any(chain[-2] is s for s in top.body) else None   # not a default or an annotation

    def calls(self):
        """follow_calls: ({top-level function: the names its body says}, the names the rest of the file says, why
        a name it reaches cannot be worked out or None). A name is any identifier: of a variable, an attribute, an
        import, or a word in a string ('tools.big.f' in mock.patch, a `python -c` line)."""
        if self._calls is None:
            units, top = {}, set()
            for stmt in self.tree.body:
                if self.unit(stmt):
                    body = units.setdefault(stmt.name, set())
                    for s in stmt.body:
                        body |= identifiers(s)
                    for part in (stmt.args, stmt.returns):
                        top |= identifiers(part) if part is not None else set()
                else:
                    top |= identifiers(stmt)
            self._calls = (units, top, reflection(self.tree, self.rel))
        return self._calls

    def module_paths(self, node, name):
        """python -m name, runpy.run_module(name): the files that module can be, from the working directory."""
        for v in self.ev(name) if isinstance(name, ast.AST) else {name}:
            if not isinstance(v, str) or not re.fullmatch(r'[A-Za-z_][\w.]*', v):
                self.mark(node, 'runs a module by a name it cannot work out (python -m, runpy)')   # everywhere
                continue
            base = v.replace('.', '/')
            parts = base.split('/')
            for path in [base + '.py', base + '/__init__.py', base + '/__main__.py'] + [
                    '/'.join(parts[:i]) + '/__init__.py' for i in range(1, len(parts))]:
                self.own('paths', path, node)

    def dash_m(self, node):
        """[sys.executable, '-m', name] (or 'python3'): the module it runs. ['find', d, ...]: a folder it lists."""
        elts = node.elts
        if elts and not isinstance(elts[0], ast.Starred) and isinstance(node.ctx, ast.Load):   # (not: for a, b in)
            first = self.ev(elts[0])
            if any(isinstance(v, str) and posixpath.basename(v) in LIST_COMMANDS for v in first):
                consts = [x.value if isinstance(x, ast.Constant) and isinstance(x.value, str) else None for x in elts]
                args = [None if isinstance(x, ast.Starred) else self.ev(x) for x in elts[1:]]
                self.listing_command(node, posixpath.basename(next(v for v in first if isinstance(v, str) and
                                                                   posixpath.basename(v) in LIST_COMMANDS)),
                                     consts[1:], args, self.call_cwd(node))
        for i, el in enumerate(elts[:-1]):
            if isinstance(el, ast.Constant) and el.value == '-m' and any(
                    self.dotted(x) == 'sys.executable' or isinstance(x, ast.Constant) and isinstance(x.value, str)
                    and re.search(r'(^|/)python[\d.]*\Z', x.value) for x in elts[:i]):
                self.module_paths(node, elts[i + 1])

    def call_cwd(self, node):
        """The cwd= values of the call around node (None: the working directory, the root here)."""
        call = self.parents.get(node)
        while call is not None and not isinstance(call, ast.Call):
            call = self.parents.get(call)
        cwd = [k.value for k in call.keywords if k.arg == 'cwd'] if call is not None else []
        if not cwd:
            return None
        out = set()
        for c in cwd:
            out |= self.ev(c)
        return out

    def listing_command(self, node, cmd, consts, args, cwd):
        """find/ls/tree/du (paths: the arguments before find's first expression), grep -r/rg/fd (paths: after the
        pattern), run as a process: the folders it lists; one it cannot work out runs the test (own)."""
        if cmd in ('grep', 'egrep', 'fgrep') and not any(c and RECURSIVE.match(c) for c in consts):
            return   # grep over named files or stdin: the files are named paths
        paths, skip = [], cmd in ('grep', 'egrep', 'fgrep', 'rgrep', 'rg', 'fd', 'fdfind')
        if skip and any(c in ('-e', '-f', '--regexp', '--file', '--files') for c in consts):
            skip = False   # the pattern is an option's value (rg --files: no pattern)
        after_e = False
        if cmd == 'find':   # find -H/-L/-P/-D x/-Ox before its paths
            while consts and consts[0] is not None and re.fullmatch(r'-[HLP]|-O\d*|-D', consts[0]):
                n = 2 if consts[0] == '-D' else 1
                consts, args = consts[n:], args[n:]
        for c, vals in zip(consts, args):
            if vals is None:
                self.mark(node, f'lists a folder it cannot work out ({cmd} with *args)', own=True)
                return
            if after_e:
                after_e = False
                continue
            if cmd == 'find' and c is not None and (c.startswith('-') or c in ('(', '!', ')')):
                break   # find's expression: the paths come before it
            if c is not None and c.startswith('-') and c != '-':
                if c in ('-e', '-f', '--regexp', '--file', '-g', '--glob', '-t', '--type', '-m', '--max-count',
                         '-A', '-B', '-C', '--include', '--exclude'):
                    after_e = True
                continue
            if skip:
                skip = False
                continue
            paths.append(vals)
        bases = cwd if cwd is not None else {self.repo.root}
        if not paths:
            paths = [{'.'}]
        for vals in paths:
            for v in vals:
                for b in bases:
                    w = join(b, v) if isinstance(v, str) and not os.path.isabs(v) else v
                    if w in (TEMP, CAPPED) or b == TEMP:
                        continue
                    if not isinstance(w, str) and not (isinstance(w, Tail) and w.rest.startswith('/')):
                        self.mark(node, f'lists a folder it cannot work out ({cmd})', own=True)
                        return
                    if isinstance(w, str) and os.path.normpath(w) == self.repo.root:   # (a tool's cwd: its caller's)
                        self.mark(node, f'lists the working directory ({cmd}): it can read any file', own=True)
                        continue
                    self.own('dirs_listed', w, node)

    def shell_listing(self, node, text):
        """A shell line in a string ('find docs -name x', 'grep -rl x docs | sort'): the folders it lists."""
        for seg in re.split(r'[;&|\n`]+|\$\(', text):
            try:
                words = shlex.split(seg)
            except ValueError:
                continue
            while words and (re.fullmatch(r'\w+=\S*', words[0]) or words[0] in ('sudo', 'env', 'exec', 'command')):
                words = words[1:]
            if words and posixpath.basename(words[0]) in LIST_COMMANDS:
                self.listing_command(node, posixpath.basename(words[0]), words[1:],
                                     [{UNKNOWN} if '$' in w or '`' in w else {w} for w in words[1:]],
                                     self.call_cwd(node))

    def listing_text(self, node):
        """A string that lists the repository (git ls-files) or runs a module (python3 -m x). An f-string's shell
        line is read whole, its fields as unknown words."""
        if isinstance(node, ast.JoinedStr):
            v = ''.join(p.value if isinstance(p, ast.Constant) else '${X}' for p in node.values)
        elif isinstance(self.parents.get(node), ast.JoinedStr):
            v = ''
        else:
            v = node.value
        if re.search(r'\s', v) and not self.only_text(node) and SHELL_LISTING.search(v):
            self.shell_listing(node, v)
        if isinstance(node, ast.JoinedStr):
            return
        v = node.value
        for m in PY_DASH_M.finditer(v):
            self.module_paths(node, m[1])
        if v not in ('ls-files', 'ls-tree') and not GIT_LISTING.search(v):
            if v != 'grep' or not self.in_git_call(node):
                return
        if self.only_text(node):
            return
        call = self.parents.get(node)
        while call is not None and not isinstance(call, ast.Call):
            call = self.parents.get(call)
        cwd = [k.value for k in call.keywords if k.arg == 'cwd'] if call is not None else []
        p = self.parents.get(node)
        if isinstance(p, (ast.List, ast.Tuple)):   # ['git', '-C', d, 'ls-files']
            cwd += [b for a, b in zip(p.elts, p.elts[1:]) if isinstance(a, ast.Constant) and a.value == '-C']
        if cwd and all(x == TEMP for c in cwd for x in self.ev(c)):
            return   # a listing of a temporary repository the test made
        self.mark(node, 'lists the repository (git ls-files): it can read any file', own=True)

    def only_text(self, node):
        """A string that is read, never run: a docstring, a side of a comparison, a message an assert checks."""
        p, child = self.parents.get(node), node
        while isinstance(p, (ast.Tuple, ast.List, ast.BinOp, ast.JoinedStr, ast.FormattedValue)):
            p, child = self.parents.get(p), p
        if isinstance(p, ast.Expr) or isinstance(p, ast.Compare):
            return True
        return isinstance(p, ast.Call) and (last_name(p.func) or '').startswith('assert')

    def in_git_call(self, node):
        p = self.parents.get(node)
        return isinstance(p, (ast.List, ast.Tuple)) and any(
            isinstance(x, ast.Constant) and x.value == 'git' for x in p.elts)

    def elements(self, node, depth=0):
        """The element nodes a sequence can hold when it is written out (a tuple, list, set or dict literal, through
        names, sorted()/list()/..., + and .items()/.keys()/.values()); None when they are not all known."""
        if depth > 8:
            return None
        sub = lambda x: self.elements(x, depth + 1)   # noqa: E731
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            out = []
            for el in node.elts:
                more = sub(el.value) if isinstance(el, ast.Starred) else [el]
                if more is None:
                    return None
                out += more
            return out
        if isinstance(node, ast.Dict):
            return None if any(k is None for k in node.keys) else list(node.keys)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            a, b = sub(node.left), sub(node.right)
            return None if a is None or b is None else a + b
        if isinstance(node, ast.Call) and not node.keywords:
            n = last_name(node.func)
            if isinstance(node.func, ast.Name) and n in ('sorted', 'list', 'tuple', 'set', 'frozenset', 'reversed',
                                                         'iter') and len(node.args) == 1:
                return sub(node.args[0])
            if isinstance(node.func, ast.Name) and n == 'enumerate' and len(node.args) == 1:
                got = sub(node.args[0])
                return None if got is None else [ast.Tuple(elts=[ast.Constant(i), el]) for i, el in enumerate(got)]
            if isinstance(node.func, ast.Attribute) and n in ('items', 'keys', 'values') and not node.args:
                dicts = self.literals(node.func.value, ast.Dict, depth)
                if dicts is None or any(k is None for d in dicts for k in d.keys):
                    return None
                pairs = [(k, v) for d in dicts for k, v in zip(d.keys, d.values)]
                return [k for k, _ in pairs] if n == 'keys' else [v for _, v in pairs] if n == 'values' else \
                    [ast.Tuple(elts=[k, v]) for k, v in pairs]
            return None
        if isinstance(node, (ast.Name, ast.Attribute)):
            seqs = self.literals(node, (ast.Tuple, ast.List, ast.Set, ast.Dict), depth)
            if seqs is None:
                return None
            out = []
            for q in seqs:
                more = sub(q)
                if more is None:
                    return None
                out += more
            return out
        return None

    def literals(self, node, types, depth):
        """The literal nodes of these types a name (or self.x) is bound to; None unless every binding is one."""
        if isinstance(node, types):
            return [node]
        if isinstance(node, ast.Name) and node in self.scope:
            key = self.lookup(node)
            values = self.binds.get(key) if key else None
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in ('self', 'cls'):
            values = self.attrs.get(node.attr)
        else:
            return None
        if not values or depth > 8:
            return None
        out = []
        for v in values:
            got = None if v is None else self.literals(v, types, depth + 1)
            if got is None:
                return None
            out += got
        return out

    @staticmethod
    def position(target, name, el):
        """The part of one element a loop target's name takes (for a, b in ((x, y), ...)); None if unknown."""
        if target is name:
            return el
        if isinstance(target, (ast.Tuple, ast.List)):
            if not isinstance(el, (ast.Tuple, ast.List)) or len(el.elts) != len(target.elts) \
                    or any(isinstance(x, ast.Starred) for x in target.elts + el.elts):
                return None
            for t, e in zip(target.elts, el.elts):
                if any(n is name for n in ast.walk(t)):
                    return PyFile.position(t, name, e)
        return None

    def mark(self, node, why, own=False):
        """An input it cannot place. own: it counts only in the test file itself (a tool may list or build paths from
        what its caller passes, mostly temporary folders in a test; the trace check catches a real miss there)."""
        m = ('own' if own else 'always', f'{self.rel}:{getattr(node, "lineno", "?")}: {why}')
        self.always.append(m)
        self.owner.setdefault(('always', m), set()).add(self.unit_of(node))

    # which expressions are whole paths (not a part of a bigger path expression)
    def maximal(self, node):
        p = self.parents.get(node)
        if isinstance(p, ast.BinOp) and isinstance(p.op, (ast.Div, ast.Add)):
            return False
        if isinstance(p, (ast.FormattedValue, ast.JoinedStr)):
            return False
        if isinstance(p, ast.Attribute) and p.value is node and p.attr in ('parent', 'parents'):
            return False
        if isinstance(p, ast.Subscript) and p.value is node:
            return False
        if isinstance(p, ast.Call) and node in p.args:
            n = last_name(p.func)
            if n in JOINS or n in SAME or n == 'dirname':
                return False
        if isinstance(p, ast.Attribute) and p.value is node and p.attr in SAME | {'joinpath'}:
            return False
        if isinstance(p, ast.Starred):
            return False
        if isinstance(node, ast.Constant) and not (isinstance(node.value, str) and re.search(r'[/.]', node.value)):
            return False   # a bare word ('docs', 'main') is not taken for a path; 'docs/x', 'x.json' and '.' are
        if not isinstance(node, (ast.Constant, ast.Name, ast.Attribute, ast.Call, ast.BinOp, ast.JoinedStr,
                                 ast.Subscript, ast.BoolOp, ast.IfExp)):
            return False
        return True

    def dotted(self, node):
        d = dotted(node) or ''
        head, _, rest = d.partition('.')
        return (self.alias[head] + ('.' + rest if rest else '')) if head in self.alias else d

    def call(self, node):
        name, n = self.dotted(node.func), last_name(node.func)
        if name in ('sys.path.insert', 'sys.path.append') and node.args:
            # An entry it cannot work out (a temporary folder) is not searched: an import it then cannot find in the
            # repository still runs every test when a module of that name is in the repository (Graph.module).
            for v in self.ev(node.args[-1]):   # a relative entry: the working directory, the root or the file's
                if isinstance(v, str):
                    for base in {self.dir, self.repo.root}:
                        self.syspath.add(os.path.normpath(os.path.join(base, v)))
        elif name in ('importlib.import_module', 'import_module', '__import__') and node.args:
            for v in self.ev(node.args[0]):
                if not isinstance(v, str):
                    raise AllTests(f'{self.rel}:{node.lineno}: a module imported by a name it cannot work out')
                self.imports.append((0, v, None, node.lineno))
        elif name in ('runpy.run_module', 'run_module') and node.args:
            self.module_paths(node, node.args[0])
        subject = None
        if name in LISTING_CALLS:
            subject = node.args[0] if node.args else ast.Constant('.')   # os.listdir(): the working directory
        elif isinstance(node.func, ast.Attribute) and n in LISTING_METHODS and not (
                isinstance(node.func.value, ast.Name) and node.func.value.id in self.modules):   # ast.walk(tree)
            subject = node.func.value
        if subject is not None:
            for v in self.ev(subject):
                if v == UNKNOWN or isinstance(v, Tail) and not v.rest.startswith('/'):
                    self.mark(node, 'lists a folder it cannot work out', own=True)
                elif v not in (TEMP, CAPPED):
                    self.own('dirs_listed', v, node)

    def imported(self, alias, attr, depth):
        """The value an import gives a name: from m import NAME (attr None), or import m then m.attr; the module-level
        value of that name in the repository module m (every file the import can load); unknown for anything else."""
        imp = self.parents.get(alias)
        if self.resolver is None:
            return {UNKNOWN}
        if isinstance(imp, ast.ImportFrom) and attr is None:
            return self.resolver(self, imp.level, imp.module or '', alias.name, depth)
        if isinstance(imp, ast.Import) and attr is not None and (alias.asname or '.' not in alias.name):
            return self.resolver(self, 0, alias.name, attr, depth)
        return {UNKNOWN}

    def lookup(self, node):
        """The (scope, name) binding a Name reads, by Python's rule (a class body is not seen from its methods)."""
        s = self.scope[node]
        while s is not None:
            if node.id in self.globals.get(s, ()):
                s = self.tree
            if (s, node.id) in self.binds:
                return (s, node.id)
            s = self.up.get(s)
            while isinstance(s, ast.ClassDef):
                s = self.up.get(s)
        return None

    def named(self, key, depth, seen):
        """A binding's values: every value any of its assignments can give (a name reused for many paths has them
        all); key = (scope, name), or an attribute name (self.x) for its assignments anywhere in the file."""
        values = self.returns.get(key[1]) if isinstance(key, tuple) and key[0] == 'return' else \
            self.binds.get(key) if isinstance(key, tuple) else self.attrs.get(key)
        if key in seen or not values:
            return {UNKNOWN}
        memo = (key, seen)
        if memo not in self.memo:
            self.memo[memo] = {UNKNOWN}   # a cycle through other names reads as unknown
            out = set()
            for v in values:
                out |= {UNKNOWN} if v is None else self.imported(v, None, depth) if isinstance(v, ast.alias) else \
                    self.ev(v, depth + 1, seen | {key})
            self.memo[memo] = out
        return self.memo[memo]

    def ev(self, node, depth=0, seen=frozenset()):
        """The set of values node can have: str, or Partial (known start)."""
        if depth > DEPTH:
            self.mark(node, f'a value deeper than {DEPTH} steps')
            return {CAPPED}
        e = lambda x: self.ev(x, depth + 1, seen)   # noqa: E731
        c = lambda a, b, op: self.combine(node, a, b, op)   # noqa: E731
        if isinstance(node, ast.Constant):
            return {node.value} if isinstance(node.value, str) else {UNKNOWN}
        if isinstance(node, ast.Name):
            if node.id == '__file__':
                return {self.abs}
            key = self.lookup(node)
            return self.named(key, depth, seen) if key else {UNKNOWN}
        if isinstance(node, ast.Attribute):
            if node.attr == 'parent':
                return {dirname(v) for v in e(node.value)}
            if isinstance(node.value, ast.Name) and node.value.id in ('self', 'cls'):
                return self.named(node.attr, depth, seen)
            key = self.lookup(node.value) if isinstance(node.value, ast.Name) else None
            values = self.binds.get(key) if key else None
            if values and all(isinstance(v, ast.alias) and isinstance(self.parents.get(v), ast.Import) for v in values):
                out = set()   # import m (as x); m.NAME: NAME's value in m
                for v in values:
                    out |= self.imported(v, node.attr, depth)
                return out
            if e(node.value) == {TEMP}:
                return {TEMP}   # TemporaryDirectory().name
            return {UNKNOWN}
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Attribute) and node.value.attr == 'parents' \
                    and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int):
                out = set()
                for v in e(node.value.value):
                    for _ in range(node.slice.value + 1):
                        v = dirname(v)
                    out.add(v)
                return out
            return {UNKNOWN}
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Div):
                return c(e(node.left), e(node.right), join)
            if isinstance(node.op, ast.Add):
                return c(e(node.left), e(node.right), concat)
            if isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
                return self.formatted(node, re.split(r'(%%|%[-#0 +]*\d*(?:\.\d+)?[a-zA-Z])', node.left.value),
                                      lambda f, i: e(args[i]) if f in ('%s', '%r') and i < len(args) else {UNKNOWN})
            return {UNKNOWN}
        if isinstance(node, ast.JoinedStr):
            out = {''}
            for part in node.values:
                vals = e(part.value) if isinstance(part, ast.FormattedValue) else e(part)
                out = c(out, vals, concat)
            return out
        if isinstance(node, ast.BoolOp):
            out = set()
            for v in node.values:
                out |= e(v)
            return out
        if isinstance(node, ast.IfExp):
            return e(node.body) | e(node.orelse)
        if isinstance(node, ast.Call):
            n = last_name(node.func)
            args = [a for a in node.args]
            if n in TEMP_CALLS:
                return {TEMP}
            if not args and (n in ('getcwd', 'getcwdb') or n == 'cwd' and isinstance(node.func, ast.Attribute)
                             and last_name(node.func.value) in ('Path', 'PurePath', 'PosixPath', 'pathlib')):
                return {self.repo.root}   # the working directory: the root, as for every relative path here
            if self.dotted(node.func) in ('os.environ.get', 'environ.get', 'os.getenv', 'getenv') and len(args) == 2:
                return {UNKNOWN} | e(args[1])   # os.environ.get('ROOT', default): unknown, or the default
            if n in ('enterContext', 'enter_context') and len(args) == 1:
                return e(args[0])   # self.enterContext(tempfile.TemporaryDirectory())
            if isinstance(node.func, ast.Attribute) and n in ('resolve', 'absolute', 'expanduser') and not args:
                return e(node.func.value)
            if isinstance(node.func, ast.Attribute) and n == 'joinpath':
                return self.joined(node, [node.func.value, *args], e)
            if isinstance(node.func, ast.Attribute) and n == 'join' and isinstance(node.func.value, ast.Constant) \
                    and isinstance(node.func.value.value, str) and len(args) == 1:   # '/'.join([ROOT, 'x'])
                elts = self.elements(args[0])
                if elts is None:
                    return {UNKNOWN}
                out, sep = {''}, node.func.value.value
                for i, el in enumerate(elts):
                    out = c(c(out, {sep}, concat) if i else out, e(el), concat)
                return out
            if isinstance(node.func, ast.Attribute) and n == 'format' and isinstance(node.func.value, ast.Constant) \
                    and isinstance(node.func.value.value, str):   # '{}/x'.format(ROOT)
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                parts = re.split(r'(\{\{|\}\}|\{[^{}]*\})', node.func.value.value)
                def field(f, i):
                    name = f[1:-1].split('!')[0].split(':')[0]
                    if name == '':
                        return e(args[i]) if i < len(args) else {UNKNOWN}
                    if name.isdigit():
                        return e(args[int(name)]) if int(name) < len(args) else {UNKNOWN}
                    return e(kw[name]) if name in kw else {UNKNOWN}
                return self.formatted(node, parts, field, '{')
            if n in JOINS and self.dotted(node.func) in ('os.path.join', 'path.join', 'posixpath.join', 'join'):
                return self.joined(node, args, e)
            if n in ('Path', 'PurePath', 'PosixPath', 'PurePosixPath'):
                return self.joined(node, args, e) if args else {UNKNOWN}
            if n in ('abspath', 'realpath', 'normpath', 'fspath', 'str') and len(args) == 1:
                return e(args[0])
            if n == 'dirname' and len(args) == 1:
                return {dirname(v) for v in e(args[0])}
            if isinstance(node.func, ast.Name) and self.lookup(node.func):   # p = lambda *a: os.path.join(T, *a)
                key = self.lookup(node.func)
                values = self.binds.get(key)
                if values and key not in seen and all(isinstance(v, ast.Lambda) for v in values):
                    out = set()
                    for v in values:
                        out |= self.ev(v.body, depth + 1, seen | {key})
                    return out
            if n in self.returns and (isinstance(node.func, ast.Name) and self.lookup(node.func) in self.defs
                                      or isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                                      and node.func.value.id in ('self', 'cls')):
                # a function of this file (any def of that name): what its returns give, its parameters unknown
                return self.named(('return', n), depth, seen)
            return {UNKNOWN}
        return {UNKNOWN}

    def formatted(self, node, parts, field, opener='%'):
        """A %- or {}-formatted string: its literal parts and its fields' values, joined."""
        out, i = {''}, 0
        for k, part in enumerate(parts):
            if k % 2 == 0:
                vals = {part}
            elif part in ('%%', '{{', '}}'):
                vals = {part[0]}
            elif part.startswith(opener):
                vals, i = field(part, i), i + 1
            else:
                vals = {part}
            out = self.combine(node, out, vals, concat)
        return out

    def combine(self, node, left, right, op):
        out = set()
        for a in left:
            for b in right:
                out.add(op(a, b))
        if len(out) > CAP:   # too many to list: the start they all share (a folder: everything under it), never a drop
            starts = [v.prefix if isinstance(v, Partial) else '\0tail' if isinstance(v, Tail) else v for v in out]
            common = os.path.commonprefix(starts)
            if '/' in common and not any('\0' in x for x in starts):
                return {Partial(common)}
            if any(self.names_repo(x) for x in starts) or any(isinstance(v, Tail) and re.search(
                    r'/[\w@+-]', v.rest) for v in out):   # no folder they share, and one is a path here: always
                self.mark(node, f'more than {CAP} values')
                return {CAPPED}
            return {UNKNOWN}   # text, not a path of the repository ('k=v' lines of a settings file)
        return out

    def names_repo(self, text):
        """Whether a value names a file or folder of the repository (from this file's folder, the root, or absolute)."""
        if not text or '\0' in text:
            return False
        for c in [text] if os.path.isabs(text) else [os.path.join(self.dir, text), os.path.join(self.repo.root, text)]:
            rel = self.repo.rel(os.path.normpath(c))
            if rel is not None and rel != '' and self.repo.kind(rel):
                return True
        return False

    def joined(self, node, args, e):
        if not args:
            return {UNKNOWN}
        out = None
        for a in args:
            vals = {UNKNOWN} if isinstance(a, ast.Starred) else e(a)
            out = vals if out is None else self.combine(node, out, vals, join)
        return out


def dirname(v):
    if isinstance(v, Tail):   # the folder of <unknown>/docs/a.md is <unknown>/docs; of <unknown>/docs/<name>: unknown
        rest = v.rest.rstrip('/')
        return Tail(rest.rsplit('/', 1)[0]) if not v.more and '/' in rest[1:] else UNKNOWN
    if isinstance(v, Partial):
        return Partial(v.prefix.rsplit('/', 1)[0] + '/') if '/' in v.prefix else UNKNOWN
    return os.path.dirname(v.rstrip('/') or v)


def tail_of(b, sep):
    """What an unknown start followed by b (after sep: '/' for a join, '' for +) is."""
    if isinstance(b, str):
        return Tail(sep + b) if b else UNKNOWN
    if isinstance(b, Tail):
        return b   # it ends with b's rest, whatever comes before
    if b == UNKNOWN or '\0' in b.prefix:
        return b if b != UNKNOWN else UNKNOWN
    return Tail(sep + b.prefix, True)   # a known middle, an unknown end


def join(a, b):
    """os.path.join and pathlib's / on values: an absolute right side wins; after an unknown start the known rest is
    kept (Tail); after a known start and an unknown part the rest is unknown (Partial)."""
    if b in (TEMP, CAPPED) or a == CAPPED:
        return b if b in (TEMP, CAPPED) else a
    if isinstance(b, str) and b.startswith('/'):
        return b
    if isinstance(b, Partial) and b.prefix.startswith('/'):
        return b
    if a == UNKNOWN:
        return tail_of(b, '/')
    if isinstance(a, Tail):
        if isinstance(b, Tail) or (a.more and b != UNKNOWN):
            return tail_of(b, '/')
        if a.more or b == UNKNOWN:
            return Tail(a.rest.rstrip('/') + '/', True)
        if isinstance(b, Partial):
            return Tail(posixpath.join(a.rest, b.prefix), True)
        return Tail(posixpath.join(a.rest, b))
    if isinstance(a, Partial):
        return a
    if isinstance(b, Tail):   # a known folder, then a name that starts unknown ('{name}.py'): that folder holds
        return b if b.rest.startswith('/') or not a else Partial(os.path.join(a, ''))
    if isinstance(b, Partial):
        return Partial(os.path.join(a, b.prefix) if b.prefix else os.path.join(a, ''))
    return os.path.join(a, b)


def concat(a, b):
    if CAPPED in (a, b):
        return CAPPED
    if a == UNKNOWN:
        return tail_of(b, '')
    if isinstance(a, Tail):
        if isinstance(b, Tail) or (a.more and b != UNKNOWN):
            return tail_of(b, '')
        if a.more or b == UNKNOWN:
            return Tail(a.rest, True)
        if isinstance(b, Partial):
            return Tail(a.rest + b.prefix, True) if '\0' not in b.prefix else b
        return Tail(a.rest + b)
    if isinstance(a, Partial):
        return a
    if isinstance(b, Tail):   # 'docs/' + f'{name}.md': the known start holds
        return b if b.rest.startswith('/') or not a else Partial(a)
    if isinstance(b, Partial):
        return Partial(a + b.prefix)
    return a + b


# ---------------------------------------------------------------- inputs of a test file

class Graph:
    def __init__(self, repo, import_roots=(), declared=None, follow=()):
        self.repo = repo
        self.declared = declared or {}   # {test file: (globs it reads, why)}: the config's inputs
        self.follow = set(follow)   # the config's follow_calls: modules whose functions count only when reached
        self.import_roots = [os.path.join(repo.root, r) for r in import_roots]
        self.py, self.text, self.text_always, self.memo = {}, {}, {}, {}
        self.building = set()

    def pyfile(self, rel):
        if rel not in self.py:
            self.building.add(rel)
            try:
                self.py[rel] = PyFile(self.repo, rel, self.imported)
            finally:
                self.building.discard(rel)
        return self.py[rel]

    def imported(self, pf, level, module, name, depth):
        """`name`'s module-level value in the repository module an import of pf loads (from m import NAME, m.NAME):
        the union over every file the import can load; unknown outside the repository or while that file is itself
        being read (an import cycle)."""
        parts = [p for p in module.split('.') if p]
        out = set()
        for f in self.module(level, module, None, pf, pf.syspath) or []:
            base = posixpath.basename(f)
            if (base == '__init__.py') != (not parts or posixpath.basename(posixpath.dirname(f)) == parts[-1]) or \
                    parts and base not in ('__init__.py', parts[-1] + '.py'):
                continue   # a package above the module (a/__init__.py for a.b)
            if f in self.building:
                out.add(UNKNOWN)
                continue
            other = self.pyfile(f)
            key = (other.tree, name)
            out |= other.named(key, depth + 1, frozenset()) if key in other.binds else {UNKNOWN}
        return out or {UNKNOWN}

    def kind_of(self, rel):
        if ('kind', rel) not in self.memo:
            self.memo[('kind', rel)] = self._kind_of(rel)
        return self.memo[('kind', rel)]

    def _kind_of(self, rel):
        if rel.endswith('.py'):
            return 'py'
        if rel.endswith(SCRIPT_EXT):
            return 'text'
        try:
            with open(os.path.join(self.repo.root, rel), 'rb') as f:
                first = f.readline(200)
        except OSError:
            return None
        if first.startswith(b'#!'):
            return 'py' if b'python' in first else 'text' if re.search(rb'\b(ba|da|z)?sh\b|\bnode\b', first) else None
        return None

    def resolve(self, value, base_dir, owner, listed=False, text=False):
        """[(kind, rel)] inputs for one named value: ('file', path), ('dir', folder: anything under it) or ('glob',
        pattern from the root). owner = the repository file that names it, base_dir its folder (a relative value is
        tried against it and against the repository root, the usual working directory)."""
        repo, out = self.repo, []
        if isinstance(value, Tail):
            # A path whose start it cannot work out (a parameter, Path.cwd() of a tool, an environment variable, x +=
            # '/a.json', a loop joining part by part) and whose rest is known: any place that ends with that rest
            # (and anything under it when it lists or copies it). A rest without a folder step ('{name}.json') or
            # with nothing named is not taken for a path.
            rest = value.rest
            if not rest.startswith('/') or not PATHLIKE.match(rest.lstrip('/') or '-'):
                return out
            folder, rest = rest.endswith('/'), posixpath.normpath(rest)
            if not re.search(r'[\w@+-]', rest):
                return out
            pat = '**' + rest.rstrip('/') + ('/*' if value.more and folder else '*' if value.more else '')
            return [('glob', pat), ('glob', pat + '/**')] if listed else [('glob', pat)]   # a folder: a base
        if not PATHLIKE.match(value.prefix if isinstance(value, Partial) else value):
            return out   # a message, a shell line, a regular expression: not a path
        owner_dir = posixpath.dirname(owner)
        inside = lambda rel: rel == '' or owner_dir == rel or owner_dir.startswith(rel + '/')   # noqa: E731
        if isinstance(value, str) and re.search(r'[*?]', value):
            # A glob pattern with a folder ('docs/*.md', ROOT / 'x/**'): the changed paths it matches. A pattern that
            # starts with a wildcard ('job-*.service', '**/*.json') names no place: it counts only through the folder
            # it lists.
            value, rest = re.split(r'(?=[^/]*[*?])', value, maxsplit=1)[0], value
            rest = rest[len(value):]
            if not value:
                if listed:   # glob('*.py'): from the working directory (the root, as everywhere here)
                    out.append(('glob', rest))
                return out
            for c in [value] if os.path.isabs(value) else [os.path.join(base_dir, value), os.path.join(repo.root, value)]:
                rel = repo.rel(c)
                if repo.kind(rel) == 'dir':
                    out.append(('glob', f'{rel}/{rest}' if rel else rest))
            return out
        if isinstance(value, Partial):
            # A path with a known folder and a rest worked out at run time (ROOT / 'Plugins' / name): anything in that
            # folder. A folder the file itself is inside (HERE / name, ROOT / name: data or a loop variable picks the
            # file) cannot be placed without taking in its whole tree: in a test file, that test always runs; in a
            # tool it reaches, it is left to the trace check (a tool builds most such paths from what its caller
            # passes: a temporary folder in a test).
            pre = value.prefix
            if '/' not in pre:
                return out   # nothing known, or no folder known ('Error: {e}')
            for c in [pre] if os.path.isabs(pre) else [os.path.join(base_dir, pre), os.path.join(repo.root, pre)]:
                rel = repo.rel(c if c.endswith('/') else os.path.dirname(c))
                if repo.kind(rel) == 'dir':
                    out.append(('dir', rel) if not inside(rel) else
                               ('own', f'{owner}: a path under {rel or "the repository root"}/ it cannot work out '
                                          f'({docs_scope.shown(pre[len(repo.root) + 1:] if pre.startswith(repo.root + "/") else pre)}...)'))
            return out
        if not value or '\0' in value or not (os.path.isabs(value) or re.search(r'[/.]', value)):
            return out
        for c in [value] if os.path.isabs(value) else [os.path.join(base_dir, value), os.path.join(repo.root, value)]:
            rel = repo.rel(c)
            k = repo.kind(rel)
            if k == 'file':
                out.append(('file', rel))
            elif k == 'dir' and listed and rel == '':
                out.append(('always', f'{owner}: lists the repository root: it can read any file'))
            elif k == 'dir':
                # Python: a folder named but not listed is a base (files in it count when named); a shell or
                # JavaScript file's folder word (cp -r dir, rsync dir/) takes all of it, but not its own folders.
                if listed or (text and not inside(rel)):
                    out.append(('dir', rel))
        return out

    def module(self, level, name, names, pf, search):
        """Repository files an import loads: [] for a module outside the repository, None when not found here."""
        key = ('module', level, name, tuple(names or ()), pf.rel, frozenset(search))
        if key not in self.memo:
            self.memo[key] = self._module(level, name, names, pf, search)
        return self.memo[key]

    def _module(self, level, name, names, pf, search):
        repo = self.repo
        if level:
            base = pf.dir
            for _ in range(level - 1):
                base = os.path.dirname(base)
            roots = [base]
        else:   # Python takes the first match in sys.path order; that order is not worked out, so every match counts
            roots = [pf.dir, *sorted(search), *self.import_roots]
        parts = [p for p in name.split('.') if p]
        found = None
        for r in roots:
            here, d = [], r
            for i, p in enumerate(parts):
                d = os.path.join(d, p)
                init, mod = repo.rel(os.path.join(d, '__init__.py')), repo.rel(d + '.py')
                if repo.kind(init) == 'file':
                    here.append(init)
                elif i == len(parts) - 1 and repo.kind(mod) == 'file':
                    here.append(mod)
                elif repo.kind(repo.rel(d)) != 'dir':   # (a folder without __init__.py: a namespace package)
                    here = None
                    break
            if here is None or (not parts and not level):
                continue
            if level and not parts:   # from . import x: the package's __init__.py runs first
                init = repo.rel(os.path.join(d, '__init__.py'))
                if repo.kind(init) == 'file':
                    here.append(init)
            for n in names or ():   # from package import module
                for sub in (os.path.join(d, n + '.py'), os.path.join(d, n, '__init__.py')):
                    if repo.kind(repo.rel(sub)) == 'file':
                        here.append(repo.rel(sub))
            if here or repo.kind(repo.rel(d)) == 'dir':
                found = (found or []) + [h for h in here if h not in (found or [])]
        if found is not None:
            return found
        if level:
            return None
        top = parts[0] if parts else ''
        if top in STDLIB or top not in repo.modules:
            return []   # the standard library, or third-party: no file of the repository
        return None

    def named_inputs(self, rel, kind, units=None):
        """The inputs one file names (paths, listed folders, words), resolved once per run. units (follow_calls):
        only what module-level code and these top-level functions name; None: the whole file."""
        key = ('named', rel, units)
        if key not in self.memo:
            if kind == 'py':
                pf = self.pyfile(rel)
                keep = lambda k, v: units is None or any(   # noqa: E731
                    u is None or u in units for u in pf.owner.get((k, v), (None,)))
                named = [(v, False) for v in pf.paths if keep('paths', v)] + \
                    [(v, True) for v in pf.dirs_listed if keep('dirs_listed', v)]
                base, out = pf.dir, {m for m in pf.always if keep('always', m)}
            else:
                named = [(w, False) for w in self.words(rel)]
                base = os.path.join(self.repo.root, posixpath.dirname(rel))
                out = set(self.text_always.get(rel, ()))
            for value, listed in named:
                out.update(self.resolve(value, base, rel, listed, kind == 'text'))
            self.memo[key] = sorted(out)
        return self.memo[key]

    def inputs(self, test):
        """(files, folders, globs, always-marks) that are the test file's inputs; AllTests when one runs every test.
        Worked out once per test."""
        key = ('inputs', test)
        if key not in self.memo:
            try:
                self.memo[key] = self._inputs(test)
            except AllTests as e:
                self.memo[key] = e
        if isinstance(self.memo[key], AllTests):
            raise self.memo[key]
        return self.memo[key]

    def reached(self, module, closure):
        """follow_calls: the top-level functions of `module` this test's other files can reach (frozenset), or None
        (the whole module: a name it cannot work out). A function is reached when module-level code (or a class, a
        decorated def) or a reached function says its name, or any other file of the test's inputs says it (in
        code, a string or a script's text); never by a call graph that could miss a way in."""
        units, said, why = self.pyfile(module).calls()
        if why:
            return None
        said = set(said)
        for rel in sorted(closure):
            if rel == module or rel not in self.repo.tracked:
                continue
            kind = self.kind_of(rel)
            if kind == 'py':
                bodies, top, why = self.pyfile(rel).calls()
                if why:
                    return None
                said |= top
                for body in bodies.values():
                    said |= body
            elif kind == 'text':
                said |= set(IDENT.findall(self.words_text(rel)))
        out, todo = set(), [n for n in said if n in units]
        while todo:
            n = todo.pop()
            if n not in out:
                out.add(n)
                todo += [m for m in units[n] if m in units]
        return frozenset(out)

    def words_text(self, rel):
        key = ('text', rel)
        if key not in self.memo:
            try:
                with open(os.path.join(self.repo.root, rel), 'rb') as f:
                    self.memo[key] = f.read().decode('utf-8', 'surrogateescape')
            except OSError:
                self.memo[key] = ''
        return self.memo[key]

    def _inputs(self, test):
        files, dirs, globs, todo, search, done, unfound = {test}, set(), set(), [test], set(), set(), []
        always, deferred, applied = set(), set(), {}

        def take(rel, named):
            for k, r in named:
                if k == 'file':
                    files.add(r)
                    todo.append(r)
                elif k == 'own' and rel == test and test in self.declared:
                    continue   # the config declares what this test reads (inputs): its globs stand for this
                elif k == 'always' or k == 'own' and rel == test:
                    always.add(r)
                elif k == 'own':
                    continue
                else:
                    (dirs if k == 'dir' else globs).add(r)
        while todo:
            while todo:
                rel = todo.pop()
                if rel in done:
                    continue
                done.add(rel)
                if rel not in self.repo.tracked:
                    continue   # deleted by the change: an input with nothing in it to follow
                kind = self.kind_of(rel)
                if kind == 'py':
                    pf = self.pyfile(rel)
                    new = pf.syspath - search
                    if new:   # sys.path is one list for the whole process: every file is looked at again with it
                        search |= new
                        todo += sorted(done)
                        done.clear()
                        unfound.clear()
                        deferred.clear()
                        applied.clear()
                        continue
                    for level, name, names, line in pf.imports:
                        got = self.module(level, name, names, pf, search)
                        if got is None:
                            unfound.append(f'{rel}:{line}: import {"." * level}{name}')
                            continue
                        for m in got:
                            files.add(m)
                            todo.append(m)
                    if rel in self.follow and rel != test:
                        deferred.add(rel)   # what it names: once the files that can call it are known (below)
                        continue
                elif kind != 'text':
                    continue
                take(rel, self.named_inputs(rel, kind))
            # follow_calls: what a followed module's reached functions name; that may bring in files that reach more
            for rel in sorted(deferred):
                units = self.reached(rel, done)
                if applied.get(rel, False) != units:
                    applied[rel] = units
                    take(rel, self.named_inputs(rel, 'py', units))
        globs.update(self.declared.get(test, ((), ''))[0])
        if unfound:
            raise AllTests(f'{unfound[0]}: a module of that name is in the repository, but not where this import '
                           f'looks (sys.path entries it can work out, the file\'s folder)')
        # A tracked symlink counts as its target, and a target as the links that reach it.
        for f in sorted(files | dirs):
            if f in self.repo.target:
                files.add(f)   # a change to the link itself
                (files if self.repo.target[f] in self.repo.files else dirs).add(self.repo.target[f])
        for f in sorted(files | dirs):
            files |= self.repo.links_to(f)
        return files, dirs, globs, sorted(always)

    def words(self, rel):
        if rel not in self.text:
            try:
                with open(os.path.join(self.repo.root, rel), encoding='utf-8', errors='replace') as f:
                    src = f.read()
            except OSError as e:
                raise AllTests(f'cannot read {rel} ({e}), a test input')
            src, words, always = self.variable_words(rel, src)
            for m in PY_DASH_M.finditer(src):
                base = m[1].replace('.', '/')
                words |= {base + '.py', base + '/__init__.py', base + '/__main__.py'}
            if GIT_LISTING.search(src):
                always.add(('own', f'{rel}: lists the repository (git ls-files): it can read any file'))
            self.text[rel] = words | {w.lstrip('/') if w.startswith('/..') or w.startswith('/.') else w
                                      for w in WORD.findall(src)}
            self.text_always[rel] = always
        return self.text[rel]

    def variable_words(self, rel, src):
        """Words that start with a variable or $(...): "$HERE/lib.sh", "${DIR}/x", "$(dirname "$0")/x". A variable
        set from the script's own folder ($(dirname "$0"), BASH_SOURCE, readlink -f, cd ... && pwd; JavaScript's
        __dirname) or from such a variable plus a path resolves; any other one before a path that names a file of the
        repository (from the script's folder or the root) runs every test that reaches this file. Returns the text
        without those words, the resolved paths (absolute) and the reasons."""
        here = os.path.join(self.repo.root, posixpath.dirname(rel))
        known = {'__dirname': {here}}
        for _ in range(4):   # a variable built from another one
            for m in ASSIGN.finditer(src):
                name, rhs = m[1], m[2].strip().strip('"\'')
                got = set()
                if re.search(r'rev-parse\s+--show-toplevel', rhs):
                    got = {self.repo.root}
                elif SCRIPT_DIR.search(rhs):
                    after = re.search(r'(?:dirname[^)]*\)|%/\*\})"?((?:/[A-Za-z0-9_.@+-]+)*)', rhs)
                    got = {os.path.normpath(here + (after[1] if after else ''))}
                else:
                    v = re.fullmatch(r'\$\{?(\w+)\}?((?:/[A-Za-z0-9_.@+-]+)*)/?', rhs)
                    if v and v[1] in known:
                        got = {os.path.normpath(b + v[2]) for b in known[v[1]]}
                if got:
                    known[name] = known.get(name, set()) | got
        words, always, cut = set(), set(), []
        for m in re.finditer(r'\$(?:\{(\w+)[^}]*\}|(\w+)|(\())', src):
            start, end = m.start(), m.end()
            if m[3]:   # $( ... ): to its closing parenthesis
                depth = 1
                while end < len(src) and depth:
                    depth += {'(': 1, ')': -1}.get(src[end], 0)
                    end += 1
                inner = src[start:end]
                bases = {self.repo.root} if re.search(r'rev-parse\s+--show-toplevel', inner) else \
                    {here} if SCRIPT_DIR.search(inner) else None
            else:
                bases = known.get(m[1] or m[2])
            q = end + 1 if src[end:end + 1] == '"' else end
            tail = re.match(r'/[A-Za-z0-9_.@+/-]*', src[q:])
            if not tail or tail[0] == '/':
                continue
            rest = tail[0].strip('/')
            cut.append((start, q + tail.end()))
            if not re.search(r'[A-Za-z0-9_@+-]', rest):
                continue   # "$tmp/." "$dir/..": no name of the repository
            if bases is not None:
                words |= {os.path.normpath(os.path.join(b, rest)) for b in bases}
                continue
            for c in (os.path.join(here, rest), os.path.join(self.repo.root, rest)):
                r = self.repo.rel(c)
                if r and self.repo.kind(r):
                    always.add(('own', f'{rel}: {docs_scope.shown(src[start:q + tail.end()])}: a path from a '
                                       f'variable it cannot work out'))
                    break
        for start, end in reversed(cut):
            src = src[:start] + ' ' + src[end:]
        return src, words, always


def reaches(changed, files, dirs, globs=()):
    """The changed paths this test's inputs take in."""
    rxs = [docs_scope.glob_re(g) for g in sorted(globs)]
    return [c for c in changed if c in files or any(d == '' or c == d or c.startswith(d + '/') for d in dirs)
            or any(rx.match(c) for rx in rxs)]


# ---------------------------------------------------------------- the config and the decision

def load_config(root, path):
    try:
        with open(os.path.join(root, path), encoding='utf-8') as f:
            c = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f'test-select: cannot read the config {path} ({type(e).__name__}: {e})')
    if not isinstance(c, dict) or set(c) - KEYS or 'tests' not in c:
        raise SystemExit(f'test-select: {path}: needs "tests" and only the keys {sorted(KEYS)} '
                         f'(unknown: {sorted(set(c) - KEYS) if isinstance(c, dict) else "not an object"})')
    tests = c['tests']
    if not isinstance(tests, list) or not tests or not all(isinstance(t, str) and t for t in tests):
        raise SystemExit(f'test-select: {path}: "tests" must be a list of globs')
    pairs = {}
    for key in ('always_all', 'always_run', 'follow_calls'):
        always = c.get(key, [])
        if not isinstance(always, list) or not all(
                isinstance(a, dict) and set(a) == {'glob', 'why'} and all(isinstance(v, str) and v for v in a.values())
                for a in always):
            raise SystemExit(f'test-select: {path}: "{key}" must be a list of {{"glob", "why"}}')
        pairs[key] = [(a['glob'], a['why']) for a in always]
    roots = c.get('import_roots', [])
    if not isinstance(roots, list) or not all(isinstance(r, str) and docs_scope.plain_path(r) for r in roots):
        raise SystemExit(f'test-select: {path}: "import_roots" must be a list of repository folders')
    inputs = c.get('inputs', [])
    if not isinstance(inputs, list) or not all(
            isinstance(a, dict) and set(a) == {'test', 'reads', 'why'} and isinstance(a['test'], str) and a['test']
            and isinstance(a['why'], str) and a['why'] and isinstance(a['reads'], list)
            and all(isinstance(g, str) and g for g in a['reads']) for a in inputs):
        raise SystemExit(f'test-select: {path}: "inputs" must be a list of {{"test", "reads": [globs], "why"}}')
    return (tests, pairs['always_all'], roots, pairs['always_run'], [(a['test'], a['reads'], a['why']) for a in inputs],
            pairs['follow_calls'])


def follow_of(follow_calls, tracked, path=DEFAULT_CONFIG):
    """{module: why} for the config's follow_calls: repository Python files whose top-level functions count only
    for the tests that can reach them. A glob that names no tracked .py file fails the step (a renamed module must be
    renamed there too)."""
    out = {}
    for g, why in follow_calls:
        rx = docs_scope.glob_re(g)
        hits = sorted(f for f in tracked if f.endswith('.py') and rx.match(f))
        if not hits:
            raise SystemExit(f'test-select: {path}: follow_calls {g!r} names no Python file of the repository')
        for f in hits:
            out.setdefault(f, why)
    return out


def tracked_files(root):
    """The repository's files, each once: `git ls-files` lists an unmerged file once per stage (three times during a
    conflict). A file in the index but gone from disk is still one (a test input that cannot be read then runs every
    test)."""
    return set(git('ls-files', '-z', cwd=root).split('\0')) - {''}


def check_config(root, config_path, runs):
    """(ok, lines): do the config's tests name exactly the files the full run runs (globs or paths from the root,
    matched the way the selector matches)? A file the full run runs that the config misses is never picked on a pull
    request; one the config names that the full run does not run is picked and never run."""
    tracked = tracked_files(root)
    tests, always, import_roots, always_run, inputs, follow_calls = load_config(root, config_path)
    every = universe(tests, tracked)
    always_run_of(always_run, every, config_path)
    declared_of(inputs, every, config_path)
    follow_of(follow_calls, tracked, config_path)
    full, lines = [], []
    for g in runs:
        rx = docs_scope.glob_re(g)
        hits = sorted(f for f in tracked if rx.match(f))
        if not hits:
            lines.append(f'the full run\'s {g!r} matches no file of the repository')
        full += [h for h in hits if h not in full]
    missed = [f for f in full if f not in every]
    extra = [t for t in every if t not in full]
    lines += [f'{f}: the full run runs it, but the config\'s tests do not name it (no pull request would pick it)'
              for f in missed]
    lines += [f'{t}: the config\'s tests name it, but the full run does not run it' for t in extra]
    ok = not lines
    head = (f'config ok: its tests name the {len(every)} files the full run runs' if ok else
            f'config does not match the full run: {len(missed)} missed, {len(extra)} not run')
    return ok, [head] + lines


def always_run_of(always_run, every, path=DEFAULT_CONFIG):
    """{test file: why} for the config's always_run; a glob that names no test file fails the step (a renamed or
    deleted test must be renamed there too, or the rule would silently stop holding)."""
    out = {}
    for g, why in always_run:
        rx = docs_scope.glob_re(g)
        hits = [t for t in every if rx.match(t)]
        if not hits:
            raise SystemExit(f'test-select: {path}: always_run {g!r} names no test file of "tests"')
        for t in hits:
            out.setdefault(t, why)
    return out


def declared_of(inputs, every, path=DEFAULT_CONFIG):
    """{test file: (globs, why)} for the config's inputs: what a test reads that this script cannot see (a folder
    its caller passes). Its own file's marks then give way to these globs; a mark from any other file still runs it,
    and the trace check measures the declaration. A test glob that names no test file fails the step."""
    out = {}
    for g, reads, why in inputs:
        rx = docs_scope.glob_re(g)
        hits = [t for t in every if rx.match(t)]
        if not hits:
            raise SystemExit(f'test-select: {path}: inputs {g!r} names no test file of "tests"')
        for t in hits:
            globs, whys = out.get(t, ((), ()))
            out[t] = (tuple(globs) + tuple(reads), tuple(whys) + (why,))
    return {t: (globs, '; '.join(whys)) for t, (globs, whys) in out.items()}


def universe(tests, tracked):
    """The test files, in config order, each once; a literal path that is not tracked, or a glob that matches no
    tracked file, is an error (a moved test folder would otherwise drop its tests from every run, green)."""
    out = []
    for g in tests:
        if not re.search(r'[*?\[]', g):
            if g not in tracked:
                raise SystemExit(f'test-select: test file {g} (named in the config) is not in the repository')
            hits = [g]
        else:
            rx = docs_scope.glob_re(g)
            hits = sorted(f for f in tracked if rx.match(f))
            if not hits:
                raise SystemExit(f'test-select: the config\'s tests glob {g!r} matches no file of the repository')
        for h in hits:
            if h not in out:
                out.append(h)
    return out


def decide(root, config_path, tests, always, changed, tracked, import_roots=(), always_run=None, declared=None,
           follow=()):
    """(chosen test files, reason lines). always_run: {test file: why}, chosen on every pull request; declared:
    declared_of(); follow: follow_of()."""
    lines = []
    if not changed:
        raise AllTests('the change touches no file')
    rules = [(config_path, 'the test-select config')] + list(BUILTIN_ALL) + always
    for c in changed:
        for g, why in rules:
            if docs_scope.glob_re(g).match(c):
                raise AllTests(f'{c} changed ({why})')
    repo = Repo(root, tracked, changed)
    graph = Graph(repo, import_roots, declared, follow)
    chosen = []
    for t in tests:
        if t in (always_run or {}):
            chosen.append(t)
            lines.append(f'run {t}: always (always_run: {always_run[t]})')
            continue
        files, dirs, globs, marks = graph.inputs(t)
        if marks:
            chosen.append(t)
            more = f' (+{len(marks) - 1} more)' if len(marks) > 1 else ''
            lines.append(f'run {t}: always ({marks[0]}){more}')
            continue
        hit = reaches(changed, files, dirs, globs)
        if hit:
            chosen.append(t)
            more = f' (+{len(hit) - 1} more)' if len(hit) > 1 else ''
            mine = (declared or {}).get(t, ((), ''))[0]
            how = ' (declared in inputs)' if reaches(hit[:1], set(), set(), mine) else ''
            lines.append(f'run {t}: takes in {hit[0]}{more}{how}')
    return chosen, lines


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--config', default=DEFAULT_CONFIG)
    p.add_argument('--event', help='the event name (required unless --check-config)')
    p.add_argument('--changed', help='a file with the changed paths, one per line (local use; CI reads git)')
    p.add_argument('--root', default='.')
    p.add_argument('--out', default='')
    p.add_argument('--check-config', action='store_true',
                   help='check that the config\'s tests are exactly what --runs names, then exit (0: they are)')
    p.add_argument('--runs', action='append', default=[],
                   help='with --check-config: a glob or path the full run runs (repeat for each)')
    a = p.parse_args(argv)
    root = os.path.abspath(a.root)
    if not docs_scope.plain_path(a.config):
        raise SystemExit(f'test-select: the config path {a.config!r} must be a plain repository path')
    if a.check_config:
        if not a.runs:
            raise SystemExit('test-select: --check-config needs --runs (what the full run runs)')
        ok, lines = check_config(root, a.config, a.runs)
        print('\n'.join(docs_scope.shown(x) for x in lines))
        return 0 if ok else 1
    if not a.event:
        raise SystemExit('test-select: --event is required')
    tracked = tracked_files(root)
    tests, always, import_roots, always_run, inputs, follow_calls = load_config(root, a.config)
    every = universe(tests, tracked)
    always_run = always_run_of(always_run, every, a.config)
    declared = declared_of(inputs, every, a.config)
    follow = follow_of(follow_calls, tracked, a.config)
    try:
        if a.event != 'pull_request':
            raise AllTests(f'a {a.event} run runs every test')
        if a.changed:
            with open(a.changed, encoding='utf-8') as f:
                changed = sorted({x for x in f.read().split('\n') if x})
        else:
            cwd = os.getcwd()
            os.chdir(root)
            try:
                rows, _, what = docs_scope.the_change(a.event, 'HEAD', '', sha=os.environ.get('GITHUB_SHA', ''),
                                                      event_path=os.environ.get('GITHUB_EVENT_PATH', ''))
            except docs_scope.Unclear as e:
                raise AllTests(str(e))
            finally:
                os.chdir(cwd)
            changed = sorted({r[0] for r in rows})
        chosen, lines = decide(root, a.config, every, always, changed, tracked, import_roots, always_run, declared,
                               follow)
        everything = False
        head = (f'selected {len(chosen)} of {len(every)} test files' if chosen else
                f'no tool test touched: 0 of {len(every)} test files take in the {len(changed)} changed paths')
        lines = [head] + lines
    except AllTests as e:
        chosen, everything, lines = every, True, [f'all {len(every)} test files: {e}']
    except Exception as e:   # an error nobody foresaw: every test, never a step that picks none
        chosen, everything, lines = every, True, [f'all {len(every)} test files: test-select failed '
                                                  f'({type(e).__name__}: {e})']
    lines = [docs_scope.shown(x) for x in lines]
    print('\n'.join(lines))
    out = a.out or os.path.join(os.environ.get('RUNNER_TEMP') or '.', 'test-select.txt')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(''.join(t + '\n' for t in chosen))
    gh = os.environ.get('GITHUB_OUTPUT')
    if gh:
        with open(gh, 'a') as f:
            f.write(f'all={"true" if everything else "false"}\ncount={len(chosen)}\ntotal={len(every)}\n'
                    f'list={out}\nreason={lines[0]}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
