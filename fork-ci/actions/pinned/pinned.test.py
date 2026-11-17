"""pinned.py: one fetch per sha and file, whole or not at all, of the bytes the config names (git blob id); no network
and no good copy is an error; a killed fetch's .part- file and an old pin's folder go; load and run run the bytes that
were checked. The README's bootstrap is run as written. No test reaches the network: every fetch is served here.
Run: python3 fork-ci/actions/pinned/pinned.test.py
"""
import contextlib, importlib.util, io, json, os, re, shutil, stat, struct, subprocess, sys, tempfile, time, unittest
from unittest import mock
try:
    import grp, pwd
except ImportError:   # no owners (Windows): the tests that need them are skipped
    grp = pwd = None

sys.dont_write_bytecode = True   # leave no __pycache__ in the action folder

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'pinned.py')
README = os.path.join(HERE, 'README.md')
spec = importlib.util.spec_from_file_location('pinned', SCRIPT)
pinned = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pinned)

SHA = 'a' * 40
BODY = b'X = 1\n'
RAW = f'https://raw.githubusercontent.com/owner/repo/{SHA}/fork-ci/actions'


class Case(unittest.TestCase):
    FILES = {'docs-scope/docs_scope.py': pinned.blob_id(BODY)}
    TRUST_ABOVE = True   # the test caches sit under the shared temp folder, which run refuses: Above tests that check

    def setUp(self):
        if self.TRUST_ABOVE:
            above = mock.patch.object(pinned, 'shared_above', lambda cache, caller='run': [])
            above.start()
            self.addCleanup(above.stop)
        self.root = tempfile.mkdtemp(prefix='pinned-root-')
        self.cache = tempfile.mkdtemp(prefix='pinned-cache-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.cache, True)
        self.cfg = os.path.join(self.root, 'fork-ci.json')
        self.config({'repo': 'owner/repo', 'sha': SHA, 'tag': 'ci/v1', 'files': self.FILES})
        self.urls = []
        self.dest = os.path.join(self.cache, SHA, 'docs-scope', 'docs_scope.py')

    def config(self, c):
        with open(self.cfg, 'w') as f:
            json.dump(c, f)

    def serve(self, body=BODY, error=None, bodies=None):
        def urlopen(url, timeout=None):
            self.urls.append(url)
            if error:
                raise error
            return io.BytesIO(bodies[url] if bodies else body)
        return mock.patch.object(pinned.urllib.request, 'urlopen', urlopen)

    def path(self, action='docs-scope', name='docs_scope.py'):
        return pinned.path(self.cfg, action, name, self.cache)

    def put(self, path, data, age=0, now=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        t = (now or time.time()) - age
        os.utime(path, (t, t))
        return path

    def private_above(self):
        """The folders above self.root (the real temp folder, /tmp) as root's 0755 folders: the test stands below."""
        real, uid, roots = os.stat, os.getuid(), (self.root, os.path.realpath(self.root))
        fake = os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 2, 0, 0, 4096, 0, 0, 0))

        def stat_(p, *a, **k):
            p = os.fspath(p)
            return fake if p == '/' or any(r.startswith(p.rstrip('/') + '/') for r in roots) else real(p, *a, **k)
        self.assertNotEqual(uid, 0, 'run the tests as a normal user')
        return mock.patch.object(os, 'stat', stat_)

    def parts(self):
        return [os.path.join(d, n) for d, _, ns in os.walk(self.cache) for n in ns if n.startswith('.part-')]


class Fetch(Case):
    def test_a_file_is_fetched_once_from_the_pinned_sha(self):
        with self.serve():
            p = self.path()
            self.assertEqual(self.path(), p)
        self.assertEqual(self.urls, [f'{RAW}/docs-scope/docs_scope.py'])
        self.assertEqual(p, self.dest)
        self.assertEqual(pinned.load(self.cfg, 'docs-scope', 'docs_scope', self.cache).X, 1)

    def test_no_network_and_no_copy_is_an_error_and_leaves_nothing(self):
        with self.serve(error=OSError('offline')), self.assertRaises(pinned.Unreachable):
            self.path()
        self.assertFalse(os.path.exists(self.dest))
        self.assertEqual(self.parts(), [])

    def test_a_new_sha_fetches_again(self):   # a bump of the pin never runs the old code
        with self.serve():
            self.path()
            self.config({'repo': 'owner/repo', 'sha': 'b' * 40, 'tag': 'ci/v2', 'files': self.FILES})
            self.path()
        self.assertEqual(len(self.urls), 2)
        self.assertIn('b' * 40, self.urls[1])

    def test_a_bad_config_or_name_is_refused_before_any_fetch(self):
        good = {'repo': 'owner/repo', 'sha': SHA, 'files': self.FILES}
        for c in ({**good, 'sha': 'main'}, {'sha': SHA, 'files': self.FILES}, {**good, 'repo': 'a b/c'},
                  {'repo': 'owner/repo', 'sha': SHA}, {**good, 'files': {'docs-scope/docs_scope.py': 'x'}},
                  {**good, 'files': ['docs-scope/docs_scope.py']}, {**good, 'sha': '../' * 13 + 'x'}, [], 'x'):
            with self.subTest(config=c):
                self.config(c)
                with self.serve(), self.assertRaises(pinned.FetchError):
                    self.path()
        with open(self.cfg, 'w') as f:
            f.write('{not json')
        with self.serve(), self.assertRaises(pinned.FetchError):
            self.path()
        self.config(good)
        for action, name in (('../x', 'a.py'), ('docs-scope', '../../a.py'), ('docs-scope', ''), ('.x', 'a.py')):
            with self.subTest(action=action, name=name), self.serve(), self.assertRaises(pinned.FetchError):
                self.path(action, name)
        self.assertEqual(self.urls, [])

    def test_a_missing_config_is_refused(self):
        os.remove(self.cfg)
        with self.serve(), self.assertRaises(pinned.FetchError):
            self.path()
        self.assertEqual(self.urls, [])

    def test_a_file_the_config_does_not_name_is_refused_before_any_fetch(self):
        with self.serve(), self.assertRaises(pinned.FetchError) as e:
            self.path('docs-scope', 'other.py')
        self.assertIn(f'is not in {self.cfg} "files"', str(e.exception))
        self.assertEqual(self.urls, [])

    def test_fetched_bytes_that_differ_are_refused_and_not_kept(self):   # the config does not match the commit
        with self.serve(body=b'X = 2\n'), self.assertRaises(pinned.FetchError) as e:
            self.path()
        self.assertIn('has blob id', str(e.exception))
        self.assertNotIsInstance(e.exception, pinned.Unreachable)   # a test must not skip it as "no network"
        self.assertFalse(os.path.exists(self.dest))
        self.assertEqual(self.parts(), [])

    def test_a_cached_copy_that_differs_is_fetched_again(self):   # a hand edit, a half write, another job's copy
        for bad in (b'X = 666\n', b'', b'X ='):
            with self.subTest(cached=bad):
                self.urls.clear()
                self.put(self.dest, bad)
                with self.serve():
                    self.assertEqual(pinned.load(self.cfg, 'docs-scope', 'docs_scope', self.cache).X, 1)
                self.assertEqual(len(self.urls), 1)
                with open(self.dest, 'rb') as f:
                    self.assertEqual(f.read(), BODY)

    def test_a_write_killed_half_way_is_never_used(self):   # the rename never ran: only a .part- file is left
        def killed(src, dst):
            with open(src, 'r+b') as f:
                f.truncate(2)   # half the bytes reached the disk
            raise KeyboardInterrupt
        with self.serve(), mock.patch.object(pinned.os, 'replace', killed), self.assertRaises(KeyboardInterrupt):
            self.path()
        self.assertFalse(os.path.exists(self.dest), 'nothing at the real name')
        self.assertEqual(self.parts(), [], 'the half-written .part- file goes too')
        # Killed so hard that no cleanup runs (SIGKILL): the half write is still only a .part- file.
        with self.serve(), mock.patch.object(pinned.os, 'replace', killed), \
                mock.patch.object(pinned.os, 'unlink', lambda f: None), self.assertRaises(KeyboardInterrupt):
            self.path()
        self.assertFalse(os.path.exists(self.dest), 'a hard kill leaves nothing at the real name')
        self.assertEqual(len(self.parts()), 1)
        for f in self.parts():
            os.remove(f)
        # A .part- file left by a process killed harder (SIGKILL: no cleanup) is never read as the file either.
        self.put(os.path.join(os.path.dirname(self.dest), '.part-killed'), BODY[:2])
        with self.serve():
            self.assertEqual(pinned.load(self.cfg, 'docs-scope', 'docs_scope', self.cache).X, 1)
        self.assertEqual(len(self.urls), 3, 'fetched again: the .part- file is not a copy')

    def test_an_existing_cache_folder_of_the_same_layout_is_used_as_is(self):   # a project's older cache folder
        self.put(self.dest, BODY)
        with self.serve(error=OSError('a hit needs no fetch')):
            self.assertEqual(self.path(), self.dest)
        self.assertEqual(self.urls, [])

    def test_the_bytes_that_run_are_the_bytes_checked(self):   # load() never reads the file a second time
        with self.serve():
            self.path()
        real = pinned.verified

        def swapped(*a, **k):
            dest, data = real(*a, **k)
            with open(dest, 'wb') as f:
                f.write(b'X = 666\n')   # changed on disk after the check
            return dest, data
        with mock.patch.object(pinned, 'verified', swapped):
            self.assertEqual(pinned.load(self.cfg, 'docs-scope', 'docs_scope', self.cache).X, 1)

    def test_a_cache_hit_marks_the_folder_used(self):   # a pin used only through hits is not pruned as unused
        with self.serve():
            self.path()
        top = os.path.join(self.cache, SHA)
        old = time.time() - 10 ** 6
        os.utime(top, (old, old))
        with self.serve(error=OSError('a hit needs no fetch')):
            self.path()
        self.assertGreater(os.stat(top).st_mtime, old + 10 ** 5)

    def test_old_part_files_and_old_pins_are_cleaned_on_use(self):
        now = time.time()
        old_part = self.put(os.path.join(self.cache, SHA, 'docs-scope', '.part-killed'), b'x', pinned.PART_AGE + 60, now)
        live_part = self.put(os.path.join(self.cache, SHA, 'docs-scope', '.part-live'), b'x', 5, now)
        old_pin, new_pin, other = (os.path.join(self.cache, x) for x in ('b' * 40, 'c' * 40, 'not-a-sha'))
        for folder, age in ((old_pin, pinned.CACHE_DAYS * 86400 + 60), (new_pin, 86400), (other, 10 ** 8)):
            self.put(os.path.join(folder, 'docs-scope', 'docs_scope.py'), b'x', age, now)
            os.utime(folder, (now - age, now - age))
        current = os.path.join(self.cache, SHA)
        os.utime(current, (now - 10 ** 8, now - 10 ** 8))   # the pinned sha's own folder stays, however old
        with self.serve():
            self.path()
        self.assertFalse(os.path.exists(old_part), 'a killed fetch older than PART_AGE goes')
        self.assertTrue(os.path.exists(live_part), 'a fetch that may still run stays')
        self.assertFalse(os.path.exists(old_pin), 'an old pin unused for CACHE_DAYS goes')
        self.assertTrue(os.path.exists(new_pin), 'a pin used lately stays (a rerun of an older run)')
        self.assertTrue(os.path.exists(other), "only sha folders are this cache's to remove")
        self.assertTrue(os.path.exists(self.dest))
        self.assertGreater(os.stat(current).st_mtime, now - 60, 'a use marks the folder used')

    def test_prune_of_a_missing_or_unreadable_cache_never_fails(self):
        pinned.prune(os.path.join(self.cache, 'nope'), SHA)
        pinned.prune(self.cfg, SHA)   # a file, not a folder


