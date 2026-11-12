"""gradle_cache.py: a Maven local repository only from the lock, the same bytes on every runner, never an older jar.

    python3 fork-ci/actions/gradle-cache/gradle_cache.test.py

No test reaches GitHub or runs gh: stage and check get a fake client, publish a fake `run`, and the real runner refuses.
The `Issue*` classes were red on the plain port of the source script; each names what it guards.
"""
import hashlib, importlib.util, json, os, re, shutil, subprocess, tempfile, textwrap, threading, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('gradle_cache', os.path.join(HERE, 'gradle_cache.py'))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
    os.environ.pop(name, None)
os.environ['FORK_CI_TEST_MODE'] = '1'   # run_gh refuses

JAR, POM = b'jar bytes', b'<project/>'
V = 'org/example/engine/1.0'
DAY = 86400


def sha(b):
    return hashlib.sha256(b).hexdigest()


class FakeGitHub:
    def __init__(self, files, tag='jars'):   # {asset name: bytes} in release `tag`
        self.releases, self.downloads, self.listed = {tag: dict(files)}, [], []

    @property
    def files(self):
        return next(iter(self.releases.values()))

    def assets(self, tag):
        self.listed.append(tag)
        if tag not in self.releases:
            raise M.Refused(f'no release {tag}')
        return {n: {'name': n, 'url': f'{tag}/{n}', 'digest': 'sha256:' + sha(b), 'state': 'uploaded'}
                for n, b in self.releases[tag].items()}

    def download(self, asset):
        self.downloads.append(asset['name'])
        tag, name = asset['url'].split('/', 1)
        return self.releases[tag][name]


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='gradle-cache-test-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.out, self.cache, self.m2 = (os.path.join(self.root, n) for n in ('out', 'cache', 'm2'))
        self.lock_file = os.path.join(self.root, 'm2-lock.json')
        self.lines = []

    def lock(self, rows, release='jars'):
        with open(self.lock_file, 'w') as f:
            json.dump({'release': release, 'files': rows}, f)

    def read_lock(self):
        with open(self.lock_file) as f:
            return json.load(f)

    def stage(self, gh, out=None):
        M.stage(self.lock_file, out or self.out, gh, cache=self.cache, say=self.lines.append)

    def rows(self):
        return [{'path': f'{V}/engine-1.0.jar', 'sha256': sha(JAR)}, {'path': f'{V}/engine-1.0.pom', 'sha256': sha(POM)}]

    def remote(self):
        return FakeGitHub({f'{sha(JAR)}-engine-1.0.jar': JAR, f'{sha(POM)}-engine-1.0.pom': POM})

    def staged(self, out=None):
        out = out or self.out
        return sorted(os.path.relpath(os.path.join(d, f), out) for d, _, fs in os.walk(out) for f in fs)


