"""The `review` block of the queue's config: which paths need which lens reads, and the round cap (README.md, "Review
rules").

Parsed and checked here; review_judge.py judges a change by it (the head gate). Strict like config.py: every key is
required, an unknown key or a wrong value is an error, and every problem is named at once. A rule set that could leave
a changed path without the lenses its weight needs is refused, never repaired: no hidden defaults in the merge path.

Globs use one small dialect, so the Python gate and T3 match a path the same way: `*` any characters inside one path
segment, `**` any number of whole segments (only as a segment of its own), `?` one character inside a segment. Paths
are repo-relative with `/`. No braces, character classes, `!` or backslashes. Matching is case-sensitive, and `*`,
`**` and `?` also match names that start with `.` (`**` matches `.github/x`): a matcher that skips dot-names by
default must be told not to.

The catch-all rule is the first rule with a glob that matches every path (`**`, `**/*`, `*/**`, `**/?*` ...), however
it is written; a rule after it can never match.
"""
import json
import re
from collections import deque
from dataclasses import dataclass
from functools import lru_cache

import lens
from config import ConfigError

VERSION = 1
MODES = ('shadow', 'enforce')
WEIGHTS = ('critical', 'normal', 'light')   # heaviest first
CATCH_ALL = '**'
KEYS = ('version', 'mode', 'rules', 'lenses', 'min_lenses', 'rounds', 'findings', 'state_line')
RULE_KEYS = ('name', 'weight', 'paths', 'lenses')
ROUND_KEYS = ('approach_at', 'max', 'approach_lens')
MAX_LENSES = 10
MAX_ROUNDS = 20
# Characters of glob dialects this one does not have: a project that wrote `{a,b}`, `[ab]` or `!x` would otherwise get
# a rule that silently matches nothing it meant.
NOT_IN_DIALECT = re.compile(r'[{}\[\]!\\]')
# The end of each shadow order warning: the config that judges a head is the base's, so a base that switched to enforce
# with this order would refuse every head, the one that fixes the order too.
SWITCH = 'mode enforce refuses this config: fix it before switching'


@dataclass(frozen=True)
class Rule:
    name: str
    weight: str
    paths: tuple
    lenses: tuple


@dataclass(frozen=True)
class Lens:
    name: str
    prompt: str


@dataclass(frozen=True)
class MinLenses:
    critical: int
    normal: int
    light: int


@dataclass(frozen=True)
class Rounds:
    approach_at: int
    max: int
    approach_lens: str


@dataclass(frozen=True)
class ReviewRules:
    version: int
    mode: str
    rules: tuple      # of Rule, in the order written: a changed path belongs to the first rule that matches it
    lenses: tuple     # of Lens, in the order written
    min_lenses: MinLenses
    rounds: Rounds
    findings: bool
    state_line: bool
    # What the config check says but does not refuse (mode shadow only: a rule above another that takes its paths with
    # fewer lenses, which enforce refuses). Shown in the gate's summary row.
    warnings: tuple = ()
    # The rule-order problems themselves, in either mode (enforce_order_problems): empty in a parsed enforce block.
    order: tuple = ()

    def lens(self, name):
        return next((x for x in self.lenses if x.name == name), None)


def _text(v):
    return isinstance(v, str) and v.strip() == v and v != '' and '\n' not in v and '\r' not in v


def _int(v, lo, hi):
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _show(v):
    return json.dumps(v)[:80]


def exact_path_problem(p):
    """Why `p` is not a repo-relative file path spelled as git names a changed file (`/` between segments, no empty, .
    or .. segment), or None. Any other character is the file's own, glob characters too (`queue[main].json`): a path
    that is only ever compared exactly, never matched as a glob, needs nothing more."""
    if not _text(p):
        return 'must be a non-empty line without surrounding blanks'
    if p.startswith('/'):
        return 'must be repo-relative (no leading /)'
    if any(seg in ('', '.', '..') for seg in p.split('/')):
        return 'has an empty, . or .. segment'
    return None


def _path_problem(p):
    """Why `p` is no repo-relative path in `/` form of the dialect's characters, or None."""
    why = exact_path_problem(p)
    if why or not NOT_IN_DIALECT.search(p):
        return why
    return 'has a character the dialect does not have ({, }, [, ], !, \\)'


