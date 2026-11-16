"""start-desk.ps1, without Windows: its in-distro part run on a fake root (twice) and read by the real WSL2 walls,
and the rules a script for Windows PowerShell 5.1 must keep. With pwsh on PATH, it is also parsed.

  python3 -I -B fork-kit/desk/tests/windows.test.py
"""
import importlib.machinery, importlib.util, os, re, shutil, subprocess, tempfile, time, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(KIT))
PS1 = os.path.join(KIT, 'windows', 'start-desk.ps1')
WSL2 = os.path.join(REPO, '.github', 'scripts', 'fork-fleet-walls', 'wsl2')


def slurp(path):
    with open(path) as f:
        return f.read()


def put(path, s):
    with open(path, 'w') as f:
        f.write(s)


def text():
    with open(PS1, 'rb') as f:
        return f.read()


def prepare(name='Prepare'):
    m = re.search(r"\$" + name + r" = @'\r?\n(.*?)\r?\n'@", text().decode('ascii'), re.S)
    return m.group(1)


def wslconfig_part():
    src = text().decode('ascii')
    return src[src.index('# --- wslconfig ---'):src.index('# --- end wslconfig ---')]


def walls(root):
    os.environ['FORK_FLEET_WALLS_ROOT'] = root
    try:
        loader = importlib.machinery.SourceFileLoader('wsl2_walls', WSL2)
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader('wsl2_walls', loader))
        loader.exec_module(mod)
        return mod
    finally:
        del os.environ['FORK_FLEET_WALLS_ROOT']


