"""Which gh commands gh-as runs, read the way gh's flag parser reads them, and the guard.

One table, used twice: the wrapper (as the agent) uses it to find file flags and pass the file on stdin; the
privileged gh-as uses it to refuse everything that is not in the table. Fails closed: an unknown family, sub-command
or flag is refused, never guessed.

Flag specs, one word each: `[s/]long` plus a kind:
  (none) on/off flag      `=` takes a value      `:jq` a jq expression (refused if it can read the environment)
  `:file` a file (only `-` passes the privileged side; the wrapper turns a path into stdin)
  `:field` key=value where `key=@file` reads a file (only `@-` passes; the wrapper turns a path into stdin)
  `:repo` must be the app's repository      `!reason` always refused
"""
import re

from .common import Refused, norm_repo

READ = 'q/jq:jq json= t/template='
WEB = 'w/web!opens-a-browser'
COMMANDS = {
    'pr list': f'app= a/assignee= A/author= B/base= d/draft H/head= l/label= L/limit= S/search= s/state= {READ} {WEB}',
    'pr view': f'c/comments {READ} {WEB}',
    'pr create': 'a/assignee= attach!reads-files B/base= b/body= F/body-file:file d/draft dry-run e/editor!opens-an-editor '
                 'f/fill!runs-git fill-first!runs-git fill-verbose!runs-git H/head= l/label= m/milestone= '
                 'no-maintainer-edit p/project= recover!reads-a-file r/reviewer= T/template!reads-a-file t/title= '
                 + WEB,
    'pr edit': 'add-assignee= add-label= add-project= add-reviewer= attach!reads-files B/base= b/body= '
               'F/body-file:file m/milestone= remove-assignee= remove-label= remove-milestone remove-project= '
               'remove-reviewer= t/title=',
    'pr comment': 'attach!reads-files b/body= F/body-file:file create-if-none delete-last edit-last '
                  f'e/editor!opens-an-editor yes {WEB}',
    'pr review': 'a/approve b/body= F/body-file:file c/comment r/request-changes',
    'pr merge': 'admin!bypasses-branch-rules A/author-email= auto b/body= F/body-file:file d/delete-branch '
                'disable-auto match-head-commit= m/merge r/rebase s/squash t/subject=',
    'pr close': 'c/comment= d/delete-branch',
    'pr reopen': 'c/comment=',
    'pr ready': 'undo',
    'pr checks': f'fail-fast i/interval= required watch {READ} {WEB}',
    'pr diff': f'color= e/exclude= name-only patch {WEB}',
    'pr status': f'c/conflict-status {READ}',
    'pr update-branch': 'rebase',
    'issue list': f'app= a/assignee= A/author= l/label= L/limit= mention= m/milestone= S/search= s/state= type= '
                  f'{READ} {WEB}',
    'issue view': f'c/comments {READ} {WEB}',
    'issue create': 'a/assignee= attach!reads-files blocked-by= blocking= b/body= F/body-file:file '
                    'e/editor!opens-an-editor l/label= m/milestone= parent= p/project= recover!reads-a-file '
                    f'T/template!reads-a-file t/title= type= {WEB}',
    'issue edit': 'add-assignee= add-blocked-by= add-blocking= add-label= add-project= add-sub-issue= '
                  'attach!reads-files b/body= F/body-file:file m/milestone= parent= remove-assignee= '
                  'remove-blocked-by= remove-blocking= remove-label= remove-milestone remove-parent remove-project= '
                  'remove-sub-issue= remove-type t/title= type=',
    'issue comment': 'attach!reads-files b/body= F/body-file:file create-if-none delete-last edit-last '
                     f'e/editor!opens-an-editor yes {WEB}',
    'issue close': 'c/comment= duplicate-of= r/reason=',
    'issue reopen': 'c/comment=',
    'run list': f'a/all b/branch= c/commit= created= e/event= L/limit= s/status= u/user= w/workflow= {READ}',
    'run view': f'a/attempt= exit-status j/job= log log-failed v/verbose!prints-request-details {READ} {WEB}',
    'run watch': 'compact exit-status i/interval=',
    'run rerun': 'd/debug failed j/job=',
    'run cancel': 'force',
    'workflow list': f'a/all L/limit= {READ}',
    'workflow view': f'r/ref= y/yaml {WEB}',
    'workflow run': 'F/field:field json f/raw-field= r/ref=',
    'workflow enable': '',
    'workflow disable': '',   # in the table so the guard can say why it is refused
    'api': 'cache= F/field:field H/header= hostname!other-hosts i/include input:file q/jq:jq X/method= paginate '
           'p/preview= f/raw-field= silent slurp t/template= verbose!prints-request-details',
    'release list': f'exclude-drafts exclude-pre-releases L/limit= O/order= {READ}',
    'release view': f'{READ} {WEB}',
    'label list': f'L/limit= order= S/search= sort= {READ} {WEB}',
    'label create': 'c/color= d/description= f/force',
    'label edit': 'c/color= d/description= n/name=',
    'repo view': f'b/branch= {READ} {WEB}',
    'cache list': f'k/key= L/limit= O/order= r/ref= S/sort= {READ}',
}
NO_REPO_FLAG = {'api', 'repo view'}   # gh has no -R here (api uses {owner}/{repo} from GH_REPO)
REFUSED_FAMILIES = {
    'auth': 'gh-as gives gh its own token; auth commands are refused',
    'extension': 'extensions run other programs', 'alias': 'aliases are refused', 'config': 'gh config is refused',
    'secret': 'secrets are owner work', 'variable': 'variables are owner work', 'ssh-key': 'owner work',
    'gpg-key': 'owner work', 'codespace': 'not this repository', 'gist': 'not this repository',
    'browse': 'opens a browser',
}
REFUSED_SUBS = {
    'pr checkout': 'runs git', 'repo clone': 'runs git', 'issue develop': 'runs git',
    'run download': 'writes files', 'release download': 'writes files', 'release upload': 'reads files',
    'repo delete': 'owner work', 'repo edit': 'owner work', 'repo rename': 'owner work',
    'repo archive': 'owner work', 'workflow disable': None,
}

