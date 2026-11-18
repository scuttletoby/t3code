"""The kit's machine facts: the "cleaner" section of the machine file (fork-kit/common/machine_file.py).

The code holds the rules and every refusal; the section holds only what differs per machine: which folders, which
repositories, how many days. It can make the cleaner keep more (mustKeep, longer waits) and can turn rules on, but
no key takes a refusal away, and the floors below cannot be lowered. An empty section means every rule does nothing.
KEY_KINDS says, for every key, which of these it is; a test fails for a key that is not in it.
"""
import fnmatch, glob, os, pwd, re

import machine_file

KIT = 'cleaner'
MACHINE_FILE = None  # tests only: read this file instead of the fixed path
MACHINE_OWNER = 0    # tests only: the uid that must own it (root)

# Never deleted, whatever a rule or the section says: the agent tools' homes whole (transcripts, sessions, their
# databases, settings and logins) and T3's data folders (its databases and logs; `dev` is a development build's).
# `~` is the home of the user the cleaner runs as; * and ? match within one path part.
AGENT_HOMES = (
    '~/.claude*', '~/.codex*',
    # other agent tools, where they keep sessions, history and logins
    '~/.gemini', '~/.qwen', '~/.cursor', '~/.config/Cursor', '~/.aider*', '~/.continue', '~/.copilot',
    '~/.config/github-copilot', '~/.codeium', '~/.kiro', '~/.amazon-q', '~/.config/amazon-q', '~/.local/share/amazon-q',
    '~/.config/opencode', '~/.local/share/opencode', '~/.local/state/opencode', '~/.config/crush',
    '~/.local/share/crush', '~/.config/goose', '~/.local/share/goose', '~/.config/amp', '~/.local/share/amp',
)
T3_DATA = ('~/.t3*/userdata', '~/.t3*/dev')
MUST_KEEP = AGENT_HOMES + T3_DATA
# The classes of the disk report (`fork-cleaner report`, report.py), in its order: the one list it sizes. Each name
# says what it measures. A rule's class ('rule:<name>') is what that rule looks at: the worktrees and clones of the
# listed repositories, the temp entries a pattern takes, and every Docker volume, named ones too (Docker measures
# them all; the rule takes only unused anonymous ones). A must-keep class is its group above, whole: the agent
# tools' home folders (transcripts, sessions, settings, caches) and T3's data. None is a class no rule takes yet.
# What is in none of them is "everything else". A new rule (rules.RULES) or must-keep group gets its class here
# (report.test.py fails until it does).
CLASSES = (
    ('worktrees', 'rule:worktrees'),
    ('all docker volumes', 'rule:docker'),
    ('temp', 'rule:tmp'),
    ('cold folders', 'rule:compress-cold'),
    ('agent tool homes', AGENT_HOMES),
    ('T3 data', T3_DATA),
    ('build caches', None),
)
# Never deleted wherever they are: a folder or file with one of these names, and anything that holds one.
MUST_KEEP_NAMES = ('repair-backups', '*log-archive*')
MIN_MERGED_HOURS = 24    # a worktree goes no sooner than this after its pull request merged
MIN_USED_HOURS = 24      # nor sooner than this after anything in it or its own git state last changed
MIN_DOCKER_DAYS = 7      # an unused anonymous volume goes no sooner than this after it was made
MIN_LOG_LINES = 10
MIN_COLD_DAYS = 7        # compress-cold: a file is compressed no sooner than this after it was last changed or read
MIN_KEEP_FREE_GB = 1     # compress-cold: the disk keeps at least this much free with a whole copy written
# Folders compress-cold never takes, whatever the section says, nor a folder inside one or holding one: Claude Code
# reads its transcripts as plain text (resume, usage scans), so they stay as they are (#468).
COMPRESS_NEVER = ('~/.claude*',)
# Folder names compress-cold never takes or enters (#468: the log archive stays as it is). `repair-backups`, the
# other must-keep name, may be compressed: #468 names it.
COMPRESS_NEVER_NAMES = ('*log-archive*',)
# Folders any tool rebuilds, so an ignored one does not keep a worktree. A repository adds its own (regenerable).
REGENERABLE = ('node_modules', '__pycache__', '.pytest_cache')

UNIT_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9@._:-]*\.timer$')
# `fork-cleaner daily` (daily.py): 'compare' plans, reports and compares and never deletes; 'run' deletes.
DAILY_MODES = ('compare', 'run')
GITHUB_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$')


def _at_least(floor):
    return lambda value: None if value >= floor else f'must be a whole number >= {floor}'


