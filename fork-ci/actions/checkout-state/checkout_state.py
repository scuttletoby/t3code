#!/usr/bin/env python3
"""No job on a self-hosted runner leaves checkout state in the runner's work folder for the next job (fork-ci
checkout-state; usage, config and what it does not catch: README.md next to this file).
A self-hosted runner keeps one work folder per repository (`_work/<repo>/<repo>`) and every job of that repository on it
checks out into the same folder. actions/checkout cleans the files (`git clean -ffdx`, `reset --hard`) but not every
setting an earlier checkout wrote into `.git`: a non-cone sparse checkout's local `core.sparseCheckout=true` outlives the
next job's `git sparse-checkout disable`, so every later job on that runner checks out sparse.
So, on any runner that is not one of GitHub's fresh machines, a checkout may not use the options that leave state the
next job did not ask for: `sparse-checkout` (sparse config and the pattern file: later jobs miss files), `filter` (a
partial clone: the promisor remote and its filter stay) and `clean` other than true (this job starts on an earlier job's
untracked files). A `path:` does not make it safe: actions/checkout only accepts a path inside the shared work folder.
A `run:` step that sets up a sparse or partial clone is refused the same way, and so is such a command in any `with:`
input of a step other than actions/checkout, or of a reusable workflow's job (an action may run an input as a command or
script: actions/github-script's `script`, a reviewed action's `run`, a local composite's `${{ inputs.cmd }}`).
A local composite action (`uses: ./x`) is read, its steps checked the same way; a local action that is not composite,
or not there, is refused (its steps cannot be read). An action from another repository (`uses: owner/repo[/path]@ref`,
or `docker://`) runs steps this cannot read: it needs a reviewed "actions" entry with the exact `uses:` (a new pin is
read again) and why it leaves nothing behind. GitHub's own actions (`actions/...`) are trusted apart from their inputs;
actions/checkout's are checked as above.
A job that calls a reusable workflow on a shared runner runs steps this cannot read: it needs a reviewed "reusable"
entry with the exact `uses:` and why it leaves nothing behind. Such a job counts as shared whatever it hands the called
workflow (an input named runs-on is only a value the callee may ignore), unless its entry names the input the reviewed
workflow really runs its jobs on ("runner-input"): then that input's value decides, as a job's runs-on would.
This lint stops a merge; a folder a job broke anyway (a pull request's own CI, a re-run of an old branch) is healed on
the runner (fork-ci git-clean as a job's first step, or a job-started hook on the runner host). The workflow files are
read as GitHub reads them (YAML 1.2: only true/false are booleans, so `clean: yes` is the string GitHub hands the
action, not true). A runs-on (or reviewed runner-input) that is not a label, a list of labels or a runner group is
reported, not trusted.
Fails closed (exit 2, nothing scanned): a config that cannot be read or is not understood (a key a newer release
added too), a config path that is a broken link, a root with no workflow file, no PyYAML.
Config (optional; default .github/checkout-state.json under --root):
  {"workflows": ["<glob relative to the root>", ...],   # scanned besides .github/workflows/*.yml|yaml
   "reusable": [{"workflow": "<path>", "job": "<job id>", "uses": "<exact uses:>", "why": "<review>",
                 "runner-input": "<input the called workflow's jobs run on; optional>"}, ...],
   "actions": [{"uses": "<exact uses: of an action from another repository>", "why": "<review>"}, ...]}
Run: python3 -I checkout_state.py [--root DIR] [--config FILE]   (exit 0 clean, 1 problems, 2 cannot judge)
"""
import argparse, glob, json, os, re, sys

try:
    import yaml
except ImportError:   # main() says so and exits 2: no runner may read "no PyYAML" as "clean"
    yaml = None

DEFAULT_CONFIG = os.path.join('.github', 'checkout-state.json')
ALWAYS = ('.github/workflows/*.yml', '.github/workflows/*.yaml')
# GitHub's own runners: a fresh machine per job, so nothing carries over. Anything else (a self-hosted label, an
# expression that picks the runner, a reusable workflow's input no review says it runs on) counts as shared.
HOSTED = re.compile(r'(ubuntu|windows|macos)-[\w.-]+')
# A run step that makes the work folder's clone sparse or partial.
RUN_STATE = re.compile(r'\bsparse-checkout\b|core\.sparsecheckout|extensions\.partialclone|\.promisor\b|'
                       r'\.partialclonefilter\b|\bgit\b[^\n]*--filter[= ]', re.I)   # git config keys ignore case
