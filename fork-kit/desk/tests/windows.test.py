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

    def test_check_only_says_whether_wsl_conf_changes_and_writes_nothing(self):
        def check_only():
            r = subprocess.run(['bash', '-s'], input='CHECK_ONLY=1\n' + prepare() + '\n# end\r\n', text=True,
                               capture_output=True, env={'PATH': '/usr/bin:/bin', 'ROOT': self.root})
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout
        before = self.etc()
        self.assertEqual(check_only(), 'differs: /etc/wsl.conf\n')
        self.assertEqual(self.etc(), before)
        self.assertFalse(os.path.exists(self.root + '/etc/binfmt.d'))
        self.run_prepare()
        self.assertEqual(check_only(), '')

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
        merge = part[part.index('function MergeLines'):part.index('function WslVersion')]
        self.assertIn("$sec -eq 'experimental'", merge)
        self.assertIn('$Matches[1] -in $Experimental) {', merge)
        self.assertIn(': removed (the [wsl2] one is used)"); continue }', merge)
        src = text().decode('ascii')
        body = src[src.index('try {'):]
        ask = min(body.index("Read-Host 'Type yes"), body.index('Read-Host "Type the distro'))
        self.assertLess(body.index('$changes = @(Changes (Read-WslConfig $WslConfig) $want)'), ask)
        check = body[body.index("Head 'check'"):]
        self.assertIn("SectionKeys (Read-WslConfig $WslConfig) 'experimental'", check)
        self.assertIn('FAIL  wslconfig  [experimental] $k=', check)

    def test_the_question_and_the_merge_decide_the_same_way(self):
        """lens d8min r3 N2: the question compared trimmed values, the merge whole text, so "no change" could be shown
        while the file was rewritten. Both now come from MergeLines: the file is written only when it has a note."""
        part = wslconfig_part()
        self.assertIn('function Changes([string[]]$lines, $want) { (MergeLines $lines $want).Notes }', part)
        merge = part[part.index('function Merge-WslConfig'):part.index('function WslVersion')]
        self.assertIn('$merged = MergeLines (Read-WslConfig $path) $want', merge)
        self.assertIn('if ($merged.Notes.Count -eq 0) { return $false }', merge)
        self.assertNotIn('-join', merge)   # no second, text-wide test

    @unittest.skipUnless(shutil.which('pwsh'), 'pwsh is not installed')
    def test_no_change_means_the_file_is_left_alone(self):
        d = tempfile.mkdtemp(prefix='desk-wslconfig-test-')
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, '.wslconfig')
        for old, notes in (('[wsl2]\nmemory = 9GB\nNetworkingMode=NAT\n', []),
                           ('[wsl2]\nmemory=9GB\nnetworkingMode=nat\nmemory=12GB\n',
                            ['[wsl2] memory=12GB: a repeated line, removed']),
                           # WSL trims space and tab only: a key after a non-breaking space or a form feed is not
                           # networkingMode to WSL, so it is not removed (and the check does not FAIL on it either)
                           ('[wsl2]\nmemory=9GB\nnetworkingMode=nat\n[experimental]\n networkingMode=mirrored\n'
                            '\fdnsTunneling=true\n', [])):
            with self.subTest(old):
                with open(path, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(old)
                script = os.path.join(d, 'merge.ps1')
                with open(script, 'w', encoding='ascii') as f:
                    f.write(wslconfig_part() + '\n$want = [ordered]@{ memory = "9GB"; networkingMode = "nat" }\n'
                            '"notes=$(@(Changes (Read-WslConfig $args[0]) $want) -join \'|\')"\n'
                            '"changed=$(Merge-WslConfig $args[0] $want)"\n')
                r = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-File', script, path],
                                   capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(f'changed={bool(notes)}', r.stdout)
                for n in notes:
                    self.assertIn(n, r.stdout)
                if not notes:
                    self.assertIn('notes=\n', r.stdout)
                    self.assertEqual(slurp(path), old)
                for n in os.listdir(d):
                    if n.startswith('.wslconfig.bak-'):
                        os.unlink(os.path.join(d, n))

    def test_the_owed_mark_goes_before_wsl_conf_changes(self):
        """lens d8min r3 N1: the start of PID 1 is read from the VM's clock, which skips the PC's sleep, so a run
        stopped after writing wsl.conf could look restarted. The owed mark now goes first when wsl.conf will change."""
        src = text().decode('ascii')
        body = src[src.index("Head \"the distro's own settings\""):]
        self.assertLess(body.index('if (InDistro \'reading /etc/wsl.conf\' ("CHECK_ONLY=1`n" + $Prepare)) { Mark $Owed }'),
                        body.index("$changed = InDistro \"the distro's settings\" $Prepare"))

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

    @unittest.skipUnless(shutil.which('pwsh'), 'pwsh is not installed')
    def test_section_headers_are_read_as_wsl_reads_them(self):
        """lens #354: WSL's ParseSection (configfile.cpp) takes space or tab, '[', a letter then letters or digits,
        ']', space or tab, then the end or a '#' comment, in any case. On a '[' line it rejects, the keys after it go
        under the old section's length of the letters it had read: none read keeps the section."""
        d = tempfile.mkdtemp(prefix='desk-wslconfig-test-')
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, '.wslconfig')
        want = '[wsl2]\nmemory=9GB\nnetworkingMode=nat\nguiApplications=false\n'
        # (old file, the [wsl2] memory WSL reads, [experimental] keys WSL reads, the file after the merge)
        rows = (
            ('[wsl2]\nmemory=9GB\n[experimental] # from an old guide\ndnsTunneling=false\n', '9GB', 'dnsTunneling',
             want + '[experimental] # from an old guide\n'),
            ('[experimental]\nautoProxy=true\n[ wsl2 ]\nmemory=4GB\n', '', 'autoProxy,memory',
             '[experimental]\nautoProxy=true\n[ wsl2 ]\nmemory=4GB\n' + want),
            ('[WSL2]\nmemory=9GB\nnetworkingMode=nat\nguiApplications=false\n', '9GB', '', None),
            ('\t[wsl2]\nmemory=9GB\nnetworkingMode=nat\nguiApplications=false\n', '9GB', '', None),
            # a vertical tab is not space to WSL: the line is rejected, so this is a repeated [wsl2] key
            (want + '\v[experimental]\nnetworkingMode=mirrored\n', '9GB', '', want + '\v[experimental]\n'),
            # rejected after the name: WSL keeps the old section's length (4) of it, 'expe' here, so neither section
            ('[wsl2]\nmemory=9GB\n[experimental] old\nnetworkingMode=mirrored\n', '9GB', '',
             want + '[experimental] old\nnetworkingMode=mirrored\n'),
            # ... and from [boot] (also 4 letters) into wsl2
            ('[boot]\nsystemd=true\n[wsl2] old\nmemory=9GB\nnetworkingMode=nat\nguiApplications=false\n', '9GB', '',
             None),
            # ... and from a longer one into 'wsl2' padded past its length, which is no section (lens re-check: a NUL
            # pad compared equal to 'wsl2' in PowerShell). WSL takes no comment but '#' on a header.
            ('[experimental]\nautoMemoryReclaim=gradual\n[wsl2] ; set by hand\nmemory=20GB\n', '', 'autoMemoryReclaim',
             '[experimental]\nautoMemoryReclaim=gradual\n[wsl2] ; set by hand\nmemory=20GB\n' + want),
            # only space or tab after ']': a form feed rejects the header after its name
            ('[experimental]\nautoProxy=true\n[wsl2]\f\nmemory=9GB\nnetworkingMode=nat\nguiApplications=false\n', '',
             'autoProxy', '[experimental]\nautoProxy=true\n[wsl2]\f\nmemory=9GB\nnetworkingMode=nat\nguiApplications=false\n'
             + want),
            # a name starts with a letter: none read, so the keys stay in [wsl2]
            ('[wsl2]\nmemory=9GB\n[1x]\nnetworkingMode=nat\nguiApplications=false\n', '9GB', '', None),
            # names are ASCII any case (strcasecmp): under a Turkish culture .ToLower() makes this 'experımental'
            (want + '[EXPERIMENTAL]\nnetworkingMode=mirrored\n', '9GB', 'networkingMode', want + '[EXPERIMENTAL]\n'),
        )
        script = os.path.join(d, 'merge.ps1')
        with open(script, 'w', encoding='ascii') as f:
            f.write(wslconfig_part() + '\n$want = [ordered]@{ memory = "9GB"; networkingMode = "nat"; '
                    'guiApplications = "false" }\n$l = Read-WslConfig $args[0]\n'
                    '"memory=$((Wsl2Keys $l)[\'memory\'])"\n'
                    '"experimental=$((SectionKeys $l \'experimental\').Keys | Sort-Object | Join-String -Separator \',\')"\n'
                    '"changed=$(Merge-WslConfig $args[0] $want)"\n'
                    '"after=$(@(Changes (Read-WslConfig $args[0]) $want).Count)"\n'
                    '"now=$((Wsl2Keys (Read-WslConfig $args[0]))[\'memory\'])"\n')
        for (old, memory, experimental, new), culture in ((row, c) for c in ('C', 'tr_TR.UTF-8') for row in rows):
            with self.subTest(old, culture=culture):   # .NET takes its culture from LC_ALL (tr: I lowers to dotless ı)
                with open(path, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(old)
                r = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-File', script, path],
                                   capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
                                   env={**os.environ, 'LC_ALL': culture})
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                out = r.stdout.splitlines()
                self.assertIn(f'memory={memory}', out)   # what the check at the end reads
                self.assertIn(f'experimental={experimental}', out)
                self.assertIn(f'changed={new is not None}', out)
                self.assertEqual(slurp(path), old if new is None else new)
                self.assertIn('after=0', out)
                self.assertIn('now=9GB', out)
                for n in os.listdir(d):
                    if n.startswith('.wslconfig.bak-'):
                        os.unlink(os.path.join(d, n))

    @unittest.skipUnless(shutil.which('pwsh'), 'pwsh is not installed')
    def test_the_memory_cap_kept_is_one_wsl_reads_within_its_bounds(self):
        """Without -MemoryGB, a higher cap a person set (more agents or jobs in WSL) is neither lowered nor a FAIL, nor
        a `wsl --shutdown` on every run (the live check's cap is the one kept), up to the RAM less 2 GB. With
        -MemoryGB, that is the cap. A value WSL itself cannot read (it then runs on half the RAM) is rewritten: its
        parser takes a number and B, K, KB, M, MB, G, GB, T or TB exactly, no space, upper case (#296)."""
        d = tempfile.mkdtemp(prefix='desk-wslconfig-test-')
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, '.wslconfig')
        script = os.path.join(d, 'memory.ps1')
        with open(script, 'w', encoding='ascii') as f:
            f.write(wslconfig_part() + '\n$want = [ordered]@{ memory = (WantMemory $args[0] 8 $args[1]); swap = "4GB" }\n'
                    '"want=$($want.memory) kb=$(CapKb $want.memory)"\n'
                    '"notes=$(@(Changes (Read-WslConfig $args[0]) $want) -join \'|\')"\n'
                    '$null = Merge-WslConfig $args[0] $want\n$cfg = Wsl2Keys (Read-WslConfig $args[0])\n'
                    '"check=$(@($want.Keys | Where-Object { $cfg[$_] -ne $want[$_] }).Count)"\n')
        for old, most, want, kb, note in (
                ('memory=26GB', 30, '26GB', 27262976, ''),   # raised: kept, no change, no FAIL
                ('memory=30GB', 30, '30GB', 31457280, ''),   # the RAM less 2 GB
                ('memory = 30720MB', 30, '30720MB', 31457280, ''),
                ('memory=8GB', 30, '8GB', 8388608, ''),
                ('memory=9G', 30, '9G', 9437184, ''),
                ('memory=31GB', 30, '8GB', 8388608, '[wsl2] memory: 31GB -> 8GB'),   # Windows would keep under 2 GB
                ('memory=64GB', 30, '8GB', 8388608, '[wsl2] memory: 64GB -> 8GB'),   # from a bigger PC
                ('memory=16777216TB', 30, '8GB', 8388608, '[wsl2] memory: 16777216TB -> 8GB'),   # WSL: 0, its default
                ('memory=26 GB', 30, '8GB', 8388608, '[wsl2] memory: 26 GB -> 8GB'),   # WSL rejects these two
                ('memory=30720mb', 30, '8GB', 8388608, '[wsl2] memory: 30720mb -> 8GB'),
                ('memory=26Gb', 30, '8GB', 8388608, '[wsl2] memory: 26Gb -> 8GB'),   # the unit is case-sensitive
                # WSL trims only space and tab: a non-breaking space (pasted from a web page) or a form feed after
                # the unit stays in its value, and it rejects that (#321)
                ('memory=26GB\u00a0', 30, '8GB', 8388608, '[wsl2] memory: 26GB\u00a0 -> 8GB'),
                ('memory=26GB\f', 30, '8GB', 8388608, '[wsl2] memory: 26GB\f -> 8GB'),
                ('memory=\u00a026GB', 30, '8GB', 8388608, '[wsl2] memory: \u00a026GB -> 8GB'),
                ('memory=4GB', 30, '8GB', 8388608, '[wsl2] memory: 4GB -> 8GB'),   # below: raised
                ('memory=8191MB', 30, '8GB', 8388608, '[wsl2] memory: 8191MB -> 8GB'),
                ('memory=lots', 30, '8GB', 8388608, '[wsl2] memory: lots -> 8GB'),
                ('', 30, '8GB', 8388608, '[wsl2] memory: (not set) -> 8GB'),
                ('memory=26GB', 8, '8GB', 8388608, '[wsl2] memory: 26GB -> 8GB'),   # -MemoryGB 8 lowers it
                ('memory=8192MB', 8, '8192MB', 8388608, '')):   # -MemoryGB 8, the same size: left as written
            with self.subTest(old=old, most=most):
                with open(path, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(f'[wsl2]\n{old}\nswap=4GB\n')
                r = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-File', script, path, str(most)],
                                   capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(f'want={want} kb={kb}\n', r.stdout)
                self.assertIn(f'notes={note and "  " + note}\n', r.stdout)   # a note is indented
                self.assertIn('check=0\n', r.stdout)
                if not note:
                    self.assertEqual(slurp(path), f'[wsl2]\n{old}\nswap=4GB\n')   # left alone

    def test_the_memory_cap_decides_through_wantmemory(self):
        src = text().decode('ascii')
        body = src[src.index('try {'):]
        # -MemoryGB given: it is the cap; not given: a higher one is kept up to what leaves Windows 2 GB
        self.assertIn('$most = if ($asked) { $MemoryGB } else { $ram - 2 }', body)
        self.assertIn('$want = [ordered]@{ memory = (WantMemory $WslConfig $MemoryGB $most);', body)
        self.assertIn('("WANT_KB=$(CapKb $want.memory)`n" + $Live)', body)   # the cap kept, not -MemoryGB
        self.assertNotIn('$MemoryGB * 1048576', body)

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
