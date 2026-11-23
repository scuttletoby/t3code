"""The kit's capacity evaluator (lib/capacity_rule.py) and reader (lib/capacity_readings.py) against the same files
the TypeScript tests read (packages/shared/src/capacityRule.test.ts, capacityReadings.test.ts): every vector of
capacity-vectors.json, every case of capacity-format.json and of capacity-readings.json, read from
packages/shared/src/capacity (one source: no number is copied here).

  python3 -I -B fork-kit/heavy/tests/capacity_rule.test.py

A changed rule value must turn the vectors red (each number of the rule, one at a time), so both evaluators are held
to the numbers, not only to each other.
"""
import copy, importlib.machinery, importlib.util, json, os, shutil, tempfile, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED = os.path.join(os.path.dirname(os.path.dirname(KIT)), 'packages', 'shared', 'src', 'capacity')


def load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cr = load_module('capacity_rule', os.path.join(KIT, 'lib', 'capacity_rule.py'))
rd = load_module('capacity_readings', os.path.join(KIT, 'lib', 'capacity_readings.py'))
FORMAT = cr.load('capacity-format.json', SHARED)
RULE_JSON = cr.load('capacity-rule.json', SHARED)
VECTORS = cr.load('capacity-vectors.json', SHARED)
READINGS = cr.load('capacity-readings.json', SHARED)


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


    def test_a_rule_from_before_the_swap_trend_or_an_empty_or_missing_one_is_refused(self):
        """A kit copy of the rule written before swap counted by its trend (calm.swapFreeAtLeastPercent, no
        swapFillingMb) is refused by name, never read half-way into a KeyError; so are an empty and a missing file
        (heavy facts then answers "not available", never free)."""
        old = copy.deepcopy(RULE_JSON)
        old['pressure']['calm']['swapFreeAtLeastPercent'] = 30
        del old['pressure']['swapFillingMb']
        self.assertEqual(sorted(cr.parse_rule(old, FORMAT)[1]),
                         ['rule.pressure.calm.swapFreeAtLeastPercent: unknown key',
                          'rule.pressure.swapFillingMb: missing'])
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(os.path.join(SHARED, 'capacity-format.json'), d)
            with open(os.path.join(d, 'capacity-rule.json'), 'w') as f:
                json.dump(old, f)
            self.assertRaisesRegex(ValueError, 'swapFillingMb: missing', cr.shipped_rule, d)
            open(os.path.join(d, 'capacity-rule.json'), 'w').close()
            self.assertRaises(ValueError, cr.shipped_rule, d)
            os.unlink(os.path.join(d, 'capacity-rule.json'))
            self.assertRaises(OSError, cr.shipped_rule, d)

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
        """Every number of the rule, changed by +1 and changed by -1 (each one at a time, #800: both directions), turns
        some vector red, so a value changed either way in capacity-rule.json shows in this test and in
        capacityRule.test.ts alike. Not held: the agents' startCentiCores, which no decision reads (the core check is
        for jobs), and `cpu`, which the shared evaluators do not read (the host kit's CPU brake, #1047: facts.test.py
        CpuBrake holds it)."""
        self.assertEqual(failing(self.rule), [])
        quiet = []
        for path in number_paths(RULE_JSON):
            for name, change in (('+1', lambda v: v + 1), ('-1', lambda v: max(0, v - 1))):
                changed = copy.deepcopy(RULE_JSON)
                box = changed
                for k in path[:-1]:
                    box = box[k]
                if change(box[path[-1]]) == box[path[-1]]:
                    continue
                box[path[-1]] = change(box[path[-1]])
                rule, errors = cr.parse_rule(changed, FORMAT)
                if not (errors or failing(rule)):   # neither refused outright nor a vector that differs
                    quiet.append('.'.join(path) + ' ' + name)
        self.assertEqual(quiet, ['kinds.agent-light.startCentiCores +1', 'kinds.agent-light.startCentiCores -1',
                                 'kinds.agent-writer.startCentiCores +1', 'kinds.agent-writer.startCentiCores -1',
                                 'cpu.pressureSomeAvg10Pct +1', 'cpu.pressureSomeAvg10Pct -1',
                                 'cpu.loadPerCorePercent +1', 'cpu.loadPerCorePercent -1'],
                         'rule numbers no vector holds: a change to them goes unseen by both evaluators')

    def test_unknown_fact_set_and_ask_or_fill(self):
        self.assertRaises(ValueError, cr.run_vector, self.rule, VECTORS,
                          {'name': 'x', 'facts': '//', 'ask': {'kind': 'agent-light'}})
        name = next(k for k in VECTORS['factSets'] if not k.startswith('//'))
        self.assertRaises(ValueError, cr.run_vector, self.rule, VECTORS, {'name': 'x', 'facts': name})

    def test_a_fill_count_is_read_as_the_typescript_loop_reads_it(self):
        """capacityFill runs `index < count`: a count of 2.5 asks three times (the format makes it a uint, so this
        only holds the two evaluators together)."""
        facts = next(v for k, v in VECTORS['factSets'].items() if not k.startswith('//'))
        kind = self.rule['unknownKinds']['other']
        whole = cr.fill(self.rule, facts, [{'kind': kind, 'count': 3}])
        self.assertEqual(cr.fill(self.rule, facts, [{'kind': kind, 'count': 2.5}]), whole)
        self.assertEqual(cr.fill(self.rule, facts, [{'kind': kind, 'count': 2.0}]),
                         cr.fill(self.rule, facts, [{'kind': kind, 'count': 2}]))


