"""Which lens reads a change needs by the `review` block's path rules (README.md, "Review rules", How the gate judges).

Pure: the changed paths come in (parse_name_status reads `git diff --name-status -z -M`; head_check.changed_paths runs
git), the judgement goes out. Nothing here reads GitHub, a file or the clock, so the same input gives the same answer
every time, and the TypeScript side can be tested on the same vectors (review.vectors.json).

  - Every changed path belongs to the first rule with a glob that matches it. A rename or copy counts under its old
    AND its new path; a delete, a mode change and a type change count under their path.
  - Built-in protected paths, not configurable: the config file itself, every lens prompt file named in `lenses`, and
    `.github/workflows/**`. Such a path also counts for the first critical rule of the list, whatever rule it
    belongs to.
  - The heaviest weight hit decides. The required lenses are the lenses of every rule of that weight that was hit, in
    the order of the rules and of each rule's list, each name once.
  - No changed path at all (an empty diff) counts as the catch-all rule: the change is judged as a new folder would
    be, never as "no lens needed".
Unknown never judges: a path no rule matches, a protected path with no critical rule (the parser refuses both, so
only a hand-built rule set gets here) or a diff line this module cannot read raises JudgeError, which the gate turns
into a failure.

judge_rounds judges the review itself, from the lens comments (README.md, "Review rules", Findings and rounds):
  - Findings (`findings: true`): a lens whose newest report that counts as a read has a `blocker:` or `major:` line
    fails the head, until that lens reads a fixed head in a new comment without one (a later round clears it) or a
    person's override comment newer than the report covers it. Owner decision: issue #99.
  - Rounds: each different full sha the lens headers name is one fix-and-review round, and a header's round marker
    (`r3`) counts as that round too, whichever is higher. From round `rounds.approach_at` the approach lens is
    required; a round past `rounds.max` fails the head until a person's override covers every lens: no more rounds,
    the person decides (override or close). Owner decision: issue #100.
Pure as judge is: the lens comments come in (lens.Review.lens_scan), the verdict goes out.
"""
import re
from dataclasses import dataclass

import lens
import review_config
import review_glob

WORKFLOWS = '.github/workflows/**'
# Every status `git diff --name-status` prints for two trees: Added, Copied, Deleted, Modified, Renamed, Type changed.
# U (unmerged), X (unknown) and B (broken pair, -B only) never come from a tree-to-tree diff without -B: unknown, so
# refused.
ONE_PATH, TWO_PATHS = set('ADMT'), set('CR')
SHOW_PATHS = 5   # paths named per rule in the summary; the count says how many there are


class JudgeError(Exception):
    """The judge cannot say which lenses the change needs: the gate fails the head."""


@dataclass(frozen=True)
class Hit:
    rule: str
    weight: str
    paths: tuple   # the changed paths that counted for this rule, in diff order


@dataclass(frozen=True)
class Judgement:
    weight: str
    lenses: tuple      # the required lens names, in rule order, each once
    hits: tuple        # of Hit, in rule order: every rule at least one path counted for
    protected: tuple   # the changed paths that are built-in protected
    paths: int         # how many different changed paths were judged

    def text(self):
        """One line for the check summary: weight, lenses, then why."""
        lenses = ', '.join(self.lenses) or 'none'
        why = '; '.join(f'{h.rule} ({h.weight}): {_some(h.paths)}' for h in self.hits)
        protected = f'; protected: {_some(self.protected)}' if self.protected else ''
        return f'{self.weight}, lenses {lenses} ({self.paths} changed path{"s" if self.paths != 1 else ""}; {why}' \
               f'{protected})'


def shown(path):
    """A changed path as text that can be written anywhere: a file name that is not UTF-8 reaches the judge with its
    bytes kept as lone surrogates (head_check.changed_paths decodes git with surrogateescape), and those are written as
    \\xNN here. Matching uses the path itself, never this."""
    try:
        return path.encode('utf-8', 'surrogateescape').decode('utf-8', 'backslashreplace')
    except UnicodeEncodeError:
        return path.encode('utf-8', 'backslashreplace').decode('utf-8')


def _some(paths):
    if not paths:
        return 'no changed path'
    shown_ = ', '.join(shown(p) for p in paths[:SHOW_PATHS])
    return shown_ + (f' and {len(paths) - SHOW_PATHS} more' if len(paths) > SHOW_PATHS else '')


