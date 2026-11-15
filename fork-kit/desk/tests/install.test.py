"""desk-install on a fake host in a temporary folder: every fixed path under PREFIX, the current user as root, and
every system command (useradd, systemctl, ssh-keygen, ...) a fake that records its arguments. A fake release folder
holds stubs of the fleet files. No root, no systemd, no network, no real user.

  python3 -I -B fork-kit/desk/tests/install.test.py
"""
import contextlib, datetime, importlib.machinery, importlib.util, io, json, os, shutil, stat, subprocess, sys
import tempfile, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(KIT))
SHA1, SHA2 = 'a' * 40, 'b' * 40
HOST_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI' + 'A' * 43
MACHINE = {'version': 1, 'desk': {'pool': 'desktop', 'host': 'fleet-host.example', 'hostKey': HOST_KEY, 'slots': 2}}

# One fake for every system command: argv[1] is the command's name. Its state lives in files beside it.
FAKE = r'''
import os, sys
here = os.path.dirname(os.path.abspath(__file__))
name, args = sys.argv[1], sys.argv[2:]
with open(os.path.join(here, 'calls.log'), 'a') as f:
    f.write(' '.join([name, *args]) + '\n')
def lines(n):
    try:
        return open(os.path.join(here, n)).read().split()
    except FileNotFoundError:
        return []
def put(n, items):
    open(os.path.join(here, n), 'w').write('\n'.join(items) + '\n')
fail = lines('fail')
if name in fail:
    put('fail', [x for x in fail if x != name])   # fails once
    sys.exit(9)
if name in lines('fail-after'):   # does its work, then dies (a run stopped right after it)
    put('fail-after', [x for x in lines('fail-after') if x != name])
    put('users', lines('users') + [args[-1]])
    sys.exit(9)
if name == 'id':
    sys.exit(0 if args[-1] in lines('users') else 1)
if name == 'useradd':
    put('users', lines('users') + [args[-1]])
if name == 'userdel':
    put('users', [u for u in lines('users') if u != args[-1]])
if name == 'ssh-keygen':
    key = args[args.index('-f') + 1]
    open(key, 'w').write('PRIVATE\n'); os.chmod(key, 0o600)
    open(key + '.pub', 'w').write('ssh-ed25519 AAAAfake desk-x\n')
if name == 'systemctl' and args[0] in ('is-active', 'is-enabled'):
    sys.exit(1 if args[-1] in lines('down') else 0)
if name == 'systemctl' and args[0] == 'list-units':   # runner units: the file `units`; `stuck` ones never stop
    print(''.join(f'{u} loaded active running runner\n' for u in lines('units')), end='')
if name == 'systemctl' and args[0] == 'stop':
    put('units', [u for u in lines('units') if u not in args[1:] or u in lines('stuck')])
if name == 'ip' and args[:2] == ['netns', 'list']:
    print(''.join(f'{n} (id: 0)\n' for n in lines('netns')), end='')
if name == 'ip' and args[:2] == ['netns', 'delete']:
    put('netns', [n for n in lines('netns') if n != args[2]])
    put('links', [n for n in lines('links') if n != 'ffh-' + args[2].rsplit('-', 1)[-1]])
if name == 'ip' and args[:2] == ['-o', 'link']:
    print(''.join(f'{i}: {n}@if2: <BROADCAST> mtu 1500\n' for i, n in enumerate(lines('links'), 5)), end='')
if name == 'ip' and args[:2] == ['link', 'delete']:
    put('links', [n for n in lines('links') if n != args[2]])
'''

