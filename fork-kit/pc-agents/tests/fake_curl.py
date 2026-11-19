"""The fake curl of fake_distro.py (its usr/bin/curl runs `python3 -I fake_curl.py ROOT ARG...`). Each call is logged to
/log/curl in the fake root (ROOT). What it serves:

  https://github.com/O/N/releases/latest     {"tag_name": ...} of the first release in /log/releases.json (a list
      as the API gives it; none: []) that is neither a draft nor a pre-release, as GitHub's latest; none: a 404.
      /log/latest-raw: its text instead. /log/releases-fail: an HTTP error, the status in it (empty: 403).
  .../vV/SHA256SUMS, .../vV/t3-V-linux-A.tar.gz   T3's release files, made here alike every time (a folder
      t3-V-linux-A with t3 in it), the archive fetches logged to /log/install. In /log: install-fail (a 404 for the
      archive), install-hang (it never ends; its sleep's pid in hang.pid), sums-bad (SHA256SUMS names another
      SHA-256), install-loose (modes as upstream's tar leaves them, a link inside, a setuid file), install-link-out
      (a link to a file outside), tar-outside (a member outside the folder), tar-through-link (a file unpacked
      through a link the archive made), tar-hardlink-out (a hard link to a file outside).
  https://api.github.com/repos/O/N/attestations/sha256:HEX   what fake_sigstore.answer() gives with the keys in
      /log/sigstore (none: a 404). /log/sigstore/by-url: the bundle is not inline but at a bundle_url (snappy, as
      GitHub stores them). /log/attestations-fail: an HTTP error, the status in it.
  http://127.0.0.1:PORT/...   T3's answer on its port: the version in the ExecStart the running T3 unit started with
      (none while it is not active, or /log/http-fail; nor for a version named in /log/http-mute: one that starts
      but never answers).
"""
import gzip, hashlib, io, json, os, re, subprocess, sys, tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_sigstore  # noqa: E402

R, ARGS = sys.argv[1], sys.argv[2:]
LOG = R + '/log'


def has(name):
    return os.path.exists(f'{LOG}/{name}')