def _matches_any_segment(seg):
    """`seg` (no `**`) matches every path segment: segments are never empty, so `*`, `*?`, `*?*` do; `?` and `??`
    (exactly one or two characters) do not."""
    return set(seg) <= {'*', '?'} and '*' in seg and seg.count('?') <= 1


def matches_every_path(g):
    """The valid glob `g` matches every repo-relative path: it has a `**` segment and at most one other segment, which
    matches any segment (every path has at least one segment)."""
    segs = g.split('/')
    rest = [s for s in segs if s != '**']
    return len(rest) < len(segs) and len(rest) <= 1 and all(_matches_any_segment(s) for s in rest)


def _search(start, end, steps):
    """A shortest list of step labels from `start` to `end` over `steps(state)` -> [(label, next state)], or None.
    Breadth-first over a finite product of positions: at most len(a) * len(b) states, never exponential."""
    seen, todo = {start: None}, deque([start])
    while todo:
        state = todo.popleft()
        if state == end:
            labels = []
            while seen[state] is not None:
                state, label = seen[state]
                labels.append(label)
            return labels[::-1]
        for label, nxt in steps(state):
            if nxt not in seen:
                seen[nxt] = (state, label)
                todo.append(nxt)
    return None


@lru_cache(maxsize=4096)
def _segment_witness(a, b):
    """A path segment both glob segments `a` and `b` (no `**`) match, or None. Each segment is a small automaton (`*`
    any characters, `?` one, a letter itself); this walks both at once."""
    def steps(state):
        i, j = state
        out = []
        if i < len(a) and a[i] == '*':
            out.append(('', (i + 1, j)))
        if j < len(b) and b[j] == '*':
            out.append(('', (i, j + 1)))
        if i < len(a) and j < len(b):
            ca, cb = a[i], b[j]
            na, nb = (i if ca == '*' else i + 1), (j if cb == '*' else j + 1)
            wild_a, wild_b = ca in '*?', cb in '*?'
            if wild_a or wild_b or ca == cb:
                out.append((cb if wild_a and not wild_b else ca if not wild_a else 'x', (na, nb)))
        return out
    labels = _search((0, 0), (len(a), len(b)), steps)
    if labels is None:
        return None
    # Only two all-`*` segments meet on the empty text; they also meet on any letter (a segment is never empty).
    return ''.join(labels) or 'x'


def glob_witness(a, b):
    """A repo-relative path both valid globs `a` and `b` match, or None when no path matches both."""
    g, h = a.split('/'), b.split('/')

    def steps(state):
        i, j = state
        out = []
        if i < len(g) and g[i] == '**':
            out.append(('', (i + 1, j)))
        if j < len(h) and h[j] == '**':
            out.append(('', (i, j + 1)))
        if i < len(g) and j < len(h):
            sa, sb = g[i], h[j]
            seg = _segment_witness('*' if sa == '**' else sa, '*' if sb == '**' else sb)
            if seg is not None:
                out.append((seg, (i if sa == '**' else i + 1, j if sb == '**' else j + 1)))
        return out
    labels = _search((0, 0), (len(g), len(h)), steps)
    if labels is None:
        return None
    return '/'.join(x for x in labels if x) or 'x'


def globs_meet(a, b):
    """Some path matches both valid globs."""
    return glob_witness(a, b) is not None


