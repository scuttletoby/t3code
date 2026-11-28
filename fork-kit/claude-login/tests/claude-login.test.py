#!/usr/bin/env python3
"""Tests of fork-kit/claude-login/bin/claude-login on a fake HOME with stand-in claude, gh and tmux (#937).

Every credentials file here is made up (FAKE_TOKEN), and so is the code (CODE): each test that runs the tool checks
that neither ever shows in its output, nor in the command lines it gave tmux and gh (the stand-ins log them). No
network, no real login, no real gh. RealTmux runs the real tmux on a socket of its own when tmux is there.

  python3 -I -B fork-kit/claude-login/tests/claude-login.test.py
"""
import fcntl, importlib.machinery, importlib.util, json, os, shutil, subprocess, sys, tempfile, time, unittest
from unittest import mock

sys.dont_write_bytecode = True
KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(KIT, 'bin', 'claude-login')
CODE, FAKE_TOKEN = 'FAKECODE42xyz', 'FAKE-REFRESH-TOKEN-1'
DAY = 86400 * 1000

FAKE_CLAUDE = '''#!/bin/sh
echo "${CLAUDE_CONFIG_DIR:-default}" >> "$STATE/claude.log"
echo '{"email":"someone@example.com","subscriptionType":"max"}'
'''
FAKE_GH = '''#!/bin/sh
printf '%s' "$*" | tr '\\n' ' ' >> "$STATE/gh.log"; echo >> "$STATE/gh.log"
case "$2" in
  list) [ ! -e "$STATE/fail-list" ] || { echo "HTTP 401" >&2; exit 1; }
        cat "$STATE/open" 2>/dev/null || echo '[]' ;;
  create) [ ! -e "$STATE/fail-create" ] || exit 1; echo https://github.com/o/r/issues/1 ;;
esac
'''
# The pane echoes what is typed, as a terminal does (a paste, or an older `send-keys -l`; each one is a line in
# $STATE/typed); Enter runs $STATE/on-enter (the stand-in sign-in) if there is one. A session's start time is in
# $STATE/created-<session>.
FAKE_TMUX = '''#!/bin/sh
echo "$*" >> "$STATE/tmux.log"
[ -z "$TMUX" ] || echo "TMUX was passed on" >> "$STATE/tmux.log"
case "$1" in
  has-session) [ -e "$STATE/session-$3" ] ;;
  kill-session) rm -f "$STATE/session-$3" "$STATE/created-$3" ;;
  new-session) touch "$STATE/session-$4"; date +%s > "$STATE/created-$4"
               [ -e "$STATE/no-link" ] || echo "Visit https://claude.ai/oauth/authorize?x=1" > "$STATE/pane" ;;
  display-message) cat "$STATE/created-$4" 2>/dev/null || date +%s ;;
  capture-pane) cat "$STATE/pane" 2>/dev/null ;;
  load-buffer) [ ! -e "$STATE/fail-load" ] || exit 1; cat > "$STATE/buffer-$3" ;;
  delete-buffer) rm -f "$STATE/buffer-$3" ;;
  paste-buffer) [ -e "$STATE/buffer-$4" ] || exit 1
                { cat "$STATE/buffer-$4"; echo; } >> "$STATE/pane"; rm -f "$STATE/buffer-$4"; echo >> "$STATE/typed" ;;
  send-keys) if [ "$4" = -l ]; then printf '%s\\n' "$5" >> "$STATE/pane"; echo >> "$STATE/typed"
             else [ ! -e "$STATE/fail-enter" ] || exit 1; [ ! -x "$STATE/on-enter" ] || "$STATE/on-enter"; fi ;;
esac
'''


