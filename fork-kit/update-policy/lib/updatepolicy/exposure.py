"""The daily exposure check (#1045 part 3): installed packages and the running kernel against the distribution's
security data, CISA's Known Exploited Vulnerabilities (KEV) list and EPSS. It prints only what needs action, one line
per CVE (KEV) or per source package (a waiting fix), at most MAX_LINES, and exits 1 when there is any; 0 when there is
none. Notes (what it could not look at, such as "kernel: not checked") are printed too but are no finding.

- KEV, fix available: a CVE on CISA's list that affects an installed version (or the running kernel), with a fixed
  version out: update (a kernel: and restart).
- KEV, no fix yet: the same with no fixed version: mitigate or watch.
- security fix waiting: a source package with a security fix out, shown when its EPSS is at least exposure.epssMin
  or a fix has been waiting for more than exposure.fixWaitingDays (dated per CVE and package from when this check
  first saw it); ranked KEV first, then by EPSS.
- cannot check: a feed that could not be downloaded or read, or one far smaller than the kept copy; a distribution it
  has no data source for; installed packages it cannot list. Never read as "all clear".
- drift: a cached feed older than exposure.staleDays; an ignore entry past its date.

Data (each a feed, see feeds.py): the distribution's OVAL file (oval.py), KEV's JSON (cisa.gov), FIRST.org's daily
EPSS file (all CVEs, so nothing about this machine is sent anywhere). The ecosystems table is where another package
system (rpm, apk) gets its installed list, version order and data source.
"""
import datetime, fcntl, gzip, io, json, os, re, tempfile

from updatepolicy import debver, feeds, oval, settings as settings_mod

UTC = datetime.timezone.utc
one_line = settings_mod.one_line
CVE_RE = re.compile(r'^CVE-[0-9]{4}-[0-9]{4,}\Z')
KEV_URL = 'https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json'
EPSS_URL = 'https://epss.empiricalsecurity.com/epss_scores-current.csv.gz'
CACHE = '/var/cache/update-policy-exposure'
REPORT = 'report.json'
MAX_LINES = 40
# seconds for exposure-alert's one alert: it runs as the exposure unit's ExecStopPost=, which systemd stops after
# TimeoutStopSec (90 s by default, and the unit sets none)
ALERT_BUDGET = 75
# absolute minimums of a feed's entries (a real one holds far more: noble's OVAL ~540k, KEV ~1,800, EPSS ~410k)
FLOORS = {'oval': 1000, 'kev': 500, 'epss': 100_000}
# kernel packages: judged by the running kernel only, never by what else is installed
KERNEL_PKG = re.compile(r'^linux-(image|headers|modules|modules-extra|tools|cloud-tools|buildinfo|objects|lib-rust)-')
INSTALLED, NOT_INSTALLED = frozenset('iHUFWt'), frozenset('nc')


class CannotCheck(Exception):
    pass


# -- ecosystems: installed packages, version order, data source -------------------------------------------------------

class Dpkg:
    """Debian and Ubuntu: dpkg's database, dpkg's version order, the distribution's OVAL file."""
    compare = staticmethod(debver.compare)

    def __init__(self, oval_url, no_fix='has no fix yet: mitigate or watch'):
        self.oval_url, self.no_fix = oval_url, no_fix

    def installed(self, host):
        """({name: [(binary, version)]}, {binary: (source, version)}). Every installed binary under its own name and
        under its source's name (Debian's OVAL names source packages, Ubuntu's binaries). Installed: dpkg's status
        installed, or with its files on disk (half-installed, unpacked, half-configured, triggers). Kernel packages
        are left out of the first map: the running kernel is judged on its own."""
        code, out, err = host.run(['dpkg-query', '-W', '-f',
                                   '${db:Status-Abbrev}\\t${binary:Package}\\t${Version}\\t${source:Package}\\t'
                                   '${source:Version}\\n'], timeout=60)
        if code != 0:
            raise CannotCheck(f'the installed packages cannot be listed (dpkg-query: '
                              f'{one_line(err.strip() or f"exit {code}", 160)})')
        found, binaries = {}, {}
        for line in out.splitlines():
            parts = line.split('\t')
            if len(parts) != 5 or len(parts[0]) < 2:
                raise CannotCheck(f'dpkg-query printed a line this check does not read ({one_line(line, 80)})')
            status = parts[0][1]
            if status in NOT_INSTALLED:
                continue
            if status not in INSTALLED:
                raise CannotCheck(f'dpkg-query: a status this check does not know ({one_line(parts[0].strip(), 8)} for '
                                  f'{one_line(parts[1], 60)})')
            binary, version, source, source_version = parts[1].split(':')[0], parts[2], parts[3], parts[4]
            binaries[binary] = (source or binary, version)
            if KERNEL_PKG.match(binary):
                continue
            found.setdefault(binary, []).append((binary, version))
            if source and source != binary:
                found.setdefault(source, []).append((binary, source_version or version))
        if not binaries:
            raise CannotCheck('dpkg-query listed no installed package')
        return found, binaries


