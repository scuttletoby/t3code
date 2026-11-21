#!/usr/bin/env python3
"""Tests of the hand-off pair (handoff-put, handoff-get): each action.yml played step by step as a runner plays a
composite action (its `if:` and `${{ }}` evaluated, each run step in bash -eo pipefail with GITHUB_OUTPUT, a
continue-on-error step's failure kept as its outcome), with the pinned upload and download actions replaced by a fake
artifact store of one run that behaves as GitHub's does (an existing name is refused unless overwrite is true; a
missing name fails the download). Run: python3 <this>.

The state questions each have a test: a re-run of the producer (Rerun), of the consumer alone (Rerun), a producer or
consumer stopped half-way (CutOff), an older commit's file and a garbled one (Refused), no hand-off at all and a stale
file at the path (Missing)."""
import contextlib, importlib.util, io, json, os, re, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ACTIONS = os.path.dirname(HERE)
PUT, GET = HERE, os.path.join(ACTIONS, 'handoff-get')
UPLOAD = 'actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9'
DOWNLOAD = 'actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333'
BASH = shutil.which('bash')
GIT = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
       'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


H = load('handoff', os.path.join(HERE, 'handoff.py'))
# The expression evaluator fleet-route's tests check the README's runs-on with (literals, contexts, ==, !=, !, &&, ||,
# fromJSON): one evaluator for both, so these steps' `if:` and `${{ }}` are read the way that test reads GitHub's.
evaluate = load('fleet_route_test', os.path.join(ACTIONS, 'fleet-route', 'fleet_route.test.py')).evaluate


def quiet(f, *args):
    """f(*args) in this process, its ::notice:: and ::warning:: lines kept out of the run's annotations."""
    with contextlib.redirect_stdout(io.StringIO()):
        return f(*args)


def text(v):
    return {None: '', True: 'true', False: 'false'}.get(v, v) if not isinstance(v, (int, float)) or isinstance(
        v, bool) else str(v)


def expand(value, context):
    return re.sub(r'\$\{\{(.*?)\}\}', lambda m: text(evaluate(m[1], context)), str(value))


