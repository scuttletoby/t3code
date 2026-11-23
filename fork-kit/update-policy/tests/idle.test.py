"""update-policy idle: what makes the machine busy, on a fake host (cgroups, /proc, heavy tickets, a fake Docker socket,
busctl's answer) in a temporary folder. No root, no systemd, no Docker. RedCheck breaks each rule in a copy of the
code and shows its test then fails.

  python3 -I -B fork-kit/update-policy/tests/idle.test.py
"""
import fcntl, json, os, socket, sys, tempfile, threading, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixture as fx  # noqa: E402
from fixture import idle  # noqa: E402

MANAGER = 'user-1000.slice/user@1000.service'


class Look(fx.HostCase):
    def look(self, **over):
        return idle.look(self.host, fx.make_settings(**over))

    def test_an_empty_machine_is_idle(self):
        self.assertEqual(self.look(), [])
        self.cgroup(f'{MANAGER}/app.slice/app-t3code.service')      # the T3 server itself is no agent
        self.cgroup(f'{MANAGER}/session.slice/dbus.service')
        self.assertEqual(self.look(), [])

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

    def test_heavy_jobs_running_and_waiting(self):
        self.cgroup(f'{MANAGER}/heavy.slice/heavy-gradle-ab12.scope', populated=False)
        self.assertEqual(self.look(), [])
        self.cgroup(f'{MANAGER}/heavy.slice/heavy-gradle-cd34.scope')
        self.assertIn('user 1000: heavy jobs (1 running, 0 live ticket(s))', self.look())
        self.cgroup(f'{MANAGER}/ci.slice/ci-heavy.slice/heavy-x-1.scope')    # another slice: heavySlice names it
        self.assertIn('user 1000: heavy jobs (1 running, 0 live ticket(s))', self.look())
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

    def test_a_ticket_folder_reached_through_a_link_is_not_followed(self):
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
        self.assertEqual(self.look(), [])

    def test_containers_system_and_rootless_less_the_ignored(self):
        self.host.users['agent'] = fx.me()
        uid = fx.me().pw_uid
        self.host.dockers = {'/run/docker.sock': ['dev-db', 'mutation-run'],
                             f'/run/user/{uid}/docker.sock': ['pg-test']}
        busy = self.look(containerUsers=['agent'], ignoreContainers=['dev-*'])
        self.assertEqual(busy, ['system Docker: 1 running container(s) (mutation-run)',
                                'agent\'s Docker: 1 running container(s) (pg-test)'])
        self.assertEqual(self.look(systemDocker=False, ignoreContainers=['*']), [])

    def test_docker_it_cannot_ask_and_a_user_who_is_not_there_are_busy(self):
        self.host.dockers = {'/run/docker.sock': 'cannot ask Docker at /run/docker.sock (Permission denied)'}
        self.assertEqual(self.look(containerUsers=['nobody-here']),
                         ['cannot tell: containerUsers names nobody-here, who is not a user here',
                          'cannot tell: system Docker: cannot ask Docker at /run/docker.sock (Permission denied)'])

    def test_block_shutdown_locks_count_delay_and_sleep_locks_do_not(self):
        rows = [['shutdown', 'Unattended Upgrades Shutdown', 'Stop ongoing upgrades', 'delay', 0, 221],
                ['sleep', 'GNOME', 'screen locked', 'block', 1000, 5],
                ['shutdown:sleep:idle', 'mutation-tests', 'hours left', 'block', 1000, 4242]]
        self.host.answers['busctl'] = (0, json.dumps({'type': 'a(ssssuu)', 'data': [rows]}), '')
        self.assertEqual(self.look(), ['inhibitor lock: mutation-tests (uid 1000): hours left'])
        self.host.answers['busctl'] = (0, json.dumps({'type': 'a(ssssuu)', 'data': [rows[:2]]}), '')
        self.assertEqual(self.look(), [])

    def test_inhibitors_it_cannot_read_are_busy_no_logind_at_all_is_not(self):
        self.host.answers['busctl'] = (1, '', 'Failed to connect to bus')
        self.assertEqual(self.look(), ['cannot tell: cannot read the inhibitor locks (Failed to connect to bus)'])
        self.host.answers['busctl'] = (0, 'not json', '')
        self.assertIn('broken answer', self.look()[0])
        del self.host.answers['busctl']                      # busctl not installed: no logind, no locks
        self.assertEqual(self.look(), [])

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
        os.chmod(self.root + '/sys/fs/cgroup/user.slice', 0)
        self.addCleanup(os.chmod, self.root + '/sys/fs/cgroup/user.slice', 0o755)
        if os.geteuid() == 0:
            self.skipTest('root reads every folder')
        self.assertIn('cannot tell', self.look()[0])