def _shadowed(rules):
    """Why a rule above another takes paths the lower one names with fewer lenses: a changed path belongs to the first
    rule that matches it and gets only its lenses. A lighter rule above a heavier one (`**/*.md` light above `src/**`
    critical: `src/notes.md` needs no lens), or a rule of the same weight without one of the lower rule's lenses
    (`**/*.py` critical [a] above `src/**/*` critical [a, b]: `src/x.py` needs no b). Only the match-every globs of the
    catch-all rule are exempt: it is the default for every path no rule above names, so a lighter rule above it is the
    normal case, but an explicit glob it also names (`[src/**, **]`) says what that folder needs like any other rule's.
    Lens names match exactly: two names the gate reads as one lens are refused in `lenses` already."""
    out = []
    every = next((r for r in rules if any(matches_every_path(g) for g in r.paths)), None)
    for k, low in enumerate(rules):
        globs = [g for g in low.paths if not (low is every and matches_every_path(g))]
        for up in rules[:k]:
            if WEIGHTS.index(up.weight) < WEIGHTS.index(low.weight):
                continue   # a heavier rule above: its paths need more than the lower rule's weight
            same = up.weight == low.weight
            missing = [n for n in low.lenses if n not in up.lenses] if same else []
            if same and not missing:
                continue
            meet = next(((ga, gb, w) for ga in up.paths for gb in globs
                         for w in [glob_witness(ga, gb)] if w is not None), None)
            if not meet:
                continue
            ga, gb, w = meet
            if same:
                lenses = ', '.join(map(repr, missing))
                what = (f'rule {up.name!r} ({up.weight}) comes before the rule {low.name!r} of the same weight '
                        f'without its lens{"es" if len(missing) > 1 else ""} {lenses}')
                fix = 'put the rule with more lenses first, or give the first rule those lenses too'
            else:
                what = f'rule {up.name!r} ({up.weight}) comes before the heavier rule {low.name!r} ({low.weight})'
                fix = 'put the heavier rule first'
            out.append(f'{what}, and {ga!r} and {gb!r} both match a path such as {w!r}: such a path belongs to the '
                       f'first rule and gets only its lenses; {fix}')
    return out


def enforce_order_problems(raw):
    """The rule-order problems mode `enforce` would refuse the raw `review` block for, whatever mode it is written in;
    [] when the block has no rules to order (it does not parse for another reason: the base's parse refuses that, this
    check is only about the order). head_check refuses a pull request whose new config is in enforce with such an
    order: merged, it would refuse every head after it, the one that fixes the order too (the config that judges a head
    is the base's)."""
    if not isinstance(raw, dict):
        return []
    try:
        return list(parse_review({**raw, 'mode': 'shadow'}).order)
    except ConfigError:
        return []


def glob_problem(g):
    """Why `g` is not a glob of the dialect (module doc), or None."""
    why = _path_problem(g)
    if why:
        return why
    if any('**' in seg and seg != '**' for seg in g.split('/')):
        return '** must be a whole segment (write a/**/b or **/*.md, not a** or **.md)'
    return None


def prompt_problem(p):
    why = _path_problem(p)
    if why:
        return why
    if '*' in p or '?' in p:
        return 'is a file path, not a glob'
    return None


def _keys(obj, keys, where, problems):
    """True when `obj` is an object; names its unknown and missing keys."""
    if not isinstance(obj, dict):
        problems.append(f'{where} is {_show(obj)}: must be an object')
        return False
    problems.extend(f'{where}: unknown key {k!r} (known: {", ".join(keys)})' for k in obj if k not in keys)
    problems.extend(f'{where}: {k} is missing' for k in keys if k not in obj)
    return True


def _lens_name(name, where, problems):
    if not _text(name) or not lens.names_a_lens(name):
        problems.append(f'{where} {_show(name)} names no lens (a placeholder, a round marker, blanks or nothing)')
        return False
    # The gate reads a lens name out of a comment header after dropping Markdown and field text: a name it would read
    # as another one (`data *2*` reads as `data 2`) would count as two lenses here and as one there.
    header = lens.lens_header(f'lens: {name}')
    if header is None or lens.lens_key(header[0]) != lens.lens_key(name):
        read = 'nothing' if header is None else repr(header[0])
        problems.append(f'{where} {_show(name)} reads as {read} in a lens header (Markdown, a reviewer: or read at '
                        f'field): write it the way the gate reads it')
        return False
    return True