def _path(value):
    """A path is absolute or starts with ~/ and has no . or .. part."""
    if not isinstance(value, str):
        return f'must be a path (not {value!r})'
    rest = value[2:] if value.startswith('~/') else value[1:] if value.startswith('/') else None
    if rest is None:
        return f'must be an absolute path or start with ~/ (not {value!r})'
    parts = rest.split('/')
    if not rest or any(p in ('', '.', '..') for p in parts[:-1]) or parts[-1] in ('.', '..'):
        return f'must name a folder or file, without . or .. (not {value!r})'
    return None


def _paths(value):
    return next((problem for problem in map(_path, value) if problem), None)


def _name_glob(value):
    return isinstance(value, str) and value not in ('', '.', '..') and '/' not in value and '\0' not in value


def _names(value):
    """Literal names only: a pattern such as * would make every ignored file build output and so take the
    "ignored files" refusal away."""
    bad = [v for v in value if not _name_glob(v) or any(c in v for c in '*?[]\\')]
    return f'every entry must be a plain name, without / or a wildcard (not {bad[0]!r})' if bad else None


def _patterns(value):
    for pattern, days in value.items():
        if not _name_glob(pattern):
            return f'{pattern!r} must be a name pattern without /'
        if type(days) is not int or days < 1:
            return f'{pattern!r}: days must be a whole number >= 1 (not {days!r})'
    return None


def _globs(value):
    bad = [v for v in value if not isinstance(v, str) or not v or '\0' in v]
    return f'every entry must be a branch name pattern (not {bad[0]!r})' if bad else None


def _unit(value):
    bad = [v for v in value if not (isinstance(v, str) and UNIT_NAME.match(v))]
    return f'every entry must be a timer unit name such as old-cleaner.timer (not {bad[0]!r})' if bad else None


def _mode(value):
    return None if value in DAILY_MODES else f'must be one of {", ".join(DAILY_MODES)} (not {value!r})'


def _github(value):
    return None if GITHUB_NAME.match(value) else f'must be owner/name (not {value!r})'


SPEC = {
    'logFile': (str, '~/.local/state/fork-cleaner/cleaner.log', _path),
    'logLines': (int, 5000, _at_least(MIN_LOG_LINES)),
    'mustKeep': (list, (), _paths),             # more paths that are never deleted; nothing takes one off the list
    'worktrees': (dict, {}, None),
    'docker': (dict, {}, None),
    'tmp': (dict, {}, None),
    'daily': (dict, {}, None),
    'compressCold': (dict, {}, None),
}
WORKTREES_SPEC = {
    'repos': (list, (), None),
    'mergedHours': (int, MIN_MERGED_HOURS, _at_least(MIN_MERGED_HOURS)),
    'usedHours': (int, MIN_USED_HOURS, _at_least(MIN_USED_HOURS)),
}
REPO_SPEC = {
    'root': (str, machine_file.REQUIRED, _path),        # the folder the clones and worktrees are directly in
    'github': (str, machine_file.REQUIRED, _github),    # owner/name: only folders whose origin is this repository
    'regenerable': (list, (), _names),                  # names of ignored build output, on top of REGENERABLE
    'branches': (list, (), _globs),                     # branch name patterns a worktree must be on; none: any
}
DOCKER_SPEC = {
    'enabled': (bool, False, None),
    'days': (int, MIN_DOCKER_DAYS, _at_least(MIN_DOCKER_DAYS)),
}
TMP_SPEC = {
    'root': (str, '/tmp', _path),
    'patterns': (dict, {}, _patterns),                  # name pattern -> days without a change; the first match counts
}
# `fork-cleaner daily`, what the timer runs (daily.py). Without the section it plans, reports and compares.
DAILY_SPEC = {
    'mode': (str, 'compare', _mode),
    'oldTimers': (list, (), _unit),                     # the timers of the tools it replaces: `run` refuses while one is on
    'outDir': (str, '~/.local/state/fork-cleaner/daily', _path),
    'keepDays': (int, 30, _at_least(1)),                # its dated plan and compare files are kept this long
    'compare': (list, (), None),                        # the old tools' dry runs (compare.py), each a COMPARE_SPEC
}
# `compress-cold` (compress.py): files in these folders nobody changed or read for `days` are replaced by a checked
# zstd copy. Without folders the rule does nothing.
COMPRESS_SPEC = {
    'folders': (list, (), None),                        # each a FOLDER_SPEC
    'keepFreeGB': (int, 5, _at_least(MIN_KEEP_FREE_GB)),   # GB the disk keeps free with a whole copy written
}
FOLDER_SPEC = {
    'path': (str, machine_file.REQUIRED, _path),        # the folder; files at any depth in it are looked at
    'days': (int, machine_file.REQUIRED, _at_least(MIN_COLD_DAYS)),     # days without a change or a read
}
COMPARE_SPEC = {
    'name': (str, machine_file.REQUIRED, None),
    'command': (list, machine_file.REQUIRED, None),     # argv; the first is an absolute path; run without a shell
    'pattern': (str, machine_file.REQUIRED, None),      # a regular expression whose one group is a path it would delete
}
SOURCE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