# jq in gh can read the environment ($ENV, env), which holds the token.
JQ_ENV_RE = re.compile(r'\$ENV|\benv\b|\$__prog|\binput_filename\b|\b(?:import|include)\b')
URL_RE = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*://|^//')


def _parse_spec(spec):
    out = {}
    for word in spec.split():
        reason = None
        if '!' in word:
            word, reason = word.split('!', 1)
            kind = 'refuse'
        elif word.endswith('='):
            word, kind = word[:-1], 'value'
        elif ':' in word:
            word, kind = word.split(':', 1)
        else:
            kind = 'bool'
        short, _, long = word.rpartition('/')
        entry = (kind, '--' + long, reason.replace('-', ' ') if reason else None)
        for key in ['--' + long] + (['-' + short] if short else []):
            if key in out:
                raise ValueError(f'gh-apps policy table: {key} twice in {spec!r}')
            out[key] = entry
    return out


TABLE = {cmd: _parse_spec(f'{spec} help' + ('' if cmd in NO_REPO_FLAG else ' R/repo:repo'))
         for cmd, spec in COMMANDS.items()}


class Parsed:
    def __init__(self, family, flags, positionals, files, stdin_uses):
        self.family = family            # 'pr view', 'api', ...
        self.flags = flags              # [(long name, value or None)]
        self.positionals = positionals
        self.files = files              # [(index in args, prefix, path)] file values that are not '-'
        self.stdin_uses = stdin_uses    # how many flags read stdin

    def values(self, long):
        return [v for f, v in self.flags if f == long]

    def has(self, long):
        return any(f == long for f, _ in self.flags)


