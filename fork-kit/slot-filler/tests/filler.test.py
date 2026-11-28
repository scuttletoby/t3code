"""slot-filler: slot counting, caps, capacity, usage, ready-when, starts that stop half-way, quiet repeat runs, and
broken queue files. No network, no GitHub, no T3: a fake world (the route, gh, the capacity command, the clock).

  python3 -I -B fork-kit/slot-filler/tests/filler.test.py

RedCheck breaks each rule in a copy of bin/slot-filler and shows its test then fails.
"""
import datetime, fcntl, importlib.machinery, importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest

THIS = os.path.abspath(__file__)
KIT = os.path.dirname(os.path.dirname(THIS))
BIN = os.environ.get('SLOT_FILLER_BIN') or os.path.join(KIT, 'bin', 'slot-filler')
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
HEAD = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678'
HEAD2 = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
HEAD3 = 'cccccccccccccccccccccccccccccccccccccccc'


def load():
    loader = importlib.machinery.SourceFileLoader('slot_filler', BIN)
    spec = importlib.util.spec_from_loader('slot_filler', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


sf = load()


class Crash(Exception):
    """The filler is killed at this point."""


class FakeApi:
    """The /api/fork/delegate route, in the server's order: a request id that started returns its task first, then
    refusals are checked, then one task per request id. `refuse_next` answers the next POSTs first, whatever they are
    (a refusal from a server that does not look the id up first)."""

    def __init__(self):
        self.live = []          # children the parent already has
        self.by_request = {}    # requestId -> taskId
        self.starts = []        # every POST body
        self.refuse = {}        # environmentId -> (code, message)
        self.refuse_next = []   # (code, message), one per POST
        self.settled = False    # the parent was settled by hand
        self.down = False
        self.crash_after_start = False

    def children(self, parent):
        if self.down:
            raise sf.ApiError('GET /api/fork/delegate: connection refused')
        return {'children': list(self.live), 'parentSettled': self.settled}

    def start(self, body):
        if self.down:
            raise sf.ApiError('POST /api/fork/delegate: connection refused')
        self.starts.append(body)
        if body.get('lookupOnly'):
            rid = body['requestId']
            if rid in self.by_request:
                return {'state': 'started', 'taskId': self.by_request[rid], 'childThreadId': 'thread-x'}
            return {'state': 'refused', 'code': 'not_found', 'message': 'nothing started under this id yet'}
        if self.refuse_next:
            code, msg = self.refuse_next.pop(0)
            return {'state': 'refused', 'code': code, 'message': msg}
        env = body['task']['target'].get('environmentId')
        rid = body['requestId']
        if rid in self.by_request:
            return {'state': 'started', 'taskId': self.by_request[rid], 'childThreadId': 'thread-' + self.by_request[rid]}
        if env in self.refuse:
            code, msg = self.refuse[env]
            return {'state': 'refused', 'code': code, 'message': msg}
        if rid not in self.by_request:
            self.by_request[rid] = f'task-{len(self.by_request) + 1}'
            self.live.append({'taskId': self.by_request[rid], 'status': 'running', 'live': True,
                              'environmentId': env})
        if self.crash_after_start:
            self.crash_after_start = False
            raise Crash()
        return {'state': 'started', 'taskId': self.by_request[rid], 'childThreadId': 'thread-' + self.by_request[rid]}


class FakeWorld:
    def __init__(self):
        self.api = FakeApi()
        self.prs = {}           # n -> {'state', 'headRefOid', 'checks': [..]}
        self.gh_calls = []
        self.gh_down = False
        self.capacity = {}      # command tuple -> (rc, out)
        self.now = lambda: NOW

    def gh(self, args):
        self.gh_calls.append(args)
        if self.gh_down:
            raise RuntimeError('gh: could not connect')
        if args[0] == 'pr':
            p = self.prs[int(args[2])]
            return {'state': p['state'], 'headRefOid': p['headRefOid']}
        for n, p in self.prs.items():
            if f'/commits/{p["headRefOid"]}/' in args[1]:
                return p.get('checks', [])
        return []

    def run(self, args):
        return self.capacity.get(tuple(args), (0, 'available=5000MB swapfree=4000MB\nOK\n'))


def green(head=HEAD):
    return {'state': 'OPEN', 'headRefOid': head, 'checks': [{'id': 2, 'status': 'completed', 'conclusion': 'success'}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='slot-filler-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.q = os.path.join(self.tmp, 'queue')
        self.state = os.path.join(self.tmp, 'state')
        self.caches = os.path.join(self.tmp, 'caches')
        os.makedirs(self.q)
        os.makedirs(self.caches)
        self.rules = self.write('rules/lens-rules.md', 'Lens rules.\n')
        self.cfg = {
            'queueDir': self.q, 'stateDir': self.state, 'parentThreadId': 'thread-orch', 'repo': 'example/app',
            'lensRules': self.rules,
            'defaults': {'account': 'claudeAgent', 'model': 'claude-opus-5-5', 'effort': 'high'},
            'usage': {'maxUsedPercent': 95, 'maxAgeMinutes': 90, 'cacheDir': self.caches,
                      'remoteAccounts': {'env-north': {'claudeAgent': 'claude-north'}}},
            'machines': {
                'local': {'maxAgents': 3, 'maxWriters': 2,
                          'capacity': {'writer': {'command': ['cap', '3072']},
                                       'default': {'command': ['cap'],
                                                   'free': {'availableMb': 1024, 'swapfreeMb': 1024}}}},
                'north': {'environmentId': 'env-north', 'maxAgents': 2, 'maxWriters': 1},
            },
        }
        self.cfg['log'] = os.path.join(self.state, 'filler.log')
        self.w = FakeWorld()
        self.meter('claudeAgent', 40)
        self.meter('claude-north', 40)

    # --- helpers ---
    def write(self, rel, text):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)
        return path

    def meter(self, name, used, age_min=5, windows=None):
        checked = (NOW - datetime.timedelta(minutes=age_min)).strftime('%Y-%m-%dT%H:%M:%SZ')
        win = windows if windows is not None else [{'id': 'five_hour', 'label': '5h', 'usedPercent': used,
                                                    'resetsAt': '2026-11-21T14:00:00Z'}]
        with open(os.path.join(self.caches, name + '.json'), 'w') as f:
            json.dump({'usageLimits': {'checkedAt': checked, 'windows': win}}, f)

    def item(self, iid, kind='writer', priority=0, mtime=None, **extra):
        brief = self.write(f'briefs/{iid}.md', f'Brief for {iid}.\n')
        it = {'id': iid, 'title': f'Task {iid}', 'kind': kind, 'brief': brief, 'priority': priority}
        it.update(extra)
        path = os.path.join(self.q, iid + '.json')
        with open(path, 'w') as f:
            json.dump(it, f)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def run_filler(self):
        return sf.run_once(self.cfg, self.w)

    def log_lines(self):
        try:
            with open(self.cfg['log']) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def started_ids(self):
        d = os.path.join(self.q, 'started')
        return sorted(n[:-5] for n in os.listdir(d)) if os.path.isdir(d) else []

    def snapshot(self):
        out = {}
        for root, _dirs, files in os.walk(self.tmp):
            for n in files:
                p = os.path.join(root, n)
                st = os.stat(p)
                with open(p, 'rb') as f:
                    out[p] = (f.read(), st.st_mtime_ns)
        return out


class SlotCounting(Base):
    def test_live_children_count_per_machine_and_settled_ones_do_not(self):
        f = sf.Filler(self.cfg, self.w)
        os.makedirs(os.path.join(self.q, 'started'))
        with open(os.path.join(self.q, 'started', 'lens-a.json'), 'w') as fh:
            json.dump({'id': 'lens-a', 'kind': 'lens', 'taskId': 't-lens'}, fh)
        with open(os.path.join(self.q, 'started', 'w-a.json'), 'w') as fh:
            json.dump({'id': 'w-a', 'kind': 'writer', 'taskId': 't-writer'}, fh)
        counts = f.count([
            {'taskId': 't-lens', 'status': 'running', 'live': True, 'environmentId': None},
            {'taskId': 't-writer', 'status': 'waiting', 'live': True, 'environmentId': None},
            {'taskId': 't-other', 'status': 'running', 'live': True, 'environmentId': 'env-north'},  # not ours
            {'taskId': 't-done', 'status': 'completed', 'live': False, 'environmentId': None},
            {'taskId': 't-gone', 'status': 'running', 'live': True, 'environmentId': 'env-unlinked'},
        ])
        self.assertEqual(counts['local'], {'agents': 2, 'writers': 1})
        self.assertEqual(counts['north'], {'agents': 1, 'writers': 1})   # unknown kind counts as a writer
        self.assertEqual(counts[None], {'agents': 1, 'writers': 1})      # an environment no machine names

    def test_children_already_running_take_their_slots(self):
        self.w.api.live = [{'taskId': f'x{i}', 'status': 'running', 'live': True, 'environmentId': None}
                           for i in range(3)]
        self.item('w1')
        self.cfg['machines']['north']['maxAgents'] = 0
        self.run_filler()
        self.assertEqual(self.w.api.starts, [])
        self.assertTrue(any('waiting w1: no machine has room (local full (3/3 agents)' in l for l in self.log_lines()))


class Caps(Base):
    def test_starts_by_priority_until_every_machine_is_full(self):
        for i, p in enumerate((5, 9, 1, 7)):
            self.item(f'w{i}', priority=p, mtime=1000 + i)
        self.item('l0', kind='lens', priority=0)
        self.item('l1', kind='lens', priority=0)
        self.run_filler()
        starts = [(b['task']['title'], b['task']['target'].get('environmentId')) for b in self.w.api.starts]
        # local: 2 writers (9, 7) + 1 lens; north: 1 writer (5) + 1 lens; the writer with priority 1 waits
        self.assertEqual(starts, [('Task w1', None), ('Task w3', None), ('Task w0', 'env-north'),
                                  ('Task l0', None), ('Task l1', 'env-north')])
        self.assertEqual(sorted(self.started_ids()), ['l0', 'l1', 'w0', 'w1', 'w3'])
        self.assertTrue(os.path.exists(os.path.join(self.q, 'w2.json')))
        self.assertEqual(len(self.w.api.starts), 5)
        self.assertTrue(any(l.endswith('waiting w2: no machine has room (local has no writer slot (2/2 writers); '
                                       'north has no writer slot (1/1 writers))') for l in self.log_lines()))

    def test_the_start_asks_for_the_model_effort_parent_and_machine(self):
        self.cfg['machines']['north']['worktreeIsolation'] = True     # opted in: its project is a git checkout
        self.item('w1', machines=['north'], role='worker', isolation='worktree')
        self.run_filler()
        b = self.w.api.starts[0]
        self.assertEqual(b['parentThreadId'], 'thread-orch')
        self.assertEqual(b['task']['target'], {'providerInstanceId': 'claudeAgent', 'model': 'claude-opus-5-5',
                                               'options': [{'id': 'effort', 'value': 'high'}],
                                               'environmentId': 'env-north'})
        self.assertIn('Brief for w1.', b['task']['task'])   # another machine: the brief goes inline
        self.assertEqual((b['task']['role'], b['task']['isolation']), ('worker', 'worktree'))
        rec = json.load(open(os.path.join(self.q, 'started', 'w1.json')))
        self.assertEqual((rec['taskId'], rec['machine']), ('task-1', 'north'))
        self.assertFalse(os.path.exists(os.path.join(self.q, 'w1.json')))

    def test_a_local_start_names_the_brief_and_a_long_inline_brief_is_refused(self):
        self.item('w1', machines=['local'])
        self.item('w2', machines=['north'])
        with open(os.path.join(self.tmp, 'briefs', 'w2.md'), 'w') as fh:
            fh.write('x' * (sf.MAX_INLINE + 1))
        self.run_filler()
        self.assertEqual(self.started_ids(), ['w1'])
        task = self.w.api.starts[0]['task']['task']
        self.assertIn(f'Read {os.path.join(self.tmp, "briefs", "w1.md")} fully', task)
        self.assertNotIn('Brief for w1.', task)
        self.assertTrue(any('refused w2 on north: brief' in l and 'is over 100000 characters' in l
                            for l in self.log_lines()))
        self.assertEqual(os.listdir(os.path.join(self.state, 'inflight')), [])
        self.assertTrue(os.path.exists(os.path.join(self.q, 'w2.json')))

    def test_writer_capacity_command_and_the_read_only_memory_rule(self):
        self.cfg['machines']['north']['maxAgents'] = 0
        self.w.capacity[('cap', '3072')] = (1, 'available=2000MB swapfree=900MB\nNOT OK\n')
        self.w.capacity[('cap',)] = (1, 'reservations: none\navailable=1500MB swapfree=1100MB load=9\nNOT OK\n')
        self.item('w1')
        self.item('l1', kind='lens')
        self.run_filler()
        self.assertEqual(self.started_ids(), ['l1'])   # rc 1, but available and swapfree >= 1024 MB
        self.assertTrue(any('local capacity NOT OK (cap 3072)' in l for l in self.log_lines()))
        self.w.capacity[('cap',)] = (0, 'available=900MB swapfree=3000MB\nOK\n')
        self.item('l2', kind='lens')
        self.run_filler()
        self.assertTrue(any('local low on memory (available=900MB' in l for l in self.log_lines()))
        self.assertEqual(self.started_ids(), ['l1'])

    def test_a_server_refusal_is_logged_once_and_the_item_kept(self):
        self.cfg['machines']['local']['maxAgents'] = 0
        self.w.api.refuse['env-north'] = ('slots_full', 'north has no free slot')
        self.item('w1')
        self.run_filler()
        self.run_filler()
        self.assertTrue(os.path.exists(os.path.join(self.q, 'w1.json')))
        self.assertEqual(os.listdir(os.path.join(self.state, 'inflight')), [])
        self.assertEqual(sum('refused w1 on north: slots_full' in l for l in self.log_lines()), 1)

    def test_refusals_back_off_doubling_under_one_request_id(self):
        self.cfg['machines']['local']['maxAgents'] = 0
        self.w.api.refuse['env-north'] = ('provider_unavailable', 'north is offline')
        self.item('w1')
        at = lambda s: (lambda: NOW + datetime.timedelta(seconds=s))
        for t in (0, 30, 61, 120, 181):
            self.w.now = at(t)
            self.run_filler()
        # tries at 0 s, 61 s (after 60 s) and 181 s (after 120 s); none between
        self.assertEqual(len(self.w.api.starts), 3)
        self.assertEqual(len({b['requestId'] for b in self.w.api.starts}), 1)   # so the wake goes once
        req = json.load(open(os.path.join(self.state, 'requests', 'w1.json')))['machines']['north']
        self.assertEqual((req['refusals'], req['lastCode']), (3, 'provider_unavailable'))
        self.assertTrue(any('north backing off after 1 refusal(s) (last: provider_unavailable) until '
                            '2026-11-21T12:01:00Z' in l for l in self.log_lines()))
        self.w.api.refuse.clear()
        self.w.now = at(181 + 240)
        self.run_filler()
        self.assertEqual(self.started_ids(), ['w1'])
        self.assertFalse(os.path.exists(os.path.join(self.state, 'requests', 'w1.json')))

    def test_a_request_id_refused_for_good_is_replaced(self):
        self.cfg['machines']['north']['maxAgents'] = 0
        self.w.api.refuse_next = [('request_rejected', 'refused at its dispatch')]
        self.item('w1')
        self.run_filler()
        self.w.now = lambda: NOW + datetime.timedelta(minutes=1)
        self.run_filler()
        ids = [b['requestId'] for b in self.w.api.starts]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])
        self.assertEqual(self.started_ids(), ['w1'])

    def test_an_offline_machine_does_not_hold_back_a_free_one(self):
        # north keeps refusing; local is full until 2 minutes in, then has room: the item starts there at once
        self.w.api.refuse['env-north'] = ('provider_unavailable', 'north is offline')
        self.w.api.live = [{'taskId': f'old-{i}', 'status': 'running', 'live': True, 'environmentId': None}
                           for i in range(3)]
        self.item('w1', kind='other')
        for s in (0, 60):
            self.w.now = lambda s=s: NOW + datetime.timedelta(seconds=s)
            self.run_filler()
        self.w.api.live = []
        self.w.now = lambda: NOW + datetime.timedelta(seconds=120)
        self.run_filler()
        self.assertEqual(self.started_ids(), ['w1'])

    def test_two_refusals_in_one_run_count_once_per_machine(self):
        self.w.api.refuse['env-north'] = ('usage_pacing_hold', 'paced')
        self.w.api.refuse[None] = ('usage_pacing_hold', 'paced')
        self.item('w1', kind='other')
        self.run_filler()
        self.assertEqual(len(self.w.api.starts), 2)
        self.w.now = lambda: NOW + datetime.timedelta(seconds=60)      # one refusal each: 60 s, both asked again
        self.run_filler()
        self.assertEqual(len(self.w.api.starts), 4)

    def test_a_wait_set_far_ahead_is_capped(self):
        # a refusal while the clock ran a year ahead, then the clock is set right: the wait is at most 15 minutes
        self.cfg['machines']['north']['maxAgents'] = 0
        self.item('w1')
        ahead = (NOW + datetime.timedelta(days=365)).timestamp()
        wait = {'refusals': 1, 'lastCode': 'usage_pacing_hold', 'nextAt': ahead}
        self.write('state/requests/w1.json', json.dumps(dict(wait, requestId='w1:1', machines={'local': wait})))
        self.run_filler()
        self.assertEqual(self.started_ids(), [])
        self.w.now = lambda: NOW + datetime.timedelta(minutes=15, seconds=1)
        self.run_filler()
        self.assertEqual(self.started_ids(), ['w1'])

    def test_a_request_id_refused_for_good_keeps_its_backoff(self):
        self.cfg['machines']['north']['maxAgents'] = 0
        self.w.api.refuse_next = [('provider_unavailable', 'offline'), ('provider_unavailable', 'offline'),
                                  ('request_rejected', 'refused at its dispatch')]
        self.item('w1')
        for s in (0, 60, 180):
            self.w.now = lambda s=s: NOW + datetime.timedelta(seconds=s)
            self.run_filler()
        req = json.load(open(os.path.join(self.state, 'requests', 'w1.json')))
        self.assertEqual(req['machines']['local']['refusals'], 3)          # the third wait: 4 minutes
        self.assertNotEqual(req['requestId'], self.w.api.starts[0]['requestId'])

    def test_a_parent_settled_by_hand_stops_the_run(self):
        self.w.api.settled = True
        self.item('w1')
        self.assertEqual(self.run_filler(), 1)
        self.assertEqual(self.w.api.starts, [])
        self.assertTrue(any('was settled by hand (handed over to a new orchestrator?)' in l for l in self.log_lines()))

    def test_the_token_file_must_be_private(self):
        path = self.write('token', 'secret-token-value\n')
        api = sf.Api({'server': {'tokenFile': path}})
        os.chmod(path, 0o644)
        with self.assertRaises(sf.ApiError) as e:
            api._token()
        self.assertIn('can be read by group or others (mode 644); run chmod 600', str(e.exception))
        self.assertNotIn('secret-token-value', str(e.exception))
        os.chmod(path, 0o600)
        self.assertEqual(api._token(), 'secret-token-value')

    def test_a_gone_parent_stops_the_run(self):
        self.w.api.refuse[None] = ('parent_missing', 'no such thread')
        self.item('w1', priority=2)
        self.item('w2', priority=1)
        self.assertEqual(self.run_filler(), 1)
        self.assertEqual(len(self.w.api.starts), 1)
        self.assertTrue(any('stopped this run: the parent thread is gone' in l for l in self.log_lines()))


