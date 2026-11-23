"""A distribution's OVAL file (Ubuntu's com.ubuntu.<codename>.cve.oval.xml.bz2, Debian's
oval-definitions-<codename>.xml.bz2) read into advisories: which packages a CVE affects and in which version it is
fixed (None: no fix yet), and for Ubuntu's kernels which running kernel it affects.

What is read:
- `dpkginfo` tests: binary (Ubuntu) or source (Debian) package names, directly or through a constant variable, and
  a state "evr less than FIXED" (none: affected in every version). Debian writes "no fix yet" as "less than 0:0";
  that is read as no fixed version.
- Ubuntu's kernels: a `uname` test whose state matches the running kernel's release (one per kernel flavour), alone
  (affected, no fix) or together with a `variable_test` "kernel version less than FIXED" on the local variable that
  turns `uname -r` into a version.
- platform checks (the release, the architecture, the family) are known and skipped.

Anything else a definition refers to (another test kind, a state operation other than "less than", an object or
variable that is not there) raises FormatError: a format change is "cannot check", never "no advisories". So does a
file whose decompressed size passes `max_bytes`, an entry with more than MAX_ENTRY elements, and a file where fewer
than RESOLVED of the dpkginfo tests resolve. The file is read as a stream and every finished entry is dropped, so
memory stays at what is kept (a real noble file: about 250 MB).
"""
import bz2, io, re, xml.etree.ElementTree as ET

from updatepolicy import debver

CVE_RE = re.compile(r'\bCVE-[0-9]{4}-[0-9]{4,}\b')
PLATFORM = frozenset({'family_test', 'textfilecontent54_test', 'textfilecontent_test'})
KINDS = PLATFORM | {'dpkginfo_test', 'uname_test', 'variable_test'}
MAX_ENTRY = 500_000             # elements in one definition, test, object, state or variable
RESOLVED = 0.9
SECTIONS = frozenset({'definitions', 'tests', 'objects', 'states', 'variables', 'generator'})


class FormatError(Exception):
    pass


class Advisory:
    __slots__ = ('cve', 'source', 'binaries', 'fixed', 'priority', 'note')

    def __init__(self, cve, source, binaries, fixed, priority, note=''):
        self.cve, self.source, self.binaries, self.fixed = cve, source, binaries, fixed
        self.priority, self.note = priority, note


class Kernel:
    """A CVE of one Ubuntu kernel flavour: `pattern` matches the running release (`uname -r`); `fixed` is the
    fixed kernel version (debian evr) or None; `version` turns the release into that version."""
    __slots__ = ('cve', 'flavour', 'pattern', 'fixed', 'priority', 'version')

    def __init__(self, cve, flavour, pattern, fixed, priority, version):
        self.cve, self.flavour, self.pattern, self.fixed = cve, flavour, pattern, fixed
        self.priority, self.version = priority, version


class Data:
    """advisories; kernels: the kernel entries that can match the running release (all when none was given);
    counts: {'packages': N, 'kernels': M} of the whole file (M counts every kernel entry, kept or not)."""

    def __init__(self, advisories, kernels, counts):
        self.advisories, self.kernels, self.counts = advisories, kernels, counts


class _Capped(io.RawIOBase):
    """A readable that raises FormatError past `limit` bytes (the decompressed size of a bz2 or gzip file)."""

    def __init__(self, f, limit):
        super().__init__()
        self.f, self.limit, self.seen = f, limit, 0

    def readable(self):
        return True

    def readinto(self, b):
        data = self.f.read(len(b))
        self.seen += len(data)
        if self.seen > self.limit:
            raise FormatError(f'larger than {self.limit >> 20} MB once decompressed')
        b[:len(data)] = data
        return len(data)


def capped(f, limit):
    return io.BufferedReader(_Capped(f, limit))


def _local(tag):
    return tag.rsplit('}', 1)[-1]


def _open(path):
    with open(path, 'rb') as f:
        magic = f.read(3)
    return bz2.open(path, 'rb') if magic == b'BZh' else open(path, 'rb')


def no_fix(version):
    """Debian's "less than 0:0" (and 0): nothing sorts below it, so it means no fixed version."""
    try:
        return debver.compare(version, '0') == 0
    except ValueError:
        return False


