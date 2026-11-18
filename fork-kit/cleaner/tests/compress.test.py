"""The `compress-cold` rule (lib/cleaner/compress.py) and its keys (config.py). Every file lives in the test's own
temporary folder; zstd is the real one, or a stub in front of it that fails one step. The state cases: a second run,
a run stopped half-way (a part file, or a copy linked and the original still there), a file in use or opened or
changed while it is compressed, a disk nearly full, zstd missing or failing, and a machine file without the keys.
RedCheck breaks each rule in a copy of the code and shows its test then fails.

  python3 -I -B fork-kit/cleaner/tests/compress.test.py
"""
import json, os, re, shutil, struct, subprocess, sys, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cleaner_fixture as fx  # noqa: E402

import machine_file  # noqa: E402

DAY = fx.DAY
GB = 1024 ** 3
REAL_ZSTD = shutil.which('zstd')

# zstd in front of the real one: `state[step]` makes one step ('compress', 'test', 'decompress') fail as a full
# disk does, give other bytes, stop reading its input early and still exit 0, or hang (`hang` seconds, reading
# nothing, then exit 0).
STUB_ZSTD = '''#!{python} -I
import json, os, sys
with open({state!r}) as f:
    state = json.load(f)
args = sys.argv[1:]
step = 'test' if '-t' in args else 'decompress' if '-d' in args else 'compress'
how = state.get(step)
if how == 'fail':
    sys.stdin.buffer.read()
    sys.stdout.buffer.write(b'(half a copy)')
    sys.stderr.write('zstd: error 70 : Write error : cannot write block : No space left on device\\n')
    sys.exit(1)
if how == 'other bytes':
    sys.stdin.buffer.read()
    sys.stdout.buffer.write(b'other bytes')
    sys.exit(0)
if how == 'hang':
    import time
    time.sleep(state['hang'])
    sys.exit(0)
if how == 'stop early':
    import subprocess
    part = sys.stdin.buffer.read(1000)
    sys.stdout.buffer.flush()
    subprocess.run([state['real']] + args, input=part, stdout=sys.stdout.buffer, check=True)
    sys.exit(0)
os.execv(state['real'], ['zstd'] + args)
'''


def text(n=6000):
    return ''.join(f'{i:08d} a line of an old agent session\n' for i in range(n))


@unittest.skipUnless(REAL_ZSTD, 'zstd is not installed')
class Case(fx.Case):
    c = fx.load()

    def setUp(self):
        super().setUp()
        self.cold = self.mkdir('cold')
        self.free = 1 << 50
        free = mock.patch.object(self.c.compress, 'free_bytes', lambda fd: self.free)
        free.start()
        self.addCleanup(free.stop)

    def cold_cfg(self, days=30, **section):
        return self.cfg(compressCold={'folders': [{'path': self.cold, 'days': days}], 'keepFreeGB': 1}, **section)

    def big(self, *parts, days=40, body=None):
        return self.file(os.path.join(self.cold, *parts), body if body is not None else text(), days)

    def unpack(self, path):
        return subprocess.run([REAL_ZSTD, '-q', '-d', '-c', path], check=True, capture_output=True).stdout.decode()

    def pack(self, source, target):
        subprocess.run([REAL_ZSTD, '-q', '-o', target, source], check=True, capture_output=True)

    def zstd_stub(self, **state):
        exe, _ = self.stub('zstd', STUB_ZSTD, {'real': REAL_ZSTD, **state})
        patch = mock.patch.object(self.c.compress, 'ZSTD', exe)
        patch.start()
        self.addCleanup(patch.stop)

    def files(self):
        return sorted(os.path.relpath(os.path.join(root, n), self.cold) for root, _, names in os.walk(self.cold) for n in names)


class Compress(Case):
    def test_a_cold_file_is_replaced_by_its_checked_copy(self):
        path = self.big('sessions', 'old.jsonl')
        os.chmod(path, 0o640)
        before = os.stat(path)
        code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.files(), ['sessions/old.jsonl.zst'])
        self.assertEqual(self.unpack(path + '.zst'), text())
        after = os.stat(path + '.zst')
        self.assertEqual((after.st_mode & 0o777, after.st_mtime_ns), (0o640, before.st_mtime_ns))
        done = [line for line in lines if line.startswith('compressed ')]
        self.assertEqual(len(done), 1, lines)
        self.assertIn('compress-cold: not changed or read for 40 days', done[0])
        self.assertIn('read back and checked', done[0])
        self.assertIn(', compressed 1 (', lines[-1])
        self.assertIn('total: deleted 0 (0 B)', lines[-1])
        with open(self.cold_cfg().log_file) as f:
            log = f.read()
        self.assertIn(' compressing ' + path, log)
        self.assertNotIn('half deleted', log)
        self.assertFalse(os.path.exists(self.cold_cfg().journal_file) and os.path.getsize(self.cold_cfg().journal_file) > 2)

    def test_the_plan_changes_nothing_reads_nothing_and_says_what_it_frees(self):
        path = self.big('old.jsonl')
        before = os.stat(path)
        code, lines = self.run_cleaner(self.cold_cfg(), really=False)
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.files(), ['old.jsonl'])
        after = os.stat(path)
        self.assertEqual((after.st_atime_ns, after.st_mtime_ns), (before.st_atime_ns, before.st_mtime_ns))
        self.assertTrue(any(line.startswith(f'would compress {path} (') for line in lines), lines)
        summary = [line for line in lines if line.startswith('  compress-cold: would compress 1 file (')]
        self.assertEqual(len(summary), 1, lines)
        self.assertIn('frees about', summary[0])
        self.assertNotIn('size not known', summary[0])
        self.assertIn(', would compress 1 (', lines[-1])

    def test_a_second_run_finds_nothing_to_do(self):
        self.big('old.jsonl', body=os.urandom(40000).hex())      # its copy is big enough to be looked at too
        self.big('older.log', days=90)
        self.assertEqual(self.run_cleaner(self.cold_cfg())[0], 0)
        self.assertEqual(self.files(), ['old.jsonl.zst', 'older.log.zst'])
        for name in self.files():
            self.age(os.path.join(self.cold, name), 400)
        plan = self.plan(self.cold_cfg())
        self.assertEqual([(x.path, getattr(x, 'code', None)) for x in plan.items + plan.kept], [])
        code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 0)
        self.assertFalse([line for line in lines if line.startswith('compressed ')], lines)

    def test_files_changed_or_read_lately_stay(self):
        changed = self.big('changed.log', days=3)
        read = self.big('read.log', days=40)
        os.utime(read, (self.now - DAY, self.now - 40 * DAY))
        cold = self.big('cold.log')
        plan = self.plan(self.cold_cfg())
        self.assertNotCandidate(plan, changed)
        self.assertNotCandidate(plan, read)
        self.assertDeletes(plan, cold, 'compress-cold')

    def test_small_and_compressed_files_are_left_alone(self):
        small = self.big('small.log', body='tiny\n')
        packed = self.big('bundle.tar.gz')
        plan = self.plan(self.cold_cfg())
        self.assertNotCandidate(plan, small)
        self.assertNotCandidate(plan, packed)

    def test_a_folder_that_is_not_there_is_nothing_to_do(self):
        os.rmdir(self.cold)
        plan = self.plan(self.cold_cfg())
        self.assertEqual(plan.items + plan.kept, [])


