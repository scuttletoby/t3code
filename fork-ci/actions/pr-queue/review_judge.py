"""Which lens reads a change needs by the `review` block's path rules (README.md, "Review rules", How the gate judges).

Pure: the changed paths come in (parse_name_status reads `git diff --name-status -z -M`; head_check.changed_paths runs
git), the judgement goes out. Nothing here reads GitHub, a file or the clock, so the same input gives the same answer
every time, and the TypeScript side can be tested on the same vectors (review.vectors.json).

  - Every changed path belongs to the first rule with a glob that matches it. A rename or copy counts under its old
    AND its new path; a delete, a mode change and a type change count under their path.
  - Built-in protected paths, not configurable: the config file itself, every lens prompt file named in `lenses`, and
    `.github/workflows/**`. Such a path also counts for the first critical rule of the list, whatever rule it
    belongs to.
  - The heaviest weight hit is the change's weight. The required lenses are the lenses of every rule that was hit,
    whatever its weight, in the order of the rules and of each rule's list, each name once: each rule's paths get
    their own rule's read, so an app change plus one gate file needs the code lens and the gate lenses (owner
    decision: issue #82, "one extra review"), never the gate lenses alone.
  - No changed path at all (an empty diff) counts as the catch-all rule: the change is judged as a new folder would
    be, never as "no lens needed".
Unknown never judges: a path no rule matches, a protected path with no critical rule (the parser refuses both, so
only a hand-built rule set gets here) or a diff line this module cannot read raises JudgeError, which the gate turns
into a failure.

judge_rounds judges the review itself, from the lens comments (README.md, "Review rules", Findings and rounds):
  - Findings (`findings: true`): a lens whose newest entry has a blocker or major finding (read_findings: labels,
    headings, bold, tables, folded Unicode; never inside a fenced code block) fails the head, until that lens posts a
    new report on a fixed head without one (a later round clears it) or a person's override newer than the report
    covers it. A person is a login listed in `override_authors`; none listed (the default) means no override clears
    anything. A report with no findings section (a blocker label and a major label, `none` allowed) cannot be judged
    and fails that lens.
    Owner decision: issue #99.
  - Rounds: a round is a lens read of a different change, not of a different sha: review_round counts the different
    own changes (head_check.own_change: the diff from the merge base, so a head that only merges the base in is the
    same round) the lens headers name, old, edited and hidden ones too, or the highest round marker (`r3`), whichever
    is higher. From round `rounds.approach_at` the approach lens is required; a round past `rounds.max` fails the head
    until a person's override for the head, newer than every lens comment, lets it through (it alone is enough: it
    also waives the approach lens). Owner decision: issue #100.
Pure as judge is: the lens comments come in (lens.Review.lens_scan), the verdict goes out.

has_state_line says what counts as a report's `state:` line (the answer to the state question). The `state_line`
setting is parsed but not enforced yet; the review kit (fork-kit/review, `check`) imports this rule, so the gate and
the kit read one rule when the gate starts enforcing it.
"""
import html
import re
import unicodedata
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
    lenses = tuple(dict.fromkeys(n for r in review.rules if any(h.rule == r.name for h in hits) for n in r.lenses))
    return Judgement(weight, lenses, hits, tuple(protected), len(paths))


