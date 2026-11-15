"""A fake WSL2 distro in a temporary folder for the pc-agents tests: every fixed path under PC_AGENTS_ROOT, the
current user as root, fake useradd, runuser, systemctl and nft (each logs to /log in the fake root). No root, no
systemd, no network, no real /etc. Fresh as `wsl --install` leaves one, from the fleet's wsl2 walls fixture."""
import contextlib, importlib.machinery, importlib.util, io, json, os, shutil, stat, sys, tempfile, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(KIT))
WSL2 = os.path.join(REPO, '.github', 'scripts', 'fork-fleet-walls', 'wsl2.fixture.json')
SHA1, SHA2 = 'a' * 40, 'b' * 40
CONFIG = {'organization': 'Example lab', 't3Version': '1.2.3', 't3ReleaseBaseUrl': 'https://example.com/releases',
          'port': 3800, 'maxRuntimeMode': 'full-access', 'lockedSettings': {'agentSlots.maxPerMachine': 2},
          'memoryMax': '8G', 'cpuQuota': '400%', 'nameservers': ['1.1.1.1', '9.9.9.9'], 'tailscaleTag': 'tag:pc-agents'}
TOOLS = {  # fake programs, under the fake root; each finds the root from its own path
    'usr/sbin/useradd': 'R="${0%/usr/sbin/useradd}"; uid=; prev=; for a; do [ "$prev" = --uid ] && uid=$a; prev=$a; '
                        'done\necho "$a:x:$uid:$uid::/home/$a:/bin/bash" >>"$R/etc/passwd"\n'
                        'echo "$a:x:$uid:" >>"$R/etc/group"\nmkdir -p "$R/home/$a"\n',
    'usr/sbin/runuser': 'R="${0%/usr/sbin/runuser}"; echo "$*" >>"$R/log/runuser"\n[ -e "$R/log/runuser-fail" ] && exit 1\n'
                        'for a; do case "$a" in T3CODE_VERSION=*) v=${a#*=};; esac; done\n'
                        'd="$R/home/agent/.t3/runtime/versions/$v"; mkdir -p "$d"; printf "#!/bin/sh\\n" >"$d/t3"\n',
    'usr/bin/systemctl': 'R="${0%/usr/bin/systemctl}"; echo "$*" >>"$R/log/systemctl"\n',
    'usr/sbin/nft': 'R="${0%/usr/sbin/nft}"\ncase "$1" in -f) cp "$R$2" "$R/log/nft-loaded";;\n'
                    'list) cat "$R/log/nft-loaded" 2>/dev/null || exit 1;; esac\n',
}
DRVFS = '43 29 0:43 / /mnt/c rw,noatime - 9p drvfs rw\n44 29 0:44 / /c rw,noatime - 9p drvfs rw\n'


def blob(path):
    with open(path, 'rb') as f:
        return f.read()


def load():
    os.environ['PC_AGENTS_ROOT'] = '/nonexistent'
    loader = importlib.machinery.SourceFileLoader('pc_agents_cli', os.path.join(KIT, 'sbin', 'pc-agents'))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader('pc_agents_cli', loader))
    loader.exec_module(mod)
    return mod


cli = load()
k = cli.k