BOOL = 'tag:yaml.org,2002:bool'


if yaml is not None:
    class Yaml12Loader(yaml.SafeLoader):
        """PyYAML's safe loader with YAML 1.2's booleans: `yes`, `on`, `y` ... stay strings, as GitHub reads them."""

    Yaml12Loader.yaml_implicit_resolvers = {
        first: [(tag, rx) for tag, rx in resolvers if tag != BOOL]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()}
    Yaml12Loader.add_implicit_resolver(BOOL, re.compile(r'^(?:true|True|TRUE|false|False|FALSE)$'), list('tTfF'))


def load_yaml(path):
    with open(path, encoding='utf-8') as f:
        return yaml.load(f, Loader=Yaml12Loader)   # noqa: S506 (a SafeLoader subclass)


def parse_config(raw):
    """The config as {'workflows': [...], 'reusable': [...], 'actions': [...]}; ValueError for anything this does not
    understand."""
    if not isinstance(raw, dict):
        raise ValueError('the config must be a JSON object')
    unknown = set(raw) - {'about', 'workflows', 'reusable', 'actions'}
    if unknown:
        raise ValueError(f'unknown config keys: {sorted(unknown)}')
    globs = raw.get('workflows', [])
    if not isinstance(globs, list) or not all(isinstance(g, str) and g for g in globs):
        raise ValueError('"workflows" must be a list of globs')
    for g in globs:
        if os.path.isabs(g) or '..' in g.replace('\\', '/').split('/'):
            raise ValueError(f'workflow glob {g!r} must stay inside the root')
    reusable = raw.get('reusable', [])
    if not isinstance(reusable, list):
        raise ValueError('"reusable" must be a list')
    for e in reusable:
        if not isinstance(e, dict) or not {'workflow', 'job', 'uses', 'why'} <= set(e) <= \
                {'workflow', 'job', 'uses', 'why', 'runner-input'} or \
                not all(isinstance(v, str) and v.strip() for v in e.values()):
            raise ValueError(f'a reusable entry needs exactly workflow, job, uses and a why (and may name a '
                             f'runner-input), all non-empty: {e!r}')
    actions = raw.get('actions', [])
    if not isinstance(actions, list):
        raise ValueError('"actions" must be a list')
    for e in actions:
        if not isinstance(e, dict) or set(e) != {'uses', 'why'} or \
                not all(isinstance(v, str) and v.strip() for v in e.values()):
            raise ValueError(f'an actions entry needs exactly uses and a why, both non-empty: {e!r}')
        if not remote_action(e['uses']):
            raise ValueError(f'an actions entry is for an action from another repository (not ./, not actions/...): '
                             f'{e["uses"]!r}')
    if len({e['uses'] for e in actions}) != len(actions):
        raise ValueError('an actions entry is listed twice')
    return {'workflows': list(globs), 'reusable': list(reusable), 'actions': list(actions)}


def load_config(root, path=None):
    """The config at `path`, or at the root's default place; no default file is an empty config."""
    if path is None:
        path = os.path.join(root, DEFAULT_CONFIG)
        if not os.path.lexists(path):   # a broken link is a config that is gone, not "no config": refused below
            return parse_config({})
    try:
        with open(path, encoding='utf-8') as f:
            return parse_config(json.load(f))
    except OSError as e:
        why = 'a broken link' if os.path.islink(path) and not os.path.exists(path) else e
        raise ValueError(f'cannot read {path}: {why}') from e


def remote_action(uses):
    """True for a step's `uses:` that runs an action from another repository this cannot read (not ./x, not GitHub's
    own actions/...)."""
    uses = uses.strip()
    return bool(uses) and not uses.startswith('./') and not uses.lower().startswith('actions/')


def runner(job, entry=None):
    """What picks the job's runner: its runs-on, or for a reusable workflow's job the value of its reviewed entry's
    runner-input (None when there is none)."""
    if 'uses' not in job:
        return job.get('runs-on')
    name = (entry or {}).get('runner-input')
    w = job.get('with')
    w = {str(k).lower(): v for k, v in w.items()} if isinstance(w, dict) else {}
    return w.get(name.lower()) if name else None   # inputs of workflow_call ignore case too