# --- findings (issue #99) -----------------------------------------------------------------------------------------
# What a lens report writes, as the fork's reports are written (README.md, "Findings and rounds"; the table in
# review_judge.test.py, Findings): a label `blocker`, `blocking`, `major` (also plural, `Major findings`, `major 2`,
# `major (state)`), then
#   - a separator and the finding on the same line: `major: x`, `**Blocking:** x`, `major — x`, `major - x`, `[major] x`,
#     `🔴 Major: x`; or a table row `| major | x |` (or a table whose header row names the label: its column);
#   - nothing on the line (`### Major`, `**Blocking**`, `Major:`): the first line under it is the finding.
# Text that says nothing (NONE_RE: `none`, `None found.`, `none (only minors)`, `n/a`, `-` ...) is no finding, and every
# label of a line is judged (`Blocker: none, major: none`). `minor` and `nit` labels never block; they only end the
# text of the label before them. Lines inside a fenced code block hold no label (a report quotes code and test vectors
# there); the line under a bare label is its content even when it opens a fence. A report has a findings section only
# when it has a blocker label AND a major label (`blocker: none` and `major: none` too), or `findings: none`, or
# `finding: <kind> ...` lines: with one of the two labels only, a major written in a form not read here
# (`F1 (major): x`) would pass as clean, so such a report cannot be judged (judge_rounds fails it). Each finding is one
# line `finding: <blocker|major|minor> ...` in that format (FINDING_LINE_RE). A recap of an earlier round with a label
# (`Major 1: fixed`) is read as a finding like any other; write it without one (`round 1, fixed: x`).
# Before any of this the text is folded (fold): NFKC (`Ｍajor` -> Major), format characters removed (zero-width space and
# joiner, soft hyphen, bidi marks: category Cf), and Cyrillic and Greek letters that look like Latin ones read as Latin
# (`majоr` with a Cyrillic о); a 0 for an o in a label is read too (`maj0r`); HTML entities decoded (`ma&#106;or`) and
# Markdown backslash escapes dropped (`major\: x`), as GitHub renders them. Each line is then read in NFKC again
# (lens.norm_line), after the entities are decoded, so what an entity writes reads as GitHub shows it (issue #1005, the
# shared vectors marked #1005): `blocker&#65306; x` (a full-width colon) blocks, `&#65358;&#65359;&#65358;&#65349;` (a
# full-width none) reads as none, and `none` with a zero-width space and then an accent is `noné`, no none, so it blocks.
CONFUSABLES = str.maketrans({
    'А': 'A', 'В': 'B', 'Е': 'E', 'К': 'K', 'М': 'M', 'Н': 'H', 'О': 'O', 'Р': 'P', 'С': 'C', 'Т': 'T', 'Х': 'X',
    'І': 'I', 'Ј': 'J', 'Ѕ': 'S', 'а': 'a', 'е': 'e', 'о': 'o', 'р': 'p', 'с': 'c', 'х': 'x', 'у': 'y', 'і': 'i',
    'ј': 'j', 'ѕ': 's', 'к': 'k', 'ԁ': 'd', 'ӏ': 'l', 'һ': 'h', 'ԛ': 'q', 'ԝ': 'w',
    'Α': 'A', 'Β': 'B', 'Ε': 'E', 'Ζ': 'Z', 'Η': 'H', 'Ι': 'I', 'Κ': 'K', 'Μ': 'M', 'Ν': 'N', 'Ο': 'O', 'Ρ': 'P',
    'Τ': 'T', 'Υ': 'Y', 'Χ': 'X', 'ο': 'o', 'ι': 'i', 'κ': 'k', 'ν': 'v', 'α': 'a', 'ρ': 'p',
    'ı': 'i', 'ℓ': 'l', 'ǀ': 'l', '∶': ':',
})
_KW = r'(?P<kw>bl[o0]ck(?:ers?|ing)|ma[j][o0]rs?|m[i1]n[o0]rs?|nits?)'
_LABEL = (_KW + r'(?![^\W_])(?:\s+(?:findings?|issues?|problems?|items?|points?|bugs?|ones?)(?![^\W_]))?'
          r'(?:\s*#?\d{1,4}(?![^\W_]))?(?:\s*\([^()\n]{0,80}\))?')
_SEP = r'\s*(?::|[—–=]|-(?=\s|$))'
# A label that starts a part of a line, with a separator after it (`Major version: 2` is no label) or in brackets
# (`[major] x`): at the line start, or after punctuation, `and` or `but` (`blocker: none, major: x`).
LABEL_RE = re.compile(r'(?:^|(?<=[,;.!?|/])\s*|\s+(?:and|but)\s+)(?:\[' + _LABEL + r'\]|\(' + _LABEL.replace('kw', 'kw2')
                      + r'\)|' + _LABEL.replace('kw', 'kw3') + _SEP + ')', re.I)
BARE_RE = re.compile(r'[\[(]?' + _LABEL + r'[\])]?' + _SEP + r'?\s*', re.I)
CELL_RE = re.compile(_LABEL + r'\s*:?\s*', re.I)
NONE_RE = re.compile(
    r'[(\[]?\s*(?:none|nothing|nil|no|zero|0|n/?a|-+|—|–)'
    r'(?:\s+(?:found|open|left|remaining|seen|here|known|so\s+far|at\s+all|this\s+round|blocking|blockers?|majors?|'
    r'findings?|issues?|problems?))*\s*(?:$|[.,;:!?)(\[]|[^\w\s\x00-\x7f]|-(?=\s|$))', re.I)