def write(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    os.chmod(path, mode)


def read(path):
    with open(path) as f:
        return f.read()


def creds(days, access_min=300, refresh=FAKE_TOKEN, end=True):
    now = time.time() * 1000
    o = {'accessToken': 'FAKE-ACCESS', 'expiresAt': now + access_min * 60000}
    if end:
        o['refreshTokenExpiresAt'] = now + days * DAY
    if refresh:
        o['refreshToken'] = refresh
    return json.dumps({'claudeAiOauth': o})


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='claude-login-test-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.state, fake = (os.path.join(self.tmp, n) for n in ('home', 'state', 'bin'))
        os.makedirs(self.state)
        os.chmod(self.state, 0o700)   # $XDG_RUNTIME_DIR: a runtime folder is the user's alone, whatever the umask
        for name, body in (('claude', FAKE_CLAUDE), ('gh', FAKE_GH), ('tmux', FAKE_TMUX)):
            write(os.path.join(fake, name), body, 0o755)
        self.cfg = {'version': 1, 'claude': os.path.join(fake, 'claude'), 'issueRepo': 'o/r', 'labels': ['ops'],
                    'machine': 'the test box', 'accounts': [{'name': 'main', 'configDir': ''},
                                                            {'name': 'second', 'configDir': '~/.claude-second'}],
                    'timeout': 5, 'wait': 2, 'readPause': 0.05}
        self.save_config()
        self.env = {'PATH': fake + ':/usr/bin:/bin', 'STATE': self.state, 'HOME': self.home,
                    'XDG_RUNTIME_DIR': self.state, 'CLAUDE_LOGIN_CONFIG': os.path.join(self.tmp, 'config.json')}
        self.login('main', 20)
        self.login('second', 20)

    def save_config(self):
        write(os.path.join(self.tmp, 'config.json'), json.dumps(self.cfg))

    def cred(self, who):
        return os.path.join(self.home, '.claude' if who == 'main' else '.claude-second', '.credentials.json')

    def login(self, who, days, **kw):
        write(self.cred(who), creds(days, **kw))

    def run_it(self, *args, timeout=60):
        r = subprocess.run([sys.executable, '-I', '-B', TOOL, *args], capture_output=True, text=True, env=self.env,
                           timeout=timeout)
        logs = ''.join(read(os.path.join(self.state, n)) for n in ('tmux.log', 'gh.log')
                       if os.path.exists(os.path.join(self.state, n)))
        for secret in (CODE, FAKE_TOKEN, 'FAKE-ACCESS'):
            self.assertNotIn(secret, r.stdout + r.stderr, 'a secret in the output')
            self.assertNotIn(secret, logs, 'a secret on a command line')
        self.assertNotIn('Traceback', r.stderr)
        return r.returncode, r.stdout, r.stderr

    def log(self):
        path = os.path.join(self.state, 'tmux.log')
        return read(path) if os.path.exists(path) else ''

    def typed(self):
        path = os.path.join(self.state, 'typed')
        return len(read(path).splitlines()) if os.path.exists(path) else 0

    def open_issues(self, *titles):
        write(os.path.join(self.state, 'open'), json.dumps([{'title': t} for t in titles]))

    def touch(self, name):
        write(os.path.join(self.state, name), '')

    def gh(self, verb):
        path = os.path.join(self.state, 'gh.log')
        return [x for x in (read(path).splitlines() if os.path.exists(path) else []) if x.startswith('issue ' + verb)]

    def sign_in(self, who='main', on_enter=None, code=CODE):
        """A sign-in that is running (started just now), the code in a file, and what Enter does (a shell line)."""
        self.touch(f'session-claude-login-{who}')
        write(os.path.join(self.state, f'created-claude-login-{who}'), f'{int(time.time())}\n')
        f = os.path.join(self.tmp, 'code')
        write(f, code + '\n')
        if on_enter:
            write(os.path.join(self.state, 'on-enter'), '#!/bin/sh\n' + on_enter, 0o755)
        return f

    def new_login(self, who, days):
        path = os.path.join(self.tmp, f'new-{who}.json')
        write(path, creds(days))
        return f'cp "{path}" "{self.cred(who)}"\n'


class CheckTest(Case):
    def test_logins_with_time_left_open_nothing(self):
        self.assertEqual(self.run_it('check')[0], 0)
        self.assertEqual(self.gh('create'), [])

    def test_a_login_that_ends_soon_opens_one_issue_in_the_configured_repo(self):
        """Do the repo, the labels and the machine name come from the config, not from the tool?"""
        self.login('second', 1)
        code, out, err = self.run_it('check')
        self.assertEqual(code, 0, err)
        create = self.gh('create')
        self.assertEqual(len(create), 1)
        self.assertIn('-R o/r --title Renew the Claude login of second on the test box (ends', create[0])
        self.assertIn('--label ops', create[0])
        self.open_issues('Renew the Claude login of second on the test box (ends 2000-01-01 00:00Z)')
        self.assertEqual(self.run_it('check')[0], 0)
        self.assertEqual(len(self.gh('create')), 1)   # one open issue per account and kind: not opened again
        self.assertIn('--json title', self.gh('list')[-1])

    def test_gh_failures_and_no_repo_are_failures_that_still_check_the_other_account(self):
        self.login('main', 1)
        self.login('second', 1)
        self.touch('fail-list')
        code, _, err = self.run_it('check')
        self.assertEqual(code, 1)
        self.assertIn('main: cannot look up', err)
        self.assertIn('second: cannot look up', err)
        os.unlink(os.path.join(self.state, 'fail-list'))
        self.cfg['issueRepo'] = ''
        self.save_config()
        code, _, err = self.run_it('check')
        self.assertEqual(code, 1)
        self.assertIn('the config names no issueRepo', err)

    def test_signed_out_and_not_refreshing(self):
        os.unlink(self.cred('main'))
        self.login('second', 20, access_min=-120)
        self.assertEqual(self.run_it('check')[0], 0)
        titles = ' | '.join(self.gh('create'))
        self.assertIn('Renew the Claude login of main on the test box (signed out)', titles)
        self.assertIn('Claude login of second on the test box is not refreshing', titles)

    def test_check_while_a_sign_in_is_running_opens_no_issue(self):
        """State: an agent is signing the account in right now; the hourly check must not open an issue for it."""
        os.unlink(self.cred('main'))
        self.touch('session-claude-login-main')
        code, out, _ = self.run_it('check')
        self.assertEqual(code, 0)
        self.assertIn('a sign-in for it is running; no issue opened', out)
        self.assertEqual(self.gh('create'), [])

    def test_a_file_of_an_older_claude_code_has_no_end_date(self):
        """State: a credentials file without refreshTokenExpiresAt: status says so, check fails loudly, no issue."""
        self.login('main', 20, end=False)
        code, out, _ = self.run_it('status')
        self.assertEqual(code, 0)
        self.assertIn('main som…@example.com plan=max login=ok', out)
        self.assertIn('login_days_left=? ends=?', out)
        code, _, err = self.run_it('check')
        self.assertEqual(code, 1)
        self.assertIn('has no end date', err)
        self.assertEqual(self.gh('create'), [])


class UnreadableTest(Case):
    """Finding 1 of #937: the "unreadable" issue said start then finish, but finish refuses a broken file every
    time. The advice now leads to a working login, and this test follows it step by step."""

    def test_the_unreadable_advice_leads_to_a_working_login(self):
        write(self.cred('main'), '{"claudeAiOauth": {"refreshTok')
        self.assertEqual(self.run_it('check')[0], 0)
        body = self.gh('create')[0]
        self.assertIn('(unreadable)', body)
        self.assertIn('first run `claude-login set-aside main`', body)
        f = self.sign_in('main', self.new_login('main', 30))
        code, _, err = self.run_it('finish', 'main', f)          # the old dead end: refused, the code kept
        self.assertEqual(code, 1)
        self.assertIn('run `claude-login set-aside main` first', err)
        self.assertTrue(os.path.exists(f))
        code, _, err = self.run_it('start', 'main', '--again')   # start says the same instead of a link
        self.assertEqual(code, 1)
        self.assertIn('set-aside main', err)
        code, out, err = self.run_it('set-aside', 'main')        # the advice, step by step
        self.assertEqual(code, 0, err)
        kept = [n for n in os.listdir(os.path.dirname(self.cred('main'))) if '.unreadable-' in n]
        self.assertEqual(len(kept), 1, 'the broken file is kept, not deleted')
        code, out, err = self.run_it('start', 'main', '--again')
        self.assertEqual(code, 0, err)
        self.assertIn('oauth/authorize', out)
        code, out, err = self.run_it('finish', 'main', f)
        self.assertEqual(code, 0, err)
        self.assertIn('login=ok', out)
        self.assertFalse(os.path.exists(f))

    def test_set_aside_moves_nothing_that_reads_fine(self):
        code, _, err = self.run_it('set-aside', 'main')
        self.assertEqual(code, 1)
        self.assertIn('reads fine now (login=ok); nothing moved', err)
        self.assertTrue(os.path.exists(self.cred('main')))


def load_tool():
    loader = importlib.machinery.SourceFileLoader('claude_login', TOOL)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader('claude_login', loader))
    loader.exec_module(mod)
    return mod


