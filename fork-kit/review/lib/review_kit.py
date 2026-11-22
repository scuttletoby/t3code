"""The review kit's helpers (../README.md): `plan` says which lenses a change needs and how to run them, `check` says how
the gate reads one lens comment, and `doc` checks that every example in COMMENTS.md reads as it says.

Every rule about paths, lens comments, findings, rounds and the `state:` line is the gate's own code
(fork-ci/actions/pr-queue: config.py, lens.py, review_config.py, review_judge.py; fork-ci/actions/head-check:
head_check.changed_paths), imported, never copied: the kit cannot read a comment or a change another way than the gate
does. It imports that code from the fork checkout it runs in, so run the kit at the release the project's gate pins
(README.md). The kit adds only what the gate does not judge: the project's kit settings (who runs a lens, how many lens
rounds, what happens to minors, the state question) and requiring the `state:` line in `check` (the gate parses
`state_line` but does not enforce it yet; what counts as one is review_judge.has_state_line).

  python3 -I fork-kit/review/lib/review_kit.py plan  [--root DIR] [--settings FILE] (--base REF | --changes FILE|-)
  python3 -I fork-kit/review/lib/review_kit.py check [--root DIR] [--settings FILE] [--head SHA] FILE|-
  python3 -I fork-kit/review/lib/review_kit.py doc   [FILE]

No state: every run reads the files again, so the same input gives the same output. A missing, empty, old or broken
file gives a plain message and exit 2, never a traceback.
"""
import glob
import json
import os
import re
import subprocess
import sys

sys.dont_write_bytecode = True   # keep the fork-ci folder free of __pycache__

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(os.path.dirname(os.path.dirname(KIT)), 'fork-ci', 'actions', 'pr-queue')
sys.path.insert(0, GATE)
sys.path.insert(0, os.path.join(os.path.dirname(GATE), 'head-check'))
import config as gate_config  # noqa: E402
import head_check  # noqa: E402
import lens  # noqa: E402
import review_judge  # noqa: E402

SETTINGS = '.github/review-kit.json'
SETTINGS_VERSION = 1
STATE_QUESTION = 'What happens when this runs again, stops half-way, or meets existing, old or empty state?'
DEFAULTS = {
    'version': SETTINGS_VERSION,
    'queue_config': '.github/pr-queue.json',
    'runner': {'model': 'claude-opus-5-5', 'effort': 'high'},
    'lens_runners': {},
    'audit_runner': {'model': 'claude-fable-5-2', 'effort': 'high'},
    'forbidden_efforts': ['low', 'max'],
    'lens_rounds': 2,
    'minors': 'same-round',
    'state_question': STATE_QUESTION,
}
MINORS = {'issue': 'minors not fixed when the pull request stops go into one follow-up issue, linked on it, with the '
                   'project\'s risk labels',
          # The one place the kit states the same-round rule; SKILL.md, REVIEW.md and docs point here.
          'same-round': 'a minor or nit is fixed in the same pull request when it is small and in scope (in the fix round '
                        'that is running, adding no lens round), else dropped with a one-line reason under "Left" in the '
                        'pull request description; it becomes an issue only when it is a real bug (wrong result, data, '
                        'hang or security) outside the pull request\'s scope, as a comment on an open issue that covers '
                        'that area if there is one. A follow-up pull request\'s own minors and nits never become an '
                        'issue, and missing tests, wording, naming or style are never an issue by themselves'}
FALLBACK_LENS = 'review'


class Problem(Exception):
    """A plain message for the person or agent: what is wrong and what to do. Exit 2."""


def read_text(path, what):
    """The file's text, or None when it does not exist. Problem when it cannot be read or is not UTF-8."""
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Problem(f'{what} {path} cannot be read: {e.strerror}') from None
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        raise Problem(f'{what} {path} is not UTF-8 text') from None


def _runner_problem(v, where, forbidden):
    if not isinstance(v, dict) or set(v) != {'model', 'effort'} or not all(isinstance(x, str) and x for x in v.values()):
        return f'{where} must be {{"model": "<model id>", "effort": "<effort>"}}'
    if v['effort'] in forbidden:
        return f'{where} has effort {v["effort"]!r}, which forbidden_efforts lists'
    return None


