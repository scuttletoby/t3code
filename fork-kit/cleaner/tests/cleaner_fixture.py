"""Shared pieces of the cleaner kit's tests. Everything lives in one temporary folder per test: a fake home, a fake
temp folder, a folder of git clones and worktrees whose "GitHub" is a bare repository reached through a
url.insteadOf rule (so remote.origin.url is a real GitHub URL), stub `gh`, `docker` and `ssh` commands, and a fake
/proc. The rule rewrites GitHub to ssh://github.invalid/<the bare repository>, and the ssh stub runs git's own
server command on this machine: to the cleaner it is a remote on another machine, as GitHub is.
Nothing here reads or changes a real worktree, real Docker, the real process list or anything in the real temp
folder but the test's own folder. Git reads only the test's own config.
"""
import itertools, json, os, shutil, stat, subprocess, sys, tempfile, time, types, unittest
from unittest import mock

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMON = os.path.join(os.path.dirname(KIT), 'common')
LIB = os.path.join(KIT, 'lib', 'cleaner')
MODULES = ('config', 'facts', 'guard', 'compress', 'rules', 'engine', 'report', 'compare', 'daily')
if COMMON not in sys.path:
    sys.path.insert(0, COMMON)

HOUR, DAY = 3600, 86400
REPO = 'acme/widget'
HEX = 'abcdef0123456789' * 4
_count = itertools.count()


def load(broken=None):
    """The kit's modules, loaded fresh under a package name of their own. `broken` = (module, text, replacement)
    loads a copy with that one piece of code replaced: the red checks use it to break one refusal at a time."""
    name = f'cleaner_under_test_{next(_count)}'
    package = types.ModuleType(name)
    package.__path__ = [LIB]
    sys.modules[name] = package
    for mod in MODULES:
        path = os.path.join(LIB, mod + '.py')
        with open(path) as f:
            source = f.read()
        if broken and broken[0] == mod:
            if source.count(broken[1]) != 1:
                raise AssertionError(f'the red check text {broken[1]!r} is in {mod}.py {source.count(broken[1])} times, not once')
            source = source.replace(broken[1], broken[2])
        module = types.ModuleType(f'{name}.{mod}')
        module.__package__, module.__file__ = name, path
        sys.modules[module.__name__] = module
        setattr(package, mod, module)
        exec(compile(source, path, 'exec'), module.__dict__)
    return package


def red_check(case, mutations):
    """Each entry of `mutations` is {rule: (module, text, broken text, test class, test name)}: with the text
    replaced, the named test must fail. A text that is no longer in the code once fails too."""
    for rule, (module, text, broken, cls, test) in mutations.items():
        with case.subTest(rule=rule):
            mutant = type('Broken', (cls,), {'c': load((module, text, broken))})
            result = unittest.TestResult()
            mutant(test).run(result)
            case.assertEqual(result.testsRun, 1)
            case.assertFalse(result.wasSuccessful(), f'{test} still passes with the rule {rule!r} broken')


STUB_GH = '''#!{python} -I
import json, sys
with open({state!r}) as f:
    state = json.load(f)
with open({state!r} + '.calls', 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
if state.get('fail'):
    sys.stderr.write('gh: could not connect\\n')
    print('[]')
    sys.exit(1)
if 'raw' in state:
    sys.stdout.write(state['raw'])
    sys.exit(0)
which = sys.argv[sys.argv.index('--state') + 1]
print(json.dumps(state[which]))
'''

STUB_DOCKER = '''#!{python} -I
import json, sys
args = sys.argv[1:]
with open({state!r}) as f:
    state = json.load(f)
with open({state!r} + '.calls', 'a') as f:
    f.write(json.dumps(args) + '\\n')
if state.get('fail'):
    sys.stderr.write('Cannot connect to the Docker daemon\\n')
    sys.exit(1)
volumes = state['volumes']
if args[:3] == ['volume', 'ls', '-q']:
    dangling = 'dangling=true' in args
    print('\\n'.join(n for n, v in volumes.items() if not (dangling and v.get('used'))))
elif args[:2] == ['volume', 'inspect']:
    for name in args[4:]:
        print(name, volumes[name]['created'], volumes[name].get('mountpoint', '/var/lib/docker/volumes/' + name + '/_data'), sep='\\t')
elif args[:2] == ['volume', 'rm']:
    name = args[2]
    if name not in volumes or volumes[name].get('used'):
        sys.stderr.write('volume is in use\\n')
        sys.exit(1)
    del volumes[name]
    with open({state!r}, 'w') as f:
        json.dump(state, f)
elif args[:3] == ['system', 'df', '-v']:
    for name, v in volumes.items():
        print(name, v.get('size', '0B'), sep='\t')
elif args[:1] == ['info']:
    print(state.get('root', '/'))
else:
    sys.exit(64)
'''