class Prepare(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='desk-windows-test-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(self.root + '/etc')
        os.makedirs(self.root + '/proc/sys/fs/binfmt_misc')
        put(self.root + '/proc/sys/fs/binfmt_misc/status', 'enabled\n')

    def run_prepare(self):
        # as InDistro sends it: LF only, then '# end' and the CR LF Windows PowerShell adds
        r = subprocess.run(['bash', '-s'], input=prepare() + '\n# end\r\n', text=True, capture_output=True,
                           env={'PATH': '/usr/bin:/bin', 'ROOT': self.root})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_the_wsl2_walls_pass_after_it_and_a_second_run_changes_nothing(self):
        # as WSL leaves a new distro: interop on, resolv.conf a link into /mnt/wsl
        put(self.root + '/etc/wsl.conf', '[boot]\nsystemd=true\n[user]\ndefault=someone\n')
        os.symlink('/mnt/wsl/resolv.conf', self.root + '/etc/resolv.conf')
        w = walls(self.root)
        self.assertIsNotNone(w.interop_wall())
        self.assertIsNotNone(w.resolv_wall())
        out = self.run_prepare()
        self.assertIn('changed: /etc/wsl.conf', out)
        self.assertEqual(slurp(self.root + '/etc/resolv.conf'), 'nameserver 10.255.255.254\n')
        backups = [n for n in os.listdir(self.root + '/etc') if n.startswith('wsl.conf.bak-')]
        self.assertEqual(len(backups), 1)
        w = walls(self.root)
        for wall in (w.interop_wall, w.path_wall, w.resolv_wall):
            self.assertIsNone(wall(), wall.__name__)
        self.assertEqual(w.wsl_conf('boot', 'systemd'), 'true')
        self.assertEqual(w.wsl_conf('automount', 'enabled'), 'false')
        before = self.etc()
        self.assertEqual(self.run_prepare(), '')
        self.assertEqual(self.etc(), before)

    def etc(self):
        out = {}
        for dirpath, _, files in os.walk(self.root + '/etc'):
            for n in files:
                full = os.path.join(dirpath, n)
                out[full] = os.readlink(full) if os.path.islink(full) else slurp(full)
        return out

    def test_an_empty_distro(self):
        self.assertIn('changed: /etc/resolv.conf', self.run_prepare())
        self.assertFalse([n for n in os.listdir(self.root + '/etc') if '.bak-' in n])
        self.assertIsNone(walls(self.root).interop_wall())

    def live(self, env=None):
        # a wslinfo that answers nat first on PATH: inside a WSL with mirrored networking the real one would answer
        fake = os.path.join(self.root, 'nat-bin')
        if not os.path.exists(fake):
            os.makedirs(fake)
            put(fake + '/wslinfo', '#!/bin/sh\necho nat\n')
            os.chmod(fake + '/wslinfo', 0o755)
        r = subprocess.run(['bash', '-s'], input=prepare('Live') + '\n# end\r\n', text=True, capture_output=True,
                           env={'PATH': fake + ':/usr/bin:/bin', 'ROOT': self.root, **(env or {})})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def started(self, at):
        """PID 1 started at `at` (seconds since the epoch): /proc/stat's btime plus field 22 of /proc/1/stat."""
        os.makedirs(self.root + '/proc/1', exist_ok=True)
        tick, boot = os.sysconf('SC_CLK_TCK'), int(at) - 500
        put(self.root + '/proc/stat', f'cpu  1 2 3 4\nbtime {boot}\nprocesses 9\n')
        fields = ['1', '(sys temd)', 'S'] + ['0'] * 49   # a name with a space: the fields count from its ')'
        fields[21] = str(500 * tick)
        put(self.root + '/proc/1/stat', ' '.join(fields) + '\n')

    def test_the_restart_decision_reads_the_live_state(self):
        # a run stopped after it wrote wsl.conf, before its `wsl --shutdown`: the next run changes nothing, but the
        # running distro still has the old settings, so it restarts
        os.makedirs(self.root + '/proc/1')
        put(self.root + '/proc/1/comm', 'systemd\n')
        put(self.root + '/proc/meminfo', 'MemTotal:        8000000 kB\n')
        self.run_prepare()
        past = time.time() - 3600
        self.started(past)
        self.assertIn('restart: /etc/wsl.conf is newer than the running distro', self.live())
        os.utime(self.root + '/etc/wsl.conf', (past - 60, past - 60))
        self.assertEqual(self.live({'WANT_KB': '8388608'}), '')
        put(self.root + '/proc/sys/fs/binfmt_misc/WSLInterop', 'enabled\n')
        put(self.root + '/proc/1/comm', 'init\n')
        out = self.live({'WANT_KB': '4194304'})
        for why in ('PID 1 is not systemd', 'WSL interop is live (WSLInterop)', 'above the cap'):
            self.assertIn(why, out)
        os.unlink(self.root + '/proc/sys/fs/binfmt_misc/WSLInterop')
        put(self.root + '/proc/1/comm', 'systemd\n')
        fake = os.path.join(self.root, 'bin')
        os.makedirs(fake)
        put(fake + '/wslinfo', '#!/bin/sh\necho mirrored\n')
        os.chmod(fake + '/wslinfo', 0o755)
        self.assertIn('the networking mode is mirrored, not nat', self.live({'PATH': fake + ':/usr/bin:/bin'}))

    def test_the_start_of_pid_1_is_read_from_proc_stat_not_the_mtime_of_proc_1(self):
        os.makedirs(self.root + '/proc/1')
        put(self.root + '/proc/1/comm', 'systemd\n')
        self.run_prepare()
        now = time.time()
        os.utime(self.root + '/etc/wsl.conf', (now - 600, now - 600))
        self.started(now - 300)                    # PID 1 started after the write ...
        os.utime(self.root + '/proc/1', (now - 3600, now - 3600))   # ... whatever the mtime of /proc/1 says
        self.assertEqual(self.live(), '')
        self.started(now - 900)                    # PID 1 older than the write: it still runs the old settings
        os.utime(self.root + '/proc/1', (now, now))
        self.assertIn('restart: /etc/wsl.conf is newer than the running distro', self.live())
        os.unlink(self.root + '/proc/stat')        # unreadable: restart, never a guess
        self.assertIn('restart: the start time of PID 1 cannot be read', self.live())

    def test_a_mounted_windows_drive_restarts(self):
        os.makedirs(self.root + '/proc/1')
        put(self.root + '/proc/1/comm', 'systemd\n')
        self.started(time.time())
        put(self.root + '/proc/mounts', 'none /usr/lib/wsl/drivers 9p ro 0 0\n/dev/sdc / ext4 rw 0 0\n')
        self.assertEqual(self.live(), '')   # WSL's own driver share is not a Windows drive
        put(self.root + '/proc/mounts', 'C:\\134 /mnt/c 9p rw 0 0\n')
        self.assertIn('restart: Windows drives are mounted under /mnt', self.live())

    def test_a_resolv_conf_of_its_own_is_kept(self):
        put(self.root + '/etc/resolv.conf', 'nameserver 192.0.2.1\n')
        self.run_prepare()
        self.assertEqual(slurp(self.root + '/etc/resolv.conf'), 'nameserver 192.0.2.1\n')


class Script(unittest.TestCase):
    def test_windows_powershell_reads_it(self):
        raw = text()
        self.assertTrue(raw.isascii(), 'Windows PowerShell 5.1 reads a file without a BOM as ANSI: ASCII only')
        self.assertNotIn(b'\t', raw)
        src = raw.decode('ascii')
        for a, b in (('{', '}'), ('(', ')'), ('[', ']')):
            self.assertEqual(src.count(a), src.count(b), a)
        if shutil.which('pwsh'):
            # the path inside the command: words after -Command join the command, they never reach $args
            path = PS1.replace("'", "''")
            r = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-Command',
                                f"$e=$null; [System.Management.Automation.Language.Parser]::ParseFile('{path}', "
                                "[ref]$null, [ref]$e) | Out-Null; if ($e) { $e | Out-String; exit 1 }"],
                               capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_it_asks_before_it_changes_anything(self):
        src = text().decode('ascii')
        body = src[src.index('try {'):]
        first_change = min(body.index(x) for x in ('wsl.exe --install', 'Merge-WslConfig $WslConfig',
                                                   'Unregister-ScheduledTask', 'InDistro', 'Register-ScheduledTask',
                                                   'wsl.exe --shutdown'))
        self.assertIn("Read-Host", body[:first_change])

    def test_a_distro_it_did_not_make_is_named_and_must_be_typed(self):
        src = text().decode('ascii')
        body = src[src.index('try {'):]
        ask = body.index("if ($have -and -not $ours) {\n    Write-Host")
        self.assertLess(ask, body.index('wsl.exe --install'))
        self.assertIn("-cne $Distro", body[ask:ask + 800])
        self.assertIn('/etc/wsl.conf is REPLACED', body[ask:ask + 800])
        self.assertIn('/etc/fork-kit/desk-distro', prepare())   # the mark of a distro it made or took over

    def test_wsl1_is_refused_before_anything_changes_and_a_new_distro_is_wsl2(self):
        src = text().decode('ascii')
        body = src[src.index('try {'):]
        install = [x for x in body.splitlines() if 'wsl.exe --install' in x]
        self.assertEqual(len(install), 1)
        self.assertIn('--version 2', install[0])
        refuse = body.index('if ($have -and $version -ne 2) { throw')
        for later in ("Read-Host 'Type yes", 'Merge-WslConfig $WslConfig', 'wsl.exe --shutdown', 'Mark $Made',
                      "InDistro \"the distro's settings\""):
            self.assertLess(refuse, body.index(later), later)
        self.assertLess(body.index('wsl.exe --install'), body.index('if ($version -ne 2) { throw'))
        self.assertLess(body.index('if ($version -ne 2) { throw'), body.index('wsl.exe --shutdown'))
        self.assertIn('WslVersion @(wsl.exe -l -v) $Distro', body)
        self.assertNotIn('wsl.exe -l -q', body)

    def test_inputs_are_checked_before_the_question(self):
        src = text().decode('ascii')
        body = src[src.index('if (-not $MachineFile)'):]
        ask = min(body.index("Read-Host 'Type yes"), body.index('Read-Host "Type the distro'))
        self.assertLess(body.index('Test-Path -LiteralPath $MachineFile -PathType Leaf'), ask)
        self.assertLess(body.index('Test-Path -LiteralPath $FleetConfig -PathType Leaf'), ask)
        self.assertIn("-cmatch '^[0-9a-f]{40}$'", src.split('try {')[0])   # an uppercase -Sha is refused too

    def test_a_stopped_run_keeps_its_marks(self):
        src = text().decode('ascii')
        body = src[src.index('try {'):]
        self.assertLess(body.index('Mark $Made'), body.index('wsl.exe --install'))
        self.assertIn('Merge-WslConfig $WslConfig $want { Mark $Owed }', body)
        self.assertIn('& $before', wslconfig_part())
        self.assertLess(wslconfig_part().index('& $before'), wslconfig_part().index('WriteAllLines'))
        shutdown = body.index('Run \'wsl --shutdown\'')
        self.assertLess(body.index('if (Test-Path -LiteralPath $Owed)'), shutdown)
        self.assertGreater(body.index('Remove-Item -LiteralPath $Owed -ErrorAction'), shutdown)
        self.assertIn('wsl --unregister $Distro (removes it and all in it)', body)
        uninstall = body[body.index('if ($Uninstall)'):]
        self.assertLess(uninstall.index('Stop-ScheduledTask'), uninstall.index('Unregister-ScheduledTask'))
        self.assertIn('.wslconfig as it was before the first run', uninstall)

    @unittest.skipUnless(shutil.which('pwsh'), 'pwsh is not installed')
    def test_wsl_version_is_read_from_the_list(self):
        d = tempfile.mkdtemp(prefix='desk-wslversion-test-')
        self.addCleanup(shutil.rmtree, d, True)
        script = os.path.join(d, 'v.ps1')
        with open(script, 'w', encoding='ascii') as f:
            f.write(wslconfig_part() + '\n$l = @("  NAME      STATE    VERSION", "* Ubuntu    Running  2", '
                    '"  fork-desk Stopped  1")\n"$(WslVersion $l fork-desk) $(WslVersion $l Ubuntu) '
                    '$(WslVersion $l none)"\n')
        r = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-File', script], capture_output=True,
                           text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), '1 2 0')

    def test_old_experimental_networking_keys_are_removed_named_and_checked(self):
        part = wslconfig_part()
        self.assertIn("$Experimental = @('networkingMode', 'dnsTunneling')", part)
        merge = part[part.index('function Merge-WslConfig'):part.index('function WslVersion')]
        self.assertIn("$sec -eq 'experimental'", merge)
        self.assertIn('$Matches[1] -in $Experimental) { continue }', merge)
        src = text().decode('ascii')
        body = src[src.index('try {'):]
        ask = min(body.index("Read-Host 'Type yes"), body.index('Read-Host "Type the distro'))
        self.assertLess(body.index('$changes = @(Changes (Read-WslConfig $WslConfig) $want)'), ask)
        check = body[body.index("Head 'check'"):]
        self.assertIn("SectionKeys (Read-WslConfig $WslConfig) 'experimental'", check)
        self.assertIn('FAIL  wslconfig  [experimental] $k=', check)

    def test_the_owed_shutdown_mark_is_shared_like_wslconfig(self):
        src = text().decode('ascii')
        self.assertIn("$Owed = Join-Path $Marks 'wslconfig.restart'", src)
        uninstall = src[src.index('if ($Uninstall)'):src.index("if (-not $MachineFile)")]
        self.assertNotIn('$Owed', uninstall)   # another distro's run still owes the shutdown

    @unittest.skipUnless(shutil.which('pwsh'), 'pwsh is not installed')
    def test_wslconfig_merge_removes_old_experimental_networking_keys(self):
        d = tempfile.mkdtemp(prefix='desk-wslconfig-test-')
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, '.wslconfig')
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write('[wsl2]\nmemory=24GB\nguiApplications=true\n[experimental]\nnetworkingMode=mirrored\n'
                    'sparseVhd=true\ndnsTunneling=false\n')
        script = os.path.join(d, 'merge.ps1')
        with open(script, 'w', encoding='ascii') as f:
            f.write(wslconfig_part() + '\n$want = [ordered]@{ memory = "9GB"; networkingMode = "nat"; '
                    'guiApplications = "false" }\nChanges (Read-WslConfig $args[0]) $want\n'
                    '"changed=$(Merge-WslConfig $args[0] $want)"\n'
                    '"left=$((SectionKeys (Read-WslConfig $args[0]) \'experimental\').Keys -join \',\')"\n'
                    '"after=$(@(Changes (Read-WslConfig $args[0]) $want).Count)"\n')
        r = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-File', script, path], capture_output=True,
                           text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for line in ('[wsl2] memory: 24GB -> 9GB', '[wsl2] networkingMode: (not set) -> nat',
                     '[wsl2] guiApplications: true -> false', '[experimental] networkingMode=mirrored: removed',
                     '[experimental] dnsTunneling=false: removed', 'changed=True', 'left=sparseVhd', 'after=0'):
            self.assertIn(line, r.stdout)

    def test_wslconfig_is_utf8_without_a_bom_never_ascii(self):
        src = text().decode('ascii')
        self.assertNotRegex(src, r'(?i)-Encoding\s+ascii')
        self.assertNotRegex(src, r'(?i)(Get|Set)-Content[^\n]*\$WslConfig')
        self.assertIn('[Text.UTF8Encoding]::new($false)', wslconfig_part())

    @unittest.skipUnless(shutil.which('pwsh'), 'pwsh is not installed')
    def test_wslconfig_merge_keeps_every_other_letter_and_key(self):
        d = tempfile.mkdtemp(prefix='desk-wslconfig-test-')
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, '.wslconfig')
        old = '[wsl2]\nkernel=D:\\kernels\\Jos\u00e9\\bzImage\nmemory=2GB\n[experimental]\nsparseVhd=true\nmemory=9GB\n'
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(old)
        script = os.path.join(d, 'merge.ps1')
        with open(script, 'w', encoding='ascii') as f:
            f.write(wslconfig_part() + '\n$want = [ordered]@{ memory = "6GB"; swap = "4GB" }\n'
                    '"changed=$(Merge-WslConfig $args[0] $want)"\n"again=$(Merge-WslConfig $args[0] $want)"\n'
                    '$k = Wsl2Keys (Read-WslConfig $args[0]); "memory=$($k[\'memory\'])"\n')
        r = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-File', script, path], capture_output=True,
                           text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('changed=True', r.stdout)
        self.assertIn('again=False', r.stdout)
        self.assertIn('memory=6GB', r.stdout)   # the [wsl2] one, not [experimental]'s
        with open(path, 'rb') as f:
            raw = f.read()
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
        new = raw.decode('utf-8').replace('\r\n', '\n')
        self.assertIn('kernel=D:\\kernels\\Jos\u00e9\\bzImage\n', new)
        self.assertIn('[wsl2]\nkernel=D:\\kernels\\Jos\u00e9\\bzImage\nmemory=6GB\nswap=4GB\n[experimental]', new)
        self.assertIn('sparseVhd=true\nmemory=9GB', new)
        backups = [n for n in os.listdir(d) if n.startswith('.wslconfig.bak-')]
        self.assertEqual(len(backups), 1)

    def test_text_piped_into_the_distro_ends_in_a_comment(self):
        src = text().decode('ascii')
        self.assertIn('''function Script([string]$s) { ($s -replace "`r", '') + "`n# end" }''', src)
        for line in src.splitlines():
            if '| wsl.exe' in line and 'Script ' not in line:
                self.assertIn('$text |', line)   # Into: data (JSON), where a trailing CR LF is only white space
        self.assertNotIn('`"', src.split('try {')[0].split('$Prepare')[0])   # no quotes inside native arguments

    def test_the_kit_it_runs_is_this_one(self):
        src = text().decode('ascii')
        self.assertIn('/fork-kit/desk/bootstrap.sh', src)
        self.assertTrue(os.path.isfile(os.path.join(KIT, 'bootstrap.sh')))
        self.assertIn('/opt/fork-fleet/desk-install check', src)


if __name__ == '__main__':
    unittest.main()