class HalfWay(Case):
    """A run stopped at any point leaves the original or a checked copy, never neither."""

    def test_a_half_written_copy_is_removed_and_the_file_compressed_again(self):
        path = self.big('old.jsonl')
        part = self.file(os.path.join(self.cold, '.old.jsonl.zst.0badc0de.cleaner-part'), 'half a copy')
        plan = self.plan(self.cold_cfg())
        self.assertEqual(self.assertDeletes(plan, part).kind, 'part')
        code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.files(), ['old.jsonl.zst'])
        self.assertEqual(self.unpack(path + '.zst'), text())

    def test_a_part_file_without_its_original_stays(self):
        part = self.file(os.path.join(self.cold, '.gone.jsonl.zst.0badc0de.cleaner-part'), 'half a copy', days=40)
        self.assertKept(self.plan(self.cold_cfg()), part, 'part without its original')
        code, _ = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(part))

    def test_a_copy_linked_before_the_original_went_is_checked_and_then_the_original_goes(self):
        path = self.big('old.jsonl')
        self.pack(path, path + '.zst')
        copy = os.stat(path + '.zst')
        self.age(path, 40)
        code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.files(), ['old.jsonl.zst'])
        self.assertEqual(os.stat(path + '.zst').st_ino, copy.st_ino)
        self.assertEqual(self.unpack(path + '.zst'), text())

    def test_a_different_copy_beside_it_keeps_both(self):
        path = self.big('old.jsonl')
        other = self.file(self.path('other.txt'), 'other content\n')
        self.pack(other, path + '.zst')
        code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {path} ') and 'other bytes' in line for line in lines), lines)
        self.assertEqual(self.files(), ['old.jsonl', 'old.jsonl.zst'])
        self.assertEqual(self.unpack(path + '.zst'), 'other content\n')

    def test_a_name_taken_by_something_else_keeps_the_file(self):
        path = self.big('old.jsonl')
        os.mkdir(path + '.zst')
        self.assertKept(self.plan(self.cold_cfg()), path, 'name taken')

    def test_a_step_that_stops_leaves_the_original_and_no_part(self):
        path = self.big('old.jsonl')

        def stop(fd, size):
            raise KeyboardInterrupt
        plan = self.plan(self.cold_cfg())
        with mock.patch.object(self.c.compress, '_content', stop), self.assertRaises(KeyboardInterrupt):
            self.c.compress.compress(self.assertDeletes(plan, path))
        self.assertEqual(self.files(), ['old.jsonl'])
        with open(path) as f:
            self.assertEqual(f.read(), text())


class InUse(Case):
    def test_files_in_use_stay(self):
        opened, named, locked = self.big('opened.log'), self.big('unit', 'named.log'), self.big('locked.log')
        self.busy.add(opened)
        self.unit_names.add(os.path.join(self.cold, 'unit'))
        self.locks.add(os.stat(locked).st_ino)
        plan = self.plan(self.cold_cfg())
        for path in (opened, named, locked):
            self.assertKept(plan, path, 'in use')

    def test_a_file_opened_while_it_is_compressed_stays_and_its_copy_goes(self):
        path = self.big('old.jsonl')
        real = self.c.compress._write_copy

        def then_opened(*args):
            out = real(*args)
            self.busy.add(path)
            return out
        with mock.patch.object(self.c.compress, '_write_copy', then_opened):
            code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {path} ') and 'in use' in line for line in lines), lines)
        self.assertEqual(self.files(), ['old.jsonl'])

    def test_a_file_changed_while_it_is_compressed_stays(self):
        path = self.big('old.jsonl')
        real = self.c.compress._pack

        def then_changed(src, dst, size):
            out = real(src, dst, size)
            with open(path, 'a') as f:
                f.write('one more line\n')
            return out
        with mock.patch.object(self.c.compress, '_pack', then_changed):
            code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 1, lines)
        self.assertEqual(self.files(), ['old.jsonl'])
        with open(path) as f:
            self.assertEqual(f.read(), text() + 'one more line\n')

    def test_a_file_in_use_at_the_last_check_is_refused(self):
        path = self.big('old.jsonl')
        cfg = self.cold_cfg()
        facts = self.facts(cfg)
        plan = self.c.rules.plan(cfg, facts)
        self.busy.add(path)
        failed, lines = self.delete_all(cfg, facts, plan.items)
        self.assertEqual(failed, 0)
        self.assertEqual(lines, [f'REFUSED {path} (compress-cold: in use: a process has it open or names it)'])
        self.assertEqual(self.files(), ['old.jsonl'])


