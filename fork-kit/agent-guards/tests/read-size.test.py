#!/usr/bin/env python3
"""Tests of fork-kit/agent-guards/bin/read-size-guard (the Claude Code PreToolUse hook that refuses a whole-file Read
whose text is over maxChars characters; #1150). Characters, not lines. Its settings entry is tested in
wiring.test.py.

  python3 -I -B fork-kit/agent-guards/tests/read-size.test.py
"""
import io, json, os, shutil, subprocess, sys, tempfile, time, unittest, uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guards_fixture as fx  # noqa: E402

HOOK = os.path.join(fx.BIN, 'read-size-guard')
G = fx.load_hook('read-size-guard')


def read(path, **more):
    return {'hook_event_name': 'PreToolUse', 'tool_name': 'Read', 'tool_input': {'file_path': path, **more},
            'session_id': 's'}


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='read-size-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def file(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as f:
            f.write(data if isinstance(data, bytes) else data.encode())
        return path

    def hook(self, event, raw=None):
        return subprocess.run([sys.executable, '-I', HOOK], input=json.dumps(event) if raw is None else raw,
                              capture_output=True, text=True, timeout=60)

    def test_a_big_whole_file_read_is_refused_with_the_way_out(self):
        path = self.file('big.ts', ('x' * 79 + '\n') * 400)          # 32,000 characters
        r = self.hook(read(path))
        self.assertEqual((r.returncode, r.stdout), (2, ''))
        self.assertTrue(r.stderr.startswith('Refused by read-size-guard: '), r.stderr)
        for said in ('32000 characters', '400 lines', 'grep -n', 'offset and limit', 'allow_large: true'):
            self.assertIn(said, r.stderr)

    def test_characters_count_not_lines(self):
        one_line = self.file('min.js', 'a' * 25000)                  # one minified line
        self.assertIsNotNone(G.measure(one_line, 20000))
        many_short = self.file('short.txt', 'ab\n' * 6000)           # 6000 lines, 18,000 characters
        self.assertIsNone(G.measure(many_short, 20000))
        wide = self.file('wide.txt', 'é' * 15000)                    # 30,000 bytes, 15,000 characters
        self.assertIsNone(G.measure(wide, 20000))

    def test_only_what_read_returns_counts(self):
        """Read returns the first 2000 lines: a long log of short lines is under the limit."""
        path = self.file('log.txt', 'ok\n' * 2000 + 'x' * 50000)
        self.assertIsNone(G.measure(path, 20000))
        path = self.file('huge.txt', 'y' * 200000)
        shown, size = G.measure(path, 20000)
        self.assertTrue(shown.startswith('at least '), shown)

    def test_reads_with_offset_limit_or_allow_large_pass(self):
        path = self.file('big.txt', 'z' * 30000)
        for more in ({'offset': 0}, {'offset': 10}, {'limit': 50}, {'offset': 1, 'limit': 2}, {'allow_large': True},
                     {'pages': '1-2'}):
            with self.subTest(more):
                r = self.hook(read(path, **more))
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))
        self.assertEqual(self.hook(read(path, allow_large=False)).returncode, 2)

    def test_small_missing_odd_and_binary_files_pass(self):
        big = 'q' * 30000
        for path in (self.file('small.txt', 'hello'), os.path.join(self.tmp, 'gone.txt'), self.tmp,
                     self.file('pic.png', big), self.file('doc.pdf', big), self.file('nb.ipynb', big),
                     self.file('blob.bin', b'\0' + big.encode()), 'relative/big.txt', self.file('empty', '')):
            with self.subTest(path):
                r = self.hook(read(path))
                self.assertEqual((r.returncode, r.stderr), (0, ''))
        for event in ({'tool_name': 'Read'}, {'tool_name': 'Read', 'tool_input': None},
                      {'tool_name': 'Read', 'tool_input': {'file_path': 7}}, {'tool_name': 'Read', 'tool_input': 'x'},
                      {'tool_name': 'Bash'}, {}, []):
            with self.subTest(event):
                self.assertEqual(self.hook(event).returncode, 0)

    def test_a_file_it_cannot_read_passes(self):
        path = self.file('locked.txt', 'w' * 30000)
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        if os.geteuid() == 0:
            self.skipTest('root reads every file')
        self.assertEqual(self.hook(read(path)).returncode, 0)

    def test_its_own_failure_lets_the_read_run_and_says_so(self):
        for raw in ('not json', ''):
            with self.subTest(raw):
                r = self.hook(None, raw=raw)
                self.assertEqual((r.returncode, r.stdout), (1, ''))
                self.assertIn('read-size-guard: failed (', r.stderr)
        event = json.dumps(read(self.file('b.txt', 'b' * 30000)))
        err = io.StringIO()
        with mock.patch.object(G, 'measure', side_effect=RuntimeError('bug')), \
                mock.patch.object(sys, 'stdin', io.StringIO(event)), mock.patch.object(sys, 'stderr', err):
            self.assertEqual(G.main([]), 1)

    def test_it_is_fast(self):
        """A file up to maxChars bytes is judged by its size alone; a big one reads at most 4 x maxChars bytes of
        text and, to count its lines, the file once (up to 16 MB; a bigger one only its start). The bytes read are
        checked; the wall time has wide slack, since a busy CI runner made a 50 ms limit fail on main."""
        small, big = self.file('s.txt', 's' * 19000), self.file('b.txt', ('b' * 99 + '\n') * 100000)  # 10 MB
        huge = self.file('h.txt', ('h' * 99 + '\n') * 1000)
        with open(huge, 'r+b') as f:
            f.truncate(G.COUNT_LINES_UP_TO + 1)                 # 16 MB + 1, sparse: too big to count its lines
        for path, most in ((small, 0), (big, 4 * 20000 + 4 + os.path.getsize(big)), (huge, 4 * 20000 + 4)):
            with self.subTest(os.path.basename(path)), fx.bytes_read(G) as reads:
                G.measure(path, 20000)
                self.assertLessEqual(sum(reads), most)
        start = time.perf_counter()
        for _ in range(20):
            G.measure(small, 20000)
            G.measure(big, 20000)
        self.assertLess((time.perf_counter() - start) / 20, 0.5)   # the bytes above are the cause; this is slack