class LensFindings(Case):
    """PR 94 lens findings (minors): files() checks the config, names end at the name, the cache stays private."""
    def test_files_checks_repo_and_full_sha_before_any_fetch(self):
        for c in ({'repo': 'owner/repo', 'sha': 'main', 'files': self.FILES},          # a branch name, not a sha
                  {'repo': '../..', 'sha': SHA, 'files': self.FILES},                 # dot segments in the URL path
                  {'sha': SHA, 'files': self.FILES}, ['not', 'an', 'object'],         # tracebacks before
                  {'repo': 'owner/repo', 'sha': SHA, 'files': ['docs-scope/docs_scope.py']}):
            with self.subTest(config=c):
                self.config(c)
                with self.serve(), self.assertRaises(pinned.FetchError):
                    pinned.files(self.cfg)
                out = io.StringIO()
                with self.serve(), contextlib.redirect_stderr(out):
                    self.assertEqual(pinned.main(['--config', self.cfg, 'files']), 1)
                self.assertTrue(out.getvalue().startswith('pinned: '), out.getvalue())
        self.assertEqual(self.urls, [])

    def test_pin_refuses_dot_segment_repos(self):
        for repo in ('../..', './x', 'x/..'):
            with self.subTest(repo=repo):
                self.config({'repo': repo, 'sha': SHA, 'files': self.FILES})
                with self.serve(), self.assertRaises(pinned.FetchError):
                    self.path()
        self.assertEqual(self.urls, [])

    def test_a_name_with_a_trailing_newline_is_not_plain(self):
        self.assertIsNone(pinned.NAME.match('docs_scope.py\n'))
        with self.serve(), self.assertRaises(pinned.FetchError):
            pinned.path(self.cfg, 'docs-scope', 'docs_scope.py\n', self.cache)

    def test_prune_never_follows_a_symlink_out_of_the_cache(self):
        outside = os.path.join(self.root, 'outside')
        victim = self.put(os.path.join(outside, '.part-keepme'), b'', age=7200)
        os.symlink(outside, os.path.join(self.cache, 'b' * 40))
        pinned.prune(self.cache, SHA)
        self.assertTrue(os.path.exists(victim), 'prune followed a symlinked sha folder out of the cache')

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFOs')
    def test_a_fifo_in_place_of_the_cached_file_is_fetched_over_not_opened(self):
        os.makedirs(os.path.dirname(self.dest))
        os.mkfifo(self.dest)   # opening it to read would wait for a writer forever
        with self.serve():
            self.assertEqual(self.path(), self.dest)
        with open(self.dest, 'rb') as f:
            self.assertEqual(f.read(), BODY)

    @unittest.skipUnless(hasattr(os, 'getuid'), 'POSIX owners and modes')
    def test_our_own_group_writable_cache_is_refused_not_chmodded(self):   # #314 item 3: the bootstrap's rule
        os.chmod(self.cache, 0o775)   # someone in the group could already have put something in it
        for _ in range(2):   # the same answer on the next run: nothing fetched, written or chmodded
            with self.serve(), self.assertRaisesRegex(pinned.FetchError, 'cache folder .* remove it'):
                self.path()
            self.assertEqual(self.urls, [])
            self.assertEqual(os.listdir(self.cache), [])
            self.assertEqual(os.stat(self.cache).st_mode & 0o777, 0o775)

    @unittest.skipUnless(hasattr(os, 'getuid'), 'POSIX owners and modes')
    def test_a_cache_another_user_owns_is_refused(self):
        with self.serve(), mock.patch.object(pinned.os, 'getuid', lambda: os.stat(self.cache).st_uid + 1), \
                self.assertRaises(pinned.FetchError):
            self.path()
        self.assertEqual(self.urls, [])


