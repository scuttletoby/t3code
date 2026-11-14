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
import json, re

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
    def __init__(self, family, flags, positionals, files, stdin_uses, repo=None):
        self.family = family            # 'pr view', 'api', ...
        self.repo = repo                # the app's repository: what gh puts in for {owner} and {repo}
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
    return Parsed(family, flags, positionals, files, stdin_uses, repo)


# ---------- the guard: refused even when the app's permissions would allow it
#
# The guard checks text that gh and GitHub read again later (gh drops a '#...' part, GitHub decodes '%xx' and JSON
# '\uXXXX'). So it first brings that text into one plain form and REFUSES anything else, then checks the plain form.

DISABLE_RE = re.compile(r'(?:^|/)actions/workflows/[^/]+/disable$', re.I)
PERMISSIONS_RE = re.compile(r'(?:^|/)actions/permissions(?:/|$)', re.I)
REPO_PERMISSIONS_RE = re.compile(r'^repos/[^/]+/[^/]+/actions/permissions$', re.I)
OWNER_WORK_RE = re.compile(r'(?:^|/)(?:rulesets|protection|environments|secrets|hooks|keys|collaborators|'
                           r'invitations|transfer|interaction-limits|actions/runners|actions/variables|actions/oidc|'
                           r'actions/runs/[^/]+/approve)(?:/|$)', re.I)
# file paths and git refs are the repository's own data: a folder src/hooks/ or a branch named keys is not a setting
DATA_TREES = ('contents/', 'git/')
READ_METHODS = {'GET', 'HEAD'}
SEGMENT_RE = re.compile(r'[A-Za-z0-9._~+@:-]+')   # (fullmatch: '$' passes a last newline) no %, #, ?, \, space
QUERY_RE = re.compile(r'[A-Za-z0-9._~+@:%=&,/*()!$\'-]*')   # a query string after ? (reads only)
PLACEHOLDER_RE = re.compile(r'\{(owner|repo|branch)\}')
HEADER_NAMES = {'accept', 'x-github-api-version', 'graphql-features', 'if-none-match', 'if-modified-since'}
GRAPHQL_KEYS = {'query', 'variables', 'operationName'}


def _fill(text, repo, what):
    """gh's own {owner}/{repo} filling (GH_REPO is the app's repository); {branch} needs git, which gh-as has not."""
    owner, _, name = (repo or '/').partition('/')

    def sub(m):
        if m.group(1) == 'branch' or not repo:
            raise Refused(f'{what}: {{branch}} is refused (gh-as runs gh outside any git folder); name the branch')
        return owner if m.group(1) == 'owner' else name
    return PLACEHOLDER_RE.sub(sub, text)


def endpoint_parts(raw, repo):
    """(path, query) in plain form: placeholders filled as gh fills them, one leading '/' dropped. Refused: '#',
    '%', '\\', spaces or control characters, non-ASCII, empty segments ('//', a trailing '/'), '.' or '..'
    segments, and any '{' or '}' left over."""
    text = _fill(raw, repo, f'endpoint {raw[:80]!r}')
    path, q, query = text.partition('?')
    if path.startswith('/'):
        path = path[1:]
    segments = path.split('/')
    bad = [s for s in segments if s in ('', '.', '..') or not SEGMENT_RE.fullmatch(s)]
    if bad or (q and not QUERY_RE.fullmatch(query)) or '#' in text:
        raise Refused(f'endpoint {raw[:80]!r}: write the path plainly (letters, digits and ._~+@:- between single '
                      'slashes; no %-escapes, #, spaces, empty, . or .. parts); gh-as checks it as written')
    return path, (query if q else None)


def _headers(parsed):
    names = []
    for h in parsed.values('--header'):
        name, colon, _value = h.partition(':')
        name = name.strip().lower()
        if not colon or name not in HEADER_NAMES or not h.isprintable():
            raise Refused(f'header {h[:60]!r}: gh-as allows only the headers {", ".join(sorted(HEADER_NAMES))}')
        names.append(name)
    return names


