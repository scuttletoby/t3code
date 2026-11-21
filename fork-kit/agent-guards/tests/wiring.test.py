"""agent_guards.wiring and agent_guards.check: the settings entries of the kit's hooks, run as Claude Code runs them
(`/bin/sh -c COMMAND`, the event on stdin). A missing or failing actions-disable-guard refuses (exit 2) and says so;
the other hooks then let the call run (exit 1) and say so. `agent-guards check` passes on the kit's own entries and
names a hook that is missing, broken or not wired. The hooks run from a folder of stand-ins that read no machine
file (their defaults), with HOME in a temporary folder, so nothing of the machine is read or changed.

  python3 -I -B fork-kit/agent-guards/tests/wiring.test.py
"""
import io, json, os, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guards_fixture as fx  # noqa: E402
from guards_fixture import wiring  # noqa: E402
from agent_guards import check  # noqa: E402

BASH = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {'command': 'ls'}, 'session_id': 's'}
STAND_IN = '''import runpy, sys
sys.path.insert(0, {lib!r})
from agent_guards import config
config.MACHINE_PATH, config.OWNER_UID = {machine!r}, {uid}
sys.argv = [{hook!r}] + sys.argv[1:]
runpy.run_path({hook!r}, run_name='__main__')
'''


class Entries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='agent-guards-wiring-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, 'bin')
        os.makedirs(self.bin)
        for hook in wiring.HOOKS:      # each one: the kit's hook with the built-in defaults
            self.stand_in(hook)
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        self.env = dict(os.environ, HOME=self.home)
        self.settings = {'hooks': wiring.hooks(self.bin, sys.executable)}

    def stand_in(self, hook, text=None):
        with open(os.path.join(self.bin, hook), 'w') as f:
            f.write(text if text is not None else STAND_IN.format(
                lib=fx.LIB, machine=fx.NO_MACHINE, uid=os.geteuid(), hook=os.path.join(fx.BIN, hook)))

    def sh(self, hook, event, command=None, timeout=60):
        cmd = command or wiring.command(hook, self.bin, sys.executable)
        return subprocess.run(['/bin/sh', '-c', cmd], input=json.dumps(event), capture_output=True, text=True,
                              timeout=timeout, env=self.env, cwd=self.tmp)

    def test_the_entries_are_the_ones_settings_had(self):
        self.assertEqual(sorted((e, x.get('matcher'), [h['timeout'] for h in x['hooks']])
                                for e, xs in wiring.hooks().items() for x in xs),
                         sorted([('PreToolUse', 'Write|Edit|MultiEdit', [5]), ('PreToolUse', 'Bash', [10]),
                                 ('PreToolUse', 'Bash|Monitor', [10]), ('PostToolUse', 'Write|Edit|MultiEdit|Bash', [5]),
                                 ('UserPromptSubmit', None, [5]), ('Stop', None, [5, 5])]))
        for _e, _m, cmd in wiring.commands_in({'hooks': wiring.hooks()}):
            self.assertIn('/usr/local/bin/', cmd)
            self.assertIn('/usr/bin/python3 -IB "$g"', cmd)
        self.assertTrue(wiring.ours(json.loads(wiring.drop_in_text())))
        self.assertFalse(wiring.ours({'hooks': wiring.hooks(), 'permissions': {}}))
        self.assertFalse(wiring.ours({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': '/x/other'}]}]}}))
        self.assertFalse(wiring.ours({'hooks': {}}))

    def test_find_matches_whole_names_only(self):
        s = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': '/x/my-bg-guard-2'},
                                           {'type': 'command', 'command': 'bg-guard 2>/dev/null || true'}]}]}}
        self.assertEqual(wiring.find(s, 'bg-guard', 'Stop'), 'bg-guard 2>/dev/null || true')
        self.assertIsNone(wiring.find(s, 'bg-guard', 'PreToolUse'))
        self.assertIsNone(wiring.find({'hooks': {'Stop': 'x'}}, 'bg-guard', 'Stop'))
        self.assertIsNone(wiring.find(None, 'bg-guard', 'Stop'))

    def test_a_present_hook_answers_through_its_entry(self):
        r = self.sh('actions-disable-guard', dict(BASH, tool_input={'command': 'gh workflow disable 1 -R a/b'}))
        self.assertEqual(fx.answer(r), 'refused', r.stderr)
        r = self.sh('actions-disable-guard', BASH)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))
        r = self.sh('capacity-pipe-guard', dict(BASH, tool_input={'command': 't3-capacity 1 | tail'}))
        self.assertEqual(r.returncode, 2)
        self.assertIn('Refused by capacity-pipe-guard', r.stderr)

    def test_a_missing_hook_fails_its_way_and_says_so(self):
        for hook in wiring.HOOKS:
            with self.subTest(hook):
                os.unlink(os.path.join(self.bin, hook))
                r = self.sh(hook, BASH)
                self.assertEqual(r.returncode, 2 if hook in wiring.CLOSED else 1)
                self.assertIn(f'{hook}: {self.bin}/{hook} is missing: {wiring.WHAT_IS_LOST[hook]}', r.stderr)
                self.stand_in(hook)

    def test_a_hook_that_cannot_be_read_counts_as_missing(self):
        if os.geteuid() == 0:
            self.skipTest('root reads every file')
        os.chmod(os.path.join(self.bin, 'actions-disable-guard'), 0)
        r = self.sh('actions-disable-guard', BASH)
        self.assertEqual(r.returncode, 2)
        self.assertIn('is missing', r.stderr)

    def test_a_failing_hook_fails_its_way(self):
        """A crash, a hook cut half-way, an exit of its own: the closed one refuses, the others let it run."""
        broken = {'crash': 'raise SystemExit(3)', 'cut half-way': 'import json\ndef main(:\n', 'empty': ''}
        for name, text in broken.items():
            for hook in wiring.HOOKS:
                with self.subTest(hook=hook, broken=name):
                    self.stand_in(hook, text)
                    r = self.sh(hook, BASH)
                    if hook in wiring.CLOSED:
                        self.assertEqual(r.returncode, 2 if name != 'empty' else 0)   # an empty file says nothing
                        if name != 'empty':
                            self.assertIn('actions-disable-guard: the guard failed (exit', r.stderr)
                            self.assertIn('the command is refused', r.stderr)
                    else:
                        self.assertNotEqual(r.returncode, 2)
                    self.stand_in(hook)

    def test_a_hook_killed_by_a_timeout_refuses_only_the_closed_one(self):
        """Claude Code ends a hook at its timeout and goes on (no onFailure). Killed by a signal on its own (the
        machine ran out of memory, say), the closed entry still refuses."""
        for hook in ('actions-disable-guard', 'capacity-pipe-guard'):
            with self.subTest(hook):
                self.stand_in(hook, 'import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n')
                r = self.sh(hook, BASH)
                self.assertEqual(r.returncode == 2, hook in wiring.CLOSED, r.stderr)
                self.stand_in(hook)


