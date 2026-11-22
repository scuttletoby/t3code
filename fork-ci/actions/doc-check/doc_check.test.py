"""doc_check.py: budgets, history, links and dated reports, on real git repositories; config missing, empty, broken
or loosened by the change itself; and the fork's own general agent rules page (docs/fork/agent-rules.md) passes.
Run: python3 fork-ci/actions/doc-check/doc_check.test.py
"""
import contextlib, importlib.util, io, json, os, re, subprocess, sys, tempfile, unittest
from unittest import mock

sys.dont_write_bytecode = True   # leave no __pycache__ in the action folder

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
_spec = importlib.util.spec_from_file_location('doc_check_api', os.path.join(HERE, 'doc_check.py'))
DC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(DC)
ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
       'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}


def lines(n, width=10):
    return ''.join('x' * width + '\n' for _ in range(n))


class Repo(unittest.TestCase):
    """Each test gets a fresh repository with one commit on main; cwd is the repository while a check runs."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('GITHUB_ACTIONS', None)
        self.tmp = tempfile.TemporaryDirectory(prefix='doc-check-')
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, 'repo')   # a folder beside it is outside the repository
        os.mkdir(self.root)
        self.old_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.old_cwd)
        os.chdir(self.root)
        self.git('init', '-q', '-b', 'main')
        self.commit({'README.md': 'hello\n'}, 'base')

    def git(self, *args):
        return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout.strip()

    def write(self, files):
        for path, text in files.items():
            full = os.path.join(self.root, path)
            if text is None:
                os.remove(full)
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'w', encoding='utf-8') as f:
                f.write(text)

    def commit(self, files, msg='change'):
        self.write(files)
        self.git('add', '-A')
        self.git('commit', '-q', '--allow-empty', '-m', msg)
        return self.git('rev-parse', 'HEAD')

    def branch(self, files):
        """A change on a branch: returns main's commit (the base)."""
        base = self.git('rev-parse', 'main')
        self.git('checkout', '-q', '-b', 'change')
        self.commit(files)
        return base

    def run_check(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = DC.main(list(args))
        return code, out.getvalue()


class Config(Repo):
    def test_missing_config_means_defaults(self):
        base = self.branch({'AGENTS.md': lines(151)})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 1, out)
        self.assertIn('no .github/doc-check.json at the base: defaults', out)
        self.assertIn('DOC  AGENTS.md: 151 lines', out)

    def test_empty_config_file_means_defaults(self):
        self.commit({'.github/doc-check.json': ''})
        base = self.branch({'AGENTS.md': lines(151)})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 1, out)
        self.assertIn('is empty: defaults', out)

    def test_empty_object_means_defaults(self):
        self.commit({'.github/doc-check.json': '{}\n'})
        base = self.branch({'CLAUDE.md': lines(41)})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 1, out)
        self.assertIn('budget 40 lines', out)

    def test_broken_config_is_an_error_not_a_pass(self):
        for text, want in [('{not json', 'not valid JSON'), ('[]', 'must be a JSON object'),
                           ('{"budget": []}', 'unknown key(s) budget'),
                           ('{"budgets": [{"glob": "A.md", "lines": 0, "bytes": 1}]}', 'budgets[0]'),
                           ('{"warn_at": 2}', 'warn_at'), ('{"history": [{"pattern": "(", "why": "x"}]}', 'history[0]'),
                           ('{"links": "yes"}', 'links')]:
            with self.subTest(text=text):
                self.write({'.github/doc-check.json': text})
                code, out = self.run_check('--all')
                self.assertEqual(code, 2, out)
                self.assertIn(want, out)

    def test_a_change_cannot_loosen_its_own_budget(self):
        base = self.branch({'AGENTS.md': lines(151),
                            '.github/doc-check.json': json.dumps({'budgets': [{'glob': '**', 'lines': 999, 'bytes': 99999}]})})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 1, out)
        self.assertIn('no .github/doc-check.json at the base', out)

    def test_project_budget_from_config(self):
        self.commit({'.github/doc-check.json': json.dumps(
            {'_comment': 'a project file', 'budgets': [{'glob': 'ENGINEERING.md', 'lines': 5, 'bytes': 9999}]})})
        base = self.branch({'ENGINEERING.md': lines(6), 'AGENTS.md': lines(200)})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 1, out)
        self.assertIn('DOC  ENGINEERING.md: 6 lines', out)
        self.assertNotIn('AGENTS.md:', out)   # the config's budgets replace the defaults

    def test_missing_base_or_no_merge_base_is_an_error(self):
        code, out = self.run_check('--base', '')
        self.assertEqual(code, 2, out)
        code, out = self.run_check('--base', '0' * 40)
        self.assertEqual(code, 2, out)
        self.assertIn('not a commit here', out)