class ReadAgainTest(Case):
    """Finding 2 of #937: a credentials file is read again while it is unreadable (a refresh rewrites it), so a
    file half-written for a moment is not reported as broken. Each test fails when only one read is made."""

    def setUp(self):
        super().setUp()
        self.m = load_tool()
        from claudelogin import creds
        self.creds = creds
        self.cfg_loaded = dict(self.m.DEFAULTS, **{k: v for k, v in self.cfg.items() if k != 'version'})
        self.acct = self.m.Account({'name': 'main', 'configDir': os.path.join(self.home, '.claude')})

    def reads(self, *states):
        it = iter(states)
        return mock.patch.object(self.creds, 'parse_creds', side_effect=lambda p: next(it))

    def test_a_file_half_written_for_one_read_is_read_again(self):
        with self.reads(('unreadable', {}), ('ok', {'expiresAt': None, 'end': None})) as p:
            self.assertEqual(self.creds.read_login(self.acct, self.cfg_loaded)[0], 'ok')
        self.assertEqual(p.call_count, 2)
        with self.reads(('unreadable', {}), ('unreadable', {}), ('signed_out', {})) as p:
            self.assertEqual(self.creds.read_login(self.acct, self.cfg_loaded)[0], 'signed_out')

    def test_a_file_that_stays_broken_is_read_three_times_then_unreadable(self):
        with self.reads(*[('unreadable', {})] * 5) as p:
            self.assertEqual(self.creds.read_login(self.acct, self.cfg_loaded)[0], 'unreadable')
        self.assertEqual(p.call_count, 3)

    def test_check_does_not_report_a_file_being_rewritten(self):
        """Through the check itself: a file unreadable at the first read is no "unreadable" issue."""
        good = self.creds.parse_creds(self.cred('main'))
        with self.reads(('unreadable', {}), good, good, good), \
                mock.patch.object(self.m, 'open_once') as opened, mock.patch.object(self.m, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 1, '', '')
            self.cfg_loaded['accounts'] = self.cfg_loaded['accounts'][:1]
            self.assertEqual(self.m.cmd_check(self.cfg_loaded), 0)
        opened.assert_not_called()

    def test_finish_never_reads_a_half_written_file_as_no_login(self):
        """With "no login" as the before value, the next good read would look like a new login."""
        write(self.cred('main'), '{"claudeAiOauth": {"refreshTok')
        f = self.sign_in('main', self.new_login('main', 20))
        code, _, err = self.run_it('finish', 'main', f)
        self.assertEqual(code, 1)
        self.assertIn('cannot be read; nothing typed', err)
        self.assertTrue(os.path.exists(f), 'the code is kept for a retry')
        self.assertNotIn('send-keys', self.log())
        self.assertNotIn('load-buffer', self.log())


