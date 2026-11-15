"""start-desk.ps1, without Windows: its in-distro part run on a fake root (twice) and read by the real WSL2 walls,
and the rules a script for Windows PowerShell 5.1 must keep. With pwsh on PATH, it is also parsed.

  python3 -I -B fork-kit/desk/tests/windows.test.py
"""
import importlib.machinery, importlib.util, os, re, shutil, subprocess, tempfile, unittest

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


def prepare():
    m = re.search(r"\$Prepare = @'\r?\n(.*?)\r?\n'@", text().decode('ascii'), re.S)
    return m.group(1)


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
        first_change = min(body.index(x) for x in ('wsl.exe --install', 'Set-Content', 'Unregister-ScheduledTask',
                                                   'InDistro', 'Register-ScheduledTask', 'wsl.exe --shutdown'))
        self.assertIn("Read-Host", body[:first_change])

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