class Store:
    """One run's artifacts, as upload-artifact and download-artifact see them (every attempt's)."""

    def __init__(self):
        self.artifacts, self.calls, self.upload_fails = {}, [], False

    def upload(self, w):
        self.calls.append(('upload', dict(w)))
        assert set(w) <= {'name', 'path', 'retention-days', 'overwrite', 'if-no-files-found'}, w
        if self.upload_fails or not os.path.isfile(w['path']):
            return False
        if w['name'] in self.artifacts and w.get('overwrite') != 'true':
            return False      # GitHub: an artifact with this name already exists (409)
        with open(w['path'], 'rb') as f:
            self.artifacts[w['name']] = {os.path.basename(w['path']): f.read()}
        return True

    def download(self, w):
        self.calls.append(('download', dict(w)))
        assert set(w) == {'name', 'path'}, w     # no run-id, repository or token: this run only
        if w['name'] not in self.artifacts:
            return False
        os.makedirs(w['path'], exist_ok=True)
        for name, data in self.artifacts[w['name']].items():
            with open(os.path.join(w['path'], name), 'wb') as f:
                f.write(data)
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='handoff-test-')
        self.addCleanup(shutil.rmtree, self.d, True)
        self.store = Store()
        self.producer = self.repo('producer')
        self.consumer = self.repo('consumer')
        shutil.rmtree(self.consumer)
        shutil.copytree(self.producer, self.consumer, symlinks=True)   # the same commit, checked out by each job

    def git(self, repo, *args):
        return subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True, text=True,
                              env={**os.environ, **GIT}).stdout.strip()

    def repo(self, name):
        path = os.path.join(self.d, name)
        os.makedirs(path)
        self.git(path, 'init', '-q', '-b', 'main')
        self.commit(path, 'one')
        return path

    def commit(self, repo, what):
        with open(os.path.join(repo, 'src.txt'), 'w') as f:
            f.write(what)
        self.git(repo, 'add', 'src.txt')
        self.git(repo, 'commit', '-q', '-m', what)
        return self.git(repo, 'rev-parse', 'HEAD')

    def write(self, repo, rel, data):
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)

    def play(self, folder, ws, inputs, stop_after=None, fail_on=None):
        """The action's outputs, or None when the job stopped after step `stop_after`. A step that fails without
        continue-on-error fails the action: AssertionError (none of these steps may)."""
        with open(os.path.join(folder, 'action.yml')) as f:
            action = yaml.safe_load(f)
        temp = os.path.join(self.d, 'runner-temp-' + os.path.basename(ws))
        os.makedirs(temp, exist_ok=True)
        defaults = {k: str(v.get('default', '')) for k, v in action['inputs'].items()}
        context = {'inputs': {**defaults, **inputs}, 'steps': {}}
        self.log = ''
        for step in action['runs']['steps']:
            sid = step['id']
            if 'if' in step and not evaluate(step['if'], context):
                context['steps'][sid] = {'outputs': {}, 'outcome': 'skipped', 'conclusion': 'skipped'}
                continue
            outputs = {}
            if 'uses' in step:
                w = {k: expand(text(v), context) for k, v in (step.get('with') or {}).items()}   # YAML true: 'true'
                uses = step['uses']
                ok = self.store.upload(w) if uses == UPLOAD else self.store.download(w) if uses == DOWNLOAD else None
                self.assertIsNotNone(ok, f'unknown action {uses}')
            else:
                out = os.path.join(temp, f'output-{sid}')
                open(out, 'w').close()
                env = {'PATH': os.environ['PATH'], 'HOME': self.d, 'GITHUB_OUTPUT': out, 'GITHUB_ACTION_PATH': folder,
                       'RUNNER_TEMP': temp, 'GITHUB_WORKSPACE': ws,
                       **{k: expand(v, context) for k, v in (step.get('env') or {}).items()}}
                if fail_on == sid:
                    env['PATH'] = self.d      # no python3: the step fails
                r = subprocess.run([BASH, '--noprofile', '--norc', '-eo', 'pipefail', '-c', step['run']], cwd=ws,
                                   env=env, capture_output=True, text=True)
                self.log += r.stdout + r.stderr
                ok = r.returncode == 0
                with open(out, encoding='utf-8') as f:
                    for line in f.read().splitlines():
                        k, _, v = line.partition('=')
                        outputs[k] = v
            if not ok:
                self.assertTrue(step.get('continue-on-error'), f'step {sid} failed the action:\n{self.log}')
            context['steps'][sid] = {'outputs': outputs, 'outcome': 'success' if ok else 'failure'}
            if stop_after == sid:
                return None
        return {k: expand(v['value'], context) for k, v in action['outputs'].items()}

    def put(self, data=b'report\n', name='report', path='out/report.json', ws=None, **kw):
        ws = ws or self.producer
        if data is not None:
            self.write(ws, path, data)
        return self.play(PUT, ws, {'name': name, 'path': path}, **kw)

    def get(self, name='report', path='in/report.json', ws=None, **kw):
        return self.play(GET, ws or self.consumer, {'name': name, 'path': path}, **kw)

    def read(self, rel, ws=None):
        with open(os.path.join(ws or self.consumer, rel), 'rb') as f:
            return f.read()

    def absent(self, rel='in/report.json', ws=None):
        self.assertFalse(os.path.lexists(os.path.join(ws or self.consumer, rel)), rel)

    def head(self, repo=None):
        return self.git(repo or self.producer, 'rev-parse', 'HEAD')


class RoundTrip(Base):
    def test_the_payload_arrives_byte_for_byte(self):
        data = 'ünïcode, no newline at the end   "quoted"'.encode()
        put = self.put(data)
        sha = self.head()
        self.assertEqual((put['ok'], put['artifact'], put['sha']), ('true', f'report-{sha}', sha), self.log)
        self.assertEqual(self.store.calls[0][1]['retention-days'], '1')      # short retention by default
        got = self.get()
        self.assertEqual((got['ok'], got['sha']), ('true', sha), self.log)
        self.assertEqual(self.read('in/report.json'), data)
        doc = json.loads(self.store.artifacts[f'report-{sha}']['handoff.json'])
        self.assertEqual(doc, {'format': H.FORMAT, 'name': 'report', 'sha': sha, 'payload': data.decode()})

    def test_retention_is_passed_on(self):
        self.assertEqual(self.play(PUT, self.producer, {'name': 'r', 'path': 'src.txt', 'retention-days': '7'})['ok'],
                         'true')
        self.assertEqual(self.store.calls[0][1]['retention-days'], '7')