class Neighbours(Case):
    """#96: load() and run run one checked file. A module that loads its neighbours (pr-queue/pr_queue.py puts its
    folder on sys.path and imports config and lens) would run unchecked files from the cache: refused."""
    BODY = (b'import os, sys\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n'
            b'import config  # noqa\n')

    def setUp(self):
        super().setUp()
        self.config({'repo': 'owner/repo', 'sha': SHA, 'files': {'pr-queue/pr_queue.py': pinned.blob_id(self.BODY)}})
        self.marker = os.path.join(self.root, 'planted-ran')
        self.put(os.path.join(self.cache, SHA, 'pr-queue', 'config.py'),   # planted next to it, never checked
                 f'open({self.marker!r}, "w").close()\n'.encode())
        path, mods = list(sys.path), dict(sys.modules)
        self.addCleanup(lambda: (sys.path.__setitem__(slice(None), path),
                                 [sys.modules.pop(m) for m in set(sys.modules) - set(mods)]))

    def test_load_refuses_a_module_that_imports_a_neighbour(self):
        for _ in range(2):   # the same answer from a cache hit
            with self.serve(self.BODY), self.assertRaisesRegex(pinned.FetchError, 'sys.path'):
                pinned.load(self.cfg, 'pr-queue', 'pr_queue', self.cache)
            self.assertFalse(os.path.exists(self.marker), 'the planted neighbour never ran')

    def test_run_refuses_it_before_running_anything(self):
        runs = []
        with self.serve(self.BODY), mock.patch.object(pinned.subprocess, 'run', lambda *a, **k: runs.append(a)), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(pinned.main(['--config', self.cfg, '--cache', self.cache,
                                          'run', 'pr-queue', 'pr_queue.py']), 1)
        self.assertEqual(runs, [])
        self.assertIn('sys.path', err.getvalue())

    def test_what_loads_a_neighbour(self):
        for body, found in ((b'import os, yaml, config\nfrom os import path\nfrom __future__ import annotations\n', []),
                            (b'import sys\nsys.path.insert(0, "x")\nimport lens\n', ['sys.path']),
                            (b'import sys\nprint(sys.path)\n', ['sys.path']), (b'from sys import path\n', ['sys.path']),
                            (b'from . import lens\n', ['.']), (b'from .lens import x\n', ['.lens']),
                            (b'import importlib.util\n', ['importlib.util']),
                            (b'def f():\n    import runpy\n', ['runpy']), (b'not python(\n', ['(does not parse)']),
                            # #314 item 4: rows that no test pinned
                            (b'from sys import *\npath.insert(0, "x")\n', ['sys.path']),
                            (b'import pkgutil\n', ['pkgutil']), (b'import imp\n', ['imp']),
                            (b'from zipimport import zipimporter\n', ['zipimport']),
                            # still self-contained: sys for its other names, a module's __file__ attribute
                            (b'import sys\nsys.exit(sys.argv[1:] and 0)\nprint(sys.version_info, file=sys.stderr)\n', []),
                            (b'import types\nm = types.ModuleType("m")\nm.__file__ = "x"\n', [])):
            with self.subTest(body=body):
                self.assertEqual(pinned.neighbour_loads(body), found)

    def test_other_spellings_that_reach_a_neighbour(self):   # #314 item 2: each returned [] before
        for body in (b'import sys as s\ns.path.insert(0, "x")\n', b'import sys\ngetattr(sys, "path").insert(0, "x")\n',
                     b'import sys\nsys.modules["sys"].path.insert(0, "x")\n', b'from sys import path_hooks\n',
                     b'from sys import meta_path\n', b'import sys\nsys.meta_path.append(1)\n',
                     b'import sys\nsys.path_importer_cache.clear()\n', b'import sys\ns = sys\ns.path.insert(0, "x")\n',
                     b'import sys\nvars(sys)["path"].insert(0, "x")\n',
                     b'import sys\nsys.__dict__["path"].insert(0, "x")\n', b'import os.path, sys as s2\nprint(s2.path)\n',
                     b'import site, os\nsite.addsitedir(os.path.dirname(os.path.abspath(__file__)))\n',
                     b'import site\n', b'import ctypes\n', b'import pkg_resources\n',
                     b'__import__("importlib")\n', b'import builtins\nbuiltins.__import__("importlib")\n',
                     b'import os\nexec(open(os.path.join(os.path.dirname(__file__), "x.py")).read())\n',
                     b'import os, subprocess, sys\nsubprocess.run([sys.executable, os.path.join(os.path.dirname('
                     b'__file__), "x.py")])\n',
                     b'import os\nos.chdir(os.path.dirname(os.path.abspath(__file__)))\nimport lens\n',
                     b'import os\nopen(os.path.join(os.path.dirname(__file__), "data.json"))\n',
                     b'print(__spec__.origin)\n', b'print(__loader__.path)\n',
                     b'print(__cached__)\n'):   # #331 item 3: no row pinned it
            with self.subTest(body=body):
                self.assertNotEqual(pinned.neighbour_loads(body), [])

    def test_the_usual_spellings_of_sys_and_the_own_path(self):   # #331 item 2: each returned [] before
        for body in (b'import os\nos.sys.path.insert(0, "x")\n', b'from os import sys as s\ns.path.insert(0, "x")\n',
                     b'from os import sys\nsys.meta_path.append(1)\n', b'import os\nprint(os.sys)\n',
                     b'import os, sys\nprint(os.path.dirname(sys.argv[0]))\n',
                     b'from sys import argv\nprint(argv[0])\n',
                     b'import __main__\nprint(__main__.__file__)\n', b'from __main__ import x\n',
                     b'import inspect\nprint(inspect.getfile(inspect.currentframe()))\n',
                     b'import sys\nprint(sys._getframe().f_code.co_filename)\n',
                     b'__builtins__.__import__("runpy")\n', b'getattr(__builtins__, "__imp" + "ort__")("runpy")\n',
                     b'import sys\nprint(sys._current_frames())\n'):   # #339 item 4: no row pinned it
            with self.subTest(body=body):
                self.assertNotEqual(pinned.neighbour_loads(body), [])
        for body in (b'import os\nprint(os.sys.version, os.sys.argv[1:])\n',   # still self-contained
                     b'import sys\nprint(sys.argv[1])\n', b'args = [0]\nprint(args[0])\n'):
            with self.subTest(body=body):
                self.assertEqual(pinned.neighbour_loads(body), [])

    def test_the_run_folder_is_inside_the_cache_so_every_dot_dot_is_ours(self):
        # #331 item 1: with the run folder in the shared temp folder, `../..` of the copy was that temp folder again,
        # so `<tmp>/pr-queue/planted.py` (any local user can make it) ran. Inside the checked cache, every `..` is
        # the run folder, the 0700 cache, or a folder above it, which the README requires to be ours.
        tmp = os.path.join(self.root, 'tmp')   # stands for the shared /tmp
        os.mkdir(tmp)
        self.put(os.path.join(tmp, 'pr-queue', 'planted.py'), f'open({self.marker!r}, "w").close()\n'.encode())
        body = (b'import os, sys\nd = os.path.dirname(os.path.abspath(sys.argv[0]))\n'
                b'sys.path.insert(0, os.path.join(d, "..", "..", "pr-queue"))\nimport planted\n')
        self.config({'repo': 'owner/repo', 'sha': SHA, 'files': {'head-check/head_check.py': pinned.blob_id(body)}})
        seen = []

        def runner(cmd):
            seen.append(cmd[2])
            return subprocess.run(cmd, capture_output=True)
        for _ in range(2):   # a second run works the same and leaves nothing either
            with self.serve(body), mock.patch.object(pinned.tempfile, 'tempdir', tmp), \
                    mock.patch.object(pinned, 'neighbour_loads', lambda data: []):   # a spelling the check misses
                code = pinned.run(self.cfg, 'head-check', 'head_check.py', cache=self.cache, runner=runner)
            self.assertNotEqual(code, 0, 'the import fails: there is no pr-queue in the cache')
            self.assertFalse(os.path.exists(self.marker), 'the planted neighbour in the shared temp folder never ran')
        for script in seen:
            run_dir = os.path.dirname(os.path.dirname(script))
            self.assertEqual(os.path.dirname(run_dir), self.cache, 'the run folder sits in the checked cache')
            self.assertFalse(os.path.exists(run_dir), 'the run folder goes after the run')
        self.assertEqual(os.listdir(tmp), ['pr-queue'], 'nothing is made in the shared temp folder')
        self.assertEqual(sorted(os.listdir(self.cache)), [SHA])

    def test_a_run_folder_a_killed_run_left_goes_after_cache_days(self):   # #331 item 1: SIGKILL skips the finally
        now = time.time()
        old, live = (os.path.join(self.cache, x) for x in ('.run-killed', '.run-live'))
        for folder, age in ((old, pinned.CACHE_DAYS * 86400 + 60), (live, 60)):
            self.put(os.path.join(folder, 'head-check', 'head_check.py'), b'x', age, now)
            os.utime(folder, (now - age, now - age))
        pinned.prune(self.cache, SHA, now)   # what every use runs first
        self.assertFalse(os.path.exists(old), 'a run folder unused for CACHE_DAYS goes')
        self.assertTrue(os.path.exists(live), 'a run that may still go on keeps its folder')

    def test_run_copies_into_a_nested_private_folder_so_dot_dot_stays_private(self):
        # #314 item 2: whatever the static check misses, `..` of the run copy is our own 0700 run folder, never the
        # shared temp folder: on main, `<tmp>/pr-queue/planted.py` (any local user can make it) ran.
        tmp = os.path.join(self.root, 'tmp')   # stands for the shared /tmp
        os.mkdir(tmp)
        self.put(os.path.join(tmp, 'pr-queue', 'planted.py'), f'open({self.marker!r}, "w").close()\n'.encode())
        body = (b'import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", '
                b'"pr-queue"))\nimport planted\n')
        self.config({'repo': 'owner/repo', 'sha': SHA, 'files': {'head-check/head_check.py': pinned.blob_id(body)}})
        seen = []

        def runner(cmd):
            up = os.path.dirname(os.path.dirname(cmd[2]))
            seen.append((up, os.stat(up).st_mode & 0o777))
            return subprocess.run(cmd, capture_output=True)
        with self.serve(body), mock.patch.object(pinned.tempfile, 'tempdir', tmp), \
                mock.patch.object(pinned, 'neighbour_loads', lambda data: []):   # a spelling the check misses
            code = pinned.run(self.cfg, 'head-check', 'head_check.py', cache=self.cache, runner=runner)
        self.assertNotEqual(code, 0, 'the import fails: there is no pr-queue in the private run folder')
        self.assertFalse(os.path.exists(self.marker), 'the planted neighbour in the shared temp folder never ran')
        (up, mode), = seen
        self.assertNotEqual(up, tmp)
        self.assertEqual(mode, 0o700)
        self.assertFalse(os.path.exists(up), 'the run folder goes after the run')

    def test_more_spellings_of_a_path_loader_sys_and_the_own_path(self):   # #339 item 2: each returned [] before
        for body in (# the private twins of BY_PATH and two more path loaders
                     b'import _frozen_importlib_external as f\nf.SourceFileLoader("c", "x.py").load_module()\n',
                     b'import _frozen_importlib\n', b'import _imp\n', b'from _ctypes import dlopen\n',
                     b'import pydoc\npydoc.importfile("x.py")\n',
                     b'import unittest\nunittest.defaultTestLoader.discover(".")\n',
                     # sys names: from-imports beyond a short list, argv used whole, the command line, frames
                     b'from sys import argv as a\nprint(a[0])\n', b'from sys import _getframe\n',
                     b'from sys import modules\n', b'from sys import orig_argv\n',
                     b'import sys\nprint(sys.orig_argv[-1])\n',
                     b'import sys\nprint(sys.argv[:1][0])\n', b'import sys\np, *_ = sys.argv\n',
                     b'import sys\nsys.argv.pop(0)\n',
                     b'import sys\nprint(sys.argv[0:][0])\n', b'import sys\nprint(list(sys.argv))\n',   # #345 minor 3
                     b'import sys\nprint(sys.argv[-1])\n', b'from sys import argv\nprint(argv[-1])\n',
                     b'import sys\nprint(sys.exc_info()[2].tb_frame)\n',
                     b'import os\ngetattr(os, "sys").path.insert(0, "x")\n',
                     b'import os\nos.__dict__["sys"].path.insert(0, "x")\n',
                     # the own path with no listed name
                     b'print((lambda: 0).__code__.co_filename)\n',
                     b'try:\n    1 / 0\nexcept Exception as e:\n'
                     b'    print(e.__traceback__.tb_frame.f_code.co_filename)\n',
                     b'def f(): pass\nprint(f.__globals__["x"])\n',
                     b'print(globals()["x"])\n', b'print(locals())\n', b'print(vars()["x"])\n',
                     b'import traceback\nprint(traceback.extract_stack()[-1].filename)\n',
                     b'import doctest\ndoctest.testfile("../x.txt")\n',   # #364 item 2: a path loader
                     b'from doctest import testfile\n'):
            with self.subTest(body=body):
                self.assertNotEqual(pinned.neighbour_loads(body), [])
        for body in (b'import sys\nprint(sys.argv[1:], sys.argv[2], len(sys.argv))\n',   # still self-contained
                     b'from sys import argv, exit, stderr\nprint(argv[1:], file=stderr)\nexit(0)\n',
                     b'import os\nprint(getattr(os, "O_NOFOLLOW", 0), vars(os) is not None)\n',
                     b'import types\nm = types.ModuleType("m")\nexec(compile("x = 1", "m", "exec"), m.__dict__)\n'):
            with self.subTest(body=body):
                self.assertEqual(pinned.neighbour_loads(body), [])

    def test_a_folder_a_killed_run_left_is_pruned_by_the_same_prefix(self):   # #339 item 3: run's and prune's prefix
        seen = []

        def runner(cmd):
            seen.append(cmd[2])
            return subprocess.CompletedProcess(cmd, 0)
        self.config({'repo': 'owner/repo', 'sha': SHA, 'files': self.FILES})
        with self.serve(), mock.patch.object(pinned.shutil, 'rmtree', lambda *a, **k: None):   # SIGKILL: no finally
            self.assertEqual(pinned.run(self.cfg, 'docs-scope', 'docs_scope.py', cache=self.cache, runner=runner), 0)
        run_dir = os.path.dirname(os.path.dirname(seen[0]))
        self.assertTrue(os.path.isdir(run_dir), 'the killed run left its folder')
        now = time.time()
        pinned.prune(self.cache, SHA, now)
        self.assertTrue(os.path.isdir(run_dir), 'a young run folder stays (the run may still go on)')
        old = now - pinned.CACHE_DAYS * 86400 - 60
        os.utime(run_dir, (old, old))
        pinned.prune(self.cache, SHA, now)
        self.assertFalse(os.path.exists(run_dir), "prune knows the folder a real run makes")
        self.assertEqual(os.listdir(self.cache), [SHA])

    def test_the_fork_ci_modules_today(self):   # the ones a project loads pass; the ones with neighbours do not
        actions = os.path.dirname(HERE)
        for rel, ok in (('pinned/pinned.py', True), ('docs-scope/docs_scope.py', True),
                        ('gradle-cache/gradle_cache.py', True), ('ci-demand/ci_demand.py', True),
                        ('job-changed/job_changed.py', True), ('pr-queue/pr_queue.py', False),
                        ('head-check/head_check.py', False), ('head-check/head_gate.py', False),
                        ('fleet-watch/fleet_watch.py', False), ('test-select/test_select.py', False),
                        ('test-select/trace_check.py', False)):
            with self.subTest(rel), open(os.path.join(actions, rel), 'rb') as f:
                self.assertEqual(pinned.neighbour_loads(f.read()) == [], ok)


