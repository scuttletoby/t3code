#!/usr/bin/env python3
"""Is this pull request's head the change its review read? The pr-queue's `head_check` as a required check (README.md).

The merge queue (../pr-queue) re-arms auto-merge only for a head that holds exactly the change the lenses read, but it
only touches pull requests that are behind their base. A plain push to an armed pull request that is already up to
date would merge unread; this check, required by the branch ruleset, fails that head. It reads the review with the
queue's own rules, imported, never copied:
  - lens.Review.reviewed_head and lens_reads: the one full 40-hex sha every lens's newest read names, or a newer
    override comment's. Only comments of the config's lens_author_associations count (bots never); an edited or
    hidden lens comment, a short sha and a comment that only looks like a lens report read nothing.
  - pr_queue.holds: the head is that commit, or a clean merge of the base into it, byte for byte (a queue update).
The config is the queue's own JSON file (config.py), read from the base commit, so the check and the queue read the
same people and override, and a pull request cannot change them for its own check.
`--required-lenses` (the project's, per pull request if it likes) adds lenses that must be among the reads.

Pass: every lens read the head (or a newer override vouches for it), and every required lens is there; a pull
request with no lens comments and no required lens (nothing to compare); a draft, with --pass-drafts only. Fail,
never pass, when anything is unknown: GitHub unreadable after ATTEMPTS, a malformed answer, a checkout that is not
GitHub's two-parent merge commit of the head GitHub reports, a shallow or tampered checkout, a read commit neither in
the checkout nor on GitHub. Reads only (a fetch of a missing read commit into the job's own checkout aside) and posts
nothing: a re-run on the same head gives the same answer, and a run stopped half-way leaves nothing behind.
Exit codes: 0 pass, 1 fail, 2 refused to start (a config or argument error; every reason printed at once).
    head_check.py --repo OWNER/NAME --pr N --config FILE [--path DIR] [--required-lenses TEXT] [--pass-drafts]
"""
import argparse, json, os, re, subprocess, sys, tempfile, time

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pr-queue'))
import config  # noqa: E402
import lens  # noqa: E402
import pr_queue  # noqa: E402

ATTEMPTS, RETRY_SECONDS = 3, 10
FIELDS = 'isDraft,headRefOid,comments,reviews'
SHA = pr_queue.SHA
REFUSED = pr_queue.REFUSED
# Every git call: the queue's safe flags (no hooks, fsmonitor, commit-graph or replace refs) and no attributes file. git
# runs in an empty folder with --git-dir, so the checkout's own .gitattributes (the pull request's) cannot change how
# merge-tree merges (merge=union would make a conflict "clean").
GIT_SAFE = (*pr_queue.GIT_SAFE, '-c', 'core.attributesFile=/dev/null')


class Checkout:
    """The job's checkout of GitHub's merge commit, read with git (`run` replaces git in tests)."""

    def __init__(self, path, run=None):
        self.git_dir = os.path.join(os.path.abspath(path), '.git')
        self.run = run

    def git(self, *a):
        if self.run:
            return self.run(*a)
        with tempfile.TemporaryDirectory(prefix='head-check-') as empty:
            return subprocess.run(['git', *GIT_SAFE, '--git-dir', self.git_dir, *a], cwd=empty, text=True,
                                  capture_output=True, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0',
                                                            'GIT_ATTR_NOSYSTEM': '1'})

    def problem(self):
        """Why the checkout cannot be read as git made it, or None: not a .git folder, shallow (the base's history is
        needed), or holding a file that changes what its objects or merges say (pr_queue.UNTRUSTED_FILES)."""
        if not os.path.isdir(self.git_dir):
            return f'{self.git_dir} is no git folder: check out the pull request first (actions/checkout)'
        if os.path.lexists(os.path.join(self.git_dir, 'shallow')):
            return 'the checkout is shallow: check it out with fetch-depth: 0'
        bad = [f for f in pr_queue.UNTRUSTED_FILES if os.path.lexists(os.path.join(self.git_dir, f))]
        return f'the checkout holds {", ".join(bad)}, which a checkout never writes' if bad else None

    def merge_parents(self):
        """(base, head) of HEAD when it is a two-parent merge commit, else None."""
        r = self.git('rev-list', '--parents', '-n', '1', 'HEAD')
        line = r.stdout.split() if r.returncode == 0 else []
        return (line[1], line[2]) if len(line) == 3 and all(SHA.fullmatch(s) for s in line) else None

    def has(self, sha):
        return self.git('cat-file', '-e', sha + '^{commit}').returncode == 0

    def fetch(self, sha):
        """A read commit no ref of the checkout reaches (a force-push dropped it; GitHub keeps it), from origin."""
        return self.git('fetch', '-q', '--no-tags', 'origin', sha).returncode == 0 and self.has(sha)


