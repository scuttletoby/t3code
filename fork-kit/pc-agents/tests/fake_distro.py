"""A fake WSL2 distro in a temporary folder for the pc-agents tests: every fixed path under PC_AGENTS_ROOT, the
current user as root, fake useradd, usermod, apt-get, dpkg-query, dpkg, systemctl, systemd-run, nft and curl
(fake_curl.py) (each logs to /log in the fake root; systemctl keeps which units are enabled, and the ExecStart and
policy a started one runs with, its main PID and its start time (CLOCK_MONOTONIC, as systemd's) in /log/units, and a
running unit's processes in its control group, /sys/fs/cgroup/system.slice/UNIT/cgroup.procs: its main PID, and
those a test adds); the real openssl. No root, no systemd, no network, no real /etc. Fresh as `wsl --install` leaves one, from the fleet's wsl2 walls fixture;
or, for a Case with platform = 'linux', a plain Linux box (the fleet's linux walls fixture) where the owner already
has a login user and the system resolver's stub."""
import contextlib, importlib.machinery, importlib.util, io, json, os, shutil, stat, sys, tempfile, unittest
from unittest import mock

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(KIT))
WSL2 = os.path.join(REPO, '.github', 'scripts', 'fork-fleet-walls', 'wsl2.fixture.json')
LINUX = os.path.join(REPO, '.github', 'scripts', 'fork-fleet-walls', 'linux.fixture.json')
SHA1, SHA2 = 'a' * 40, 'b' * 40
CONFIG = {'organization': 'Example lab', 't3Version': '1.2.3', 't3ReleaseBaseUrl': 'https://example.com/releases',
          'port': 3800, 'maxRuntimeMode': 'full-access', 'lockedSettings': {'agentSlots.maxPerMachine': 2},
          'memoryMax': '8G', 'cpuQuota': '400%', 'nameservers': ['1.1.1.1', '9.9.9.9'], 'peers': ['100.101.102.103/32'],
          'tailscaleTag': 'tag:pc-agents'}
