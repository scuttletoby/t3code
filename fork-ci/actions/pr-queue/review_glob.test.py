"""Tests of review_glob.py (run: python3 fork-ci/actions/pr-queue/review_glob.test.py).

The `review` block's glob dialect: `*` inside one segment, `**` any number of whole segments, `?` one character. The
shared vectors (review.vectors.json, `globs`) are the contract both sides test; the rest here are this side's own
checks: against a slow reference matcher on many generated globs and paths, against review_config's catch-all rule,
and that no shortcut (fnmatch) crept in.
"""
import itertools, json, os, re, sys, unittest

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import review_config as rc  # noqa: E402
import review_glob as rg  # noqa: E402

with open(os.path.join(HERE, 'review.vectors.json'), encoding='utf-8') as _f:
    VECTORS = json.load(_f)


def reference(glob, path):
    """A slow, obviously right matcher: recursion over segments, and a recursion over characters inside one."""
    def seg(g, s):
        if not g:
            return not s
        if g[0] == '*':
            return any(seg(g[1:], s[i:]) for i in range(len(s) + 1))
        return bool(s) and (g[0] == '?' or g[0] == s[0]) and seg(g[1:], s[1:])

    def whole(g, p):
        if not g:
            return not p
        if g[0] == '**':
            return any(whole(g[1:], p[i:]) for i in range(len(p) + 1))
        return bool(p) and seg(g[0], p[0]) and whole(g[1:], p[1:])
    return whole(glob.split('/'), path.split('/'))


class Vectors(unittest.TestCase):
    def test_every_shared_glob_vector(self):
        self.assertGreaterEqual(len(VECTORS['globs']), 40)
        for glob, path, want in VECTORS['globs']:
            with self.subTest(glob=glob, path=path):
                self.assertIsNone(rc.glob_problem(glob), 'every vector glob is in the dialect')
                self.assertIs(rg.matches(glob, path), want)
                self.assertIs(reference(glob, path), want, 'the reference agrees with the vector')

    def test_the_vectors_cover_the_cases_the_brief_names(self):
        globs = {g for g, _, _ in VECTORS['globs']}
        self.assertTrue(any(g.startswith('**/') for g in globs), '** at the start')
        self.assertTrue(any('/**/' in g for g in globs), '** in the middle')
        self.assertTrue(any(g.endswith('/**') for g in globs), '** at the end')
        crossing = [(g, p) for g, p, want in VECTORS['globs'] if '*' in g and '**' not in g and not want and '/' in p]
        self.assertTrue(crossing, '* not crossing /')
        self.assertTrue(any(want and any(s.startswith('.') for s in p.split('/')) for _, p, want in VECTORS['globs']),
                        'dot-names')


class Reference(unittest.TestCase):
    ALPHABET = ('a', 'b', '.x', 'ab', 'a.b')
    GLOB_SEGMENTS = ('**', '*', '?', 'a', 'b', 'a*', '*b', '?b', '.*', '*.*', 'a?b', '*a*')

    def test_matches_the_reference_on_every_small_glob_and_path(self):
        paths = ['/'.join(p) for n in range(1, 4) for p in itertools.product(self.ALPHABET, repeat=n)]
        globs = ['/'.join(g) for n in range(1, 4) for g in itertools.product(self.GLOB_SEGMENTS, repeat=n)]
        checked = 0
        for glob in globs:
            for path in paths:
                if rg.matches(glob, path) != reference(glob, path):
                    self.fail(f'{glob!r} on {path!r}: {rg.matches(glob, path)}, reference {reference(glob, path)}')
                checked += 1
        self.assertGreater(checked, 100000)

    def test_a_deep_path_and_many_double_stars_are_quick(self):   # no recursion, no exponential blow-up
        path = '/'.join(['a'] * 500) + '/b'
        self.assertTrue(rg.matches('/'.join(['**'] * 50) + '/b', path))
        self.assertFalse(rg.matches('/'.join(['**', 'a'] * 50) + '/c', path))
        self.assertFalse(rg.matches('*' * 40 + 'c', 'a' * 200))   # a regex backtracks on this for hours
        self.assertFalse(rg.matches('*a' * 30 + '*b', 'a' * 300))
        self.assertTrue(rg.matches('*a' * 30 + '*b', 'a' * 300 + 'b'))


class Dialect(unittest.TestCase):
    def test_every_catch_all_review_config_accepts_matches_every_path(self):
        """review_config calls a rule the catch-all by its glob alone; the matcher must agree on every path, or a
        path could fall to no rule after the parser said it cannot."""
        paths = ['a', '.a', 'a/b', '.github/workflows/ci.yml', 'x/y/z/w.md', 'a b/c']
        globs = ['/'.join(g) for n in range(1, 4) for g in itertools.product(('**', '*', '?', '*?', '?*', '*?*', '??',
                                                                                    'a'), repeat=n)]
        catch_alls = [g for g in globs if rc.glob_problem(g) is None and rc.matches_every_path(g)]
        self.assertIn('**', catch_alls)
        self.assertIn('*/**', catch_alls)
        self.assertIn('**/?*', catch_alls)
        for g in catch_alls:
            for p in paths:
                self.assertTrue(rg.matches(g, p), f'{g!r} is a catch-all to the parser but misses {p!r}')
        for g in set(globs) - set(catch_alls):   # and the parser's "not a catch-all" misses some path
            if rc.glob_problem(g) is None:
                self.assertFalse(all(rg.matches(g, p) for p in paths + ['ab/cd/ef/gh']), g)

    def test_characters_regex_would_read_are_plain(self):
        self.assertTrue(rg.matches('a.b', 'a.b'))
        self.assertFalse(rg.matches('a.b', 'axb'))
        self.assertTrue(rg.matches('a+(b)|$^', 'a+(b)|$^'))

    def test_case_sensitive(self):
        self.assertFalse(rg.matches('README.md', 'readme.md'))

    def test_no_fnmatch_glob_or_pathlib(self):   # their * crosses / or skips dot-names
        with open(os.path.join(HERE, 'review_glob.py'), encoding='utf-8') as f:
            code = f.read()
        self.assertFalse(re.search(r'^\s*(import|from)\s+(fnmatch|glob|pathlib)\b', code, re.M))


if __name__ == '__main__':
    unittest.main()
