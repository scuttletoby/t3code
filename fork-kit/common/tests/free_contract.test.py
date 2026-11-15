"""Contract tests for free/v1 (fork-kit/common/FREE.md): free_contract.py against free-fixtures.json (the cases
every reader shares), running real scripts (time limit, output limit, process group, safety), its command line,
and the example scripts in free/.

  python3 -I -B fork-kit/common/tests/free_contract.test.py
"""
import datetime, importlib.machinery, importlib.util, json, os, shutil, subprocess, sys, tempfile, time, unittest

COMMON = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READER = os.path.join(COMMON, 'free_contract.py')


def load():
    loader = importlib.machinery.SourceFileLoader('free_contract', READER)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader('free_contract', loader))
    loader.exec_module(mod)
    return mod


fc = load()


def now_text(delta=0):
    t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delta)
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


class Fixtures(unittest.TestCase):
    def test_every_shared_case(self):
        with open(os.path.join(COMMON, 'free-fixtures.json'), encoding='utf-8') as f:
            data = json.load(f)
        now = fc.parse_at(data['now'])
        self.assertGreater(len(data['cases']), 20)
        for case in data['cases']:
            with self.subTest(case['name']):
                out = case['stdout']
                if 'padTo' in case:
                    out = out.replace('PAD', 'x' * (case['padTo'] - len(out) + 3))
                    self.assertEqual(len(out), case['padTo'])
                verdict, reason, facts = fc.judge(case['exit'], out.encode(), now, case.get('needMb'))
                self.assertEqual(verdict, case['verdict'], reason)
                self.assertEqual(bool(reason), verdict != 'free')
                self.assertEqual(facts is None, verdict == 'unavailable')


class Ask(unittest.TestCase):
    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp(prefix='free-contract-test-'))
        self.addCleanup(shutil.rmtree, self.d, True)

    def script(self, body, name='free', mode=0o755):
        path = os.path.join(self.d, name)
        with open(path, 'w') as f:
            f.write('#!/bin/sh\n' + body)
        os.chmod(path, mode)
        return path

    def test_free_busy_and_the_environment_it_gets(self):
        s = self.script(f'env > {self.d}/env; pwd > {self.d}/pwd; echo "$#" > {self.d}/argc\n'
                        f'echo \'{{"v": 1, "free": true, "at": "{now_text()}"}}\'\n')
        self.assertEqual(fc.ask(s)[0], 'free')
        with open(f'{self.d}/env') as f:
            names = sorted(line.split('=', 1)[0] for line in f.read().splitlines())
        self.assertEqual([n for n in names if n not in ('PWD', 'SHLVL', '_', 'OLDPWD')], ['LANG', 'PATH'])
        with open(f'{self.d}/pwd') as f, open(f'{self.d}/argc') as g:
            self.assertEqual((f.read(), g.read()), ('/\n', '0\n'))
        busy = f'{{"v": 1, "free": false, "reason": "a game\\nis on", "at": "{now_text()}"}}'
        s = self.script(f'printf "%s\\n" \'{busy}\'\nexit 75\n')   # not echo: in dash it turns \\n into a newline
        self.assertEqual(fc.ask(s)[:2], ('busy', 'a game is on'))

    def test_no_answer_in_time_ends_its_whole_group(self):
        s = self.script(f'sleep 30 & echo $! > {self.d}/child\nsleep 30\n')
        t = time.monotonic()
        verdict, reason, _ = fc.ask(s, timeout=1)
        self.assertLess(time.monotonic() - t, 10)
        self.assertEqual(verdict, 'unavailable')
        self.assertIn('no answer', reason)
        with open(f'{self.d}/child') as f:
            child = int(f.read())
        for _ in range(50):
            try:
                with open(f'/proc/{child}/stat') as f:
                    if f.read().rsplit(')', 1)[1].split()[0] == 'Z':
                        break
            except (FileNotFoundError, ProcessLookupError):   # gone between the open and the read
                break
            time.sleep(0.1)
        else:
            self.fail('the script\'s background child still runs')

    def test_a_child_that_keeps_stdout_open_is_no_answer(self):
        s = self.script(f'(sleep 30) &\necho \'{{"v": 1, "free": true, "at": "{now_text()}"}}\'\n')
        self.assertEqual(fc.ask(s, timeout=1)[0], 'unavailable')

    def test_endless_output_is_cut_off(self):
        s = self.script('yes\n')
        verdict, reason, _ = fc.ask(s, timeout=5)
        self.assertEqual(verdict, 'unavailable')
        self.assertIn('4096 bytes', reason)

    def test_unsafe_scripts_never_run(self):
        mark = os.path.join(self.d, 'ran')
        body = f': > {mark}\necho \'{{"v": 1, "free": true, "at": "{now_text()}"}}\'\n'
        cases = {'relative': lambda: 'free',
                 'dot-dot': lambda: self.script(body).replace('/free', '/../' + os.path.basename(self.d) + '/free'),
                 'writable by others': lambda: self.script(body, mode=0o757),
                 'not executable': lambda: self.script(body, mode=0o644),
                 'a folder': lambda: self.d,
                 'missing': lambda: os.path.join(self.d, 'nothing')}
        for name, make in cases.items():
            with self.subTest(name):
                verdict, reason, _ = fc.ask(make())
                self.assertEqual(verdict, 'unavailable')
                self.assertFalse(os.path.exists(mark), reason)
        os.chmod(self.d, 0o777)   # a folder others may change, no sticky bit
        try:
            self.assertIn('may be changed by others', fc.ask(self.script(body))[1])
        finally:
            os.chmod(self.d, 0o700)
        self.assertFalse(os.path.exists(mark))
        self.assertIn('belongs to uid', fc.unsafe(self.script(body), uid=os.geteuid() + 12345) or '')
        os.chmod(self.d, 0o1777)   # sticky, like /tmp: fine
        self.assertEqual(fc.ask(self.script(body))[0], 'free')

    def test_a_link_is_judged_by_where_it_leads(self):
        s = self.script(f'echo \'{{"v": 1, "free": true, "at": "{now_text()}"}}\'\n')
        os.symlink(s, os.path.join(self.d, 'link'))
        self.assertEqual(fc.ask(os.path.join(self.d, 'link'))[0], 'free')

    def test_a_link_repointed_after_the_check_does_not_change_what_runs(self):
        good = self.script(f': > {self.d}/good-ran\necho \'{{"v": 1, "free": true, "at": "{now_text()}"}}\'\n',
                           name='good')
        bad = self.script(f': > {self.d}/bad-ran\necho \'{{"v": 1, "free": true, "at": "{now_text()}"}}\'\n',
                          name='bad')
        link = os.path.join(self.d, 'link')
        os.symlink(good, link)
        checked = fc.unsafe

        def check_then_repoint(command, uid=None):
            why = checked(command, uid)
            os.unlink(link)
            os.symlink(bad, link)   # what another user could do in a folder they may change
            return why
        fc.unsafe = check_then_repoint
        try:
            self.assertEqual(fc.ask(link)[0], 'free')
        finally:
            fc.unsafe = checked
        self.assertEqual((os.path.exists(f'{self.d}/good-ran'), os.path.exists(f'{self.d}/bad-ran')), (True, False))

    def test_a_child_that_left_the_group_and_holds_stdout_is_ended(self):
        s = self.script(f'setsid sh -c \'echo $$ > {self.d}/child; exec sleep 30\' &\n'
                        f'echo \'{{"v": 1, "free": true, "at": "{now_text()}"}}\'\n')
        self.assertEqual(fc.ask(s, timeout=1)[0], 'unavailable')
        with open(f'{self.d}/child') as f:
            child = int(f.read())
        for _ in range(50):
            try:
                with open(f'/proc/{child}/stat') as f:
                    if f.read().rsplit(')', 1)[1].split()[0] == 'Z':
                        break
            except (FileNotFoundError, ProcessLookupError):
                break
            time.sleep(0.1)
        else:
            os.kill(child, 9)
            self.fail('the child that left the group with setsid still runs')

    def test_the_command_line(self):
        def cli(*args):
            return subprocess.run([sys.executable, '-I', '-B', READER, *args], capture_output=True, text=True,
                                  timeout=30)
        s = self.script(f'echo \'{{"v": 1, "free": true, "at": "{now_text()}", "memory": {{"freeMb": 500}}}}\'\n')
        got = cli('ask', s)
        self.assertEqual((got.returncode, got.stdout), (0, 'free\n'))
        got = cli('ask', '--need-mb', '501', s)
        self.assertEqual((got.returncode, got.stdout), (75, 'busy: 500 MB free, 501 MB needed\n'))
        got = cli('ask', 'relative')
        self.assertEqual(got.returncode, 1)
        self.assertTrue(got.stdout.startswith('not available: '))
        self.assertEqual(cli('ask').returncode, 2)


