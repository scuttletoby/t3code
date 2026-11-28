"""agent_guards.leak: the private patterns of leak-guard and leak-scan, and how text is matched against them.

The pattern file is private: it lives outside every repo (default /etc/fork-kit/leak-patterns, the machine file's
leakGuard.patternsFile) and nothing here ever prints a pattern or the text it matched. A match is named by its line
number in the file only, so a refusal or a report can be pasted anywhere.

File format, one pattern per line:
  Alex Example            plain text: found in any case, not inside a longer word (`Alex` is not in `Alexander`)
  re:box-\\d+\\.lan         a Python regular expression, in any case
  # a comment             lines starting with # and blank lines are skipped
"""
import os, re

MAX_FILE = 1_000_000   # a pattern file bigger than this is no pattern file (it is refused, not read)


class Patterns:
    """rows: [(line number, compiled pattern)]; problem: why the file cannot be used whole (a line that is no valid
    pattern, a file that cannot be read), or None; present: whether there is a file at all."""

    def __init__(self, rows=(), problem=None, present=False, path=''):
        self.rows, self.problem, self.present, self.path = list(rows), problem, present, path

    def first(self, text):
        """The line number of the first pattern found in text, or None."""
        if not text:
            return None
        for n, rx in self.rows:
            if rx.search(text):
                return n
        return None


def plain(text):
    """A plain-text pattern as a regular expression: any case, not inside a longer word or number."""
    rx = re.escape(text)
    if re.match(r'\w', text):
        rx = r'(?<![^\W_])' + rx
    if re.search(r'\w$', text):
        rx += r'(?![^\W_])'
    return rx


def parse(text, path=''):
    rows, bad = [], []
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        regex = s.startswith('re:')
        if regex and not s[3:].strip():
            bad.append(n)
            continue
        try:
            rows.append((n, re.compile(s[3:].strip() if regex else plain(s), re.I)))
        except re.error:
            bad.append(n)
    problem = None
    if bad:
        problem = 'line ' + ', '.join(map(str, bad)) + ' of the pattern file is no valid pattern'
    return Patterns(rows, problem, True, path)


def load(path):
    """The patterns of the file at path. No file: no patterns and no problem (nothing to guard yet; self-check
    says so). A file that is there but cannot be read whole is a problem: the guard then refuses every post."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return Patterns(path=path)
    except OSError as e:
        return Patterns(problem=f'the pattern file cannot be read ({type(e).__name__})', present=True, path=path)
    import stat
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE:
        return Patterns(problem='the pattern file is not a plain file of at most 1 MB', present=True, path=path)
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read(MAX_FILE + 1)
    except (OSError, UnicodeDecodeError) as e:
        return Patterns(problem=f'the pattern file cannot be read ({type(e).__name__})', present=True, path=path)
    return parse(text, path)


def in_checkout(path):
    """The git checkout the file is in (a folder above it holds .git), or None: a pattern file there could be
    committed and pushed with the repo."""
    d = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(d, '.git')):
            return d
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up
