#!/usr/bin/env python3
"""Tests of fork-kit/agent-guards/bin/leak-guard (refuse a post or a push that holds a private pattern, #892),
bin/leak-scan (report old GitHub text that does) and lib/agent_guards/leak.py (the pattern file).

Every pattern here is made up (FAKE_NAME, FAKE_HOST); no test reads the machine's own pattern file. Each refusal
and report is also checked for never holding the pattern or the text it matched. No root, no network: git runs on
temporary repos with a bare remote, leak-scan on a stand-in gh."""
import contextlib, io, json, os, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guards_fixture as fx  # noqa: E402
from agent_guards import leak  # noqa: E402

FAKE_NAME, FAKE_HOST = 'Alex Example', 'box-17.lan'
FAKE_PLACE = 'alexbox'   # a made-up private folder name (line 6)
PATTERNS = (f"# made-up patterns for the tests\n\n{FAKE_NAME}\nre:box-\\d+\\.lan\nre:fake-user@example\\.org\n"
            f"{FAKE_PLACE}\n")
SECRETS = (FAKE_NAME, FAKE_NAME.lower(), FAKE_HOST, 'box-', 'fake-user', FAKE_PLACE)
GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 'Agent',
           'GIT_AUTHOR_EMAIL': 'agent@example.com', 'GIT_COMMITTER_NAME': 'Agent',
           'GIT_COMMITTER_EMAIL': 'agent@example.com'}


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='leak-guard-test-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.pfile = self.write('private/leak-patterns', PATTERNS)
        self.machine = fx.machine_file(self.dir, {'leakGuard': {'patternsFile': self.pfile}})
        self.g = fx.load_hook('leak-guard', self.machine)
        self.env = dict(os.environ, **GIT_ENV)
        os.environ.update(GIT_ENV)   # the guard's own git calls (in this process) read no user config either

    def write(self, name, text):
        p = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w') as f:
            f.write(text)
        return p

    def decide(self, cmd, cwd=None):
        return self.g.decide(cmd, cwd or self.dir, leak.load(self.pfile))

    def assertQuiet(self, text):
        for s in SECRETS:
            self.assertNotIn(s, text or '')

    def refused(self, *cmds, cwd=None, says='matches line'):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                why = self.decide(cmd, cwd)
                self.assertIsNotNone(why, cmd)
                self.assertIn(says, why)
                self.assertQuiet(why)

    def passes(self, *cmds, cwd=None):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decide(cmd, cwd), cmd)


class PostTest(Case):
    def test_a_post_that_holds_a_pattern_is_refused(self):
        """Is the pattern found wherever the text of a post is: the command, a heredoc, a named file, any case?"""
        self.write('pr.md', f'Thanks to {FAKE_NAME} for the box.\n')
        self.write('clean.md', 'Nothing personal here.\n')
        self.write('msg.txt', f'Fix the build on {FAKE_HOST}\n')
        self.refused(f"gh issue comment 1 -R o/r --body 'hi {FAKE_NAME}'",
                     f"gh pr create --title x --body 'runs on {FAKE_HOST.upper()}'",
                     f"cat <<'E' | gh issue create -R o/r -F -\nby {FAKE_NAME.lower()}\nE",
                     'gh pr create --body-file pr.md', 'gh pr edit 3 --body-file=pr.md',
                     'gh api repos/o/r/issues -F body=@pr.md', 'gh issue create --title t -F - < pr.md',
                     'git commit -F msg.txt', f"git commit -qm 'by {FAKE_NAME}'", f"git tag -m '{FAKE_HOST}' v1",
                     f"timeout 30 gh api repos/o/r/issues -f body='{FAKE_NAME}'",
                     f"curl -d '{FAKE_NAME}' https://api.github.com/repos/o/r/issues")
        self.passes("gh pr create --title x --body 'nothing personal'", 'gh pr create --body-file clean.md',
                    'git commit -F clean.md', "git commit -m 'fix the build'")

    def test_text_that_is_not_posted_passes(self):
        """Does the guard leave alone commands that post nothing, words that only contain a pattern, and folders?"""
        os.makedirs(os.path.join(self.dir, FAKE_NAME))
        self.passes(f'echo {FAKE_NAME}', f"grep -r '{FAKE_HOST}' .", f'cat > note.md <<E\n{FAKE_NAME}\nE',
                    'gh pr create --body "Alexander Examples"',          # a longer word is another word
                    f'git status  # {FAKE_NAME}',                        # a comment
                    f"cd '{FAKE_NAME}' && gh pr view 1", f"git -C '{FAKE_NAME}' commit -m 'fix'",
                    f"curl -d '{FAKE_NAME}' https://example.org/x")      # not GitHub

    def test_no_pattern_file_passes_and_a_broken_one_refuses_posts_only(self):
        """Without a file nothing is checked; with a file that cannot be used whole, is every post refused while
        other commands still run, naming the line and never the pattern?"""
        os.unlink(self.pfile)
        self.passes(f"gh issue comment 1 --body '{FAKE_NAME}'")
        self.write('private/leak-patterns', f'{FAKE_NAME}\nre:box-(\n')
        self.refused("gh issue comment 1 --body 'clean'", "git commit -m 'clean'", says='line 2 of the pattern file')
        self.passes('ls', f'echo {FAKE_NAME}')
        self.write('private/leak-patterns', 're:   \n')
        self.refused('gh pr create --body x', says='line 1 of the pattern file is no valid pattern')