def required_names(text):
    """{lens key: name as written} from one lens name per line or comma; (keys, problems)."""
    names, problems = {}, []
    for name in (n.strip() for n in re.split(r'[\n,]', text or '')):
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
    """(the PR, None) or (None, why it could not be read), after ATTEMPTS tries."""
    why = ''
    for attempt in range(ATTEMPTS):
        if attempt:
            sleep(RETRY_SECONDS)
        r = gh('pr', 'view', str(n), '--repo', repo, '--json', FIELDS)
        try:
            pr = json.loads(r.stdout) if r.returncode == 0 else None
        except ValueError:
            pr = None
        if well_formed(pr):
            return pr, None
        why = (r.stderr or r.stdout or '').strip()[:300] or 'gh gave no usable answer'
    return None, why


def same_change(checkout, head, read):
    """(True, how) when the checkout's merge commit merges `head` and head holds `read`'s change (pr_queue.holds),
    else (False, why). Only the checkout's own commits and GitHub's (a fetch of a missing read) are looked at."""
    parents = checkout.merge_parents()
    if not parents:
        return False, "HEAD is not GitHub's two-parent merge commit, so the pull request head is unknown"
    base, merged = parents
    if merged != head:
        return False, (f'the checkout merges {merged[:12]} but GitHub says the head is {head[:12]}: '
                       'a newer head has its own run')
    if head == read:
        return True, f'{head[:12]} is the commit read'
    if not (checkout.has(read) or checkout.fetch(read)):
        return False, f'the read commit {read[:12]} is neither in the checkout nor on GitHub'
    if pr_queue.holds(checkout.git, head, read, pr_queue.UPDATES, base):
        return True, f'{head[:12]} is {read[:12]} merged with the base, nothing else'
    return False, f'{head[:12]} is not the change read at {read[:12]}, nor that change merged with the base'


def verdict(pr, review, checkout, required=None, pass_drafts=False):
    """(ok, reason, rows): rows is [(lens, ok, short reason)] per lens, required ones first."""
    required = required or {}
    head = str(pr.get('headRefOid') or '').lower()
    if not SHA.fullmatch(head):
        return False, 'GitHub names no head commit for the pull request', []
    if pr['isDraft'] and pass_drafts:
        return True, 'a draft: checked again when it is marked ready for review', []
    reads = review.lens_reads(pr)
    reviewed, read, why = review.reviewed_head(pr)
    missing = [name for key, name in required.items() if key not in reads]
    if not reads and not required:
        return True, 'no lens comments and no required lens: nothing to compare', []
    ok, how = same_change(checkout, head, read) if reviewed and read else (False, None)
    rows = [(name, False, 'required: no lens comment of a counted author') for name in missing]
    for key, (sha, gap, override) in sorted(reads.items()):
        if not sha:
            text = f'reads nothing: {gap}'
        elif not read:
            text = f'read {sha[:12]}; the lenses did not all read one head'
        else:
            text = f'{"override " if override else "read "}{sha[:12]}: {how}'
        rows.append(((f'{key} (required)' if key in required else key), bool(sha and read and ok), text))
    if missing:
        return False, f'required lens without a read: {", ".join(missing)}', rows
    if not read:
        return False, why, rows
    return ok, how, rows


def escape(text):
    """One line for a workflow command (::error::) or the log: nothing in a comment can start a command."""
    return str(text).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def cell(text):
    return re.sub(r'[|<>`\r\n]', ' ', str(text))