ECOSYSTEMS = {
    'ubuntu': lambda codename: Dpkg(f'https://security-metadata.canonical.com/oval/com.ubuntu.{codename}.cve.oval.xml.bz2'),
    # Debian writes "no fixed version" and some "does not apply" entries alike (less than 0:0)
    'debian': lambda codename: Dpkg(f'https://www.debian.org/security/oval/oval-definitions-{codename}.xml.bz2',
                                    'has no fixed version listed by Debian: mitigate or watch (or, if Debian judged it '
                                    'not to apply, add an ignore entry)'),
}


def os_release(host):
    """(ID, VERSION_CODENAME) of /etc/os-release."""
    fields = {}
    try:
        with open(host.p('/etc/os-release'), encoding='utf-8') as f:
            for line in f:
                key, _, value = line.strip().partition('=')
                fields[key] = value.strip().strip('"\'')
    except OSError as e:
        raise CannotCheck(f'/etc/os-release cannot be read ({e.strerror})') from None
    return fields.get('ID', ''), fields.get('VERSION_CODENAME', '')


# -- feeds ------------------------------------------------------------------------------------------------------------

def _floor(name, n):
    if n < FLOORS[name]:
        raise ValueError(f'only {n} entries, under the floor of {FLOORS[name]}')
    return n


def read_kev(path):
    """{CVE: {dueDate, product}} of CISA's KEV JSON. Raises ValueError for a format it does not know."""
    with open(path, encoding='utf-8') as f:
        data = json.loads(f.read(64 << 20))
    rows = data.get('vulnerabilities') if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError('no "vulnerabilities" list')
    out = {}
    for r in rows:
        cve = r.get('cveID') if isinstance(r, dict) else None
        if not isinstance(cve, str) or not CVE_RE.match(cve):
            raise ValueError(f'an entry without a CVE id ({one_line(repr(r), 80)})')
        out[cve] = {'dueDate': str(r.get('dueDate') or ''),
                    'product': f'{r.get("vendorProject", "")} {r.get("product", "")}'.strip()}
    return out


def read_epss(path, want=None, max_bytes=256 << 20):
    """FIRST.org's daily EPSS file (gzip CSV: an optional #model line, `cve,epss,percentile`, one row per CVE). Without
    `want`: how many rows it holds; with it: {CVE: score} of those CVEs. Raises ValueError for a format it does not
    know."""
    with gzip.open(path, 'rb') as raw:
        f = io.TextIOWrapper(oval.capped(raw, max_bytes), encoding='utf-8')
        line = f.readline()
        if line.startswith('#'):
            line = f.readline()
        if line.strip() != 'cve,epss,percentile':
            raise ValueError(f'no "cve,epss,percentile" header ({one_line(line.strip(), 60)})')
        n, scores = 0, {}
        for line in f:
            cve, _, rest = line.partition(',')
            score = float(rest.split(',')[0])
            if not CVE_RE.match(cve) or not 0 <= score <= 1:
                raise ValueError(f'a row this check does not read ({one_line(line.strip(), 60)})')
            n += 1
            if want is not None and cve in want:
                scores[cve] = score
    return scores if want is not None else n


# -- the check --------------------------------------------------------------------------------------------------------

