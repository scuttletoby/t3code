#!/usr/bin/env python3
"""Mutation report (fork-ci mutation-report; result format, config and usage: README.md next to this file): reads a
run's mutation results, reports each module's kill rate beside its baseline and its last runs, and writes one rolling
GitHub issue with a trend table of the last TREND_KEEP runs, kept in the issue between runs.

  python3 -I mutation_report.py --results DIR --baseline FILE --expect TARGETS [--config FILE] \
      [--issue --repo OWNER/NAME --run-url URL] [--suggest FILE] [--summary FILE] [--trend FILE] [--out FILE]

A kill rate below the baseline is a number to read, never a failure: the report marks it as a drop. Exit 1 only when a
module has no score (did not finish, failed, far fewer planted bugs than its baseline), the baseline is missing,
unreadable or names a module that is no longer a target, the config is bad, GitHub could not be read or written, or
an issue it created is not the config's `author`'s (the token writes as another account: the new issue is closed): a
run that measured nothing must stay loud. The baseline file is only ever changed by a pull request; --suggest writes
this run's numbers in its format. A module without a result this run keeps its last section, marked as not refreshed.
The rolling issue: every issue with the configured title, open or closed, is read and their trends are merged (a run
once, by its URL). The oldest open one is written; with none open, the newest closed one is reopened (closing it never
restarts the trend); any other open one is closed as a copy. Each write is one call, so a run stopped between two of
them leaves state the next run finishes the same way.
With --issue a red run also opens or comments on one issue (red_title: a comment notifies, an edited rolling issue
notifies nobody), and a green run closes it.
"""
import argparse, datetime, glob, json, os, re, subprocess, sys

CONFIG_DEFAULTS = {'title': 'Mutation testing: report', 'red_title': 'mutation run red', 'labels': [],
                   'red_labels': [], 'author': 'app/github-actions'}   # author: the account the token writes as
TITLE, RED_TITLE = CONFIG_DEFAULTS['title'], CONFIG_DEFAULTS['red_title']
MAX_BODY = 60000   # GitHub's limit is 65536
TOP = 8
TREND_KEEP = 26   # runs in the trend table: half a year of weekly runs
TREND_RE = re.compile(r'<!-- mutation-trend (.*?) -->', re.S)
AUTHOR_RE = re.compile(r'(app/)?[A-Za-z0-9][A-Za-z0-9-]{0,38}')   # a login as gh names it; an app's is app/NAME
DATE_RE, COMMIT_RE = re.compile(r'\d{4}-\d{2}-\d{2}'), re.compile(r'[0-9A-Za-z]{1,40}')
SEARCH_LIMIT = 1000   # gh pages through the search; a full last page: the report may lie past it (an error)


def load_results(paths, skipped=None):
    """{module: result}. `skipped` (a dict) receives the test files a run left out ({file: why})."""
    out = {}
    for p in paths:
        for f in sorted(glob.glob(os.path.join(p, '*.json'))) if os.path.isdir(p) else [p]:
            with open(f) as fh:
                data = json.load(fh)
            mods = data.get('modules')
            if not isinstance(mods, list):   # e.g. a --suggest baseline written into the same folder
                continue
            for m in mods:
                out[m['module']] = m
            if skipped is not None:
                skipped.update(data.get('skipped_tests') or {})
    return out


def scored(r):
    """A result counts only with planted bugs and a numeric kill rate: anything else is no score, never a pass."""
    return (isinstance(r.get('kill_rate'), (int, float)) and not isinstance(r.get('kill_rate'), bool)
            and isinstance(r.get('mutants'), int) and r['mutants'] > 0)


def shrunken(r, b):
    """Fewer than half the baseline's planted bugs: a rate over a shrunken module, no score."""
    return isinstance((b or {}).get('mutants'), int) and r['mutants'] < b['mutants'] / 2


