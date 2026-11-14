"""Test helpers for the gh-apps kit: a fake host in a temporary folder (every fixed path lives under PREFIX), with
the current user standing in for root, ghrun and ghkeys. Nothing here needs root, sudo or the network."""
import contextlib, json, os, shutil, subprocess, sys, tempfile

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KIT, 'lib'))

from ghapps import common as c  # noqa: E402

TOKEN = 'ghs_' + 'T0kEn' * 7 + 'x'          # what the fake gh-mint hands out
AGENT_IDS = (os.getuid() + 54321, os.getgid() + 54321)
SAVED = {}


def patch(module, **attrs):
    for k, v in attrs.items():
        SAVED.setdefault((module, k), getattr(module, k))
        setattr(module, k, v)


def restore():
    for (module, k), v in SAVED.items():
        setattr(module, k, v)
    SAVED.clear()


class FakeHost:
    """A fake host under a temporary folder. setup(installed=True) lays out what the installer makes, by hand."""

    def __init__(self):
        self.umask = os.umask(0o022)   # a shared-group umask (002) would make the fake root's folders untrusted
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='ghapps-test-'))
        uid, gid = os.getuid(), os.getgid()
        self.ids = {'root': (uid, gid), c.RUN_USER: (uid, gid), c.KEYS_USER: (uid, gid), 'agent': AGENT_IDS}
        patch(c, PREFIX=self.root, ROOT_UID=uid, FAKE_IDS=self.ids)
        for d in ('etc/sudoers.d', 'usr/bin', 'usr/local/bin', 'usr/local/sbin', 'var/lib', 'var/cache', 'home/agent'):
            os.makedirs(self.path('/' + d), mode=0o755, exist_ok=True)
        with open(self.path('/etc/sudoers'), 'w') as f:
            f.write('Defaults env_reset\n@includedir /etc/sudoers.d\n')
        self.gh_marker = self.path('/gh-ran')
        self.write_gh()

    def path(self, p):
        return self.root + p

    def write_gh(self, body=None):
        """A fake gh (root-owned in the fake host): prints its args, cwd, environment and stdin as JSON."""
        body = body or ('import json, os, sys\n'
                        f'open({self.gh_marker!r}, "a").write("ran\\n")\n'
                        'data = sys.stdin.read()\n'
                        'print(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ), '
                        '"stdin": data}))\n'
                        'print("debug: token is " + os.environ.get("GH_TOKEN", ""), file=sys.stderr)\n')
        gh = self.path('/usr/bin/gh')
        with open(gh, 'w') as f:
            f.write(f'#!{sys.executable} -I\n{body}')
        os.chmod(gh, 0o755)
        return gh

    def lay_out(self):
        """The folders, config and one app as the installer and gh-apps-admin would leave them."""
        for path, mode in ((c.ETC, 0o755), (c.APPS, 0o755), (c.KEYS, 0o750), (c.VARLIB, 0o755), (c.RUN, 0o700),
                           (c.EMPTY, 0o555), (c.CACHE, 0o700), (c.LIBEXEC, 0o755)):
            os.makedirs(self.path(path), exist_ok=True)
            os.chmod(self.path(path), mode)
        self.write_json(c.CONFIG, {'version': 1, 'gh_path': '/usr/bin/gh', 'agent_users': ['agent']})

    def write_json(self, path, data, mode=0o644):
        with open(self.path(path), 'w') as f:
            json.dump(data, f)
        os.chmod(self.path(path), mode)

    def add_app(self, name='demo', repo='octo/demo', app_id=11, installation_id=22, key=None):
        self.write_json(f'{c.APPS}/{name}.json', {'app_id': app_id, 'installation_id': installation_id,
                                                  'repo': repo})
        if key is not None:
            dst = self.path(f'{c.KEYS}/{name}.pem')
            shutil.copyfile(key, dst)
            os.chmod(dst, 0o640)

    def close(self):
        restore()
        os.umask(self.umask)
        shutil.rmtree(self.root, ignore_errors=True)


def make_key(folder):
    """A throw-away RSA key (openssl), and its public half."""
    key, pub = os.path.join(folder, 'k.pem'), os.path.join(folder, 'k.pub')
    subprocess.run(['/usr/bin/openssl', 'genrsa', '-out', key, '2048'], check=True, capture_output=True)
    subprocess.run(['/usr/bin/openssl', 'rsa', '-in', key, '-pubout', '-out', pub], check=True, capture_output=True)
    return key, pub


def drive(host, module, args, *, stdin=b'', token=TOKEN, extra=None, popen=False, timeout=60):
    """Run module.main(args) in a fresh Python, with the fake host's settings (tests/kit_driver.py)."""
    spec = {'prefix': c.PREFIX, 'root_uid': c.ROOT_UID, 'ids': c.FAKE_IDS, 'module': module, 'args': args,
            'token': token, 'extra': extra or {}}
    cmd = [sys.executable, '-I', '-B', os.path.join(KIT, 'tests', 'kit_driver.py'), json.dumps(spec)]
    env = {'PATH': os.environ.get('PATH', c.SAFE_PATH), 'LANG': 'C.UTF-8',
           'LD_PRELOAD': '', 'GH_DEBUG': 'api', 'GH_HOST': 'evil.example', 'GIT_DIR': '/nonexistent',
           'PYTHONPATH': '/nonexistent', 'GH_TOKEN': 'ghp_' + 'agentsown' * 4}
    if popen:
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    return subprocess.run(cmd, input=stdin, capture_output=True, env=env, timeout=timeout)


def read(path):
    with open(path) as f:
        return f.read()


@contextlib.contextmanager
def chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)