def _ignores(ex, today):
    active, lines = {}, []
    for entry in ex['ignore']:
        if entry['until'] < today.isoformat():
            lines.append(f'drift: the ignore entry for {entry["cve"]} ended on {entry["until"]} ({entry["reason"]}): '
                         'remove it or give it a new date')
        else:
            active[entry['cve']] = entry
    return active, lines


def _read_state(cache):
    try:
        with open(os.path.join(cache, 'state.json'), encoding='utf-8') as f:
            st = json.load(f)
        return st if isinstance(st, dict) and isinstance(st.get('fixSeen'), dict) else {'fixSeen': {}}
    except (OSError, ValueError):
        return {'fixSeen': {}}


def _write_json(cache, name, data):
    fd, tmp = tempfile.mkstemp(dir=cache, prefix=f'.{name}.', suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, sort_keys=True)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(cache, name))


def kernel_flavour(release, binaries):
    """The flavour (Ubuntu kernel source) of the running kernel, from its installed packages, or None."""
    for name in (f'linux-modules-{release}', f'linux-image-unsigned-{release}', f'linux-image-{release}'):
        if name in binaries:
            source = binaries[name][0]
            return re.sub(r'^linux-signed-', 'linux-', source) if source.startswith('linux-signed') else source
    return None


def kernel_affected(data, release, binaries, distro):
    """([(cve, source, binary, version, fixed, note, priority)], note or None) of the running kernel."""
    if data.counts['kernels']:                          # Ubuntu: the running kernel's own tests
        candidates = [k for k in data.kernels if k.pattern.search(release)]
        flavours = sorted({k.flavour for k in candidates})
        if not flavours:
            return [], f'kernel: not checked (the running kernel {release} is none of {distro}\'s kernels in its data)'
        own = kernel_flavour(release, binaries)
        flavour = own if own in flavours else flavours[0] if len(flavours) == 1 else None
        if flavour is None:
            return [], (f'kernel: not checked (the running kernel {release} matches several kernels '
                        f'({", ".join(flavours[:4])}) and its packages do not say which)')
        out = []
        for k in candidates:
            if k.flavour != flavour:
                continue
            if k.fixed is None:
                out.append((k.cve, f'kernel {flavour}', f'kernel {release}', release, None, '', k.priority))
                continue
            literal, capture = k.version
            m = re.search(capture, release)
            if not m:
                return [], f'kernel: not checked (its version cannot be read from {release})'
            running = literal + m.group(1)
            if debver.compare(running, k.fixed) < 0:
                out.append((k.cve, f'kernel {flavour}', f'kernel {release}', release,
                            k.fixed[2:] if k.fixed.startswith('0:') else k.fixed, '', k.priority))
        return out, None
    image = next((n for n in (f'linux-image-{release}', f'linux-image-{release}-unsigned') if n in binaries), None)
    if image is None:                                   # Debian: the running image is source `linux`
        return [], f'kernel: not checked (the running kernel {release} has no linux-image package)'
    out = []
    for adv in data.advisories:
        if 'linux' in adv.binaries:
            version = binaries[image][1]
            if adv.fixed is None or debver.compare(version, adv.fixed) < 0:
                fixed = adv.fixed[2:] if adv.fixed and adv.fixed.startswith('0:') else adv.fixed
                out.append((adv.cve, 'kernel linux', image, version, fixed, adv.note, adv.priority))
    return out, None


def _seen_key(cve, source, binary):
    """The waiting clock's key: per CVE and package; for the kernel per flavour, so booting a newer kernel that is
    still behind keeps the days."""
    return f'{cve} {source}' if source.startswith('kernel ') else f'{cve} {binary}'


def _names(binaries):
    names = sorted(binaries)
    return ', '.join(names[:3]) + (f' +{len(names) - 3}' if len(names) > 3 else '')