def load_baseline(path):
    """(modules dict or None, problem or None)"""
    if not os.path.exists(path):
        return None, f'baseline file `{path}` is missing: record the numbers below with a PR'
    try:
        with open(path) as f:
            mods = json.load(f)['modules']
        bad = [n for n, b in mods.items() if not isinstance(b.get('kill_rate'), (int, float))]
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return None, f'baseline file `{path}` is unreadable ({e})'
    if bad:
        return None, f'baseline file `{path}` has no kill_rate for: {", ".join(bad)}'
    return mods, None


def judge(expected, results, base):
    """[(module, status, note)], problems. status: ok, improved, new, dropped, missing, error. A drop below the
    baseline is a row, not a problem: only a module without a score (or a broken baseline) is one."""
    rows, problems = [], []
    for name in expected:
        r, b = results.get(name), (base or {}).get(name)
        if r is None:
            rows.append((name, 'missing', 'did not finish this run'))
            problems.append(f'{name}: no result (the run stopped before this module finished)')
        elif 'error' in r:
            rows.append((name, 'error', r['error'][:300]))
            problems.append(f'{name}: {r["error"][:300]}')
        elif not scored(r):
            rows.append((name, 'error', 'no score (no planted bugs or no kill rate)'))
            problems.append(f'{name}: no score (mutants {r.get("mutants")!r}, kill rate {r.get("kill_rate")!r})')
        elif b is None:
            rows.append((name, 'new', 'no baseline yet: add it with a PR'))
        elif shrunken(r, b):
            rows.append((name, 'error', f'only {r["mutants"]} planted bugs, the baseline had {b["mutants"]}'))
            problems.append(f'{name}: only {r["mutants"]} planted bugs, the baseline had {b["mutants"]} (code moved out '
                            f'of the target? point targets.json and the baseline at it in a PR)')
        elif r['kill_rate'] < b['kill_rate']:
            rows.append((name, 'dropped', f'below its baseline {b["kill_rate"]}%'))
        elif r['kill_rate'] > b['kill_rate']:
            rows.append((name, 'improved', f'above its baseline {b["kill_rate"]}%: raise it with a PR'))
        else:
            rows.append((name, 'ok', ''))
    for name in sorted(set(base or {}) - set(expected)):
        problems.append(f'{name}: in the baseline but no longer a target (renamed or removed? change the baseline in '
                        f'the same PR)')
    return rows, problems


def section(name, r, max_details, day=''):
    surv = r.get('survivors', [])
    head = f'<!-- module:{name} -->\n#### {name}'
    if 'error' in r:
        lines = [head, f'ERROR: {r["error"]}']
        for part in r.get('finished_parts', []):   # finished work stays useful, but only a whole module has a score
            caught = part['killed'] + part['timeout']
            lines += ['', f'`{part["part"]}`: {caught} of {part["mutants"]} planted bugs caught; survivors:']
            for s in part['survivors'][:max_details]:
                lines.append(f'- {os.path.basename(s.get("file", ""))}:{s["line"]}: {s["what"]} ({s["why"]})')
        lines.append(f'<!-- /module:{name} -->')
        return '\n'.join(lines)
    by_line = {}
    for s in surv:
        by_line.setdefault((s.get('file', r.get('file', '')), s['line']), []).append(s)
    top = sorted(by_line.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:TOP]
    lines = [head, f'{len(surv)} of {r["mutants"]} planted bugs survived in `{r.get("file", name)}`'
             + (f' ({day.strip()} run).' if day else '.')]
    if r.get('timeout'):   # counted as caught
        lines.append(f'{r["timeout"]} of {r["mutants"]} were caught only by a time limit.')
    if top:
        lines += ['', '| Survived | Where | Code |', '|---|---|---|']
        for (f, ln), ss in top:
            code = ss[0]['code'].replace('|', '\\|').replace('`', "'")
            lines.append(f'| {len(ss)} | `{os.path.basename(f)}:{ln}` | `{code[:90]}` |')
    if surv:
        lines += ['', f'<details><summary>All {len(surv)} survivors</summary>', '']
        for s in sorted(surv, key=lambda s: (s.get('file', ''), s['line']))[:max_details]:
            where = f'{os.path.basename(s["file"])}:' if s.get('file') else 'line '
            lines.append(f'- {where}{s["line"]}: {s["what"]} ({s["why"]})')
        if len(surv) > max_details:
            lines.append(f'- ... {len(surv) - max_details} more in the run\'s `mutation-results` artifact')
        lines += ['', '</details>']
    if r.get('blocked_calls'):
        lines += ['', 'Tests tried to reach the outside (blocked): ' + '; '.join(f'`{c[:80]}`' for c in
                                                                                r['blocked_calls'][:5])]
    lines.append(f'<!-- /module:{name} -->')
    return '\n'.join(lines)