# GraphQL: a small lexer (GraphQL spec, 2021), enough to know every name in the document. Strings and comments
# are skipped as GitHub's parser skips them; anything the lexer does not know is refused.
GQL_TOKEN = re.compile(r'''
    (?P<skip>[\s,﻿]+ | \#[^\n\r]*)
  | (?P<block>"""(?:\\"""|[^"]|"(?!""))*""")
  | (?P<string>"(?:\\["\\/bfnrt]|\\u[0-9A-Fa-f]{4}|[^"\\\n\r])*")
  | (?P<name>[_A-Za-z][_0-9A-Za-z]*)
  | (?P<number>-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)
  | (?P<punct>\.\.\.|[!$&()\:=@\[\]{|}])
''', re.X)


def graphql_names(doc):
    names, i = [], 0
    while i < len(doc):
        m = GQL_TOKEN.match(doc, i)
        if not m:
            raise Refused(f'the GraphQL text cannot be read at {doc[i:i + 20]!r}: refused')
        if m.lastgroup == 'name':
            names.append(m.group())
        i = m.end()
    return names


def check_graphql_text(doc):
    """Only read operations ('query' or the short '{ ... }' form). Agents write through the gh commands on the
    list, never through GraphQL mutations or subscriptions."""
    if not isinstance(doc, str):
        raise Refused('the GraphQL query must be text')
    names = graphql_names(doc)
    if not names:
        raise Refused('an empty GraphQL query')
    for n in names:
        if n.lower() in ('mutation', 'subscription'):
            raise Refused('gh api graphql: only queries (reads) pass gh-as; a GraphQL mutation or subscription is '
                          'refused (use the gh command for it, e.g. gh pr comment)')


def _strict_json(data):
    def pairs(items):
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            raise Refused('the GraphQL body has a key twice: refused')
        return dict(items)

    def no_constant(word):
        raise Refused(f'the GraphQL body has {word}: refused')
    try:
        text = data.decode('utf-8', 'strict')   # GitHub reads UTF-8; json.loads(bytes) would also guess UTF-16/32
        if text.startswith('﻿'):
            raise Refused('the GraphQL body starts with a byte order mark: refused')
        obj = json.loads(text, object_pairs_hook=pairs, parse_constant=no_constant)
        json.dumps(obj, ensure_ascii=False).encode('utf-8', 'strict')   # a lone \ud800 would not survive this
    except (UnicodeError, ValueError, RecursionError):
        raise Refused('the GraphQL body (--input) is not plain UTF-8 JSON: refused') from None
    return obj


def graphql_texts(parsed, stdin):
    """Every query text GitHub will run, decoded the way GitHub decodes it."""
    fields = [(f, v) for f, v in parsed.flags if f in ('--field', '--raw-field')]
    if parsed.has('--input'):
        if fields:
            raise Refused('gh api graphql --input: other fields would go into the URL; put everything in the body')
        if stdin is None:
            raise Refused('a GraphQL call that reads stdin, which gh-as has not read first: refused')
        body = _strict_json(stdin)
        if not isinstance(body, dict) or not set(body) <= GRAPHQL_KEYS or not isinstance(body.get('query'), str):
            raise Refused('the GraphQL body must be {"query": "...", "variables": {...}, "operationName": "..."}')
        return [body['query']]
    texts = []
    for flag, value in fields:
        key, eq, val = value.partition('=')
        base = re.split(r'[\[\]]', key, maxsplit=1)[0]
        if base in ('query', 'operationName') and key != base or not eq and base == 'query':
            raise Refused(f'{flag} {key!r}: write the GraphQL query as query=TEXT')
        if key != 'query':
            continue
        if flag == '--field':
            if val.startswith('@'):
                if stdin is None:
                    raise Refused('a GraphQL call that reads stdin, which gh-as has not read first: refused')
                try:
                    val = stdin.decode('utf-8', 'strict')
                except UnicodeError:
                    raise Refused('the GraphQL query on stdin is not UTF-8: refused') from None
            elif val in ('true', 'false', 'null') or re.fullmatch(r'[+-]?[0-9]+', val):
                raise Refused('the GraphQL query must be text')
            elif PLACEHOLDER_RE.search(val):
                raise Refused('{owner}, {repo} or {branch} in a GraphQL query: use -f and a variable instead')
        texts.append(val)
    if len(texts) != 1:
        raise Refused('gh api graphql needs exactly one query=... field (or a JSON body with --input)')
    return texts