def check(host, s, cache, now, opener=feeds.urlopen):
    """(lines, notes, report). Lines: what needs action, cannot-check and drift lines; [] means nothing to do. Call
    under the cache's lock."""
    ex = s['exposure']
    if not ex['enabled']:
        return [], [], {'v': 2, 'at': now.isoformat(), 'enabled': False, 'lines': [], 'notes': []}
    feeds.sweep(cache)
    try:
        os.unlink(os.path.join(cache, 'epss.json'))     # the first format's per-CVE EPSS answers: not used
    except FileNotFoundError:
        pass
    today = now.date()
    ignored, drift = _ignores(ex, today)
    notes = []
    try:
        distro, codename = os_release(host)
        make = ECOSYSTEMS.get(distro)
        if make is None or not re.match(r'^[a-z0-9-]{1,40}$', codename or ''):
            raise CannotCheck(f'no security data source for this distribution ({one_line(distro or "unknown", 40)} '
                              f'{one_line(codename, 40)}); the check knows {", ".join(sorted(ECOSYSTEMS))}')
        eco = make(codename)
        installed, binaries = eco.installed(host)
    except CannotCheck as e:
        lines = [one_line(f'cannot check: {e}', 400)] + drift
        return lines, notes, {'v': 2, 'at': now.isoformat(), 'enabled': True, 'lines': lines, 'notes': notes}
    cap, timeout, problems, parsed = ex['maxFeedMb'] << 20, ex['timeoutSeconds'], [], {}
    release = host.kernel_release()
    xml_cap = ex['maxXmlMb'] << 20

    def check_oval(path):                           # a new download is read once: to check it and to use it
        parsed[path] = oval.read(path, xml_cap, release)
        return dict(parsed[path].counts, packages=_floor('oval', parsed[path].counts['packages']))
    got = {'oval': feeds.fetch(cache, 'oval', ex['ovalUrl'] or eco.oval_url, check_oval, now, cap, timeout, opener),
           'kev': feeds.fetch(cache, 'kev', ex['kevUrl'] or KEV_URL, lambda p: _floor('kev', len(read_kev(p))), now,
                              cap, timeout, opener),
           'epss': feeds.fetch(cache, 'epss', ex['epssUrl'] or EPSS_URL, lambda p: _floor('epss', read_epss(p)), now,
                               cap, timeout, opener)}
    problems += [f.problem for f in got.values() if f.problem]
    drift += [f'drift: {f.notice}' for f in got.values() if f.notice]
    data = kev = None
    if got['oval'].path:
        try:
            fresh = [d for p, d in parsed.items() if not got['oval'].problem]
            data = fresh[0] if fresh else oval.read(got['oval'].path, xml_cap, release)
        except (oval.FormatError, OSError) as e:
            problems.append(f'oval: the kept copy cannot be read ({e})')
    if got['kev'].path:
        try:
            kev = read_kev(got['kev'].path)
        except (OSError, ValueError) as e:
            problems.append(f'kev: the kept copy cannot be read ({e})')
    for name, f in got.items():
        if f.path and f.fetched_at and now - f.fetched_at > datetime.timedelta(days=ex['staleDays']):
            drift.append(f'drift: the cached {name} feed is {(now - f.fetched_at).days} days old (more than '
                         f'{ex["staleDays"]}): the check works on old data')
    lines = [f'cannot check: {p}' for p in problems]
    if data is None:
        lines.append('cannot check: no usable copy of the distribution\'s security data')
        notes.append('kernel: not checked (no security data)')
    if kev is None:
        lines.append('cannot check: no usable copy of CISA\'s KEV list')
    affected = []                                   # (cve, source, binary, version, fixed, note, priority)
    for adv in data.advisories if data else ():
        for name in adv.binaries:
            for binary, version in installed.get(name, ()):
                try:
                    if adv.fixed is None or eco.compare(version, adv.fixed) < 0:
                        affected.append((adv.cve, adv.source or name, binary, version, adv.fixed, adv.note,
                                         adv.priority))
                except ValueError:
                    continue
    if data is not None:
        found, note = kernel_affected(data, release, binaries, distro)
        affected += found
        if note:
            notes.append(one_line(note, 300))
    affected = [a for a in affected if a[0] not in ignored]
    kev = kev or {}
    state = _read_state(cache)
    seen = dict(state['fixSeen']) if data is None else {}      # no usable data: keep every first sight
    # the first cache format keyed "BINARY FIXED": its first sights carry over (per binary)
    older = {k.split(' ', 1)[0]: v for k, v in state['fixSeen'].items() if not k.startswith('CVE-')}
    for cve, source, binary, _v, fixed, _n, _p in affected:
        if fixed is not None:
            key = _seen_key(cve, source, binary)
            seen[key] = state['fixSeen'].get(key) or older.get(binary) or now.isoformat()
    scores = {}
    if got['epss'].path:
        want = {a[0] for a in affected}
        try:
            scores = read_epss(got['epss'].path, want) if want else {}
        except (OSError, ValueError, oval.FormatError) as e:
            lines.append(one_line(f'cannot check: epss: the kept copy cannot be read ({e})', 300))
    elif 'epss' not in ' '.join(problems):
        lines.append('cannot check: no usable copy of the EPSS scores')
    rows = []
    by_cve = {}
    for a in affected:
        if a[0] in kev:
            by_cve.setdefault(a[0], []).append(a)
    for cve, items in by_cve.items():
        sources = {}
        for _c, source, binary, version, fixed, note, _p in items:
            g = sources.setdefault(source, {'binaries': set(), 'version': version, 'fixed': fixed, 'note': note})
            g['binaries'].add(binary)
            if g['fixed'] is not None and (fixed is None or eco.compare(fixed, g['fixed']) > 0):
                g['fixed'] = fixed                  # the highest fix of the source (none: no fix)
            if not source.startswith('kernel') and eco.compare(version, g['version']) < 0:
                g['version'] = version
        parts, unfixed = [], []
        for source, g in sorted(sources.items()):
            who = f'{source} {g["version"]} ({_names(g["binaries"])})'
            if g['fixed'] is None:
                unfixed.append(who)
            else:
                restart = ' and restart' if source.startswith('kernel') else ''
                pro = ' (the fix needs Ubuntu Pro: esm)' if g['note'] == 'esm' else ''
                parts.append(f'{who}: update to {g["fixed"]}{restart}{pro}')
        top = scores.get(cve, 0.0)
        info = f'(EPSS {top:.2f}; CISA due {kev[cve]["dueDate"] or "?"}; {kev[cve]["product"]})'
        if parts:
            text = f'KEV {cve} is exploited in the wild: ' + '; '.join(parts)
            if unfixed:
                text += '; no fix yet: ' + '; '.join(unfixed)
        else:
            text = f'KEV {cve} is exploited in the wild and {eco.no_fix}: ' + '; '.join(unfixed)
        rows.append((0, -top, one_line(f'{text} {info}', 400), {'cve': cve, 'kev': True, 'epss': top,
                                                                'action': 'update' if parts else 'mitigate',
                                                                'sources': sorted(sources)}))
    waiting = {}
    for cve, source, binary, version, fixed, note, _p in affected:
        if fixed is None or cve in kev:
            continue
        w = waiting.setdefault(source, {'binaries': set(), 'version': version, 'fixed': fixed, 'cves': set(),
                                        'note': '', 'since': now})
        w['binaries'].add(binary)
        w['cves'].add(cve)
        if eco.compare(fixed, w['fixed']) > 0:
            w['fixed'] = fixed
        w['note'] = w['note'] or note
        first = feeds._parse_time(seen.get(_seen_key(cve, source, binary))) or now
        w['since'] = min(w['since'], first)
    for source, w in waiting.items():
        top = max((scores.get(c, 0.0) for c in w['cves']), default=0.0)
        days = max(0, (now - w['since']).days)
        if not (top >= ex['epssMin'] or days > ex['fixWaitingDays']):
            continue
        pro = ' (the fix needs Ubuntu Pro: esm)' if w['note'] == 'esm' else ''
        restart = ' and restart' if source.startswith('kernel') else ''
        cves = sorted(w['cves'], key=lambda c: (-scores.get(c, 0.0), c))
        more = f' +{len(cves) - 4}' if len(cves) > 4 else ''
        text = (f'security fix waiting {days} day(s): {source} {w["version"]} ({_names(w["binaries"])}) -> '
                f'{w["fixed"]}{restart}{pro}: {", ".join(cves[:4])}{more} (EPSS {top:.2f})')
        rows.append((1, -top, one_line(text, 400), {'source': source, 'version': w['version'], 'fixed': w['fixed'],
                                                     'cves': cves, 'epss': top, 'days': days, 'action': 'update'}))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    found = [r[2] for r in rows]
    if len(found) > MAX_LINES:
        found = found[:MAX_LINES - 1] + [f'+{len(found) - MAX_LINES + 1} more lines (all in {cache}/{REPORT})']
    lines = [one_line(x, 400) for x in lines + found + drift]
    try:
        _write_json(cache, 'state.json', dict(state, fixSeen=seen))
    except OSError as e:
        lines.append(f'cannot check: the cache folder {cache} cannot be written ({e.strerror})')
    return lines, notes, {'v': 2, 'at': now.isoformat(), 'enabled': True, 'lines': lines, 'notes': notes,
                          'findings': [r[3] for r in rows]}