def read(path, max_bytes=1 << 30, release=None):
    """Data of the OVAL file at `path`. With `release` (`uname -r`), only the kernel entries whose pattern matches it
    are kept (jammy's file has 887k kernel entries over 60 flavours). Raises FormatError (or OSError)."""
    tests, objects, states, variables, locals_, defs = {}, {}, {}, {}, {}, []
    try:
        with _open(path) as raw:
            stack, sizes = [], []
            for event, el in ET.iterparse(capped(raw, max_bytes), events=('start', 'end')):
                if event == 'start':
                    stack.append(el)
                    if len(stack) == 3:
                        sizes.append(0)
                    elif len(stack) > 3:
                        sizes[-1] += 1
                        if sizes[-1] > MAX_ENTRY:
                            raise FormatError(f'an entry with more than {MAX_ENTRY} elements')
                    continue
                stack.pop()
                depth = len(stack) + 1
                if depth == 3:
                    sizes.pop()
                    _entry(el, tests, objects, states, variables, locals_, defs)
                if depth in (2, 3) and stack:
                    stack[-1].remove(el)            # finished entries and sections are dropped
                    if depth == 2 and _local(el.tag) not in SECTIONS:
                        raise FormatError(f'an unknown section <{_local(el.tag)}>')
    except FormatError:
        raise
    except (ET.ParseError, EOFError, OSError, ValueError) as e:
        raise FormatError(f'not readable OVAL ({type(e).__name__}: {e})') from None
    if not defs or not any(t[0] == 'dpkginfo_test' for t in tests.values()):
        raise FormatError(f'no {"definitions" if not defs else "dpkginfo tests"}: a format this check does not know')
    return _resolve(tests, objects, states, variables, locals_, defs, release)


def _entry(el, tests, objects, states, variables, locals_, defs):
    tag, ident = _local(el.tag), el.get('id')
    kids = list(el)
    if tag == 'definition':
        defs.append(_definition(el))
    elif tag.endswith('_test'):
        obj = next((c.get('object_ref') for c in kids if _local(c.tag) == 'object'), None)
        state = next((c.get('state_ref') for c in kids if _local(c.tag) == 'state'), None)
        tests[ident] = (tag, obj, state, el.get('comment') or '')
    elif tag == 'dpkginfo_object':
        name = next((c for c in kids if _local(c.tag) == 'name'), None)
        if name is not None:
            objects[ident] = ('var', name.get('var_ref')) if name.get('var_ref') else ('name', (name.text or '').strip())
    elif tag == 'variable_object':
        ref = next((c for c in kids if _local(c.tag) == 'var_ref'), None)
        if ref is not None:
            objects[ident] = ('local', (ref.text or '').strip())
    elif tag in ('dpkginfo_state', 'variable_state'):
        value = next((c for c in kids if _local(c.tag) in ('evr', 'value')), None)
        if value is not None:
            states[ident] = ('evr', value.get('operation') or 'equals', (value.text or '').strip())
    elif tag == 'uname_state':
        release = next((c for c in kids if _local(c.tag) == 'os_release'), None)
        states[ident] = ('release', release.get('operation'), (release.text or '').strip()) if release is not None \
            else ('other', None, None)
    elif tag == 'constant_variable':
        variables[ident] = [(v.text or '').strip() for v in kids if _local(v.tag) == 'value']
    elif tag == 'local_variable':
        literal = ''.join((c.text or '') for c in el.iter() if _local(c.tag) == 'literal_component')
        capture = next((c.get('pattern') for c in el.iter() if _local(c.tag) == 'regex_capture'), None)
        locals_[ident] = (literal, capture)


def _definition(el):
    """(CVE ids, priority, [[(test_ref, comment)] per criteria element], title)."""
    cves, priority, groups, title = [], '', [], ''
    for c in el.iter():
        tag = _local(c.tag)
        if tag == 'reference' and c.get('source') == 'CVE' and CVE_RE.fullmatch(c.get('ref_id') or ''):
            cves.append(c.get('ref_id'))
        elif tag == 'cve':
            text = (c.text or '').strip()
            if CVE_RE.fullmatch(text):
                cves.append(text)
            priority = priority or (c.get('priority') or '')
        elif tag == 'severity' and not priority:
            priority = (c.text or '').strip().lower()
        elif tag == 'criteria':
            groups.append([(k.get('test_ref'), k.get('comment') or '') for k in c
                           if _local(k.tag) == 'criterion' and k.get('test_ref')])
        elif tag == 'title':
            title = (c.text or '').strip()
    return sorted(set(cves or CVE_RE.findall(title))), priority, groups, title