class PushTest(Case):
    def git(self, *args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd or self.repo, env=self.env, check=True, capture_output=True,
                              text=True).stdout

    def setUp(self):
        super().setUp()
        self.remote, self.repo = os.path.join(self.dir, 'remote.git'), os.path.join(self.dir, 'repo')
        self.git('init', '-q', '--bare', self.remote, cwd=self.dir)
        self.git('init', '-q', '-b', 'main', self.repo, cwd=self.dir)
        self.git('remote', 'add', 'origin', self.remote)
        self.commit('a.txt', 'first\n', 'first')
        self.git('push', '-q', 'origin', 'main')

    def commit(self, name, text, msg, **env):
        with open(os.path.join(self.repo, name), 'w') as f:
            f.write(text)
        self.git('add', name)
        subprocess.run(['git', 'commit', '-qm', msg], cwd=self.repo, env=dict(self.env, **env), check=True)

    def test_a_clean_push_passes(self):
        """Does a push of commits that hold no pattern run, from the repo, through `cd` and through `-C`?"""
        self.commit('b.txt', 'second\n', 'second')
        self.passes('git push origin main', cwd=self.repo)
        self.passes('cd repo && git push', f'git -C {self.repo} push -q origin main')

    def test_a_push_of_a_commit_that_holds_a_pattern_is_refused(self):
        """Is each part of an outgoing commit read: its message, author email, an added line, a file path?"""
        for name, args in {'message': ('m.txt', 'ok\n', f'thanks {FAKE_NAME}', {}),
                           'author': ('n.txt', 'ok\n', 'ok', {'GIT_AUTHOR_EMAIL': 'fake-user@example.org'}),
                           'added line': ('o.txt', f'host {FAKE_HOST}\n', 'ok', {}),
                           'file path': (f'{FAKE_HOST}.txt', 'ok\n', 'ok', {})}.items():
            with self.subTest(name):
                self.git('reset', '-q', '--hard', 'origin/main')
                self.commit(args[0], args[1], args[2], **args[3])
                self.refused('git push origin main', 'git push', cwd=self.repo, says='matches line')
                self.refused(f'git -C {self.repo} push', 'cd repo; git push --force-with-lease')

    def test_what_is_on_the_remote_already_and_removed_lines_are_not_read(self):
        """Is only what the push sends read: not an old commit already pushed, not a line it removes?"""
        self.commit('p.txt', f'{FAKE_NAME}\n', 'old')
        self.git('push', '-q', 'origin', 'main')
        self.commit('p.txt', 'gone\n', 'take it out')
        self.passes('git push origin main', cwd=self.repo)

    def test_a_push_it_cannot_read_is_refused(self):
        """Is a push refused when its commits cannot be read (no repo there) or are too big to read in time?"""
        os.makedirs(os.path.join(self.dir, 'plain'))
        self.refused('git push', cwd=os.path.join(self.dir, 'plain'), says='could not be read')
        self.g.SETTINGS['maxPushBytes'] = 10
        self.commit('q.txt', 'x' * 100 + '\n', 'big')
        self.refused('git push', cwd=self.repo, says='push fewer at once')

    def test_a_detached_head_or_sha_push_reads_what_it_sends(self):
        """Major: `git push origin HEAD:refs/heads/x` from a detached HEAD, or by sha, sent a commit no branch or tag
        holds, so it was not read."""
        self.git('checkout', '-q', '--detach')
        self.commit('d.txt', f'{FAKE_NAME}\n', 'detached')
        sha = self.git('rev-parse', 'HEAD').strip()
        self.refused('git push origin HEAD:refs/heads/x', f'git push origin {sha}:refs/heads/y',
                     f'git push origin +{sha[:10]}:x', cwd=self.repo)

    def test_a_clean_push_is_not_refused_over_other_local_refs(self):
        """Major: every local branch and tag that no remote holds was read, so a clean push was refused (and slow)
        over another branch, another worktree's branch or an old tag-only commit."""
        self.git('checkout', '-q', '-b', 'other')
        self.commit('e.txt', f'{FAKE_HOST}\n', 'other work')
        self.git('tag', 'old-tag')
        self.git('checkout', '-q', 'main')
        self.commit('f.txt', 'clean\n', 'clean')
        self.passes('git push origin main', 'git push origin HEAD', 'git push', 'git push origin main:main',
                    cwd=self.repo)
        self.refused('git push origin other', 'git push origin old-tag', 'git push --all origin',
                     'git push origin --tags', 'git push origin main other', cwd=self.repo)

    def test_a_push_to_another_remote_reads_what_that_remote_lacks(self):
        """Major: a commit on origin's tracking refs was taken as pushed, also by a push to another remote."""
        other = os.path.join(self.dir, 'other.git')
        self.git('init', '-q', '--bare', other, cwd=self.dir)
        self.git('remote', 'add', 'pub', other)
        self.commit('g.txt', 'ok\n', f'by {FAKE_NAME}')
        self.git('push', '-q', 'origin', 'main')
        self.passes('git push origin main', cwd=self.repo)
        self.refused('git push pub main', f'git push {other} main', 'git push pub', cwd=self.repo)

    def test_an_annotated_tag_push_reads_the_tag(self):
        """What a tag push sends: the tag's message and tagger, not only the commits."""
        self.git('tag', '-a', 'v2', '-m', f'release by {FAKE_NAME}')
        self.git('tag', '-a', 'v3', '-m', 'clean')
        self.refused('git push origin v2', 'git push origin tag v2', 'git push origin --tags',
                     'git push --follow-tags', cwd=self.repo)
        self.passes('git push origin v3', 'git push origin main', 'git push origin :old', cwd=self.repo)

    def test_pushes_to_unlisted_repos_pass_when_there_is_a_list(self):
        """Major (state lens): a push to a private repo was refused. With leakGuard.repos it is checked only when it
        goes to a listed GitHub repo; a remote that is no GitHub repo is no listed one."""
        self.commit('h.txt', f'{FAKE_NAME}\n', 'notes')
        self.g.SETTINGS['repos'] = ['o/public']
        self.passes('git push origin main', cwd=self.repo)
        self.git('remote', 'add', 'gpub', 'https://github.com/o/public.git')
        self.git('remote', 'add', 'gpriv', 'git@github.com:o/private.git')
        self.refused('git push gpub main', 'git push https://github.com/O/public main', cwd=self.repo)
        self.passes('git push gpriv main', cwd=self.repo)


