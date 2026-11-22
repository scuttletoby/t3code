"""doc_check.py: budgets, history, links and dated reports, on real git repositories; config missing, empty, broken
or loosened by the change itself; docs removed while still linked; symlinks, bytes that are not UTF-8 and CRLF; and
the fork's own general agent rules page (docs/fork/agent-rules.md) passes.
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

    def test_link_in_inline_code_or_with_a_query_and_a_fence_in_a_list_item(self):
        text = ('Write `[x](nowhere.md)` or ``[y](`gone`.md)``; see [b](b.md?plain=1#L3).\n\n'
                '- a step:\n\n    ```md\n    [z](missing.md)\n    ```\n\n[real](missing.md)\n')
        base = self.branch({'docs/a.md': text, 'docs/b.md': 'b\n'})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 1, out)
        self.assertIn('DOC  docs/a.md:9: broken link missing.md', out)
        self.assertIn('1 problem(s)', out)   # not the inline code, the query or the fenced one

    def test_a_fence_opens_only_with_0_to_3_spaces_of_indent(self):
        # CommonMark: indented 4 or more (a tab is 4), or with a backtick after a backtick fence, it is no fence, so
        # the links after it are still checked; inside a list item the indent counts from the item's content.
        for text in ['Shown:\n\n    ```\n\n[gone](nope.md)\n', '\t~~~\n[gone](nope.md)\n',
                     '- item\n\n        ```\n[gone](nope.md)\n', '```x```\n[gone](nope.md)\n']:
            with self.subTest(text=text):
                self.write({'docs/a.md': text})
                code, out = self.run_check('--files', 'docs/a.md')
                self.assertEqual(code, 1, out)
                self.assertIn('broken link nope.md: no such file', out)
        text = '1. step\n\n   ```\n   [in](nope.md)\n   ```\n2. next\n\n   ~~~\n   [in](nope.md)\n[after](nope.md)\n'
        self.write({'docs/a.md': text})   # a fence in a list item ends with the item
        code, out = self.run_check('--files', 'docs/a.md')
        self.assertIn('DOC  docs/a.md:10: broken link nope.md', out)
        self.assertIn('1 problem(s)', out)

    def test_an_unmatched_backtick_run_hides_no_link(self):
        # #947 item 3: inline code never starts in the middle of a run of backticks, so a run with no closing run of
        # the same length is text and the link after it is still checked.
        for text in ['`` [g](nope.md) `\n', 'close it with ``` and see [g](nope.md) and `x`\n']:
            with self.subTest(text=text):
                self.write({'docs/a.md': text})
                code, out = self.run_check('--files', 'docs/a.md')
                self.assertEqual(code, 1, out)
                self.assertIn('DOC  docs/a.md:1: broken link nope.md: no such file', out)

    def test_a_fence_on_a_list_items_own_line(self):
        # #947 item 4: "- ```sh" opens a fence in the item, so its closing line closes it (and opens nothing).
        for marker in ['-', '1.', '10)']:
            pad = ' ' * (len(marker) + 1)
            text = f'{marker} ```sh\n{pad}[in](nope.md)\n{pad}```\n{pad}[after](nope.md)\n'
            with self.subTest(text=text):
                self.write({'docs/a.md': text})
                code, out = self.run_check('--files', 'docs/a.md')
                self.assertEqual(code, 1, out)
                self.assertIn('DOC  docs/a.md:4: broken link nope.md', out)
                self.assertIn('1 problem(s)', out)   # not the fenced one

    def test_a_link_in_indented_code_is_skipped(self):
        # #947 item 5: as CommonMark reads it, a line 4 or more past its list item's content (or the margin), after a
        # blank line, is indented code: no link. Without the blank line it continues the paragraph: a link.
        for text in ['- item\n\n      ```\n      [in](nope.md)\n      ```\n\n  [after](nope.md)\n',
                     '1. item\n\n       [in](nope.md)\n\n       [in](nope.md)\n[after](nope.md)\n',
                     'Shown:\n\n    [in](nope.md)\n\t[in](nope.md)\n\n[after](nope.md)\n']:
            with self.subTest(text=text):
                self.write({'docs/a.md': text})
                code, out = self.run_check('--files', 'docs/a.md')
                self.assertEqual(code, 1, out)
                self.assertIn(f'DOC  docs/a.md:{text.count(chr(10))}: broken link nope.md', out)
                self.assertIn('1 problem(s)', out)
        for text in ['- item\n      [in](nope.md)\n', 'para\n    [in](nope.md)\n']:
            with self.subTest(text=text):
                self.write({'docs/a.md': text})
                code, out = self.run_check('--files', 'docs/a.md')
                self.assertIn('DOC  docs/a.md:2: broken link nope.md', out)

    def test_a_list_item_stays_open_on_a_lazy_line_and_every_marker_on_a_line_counts(self):
        # #1020 round 1: an unindented line that continues an item's paragraph (lazy) keeps the item open, and in
        # "- - item" both markers open an item, so the item's next paragraph is text, not indented code: a link. A
        # lazy line opens nothing; an empty item or one numbered from 2 cannot interrupt a paragraph; a line 4 in
        # after a closed list is code, so "10." opens a list. (commonmark.js 0.31.2 reads each case so.)
        for text in ['1. Step one that wraps\nonto the next line.\n\n    More about step one, see [g](nope.md).\n',
                     '- item\nlazy\n\n    [x](nope.md)\n',
                     '1. a\n   - b\nlazy\n\n      [x](nope.md)\n',
                     '- - nested item\n\n      more text [x](nope.md)\n',
                     '1. - item\n\n       [x](nope.md)\n',
                     '- - item\nlazy\n\n      [x](nope.md)\n',
                     'para\n2) ```\n   [x](nope.md)\n',
                     '  1. [g](a.md)\n    ```sh\n      1. [x](nope.md)\n',
                     '   * ```\n    ```sh\n 10. text\n\n       - text [x](nope.md)\n',
                     'para\n-\n  [x](nope.md)\n']:
            with self.subTest(text=text):
                self.write({'docs/a.md': text})
                code, out = self.run_check('--files', 'docs/a.md')
                self.assertEqual(code, 1, out)
                self.assertIn(f'DOC  docs/a.md:{text.count(chr(10))}: broken link nope.md', out)
        # A heading, a thematic break or a fence is no lazy line: it ends the item, and "- - -" is a thematic
        # break, not three items; the indented line after it is code (no blank needed after a heading or a fence).
        for text in ['- item\n# heading\n\n    [in](nope.md)\n', '- item\n---\n\n    [in](nope.md)\n',
                     '- item\n```\n```\n\n    [in](nope.md)\n', '- - -\n\n    [in](nope.md)\n',
                     '- item\n\nnot lazy\n\n    [in](nope.md)\n', '# h\n    [in](nope.md)\n',
                     '```\n```\n    [in](nope.md)\n']:
            with self.subTest(text=text):
                self.write({'docs/a.md': text})
                code, out = self.run_check('--files', 'docs/a.md')
                self.assertEqual(code, 0, out)

    def test_a_block_quote_line_is_no_paragraph_the_next_line_goes_on(self):
        # #1020 narrow check: this checker does not model block quotes, so a ">" line must not count as a paragraph
        # that the next line continues: "2." right below a quote opens an item (commonmark.js 0.31.2 renders the link).
        for text in ['> **Note:** this needs a recent runtime.\n2. Run it.\n\n    See [setup](nope.md) for flags.\n',
                     '> quote\n    [x](nope.md)\n']:
            with self.subTest(text=text):
                self.write({'docs/a.md': text})
                code, out = self.run_check('--files', 'docs/a.md')
                self.assertEqual(code, 1, out)
                self.assertIn(f'DOC  docs/a.md:{text.count(chr(10))}: broken link nope.md', out)

    def test_a_link_that_is_no_path_is_a_broken_link_not_a_traceback(self):
        self.commit({'docs/a.md': '[nul](x%00.md)\n', 'docs/b.md': 'b\n'})
        base = self.branch({'docs/b.md': None})   # the removed-link scan reads the untouched a.md
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 0, out)            # an old broken link is not this change
        code, out = self.run_check('--files', 'docs/a.md')
        self.assertEqual(code, 1, out)
        self.assertIn('DOC  docs/a.md:1: broken link x%00.md: not a path (ValueError)', out)


class Removed(Repo):
    """#908: a change that deletes or renames a doc another file links to fails, though it did not touch that file."""

    def test_renamed_or_deleted_doc_still_linked_from_an_untouched_file(self):
        self.commit({'docs/a.md': 'See [b](b.md) and [guide](guide/).\n', 'docs/b.md': 'b\n',
                     'docs/guide/one.md': '1\n', 'docs/c.md': '[ok](a.md)\n'})
        self.git('checkout', '-q', '-b', 'change')
        self.git('mv', 'docs/b.md', 'docs/b2.md')
        self.git('rm', '-q', 'docs/guide/one.md')
        self.git('commit', '-q', '-m', 'rename and delete')
        code, out = self.run_check('--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('DOC  docs/a.md:1: broken link b.md: this change removed docs/b.md', out)
        self.assertIn('DOC  docs/a.md:1: broken link guide/: this change removed docs/guide', out)
        self.assertIn('2 problem(s)', out)

    def test_an_old_broken_link_in_an_untouched_file_is_not_this_change(self):
        self.commit({'docs/old.md': '[gone](never.md)\n', 'docs/b.md': 'b\n'})
        base = self.branch({'docs/b.md': None})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 0, out)

    def test_a_deleted_doc_named_like_a_pathspec_magic_word(self):
        # GIT_LITERAL_PATHSPECS: git reads ":(top)A.md" as the name it is, not as "A.md" from the top.
        self.commit({'a.md': '[a](%3A%28top%29A.md)\n', ':(top)A.md': 'a\n'})
        base = self.branch({':(top)A.md': None})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 1, out)
        self.assertIn('DOC  a.md:1: broken link %3A%28top%29A.md: this change removed :(top)A.md', out)

    def untouched_symlink_loses_its_target(self, how):
        """Base: docs/AGENTS.md and CLAUDE.md -> docs/AGENTS.md; the change touches only the target."""
        self.commit({'docs/AGENTS.md': 'rules\n'})
        os.symlink('docs/AGENTS.md', 'CLAUDE.md')
        self.commit({})
        self.git('checkout', '-q', '-b', 'change')
        how()
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'change')
        code, out = self.run_check('--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r'DOC  CLAUDE.md: a symlink that points [a-z ]+: make it a file: '
                              r'this change removed docs/AGENTS.md')

    def test_the_target_of_an_untouched_symlink_deleted(self):
        self.untouched_symlink_loses_its_target(lambda: os.remove('docs/AGENTS.md'))

    def test_the_target_of_an_untouched_symlink_renamed(self):
        self.untouched_symlink_loses_its_target(lambda: os.rename('docs/AGENTS.md', 'docs/RULES.md'))

    def test_the_folder_of_an_untouched_symlinks_target_turned_into_a_symlink_out(self):
        away = os.path.join(self.tmp.name, 'away')
        def how():
            os.rename('docs', away)
            os.symlink(away, 'docs')
        self.untouched_symlink_loses_its_target(how)

    def test_a_symlink_already_dangling_on_the_base_is_not_this_change(self):
        os.symlink('missing.md', 'CLAUDE.md')
        self.commit({'docs/b.md': 'b\n'})
        base = self.branch({'docs/b.md': None})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 0, out)


    def through_a_folder_symlink(self, how, want, link=False):
        """#947 item 1. Base: real/A.md, docs -> real, an untouched CLAUDE.md -> docs/A.md (or, link: an untouched
        a.md that links to docs/A.md); the change touches only real/ or docs."""
        self.commit({'real/A.md': 'rules\n', 'a.md': '[a](docs/A.md)\n' if link else 'a\n'})
        os.symlink('real', 'docs')
        if not link:
            os.symlink('docs/A.md', 'CLAUDE.md')
        self.commit({})
        self.git('checkout', '-q', '-b', 'change')
        how()
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'change')
        code, out = self.run_check('--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn(want, out)
        self.assertIn('1 problem(s)', out)

    def test_the_target_of_an_untouched_symlink_through_a_folder_symlink_deleted(self):
        want = 'DOC  CLAUDE.md: a symlink that points to no file: make it a file: this change removed real/A.md'
        self.through_a_folder_symlink(lambda: os.remove('real/A.md'), want)

    def test_the_target_of_an_untouched_symlink_through_a_folder_symlink_renamed(self):
        want = 'DOC  CLAUDE.md: a symlink that points to no file: make it a file: this change removed real/A.md'
        self.through_a_folder_symlink(lambda: os.rename('real', 'real2'), want)

    def test_a_folder_symlink_retargeted_to_nothing_or_out_of_the_repository(self):
        def to(target):
            def how():
                os.remove('docs')
                os.symlink(target, 'docs')
            return how
        with open(os.path.join(self.tmp.name, 'A.md'), 'w') as f:
            f.write('x\n')
        for target, why in [('nowhere', 'points to no file'), (self.tmp.name, 'points outside the repository')]:
            with self.subTest(target=target):
                self.setUp()
                self.through_a_folder_symlink(to(target), f'DOC  CLAUDE.md: a symlink that {why}: make it a file: '
                                                          'this change changed the symlink docs')

    def test_a_folder_symlink_removed(self):
        self.through_a_folder_symlink(lambda: os.remove('docs'),
                                      'DOC  CLAUDE.md: a symlink that points to no file: make it a file: '
                                      'this change removed docs')

    def test_a_link_through_a_folder_symlink(self):
        self.through_a_folder_symlink(lambda: os.remove('real/A.md'),
                                      'DOC  a.md:1: broken link docs/A.md: this change removed real/A.md', link=True)
        self.setUp()
        def how():
            os.remove('docs')
            os.symlink('nowhere', 'docs')
        self.through_a_folder_symlink(how, 'DOC  a.md:1: broken link docs/A.md: this change changed the symlink docs',
                                      link=True)

    def test_already_dangling_on_the_base_or_a_loop_through_a_changed_symlink_is_not_this_change(self):
        # docs -> real is retargeted to real2 (which has A.md); CLAUDE.md -> docs/missing.md pointed to no file on the
        # base already, a.md's link too, and loop.md loops on both sides.
        self.commit({'real/A.md': 'a\n', 'real2/A.md': 'a\n', 'a.md': '[m](docs/missing.md) [l](loop.md)\n'})
        os.symlink('real', 'docs')
        os.symlink('docs/missing.md', 'CLAUDE.md')
        os.symlink('loop.md', 'loop.md')
        os.symlink('docs/A.md', 'AGENTS.md')
        self.commit({})
        base = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-q', '-b', 'change')
        os.remove('docs')
        os.symlink('real2', 'docs')
        os.remove('real/A.md')
        self.commit({})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 0, out)


    def test_git_failing_on_the_base_tree_is_an_error_not_a_pass(self):
        self.commit({'real/A.md': 'a\n', 'a.md': '[a](docs/A.md)\n'})
        os.symlink('real', 'docs')
        self.commit({})
        base = self.branch({'real/A.md': None})
        real_git = DC.git
        with mock.patch.object(DC, 'git', lambda *a: None if a[0] == 'ls-tree' else real_git(*a)):
            code, out = self.run_check('--base', base)
        self.assertEqual(code, 2, out)
        self.assertIn('git ls-tree', out)