def _lenses(raw, problems):
    """{name: prompt} of the valid entries; problems for the rest, and for two names the gate reads as one lens."""
    out, by_key = {}, {}
    if not isinstance(raw, dict) or raw == {}:
        problems.append(f'lenses is {_show(raw)}: must be an object of lens name -> {{"prompt": "<file>"}}')
        return out
    for name, entry in raw.items():
        where = f'lenses[{name!r}]'
        if not _lens_name(name, 'lens name', problems):
            continue
        key = lens.lens_key(name)
        if key in by_key:
            problems.append(f'lens names {by_key[key]!r} and {name!r} are one lens to the gate (both read as '
                            f'{key!r}): rename one')
            continue
        by_key[key] = name
        if not _keys(entry, ('prompt',), where, problems) or 'prompt' not in entry:
            continue
        why = prompt_problem(entry['prompt'])
        if why:
            problems.append(f'{where}.prompt {_show(entry["prompt"])} {why}')
            continue
        out[name] = entry['prompt']
    return out


def _min_lenses(raw, problems):
    if not _keys(raw, WEIGHTS, 'min_lenses', problems):
        return None
    bad = [w for w in WEIGHTS if w in raw and not _int(raw[w], 0, MAX_LENSES)]
    problems.extend(f'min_lenses.{w} is {_show(raw[w])}: must be a whole number 0-{MAX_LENSES}' for w in bad)
    if bad or any(w not in raw for w in WEIGHTS) or any(k not in WEIGHTS for k in raw):
        return None
    if not raw['critical'] >= raw['normal'] >= raw['light']:
        problems.append('min_lenses must not fall with weight: critical >= normal >= light')
    return MinLenses(**raw)


def _rules(raw, declared, mins, problems):
    if not isinstance(raw, list) or raw == []:
        problems.append(f'rules is {_show(raw)}: must be a non-empty list of rules')
        return ()
    rules, names, catch_all = [], set(), None
    for i, r in enumerate(raw):
        where, before = f'rules[{i}]', len(problems)
        if not _keys(r, RULE_KEYS, where, problems):
            continue
        if 'name' in r:
            if not _text(r['name']):
                problems.append(f'{where}.name is {_show(r["name"])}: must be a non-empty line')
            elif r['name'] in names:
                problems.append(f'{where}: rule name {r["name"]!r} is written twice')
            else:
                names.add(r['name'])
                where = f'{where} ({r["name"]!r})'
        weight = r.get('weight')
        if 'weight' in r and weight not in WEIGHTS:
            problems.append(f'{where}.weight is {_show(weight)}: must be one of {", ".join(WEIGHTS)}')
            weight = None
        paths = r.get('paths')
        if 'paths' in r:
            if not isinstance(paths, list) or paths == []:
                problems.append(f'{where}.paths is {_show(paths)}: must be a non-empty list of globs')
                paths = None
            else:
                for g in paths:
                    why = glob_problem(g)
                    if why:
                        problems.append(f'{where}.paths: {_show(g)} {why}')
                if len(set(map(json.dumps, paths))) != len(paths):
                    problems.append(f'{where}.paths has a glob written twice')
                if catch_all is not None:
                    problems.append(f'{where} comes after the catch-all rule {catch_all} and can never match: '
                                    f'move it above (or drop it, if it matches every path too)')
                elif any(isinstance(g, str) and not glob_problem(g) and matches_every_path(g) for g in paths):
                    catch_all = where
                    if weight == 'light':
                        problems.append(f'{where} is the catch-all rule (a glob that matches every path, such as '
                                        f'**) and must not be light: a new folder must never need no lens')
        names_ = r.get('lenses')
        if 'lenses' in r:
            if not isinstance(names_, list):
                problems.append(f'{where}.lenses is {_show(names_)}: must be a list of lens names')
                names_ = None
            else:
                good = [n for n in names_ if _lens_name(n, f'{where}.lenses:', problems)]
                for n in good:
                    if n not in declared:
                        problems.append(f'{where}.lenses: {n!r} has no entry in lenses')
                if len(set(good)) != len(good):
                    problems.append(f'{where}.lenses has a name written twice')
                if names_ == [] and weight not in (None, 'light'):
                    problems.append(f'{where} is {weight} with no lenses: only a light rule may have none')
                elif weight and mins:
                    keys = {lens.lens_key(n) for n in good}
                    need = getattr(mins, weight)
                    if len(keys) < need:
                        problems.append(f'{where}: {weight} needs {need} different lens{"es" if need != 1 else ""}, '
                                        f'found {len(keys)} (names that differ only in case, blanks or a round '
                                        f'marker such as r2 are one lens)')
        if len(problems) == before:
            rules.append(Rule(r['name'], weight, tuple(paths), tuple(names_)))
    if catch_all is None:
        problems.append(f'no rule has the catch-all path {CATCH_ALL!r} (or another glob that matches every path): a '
                        f'new folder must never fall to no rule')
    if not any(isinstance(r, dict) and r.get('weight') == 'critical' for r in raw):
        problems.append('no critical rule: the built-in protected paths (the config, every lens prompt, '
                        '.github/workflows/**) take the lenses of the first critical rule')
    return tuple(rules)


