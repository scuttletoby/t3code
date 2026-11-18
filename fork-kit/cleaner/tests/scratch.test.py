"""The `agent-scratch` rule and its keep-first step (lib/cleaner/rules.py, engine.py, config.py, facts.py): the
scratch folders agent sessions leave behind (`<root>/<project>/<session id>`). Everything lives in the test's own
temporary folder: the scratch root, the archive, the transcripts and a fake /proc. RedCheck breaks each refusal and
each keep-first step in a copy of the code and shows its test then fails.

  python3 -I -B fork-kit/cleaner/tests/scratch.test.py
"""
import hashlib, json, os, sys, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cleaner_fixture as fx  # noqa: E402

import machine_file  # noqa: E402

DAY = fx.DAY
PROJECT = '-work-acme-widget'
SID = '7d2f9c41-58ab-4e0f-9a3c-2b6e1f0d8c57'
OTHER = '11111111-2222-4333-8444-555555555555'
REPORT = '# verdict\nall good\n'


class Case(fx.Case):
    c = fx.load()

    def setUp(self):
        super().setUp()
        self.scratch = self.mkdir('scratch')
        self.archive = self.path('state', 'kept')

    def scfg(self, keep=None, **scratch):
        section = {'enabled': True, 'root': self.scratch, 'keepFirst': {'archive': self.archive, **(keep or {})}, **scratch}
        return self.cfg(agentScratch=section)

    def session(self, sid=SID, days=10, files=None, project=PROJECT):
        """A session folder `days` days old. `files`: {relative path: text}; by default a small report, a result,
        a note one folder down, and things not worth keeping."""
        path = self.mkdir('scratch', project, sid)
        files = files if files is not None else {
            'scratchpad/notes.md': REPORT, 'scratchpad/result.json': '{"ok": true}\n',
            'scratchpad/run-1/summary.md': 'summary\n', 'scratchpad/clone/package.json': '{}\n',
            'scratchpad/clone/src/deep.md': 'deep\n', 'scratchpad/shot.png': 'png', 'tasks/b1.output': 'output\n'}
        for rel, text in files.items():
            self.file(os.path.join(path, *rel.split('/')), text)
        return self.age(path, days)

    def kept_copy(self, sid=SID, project=PROJECT, name=None):
        folder = os.path.join(self.archive, project, name or sid)
        with open(os.path.join(folder, 'manifest.json')) as f:
            return folder, json.load(f)

    def journal(self, cfg):
        return self.c.engine.read_journal(cfg)

    def boom(self, text='/proc: Permission denied'):
        def read():
            raise self.c.facts.Unreadable(text)
        return read


class Rule(Case):
    def test_a_gone_idle_session_is_deleted_after_its_reports_are_kept(self):
        path = self.session()
        cfg = self.scfg()
        plan = self.plan(cfg)
        item = self.assertDeletes(plan, path, 'agent-scratch')
        self.assertIn('session gone, not changed for 10 days; keep first: 4 files', item.why)
        self.assertEqual([name for name, _ in item.look.keep_first],
                         ['scratchpad/notes.md', 'scratchpad/result.json', 'scratchpad/run-1/summary.md', 'tasks/b1.output'])
        code, lines = self.run_cleaner(cfg, really=False)                  # the plan shows both
        self.assertEqual(code, 0, lines)
        self.assertIn(f'  would keep first {path}/scratchpad/notes.md (19 B)', lines)
        self.assertTrue(any(line.startswith(f'would delete {path} (') for line in lines), lines)
        self.assertTrue(any(line.startswith('  agent-scratch: would keep first 4 files') for line in lines), lines)
        self.assertFalse(os.path.exists(self.archive))
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 0, lines)
        self.assertFalse(os.path.lexists(path))
        self.assertTrue(os.path.isdir(os.path.join(self.scratch, PROJECT)), 'a project folder never goes')
        folder, manifest = self.kept_copy()
        self.assertEqual((manifest['source'], manifest['project'], manifest['session'], manifest['rule']),
                         (path, PROJECT, SID, 'agent-scratch'))
        self.assertEqual([f['path'] for f in manifest['files']],
                         ['scratchpad/notes.md', 'scratchpad/result.json', 'scratchpad/run-1/summary.md', 'tasks/b1.output'])
        self.assertEqual((manifest['notCopied'], manifest['linksOut']), ([], []))
        for f in manifest['files']:
            with open(os.path.join(folder, *f['path'].split('/')), 'rb') as copy:
                body = copy.read()
            self.assertEqual((hashlib.sha256(body).hexdigest(), len(body)), (f['sha256'], f['bytes']))
        with open(os.path.join(folder, 'scratchpad', 'notes.md')) as f:
            self.assertEqual(f.read(), REPORT)
        with open(cfg.log_file) as f:
            log = f.read().splitlines()
        first = [line for line in log if 'kept first ' in line]
        self.assertEqual(len(first), 1, log)
        self.assertLess(log.index(first[0]), next(i for i, line in enumerate(log) if ' deleting ' in line))
        freed = self.c.report.freed(cfg)
        self.assertEqual((freed.deletes, freed.skipped), (1, 0), 'the report reads the keep-first line as the cleaner\'s own')
        self.assertEqual(self.journal(cfg), [])
        code, lines = self.run_cleaner(cfg)                                # runs again: nothing left to do
        self.assertEqual((code, [line for line in lines if line.startswith(('deleted ', 'kept first '))]), (0, []))

    def test_a_session_with_nothing_worth_keeping_is_deleted_without_a_copy(self):
        path = self.session(files={'scratchpad/clone/package.json': '{}', 'scratchpad/a.png': 'png'})
        cfg = self.scfg()
        self.assertIn('nothing to keep first', self.assertDeletes(self.plan(cfg), path).why)
        self.assertEqual(self.run_cleaner(cfg)[0], 0)
        self.assertFalse(os.path.lexists(path))
        self.assertFalse(os.path.exists(os.path.join(self.archive, PROJECT, SID)))

    def test_patterns_from_the_machine_file_pick_what_is_kept_first(self):
        path = self.session()
        plan = self.plan(self.scfg({'patterns': ['scratchpad/*.txt', 'scratchpad/run-*/*.md']}))
        self.assertEqual([n for n, _ in self.assertDeletes(plan, path).look.keep_first],
                         ['scratchpad/run-1/summary.md', 'tasks/b1.output'])
        plan = self.plan(self.scfg({'patterns': ['scratchpad/*.txt'], 'outputs': ['tasks/none-*']}))
        self.assertIn('nothing to keep first', self.assertDeletes(plan, path).why)
        for empty in ({'patterns': []}, {'outputs': []}):             # keep first cannot be turned off
            with self.subTest(empty=empty), self.assertRaises(machine_file.MachineFileError):
                self.scfg(empty)

    def test_a_report_too_large_to_keep_keeps_the_session(self):
        path = self.session(files={'scratchpad/notes.md': REPORT, 'scratchpad/dump.json': 'x' * (300 * 1024)})
        cfg = self.scfg()
        self.assertKept(self.plan(cfg), path, 'report too large to keep first', 'scratchpad/dump.json')
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 0, lines)
        self.assertTrue(os.path.isfile(os.path.join(path, 'scratchpad', 'dump.json')))
        self.assertFalse(os.path.exists(self.archive), 'nothing is copied for a session that stays')
        item = self.c.rules.Item('session', path, 'agent-scratch', '', 1, self.c.guard.examine(cfg, self.facts(cfg), path, 'path'))
        with self.assertRaises(self.c.rules.KeepFirstError):        # nor by keep first itself
            self.c.rules.keep_first(cfg, item)

    def test_task_outputs_are_kept_first_and_a_larger_one_is_named(self):
        transcript = self.file(self.path('home', '.claude', 'projects', PROJECT, SID, 'subagents', 'a.jsonl'), 'agent')
        path = self.session(files={'scratchpad/notes.md': REPORT, 'tasks/b1.output': 'output\n',
                                   'tasks/big.output': 'y' * (300 * 1024), 'scratchpad/run.log': 'linked\n'})
        os.symlink('../scratchpad/run.log', os.path.join(path, 'tasks', 'in.output'))
        os.symlink(transcript, os.path.join(path, 'tasks', 'out.output'))
        os.symlink('/etc/hostname', os.path.join(path, 'tasks', 'etc.output'))
        self.age(path, 10)
        cfg = self.scfg()
        item = self.assertDeletes(self.plan(cfg), path)
        self.assertEqual([n for n, _ in item.look.keep_first], ['scratchpad/notes.md', 'tasks/b1.output', 'tasks/in.output'])
        self.assertEqual(item.look.keep_named, [('tasks/big.output', 300 * 1024)])
        self.assertIn('1 larger output named only', item.why)
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 0, lines)
        self.assertFalse(os.path.lexists(path))
        folder, manifest = self.kept_copy()
        self.assertEqual([f['path'] for f in manifest['files']], ['scratchpad/notes.md', 'tasks/b1.output', 'tasks/in.output'])
        with open(os.path.join(folder, 'tasks', 'in.output')) as f:
            self.assertEqual(f.read(), 'linked\n')
        self.assertFalse(os.path.islink(os.path.join(folder, 'tasks', 'in.output')))
        self.assertEqual([(f['path'], f['bytes'], f['sha256']) for f in manifest['notCopied']],
                         [('tasks/big.output', 300 * 1024, hashlib.sha256(b'y' * (300 * 1024)).hexdigest())])
        self.assertFalse(os.path.exists(os.path.join(folder, 'tasks', 'big.output')))
        self.assertEqual(sorted((f['path'], f['to']) for f in manifest['linksOut']),
                         [('tasks/etc.output', '/etc/hostname'), ('tasks/out.output', transcript)])
        with open(transcript) as f:
            self.assertEqual(f.read(), 'agent', 'a link out is never followed')

    def test_too_much_to_keep_first_keeps_the_session_whole(self):
        path = self.session()
        self.assertKept(self.plan(self.scfg({'maxSessionBytes': 10})), path, 'too much to keep first', '4 files')
        self.assertEqual(self.run_cleaner(self.scfg({'maxSessionBytes': 10}))[0], 0)
        self.assertTrue(os.path.isdir(path))

    def test_a_link_is_never_followed_into_or_kept_from(self):
        path = self.session(files={'scratchpad/notes.md': REPORT})
        secret = self.file(self.path('home', 'private.md'), 'private')
        os.symlink(secret, os.path.join(path, 'scratchpad', 'linked.md'))
        self.age(path, 10)
        item = self.assertDeletes(self.plan(self.scfg()), path)
        self.assertEqual([n for n, _ in item.look.keep_first], ['scratchpad/notes.md'])

    def test_only_session_folders_are_candidates(self):
        self.session()
        other = [self.age(self.mkdir('scratch', PROJECT, 'not-a-session'), 30),
                 self.file(self.path('scratch', PROJECT, OTHER + '.txt'), days=30),
                 self.age(self.mkdir('scratch', OTHER), 30)]           # directly in the root: no project folder
        plan = self.plan(self.scfg())
        for path in other:
            self.assertNotCandidate(plan, path)
        self.assertNotCandidate(plan, os.path.join(self.scratch, PROJECT))
        os.symlink(self.path('home'), self.path('scratch', 'linked-project'))
        self.assertKept(self.plan(self.scfg()), self.path('scratch', 'linked-project'), 'symbolic link')


