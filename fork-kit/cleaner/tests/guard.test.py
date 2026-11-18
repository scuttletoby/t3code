"""The cleaner's hard refusals (lib/cleaner/guard.py and the readers in facts.py): what is never deleted, whatever
a rule or the config says. Each test asks the guard about one path in a temporary folder. RedCheck breaks each
refusal in a copy of the code and shows its test then fails.

  python3 -I -B fork-kit/cleaner/tests/guard.test.py
"""
import fcntl, os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cleaner_fixture as fx  # noqa: E402

import machine_file  # noqa: E402


class GuardCase(fx.Case):
    c = fx.load()

    def ask(self, path, kind='path', facts=None, **section):
        """The guard's answer for `path`, with its folder as the temp rule's root."""
        cfg = self.cfg(tmp={'root': os.path.dirname(path), 'patterns': {'*': 1}}, **section)
        return self.c.guard.refusal(cfg, self.facts(cfg, **(facts or {})), path, kind)

    def refused(self, path, code, detail=None, **kw):
        got = self.ask(path, **kw)
        self.assertIsNotNone(got, f'{path} may be deleted')
        self.assertEqual(got.code, code, got)
        if detail is not None:
            self.assertIn(detail, got.detail)

    def allowed(self, path, **kw):
        self.assertIsNone(self.ask(path, **kw))

    def folder(self, *parts):
        d = self.mkdir(*parts)
        self.file(os.path.join(d, 'data.bin'))
        return d


class MustKeep(GuardCase):
    def test_a_plain_folder_may_be_deleted(self):
        self.allowed(self.folder('tmp', 'scratch-1'))
        self.allowed(self.file(self.path('tmp', 'notes.txt')))

    def test_transcripts_sessions_and_t3_data_are_never_deleted(self):
        for inside in ('.claude/projects', '.claude-second/projects', '.codex/sessions', '.t3/userdata'):
            with self.subTest(must_keep=inside):
                kept = self.folder('home', *inside.split('/'))
                self.refused(kept, 'must keep')                                     # the folder itself
                self.refused(self.folder(kept, 'thread-1'), 'must keep')            # anything in it
        self.allowed(self.folder('home', '.claude', 'cache'))

    def test_a_log_archive_and_repair_backups_are_never_deleted_wherever_they_are(self):
        for name in ('repair-backups', 't3-log-archive', 'log-archive'):
            with self.subTest(name=name):
                self.refused(self.folder('tmp', 'a', name), 'must keep')
                self.refused(self.folder('tmp', 'b', name, '2026-11'), 'must keep')
                self.folder('tmp', 'c-' + name, 'deep', 'er', name)
                self.refused(self.path('tmp', 'c-' + name), 'must keep', name)

    def test_the_config_adds_to_the_list(self):
        mine = self.folder('home', 'books', 'ledger')
        elsewhere = self.folder('data', 'x', 'books-2026')
        holder = self.path('data', 'x')
        for path in (mine, elsewhere, holder):
            self.allowed(path)
        more = ['~/books/ledger', self.path('data', 'x', 'books-*')]
        self.refused(mine, 'must keep', mustKeep=more)
        self.refused(elsewhere, 'must keep', mustKeep=more)
        self.refused(self.folder('home', 'books', 'ledger', 'q3'), 'must keep', mustKeep=more)       # anything in it
        self.refused(holder, 'must keep', mustKeep=more)                                    # a folder that holds it
        self.allowed(self.folder('data', 'y'), mustKeep=more)

    def test_the_config_cannot_take_anything_off_the_list(self):
        kept = self.folder('home', '.codex', 'sessions')
        for more in ([], ['~/other']):
            self.refused(kept, 'must keep', mustKeep=more)
            cfg = self.cfg(mustKeep=more)
            for built_in in self.c.config.MUST_KEEP:
                self.assertIn(built_in, cfg.must_keep_text)
            self.assertEqual(cfg.must_keep_names[:len(self.c.config.MUST_KEEP_NAMES)], self.c.config.MUST_KEEP_NAMES)
        for key in ('mustKeepNot', 'allow', 'mayDelete', 'mustKeepNames'):
            with self.assertRaises(machine_file.MachineFileError):
                self.cfg(**{key: ['~/.codex/sessions']})


