"""Which head a pull request's review vouched for: the lens-comment rules of the merge queue (pr_queue.py).

A lens is one review angle, posted as one PR comment whose FIRST line is its header:

    lens: <name> · reviewer: <who> · read at: <sha>

also `Lens:`, `**Lens:**`, `## lens:`, `- lens:`. This grammar is fixed (README.md, "Lens comments"): it is the one
format the queue reads, and a project that writes its lens reports differently must change its reports, not this
file. What differs per project comes from the config (config.py): whose comments count at all (author associations;
which logins are bots), and the override comment (`<prefix> <40-hex sha>: ...`, posted by an account with one author
association and never edited) that vouches for a head when the lenses read older commits.

Only what GitHub records decides who wrote a comment: one by an account without a configured lens association (anyone
can comment on a public repository), by a bot, or hidden (minimized) is ignored entirely, neither a read nor a lens
that read nothing. Among the rest, fail closed throughout: a comment that LOOKS like a lens report but is not exactly
one valid read, or a lens comment edited after posting, counts as a lens that read nothing, so the queue never takes a
reviewed PR for an unreviewed one. A read is a full 40-hex sha: a shorter one could be matched by a commit pushed later.
"""
import re

# One normaliser (norm_line) reads every line alike, plain or in a fence, a quote or an indented block: quote, list and
# heading markers, emphasis, backticks, pipes, links and HTML tags go first, so no Markdown shape hides a header or a
# `read at`. A header line (header_line) is a normalised line starting `lens:` with a hex `read at` in it. A first line
# that is indented 4+ spaces, quoted or a fence is never the header (lens_header), but its header line still counts as
# one (lens_problem: fail closed).
LENS_RE = re.compile(r'lens\s*:\s*(.*)', re.I)
# The name ends at the first field: ` · reviewer:` or ` · read at:` (also `read at <hex>`; the separator may also be
# `,` `—` or none). A bare `reviewer` or `read at` stays in the name (`data-integrity reviewer 1` and `... 2` are two
# lenses).
FIELD_RE = re.compile(r'\s*(?:[·,—]\s*)?\b(?:reviewer\s*:|read[\s-]*at\b\s*(?::|[0-9a-f]{7}))', re.I)
READ_AT_RE = re.compile(r'\bread[\s-]*at\b\s*:?\s*([0-9a-f]{7,40})\b', re.I)
FULL_SHA = re.compile(r'[0-9a-f]{40}')
NO_HEADER = 'comment without a first-line lens header'
EDITED = 'edited after posting: post a new comment instead'
EMBEDDED = 'holds a lens header line'   # lens_problem: `not a lens:` cannot clear it
# What a lens report looks like (ONE rule on normalised lines): on a plain line (outside code blocks and quotes) a
# `lens:`, a `read at:` or `read at <hex>`, `safe to merge`, or in its first TOP_LINES plain lines a Markdown heading
# naming a lens (LOOKS_HEADING_RE reads the raw line: normalising drops `#`), or two `covered:`/`checks:` field lines;
# on ANY line, fenced, indented or quoted too, a `read at` with a hex sha, a header line or a `lens:` field with any hex
# sha (loose_header). Such a comment without a valid first-line header is a lens that read nothing. So is a comment
# with two or more header lines (one comment per lens). A round, fix or note comment says so on its first line,
# `not a lens: ...`; that clears it only when none of its lines is a header line or a `lens:` field with a sha.
LOOKS_RE = re.compile(r'\blens\s*:|\bread[\s-]*at\b\s*(?::|[0-9a-f]{7})|\bsafe\s+to\s+merge\b', re.I)
NOT_A_LENS_RE = re.compile(r'not\s+a\s+lens\s*:', re.I)
FIELD_LINE_RE = re.compile(r'(?:covered|checks)\s*:', re.I)
LOOKS_HEADING_RE = re.compile(r' {0,3}#{1,6}[ \t].*\blens\b', re.I)
TOP_LINES = 5
# Any hex sha with a digit (7-40 hex, not part of a longer word): `abc1234`, never `defaced` or `facade1x`.
HEX_RE = re.compile(r'(?<![0-9a-z])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}(?![0-9a-z])', re.I)
OK_CONCLUSIONS = ('SUCCESS', 'SKIPPED', 'NEUTRAL')


