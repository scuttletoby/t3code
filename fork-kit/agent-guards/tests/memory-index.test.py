"""Tests of fork-kit/agent-guards/bin/memory-index-guard (PreToolUse: refuse a write that puts the auto-memory index
over budget; PostToolUse: block once when the index changed and is over). Each test has its own HOME, project folder
and state folder; the budget comes from a machine file of the test's own or the defaults (6000 / 160).

  python3 -I -B fork-kit/agent-guards/tests/memory-index.test.py
"""
import json, os, shutil, subprocess, sys, tempfile, time, unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guards_fixture as fx  # noqa: E402

HOOK = 'memory-index-guard'
SMALL = '- [A](a.md) - a memory\n'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='memory-index-test-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        self.proj = os.path.join(self.home, '.claude', 'projects', '-srv-work')
        os.makedirs(os.path.join(self.proj, 'memory'))
        self.transcript = os.path.join(self.proj, 'session-1.jsonl')
        self.started(time.time() - 3600)
        self.index = os.path.join(self.proj, 'memory', 'MEMORY.md')
        self.env = dict(os.environ, HOME=self.home)
        self.machine = fx.NO_MACHINE
        self.state = os.path.join(self.home, '.cache', 'memory-index-guard')

    def started(self, when):
        with open(self.transcript, 'w') as f:
            f.write(json.dumps({'type': 'user', 'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S.000Z',
                                                                            time.gmtime(when))}) + '\n')

    def set_index(self, text, age=0):
        with open(self.index, 'w') as f:
            f.write(text)
        if age:
            t = time.time() - age
            os.utime(self.index, (t, t))

    def run_hook(self, event, sid='session-1', **extra):
        ev = dict({'session_id': sid, 'transcript_path': self.transcript}, **event, **extra)
        return fx.run_hook(HOOK, ev, self.machine, env=self.env)

    def pre(self, tool, **tool_input):
        tool_input.setdefault('file_path', self.index)
        return self.run_hook({'hook_event_name': 'PreToolUse', 'tool_name': tool, 'tool_input': tool_input})

    def post(self, tool='Bash', sid='session-1'):
        return self.run_hook({'hook_event_name': 'PostToolUse', 'tool_name': tool, 'tool_input': {'command': 'x'}},
                             sid=sid)

    def assert_refused(self, r, *words):
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(fx.answer(r), 'refused', r.stdout)
        out = json.loads(r.stdout)
        text = out.get('reason') or out['hookSpecificOutput']['permissionDecisionReason']
        self.assertIn(f'Memory index {self.index} is over budget', text)
        for w in words:
            self.assertIn(w, text)

    def assert_passed(self, r):
        self.assertEqual((r.returncode, r.stdout), (0, ''), r.stderr)


class Pre(Base):
    def test_a_write_over_budget_is_refused_one_inside_passes(self):
        self.set_index(SMALL)
        self.assert_refused(self.pre('Write', content='x' * 6001), '6001 chars, budget 6000')
        self.assert_refused(self.pre('Write', content=SMALL + 'y' * 161 + '\n'), 'line(s) 2 longer than 160')
        self.assert_passed(self.pre('Write', content=('x' * 99 + '\n') * 60))     # 6000 chars
        self.assert_passed(self.pre('Write', content=SMALL + 'y' * 160 + '\n'))

    def test_edits_are_worked_out_on_the_file(self):
        self.set_index(SMALL + 'B\n')
        self.assert_refused(self.pre('Edit', old_string='B', new_string='z' * 200))
        self.assert_passed(self.pre('Edit', old_string='B', new_string='z' * 100))
        self.assert_passed(self.pre('Edit', old_string='not there', new_string='z' * 9000))   # the tool refuses it
        self.assert_refused(self.pre('MultiEdit', edits=[{'old_string': 'B', 'new_string': 'C'},
                                                         {'old_string': 'C', 'new_string': 'w' * 7000}]))
        self.set_index('B B B\n')
        self.assert_refused(self.pre('Edit', old_string='B', new_string='q' * 60, replace_all=True))
        self.assert_passed(self.pre('Edit', old_string='B', new_string='q' * 60))

    def test_cutting_down_an_index_already_over_is_allowed(self):
        self.set_index('x' * 9000)
        self.assert_passed(self.pre('Write', content='x' * 8000))
        self.assert_passed(self.pre('Edit', old_string='x' * 100, new_string=''))
        self.assert_refused(self.pre('Write', content='x' * 9001))

    def test_only_a_memory_index_is_read(self):
        other = os.path.join(self.tmp, 'notes', 'MEMORY.md')
        for path in (other, os.path.join(self.proj, 'memory', 'other.md'), ''):
            with self.subTest(path):
                self.assert_passed(self.pre('Write', file_path=path, content='x' * 9000))
        # another project's index (any project's memory/MEMORY.md) counts
        os.makedirs(os.path.join(self.home, '.claude', 'projects', 'p2', 'memory'))
        r = self.pre('Write', file_path=os.path.join(self.home, '.claude', 'projects', 'p2', 'memory', 'MEMORY.md'),
                     content='x' * 9000)
        self.assertEqual(fx.answer(r), 'refused')
        self.assert_passed(self.pre('Read', content='x' * 9000))

    def test_the_budget_comes_from_the_machine_file(self):
        self.machine = fx.machine_file(self.tmp, {'memoryIndex': {'maxChars': 100, 'maxLine': 20}})
        self.set_index('')
        self.assert_refused(self.pre('Write', content='x' * 101), 'budget 100')
        self.assert_refused(self.pre('Write', content='y' * 21), 'longer than 20')
        self.assert_passed(self.pre('Write', content='- a\n' * 20))

    def test_a_very_long_write_still_answers_in_time(self):
        self.set_index(SMALL)
        start = time.monotonic()
        self.assert_refused(self.pre('Write', content=('z' * 200 + '\n') * 30000))
        self.assertLess(time.monotonic() - start, 5)