STUBS = {
    '.github/scripts/fork-fleet-launch.sh': '#!/bin/bash\necho "launch $*" >> {log}\n'
                                            '[ "$1" = active ] && cat {here}/active 2>/dev/null\n'
                                            'if [ "$1" = stop ]; then grep -vx "$2" {here}/active > {here}/a.new;'
                                            ' mv {here}/a.new {here}/active; fi\nexit 0\n',
    '.github/scripts/fork-fleet-walls.py': '#!/usr/bin/python3 -I\nimport sys, os\n'
                                           'v = sys.argv[1]\nif v == "os":\n'
                                           '    print("linux"); sys.exit(int(open("{here}/os-exit").read()))\n'
                                           'bad = os.path.exists("{here}/wall-fails")\n'
                                           'print("PASS  os  linux")\nprint(("FAIL  vsock  closed: open" if bad else'
                                           ' "PASS  vsock  closed"))\nsys.exit(1 if bad else 0)\n',
    '.github/scripts/fork-fleet.py': 'import sys\nprint("::error::no Runner.Worker above this hook")\nsys.exit(1)\n',
    '.github/scripts/fork-fleet-desk-agent.py': 'import os, sys\nif sys.argv[-1] == "check" and '
                                                'os.path.exists("{here}/agent-bad"):\n'
                                                '    print("bad settings", file=sys.stderr); sys.exit(2)\n'
                                                'print("settings ok")\n',
    '.github/scripts/fork-fleet-desk-wire.py': '# wire\n',
    '.github/scripts/fork-fleet-desk-seed.py': '# seed\n',
    '.github/scripts/fork-fleet-hook.sh': '#!/bin/bash\nexit 0\n',
    '.github/scripts/fork-fleet-job-paths.txt': '/usr\n',
    '.github/fork-fleet.json': json.dumps({'pools': [{'name': 'desktop'}, {'name': 'vm'}]}),
    '.github/scripts/fork-fleet-walls/linux': '#!/bin/sh\n',
    '.github/scripts/fork-fleet-walls/linux.fixture.json': '{}\n',
    '.github/scripts/fork-fleet-walls/wsl2': '#!/bin/sh\n',
    '.github/scripts/fork-fleet-walls/wsl2.fixture.json': '{}\n',
    '.github/scripts/fork-fleet-desk-agent.service': '# agent unit\n[Service]\n',
    '.github/scripts/fork-fleet-refresh.service': '# refresh unit\n[Service]\n',
    '.github/scripts/fork-fleet-refresh.timer': '# refresh timer\n[Timer]\n',
    '.github/scripts/fork-fleet-seed.service': '# seed unit\n[Service]\n',
    '.github/scripts/fork-fleet-seed.timer': '# seed timer\n[Timer]\n',
}


def slurp(path, mode='r'):
    with open(path, mode) as f:
        return f.read()


def load():
    path = os.path.join(KIT, 'sbin', 'desk-install')
    loader = importlib.machinery.SourceFileLoader('desk_install', path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader('desk_install', loader))
    loader.exec_module(mod)
    return mod


