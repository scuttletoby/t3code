#!/usr/bin/env python3
"""Is this pull request's head the change its review read? The pr-queue's `head_check` as a required check (README.md).

The merge queue (../pr-queue) re-arms auto-merge only for a head that holds exactly the change the lenses read, but it
only touches pull requests that are behind their base. A plain push to an armed pull request that is already up to
date would merge unread; this check, required by the branch ruleset, fails that head. It reads the review with the
queue's own rules, imported, never copied:
  - lens.Review.reviewed_head and lens_reads: the one full 40-hex sha every lens's newest read names, or a newer
    override comment's. Only comments of the config's lens_author_associations count (bots never); an edited or
    hidden lens comment, a short sha and a comment that only looks like a lens report read nothing. A review's edited
    and hidden flags come from the queue's own query (pr_queue.review_flags), never from gh pr view; the same query
    reads the inline review comments (on a line of the diff), which count like any other comment.
  - pr_queue.holds: the head is that commit, or a clean merge of the base into it, byte for byte (a queue update).
The config is the queue's own JSON file (config.py), read from the base commit, so the check and the queue read the
same people and override, and a pull request cannot change them for its own check. The config the merge would leave is
read for one thing only: it must parse, under size caps, or the head is refused (merged_config_problem), since merged
it would refuse every head after it.
`--required-lenses` (the project's, per pull request if it likes) adds lenses that must be among the reads.
The config's optional `review` block (../pr-queue/README.md, "Review rules") names lenses by changed path: the paths
are `git diff --name-status -M` from the merge base to the head (changed_paths), judged by review_judge.judge. In
`enforce` its lenses are required too; in `shadow` the summary only says what they would have been. A change it cannot
judge (no merge base, a failed diff) is refused, never passed. The same block judges the review itself
(review_judge.judge_rounds, review_rounds): in `enforce` a lens's newest report with a blocker or major finding that
no later round or person cleared fails the head (`findings: true`), and so does a report with no findings section;
the approach lens is required from round `rounds.approach_at`; a round past `rounds.max` fails the head until a
person's override comment for it (a login in `override_authors`; none listed: no override is a person's, issue #78)
lets it through. A round is a read of a different own change (own_change), so a queue update adds none. In `shadow`
the summary only says so. Without the block nothing of this runs.

Pass: every lens read the head (or a newer override vouches for it), and every required lens is there; a pull
request with no lens comments and no required lens (nothing to compare); a draft, with --pass-drafts only. Fail,
never pass, when anything is unknown: GitHub unreadable after ATTEMPTS (the reviews' flags and inline comments too), a
malformed answer, a checkout whose merge commit merges another head than the one GitHub reports, a read commit that is
not in the checkout and cannot be fetched.
Refuse to start (exit 2, before GitHub is read) on a config or argument error, a git older than 2.43, and a checkout
that is not GitHub's two-parent merge commit, is shallow or is tampered with. Reads only (a fetch of a missing read
commit into the job's own checkout aside) and posts nothing: a re-run on the same head gives the same answer, and a
run stopped half-way leaves nothing behind.
Exit codes: 0 pass, 1 fail, 2 refused to start (every reason printed at once).
    head_check.py --repo OWNER/NAME --pr N --config FILE [--path DIR] [--required-lenses TEXT] [--pass-drafts]
    head_check.py ... --bare --path BARE_REPO --base SHA --head SHA [--remote URL]
Bare mode is the same rules on a bare repository the gate (head_gate.py) fetched the base tip and the head into by sha,
for a check that runs where nothing of the pull request is ever checked out (fork-head-gate.yml); only where base and
head come from differs (Checkout).
`--required-lenses` takes one lens name per line (a name may hold a comma: `data-integrity (DI-1, DI-2)`).
"""
import argparse, hashlib, json, os, re, subprocess, sys, tempfile, time, unicodedata

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pr-queue'))
import config  # noqa: E402
import lens  # noqa: E402
import pr_queue  # noqa: E402
import review_config  # noqa: E402
import review_judge  # noqa: E402

ATTEMPTS, RETRY_SECONDS = 3, 10
FETCH_SECONDS = 120   # a fetch of a read commit that takes longer counts as failed (the job has 15 minutes in all)
FIELDS = 'isDraft,headRefOid,comments,reviews'
WARN = 'warn'   # a verdict row that passes but a reader should look at (a comment that is no lens report): not green
LOG_MARK = {'ok': 'ok  ', 'warn': 'WARN', 'fail': 'FAIL'}
SHA = pr_queue.SHA
REFUSED = pr_queue.REFUSED
# Every git call: the queue's safe flags (no hooks, fsmonitor, commit-graph, replace refs or attributes file). git runs
# in an empty folder with --git-dir, so the checkout's own .gitattributes (the pull request's) cannot change how
# merge-tree merges (merge=union would make a conflict "clean").
GIT_SAFE = pr_queue.GIT_SAFE