class Above(Case):
    """#339 item 1: run's copy is <cache>/.run-X/<action>/<file>, so `../../..` is the folder above the cache. With
    --cache <a shared sticky folder>/fci, a script that goes three levels up (fleet_watch.py does) ran a planted
    <shared>/pr-queue/config.py. run now refuses a cache with a folder above it that someone else could write."""
    TRUST_ABOVE = False

    def setUp(self):
        super().setUp()
        self.root = os.path.realpath(self.root)   # the real path, as the check sees it (/tmp is a symlink on macOS)
        self.cfg = os.path.join(self.root, 'fork-ci.json')
        self.marker = os.path.join(self.root, 'planted-ran')
        self.shared = os.path.join(self.root, 'shared')   # stands for /tmp
        os.mkdir(self.shared)
        os.chmod(self.shared, 0o1777)
        self.put(os.path.join(self.shared, 'pr-queue', 'config.py'), f'open({self.marker!r}, "w").close()\n'.encode())
        self.body = (b'import os, sys\nd = os.path.dirname(os.path.abspath(sys.argv[0]))\n'
                     b'sys.path.insert(0, os.path.join(d, "..", "..", "..", "pr-queue"))\nimport config\n')
        self.config({'repo': 'owner/repo', 'sha': SHA,
                     'files': {'fleet-watch/fleet_watch.py': pinned.blob_id(self.body)}})
        self.runs = []

    def runner(self, cmd):
        self.runs.append(cmd[2])
        return subprocess.run(cmd, capture_output=True)

    def run_it(self, cache):
        with self.serve(self.body), mock.patch.object(pinned, 'neighbour_loads', lambda data: []):   # a missed spelling
            return pinned.run(self.cfg, 'fleet-watch', 'fleet_watch.py', cache=cache, runner=self.runner)

    @unittest.skipUnless(hasattr(os, 'getuid'), 'owners and modes')
    def test_a_cache_in_a_shared_folder_is_refused_before_anything_runs(self):
        cache = os.path.join(self.shared, 'fci')
        for _ in range(2):   # the same answer the second time (the cache folder is already there)
            with self.private_above(), self.assertRaisesRegex(pinned.FetchError, 'above the cache') as e:
                self.run_it(cache)
            self.assertIn(self.shared, str(e.exception))
            with self.private_above():
                self.assertEqual(pinned.shared_above(cache), [self.shared])
            self.assertEqual((self.runs, self.urls), ([], []), 'nothing fetched, nothing run')
            self.assertFalse(os.path.exists(self.marker), 'the planted file never ran')

    @unittest.skipUnless(hasattr(os, 'getuid'), 'owners and modes')
    def test_which_folders_above_are_refused(self):
        def chain(name, mode):
            top = os.path.join(self.root, name)
            os.makedirs(os.path.join(top, 'fci'), 0o700)
            os.chmod(top, mode)
            return os.path.join(top, 'fci')
        link = os.path.join(self.root, 'link')   # our own folder, but a symlink into the shared one
        os.mkdir(os.path.join(self.shared, 'x'), 0o700)
        os.symlink(os.path.join(self.shared, 'x'), link)
        os.mkdir(os.path.join(link, 'fci'), 0o700)
        with self.private_above(), self.lookups(os.stat(self.root).st_gid, name='staff'):   # a shared group: 0775 is
            for name, mode, bad in (('a', 0o700, False), ('b', 0o755, False), ('c', 0o775, True),   # refused (own: below)
                                    ('d', 0o757, True), ('e', 0o1777, True)):
                with self.subTest(oct(mode)):
                    top = os.path.join(self.root, name)
                    self.assertEqual(pinned.shared_above(chain(name, mode)), [top] if bad else [])
            self.assertEqual(pinned.shared_above(os.path.join(link, 'fci')), [self.shared], 'by its real path too')
            self.assertEqual(pinned.shared_above(self.shared), [], 'only the folders above: the cache is private()')
            uid = os.getuid()
            with mock.patch.object(os, 'getuid', lambda: uid + 1):   # every folder of the test is another user's
                self.assertIn(self.root, pinned.shared_above(os.path.join(self.root, 'a', 'fci')))

    @unittest.skipUnless(hasattr(os, 'getuid'), 'owners and modes')
    def test_a_private_cache_runs_and_its_missing_parents_are_made_private(self):
        old = os.umask(0o002)   # a missing parent made 0775 would be refused on every run after
        self.addCleanup(os.umask, old)
        cache = os.path.join(self.root, 'home', '.cache', 'fci')
        os.mkdir(os.path.join(self.root, 'home'), 0o700)
        for _ in range(2):
            with self.private_above():
                self.assertNotEqual(self.run_it(cache), 0, 'the import fails: there is no pr-queue above the cache')
        self.assertEqual(len(self.runs), 2)
        self.assertEqual(os.stat(os.path.dirname(cache)).st_mode & 0o777, 0o700)
        self.assertFalse(os.path.exists(self.marker))

    @unittest.skipUnless(hasattr(os, 'getuid'), 'owners and modes')
    def test_run_without_a_cache_puts_its_folder_in_the_default_cache(self):   # #339 item 3: not $TMPDIR
        tmp = os.path.join(self.root, 'tmp')
        os.mkdir(tmp, 0o700)
        with mock.patch.dict(os.environ, {'XDG_CACHE_HOME': os.path.join(self.root, 'xdg')}), self.private_above(), \
                mock.patch.object(pinned.tempfile, 'tempdir', tmp):
            self.assertNotEqual(self.run_it(None), 0)
            default = pinned.default_cache()
        (script,) = self.runs
        self.assertEqual(os.path.dirname(os.path.dirname(os.path.dirname(script))), default)
        self.assertEqual(os.listdir(tmp), [], 'nothing in the temp folder')
        self.assertEqual(os.listdir(default), [SHA])


    def lookups(self, gid, name='me', primary=None, members=(), other_primary=None, no_group=False, fails=None):
        """Fakes the user and group lookups (no real user or group is made): you are `me` with primary gid `primary`
        (default `gid`); group `gid` is `name` with `members`; one other user has primary gid `other_primary`.
        fails=(lookup name, exception): that lookup raises it."""
        uid = os.getuid()
        me = pwd.struct_passwd(('me', 'x', uid, gid if primary is None else primary, '', '/home/me', '/bin/sh'))
        other = pwd.struct_passwd(('other', 'x', uid + 1, gid + 1 if other_primary is None else other_primary, '',
                                   '/home/other', '/bin/sh'))

        def getgrgid(g):
            if no_group or g != gid:
                raise KeyError(f'getgrgid(): gid not found: {g}')
            return grp.struct_group((name, 'x', gid, list(members)))

        def getpwuid(u):
            if u != uid:
                raise KeyError(f'getpwuid(): uid not found: {u}')
            return me
        fakes = {'getgrgid': getgrgid, 'getpwuid': getpwuid, 'getpwall': lambda: [me, other]}
        if fails:
            def raises(*a):
                raise fails[1]
            fakes[fails[0]] = raises
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(grp, 'getgrgid', fakes['getgrgid']))
        stack.enter_context(mock.patch.object(pwd, 'getpwuid', fakes['getpwuid']))
        stack.enter_context(mock.patch.object(pwd, 'getpwall', fakes['getpwall']))
        return stack

    def as_roots(self, folder):
        """os.stat (as it is now: enter it inside private_above) says folder is root's, as /tmp is."""
        real = os.stat

        def stat_(p, *a, **k):
            st = real(p, *a, **k)
            return os.stat_result((st.st_mode, *st[1:4], 0, *st[5:])) if os.fspath(p) == folder else st
        return mock.patch.object(os, 'stat', stat_)

    @unittest.skipUnless(hasattr(os, 'getuid'), 'owners and modes')
    def test_a_group_writable_folder_of_your_own_private_group_is_accepted(self):   # #345 M1 (b)
        # Debian/Ubuntu: each user has a private group and umask 002, so ~/.cache is often 0775. Debian's OpenSSH
        # (StrictModes) trusts such a folder of yours; run did not, and refused the default cache forever.
        parent = os.path.join(self.root, 'home', '.cache')
        os.mkdir(os.path.dirname(parent), 0o700)   # one by one: makedirs makes the middle one with the umask
        os.mkdir(parent, 0o700)
        os.chmod(parent, 0o775)
        gid, cache = os.stat(parent).st_gid, os.path.join(parent, 'fci')
        for _ in range(2):   # and on the next run
            with self.private_above(), self.lookups(gid):
                self.assertEqual(pinned.shared_above(cache), [])
                self.assertNotEqual(self.run_it(cache), 0, 'it ran: the import fails, no pr-queue above the cache')
        self.assertEqual(len(self.runs), 2)
        self.assertEqual(os.stat(parent).st_mode & 0o7777, 0o775, 'never chmodded')
        uid = os.getuid()
        rows = (('a group with another name', dict(name='staff')), ('a group with a member', dict(members=['other'])),
                ('a group with you as a listed member', dict(members=['me'])),
                ("another user's primary group too", dict(other_primary=gid)),
                ('not your primary group', dict(primary=gid + 1)), ('no such group', dict(no_group=True)))
        for what, kw in rows:
            with self.subTest(what), self.private_above(), self.lookups(gid, **kw):
                self.assertEqual(pinned.shared_above(cache), [parent])
                with self.assertRaises(pinned.FetchError) as e:
                    self.run_it(cache)
                self.assertIn(f'chmod g-w {parent}', str(e.exception), 'the fix, for that folder')
                self.assertNotIn('--cache', str(e.exception))
        os.chmod(parent, 0o777)
        with self.subTest('world-writable'), self.private_above(), self.lookups(gid):
            self.assertEqual(pinned.shared_above(cache), [parent])
            with self.assertRaisesRegex(pinned.FetchError, re.escape(f'chmod go-w {parent}')):
                self.run_it(cache)
        os.chmod(parent, 0o775)
        with self.subTest("root's"), self.private_above(), self.as_roots(parent), self.lookups(gid):
            self.assertEqual(pinned.shared_above(cache), [parent], 'only a folder of yours may be group-writable')
        self.assertEqual(len(self.runs), 2, 'nothing refused ran')
        self.assertEqual(self.urls, [f'{RAW}/fleet-watch/fleet_watch.py'], 'fetched once, by the first run')
        self.assertEqual(os.getuid(), uid)

    def private_group_folder(self):
        """A 0700 home with a .cache in it (made 0700, one by one: makedirs makes the middle one with the umask)."""
        parent = os.path.join(self.root, 'home', '.cache')
        os.mkdir(os.path.dirname(parent), 0o700)
        os.mkdir(parent, 0o700)
        return parent, os.path.join(parent, 'fci')

    @staticmethod
    def posix_acl(*entries):
        """The system.posix_acl_access value (Linux's xattr format, version 2) of (tag, perm, id) entries."""
        return struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *e) for e in entries)

    @unittest.skipUnless(hasattr(os, 'setxattr'), 'POSIX ACLs (Linux)')
    def test_a_folder_whose_acl_gives_another_user_write_is_refused(self):   # #345 re-check: the ACL mask
        # A 0755 folder of yours plus `setfacl -m u:<another user>:rwx` shows 0775: the group bits are the ACL mask,
        # not the group's. Its group is your private one, so the group bits alone said "only you".
        parent, cache = self.private_group_folder()
        os.chmod(parent, 0o755)
        undefined, other = 0xFFFFFFFF, os.getuid() + 1
        acl = self.posix_acl((0x01, 7, undefined), (0x02, 7, other), (0x04, 5, undefined), (0x10, 7, undefined),
                             (0x20, 5, undefined))
        try:
            os.setxattr(parent, 'system.posix_acl_access', acl)
        except OSError as e:
            self.skipTest(f'this file system refuses POSIX ACLs ({e}); test_an_acl_faked covers it')
        self.assertEqual(os.stat(parent).st_mode & 0o7777, 0o775, 'the mask shows as the group bits')
        gid = os.stat(parent).st_gid
        for _ in range(2):   # and on the next run
            with self.private_above(), self.lookups(gid):
                self.assertEqual(pinned.shared_above(cache), [parent])
                with self.assertRaises(pinned.FetchError) as e:
                    self.run_it(cache)
                self.assertIn(f'setfacl -b {parent}', str(e.exception), 'the fix, for that folder')
                self.assertIn(f'chmod g-w {parent}', str(e.exception))
        os.removexattr(parent, 'system.posix_acl_access')   # setfacl -b
        with self.private_above(), self.lookups(gid):
            self.assertEqual(pinned.shared_above(cache), [], 'without the ACL, your private group is yours')
        self.assertEqual((self.runs, self.urls), ([], []), 'nothing refused was fetched or ran')

    @unittest.skipUnless(hasattr(os, 'getuid'), 'owners and modes')
    def test_an_acl_faked(self):   # #345 re-check: CI covers the ACL rule even where the file system has no ACLs
        parent, cache = self.private_group_folder()
        os.chmod(parent, 0o775)
        gid = os.stat(parent).st_gid

        def listing(names=(), error=None):
            def listxattr(p, *a, **k):
                if error:
                    raise error
                return list(names) if os.fspath(p) == parent else []
            return mock.patch.object(os, 'listxattr', listxattr, create=True)
        rows = (('a POSIX ACL', listing(['user.x', 'system.posix_acl_access']), 'setfacl -b'),
                ('an NFSv4 ACL', listing(['system.nfs4_acl']), 'setfacl -b'),
                ('listxattr fails', listing(error=OSError(95, 'Operation not supported')), 'cannot tell'),
                ('listxattr cannot read it', listing(error=PermissionError(13, 'Permission denied')), 'cannot tell'),
                ('no listxattr here', mock.patch.object(os, 'listxattr', None, create=True), 'cannot tell'))
        for what, xattrs, why in rows:
            with self.subTest(what), self.private_above(), self.lookups(gid), xattrs:
                self.assertEqual(pinned.shared_above(cache), [parent])
                with self.assertRaises(pinned.FetchError) as e:
                    self.run_it(cache)
                self.assertIn(why, str(e.exception))
                self.assertIn(f'chmod g-w {parent}', str(e.exception), 'the fix, for that folder')
        with self.subTest('other xattrs, no ACL'), self.private_above(), self.lookups(gid), listing(['user.x']):
            self.assertEqual(pinned.shared_above(cache), [])
        os.chmod(parent, 0o755)
        with self.subTest('an ACL on a folder the group bits (the mask) keep unwritable'), self.private_above(), \
                self.lookups(gid), listing(['system.posix_acl_access']):
            self.assertEqual(pinned.shared_above(cache), [], 'the mask caps every named entry')
        self.assertEqual((self.runs, self.urls), ([], []))

    @unittest.skipUnless(hasattr(os, 'getuid'), 'owners and modes')
    def test_any_failed_group_lookup_is_a_clean_refusal(self):   # #345 re-check: a RuntimeError was a traceback
        parent, cache = self.private_group_folder()
        os.chmod(parent, 0o775)
        gid = os.stat(parent).st_gid
        for lookup in ('getgrgid', 'getpwuid', 'getpwall'):
            for error in (RuntimeError('nss'), KeyError(gid), OSError(5, 'I/O error')):
                with self.subTest(lookup=lookup, error=type(error).__name__), self.private_above(), \
                        self.lookups(gid, fails=(lookup, error)), mock.patch.object(os, 'listxattr', lambda p: []):
                    self.assertEqual(pinned.shared_above(cache), [parent])
                    with self.assertRaises(pinned.FetchError) as e:
                        self.run_it(cache)
                    self.assertIn(f'cannot tell if the group of {parent} is private', str(e.exception))
                    self.assertIn(f'chmod g-w {parent}', str(e.exception))
        self.assertEqual((self.runs, self.urls), ([], []))

    @unittest.skipUnless(hasattr(os, 'getuid'), 'owners and modes')
    def test_a_refused_cache_leaves_no_folders_behind(self):   # #345 minor 4: checked before private() makes any
        cache = os.path.join(self.shared, 'a', 'b', 'fci')
        for _ in range(2):
            with self.private_above(), self.as_roots(self.shared), \
                    self.assertRaisesRegex(pinned.FetchError, 'above the cache') as e:
                self.run_it(cache)
            self.assertIn('pass --cache', str(e.exception), "root's sticky folder (as /tmp): the fix is another cache")
            self.assertNotIn('chmod', str(e.exception))
            self.assertEqual(sorted(os.listdir(self.shared)), ['pr-queue'], 'no folder made')
        self.assertEqual((self.runs, self.urls), ([], []))

    def load_it(self, cache):
        """load() of a module that goes three levels up from its own __file__ (<cache>/<sha>/<action>/<file>)."""
        body = (b'import os, sys\nd = os.path.dirname(os.path.abspath(__file__))\n'
                b'sys.path.insert(0, os.path.join(d, "..", "..", "..", "pr-queue"))\nimport config\n')
        self.config({'repo': 'owner/repo', 'sha': SHA, 'files': {'fleet-watch/fleet_watch.py': pinned.blob_id(body)}})
        path = list(sys.path)   # a load that ran would leave its sys.path entry and the planted module behind
        self.addCleanup(sys.path.__setitem__, slice(None), path)
        self.addCleanup(sys.modules.pop, 'config', None)
        with self.serve(body), mock.patch.object(pinned, 'neighbour_loads', lambda data: []):   # a missed spelling
            return pinned.load(self.cfg, 'fleet-watch', 'fleet_watch', cache)

    @unittest.skipUnless(hasattr(os, 'getuid'), 'owners and modes')
    def test_load_refuses_a_cache_in_a_shared_folder_as_run_does(self):   # #364 item 1
        cache = os.path.join(self.shared, 'fci')
        for _ in range(2):   # the same answer the second time
            with self.private_above(), self.assertRaisesRegex(pinned.FetchError, 'above the cache') as e:
                self.load_it(cache)
            self.assertIn(self.shared, str(e.exception))
            self.assertIn('so load refuses this cache', str(e.exception))
            self.assertEqual(self.urls, [], 'nothing fetched')
            self.assertFalse(os.path.exists(self.marker), 'the planted file never ran')
            self.assertEqual(os.listdir(self.shared), ['pr-queue'], 'no folder made')
        cache = os.path.join(self.root, 'home', 'fci')   # a private cache: loaded (no pr-queue above it to import)
        os.mkdir(os.path.dirname(cache), 0o700)
        with self.private_above(), self.assertRaises(ModuleNotFoundError):
            self.load_it(cache)
        self.assertEqual(len(self.urls), 1)

    def locked(self):
        """A cache under a folder of ours with no x: os.stat of anything below it is EACCES."""
        locked = os.path.join(self.root, 'locked')
        os.makedirs(os.path.join(locked, 'a', 'b'), 0o700)   # two levels: the folder to name is not the cache's parent
        os.chmod(locked, 0o600)
        self.addCleanup(os.chmod, locked, 0o700)   # so the cleanup can remove it
        return locked, os.path.join(locked, 'a', 'b', 'fci')

    @unittest.skipUnless(hasattr(os, 'getuid'), 'owners and modes')
    def test_a_folder_above_the_cache_you_cannot_enter_is_a_clean_refusal(self):   # #364 item 3
        locked, cache = self.locked()
        for _ in range(2):   # the same answer the second time; nothing made in between
            for call in (self.run_it, self.load_it):
                with self.subTest(call.__name__), self.private_above(), \
                        self.assertRaises(pinned.FetchError) as e:   # a FetchError, never a raw PermissionError
                    call(cache)
                self.assertIn(f'{locked} cannot be entered', str(e.exception))
                self.assertIn(f'chmod u+x {locked}', str(e.exception))
            with self.private_above(), contextlib.redirect_stderr(io.StringIO()) as err:
                code = pinned.main(['--config', self.cfg, '--cache', cache, 'run', 'fleet-watch', 'fleet_watch.py'])
            self.assertEqual(code, 1)
            self.assertRegex(err.getvalue(), f'^pinned: .*{re.escape(locked)} cannot be entered')
            self.assertNotIn('Errno', err.getvalue())
        os.chmod(locked, 0o700)
        self.assertEqual(os.listdir(os.path.join(locked, 'a', 'b')), [], 'fail closed: no folder made, nothing fetched')
        self.assertEqual((self.runs, self.urls), ([], []))
        self.assertFalse(os.path.exists(self.marker))
        uid = os.getuid()
        os.chmod(locked, 0o600)
        with self.private_above(), mock.patch.object(os, 'getuid', lambda: uid + 1), \
                self.assertRaises(pinned.FetchError) as e:   # not yours: the fix is another cache
            pinned.shared_above(cache)
        self.assertIn('pass --cache', str(e.exception))
        self.assertNotIn('chmod', str(e.exception))

    def refused_before_anything(self, cache, *expect, absent=('chmod',)):
        """run and load (twice each: the same answer again) refuse this cache with a FetchError that says each of
        expect and none of absent ({how}: --cache for run, the cache argument for load: load never says --cache);
        nothing made, fetched or run."""
        made = sorted(os.listdir(self.root))
        for _ in range(2):
            for call, how in ((self.run_it, '--cache'), (self.load_it, 'the cache argument')):
                with self.subTest(call.__name__), self.private_above(), \
                        self.assertRaises(pinned.FetchError) as e:   # never a raw OSError
                    call(cache)
                for text in expect:
                    self.assertIn(text.format(how=how), str(e.exception))
                for text in absent:
                    self.assertNotIn(text.format(how=how), str(e.exception))
                if call == self.load_it:
                    self.assertNotIn('--cache', str(e.exception), 'load() is the Python API: it has no --cache')
        self.assertEqual(sorted(os.listdir(self.root)), made, 'no folder made')
        self.assertEqual((self.runs, self.urls), ([], []), 'nothing fetched, nothing run')
        self.assertFalse(os.path.exists(self.marker))

    @unittest.skipUnless(hasattr(os, 'getuid'), 'owners and modes')
    def test_through_a_symlink_the_folder_named_is_the_real_one_you_cannot_enter(self):   # #376 item 1 (#369 m1)
        # B/sl -> B/locked/a, B/locked 0600, cache B/sl/fci: the message named B (0700) and `chmod u+x B`, which
        # changes nothing. The folder without x is B/locked, reached through the link.
        b = os.path.join(self.root, 'B')
        locked = os.path.join(b, 'locked')
        os.makedirs(os.path.join(locked, 'a'), 0o700)
        os.symlink(os.path.join(locked, 'a'), os.path.join(b, 'sl'))
        os.chmod(locked, 0o600)
        self.addCleanup(os.chmod, locked, 0o700)
        self.refused_before_anything(os.path.join(b, 'sl', 'fci'), f'{locked} cannot be entered',
                                     f'Fix: chmod u+x {locked}', absent=(f'{b} cannot',))
        rel = os.path.join(self.root, 'C')   # a relative link to a link
        os.mkdir(rel, 0o700)
        os.symlink(os.path.join('..', 'B', 'sl'), os.path.join(rel, 'r1'))
        os.symlink('r1', os.path.join(rel, 'r2'))
        self.refused_before_anything(os.path.join(rel, 'r2', 'fci'), f'{locked} cannot be entered',
                                     f'Fix: chmod u+x {locked}', absent=(f'{rel} cannot',))
        os.chmod(locked, 0o700)
        self.assertEqual(os.listdir(os.path.join(locked, 'a')), [], 'nothing made under it')

    @unittest.skipUnless(hasattr(os, 'getuid'), 'owners and modes')
    def test_a_symlink_loop_above_the_cache_is_refused_with_its_own_message(self):   # #376 items 1, 2 (#369 m1, m2)
        # It said `B cannot be entered (Too many levels of symbolic links) ... chmod u+x B`. And narrowing the catch
        # to EACCES (other errors skip the folder) left every test green: private() then raised a raw OSError.
        b = os.path.join(self.root, 'B')
        os.mkdir(b, 0o700)
        loop = os.path.join(b, 'loop')
        os.symlink(loop, loop)
        self.refused_before_anything(os.path.join(loop, 'fci'), f'{loop} above the cache', 'symlink loop',
                                     'Fix: pass {how} with a folder', absent=('chmod', 'cannot be entered'))

    @unittest.skipUnless(hasattr(os, 'getuid'), 'owners and modes')
    def test_a_file_above_the_cache_is_a_clean_refusal(self):   # #376 item 2: an error other than EACCES (ENOTDIR)
        b = os.path.join(self.root, 'B')
        os.mkdir(b, 0o700)
        self.put(os.path.join(b, 'file'), b'')
        up = os.path.join(b, 'file', 'x')
        self.refused_before_anything(os.path.join(up, 'fci'), f'{up} above the cache', 'cannot be checked',
                                     'Fix: pass {how} with a folder', absent=('chmod', 'cannot be entered'))

    @unittest.skipUnless(hasattr(os, 'getuid'), 'owners and modes')
    def test_load_names_its_cache_argument_and_run_its_flag(self):   # #376 item 3 (#369 nit)
        uid = os.getuid()
        with mock.patch.object(os, 'getuid', lambda: uid + 1):   # every folder of the test is another user's
            self.refused_before_anything(os.path.join(self.root, 'home', 'fci'),
                                         'Fix: pass {how} with a folder whose every parent only you or root can write')
            locked, cache = self.locked()   # and one you cannot enter
            self.refused_before_anything(cache, f'{locked} cannot be entered', 'Fix: pass {how} with a folder whose '
                                         'every parent you can enter and only you or root can write')


