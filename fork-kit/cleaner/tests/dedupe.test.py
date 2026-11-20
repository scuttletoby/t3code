"""The `dedupe` rule (lib/cleaner/dedupe.py, #486) and its keys (config.py). Every file lives in the test's own
temporary folder (one test also uses a folder of its own in /dev/shm, another filesystem). The state cases: a second
run (nothing to do), a run killed between the link and the rename (the duplicate stays; the next run finishes), a
part left whose other names are gone (kept), a file that changes while it is read or compared (kept), a file opened
or changed between the plan and the link, a machine file without the keys and a folder that is not there.
RedCheck breaks each rule in a copy of the code and shows its test then fails.

  python3 -I -B fork-kit/cleaner/tests/dedupe.test.py
"""
import errno, os, shutil, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cleaner_fixture as fx  # noqa: E402

import machine_file  # noqa: E402

DAY = fx.DAY


def body(seed='a', n=200 * 1024):
    """`n` bytes of one pattern: two bodies with the same seed are the same bytes."""
    line = f'{seed} the same bytes in more than one place\n'.encode()
    return (line * (n // len(line) + 1))[:n]


class Case(fx.Case):
    c = fx.load()

    def setUp(self):
        super().setUp()
        self.dd = self.mkdir('dd')

    def dd_cfg(self, days=30, folders=None, **section):
        return self.cfg(dedupe={'folders': folders or [{'path': self.dd, 'days': days}]}, **section)

    def put(self, *parts, data=None, days=40, mode=0o644):
        path = os.path.join(self.dd, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(body() if data is None else data)
        os.chmod(path, mode)
        if days is not None:
            self.age(path, days)
        return path

    def ino(self, path):
        return os.stat(path).st_ino

    def read(self, path):
        with open(path, 'rb') as f:
            return f.read()

    def files(self, top=None):
        top = top or self.dd
        return sorted(os.path.relpath(os.path.join(root, n), top) for root, _, names in os.walk(top) for n in names)

    def items(self, plan, kind='dedupe'):
        return sorted(item.path for item in plan.items if item.kind == kind)


class Dedupe(Case):
    def test_copies_become_hard_links_of_one_file(self):
        a, b, c = self.put('a.bin', days=50), self.put('sub', 'b.bin'), self.put('sub', 'deep', 'c.bin', days=45)
        before = os.stat(a)
        code, lines = self.run_cleaner(self.dd_cfg())
        self.assertEqual(code, 0, lines)
        self.assertEqual({self.ino(p) for p in (a, b, c)}, {before.st_ino})   # the oldest is the one kept
        self.assertEqual(os.stat(a).st_nlink, 3)
        for path in (a, b, c):
            self.assertEqual(self.read(path), body())
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(a).st_mtime_ns, before.st_mtime_ns)
        done = [line for line in lines if line.startswith('linked ')]
        self.assertEqual(len(done), 2, lines)
        self.assertIn(f'dedupe: the same bytes as {a}, not changed for 40 days', ' '.join(done))
        self.assertIn('now a hard link of ' + a + ', compared byte for byte', done[0])
        self.assertIn('total: deleted 0 (0 B), linked 2 (400.0 KB freed)', lines[-1])
        cfg = self.dd_cfg()
        with open(cfg.log_file) as f:
            log = f.read()
        self.assertIn(' linking ' + b, log)
        self.assertNotIn('half deleted', log)
        self.assertFalse(os.path.exists(cfg.journal_file) and os.path.getsize(cfg.journal_file) > 2)
        self.assertEqual(self.files(), ['a.bin', 'sub/b.bin', 'sub/deep/c.bin'])

    def test_the_plan_changes_nothing_and_shows_what_it_would_free(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        before = (self.ino(a), self.ino(b))
        code, lines = self.run_cleaner(self.dd_cfg(), really=False)
        self.assertEqual(code, 0, lines)
        self.assertEqual((self.ino(a), self.ino(b)), before)
        self.assertTrue(any(line.startswith(f'would link {b} (200.0 KB, dedupe: the same bytes as {a}') for line in lines), lines)
        self.assertIn('  dedupe: would link 1 file (200.0 KB)', lines)
        self.assertIn('total: would delete 0 (0 B), would link 1 (200.0 KB), kept 0', lines[-1])
        self.assertFalse(os.path.exists(self.dd_cfg().log_file))

    def test_a_second_run_has_nothing_to_do(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        self.run_cleaner(self.dd_cfg())
        self.assertEqual(self.ino(a), self.ino(b))
        plan = self.plan(self.dd_cfg())
        self.assertEqual((plan.items, plan.kept), ([], []))
        code, lines = self.run_cleaner(self.dd_cfg())
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[-1], 'total: deleted 0 (0 B), kept 0, failed 0')

    def test_off_without_folders_and_nothing_when_the_folder_is_missing_or_empty(self):
        self.put('a.bin'), self.put('b.bin')
        for cfg in (self.cfg(), self.cfg(dedupe={}), self.cfg(dedupe={'folders': []})):
            self.assertEqual(cfg.dedupe_folders, ())
            plan = self.plan(cfg)
            self.assertEqual((plan.items, plan.kept), ([], []))
        empty = self.mkdir('empty')
        for path in (self.path('missing'), empty):
            plan = self.plan(self.dd_cfg(folders=[{'path': path, 'days': 30}]))
            self.assertEqual((plan.items, plan.kept), ([], []), path)
        self.assertNotEqual(self.ino(os.path.join(self.dd, 'a.bin')), self.ino(os.path.join(self.dd, 'b.bin')))

    def test_files_changed_lately_small_or_with_other_bytes_are_not_taken(self):
        old = self.put('old.bin', days=50)
        new = self.put('new.bin', days=5)
        other = self.put('other.bin', data=body()[:-1] + b'!')         # same size, one byte differs
        small = self.put('small.bin', data=b'x' * 1000)
        self.put('small2.bin', data=b'x' * 1000)
        plan = self.plan(self.dd_cfg())
        self.assertEqual((plan.items, plan.kept), ([], []))
        self.run_cleaner(self.dd_cfg())
        self.assertEqual(len({self.ino(p) for p in (old, new, other, small)}), 4)

    def test_the_days_of_the_folder_count(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin', days=20)
        self.assertEqual(self.items(self.plan(self.dd_cfg(days=30))), [])
        self.assertEqual(self.items(self.plan(self.dd_cfg(days=10))), [b])


class Compared(Case):
    """Content is compared byte for byte before the link: the sha256 of the plan only finds the candidates."""

    def test_files_a_hash_calls_the_same_are_compared_byte_for_byte(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin', data=body('b'))
        real = self.c.dedupe._digest
        with mock.patch.object(self.c.dedupe, '_digest', lambda path, st: ('0' * 64, real(path, st)[1])):
            plan = self.plan(self.dd_cfg())
            self.assertEqual(self.items(plan), [b])     # what a hash collision would look like
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {b} ') and 'does not hold the same bytes' in line for line in lines), lines)
        self.assertNotEqual(self.ino(a), self.ino(b))
        self.assertEqual((self.read(a), self.read(b)), (body(), body('b')))
        self.assertEqual(self.files(), ['a.bin', 'b.bin'])

    def test_a_file_that_changes_while_it_is_read_is_kept(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        real = os.read

        def read(fd, n):
            data = real(fd, n)
            if os.readlink(f'/proc/self/fd/{fd}') == b and not os.path.exists(b + '.done'):
                open(b + '.done', 'w').close()
                with open(b, 'r+b') as f:
                    f.write(b'Z')       # same size, other bytes, written while the plan reads it
                self.age(b, 40)
            return data
        with mock.patch.object(self.c.dedupe.os, 'read', read):
            plan = self.plan(self.dd_cfg())
        self.assertKept(plan, b, 'changed', 'it changed while it was read')
        self.assertEqual(self.items(plan), [])

    def test_a_file_that_changes_while_it_is_compared_stays(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        real = self.c.dedupe._equal

        def then_changed(x, y, size):
            out = real(x, y, size)
            with open(b, 'r+b') as f:
                f.write(b'Z')
            return out
        with mock.patch.object(self.c.dedupe, '_equal', then_changed):
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {b} ') and 'changed while they were compared' in line for line in lines), lines)
        self.assertNotEqual(self.ino(a), self.ino(b))
        self.assertEqual(self.read(b), b'Z' + body()[1:])
        self.assertEqual(self.read(a), body())
        self.assertEqual(self.files(), ['a.bin', 'b.bin'])

    def test_a_file_changed_after_the_plan_is_refused(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        cfg = self.dd_cfg()
        facts = self.facts(cfg)
        plan = self.c.rules.plan(cfg, facts)
        self.assertEqual(self.items(plan), [b])
        with open(b, 'r+b') as f:
            f.write(b'Z')
        failed, lines = self.delete_all(cfg, facts, plan.items)
        self.assertEqual(failed, 0)
        self.assertTrue(lines[0].startswith(f'REFUSED {b} (dedupe: '), lines)
        self.assertEqual(self.read(b), b'Z' + body()[1:])
        self.assertNotEqual(self.ino(a), self.ino(b))

    def test_a_kept_file_replaced_after_the_plan_is_refused(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        cfg = self.dd_cfg()
        facts = self.facts(cfg)
        plan = self.c.rules.plan(cfg, facts)
        self.assertEqual(self.items(plan), [b])
        os.rename(a, self.path('a.old'))        # the old file stays open under another name: a new inode number
        self.put('a.bin', data=body('other'), days=50)
        failed, lines = self.delete_all(cfg, facts, plan.items)
        self.assertTrue(lines[0].startswith(f'REFUSED {b} (dedupe: not a candidate: the kept file {a} changed'), lines)
        self.assertEqual((self.read(a), self.read(b)), (body('other'), body()))


class Stopped(Case):
    """A run killed between the link and the rename leaves the duplicate as it was and one more name of the kept
    file; the next run removes that name and links again."""

    def kill_at_rename(self):
        real = os.rename

        def killed(src, dst, **kw):
            if src.endswith('.cleaner-link'):
                raise KeyboardInterrupt('killed')
            return real(src, dst, **kw)
        return mock.patch.object(self.c.dedupe.os, 'rename', killed)

    def test_killed_between_the_link_and_the_rename_the_next_run_finishes(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        dup = self.ino(b)
        with self.kill_at_rename(), self.assertRaises(KeyboardInterrupt):
            self.run_cleaner(self.dd_cfg())
        names = self.files()
        self.assertEqual(len(names), 3, names)
        part = os.path.join(self.dd, next(n for n in names if n.endswith('.cleaner-link')))
        self.assertRegex(os.path.basename(part), r'^\.b\.bin\.[0-9a-f]{8}\.cleaner-link$')
        self.assertEqual((self.ino(b), self.read(b)), (dup, body()))         # the duplicate as it was
        self.assertEqual(self.ino(part), self.ino(a))                       # one more name of the kept file
        plan = self.plan(self.dd_cfg())
        self.assertEqual(self.items(plan, 'link-part'), [part])
        self.assertEqual(self.items(plan), [b])
        code, lines = self.run_cleaner(self.dd_cfg())
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.files(), ['a.bin', 'b.bin'])
        self.assertEqual(self.ino(a), self.ino(b))
        self.assertEqual(os.stat(a).st_nlink, 2)
        self.assertEqual(self.read(b), body())
        plan = self.plan(self.dd_cfg())
        self.assertEqual((plan.items, plan.kept), ([], []))

    def test_a_part_that_is_the_last_name_of_its_content_stays(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        with self.kill_at_rename(), self.assertRaises(KeyboardInterrupt):
            self.run_cleaner(self.dd_cfg())
        part = os.path.join(self.dd, next(n for n in self.files() if n.endswith('.cleaner-link')))
        os.unlink(a)                    # the kept file is gone by hand: the part holds the only copy of its bytes
        plan = self.plan(self.dd_cfg())
        self.assertKept(plan, part, 'part without its file', 'the only copy')
        self.run_cleaner(self.dd_cfg())
        self.assertEqual(self.read(part), body())
        self.assertEqual(os.stat(part).st_nlink, 1)

    def test_a_part_that_lost_its_other_name_after_the_plan_stays(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        with self.kill_at_rename(), self.assertRaises(KeyboardInterrupt):
            self.run_cleaner(self.dd_cfg())
        part = os.path.join(self.dd, next(n for n in self.files() if n.endswith('.cleaner-link')))
        cfg = self.dd_cfg()
        facts = self.facts(cfg)
        items = [i for i in self.c.rules.plan(cfg, facts).items if i.kind == 'link-part']
        self.assertEqual([i.path for i in items], [part])
        os.unlink(a)
        with mock.patch.object(self.c.dedupe, 'recheck', lambda cfg, facts, item: None):  # the last check passed
            failed, lines = self.delete_all(cfg, facts, items)
        self.assertEqual(failed, 1, lines)
        self.assertIn('the last name of its content', lines[-1])
        self.assertEqual(self.read(part), body())

    def test_a_failed_rename_removes_the_part_and_keeps_the_duplicate(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        dup = self.ino(b)

        def refused(src, dst, **kw):
            raise PermissionError(errno.EACCES, 'Permission denied')
        with mock.patch.object(self.c.dedupe.os, 'rename', refused):
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {b} ') and 'the duplicate stays' in line for line in lines), lines)
        self.assertEqual(self.files(), ['a.bin', 'b.bin'])
        self.assertEqual((self.ino(b), os.stat(a).st_nlink), (dup, 1))


class InUse(Case):
    def test_files_in_use_stay(self):
        a = self.put('a.bin', days=50)
        opened, named, locked = self.put('opened.bin'), self.put('unit', 'named.bin'), self.put('locked.bin')
        self.busy.add(opened)
        self.unit_names.add(os.path.join(self.dd, 'unit'))
        self.locks.add(os.stat(locked).st_ino)
        plan = self.plan(self.dd_cfg())
        for path in (opened, named, locked):
            self.assertKept(plan, path, 'in use')
        self.assertEqual(self.items(plan), [])

    def test_a_kept_file_in_use_keeps_its_duplicates(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        self.busy.add(a)
        plan = self.plan(self.dd_cfg())
        self.assertKept(plan, a, 'in use')
        self.assertEqual(self.items(plan), [])

    def test_a_file_opened_while_it_is_compared_stays(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        for opened in (b, a):
            self.busy.clear()
            real = self.c.dedupe._compare

            def then_opened(*args):
                out = real(*args)
                self.busy.add(opened)
                return out
            with self.subTest(opened=opened), mock.patch.object(self.c.dedupe, '_compare', then_opened):
                code, lines = self.run_cleaner(self.dd_cfg())
                self.assertEqual(code, 1, lines)
                self.assertTrue(any(line.startswith(f'FAILED {b} ') and 'in use' in line for line in lines), lines)
                self.assertNotEqual(self.ino(a), self.ino(b))
                self.assertEqual(self.files(), ['a.bin', 'b.bin'])


class Refused(Case):
    def test_what_it_never_takes(self):
        self.put('a.bin', days=50)
        secret = self.put('api-token.bin')
        lock = self.put('db.lock')
        db = self.put('state.sqlite')
        beside = self.put('store.dat')
        self.put('store.dat-wal', data=b'')
        log = self.put('app.log')
        jsonl = self.put('session.jsonl')
        kept_name = self.put('repair-backups', 'copy.bin')
        keep = self.put('audit', 'copy.bin')
        self.file(os.path.join(self.dd, 'audit', 'KEEP'), 'for the audit')
        repo = self.put('repo', 'copy.bin')
        os.mkdir(os.path.join(self.dd, 'repo', '.git'))
        linked = self.put('linked.bin')
        os.link(linked, self.path('elsewhere.bin'))
        outside = self.path('outside', 'copy.bin')
        os.makedirs(os.path.dirname(outside))
        shutil.copy(os.path.join(self.dd, 'a.bin'), outside)
        self.age(outside, 40)
        os.symlink(outside, os.path.join(self.dd, 'link.bin'))
        os.symlink(os.path.dirname(outside), os.path.join(self.dd, 'linked-folder'))
        for folder in ('audit', 'repo', 'repair-backups'):
            self.age(os.path.join(self.dd, folder), 40)
        plan = self.plan(self.dd_cfg())
        self.assertKept(plan, secret, 'credential name')
        self.assertKept(plan, lock, 'lock file')
        for path in (db, beside, log, jsonl):
            self.assertKept(plan, path, 'written in place')
        self.assertKept(plan, os.path.dirname(kept_name), 'must keep')
        self.assertKept(plan, os.path.dirname(keep), 'KEEP file', 'for the audit')
        self.assertKept(plan, os.path.dirname(repo), 'git folder')
        self.assertKept(plan, linked, 'hard-linked', 'outside dedupe.folders')       # never the file kept either
        for path in (kept_name, keep, repo, os.path.join(self.dd, 'link.bin'), os.path.join(self.dd, 'linked-folder'), outside):
            self.assertNotCandidate(plan, path)
        self.assertEqual(plan.items, [])
        self.run_cleaner(self.dd_cfg())
        self.assertEqual(os.stat(outside).st_nlink, 1)
        self.assertEqual(os.stat(linked).st_nlink, 2)

    def test_another_owners_file_stays(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        theirs = self.ino(b)
        real = self.c.guard.owner
        with mock.patch.object(self.c.guard, 'owner', lambda st: 0 if st.st_ino == theirs else real(st)):
            plan = self.plan(self.dd_cfg())
        self.assertKept(plan, b, 'another owner')
        self.assertEqual(plan.items, [])

    def test_files_with_other_metadata_are_not_linked(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin', mode=0o600)
        plan = self.plan(self.dd_cfg())
        self.assertEqual(plan.items, [])
        self.assertKept(plan, b, 'other metadata', a)
        c = self.put('c.bin', mode=0o600, days=45)      # a set of its own: the two 0600 files become one
        plan = self.plan(self.dd_cfg())
        self.assertEqual(self.items(plan), [b])
        self.assertEqual(plan.items[0].keeper, c)

    def test_files_with_other_extended_attributes_are_not_linked(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        try:
            os.setxattr(b, 'user.origin', b'mirror')
        except OSError as ex:
            if ex.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
                self.skipTest('no user extended attributes on this filesystem')
            raise
        self.age(b, 40)
        plan = self.plan(self.dd_cfg())
        self.assertEqual(plan.items, [])
        self.assertKept(plan, b, 'other metadata')

    def test_a_mount_point_inside_is_another_filesystem(self):
        self.put('a.bin', days=50)
        inner = self.put('mnt', 'b.bin')
        self.mount_points.add(os.path.dirname(inner))
        plan = self.plan(self.dd_cfg())
        self.assertKept(plan, os.path.dirname(inner), 'another filesystem')
        self.assertEqual(plan.items, [])

    def test_never_across_filesystems(self):
        if not os.path.isdir('/dev/shm') or os.stat('/dev/shm').st_dev == os.stat(self.dd).st_dev:
            self.skipTest('no second filesystem at /dev/shm')
        shm = os.path.realpath(tempfile.mkdtemp(prefix='cleaner-test-', dir='/dev/shm'))
        self.addCleanup(shutil.rmtree, shm)
        a = self.put('a.bin', days=50)
        b = os.path.join(shm, 'b.bin')
        shutil.copy(a, b)
        self.age(b, 40)
        cfg = self.dd_cfg(folders=[{'path': self.dd, 'days': 30}, {'path': shm, 'days': 30}])
        plan = self.plan(cfg)
        self.assertEqual((plan.items, plan.kept), ([], []))

    def test_a_folder_reached_through_a_link_is_kept(self):
        real = self.mkdir('real', 'dd')
        for name in ('a.bin', 'b.bin'):
            with open(os.path.join(real, name), 'wb') as f:
                f.write(body())
            self.age(os.path.join(real, name), 40)
        os.symlink(self.path('real'), self.path('via'))
        cfg = self.cfg(dedupe={'folders': [{'path': self.path('via', 'dd'), 'days': 30}]})
        self.assertKept(self.plan(cfg), self.path('via', 'dd'), 'symbolic link')
        self.run_cleaner(cfg)
        self.assertNotEqual(self.ino(os.path.join(real, 'a.bin')), self.ino(os.path.join(real, 'b.bin')))


class Keys(Case):
    def test_folders_it_may_never_take_are_refused(self):
        self.mkdir('home', '.codex', 'sessions')
        bad = [
            (self.home, 'the home folder or holds it'),
            (self.dir, 'the home folder or holds it'),
            (os.path.join(self.home, '.codex', 'sessions'), 'must-keep path'),
            (os.path.join(self.home, '.t3'), 'must-keep path'),            # holds ~/.t3*/userdata
            (self.path('x', 'repair-backups'), 'must-keep name'),
            (self.path('state'), 'logFile'),
            (self.tmp, 'another rule deletes from'),
            (self.path('cold', 'inner'), 'compressCold'),
        ]
        for path, why in bad:
            with self.subTest(path=path):
                with self.assertRaises(machine_file.MachineFileError) as got:
                    self.cfg(tmp={'root': self.tmp, 'patterns': {'*': 7}},
                             compressCold={'folders': [{'path': self.path('cold'), 'days': 30}]},
                             dedupe={'folders': [{'path': path, 'days': 30}]})
                self.assertIn(why, str(got.exception))
        with self.assertRaises(machine_file.MachineFileError) as got:
            self.dd_cfg(folders=[{'path': self.dd, 'days': 30}, {'path': os.path.join(self.dd, 'in'), 'days': 30}])
        self.assertIn('another of its folders', str(got.exception))

    def test_days_have_a_floor_and_a_path_is_needed(self):
        for entry in ({'path': self.dd, 'days': 6}, {'path': self.dd}, {'days': 30}, {'path': self.dd, 'days': 30, 'more': 1}):
            with self.subTest(entry=entry), self.assertRaises(machine_file.MachineFileError):
                self.dd_cfg(folders=[entry])
        self.assertEqual(self.dd_cfg(days=7).dedupe_folders[0].days, 7)


class Report(Case):
    def test_the_report_counts_what_links_freed_and_sizes_the_folders(self):
        self.put('a.bin', days=50), self.put('b.bin')
        cfg = self.dd_cfg()
        self.run_cleaner(cfg)
        got = self.c.report.freed(cfg)
        self.assertEqual((got.links, got.deletes, got.bytes, got.skipped), (1, 0, 200 * 1024, 0))
        self.assertIn(('dedupe folders', 'rule:dedupe'), self.c.config.CLASSES)


MUTATIONS = {
    'days': ('dedupe', 'if age < folder.days * DAY:', 'if False:', Dedupe, 'test_files_changed_lately_small_or_with_other_bytes_are_not_taken'),
    'small files': ('dedupe', "if name.endswith(PART) or st.st_size < MIN_BYTES:", "if name.endswith(PART):",
                    Dedupe, 'test_files_changed_lately_small_or_with_other_bytes_are_not_taken'),
    'byte for byte': ('dedupe', 'if not equal:', 'if False:', Compared, 'test_files_a_hash_calls_the_same_are_compared_byte_for_byte'),
    'changed while read': ('dedupe', "        if not _same(os.fstat(fd), _look(st)):\n            return Refusal('changed', 'it changed while it was read: it stays as it is')\n        return",
                           "        if False:\n            return Refusal('changed', 'it changed while it was read: it stays as it is')\n        return",
                           Compared, 'test_a_file_that_changes_while_it_is_read_is_kept'),
    'changed while compared': ('dedupe', 'if not _same(os.fstat(a), item.look) or not _keeper_same(os.fstat(b), item.keeper_look):', 'if False:',
                               Compared, 'test_a_file_that_changes_while_it_is_compared_stays'),
    'the last check': ('rules', 'return dedupe.recheck(cfg, facts, item)', 'return None', Compared, 'test_a_file_changed_after_the_plan_is_refused'),
    'the kept file checked again': ('dedupe', 'if not _keeper_same(kst, item.keeper_look):', 'if False:',
                                    Compared, 'test_a_kept_file_replaced_after_the_plan_is_refused'),
    'a part goes on the next run': ('dedupe', "            item = Item('link-part'", "            continue\n            item = Item('link-part'",
                                    Stopped, 'test_killed_between_the_link_and_the_rename_the_next_run_finishes'),
    'a last name stays': ('dedupe', 'if st.st_nlink < 2:\n        return Refusal', 'if False:\n        return Refusal',
                          Stopped, 'test_a_part_that_is_the_last_name_of_its_content_stays'),
    'a last name stays at the delete': ('dedupe', "!= item.look.ident or st.st_nlink < 2:", "!= item.look.ident:",
                                        Stopped, 'test_a_part_that_lost_its_other_name_after_the_plan_stays'),
    'a failed rename removes the part': ('dedupe', "        except OSError as ex:\n            _unlink_part(parent, part, kident)\n",
                                         "        except OSError as ex:\n", Stopped, 'test_a_failed_rename_removes_the_part_and_keeps_the_duplicate'),
    'in use': ('dedupe', "    refusal = _in_use(facts, folder, path, st)\n    if refusal is not None:\n        return refusal\n    return f'not changed",
               "    return f'not changed", InUse, 'test_files_in_use_stay'),
    'in use at the link': ('dedupe', 'refusal = _in_use(facts, folder, which, st)', 'refusal = None', InUse, 'test_a_file_opened_while_it_is_compared_stays'),
    'credential names': ('dedupe', "if matches(name, CREDENTIAL_NAMES):\n        return Refusal('credential name', name)", "if False:\n        return Refusal('credential name', name)",
                         Refused, 'test_what_it_never_takes'),
    'written in place': ('dedupe', 'if matches(name, IN_PLACE) or any(name + s in names for s in SIDECARS):', 'if False:',
                         Refused, 'test_what_it_never_takes'),
    'must-keep names': ('dedupe', "if guard.kept_name(cfg, name):\n            return Refusal('must keep', name)", "if False:\n            return Refusal('must keep', name)",
                        Refused, 'test_what_it_never_takes'),
    'hard-linked': ('dedupe', 'if st.st_nlink > 1:', 'if False:', Refused, 'test_what_it_never_takes'),
    'kept only with every name inside': ('dedupe', 'if es[0][2].st_nlink <= seen[ident]]', 'if True]', Refused, 'test_what_it_never_takes'),
    'git folders': ('dedupe', "if '.git' in names or", "if False or", Refused, 'test_what_it_never_takes'),
    'another owner': ('dedupe', "    if guard.owner(st) != os.getuid():\n        return Refusal('another owner', f'uid {guard.owner(st)}')\n    if not st.st_mode",
                      "    if not st.st_mode", Refused, 'test_another_owners_file_stays'),
    'the same metadata': ('dedupe', 'by_meta.setdefault(entry[4], []).append(entry)', 'by_meta.setdefault(0, []).append(entry)',
                          Refused, 'test_files_with_other_metadata_are_not_linked'),
    'extended attributes count': ('dedupe', "attrs = tuple(sorted((name, os.getxattr(fd, name)) for name in xattrs(fd)))", "attrs = ()",
                                  Refused, 'test_files_with_other_extended_attributes_are_not_linked'),
    'mount points': ('dedupe', "if here != folder.path and here in points:", "if False:", Refused, 'test_a_mount_point_inside_is_another_filesystem'),
    'one filesystem': ('dedupe', 'by_size.setdefault((entry[2].st_dev, entry[2].st_size), [])', 'by_size.setdefault((0, entry[2].st_size), [])',
                       Refused, 'test_never_across_filesystems'),
    'no links followed': ('dedupe', "if os.path.realpath(path) != path:", "if False:", Refused, 'test_a_folder_reached_through_a_link_is_kept'),
    'config: must keep': ('config', "                held = must_keep_match(self.must_keep, form)\n                if held is not None:",
                          "                held = None\n                if held is not None:", Keys, 'test_folders_it_may_never_take_are_refused'),
    'config: other rules': ('config', "            for other in self.cold_folders:", "            for other in ():", Keys, 'test_folders_it_may_never_take_are_refused'),
    'report: links counted': ('report', "elif verb == 'linked':\n                    out.links += 1", "elif False:\n                    out.links += 1",
                              Report, 'test_the_report_counts_what_links_freed_and_sizes_the_folders'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
