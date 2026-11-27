"""owner-questions: lists a repository's open owner questions and, in label mode, keeps the rule's label on exactly
the ones still waiting.

An issue asks the owner when it carries the rule's label or its title starts with one of the rule's prefixes (or
`<Topic> Q<n>:`); a comment starting with the decision prefix, then `:` or ` (`, answers it. This is a copy of
packages/contracts/src/forkOwnerQuestions.ts (T3's pages and tools); rule-cases.json holds the cases both copies run.
The project's config file (default `.t3code/issues.json`, optional) may name the repositories (`repositories`) and
rename the rule (`ownerQuestions`). GitHub only, through `gh api` with GH_TOKEN. Python standard library only.
Usage: README.md next to this file.
"""
import argparse, json, os, re, subprocess, sys, urllib.parse

DEFAULT_RULE = {
    'label': 'status:needs-owner-decision',
    'titlePrefixes': ['Owner question'],
    'topicQuestionTitles': True,
    'decisionPrefix': 'Owner decision',
}
# A topic of a few words, then ` Q<n>:` (the same bound as the TypeScript rule).
TOPIC_QUESTION = re.compile(r'^[^\s:][^:\n]{0,80} Q\d{1,3}:')
REPOSITORY = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
MODES = ('list', 'label')


class ConfigError(Exception):
    pass


def _starts_with_word(text, prefix):
    if text[:len(prefix)].lower() != prefix.lower():
        return False
    # `Owner question:` asks; `Owner questions are...` does not.
    after = text[len(prefix):len(prefix) + 1]
    return not (after.isalpha() or after.isdigit() or after.isnumeric())


def is_owner_question(title, labels, rule):
    label = rule['label'].lower()
    if any(name.lower() == label for name in labels):
        return True
    title = title.strip()
    if any(_starts_with_word(title, prefix) for prefix in rule['titlePrefixes']):
        return True
    return bool(rule['topicQuestionTitles'] and TOPIC_QUESTION.match(title))


def _drop_bold(text):
    return text[2:] if text.startswith('**') or text.startswith('__') else text


def is_owner_decision(body, rule):
    text = _drop_bold(body.lstrip())
    prefix = rule['decisionPrefix']
    if text[:len(prefix)].lower() != prefix.lower():
        return False
    rest = _drop_bold(text[len(prefix):])
    return rest.startswith(':') or rest.startswith(' (')


def load_config(path, default_repo):
    """The repositories and the rule: the file's, or this repository and the defaults when it is absent."""
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except FileNotFoundError:
        return [default_repo], dict(DEFAULT_RULE)
    except (OSError, ValueError) as e:
        raise ConfigError(f'{path} could not be read: {e}') from None
    if not isinstance(raw, dict):
        raise ConfigError(f'{path}: the file must hold one JSON object')
    repos = raw.get('repositories', [default_repo])
    if not isinstance(repos, list) or not all(isinstance(r, str) and REPOSITORY.match(r) for r in repos):
        raise ConfigError(f'{path}: repositories must be a list of owner/repo names')
    rule = dict(DEFAULT_RULE)
    override = raw.get('ownerQuestions', {})
    if not isinstance(override, dict):
        raise ConfigError(f'{path}: ownerQuestions must be an object')
    for key, value in override.items():
        if key not in DEFAULT_RULE:
            raise ConfigError(f'{path}: ownerQuestions.{key} is not a rule key ({", ".join(DEFAULT_RULE)})')
        ok = (isinstance(value, bool) if key == 'topicQuestionTitles'
              else isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)
              if key == 'titlePrefixes' else isinstance(value, str) and value.strip() != '')
        if not ok:
            raise ConfigError(f'{path}: ownerQuestions.{key} has the wrong type')
        rule[key] = value
    return list(dict.fromkeys(repos)), rule


def gh_run(*args, input=None):
    return subprocess.run(['gh', *args], input=input, text=True, capture_output=True)


def _api_lines(gh, path, jq):
    r = gh('api', '--paginate', path, '--jq', jq)
    if r.returncode:
        raise RuntimeError(f'gh api {path} failed: {(r.stderr or "").strip()[:300]}')
    return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]