class Run(Case):
    def test_run_executes_the_verified_bytes_even_if_the_cache_file_changes_after_the_check(self):
        body = b'print("good")\n'
        self.config({'repo': 'owner/repo', 'sha': SHA, 'files': {'gradle-cache/gradle_cache.py': pinned.blob_id(body)}})
        real, runs = pinned.verified, []

        def swapped(*a, **k):
            dest, data = real(*a, **k)
            with open(dest, 'wb') as f:
                f.write(b'print("swapped")\n')   # another job on the runner, after the check
            return dest, data

        def run(cmd, **k):   # what the child would read: the script file's bytes at run time
            with open(cmd[2], 'rb') as f:
                runs.append((cmd, f.read(), oct(os.stat(os.path.dirname(cmd[2])).st_mode & 0o777)))
            return subprocess.CompletedProcess(cmd, 7)
        with self.serve(body), mock.patch.object(pinned, 'verified', swapped), \
                mock.patch.object(pinned.subprocess, 'run', run):
            code = pinned.main(['--config', self.cfg, '--cache', self.cache,
                                'run', 'gradle-cache', 'gradle_cache.py', 'publish', '--x', '--config', 'y'])
        self.assertEqual(code, 7)
        (cmd, ran, mode), = runs
        self.assertEqual(ran, body)
        self.assertEqual(mode, '0o700', 'a private copy')
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1:2] + cmd[3:], ['-I', 'publish', '--x', '--config', 'y'])
        self.assertFalse(os.path.exists(cmd[2]), 'the private copy goes after the run')

    def test_run_really_runs_the_script_with_its_arguments(self):
        body = b'import sys\nprint("args", sys.argv[1:])\nsys.exit(3)\n'
        self.config({'repo': 'owner/repo', 'sha': SHA, 'files': {'tool/t.py': pinned.blob_id(body)}})
        with self.serve(body):
            r = subprocess.run([sys.executable, '-I', '-c', 'import sys, runpy; sys.argv = sys.argv[1:]; '
                                'import unittest.mock as m, io, os, urllib.request as u; '
                                f'm.patch.object(u, "urlopen", lambda *a, **k: io.BytesIO({body!r})).start(); '
                                # the folders above the test cache (the shared temp folder) as root's 0755 ones
                                'F = os.stat_result((0o40755, 0, 0, 2, 0, 0, 0, 0, 0, 0)); R = os.stat; '
                                f'os.stat = lambda p, *a, **k: F if {self.cache!r}.startswith(os.fspath(p).rstrip("/") '
                                '+ "/") else R(p, *a, **k); '
                                f'runpy.run_path({SCRIPT!r}, run_name="__main__")',
                                SCRIPT, '--config', self.cfg, '--cache', self.cache, 'run', 'tool', 't.py', 'a', 'b c'],
                               capture_output=True, text=True)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(r.stdout, "args ['a', 'b c']\n")

    def test_the_cli_fails_red_when_the_file_cannot_be_fetched(self):
        err = io.StringIO()
        with self.serve(error=OSError('offline')), contextlib.redirect_stderr(err):
            self.assertEqual(pinned.main(['--config', self.cfg, '--cache', self.cache, 'run', 'docs-scope',
                                          'docs_scope.py']), 1)
        self.assertIn('pinned: cannot fetch', err.getvalue())
        for argv in ([], ['--config', self.cfg, 'nope'], ['--config', self.cfg, 'run', 'docs-scope'],
                     ['--config', self.cfg, 'files', 'x'], ['run', 'docs-scope', 'docs_scope.py']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
                pinned.main(argv)
            self.assertEqual(e.exception.code, 2)

    def test_files_prints_the_map_at_the_configs_sha(self):
        other = b'Y = 2\n'
        self.config({'repo': 'owner/repo', 'sha': SHA, 'files': {'docs-scope/docs_scope.py': '0' * 40,
                                                                 'pinned/pinned.py': '0' * 40}})
        out = io.StringIO()
        with self.serve(bodies={f'{RAW}/docs-scope/docs_scope.py': BODY, f'{RAW}/pinned/pinned.py': other}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(pinned.main(['--config', self.cfg, 'files']), 0)
        self.assertEqual(json.loads(out.getvalue()), {'docs-scope/docs_scope.py': pinned.blob_id(BODY),
                                                      'pinned/pinned.py': pinned.blob_id(other)})
        self.config({'repo': 'owner/repo', 'sha': SHA, 'files': {'../x': '0' * 40}})
        with self.serve(), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(pinned.main(['--config', self.cfg, 'files']), 1)


class Bootstrap(Case):
    """The README's load_pinned, run as written: the one piece a project copies."""
    with open(SCRIPT, 'rb') as f:
        REAL = f.read()

    def setUp(self):
        super().setUp()
        with open(README) as f:
            block = re.search(r'<!-- bootstrap:start -->\s*```python\n(.*?)```\s*<!-- bootstrap:end -->', f.read(), re.S)
        self.assertTrue(block, 'README has the bootstrap block')
        self.ns = {}
        exec(compile(block[1], README, 'exec'), self.ns)
        self.config({'repo': 'owner/repo', 'sha': SHA,
                     'files': {'pinned/pinned.py': pinned.blob_id(self.REAL), **self.FILES}})
        self.pdest = os.path.join(self.cache, SHA, 'pinned', 'pinned.py')

    def boot(self, body=None, error=None):
        def urlopen(url, timeout=None):
            self.urls.append(url)
            if error:
                raise error
            return io.BytesIO(self.REAL if body is None else body)
        with mock.patch.object(self.ns['urllib'].request, 'urlopen', urlopen):
            p = self.ns['load_pinned'](self.cfg, self.cache)
        if self.TRUST_ABOVE:   # this copy is not the module Case patched: its caches sit under the shared temp folder
            above = mock.patch.object(p, 'shared_above', lambda cache, caller='run': [])
            above.start()
            self.addCleanup(above.stop)
        return p

    def test_it_loads_this_module_once_then_from_the_cache(self):
        p = self.boot()
        self.assertEqual(self.urls, [f'{RAW}/pinned/pinned.py'])
        with open(self.pdest, 'rb') as f:
            self.assertEqual(f.read(), self.REAL)
        with mock.patch.object(p.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(BODY)):
            self.assertEqual(p.load(self.cfg, 'docs-scope', 'docs_scope', self.cache).X, 1)
        self.boot(error=OSError('a hit needs no fetch'))
        self.assertEqual(len(self.urls), 1)

    def test_other_bytes_are_refused_and_not_kept(self):
        with self.assertRaises(ValueError):
            self.boot(body=self.REAL + b'#')
        self.assertFalse(os.path.exists(self.pdest))
        self.assertEqual(self.parts(), [])

    def test_a_cached_copy_that_differs_is_fetched_again(self):
        self.put(self.pdest, b'raise SystemExit("tampered")\n')
        self.boot()
        self.assertEqual(len(self.urls), 1)
        with open(self.pdest, 'rb') as f:
            self.assertEqual(f.read(), self.REAL)

    def test_no_network_and_no_copy_is_an_error(self):
        with self.assertRaises(OSError):
            self.boot(error=OSError('offline'))
        self.assertFalse(os.path.exists(self.pdest))

    def test_a_bad_config_is_refused_before_any_fetch(self):
        for c in ({'repo': 'owner/repo', 'sha': '../' * 13 + 'x', 'files': {'pinned/pinned.py': '0' * 40}},
                  {'repo': 'owner/repo', 'sha': SHA, 'files': {}}, {'repo': 'a b', 'sha': SHA},
                  ['not', 'an', 'object'], {'repo': '../..', 'sha': SHA, 'files': {'pinned/pinned.py': '0' * 40}}):
            with self.subTest(config=c):
                self.config(c)
                with self.assertRaises(ValueError):
                    self.boot()
        self.assertEqual(self.urls, [])


class UmaskAndFifo(Bootstrap):
    """Lens t3lens94q probes: the private-cache refusal in normal use."""
    def fresh(self):
        c = os.path.join(self.root, 'fresh-cache')   # does not exist yet: the README adapter's first run
        self.cache = c
        self.pdest = os.path.join(c, SHA, 'pinned', 'pinned.py')
        return c

    def test_readme_adapter_first_run_under_umask_002(self):
        # Stock Ubuntu/Debian user (USERGROUPS_ENAB yes): umask 002. The bootstrap makes the cache folder 0775,
        # then P.load (the README adapter's next line) refuses it as "not private" on this and every later run.
        self.fresh()
        old = os.umask(0o002)
        try:
            p = self.boot()
            with mock.patch.object(p.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(BODY)):
                self.assertEqual(p.load(self.cfg, 'docs-scope', 'docs_scope', self.cache).X, 1)   # red at the head
        finally:
            os.umask(old)

    @unittest.skipUnless(hasattr(os, 'getuid'), 'POSIX owners and modes')
    def test_an_existing_cache_of_our_own_made_under_umask_002(self):
        # The same layout made by a project's older tooling (os.makedirs, umask 002): 0775. #314 item 3: pinned.py
        # refuses it like the bootstrap (which the README adapter runs first) instead of chmodding it and going on.
        self.fresh()
        old = os.umask(0o002)
        try:
            os.makedirs(os.path.join(self.cache, SHA, 'docs-scope'))
        finally:
            os.umask(old)
        with self.serve(), self.assertRaisesRegex(pinned.FetchError, 'cache folder .* remove it'):
            self.path()
        with self.assertRaisesRegex(ValueError, 'cache folder .* remove it'):
            self.boot()
        self.assertEqual(self.urls, [])

    def test_a_symlink_at_dest_is_replaced_not_its_target(self):
        target = os.path.join(self.root, 'target'); open(target, 'wb').write(b'other')
        os.makedirs(os.path.dirname(self.dest)); os.symlink(target, self.dest)
        with self.serve():
            self.path()
        self.assertFalse(os.path.islink(self.dest))
        self.assertEqual(open(target, 'rb').read(), b'other')

    def test_prune_still_removes_real_old_folders_and_parts(self):
        now = time.time()
        old = self.put(os.path.join(self.cache, 'b' * 40, 'x', 'y.py'), b'', age=0)
        t = now - (pinned.CACHE_DAYS + 1) * 86400; os.utime(os.path.join(self.cache, 'b' * 40), (t, t))
        part = self.put(os.path.join(self.cache, SHA, 'x', '.part-1'), b'', age=7200)
        pinned.prune(self.cache, SHA, now)
        self.assertFalse(os.path.exists(old)); self.assertFalse(os.path.exists(part))


@unittest.skipUnless(hasattr(os, 'getuid'), 'POSIX owners and modes')
class BootstrapCacheFolder(Bootstrap):
    """#96: the bootstrap checks the cache folder (ours, a real folder, not group- or world-writable) before it reads or
    writes anything in it, and fails closed otherwise; the same answer on every run."""
    def refused(self, cache, inside):
        self.cache = cache
        for _ in range(2):   # a second run: refused again, still nothing read, fetched or written
            with self.assertRaisesRegex(ValueError, 'cache folder'):
                self.boot()
            self.assertEqual(self.urls, [])
            self.assertEqual(os.listdir(inside), [])

    def test_a_folder_others_can_write_is_refused_and_left_as_it_is(self):
        for mode in (0o770, 0o720, 0o702, 0o777, 0o1777):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.cache, mode)
                self.refused(self.cache, self.cache)
                self.assertEqual(os.stat(self.cache).st_mode & 0o7777, mode, 'not made private behind the owner')

    def test_a_folder_another_user_owns_is_refused(self):
        with mock.patch.object(os, 'getuid', lambda: os.stat(self.cache).st_uid + 1):
            self.refused(self.cache, self.cache)

    def test_a_symlinked_folder_is_refused_even_to_a_good_folder(self):
        link = os.path.join(self.root, 'link')
        os.symlink(self.cache, link)   # self.cache: ours and 0700
        self.refused(link, self.cache)
        empty = os.path.join(self.root, 'empty')
        os.mkdir(empty, 0o700)
        gone = os.path.join(self.root, 'gone')
        os.symlink(os.path.join(empty, 'target'), gone)   # a dangling one: its target is not made
        self.refused(gone, empty)

    def test_a_fresh_folder_and_a_good_existing_one_work_on_every_run(self):
        fresh = os.path.join(self.root, 'new', 'cache')   # its parent is missing too
        old = os.umask(0o002)   # #314 item 4: the 0700 below must not come from a strict umask
        self.addCleanup(os.umask, old)
        for cache, mode in ((fresh, None), (os.path.join(self.root, '700'), 0o700),
                            (os.path.join(self.root, '755'), 0o755)):   # 755: ours, only we can write it
            with self.subTest(cache=cache, mode=mode and oct(mode)):
                if mode:
                    os.mkdir(cache)
                    os.chmod(cache, mode)
                self.cache, self.urls = cache, []
                self.pdest = os.path.join(cache, SHA, 'pinned', 'pinned.py')
                self.boot()
                self.boot(error=OSError('the second run is a cache hit'))
                self.assertEqual(len(self.urls), 1)
                with open(self.pdest, 'rb') as f:
                    self.assertEqual(f.read(), self.REAL)
        self.assertEqual(os.stat(fresh).st_mode & 0o777, 0o700)

    def test_a_regular_file_at_the_cache_path_is_refused_with_no_fetch(self):   # #314 item 4: the S_ISDIR part
        f = os.path.join(self.root, 'a-file')
        with open(f, 'wb') as fh:
            fh.write(b'mine')
        os.chmod(f, 0o600)   # ours, only we can write it: only "not a folder" refuses it
        self.cache = f
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'cache folder'):
                self.boot()
            self.assertEqual(self.urls, [])
        with open(f, 'rb') as fh:
            self.assertEqual(fh.read(), b'mine')