class Usage(Base):
    def test_full_or_old_meters_skip_the_machine(self):
        self.meter('claudeAgent', 95)
        self.meter('claude-north', 10, age_min=91)
        self.item('w1')
        self.run_filler()
        self.assertEqual(self.w.api.starts, [])
        line = [l for l in self.log_lines() if 'waiting w1' in l][0]
        self.assertIn('claudeAgent is full (5h 95%)', line)
        self.assertIn('usage reading for claude-north is 91 min old', line)

    def test_windows_of_other_models_and_past_resets_do_not_count(self):
        self.meter('claudeAgent', 0, windows=[
            {'id': 'seven_day_fable', 'usedPercent': 100, 'resetsAt': '2026-11-25T00:00:00Z'},
            {'id': 'five_hour', 'usedPercent': 99, 'resetsAt': '2026-11-21T11:00:00Z'},
            {'id': 'seven_day', 'usedPercent': 60, 'resetsAt': '2026-11-25T00:00:00Z'}])
        self.item('w1', machines=['local'])
        self.run_filler()
        self.assertEqual(self.started_ids(), ['w1'])
        self.item('w2', machines=['local'], model='claude-fable-5-2')
        self.run_filler()
        self.assertTrue(any('waiting w2' in l and 'claudeAgent is full' in l for l in self.log_lines()))

    def test_a_missing_meter_or_mapping_skips_and_a_guard_config_is_read(self):
        os.remove(os.path.join(self.caches, 'claude-north.json'))
        self.cfg['usage']['remoteAccounts'] = {}
        self.cfg['machines']['local']['maxAgents'] = 0
        self.item('w1')
        self.run_filler()
        self.assertTrue(any('claudeAgent on north has no meter mapping' in l for l in self.log_lines()))
        guard = self.write('guard.json', json.dumps({'maxUsedPercent': 95, 'maxAgeMinutes': 90,
                                                     'cacheDir': self.caches,
                                                     'remoteAccounts': {'env-north': {'claudeAgent': 'claude-north'}}}))
        self.cfg['usage'] = {'guardConfig': guard}
        self.run_filler()
        self.assertTrue(any('no usage reading for claude-north' in l for l in self.log_lines()))
        self.meter('claude-north', 10)
        self.run_filler()
        self.assertEqual(self.started_ids(), ['w1'])