def _rounds(raw, declared, problems):
    if not _keys(raw, ROUND_KEYS, 'rounds', problems):
        return None
    ok = True
    for k in ('approach_at', 'max'):
        if k in raw and not _int(raw[k], 1, MAX_ROUNDS):
            problems.append(f'rounds.{k} is {_show(raw[k])}: must be a whole number 1-{MAX_ROUNDS}')
            ok = False
    if ok and 'approach_at' in raw and 'max' in raw and raw['max'] < raw['approach_at']:
        problems.append(f'rounds.max {raw["max"]} is below rounds.approach_at {raw["approach_at"]}: the approach '
                        f'lens would never be asked for')
        ok = False
    if 'approach_lens' in raw:
        name = raw['approach_lens']
        if not _lens_name(name, 'rounds.approach_lens', problems):
            ok = False
        elif name not in declared:
            problems.append(f'rounds.approach_lens {name!r} has no entry in lenses')
            ok = False
    if not ok or any(k not in raw for k in ROUND_KEYS):
        return None
    return Rounds(raw['approach_at'], raw['max'], raw['approach_lens'])


def parse_review(obj, where='review'):
    """ReviewRules from the parsed `review` value, or ConfigError naming every problem at once."""
    problems = []
    if not isinstance(obj, dict):
        raise ConfigError(f'{where} is {_show(obj)}: must be an object (README.md, "Review rules")')
    if 'version' in obj and not (_int(obj['version'], VERSION, VERSION)):
        # Another version may have other keys: naming them all as unknown would only hide the one real problem.
        newer = _int(obj['version'], VERSION + 1, 2 ** 31)
        raise ConfigError(f'{where}: version {_show(obj["version"])} is not {VERSION}, the only version this release '
                          f'reads' + (': update the pinned release first' if newer else ''))
    problems.extend(f'unknown key {k!r} (known: {", ".join(KEYS)})' for k in obj if k not in KEYS)
    problems.extend(f'{k} is missing' for k in KEYS if k not in obj)
    if 'mode' in obj and obj['mode'] not in MODES:
        problems.append(f'mode is {_show(obj["mode"])}: must be one of {", ".join(MODES)}')
    for k in ('findings', 'state_line'):
        if k in obj and not isinstance(obj[k], bool):
            problems.append(f'{k} is {_show(obj[k])}: must be true or false')
    lenses = _lenses(obj['lenses'], problems) if 'lenses' in obj else {}
    # Every name written in `lenses`, even one whose entry is wrong: that entry's own problem is named once, not again
    # as "no entry" by each rule that lists it.
    declared = set(obj['lenses']) if isinstance(obj.get('lenses'), dict) else set()
    mins = _min_lenses(obj['min_lenses'], problems) if 'min_lenses' in obj else None
    rules = _rules(obj['rules'], declared, mins, problems) if 'rules' in obj else ()
    rounds = _rounds(obj['rounds'], declared, problems) if 'rounds' in obj else None
    # Rule order: refused in enforce (the rules decide), a warning in shadow (they only say what they would have said).
    order = _shadowed(rules)
    warnings = tuple(f'{w}; {SWITCH}' for w in order) if obj.get('mode') == 'shadow' else ()
    if obj.get('mode') != 'shadow':
        problems.extend(order)
    if problems:
        raise ConfigError(f'{where}: ' + '; '.join(problems))
    return ReviewRules(obj['version'], obj['mode'], rules, tuple(Lens(n, p) for n, p in lenses.items()), mins,
                       rounds, obj['findings'], obj['state_line'], warnings, tuple(order))