def clean_inline(text):
    """Markdown that can hide a word, gone: HTML tags (`<b>`, also a `<name>` placeholder), links `[x](y)` -> x, `*` and
    backticks, `|` -> space, `_` at a word edge (`_lens_` -> lens; `pr_queue` stays)."""
    text = re.sub(r'</?[A-Za-z][^>]*>', '', text)
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'[*`]', '', text).replace('|', ' ')
    return re.sub(r'(?<![^\W_])_+|_+(?![^\W_])', '', text)


def norm_line(line):
    """One line as lens_problem and lens_header read it: clean_inline, then any leading quote markers, list markers (`-`
    `+` `•` `1.` `1)`, a task box `[x]`; `*` is gone already), heading marks and `~~~` fences stripped."""
    s, prev = clean_inline(line), None
    while s != prev:
        prev = s
        s = re.sub(r'^\s*(?:>|[-+•](?=\s|$)|\d{1,9}[.)](?=\s|$)|#{1,6}(?=\s|$)|~{3,}|\[[ xX]\](?=\s|$))\s*', '', s)
    return s.strip()


def opens_plain(line):
    """The line is no code block, quote or fence (at most 3 leading spaces): only such a first line is a header."""
    return not re.match(r' {4}|\t|[ ]{0,3}(>|`{3}|~{3})', line)


def lens_key(name):
    """One key per lens across rounds: the whole name, only case and whitespace normalised, minus a trailing round
    marker (`r2`, `re-check`, `lens`) and Markdown emphasis around it. Everything else is the lens's identity:
    'data-integrity (DI-1)' and 'data-integrity (DI-2)' are different lenses; 'data-integrity DI-1 r2' =
    'Data-integrity DI-1'."""
    n, prev = name.lower(), None
    while n != prev:
        prev = n
        n = ' '.join(n.split()).strip('*_`')
        n = re.sub(r'\s+(r\d+|lens|re-?check)$', '', n)
    return n or 'unnamed'


def names_a_lens(name):
    """A header's name names a lens: it has a letter or digit, is no `<...>` template placeholder and is more than a
    round marker (`r4`, `re-check`). Otherwise two such headers would share one key and the newer read would stand for
    both."""
    key = lens_key(name)
    return bool(re.search(r'[^\W_]', key)) and not re.search(r'<[^<>]*>', name) \
        and not re.fullmatch(r'unnamed|r\d+|lens|re-?check', key)


def lens_header(body):
    """(name, sha or None) of a comment's lens header, or None. The header is the first non-blank line, if it opens
    plain and its normalised form is a `lens: <name> ...` line whose name names a lens."""
    first = next((line for line in (body or '').splitlines() if line.strip()), '')
    if not opens_plain(first):
        return None
    line = norm_line(first)
    m = LENS_RE.match(line)
    if not m:
        return None
    rest = m.group(1)
    field = FIELD_RE.search(rest)
    name = rest[:field.start()] if field else rest
    if not names_a_lens(name):
        return None
    at = READ_AT_RE.search(line)
    return name, (at.group(1).lower() if at else None)