# `ssh [options] host "git-upload-pack '/path'"`: run the last argument here, as git's own subcommand.
STUB_SSH = r'''#!/bin/sh
for last; do :; done
case "$last" in git-upload-pack\ *|git-receive-pack\ *) ;; *) echo "ssh stub: $last" >&2; exit 255 ;; esac
exec sh -c "git ${last#git-}"
'''


class Case(unittest.TestCase):
    """One temporary folder, a config over it, fake facts. Subclasses set `c` to the loaded kit."""
    c = None

    def setUp(self):
        umask = os.umask(0o022)
        self.addCleanup(os.umask, umask)
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix='cleaner-test-'))
        self.addCleanup(self.close)
        self.home, self.tmp, self.trees = (self.mkdir(n) for n in ('home', 'tmp', 'trees'))
        self.now = time.time()
        with open(self.path('gitconfig'), 'w') as f:
            f.write('[user]\n\tname = test\n\temail = test@example.invalid\n[init]\n\tdefaultBranch = main\n'
                    '[advice]\n\tdetachedHead = false\n[commit]\n\tgpgsign = false\n'
                    f'[url "ssh://github.invalid{self.dir}/gh/"]\n\tinsteadOf = https://github.com/\n'
                    f'[core]\n\tsshCommand = {self.path("bin-ssh")}\n[ssh]\n\tvariant = ssh\n')
        with open(self.path('bin-ssh'), 'w') as f:
            f.write(STUB_SSH)
        os.chmod(self.path('bin-ssh'), 0o755)
        env = mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': self.path('gitconfig'), 'GIT_CONFIG_NOSYSTEM': '1',
                                           'HOME': self.home})
        env.start()
        self.addCleanup(env.stop)
        for name in [n for n in os.environ if n.startswith('GIT_') and n not in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM')]:
            del os.environ[name]
        self.section = {'logFile': self.path('state', 'cleaner.log'), 'daily': {'outDir': self.path('state', 'daily')}}
        self.merged, self.open = [], []
        self.busy, self.unit_names, self.locks, self.texts = set(), set(), set(), []
        self.mount_points = {'/'}
        self.mount_lines, self.bind_sources = [], set()     # (point, device, root) and (device, root)
        self.mount_options = {'/': ('rw', 'relatime')}       # {point: its per-mount options}
        # Tests set a file's age with its modification time. The kit also counts the inode change time, which no
        # test can set, so the tests read the modification time alone; one test puts the real reader back.
        self.real_changed = self.c.guard.changed
        clock = mock.patch.object(self.c.guard, 'changed', lambda st: st.st_mtime)
        clock.start()
        self.addCleanup(clock.stop)
        # The worktrees rule keeps a worktree used in the last day. Tests make a tree and judge it at once, so they
        # read "never used"; the tests of that refusal (worktrees.test.py, Used) put the real reader back.
        self.real_last_used = self.c.rules.last_used
        unused = mock.patch.object(self.c.rules, 'last_used', lambda path, git_dir, look: (0.0, 'nothing'))
        unused.start()
        self.addCleanup(unused.stop)

    def close(self):
        for root, dirs, _ in os.walk(self.dir):
            for d in dirs:
                p = os.path.join(root, d)
                if not os.path.islink(p):
                    os.chmod(p, 0o700)
        shutil.rmtree(self.dir)

    # --- files -------------------------------------------------------------------------------------------------
    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def mkdir(self, *parts):
        os.makedirs(self.path(*parts), exist_ok=True)
        return self.path(*parts)

    def file(self, path, text='x', days=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)
        if days is not None:
            self.age(path, days)
        return path

    def age(self, path, days):
        """The entry, and everything in it, last changed `days` days ago."""
        when = self.now - days * DAY
        if os.path.isdir(path) and not os.path.islink(path):
            for root, dirs, files in os.walk(path):
                for n in dirs + files:
                    os.utime(os.path.join(root, n), (when, when), follow_symlinks=False)
        os.utime(path, (when, when), follow_symlinks=False)
        return path

    def old_dir(self, name, days=30, inside='data.bin'):
        """A folder in the fake temp folder, `days` days old, with one file in it."""
        d = self.mkdir('tmp', name)
        if inside:
            self.file(os.path.join(d, inside))
        return self.age(d, days)

    # --- config and facts --------------------------------------------------------------------------------------
    def cfg(self, **section):
        merged = {**self.section, **section}
        return self.c.config.Config({k: v for k, v in merged.items() if v is not None}, home=self.home)

    def tmp_cfg(self, patterns=None, **section):
        return self.cfg(tmp={'root': self.tmp, 'patterns': patterns or {'*': 7}}, **section)

    def repo_cfg(self, regenerable=None, **section):
        repo = {'root': self.trees, 'github': REPO}
        if regenerable is not None:
            repo['regenerable'] = regenerable
        return self.cfg(worktrees={'repos': [repo]}, **section)

    def processes(self):
        return self.c.facts.Processes(busy=set(self.busy), locks=set(self.locks), texts=list(self.texts))

    def facts(self, cfg, **over):
        readers = {'processes': self.processes, 'units': lambda: set(self.unit_names),
                   'prs': lambda repo: {'merged': list(self.merged), 'open': list(self.open)},
                   'dangling': lambda: [], 'now': self.now, 'mounts': lambda: self.c.facts.Mounts(self.mount_points, self.mount_lines, self.bind_sources, self.mount_options)}
        return self.c.facts.Facts(cfg, **{**readers, **over})

    def plan(self, cfg, **over):
        return self.c.rules.plan(cfg, self.facts(cfg, **over))

    def run_cleaner(self, cfg, really=True, **over):
        """(exit code, lines) of one `run` (or `plan` with really=False)."""
        lines = []
        code = self.c.engine.execute(cfg, self.facts(cfg, **over), really, say=lines.append)
        return code, lines

    def delete_all(self, cfg, facts, items):
        """(failed, lines) of the engine's delete loop as a run does it: with the log open and a journal."""
        lines, log = [], self.c.engine.Log(cfg)
        try:
            failed = self.c.engine.delete_all(cfg, facts, items, True, lines.append, log, [])
        finally:
            log.close()
        return failed, lines

    def assertDeletes(self, plan, path, rule=None):
        found = [i for i in plan.items if i.path == path]
        self.assertEqual(len(found), 1, f"{path} is not in the plan; kept: {[(k.code, k.detail) for k in plan.kept if k.path == path]}")
        if rule:
            self.assertEqual(found[0].rule, rule)
        return found[0]

    def assertKept(self, plan, path, code, detail=None):
        self.assertEqual([i for i in plan.items if i.path == path], [], f'{path} is in the plan')
        found = [k for k in plan.kept if k.path == path]
        self.assertEqual([k.code for k in found], [code], f'{path}: {found}')
        if detail is not None:
            self.assertIn(detail, found[0].detail)
        return found[0]

    def assertNotCandidate(self, plan, path):
        self.assertEqual([x for x in plan.items + plan.kept if x.path == path], [])

    # --- git ---------------------------------------------------------------------------------------------------
    def git(self, cwd, *args):
        return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def origin(self, repo=REPO):
        """The bare repository that stands in for github.com/<repo>, with one commit on main."""
        bare = self.path('gh', repo + '.git')
        if not os.path.isdir(bare):
            os.makedirs(bare)
            self.git(bare, 'init', '-q', '--bare', '-b', 'main')
            seed = self.mkdir('seed', repo.replace('/', '-'))
            self.git(seed, 'init', '-q', '-b', 'main')
            self.file(os.path.join(seed, 'README.md'), 'hello\n')
            self.file(os.path.join(seed, '.gitignore'), 'node_modules/\n__pycache__/\n*.log\nlocal/\n.env\n')
            self.git(seed, 'add', '-A')
            self.git(seed, 'commit', '-q', '-m', 'first')
            self.git(seed, 'push', '-q', bare, 'main')
        return bare

    def clone(self, name, repo=REPO, into=None):
        self.origin(repo)
        path = os.path.join(into or self.trees, name)
        self.git(self.dir, 'clone', '-q', f'https://github.com/{repo}.git', path)
        return path

    def worktree(self, clone, name, branch=None, into=None):
        path = os.path.join(into or self.trees, name)
        self.git(clone, 'worktree', 'add', '-q', '-b', branch or name, path)
        return path

    def commit(self, tree, name='work.txt', text=None):
        self.file(os.path.join(tree, name), text or f'{name} {next(_count)}\n')
        self.git(tree, 'add', '-A')
        self.git(tree, 'commit', '-q', '-m', f'change {name}')
        return self.head(tree)

    def head(self, tree):
        return self.git(tree, 'rev-parse', 'HEAD')

    def branch(self, tree):
        return self.git(tree, 'rev-parse', '--abbrev-ref', 'HEAD')

    def push(self, tree):
        self.git(tree, 'push', '-q', '-u', 'origin', 'HEAD')

    def pr(self, tree, number=7, hours=48, oid=None, state='merged', base='main', fork=False, branch=None):
        """A pull request row for the tree's branch as `gh pr list --json` prints it: merged `hours` ago, or open;
        `fork`: its head branch is in a fork (isCrossRepository)."""
        when = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.now - hours * HOUR))
        row = {'number': number, 'headRefName': branch or self.branch(tree), 'headRefOid': oid or self.head(tree),
               'mergedAt': when if state == 'merged' else None, 'baseRefName': base, 'isCrossRepository': fork}
        (self.merged if state == 'merged' else self.open).append(row)
        return row

    def finished(self, name='fix-1', clone=None, number=7, hours=48):
        """A worktree whose one commit is pushed and whose pull request merged `hours` ago: the cleaner may remove it."""
        clone = clone or (self.path('trees', 'base') if os.path.isdir(self.path('trees', 'base')) else self.clone('base'))
        tree = self.worktree(clone, name)
        self.commit(tree)
        self.push(tree)
        self.pr(tree, number, hours)
        return tree

    # --- stub commands -----------------------------------------------------------------------------------------
    def stub(self, name, template, state):
        """An executable stub; its state file is JSON the test may change, its calls are in <state>.calls."""
        state_path = self.path(name + '-state.json')
        with open(state_path, 'w') as f:
            json.dump(state, f)
        exe = self.path('bin-' + name)
        with open(exe, 'w') as f:
            f.write(template.format(python=sys.executable, state=state_path))
        os.chmod(exe, 0o755)
        return exe, state_path

    def calls(self, state_path):
        if not os.path.exists(state_path + '.calls'):
            return []
        with open(state_path + '.calls') as f:
            return [json.loads(line) for line in f]

    def fake_proc(self, processes, locks='', sockets=()):
        """A fake /proc: {pid: {'cwd': path, 'exe': path, 'fd': [paths], 'cmdline': [args], 'maps': [paths]}}."""
        if os.path.isdir(self.path('proc')):
            shutil.rmtree(self.path('proc'))
        root = self.mkdir('proc')
        self.file(os.path.join(root, 'locks'), locks)
        self.file(os.path.join(root, 'net', 'unix'), 'Num       RefCount Protocol Flags    Type St Inode Path\n' + ''.join(
            f'0000000000000000: 00000002 00000000 00010000 0001 01 {i + 9} {s}\n' for i, s in enumerate(sockets))
            + '0000000000000000: 00000002 00000000 00000000 0002 01 7\n')
        for pid, p in processes.items():
            d = self.mkdir('proc', str(pid))
            os.mkdir(os.path.join(d, 'fd'))
            for key in ('cwd', 'root', 'exe'):
                if p.get(key, '/' if key != 'exe' else None):
                    os.symlink(p.get(key, '/'), os.path.join(d, key))
            for i, target in enumerate(p.get('fd', [])):
                os.symlink(target, os.path.join(d, 'fd', str(i)))
            with open(os.path.join(d, 'cmdline'), 'wb') as f:
                f.write(b'\0'.join(a.encode() for a in p.get('cmdline', ['sleep'])) + b'\0')
            with open(os.path.join(d, 'maps'), 'w') as f:
                f.write(''.join(f'7f00-7f01 r--p 00000000 08:01 {i + 5}    {m}\n' for i, m in enumerate(p.get('maps', []))))
        return root


def read_only(path):
    os.chmod(path, stat.S_IRUSR | stat.S_IXUSR)