class RoundOnePostTest(Case):
    """The repros of review round 1 of #1118 (lenses code and state); each one got the wrong answer at 852df457."""

    def setUp(self):
        super().setUp()
        self.write('body.md', f'Thanks to {FAKE_NAME}.\n')
        self.write('clean.md', 'Nothing personal here.\n')

    def test_a_post_behind_a_wrapper_with_options_is_read(self):
        """Blocker: `env -u NAME gh` (the fleet's way to run gh) and `sudo -u USER gh` took NAME or USER as the
        command, so nothing was checked."""
        body = f"--body 'hi {FAKE_NAME}'"
        self.refused(f'env -u GH_TOKEN gh pr comment 1 {body}', f'sudo -u bob gh pr comment 1 {body}',
                     f'env -u GH_TOKEN -u GITHUB_TOKEN GH_REPO=o/r gh pr comment 1 {body}',
                     f'env -i -C /tmp gh pr comment 1 {body}', f'sudo -E -u bob -- gh pr comment 1 {body}',
                     f'timeout -k 5 30 gh pr comment 1 {body}', f'timeout --signal=KILL 2m gh pr comment 1 {body}',
                     f'nice -n 5 gh pr comment 1 {body}', f'nice -5 gh pr comment 1 {body}',
                     f'echo 1 | xargs -I X gh pr comment X {body}', f'stdbuf -o L gh pr comment 1 {body}',
                     f'/usr/bin/env -u GH_TOKEN /usr/bin/gh pr comment 1 {body}',
                     f'env -u GH_TOKEN git commit -m "by {FAKE_NAME}"', f'sudo -u bob git -C . tag -m "{FAKE_HOST}" v1',
                     f'''bash -c "gh pr comment 1 --body '{FAKE_NAME}'"''',
                     f'''sh -ec "gh pr comment 1 --body '{FAKE_NAME}'"''',
                     f'''eval "gh pr comment 1 --body '{FAKE_NAME}'"''',
                     f"env -S 'gh pr comment 1' --body '{FAKE_NAME}'")
        self.passes('env -u GH_TOKEN gh pr comment 1 --body ok', 'sudo -u bob gh pr view 1')

    def test_a_body_file_named_in_other_ways_is_read(self):
        """Major: a body file on a backslash-continued line, in `$(cat f)`, and piped in (`cat f | gh ... -`)."""
        self.refused('gh pr create --title t \\\n  --body-file body.md', 'gh pr comment 1 --body "$(cat body.md)"',
                     'gh pr comment 1 --body "`cat body.md`"', 'cat body.md | gh pr comment 1 --body-file -',
                     'cat body.md | tee /dev/null | gh pr comment 1 -F -',
                     'env -u GH_TOKEN gh pr create -t t \\\n  -F body.md')
        self.passes('gh pr create --title t \\\n  --body-file clean.md', 'cat clean.md | gh pr comment 1 --body-file -',
                    'gh pr comment 1 --body "$(cat clean.md)"')

    def test_only_the_folder_word_of_cd_and_git_C_is_left_out(self):
        """Major: the folder of `cd DIR` / `git -C DIR` was taken out of the whole text, so the same private path in
        the body passed."""
        place = f'/srv/{FAKE_PLACE}'
        self.refused(f"cd {place}/work && gh pr comment 1 --body 'log: {place}/work/out.log'",
                     f"git -C {place}/r commit -m 'see {place}/r/notes'")
        self.passes(f'cd {place}/work && gh pr comment 1 --body ok', f"git -C {place}/r commit -m 'fix'")

    def test_commands_that_post_nothing_pass(self):
        """Major: read-only gh, the other parts of a command, `>` targets and output under a private path were read
        as a post."""
        self.write('out.txt', f'{FAKE_NAME}\n')
        self.passes(f"gh pr view 3 --json body | grep -c '{FAKE_NAME}'", f"grep -rn '{FAKE_NAME}' ~/notes; gh pr list",
                    'gh run view 1 --log > out.txt', f'gh pr view 1 --json body > /srv/{FAKE_PLACE}/b.json',
                    'gh pr diff 1 > out.txt', f'gh api repos/o/r/pulls/1 > /srv/{FAKE_PLACE}/p.json',
                    f'gh api -X GET repos/o/r/issues -f q={FAKE_PLACE}', 'gh pr checks 1 --watch',
                    f'env -u GH_TOKEN gh search issues "{FAKE_NAME}"', 'gh run download 1 -D out',
                    f'gh pr comment 1 --body ok > /srv/{FAKE_PLACE}/log.txt')
        self.refused(f'gh api -X PATCH repos/o/r/issues/1 -f title=x -f body="{FAKE_NAME}"',
                     'gh api repos/o/r/issues --input body.md', f"gh issue comment 1 -b '{FAKE_NAME}' | grep x")

    def test_a_clean_file_in_a_folder_whose_path_matches_passes(self):
        """Major: the body file's path was matched as well as its text, so a clean body in a private folder was
        refused, naming a file that holds nothing."""
        folder = os.path.join(self.dir, f'{FAKE_PLACE}-x')
        self.write(f'{FAKE_PLACE}-x/clean.md', 'Nothing personal here.\n')
        self.write(f'{FAKE_PLACE}-x/body.md', f'by {FAKE_NAME}\n')
        self.passes('gh pr comment 1 --body-file clean.md', 'git commit -F clean.md', cwd=folder)
        self.passes(f'gh pr comment 1 --body-file {folder}/clean.md')
        self.refused('gh pr comment 1 --body-file body.md', cwd=folder)

    def test_a_here_string_or_arithmetic_starts_no_heredoc(self):
        """Minor: `<<<` and `$((1<<2))` were read as a heredoc start, so every line after them was taken as text."""
        self.refused('grep x <<< hello\ngh pr comment 1 --body-file body.md',
                     'echo $((1<<2))\ngh pr comment 1 --body-file body.md',
                     "echo '<<E'\ngh pr comment 1 --body-file body.md")
        self.refused(f"gh pr comment 1 -F - <<'E'\nby {FAKE_NAME}\nE\nls")

    def test_git_C_commit_F_reads_the_file_in_that_folder(self):
        """Minor: `git -C DIR commit -F msg` read msg in the cwd, not in DIR."""
        self.write('sub/msg.txt', f'Fix it on {FAKE_HOST}\n')
        self.write('msg.txt', 'clean\n')
        self.refused('git -C sub commit -F msg.txt', 'cd sub && git commit -F msg.txt')
        self.passes('git commit -F msg.txt')

    def test_url_encoded_text_is_read(self):
        """Minor: `%20`-encoded text passed (split strings stay out of reach: LEAK.md names them)."""
        self.refused("gh api repos/o/r/issues -f body='Alex%20Example'",
                     "curl -d 'body=Alex%20Example' https://api.github.com/repos/o/r/issues")

    def test_only_listed_repos_are_checked_when_there_is_a_list(self):
        """Major (state lens): posts to a private repo, which may hold personal details by design, were refused.
        With leakGuard.repos, only those repos are checked; a post whose repo is not known is checked."""
        self.g.SETTINGS['repos'] = ['o/public', 'pub-org/*']
        body = f"--body '{FAKE_NAME}'"
        self.refused(f'gh pr comment 1 -R o/public {body}', f'gh pr comment 1 --repo pub-org/x {body}',
                     f'GH_REPO=o/public gh pr comment 1 {body}', f'gh api repos/o/public/issues -f body={FAKE_PLACE}',
                     f'gh pr comment 1 {body}',   # no remote here: not known, checked
                     'gh gist create body.md')
        self.passes(f'gh pr comment 1 -R o/private {body}', f'gh api repos/o/private/issues -f body={FAKE_PLACE}',
                    f'gh pr comment 1 -R https://github.com/o/private {body}')