# What each key can do. 'keeps more': it can only make the cleaner keep more. 'picks': it names what a rule looks
# at or turns a rule on; every candidate still passes every refusal. 'floor': a number that cannot go below its
# floor. 'own file': the cleaner's own log (it must be a new file or the cleaner's, never a must-keep path).
# 'loosens': it takes a refusal away for what it names. Only `regenerable` does, and only for literal names it
# lists (never a wildcard and never a credential-looking name). 'compare only': read by `fork-cleaner compare`
# alone, which runs another tool's dry run and reads its output; it decides no delete. The compressCold keys pick
# folders, also in must-keep paths: that rule deletes no content (it keeps every file, as a copy read back and
# checked before the original goes), and every other refusal still holds there (compress.py).
KEY_KINDS = {
    'logFile': 'own file', 'logLines': 'floor', 'mustKeep': 'keeps more', 'worktrees': 'picks', 'docker': 'picks',
    'tmp': 'picks', 'worktrees.repos': 'picks', 'worktrees.mergedHours': 'floor', 'repos.root': 'picks',
    'repos.github': 'picks', 'repos.regenerable': 'loosens', 'docker.enabled': 'picks', 'docker.days': 'floor',
    'tmp.root': 'picks', 'tmp.patterns': 'picks', 'worktrees.usedHours': 'floor', 'repos.branches': 'picks',
    'daily': 'picks', 'daily.mode': 'picks', 'daily.oldTimers': 'keeps more', 'daily.outDir': 'own file',
    'daily.keepDays': 'floor', 'daily.compare': 'compare only', 'compare.name': 'compare only',
    'compare.command': 'compare only', 'compare.pattern': 'compare only',
    'compressCold': 'picks', 'compressCold.folders': 'picks', 'compressCold.keepFreeGB': 'floor',
    'folders.path': 'picks', 'folders.days': 'floor',
}


def _holds(folder, path):
    """True when `folder` is `path` or a folder above it."""
    return folder == '/' or path == folder or path.startswith(folder + '/')


def must_keep_match(patterns, path):
    """The must-keep pattern `path` is equal to, inside of or a folder above; None when there is none."""
    parts = tuple(path.split('/')[1:])
    for pattern in patterns:
        if all(fnmatch.fnmatchcase(part, glob) for part, glob in zip(parts, pattern)):
            return pattern
    return None


class Repo:
    def __init__(self, root, github, regenerable, branches=()):
        self.root, self.github, self.regenerable, self.branches = root, github, regenerable, branches


class ColdFolder:
    """One folder of compress-cold: its files untouched for `days` are compressed."""

    def __init__(self, path, days):
        self.path, self.days = path, days


class Source:
    """One old tool's dry run for `fork-cleaner compare`."""

    def __init__(self, name, command, pattern):
        self.name, self.command, self.pattern = name, command, pattern


def _source(entry, at):
    values = machine_file.fields(entry, COMPARE_SPEC, at)
    if not SOURCE_NAME.match(values['name']):
        raise machine_file.MachineFileError(f'{at}.name: must be a short name of letters, digits, . _ - (not {values["name"]!r})')
    command = values['command']
    if not command or not all(isinstance(arg, str) and arg and '\0' not in arg for arg in command):
        raise machine_file.MachineFileError(f'{at}.command: must be a list of non-empty strings (the program and its arguments)')
    if not command[0].startswith('/'):
        raise machine_file.MachineFileError(f'{at}.command: the program must be an absolute path (not {command[0]!r})')
    try:
        pattern = re.compile(values['pattern'])
    except re.error as ex:
        raise machine_file.MachineFileError(f'{at}.pattern: not a regular expression ({ex})') from None
    if pattern.groups != 1:
        raise machine_file.MachineFileError(f'{at}.pattern: must have exactly one group, the path (has {pattern.groups})')
    return Source(values['name'], tuple(command), pattern)


