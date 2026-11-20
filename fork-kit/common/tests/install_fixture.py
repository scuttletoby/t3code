"""Shared by the installer tests of every kit that uses fork-kit/common/kit_install.py: load a kit's installer (or a
copy with one text broken, in it or in kit_install.py) on a fake host in a temporary folder, every fixed path under
PREFIX and the current user as root. No root, no systemd, no network."""
import contextlib, importlib.util, io, os, shutil, subprocess, sys, tempfile, unittest

FORK_KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMON = os.path.join(FORK_KIT, 'common')
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
        self.bootstrap = os.path.join(self.root, 'bootstrap.sh')
        with open(self.bootstrap, 'w', encoding='utf-8') as f:
            f.write(source_of(BOOTSTRAP, self.broken[1:] if where == 'bootstrap' else None))

    def close(self):
        os.umask(self.umask)
        shutil.rmtree(self.root, ignore_errors=True)

    def path(self, p):
        return self.root + p

    def run_main(self, *argv, stop_after=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.mod.main(list(argv), stop_after=stop_after)
            except SystemExit as e:
                code = str(e)
        return code, out.getvalue(), err.getvalue()

    def install(self, *extra, sha=SHA1, stop_after=None, source=None):
        return self.run_main('install', '--source', source or self.KIT_DIR, '--sha', sha, *extra,
                             stop_after=stop_after)

    def copy_kit(self):
        """A source folder of the kit (and its common/ beside it) the test may change."""
        src = os.path.join(self.root, 'src')
        shutil.copytree(self.KIT_DIR, src, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(COMMON, os.path.join(self.root, 'common'), ignore=shutil.ignore_patterns('__pycache__'))
        return src

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
            case.assertFalse([e for e in result.errors if 'exactly once' in e[1]], f'{rule}: text not found')