class Refused(Case):
    def test_a_disk_nearly_full_keeps_the_original(self):
        path = self.big('old.jsonl')
        self.free = GB + 1000
        self.assertKept(self.plan(self.cold_cfg()), path, 'disk nearly full')
        self.assertEqual(self.run_cleaner(self.cold_cfg())[0], 0)
        self.assertEqual(self.files(), ['old.jsonl'])

    def test_compressing_refuses_when_the_disk_filled_up_since_the_check(self):
        path = self.big('old.jsonl')
        item = self.assertDeletes(self.plan(self.cold_cfg()), path)
        self.free = GB
        with self.assertRaises(self.c.facts.Untouched) as raised:
            self.c.compress.compress(item)
        self.assertIn('disk nearly full', str(raised.exception))
        self.assertEqual(self.files(), ['old.jsonl'])

    def test_a_full_disk_while_writing_keeps_the_original(self):
        path = self.big('old.jsonl')
        self.zstd_stub(compress='fail')
        code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {path} ') and 'No space left' in line for line in lines), lines)
        self.assertEqual(self.files(), ['old.jsonl'])

    def test_a_zstd_that_stops_reading_early_keeps_the_original(self):
        path = self.big('old.jsonl')
        self.zstd_stub(compress='stop early')
        code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {path} ') and '1000 read back' in line for line in lines), lines)
        self.assertEqual(self.files(), ['old.jsonl'])
        with open(path) as f:
            self.assertEqual(f.read(), text())

    def test_a_copy_that_does_not_read_back_keeps_the_original(self):
        for step, how in (('decompress', 'other bytes'), ('test', 'fail'), ('decompress', 'fail')):
            with self.subTest(step=step, how=how):
                path = self.big('old.jsonl')
                self.zstd_stub(**{step: how})
                code, lines = self.run_cleaner(self.cold_cfg())
                self.assertEqual(code, 1, lines)
                self.assertTrue(any(line.startswith(f'FAILED {path} ') for line in lines), lines)
                self.assertEqual(self.files(), ['old.jsonl'])
                with open(path) as f:
                    self.assertEqual(f.read(), text())

    def test_without_zstd_nothing_is_compressed_and_the_run_says_so(self):
        path = self.big('old.jsonl')
        with mock.patch.object(self.c.compress, 'ZSTD', 'zstd-not-installed-here'):
            self.assertKept(self.plan(self.cold_cfg()), self.cold, 'could not read', 'not installed')
            code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 1, lines)
        self.assertEqual(self.files(), ['old.jsonl'])
        self.assertTrue(os.path.exists(path))

    def test_what_it_never_takes(self):
        keep = self.big('kept', 'a.log')
        self.file(os.path.join(self.cold, 'kept', 'KEEP'), 'for the audit\n')
        repo = self.big('repo', 'data.pack')
        os.mkdir(os.path.join(self.cold, 'repo', '.git'))
        secret = self.big('api-token.json')
        lock = self.big('job.lock')
        db = self.big('state.sqlite')
        wal = self.big('state.sqlite-wal')
        linked = self.big('linked.log')
        os.link(linked, self.path('elsewhere.log'))
        outside = self.file(self.path('outside', 'big.log'), text(), days=40)
        os.symlink(outside, os.path.join(self.cold, 'link.log'))
        os.symlink(os.path.dirname(outside), os.path.join(self.cold, 'linked-folder'))
        plan = self.plan(self.cold_cfg())
        self.assertKept(plan, os.path.dirname(keep), 'KEEP file', 'for the audit')
        self.assertKept(plan, os.path.dirname(repo), 'git folder')
        self.assertKept(plan, secret, 'credential name')
        self.assertKept(plan, lock, 'lock file')
        self.assertKept(plan, db, 'database')
        self.assertKept(plan, wal, 'database')
        self.assertKept(plan, linked, 'hard-linked')
        for path in (keep, repo, os.path.join(self.cold, 'link.log'), os.path.join(self.cold, 'linked-folder'), outside):
            self.assertNotCandidate(plan, path)
        self.assertEqual(plan.items, [])
        self.run_cleaner(self.cold_cfg())
        self.assertFalse([name for name in self.files() if name.endswith('.zst')])
        self.assertTrue(os.path.exists(outside))

    def test_a_cold_folder_reached_through_a_link_is_kept(self):
        real = self.mkdir('real', 'cold')
        self.file(os.path.join(real, 'old.log'), text(), days=40)
        os.symlink(self.path('real'), self.path('via'))
        for path in (self.path('via', 'cold'), self.cold):
            if path == self.cold:
                os.rmdir(self.cold)
                os.symlink(real, self.cold)
            with self.subTest(path=path):
                cfg = self.cfg(compressCold={'folders': [{'path': path, 'days': 30}]})
                self.assertKept(self.plan(cfg), path, 'symbolic link')
        self.assertTrue(os.path.exists(os.path.join(real, 'old.log')))

class RealProcesses(Case):
    """The real process list (/proc), not the fixture's fake one: the cleaner's own open file must not count as a
    use, and a file another process really holds open must be kept."""

    def hold(self, path):
        """A child process that holds `path` open (as its stdin) and names nothing else."""
        with open(path, 'rb') as f:
            child = subprocess.Popen(['sleep', '300'], stdin=f, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(os.readlink(f'/proc/{child.pid}/fd/0'), path)
        return child

    def test_with_the_real_process_list_a_cold_file_is_compressed_and_an_open_one_kept(self):
        cold, held = self.big('old.jsonl'), self.big('held.jsonl')
        self.hold(held)
        code, lines = self.run_cleaner(self.cold_cfg(), processes=self.c.facts.read_processes)
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.files(), ['held.jsonl', 'old.jsonl.zst'])
        self.assertEqual(self.unpack(cold + '.zst'), text())
        self.assertIn(f'kept {held} (compress-cold: in use: a process has it open or names it)', lines)
        self.assertTrue(any(line.startswith(f'compressed {cold} (') for line in lines), lines)

    def test_a_file_a_real_process_opens_while_it_is_compressed_stays(self):
        path = self.big('old.jsonl')
        real = self.c.compress._write_copy

        def then_opened(*args):
            out = real(*args)
            self.hold(path)
            return out
        with mock.patch.object(self.c.compress, '_write_copy', then_opened):
            code, lines = self.run_cleaner(self.cold_cfg(), processes=self.c.facts.read_processes)
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {path} ') and 'in use' in line for line in lines), lines)
        self.assertEqual(self.files(), ['old.jsonl'])