FINDING_LINE_RE = re.compile(r'findings?\s*:\s*[\[(]?' + _KW + r'(?![^\W_])', re.I)   # `finding: major x`
FINDINGS_NONE_RE = re.compile(r'(?:findings|issues|problems)\s*:\s*(?:none|nothing|no|0|n/?a)\s*\.?\s*$', re.I)
NOT_SAFE_RE = re.compile(r'\bnot\s+safe\b', re.I)
NOT_SAFE_TO_MERGE_RE = re.compile(r'\bnot\s+safe\s+to\s+merge\b', re.I)   # read_findings(unmarked=True), any line
VERDICT_NOT_SAFE_RE = re.compile(r'verdict\s*[:—–-]?\s*not\s+safe\b', re.I)
RULE_LINE_RE = re.compile(r'\s*(?:[-*_]\s*){3,}')   # a thematic break: no content
HEADING_RE = re.compile(r' {0,3}#{1,6}(?:\s|$)')
TABLE_RE = re.compile(r'\s*\|')
TABLE_RULE_RE = re.compile(r'\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?')   # on a stripped line: linear time
ROUND_RE = re.compile(r'r(\d{1,9})')
SHOW_FINDING = 120   # characters of a finding line named in the reason
BLOCKING = ('blocker', 'major')


@lens.memo
def _fold(text, _=False):
    """fold, remembered (lens.memo)."""
    text = lens.nfkc(text)
    # html.unescape raises on a number reference past int's 4,300-digit limit: its value, or one past Unicode (U+FFFD)
    text = re.sub(r'&#0*([0-9]+)', lambda m: '&#' + (m[1] if len(m[1]) <= 7 else '1114112'), text)
    text = html.unescape(text)
    lens.unicode_check(text)   # `&#x1C89;` is read as U+1C89, so it drifts as the letter itself does
    text = re.sub(r'\\([!-/:-@\[-`{-~])', r'\1', text)
    cf = ''.join(re.escape(ch) for ch in set(text) if unicodedata.category(ch) == 'Cf')
    return (re.sub(f'[{cf}]', '', text) if cf else text).translate(CONFUSABLES)


def fold(text):
    """The text as the findings rules read it: NFKC, format characters (category Cf) gone, look-alike letters Latin.
    Refused on a Python whose Unicode is not the rules' (lens.unicode_check)."""
    lens.unicode_check(text or '')
    return _fold(text or '')


def _kind(kw):
    kw = kw.lower().replace('0', 'o').replace('1', 'i')
    return 'blocker' if kw.startswith('block') else 'major' if kw.startswith('major') else 'minor'


def _plain(line, narrow):
    """One line normalised (lens.norm_line), folded, with leading symbols (emoji, arrows, bullets) gone and each run of
    whitespace one space (the label patterns then take linear time on a long run of spaces)."""
    return re.sub(r'\s+', ' ', re.sub(r'^[^\w\[(]+', '', lens.norm_line(fold(line), narrow))).strip()


def _says_nothing(text):
    return bool(NONE_RE.match(re.sub(r'^[^\w\-—–(\[]+', '', text.strip())))


def _cells(raw):
    return [c.strip() for c in fold(raw).strip().strip('|').split('|')]


def _read(body, narrow):
    """(findings [(line index, kind, text)], the set of label kinds seen), one way of reading."""
    lines = (body or '').splitlines()
    out, seen, fence, columns = [], set(), None, None
    for i, raw in enumerate(lines):
        fence, fenced = lens.fence_step(fence, raw)
        if fenced:
            columns = None
            continue
        if TABLE_RE.match(raw):
            cells = [_plain(c, narrow) for c in _cells(raw)]
            if i + 1 < len(lines) and TABLE_RULE_RE.fullmatch(lines[i + 1].strip()):   # a header row: its label columns
                columns = {j: _kind(m.group('kw')) for j, c in enumerate(cells) for m in [CELL_RE.fullmatch(c)] if m}
                seen |= set(columns.values())
                continue
            if TABLE_RULE_RE.fullmatch(raw.strip()):
                continue
            row, after, said = _plain(raw, narrow), [], ''   # once per row: per cell, a wide row took minutes
            for c in reversed(cells):   # after[j]: the first non-empty cell after cell j
                after.append(said)
                said = c or said
            after.reverse()
            for j, c in enumerate(cells):   # a label cell: the next cell says what it found
                m = CELL_RE.fullmatch(c)
                if m and _kind(m.group('kw')) in BLOCKING:
                    seen.add(_kind(m.group('kw')))
                    if not _says_nothing(after[j]):
                        out.append((i, _kind(m.group('kw')), row))
            for j, kind in (columns or {}).items():
                if kind in BLOCKING and j < len(cells):
                    if not _says_nothing(cells[j]):
                        out.append((i, kind, row))
            continue
        columns = None
        line = _plain(raw, narrow)
        if FINDINGS_NONE_RE.match(line):
            seen |= set(BLOCKING)
            continue
        m = FINDING_LINE_RE.match(line)
        if m:   # `finding: major x`: that format's section, and a finding of its kind
            seen |= set(BLOCKING)
            if _kind(m.group('kw')) in BLOCKING:
                out.append((i, _kind(m.group('kw')), line))
            continue
        if BARE_RE.fullmatch(line):   # `### Major`: the first line under it says what it found
            kind = _kind(BARE_RE.fullmatch(line).group('kw'))
            if kind not in BLOCKING:
                continue
            seen.add(kind)
            under = _under(lines, i + 1, narrow)
            if under is None or not _says_nothing(under):
                out.append((i, kind, f'{line} (nothing under it)' if under is None else f'{line} {under}'))
            continue
        marks = list(LABEL_RE.finditer(line))
        for n, m in enumerate(marks):
            kind = _kind(m.group('kw') or m.group('kw2') or m.group('kw3'))
            if kind not in BLOCKING:
                continue
            seen.add(kind)
            said = line[m.end():marks[n + 1].start() if n + 1 < len(marks) else len(line)]
            if not _says_nothing(said):
                out.append((i, kind, line))
                break
    return out, seen


