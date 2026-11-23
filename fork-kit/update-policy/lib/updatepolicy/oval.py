"""A distribution's OVAL file (Ubuntu's com.ubuntu.<codename>.cve.oval.xml.bz2, Debian's
oval-definitions-<codename>.xml.bz2) read into advisories: which binary packages a CVE affects, and in which version
it is fixed (None: no fix yet).

Only what dpkg-based OVAL says through `dpkginfo` tests is read: a test names binary packages (directly or through a
constant variable) and, when the CVE is fixed, a state "evr less than FIXED". A test without a state means "affected
in every version". Tests of other kinds (the running kernel, the release itself) are not read: the kernel's CVEs are
judged by Ubuntu with its own tests, and a new kernel is the restart policy's business (/run/reboot-required).

The file is read as a stream (bz2 or plain XML): definitions are dropped once read, so a 300 MB file needs a few
tens of MB of memory. A file that holds no definition or no dpkginfo test is "not understood" (FormatError): a
format change never reads as "no advisories".
"""
import bz2, re, xml.etree.ElementTree as ET

CVE_RE = re.compile(r'\bCVE-[0-9]{4}-[0-9]{4,}\b')


class FormatError(Exception):
    pass


class Advisory:
    __slots__ = ('cve', 'source', 'binaries', 'fixed', 'priority', 'note')

    def __init__(self, cve, source, binaries, fixed, priority, note=''):
        self.cve, self.source, self.binaries, self.fixed = cve, source, binaries, fixed
        self.priority, self.note = priority, note


def _local(tag):
    return tag.rsplit('}', 1)[-1]


def _open(path):
    with open(path, 'rb') as f:
        magic = f.read(3)
    return bz2.open(path, 'rb') if magic == b'BZh' else open(path, 'rb')


def read(path):
    """[Advisory] of the OVAL file at `path`. Raises FormatError (or OSError)."""
    tests, objects, states, variables, defs = {}, {}, {}, {}, []
    try:
        with _open(path) as f:
            for _event, el in ET.iterparse(f, events=('end',)):
                tag = _local(el.tag)
                if tag == 'definition':
                    defs.append(_definition(el))
                    el.clear()
                elif tag == 'dpkginfo_test':
                    obj = state = None
                    for c in el:
                        if _local(c.tag) == 'object':
                            obj = c.get('object_ref')
                        elif _local(c.tag) == 'state':
                            state = c.get('state_ref')
                    tests[el.get('id')] = (obj, state)
                    el.clear()
                elif tag == 'dpkginfo_object':
                    for c in el:
                        if _local(c.tag) == 'name':
                            objects[el.get('id')] = ('var', c.get('var_ref')) if c.get('var_ref') else \
                                ('name', (c.text or '').strip())
                    el.clear()
                elif tag == 'dpkginfo_state':
                    for c in el:
                        if _local(c.tag) == 'evr':
                            states[el.get('id')] = (c.get('operation') or 'equals', (c.text or '').strip())
                    el.clear()
                elif tag == 'constant_variable':
                    variables[el.get('id')] = [(v.text or '').strip() for v in el if _local(v.tag) == 'value']
                    el.clear()
    except (ET.ParseError, EOFError, OSError, ValueError) as e:
        raise FormatError(f'not readable OVAL ({type(e).__name__}: {e})') from None
    if not defs or not tests:
        raise FormatError(f'no {"definitions" if not defs else "dpkginfo tests"}: a format this check does not know')
    out = []
    for cves, priority, refs, title in defs:
        for ref, comment in refs:
            test = tests.get(ref)
            if test is None:
                continue                                # a test of another kind (kernel, release)
            kind, value = objects.get(test[0], (None, None))
            names = variables.get(value, []) if kind == 'var' else [value] if kind == 'name' else []
            if not names:
                continue
            fixed = None
            if test[1] is not None:
                op, version = states.get(test[1], (None, None))
                if op != 'less than' or not version:
                    continue                            # a state this check does not read
                fixed = version
            source = _source(comment) or names[0]
            note = 'esm' if 'esm' in (comment + (fixed or '')).lower() else ''
            for cve in cves:
                out.append(Advisory(cve, source, tuple(n for n in names if n), fixed, priority, note))
    return out


def _source(comment):
    m = re.match(r'^(?:\(CVE-[^)]*\) )?(\S+) (?:source )?package', comment or '')
    return m.group(1) if m else ''


def _definition(el):
    """(CVE ids, priority, [(test_ref, comment)], title) of one definition element."""
    cves, priority, refs, title = [], '', [], ''
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
        elif tag == 'criterion' and c.get('test_ref'):
            refs.append((c.get('test_ref'), c.get('comment') or ''))
        elif tag == 'title':
            title = (c.text or '').strip()
    if not cves:
        title_cves = CVE_RE.findall(title)
        cves = title_cves
    return sorted(set(cves)), priority, refs, title