class InUse(GuardCase):
    def proc(self, processes, me=100, locks='', sockets=()):
        root = self.fake_proc({100: {'cwd': '/'}, **processes}, locks, sockets)
        return {'processes': lambda: self.c.facts.read_processes(root, me)}

    def test_a_process_using_it_keeps_it(self):
        d = self.folder('tmp', 'job-1')
        inner = os.path.join(d, 'data.bin')
        for how, process in {
            'working folder': {'cwd': d}, 'working folder below': {'cwd': self.mkdir('tmp', 'job-1', 'sub')},
            'root folder': {'root': d}, 'program': {'exe': os.path.join(d, 'tool')}, 'open file': {'fd': [inner]},
            'open deleted file': {'fd': [inner + ' (deleted)']}, 'mapped file': {'maps': [inner]},
            'command line': {'cmdline': ['server', '--data', d]},
            'command line option': {'cmdline': ['server', f'--data={d}/x.db']},
            'command line text': {'cmdline': ['sh', '-c', f'cd {d}; ./run']},
        }.items():
            with self.subTest(how=how):
                self.refused(d, 'in use', facts=self.proc({200: process}))
        spaced = self.folder('tmp', 'job with space')
        self.refused(spaced, 'in use', facts=self.proc({200: {'cmdline': ['server', os.path.join(spaced, 'db')]}}))

    def test_a_socket_a_process_listens_on_keeps_it(self):
        d = self.folder('tmp', 'job-1')
        self.refused(d, 'in use', facts=self.proc({}, sockets=[os.path.join(d, 'run', 'agent.sock')]))
        self.allowed(d, facts=self.proc({}, sockets=[d + '-other/agent.sock', '@' + d]))

    def test_a_process_using_a_neighbour_does_not(self):
        d = self.folder('tmp', 'job-1')
        other = self.folder('tmp', 'job-10')
        facts = self.proc({200: {'cwd': other, 'fd': [os.path.join(other, 'data.bin')],
                                 'cmdline': ['server', other, f'--log={d}.log']}})
        self.allowed(d, facts=facts)
        self.refused(other, 'in use', facts=facts)

    def test_the_real_process_list_sees_an_open_file_and_a_held_lock(self):
        d = self.folder('tmp', 'job-1')
        live = {'processes': self.c.facts.read_processes}
        self.allowed(d, facts=live)
        with open(os.path.join(d, 'data.bin')) as f:
            self.refused(d, 'in use', facts=live)
            fcntl.flock(f, fcntl.LOCK_EX)
            locks = self.c.facts.read_processes().locks
            self.assertIn(os.fstat(f.fileno()).st_ino, locks)
        self.allowed(d, facts=live)

    def test_a_held_lock_inside_keeps_it(self):
        d = self.folder('tmp', 'job-1')
        ino = os.stat(os.path.join(d, 'data.bin')).st_ino
        self.refused(d, 'in use', 'lock', facts=self.proc({}, locks=f'1: FLOCK  ADVISORY  WRITE 200 08:01:{ino} 0 EOF\n'))
        self.allowed(d, facts=self.proc({}, locks=f'1: FLOCK  ADVISORY  WRITE 200 08:01:{ino + 100000} 0 EOF\n'))

    def test_no_process_list_means_keep(self):
        d = self.folder('tmp', 'job-1')
        missing = {'processes': lambda: self.c.facts.read_processes(self.path('no-proc'), 100)}
        self.refused(d, 'could not read', 'process list', facts=missing)
        self.refused(d, 'could not read', 'process list', facts=self.proc({}, me=4242))      # not even ourselves
        proc = self.proc({})
        os.unlink(self.path('proc', 'locks'))
        self.refused(d, 'could not read', 'process list', facts=proc)
        proc = self.proc({})
        os.unlink(self.path('proc', 'net', 'unix'))
        self.refused(d, 'could not read', 'process list', facts=proc)

    def units(self, text):
        d = self.mkdir('units')
        self.file(os.path.join(d, 'job.service'), text)
        return {'units': lambda: self.c.facts.read_units([d, self.path('no-such-units')])}

    def test_a_systemd_unit_that_names_it_keeps_it(self):
        d = self.folder('tmp', 'job-1')
        self.refused(d, 'in use', 'systemd unit', facts=self.units(f'[Service]\nExecStart={d}/bin/run --fast\n'))
        self.refused(d, 'in use', 'systemd unit', facts=self.units(f'[Service]\nWorkingDirectory={d}\n'))
        self.allowed(d, facts=self.units(f'[Service]\nExecStart={d}-other/bin/run\nWorkingDirectory={self.tmp}\n'))

    @unittest.skipIf(os.getuid() == 0, 'root reads every file')
    def test_an_unreadable_unit_means_keep(self):
        d = self.folder('tmp', 'job-1')
        facts = self.units('[Service]\n')
        os.chmod(self.path('units', 'job.service'), 0)
        self.refused(d, 'could not read', 'systemd unit', facts=facts)

    def test_a_keep_file_keeps_it_and_gives_the_reason(self):
        d = self.folder('tmp', 'job-1')
        self.file(os.path.join(d, 'KEEP'), 'bench data for the open bug\nsecond line\n')
        self.refused(d, 'KEEP file', 'bench data for the open bug')
        self.file(os.path.join(d, 'KEEP'), '')
        self.refused(d, 'KEEP file', 'no reason given')

    def test_a_lock_file_is_never_deleted(self):
        for name in ('build.lock', 'LOCK', '.lock', 'db.lck'):
            with self.subTest(name=name):
                self.refused(self.file(self.path('tmp', name)), 'lock file')


