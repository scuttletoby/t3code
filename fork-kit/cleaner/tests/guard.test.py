"""The cleaner's hard refusals (lib/cleaner/guard.py and the readers in facts.py): what is never deleted, whatever
a rule or the config says. Each test asks the guard about one path in a temporary folder. RedCheck breaks each
refusal in a copy of the code and shows its test then fails.

  python3 -I -B fork-kit/cleaner/tests/guard.test.py
"""
import fcntl, os, subprocess, sys, unittest
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
        self.allowed(self.folder('home', '.t3', 'caches'))

    def test_the_agent_homes_are_kept_whole(self):
        # Codex keeps more than sessions: its thread database, archived sessions, logs, settings and login.
        for inside in ('.codex/state_5.sqlite', '.codex/archived_sessions', '.codex/log/codex-tui.log', '.codex/auth.json',
                       '.codex-second/history.jsonl', '.claude/todos', '.claude-second/settings.json', '.claude/cache'):
            with self.subTest(must_keep=inside):
                self.refused(self.file(self.path('home', *inside.split('/'))), 'must keep')

    def test_a_must_keep_path_written_through_a_link(self):
        real = self.folder('data', 'realdata', 'ledger')
        os.symlink(self.path('data', 'realdata'), self.path('data', 'linked'))
        more = [self.path('data', 'linked', 'ledger')]
        self.allowed(real)
        self.refused(real, 'must keep', mustKeep=more)                          # the folder the link leads to
        self.refused(self.path('data', 'realdata'), 'must keep', mustKeep=more)

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

    def test_t3_development_data_and_other_agent_tools_are_kept(self):
        for inside in ('.t3/dev/state.sqlite', '.t3-second/dev/logs/server.log', '.gemini/tmp/chats/session.json',
                       '.config/opencode/opencode.json', '.local/share/opencode/storage/session/1.json',
                       '.aider.tags.cache.v4/cache.db', '.cursor/chats/1.json', '.continue/sessions/1.json', '.qwen/settings.json',
                       '.local/share/goose/sessions/1.jsonl', '.copilot/history.json'):
            with self.subTest(must_keep=inside):
                self.refused(self.file(self.path('home', *inside.split('/'))), 'must keep')
        self.allowed(self.folder('home', '.t3', 'caches'))
        self.allowed(self.folder('home', '.config', 'other-tool'))


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

    def test_a_path_with_spaces_inside_command_text(self):
        spaced = self.folder('tmp', 'my dir')
        for text in (f'cd "{spaced}" && ./run', f"cd '{spaced}/sub'; ./run", f'cd {spaced.replace(" ", "\\ ")} && make',
                     f'tar -C "{spaced}" -c .', f'X={spaced}/out make'):
            with self.subTest(text=text):
                self.refused(spaced, 'in use', facts=self.proc({200: {'cmdline': ['sh', '-c', text]}}))
        for text in (f'cd "{spaced}-2" && ./run', f'cd "{spaced}x"', f'cd "/elsewhere{spaced}"'):
            with self.subTest(neighbour=text):
                self.allowed(spaced, facts=self.proc({200: {'cmdline': ['sh', '-c', text]}}))

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

    def test_a_held_lock_on_a_file_keeps_it(self):
        f = self.file(self.path('tmp', 'cache.db'))
        ino = os.stat(f).st_ino
        self.refused(f, 'in use', 'lock', facts=self.proc({}, locks=f'1: POSIX  ADVISORY  WRITE 200 08:01:{ino} 0 EOF\n'))
        self.allowed(f, facts=self.proc({}, locks=f'1: POSIX  ADVISORY  WRITE 200 08:01:{ino + 100000} 0 EOF\n'))

    def test_an_open_file_that_was_deleted_keeps_the_file_it_names(self):
        f = self.file(self.path('tmp', 'cache.db'))
        self.refused(f, 'in use', facts=self.proc({200: {'fd': [f + ' (deleted)']}}))

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

    def test_a_keep_file_deep_inside_keeps_the_whole_entry(self):
        d = self.folder('tmp', 'job-1')
        self.file(os.path.join(d, 'results', 'run-3', 'KEEP'), 'the numbers for the report\n')
        self.refused(d, 'KEEP file', 'the numbers for the report (results/run-3/KEEP)')
        self.folder('tmp', 'job-2')
        os.symlink('/nowhere', self.path('tmp', 'job-2', 'KEEP'))
        self.refused(self.path('tmp', 'job-2'), 'KEEP file', 'no reason given')         # a link named KEEP counts too

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

    def test_a_device_inside(self):
        d = self.folder('tmp', 'job-1')
        ino = os.lstat(os.path.join(d, 'data.bin')).st_ino
        with mock.patch.object(self.c.guard, 'is_device', lambda st: st.st_ino == ino):
            self.refused(d, 'not a file or folder', 'data.bin is a device')

    def test_a_bind_mount_inside(self):
        """A bind mount of a folder on the same filesystem has the same device number: the mount list shows it."""
        d = self.folder('tmp', 'job-1')
        neighbour = self.folder('tmp', 'job-10')
        for point in (os.path.join(d, 'mnt'), os.path.join(d, 'a', 'b'), d):
            with self.subTest(point=point):
                self.mount_points = {'/', self.tmp, point}
                self.refused(d, 'another filesystem', 'mount point')
                self.allowed(neighbour)
        self.mount_points = {'/', self.tmp, os.path.dirname(self.tmp)}
        self.allowed(d)

        def unreadable():
            raise self.c.facts.Unreadable('/proc/self/mountinfo: Permission denied')
        self.refused(d, 'could not read', 'mount list', facts={'mounts': unreadable})

    def test_the_mount_list_is_read_with_its_escapes(self):
        info = self.file(self.path('mountinfo'), ''.join(line + '\n' for line in (
            '22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw',
            r'90 22 8:1 /home/u/src /tmp/job\0401/mnt rw,relatime shared:1 - ext4 /dev/sda1 rw',
            r'91 22 0:50 / /tmp/tab\011and\134slash rw - tmpfs tmpfs rw',
            '92 22 0:51 / /run/user/1000 rw,nosuid master:3 propagate_from:2 - tmpfs tmpfs rw')))
        self.assertEqual(self.c.facts.read_mounts(info, proc=None).points,
                         {'/', '/tmp/job 1/mnt', '/tmp/tab\tand\\slash', '/run/user/1000'})
        self.assertIn('/', self.c.facts.read_mounts().points)                    # the real list
        for text in ('22 1 8:1 / / rw\n', '90 22 8:1 / /tmp/x rw - ext4 /dev/sda1 rw\n'):
            with self.subTest(text=text), self.assertRaises(self.c.facts.Unreadable):
                self.c.facts.read_mounts(self.file(self.path('mountinfo'), text))
        with self.assertRaises(self.c.facts.Unreadable):
            self.c.facts.read_mounts(self.path('no-such-mountinfo'))

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

    def test_the_source_of_a_bind_mount_is_kept(self):
        """A folder a container or a sandbox shows through a bind mount: the mount is in another mount namespace,
        and only its source (device and folder of that filesystem) says it is this one."""
        d = self.folder('tmp', 'job-1')
        neighbour = self.folder('tmp', 'job-10')
        st = os.lstat(d)
        dev = f'{os.major(st.st_dev)}:{os.minor(st.st_dev)}'
        self.mount_lines = [('/', dev, '/')]
        for source in (d, os.path.join(d, 'deep', 'er')):
            with self.subTest(source=source):
                self.bind_sources = {(dev, source)}
                self.refused(d, 'in use', 'bind mount')
                self.allowed(neighbour)
        self.bind_sources = {('0:999', d)}                            # another filesystem's folder of that name
        self.allowed(d)
        # The filesystem is mounted at self.tmp, showing its folder /export: the source names it from there.
        self.mount_lines = [('/', '0:998', '/'), (self.tmp, dev, '/export')]
        self.bind_sources = {(dev, '/export/job-1/data')}
        self.refused(d, 'in use', '/export/job-1/data')
        self.allowed(neighbour)

    def test_bind_mount_sources_are_read_from_every_mount_namespace_it_can_read(self):
        own = '22 1 8:1 / / rw shared:1 - ext4 /dev/sda1 rw\n'
        info = self.file(self.path('mountinfo'), own + '30 22 8:1 /srv/own /mnt/own rw - ext4 /dev/sda1 rw\n')
        proc = self.mkdir('fakeproc')
        for pid, space, line in (('10', 'mnt:[1]', '40 22 8:1 /home/u/tmp/job-1 /work rw - ext4 /dev/sda1 rw'),
                                 ('11', 'mnt:[1]', '41 22 8:1 /same/space /x rw - ext4 /dev/sda1 rw'),
                                 ('12', 'mnt:[2]', '42 22 0:5 /var/lib/box /data rw - ext4 /dev/sdb1 rw'),
                                 ('13', None, '43 22 8:1 /hidden /y rw - ext4 /dev/sda1 rw')):
            os.makedirs(os.path.join(proc, pid, 'ns'))
            if space:
                os.symlink(space, os.path.join(proc, pid, 'ns', 'mnt'))
            self.file(os.path.join(proc, pid, 'mountinfo'), own + line + '\n')
        self.file(os.path.join(proc, 'self'), '')
        got = self.c.facts.read_mounts(info, proc)
        self.assertEqual(got.points, {'/', '/mnt/own'})
        self.assertEqual(got.sources, {('8:1', '/srv/own'), ('8:1', '/home/u/tmp/job-1'), ('0:5', '/var/lib/box')})
        self.assertEqual(got.lines, [('/', '8:1', '/'), ('/mnt/own', '8:1', '/srv/own')])


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

    def test_a_commit_only_a_stale_tracking_ref_names(self):
        """The branch was deleted on GitHub after the push: origin/side still names the commit, GitHub does not."""
        d, repo = self.job()
        self.git(repo, 'checkout', '-q', '-b', 'side')
        lost = self.commit(repo)
        self.push(repo)
        self.git(repo, 'checkout', '-q', 'main')
        self.git(repo, 'branch', '-q', '-D', 'side')
        self.git(self.origin(), 'branch', '-q', '-D', 'side')
        self.assertEqual(self.git(repo, 'rev-parse', 'origin/side'), lost)
        self.refused(d, 'git: unpushed commits', lost[:10])

    def test_a_remote_on_this_machine_does_not_count(self):
        src = self.mkdir('tmp', 'job-1', 'src')
        self.git(src, 'init', '-q')
        self.commit(src)
        copy = os.path.join(self.mkdir('tmp', 'job-2'), 'copy')
        self.git(self.dir, 'clone', '-q', src, copy)
        self.refused(self.path('tmp', 'job-2'), 'git: unpushed commits')       # src may be the next to go
        self.git(copy, 'remote', 'set-url', 'origin', 'file://' + src)
        self.refused(self.path('tmp', 'job-2'), 'git: unpushed commits')

    def test_local_tags(self):
        for how in (('-a', '-m', 'release notes only here'), ()):
            with self.subTest(annotated=bool(how)):
                d, repo = self.job(f'job-{len(how)}')
                self.git(repo, 'tag', *how, f'v-local-{len(how)}')
                self.refused(d, 'git: tags not pushed', f'v-local-{len(how)}')
                self.git(repo, 'push', '-q', 'origin', f'v-local-{len(how)}')
                self.allowed(d)
        d, repo = self.job('job-moved')
        self.git(repo, 'tag', 'moved')
        self.git(repo, 'push', '-q', 'origin', 'moved')
        self.commit(repo)
        self.push(repo)
        self.git(repo, 'tag', '-f', 'moved')                                    # the same name, another commit
        self.refused(d, 'git: tags not pushed', 'moved')

    def test_a_git_link_is_never_passed_over(self):
        d, repo = self.job()
        work = self.mkdir('tmp', 'job-2', 'w')
        os.rename(os.path.join(repo, '.git'), os.path.join(self.mkdir('gitdirs'), 'repo.git'))
        os.symlink(self.path('gitdirs', 'repo.git'), os.path.join(work, '.git'))
        self.git(work, 'checkout', '-q', '-f', 'main')
        self.file(os.path.join(work, 'README.md'), 'changed\n')
        self.refused(self.path('tmp', 'job-2'), 'could not read', '.git is a symbolic link')

    def test_a_remote_it_cannot_ask_means_keep(self):
        d, repo = self.job()
        self.git(repo, 'remote', 'set-url', 'origin', 'https://github.com/acme/gone.git')
        self.refused(d, 'could not read', 'git')

    def test_git_that_cannot_answer_means_keep(self):
        d, repo = self.job()
        self.file(os.path.join(repo, '.git', 'HEAD'), 'not a ref\n')
        self.refused(d, 'could not read', 'git')
        d, repo = self.job('job-2')
        self.allowed(d)
        with mock.patch.object(self.c.facts, 'GIT', self.path('no-such-git')):
            self.refused(d, 'could not read', 'git')

    def test_strange_git_answers_mean_keep(self):
        d, repo = self.job()
        self.allowed(d)
        real = self.c.facts.git
        for what, change in {'ls-remote': lambda out: out + 'not an object id\trefs/heads/x\n',
                             'cat-file': lambda out: ''.join(out.splitlines(True)[1:])}.items():
            def answer(cwd, *args, **kw):
                out = real(cwd, *args, **kw)
                return change(out) if args[0] == what else out
            with self.subTest(what=what), mock.patch.object(self.c.facts, 'git', answer):
                self.refused(d, 'could not read', what)

    def test_a_remote_is_judged_by_the_urls_git_really_uses(self):
        """url.<base>.insteadOf (in the user's global git config) can turn a GitHub URL into a folder on this
        machine, and a remote can have more URLs and push URLs. Each must be on another machine."""
        src = self.mkdir('tmp', 'job-1', 'src')
        self.git(src, 'init', '-q')
        self.commit(src)
        copy = os.path.join(self.mkdir('tmp', 'job-2'), 'copy')
        self.git(self.dir, 'clone', '-q', src, copy)
        job = self.path('tmp', 'job-2')
        self.git(copy, 'remote', 'set-url', 'origin', f'ssh://github.invalid{src}')     # through the ssh stub
        self.allowed(job)
        with open(self.path('gitconfig'), 'a') as f:
            f.write(f'[url "{src}"]\n\tinsteadOf = https://example.org/src.git\n')
        self.git(copy, 'remote', 'set-url', 'origin', 'https://example.org/src.git')
        self.refused(job, 'git: unpushed commits')
        self.git(copy, 'remote', 'set-url', 'origin', f'ssh://github.invalid{src}')
        self.git(copy, 'remote', 'set-url', '--add', 'origin', src)                      # a second URL
        self.refused(job, 'git: unpushed commits')
        self.git(copy, 'remote', 'set-url', '--delete', 'origin', '^/')
        self.allowed(job)
        self.git(copy, 'remote', 'set-url', '--push', 'origin', 'file://' + src)        # a push URL
        self.refused(job, 'git: unpushed commits')

    def test_what_counts_as_another_machine(self):
        host = __import__('socket').gethostname()
        for url, far in (('https://github.com/acme/widget.git', True), ('git@github.com:acme/widget.git', True),
                         ('ssh://git@github.com/acme/widget', True), ('ssh://[2001:db8::1]/x.git', True),
                         ('/srv/git/x.git', False), ('./x', False), ('file:///srv/x.git', False),
                         ('FILE://host/x', False), ('localhost:/srv/x.git', False), ('ssh://localhost/x', False),
                         ('git@127.0.0.1:x.git', False), ('ssh://127.0.0.2:22/x', False), ('[::1]:x.git', False),
                         ('ssh://[::1]/x', False), ('https://0.0.0.0/x', False), ('ssh://box.localhost/x', False),
                         (f'ssh://{host}/x', False), (f'{host}:x.git', False), ('ext::ssh github.com %S x', False),
                         ('no_push', False)):
            with self.subTest(url=url):
                self.assertEqual(self.c.facts.elsewhere(url), far)

    def test_a_deinitialised_submodule_keeps_its_history(self):
        """`git submodule deinit` empties the submodule's folder; its history, local commits too, stays in
        .git/modules/<name>, and goes with the folder that holds it."""
        d, repo = self.job()
        self.origin('acme/sub')
        self.git(repo, 'submodule', 'add', '-q', 'https://github.com/acme/sub.git', 'sub')
        self.git(repo, 'commit', '-q', '-m', 'add sub')
        self.push(repo)
        self.allowed(d)
        lost = self.commit(os.path.join(repo, 'sub'), 'only-here.txt')
        self.git(repo, 'submodule', 'deinit', '-q', '-f', 'sub')
        self.assertEqual(os.listdir(os.path.join(repo, 'sub')), [])
        self.assertEqual(self.git(repo, 'status', '--porcelain'), '')
        self.refused(d, 'git: unpushed commits', lost[:10])
        self.refused(d, 'git: unpushed commits', 'modules')
        self.git(os.path.join(repo, '.git', 'modules', 'sub'), 'push', '-q', 'origin', f'{lost}:refs/heads/kept')
        self.allowed(d)
        self.git(os.path.join(repo, '.git', 'modules', 'sub'), 'tag', 'v-sub')
        self.refused(d, 'git: tags not pushed', 'v-sub')

    def test_a_partial_clone_is_never_made_to_fetch(self):
        """In a partial clone, asking about an object it does not have would fetch it ("lazy fetch"). The cleaner
        only reads: a missing object is a missing object."""
        bare = self.origin()
        self.git(bare, 'config', 'uploadpack.allowFilter', 'true')
        d = self.mkdir('tmp', 'job-1')
        repo = os.path.join(d, 'repo')
        self.git(self.dir, 'clone', '-q', '--filter=blob:none', f'https://github.com/{fx.REPO}.git', repo)
        later = self.clone('later', into=self.mkdir('elsewhere'))
        newer = self.commit(later, 'newer.txt')
        self.git(later, 'push', '-q', 'origin', 'HEAD:refs/heads/other')    # GitHub has a commit the clone has not
        self.assertEqual(self.c.facts.present(repo, [newer]), set())
        self.allowed(d)
        missing = subprocess.run(['git', '-c', 'protocol.allow=never', 'cat-file', '-e', newer], cwd=repo,
                                 capture_output=True)
        self.assertNotEqual(missing.returncode, 0, 'the cleaner fetched the commit')


