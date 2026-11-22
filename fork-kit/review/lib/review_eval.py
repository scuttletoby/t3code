"""The review eval (../eval/README.md): which model and effort finds the confirmed blocker and major bugs of past pull
requests. Every project fact (the repo, the cases, the work folder, the configs to compare, prices, where transcripts
are) comes from one eval config file; nothing here names a project or a machine.

  python3 -I fork-kit/review/lib/review_eval.py <command> --config eval.json [args]

  case [CASE...]       build each case folder (src/, diff.patch, diff.stat, brief.md) from the repo
  runs                 write the run map (every case x config x rep, ids and order fixed by the seed) and list it
  prompt RUN-ID        the prompt for one run, filled in
  judge-prep CASE      copy the case's run reports under neutral names for the blind judge
  judge-prompt CASE    the judge's prompt, filled in
  cost                 tokens, $ and active minutes per run from the agents' transcripts
  analyze              recall per config, worst run, pairs, and which setting the rule picks

Safe to run again (the same input gives the same files and output) and to stop at any point: every file is written
to a temporary name and renamed, a case folder is built beside its final name and renamed when complete, and a run
map or case that would change under existing results is refused, never overwritten.
"""
import glob
import hashlib
import io
import itertools
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
from datetime import datetime

sys.dont_write_bytecode = True

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = 1
KEYS = {'version': int, 'workdir': str, 'repo': str, 'truth': str, 'briefs': str, 'prompt': str,
        'judge_prompt': str, 'transcripts': list, 'prices': dict, 'configs': list, 'reps': int, 'seed': str,
        'idle_gap_seconds': int}
REQUIRED = ('version', 'workdir', 'repo', 'truth', 'configs')
DEFAULTS = {'briefs': None, 'prompt': os.path.join(KIT, 'eval', 'prompt.txt'),
            'judge_prompt': os.path.join(KIT, 'eval', 'judge_prompt.txt'), 'transcripts': [], 'prices': {},
            'reps': 2, 'seed': 'review-eval', 'idle_gap_seconds': 300}
PRICE_KEYS = ('input', 'output', 'cache_write_5m', 'cache_write_1h', 'cache_read')   # $ per million tokens
LISTS = ('blocker_major', 'minor', 'known_other_lens')
NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
RUN_RE = re.compile(r'RUN-ID: (r[0-9a-f]{8})\b')
VERDICTS = ('match', 'partial', 'real', 'fp', 'unsure')


class Problem(Exception):
    """A plain message: what is wrong and what to do. Exit 2."""


# --- files -------------------------------------------------------------------------------------------------------

def dump(obj):
    return json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + '\n'


