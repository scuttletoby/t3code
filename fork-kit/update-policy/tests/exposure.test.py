"""update-policy exposure: Debian version order, the OVAL reader, the feed cache and the daily check, against small
recorded fixtures (tests/fixtures: cuts of Ubuntu's noble CVE OVAL with one kernel CVE, Debian's bookworm OVAL, CISA's
KEV list and FIRST.org's EPSS file), a fake web (no network), a fake dpkg-query and a fixed clock. RedCheck breaks each
rule in a copy of the code and shows its test then fails.

  python3 -I -B fork-kit/update-policy/tests/exposure.test.py
"""
import bz2, datetime, fcntl, gzip, io, json, os, shutil, subprocess, sys, tempfile, unittest, urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixture as fx  # noqa: E402
from fixture import at  # noqa: E402
from updatepolicy import debver, exposure, feeds, oval  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
UBUNTU_OVAL = 'https://security-metadata.canonical.com/oval/com.ubuntu.noble.cve.oval.xml.bz2'
DEBIAN_OVAL = 'https://www.debian.org/security/oval/oval-definitions-bookworm.xml.bz2'
NOW = at('2026-11-24 07:00')
SMALL = {'oval': 1, 'kev': 1, 'epss': 1}
NS = 'xmlns="http://oval.mitre.org/XMLSchema/oval-definitions-5" xmlns:l="x#linux"'


def text_of(name):
    with open(os.path.join(FIXTURES, name), encoding='utf-8') as f:
        return f.read()


def fixture_bytes(name, compress=None, replace=()):
    data = text_of(name)
    for a, b in replace:
        assert a in data, a
        data = data.replace(a, b)
    data = data.encode()
    return bz2.compress(data) if compress == 'bz2' else gzip.compress(data) if compress == 'gz' else data


def dpkg_line(binary, version, source='', source_version='', status='ii '):
    return f'{status}\t{binary}\t{version}\t{source or binary}\t{source_version or version}\n'


def mini_oval(*advisories):
    """A small OVAL file: (cve, package, fixed or None) per advisory, one dpkginfo test each."""
    defs, tests, objs, states = [], [], [], []
    for i, (cve, package, fixed) in enumerate(advisories):
        defs.append(f'<definition id="d{i}"><metadata><title>{cve} on X</title></metadata><criteria>'
                    f'<criterion test_ref="t{i}" comment="{package} package in x"/></criteria></definition>')
        state = f'<l:state state_ref="s{i}"/>' if fixed else ''
        tests.append(f'<l:dpkginfo_test id="t{i}"><l:object object_ref="o{i}"/>{state}</l:dpkginfo_test>')
        objs.append(f'<l:dpkginfo_object id="o{i}"><l:name>{package}</l:name></l:dpkginfo_object>')
        if fixed:
            states.append(f'<l:dpkginfo_state id="s{i}"><l:evr operation="less than">{fixed}</l:evr>'
                          '</l:dpkginfo_state>')
    return (f'<oval_definitions {NS}><definitions>{"".join(defs)}</definitions><tests>{"".join(tests)}</tests>'
            f'<objects>{"".join(objs)}</objects><states>{"".join(states)}</states></oval_definitions>')


def re_error():
    import re
    return re.error('bad pattern')


def write_tmp(case, text, name='f'):
    tmp = tempfile.mkdtemp()
    case.addCleanup(shutil.rmtree, tmp)
    path = os.path.join(tmp, name)
    with open(path, 'wb') as f:
        f.write(text if isinstance(text, bytes) else text.encode())
    return path


class Web:
    """A fake web: url -> (status, headers, bytes) or an exception; calls are recorded."""

    def __init__(self):
        self.pages, self.calls = {}, []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        page = self.pages.get(url)
        if page is None:
            raise OSError('Name or service not known')
        if isinstance(page, Exception):
            raise page
        status, h, body = page
        if headers.get('If-None-Match') and headers['If-None-Match'] == h.get('etag'):
            return 304, h, None
        return status, h, io.BytesIO(body)