class Desk(unittest.TestCase):
    def setUp(self):
        self.umask = os.umask(0o022)
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='desk-install-test-'))
        self.addCleanup(self.close)
        self.root, self.fakes = os.path.join(self.tmp, 'root'), os.path.join(self.tmp, 'fakes')
        os.makedirs(self.fakes)
        with open(os.path.join(self.fakes, 'fake.py'), 'w') as f:
            f.write(FAKE)
        self.write(os.path.join(self.fakes, 'os-exit'), '0')
        self.source = self.release()
        self.m = load()
        self.m.PREFIX, self.m.ROOT_UID = self.root, os.geteuid()
        self.m.MACHINE = os.path.join(self.tmp, 'machine', 'machine.json')
        fake = os.path.join(self.fakes, 'fake.py')
        self.m.CMD = {k: [sys.executable, '-I', fake, k] for k in self.m.CMD}
        self.m.CMD.update(python3=[sys.executable, '-I', '-B'], bash=['bash'])
        self.machine(MACHINE)
        self.write(self.path('/proc/sys/net/ipv4/ip_forward'), '1\n')

    def close(self):
        os.umask(self.umask)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, path, text, mode=0o644):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)
        os.chmod(path, mode)

    def release(self, name='release', extra=None):
        top = os.path.join(self.tmp, name)
        for rel, text in {**STUBS, **(extra or {})}.items():
            if text is not None:
                self.write(os.path.join(top, rel), text.replace('{log}', os.path.join(self.fakes, 'calls.log'))
                           .replace('{here}', self.fakes), 0o755)
        for rel in ('fork-kit/desk/sbin/desk-install', 'fork-kit/desk/README.md', 'fork-kit/common/machine_file.py',
                    'fork-kit/common/free_contract.py', 'fork-kit/common/free/memory'):
            self.write(os.path.join(top, rel), slurp(os.path.join(REPO, rel)), 0o755)
        return top

    def machine(self, data):
        path = self.m.MACHINE
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        self.write(path, json.dumps(data) if not isinstance(data, str) else data)

    def path(self, p):
        return self.root + p

    def calls(self):
        with contextlib.suppress(FileNotFoundError):
            with open(os.path.join(self.fakes, 'calls.log')) as f:
                return f.read().splitlines()
        return []

    def clear_calls(self):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(os.path.join(self.fakes, 'calls.log'))

    def run_main(self, *argv, stop_after=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.m.main(list(argv), stop_after=stop_after)
            except SystemExit as e:
                code = str(e)
        return code, out.getvalue(), err.getvalue()

    def install(self, *extra, sha=SHA1, stop_after=None, source=None):
        return self.run_main('install', '--source', source or self.source, '--sha', sha, *extra,
                             stop_after=stop_after)

    def read(self, p):
        with open(self.path(p)) as f:
            return f.read()

    def tree(self):
        """Every path under the fake root with its kind, mode and contents (no times)."""
        out = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                st = os.lstat(full)
                body = slurp(full, 'rb') if stat.S_ISREG(st.st_mode) else None
                out[full[len(self.root):]] = (stat.S_IFMT(st.st_mode), st.st_mode & 0o777, body)
        return out

    def template(self):
        self.write(self.path('/opt/fork-fleet/template/runner-template/.fork-fleet-version'), '2.330.0\n')

    # ------------------------------------------------------------------------------------------------- install

    def test_fresh_install_files_settings_users_key_units(self):
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read('/opt/fork-fleet/SOURCE'), SHA1 + '\n')
        for rel in ('fork-fleet-launch.sh', 'hook.sh', 'fork-fleet.py', 'fork-fleet.json', 'fork-fleet-walls.py',
                    'fork-fleet-walls/linux', 'fork-fleet-walls/wsl2.fixture.json', 'desk-install', 'machine_file.py'):
            self.assertTrue(os.path.isfile(self.path(f'/opt/fork-fleet/{rel}')), rel)
        self.assertEqual(stat.S_IMODE(os.stat(self.path('/opt/fork-fleet/hook.sh')).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(self.path('/opt/fork-fleet/fork-fleet.json')).st_mode), 0o644)
        self.assertFalse([n for n in os.listdir(self.path('/opt/fork-fleet')) if n.startswith('.kit-stage')])
        launch = self.read('/etc/fork-fleet/launch.conf')
        self.assertTrue(launch.startswith(self.m.HEADER))
        self.assertIn('RUNNER_USERS=fork-runner-1,fork-runner-2\nALLOW_PATHS=/var/lib/fork-fleet/seeds\n', launch)
        self.assertTrue(os.path.isfile(self.path('/opt/fork-fleet/fork-fleet-desk-seed.py')))
        self.assertNotIn('RESERVE_MB', launch)
        self.assertIn('POOL=desktop\nHOST=fleet-host.example\nPORT=22\nUSER=fleet-desk\n',
                      self.read('/etc/fork-fleet/desk.conf'))
        self.assertTrue(self.read('/etc/fork-fleet/desk-known_hosts').endswith(f'fleet-host.example {HOST_KEY}\n'))
        for f, mode in (('launch.conf', 0o600), ('desk.conf', 0o600), ('desk-key', 0o600), ('desk-kit.json', 0o600)):
            self.assertEqual(stat.S_IMODE(os.stat(self.path(f'/etc/fork-fleet/{f}')).st_mode), mode, f)
        self.assertEqual(stat.S_IMODE(os.stat(self.path('/etc/fork-fleet')).st_mode), 0o700)
        self.assertIn('ssh-ed25519 AAAAfake desk-x', out)
        self.assertEqual(self.read('/etc/sysctl.d/60-fork-fleet.conf'), self.m.HEADER + 'net.ipv4.ip_forward = 1\n')
        for u in self.m.UNITS:
            self.assertTrue(self.read(f'/etc/systemd/system/{u}').startswith(self.m.HEADER), u)
        calls = self.calls()
        self.assertEqual([c for c in calls if c.startswith('useradd')], [
            f'useradd --no-create-home --home-dir /nonexistent --user-group --shell /usr/sbin/nologin fork-runner-{i}'
            for i in (1, 2)])
        self.assertEqual(self.read('/etc/cron.deny'), 'fork-runner-1\nfork-runner-2\n')
        self.assertEqual(json.loads(self.read('/etc/fork-fleet/desk-kit.json'))['createdUsers'],
                         ['fork-runner-1', 'fork-runner-2'])
        order = [c for c in calls if c.startswith(('systemctl', 'launch')) and not c.startswith('systemctl show')]
        self.assertEqual(order, ['systemctl stop t3code-fleet-refresh.timer t3code-fleet-refresh.service '
                                 't3code-fleet-seed.timer t3code-fleet-seed.service fork-fleet-desk-agent.service', 'systemctl daemon-reload',
                                 'systemctl enable --now t3code-fleet-refresh.timer t3code-fleet-seed.timer',
                                 'launch prune',
                                 'systemctl start --no-block t3code-fleet-refresh.service',
                                 'systemctl enable fork-fleet-desk-agent.service',
                                 'systemctl restart fork-fleet-desk-agent.service'])
        self.template()
        code, out, _ = self.run_main('check')
        self.assertEqual(code, 0, out)
        self.assertTrue(out.endswith('ALL PASS\n'), out)
        self.assertIn('PASS  vsock  closed', out)

    def test_runs_again_and_changes_nothing(self):
        self.assertEqual(self.install()[0], 0)
        self.template()
        before = self.tree()
        self.clear_calls()
        code, _, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tree(), before)
        calls = self.calls()
        self.assertFalse([c for c in calls if c.startswith(('useradd', 'ssh-keygen', 'systemctl stop',
                                                            'systemctl restart'))], calls)
        self.assertIn('systemctl start fork-fleet-desk-agent.service', calls)   # a no-op for a running agent
        self.assertNotIn('systemctl start --no-block t3code-fleet-refresh.service', calls)   # the template is there

    def test_a_changed_setting_restarts_the_agent(self):
        self.assertEqual(self.install()[0], 0)
        self.machine({'version': 1, 'desk': {**MACHINE['desk'], 'reserveMb': 2048}})
        self.clear_calls()
        self.assertEqual(self.install()[0], 0)
        self.assertIn('systemctl restart fork-fleet-desk-agent.service', self.calls())

    def test_stopped_at_any_step_the_next_run_ends_it(self):
        self.assertEqual(self.install()[0], 0)
        want = self.tree()
        for step in ('check', 'users', 'stage', 'files', 'settings', 'units'):
            with self.subTest(step=step):
                shutil.rmtree(self.root)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(self.fakes, 'users'))
                self.write(self.path('/proc/sys/net/ipv4/ip_forward'), '1\n')
                code, _, _ = self.install(stop_after=step)
                self.assertIn('stopped after', str(code))
                if step in ('stage', 'files', 'settings'):
                    self.assertNotEqual(self.run_main('check')[0], 0)
                code, _, err = self.install()
                self.assertEqual(code, 0, err)
                self.assertEqual(self.tree(), want)

    def test_upgrade_replaces_changed_files_and_removes_gone_ones(self):
        self.assertEqual(self.install()[0], 0)
        launcher = os.stat(self.path('/opt/fork-fleet/fork-fleet-launch.sh')).st_ino
        new = self.release('release2', {'.github/scripts/fork-fleet-walls/wsl2': None,
                                        '.github/scripts/fork-fleet-walls/wsl2.fixture.json': None,
                                        '.github/scripts/fork-fleet-hook.sh': '#!/bin/bash\nexit 0 # new\n'})
        self.clear_calls()
        code, _, err = self.install(sha=SHA2, source=new)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read('/opt/fork-fleet/SOURCE'), SHA2 + '\n')
        self.assertFalse(os.path.exists(self.path('/opt/fork-fleet/fork-fleet-walls/wsl2')))
        self.assertIn('# new', self.read('/opt/fork-fleet/hook.sh'))
        self.assertEqual(os.stat(self.path('/opt/fork-fleet/fork-fleet-launch.sh')).st_ino, launcher)  # unchanged
        calls = self.calls()
        stop = 'systemctl stop t3code-fleet-refresh.timer t3code-fleet-refresh.service fork-fleet-desk-agent.service'
        self.assertLess(calls.index(stop), calls.index('systemctl restart fork-fleet-desk-agent.service'))
        self.assertEqual(json.loads(self.read('/etc/fork-fleet/desk-kit.json'))['sha'], SHA2)

    def test_project_fleet_config_is_used_and_kept(self):
        project = os.path.join(self.tmp, 'project.json')
        self.write(project, json.dumps({'pools': [{'name': 'desktop'}], 'project': True}))
        code, _, err = self.install('--fleet-config', project)
        self.assertEqual(code, 0, err)
        self.assertIn('"project": true', self.read('/opt/fork-fleet/fork-fleet.json'))
        code, _, err = self.install(sha=SHA2)   # no --fleet-config: the project's stays
        self.assertEqual(code, 0, err)
        self.assertIn('"project": true', self.read('/opt/fork-fleet/fork-fleet.json'))
        code, _, err = self.install('--release-fleet-config', sha=SHA2)   # and back to the release's own
        self.assertEqual(code, 0, err)
        self.assertNotIn('"project": true', self.read('/opt/fork-fleet/fork-fleet.json'))
        self.assertEqual(json.loads(self.read('/etc/fork-fleet/desk-kit.json'))['fleetConfig'], 'release')
        code, _, err = self.install('--release-fleet-config', '--fleet-config', project)
        self.assertEqual(code, 1)
        self.assertIn('one or the other', err)

    def test_refusals_change_nothing(self):
        cases = {
            'not a release sha': (lambda: None, ['--sha', 'abc'], '--sha must be'),
            'no machine file': (lambda: os.unlink(self.m.MACHINE), [], 'machine file'),
            'no desk section': (lambda: self.machine({'version': 1}), [], 'no "desk" section'),
            'bad host key': (lambda: self.machine({'version': 1, 'desk': {**MACHINE['desk'],
                                                                          'hostKey': 'ssh-rsa AAAA'}}), [], 'hostKey'),
            'too many slots': (lambda: self.machine({'version': 1, 'desk': {**MACHINE['desk'], 'slots': 9}}), [],
                               'slots'),
            'bad net base': (lambda: self.machine({'version': 1, 'desk': {**MACHINE['desk'], 'netBase': '10.256'}}),
                             [], 'netBase'),
            'unknown key': (lambda: self.machine({'version': 1, 'desk': {**MACHINE['desk'], 'token': 'x'}}), [],
                            'token'),
            'os not supported': (lambda: self.write(os.path.join(self.fakes, 'os-exit'), '3'), [],
                                 'not supported yet'),
            'an install the kit did not make': (
                lambda: self.write(self.path('/opt/fork-fleet/fork-fleet-launch.sh'), 'old'), [],
                'this kit did not make'),
            'a foreign unit': (lambda: self.write(self.path('/etc/systemd/system/fork-fleet-desk-agent.service'), 'x'),
                               [], 'this kit did not make'),
            'no such fleet config': (lambda: None, ['--fleet-config', '/nonexistent/x.json'], 'no such file'),
            'a drop-in for the agent': (lambda: self.write(self.path(
                '/etc/systemd/system/fork-fleet-desk-agent.service.d/override.conf'), '[Service]\nExecStart=\n'), [],
                'override.conf changes fork-fleet-desk-agent.service'),
            'a dash-prefix drop-in': (lambda: self.write(self.path(
                '/run/systemd/system/t3code-fleet-.timer.d/x.conf'), '[Timer]\n'), [], 'changes t3code-fleet'),
            'a record of another shape': (lambda: self.write(self.path('/etc/fork-fleet/desk-kit.json'),
                                                             '{"version": 1}'), [], 'not an install record'),
        }
        for name, (setup, args, msg) in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.root, ignore_errors=True)
                os.makedirs(self.root)
                self.machine(MACHINE)
                self.write(os.path.join(self.fakes, 'os-exit'), '0')
                setup()
                before = self.tree()
                self.clear_calls()
                argv = ['install', '--source', self.source, '--sha', SHA1, *args]
                if '--sha' in args:
                    argv = ['install', '--source', self.source, *args]
                code, _, err = self.run_main(*argv)
                self.assertEqual(code, 1)
                self.assertIn(msg, err)
                self.assertEqual(self.tree(), before)
                self.assertFalse([c for c in self.calls() if not c.startswith(('id ', 'systemctl show'))],
                                 self.calls())

    def test_bad_release_is_not_installed(self):
        self.assertEqual(self.install()[0], 0)
        before = {k: v for k, v in self.tree().items() if k != '/etc/fork-fleet/desk-kit.json'}
        for name, extra, msg in (
                ('no pool', {'.github/fork-fleet.json': json.dumps({'pools': [{'name': 'vm'}]})}, "no pool 'desktop'"),
                ('gate does not load', {'.github/scripts/fork-fleet.py': 'raise SystemExit("bad config")\n'},
                 'the gate does not load'),
                ('launcher syntax', {'.github/scripts/fork-fleet-launch.sh': '#!/bin/bash\nif then\n'}, 'bash -n'),
                ('pools not a list', {'.github/fork-fleet.json': json.dumps({'pools': {'desktop': {}}})},
                 'not a list of pools')):
            with self.subTest(name):
                bad = self.release('bad-' + name.replace(' ', '-'), extra)
                code, _, err = self.install(sha=SHA2, source=bad)
                self.assertEqual(code, 1)
                self.assertIn(msg, err)
                after = {k: v for k, v in self.tree().items() if k != '/etc/fork-fleet/desk-kit.json'}
                self.assertEqual(after, before)

    def test_key_is_made_once_and_a_half_key_is_refused(self):
        self.assertEqual(self.install()[0], 0)
        os.unlink(self.path('/etc/fork-fleet/desk-key.pub'))
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn('without', err)
        os.unlink(self.path('/etc/fork-fleet/desk-key'))
        self.write(self.path('/etc/fork-fleet/.desk-key.new'), 'torn')   # a keygen that stopped
        code, _, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read('/etc/fork-fleet/desk-key'), 'PRIVATE\n')
        self.assertFalse(os.path.exists(self.path('/etc/fork-fleet/.desk-key.new')))

    def test_reserve_and_port_reach_the_settings(self):
        self.machine({'version': 1, 'desk': {**MACHINE['desk'], 'reserveMb': 4096, 'port': 2222, 'slots': 1,
                                             'netBase': '10.77'}})
        self.assertEqual(self.install()[0], 0)
        self.assertIn('RUNNER_USERS=fork-runner-1\nALLOW_PATHS=/var/lib/fork-fleet/seeds\nRESERVE_MB=4096\n'
                      'NET_BASE=10.77\n',
                      self.read('/etc/fork-fleet/launch.conf'))
        self.assertTrue(self.read('/etc/fork-fleet/desk-known_hosts').endswith(
            f'[fleet-host.example]:2222 {HOST_KEY}\n'))

    def test_a_free_script_reaches_the_settings_and_the_check(self):
        script = os.path.join(self.tmp, 'free')
        self.machine({'version': 1, 'desk': {**MACHINE['desk'], 'freeCommand': script}})
        self.assertEqual(self.install()[0], 0)
        self.assertIn(f'FREE_COMMAND={script}\n', self.read('/etc/fork-fleet/launch.conf'))
        self.assertEqual(stat.S_IMODE(os.stat(self.path('/opt/fork-fleet/free-memory')).st_mode), 0o755)
        self.template()
        now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        for code, body, want in ((0, '"free": true', 'PASS  free-script  ' + script + ': free\n'),
                                 (75, '"free": false, "reason": "a game"', 'busy: a game'),
                                 (75, '"free": true', 'FAIL  free-script')):
            self.write(script, f"#!/bin/sh\necho '{{\"v\": 1, {body}, \"at\": \"{now}\"}}'\nexit {code}\n", 0o755)
            out = self.run_main('check')[1]
            self.assertIn(want, out)
        self.machine({'version': 1, 'desk': {**MACHINE['desk'], 'freeCommand': 'free'}})
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn('freeCommand', err)

    def test_existing_user_is_used_not_recorded(self):
        self.write(os.path.join(self.fakes, 'users'), 'fork-runner-1\n')
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(json.loads(self.read('/etc/fork-fleet/desk-kit.json'))['createdUsers'], ['fork-runner-2'])
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertEqual(slurp(os.path.join(self.fakes, 'users')).split(), ['fork-runner-1'])

    def test_a_user_made_by_a_run_that_died_is_removed_by_uninstall(self):
        self.write(os.path.join(self.fakes, 'fail-after'), 'useradd\n')
        code, _, _ = self.install()
        self.assertEqual(code, 1)
        self.assertEqual(slurp(os.path.join(self.fakes, 'users')).split(), ['fork-runner-1'])
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertEqual(slurp(os.path.join(self.fakes, 'users')).split(), [])

    def test_agent_refusing_its_settings_starts_nothing(self):
        self.write(os.path.join(self.fakes, 'agent-bad'), '')
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn('the desk agent refuses its settings', err)
        self.assertFalse([c for c in self.calls() if c.startswith(('systemctl enable', 'systemctl restart'))])

    # --------------------------------------------------------------------------------------------------- check

    def test_check_names_each_failure(self):
        self.assertEqual(self.install()[0], 0)
        self.write(os.path.join(self.fakes, 'wall-fails'), '')
        self.write(os.path.join(self.fakes, 'agent-bad'), '')
        self.write(os.path.join(self.fakes, 'down'), 'fork-fleet-desk-agent.service\n')
        self.write(self.path('/opt/fork-fleet/hook.sh'), '#!/bin/bash\nevil\n', 0o755)
        self.write(self.path('/proc/sys/net/ipv4/ip_forward'), '0\n')
        os.unlink(os.path.join(self.fakes, 'users'))
        code, out, _ = self.run_main('check')
        self.assertEqual(code, 1)
        for line in ('FAIL  kit  /opt/fork-fleet/hook.sh: differs', 'FAIL  vsock  closed: open',
                     'FAIL  agent-settings  bad settings', 'FAIL  unit  fork-fleet-desk-agent.service',
                     'PASS  unit  t3code-fleet-refresh.timer', 'PASS  unit  t3code-fleet-seed.timer',
                     'FAIL  template', 'FAIL  runner-user  fork-runner-1',
                     'FAIL  ip-forward'):
            self.assertIn(line, out)
        self.assertTrue(out.endswith('8 FAIL\n'), out)

    def test_check_fails_on_a_drop_in_and_on_settings_the_machine_file_no_longer_gives(self):
        self.assertEqual(self.install()[0], 0)
        self.template()
        self.assertEqual(self.run_main('check')[0], 0)
        self.machine({'version': 1, 'desk': {**MACHINE['desk'], 'slots': 1}})
        code, out, _ = self.run_main('check')
        self.assertEqual(code, 1)
        self.assertIn('FAIL  settings  /etc/fork-fleet/launch.conf differs from the machine file', out)
        self.machine(MACHINE)
        self.write(self.path('/etc/systemd/system/fork-fleet-desk-agent.service.d/override.conf'),
                   '[Service]\nExecStart=\nExecStart=/bin/sh -c evil\n')
        code, out, _ = self.run_main('check')
        self.assertEqual(code, 1)
        self.assertIn('override.conf: a drop-in the kit did not write changes fork-fleet-desk-agent.service', out)

    def test_check_before_install_fails_without_writing(self):
        code, out, _ = self.run_main('check')
        self.assertEqual(code, 1)
        self.assertIn('FAIL  kit', out)
        self.assertFalse(os.path.exists(self.path('/opt')))

    # ----------------------------------------------------------------------------------------------- uninstall

    def test_uninstall_leaves_nothing_of_its_own(self):
        self.write(self.path('/etc/cron.deny'), 'someone\n')
        self.write(self.path('/etc/systemd/system/other.service'), 'x')
        self.write(os.path.join(self.fakes, 'users'), 'someone\n')
        self.assertEqual(self.install()[0], 0)
        self.write(os.path.join(self.fakes, 'active'), 'desktop-1\n')
        self.write(self.path('/var/lib/fork-fleet/runners/x'), '')
        self.write(self.path('/var/lib/fork-fleet/seeds/.v/m2.1/data/x'), '')
        self.write(os.path.join(self.fakes, 'netns'), 'fork-fleet-fork-runner-1\n')   # the namespaces that are there
        self.clear_calls()
        code, out, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        calls = self.calls()
        for c in ('systemctl disable --now fork-fleet-desk-agent.service',
                  'systemctl disable --now t3code-fleet-seed.timer', 'launch active', 'launch stop desktop-1',
                  'launch prune', 'ip netns delete fork-fleet-fork-runner-1', 'nft delete table inet fork_fleet',
                  'userdel fork-runner-1', 'userdel fork-runner-2'):
            self.assertIn(c, calls)
        self.assertLess(calls.index('launch stop desktop-1'), calls.index('userdel fork-runner-1'))
        for path in ('/opt/fork-fleet', '/etc/fork-fleet', '/var/lib/fork-fleet', '/etc/sysctl.d/60-fork-fleet.conf',
                     '/etc/systemd/system/fork-fleet-desk-agent.service'):
            self.assertFalse(os.path.lexists(self.path(path)), path)
        self.assertEqual(self.read('/etc/cron.deny'), 'someone\n')
        self.assertTrue(os.path.exists(self.path('/etc/systemd/system/other.service')))
        self.assertEqual(slurp(os.path.join(self.fakes, 'users')).split(), ['someone'])
        code, out, _ = self.run_main('uninstall')   # again: nothing to do
        self.assertEqual(code, 0)
        self.assertIn('nothing here was installed by this kit', out)

    def test_uninstall_keeps_what_the_kit_did_not_make(self):
        self.assertEqual(self.install()[0], 0)
        for path in ('/etc/fork-fleet/token', '/opt/fork-fleet/notes.txt', '/var/lib/fork-fleet/other/x'):
            self.write(self.path(path), 'not the kit\'s\n')
        self.write(self.path('/var/lib/fork-fleet/runners/desktop-1/x'), '')
        code, out, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        left = {k for k in self.tree() if k.startswith(('/etc/fork-fleet', '/opt/fork-fleet', '/var/lib/fork-fleet'))}
        self.assertEqual(left, {'/etc/fork-fleet', '/etc/fork-fleet/token', '/opt/fork-fleet',
                                '/opt/fork-fleet/notes.txt', '/var/lib/fork-fleet', '/var/lib/fork-fleet/other',
                                '/var/lib/fork-fleet/other/x'})
        self.assertIn('left /etc/fork-fleet/token: the kit did not make it', out)

    def test_uninstall_removes_the_deny_files_it_made(self):
        self.write(self.path('/etc/cron.deny'), 'someone\n')   # there before: kept, without the kit's lines
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.read('/etc/at.deny'), 'fork-runner-1\nfork-runner-2\n')
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertFalse(os.path.lexists(self.path('/etc/at.deny')))   # with none, only root may use at again
        self.assertEqual(self.read('/etc/cron.deny'), 'someone\n')

    def test_uninstall_keeps_the_table_while_a_runner_still_runs(self):
        self.assertEqual(self.install()[0], 0)
        self.write(os.path.join(self.fakes, 'units'), 'fork-fleet-desktop-1.service\nfork-fleet-desktop-2.service\n')
        self.write(os.path.join(self.fakes, 'stuck'), 'fork-fleet-desktop-2.service\n')
        self.clear_calls()
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1)
        self.assertIn('fork-fleet-desktop-2.service is still running', err)
        calls = self.calls()
        self.assertIn('systemctl stop fork-fleet-desktop-1.service', calls)   # what the launcher did not stop
        self.assertFalse([c for c in calls if c.startswith(('nft', 'userdel', 'ip netns delete'))], calls)
        self.assertTrue(os.path.exists(self.path('/etc/fork-fleet/desk-kit.json')))
        os.unlink(os.path.join(self.fakes, 'stuck'))
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertIn('nft delete table inet fork_fleet', self.calls())

    def test_uninstall_removes_every_runner_namespace(self):
        self.write(os.path.join(self.fakes, 'users'), 'fork-runner-1\n')   # made by hand, not by the kit
        self.assertEqual(self.install()[0], 0)
        self.write(os.path.join(self.fakes, 'netns'), 'fork-fleet-fork-runner-1\nfork-fleet-fork-runner-2\nother\n')
        self.write(os.path.join(self.fakes, 'links'), 'ffh-1001\neth0\nffh-1009\n')
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertEqual(slurp(os.path.join(self.fakes, 'netns')).split(), ['other'])
        self.assertEqual(slurp(os.path.join(self.fakes, 'links')).split(), ['eth0'])

    def test_uninstall_removes_its_record_last(self):
        self.assertEqual(self.install()[0], 0)
        gone, unlink, scandir = [], os.unlink, os.scandir

        def record(path, *a, **kw):
            gone.append(os.path.basename(path))
            return unlink(path, *a, **kw)

        class Sorted:
            """Folders listed by name, so the record (desk-kit.json sorts before desk.conf and launch.conf) comes
            first: a whole-folder removal would take it before the rest, whatever order the file system gives."""
            def __init__(self, *a, **kw):
                self.it = scandir(*a, **kw)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.it.close()

            def __iter__(self):
                return iter(sorted(self.it, key=lambda e: e.name))

            def close(self):
                self.it.close()
        os.unlink, os.scandir = record, Sorted
        try:
            self.assertEqual(self.run_main('uninstall')[0], 0)
        finally:
            os.unlink, os.scandir = unlink, scandir
        self.assertEqual(gone[-1], 'desk-kit.json', gone)

    def test_uninstall_refuses_a_record_of_another_shape(self):
        self.assertEqual(self.install()[0], 0)
        for bad in ('[]', '{"version": 1, "createdUsers": "fork-runner-1", "fleetConfig": "release"}',
                    '{"version": 1, "createdUsers": ["root;x"], "fleetConfig": "release"}'):
            with self.subTest(bad):
                self.write(self.path('/etc/fork-fleet/desk-kit.json'), bad, 0o600)
                code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 1)
                self.assertIn('not an install record', err)

    def test_uninstall_stopped_half_way_runs_again(self):
        self.assertEqual(self.install()[0], 0)
        self.write(os.path.join(self.fakes, 'fail'), 'userdel\n')
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(self.path('/etc/fork-fleet/desk-kit.json')))   # the record stays
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.path('/etc/fork-fleet')))
        self.assertEqual(slurp(os.path.join(self.fakes, 'users')).split(), [])

    def test_uninstall_of_a_foreign_install_changes_nothing(self):
        self.write(self.path('/opt/fork-fleet/fork-fleet-launch.sh'), 'old')
        before = self.tree()
        code, out, _ = self.run_main('uninstall')
        self.assertEqual(code, 0)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.calls(), [])