class Docker(unittest.TestCase):
    """idle.docker_running against a fake Docker Engine API on a unix socket."""

    def serve(self, answer):
        folder = tempfile.mkdtemp(prefix='update-policy-docker-')
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

    def test_errors_are_reasons_not_names(self):
        path = self.serve(b'HTTP/1.0 500 Internal Server Error\r\n\r\n{}')
        self.assertIn('answered', idle.docker_running(path))
        path = self.serve(b'HTTP/1.0 200 OK\r\n\r\nnot json')
        self.assertIn('broken answer', idle.docker_running(path))
        self.assertIn('cannot ask Docker', idle.docker_running('/nonexistent/docker.sock'))

    def test_another_users_docker_is_never_asked_as_root_in_process(self):
        h = idle.Host()
        h.ROOT = tempfile.mkdtemp(prefix='update-policy-host-')
        os.makedirs(h.ROOT + '/run/user/4242')
        open(h.ROOT + '/run/user/4242/docker.sock', 'w').close()
        other = type('pw', (), {'pw_uid': 4242, 'pw_gid': 4242, 'pw_name': 'someone'})()
        if os.geteuid() == 0:
            self.skipTest('as root it would run the helper as that user')
        self.assertEqual(h.docker_names('/run/user/4242/docker.sock', other),
                         "cannot ask someone's Docker (run as root)")
        self.assertEqual(h.docker_names('/run/user/4242/missing.sock', other), [])   # no socket: no containers


MUTATIONS = {
    'an agent scope is busy': (
        'idle', "        if agents:\n", "        if False:\n",
        'Look.test_a_running_agent_scope_is_busy_an_empty_one_is_not'),
    'an empty scope is not': (
        'idle', "                    return line.split()[1] != '0'", "                    return True",
        'Look.test_a_running_agent_scope_is_busy_an_empty_one_is_not'),
    'a live ticket is busy': (
        'idle', "                if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):\n                    count += 1",
        "                if e.errno in ():\n                    count += 1",
        'Look.test_a_live_heavy_ticket_is_busy_a_dead_one_is_not'),
    'ticket folders: no link followed': (
        'idle', "DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC",
        "DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC",
        'Look.test_a_ticket_folder_reached_through_a_link_is_not_followed'),
    'only block locks on shutdown': (
        'idle', "if mode == 'block' and 'shutdown' in what.split(':'):", "if 'shutdown' in what.split(':'):",
        'Look.test_block_shutdown_locks_count_delay_and_sleep_locks_do_not'),
    'locks it cannot read are busy': (
        'idle', "    if isinstance(locks, str):\n        busy.append(f'cannot tell: {locks}')",
        "    if isinstance(locks, str):\n        pass",
        'Look.test_inhibitors_it_cannot_read_are_busy_no_logind_at_all_is_not'),
    'a script run by an interpreter counts by its name': (
        'idle', "        if INTERPRETERS.match(names[0]):", "        if False:",
        'Look.test_a_package_manager_at_work_is_busy_the_shutdown_helper_is_not'),
    'docker it cannot ask is busy': (
        'idle', "            busy.append(f'cannot tell: {label}: {names}')", "            pass",
        'Look.test_docker_it_cannot_ask_and_a_user_who_is_not_there_are_busy'),
    'ignored containers': (
        'idle', "if not _settings.ignored_container(settings, n)]", "]",
        'Look.test_containers_system_and_rootless_less_the_ignored'),
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
