"""Debian package versions compared the way dpkg compares them (deb-version(7)): [epoch:]upstream[-revision], with
`~` sorting before everything, even the end of the string. Pure; no dpkg needed."""
import re

VERSION_RE = re.compile(r'^(?:([0-9]+):)?([A-Za-z0-9.+~:-]+?)(?:-([A-Za-z0-9.+~]+))?$')


def _order(c):
    if c == '~':
        return -1
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    return ord(c) + 256


def _part(a, b):
    """dpkg's verrevcmp for one upstream or revision part."""
    i = j = 0
    while i < len(a) or j < len(b):
        first = 0
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = _order(a[i]) if i < len(a) else 0
            bc = _order(b[j]) if j < len(b) else 0
            if ac != bc:
                return ac - bc
            i, j = i + 1, j + 1
        while i < len(a) and a[i] == '0':
            i += 1
        while j < len(b) and b[j] == '0':
            j += 1
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            if not first:
                first = ord(a[i]) - ord(b[j])
            i, j = i + 1, j + 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first:
            return first
    return 0


def split(version):
    """(epoch, upstream, revision). Raises ValueError for a string that is no Debian version."""
    m = VERSION_RE.match(version.strip())
    if not m:
        raise ValueError(f'not a Debian version: {version!r}')
    return int(m.group(1) or 0), m.group(2), m.group(3) or ''


def compare(a, b):
    """<0, 0 or >0 as version a sorts before, with or after version b."""
    ea, ua, ra = split(a)
    eb, ub, rb = split(b)
    if ea != eb:
        return ea - eb
    return _part(ua, ub) or _part(ra, rb)