class Refusals(Case):
    def test_a_recent_change_keeps_it(self):
        path = self.session(days=10)
        self.file(os.path.join(path, 'scratchpad', 'new.md'), days=1)
        self.assertNotCandidate(self.plan(self.scfg()), path)
        self.age(path, 4)
        self.assertNotCandidate(self.plan(self.scfg(days=5)), path)
        self.assertDeletes(self.plan(self.scfg()), path)
        self.assertDeletes(self.plan(self.scfg(days=3)), path)

    def test_a_live_session_is_kept_by_its_command_line(self):
        path = self.session()
        for text in (f'claude --session-id={SID} --model x', f'claude --resume {SID}', f'node cli.js -r "{SID}"'):
            with self.subTest(text=text):
                self.texts = [text]
                self.assertKept(self.plan(self.scfg()), path, 'session alive')
        self.texts = [f'claude --session-id={SID}0', f'claude --session-id={OTHER}']    # no whole word: not this one
        self.assertDeletes(self.plan(self.scfg()), path)

    def test_a_live_session_is_kept_by_a_child_process_environment(self):
        path = self.session()
        root = self.fake_proc({100: {'cwd': '/'}, 200: {'cmdline': ['bash'], 'environ': {'CLAUDE_CODE_SESSION_ID': SID}}})
        live = {'processes': lambda: self.c.facts.read_processes(root, 100)}
        self.assertKept(self.plan(self.scfg(), **live), path, 'session alive')
        root = self.fake_proc({100: {'cwd': '/'}, 200: {'cmdline': ['bash'], 'environ': {'CLAUDE_CODE_SESSION_ID': OTHER}}})
        self.assertDeletes(self.plan(self.scfg(), **live), path)

    def test_a_process_list_that_cannot_be_read_keeps_it(self):
        path = self.session()
        self.assertKept(self.plan(self.scfg(), processes=self.boom()), path, 'could not read', 'process list')
        code, lines = self.run_cleaner(self.scfg(), processes=self.boom())
        self.assertEqual(code, 1, lines)
        self.assertTrue(os.path.isdir(path))

    def test_a_recent_transcript_keeps_it(self):
        path = self.session()
        self.transcript_times = {SID: self.now - 1 * DAY}
        self.assertKept(self.plan(self.scfg()), path, 'transcript changed under 3 days ago')
        self.transcript_times = {SID: self.now - 5 * DAY, OTHER: self.now}
        self.assertDeletes(self.plan(self.scfg()), path)
        self.assertKept(self.plan(self.scfg(), transcripts=self.boom('~/.claude/projects: denied')), path,
                        'could not read', 'transcripts')

    def test_transcripts_are_read_by_their_times_only(self):
        home = self.path('home')
        globs = [os.path.join(home, '.claude*', 'projects', '*')]
        a = self.file(os.path.join(home, '.claude', 'projects', PROJECT, SID + '.jsonl'), '{}', days=4)
        self.mkdir('home', '.claude-second', 'projects', PROJECT, OTHER, 'subagents')
        self.file(os.path.join(home, '.claude', 'projects', PROJECT, 'notes.txt'), days=1)
        third = '33333333-2222-4333-8444-555555555555'                     # a config folder that is a link
        self.file(self.path('elsewhere', 'cfg', 'projects', PROJECT, third + '.jsonl'), '{}')
        os.symlink(self.path('elsewhere', 'cfg'), os.path.join(home, '.claude-linked'))
        found = self.c.facts.read_transcripts(globs)
        self.assertAlmostEqual(found[SID], max(os.stat(a).st_mtime, os.stat(a).st_ctime), places=3)
        self.assertIn(OTHER, found)
        self.assertIn(third, found)
        for missing in ([self.path('nobody', 'projects', '*')], [os.path.join(home, '.config', 'claude', 'projects', '*')]):
            with self.subTest(missing=missing), self.assertRaises(self.c.facts.Unreadable):   # moved: no answer
                self.c.facts.read_transcripts(missing)
        if os.geteuid() != 0:
            fx.read_only(os.path.join(home, '.claude', 'projects', PROJECT))
            os.chmod(os.path.join(home, '.claude', 'projects', PROJECT), 0)
            with self.assertRaises(self.c.facts.Unreadable):
                self.c.facts.read_transcripts(globs)

    def test_the_transcript_folders_come_from_the_machine_file(self):
        path = self.session()
        self.assertEqual(self.scfg().transcript_globs, (os.path.join(self.home, '.claude*', 'projects', '*'),))
        real = {'transcripts': None}                                       # the kit's own reader
        self.file(os.path.join(self.home, '.claude', 'projects', PROJECT, SID + '.jsonl'), '{}', days=1)
        self.assertKept(self.plan(self.scfg(), **real), path, 'transcript changed under 3 days ago')
        moved = self.mkdir('moved', 'claude', 'projects', PROJECT)
        self.file(os.path.join(moved, OTHER + '.jsonl'), '{}', days=1)
        cfg = self.scfg(transcripts=[self.path('moved', '*', 'projects', '*')])
        self.assertDeletes(self.plan(cfg, **real), path)
        cfg = self.scfg(transcripts=[self.path('moved', '*', 'projects', '*'), self.path('gone', 'projects', '*')])
        self.assertKept(self.plan(cfg, **real), path, 'could not read', 'no folder matches')
        code, lines = self.run_cleaner(cfg, **real)
        self.assertEqual(code, 1, lines)
        self.assertTrue(os.path.isdir(path))

    def test_a_lock_file_keeps_it(self):
        for name in ('session.lock', 'lock'):
            with self.subTest(name=name):
                path = self.session(files={'scratchpad/notes.md': REPORT, name: ''})
                self.assertKept(self.plan(self.scfg()), path, 'lock file', name)
                os.unlink(os.path.join(path, name))
        path = self.session(files={'scratchpad/notes.md': REPORT, 'scratchpad/clone/yarn.lock': ''})
        self.assertDeletes(self.plan(self.scfg()), path)                   # deeper: a clone's own, not the session's

    def live(self, processes):
        root = self.fake_proc({100: {'cwd': '/'}, **processes})
        return {'processes': lambda: self.c.facts.read_processes(root, 100)}

    def test_a_process_with_a_handle_or_its_folder_in_it_keeps_it(self):
        path = self.session()
        cfg = self.scfg()
        for p in ({'fd': [os.path.join(path, 'tasks')]}, {'cwd': os.path.join(path, 'scratchpad', 'clone')},
                  {'fd': [os.path.join(path, 'tasks', 'b1.output') + ' (deleted)']}):
            with self.subTest(p=p):
                # An absolute command line, as real agents have: a relative one is read under the working folder.
                live = self.live({200: {'cmdline': ['/opt/agent/bin/claude'], 'start': self.now, **p}})
                self.assertKept(self.plan(cfg, **live), path, 'in use')                      # the plan: the guard
                refusal = self.c.rules.session_alive(cfg, self.facts(cfg, **live), path, None)   # the last word
                self.assertEqual((refusal.code, refusal.detail),
                                 ('session alive', 'a process works in it, has a file or folder in it open, or names its path'))
        live = self.live({200: {'cmdline': ['/opt/agent/bin/claude'], 'start': self.now, 'fd': [os.path.join(self.scratch, PROJECT, SID + '0')]}})
        self.assertDeletes(self.plan(cfg, **live), path)

    def test_a_session_cleared_into_a_new_id_is_kept_by_its_process(self):
        path = self.session()
        cfg = self.scfg()
        on = os.path.join(self.scratch, PROJECT, OTHER, 'tasks')        # /clear: the same process, a new session id
        for start in (None, self.now - 20 * DAY):
            with self.subTest(start=start):
                p = {'cmdline': ['claude'], 'fd': [on], 'comm': 'claude'}
                live = self.live({200: p if start is None else {**p, 'start': start}})
                self.assertKept(self.plan(cfg, **live), path, 'session alive', f'works in {OTHER} of the same project')
        for p in ({'start': self.now - 1 * DAY},                                    # began after this one was left
                  {'start': self.now - 20 * DAY, 'environ': {'CLAUDE_CODE_SESSION_ID': OTHER}},   # says it is that one
                  {'start': self.now - 20 * DAY, 'fd': [os.path.join(self.scratch, '-work-acme-other', OTHER, 'tasks')]}):
            with self.subTest(p=p):
                live = self.live({200: {'cmdline': ['claude'], 'fd': [on], **p}})
                self.assertDeletes(self.plan(cfg, **live), path)

    @unittest.skipIf(os.geteuid() == 0, 'root reads every process')
    def test_a_process_of_the_user_that_cannot_be_read_keeps_it(self):
        path = self.session()
        cfg = self.scfg()
        live = self.live({200: {'comm': 'node', 'hidden': True}})
        self.assertKept(self.plan(cfg, **live), path, 'could not read', 'process 200 (node)')
        for name in ('gpg-agent', 'systemd'):                                      # helpers that hide on purpose
            with self.subTest(name=name):
                self.assertDeletes(self.plan(cfg, **self.live({200: {'comm': name, 'hidden': True}})), path)

    @unittest.skipIf(os.geteuid() == 0, 'root reads every process')
    def test_a_hidden_process_is_judged_by_its_nearest_readable_ancestor(self):
        path = self.session()
        cfg = self.scfg()
        codex = {'comm': 'codex', 'hidden': True, 'cmdline': ['codex', 'exec']}
        shell = {'comm': 'bash', 'cmdline': ['/bin/bash'], 'start': self.now - 20 * DAY}
        # The user's service manager, even one that can be read: what it starts is no agent's.
        manager = {'comm': 'systemd', 'cmdline': ['/lib/systemd/systemd', '--user'], 'parent': 1}
        cases = (
            # a Codex started by another session's tool: that session's, not this one's
            ('other session', {300: {**shell, 'environ': {'CLAUDE_CODE_SESSION_ID': OTHER}}, 200: {**codex, 'parent': 300}}, None),
            ('a server above it', {300: {**shell, 'cmdline': ['/usr/bin/node', 'server.js']}, 200: {**codex, 'parent': 300}}, None),
            ('through a hidden parent', {300: shell, 301: {**codex, 'parent': 300}, 200: {**codex, 'parent': 301}}, None),
            # the ancestor decides for it: its environment, its working folder
            ('this session', {300: {**shell, 'environ': {'CLAUDE_CODE_SESSION_ID': SID}}, 200: {**codex, 'parent': 300}},
             'session alive'),
            ('works in it', {300: {**shell, 'cwd': os.path.join(path, 'scratchpad')}, 200: {**codex, 'parent': 300}},
             'in use'),
            # no readable ancestor short of pid 1 or the user's service manager: nothing can be said
            ('under pid 1', {200: {**codex, 'parent': 1}}, 'could not read'),
            ('under the service manager', {250: manager, 200: {**codex, 'parent': 250}}, 'could not read'),
            ('under a hidden service manager', {250: {**manager, 'hidden': True}, 200: {**codex, 'parent': 250}}, 'could not read'),
            ('its parent ended', {200: {**codex, 'parent': 4321}}, 'could not read'),
            ('a parent that is its own child', {200: {**codex, 'parent': 201}, 201: {**codex, 'parent': 200}}, 'could not read'),
        )
        for name, processes, code in cases:
            with self.subTest(case=name):
                plan = self.plan(cfg, **self.live(processes))
                if code is None:
                    self.assertDeletes(plan, path)
                else:
                    self.assertKept(plan, path, code)

    @unittest.skipIf(os.geteuid() == 0, 'root reads every process')
    def test_a_helper_name_excuses_only_what_cannot_be_read(self):
        path = self.session()
        cfg = self.scfg()
        for p in ({'cwd': os.path.join(path, 'scratchpad'), 'cmdline': ['/usr/bin/gpg-agent', '--daemon']},
                  {'cmdline': ['gpg-agent', '--homedir', os.path.join(path, 'scratchpad', 'gnupg'), '--daemon']}):
            with self.subTest(p=p):
                live = self.live({200: {'comm': 'gpg-agent', 'hidden': True, **p}})
                refusal = self.c.rules.session_alive(cfg, self.facts(cfg, **live), path, None)
                self.assertEqual(refusal.code if refusal is not None else None, 'session alive')

    def test_a_credential_name_keeps_it(self):
        path = self.session(files={'scratchpad/notes.md': REPORT, 'scratchpad/app/.env': 'A=1'})
        self.assertKept(self.plan(self.scfg()), path, 'credential name')

    def test_git_work_that_exists_only_here_is_kept_and_listed(self):
        path = self.session(files={'scratchpad/notes.md': REPORT})
        clone = self.clone('w1', into=os.path.join(path, 'scratchpad'))
        self.age(path, 10)
        self.assertDeletes(self.plan(self.scfg()), path)                   # clean and pushed: may go
        self.file(os.path.join(clone, 'draft.txt'), days=10)
        self.age(path, 10)
        self.assertKept(self.plan(self.scfg()), path, 'git: uncommitted or untracked', 'scratchpad/w1')
        self.commit(clone, 'draft.txt')
        self.age(path, 10)
        self.assertKept(self.plan(self.scfg()), path, 'git: unpushed commits', 'scratchpad/w1')
        code, lines = self.run_cleaner(self.scfg())
        self.assertEqual(code, 0, lines)
        self.assertIn(f'kept {path} (agent-scratch: git: unpushed commits: scratchpad/w1', '\n'.join(lines))
        self.assertTrue(os.path.isdir(clone))

    def test_the_session_comes_back_while_keep_first_copies(self):
        path = self.session()
        cfg = self.scfg()
        copy = self.c.rules.keep_first

        def slow(cfg, item):
            told = copy(cfg, item)
            self.texts = [f'claude --resume {SID}']                       # it comes back during the copy
            return told
        with mock.patch.object(self.c.rules, 'keep_first', slow):
            code, lines = self.run_cleaner(cfg)
        self.assertEqual([line for line in lines if line.startswith('REFUSED')],
                         [f'REFUSED {path} (agent-scratch: session alive: a process names its id (command line or environment))'], lines)
        self.assertTrue(os.path.isfile(os.path.join(path, 'scratchpad', 'notes.md')))
        self.assertEqual(self.kept_copy()[1]['session'], SID, 'the copy stays; the next run uses it again')
        self.assertEqual(self.journal(cfg), [])

    def test_the_session_comes_back_between_plan_and_delete(self):
        path = self.session()
        cfg = self.scfg()
        facts = self.facts(cfg)
        plan = self.c.rules.plan(cfg, facts)
        self.assertDeletes(plan, path)
        self.texts = [f'claude --resume {SID}']                           # it comes back
        failed, lines = self.delete_all(cfg, facts, plan.items)
        self.assertEqual(failed, 0)
        self.assertEqual([line for line in lines if line.startswith('REFUSED')],
                         [f'REFUSED {path} (agent-scratch: session alive: a process names its id (command line or environment))'])
        self.assertTrue(os.path.isfile(os.path.join(path, 'scratchpad', 'notes.md')))
        self.assertFalse(os.path.exists(self.archive), 'nothing is copied for a session that stays')
        self.texts = []
        self.file(os.path.join(path, 'scratchpad', 'later.md'))           # or writes again
        failed, lines = self.delete_all(cfg, facts, plan.items)
        self.assertTrue(any(line.startswith(f'REFUSED {path} (agent-scratch: not a candidate') for line in lines), lines)
        self.assertTrue(os.path.isdir(path))