def family_of(args):
    if not args:
        raise Refused('no gh command given')
    first = args[0]
    if first.startswith('-'):
        raise Refused(f'{first!r}: the gh command word comes first (gh <command> <sub-command> ...)')
    if first in REFUSED_FAMILIES:
        raise Refused(f'gh {first}: {REFUSED_FAMILIES[first]}')
    if first == 'api':
        return 'api', 1
    sub = args[1] if len(args) > 1 else ''
    name = f'{first} {sub}'
    if name in REFUSED_SUBS and REFUSED_SUBS[name]:
        raise Refused(f'gh {name}: {REFUSED_SUBS[name]}')
    if name not in TABLE:
        known = sorted({c.split()[0] for c in TABLE})
        raise Refused(f'gh {name.strip()}: not on the list gh-as runs ({", ".join(known)}; see the gh-apps README)')
    return name, 2


def parse(args, repo, mode):
    """args: the gh arguments without the word gh. mode 'wrapper' lets file paths through (and lists them in
    .files for the wrapper to read); mode 'run' refuses every file path. Raises Refused."""
    for a in args:
        if '\0' in a:
            raise Refused('an argument holds NUL: refused')
    family, i = family_of(args)
    table = TABLE[family]
    flags, positionals, files, stdin_uses = [], [], [], 0
    while i < len(args):
        a = args[i]
        if a == '--':
            raise Refused('a bare -- is refused: put text that starts with - in a flag value or a file')
        if a.startswith('--') or (a.startswith('-') and len(a) > 1 and not re.match(r'^-\d', a)):
            name, eq, inline = a.partition('=') if a.startswith('--') else (a, '', '')
            if not a.startswith('--') and len(a) != 2:
                raise Refused(f'{a!r}: write each short flag on its own, with its value as the next word')
            entry = table.get(name)
            if entry is None:
                raise Refused(f'{name!r} is not a flag gh-as allows for gh {family}')
            kind, long, reason = entry
            if kind == 'refuse':
                raise Refused(f'gh {family} {long}: refused ({reason})')
            if kind == 'bool':
                if eq:
                    raise Refused(f'{a!r}: on/off flags take no value here')
                flags.append((long, None))
                i += 1
                continue
            if eq:
                value, at, prefix = inline, i, name + '='
                i += 1
            else:
                if i + 1 >= len(args):
                    raise Refused(f'{name} needs a value')
                value, at, prefix = args[i + 1], i + 1, ''
                i += 2
            if kind == 'repo':
                if (norm_repo(value) or '').lower() != repo.lower():
                    raise Refused(f'{name} {value!r}: this app may use only {repo}')
            elif kind == 'jq':
                if JQ_ENV_RE.search(value):
                    raise Refused(f'{name}: a jq expression may not read the environment or files')
            elif kind == 'file':
                if value == '-':
                    stdin_uses += 1
                elif mode == 'wrapper':
                    files.append((at, prefix, value))
                    stdin_uses += 1
                else:
                    raise Refused(f'{long} {value!r}: file paths are refused here; the gh-as wrapper sends the file '
                                  'on stdin (or give - and pipe it)')
            elif kind == 'field':
                key, eqs, val = value.partition('=')
                if eqs and val.startswith('@'):
                    if val == '@-':
                        stdin_uses += 1
                    elif mode == 'wrapper':
                        files.append((at, prefix + key + '=@', val[1:]))
                        stdin_uses += 1
                    else:
                        raise Refused(f'{long} {key}=@{val[1:]!r}: file paths are refused here; the gh-as wrapper '
                                      'sends the file on stdin')
            flags.append((long, value))
            continue
        if URL_RE.match(a):
            raise Refused(f'{a[:80]!r}: full URLs are refused; name the pull request, issue or path inside {repo}')
        positionals.append(a)
        i += 1
    if stdin_uses > 1:
        raise Refused('only one flag may read a file or stdin per gh-as call')
    return Parsed(family, flags, positionals, files, stdin_uses)


