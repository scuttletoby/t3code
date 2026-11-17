"""pinned.py: one fetch per sha and file, whole or not at all, of the bytes the config names (git blob id); no network
and no good copy is an error; a killed fetch's .part- file and an old pin's folder go; load and run run the bytes that
were checked. The README's bootstrap is run as written. No test reaches the network: every fetch is served here.
Run: python3 fork-ci/actions/pinned/pinned.test.py
"""
import contextlib, importlib.util, io, json, os, re, shutil, stat, subprocess, sys, tempfile, time, unittest
from unittest import mock

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

    def setUp(self):
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
                     b'print(__spec__.origin)\n', b'print(__loader__.path)\n'):
            with self.subTest(body=body):
                self.assertNotEqual(pinned.neighbour_loads(body), [])

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
                                'import unittest.mock as m, io, urllib.request as u; '
                                f'm.patch.object(u, "urlopen", lambda *a, **k: io.BytesIO({body!r})).start(); '
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
            return self.ns['load_pinned'](self.cfg, self.cache)

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


class BootstrapFifo(Bootstrap):
    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFOs')
    def test_bootstrap_fetches_over_a_fifo_not_opens_it(self):
        os.makedirs(os.path.dirname(self.pdest)); os.mkfifo(self.pdest)
        self.boot()   # red at the head: open(dest, 'rb') waits for a writer forever


if __name__ == '__main__':
    unittest.main(verbosity=1)