def write(path, text):
    """Write by temporary file and rename: a stop part-way leaves the old file or the new one, never half of one."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def load_json(path, what):
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError:
        raise Problem(f'{what} {path} does not exist') from None
    except (OSError, UnicodeDecodeError) as e:
        raise Problem(f'{what} {path} cannot be read: {e}') from None
    if not text.strip():
        raise Problem(f'{what} {path} is empty')
    try:
        return json.loads(text)
    except ValueError as e:
        raise Problem(f'{what} {path} is not JSON: {e}') from None


def load_config(path):
    """The eval config, paths resolved against its folder. Problem for a missing, empty, old or newer file, an unknown
    key or a wrong value, every problem named at once."""
    if not path:
        raise Problem('give the eval config with --config FILE (eval/eval.example.json shows one)')
    raw = load_json(path, 'the eval config')
    if not isinstance(raw, dict):
        raise Problem(f'the eval config {path} must be a JSON object')
    if raw.get('version') != VERSION:
        v = raw.get('version')
        newer = isinstance(v, int) and v > VERSION
        raise Problem(f'the eval config {path} has version {json.dumps(v)}; this kit reads version {VERSION}'
                      + (': update the kit' if newer else ''))
    problems = [f'unknown key {k!r}' for k in raw if k not in KEYS]
    problems += [f'missing key {k!r}' for k in REQUIRED if k not in raw]
    c = {**DEFAULTS, **raw}
    for k, t in KEYS.items():
        if k in raw and not (isinstance(raw[k], t) and not (t is int and isinstance(raw[k], bool))):
            problems.append(f'{k} must be {t.__name__}')
    if problems:
        raise Problem(f'the eval config {path}: ' + '; '.join(problems))
    names = [x.get('name') if isinstance(x, dict) else None for x in c['configs']]
    if not c['configs']:
        problems.append('configs is empty: list at least one {"name", "model", "effort"}')
    for x in c['configs']:
        if not (isinstance(x, dict) and set(x) == {'name', 'model', 'effort'}
                and all(isinstance(v, str) and NAME_RE.fullmatch(v) for v in x.values())):
            problems.append(f'config {json.dumps(x)} must be {{"name", "model", "effort"}} of plain names')
    if len(set(names)) != len(names):
        problems.append('two configs share a name')
    if not 1 <= c['reps'] <= 20:
        problems.append('reps must be 1-20')
    for model, p in c['prices'].items():
        if not (isinstance(p, dict) and set(p) == set(PRICE_KEYS)
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in p.values())):
            problems.append(f'prices.{model} must be {{{", ".join(PRICE_KEYS)}}} in $ per million tokens')
    if not all(isinstance(g, str) for g in c['transcripts']):
        problems.append('transcripts must be a list of glob patterns')
    if problems:
        raise Problem(f'the eval config {path}: ' + '; '.join(problems))
    here = os.path.dirname(os.path.abspath(path))
    for k in ('workdir', 'repo', 'truth', 'briefs', 'prompt', 'judge_prompt'):
        if c[k] is not None:
            c[k] = os.path.normpath(os.path.join(here, os.path.expanduser(c[k])))
    c['transcripts'] = [os.path.join(here, os.path.expanduser(g)) for g in c['transcripts']]
    return c


def load_truth(c):
    t = load_json(c['truth'], 'the truth set')
    cases = t.get('cases') if isinstance(t, dict) else None
    if not isinstance(cases, dict) or not cases:
        raise Problem(f'the truth set {c["truth"]} needs {{"cases": {{"<case>": {{...}}}}}} with at least one case')
    problems = []
    for name, k in cases.items():
        if not NAME_RE.fullmatch(name):
            problems.append(f'case name {name!r} must be a plain name')
            continue
        if not isinstance(k, dict):
            problems.append(f'case {name} must be an object')
            continue
        for f in ('reviewed_sha', 'base'):
            if not isinstance(k.get(f), str) or not k[f]:
                problems.append(f'case {name} needs {f}')
        for f in LISTS:
            items = k.get(f, [])
            if not isinstance(items, list) or not all(isinstance(i, dict) and isinstance(i.get('id'), str)
                                                      for i in items):
                problems.append(f'case {name}: {f} must be a list of {{"id", "loc", "bug"}}')
    if problems:
        raise Problem(f'the truth set {c["truth"]}: ' + '; '.join(problems))
    return cases


def ids(cases, name):
    return {f: [i['id'] for i in cases[name].get(f, [])] for f in LISTS}


# --- case --------------------------------------------------------------------------------------------------------

def git(c, *args):
    r = subprocess.run(['git', '-C', c['repo'], *args], capture_output=True)
    if r.returncode != 0:
        raise Problem(f'git {" ".join(args)} in {c["repo"]} failed: {r.stderr.decode("utf-8", "replace").strip()}')
    return r.stdout


def build_case(c, cases, name, out):
    k = cases[name]
    final = os.path.join(c['workdir'], 'cases', name)
    stamp = {'case': name, 'reviewed_sha': k['reviewed_sha'], 'base': k['base']}
    if os.path.isdir(final):
        try:
            with open(os.path.join(final, 'case.json'), encoding='utf-8') as f:
                old = json.load(f)
        except (OSError, ValueError):
            old = None
        if old != stamp:
            raise Problem(f'case folder {final} was built from other commits (or is not a case folder): move it away '
                          f'to build it again')
        print(f'{name}: already built', file=out)
        return
    tmp = os.path.join(c['workdir'], 'cases', f'.{name}.part')
    if os.path.isdir(tmp):   # a build that stopped part-way: start it again
        shutil.rmtree(tmp)
    os.makedirs(os.path.join(tmp, 'src'))
    archive = git(c, 'archive', '--format=tar', k['reviewed_sha'])
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(os.path.join(tmp, 'src'), filter='data')
    diff = git(c, 'diff', k['base'], k['reviewed_sha'])
    stat = git(c, 'diff', '--stat=200', k['base'], k['reviewed_sha'])
    with open(os.path.join(tmp, 'diff.patch'), 'wb') as f:
        f.write(diff)
    with open(os.path.join(tmp, 'diff.stat'), 'wb') as f:
        f.write(stat)
    brief = os.path.join(c['briefs'], f'{name}.md') if c['briefs'] else None
    if brief and os.path.isfile(brief):
        shutil.copyfile(brief, os.path.join(tmp, 'brief.md'))
    else:
        print(f'{name}: no brief ({"set briefs" if not c["briefs"] else brief}): write brief.md into the case folder '
              f'before the runs', file=out)
    write(os.path.join(tmp, 'case.json'), dump(stamp))   # last: its presence says the folder is complete
    os.replace(tmp, final)
    lines = diff.count(b'\n')
    print(f'{name}: built, diff {lines} lines', file=out)


# --- runs --------------------------------------------------------------------------------------------------------

def h(*parts):
    return hashlib.sha1('\0'.join(map(str, parts)).encode()).hexdigest()


def runmap(c, cases):
    runs = [{'id': 'r' + h(c['seed'], name, x['name'], rep)[:8], 'case': name, 'config': x['name'],
             'model': x['model'], 'effort': x['effort'], 'rep': rep}
            for name in sorted(cases) for x in c['configs'] for rep in range(1, c['reps'] + 1)]
    return sorted(runs, key=lambda r: h(c['seed'], 'order', r['id']))


def gt(c, name):
    return os.path.join(c['workdir'], 'gt', name)


def write_runs(c, cases, out):
    runs = runmap(c, cases)
    path = gt(c, 'runmap.json')
    if os.path.exists(path):
        old = load_json(path, 'the run map')
        if old != runs:
            have = [r['id'] for r in old if os.path.exists(os.path.join(c['workdir'], 'runs', f'{r["id"]}.md'))]
            if have:
                raise Problem(f'the run map {path} differs from the config, and {len(have)} runs already have reports: '
                              f'start a new workdir for a new set')
    write(path, dump(runs))
    for r in runs:
        print(f'{r["id"]}  {r["case"]}  {r["config"]}  {r["model"]} {r["effort"]}  rep {r["rep"]}', file=out)


def read_runs(c):
    return load_json(gt(c, 'runmap.json'), 'the run map (write it with `runs`)')


def fill(template, **values):
    try:
        with open(template, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        raise Problem(f'the prompt template {template} cannot be read: {e.strerror}') from None
    for k, v in values.items():
        text = text.replace('{' + k + '}', v)
    return text


# --- judge -------------------------------------------------------------------------------------------------------

def judge_prep(c, cases, name, out):
    if name not in cases:
        raise Problem(f'no case {name!r} in the truth set')
    runs = [r for r in read_runs(c) if r['case'] == name]
    missing = [r['id'] for r in runs if not os.path.isfile(os.path.join(c['workdir'], 'runs', f'{r["id"]}.md'))]
    if missing:
        raise Problem(f'case {name}: no report yet for {", ".join(missing)} (runs/<id>.md)')
    d = os.path.join(c['workdir'], 'judge', name)
    write(os.path.join(d, 'truth.json'), dump(cases[name]))
    names = {}
    for n, r in enumerate(sorted(runs, key=lambda r: h(c['seed'], 'judge', r['id'])), 1):
        review = f'review-{n:02d}.md'
        with open(os.path.join(c['workdir'], 'runs', f'{r["id"]}.md'), encoding='utf-8') as f:
            write(os.path.join(d, review), f.read())
        names[review] = r['id']
    path = gt(c, 'judgemap.json')
    allm = load_json(path, 'the judge map') if os.path.exists(path) else {}
    allm[name] = names
    write(path, dump(allm))
    print(f'{name}: {len(runs)} reviews in {d}', file=out)


# --- cost --------------------------------------------------------------------------------------------------------

def _first_user_text(path):
    """The text of a transcript's first user message (a Claude Code session file, JSON lines), or ''."""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get('type') == 'user':
                    m = (e.get('message') or {}).get('content')
                    if isinstance(m, list):
                        m = ' '.join(x.get('text', '') for x in m if isinstance(x, dict))
                    return m if isinstance(m, str) else ''
    except OSError:
        return ''
    return ''