class SymlinkParity(Repo):
    """#947 item 2 and the state question: --base fails a change exactly when it makes --all fail an untouched symlink
    (the same line); what was already broken on the base fails --all only."""

    def both(self, base_files, links, how, fails=True):
        self.commit(base_files)
        for name, target in links.items():
            os.symlink(target, name)
        self.commit({})
        self.git('checkout', '-q', '-b', 'change')
        how()
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'change')
        code_base, out_base = self.run_check('--base', 'main')
        code_all, out_all = self.run_check('--all')
        self.assertEqual(code_all, 1, out_all)
        self.assertIn('DOC  CLAUDE.md:', out_all)
        if fails:
            self.assertEqual(code_base, 1, out_base)
            line_all = re.search(r'DOC  CLAUDE\.md: [^:\n]*', out_all).group(0)
            self.assertIn(line_all, out_base)
        else:
            self.assertEqual(code_base, 0, out_base)
        return out_base

    def test_an_untouched_symlink_is_judged_by_its_own_budget_when_its_target_grows(self):
        out = self.both({'AGENTS.md': lines(30)}, {'CLAUDE.md': 'AGENTS.md'},
                        lambda: self.write({'AGENTS.md': lines(61)}))
        self.assertIn('DOC  CLAUDE.md: 61 lines / 671 B, budget 40 lines', out)
        self.assertNotIn('DOC  AGENTS.md', out)

    def test_an_untouched_symlink_through_a_retargeted_folder_symlink(self):
        def how():
            os.remove('docs')
            os.symlink('big', 'docs')
        out = self.both({'small/A.md': lines(5), 'big/A.md': lines(61)}, {'docs': 'small', 'CLAUDE.md': 'docs/A.md'},
                        how)
        self.assertIn('DOC  CLAUDE.md: 61 lines', out)

    def test_a_symlink_retargeted_removed_half_way_or_looped(self):
        def retarget(name, target):
            def how():
                os.remove(name)
                os.symlink(target, name)
            return how
        cases = [('retargeted', retarget('docs', 'nowhere')),
                 ('removed half-way', lambda: os.remove('X.md')),
                 ('looped', retarget('docs', 'docs'))]
        for name, how in cases:
            with self.subTest(name):
                self.setUp()
                self.both({'real/A.md': 'a\n'}, {'docs': 'real', 'X.md': 'docs/A.md', 'CLAUDE.md': 'X.md'}, how)

    def test_already_dangling_on_the_base_fails_all_only(self):
        for links in [{'CLAUDE.md': 'missing.md'}, {'CLAUDE.md': 'loop.md', 'loop.md': 'loop.md'}]:
            with self.subTest(links=links):
                self.setUp()
                self.both({'docs/b.md': 'b\n'}, links, lambda: os.remove('docs/b.md'), fails=False)


