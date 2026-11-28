"""agent_guards.quick: actions-disable-guard's fast exit, for the hook's fast path (agent_guards.fast).

The tables and functions below are a copy of the guard's own fast exit (bin/actions-disable-guard, "the fast exit",
with the few names of the guard it uses: COPIERS, GIT_SAFE, git_runs, SED_RUNS, home_path_dirs), so the fast path
can answer a plain command (ls, git status, sed -n 1,80p f) without loading the 5,000 lines of the full guard.
The guard itself keeps its own copy: a copy of the guard without this lib still guards, and its tests swap its
names. tests/fast.test.py fails when any name here differs from the guard's (the source of every function, the
value of every table), so the two cannot drift apart.

The full guard stays the authority: the fast path only lets through what quick_pass() and quick_shadowed() pass,
which the guard's tests prove the full reading passes too; everything else goes to the full guard.
"""
import fnmatch, os, re

from agent_guards import config

SETTINGS, SETTINGS_PROBLEM = config.load('actionsDisable')
REFUSE_SCRIPTS = tuple(SETTINGS['refuseScripts'])

_HOME_DIRS = {}


def home_path_dirs(path, home):
    """PATH entries under $HOME (computed once: realpath on every entry for every segment was slow)."""
    key = (path, home)
    if key not in _HOME_DIRS:
        h = os.path.realpath(home) + os.sep
        _HOME_DIRS[key] = [d for d in path.split(':') if d and os.path.realpath(d).startswith(h)]
    return _HOME_DIRS[key]


COPIERS = {'cp', 'mv', 'ln'}                                        # data words that give a file another name
GIT_SAFE = frozenset('''commit log grep diff show add status tag blame rm mv checkout switch branch restore ls-files
    rev-parse merge notes reflog shortlog describe cat-file ls-tree rev-list stash cherry-pick reset revert
    format-patch worktree'''.split())
GIT_RUNS = re.compile(r'--upload-pack|--receive-pack|--exec|--open-files-in-pager|-O')   # flags that run a command
GIT_RUNS_LONG = ('--upload-pack', '--receive-pack', '--exec', '--open-files-in-pager')


def git_runs(a):
    """A git argument that can run a command: GIT_RUNS, -O in a cluster of short flags (-nO), or a long flag cut
    short the way git accepts (--open=less, --upl=x)."""
    if GIT_RUNS.match(a) or re.match(r'-[A-Za-z0-9]*O', a):
        return True
    name = a.split('=', 1)[0]
    return len(name) > 2 and name.startswith('--') and any(f.startswith(name) for f in GIT_RUNS_LONG)

SED_RUNS = re.compile(r'(?:^|[;{}!,$/\d\s])e(?:\s|$)|[^\w\s][a-zA-Z0-9]*e[a-zA-Z0-9]*\s*(?:[;}\n]|$)')   # e, s///e


# ---------- the fast exit

# Words the fast exit lets through: DATA words that run no command, set no variable and read no shell text (not
# printf/read/export/alias/[ ..: they set names or take text that matters later), and git with a subcommand that
# runs none of its text. Each one is checked below for the forms of it that do run something.
QUICK_WORDS = frozenset('''echo cat head tail wc ls stat file du df basename dirname realpath readlink mkdir rmdir rm cp
    mv ln touch chmod tee sort uniq cut tr paste column fold fmt nl tac rev comm join diff cmp grep egrep fgrep rg jq
    date printenv id whoami hostname uname uptime nproc seq mktemp sha256sum sha1sum md5sum base64 xxd od tree ps
    pgrep sleep true false test cd pushd popd pwd sed find'''.split())
QUICK_GIT = GIT_SAFE
QUICK_FLOW = frozenset('if then else elif fi do done while until'.split())   # keywords in front of a command word
# Text that sends the command to the full reading wherever it stands, letters only (dis-able, Work_Flow ..):
QUICK_KEYS = ('disable', 'permission', 'protection', 'ruleset', 'workflow', 'graphql', 'mutation', 'github', 'curl',
              'wget', 'http', 'aria2') \
    + tuple(re.sub(r'[^a-z0-9]', '', os.path.splitext(r['name'])[0].lower()) for r in REFUSE_SCRIPTS)   # garden.py: garden
