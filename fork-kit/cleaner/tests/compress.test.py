"""The `compress-cold` rule (lib/cleaner/compress.py) and its keys (config.py). Every file lives in the test's own
temporary folder; zstd is the real one, or a stub in front of it that fails one step. The state cases: a second run,
a run stopped half-way (a part file, or a copy linked and the original still there), a file in use or opened or
changed while it is compressed, a disk nearly full, zstd missing or failing, and a machine file without the keys.
RedCheck breaks each rule in a copy of the code and shows its test then fails.

  python3 -I -B fork-kit/cleaner/tests/compress.test.py
"""
import json, os, re, shutil, subprocess, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cleaner_fixture as fx  # noqa: E402

import machine_file  # noqa: E402

DAY = fx.DAY
GB = 1024 ** 3
REAL_ZSTD = shutil.which('zstd')

# zstd in front of the real one: `state[step]` makes one step ('compress', 'test', 'decompress') fail as a full
# disk does, give other bytes, or stop reading its input early and still exit 0.
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

        def stop(fd):
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

        def then_changed(src, dst):
            out = real(src, dst)
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
    'zstd -t': ("compress", "proc, err = _run(['-q', '-t'], fd, subprocess.DEVNULL)\n    with err:\n        _finish(proc, err, 'test (-t)')",
                'pass', Refused, 'test_a_copy_that_does_not_read_back_keeps_the_original'),
    'a failed zstd fails': ('compress', 'if code != 0:', 'if False:', Refused, 'test_a_full_disk_while_writing_keeps_the_original'),
    'a part is removed': ("compress", "if original in names or original + '.zst' in names:", 'if False:',
                          HalfWay, 'test_a_half_written_copy_is_removed_and_the_file_compressed_again'),
    'a part is removed after every step': ('compress', "            os.unlink(part, dir_fd=parent)\n", '            pass\n',
                                           HalfWay, 'test_a_step_that_stops_leaves_the_original_and_no_part'),
    'a copy beside it is checked': ('compress', 'if _content(fd) != _hash(src):', 'if False:', HalfWay, 'test_a_different_copy_beside_it_keeps_both'),
    'in use': ('compress', 'if processes.uses(path):', 'if False:', InUse, 'test_files_in_use_stay'),
    'in use: a unit names a folder above': ('compress', 'if here in units:', 'if False:', InUse, 'test_files_in_use_stay'),
    'in use: a lock': ('compress', 'if st.st_ino in processes.locks:', 'if False:', InUse, 'test_files_in_use_stay'),
    'opened while it is compressed': ('compress', 'if refusal is not None or not _same(now, item.look)', 'if not _same(now, item.look)',
                                      InUse, 'test_a_file_opened_while_it_is_compressed_stays_and_its_copy_goes'),
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
    'config: never the Claude homes': ('config', 'if must_keep_match(never, path) is not None:', 'if False:',
                                       Config, 'test_folders_it_refuses'),
    'config: never a must-keep path whole': ('config', 'if held:', 'if False:', Config, 'test_folders_it_refuses'),
    'config: no overlap with another rule': ('config', "raise machine_file.MachineFileError(f'{at}.path: {text} holds or is inside {root}, a folder '\n"
                                             "                                                        'another rule deletes from')", 'pass',
                                             Config, 'test_folders_it_refuses'),
    'the report counts compressing': ("report", "if verb == 'compressed':", 'if False:', Report, 'test_the_report_counts_what_compressing_freed'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