class Missing(Base):
    def test_no_artifact_is_ok_false_never_a_failed_step(self):
        got = self.get()          # play() fails the test if any step fails the action
        self.assertEqual(got['ok'], 'false')
        self.assertIn('no hand-off report-', got['reason'])
        self.absent()

    def test_a_stale_file_at_the_path_is_removed(self):
        self.write(self.consumer, 'in/report.json', b'from an earlier job')
        self.assertEqual(self.get()['ok'], 'false')
        self.absent()

    def test_a_stale_symlink_at_the_path_is_removed_not_followed(self):
        self.write(self.consumer, 'keep.txt', b'keep')
        os.makedirs(os.path.join(self.consumer, 'in'))
        os.symlink('../keep.txt', os.path.join(self.consumer, 'in/report.json'))
        self.assertEqual(self.get()['ok'], 'false')
        self.absent()
        self.assertEqual(self.read('keep.txt'), b'keep')

    def test_a_newer_head_finds_no_older_hand_off(self):
        self.assertEqual(self.put()['ok'], 'true')
        self.commit(self.consumer, 'two')
        got = self.get()
        self.assertEqual(got['ok'], 'false')
        downloads = [c[1]['name'] for c in self.store.calls if c[0] == 'download']
        self.assertEqual(downloads, [f'report-{self.head(self.consumer)}'])
        self.absent()

    def test_a_download_that_failed_is_not_read_even_with_a_whole_file(self):
        self.assertEqual(self.put()['ok'], 'true')
        real = self.store.download

        def half(w):
            real(w)
            return False      # the file is there, the step failed (a digest mismatch, a cut connection)
        self.store.download = half
        self.assertEqual(self.get()['ok'], 'false')
        self.absent()

    def test_no_checkout_is_ok_false(self):
        os.makedirs(os.path.join(self.d, 'empty'))
        got = self.get(ws=os.path.join(self.d, 'empty'))
        self.assertEqual(got['ok'], 'false')
        self.assertEqual(self.store.calls, [])

    def test_where_not_run_is_ok_false(self):
        """get with no ready (its step before stopped): ok false, the earlier step's reason passed on."""
        out = os.path.join(self.d, 'out')
        with mock.patch.dict(os.environ, {'GITHUB_OUTPUT': out}):
            quiet(H.main, ['get', '--name', 'r', '--path', 'x', '--why', 'stopped'])
        with open(out) as f:
            self.assertEqual(f.read(), 'ok=false\nsha=\nreason=not looked for: stopped\n')


