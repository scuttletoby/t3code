"""A fake WSL2 distro in a temporary folder for the pc-agents tests: every fixed path under PC_AGENTS_ROOT, the
current user as root, fake useradd, usermod, env (T3's installer), apt-get, dpkg-query, dpkg, systemctl, systemd-run
and nft (each logs to /log in the fake root; systemctl keeps which units are enabled, and the ExecStart and policy a
started one runs with, its main PID and its start time (CLOCK_MONOTONIC, as systemd's) in /log/units). No root, no
systemd, no network, no real /etc. Fresh as `wsl --install` leaves one, from the fleet's wsl2 walls fixture."""
import contextlib, importlib.machinery, importlib.util, io, json, os, shutil, stat, sys, tempfile, unittest
from unittest import mock

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(KIT))
WSL2 = os.path.join(REPO, '.github', 'scripts', 'fork-fleet-walls', 'wsl2.fixture.json')
SHA1, SHA2 = 'a' * 40, 'b' * 40
CONFIG = {'organization': 'Example lab', 't3Version': '1.2.3', 't3ReleaseBaseUrl': 'https://example.com/releases',
          'port': 3800, 'maxRuntimeMode': 'full-access', 'lockedSettings': {'agentSlots.maxPerMachine': 2},
          'memoryMax': '8G', 'cpuQuota': '400%', 'nameservers': ['1.1.1.1', '9.9.9.9'], 'peers': ['100.101.102.103/32'],
          'tailscaleTag': 'tag:pc-agents'}