class FinishTest(Case):
    def test_a_new_login_is_a_success_and_the_code_file_is_gone(self):
        f = self.sign_in('second', self.new_login('second', 30))
        code, out, err = self.run_it('finish', 'second', f)
        self.assertEqual(code, 0, err)
        self.assertIn('second som…@example.com plan=max login=ok', out)
        self.assertFalse(os.path.exists(f))
        self.assertFalse(os.path.exists(os.path.join(self.state, 'session-claude-login-second')))
        self.assertIn('/.claude-second', read(os.path.join(self.state, 'claude.log')))   # its own config folder

    def test_an_access_token_refresh_is_not_a_new_login(self):
        old = json.loads(read(self.cred('main')))['claudeAiOauth']['refreshTokenExpiresAt']
        path = os.path.join(self.tmp, 'refresh.json')
        data = json.loads(creds(20))
        data['claudeAiOauth']['refreshTokenExpiresAt'] = old + 10 * 60000   # moves less than an hour
        write(path, json.dumps(data))
        f = self.sign_in('main', f'cp "{path}" "{self.cred("main")}"\n')
        code, _, err = self.run_it('finish', 'main', f)
        self.assertEqual(code, 1)
        self.assertIn('no new login after 2 s', err)

    def test_a_stale_code_prints_only_the_error_word(self):
        """State: finish with a stale code: the screen shows an error next to the pasted code; only the word."""
        f = self.sign_in('main', 'echo "Expired code: $(tail -1 "$STATE/pane")" >> "$STATE/pane"\n')
        code, _, err = self.run_it('finish', 'main', f)
        self.assertEqual(code, 1)
        self.assertIn('the screen says: expired', err)
        self.assertIn('start main --again', err)

    def test_typing_that_fails_and_no_sign_in_keep_the_code_file(self):
        """State: a login stopped half-way leaves the code file; it is kept for a retry, never typed twice."""
        f = self.sign_in('main')
        self.touch('fail-load')
        code, _, err = self.run_it('finish', 'main', f)
        self.assertEqual(code, 1)
        self.assertIn('nothing typed', err)
        self.assertTrue(os.path.exists(f))
        self.assertEqual(self.typed(), 0)
        self.assertTrue(os.path.exists(os.path.join(self.state, 'session-claude-login-main')), 'the sign-in is kept')
        os.unlink(os.path.join(self.state, 'session-claude-login-main'))
        code, _, err = self.run_it('finish', 'main', f)
        self.assertEqual(code, 1)
        self.assertIn('no sign-in for main is running', err)
        self.assertTrue(os.path.exists(f))


class StartTest(Case):
    def test_start_twice_keeps_the_running_sign_in_unless_told_again(self):
        """State: start run twice: the second is refused (another agent may be about to finish it)."""
        code, out, err = self.run_it('start', 'main')
        self.assertEqual(code, 0, err)
        self.assertIn('https://claude.ai/oauth/authorize?x=1', out)
        code, _, err = self.run_it('start', 'main')
        self.assertEqual(code, 1)
        self.assertIn('already running', err)
        self.assertNotIn('kill-session', read(os.path.join(self.state, 'tmux.log')))
        self.assertEqual(self.run_it('start', 'main', '--again')[0], 0)
        self.assertEqual(self.run_it('start', 'second')[0], 0)   # another account is another sign-in

    def test_config_and_arguments(self):
        self.assertEqual(self.run_it('start', 'nobody')[0], 2)
        self.assertEqual(self.run_it('start')[0], 2)
        write(os.path.join(self.tmp, 'config.json'), '{"version": 1, "accounts": [{"name": "Bad Name"}]}')
        code, _, err = self.run_it('status')
        self.assertEqual(code, 2)
        self.assertIn('accounts:', err)
        os.unlink(os.path.join(self.tmp, 'config.json'))
        self.assertEqual(self.run_it('check')[0], 2)