class Names(Case):
    """The copy never replaces a file, and the name checks right before the original goes."""

    def test_a_name_that_appears_while_the_copy_is_written_is_never_replaced(self):
        path = self.big('old.jsonl')
        real = self.c.compress._content

        def then_taken(fd, size):
            out = real(fd, size)
            if not os.path.exists(path + '.zst'):
                self.file(path + '.zst', 'someone else\n')
            return out
        with mock.patch.object(self.c.compress, '_content', then_taken):
            code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {path} ') and 'appeared while the copy was written' in line
                            for line in lines), lines)
        self.assertEqual(self.files(), ['old.jsonl', 'old.jsonl.zst'])
        with open(path + '.zst') as f:
            self.assertEqual(f.read(), 'someone else\n')
        with open(path) as f:
            self.assertEqual(f.read(), text())

    def test_a_copy_name_swapped_before_the_original_goes_keeps_the_original(self):
        path = self.big('old.jsonl')
        write, in_use, written = self.c.compress._write_copy, self.c.compress._in_use, []

        def copied(*args):
            written.append(write(*args))
            return written[-1]

        def then_swapped(*args):
            out = in_use(*args)
            if written:                         # the last check of the compress: an editor saves onto the name
                other = self.file(self.path('saved.zst'), 'an atomic save\n')
                os.rename(other, path + '.zst')
            return out
        with mock.patch.object(self.c.compress, '_write_copy', copied), \
                mock.patch.object(self.c.compress, '_in_use', then_swapped):
            code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {path} ') and 'not the copy this run checked' in line
                            for line in lines), lines)
        self.assertEqual(self.files(), ['old.jsonl', 'old.jsonl.zst'])
        with open(path + '.zst') as f:
            self.assertEqual(f.read(), 'an atomic save\n')
        with open(path) as f:
            self.assertEqual(f.read(), text())

    def test_an_original_saved_over_before_it_goes_stays(self):
        path = self.big('old.jsonl')
        write, in_use, written = self.c.compress._write_copy, self.c.compress._in_use, []

        def copied(*args):
            written.append(write(*args))
            return written[-1]

        def then_saved(*args):
            out = in_use(*args)
            if written:                         # the last check of the compress: an editor saves onto the original
                other = self.file(self.path('saved.jsonl'), 'NEW CONTENT the user just saved\n')
                os.rename(other, path)
            return out
        with mock.patch.object(self.c.compress, '_write_copy', copied), \
                mock.patch.object(self.c.compress, '_in_use', then_saved):
            code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {path} ') and 'changed while it was compressed' in line
                            for line in lines), lines)
        self.assertEqual(self.files(), ['old.jsonl'])
        with open(path) as f:
            self.assertEqual(f.read(), 'NEW CONTENT the user just saved\n')

    def test_a_copy_with_another_name_elsewhere_is_no_copy_of_this_rule(self):
        path = self.big('old.jsonl')
        self.pack(path, path + '.zst')
        os.link(path + '.zst', self.path('elsewhere.zst'))
        self.age(path, 40)
        self.assertKept(self.plan(self.cold_cfg()), path, 'name taken', 'no copy this rule made')
        self.assertEqual(self.run_cleaner(self.cold_cfg())[0], 0)
        self.assertEqual(self.files(), ['old.jsonl', 'old.jsonl.zst'])

    def test_a_copy_still_linked_to_its_part_after_a_crash_is_named_so_and_finished_later(self):
        path = self.big('old.jsonl')
        part = os.path.join(self.cold, '.old.jsonl.zst.0badc0de.cleaner-part')
        self.pack(path, part)
        os.link(part, path + '.zst')
        copy = os.stat(path + '.zst').st_ino
        self.age(path, 40)
        plan = self.plan(self.cold_cfg())
        self.assertKept(plan, path, 'name taken', 'the copy of a run that stopped')
        self.assertEqual(self.assertDeletes(plan, part).kind, 'part')
        self.assertEqual(self.run_cleaner(self.cold_cfg())[0], 0)
        self.assertEqual(self.files(), ['old.jsonl', 'old.jsonl.zst'])
        code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.files(), ['old.jsonl.zst'])
        self.assertEqual(os.stat(path + '.zst').st_ino, copy)
        self.assertEqual(self.unpack(path + '.zst'), text())

    def test_a_copy_of_another_owner_beside_it_is_no_copy_of_this_rule(self):
        path = self.big('old.jsonl')
        self.pack(path, path + '.zst')
        self.age(path, 40)
        ino, owner = os.stat(path + '.zst').st_ino, self.c.guard.owner
        with mock.patch.object(self.c.guard, 'owner', lambda st: 4242 if st.st_ino == ino else owner(st)):
            self.assertKept(self.plan(self.cold_cfg()), path, 'name taken', 'no copy this rule made')

    def test_a_part_file_of_another_owner_stays(self):
        self.big('old.jsonl')
        part = self.file(os.path.join(self.cold, '.old.jsonl.zst.0badc0de.cleaner-part'), 'half a copy', days=40)
        ino, owner = os.stat(part).st_ino, self.c.guard.owner
        with mock.patch.object(self.c.guard, 'owner', lambda st: 4242 if st.st_ino == ino else owner(st)):
            self.assertKept(self.plan(self.cold_cfg()), part, 'another owner')

    def test_an_original_that_cannot_be_removed_keeps_both_and_says_so(self):
        path = self.big('old.jsonl')
        unlink = os.unlink

        def refused(name, *args, dir_fd=None):
            if name == 'old.jsonl' and dir_fd is not None:
                raise PermissionError(1, 'Operation not permitted')
            return unlink(name, *args, dir_fd=dir_fd)
        with mock.patch.object(os, 'unlink', refused):
            code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {path} ') and 'both stay' in line for line in lines), lines)
        self.assertEqual(self.files(), ['old.jsonl', 'old.jsonl.zst'])


class Atime(Case):
    def mountinfo(self, options):
        info = self.file(self.path('mountinfo'), '22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n'
                         f'40 22 8:2 / {self.cold} rw,nosuid,{options} shared:2 - ext4 /dev/sdb1 rw\n')
        return lambda: self.c.facts.read_mounts(path=info, proc=None)

    def test_a_folder_on_a_noatime_filesystem_is_kept_whole(self):
        path = self.big('old.jsonl')
        mounts = self.mountinfo('noatime')
        self.assertEqual(mounts().options_of(path), frozenset({'rw', 'nosuid', 'noatime'}))
        self.assertEqual(mounts().options_of(self.home), frozenset({'rw', 'relatime'}))
        self.assertKept(self.plan(self.cold_cfg(), mounts=mounts), self.cold, 'noatime', 'cannot be known')
        code, lines = self.run_cleaner(self.cold_cfg(), mounts=mounts)
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.files(), ['old.jsonl'])

    def test_relatime_is_fine(self):
        path = self.big('old.jsonl')
        self.assertDeletes(self.plan(self.cold_cfg(), mounts=self.mountinfo('relatime')), path, 'compress-cold')