class HookTest(Case):
    def event(self, cmd, tool='Bash'):
        return {'hook_event_name': 'PreToolUse', 'tool_name': tool, 'tool_input': {'command': cmd},
                'session_id': 't', 'cwd': self.dir}

    def test_the_hook_refuses_with_exit_2_and_passes_with_0(self):
        """As Claude Code runs it: exit 2 and a quiet reason for a leak, exit 0 for the rest and other tools?"""
        r = fx.run_hook('leak-guard', self.event(f"gh pr comment 1 --body '{FAKE_NAME}'"), self.machine)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('Refused by leak-guard', r.stderr)
        self.assertQuiet(r.stderr + r.stdout)
        r = fx.run_hook('leak-guard', self.event(f"gh pr comment 1 --body '{FAKE_NAME}'", 'Monitor'), self.machine)
        self.assertEqual(r.returncode, 2, r.stderr)
        for ev in (self.event("gh pr comment 1 --body 'ok'"), self.event(FAKE_NAME, 'Write')):
            r = fx.run_hook('leak-guard', ev, self.machine)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))

    def test_through_the_settings_entry_wrapper_forms_are_refused(self):
        """The blocker as Claude Code runs it: `env -u GH_TOKEN gh` with a leak exits 2."""
        r = fx.run_hook('leak-guard', self.event(f"env -u GH_TOKEN gh pr comment 1 --body '{FAKE_NAME}'"),
                        self.machine)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertQuiet(r.stderr + r.stdout)

    def test_broken_input_refuses(self):
        """Closed: does input the hook cannot read refuse, without saying more than the error's kind?"""
        r = fx.run_hook('leak-guard', '{not json', self.machine)
        self.assertEqual(r.returncode, 2)
        self.assertIn('the guard failed (JSONDecodeError)', r.stderr)