def run(host, read_settings, cache, now=None, opener=feeds.urlopen):
    """`update-policy exposure`: prints the lines and notes, writes the report for the alert step; 0 none, 1 any."""
    now = now or datetime.datetime.now(UTC)
    if os.geteuid() == 0 and os.path.abspath(cache) == CACHE:
        print('update-policy exposure: as root it would leave root-owned files in the unit\'s cache: run '
              '`systemctl start update-policy-exposure.service` (journalctl -u update-policy-exposure), or pass '
              '--cache DIR')
        return 1
    try:
        s = read_settings()
    except settings_mod.MachineFileError as e:
        print(one_line(f'cannot check: the machine file cannot be used: {e}', 400))
        return 1
    if not s['exposure']['enabled']:
        print('update-policy exposure: off (exposure.enabled is false in the machine file)')
        return 0
    lock = None
    try:
        os.makedirs(cache, mode=0o750, exist_ok=True)
        lock = os.open(os.path.join(cache, 'lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('update-policy exposure: another run holds the cache\'s lock: nothing to do')
            return 0
        try:
            lines, notes, report = check(host, s, cache, now, opener)
        except Exception as e:      # noqa: BLE001 - a fault is a cannot-check report, never a missing one
            lines = [one_line(f'cannot check: the check failed ({type(e).__name__}: {e})', 300)]
            notes, report = [], {'v': 2, 'at': now.isoformat(), 'enabled': True, 'lines': lines, 'notes': []}
        try:
            _write_json(cache, REPORT, report)              # under the lock: no other run sweeps its temporary file
        except OSError:
            pass
    except OSError as e:
        lines, notes = [one_line(f'cannot check: the cache folder {cache} cannot be used ({e.strerror or e})', 300)], []
    finally:
        if lock is not None:
            os.close(lock)
    for line in lines + notes:
        print(one_line(line, 400))
    return 1 if lines else 0


def alert(m, read_settings, cache, now=None, max_age=datetime.timedelta(hours=6)):
    """`update-policy exposure-alert` (as root, after the unprivileged check): sends the report's lines (and notes)
    through the alert command. A report that is missing, not a plain file, broken or old is itself one line."""
    from updatepolicy import tick
    m.limit_alerts(ALERT_BUDGET)
    now = now or datetime.datetime.now(UTC)
    try:
        s = read_settings()
    except settings_mod.MachineFileError:
        return 1
    if not s['exposure']['enabled']:
        return 0
    path = os.path.join(cache, REPORT)
    notes = []
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as f:
            report = json.loads(f.read(1 << 20).decode('utf-8'))
        at = feeds._parse_time(report.get('at'))
        lines, notes = report.get('lines'), report.get('notes') or []
        if not at or not isinstance(lines, list) or not isinstance(notes, list) \
                or not now - max_age <= at <= now + datetime.timedelta(minutes=5):
            raise ValueError('old or broken')
        lines = [one_line(x, 400) for x in lines if isinstance(x, str)][:MAX_LINES + 5]
        notes = [one_line(x, 400) for x in notes if isinstance(x, str)][:5]
    except (OSError, ValueError, AttributeError) as e:
        lines, notes = [f'cannot check: the exposure report {path} is missing or broken ({one_line(e, 120)})'], []
    if not lines:
        return 0
    if not s['alertCommand']:
        return 1                                    # the check's own output is in the journal already
    err = tick.send_alert(m, s, 'exposure', '\n'.join(lines + notes))
    return 1 if err else 0