class Metadata(Case):
    """The copy keeps what the original had: group, mode, ACL and extended attributes; what it could not keep, or
    could never finish, is skipped in the plan instead of failing every run."""

    def xattr_or_skip(self, path, name, value):
        try:
            os.setxattr(path, name, value)
        except OSError as ex:
            self.skipTest(f'{name}: {ex.strerror}')

    def test_the_copy_keeps_the_group_acl_and_extended_attributes(self):
        path = self.big('old.jsonl')
        others = [g for g in os.getgroups() if g != os.getegid()]
        if others:
            os.chown(path, -1, others[0])
        os.chmod(path, 0o640)
        # an ACL that also lets uid 65534 read it: user::rw- user:65534:r-- group::r-- mask::r-- other::---
        undefined = 0xFFFFFFFF
        acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', tag, perm, uid) for tag, perm, uid in (
            (0x01, 6, undefined), (0x02, 4, 65534), (0x04, 4, undefined), (0x10, 4, undefined), (0x20, 0, undefined)))
        self.xattr_or_skip(path, 'system.posix_acl_access', acl)
        self.xattr_or_skip(path, 'user.origin', b'session of october')
        before = os.stat(path)
        want = {name: os.getxattr(path, name) for name in os.listxattr(path)}
        self.age(path, 40)
        code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 0, lines)
        copy = path + '.zst'
        after = os.stat(copy)
        self.assertEqual((after.st_gid, after.st_mode & 0o7777), (before.st_gid, before.st_mode & 0o7777))
        self.assertEqual({name: os.getxattr(copy, name) for name in os.listxattr(copy)}, want)
        if not others:
            self.skipTest('the user has one group: the group copy is not shown')

    def test_a_read_only_file_keeps_its_attributes(self):
        path = self.big('old.jsonl')
        self.xattr_or_skip(path, 'user.origin', b'session of october')
        os.chmod(path, 0o444)
        self.age(path, 40)
        code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 0, lines)
        copy = path + '.zst'
        self.assertEqual(self.files(), ['old.jsonl.zst'])
        self.assertEqual(os.stat(copy).st_mode & 0o7777, 0o444)
        self.assertEqual(os.getxattr(copy, 'user.origin'), b'session of october')

    def test_the_copy_gets_no_acl_the_original_lacks(self):
        path = self.big('old.jsonl')
        os.chmod(path, 0o640)
        # set after the file was made, as on a folder a file was moved into: user::rw- user:65534:rw- group::r--
        # mask::rw- other::---; a new file in the folder gets it as its ACL
        undefined = 0xFFFFFFFF
        acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', tag, perm, uid) for tag, perm, uid in (
            (0x01, 6, undefined), (0x02, 6, 65534), (0x04, 4, undefined), (0x10, 6, undefined), (0x20, 0, undefined)))
        self.xattr_or_skip(self.cold, 'system.posix_acl_default', acl)
        probe = self.file(os.path.join(self.cold, 'probe'))
        inherited = 'system.posix_acl_access' in os.listxattr(probe)
        os.unlink(probe)
        if not inherited:
            self.skipTest('a default ACL is not given to new files here')
        self.assertEqual(os.listxattr(path), [])
        self.age(path, 40)
        code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 0, lines)
        copy = path + '.zst'
        self.assertEqual(self.files(), ['old.jsonl.zst'])
        self.assertEqual(os.listxattr(copy), [])
        self.assertEqual(os.stat(copy).st_mode & 0o7777, 0o640)

    def test_what_a_copy_could_not_keep_is_skipped(self):
        foreign, tagged = self.big('foreign.log'), self.big('tagged.log')
        self.xattr_or_skip(tagged, 'user.origin', b'x')
        gid = os.stat(foreign).st_gid
        with mock.patch.object(os, 'getegid', lambda: gid + 1), mock.patch.object(os, 'getgroups', lambda: []):
            self.assertKept(self.plan(self.cold_cfg()), foreign, 'group not kept')
        with mock.patch.object(self.c.compress, 'COPYABLE_XATTRS', ('system.posix_acl_access',)):
            plan = self.plan(self.cold_cfg())
        self.assertKept(plan, tagged, 'attributes not kept', 'user.origin')
        self.assertDeletes(plan, foreign, 'compress-cold')

    def test_files_it_could_never_finish_are_skipped_not_failed(self):
        unreadable = self.big('unreadable.log')
        os.chmod(unreadable, 0o200)
        frozen, appended = self.big('frozen.log'), self.big('appended', 'a.log')
        flags = self.c.compress.flags
        stuck = {frozen: self.c.compress.FS_IMMUTABLE_FL, os.path.dirname(appended): self.c.compress.FS_APPEND_FL}
        with mock.patch.object(self.c.compress, 'flags', lambda p: stuck.get(p) or flags(p)):
            plan = self.plan(self.cold_cfg())
            self.assertKept(plan, unreadable, 'unreadable')
            self.assertKept(plan, frozen, 'immutable')
            self.assertKept(plan, os.path.dirname(appended), 'append-only')
            code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual(code, 0, lines)
        self.assertFalse([name for name in self.files() if name.endswith('.zst')])


class Disk(Case):
    def test_the_copy_is_read_back_from_the_disk_not_the_page_cache(self):
        path = self.big('old.jsonl')
        real, dropped = os.posix_fadvise, []

        def fadvise(fd, offset, length, advice):
            dropped.append((os.fstat(fd).st_ino, advice))
            return real(fd, offset, length, advice)
        with mock.patch.object(os, 'posix_fadvise', fadvise):
            self.assertEqual(self.run_cleaner(self.cold_cfg())[0], 0)
        self.assertIn((os.stat(path + '.zst').st_ino, os.POSIX_FADV_DONTNEED), dropped)

    def test_a_zstd_that_hangs_is_stopped_and_the_original_stays(self):
        path = self.big('old.jsonl')
        self.zstd_stub(compress='hang', hang=5)
        with mock.patch.object(self.c.compress, 'deadline', lambda size: 0.5):
            started = time.monotonic()
            code, lines = self.run_cleaner(self.cold_cfg())
            took = time.monotonic() - started
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith(f'FAILED {path} ') and 'took longer than' in line for line in lines), lines)
        self.assertLess(took, 4)
        self.assertEqual(self.files(), ['old.jsonl'])
        with open(path) as f:
            self.assertEqual(f.read(), text())

    def test_the_deadline_grows_with_the_file(self):
        self.assertGreaterEqual(self.c.compress.deadline(0), 600)
        self.assertGreater(self.c.compress.deadline(100 * GB), self.c.compress.deadline(GB) + 90000)