class Configured(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='read-size-config-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.big = os.path.join(self.tmp, 'big.txt')
        with open(self.big, 'w') as f:
            f.write('c' * 30000)

    def run_hook(self, section=None, text=None, path=None, sid=None):
        event = dict(read(path or self.big), session_id=uuid.uuid4().hex if sid is None else sid)
        return fx.run_hook('read-size-guard', event, fx.machine_file(self.tmp, section, text),
                           env=dict(os.environ, HOME=self.tmp))

    def test_a_broken_file_is_said_once_a_session(self):
        """#1151: one notice per hook per session, not one on every call; stderr (not read by the agent) keeps it."""
        broken = {'text': '{"version": 1, "agent-guards": {"readSize": {"ena'}
        first = self.run_hook(sid='one', **broken)
        again = self.run_hook(sid='one', **broken)
        other = self.run_hook(sid='two', **broken)
        self.assertIn('systemMessage', json.loads(first.stdout))
        self.assertEqual((again.returncode, again.stdout), (0, ''))
        self.assertIn('built-in defaults used', again.stderr)
        self.assertIn('systemMessage', json.loads(other.stdout))
        nosid = self.run_hook(sid='', **broken)
        self.assertIn('systemMessage', json.loads(nosid.stdout))

    def test_the_off_switch_and_the_limit(self):
        self.assertEqual(self.run_hook({'readSize': {'enabled': False}}).returncode, 0)
        self.assertEqual(self.run_hook({'readSize': {'maxChars': 40000}}).returncode, 0)
        self.assertEqual(self.run_hook({'readSize': {'maxChars': 1000}}).returncode, 2)

    def test_a_broken_part_lets_every_read_through_and_says_so(self):
        for name, kw in (('wrong type', {'section': {'readSize': {'maxChars': '20000'}}}),
                         ('a bool for a number', {'section': {'readSize': {'maxChars': True}}}),
                         ('unknown key', {'section': {'readSize': {'maxLines': 10}}}),
                         ('half-written file', {'text': '{"version": 1, "agent-guards": {"readSize": '}),
                         ('empty file', {'text': ''})):
            with self.subTest(name):
                r = self.run_hook(**kw)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn('built-in defaults used', r.stderr)
                self.assertIn('lets every Read through', json.loads(r.stdout)['systemMessage'])

    def test_an_old_machine_file_without_the_part_uses_the_defaults_quietly(self):
        for section in (None, {'facts': {'staleDays': 30}}):
            with self.subTest(section):
                r = self.run_hook(section)
                self.assertEqual(r.returncode, 2)
                self.assertNotIn('defaults', r.stderr)


if __name__ == '__main__':
    unittest.main()
