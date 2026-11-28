"""The lane claims contract: one test file any project runs against its adapter.

A project that puts its own code in front of claims.py (its lane ids, its server names, a holder that is a service
unit) runs these tests against that code:

    import sys, unittest
    sys.path.insert(0, '<pinned fork-ci>/lanes')
    import contract

    class MyLanes(contract.LaneContract, unittest.TestCase):
        servers = ('api', 'db')                       # two of the project's servers
        def claims(self, folder, alive, clock):       # the adapter: claim/release/withdraw/sweep/holders
            return MyClaims(folder, alive=alive, clock=clock)

The fork runs it against claims.Claims itself (lanes.test.py). Each test answers one question; no test sleeps or
reads the wall clock (the clock and who is alive are the test's).
"""
import json, os, stat, tempfile, shutil, sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import claims as claims_mod   # noqa: E402


def holder(n):
    return {'pid': 100000 + n, 'start': f's{n}'}


class LaneContract:
    servers = ('a', 'b')

    def claims(self, folder, alive, clock):
        return claims_mod.Claims(folder, alive=alive, clock=clock)

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix='lanes-contract-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.folder = os.path.join(self.dir, 'claims')
        self.dead, self.now = set(), [1000.0]
        self.c = self.make(self.folder)

    def make(self, folder):
        return self.claims(folder, lambda h: h.get('pid') not in self.dead, self.tick)

    def tick(self):
        self.now[0] += 1
        return self.now[0]

    def take(self, n, mode='shared', server=None, restart=False, admit=None):
        return self.c.claim(server or self.servers[0], mode, holder(n), f'lane{n}', restart=restart, admit=admit)

    def granted(self, *a, **kw):
        token, why = self.take(*a, **kw)
        self.assertIsNotNone(token, why)
        return token

    def refused(self, *a, **kw):
        token, why = self.take(*a, **kw)
        self.assertIsNone(token, why)
        return why

    def test_the_conflict_table(self):
        # Which pairs of claims share one server? Only shared beside shared; another server never conflicts.
        for held, wanted, ok in (('shared', 'shared', True), ('shared', 'alone', False), ('alone', 'shared', False),
                                 ('alone', 'alone', False)):
            with self.subTest(held=held, wanted=wanted):
                t = self.granted(1, held)
                token, why = self.take(2, wanted)
                self.assertEqual(token is not None, ok, why)
                self.assertIsNotNone(self.take(3, wanted, server=self.servers[1])[0])   # another server: free
                for tok in [t, token] + [c['id'] for c in self.c.holders()]:
                    if tok:
                        self.c.release(tok)
                self.assertEqual(self.c.holders(), [])

    def test_a_waiting_restart_is_not_starved(self):
        # Can a stream of shared runs keep a restart out? No: once it waits, new claims wait behind it.
        a = self.granted(1)
        self.assertIn('held shared', self.refused(9, 'alone', restart=True))
        self.assertIn('waiting restart goes first', self.refused(2))   # a new shared run waits behind it
        self.c.release(a)
        r = self.granted(9, 'alone', restart=True)
        self.refused(2)   # held alone now
        self.c.release(r)
        self.granted(2)

    def test_the_oldest_waiting_restart_goes_first(self):
        a = self.granted(1)
        self.refused(8, 'alone', restart=True)
        self.refused(9, 'alone', restart=True)
        self.c.release(a)
        self.assertIn('waiting restart goes first', self.refused(9, 'alone', restart=True))
        self.granted(8, 'alone', restart=True)

    def test_a_dead_holder_is_swept(self):
        # Does a holder that died (its claim never released) block the server? No: the next call sweeps it.
        self.granted(1, 'alone')
        self.refused(2)
        self.dead.add(holder(1)['pid'])
        self.granted(2)
        self.assertEqual([c['lane'] for c in self.c.holders()], ['lane2'])

    def test_a_dead_waiting_restart_is_swept(self):
        a = self.granted(1)
        self.refused(9, 'alone', restart=True)
        self.dead.add(holder(9)['pid'])
        self.granted(2)
        self.c.release(a)
        self.assertEqual([c['kind'] for c in self.c.holders()], ['claim'])

    def test_a_release_twice_or_after_a_sweep_is_fine(self):
        t = self.granted(1, 'alone')
        self.c.release(t)
        self.c.release(t)
        self.granted(2, 'alone')

    def test_an_empty_or_missing_folder_holds_nothing(self):
        self.assertEqual(self.c.holders(), [])   # missing: made on first use
        os.makedirs(self.folder, exist_ok=True)
        self.granted(1, 'alone')

    def test_an_unreadable_folder_grants_nothing(self):
        # A folder it cannot read: never a grant on a guess.
        if os.geteuid() == 0:
            self.skipTest('root reads any folder')
        self.granted(1)
        os.chmod(self.folder, stat.S_IWUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, self.folder, stat.S_IRWXU)
        with self.assertRaises(claims_mod.ClaimsError):
            self.take(2)

    def plant(self, name, body):
        os.makedirs(self.folder, exist_ok=True)
        path = os.path.join(self.folder, name)
        with open(path, 'w') as f:
            f.write(body)
        return path

    def test_a_planted_or_garbage_claim_is_swept(self):
        # Does a claim file this version never writes (junk, an empty holder, a pid with no start time, which would
        # read as alive for ever) block the server? No: it is removed at the next call, which is granted.
        bodies = ('{"kind": "claim", "ser', '{}', '[]',
                  '{"kind": "claim", "mode": "alone", "server": "%s", "holder": {}}',
                  '{"kind": "claim", "mode": "alone", "server": "%s", "holder": {"pid": 1}}',
                  '{"kind": "claim", "mode": "alone", "server": "%s", "holder": {"pid": "x", "start": "s"}}')
        for i, body in enumerate(bodies):
            with self.subTest(body=body):
                path = self.plant(f'x{i}' + claims_mod.SUFFIX, body.replace('%s', self.servers[0]))
                t = self.granted(1, 'alone')
                self.assertFalse(os.path.exists(path))
                self.c.release(t)

    def test_a_claim_of_a_newer_version_holds_only_its_server(self):
        # A claim a newer release wrote (its shape may be one this version cannot judge): never swept, and it holds
        # the server it names (all of them when it names none); another server is free.
        a, b = self.servers
        path = self.plant('x' + claims_mod.SUFFIX, '{"v": 999, "kind": "claim", "server": "%s"}' % b)
        self.granted(1, 'alone', server=a)
        self.assertIn('newer release', self.refused(2, server=b))
        self.assertTrue(os.path.exists(path))
        os.unlink(path)
        path = self.plant('y' + claims_mod.SUFFIX, '{"v": 999, "kind": "claim"}')
        self.assertIn('newer release', self.refused(3, server=b))
        self.assertTrue(os.path.exists(path))

    def test_a_claim_with_a_field_this_version_does_not_know_is_honoured(self):
        # A claim of this version with one field more (in the entry or its holder): is it swept as garbage? No: it
        # holds its server, judged by the fields this version knows, and is swept once its holder is gone.
        body = {'v': claims_mod.VERSION, 'kind': 'claim', 'mode': 'alone', 'server': self.servers[0],
                'holder': {**holder(1), 'extra': 1}, 'lane': 'lane1', 'since': 1.0, 'tag': 'x'}
        path = self.plant('x' + claims_mod.SUFFIX, json.dumps(body))
        self.refused(2)
        self.assertTrue(os.path.exists(path))
        self.dead.add(holder(1)['pid'])
        self.granted(2)

    def test_a_temporary_file_a_crash_left_is_removed(self):
        # A writer that died between writing its temporary file and the rename leaves `.tmp-*`: the next call (under
        # the lock, so no writer is half-way) removes it.
        path = self.plant('.tmp-00000000000000000001-1-abcdef', '{"kind": "cl')
        self.granted(1)
        self.assertFalse(os.path.exists(path))

    def test_lanes_are_counted_by_holder(self):
        # Two runners that pass the same lane name are two lanes against admission, not one; one lane's two servers
        # are one.
        self.c.claim(self.servers[0], 'shared', holder(1), 'same')
        self.c.claim(self.servers[1], 'shared', holder(1), 'same')
        self.c.claim(self.servers[0], 'shared', holder(2), 'same')
        seen = []
        self.c.claim(self.servers[1], 'shared', holder(3), 'x',
                     admit=lambda lanes: (seen.append(len(lanes)), (True, ''))[1])
        self.assertEqual(seen, [2])

    def test_a_restart_keeps_its_place_on_every_server(self):
        # A restart that needs two servers, refused on one: does a younger claim get in on the other meanwhile? No:
        # queue() holds its place there too, and keep_wait keeps the wait while it holds a server.
        a, b = self.servers
        s1 = self.c.claim(b, 'shared', holder(1), 'lane1')[0]
        r = self.c.claim(a, 'alone', holder(9), 'lane9', restart=True, keep_wait=True)[0]
        self.assertIsNotNone(r)
        self.assertIsNone(self.c.claim(b, 'alone', holder(9), 'lane9', restart=True, keep_wait=True)[0])
        self.c.release(r)
        self.c.queue(a, holder(9), 'lane9')
        token, why = self.c.claim(a, 'shared', holder(2), 'lane2')
        self.assertIsNone(token, why)
        self.assertIn('waiting restart goes first', why)
        self.c.release(s1)
        self.assertIsNotNone(self.c.claim(a, 'alone', holder(9), 'lane9', restart=True, keep_wait=True)[0])
        self.assertIsNotNone(self.c.claim(b, 'alone', holder(9), 'lane9', restart=True, keep_wait=True)[0])

    def test_waits_of_one_lane_share_one_place(self):
        # Two restarts that each need a and b never deadlock: a lane's waits carry the time it began waiting, so the
        # same lane is first on every server they share.
        a, b = self.servers
        s1 = self.c.claim(b, 'shared', holder(1), 'lane1')[0]
        self.c.queue(a, holder(8), 'lane8', since=10.0)
        self.c.queue(b, holder(9), 'lane9', since=20.0)
        self.c.queue(a, holder(9), 'lane9', since=20.0)
        self.c.queue(b, holder(8), 'lane8', since=10.0)
        self.c.release(s1)
        self.assertIsNone(self.c.claim(a, 'alone', holder(9), 'lane9', restart=True, keep_wait=True)[0])
        self.assertIsNotNone(self.c.claim(a, 'alone', holder(8), 'lane8', restart=True, keep_wait=True)[0])
        self.assertIsNotNone(self.c.claim(b, 'alone', holder(8), 'lane8', restart=True, keep_wait=True)[0])

    def test_a_refused_admission_claims_nothing(self):
        # The pool says no: nothing is held (and a restart still waits first in line).
        self.refused(1, admit=lambda lanes: (False, 'no memory'))
        self.assertEqual(self.c.holders(), [])
        self.refused(9, 'alone', restart=True, admit=lambda lanes: (False, 'no memory'))
        self.assertEqual([c['kind'] for c in self.c.holders()], ['wait'])
        seen = []
        self.granted(9, 'alone', restart=True, admit=lambda lanes: (seen.append(lanes), (True, ''))[1])
        self.assertEqual(seen, [[]])

    def test_two_users_of_one_folder_see_each_other(self):
        # Two processes' views of one folder: a claim made through one holds against the other.
        other = self.make(self.folder)
        self.granted(1, 'alone')
        token, why = other.claim(self.servers[0], 'shared', holder(2), 'lane2')
        self.assertIsNone(token, why)

    def test_withdraw_ends_a_wait(self):
        a = self.granted(1)
        self.refused(9, 'alone', restart=True)
        self.c.withdraw(self.servers[0], holder(9), 'lane9')
        self.c.release(a)
        self.granted(2)
