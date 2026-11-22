"""The `dedupe` rule (lib/cleaner/dedupe.py, #486) and its keys (config.py). Every file lives in the test's own
temporary folder (one test also uses a folder of its own in /dev/shm, another filesystem). The inode time (ctime) is
modelled as on a real host: what a test made before the first plan counts by its modification time, every inode
change after that (a link, a rename, a write) counts at its real time; RealTime runs on the real reader. The state
cases: a second run (nothing to do), a run killed at each step (before the link, after it, after the rename, while
the cache is written: the duplicate stays whole, the next run finishes), a part left whose other names are gone
(kept), a name like a part a person made (kept), a file that changes while it is read or compared (kept), a file
opened, changed or given a name between the plan and the link, a missing, broken, old or unwritable cache, a machine
file without the keys and a folder that is not there. RedCheck breaks each rule in a copy of the code and shows its
test then fails.

  python3 -I -B fork-kit/cleaner/tests/dedupe.test.py
"""
import errno, fcntl, json, os, shutil, struct, subprocess, sys, tempfile, time, unittest
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
        self.settled = None
        clock = mock.patch.object(self.c.guard, 'changed', self.changed)
        clock.start()
        self.addCleanup(clock.stop)

    def changed(self, st):
        """guard.changed as a real host reads it, with the files the test made before the first plan as old as their
        modification time says: any change of an inode after that (a link, a rename, a write) counts at its time."""
        if self.settled is not None and (st.st_dev, st.st_ino, st.st_ctime_ns) in self.settled:
            return st.st_mtime
        return max(st.st_mtime, st.st_ctime)

    def settle(self, *more):
        """What is there now counts by its modification time; later inode changes are new."""
        self.settled = set()
        for top in (self.dir,) + more:
            for root, dirs, names in os.walk(top):
                self.old(*(os.path.join(root, n) for n in dirs + names))

    def old(self, *paths):
        """These files, made after the first plan, count by their modification time too (as if made long ago)."""
        for path in paths:
            st = os.lstat(path)
            self.settled.add((st.st_dev, st.st_ino, st.st_ctime_ns))
        time.sleep(0.02)            # the kernel's inode clock is coarse: a change after this has a later time

    def facts(self, cfg, **over):
        if self.settled is None:
            self.settle()
        return super().facts(cfg, **over)

    def dd_cfg(self, days=30, folders=None, in_place=(), **section):
        """A dedupe section for the test's folder; `in_place` is `dedupe.writtenInPlace` (None leaves it out)."""
        dedupe = {'folders': folders or [{'path': self.dd, 'days': days}]}
        if in_place is not None:
            dedupe['writtenInPlace'] = list(in_place)
        return self.cfg(dedupe=dedupe, **section)

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
        with mock.patch.object(self.c.dedupe, '_digest', lambda path, st, run: ('0' * 64, real(path, st, run)[1])):
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
        # Without `dedupe.writtenInPlace` the rule reads no folder: the part stays until the key is back (README).
        code, lines = self.run_cleaner(self.dd_cfg(in_place=None))
        self.assertEqual((code, self.files()), (1, names), lines)
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
        self.old(c)
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
        self.settle(shm)
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
        cfg = self.dd_cfg(folders=[{'path': self.path('via', 'dd'), 'days': 30}])
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


FS_IOC_GETFLAGS = (2 << 30) | (struct.calcsize('l') << 16) | (ord('f') << 8) | 1
FS_IOC_SETFLAGS = (1 << 30) | (struct.calcsize('l') << 16) | (ord('f') << 8) | 2
NODUMP = 0x40


def set_flag(path, flag):
    """Sets a chattr flag on the file; False where the filesystem has none."""
    fd = os.open(path, os.O_RDONLY)
    try:
        old = struct.unpack('i', fcntl.ioctl(fd, FS_IOC_GETFLAGS, struct.pack('i', 0))[:4])[0]
        fcntl.ioctl(fd, FS_IOC_SETFLAGS, struct.pack('i', old | flag))
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def leases_work(folder):
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return fx.load().dedupe._leases_work(fd)
    finally:
        os.close(fd)