class Versions(unittest.TestCase):
    TABLE = [('1.0', '1.0', 0), ('1.0', '1.1', -1), ('1.0~rc1', '1.0', -1), ('1.0', '1.0+b1', -1),
             ('1:0.9', '2.0', 1), ('2.38-1ubuntu5', '2.38-1ubuntu6', -1), ('2.38-1ubuntu6', '2.38-1ubuntu6.1', -1),
             ('0.18.6-2ubuntu0.1', '0.18.6-2', 1), ('1.2.4-0.2+deb12u1', '1.2.4-0.2', 1), ('1.0a', '1.0', 1),
             ('1.0-1', '1.0', 1), ('5.6.1+really5.4.5-1', '5.6.0-0.1', 1), ('1.0~~', '1.0~', -1),
             ('0:117.0.5938.62-1', '117.0.5938.62-1', 0), ('10', '9', 1), ('1.001', '1.1', 0), ('0:0', '0', 0),
             ('0:6.8.0-31', '0:6.8.0-44', -1)]

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
    def read(self, name, replace=(), **kw):
        return oval.read(write_tmp(self, fixture_bytes(name, 'bz2', replace)), **kw)

    def test_ubuntu_packages_fixed_and_unfixed_and_the_kernel(self):
        data = self.read('ubuntu-noble.cve.oval.xml')
        found = {(a.cve, a.source): a for a in data.advisories}
        glibc = found[('CVE-2023-4911', 'glibc')]
        self.assertEqual((glibc.fixed, glibc.priority), ('2.38-1ubuntu6', 'high'))
        self.assertIn('libc-bin', glibc.binaries)
        self.assertIsNone(found[('CVE-2021-1789', 'qtwebkit-opensource-src')].fixed)
        linux = [k for k in data.kernels if k.flavour == 'linux']
        self.assertEqual([(k.cve, k.fixed) for k in linux], [('CVE-2024-36971', '0:6.8.0-44')])
        self.assertTrue(linux[0].pattern.search('6.8.0-31-generic'))
        self.assertEqual(linux[0].version, ('0:', r'^([\d|\.]+-\d+)[-|\w]+$'))

    def test_debian_names_source_packages_and_0_0_is_no_fix(self):
        found = {(a.cve, a.source): a.fixed for a in self.read('debian-bookworm.oval.xml').advisories}
        self.assertEqual(found[('CVE-2023-4863', 'libwebp')], '0:1.2.4-0.2+deb12u1')
        self.assertEqual(found[('CVE-2024-36971', 'linux')], '0:6.1.94-1')
        self.assertIsNone(found[('CVE-2022-22965', 'libspring-java')])        # "less than 0:0"

    def test_a_shape_it_does_not_know_is_an_error_never_fewer_advisories(self):
        """Both lenses: renamed objects or another state operation silently dropped every fixed advisory."""
        cases = {
            'state operation': (('operation="less than">2.38-1ubuntu6',
                                 'operation="less than or equal">2.38-1ubuntu6'),),
            'object renamed': (('dpkginfo_object id=', 'dpkginfo_thing id='), ('</linux-def:dpkginfo_object>',
                                                                              '</linux-def:dpkginfo_thing>')),
            'a test kind': (('<unix-def:uname_test ', '<unix-def:uname2_test '), ('</unix-def:uname_test>',
                                                                                 '</unix-def:uname2_test>')),
            'variable gone': (('<constant_variable id=', '<constant_thing id='), ('</constant_variable>',
                                                                                   '</constant_thing>')),
            'unknown section': (('<tests>', '<checks></checks><tests>'),),
            'kernel test operation': (('operation="pattern match"', 'operation="equals"'),),
        }
        for name, replace in cases.items():
            with self.subTest(name), self.assertRaises(oval.FormatError):
                self.read('ubuntu-noble.cve.oval.xml', replace)

    def test_package_tests_that_name_nothing_are_a_format_error(self):
        mini = mini_oval(('CVE-2024-0001', 'a', '1-1'), ('CVE-2024-0002', 'b', None))
        self.assertEqual(len(oval.read(write_tmp(self, mini)).advisories), 2)
        with self.assertRaises(oval.FormatError):
            oval.read(write_tmp(self, mini.replace('<l:name>a</l:name>', '<l:name></l:name>')))

    def test_one_test_whose_object_is_gone_is_a_format_error(self):
        mini = mini_oval(*[(f'CVE-2024-{i:04d}', f'p{i}', '1-1') for i in range(1, 21)])
        self.assertEqual(len(oval.read(write_tmp(self, mini)).advisories), 20)
        gone = mini.replace('<l:dpkginfo_object id="o3"><l:name>p4</l:name></l:dpkginfo_object>', '')
        with self.assertRaisesRegex(oval.FormatError, 'whose object is not there'):
            oval.read(write_tmp(self, gone))

    def test_broken_files_are_errors(self):
        for name, text in (('html', b'<html><body>moved</body></html>'), ('cut', b'<oval_definitions><definitions>'),
                           ('empty', b''), ('no-tests', f'<oval_definitions {NS}><definitions><definition id="x">'
                                                       '<metadata><title>CVE-2024-0001</title></metadata>'
                                                       '</definition></definitions></oval_definitions>'.encode())):
            with self.subTest(name=name), self.assertRaises(oval.FormatError):
                oval.read(write_tmp(self, text))

    def test_the_decompressed_size_and_one_entry_are_capped(self):
        """Code lens: a 10 KB bz2 of 200 MB of unknown elements took 4.6 GB."""
        bomb = bz2.compress(f'<oval_definitions {NS}><definitions>'.encode() + b'<x/>' * 2_000_000)
        with self.assertRaisesRegex(oval.FormatError, 'larger than 1 MB once decompressed'):
            oval.read(write_tmp(self, bomb), max_bytes=1 << 20)
        big = f'<oval_definitions {NS}><definitions><definition id="d">' + '<x/>' * 2000 + '</definition>'
        with mock.patch.object(oval, 'MAX_ENTRY', 1000), self.assertRaisesRegex(oval.FormatError, 'more than 1000'):
            oval.read(write_tmp(self, big + '</definitions></oval_definitions>'))

    def test_a_broken_kernel_entry_is_a_format_error(self):
        for replace in ((r'pattern="^([\d|\.]+-\d+)[-|\w]+$"', r'pattern="^[\d|\.]+$"'),
                        (r'pattern="^([\d|\.]+-\d+)[-|\w]+$"', r'pattern="^([\d+$"'),
                        ('>0:6.8.0-44<', '>0:6.8.0 44 beta<')):
            with self.subTest(replace=replace), self.assertRaisesRegex(oval.FormatError, 'a kernel version'):
                self.read('ubuntu-noble.cve.oval.xml', (replace,))

    def test_only_the_running_kernels_entries_are_kept_and_all_are_counted(self):
        everything = self.read('ubuntu-noble.cve.oval.xml')
        mine = self.read('ubuntu-noble.cve.oval.xml', release='6.8.0-31-generic')
        self.assertEqual(mine.counts, everything.counts)
        self.assertEqual(everything.counts['kernels'], len(everything.kernels))
        self.assertLess(len(mine.kernels), len(everything.kernels))
        self.assertTrue(mine.kernels and all(k.pattern.search('6.8.0-31-generic') for k in mine.kernels))
        self.assertEqual(self.read('ubuntu-noble.cve.oval.xml', release='6.6.87.2-microsoft-standard-WSL2').kernels,
                         [])

    def test_finished_entries_and_sections_are_dropped_as_it_reads(self):
        roots, real = [], oval.ET.iterparse

        def spy(source, events):
            for event, el in real(source, events):
                if not roots:
                    roots.append(el)
                yield event, el
        with mock.patch.object(oval.ET, 'iterparse', spy):
            self.read('ubuntu-noble.cve.oval.xml')
        self.assertEqual(list(roots[0]), [])                    # nothing kept under the root

    def test_a_less_than_state_is_the_fix(self):
        data = oval.read(write_tmp(self, mini_oval(('CVE-2024-0001', 'a', '1.2-1'), ('CVE-2024-0002', 'a', '0:0'))))
        self.assertEqual([(a.cve, a.fixed) for a in data.advisories], [('CVE-2024-0001', '1.2-1'),
                                                                       ('CVE-2024-0002', None)])