TOOLS = {  # fake programs, under the fake root; each finds the root from its own path
    'usr/sbin/useradd': 'R="${0%/usr/sbin/useradd}"; uid=; sh=/bin/sh; prev=; for a; do [ "$prev" = --uid ] && uid=$a; '
                        '[ "$prev" = --shell ] && sh=$a; prev=$a; done\n'
                        'echo "$a:x:$uid:$uid::/home/$a:$sh" >>"$R/etc/passwd"\n'
                        'echo "$a:x:$uid:" >>"$R/etc/group"\nmkdir -p "$R/home/$a"\n',
    'usr/sbin/usermod': 'R="${0%/usr/sbin/usermod}"; echo "$*" >>"$R/log/usermod"\n'
                        '[ "$1" = --shell ] && sed -i "s#^$3:\\(\\([^:]*:\\)\\{5\\}\\)[^:]*\\$#$3:\\1$2#" "$R/etc/passwd"\n',
    # T3's installer, run by root through env -i (scripts/install.sh with T3CODE_HOME=/opt/pc-agents/t3)
    'usr/bin/env': 'R="${0%/usr/bin/env}"; echo "$*" >>"$R/log/install"\n[ -e "$R/log/install-fail" ] && exit 1\n'
                   # install-hang: a download that never ends, its pid noted (the group kill must end it)
                   '[ -e "$R/log/install-hang" ] && { sleep 60 & echo $! >"$R/log/hang.pid"; wait; }\n'
                   'for a; do case "$a" in T3CODE_VERSION=*) v=${a#*=};; esac; done\n'
                   'd="$R/opt/pc-agents/t3/runtime/versions/$v"; mkdir -p "$d"; printf "#!/bin/sh\\n# t3 $v\\n" >"$d/t3"\n'
                   'chmod 755 "$d/t3"\n'
                   # install-loose: as upstream's tar leaves it (a mktemp -d top, the archive's group-writable modes)
                   '[ -e "$R/log/install-loose" ] && { mkdir -p "$d/lib/x"; echo x >"$d/lib/x/m.js"; chmod 775 "$d/lib" '
                   '"$d/lib/x"; chmod 664 "$d/lib/x/m.js"; ln -s x "$d/lib/y"; echo s >"$d/lib/s"; chmod 4775 "$d/lib/s"; '
                   'chmod 700 "$d"; }\n'
                   # install-link-out: a link from the runtime to a file outside it
                   '[ -e "$R/log/install-link-out" ] && { echo o >"$R/log/outside"; chmod 664 "$R/log/outside"; '
                   'ln -s "$R/log/outside" "$d/out"; }; exit 0\n',
    # systemd-run: like the real one, a bare command name is looked up in the CALLER's PATH, before anything runs
    'usr/bin/systemd-run': 'R="${0%/usr/bin/systemd-run}"; printf "%s\\n" "$@" >"$R/log/systemd-run"\n'
                           'while [ $# -gt 0 ] && [ "$1" != -- ]; do shift; done; cmd=$2\n'
                           'case "$cmd" in */*) [ -x "$R$cmd" ] || [ -x "$cmd" ];; *) command -v "$cmd" >/dev/null;; esac'
                           ' || { echo "Failed to find executable $cmd: No such file or directory" >&2; exit 1; }\n',
    # journalctl: prints what the test put in /log/journal (none: an empty journal)
    'usr/bin/journalctl': 'R="${0%/usr/bin/journalctl}"; echo "$*" >>"$R/log/journalctl"; cat "$R/log/journal" 2>/dev/null; :\n',
    'usr/bin/systemctl': 'R="${0%/usr/bin/systemctl}"; echo "$*" >>"$R/log/systemctl"; S="$R/log/units"; mkdir -p "$S"\n'
                         '[ -e "$R/log/systemctl-fail" ] && { echo "systemctl: failed (test)" >&2; exit 1; }\n'
                         # what a started unit runs with: its ExecStart and T3's policy
                         # a start: refused while the start limit is hit ($S/U.limit, until reset-failed); with
                         # /log/t3-crash T3 exits at once, with /log/t3-dies after the first look (show) at it
                         'run() { [ -e "$S/$1.limit" ] && { echo "$1: start request repeated too quickly" >&2; '
                         'return 1; }\nrm -f "$S/$1.seen" "$S/$1.status" "$S/$1.code"\n'
                         # /log/t3-crash: T3's exit status (empty: 1), or "CODE STATUS" (2 9: killed by signal 9)
                         '[ "$1" = t3code-agents.service ] && [ -e "$R/log/t3-crash" ] && { rm -f "$S/$1.active"; '
                         'c=$(cat "$R/log/t3-crash"); case "$c" in *" "*) echo "${c%% *}" >"$S/$1.code"; c=${c#* };; '
                         '*) echo 1 >"$S/$1.code";; esac; echo "${c:-1}" >"$S/$1.status"; return; }\n'
                         '{ grep "^ExecStart=" "$R/etc/systemd/system/$1"; '
                         'cat "$R/etc/t3code/policy.json"; } >"$S/$1.active" 2>/dev/null || :\n'
                         'echo $$ >"$S/$1.pid"; python3 -I -c "import time; print(time.monotonic_ns() // 1000)" '
                         '>"$S/$1.start"; }\n'
                         'case "$1" in\n'
                         'enable|disable) v=$1; shift; now=; [ "$1" = --now ] && { now=1; shift; }\n'
                         '  for u; do if [ $v = enable ]; then : >"$S/$u.enabled"; [ -n "$now" ] && '
                         '{ [ -e "$S/$u.active" ] || run "$u" || exit 1; }\n'
                         '  else rm -f "$S/$u.enabled"; [ -z "$now" ] || rm -f "$S/$u.active"; fi; done;;\n'
                         'restart) shift; for u; do run "$u" || exit 1; done;;\n'
                         'reset-failed) shift; for u; do rm -f "$S/$u.limit"; done;;\n'
                         'stop) shift; for u; do rm -f "$S/$u.active"; done;;\n'
                         'is-active) [ -e "$S/$2.active" ] && echo active || { echo inactive; exit 3; };;\n'
                         'show) u=$4; f=; [ -e "$R/etc/systemd/system/$u" ] && f=/etc/systemd/system/$u\n'
                         '  [ -e "$R/log/t3-dies" ] && [ -e "$S/$u.active" ] && { [ -e "$S/$u.seen" ] && '
                         'rm -f "$S/$u.active"; : >"$S/$u.seen"; }\n'
                         '  st=disabled; [ -e "$S/$u.enabled" ] && st=enabled\n'
                         '  [ "$u" = pc-agents-walls.service ] && st=static\n'
                         '  echo "LoadState=${f:+loaded}"; echo "FragmentPath=$f"\n'
                         '  echo "DropInPaths=$(cat "$S/$u.dropins" 2>/dev/null)"; echo "UnitFileState=$st"\n'
                         '  [ -e "$S/reload" ] && echo NeedDaemonReload=yes || echo NeedDaemonReload=no\n'
                         '  if [ -e "$S/$u.active" ]; then echo ActiveState=active; echo "MainPID=$(cat "$S/$u.pid")"\n'
                         '  else echo ActiveState=inactive; echo MainPID=0; fi; echo NRestarts=0\n'
                         '  [ -e "$S/$u.status" ] && { echo "ExecMainCode=$(cat "$S/$u.code")"; echo "ExecMainStatus=$(cat "$S/$u.status")"; }\n'
                         '  echo "ExecMainStartTimestampMonotonic=$(cat "$S/$u.start" 2>/dev/null || echo 0)";;\n'
                         'esac\n',
    # apt-get [-o OPTION]... update|install -y PKG...: logs the rest (and the options, in /log/apt-get-options);
    # installs each package in /log/packages (nftables brings the fake nft kept in /log/nft-package). As real apt-get:
    # refuses while a package is unpacked (dpkg was interrupted). /log/apt-locked-update (or -install) holds how many
    # more runs find the lists (or archives) lock held, as while apt-daily runs: real apt-get does not wait for those.
    # /log/apt-fail makes it fail; /log/dns holds the resolv.conf line `update` needs to reach the mirror; with
    # /log/questions it fails unless noninteractive and keeping changed config files.
    'usr/bin/apt-get': 'R="${0%/usr/bin/apt-get}"; o=; while [ "$1" = -o ]; do o="$o $2"; shift 2; done\n'
                       'echo "$*" >>"$R/log/apt-get"; echo "${o# }" >>"$R/log/apt-get-options"\n'
                       'n=$(cat "$R/log/apt-locked-$1" 2>/dev/null || echo 0); if [ "$n" -gt 0 ]; then\n'
                       '  echo $((n - 1)) >"$R/log/apt-locked-$1"; [ "$1" = update ] && d=/var/lib/apt/lists || '
                       'd=/var/cache/apt/archives\n'
                       '  echo "E: Could not get lock $d/lock. It is held by process 4242 (apt-get) (test)" >&2\n'
                       '  echo "E: Unable to lock directory $d/" >&2; exit 100; fi\n'
                       '[ -e "$R/log/apt-fail" ] && { echo "E: no network (test)" >&2; exit 100; }\n'
                       'grep -qs unpacked "$R"/log/packages/* && { echo "E: dpkg was interrupted, you must manually '
                       'run \'dpkg --configure -a\' to correct the problem." >&2; exit 100; }\n'
                       '[ ! -e "$R/log/questions" ] || { [ "$DEBIAN_FRONTEND" = noninteractive ] && case "$o" in '
                       '*Dpkg::Options::=--force-confold*) :;; *) false;; esac; } || '
                       '{ echo "E: end of file on stdin at conffile prompt (test)" >&2; exit 100; }\n'
                       '[ "$1" != update ] || [ ! -e "$R/log/dns" ] || grep -qxF "$(cat "$R/log/dns")" "$R/etc/resolv.conf" '
                       '|| { echo "E: Temporary failure resolving the mirror (test)" >&2; exit 100; }\n'
                       '[ "$1" = install ] || exit 0; shift 2\n'
                       'for p; do echo "install ok installed" >"$R/log/packages/$p"\n'
                       '  [ "$p" != nftables ] || cp "$R/log/nft-package" "$R/usr/sbin/nft"; done\n',
    # dpkg-query -W -f=FORMAT PKG...: "PKG<TAB>its status" for each package in /log/packages (the status inside)
    'usr/bin/dpkg-query': 'R="${0%/usr/bin/dpkg-query}"; echo "$*" >>"$R/log/dpkg-query"; shift 2; rc=0\n'
                          'for p; do if [ -e "$R/log/packages/$p" ]; then printf "%s\\t%s\\n" "$p" "$(cat "$R/log/packages/$p")"\n'
                          '  else echo "dpkg-query: no packages found matching $p" >&2; rc=1; fi; done; exit $rc\n',
    # dpkg --audit: names each package an apt-get stopped half-way left unpacked (no lock: it only reads).
    # dpkg [--force-...]... --configure -a: finishes them. /log/dpkg-locked holds how many more runs find the lock
    # held (real dpkg does not wait); /log/questions as for apt-get.
    'usr/bin/dpkg': 'R="${0%/usr/bin/dpkg}"; echo "$*" >>"$R/log/dpkg"\n'
                    'if [ "$1" = --audit ]; then rc=0; for f in "$R"/log/packages/*; do grep -qs unpacked "$f" || '
                    'continue\n'
                    '  [ $rc = 1 ] || echo "The following packages have been unpacked but not yet configured:"\n'
                    '  echo " ${f##*/}"; rc=1; done; exit $rc; fi\n'
                    'n=$(cat "$R/log/dpkg-locked" 2>/dev/null || echo 0); if [ "$n" -gt 0 ]; then\n'
                    '  echo $((n - 1)) >"$R/log/dpkg-locked"\n'
                    '  echo "dpkg: error: dpkg frontend lock was locked by another process with pid 4242 (test)" >&2\n'
                    '  exit 2; fi\n'

                    '[ ! -e "$R/log/questions" ] || { [ "$DEBIAN_FRONTEND" = noninteractive ] && case "$*" in '
                    '*--force-confold*) :;; *) false;; esac; } || '
                    '{ echo "dpkg: error: end of file on stdin at conffile prompt (test)" >&2; exit 1; }\n'
                    'for f in "$R"/log/packages/*; do [ ! -e "$f" ] || sed -i "s/unpacked/installed/" "$f"; done\n',
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
    # How long setup watches T3 after a start here (15 s on a machine). 0: one look, for a test about the writes
    # and not the watch (no_watch()).
    settle = 0.3

    def setUp(self):
        k.SETTLE, k.SETTLE_STEP = self.settle, 0.05
        k.LOCK_WAIT, k.LOCK_STEP = 0.3, 0.01   # 300 s and 5 s on a machine
        # No power cut in a fake root, so when a write reaches the disk is not tested here: fsync only costs time.
        fsync = mock.patch.object(os, 'fsync', lambda fd: None)
        fsync.start()
        self.addCleanup(fsync.stop)
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
        files['proc/sys/kernel/random/boot_id'] = '0f8e4f9c-1b6a-4d2e-9a57-3c1d2e4f5a6b\n'
        files['proc/net/route'] = 'Iface\tDestination\tGateway\tFlags\nboth\t00000000\t011014AC\t0003\n'
        files['log/'] = files['opt/'] = files['etc/systemd/system/'] = ''   # as in a fresh Ubuntu
        files['log/packages/nftables'] = 'install ok installed'   # and no libatomic1 or unzip, as in a fresh Ubuntu
        files.update({rel: {'text': '#!/bin/sh\n' + text, 'mode': '755'} for rel, text in TOOLS.items()})
        for rel, value in files.items():
            self.write('/' + rel, value)
        os.symlink('/mnt/wsl/resolv.conf', self.path('/etc/resolv.conf'))
        self.machine(CONFIG)

    def close(self):
        os.umask(self.umask)
        shutil.rmtree(self.root, ignore_errors=True)

    def no_watch(self):
        """Setup looks at T3 once after a start, for this test and every setUp() it calls: a test that runs setup
        hundreds of times for its writes, not its watch (the watch has tests of its own)."""
        self.settle = k.SETTLE = 0

    def keep(self):
        """A copy of the fake root as it is now, for restore(): the same machine again, without the setup runs that
        made it."""
        copy = tempfile.mkdtemp(prefix='pc-agents-kept-')
        self.addCleanup(shutil.rmtree, copy, ignore_errors=True)
        shutil.copytree(self.root, copy, symlinks=True, dirs_exist_ok=True)
        return copy

    def restore(self, copy):
        """The fake root as keep() copied it, at the same path (the fake tools find the root from their own path)."""
        shutil.rmtree(self.root)
        shutil.copytree(copy, self.root, symlinks=True)

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

    def files(self):
        """The kit's files from machine.json, T3's runtime and the record of both, as setup writes them."""
        c = k.config()
        files = k.render(c)
        for p, (text, mode) in files.items():
            self.write(p, {'text': text, 'mode': f'{mode:o}'})
        t3 = k.runtime(c) + '/t3'
        self.write(t3, {'text': '#!/bin/sh\n', 'mode': '755'})
        self.write(k.RECORD, ''.join(f'{k.sha(t)}\t{p}\n' for p, (t, _) in sorted(files.items())) +
                   f'{k.file_sha(t3)}\t{t3}\n')
        return c

    def walled(self, sha=SHA1):
        """What `pc-agents setup` leaves after `wsl --terminate` and a second run, written here directly."""
        c = self.files()
        for p in k.masks(c):
            os.makedirs(os.path.dirname(self.path(p)), exist_ok=True)
            os.symlink('/dev/null', self.path(p))
        with open(self.path('/etc/passwd'), 'a') as f:
            f.write('agent:x:1100:1100::/home/agent:/usr/sbin/nologin\n')
        with open(self.path('/etc/group'), 'a') as f:
            f.write('agent:x:1100:\n')
        for src, (dst, mode) in k.CODE.items():
            with open(os.path.join(REPO, src), encoding='utf-8') as f:
                self.write(f'/opt/pc-agents/{sha}/{dst}', {'text': f.read(), 'mode': f'{mode:o}'})
        os.symlink(sha, self.path('/opt/pc-agents/current'))
        self.assertIsNone(k.load_firewall())
        k.run(['/usr/bin/systemctl', 'enable', '--now', k.UNIT, k.WALLS + '.timer'])
        os.unlink(self.path('/log/systemctl'))
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