# ---------- the guard: refused even when the app's permissions would allow it

DISABLE_RE = re.compile(r'(?:^|/)actions/workflows/[^/]+/disable/?$', re.I)
PERMISSIONS_RE = re.compile(r'(?:^|/)actions/permissions(?:/|$)', re.I)
REPO_PERMISSIONS_RE = re.compile(r'^repos/[^/]+/[^/]+/actions/permissions/?$', re.I)
OWNER_WORK_RE = re.compile(r'(?:^|/)(?:rulesets|protection|environments|secrets|hooks|keys|collaborators|'
                           r'invitations|transfer|interaction-limits|actions/runners|actions/variables)(?:/|$)', re.I)
BARE_REPO_RE = re.compile(r'^repos/[^/]+/[^/]+/?$', re.I)
GRAPHQL_OWNER_RE = re.compile(r'mutation[\s\S]*(?:branchprotection|ruleset|environment|deleterepository|'
                              r'updaterepository|archiverepository|transferrepository|deploykey|'
                              r'repositorycollaborator|updatenotification)', re.I)
READ_METHODS = {'GET', 'HEAD'}


def guard(parsed, stdin=None):
    """Refuse what the owner keeps for themselves: turning workflows or Actions off, and repository settings, rules,
    secrets, keys and members. The app's own permissions are the wall; this is the second layer. stdin: the bytes
    gh will read (the privileged gh-as reads them first, so a GraphQL query from a file is checked too)."""
    if parsed.family == 'workflow disable':
        raise Refused('gh workflow disable: agents do not turn workflows off (ask the owner; `gh workflow enable` '
                      'is fine)')
    if parsed.family != 'api':
        return
    if not parsed.positionals:
        raise Refused('gh api needs an endpoint')
    endpoint = parsed.positionals[0].split('?', 1)[0].lstrip('/')
    methods = [v.upper() for v in parsed.values('--method')]
    headers = ' '.join(parsed.values('--header')).lower()
    body = parsed.has('--field') or parsed.has('--raw-field') or parsed.has('--input')
    method = methods[-1] if methods else ('POST' if body else 'GET')
    if 'x-http-method-override' in headers:
        method = 'OVERRIDE'
    if DISABLE_RE.search(endpoint):
        raise Refused('a call to .../actions/workflows/<id>/disable: agents do not turn workflows off')
    if method in READ_METHODS:
        return
    if PERMISSIONS_RE.search(endpoint):
        fields = dict(f.partition('=')[::2] for f in parsed.values('--field') + parsed.values('--raw-field'))
        reenable = (method == 'PUT' and REPO_PERMISSIONS_RE.match(endpoint) and not parsed.has('--input')
                    and fields.get('enabled', '').lower() == 'true'
                    and set(fields) <= {'enabled', 'allowed_actions'} and fields.get('allowed_actions', 'all') == 'all')
        if not reenable:
            raise Refused(f'a {method} to .../actions/permissions: agents do not turn Actions off or limit it')
        return
    if OWNER_WORK_RE.search(endpoint) or BARE_REPO_RE.match(endpoint):
        raise Refused(f'a {method} to {endpoint}: repository settings, rules, secrets, keys and members are owner '
                      'work')
    if endpoint == 'graphql':
        values = [v.partition('=')[2] for v in parsed.values('--field') + parsed.values('--raw-field')]
        from_stdin = parsed.has('--input') or any(v.startswith('@') for v in values)
        if from_stdin:
            if stdin is None:
                raise Refused('a GraphQL call that reads stdin, which gh-as has not read first: refused')
            values.append(stdin.decode('utf-8', 'replace'))
        if GRAPHQL_OWNER_RE.search(' '.join(values)):
            raise Refused('a GraphQL mutation of repository settings, rules or members is owner work')
