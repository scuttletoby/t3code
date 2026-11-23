"""update-policy exposure: Debian version order, the OVAL reader, the feed cache and the daily check, against small
recorded fixtures (tests/fixtures: cuts of Ubuntu's noble CVE OVAL, Debian's bookworm OVAL and CISA's KEV list), a
fake web (no network), a fake dpkg-query and a fixed clock. RedCheck breaks each rule in a copy of the code and shows
its test then fails.

  python3 -I -B fork-kit/update-policy/tests/exposure.test.py
"""
import bz2, datetime, io, json, os, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixture as fx  # noqa: E402
from fixture import at  # noqa: E402
from updatepolicy import debver, exposure, feeds, oval  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
UBUNTU_OVAL = 'https://security-metadata.canonical.com/oval/com.ubuntu.noble.cve.oval.xml.bz2'
DEBIAN_OVAL = 'https://www.debian.org/security/oval/oval-definitions-bookworm.xml.bz2'
NOW = at('2026-11-24 07:00')


def fixture_bytes(name, compress=False):
    with open(os.path.join(FIXTURES, name), 'rb') as f:
        data = f.read()
    return bz2.compress(data) if compress else data


def dpkg_line(binary, version, source='', source_version=''):
    return f'ii \t{binary}\t{version}\t{source or binary}\t{source_version or version}\n'


class Web:
    """A fake web: url (without its query) -> (status, headers, bytes) or an exception; calls are recorded."""

    def __init__(self):
        self.pages, self.calls, self.epss = {}, [], {}

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        if url.startswith(exposure.EPSS_URL):
            if isinstance(self.epss, Exception):
                raise self.epss
            asked = url.split('cve=')[1].split('&')[0].replace('%2C', ',').split(',')
            rows = [{'cve': c, 'epss': f'{self.epss[c]:.9f}', 'percentile': '0.5', 'date': '2026-11-23'}
                    for c in asked if c in self.epss]
            return 200, {}, io.BytesIO(json.dumps({'status': 'OK', 'data': rows}).encode())
        page = self.pages.get(url)
        if page is None:
            raise OSError('Name or service not known')
        if isinstance(page, Exception):
            raise page
        status, h, body = page
        if headers.get('If-None-Match') and headers['If-None-Match'] == h.get('etag'):
            return 304, h, None
        return status, h, io.BytesIO(body)

    def asked(self, prefix):
        return [c for c in self.calls if c[0].startswith(prefix)]


class Versions(unittest.TestCase):
    TABLE = [('1.0', '1.0', 0), ('1.0', '1.1', -1), ('1.0~rc1', '1.0', -1), ('1.0', '1.0+b1', -1),
             ('1:0.9', '2.0', 1), ('2.38-1ubuntu5', '2.38-1ubuntu6', -1), ('2.38-1ubuntu6', '2.38-1ubuntu6.1', -1),
             ('0.18.6-2ubuntu0.1', '0.18.6-2', 1), ('1.2.4-0.2+deb12u1', '1.2.4-0.2', 1), ('1.0a', '1.0', 1),
             ('1.0-1', '1.0', 1), ('5.6.1+really5.4.5-1', '5.6.0-0.1', 1), ('1.0~~', '1.0~', -1),
             ('0:117.0.5938.62-1', '117.0.5938.62-1', 0), ('10', '9', 1), ('1.001', '1.1', 0)]

    def test_dpkg_order(self):
        for a, b, want in self.TABLE:
            with self.subTest(a=a, b=b):
                got = debver.compare(a, b)
                self.assertEqual((got > 0) - (got < 0), want)

    def test_the_same_as_dpkg_where_dpkg_is_there(self):
        if not shutil.which('dpkg'):
            self.skipTest('no dpkg here')
        for a, b, want in self.TABLE:
            r = subprocess.run(['dpkg', '--compare-versions', a, 'lt', b])
            self.assertEqual(r.returncode == 0, want < 0, (a, b))

    def test_not_a_version(self):
        with self.assertRaises(ValueError):
            debver.compare('1.0 beta', '1.0')