class Config:
    def __init__(self, section, where=KIT, home=None):
        values = machine_file.fields(section, SPEC, where)
        self.home = os.path.realpath(home or pwd.getpwuid(os.getuid()).pw_dir)
        self.log_lines = values['logLines']
        self.must_keep_text = MUST_KEEP + tuple(values['mustKeep'])
        # A path written through a symbolic link is kept as written and as the link resolves: both forms count.
        forms = []
        for text in self.must_keep_text:
            for form in (self._expand(text), os.path.realpath(self._expand(text))):
                if tuple(form.split('/')[1:]) not in forms:
                    forms.append(tuple(form.split('/')[1:]))
        self.must_keep = tuple(forms)
        self.must_keep_names = MUST_KEEP_NAMES
        log = self._expand(values['logFile'])       # its folder resolved; the file itself is never reached by a link
        self.log_file = os.path.join(os.path.realpath(os.path.dirname(log)), os.path.basename(log))
        self.lock_file = self.log_file + '.lock'
        self.journal_file = self.log_file + '.deleting'     # deletes started and not ended (engine.py)
        self.report_file = self.log_file + '.reported'      # the last line of the log the last report counted
        self.report_lock_file = self.report_file + '.lock'  # one report at a time (report.py)
        for own in (self.log_file, self.lock_file, self.journal_file, self.report_file, self.report_lock_file):
            kept = must_keep_match(self.must_keep, own)
            if kept is not None or any(fnmatch.fnmatchcase(part.lower(), glob) for part in own.split('/') for glob in MUST_KEEP_NAMES):
                raise machine_file.MachineFileError(f'{where}.logFile: {values["logFile"]} is in a must-keep path, '
                                                    'and the cleaner trims its log')

        tmp = machine_file.fields(values['tmp'], TMP_SPEC, f'{where}.tmp')
        self.tmp_patterns = tuple(tmp['patterns'].items())
        # Without a pattern the rule does nothing and its folder is no root, so the folder is not judged either.
        self.tmp_root = self._root(tmp['root'], f'{where}.tmp.root') if self.tmp_patterns else self._expand(tmp['root'])

        docker = machine_file.fields(values['docker'], DOCKER_SPEC, f'{where}.docker')
        self.docker_enabled, self.docker_days = docker['enabled'], docker['days']

        worktrees = machine_file.fields(values['worktrees'], WORKTREES_SPEC, f'{where}.worktrees')
        self.merged_hours = worktrees['mergedHours']
        self.used_hours = worktrees['usedHours']
        self.repos = []
        for i, entry in enumerate(worktrees['repos']):
            at = f'{where}.worktrees.repos[{i}]'
            repo = machine_file.fields(entry, REPO_SPEC, at)
            self.repos.append(Repo(self._root(repo['root'], f'{at}.root'), repo['github'],
                                   REGENERABLE + tuple(repo['regenerable']), tuple(repo['branches'])))

        daily = machine_file.fields(values['daily'], DAILY_SPEC, f'{where}.daily')
        self.daily_mode, self.old_timers, self.daily_keep_days = daily['mode'], tuple(daily['oldTimers']), daily['keepDays']
        out = self._expand(daily['outDir'])
        self.daily_dir = os.path.join(os.path.realpath(os.path.dirname(out)), os.path.basename(out))
        self.sources = tuple(_source(entry, f'{where}.daily.compare[{i}]') for i, entry in enumerate(daily['compare']))
        names = [source.name for source in self.sources]
        if len(set(names)) != len(names):
            raise machine_file.MachineFileError(f'{where}.daily.compare: two sources have the same name')
        held = must_keep_match(self.must_keep, self.daily_dir)
        if held is not None or any(fnmatch.fnmatchcase(part.lower(), glob) for part in self.daily_dir.split('/') for glob in MUST_KEEP_NAMES):
            raise machine_file.MachineFileError(f'{where}.daily.outDir: {daily["outDir"]} is in a must-keep path, '
                                                'and the cleaner trims its files')
        if _holds(self.daily_dir, self.home):
            raise machine_file.MachineFileError(f'{where}.daily.outDir: {daily["outDir"]} is the home folder or holds it')

        self.cold_folders = self._cold(values['compressCold'], f'{where}.compressCold', values['logFile'], daily['outDir'])

        for root in self.roots():
            for key, text, own in (('logFile', values['logFile'], self.log_file),
                                   ('daily.outDir', daily['outDir'], self.daily_dir)):
                if _holds(root, own) or _holds(own, root):
                    raise machine_file.MachineFileError(f'{where}.{key}: {text} is inside {root}, a folder '
                                                        'the cleaner deletes from')

    def _cold(self, section, where, log_text, out_text):
        """The folders of compress-cold. A folder may be inside a must-keep path (the rule keeps every file's
        content), but never one of them whole or a folder above one (`~/.codex` holds logins and settings; name
        `~/.codex/sessions`), never in or above COMPRESS_NEVER, the cleaner's own files or a folder another rule
        deletes from, and never in or above another of its folders."""
        values = machine_file.fields(section, COMPRESS_SPEC, where)
        self.keep_free = values['keepFreeGB'] * 1024 ** 3
        folders = []
        never = self._never()
        for i, entry in enumerate(values['folders']):
            at = f'{where}.folders[{i}]'
            folder = machine_file.fields(entry, FOLDER_SPEC, at)
            path, text = self._expand(folder['path']), folder['path']
            parts = tuple(path.split('/')[1:])
            if _holds(path, self.home):
                raise machine_file.MachineFileError(f'{at}.path: {text} is the home folder or holds it')
            real = os.path.realpath(path)
            if must_keep_match(never, path) is not None or must_keep_match(never, real) is not None:
                raise machine_file.MachineFileError(f'{at}.path: {text} is in, or holds, a folder this rule never '
                                                    f'takes ({", ".join(COMPRESS_NEVER)}, also where a link to one '
                                                    'leads: read as plain text)')
            if any(matches(part.lower(), COMPRESS_NEVER_NAMES) for form in (path, real) for part in form.split('/')):
                raise machine_file.MachineFileError(f'{at}.path: {text} is or is in a log archive '
                                                    f'({", ".join(COMPRESS_NEVER_NAMES)}): it stays as it is (#468)')
            held = [p for p in self.must_keep if must_keep_match((p,), path) is not None and len(parts) <= len(p)]
            if held:
                raise machine_file.MachineFileError(f'{at}.path: {text} is or holds the must-keep path /{"/".join(held[0])}: '
                                                    'name a folder inside it (its settings and logins stay as they are)')
            for key, own_text, own in (('logFile', log_text, self.log_file), ('daily.outDir', out_text, self.daily_dir)):
                if _holds(path, own) or _holds(own, path):
                    raise machine_file.MachineFileError(f'{at}.path: {text} holds or is inside {key} {own_text}')
            for root in self.roots():
                if _holds(path, root) or _holds(root, path):
                    raise machine_file.MachineFileError(f'{at}.path: {text} holds or is inside {root}, a folder '
                                                        'another rule deletes from')
            for other in folders:
                if _holds(path, other.path) or _holds(other.path, path):
                    raise machine_file.MachineFileError(f'{at}.path: {text} holds or is inside {other.path}, '
                                                        'another of its folders')
            folders.append(ColdFolder(path, folder['days']))
        return tuple(folders)

    def _never(self):
        """COMPRESS_NEVER as must-keep patterns: each as written, with its literal folder resolved, and where each
        entry it matches now leads (`~/.claude` may be a link to a folder elsewhere: that folder is never taken
        either)."""
        forms = []
        for text in COMPRESS_NEVER:
            expanded = self._expand(text)
            parent, last = os.path.split(expanded)
            found = sorted(glob.glob(os.path.join(glob.escape(parent), last)))
            for form in [expanded, os.path.join(os.path.realpath(parent), last)] + [os.path.realpath(f) for f in found]:
                if tuple(form.split('/')[1:]) not in forms:
                    forms.append(tuple(form.split('/')[1:]))
        return forms

    def _expand(self, text):
        return os.path.normpath(os.path.join(self.home, text[2:]) if text.startswith('~/') else text)

    def _root(self, text, where):
        """A folder whose direct entries a rule may delete: never / or the home folder or a folder above it."""
        root = self._expand(text)
        if _holds(root, self.home):
            raise machine_file.MachineFileError(f'{where}: {text} is the home folder or holds it')
        return root

    def roots(self):
        """The folders whose direct entries may be deleted at all. Nothing else is ever a candidate."""
        roots = {repo.root for repo in self.repos}
        if self.tmp_patterns:
            roots.add(self.tmp_root)
        return frozenset(roots)


def matches(name, globs):
    return any(fnmatch.fnmatchcase(name, glob) for glob in globs)


def load():
    """The config, or MachineFileError naming the file and what is wrong."""
    path = MACHINE_FILE or machine_file.default_path()
    return Config(machine_file.read_section(KIT, path, MACHINE_OWNER), f'{path}: {KIT}')
