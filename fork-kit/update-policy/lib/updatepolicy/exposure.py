"""The daily exposure check (#1045 part 3): installed packages against the distribution's security data, CISA's Known
Exploited Vulnerabilities (KEV) list and EPSS. It prints only what needs action, one line each, and exits 1 when
there is any line; 0 when there is none.

- KEV, fix available: a CVE on CISA's list that affects an installed version, with a fixed version out: update.
- KEV, no fix yet: the same with no fixed version: mitigate or watch.
- security fix waiting: an installed package with a security fix out, shown when one of its CVEs is on KEV, its
  EPSS is at least exposure.epssMin, or the fix has been waiting for more than exposure.fixWaitingDays (as this
  check first saw it); ranked KEV first, then by EPSS.
- cannot check: a feed that could not be downloaded or read, a distribution it has no data source for, installed
  packages it cannot list, EPSS it cannot ask. Never read as "all clear".
- drift: a cached feed older than exposure.staleDays; an ignore entry past its date.

Data: the distribution's OVAL file (oval.py), KEV's JSON feed (cisa.gov), EPSS from FIRST.org's API for the CVEs
found only. Nothing about the machine is sent anywhere but those CVE ids to FIRST.org. The ecosystems table is where
another package system (rpm, apk) gets its installed list, version order and data source.
"""
import datetime, json, os, re, tempfile, urllib.parse

from updatepolicy import debver, feeds, oval, settings as settings_mod

UTC = datetime.timezone.utc
one_line = settings_mod.one_line
CVE_RE = re.compile(r'^CVE-[0-9]{4}-[0-9]{4,}\Z')
KEV_URL = 'https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json'
EPSS_URL = 'https://api.first.org/data/v1/epss'
EPSS_BATCH = 100
CACHE = '/var/cache/update-policy-exposure'
REPORT = 'report.json'


class CannotCheck(Exception):
    pass


# -- ecosystems: installed packages, version order, data source -------------------------------------------------------

class Dpkg:
    """Debian and Ubuntu: dpkg's database, dpkg's version order, the distribution's OVAL file."""
    compare = staticmethod(debver.compare)

    def __init__(self, oval_url):
        self.oval_url = oval_url

    def installed(self, host):
        """{name: [(binary, version)]}: every installed binary under its own name and under its source package's
        name (Debian's OVAL names source packages, Ubuntu's binaries), with the version that name is versioned by."""
        code, out, err = host.run(['dpkg-query', '-W', '-f',
                                   '${db:Status-Abbrev}\\t${binary:Package}\\t${Version}\\t${source:Package}\\t'
                                   '${source:Version}\\n'], timeout=60)
        if code != 0:
            raise CannotCheck(f'the installed packages cannot be listed (dpkg-query: '
                              f'{one_line(err.strip() or f"exit {code}", 160)})')
        found = {}
        for line in out.splitlines():
            parts = line.split('\t')
            if len(parts) != 5 or not parts[0].startswith(('ii', 'hi')):
                continue
            binary, version, source, source_version = parts[1].split(':')[0], parts[2], parts[3], parts[4]
            found.setdefault(binary, []).append((binary, version))
            if source and source != binary:
                found.setdefault(source, []).append((binary, source_version or version))
        if not found:
            raise CannotCheck('dpkg-query listed no installed package')
        return found

    def advisories(self, path):
        return oval.read(path)