class AccountFallback(Base):
    """An item (or the defaults) may name an ordered list of accounts: the first with usage left on that machine is
    used, by the same rules as the usage guard."""
    ACCTS = ['claude-anne', 'claudeAgent']

    def setUp(self):
        super().setUp()
        self.cfg['machines']['north']['maxAgents'] = 0
        self.meter('claude-anne', 40)

    def account_started(self, iid='w1'):
        with open(os.path.join(self.q, 'started', iid + '.json')) as f:
            return json.load(f)['account']

    def test_a_full_first_account_falls_back_to_the_second(self):
        self.meter('claude-anne', 95)
        self.item('w1', account=self.ACCTS)
        self.run_filler()
        self.assertEqual([b['task']['target']['providerInstanceId'] for b in self.w.api.starts], ['claudeAgent'])
        self.assertEqual(self.account_started(), 'claudeAgent')
        line = [l for l in self.log_lines() if 'started w1' in l][0]
        self.assertIn('on local as claudeAgent', line)
        self.assertIn('claude-anne is full (5h 95%)', line)

    def test_the_first_account_with_room_is_used_first(self):
        self.item('w1', account=self.ACCTS)
        self.run_filler()
        self.assertEqual(self.w.api.starts[0]['task']['target']['providerInstanceId'], 'claude-anne')
        self.assertEqual(self.account_started(), 'claude-anne')

    def test_both_full_waits_and_says_why_once(self):
        self.meter('claude-anne', 99)
        self.meter('claudeAgent', 95)
        self.item('w1', account=self.ACCTS)
        self.run_filler()
        self.assertEqual(self.w.api.starts, [])
        line = [l for l in self.log_lines() if 'waiting w1' in l][0]
        self.assertIn('claude-anne is full (5h 99%)', line)
        self.assertIn('claudeAgent is full (5h 95%)', line)
        lines = self.log_lines()
        self.run_filler()                                   # runs again: nothing new, nothing written
        self.assertEqual(self.log_lines(), lines)
        self.meter('claudeAgent', 10)                       # the second frees up
        self.run_filler()
        self.assertEqual(self.account_started(), 'claudeAgent')

    def test_stale_or_missing_data_on_the_first_uses_the_second(self):
        self.meter('claude-anne', 10, age_min=91)
        self.item('w1', account=self.ACCTS)
        os.remove(os.path.join(self.caches, 'claude-anne.json'))
        self.item('w2', account=['claude-gone', 'claude-anne', 'claudeAgent'])
        self.meter('claude-anne', 10, age_min=91)
        self.run_filler()
        self.assertEqual([b['task']['target']['providerInstanceId'] for b in self.w.api.starts],
                         ['claudeAgent', 'claudeAgent'])

    def test_windows_of_another_family_or_past_resets_do_not_block_the_first(self):
        self.meter('claude-anne', 0, windows=[
            {'id': 'seven_day_fable', 'usedPercent': 100, 'resetsAt': '2026-11-25T00:00:00Z'},
            {'id': 'five_hour', 'usedPercent': 99, 'resetsAt': '2026-11-21T11:00:00Z'}])
        self.item('w1', account=self.ACCTS)
        self.run_filler()
        self.assertEqual(self.account_started(), 'claude-anne')

    def test_a_single_string_and_a_default_list_keep_working(self):
        self.item('w1', account='claudeAgent')
        self.run_filler()
        self.assertEqual(self.account_started(), 'claudeAgent')
        self.meter('claude-anne', 96)
        self.cfg['defaults']['account'] = self.ACCTS        # a machine default list; the item names none
        self.item('w2')
        self.run_filler()
        self.assertEqual(self.account_started('w2'), 'claudeAgent')
        self.meter('claude-anne', 10)
        self.item('w3', kind='other')                       # two writers already hold the writer slots
        self.run_filler()
        self.assertEqual(self.account_started('w3'), 'claude-anne')

    def test_a_remote_machine_skips_accounts_without_a_meter_there(self):
        self.cfg['machines']['local']['maxAgents'] = 0
        self.cfg['machines']['north']['maxAgents'] = 2
        self.item('w1', account=self.ACCTS)
        self.run_filler()
        b = self.w.api.starts[0]['task']['target']
        self.assertEqual((b['providerInstanceId'], b['environmentId']), ('claudeAgent', 'env-north'))

    def test_bad_account_fields_are_refused(self):
        for iid, acct in (('a1', []), ('a2', 5), ('a3', ['claudeAgent', '']), ('a4', [None])):
            self.item(iid, account=acct)
        self.run_filler()
        self.assertEqual(self.w.api.starts, [])
        for iid in ('a1', 'a2', 'a3', 'a4'):
            self.assertTrue(any(f'refused {iid}.json: "account" must be' in l for l in self.log_lines()), iid)
        cfg = dict(self.cfg, defaults={'account': []})
        path = self.write('cfg.json', json.dumps(cfg))
        with self.assertRaises(sf.ConfigError):
            sf.load_config(path)

    def test_a_start_cut_off_mid_way_asks_again_on_the_same_account(self):
        self.item('w1', account=self.ACCTS)
        self.w.api.crash_after_start = True
        with self.assertRaises(Crash):
            self.run_filler()
        self.meter('claude-anne', 99)                       # the first fills while the answer was lost
        self.w.now = lambda: NOW + datetime.timedelta(minutes=1)
        self.run_filler()
        self.assertEqual(len(self.w.api.by_request), 1)     # never a second task on the other account
        self.assertEqual([b['task']['target']['providerInstanceId'] for b in self.w.api.starts],
                         ['claude-anne', 'claude-anne'])
        self.assertEqual(self.account_started(), 'claude-anne')

    def test_a_first_account_refused_by_the_server_moves_to_the_second_after_its_wait(self):
        self.item('w1', account=self.ACCTS)
        self.w.api.refuse_next = [('usage_pacing_hold', 'claude-anne filled since the last reading')]
        self.run_filler()
        self.assertEqual(self.started_ids(), [])
        self.meter('claude-anne', 99)
        self.w.now = lambda: NOW + datetime.timedelta(seconds=61)
        self.run_filler()
        self.assertEqual([b['task']['target']['providerInstanceId'] for b in self.w.api.starts],
                         ['claude-anne', 'claudeAgent'])
        self.assertEqual(self.account_started(), 'claudeAgent')

    def test_a_pacing_hold_on_the_first_moves_to_the_second_while_its_meter_is_low(self):
        # the server's pacing gate holds an account ahead of pace well under 95%: the meter here still reads 40%
        self.item('w1', account=self.ACCTS)
        self.w.api.refuse_next = [('usage_pacing_hold', 'claude-anne is ahead of pace; claudeAgent has room')]
        self.run_filler()
        self.assertEqual(self.started_ids(), [])
        self.w.now = lambda: NOW + datetime.timedelta(seconds=30)      # the next run, inside the first's wait
        self.run_filler()
        self.assertEqual([b['task']['target']['providerInstanceId'] for b in self.w.api.starts],
                         ['claude-anne', 'claudeAgent'])
        self.assertEqual(self.account_started(), 'claudeAgent')
        self.assertEqual(len({b['requestId'] for b in self.w.api.starts}), 1)   # one request id: no double start

    def test_a_held_account_is_tried_again_after_its_wait_and_doubles(self):
        self.item('w1', account=self.ACCTS)
        self.meter('claudeAgent', 99)
        self.w.api.refuse[None] = ('usage_pacing_hold', 'paced')
        at = lambda s: (lambda: NOW + datetime.timedelta(seconds=s))
        for t in (0, 30, 61, 120, 181):
            self.w.now = at(t)
            self.run_filler()
        self.assertEqual(len(self.w.api.starts), 3)                    # 0 s, 61 s, 181 s
        with open(os.path.join(self.state, 'requests', 'w1.json')) as f:
            req = json.load(f)
        self.assertEqual(req['accounts']['local']['claude-anne']['refusals'], 3)
        self.assertNotIn('local', req.get('machines') or {})           # the machine itself does not wait
        self.w.now = at(200)
        self.run_filler()
        line = [l for l in self.log_lines() if 'waiting w1' in l][-1]
        self.assertIn('claude-anne on local backing off after', line)
        self.assertIn('claudeAgent is full', line)

    def test_an_account_null_uses_the_default_as_on_main(self):
        self.item('w1', account=None)
        self.run_filler()
        self.assertEqual(self.account_started(), 'claudeAgent')

    def test_a_lens_watch_with_a_bad_account_is_refused_and_a_list_is_kept(self):
        self.w.prs[7] = green()
        self.write('queue/lens-watch/bad.json', json.dumps({'pr': 7, 'lenses': [{'lens': 'code'}], 'account': []}))
        self.write('queue/lens-watch/good.json', json.dumps({'pr': 7, 'lenses': [{'lens': 'tests'}],
                                                             'account': self.ACCTS}))
        self.run_filler()
        self.assertTrue(any('refused lens-watch/bad.json: "account" must be' in l for l in self.log_lines()))
        self.assertFalse(any(n.startswith('lens-7-code-') for n in os.listdir(self.q)))
        self.assertEqual(self.account_started('lens-7-tests-' + HEAD[:7]), 'claude-anne')

    def test_a_stale_reading_is_logged_once_while_only_its_age_grows(self):
        self.meter('claude-anne', 10, age_min=91)
        self.meter('claudeAgent', 10, age_min=95)
        self.item('w1', account=self.ACCTS)
        for m in (0, 1, 2):
            self.w.now = lambda m=m: NOW + datetime.timedelta(minutes=m)
            self.run_filler()
        self.assertEqual(len([l for l in self.log_lines() if 'waiting w1' in l]), 1)
        self.meter('claudeAgent', 96)                                   # a real change is logged again
        self.run_filler()
        self.assertEqual(len([l for l in self.log_lines() if 'waiting w1' in l]), 2)

    def test_an_old_marker_without_a_chosen_account_still_recovers(self):
        self.item('w1')
        os.makedirs(os.path.join(self.state, 'inflight'))
        with open(os.path.join(self.q, 'w1.json')) as f:
            it = json.load(f)
        sf.write_json(os.path.join(self.state, 'inflight', 'w1.json'),
                      {'requestId': 'w1:1', 'parentThreadId': 'thread-orch', 'machine': 'local',
                       'at': '2026-11-21T11:59:00Z', 'item': it})
        self.run_filler()
        self.assertEqual([b['requestId'] for b in self.w.api.starts], ['w1:1'])
        self.assertEqual(self.w.api.starts[0]['task']['target']['providerInstanceId'], 'claudeAgent')
        self.assertEqual(self.started_ids(), ['w1'])