class Check(unittest.TestCase):
    """agent-guards check, on the kit's entries (stand-ins with the defaults) and on broken wiring."""

    def setUp(self):
        Entries.setUp(self)
        self.old_home = os.environ.get('HOME')
        os.environ['HOME'] = self.home     # check.run's clean-up of its made-up session, in this process
        self.addCleanup(self.restore_home)
        fx.use_machine(fx.NO_MACHINE)

    def restore_home(self):
        if self.old_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = self.old_home

    stand_in = Entries.stand_in

    def run_check(self, settings):
        lines = []
        code = check.run(settings, 'test settings', say=lines.append)
        return code, lines

    def test_the_kit_entries_pass(self):
        code, lines = self.run_check(self.settings)
        self.assertEqual(code, 0, '\n'.join(lines))
        self.assertEqual(sum(ln.startswith('PASS') for ln in lines), 11, '\n'.join(lines))
        self.assertEqual(lines[-1], 'check: test settings: OK')
        self.assertEqual(os.listdir(os.path.join(self.home, '.cache', 'memory-index-guard')), [])   # cleaned up
        self.assertEqual(os.listdir(os.path.join(self.home, '.local', 'state', 'bg-guard')), [])

    def test_a_hook_not_wired_is_missing(self):
        s = json.loads(json.dumps(self.settings))
        s['hooks']['Stop'] = [{'hooks': [h for h in s['hooks']['Stop'][0]['hooks'] if 'bg-guard' not in h['command']]}]
        code, lines = self.run_check(s)
        self.assertEqual(code, 1)
        self.assertEqual([ln for ln in lines if ln.startswith('MISSING')],
                         ['MISSING bg-guard (Stop): not wired in test settings'])
        self.assertEqual(self.run_check({})[0], 1)

    def test_a_missing_or_broken_hook_fails_the_check(self):
        os.unlink(os.path.join(self.bin, 'capacity-pipe-guard'))
        code, lines = self.run_check(self.settings)
        self.assertEqual(code, 1)
        self.assertIn('FAIL capacity-pipe-guard (PreToolUse) t3-capacity piped: failed (exit 1)',
                      ' '.join(lines))
        self.stand_in('capacity-pipe-guard')
        self.stand_in('memory-index-guard', 'import sys\nsys.exit(0)\n')     # a hook that lets everything through
        code, lines = self.run_check(self.settings)
        self.assertEqual(code, 1)
        self.assertTrue([ln for ln in lines if ln.startswith('FAIL memory-index-guard')])

    def test_a_hook_that_hangs_is_a_failure_not_a_hang(self):
        self.stand_in('bg-guard', 'import time\ntime.sleep(600)\n')
        real = check._run
        check._run = lambda *a, **k: real(*a, **dict(k, timeout=2))
        self.addCleanup(setattr, check, '_run', real)
        code, lines = self.run_check(self.settings)
        self.assertEqual(code, 1)
        self.assertTrue([ln for ln in lines if ln.startswith('FAIL bg-guard')], '\n'.join(lines))

    def test_the_command(self):
        """agent-guards hooks / check --settings / --help, as a command."""
        cli = os.path.join(fx.BIN, 'agent-guards')
        r = subprocess.run([sys.executable, '-I', '-B', cli, 'hooks', '--bin', '/opt/x'], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {'hooks': wiring.hooks('/opt/x')})
        r = subprocess.run([sys.executable, '-I', '-B', cli, '--help'], capture_output=True, text=True, timeout=60)
        self.assertIn('self-check', r.stdout)
        path = os.path.join(self.tmp, 'settings.json')
        with open(path, 'w') as f:
            f.write('{not json')
        r = subprocess.run([sys.executable, '-I', '-B', cli, 'check', '--settings', path], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn('cannot read', r.stderr)


if __name__ == '__main__':
    unittest.main()