class KeepFirst(Case):
    def test_the_copy_is_verified_before_anything_is_deleted(self):
        path = self.session()
        cfg = self.scfg()
        with mock.patch.object(self.c.rules, '_verified', return_value=False):
            code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {path} (agent-scratch: keep first: ') and 'nothing deleted' in line
                            for line in lines), lines)
        self.assertTrue(os.path.isfile(os.path.join(path, 'scratchpad', 'notes.md')))
        self.assertFalse(os.path.exists(os.path.join(self.archive, PROJECT, SID)), 'an unverified copy is never the kept one')
        self.assertEqual(self.journal(cfg), [])
        code, lines = self.run_cleaner(cfg)                                # the next run makes it again
        self.assertEqual(code, 0, lines)
        self.assertFalse(os.path.lexists(path))
        self.assertEqual(self.kept_copy()[1]['session'], SID)

    def test_a_copy_that_cannot_be_written_keeps_the_folder(self):
        path = self.session()
        self.file(self.archive, 'not a folder')
        cfg = self.scfg()
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {path} (agent-scratch: keep first: ') for line in lines), lines)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(self.journal(cfg), [])

    def test_a_copy_made_by_a_run_that_stopped_before_the_delete_is_used_again(self):
        path = self.session()
        cfg = self.scfg()
        item = self.assertDeletes(self.plan(cfg), path)
        self.assertIn('(verified)', self.c.rules.keep_first(cfg, item))   # the run stops here
        folder, manifest = self.kept_copy()
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 0, lines)
        self.assertTrue(any(line.startswith(f'kept first {path}: 4 files') and f'already in {folder} (verified)' in line
                            for line in lines), lines)
        self.assertEqual(self.kept_copy()[1], manifest)
        self.assertEqual(sorted(os.listdir(os.path.join(self.archive, PROJECT))), [SID])
        self.assertFalse(os.path.lexists(path))

    def test_a_half_written_copy_is_thrown_away_and_made_again(self):
        path = self.session()
        partial = os.path.join(self.archive, PROJECT, f'.{SID}.partial')
        self.file(os.path.join(partial, 'scratchpad', 'notes.md'), 'half')       # the copy stopped half-way
        cfg = self.scfg()
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 0, lines)
        self.assertFalse(os.path.lexists(partial))
        folder, manifest = self.kept_copy()
        with open(os.path.join(folder, 'scratchpad', 'notes.md')) as f:
            self.assertEqual(f.read(), REPORT)
        self.assertFalse(os.path.lexists(path))

    def test_a_link_at_the_half_written_copy_is_removed_as_a_link(self):
        for kind in ('folder link', 'file link', 'file', 'folder with a link in it'):
            with self.subTest(kind=kind):
                path = self.session()
                target = self.mkdir('elsewhere', kind)
                precious = self.file(os.path.join(target, 'precious.md'), 'mine')
                project = self.mkdir('state', 'kept', PROJECT)
                partial = os.path.join(project, f'.{SID}.partial')
                if kind == 'folder link':
                    os.symlink(target, partial)
                elif kind == 'file link':
                    os.symlink(precious, partial)
                elif kind == 'file':
                    self.file(partial, 'half')
                else:
                    os.symlink(target, self.file(os.path.join(partial, 'scratchpad', 'notes.md'), 'half') + '.link')
                code, lines = self.run_cleaner(self.scfg())
                self.assertEqual(code, 0, lines)
                self.assertFalse(os.path.lexists(partial))
                self.assertEqual(sorted(os.listdir(target)), ['precious.md'], 'what a link leads to stays')
                with open(precious) as f:
                    self.assertEqual(f.read(), 'mine')
                self.assertFalse(os.path.lexists(path))
                self.assertEqual(self.kept_copy()[1]['session'], SID)
                self.c.rules._remove_own(os.path.join(project, SID))           # the next kind starts afresh

    def test_the_archive_folders_are_this_users_real_folders_synced_before_the_delete(self):
        path = self.session()
        synced = []
        real = self.c.rules._sync_folder
        with mock.patch.object(self.c.rules, '_sync_folder', lambda folder: (synced.append(folder), real(folder))):
            code, lines = self.run_cleaner(self.scfg())
        self.assertEqual(code, 0, lines)
        project = os.path.join(self.archive, PROJECT)
        for folder in (os.path.dirname(self.archive), self.archive, project, os.path.join(project, f'.{SID}.partial')):
            self.assertIn(folder, synced)                               # each new folder is in its parent for good
        self.assertEqual(os.stat(self.archive).st_mode & 0o777, 0o700)
        os.rename(project, self.path('state', 'moved'))
        elsewhere = self.mkdir('elsewhere')
        os.symlink(elsewhere, project)                                  # a link in the archive: no copy through it
        path = self.session()
        code, lines = self.run_cleaner(self.scfg())
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {path} (agent-scratch: keep first: ') and 'a link' in line
                            for line in lines), lines)
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertTrue(os.path.isdir(path))

    def test_a_folder_swapped_for_a_link_after_the_check_is_not_read_through(self):
        path = self.session()
        cfg = self.scfg()
        item = self.assertDeletes(self.plan(cfg), path)
        secret = self.mkdir('elsewhere', 'secret')
        self.file(os.path.join(secret, 'notes.md'), 'not this session\'s')
        os.rename(os.path.join(path, 'scratchpad'), os.path.join(path, 'was'))
        os.symlink(secret, os.path.join(path, 'scratchpad'))                   # between the check and the copy
        with self.assertRaises(self.c.rules.KeepFirstError):
            self.c.rules.keep_first(cfg, item)
        self.assertFalse(os.path.exists(os.path.join(self.archive, PROJECT, SID)))

    def swap_after_the_list(self, change):
        """keep_first, with `change(path)` run after it listed what to copy and before it reads a file: the names
        and sizes it compares are still what the last check saw, so only the reads themselves can refuse."""
        listed = self.c.rules.keep_first_files

        def then_change(cfg, path, look):
            found = listed(cfg, path, look)
            change(path)
            return found
        return mock.patch.object(self.c.rules, 'keep_first_files', then_change)

    def test_the_reads_never_go_through_a_link_and_read_only_what_the_check_saw(self):
        def to_link(path):                       # a folder on the way swapped for a link (to a copy of itself)
            os.rename(os.path.join(path, 'scratchpad'), os.path.join(path, 'was'))
            os.symlink('was', os.path.join(path, 'scratchpad'))

        def new_file(path):                      # a file swapped for a new one of the same size
            notes = os.path.join(path, 'scratchpad', 'notes.md')
            self.file(notes + '.new', REPORT.upper())
            os.rename(notes + '.new', notes)

        def file_link(path):                     # a file swapped for a link to a file the check saw
            notes = os.path.join(path, 'scratchpad', 'notes.md')
            os.rename(notes, notes + '.was')
            os.symlink('notes.md.was', notes)
        for change in (to_link, new_file, file_link):
            with self.subTest(change=change.__name__):
                path = self.session()
                cfg = self.scfg()
                item = self.assertDeletes(self.plan(cfg), path)
                with self.swap_after_the_list(change), self.assertRaises(self.c.rules.KeepFirstError):
                    self.c.rules.keep_first(cfg, item)
                self.assertFalse(os.path.exists(os.path.join(self.archive, PROJECT, SID)))
                self.c.rules._remove_own(path)

    def test_an_output_link_is_followed_only_to_a_file_inside_the_session(self):
        path = self.session(files={'scratchpad/notes.md': REPORT})
        outside = self.file(self.path('elsewhere', 'b1.output'), 'not this session\'s\n')
        os.symlink(outside, self.mkdir('scratch', PROJECT, SID, 'tasks') + '/b1.output')
        self.age(path, 10)
        cfg = self.scfg()
        look = self.c.guard.examine(cfg, self.facts(cfg), path, 'path')
        look.seen.add(os.stat(outside).st_ino)  # even when its inode number is one the check saw (another device)
        found = self.c.rules.keep_first_files(cfg, path, look)
        self.assertEqual([name for name, _, _ in found.keep], ['scratchpad/notes.md'])
        self.assertEqual(found.out, [('tasks/b1.output', outside)])

    def test_keep_first_itself_refuses_a_report_too_large_to_copy(self):
        path = self.session(files={'scratchpad/notes.md': REPORT, 'scratchpad/dump.json': 'x' * (300 * 1024)})
        cfg = self.scfg()
        look = self.c.guard.examine(cfg, self.facts(cfg), path, 'path')
        found = self.c.rules.keep_first_files(cfg, path, look)
        look.keep_first = [(name, size) for name, size, _ in found.keep]   # as if the plan had let it through
        item = self.c.rules.Item('session', path, 'agent-scratch', '', 1, look)
        with self.assertRaisesRegex(self.c.rules.KeepFirstError, 'dump.json: larger than maxFileBytes'):
            self.c.rules.keep_first(cfg, item)
        self.assertFalse(os.path.exists(self.archive))

    def test_a_link_where_an_earlier_copy_is_kept_is_not_used_again(self):
        path = self.session()
        cfg = self.scfg()
        item = self.assertDeletes(self.plan(cfg), path)
        self.c.rules.keep_first(cfg, item)                                 # a good copy...
        moved = self.path('elsewhere', 'copy')
        os.makedirs(os.path.dirname(moved))
        os.rename(os.path.join(self.archive, PROJECT, SID), moved)
        os.symlink(moved, os.path.join(self.archive, PROJECT, SID))     # ...now behind a link in the archive
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 0, lines)
        told = [line for line in lines if line.startswith(f'kept first {path}: ')]
        self.assertEqual(len(told), 1, lines)
        self.assertNotIn('already in', told[0])
        self.assertEqual(self.kept_copy(name=SID + '.2')[1]['session'], SID)
        self.assertTrue(os.path.islink(os.path.join(self.archive, PROJECT, SID)))

    def test_a_different_older_copy_stays_and_the_new_one_gets_the_next_name(self):
        path = self.session()
        old = os.path.join(self.archive, PROJECT, SID)
        self.file(os.path.join(old, 'manifest.json'), json.dumps({'files': [{'path': 'scratchpad/notes.md', 'sha256': '0' * 64}]}))
        code, lines = self.run_cleaner(self.scfg())
        self.assertEqual(code, 0, lines)
        self.assertEqual(sorted(os.listdir(os.path.join(self.archive, PROJECT))), [SID, SID + '.2'])
        self.assertEqual(self.kept_copy(name=SID + '.2')[1]['session'], SID)
        with open(os.path.join(old, 'manifest.json')) as f:
            self.assertIn('0' * 64, f.read())

    def test_the_archive_may_not_be_in_a_folder_the_cleaner_deletes_from(self):
        for archive in (os.path.join(self.scratch, 'kept'), self.path('home'), '/'):
            with self.subTest(archive=archive), self.assertRaises(machine_file.MachineFileError):
                self.scfg({'archive': archive})