class PathSafety(GuardCase):
    def test_only_what_is_directly_in_a_configured_root(self):
        d = self.folder('tmp', 'job-1')
        cfg = self.tmp_cfg()
        facts = self.facts(cfg)
        self.assertIsNone(self.c.guard.refusal(cfg, facts, d, 'path'))
        for path in (self.tmp, os.path.dirname(self.tmp), os.path.join(d, 'data.bin'), self.folder('elsewhere'),
                     self.tmp + '/', os.path.join(self.tmp, 'x', '..', 'job-1'), 'job-1', '/'):
            with self.subTest(path=path):
                self.assertEqual(self.c.guard.refusal(cfg, facts, path, 'path').code, 'not in a configured root')

    def test_a_symbolic_link_is_never_followed(self):
        target = self.folder('real')
        link = self.path('tmp', 'link')
        os.symlink(target, link)
        self.refused(link, 'symbolic link')
        os.symlink(self.tmp, self.path('tmp-link'))
        self.folder('tmp', 'job-1')
        self.refused(self.path('tmp-link', 'job-1'), 'symbolic link')
        self.allowed(self.path('tmp', 'job-1'))

    def test_another_filesystem(self):
        d = self.folder('tmp', 'job-1')
        sub = self.folder('tmp', 'job-2', 'mounted')
        real = self.c.guard.device
        for other in (d, sub):
            with mock.patch.object(self.c.guard, 'device', lambda st, ino=os.lstat(other).st_ino: -1 if st.st_ino == ino else real(st)):
                self.refused(d if other == d else os.path.dirname(sub), 'another filesystem')
        self.allowed(d)

    def test_another_owner(self):
        d = self.folder('tmp', 'job-1')
        inner = self.file(self.path('tmp', 'job-2', 'deep', 'theirs.bin'))
        real = self.c.guard.owner
        for other, candidate in ((d, d), (inner, self.path('tmp', 'job-2'))):
            with mock.patch.object(self.c.guard, 'owner', lambda st, ino=os.lstat(other).st_ino: 1 if st.st_ino == ino else real(st)):
                self.refused(candidate, 'another owner')
        self.allowed(d)

    def test_credential_looking_names(self):
        for name in ('.env', '.env.local', 'prod.env', 'id_rsa', 'id_ed25519.pub', 'server.pem', 'signing.key',
                     'api-token.txt', 'credentials.json', 'db-password', 'client_secret.json', '.netrc', 'store.p12'):
            with self.subTest(name=name):
                self.refused(self.file(self.path('tmp', name)), 'credential name')
                self.file(self.path('tmp', 'holds-' + name, 'a', 'b', name))
                self.refused(self.path('tmp', 'holds-' + name), 'credential name', name)
        self.allowed(self.file(self.path('tmp', 'notes.txt')))

    def test_only_plain_files_and_folders(self):
        fifo = self.path('tmp', 'pipe')
        os.mkfifo(fifo)
        self.refused(fifo, 'not a file or folder')
        self.refused(self.path('tmp', 'gone'), 'could not read')

    @unittest.skipIf(os.getuid() == 0, 'root reads every folder')
    def test_a_folder_it_cannot_read_means_keep(self):
        d = self.folder('tmp', 'job-1')
        os.chmod(self.folder('tmp', 'job-1', 'closed'), 0)
        self.refused(d, 'could not read')


