"""Check a GitHub build attestation (a Sigstore bundle) of a release file, with the standard library and the openssl
program only (fork-kit/pc-agents/README.md "Automatic updates"). Fails closed: anything not understood is a refusal.

What a pass means: the Sigstore public-good certificate authority (Fulcio, its root pinned in sigstore-trust.json)
gave the signing key to a GitHub Actions run of exactly the workflow, repository and branch the policy names, at the
time the public transparency log (Rekor, its key pinned there too) recorded, and that key signed an in-toto statement
naming the file by name and SHA-256. A release file anyone uploaded by hand, or a build from another workflow, branch
or repository, has no such attestation."""
import base64, hashlib, json, os

# Fulcio's certificate extensions (github.com/sigstore/fulcio docs/oid-info.md): DER UTF8String values.
ISSUER = '1.3.6.1.4.1.57264.1.8'
RUNNER = '1.3.6.1.4.1.57264.1.11'
SOURCE_REPO = '1.3.6.1.4.1.57264.1.12'
SOURCE_REF = '1.3.6.1.4.1.57264.1.14'
BUILD_CONFIG = '1.3.6.1.4.1.57264.1.18'
SAN = '2.5.29.17'
GITHUB_OIDC = 'https://token.actions.githubusercontent.com'


def unsnappy(data):
    """Snappy's raw block format (the bundles GitHub stores, .json.sn), decoded."""
    n = shift = i = 0
    while True:
        if i >= len(data) or shift > 35:
            raise ValueError('bad snappy length')
        b = data[i]
        i += 1
        n |= (b & 0x7f) << shift
        shift += 7
        if not b & 0x80:
            break
    if n > 64 << 20:
        raise ValueError('snappy block too large')
    out = bytearray()
    while i < len(data):
        tag = data[i]
        i += 1
        kind = tag & 3
        if kind == 0:
            size = tag >> 2
            if size >= 60:
                extra = size - 59
                size = int.from_bytes(data[i:i + extra], 'little')
                i += extra
            size += 1
            if i + size > len(data):
                raise ValueError('snappy literal past the end')
            out += data[i:i + size]
            i += size
            continue
        if kind == 1:
            size, off = 4 + ((tag >> 2) & 7), ((tag >> 5) << 8) | data[i]
            i += 1
        elif kind == 2:
            size, off = (tag >> 2) + 1, int.from_bytes(data[i:i + 2], 'little')
            i += 2
        else:
            size, off = (tag >> 2) + 1, int.from_bytes(data[i:i + 4], 'little')
            i += 4
        if not 0 < off <= len(out) or len(out) + size > n:
            raise ValueError('bad snappy copy')
        for _ in range(size):
            out.append(out[-off])
    if len(out) != n:
        raise ValueError('snappy length does not match')
    return bytes(out)


def _tlv(data, i):
    """(tag, value, next index) of the DER element at i."""
    if i + 2 > len(data):
        raise ValueError('DER past the end')
    tag, size = data[i], data[i + 1]
    i += 2
    if size & 0x80:
        count = size & 0x7f
        if not 0 < count <= 4:
            raise ValueError('bad DER length')
        size = int.from_bytes(data[i:i + count], 'big')
        i += count
    if i + size > len(data):
        raise ValueError('DER past the end')
    return tag, data[i:i + size], i + size


def _items(data):
    i, out = 0, []
    while i < len(data):
        tag, value, i = _tlv(data, i)
        out.append((tag, value))
    return out


