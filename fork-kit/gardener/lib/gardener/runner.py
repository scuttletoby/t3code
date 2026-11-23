"""gardener.runner: run one check under the contract (gardener.contract) and turn what it did into findings.

A check that fails in any way (cannot start, exits non-zero, runs past its timeout, prints junk or too much)
becomes exactly one finding, id gardener.check-failed, and never stops the run: the next check still runs.
"""
import json, os, signal, subprocess, sys

from gardener import config, contract

FAILED = 'gardener.check-failed'


def command(check):
    if check['name']:
        return [sys.executable, '-I', '-B', os.path.join(config.CHECKS_DIR, check['name'])]
    return [config.expand(check['path'])]


def context(project, check, today, gh):
    return {'version': contract.VERSION,
            'project': {'name': project['name'], 'repo': project['repo'],
                        'root': config.expand(project['root']) if project['root'] else ''},
            'check': check['id'], 'options': check['options'], 'fix': check['fix'], 'today': today, 'gh': gh}


def failed(check, text, fix=''):
    return {'id': FAILED, 'severity': 'error', 'where': f'check {check["id"]}', 'text': text,
            'fix': fix or 'run the check by hand with the same context and fix it, or remove it from the config',
            'check': check['id']}


def _last_line(data):
    lines = [ln.strip() for ln in data.decode('utf-8', 'replace').splitlines() if ln.strip()]
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


def run(project, check, today, gh):
    """(findings, ok): the check's findings with "check" set to its id; ok is False when it failed."""
    ctx = json.dumps(context(project, check, today, gh)).encode()
    root = config.expand(project['root']) if project['root'] else None
    if root and not os.path.isdir(root):
        return [failed(check, f'the project root {project["root"]} is not a folder', 'fix "root" in the config')], \
            False
    try:
        proc = subprocess.Popen(command(check), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=root, start_new_session=True, close_fds=True)
    except OSError as e:
        return [failed(check, f'cannot start: {e.strerror or e}')], False
    try:
        out, err = proc.communicate(ctx, timeout=check['timeoutSeconds'])
    except subprocess.TimeoutExpired:
        _kill(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:     # a child that left the group still holds the pipes
            pass
        for f in (proc.stdout, proc.stderr):
            try:
                f.close()
            except OSError:
                pass
        return [failed(check, f'stopped after its timeout of {check["timeoutSeconds"]} s (timeoutSeconds)',
                       'make the check faster, or raise timeoutSeconds')], False
    finally:
        try:                                   # nothing the check started outlives it
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    if proc.returncode != 0:
        why = _last_line(err)
        return [failed(check, f'exited with code {proc.returncode}' + (f': {why}' if why else ''))], False
    if len(out) > contract.MAX_OUTPUT:
        return [failed(check, f'printed more than {contract.MAX_OUTPUT} bytes')], False
    findings = []
    for n, line in enumerate(out.decode('utf-8', 'replace').splitlines(), 1):
        if not line.strip():
            continue
        try:
            f = contract.parse_finding(line)
        except ValueError as e:
            return [failed(check, f'printed a line that is not a finding (line {n}: {e}): {line.strip()[:120]!r}',
                           'a check prints only finding lines on stdout (README, Checks); notes go to stderr')], \
                False
        f['check'] = check['id']
        findings.append(f)
    if len(findings) > contract.MAX_FINDINGS:
        more = len(findings) - contract.MAX_FINDINGS
        findings = findings[:contract.MAX_FINDINGS] + [{
            'id': 'gardener.more-findings', 'severity': 'info', 'where': f'check {check["id"]}',
            'text': f'{more} more finding(s) not shown', 'fix': 'run the check by hand to see them all',
            'check': check['id']}]
    return findings, True