def scan(gh, repo, rule):
    """Every open owner question of the repository (all pages, pull requests left out), with whether it is answered."""
    issues = _api_lines(
        gh, f'repos/{repo}/issues?state=open&per_page=100',
        '.[] | select(.pull_request | not) | {number, title, url: .html_url, comments,'
        ' labels: [.labels[].name]} | @json')
    found = []
    for issue in issues:
        if not is_owner_question(issue['title'], issue['labels'], rule):
            continue
        answered = issue['comments'] > 0 and any(
            is_owner_decision(body or '', rule) for body in _api_lines(
                gh, f'repos/{repo}/issues/{issue["number"]}/comments?per_page=100', '.[] | .body | @json'))
        labelled = any(name.lower() == rule['label'].lower() for name in issue['labels'])
        found.append({'repo': repo, 'number': issue['number'], 'title': issue['title'], 'url': issue['url'],
                      'labelled': labelled, 'answered': answered})
    return found


def relabel(gh, question, rule):
    """label mode: the label on a waiting question, off an answered one. Returns what it did, or None."""
    repo, number = question['repo'], question['number']
    if not question['answered'] and not question['labelled']:
        r = gh('api', '-X', 'POST', f'repos/{repo}/issues/{number}/labels', '--input', '-',
               input=json.dumps({'labels': [rule['label']]}))
        action = 'labelled'
    elif question['answered'] and question['labelled']:
        r = gh('api', '-X', 'DELETE', f'repos/{repo}/issues/{number}/labels/{urllib.parse.quote(rule["label"], safe="")}')
        action = 'unlabelled'
    else:
        return None
    if r.returncode:
        raise RuntimeError(f'could not change the label on {repo}#{number}: {(r.stderr or "").strip()[:300]}')
    return action


def escape(text):
    """One log or summary line: nothing in an issue title can start a workflow command or break the table."""
    return str(text).replace('%', '%25').replace('\r', ' ').replace('\n', ' ').replace('|', '\\|')


def summary(waiting, changes, rule, mode):
    lines = [f'### Owner questions waiting: {len(waiting)}', '']
    if waiting:
        lines += ['| Issue | Title |', '| --- | --- |']
        lines += [f'| [{q["repo"]}#{q["number"]}]({q["url"]}) | {escape(q["title"])} |' for q in waiting]
    else:
        lines.append('None.')
    if mode == 'label':
        lines += ['', f'Label `{rule["label"]}`: ' + (', '.join(f'{what} {q["repo"]}#{q["number"]}'
                                                       for q, what in changes) or 'no change') + '.']
    return '\n'.join(lines) + '\n'


def append(path, text):
    if path:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(text)


def cli(argv=None, env=None, gh=None, out=print):
    env, gh = os.environ if env is None else env, gh or gh_run
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--config', default='.t3code/issues.json')
    p.add_argument('--repo', required=True, help='this repository, owner/name (used when the config names none)')
    p.add_argument('--mode', choices=MODES, default='list')
    a = p.parse_args(argv)
    try:
        repos, rule = load_config(a.config, a.repo)
        questions = [q for repo in repos for q in scan(gh, repo, rule)]
        waiting = [q for q in questions if not q['answered']]
        changes = []
        if a.mode == 'label':
            for q in questions:
                what = relabel(gh, q, rule)
                if what is not None:
                    changes.append((q, what))
    except (ConfigError, RuntimeError) as e:
        out(f'::error::owner-questions: {escape(e)}')
        return 1
    for q in waiting:
        out(f'waiting: {q["repo"]}#{q["number"]} {escape(q["title"])}')
    for q, what in changes:
        out(f'{what}: {q["repo"]}#{q["number"]}')
    append(env.get('GITHUB_STEP_SUMMARY'), summary(waiting, changes, rule, a.mode))
    keep = ('repo', 'number', 'title', 'url', 'labelled')
    append(env.get('GITHUB_OUTPUT'),
           f'count={len(waiting)}\nchanged={len(changes)}\n'
           f'questions={json.dumps([{k: q[k] for k in keep} for q in waiting], separators=(",", ":"))}\n')
    return 0


if __name__ == '__main__':
    sys.exit(cli())
