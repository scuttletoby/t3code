"""Shared by the homelab tests: a machine file and a lease folder of their own, a fixed clock, and RedCheck. No
root, no network beyond 127.0.0.1.

The kit reads the machine file at the fixed path; a test points config.MACHINE_PATH at a file in a temporary folder
owned by the user who runs it (config.OWNER_UID). RedCheck copies lib/ to a temporary folder, breaks one text in
one module, and runs one named test against that copy (HOMELAB_TEST_LIB names it). The test must then fail."""
import contextlib, io, json, os, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

sys.dont_write_bytecode = True

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB_ENV = 'HOMELAB_TEST_LIB'
LIB = os.environ.get(LIB_ENV) or os.path.join(KIT, 'lib')
sys.path.insert(0, LIB)

from homelab import check, cli, config, lease  # noqa: E402,F401

NOW = 1795766400.0          # 2026-11-27T08:00:00Z
TEST_BOX = {'name': 'test-box', 'host': 'test-box.example', 'roles': ['test'],
            'lease': {'defaultMinutes': 60, 'maxMinutes': 240}}
BUILD_BOX = {'name': 'build-box', 'host': '192.0.2.20', 'roles': ['build']}


class Temp(unittest.TestCase):
    """A temporary folder with a lease folder in it, and the machine file path pointed there (not yet written)."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='homelab-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.leases = os.path.join(self.tmp, 'leases')
        os.makedirs(self.leases)
        self.machine = os.path.join(self.tmp, 'machine.json')
        for p in (mock.patch.object(config, 'MACHINE_PATH', self.machine),
                  mock.patch.object(config, 'OWNER_UID', os.getuid())):
            p.start()
            self.addCleanup(p.stop)

    def write_machine(self, *machines, **section):
        machines = machines or (TEST_BOX, BUILD_BOX)
        data = {'version': 1, 'homelab': dict({'leaseDir': self.leases, 'machines': list(machines)}, **section)}
        with open(self.machine, 'w') as f:
            json.dump(data, f)
        os.chmod(self.machine, 0o644)

    def run_cli(self, *argv, now=NOW, **kw):
        """(exit code, stdout, stderr) of cli.main in this process."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), now=now, **kw)
        return code, out.getvalue(), err.getvalue()

    def lease_file(self, name='test-box'):
        with open(os.path.join(self.leases, f'{name}.json')) as f:
            return json.load(f)


def red_check(case, test_file, mutations):
    """mutations: {rule: (module, text, broken text, 'Class.test_name')}."""
    for rule, (module, text, broken, test) in mutations.items():
        with case.subTest(rule=rule):
            tmp = tempfile.mkdtemp(prefix='homelab-red-')
            try:
                lib = os.path.join(tmp, 'lib')
                shutil.copytree(os.path.join(KIT, 'lib'), lib, ignore=shutil.ignore_patterns('__pycache__'))
                os.makedirs(os.path.join(tmp, 'common'))      # where config finds machine_file in an installed copy
                shutil.copy(os.path.join(os.path.dirname(KIT), 'common', 'machine_file.py'),
                            os.path.join(tmp, 'common'))
                path = os.path.join(lib, 'homelab', f'{module}.py')
                with open(path, encoding='utf-8') as f:
                    source = f.read()
                case.assertEqual(source.count(text), 1, f'{rule}: the text is not in {module} exactly once')
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(source.replace(text, broken))
                r = subprocess.run([sys.executable, '-I', '-B', test_file, test], capture_output=True, text=True,
                                   timeout=120, env=dict(os.environ, **{LIB_ENV: lib}), stdin=subprocess.DEVNULL)
                case.assertIn('Ran 1 test', r.stderr, f'{rule}: {test} did not run:\n{r.stderr[-800:]}')
                case.assertNotEqual(r.returncode, 0, f'{test} still passes with {rule!r} broken')
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