ECOSYSTEMS = {
    'ubuntu': lambda codename: Dpkg(f'https://security-metadata.canonical.com/oval/com.ubuntu.{codename}.cve.oval.xml.bz2'),
    'debian': lambda codename: Dpkg(f'https://www.debian.org/security/oval/oval-definitions-{codename}.xml.bz2'),
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

def read_kev(path):
    """{CVE: {dueDate, ransomware, product}} of CISA's KEV JSON. Raises ValueError for a format it does not know."""
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    rows = data.get('vulnerabilities') if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError('no "vulnerabilities" list')
    out = {}
    for r in rows:
        cve = r.get('cveID') if isinstance(r, dict) else None
        if not isinstance(cve, str) or not CVE_RE.match(cve):
            raise ValueError(f'an entry without a CVE id ({one_line(repr(r), 80)})')
        out[cve] = {'dueDate': str(r.get('dueDate') or ''), 'ransomware': r.get('knownRansomwareCampaignUse') == 'Known',
                    'product': f'{r.get("vendorProject", "")} {r.get("product", "")}'.strip()}
    return out


def epss_scores(cache, cves, now, timeout, opener):
    """{CVE: score} from FIRST.org's API (100 per request), kept a day in epss.json. Returns (scores, problem)."""
    path = os.path.join(cache, 'epss.json')
    try:
        with open(path, encoding='utf-8') as f:
            kept = json.load(f)
        if not isinstance(kept, dict):
            kept = {}
    except (OSError, ValueError):
        kept = {}
    fresh = {c: v for c, v in kept.items() if isinstance(v, list) and len(v) == 2 and
             (feeds._parse_time(v[1]) or now - feeds.DAY * 2) > now - feeds.DAY}
    want = sorted(set(cves) - set(fresh))
    problem = None
    for i in range(0, len(want), EPSS_BATCH):
        batch = want[i:i + EPSS_BATCH]
        url = f'{EPSS_URL}?{urllib.parse.urlencode({"cve": ",".join(batch), "limit": EPSS_BATCH})}'
        try:
            status, _h, body = opener(url, {}, timeout)
            try:
                data = json.loads(body.read(1 << 20).decode('utf-8')) if status == 200 and body else None
            finally:
                if body is not None and hasattr(body, 'close'):
                    body.close()
            if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                raise ValueError(f'HTTP {status}, not the API\'s answer')
            got = {}
            for row in data['data']:
                cve, score = row.get('cve'), float(row.get('epss'))
                if isinstance(cve, str) and CVE_RE.match(cve) and 0 <= score <= 1:
                    got[cve] = [score, now.isoformat()]
            for cve in batch:
                fresh[cve] = got.get(cve, [0.0, now.isoformat()])     # not scored (yet): 0
        except Exception as e:      # noqa: BLE001 - a ranking aid: its failure is one line, never a crash
            problem = f'EPSS: FIRST.org could not be asked ({one_line(e, 120)}); ranked without it'
            break
    try:
        fd, tmp = tempfile.mkstemp(dir=cache, prefix='.epss.', suffix='.tmp')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(fresh, f)
        os.replace(tmp, path)
    except OSError:
        pass
    return {c: v[0] for c, v in fresh.items()}, problem


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


def check(host, s, cache, now, opener=feeds.urlopen):
    """(lines, report dict). Lines: what needs action, cannot-check and drift lines; [] means nothing to do."""
    ex = s['exposure']
    lines, findings = [], []
    if not ex['enabled']:
        return [], {'v': 1, 'at': now.isoformat(), 'enabled': False, 'lines': []}
    today = now.date()
    ignored, drift = _ignores(ex, today)
    try:
        distro, codename = os_release(host)
        make = ECOSYSTEMS.get(distro)
        if make is None or not re.match(r'^[a-z0-9-]{1,40}$', codename or ''):
            raise CannotCheck(f'no security data source for this distribution ({one_line(distro or "unknown", 40)} '
                              f'{one_line(codename, 40)}); the check knows {", ".join(sorted(ECOSYSTEMS))}')
        eco = make(codename)
        installed = eco.installed(host)
    except CannotCheck as e:
        lines = [f'cannot check: {e}'] + drift
        return lines, {'v': 1, 'at': now.isoformat(), 'enabled': True, 'lines': lines}
    cap, timeout = ex['maxFeedMb'] << 20, ex['timeoutSeconds']
    advisories, kev, problems, parsed = None, None, [], {}

    def read_oval(path):                            # a new download is read once: to check it, and to use it
        parsed[path] = eco.advisories(path)
    got = feeds.fetch(cache, 'oval', ex['ovalUrl'] or eco.oval_url, read_oval, now, cap, timeout, opener)
    if got.problem:
        problems.append(got.problem)
    if got.path:
        try:
            advisories = next(iter(parsed.values())) if parsed and not got.problem else eco.advisories(got.path)
        except (oval.FormatError, OSError) as e:
            problems.append(f'oval: the cached copy cannot be read ({e})')
    got_kev = feeds.fetch(cache, 'kev', ex['kevUrl'] or KEV_URL, read_kev, now, cap, timeout, opener)
    stale = [('oval', got), ('kev', got_kev)]
    if got_kev.problem:
        problems.append(got_kev.problem)
    if got_kev.path:
        try:
            kev = read_kev(got_kev.path)
        except (OSError, ValueError) as e:
            problems.append(f'kev: the cached copy cannot be read ({e})')
    for name, f in stale:
        if f.path and f.fetched_at and now - f.fetched_at > datetime.timedelta(days=ex['staleDays']):
            drift.append(f'drift: the cached {name} feed is {(now - f.fetched_at).days} days old (more than '
                         f'{ex["staleDays"]}): the check works on old data')
    lines += [f'cannot check: {p}' for p in problems]
    if advisories is None:
        lines.append('cannot check: no usable copy of the distribution\'s security data')
    if kev is None:
        lines.append('cannot check: no usable copy of CISA\'s KEV list')
    affected = []                                   # (cve, source, binary, version, fixed, note, priority)
    for adv in advisories or []:
        for name in adv.binaries:
            for binary, version in installed.get(name, ()):
                try:
                    if adv.fixed is None or eco.compare(version, adv.fixed) < 0:
                        affected.append((adv.cve, adv.source or name, binary, version, adv.fixed, adv.note,
                                         adv.priority))
                except ValueError:
                    continue
    affected = [a for a in affected if a[0] not in ignored]
    kev = kev or {}
    state = _read_state(cache)
    seen = {}
    waiting = {}                                    # binary -> {version, fixed, cves, note}
    for cve, source, binary, version, fixed, note, priority in affected:
        if fixed is None:
            continue
        w = waiting.setdefault(binary, {'source': source, 'version': version, 'fixed': fixed, 'cves': set(),
                                        'note': ''})
        w['cves'].add(cve)
        if eco.compare(fixed, w['fixed']) > 0:
            w['fixed'] = fixed
        w['note'] = w['note'] or note
    for binary, w in waiting.items():
        key = f'{binary} {w["fixed"]}'
        first = feeds._parse_time(state['fixSeen'].get(key)) or now
        seen[key] = first.isoformat()
        w['days'] = max(0, (now - first).days)
    kev_unfixed = sorted({(cve, source) for cve, source, _b, _v, fixed, _n, _p in affected
                          if fixed is None and cve in kev})
    epss_for = sorted({c for w in waiting.values() for c in w['cves']} | {c for c, _s in kev_unfixed})
    scores, problem = ({}, None) if not epss_for else epss_scores(cache, epss_for, now, timeout, opener)
    if problem:
        lines.append(f'cannot check: {problem}')
    rows = []
    for binary, w in waiting.items():
        kevs = sorted(c for c in w['cves'] if c in kev)
        top = max((scores.get(c, 0.0) for c in w['cves']), default=0.0)
        if not (kevs or top >= ex['epssMin'] or w['days'] > ex['fixWaitingDays']):
            continue
        pro = ' (the fix needs Ubuntu Pro: esm)' if w['note'] == 'esm' else ''
        cves = ', '.join(sorted(w['cves'], key=lambda c: -scores.get(c, 0.0))[:5])
        more = f' and {len(w["cves"]) - 5} more' if len(w['cves']) > 5 else ''
        if kevs:
            text = (f'KEV {", ".join(kevs[:3])}: {binary} {w["version"]} is exploited in the wild; update to '
                    f'{w["fixed"]}{pro} (EPSS {top:.2f}; CISA due {kev[kevs[0]]["dueDate"] or "?"})')
        else:
            text = (f'security fix waiting {w["days"]} day(s): {binary} {w["version"]} -> {w["fixed"]}{pro}: '
                    f'{cves}{more} (EPSS {top:.2f})')
        rows.append((0 if kevs else 1, -top, text, {'binary': binary, 'version': w['version'], 'fixed': w['fixed'],
                                                   'cves': sorted(w['cves']), 'kev': kevs, 'epss': top,
                                                   'days': w['days'], 'action': 'update'}))
    for cve, source in kev_unfixed:
        top = scores.get(cve, 0.0)
        rows.append((0, -top, f'KEV {cve}: {source} is exploited in the wild and has no fix yet: mitigate or watch '
                              f'(EPSS {top:.2f}; CISA due {kev[cve]["dueDate"] or "?"}; {kev[cve]["product"]})',
                     {'cve': cve, 'source': source, 'kev': [cve], 'epss': top, 'action': 'mitigate'}))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    lines += [one_line(r[2], 400) for r in rows] + drift
    findings = [r[3] for r in rows]
    state['fixSeen'] = seen
    try:
        fd, tmp = tempfile.mkstemp(dir=cache, prefix='.state.', suffix='.tmp')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, sort_keys=True)
        os.replace(tmp, os.path.join(cache, 'state.json'))
    except OSError as e:
        lines.append(f'cannot check: the cache folder {cache} cannot be written ({e.strerror})')
    return lines, {'v': 1, 'at': now.isoformat(), 'enabled': True, 'lines': lines, 'findings': findings}