def text(name, default=''):
    try:
        with open(f'{LOG}/{name}', encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        return default


def fail(status):
    sys.stderr.write(f'curl: (22) The requested URL returned error: {status} (test)\n')
    sys.exit(22)


def give(data):
    """data to -o's file, else stdout."""
    data = data.encode() if isinstance(data, str) else data
    if DEST:
        with open(DEST, 'wb') as f:
            f.write(data)
    else:
        sys.stdout.buffer.write(data)
    sys.exit(0)


def archive(version, arch):
    """The release archive t3-VERSION-linux-ARCH.tar.gz (the same bytes each time for the same markers)."""
    stem = f't3-{version}-linux-{arch}'
    buf = io.BytesIO()

    def add(tar, name, kind=tarfile.REGTYPE, mode=0o644, data=b'', link=''):
        info = tarfile.TarInfo(name)
        info.type, info.mode, info.linkname, info.size, info.mtime = kind, mode, link, len(data), 0
        tar.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    with gzip.GzipFile(fileobj=buf, mode='wb', mtime=0) as gz, tarfile.open(fileobj=gz, mode='w') as tar:
        add(tar, stem, tarfile.DIRTYPE, 0o755)
        # a run of it is noted in /log/t3-ran (root never runs what it downloaded)
        add(tar, stem + '/t3', mode=0o755,
            data=f'#!/bin/sh\n# t3 {version}\n: >>"${{0%/opt/pc-agents/*}}/log/t3-ran"\n'.encode())
        if has('install-loose'):
            add(tar, stem + '/lib', tarfile.DIRTYPE, 0o775)
            add(tar, stem + '/lib/x', tarfile.DIRTYPE, 0o775)
            add(tar, stem + '/lib/x/m.js', mode=0o664, data=b'x\n')
            add(tar, stem + '/lib/y', tarfile.SYMTYPE, 0o777, link='x')
            add(tar, stem + '/lib/s', mode=0o4775, data=b's\n')
        if has('install-link-out'):
            add(tar, stem + '/out', tarfile.SYMTYPE, 0o777, link=LOG + '/outside')
        if has('tar-outside'):
            add(tar, f'{stem}/../evil', data=b'evil\n')
        if has('tar-through-link'):   # a link inside, then a file written through it (CVE-2025-4138's way)
            add(tar, stem + '/inner', tarfile.DIRTYPE, 0o755)
            add(tar, stem + '/l', tarfile.SYMTYPE, 0o777, link='inner')
            add(tar, stem + '/l/f', data=b'f\n')
        if has('tar-hardlink-out'):
            add(tar, stem + '/h', tarfile.LNKTYPE, 0o644, link='/etc/passwd')
    return buf.getvalue()


def sums(version):
    lines = []
    for arch in ('x64', 'arm64'):
        name = f't3-{version}-linux-{arch}.tar.gz'
        digest = '0' * 64 if has('sums-bad') else hashlib.sha256(archive(version, arch)).hexdigest()
        lines.append(f'{digest}  {name}\n')
    return ''.join(lines)


DEST, URL = None, ARGS[-1]
for i, a in enumerate(ARGS[:-1]):
    if a == '-o':
        DEST = ARGS[i + 1]
with open(f'{LOG}/curl', 'a', encoding='utf-8') as f:
    f.write(' '.join(ARGS) + '\n')

if m := re.fullmatch(r'https://github\.com/[^/]+/[^/]+/releases/latest', URL):
    if has('releases-fail'):
        fail(text('releases-fail') or 403)
    if has('latest-raw'):
        give(text('latest-raw'))
    found = next((r for r in json.loads(text('releases.json', '[]'))
                  if not r.get('draft') and not r.get('prerelease')), None)
    if found is None:
        fail(404)
    give(json.dumps({'id': 1, 'tag_name': found['tag_name'], 'update_url': '/x', 'update_authenticity_token': 'x'}))
if m := re.fullmatch(r'https://[^ ]+/v([^/]+)/(SHA256SUMS|t3-([^/]+)-linux-(x64|arm64)\.tar\.gz)', URL):
    version = m[1]
    if m[2] == 'SHA256SUMS':
        give(sums(version))
    if m[3] != version:
        fail(404)
    with open(f'{LOG}/install', 'a', encoding='utf-8') as f:
        f.write(f'{version} {m[4]}\n')
    if has('install-fail'):
        fail(404)
    if has('install-hang'):   # a download that never ends (the group kill must end it)
        p = subprocess.Popen(['sleep', '60'])
        with open(f'{LOG}/hang.pid', 'w', encoding='utf-8') as f:
            f.write(f'{p.pid}\n')
        p.wait()
    give(archive(version, m[4]))
if m := re.fullmatch(r'https://api\.github\.com/repos/[^/]+/[^/]+/attestations/sha256:([0-9a-f]{64})', URL):
    if has('attestations-fail'):
        fail(text('attestations-fail'))
    if not has('sigstore'):
        fail(404)
    got = fake_sigstore.answer(f'{LOG}/sigstore', m[1])
    if got is None:
        fail(404)
    if has('sigstore/by-url'):
        for item in got['attestations']:
            with open(f'{LOG}/sigstore/{m[1]}.json.sn', 'wb') as f:
                f.write(fake_sigstore.snappy(json.dumps(item['bundle']).encode()))
            item['bundle'], item['bundle_url'] = None, f'https://attestations.example/{m[1]}.json.sn'
    give(json.dumps(got))
if m := re.fullmatch(r'https://attestations\.example/([0-9a-f]{64})\.json\.sn', URL):
    try:
        with open(f'{LOG}/sigstore/{m[1]}.json.sn', 'rb') as f:
            give(f.read())
    except FileNotFoundError:
        fail(404)
if re.match(r'http://127\.0\.0\.1:\d+/', URL):
    active = f'{LOG}/units/t3code-agents.service.active'
    if not os.path.exists(active) or has('http-fail'):
        sys.stderr.write('curl: (7) Failed to connect (test)\n')
        sys.exit(7)
    with open(active, encoding='utf-8') as f:
        v = next((m[1] for line in f if (m := re.match(r'ExecStart=.*/runtime/versions/([^/]*)/t3 ', line))), '')
    if v and v in text('http-mute').splitlines():
        sys.stderr.write('curl: (28) Operation timed out (test)\n')
        sys.exit(28)
    give(json.dumps({'serverVersion': v, 'httpBaseUrl': '/'}) + '\n')
sys.stderr.write(f'curl: not a URL of this test: {URL}\n')
sys.exit(2)
