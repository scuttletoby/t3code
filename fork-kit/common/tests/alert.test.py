"""Contract tests for fork-kit/common/alert.py: the alert channel the kits of a machine share (README.md next to it,
"The alert channel"). No root: the tests pass a path and the uid that must own it. RedCheck breaks each rule in a copy
of alert.py and shows its test then fails.

  python3 -I -B fork-kit/common/tests/alert.test.py
"""
import importlib.machinery, importlib.util, json, os, shutil, sys, tempfile, types, unittest

COMMON = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, COMMON)
import machine_file  # noqa: E402

# a made-up GitHub-style token, built at run time so the source holds no token-shaped string
FAKE_TOKEN = 'gh' + 'p_' + 'Example0Not0A0Real0Token0Value00'


def load(source=None):
    path = os.path.join(COMMON, 'alert.py')
    if source is not None:
        mod = types.ModuleType('alert')
        exec(compile(source, path, 'exec'), mod.__dict__)
        return mod
    loader = importlib.machinery.SourceFileLoader('alert', path)
    spec = importlib.util.spec_from_loader('alert', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Alert(unittest.TestCase):
    alert = load()

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='alert-test-')
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        os.chmod(self.dir, 0o755)
        self.path = os.path.join(self.dir, 'machine.json')

    def write(self, content):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(dict(content, version=1), f)
        os.chmod(self.path, 0o644)

    def read(self):
        return self.alert.read(path=self.path, owner_uid=os.getuid())

    def test_no_machine_file_or_no_section_is_no_channel(self):
        self.assertIsNone(self.read())                       # no file at all
        self.write({'update-policy': {}})
        self.assertIsNone(self.read())                       # a file without the section

    def test_a_section_is_read_with_its_defaults(self):
        self.write({'alert': {'command': ['/usr/local/bin/notify', '--topic', 'machine']}})
        self.assertEqual(self.read(), {'command': ['/usr/local/bin/notify', '--topic', 'machine'],
                                       'timeoutSeconds': 60})
        self.write({'alert': {'command': ['/usr/local/bin/notify'], 'timeoutSeconds': 120}})
        self.assertEqual(self.read()['timeoutSeconds'], 120)

    def test_a_broken_section_is_refused_never_taken_for_no_channel(self):
        for bad, words in (({}, '"command" is missing'), ({'command': []}, 'must name a program'),
                           ({'command': ['notify']}, 'absolute path'), ({'command': ['/x', 3]}, 'list of strings'),
                           ({'command': ['/x', 'a\0b']}, 'list of strings'),
                           ({'command': ['/x'], 'timeoutSeconds': 4}, 'from 5 to 120'),
                           ({'command': ['/x'], 'timeoutSeconds': 600}, 'from 5 to 120'),
                           ({'command': ['/x'], 'timeoutSeconds': True}, 'must be int'),
                           ({'command': ['/x'], 'retries': 2}, 'unknown key'),
                           ({'command': '/usr/local/bin/notify'}, 'must be list'),
                           ({'command': ['/usr/bin/notify', '--token', FAKE_TOKEN]}, 'token'),
                           ({'command': ['/usr/bin/curl', '-H', 'Authorization: Bearer ' + 'aB3' * 12]},
                            'root-only file')):
            with self.subTest(bad=bad), self.assertRaises(machine_file.MachineFileError) as e:
                self.write({'alert': bad})
                self.read()
            self.assertIn(words, str(e.exception))
            self.assertIn(': alert', str(e.exception))         # names the file and the section

    def test_plain_arguments_are_fine(self):
        self.assertIsNone(self.alert.command_problem(
            ['/usr/local/bin/notify', '--topic', 'updates', 'https://ntfy.example/updates',
             '--key-file=/etc/notify/key', '--title=a-very-long-machine-name-that-is-not-a-token-01']))

    def test_the_environment_is_plain_and_names_the_kit_event_and_host(self):
        env = self.alert.environment('update-policy', 'drift')
        self.assertEqual(sorted(env), ['FORK_KIT_ALERT_EVENT', 'FORK_KIT_ALERT_HOST', 'FORK_KIT_ALERT_KIT', 'LANG',
                                       'PATH'])
        self.assertEqual((env['FORK_KIT_ALERT_KIT'], env['FORK_KIT_ALERT_EVENT']), ('update-policy', 'drift'))
        self.assertTrue(env['FORK_KIT_ALERT_HOST'])
        self.assertNotIn(':/home', env['PATH'])
        for bad in ('', 'Drift', 'drift now', 'drift\n', 'a' * 65, None):
            with self.subTest(event=bad), self.assertRaises(ValueError):
                self.alert.environment('update-policy', bad)

    def test_the_example_machine_file_has_a_channel_that_parses(self):
        with open(os.path.join(COMMON, 'machine.example.json'), encoding='utf-8') as f:
            example = json.load(f)
        self.write({'alert': example['alert']})
        self.assertTrue(os.path.isabs(self.read()['command'][0]))


MUTATIONS = {
    'no section is no channel': (
        "    return None if section is None else machine_file", "    return machine_file",
        'test_no_machine_file_or_no_section_is_no_channel'),
    'the section is checked': (
        "else machine_file.fields(section, SPEC, ", "else section or machine_file.fields(section, SPEC, ",
        'test_a_broken_section_is_refused_never_taken_for_no_channel'),
    'token-like arguments are refused': (
        "        if any(r.search(x) for r in TOKEN_RES):", "        if False:",
        'test_a_broken_section_is_refused_never_taken_for_no_channel'),
    'the program is an absolute path': (
        "    return None if os.path.isabs(value[0]) else", "    return None if True else",
        'test_a_broken_section_is_refused_never_taken_for_no_channel'),
    'the event is checked': (
        "    if not EVENT_RE.match(event or ''):", "    if False:",
        'test_the_environment_is_plain_and_names_the_kit_event_and_host'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        with open(os.path.join(COMMON, 'alert.py'), encoding='utf-8') as f:
            source = f.read()
        for rule, (text, broken, test) in MUTATIONS.items():
            with self.subTest(rule=rule):
                self.assertEqual(source.count(text), 1, f'the red check for {rule!r} no longer matches the code')
                cls = type('Broken', (Alert,), {'alert': load(source.replace(text, broken))})
                result = unittest.TestResult()
                cls(test).run(result)
                self.assertFalse(result.wasSuccessful(), f'{test} still passes with the rule {rule!r} broken')


if __name__ == '__main__':
    unittest.main(verbosity=2)