class Names(Case):
    """The kept file is judged by every one of its names (both reviews of #563): a file with one name the rule
    refuses, or in use through one name, is neither kept nor joined, and its names are counted again at the link."""

    def keeper_with(self, second):
        a = self.put('a.bin', days=50)
        two = os.path.join(self.dd, second)
        os.link(a, two)
        b = self.put('b.bin')
        return a, two, b

    def test_keeper_with_an_in_place_name_is_not_joined(self):
        a, db, b = self.keeper_with('a.sqlite')
        code, lines = self.run_cleaner(self.dd_cfg())
        self.assertNotEqual(self.ino(b), self.ino(db), 'b.bin now shares every in-place write made through a.sqlite')
        self.assertTrue(any(line.startswith(f'kept {a} (dedupe: hard-linked: 2 names, and the rule keeps {db} (written in place)')
                            for line in lines), lines)

    def test_kept_file_with_an_in_place_name_is_never_joined(self):
        for second in ('a.log', 'id_rsa'):
            with self.subTest(second=second):
                shutil.rmtree(self.dd)
                os.mkdir(self.dd)
                a, two, b = self.keeper_with(second)
                self.settle()
                self.run_cleaner(self.dd_cfg())
                self.assertNotEqual(self.ino(a), self.ino(b))
                self.assertEqual(os.stat(a).st_nlink, 2)

    def test_keeper_with_a_credential_name_is_not_joined(self):
        a, secret, b = self.keeper_with('api-token.bin')
        self.run_cleaner(self.dd_cfg())
        self.assertNotEqual(self.ino(b), self.ino(secret), 'b.bin is now a name of a credential-named file')

    def test_keeper_open_through_its_other_name_is_not_joined(self):
        a, two, b = self.keeper_with('a2.bin')
        self.busy.add(two)                  # a process has the keeper open (for writing) through a2.bin
        self.run_cleaner(self.dd_cfg())
        self.assertNotEqual(self.ino(b), self.ino(a), 'b.bin joined a file a process has open through another name')

    def test_keeper_gaining_a_name_outside_after_the_plan(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        outside = self.path('elsewhere.bin')
        real = self.c.dedupe._compare

        def then_linked(*args):
            out = real(*args)
            if not os.path.exists(outside):
                os.link(a, outside)
            return out
        with mock.patch.object(self.c.dedupe, '_compare', then_linked):
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertNotEqual(self.ino(b), self.ino(outside), 'b.bin joined a file with a name outside the folders')
        self.assertTrue(any(line.startswith(f'FAILED {b} ') and 'its name' in line for line in lines), lines)

    def test_a_name_made_between_the_link_and_the_rename_stops_it(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        outside = self.path('elsewhere.bin')
        real = os.link

        def link_and_another(src, dst, **kw):
            real(src, dst, **kw)
            if dst.endswith('.cleaner-link'):
                real(a, outside)
        with mock.patch.object(self.c.dedupe.os, 'link', link_and_another):
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {b} ') and 'got or lost a name while it was linked' in line
                            for line in lines), lines)
        self.assertNotEqual(self.ino(b), self.ino(a))
        self.assertEqual(self.files(), ['a.bin', 'b.bin'])        # the part is gone again

    def test_a_name_made_and_removed_after_the_plan_stops_it(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        cfg = self.dd_cfg()
        facts = self.facts(cfg)
        plan = self.c.rules.plan(cfg, facts)
        os.link(a, self.path('elsewhere.bin'))
        os.unlink(self.path('elsewhere.bin'))       # as many names as before; only its inode time tells
        failed, lines = self.delete_all(cfg, facts, plan.items)
        self.assertTrue(lines[0].startswith(f'REFUSED {b} (dedupe: not a candidate: the kept file {a} changed'), lines)
        self.assertNotEqual(self.ino(a), self.ino(b))

    def test_a_new_copy_joins_files_linked_before(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        self.run_cleaner(self.dd_cfg())
        self.assertEqual(self.ino(a), self.ino(b))
        c = self.put('c.bin', days=40)              # arrives later; the two linked files' inode time is the link's
        self.old(c)
        code, lines = self.run_cleaner(self.dd_cfg())
        self.assertEqual(code, 0, lines)
        self.assertEqual({self.ino(p) for p in (a, b, c)}, {self.ino(a)}, lines)
        self.assertEqual(os.stat(a).st_nlink, 3)

    def test_a_name_like_a_part_a_person_made_stays(self):
        mine = self.put('.notes.0123abcd.cleaner-link', days=50)
        os.link(mine, os.path.join(self.dd, 'notes.bin'))
        self.put('b.bin')
        plan = self.plan(self.dd_cfg())
        self.assertKept(plan, mine, 'not a part')
        self.assertEqual(plan.items, [])
        self.run_cleaner(self.dd_cfg())
        self.assertEqual(os.stat(mine).st_nlink, 2)


class RealTime(Case):
    """The inode time as the host keeps it: each link changes the kept file's, and the cleaner's own do not count."""

    def test_real_inode_time_three_copies_one_run(self):
        a, b, c = self.put('a.bin', days=50), self.put('b.bin'), self.put('c.bin')
        t0 = time.time_ns()
        time.sleep(0.02)
        # Files made before t0 look old by their content time; any inode change after t0 counts, as the real reader does.
        clock = lambda st: st.st_mtime if st.st_ctime_ns <= t0 else max(st.st_mtime, st.st_ctime)  # noqa: E731
        with mock.patch.object(self.c.guard, 'changed', clock):
            plan = self.plan(self.dd_cfg())
            self.assertEqual(self.items(plan), [b, c])
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertEqual({self.ino(p) for p in (a, b, c)}, {self.ino(a)}, lines)

    def test_real_inode_time_three_copies_all_linked_in_one_run(self):
        a, b, c = self.put('a.bin', days=50), self.put('b.bin'), self.put('c.bin')
        time.sleep(0.05)
        t0 = time.time()
        changed = lambda st: max(st.st_mtime, st.st_ctime if st.st_ctime > t0 else st.st_ctime - 40 * DAY)  # noqa: E731
        with mock.patch.object(self.c.guard, 'changed', changed):
            self.run_cleaner(self.dd_cfg())
        self.assertEqual(len({self.ino(p) for p in (a, b, c)}), 1)

    def test_on_the_real_reader_forty_days_on(self):
        later = self.now + 40 * DAY
        with mock.patch.object(self.c.guard, 'changed', self.real_changed):
            a, b, c = (self.put(n, days=None) for n in ('a.bin', 'b.bin', 'c.bin'))
            self.assertEqual(self.plan(self.dd_cfg()).items, [])       # today they are new
            code, lines = self.run_cleaner(self.dd_cfg(), now=later)
            self.assertEqual(code, 0, lines)
            self.assertEqual({self.ino(p) for p in (a, b, c)}, {self.ino(a)}, lines)
            plan = self.plan(self.dd_cfg(), now=later)
            self.assertEqual((plan.items, plan.kept), ([], []))
            d = self.put('d.bin', days=None)
            code, lines = self.run_cleaner(self.dd_cfg(), now=later)
            self.assertEqual(code, 0, lines)
            self.assertEqual(os.stat(d).st_ino, self.ino(a), lines)
            self.assertEqual(os.stat(a).st_nlink, 4)


class Writes(Case):
    """Names of one file share every later write into any of them (both reviews): the README and the plan say so."""

    def test_cp_onto_one_name_changes_the_other(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        self.run_cleaner(self.dd_cfg())
        self.assertEqual(self.ino(a), self.ino(b))
        new = self.path('new.bin')
        with open(new, 'wb') as f:
            f.write(body('z'))
        subprocess.run(['cp', new, b], check=True)
        self.assertEqual(self.read(a), body('z'), 'cp writes into the file every name shares')

    def test_the_readme_and_the_plan_say_so(self):
        with open(os.path.join(fx.KIT, 'README.md')) as f:
            readme = f.read()
        self.assertNotIn('editors, `cp`', readme)
        for words in ('`cp` onto an existing file', 'shell `>`', '`curl -o`', 'changes every name',
                      'only folders of copies written once'):
            self.assertIn(words, readme)
        self.put('a.bin', days=50), self.put('b.bin')
        code, lines = self.run_cleaner(self.dd_cfg(), really=False)
        self.assertIn(self.c.dedupe.WARNING, lines)
        self.assertIn('cp onto an existing file', self.c.dedupe.WARNING)

    def test_more_kinds_written_in_place(self):
        names = ('disk.qcow2', 'disk.img', 'disk.vmdk', 'disk.vdi', 'disk.raw', 'x.wal', 'x.ldb')
        for name in names:
            self.put(name)
        plan = self.plan(self.dd_cfg())
        for name in names:
            self.assertKept(plan, os.path.join(self.dd, name), 'written in place')

    def test_the_machine_file_adds_kinds_written_in_place(self):
        """A program's own kinds (a game's save files) are named in the machine file, not in the code."""
        a, b = self.put('slot1.SAV', days=50), self.put('slot2.sav')
        c, d = self.put('x.bin', days=50), self.put('y.bin')
        kinds = lambda value: self.dd_cfg(in_place=value)
        plan = self.plan(kinds(['*.sav', 'world.dat']))
        for path in (a, b):
            self.assertKept(plan, path, 'written in place')
        self.assertEqual(self.items(plan), [d])
        for bad in (['saves/*.sav'], [''], ['..'], [5], '*.sav'):
            with self.subTest(bad=bad), self.assertRaises(machine_file.MachineFileError):
                kinds(bad)

    def test_an_older_machine_file_without_written_in_place_links_nothing(self):
        """An older machine file has no `dedupe.writtenInPlace`, and an older cleaner refused kinds the built-in list
        no longer has: with the key missing the rule links nothing and says why, every run, until the owner adds it
        (an explicit [] links as before). The other rules still run."""
        a, b = self.put('one.sav', days=50), self.put('two.sav')
        c, d = self.put('x.bin', days=50), self.put('y.bin')
        gone = self.old_dir('job-1', 3)
        older = self.dd_cfg(in_place=None, tmp={'root': self.tmp, 'patterns': {'job-*': 1}})
        for again in (False, True):
            with self.subTest(again=again):
                code, lines = self.run_cleaner(older)
                self.assertEqual(code, 1, lines)
                said = [line for line in lines if 'dedupe.writtenInPlace is missing' in line]
                self.assertEqual(len(said), 1, lines)
                self.assertTrue(said[0].startswith(f'kept {self.dd} (dedupe: could not read: '), said)
                self.assertEqual(len({self.ino(p) for p in (a, b, c, d)}), 4)
                self.assertFalse(os.path.exists(gone))
        plan = self.plan(older)
        self.assertEqual((self.items(plan), [(k.path, k.code) for k in plan.kept]), ([], [(self.dd, 'could not read')]))
        # The owner names the kinds: those stay, the rest is linked, and a second run has nothing to do.
        for again in (False, True):
            code, lines = self.run_cleaner(self.dd_cfg(in_place=['*.sav']))
            self.assertEqual(code, 0, lines)
            self.assertNotEqual(self.ino(a), self.ino(b))
            self.assertEqual(self.ino(c), self.ino(d))
            self.assertEqual(len([line for line in lines if line.startswith('linked ')]), 0 if again else 1, lines)
        # [] is the owner's word that no program here writes in place: every copy is linked.
        code, lines = self.run_cleaner(self.dd_cfg(in_place=[]))
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.ino(a), self.ino(b))

    def test_files_with_other_chattr_flags_are_not_linked(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        if not set_flag(b, NODUMP):
            self.skipTest('no chattr flags on this filesystem')
        self.age(b, 40)
        plan = self.plan(self.dd_cfg())
        self.assertEqual(plan.items, [])
        self.assertKept(plan, b, 'other metadata')

    def test_an_open_while_it_is_linked_stops_it(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        if not leases_work(self.dd):
            self.skipTest('no leases on this filesystem')
        real = os.link
        for opened in (b, a):
            waited = []

            def link_then_open(src, dst, **kw):
                real(src, dst, **kw)
                try:            # a writer opening it now: told to wait until the cleaner lets go of its lease
                    os.close(os.open(opened, os.O_RDONLY | os.O_NONBLOCK))
                except BlockingIOError:
                    waited.append(opened)
            with self.subTest(opened=opened), mock.patch.object(self.c.dedupe.os, 'link', link_then_open):
                code, lines = self.run_cleaner(self.dd_cfg())
                self.assertEqual(waited, [opened])
                self.assertEqual(code, 1, lines)
                self.assertTrue(any(line.startswith(f'FAILED {b} ') and f'{opened} was opened while it was linked' in line
                                    for line in lines), lines)
                self.assertNotEqual(self.ino(a), self.ino(b))
                self.assertEqual(self.files(), ['a.bin', 'b.bin'])
        code, lines = self.run_cleaner(self.dd_cfg())       # the failed tries were its own: the next run links
        self.assertEqual((code, self.ino(b)), (0, self.ino(a)), lines)

    def test_a_file_open_at_the_link_stays(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        if not leases_work(self.dd):
            self.skipTest('no leases on this filesystem')
        with open(b, 'rb'):     # open in a process the process list did not show (the fake one is empty)
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertTrue(any(line.startswith(f'FAILED {b} ') and 'another descriptor of it is open' in line
                            for line in lines), lines)
        self.assertNotEqual(self.ino(a), self.ino(b))


class Killed(Case):
    """A run killed at each step leaves every name whole; the next run finishes, or the file waits its `days`."""

    def kill_mark(self, at):
        real, calls = self.c.dedupe.Run.mark, []

        def mark(run, ident, ctime):
            calls.append(ident)
            if len(calls) == at:
                raise KeyboardInterrupt('killed')
            return real(run, ident, ctime)
        return mock.patch.object(self.c.dedupe.Run, 'mark', mark)

    def test_killed_before_the_link(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        real = os.link

        def killed(src, dst, **kw):
            if dst.endswith('.cleaner-link'):
                raise KeyboardInterrupt('killed')
            return real(src, dst, **kw)
        with mock.patch.object(self.c.dedupe.os, 'link', killed), self.assertRaises(KeyboardInterrupt):
            self.run_cleaner(self.dd_cfg())
        self.assertEqual(self.files(), ['a.bin', 'b.bin'])
        code, lines = self.run_cleaner(self.dd_cfg())
        self.assertEqual((code, self.ino(b)), (0, self.ino(a)), lines)

    def test_killed_after_the_link_before_its_time_is_kept(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        with self.kill_mark(1), self.assertRaises(KeyboardInterrupt):
            self.run_cleaner(self.dd_cfg())
        part = os.path.join(self.dd, next(n for n in self.files() if n.endswith('.cleaner-link')))
        self.assertEqual((self.read(b), self.ino(part)), (body(), self.ino(a)))
        # Its inode time is not known as the cleaner's own: the kept file waits its days; the part is the cleaner's
        # (written down before the link) and goes.
        code, lines = self.run_cleaner(self.dd_cfg())
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.files(), ['a.bin', 'b.bin'])
        self.assertNotEqual(self.ino(a), self.ino(b))
        code, lines = self.run_cleaner(self.dd_cfg(), now=self.now + 40 * DAY)
        self.assertEqual((code, self.ino(b)), (0, self.ino(a)), lines)

    def test_part_removal_hides_no_foreign_change(self):
        # lens A round 2: a killed link, then a foreign in-place write that sets the time back; the next run removes
        # the part, and must not take that inode time as its own.
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        with self.kill_mark(1), self.assertRaises(KeyboardInterrupt):
            self.run_cleaner(self.dd_cfg())
        st = os.stat(a)
        with open(a, 'r+b') as f:
            f.write(body('z'))
        os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.run_cleaner(self.dd_cfg())
        c = self.put('c.bin', data=body('z'))
        self.old(c)
        code, lines = self.run_cleaner(self.dd_cfg())
        self.assertNotEqual(self.ino(c), self.ino(a), lines)

    def test_killed_after_the_rename(self):
        a, b, c = self.put('a.bin', days=50), self.put('b.bin'), self.put('c.bin')
        with self.kill_mark(2), self.assertRaises(KeyboardInterrupt):
            self.run_cleaner(self.dd_cfg())
        self.assertEqual(self.files(), ['a.bin', 'b.bin', 'c.bin'])
        self.assertEqual(self.ino(a), self.ino(b))
        for p in (a, b, c):
            self.assertEqual(self.read(p), body())
        code, lines = self.run_cleaner(self.dd_cfg(), now=self.now + 40 * DAY)
        self.assertEqual(code, 0, lines)
        self.assertEqual({self.ino(p) for p in (a, b, c)}, {self.ino(a)}, lines)


class Cached(Case):
    """The plan's sha256 comes from the cache while a file's identity, size and both times are unchanged."""

    def reads(self):
        """A patch of os.read that counts reads of the dedupe folder's files."""
        real, got = os.read, []

        def read(fd, n):
            try:
                where = os.readlink(f'/proc/self/fd/{fd}')
            except OSError:
                where = ''
            if where.startswith(self.dd + '/'):
                got.append(where)
            return real(fd, n)
        return got, mock.patch.object(self.c.dedupe.os, 'read', read)

    def test_unchanged_files_are_not_read_again(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin', data=body()[:-1] + b'!')    # the same size, never linked
        cfg = self.dd_cfg()
        got, patch = self.reads()
        with patch:
            self.run_cleaner(cfg)
            self.assertTrue(got)
            del got[:]
            code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, got), (0, []), lines)
        with open(cfg.dedupe_cache) as f:
            self.assertEqual(json.load(f)['version'], 1)

    def test_a_changed_file_is_read_again(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin', data=body()[:-1] + b'!')
        cfg = self.dd_cfg()
        self.run_cleaner(cfg)
        with open(b, 'r+b') as f:          # now the same bytes as a.bin: the same size, a new stamp
            f.seek(len(body()) - 1)
            f.write(body()[-1:])
        self.age(b, 40)
        self.old(b)
        code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, self.ino(b)), (0, self.ino(a)), lines)

    def test_a_missing_broken_old_or_foreign_cache_is_an_empty_one(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin', data=body()[:-1] + b'!')
        cfg = self.dd_cfg()
        os.makedirs(os.path.dirname(cfg.dedupe_cache), exist_ok=True)
        other = self.path('other.json')
        with open(other, 'w') as f:
            f.write('{"version": 1, "digests": {}, "own": {}}')
        for text in (None, '{not json', '{"version": 0, "digests": {"1:2": [1, 2, 3, "x"]}}', '[]', 'link'):
            with self.subTest(text=text):
                if os.path.lexists(cfg.dedupe_cache):
                    os.unlink(cfg.dedupe_cache)
                if text == 'link':
                    os.symlink(other, cfg.dedupe_cache)
                elif text is not None:
                    with open(cfg.dedupe_cache, 'w') as f:
                        f.write(text)
                got, patch = self.reads()
                with patch:
                    code, lines = self.run_cleaner(cfg)
                self.assertEqual(code, 0, lines)
                self.assertTrue(got)                    # read again
                self.assertFalse(os.path.islink(cfg.dedupe_cache))
                with open(cfg.dedupe_cache) as f:
                    self.assertEqual(json.load(f)['version'], 1)
        with open(other) as f:
            self.assertEqual(f.read(), '{"version": 1, "digests": {}, "own": {}}')

    def test_killed_while_the_cache_is_written(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        cfg = self.dd_cfg()
        real = os.replace

        def killed(src, dst, **kw):
            if dst == cfg.dedupe_cache:
                raise KeyboardInterrupt('killed')
            return real(src, dst, **kw)
        with mock.patch.object(self.c.dedupe.os, 'replace', killed), self.assertRaises(KeyboardInterrupt):
            self.run_cleaner(cfg)
        self.assertEqual(self.ino(a), self.ino(b))
        state = os.path.dirname(cfg.dedupe_cache)
        self.assertEqual([n for n in os.listdir(state) if '.dedupe.' in n], [])    # no half-written cache left
        self.assertTrue(os.path.exists(cfg.dedupe_own))                             # what it did is still known
        c = self.put('c.bin', days=40)
        self.old(c)
        code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, self.ino(c)), (0, self.ino(a)), lines)
        self.assertFalse(os.path.exists(cfg.dedupe_own))

    def test_an_unwritable_cache_is_named_and_the_run_goes_on(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        cfg = self.dd_cfg()
        os.makedirs(cfg.dedupe_cache)
        code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, self.ino(b)), (0, self.ino(a)), lines)
        self.assertTrue(any(line.startswith(f'dedupe: could not save {cfg.dedupe_cache}') for line in lines), lines)
        self.assertTrue(os.path.isdir(cfg.dedupe_cache))

    def test_a_plan_writes_no_cache(self):
        self.put('a.bin', days=50), self.put('b.bin')
        cfg = self.dd_cfg()
        self.run_cleaner(cfg, really=False)
        self.assertFalse(os.path.exists(cfg.dedupe_cache))
        self.assertFalse(os.path.exists(cfg.dedupe_own))


def scribble(path, keep_time=False):
    """Writes into the file in place (open, write, close: no descriptor is left for a lease to see); `keep_time`
    sets its modification time back, as `rsync --inplace -t` does, so only its inode time is new."""
    st = os.stat(path)
    with open(path, 'r+b') as f:
        f.write(b'NEW' * 100)
    if keep_time:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


class Gaps(Case):
    """Round 2 of the reviews of #563: the kept file written, or one of its names moved, in the last moments before
    the rename, and a KEEP file or git folder above a configured folder. Each must leave the duplicate's own bytes."""

    def assertUntouched(self, a, b, lines, why):
        self.assertEqual(self.read(b), body(), ('b.bin lost its bytes', lines))
        self.assertNotEqual(self.ino(a), self.ino(b), lines)
        self.assertFalse(any(line.startswith('linked ') for line in lines), lines)
        self.assertTrue(any(line.startswith(f'FAILED {b} ') and why in line for line in lines), lines)
        self.assertEqual(sorted(self.files()), ['a.bin', 'b.bin'])
        # Runs again: the kept file holds other bytes now, so nothing is joined, and the duplicate is still its own.
        code, lines = self.run_cleaner(self.dd_cfg(), now=self.now + 60 * DAY)
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.read(b), body(), lines)
        self.assertNotEqual(self.ino(a), self.ino(b), lines)

    def wrap_take(self, before):
        real = self.c.dedupe._Leases.take

        def take(leases, parent, name, ident, which):
            before(which)
            return real(leases, parent, name, ident, which)
        return mock.patch.object(self.c.dedupe._Leases, 'take', take)

    # lens A, blocking 1
    def test_kept_file_written_just_before_its_lease(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        with self.wrap_take(lambda which: which == a and scribble(a)):
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertUntouched(a, b, lines, 'is not what the last check saw')

    def test_kept_file_written_with_its_time_set_back_just_before_its_lease(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        with self.wrap_take(lambda which: which == a and scribble(a, keep_time=True)):
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertUntouched(a, b, lines, 'is not what the last check saw')

    def test_duplicate_written_with_its_time_set_back_just_before_its_lease(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        before = self.read(b)
        with self.wrap_take(lambda which: which == b and scribble(b, keep_time=True)):
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertNotEqual(self.read(b), before)           # the write stays where it was made
        self.assertEqual(self.read(a), body(), lines)
        self.assertNotEqual(self.ino(a), self.ino(b), lines)
        self.assertTrue(any(line.startswith(f'FAILED {b} ') for line in lines), lines)

    def test_kept_file_written_during_the_link_without_leases(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        real = os.link

        def link(src, dst, **kw):
            if dst.endswith('.cleaner-link'):
                scribble(a)
            return real(src, dst, **kw)
        with mock.patch.object(self.c.dedupe._Leases, 'take', lambda *a, **k: None), \
                mock.patch.object(self.c.dedupe.os, 'link', link):
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertUntouched(a, b, lines, 'changed while it was linked')

    def test_kept_file_written_with_its_time_set_back_before_the_link_without_leases(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        real = self.c.dedupe.Cache.part

        def part(cache, path):
            scribble(a, keep_time=True)
            return real(cache, path)
        with mock.patch.object(self.c.dedupe._Leases, 'take', lambda *a, **k: None), \
                mock.patch.object(self.c.dedupe.Cache, 'part', part):
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertUntouched(a, b, lines, 'is not what the last check saw')

    def test_kept_file_written_with_its_time_set_back_after_the_link_without_leases(self):
        # Right after the link's inode time was written down: only that inode time shows the write.
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        real, done = self.c.dedupe.Cache.record, []

        def record(cache, ident, ctime_ns):
            real(cache, ident, ctime_ns)
            if not done:
                done.append(ident)
                time.sleep(0.02)    # a tick of the kernel's coarse inode clock after the link (README: same tick)
                scribble(a, keep_time=True)
        with mock.patch.object(self.c.dedupe._Leases, 'take', lambda *a, **k: None), \
                mock.patch.object(self.c.dedupe.Cache, 'record', record):
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertUntouched(a, b, lines, 'changed while it was linked')

    def test_the_run_line_says_what_was_checked(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        code, lines = self.run_cleaner(self.dd_cfg())
        self.assertTrue(any(line.startswith(f'linked {b} ') and 'compared byte for byte, both unchanged at the last '
                            'look before the rename' in line for line in lines), lines)

    # lens B, major 1
    def linked_pair_then_copy(self):
        a, b = self.put('a.bin', days=50), self.put('b.bin')
        self.run_cleaner(self.dd_cfg())
        self.assertEqual(self.ino(a), self.ino(b))
        c = self.put('c.bin', days=40)
        self.old(c)
        return a, b, c

    def test_other_name_of_keeper_moved_outside_between_last_check_and_link(self):
        a, b, c = self.linked_pair_then_copy()
        outside, real = self.path('elsewhere.bin'), self.c.dedupe.Cache.part

        def part(cache, path):
            if not os.path.exists(outside):
                os.rename(b, outside)
            return real(cache, path)
        with mock.patch.object(self.c.dedupe.Cache, 'part', part):
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertNotEqual(self.ino(c), self.ino(outside), lines)
        self.assertTrue(any(line.startswith(f'FAILED {c} ') for line in lines), lines)
        self.assertEqual(self.read(c), body())

    def test_other_name_of_keeper_renamed_to_db_between_link_and_rename(self):
        a, b, c = self.linked_pair_then_copy()
        db, real = os.path.join(self.dd, 'b.sqlite'), os.link

        def link(src, dst, **kw):
            real(src, dst, **kw)
            if dst.endswith('.cleaner-link') and not os.path.exists(db):
                os.rename(b, db)
        with mock.patch.object(self.c.dedupe.os, 'link', link):
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertNotEqual(self.ino(c), self.ino(db), lines)
        self.assertTrue(any(line.startswith(f'FAILED {c} ') for line in lines), lines)
        self.assertEqual(sorted(self.files()), ['a.bin', 'b.sqlite', 'c.bin'])     # the part is gone

    def test_other_name_of_keeper_moved_at_the_lease_check(self):
        # The count of names stays the same; the move happens at the last look at the leases.
        a, b, c = self.linked_pair_then_copy()
        outside, real = self.path('elsewhere.bin'), self.c.dedupe._Leases.broken

        def broken(leases):
            if not os.path.exists(outside):
                os.rename(b, outside)
            return real(leases)
        with mock.patch.object(self.c.dedupe._Leases, 'broken', broken):
            code, lines = self.run_cleaner(self.dd_cfg())
        self.assertNotEqual(self.ino(c), self.ino(outside), lines)
        self.assertEqual(self.read(c), body())

    # lens A, major 2
    def test_folder_inside_a_git_work_tree(self):
        repo = os.path.join(self.dd, 'repo')
        os.makedirs(os.path.join(repo, '.git'))
        a = self.put('repo', 'assets', 'a.bin', days=50)
        b = self.put('repo', 'assets', 'b.bin')
        cfg = self.dd_cfg(folders=[{'path': os.path.join(repo, 'assets'), 'days': 30}])
        code, lines = self.run_cleaner(cfg)
        self.assertNotEqual(self.ino(a), self.ino(b), ('linked inside a git work tree', lines))
        self.assertTrue(any(line.startswith(f'kept {os.path.join(repo, "assets")} (dedupe: git folder') for line in lines),
                        lines)

    def test_folder_inside_a_bare_repository(self):
        repo = os.path.join(self.dd, 'repo.git')
        for name in ('objects', 'refs'):
            os.makedirs(os.path.join(repo, name))
        open(os.path.join(repo, 'HEAD'), 'w').close()
        a = self.put('repo.git', 'objects', 'x', 'a.bin', days=50)
        b = self.put('repo.git', 'objects', 'x', 'b.bin')
        cfg = self.dd_cfg(folders=[{'path': os.path.join(repo, 'objects', 'x'), 'days': 30}])
        code, lines = self.run_cleaner(cfg)
        self.assertNotEqual(self.ino(a), self.ino(b), lines)

    def test_folder_under_a_keep_file(self):
        open(os.path.join(self.dd, 'KEEP'), 'w').close()
        a = self.put('sub', 'deeper', 'a.bin', days=50)
        b = self.put('sub', 'deeper', 'b.bin')
        cfg = self.dd_cfg(folders=[{'path': os.path.join(self.dd, 'sub', 'deeper'), 'days': 30}])
        code, lines = self.run_cleaner(cfg)
        self.assertNotEqual(self.ino(a), self.ino(b), ('linked under a KEEP file', lines))
        self.assertTrue(any('(dedupe: KEEP file' in line for line in lines), lines)

    def test_a_keep_file_above_made_between_the_plan_and_the_link(self):
        a = self.put('sub', 'a.bin', days=50)
        b = self.put('sub', 'b.bin')
        cfg = self.dd_cfg(folders=[{'path': os.path.join(self.dd, 'sub'), 'days': 30}])
        real = self.c.dedupe.recheck

        def recheck(cfg, facts, item):
            open(os.path.join(self.dd, 'KEEP'), 'w').close()
            return real(cfg, facts, item)
        with mock.patch.object(self.c.dedupe, 'recheck', recheck):
            code, lines = self.run_cleaner(cfg)
        self.assertNotEqual(self.ino(a), self.ino(b), lines)
        self.assertEqual(self.read(b), body())

    def test_a_marker_made_in_a_subfolder_at_the_last_check_stops_the_link(self):
        """#620: the last check reads every folder from the configured folder down to the file's folder: a KEEP file
        or a `.git` made between the plan and the link in `sub/mid`, with both files in `sub/mid/deeper`, leaves
        both. Once it is gone, the next run links."""
        for marker in ('KEEP', '.git'):
            with self.subTest(marker=marker):
                a = self.put(f'sub{marker}', 'mid', 'deeper', 'a.bin', days=50)
                b = self.put(f'sub{marker}', 'mid', 'deeper', 'b.bin')
                self.settle()
                cfg = self.dd_cfg(folders=[{'path': os.path.join(self.dd, f'sub{marker}'), 'days': 30}])
                at, real = os.path.join(self.dd, f'sub{marker}', 'mid', marker), self.c.dedupe.recheck

                def recheck(cfg, facts, item):
                    if not os.path.lexists(at):
                        open(at, 'w').close()
                        self.old(at, os.path.dirname(at))
                    return real(cfg, facts, item)
                with mock.patch.object(self.c.dedupe, 'recheck', recheck):
                    code, lines = self.run_cleaner(cfg)
                self.assertNotEqual(self.ino(a), self.ino(b), lines)
                self.assertEqual((self.read(a), self.read(b)), (body(), body()))
                self.assertTrue(any('KEEP file' in line or 'git folder' in line for line in lines), lines)
                os.remove(at)
                self.old(os.path.dirname(at))
                code, lines = self.run_cleaner(cfg)
                self.assertEqual((code, self.ino(a)), (0, self.ino(b)), lines)

    def test_a_keep_folder_above_keeps_it_as_one_below_does(self):
        """#604: a folder named KEEP counts the same above a configured folder and in it."""
        os.makedirs(os.path.join(self.dd, 'KEEP'))
        a = self.put('KEEP', 'a.bin', days=50)
        b = self.put('KEEP', 'b.bin')
        cfg = self.dd_cfg(folders=[{'path': os.path.join(self.dd, 'KEEP'), 'days': 30}])
        code, lines = self.run_cleaner(cfg)
        self.assertNotEqual(self.ino(a), self.ino(b), lines)
        self.assertTrue(any('(dedupe: KEEP file: a folder named KEEP' in line for line in lines), lines)

    def test_a_marker_above_that_cannot_be_read_keeps_it(self):
        """#604: a `.git` or a KEEP above that cannot be looked up is not "not there"."""
        for name in ('.git', 'KEEP'):
            with self.subTest(name=name):
                a = self.put(f'no{name}', 'in', 'a.bin', days=50)
                b = self.put(f'no{name}', 'in', 'b.bin')
                target, real = os.path.join(self.dd, f'no{name}', name), os.lstat

                def lstat(at, *args, **kwargs):
                    if at == target:
                        raise PermissionError(errno.EACCES, 'Permission denied', at)
                    return real(at, *args, **kwargs)
                cfg = self.dd_cfg(folders=[{'path': os.path.dirname(a), 'days': 30}])
                with mock.patch.object(os, 'lstat', lstat):
                    code, lines = self.run_cleaner(cfg)
                self.assertNotEqual(self.ino(a), self.ino(b), lines)
                self.assertTrue(any('(dedupe: could not read' in line for line in lines), lines)

    def test_a_git_folder_above_a_bind_mount_on_its_own_filesystem(self):
        """#604: the configured folder is reached through a mount that shows the folder /data of a disk; a second
        mount shows the whole disk, with a `.git` in its top folder. A second run still leaves it; once the `.git`
        is gone the next run links."""
        fs, view = self.mkdir('dd', 'fs'), self.mkdir('dd', 'view')
        self.mount_lines += [(fs, '8:9', '/'), (view, '8:9', '/data')]
        os.mkdir(os.path.join(fs, '.git'))
        a = self.put('view', 'in', 'a.bin', days=50)
        b = self.put('view', 'in', 'b.bin')
        cfg = self.dd_cfg(folders=[{'path': os.path.dirname(a), 'days': 30}])
        for _ in range(2):
            code, lines = self.run_cleaner(cfg)
            self.assertNotEqual(self.ino(a), self.ino(b), lines)
            self.assertTrue(any(f'(dedupe: git folder: {fs}, above {os.path.dirname(a)}' in line for line in lines), lines)
        os.rmdir(os.path.join(fs, '.git'))
        code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, self.ino(a)), (0, self.ino(b)), lines)
        self.assertEqual(self.read(b), body())

    def test_a_mount_taken_away_after_the_list_was_read_keeps_both(self):
        """#624: the list (this process's own) names a mount at `fs` showing the whole disk, with a view of its folder
        /data holding the files; `fs` was unmounted after the list was read, so a `.git` there would not be seen.
        Kept, on a second run too; once the kernel shows the disk at `fs` again, the next run links."""
        dev = os.lstat(self.dir).st_dev
        device = f'{os.major(dev)}:{os.minor(dev)}'
        fs, view = self.mkdir('dd', 'fs'), self.mkdir('dd', 'view')
        self.mount_lines[:] = [('/', device, '/'), (fs, device, '/'), (view, device, '/data')]
        self.mount_ids, self.mount_types, self.mount_own = [(1, 0), (2, 1), (3, 1)], ['ext4'] * 3, True
        a = self.put('view', 'in', 'a.bin', days=50)
        b = self.put('view', 'in', 'b.bin')
        cfg = self.dd_cfg(folders=[{'path': os.path.dirname(a), 'days': 30}])
        table = {'/': 1, fs: 2, view: 3}
        with self.kernel({'/': 1, view: 3}):
            for _ in range(2):
                code, lines = self.run_cleaner(cfg)
                self.assertNotEqual(self.ino(a), self.ino(b), lines)
                self.assertTrue(any(f'{fs} is not on the mount the mount list names' in line for line in lines), lines)
        with self.kernel(table):
            code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, self.ino(a)), (0, self.ino(b)), lines)
        self.assertEqual(self.read(b), body())

    def test_a_folder_on_a_layered_filesystem_keeps_both(self):
        """#624: the dedupe folder is on a type that is not LOCAL (an overlay, a FUSE or network share, an unknown
        type), below a folder on one, or on a local mount of its own below one ('above'): what is above what that
        type shows is not seen, so both are kept, on a second run too, unless its entry says `layered: true`."""
        a = self.put('in', 'a.bin', days=50)
        b = self.put('in', 'b.bin')
        folder = os.path.dirname(a)
        for kind, lines_, types_, first in (
                ('overlay', [(folder, '0:77', '/')], ['ext4', 'overlay'], folder),
                ('fuse.bindfs', [], ['fuse.bindfs'], folder),
                ('nfs4', [(self.dd, '0:77', '/'), (folder, '0:0', '/')], ['ext4', 'nfs4', 'ext4'], self.dd),
                ('somefs', [], ['somefs'], folder)):
            with self.subTest(kind=kind):
                self.mount_lines[:] = [('/', '0:0', '/')] + lines_
                self.mount_types = types_
                cfg = self.dd_cfg(folders=[{'path': folder, 'days': 30}])
                for _ in range(2):
                    code, lines = self.run_cleaner(cfg)
                    self.assertNotEqual(self.ino(a), self.ino(b), lines)
                    self.assertTrue(any(f'{first} is on {kind} at' in line for line in lines), lines)
        code, lines = self.run_cleaner(self.dd_cfg(folders=[{'path': folder, 'days': 30, 'layered': True}]))
        self.assertEqual((code, self.ino(a)), (0, self.ino(b)), lines)

    def test_only_local_filesystems_link(self):
        """#624: LOCAL is an allow list, the same as for compress-cold: on each type in it the files are linked."""
        kinds = ('ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'bcachefs', 'f2fs', 'zfs', 'jfs', 'nilfs2', 'tmpfs', 'devtmpfs',
                 'ramfs', 'vfat', 'msdos', 'exfat', 'ntfs', 'ntfs3', 'hfsplus', 'fuseblk')
        pairs = [(self.put(f'local{n}', 'a.bin', days=50), self.put(f'local{n}', 'b.bin')) for n in range(len(kinds))]
        for kind, (a, b) in zip(kinds, pairs):
            with self.subTest(kind=kind):
                self.mount_lines[:], self.mount_types = [('/', '0:0', '/')], [kind]
                code, lines = self.run_cleaner(self.dd_cfg(folders=[{'path': os.path.dirname(a), 'days': 30}]))
                self.assertEqual((code, self.ino(a)), (0, self.ino(b)), lines)

    def test_a_marker_made_while_the_bytes_are_compared_stops_the_link(self):
        """#604: every name's folders are read once more after the comparison: a KEEP file made then above the
        duplicate's folder or above the kept file's folder leaves both. Once it is gone, the next run links."""
        for n, side in enumerate(('one', 'two')):
            with self.subTest(side=side):
                one, two = self.mkdir('dd', f'pair{n}', 'one', 'in'), self.mkdir('dd', f'pair{n}', 'two', 'in')
                a = self.put(f'pair{n}', 'one', 'in', 'a.bin', days=50)
                b = self.put(f'pair{n}', 'two', 'in', 'b.bin')
                self.settle()               # made after the first subtest's plan: as old as their times say
                cfg = self.dd_cfg(folders=[{'path': one, 'days': 30}, {'path': two, 'days': 30}])
                at, real = os.path.join(self.dd, f'pair{n}', side, 'KEEP'), self.c.dedupe._compare

                def compare(*args):
                    got = real(*args)
                    open(at, 'w').close()
                    return got
                with mock.patch.object(self.c.dedupe, '_compare', compare):
                    code, lines = self.run_cleaner(cfg)
                self.assertNotEqual(self.ino(a), self.ino(b), lines)
                self.assertEqual((self.read(a), self.read(b)), (body(), body()))
                self.assertTrue(any('KEEP file' in line and 'nothing changed' in line for line in lines), lines)
                os.remove(at)
                code, lines = self.run_cleaner(cfg)
                self.assertEqual((code, self.ino(a)), (0, self.ino(b)), lines)

    # minors
    def test_dax_and_verity_flags_are_compared(self):
        self.assertEqual(self.c.dedupe.FLAGS_MASK & 0x02000000, 0x02000000)     # FS_DAX_FL, chattr +x
        self.assertEqual(self.c.dedupe.FLAGS_MASK & 0x00100000, 0x00100000)     # FS_VERITY_FL

    def test_mail_boxes_and_partial_downloads_are_written_in_place(self):
        names = ('mbox', 'inbox.mbox', 'big.iso.part', 'big.iso.crdownload', 'big.iso.partial', 'big.iso.!qb')
        for name in names:
            self.put(name)
        plan = self.plan(self.dd_cfg())
        for name in names:
            self.assertKept(plan, os.path.join(self.dd, name), 'written in place')

    def test_files_in_two_folders_are_linked_to_each_other_and_the_readme_says_so(self):
        one, two = self.mkdir('dd', 'one'), self.mkdir('dd', 'two')
        a, b = self.put('one', 'a.bin', days=50), self.put('two', 'b.bin', days=40)
        cfg = self.dd_cfg(folders=[{'path': one, 'days': 30}, {'path': two, 'days': 7}])
        code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, self.ino(a)), (0, self.ino(b)), lines)
        with open(os.path.join(fx.KIT, 'README.md')) as f:
            readme = ' '.join(f.read().split())
        self.assertIn('two configured folders may be linked to each other', readme)
        self.assertIn('a folder above it up to `/`', readme)
        self.assertNotIn('two system calls between that last check', readme)


MUTATIONS = {
    'days': ('dedupe', 'if age < folder.days * DAY:', 'if False:', Dedupe, 'test_files_changed_lately_small_or_with_other_bytes_are_not_taken'),
    'small files': ('dedupe', "if name.endswith(PART) or st.st_size < MIN_BYTES:", "if name.endswith(PART):",
                    Dedupe, 'test_files_changed_lately_small_or_with_other_bytes_are_not_taken'),
    'byte for byte': ('dedupe', 'if not equal:', 'if False:', Compared, 'test_files_a_hash_calls_the_same_are_compared_byte_for_byte'),
    'changed while read': ('dedupe', "        if not _same(os.fstat(fd), _look(st)):\n            return Refusal('changed', 'it changed while it was read: it stays as it is')\n        run.cache",
                           "        if False:\n            return Refusal('changed', 'it changed while it was read: it stays as it is')\n        run.cache",
                           Compared, 'test_a_file_that_changes_while_it_is_read_is_kept'),
    'changed while compared': ('dedupe', 'if not _same(os.fstat(a), item.look) or not _keeper_same(os.fstat(b), item):', 'if False:',
                               Compared, 'test_a_file_that_changes_while_it_is_compared_stays'),
    'the last check': ('rules', 'return dedupe.recheck(cfg, facts, item)', 'return None', Compared, 'test_a_file_changed_after_the_plan_is_refused'),
    'the kept file checked again': ('dedupe', 'if not _keeper_same(kst, item):\n            return Refusal', 'if False:\n            return Refusal',
                                    Compared, 'test_a_kept_file_replaced_after_the_plan_is_refused'),
    'a part goes on the next run': ('dedupe', "            item = Item('link-part'", "            continue\n            item = Item('link-part'",
                                    Stopped, 'test_killed_between_the_link_and_the_rename_the_next_run_finishes'),
    'a last name stays': ('dedupe', 'if st.st_nlink < 2:\n        return Refusal', 'if False:\n        return Refusal',
                          Stopped, 'test_a_part_that_is_the_last_name_of_its_content_stays'),
    'a last name stays at the delete': ('dedupe', "!= item.look.ident or st.st_nlink < 2:", "!= item.look.ident:",
                                        Stopped, 'test_a_part_that_lost_its_other_name_after_the_plan_stays'),
    'a failed rename removes the part': ('dedupe', "        except OSError as ex:\n            _unlink_part(parent, part, kident)\n",
                                         "        except OSError as ex:\n", Stopped, 'test_a_failed_rename_removes_the_part_and_keeps_the_duplicate'),
    'in use': ('dedupe', "        return refusal\n    return _in_use(facts, folder, path, st)\n", "        return refusal\n    return None\n",
               InUse, 'test_files_in_use_stay'),
    'in use at the link': ('dedupe', 'refusal = _in_use(facts, item.folder, path, st)', 'refusal = None', InUse, 'test_a_file_opened_while_it_is_compared_stays'),
    'in use at the link through each name of the kept file': ('dedupe', 'refusal = _in_use(facts, kfolder, kpath, kst)', 'refusal = None',
                                                              InUse, 'test_a_file_opened_while_it_is_compared_stays'),
    'credential names': ('dedupe', "if matches(name, CREDENTIAL_NAMES):\n        return Refusal('credential name', name)", "if False:\n        return Refusal('credential name', name)",
                         Refused, 'test_what_it_never_takes'),
    'written in place': ('dedupe', 'if matches(name, IN_PLACE + cfg.dedupe_in_place) or any(name + s in names for s in SIDECARS):', 'if False:',
                         Refused, 'test_what_it_never_takes'),
    'must-keep names': ('dedupe', "if guard.kept_name(cfg, name):\n            return Refusal('must keep', name)", "if False:\n            return Refusal('must keep', name)",
                        Refused, 'test_what_it_never_takes'),
    'hard-linked': ('dedupe', 'if st.st_nlink > 1:', 'if False:', Refused, 'test_what_it_never_takes'),
    'kept only with every name inside': ('dedupe', 'if _names_refusal(answers[ident], es[0][2].st_nlink) is None]', 'if True]',
                                         Refused, 'test_what_it_never_takes'),
    'kept only when the rule takes every name': ('dedupe', "refused = next(((path, a) for _, path, a in names if isinstance(a, Refusal)), None)",
                                                 'refused = None', Names, 'test_keeper_with_an_in_place_name_is_not_joined'),
    'names counted just before the rename': ('dedupe', 'or pst.st_nlink != len(knames) + 1:', ':',
                                             Names, 'test_a_name_made_between_the_link_and_the_rename_stops_it'),
    'the inode time of the kept file': ('dedupe', 'and st.st_ctime_ns == run.ctime.get(look.ident)', '',
                                        Names, 'test_a_name_made_and_removed_after_the_plan_stops_it'),
    "the cleaner's own links do not count as changes": ('dedupe', 'if run is not None and run.cache.own.get((st.st_dev, st.st_ino)) == st.st_ctime_ns:',
                                                        'if False:', Names, 'test_a_new_copy_joins_files_linked_before'),
    'a part is one the cleaner wrote down': ('dedupe', 'if path not in run.cache.parts:', 'if False:',
                                             Names, 'test_a_name_like_a_part_a_person_made_stays'),
    'a lease broken by an open': ('dedupe', 'opened = leases.broken()', 'opened = None', Writes, 'test_an_open_while_it_is_linked_stops_it'),
    'a lease refused for an open file': ('dedupe', "if ex.errno in (errno.EAGAIN, errno.EBUSY) and _leases_work(parent):", 'if False:',
                                         Writes, 'test_a_file_open_at_the_link_stays'),
    'chattr flags count': ('dedupe', 'return (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, attrs, _flags(fd))',
                           'return (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, attrs, 0)', Writes, 'test_files_with_other_chattr_flags_are_not_linked'),
    'more kinds written in place': ('dedupe', "'*.pid', '*.qcow2',", "'*.pid',", Writes, 'test_more_kinds_written_in_place'),
    'kinds the machine file names': ('dedupe', 'matches(name, IN_PLACE + cfg.dedupe_in_place)', 'matches(name, IN_PLACE)',
                                     Writes, 'test_the_machine_file_adds_kinds_written_in_place'),
    'an older machine file links nothing': ('dedupe', '    if cfg.dedupe_in_place is None:\n        for folder in cfg.dedupe_folders:',
                                            '    if False:\n        for folder in cfg.dedupe_folders:',
                                            Writes, 'test_an_older_machine_file_without_written_in_place_links_nothing'),
    'a missing writtenInPlace is not []': ('config', 'None if listed is None else tuple(v.lower() for v in listed)',
                                           'tuple(v.lower() for v in listed or ())',
                                           Writes, 'test_an_older_machine_file_without_written_in_place_links_nothing'),
    'the plan warns': ('dedupe', '        plan.notes.append(WARNING)', '        pass', Writes, 'test_the_readme_and_the_plan_say_so'),
    'the cache needs an unchanged stamp': ('dedupe', 'if cached is not None and tuple(cached[:3]) == stamp:', 'if cached is not None:',
                                           Cached, 'test_a_changed_file_is_read_again'),
    'markers in the folder and under it': ('dedupe', 'found = guard.marker(here)', 'found = None', Refused, 'test_what_it_never_takes'),
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
    'the look before the link': ('dedupe', "            if kst is None or not _keeper_same(kst, item):\n                raise Untouched(f'{item.keeper} is not what",
                                 "            if False:\n                raise Untouched(f'{item.keeper} is not what",
                                 Gaps, 'test_kept_file_written_with_its_time_set_back_just_before_its_lease'),
    'every name read before the rename': ('dedupe', "if kst is None or (kst.st_dev, kst.st_ino) != kident:\n                    why =",
                                          "if False:\n                    why =",
                                          Gaps, 'test_other_name_of_keeper_renamed_to_db_between_link_and_rename'),
    'the kept file as compared before the rename': ('dedupe', "elif ((pst.st_size, pst.st_mtime_ns) != item.keeper_look.stamp[:2]",
                                                    "elif ((pst.st_size, pst.st_mtime_ns) == (None, None)",
                                                    Gaps, 'test_kept_file_written_during_the_link_without_leases'),
    'the inode time before the rename': ('dedupe', "or pst.st_ctime_ns != run.ctime.get(kident)):", "):",
                                         Gaps, 'test_kept_file_written_with_its_time_set_back_after_the_link_without_leases'),
    'a KEEP entry above': ('guard', "    if st is not None:\n        if stat.S_ISDIR", "    if False:\n        if stat.S_ISDIR",
                           Gaps, 'test_folder_under_a_keep_file'),
    'a KEEP folder counts': ('guard', "        if stat.S_ISDIR(st.st_mode):\n            return 'KEEP file', 'a folder named KEEP'",
                             "        if stat.S_ISDIR(st.st_mode):\n            return None",
                             Gaps, 'test_a_keep_folder_above_keeps_it_as_one_below_does'),
    'a git folder above': ('guard', "if at('.git') is not None:", 'if False:',
                           Gaps, 'test_folder_inside_a_git_work_tree'),
    'a .git that cannot be read': ('guard', "if at('.git') is not None:",
                                   "if os.path.lexists(os.path.join(here, '.git')):", Gaps, 'test_a_marker_above_that_cannot_be_read_keeps_it'),
    'the folders above on its own filesystem': ('guard', 'stack.extend((place, True) for place in reversed(places))', 'pass',
                                                Gaps, 'test_a_git_folder_above_a_bind_mount_on_its_own_filesystem'),
    'the folders above, with the mount list': ('dedupe', 'refusal = guard.above_refusal(path, mounts, folder.layered)',
                                               "refusal = guard.above_refusal(path, type(mounts)(lines=[('/', '0:0', '/')]), folder.layered)",
                                               Gaps, 'test_a_git_folder_above_a_bind_mount_on_its_own_filesystem'),
    'the last check reads down to the file': ('dedupe', "        refusal = _folder_refusal(cfg, facts, folder, here, hst, names)\n        if refusal is not None:\n            return refusal\n    try:\n        fst = os.lstat(path)",
                                              "        refusal = _folder_refusal(cfg, facts, folder, here, hst, names) if part is None else None\n        if refusal is not None:\n            return refusal\n    try:\n        fst = os.lstat(path)",
                                              Gaps, 'test_a_marker_made_in_a_subfolder_at_the_last_check_stops_the_link'),
    'the folders again after the comparison': ('dedupe', '                got = _recheck_path(cfg, facts, fdir.path, fpath)\n',
                                               '                got = None\n', Gaps, 'test_a_marker_made_while_the_bytes_are_compared_stops_the_link'),
    'mail boxes and partial downloads': ('dedupe', "'*.swap', 'mbox', '*.mbox', '*.part', '*.crdownload', '*.partial', '*.!qb')", "'*.swap')",
                                         Gaps, 'test_mail_boxes_and_partial_downloads_are_written_in_place'),
    'verity and DAX flags': ('dedupe', ' | FS_VERITY_FL | FS_DAX_FL', '', Gaps, 'test_dax_and_verity_flags_are_compared'),
    'config: must keep': ('config', "                held = must_keep_match(self.must_keep, form)\n                if held is not None:",
                          "                held = None\n                if held is not None:", Keys, 'test_folders_it_may_never_take_are_refused'),
    'config: other rules': ('config', "            for other in self.cold_folders:", "            for other in ():", Keys, 'test_folders_it_may_never_take_are_refused'),
    'report: links counted': ('report', "elif verb == 'linked':\n                    out.links += 1", "elif False:\n                    out.links += 1",
                              Report, 'test_the_report_counts_what_links_freed_and_sizes_the_folders'),
    'dedupe: a layered filesystem keeps both': ('dedupe', 'refusal = guard.above_refusal(path, mounts, folder.layered)',
                                                'refusal = guard.above_refusal(path, mounts, True)',
                                                Gaps, 'test_a_folder_on_a_layered_filesystem_keeps_both'),
    'dedupe: layered true links': ('dedupe', 'refusal = guard.above_refusal(path, mounts, folder.layered)',
                                   'refusal = guard.above_refusal(path, mounts, False)',
                                   Gaps, 'test_a_folder_on_a_layered_filesystem_keeps_both'),
    'dedupe: the mount list must still be true': ('guard', 'if (want is None or now[0] != want\n', 'if (False\n',
                                                  Gaps, 'test_a_mount_taken_away_after_the_list_was_read_keeps_both'),
    'dedupe: every folder above is on a local type': ('guard', 'if kind not in LOCAL and not layered:',
                                                      'if kind not in LOCAL and not layered and not look:',
                                                      Gaps, 'test_a_folder_on_a_layered_filesystem_keeps_both'),
    'dedupe: an allow list': ('guard', 'if kind not in LOCAL and not layered:',
                              "if kind in ('overlay', 'aufs', 'fuse', 'fuse.bindfs', 'fuse.mergerfs') and not layered:",
                              Gaps, 'test_a_folder_on_a_layered_filesystem_keeps_both'),
    'dedupe: local types link': ('guard', "'msdos', 'exfat', 'ntfs', 'ntfs3', 'hfsplus', 'fuseblk')",
                                 "'msdos', 'exfat', 'ntfs', 'ntfs3', 'hfsplus')", Gaps, 'test_only_local_filesystems_link'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
