#!/usr/bin/env python3
"""Tests of fork-fleet-desk-key.py: a temporary HOME and fleet config only. Run: python3 <this>. POSIX."""
import base64, contextlib, fcntl, glob, importlib.util, io, json, os, pwd, re, shlex, shutil, struct, subprocess
import sys, tempfile, unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, 'fork-fleet-desk-key.py')
_spec = importlib.util.spec_from_file_location('desk_key', TOOL)
dk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dk)


def blob(seed, kind=b'ssh-ed25519', size=32):
    data = struct.pack('>I', len(kind)) + kind + struct.pack('>I', size) + bytes([seed]) * size
    return base64.b64encode(data).decode()


K1, K2 = blob(1), blob(2)
PUB1 = f'ssh-ed25519 {K1} someone@desktop\n'.encode()
ME = pwd.getpwuid(os.geteuid()).pw_name
PRIVATE = b'-----BEGIN OPENSSH PRIVATE KEY-----\nc2VjcmV0LW1hcmtlcg==\n-----END OPENSSH PRIVATE KEY-----\n'
OK, PROBLEM, REFUSED, MISSING, ERROR = 0, 1, 2, 3, 4
# The tool as a test user: this user, whose password-database home is the temporary HOME, in no admin group, and not
# held to the system-account rule (CI users are often in docker or adm, or have a low uid). Those checks have their
# own tests below, run in process.
RUN = r'''
import importlib.util, os, pwd, sys
sys.dont_write_bytecode = True
tool, home = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location('desk_key', tool)
dk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dk)
real = pwd.getpwuid
pwd.getpwuid = lambda uid: (lambda e: pwd.struct_passwd((e.pw_name, e.pw_passwd, e.pw_uid, e.pw_gid, e.pw_gecos, home,
                                                          e.pw_shell)))(real(uid))
os.getgrouplist = lambda name, gid: []
dk.SYSTEM_UID_MAX = -1
sys.exit(dk.main(sys.argv[3:]))
'''


def desk(name):
    return {'name': name, 'label': f'fleet-{name}-example', 'slots': 1, 'waits': False,
            'beacon': f'FLEET_{name.upper()}',
            'launch': ['python3', '-I', '/opt/fork-fleet/fork-fleet-desk.py', 'launch', name]}