class Checkout:
    """What the check reads with git (`run` replaces git in tests), in one of two modes around the same rules:
    - checkout: the job's checkout of GitHub's merge commit (`path` holds `.git`); base and head are its two parents.
    - bare (`bare=(base, head)`): a bare repository the gate fetched the base tip and the head into (head_gate.py),
      objects only: no work tree, no hooks, no submodules, and nothing of the pull request's is ever checked out. Base
      and head are the shas the gate read from GitHub's API."""

    def __init__(self, path, run=None, bare=None, remote='origin'):
        self.bare = tuple(bare) if bare else None
        self.git_dir = os.path.abspath(path) if self.bare else os.path.join(os.path.abspath(path), '.git')
        self.run, self.remote = run, remote
        self.holder = 'the gate fetched' if self.bare else 'the checkout merges'
        # Bare, git logs in the way the queue does (gh's login, GH_TOKEN), never with a helper of the machine's config.
        self.auth = pr_queue.GIT_AUTH if self.bare else ()

    def git(self, *a, errors='strict', timeout=None, fetch=False):
        """errors: how git's output is decoded (as UTF-8, whatever the locale); 'surrogateescape' for file names, which
        need not be UTF-8. timeout: seconds, after which git is killed and subprocess.TimeoutExpired raised. fetch: the
        call fetches from `remote` (pr_queue.git_env: what of the job's GIT_RUN it keeps). In checkout mode every call
        may fetch from the caller's origin (a partial clone's missing object), so each keeps them, as before."""
        if self.run:
            return self.run(*a)
        with tempfile.TemporaryDirectory(prefix='head-check-') as empty:
            return subprocess.run(['git', *GIT_SAFE, *self.auth, '--git-dir', self.git_dir, *a], cwd=empty, text=True,
                                  encoding='utf-8', errors=errors, capture_output=True, timeout=timeout,
                                  env=pr_queue.git_env(os.environ, self.remote if fetch or not self.bare else None))

    def problem(self):
        """Why the repository cannot be read as git made it, or None: not a git folder, shallow (the base's history is
        needed), holding a file that changes what its objects or merges say (pr_queue.UNTRUSTED_FILES), or, bare,
        without the base or the head."""
        if not os.path.isdir(self.git_dir):
            if self.bare:
                return f'{self.git_dir} is no git folder: fetch the base and the head into a bare repository first'
            return f'{self.git_dir} is no git folder: check out the pull request first (actions/checkout)'
        if os.path.lexists(os.path.join(self.git_dir, 'shallow')):
            return ('the bare repository is shallow: fetch the full history' if self.bare
                    else 'the checkout is shallow: check it out with fetch-depth: 0')
        bad = [f for f in pr_queue.UNTRUSTED_FILES if os.path.lexists(os.path.join(self.git_dir, f))]
        if bad:
            return f'the {"bare repository" if self.bare else "checkout"} holds {", ".join(bad)}, which a fetch never writes'
        if self.bare:
            missing = [f'{what} {sha[:12]}' for what, sha in zip(('base', 'head'), self.bare) if not self.has(sha)]
            if missing:
                return f'the bare repository does not hold the {" or the ".join(missing)}'
        return None

    def merge_parents(self):
        """(base, head): bare, the two shas the gate fetched; else HEAD's parents when it is a two-parent merge commit,
        else None."""
        if self.bare:
            return self.bare
        r = self.git('rev-list', '--parents', '-n', '1', 'HEAD')
        line = r.stdout.split() if r.returncode == 0 else []
        return (line[1], line[2]) if len(line) == 3 and all(SHA.fullmatch(s) for s in line) else None

    def has(self, sha):
        return self.git('cat-file', '-e', sha + '^{commit}').returncode == 0

    def fetch(self, sha):
        """A read commit no ref of the repository reaches (a force-push dropped it; GitHub keeps it), from origin with the
        credentials the checkout stored or the gate's environment gives (none: a private repository then fails). A fetch
        that takes over FETCH_SECONDS failed."""
        try:
            done = self.git('fetch', '-q', '--no-tags', '--no-recurse-submodules', self.remote, sha, fetch=True,
                            timeout=FETCH_SECONDS).returncode == 0
        except subprocess.TimeoutExpired:
            return False
        return done and self.has(sha)


def required_names(text):
    """{lens key: name as written} from one lens name per line, blank lines skipped; (keys, problems). Lines only, never
    commas: a lens name may hold one (`data-integrity (DI-1, DI-2)`)."""
    names, problems = {}, []
    for name in (n.strip() for n in (text or '').splitlines()):
        if not name:
            continue
        if not lens.names_a_lens(name):
            problems.append(f'required lens {name!r} names no lens (a placeholder, a round marker or nothing)')
        else:
            names.setdefault(lens.lens_key(name), name)
    return names, problems


def well_formed(pr):
    return isinstance(pr, dict) and isinstance(pr.get('isDraft'), bool) \
        and all(isinstance(pr.get(k), list) for k in ('comments', 'reviews'))


def read_pr(repo, n, gh, sleep):
    """(the PR, None) or (None, why it could not be read), after ATTEMPTS tries. Every PR also needs its reviews' own
    flags and its inline review comments (pr_queue.review_flags, read after the view): gh pr view prints
    includesCreatedEdit false for every review without asking GitHub and lists no inline comment at all, so a PR for
    which that read fails is a PR that could not be read."""
    why = ''
    for attempt in range(ATTEMPTS):
        if attempt:
            sleep(RETRY_SECONDS)
        r = gh('pr', 'view', str(n), '--repo', repo, '--json', FIELDS)
        try:
            pr = json.loads(r.stdout) if r.returncode == 0 else None
        except ValueError:
            pr = None
        if not well_formed(pr):
            why = (r.stderr or r.stdout or '').strip()[:300] or 'gh gave no usable answer'
            continue
        flags, inline, why = pr_queue.review_flags(gh, repo, n)
        if flags is not None:
            pr[lens.REVIEW_FLAGS], pr[lens.REVIEW_COMMENTS] = flags, inline
            return pr, None
    return None, why