class Oval(unittest.TestCase):
    def test_ubuntu_binaries_fixed_and_unfixed(self):
        found = {(a.cve, a.source): a for a in oval.read(os.path.join(FIXTURES, 'ubuntu-noble.cve.oval.xml'))}
        glibc = found[('CVE-2023-4911', 'glibc')]
        self.assertEqual((glibc.fixed, glibc.priority), ('2.38-1ubuntu6', 'high'))
        self.assertIn('libc-bin', glibc.binaries)
        qt = found[('CVE-2021-1789', 'qtwebkit-opensource-src')]
        self.assertEqual((qt.fixed, qt.binaries), (None, ('libqt5webkit5', 'qml-module-qtwebkit')))

    def test_only_a_less_than_state_is_a_fix(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, 'oval.xml')
        ns = 'xmlns="http://oval.mitre.org/XMLSchema/oval-definitions-5" xmlns:l="x#linux"'
        with open(path, 'w') as f:
            f.write(f'<oval_definitions {ns}><definitions>'
                    '<definition id="d1"><metadata><title>CVE-2024-0001 on X</title></metadata><criteria>'
                    '<criterion test_ref="t1" comment="a package in x"/><criterion test_ref="t2" comment="b package"/>'
                    '</criteria></definition></definitions><tests>'
                    '<l:dpkginfo_test id="t1"><l:object object_ref="o1"/><l:state state_ref="s1"/></l:dpkginfo_test>'
                    '<l:dpkginfo_test id="t2"><l:object object_ref="o2"/><l:state state_ref="s2"/></l:dpkginfo_test>'
                    '</tests><objects><l:dpkginfo_object id="o1"><l:name>a</l:name></l:dpkginfo_object>'
                    '<l:dpkginfo_object id="o2"><l:name>b</l:name></l:dpkginfo_object></objects><states>'
                    '<l:dpkginfo_state id="s1"><l:evr operation="less than">1.2-1</l:evr></l:dpkginfo_state>'
                    '<l:dpkginfo_state id="s2"><l:evr operation="greater than">1.0</l:evr></l:dpkginfo_state>'
                    '</states></oval_definitions>')
        self.assertEqual([(a.cve, a.binaries, a.fixed) for a in oval.read(path)], [('CVE-2024-0001', ('a',), '1.2-1')])

    def test_debian_names_source_packages_and_bz2_is_read_too(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, 'oval.bz2')
        with open(path, 'wb') as f:
            f.write(fixture_bytes('debian-bookworm.oval.xml', compress=True))
        webp = [a for a in oval.read(path) if a.source == 'libwebp']
        self.assertEqual([(a.cve, a.binaries, a.fixed) for a in webp],
                         [('CVE-2023-4863', ('libwebp',), '0:1.2.4-0.2+deb12u1')])

    def test_a_format_it_does_not_know_is_an_error_never_no_advisories(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for name, text in (('html', b'<html><body>moved</body></html>'), ('cut', b'<oval_definitions><definitions>'),
                           ('empty', b''), ('no-tests', b'<oval_definitions><definitions><definition id="x"><metadata>'
                                                     b'<title>CVE-2024-0001</title></metadata></definition>'
                                                     b'</definitions></oval_definitions>')):
            with self.subTest(name=name):
                path = os.path.join(tmp, name)
                with open(path, 'wb') as f:
                    f.write(text)
                with self.assertRaises(oval.FormatError):
                    oval.read(path)


class Feeds(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix='update-policy-cache-')
        self.addCleanup(shutil.rmtree, self.cache)
        self.web = Web()
        self.url = 'https://feeds.example/kev.json'
        self.web.pages[self.url] = (200, {'etag': '"v1"', 'content-length': '3532'}, fixture_bytes('kev.json'))

    def fetch(self, now=NOW, cap=1 << 20, check=exposure.read_kev):
        return feeds.fetch(self.cache, 'kev', self.url, check, now, cap, 10, self.web)

    def test_once_a_day_then_etag(self):
        got = self.fetch()
        self.assertEqual((got.problem, got.fetched_at), (None, NOW))
        self.assertEqual(len(exposure.read_kev(got.path)), 4)
        self.fetch(NOW + datetime.timedelta(hours=5))
        self.assertEqual(len(self.web.calls), 1)                      # once a day
        got = self.fetch(NOW + datetime.timedelta(days=1))
        self.assertEqual(self.web.calls[-1][1], {'If-None-Match': '"v1"'})
        self.assertEqual((got.problem, got.fetched_at), (None, NOW + datetime.timedelta(days=1)))   # 304: fresh

    def test_offline_keeps_the_copy_and_says_so(self):
        self.fetch()
        self.web.pages[self.url] = OSError('Network is unreachable')
        got = self.fetch(NOW + datetime.timedelta(days=2))
        self.assertEqual((got.path is not None, got.fetched_at), (True, NOW))
        self.assertEqual(got.problem, 'kev: download failed (Network is unreachable)')

    def test_a_download_it_does_not_understand_never_replaces_a_good_copy(self):
        self.fetch()
        self.web.pages[self.url] = (200, {'etag': '"v2"'}, b'{"title": "a new format", "items": []}')
        got = self.fetch(NOW + datetime.timedelta(days=1))
        self.assertIn('kev: the download is in a format this check does not understand', got.problem)
        self.assertEqual(len(exposure.read_kev(got.path)), 4)          # the old copy stays

    def test_the_size_cap(self):
        got = self.fetch(cap=1000)
        self.assertEqual((got.path, got.problem), (None, 'kev: larger than the cap of 0 MB'))
        self.web.pages[self.url] = (200, {}, fixture_bytes('kev.json'))   # no content-length: cut while reading
        self.assertEqual(self.fetch(cap=1000).problem, 'kev: larger than the cap of 0 MB')
        self.assertEqual([n for n in os.listdir(self.cache) if n.endswith('.tmp')], [])

    def test_a_copy_that_does_not_match_its_checksum_is_no_copy(self):
        self.fetch()
        with open(os.path.join(self.cache, 'kev.data'), 'ab') as f:
            f.write(b' ')
        self.web.pages[self.url] = OSError('offline')
        got = self.fetch(NOW + datetime.timedelta(days=1))
        self.assertEqual((got.path, got.fetched_at), (None, None))

    def test_another_url_starts_again(self):
        self.fetch()
        self.url = 'https://feeds.example/other.json'
        self.web.pages[self.url] = OSError('offline')
        self.assertIsNone(self.fetch().path)


class Base(fx.HostCase):
    def setUp(self):
        super().setUp()
        self.cache = os.path.join(self.root, 'cache')
        os.makedirs(self.cache)
        self.write('/etc/os-release', 'NAME="Ubuntu"\nID=ubuntu\nVERSION_CODENAME=noble\n')
        self.packages = [dpkg_line('libc-bin', '2.38-1ubuntu5', 'glibc'),
                         dpkg_line('libqt5webkit5', '5.212.0~alpha4-36ubuntu2', 'qtwebkit-opensource-src'),
                         dpkg_line('aide', '0.18.6-2', 'aide'), dpkg_line('bash', '5.2.21-2ubuntu4'),
                         dpkg_line('gcc-h8300-hms', '1:3.4.6+dfsg2-4.1')]     # an unfixed CVE, not on KEV: no line
        self.host.answers['dpkg-query'] = lambda argv: (0, ''.join(self.packages), '')
        self.web = Web()
        self.web.pages[UBUNTU_OVAL] = (200, {'etag': '"o1"'}, fixture_bytes('ubuntu-noble.cve.oval.xml', True))
        self.web.pages[exposure.KEV_URL] = (200, {'etag': '"k1"'}, fixture_bytes('kev.json'))
        self.web.epss = {'CVE-2023-4911': 0.88, 'CVE-2021-1789': 0.31, 'CVE-2025-54389': 0.02}

    def check(self, now=NOW, **over):
        s = fx.make_settings(exposure=dict({'enabled': True}, **over))
        return exposure.check(self.host, s, self.cache, now, self.web)


class Check(Base):
    def test_kev_with_a_fix_kev_without_and_nothing_else(self):
        lines, report = self.check()
        self.assertEqual(lines, [
            'KEV CVE-2023-4911: libc-bin 2.38-1ubuntu5 is exploited in the wild; update to 2.38-1ubuntu6 (EPSS 0.88; '
            'CISA due 2023-12-12)',
            'KEV CVE-2021-1789: qtwebkit-opensource-src is exploited in the wild and has no fix yet: mitigate or watch '
            '(EPSS 0.31; CISA due 2022-05-25; Apple Multiple Products)'])
        self.assertEqual([f['action'] for f in report['findings']], ['update', 'mitigate'])
        self.assertEqual(sorted(self.web.asked(exposure.EPSS_URL)[0][0].split('cve=')[1].split('&')[0]
                                .replace('%2C', ',').split(',')),
                         ['CVE-2021-1789', 'CVE-2023-4911', 'CVE-2025-54389'])   # only the CVEs found

    def test_a_fix_waiting_shows_by_epss_or_after_its_days(self):
        self.packages = [dpkg_line('aide', '0.18.6-2', 'aide')]
        self.assertEqual(self.check()[0], [])                          # EPSS 0.02, first seen today
        lines, _ = self.check(epssMin=0.01)
        self.assertEqual(lines, ['security fix waiting 0 day(s): aide 0.18.6-2 -> 0.18.6-2ubuntu0.1: CVE-2025-54389 '
                                 '(EPSS 0.02)'])
        later = NOW + datetime.timedelta(days=4)
        self.web.pages[UBUNTU_OVAL] = (200, {'etag': '"o1"'}, fixture_bytes('ubuntu-noble.cve.oval.xml', True))
        self.assertEqual(self.check(later)[0], ['security fix waiting 4 day(s): aide 0.18.6-2 -> 0.18.6-2ubuntu0.1: '
                                                'CVE-2025-54389 (EPSS 0.02)'])
        self.packages = [dpkg_line('aide', '0.18.6-2ubuntu0.1', 'aide')]          # updated: gone
        self.assertEqual(self.check(later)[0], [])

    def test_ranked_kev_first_then_by_epss(self):
        self.packages.append(dpkg_line('aide', '0.18.6-2', 'aide'))
        self.web.epss['CVE-2025-54389'] = 0.95
        lines, _ = self.check()
        self.assertEqual([x.split(':')[0] for x in lines],
                         ['KEV CVE-2023-4911', 'KEV CVE-2021-1789', 'security fix waiting 0 day(s)'])

    def test_debian_matches_source_names(self):
        self.write('/etc/os-release', 'ID=debian\nVERSION_CODENAME=bookworm\n')
        self.packages = [dpkg_line('libwebp7', '1.2.4-0.2', 'libwebp', '1.2.4-0.2'),
                         dpkg_line('exim4-base', '4.96-15+deb12u2', 'exim4')]
        self.web.pages[DEBIAN_OVAL] = (200, {}, fixture_bytes('debian-bookworm.oval.xml', True))
        lines, _ = self.check()
        self.assertEqual(lines, ['KEV CVE-2023-4863: libwebp7 1.2.4-0.2 is exploited in the wild; update to '
                                 '0:1.2.4-0.2+deb12u1 (EPSS 0.00; CISA due 2023-10-04)'])

    def test_offline_is_cannot_check_never_all_clear(self):
        self.web.pages.clear()
        lines, _ = self.check()
        self.assertEqual(lines, ['cannot check: oval: download failed (Name or service not known)',
                                 'cannot check: kev: download failed (Name or service not known)',
                                 'cannot check: no usable copy of the distribution\'s security data',
                                 'cannot check: no usable copy of CISA\'s KEV list'])

    def test_a_stale_cache_is_drift_and_still_used(self):
        self.check()
        self.web.pages.clear()
        lines, _ = self.check(NOW + datetime.timedelta(days=5))
        self.assertEqual(lines[:2], ['cannot check: oval: download failed (Name or service not known)',
                                     'cannot check: kev: download failed (Name or service not known)'])
        self.assertIn('KEV CVE-2023-4911', lines[2])                     # the old copy is still read
        self.assertEqual(lines[-2:], ['drift: the cached oval feed is 5 days old (more than 3): the check works on '
                                      'old data', 'drift: the cached kev feed is 5 days old (more than 3): the check '
                                                  'works on old data'])

    def test_a_feed_format_change_is_cannot_check(self):
        self.web.pages[exposure.KEV_URL] = (200, {}, b'{"catalogVersion": "2", "entries": []}')
        lines, _ = self.check()
        self.assertEqual(lines[:2], ['cannot check: kev: the download is in a format this check does not understand '
                                     '(no "vulnerabilities" list)', 'cannot check: no usable copy of CISA\'s KEV list'])

    def test_epss_down_is_one_line_and_kev_still_shows(self):
        self.web.epss = OSError('timed out')
        lines, _ = self.check()
        self.assertEqual(lines[0], 'cannot check: EPSS: FIRST.org could not be asked (timed out); ranked without it')
        self.assertTrue(any(x.startswith('KEV CVE-2023-4911') for x in lines[1:]), lines)

    def test_ignore_until_a_date_then_drift(self):
        ignore = [{'cve': 'CVE-2021-1789', 'reason': 'qtwebkit unused, removal planned', 'until': '2026-12-01'}]
        lines, _ = self.check(ignore=ignore)
        self.assertEqual([x.split(':')[0] for x in lines], ['KEV CVE-2023-4911'])
        lines, _ = self.check(NOW + datetime.timedelta(days=10), ignore=ignore)
        self.assertIn('KEV CVE-2021-1789', lines[1])
        self.assertEqual(lines[-1], 'drift: the ignore entry for CVE-2021-1789 ended on 2026-12-01 (qtwebkit unused, '
                                    'removal planned): remove it or give it a new date')

    def test_a_distribution_without_a_source_and_dpkg_failing_cannot_check(self):
        self.write('/etc/os-release', 'ID=fedora\nVERSION_CODENAME=\n')
        self.assertEqual(self.check()[0], ['cannot check: no security data source for this distribution (fedora ); '
                                           'the check knows debian, ubuntu'])
        self.write('/etc/os-release', 'ID=ubuntu\nVERSION_CODENAME=noble\n')
        self.host.answers['dpkg-query'] = (2, '', 'dpkg-query: error: cannot access archive')
        self.assertEqual(self.check()[0], ['cannot check: the installed packages cannot be listed (dpkg-query: '
                                           'dpkg-query: error: cannot access archive)'])

    def test_old_or_broken_cache_files_start_again(self):
        for name, text in (('state.json', '[1, 2'), ('epss.json', '"x"'), ('kev.meta.json', '{"url": 1}'),
                           ('oval.data', 'not oval')):
            with open(os.path.join(self.cache, name), 'w') as f:
                f.write(text)
        lines, _ = self.check()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(all(x.startswith('KEV ') for x in lines))

    def test_off_does_nothing(self):
        s = fx.make_settings()
        self.assertEqual(exposure.check(self.host, s, self.cache, NOW, self.web)[0], [])
        self.assertEqual(self.web.calls, [])


class Commands(Base):
    def run_cmd(self, now=NOW):
        s = fx.make_settings(alertCommand=['/usr/local/bin/notify'], exposure={'enabled': True})
        with mock.patch('builtins.print') as out:
            code = exposure.run(self.host, lambda: s, self.cache, now, self.web)
        return code, [c.args[0] for c in out.call_args_list], s

    def test_run_prints_writes_the_report_and_the_alert_step_sends_it(self):
        code, printed, s = self.run_cmd()
        self.assertEqual((code, len(printed)), (1, 2))
        m = fx.FakeMachine(self.host, NOW)
        self.assertEqual(exposure.alert(m, lambda: s, self.cache, NOW + datetime.timedelta(minutes=1)), 0)
        self.assertEqual(m.alerts, [('exposure', '\n'.join(printed))])

    def test_the_alert_step_reports_a_missing_old_or_linked_report(self):
        s = fx.make_settings(alertCommand=['/usr/local/bin/notify'], exposure={'enabled': True})
        m = fx.FakeMachine(self.host, NOW)
        exposure.alert(m, lambda: s, self.cache, NOW)
        self.assertIn('cannot check: the exposure report', m.alerts[-1][1])
        self.run_cmd()
        exposure.alert(m, lambda: s, self.cache, NOW + datetime.timedelta(days=1))
        self.assertIn('missing or broken (old or broken)', m.alerts[-1][1])
        report = os.path.join(self.cache, exposure.REPORT)
        os.rename(report, report + '.real')
        os.symlink(report + '.real', report)
        exposure.alert(m, lambda: s, self.cache, NOW)
        self.assertIn('missing or broken', m.alerts[-1][1])

    def test_nothing_found_is_exit_0_and_no_alert(self):
        self.packages = [dpkg_line('bash', '5.2.21-2ubuntu4')]
        code, printed, s = self.run_cmd()
        self.assertEqual((code, printed), (0, []))
        m = fx.FakeMachine(self.host, NOW)
        self.assertEqual(exposure.alert(m, lambda: s, self.cache, NOW), 0)
        self.assertEqual(m.alerts, [])


class ExposureSettings(unittest.TestCase):
    def test_defaults_off_and_bad_values_refused(self):
        s = fx.make_settings()['exposure']
        self.assertEqual((s['enabled'], s['epssMin'], s['fixWaitingDays'], s['staleDays'], s['maxFeedMb']),
                         (False, 0.1, 3, 3, 64))
        for bad, words in (({'ignore': [{'cve': 'CVE-2024-1', 'reason': 'x', 'until': '2026-12-01'}]}, 'not a CVE'),
                           ({'ignore': [{'cve': 'CVE-2024-0001', 'reason': ' ', 'until': '2026-12-01'}]}, 'reason'),
                           ({'ignore': [{'cve': 'CVE-2024-0001', 'reason': 'x', 'until': 'soon'}]}, 'YYYY-MM-DD'),
                           ({'ignore': [{'cve': 'CVE-2024-0001'}]}, 'each entry'),
                           ({'epssMin': 2}, 'from 0 to 1'), ({'ovalUrl': 'http://x'}, 'https://'),
                           ({'enabled': 'yes'}, 'bool'), ({'feeds': []}, 'unknown key')):
            with self.subTest(bad=bad), self.assertRaises(fx.settings.MachineFileError) as e:
                fx.make_settings(exposure=bad)
            self.assertIn(words, str(e.exception))


MUTATIONS = {
    'dpkg order: ~ sorts first': (
        'debver', "    if c == '~':\n        return -1", "    if c == '~':\n        return 1", 'Versions.test_dpkg_order'),
    'oval: no tests is no format': (
        'oval', "    if not defs or not tests:", "    if not defs:",
        'Oval.test_a_format_it_does_not_know_is_an_error_never_no_advisories'),
    'oval: only "less than" states are fixes': (
        'oval', "                if op != 'less than' or not version:", "                if not version:",
        'Oval.test_only_a_less_than_state_is_a_fix'),
    'feeds: once a day': (
        'feeds', "        return Fetched(copy, None, fetched_at)          # once a day", "        pass",
        'Feeds.test_once_a_day_then_etag'),
    'feeds: a download is checked before it replaces the copy': (
        'feeds', "            check(tmp)\n", "            pass\n",
        'Feeds.test_a_download_it_does_not_understand_never_replaces_a_good_copy'),
    'feeds: the size cap while reading': (
        'feeds', "                if size > max_bytes:", "                if False:", 'Feeds.test_the_size_cap'),
    'feeds: the checksum': (
        'feeds', "        return path if meta.get('sha256') and _sha256(path) == meta['sha256'] else None",
        "        return path if os.path.exists(path) else None",
        'Feeds.test_a_copy_that_does_not_match_its_checksum_is_no_copy'),
    'check: fixed versions are compared': (
        'exposure', "                    if adv.fixed is None or eco.compare(version, adv.fixed) < 0:",
        "                    if True:", 'Check.test_a_fix_waiting_shows_by_epss_or_after_its_days'),
    'check: only KEV among unfixed': (
        'exposure', "if fixed is None and cve in kev})", "if fixed is None})",
        'Check.test_kev_with_a_fix_kev_without_and_nothing_else'),
    'check: a fix waiting is shown only past its thresholds': (
        'exposure', "        if not (kevs or top >= ex['epssMin'] or w['days'] > ex['fixWaitingDays']):",
        "        if False:", 'Check.test_a_fix_waiting_shows_by_epss_or_after_its_days'),
    'check: no copy is cannot check': (
        'exposure', "    if kev is None:\n        lines.append(", "    if False:\n        lines.append(",
        'Check.test_offline_is_cannot_check_never_all_clear'),
    'check: stale is drift': (
        'exposure', "        if f.path and f.fetched_at and now - f.fetched_at > datetime.timedelta(days=ex['staleDays']):",
        "        if False:", 'Check.test_a_stale_cache_is_drift_and_still_used'),
    'check: expired ignores are drift': (
        'exposure', "        if entry['until'] < today.isoformat():", "        if False:",
        'Check.test_ignore_until_a_date_then_drift'),
    'alert: an old report is no report': (
        'exposure', "or not now - max_age <= at <= now + datetime.timedelta(minutes=5):", ":",
        'Commands.test_the_alert_step_reports_a_missing_old_or_linked_report'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        if os.environ.get(fx.LIB_ENV):
            self.skipTest('a red check run')
        fx.red_check(self, os.path.abspath(__file__), MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