class FixtureHost:
    """A host of capacity-readings.json with a case's patch: files and commands merged key by key (null removes)."""

    def __init__(self, name, patch=None):
        base, patch = READINGS['hosts'][name], dict(patch or {})
        files, commands = dict(base['files']), dict(base['commands'])
        files.update(patch.pop('files', {}))
        commands.update(patch.pop('commands', {}))
        merged = dict(base, **patch)
        self.platform, self.files, self.commands, self.n = merged['platform'], files, commands, merged

    def read_file(self, path):
        f = self.files.get(path)
        if f is None:
            return {'error': 'missing'}
        return {'text': f} if isinstance(f, str) else dict(f)

    def run(self, file, args):
        return self.commands.get(' '.join([file, *args]))

    def total_mem_bytes(self):
        return self.n['totalMemBytes']

    def free_mem_bytes(self):
        return self.n['freeMemBytes']

    def cpu_count(self):
        return self.n['cpuCount']


def run_reading(case):
    host = FixtureHost(case['host'], case.get('patch'))
    machine = rd.read_machine(host)
    read = case['read']
    if read == 'machine':
        return dict(machine, gaps=[g['reading'] for g in machine['gaps']])
    if 'piece' in read:
        return rd.read_piece_demand(host, machine['cgroup'], read['piece'])
    return {'psi': rd.read_box_psi(host, machine, read['box'])}


class Readings(unittest.TestCase):
    """The kit's reader answers every shared readings case as capacityReadings.ts does."""

    def test_every_readings_case(self):
        self.assertEqual(READINGS['format'], 'capacity-readings/1')
        self.assertGreaterEqual(len(READINGS['cases']), 50)
        for case in READINGS['cases']:
            with self.subTest(case['name']):
                got = run_reading(case)
                self.assertEqual({k: got.get(k, '<absent>') for k in case['expect']}, case['expect'], case['why'])

    def test_a_failed_reading_never_admits_more_than_the_healthy_host(self):
        rule = cr.shipped_rule(SHARED)
        rank = {'ok': 0, 'elevated': 1, 'severe': 2}

        def decide(case):
            m = rd.read_machine(FixtureHost(case['host'], case.get('patch')))
            facts = {k: m[k] for k in ('totalMb', 'availableMb', 'cpuCores', 'swap', 'psi', 'osLevel')}
            return cr.decision(rule, dict(facts, pieceCaps=True, pieces=[]), {'kind': 'agent-light'})
        for case in READINGS['cases']:
            if not case.get('failed'):
                continue
            with self.subTest(case['name']):
                bad, good = decide(case), decide(dict(case, patch=None))
                if bad['admit']:
                    self.assertTrue(good['admit'])
                self.assertGreaterEqual(rank[bad['level']], rank[good['level']])
                if bad['freeMb'] is not None:
                    self.assertLessEqual(bad['freeMb'], good['freeMb'])

    def test_file_errors_sort_as_the_typescript_host_sorts_them(self):
        import errno
        self.assertEqual([rd.file_error(e) for e in (errno.ENOENT, errno.ENOTDIR, errno.EOPNOTSUPP, errno.EACCES,
                                                     errno.EIO)],
                         ['missing', 'missing', 'unsupported', 'unreadable', 'unreadable'])


if __name__ == '__main__':
    unittest.main()