class CodeOnStdinTest(Case):
    """r1 blocker 1: the code went on tmux's command line (`send-keys -l CODE`), readable in ps and /proc/*/cmdline
    by any local user, and a code starting with '-' was read as tmux options. run_it checks the logged command
    lines of every test; these check the paste itself."""

    def test_the_code_goes_to_tmux_on_stdin_and_a_dash_code_works(self):
        f = self.sign_in('main', self.new_login('main', 30), code='-' + CODE)
        code, out, err = self.run_it('finish', 'main', f)
        self.assertEqual(code, 0, err)
        self.assertIn('load-buffer -b claude-login-main-', self.log())
        self.assertIn('-' + CODE, read(os.path.join(self.state, 'pane')), 'the code reached the sign-in')
        self.assertEqual(self.typed(), 1)
        self.assertEqual([n for n in os.listdir(self.state) if n.startswith('buffer-')], [], 'no buffer left')
        self.assertFalse(os.path.exists(f))

    def test_an_enter_that_fails_ends_the_sign_in_so_a_retry_never_types_twice(self):
        f = self.sign_in('main')
        self.touch('fail-enter')
        code, _, err = self.run_it('finish', 'main', f)
        self.assertEqual(code, 1)
        self.assertIn('start main --again', err)
        self.assertFalse(os.path.exists(os.path.join(self.state, 'session-claude-login-main')), 'no pane keeps it')
        os.unlink(os.path.join(self.state, 'fail-enter'))
        if os.path.exists(f):   # the old way kept it "for a retry"
            self.run_it('finish', 'main', f)
        self.assertEqual(self.typed(), 1, 'the code was typed twice')