class HalfDone(Case):
    def stop_half_way(self, cfg, path):
        """A run whose delete stops after one file: the journal names the folder."""
        def half(item):
            os.unlink(os.path.join(item.path, 'scratchpad', 'shot.png'))
            raise OSError(5, 'Input/output error')
        with mock.patch.object(self.c.rules, 'delete', half):
            code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 1, lines)
        self.assertTrue(any('(half deleted: see the next plan)' in line for line in lines), lines)
        self.assertEqual([e['path'] for e in self.journal(cfg)], [path])

    def test_a_half_deleted_session_is_finished_by_the_next_run(self):
        path = self.session()
        cfg = self.scfg()
        self.stop_half_way(cfg, path)
        code, lines = self.run_cleaner(cfg, really=False)
        self.assertEqual(code, 0, lines)
        self.assertTrue(any(line.startswith(f'would delete {path} (') and 'finishing the delete an earlier run began'
                            in line for line in lines), lines)
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 0, lines)
        self.assertFalse(os.path.lexists(path))
        self.assertTrue(any('already in' in line for line in lines if line.startswith('kept first')), lines)
        self.assertEqual(self.journal(cfg), [])

    def test_a_half_deleted_session_that_changed_since_is_kept(self):
        path = self.session()
        cfg = self.scfg()
        self.stop_half_way(cfg, path)
        self.file(os.path.join(path, 'scratchpad', 'after.md'), days=-0.01)
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'kept {path} (agent-scratch: half deleted: ') and
                            'changed since the delete began' in line for line in lines), lines)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual([e['path'] for e in self.journal(cfg)], [path])

    def test_a_half_deleted_session_that_came_back_is_kept(self):
        path = self.session()
        cfg = self.scfg()
        self.stop_half_way(cfg, path)
        self.texts = [f'claude --resume {SID}']
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'kept {path} (agent-scratch: half deleted: ') and 'session alive' in line
                            for line in lines), lines)
        self.assertTrue(os.path.isdir(path))

    def test_a_file_made_after_the_last_check_stops_the_delete_even_with_a_reused_inode(self):
        path = self.session()
        cfg = self.scfg()
        alive = self.c.rules.session_alive
        made = []

        def resumed(cfg, facts, where, look):
            refusal = alive(cfg, facts, where, look)
            if look is not None and look.checked is not None:      # the last check, right before the delete
                new = self.file(os.path.join(where, 'tasks', 'new.output'), 'resumed\n')
                look.seen.add(os.lstat(new).st_ino)                # ext4 can give it a just-freed number
                made.append(new)
            return refusal
        with mock.patch.object(self.c.rules, 'session_alive', resumed):
            code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {path} ') and 'changed after the last check' in line
                            for line in lines), lines)
        self.assertTrue(made and os.path.isfile(made[0]), 'the new file stays')
        self.assertEqual([e['path'] for e in self.journal(cfg)], [path])

    def test_hard_links_do_not_stop_the_delete(self):
        path = self.session(files={'scratchpad/notes.md': REPORT, 'scratchpad/a.bin': 'shared'})
        os.link(os.path.join(path, 'scratchpad', 'a.bin'), os.path.join(path, 'scratchpad', 'b.bin'))
        self.age(path, 10)
        cfg = self.scfg()
        look = self.c.guard.examine(cfg, self.facts(cfg), path, 'path')
        self.assertIsNone(look.refusal)
        time.sleep(0.02)                                                    # past the clock's tick
        look.checked = self.c.rules.clock()
        time.sleep(0.02)                                                    # the first unlink changes the other's ctime
        self.c.rules.remove_tree(path, look)
        self.assertFalse(os.path.lexists(path))

    def test_with_the_rule_off_a_half_deleted_session_stays_half_deleted(self):
        path = self.session()
        self.stop_half_way(self.scfg(), path)
        cfg = self.cfg(agentScratch={'root': self.scratch})
        code, lines = self.run_cleaner(cfg)
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'kept {path} (agent-scratch: half deleted: since ') for line in lines), lines)
        self.assertTrue(os.path.isdir(path))