def load_settings(root, path=None):
    """(settings, note). The project's kit settings over DEFAULTS; no file: DEFAULTS, and the note says so. Problem for an
    empty file, a file that is not a JSON object, a newer or unknown version, an unknown key or a wrong value (every
    problem named at once)."""
    rel = path or SETTINGS
    full = os.path.join(root, rel)
    text = read_text(full, 'the kit settings')
    if text is None:
        if path:
            raise Problem(f'the kit settings {rel} do not exist (given with --settings)')
        return dict(DEFAULTS), f'no {rel}: the kit defaults apply (REVIEW.md, "Kit settings")'
    if not text.strip():
        raise Problem(f'the kit settings {rel} are empty: write {{"version": {SETTINGS_VERSION}}} for the defaults, '
                      f'or delete the file')
    try:
        raw = json.loads(text)
    except ValueError as e:
        raise Problem(f'the kit settings {rel} are not JSON: {e}') from None
    if not isinstance(raw, dict):
        raise Problem(f'the kit settings {rel} must be a JSON object')
    if raw.get('version') != SETTINGS_VERSION:
        newer = isinstance(raw.get('version'), int) and raw['version'] > SETTINGS_VERSION
        raise Problem(f'the kit settings {rel} have version {json.dumps(raw.get("version"))}; this kit reads version '
                      f'{SETTINGS_VERSION}' + (': update the kit' if newer else ''))
    problems = [f'unknown key {k!r} (known: {", ".join(DEFAULTS)})' for k in raw if k not in DEFAULTS]
    s = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    forbidden = s['forbidden_efforts']
    if not isinstance(forbidden, list) or not all(isinstance(x, str) for x in forbidden):
        problems.append('forbidden_efforts must be a list of effort names')
        forbidden = []
    for key in ('runner', 'audit_runner'):
        problems.append(_runner_problem(s[key], key, forbidden))
    if not isinstance(s['lens_runners'], dict):
        problems.append('lens_runners must be an object {"<lens name>": {"model", "effort"}}')
    else:
        problems += [_runner_problem(v, f'lens_runners.{k}', forbidden) for k, v in s['lens_runners'].items()]
    if not isinstance(s['queue_config'], str) or not s['queue_config']:
        problems.append('queue_config must be the repo-relative path of the queue config')
    if not (isinstance(s['lens_rounds'], int) and not isinstance(s['lens_rounds'], bool) and 1 <= s['lens_rounds'] <= 20):
        problems.append('lens_rounds must be a whole number 1-20')
    if not isinstance(s['minors'], str) or s['minors'] not in MINORS:
        problems.append(f'minors must be one of {", ".join(MINORS)}')
    if not isinstance(s['state_question'], str) or not 20 <= len(s['state_question'].strip()) <= 600:
        problems.append('state_question must be text of 20-600 characters (it cannot be emptied)')
    problems = [p for p in problems if p]
    if problems:
        raise Problem(f'the kit settings {rel}: ' + '; '.join(problems))
    return s, None


def commit_of(root, ref):
    """The full sha of the commit `ref` names in the repository at `root`; Problem when it names none."""
    r = subprocess.run(['git', '-C', root, 'rev-parse', '--verify', '--quiet', '--end-of-options', f'{ref}^{{commit}}'],
                       capture_output=True)
    sha = r.stdout.decode('ascii', 'replace').strip()
    if r.returncode != 0 or not head_check.SHA.fullmatch(sha):
        raise Problem(f'{ref} is not a commit in {root}')
    return sha


def _git_said(r):
    return os.fsdecode(r.stderr or b'').strip()[:200] or f'exit {r.returncode}'


def base_file(root, base, rel):
    """The bytes of `rel` in the commit `base` (a full sha), or None only when its tree has no such entry. Any other
    git failure (a missing tree or blob, a partial clone offline) is a Problem: the file is there, so judging as if it
    were not would advise fewer lenses than the gate asks."""
    path = head_check.repo_path(rel)
    r = subprocess.run(['git', '-C', root, 'ls-tree', '-z', '--full-tree', base, '--', path], capture_output=True)
    if r.returncode != 0:
        raise Problem(f'git cannot read the tree of {base[:12]} in {root} to find {rel}: {_git_said(r)}')
    if not r.stdout:
        return None
    r = subprocess.run(['git', '-C', root, 'show', f'{base}:{path}'], capture_output=True)
    if r.returncode != 0:
        raise Problem(f'{rel} is in {base[:12]} but git cannot read it: {_git_said(r)}; fetch the missing objects '
                      f'(or run from a full clone) and plan again')
    return r.stdout


def other_settings(root):
    """Files in `.github` named like kit settings under another name (the fork's `fork-review-kit.json`): the plan
    names them when it found no settings and no queue config, as the likely missing --settings."""
    found = glob.glob(os.path.join(glob.escape(os.path.join(root, '.github')), '*review-kit*.json'))
    return sorted(os.path.relpath(f, root) for f in found if os.path.relpath(f, root) != SETTINGS)