LINUX_CONFIG = {**CONFIG, 'platform': 'linux'}
TOOLS = {  # fake programs, under the fake root; each finds the root from its own path
    'usr/sbin/useradd': 'R="${0%/usr/sbin/useradd}"; uid=; sh=/bin/sh; prev=; for a; do [ "$prev" = --uid ] && uid=$a; '
                        '[ "$prev" = --shell ] && sh=$a; prev=$a; done\n'
                        'echo "$a:x:$uid:$uid::/home/$a:$sh" >>"$R/etc/passwd"\n'
                        'echo "$a:x:$uid:" >>"$R/etc/group"\nmkdir -p "$R/home/$a"\n',
    'usr/sbin/usermod': 'R="${0%/usr/sbin/usermod}"; echo "$*" >>"$R/log/usermod"\n'
                        '[ "$1" = --shell ] && sed -i "s#^$3:\\(\\([^:]*:\\)\\{5\\}\\)[^:]*\\$#$3:\\1$2#" "$R/etc/passwd"\n',
    # systemd-run: like the real one, a bare command name is looked up in the CALLER's PATH, before anything runs
    'usr/bin/systemd-run': 'R="${0%/usr/bin/systemd-run}"; printf "%s\\n" "$@" >"$R/log/systemd-run"\n'
                           'while [ $# -gt 0 ] && [ "$1" != -- ]; do shift; done; cmd=$2\n'
                           'case "$cmd" in */*) [ -x "$R$cmd" ] || [ -x "$cmd" ];; *) command -v "$cmd" >/dev/null;; esac'
                           ' || { echo "Failed to find executable $cmd: No such file or directory" >&2; exit 1; }\n',
    # journalctl: prints what the test put in /log/journal (none: an empty journal); with _SYSTEMD_INVOCATION_ID=ID,
    # that run's own lines (/log/journal.ID), with INVOCATION_ID=ID systemd's entries about the run
    # (/log/journal.ID.start). A started T3 (systemctl's run below) writes both: its start, and T3 saying it is ready.
    # Of the version in /log/t3-not-ready T3 says only "Listening"; of the version in /log/t3-silent nothing yet.
    # /log/journal-none: the journal keeps nothing of a run; /log/journal-rotated: only a later line of T3's, the
    # run's start and its ready line rotated away; /log/journal-fail: journalctl fails.
    'usr/bin/journalctl': 'R="${0%/usr/bin/journalctl}"; echo "$*" >>"$R/log/journalctl"\n'
                          '[ -e "$R/log/journal-fail" ] && { echo "Failed to open journal (test)" >&2; exit 1; }\n'
                          'case "$1" in _SYSTEMD_INVOCATION_ID=*) cat "$R/log/journal.${1#*=}";; '
                          'INVOCATION_ID=*) cat "$R/log/journal.${1#*=}.start";; *) cat "$R/log/journal";; '
                          'esac 2>/dev/null; :\n',
    'usr/bin/systemctl': 'R="${0%/usr/bin/systemctl}"; echo "$*" >>"$R/log/systemctl"; S="$R/log/units"; mkdir -p "$S"\n'
                         '[ -e "$R/log/systemctl-fail" ] && { echo "systemctl: failed (test)" >&2; exit 1; }\n'
                         # what a started unit runs with: its ExecStart and T3's policy
                         # a start: refused while the start limit is hit ($S/U.limit, until reset-failed); with
                         # /log/t3-crash T3 exits at once, with /log/t3-dies after the first look (show) at it
                         'run() { [ -e "$S/$1.limit" ] && { echo "$1: start request repeated too quickly" >&2; '
                         'return 1; }\nrm -f "$S/$1.seen" "$S/$1.status" "$S/$1.code"\n'
                         # /log/t3-crash: T3's exit status (empty: 1), or "CODE STATUS" (2 9: killed by signal 9)
                         # /log/t3-crash-version: only T3 of that version crashes so (exit status 1)
                         '[ "$1" = t3code-agents.service ] && [ -e "$R/log/t3-crash-version" ] && grep -qF '
                         '"/versions/$(cat "$R/log/t3-crash-version")/" "$R/etc/systemd/system/$1" && { '
                         'rm -f "$S/$1.active"; echo 1 >"$S/$1.code"; echo 1 >"$S/$1.status"; return; }\n'
                         '[ "$1" = t3code-agents.service ] && [ -e "$R/log/t3-crash" ] && { rm -f "$S/$1.active"; '
                         'c=$(cat "$R/log/t3-crash"); case "$c" in *" "*) echo "${c%% *}" >"$S/$1.code"; c=${c#* };; '
                         '*) echo 1 >"$S/$1.code";; esac; echo "${c:-1}" >"$S/$1.status"; return; }\n'
                         '{ grep "^ExecStart=" "$R/etc/systemd/system/$1"; '
                         'cat "$R/etc/t3code/policy.json"; } >"$S/$1.active" 2>/dev/null || :\n'
                         'i=$(printf %032x $$); echo $i >"$S/$1.invocation"\n'
                         'if [ "$1" = t3code-agents.service ] && [ ! -e "$R/log/journal-none" ]; then\n'
                         '  of() { [ -e "$R/log/$1" ] && grep -qF "/versions/$(cat "$R/log/$1")/" '
                         '"$R/etc/systemd/system/t3code-agents.service"; }\n'
                         '  if [ -e "$R/log/journal-rotated" ]; then echo "a later line" >"$R/log/journal.$i"\n'
                         '  else echo "Started t3code-agents.service" >"$R/log/journal.$i.start"\n'
                         '    if of t3-not-ready; then echo Listening >"$R/log/journal.$i"\n'
                         '    elif ! of t3-silent; then printf "Listening\\nT3 Code server is ready.\\n" '
                         '>"$R/log/journal.$i"; fi; fi; fi\n'
                         'echo $$ >"$S/$1.pid"; g="$R/sys/fs/cgroup/system.slice/$1"; rm -rf "$g"; mkdir -p "$g"; '
                         'echo $$ >"$g/cgroup.procs"; python3 -I -c "import time; print(time.monotonic_ns() // 1000)" '
                         '>"$S/$1.start"; }\n'
                         'case "$1" in\n'
                         'enable|disable) v=$1; shift; now=; [ "$1" = --now ] && { now=1; shift; }\n'
                         '  for u; do if [ $v = enable ]; then : >"$S/$u.enabled"; [ -n "$now" ] && '
                         '{ [ -e "$S/$u.active" ] || run "$u" || exit 1; }\n'
                         '  else rm -f "$S/$u.enabled"; [ -z "$now" ] || { rm -f "$S/$u.active"; '
                         'rm -rf "$R/sys/fs/cgroup/system.slice/$u"; }; fi; done;;\n'
                         'restart) shift; for u; do run "$u" || exit 1; done;;\n'
                         'reset-failed) shift; for u; do rm -f "$S/$u.limit"; done;;\n'
                         'stop) shift; for u; do rm -f "$S/$u.active"; rm -rf "$R/sys/fs/cgroup/system.slice/$u"; done;;\n'
                         'is-active) [ -e "$S/$2.active" ] && echo active || { echo inactive; exit 3; };;\n'
                         'show) u=$4; f=; [ -e "$R/etc/systemd/system/$u" ] && f=/etc/systemd/system/$u\n'
                         '  [ -e "$R/log/t3-dies" ] && [ -e "$S/$u.active" ] && { [ -e "$S/$u.seen" ] && '
                         'rm -f "$S/$u.active"; : >"$S/$u.seen"; }\n'
                         '  st=disabled; [ -e "$S/$u.enabled" ] && st=enabled\n'
                         '  case "$u" in pc-agents-walls.service|pc-agents-update.service) st=static;; esac\n'
                         '  echo "LoadState=${f:+loaded}"; echo "FragmentPath=$f"\n'
                         '  echo "DropInPaths=$(cat "$S/$u.dropins" 2>/dev/null)"; echo "UnitFileState=$st"\n'
                         '  [ -e "$S/reload" ] && echo NeedDaemonReload=yes || echo NeedDaemonReload=no\n'
                         '  if [ -e "$S/$u.active" ]; then echo ActiveState=active; echo "MainPID=$(cat "$S/$u.pid")"\n'
                         '  echo "ControlGroup=/system.slice/$u"\n'
                         '  else echo ActiveState=inactive; echo MainPID=0; echo ControlGroup=; fi; echo NRestarts=0\n'
                         '  echo "InvocationID=$(cat "$S/$u.invocation" 2>/dev/null)"\n'
                         '  [ -e "$S/$u.status" ] && { echo "ExecMainCode=$(cat "$S/$u.code")"; echo "ExecMainStatus=$(cat "$S/$u.status")"; }\n'
                         '  echo "ExecMainStartTimestampMonotonic=$(cat "$S/$u.start" 2>/dev/null || echo 0)";;\n'
                         'esac\n',
    # apt-get [-o OPTION]... update|install -y PKG...: logs the rest (and the options, in /log/apt-get-options);
    # installs each package in /log/packages (nftables brings the fake nft kept in /log/nft-package). As real apt-get:
    # refuses while a package is unpacked, and `install` while /var/lib/dpkg/updates holds a file named all digits
    # (dpkg was interrupted). /log/apt-locked-update (or -install) holds how many more runs find the lists (or
    # archives) lock held, as while apt-daily runs: real apt-get does not wait for those. Its lock messages are in
    # German under a German locale (LC_ALL, else LC_MESSAGES, else LANG), as libapt's translations; /log/apt-denied
    # makes the lock file one it may not open (not root). /log/apt-fail makes it fail; /log/dns holds the resolv.conf
    # line `update` needs to reach the mirror; with /log/questions it fails unless noninteractive and keeping changed
    # config files.
    'usr/bin/apt-get': 'R="${0%/usr/bin/apt-get}"; o=; while [ "$1" = -o ]; do o="$o $2"; shift 2; done\n'
                       'echo "$*" >>"$R/log/apt-get"; echo "${o# }" >>"$R/log/apt-get-options"\n'
                       '[ "$1" = update ] && d=/var/lib/apt/lists || d=/var/cache/apt/archives\n'
                       '[ -e "$R/log/apt-denied" ] && { echo "E: Could not open lock file $d/lock - open (13: '
                       'Permission denied)" >&2; echo "E: Unable to lock directory $d/" >&2; exit 100; }\n'
                       'n=$(cat "$R/log/apt-locked-$1" 2>/dev/null || echo 0); if [ "$n" -gt 0 ]; then\n'
                       '  echo $((n - 1)) >"$R/log/apt-locked-$1"\n'
                       '  case "${LC_ALL:-${LC_MESSAGES:-$LANG}}" in de*)\n'
                       '    echo "E: Sperre $d/lock konnte nicht erlangt werden. Sie wird von Prozess 4242 (apt-get) '
                       'gehalten (test)" >&2\n'
                       '    echo "E: Das Verzeichnis $d/ kann nicht gesperrt werden." >&2;;\n'
                       '  *) echo "E: Could not get lock $d/lock. It is held by process 4242 (apt-get) (test)" >&2\n'
                       '    echo "E: Unable to lock directory $d/" >&2;; esac; exit 100; fi\n'
                       '[ -e "$R/log/apt-fail" ] && { echo "E: no network (test)" >&2; exit 100; }\n'
                       'grep -qs unpacked "$R"/log/packages/* && { echo "E: dpkg was interrupted, you must manually '
                       'run \'dpkg --configure -a\' to correct the problem." >&2; exit 100; }\n'
                       '[ "$1" != install ] || ! ls "$R/var/lib/dpkg/updates" 2>/dev/null | grep -qxE "[0-9]+" || '
                       '{ echo "E: dpkg was interrupted, you must manually run \'dpkg --configure -a\' to correct the '
                       'problem." >&2; exit 100; }\n'
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
    # dpkg --audit: names each package an apt-get stopped half-way left unpacked (no lock: it only reads) and, as
    # real dpkg, exits 0 all the same; /log/dpkg-audit-fail makes it fail (exit 2) and name nothing, as on a status
    # file it cannot parse. It does not name a package whose last state, in a journal file under
    # /var/lib/dpkg/updates, is installed. dpkg [--force-...]... --configure -a: finishes them and merges that journal
    # (removes its files). /log/dpkg-locked holds how many more runs find the lock held (real dpkg does not wait),
    # with the message in German under a German locale; /log/questions as for apt-get.
    'usr/bin/dpkg': 'R="${0%/usr/bin/dpkg}"; echo "$*" >>"$R/log/dpkg"\n'
                    'if [ "$1" = --audit ]; then [ ! -e "$R/log/dpkg-audit-fail" ] || '
                    '{ echo "dpkg: error: parsing file \'/var/lib/dpkg/status\' (test)" >&2; exit 2; }; h=\n'
                    '  for f in "$R"/log/packages/*; do grep -qs unpacked "$f" || continue\n'
                    '  [ -n "$h" ] || echo "The following packages have been unpacked but not yet configured:"\n'
                    '  echo " ${f##*/}"; h=1; done; exit 0; fi\n'
                    'n=$(cat "$R/log/dpkg-locked" 2>/dev/null || echo 0); if [ "$n" -gt 0 ]; then\n'
                    '  echo $((n - 1)) >"$R/log/dpkg-locked"\n'
                    '  case "${LC_ALL:-${LC_MESSAGES:-$LANG}}" in de*) echo "dpkg: Fehler: dpkg-Frontend-Sperre wurde '
                    'von einem anderen Prozess mit PID 4242 gesperrt (test)" >&2;;\n'
                    '  *) echo "dpkg: error: dpkg frontend lock was locked by another process with pid 4242 (test)" >&2;; '
                    'esac\n'
                    '  exit 2; fi\n'

                    '[ ! -e "$R/log/questions" ] || { [ "$DEBIAN_FRONTEND" = noninteractive ] && case "$*" in '
                    '*--force-confold*) :;; *) false;; esac; } || '
                    '{ echo "dpkg: error: end of file on stdin at conffile prompt (test)" >&2; exit 1; }\n'
                    'for f in "$R"/log/packages/*; do [ ! -e "$f" ] || sed -i "s/unpacked/installed/" "$f"; done\n'
                    'for f in "$R"/var/lib/dpkg/updates/[0-9]*; do [ ! -e "$f" ] || rm "$f"; done\n',
    # curl: fake_curl.py (GitHub's latest release, T3's release files and their attestations, T3's answer)
    'usr/bin/curl': f'exec python3 -I {os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_curl.py")} '
                    '"${0%/usr/bin/curl}" "$@"\n',
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
    platform = 'wsl2'   # the kind of machine: 'wsl2' (a fresh WSL2 distro) or 'linux' (a plain Linux box)

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
        k.USER, k.HOME = 'agent', '/home/agent'   # the defaults, as a fresh import has them (config() sets them)
        linux = self.platform == 'linux'
        with open(LINUX if linux else WSL2, encoding='utf-8') as f:
            files = json.load(f)['pass']
        people = 'root:x:0:0:root:/root:/bin/bash\nnobody:x:65534:65534::/nonexistent:/usr/sbin/nologin\n'
        if linux:   # the owner's box: their login user, the system resolver's stub, the router as the gateway
            files['etc/passwd'] = people + 'owner:x:1000:1000:Owner:/home/owner:/bin/bash\n'
            files['etc/group'] = 'root:x:0:\nsudo:x:27:owner\nnogroup:x:65534:\nowner:x:1000:\n'
            files['run/systemd/resolve/stub-resolv.conf'] = 'nameserver 127.0.0.53\noptions edns0 trust-ad\n'
            files['proc/net/route'] = 'Iface\tDestination\tGateway\tFlags\neth0\t00000000\t0101A8C0\t0003\n'
        else:
            for rel in ('etc/wsl.conf', 'etc/resolv.conf', 'etc/binfmt.d/WSLInterop.conf'):
                del files[rel]   # a fresh distro: the kit writes these
            files['etc/wsl.conf'] = '[boot]\nsystemd=true\n'
            files['etc/passwd'] = people
            files['etc/group'] = 'root:x:0:\nsudo:x:27:\nnogroup:x:65534:\n'
            files['proc/net/route'] = 'Iface\tDestination\tGateway\tFlags\nboth\t00000000\t011014AC\t0003\n'
        files['proc/sys/kernel/random/boot_id'] = '0f8e4f9c-1b6a-4d2e-9a57-3c1d2e4f5a6b\n'
        files['log/'] = files['opt/'] = files['etc/systemd/system/'] = ''   # as in a fresh Ubuntu
        files['log/packages/nftables'] = 'install ok installed'   # and no libatomic1 or unzip, as in a fresh Ubuntu
        files.update({rel: {'text': '#!/bin/sh\n' + text, 'mode': '755'} for rel, text in TOOLS.items()})
        for rel, value in files.items():
            self.write('/' + rel, value)
        os.symlink('../run/systemd/resolve/stub-resolv.conf' if linux else '/mnt/wsl/resolv.conf',
                   self.path('/etc/resolv.conf'))
        os.symlink(shutil.which('openssl'), self.path('/usr/bin/openssl'))   # the attestation check's
        self.machine(LINUX_CONFIG if linux else CONFIG)

    def close(self):
        os.umask(self.umask)
        shutil.rmtree(self.root, ignore_errors=True)

    def no_watch(self):
        """Setup looks at T3 once after a start, for this test and every setUp() it calls: a test that runs setup
        hundreds of times for its writes, not its watch (the watch has tests of its own)."""
        self.settle = k.SETTLE = 0

    def same_fleet_walls(self):
        """The fleet's walls (wsl2 or linux) (fork-fleet-walls.py describe, a subprocess with about 80 more: some 0.3 s, most of
        a setup run here) are looked at once for real, and every later look gets that answer: for a test that runs
        setup hundreds of times for its writes. A write a rerun leaves out still fails such a test, in one of two
        ways: setup.test.py's stopped-half-way and killed-upgrade tests compare the fake root file by file
        (same_files(): kind, mode, owner, group, content) with a whole run's; update.test.py's tests have the kit's own walls
        (check, whose file:* walls are not cached) say so. A fleet wall added later that reads a file the kit writes
        gets the cached answer too: in update.test.py, a write only that wall would catch goes unseen. The walls' own
        answers have their tests (check.test.py, the fleet walls' tests)."""
        real, seen = cli.fleet_walls, []

        def once(c):
            if not seen:
                seen.append(real(c))
            return list(seen[0])
        cli.fleet_walls = once
        self.addCleanup(setattr, cli, 'fleet_walls', real)

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
        """A distro set up all the way: first run, `wsl --terminate`, second run. A plain Linux box: one run (no
        Windows drive to unmount)."""
        code, out = self.setup_run()
        if self.platform == 'linux':
            return self.assertEqual(code, 0, out)
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
        k.run(['/usr/bin/systemctl', 'enable', '--now', k.UNIT, *k.TIMERS])
        os.unlink(self.path('/log/systemctl'))
        self.terminate()

    def snapshot(self):
        """Every path under the fake root but the fake tools' logs and the running system (/proc, /sys: processes):
        kind, mode, owner, group, mtime, inode, link, content."""
        out = {}
        for top, dirs, names in os.walk(self.root):
            for n in dirs + names:
                full = os.path.join(top, n)
                rel = full[len(self.root):]
                if rel.startswith(('/log', '/proc', '/sys')):
                    continue
                st = os.lstat(full)
                body = os.readlink(full) if stat.S_ISLNK(st.st_mode) else \
                    blob(full) if stat.S_ISREG(st.st_mode) else None
                out[rel] = (st.st_mode, st.st_uid, st.st_gid, st.st_mtime_ns, st.st_ino, body)
        return out

    def same_files(self, skip=()):
        """snapshot() as two runs that end the same must leave it: each path's kind and mode, owner, group and content
        (not its mtime or inode), but the paths in skip."""
        return {p: (v[0], v[1], v[2], v[5]) for p, v in self.snapshot().items() if p not in skip}