class Machine(Case):
    def test_an_old_machine_file_without_the_keys_leaves_the_rule_off(self):
        self.session()
        cfg = self.cfg()
        self.assertFalse(cfg.scratch_enabled)
        read = AssertionError('the scratch root was read')
        with mock.patch.object(self.c.rules, '_entries', side_effect=read):
            plan = self.plan(cfg)
        self.assertEqual((plan.items, plan.kept), ([], []))
        self.assertEqual(self.cfg(agentScratch={}).scratch_enabled, False)
        self.assertTrue(os.path.isdir(self.path('scratch', PROJECT, SID)))

    def test_an_empty_or_missing_root_is_nothing_to_do(self):
        cfg = self.scfg()
        self.assertEqual(self.run_cleaner(cfg), (0, ['total: deleted 0 (0 B), kept 0, failed 0']))
        os.rmdir(self.scratch)
        self.assertEqual(self.run_cleaner(cfg), (0, ['total: deleted 0 (0 B), kept 0, failed 0']))
        self.mkdir('scratch', PROJECT)
        self.assertEqual(self.run_cleaner(cfg)[0], 0)

    def test_the_root_and_the_keys_are_checked(self):
        cfg = self.cfg(agentScratch={'enabled': True, 'root': self.path('u{uid}'), 'keepFirst': {'archive': self.archive}})
        self.assertEqual(cfg.scratch_root, self.path(f'u{os.getuid()}'))
        self.assertEqual(self.cfg().scratch_root, f'/tmp/claude-{os.getuid()}')
        self.assertEqual(self.cfg().keep_archive, self.path('home', '.local', 'state', 'fork-cleaner', 'kept'))
        linked = self.path('linked-scratch')
        os.symlink(self.scratch, linked)
        for bad in ({'root': self.path('x{user}')}, {'root': self.path('home', '..')}, {'root': '/'}, {'days': 2},
                    {'root': '/tmp'}, {'root': '/tmp/'}, {'root': '/var/tmp'}, {'root': '/dev/shm'}, {'root': self.home},
                    {'root': linked}, {'root': self.file(self.path('a-file'))}, {'transcripts': []},
                    {'transcripts': ['relative/*']}, {'keepFirst': {'patterns': []}}, {'keepFirst': {'outputs': []}},
                    {'keepFirst': {'patterns': ['../x.md']}}, {'keepFirst': {'patterns': ['/abs/*.md']}},
                    {'keepFirst': {'patterns': ['a//b']}}, {'keepFirst': {'maxFileBytes': 1000}},
                    {'keepFirst': {'maxSessionBytes': -1}}, {'enabled': 'yes'}, {'keepFirst': {'other': 1}}):
            with self.subTest(bad=bad), self.assertRaises(machine_file.MachineFileError):
                self.cfg(agentScratch={'enabled': True, 'root': self.scratch, **bad})

    @unittest.skipIf(os.geteuid() == 0, 'root owns the system folders')
    def test_the_root_is_one_users_own_folder(self):
        for other in ('/usr', '/var/log'):
            with self.subTest(other=other), self.assertRaises(machine_file.MachineFileError):
                self.cfg(agentScratch={'enabled': True, 'root': other, 'keepFirst': {'archive': self.archive}})
        self.assertEqual(self.scfg().scratch_root, self.scratch)
        self.cfg(agentScratch={'enabled': True, 'root': self.path('not-yet'), 'keepFirst': {'archive': self.archive}})
        with mock.patch('tempfile.gettempdir', return_value=self.tmp):       # $TMPDIR: this user's, still shared
            with self.assertRaises(machine_file.MachineFileError):
                self.cfg(agentScratch={'enabled': True, 'root': self.tmp, 'keepFirst': {'archive': self.archive}})
            # The same folder reached through a link in the path: lstat sees a real folder of this user, only the
            # real path shows it is the shared one.
            os.symlink(os.path.dirname(self.tmp), self.path('up'))
            through = self.path('up', os.path.basename(self.tmp))
            with self.assertRaises(machine_file.MachineFileError):
                self.cfg(agentScratch={'enabled': True, 'root': through, 'keepFirst': {'archive': self.archive}})
        runtime = self.mkdir('runtime')                                    # the user's runtime folder: shared too
        for env, root in (({'XDG_RUNTIME_DIR': runtime}, runtime), ({}, f'/run/user/{os.getuid()}')):
            with self.subTest(root=root), mock.patch.dict(os.environ, env):
                if not env:
                    os.environ.pop('XDG_RUNTIME_DIR', None)
                with self.assertRaises(machine_file.MachineFileError):
                    self.cfg(agentScratch={'enabled': True, 'root': root, 'keepFirst': {'archive': self.archive}})

    def test_the_root_never_overlaps_a_transcript_folder(self):
        logs = self.mkdir('logs', 'cfg', 'projects')
        self.mkdir('logs', 'cfg', 'projects', PROJECT)
        os.symlink(self.path('logs', 'cfg'), self.path('linked-cfg'))
        globs = [self.path('logs', '*', 'projects', '*')]
        for root in (logs,                                                 # the transcripts' own folder
                     self.path('logs', 'cfg', 'projects', PROJECT),        # one the pattern matches
                     self.path('logs', 'cfg', 'projects', PROJECT, 'deeper'),    # inside one
                     self.path('logs', 'cfg'),                             # holds one
                     self.path('linked-cfg', 'projects')):                 # the same, through a link
            with self.subTest(root=root), self.assertRaises(machine_file.MachineFileError):
                self.cfg(agentScratch={'enabled': True, 'root': root, 'transcripts': globs,
                                       'keepFirst': {'archive': self.archive}})
        with self.assertRaises(machine_file.MachineFileError):            # no folder matches yet: by the real path
            self.cfg(agentScratch={'enabled': True, 'root': self.path('linked-cfg', 'projects'),
                                   'transcripts': [self.path('logs', '*', 'projects', 'none-*')],
                                   'keepFirst': {'archive': self.archive}})
        os.symlink(logs, self.path('scratch-link-target'))                # a pattern that leads through a link
        with self.assertRaises(machine_file.MachineFileError):
            self.cfg(agentScratch={'enabled': True, 'root': logs, 'transcripts': [self.path('scratch-link-*', '*')],
                                   'keepFirst': {'archive': self.archive}})
        self.assertEqual(self.cfg(agentScratch={'enabled': True, 'root': self.scratch, 'transcripts': globs,
                                                'keepFirst': {'archive': self.archive}}).scratch_root, self.scratch)

    def test_the_compare_reads_the_rule_name(self):
        lines = [f'would delete /s/{SID} (1.0 KB, agent-scratch: session gone)',
                 f'kept /s/{OTHER} (agent-scratch: session alive: a process names its id)']
        would, kept = self.c.compare.read_plan(lines)
        self.assertEqual(would, {f'/s/{SID}': ('agent-scratch', 'session gone')})
        self.assertEqual(list(kept), [f'/s/{OTHER}'])