def old_sections(body):
    return {m.group(1): m.group(0) for m in re.finditer(r'<!-- module:(\S+) -->.*?<!-- /module:\1 -->', body or '', re.S)}


def old_trend(body):
    """The runs the last report kept ([{run, date, commit, rates}], oldest first), and a note when they were there but
    could not be read (the trend then starts again, said in the report, never a failed run)."""
    m = TREND_RE.search(body or '')
    if not m:
        return [], None
    try:
        runs = json.loads(m.group(1))
        if isinstance(runs, list) and all(isinstance(r, dict) and isinstance(r.get('rates'), dict) for r in runs):
            return [clean_run(r) for r in runs], None
    except ValueError:
        pass
    return [], 'the trend kept in the last report could not be read: it starts again with this run'


def clean_run(r):
    """A kept run as it may be shown: a date or a commit that is not one (letters and digits) becomes '?', and a rate
    that is not a number is left out. The body is read back from an issue: nothing in it reaches the table as Markdown."""
    date, commit = str(r.get('date', '')), str(r.get('commit', ''))
    rates = {str(n): v for n, v in r['rates'].items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return {'run': str(r.get('run', '')), 'date': date if DATE_RE.fullmatch(date) else '?',
            'commit': commit if COMMIT_RE.fullmatch(commit) else '?', 'rates': rates}


def trend(old_runs, results, run_url, commit, now, base=None):
    """old_runs plus this run (a rerun of the same run replaces its entry), the last TREND_KEEP. Only scored modules
    keep a rate: one judge calls no score (a shrunken module included) is '-', never next week's comparison."""
    rates = {n: r['kill_rate'] for n, r in sorted(results.items())
             if 'error' not in r and scored(r) and not shrunken(r, (base or {}).get(n))}
    this = clean_run({'run': run_url, 'date': f'{now:%Y-%m-%d}', 'commit': commit[:10], 'rates': rates})
    return ([r for r in old_runs if r.get('run') != run_url] + [this])[-TREND_KEEP:]


def last_rate(runs, name):
    """The module's rate in the newest run before this one that scored it, or None."""
    return next((r['rates'][name] for r in reversed(runs[:-1]) if isinstance(r['rates'].get(name), (int, float))), None)


def trend_table(runs, names):
    out = ['| Run | Commit | ' + ' | '.join(names) + ' |', '|---|---|' + '---|' * len(names)]
    for r in reversed(runs):   # newest first
        cells = [f'{r["rates"][n]}%' if isinstance(r['rates'].get(n), (int, float)) else '-' for n in names]
        out.append(f'| {r.get("date", "?")} | `{r.get("commit", "?")}` | ' + ' | '.join(cells) + ' |')
    return out


def change(rate, before):
    if before is None:
        return '-'
    d = round(rate - before, 1)
    return f'{d:+}' if d else '0'


def render(rows, problems, results, base, old_body, run_url, commit, now, max_details=200, skipped=None, runs=None,
           trend_note=None):
    runs = runs if runs is not None else trend([], results, run_url, commit, now, base)
    drops = [f'{n} {results[n]["kill_rate"]}% (baseline {base[n]["kill_rate"]}%)' for n, s, _ in rows if s == 'dropped']
    out = ['<!-- fork-ci mutation-report -->',
           f'**Mutation testing** ({now:%Y-%m-%d %H:%M} UTC, commit `{commit[:10]}`, '
           f'{f"[run]({run_url})" if run_url.startswith("http") else run_url}). '
           'A planted bug "survives" when every test still passes. Kill rate = planted bugs that a test caught. '
           'A rate below its baseline is a number to read, not a failure. Closing this issue does not stop the '
           'report: the next run reopens it and keeps the trend.',
           '', ('✅ Every module has a score.' if not problems else '❌ ' + ' · '.join(problems)), '']
    if drops:
        out += ['📉 Below the baseline: ' + ' · '.join(drops), '']
    out += ['| Module | Kill rate | Since last run | Baseline | Planted | Survived | Not run by any test | Note |',
            '|---|---|---|---|---|---|---|---|']
    for name, status, note in rows:
        r, b = results.get(name), (base or {}).get(name)
        bl = f'{b["kill_rate"]}%' if b else '-'
        if r is None or status == 'error':
            out.append(f'| {name} | - | - | {bl} | - | - | - | {status}: {note[:120]} |')
            continue
        surv = r['survived'] + r['no_cover']
        out.append(f'| {name} | **{r["kill_rate"]}%** | {change(r["kill_rate"], last_rate(runs, name))} | {bl} | '
                   f'{r["mutants"]} | {surv} | {r["no_cover"]} | {note} |')
    out += ['', f'### Kill rate per run (newest first, the last {TREND_KEEP} runs)']
    if trend_note:
        out.append(f'⚠️ {trend_note}')
    kept = json.dumps(runs).replace('>', '\\u003e')   # '>' is only ever inside a string: no '-->' ends the marker
    out += trend_table(runs, [name for name, _, _ in rows]) + [f'<!-- mutation-trend {kept} -->']
    if skipped:   # left out of this run's scores: said here, never only in the artifact
        out += ['', f'⚠️ {len(skipped)} test file(s) left out this run (red without any mutation, run no target): '
                + '; '.join(f'`{f}`: {why[:80]}' for f, why in sorted(skipped.items()))[:2000]]
    out += ['', '### Where planted bugs survive (most per line first)']
    olds = old_sections(old_body)
    for detail in (max_details, 50, 10):
        parts = []
        for name, status, note in rows:
            r = results.get(name)
            if r is not None:
                parts.append(section(name, r if 'error' in r or scored(r) else dict(r, error=note), detail,
                                     f'{now:%Y-%m-%d} '))
            elif name in olds:   # one marker, however many runs it stays missing; the section names its run
                parts.append(re.sub(rf'#### {re.escape(name)}[^\n]*', lambda _: f'#### {name} (not refreshed this run: '
                                    'the last result is below)', olds[name], count=1))
        body = '\n'.join(out + ['', '\n\n'.join(parts)])
        if len(body) <= MAX_BODY:
            return body
    return body[:MAX_BODY]


def suggest(results, base, commit, now):
    mods = dict(base or {})
    for name, r in results.items():
        if 'error' not in r and scored(r):
            mods[name] = {'kill_rate': r['kill_rate'], 'mutants': r['mutants'], 'recorded': f'{now:%Y-%m-%d}',
                          'commit': commit[:10]}
    return {'comment': 'Kill-rate baseline per module for the mutation run: a rate below it is reported, not a '
                       'failure. Changed only by a pull request; mutation_report.py --suggest writes a run\'s numbers '
                       'in this format.', 'modules': mods}


def load_config(path):
    """The config (CONFIG_DEFAULTS for each key it leaves out); ValueError for anything this does not understand. No
    path: the defaults."""
    if path is None:
        return dict(CONFIG_DEFAULTS)
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except OSError as e:
        raise ValueError(f'cannot read {path}: {e}') from e
    except ValueError as e:
        raise ValueError(f'{path} is not JSON: {e}') from e
    if not isinstance(raw, dict):
        raise ValueError(f'{path}: the config must be a JSON object')
    unknown = set(raw) - set(CONFIG_DEFAULTS) - {'about'}
    if unknown:
        raise ValueError(f'{path}: unknown config keys {sorted(unknown)} (from a newer release? move the pin first)')
    config = dict(CONFIG_DEFAULTS, **{k: v for k, v in raw.items() if k != 'about'})
    for k in ('title', 'red_title'):
        if not isinstance(config[k], str) or not config[k].strip() or '"' in config[k]:
            raise ValueError(f'{path}: "{k}" must be a non-empty title without a double quote')
    if config['title'] == config['red_title']:
        raise ValueError(f'{path}: "title" and "red_title" must differ')
    if not isinstance(config['author'], str) or not AUTHOR_RE.fullmatch(config['author']):
        raise ValueError(f'{path}: "author" must be the login the token writes as (an app\'s: app/NAME)')
    for k in ('labels', 'red_labels'):
        if not isinstance(config[k], list) or not all(isinstance(x, str) and x.strip() for x in config[k]):
            raise ValueError(f'{path}: "{k}" must be a list of label names')
    return config


def login(name):
    """A login as one string to compare: gh names an app app/NAME, the API NAME[bot]."""
    name = str(name or '').casefold()
    return name.removeprefix('app/').removesuffix('[bot]')


def find_issues(run, repo, title, author, state='all'):
    """Every issue titled exactly `title` that `author` opened, in `state` (all: open or closed): [{number, state,
    body}], oldest first. In a public repository anyone can open an issue with the same title: only the writer's own
    count, so a stranger's is never read, merged, written or closed. ValueError when the search returned a full last
    page (the report may lie past it): no write then, never a second report."""
    found = json.loads(run(['gh', 'issue', 'list', '--repo', repo, '--state', state, '--author', author, '--search',
                            f'"{title}" in:title', '--json', 'number,title,state,body,author',
                            '--limit', str(SEARCH_LIMIT)]) or '[]')
    if len(found) >= SEARCH_LIMIT:
        raise ValueError(f'the search for "{title}" by {author} found more than {SEARCH_LIMIT - 1} issues')
    return sorted(({'number': i['number'], 'state': str(i.get('state', '')).upper(), 'body': i.get('body') or ''}
                   for i in found if i['title'] == title and isinstance(i.get('author'), dict)
                   and login(i['author'].get('login')) == login(author)), key=lambda i: i['number'])


def pick_issue(issues):
    """(the issue to write or None, the other open ones). The oldest open one; with none open, the newest closed one."""
    opened = [i for i in issues if i['state'] == 'OPEN']
    if opened:
        return opened[0], opened[1:]
    return (issues[-1] if issues else None), []


def merged_trend(issues):
    """The runs every issue kept, each run once (by its URL; the newest issue's copy wins), oldest first; and a note when
    a trend was there but could not be read."""
    by_run, notes = {}, []
    for i in issues:
        runs, note = old_trend(i['body'])
        if note:
            notes.append(f'#{i["number"]}')
        for r in runs:
            by_run.pop(r.get('run'), None)
            by_run[r.get('run')] = r
    runs = sorted(by_run.values(), key=lambda r: str(r.get('date', '')))   # stable: same day keeps its order
    note = (f'the trend kept in {", ".join(notes)} could not be read: '
            + ('its runs are left out' if runs else 'it starts again with this run')) if notes else None
    return runs[-TREND_KEEP:], note


class WrongAuthor(ValueError):
    """The token writes as another account than the config's `author`."""


def create(run, repo, cmd, body, author):
    """gh issue create, then reads the new issue's author back. A token that writes as another account than `author`
    (a personal token, an app's) would open a new issue every run that the next run never finds (it searches by
    `author`): WrongAuthor then, naming the login to set, and the new issue is closed, so no run leaves one open."""
    out = run(cmd, stdin=body)
    m = re.search(r'/issues/(\d+)\s*\Z', out or '')
    if not m:
        raise ValueError(f'gh issue create printed no issue URL ({str(out)[:200]!r}): cannot check who wrote it')
    number = m.group(1)
    got = ((json.loads(run(['gh', 'issue', 'view', number, '--repo', repo, '--json', 'author']) or '{}')
            .get('author') or {}).get('login'))
    if login(got) == login(author):
        return
    name = str(got or '?')[:60]
    try:
        run(['gh', 'issue', 'close', number, '--repo', repo, '--comment',
             f'Closed by the mutation report: the token writes as `{name}`, not as the config\'s "author" '
             f'`{author}`, so the next run would not find this issue. Set "author" to `{name}`.'])
    except (subprocess.CalledProcessError, OSError):
        pass
    raise WrongAuthor(f'the new issue #{number} was written as {name}, not as the config\'s "author" ({author}): '
                      f'set "author": "{name}" in the config (the new issue is closed; the next run would not find it)')


def alert(run, repo, problems, run_url, commit, config=None):
    """One open issue red_title while runs are red (a module without a score): created or commented on (a comment
    notifies and bumps it), and closed by the first green run. Raises what gh raises."""
    config = config or CONFIG_DEFAULTS
    red_title = config['red_title']
    number = min((i['number'] for i in find_issues(run, repo, red_title, config['author'], 'open')), default=None)
    if not problems:
        if number:
            run(['gh', 'issue', 'close', str(number), '--repo', repo, '--comment',
                 f'The mutation run has a score for every module again at `{commit[:10]}` ({run_url}).'])
        return
    body = (f'The mutation run is red at `{commit[:10]}` ({run_url}): a module has no score.\n\n'
            + '\n'.join(f'- {p[:300]}' for p in problems[:20])
            + f'\n\nDetails: the issue "{config["title"]}". Fix forward in a pull request; the first green run closes '
              f'this issue.')
    if number:
        run(['gh', 'issue', 'comment', str(number), '--repo', repo, '--body-file', '-'], stdin=body)
    else:
        create(run, repo, ['gh', 'issue', 'create', '--repo', repo, '--title', red_title, '--body-file', '-',
                           *[x for lab in config['red_labels'] for x in ('--label', lab)]], body, config['author'])


def write_issue(run, repo, config, target, extra, body):
    """Writes the rolling issue: edit (or create), reopen a closed one, then close the other open copies. Each step
    alone leaves state the next run finishes: an edited issue still closed is the newest closed one, reopened next
    time; a copy still open is closed next time, its trend already merged."""
    if target is None:
        create(run, repo, ['gh', 'issue', 'create', '--repo', repo, '--title', config['title'], '--body-file', '-',
                           *[x for lab in config['labels'] for x in ('--label', lab)]], body, config['author'])
        return
    number = str(target['number'])
    run(['gh', 'issue', 'edit', number, '--repo', repo, '--body-file', '-'], stdin=body)
    if target['state'] != 'OPEN':
        run(['gh', 'issue', 'reopen', number, '--repo', repo, '--comment',
             'Reopened by the mutation report: this issue keeps its trend. Closing it does not stop the report.'])
    for i in extra:
        run(['gh', 'issue', 'close', str(i['number']), '--repo', repo, '--comment',
             f'A second copy of the mutation report #{number}; its trend is carried there.'])


def default_run(cmd, stdin=None):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, input=stdin).stdout