def _stamp(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def cost(c, out):
    runs = {r['id']: r for r in read_runs(c)}
    files = {}
    for pattern in c['transcripts']:
        for f in sorted(glob.glob(pattern)):
            m = RUN_RE.match(_first_user_text(f).lstrip())   # the run's own session starts with its RUN-ID line
            if m and m.group(1) in runs:
                files.setdefault(m.group(1), []).append(f)
    result = {}
    for rid, fs in sorted(files.items()):
        usage, times, model = {}, [], None
        for f in fs:
            with open(f, encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(e, dict):
                        continue
                    if isinstance(e.get('timestamp'), str):
                        try:
                            times.append(_stamp(e['timestamp']))
                        except ValueError:
                            pass
                    m = e.get('message')
                    if e.get('type') == 'assistant' and isinstance(m, dict) and isinstance(m.get('usage'), dict):
                        model = m.get('model') or model
                        usage[m.get('id') or e.get('requestId') or len(usage)] = m['usage']
        t = dict.fromkeys(('input', 'output', 'cache_write_5m', 'cache_write_1h', 'cache_read'), 0)
        for u in usage.values():
            t['input'] += u.get('input_tokens', 0)
            t['output'] += u.get('output_tokens', 0)
            t['cache_read'] += u.get('cache_read_input_tokens', 0)
            cc = u.get('cache_creation') or {}
            if cc:
                t['cache_write_5m'] += cc.get('ephemeral_5m_input_tokens', 0)
                t['cache_write_1h'] += cc.get('ephemeral_1h_input_tokens', 0)
            else:
                t['cache_write_5m'] += u.get('cache_creation_input_tokens', 0)
        times.sort()
        gap = c['idle_gap_seconds']
        active = sum(b - a for a, b in zip(times, times[1:]) if b - a <= gap)
        p = c['prices'].get(runs[rid]['model'])
        usd = round(sum(t[k] * p[k] for k in PRICE_KEYS) / 1e6, 3) if p else None
        result[rid] = {**t, 'usd': usd, 'active_min': round(active / 60, 1), 'calls': len(usage),
                       'model_seen': model, 'files': len(fs)}
    write(gt(c, 'cost.json'), dump(result))
    missing = sorted(set(runs) - set(result))
    unpriced = sorted({runs[r]['model'] for r in result if result[r]['usd'] is None})
    print(f'{len(result)} runs costed; no transcript: {", ".join(missing) or "none"}', file=out)
    if unpriced:
        print(f'no price for {", ".join(unpriced)}: add it under prices', file=out)


# --- analyze -----------------------------------------------------------------------------------------------------

def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def analyze(c, cases, out):
    runmap_ = {r['id']: r for r in read_runs(c)}
    jpath = gt(c, 'judgemap.json')
    jm = load_json(jpath, 'the judge map') if os.path.exists(jpath) else {}
    cpath = gt(c, 'cost.json')
    costs = load_json(cpath, 'the cost file') if os.path.exists(cpath) else {}
    runs, judged, skipped = {}, [], []
    for name in sorted(jm):
        p = os.path.join(c['workdir'], 'judge', name, 'scores.json')
        if name not in cases or not os.path.exists(p):
            skipped.append(name)
            continue
        s = load_json(p, f'the scores of case {name}')
        t = ids(cases, name)
        bm, every = set(t['blocker_major']), set(itertools.chain(*t.values()))
        judged.append(name)
        for review, rid in sorted(jm[name].items()):
            r = (s.get('reviews') or {}).get(review)
            if not isinstance(r, dict) or rid not in runmap_:
                raise Problem(f'case {name}: scores.json has no entry for {review} (or the run map lost {rid}): judge '
                              f'the case again')
            found = set(r.get('found') or [])
            v = [f.get('verdict') for f in r.get('findings') or []]
            k = costs.get(rid, {})
            runs[rid] = {**runmap_[rid], 'found': sorted(found), 'bm_found': sorted(found & bm),
                         'bm_recall': len(found & bm) / len(bm) if bm else None,
                         'all_recall': len(found & every) / len(every) if every else None,
                         'n_findings': len(v), **{x: v.count(x) for x in VERDICTS},
                         'usd': k.get('usd'), 'active_min': k.get('active_min')}
    configs = {}
    for name in sorted({r['config'] for r in runs.values()}):
        rs = [r for r in runs.values() if r['config'] == name]
        rec = [r['bm_recall'] for r in rs if r['bm_recall'] is not None]
        missed = {}
        for r in rs:
            for b in ids(cases, r['case'])['blocker_major']:
                if b not in r['bm_found']:
                    missed[b] = missed.get(b, 0) + 1
        configs[name] = {'runs': len(rs), 'bm_mean': mean(rec), 'bm_worst': min(rec) if rec else None,
                         'all_mean': mean(r['all_recall'] for r in rs), 'fp_mean': mean(r['fp'] for r in rs),
                         'usd_mean': mean(r['usd'] for r in rs), 'min_mean': mean(r['active_min'] for r in rs),
                         'missed': dict(sorted(missed.items()))}
    pairs = {}
    for a, b in itertools.combinations_with_replacement(sorted(configs), 2):
        vals, usd = [], []
        for name in judged:
            bm = set(ids(cases, name)['blocker_major'])
            if not bm:
                continue
            ra = [r for r in runs.values() if r['case'] == name and r['config'] == a]
            rb = [r for r in runs.values() if r['case'] == name and r['config'] == b]
            for x, y in (itertools.combinations(ra, 2) if a == b else itertools.product(ra, rb)):
                vals.append(len((set(x['bm_found']) | set(y['bm_found'])) & bm) / len(bm))
                usd.append((x['usd'] or 0) + (y['usd'] or 0) if x['usd'] is not None and y['usd'] is not None else None)
        if vals:
            pairs[f'{a}+{b}'] = {'bm_mean': mean(vals), 'bm_worst': min(vals), 'usd_mean': mean(usd),
                                 'pairs': len(vals)}
    pick = None
    scored = {k: v for k, v in configs.items() if v['bm_worst'] is not None}
    if scored:
        best = max(v['bm_worst'] for v in scored.values())
        tied = [k for k, v in scored.items() if v['bm_worst'] == best]
        pick = min(tied, key=lambda k: (scored[k]['usd_mean'] is None, scored[k]['usd_mean'] or 0, k))
    result = {'judged': judged, 'not_judged': skipped, 'runs': runs, 'configs': configs, 'pairs': pairs, 'pick': pick}
    write(gt(c, 'analysis.json'), dump(result))
    if not judged:
        print('no judged case yet: run judge-prep and the judge first', file=out)
        return
    print(f'cases judged: {", ".join(judged)}' + (f'; not judged: {", ".join(skipped)}' if skipped else ''), file=out)
    fmt = lambda x, f='.2f': '-' if x is None else format(x, f)   # noqa: E731
    print(f'{"config":16}{"runs":>5}{"bm mean":>9}{"bm worst":>9}{"all":>6}{"fp":>5}{"$":>7}{"min":>6}', file=out)
    for k, v in sorted(configs.items(), key=lambda kv: (-(kv[1]['bm_mean'] or 0), kv[0])):
        print(f'{k:16}{v["runs"]:>5}{fmt(v["bm_mean"]):>9}{fmt(v["bm_worst"]):>9}{fmt(v["all_mean"]):>6}'
              f'{fmt(v["fp_mean"], ".1f"):>5}{fmt(v["usd_mean"]):>7}{fmt(v["min_mean"], ".1f"):>6}', file=out)
    for k, v in configs.items():
        if v['missed']:
            print(f'missed by {k}: ' + ', '.join(f'{b} ({n})' for b, n in v['missed'].items()), file=out)
    for k, v in sorted(pairs.items(), key=lambda kv: (-kv[1]['bm_worst'], -kv[1]['bm_mean'], kv[0])):
        print(f'pair {k}: mean {fmt(v["bm_mean"])} worst {fmt(v["bm_worst"])} $ {fmt(v["usd_mean"])} '
              f'({v["pairs"]} pairs)', file=out)
    if pick:
        print(f'rule (the cheapest setting whose worst-case blocker/major recall equals the best): {pick}', file=out)


# --- command line ------------------------------------------------------------------------------------------------

COMMANDS = ('case', 'runs', 'prompt', 'judge-prep', 'judge-prompt', 'cost', 'analyze')


def main(argv, out=sys.stdout):
    if not argv or argv[0] not in COMMANDS:
        print('\n\n'.join(__doc__.split('\n\n')[1:3]), file=out)
        return 2
    cmd, rest, path = argv[0], [], None
    it = iter(argv[1:])
    for a in it:
        if a == '--config':
            path = next(it, None)
        elif a.startswith('--'):
            raise Problem(f'unknown option {a}')
        else:
            rest.append(a)
    c = load_config(path)
    cases = load_truth(c)
    one = cmd in ('prompt', 'judge-prep', 'judge-prompt')
    if one and len(rest) != 1 or cmd in ('runs', 'cost', 'analyze') and rest:
        raise Problem(f'{cmd} takes {"one argument" if one else "no arguments"}')
    if cmd == 'case':
        unknown = [x for x in rest if x not in cases]
        if unknown:
            raise Problem(f'no case {", ".join(unknown)} in the truth set')
        for name in rest or sorted(cases):
            build_case(c, cases, name, out)
    elif cmd == 'runs':
        write_runs(c, cases, out)
    elif cmd == 'prompt':
        r = next((r for r in read_runs(c) if r['id'] == rest[0]), None)
        if not r:
            raise Problem(f'no run {rest[0]} in the run map')
        print(f'# model {r["model"]} effort {r["effort"]}', file=out)
        print(fill(c['prompt'], id=r['id'], case=r['case'], workdir=c['workdir']), end='', file=out)
    elif cmd == 'judge-prep':
        judge_prep(c, cases, rest[0], out)
    elif cmd == 'judge-prompt':
        if rest[0] not in cases:
            raise Problem(f'no case {rest[0]!r} in the truth set')
        print(fill(c['judge_prompt'], case=rest[0], workdir=c['workdir']), end='', file=out)
    elif cmd == 'cost':
        cost(c, out)
    else:
        analyze(c, cases, out)
    return 0


def run(argv, out=sys.stdout, err=sys.stderr):
    try:
        return main(argv, out)
    except Problem as e:
        print(f'review-eval: {e}', file=err)
        return 2


if __name__ == '__main__':
    sys.exit(run(sys.argv[1:]))
