"""Contract tests for fork-kit/common/machine_file.py: every rule of the machine file (README.md next to it).
No root: the tests pass a path and the uid that must own it.

  python3 -I -B fork-kit/common/tests/machine_file.test.py
"""
import importlib.machinery, importlib.util, json, os, shutil, tempfile, unittest

COMMON = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load():
    path = os.path.join(COMMON, 'machine_file.py')
    loader = importlib.machinery.SourceFileLoader('machine_file', path)
    spec = importlib.util.spec_from_loader('machine_file', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


mf = load()
GOOD = {'version': 1, 'heavy': {'sweep': {'stop': True}}, 'homelab': {'anything': [1, 2]}}


class MachineFile(unittest.TestCase):
    def setUp(self):
        self.umask = os.umask(0o022)
        self.dir = tempfile.mkdtemp(prefix='machine-file-test-')
        os.chmod(self.dir, 0o755)
        self.path = os.path.join(self.dir, 'machine.json')
        self.addCleanup(self.close)

    def close(self):
        os.umask(self.umask)
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, data, mode=0o644):
        with open(self.path, 'w') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        os.chmod(self.path, mode)

    def read(self, kit='heavy', uid=None):
        return mf.read_section(kit, self.path, os.getuid() if uid is None else uid)

    def refused(self, *words, kit='heavy', uid=None):
        with self.assertRaises(mf.MachineFileError) as cm:
            self.read(kit, uid)
        for w in words:
            self.assertIn(w, str(cm.exception))
        self.assertIn(self.path, str(cm.exception))      # every refusal names the file
        return str(cm.exception)

    def test_own_section_is_returned(self):
        self.write(GOOD)
        self.assertEqual(self.read(), {'sweep': {'stop': True}})

    def test_missing_file(self):
        self.refused('not there')

    def test_broken_json_and_not_an_object(self):
        self.write('{"version": 1, ')
        self.refused('not valid JSON')
        self.write('[1]')
        self.refused('JSON object')

    def test_version_rules(self):
        for bad, words in (({}, ['"version"']), ({'version': '1'}, ['whole number']), ({'version': True}, ['whole number']),
                           ({'version': 1.0}, ['whole number']), ({'version': 0}, ['bad version']),
                           ({'version': 2}, ['newer', 'update the kit'])):
            with self.subTest(bad=bad):
                self.write(dict(bad, heavy={}))
                self.refused(*words)

    def test_missing_or_bad_section(self):
        self.write({'version': 1, 'homelab': {}})
        self.refused('no "heavy" section')
        self.write({'version': 1, 'heavy': []})
        self.refused('"heavy" must be a JSON object')

    def test_other_sections_are_not_read(self):
        self.write({'version': 1, 'heavy': {}, 'homelab': 'not even an object', 'futureKit': {'x': 1}})
        self.assertEqual(self.read(), {})

    def test_owner_and_mode(self):
        self.write(GOOD)
        self.refused('owned by uid', uid=os.getuid() + 1)
        self.write(GOOD, mode=0o664)
        self.refused('writable by group or others')
        self.write(GOOD, mode=0o646)
        self.refused('writable by group or others')
        self.write(GOOD)
        os.chmod(self.dir, 0o777)
        self.refused('writable by group or others')
        os.chmod(self.dir, 0o755)
        self.assertEqual(self.read(), GOOD['heavy'])

    def test_secrets_only_as_file_references(self):
        for key in ('token', 'apiToken', 'password', 'clientSecret', 'privateKey', 'apiKey'):
            with self.subTest(key=key):
                self.write({'version': 1, 'heavy': {'nested': [{key: 'abc123'}]}})
                self.refused(key, 'file:<path>')
                self.write({'version': 1, 'heavy': {'nested': [{key: 'file:/etc/keys/x'}]}})
                self.read()

    def test_fields_defaults_types_and_unknown_keys(self):
        spec = {'stop': (bool, False, None), 'age': ((int, float), 600, lambda v: None if v >= 60 else 'too small'),
                'name': (str, mf.REQUIRED, None)}
        self.assertEqual(mf.fields({'name': 'x'}, spec, 'w'), {'stop': False, 'age': 600, 'name': 'x'})
        self.assertEqual(mf.fields({'name': 'x', 'age': 61.5, 'stop': True}, spec, 'w')['age'], 61.5)
        for obj, words in (({}, ['"name" is missing']), ({'name': 'x', 'other': 1}, ["unknown key 'other'", 'known:']),
                           ({'name': 'x', 'stop': 'yes'}, ['w.stop', 'bool']), ({'name': 'x', 'age': True}, ['w.age']),
                           ({'name': 'x', 'age': 5}, ['too small']), ([], ['JSON object'])):
            with self.subTest(obj=obj):
                with self.assertRaises(mf.MachineFileError) as cm:
                    mf.fields(obj, spec, 'w')
                for w in words:
                    self.assertIn(w, str(cm.exception))

    def test_example_file_follows_the_rules(self):
        shutil.copy(os.path.join(COMMON, 'machine.example.json'), self.path)
        os.chmod(self.path, 0o644)
        with open(self.path) as f:
            data = json.load(f)
        for kit in data:
            if kit != 'version':
                self.assertIsInstance(self.read(kit), dict)

    def test_default_paths(self):
        self.assertEqual(mf.default_path('linux'), '/etc/fork-kit/machine.json')
        self.assertEqual(mf.default_path('darwin'), '/Library/Application Support/fork-kit/machine.json')
        self.assertTrue(mf.default_path('win32').endswith(os.path.join('fork-kit', 'machine.json')))
        with self.assertRaises(mf.MachineFileError):
            mf.default_path('plan9')


if __name__ == '__main__':
    unittest.main(verbosity=2)