def load_queue_config(root, settings, base=None):
    """(gate Config or None, note). The config is read from the commit `base` (a full sha) with git when given (as the
    gate reads it, from the base tip, as bytes: one that is not UTF-8 is refused as the gate refuses it), else from the
    file. No file, or no `review` block (a config from before the review rules): None, and the note says how the plan
    judges. Problem for an empty or invalid config (the gate would refuse it too), and for a base config git cannot
    read (base_file)."""
    rel = settings['queue_config']
    if base:
        text = base_file(root, base, rel)
    else:
        text = read_text(os.path.join(root, rel), 'the queue config')
    where = f'{rel} at {base}' if base else rel
    if text is None:
        return None, (f'no queue config {where}: no review rules, so every change is judged normal with one lens '
                      f'({FALLBACK_LENS!r}); no path is critical, because the kit does not guess (REVIEW.md, step 1)')
    if not text.strip():
        raise Problem(f'the queue config {where} is empty: the gate refuses it too')
    try:
        cfg = gate_config.parse(text, where)
    except gate_config.ConfigError as e:
        raise Problem(f'the gate refuses the queue config: {e}') from None
    if cfg.review is None:
        return cfg, (f'{where} has no `review` block (a config from before the review rules): every change is judged '
                     f'normal with one lens ({FALLBACK_LENS!r}); add the block (REVIEW.md, step 1)')
    return cfg, None


