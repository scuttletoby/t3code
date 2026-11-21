"""The kit's capacity evaluator (lib/capacity_rule.py) against the same files the TypeScript evaluator's tests read
(packages/shared/src/capacityRule.test.ts): every vector of capacity-vectors.json and every case of
capacity-format.json, read from packages/shared/src/capacity (one source: no number is copied here).

  python3 -I -B fork-kit/heavy/tests/capacity_rule.test.py

A changed rule value must turn the vectors red (each number of the rule, one at a time), so both evaluators are held
to the numbers, not only to each other.
"""
import copy, importlib.machinery, importlib.util, json, os, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED = os.path.join(os.path.dirname(os.path.dirname(KIT)), 'packages', 'shared', 'src', 'capacity')


def load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cr = load_module('capacity_rule', os.path.join(KIT, 'lib', 'capacity_rule.py'))
FORMAT = cr.load('capacity-format.json', SHARED)
RULE_JSON = cr.load('capacity-rule.json', SHARED)
VECTORS = cr.load('capacity-vectors.json', SHARED)


def failing(rule):
    """Names of the vectors whose answer differs from their expect (on their given keys only)."""
    out = []
    for v in VECTORS['vectors']:
        try:
            got = cr.run_vector(rule, VECTORS, v)
        except (ValueError, KeyError, TypeError, ZeroDivisionError):
            out.append(v['name'])
            continue
        if {k: got.get(k) for k in v['expect']} != v['expect']:
            out.append(v['name'])
    return out


def number_paths(value, path=()):
    if isinstance(value, dict):
        for k, v in value.items():
            if not k.startswith('//'):
                yield from number_paths(v, path + (k,))
    elif cr.is_num(value):
        yield path


class Files(unittest.TestCase):
    def test_reads_the_shared_folder_from_the_repository(self):
        self.assertEqual(os.path.realpath(cr.folder()), os.path.realpath(SHARED))

    def test_shipped_rule_and_vectors_are_well_formed(self):
        self.assertEqual(cr.spec_errors(RULE_JSON, '@rule', 'rule', FORMAT), [])
        self.assertEqual(cr.spec_errors(VECTORS, '@vectors', 'vectors', FORMAT), [])
        self.assertEqual(cr.parse_rule(RULE_JSON, FORMAT)[1], [])

    def test_format_cases(self):
        self.assertGreater(len(FORMAT['cases']), 0)
        for case in FORMAT['cases']:
            with self.subTest(spec=case['spec'], json=case['json']):
                self.assertEqual(sorted(case), ['json', 'ok', 'spec', 'why'])
                try:
                    ok = cr.spec_errors(cr.loads(case['json']), case['spec'], fmt=FORMAT) == []
                except ValueError:
                    ok = False
                self.assertEqual(ok, case['ok'], case['why'])

    def test_refuses_old_new_and_broken_rules_by_name(self):
        old = {k: v for k, v in RULE_JSON.items() if k != 'jobCoresPercent'}
        self.assertIn('rule.jobCoresPercent: missing', cr.parse_rule(old, FORMAT)[1])
        newer = dict(RULE_JSON, format='capacity-rule/2', extra=1)
        self.assertEqual(cr.parse_rule(newer, FORMAT)[1],
                         ['rule.format: expected =capacity-rule/1, got "capacity-rule/2"', 'rule.extra: unknown key'])
        bad = copy.deepcopy(RULE_JSON)
        bad['floors']['agent'] = True
        bad['margin']['minMb'] = -1
        self.assertEqual(cr.parse_rule(bad, FORMAT)[1], ['rule.margin.minMb: expected uint, got -1',
                                                         'rule.floors.agent: expected uint, got true'])
        wrong = copy.deepcopy(RULE_JSON)
        wrong['unknownKinds']['job'] = wrong['writers']['kind']
        wrong['kinds']['//note'] = {'group': 'job', 'startMb': 0, 'startCentiCores': 0}
        rule, errors = cr.parse_rule(wrong, FORMAT)
        self.assertEqual(errors, [f'rule.unknownKinds.job: "{wrong["writers"]["kind"]}" is not a job kind'])
        self.assertEqual(cr.parse_rule(RULE_JSON, {'format': 'capacity-format/2'})[1],
                         ['format file: not capacity-format/1'])
        self.assertRaises(ValueError, cr.loads, '{"a": NaN}')


class Vectors(unittest.TestCase):
    rule = cr.parse_rule(RULE_JSON, FORMAT)[0]

    def test_every_vector(self):
        self.assertGreater(len(VECTORS['vectors']), 100)
        for v in VECTORS['vectors']:
            with self.subTest(v['name']):
                got = cr.run_vector(self.rule, VECTORS, v)
                self.assertGreater(len(v['expect']), 0)
                self.assertEqual({k: got.get(k) for k in v['expect']}, v['expect'], v['why'])

    def test_a_changed_rule_value_turns_the_vectors_red(self):
        """Every number of the rule, changed by +1, -1, x2 or /2 (one at a time), turns some vector red, so a value
        changed in capacity-rule.json shows in this test and in capacityRule.test.ts alike. Not held: the agents'
        startCentiCores, which no decision reads (the core check is for jobs)."""
        self.assertEqual(failing(self.rule), [])
        quiet = []
        for path in number_paths(RULE_JSON):
            seen = False
            for change in (lambda v: v + 1, lambda v: max(0, v - 1), lambda v: v * 2, lambda v: v // 2):
                changed = copy.deepcopy(RULE_JSON)
                box = changed
                for k in path[:-1]:
                    box = box[k]
                if change(box[path[-1]]) == box[path[-1]]:
                    continue
                box[path[-1]] = change(box[path[-1]])
                rule, errors = cr.parse_rule(changed, FORMAT)
                if errors or failing(rule):         # refused outright, or a vector differs: red either way
                    seen = True
                    break
            if not seen:
                quiet.append('.'.join(path))
        self.assertEqual(quiet, ['kinds.agent-light.startCentiCores', 'kinds.agent-writer.startCentiCores'],
                         'rule numbers no vector holds: a change to them goes unseen by both evaluators')

    def test_unknown_fact_set_and_ask_or_fill(self):
        self.assertRaises(ValueError, cr.run_vector, self.rule, VECTORS,
                          {'name': 'x', 'facts': '//', 'ask': {'kind': 'agent-light'}})
        name = next(k for k in VECTORS['factSets'] if not k.startswith('//'))
        self.assertRaises(ValueError, cr.run_vector, self.rule, VECTORS, {'name': 'x', 'facts': name})


if __name__ == '__main__':
    unittest.main()
