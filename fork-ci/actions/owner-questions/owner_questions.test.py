"""owner_questions.py: the rule's shared cases, the config file, and list and label mode against a fake `gh api`
that serves issues and comments and records every write.
Run: python3 fork-ci/actions/owner-questions/owner_questions.test.py
"""
import json, os, subprocess, sys, tempfile, unittest

sys.dont_write_bytecode = True   # the tests import owner_questions.py: leave no __pycache__ in the action folder

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import owner_questions as oq   # noqa: E402

with open(os.path.join(HERE, 'rule-cases.json'), encoding='utf-8') as f:
    CASES = json.load(f)
RULES = {'default': oq.DEFAULT_RULE, 'renamed': CASES['renamedRule']}
LABEL = oq.DEFAULT_RULE['label']


def issue(number, title, labels=(), comments=(), pull=False):
    return {'number': number, 'title': title, 'labels': list(labels), 'comments': list(comments), 'pull': pull}


class FakeGh:
    """Serves `gh api --paginate <path> --jq ...` reads from `repos` ({repo: [issue]}) and records writes."""

    def __init__(self, repos, fail=None):
        self.repos, self.fail, self.writes = repos, fail, []

    def __call__(self, *args, input=None):
        args = list(args)
        done = lambda out='', code=0, err='': subprocess.CompletedProcess(args, code, out, err)   # noqa: E731
        if '-X' in args:
            method, path = args[args.index('-X') + 1], args[args.index('-X') + 2]
            self.writes.append((method, path, json.loads(input) if input else None))
            return done(code=1, err='refused') if self.fail == method else done()
        path = args[args.index('--paginate') + 1]
        if self.fail == path:
            return done(code=1, err='HTTP 502')
        parts = path.split('?')[0].split('/')
        repo = '/'.join(parts[1:3])
        if parts[3:] == ['issues']:
            rows = [{'number': i['number'], 'title': i['title'], 'url': f'https://github.com/{repo}/issues/{i["number"]}',
                     'comments': len(i['comments']), 'labels': i['labels']}
                    for i in self.repos[repo] if not i['pull']]
            return done(''.join(json.dumps(r) + '\n' for r in rows))
        number = int(parts[4])
        bodies = next(i['comments'] for i in self.repos[repo] if i['number'] == number)
        return done(''.join(json.dumps(b) + '\n' for b in bodies))


def run(repos, mode='list', config=None, fail=None):
    gh, lines = FakeGh(repos, fail), []
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'issues.json')
        if config is not None:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(config if isinstance(config, str) else json.dumps(config))
        env = {'GITHUB_OUTPUT': os.path.join(tmp, 'out'), 'GITHUB_STEP_SUMMARY': os.path.join(tmp, 'summary')}
        code = oq.cli(['--config', path, '--repo', 'acme/web', '--mode', mode], env=env, gh=gh, out=lines.append)
        def read(p):
            if not os.path.exists(p):
                return ''
            with open(p, encoding='utf-8') as f:
                return f.read()
        outputs = dict(line.split('=', 1) for line in read(env['GITHUB_OUTPUT']).splitlines())
        return code, gh.writes, outputs, read(env['GITHUB_STEP_SUMMARY']), lines


class SharedRuleCases(unittest.TestCase):
    """Does this copy of the rule answer every case the TypeScript rule answers, with the same defaults?"""

    def test_defaults_are_the_shared_ones(self):
        self.assertEqual(oq.DEFAULT_RULE, CASES['defaultRule'])

    def test_question_cases(self):
        for case in CASES['questions']:
            with self.subTest(case['title']):
                self.assertIs(oq.is_owner_question(case['title'], case['labels'], RULES[case['rule']]), case['expect'])

    def test_decision_cases(self):
        for case in CASES['decisions']:
            with self.subTest(case['body']):
                self.assertIs(oq.is_owner_decision(case['body'], RULES[case['rule']]), case['expect'])


class Config(unittest.TestCase):
    """Which repositories and rule does a project's config give, and is a broken one refused?"""

    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'issues.json')
            if text is not None:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(text)
            return oq.load_config(path, 'acme/web')

    def test_no_file_reads_this_repository_with_the_defaults(self):
        self.assertEqual(self.load(None), (['acme/web'], oq.DEFAULT_RULE))

    def test_named_repositories_and_a_renamed_rule(self):
        repos, rule = self.load(json.dumps({'repositories': ['me/web', 'me/docs', 'me/web'],
                                            'ownerQuestions': {'label': 'needs-decision'}}))
        self.assertEqual(repos, ['me/web', 'me/docs'])
        self.assertEqual(rule, {**oq.DEFAULT_RULE, 'label': 'needs-decision'})

    def test_a_broken_file_is_refused(self):
        for text in ('{ nope', '[]', '{"repositories": "me/web"}', '{"repositories": ["me web"]}',
                     '{"ownerQuestions": {"labels": "x"}}', '{"ownerQuestions": {"label": ""}}',
                     '{"ownerQuestions": {"topicQuestionTitles": "yes"}}'):
            with self.subTest(text), self.assertRaises(oq.ConfigError):
                self.load(text)