MUTATIONS = {
    'must keep: the built-in paths': ('guard', 'kept = must_keep_match(cfg.must_keep, path)', 'kept = None',
                                      MustKeep, 'test_transcripts_sessions_and_t3_data_are_never_deleted'),
    'must keep: the agent homes whole': ('config', "'~/.claude*', '~/.codex*',\n", "'~/.claude*/projects', '~/.codex*/sessions',\n",
                                         MustKeep, 'test_the_agent_homes_are_kept_whole'),
    'must keep: T3 development data': ('config', "T3_DATA = ('~/.t3*/userdata', '~/.t3*/dev')", "T3_DATA = ('~/.t3*/userdata',)", MustKeep, 'test_t3_development_data_and_other_agent_tools_are_kept'),
    'must keep: other agent tools': ('config', "'~/.gemini', ", '', MustKeep, 'test_t3_development_data_and_other_agent_tools_are_kept'),
    'must keep: through a link': ('config', 'for form in (self._expand(text), os.path.realpath(self._expand(text))):',
                                  'for form in (self._expand(text),):', MustKeep, 'test_a_must_keep_path_written_through_a_link'),
    'must keep: a name on the path': ('guard', 'if kept_name(cfg, part)', 'if False',
                                      MustKeep, 'test_a_log_archive_and_repair_backups_are_never_deleted_wherever_they_are'),
    'must keep: a name inside': ('guard', 'if kept_name(cfg, entry.name)', 'if False',
                                 MustKeep, 'test_a_log_archive_and_repair_backups_are_never_deleted_wherever_they_are'),
    'must keep: the config adds': ('config', "MUST_KEEP + tuple(values['mustKeep'])", 'MUST_KEEP',
                                   MustKeep, 'test_the_config_adds_to_the_list'),
    'must keep: the config cannot remove': ('config', "MUST_KEEP + tuple(values['mustKeep'])", "tuple(values['mustKeep'])",
                                            MustKeep, 'test_the_config_cannot_take_anything_off_the_list'),
    'in use: a process': ('guard', 'if processes.uses(path):', 'if False:', InUse, 'test_a_process_using_it_keeps_it'),
    'in use: a path with spaces in command text': ('facts', "return any(named.search(text.replace('\\\\ ', ' ')) for text in self.texts)",
                                                   'return False', InUse, 'test_a_path_with_spaces_inside_command_text'),
    'in use: command text edges': ('facts', "named = re.compile(f'(?:^|(?<=[{EDGE}]))' + re.escape(path) + f'(?=$|[/{EDGE}])')",
                                   'named = re.compile(re.escape(path))', InUse, 'test_a_path_with_spaces_inside_command_text'),
    'in use: a deleted open file': ('facts', 'path = path[:-len(DELETED)]', 'pass', InUse, 'test_an_open_file_that_was_deleted_keeps_the_file_it_names'),
    'in use: the real process list': ('facts', 'paths.add(target)           # its folder, root or program', 'pass',
                                      InUse, 'test_a_process_using_it_keeps_it'),
    'in use: open files': ('facts', 'paths.add(target)           # an open file', 'pass',
                           InUse, 'test_the_real_process_list_sees_an_open_file_and_a_held_lock'),
    'in use: a socket': ('facts', 'paths.add(fields[7])', 'pass', InUse, 'test_a_socket_a_process_listens_on_keeps_it'),
    'in use: a held lock': ('guard', 'if held:  # locks', 'if False:', InUse, 'test_a_held_lock_inside_keeps_it'),
    'in use: a held lock on a file': ('guard', 'inodes = [(st.st_ino, name)]', 'inodes = []', InUse, 'test_a_held_lock_on_a_file_keeps_it'),
    'in use: no process list': ('guard', "return Refusal('could not read', f'the process list ({ex})')", 'processes = EMPTY',
                                InUse, 'test_no_process_list_means_keep'),
    'in use: our own process must be in the list': ('facts', 'if me not in seen:', 'if False:', InUse, 'test_no_process_list_means_keep'),
    'in use: a systemd unit': ('guard', 'if path in units:', 'if False:', InUse, 'test_a_systemd_unit_that_names_it_keeps_it'),
    'in use: an unreadable unit': ('guard', "return Refusal('could not read', f'a systemd unit ({ex})')", 'units = ()',
                                   InUse, 'test_an_unreadable_unit_means_keep'),
    'KEEP file': ('guard', 'if plain and entry.name == KEEP_FILE and not stat.S_ISDIR(est.st_mode):', 'if False:',
                  InUse, 'test_a_keep_file_keeps_it_and_gives_the_reason'),
    'KEEP file deep inside': ('guard', 'if plain and entry.name == KEEP_FILE', 'if plain and here == path and entry.name == KEEP_FILE',
                              InUse, 'test_a_keep_file_deep_inside_keeps_the_whole_entry'),
    'lock file': ('guard', 'if matches(name, LOCK_NAMES):', 'if False:', InUse, 'test_a_lock_file_is_never_deleted'),
    'directly in a configured root': ('guard', 'if not (root in cfg.roots() or cfg.session_parent(root))', 'if False',
                                      PathSafety, 'test_only_what_is_directly_in_a_configured_root'),
    'a symbolic link': ('guard', 'if stat.S_ISLNK(st.st_mode):', 'if False:', PathSafety, 'test_a_symbolic_link_is_never_followed'),
    'a symbolic link on the path': ('guard', 'if os.path.realpath(root) != root:', 'if False:',
                                    PathSafety, 'test_a_symbolic_link_is_never_followed'),
    'another filesystem': ('guard', 'if device(st) != dev:', 'if False:', PathSafety, 'test_another_filesystem'),
    'another filesystem inside': ('guard', 'if device(est) != dev:', 'if False:', PathSafety, 'test_another_filesystem'),
    'a bind mount': ('guard', 'if inside:', 'if False:', PathSafety, 'test_a_bind_mount_inside'),
    'a bind mount: no mount list': ('guard', "return Refusal('could not read', f'the mount list ({ex})')", 'mounts = ()',
                                    PathSafety, 'test_a_bind_mount_inside'),
    'the mount list: escapes': ('facts', "return re.sub(r'\\\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), field)", 'return field',
                                PathSafety, 'test_the_mount_list_is_read_with_its_escapes'),
    'a bind mount two levels down': ('guard', "m.startswith(path + '/')", 'os.path.dirname(m) == path',
                                     PathSafety, 'test_a_bind_mount_inside'),
    'a bind mount source': ('guard', 'if source is not None:  # bind source', 'if False:',
                            PathSafety, 'test_the_source_of_a_bind_mount_is_kept'),
    'a bind mount source: where on its filesystem': ('facts', 'return os.path.normpath(os.path.join(best[1], rest))', 'return path',
                                                     PathSafety, 'test_the_source_of_a_bind_mount_is_kept'),
    'a bind mount source: other namespaces': ('facts', "sources.update((device, root) for _, device, root in other if root != '/')",
                                              'pass', PathSafety, 'test_bind_mount_sources_are_read_from_every_mount_namespace_it_can_read'),
    'a bind mount source: one read per namespace': ('facts', 'if space in seen:', 'if False:',
                                                    PathSafety, 'test_bind_mount_sources_are_read_from_every_mount_namespace_it_can_read'),
    'the mount list: a strange line': ("facts", "if len(fields) < 10 or '-' not in fields[6:] or not re.match(r'^\\d+:\\d+$', fields[2]):", 'if False:',
                                       PathSafety, 'test_the_mount_list_is_read_with_its_escapes'),
    'a device inside': ('guard', "return Refusal('not a file or folder', f'{shown} is a device')", 'pass', PathSafety, 'test_a_device_inside'),
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
    'git: unpushed commits': ('guard', 'if alone:  # tmp', 'if False:', GitFolders, 'test_unpushed_commits'),
    'git: reflog commits': ('guard', "lonely(where, ['--all', '--reflog'], sorted(", "lonely(where, ['--all'], sorted(",
                            GitFolders, 'test_a_commit_only_the_reflog_still_has'),
    'git: a live answer, not tracking refs': ('guard', "lonely(where, ['--all', '--reflog'], sorted(present(where, has, bare)), bare)",
                                              "lonely(where, ['--all', '--reflog'], ['--remotes'], bare)",
                                              GitFolders, 'test_a_commit_only_a_stale_tracking_ref_names'),
    'git: a remote on this machine': ('guard', 'if not all(elsewhere(url) for url in remote_urls(where, name, bare)):', 'if False:',
                                      GitFolders, 'test_a_remote_on_this_machine_does_not_count'),
    'git: the URLs git really uses': ('guard', 'if not all(elsewhere(url) for url in remote_urls(where, name, bare)):',
                                      "if not elsewhere(git(where, 'config', f'remote.{name}.url', bare=bare).strip()):",
                                      GitFolders, 'test_a_remote_is_judged_by_the_urls_git_really_uses'),
    'git: every URL and push URL': ('facts', "for push in ([], ['--push']):", 'for push in ([],):',
                                    GitFolders, 'test_a_remote_is_judged_by_the_urls_git_really_uses'),
    'git: a file:// remote': ('facts', "return found.group(1).lower() != 'file' and not this_machine(found.group(2))", 'return True',
                              GitFolders, 'test_a_remote_on_this_machine_does_not_count'),
    'git: a host that is this machine': ('facts', "return bool(found) and not this_machine(found.group(1))", 'return bool(found)',
                                         GitFolders, 'test_what_counts_as_another_machine'),
    'git: loopback addresses': ('facts', 'return address.is_loopback or address.is_unspecified', 'return False',
                                GitFolders, 'test_what_counts_as_another_machine'),
    'git: its own host name': ('facts', "return host in (name, name.split('.')[0])", 'return False',
                               GitFolders, 'test_what_counts_as_another_machine'),
    'git: a remote helper': ('facts', "if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*::', url):", 'if False:',
                             GitFolders, 'test_what_counts_as_another_machine'),
    'git: deinitialised submodules (tmp)': ('guard', 'refusal = modules_refusal(path, where if bare else os.path.join(where, \'.git\'))',
                                            'refusal = None', GitFolders, 'test_a_deinitialised_submodule_keeps_its_history'),
    'git: no lazy fetch': ('facts', "offline = args[0] != 'ls-remote'", 'offline = False',
                           GitFolders, 'test_a_partial_clone_is_never_made_to_fetch'),
    'git: local tags': ('guard', 'if missing:', 'if False:', GitFolders, 'test_local_tags'),
    'git: a moved tag': ('guard', 'if oid not in tags.get(ref, ())', 'if ref not in tags', GitFolders, 'test_local_tags'),
    'git: a .git link': ('guard', "return Refusal('could not read', f'{shown} is a symbolic link or not a folder: its git folder is not judged')",
                         'pass', GitFolders, 'test_a_git_link_is_never_passed_over'),
    'git: ls-remote line check': ('facts', 'if not OID.match(oid) or not tab or not ref:', 'if False:',
                                  GitFolders, 'test_strange_git_answers_mean_keep'),
    'git: cat-file answered for each': ('facts', 'if len(out) != len(oids):', 'if False:', GitFolders, 'test_strange_git_answers_mean_keep'),
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