def _oid(value):
    first, parts, n = value[0], [], 0
    parts += [str(min(first // 40, 2)), str(first - 40 * min(first // 40, 2))]
    for b in value[1:]:
        n = (n << 7) | (b & 0x7f)
        if not b & 0x80:
            parts.append(str(n))
            n = 0
    return '.'.join(parts)


def extensions(der):
    """{oid: value bytes} of a certificate's extensions (DER)."""
    tag, cert, _ = _tlv(der, 0)
    tag2, tbs, _ = _tlv(cert, 0)
    if tag != 0x30 or tag2 != 0x30:
        raise ValueError('not a certificate')
    exts = [v for t, v in _items(tbs) if t == 0xa3]
    if len(exts) != 1:
        raise ValueError('no extensions')
    out = {}
    for t, ext in _items(_items(exts[0])[0][1]):
        fields = _items(ext)
        oid = _oid(fields[0][1])
        if oid in out:
            raise ValueError(f'extension {oid} twice')
        out[oid] = fields[-1][1]
    return out


def claims(der):
    """The identity a Fulcio certificate states: {'san': [URIs], oid: text} for the extensions in this module."""
    exts = extensions(der)
    out = {'san': [v.decode() for t, v in _items(_items(exts.get(SAN, b'0\x00'))[0][1]) if t == 0x86]}
    for oid in (ISSUER, RUNNER, SOURCE_REPO, SOURCE_REF, BUILD_CONFIG):
        if oid in exts:
            tag, value, _ = _tlv(exts[oid], 0)
            if tag != 0x0c:
                raise ValueError(f'extension {oid} is not text')
            out[oid] = value.decode()
    return out


def github_policy(repo, workflow, builders, ref='refs/heads/main'):
    """What a certificate must state for a build of repo's workflow file (a name under .github/workflows) on ref,
    started by one of builders (workflow file names: the workflow itself, or one that calls it)."""
    base = f'https://github.com/{repo}'.lower()
    return {'san': [f'{base}/.github/workflows/{workflow}@{ref}'.lower()],
            ISSUER: [GITHUB_OIDC], RUNNER: ['github-hosted'], SOURCE_REPO: [base], SOURCE_REF: [ref],
            BUILD_CONFIG: [f'{base}/.github/workflows/{b}@{ref}'.lower() for b in builders]}


def _pem(der, label='CERTIFICATE'):
    body = base64.b64encode(der).decode()
    return f'-----BEGIN {label}-----\n' + '\n'.join(body[i:i + 64] for i in range(0, len(body), 64)) + \
        f'\n-----END {label}-----\n'


def _b64(text):
    return base64.b64decode(text, validate=True)


def verify(bundle, name, digest, policy, trust, work, run):
    """Why bundle (a parsed Sigstore bundle) does not attest the file name with SHA-256 digest (hex) under policy
    (github_policy) and trust (sigstore-trust.json), or None. work: an empty folder only root can reach, for openssl's
    inputs. run(argv) -> CompletedProcess (text): openssl is argv[0]."""
    try:
        return _verify(bundle, name, digest, policy, trust, work, run)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, UnicodeError) as e:
        return f'not a bundle this kit can read ({e.__class__.__name__}: {e})'


def _write(work, name, data):
    path = os.path.join(work, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data.encode() if isinstance(data, str) else data)
    return path


def _signed(run, key, signature, data, work, what):
    """Why openssl does not find signature (DER) by the public key (PEM) over data, or None."""
    r = run(['/usr/bin/openssl', 'dgst', '-sha256', '-verify', _write(work, f'{what}.key', key),
             '-signature', _write(work, f'{what}.sig', signature), _write(work, f'{what}.data', data)])
    return None if r.returncode == 0 and r.stdout.strip() == 'Verified OK' else \
        f'the {what} signature does not verify ({(r.stderr or r.stdout).strip()[-200:]})'


def _verify(bundle, name, digest, policy, trust, work, run):
    if not str(bundle.get('mediaType', '')).startswith('application/vnd.dev.sigstore.bundle'):
        return f'not a Sigstore bundle ({str(bundle.get("mediaType"))[:80]})'
    material = bundle['verificationMaterial']
    cert = material.get('certificate') or (material.get('x509CertificateChain') or {}).get('certificates', [None])[0]
    if not cert:
        return 'no signing certificate (a public key only: not a Fulcio-issued GitHub Actions identity)'
    leaf = _b64(cert['rawBytes'])
    envelope = bundle['dsseEnvelope']
    payload, kind = _b64(envelope['payload']), envelope['payloadType']
    if len(envelope['signatures']) != 1:
        return 'not one signature'
    signature = _b64(envelope['signatures'][0]['sig'])

    # 1. The transparency log recorded this signature, by this certificate, at integratedTime (its signed promise).
    entries = material.get('tlogEntries') or []
    if len(entries) != 1:
        return f'{len(entries)} transparency log entries, not one'
    entry = entries[0]
    if _b64(entry['logId']['keyId']).hex() != trust['rekor']['keyId']:
        return 'its transparency log is not the pinned Rekor log'
    if entry['kindVersion'] != {'kind': 'dsse', 'version': '0.0.1'}:
        return f'a log entry of kind {entry["kindVersion"]}, not dsse 0.0.1'
    when = int(entry['integratedTime'])
    promise = json.dumps({'body': entry['canonicalizedBody'], 'integratedTime': when,
                          'logID': trust['rekor']['keyId'], 'logIndex': int(entry['logIndex'])},
                         sort_keys=True, separators=(',', ':'))
    if why := _signed(run, trust['rekor']['key'], _b64(entry['inclusionPromise']['signedEntryTimestamp']),
                      promise, work, 'log'):
        return why
    body = json.loads(_b64(entry['canonicalizedBody']))
    if (body.get('kind'), body.get('apiVersion')) != ('dsse', '0.0.1'):
        return 'the log entry is not a dsse 0.0.1 entry'
    spec = body['spec']
    logged = spec['signatures']
    if spec['payloadHash'] != {'algorithm': 'sha256', 'value': hashlib.sha256(payload).hexdigest()}:
        return 'the log entry is for another statement'
    if len(logged) != 1 or _b64(logged[0]['signature']) != signature:
        return 'the log entry is for another signature'
    pem = _b64(logged[0]['verifier']).decode()
    if _b64(''.join(pem.strip().splitlines()[1:-1])) != leaf:
        return 'the log entry is for another certificate'

    # 2. The certificate chains to the pinned Fulcio root, and was valid when the log recorded the signature.
    run_at = ['-attime', str(when)]
    if when < _epoch(trust['fulcio']['start']) or when < _epoch(trust['rekor']['start']):
        return 'recorded before the pinned authorities were valid'
    r = run(['/usr/bin/openssl', 'verify', *run_at, '-purpose', 'any', '-x509_strict',
             '-CAfile', _write(work, 'root.pem', trust['fulcio']['root']),
             '-untrusted', _write(work, 'intermediates.pem', ''.join(trust['fulcio']['intermediates'])),
             _write(work, 'leaf.pem', _pem(leaf))])
    if r.returncode or not r.stdout.strip().endswith(': OK'):
        return f'the certificate does not chain to the pinned Fulcio root ({(r.stderr or r.stdout).strip()[-200:]})'

    # 3. Who it was given to: the policy's repository, workflow, branch and builders, on GitHub's own runners.
    got = claims(leaf)
    for key, allowed in policy.items():
        values = [v.lower() for v in got['san']] if key == 'san' else [got.get(key, '').lower()]
        if not values or any(v not in allowed for v in values):
            return f'signed for {got["san"] if key == "san" else got.get(key)!r}, not {allowed} ({key})'

    # 4. The certificate's key signed the statement.
    r = run(['/usr/bin/openssl', 'x509', '-pubkey', '-noout', '-in', os.path.join(work, 'leaf.pem')])
    if r.returncode:
        return f'cannot read the certificate key ({r.stderr.strip()[-200:]})'
    pae = b'DSSEv1 %d %s %d %s' % (len(kind.encode()), kind.encode(), len(payload), payload)
    if why := _signed(run, r.stdout, signature, pae, work, 'statement'):
        return why

    # 5. The statement names this file by name and digest.
    if kind != 'application/vnd.in-toto+json':
        return f'a statement of type {kind}'
    statement = json.loads(payload)
    if statement.get('_type') != 'https://in-toto.io/Statement/v1' or \
            statement.get('predicateType') != 'https://slsa.dev/provenance/v1':
        return 'not an in-toto SLSA provenance statement'
    if not any(s.get('name') == name and (s.get('digest') or {}).get('sha256') == digest
               for s in statement.get('subject', [])):
        return f'its statement does not name {name} with SHA-256 {digest}'
    return None


def _epoch(stamp):
    import datetime
    return int(datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp())