def plain_lines(body):
    """The comment's lines outside fenced blocks, 4-space/tab indented blocks and quotes."""
    out, fence = [], None
    for line in (body or '').splitlines():
        m = re.match(r' {0,3}(`{3,}|~{3,})', line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
        elif line.strip() and not re.match(r' {4}|\t|[ ]{0,3}>', line):
            out.append(line)
    return out


def header_line(line):
    """A normalised line that is a lens header with a hex `read at`. A `<sha>` template line is none."""
    return bool(LENS_RE.match(line) and READ_AT_RE.search(line))


def loose_header(line):
    """A normalised line with a `lens:` field (at its start or after other text) naming any hex sha after it, however
    the read is written (`read at commit X`, `reviewed at: X`, `read_at: X`, a commit URL, `(X)`). Never a valid
    read; lens_problem flags it."""
    m = re.search(r'\blens\s*:(.*)', re.sub(r'\bnot\s+a\s+lens\s*:', ' ', line, flags=re.I), re.I)   # opt-out: no field
    return bool(m and HEX_RE.search(m.group(1)))


def no_lens_text(text):
    """Text that can never count as a lens read, name a lens or look like a lens report (lens_problem), for anything
    the queue quotes into a comment (gh errors): no `read at`, no `lens:`, no `safe to merge`."""
    text = clean_inline(text)
    text = re.sub(r'\bread([\s-]*)at\b', r'read\1@', text, flags=re.I)
    text = re.sub(r'\bsafe(\s+)to(\s+)merge\b', r'safe\1to\2land', text, flags=re.I)
    return re.sub(r'\blens(\s*):', r'lens\1-', text, flags=re.I)


def check_state(c):
    """'ok', 'running' or the failing result's name, for one statusCheckRollup item: a CheckRun (status/conclusion) or a
    commit StatusContext (state). Only an unfinished check is running: TIMED_OUT, CANCELLED, STARTUP_FAILURE,
    ACTION_REQUIRED, STALE... are failing, never 'running'."""
    if c.get('__typename') == 'StatusContext' or 'conclusion' not in c:
        st = (c.get('state') or '').upper()
        return 'running' if st in ('', 'PENDING', 'EXPECTED') else 'ok' if st == 'SUCCESS' else st
    concl = (c.get('conclusion') or '').upper()
    return 'running' if not concl else 'ok' if concl in OK_CONCLUSIONS else concl


def latest_checks(rollup):
    """The newest item per check (workflow + name, or status context). The rollup lists every run of a check on the
    head: a run cancelled because a newer one replaced it stays in it, and must not outvote that newer run. An item that
    has not started yet counts as the newest."""
    newest = {}
    for c in rollup or []:
        key = (c.get('__typename'), c.get('workflowName') or '', c.get('name') or c.get('context') or '')
        when = c.get('startedAt') or '9999'
        if key not in newest or when >= newest[key][0]:
            newest[key] = (when, c)
    return [c for _, c in newest.values()]


class Review:
    """The rules that read a PR's comments, with the project's bot logins and override comment (config.py)."""

    def __init__(self, bot_logins, override_prefix, override_association, lens_associations):
        self.bots = tuple(b.lower() for b in bot_logins)
        self.override_re = re.compile(re.escape(override_prefix) + r' ([0-9a-f]{40}):')
        self.override_association = override_association
        self.lens_associations = tuple(lens_associations)

    def is_bot(self, post):
        login = ((post.get('author') or {}).get('login') or '').lower()
        return login in self.bots or login.endswith('[bot]')

    def counts(self, post):
        """The comment or review takes part in the review at all: GitHub records its author with one of the configured
        lens associations, and it is not hidden (minimized; gh gives no isMinimized for reviews, so a review counts as
        shown). Everyone else's posts are ignored entirely: a stranger's lens-looking comment neither vouches for a
        head nor blocks one. A hidden comment is treated like a deleted one: whoever can hide it can delete it."""
        return post.get('authorAssociation') in self.lens_associations and post.get('isMinimized') is not True

    def override_sha(self, post):
        """The sha a valid override comment vouches for, else None. Text proves nothing, so only what GitHub itself
        records counts: written by an account with the configured author association and never edited since
        (includesCreatedEdit false). A missing field counts as not valid."""
        if post.get('authorAssociation') != self.override_association or post.get('includesCreatedEdit') is not False:
            return None
        m = self.override_re.match(post.get('body') or '')
        return m.group(1) if m else None

    def lens_problem(self, post):
        """Why a comment that looks like a lens report is not one valid lens read (None: it is one, or it does not look
        like a lens report). An override comment never counts. Every line is read normalised, in code blocks and
        quotes too. Two or more header lines, a first-line header that names no lens, or a header line that is not the
        first plain line: flagged, and `not a lens:` cannot clear it (the text holds EMBEDDED)."""
        body = post.get('body') or ''
        if self.override_re.match(body):
            return None
        lines = body.splitlines()
        first = next((line for line in lines if line.strip()), '')
        normed, start = [norm_line(line) for line in lines], f'starts with {first.strip()[:60]!r}'
        at = next((i for i, line in enumerate(lines) if line.strip()), 0)
        # a header line, or (after the first line) a line starting `lens:` with a sha in any form (a second lens's
        # loosely written read behind a valid header). Only at the line start: `lens:` in mid-line is often a mention.
        headers = sum(header_line(line) or (i > at and bool((m := LENS_RE.match(line)) and HEX_RE.search(m.group(1))))
                      for i, line in enumerate(normed))
        if headers > 1:
            return f'{EMBEDDED} {headers} times (post one comment per lens), {start}'
        if lens_header(body):
            return None
        if opens_plain(first) and LENS_RE.match(norm_line(first)):
            return f'its first-line header names no lens (a placeholder, a round or nothing), {start}'
        if headers:
            return f'{EMBEDDED} that is not its first plain line (a later line, a code block, quote or table), {start}'
        if any(loose_header(line) for line in normed):
            return f'{EMBEDDED} (a lens line with a sha not written `read at: <sha>`), {start}'
        if opens_plain(first) and NOT_A_LENS_RE.match(norm_line(first)):
            return None
        plain = plain_lines(body)
        if sum(bool(FIELD_LINE_RE.match(norm_line(line))) for line in plain) >= 2:   # fields of a headerless report
            return start
        if any(READ_AT_RE.search(line) for line in normed) or any(LOOKS_RE.search(norm_line(line)) for line in plain) \
                or any(LOOKS_HEADING_RE.match(line) for line in plain[:TOP_LINES]):
            return start
        return None

    def lens_posts(self, pr):
        """[(when, index, post)] of the PR's comments and reviews, bots skipped."""
        posts = [(c.get('createdAt') or '', c) for c in pr.get('comments') or []]
        posts += [(r.get('submittedAt') or '', r) for r in pr.get('reviews') or []]
        return [(when, i, post) for i, (when, post) in enumerate(posts) if not self.is_bot(post)]

    def lens_entries(self, pr):
        """{lens key: ((when, index), sha or None, why or None)}: each lens's NEWEST header, from the posts that count
        (counts). A comment that looks like a lens report but is not one valid read (lens_problem) is an entry of its
        own with no sha: it read nothing until it is re-posted as one comment per lens with a first-line header, or an
        override covers it. A lens comment edited after posting (includesCreatedEdit not false: GitHub records that it
        changed, not what) read nothing until that lens posts a new comment."""
        found = {}
        for when, i, post in self.lens_posts(pr):
            if not self.counts(post):
                continue
            problem = self.lens_problem(post)
            if problem:
                where = post.get('url') or f'by {(post.get("author") or {}).get("login") or "?"} at {when or "?"}'
                found[f'{NO_HEADER} ({where}, {problem})'] = ((when, i), None, None)
                continue
            header = lens_header(post.get('body') or '')
            if not header:
                continue
            key = lens_key(header[0])
            if key not in found or (when, i) >= found[key][0]:
                edited = post.get('includesCreatedEdit') is not False
                found[key] = ((when, i), None, EDITED) if edited else ((when, i), header[1], None)
        return found

    def reviewed_head(self, pr):
        """(reviewed, sha, why). Not reviewed (no lens comments): (False, None, ...). Reviewed: the one full 40-hex sha
        every lens's newest read names. A shorter read names nothing: the queue can only match a prefix against the
        commits GitHub lists now, and after a force-push a new commit can be made to share the prefix of the dropped
        one that was read. A full sha need not be a commit of the PR now (a rebase or force-push drops the read
        commit; GitHub keeps it, and the queue compares content). An override covers each lens whose newest read is
        older than it. Lenses that name different commits, a short sha or none: sha None."""
        found = self.lens_entries(pr)
        overrides = [((when, i), sha) for when, i, post in self.lens_posts(pr) for sha in [self.override_sha(post)]
                     if sha]
        if not found:
            return False, None, 'no lens comments'
        newest = max(overrides) if overrides else None
        reads = {k: (newest[1] if newest and at < newest[0] else sha if FULL_SHA.fullmatch(sha or '') else None)
                 for k, (at, sha, _) in found.items()}
        shas = set(reads.values())
        if len(shas) == 1 and None not in shas:
            return True, shas.pop(), 'every lens read it'

        def gap(k):
            _, sha, why = found[k]
            return why or (f'{sha[:12]}: not a full 40-hex sha' if sha else 'no sha: no single commit of this PR')
        listed = ', '.join(f'{k} {v[:12] if v else "(" + gap(k) + ")"}' for k, v in sorted(reads.items()))
        return True, None, f'the lenses did not all read one head: {listed}'