def guard(parsed, stdin=None):
    """Refuse what the owner keeps for themselves: turning workflows or Actions off, and repository settings, rules,
    secrets, keys and members. The app's own permissions are the wall; this is the second layer. stdin: the bytes
    gh will read (the privileged gh-as reads them first, so a GraphQL query from a file is checked too)."""
    if parsed.family == 'workflow disable':
        raise Refused('gh workflow disable: agents do not turn workflows off (ask the owner; `gh workflow enable` '
                      'is fine)')
    if parsed.family != 'api':
        return
    if len(parsed.positionals) != 1:
        raise Refused('gh api needs exactly one endpoint')
    path, query = endpoint_parts(parsed.positionals[0], parsed.repo)
    # the rules below match each part without what follows its first dot, so 'rulesets.json' (a format suffix
    # a router may drop) or 'graphql.json' count as 'rulesets' and 'graphql'
    endpoint = '/'.join(s.split('.', 1)[0] or s for s in path.split('/'))
    _headers(parsed)
    methods = [v.upper() for v in parsed.values('--method')]
    body = parsed.has('--field') or parsed.has('--raw-field') or parsed.has('--input')
    method = methods[-1] if methods else ('POST' if body else 'GET')
    if DISABLE_RE.search(endpoint):
        raise Refused('a call to .../actions/workflows/<id>/disable: agents do not turn workflows off')
    if endpoint.lower() == 'graphql':
        if path != 'graphql' or query is not None or method != 'POST':
            raise Refused('gh api graphql: write it as `graphql`, POST, with no ?query')
        for text in graphql_texts(parsed, stdin):
            check_graphql_text(text)
        return
    if method in READ_METHODS:
        return
    if query is not None:
        raise Refused(f'a {method} with a ?query in the endpoint: send the values as fields (-f, -F) instead')
    # every write goes to something inside the App's own repository, written as repos/OWNER/NAME/...: not the
    # repository itself (its settings), not the numeric alias repositories/ID, not orgs/..., users/..., teams/...
    # (members, teams, org settings). The token carries the App's org permissions too, so this is not left to it.
    owner, _, name = (parsed.repo or '/').partition('/')
    parts = path.split('/')
    if not (len(parts) > 3 and parts[0] == 'repos' and owner and name and parts[1].lower() == owner.lower()
            and parts[2].lower() == name.lower()):
        raise Refused(f'a {method} to {path}: gh-as writes only inside the App\'s own repository, as '
                      f'repos/{parsed.repo}/...; repository, org, team and member settings are owner work')
    if PERMISSIONS_RE.search(endpoint):
        # the one exception turns Actions back on: exactly one field, enabled=true, and nothing else (allowed_actions
        # or any other field could widen which actions may run)
        fields = parsed.values('--field') + parsed.values('--raw-field')
        reenable = (method == 'PUT' and REPO_PERMISSIONS_RE.match(endpoint) and not parsed.has('--input')
                    and fields == ['enabled=true'])
        if not reenable:
            raise Refused(f'a {method} to the Actions permissions: agents only turn Actions back on (one field, '
                          'enabled=true); everything else there is owner work')
        return
    rest = '/'.join(endpoint.split('/')[3:])   # the part after repos/OWNER/NAME/
    if not rest.lower().startswith(DATA_TREES) and OWNER_WORK_RE.search(rest):
        raise Refused(f'a {method} to {path}: repository settings, rules, secrets, keys and members are owner '
                      'work')