REPO = {'acme/web': [
    issue(1, 'Owner question: which runner?'),                                       # waiting, by title
    issue(2, 'Bug', labels=[LABEL]),                                                  # waiting, by label
    issue(3, 'Issues page Q1: how far?', labels=[LABEL], comments=['A note', 'Owner decision: B']),  # answered
    issue(4, 'Owner question: done by hand', comments=['**Owner decision:** yes']),   # answered, no label
    issue(5, 'Owner question: waits despite a status note', comments=['Owner decision needed soon']),
    issue(6, 'Plain bug', comments=['Owner decision: unrelated']),                    # not a question
    issue(7, 'Owner question: a pull request', pull=True),                           # pull requests are left out
]}


class ListMode(unittest.TestCase):
    """Which questions does list mode report, and does it write nothing?"""

    def test_lists_the_waiting_questions_and_writes_nothing(self):
        code, writes, outputs, summary, lines = run(REPO)
        self.assertEqual(code, 0)
        self.assertEqual(writes, [])
        self.assertEqual(outputs['count'], '3')
        self.assertEqual([q['number'] for q in json.loads(outputs['questions'])], [1, 2, 5])
        self.assertIn('Owner questions waiting: 3', summary)
        self.assertNotIn('Label', summary)

    def test_reads_every_named_repository(self):
        repos = {**REPO, 'me/docs': [issue(9, 'Docs Q2: one page or two?')]}
        code, _, outputs, _, _ = run(repos, config={'repositories': ['acme/web', 'me/docs']})
        self.assertEqual(code, 0)
        self.assertEqual([(q['repo'], q['number']) for q in json.loads(outputs['questions'])],
                         [('acme/web', 1), ('acme/web', 2), ('acme/web', 5), ('me/docs', 9)])

    def test_a_title_cannot_start_a_workflow_command_or_break_the_table(self):
        code, _, _, summary, lines = run({'acme/web': [issue(1, 'Owner question: x\n::error::y | z')]})
        self.assertEqual(code, 0)
        self.assertFalse(any(line.startswith('::') for line in lines))
        self.assertIn('x ::error::y \\| z', summary)


class LabelMode(unittest.TestCase):
    """Does label mode put the label on exactly the waiting questions and take it off the answered ones?"""

    def test_labels_waiting_and_unlabels_answered(self):
        code, writes, outputs, summary, _ = run(REPO, mode='label')
        self.assertEqual(code, 0)
        self.assertEqual(writes, [
            ('POST', 'repos/acme/web/issues/1/labels', {'labels': [LABEL]}),
            ('DELETE', 'repos/acme/web/issues/3/labels/status%3Aneeds-owner-decision', None),
            ('POST', 'repos/acme/web/issues/5/labels', {'labels': [LABEL]}),
        ])
        self.assertEqual(outputs['changed'], '3')
        self.assertIn('labelled acme/web#1, unlabelled acme/web#3, labelled acme/web#5', summary)

    def test_a_second_run_changes_nothing(self):
        settled = {'acme/web': [issue(1, 'Owner question: x', labels=[LABEL]),
                                issue(3, 'Topic Q1: y', comments=['Owner decision: z'])]}
        _, writes, outputs, _, _ = run(settled, mode='label')
        self.assertEqual(writes, [])
        self.assertEqual(outputs['changed'], '0')

    def test_a_renamed_rule_labels_with_its_own_label(self):
        repos = {'acme/web': [issue(1, 'Decision needed: x'), issue(2, 'Owner question: not under this rule')]}
        _, writes, _, _, _ = run(repos, mode='label', config={
            'ownerQuestions': {'label': 'needs-decision', 'titlePrefixes': ['Decision needed']}})
        self.assertEqual(writes, [('POST', 'repos/acme/web/issues/1/labels', {'labels': ['needs-decision']})])


class Failures(unittest.TestCase):
    """Does a failed read, write or config stop the step red, with no outputs claiming a count?"""

    def test_a_failed_listing_fails_the_step(self):
        code, writes, outputs, _, lines = run(REPO, fail='repos/acme/web/issues?state=open&per_page=100')
        self.assertEqual(code, 1)
        self.assertEqual((writes, outputs), ([], {}))
        self.assertTrue(lines[0].startswith('::error::owner-questions: gh api'))

    def test_a_failed_comment_read_fails_rather_than_guessing(self):
        code, _, outputs, _, _ = run(REPO, fail='repos/acme/web/issues/3/comments?per_page=100')
        self.assertEqual((code, outputs), (1, {}))

    def test_a_refused_label_write_fails_the_step(self):
        code, writes, outputs, _, lines = run(REPO, mode='label', fail='POST')
        self.assertEqual(code, 1)
        self.assertEqual(len(writes), 1)
        self.assertEqual(outputs, {})
        self.assertIn('could not change the label on acme/web#1', lines[0])

    def test_a_broken_config_fails_before_any_read(self):
        code, writes, outputs, _, lines = run(REPO, config='{ nope')
        self.assertEqual((code, writes, outputs), (1, [], {}))
        self.assertIn('could not be read', lines[0])


if __name__ == '__main__':
    unittest.main()