def parse_name_status(text):
    """[(status letter, (path, ...))] from `git diff --name-status -z` output (NUL-separated: no quoting, any path).
    JudgeError for anything else: a status this module does not know, a missing path, an empty path."""
    if text == '':
        return []
    if not text.endswith('\0'):
        raise JudgeError('git diff --name-status -z output does not end with NUL: cut short')
    fields, out, i = text[:-1].split('\0'), [], 0
    while i < len(fields):
        status = fields[i]
        letter = status[:1]
        if letter in TWO_PATHS and status[1:].isdigit() and len(status) <= 4:
            n = 2
        elif letter in ONE_PATH and len(status) == 1:
            n = 1
        else:
            raise JudgeError(f'git diff printed the status {status[:20]!r}, which the judge does not know')
        paths = tuple(fields[i + 1:i + 1 + n])
        if len(paths) != n or any(p == '' for p in paths):
            raise JudgeError(f'git diff printed the status {status!r} without its path{"s" if n > 1 else ""}')
        out.append((letter, paths))
        i += 1 + n
    return out


def protected_paths(review, config_path):
    """The exact paths that always count as critical: the config file and every lens prompt file. The config path is
    written as git names a changed file (one leading ./ aside): git also reads `././x` or `a//b`, but no changed path is
    ever spelled so, and the config would not be protected: JudgeError. Glob characters are part of the name
    (`queue[main].json`): the set is compared exactly, never matched."""
    rel = config_path[2:] if config_path.startswith('./') else config_path
    why = review_config.exact_path_problem(rel)
    if why:
        raise JudgeError(f'the config path {config_path!r} {why}: write it as git names the file (such as '
                         '.github/pr-queue.json), or a change to it would not count as protected')
    return {rel, *(x.prompt for x in review.lenses)}


def is_protected(path, exact):
    return path in exact or review_glob.matches(WORKFLOWS, path)


def catch_all(review):
    return next((r for r in review.rules if any(review_config.matches_every_path(g) for g in r.paths)), None)


def judge(review, changes, config_path):
    """The Judgement of `changes` ([(status, paths)], parse_name_status) under the parsed `review` rules; the config
    file is `config_path` (repo-relative). JudgeError when it cannot say."""
    paths = list(dict.fromkeys(p for _, ps in changes for p in ps))   # each once, in diff order
    exact = protected_paths(review, config_path)
    first_critical = next((r for r in review.rules if r.weight == 'critical'), None)
    counted = {r.name: [] for r in review.rules}
    protected = []
    if not paths:
        rule = catch_all(review)
        if rule is None:
            raise JudgeError('no changed path, and no catch-all rule to judge an empty change by')
        counted[rule.name] = None   # hit, with no path
    for p in paths:
        rule = next((r for r in review.rules if review_glob.matches_any(r.paths, p)), None)
        if rule is None:
            raise JudgeError(f'no rule matches the changed path {p!r} (a rule set without a catch-all rule)')
        counted[rule.name].append(p)
        if is_protected(p, exact):
            if first_critical is None:
                raise JudgeError(f'{p!r} is a protected path, but the rules have no critical rule to take its lenses '
                                 'from')
            protected.append(p)
            if first_critical is not rule:
                counted[first_critical.name].append(p)
    hits = tuple(Hit(r.name, r.weight, tuple(counted[r.name] or ())) for r in review.rules
                 if counted[r.name] is None or counted[r.name])
    weight = next(w for w in review_config.WEIGHTS if any(h.weight == w for h in hits))
    lenses = tuple(dict.fromkeys(n for r in review.rules if r.weight == weight
                                 and any(h.rule == r.name for h in hits) for n in r.lenses))
    return Judgement(weight, lenses, hits, tuple(protected), len(paths))