def report(ok, reason, rows, n, env, out=print):
    """The log, the step summary (one table, appended) and the `result` output."""
    for name, good, text in rows:
        out(f'  {"ok  " if good else "FAIL"} {escape(name)}: {escape(text)}')
    out(f'#{n}: head check {"passed" if ok else "FAILED"}: {escape(reason)}' if ok else f'::error::#{n}: head check '
        f'failed: {escape(reason)}')
    summary = env.get('GITHUB_STEP_SUMMARY')
    if summary:
        lines = [f'### Head check #{n}: {"pass" if ok else "fail"}', '', cell(reason), '']
        if rows:
            lines += ['| lens | | reason |', '| --- | --- | --- |']
            lines += [f'| {cell(name)} | {"ok" if good else "fail"} | {cell(text)} |' for name, good, text in rows]
        with open(summary, 'a', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
    output = env.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a', encoding='utf-8') as f:
            f.write(f'result={"pass" if ok else "fail"}\n')


def gh_run(*a):
    return subprocess.run(['gh', *a], text=True, capture_output=True)


def cli(argv=None, env=None, gh=gh_run, sleep=time.sleep, git=None, out=print):
    env = os.environ if env is None else env
    p = argparse.ArgumentParser(prog='head_check.py', description=__doc__.split('\n')[0])
    p.add_argument('--repo', required=True, help='owner/name')
    p.add_argument('--pr', required=True, help='the pull request number')
    p.add_argument('--config', required=True, help="the merge queue's JSON config (../pr-queue/README.md, Config): a "
                   'path in the repository, read from the base commit the merge commit tests')
    p.add_argument('--path', default='.', help="the checkout of GitHub's merge commit (fetch-depth: 0)")
    p.add_argument('--required-lenses', default='', help='lens names, one per line or comma, that must read the head')
    p.add_argument('--pass-drafts', action='store_true', help='pass a draft without reading its review')
    a = p.parse_args(argv)
    problems = []
    if not pr_queue.REPO_RE.fullmatch(a.repo):
        problems.append(f'--repo {a.repo!r} is not owner/name')
    if not re.fullmatch(r'[1-9][0-9]{0,9}', a.pr):
        problems.append(f'--pr {a.pr!r} is not a pull request number (run it on a pull_request event)')
    required, bad = required_names(a.required_lenses)
    problems += bad
    checkout = Checkout(a.path, git)
    if git is None:
        problems += filter(None, [pr_queue.git_problem(), checkout.problem()])
    cfg = None
    if not problems:
        cfg = base_config(checkout, a.config, problems)
    if problems:
        out('::error::head check refused to start: ' + escape('; '.join(problems)))
        return REFUSED
    pr, why = read_pr(a.repo, a.pr, gh, sleep)
    if pr is None:
        report(False, f'could not read the pull request from GitHub after {ATTEMPTS} tries: {why}', [], a.pr, env, out)
        return 1
    review = lens.Review(cfg.bot_logins, cfg.override_prefix, cfg.override_association, cfg.lens_author_associations,
                         log=lambda text: out(escape(text)))
    ok, reason, rows = verdict(pr, review, checkout, required, a.pass_drafts)
    report(ok, reason, rows, a.pr, env, out)
    return 0 if ok else 1


def base_config(checkout, path, problems):
    """The queue's config as the base commit holds it (the merge commit's first parent), never the pull request's own
    copy: a pull request cannot widen whose comments count for its own check. None with a problem added."""
    parents = checkout.merge_parents()
    if not parents:
        problems.append("HEAD is not GitHub's two-parent merge commit (check out the pull_request event's default ref)")
        return None
    rel = path[2:] if path.startswith('./') else path
    r = checkout.git('show', f'{parents[0]}:{rel}')
    if r.returncode != 0:
        problems.append(f'the base commit {parents[0][:12]} has no config {path}')
        return None
    try:
        return config.parse(r.stdout, f'{path} at the base {parents[0][:12]}')
    except config.ConfigError as e:
        problems.append(str(e))
        return None


if __name__ == '__main__':
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(cli())
