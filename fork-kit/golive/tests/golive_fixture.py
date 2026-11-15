"""A fake machine for the go-live kit's tests, all in one temporary folder: a T3 home served by a fake boot
service (stub systemctl and systemd-run that only record), a real child process standing in for the server (a
copy of `sleep` placed at the home's runtime path, so /proc shows it like the real one), a staged release with
SHA256SUMS, an archive and its extracted runtime (a stub `t3` that prints a report and records how it was run),
and a machine file. Nothing here touches a real T3 home, a real unit or ~/.t3; PATH holds only the stubs.
"""
import hashlib, io, json, os, shutil, signal, subprocess, sys, tarfile, tempfile

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMON = os.path.join(os.path.dirname(KIT), 'common')
sys.path[:0] = [os.path.join(KIT, 'lib'), COMMON]

from golive import config, live, osadapter  # noqa: E402

LIVE, TARGET = '0.1.9', '0.1.10-nightly.20261114.4682.fork.1'

STUB_T3 = '''#!{python} -I
import json, os, sys
root = os.path.dirname(os.path.realpath(sys.argv[sys.argv.index('--base-dir') + 1]))
with open(os.path.join(root, 'stub-t3-calls.jsonl'), 'a') as f:
    f.write(json.dumps({{'argv': sys.argv[1:], 'env': dict(os.environ), 'cwd': os.getcwd()}}) + '\\n')
with open(os.path.join(root, 'stub-report.txt')) as f:
    sys.stdout.write(f.read())
'''

STUB_SYSTEMCTL = '''#!{python} -I
import json, os, sys
root = {root!r}
with open(os.path.join(root, 'systemctl-calls.jsonl'), 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
with open(os.path.join(root, 'service.json')) as f:
    svc = json.load(f)
if sys.argv[1:3] == ['--user', 'show']:
    print('\\n'.join(f'{{k}}={{v}}' for k, v in svc.items()))
sys.exit(0)
'''

STUB_SYSTEMD_RUN = '''#!{python} -I
import json, os, sys
with open(os.path.join({root!r}, 'systemd-run-calls.jsonl'), 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
'''