class Feeds(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix='update-policy-cache-')
        self.addCleanup(shutil.rmtree, self.cache)
        self.web = Web()
        self.url = 'https://feeds.example/kev.json'
        self.web.pages[self.url] = (200, {'etag': '"v1"', 'content-length': '5312'}, fixture_bytes('kev.json'))

    def fetch(self, now=NOW, cap=1 << 20, check=lambda p: len(exposure.read_kev(p))):
        return feeds.fetch(self.cache, 'kev', self.url, check, now, cap, 10, self.web)

    def test_once_a_day_then_etag(self):
        got = self.fetch()
        self.assertEqual((got.problem, got.fetched_at), (None, NOW))
        self.assertEqual(len(exposure.read_kev(got.path)), 6)
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
        self.assertEqual(len(exposure.read_kev(got.path)), 6)          # the old copy stays

    def test_a_download_far_smaller_than_the_kept_copy_never_replaces_it(self):
        self.fetch()
        small = json.loads(fixture_bytes('kev.json'))
        small['vulnerabilities'] = small['vulnerabilities'][:2]
        self.web.pages[self.url] = (200, {'etag': '"v2"'}, json.dumps(small).encode())
        got = self.fetch(NOW + datetime.timedelta(days=1))
        self.assertEqual(got.problem, 'kev: the download holds entries 2 under half of 6 of the kept copy: kept the '
                                      'old copy (day 1 of 3; the same smaller feed 3 days in a row is accepted)')
        self.assertEqual(len(exposure.read_kev(got.path)), 6)

    def test_a_lasting_shrink_is_accepted_on_the_third_day_in_a_row_and_said(self):
        self.fetch()
        small = json.loads(fixture_bytes('kev.json'))
        small['vulnerabilities'] = small['vulnerabilities'][:2]
        self.web.pages[self.url] = (200, {'etag': '"v2"'}, json.dumps(small).encode())
        for day in (1, 2):                                             # refused, counted
            got = self.fetch(NOW + datetime.timedelta(days=day))
            self.assertIn(f'(day {day} of 3;', got.problem)
        self.fetch(NOW + datetime.timedelta(days=2, hours=3))           # again the same day: still day 2
        small['vulnerabilities'] = small['vulnerabilities'][:1]          # changed again: starts over
        self.web.pages[self.url] = (200, {}, json.dumps(small).encode())
        self.assertIn('(day 1 of 3;', self.fetch(NOW + datetime.timedelta(days=3)).problem)
        for day in (4, 5):
            got = self.fetch(NOW + datetime.timedelta(days=day))
        self.assertIsNone(got.problem)
        self.assertEqual(got.notice, 'kev: the feed shrank (entries 1 under half of 6) and stayed so 3 days in a '
                                     'row: accepted, the smaller copy is in use')
        self.assertEqual(len(exposure.read_kev(got.path)), 1)

    def test_package_and_kernel_counts_are_floored_apart(self):
        self.url = 'https://feeds.example/oval.xml'
        self.web.pages[self.url] = (200, {}, b'x')
        counts = [{'packages': 100, 'kernels': 1000}, {'packages': 10, 'kernels': 1090}]
        feeds.fetch(self.cache, 'oval', self.url, lambda p: counts[0], NOW, 1 << 20, 10, self.web)
        got = feeds.fetch(self.cache, 'oval', self.url, lambda p: counts[1], NOW + datetime.timedelta(days=1),
                          1 << 20, 10, self.web)
        self.assertIn('the download holds packages 10 under half of 100 of the kept copy', got.problem)

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

    def test_only_https_and_no_redirect_to_http(self):
        with self.assertRaisesRegex(urllib.error.URLError, 'only https'):
            feeds.urlopen('http://feeds.example/kev.json', {}, 5)
        handler = feeds._HttpsOnly()
        with self.assertRaisesRegex(urllib.error.URLError, 'not https'):
            handler.redirect_request(None, None, 302, 'Found', {}, 'http://evil.example/kev.json')

    def test_a_killed_runs_temporary_files_are_swept(self):
        for name in ('.oval.abc.tmp', '.kev.x.tmp', 'kev.data'):
            open(os.path.join(self.cache, name), 'w').close()
        feeds.sweep(self.cache)
        self.assertEqual(sorted(os.listdir(self.cache)), ['kev.data'])


class Base(fx.HostCase):
    def setUp(self):
        super().setUp()
        floors = mock.patch.dict(exposure.FLOORS, SMALL)
        floors.start()
        self.addCleanup(floors.stop)
        self.cache = os.path.join(self.root, 'cache')
        os.makedirs(self.cache)
        self.write('/etc/os-release', 'NAME="Ubuntu"\nID=ubuntu\nVERSION_CODENAME=noble\n')
        self.packages = [dpkg_line('libc-bin', '2.38-1ubuntu5', 'glibc'), dpkg_line('libc6', '2.38-1ubuntu5', 'glibc'),
                         dpkg_line('libqt5webkit5', '5.212.0~alpha4-36ubuntu2', 'qtwebkit-opensource-src'),
                         dpkg_line('aide', '0.18.6-2', 'aide'), dpkg_line('bash', '5.2.21-2ubuntu4'),
                         dpkg_line('gcc-h8300-hms', '1:3.4.6+dfsg2-4.1'),     # unfixed, not on KEV: no line
                         dpkg_line('linux-modules-6.8.0-45-generic', '6.8.0-45.45', 'linux'),
                         dpkg_line('linux-headers-6.8.0-31-generic', '6.8.0-31.31', 'linux')]
        self.host.answers['dpkg-query'] = lambda argv: (0, ''.join(self.packages), '')
        self.host.release = '6.8.0-45-generic'                          # fixed (44): no kernel line
        self.web = Web()
        self.web.pages[UBUNTU_OVAL] = (200, {'etag': '"o1"'}, fixture_bytes('ubuntu-noble.cve.oval.xml', 'bz2'))
        self.web.pages[exposure.KEV_URL] = (200, {'etag': '"k1"'}, fixture_bytes('kev.json'))
        self.web.pages[exposure.EPSS_URL] = (200, {'etag': '"e1"'}, fixture_bytes('epss.csv', 'gz'))

    def check(self, now=NOW, **over):
        s = fx.make_settings(exposure=dict({'enabled': True}, **over))
        return exposure.check(self.host, s, self.cache, now, self.web)