class Stage(Base):
    def test_a_fresh_runner_gets_exactly_the_locked_files(self):
        self.lock(self.rows())
        self.stage(self.remote())
        self.assertEqual(self.staged(), [f'{V}/engine-1.0.jar', f'{V}/engine-1.0.pom'])
        with open(os.path.join(self.out, V, 'engine-1.0.jar'), 'rb') as f:
            self.assertEqual(f.read(), JAR)

    def test_a_second_run_takes_the_cache_and_still_asks_github(self):
        self.lock(self.rows())
        self.stage(self.remote())
        shutil.rmtree(self.out)
        gh = self.remote()
        self.stage(gh)
        self.assertEqual(gh.downloads, [])
        gone = self.remote(); del gone.files[f'{sha(POM)}-engine-1.0.pom']
        shutil.rmtree(self.out)   # a warm cache never hides an asset that is gone: the fresh runner would be red
        with self.assertRaisesRegex(M.Refused, 'engine-1.0.pom: the release jars has no asset'):
            self.stage(gone)

    def test_a_missing_asset_is_red_by_name_and_nothing_is_staged(self):
        self.lock(self.rows())
        gh = self.remote(); del gh.files[f'{sha(JAR)}-engine-1.0.jar']
        with self.assertRaisesRegex(M.Refused, r'engine-1\.0\.jar: the release jars has no asset'):
            self.stage(gh)
        self.assertFalse(os.path.exists(self.out))

    def test_other_bytes_under_the_asset_name_are_red_and_never_cached(self):
        self.lock(self.rows())
        gh = self.remote()
        gh.files[f'{sha(JAR)}-engine-1.0.jar'] = b'an older jar'
        real = gh.assets
        gh.assets = lambda tag: {n: {k: v for k, v in a.items() if k != 'digest'} for n, a in real(tag).items()}
        with self.assertRaisesRegex(M.Refused, 'the download has sha256'):
            self.stage(gh)
        self.assertFalse(os.path.exists(os.path.join(self.cache, sha(JAR))))
        self.assertFalse(os.path.exists(self.out))

    def test_a_listed_digest_that_differs_is_red_before_any_download(self):
        self.lock(self.rows())
        gh = self.remote()
        real = gh.assets
        gh.assets = lambda tag: {n: dict(a, digest='sha256:' + '0' * 64) for n, a in real(tag).items()}
        with self.assertRaisesRegex(M.Refused, 'the asset has digest'):
            self.stage(gh)
        self.assertEqual(gh.downloads, [])

    def test_a_damaged_cache_file_is_named_and_downloaded_again(self):
        self.lock(self.rows())
        os.makedirs(self.cache)
        with open(os.path.join(self.cache, sha(JAR)), 'wb') as f:
            f.write(b'cut off')
        gh = self.remote()
        self.stage(gh)
        self.assertEqual(sorted(gh.downloads), sorted([f'{sha(JAR)}-engine-1.0.jar', f'{sha(POM)}-engine-1.0.pom']))
        self.assertTrue(any(line.startswith('WARN:') and sha(JAR) in line for line in self.lines))
        with open(os.path.join(self.cache, sha(JAR)), 'rb') as f:
            self.assertEqual(f.read(), JAR)

    def test_a_folder_with_files_is_refused(self):
        self.lock(self.rows())
        os.makedirs(os.path.join(self.out, V))
        open(os.path.join(self.out, V, 'engine-0.9.jar'), 'w').close()
        with self.assertRaisesRegex(M.Refused, 'is not empty'):
            self.stage(self.remote())

    def test_bad_locks_are_refused(self):
        for rows, why in (([{'path': '../x/y.jar', 'sha256': sha(JAR)}], 'bad path'),
                          ([{'path': f'{V}/a.jar', 'sha256': 'abc'}], 'bad sha256'),
                          (self.rows() + self.rows()[:1], 'listed twice'), ([], 'lists no files')):
            self.lock(rows)
            with self.subTest(why), self.assertRaisesRegex(M.Refused, why):
                self.stage(self.remote())
        for text, why in (('{"files": [', 'cannot be read'), ('{"files": []}', 'no "release"'), ('', 'cannot be read')):
            with open(self.lock_file, 'w') as f:
                f.write(text)
            with self.subTest(why), self.assertRaisesRegex(M.Refused, why):
                self.stage(self.remote())
        self.assertFalse(os.path.exists(self.out))

    def test_two_runners_at_once_share_one_cache(self):
        self.lock(self.rows())
        errors = []

        def one(out):
            try:
                M.stage(self.lock_file, out, self.remote(), cache=self.cache, say=lambda _: None)
            except BaseException as e:   # noqa: BLE001 (reported below)
                errors.append(e)
        threads = [threading.Thread(target=one, args=(os.path.join(self.root, f'out{i}'),)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        for i in range(4):
            self.assertEqual(self.staged(os.path.join(self.root, f'out{i}')), [f'{V}/engine-1.0.jar', f'{V}/engine-1.0.pom'])
        self.assertEqual(sorted(os.listdir(self.cache)), sorted([sha(JAR), sha(POM)]))


class PublishBase(Base):
    def setUp(self):
        super().setUp()
        folder = os.path.join(self.m2, *V.split('/'))
        os.makedirs(folder)
        for name, data in (('engine-1.0.jar', JAR), ('engine-1.0.pom', POM), ('engine-1.0-sources.jar', b's'),
                           ('engine-1.0-javadoc.jar', b'j'), ('_remote.repositories', b'r')):
            with open(os.path.join(folder, name), 'wb') as f:
                f.write(data)
        self.uploaded, self.release, self.calls = [], {}, []
        self.states = {}   # asset name -> state other than 'uploaded'
        self.exists = True

    def gh(self, cmd):
        """A stand-in for gh: the release `jars` of example/repo, in self.release."""
        self.calls.append(cmd)
        if cmd[:3] == ['gh', 'release', 'view']:   # the port's listing call
            if not self.exists:
                raise subprocess.CalledProcessError(1, cmd, stderr='release not found')
            return json.dumps({'assets': [{'name': n} for n in self.release]})
        if cmd[:2] == ['gh', 'api'] and cmd[2].endswith('/releases/tags/jars'):
            if not self.exists:
                raise subprocess.CalledProcessError(1, cmd, stderr='gh: Not Found (HTTP 404)')
            return json.dumps({'id': 7})
        if cmd[:3] == ['gh', 'api', '--paginate'] and '/releases/7/assets' in cmd[3]:
            return ''.join(json.dumps({'name': n, 'size': len(b), 'digest': 'sha256:' + sha(b),
                                       'state': self.states.get(n, 'uploaded')}) + '\n' for n, b in self.release.items())
        if cmd[:3] == ['gh', 'release', 'create']:
            self.exists = True
            return ''
        if cmd[:3] == ['gh', 'release', 'upload']:
            name = os.path.basename(cmd[-1])
            if name in self.release and '--clobber' not in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr='asset under the same name already exists')
            with open(cmd[-1], 'rb') as f:
                self.release[name] = f.read()
            self.states.pop(name, None)
            self.uploaded.append(name)
            return ''
        raise AssertionError(cmd)

    def publish(self, run=None, coords=('org.example:engine:1.0',), **kw):
        M.publish(self.lock_file, list(coords), 'example/repo', m2=self.m2, run=run or self.gh, say=self.lines.append, **kw)

    def paths(self):
        return [(r['path'], r['sha256']) for r in self.read_lock()['files']]


class Publish(PublishBase):

    def test_publish_uploads_the_build_files_and_a_second_run_changes_nothing(self):
        self.lock([])
        self.exists = False
        self.publish()
        self.assertEqual(sorted(self.uploaded), sorted([f'{sha(JAR)}-engine-1.0.jar', f'{sha(POM)}-engine-1.0.pom']))
        self.assertEqual(self.paths(), [(r['path'], r['sha256']) for r in self.rows()])
        self.uploaded.clear()
        self.publish()
        self.assertEqual(self.uploaded, [])
        self.assertEqual(self.paths(), [(r['path'], r['sha256']) for r in self.rows()])
        self.stage(FakeGitHub(self.release))   # what publish wrote is what stage takes

    def test_a_crash_after_the_upload_is_finished_by_the_next_run(self):
        self.lock([])

        def dies_after_first_upload(cmd):
            out = self.gh(cmd)
            if cmd[:3] == ['gh', 'release', 'upload']:
                raise KeyboardInterrupt
            return out
        with self.assertRaises(KeyboardInterrupt):
            self.publish(dies_after_first_upload)
        self.assertEqual(self.read_lock()['files'], [])   # nothing pins a half-published version
        self.publish()
        self.assertEqual(self.paths(), [(r['path'], r['sha256']) for r in self.rows()])
        self.assertEqual(len(self.release), 2)

    def test_other_bytes_under_a_locked_path_are_refused_and_the_lock_stays(self):
        self.lock([{'path': f'{V}/engine-1.0.jar', 'sha256': sha(b'the first build')}])
        with self.assertRaisesRegex(M.Refused, 'never reused'):
            self.publish()
        self.assertEqual(self.uploaded, [])
        self.assertEqual(len(self.read_lock()['files']), 1)

    def test_a_version_not_in_m2_is_refused(self):
        self.lock([])
        with self.assertRaisesRegex(M.Refused, 'has no jar and pom'):
            self.publish(coords=['org.example:engine:2.0'])

    def test_a_path_the_lock_could_not_read_is_refused_before_any_upload(self):
        # source #1257 r1: Maven's 1.0+b1 was uploaded and written, then the lock refused itself (every stage red)
        self.lock([])
        folder = os.path.join(self.m2, 'org', 'example', 'engine', '1.0+b1')
        os.makedirs(folder)
        for name in ('engine-1.0+b1.jar', 'engine-1.0+b1.pom'):
            with open(os.path.join(folder, name), 'wb') as f:
                f.write(name.encode())
        with self.assertRaisesRegex(M.Refused, r"bad path 'org/example/engine/1\.0\+b1/engine-1\.0\+b1\.jar'"):
            self.publish(coords=['org.example:engine:1.0+b1'])
        self.assertEqual(self.uploaded, [])
        self.assertEqual(self.read_lock()['files'], [])
        M.read_lock(self.lock_file)   # the lock is still readable: every other stage stays green

    def test_the_real_runner_never_runs_in_a_test(self):
        self.assertRaisesRegex(M.Refused, 'test', M.run_gh, ['gh', 'release', 'list'])


class Issue1324KilledDownloads(Base):
    """Source issue #1324: a killed download leaves .part-* files nothing removes; a cache file another runner removes
    between the check and the copy turns the job red."""
    def test_a_stale_part_file_is_removed_and_a_fresh_one_kept(self):
        self.lock(self.rows())
        os.makedirs(self.cache)
        old, new = os.path.join(self.cache, '.part-old'), os.path.join(self.cache, '.part-new')
        for p in (old, new):
            open(p, 'w').close()
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        self.stage(self.remote())
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))   # another runner may be writing it right now

    def test_a_cache_file_removed_right_after_its_check_is_still_staged(self):
        self.lock(self.rows())
        self.stage(self.remote())
        shutil.rmtree(self.out)
        real = M.sha256

        def check_then_another_runner_removes_it(data):
            for name in os.listdir(self.cache):
                os.remove(os.path.join(self.cache, name))
            return real(data)
        M.sha256 = check_then_another_runner_removes_it
        try:
            self.stage(self.remote())
        finally:
            M.sha256 = real
        self.assertEqual(self.staged(), [f'{V}/engine-1.0.jar', f'{V}/engine-1.0.pom'])

    def test_a_stage_that_fails_half_way_leaves_no_out_and_the_rerun_is_green(self):
        self.lock(self.rows())
        gh = self.remote()
        real = gh.download
        gh.download = lambda a: real(a) if a['name'].endswith('.jar') else (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.stage(gh)
        self.assertFalse(os.path.exists(self.out))
        self.stage(self.remote())
        self.assertEqual(self.staged(), [f'{V}/engine-1.0.jar', f'{V}/engine-1.0.pom'])


class Issue1330CachePrune(Base):
    """Source issue #1330: the runner cache is never pruned."""
    def test_unused_old_entries_go_used_ones_stay_and_unknown_files_are_named(self):
        self.lock(self.rows())
        os.makedirs(self.cache)
        long_ago, recent = time.time() - 40 * DAY, time.time() - DAY
        files = {sha(b'old engine'): long_ago, sha(b'branch engine'): recent, sha(JAR): long_ago, 'notes.txt': long_ago}
        for name, t in files.items():
            with open(os.path.join(self.cache, name), 'wb') as f:
                f.write(JAR if name == sha(JAR) else b'x')
            os.utime(os.path.join(self.cache, name), (t, t))
        self.stage(self.remote())
        left = set(os.listdir(self.cache))
        self.assertNotIn(sha(b'old engine'), left)          # no lock row, unused for 40 days
        self.assertIn(sha(b'branch engine'), left)          # another branch's lock may still use it
        self.assertIn('notes.txt', left)                     # never removes a file it did not write
        self.assertGreater(os.stat(os.path.join(self.cache, sha(JAR))).st_mtime, recent)   # used: its age restarts
        self.assertTrue(any('notes.txt' in line for line in self.lines))


class Issue1326ReleaseGrowth(PublishBase):
    """Source issue #1326: the lock and the release only grow, and a release holds at most 1000 assets."""
    def test_a_publish_past_the_asset_limit_is_refused_before_any_upload(self):
        self.lock([])
        self.release = {f'{i:064x}-old.jar': b'x' for i in range(999)}
        with self.assertRaisesRegex(M.Refused, r'release jars has 999 assets.*1000.*new tag'):
            self.publish()
        self.assertEqual(self.uploaded, [])
        self.assertEqual(self.read_lock()['files'], [])

    def test_rows_keep_their_release_when_the_lock_moves_to_a_new_one(self):
        self.lock([])
        self.publish()
        self.assertEqual({r['release'] for r in self.read_lock()['files']}, {'jars'})
        lock = self.read_lock()
        lock['release'] = 'jars-2'   # the rollover: new uploads go to a new release, old rows still stage from theirs
        with open(self.lock_file, 'w') as f:
            json.dump(lock, f)
        gh = FakeGitHub(self.release)
        gh.releases['jars-2'] = {}
        self.stage(gh)
        self.assertEqual(gh.listed, ['jars'])
        self.assertEqual(self.staged(), [f'{V}/engine-1.0.jar', f'{V}/engine-1.0.pom'])

    def test_prune_lock_keeps_only_the_named_versions(self):
        other = 'org/example/engine/0.9'
        self.lock(self.rows() + [{'path': f'{other}/engine-0.9.jar', 'sha256': sha(b'0.9')}])
        M.prune_lock(self.lock_file, ['org.example:engine:1.0'], say=self.lines.append)
        self.assertEqual(self.paths(), [(r['path'], r['sha256']) for r in self.rows()])
        M.prune_lock(self.lock_file, ['org.example:engine:1.0'], say=self.lines.append)   # a rerun changes nothing
        self.assertEqual(len(self.read_lock()['files']), 2)
        with self.assertRaisesRegex(M.Refused, 'org.example:engine:3.0.*no rows'):   # a typo never empties the lock
            M.prune_lock(self.lock_file, ['org.example:engine:1.0', 'org.example:engine:3.0'], say=self.lines.append)
        self.assertEqual(len(self.read_lock()['files']), 2)


class Issue1329ReleaseMistakes(PublishBase):
    """Source issue #1329: nothing notices a deleted asset before a build needs it, no way to put it back, no
    troubleshooting rows."""
    def test_check_names_every_missing_or_unfinished_asset_without_downloading(self):
        self.lock(self.rows())
        gh = self.remote()
        del gh.files[f'{sha(POM)}-engine-1.0.pom']
        real = gh.assets
        gh.assets = lambda tag: {n: dict(a, state='starter') for n, a in real(tag).items()}
        with self.assertRaises(M.Refused) as e:
            M.check(self.lock_file, gh, say=self.lines.append)
        self.assertIn('engine-1.0.pom: the release jars has no asset', str(e.exception))
        self.assertIn('engine-1.0.jar: the upload of', str(e.exception))
        self.assertEqual(gh.downloads, [])
        M.check(self.lock_file, self.remote(), say=self.lines.append)

    def test_publish_puts_back_a_deleted_asset_of_a_locked_version(self):
        self.lock([])
        self.publish()
        del self.release[f'{sha(JAR)}-engine-1.0.jar']
        self.uploaded.clear()
        self.publish()
        self.assertEqual(self.uploaded, [f'{sha(JAR)}-engine-1.0.jar'])
        self.stage(FakeGitHub(self.release))

    def test_an_unfinished_upload_is_replaced(self):
        self.lock([])
        self.release = {f'{sha(JAR)}-engine-1.0.jar': b'cut'}
        self.states = {f'{sha(JAR)}-engine-1.0.jar': 'starter'}
        self.publish()
        self.assertEqual(self.release[f'{sha(JAR)}-engine-1.0.jar'], JAR)

    def test_every_error_stage_and_check_print_has_a_troubleshooting_row(self):
        with open(os.path.join(HERE, 'README.md')) as f:
            readme = f.read()
        rows = '\n'.join(line for line in readme.split('## Troubleshooting', 1)[1].splitlines() if line.startswith('|'))
        for message in ('has no asset', 'the upload of', 'the asset has digest', 'the download has sha256',
                        'GitHub answered 404', 'is not empty', 'cannot be read', 'asset limit', 'never reused'):
            self.assertIn(message, rows)


class Issue1336ActionRunsItsOwnScript(unittest.TestCase):
    """Source issue #1336: a caller ran the branch's copy of the script. The action runs the copy at its own pinned
    path, never one from the caller's workspace, and removes OUT only inside RUNNER_TEMP."""
    def test_the_action_runs_the_script_next_to_it(self):
        with open(os.path.join(HERE, 'action.yml')) as f:
            action = f.read()
        calls = re.findall(r'python3 -I (\S+gradle_cache\.py"?)', action)
        self.assertEqual(calls, ['"$ACTION_PATH/gradle_cache.py"'] * len(calls))
        self.assertTrue(calls)
        self.assertIn('ACTION_PATH: ${{ github.action_path }}', action)
        self.assertNotIn('${{ inputs.', action.split('run: |', 1)[1])   # inputs reach the shell only as env

    def test_the_action_removes_out_only_inside_runner_temp(self):
        with open(os.path.join(HERE, 'action.yml')) as f:
            script = f.read().split('run: |', 1)[1]
        script = textwrap.dedent(script)
        tmp = tempfile.mkdtemp(prefix='gradle-cache-action-')
        self.addCleanup(shutil.rmtree, tmp, True)
        bin_dir = os.path.join(tmp, 'bin')
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, 'python3'), 'w') as f:   # stands in for the script: records, then stages
            f.write('#!/bin/sh\necho "$@" > "$RUNNER_TEMP/args"; for a; do last=$a; done\n')
        os.chmod(os.path.join(bin_dir, 'python3'), 0o755)
        for out, ok in (('', True), ('m2', True), ('../outside', False), ('/', False), ('.', False)):
            runner_temp = os.path.join(tmp, 'rt')
            os.makedirs(runner_temp, exist_ok=True)
            env = dict(os.environ, PATH=bin_dir + ':' + os.environ['PATH'], RUNNER_TEMP=runner_temp,
                       GITHUB_OUTPUT=os.path.join(tmp, 'output'), ACTION_PATH=HERE, LOCK='l.json', OUT=out,
                       CACHE_DIR='', MAX_AGE='30', REPO='example/repo', GH_TOKEN='')
            if out and not out.startswith('/'):
                env['OUT'] = os.path.join(runner_temp, out)
            r = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, cwd=tmp)
            with self.subTest(out=out):
                self.assertEqual(r.returncode == 0, ok, r.stdout + r.stderr)
                self.assertTrue(os.path.isdir(tmp))


if __name__ == '__main__':
    unittest.main()
