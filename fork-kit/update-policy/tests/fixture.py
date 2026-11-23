"""Shared by the update-policy tests: where the code is loaded from, a fake host in a temporary folder, and RedCheck.

The fake host is a folder that stands for / (cgroups, /proc, /run): idle.Host and tick.Machine read every file under
it (ROOT). Programs (busctl, apt-config, systemctl, sshd, the alert command, the reboot) are answered from a table,
and every call is recorded. RedCheck copies lib/ to a temporary folder, breaks one text in one module, and runs one
named test of a test file against that copy (UPDATE_POLICY_TEST_LIB names it). The test must then fail."""
import datetime, json, os, pwd, shutil, subprocess, sys, tempfile, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMON = os.path.join(os.path.dirname(KIT), 'common')
LIB_ENV = 'UPDATE_POLICY_TEST_LIB'
LIB = os.environ.get(LIB_ENV) or os.path.join(KIT, 'lib')
sys.path[:0] = [LIB, COMMON]

from updatepolicy import baseline, cli, idle, settings, tick, window  # noqa: E402,F401

UTC = datetime.timezone.utc
NO_INHIBITORS = (0, '{"type":"a(ssssuu)","data":[[]]}', '')
APT_OFF = (0, 'Unattended-Upgrade::Automatic-Reboot "false";\n', '')


def at(text):
    """'2026-11-24 05:00' (UTC) -> an aware datetime."""
    return datetime.datetime.fromisoformat(text).replace(tzinfo=UTC)


def make_settings(**over):
    base = {'role': 'dev', 'quietHour': '05:00', 'timeZone': 'UTC'}
    base.update(over)
    return settings.check(base, 'test')


class FakeHost(idle.Host):
    def __init__(self, root):
        self.ROOT = root
        self.calls = []
        self.answers = {'busctl': NO_INHIBITORS, 'apt-config': APT_OFF}
        self.dockers = {}           # socket -> names or why not
        self.users = {}             # name -> pwd-like entry
        self.free = {}              # path -> (verdict, reason)

    def run(self, argv, timeout=20, user=None, stdin=None, env=None):
        self.calls.append((list(argv), stdin, dict(env or {})))
        key = ' '.join(argv[:2]) if ' '.join(argv[:2]) in self.answers else argv[0]
        answer = self.answers.get(key, (None, '', f'{argv[0]}: not installed'))
        return answer(argv) if callable(answer) else answer

    def user(self, name):
        return self.users.get(name)

    def docker_names(self, sock, user=None):
        return self.dockers.get(sock, [])

    def free_answer(self, path):
        return self.free.get(path, ('free', None))


class Logs(list):
    def __call__(self, level, text):
        self.append((level, text))

    def text(self):
        return '\n'.join(t for _, t in self)


class FakeMachine(tick.Machine):
    def __init__(self, host, clock):
        super().__init__(host, Logs())
        self.clock = clock
        self.boot = at('2026-11-20 00:00')
        self.boot_ids = ['boot-1']
        self.reboots, self.alerts = [], []
        self.reboot_error = None
        self.alert_error = None

    def now(self):
        return self.clock

    def booted(self):
        return self.boot

    def boot_id(self):
        return self.boot_ids[-1]

    def reboot(self, why):
        self.reboots.append(why)
        return self.reboot_error

    def alert(self, command, event, text):
        self.alerts.append((event, text))
        return self.alert_error


class HostCase(unittest.TestCase):
    """self.root: an empty fake / ; self.host: a FakeHost on it."""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='update-policy-test-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.host = FakeHost(self.root)
        self.write('/sys/fs/cgroup/cgroup.controllers', 'cpu memory pids\n')      # cgroup v2
        os.makedirs(self.root + '/proc')

    def write(self, path, text='', mtime=None):
        full = self.root + path
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8') as f:
            f.write(text)
        if mtime is not None:
            os.utime(full, (mtime.timestamp(), mtime.timestamp()))
        return full

    def cgroup(self, path, populated=True):
        """A cgroup folder below /sys/fs/cgroup/user.slice with its cgroup.events."""
        self.write(f'/sys/fs/cgroup/user.slice/{path}/cgroup.events', f'populated {int(populated)}\nfrozen 0\n')

    def process(self, pid, *argv, ppid=1, uid=0, comm=None, cgroup='/system.slice/x.service', env=(), exe=None,
                cwd=None):
        """A fake /proc/<pid>: cmdline, stat (comm, ppid), status (uid), cgroup, environ (NAME=value strings), and
        the links exe and cwd when given."""
        comm = comm or os.path.basename(argv[0])[:15]
        self.write(f'/proc/{pid}/environ', ''.join(f'{e}\0' for e in ('PATH=/usr/bin',) + tuple(env)))
        for name, target in (('exe', exe), ('cwd', cwd)):
            link = self.root + f'/proc/{pid}/{name}'
            if os.path.lexists(link):
                os.unlink(link)
            if target:
                os.symlink(target, link)
        self.write(f'/proc/{pid}/cmdline', '\0'.join(argv) + '\0')
        self.write(f'/proc/{pid}/stat', f'{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 4194560 0 0 0 0\n')
        self.write(f'/proc/{pid}/status', f'Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n')
        self.write(f'/proc/{pid}/cgroup', f'0::{cgroup}\n')

    def pending(self, since, pkgs=()):
        self.write(tick.PENDING, '*** System restart required ***\n', mtime=since)
        if pkgs:
            self.write(tick.PENDING_PKGS, ''.join(f'{p}\n' for p in pkgs))


def fake_user(name, uid):
    """A password-database entry of a made-up user, so no test depends on the users of the machine it runs on."""
    return pwd.struct_passwd((name, 'x', uid, uid, '', f'/home/{name}', '/bin/sh'))


def red_check(case, test_file, mutations):
    """mutations: {rule: (module, text, broken text, 'Class.test_name')}."""
    for rule, (module, text, broken, test) in mutations.items():
        with case.subTest(rule=rule):
            tmp = tempfile.mkdtemp(prefix='update-policy-red-')
            try:
                lib = os.path.join(tmp, 'lib')
                shutil.copytree(os.path.join(KIT, 'lib'), lib, ignore=shutil.ignore_patterns('__pycache__'))
                path = os.path.join(lib, 'updatepolicy', f'{module}.py')
                with open(path, encoding='utf-8') as f:
                    source = f.read()
                case.assertEqual(source.count(text), 1, f'{rule}: the text is not in {module}.py exactly once')
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(source.replace(text, broken))
                r = subprocess.run([sys.executable, '-I', '-B', test_file, test], capture_output=True, text=True,
                                   timeout=300, env=dict(os.environ, **{LIB_ENV: lib}), stdin=subprocess.DEVNULL)
                case.assertIn('Ran 1 test', r.stderr, f'{rule}: {test} did not run:\n{r.stderr[-800:]}')
                case.assertNotEqual(r.returncode, 0, f'{test} still passes with {rule!r} broken')
            finally:
                shutil.rmtree(tmp, ignore_errors=True)


def dumps(x):
    return json.dumps(x)
