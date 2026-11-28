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
                         sorted([('PreToolUse', 'Write|Edit|MultiEdit', [5]), ('PreToolUse', 'Bash|Monitor', [10]),
                                 ('PreToolUse', 'Bash|Monitor', [10]),
                                 ('PreToolUse', 'Bash', [5]), ('PreToolUse', 'Read', [5]),
                                 ('PostToolUse', 'Write|Edit|MultiEdit|Bash|Monitor', [5]),
                                 ('PostToolUse', 'Read|Grep|Glob|LS|NotebookRead|Bash', [5]),
                                 ('UserPromptSubmit', None, [5]), ('Stop', None, [5, 5]),
                                 ('SessionStart', None, [5]), ('PreCompact', None, [5])]))
        for _e, _m, cmd in wiring.commands_in({'hooks': wiring.hooks()}):
            self.assertIn('/usr/local/bin/', cmd)
            fast = any(f'g=/usr/local/bin/{h};' in cmd for h in wiring.FAST_HOOKS)
            self.assertIn('/usr/bin/python3 -IBS ${f:+"$f"} "$g"' if fast else '/usr/bin/python3 -IB "$g"', cmd)
            self.assertEqual('f=/usr/local/bin/agent-guards-fast; [ -r "$f" ] && [ -s "$f" ] || f=; ' in cmd, fast)
        self.assertTrue(wiring.ours(json.loads(wiring.drop_in_text())))
        self.assertFalse(wiring.ours({'hooks': wiring.hooks(), 'permissions': {}}))
        self.assertFalse(wiring.ours({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': '/x/other'}]}]}}))
        self.assertFalse(wiring.ours({'hooks': {}}))
        env = {'API_TIMEOUT_MS': '180000'}
        self.assertEqual(json.loads(wiring.drop_in_text(env=env)), {'hooks': wiring.hooks(), 'env': env})
        self.assertEqual(json.loads(wiring.drop_in_text(env={})), {'hooks': wiring.hooks()})
        self.assertTrue(wiring.ours(json.loads(wiring.drop_in_text(env=env))))
        self.assertFalse(wiring.ours({'hooks': wiring.hooks(), 'env': {'X': 1}}))       # only text values
        self.assertFalse(wiring.ours({'hooks': wiring.hooks(), 'env': ['X=1']}))
        self.assertFalse(wiring.ours({'env': env}))
        # lens B nit: a command that only names a kit hook somewhere is not the kit's
        other = {'type': 'command', 'command': 'echo /usr/local/bin/bg-guard; g=/usr/local/bin/bg-guard; x'}
        self.assertFalse(wiring.ours({'hooks': {'Stop': [{'hooks': [other]}]}}))
        self.assertFalse(wiring.ours({'hooks': {'Stop': [{'hooks': [
            {'type': 'command', 'command': 'g=/usr/local/bin/bg-guard-old; exec x'}]}]}}))

    def test_every_tool_that_runs_shell_text_is_matched(self):
        """Lens B major: Monitor runs shell text like Bash; a guard wired for Bash only let it through."""
        for event, hook in (('PreToolUse', 'actions-disable-guard'), ('PreToolUse', 'capacity-pipe-guard'),
                            ('PostToolUse', 'memory-index-guard')):
            for tool in ('Bash', 'Monitor'):
                with self.subTest(hook=hook, tool=tool):
                    self.assertIsNotNone(wiring.find({'hooks': wiring.hooks()}, hook, event, tool))
        self.assertIsNone(wiring.find({'hooks': wiring.hooks()}, 'actions-disable-guard', 'PreToolUse', 'Read'))

    def test_matches_reads_the_matcher_as_claude_code_does(self):
        for matcher, tool, want in ((None, 'Monitor', True), ('', 'Monitor', True), ('*', 'Monitor', True),
                                    ('Bash', 'Monitor', False), ('Bash|Monitor', 'Monitor', True),
                                    ('Bash|Monitor', 'Mon', False), ('Mon.*', 'Monitor', True),
                                    ('(', 'Monitor', False), ('Bash', None, True)):
            with self.subTest(matcher=matcher, tool=tool):
                self.assertEqual(wiring.matches(matcher, tool), want)

    def test_the_closed_hook_blocks_on_a_timeout(self):
        """Lens B minor: Claude Code lets the call run when a hook times out, unless the entry says
        "onFailure": "block". The closed hook says it; the open ones do not."""
        for _event, entries in wiring.hooks().items():
            for e in entries:
                for h in e['hooks']:
                    closed = 'actions-disable-guard' in h['command']
                    self.assertEqual(h.get('onFailure'), 'block' if closed else None, h['command'])

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
                gone = 'is missing or empty' if hook in wiring.CLOSED else 'is missing'
                self.assertIn(f'{hook}: {self.bin}/{hook} {gone}: {wiring.WHAT_IS_LOST[hook]}', r.stderr)
                self.stand_in(hook)

    def fast_stand_in(self, text=None):
        """<bin>/agent-guards-fast: the fast path with the stand-ins' config (no machine file), or that text."""
        path = os.path.join(self.bin, wiring.FAST)
        with open(path, 'w') as f:
            f.write(text if text is not None else
                    f'import sys\nsys.argv[1:1] = [{fx.LIB!r}, {fx.NO_MACHINE!r}, "{os.geteuid()}"]\n' + fx.FAST_RUNNER)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

    def test_the_fast_path_answers_as_the_hook_and_is_left_out_when_it_is_not_there(self):
        """#1094: with agent-guards-fast there, without it, and with an empty one (which would answer nothing and
        let every call run): the same answers."""
        cases = [('actions-disable-guard', dict(BASH, tool_input={'command': 'gh workflow disable 1 -R a/b'})),
                 ('actions-disable-guard', BASH), ('actions-disable-guard', dict(BASH, tool_input={'command': 'make'})),
                 ('capacity-pipe-guard', dict(BASH, tool_input={'command': 't3-capacity 1 | tail'})),
                 ('capacity-pipe-guard', BASH), ('memory-index-guard', dict(BASH, hook_event_name='PostToolUse'))]
        before = [(r.returncode, fx.answer(r)) for r in (self.sh(h, e) for h, e in cases)]
        self.assertEqual([a for _c, a in before], ['refused', 'passed', 'passed', 'refused', 'passed', 'passed'])
        for text in (None, ''):
            self.fast_stand_in(text)
            with self.subTest(fast='empty' if text == '' else 'there'):
                self.assertEqual([(r.returncode, fx.answer(r)) for r in (self.sh(h, e) for h, e in cases)], before)
        self.fast_stand_in('import sys\nprint("FAST", sys.argv[1:])\n')   # it is what runs, with the hook file
        r = self.sh('capacity-pipe-guard', BASH)
        self.assertEqual(r.stdout, f"FAST ['{self.bin}/capacity-pipe-guard']\n")
        self.assertNotIn('FAST', self.sh('bg-guard', BASH).stdout)   # the other hooks do not use it

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
                        self.assertEqual(r.returncode, 2, r.stderr)   # lens A nit: empty refuses too
                        self.assertIn('is missing or empty' if name == 'empty'
                                      else 'actions-disable-guard: the guard failed (exit', r.stderr)
                        if name != 'empty':
                            self.assertIn('the command is refused', r.stderr)
                    else:
                        self.assertNotEqual(r.returncode, 2)
                    self.stand_in(hook)

    def test_a_hook_killed_by_a_timeout_refuses_only_the_closed_one(self):
        """Killed by a signal on its own (the machine ran out of memory, say), the closed entry still refuses.
        Claude Code's own timeout is the entry's "onFailure" (test_the_closed_hook_blocks_on_a_timeout)."""
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
        self.assertEqual(sum(ln.startswith('PASS') for ln in lines), 20, '\n'.join(lines))
        self.assertEqual(lines[-1], 'check: test settings: OK')
        # lens A nit: the hooks ran with HOME in check's own temporary folder; nothing (log, cache, state) is
        # written in the caller's HOME
        self.assertEqual(os.listdir(self.home), [])

    def test_facts_hook_with_a_home_folder_table_wants_and_gets_context(self):
        # review round 1: the wanted answer and the hook read "~/" under the same HOME (a false FAIL before)
        os.makedirs(os.path.join(self.home, 'facts'))
        with open(os.path.join(self.home, 'facts', 'STATE.md'), 'w') as f:
            f.write('| Thing | State now | Updated |\n|---|---|---|\n| Example thing | on | 11-23 |\n')
        machine = fx.machine_file(self.tmp, {'facts': {'dirs': ['~/facts']}})
        fx.use_machine(machine)
        with open(os.path.join(self.bin, 'facts-hook'), 'w') as f:
            f.write(STAND_IN.format(lib=fx.LIB, machine=machine, uid=os.geteuid(),
                                    hook=os.path.join(fx.BIN, 'facts-hook')))
        code, lines = self.run_check(self.settings)
        self.assertEqual(code, 0, '\n'.join(lines))
        self.assertEqual([ln.split(':')[0] for ln in lines if 'facts-hook' in ln],
                         ['PASS facts-hook (SessionStart) a new session'])
        self.assertIn('context', [ln for ln in lines if 'facts-hook' in ln][0])

    def test_a_hook_not_wired_is_missing(self):
        s = json.loads(json.dumps(self.settings))
        s['hooks']['Stop'] = [{'hooks': [h for h in s['hooks']['Stop'][0]['hooks'] if 'bg-guard' not in h['command']]}]
        code, lines = self.run_check(s)
        self.assertEqual(code, 1)
        self.assertEqual([ln for ln in lines if ln.startswith('MISSING')],
                         ['MISSING bg-guard (Stop): not wired in test settings'])
        self.assertEqual(self.run_check({})[0], 1)

    def test_a_guard_wired_for_bash_only_is_missing_for_monitor(self):
        s = json.loads(json.dumps(self.settings))
        for e in s['hooks']['PreToolUse']:
            e['matcher'] = e['matcher'].replace('|Monitor', '')
        code, lines = self.run_check(s)
        self.assertEqual(code, 1)
        self.assertIn('MISSING actions-disable-guard (PreToolUse, Monitor): not wired in test settings', lines)

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



def load_cli():
    import importlib.machinery, importlib.util
    loader = importlib.machinery.SourceFileLoader('agent_guards_cli', os.path.join(fx.BIN, 'agent-guards'))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    return mod


class Command(unittest.TestCase):
    """bin/agent-guards as a module: root is this test's user (ROOT_UID), the approval file is in a temporary
    folder (approval.PATH, approval.OWNER_UID)."""

    def setUp(self):
        from agent_guards import approval
        self.cli, self.approval = load_cli(), approval
        self.tmp = tempfile.mkdtemp(prefix='agent-guards-cli-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cli.ROOT_UID = os.geteuid()
        for name, value in (('PATH', os.path.join(self.tmp, 'etc', 'approval.json')), ('OWNER_UID', os.geteuid())):
            self.addCleanup(setattr, approval, name, getattr(approval, name))
            setattr(approval, name, value)
        fx.use_machine(fx.NO_MACHINE)

    def main(self, *argv):
        import contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def settings_file(self, mode=0o644, name='s.json'):
        path = os.path.join(self.tmp, name)
        with open(path, 'w') as f:
            f.write('{"hooks": {}}')
        os.chmod(path, mode)
        return path

    def test_check_as_root_runs_only_a_settings_file_only_root_can_write(self):
        """Lens B minor: as root, `check --settings F` runs F's commands; a file another user can write (or a link
        to one) would run that user's commands as root."""
        good = self.settings_file()
        self.assertEqual(self.cli.read_settings(good), '{"hooks": {}}')
        for path in (self.settings_file(0o666, 'w.json'), self.settings_file(0o620, 'g.json')):
            with self.subTest(path):
                with self.assertRaises(OSError) as e:
                    self.cli.read_settings(path)
                self.assertIn('not safe to run as root', str(e.exception))
                code, _out, err = self.main('check', '--settings', path)
                self.assertEqual(code, 1)
                self.assertIn('not safe to run as root', err)
        link = os.path.join(self.tmp, 'link.json')
        os.symlink(good, link)
        with self.assertRaises(OSError):
            self.cli.read_settings(link)
        self.cli.ROOT_UID = os.geteuid() + 1          # not root: the user's own file, as it is
        self.assertEqual(self.cli.read_settings(os.path.join(self.tmp, 'w.json')), '{"hooks": {}}')

    def test_approve_prints_a_code_that_the_approval_takes(self):
        code, out, err = self.main('approve', '--hours', '2', '--cwd', self.tmp, '--command', 'gh  x  1')
        self.assertEqual(code, 0, err)
        mark = [w for w in out.split() if w.startswith(self.approval.MARK)]
        self.assertEqual(len(mark), 1, out)
        self.assertIn('once', out)
        self.assertIn('gh x 1', out)                                     # the command, as it is compared
        self.assertFalse(self.approval.approved(f'gh y {mark[0]}', self.tmp, used_dir=self.tmp)[0])
        self.assertEqual(oct(os.stat(self.approval.PATH).st_mode & 0o777), '0o644')
        code, out, _ = self.main('approve', '--revoke')
        self.assertEqual((code, os.path.exists(self.approval.PATH)), (0, False))
        self.assertFalse(self.approval.approved(f'gh x 1 {mark[0]}', self.tmp, used_dir=self.tmp)[0])
        self.assertEqual(self.main('approve', '--revoke')[0], 0)          # again: nothing there, still fine
        for hours in ('0', '25', '-1'):
            self.assertEqual(self.main('approve', '--hours', hours, '--command', 'gh x')[0], 1)
        self.assertFalse(os.path.exists(self.approval.PATH))
        self.cli.ROOT_UID = os.geteuid() + 1
        code, _out, err = self.main('approve', '--command', 'gh x')
        self.assertEqual(code, 1)
        self.assertIn('run this as root', err)
        self.assertFalse(os.path.exists(self.approval.PATH))

    # lens B r2 major 2: an approval names the one command it is for
    def test_r2_major2_approve_needs_the_command_and_the_code_passes_it_once(self):
        for args in ((), ('--command', ''), ('--command', '   ')):
            code, _out, err = self.main('approve', *args)
            self.assertEqual(code, 1, args)
            self.assertIn('name the one command you approve', err)
        self.assertFalse(os.path.exists(self.approval.PATH))
        code, out, err = self.main('approve', '--cwd', self.tmp, '--command', 'gh x 1')
        self.assertEqual(code, 0, err)
        mark = [w for w in out.split() if w.startswith(self.approval.MARK)][0]
        used = os.path.join(self.tmp, 'state')
        self.assertEqual(self.approval.approved(f'gh x 1 {mark}', self.tmp, used_dir=used), (True, None))
        self.assertFalse(self.approval.approved(f'gh x 1 {mark}', self.tmp, used_dir=used)[0])   # used up

    def test_self_check_names_links_root_ways_and_the_approval(self):
        self.cli.root_ways = lambda user: [f'{user} is in the docker group (a container can write any root file)']
        links = os.path.join(self.tmp, 'links')
        os.makedirs(links)
        drop_in = os.path.join(self.tmp, 'agent-guards.json')
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.cli.self_check(fx.BIN, drop_in, links)          # not wired: a link elsewhere is a note
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn(f'note: {links}/bg-guard is not this kit\'s bg-guard (missing)', text)
        self.assertIn('WARNING:', text)
        self.assertIn('docker group', text)
        with open(drop_in, 'w') as f:                                    # wired: the same is a problem
            f.write(wiring.drop_in_text())
        self.approval.write(self.approval.new_code(), 1, 'gh x', self.tmp)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.cli.self_check(fx.BIN, drop_in, links)
        text = out.getvalue()
        self.assertEqual(code, 1, text)
        self.assertIn(f'PROBLEM: {links}/bg-guard is not this kit\'s bg-guard', text)
        self.assertIn('an owner approval for actions-disable-guard is open', text)
        for hook in wiring.HOOKS:
            os.symlink(os.path.join(fx.BIN, hook), os.path.join(links, hook))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.cli.self_check(fx.BIN, drop_in, links), 0, out.getvalue())

    def test_self_check_takes_a_drop_in_with_env(self):
        drop_in = os.path.join(self.tmp, 'agent-guards.json')
        for hook in wiring.HOOKS:
            os.symlink(os.path.join(fx.BIN, hook), os.path.join(self.tmp, hook))
        import contextlib
        for env, want in (({'API_TIMEOUT_MS': '180000'}, 0), ({'API_TIMEOUT_MS': 180000}, 1)):
            with open(drop_in, 'w') as f:
                f.write(json.dumps({'hooks': wiring.hooks(), 'env': env}))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(self.cli.self_check(fx.BIN, drop_in, self.tmp), want, out.getvalue())
            if not want:
                self.assertIn('managed settings env: API_TIMEOUT_MS=180000', out.getvalue())

    def test_self_check_names_an_unknown_key(self):
        machine = fx.machine_file(self.tmp, {'gardener': {}})
        fx.use_machine(machine)
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.cli.self_check(fx.BIN, os.path.join(self.tmp, 'none.json'), self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("PROBLEM: machine file: agent-guards: unknown key 'gardener'", out.getvalue())


if __name__ == '__main__':
    unittest.main()