QUICK_NAMES = frozenset({'gh', 'xh', 'xhs', 'curlie', 'httpie'})   # as a word anywhere (/usr/bin/gh, ln -s .. gh)
QUICK_SED = frozenset({'-n', '-E', '-r', '-e', '-i', '-s', '-u', '-z', '--quiet', '--silent', '--regexp-extended',
                       '--null-data', '--expression'})
QUICK_CHARS = re.compile(r'[^\x20-\x7e\t\n]|[$`\\%!]|<<')
QUICK_MAX, QUICK_CDS = 4000, 8
# sed scripts the fast exit takes: lines printed, deleted or counted, s/a/b/ with plain flags (no e, no w file)
_SED_ADDR = r'(?:\d+|\$|/[^/\n]*/)'
_SED_ONE = (rf'\s*(?:{_SED_ADDR}(?:\s*,\s*(?:{_SED_ADDR}|[+~]\d+))?\s*)?'
            r'(?:[pdnNgGhHxlz=]|[qQ]\d*|s/[^/\n]*/[^/\n]*/[gpiI\d]*)\s*')
QUICK_SED_SCRIPT = re.compile(rf'{_SED_ONE}(?:;{_SED_ONE})*(?:;\s*)?')


def quick_words(cmd, globs=None):
    """[[(word without its quotes, whether it had quotes)] per command] of a command in the small shell subset the
    fast exit reads (plain words, '..' and ".." with no $ ` \\ inside, ; | & && || and line breaks, redirections);
    None for anything else: an unclosed quote, # ( ) { } ! outside quotes. globs: each word with * ? [ outside
    quotes (the shell may turn it into other names) is added to it, when given."""
    out, cmd_, word, quoted, started, glob, i = [], [], '', False, False, False, 0
    def end_word():
        nonlocal word, quoted, started, glob
        if started:
            cmd_.append((word, quoted))
            if glob and globs is not None:
                globs.append(word)
        word, quoted, started, glob = '', False, False, False
    while i < len(cmd):
        c = cmd[i]
        if c in '\'"':
            j = cmd.find(c, i + 1)
            if j < 0:
                return None
            word, quoted, started, i = word + cmd[i + 1:j], True, True, j + 1
            continue
        if c in '#(){}!' or (c == '|' and cmd[i - 1:i] in ('>', '&') and cmd[max(i - 2, 0):i] != '|&' and
                                 (cmd[i - 1:i] == '>' or cmd[i - 2:i - 1] in ('>', '<'))):
            return None   # >| (also zsh >>| >&|) writes a file, it is no pipe
        if c in ' \t':
            end_word()
        elif c in ';|\n' or (c == '&' and not (cmd[i - 1:i] in ('<', '>') or cmd[i + 1:i + 2] == '>')):
            end_word()
            out.append(cmd_)
            cmd_ = []
        else:
            word, started, glob = word + c, True, glob or c in '*?['
        i += 1
    end_word()
    out.append(cmd_)
    return out


def quick_sed_scripts(args):
    """The scripts of a sed command the fast exit reads (-e X, --expression X, else the first word that is no
    option); None for a form it does not read."""
    scripts, i, rest = [], 0, []
    while i < len(args):
        a = args[i]
        if a in ('-e', '--expression'):
            if i + 1 >= len(args):
                return None
            scripts.append(args[i + 1])
            i += 2
            continue
        if a.startswith('-') and a != '-':
            if a not in QUICK_SED:
                return None
        else:
            rest.append(a)
        i += 1
    if not scripts:
        scripts = rest[:1]
    return scripts or None