def bad_runner(r):
    """Why a runs-on value is no runner GitHub accepts (true, a number, a list holding one), or None."""
    if r is None or isinstance(r, (str, dict)) or isinstance(r, list) and all(isinstance(x, str) for x in r):
        return None
    return f'runs-on {r!r} is not a label, a list of labels or a runner group'


def shared_runner(job, entry=None):
    """True unless the job surely runs on one of GitHub's fresh machines. A reusable workflow's job runs where the
    called workflow says: only its reviewed entry's runner-input names an input that really picks the runner."""
    r = runner(job, entry)
    if isinstance(r, dict) or bad_runner(r):   # {group:, labels:} is a runner group: self-hosted or larger runners
        return True
    labels = [r] if isinstance(r, str) else list(r or [])
    return not labels or not all(isinstance(x, str) and HOSTED.fullmatch(x) for x in labels)


def run_state(text):
    """The first command in `text` that makes the work folder's clone sparse or partial, or None."""
    m = RUN_STATE.search(re.sub(r'\\\r?\n', ' ', text))   # lines continued with a backslash
    return m.group(0) if m else None


def input_state(w):
    """Problems for `with:` inputs (raw mapping) whose value runs such a command: an action may run an input."""
    out = []
    for k, v in (w.items() if isinstance(w, dict) else ()):
        hit = None if v is None else run_state(str(v))
        if hit:
            out.append(f'with {k}: {hit!r}')
    return out


def local_action(root, uses):
    """(folder relative to the root, action) of a `uses: ./x` step, or (None, why it cannot be read)."""
    if root is None:
        return None, 'a local action, not read here'
    real_root = os.path.realpath(root)
    path = os.path.realpath(os.path.join(real_root, uses[2:]))
    if os.path.commonpath([real_root, path]) != real_root:
        return None, 'outside the repository'
    for name in ('action.yml', 'action.yaml'):
        f = os.path.join(path, name)
        if os.path.isfile(f):
            try:
                action = load_yaml(f)
            except (OSError, yaml.YAMLError) as e:
                return None, f'{name} cannot be read: {e}'.splitlines()[0]
            return os.path.relpath(path, real_root), action if isinstance(action, dict) else {}
    return None, 'no action.yml or action.yaml there'


def problems(job, root=None, _read=None, actions=(), used=None):
    """What in this job's (or composite action's) steps would leave checkout state in a shared work folder. `actions`
    holds the reviewed `uses:` of actions from other repositories; each one met is added to `used`."""
    read = set() if _read is None else _read
    out = []
    for i, step in enumerate(job.get('steps') or []):
        if not isinstance(step, dict):
            continue
        raw_uses = str(step.get('uses') or '').strip()
        uses = raw_uses.lower()   # GitHub accepts Actions/Checkout too
        w = step.get('with')
        # the runner hands inputs to the action by name, ignoring case; getInput trims, and an empty input is unset
        w = {str(k).lower(): ('' if v is None else str(v).strip()) for k, v in w.items()} if isinstance(w, dict) else {}
        if uses.startswith('actions/checkout@'):
            if w.get('sparse-checkout'):   # any value, `false` too: the action reads it as patterns
                out.append(f'step {i}: sparse-checkout')
            if w.get('filter'):
                out.append(f'step {i}: filter')
            # actions/checkout: (getInput('clean') || 'true').toUpperCase() === 'TRUE'
            if (w.get('clean') or 'true').upper() != 'TRUE':
                out.append(f'step {i}: clean: {w.get("clean")!r}')
        elif raw_uses.startswith('./'):
            folder, action = local_action(root, raw_uses)
            if folder is None:
                out.append(f'step {i}: {raw_uses}: {action} (a local action on a shared runner must be a composite '
                           f'action this can read)')
            elif folder not in read:
                read.add(folder)
                runs = action.get('runs') or {}
                using = runs.get('using') if isinstance(runs, dict) else None
                if using != 'composite':
                    out.append(f'step {i}: {raw_uses}: not a composite action (using: {using}), its steps cannot be read')
                else:
                    out += [f'step {i}: {raw_uses} {p}' for p in problems(runs, root, read, actions, used)]
        elif remote_action(raw_uses):
            if raw_uses in actions:
                if used is not None:
                    used.add(raw_uses)
            else:
                out.append(f'step {i}: action {raw_uses} from another repository (its steps are not checked: add a '
                           f'reviewed "actions" entry to the config)')
        if not uses.startswith('actions/checkout@'):   # actions/checkout's inputs are read above
            out += [f'step {i}: {p}' for p in input_state(step.get('with'))]
        hit = run_state(str(step.get('run') or ''))
        if hit:
            out.append(f'step {i}: run {hit!r}')
    return out