class FailClosed(Repo):
    """#908: what used to pass unchecked now fails: a symlink to a large file, a byte that is not UTF-8, CRLF size."""

    def test_agents_md_turned_into_a_symlink_to_a_large_file_is_checked_as_that_file(self):
        self.commit({'AGENTS.md': 'short\n', 'docs/big.md': lines(200)})
        self.git('checkout', '-q', '-b', 'change')
        os.remove('AGENTS.md')
        os.symlink('docs/big.md', 'AGENTS.md')
        self.commit({})
        code, out = self.run_check('--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('DOC  AGENTS.md: 200 lines / 2200 B, budget 150 lines', out)

    def test_a_symlink_out_of_the_repository_or_to_no_file_fails(self):
        with open(os.path.join(self.tmp.name, 'outside.md'), 'w') as f:
            f.write('x\n')
        self.git('checkout', '-q', '-b', 'change')
        os.symlink('../outside.md', 'AGENTS.md')
        os.symlink('missing.md', 'CLAUDE.md')
        self.commit({})
        code, out = self.run_check('--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('DOC  AGENTS.md: a symlink that points outside the repository', out)
        self.assertIn('DOC  CLAUDE.md: a symlink that points to no file', out)

    def test_a_symlink_into_git_fails(self):
        self.git('checkout', '-q', '-b', 'change')
        os.symlink('.git/config', 'AGENTS.md')
        self.commit({})
        code, out = self.run_check('--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('DOC  AGENTS.md: a symlink that points into .git', out)
        self.assertNotIn('core', out)

    def test_a_symlink_warning_says_when_this_change_moved_it_past(self):
        self.commit({'docs/a.md': lines(30), 'docs/b.md': lines(38), 'CLAUDE.md': 'x\n'})
        self.git('checkout', '-q', '-b', 'change')
        os.remove('CLAUDE.md')
        os.symlink('docs/b.md', 'CLAUDE.md')
        self.commit({})
        code, out = self.run_check('--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertIn('WARN CLAUDE.md: 38 lines', out)
        self.assertIn('this change moved it past 90%', out)
        self.git('checkout', '-q', 'main')
        self.git('merge', '-q', '--ff-only', 'change')
        self.git('checkout', '-q', '-b', 'change2')
        os.remove('CLAUDE.md')
        os.symlink('docs/a.md', 'CLAUDE.md')
        self.commit({'docs/a.md': lines(38)})   # already past 90% on the base (through its old target)
        code, out = self.run_check('--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertIn('WARN CLAUDE.md: 38 lines', out)
        self.assertNotIn('moved it past', out)

    def test_all_reports_a_symlink_to_an_over_budget_file_once(self):
        target = lines(300) + 'Updated: 2026-01-01\n[gone](nope.md)\n'
        self.commit({'skills/one/SKILL.md': target, 'AGENTS.md': lines(60)})
        os.makedirs('vendor/one')
        os.symlink('../../skills/one/SKILL.md', 'vendor/one/SKILL.md')
        os.symlink('AGENTS.md', 'CLAUDE.md')
        self.commit({})
        code, out = self.run_check('--all')
        self.assertEqual(code, 1, out)
        self.assertEqual(out.count('302 lines'), 1, out)
        self.assertEqual(out.count('dated "Updated" banner'), 1, out)
        self.assertEqual(out.count('broken link nope.md'), 1, out)
        self.assertNotIn('vendor/one', out)
        self.assertIn('DOC  CLAUDE.md: 60 lines / 660 B, budget 40 lines', out)   # its own, smaller budget

    def test_a_byte_that_is_not_utf8_fails_and_the_size_is_still_checked(self):
        base = self.branch({'a.md': 'ok\n'})
        with open('AGENTS.md', 'wb') as f:
            f.write(b'\xff\n' + lines(200).encode())
        with open('a.md', 'wb') as f:
            f.write(b'\xfe bad\n')
        self.commit({})
        first = self.run_check('--base', base)
        self.assertEqual(first[0], 1, first[1])
        self.assertIn('DOC  AGENTS.md: not UTF-8 text (byte 0)', first[1])
        self.assertIn('DOC  AGENTS.md: 201 lines / 2202 B', first[1])
        self.assertIn('DOC  a.md: not UTF-8 text', first[1])
        self.assertEqual(first, self.run_check('--base', base))   # a re-run gives the same output

    def test_crlf_is_measured_as_its_bytes_on_disk(self):
        base = self.branch({})
        with open('AGENTS.md', 'wb') as f:
            f.write(b''.join(b'x' * 118 + b'\r\n' for _ in range(100)))   # 12000 B on disk; 11900 read as LF
        self.commit({})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 0, out)
        self.assertIn('WARN AGENTS.md: 100 lines / 12000 B = 100%', out)
        with open('AGENTS.md', 'ab') as f:
            f.write(b'x\r\n')
        self.commit({})
        code, out = self.run_check('--base', base)
        self.assertEqual(code, 1, out)
        self.assertIn('DOC  AGENTS.md: 101 lines / 12003 B, budget 150 lines / 12000 B', out)


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