class LinkedMachines(Base):
    def test_a_machine_with_a_project_id_sends_it_and_one_without_does_not(self):
        self.cfg['machines']['north']['projectId'] = 'project-north'
        self.item('w1', machines=['north'])
        self.item('w2', machines=['local'])
        self.run_filler()
        targets = {b['task']['title']: b['task']['target'] for b in self.w.api.starts}
        self.assertEqual((targets['Task w1']['environmentId'], targets['Task w1']['projectId']),
                         ('env-north', 'project-north'))
        self.assertNotIn('projectId', targets['Task w2'])

    def test_worktree_isolation_on_a_machine_without_it_is_refused_when_read(self):
        self.item('wt1', machines=['north'], isolation='worktree')            # linked: off by default
        self.cfg['machines']['local']['worktreeIsolation'] = False            # not a git checkout
        self.item('wt2', machines=['local'], isolation='worktree')
        self.run_filler()
        self.assertEqual(self.w.api.starts, [])
        lines = self.log_lines()
        for iid, name in (('wt1', 'north'), ('wt2', 'local')):
            self.assertEqual(len([l for l in lines if f'refused {iid}.json: "isolation": "worktree" cannot run on '
                                  f'{name}' in l]), 1, iid)
        self.run_filler()
        self.assertEqual(self.log_lines(), lines)                              # once, not every run
        self.assertTrue(os.path.exists(os.path.join(self.q, 'wt1.json')))     # kept

    def test_a_worktree_item_without_machines_runs_only_where_it_can(self):
        self.cfg['machines']['local']['maxAgents'] = 0
        self.item('wt1', isolation='worktree')
        self.run_filler()
        self.assertEqual(self.w.api.starts, [])
        line = [l for l in self.log_lines() if 'waiting wt1' in l][0]
        self.assertIn('north has no worktree isolation', line)
        self.cfg['machines']['local']['maxAgents'] = 3
        self.run_filler()
        self.assertEqual([b['task']['target'].get('environmentId') for b in self.w.api.starts], [None])
        self.cfg['machines']['local']['worktreeIsolation'] = False             # no machine can: refused
        self.item('wt2', isolation='worktree')
        self.run_filler()
        self.assertTrue(any('refused wt2.json: "isolation": "worktree" cannot run on local, north' in l
                            for l in self.log_lines()))
        self.item('w3', machines=['north'])                                    # no isolation: fine there
        self.run_filler()
        self.assertIn('w3', self.started_ids())


