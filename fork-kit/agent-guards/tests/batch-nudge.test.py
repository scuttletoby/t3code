#!/usr/bin/env python3
"""Tests of fork-kit/agent-guards/bin/batch-nudge (the Claude Code PostToolUse hook that adds a hint to batch reads
after afterTurns turns in a row of one read-only call each; #1150). It never refuses. The transcripts are made up,
in the shape Claude Code writes them (one line per content block; the lines of one turn share message.id).

  python3 -I -B fork-kit/agent-guards/tests/batch-nudge.test.py
"""
import io, json, os, shutil, subprocess, sys, tempfile, time, unittest, uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guards_fixture as fx  # noqa: E402

HOOK = os.path.join(fx.BIN, 'batch-nudge')
G = fx.load_hook('batch-nudge')
S = fx.config.load('batchNudge')[0]          # the built-in defaults (no machine file)


def prompt(text='go on'):
    return {'type': 'user', 'message': {'role': 'user', 'content': text}}


def turn(mid, *calls, text=None):
    """The lines of one assistant turn: an optional text block, then one line per tool call."""
    lines = []
    if text:
        lines.append({'type': 'assistant', 'message': {'id': mid, 'content': [{'type': 'text', 'text': text}]}})
    for k, (name, inp) in enumerate(calls):
        lines.append({'type': 'assistant', 'message': {'id': mid, 'content': [
            {'type': 'tool_use', 'id': f'{mid}-{k}', 'name': name, 'input': inp}]}})
    lines.append({'type': 'user', 'message': {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': f'{mid}-0', 'content': 'x'}]}})
    return lines


READ = ('Read', {'file_path': '/work/a.ts'})
GREP = ('Grep', {'pattern': 'x'})
CAT = ('Bash', {'command': 'sed -n 1,80p a.ts'})
EDIT = ('Edit', {'file_path': '/work/a.ts'})


class Transcripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='batch-nudge-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def transcript(self, *entries):
        path = os.path.join(self.tmp, 'session.jsonl')
        with open(path, 'w') as f:
            for e in entries:
                for line in (e if isinstance(e, list) else [e]):
                    f.write(json.dumps(line) + '\n')
        return path

    def hook(self, path, call=READ, raw=None):
        event = {'hook_event_name': 'PostToolUse', 'tool_name': call[0], 'tool_input': call[1],
                 'transcript_path': path, 'session_id': 's'}
        return subprocess.run([sys.executable, '-I', HOOK], input=json.dumps(event) if raw is None else raw,
                              capture_output=True, text=True, timeout=60)

    def test_three_one_read_turns_get_the_hint(self):
        path = self.transcript(prompt(), turn('m1', READ), turn('m2', GREP), turn('m3', CAT))
        r = self.hook(path, CAT)
        self.assertEqual((r.returncode, r.stderr), (0, ''))
        ctx = json.loads(r.stdout)['hookSpecificOutput']
        self.assertEqual(ctx['hookEventName'], 'PostToolUse')
        self.assertIn('3 turns in a row', ctx['additionalContext'])
        self.assertIn('ONE call', ctx['additionalContext'])
        self.assertNotIn('decision', r.stdout)                  # a hint, never a refusal

    def test_it_comes_again_only_every_after_turns(self):
        turns = [turn(f'm{k}', READ) for k in range(7)]
        for n, want in ((1, False), (2, False), (3, True), (4, False), (5, False), (6, True), (7, False)):
            with self.subTest(n):
                self.assertEqual(G.streak(self.transcript(prompt(), *turns[:n]), S), n)
                self.assertEqual(bool(self.hook(self.transcript(prompt(), *turns[:n])).stdout), want)

    def test_a_batched_turn_a_write_a_prompt_or_text_only_starts_again(self):
        for name, last in (('two calls in one turn', turn('m9', READ, GREP)), ('an edit', turn('m9', EDIT)),
                           ('a prompt', prompt()), ('text only', turn('m9', text='done'))):
            with self.subTest(name):
                path = self.transcript(prompt(), turn('m1', READ), turn('m2', READ), last)
                self.assertEqual(G.streak(path, S), 0)
        path = self.transcript(turn('m1', READ), turn('m2', READ, READ), turn('m3', READ), turn('m4', READ))
        self.assertEqual(G.streak(path, S), 2)
        self.assertEqual(self.hook(path).stdout, '')

    def test_a_turn_with_text_and_one_call_counts(self):
        path = self.transcript(turn('m1', READ, text='a'), turn('m2', READ, text='b'), turn('m3', READ))
        self.assertEqual(G.streak(path, S), 3)

    def test_bash_counts_only_when_it_only_reads(self):
        reads = ('cat a | head -5', 'grep -n x a b 2>/dev/null', 'git log --oneline -5', 'ls; wc -l a',
                 "grep -E 'a|b' x", 'FOO=1 sed -n 1,9p a', 'find . -name "*.ts"', 'git diff 2>&1 | tail',
                 'git -C /work/x log -3', 'git -c core.pager=cat show HEAD', 'git --no-pager status')
        writes = ('rm -rf x', 'sed -i s/a/b/ f', 'cat a > b', 'echo $(rm x)', 'git commit -m x', 'find . -delete',
                  'git -C /work/x push', 'git -C',
                  'npm test', 'ls `rm x`', 'cat <<EOF > f', "echo 'open", '', 'python3 x.py')
        for cmd in reads:
            with self.subTest(cmd):
                self.assertTrue(G.read_only('Bash', {'command': cmd}, S))
        for cmd in writes:
            with self.subTest(cmd):
                self.assertFalse(G.read_only('Bash', {'command': cmd}, S))

    def test_only_a_read_only_call_gets_the_hint(self):
        path = self.transcript(turn('m1', READ), turn('m2', READ), turn('m3', READ))
        self.assertEqual(self.hook(path, EDIT).stdout, '')

    def test_a_missing_broken_or_huge_transcript_adds_nothing(self):
        lines = [turn(f'm{k}', READ) for k in range(3)]
        path = self.transcript(*lines)
        with open(path, 'a') as f:
            f.write('{"type": "assistant", "mess')            # a line still being written
        self.assertEqual(G.streak(path, S), 3)
        for p in (os.path.join(self.tmp, 'gone.jsonl'), self.tmp, ''):
            with self.subTest(p):
                r = self.hook(p)
                self.assertEqual((r.returncode, r.stdout), (0, ''))
        big = self.transcript(*([turn(f'x{k}', READ, READ) for k in range(20000)] + lines))
        self.assertGreater(os.path.getsize(big), S['tailBytes'])
        took = []
        for _ in range(3):                                         # the best of 3: a busy test machine is no bug
            start = time.perf_counter()
            self.assertEqual(G.streak(big, S), 3)
            took.append(time.perf_counter() - start)
        self.assertLess(min(took), 0.05)                           # only the last tailBytes are read

    def test_odd_input_adds_nothing_and_its_own_failure_says_so(self):
        for raw in ('{}', '[]', '{"hook_event_name": "PreToolUse", "tool_name": "Read"}'):
            with self.subTest(raw):
                self.assertEqual((self.hook('', raw=raw).returncode, self.hook('', raw=raw).stdout), (0, ''))
        r = self.hook('', raw='not json')
        self.assertEqual((r.returncode, r.stdout), (1, ''))
        self.assertIn('batch-nudge: failed (', r.stderr)
        event = json.dumps({'hook_event_name': 'PostToolUse', 'tool_name': 'Read', 'transcript_path': '/x'})
        err = io.StringIO()
        with mock.patch.object(G, 'streak', side_effect=RuntimeError('bug')), \
                mock.patch.object(sys, 'stdin', io.StringIO(event)), mock.patch.object(sys, 'stderr', err):
            self.assertEqual(G.main([]), 1)


class Configured(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='batch-nudge-config-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, 's.jsonl')
        with open(self.path, 'w') as f:
            for k in range(3):
                for line in turn(f'm{k}', READ):
                    f.write(json.dumps(line) + '\n')

    def run_hook(self, section=None, text=None, call=READ, sid=None):
        event = {'hook_event_name': 'PostToolUse', 'tool_name': call[0], 'tool_input': call[1],
                 'transcript_path': self.path, 'session_id': uuid.uuid4().hex if sid is None else sid}
        return fx.run_hook('batch-nudge', event, fx.machine_file(self.tmp, section, text),
                           env=dict(os.environ, HOME=self.tmp))

    def test_a_broken_file_is_said_once_a_session(self):
        """#1151: one notice per hook per session, not one on every call; stderr (not read by the agent) keeps it."""
        broken = {'text': '{"version": 1, "agent-guards": {"batchNudge": {"ena'}
        first = self.run_hook(sid='one', **broken)
        again = self.run_hook(sid='one', **broken)
        other = self.run_hook(sid='two', **broken)
        self.assertIn('systemMessage', json.loads(first.stdout))
        self.assertEqual((again.returncode, again.stdout), (0, ''))
        self.assertIn('built-in defaults used', again.stderr)
        self.assertIn('systemMessage', json.loads(other.stdout))
        nosid = self.run_hook(sid='', **broken)
        self.assertIn('systemMessage', json.loads(nosid.stdout))

    def test_the_off_switch_and_the_limits(self):
        self.assertEqual(self.run_hook({'batchNudge': {'enabled': False}}).stdout, '')
        self.assertEqual(self.run_hook({'batchNudge': {'afterTurns': 5}}).stdout, '')
        self.assertIn('3 turns', self.run_hook({'batchNudge': {'afterTurns': 1}}).stdout)
        self.assertEqual(self.run_hook({'batchNudge': {'readOnlyTools': ['Grep']}}).stdout, '')

    def test_a_broken_part_adds_no_hint_and_says_so(self):
        for name, kw in (('wrong type', {'section': {'batchNudge': {'afterTurns': '3'}}}),
                         ('a bool for a number', {'section': {'batchNudge': {'afterTurns': True}}}),
                         ('unknown key', {'section': {'batchNudge': {'after': 3}}}),
                         ('half-written file', {'text': '{"version": 1, "agent-guards": {"batchN'}),
                         ('empty file', {'text': ''})):
            with self.subTest(name):
                r = self.run_hook(**kw)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn('built-in defaults used', r.stderr)
                out = json.loads(r.stdout)
                self.assertIn('adds nothing until it is fixed', out['systemMessage'])
                self.assertNotIn('turns in a row', r.stdout)

    def test_an_old_machine_file_without_the_part_uses_the_defaults_quietly(self):
        for section in (None, {'facts': {'staleDays': 30}}):
            with self.subTest(section):
                r = self.run_hook(section)
                self.assertEqual(r.stderr, '')
                self.assertIn('3 turns in a row', r.stdout)


if __name__ == '__main__':
    unittest.main()