class GitFolders(GuardCase):
    """A folder that is, or holds, a git repository: only when nothing in it exists there alone."""

    def job(self, name='job-1'):
        """A clone in tmp/<name>/repo: clean, everything pushed."""
        d = self.mkdir('tmp', name)
        return d, self.clone('repo', into=d)

    def test_a_clean_clone_with_everything_pushed_may_go(self):
        d, repo = self.job()
        self.allowed(d)
        self.allowed(self.clone('clone-1', into=self.tmp))
        self.commit(repo)
        self.push(repo)
        self.allowed(d)

    def test_uncommitted_and_untracked_changes(self):
        d, repo = self.job()
        self.file(os.path.join(repo, 'README.md'), 'changed\n')
        self.refused(d, 'git: uncommitted or untracked')
        d, repo = self.job('job-2')
        self.file(os.path.join(repo, 'new.txt'))
        self.refused(d, 'git: uncommitted or untracked')
        self.refused(repo, 'git: uncommitted or untracked')

    def test_unpushed_commits(self):
        d, repo = self.job()
        self.commit(repo)
        self.refused(d, 'git: unpushed commits')
        self.push(repo)
        self.allowed(d)
        self.git(repo, 'checkout', '-q', '-b', 'side')
        self.commit(repo)
        self.git(repo, 'checkout', '-q', 'main')
        self.refused(d, 'git: unpushed commits')

    def test_a_commit_only_the_reflog_still_has(self):
        d, repo = self.job()
        self.commit(repo)
        self.git(repo, 'reset', '-q', '--hard', 'origin/main')
        self.refused(d, 'git: unpushed commits')

    def test_a_repository_that_never_had_a_remote(self):
        d = self.mkdir('tmp', 'job-1')
        self.git(d, 'init', '-q')
        self.allowed(d)                              # nothing in it yet
        self.commit(d)
        self.refused(d, 'git: unpushed commits')
        bare = self.mkdir('tmp', 'job-2', 'store.git')
        self.git(d, 'clone', '-q', '--bare', d, bare)
        self.git(bare, 'remote', 'remove', 'origin')
        self.refused(self.path('tmp', 'job-2'), 'git: unpushed commits')

    def test_stash_entries(self):
        d, repo = self.job()
        self.file(os.path.join(repo, 'README.md'), 'half done\n')
        self.git(repo, 'stash')
        self.assertEqual(self.git(repo, 'status', '--porcelain'), '')
        self.refused(d, 'git: stash entries')

    def test_a_clone_other_worktrees_hang_off(self):
        d, repo = self.job()
        self.worktree(repo, 'side', into=self.mkdir('elsewhere'))
        self.refused(d, 'git: worktrees')

    def test_git_that_cannot_answer_means_keep(self):
        d, repo = self.job()
        self.file(os.path.join(repo, '.git', 'HEAD'), 'not a ref\n')
        self.refused(d, 'could not read', 'git')
        d, repo = self.job('job-2')
        self.allowed(d)
        with mock.patch.object(self.c.facts, 'GIT', self.path('no-such-git')):
            self.refused(d, 'could not read', 'git')