class Release(unittest.TestCase):
    def test_every_file_the_kit_installs_is_in_this_repository(self):
        m = load()
        for src in [*m.FILES.values(), *m.UNITS.values(), m.WALLS]:
            self.assertTrue(os.path.exists(os.path.join(REPO, src)), src)
        self.assertTrue(os.listdir(os.path.join(REPO, m.WALLS)))

    def test_bootstrap_checks_its_arguments_before_anything(self):
        boot = os.path.join(KIT, 'bootstrap.sh')
        self.assertEqual(subprocess.run(['sh', '-n', boot]).returncode, 0)
        env = {'PATH': '/usr/bin:/bin'}
        for args, msg in ((['--repo', 'x'], '--repo must be'), (['--repo', 'a/b', '--sha', 'x'], '--sha must be'),
                          (['--nope'], 'unknown option'),
                          # flags after --fleet-config are checked too: a later --sha cannot slip past
                          (['--repo', 'a/b', '--fleet-config', boot, '--sha', 'x'], '--sha must be'),
                          (['--fleet-config', boot, '--repo', 'x'], '--repo must be'),
                          (['--repo', 'a/b', '--sha', 'a' * 40, '--fleet-config', '/nonexistent/f.json'],
                           'no such file'),
                          (['--repo', 'a/b', '--sha', 'a' * 40, '--fleet-config'], 'needs a value')):
            r = subprocess.run(['sh', boot, *args], capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 1)
            self.assertIn(msg, r.stderr)
        if os.geteuid() != 0:
            r = subprocess.run(['sh', boot, '--repo', 'a/b', '--sha', 'a' * 40], capture_output=True, text=True,
                               env=env)
            self.assertIn('run this as root', r.stderr)


if __name__ == '__main__':
    unittest.main()
