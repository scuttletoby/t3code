"""Which head a pull request's review vouched for: the lens-comment rules of the merge queue (pr_queue.py).

A lens is one review angle, posted as one PR comment whose FIRST line is its header:

    lens: <name> · reviewer: <who> · read at: <sha>

also `Lens:`, `**Lens:**`, `## lens:`, `- lens:`. This grammar is fixed (README.md, "Lens comments"): it is the one
format the queue reads, and a project that writes its lens reports differently must change its reports, not this
file. What differs per project comes from the config (config.py): whose comments count at all (author associations;
which logins are bots), and the override comment (`<prefix> <40-hex sha>: ...`, posted by an account with one author
association and never edited) that vouches for a head when the lenses read older commits.

Only what GitHub records decides who wrote a comment: one by an account without a configured lens association (anyone
can comment on a public repository) or by a bot is ignored entirely, neither a read nor a lens that read nothing (the
run log names each such lens-looking comment). Among the rest, fail closed throughout: a comment that LOOKS like a lens
report but is not exactly one valid read, or a lens comment edited after posting or hidden (minimized), counts as a
lens that read nothing, so the queue never takes a reviewed PR for an unreviewed one. A hidden override counts nothing. A read is a full 40-hex sha: a shorter one could be matched by a commit pushed later.
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
# Every pattern here runs in time linear in the line, also for a trusted author's 64 KB comment: a blank run is tried
# once, from its start ((?<!\s)), and `\s*(?::\s*)?` never splits one blank run between two `\s*`.
FIELD_RE = re.compile(r'(?<!\s)\s*(?:[·,—]\s*)?\b(?:reviewer\s*:|read[\s-]*at\b\s*(?::|[0-9a-f]{7}))', re.I)
READ_AT_RE = re.compile(r'\bread[\s-]*at\b\s*(?::\s*)?([0-9a-f]{7,40})\b', re.I)
FULL_SHA = re.compile(r'[0-9a-f]{40}')
NO_HEADER = 'comment without a first-line lens header'
# The ways out of a NO_HEADER entry (lens_entries), for the queue's advice. A correct re-post adds a new entry and
# leaves the old one; hiding the old one is not enough either.
# The queue posts this text, so it never holds the lens marker with its colon (no_lens_text).
NO_HEADER_WAY_OUT = ('delete that comment (hiding it is not enough), or edit it to start with `not a lens` and a colon '
                     'and hold no lens header line, or')
EDITED = 'edited after posting: post a new comment instead'
# A review's edit and hidden flags, by review node id, as the caller read them with a query of its own (pr_queue
# review_flags). gh pr view never fetches them for reviews but prints `includesCreatedEdit: false` anyway, so a review
# takes its flags from here only; one this does not hold reads nothing (FLAGS_UNREAD).
REVIEW_FLAGS = 'reviewFlags'
FLAGS_UNREAD = 'a review whose edit and hidden flags were not read from GitHub: re-run, or post a new comment'
UNREAD = '_flagsUnread'   # the mark review_post puts on such a review
HIDDEN = 'hidden: post a new comment instead'
SHORT = 'not a full 40-hex sha'
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
STRANGER_SCAN = 4096   # characters of a post that does not count read to decide whether to log it


_TAG_OPEN = re.compile(r'</?[A-Za-z]')


def _strip_tags(text):
    """re.sub(r'</?[A-Za-z][^>]*>', '', text) in linear time: each tag runs to the first `>` after its start (a `<` inside
    a quoted attribute stays inside the tag, as GitHub renders it). No `>` left: no later tag can close either."""
    out, pos = [], 0
    for m in _TAG_OPEN.finditer(text):
        if m.start() < pos:
            continue
        end = text.find('>', m.end())
        if end < 0:
            break
        out.append(text[pos:m.start()])
        pos = end + 1
    out.append(text[pos:])
    return ''.join(out)


def _strip_links(text):
    """re.sub(r'!?\\[([^\\]]*)\\]\\([^)]*\\)', r'\\1', text) in linear time. Every `[` before a `]` shares that `]`; no `]`
    or no `)` left: no later link can close either."""
    out, pos, i = [], 0, text.find('[')
    while i >= 0:
        close = text.find(']', i + 1)
        if close < 0:
            break
        if text[close + 1:close + 2] != '(':
            i = text.find('[', close + 1)
            continue
        end = text.find(')', close + 2)
        if end < 0:
            break
        start = i - 1 if i > pos and text[i - 1] == '!' else i
        out += [text[pos:start], text[i + 1:close]]
        pos = end + 1
        i = text.find('[', pos)
    out.append(text[pos:])
    return ''.join(out)

# The earlier, narrower forms (a tag holds no `<`, a link text no bracket, its target no paren). Each line is read both
# ways (lens_problem): either way can show a header the other hides (`lens<a title="<">: ...` only the source's way,
# `<lens: ... read at: <sha></b>` only this way), so a comment counts only when both read it alike.
_TAG_NARROW = re.compile(r'</?[A-Za-z][^<>]*>')
_LINK_NARROW = re.compile(r'!?\[([^\[\]]*)\]\([^()]*\)')


def clean_inline(text, narrow=False):
    """Markdown that can hide a word, gone: HTML tags (`<b>`, also a `<name>` placeholder), links `[x](y)` -> x, `*` and
    backticks, `|` -> space, `_` at a word edge (`_lens_` -> lens; `pr_queue` stays). narrow: the narrower tag and link
    forms (_TAG_NARROW, _LINK_NARROW)."""
    # The source's regexes `</?[A-Za-z][^>]*>` and `!?\[([^\]]*)\]\([^)]*\)`, as linear scans: the regexes took time
    # that grows with the square of the text (an unclosed `[` x 64 KB took minutes), and the earlier fix ([^<>],
    # [^\[\]], [^()]) changed the answers: `lens<a title="<">: x · read at<a title="<">: <sha>` renders as a lens header
    # but read as none, so a stale lens went unseen.
    text = _LINK_NARROW.sub(r'\1', _TAG_NARROW.sub('', text)) if narrow else _strip_links(_strip_tags(text))
    text = re.sub(r'[*`]', '', text).replace('|', ' ')
    return re.sub(r'(?<![^\W_])_+|_+(?![^\W_])', '', text)


def norm_line(line, narrow=False):
    """One line as lens_problem and lens_header read it: clean_inline, then any leading quote markers, list markers (`-`
    `+` `•` `1.` `1)`, a task box `[x]`; `*` is gone already), heading marks and `~~~` fences stripped."""
    s, prev = clean_inline(line, narrow), None
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
    # The old loop (join the words, strip `*_\`` from both ends, drop one trailing ` <marker>`, until nothing changes)
    # copied the whole name per pass: minutes for a 64 KB name of 13 K markers. The same steps on a word list, where a
    # word stripped empty at an end stays as the blank in front of a marker until the next pass, as in the text.
    words = name.lower().split()
    lo, hi = 0, len(words)
    while True:
        while lo < hi and not words[lo]:
            lo += 1
        while lo < hi and not words[hi - 1]:
            hi -= 1
        if lo == hi:
            return 'unnamed'
        before = (words[lo], words[hi - 1], hi)
        words[lo] = words[lo].lstrip('*_`')
        words[hi - 1] = words[hi - 1].rstrip('*_`')
        if hi - lo >= 2 and words[hi - 1] and re.fullmatch(r'r\d+|lens|re-?check', words[hi - 1]):
            hi -= 1
        if (words[lo], words[hi - 1], hi) == before:
            return ' '.join(w for w in words[lo:hi] if w) or 'unnamed'


def names_a_lens(name):
    """A header's name names a lens: it has a letter or digit, is no `<...>` template placeholder and is more than a
    round marker (`r4`, `re-check`). Otherwise two such headers would share one key and the newer read would stand for
    both."""
    key = lens_key(name)
    return bool(re.search(r'[^\W_]', key)) and not re.search(r'<[^<>]*>', name) \
        and not re.fullmatch(r'unnamed|r\d+|lens|re-?check', key)


def lens_header(body, narrow=False):
    """(name, sha or None) of a comment's lens header, or None. The header is the first non-blank line, if it opens
    plain and its normalised form is a `lens: <name> ...` line whose name names a lens."""
    first = next((line for line in (body or '').splitlines() if line.strip()), '')
    if not opens_plain(first):
        return None
    line = norm_line(first, narrow)
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


def no_lens_text(text, keep_markdown=False):
    """Text that can never count as a lens read, name a lens or look like a lens report (lens_problem), for anything
    the queue quotes into a comment (gh errors): no `read at`, no `lens:`, no `safe to merge`. keep_markdown: the
    queue's own advice (a command in backticks, a `<sha>` placeholder) stays as written when, read without its markdown,
    it holds none of these; otherwise it is cleaned like any other text."""
    plain = clean_inline(text)
    out = re.sub(r'\bread([\s-]*)at\b', r'read\1@', plain, flags=re.I)
    out = re.sub(r'\bsafe(\s+)to(\s+)merge\b', r'safe\1to\2land', out, flags=re.I)
    out = re.sub(r'\blens(\s*):', r'lens\1-', out, flags=re.I)
    return text if keep_markdown and out == plain else out


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

    def __init__(self, bot_logins, override_prefix, override_association, lens_associations, log=print):
        self.bots = tuple(b.lower() for b in bot_logins)
        self.override_re = re.compile(re.escape(override_prefix) + r' ([0-9a-f]{40}):')
        self.override_association = override_association
        self.lens_associations = tuple(lens_associations)
        self.log, self.logged = log, set()

    def is_bot(self, post):
        """A configured bot login, a login ending in `[bot]` (GitHub's REST API), or an author whose type is Bot
        (`is_bot` true, or GraphQL's `__typename` Bot). `gh pr view --json comments,reviews` gives only the login, and
        without the `[bot]` suffix, so for comments only bot_logins (and the author associations) keep bots out; a
        review gets its author type from REVIEW_FLAGS (review_post)."""
        author = post.get('author') or {}
        login = (author.get('login') or '').lower()
        return login in self.bots or login.endswith('[bot]') or author.get('is_bot') is True \
            or author.get('__typename') == 'Bot'

    def counts(self, post):
        """The comment or review takes part in the review at all: GitHub records its author with one of the configured
        lens associations. Everyone else's posts are ignored entirely: a stranger's lens-looking comment neither
        vouches for a head nor blocks one. Hidden (minimized) posts of these authors still take part, as lenses that
        read nothing (lens_entries): hiding old rounds is routine, and must not make a reviewed PR look unreviewed."""
        return post.get('authorAssociation') in self.lens_associations

    @staticmethod
    def hidden(post):
        """Hidden (minimized). A review's isMinimized comes from REVIEW_FLAGS (lens_posts), never from gh pr view."""
        return post.get('isMinimized') is True

    def override_sha(self, post):
        """The sha a valid override comment vouches for, else None. Text proves nothing, so only what GitHub itself
        records counts: written by an account with the configured author association, never edited since
        (includesCreatedEdit false) and not hidden. A missing includesCreatedEdit counts as not valid."""
        if post.get('authorAssociation') != self.override_association or post.get('includesCreatedEdit') is not False \
                or self.hidden(post):
            return None
        m = self.override_re.match(post.get('body') or '')
        return m.group(1) if m else None

    def lens_problem(self, post):
        """Why a comment that looks like a lens report is not one valid lens read (None: it is one, or it does not look
        like a lens report). An override comment never counts. Every line is read normalised, in code blocks and
        quotes too. Two or more header lines, a first-line header that names no lens, or a header line that is not the
        first plain line: flagged, and `not a lens:` cannot clear it (the text holds EMBEDDED). Read both ways
        (clean_inline narrow or not): flagged when either way flags it, or when the two read different headers."""
        body = post.get('body') or ''
        if self.override_re.match(body):
            return None
        problem = self.read_problem(body) or self.read_problem(body, narrow=True)
        if problem is None and lens_header(body) != lens_header(body, narrow=True):
            first = next((line for line in body.splitlines() if line.strip()), '')
            return (f'its first line reads as a different lens header, or as one and as none, depending on how its HTML '
                    f'tags and links are read; starts with {first.strip()[:60]!r}')
        return problem

    def read_problem(self, body, narrow=False):
        """lens_problem's rules with one way of reading tags and links (clean_inline)."""
        def norm(line):
            return norm_line(line, narrow)
        lines = body.splitlines()
        first = next((line for line in lines if line.strip()), '')
        normed, start = [norm(line) for line in lines], f'starts with {first.strip()[:60]!r}'
        at = next((i for i, line in enumerate(lines) if line.strip()), 0)
        # a header line, or (after the first line) a line starting `lens:` with a sha in any form (a second lens's
        # loosely written read behind a valid header). Only at the line start: `lens:` in mid-line is often a mention.
        headers = sum(header_line(line) or (i > at and bool((m := LENS_RE.match(line)) and HEX_RE.search(m.group(1))))
                      for i, line in enumerate(normed))
        if headers > 1:
            return f'{EMBEDDED} {headers} times (post one comment per lens), {start}'
        if lens_header(body, narrow):
            return None
        if opens_plain(first) and LENS_RE.match(norm(first)):
            return f'its first-line header names no lens (a placeholder, a round or nothing), {start}'
        if headers:
            return f'{EMBEDDED} that is not its first plain line (a later line, a code block, quote or table), {start}'
        if any(loose_header(line) for line in normed):
            return f'{EMBEDDED} (a lens line with a sha not written `read at: <sha>`), {start}'
        if opens_plain(first) and NOT_A_LENS_RE.match(norm(first)):
            return None
        plain = plain_lines(body)
        if sum(bool(FIELD_LINE_RE.match(norm(line))) for line in plain) >= 2:   # fields of a headerless report
            return start
        if any(READ_AT_RE.search(line) for line in normed) or any(LOOKS_RE.search(norm(line)) for line in plain) \
                or any(LOOKS_HEADING_RE.match(line) for line in plain[:TOP_LINES]):
            return start
        return None

    @staticmethod
    def review_post(review, flags):
        """A review with the flags the caller read for it (REVIEW_FLAGS), never the ones gh pr view printed: gh does not
        fetch includesCreatedEdit or isMinimized for reviews and prints its defaults (false). No flags read for it: not
        unedited (includesCreatedEdit None, FLAGS_UNREAD), so it reads nothing and is no override."""
        own = flags.get(review.get('id')) if isinstance(flags, dict) and isinstance(review.get('id'), str) else None
        post = {**review, 'includesCreatedEdit': None, 'isMinimized': None, UNREAD: True}
        if isinstance(own, dict) and isinstance(own.get('includesCreatedEdit'), bool) \
                and isinstance(own.get('isMinimized'), bool):
            post.update(includesCreatedEdit=own['includesCreatedEdit'], isMinimized=own['isMinimized'])
            del post[UNREAD]
            if isinstance(own.get('url'), str) and own['url']:
                post['url'] = own['url']
            author = own.get('author') if isinstance(own.get('author'), dict) else {}
            if author.get('__typename'):   # GitHub's own author type: a Bot is a bot whatever its login
                post['author'] = {**(review.get('author') or {}), '__typename': author['__typename']}
        return post

    def lens_posts(self, pr):
        """[(when, index, post)] of the PR's comments and reviews, bots skipped. Reviews carry the flags read for them
        (review_post)."""
        flags = pr.get(REVIEW_FLAGS)
        posts = [(c.get('createdAt') or '', c) for c in pr.get('comments') or []]
        posts += [(r.get('submittedAt') or '', self.review_post(r, flags)) for r in pr.get('reviews') or []]
        return [(when, i, post) for i, (when, post) in enumerate(posts) if not self.is_bot(post)]

    def lens_entries(self, pr):
        """{lens key: ((when, index), sha or None, why or None)}: each lens's NEWEST header, from the posts that count
        (counts). A comment that looks like a lens report but is not one valid read (lens_problem) is an entry of its
        own, keyed by the comment, with no sha: a correct new post of that lens does not replace it, and hiding it
        does not clear it. It reads nothing until it is deleted, its first line is edited to `not a lens: ...` with no
        lens header line left in it, or a newer override covers it (NO_HEADER_WAY_OUT). A lens comment edited after
        posting (includesCreatedEdit not false: GitHub records that it changed, not what) or hidden (minimized) reads
        nothing until that lens posts a new comment; so does a review whose flags were not read (FLAGS_UNREAD). A
        lens-looking post that does not count is logged (log only, never a comment), so a wrong lens_author_associations
        shows in the run."""
        found = {}
        for when, i, post in self.lens_posts(pr):
            where = post.get('url') or f'by {(post.get("author") or {}).get("login") or "?"} at {when or "?"}'
            if not self.counts(post):   # the author first: a stranger's text is only ever read in part, for the log
                head = {**post, 'body': (post.get('body') or '')[:STRANGER_SCAN]}
                if where not in self.logged and (self.lens_problem(head) or lens_header(head['body'])):
                    self.logged.add(where)
                    association = post.get('authorAssociation')
                    self.log(f'ignored a lens-looking post {where}: '
                             f'{"author association " + association if association else "no author association"} '
                             f'is not one of lens_author_associations ({", ".join(self.lens_associations)})')
                continue
            problem = self.lens_problem(post)
            header = None if problem else lens_header(post.get('body') or '')
            if problem:
                hidden = ', hidden' if self.hidden(post) else ''
                found[f'{NO_HEADER} ({where}, {problem}{hidden})'] = ((when, i), None, None)
                continue
            if not header:
                continue
            key = lens_key(header[0])
            if key not in found or (when, i) >= found[key][0]:
                why = FLAGS_UNREAD if post.get(UNREAD) else HIDDEN if self.hidden(post) \
                    else EDITED if post.get('includesCreatedEdit') is not False else None
                found[key] = ((when, i), None, why) if why else ((when, i), header[1], None)
        return found

    def lens_reads(self, pr):
        """{lens key: (sha, gap, override)}: what each lens of lens_entries counts as reading, the one rule reviewed_head
        and the head-check action (../head-check) both use. sha: the full 40-hex sha it read, or the newest override's
        sha when that override is newer than the lens's newest read (override is then that sha too); None when it reads
        nothing, and gap then says why (edited, hidden, a short sha, no sha). Empty: no lens comments."""
        found = self.lens_entries(pr)
        overrides = [((when, i), sha) for when, i, post in self.lens_posts(pr) for sha in [self.override_sha(post)]
                     if sha]
        newest = max(overrides) if overrides else None
        reads = {}
        for k, (at, sha, why) in found.items():
            if newest and at < newest[0]:
                reads[k] = (newest[1], None, newest[1])
            elif FULL_SHA.fullmatch(sha or ''):
                reads[k] = (sha, None, None)
            else:
                reads[k] = (None, why or (f'{sha[:12]}: {SHORT}' if sha else 'no sha: no single commit of this PR'), None)
        return reads

    def reviewed_head(self, pr):
        """(reviewed, sha, why). Not reviewed (no lens comments): (False, None, ...). Reviewed: the one full 40-hex sha
        every lens's newest read names. A shorter read names nothing: the queue can only match a prefix against the
        commits GitHub lists now, and after a force-push a new commit can be made to share the prefix of the dropped
        one that was read. A full sha need not be a commit of the PR now (a rebase or force-push drops the read
        commit; GitHub keeps it, and the queue compares content). An override covers each lens whose newest read is
        older than it (lens_reads). Lenses that name different commits, a short sha or none: sha None."""
        reads = self.lens_reads(pr)
        if not reads:
            return False, None, 'no lens comments'
        shas = {sha for sha, _, _ in reads.values()}
        if len(shas) == 1 and None not in shas:
            return True, shas.pop(), 'every lens read it'
        listed = ', '.join(f'{k} {sha[:12] if sha else "(" + gap + ")"}' for k, (sha, gap, _) in sorted(reads.items()))
        return True, None, f'the lenses did not all read one head: {listed}'