class ReadyWhen(Base):
    def setUp(self):
        super().setUp()
        self.cfg['machines']['north']['maxAgents'] = 0

    def test_pr_green_on_its_current_head(self):
        self.w.prs[7] = {'state': 'OPEN', 'headRefOid': HEAD,
                         'checks': [{'id': 1, 'status': 'completed', 'conclusion': 'success'},
                                    {'id': 2, 'status': 'in_progress', 'conclusion': None}]}
        self.item('w1', readyWhen={'type': 'pr-green', 'pr': 7})
        self.run_filler()
        self.assertEqual(self.started_ids(), [])   # the newest run is still going
        self.assertTrue(any('waiting w1: PR #7 fork-ci-result not green on a1b2c3d' in l for l in self.log_lines()))
        self.w.prs[7] = green()
        self.run_filler()
        self.assertEqual(self.started_ids(), ['w1'])

    def test_pr_merged_closed_and_unreadable(self):
        self.w.prs[8] = {'state': 'OPEN', 'headRefOid': HEAD}
        self.w.prs[9] = {'state': 'CLOSED', 'headRefOid': HEAD}
        self.item('m8', readyWhen={'type': 'pr-merged', 'pr': 8})
        self.item('m9', readyWhen={'type': 'pr-merged', 'pr': 9})
        self.item('g9', readyWhen={'type': 'pr-green', 'pr': 9})
        self.run_filler()
        lines = self.log_lines()
        self.assertTrue(any('waiting m8: PR #8 not merged yet' in l for l in lines))
        self.assertTrue(any('waiting m9: PR #9 is closed without a merge' in l for l in lines))
        self.assertTrue(any('waiting g9: PR #9 is closed' in l for l in lines))
        self.w.prs[8]['state'] = 'MERGED'
        self.run_filler()
        self.assertEqual(self.started_ids(), ['m8'])
        self.w.gh_down = True
        self.item('m10', readyWhen={'type': 'pr-merged', 'pr': 10})
        self.run_filler()
        self.assertTrue(any('waiting m10: PR #10 unreadable (gh: could not connect)' in l for l in self.log_lines()))

    def test_a_head_pinned_item_waits_when_the_head_moves(self):
        self.w.prs[7] = green(HEAD2)
        self.item('l1', kind='lens', readyWhen={'type': 'pr-green', 'pr': 7, 'head': HEAD})
        self.run_filler()
        self.assertEqual(self.started_ids(), [])
        self.assertTrue(any('PR #7 head moved from a1b2c3d to bbbbbbb' in l for l in self.log_lines()))

    def test_no_readiness_means_ready_and_gh_is_asked_once_per_pr(self):
        self.w.prs[7] = green()
        self.item('w1')
        self.item('l1', kind='lens', readyWhen={'type': 'pr-green', 'pr': 7})
        self.item('l2', kind='lens', readyWhen={'type': 'pr-green', 'pr': 7})
        self.run_filler()
        self.assertEqual(self.started_ids(), ['l1', 'l2', 'w1'])
        self.assertEqual(len(self.w.gh_calls), 2)   # one pr view, one check-runs read


