"""update-policy idle: what makes the machine busy, on a fake host (cgroups, /proc, heavy tickets, a fake Docker socket,
busctl's and the container tools' answers) in a temporary folder. No root, no systemd, no Docker. RedCheck breaks each
rule in a copy of the code and shows its test then fails.

  python3 -I -B fork-kit/update-policy/tests/idle.test.py
"""
import fcntl, json, os, shutil, socket, sys, tempfile, threading, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixture as fx  # noqa: E402
from fixture import idle  # noqa: E402

MANAGER = 'user-1000.slice/user@1000.service'
SCOPED = f'/user.slice/{MANAGER}/app.slice/t3code.service'


class Look(fx.HostCase):
    def look(self, **over):
        return idle.look(self.host, fx.make_settings(**over))

    def unreadable(self, path):
        if os.geteuid() == 0:
            self.skipTest('root reads every folder')
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o755)

    def test_an_empty_machine_is_idle(self):
        self.assertEqual(self.look(), [])
        self.cgroup(f'{MANAGER}/app.slice/app-t3code.service')      # the T3 server itself is no agent
        self.cgroup(f'{MANAGER}/session.slice/dbus.service')
        self.process(1, '/sbin/init', ppid=0)
        self.assertEqual(self.look(), [])

    def test_no_cgroup_v2_cannot_tell(self):
        os.unlink(self.root + '/sys/fs/cgroup/cgroup.controllers')       # v1 or hybrid: nothing at the top
        self.cgroup(f'{MANAGER}/app.slice/app-t3code.slice/t3code-codex-1.scope', populated=False)
        self.assertEqual(self.look(), ['cannot tell: no cgroup v2 at /sys/fs/cgroup (agents and heavy jobs cannot '
                                       'be counted)'])

    def test_a_running_agent_scope_is_busy_an_empty_one_is_not(self):
        self.cgroup(f'{MANAGER}/app.slice/app-t3code.slice/app-t3code-agents-0123456789ab.slice/'
                    't3code-codex-1a2b3c4d.scope', populated=False)
        self.assertEqual(self.look(), [])
        self.cgroup(f'{MANAGER}/app.slice/app-t3code.slice/app-t3code-agents-0123456789ab.slice/'
                    't3code-claude-5e6f7a8b.scope')
        busy = self.look()
        self.assertEqual(len(busy), 1, busy)
        self.assertIn('user 1000: 1 T3 agent scope(s) running (t3code-claude-5e6f7a8b.scope)', busy[0])

    def test_any_scope_below_an_app_t3code_slice_and_a_t3code_scope_anywhere_count(self):
        self.cgroup('user-1001.slice/user@1001.service/app.slice/app-t3code-agents.slice/run-u7.scope')
        self.cgroup('user-1002.slice/user@1002.service/other.slice/t3code-pi-00000000.scope')
        busy = self.look()
        self.assertEqual([b.split(':')[0] for b in busy], ['user 1001', 'user 1002'], busy)

    def test_an_unreadable_scope_counts_as_running(self):
        os.makedirs(self.root + f'/sys/fs/cgroup/user.slice/{MANAGER}/app.slice/app-t3code.slice/t3code-x-1.scope')
        self.assertIn('T3 agent scope', self.look()[0])

    def test_a_cgroup_folder_it_cannot_list_cannot_tell(self):
        self.cgroup(f'{MANAGER}/app.slice/app-t3code.slice/t3code-x-1.scope', populated=False)
        self.unreadable(self.root + f'/sys/fs/cgroup/user.slice/{MANAGER}/app.slice')
        busy = self.look()
        self.assertEqual(len(busy), 1, busy)
        self.assertTrue(busy[0].startswith('cannot tell: '), busy)
        self.assertIn('app.slice cannot be listed', busy[0])

    def t3_line(self, **over):
        busy = [b for b in self.look(**over) if b.startswith('T3 runs without agent scopes')]
        return busy[0] if busy else ''

    def package(self, folder, name='t3'):
        self.write(f'{folder}/package.json', json.dumps({'name': name, 'type': 'module'}))

    def test_t3_without_agent_scopes_is_busy_whatever_it_runs(self):
        """T3 turns scopes off when it runs without a user manager (a system service): its agents are its plain
        children and nothing below an app-t3code slice shows them. Then T3 running at all is busy (an idle server
        too: such a machine rarely restarts by itself, on purpose)."""
        self.process(500, '/opt/t3/runtime/versions/0.1.12/t3', 'serve', uid=1000, comm='node-MainThread',
                     cgroup='/system.slice/t3code-agents.service')
        self.assertEqual(self.look(), ['T3 runs without agent scopes (user 1000): 1 process(es): 500 node-MainThread: '
                                       'the t3 command'])
        self.process(502, '/home/x/.local/bin/claude', '--output-format', 'stream-json', ppid=500, uid=1000)
        self.assertIn('502 claude: an agent CLI below T3 server 500', self.t3_line())

    def test_every_launch_form_of_a_t3_server(self):
        """Both round-2 lenses: the fork's documented `node apps/server/dist/bin.mjs`, npm start (`node
        dist/bin.mjs` in apps/server), npm's install, the desktop app's Electron running as node, relative dev runs,
        and the t3 binary known by its exe only."""
        self.package('/w/t3code/apps/server')
        self.package('/usr/lib/node_modules/t3')
        self.package('/w/other', name='not-t3')
        forms = [
            (('/usr/bin/node', 'apps/server/dist/bin.mjs'), '/w/t3code', 'apps/server/dist/bin.mjs'),
            (('node', 'dist/bin.mjs'), '/w/t3code/apps/server', 'apps/server/dist/bin.mjs'),
            (('/usr/bin/node', '/usr/lib/node_modules/t3/dist/bin.mjs', 'serve'), '/', 'node_modules/t3/dist/bin.mjs'),
            (('/opt/T3 Code/t3code', '--require', '/home/x/.cache/t3/compile-cache.js',
              '/opt/T3 Code/resources/app.asar/apps/server/dist/bin.mjs', '--bootstrap-fd', '3'), '/',
             'app.asar/apps/server/dist/bin.mjs'),
            (('bun', 'run', 'src/bin.ts'), '/w/t3code/apps/server', 'apps/server/src/bin.ts'),
            (('/usr/bin/node', '--import', 'tsx', 'apps/server/src/bin.ts'), '/w/t3code', 'apps/server/src/bin.ts'),
            (('node', 'scripts/dev.ts'), '/w/t3code/apps/server', 'apps/server/scripts/dev.ts'),
            (('npm start',), '/w/t3code/apps/server', 'a package runner starting T3'),
            (('/usr/bin/node', '/usr/bin/pnpm', 'start'), '/w/t3code/apps/server', 'a package runner starting T3'),
        ]
        for argv, cwd, words in forms:
            with self.subTest(argv=argv):
                shutil.rmtree(self.root + '/proc')
                self.process(800, *argv, uid=1000, cwd=cwd, cgroup='/user.slice/user-1000.slice/session-3.scope')
                self.assertIn(words, self.t3_line())
        shutil.rmtree(self.root + '/proc')
        self.process(801, '/usr/lib/t3/node-runtime', 'x', uid=1000, comm='node-MainThread', exe='/opt/t3/bin/t3')
        self.assertIn('801 node-MainThread: the t3 command', self.t3_line())
        for argv, cwd in ((('node', 'dist/bin.mjs'), '/w/other'), (('npm', 'start'), '/w/other'),
                          (('/usr/bin/node', '/srv/app/server.mjs'), '/')):
            with self.subTest(not_t3=argv):
                shutil.rmtree(self.root + '/proc')
                self.process(802, *argv, uid=1000, cwd=cwd)
                self.assertEqual(self.look(), [])

    def test_t3_environment_markers_count_without_a_known_server(self):
        self.process(900, '/usr/local/bin/some-wrapper', uid=1000)
        self.process(901, '/home/x/.local/bin/claude', '-p', ppid=900, uid=1000,
                     env=('T3CODE_HOME=/home/x/.t3', 'T3CODE_THREAD_ID=thread-1'))
        self.assertEqual(self.look(), ["T3 runs without agent scopes (user 1000): 1 process(es): 901 claude: T3's "
                                       'environment'])
        self.process(901, '/home/x/.local/bin/claude', '-p', ppid=900, uid=1000, env=('NOT_T3CODE_HOME=x',))
        self.assertEqual(self.look(), [])

    def test_with_scopes_on_t3_is_counted_by_its_scopes(self):
        self.process(600, '/usr/bin/node', '/usr/local/bin/t3', 'serve', uid=1000, cgroup=SCOPED,
                     env=('T3CODE_PORT=3773',))
        self.process(601, '/usr/bin/claude', uid=1000, ppid=600, env=('T3CODE_THREAD_ID=t1',),
                     cgroup=f'/user.slice/{MANAGER}/app.slice/app-t3code.slice/app-t3code-agents.slice/'
                            't3code-claude-1.scope')
        self.assertIn('T3 runs without agent scopes (user 1000): 2 process(es)', self.t3_line())   # no slice yet
        self.cgroup(f'{MANAGER}/app.slice/app-t3code.slice/app-t3code-agents.slice', populated=False)
        self.assertEqual(self.look(), [])                # scopes on: its agents are counted as scopes
        self.process(600, '/usr/bin/node', '/usr/local/bin/t3', 'serve', uid=1000,
                     cgroup='/system.slice/t3.service')  # a server outside its manager has no scopes of its own
        self.assertIn('600 node: the t3 command', self.t3_line())

    def test_what_it_cannot_read_about_a_process_cannot_tell(self):
        self.process(700, 'node', 'dist/bin.mjs', uid=1000)
        os.unlink(self.root + '/proc/700/environ')
        os.makedirs(self.root + '/proc/700/environ')              # EISDIR, not "gone"
        self.assertEqual(self.look(), ['cannot tell: process 700 cannot be read (Is a directory)'])
        os.rmdir(self.root + '/proc/700/environ')
        self.process(700, 'node', 'dist/bin.mjs', uid=1000)
        os.symlink('/w', self.root + '/proc/700/cwd')
        real = os.readlink

        def no_cwd(path, *a, **k):
            if path.endswith('/proc/700/cwd'):
                raise PermissionError(13, 'Permission denied')
            return real(path, *a, **k)
        with mock.patch.object(idle.os, 'readlink', no_cwd):
            self.assertEqual(self.look(), ['cannot tell: the folder of process 700 cannot be read (Permission '
                                           'denied)'])

    def test_proc_it_cannot_list_or_read_cannot_tell(self):
        self.process(9, '/usr/bin/sleep', '100')
        os.unlink(self.root + '/proc/9/status')
        os.makedirs(self.root + '/proc/9/status')                     # read fails with EISDIR, not "gone"
        self.assertEqual(self.look(), ['cannot tell: process 9 cannot be read (Is a directory)'])
        shutil.rmtree(self.root + '/proc')
        self.assertEqual(self.look(), ['cannot tell: /proc cannot be listed (No such file or directory)'])

    def test_heavy_jobs_running_and_waiting(self):
        self.cgroup(f'{MANAGER}/heavy.slice/heavy-gradle-ab12.scope', populated=False)
        self.assertEqual(self.look(), [])
        self.cgroup(f'{MANAGER}/heavy.slice/heavy-gradle-cd34.scope')
        self.assertIn('user 1000: heavy jobs (1 running, 0 live ticket(s))', self.look())
        self.cgroup(f'{MANAGER}/ci.slice/ci-heavy.slice/heavy-x-1.scope')    # another slice: heavySlice names it
        self.assertEqual(self.look(heavySlice='ci-heavy.slice'),
                         ['user 1000: heavy jobs (1 running, 0 live ticket(s))'])
        self.assertEqual(self.look(heavySlice='other.slice'), [])

    def test_a_live_heavy_ticket_is_busy_a_dead_one_is_not(self):
        os.makedirs(self.root + f'/sys/fs/cgroup/user.slice/{MANAGER}')
        path = self.write('/run/user/1000/heavy-queue/heavy.slice/t-1700000000000-42.json',
                          '{"kind": "build", "state": "waiting"}')
        self.write('/run/user/1000/heavy-queue/heavy.slice/notes.txt', 'not a ticket')
        self.assertEqual(self.look(), [])                    # nobody holds it: that heavy is gone
        held = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, held)
        fcntl.flock(held, fcntl.LOCK_EX)
        self.assertEqual(self.look(), ['user 1000: heavy jobs (0 running, 1 live ticket(s))'])

    def test_tickets_it_cannot_read_cannot_tell_and_a_link_is_not_followed(self):
        os.makedirs(self.root + f'/sys/fs/cgroup/user.slice/{MANAGER}')
        elsewhere = tempfile.mkdtemp(dir=self.root)
        path = os.path.join(elsewhere, 't-1-1.json')
        with open(path, 'w') as f:
            f.write('{}')
        os.makedirs(self.root + '/run/user/1000/heavy-queue')
        os.symlink(elsewhere, self.root + '/run/user/1000/heavy-queue/heavy.slice')
        held = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, held)
        fcntl.flock(held, fcntl.LOCK_EX)
        busy = self.look()
        self.assertEqual(len(busy), 1, busy)
        self.assertIn('cannot tell: heavy\'s tickets in /run/user/1000/heavy-queue/heavy.slice cannot be read',
                      busy[0])
        self.assertNotIn('live ticket', busy[0])
        os.unlink(self.root + '/run/user/1000/heavy-queue/heavy.slice')
        os.makedirs(self.root + '/run/user/1000/heavy-queue/heavy.slice')
        self.unreadable(self.root + '/run/user/1000/heavy-queue/heavy.slice')
        self.assertIn('cannot tell: heavy\'s tickets', self.look()[0])

    def test_docker_is_asked_only_while_its_daemon_runs(self):
        """A socket-activated docker.sock starts dockerd (and containers with restart: always) when asked."""
        uid = 4242                                       # a made-up user: nothing of the host's own
        self.host.users['agent'] = fx.fake_user('agent', uid)
        self.host.dockers = {'/run/docker.sock': ['dev-db', 'mutation-run'],
                             f'/run/user/{uid}/docker.sock': ['pg-test']}
        self.assertEqual(self.look(containerUsers=['agent']), [])     # no dockerd: never asked
        self.process(40, '/usr/bin/dockerd', '-H', 'fd://', uid=0)
        self.process(41, '/usr/bin/dockerd', uid=uid)
        busy = self.look(containerUsers=['agent'], ignoreContainers=['dev-*'])
        self.assertEqual(busy, ['system Docker: 1 running (mutation-run)', 'agent\'s Docker: 1 running (pg-test)'])
        self.assertEqual(self.look(systemDocker=False, containerUsers=['agent'], ignoreContainers=['*']), [])

    def test_docker_it_cannot_ask_and_a_user_who_is_not_there_are_busy(self):
        self.process(40, '/usr/bin/dockerd', uid=0)
        self.host.dockers = {'/run/docker.sock': 'cannot ask Docker at /run/docker.sock (Permission denied)'}
        self.assertEqual(self.look(),
                         ['cannot tell: system Docker: cannot ask Docker at /run/docker.sock (Permission denied)'])
        self.assertEqual(self.look(containerUsers=['nobody-here']),
                         ['cannot tell: containerUsers names nobody-here, who is not a user here'])

    def test_podman_lxc_incus_lxd_and_libvirt(self):
        self.process(50, '/usr/bin/conmon', '--api-version', '1', '-c', 'abc', '-n', 'pg-rootless', uid=1000)
        self.process(51, '/usr/bin/conmon', '--name=build-root', uid=0)
        self.process(52, '/usr/bin/incusd', '--group', 'incus-admin')
        self.process(53, '/usr/lib/libvirt/virtqemud', '--timeout', '120')
        self.host.answers['incus list'] = (0, 'web,RUNNING\nold,STOPPED\n', '')
        self.host.answers['lxc-ls'] = (0, 'c1\n', '')
        self.host.answers['virsh'] = (0, 'win11\n\n', '')
        self.assertEqual(self.look(ignoreContainers=['build-*']),
                         ['Podman/CRI-O: 1 running (pg-rootless)', 'incus: 1 running (web)', 'LXC: 1 running (c1)',
                          'libvirt: 1 running (win11)'])
        self.host.answers['virsh'] = (1, '', 'failed to connect to the hypervisor')
        self.assertIn('cannot tell: libvirt: virsh: failed to connect to the hypervisor', self.look())
        del self.host.answers['lxc-ls']                       # not installed: nothing to ask
        self.assertNotIn('LXC', ' '.join(self.look()))

    def test_block_locks_on_shutdown_or_sleep_count_delay_and_other_locks_do_not(self):
        rows = [['shutdown', 'Unattended Upgrades Shutdown', 'Stop ongoing upgrades', 'delay', 0, 221],
                ['handle-lid-switch', 'GNOME', 'lid', 'block', 1000, 5],
                ['shutdown:sleep:idle', 'mutation-tests', 'hours left', 'block', 1000, 4242],
                ['sleep', 'backup', 'copying', 'block', 0, 77]]
        self.host.answers['busctl'] = (0, json.dumps({'type': 'a(ssssuu)', 'data': [rows]}), '')
        self.assertEqual(self.look(), ['inhibitor lock: mutation-tests (uid 1000): hours left; backup (uid 0): copying'])
        self.host.answers['busctl'] = (0, json.dumps({'type': 'a(ssssuu)', 'data': [rows[:2]]}), '')
        self.assertEqual(self.look(), [])

    def test_inhibitors_it_cannot_read_are_busy_busctl_missing_too(self):
        self.host.answers['busctl'] = (1, '', 'Failed to connect to bus')
        self.assertEqual(self.look(), ['cannot tell: the inhibitor locks cannot be read (Failed to connect to bus)'])
        self.host.answers['busctl'] = (0, 'not json', '')
        self.assertIn('broken answer', self.look()[0])
        del self.host.answers['busctl']
        self.assertEqual(self.look(), ['cannot tell: the inhibitor locks cannot be read (busctl: not installed)'])

    def test_text_from_users_cannot_forge_a_journal_line(self):
        rows = [['shutdown', 'x\n<0>update-policy: restarting now', 'why\r\n<3>fake\x1b[2J', 'block', 1000, 1]]
        self.host.answers['busctl'] = (0, json.dumps({'type': 'a(ssssuu)', 'data': [rows]}), '')
        self.process(51, '/usr/bin/conmon', '-n', 'evil\n<2>name', uid=1000)
        self.host.free = {'/usr/local/lib/busy/game': ('busy', 'a\nb')}
        busy = self.look(busyScripts=['/usr/local/lib/busy/game'])
        self.assertEqual(len(busy), 3, busy)
        for line in busy:
            self.assertEqual(line, line.replace('\n', '').replace('\r', '').replace('\x1b', ''))
        self.assertIn('x\\x0a<0>update-policy: restarting now (uid 1000): why\\x0d\\x0a<3>fake\\x1b[2J', busy[1])

    def test_a_package_manager_at_work_is_busy_the_shutdown_helper_is_not(self):
        self.process(221, '/usr/bin/python3', '/usr/share/unattended-upgrades/unattended-upgrade-shutdown',
                     '--wait-for-signal')
        self.process(300, '/usr/bin/sleep', '100')
        self.assertEqual(self.look(), [])
        self.process(512, '/usr/bin/python3', '-u', '/usr/bin/unattended-upgrade')
        self.process(513, '/usr/bin/dpkg', '--configure', '-a')
        self.assertEqual(self.look(), ['package manager at work: unattended-upgrade (512), dpkg (513)'])

    def test_busy_scripts_anything_but_free_is_busy(self):
        self.host.free = {'/usr/local/lib/busy/game': ('busy', 'a game is running'),
                          '/usr/local/lib/busy/broken': ('unavailable', 'exit 2')}
        self.assertEqual(self.look(busyScripts=['/usr/local/lib/busy/free']), [])
        self.assertEqual(self.look(busyScripts=['/usr/local/lib/busy/game', '/usr/local/lib/busy/broken']),
                         ['busy script /usr/local/lib/busy/game: busy (a game is running)',
                          'busy script /usr/local/lib/busy/broken: unavailable (exit 2)'])

    def test_user_slice_it_cannot_list_is_busy(self):
        os.makedirs(self.root + '/sys/fs/cgroup/user.slice')
        self.unreadable(self.root + '/sys/fs/cgroup/user.slice')
        self.assertIn('cannot tell', self.look()[0])


