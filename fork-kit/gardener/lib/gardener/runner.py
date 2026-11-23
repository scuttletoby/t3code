"""gardener.runner: run one check under the contract (gardener.contract) and turn what it did into findings.

A check that fails in any way (cannot start, exits non-zero, runs past its timeout, prints junk or too much)
becomes exactly one finding, id gardener.check-failed, and never stops the run: the next check still runs.
Its output is read as it comes, never more than contract.MAX_OUTPUT bytes of stdout and the last 4 KiB of stderr,
so a check that prints without end costs no memory: it is stopped at the cap.

The text and fix of the gardener's own findings are fixed words written here, naming no local thing. What the check
printed on stderr, or why it could not start, is the finding's `detail`: the local report and log only, never the
issue.
"""
import json, os, selectors, signal, subprocess, sys, time

from gardener import config, contract

FAILED = 'gardener.check-failed'
ERR_KEEP = 4096
CHUNK = 65536


def command(check):
    if check['name']:
        return [sys.executable, '-I', '-B', os.path.join(config.CHECKS_DIR, check['name'])]
    return [config.expand(check['path'])]


def context(project, check, today, gh):
    return {'version': contract.VERSION,
            'project': {'name': project['name'], 'repo': project['repo'],
                        'root': config.expand(project['root']) if project['root'] else ''},
            'check': check['id'], 'options': check['options'], 'fix': check['fix'], 'today': today, 'gh': gh}


def failed(check, text, detail='', fix=''):
    """The one finding of a failed check. text and fix: fixed words of the gardener (they go to the issue);
    detail: what the check or the system said (local report and log only)."""
    return {'id': FAILED, 'severity': 'error', 'where': '', 'text': text, 'detail': detail, 'own': True,
            'fix': fix or 'run the check by hand with the same context and fix it, or remove it from the config',
            'check': check['id']}


def _last_line(data):
    lines = [ln.strip() for ln in bytes(data).decode('utf-8', 'replace').splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ''


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _talk(proc, data, deadline):
    """Write data to the check's stdin and read its stdout and stderr until both close, the deadline passes or
    stdout passes MAX_OUTPUT. (stdout bytes, last stderr bytes, None, 'timeout' or 'too-much')."""
    out, err = bytearray(), bytearray()
    sel = selectors.DefaultSelector()
    try:
        pending = memoryview(data)
        os.set_blocking(proc.stdin.fileno(), False)
        sel.register(proc.stdin, selectors.EVENT_WRITE, 'in')
        sel.register(proc.stdout, selectors.EVENT_READ, 'out')
        sel.register(proc.stderr, selectors.EVENT_READ, 'err')
        reading = 2
        while reading:
            left = deadline - time.monotonic()
            if left <= 0:
                return out, err, 'timeout'
            for key, _ in sel.select(left):
                if key.data == 'in':
                    try:
                        pending = pending[os.write(key.fd, pending[:CHUNK]):]
                    except BlockingIOError:
                        continue
                    except OSError:                    # the check closed its stdin: it does not want the rest
                        pending = pending[len(pending):]
                    if not len(pending):
                        sel.unregister(proc.stdin)
                        proc.stdin.close()
                    continue
                chunk = os.read(key.fd, CHUNK)
                if not chunk:
                    sel.unregister(key.fileobj)
                    reading -= 1
                elif key.data == 'out':
                    out += chunk
                    if len(out) > contract.MAX_OUTPUT:
                        return out, err, 'too-much'
                else:
                    err += chunk
                    if len(err) > 2 * ERR_KEEP:
                        del err[:-ERR_KEEP]
        try:
            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:          # closed its output but runs on
            return out, err, 'timeout'
        return out, err, None
    finally:
        sel.close()


def run(project, check, today, gh):
    """(findings, ok): the check's findings with "check" set to its id; ok is False when it failed."""
    ctx = json.dumps(context(project, check, today, gh)).encode()
    root = config.expand(project['root']) if project['root'] else None
    if root and not os.path.isdir(root):
        return [failed(check, 'the project root is not a folder', f'root {project["root"]}',
                       'fix "root" in the config')], False
    try:
        proc = subprocess.Popen(command(check), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=root, start_new_session=True, close_fds=True)
    except OSError as e:
        return [failed(check, 'cannot start', str(e.strerror or e))], False
    try:
        out, err, why = _talk(proc, ctx, time.monotonic() + check['timeoutSeconds'])
    finally:
        _kill(proc)                            # nothing the check started outlives it
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for f in (proc.stdin, proc.stdout, proc.stderr):
            try:
                f.close()
            except OSError:
                pass
    if why == 'timeout':
        return [failed(check, f'stopped after its timeout of {check["timeoutSeconds"]} s (timeoutSeconds)',
                       _last_line(err), 'make the check faster, or raise timeoutSeconds')], False
    if why == 'too-much':
        return [failed(check, f'printed more than {contract.MAX_OUTPUT} bytes: stopped', _last_line(err),
                       'a check prints only its findings; notes go to stderr')], False
    if proc.returncode != 0:
        return [failed(check, f'exited with code {proc.returncode}', _last_line(err))], False
    findings = []
    for n, line in enumerate(bytes(out).decode('utf-8', 'replace').splitlines(), 1):
        if not line.strip():
            continue
        try:
            f = contract.parse_finding(line)
        except ValueError as e:
            return [failed(check, f'printed a line that is not a finding (line {n})',
                           f'{e}: {line.strip()[:120]!r}',
                           'a check prints only finding lines on stdout (README, Checks); notes go to stderr')], \
                False
        f['check'] = check['id']
        findings.append(f)
    if len(findings) > contract.MAX_FINDINGS:
        more = len(findings) - contract.MAX_FINDINGS
        findings = findings[:contract.MAX_FINDINGS] + [{
            'id': 'gardener.more-findings', 'severity': 'info', 'where': '', 'own': True,
            'text': f'{more} more finding(s) not shown', 'fix': 'run the check by hand to see them all',
            'check': check['id']}]
    return findings, True
