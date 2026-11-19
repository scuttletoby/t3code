"""A fake Sigstore for the pc-agents tests, made with the openssl program: a root and an intermediate certificate
authority (Fulcio's), a transparency log key (Rekor's), leaf certificates with Fulcio's GitHub Actions extensions, and
bundles as actions/attest-build-provenance makes them (an in-toto SLSA provenance statement in a DSSE envelope, one
Rekor entry with its signed promise), signed on the fly.

fake_curl.py serves answer(DIR, HEX) for GitHub's attestations API (sha256:HEX).

DIR holds the keys (ca(DIR) makes them) and trust.json, the kit's sigstore-trust.json for them. DIR/fault names one
fault of FAULTS (none: a good bundle), to test that the kit refuses each."""
import atexit, base64, hashlib, json, os, shutil, subprocess, tempfile, time

FORK = 'example/t3code'
GITHUB = 'https://github.com/' + FORK
WORKFLOWS = GITHUB + '/.github/workflows/'
OIDC = 'https://token.actions.githubusercontent.com'
# What a good leaf certificate states (Fulcio's extensions; SAN: the workflow that ran, job_workflow_ref)
GOOD = {'san': WORKFLOWS + 'fork-release.yml@refs/heads/main', '1.8': OIDC, '1.11': 'github-hosted',
        '1.12': GITHUB, '1.14': 'refs/heads/main', '1.18': WORKFLOWS + 'fork-follow.yml@refs/heads/main'}
CLAIMS = {  # a fault in who the certificate was given to
    'branch': {'san': WORKFLOWS + 'fork-release.yml@refs/heads/side', '1.14': 'refs/heads/side',
               '1.18': WORKFLOWS + 'fork-release.yml@refs/heads/side'},
    'workflow': {'san': WORKFLOWS + 'build.yml@refs/heads/main', '1.18': WORKFLOWS + 'build.yml@refs/heads/main'},
    'caller': {'1.18': WORKFLOWS + 'other.yml@refs/heads/main'},   # fork-release.yml, called by another workflow
    'repo': {'san': 'https://github.com/other/t3code/.github/workflows/fork-release.yml@refs/heads/main',
             '1.12': 'https://github.com/other/t3code',
             '1.18': 'https://github.com/other/t3code/.github/workflows/fork-release.yml@refs/heads/main'},
    'runner': {'1.11': 'self-hosted'},
    'issuer': {'1.8': 'https://issuer.example'},
}
FAULTS = set(CLAIMS) | {
    'missing',    # no attestation for the digest (a file uploaded by hand)
    'github',     # GitHub's own release attestation: no transparency log entry
    'digest',     # the statement names another digest
    'name',       # the statement names another file
    'statement',  # the envelope signed by another key than the certificate's
    'promise',    # the log's promise signed by another key than the pinned Rekor key
    'log',        # an entry of another log
    'ca',         # a certificate from another authority
    'time',       # the certificate was not valid when the log recorded the signature
}


def openssl(*args, data=None):
    r = subprocess.run(['openssl', *args], input=data, capture_output=True)
    if r.returncode:
        raise RuntimeError(f'openssl {" ".join(args)}: {r.stderr.decode()[-400:]}')
    return r.stdout


def key(path):
    openssl('genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:P-256', '-out', path)


def cert(d, name, issuer, exts, days=3650, subject=None):
    """d/name.pem: a certificate for d/name.key, issued by d/issuer (self-signed when issuer is None)."""
    with open(f'{d}/{name}.cnf', 'w') as f:
        f.write('[req]\ndistinguished_name=dn\n[dn]\n[ext]\n' + exts)
    subject = subject or f'/O=pc-agents test/CN={name}'
    if issuer is None:
        openssl('req', '-x509', '-new', '-key', f'{d}/{name}.key', '-subj', subject, '-days', str(days),
                '-config', f'{d}/{name}.cnf', '-extensions', 'ext', '-out', f'{d}/{name}.pem')
        return
    openssl('req', '-new', '-key', f'{d}/{name}.key', '-subj', subject, '-config', f'{d}/{name}.cnf',
            '-out', f'{d}/{name}.csr')
    openssl('x509', '-req', '-in', f'{d}/{name}.csr', '-CA', f'{d}/{issuer}.pem', '-CAkey', f'{d}/{issuer}.key',
            '-set_serial', str(int.from_bytes(os.urandom(8), 'big')), '-days', str(days), '-extfile',
            f'{d}/{name}.cnf', '-extensions', 'ext', '-out', f'{d}/{name}.pem')


CA_EXT = 'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n'


def read(path):
    with open(path) as f:
        return f.read()


_made = None


def ca(d):
    """Make the authorities and the good leaf in d (once per test process, then copied), and d/trust.json."""
    global _made
    if _made is None:
        _made = tempfile.mkdtemp(prefix='pc-agents-sigstore-')
        atexit.register(shutil.rmtree, _made, True)
        for name in ('root', 'intermediate', 'other-root', 'rekor', 'other-rekor'):
            key(f'{_made}/{name}.key')
        cert(_made, 'root', None, CA_EXT)
        cert(_made, 'other-root', None, CA_EXT)
        cert(_made, 'intermediate', 'root', CA_EXT + 'authorityKeyIdentifier=keyid:always\n')
        for name in ('rekor', 'other-rekor'):
            with open(f'{_made}/{name}.pub', 'wb') as f:
                f.write(openssl('pkey', '-in', f'{_made}/{name}.key', '-pubout'))
        leaf(_made, '')
    os.makedirs(d, exist_ok=True)
    for n in os.listdir(_made):
        shutil.copy(f'{_made}/{n}', d)
    der = openssl('pkey', '-pubin', '-in', f'{d}/rekor.pub', '-outform', 'DER')
    trust = {'fulcio': {'start': '2022-04-13T20:06:15Z', 'intermediates': [read(f'{d}/intermediate.pem')],
                        'root': read(f'{d}/root.pem')},
             'rekor': {'start': '2021-01-12T11:53:27Z', 'keyId': hashlib.sha256(der).hexdigest(),
                       'key': read(f'{d}/rekor.pub')}}
    with open(f'{d}/trust.json', 'w') as f:
        json.dump(trust, f)
    return f'{d}/trust.json'