class Budgets(Repo):
    def test_file_over_budget_fails_with_size(self):
        base = self.branch({'AGENTS.md': lines(10, width=1300)})   # 10 lines, 13010 B > 12000 B
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 1, out)
        self.assertIn('DOC  AGENTS.md: 10 lines / 13010 B, budget 150 lines / 12000 B (AGENTS.md)', out)

    def test_folder_agents_file_has_the_smaller_budget(self):
        base = self.branch({'pkg/AGENTS.md': lines(101), 'AGENTS.md': lines(101)})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 1, out)
        self.assertIn('DOC  pkg/AGENTS.md: 101 lines / 1111 B, budget 100 lines', out)
        self.assertNotIn('DOC  AGENTS.md', out)

    def test_near_budget_warns_and_says_this_change_moved_it(self):
        self.commit({'AGENTS.md': lines(100)})
        base = self.branch({'AGENTS.md': lines(140)})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 0, out)
        self.assertIn('WARN AGENTS.md: 140 lines', out)
        self.assertIn('this change moved it past 90%', out)

    def test_already_near_budget_warns_without_blame(self):
        self.commit({'AGENTS.md': lines(140)})
        base = self.branch({'AGENTS.md': lines(141)})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 0, out)
        self.assertIn('WARN AGENTS.md: 141 lines', out)
        self.assertNotIn('moved it past', out)

    def test_unchanged_files_and_a_newer_main_are_not_this_change(self):
        self.commit({'CLAUDE.md': lines(60)})            # over budget, but already on main
        base_old = self.branch({'docs/a.md': 'ok\n'})
        self.git('checkout', '-q', 'main')
        self.commit({'pkg/AGENTS.md': lines(300)})       # main moves on
        self.git('checkout', '-q', 'change')
        code, out = self.run_check('--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertIn('1 file(s), 0 problem(s)', out)
        self.assertTrue(base_old)

    def test_github_annotations(self):
        base = self.branch({'AGENTS.md': lines(151)})
        with mock.patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}):
            code, out = self.run_check('--base', base)
        self.assertEqual(code, 1)
        self.assertIn('::error file=AGENTS.md::151 lines', out)


class History(Repo):
    def test_history_in_an_instruction_file(self):
        base = self.branch({'AGENTS.md': 'Rules\n\nUpdated: 2026-01-02\n', 'docs/notes.md': 'Updated: 2026-01-02\n'})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 1, out)
        self.assertIn('DOC  AGENTS.md:3: dated "Updated" banner', out)
        self.assertNotIn('docs/notes.md', out)   # not an instruction file

    def test_live_claim_and_changelog_block(self):
        base = self.branch({'CLAUDE.md': 'v2 is live on prod\n<!-- RELEASE 1.2 -->\n'})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 1, out)
        self.assertIn('CLAUDE.md:1: "live/deployed on" claim', out)
        self.assertIn('CLAUDE.md:2: changelog block', out)

    def test_dated_report_only_when_added(self):
        self.commit({'docs/REVIEW-2026-01-01.md': 'old\n'})
        base = self.branch({'docs/REVIEW-2026-01-01.md': 'edited\n', 'docs/HANDOFF-2026-02-03.md': 'new\n'})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 1, out)
        self.assertIn('DOC  docs/HANDOFF-2026-02-03.md: dated report', out)
        self.assertNotIn('REVIEW-2026-01-01', out)