@unittest.skipUnless(hasattr(os, 'getuid'), 'POSIX owners and modes')
class OneCacheRule(Bootstrap):
    """#314: two first runs at once both work (item 1), and pinned.py refuses exactly the cache folders the bootstrap
    refuses (item 3), with nothing fetched, written or chmodded."""
    def race(self, make):
        """os.makedirs of the cache folder finds that another run made it after this run's lexists."""
        real, raced = os.makedirs, []

        def makedirs(name, mode=0o777, exist_ok=False):
            if name == self.cache and not raced:
                raced.append(name)
                make(name)   # the other run, between this run's lexists and makedirs
            return real(name, mode, exist_ok)
        return mock.patch.object(os, 'makedirs', makedirs)

    def test_two_first_runs_at_once_both_work(self):
        self.cache = os.path.join(self.root, 'new', 'cache')
        self.pdest = os.path.join(self.cache, SHA, 'pinned', 'pinned.py')
        with self.race(lambda c: os.makedirs(c, 0o700)):   # the other run won: a good folder
            p = self.boot()   # red before: FileExistsError, an OSError an adapter reports as "no network"
        self.assertEqual(p.blob_id(b''), pinned.blob_id(b''))
        self.cache = os.path.join(self.root, 'c2')   # pinned.py's own check: the same window
        with self.race(lambda c: os.makedirs(c, 0o700)), self.serve():
            self.assertEqual(self.path(), os.path.join(self.cache, SHA, 'docs-scope', 'docs_scope.py'))

    def test_a_bad_folder_made_in_that_window_is_still_refused(self):   # the check after makedirs still decides
        self.cache = os.path.join(self.root, 'raced')

        def bad(c):
            os.mkdir(c)
            os.chmod(c, 0o777)
        with self.race(bad), self.assertRaisesRegex(ValueError, 'cache folder'):
            self.boot()
        self.assertEqual(self.urls, [])
        self.assertEqual(os.listdir(self.cache), [])

    def test_pinned_and_the_bootstrap_agree_on_every_cache_folder(self):
        def folder(mode):
            def make(p):
                os.mkdir(p)
                os.chmod(p, mode)
            return make

        def regular_file(p):
            with open(p, 'wb') as f:
                f.write(b'mine')
            os.chmod(p, 0o600)
        good = os.path.join(self.root, 'good')
        os.mkdir(good, 0o700)
        dangling_target = os.path.join(self.root, 'not-made')
        old = os.umask(0o002)   # a fresh folder is 0700 by its own mode, not by a strict umask
        self.addCleanup(os.umask, old)
        states = [('missing, parent too', lambda p: None, True), ('0700', folder(0o700), True),
                  ('0755', folder(0o755), True), ('a regular file', regular_file, False),
                  ('a symlink to a good folder', lambda p: os.symlink(good, p), False),
                  ('a dangling symlink', lambda p: os.symlink(dangling_target, p), False)]
        states += [(oct(m), folder(m), False) for m in (0o775, 0o777, 0o720, 0o702, 0o1777)]
        for i, (what, make, ok) in enumerate(states):
            with self.subTest(what):
                cache = os.path.join(self.root, str(i), 'cache')
                if i:   # state 0: the parent is missing too
                    os.makedirs(os.path.dirname(cache))
                make(cache)
                before = os.lstat(cache) if os.path.lexists(cache) else None
                self.urls = []
                with self.serve():
                    if ok:
                        pinned.path(self.cfg, 'docs-scope', 'docs_scope.py', cache)
                    else:
                        with self.assertRaisesRegex(pinned.FetchError, 'cache folder'):
                            pinned.path(self.cfg, 'docs-scope', 'docs_scope.py', cache)
                self.cache = cache
                if ok:
                    self.boot()
                else:
                    with self.assertRaisesRegex(ValueError, 'cache folder'):
                        self.boot()
                    self.assertEqual(self.urls, [])
                    self.assertEqual(os.lstat(cache).st_mode, before.st_mode, 'left as it is')
                    if stat.S_ISDIR(before.st_mode):
                        self.assertEqual(os.listdir(cache), [])
                if before is None:
                    self.assertEqual(os.stat(cache).st_mode & 0o777, 0o700, 'pinned.py made it 0700')
        self.assertFalse(os.path.lexists(dangling_target), "a dangling symlink's target is not made")
        self.assertEqual(os.listdir(good), [])
        uid = os.stat(good).st_uid   # another user's folder: refused by both (the bootstrap: BootstrapCacheFolder)
        with mock.patch.object(os, 'getuid', lambda: uid + 1):
            with self.serve(), self.assertRaisesRegex(pinned.FetchError, 'cache folder'):
                pinned.path(self.cfg, 'docs-scope', 'docs_scope.py', good)
        self.assertEqual(os.listdir(good), [])