def quick_pass(cmd):
    """True for a command that cannot name or run anything this guard refuses, decided from its text alone, before
    any file is read: every command in it is a QUICK_WORDS word (or safe git) in a form that runs nothing, and no
    word of it can name a guarded token, gh, an HTTP client or a git hook, or one of its own command words (a file
    the text writes and then runs). Anything else, however harmless, goes to the full reading. The tests compare
    the two over every command they know and over random ones (what is fast passes there too)."""
    if not isinstance(cmd, str) or len(cmd) > QUICK_MAX or QUICK_CHARS.search(cmd):
        return False
    low = cmd.lower()
    letters = re.sub(r'[^a-z0-9]', '', low)
    plain = re.sub(r'[\'"]', '', low)
    if any(k in letters for k in QUICK_KEYS) or any(k in plain for k in ('hooks', '.git/', '.husky')) \
            or QUICK_NAMES & set(re.split(r'[^a-z0-9_.-]+', plain)):
        return False
    globs = []
    cmds = quick_words(cmd, globs)
    if cmds is None:
        return False
    cds, heads, rest = 0, set(), set()
    for words in cmds:
        while words and not words[0][1] and words[0][0] in QUICK_FLOW:
            words = words[1:]
        if not words:
            continue
        (head, quoted), args = words[0], [w for w, _ in words[1:]]
        if quoted or (head not in QUICK_WORDS and head != 'git'):
            return False
        heads.add(head)
        rest.update(p for a in args for p in re.split(r'[^a-z0-9_.+-]+', a.lower()))
        cds += head in ('cd', 'pushd', 'popd')
        if cds > QUICK_CDS:
            return False
        if head == 'find' and any(a in ('-exec', '-execdir', '-ok', '-okdir') for a in args):
            return False
        if head in COPIERS and re.search(r'[*?\[]', cmd):
            return False
        if head == 'sed':
            scripts = quick_sed_scripts(args)
            if scripts is None or not all(QUICK_SED_SCRIPT.fullmatch(t) for t in scripts) \
                    or any(SED_RUNS.search(a) for a in args):
                return False
        if head == 'rg' and any(a.startswith(('--pre', '--hostname-bin')) for a in args):
            return False
        if head == 'sort' and any(a.startswith('--co') for a in args):
            return False
        if head == 'git':
            i = 0
            while i < len(args) and args[i] in ('-C', '--no-pager', '-P'):
                i += 2 if args[i] == '-C' else 1
            if i >= len(args) or args[i] not in QUICK_GIT or any(git_runs(a) for a in args[i + 1:]):
                return False
    # echo .. > ~/.local/bin/ls; ls: a file the text may write under a name it runs, also as a glob (l?, [l]s)
    return not heads & rest and not any(quick_glob_names(g, heads) for g in globs)


def quick_shadowed(cmd, cwd, env):
    """For a command quick_pass() took: whether a command word of it has a file of that name in a PATH folder under
    $HOME (a script an earlier call left, ~/.local/bin/ls), or a cp/mv/ln of it puts files into such a folder. Then
    the full reading decides. A few stat calls, no file is read."""
    dirs = home_path_dirs(env.get('PATH') or '', env.get('HOME') or os.path.expanduser('~'))
    if not dirs:
        return False
    real = None
    for words in quick_words(cmd) or ():
        words = [w for w, _ in words]
        while words and words[0] in QUICK_FLOW:
            words = words[1:]
        if not words:
            continue
        if any(os.path.exists(os.path.join(d, words[0])) for d in dirs):
            return True
        if words[0] in COPIERS:
            real = real or {os.path.realpath(d) for d in dirs}
            home = env.get('HOME') or os.path.expanduser('~')
            if any(os.path.realpath(os.path.join(cwd or os.getcwd(), re.sub(r'^~(?=/|$)', home, a))) in real
                   for a in words[1:] if not a.startswith('-')):
                return True
    return False


def quick_glob_names(word, heads):
    """Whether a word with * ? [ outside quotes may name one of heads (each part between / = < > & : , is a glob).
    A part with [ may name anything ([[:lower:]], [^x] ..: the guard does not read classes); * and ? are read the
    way the shell does."""
    for part in re.split(r'[/=<>&:,]+', word.lower()):
        if '[' in part:
            return True
        if re.search(r'[*?]', part) and any(fnmatch.fnmatchcase(h, part) for h in heads):   # no [ here; never
            return True                                                                     # backtracks on ***
    return False