# A finding line, as a lens report writes it (README.md, "Findings and rounds"): `blocker:` or `major:` at the start
# of a line, after the Markdown lens.norm_line strips (quote and list markers, emphasis, headings), any case, a plural,
# a number and a short note in brackets allowed (`Majors:`, `major 2:`, `major (state):`). Every line counts, in code
# blocks and quotes too: fail closed, as for the lens header. Only an explicit nothing after the colon (NO_FINDING) is
# no finding; any other text is one.
FINDING_RE = re.compile(r'(blocker|major)s?(?:\s*#?\d{1,4})?(?:\s*\([^()]{0,80}\))?\s*:(.*)', re.I)
NO_FINDING_RE = re.compile(r'(?:none|nothing|no|0|n/?a|-+|\u2014|\u2013)\s*\.?', re.I)
ROUND_RE = re.compile(r'r(\d{1,9})')
SHOW_FINDING = 120   # characters of a finding line named in the reason


def findings(body):
    """[(kind, line)] of the report's blocker and major lines, in order: kind is 'blocker' or 'major', line the line as
    normalised (cut to SHOW_FINDING). Read both ways lens reads a line (clean_inline narrow or not): a line either way
    reads as a finding is one."""
    out = []
    for raw in (body or '').splitlines():
        for narrow in (False, True):
            m = FINDING_RE.match(lens.norm_line(raw, narrow))
            if m and not NO_FINDING_RE.fullmatch(m.group(2).strip()):
                out.append((m.group(1).lower(), lens.norm_line(raw, narrow)[:SHOW_FINDING]))
                break
    return out


def round_marker(name):
    """The highest round marker (`r3`) among the words of a lens name after its first, or 0. Emphasis around a word
    does not count (`data **r3**`)."""
    words = [w.strip('*_`') for w in name.lower().split()[1:]]
    return max((int(m.group(1)) for w in words for m in [ROUND_RE.fullmatch(w)] if m), default=0)


def review_round(history):
    """The fix-and-review round the lens headers say: how many different full 40-hex shas they read, or the highest
    round marker, whichever is higher. history: [(lens name, sha or None)] of every lens header that counts
    (lens.Review.lens_scan), old, edited and hidden ones too. 0: no lens header yet."""
    shas = {sha for _, sha in history if sha and lens.FULL_SHA.fullmatch(sha)}
    return max([len(shas)] + [round_marker(name) for name, _ in history])


@dataclass(frozen=True)
class Rounds:
    round: int          # review_round of the lens headers
    approach: bool      # the approach lens is required (round >= rounds.approach_at)
    over_cap: bool      # round > rounds.max and no override covers every lens
    findings: tuple     # (lens key, kind, line) of every blocker or major no later round or override cleared
    problems: tuple     # why the head fails, plain text; empty: findings and rounds pass

    def text(self, review):
        """One line for the check summary."""
        cap = review.rounds.max
        said = f'round {self.round} of {cap}'
        if self.approach:
            said += f', approach lens {review.rounds.approach_lens} required from round {review.rounds.approach_at}'
        if not review.findings:
            said += '; findings are not judged (findings: false)'
        return said + ('; ' + '; '.join(self.problems) if self.problems else '; no blocker or major open')


def judge_rounds(review, history, newest, overridden):
    """Rounds of a pull request's lens comments under the parsed `review` rules. history: [(lens name, sha)] of every
    lens header that counts (lens.Review.lens_scan); newest: {lens key: body} of every lens (lens.Review.lens_reads),
    the body of its newest report when that report counts as a read, else None (it reads nothing: the head fails for
    it already, so it has no findings here); overridden: the lens keys a newer override covers (lens_reads)."""
    n, cap = review_round(history), review.rounds.max
    every = bool(newest) and set(newest) <= set(overridden)   # a person's override covers every lens
    over = n > cap and not every
    decides = 'a person decides: an override comment for the head, or close the pull request'
    problems = []
    if over:
        problems.append(f'review round {n} is past the cap of {cap} rounds (issue #100): no more rounds; {decides}')
    found = []
    if review.findings:
        for key in sorted(newest):
            if key in overridden or newest[key] is None:
                continue
            for kind, line in findings(newest[key]):
                found.append((key, kind, line))
        for key, kind, line in found:
            way = (f'this is round {n} of {cap}: no more rounds; {decides}' if n >= cap else
                   'fix it and have that lens read the fixed head in a new comment with no blocker or major line, or a '
                   'person posts an override comment for the head')
            problems.append(f'lens {key} reported a {kind} that no later round cleared (issue #99): {line!r}; {way}')
    return Rounds(n, n >= review.rounds.approach_at, over, tuple(found), tuple(problems))