def write_report(cache, report):
    fd, tmp = tempfile.mkstemp(dir=cache, prefix='.report.', suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(report, f, sort_keys=True)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(cache, REPORT))


def run(host, read_settings, cache, now=None, opener=feeds.urlopen):
    """`update-policy exposure`: prints the lines, writes the report for the alert step; 0 none, 1 any."""
    now = now or datetime.datetime.now(UTC)
    try:
        s = read_settings()
    except settings_mod.MachineFileError as e:
        print(f'cannot check: the machine file cannot be used: {e}')
        return 1
    if not s['exposure']['enabled']:
        print('update-policy exposure: off (exposure.enabled is false in the machine file)')
        return 0
    try:
        os.makedirs(cache, mode=0o750, exist_ok=True)
        lines, report = check(host, s, cache, now, opener)
    except OSError as e:
        lines = [f'cannot check: the cache folder {cache} cannot be used ({e.strerror or e})']
        report = {'v': 1, 'at': now.isoformat(), 'enabled': True, 'lines': lines}
    for line in lines:
        print(line)
    try:
        write_report(cache, report)
    except OSError:
        pass
    return 1 if lines else 0


def alert(m, read_settings, cache, now=None, max_age=datetime.timedelta(hours=6)):
    """`update-policy exposure-alert` (as root, after the unprivileged check): sends the report's lines through the
    alert command. A report that is missing, not a plain file, broken or old is itself one line."""
    from updatepolicy import tick
    now = now or datetime.datetime.now(UTC)
    try:
        s = read_settings()
    except settings_mod.MachineFileError:
        return 1
    if not s['exposure']['enabled']:
        return 0
    path = os.path.join(cache, REPORT)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as f:
            report = json.loads(f.read(1 << 20).decode('utf-8'))
        at = feeds._parse_time(report.get('at'))
        lines = report.get('lines')
        if not at or not isinstance(lines, list) or not now - max_age <= at <= now + datetime.timedelta(minutes=5):
            raise ValueError('old or broken')
        lines = [one_line(x, 400) for x in lines if isinstance(x, str)]
    except (OSError, ValueError, AttributeError) as e:
        lines = [f'cannot check: the exposure report {path} is missing or broken ({one_line(e, 120)})']
    if not lines:
        return 0
    if not s['alertCommand']:
        return 1                                    # the check's own output is in the journal already
    err = tick.send_alert(m, s, 'exposure', '\n'.join(lines))
    return 1 if err else 0