def same_change(checkout, head, read):
    """(True, how) when the checkout's merge commit merges `head` and head holds `read`'s change (pr_queue.holds),
    else (False, why). Only the checkout's own commits and GitHub's (a fetch of a missing read) are looked at. cli
    refuses a checkout that is no merge commit before this runs (base_config); the check here is for other callers."""
    parents = checkout.merge_parents()
    if not parents:
        return False, "HEAD is not GitHub's two-parent merge commit, so the pull request head is unknown"
    base, merged = parents
    if merged != head:
        return False, (f'{checkout.holder} {merged[:12]} but GitHub says the head is {head[:12]}: '
                       'a newer head has its own run')
    if head == read:
        return True, f'{head[:12]} is the commit read'
    if not (checkout.has(read) or checkout.fetch(read)):
        return False, (f'the read commit {read[:12]} is not in the repository and could not be fetched from origin '
                       '(GitHub no longer has it, or the fetch had no credentials: persist-credentials: false)')
    if pr_queue.holds(checkout.git, head, read, pr_queue.UPDATES, base):
        return True, f'{head[:12]} is {read[:12]} merged with the base, nothing else'
    return False, f'{head[:12]} is not the change read at {read[:12]}, nor that change merged with the base'


def verdict(pr, review, checkout, required=None, pass_drafts=False, rules_name_lenses=False):
    """(ok, reason, rows): rows is [(lens, ok, short reason)] per lens, required ones first. rules_name_lenses: the
    config's review rules (enforce) added required lenses, so a missing one may come from there too."""
    required = required or {}
    head = str(pr.get('headRefOid') or '').lower()
    if not SHA.fullmatch(head):
        return False, 'GitHub names no head commit for the pull request', []
    if pr['isDraft'] and pass_drafts:
        return True, 'a draft: checked again when it is marked ready for review', []
    reads = review.lens_reads(pr)
    reviewed, read, why = review.reviewed_head(pr)
    missing = [name for key, name in required.items() if key not in reads]
    # comments that look like a lens report but carry no lens marker and hold no open finding: no report, so they pass;
    # named in a WARN row, never silent. Those that hold an open finding block: NO_HEADER entries of reads (held).
    notes = [(f'{lens.NOT_LENS} ({where})', WARN, why) for where, why in review.not_lens_reports(pr)]
    held = review.unmarked_held(pr)
    why = short_held(why, held)   # the reason names a held comment by where it is: the fix is said once, first
    found = short_held(', '.join(sorted(reads)), held) or 'none'
    if not reads and not required:
        return True, 'no lens comments and no required lens: nothing to compare', notes
    ok, how = same_change(checkout, head, read) if reviewed and read else (False, None)
    rows = [(name, False, f'required: no lens comment of a counted author has this name (lenses found: {found})')
            for name in missing]
    for key, (sha, gap, override) in sorted(reads.items()):
        if not sha:
            text = f'reads nothing: {gap}'
        elif not read:
            text = f'read {sha[:12]}; the lenses did not all read one head'
        else:
            text = f'{"override " if override else "read "}{sha[:12]}: {how}'
        rows.append(((f'{key} (required)' if key in required else key), bool(sha and read and ok), text))
    rows += notes
    if missing:
        if notes or held:   # the fix first: a look-alike is most likely the report, so no config hint
            return False, (f'{marker_hint(len(notes) + len(held), held)}. Required lens without a read: '
                           f'{", ".join(missing)}; lenses found: {found}'), rows
        where = ' or the review rules of the config' if rules_name_lenses else ''
        return False, (f'required lens without a read: {", ".join(missing)}. No counted lens comment has that name: '
                       f'check it in required-lenses (one name per line){where}; lenses found: {found}'), rows
    if not read:
        return False, (f'{marker_hint(len(held), held)}. {why}' if held else why), rows
    return ok, how, rows


def marker_hint(count, held=()):
    """The fix for comments that look like a lens report but carry no lens marker, first in a failure reason; with
    held ones (unmarked_held), the way out of those too (lens.HELD_WAY_OUT, the one wording)."""
    out = f'{count} comment(s) look like a lens report but carry no lens marker: {lens.REPOST}'
    return f'{out}, and {lens.HELD_WAY_OUT} (one that holds an open finding blocks until then)' if held else out


def short_held(text, held):
    """text with each held comment's long reason (its NO_HEADER key) cut to where it is: its row says the rest."""
    for where, why in held:
        text = text.replace(f'{where}, {why}', where)
    return text


def escape(text):
    """One line for a workflow command (::error::) or the log: nothing in a comment can start a command."""
    return str(text).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def cell(text):
    return re.sub(r'[|<>`\r\n]', ' ', str(text))


def mark(good):
    """A row's mark: ok, warn (WARN: it passes, but a reader should look) or fail."""
    return 'warn' if good is WARN else 'ok' if good else 'fail'


