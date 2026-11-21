"""Tests of fork-kit/agent-guards/bin/agent-slots (UserPromptSubmit: a context line of agent slots and the ready
queue; Stop: block once while slots are free and the ready queue has items), for the sessions listed in sessionsFile
only. The process count comes from a stand-in `pgrep` on PATH. Read only: it writes nothing.

  python3 -I -B fork-kit/agent-guards/tests/agent-slots.test.py
"""
import json, os, shutil, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guards_fixture as fx  # noqa: E402

HOOK = 'agent-slots'
QUEUE = '# Queue\n\n## Ready\n- first item\n- second item\n\n## Later\n- not ready\n'


class Slots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='agent-slots-test-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, 'bin')
        os.makedirs(self.bin)
        self.pgrep(3)
        self.sessions = os.path.join(self.tmp, 'sessions.txt')
        with open(self.sessions, 'w') as f:
            f.write('# orchestrators\norch-1\n')
        self.queue = os.path.join(self.tmp, 'QUEUE.md')
        with open(self.queue, 'w') as f:
            f.write(QUEUE)
        self.configure()
        self.env = dict(os.environ, HOME=self.tmp, PATH=self.bin + os.pathsep + os.environ.get('PATH', ''))

    def pgrep(self, n, args_file=None):
        path = os.path.join(self.bin, 'pgrep')
        with open(path, 'w') as f:
            f.write('#!/bin/sh\n' + (f'echo "$@" > {args_file}\n' if args_file else '') + f'echo {n}\n')
        os.chmod(path, 0o755)

    def configure(self, **keys):
        section = dict({'sessionsFile': self.sessions, 'queue': self.queue, 'note': 'at most 4 build agents'},
                       **keys)
        self.machine = fx.machine_file(self.tmp, {'agentSlots': section})

    def run_hook(self, event, sid='orch-1', **extra):
        return fx.run_hook(HOOK, dict({'hook_event_name': event, 'session_id': sid}, **extra), self.machine,
                           env=self.env)

    def assert_passed(self, r):
        self.assertEqual((r.returncode, r.stdout), (0, ''), r.stderr)

    def test_a_prompt_gets_the_context_line(self):
        args = os.path.join(self.tmp, 'pgrep-args')
        self.pgrep(3, args)
        r = self.run_hook('UserPromptSubmit', prompt='go')
        self.assertEqual((r.returncode, fx.answer(r)), (0, 'context'), r.stderr)
        line = json.loads(r.stdout)['hookSpecificOutput']['additionalContext']
        self.assertEqual(line, f'Agent slots: 3/10 claude processes alive, 7 free (at most 4 build agents). '
                               f'Ready queue ({self.queue}): 2 item(s): first item; second item.')
        with open(args) as f:
            self.assertEqual(f.read().split(), ['-c', '-x', 'claude'])

    def test_a_stop_with_free_slots_and_ready_items_is_blocked_once(self):
        r = self.run_hook('Stop')
        self.assertEqual(fx.answer(r), 'refused', r.stderr)
        self.assertIn('Fill the free slots from the ready queue', json.loads(r.stdout)['reason'])
        self.assert_passed(self.run_hook('Stop', stop_hook_active=True))

    def test_a_stop_passes_when_full_or_nothing_ready(self):
        self.pgrep(10)
        self.assert_passed(self.run_hook('Stop'))
        self.pgrep(12)
        r = self.run_hook('UserPromptSubmit')
        self.assertIn('12/10 claude processes alive, 0 free', r.stdout)
        self.pgrep(0)
        for text in ('# Queue\n## Ready\n\n## Later\n- x\n', ''):
            with self.subTest(text=text):
                with open(self.queue, 'w') as f:
                    f.write(text)
                self.assert_passed(self.run_hook('Stop'))
        os.unlink(self.queue)
        self.assert_passed(self.run_hook('Stop'))                  # a queue that is not there: nothing ready

    def test_only_listed_sessions(self):
        self.assert_passed(self.run_hook('Stop', sid='other'))
        self.assert_passed(self.run_hook('UserPromptSubmit', sid='# orchestrators'))
        os.unlink(self.sessions)
        self.assert_passed(self.run_hook('Stop'))
        self.machine = fx.NO_MACHINE                                  # the default: no sessions file, nothing
        self.assert_passed(self.run_hook('UserPromptSubmit'))

    def test_settings_from_the_machine_file(self):
        with open(self.queue, 'w') as f:
            f.write('## Next up\n- only one\n## Ready\n- not this\n')
        self.configure(readyHeading='## next UP', maxAlive=4, processName='codex', note='')
        r = self.run_hook('UserPromptSubmit')
        line = json.loads(r.stdout)['hookSpecificOutput']['additionalContext']
        self.assertTrue(line.startswith('Agent slots: 3/4 codex processes alive, 1 free. Ready queue'), line)
        self.assertIn('1 item(s): only one.', line)

    def test_a_missing_pgrep_or_odd_input_never_breaks_a_call(self):
        os.unlink(os.path.join(self.bin, 'pgrep'))
        self.env['PATH'] = self.bin
        self.assert_passed(self.run_hook('Stop'))
        self.pgrep('not-a-number')
        self.assert_passed(self.run_hook('Stop'))
        for text in ('', 'not json', '[]'):
            with self.subTest(text):
                self.assert_passed(fx.run_hook(HOOK, text, self.machine, env=self.env))


if __name__ == '__main__':
    unittest.main()