class Examples(unittest.TestCase):
    """Each example answers by the contract on this machine (free or busy, never not available). Run from a copy in
    a folder of its own: a checkout's folders may be group-writable, and then the reader rightly refuses them."""

    def copy(self, name, text=None):
        d = os.path.realpath(tempfile.mkdtemp(prefix='free-contract-test-'))
        self.addCleanup(shutil.rmtree, d, True)
        if text is None:
            with open(os.path.join(COMMON, 'free', name)) as f:
                text = f.read()
        path = os.path.join(d, name)
        with open(path, 'w') as f:
            f.write(text)
        os.chmod(path, 0o755)
        return path

    def test_memory(self):
        verdict, reason, facts = fc.ask(self.copy('memory'))
        self.assertIn(verdict, ('free', 'busy'), reason)
        self.assertIsInstance(facts['memory']['freeMb'], int)

    def test_busy_while_running(self):
        with open(os.path.join(COMMON, 'free', 'busy-while-running')) as f:
            text = f.read()
        self.assertEqual(fc.ask(self.copy('busy-while-running'))[0], 'free')
        names = "NAMES='example-game example-editor'"
        self.assertIn(names, text)
        copy = self.copy('busy', text.replace(names, "NAMES='not-running sleep'"))
        p = subprocess.Popen(['sleep', '30'])
        try:
            self.assertEqual(fc.ask(copy)[:2], ('busy', 'sleep is running'))
        finally:
            p.kill()
            p.wait()

    def test_busy_while_running_knows_names_longer_than_15_characters(self):
        with open(os.path.join(COMMON, 'free', 'busy-while-running')) as f:
            text = f.read()
        copy = self.copy('busy', text.replace("NAMES='example-game example-editor'", "NAMES='sleeps-a-long-while'"))
        d = os.path.dirname(copy)
        shutil.copy(shutil.which('sleep'), os.path.join(d, 'sleeps-a-long-while'))
        p = subprocess.Popen([os.path.join(d, 'sleeps-a-long-while'), '30'])
        try:
            self.assertEqual(fc.ask(copy)[:2], ('busy', 'sleeps-a-long-while is running'))
        finally:
            p.kill()
            p.wait()


if __name__ == '__main__':
    unittest.main()