class Refused(Base):
    """A file in the artifact named for this HEAD that is not this HEAD's hand-off: refused, path left absent."""

    def planted(self, data):
        self.write(self.consumer, 'in/report.json', b'stale')
        self.store.artifacts[f'report-{self.head(self.consumer)}'] = {'handoff.json': data}
        got = self.get()
        self.assertEqual(got['ok'], 'false', self.log)
        self.absent()
        return got

    def test_the_sha_inside_must_be_this_head(self):
        old = self.head()
        self.commit(self.consumer, 'two')
        got = self.planted(H.envelope('report', old, 'from the older commit'))
        self.assertIn(f'is for commit {old}, not this checkout HEAD {self.head(self.consumer)}: refused', got['reason'])
        self.assertIn('::warning::', self.log)

    def test_another_name_inside_is_refused(self):
        self.assertIn('is for other, not report', self.planted(H.envelope('other', self.head(), 'x'))['reason'])

    def test_garbled_files(self):
        sha = self.head()
        whole = H.envelope('report', sha, 'payload')
        for what, data in [('bytes', b'\x00\xff\xfe garbage'), ('cut short', whole[:len(whole) // 2]),
                           ('empty', b''), ('a list', b'[]'), ('not utf-8', whole.replace(b'payload', b'\xff')),
                           ('another format', whole.replace(H.FORMAT.encode(), b'fork-ci-handoff/2')),
                           ('an extra key', whole.replace(b'{', b'{"x": 1, ', 1)),
                           ('no payload', json.dumps({'format': H.FORMAT, 'name': 'report', 'sha': sha}).encode()),
                           ('payload not text', json.dumps({'format': H.FORMAT, 'name': 'report', 'sha': sha,
                                                            'payload': {'a': 1}}).encode())]:
            with self.subTest(what):
                self.assertIn('refused', self.planted(data)['reason'])

    def test_a_symlink_in_the_artifact_is_not_followed(self):
        """handoff.json itself is opened with O_NOFOLLOW (a download folder that holds a link is refused)."""
        self.write(self.consumer, 'secret', H.envelope('report', self.head(), 'x'))
        def link(w):
            os.makedirs(w['path'], exist_ok=True)
            os.symlink(os.path.join(self.consumer, 'secret'), os.path.join(w['path'], 'handoff.json'))
            return True
        self.store.download = link
        self.assertEqual(self.get()['ok'], 'false')
        self.absent()


class Rerun(Base):
    def test_a_rerun_of_the_producer_overwrites_its_artifact(self):
        self.assertEqual(self.put(b'first')['ok'], 'true')
        self.assertEqual(self.put(b'second')['ok'], 'true', self.log)    # GitHub refuses the name without overwrite
        self.assertEqual(self.get()['ok'], 'true')
        self.assertEqual(self.read('in/report.json'), b'second')

    def test_a_rerun_of_the_consumer_alone_reads_it_again(self):
        self.assertEqual(self.put(b'first')['ok'], 'true')
        self.assertEqual(self.get()['ok'], 'true')
        self.assertEqual(self.get()['ok'], 'true')     # same run, earlier attempt's artifact, path written again
        self.assertEqual(self.read('in/report.json'), b'first')

    def test_each_get_reads_only_its_own_download(self):
        """A second get on the same runner whose artifact holds no handoff.json does not read the first get's."""
        self.assertEqual(self.put(b'first')['ok'], 'true')
        self.assertEqual(self.get()['ok'], 'true')
        self.store.artifacts[f'report-{self.head()}'] = {'other.txt': b'x'}
        self.assertEqual(self.get()['ok'], 'false')
        self.absent()


class CutOff(Base):
    def test_a_producer_stopped_before_its_upload_leaves_no_hand_off(self):
        self.assertIsNone(self.put(stop_after='stage'))
        self.assertEqual(self.store.artifacts, {})
        self.assertEqual(self.get()['ok'], 'false')
        self.absent()

    def test_a_failed_upload_is_ok_false(self):
        self.store.upload_fails = True
        put = self.put()
        self.assertEqual((put['ok'], put['reason']), ('false', 'upload failure'))

    # A kill runs no cleanup: os.unlink is a no-op while the write is stopped half-way (after its bytes, at fsync).
    killed = staticmethod(lambda: (mock.patch.object(H.os, 'fsync', side_effect=KeyboardInterrupt),
                                   mock.patch.object(H.os, 'unlink')))

    def test_a_stage_stopped_half_way_leaves_no_file_and_no_staged(self):
        """Killed after the bytes, before the rename: no handoff.json the upload could take, staged never true."""
        out, stage = os.path.join(self.d, 'out'), os.path.join(self.d, 'stage')
        self.write(self.producer, 'r.txt', b'x' * 100000)
        cwd = os.getcwd()
        os.chdir(self.producer)
        self.addCleanup(os.chdir, cwd)
        fsync, unlink = self.killed()
        with mock.patch.dict(os.environ, {'GITHUB_OUTPUT': out}), fsync, unlink, self.assertRaises(KeyboardInterrupt):
            quiet(H.main, ['put', '--name', 'r', '--path', 'r.txt', '--stage-dir', stage])
        self.assertFalse(os.path.exists(out))
        left = [f for _, _, fs in os.walk(stage) for f in fs]
        self.assertEqual([f for f in left if not f.startswith('.handoff-')], [])
        self.assertEqual(len(left), 1)      # the temporary file, which no upload names

    def test_a_consumer_stopped_half_way_leaves_no_file(self):
        self.assertEqual(self.put(b'whole')['ok'], 'true')
        where = self.get(stop_after='download')
        self.assertIsNone(where)
        downloaded = [os.path.join(r, 'handoff.json') for r, _, fs in
                      os.walk(os.path.join(self.d, 'runner-temp-consumer')) if 'handoff.json' in fs][0]
        cwd = os.getcwd()
        os.chdir(self.consumer)
        self.addCleanup(os.chdir, cwd)
        fsync, unlink = self.killed()
        with mock.patch.dict(os.environ, {'GITHUB_OUTPUT': os.path.join(self.d, 'out')}), fsync, unlink, \
                self.assertRaises(KeyboardInterrupt):
            quiet(H.main, ['get', '--name', 'report', '--path', 'in/report.json', '--ready', 'true',
                           '--outcome', 'success', '--dir', os.path.dirname(downloaded)])
        self.absent()
        left = os.listdir(os.path.join(self.consumer, 'in'))
        self.assertEqual([f for f in left if not f.startswith('.handoff-')], [])

    def test_a_write_that_fails_leaves_nothing(self):
        """A write that fails (a full disk) removes its temporary file: path holds nothing."""
        self.assertEqual(self.put(b'whole')['ok'], 'true')
        with mock.patch.object(H.os, 'fsync', side_effect=OSError(28, 'No space left on device')):
            folder = os.path.join(self.d, 'w')
            with self.assertRaises(OSError):
                H.write_atomic(os.path.join(folder, 'f'), b'data')
        self.assertEqual(os.listdir(folder), [])


class Inputs(Base):
    def test_bad_inputs_are_ok_false_not_a_failed_step(self):
        for name, path, retention in [('', 'src.txt', '1'), ('a/b', 'src.txt', '1'), ('-x', 'src.txt', '1'),
                                      ('r', '../src.txt', '1'), ('r', '/etc/hosts', '1'), ('r', '.', '1'),
                                      ('r', 'src.txt', '0'), ('r', 'src.txt', '91'), ('r', 'src.txt', 'x'),
                                      ('r', 'missing.txt', '1')]:
            with self.subTest(name=name, path=path, retention=retention):
                put = self.play(PUT, self.producer, {'name': name, 'path': path, 'retention-days': retention})
                self.assertEqual(put['ok'], 'false')
        self.assertEqual(self.store.calls, [])
        for name, path in [('a b', 'x'), ('r', '../x'), ('r', '/tmp/x')]:
            with self.subTest(name=name, path=path):
                self.assertEqual(self.get(name=name, path=path)['ok'], 'false')
        self.assertEqual(self.store.calls, [])

    def test_a_folder_at_the_get_path_is_left_and_refused(self):
        os.makedirs(os.path.join(self.consumer, 'in/report.json'))
        self.assertEqual(self.get()['ok'], 'false')
        self.assertTrue(os.path.isdir(os.path.join(self.consumer, 'in/report.json')))

    def test_the_file_must_be_utf8_text_and_not_a_symlink(self):
        self.assertIn('not UTF-8', self.put(b'\xff\xfe')['reason'])
        self.write(self.producer, 'secret', b'outside the path')
        os.symlink('../secret', os.path.join(self.producer, 'out/link'))
        self.assertIn('symlink is refused', self.put(None, path='out/link')['reason'])
        with mock.patch.object(H, 'MAX_PAYLOAD', 4):
            self.assertIn('over 4', self.call_put(b'12345'))
        self.assertEqual(self.store.calls, [])

    def call_put(self, data):
        self.write(self.producer, 'big', data)
        cwd = os.getcwd()
        os.chdir(self.producer)
        try:
            with mock.patch.dict(os.environ, {'GITHUB_OUTPUT': os.path.join(self.d, 'o')}):
                quiet(H.main, ['put', '--name', 'r', '--path', 'big', '--stage-dir', os.path.join(self.d, 's')])
        finally:
            os.chdir(cwd)
        with open(os.path.join(self.d, 'o')) as f:
            return f.read()

    def test_a_newline_in_an_input_adds_no_output(self):
        put = self.play(PUT, self.producer, {'name': 'r', 'path': 'x\nstaged=true'})
        self.assertEqual(put['ok'], 'false')
        self.assertEqual(self.store.calls, [])

    def test_a_stage_that_cannot_run_hands_off_nothing(self):
        """No python3 for the first step: nothing staged or uploaded, ok false."""
        self.assertEqual(self.put(fail_on='stage')['ok'], 'false')
        self.assertEqual(self.store.calls, [])


class GitClean(Base):
    """A .git config actions/checkout does not write: no git runs, ok false, the step still passes."""

    def plant(self, repo):
        self.git(repo, 'config', 'core.fsmonitor', 'touch pwned')

    def test_put_refuses(self):
        self.plant(self.producer)
        put = self.put()
        self.assertEqual((put['ok'], put['reason']), ('false', "git-clean refused the checkout's git config"))
        self.assertEqual(self.store.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.producer, 'pwned')))

    def test_get_refuses_and_still_clears_the_path(self):
        self.assertEqual(self.put()['ok'], 'true')
        self.plant(self.consumer)
        self.write(self.consumer, 'in/report.json', b'stale')
        got = self.get()
        self.assertEqual(got['ok'], 'false')
        self.assertIn('git-clean refused', got['reason'])
        self.absent()
        self.assertEqual([c[0] for c in self.store.calls], ['upload'])
        self.assertFalse(os.path.exists(os.path.join(self.consumer, 'pwned')))


