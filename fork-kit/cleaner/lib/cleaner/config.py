"""The kit's machine facts: the "cleaner" section of the machine file (fork-kit/common/machine_file.py).

The code holds the rules and every refusal; the section holds only what differs per machine: which folders, which
repositories, how many days. It can make the cleaner keep more (mustKeep, longer waits) and can turn rules on, but
no key takes a refusal away, and the floors below cannot be lowered. An empty section means every rule does nothing.
"""
import fnmatch, os, pwd, re

import machine_file

KIT = 'cleaner'
MACHINE_FILE = None  # tests only: read this file instead of the fixed path
MACHINE_OWNER = 0    # tests only: the uid that must own it (root)

# Never deleted, whatever a rule or the section says: agent transcripts and sessions, and the T3 data folder (its
# databases and logs). `~` is the home of the user the cleaner runs as; * and ? match within one path part.
MUST_KEEP = ('~/.claude*/projects', '~/.codex*/sessions', '~/.t3*/userdata')
# Never deleted wherever they are: a folder or file with one of these names, and anything that holds one.
MUST_KEEP_NAMES = ('repair-backups', '*log-archive*')
MIN_MERGED_HOURS = 24    # a worktree goes no sooner than this after its pull request merged
MIN_DOCKER_DAYS = 7      # an unused anonymous volume goes no sooner than this after it was made
MIN_LOG_LINES = 10
# Folders any tool rebuilds, so an ignored one does not keep a worktree. A repository adds its own (regenerable).
REGENERABLE = ('node_modules', '__pycache__', '.pytest_cache')

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


def _name_globs(value):
    bad = [v for v in value if not _name_glob(v)]
    return f'every entry must be a name pattern without / (not {bad[0]!r})' if bad else None


def _patterns(value):
    for pattern, days in value.items():
        if not _name_glob(pattern):
            return f'{pattern!r} must be a name pattern without /'
        if type(days) is not int or days < 1:
            return f'{pattern!r}: days must be a whole number >= 1 (not {days!r})'
    return None


def _github(value):
    return None if GITHUB_NAME.match(value) else f'must be owner/name (not {value!r})'


SPEC = {
    'logFile': (str, '~/.local/state/fork-cleaner/cleaner.log', _path),
    'logLines': (int, 5000, _at_least(MIN_LOG_LINES)),
    'mustKeep': (list, (), _paths),             # more paths that are never deleted; nothing takes one off the list
    'worktrees': (dict, {}, None),
    'docker': (dict, {}, None),
    'tmp': (dict, {}, None),
}
WORKTREES_SPEC = {
    'repos': (list, (), None),
    'mergedHours': (int, MIN_MERGED_HOURS, _at_least(MIN_MERGED_HOURS)),
}
REPO_SPEC = {
    'root': (str, machine_file.REQUIRED, _path),        # the folder the clones and worktrees are directly in
    'github': (str, machine_file.REQUIRED, _github),    # owner/name: only folders whose origin is this repository
    'regenerable': (list, (), _name_globs),             # names of ignored build output, on top of REGENERABLE
}
DOCKER_SPEC = {
    'enabled': (bool, False, None),
    'days': (int, MIN_DOCKER_DAYS, _at_least(MIN_DOCKER_DAYS)),
}
TMP_SPEC = {
    'root': (str, '/tmp', _path),
    'patterns': (dict, {}, _patterns),                  # name pattern -> days without a change; the first match counts
}


def _holds(folder, path):
    """True when `folder` is `path` or a folder above it."""
    return folder == '/' or path == folder or path.startswith(folder + '/')


class Repo:
    def __init__(self, root, github, regenerable):
        self.root, self.github, self.regenerable = root, github, regenerable


class Config:
    def __init__(self, section, where=KIT, home=None):
        values = machine_file.fields(section, SPEC, where)
        self.home = os.path.realpath(home or pwd.getpwuid(os.getuid()).pw_dir)
        self.log_file = self._expand(values['logFile'])
        self.lock_file = self.log_file + '.lock'
        self.log_lines = values['logLines']
        self.must_keep_text = MUST_KEEP + tuple(values['mustKeep'])
        self.must_keep = tuple(tuple(self._expand(text).split('/')[1:]) for text in self.must_keep_text)
        self.must_keep_names = MUST_KEEP_NAMES

        tmp = machine_file.fields(values['tmp'], TMP_SPEC, f'{where}.tmp')
        self.tmp_patterns = tuple(tmp['patterns'].items())
        # Without a pattern the rule does nothing and its folder is no root, so the folder is not judged either.
        self.tmp_root = self._root(tmp['root'], f'{where}.tmp.root') if self.tmp_patterns else self._expand(tmp['root'])

        docker = machine_file.fields(values['docker'], DOCKER_SPEC, f'{where}.docker')
        self.docker_enabled, self.docker_days = docker['enabled'], docker['days']

        worktrees = machine_file.fields(values['worktrees'], WORKTREES_SPEC, f'{where}.worktrees')
        self.merged_hours = worktrees['mergedHours']
        self.repos = []
        for i, entry in enumerate(worktrees['repos']):
            at = f'{where}.worktrees.repos[{i}]'
            repo = machine_file.fields(entry, REPO_SPEC, at)
            self.repos.append(Repo(self._root(repo['root'], f'{at}.root'), repo['github'],
                                   REGENERABLE + tuple(repo['regenerable'])))

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