@unittest.skipUnless(hasattr(os, 'getuid'), 'POSIX owners and modes')
class BootstrapThenRun(Bootstrap):
    """#345 M1 (a): the README adapter as written (load_pinned, then P.main run) on a first run under umask 002 with no
    ~/.cache. The bootstrap made ~/.cache with the umask (0775), and run refused it on that and every later run."""

    def test_the_readme_adapter_under_umask_002_runs_and_keeps_running(self):
        self.TRUST_ABOVE = False   # boot() leaves the module's real shared_above: checked below private_above()
        self.root = os.path.realpath(self.root)
        home = os.path.join(self.root, 'home')
        os.mkdir(home, 0o700)
        self.cache = os.path.join(home, '.cache', 'my-project-fork-ci')   # the adapter's CACHE; ~/.cache is missing
        self.pdest = os.path.join(self.cache, SHA, 'pinned', 'pinned.py')
        tool = b'import sys\nsys.exit(4)\n'
        self.config({'repo': 'owner/repo', 'sha': SHA, 'files': {'pinned/pinned.py': pinned.blob_id(self.REAL),
                                                                 'tool/t.py': pinned.blob_id(tool), **self.FILES}})
        old = os.umask(0o002)
        self.addCleanup(os.umask, old)
        for _ in range(2):
            p = self.boot()
            with self.private_above(), contextlib.redirect_stderr(io.StringIO()) as err, \
                    mock.patch.object(p.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(tool)):
                code = p.main(['--config', self.cfg, '--cache', self.cache, 'run', 'tool', 't.py'])
            self.assertEqual(code, 4, err.getvalue())
            with self.private_above(), mock.patch.object(p.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(BODY)):
                self.assertEqual(p.load(self.cfg, 'docs-scope', 'docs_scope', self.cache).X, 1)   # #364 item 1: load too
            self.assertEqual(os.stat(os.path.dirname(self.cache)).st_mode & 0o777, 0o700, '~/.cache made 0700')
            self.assertEqual(os.stat(self.cache).st_mode & 0o777, 0o700)
        self.assertEqual(self.urls, [f'{RAW}/pinned/pinned.py'], 'the second run is a cache hit')

class BootstrapFifo(Bootstrap):
    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFOs')
    def test_bootstrap_fetches_over_a_fifo_not_opens_it(self):
        os.makedirs(os.path.dirname(self.pdest)); os.mkfifo(self.pdest)
        self.boot()   # red at the head: open(dest, 'rb') waits for a writer forever


if __name__ == '__main__':
    unittest.main(verbosity=1)