class StopsHalfWay(Base):
    def setUp(self):
        super().setUp()
        self.cfg['machines']['north']['maxAgents'] = 0

    def test_a_crash_after_the_start_never_starts_twice(self):
        self.item('w1')
        self.w.api.crash_after_start = True
        with self.assertRaises(Crash):
            self.run_filler()
        self.assertTrue(os.path.exists(os.path.join(self.q, 'w1.json')))   # not moved: no task id came back
        self.assertEqual(len(os.listdir(os.path.join(self.state, 'inflight'))), 1)
        self.w.now = lambda: NOW + datetime.timedelta(minutes=1)          # the next run, a minute later
        self.run_filler()
        self.assertEqual(self.started_ids(), ['w1'])
        self.assertEqual(len(self.w.api.by_request), 1)                    # one task in T3
        self.assertEqual({b['requestId'] for b in self.w.api.starts}, set(self.w.api.by_request))
        self.assertEqual(os.listdir(os.path.join(self.state, 'inflight')), [])
        self.assertEqual(json.load(open(os.path.join(self.q, 'started', 'w1.json')))['taskId'], 'task-1')

    def test_a_lost_answer_then_a_refused_retry_never_starts_twice(self):
        # The lenses' case: T3 started it, the answer was lost, the retry is refused (the parent is idle by then).
        self.item('w1')
        self.w.api.crash_after_start = True
        with self.assertRaises(Crash):
            self.run_filler()
        self.w.api.refuse_next = [('parent_not_active', 'no run')]
        for minutes in (1, 2, 5):
            self.w.now = lambda m=minutes: NOW + datetime.timedelta(minutes=m)
            self.run_filler()
        self.assertEqual(len(self.w.api.by_request), 1)                    # one task in T3
        self.assertEqual(len({b['requestId'] for b in self.w.api.starts}), 1)
        self.assertEqual(self.started_ids(), ['w1'])
        self.assertEqual(json.load(open(os.path.join(self.q, 'started', 'w1.json')))['taskId'], 'task-1')

    def lose_remote_answer(self):
        """north launches the thread, then the link stops answering (no record here yet); asked again with the same
        request id, north answers with that thread (its launch dedupes on the request id)."""
        real = self.w.api.start
        launched = {}

        def start(body):
            env = body['task']['target'].get('environmentId')
            rid = body['requestId']
            if env == 'env-north' and not body.get('lookupOnly'):
                if rid not in launched:
                    launched[rid] = True
                    self.w.api.starts.append(body)
                    return {'state': 'refused', 'code': 'orchestration_error',
                            'message': 'The linked environment stopped answering.'}
                self.w.api.by_request.setdefault(rid, 'remote-task')
            return real(body)
        self.w.api.start = start
        return launched

    def test_an_unclear_remote_answer_never_starts_on_another_machine(self):
        # The code lens's case: 1 thread on north (unrecorded) + 1 local task. Now: one child, on north.
        self.cfg['machines']['north']['maxAgents'] = 2
        launched = self.lose_remote_answer()
        self.item('w1', kind='other', machines=['north', 'local'])
        self.run_filler()
        self.assertEqual([b['task']['target'].get('environmentId') for b in self.w.api.starts], ['env-north'])
        self.assertEqual(self.started_ids(), [])
        self.assertEqual(len(os.listdir(os.path.join(self.state, 'inflight'))), 1)   # pinned to north
        self.assertTrue(any('no clear answer for w1 on north' in l for l in self.log_lines()))
        self.w.now = lambda: NOW + datetime.timedelta(seconds=30)                    # backing off: nothing asked
        self.run_filler()
        self.assertEqual(len(self.w.api.starts), 1)
        self.w.now = lambda: NOW + datetime.timedelta(seconds=61)
        self.run_filler()
        self.assertEqual(len(launched) + len([b for b in self.w.api.starts
                                              if b['task']['target'].get('environmentId') is None]), 1)
        self.assertEqual(json.load(open(os.path.join(self.q, 'started', 'w1.json')))['machine'], 'north')

    def test_an_unclear_answer_then_a_clear_refusal_moves_on(self):
        self.cfg['machines']['north']['maxAgents'] = 2
        self.w.api.refuse_next = [('orchestration_error', 'stopped answering'), ('slots_full', 'north is full')]
        self.item('w1', kind='other', machines=['north', 'local'])
        self.run_filler()
        self.w.now = lambda: NOW + datetime.timedelta(seconds=61)
        self.run_filler()       # north answers clearly (nothing started there), so local is tried in this run
        self.assertEqual([b['task']['target'].get('environmentId') for b in self.w.api.starts],
                         ['env-north', 'env-north', None])
        self.assertEqual(self.started_ids(), ['w1'])

    def test_a_settled_parent_stops_before_a_left_over_start_is_asked(self):
        self.item('w1')
        real = self.w.api.start
        self.w.api.start = lambda body: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            self.run_filler()
        self.w.api.start = real
        self.w.api.settled = True
        self.assertEqual(self.run_filler(), 1)
        self.assertEqual(self.w.api.starts, [])

    def test_a_left_over_start_of_an_old_parent_is_only_looked_up(self):
        # started under thread-orch, answer lost, then the config names a new orchestrator
        self.item('w1')
        self.w.api.crash_after_start = True
        with self.assertRaises(Crash):
            self.run_filler()
        self.cfg['parentThreadId'] = 'thread-new'
        self.run_filler()
        self.assertEqual([(b['parentThreadId'], bool(b.get('lookupOnly'))) for b in self.w.api.starts],
                         [('thread-orch', False), ('thread-orch', True)])
        self.assertEqual(self.started_ids(), ['w1'])
        self.assertEqual(len(self.w.api.by_request), 1)

    def test_an_unstarted_start_of_an_old_parent_is_never_started(self):
        self.item('w1')
        real = self.w.api.start
        self.w.api.start = lambda body: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            self.run_filler()
        self.w.api.start = real
        self.cfg['parentThreadId'] = 'thread-new'
        self.run_filler()
        self.run_filler()
        self.assertTrue(all(b.get('lookupOnly') for b in self.w.api.starts))
        self.assertEqual(self.w.api.by_request, {})
        self.assertEqual(len(os.listdir(os.path.join(self.state, 'inflight'))), 1)
        self.assertTrue(any('under the old parent thread-orch' in l for l in self.log_lines()))

    def test_a_crash_before_the_start_starts_it_once_later(self):
        self.item('w1')
        real = self.w.api.start
        self.w.api.start = lambda body: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            self.run_filler()
        self.w.api.start = real
        self.run_filler()
        self.run_filler()
        self.assertEqual(self.started_ids(), ['w1'])
        self.assertEqual(len(self.w.api.starts), 1)

    def test_a_crash_after_the_record_finishes_without_asking(self):
        self.item('w1')
        self.w.api.crash_after_start = True
        with self.assertRaises(Crash):
            self.run_filler()
        intent = json.load(open(os.path.join(self.state, 'inflight', 'w1.json')))
        os.makedirs(os.path.join(self.q, 'started'))
        with open(os.path.join(self.q, 'started', 'w1.json'), 'w') as fh:
            json.dump(dict(intent['item'], taskId='task-1', requestId=intent['requestId']), fh)
        asks = len(self.w.api.starts)
        self.run_filler()
        self.assertEqual(len(self.w.api.starts), asks)
        self.assertFalse(os.path.exists(os.path.join(self.q, 'w1.json')))
        self.assertTrue(any('finished w1: it was started and recorded before a stop' in l for l in self.log_lines()))

    def test_t3_down_keeps_the_marker_and_logs_once(self):
        self.item('w1')
        self.w.api.crash_after_start = True
        with self.assertRaises(Crash):
            self.run_filler()
        self.w.api.down = True
        self.assertEqual(self.run_filler(), 1)
        self.assertEqual(self.run_filler(), 1)
        self.assertEqual(len(os.listdir(os.path.join(self.state, 'inflight'))), 1)
        self.assertEqual(sum('stopped this run' in l for l in self.log_lines()), 1)
        self.assertEqual(sum('asking again for w1' in l for l in self.log_lines()), 0)   # the parent check comes first
        self.w.api.down = False
        self.assertEqual(self.run_filler(), 0)
        self.assertEqual(sum('asking again for w1' in l for l in self.log_lines()), 1)
        self.assertEqual(self.started_ids(), ['w1'])
        self.assertTrue(any(l.endswith('T3 answers again') for l in self.log_lines()))

    def test_a_queue_file_deleted_mid_start_drops_the_intent(self):
        self.item('w1')
        self.w.api.start = lambda body: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            self.run_filler()
        os.remove(os.path.join(self.q, 'w1.json'))
        self.run_filler()
        self.assertEqual(os.listdir(os.path.join(self.state, 'inflight')), [])
        self.assertTrue(any('dropped the intent for w1' in l for l in self.log_lines()))
        self.assertEqual(os.listdir(os.path.join(self.state, 'requests')), [])

    def test_a_second_run_while_one_holds_the_lock_does_nothing(self):
        self.item('w1')
        os.makedirs(self.state, exist_ok=True)
        fd = os.open(os.path.join(self.state, 'filler.lock'), os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.assertEqual(self.run_filler(), 0)
        finally:
            os.close(fd)
        self.assertEqual(self.w.api.starts, [])


class Quiet(Base):
    def test_a_second_run_with_nothing_new_writes_nothing(self):
        self.cfg['machines']['north']['maxAgents'] = 0
        self.cfg['machines']['local']['maxAgents'] = 1
        self.w.prs[7] = {'state': 'OPEN', 'headRefOid': HEAD, 'checks': []}
        self.item('w1')
        self.item('w2')
        self.item('w3', readyWhen={'type': 'pr-green', 'pr': 7})
        self.write('queue/bad.json', '{')
        self.write('queue/lens-watch/pr7.json', json.dumps({'pr': 7, 'lenses': [{'lens': 'code'}]}))
        self.run_filler()
        self.assertEqual(self.started_ids(), ['w1'])
        before, lines = self.snapshot(), self.log_lines()
        self.run_filler()
        self.run_filler()
        self.assertEqual(self.log_lines(), lines)
        self.assertEqual(self.snapshot(), before)

    def test_a_reason_that_changes_is_logged_again(self):
        self.cfg['machines']['north']['maxAgents'] = 0
        self.w.prs[7] = {'state': 'OPEN', 'headRefOid': HEAD, 'checks': []}
        self.item('w1', readyWhen={'type': 'pr-merged', 'pr': 7})
        self.run_filler()
        self.w.prs[7]['state'] = 'CLOSED'
        self.run_filler()
        self.run_filler()
        waits = [l for l in self.log_lines() if 'waiting w1' in l]
        self.assertEqual(len(waits), 2)


class BrokenQueueFiles(Base):
    def test_empty_broken_and_incomplete_files_are_refused_and_kept(self):
        self.write('queue/empty.json', '')
        self.write('queue/notjson.json', '{"id": ')
        self.write('queue/list.json', '[1, 2]')
        self.write('queue/wrongid.json', json.dumps({'id': 'other', 'title': 't', 'kind': 'writer', 'brief': '/x'}))
        self.write('queue/nokind.json', json.dumps({'id': 'nokind', 'title': 't', 'brief': self.rules}))
        self.write('queue/nobrief.json', json.dumps({'id': 'nobrief', 'title': 't', 'kind': 'writer',
                                                     'brief': os.path.join(self.tmp, 'gone.md')}))
        self.write('blank.md', '')
        self.write('queue/blankbrief.json', json.dumps({'id': 'blankbrief', 'title': 't', 'kind': 'writer',
                                                        'brief': os.path.join(self.tmp, 'blank.md')}))
        self.write('queue/badready.json', json.dumps({'id': 'badready', 'title': 't', 'kind': 'writer',
                                                      'brief': self.rules, 'readyWhen': {'type': 'pr-green'}}))
        self.write('queue/badmachine.json', json.dumps({'id': 'badmachine', 'title': 't', 'kind': 'writer',
                                                        'brief': self.rules, 'machines': ['south']}))
        names = sorted(os.listdir(self.q))
        self.run_filler()
        self.assertEqual(self.w.api.starts, [])
        self.assertEqual(sorted(n for n in os.listdir(self.q) if n.endswith('.json')), names)
        lines = self.log_lines()
        for name, why in (('empty', 'empty file'), ('notjson', 'not JSON'), ('list', 'not a JSON object'),
                          ('wrongid', '"id" must equal the file name'), ('nokind', '"kind" must be one of'),
                          ('nobrief', 'is missing'), ('blankbrief', 'is empty'), ('badready', '"readyWhen" must'),
                          ('badmachine', '"machines" must list')):
            self.assertTrue(any(f'refused {name}.json: ' in l and why in l and l.endswith('; kept') for l in lines),
                            name)
        self.run_filler()
        self.assertEqual(self.log_lines(), lines)

    def test_a_brief_deleted_after_queueing_is_refused_until_it_returns(self):
        self.cfg['machines']['north']['maxAgents'] = 0
        self.item('w1')
        brief = os.path.join(self.tmp, 'briefs', 'w1.md')
        os.rename(brief, brief + '.away')
        self.run_filler()
        self.assertEqual(self.started_ids(), [])
        os.rename(brief + '.away', brief)
        self.run_filler()
        self.assertEqual(self.started_ids(), ['w1'])

    def test_missing_queue_folder_and_bad_config(self):
        shutil.rmtree(self.q)
        self.assertEqual(self.run_filler(), 0)
        self.assertEqual(self.w.api.starts, [])
        cfg = self.write('cfg.json', '{"queueDir": "/q"}')
        with self.assertRaises(sf.ConfigError):
            sf.load_config(cfg)
        r = subprocess.run([sys.executable, '-I', '-B', BIN, '--config', cfg], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn('"stateDir" is required', r.stderr)

    def test_an_item_whose_id_was_already_started_is_refused(self):
        self.cfg['machines']['north']['maxAgents'] = 0
        self.item('w1')
        self.run_filler()
        self.item('w1')
        self.run_filler()
        self.assertEqual(len(self.w.api.starts), 1)
        self.assertTrue(any('refused w1.json: started/w1.json exists' in l for l in self.log_lines()))


class Lenses(Base):
    def setUp(self):
        super().setUp()
        self.cfg['machines']['local']['maxAgents'] = 10
        self.cfg['machines']['north']['maxAgents'] = 0
        self.write('queue/lens-watch/pr7.json', json.dumps(
            {'pr': 7, 'lenses': [{'lens': 'code', 'focus': 'the state question'}, {'lens': 'security'}],
             'priority': 3}))

    def test_green_ci_renders_and_starts_one_lens_item_per_lens(self):
        self.w.prs[7] = {'state': 'OPEN', 'headRefOid': HEAD, 'checks': []}
        self.run_filler()
        self.assertEqual(self.started_ids(), [])
        self.w.prs[7] = green()
        self.run_filler()
        self.assertEqual(self.started_ids(), ['lens-7-code-a1b2c3d', 'lens-7-security-a1b2c3d'])
        body = self.w.api.starts[0]['task']
        self.assertEqual(body['role'], 'review')
        brief = open(os.path.join(self.q, 'briefs', 'lens-7-code-a1b2c3d.md')).read()
        self.assertIn(f'PR #7 at head {HEAD}, lens code. Focus: the state question.', brief)
        self.assertIn(f'Read {self.rules} fully and follow it.', brief)
        self.run_filler()
        self.assertEqual(len(self.w.api.starts), 2)

    def test_a_new_head_is_a_new_round_and_rounds_stop_at_the_limit(self):
        for h in (HEAD, HEAD2, HEAD3):
            self.w.prs[7] = green(h)
            self.run_filler()
        self.assertEqual(self.started_ids(), ['lens-7-code-a1b2c3d', 'lens-7-code-bbbbbbb',
                                              'lens-7-security-a1b2c3d', 'lens-7-security-bbbbbbb'])
        self.assertTrue(any('not rendering lens-7-code-ccccccc: 2 rounds' in l for l in self.log_lines()))

    def test_a_broken_watch_file_or_missing_rules_is_refused(self):
        self.write('queue/lens-watch/bad.json', '{"pr": "seven"}')
        os.remove(self.rules)
        self.w.prs[7] = green()
        self.run_filler()
        lines = self.log_lines()
        self.assertTrue(any('refused lens-watch/bad.json' in l for l in lines))
        self.assertTrue(any('waiting lens-watch/pr7.json: the lens rules file' in l for l in lines))
        self.assertEqual(self.started_ids(), [])


MUTATIONS = {
    'settled children free their slot': (
        "if not c.get('live', c.get('status') not in SETTLED):", "if False:",
        'SlotCounting.test_live_children_count_per_machine_and_settled_ones_do_not'),
    'writer cap': (
        "if kind == 'writer' and c['writers'] >=", "if False and c['writers'] >=",
        'Caps.test_starts_by_priority_until_every_machine_is_full'),
    'priority order': (
        "items.sort(key=lambda x: (-x[0]['priority'], x[1], x[0]['id']))", "items.sort(key=lambda x: x[0]['id'])",
        'Caps.test_starts_by_priority_until_every_machine_is_full'),
    'usage full': (
        "if (w.get('usedPercent') or 0) >= maxp:", "if (w.get('usedPercent') or 0) > 200:",
        'Usage.test_full_or_old_meters_skip_the_machine'),
    'usage age': (
        "if age > (u.get('maxAgeMinutes') or 90):", "if age > 100000:",
        'Usage.test_full_or_old_meters_skip_the_machine'),
    'green on the newest run': (
        "p['green'] = bool(newest and newest.get('status') == 'completed' and newest.get('conclusion') == 'success')",
        "p['green'] = any(r.get('conclusion') == 'success' for r in runs)",
        'ReadyWhen.test_pr_green_on_its_current_head'),
    'one request id per item': (
        "        req = self.read_request(iid)\n        if req is None or", "        req = None\n        if req is None or",
        'StopsHalfWay.test_a_lost_answer_then_a_refused_retry_never_starts_twice'),
    'refusals back off': (
        "wait = min(BACKOFF_MAX_S, BACKOFF_FIRST_S * 2 ** (n - 1))", "wait = 0",
        'Caps.test_refusals_back_off_doubling_under_one_request_id'),
    'request_rejected gets a new id': (
        "        if code == 'request_rejected':\n            req['requestId']", "        if False:\n            req['requestId']",
        'Caps.test_a_request_id_refused_for_good_is_replaced'),
    'an unclear answer pins the machine': (
        "        if code in DEFINITE:\n", "        if True:\n",
        'StopsHalfWay.test_an_unclear_remote_answer_never_starts_on_another_machine'),
    'a clear refusal frees the item': (
        "    'usage_pacing_hold', 'slots_full',", "    'usage_pacing_hold',",
        'StopsHalfWay.test_an_unclear_answer_then_a_clear_refusal_moves_on'),
    'an old parent is only looked up': (
        "        look_only = parent != self.cfg['parentThreadId']\n", "        look_only = False\n",
        'StopsHalfWay.test_an_unstarted_start_of_an_old_parent_is_never_started'),
    'settled before recovery': (
        "        children = self.children()     #", "        self.recover(); children = self.children()     #",
        'StopsHalfWay.test_a_settled_parent_stops_before_a_left_over_start_is_asked'),
    'a far wait is capped': (
        "        if nxt - now > BACKOFF_MAX_S:\n", "        if False:\n",
        'Caps.test_a_wait_set_far_ahead_is_capped'),
    'backoff per machine': (
        "                backoff = self.waiting_on(it['id'], name)\n",
        "                backoff = self.waiting_on(it['id'], 'local') or self.waiting_on(it['id'], 'north')\n",
        'Caps.test_an_offline_machine_does_not_hold_back_a_free_one'),
    'token file mode': (
        "                if mode & 0o077:\n", "                if False:\n",
        'Caps.test_the_token_file_must_be_private'),
    'settled parent stops': (
        "        if answer.get('parentSettled') is True:\n", "        if False:\n",
        'Caps.test_a_parent_settled_by_hand_stops_the_run'),
    'notes stay quiet': (
        "        if AGE_RE.sub('is old', self.notes.get(key) or '') != AGE_RE.sub('is old', line):\n", "        if True:\n",
        'Quiet.test_a_second_run_with_nothing_new_writes_nothing'),
    'broken files are refused': (
        "        if not text.strip():\n            return None, 'empty file'\n", "",
        'BrokenQueueFiles.test_empty_broken_and_incomplete_files_are_refused_and_kept'),
    'account fallback': (
        "            if r is None:\n                return account, '; '.join(skipped) or None\n",
        "            if True:\n                return account, None\n",
        'AccountFallback.test_a_full_first_account_falls_back_to_the_second'),
    'worktree isolation is checked': (
        "        return bool(m.get('worktreeIsolation', not m.get('environmentId')))\n",
        "        return True\n",
        'LinkedMachines.test_worktree_isolation_on_a_machine_without_it_is_refused_when_read'),
    'a usage refusal holds only that account': (
        "        usage = bool(account) and code.startswith('usage_')\n", "        usage = False\n",
        'AccountFallback.test_a_pacing_hold_on_the_first_moves_to_the_second_while_its_meter_is_low'),
    'lens rounds': (
        "if len(rounds) >= int(lw.get('maxRounds', 2)):", "if False:",
        'Lenses.test_a_new_head_is_a_new_round_and_rounds_stop_at_the_limit'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        with open(os.path.join(KIT, 'bin', 'slot-filler')) as f:
            source = f.read()
        for rule, (text, broken, test) in MUTATIONS.items():
            with self.subTest(rule=rule):
                self.assertEqual(source.count(text), 1, f'{rule}: the text is not in bin/slot-filler exactly once')
                tmp = tempfile.mkdtemp(prefix='slot-filler-red-')
                try:
                    copy = os.path.join(tmp, 'slot-filler')
                    with open(copy, 'w') as f:
                        f.write(source.replace(text, broken))
                    r = subprocess.run([sys.executable, '-I', '-B', THIS, test], capture_output=True, text=True,
                                       timeout=300, env=dict(os.environ, SLOT_FILLER_BIN=copy),
                                       stdin=subprocess.DEVNULL)
                    self.assertIn('Ran 1 test', r.stderr, f'{rule}: {test} did not run:\n{r.stderr[-800:]}')
                    self.assertNotEqual(r.returncode, 0, f'{test} still passes with {rule!r} broken')
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