def gh_error(e):
    return getattr(e, 'stderr', None) or e


def main(argv=None, run=default_run, now=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--results', action='append', default=[], help='results JSON file or folder (repeatable)')
    ap.add_argument('--baseline', required=True)
    ap.add_argument('--expect', action='append', required=True, help='targets JSON whose modules must have a result')
    ap.add_argument('--config', help='the JSON config (issue titles and labels); none: the defaults')
    ap.add_argument('--issue', action='store_true', help='write the rolling issue (needs gh and GH_TOKEN)')
    ap.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY', ''))
    ap.add_argument('--run-url', default=os.environ.get('RUN_URL', '(local run)'))
    ap.add_argument('--commit', default=os.environ.get('GITHUB_SHA', 'local'))
    ap.add_argument('--out', help='also write the report here')
    ap.add_argument('--suggest', help='write this run\'s numbers as a baseline file here')
    ap.add_argument('--summary', help='append the rates and the trend here (the job summary: $GITHUB_STEP_SUMMARY)')
    ap.add_argument('--trend', help='write the kept runs (the trend) here as JSON (kept in the run\'s artifact)')
    a = ap.parse_args(argv)
    if a.issue and not a.repo:
        ap.error('--issue needs --repo (or GITHUB_REPOSITORY)')
    now = now or datetime.datetime.now(datetime.timezone.utc)
    config_error = None
    try:
        config = load_config(a.config)
    except ValueError as e:   # the report still appears; no issue is written with titles this cannot read
        config, config_error = dict(CONFIG_DEFAULTS), f'bad config: {e}'
    expected = []
    for t in a.expect:
        with open(t) as f:
            expected += [m for m in json.load(f)['modules'] if m not in expected]
    skipped = {}
    results = load_results([p for p in a.results if os.path.exists(p)], skipped)
    base, problem = load_baseline(a.baseline)
    rows, problems = judge(expected, results, base)
    if problem:
        problems.insert(0, problem)
    target, extra, issues, issue_error = None, [], [], config_error
    if a.issue and not config_error:
        try:
            issues = find_issues(run, a.repo, config['title'], config['author'])
            target, extra = pick_issue(issues)
        except (subprocess.CalledProcessError, OSError, ValueError, TypeError, KeyError) as e:   # gh down or missing
            issue_error = f'could not read the rolling issue: {gh_error(e)}'
    old_runs, trend_note = merged_trend(issues)
    if issue_error:   # the summary and trend.json would show a one-run trend without saying why
        trend_note = 'the rolling issue could not be read: the trend here shows this run only'
    runs = trend(old_runs, results, a.run_url, a.commit, now, base)
    body = render(rows, problems, results, base, (target or {}).get('body', ''), a.run_url, a.commit, now,
                  skipped=skipped, runs=runs, trend_note=trend_note)
    if a.summary:   # the rates and the trend, without the survivor sections (those are in the issue and the artifact)
        with open(a.summary, 'a') as f:
            f.write(body.split('\n### Where planted bugs survive')[0] + '\n')
    if a.trend:
        with open(a.trend, 'w') as f:
            json.dump({'runs': runs}, f, indent=1)
            f.write('\n')
    if a.out:
        with open(a.out, 'w') as f:
            f.write(body)
    if a.suggest:
        with open(a.suggest, 'w') as f:
            json.dump(suggest(results, base, a.commit, now), f, indent=2)
            f.write('\n')
    print(body if not a.issue else '\n'.join(body.split('\n')[:20]))
    for p in problems:   # the verdict is logged before anything can fail on GitHub
        print(f'::error::{p}')
    wrong_author = False
    if a.issue and not issue_error:   # never create a second issue when the first couldn't be read
        try:
            write_issue(run, a.repo, config, target, extra, body)
        except (subprocess.CalledProcessError, OSError, ValueError, TypeError, AttributeError) as e:
            wrong_author = isinstance(e, WrongAuthor)
            issue_error = f'could not write the rolling issue: {gh_error(e)}'
    # Also when the rolling issue failed (the alert is the part that reaches someone), but not with a token that
    # writes as another account: its red issue would be one more that no run finds.
    if a.issue and not config_error and not wrong_author:
        try:
            alert(run, a.repo, problems, a.run_url, a.commit, config)
        except (subprocess.CalledProcessError, OSError, ValueError, TypeError, KeyError) as e:
            issue_error = '; '.join(filter(None, [issue_error, f'could not open/close the "{config["red_title"]}" '
                                                               f'issue: {gh_error(e)}']))
    if issue_error:
        print(f'::error::{issue_error[:500]}')
    return 1 if problems or issue_error else 0


if __name__ == '__main__':
    sys.exit(main())