MUTATIONS = {
    'session alive': ('rules', 'if processes.names(sid):', 'if False:', Refusals, 'test_a_live_session_is_kept_by_its_command_line'),
    'session alive: whole words': ('facts', 'return word in self.words', 'return any(word in w for w in self.words)',
                                   Refusals, 'test_a_live_session_is_kept_by_its_command_line'),
    'session alive: environment': ('facts', "found = WORD.findall(f.read().decode('utf-8', 'surrogateescape'))",
                                   'found = []', Refusals, 'test_a_live_session_is_kept_by_a_child_process_environment'),
    'session alive: a handle or the folder in it, asked again': ('rules', '    if processes.uses(path):\n        return Refusal(',
                                                                 '    if False:\n        return Refusal(',
                                                                 Refusals, 'test_a_process_with_a_handle_or_its_folder_in_it_keeps_it'),
    'session alive: after /clear': ('rules', 'if p.start is None or p.start <= newest:', 'if False:',
                                    Refusals, 'test_a_session_cleared_into_a_new_id_is_kept_by_its_process'),
    'session alive: after /clear, only before this one changed': ('rules', 'if p.start is None or p.start <= newest:',
                                                                  'if True:', Refusals,
                                                                  'test_a_session_cleared_into_a_new_id_is_kept_by_its_process'),
    'session alive: after /clear, not a session it names': ('rules', 'if other == sid or (SESSION_NAME.match(other) and other in p.words):',
                                                            'if other == sid:', Refusals,
                                                            'test_a_session_cleared_into_a_new_id_is_kept_by_its_process'),
    'session alive: a process\'s own open files': ('facts', '            paths.add(target)           # an open file\n            one.paths.add(target)',
                                                  '            paths.add(target)           # an open file', Refusals,
                                                  'test_a_session_cleared_into_a_new_id_is_kept_by_its_process'),
    'session alive: when a process started': ('facts', 'one.start, one.parent = _stat(base, boot)',
                                              'one.start, one.parent = None, _stat(base, boot)[1]',
                                              Refusals, 'test_a_session_cleared_into_a_new_id_is_kept_by_its_process'),
    'could not read: a hidden process of the user': ('rules', "    if unread:\n        return Refusal('could not read'",
                                                     "    if False:\n        return Refusal('could not read'",
                                                     Refusals, 'test_a_process_of_the_user_that_cannot_be_read_keeps_it'),
    'could not read: helpers that hide on purpose': ('rules', 'p.hidden and p.name not in HIDDEN_OK', 'p.hidden',
                                                     Refusals, 'test_a_process_of_the_user_that_cannot_be_read_keeps_it'),
    'could not read: open files hidden': ('facts', 'fds = []\n            one.hidden = one.hidden or (mine and ex.errno in (errno.EACCES, errno.EPERM))',
                                          'fds = []', Refusals, 'test_a_process_of_the_user_that_cannot_be_read_keeps_it'),
    'could not read: a hidden process is its readable ancestor\'s': ('rules', 'and _judged_by(p, processes.own) is None]', ']',
                                                                    Refusals, 'test_a_hidden_process_is_judged_by_its_nearest_readable_ancestor'),
    'could not read: the parent from stat': ('facts', 'one.start, one.parent = _stat(base, boot)',
                                             'one.start, one.parent = _stat(base, boot)[0], None',
                                             Refusals, 'test_a_hidden_process_is_judged_by_its_nearest_readable_ancestor'),
    'could not read: the walk goes past a hidden parent': ('rules', '        pid = q.parent\n    return None', '        return None\n    return None',
                                                           Refusals, 'test_a_hidden_process_is_judged_by_its_nearest_readable_ancestor'),
    'could not read: the walk stops at the service manager': ('rules', 'if q is None or q.name == USER_MANAGER:', 'if q is None:',
                                                              Refusals, 'test_a_hidden_process_is_judged_by_its_nearest_readable_ancestor'),
    'session alive: the working folder': ('facts', 'paths.add(target)           # its folder, root or program',
                                          "paths.add(target) if name == 'exe' else None",
                                          Refusals, 'test_a_process_with_a_handle_or_its_folder_in_it_keeps_it'),
    'session alive: a helper\'s readable parts': ('facts', 'paths.add(target)           # its folder, root or program',
                                                  "paths.add(target) if name == 'exe' else None",
                                                  Refusals, 'test_a_helper_name_excuses_only_what_cannot_be_read'),
    'session alive: asked again after keep first': ('engine', 'refusal = rules.session_alive(cfg, facts, item.path, item.look)',
                                                    'refusal = None', Refusals, 'test_the_session_comes_back_while_keep_first_copies'),
    'transcripts: a folder that is not there is no answer': ('facts', "raise Unreadable(f'no folder matches {pattern}')", 'pass',
                                                             Refusals, 'test_the_transcript_folders_come_from_the_machine_file'),
    'lock file: in the session folder': ('rules', "        look.refusal = Refusal('lock file', some(locks))\n", '',
                                         Refusals, 'test_a_lock_file_keeps_it'),
    'transcript': ('rules', 'if said is not None and facts.now - said < days * DAY:', 'if False:',
                   Refusals, 'test_a_recent_transcript_keeps_it'),
    'days unchanged': ('rules', "        if age < days * DAY:\n            return None\n        why = f'session gone",
                       "        if False:\n            return None\n        why = f'session gone", Refusals, 'test_a_recent_change_keeps_it'),
    'too much to keep first': ('rules', 'if total > cfg.keep_max_session:', 'if False:',
                               Rule, 'test_too_much_to_keep_first_keeps_the_session_whole'),
    'report too large: the plan keeps it': ('rules', "    if found.held:\n        look.refusal", "    if False:\n        look.refusal",
                                            Rule, 'test_a_report_too_large_to_keep_keeps_the_session'),
    'task outputs are kept first': ('rules', '((False, cfg.keep_patterns), (True, cfg.keep_outputs))', '((False, cfg.keep_patterns),)',
                                    Rule, 'test_task_outputs_are_kept_first_and_a_larger_one_is_named'),
    'a larger output is named, not held': ('rules', '                elif outputs:\n', '                elif False:\n',
                                           Rule, 'test_task_outputs_are_kept_first_and_a_larger_one_is_named'),
    'an output link resolved inside': ('rules', '                    source, st = resolved', '                    continue',
                                       Rule, 'test_task_outputs_are_kept_first_and_a_larger_one_is_named'),
    'keep first cannot be turned off': ('config', "    if not value:\n        return 'must name at least one pattern (keep first",
                                        "    if False:\n        return 'must name at least one pattern (keep first",
                                        Rule, 'test_patterns_from_the_machine_file_pick_what_is_kept_first'),
    'keep first before the delete': ('engine', 'kept = rules.keep_first(cfg, item)', 'kept = None',
                                     Rule, 'test_a_gone_idle_session_is_deleted_after_its_reports_are_kept'),
    'a failed keep first stops the delete': ('engine', "out(f'FAILED {item.label} ({item.rule}: keep first: {ex}): nothing deleted')\n                continue",
                                             "kept = None", KeepFirst, 'test_the_copy_is_verified_before_anything_is_deleted'),
    'verify the copy before it is the kept one': ('rules', 'if not _verified(partial, wanted, named):', 'if False:',
                                                  KeepFirst, 'test_the_copy_is_verified_before_anything_is_deleted'),
    'a verified copy is used again': ('rules', 'if _verified(version, wanted, named):', 'if False:',
                                      KeepFirst, 'test_a_copy_made_by_a_run_that_stopped_before_the_delete_is_used_again'),
    'a half-written copy: a link is not followed': ('rules', 'st = os.lstat(partial)', 'st = os.stat(partial)',
                                                    KeepFirst, 'test_a_link_at_the_half_written_copy_is_removed_as_a_link'),
    'keep first: folders entered never through a link': ('rules', 'for part in parts[:-1]:\n            new = os.open(part, DIR_FLAGS, dir_fd=fd)',
                                                         'for part in parts[:-1]:\n            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)',
                                                         KeepFirst, 'test_the_reads_never_go_through_a_link_and_read_only_what_the_check_saw'),
    'keep first: each file read is the one the check saw': ('rules', 'if not stat.S_ISREG(st.st_mode) or st.st_ino not in look.seen:\n        os.close(out)',
                                                            'if not stat.S_ISREG(st.st_mode):\n        os.close(out)',
                                                            KeepFirst, 'test_the_reads_never_go_through_a_link_and_read_only_what_the_check_saw'),
    'keep first: a file opened never through a link': ('rules', '        out = os.open(parts[-1], NO_FOLLOW, dir_fd=fd)',
                                                       '        out = os.open(parts[-1], os.O_RDONLY, dir_fd=fd)',
                                                       KeepFirst, 'test_the_reads_never_go_through_a_link_and_read_only_what_the_check_saw'),
    'an output link stays inside the session': ("rules", "    if not target.startswith(path + '/'):\n        return None",
                                                "    if False:\n        return None",
                                                KeepFirst, 'test_an_output_link_is_followed_only_to_a_file_inside_the_session'),
    'report too large: keep first refuses it itself': ('rules', "    if found.held:\n        raise KeepFirstError", "    if False:\n        raise KeepFirstError",
                                                       KeepFirst, 'test_keep_first_itself_refuses_a_report_too_large_to_copy'),
    'a reused copy is a real folder': ('rules', '        if not stat.S_ISDIR(os.lstat(folder).st_mode):\n            return False',
                                       '        if False:\n            return False',
                                       KeepFirst, 'test_a_link_where_an_earlier_copy_is_kept_is_not_used_again'),
    'keep first: what the last check saw': ('rules', "        raise KeepFirstError('what is worth keeping is not what the last check saw", "        pass  # ('",
                                            KeepFirst, 'test_a_folder_swapped_for_a_link_after_the_check_is_not_read_through'),
    'archive: new folders synced into their parents': ('rules', '        _sync_folder(os.path.dirname(folder))\n', '',
                                                       KeepFirst, 'test_the_archive_folders_are_this_users_real_folders_synced_before_the_delete'),
    'archive: no copy through a link': ('rules', "        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():\n            raise KeepFirstError(f'{here}",
                                        "        if False:\n            raise KeepFirstError(f'{here}",
                                        KeepFirst, 'test_the_archive_folders_are_this_users_real_folders_synced_before_the_delete'),
    'changed since the delete began': ('rules', 'if look.newest_file > started:', 'if False:',
                                       HalfDone, 'test_a_half_deleted_session_that_changed_since_is_kept'),
    'a file changed after the last check stops the delete': ('rules', '            if (look.checked is not None and st.st_ctime >= look.checked and  # made since',
                                                             '            if (False and  # made since',
                                                             HalfDone, 'test_a_file_made_after_the_last_check_stops_the_delete_even_with_a_reused_inode'),
    'the last check sets when it began': ('engine', 'item.look.checked = rules.clock()', 'pass',
                                          HalfDone, 'test_a_file_made_after_the_last_check_stops_the_delete_even_with_a_reused_inode'),
    'hard links do not stop the delete': ('rules', '(st.st_nlink == 1 and st.st_ino not in look.linked)', 'True',
                                          HalfDone, 'test_hard_links_do_not_stop_the_delete'),
    'finish a half delete': ('engine', 'if isinstance(again, rules.Item):', 'if False:',
                             HalfDone, 'test_a_half_deleted_session_is_finished_by_the_next_run'),
    'one journal line per delete': ('engine', 'if journaled and not prior:\n            journal.append(entry)',
                                    'if journaled:\n            journal.append(entry)',
                                    HalfDone, 'test_a_half_deleted_session_is_finished_by_the_next_run'),
    'report: the keep-first line is the cleaner\'s own': ('report', "'kept first ', ", '',
                                                         Rule, 'test_a_gone_idle_session_is_deleted_after_its_reports_are_kept'),
    'off unless the machine file turns it on': ('config', "'enabled': (bool, False, None),\n    'root': (str, '/tmp/claude-{uid}'",
                                                 "'enabled': (bool, True, None),\n    'root': (str, '/tmp/claude-{uid}'",
                                                 Machine, 'test_an_old_machine_file_without_the_keys_leaves_the_rule_off'),
    'root: not a shared temp folder': ('config', 'if root in shared or os.path.realpath(root) in shared:', 'if False:',
                                       Machine, 'test_the_root_is_one_users_own_folder'),
    'root: a real folder': ('config', "        if not stat.S_ISDIR(st.st_mode):\n            raise machine_file.MachineFileError(f'{where}: {text} is a link",
                            "        if False:\n            raise machine_file.MachineFileError(f'{where}: {text} is a link",
                            Machine, 'test_the_root_and_the_keys_are_checked'),
    'root: not the user\'s runtime folder': ('config', "SHARED_TEMP + runtime + (tempfile.gettempdir(),)",
                                              "SHARED_TEMP[:-1] + (tempfile.gettempdir(),)", Machine, 'test_the_root_is_one_users_own_folder'),
    'root: not a transcript folder': ('config', "            if held is not None:\n                raise machine_file.MachineFileError(f'{where}.agentScratch.root",
                                      "            if False:\n                raise machine_file.MachineFileError(f'{where}.agentScratch.root",
                                      Machine, 'test_the_root_never_overlaps_a_transcript_folder'),
    'root: a transcript folder by its real path': ('config', '        for path in (root, real):', '        for path in (root,):',
                                                   Machine, 'test_the_root_never_overlaps_a_transcript_folder'),
    'root: a transcript folder a pattern leads to': ('config', '            if os.path.isdir(folder) and (_holds(real, folder) or _holds(folder, real)):',
                                                     '            if False:', Machine, 'test_the_root_never_overlaps_a_transcript_folder'),
    'root: this user\'s own': ('config', 'if st.st_uid != os.getuid():', 'if False:', Machine, 'test_the_root_is_one_users_own_folder'),
    'the archive not where the cleaner deletes': ('config', "if self.scratch_enabled else ()", "if False else ()",
                                                  KeepFirst, 'test_the_archive_may_not_be_in_a_folder_the_cleaner_deletes_from'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main()
