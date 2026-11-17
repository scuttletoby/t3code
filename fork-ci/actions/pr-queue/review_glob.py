"""Does a glob of the `review` block's dialect match a changed path (README.md, "Review rules", Globs)?

The dialect (review_config.py checks a glob's syntax; this module only matches):
  - paths are repo-relative and use `/`; a path is split into its segments at `/`;
  - `**` (always a whole segment) matches any number of whole segments, none included: `a/**` matches `a/x/y` and
    the file `a` itself, and `**` matches every path;
  - `*` matches any characters inside one segment, none included: it never crosses `/`;
  - `?` matches one character inside one segment;
  - every other character matches itself, case-sensitively; `.` at the start of a name is not special (`*` matches
    `.github`, `**` matches `.github/x`).
No fnmatch, glob or pathlib: their `*` crosses `/` or skips dot-names, and `[...]` would mean something here.
Pure: no file system, no git. The TypeScript side matches the same way, on the same vectors (review.vectors.json).
"""


def segment_matches(glob_seg, path_seg):
    """True when one glob segment (no `**`) matches one whole path segment. The classic wildcard walk: on a mismatch,
    go back to the last `*` and let it take one more character. At most len(glob) * len(path) steps, never
    exponential, whatever the glob (a regex of many `*` can backtrack for minutes)."""
    g, s = glob_seg, path_seg
    i = j = 0
    star, mark = -1, 0   # the last `*` seen in g, and where in s it started to match
    while j < len(s):
        if i < len(g) and g[i] == '*':
            star, mark = i, j
            i += 1
        elif i < len(g) and (g[i] == '?' or g[i] == s[j]):
            i += 1
            j += 1
        elif star >= 0:
            mark += 1
            i, j = star + 1, mark
        else:
            return False
    while i < len(g) and g[i] == '*':
        i += 1
    return i == len(g)


def matches(glob, path):
    """True when `glob` matches the whole repo-relative `path`. Both are split at `/`; `**` takes zero or more whole
    segments, every other glob segment takes exactly one path segment."""
    g, p = glob.split('/'), path.split('/')
    # reach[j]: glob segments g[:i] can match path segments p[:j]. One row per glob segment: no recursion, so a deep
    # path or many `**` cannot exhaust the stack or take exponential time.
    reach = [True] + [False] * len(p)
    for seg in g:
        if seg == '**':
            nxt, seen = [], False
            for j in range(len(p) + 1):
                seen = seen or reach[j]
                nxt.append(seen)
        else:
            nxt = [False] + [reach[j] and segment_matches(seg, p[j]) for j in range(len(p))]
        reach = nxt
        if not any(reach):
            return False
    return reach[len(p)]


def matches_any(globs, path):
    return any(matches(g, path) for g in globs)