def _under(lines, start, narrow):
    """The first content line after a bare label, normalised, or None when the next one is a heading, another label or
    the end (nothing under it). Blank lines and thematic breaks are skipped; a fence line is content."""
    for raw in lines[start:]:
        if not raw.strip() or RULE_LINE_RE.fullmatch(raw):
            continue
        if HEADING_RE.match(raw):
            return None
        line = _plain(raw, narrow)
        if BARE_RE.fullmatch(line) or LABEL_RE.match(line):
            return None
        return line or raw.strip()
    return None


def read_findings(body, unmarked=False):
    """(findings, has_section). findings: [(kind, line)] of the report's blocker and major findings in line order, kind
    'blocker' or 'major', line as normalised (cut to SHOW_FINDING). Read both ways lens reads a line (clean_inline
    narrow or not): a line either way reads as a finding is one. has_section: the report has a blocker label and a major
    label (`blocker: none` and `major: none` too), or `findings: none`, or `finding: <kind>` lines. A first line (the
    lens header, or the title line under it) or a `verdict:` line that says NOT SAFE is a blocker whatever the labels
    say. unmarked: a comment without the lens marker (lens.Review.report_problem): its first two lines are read as a
    header and its title line are (a heading, then NOT SAFE under it), and `not safe to merge` on any line outside a
    fenced code block is a blocker too."""
    found = {}
    seen = set()
    for narrow in (False, True):
        out, saw = _read(body, narrow)
        seen |= saw
        for i, kind, text in out:
            found.setdefault(i, (kind, text[:SHOW_FINDING]))
    lines = (body or '').splitlines()
    top = [i for i, line in enumerate(lines) if line.strip()][:2]
    first = set(top if unmarked or lens.lens_header(body) else top[:1])   # the line after a header is a title too
    fence = None
    for i, raw in enumerate(lines):
        fence, fenced = lens.fence_step(fence, raw)
        if fenced or i in found:
            continue
        line = _plain(raw, False)
        if (i in first and NOT_SAFE_RE.search(line)) or VERDICT_NOT_SAFE_RE.match(line) \
                or (unmarked and NOT_SAFE_TO_MERGE_RE.search(line)):
            found[i] = ('blocker', f'the report says NOT SAFE: {line}'[:SHOW_FINDING])
    return [found[i] for i in sorted(found)], seen >= set(BLOCKING)


STATE_RE = re.compile(r'state\s*:\s*\S', re.I)


def has_state_line(body):
    """A `state:` line with an answer (anything but blanks after the colon), outside fenced and indented code blocks and
    quotes, read as a lens header is (folded, markdown and list markers stripped: `**State:** ok`, `- state: ok`)."""
    return any(STATE_RE.match(lens.norm_line(fold(raw))) for raw in lens.plain_lines(body))


def findings(body, unmarked=False):
    """[(kind, line)] of the report's blocker and major findings (read_findings)."""
    return read_findings(body, unmarked)[0]


def round_marker(name):
    """The highest round marker (`r3`) among the words of a lens name after its first, or 0. Markdown emphasis or
    backticks around the marker are stripped first (`data **r3**` is round 3), and the name is folded (`r３`)."""
    words = [w.strip('*_`') for w in fold(name).lower().split()[1:]]
    return max((int(m.group(1)) for w in words for m in [ROUND_RE.fullmatch(w)] if m), default=0)