class Report(Case):
    def test_the_report_counts_what_compressing_freed(self):
        self.big('old.jsonl')
        cfg = self.cold_cfg()
        self.assertEqual(self.run_cleaner(cfg)[0], 0)
        got = self.c.report.freed(cfg)
        self.assertEqual((got.deletes, got.compresses, got.skipped), (0, 1, 0))
        self.assertGreater(got.bytes, 0)

    def test_the_report_sizes_the_cold_folders(self):
        self.big('old.jsonl')
        sized = self.c.report.measure(self.cold_cfg(), self.c.report.Sizer(), 'cold folders', 'rule:compress-cold', None)
        self.assertGreater(sized.total, 0)
        sized = self.c.report.measure(self.cfg(), self.c.report.Sizer(), 'cold folders', 'rule:compress-cold', None)
        self.assertEqual(sized.total, 0)


class Config(Case):
    def test_a_machine_file_without_the_keys_compresses_nothing(self):
        self.big('old.jsonl')
        cfg = self.cfg()
        self.assertEqual(cfg.cold_folders, ())
        plan = self.plan(cfg)
        self.assertFalse([x for x in plan.items + plan.kept if x.rule == 'compress-cold'])
        self.assertEqual(self.run_cleaner(cfg)[0], 0)
        self.assertEqual(self.files(), ['old.jsonl'])
        self.assertEqual(self.cfg(compressCold={}).cold_folders, ())

    def test_folders_it_refuses(self):
        home = self.home
        for path in ('/', home, os.path.dirname(home), '~/.claude', '~/.claude/projects', '~/.claude-2/projects',
                     '~/.codex', '~/.t3/userdata', '~/.t3', self.tmp, os.path.join(self.tmp, 'cold'),
                     self.path('state'), self.path('state', 'daily', 'x')):
            section = {'tmp': {'root': self.tmp, 'patterns': {'*': 7}}}
            with self.subTest(path=path), self.assertRaises(machine_file.MachineFileError):
                self.cfg(compressCold={'folders': [{'path': path, 'days': 30}]}, **section)
        for days in (0, 6, '30'):
            with self.subTest(days=days), self.assertRaises(machine_file.MachineFileError):
                self.cfg(compressCold={'folders': [{'path': self.cold, 'days': days}]})
        with self.assertRaises(machine_file.MachineFileError):
            self.cfg(compressCold={'folders': [{'path': self.cold, 'days': 30}, {'path': os.path.join(self.cold, 'x'), 'days': 9}]})
        with self.assertRaises(machine_file.MachineFileError):
            self.cfg(compressCold={'folders': [{'path': self.cold}]})
        with self.assertRaises(machine_file.MachineFileError):
            self.cfg(compressCold={'keepFreeGB': 0})

    def test_never_where_agent_scratch_deletes_or_keeps_first(self):
        """While agent-scratch is on, its root is a folder another rule deletes from and its keep-first archive is
        one of the cleaner's own files: compress-cold takes neither, nor a folder inside or above one. Off, both may
        be named."""
        scratch = self.mkdir('scratch')
        archive = self.path('data', 'kept')
        on = {'enabled': True, 'root': scratch, 'keepFirst': {'archive': archive}}
        for path, why in ((scratch, 'another rule deletes from'), (os.path.join(scratch, 'project'), 'another rule deletes from'),
                          (archive, 'agentScratch.keepFirst.archive'), (os.path.join(archive, 'x'), 'agentScratch.keepFirst.archive'),
                          (self.path('data'), 'agentScratch.keepFirst.archive')):
            with self.subTest(path=path), self.assertRaises(machine_file.MachineFileError) as raised:
                self.cfg(compressCold={'folders': [{'path': path, 'days': 30}]}, agentScratch=on)
            self.assertIn(why, str(raised.exception))
        off = {**on, 'enabled': False}
        for path in (scratch, archive):
            cfg = self.cfg(compressCold={'folders': [{'path': path, 'days': 30}]}, agentScratch=off)
            self.assertEqual([f.path for f in cfg.cold_folders], [path])

    def test_a_claude_home_that_is_a_link_is_never_taken_where_it_leads(self):
        real = self.mkdir('data', 'claude-real', 'projects')
        os.symlink(os.path.dirname(real), os.path.join(self.home, '.claude'))
        os.symlink(self.path('data'), self.path('via-data'))
        for path in (real, os.path.dirname(real), self.path('data'), self.path('via-data', 'claude-real', 'projects')):
            with self.subTest(path=path), self.assertRaises(machine_file.MachineFileError) as raised:
                self.cfg(compressCold={'folders': [{'path': path, 'days': 30}]})
            self.assertIn('a link to one leads', str(raised.exception))
        other = self.mkdir('data', 'other')
        self.assertEqual([f.path for f in self.cfg(compressCold={'folders': [{'path': other, 'days': 30}]}).cold_folders],
                         [other])

    def test_a_log_archive_is_never_taken(self):
        for path in (self.path('data', 'log-archive'), self.path('data', 'Old-Log-Archive', 'x')):
            with self.subTest(path=path), self.assertRaises(machine_file.MachineFileError) as raised:
                self.cfg(compressCold={'folders': [{'path': path, 'days': 30}]})
            self.assertIn('log archive', str(raised.exception))
        inside = self.big('log-archive', 'old.log')
        self.assertKept(self.plan(self.cold_cfg()), os.path.dirname(inside), 'log archive')

    def test_the_example_in_the_readme_is_a_config(self):
        with open(os.path.join(fx.KIT, 'README.md')) as f:
            example = re.search(r'```jsonc\n// in the "cleaner" section\n(.*?)```', f.read(), re.S).group(1)
        cfg = self.c.config.Config(json.loads('{' + example + '}'), home='/srv/example-home')
        self.assertEqual([(f.path, f.days) for f in cfg.cold_folders],
                         [('/srv/example-home/.codex/sessions', 30), ('/srv/example-home/.t3/repair-backups', 14)])
        self.assertNotIn('/home/', example)

    def test_folders_inside_must_keep_paths_may_be_named(self):
        cfg = self.cfg(compressCold={'folders': [{'path': '~/.codex/sessions', 'days': 30},
                                                 {'path': '~/.t3/repair-backups', 'days': 14},
                                                 {'path': '~/.t3/userdata/logs', 'days': 7}]})
        self.assertEqual([(f.path, f.days) for f in cfg.cold_folders],
                         [(os.path.join(self.home, '.codex', 'sessions'), 30), (os.path.join(self.home, '.t3', 'repair-backups'), 14),
                          (os.path.join(self.home, '.t3', 'userdata', 'logs'), 7)])
        self.assertEqual(cfg.keep_free, 5 * GB)