class Case(unittest.TestCase):
    def setUp(self):
        self.umask = os.umask(0o022)
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='pc-agents-test-'))
        os.chmod(self.root, 0o755)
        self.addCleanup(self.close)
        k.ROOT, k.OWNER = self.root, os.geteuid()
        with open(WSL2, encoding='utf-8') as f:
            files = json.load(f)['pass']
        for rel in ('etc/wsl.conf', 'etc/resolv.conf', 'etc/binfmt.d/WSLInterop.conf'):
            del files[rel]   # a fresh distro: the kit writes these
        files['etc/wsl.conf'] = '[boot]\nsystemd=true\n'
        files['etc/passwd'] = 'root:x:0:0:root:/root:/bin/bash\nnobody:x:65534:65534::/nonexistent:/usr/sbin/nologin\n'
        files['etc/group'] = 'root:x:0:\nsudo:x:27:\nnogroup:x:65534:\n'
        files['proc/net/route'] = 'Iface\tDestination\tGateway\tFlags\nboth\t00000000\t011014AC\t0003\n'
        files['log/'] = ''
        files.update({rel: {'text': '#!/bin/sh\n' + text, 'mode': '755'} for rel, text in TOOLS.items()})
        for rel, value in files.items():
            self.write('/' + rel, value)
        os.symlink('/mnt/wsl/resolv.conf', self.path('/etc/resolv.conf'))
        self.machine(CONFIG)

    def close(self):
        os.umask(self.umask)
        shutil.rmtree(self.root, ignore_errors=True)

    def path(self, p):
        return self.root + p

    def write(self, p, value):
        full = self.path(p)
        if os.path.islink(full):
            os.unlink(full)
        if p.endswith('/'):
            return os.makedirs(full, exist_ok=True)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        text, mode = (value['text'], int(value['mode'], 8)) if isinstance(value, dict) else (value, 0o644)
        with open(full, 'w', encoding='utf-8') as f:
            f.write(text)
        os.chmod(full, mode)

    def read(self, p):
        with open(self.path(p), encoding='utf-8') as f:
            return f.read()

    def machine(self, section):
        self.write('/etc/fork-kit/machine.json', json.dumps({'version': 1, 'pc-agents': section}))

    def terminate(self):
        """What `wsl --terminate` does once wsl.conf turned automount off: Windows' drives are no longer mounted."""
        self.write('/proc/self/mountinfo', self.read('/proc/self/mountinfo').replace(DRVFS, ''))

    def call(self, fn, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = fn(*argv)
        return code, out.getvalue()

    def setup_run(self, sha=SHA1):
        return self.call(cli.main, ['setup', '--source', REPO, '--sha', sha])

    def check(self, *flags):
        return self.call(cli.main, ['check', *flags])

    def log(self, name='systemctl'):
        try:
            return self.read('/log/' + name).splitlines()
        except FileNotFoundError:
            return []

    def ready(self):
        """A distro set up all the way: first run, `wsl --terminate`, second run."""
        code, out = self.setup_run()
        self.assertEqual(code, 3, out)
        self.terminate()
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)

    def walled(self, sha=SHA1):
        """What `pc-agents setup` leaves after `wsl --terminate` and a second run, written here directly."""
        files = k.render(k.config())
        for p, (text, mode) in files.items():
            self.write(p, {'text': text, 'mode': f'{mode:o}'})
        self.write(k.RECORD, ''.join(f'{k.sha(t)}\t{p}\n' for p, (t, _) in sorted(files.items())))
        for p in k.MASKS:
            os.makedirs(os.path.dirname(self.path(p)), exist_ok=True)
            os.symlink('/dev/null', self.path(p))
        with open(self.path('/etc/passwd'), 'a') as f:
            f.write('agent:x:1100:1100::/home/agent:/bin/bash\n')
        with open(self.path('/etc/group'), 'a') as f:
            f.write('agent:x:1100:\n')
        for src, (dst, mode) in k.CODE.items():
            with open(os.path.join(REPO, src), encoding='utf-8') as f:
                self.write(f'/opt/pc-agents/{sha}/{dst}', {'text': f.read(), 'mode': f'{mode:o}'})
        os.symlink(sha, self.path('/opt/pc-agents/current'))
        shutil.copy(self.path(k.STATE + '/firewall.nft'), self.path('/log/nft-loaded'))
        self.terminate()

    def snapshot(self):
        """Every path under the fake root but the fake tools' logs: kind, mode, owner, mtime, inode, link, content."""
        out = {}
        for top, dirs, names in os.walk(self.root):
            for n in dirs + names:
                full = os.path.join(top, n)
                rel = full[len(self.root):]
                if rel.startswith('/log'):
                    continue
                st = os.lstat(full)
                body = os.readlink(full) if stat.S_ISLNK(st.st_mode) else \
                    blob(full) if stat.S_ISREG(st.st_mode) else None
                out[rel] = (st.st_mode, st.st_uid, st.st_mtime_ns, st.st_ino, body)
        return out