class Post(Base):
    def test_a_change_over_budget_blocks_once(self):
        self.set_index('x' * 7000)                     # changed after the session started
        self.assert_refused(self.post(), '7000 chars')
        self.assert_passed(self.post())                # seen: not again
        self.set_index('x' * 7001)
        self.assert_refused(self.post(), '7001 chars')
        self.assert_passed(self.post('Write'))
        self.assert_refused(self.post(sid='session-2'))   # another session looks for itself

    def test_an_index_over_before_the_session_started_is_left_alone(self):
        self.set_index('x' * 7000, age=7200)           # older than the session (1 h)
        self.assert_passed(self.post())
        self.assert_passed(self.post())

    def test_inside_budget_or_no_index_passes(self):
        self.assert_passed(self.post())                # no index at all
        self.set_index(SMALL)
        self.assert_passed(self.post())

    def test_a_transcript_missing_or_odd_counts_as_just_started(self):
        for text in (None, '', 'not json\n', '{"timestamp": "yesterday"}\n'):
            with self.subTest(text=text):
                shutil.rmtree(self.state, ignore_errors=True)
                if text is None:
                    if os.path.exists(self.transcript):
                        os.unlink(self.transcript)
                else:
                    with open(self.transcript, 'w') as f:
                        f.write(text)
                self.set_index('x' * 7000, age=60)
                self.assert_passed(self.post())         # first look, older than "now"

    def test_state_it_cannot_write_or_read_never_breaks_a_call(self):
        """A state folder that is a file: every call looks again (and blocks again); a garbled state file: changed."""
        self.machine = fx.machine_file(self.tmp, {'memoryIndex': {'stateDir': os.path.join(self.tmp, 'a-file')}})
        with open(os.path.join(self.tmp, 'a-file'), 'w') as f:
            f.write('x')
        self.set_index('x' * 7000)
        self.assert_refused(self.post())
        self.assert_refused(self.post())
        self.machine = fx.NO_MACHINE
        os.makedirs(self.state)
        with open(os.path.join(self.state, 'session-1'), 'w') as f:
            f.write('\0garbled')
        self.assert_refused(self.post())
        self.assert_passed(self.post())

    def test_an_odd_session_id_stays_in_the_state_folder(self):
        self.set_index('x' * 7000)
        self.assert_refused(self.post(sid='../../escape'))
        self.assertEqual(os.listdir(self.state), ['escape'])
        self.assertFalse(os.path.exists(os.path.join(self.home, 'escape')))

    def test_two_at_once_leave_one_whole_state_file(self):
        self.set_index('x' * 7000)
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: self.post(), range(16)))
        self.assertTrue(all(r.returncode == 0 for r in results))
        self.assertGreaterEqual(sum(fx.answer(r) == 'refused' for r in results), 1)
        self.assertEqual(os.listdir(self.state), ['session-1'])          # no temporary file left
        with open(os.path.join(self.state, 'session-1')) as f:
            mtime, size = f.read().split()
        self.assertEqual(int(size), 7000)
        self.assert_passed(self.post())


class Robust(Base):
    def test_odd_input_never_breaks_a_call(self):
        for text in ('', 'not json', '[]', '{"hook_event_name": "PreToolUse", "tool_input": "x"}',
                     '{"hook_event_name": "PostToolUse", "transcript_path": 5}'):
            with self.subTest(text):
                r = fx.run_hook(HOOK, text, env=self.env)
                self.assert_passed(r)


if __name__ == '__main__':
    unittest.main()