MUTATIONS = {
    'cold: a read counts': ('compress', 'return max(guard.changed(st), st.st_atime)', 'return guard.changed(st)',
                            Compress, 'test_files_changed_or_read_lately_stay'),
    'cold: days': ('compress', 'if age < folder.days * DAY:', 'if False:', Compress, 'test_files_changed_or_read_lately_stay'),
    'the plan reads without an access time': ("compress", "| getattr(os, 'O_NOATIME', 0)", '',
                                              Compress, 'test_the_plan_changes_nothing_reads_nothing_and_says_what_it_frees'),
    'compressed files are skipped': ('compress', 'if matches(name, DONE) or name.endswith(PART)', 'if name.endswith(PART)',
                                     Compress, 'test_a_second_run_finds_nothing_to_do'),
    'the copy is read back': ('compress', 'if got != want:', 'if False:', Refused, 'test_a_copy_that_does_not_read_back_keeps_the_original'),
    'zstd -t': ("compress", "_Step(['-q', '-t'], fd, subprocess.DEVNULL, deadline(size), 'test (-t)').finish()",
                'pass', Refused, 'test_a_copy_that_does_not_read_back_keeps_the_original'),
    'a failed zstd fails': ('compress', 'if code != 0:', 'if False:', Refused, 'test_a_full_disk_while_writing_keeps_the_original'),
    'a part is removed': ("compress", "if original in names or original + '.zst' in names:", 'if False:',
                          HalfWay, 'test_a_half_written_copy_is_removed_and_the_file_compressed_again'),
    'a part is removed after every step': ('compress', "            os.unlink(part, dir_fd=parent)\n", '            pass\n',
                                           HalfWay, 'test_a_step_that_stops_leaves_the_original_and_no_part'),
    'a copy beside it is checked': ('compress', 'if _content(fd, size) != _hash(src):', 'if False:', HalfWay,
                                    'test_a_different_copy_beside_it_keeps_both'),
    'in use': ('compress', 'if processes.uses(path):', 'if False:', InUse, 'test_files_in_use_stay'),
    'in use: a unit names a folder above': ('compress', 'if here in units:', 'if False:', InUse, 'test_files_in_use_stay'),
    'in use: a lock': ('compress', 'if st.st_ino in processes.locks:', 'if False:', InUse, 'test_files_in_use_stay'),
    'opened while it is compressed': ('compress', 'if refusal is not None or now is None', 'if now is None',
                                      InUse, 'test_a_file_opened_while_it_is_compressed_stays_and_its_copy_goes'),
    'the cleaner closes the original before the last check': (
        'compress', "            # Closed before the last look at the process list: the cleaner's own descriptor would count as a use.\n"
                    "            os.close(src)\n", '            pass\n',
        RealProcesses, 'test_with_the_real_process_list_a_cold_file_is_compressed_and_an_open_one_kept'),
    'the last check with the real process list': ('compress', 'if refusal is not None or now is None', 'if now is None',
                                                  RealProcesses, 'test_a_file_a_real_process_opens_while_it_is_compressed_stays'),
    'a copy never replaces a file (link, not rename)': (
        'compress', 'os.link(part, target, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)',
        'os.rename(part, target, src_dir_fd=parent, dst_dir_fd=parent)',
        Names, 'test_a_name_that_appears_while_the_copy_is_written_is_never_replaced'),
    'both names checked just before the original goes': ('compress', ' or named != copy:', ':',
                                                          Names, 'test_a_copy_name_swapped_before_the_original_goes_keeps_the_original'),
    'the original checked by name just before it goes': ('compress', ' or not _same(now, item.look) or named', ' or named',
                                                         Names, 'test_an_original_saved_over_before_it_goes_stays'),
    'a twin of another owner is no copy of this rule': ('compress', 'or st.st_nlink != 1 or guard.owner(st) != os.getuid():',
                                                        'or st.st_nlink != 1:', Names,
                                                        'test_a_copy_of_another_owner_beside_it_is_no_copy_of_this_rule'),
    'a twin must have one name': ('compress', 'or st.st_nlink != 1 or guard.owner(st)', 'or guard.owner(st)',
                                  Names, 'test_a_copy_with_another_name_elsewhere_is_no_copy_of_this_rule'),
    'a twin still linked to its part is named so': ('compress', 'and _linked_part(path, st):', 'and False:',
                                                    Names, 'test_a_copy_still_linked_to_its_part_after_a_crash_is_named_so_and_finished_later'),
    'a part file of another owner stays': ("compress", "        if guard.owner(st) != os.getuid():\n            return None, Kept(path, RULE, 'another owner'",
                                           "        if False:\n            return None, Kept(path, RULE, 'another owner'",
                                           Names, 'test_a_part_file_of_another_owner_stays'),
    'noatime': ('compress', "if 'noatime' in options:", 'if False:', Atime, 'test_a_folder_on_a_noatime_filesystem_is_kept_whole'),
    'mount options are read': ('facts', "options[point] = fields[5].split(',')", 'pass',
                               Atime, 'test_a_folder_on_a_noatime_filesystem_is_kept_whole'),
    'the copy keeps the group': ('compress', 'os.fchown(dst, -1, st.st_gid)', 'pass',
                                 Metadata, 'test_the_copy_keeps_the_group_acl_and_extended_attributes'),
    'the copy keeps the ACL and attributes': ('compress', 'os.setxattr(dst, attr, os.getxattr(src, attr))', 'pass',
                                              Metadata, 'test_the_copy_keeps_the_group_acl_and_extended_attributes'),
    'the copy is given no attribute the original lacks': ('compress', '            _drop_xattr(dst, attr)\n', '            pass\n',
                                                          Metadata, 'test_the_copy_gets_no_acl_the_original_lacks'),
    'the mode is set after the attributes': (
        'compress', "    for attr in want:\n        os.setxattr(dst, attr, os.getxattr(src, attr))\n    os.fchmod(dst, stat.S_IMODE(st.st_mode))\n",
        "    os.fchmod(dst, stat.S_IMODE(st.st_mode))\n    for attr in want:\n        os.setxattr(dst, attr, os.getxattr(src, attr))\n",
        Metadata, 'test_a_read_only_file_keeps_its_attributes'),
    'a group the copy could not keep': ("compress", "if st.st_gid != os.getegid() and st.st_gid not in os.getgroups():", 'if False:',
                                        Metadata, 'test_what_a_copy_could_not_keep_is_skipped'),
    'attributes the copy could not keep': ('compress', 'if other:', 'if False:', Metadata, 'test_what_a_copy_could_not_keep_is_skipped'),
    'unreadable files are skipped': ('compress', 'if not st.st_mode & stat.S_IRUSR:', 'if False:',
                                     Metadata, 'test_files_it_could_never_finish_are_skipped_not_failed'),
    'immutable and append-only are skipped': ('compress', 'if got & flag:', 'if False:',
                                              Metadata, 'test_files_it_could_never_finish_are_skipped_not_failed'),
    'read back from the disk': ('compress', 'os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)', 'pass',
                                Disk, 'test_the_copy_is_read_back_from_the_disk_not_the_page_cache'),
    'a hung zstd is stopped': ('compress', '        self.timer.start()\n', '        pass\n',
                               Disk, 'test_a_zstd_that_hangs_is_stopped_and_the_original_stays'),
    'a log archive inside is not entered': ('compress', 'if matches(os.path.basename(here).lower(), COMPRESS_NEVER_NAMES):', 'if False:',
                                            Config, 'test_a_log_archive_is_never_taken'),
    'config: never a log archive': ('config', 'if any(matches(part.lower(), COMPRESS_NEVER_NAMES)', 'if False and any(matches(part.lower(), COMPRESS_NEVER_NAMES)',
                                    Config, 'test_a_log_archive_is_never_taken'),
    'config: where a Claude home link leads': ('config', ' + [os.path.realpath(f) for f in found]', '',
                                               Config, 'test_a_claude_home_that_is_a_link_is_never_taken_where_it_leads'),
    'config: a folder named through a link': ('config', ' or must_keep_match(never, real) is not None', '',
                                              Config, 'test_a_claude_home_that_is_a_link_is_never_taken_where_it_leads'),
    'changed while it is compressed': ('compress', 'def _same(st, look):\n    return', 'def _same(st, look):\n    return True or',
                                       InUse, 'test_a_file_changed_while_it_is_compressed_stays'),
    'the last check': ("rules", "if item.kind in ('compress', 'part'):\n        return compress.recheck(cfg, facts, item)", 'pass',
                       InUse, 'test_a_file_in_use_at_the_last_check_is_refused'),
    'disk nearly full: plan': ('compress', 'elif free is not None and free < st.st_size + cfg.keep_free:', 'elif False:',
                               Refused, 'test_a_disk_nearly_full_keeps_the_original'),
    'disk nearly full: compressing': ('compress', 'if free < st.st_size + cfg.keep_free:', 'if False:',
                                      Refused, 'test_compressing_refuses_when_the_disk_filled_up_since_the_check'),
    'zstd missing': ('compress', 'if refusal is None and st is not None and program is None:', 'if False:',
                     Refused, 'test_without_zstd_nothing_is_compressed_and_the_run_says_so'),
    'KEEP': ('compress', 'if KEEP_FILE in names:', 'if False:', Refused, 'test_what_it_never_takes'),
    'git folders': ('compress', "if '.git' in names or {'HEAD', 'objects', 'refs'} <= set(names):", 'if False:',
                    Refused, 'test_what_it_never_takes'),
    'databases': ('compress', 'if name.endswith(SIDECARS) or any(name + s in names for s in SIDECARS):', 'if False:',
                  Refused, 'test_what_it_never_takes'),
    'hard links': ('compress', 'if st.st_nlink > 1:', 'if False:', Refused, 'test_what_it_never_takes'),
    'credential names': ('compress', "if matches(name, CREDENTIAL_NAMES):\n        return Refusal('credential name', name)",
                         'pass', Refused, 'test_what_it_never_takes'),
    'a cold folder through a link': ('compress', 'if os.path.realpath(path) != path:', 'if False:',
                                     Refused, 'test_a_cold_folder_reached_through_a_link_is_kept'),
    'config: never the Claude homes': ('config', 'if must_keep_match(never, path) is not None or', 'if False and',
                                       Config, 'test_folders_it_refuses'),
    'config: never a must-keep path whole': ('config', 'if held:', 'if False:', Config, 'test_folders_it_refuses'),
    'config: no overlap with another rule': ('config', "raise machine_file.MachineFileError(f'{at}.path: {text} holds or is inside {root}, a folder '\n"
                                             "                                                        'another rule deletes from')", 'pass',
                                             Config, 'test_folders_it_refuses'),
    'config: never the agent-scratch root': ('config', "for root in self.roots() | ({self.scratch_root} if self.scratch_enabled else set()):\n                if _holds(path, root)",
                                             "for root in self.roots():\n                if _holds(path, root)",
                                             Config, 'test_never_where_agent_scratch_deletes_or_keeps_first'),
    'config: never the keep-first archive': ('config', "('daily.outDir', out_text, self.daily_dir)) + archive:",
                                             "('daily.outDir', out_text, self.daily_dir)):",
                                             Config, 'test_never_where_agent_scratch_deletes_or_keeps_first'),
    'the report counts compressing': ("report", "if verb == 'compressed':", 'if False:', Report, 'test_the_report_counts_what_compressing_freed'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
