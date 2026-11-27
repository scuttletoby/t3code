"""Shared fixture of the homelab kit's tests: a machine file and a lease folder in a temporary folder (no root; the
tests pass the machine file's path and the uid that must own it), and `run(argv)`, which calls the command's main()
and returns (exit code, stdout, stderr)."""
import contextlib, io, json, os, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMON = os.path.join(os.path.dirname(KIT), 'common')
LIB_ENV = 'HOMELAB_TEST_LIB'        # red_check: a broken copy of lib/ to test instead
sys.path.insert(0, os.environ.get(LIB_ENV) or os.path.join(KIT, 'lib'))

from homelabkit import leases, main  # noqa: E402


class Fixture(unittest.TestCase):
    def setUp(self):
        self.umask = os.umask(0o022)
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix='homelab-test-'))
        os.chmod(self.dir, 0o755)
        self.machine_path = os.path.join(self.dir, 'machine.json')
        self.lease_dir = os.path.join(self.dir, 'leases')
        os.mkdir(self.lease_dir, 0o755)
        self.clock = [1_800_000_000.0]
        patches = [mock.patch.object(main, 'MACHINE_PATH', self.machine_path),
                   mock.patch.object(main, 'OWNER_UID', os.getuid()),
                   mock.patch.object(leases, 'now', lambda: self.clock[0])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.close)

    def close(self):
        os.umask(self.umask)
        shutil.rmtree(self.dir, ignore_errors=True)

    def section(self, **over):
        s = {'leases': {'dir': self.lease_dir, 'defaultMinutes': 60, 'maxMinutes': 240},
             'machines': [{'name': 'test-a', 'host': '127.0.0.1', 'port': 9},
                          {'name': 'test-b', 'host': '127.0.0.1', 'port': 9}]}
        s.update(over)
        return s

    def write_machine_file(self, section=None, mode=0o644, data=None):
        data = data if data is not None else {'version': 1, 'homelab': section if section is not None
                                              else self.section()}
        with open(self.machine_path, 'w') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        os.chmod(self.machine_path, mode)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main.main(list(argv))
            except SystemExit as e:          # argparse
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def lease_file(self, machine):
        path = os.path.join(self.lease_dir, f'{machine}.json')
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)


def red_check(case, test_file, mutations):
    """mutations: {rule: (module, text, broken text, 'Class.test_name')}. Each rule is broken in a copy of lib/
    (with common/machine_file.py beside it, where the kit finds it once installed), and its test must then fail."""
    for rule, (module, text, broken, test) in mutations.items():
        with case.subTest(rule=rule):
            tmp = tempfile.mkdtemp(prefix='homelab-red-')
            try:
                lib = os.path.join(tmp, 'lib')
                shutil.copytree(os.path.join(KIT, 'lib'), lib, ignore=shutil.ignore_patterns('__pycache__'))
                os.mkdir(os.path.join(tmp, 'common'))
                shutil.copy(os.path.join(COMMON, 'machine_file.py'), os.path.join(tmp, 'common'))
                path = os.path.join(lib, 'homelabkit', f'{module}.py')
                with open(path, encoding='utf-8') as f:
                    source = f.read()
                case.assertEqual(source.count(text), 1, f'{rule}: the text is not in {module}.py exactly once')
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(source.replace(text, broken))
                r = subprocess.run([sys.executable, '-I', '-B', test_file, test], capture_output=True, text=True,
                                   timeout=120, env=dict(os.environ, **{LIB_ENV: lib}), stdin=subprocess.DEVNULL)
                case.assertIn('Ran 1 test', r.stderr, f'{rule}: {test} did not run:\n{r.stderr[-800:]}')
                case.assertNotEqual(r.returncode, 0, f'{test} still passes with {rule!r} broken')
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