MUTATIONS = {
    'must keep: the built-in paths': ('guard', 'for pattern in cfg.must_keep:', 'for pattern in ():',
                                      MustKeep, 'test_transcripts_sessions_and_t3_data_are_never_deleted'),
    'must keep: a name on the path': ('guard', 'if kept_name(cfg, part)', 'if False',
                                      MustKeep, 'test_a_log_archive_and_repair_backups_are_never_deleted_wherever_they_are'),
    'must keep: a name inside': ('guard', 'if kept_name(cfg, entry.name)', 'if False',
                                 MustKeep, 'test_a_log_archive_and_repair_backups_are_never_deleted_wherever_they_are'),
    'must keep: the config adds': ('config', "MUST_KEEP + tuple(values['mustKeep'])", 'MUST_KEEP',
                                   MustKeep, 'test_the_config_adds_to_the_list'),
    'must keep: the config cannot remove': ('config', "MUST_KEEP + tuple(values['mustKeep'])", "tuple(values['mustKeep'])",
                                            MustKeep, 'test_the_config_cannot_take_anything_off_the_list'),
    'in use: a process': ('guard', 'if path in processes.busy:', 'if False:', InUse, 'test_a_process_using_it_keeps_it'),
    'in use: the real process list': ('facts', 'paths.add(target)', 'pass',
                                      InUse, 'test_a_process_using_it_keeps_it'),
    'in use: open files': ('facts', "paths.update(links(os.path.join(base, 'fd')))", 'pass',
                           InUse, 'test_the_real_process_list_sees_an_open_file_and_a_held_lock'),
    'in use: a socket': ('facts', 'paths.add(fields[7])', 'pass', InUse, 'test_a_socket_a_process_listens_on_keeps_it'),
    'in use: a held lock': ('guard', 'if entry.inode() in processes.locks', 'if False', InUse, 'test_a_held_lock_inside_keeps_it'),
    'in use: no process list': ('guard', "return Refusal('could not read', f'the process list ({ex})')", 'processes = EMPTY',
                                InUse, 'test_no_process_list_means_keep'),
    'in use: our own process must be in the list': ('facts', 'if me not in seen:', 'if False:', InUse, 'test_no_process_list_means_keep'),
    'in use: a systemd unit': ('guard', 'if path in units:', 'if False:', InUse, 'test_a_systemd_unit_that_names_it_keeps_it'),
    'in use: an unreadable unit': ('guard', "return Refusal('could not read', f'a systemd unit ({ex})')", 'units = ()',
                                   InUse, 'test_an_unreadable_unit_means_keep'),
    'KEEP file': ('guard', 'if keep is not None:', 'if False:', InUse, 'test_a_keep_file_keeps_it_and_gives_the_reason'),
    'lock file': ('guard', 'if matches(name, LOCK_NAMES):', 'if False:', InUse, 'test_a_lock_file_is_never_deleted'),
    'directly in a configured root': ('guard', 'if os.path.dirname(path) not in cfg.roots()', 'if False',
                                      PathSafety, 'test_only_what_is_directly_in_a_configured_root'),
    'a symbolic link': ('guard', 'if stat.S_ISLNK(st.st_mode):', 'if False:', PathSafety, 'test_a_symbolic_link_is_never_followed'),
    'a symbolic link on the path': ('guard', 'if os.path.realpath(root) != root:', 'if False:',
                                    PathSafety, 'test_a_symbolic_link_is_never_followed'),
    'another filesystem': ('guard', 'if device(st) != dev:', 'if False:', PathSafety, 'test_another_filesystem'),
    'another filesystem inside': ('guard', 'if device(est) != dev:', 'if False:', PathSafety, 'test_another_filesystem'),
    'another owner': ('guard', 'if owner(st) != uid:', 'if False:', PathSafety, 'test_another_owner'),
    'another owner inside': ('guard', 'if owner(est) != uid:', 'if False:', PathSafety, 'test_another_owner'),
    'credential name': ('guard', 'if matches(name, CREDENTIAL_NAMES):', 'if False:', PathSafety, 'test_credential_looking_names'),
    'credential name inside': ('guard', 'if names and matches(entry.name, CREDENTIAL_NAMES):', 'if False:',
                               PathSafety, 'test_credential_looking_names'),
    'only plain files and folders': ('guard', 'if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):', 'if False:',
                                     PathSafety, 'test_only_plain_files_and_folders'),
    'an unreadable folder': ('guard', "return Refusal('could not read', f'{ex.filename or here}: {ex.strerror}')  # a folder", 'continue',
                             PathSafety, 'test_a_folder_it_cannot_read_means_keep'),
    'git: uncommitted or untracked': ('guard', "if not bare and git(where, 'status'", "if False and git(where, 'status'",
                                      GitFolders, 'test_uncommitted_and_untracked_changes'),
    'git: unpushed commits': ('guard', "if git(where, 'rev-list'", "if False and git(where, 'rev-list'", GitFolders, 'test_unpushed_commits'),
    'git: reflog commits': ('guard', "'--all', '--reflog', '--not'", "'--all', '--not'", GitFolders, 'test_a_commit_only_the_reflog_still_has'),
    'git: stash entries': ('guard', "if git_code(where, 'rev-parse', '-q', '--verify', 'refs/stash', bare=bare) == 0:", 'if False:',
                           GitFolders, 'test_stash_entries'),
    'git: worktrees': ('guard', 'if len(worktrees) > 1:', 'if False:', GitFolders, 'test_a_clone_other_worktrees_hang_off'),
    'git: no answer': ('guard', "return Refusal('could not read', f'git in {shown}: {ex}')", 'return None',
                       GitFolders, 'test_git_that_cannot_answer_means_keep'),
    'git: a folder inside is looked at': ('guard', 'gits.append((here, False))', 'pass', GitFolders, 'test_unpushed_commits'),
}


class RedCheck(unittest.TestCase):
    def test_each_refusal_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
