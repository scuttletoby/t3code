#!/usr/bin/env python3
"""Notes which base commit a pull_request run tests, for pr_queue.py's update_when_base_was_red (../README.md).

A pull_request run tests GitHub's merge commit of the PR head into its base, and that commit's first parent is the
base commit the run tested. That stays true for a re-run (it tests the same merge again) and for a job that started
late, which a clock would get wrong. The note is an annotation on the job, `::notice title=pr_queue tested base::<sha>`,
which the queue reads.

It is written only when the checkout is exactly that merge commit: HEAD is GITHUB_SHA, a commit with two parents whose
second is the event's PR head. Anything else (another checkout, no checkout, an odd event file) writes a warning and no
note, so the queue reads the base as unknown and treats a red as the PR's own; other events write one plain line. It
always exits 0: a missing note can only cost an update, never cause a wrong one.
    tested_base.py [--path DIR]
"""
import argparse, json, os, re, subprocess, sys

TITLE = 'pr_queue tested base'   # pr_queue.TESTED_BASE (tested_base.test.py pins the two together)
SHA = re.compile(r'[0-9a-f]{40}')


def git(path, *a):
    r = subprocess.run(['git', '-C', path, *a], text=True, capture_output=True)
    return r.stdout.strip() if r.returncode == 0 else None


def problem(env, path):
    """(None, base) when the checkout at `path` is the run's merge commit, else (why not, None)."""
    try:
        with open(env.get('GITHUB_EVENT_PATH') or '', encoding='utf-8') as f:
            head = json.load(f)['pull_request']['head']['sha']
    except (OSError, ValueError, KeyError, TypeError):
        head = None
    sha = env.get('GITHUB_SHA') or ''
    if not SHA.fullmatch(head or ''):
        return 'the event file names no pull request head', None
    if not SHA.fullmatch(sha):
        return f'GITHUB_SHA {sha[:40]!r} is no commit', None
    checked_out = git(path, 'rev-parse', '--verify', '-q', 'HEAD^{commit}')
    if checked_out != sha:
        return f'{path} has {checked_out or "no commit"} checked out, not the run\'s merge commit {sha}', None
    raw = git(path, 'cat-file', 'commit', sha) or ''   # the raw object: its parents even in a shallow clone
    parents = re.findall(r'^parent ([0-9a-f]{40})$', raw.split('\n\n', 1)[0], re.M)
    if len(parents) != 2:
        return f'{sha[:12]} has {len(parents)} parents, so it is no merge of the pull request into its base', None
    if parents[1] != head:
        return f'{sha[:12]} merges {parents[1][:12]}, not the pull request head {head[:12]}', None
    return None, parents[0]


def main(argv=None, env=None):
    env = os.environ if env is None else env
    p = argparse.ArgumentParser(prog='tested_base.py', description='Note the base a pull_request run tests.')
    p.add_argument('--path', default='.', help='where the merge commit is checked out (default: .)')
    a = p.parse_args(argv)
    event = env.get('GITHUB_EVENT_NAME') or ''
    if event != 'pull_request':
        print(f'{TITLE}: a {event or "no"} event, not pull_request: no note (only pull_request runs test a merge)')
        return 0
    why, base = problem(env, a.path)
    if why:
        print(f'::warning title={TITLE} unknown::{why}. pr_queue reads this run\'s base as unknown')
    else:
        print(f'::notice title={TITLE}::{base}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