class Links(Repo):
    def test_broken_link_fails_with_its_line(self):
        base = self.branch({'docs/a.md': 'See [b](b.md) and [gone](missing.md#top).\n\n[x](<sub dir/nope.md>)\n',
                            'docs/b.md': 'b\n'})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 1, out)
        self.assertIn('DOC  docs/a.md:1: broken link missing.md: no such file', out)
        self.assertIn('DOC  docs/a.md:3: broken link sub dir/nope.md: no such file', out)
        self.assertNotIn('broken link b.md', out)

    def test_links_that_are_fine(self):
        text = ('[web](https://example.com/x) [mail](mailto:a@b) [anchor](#top) [root](/README.md)\n'
                '[folder](../docs/) [space](<sub dir/c.md>) [enc](sub%20dir/c.md) [img](sub%20dir/c.md "title")\n'
                '```\n[in a fence](nowhere.md)\n```\n')
        base = self.branch({'docs/a.md': text, 'docs/sub dir/c.md': 'c\n'})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 0, out)

    def test_link_out_of_the_repository_is_broken(self):
        base = self.branch({'a.md': '[up](../outside.md)\n'})
        open(os.path.join(self.tmp.name, 'outside.md'), 'w').close()
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 1, out)
        self.assertIn('points outside the repository', out)

    def test_links_off_in_config(self):
        self.commit({'.github/doc-check.json': '{"links": false}'})
        base = self.branch({'a.md': '[gone](missing.md)\n'})
        self.assertEqual(self.run_check('--base', base)[0], 0)

    def test_all_mode_reads_every_tracked_file_and_the_checkout_config(self):
        self.commit({'docs/a.md': '[gone](missing.md)\n', 'vendor/x.md': '[gone](missing.md)\n',
                     '.github/doc-check.json': '{"exclude": ["vendor/**"]}'})
        code, out = self.run_check('--all')
        self.assertEqual(code, 1, out)
        self.assertIn('docs/a.md:1: broken link', out)
        self.assertNotIn('vendor/x.md', out)
        self.assertIn('rules from .github/doc-check.json', out)

    def test_not_utf8_warns_and_rerun_is_identical(self):
        base = self.branch({'a.md': 'ok\n'})
        with open(os.path.join(self.root, 'a.md'), 'wb') as f:
            f.write(b'\xff\xfe bad\n')
        first = self.run_check('--base', base)
        self.assertEqual(first[0], 0, first[1])
        self.assertIn('WARN a.md: cannot read it', first[1])
        self.assertEqual(first, self.run_check('--base', base))


class AgentRulesPage(unittest.TestCase):
    """docs/fork/agent-rules.md: the general rules every project links to. Small, every link resolves, and it names
    nothing personal (the fork is public): no home paths, machine ids, addresses or account names."""
    PAGE = 'docs/fork/agent-rules.md'

    def test_page_passes_doc_check_and_stays_small(self):
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(REPO)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = DC.main(['--config', '/nonexistent/doc-check.json', '--files', self.PAGE])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn('0 problem(s)', out.getvalue())
        self.assertLessEqual(os.path.getsize(os.path.join(REPO, self.PAGE)), 6500)

    def test_page_names_nothing_personal(self):
        with open(os.path.join(REPO, self.PAGE), encoding='utf-8') as f:
            text = f.read()
        for rx in [r'/home/', r'~/\.', r'\bVM ?\d{2,}', r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', r'@[a-z0-9-]+\.[a-z]',
                   r'scuttletoby', r'skyrpg', r'\bOpus\b', r'\bFable\b', r'\bSonnet\b']:
            self.assertIsNone(re.search(rx, text, re.I), rx)


if __name__ == '__main__':
    unittest.main()
