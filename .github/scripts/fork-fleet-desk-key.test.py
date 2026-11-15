#!/usr/bin/env python3
"""Tests of fork-fleet-desk-key.py: a temporary HOME and fleet config only. Run: python3 <this>. POSIX."""
import base64, importlib.util, io, json, os, pwd, shutil, struct, subprocess, sys, tempfile, unittest
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

    def run_tool(self, *args, stdin=b''):
        env = {'HOME': self.home, 'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LC_ALL': 'C'}
        p = subprocess.run([sys.executable, '-I', TOOL, '--config', self.config, '--user', ME, *args], input=stdin,
                           env=env, capture_output=True, timeout=60)
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
        line = f'restrict,command="python3 -I /opt/fork-fleet/fork-fleet-desk.py serve desk" ssh-ed25519 {K1} ' \
               f'fork-fleet-desk:desk\n'
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
        self.assertEqual(self.run_tool('status', 'desk')[0], 3)

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
                self.assertEqual(code, 1, out)
                self.assertEqual(self.snap(), before)
                self.assertNotIn('c2VjcmV0LW1hcmtlcg', out)
        self.assertIn('private key', self.run_tool('add', 'desk', stdin=PRIVATE)[1])

    def test_refuses_a_pool_it_does_not_know(self):
        for pool in ('nope', 'vm', 'pc', '../desk', 'desk "x"'):
            with self.subTest(pool):
                code, out = self.run_tool('add', pool, stdin=PUB1)
                self.assertEqual(code, 1, out)
                self.assertFalse(os.path.lexists(self.ssh))
                self.assertEqual(self.run_tool('remove', pool)[0], 1)
                self.assertEqual(self.run_tool('status', pool)[0], 1)

    def test_one_line_per_pool_and_one_line_per_key(self):
        self.assertEqual(self.run_tool('add', 'desk', stdin=PUB1)[0], 0)
        before = self.snap()
        code, out = self.run_tool('add', 'desk', stdin=f'ssh-ed25519 {K2}\n'.encode())
        self.assertEqual(code, 1)
        self.assertIn(K1, out)
        self.assertIn(K2, out)
        code, out = self.run_tool('add', 'desk2', stdin=PUB1)   # the same key for a second pool: sshd uses the first
        self.assertEqual((code, self.snap()), (1, before), out)
        self.put(f'ssh-ed25519 {K2} a plain login line\n'.encode())
        before = self.snap()
        code, out = self.run_tool('add', 'desk', stdin=f'ssh-ed25519 {K2}\n'.encode())
        self.assertEqual((code, self.snap()), (1, before), out)
        self.assertIn('another line', out)

    def test_remove_and_status_touch_only_their_pool(self):
        self.put(b'ssh-rsa AAAAB3 me\n')
        self.assertEqual(self.run_tool('add', 'desk', stdin=PUB1)[0], 0)
        self.assertEqual(self.run_tool('add', 'desk2', stdin=f'ssh-ed25519 {K2}\n'.encode())[0], 0)
        self.assertEqual(self.run_tool('status')[0], 0)
        self.assertEqual(self.run_tool('remove', 'desk')[0], 0)
        self.assertEqual(self.content(), b'ssh-rsa AAAAB3 me\n' + dk.line_for('desk2', K2).encode() + b'\n')
        self.assertEqual(self.run_tool('status')[0], 3)
        self.assertEqual(self.run_tool('status', 'desk2')[0], 0)

    def test_status_finds_wrong_lines(self):
        stale = f'restrict,command="python3 -I /old/fork-fleet-desk.py serve desk" ssh-ed25519 {K1} ' \
                'fork-fleet-desk:desk\n'
        good = dk.line_for('desk', K1) + '\n'
        for name, data in (('old command', stale), ('two lines', good + good), ('no restrict', good[len('restrict,'):]),
                           ('key also elsewhere', good + f'ssh-ed25519 {K1} me\n')):
            with self.subTest(name):
                self.put(data.encode())
                code, out = self.run_tool('status', 'desk')
                self.assertEqual(code, 1, out)
                self.assertIn('wrong', out)
                self.assertEqual(self.run_tool('add', 'desk', stdin=PUB1)[0], 1)
        self.put('# fork-fleet-desk:desk\n'.encode())   # a comment line is no line
        self.assertEqual(self.run_tool('status', 'desk')[0], 3)

    def test_refuses_symlinks(self):
        real = os.path.join(self.tmp, 'real')
        with open(real, 'wb') as f:
            f.write(b'# elsewhere\n')
        os.makedirs(self.ssh)
        os.symlink(real, self.keys)
        for args, stdin in ((('add', 'desk'), PUB1), (('remove', 'desk'), b''), (('status', 'desk'), b'')):
            code, out = self.run_tool(*args, stdin=stdin)
            self.assertEqual((code, 'is a symlink' in out), (1, True), out)
        self.assertTrue(os.path.islink(self.keys))
        os.unlink(self.keys)
        os.rmdir(self.ssh)
        os.mkdir(os.path.join(self.tmp, 'dot-ssh'))
        os.symlink(os.path.join(self.tmp, 'dot-ssh'), self.ssh)
        code, out = self.run_tool('add', 'desk', stdin=PUB1)
        self.assertEqual((code, 'is a symlink' in out), (1, True), out)
        self.assertEqual(os.listdir(os.path.join(self.tmp, 'dot-ssh')), [])
        with open(real, 'rb') as f:
            self.assertEqual(f.read(), b'# elsewhere\n')

    def test_only_as_the_desk_link_user_and_never_as_root(self):
        self.assertIn('never as root', self.in_process('--user', 'root', 'add', 'desk', stdin=PUB1, euid=0)[1])
        code, out = self.in_process('add', 'desk', stdin=PUB1, euid=os.geteuid())   # default user name
        self.assertEqual((code, dk.USER in out), (1, True), out)
        self.assertEqual(self.run_tool('--user', ME + 'x', 'status')[0], 1)
        self.assertFalse(os.path.lexists(self.ssh))
        other = next((u for u in pwd.getpwall() if u.pw_uid not in (0, os.geteuid())), None)
        if other is None:
            self.skipTest('no second user')
        for made in (False, True):         # HOME (to make ~/.ssh), then ~/.ssh, of another user: refused
            if made:
                self.put(b'# mine\n')
            code, out = self.in_process('--user', other.pw_name, 'add', 'desk', stdin=PUB1, euid=other.pw_uid)
            self.assertEqual((code, 'does not belong' in out), (1, True), out)
        self.assertEqual(self.content(), b'# mine\n')

    def in_process(self, *argv, stdin=b'', euid=None):
        out = io.StringIO()
        argv = argv if '--user' in argv or euid is not None else ('--user', ME) + argv
        with mock.patch.dict(os.environ, {'HOME': self.home}), redirect_stdout(out), redirect_stderr(out), \
                mock.patch.object(dk.os, 'geteuid', return_value=os.geteuid() if euid is None else euid):
            return dk.main(['--config', self.config, *argv], io.BytesIO(stdin)), out.getvalue()

    def test_stopped_half_way_leaves_the_old_file(self):
        self.put(b'# old\n', 0o644)
        before = self.snap()
        with mock.patch.object(dk.os, 'replace', side_effect=OSError('cut')):
            self.assertEqual(self.in_process('add', 'desk', stdin=PUB1)[0], 1)
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
        self.assertEqual(code, 1, out)
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

    def test_serve_command_matches_the_desk_link(self):
        serve = os.path.join(HERE, os.path.basename(dk.DESK))
        if not os.path.exists(serve):
            self.skipTest('the desk link (row D2) is not here yet')
        with open(serve, encoding='utf-8') as f:
            self.assertIn(f'command="{dk.SERVE} POOL"', ' '.join(f.read().split()))


if __name__ == '__main__':
    unittest.main()