class LockTest(Case):
    """r1 blocker 2: the start lock fell back to a shared /tmp, followed a link, ended in a traceback on another
    user's file, and finish and set-aside took no lock at all."""

    def lock_path(self, base):
        return os.path.join(base, 'claude-login', 'main.lock')

    def test_a_link_at_the_lock_is_refused_and_never_followed(self):
        os.makedirs(os.path.join(self.state, 'claude-login'), 0o700)
        planted = [os.path.join(self.tmp, n) for n in ('planted-1', 'planted-2')]
        os.symlink(planted[0], self.lock_path(self.state))
        os.symlink(planted[1], os.path.join(self.state, 'claude-login-main.lock'))   # where the old start looked
        code, _, err = self.run_it('start', 'main')
        self.assertEqual(code, 1)
        self.assertIn('cannot open the lock', err)
        self.assertEqual([os.path.exists(p) for p in planted], [False, False])

    def test_a_runtime_folder_others_can_write_to_is_not_used(self):
        os.chmod(self.state, 0o777)
        self.addCleanup(os.chmod, self.state, 0o700)
        code, _, err = self.run_it('start', 'main')
        self.assertEqual(code, 0, err)
        lock = self.lock_path(os.path.join(self.home, '.local', 'state'))
        self.assertTrue(os.path.isfile(lock))
        self.assertEqual(os.stat(os.path.dirname(lock)).st_mode & 0o777, 0o700)

    def test_without_a_runtime_folder_the_lock_is_in_the_users_state_folder(self):
        del self.env['XDG_RUNTIME_DIR']
        code, _, err = self.run_it('start', 'main')
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(self.lock_path(os.path.join(self.home, '.local', 'state'))))

    def test_a_lock_folder_others_can_use_is_refused(self):
        os.makedirs(os.path.join(self.state, 'claude-login'))
        os.chmod(os.path.join(self.state, 'claude-login'), 0o755)
        code, _, err = self.run_it('start', 'main')
        self.assertEqual(code, 1)
        self.assertIn('only you can use', err)

    def test_finish_and_set_aside_wait_for_a_running_start(self):
        os.makedirs(os.path.join(self.state, 'claude-login'), 0o700)
        fd = os.open(self.lock_path(self.state), os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        f = self.sign_in('main', self.new_login('main', 30))
        code, _, err = self.run_it('finish', 'main', f)
        self.assertEqual(code, 1)
        self.assertIn('another claude-login start, finish or set-aside for main is running', err)
        self.assertEqual(self.typed(), 0)
        self.assertTrue(os.path.exists(f))
        write(self.cred('main'), '{"claudeAiOauth": {"refreshTok')
        code, _, err = self.run_it('set-aside', 'main')
        self.assertEqual(code, 1)
        self.assertIn('another claude-login', err)
        self.assertTrue(os.path.exists(self.cred('main')), 'nothing moved')


class StaleSignInTest(Case):
    """r1 major: a start nobody finished kept its session, and check skipped the account for as long as it was
    left (the sleep after the sign-in only starts once claude exits)."""

    def test_a_sign_in_left_for_long_is_ended_and_check_opens_the_issue(self):
        os.unlink(self.cred('main'))
        self.touch('session-claude-login-main')
        write(os.path.join(self.state, 'created-claude-login-main'), f'{int(time.time()) - 7200}\n')
        code, _, err = self.run_it('check')
        self.assertEqual(code, 0, err)
        self.assertIn('ended a sign-in older than 30 minutes', err)
        self.assertIn('(signed out)', ' '.join(self.gh('create')))
        self.assertFalse(os.path.exists(os.path.join(self.state, 'session-claude-login-main')))

    def test_a_stale_sign_in_is_not_finished_and_a_plain_start_replaces_it(self):
        f = self.sign_in('main', self.new_login('main', 30))
        write(os.path.join(self.state, 'created-claude-login-main'), f'{int(time.time()) - 7200}\n')
        code, _, err = self.run_it('finish', 'main', f)
        self.assertEqual(code, 1)
        self.assertIn('no sign-in for main is running', err)
        self.assertEqual(self.typed(), 0)
        self.touch('session-claude-login-main')
        write(os.path.join(self.state, 'created-claude-login-main'), f'{int(time.time()) - 7200}\n')
        self.assertEqual(self.run_it('start', 'main')[0], 0)

    def test_check_opens_no_issue_of_any_kind_while_a_fresh_sign_in_runs(self):
        self.login('second', 1, access_min=-120)
        self.sign_in('second')
        code, out, _ = self.run_it('check')
        self.assertEqual(code, 0)
        self.assertIn('second: login=ok, but a sign-in for it is running', out)
        self.assertEqual(self.gh('create'), [])

    def test_a_start_whose_link_never_shows_leaves_no_session(self):
        self.touch('no-link')
        code, _, err = self.run_it('start', 'main')
        self.assertEqual(code, 1)
        self.assertIn('no sign-in link appeared', err)
        self.assertFalse(os.path.exists(os.path.join(self.state, 'session-claude-login-main')))


class OneIssueTest(Case):
    """r1 major: the open-issue lookup searched `Renew the Claude login of <account>` and trusted the count, so one
    machine's issue hid another's, `work-2` hid `work`, and one kind hid another."""

    def test_issues_of_other_machines_accounts_and_kinds_hide_nothing(self):
        self.login('second', 1)
        self.open_issues('Renew the Claude login of second on box-a (ends 2000-01-01 00:00Z)',
                         'Renew the Claude login of second-2 on the test box (ends 2000-01-01 00:00Z)',
                         'Renew the Claude login of second on the test box (unreadable)',
                         'Claude login of second is not refreshing')
        self.assertEqual(self.run_it('check')[0], 0)
        create = self.gh('create')
        self.assertEqual(len(create), 1)
        self.assertIn('--title Renew the Claude login of second on the test box (ends', create[0])

    def test_the_not_refreshing_issue_is_per_machine(self):
        self.login('second', 20, access_min=-120)
        self.open_issues('Claude login of second on box-a is not refreshing')
        self.assertEqual(self.run_it('check')[0], 0)
        self.assertEqual(len(self.gh('create')), 1)
        self.open_issues('Claude login of second on the test box is not refreshing')
        self.assertEqual(self.run_it('check')[0], 0)
        self.assertEqual(len(self.gh('create')), 1, 'its own open issue: not opened again')

    def test_a_list_that_is_not_json_fails_the_run(self):
        self.login('second', 1)
        write(os.path.join(self.state, 'open'), '3\n')
        code, _, err = self.run_it('check')
        self.assertEqual(code, 1)
        self.assertIn('gh printed no list', err)
        self.assertEqual(self.gh('create'), [])


class FileReadTest(Case):
    """r1 major: files were read with a plain open: a dangling link read as signed out, a FIFO hung every command,
    set-aside could overwrite a backup, and Infinity, 1e30 or NaN crashed status or turned the warning off."""

    def status(self):
        code, out, err = self.run_it('status', timeout=20)
        self.assertEqual(code, 0, err)
        return out

    def test_a_dangling_link_is_unreadable_not_signed_out(self):
        os.unlink(self.cred('main'))
        os.symlink(os.path.join(self.tmp, 'nowhere.json'), self.cred('main'))
        self.assertIn('main som…@example.com plan=max login=unreadable', self.status())
        self.assertEqual(self.run_it('set-aside', 'main')[0], 0)
        self.assertFalse(os.path.lexists(self.cred('main')))

    def test_a_fifo_never_hangs_status(self):
        os.unlink(self.cred('main'))
        os.mkfifo(self.cred('main'), 0o600)
        self.assertIn('login=unreadable', self.status())

    def test_numbers_that_are_no_times_do_not_crash_status(self):
        for text in ('{"claudeAiOauth": {"refreshToken": "x", "expiresAt": Infinity}}',
                     '{"claudeAiOauth": {"refreshToken": "x", "expiresAt": NaN}}'):
            with self.subTest(text=text):
                write(self.cred('main'), text)
                self.assertIn('login=unreadable', self.status())
        write(self.cred('main'), '{"claudeAiOauth": {"refreshToken": "x", "expiresAt": 1e400, '
                                 '"refreshTokenExpiresAt": 1e30}}')
        self.assertIn('login=ok access_min_left=? login_days_left=? ends=?', self.status())

    def test_numbers_in_the_config_that_would_turn_checks_off_are_refused(self):
        for key, text in (('warnDays', 'NaN'), ('warnDays', '1e400'), ('wait', 'Infinity'), ('timeout', '0')):
            with self.subTest(key=key, text=text):
                raw = json.dumps(dict(self.cfg, **{key: 1}))
                write(os.path.join(self.tmp, 'config.json'), raw.replace(f'"{key}": 1', f'"{key}": {text}'))
                self.assertEqual(self.run_it('check')[0], 2)

    def test_set_aside_never_overwrites_a_backup_of_the_same_second(self):
        write(self.cred('main'), '{"claudeAiOauth": {"refreshTok')
        now = time.time()
        old = [self.cred('main') + time.strftime('.unreadable-%Y%m%dT%H%M%SZ', time.gmtime(now + d)) for d in range(3)]
        for p in old:
            write(p, 'an earlier broken file')
        code, _, err = self.run_it('set-aside', 'main')
        self.assertEqual(code, 0, err)
        self.assertEqual([read(p) for p in old], ['an earlier broken file'] * 3)
        kept = [n for n in os.listdir(os.path.dirname(self.cred('main'))) if '.unreadable-' in n]
        self.assertEqual(len(kept), 4)

    def test_a_code_file_that_is_a_link_or_a_fifo_or_not_text_is_refused_and_nothing_typed(self):
        self.sign_in('main')
        other = os.path.join(self.tmp, 'other')
        write(other, 'NOT-A-CODE\n')
        link, fifo, binary = (os.path.join(self.tmp, n) for n in ('link', 'fifo', 'binary'))
        os.symlink(other, link)
        os.mkfifo(fifo, 0o600)
        with open(binary, 'wb') as f:
            f.write(b'\xff\xfe\n')
        for path, why in ((link, 'a regular file, not a link'), (fifo, 'a regular file'), (binary, 'is not text')):
            with self.subTest(path=path):
                code, _, err = self.run_it('finish', 'main', path, timeout=20)
                self.assertEqual(code, 2)
                self.assertIn(why, err)
        self.assertEqual(self.typed(), 0)


class FinishEdgeTest(Case):
    """r1 major and minors of finish: an unlink that fails, a path that is not UTF-8, a screen that is not UTF-8,
    and a login redone within the hour or with no end date."""

    @unittest.skipIf(os.geteuid() == 0, 'root can delete from a read-only folder')
    def test_a_code_file_that_cannot_be_deleted_is_a_warning_and_the_login_is_still_checked(self):
        self.sign_in('main', self.new_login('main', 30))
        folder = os.path.join(self.tmp, 'codes')
        f = os.path.join(folder, 'code')
        write(f, CODE + '\n')
        os.chmod(folder, 0o500)
        self.addCleanup(os.chmod, folder, 0o700)
        code, out, err = self.run_it('finish', 'main', f)
        self.assertEqual(code, 0, err)
        self.assertIn('could not delete the code file', err)
        self.assertIn('login=ok', out)
        self.assertFalse(os.path.exists(os.path.join(self.state, 'session-claude-login-main')))

    def test_a_path_that_is_not_utf8_is_a_clean_message(self):
        gone = os.fsencode(self.tmp) + b'/gone-\xff'
        r = subprocess.run([sys.executable, '-I', '-B', TOOL, 'finish', 'main', gone], capture_output=True,
                           env=self.env, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn(b'is missing', r.stderr)
        self.assertNotIn(b'Traceback', r.stderr)

    def test_a_screen_that_is_not_utf8_is_read_not_a_traceback(self):
        f = self.sign_in('main', 'printf \'\\377\\376 Invalid code\\n\' >> "$STATE/pane"\n')
        code, _, err = self.run_it('finish', 'main', f)
        self.assertEqual(code, 1)
        self.assertIn('the screen says: invalid', err)

    def redo(self, end):
        path = os.path.join(self.tmp, 'redo.json')
        data = json.loads(creds(20, end=end))
        if end:
            old = json.loads(read(self.cred('main')))['claudeAiOauth']['refreshTokenExpiresAt']
            data['claudeAiOauth']['refreshTokenExpiresAt'] = old + 10 * 60000
        write(path, json.dumps(data))
        return f'cp "{path}" "{self.cred("main")}"; echo "claude-login: claude exited 0" >> "$STATE/pane"\n'

    def test_a_login_redone_within_the_hour_or_with_no_end_date_is_a_new_login(self):
        for end in (True, False):
            with self.subTest(end=end):
                f = self.sign_in('main', self.redo(end))
                code, out, err = self.run_it('finish', 'main', f)
                self.assertEqual(code, 0, err)
                self.assertIn('login=ok', out)

    def test_claude_that_exits_with_a_failure_is_a_failed_sign_in(self):
        f = self.sign_in('main', 'echo "claude-login: claude exited 1" >> "$STATE/pane"\n')
        code, _, err = self.run_it('finish', 'main', f)
        self.assertEqual(code, 1)
        self.assertIn('claude exited 1', err)


class MinorTest(Case):
    def setUp(self):
        super().setUp()
        self.m = load_tool()

    def test_an_ended_login_is_not_ok_and_not_also_not_refreshing(self):
        self.login('main', -2, access_min=-120)
        self.assertIn('main som…@example.com plan=max login=ended', self.run_it('status')[1])
        self.assertEqual(self.run_it('check')[0], 0)
        titles = ' | '.join(self.gh('create'))
        self.assertIn('Renew the Claude login of main on the test box (ended)', titles)
        self.assertNotIn('not refreshing', titles)

    def test_a_short_email_shows_never_its_whole_name(self):
        self.assertEqual(self.m.mask('someone@example.com'), 'som…@example.com')
        self.assertEqual(self.m.mask('ab@example.com'), 'a…@example.com')
        self.assertEqual(self.m.mask('a@example.com'), '…@example.com')
        self.assertEqual(self.m.mask('no-at-sign'), '?')

    def test_a_tmux_it_runs_in_is_not_the_one_it_talks_to(self):
        self.env['TMUX'] = '/elsewhere/default,1,0'
        self.assertEqual(self.run_it('start', 'main')[0], 0)
        self.assertNotIn('TMUX was passed on', self.log())

    def test_small_rules(self):
        with mock.patch.dict(os.environ, {'HOME': self.home}):
            self.assertEqual(self.m.expand('~'), self.home)
        self.assertEqual(self.m.ERROR_WORDS.findall('errorless, invalidated, unfailed'), [])
        self.assertEqual(self.m.ERROR_WORDS.findall('Error: code expired'), ['Error', 'expired'])
        for key, value in (('claude', ''), ('machine', ''), ('machine', ' box'), ('machine', 'a\nb')):
            with self.subTest(key=key, value=value):
                write(os.path.join(self.tmp, 'config.json'), json.dumps(dict(self.cfg, **{key: value})))
                code, _, err = self.run_it('status')
                self.assertEqual(code, 2)
                self.assertIn(f'{key}:', err)


@unittest.skipUnless(shutil.which('tmux'), 'needs tmux')
class RealTmux(unittest.TestCase):
    """The start lock with the real tmux, on a socket of its own: a tmux server started by `start` must not keep
    the lock, or every later start (also --again) would be refused."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cl-')   # short: a unix socket path is at most 107 bytes
        home, run = os.path.join(self.tmp, 'h'), os.path.join(self.tmp, 'r')
        os.makedirs(home)
        os.makedirs(run)
        claude = os.path.join(self.tmp, 'claude')
        write(claude, '#!/bin/sh\necho "Visit https://claude.ai/oauth/authorize?code=x"\nsleep 600\n', 0o755)
        write(os.path.join(self.tmp, 'c.json'), json.dumps({'version': 1, 'claude': claude, 'accounts': [
            {'name': 'a', 'configDir': ''}, {'name': 'b', 'configDir': '~/.b'}]}))
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': home, 'TMUX_TMPDIR': self.tmp, 'XDG_RUNTIME_DIR': run,
                    'TERM': 'xterm', 'CLAUDE_LOGIN_CONFIG': os.path.join(self.tmp, 'c.json')}
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(subprocess.run, ['tmux', 'kill-server'], env=self.env, capture_output=True, timeout=60)

    def run_it(self, *args):
        r = subprocess.run([sys.executable, '-I', '-B', TOOL, *args], capture_output=True, text=True, env=self.env,
                           timeout=60)
        return r.returncode, r.stdout + r.stderr

    def test_again_replaces_a_sign_in_and_another_session_never_blocks_a_start(self):
        code, out = self.run_it('start', 'a')
        self.assertEqual(code, 0, out)
        code, out = self.run_it('start', 'a')
        self.assertEqual(code, 1, out)
        self.assertIn('already running', out, 'the right reason, not "another start is running"')
        self.assertEqual(self.run_it('start', 'a', '--again')[0], 0)
        self.assertEqual(self.run_it('start', 'b')[0], 0)
        subprocess.run(['tmux', 'kill-session', '-t', 'claude-login-a'], env=self.env, check=True, timeout=60)
        self.assertEqual(self.run_it('start', 'a')[0], 0)

    def test_finish_pastes_a_dash_code_into_the_real_sign_in(self):
        """The code reaches the sign-in through a tmux buffer (on stdin, never a command line), also when it starts
        with '-', which `send-keys` read as its options; the sign-in is ended after it."""
        cred = os.path.join(self.env['HOME'], '.claude', '.credentials.json')
        new = os.path.join(self.tmp, 'new.json')
        write(new, creds(30))
        claude = os.path.join(self.tmp, 'claude')
        write(claude, '#!/bin/sh\necho "Visit https://claude.ai/oauth/authorize?code=x"\nprintf "Paste code > "\n'
                      f'read -r code\n[ "$code" = "-{CODE}" ] || {{ echo "Invalid code"; exit 1; }}\n'
                      f'mkdir -p "{os.path.dirname(cred)}"\ncp "{new}" "{cred}"\n', 0o755)
        self.assertEqual(self.run_it('start', 'a')[0], 0)
        f = os.path.join(self.tmp, 'code')
        write(f, f'-{CODE}\n')
        code, out = self.run_it('finish', 'a', f)
        self.assertEqual(code, 0, out.replace(CODE, '<code>'))
        self.assertIn('login=ok', out)
        self.assertNotIn(CODE, out)
        self.assertFalse(os.path.exists(f))
        r = subprocess.run(['tmux', 'has-session', '-t', 'claude-login-a'], env=self.env, capture_output=True,
                           timeout=60)
        self.assertNotEqual(r.returncode, 0, 'the sign-in is ended')


if __name__ == '__main__':
    unittest.main()