def report(**over):
    """A report as fork PR "read-only report command" prints it (only the keys the kit reads, plus a few)."""
    base = {
        'reportVersion': 1, 'version': TARGET, 'home': {'baseDir': '', 'exists': True},
        'database': {'state': 'present', 'openMode': 'readonly-shm', 'pendingMigrations': [82, 83],
                     'unknownMigrations': [], 'counts': {'projects': 3, 'projectsArchived': 0, 'threads': 40,
                                                         'threadsArchived': 2}},
        'settings': {'state': 'present', 'parseWarning': None, 'providerInstances': []},
        'fork': {'features': {'agentSlots': 'off', 'waitFor': 'off', 'usagePacingGate': 'off',
                              'automaticUpdates': 'off'}, 'featuresOn': [], 'keysInFile': [], 'settings': {}},
        'problems': [], 'ok': True,
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


class Machine:
    def __init__(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='golive-test-'))
        os.chmod(self.root, 0o700)
        self.home = self.path('home')
        self.stage = self.path('stage')
        self.state = self.path('kit-state')
        self.backups = self.path('backups')
        self.bin = self.path('bin')
        self.server = None
        self._saved = (config.MACHINE_FILE, config.MACHINE_OWNER, osadapter.SYSTEMCTL, osadapter.SYSTEMD_RUN,
                       os.environ.get('PATH'))
        for d in (self.bin, self.backups, self.path('etc')):
            os.makedirs(d)
            os.chmod(d, 0o755)  # a umask of 002 would leave them group-writable, which the loader refuses
        for name, text in (('systemctl', STUB_SYSTEMCTL), ('systemd-run', STUB_SYSTEMD_RUN)):
            self.write(f'bin/{name}', text.format(python=sys.executable, root=self.root), mode=0o755)
        os.environ['PATH'] = self.bin
        config.MACHINE_FILE, config.MACHINE_OWNER = self.path('etc/machine.json'), os.getuid()
        osadapter.SYSTEMCTL = os.path.join(self.bin, 'systemctl')
        osadapter.SYSTEMD_RUN = os.path.join(self.bin, 'systemd-run')

    def path(self, rel):
        return os.path.join(self.root, rel)

    def write(self, rel, text, mode=0o644):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb' if isinstance(text, bytes) else 'w') as f:
            f.write(text)
        os.chmod(p, mode)
        return p

    def close(self):
        if self.server is not None:
            kill(self.server)
            self.server = None
        (config.MACHINE_FILE, config.MACHINE_OWNER, osadapter.SYSTEMCTL, osadapter.SYSTEMD_RUN, path) = self._saved
        os.environ['PATH'] = path or ''
        shutil.rmtree(self.root, ignore_errors=True)

    # --- the pieces; each test changes what it needs afterwards ---
    def standard(self, version=LIVE, target=TARGET):
        self.config(targetVersion=target)
        self.t3_home(version)
        self.unit()
        self.stage_release(target)
        self.set_report(report(version=target))
        return self

    def config(self, **golive):
        section = {'home': self.home, 'targetVersion': TARGET, 'stageDir': self.stage, 'backupDir': self.backups,
                   'stateDir': self.state, 'freeFloorMb': 1, **golive}
        self.write('etc/machine.json', json.dumps({'version': 1, 'golive': {k: v for k, v in section.items()
                                                                             if v is not None}}))
        os.chmod(self.path('etc'), 0o755)

    def t3_home(self, version=LIVE):
        if self.server is not None:
            kill(self.server)
        runtime = self.write(f'home/runtime/versions/{version}/t3', open(shutil.which('sleep', path='/usr/bin:/bin'),
                                                                          'rb').read(), mode=0o755)
        self.write(f'home/runtime/versions/{version}/.install-complete', version)
        self.write('home/runtime/service-state.json', json.dumps({'protocol': 3, 'activeVersion': version}))
        self.write('home/userdata/statev2.sqlite', b'SQLite format 3\0' + b'x' * 4096)
        self.write('home/userdata/statev2.sqlite-wal', b'w' * 1024)
        self.write('home/userdata/settings.json', '{}')
        self.server = subprocess.Popen([runtime, '600'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
        self.runtime_file(pid=self.server.pid, serverVersion=version)

    def runtime_file(self, **fields):
        self.write('home/userdata/server-runtime.json', json.dumps({
            'version': 1, 'port': 3773, 'origin': 'http://127.0.0.1:3773', 'startedAt': '2026-01-01T00:00:00Z',
            'ownerId': 'x', **fields}))

    def unit(self, home=None, **props):
        unit_path = self.write('units/t3code.service',
                               f'[Service]\nEnvironment=T3CODE_HOME="{home or self.home}"\nExecStart=/x\n')
        self.service(**{'LoadState': 'loaded', 'ActiveState': 'active', 'UnitFileState': 'enabled',
                        'FragmentPath': unit_path, 'MainPID': os.getpid(), **props})

    def service(self, **props):
        self.write('service.json', json.dumps(props))

    def stage_release(self, version=TARGET, files=None):
        stem = f't3-{version}-{live.platform_key()}'
        files = files or {'t3': STUB_T3.format(python=sys.executable), 'lib/runtime.txt': 'runtime data'}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tar:
            for rel, text in files.items():
                data = text.encode()
                info = tarfile.TarInfo(f'{stem}/{rel}')
                info.size, info.mode = len(data), 0o755 if rel == 't3' else 0o644
                tar.addfile(info, io.BytesIO(data))
        archive = buf.getvalue()
        self.write(f'stage/{stem}.tar.gz', archive)
        self.write('stage/SHA256SUMS', f'{hashlib.sha256(archive).hexdigest()}  {stem}.tar.gz\n')
        for rel, text in files.items():
            self.write(f'stage/{version}/{rel}', text, mode=0o755 if rel == 't3' else 0o644)

    def set_report(self, value, prefix=''):
        self.write('stub-report.txt', prefix + (json.dumps(value, indent=2) if isinstance(value, dict) else value))

    # --- what happened ---
    def calls(self, name):
        try:
            with open(self.path(name)) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def snapshot(self, top=None):
        """Every file and folder under the T3 home: bytes hash, size, mtime, mode."""
        out = {}
        top = top or self.home
        for d, dirs, names in os.walk(top):
            for n in dirs + names:
                p = os.path.join(d, n)
                st = os.lstat(p)
                digest = hashlib.sha256(open(p, 'rb').read()).hexdigest() if os.path.isfile(p) else None
                out[os.path.relpath(p, top)] = (digest, st.st_size, st.st_mtime_ns, st.st_mode)
        return out


def kill(proc):
    try:
        proc.send_signal(signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()