def workflows(root, config):
    seen = set()
    for pattern in ALWAYS + tuple(config['workflows']):
        for path in sorted(glob.glob(os.path.join(root, pattern))):
            rel = os.path.relpath(path, root).replace(os.sep, '/')
            if rel not in seen:
                seen.add(rel)
                yield rel, path


def scan(root, config):
    """(problems, number of jobs on a shared runner, folders seen) over the root's workflows; no folder seen means no
    workflow file was found."""
    bad, seen, dirs = [], 0, set()
    listed = {(e['workflow'], e['job'], e['uses']): e for e in config['reusable']}
    actions = {e['uses'] for e in config['actions']}
    used, used_actions = set(), set()
    for rel, path in workflows(root, config):
        dirs.add(os.path.dirname(rel))
        try:
            wf = load_yaml(path)
        except (OSError, yaml.YAMLError) as e:
            bad.append(f'{rel}: cannot be read as YAML: {e}'.splitlines()[0])
            continue
        jobs = (wf or {}).get('jobs') if isinstance(wf, dict) else None
        for name, job in (jobs or {}).items() if isinstance(jobs, dict) else ():
            if not isinstance(job, dict):
                continue
            key = (rel, name, str(job.get('uses')))
            if 'uses' in job and key in listed:
                used.add(key)   # its review is read even when the job runs on a fresh machine
            why = bad_runner(runner(job, listed.get(key)))
            if why:
                bad.append(f'{rel}/{name}: {why}')
            if not shared_runner(job, listed.get(key)):
                continue
            seen += 1
            bad += [f'{rel}/{name}: {p}' for p in problems(job, root, actions=actions, used=used_actions)]
            if 'uses' in job:
                bad += [f'{rel}/{name}: {p}' for p in input_state(job.get('with'))]
                if key not in listed:
                    bad.append(f'{rel}/{name}: reusable workflow {job["uses"]} (its steps are not checked: add a '
                               f'reviewed "reusable" entry to the config)')
    bad += [f'config: reusable entry {w}/{j} {u}: nothing uses it (remove it)'
            for w, j, u in sorted(set(listed) - used)]
    bad += [f'config: actions entry {u}: no job on a shared runner uses it (remove it)'
            for u in sorted(actions - used_actions)]
    return bad, seen, dirs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    ap.add_argument('--root', default='.', help='the repository (default: the current folder)')
    ap.add_argument('--config', help=f'the config (default: <root>/{DEFAULT_CONFIG}, if there)')
    args = ap.parse_args(argv)
    if yaml is None:
        print('checkout_state: cannot judge: PyYAML is not installed for this python3 (install python3-yaml on the '
              'runner; GitHub\'s Ubuntu images have it)')
        return 2
    if not os.path.isdir(args.root):
        print(f'checkout_state: cannot judge: the root {args.root!r} is not a folder')
        return 2
    try:
        config = load_config(args.root, args.config)
    except ValueError as e:
        print(f'checkout_state: bad config: {e}')
        return 2
    bad, seen, dirs = scan(args.root, config)
    if not dirs:   # a wrong root or an empty checkout must not read as "clean"
        where = ', '.join(ALWAYS + tuple(config['workflows']))
        print(f'checkout_state: cannot judge: no workflow file under {args.root!r} ({where}): a wrong root or an '
              f'empty checkout?')
        return 2
    for p in bad:
        print(p)
    print(f'checkout_state: {seen} jobs on shared runners, {len(bad)} problems'
          + (' (each leaves checkout state for the next job on that runner)' if bad else ''))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