class PatternFileTest(Case):
    def test_format(self):
        """Plain text in any case and as a whole word, re: lines as regular expressions, comments skipped?"""
        pats = leak.parse(f'# {FAKE_HOST}\n{FAKE_NAME}\n\nre:^x\\d$\nc++\n')
        self.assertEqual([n for n, _ in pats.rows], [2, 4, 5])
        self.assertIsNone(pats.first(f'only {FAKE_HOST}'))
        self.assertEqual(pats.first(f'hi {FAKE_NAME.upper()}!'), 2)
        self.assertIsNone(pats.first(f'{FAKE_NAME}s'))
        self.assertEqual(pats.first('x7'), 4)
        self.assertEqual(pats.first('use c++ here'), 5)

    def test_self_check_says_where_the_file_stands(self):
        """Does self-check name a missing file, a broken one, and one inside a git checkout, never a pattern?"""
        cli = fx.load_hook('agent-guards', self.machine)
        for text, want, problems in ((None, 'no pattern file', 0), ('re:(\n', 'line 1 of the pattern file', 1),
                                     (PATTERNS, '4 pattern(s)', 0)):
            with self.subTest(want):
                if text is None:
                    os.unlink(self.pfile)
                else:
                    self.write('private/leak-patterns', text)
                notes = []
                got = cli.leak_notes(self.pfile, notes)
                self.assertEqual(len(got), problems)
                self.assertIn(want, ' '.join(notes + got))
        for text in ('', '# only a comment\n\n'):   # fix round 1 of #1118: checks nothing, as no file does
            with self.subTest(text=text):
                self.write('private/leak-patterns', text)
                notes = []
                self.assertEqual(cli.leak_notes(self.pfile, notes), [])
                self.assertIn('WARNING: leak-guard: the pattern file', ' '.join(notes))
                self.assertIn('holds no pattern', ' '.join(notes))
        notes = []
        cli.leak_notes(self.pfile, notes, ['o/public'])
        self.assertIn('posts and pushes to o/public are checked', ' '.join(notes))
        self.write('private/leak-patterns', PATTERNS)
        os.makedirs(os.path.join(self.dir, 'private', '.git'))
        got = cli.leak_notes(self.pfile, [])
        self.assertIn('inside the git checkout', ' '.join(got))
        self.assertQuiet(' '.join(got))