def report(ok, reason, rows, n, env, out=print):
    """The log, the step summary (one table, appended) and the `result` output."""
    for name, good, text in rows:
        out(f'  {LOG_MARK[mark(good)]} {escape(name)}: {escape(text)}')
    out(f'#{n}: head check {"passed" if ok else "FAILED"}: {escape(reason)}' if ok else f'::error::#{n}: head check '
        f'failed: {escape(reason)}')
    summary = env.get('GITHUB_STEP_SUMMARY')
    if summary:
        lines = [f'### Head check #{n}: {"pass" if ok else "fail"}', '', cell(reason), '']
        if rows:
            lines += ['| lens | | reason |', '| --- | --- | --- |']
            lines += [f'| {cell(name)} | {mark(good)} | {cell(text)} |' for name, good, text in rows]
        with open(summary, 'a', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
    output = env.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a', encoding='utf-8') as f:
            f.write(f'result={"pass" if ok else "fail"}\n')


def gh_run(*a):
    return subprocess.run(['gh', *a], text=True, capture_output=True)


def changed_paths(checkout, base, head):
    """([(status, paths)], None) of every path the head changes against its merge base with `base` (three dots: what
    the base changed meanwhile is not the pull request's), or (None, why). `git diff --name-status -z -M` in the
    repository: no API and no cap on the number of files. With more than one merge base (criss-cross merges), the
    changes against each one, together: more paths, never fewer. A rename counts under both paths (review_judge)."""
    r = checkout.git('merge-base', '--all', base, head)
    bases = r.stdout.split() if r.returncode == 0 else []
    if not bases or not all(SHA.fullmatch(b) for b in bases):
        said = (r.stderr or r.stdout or '').strip()[:200]
        return None, (f'the base {base[:12]} and the head {head[:12]} have no merge base (the base was force-pushed, '
                      f'or the histories are unrelated){": " + said if said else ""}')
    changes = []
    for b in bases:
        # A file name need not be UTF-8: its bytes stay as lone surrogates, so the path is judged by its folder like any
        # other (review_judge.shown writes them as \xNN), never a UnicodeDecodeError that fails the head unexplained.
        d = checkout.git('diff', '--no-ext-diff', '--no-textconv', '--no-relative', '--ignore-submodules=none',
                         '--no-color', '--name-status', '-z', '-M', b, head, '--', errors='surrogateescape')
        if d.returncode != 0:   # stderr as \xNN too (backslashreplace): a lone surrogate cannot be printed as UTF-8
            return None, f'git diff {b[:12]} {head[:12]} failed: {review_judge.shown((d.stderr or "").strip()[:200])}'
        try:
            changes += review_judge.parse_name_status(d.stdout)
        except review_judge.JudgeError as e:
            return None, str(e)
    return changes, None


def path_judgement(checkout, rules, config_path):
    """(review_judge.Judgement, None) of the head's changed paths under the base's `review` rules, or (None, why)."""
    parents = checkout.merge_parents()
    if not parents:
        return None, "HEAD is not GitHub's two-parent merge commit, so the changed paths are unknown"
    changes, why = changed_paths(checkout, *parents)
    if changes is None:
        return None, why
    try:
        return review_judge.judge(rules, changes, config_path), None
    except review_judge.JudgeError as e:
        return None, str(e)


def path_row(mode, judgement, warnings=()):
    """The summary row of the path rules: what they decide (enforce) or would have said (shadow), and the config check's
    warnings (shadow only: enforce refuses them). Never a verdict of its own: the required lens rows and the reason
    carry that."""
    said = ''.join(f'; config warning: {w}' for w in warnings)
    if mode == 'enforce':
        return ('path rules (enforce)', True, f'the path rules decide: {judgement.text()}; the required-lenses input '
                f'is required as well{said}')
    return ('path rules (shadow)', True, f'the path rules would have said: {judgement.text()}; the required-lenses '
            f'input decides (mode shadow){said}')


def decide(repo, n, config_path, checkout, required, pass_drafts, gh, sleep, out, problems):
    """The one rule path of both modes: (code, ok, reason, rows). code is REFUSED (every problem printed at once, before
    GitHub is read), 1 for a failed head (or a pull request GitHub would not give), 0 for a pass. With a `review` block
    in the base's config, the path rules are judged first: a change they cannot judge is REFUSED (fail closed, in
    either mode), and in `enforce` their lenses join `required`."""
    cfg, paths = None, None
    if not problems:
        cfg = base_config(checkout, config_path, problems)
    if cfg is not None:
        why = merged_config_problem(checkout, config_path)
        if why:
            problems.append(why)
    if cfg is not None and cfg.review is not None:
        paths, why = path_judgement(checkout, cfg.review, config_path)
        if paths is None:
            problems.append(f'the review path rules cannot judge this head, so it is not passed: {why}')
    if problems:
        out('::error::head check refused to start: ' + escape('; '.join(problems)))
        return REFUSED, False, '; '.join(problems), []
    enforced = False
    if paths is not None:
        row = path_row(cfg.review.mode, paths, cfg.review.warnings)
        for w in cfg.review.warnings:
            out('::warning::the review rules of the config: ' + escape(w))
        out(f'{row[0]}: {escape(row[2])}')
        if cfg.review.mode == 'enforce':
            enforced = True
            required = dict(required or {})
            for name in paths.lenses:
                required.setdefault(lens.lens_key(name), name)
    pr, why = read_pr(repo, n, gh, sleep)
    if pr is None:
        return 1, False, f'could not read the pull request from GitHub after {ATTEMPTS} tries: {why}', []
    review = lens.Review(cfg.bot_logins, cfg.override_prefix, cfg.override_association, cfg.lens_author_associations,
                         log=lambda text: out(escape(text)))
    rounds = None
    if paths is not None and not (pr['isDraft'] and pass_drafts):
        rounds, why = review_rounds(cfg.review, review, pr, checkout, cfg.override_prefix)
        if rounds is None:   # fail closed, in shadow as in enforce, as for the path rules
            return 1, False, f'the review rounds and findings cannot be judged, so the head is not passed: {why}', [row]
        if enforced and rounds.approach:
            required.setdefault(lens.lens_key(cfg.review.rounds.approach_lens), cfg.review.rounds.approach_lens)
    ok, reason, rows = verdict(pr, review, checkout, required, pass_drafts, enforced)
    if rounds is not None:
        mode = cfg.review.mode
        rows = [(f'findings and rounds ({mode})', not (enforced and rounds.problems),
                 ('' if enforced else 'they would have said: ') + rounds.text(cfg.review))] + rows
        out(f'findings and rounds ({mode}): {escape(rows[0][2])}')
        if enforced and rounds.problems:
            reason = '; '.join(rounds.problems) + ('' if ok else f'; and: {reason}')
            ok = False
    if paths is not None:
        rows = [row] + rows
    return (0 if ok else 1), ok, reason, rows


def own_change(checkout, base):
    """change_of for review_judge.review_round: what the pull request itself changes at a read commit, so two reads of
    the same change are one round. The key is a hash of `git diff -U0` from the commit's merge base with `base` to it,
    hunk line numbers dropped: a head that only merges the base in (a queue or branch update) has the same own change
    as the head it updates, and is no new round. The `index <blob>..<blob>` lines and the whole hunk header (line
    numbers, and the function name git shows) are dropped too: they move when the base edits other lines of a file the
    pull request changes; --binary keeps a binary file's content in the key. A short sha is resolved when it names one
    commit. A commit that is not in the repository and cannot be fetched, more than one merge base or a failed diff: a
    round of its own (unknown counts more rounds, never fewer)."""
    cache = {}

    def of(sha):
        if sha not in cache:
            cache[sha] = diff_key(checkout, base, sha) or ('unknown', sha)
        return cache[sha]
    return of


def diff_key(checkout, base, sha):
    if lens.FULL_SHA.fullmatch(sha):
        if not (checkout.has(sha) or checkout.fetch(sha)):
            return None
        full = sha
    else:
        r = checkout.git('rev-parse', '-q', '--verify', '--end-of-options', sha + '^{commit}')
        full = r.stdout.strip() if r.returncode == 0 else ''
        if not SHA.fullmatch(full):
            return None
    r = checkout.git('merge-base', '--all', base, full)
    bases = r.stdout.split() if r.returncode == 0 else []
    if len(bases) != 1 or not SHA.fullmatch(bases[0]):
        return None
    d = checkout.git('diff', '--no-ext-diff', '--no-textconv', '--no-relative', '--ignore-submodules=none',
                     '--no-color', '--no-renames', '--binary', '-U0', bases[0], full, '--',
                     errors='surrogateescape')
    if d.returncode != 0:
        return None
    text = re.sub(r'^index .*\n', '', re.sub(r'^@@ .*', '@@', d.stdout, flags=re.M), flags=re.M)
    return ('change', hashlib.sha256(text.encode('utf-8', 'surrogateescape')).hexdigest())


def review_rounds(rules, review, pr, checkout, prefix):
    """(review_judge.Rounds, None) of the pull request's lens comments under the base's `review` rules, or (None, why).
    The round counts every lens header the check reads now (lens_scan's history: old, edited and hidden ones too;
    a deleted comment is gone from GitHub, so only the round marker of a later header keeps it counted), each read
    change once (own_change). Each lens's newest entry is judged as review_judge says (REPORT ...): a person's override
    (an override comment by a login in review.override_authors that vouches for this head: its sha or that change
    merged with the base) newer than the entry clears it; any other override clears no finding. Any error is a why,
    never a pass."""
    try:
        head = str(pr.get('headRefOid') or '').lower()
        parents = checkout.merge_parents()
        if not parents:
            return None, "HEAD is not GitHub's two-parent merge commit, so the rounds are unknown"
        entries, history = review.lens_scan(pr)
        people = {a.lower() for a in rules.override_authors}
        for _, _, post in review.lens_posts(pr):   # listed, but GitHub records another association: it never counts
            login = (post.get('author') or {}).get('login') or ''
            if login.lower() in people and review.override_re.match(post.get('body') or '') \
                    and post.get('authorAssociation') != review.override_association:
                review.log(f'warning: the override comment by {login} does not count: GitHub records its author '
                           f'association as {post.get("authorAssociation") or "none"}, and an override counts only '
                           f'with {review.override_association} (override.author_association); being listed in '
                           f'review.override_authors does not change that')
        overrides = [((when, i), sha, post) for when, i, post in review.lens_posts(pr)
                     for sha in [review.override_sha(post)] if sha]
        newest = max((at for at, _, _ in overrides), default=None)
        person = max((at for at, sha, post in overrides
                      if ((post.get('author') or {}).get('login') or '').lower() in people
                      and (sha == head or same_change(checkout, head, sha)[0])), default=None)
        reports = {}
        for key, (at, sha, _, post) in entries.items():
            if person is not None and at < person:
                how = review_judge.CLEARED
            elif key.startswith(lens.NO_HEADER):
                how = review_judge.LOOSE if newest is not None and at < newest else review_judge.NOT_JUDGED
            elif lens.FULL_SHA.fullmatch(sha or ''):
                how = review_judge.REPORT
            else:
                how = review_judge.UNREAD if newest is not None and at < newest else review_judge.NOT_JUDGED
            reports[key] = (how, post.get('body') or '')
        decided = person is not None and all(at < person for at, _, _, _ in entries.values())
        return review_judge.judge_rounds(rules, history, reports, decided, own_change(checkout, parents[0]),
                                         prefix), None
    except Exception as e:   # noqa: BLE001 - unknown never passes
        return None, f'{type(e).__name__}: {review_judge.shown(str(e))[:300]}'


def argument_problems(repo, n, required_text):
    """(required, problems) for the arguments both modes take."""
    problems = []
    if not pr_queue.REPO_RE.fullmatch(repo):
        problems.append(f'--repo {repo!r} is not owner/name')
    if not re.fullmatch(r'[1-9][0-9]{0,9}', n):
        problems.append(f'--pr {n!r} is not a pull request number (run it on a pull_request event)')
    required, bad = required_names(required_text)
    return required, problems + bad


def cli(argv=None, env=None, gh=gh_run, sleep=time.sleep, git=None, out=print):
    env = os.environ if env is None else env
    p = argparse.ArgumentParser(prog='head_check.py', description=__doc__.split('\n')[0])
    p.add_argument('--repo', required=True, help='owner/name')
    p.add_argument('--pr', required=True, help='the pull request number')
    p.add_argument('--config', required=True, help="the merge queue's JSON config (../pr-queue/README.md, Config): a "
                   'path in the repository, read from the base commit the merge commit tests')
    p.add_argument('--path', default='.', help="the checkout of GitHub's merge commit (fetch-depth: 0); with --bare, "
                   'the bare repository holding --base and --head')
    p.add_argument('--required-lenses', default='', help='lens names, one per line, that must read the head')
    p.add_argument('--pass-drafts', action='store_true', help='pass a draft without reading its review')
    p.add_argument('--bare', action='store_true', help='read a bare repository (head_gate.py) instead of a checkout')
    p.add_argument('--base', default='', help='with --bare: the base tip the config is read from')
    p.add_argument('--head', default='', help='with --bare: the head GitHub named when the base was read')
    p.add_argument('--remote', help='with --bare: where a missing read commit is fetched from (default origin): a '
                   'remote name, a path or a URL; not plain http, not empty')
    a = p.parse_args(argv)
    required, problems = argument_problems(a.repo, a.pr, a.required_lenses)
    bare = None
    if a.bare:
        bare = (a.base.lower(), a.head.lower())
        problems += [f'--{k} {v!r} is not a full commit sha' for k, v in zip(('base', 'head'), bare)
                     if not SHA.fullmatch(v)]
    elif a.base or a.head:
        problems.append('--base and --head need --bare (a checkout names its own through its merge commit)')
    if a.remote is not None and not a.bare:
        problems.append("--remote needs --bare (a checkout fetches from its own origin)")
    elif a.remote is not None and (why := pr_queue.remote_problem(a.remote)):   # an empty one is no "origin"
        problems.append(f'--remote: {why}')
    checkout = Checkout(a.path, git, bare=bare, remote='origin' if a.remote is None else a.remote)
    if git is None:
        problems += filter(None, [pr_queue.git_problem(), checkout.problem()])
    code, ok, reason, rows = decide(a.repo, a.pr, a.config, checkout, required, a.pass_drafts, gh, sleep, out, problems)
    if code != REFUSED:
        report(ok, reason, rows, a.pr, env, out)
    return code


# Caps on a config a pull request writes, checked before it is parsed: the rule order check searches every pair of
# globs (review_config._shadowed), so its work grows with the square of the glob characters. Measured: a 20 KB config
# of two long globs took 2.9 GB, 100 rules took 15.6 s. Under these caps the worst shapes take about a second
# (head_check.test.py, HostileConfig). The base's own config is never capped: a pull request that leaves it as it is
# is not read again (merged_config_problem).
MAX_CONFIG_BYTES = 32 * 1024
MAX_GLOB_LENGTH = 128   # characters of one glob in review.rules[].paths
MAX_GLOB_TOTAL = 2048   # characters of all of them together
# The queue reads the config only from a regular file's blob (pr_queue.committed_config). git keeps a symlink (120000)
# as a blob of its target text, and a submodule (160000) can name any object, so the object alone says nothing.
FILE_MODES = ('100644', '100755')
MERGE_TREE_SECONDS = 300   # a merge that takes longer is refused; the gate's job has 15 minutes in all
LOCKS = ('the base config would refuse every pull request after it, the one that fixes it too, and the queue would '
         'stop')


def repo_path(path):
    """The config's path in a tree: `git show rev:./x` resolves against the work tree, and a bare repository has
    none."""
    return path[2:] if path.startswith('./') else path


def base_config(checkout, path, problems):
    """The queue's config as the base commit holds it (the merge commit's first parent), never the pull request's own
    copy: a pull request cannot widen whose comments count for its own check. None with a problem added."""
    parents = checkout.merge_parents()
    if not parents:
        problems.append("HEAD is not GitHub's two-parent merge commit (check out the pull_request event's default ref)")
        return None
    # Bare, the base is the tip the gate read from GitHub's API, fetched by sha: what the pull request's branch says
    # about its own config is never read.
    r = checkout.git('show', f'{parents[0]}:{repo_path(path)}', errors='surrogateescape')
    if r.returncode != 0:
        problems.append(f'the base commit {parents[0][:12]} has no config {path}')
        return None
    try:   # as bytes: config.parse refuses one that is not UTF-8, as the queue does
        return config.parse(r.stdout.encode('utf-8', 'surrogateescape'), f'{path} at the base {parents[0][:12]}')
    except config.ConfigError as e:
        problems.append(str(e))
        return None


def merged_config_problem(checkout, path):
    """Why the config this pull request would merge must not merge, or None. The config that judges a head is the
    base's, so a head is judged by the old config and could merge a new one that does not parse (or is in enforce with
    a rule order enforce refuses, or is deleted); after it, the base config refuses every head, the one that fixes it
    too, and the queue stops. So the config the merge leaves must parse (config.parse), under the caps above, and stay
    a regular file (FILE_MODES: the queue reads no other). It is the merge's, never the head's own copy: two
    pull requests that each leave a good config can merge into a bad one, and an older head's stale copy says nothing
    once an owner fixed the base. Checkout mode reads GitHub's merge commit
    (HEAD), bare mode `git merge-tree` of the base and the head (a head that does not merge cleanly is refused: the
    merged config is unknown, and GitHub cannot merge it either). The merged config is never used for anything else:
    the rules that judge the head stay the base's. Any exception is a refusal that says why, never a crash or a pass."""
    parents = checkout.merge_parents()
    if not parents:
        return None   # base_config refused it already
    try:
        return _merged_config_problem(checkout, path, *parents)
    except Exception as e:   # unknown never passes
        return (f'the config {path} this pull request would merge could not be checked, so it is not passed: '
                f'{type(e).__name__}: {review_judge.shown(str(e))[:300]}')


def tree_entry(checkout, tree, rel):
    """(mode, type, oid) of the entry `rel` in `tree` as git ls-tree lists it, or None when there is none. Raises
    ValueError when git fails or lists anything but that one entry (the caller refuses)."""
    r = checkout.git('ls-tree', '-z', tree, '--', rel, errors='surrogateescape')
    records = [x for x in (r.stdout or '').split('\0') if x] if r.returncode == 0 else None
    if records == []:
        return None
    meta, _, name = records[0].partition('\t') if records else ('', '', '')
    fields = meta.split(' ')
    if len(records or ()) != 1 or name != rel or len(fields) != 3 or not re.fullmatch(r'[0-7]{6}', fields[0]) \
            or not SHA.fullmatch(fields[2]):
        said = review_judge.shown((r.stderr if r.returncode else r.stdout or '').strip()[:200])
        raise ValueError(f'git ls-tree {tree[:12]} -- {rel} did not list one entry: {said}')
    return tuple(fields)


def folded(name):
    """`name` as a case-insensitive file system compares it: fully case folded (`ſ` is `s`, `ß` is `ss`), NFC and
    NFD as one (APFS, HFS+). NTFS compares with its own upcase table, which may pair names this keeps apart (`ı`, `İ`
    and `i`): out of scope, like HFS+'s ignorable code points."""
    return unicodedata.normalize('NFD', unicodedata.normalize('NFD', name).casefold())


def case_matches(checkout, tree, rel):
    """The paths in `tree` that name `rel` or one of its folders ignoring case (folded), `rel` itself too: at each level
    the entries of any type whose name folds to the same as `rel`'s there (a file, a symlink or a submodule next to the
    folder collides with it too); only folders are read on. Raises ValueError when git fails (the caller refuses)."""
    parts = [folded(p) for p in rel.split('/')]
    level, matches = [('', tree)], set()
    for i, want in enumerate(parts):
        found = []
        for prefix, oid in level:
            r = checkout.git('ls-tree', '-z', oid, errors='surrogateescape')
            if r.returncode != 0:
                said = review_judge.shown((r.stderr or '').strip()[:200])
                raise ValueError(f'git ls-tree {oid[:12]} failed: {said}')
            for record in filter(None, (r.stdout or '').split('\0')):
                meta, _, name = record.partition('\t')
                fields = meta.split(' ')
                if folded(name) == want:
                    if i < len(parts) - 1 and fields[1:2] == ['tree']:
                        found.append((prefix + name + '/', fields[-1]))
                    else:
                        matches.add(prefix + name)
        level = found
    return matches


def _merged_config_problem(checkout, path, base, head):
    rel = repo_path(path)
    merged = 'HEAD'
    if checkout.bare:
        try:
            m = pr_queue.merge_tree(checkout.git, base, head, errors='surrogateescape', timeout=MERGE_TREE_SECONDS)
        except subprocess.TimeoutExpired:
            return (f'git merge-tree of the base {base[:12]} and the head {head[:12]} took over {MERGE_TREE_SECONDS} '
                    'seconds, so the config it would merge is unknown')
        tree = (m.stdout or '').split()[:1]
        if m.returncode == 1:
            return (f'the head {head[:12]} does not merge cleanly into the base {base[:12]}, so the config it would '
                    'merge is unknown: merge the base into the branch and resolve the conflict')
        if m.returncode != 0 or not tree or not SHA.fullmatch(tree[0]):
            said = review_judge.shown((m.stderr or m.stdout or '').strip()[:200])
            return f'git merge-tree of the base {base[:12]} and the head {head[:12]} failed: {said}'
        merged = tree[0]
    new = tree_entry(checkout, merged, rel)
    if new is None:
        return (f'merged, this pull request deletes {path}: {LOCKS}. To stop using the queue, take its checks out of '
                'the branch ruleset first')
    mode, kind, oid = new
    if kind == 'tree':
        return f'merged, this pull request leaves {path} as a tree, not a file: {LOCKS}'
    if mode not in FILE_MODES or kind != 'blob':   # even when the base holds the same: unknown never passes
        what = {'120000': 'symlink', '160000': 'submodule'}.get(mode) or review_judge.shown(kind[:20])
        return (f'merged, this pull request leaves {path} as a {what} (mode {mode}), not a file (mode 100644 or '
                '100755): the queue reads only a regular file\'s config and would stop. Make it a file again')
    added = sorted(case_matches(checkout, merged, rel) - case_matches(checkout, base, rel))
    if added:   # checked before the skip below: a twin leaves the config's own entry as it was
        return (f'merged, this pull request adds {review_judge.shown(added[0])[:200]}, which differs from {path} only '
                'in case: a checkout on a case-insensitive file system (macOS, Windows) keeps one of the two, and '
                'which one is up to git. Rename or delete it')
    if tree_entry(checkout, base, rel) == new:
        return None   # the merge leaves the base's own config, mode and text, which base_config parsed
    size = checkout.git('cat-file', '-s', oid).stdout.strip()
    if not size.isdigit() or int(size) > MAX_CONFIG_BYTES:
        return config_size_problem(path, size)
    r = checkout.git('cat-file', 'blob', oid)
    if r.returncode != 0:
        said = review_judge.shown(r.stderr or '')[:200]
        return f'the config {path} this pull request would merge could not be read: {said}'
    return config_text_problem(r.stdout, path)


def config_size_problem(path, size):
    return (f'merged, this pull request leaves {path} at {size} bytes, over the {MAX_CONFIG_BYTES} bytes the check '
            'reads of a config a pull request writes: keep it smaller')


def glob_cap_problem(raw):
    """Why the raw config's review globs are over the caps, or None. Only what config.parse would search is counted;
    anything of another shape is left to config.parse, which refuses it without a search."""
    block = raw.get('review') if isinstance(raw, dict) else None
    rules = block.get('rules') if isinstance(block, dict) else None
    if not isinstance(rules, list):
        return None
    total = 0
    for i, rule in enumerate(rules):
        globs = rule.get('paths') if isinstance(rule, dict) else None
        for g in globs if isinstance(globs, list) else ():
            if isinstance(g, str):
                if len(g) > MAX_GLOB_LENGTH:
                    return (f'rules[{i}].paths has a glob of {len(g)} characters, over the {MAX_GLOB_LENGTH} the check '
                            'reads in a config a pull request writes')
                total += len(g)
    if total > MAX_GLOB_TOTAL:
        return (f'globs of {total} characters in all, over the {MAX_GLOB_TOTAL} the check reads in a config a pull '
                'request writes')
    return None


def config_text_problem(text, path):
    """Why the config text a merge would leave must not merge, or None: over the caps, or it does not parse
    (config.parse: an enforce block with a rule order enforce refuses is one such). Any exception is a refusal."""
    try:
        size = len(text.encode('utf-8', 'surrogateescape'))
        if size > MAX_CONFIG_BYTES:
            return config_size_problem(path, size)
        try:
            raw = json.loads(text)
        except ValueError:
            raw = None   # config.parse names it
        why = glob_cap_problem(raw)
        if why:
            return f'merged, this pull request leaves {path} with {why}: keep the rules shorter'
        try:
            config.parse(text, f'{path} after the merge')
        except config.ConfigError as e:
            block = raw.get('review') if isinstance(raw, dict) else None
            order = (review_config.enforce_order_problems(block)
                     if isinstance(block, dict) and block.get('mode') == 'enforce' else [])
            if order:
                return (f'merged, this pull request leaves {path} in mode enforce with a rule order enforce refuses: '
                        f'{"; ".join(order)}. {LOCKS[0].upper()}{LOCKS[1:]}: fix the rule order first (or keep mode '
                        'shadow until it is fixed)')
            return (f'merged, this pull request leaves {path} a config that does not parse: {e}. Then {LOCKS}: fix '
                    'it first')
        return None
    except Exception as e:   # RecursionError, MemoryError, ...: unknown never passes
        return (f'the config {path} this pull request would merge could not be checked, so it is not passed: '
                f'{type(e).__name__}: {review_judge.shown(str(e))[:300]}')


if __name__ == '__main__':
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(cli())
