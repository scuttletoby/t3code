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
A test file runs when a changed path is one of its inputs or lies under one of its input folders. A deleted path
counts as present, so a test that still names a deleted or renamed module or file is chosen (and fails).

Every test runs (fails closed) for: an event other than pull_request (main, merge queue, schedule, manual), a change
that is unknown or empty (docs-scope's rule: HEAD must be GitHub's merge commit), a change to the config, to a
workflow file (.github/workflows/) or to a path the config's `always_all` names (the runner, shared helpers,
fixtures), a Python file in a test's inputs that does not parse, a sys.path entry or importlib.import_module name it
cannot work out, an import that is neither the standard library, nor a repository file, nor clearly third-party (a
module of that name exists somewhere in the repository), and any error this script did not foresee.

What it cannot see: a path built from data at run time (a name read from a JSON file, a loop over a list, a file in
the test's own folder or the root picked by a variable). A test whose inputs hide that way is missed only on pull
requests; the default branch runs every test, and the project's `always_all` covers files such a test reads.

  python3 -I test_select.py --config .github/test-select.json --event "$GITHUB_EVENT_NAME" [--changed FILE]
Reads $GITHUB_SHA and $GITHUB_EVENT_PATH (as docs-scope does) unless --changed gives the changed paths (one per line;
for a local check or a measure, never in CI). Prints why, writes the chosen test files (one per line, config order) to
--out (default $RUNNER_TEMP/test-select.txt), and writes all=true|false, count=<n>, total=<n>, list=<file> and
reason=<one line> to $GITHUB_OUTPUT. Exit 1 only when the config cannot be read (no test list: the job goes red).
"""
import argparse, ast, json, os, posixpath, re, subprocess, sys

sys.dont_write_bytecode = True   # leave no __pycache__ in the action folder
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'docs-scope'))
import docs_scope   # noqa: E402  (the same release: the change as docs-scope computes it, and its glob rule)

DEFAULT_CONFIG = '.github/test-select.json'
KEYS = {'tests', 'always_all', 'import_roots'}
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


UNKNOWN = Partial('')


def git(*args, cwd=None):
    r = subprocess.run(['git', *args], capture_output=True, cwd=cwd)
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


class PyFile:
    """One Python file's imports, sys.path entries and named paths, worked out once per run."""

    def __init__(self, repo, rel):
        self.repo, self.rel = repo, rel
        self.abs = os.path.join(repo.root, rel)
        self.dir = os.path.dirname(self.abs)
        try:
            with open(self.abs, 'rb') as f:
                src = f.read()
            tree = ast.parse(src, filename=rel)
        except (OSError, SyntaxError, ValueError) as e:
            raise AllTests(f'cannot parse {rel} ({type(e).__name__}: {str(e)[:120]}), a test input')
        self.tree, self.parents, self.memo = tree, {}, {}
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
        self.binds, self.attrs = {}, {}
        bind = lambda name, where, value: self.binds.setdefault((where, name), []).append(value)   # noqa: E731
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
            elif isinstance(node, ast.AugAssign):
                pairs = [(node.target, None)]
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                pairs = [(node.target, None)]
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                pairs = [(node.optional_vars, None)]
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bind(node.name, here, None)
            elif isinstance(node, ast.arg):
                bind(node.arg, here, None)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bind(node.name, here, None)
            elif isinstance(node, ast.alias):
                bind((node.asname or node.name).split('.')[0], here, None)
            elif isinstance(node, ast.Global):
                self.globals.setdefault(here, set()).update(node.names)
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
                        bind(tt.id, where, vv)
                    elif isinstance(tt, ast.Attribute):
                        self.attrs.setdefault(tt.attr, []).append(vv)   # self.x: any assignment in the file
                    else:
                        for n in ast.walk(tt):
                            if isinstance(n, ast.Name):
                                bind(n.id, self.scope[n], None)
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
        for node in nodes:
            if isinstance(node, ast.expr) and self.maximal(node):
                for v in self.ev(node):
                    self.paths.add(v)

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
        subject = None
        if name in LISTING_CALLS and node.args:
            subject = node.args[0]
        elif isinstance(node.func, ast.Attribute) and n in LISTING_METHODS:
            subject = node.func.value
        if subject is not None:
            for v in self.ev(subject):
                self.dirs_listed.add(v)

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
        values = self.binds.get(key) if isinstance(key, tuple) else self.attrs.get(key)
        if key in seen or not values:
            return {UNKNOWN}
        memo = (key, seen)
        if memo not in self.memo:
            self.memo[memo] = {UNKNOWN}   # a cycle through other names reads as unknown
            out = set()
            for v in values:
                out |= {UNKNOWN} if v is None else self.ev(v, depth + 1, seen | {key})
            self.memo[memo] = out
        return self.memo[memo]

    def ev(self, node, depth=0, seen=frozenset()):
        """The set of values node can have: str, or Partial (known start)."""
        if depth > 40:
            return {UNKNOWN}
        e = lambda x: self.ev(x, depth + 1, seen)   # noqa: E731
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
                return combine(e(node.left), e(node.right), join)
            if isinstance(node.op, ast.Add):
                return combine(e(node.left), e(node.right), concat)
            return {UNKNOWN}
        if isinstance(node, ast.JoinedStr):
            out = {''}
            for part in node.values:
                vals = e(part.value) if isinstance(part, ast.FormattedValue) else e(part)
                out = combine(out, vals, concat)
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
            if isinstance(node.func, ast.Attribute) and n in ('resolve', 'absolute', 'expanduser') and not args:
                return e(node.func.value)
            if isinstance(node.func, ast.Attribute) and n == 'joinpath':
                return joined([node.func.value, *args], e)
            if n in JOINS and self.dotted(node.func) in ('os.path.join', 'path.join', 'posixpath.join', 'join'):
                return joined(args, e)
            if n in ('Path', 'PurePath', 'PosixPath', 'PurePosixPath'):
                return joined(args, e) if args else {UNKNOWN}
            if n in ('abspath', 'realpath', 'normpath', 'fspath', 'str') and len(args) == 1:
                return e(args[0])
            if n == 'dirname' and len(args) == 1:
                return {dirname(v) for v in e(args[0])}
            return {UNKNOWN}
        return {UNKNOWN}


def dirname(v):
    if isinstance(v, Partial):
        return Partial(v.prefix.rsplit('/', 1)[0] + '/') if '/' in v.prefix else UNKNOWN
    return os.path.dirname(v.rstrip('/') or v)


def join(a, b):
    """os.path.join and pathlib's / on values: an absolute right side wins; after an unknown part all is unknown."""
    if isinstance(b, str) and b.startswith('/'):
        return b
    if isinstance(b, Partial) and b.prefix.startswith('/'):
        return b
    if isinstance(a, Partial):
        return a
    if isinstance(b, Partial):
        return Partial(os.path.join(a, b.prefix) if b.prefix else os.path.join(a, ''))
    return os.path.join(a, b)


def concat(a, b):
    if isinstance(a, Partial):
        return a
    if isinstance(b, Partial):
        return Partial(a + b.prefix)
    return a + b


def combine(left, right, op, cap=64):
    out = set()
    for a in left:
        for b in right:
            out.add(op(a, b))
            if len(out) > cap:
                return out
    return out


def joined(args, e):
    if not args:
        return {UNKNOWN}
    out = None
    for a in args:
        vals = {UNKNOWN} if isinstance(a, ast.Starred) else e(a)
        out = vals if out is None else combine(out, vals, join)
    return out


# ---------------------------------------------------------------- inputs of a test file

class Graph:
    def __init__(self, repo, import_roots=()):
        self.repo = repo
        self.import_roots = [os.path.join(repo.root, r) for r in import_roots]
        self.py, self.text, self.memo = {}, {}, {}

    def pyfile(self, rel):
        if rel not in self.py:
            self.py[rel] = PyFile(self.repo, rel)
        return self.py[rel]

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
                return out
            for c in [value] if os.path.isabs(value) else [os.path.join(base_dir, value), os.path.join(repo.root, value)]:
                rel = repo.rel(c)
                if repo.kind(rel) == 'dir':
                    out.append(('glob', f'{rel}/{rest}' if rel else rest))
            return out
        if isinstance(value, Partial):
            # A path with a known folder and a rest worked out at run time (ROOT / 'Plugins' / name): anything in that
            # folder. A folder the file itself is inside (HERE, ROOT, the repository root: the base of nearly every
            # path a tool builds) is not taken, or a tool would take in its whole tree; data that picks such a path
            # belongs in the project's always_all.
            pre = value.prefix
            if '/' not in pre:
                return out   # nothing known, or no folder known ('Error: {e}')
            for c in [pre] if os.path.isabs(pre) else [os.path.join(base_dir, pre), os.path.join(repo.root, pre)]:
                rel = repo.rel(c if c.endswith('/') else os.path.dirname(c))
                if repo.kind(rel) == 'dir' and not inside(rel):
                    out.append(('dir', rel))
            return out
        if not value or '\0' in value or not (os.path.isabs(value) or re.search(r'[/.]', value)):
            return out
        for c in [value] if os.path.isabs(value) else [os.path.join(base_dir, value), os.path.join(repo.root, value)]:
            rel = repo.rel(c)
            k = repo.kind(rel)
            if k == 'file':
                out.append(('file', rel))
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
        else:
            roots = [pf.dir, *sorted(search), *self.import_roots]
        parts = [p for p in name.split('.') if p]
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
                return here
        if level:
            return None
        top = parts[0] if parts else ''
        if top in STDLIB or top not in repo.modules:
            return []   # the standard library, or third-party: no file of the repository
        return None

    def named_inputs(self, rel, kind):
        """The inputs one file names (paths, listed folders, words), resolved once per run."""
        key = ('named', rel)
        if key not in self.memo:
            if kind == 'py':
                pf = self.pyfile(rel)
                named, base = [(v, False) for v in pf.paths] + [(v, True) for v in pf.dirs_listed], pf.dir
            else:
                named = [(w, False) for w in self.words(rel)]
                base = os.path.join(self.repo.root, posixpath.dirname(rel))
            out = set()
            for value, listed in named:
                out.update(self.resolve(value, base, rel, listed, kind == 'text'))
            self.memo[key] = sorted(out)
        return self.memo[key]

    def inputs(self, test):
        """(files, folders) that are the test file's inputs."""
        files, dirs, globs, todo, search, done, unfound = {test}, set(), set(), [test], set(), set(), []
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
                    continue
                for level, name, names, line in pf.imports:
                    got = self.module(level, name, names, pf, search)
                    if got is None:
                        unfound.append(f'{rel}:{line}: import {"." * level}{name}')
                        continue
                    for m in got:
                        files.add(m)
                        todo.append(m)
            elif kind != 'text':
                continue
            for k, r in self.named_inputs(rel, kind):
                if k == 'file':
                    files.add(r)
                    todo.append(r)
                else:
                    (dirs if k == 'dir' else globs).add(r)
        if unfound:
            raise AllTests(f'{unfound[0]}: a module of that name is in the repository, but not where this import '
                           f'looks (sys.path entries it can work out, the file\'s folder)')
        return files, dirs, globs

    def words(self, rel):
        if rel not in self.text:
            try:
                with open(os.path.join(self.repo.root, rel), encoding='utf-8', errors='replace') as f:
                    src = f.read()
            except OSError as e:
                raise AllTests(f'cannot read {rel} ({e}), a test input')
            self.text[rel] = {w.lstrip('/') if w.startswith('/..') or w.startswith('/.') else w
                              for w in WORD.findall(src)}
        return self.text[rel]


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
    tests, always = c['tests'], c.get('always_all', [])
    if not isinstance(tests, list) or not tests or not all(isinstance(t, str) and t for t in tests):
        raise SystemExit(f'test-select: {path}: "tests" must be a list of globs')
    if not isinstance(always, list) or not all(
            isinstance(a, dict) and set(a) == {'glob', 'why'} and all(isinstance(v, str) and v for v in a.values())
            for a in always):
        raise SystemExit(f'test-select: {path}: "always_all" must be a list of {{"glob", "why"}}')
    roots = c.get('import_roots', [])
    if not isinstance(roots, list) or not all(isinstance(r, str) and docs_scope.plain_path(r) for r in roots):
        raise SystemExit(f'test-select: {path}: "import_roots" must be a list of repository folders')
    return tests, [(a['glob'], a['why']) for a in always], roots


def universe(tests, tracked):
    """The test files, in config order, each once; a literal path that is not tracked is an error."""
    out = []
    for g in tests:
        if not re.search(r'[*?\[]', g):
            if g not in tracked:
                raise SystemExit(f'test-select: test file {g} (named in the config) is not in the repository')
            hits = [g]
        else:
            rx = docs_scope.glob_re(g)
            hits = sorted(f for f in tracked if rx.match(f))
        for h in hits:
            if h not in out:
                out.append(h)
    return out


def decide(root, config_path, tests, always, changed, tracked, import_roots=()):
    """(chosen test files, all?, reason lines)."""
    lines = []
    if not changed:
        raise AllTests('the change touches no file')
    rules = [(config_path, 'the test-select config')] + list(BUILTIN_ALL) + always
    for c in changed:
        for g, why in rules:
            if docs_scope.glob_re(g).match(c):
                raise AllTests(f'{c} changed ({why})')
    repo = Repo(root, tracked, changed)
    graph = Graph(repo, import_roots)
    chosen = []
    for t in tests:
        hit = reaches(changed, *graph.inputs(t))
        if hit:
            chosen.append(t)
            more = f' (+{len(hit) - 1} more)' if len(hit) > 1 else ''
            lines.append(f'run {t}: takes in {hit[0]}{more}')
    return chosen, lines


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--config', default=DEFAULT_CONFIG)
    p.add_argument('--event', required=True)
    p.add_argument('--changed', help='a file with the changed paths, one per line (local use; CI reads git)')
    p.add_argument('--root', default='.')
    p.add_argument('--out', default='')
    a = p.parse_args(argv)
    root = os.path.abspath(a.root)
    if not docs_scope.plain_path(a.config):
        raise SystemExit(f'test-select: the config path {a.config!r} must be a plain repository path')
    tracked = set(git('ls-files', '-z', cwd=root).split('\0')) - {''}
    tests, always, import_roots = load_config(root, a.config)
    every = universe(tests, tracked)
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
        chosen, lines = decide(root, a.config, every, always, changed, tracked, import_roots)
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