def review_round(history, change_of=None):
    """The fix-and-review round the lens headers say: how many different changes they read, or the highest round
    marker, whichever is higher. history: [(lens name, sha or None)] of every lens header that counts
    (lens.Review.lens_scan), old, edited and hidden ones too, a short sha too. change_of(sha): what the pull request
    changes at that commit (head_check.own_change: the diff from its merge base with the base, so a commit that only
    merges the base in, or another lens's read of the same change, is the same round); without it each different sha
    is one. 0: no lens header yet."""
    change_of = change_of or (lambda sha: sha)
    changes = {change_of(sha.lower()) for _, sha in history if sha}
    return max([len(changes)] + [round_marker(name) for name, _ in history])


# How judge_rounds reads each lens's newest entry (head_check.review_rounds says which one it is):
REPORT = 'report'       # a read that counts: its findings, and it needs a findings section
LOOSE = 'loose'         # a comment that looks like a report but reads nothing, covered by an override that is no
#                         person's: its finding lines block, it needs no section (it may be a fix-round note)
UNREAD = 'unread'       # a lens report that reads nothing (edited, hidden, a short sha), covered by such an override:
#                         its findings cannot be read, so it blocks
CLEARED = 'cleared'     # a person's override is newer: nothing of it is judged
NOT_JUDGED = 'skip'     # it reads nothing and nothing covers it: the head fails for it already


@dataclass(frozen=True)
class Rounds:
    round: int          # review_round of the lens headers
    approach: bool      # the approach lens is required (round >= rounds.approach_at and no person decided)
    over_cap: bool      # round > rounds.max and no person decided
    findings: tuple     # (lens key, kind, line) of every blocker or major no later round or person cleared
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


def person_way(review, prefix='override for head'):
    """How a person lets a head through, as the reasons say it: who may, and what they write."""
    if not review.override_authors:
        return ('a person\'s override comment, but no one is listed in review.override_authors yet, so no override '
                'is accepted (issue #78: until agents no longer post as the person\'s login, an override is not proof '
                'of a person)')
    return (f'an override comment `{prefix} <head sha>: <why>` for this head by '
            f'{" or ".join(review.override_authors)}, newer than every lens comment')


def judge_rounds(review, history, reports, decided=False, change_of=None, prefix='override for head'):
    """Rounds of a pull request's lens comments under the parsed `review` rules. history: [(lens name, sha)] of every
    lens header that counts (lens.Review.lens_scan); reports: {lens key: (how, body)} of each lens's newest entry, how
    one of REPORT, LOOSE, UNREAD, CLEARED, NOT_JUDGED (head_check.review_rounds); decided: a person's override (one of
    review.override_authors) for this head is newer than every lens comment; change_of: review_round's. prefix: the
    config's override prefix, for the reasons."""
    n, cap = review_round(history, change_of), review.rounds.max
    over = n > cap and not decided
    way = person_way(review, prefix)
    problems = []
    if over:
        problems.append(f'review round {n} is past the cap of {cap} rounds (issue #100): no more rounds; a person '
                        f'decides: close the pull request, or {way}. A person\'s override alone is enough, also for a '
                        f'required approach lens that never posted')
    found = []
    if review.findings:
        for key in sorted(reports):
            how, body = reports[key]
            if how in (CLEARED, NOT_JUDGED):
                continue
            if how == UNREAD:
                problems.append(f'lens {key}: its newest report reads nothing and only an override that is no '
                                f'person\'s covers it, so its findings cannot be read (issue #99): that lens posts a '
                                f'new report, or {way}')
                continue
            said, section = read_findings(body)
            for kind, line in said:
                found.append((key, kind, line))
            if how == REPORT and not said and not section:
                problems.append(f'lens {key}: its newest report has no findings section, so it cannot be judged '
                                f'(issue #99): write `blocker: none` and `major: none`, or the findings, as lines of '
                                f'the report body (findings in inline review comments are not read)')
        for key, kind, line in found:
            again = (f'this is round {n} of {cap}: no more rounds; a person decides: close the pull request, or {way}'
                     if n >= cap else
                     f'fix it, and lens {key} posts a new report on the fixed head with no blocker or major '
                     f'(`major: none`); or {way}')
            problems.append(f'lens {key} reported a {kind} that no later round cleared (issue #99): {line!r}; {again}')
    approach = n >= review.rounds.approach_at and not decided
    return Rounds(n, approach, over, tuple(found), tuple(problems))