class Pins(unittest.TestCase):
    def test_upload_and_download_are_pinned_by_full_sha(self):
        for folder, uses in ((PUT, UPLOAD), (GET, DOWNLOAD)):
            with open(os.path.join(folder, 'action.yml')) as f:
                text = f.read()
            self.assertEqual(re.findall(r'uses: (\S+)', text), [uses])
            self.assertRegex(text, re.escape(uses) + r' # v\d+\.\d+\.\d+\n')

    def test_upload_overwrites_and_download_reads_this_run_only(self):
        with open(os.path.join(PUT, 'action.yml')) as f:
            put = yaml.safe_load(f)['runs']['steps']
        with open(os.path.join(GET, 'action.yml')) as f:
            get = yaml.safe_load(f)['runs']['steps']
        upload = [s for s in put if 'uses' in s][0]
        self.assertEqual(upload['with']['overwrite'], True)
        self.assertTrue(upload['continue-on-error'])
        download = [s for s in get if 'uses' in s][0]
        self.assertEqual(set(download['with']), {'name', 'path'})
        self.assertTrue(download['continue-on-error'])


class Example(unittest.TestCase):
    """The README's caller example: the consumer picks its runner from the producer's ok and still runs when the
    producer failed or was cut off."""

    def setUp(self):
        with open(os.path.join(HERE, 'handoff-caller.example.yml')) as f:
            self.wf = yaml.safe_load(f)

    def test_runner_from_ok(self):
        jobs = self.wf['jobs']
        build, check = jobs['build'], jobs['check']
        self.assertEqual(check['needs'], 'build')
        self.assertEqual(check['if'].replace(' ', ''), '${{!cancelled()}}')
        put = [s for s in build['steps'] if 'handoff-put' in str(s.get('uses'))][0]
        self.assertEqual(build['outputs']['handoff'], '${{ steps.%s.outputs.ok }}' % put['id'])
        self.assertTrue(put['continue-on-error'])
        expression = re.fullmatch(r'\$\{\{(.*)\}\}', check['runs-on'].strip(), re.S)[1]
        light, fallback = 'ubuntu-latest', ['self-hosted', 'build']
        for ok, want in [('true', light), ('false', fallback), ('', fallback), (None, fallback)]:
            with self.subTest(ok=ok):
                got = evaluate(expression, {'needs': {'build': {'outputs': {'handoff': ok} if ok is not None else {}}}})
                self.assertEqual(got, want)
        steps = check['steps']
        get = [s for s in steps if 'handoff-get' in str(s.get('uses'))][0]
        self.assertNotIn('continue-on-error', get)

    def test_no_project_facts_in_the_actions(self):
        for folder in (PUT, GET):
            with open(os.path.join(folder, 'action.yml')) as f:
                text = f.read().lower()
            for fact in ('self-hosted', 'ubuntu', 'runs-on', 'gradle'):
                self.assertNotIn(fact, text, folder)


if __name__ == '__main__':
    unittest.main()