def _source(comment):
    m = re.match(r'^(?:\(CVE-[^)]*\) )?(\S+) (?:source )?package', comment or '')
    return m.group(1) if m else ''


def _resolve(tests, objects, states, variables, locals_, defs, release=None):
    def test(ref):
        t = tests.get(ref)
        if t is None:
            raise FormatError(f'a definition refers to a test that is not there ({ref})')
        if t[0] not in KINDS:
            raise FormatError(f'a test kind this check does not read ({t[0]})')
        return t

    resolved = total = kernel_total = 0
    out, kernels, checked = [], [], set()
    for cves, priority, groups, title in defs:
        for group in groups:
            kinds = [(ref, comment, test(ref)) for ref, comment in group]
            unames = [(r, c, t) for r, c, t in kinds if t[0] == 'uname_test' and states.get(t[2], ('',))[0] == 'release']
            versions = [(r, c, t) for r, c, t in kinds if t[0] == 'variable_test']
            if versions and len(unames) != 1:
                raise FormatError('a kernel version test without its one kernel test')
            for ref, comment, t in unames:
                kind, op, pattern = states[t[2]]
                if op != 'pattern match':
                    raise FormatError(f'a kernel test with operation "{op}"')
                try:
                    compiled = re.compile(pattern)
                except re.error as e:
                    raise FormatError(f'a kernel pattern this check cannot read ({e})') from None
                flavour = (re.search(r"kernel '([^']+)'", t[3] + ' ' + comment) or [None, 'linux'])[1]
                fixed = version = None
                if versions:
                    _r, _c, vt = versions[0]
                    kind, op, value = states.get(vt[2], (None, None, None))
                    obj = objects.get(vt[1])
                    if kind != 'evr' or op != 'less than' or not obj or obj[0] != 'local' or obj[1] not in locals_ \
                            or not locals_[obj[1]][1]:
                        raise FormatError('a kernel version test this check does not read')
                    fixed, version = value, locals_[obj[1]]
                    if (fixed, version) not in checked:     # a broken entry: cannot check, never a traceback
                        try:
                            debver.split(fixed)
                            if re.compile(version[1]).groups < 1:
                                raise ValueError('its version pattern captures nothing')
                        except (ValueError, re.error) as e:
                            raise FormatError(f'a kernel version this check cannot read ({e})') from None
                        checked.add((fixed, version))
                kernel_total += len(cves)
                if release is not None and not compiled.search(release):
                    continue                                # another kernel than the running one
                for cve in cves:
                    kernels.append(Kernel(cve, flavour, compiled, fixed, priority, version))
            for ref, comment, t in kinds:
                if t[0] != 'dpkginfo_test':
                    continue
                total += 1
                obj = objects.get(t[1])
                if obj is None:
                    raise FormatError(f'a dpkginfo test whose object is not there ({t[1]})')
                names = variables.get(obj[1]) if obj[0] == 'var' else [obj[1]] if obj[0] == 'name' else None
                if names is None:
                    raise FormatError(f'a dpkginfo object whose names are not there ({obj[1]})')
                fixed = None
                if t[2] is not None:
                    state = states.get(t[2])
                    if state is None or state[0] != 'evr':
                        raise FormatError(f'a dpkginfo test whose state is not there ({t[2]})')
                    if state[1] != 'less than' or not state[2]:
                        raise FormatError(f'a dpkginfo state with operation "{state[1]}"')
                    fixed = None if no_fix(state[2]) else state[2]
                names = tuple(n for n in names if n)
                if not names:
                    continue
                resolved += 1
                source = _source(comment) or names[0]
                note = 'esm' if 'esm' in (comment + (fixed or '')).lower() else ''
                for cve in cves:
                    out.append(Advisory(cve, source, names, fixed, priority, note))
    if total and resolved < RESOLVED * total:
        raise FormatError(f'only {resolved} of {total} package tests name a package')
    return Data(out, kernels, {'packages': len(out), 'kernels': kernel_total})


