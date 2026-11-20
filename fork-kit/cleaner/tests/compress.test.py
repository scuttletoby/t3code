"""The `compress-cold` rule (lib/cleaner/compress.py) and its keys (config.py). Every file lives in the test's own
temporary folder; zstd is the real one, or a stub in front of it that fails one step. The state cases: a second run,
a run stopped half-way (a part file, or a copy linked and the original still there), a file in use or opened or
changed while it is compressed, a disk nearly full, zstd missing or failing, and a machine file without the keys.
RedCheck breaks each rule in a copy of the code and shows its test then fails.

  python3 -I -B fork-kit/cleaner/tests/compress.test.py
"""
import errno, json, os, re, shutil, struct, subprocess, sys, time, unittest
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


class Above(Case):
    """A KEEP entry or a git folder in a folder above the cold folder, up to `/` and on its own filesystem behind a
    bind mount, keeps it whole, as one in it does (#578, #604). Read at the plan, at the last check and once more
    after the copy is written. The state cases: a second run, a run stopped half-way before the marker came, a
    marker made between the plan and the run or while the copy is written, and a marker that is gone, empty, a
    folder or cannot be read; a mount list that does not show the folders above."""

    def nested(self, *parts):
        """A cold folder at `parts` with one cold file in it: (folder, file, config)."""
        cold = self.mkdir(*parts)
        path = self.file(os.path.join(cold, 'old.jsonl'), text(), 40)
        return cold, path, self.cfg(compressCold={'folders': [{'path': cold, 'days': 30}], 'keepFreeGB': 1})

    def assertUntouched(self, cfg, path):
        code, lines = self.run_cleaner(cfg)
        self.assertFalse(os.path.exists(path + '.zst'), ('compressed under a marker', lines))
        with open(path) as f:
            self.assertEqual(f.read(), text())
        return code, lines

    def test_folder_inside_a_git_work_tree(self):
        os.makedirs(self.path('repo', '.git'))
        cold, path, cfg = self.nested('repo', 'logs', 'sessions')
        self.assertKept(self.plan(cfg), cold, 'git folder', f'{self.path("repo")}, above {cold}')
        code, lines = self.assertUntouched(cfg, path)
        self.assertEqual(code, 0, lines)

    def test_folder_inside_a_linked_worktree(self):
        self.file(self.path('wt', '.git'), 'gitdir: /elsewhere/.git/worktrees/wt\n')
        cold, path, cfg = self.nested('wt', 'sessions')
        self.assertKept(self.plan(cfg), cold, 'git folder')
        self.assertUntouched(cfg, path)

    def test_folder_inside_a_bare_repository(self):
        for name in ('objects', 'refs'):
            os.makedirs(self.path('repo.git', name))
        self.file(self.path('repo.git', 'HEAD'), 'ref: refs/heads/main\n')
        cold, path, cfg = self.nested('repo.git', 'objects', 'info')
        self.assertKept(self.plan(cfg), cold, 'git folder')
        self.assertUntouched(cfg, path)

    def test_folder_under_a_keep_file(self):
        self.file(self.path('KEEP'), 'audit copies\n')
        path = self.big('sessions', 'old.jsonl')
        cfg = self.cold_cfg()
        self.assertKept(self.plan(cfg), self.cold, 'KEEP file', f'audit copies ({self.dir}, above {self.cold})')
        code, lines = self.assertUntouched(cfg, path)
        self.assertEqual(code, 0, lines)

    def test_an_empty_keep_file_above_keeps_it_too(self):
        self.file(self.path('KEEP'), '')
        path = self.big('old.jsonl')
        self.assertKept(self.plan(self.cold_cfg()), self.cold, 'KEEP file', 'no reason given')
        self.assertUntouched(self.cold_cfg(), path)

    def test_a_keep_folder_above_keeps_it_as_one_below_does(self):
        """#604: a folder named KEEP counts the same above the cold folder and in it."""
        cold, below, cfg = self.nested('below', 'cold', 'sub')
        os.mkdir(os.path.join(cold, 'KEEP'))
        cfg = self.cfg(compressCold={'folders': [{'path': os.path.dirname(cold), 'days': 30}], 'keepFreeGB': 1})
        self.assertKept(self.plan(cfg), cold, 'KEEP file', 'a folder named KEEP')
        self.assertUntouched(cfg, below)
        cold, path, cfg = self.nested('KEEP', 'cold')        # self.dir/KEEP: above everything after this
        self.assertKept(self.plan(cfg), cold, 'KEEP file', f'a folder named KEEP ({self.dir}, above {cold})')
        self.assertUntouched(cfg, path)

    def test_a_marker_that_cannot_be_read_keeps_it(self):
        """#604: a KEEP, `.git`, HEAD, objects or refs that cannot be looked up is not "not there", above the cold
        folder or in it. The other names of a bare repository are there, so each one is really looked up."""
        bare = ('HEAD', 'objects', 'refs')
        for where in ('above', 'below'):
            for name in ('KEEP', '.git') + bare:
                with self.subTest(where=where, name=name):
                    if where == 'above':
                        cold, path, cfg = self.nested(f'{where}{name}', 'cold')
                        folder = os.path.dirname(cold)
                    else:
                        cold, path, cfg = self.nested(f'{where}{name}', 'cold', 'sub')
                        cold, folder = os.path.dirname(cold), cold
                        cfg = self.cfg(compressCold={'folders': [{'path': cold, 'days': 30}], 'keepFreeGB': 1})
                    for other in bare if name in bare else ():
                        if other != name:
                            os.mkdir(os.path.join(folder, other))
                    target, real = os.path.join(folder, name), os.lstat

                    def lstat(at, *args, **kwargs):
                        if at == target:
                            raise PermissionError(errno.EACCES, 'Permission denied', at)
                        return real(at, *args, **kwargs)
                    with mock.patch.object(os, 'lstat', lstat):
                        self.assertKept(self.plan(cfg), cold if where == 'above' else folder, 'could not read', folder)
                        self.assertUntouched(cfg, path)

    def test_a_bare_repository_needs_all_three_names(self):
        for missing in ('HEAD', 'objects', 'refs'):
            with self.subTest(missing=missing):
                cold, path, cfg = self.nested(f'no{missing}', 'cold')
                for name in {'HEAD', 'objects', 'refs'} - {missing}:
                    os.mkdir(self.path(f'no{missing}', name))
                code, lines = self.run_cleaner(cfg)
                self.assertEqual(code, 0, lines)
                self.assertFalse(os.path.exists(path), lines)
                self.assertEqual(self.unpack(path + '.zst'), text())

    def test_a_folder_that_folds_case_counts_keep_above_and_below(self):
        """#604: on a folder that folds case (ext4 casefold, a vfat or SMB mount) `keep` is KEEP. Each name is
        looked up, above the cold folder and in it, so both see it; here lstat folds case as such a folder does."""
        real = os.lstat

        def lstat(at, *args, **kwargs):
            folded = os.path.join(os.path.dirname(at), os.path.basename(at).lower())
            if os.path.basename(at) == 'KEEP' and folded != at and os.path.lexists(folded):
                return real(folded, *args, **kwargs)
            return real(at, *args, **kwargs)
        cold, above, cfg = self.nested('folds', 'cold')
        self.file(self.path('folds', 'keep'), 'lower case\n')
        below = self.big('sub', 'old.jsonl')
        self.file(os.path.join(self.cold, 'sub', 'keep'), 'lower case\n')
        with mock.patch.object(os, 'lstat', lstat):
            self.assertKept(self.plan(cfg), cold, 'KEEP file', f'({self.path("folds")}, above {cold})')
            self.assertUntouched(cfg, above)
            self.assertKept(self.plan(self.cold_cfg()), os.path.dirname(below), 'KEEP file')
            self.assertUntouched(self.cold_cfg(), below)

    def test_a_marker_made_while_the_copy_is_written_keeps_the_original(self):
        """#604: the folders are read once more after the copy is written, before the original goes: the copy is
        removed and the original stays. A KEEP file above the cold folder, a `.git` in it."""
        for marker in ('KEEP', '.git'):
            with self.subTest(marker=marker):
                cold, path, cfg = self.nested(f'copy{marker}', 'cold')
                at = self.path(f'copy{marker}', 'KEEP') if marker == 'KEEP' else os.path.join(cold, '.git')
                real = self.c.compress._write_copy

                def write_copy(*args):
                    got = real(*args)
                    self.file(at, 'made while it was compressed\n')
                    return got
                with mock.patch.object(self.c.compress, '_write_copy', write_copy):
                    code, lines = self.assertUntouched(cfg, path)
                self.assertEqual(sorted(os.listdir(cold)), sorted(['old.jsonl'] + (['.git'] if marker == '.git' else [])))
                self.assertTrue(any('the original stays, its copy was removed' in line for line in lines), lines)
                self.assertTrue(any('KEEP file' in line or 'git folder' in line for line in lines), lines)
                os.remove(at)
                self.age(path, 40)
                code, lines = self.run_cleaner(cfg)          # runs again once it is gone
                self.assertEqual((code, sorted(os.listdir(cold))), (0, ['old.jsonl.zst']), lines)

    # --- a cold folder reached through a bind mount (#604) ----------------------------------------------------------
    def view(self, name, root='/data', whole=False, fs=True, device='8:9'):
        """A cold folder `<name>/view/cold` reached through a mount that shows the folder `root` of the filesystem
        `device`; with `fs`, `<name>/fs` shows that whole filesystem (its folder /), as a second mount of a disk
        does. `whole`: that mount shows a whole btrfs subvolume. (fs or None, cold folder, file, config)"""
        top = self.mkdir(name)
        lines = [(os.path.join(top, 'view'), device, root)]
        if fs:
            lines.insert(0, (self.mkdir(name, 'fs'), device, '/'))
        self.mount_lines += lines
        if whole:
            self.mount_whole.add((device, root))
        cold, path, cfg = self.nested(name, 'view', 'cold')
        return (os.path.join(top, 'fs') if fs else None), cold, path, cfg

    def test_a_marker_above_a_bind_mount_on_its_own_filesystem_keeps_it(self):
        """The folder above the one the mount shows is read where another mount shows it: a `.git` there is a work
        tree git still sees through that mount, a KEEP file there keeps everything under it."""
        for n, marker in enumerate(('.git', 'KEEP', 'bare')):
            with self.subTest(marker=marker):
                fs, cold, path, cfg = self.view(f'bind{marker}', device=f'8:{20 + n}')
                if marker == 'bare':
                    for name in ('HEAD', 'objects', 'refs'):
                        os.mkdir(os.path.join(fs, name))
                else:
                    self.file(os.path.join(fs, marker), 'the whole disk\n')
                if marker == 'KEEP':
                    self.assertKept(self.plan(cfg), cold, 'KEEP file', f'the whole disk ({fs}, above {cold})')
                else:
                    self.assertKept(self.plan(cfg), cold, 'git folder', f'{fs}, above {cold}')
                code, lines = self.assertUntouched(cfg, path)
                self.assertEqual(code, 0, lines)

    def test_a_bind_mount_with_nothing_above_on_its_filesystem_compresses(self):
        fs, cold, path, cfg = self.view('clean')
        code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, sorted(os.listdir(cold))), (0, ['old.jsonl.zst']), lines)
        self.assertEqual(self.unpack(path + '.zst'), text())

    def test_a_bind_mount_whose_folder_above_no_mount_shows_keeps_it(self):
        """Not knowing is keeping: nothing here shows the folder above the one the mount shows."""
        _, cold, path, cfg = self.view('hidden', fs=False)
        self.assertKept(self.plan(cfg), cold, 'could not read', 'no mount here shows the folder above it (/)')
        self.assertUntouched(cfg, path)

    def test_a_view_another_mount_hides_is_not_used(self):
        fs, cold, path, cfg = self.view('shadowed')
        self.mount_lines.append((fs, '8:10', '/'))     # mounted over the view of the whole disk
        self.assertKept(self.plan(cfg), cold, 'could not read', 'no mount here shows the folder above it')
        self.assertUntouched(cfg, path)

    def test_a_whole_btrfs_subvolume_needs_no_view_above(self):
        """A btrfs subvolume mounted whole (`subvol=` is the mount's root) hides only the btrfs top folder, which
        holds the subvolumes, not a user's files: no view of it is needed. So btrfs systems are not refused whole,
        the usual root subvolume `/@` on `/` among them. A part of a subvolume is not whole: refused as above."""
        _, cold, path, cfg = self.view('subvol', root='/@data', whole=True, fs=False)
        code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, sorted(os.listdir(cold))), (0, ['old.jsonl.zst']), lines)
        _, cold, path, cfg = self.view('part', root='/@data/part', fs=False, device='8:11')
        self.mount_whole.add(('8:11', '/@data'))
        self.assertKept(self.plan(cfg), cold, 'could not read', 'no mount here shows the folder above it (/@data)')
        self.mount_lines[:] = [('/', '0:0', '/@')]
        self.mount_whole.clear()
        self.assertKept(self.plan(self.cold_cfg()), self.cold, 'could not read', '/ shows the folder /@ of its filesystem')
        self.mount_whole.add(('0:0', '/@'))
        path = self.big('old.jsonl')
        code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual((code, self.files()), (0, ['old.jsonl.zst']), lines)

    def test_views_are_followed_through_a_chain_of_mounts(self):
        """The view of the folder above can itself show only one folder: its own folder above is read in turn."""
        _, cold, path, cfg = self.view('chain', root='/a/b', fs=False)
        middle = self.mkdir('chain', 'middle')
        self.mount_lines.append((middle, '8:9', '/a'))
        self.assertKept(self.plan(cfg), cold, 'could not read', 'no mount here shows the folder above it (/)')
        self.mount_whole.add(('8:9', '/a'))
        self.file(os.path.join(middle, 'KEEP'), 'two mounts up\n')
        self.assertKept(self.plan(cfg), cold, 'KEEP file', f'two mounts up ({middle}, above {cold})')
        self.assertUntouched(cfg, path)
        os.remove(os.path.join(middle, 'KEEP'))
        self.age(path, 40)              # the checks above read it
        code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, sorted(os.listdir(cold))), (0, ['old.jsonl.zst']), lines)

    def test_a_mount_list_that_does_not_show_the_folder_keeps_it(self):
        """No mount line holds the cold folder, or the mount shows something that is not a folder (a namespace
        file): it cannot be known what is above, so it is kept."""
        self.mount_lines[:] = [(self.path('elsewhere'), '8:1', '/')]
        path = self.big('old.jsonl')
        self.assertKept(self.plan(self.cold_cfg()), self.cold, 'could not read', 'no mount in the mount list holds')
        self.assertUntouched(self.cold_cfg(), path)
        self.mount_lines[:] = [('/', '0:0', '/'), (self.dir, '0:4', 'net:[4026531840]')]
        self.assertKept(self.plan(self.cold_cfg()), self.cold, 'could not read', 'not a folder')
        self.assertUntouched(self.cold_cfg(), path)

    def test_a_bind_mount_state_cases(self):
        """A marker made above the view between the plan and the run stops it; a second run still leaves it; a
        run stopped half-way (a part file; a copy linked, the original still there) leaves all of it; once the
        marker is gone the next run compresses."""
        fs, cold, path, cfg = self.view('state')
        real = self.c.compress.recheck

        def recheck(cfg, facts, item):
            self.file(os.path.join(fs, '.git'), 'gitdir: /elsewhere\n')
            return real(cfg, facts, item)
        with mock.patch.object(self.c.compress, 'recheck', recheck):
            code, lines = self.assertUntouched(cfg, path)
        self.assertTrue(any('git folder' in line for line in lines), lines)
        part = self.file(os.path.join(cold, '.old.jsonl.zst.0badc0de.cleaner-part'), 'half a copy')
        linked = self.file(os.path.join(cold, 'linked.jsonl'), text(), 40)
        self.pack(linked, linked + '.zst')
        self.age(linked, 40)
        before = sorted(os.listdir(cold))
        for _ in range(2):
            code, lines = self.assertUntouched(cfg, path)
            self.assertEqual((code, sorted(os.listdir(cold))), (0, before), lines)
        with open(part) as f:
            self.assertEqual(f.read(), 'half a copy')
        os.remove(os.path.join(fs, '.git'))
        self.age(path, 40)
        self.age(linked, 40)
        code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, sorted(os.listdir(cold))), (0, ['linked.jsonl.zst', 'old.jsonl.zst']), lines)
        self.assertEqual(self.unpack(path + '.zst'), text())

    def test_the_cold_folder_itself_a_bind_mount_point(self):
        """#620: the cold folder is itself the mount point of a view of the folder /data of a disk (no folder
        between it and the mount): the folder above /data is read as for a view higher up. A `.git` there keeps it,
        as does a list with no view of it; with nothing there it compresses; a second run and a run after the
        marker is gone behave the same."""
        top = self.mkdir('point')
        fs, cold = self.mkdir('point', 'fs'), self.mkdir('point', 'cold')
        path = self.file(os.path.join(cold, 'old.jsonl'), text(), 40)
        cfg = self.cfg(compressCold={'folders': [{'path': cold, 'days': 30}], 'keepFreeGB': 1})
        self.mount_lines += [(cold, '8:9', '/data')]
        self.assertKept(self.plan(cfg), cold, 'could not read', f'{cold} shows the folder /data of its filesystem')
        self.mount_lines.insert(1, (fs, '8:9', '/'))
        os.mkdir(os.path.join(fs, '.git'))
        for _ in range(2):
            self.assertKept(self.plan(cfg), cold, 'git folder', f'{fs}, above {cold}')
            self.assertUntouched(cfg, path)
        os.rmdir(os.path.join(fs, '.git'))
        self.age(path, 40)
        code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, sorted(os.listdir(cold))), (0, ['old.jsonl.zst']), lines)
        self.assertTrue(os.path.isdir(top))

    def test_a_place_the_mount_list_shows_that_is_not_a_folder_now_keeps_it(self):
        """#620: the mount list names a place that shows the folder above, but that place is not a folder now: it
        was unmounted after the list was read (gone, or a link to a clean folder put there), or another mount covers
        it (a list without mount ids cannot tell). Its markers cannot be read there: kept. Once the place shows the
        folder again, the next run compresses."""
        fs, cold, path, cfg = self.view('gone')
        os.rmdir(fs)
        self.assertKept(self.plan(cfg), cold, 'could not read', f'{fs}: No such file or directory')
        self.assertUntouched(cfg, path)
        clean = self.mkdir('gone', 'clean')
        os.symlink(clean, fs)
        self.assertKept(self.plan(cfg), cold, 'could not read', f'{fs} is a link or not a folder now')
        self.assertUntouched(cfg, path)
        os.remove(fs)
        self.file(fs, 'a file where the folder was\n')
        self.assertKept(self.plan(cfg), cold, 'could not read', f'{fs} is a link or not a folder now')
        self.assertUntouched(cfg, path)
        os.remove(fs)
        os.mkdir(fs)
        self.age(path, 40)
        code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, sorted(os.listdir(cold))), (0, ['old.jsonl.zst']), lines)
        # Covered: the only view of the whole disk is at x/fs, and later a mount goes over x. The list still names
        # x/fs, but x/fs on disk is now a path in the mount over it, here not there.
        x = self.mkdir('covered', 'x')
        view = self.mkdir('covered', 'view')
        self.mount_lines += [(os.path.join(x, 'fs'), '8:40', '/'), (view, '8:40', '/data'), (x, '8:41', '/')]
        cold = self.mkdir('covered', 'view', 'cold')
        path = self.file(os.path.join(cold, 'old.jsonl'), text(), 40)
        cfg = self.cfg(compressCold={'folders': [{'path': cold, 'days': 30}], 'keepFreeGB': 1})
        self.assertKept(self.plan(cfg), cold, 'could not read', f'{x}/fs: No such file or directory')
        self.assertUntouched(cfg, path)

    # --- the mount list checked against the kernel, and layered filesystems (#624) -----------------------------------
    def own_view(self, name, kind='ext4', device=None):
        """`view`, with a mount list that is this process's own: ids, filesystem types and (unless `device` is
        given) the device the kernel gives the test's folders. Lines: / (id 1), `fs` (2), `view` (3).
        (fs, view, cold folder, file, config, {point: id} of the list)"""
        if device is None:
            dev = os.lstat(self.dir).st_dev
            device = f'{os.major(dev)}:{os.minor(dev)}'
        self.mount_lines[:] = [('/', device, '/')]
        fs, cold, path, cfg = self.view(name, device=device)
        self.mount_ids, self.mount_types, self.mount_own = [(1, 0), (2, 1), (3, 1)], [kind] * 3, True
        return fs, os.path.dirname(cold), cold, path, cfg, {'/': 1, fs: 2, os.path.dirname(cold): 3}

    def test_a_mount_taken_away_after_the_list_was_read_keeps_it(self):
        """#624: the list names a mount at `fs` that shows the whole disk, but it was unmounted after the list was
        read: `fs` is now an empty folder of the filesystem above, where no marker is. What is above the view is not
        known: kept, on a second run too. Once the kernel shows the disk at `fs` again, the next run compresses."""
        fs, view, cold, path, cfg, table = self.own_view('taken')
        now = dict(table)
        del now[fs]
        with self.kernel(now):
            for _ in range(2):
                self.assertKept(self.plan(cfg), cold, 'could not read', f'{fs} is not on the mount the mount list names')
                self.assertUntouched(cfg, path)
        self.age(path, 40)              # the checks above read it
        with self.kernel(table):
            code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, sorted(os.listdir(cold))), (0, ['old.jsonl.zst']), lines)

    def test_a_mount_put_over_a_folder_after_the_list_was_read_keeps_it(self):
        """#624: a mount the list does not name is over a folder now (the list is old): over the view of the whole
        disk above, or over the cold folder itself. The folder read is not the one the list names: kept."""
        for over in ('fs', 'cold'):
            with self.subTest(over=over):
                fs, view, cold, path, cfg, table = self.own_view(f'over{over}')
                shown = fs if over == 'fs' else cold
                with self.kernel({**table, shown: 99}):
                    self.assertKept(self.plan(cfg), cold, 'could not read', f'{shown} is not on the mount the mount list names')
                    self.assertUntouched(cfg, path)

    def test_a_folder_that_changes_while_its_markers_are_read_keeps_it(self):
        """#624: the kernel is asked once before the markers of a folder above are read and once after: a mount
        taken away in between (the markers read may be the empty mount point's) keeps it."""
        fs, view, cold, path, cfg, table = self.own_view('changes')
        now, real = dict(table), self.c.guard.marker

        def marker(here):
            got = real(here)
            if here == fs:
                now.pop(fs, None)
            return got
        with self.kernel(now), mock.patch.object(self.c.guard, 'marker', marker):
            self.assertKept(self.plan(cfg), cold, 'could not read', f'{fs} is not on the mount the mount list names')
            self.assertUntouched(cfg, path)
        self.assertNotIn(fs, now)
        # Another folder put in its place on the same mount while its markers were read (the ones read were the
        # old folder's): the kernel names the same mount and device, but not the same folder.
        swapped = []

        def swap(here):
            got = real(here)
            if here == fs and not swapped:
                os.rename(fs, fs + '.old')
                os.mkdir(fs)
                swapped.append(here)
            return got
        with self.kernel(table), mock.patch.object(self.c.guard, 'marker', swap):
            self.assertKept(self.plan(cfg), cold, 'could not read', f'{fs} changed while its markers were read')
        self.assertEqual(swapped, [fs])
        os.rmdir(fs)
        os.rename(fs + '.old', fs)
        self.age(path, 40)
        with self.kernel(table):
            code, lines = self.run_cleaner(cfg)
        self.assertEqual((code, sorted(os.listdir(cold))), (0, ['old.jsonl.zst']), lines)

    def test_the_device_is_compared_where_the_type_gives_it(self):
        """#624: on ext4 (and every type but btrfs, bcachefs and overlay) a folder has the device mountinfo names:
        a mount id that matches with another device is not that mount (an id used again), so it is kept. On btrfs a
        subvolume has a device of its own, so there only the mount id counts."""
        for kind, kept in (('ext4', True), ('xfs', True), ('btrfs', False), ('bcachefs', False)):
            with self.subTest(kind=kind):
                fs, view, cold, path, cfg, table = self.own_view(f'dev{kind}', kind=kind, device='250:250')
                with self.kernel(table):
                    if kept:
                        self.assertKept(self.plan(cfg), cold, 'could not read', 'is not on the mount the mount list names')
                        self.assertUntouched(cfg, path)
                    else:
                        code, lines = self.run_cleaner(cfg)
                        self.assertEqual((code, sorted(os.listdir(cold))), (0, ['old.jsonl.zst']), lines)

    def test_a_mount_id_that_cannot_be_read_keeps_it(self):
        fs, view, cold, path, cfg, table = self.own_view('noid')

        def mount_id_of(fd):
            raise self.c.facts.Unreadable('/proc/self/fdinfo: gone')
        with mock.patch.object(self.c.guard, 'mount_id_of', mount_id_of):
            self.assertKept(self.plan(cfg), cold, 'could not read', 'gone')
            self.assertUntouched(cfg, path)

    def test_the_real_mount_list_and_the_kernel_agree(self):
        """#624: with this machine's own list, the test's folder passes (or, on an overlay or another layered
        filesystem, is refused for that alone). The same list with one more line, a mount on the test's folder that
        is not there (a list read before it was taken away), keeps it."""
        real = self.c.facts.read_mounts()
        self.assertTrue(real.own)
        self.assertFalse(self.c.facts.read_mounts(self.file(self.path('mountinfo'), '1 0 8:1 / / rw - ext4 /dev/sda1 rw\n'),
                                                  proc=None).own)
        guard, cold = self.c.guard, self.mkdir('real', 'cold')
        got = guard.above_refusal(cold, real)
        here, layered = cold, False
        while True:
            layered = layered or real.type_of(real.held(here)) in guard.LAYERED
            if here == '/':
                break
            here = os.path.dirname(here)
        if layered:
            self.assertEqual((got.code, 'shows folders of other filesystems' in got.detail), ('could not read', True), got)
        self.assertIsNone(guard.above_refusal(cold, real, layered=True))
        index = real.held(self.dir)
        stale = self.c.facts.Mounts(
            real.points | {self.dir}, real.lines + [(self.dir, real.lines[index][1], '/')], (), {}, (), real.whole,
            real.ids + [(max(mid for mid, _ in real.ids) + 1, real.ids[index][0])], real.types + [real.types[index]],
            own=True)
        got = guard.above_refusal(cold, stale, layered=True)
        self.assertEqual((got.code, f'{cold} is not on the mount the mount list names' in got.detail),
                         ('could not read', True), got)
        fd = os.open(cold, os.O_PATH)
        try:
            self.assertEqual(self.c.facts.mount_id_of(fd), real.id_of(index))
        finally:
            os.close(fd)

    def test_a_folder_on_a_layered_filesystem_keeps_it(self):
        """#624: an overlay, bindfs or union filesystem shows folders of others (its upper and lower folders, a
        bindfs source) with mount root `/`: the folders above those are not shown, and a `.git` or KEEP there would
        be missed. A cold folder on one, or below a folder on one, is kept (on a second run too) unless its config
        says `layered: true`; then it is compressed, and a marker it can see still keeps it."""
        for kind in ('overlay', 'fuse.bindfs', 'fuse.mergerfs', 'fuse', 'fuse.overlayfs'):
            for where in ('cold', 'root'):
                with self.subTest(kind=kind, where=where):
                    cold, path, cfg = self.nested(f'layer{kind}{where}', 'cold')
                    if where == 'cold':
                        self.mount_lines[:] = [('/', '0:0', '/'), (cold, '0:77', '/')]
                        self.mount_types = ['ext4', kind]
                    else:
                        self.mount_lines[:] = [('/', '0:0', '/')]
                        self.mount_types = [kind]
                    for _ in range(2):
                        self.assertKept(self.plan(cfg), cold, 'could not read', f'is on {kind} at')
                        self.assertUntouched(cfg, path)
                    entry = {'path': cold, 'days': 30, 'layered': True}
                    marked = self.cfg(compressCold={'folders': [entry], 'keepFreeGB': 1})
                    os.mkdir(os.path.join(os.path.dirname(cold), '.git'))
                    self.assertKept(self.plan(marked), cold, 'git folder')
                    self.assertUntouched(marked, path)
                    os.rmdir(os.path.join(os.path.dirname(cold), '.git'))
                    self.age(path, 40)
                    code, lines = self.run_cleaner(marked)
                    self.assertEqual((code, sorted(os.listdir(cold))), (0, ['old.jsonl.zst']), lines)
        self.mount_lines[:], self.mount_types = [('/', '0:0', '/')], ['ext4']     # sshfs, ext4: not layered
        for kind in ('ext4', 'fuse.sshfs', 'tmpfs'):
            self.mount_types = [kind]
            cold, path, cfg = self.nested(f'plain{kind}', 'cold')
            code, lines = self.run_cleaner(cfg)
            self.assertEqual((code, sorted(os.listdir(cold))), (0, ['old.jsonl.zst']), lines)

    def test_layered_is_a_yes_or_no_and_no_by_default(self):
        cold, path, cfg = self.nested('flag', 'cold')
        self.assertEqual([f.layered for f in cfg.cold_folders], [False])
        with self.assertRaises(machine_file.MachineFileError):
            self.cfg(compressCold={'folders': [{'path': cold, 'days': 30, 'layered': 'yes'}], 'keepFreeGB': 1})

    def test_a_nested_btrfs_subvolume_needs_its_folders_above(self):
        """#620: only a subvolume directly in the btrfs top folder (`/@data`: one part) needs no view of the folder
        above. A subvolume deeper down, mounted whole (`/projects/repo/data`, `/@/home`), has ordinary folders above
        it that can hold a work tree: with no view of them it is kept; with a view they are read like any other."""
        for n, root in enumerate(('/projects/repo/data', '/@/home')):
            with self.subTest(root=root):
                device = f'8:{50 + n}'
                _, cold, path, cfg = self.view(f'nested{n}', root=root, whole=True, fs=False, device=device)
                self.assertKept(self.plan(cfg), cold, 'could not read',
                                f'no mount here shows the folder above it ({os.path.dirname(root)})')
                self.assertUntouched(cfg, path)
                fs = self.mkdir(f'nested{n}', 'fs')
                above = self.mkdir(f'nested{n}', 'fs', *os.path.dirname(root).split('/')[1:])
                self.mount_lines.append((fs, device, '/'))
                os.mkdir(os.path.join(above, '.git'))
                self.assertKept(self.plan(cfg), cold, 'git folder', f'{above}, above {cold}')
                self.assertUntouched(cfg, path)
                os.rmdir(os.path.join(above, '.git'))
                self.age(path, 40)
                code, lines = self.run_cleaner(cfg)
                self.assertEqual((code, sorted(os.listdir(cold))), (0, ['old.jsonl.zst']), lines)

    def test_a_marker_made_in_a_subfolder_at_the_last_check_stops_it(self):
        """#620: the last check reads every folder from the cold folder down to the file's folder: a KEEP file or a
        `.git` made between the plan and the run in `cold/sub`, with the file in `cold/sub/deeper`, stops it."""
        for marker in ('KEEP', '.git'):
            with self.subTest(marker=marker):
                cold = self.mkdir(f'sub{marker}', 'cold')
                path = self.file(os.path.join(cold, 'sub', 'deeper', 'old.jsonl'), text(), 40)
                cfg = self.cfg(compressCold={'folders': [{'path': cold, 'days': 30}], 'keepFreeGB': 1})
                at, real = os.path.join(cold, 'sub', marker), self.c.compress.recheck

                def recheck(cfg, facts, item):
                    if not os.path.lexists(at):
                        self.file(at, 'made late\n')
                    return real(cfg, facts, item)
                with mock.patch.object(self.c.compress, 'recheck', recheck):
                    code, lines = self.assertUntouched(cfg, path)
                self.assertTrue(any(('KEEP file' in line or 'git folder' in line) and path in line for line in lines), lines)
                os.remove(at)
                self.age(path, 40)
                code, lines = self.run_cleaner(cfg)
                self.assertEqual((code, os.path.exists(path + '.zst')), (0, True), lines)

    def test_a_view_under_a_mount_placed_beneath_is_not_used(self):
        """#620: with mount ids the mount on top of a point is the one no other mount on that point sits on, not
        the one listed last. Here the whole-disk view at `fs` is listed before another mount on `fs` but sits on it:
        that one was placed beneath it (`move_mount` beneath) or the view was moved onto `fs` later. The view is on
        top, so the KEEP file in it counts. When the ids say the later mount is on top, it hides the view."""
        fs, cold, path, cfg = self.view('beneath')
        self.file(os.path.join(fs, 'KEEP'), 'the whole disk\n')
        # ids: / 1; the view of the whole disk at fs 10, sitting on 30; the cold folder's view 11; the other mount 30.
        self.mount_lines.append((fs, '8:10', '/'))
        self.mount_ids = [(1, 0), (10, 30), (11, 1), (30, 1)]
        for _ in range(2):
            self.assertKept(self.plan(cfg), cold, 'KEEP file', f'the whole disk ({fs}, above {cold})')
            self.assertUntouched(cfg, path)
        self.mount_ids = [(1, 0), (10, 1), (11, 1), (30, 10)]
        self.assertKept(self.plan(cfg), cold, 'could not read', 'no mount here shows the folder above it (/)')
        self.assertUntouched(cfg, path)
        self.mount_ids = [(1, 0), (10, 30), (11, 1), (30, 10)]          # a loop: what holds fs is not known
        self.assertKept(self.plan(cfg), cold, 'could not read', 'no mount here shows the folder above it (/)')
        self.assertUntouched(cfg, path)

    def test_the_tests_make_their_folders_where_no_marker_is_above(self):
        """#604: a $TMPDIR inside a git checkout or under a KEEP file would make every test that expects a file
        compressed fail: the fixture takes the first temp folder with none above it."""
        repo, keep, plain = self.mkdir('repo', 'tmp'), self.mkdir('kept', 'tmp'), self.mkdir('plain')
        os.mkdir(self.path('repo', '.git'))
        self.file(self.path('kept', 'KEEP'), '')
        self.assertEqual(fx.first_unmarked([repo, keep, plain]), plain)
        self.assertIsNone(fx.first_unmarked([repo, keep]))

    def test_the_tests_make_their_folders_where_no_other_user_can_write_above(self):
        """#624: a group-writable $TMPDIR (or one under a group-writable folder) makes the machine file be refused
        rightly, so the fixture passes over it; a sticky one (like /tmp) is fine, and so is the same call again."""
        shared, under, sticky, plain = (self.mkdir('shared'), self.mkdir('open', 'tmp'), self.mkdir('sticky'),
                                        self.mkdir('plain'))
        os.chmod(shared, 0o770)
        os.chmod(self.path('open'), 0o777)
        os.chmod(sticky, 0o1777)
        try:
            self.assertEqual(fx.first_unmarked([shared, under, sticky, plain]), sticky)
            self.assertEqual(fx.first_unmarked([shared, under, plain]), plain)
            self.assertEqual(fx.first_unmarked([shared, under, plain]), plain)
            self.assertIsNone(fx.first_unmarked([shared, under, self.path('gone')]))
        finally:
            for one in (shared, self.path('open'), sticky):
                os.chmod(one, 0o700)

    def test_a_marker_made_between_the_plan_and_the_run_stops_it(self):
        for marker in ('KEEP', '.git'):
            with self.subTest(marker=marker):
                cold, path, cfg = self.nested(f'with{marker}', 'cold')
                real = self.c.compress.recheck

                def recheck(cfg, facts, item):
                    self.file(self.path(f'with{marker}', marker), 'made late\n')
                    return real(cfg, facts, item)
                with mock.patch.object(self.c.compress, 'recheck', recheck):
                    code, lines = self.assertUntouched(cfg, path)
                self.assertTrue(any('KEEP file' in line or 'git folder' in line for line in lines), lines)

    def test_a_second_run_still_leaves_it_and_a_marker_removed_lets_the_next_run_compress(self):
        keep = self.file(self.path('KEEP'), 'for now\n')
        path = self.big('old.jsonl')
        for _ in range(2):
            self.assertKept(self.plan(self.cold_cfg()), self.cold, 'KEEP file', 'for now')
            self.assertUntouched(self.cold_cfg(), path)
        os.remove(keep)
        self.age(path, 40)              # the checks above read it
        code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual((code, self.files()), (0, ['old.jsonl.zst']), lines)
        self.assertEqual(self.unpack(path + '.zst'), text())

    def test_a_run_stopped_half_way_before_the_marker_came_leaves_everything(self):
        written = self.big('written', 'old.jsonl')
        part = self.file(os.path.join(self.cold, 'written', '.old.jsonl.zst.0badc0de.cleaner-part'), 'half a copy')
        linked = self.big('linked', 'old.jsonl')
        self.pack(linked, linked + '.zst')
        self.age(linked, 40)
        before = self.files()
        os.makedirs(self.path('.git'))
        plan = self.plan(self.cold_cfg())
        self.assertKept(plan, self.cold, 'git folder')
        self.assertEqual(plan.items, [])
        code, lines = self.run_cleaner(self.cold_cfg())
        self.assertEqual((code, self.files()), (0, before), lines)
        with open(part) as f:
            self.assertEqual(f.read(), 'half a copy')
        for path in (written, linked):
            with open(path) as f:
                self.assertEqual(f.read(), text())


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
    'markers in the cold folder and under it': ('compress', 'found = guard.marker(here)', 'found = None',
                                                 Refused, 'test_what_it_never_takes'),
    'each name is looked up below (case-folding)': ('compress', 'found = guard.marker(here)',
                                                    "found = guard.marker(here) if 'KEEP' in names or '.git' in names or {'HEAD', 'objects', 'refs'} <= set(names) else None",
                                                    Above, 'test_a_folder_that_folds_case_counts_keep_above_and_below'),
    'a KEEP entry': ('guard', "    if st is not None:\n        return 'KEEP file',", "    if False:\n        return 'KEEP file',",
                     Above, 'test_folder_under_a_keep_file'),
    'a KEEP folder counts': ('guard', "    if st is not None:\n        return 'KEEP file',",
                             "    if st is not None and not stat.S_ISDIR(st.st_mode):\n        return 'KEEP file',",
                             Above, 'test_a_keep_folder_above_keeps_it_as_one_below_does'),
    'a git folder': ('guard', "if _lstat_or_none(os.path.join(here, '.git')) is not None:", 'if False:',
                     Above, 'test_folder_inside_a_git_work_tree'),
    'a bare repository': ('guard', 'if all(_lstat_or_none(os.path.join(here, n)) is not None for n in BARE):', 'if False:',
                          Above, 'test_folder_inside_a_bare_repository'),
    'a bare repository needs refs': ('guard', "BARE = ('HEAD', 'objects', 'refs')", "BARE = ('HEAD', 'objects')",
                                     Above, 'test_a_bare_repository_needs_all_three_names'),
    'a .git that cannot be read': ('guard', "if _lstat_or_none(os.path.join(here, '.git')) is not None:",
                                   "if os.path.lexists(os.path.join(here, '.git')):", Above, 'test_a_marker_that_cannot_be_read_keeps_it'),
    'a bare repository name that cannot be read': ('guard', 'if all(_lstat_or_none(os.path.join(here, n)) is not None for n in BARE):',
                                                   'if all(os.path.lexists(os.path.join(here, n)) for n in BARE):',
                                                   Above, 'test_a_marker_that_cannot_be_read_keeps_it'),
    'a folder above that cannot be read': ('guard', '    except FileNotFoundError:\n        return None\n\n\ndef _unpushed',
                                           '    except OSError:\n        return None\n\n\ndef _unpushed',
                                           Above, 'test_a_marker_that_cannot_be_read_keeps_it'),
    'a marker below that cannot be read': ('compress', "    except OSError as ex:\n        return Refusal('could not read', f'{here}: {ex.strerror}')\n    if found is not None and found[0] == 'KEEP file':",
                                           "    except OSError as ex:\n        found = None\n    if found is not None and found[0] == 'KEEP file':",
                                           Above, 'test_a_marker_that_cannot_be_read_keeps_it'),
    'the folders above, at the plan': ('compress', '    refusal = guard.above_refusal(path, mounts, folder.layered)\n    if refusal is not None:\n        return None, refusal\n',
                                       '', Above, 'test_folder_under_a_keep_file'),
    # Read at the plan only (kept on the folder): the refusal comes from nothing else, so only a marker made
    # after the plan shows it.
    'the folders above, at the last check': ('compress', '    refusal = guard.above_refusal(path, mounts, folder.layered)\n',
                                             "    if not hasattr(folder, 'above'):\n        folder.above = guard.above_refusal(path, mounts, folder.layered)\n    refusal = folder.above\n",
                                             Above, 'test_a_marker_made_between_the_plan_and_the_run_stops_it'),
    'the folders again after the copy is written': ('compress', 'refusal = _folders(cfg, facts, item)\n        refusal = refusal if isinstance(refusal, Refusal) else _in_use(',
                                                    'refusal = _in_use(', Above, 'test_a_marker_made_while_the_copy_is_written_keeps_the_original'),
    'the folders above on its own filesystem': ('guard', 'stack.extend((place, True) for place in reversed(places))', 'pass',
                                                Above, 'test_a_marker_above_a_bind_mount_on_its_own_filesystem_keeps_it'),
    'no view of the folder above keeps it': ('guard', "if not places and not ((device, root) in mounts.whole and up == '/'):", 'if False:',
                                             Above, 'test_a_bind_mount_whose_folder_above_no_mount_shows_keeps_it'),
    'a whole btrfs subvolume needs no view': ('guard', "if not places and not ((device, root) in mounts.whole and up == '/'):", 'if not places:',
                                              Above, 'test_a_whole_btrfs_subvolume_needs_no_view_above'),
    'a view must not be hidden': ('facts', 'if self.holding(place) == line:', 'if True:', Above, 'test_a_view_another_mount_hides_is_not_used'),
    'only a top btrfs subvolume needs no view': ('guard', "if not places and not ((device, root) in mounts.whole and up == '/'):",
                                                 'if not places and (device, root) not in mounts.whole:',
                                                 Above, 'test_a_nested_btrfs_subvolume_needs_its_folders_above'),
    'a place shown must be a folder now': ('guard', 'hst = os.lstat(here)\n            except OSError as ex:',
                                           "hst = os.lstat('/')\n            except OSError as ex:",
                                           Above, 'test_a_place_the_mount_list_shows_that_is_not_a_folder_now_keeps_it'),
    'the cold folder itself a mount point': ('guard', "if here == point and root != '/':", "if here == point and root != '/' and look:",
                                             Above, 'test_the_cold_folder_itself_a_bind_mount_point'),
    'the last check reads down to the file': ('compress', "    for part in [None] + ([] if rel == '.' else rel.split('/')):\n        if part is not None:\n            here = os.path.join(here, part)\n        try:\n            hst = os.lstat(here) if part is not None else hst\n            names = set(os.listdir(here)) if stat.S_ISDIR(hst.st_mode) and not stat.S_ISLNK(hst.st_mode) else set()\n        except OSError as ex:\n            return Refusal('could not read', f'{here}: {ex.strerror}')\n        refusal = _folder_refusal(cfg, facts, folder, here, hst, names)\n        if refusal is not None:\n            return refusal\n    return folder, names",
                                              "    for part in [None] + ([] if rel == '.' else rel.split('/')):\n        if part is not None:\n            here = os.path.join(here, part)\n        try:\n            hst = os.lstat(here) if part is not None else hst\n            names = set(os.listdir(here)) if stat.S_ISDIR(hst.st_mode) and not stat.S_ISLNK(hst.st_mode) else set()\n        except OSError as ex:\n            return Refusal('could not read', f'{here}: {ex.strerror}')\n        refusal = _folder_refusal(cfg, facts, folder, here, hst, names) if part is None else None\n        if refusal is not None:\n            return refusal\n    return folder, names",
                                              Above, 'test_a_marker_made_in_a_subfolder_at_the_last_check_stops_it'),
    'the top mount by mount ids': ('facts', 'if self.ids is not None:\n            return self._holding_by_ids(path)', 'if False:\n            pass',
                                   Above, 'test_a_view_under_a_mount_placed_beneath_is_not_used'),
    'views through a chain of mounts': ('guard', "if here == point and root != '/':", "if here == point and root != '/' and path.startswith(here + '/'):",
                                        Above, 'test_views_are_followed_through_a_chain_of_mounts'),
    'no mount line holds it': ('guard', "        if index is None:\n            return Refusal('could not read', f'no mount in the mount list holds {here}')\n",
                               "        if index is None:\n            index = 0\n", Above, 'test_a_mount_list_that_does_not_show_the_folder_keeps_it'),
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
                                             "                                                    'another rule deletes from')", 'pass',
                                             Config, 'test_folders_it_refuses'),
    'config: never the agent-scratch root': ('config', "for root in self.roots() | ({self.scratch_root} if self.scratch_enabled else set()):\n            if _holds(path, root)",
                                             "for root in self.roots():\n            if _holds(path, root)",
                                             Config, 'test_never_where_agent_scratch_deletes_or_keeps_first'),
    'config: never the keep-first archive': ('config', "('daily.outDir', out_text, self.daily_dir)) + archive:",
                                             "('daily.outDir', out_text, self.daily_dir)):",
                                             Config, 'test_never_where_agent_scratch_deletes_or_keeps_first'),
    'the report counts compressing': ("report", "if verb == 'compressed':", 'if False:', Report, 'test_the_report_counts_what_compressing_freed'),
    'a layered filesystem keeps it': ('guard', 'if kind in LAYERED and not layered:', 'if False:',
                                      Above, 'test_a_folder_on_a_layered_filesystem_keeps_it'),
    'layered: true takes that away': ('guard', 'if kind in LAYERED and not layered:', 'if kind in LAYERED:',
                                      Above, 'test_a_folder_on_a_layered_filesystem_keeps_it'),
    'cold: layered is passed on': ('compress', 'refusal = guard.above_refusal(path, mounts, folder.layered)',
                                   'refusal = guard.above_refusal(path, mounts, False)',
                                   Above, 'test_a_folder_on_a_layered_filesystem_keeps_it'),
    'layered is no by default': ('config', "'layered': (bool, False, None),", "'layered': (bool, True, None),",
                                 Above, 'test_layered_is_a_yes_or_no_and_no_by_default'),
    'the kernel finds the listed mount': ('guard', 'if (want is None or now[0] != want\n', 'if (False\n',
                                          Above, 'test_a_mount_taken_away_after_the_list_was_read_keeps_it'),
    'a mount put over a folder': ('guard', 'if (want is None or now[0] != want\n', 'if (want is None\n',
                                  Above, 'test_a_mount_put_over_a_folder_after_the_list_was_read_keeps_it'),
    'the device is compared': ('guard', "and f'{os.major(now[1])}:{os.minor(now[1])}' != device):", 'and False):',
                               Above, 'test_the_device_is_compared_where_the_type_gives_it'),
    'not on btrfs and bcachefs': ('guard', '(kind is None or kind not in OTHER_DEVICES)', '(True)',
                                  Above, 'test_the_device_is_compared_where_the_type_gives_it'),
    'the folder must not change while read': ('guard', 'if after != before:', 'if False:',
                                              Above, 'test_a_folder_that_changes_while_its_markers_are_read_keeps_it'),
    'no mount id keeps it': ('guard', "return Refusal('could not read', f'the mount {here} is on ({ex})'), None",
                             'return None, None', Above, 'test_a_mount_id_that_cannot_be_read_keeps_it'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