class DeskKey(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, 'home')
        os.mkdir(self.home)
        cfg = dk.ff.load()
        cfg['pools'] = cfg['pools'] + [desk('desk'), desk('desk2')]
        self.config = os.path.join(self.tmp, 'fleet.json')
        with open(self.config, 'w') as f:
            json.dump(cfg, f)
        self.ssh = os.path.join(self.home, '.ssh')
        self.keys = os.path.join(self.ssh, 'authorized_keys')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def env(self):
        return {'HOME': self.home, 'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LC_ALL': 'C'}

    def cmd(self, *args):
        return [sys.executable, '-I', '-c', RUN, TOOL, self.home, '--config', self.config, '--user', ME, *args]

    def run_tool(self, *args, stdin=b''):
        p = subprocess.run(self.cmd(*args), input=stdin, env=self.env(), capture_output=True, timeout=60)
        return p.returncode, p.stdout.decode() + p.stderr.decode()

    def put(self, data, mode=0o600):
        os.makedirs(self.ssh, mode=0o700, exist_ok=True)
        with open(self.keys, 'wb') as f:
            f.write(data)
        os.chmod(self.keys, mode)

    def content(self):
        with open(self.keys, 'rb') as f:
            return f.read()

    def snap(self):
        st = os.lstat(self.keys)
        return self.content(), st.st_ino, st.st_mtime_ns, st.st_mode

    def test_add_to_empty_home_then_again_then_remove(self):
        self.assertEqual(self.run_tool('add', 'desk', stdin=PUB1)[0], 0)
        line = 'restrict,command="/usr/bin/python3 -I /opt/fork-fleet/fork-fleet-desk.py serve desk" ' \
               f'ssh-ed25519 {K1} fork-fleet-desk:desk\n'
        self.assertEqual(self.content(), line.encode())
        self.assertEqual(os.stat(self.ssh).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.keys).st_mode & 0o777, 0o600)
        before = self.snap()
        code, out = self.run_tool('add', 'desk', stdin=PUB1)
        self.assertEqual((code, self.snap()), (0, before), out)
        self.assertIn('already there', out)
        self.assertEqual(self.run_tool('status', 'desk')[0], 0)
        self.assertEqual(self.run_tool('remove', 'desk')[0], 0)
        self.assertEqual(self.content(), b'')
        before = self.snap()
        self.assertEqual(self.run_tool('remove', 'desk')[0], 0)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.run_tool('status', 'desk')[0], MISSING)

    def test_other_lines_stay_byte_for_byte_and_mode_kept(self):
        for start in (b'# mine\r\nssh-rsa AAAAB3 me@laptop\r\n\xff\xfe odd bytes\n\n',
                      f'from="10.0.0.1" ssh-ed25519 {K2} other\nno newline at the end'.encode()):
            with self.subTest(start=start):
                self.put(start, 0o640)
                self.assertEqual(self.run_tool('add', 'desk', stdin=PUB1)[0], 0)
                got = self.content()
                end = b'' if start.endswith(b'\n') else b'\n'
                self.assertTrue(got.startswith(start + end), got)
                self.assertEqual(got.count(b'\n'), start.count(b'\n') + len(end) + 1)
                self.assertEqual(os.stat(self.keys).st_mode & 0o777, 0o640)
                self.assertEqual(os.stat(self.keys).st_uid, os.getuid())
                self.assertEqual(self.run_tool('remove', 'desk')[0], 0)
                self.assertEqual(self.content(), start + end)

    def test_refuses_anything_but_one_valid_ed25519_public_key(self):
        self.put(b'# untouched\n')
        before = self.snap()
        bad = {
            'empty': b'', 'blank': b'\n',
            'two keys': PUB1 + PUB1,
            'two keys, one line': f'ssh-ed25519 {K1} ssh-ed25519 {K2}\n'.encode(),
            'a control byte': f'ssh-ed25519 {K1} x'.encode() + b'\x00',
            'rsa': f'ssh-rsa {blob(1, b"ssh-rsa")} x\n'.encode(),
            'type and blob disagree': f'ssh-ed25519 {blob(1, b"ssh-rsa")}\n'.encode(),
            'options in front': f'command="sh" ssh-ed25519 {K1}\n'.encode(),
            'bad base64': b'ssh-ed25519 AAAA!!!!\n',
            'short key': f'ssh-ed25519 {blob(1, size=31)}\n'.encode(),
            'trailing bytes': f'ssh-ed25519 {base64.b64encode(base64.b64decode(K1) + b"x").decode()}\n'.encode(),
            'extra padding': f'ssh-ed25519 {K1}==\n'.encode(),
            'non-ascii': f'ssh-ed25519 {K1} caf\u00e9\n'.encode(),
            'type only': b'ssh-ed25519\n',
            'too long': b'ssh-ed25519 ' + b'A' * 20000 + b'\n',
            'private key': PRIVATE,
        }
        for name, key in bad.items():
            with self.subTest(name):
                code, out = self.run_tool('add', 'desk', stdin=key)
                self.assertEqual(code, REFUSED, out)
                self.assertEqual(self.snap(), before)
                self.assertNotIn('c2VjcmV0LW1hcmtlcg', out)
        self.assertIn('private key', self.run_tool('add', 'desk', stdin=PRIVATE)[1])

    def test_refuses_a_pool_it_does_not_know(self):
        for pool in ('nope', 'vm', 'pc', '../desk', 'desk "x"'):
            with self.subTest(pool):
                code, out = self.run_tool('add', pool, stdin=PUB1)
                self.assertEqual(code, REFUSED, out)
                self.assertFalse(os.path.lexists(self.ssh))
                code, out = self.run_tool('remove', pool)
                self.assertEqual(code, REFUSED, out)
                self.assertEqual(self.run_tool('status', pool)[0], REFUSED)
                self.assertFalse(os.path.lexists(self.ssh))
                if not dk.ff.POOL.fullmatch(pool):          # not even a pool name: refused before anything is read
                    self.assertIn('is not a pool name', out)

    def test_one_line_per_pool_and_one_line_per_key(self):
        self.assertEqual(self.run_tool('add', 'desk', stdin=PUB1)[0], 0)
        before = self.snap()
        code, out = self.run_tool('add', 'desk', stdin=f'ssh-ed25519 {K2}\n'.encode())
        self.assertEqual(code, REFUSED)
        self.assertIn(K1, out)
        self.assertIn(K2, out)
        code, out = self.run_tool('add', 'desk2', stdin=PUB1)   # the same key for a second pool: sshd uses the first
        self.assertEqual((code, self.snap()), (REFUSED, before), out)
        self.put(f'ssh-ed25519 {K2} a plain login line\n'.encode())
        before = self.snap()
        code, out = self.run_tool('add', 'desk', stdin=f'ssh-ed25519 {K2}\n'.encode())
        self.assertEqual((code, self.snap()), (REFUSED, before), out)
        self.assertIn('another line', out)

    def test_remove_and_status_touch_only_their_pool(self):
        self.put(b'ssh-rsa AAAAB3 me\n')
        self.assertEqual(self.run_tool('add', 'desk', stdin=PUB1)[0], 0)
        self.assertEqual(self.run_tool('add', 'desk2', stdin=f'ssh-ed25519 {K2}\n'.encode())[0], 0)
        self.assertEqual(self.run_tool('status')[0], 0)
        self.assertEqual(self.run_tool('remove', 'desk')[0], 0)
        self.assertEqual(self.content(), b'ssh-rsa AAAAB3 me\n' + dk.line_for('desk2', K2).encode() + b'\n')
        self.assertEqual(self.run_tool('status')[0], MISSING)
        self.assertEqual(self.run_tool('status', 'desk2')[0], 0)

    def test_status_finds_wrong_lines(self):
        stale = f'restrict,command="python3 -I /old/fork-fleet-desk.py serve desk" ssh-ed25519 {K1} ' \
                'fork-fleet-desk:desk\n'
        good = dk.line_for('desk', K1) + '\n'
        for name, data in (('old command', stale), ('two lines', good + good), ('no restrict', good[len('restrict,'):]),
                           ('key also elsewhere', good + f'ssh-ed25519 {K1} me\n'),
                           ('no options at all', f'ssh-ed25519 {K1} fork-fleet-desk:desk\n')):
            with self.subTest(name):
                self.put(data.encode())
                code, out = self.run_tool('status', 'desk')
                self.assertEqual(code, PROBLEM, out)
                self.assertIn('wrong', out)
                self.assertEqual(self.run_tool('add', 'desk', stdin=PUB1)[0], REFUSED)
        self.put(f'# x\nssh-ed25519 {K1} fork-fleet-desk:desk\n'.encode())   # an unrestricted line with the mark
        self.assertEqual(self.run_tool('remove', 'desk')[0], OK)
        self.assertEqual(self.content(), b'# x\n')
        self.put('# fork-fleet-desk:desk\n'.encode())   # a comment line is no line
        self.assertEqual(self.run_tool('status', 'desk')[0], MISSING)

    def test_refuses_symlinks(self):
        real = os.path.join(self.tmp, 'real')
        with open(real, 'wb') as f:
            f.write(b'# elsewhere\n')
        os.makedirs(self.ssh, 0o700)
        os.symlink(real, self.keys)
        for args, stdin in ((('add', 'desk'), PUB1), (('remove', 'desk'), b''), (('status', 'desk'), b'')):
            code, out = self.run_tool(*args, stdin=stdin)
            self.assertEqual((code, 'is a symlink' in out), (REFUSED, True), out)
        self.assertTrue(os.path.islink(self.keys))
        os.unlink(self.keys)
        os.rmdir(self.ssh)
        os.mkdir(os.path.join(self.tmp, 'dot-ssh'))
        os.symlink(os.path.join(self.tmp, 'dot-ssh'), self.ssh)
        code, out = self.run_tool('add', 'desk', stdin=PUB1)
        self.assertEqual((code, 'is a symlink' in out), (REFUSED, True), out)
        self.assertEqual(os.listdir(os.path.join(self.tmp, 'dot-ssh')), [])
        with open(real, 'rb') as f:
            self.assertEqual(f.read(), b'# elsewhere\n')

    def test_only_as_the_desk_link_user_and_never_as_root(self):
        self.assertIn('never as root', self.in_process('--user', 'root', 'add', 'desk', stdin=PUB1, euid=0)[1])
        code, out = self.in_process('add', 'desk', stdin=PUB1, euid=os.geteuid())   # default user name
        self.assertEqual((code, dk.USER in out), (REFUSED, True), out)
        self.assertEqual(self.run_tool('--user', ME + 'x', 'status')[0], REFUSED)
        self.assertFalse(os.path.lexists(self.ssh))
        other = next((u for u in pwd.getpwall() if u.pw_uid not in (0, os.geteuid())), None)
        if other is None:
            self.skipTest('no second user')
        for made in (False, True):         # HOME (to make ~/.ssh), then ~/.ssh, of another user: refused
            if made:
                self.put(b'# mine\n')
            code, out = self.in_process('--user', other.pw_name, 'add', 'desk', stdin=PUB1, euid=other.pw_uid)
            self.assertEqual((code, 'does not belong' in out), (REFUSED, True), out)
        self.assertEqual(self.content(), b'# mine\n')

    @contextlib.contextmanager
    def account(self, home=None, groups=(), system_max=-1):
        """What RUN fakes, in process: the password-database home, the groups (names), the system-account limit."""
        real = pwd.getpwuid

        def getpwuid(uid):
            e = real(uid)
            return pwd.struct_passwd((e.pw_name, e.pw_passwd, e.pw_uid, e.pw_gid, e.pw_gecos, home or self.home,
                                      e.pw_shell))
        gids = {90000 + i: name for i, name in enumerate(groups)}
        with mock.patch.object(dk.pwd, 'getpwuid', getpwuid), \
                mock.patch.object(dk.os, 'getgrouplist', return_value=list(gids)), \
                mock.patch.object(dk.grp, 'getgrgid', side_effect=lambda g: mock.Mock(gr_name=gids[g])), \
                mock.patch.object(dk, 'SYSTEM_UID_MAX', system_max):
            yield

    def in_process(self, *argv, stdin=b'', euid=None, **account):
        out = io.StringIO()
        argv = argv if '--user' in argv or euid is not None else ('--user', ME) + argv
        with mock.patch.dict(os.environ, {'HOME': self.home}), redirect_stdout(out), redirect_stderr(out), \
                mock.patch.object(dk.os, 'geteuid', return_value=os.geteuid() if euid is None else euid), \
                self.account(**account):
            return dk.main(['--config', self.config, *argv], io.BytesIO(stdin)), out.getvalue()

    def test_stopped_half_way_leaves_the_old_file(self):
        self.put(b'# old\n', 0o644)
        before = self.snap()
        with mock.patch.object(dk.os, 'replace', side_effect=OSError('cut')):
            self.assertEqual(self.in_process('add', 'desk', stdin=PUB1)[0], ERROR)
        self.assertEqual(self.snap(), before)
        self.assertEqual(sorted(os.listdir(self.ssh)), ['authorized_keys'])
        leftover = os.path.join(self.ssh, dk.TMP + 'x')       # from a run that was killed before its rename
        with open(leftover, 'wb') as f:
            f.write(b'half')
        self.assertEqual(self.run_tool('add', 'desk', stdin=PUB1)[0], 0)
        self.assertFalse(os.path.lexists(leftover))

    def test_file_changed_meanwhile_is_left_alone(self):
        self.put(b'# before\n')
        real_mkstemp = dk.tempfile.mkstemp

        def someone_writes(*a, **kw):
            self.put(b'# written by someone else\n')
            return real_mkstemp(*a, **kw)
        with mock.patch.object(dk.tempfile, 'mkstemp', side_effect=someone_writes):
            code, out = self.in_process('add', 'desk', stdin=PUB1)
        self.assertEqual(code, REFUSED, out)
        self.assertIn('changed while this ran', out)
        self.assertEqual(self.content(), b'# written by someone else\n')
        self.assertEqual(sorted(os.listdir(self.ssh)), ['authorized_keys'])

    @unittest.skipUnless(shutil.which('ssh-keygen'), 'no ssh-keygen')
    def test_openssh_reads_the_line_and_agrees_on_the_fingerprint(self):
        key = os.path.join(self.tmp, 'id')
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'x', '-f', key], check=True)
        with open(key + '.pub', 'rb') as f:
            self.assertEqual(self.run_tool('add', 'desk', stdin=f.read())[0], 0)
        got = subprocess.run(['ssh-keygen', '-l', '-E', 'sha256', '-f', self.keys], capture_output=True, text=True)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn(self.run_tool('status', 'desk')[1].split('(')[1].rstrip(')\n'), got.stdout)

    def without(self, name):
        with open(self.config) as f:
            cfg = json.load(f)
        cfg['pools'] = [p for p in cfg['pools'] if p['name'] != name]
        with open(self.config, 'w') as f:
            json.dump(cfg, f)

    def test_a_retired_pool_line_is_shown_and_removable(self):
        self.put(b'ssh-rsa AAAAB3 me\n')
        self.assertEqual(self.run_tool('add', 'desk', stdin=PUB1)[0], 0)
        self.assertEqual(self.run_tool('add', 'desk2', stdin=f'ssh-ed25519 {K2}\n'.encode())[0], 0)
        self.without('desk')                   # the desktop is retired: its pool leaves the config first
        for args in (('status',), ('status', 'desk')):
            code, out = self.run_tool(*args)
            self.assertEqual(code, PROBLEM, out)
            self.assertIn('key for retired pool desk still allows a login: run desk-key remove desk', out)
        code, out = self.run_tool('add', 'desk3', stdin=PUB1)   # not a desk pool: refused before anything is read
        self.assertEqual(code, REFUSED, out)
        before = self.snap()
        self.assertEqual(self.run_tool('add', 'desk2', stdin=PUB1)[0], REFUSED)   # the key is still on a line
        self.assertEqual(self.snap(), before)
        code, out = self.run_tool('remove', 'desk')
        self.assertEqual(code, OK, out)
        self.assertEqual(self.content(), b'ssh-rsa AAAAB3 me\n' + dk.line_for('desk2', K2).encode() + b'\n')
        self.assertEqual(self.run_tool('status')[0], OK)
        self.assertEqual(self.run_tool('remove', 'desk')[0], REFUSED)   # no line and no pool: a typo, say so
        self.assertEqual(self.run_tool('status', 'desk')[0], REFUSED)

    def test_lines_split_at_newline_only_as_sshd_does(self):
        tail = f'ssh-ed25519 {K2} tail'
        for name, raw, status_code in (
                ('desk line, then a lone CR and a key in its comment',
                 f'{dk.line_for("desk", K1)}\r{tail}\n', PROBLEM),
                ('a login line whose comment runs on into a desk line',
                 f'ssh-rsa AAAAB3 me\r{dk.line_for("desk", K1)}\n', PROBLEM),
                ('the same with a space: one ssh-rsa line to sshd',
                 f'ssh-rsa AAAAB3 me {dk.line_for("desk", K1)}\n', PROBLEM),
                ('a CR inside the options',
                 dk.line_for('desk', K1).replace(',command', '\r,command') + '\n', PROBLEM)):
            with self.subTest(name):
                self.put(raw.encode())
                before = self.snap()
                code, out = self.run_tool('status', 'desk')
                self.assertEqual(code, status_code, out)
                self.assertIn('edit it by hand', out)
                code, out = self.run_tool('remove', 'desk')
                self.assertEqual(code, PROBLEM, out)
                self.assertEqual(self.snap(), before)   # never cut: no tail can become a key line of its own
                self.assertFalse(any(ln.lstrip().startswith(b'ssh-ed25519') for ln in self.content().split(b'\n')))
        self.put(f'# x\r\n{dk.line_for("desk", K1)}\r\nssh-rsa AAAAB3 me\r\n'.encode())   # CRLF: a normal file
        self.assertEqual(self.run_tool('status', 'desk')[0], OK)
        self.assertEqual(self.run_tool('remove', 'desk')[0], OK)
        self.assertEqual(self.content(), b'# x\r\nssh-rsa AAAAB3 me\r\n')

    def test_exit_codes_are_distinct_and_documented(self):
        p = subprocess.run([sys.executable, '-I', TOOL, '--help'], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        for code in (OK, PROBLEM, REFUSED, MISSING, ERROR):
            self.assertRegex(p.stdout, rf'\n  {code}  \w')
        doc = os.path.join(os.path.dirname(HERE), 'fork-fleet-desk-key.md')
        with open(doc, encoding='utf-8') as f:
            text = f.read()
        for code in (OK, PROBLEM, REFUSED, MISSING, ERROR):
            self.assertRegex(text, rf'\n\| {code} \|')
        self.assertEqual((OK, PROBLEM, REFUSED, MISSING, ERROR), (dk.OK, dk.PROBLEM, dk.REFUSED, dk.MISSING, dk.ERROR))
        self.assertEqual(self.run_tool('add', 'desk', stdin=b'junk\n')[0], REFUSED)
        self.assertEqual(self.run_tool('status', 'desk')[0], MISSING)
        self.put(((dk.line_for('desk', K1) + '\n') * 2).encode())
        self.assertEqual(self.run_tool('status', 'desk')[0], PROBLEM)
        self.assertEqual(self.run_tool('--user', ME + 'x', 'status', 'desk')[0], REFUSED)
        self.assertEqual(self.run_tool('frobnicate')[0], REFUSED)     # bad usage
        with mock.patch.object(dk.os, 'fsync', side_effect=OSError(28, 'No space left on device')):
            self.assertEqual(self.in_process('remove', 'desk')[0], ERROR)

    def test_a_second_run_waits_for_the_lock(self):
        self.put(b'# start\n')
        fd = os.open(self.ssh, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)          # another desk-key run holds ~/.ssh
            p = subprocess.Popen(self.cmd('add', 'desk'), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, env=self.env())
            p.stdin.write(PUB1)
            p.stdin.close()
            with self.assertRaises(subprocess.TimeoutExpired, msg='add did not wait for the lock on ~/.ssh'):
                p.wait(timeout=3)
            self.put(b'# start\n# the other run\n')   # what the lock holder writes before it lets go
        finally:
            os.close(fd)
            out = p.stdout.read()
            p.stdout.close()
        self.assertEqual(p.wait(timeout=60), OK, out)
        self.assertEqual(self.content(), b'# start\n# the other run\n' + dk.line_for('desk', K1).encode() + b'\n')

    def test_two_adds_at_once_on_an_empty_home(self):
        real_mkdir = os.mkdir

        def the_other_run_made_it_first(path, mode=0o777):
            real_mkdir(path, 0o700)
            raise FileExistsError(17, 'File exists', path)
        with mock.patch.object(dk.os, 'mkdir', side_effect=the_other_run_made_it_first):
            code, out = self.in_process('add', 'desk', stdin=PUB1)
        self.assertEqual(code, OK, out)
        self.assertEqual(self.content(), dk.line_for('desk', K1).encode() + b'\n')
        os.unlink(self.keys)
        os.rmdir(self.ssh)
        os.symlink(self.tmp, self.ssh)                 # what the other "run" made is still checked: a symlink
        with mock.patch.object(dk.os.path, 'lexists', side_effect=lambda p: p != self.ssh and os.path.exists(p)), \
                mock.patch.object(dk.os, 'mkdir', side_effect=FileExistsError(17, 'File exists')):
            code, out = self.in_process('add', 'desk', stdin=PUB1)
        self.assertEqual((code, 'is a symlink' in out), (REFUSED, True), out)

    def test_the_home_sshd_reads_admins_and_system_accounts(self):
        elsewhere = os.path.join(self.tmp, 'real-home')
        os.mkdir(elsewhere)
        code, out = self.in_process('add', 'desk', stdin=PUB1, home=elsewhere)   # $HOME is not the user's home
        self.assertEqual((code, 'sshd reads' in out), (REFUSED, True), out)
        for group in ('sudo', 'wheel', 'adm', 'docker'):
            code, out = self.in_process('add', 'desk', stdin=PUB1, groups=('users', group))
            self.assertEqual((code, 'admin group' in out), (REFUSED, True), out)
        code, out = self.in_process('add', 'desk', stdin=PUB1, system_max=10 ** 9)   # a system uid, not USER
        self.assertEqual((code, 'system account' in out), (REFUSED, True), out)
        self.assertFalse(os.path.lexists(self.ssh))
        with mock.patch.object(dk, 'USER', ME):        # the default name may be a system account
            code, out = self.in_process('add', 'desk', stdin=PUB1, system_max=10 ** 9, groups=('users',))
        self.assertEqual(code, OK, out)

    def test_unsafe_modes_and_leftovers(self):
        self.put(b'# x\n', 0o620)
        self.assertEqual(self.run_tool('add', 'desk', stdin=PUB1)[0], REFUSED)   # sshd would not read it
        os.chmod(self.keys, 0o600)
        os.chmod(self.ssh, 0o770)
        self.assertEqual(self.run_tool('add', 'desk', stdin=PUB1)[0], REFUSED)
        os.chmod(self.ssh, 0o700)
        self.assertEqual(self.content(), b'# x\n')
        junk = os.path.join(self.ssh, dk.TMP + 'made-by-hand')
        os.mkdir(junk)                                 # not this tool's: skipped, never in the way
        stale = os.path.join(self.ssh, dk.TMP + 'stale')
        with open(stale, 'wb') as f:
            f.write(b'half')
        os.utime(stale, (1, 1))                        # from a run long gone
        self.assertEqual(self.run_tool('add', 'desk', stdin=PUB1)[0], OK)
        self.assertEqual((os.path.isdir(junk), os.path.lexists(stale)), (True, False))

    def test_serve_takes_the_forced_command_of_every_pool_name(self):
        """The contract with the desk link (row D2): its program is DESK's file and `serve` takes SERVE POOL."""
        desk = os.path.join(HERE, os.path.basename(dk.DESK))
        if not os.path.exists(desk):
            mine = {os.path.basename(TOOL), os.path.basename(__file__)}
            d2 = [os.path.basename(p) for p in glob.glob(os.path.join(HERE, 'fork-fleet-desk*'))
                  if os.path.basename(p) not in mine]
            runbook = os.path.join(os.path.dirname(HERE), 'fork-fleet.md')
            with open(runbook, encoding='utf-8') as f:
                d2 += ['fork-fleet.md'] * ('# The desk link' in f.read())
            self.assertEqual(d2, [], f'the desk link (row D2) is here, but {os.path.basename(dk.DESK)} is not: '
                                     'point DESK and SERVE at its program')
            self.skipTest('the desk link (row D2) is not here yet')
        with open(desk, encoding='utf-8') as f:
            self.assertIn(f'{dk.DESK} serve POOL', ' '.join(f.read().split()), 'its usage names another path')
        argv = shlex.split(dk.line_for('desk', K1).split('"')[1])
        self.assertEqual(argv[:4], [dk.PYTHON, '-I', dk.DESK, 'serve'])
        run = os.path.join(self.tmp, 'run')
        os.mkdir(run, 0o700)

        def serve(*args):
            p = subprocess.run([sys.executable, '-I', desk, *args], stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=60, env={**self.env(), 'FORK_FLEET_DESK_DIR': run})
            return p.returncode, re.sub(rf'(?<![\w-]){re.escape(args[-1])}(?![\w-])', 'POOL', p.stderr)
        bad_name, bad_usage = serve('serve', 'Not a pool!'), serve('serve', 'desk', 'x')
        self.assertNotEqual(bad_name[0], 0)
        self.assertNotEqual(bad_usage[0], 0)
        for pool in ('desk', 'q', 'q' * 40, 'pc-2_b'):   # the fleet's pool names, at their edges
            self.assertTrue(dk.ff.POOL.fullmatch(pool))
            got = serve(*argv[3:4], pool)
            self.assertNotIn(got, (bad_name, bad_usage), f'serve refuses the forced command for pool {pool}')

if __name__ == '__main__':
    unittest.main()