class Check(Base):
    def test_kev_with_a_fix_kev_without_and_nothing_else(self):
        lines, notes, report = self.check()
        self.assertEqual(lines, [
            'KEV CVE-2023-4911 is exploited in the wild: glibc 2.38-1ubuntu5 (libc-bin, libc6): update to '
            '2.38-1ubuntu6 (EPSS 0.64; CISA due 2023-12-12; GNU GNU C Library)',
            'KEV CVE-2021-1789 is exploited in the wild and has no fix yet: mitigate or watch: '
            'qtwebkit-opensource-src 5.212.0~alpha4-36ubuntu2 (libqt5webkit5) (EPSS 0.14; CISA due 2022-05-25; '
            'Apple Multiple Products)'])
        self.assertEqual(notes, [])
        self.assertEqual([f['action'] for f in report['findings']], ['update', 'mitigate'])
        self.assertEqual(sorted({c[0] for c in self.web.calls}), sorted([UBUNTU_OVAL, exposure.KEV_URL,
                                                                          exposure.EPSS_URL]))   # nothing sent

    def test_the_running_kernel_by_its_flavour(self):
        self.host.release = '6.8.0-31-generic'
        self.packages[-2] = dpkg_line('linux-modules-6.8.0-31-generic', '6.8.0-31.31', 'linux')
        lines, notes, _ = self.check()
        self.assertIn('KEV CVE-2024-36971 is exploited in the wild: kernel linux 6.8.0-31-generic '
                      '(kernel 6.8.0-31-generic): update to 6.8.0-44 and restart (EPSS 0.03', '\n'.join(lines))
        self.assertEqual(notes, [])

    def test_a_kernel_it_cannot_place_is_a_note_never_a_finding(self):
        self.host.release = '6.6.87.2-microsoft-standard-WSL2'
        lines, notes, _ = self.check()
        self.assertEqual(notes, ['kernel: not checked (the running kernel 6.6.87.2-microsoft-standard-WSL2 is none of '
                                 "ubuntu's kernels in its data)"])
        self.assertEqual(len(lines), 2)                                  # the two KEV package lines only
        self.host.release = '6.8.0-31-generic'                           # linux and linux-riscv match; no modules
        self.packages = self.packages[:-2]
        _, notes, _ = self.check()
        self.assertIn('kernel: not checked (the running kernel 6.8.0-31-generic matches several kernels', notes[0])

    def test_old_kernel_packages_never_give_lines(self):
        self.packages = [dpkg_line('linux-headers-6.8.0-20-generic', '6.8.0-20.20', 'linux'),
                         dpkg_line('linux-image-6.8.0-20-generic', '6.8.0-20.20', 'linux-signed'),
                         dpkg_line('linux-modules-6.8.0-45-generic', '6.8.0-45.45', 'linux')]
        self.assertEqual(self.check(epssMin=0, fixWaitingDays=0)[0], [])

    def test_a_fix_waiting_shows_by_epss_or_after_its_days(self):
        self.packages = [dpkg_line('aide', '0.18.6-2', 'aide')]
        self.assertEqual(self.check()[0], [])                          # EPSS 0.003, first seen today
        lines = self.check(epssMin=0.001)[0]
        self.assertEqual(lines, ['security fix waiting 0 day(s): aide 0.18.6-2 (aide) -> 0.18.6-2ubuntu0.1: '
                                 'CVE-2025-54389 (EPSS 0.00)'])
        later = NOW + datetime.timedelta(days=4)
        self.assertEqual(self.check(later)[0], ['security fix waiting 4 day(s): aide 0.18.6-2 (aide) -> '
                                                '0.18.6-2ubuntu0.1: CVE-2025-54389 (EPSS 0.00)'])
        self.packages = [dpkg_line('aide', '0.18.6-2ubuntu0.1', 'aide')]          # updated: gone
        self.assertEqual(self.check(later)[0], [])

    def test_the_waiting_clock_survives_a_run_without_data_and_a_newer_fix(self):
        """Code lens: a run with no usable OVAL copy wrote fixSeen={}; the key held the newest fixed version."""
        self.packages = [dpkg_line('aide', '0.18.6-2', 'aide')]
        self.check()                                                    # first seen now
        with open(os.path.join(self.cache, 'oval.data'), 'ab') as f:
            f.write(b'x')                                               # no usable copy, and offline
        self.web.pages[UBUNTU_OVAL] = OSError('offline')
        self.assertIn('cannot check: no usable copy of the distribution\'s security data',
                      self.check(NOW + datetime.timedelta(days=2))[0])
        newer = (('0.18.6-2ubuntu0.1', '0.18.6-2ubuntu0.2'),)          # a newer fix comes out
        self.web.pages[UBUNTU_OVAL] = (200, {}, fixture_bytes('ubuntu-noble.cve.oval.xml', 'bz2', newer))
        lines = self.check(NOW + datetime.timedelta(days=4))[0]
        self.assertEqual(lines, ['security fix waiting 4 day(s): aide 0.18.6-2 (aide) -> 0.18.6-2ubuntu0.2: '
                                 'CVE-2025-54389 (EPSS 0.00)'])

    def test_a_source_waits_since_its_oldest_first_sight(self):
        self.web.pages[UBUNTU_OVAL] = (200, {}, mini_oval(('CVE-2024-0001', 'a', '1.2-1')).encode())
        self.packages = [dpkg_line('a', '1.0-1')]
        self.check()
        self.web.pages[UBUNTU_OVAL] = (200, {}, mini_oval(('CVE-2024-0001', 'a', '1.2-1'),
                                                          ('CVE-2024-0002', 'a', '1.3-1')).encode())
        self.check(NOW + datetime.timedelta(days=3))                   # a second CVE, first seen on day 3
        lines = self.check(NOW + datetime.timedelta(days=4))[0]
        self.assertEqual(lines, ['security fix waiting 4 day(s): a 1.0-1 (a) -> 1.3-1: CVE-2024-0001, CVE-2024-0002 '
                                 '(EPSS 0.00)'])

    def test_the_kernels_waiting_clock_survives_a_reboot_into_a_newer_kernel_still_behind(self):
        kev = json.loads(fixture_bytes('kev.json'))
        kev['vulnerabilities'] = [v for v in kev['vulnerabilities'] if v['cveID'] != 'CVE-2024-36971']
        self.web.pages[exposure.KEV_URL] = (200, {}, json.dumps(kev).encode())
        self.host.release = '6.8.0-31-generic'
        self.packages[-2] = dpkg_line('linux-modules-6.8.0-31-generic', '6.8.0-31.31', 'linux')
        self.check()
        self.host.release = '6.8.0-40-generic'                         # rebooted; 44 is the fix
        self.packages[-2] = dpkg_line('linux-modules-6.8.0-40-generic', '6.8.0-40.40', 'linux')
        lines = self.check(NOW + datetime.timedelta(days=4))[0]
        self.assertIn('security fix waiting 4 day(s): kernel linux 6.8.0-40-generic (kernel 6.8.0-40-generic) -> '
                      '6.8.0-44 and restart: CVE-2024-36971', '\n'.join(lines))

    def test_the_first_cache_format_is_cleaned_up_and_its_clocks_kept(self):
        self.packages = [dpkg_line('aide', '0.18.6-2', 'aide')]
        with open(os.path.join(self.cache, 'epss.json'), 'w') as f:
            json.dump({'CVE-2025-54389': [0.1, NOW.isoformat()]}, f)
        with open(os.path.join(self.cache, 'state.json'), 'w') as f:       # "BINARY FIXED" keys
            json.dump({'fixSeen': {'aide 0.18.6-2ubuntu0.1': (NOW - datetime.timedelta(days=5)).isoformat()}}, f)
        self.assertEqual(self.check()[0], ['security fix waiting 5 day(s): aide 0.18.6-2 (aide) -> 0.18.6-2ubuntu0.1: '
                                           'CVE-2025-54389 (EPSS 0.00)'])
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'epss.json')))

    def test_one_line_per_source_with_its_highest_fix(self):
        mini = mini_oval(('CVE-2024-0001', 'a', '1.2-1'), ('CVE-2024-0002', 'a', '1.3-1'),
                         ('CVE-2024-0003', 'b', '2-1'))
        self.web.pages[UBUNTU_OVAL] = (200, {}, mini.encode())
        self.packages = [dpkg_line('a', '1.0-1'), dpkg_line('b1', '1-1', 'b'), dpkg_line('b2', '1-1', 'b')]
        lines = self.check(epssMin=0)[0]
        self.assertEqual(lines, ['security fix waiting 0 day(s): a 1.0-1 (a) -> 1.3-1: CVE-2024-0001, CVE-2024-0002 '
                                 '(EPSS 0.00)', 'security fix waiting 0 day(s): b 1-1 (b1, b2) -> 2-1: CVE-2024-0003 '
                                                '(EPSS 0.00)'])
        with mock.patch.object(exposure, 'MAX_LINES', 1):
            lines = self.check(epssMin=0)[0]
        self.assertEqual(lines[-1], f'+2 more lines (all in {self.cache}/report.json)')

    def test_held_and_half_installed_packages_count_removed_do_not_and_unknown_is_cannot_check(self):
        for status in ('hi ', 'iU ', 'iF ', 'iH ', 'iW ', 'it ', 'iiR'):
            with self.subTest(status=status):
                self.packages = [dpkg_line('aide', '0.18.6-2', 'aide', status=status)]
                self.assertIn('aide 0.18.6-2', self.check(epssMin=0)[0][0])
        for status in ('rc ', 'un '):
            self.packages = [dpkg_line('aide', '0.18.6-2', 'aide', status=status), dpkg_line('bash', '5.2')]
            self.assertEqual(self.check(epssMin=0)[0], [])
        self.packages = [dpkg_line('aide', '0.18.6-2', 'aide', status='iX ')]
        self.assertEqual(self.check()[0], ['cannot check: dpkg-query: a status this check does not know (iX for '
                                           'aide)'])

    def test_ranked_kev_first_then_by_epss(self):
        self.packages.append(dpkg_line('aide', '0.18.6-2', 'aide'))
        lines = self.check(epssMin=0)[0]
        self.assertEqual([x.split(' is ')[0].split(':')[0] for x in lines],
                         ['KEV CVE-2023-4911', 'KEV CVE-2021-1789', 'security fix waiting 0 day(s)'])

    def test_debian_source_names_no_fix_kev_and_the_running_image(self):
        self.write('/etc/os-release', 'ID=debian\nVERSION_CODENAME=bookworm\n')
        self.host.release = '6.1.0-21-amd64'
        self.packages = [dpkg_line('libwebp7', '1.2.4-0.2', 'libwebp', '1.2.4-0.2'),
                         dpkg_line('libspring-java', '4.3.30-2'),
                         dpkg_line('linux-image-6.1.0-21-amd64', '6.1.90-1', 'linux-signed-amd64', '6.1.90+1'),
                         dpkg_line('linux-headers-6.1.0-18-amd64', '6.1.76-1', 'linux'),
                         dpkg_line('linux-libc-dev', '6.1.94-1', 'linux')]
        self.web.pages[DEBIAN_OVAL] = (200, {}, fixture_bytes('debian-bookworm.oval.xml', 'bz2'))
        lines, notes, _ = self.check()
        self.assertEqual(lines, [
            'KEV CVE-2023-4863 is exploited in the wild: libwebp 1.2.4-0.2 (libwebp7): update to 0:1.2.4-0.2+deb12u1 '
            '(EPSS 1.00; CISA due 2023-10-04; Google Chromium WebP)',
            'KEV CVE-2022-22965 is exploited in the wild and has no fixed version listed by Debian: mitigate or watch '
            '(or, if Debian judged it not to apply, add an ignore entry): libspring-java 4.3.30-2 (libspring-java) '
            '(EPSS 1.00; CISA due 2022-04-25; VMware Spring Framework)',
            'KEV CVE-2024-36971 is exploited in the wild: kernel linux 6.1.90-1 (linux-image-6.1.0-21-amd64): update '
            'to 6.1.94-1 and restart (EPSS 0.03; CISA due 2024-08-28; Android Kernel)'])
        self.assertEqual(notes, [])
        self.packages = self.packages[:2]
        _, notes, _ = self.check()
        self.assertEqual(notes, ['kernel: not checked (the running kernel 6.1.0-21-amd64 has no linux-image '
                                 'package)'])

    def test_offline_is_cannot_check_never_all_clear(self):
        self.web.pages.clear()
        lines, notes, _ = self.check()
        self.assertEqual(lines, ['cannot check: oval: download failed (Name or service not known)',
                                 'cannot check: kev: download failed (Name or service not known)',
                                 'cannot check: epss: download failed (Name or service not known)',
                                 'cannot check: no usable copy of the distribution\'s security data',
                                 'cannot check: no usable copy of CISA\'s KEV list'])
        self.assertEqual(notes, ['kernel: not checked (no security data)'])

    def test_a_stale_cache_is_drift_and_still_used(self):
        self.check()
        self.web.pages.clear()
        lines, _, _ = self.check(NOW + datetime.timedelta(days=5))
        self.assertEqual(lines[:3], [f'cannot check: {n}: download failed (Name or service not known)'
                                     for n in ('oval', 'kev', 'epss')])
        self.assertIn('KEV CVE-2023-4911', lines[3])                     # the old copy is still read
        self.assertEqual(lines[-3:], [f'drift: the cached {n} feed is 5 days old (more than 3): the check works on '
                                      'old data' for n in ('oval', 'kev', 'epss')])

    def test_a_feed_format_change_or_a_too_small_feed_is_cannot_check(self):
        self.web.pages[exposure.KEV_URL] = (200, {}, b'{"catalogVersion": "2", "entries": []}')
        lines, _, _ = self.check()
        self.assertEqual(lines[:2], ['cannot check: kev: the download is in a format this check does not understand '
                                     '(no "vulnerabilities" list)', 'cannot check: no usable copy of CISA\'s KEV list'])
        with mock.patch.dict(exposure.FLOORS, {'kev': 100}):
            self.web.pages[exposure.KEV_URL] = (200, {}, fixture_bytes('kev.json'))
            self.assertIn('cannot check: kev: the download is in a format this check does not understand (only 6 '
                          'entries, under the floor of 100)', self.check()[0])
        self.web.pages[exposure.EPSS_URL] = (200, {}, gzip.compress(b'cve,score\nCVE-2024-0001,0.1\n'))
        self.assertIn('cannot check: epss: the download is in a format this check does not understand (no '
                      '"cve,epss,percentile" header (cve,score))', self.check(NOW + datetime.timedelta(days=1))[0])

    def test_ignore_until_a_date_then_drift(self):
        ignore = [{'cve': 'CVE-2021-1789', 'reason': 'qtwebkit unused, removal planned', 'until': '2026-12-01'}]
        lines, _, _ = self.check(ignore=ignore)
        self.assertEqual([x.split(' is ')[0] for x in lines], ['KEV CVE-2023-4911'])
        lines, _, _ = self.check(NOW + datetime.timedelta(days=10), ignore=ignore)
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
        for name, text in (('state.json', '[1, 2'), ('kev.meta.json', '{"url": 1}'), ('oval.data', 'not oval')):
            with open(os.path.join(self.cache, name), 'w') as f:
                f.write(text)
        lines, _, _ = self.check()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(all(x.startswith('KEV ') for x in lines))

    def test_server_text_reaches_the_lines_as_one_line(self):
        self.web.pages[exposure.KEV_URL] = OSError('bad gateway\n<0>update-policy: forged')
        lines, _, _ = self.check()
        self.assertIn('cannot check: kev: download failed (bad gateway\\x0a<0>update-policy: forged)', lines)

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

    def test_run_prints_writes_the_report_and_the_alert_step_sends_it_with_the_notes(self):
        self.host.release = '6.6.87.2-microsoft-standard-WSL2'
        code, printed, s = self.run_cmd()
        self.assertEqual((code, len(printed)), (1, 3))
        self.assertTrue(printed[-1].startswith('kernel: not checked'))
        m = fx.FakeMachine(self.host, NOW)
        self.assertEqual(exposure.alert(m, lambda: s, self.cache, NOW + datetime.timedelta(minutes=1)), 0)
        self.assertEqual(m.alerts, [('exposure', '\n'.join(printed))])

    def test_a_note_alone_is_no_finding_and_no_alert(self):
        self.host.release = '6.6.87.2-microsoft-standard-WSL2'
        self.packages = [dpkg_line('bash', '5.2.21-2ubuntu4')]
        code, printed, s = self.run_cmd()
        self.assertEqual((code, printed), (0, ["kernel: not checked (the running kernel 6.6.87.2-microsoft-standard-"
                                               "WSL2 is none of ubuntu's kernels in its data)"]))
        m = fx.FakeMachine(self.host, NOW)
        self.assertEqual(exposure.alert(m, lambda: s, self.cache, NOW), 0)
        self.assertEqual(m.alerts, [])

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

    def test_a_killed_runs_temporary_files_are_swept_and_a_second_run_waits_for_none(self):
        open(os.path.join(self.cache, '.oval.abc123.tmp'), 'w').close()
        self.run_cmd()
        self.assertFalse(os.path.exists(os.path.join(self.cache, '.oval.abc123.tmp')))
        fd = os.open(os.path.join(self.cache, 'lock'), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        code, printed, _ = self.run_cmd()
        self.assertEqual((code, printed), (0, ["update-policy exposure: another run holds the cache's lock: nothing "
                                               'to do']))

    def test_a_fault_is_a_cannot_check_report_never_a_traceback(self):
        with mock.patch.object(exposure, 'check', side_effect=re_error()):
            code, printed, _ = self.run_cmd()
        self.assertEqual((code, printed), (1, ['cannot check: the check failed (error: bad pattern)']))
        with open(os.path.join(self.cache, exposure.REPORT)) as f:
            self.assertEqual(json.load(f)['lines'], printed)

    def test_the_report_is_written_while_the_lock_is_held(self):
        held, real = [], exposure._write_json

        def spy(cache, name, data):
            if name == exposure.REPORT:
                fd = os.open(os.path.join(cache, 'lock'), os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append(False)
                except BlockingIOError:
                    held.append(True)
                finally:
                    os.close(fd)
            return real(cache, name, data)
        with mock.patch.object(exposure, '_write_json', spy):
            self.run_cmd()
        self.assertEqual(held, [True])

    def test_as_root_on_the_units_cache_it_refuses(self):
        s = fx.make_settings(exposure={'enabled': True})
        with mock.patch.object(exposure.os, 'geteuid', return_value=0), mock.patch('builtins.print') as out:
            self.assertEqual(exposure.run(self.host, lambda: s, exposure.CACHE, NOW, self.web), 1)
        self.assertIn('systemctl start update-policy-exposure.service', out.call_args_list[0].args[0])
        self.assertEqual(self.web.calls, [])


class ExposureSettings(unittest.TestCase):
    def test_defaults_off_and_bad_values_refused(self):
        s = fx.make_settings()['exposure']
        self.assertEqual((s['enabled'], s['epssMin'], s['fixWaitingDays'], s['staleDays'], s['maxFeedMb'],
                          s['maxXmlMb']), (False, 0.1, 3, 3, 64, 1024))
        for bad, words in (({'ignore': [{'cve': 'CVE-2024-1', 'reason': 'x', 'until': '2026-12-01'}]}, 'not a CVE'),
                           ({'ignore': [{'cve': 'CVE-2024-0001', 'reason': ' ', 'until': '2026-12-01'}]}, 'reason'),
                           ({'ignore': [{'cve': 'CVE-2024-0001', 'reason': 'x', 'until': 'soon'}]}, 'YYYY-MM-DD'),
                           ({'ignore': [{'cve': 'CVE-2024-0001'}]}, 'each entry'),
                           ({'epssMin': 2}, 'from 0 to 1'), ({'epssUrl': 'http://x'}, 'https://'),
                           ({'enabled': 'yes'}, 'bool'), ({'feeds': []}, 'unknown key')):
            with self.subTest(bad=bad), self.assertRaises(fx.settings.MachineFileError) as e:
                fx.make_settings(exposure=bad)
            self.assertIn(words, str(e.exception))


MUTATIONS = {
    'dpkg order: ~ sorts first': (
        'debver', "    if c == '~':\n        return -1", "    if c == '~':\n        return 1",
        'Versions.test_dpkg_order'),
    'oval: no tests is no format': (
        'oval', "    if not defs or not any(", "    if not defs or not all(", 'Oval.test_broken_files_are_errors'),
    'oval: another state operation is a format error': (
        'oval', "                    if state[1] != 'less than' or not state[2]:",
        "                    if not state[2]:",
        'Oval.test_a_shape_it_does_not_know_is_an_error_never_fewer_advisories'),
    'oval: a missing object is a format error': (
        'oval', "                if obj is None:\n                    raise",
        "                if obj is None:\n                    continue\n                    raise",
        'Oval.test_one_test_whose_object_is_gone_is_a_format_error'),
    'oval: 0:0 is no fix': (
        'oval', "                    fixed = None if no_fix(state[2]) else state[2]",
        "                    fixed = state[2]", 'Oval.test_debian_names_source_packages_and_0_0_is_no_fix'),
    'oval: the decompressed cap': (
        'oval', "        if self.seen > self.limit:", "        if False:",
        'Oval.test_the_decompressed_size_and_one_entry_are_capped'),
    'oval: package tests must resolve': (
        'oval', "    if total and resolved < RESOLVED * total:", "    if False:",
        'Oval.test_package_tests_that_name_nothing_are_a_format_error'),
    'oval: a broken kernel version is a format error': (
        'oval', "                            if re.compile(version[1]).groups < 1:",
        "                            if False:", 'Oval.test_a_broken_kernel_entry_is_a_format_error'),
    'oval: only the running kernel\'s entries are kept': (
        'oval', "                if release is not None and not compiled.search(release):",
        "                if False:", 'Oval.test_only_the_running_kernels_entries_are_kept_and_all_are_counted'),
    'oval: finished entries are dropped': (
        'oval', "                    stack[-1].remove(el)            # finished entries and sections are dropped",
        "                    pass", 'Oval.test_finished_entries_and_sections_are_dropped_as_it_reads'),
    'oval: kernel tests are read': (
        'oval', "                for cve in cves:\n                    kernels.append(",
        "                for cve in []:\n                    kernels.append(",
        'Oval.test_ubuntu_packages_fixed_and_unfixed_and_the_kernel'),
    'feeds: once a day': (
        'feeds', "        return Fetched(copy, None, fetched_at)          # once a day", "        pass",
        'Feeds.test_once_a_day_then_etag'),
    'feeds: a download is checked before it replaces the copy': (
        'feeds', "            entries = _counts(check(tmp))", "            entries = {'entries': 10 ** 9}",
        'Feeds.test_a_download_it_does_not_understand_never_replaces_a_good_copy'),
    'feeds: the floor against the kept copy': (
        'feeds', "        shrunk = _shrunk(entries, meta.get('count')) if copy else []", "        shrunk = []",
        'Feeds.test_a_download_far_smaller_than_the_kept_copy_never_replaces_it'),
    'feeds: each part is floored': (
        'feeds', "    return [(k, new.get(k, 0), v) for k, v in sorted(kept.items()) if new.get(k, 0) < FLOOR * v]",
        "    return [(k, sum(new.values()), sum(kept.values())) for k in ['all'] if sum(new.values()) < FLOOR * "
        "sum(kept.values())]", 'Feeds.test_package_and_kernel_counts_are_floored_apart'),
    'feeds: a lasting shrink needs days in a row': (
        'feeds', "            if days < SHRINK_DAYS:", "            if days < 2:",
        'Feeds.test_a_lasting_shrink_is_accepted_on_the_third_day_in_a_row_and_said'),
    'feeds: the size cap while reading': (
        'feeds', "                if size > max_bytes:", "                if False:", 'Feeds.test_the_size_cap'),
    'feeds: the checksum': (
        'feeds', "        return path if meta.get('sha256') and _sha256(path) == meta['sha256'] else None",
        "        return path if os.path.exists(path) else None",
        'Feeds.test_a_copy_that_does_not_match_its_checksum_is_no_copy'),
    'feeds: no redirect to http': (
        'feeds', "        if not newurl.lower().startswith('https://'):", "        if False:",
        'Feeds.test_only_https_and_no_redirect_to_http'),
    'check: the temporary files are swept': (
        'exposure', "    feeds.sweep(cache)\n", "",
        'Commands.test_a_killed_runs_temporary_files_are_swept_and_a_second_run_waits_for_none'),
    'check: fixed versions are compared': (
        'exposure', "                    if adv.fixed is None or eco.compare(version, adv.fixed) < 0:",
        "                    if True:", 'Check.test_a_fix_waiting_shows_by_epss_or_after_its_days'),
    'check: only KEV among unfixed': (
        'exposure', "        if a[0] in kev:\n            by_cve",
        "        if a[0] in kev or a[4] is None:\n            by_cve",
        'Check.test_kev_with_a_fix_kev_without_and_nothing_else'),
    'check: a fix waiting is shown only past its thresholds': (
        'exposure', "        if not (top >= ex['epssMin'] or days > ex['fixWaitingDays']):", "        if False:",
        'Check.test_a_fix_waiting_shows_by_epss_or_after_its_days'),
    'check: the waiting clock survives a run without data': (
        'exposure', "    seen = dict(state['fixSeen']) if data is None else {}", "    seen = {}",
        'Check.test_the_waiting_clock_survives_a_run_without_data_and_a_newer_fix'),
    'check: the highest fix of a source': (
        'exposure', "        if eco.compare(fixed, w['fixed']) > 0:\n            w['fixed'] = fixed",
        "        if False:\n            w['fixed'] = fixed", 'Check.test_one_line_per_source_with_its_highest_fix'),
    'check: held packages count': (
        'exposure', "INSTALLED, NOT_INSTALLED = frozenset('iHUFWt'), frozenset('nc')",
        "INSTALLED, NOT_INSTALLED = frozenset('i'), frozenset('ncHUFWt')",
        'Check.test_held_and_half_installed_packages_count_removed_do_not_and_unknown_is_cannot_check'),
    'check: kernel packages are judged by the running kernel only': (
        'exposure', "            if KERNEL_PKG.match(binary):\n                continue\n", "",
        'Check.test_debian_source_names_no_fix_kev_and_the_running_image'),
    'check: the running kernel': (
        'exposure', "        found, note = kernel_affected(data, release, binaries, distro)",
        "        found, note = [], None", 'Check.test_the_running_kernel_by_its_flavour'),
    'check: an unplaced kernel is a note': (
        'exposure', "        if note:\n            notes.append(", "        if False:\n            notes.append(",
        'Check.test_a_kernel_it_cannot_place_is_a_note_never_a_finding'),
    'check: no copy is cannot check': (
        'exposure', "    if kev is None:\n        lines.append(", "    if False:\n        lines.append(",
        'Check.test_offline_is_cannot_check_never_all_clear'),
    'check: stale is drift': (
        'exposure', "        if f.path and f.fetched_at and now - f.fetched_at > datetime.timedelta(days=ex['staleDays']):",
        "        if False:", 'Check.test_a_stale_cache_is_drift_and_still_used'),
    'check: expired ignores are drift': (
        'exposure', "        if entry['until'] < today.isoformat():", "        if False:",
        'Check.test_ignore_until_a_date_then_drift'),
    'check: lines are one line': (
        'exposure', "    lines = [one_line(x, 400) for x in lines + found + drift]",
        "    lines = lines + found + drift", 'Check.test_server_text_reaches_the_lines_as_one_line'),
    'run: a fault is a cannot-check report': (
        'exposure', "        except Exception as e:      # noqa: BLE001 - a fault is a cannot-check report",
        "        except OSError as e:      # noqa: BLE001 - a fault is a cannot-check report",
        'Commands.test_a_fault_is_a_cannot_check_report_never_a_traceback'),
    'run: the report under the lock': (
        'exposure', "            _write_json(cache, REPORT, report)              # under the lock",
        "            pass              # under the lock", 'Commands.test_the_report_is_written_while_the_lock_is_held'),
    'check: a source waits since its oldest first sight': (
        'exposure', "        w['since'] = min(w['since'], first)", "        w['since'] = first",
        'Check.test_a_source_waits_since_its_oldest_first_sight'),
    'check: the kernel clock is per flavour': (
        'exposure', "    return f'{cve} {source}' if source.startswith('kernel ') else f'{cve} {binary}'",
        "    return f'{cve} {binary}'",
        'Check.test_the_kernels_waiting_clock_survives_a_reboot_into_a_newer_kernel_still_behind'),
    'check: the first format\'s clocks carry over': (
        'exposure', "or older.get(binary) or now.isoformat()", "or now.isoformat()",
        'Check.test_the_first_cache_format_is_cleaned_up_and_its_clocks_kept'),
    "run: not as root on the unit's cache": (
        'exposure', "    if os.geteuid() == 0 and os.path.abspath(cache) == CACHE:", "    if False:",
        'Commands.test_as_root_on_the_units_cache_it_refuses'),
    'alert: an old report is no report': (
        'exposure', "                or not now - max_age <= at <= now + datetime.timedelta(minutes=5):", ":",
        'Commands.test_the_alert_step_reports_a_missing_old_or_linked_report'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        if os.environ.get(fx.LIB_ENV):
            self.skipTest('a red check run')
        fx.red_check(self, os.path.abspath(__file__), MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