GH_STAND_IN = '''import json, sys
args = sys.argv[1:]
path = args[args.index('api') + 1 + (args[args.index('api') + 1] == '--paginate')]
data = json.load(open(sys.argv[0] + '.json'))
for key, rows in data.items():
    if path.split('repos/o/r/')[1].startswith(key):
        for row in rows:
            print(json.dumps(row))
        break
if 'fail' in data:
    print('HTTP 404', file=sys.stderr)
    sys.exit(1)
'''


class ScanTest(Case):
    def scan(self, data):
        gh = self.write('gh', '#!' + sys.executable + '\n' + GH_STAND_IN)
        os.chmod(gh, 0o755)
        self.write('gh.json', json.dumps(data))
        cli = fx.load_hook('leak-scan', self.machine)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['--repo', 'o/r', '--gh', gh, '--patterns', self.pfile])
        return code, out.getvalue(), err.getvalue()

    def test_old_text_is_reported_by_url_and_line_only(self):
        """Does the scan find a pattern in each kind of text, name the URL and the line number, and not the text?"""
        code, out, err = self.scan({
            'issues?': [['https://x/1', 'PR title and body', f'by {FAKE_NAME}'], ['https://x/2', 'issue', 'clean']],
            'issues/comments': [['https://x/3', 'comment', f'on {FAKE_HOST}']],
            'pulls/comments': [], 'releases': [],
            'commits': [['https://x/c', 'commit', 'fix\nAgent\nfake-user@example.org\nAgent\na@example.com']]})
        self.assertEqual(code, 1, err)
        self.assertIn('MATCH https://x/1 (PR title and body): pattern line 3', out)
        self.assertIn('MATCH https://x/3 (comment): pattern line 4', out)
        self.assertIn('MATCH https://x/c (commit): pattern line 5', out)
        self.assertNotIn('https://x/2', out)
        self.assertIn('3 match(es) in 4 texts', out)
        self.assertQuiet(out + err)

    def test_clean_and_failing_scans(self):
        """Exit 0 when clean, 2 when gh fails or there are no patterns: never a quiet "clean" it did not read?"""
        self.assertEqual(self.scan({'issues?': [['https://x/2', 'issue', 'clean']]})[0], 0)
        code, _, err = self.scan({'fail': []})
        self.assertEqual(code, 2)
        self.assertIn('failed (exit 1)', err)
        os.unlink(self.pfile)
        self.assertEqual(self.scan({})[0], 2)


if __name__ == '__main__':
    unittest.main()