def git_dir(root):
    """The repository's common git folder (a linked worktree's too): where its objects, `shallow` and `info/grafts`
    are."""
    r = subprocess.run(['git', '-C', root, 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                       capture_output=True)
    if r.returncode != 0:
        raise Problem(f'{root} is not in a git repository: {r.stderr.decode("utf-8", "replace").strip()}')
    return os.fsdecode(r.stdout.rstrip(b'\n'))


def changes_from(root, base=None, changes=None):
    """[(status, paths)] as the gate reads them: with `base` (a full sha), the gate's own head_check.changed_paths from
    `base` to HEAD (every merge base, git's safe flags and no global config), after the gate's own Checkout.problem()
    (a repository whose grafts, shallow file, alternates or attributes change what git reads is refused, as the gate
    refuses it): never fewer paths than the gate sees. Else a file of `git diff --name-status -z -M` output."""
    if base:
        head = commit_of(root, 'HEAD')
        checkout = head_check.Checkout(git_dir(root), bare=(base, head))
        why = checkout.problem()
        if why:
            raise Problem(f'the repository at {root} cannot be read as the gate reads it ({why}): its paths could differ '
                          f'from the gate\'s; plan from a full clone without it, or give the changes with --changes')
        found, why = head_check.changed_paths(checkout, base, head)
        if found is None:
            raise Problem(why)
        return found
    try:
        if changes == '-':
            text = sys.stdin.buffer.read().decode('utf-8', 'surrogateescape')
        else:
            with open(changes, 'rb') as f:
                text = f.read().decode('utf-8', 'surrogateescape')
    except OSError as e:
        raise Problem(f'the changes file {changes} cannot be read: {e.strerror}') from None
    try:
        return review_judge.parse_name_status(text)
    except review_judge.JudgeError as e:
        raise Problem(str(e)) from None


def runner_text(settings, name):
    r = settings['lens_runners'].get(name) or settings['runner']
    return f'{r["model"]} {r["effort"]}'


def plan(root, settings, cfg, note, changes):
    """The plan as text lines: which lenses, run by whom, with which question, for how many rounds."""
    out = ['review plan']
    if note:
        out.append(f'note: {note}')
    review = cfg.review if cfg else None
    if review is None:
        weight, lenses, why, prompts = 'normal', (FALLBACK_LENS,), 'no review rules', {}
    else:
        try:
            j = review_judge.judge(review, changes, settings['queue_config'])
        except review_judge.JudgeError as e:
            raise Problem(f'the gate cannot judge this change: {e}') from None
        weight, lenses, why = j.weight, j.lenses, j.text()
        prompts = {x.name: x.prompt for x in review.lenses}
        if review.mode == 'shadow':
            out.append('note: the review block is in shadow: the gate only reports these lenses; its required-lenses '
                       'input still decides')
        out += [f'note: {w}' for w in review.warnings]
    out.append(f'weight: {weight}')
    if not lenses:
        out.append('lenses: none (light): CI only')
    else:
        how = ('independent: one new read-only agent each, no shared context, none sees another\'s report'
               if len(lenses) > 1 else 'one read-only agent')
        out.append(f'lenses: {", ".join(lenses)} ({how})')
        for n in lenses:
            prompt = prompts.get(n, 'the kit\'s built-in lens text (SKILL.md, "Lens prompt")')
            out.append(f'  {n}: prompt {prompt}; runner {runner_text(settings, n)}')
    out.append(f'why: {why}')
    out.append(f'state question (word for word in every lens prompt, answered on a `state:` line): '
               f'{settings["state_question"].strip()}')
    rounds = f'lens rounds: at most {settings["lens_rounds"]}; after that the orchestrator checks each fix diff itself'
    if review is not None:
        rounds += (f'; the gate caps rounds at {review.rounds.max} and asks for the lens '
                   f'{review.rounds.approach_lens!r} from round {review.rounds.approach_at}')
        if settings['lens_rounds'] > review.rounds.max:
            out.append(f'note: lens_rounds {settings["lens_rounds"]} is above the gate\'s rounds.max '
                       f'{review.rounds.max}: the gate fails the head first')
    out.append(rounds)
    out.append(f'stuck after the last lens round: one audit, {runner_text({**settings, "runner": settings["audit_runner"]}, "")}'
               f' ("good enough to merge / what next")')
    out.append(f'minors: {MINORS[settings["minors"]]}')
    out.append(f'findings: {"judged by the gate" if review is not None and review.findings else "not judged by the gate"}'
               f'; state line: {"required by config, not enforced by the gate yet" if review is not None and review.state_line else "kit check only"}')
    return out


# --- one comment, as the gate reads it ---------------------------------------------------------------------------

# What counts as a `state:` line is the gate's rule; the kit's `check` requires one (the gate does not enforce it yet).
has_state_line = review_judge.has_state_line


def reader(override_prefix):
    return lens.Review((), override_prefix, 'OWNER', ('OWNER',), log=lambda *_: None)


def verdict(body, override_prefix, head=None):
    """How the gate reads one lens comment by a counted author, as a list of words (COMMENTS.md, "Verdicts")."""
    review = reader(override_prefix)
    post = {'body': body, 'authorAssociation': 'OWNER', 'includesCreatedEdit': False, 'createdAt': '1'}
    if review.override_sha(post):
        return ['override']
    kind = review.report_problem(post)[1]   # the gate's one rule for a comment that looks like a report (issue #865)
    if kind == lens.BROKEN:
        return ['reads nothing']
    if kind == lens.HELD:   # no lens marker, but an open finding: it blocks until deleted or `not a lens:`
        return ['reads nothing', 'no lens marker']
    if kind == lens.NOT_LENS:   # no lens marker and no open finding: no report, never a read or a block
        return ['not a lens', 'no lens marker']
    header = lens.lens_header(body)
    if not header:
        return ['not a lens']
    name, sha = header
    words = [f'lens {lens.lens_key(name)}']
    marker = review_judge.round_marker(name)
    if marker:
        words.append(f'round {marker}')
    if not lens.FULL_SHA.fullmatch(sha or ''):
        return words + ['short sha' if sha else 'no sha']
    words.append('read')
    if head and sha != head.lower():
        words.append('not this head')
    found, section = review_judge.read_findings(body)
    kinds = [k for k in review_judge.BLOCKING if any(kind == k for kind, _ in found)]
    words += kinds or (['clean'] if section else ['no findings section'])
    words.append('state' if has_state_line(body) else 'no state')
    return words


GOOD = {'read', 'clean', 'state'}


def check(body, override_prefix, head=None):
    """(exit code, text): 0 only for a read of the head (when given) with a findings section, no blocker or major, and a
    state line."""
    words = verdict(body, override_prefix, head)
    ok = GOOD <= set(words) and 'not this head' not in words
    return (0 if ok else 1), ', '.join(words)


# --- COMMENTS.md: every example reads as it says -----------------------------------------------------------------

DOC = os.path.join(KIT, 'COMMENTS.md')
PREFIX_RE = re.compile(r'The examples use the override prefix `([^`]+)`\.')
SAYS_RE = re.compile(r'The gate reads: `([^`]+)`\s*$')
OPEN_RE = re.compile(r'(`{3,}|~{3,})text\s*$')


def doc_examples(text):
    """(override prefix, [(line number, stated verdict, comment body)]); Problem when the doc's own shape is broken: an
    example fence with no stated verdict before it, a stated verdict with no example after it, no prefix line."""
    m = PREFIX_RE.search(text)
    if not m:
        raise Problem('COMMENTS.md does not say which override prefix its examples use')
    lines, out, i, said = text.splitlines(), [], 0, None
    while i < len(lines):
        s = SAYS_RE.match(lines[i])
        o = OPEN_RE.match(lines[i])
        if s:
            if said:
                raise Problem(f'COMMENTS.md line {said[0]}: a stated verdict with no example after it')
            said = (i + 1, s.group(1))
        elif o:
            if not said:
                raise Problem(f'COMMENTS.md line {i + 1}: an example with no `The gate reads:` line before it')
            fence, body, i = o.group(1), [], i + 1
            while i < len(lines) and lines[i].strip() != fence:
                body.append(lines[i])
                i += 1
            if i == len(lines):
                raise Problem(f'COMMENTS.md line {said[0]}: the example is never closed')
            out.append((said[0], said[1], '\n'.join(body)))
            said = None
        i += 1
    if said:
        raise Problem(f'COMMENTS.md line {said[0]}: a stated verdict with no example after it')
    return m.group(1), out


def doc_problems(text):
    """Every example of COMMENTS.md whose stated verdict is not what the gate's parser reads."""
    prefix, examples = doc_examples(text)
    if not examples:
        raise Problem('COMMENTS.md has no examples')
    out = []
    for line, said, body in examples:
        got = ', '.join(verdict(body, prefix))
        if got != said:
            out.append(f'COMMENTS.md line {line}: says `{said}`, the gate reads `{got}`')
    return out


# --- command line ------------------------------------------------------------------------------------------------

def _args(argv, flags, positional=0):
    opts, rest = {}, []
    it = iter(argv)
    for a in it:
        if a in flags:
            v = next(it, None)
            if v is None:
                raise Problem(f'{a} needs a value')
            opts[a] = v
        elif a.startswith('--') and a != '-':
            raise Problem(f'unknown option {a}')
        else:
            rest.append(a)
    if len(rest) > positional:
        raise Problem(f'unexpected argument {rest[positional]}')
    return opts, rest


def main(argv, out=sys.stdout):
    if not argv or argv[0] not in ('plan', 'check', 'doc'):
        print(__doc__.split('\n\n')[2], file=out)
        return 2
    cmd, argv = argv[0], argv[1:]
    if cmd == 'doc':
        _, rest = _args(argv, (), 1)
        text = read_text(rest[0] if rest else DOC, 'the comment format doc')
        if text is None:
            raise Problem(f'{rest[0] if rest else DOC} does not exist')
        problems = doc_problems(text)
        for p in problems:
            print(p, file=out)
        if not problems:
            print(f'ok: every example reads as it says ({len(doc_examples(text)[1])} examples)', file=out)
        return 1 if problems else 0
    opts, rest = _args(argv, ('--root', '--settings', '--base', '--changes', '--head'), 1 if cmd == 'check' else 0)
    if cmd == 'plan' and ('--base' in opts) == ('--changes' in opts):
        raise Problem('plan needs exactly one of --base REF or --changes FILE (git diff --name-status -z -M output)')
    root = opts.get('--root', '.')
    settings, note = load_settings(root, opts.get('--settings'))
    base = commit_of(root, opts['--base']) if '--base' in opts else None
    cfg, cfg_note = load_queue_config(root, settings, base)
    others = other_settings(root) if cfg is None and note else []
    if others:
        cfg_note += (f'; {", ".join(others)} looks like kit settings under another name: if it is this project\'s, '
                     f'pass --settings {others[0]}')
    if cmd == 'check':
        if not rest:
            raise Problem('check needs the comment: a file, or - for stdin')
        if rest[0] == '-':
            body = sys.stdin.read()
        else:
            body = read_text(rest[0], 'the comment')
            if body is None:
                raise Problem(f'the comment file {rest[0]} does not exist')
        if cfg is None:
            print(f'note: {cfg_note}; override comments are not recognised', file=out)
        code, text = check(body, cfg.override_prefix if cfg else '\0', opts.get('--head'))
        print(text, file=out)
        return code
    changes = changes_from(root, base, opts.get('--changes'))
    for line in plan(root, settings, cfg, '; '.join(x for x in (note, cfg_note) if x), changes):
        print(line, file=out)
    return 0


def run(argv, out=sys.stdout, err=sys.stderr):
    try:
        return main(argv, out)
    except Problem as e:
        print(f'review-kit: {e}', file=err)
        return 2


if __name__ == '__main__':
    sys.exit(run(sys.argv[1:]))