def leaf(d, fault):
    """d/leaf-FAULT.pem (and .key): a Fulcio-style leaf certificate, made once."""
    name = f'leaf-{fault}'
    if os.path.exists(f'{d}/{name}.pem'):
        return name
    claims = {**GOOD, **CLAIMS.get(fault, {})}
    exts = ''.join(f'1.3.6.1.4.1.57264.{oid}=ASN1:UTF8String:{v}\n' for oid, v in claims.items() if oid != 'san')
    exts += (f'subjectAltName=critical,URI:{claims["san"]}\nkeyUsage=critical,digitalSignature\n'
             'extendedKeyUsage=codeSigning\nauthorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n')
    key(f'{d}/{name}.key')
    issuer = 'other-root' if fault == 'ca' else 'intermediate'
    cert(d, name, issuer, exts, days=1 if fault == 'time' else 3650, subject='/O=sigstore.dev')
    return name


def b64(data):
    return base64.b64encode(data.encode() if isinstance(data, str) else data).decode()


def signed(d, key_name, data):
    with tempfile.NamedTemporaryFile(dir=d) as f:
        f.write(data)
        f.flush()
        return openssl('dgst', '-sha256', '-sign', f'{d}/{key_name}.key', f.name)


def bundle(d, digest, fault=''):
    """A bundle for the release files t3-*-linux-{x64,arm64}.tar.gz with SHA-256 digest (hex)."""
    name = leaf(d, fault)
    der = openssl('x509', '-in', f'{d}/{name}.pem', '-outform', 'DER')
    # the fake serves every version's archive alike: the statement names them all, as one run's statement does
    names = ['other.tar.gz'] if fault == 'name' else [f't3-{v}-linux-{a}.tar.gz' for v in VERSIONS
                                                      for a in ('x64', 'arm64')]
    subjects = [{'name': n, 'digest': {'sha256': '0' * 64 if fault == 'digest' else digest}} for n in names]
    statement = json.dumps({'_type': 'https://in-toto.io/Statement/v1', 'subject': subjects,
                            'predicateType': 'https://slsa.dev/provenance/v1', 'predicate': {}}).encode()
    kind = 'application/vnd.in-toto+json'
    pae = b'DSSEv1 %d %s %d %s' % (len(kind), kind.encode(), len(statement), statement)
    sig = signed(d, 'other-rekor' if fault == 'statement' else name, pae)
    body = json.dumps({'apiVersion': '0.0.1', 'kind': 'dsse', 'spec': {
        'envelopeHash': {'algorithm': 'sha256', 'value': hashlib.sha256(statement).hexdigest()},
        'payloadHash': {'algorithm': 'sha256', 'value': hashlib.sha256(statement).hexdigest()},
        'signatures': [{'signature': b64(sig), 'verifier': b64(read(f'{d}/{name}.pem'))}]}})
    trust = json.loads(read(f'{d}/trust.json'))
    log_id = hashlib.sha256(b'another log').hexdigest() if fault == 'log' else trust['rekor']['keyId']
    when = int(time.time()) + (3 * 86400 if fault == 'time' else 60)   # after the leaf was made
    promise = json.dumps({'body': b64(body), 'integratedTime': when, 'logID': log_id, 'logIndex': 7},
                         sort_keys=True, separators=(',', ':')).encode()
    entry = {'logIndex': '7', 'logId': {'keyId': b64(bytes.fromhex(log_id))},
             'kindVersion': {'kind': 'dsse', 'version': '0.0.1'}, 'integratedTime': str(when),
             'inclusionPromise': {'signedEntryTimestamp': b64(signed(d, 'other-rekor' if fault == 'promise' else
                                                                     'rekor', promise))},
             'canonicalizedBody': b64(body)}
    return {'mediaType': 'application/vnd.dev.sigstore.bundle.v0.3+json',
            'verificationMaterial': {'certificate': {'rawBytes': b64(der)},
                                     'tlogEntries': [] if fault == 'github' else [entry]},
            'dsseEnvelope': {'payload': b64(statement), 'payloadType': kind, 'signatures': [{'sig': b64(sig)}]}}


VERSIONS = ('1.2.3', '1.2.4', '1.2.5', '1.2.6', '1.2.7', '1.3.0')


def snappy(data):
    """data in Snappy's raw block format, as literals only (what GitHub's .json.sn bundles are; any encoder's output
    decodes the same)."""
    n, out = len(data), bytearray()
    while True:
        out.append(n & 0x7f | (0x80 if n > 0x7f else 0))
        n >>= 7
        if not n:
            break
    for i in range(0, len(data), 1 << 16):
        chunk = data[i:i + (1 << 16)]
        out += bytes([61 << 2]) + (len(chunk) - 1).to_bytes(2, 'little') + chunk
    return bytes(out)


def answer(d, digest):
    """What GitHub's attestations API answers for sha256:digest (None: a 404)."""
    fault = read(f'{d}/fault').strip() if os.path.exists(f'{d}/fault') else ''
    if fault == 'missing':
        return None
    return {'attestations': [{'repository_id': 1, 'bundle': bundle(d, digest, fault)}]}
