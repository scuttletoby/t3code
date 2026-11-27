"""Contract tests for fork-kit/common/machine_file.py: every rule of the machine file (README.md next to it).
No root: the tests pass a path and the uid that must own it. RedCheck breaks each file-safety rule in a copy of
machine_file.py and shows its test then fails.

  python3 -I -B fork-kit/common/tests/machine_file.test.py
"""
import importlib.machinery, importlib.util, json, os, shutil, tempfile, types, unittest
from unittest import mock

COMMON = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(source=None):
    path = os.path.join(COMMON, 'machine_file.py')
    if source is not None:
        mod = types.ModuleType('machine_file')
        exec(compile(source, path, 'exec'), mod.__dict__)
        return mod
    loader = importlib.machinery.SourceFileLoader('machine_file', path)
    spec = importlib.util.spec_from_loader('machine_file', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


mf = load()
GOOD = {'version': 1, 'heavy': {'sweep': {'stop': True}}, 'homelab': {'anything': [1, 2]}}


class MachineFile(unittest.TestCase):
    mf = mf

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
        return self.mf.read_section(kit, self.path, os.getuid() if uid is None else uid)

    def refused(self, *words, kit='heavy', uid=None):
        with self.assertRaises(self.mf.MachineFileError) as cm:
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
        for bad, words in (({}, ['"version"']), ({'version': '1'}, ['whole number']),
                           ({'version': True}, ['whole number']),
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

    def test_optional_section_is_none_only_when_missing(self):
        opt = lambda path=None: self.mf.read_section('heavy', path or self.path, os.getuid(), optional=True)
        self.assertIsNone(opt())                                        # no file
        self.assertIsNone(opt(os.path.join(self.dir, 'no-folder', 'machine.json')))
        self.write({'version': 1, 'homelab': {}})
        self.assertIsNone(opt())                                        # no section
        self.write({'version': 1, 'heavy': {'queue': {}}})
        self.assertEqual(opt(), {'queue': {}})
        self.write({'version': 1, 'heavy': []})
        with self.assertRaises(self.mf.MachineFileError):
            opt()
        self.write({'version': 1, 'homelab': {}}, mode=0o666)            # every other rule still refuses
        with self.assertRaises(self.mf.MachineFileError):
            opt()

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

    def test_symbolic_links_are_refused(self):
        # a link in a root-owned folder could point into a folder anyone can write (#183 security finding)
        shared = os.path.join(self.dir, 'shared')
        os.mkdir(shared)
        os.chmod(shared, 0o777)
        real = os.path.join(shared, 'machine.json')
        with open(real, 'w') as f:
            json.dump(GOOD, f)
        os.chmod(real, 0o644)
        os.symlink(real, self.path)
        self.refused('is a symbolic link')
        os.unlink(self.path)
        folder = os.path.join(self.dir, 'real-folder')
        os.mkdir(folder, 0o755)
        shutil.copy(real, os.path.join(folder, 'machine.json'))
        link = os.path.join(self.dir, 'linked-folder')
        os.symlink(folder, link)
        self.path = os.path.join(link, 'machine.json')
        self.refused('is a symbolic link')

    def test_the_opened_file_is_checked_not_the_path(self):
        # swapped for a file anyone can write between a check of the path and the open: the open file is checked
        self.write(GOOD)
        real_open = os.open

        def swap_then_open(path, *args, **kw):
            if path == os.path.basename(self.path) and kw.get('dir_fd') is not None:   # the file, by its folder
                self.write({'version': 1, 'heavy': {'sweep': {'stop': 'evil'}}}, mode=0o666)
            return real_open(path, *args, **kw)
        with mock.patch.object(self.mf.os, 'open', swap_then_open):
            self.refused('writable by group or others')

    def test_every_folder_above_is_safe(self):
        deep = os.path.join(self.dir, 'a', 'b')
        os.makedirs(deep, 0o755)
        self.path = os.path.join(deep, 'machine.json')
        self.write(GOOD)
        self.assertEqual(self.read(), GOOD['heavy'])
        above = os.path.join(self.dir, 'a')
        os.chmod(above, 0o777)                               # anyone could rename b and put their own in its place
        self.assertIn(f'{above} is writable by group or others', self.refused())
        os.chmod(above, 0o1777)                              # sticky, like /tmp: nobody else can rename b
        self.assertEqual(self.read(), GOOD['heavy'])
        os.chmod(above, 0o755)
        self.assertIn(f'{self.dir} is owned by uid', self.refused(uid=os.getuid() + 1))
        os.rename(above, above + '-real')
        os.symlink(above + '-real', above)
        self.assertIn(f'{above} is a symbolic link', self.refused())
        os.unlink(above)
        with open(above, 'w') as f:
            f.write('')
        self.assertIn(f'{above} is not a folder', self.refused())

    def test_only_a_plain_file_of_at_most_1_mib(self):
        os.mkfifo(self.path, 0o644)                    # would block a plain open
        self.refused('not a plain file')
        os.unlink(self.path)
        os.mkdir(self.path, 0o755)
        self.refused('not a plain file')
        os.rmdir(self.path)
        self.write('{"version": 1, "heavy": {}, "pad": "' + 'x' * (1 << 20) + '"}')
        self.refused('larger than')
        with open(self.path, 'wb') as f:
            f.write(b'{"version": 1, "heavy": {"x": "\xff"}}')
        self.refused('not UTF-8')

    def test_deep_json_is_refused_cleanly(self):
        self.write('{"version": 1, "heavy": ' + '[' * 200000 + ']' * 200000 + '}')
        self.refused('nested too deeply')

    def test_a_key_given_twice_is_refused(self):
        self.write('{"version": 1, "heavy": {"sweep": {"stop": false, "stop": true}}}')
        self.refused("'stop' is given twice")
        self.write('{"version": 1, "heavy": {}, "version": 2}')
        self.refused("'version' is given twice")

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

    def test_windows_is_refused_until_its_checks_exist(self):
        # #193: owner, mode and link checks are POSIX only; no kit reads the file on Windows yet
        self.write(GOOD)
        with mock.patch.object(self.mf.os, 'name', 'nt'):
            self.refused('Windows')

    def test_default_paths(self):
        self.assertEqual(mf.default_path('linux'), '/etc/fork-kit/machine.json')
        self.assertEqual(mf.default_path('darwin'), '/Library/Application Support/fork-kit/machine.json')
        # the fixed folder, never %ProgramData% from the environment, which any caller can point elsewhere
        from unittest import mock
        with mock.patch.dict(os.environ, {'ProgramData': self.dir}):
            self.assertEqual(mf.default_path('win32'), r'C:\ProgramData\fork-kit\machine.json')
        with self.assertRaises(mf.MachineFileError):
            mf.default_path('plan9')


# Each file-safety rule broken on purpose in a copy of machine_file.py: (text, broken text, the test that must fail)
MUTATIONS = {
    'optional: a missing file is None': ('        if optional:        # the file or a folder', '        if False:  #',
                                         'test_optional_section_is_none_only_when_missing'),
    'optional: a missing section is None': ('            if optional:\n                return None\n'
                                            '            if self.data is None', '            if self.data is None',
                                            'test_optional_section_is_none_only_when_missing'),
    'folders above are safe': ('            _check_folder_on_the_way(path, where, os.fstat(fd), owner_uid)\n', '',
                               'test_every_folder_above_is_safe'),
    'sticky folders above are fine': ('and not st.st_mode & stat.S_ISVTX', '', 'test_every_folder_above_is_safe'),
    'no linked folder followed': ('os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW', 'os.O_RDONLY | os.O_DIRECTORY',
                                  'test_every_folder_above_is_safe'),
    'no link followed': ('os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK', 'os.O_RDONLY | os.O_NONBLOCK',
                         'test_symbolic_links_are_refused'),
    'protected text: owner checked': ('        _check_owner(path, where, os.fstat(fd), owner_uid)\n', '',
                                      'test_writable_by_others_the_file_or_its_folder'),
    'protected text: no dots': ("any(p in ('.', '..') for p in parts)", 'False',
                                'test_only_an_absolute_path_without_dots'),
    'opened file checked': ('        _check_owner(path, path, st, owner_uid)\n', '',
                            'test_the_opened_file_is_checked_not_the_path'),
    'plain file only': ('if not stat.S_ISREG(st.st_mode):', 'if False:', 'test_only_a_plain_file_of_at_most_1_mib'),
    'size limit': ('if len(data) > MAX_BYTES:', 'if False:', 'test_only_a_plain_file_of_at_most_1_mib'),
    'deep JSON': ('    except RecursionError:\n', '    except ZeroDivisionError:\n',
                  'test_deep_json_is_refused_cleanly'),
    'no key twice': ('json.loads(text, object_pairs_hook=_no_duplicates)', 'json.loads(text)',
                     'test_a_key_given_twice_is_refused'),
    'Windows refused': ("    if os.name == 'nt':\n", "    if False:\n", 'test_windows_is_refused_until_its_checks_exist'),
}


class ProtectedText(unittest.TestCase):
    """read_protected_text: another root-owned file a kit trusts (a policy file), under the machine file's rules."""
    mf = mf

    def setUp(self):
        self.umask = os.umask(0o022)
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix='protected-text-test-'))
        os.chmod(self.dir, 0o755)
        self.path = os.path.join(self.dir, 'policy.json')
        self.addCleanup(self.close)
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('{"any": "text"}')
        os.chmod(self.path, 0o644)

    def close(self):
        os.umask(self.umask)
        shutil.rmtree(self.dir, ignore_errors=True)

    def refused(self, *words, path=None, uid=None):
        path = path or self.path
        with self.assertRaises(self.mf.MachineFileError) as cm:
            self.mf.read_protected_text(path, os.getuid() if uid is None else uid)
        for w in words:
            self.assertIn(w, str(cm.exception))
        self.assertIn(path, str(cm.exception))
        return str(cm.exception)

    def test_the_text_is_returned_as_it_is(self):
        self.assertEqual(self.mf.read_protected_text(self.path, os.getuid()), '{"any": "text"}')

    def test_not_there(self):
        self.refused('not there', path=os.path.join(self.dir, 'gone.json'))
        self.refused('not there', path=os.path.join(self.dir, 'gone', 'policy.json'))

    def test_not_root_owned(self):
        self.refused('owned by uid', f'not {os.getuid() + 1} (root)', uid=os.getuid() + 1)

    def test_writable_by_others_the_file_or_its_folder(self):
        os.chmod(self.path, 0o664)
        self.refused('writable by group or others')
        os.chmod(self.path, 0o644)
        os.chmod(self.dir, 0o775)
        self.refused(f'{self.dir} is writable by group or others')

    def test_a_link_the_file_or_a_folder_above(self):
        link = os.path.join(self.dir, 'link.json')
        os.symlink(self.path, link)
        self.refused('is a symbolic link', path=link)
        os.symlink(self.dir, self.dir + '-link')
        self.addCleanup(os.unlink, self.dir + '-link')
        self.refused('is a symbolic link', path=os.path.join(self.dir + '-link', 'policy.json'))

    def test_only_an_absolute_path_without_dots(self):
        for path in ('policy.json', os.path.join(self.dir, '..', os.path.basename(self.dir), 'policy.json'),
                     os.path.join(self.dir, '.', 'policy.json'), self.dir + '/'):
            with self.subTest(path=path):
                self.refused('must be an absolute path', path=path)

    def test_not_a_plain_file_or_not_utf8(self):
        os.unlink(self.path)
        os.mkdir(self.path, 0o755)
        self.refused('not a plain file')
        os.rmdir(self.path)
        with open(self.path, 'wb') as f:
            f.write(b'\xff')
        os.chmod(self.path, 0o644)
        self.refused('not UTF-8')


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        with open(os.path.join(COMMON, 'machine_file.py')) as f:
            source = f.read()
        for rule, (text, broken, test) in MUTATIONS.items():
            with self.subTest(rule=rule):
                self.assertEqual(source.count(text), 1, f'the red check for {rule!r} no longer matches the code')
                base = ProtectedText if test in vars(ProtectedText) else MachineFile
                cls = type('Broken', (base,), {'mf': load(source.replace(text, broken))})
                result = unittest.TestResult()
                cls(test).run(result)
                self.assertFalse(result.wasSuccessful(), f'{test} still passes with the rule {rule!r} broken')


if __name__ == '__main__':
    unittest.main(verbosity=2)
