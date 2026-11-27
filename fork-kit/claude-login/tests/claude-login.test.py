#!/usr/bin/env python3
"""Tests of fork-kit/claude-login/bin/claude-login on a fake HOME with stand-in claude, gh and tmux (#937).

Every credentials file here is made up (FAKE_TOKEN), and so is the code (CODE): each test that runs the tool checks
that neither ever shows in its output. No network, no real login, no real gh. RealTmux runs the real tmux on a
socket of its own when tmux is there.

  python3 -I -B fork-kit/claude-login/tests/claude-login.test.py
"""
import importlib.machinery, importlib.util, json, os, shutil, subprocess, sys, tempfile, time, unittest
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
        cat "$STATE/open" 2>/dev/null || echo 0 ;;
  create) [ ! -e "$STATE/fail-create" ] || exit 1; echo https://github.com/o/r/issues/1 ;;
esac
'''
# The pane echoes what is typed, as a terminal does; Enter runs $STATE/on-enter (the stand-in sign-in) if there is one.
FAKE_TMUX = '''#!/bin/sh
echo "$*" >> "$STATE/tmux.log"
case "$1" in
  has-session) [ -e "$STATE/session-$3" ] ;;
  kill-session) rm -f "$STATE/session-$3" ;;
  new-session) touch "$STATE/session-$4"; echo "Visit https://claude.ai/oauth/authorize?x=1" > "$STATE/pane" ;;
  capture-pane) cat "$STATE/pane" 2>/dev/null ;;
  send-keys) [ ! -e "$STATE/fail-send" ] || exit 1
             if [ "$4" = -l ]; then printf '%s\\n' "$5" >> "$STATE/pane"; else [ ! -x "$STATE/on-enter" ] || "$STATE/on-enter"; fi ;;
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

    def run_it(self, *args):
        r = subprocess.run([sys.executable, '-I', '-B', TOOL, *args], capture_output=True, text=True, env=self.env,
                           timeout=60)
        for secret in (CODE, FAKE_TOKEN, 'FAKE-ACCESS'):
            self.assertNotIn(secret, r.stdout + r.stderr)
        return r.returncode, r.stdout, r.stderr

    def touch(self, name):
        write(os.path.join(self.state, name), '')

    def gh(self, verb):
        path = os.path.join(self.state, 'gh.log')
        return [x for x in (read(path).splitlines() if os.path.exists(path) else []) if x.startswith('issue ' + verb)]

    def sign_in(self, who='main', on_enter=None):
        """A sign-in that is running, the code in a file, and what Enter does (a shell line)."""
        self.touch(f'session-claude-login-{who}')
        f = os.path.join(self.tmp, 'code')
        write(f, CODE + '\n')
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
        self.touch('open')
        write(os.path.join(self.state, 'open'), '1\n')
        self.assertEqual(self.run_it('check')[0], 0)
        self.assertEqual(len(self.gh('create')), 1)   # one open issue per account: not opened again

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
        self.assertIn('Claude login of second is not refreshing', titles)

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
        self.assertNotIn('send-keys', read(os.path.join(self.state, 'tmux.log')))


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
        self.touch('fail-send')
        self.assertEqual(self.run_it('finish', 'main', f)[0], 1)
        self.assertTrue(os.path.exists(f))
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


if __name__ == '__main__':
    unittest.main()
