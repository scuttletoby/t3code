"""Shared by the installer tests of every kit that uses fork-kit/common/kit_install.py: load a kit's installer (or a
copy with one text broken, in it or in kit_install.py) on a fake host in a temporary folder, every fixed path under
PREFIX and the current user as root, with a fake process list and runtime folder of its own (PROC, RUN). No root,
no systemd, no network. A link the installer writes names a host path: resolve() follows it inside the fake host
only, never onto the real machine."""
import contextlib, importlib.util, io, os, shutil, signal, subprocess, sys, tempfile, unittest
from unittest import mock

FORK_KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMON = os.path.join(FORK_KIT, 'common')
GATE = os.path.join(os.path.dirname(FORK_KIT), 'fork-ci', 'actions', 'pr-queue')
ENGINE = os.path.join(COMMON, 'kit_install.py')
BOOTSTRAP = os.path.join(COMMON, 'bootstrap.sh')
SHA1, SHA2 = 'a' * 40, 'b' * 40


def source_of(path, replace=None):
    """The text of `path`; with `replace` = (text, broken text), that text (which must be there exactly once)
    replaced."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    if replace is not None:
        if text.count(replace[0]) != 1:
            raise AssertionError(f'{replace[0]!r} is not in {path} exactly once')
        text = text.replace(replace[0], replace[1])
    return text


def _module(name, path, text):
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = path
    exec(compile(text, path, 'exec'), mod.__dict__)
    return mod


def load(installer, engine_replace=None, installer_replace=None):
    """The installer module of a kit (its `INSTALLER` and `main`), on the shared engine, either broken as asked."""
    engine = _module('kit_install', ENGINE, source_of(ENGINE, engine_replace))
    saved_path, saved = list(sys.path), sys.modules.get('kit_install')
    sys.modules['kit_install'] = engine
    try:
        return _module('kit_installer', installer, source_of(installer, installer_replace))
    finally:
        sys.path[:] = saved_path
        if saved is None:
            sys.modules.pop('kit_install', None)
        else:
            sys.modules['kit_install'] = saved


class FakeHost(unittest.TestCase):
    """A test case with self.m: one kit's installer on a fake host under self.root."""
    KIT_DIR = None          # fork-kit/<kit>
    INSTALLER = None        # fork-kit/<kit>/sbin/<name>-install
    broken = None           # RedCheck: ('engine' | 'installer' | 'bootstrap', text, broken text)

    def setUp(self):
        self.umask = os.umask(0o022)
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='kit-install-test-'))
        self.addCleanup(self.close)
        where = self.broken[0] if self.broken else None
        mod = load(self.INSTALLER, self.broken[1:] if where == 'engine' else None,
                   self.broken[1:] if where == 'installer' else None)
        self.mod, self.m = mod, mod.INSTALLER
        self.m.PREFIX, self.m.ROOT_UID = self.root, os.geteuid()
        self.host = os.path.realpath(tempfile.mkdtemp(prefix='kit-install-host-'))     # outside root: kept by resets
        self.m.PROC, self.m.RUN = os.path.join(self.host, 'proc'), os.path.join(self.host, 'run')
        os.makedirs(self.m.PROC)
        os.makedirs(self.m.RUN)
        self.bootstrap = os.path.join(self.root, 'bootstrap.sh')
        with open(self.bootstrap, 'w', encoding='utf-8') as f:
            f.write(source_of(BOOTSTRAP, self.broken[1:] if where == 'bootstrap' else None))

    def close(self):
        os.umask(self.umask)
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.host, ignore_errors=True)

    def path(self, p):
        return self.root + p

    def resolve(self, p):
        """Host path p with every link on it followed inside the fake host (an absolute target is read under the
        root, as on the host it would be): the real file under root, or None when the chain ends nowhere. Never
        follows a link out of the fake host, so a test cannot pass because the real machine has the kit."""
        parts, done, hops = [x for x in p.split('/') if x], '', 0
        while parts:
            part = parts.pop(0)
            if part == '.':
                continue
            if part == '..':
                done = os.path.dirname(done)
                continue
            here = f'{done}/{part}'
            if os.path.islink(self.path(here)):
                hops += 1
                if hops > 40:
                    return None
                target = os.readlink(self.path(here))
                parts = [x for x in target.split('/') if x] + parts
                done = '' if target.startswith('/') else done
                continue
            if not os.path.lexists(self.path(here)):
                return None
            done = here
        return self.path(done or '/')

    def is_file(self, p):
        """p, links followed inside the fake host only, is a file."""
        real = self.resolve(p)
        return real is not None and os.path.isfile(real) and not os.path.islink(real)

    @contextlib.contextmanager
    def no_hang(self, seconds=5):
        """Fails the test (instead of hanging it) when the body waits longer than that, on a FIFO say. Each body
        takes well under a second, so 5 s is a wide margin on a slow runner; a blocked open waits forever, so a
        longer limit finds nothing more and only costs time (RedCheck's mutants that block wait it out each)."""
        def alarm(*_):
            raise AssertionError(f'still waiting after {seconds} s (it blocked)')
        old = signal.signal(signal.SIGALRM, alarm)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    def process(self, pid, *argv, cwd=None):
        """A fake process with that command line (and that folder, cwd) in the fake process list."""
        os.makedirs(os.path.join(self.m.PROC, str(pid)), exist_ok=True)
        with open(os.path.join(self.m.PROC, str(pid), 'cmdline'), 'wb') as f:
            f.write(b'\0'.join(os.fsencode(a) for a in argv) + b'\0')
        if cwd is not None:
            os.symlink(cwd, os.path.join(self.m.PROC, str(pid), 'cwd'))
        self.addCleanup(shutil.rmtree, os.path.join(self.m.PROC, str(pid)), True)

    def run_main(self, *argv, stop_after=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.mod.main(list(argv), stop_after=stop_after)
            except SystemExit as e:
                code = str(e)
        return code, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def stop_context(self, n):
        """Names the stop (n) in a failure, without going on to the next one as subTest would."""
        try:
            yield
        except AssertionError as e:
            raise AssertionError(f'stopped after change {n}: {e}') from e

    CHANGES = ('unlink', 'rename', 'replace', 'rmdir', 'chmod', 'chown')

    def uninstall_stopped_after(self, n, check=None):
        """uninstall, stopped (as by a kill) right after its n-th change to the files (each of CHANGES, inside
        rmtree too); check() runs after every change. (code, out, err, whether it stopped)."""
        count, real = [0], {name: getattr(os, name) for name in self.CHANGES}

        def wrap(name):
            def change(*args, **kwargs):
                result = real[name](*args, **kwargs)
                count[0] += 1
                if check is not None:
                    check()
                if count[0] == n:
                    raise SystemExit('stopped (test)')
                return result
            return change
        with contextlib.ExitStack() as stack:
            for name in self.CHANGES:
                stack.enter_context(mock.patch.object(os, name, wrap(name)))
            code, out, err = self.run_main('uninstall')
        return code, out, err, count[0] >= n

    def install(self, *extra, sha=SHA1, stop_after=None, source=None):
        return self.run_main('install', '--source', source or self.KIT_DIR, '--sha', sha, *extra,
                             stop_after=stop_after)

    def copy_kit(self):
        """A source folder of the kit the test may change, laid out as in the fork: its common/ beside it, and the
        fork's other folders a kit reads from at install (fork-ci/actions/pr-queue: the gardener's gate/) two up."""
        src = os.path.join(self.root, 'fork-kit', 'src')
        ignore = shutil.ignore_patterns('__pycache__')
        shutil.copytree(self.KIT_DIR, src, ignore=ignore)
        shutil.copytree(COMMON, os.path.join(self.root, 'fork-kit', 'common'), ignore=ignore)
        shutil.copytree(GATE, os.path.join(self.root, 'fork-ci', 'actions', 'pr-queue'), ignore=ignore)
        return src

    @staticmethod
    def files_under(top):
        """Every path below top (links not followed) with what it is: a link's target, or 'dir' / 'file'."""
        seen = {}
        for folder, dirs, files in os.walk(top):
            for name in dirs + files:
                p = os.path.join(folder, name)
                seen[os.path.relpath(p, top)] = (os.readlink(p) if os.path.islink(p)
                                                 else 'dir' if os.path.isdir(p) else 'file')
        return seen

    def version(self, sha=SHA1, rel=''):
        return self.path(f'{self.m.LIBEXEC}/versions/{sha}' + (f'/{rel}' if rel else ''))

    def current(self):
        return os.readlink(self.path(self.m.CURRENT))

    def snapshot(self):
        out = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                st = os.lstat(full)
                out[full[len(self.root):]] = (st.st_mode, st.st_mtime_ns,
                                              os.readlink(full) if os.path.islink(full) else None)
        return out

    def run_bootstrap(self, *args):
        return subprocess.run(['sh', self.bootstrap, *args], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)


def red_check(case, mutations):
    """For each rule: break its text, run its test, and show the test then fails."""
    for rule, (where, text, broken, test) in mutations.items():
        with case.subTest(rule=rule):
            mutant = type('Broken', (case.BASE,), {'broken': (where, text, broken)})
            result = unittest.TestResult()
            mutant(test).run(result)
            case.assertEqual(result.testsRun, 1)
            case.assertFalse(result.wasSuccessful(), f'{test} still passes with {rule!r} broken')
            case.assertFalse([e for e in result.errors + result.failures if 'exactly once' in e[1]],
                             f'{rule}: text not found')         # an AssertionError in setUp is a failure