class Docker(unittest.TestCase):
    """idle.docker_running against a fake Docker Engine API on a unix socket."""

    def serve(self, answer):
        folder = tempfile.mkdtemp(prefix='update-policy-docker-')
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        path = os.path.join(folder, 'docker.sock')
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        self.requests = []

        def one():
            conn, _ = srv.accept()
            with conn:
                self.requests.append(conn.recv(65536))
                conn.sendall(answer)
            srv.close()
        threading.Thread(target=one, daemon=True).start()
        return path

    def test_running_names(self):
        body = json.dumps([{'Id': 'abc', 'Names': ['/mutation-run'], 'State': 'running'},
                           {'Id': 'def1234567890', 'Names': [], 'State': 'running'}]).encode()
        path = self.serve(b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n' + body)
        self.assertEqual(idle.docker_running(path), ['mutation-run', 'def123456789'])
        self.assertTrue(self.requests[0].startswith(b'GET /containers/json HTTP/1.0\r\n'))

    def test_chunked_answer(self):
        body = b'[{"Names": ["/a"]}]'
        chunked = b'%x\r\n%s\r\n0\r\n\r\n' % (len(body), body)
        path = self.serve(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n' + chunked)
        self.assertEqual(idle.docker_running(path), ['a'])

    def test_errors_are_reasons_not_names_nor_crashes(self):
        path = self.serve(b'HTTP/1.0 500 Internal Server Error\r\n\r\n{}')
        self.assertIn('answered', idle.docker_running(path))
        path = self.serve(b'HTTP/1.0 200 OK\r\n\r\nnot json')
        self.assertIn('broken answer', idle.docker_running(path))
        path = self.serve(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\nx\r\n')
        self.assertIn('broken answer', idle.docker_running(path))
        self.assertIn('cannot ask Docker', idle.docker_running('/nonexistent/docker.sock'))

    def test_another_users_docker_is_never_asked_as_root_in_process(self):
        h = idle.Host()
        h.ROOT = tempfile.mkdtemp(prefix='update-policy-host-')
        self.addCleanup(shutil.rmtree, h.ROOT, ignore_errors=True)
        other = type('pw', (), {'pw_uid': 4242, 'pw_gid': 4242, 'pw_name': 'someone'})()
        if os.geteuid() == 0:
            self.skipTest('as root it would run the helper as that user')
        self.assertEqual(h.docker_names('/run/user/4242/docker.sock', other),
                         "cannot ask someone's Docker (run as root)")


MUTATIONS = {
    'no cgroup v2: cannot tell': (
        'idle', "    if not os.path.exists(host.p(f'{host.CGROUP}/cgroup.controllers')):", "    if False:",
        'Look.test_no_cgroup_v2_cannot_tell'),
    'T3 without scopes is busy': (
        'idle', "        if why:\n            found.setdefault", "        if False:\n            found.setdefault",
        'Look.test_t3_without_agent_scopes_is_busy_whatever_it_runs'),
    'scopes on only under its own manager with a slice': (
        'idle', "        if p.cgroup.startswith(f'/user.slice/user-{p.uid}.slice/user@{p.uid}.service/') and sliced.get(p.uid):",
        "        if sliced.get(p.uid):", 'Look.test_with_scopes_on_t3_is_counted_by_its_scopes'),
    'any argument is an entry, whatever argv[0]': (
        'idle', "    for a in p.argv[1:] or p.argv:", "    for a in (p.argv[1:] if len(names) > 1 else []):",
        'Look.test_every_launch_form_of_a_t3_server'),
    'relative entries are resolved against the folder': (
        'idle', "            a = os.path.normpath(os.path.join(cwd, a))", "            pass",
        'Look.test_every_launch_form_of_a_t3_server'),
    'the t3 binary by its exe': (
        'idle', " or os.path.basename(p.exe) in T3_NAMES", "", 'Look.test_every_launch_form_of_a_t3_server'),
    'only a package named t3': (
        'idle', "if a.endswith(entry) and _package_name(host, a[:-len(entry)]) == 't3':", "if a.endswith(entry):",
        'Look.test_every_launch_form_of_a_t3_server'),
    'package runners starting t3': (
        'idle', "            return 'a package runner starting T3'", "            return ''",
        'Look.test_every_launch_form_of_a_t3_server'),
    "T3's environment markers": (
        'idle', "        why = form or ('T3\\'s environment' if p.marked else '')", "        why = form",
        'Look.test_t3_environment_markers_count_without_a_known_server'),
    'a folder it cannot read is no idle': (
        'idle', "                    raise CannotTell(f'the folder of process {p.pid} cannot be read ({e.strerror})') from None",
        "                    continue", 'Look.test_what_it_cannot_read_about_a_process_cannot_tell'),
    'an agent scope is busy': (
        'idle', "        if agents:\n", "        if False:\n",
        'Look.test_a_running_agent_scope_is_busy_an_empty_one_is_not'),
    'an empty scope is not': (
        'idle', "                    return line.split()[1] != '0'", "                    return True",
        'Look.test_a_running_agent_scope_is_busy_an_empty_one_is_not'),
    'a folder it cannot list is no idle': (
        'idle', "    return os.walk(top, onerror=fail)", "    return os.walk(top)",
        'Look.test_a_cgroup_folder_it_cannot_list_cannot_tell'),
    '/proc it cannot read is no idle': (
        'idle', "            raise CannotTell(f'process {pid} cannot be read ({e.strerror})') from None",
        "            continue", 'Look.test_proc_it_cannot_list_or_read_cannot_tell'),
    'a live ticket is busy': (
        'idle', "                count += 1\n", "                pass\n",
        'Look.test_a_live_heavy_ticket_is_busy_a_dead_one_is_not'),
    'tickets it cannot read are no idle': (
        'idle', "    except FileNotFoundError:\n        return count\n    except OSError as e:\n        raise CannotTell(",
        "    except OSError:\n        return count\n    except ValueError as e:\n        raise CannotTell(",
        'Look.test_tickets_it_cannot_read_cannot_tell_and_a_link_is_not_followed'),
    'ticket folders: no link followed': (
        'idle', "DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC",
        "DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC",
        'Look.test_tickets_it_cannot_read_cannot_tell_and_a_link_is_not_followed'),
    'docker only while dockerd runs': (
        'idle', "    if settings['systemDocker'] and _running(procs, ('dockerd',), 0):",
        "    if settings['systemDocker']:", 'Look.test_docker_is_asked_only_while_its_daemon_runs'),
    'podman containers count': (
        'idle', "    if conmons:\n", "    if False:\n", 'Look.test_podman_lxc_incus_lxd_and_libvirt'),
    'only block locks on shutdown': (
        'idle', "if mode == 'block' and {'shutdown', 'sleep'} & set(what.split(':')):",
        "if 'shutdown' in what.split(':'):",
        'Look.test_block_locks_on_shutdown_or_sleep_count_delay_and_other_locks_do_not'),
    'no busctl is no idle': (
        'idle', "    if code != 0:\n        raise CannotTell(f'the inhibitor locks",
        "    if code is None:\n        return []\n    if code != 0:\n        raise CannotTell(f'the inhibitor locks",
        'Look.test_inhibitors_it_cannot_read_are_busy_busctl_missing_too'),
    'user text is one safe line': (
        'settings', "        elif unicodedata.category(c)[0] == 'C':", "        elif False:",
        'Look.test_text_from_users_cannot_forge_a_journal_line'),
    'a script run by an interpreter counts by its name': (
        'idle', "    if INTERPRETERS.match(names[0]):", "    if False:",
        'Look.test_a_package_manager_at_work_is_busy_the_shutdown_helper_is_not'),
    'docker it cannot ask is busy': (
        'idle', "                busy.append(f'cannot tell: {label}: {one_line(names, 200)}')\n                continue",
        "                continue", 'Look.test_docker_it_cannot_ask_and_a_user_who_is_not_there_are_busy'),
    'ignored containers': (
        'idle', "if not _settings.ignored_container(settings, n)]", "]",
        'Look.test_docker_is_asked_only_while_its_daemon_runs'),
    'busy scripts: only free is idle': (
        'idle', "        if verdict != 'free':", "        if verdict == 'busy':",
        'Look.test_busy_scripts_anything_but_free_is_busy'),
    'never root on a user socket': (
        'idle', "        if os.geteuid() != 0:\n            return f'cannot ask",
        "        if False:\n            return f'cannot ask",
        'Docker.test_another_users_docker_is_never_asked_as_root_in_process'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        if os.environ.get(fx.LIB_ENV):
            self.skipTest('a red check run')
        fx.red_check(self, os.path.abspath(__file__), MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
